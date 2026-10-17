"""Phase 3 recipe runner: apply a matched active profile's `recipe` through the HA WebSocket API.

SAFETY MODEL
- DRY-RUN IS THE DEFAULT. Mutating commands (MUTATING_TYPES) are only sent when
  Options.apply is True (`--apply` on the CLI, `--provision apply` on the watcher). In dry-run they
  are recorded in the plan with "sent": false and never leave this process. Read-only commands
  (registry/state/dashboard lists) are still sent so the plan reflects real state.
- The decision is made in one place (Session.call), keyed on the command type, so a step cannot
  forget to ask.
- Every step reports ok / skipped / failed. Exceptions inside a step become `failed`; nothing here
  raises into the watcher.
- Idempotent: each step compares current state to the desired state and does nothing (ok,
  changed=false) when they already agree.

CLI: python -m ha_watcher.provision --device-id <id> [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import matcher, notify
from . import placeholders as ph
from .client import AuthFailed, HAClient
from .jsonl import emit, make_capture_logger
from .profiles import Profile, load_profiles

diag = logging.getLogger("ha_watcher")

Caller = Callable[[dict], Awaitable[dict]]

STEPS = ("device_name", "device_area", "entity_id_cleanup", "dashboard_card", "welcome_automation")
MUTATING_TYPES = frozenset({
    "config/device_registry/update",
    "config/entity_registry/update",
    "lovelace/config/save",
    "call_service",
})
# Same rule as homeassistant.core.valid_entity_id
VALID_ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
AUTOMATION_ID = re.compile(r"^automation\.[a-z0-9_]+$")
CARD_VIEW_TYPES = (None, "masonry", "sidebar")  # views that keep a plain `cards:` list

EXIT_OK, EXIT_STEP_FAILED, EXIT_AUTH_FAILED, EXIT_CONFIG_ERROR, EXIT_NO_TARGET = 0, 1, 2, 3, 4


@dataclass
class Options:
    apply: bool = False
    backup_dir: str = "logs/backups"
    force_welcome: bool = False


# --------------------------------------------------------------------------- session

class Session:
    """Wraps the transport. The ONLY place that decides whether a command may be sent."""

    def __init__(self, caller: Caller, apply: bool):
        self.caller, self.apply = caller, apply

    async def call(self, payload: dict, calls: list) -> dict:
        mutating = payload["type"] in MUTATING_TYPES
        entry = {"type": payload["type"], "payload": {k: v for k, v in payload.items() if k != "type"},
                 "mutating": mutating, "sent": False}
        calls.append(entry)
        if mutating and not self.apply:
            entry["dry_run"] = True
            return {"success": True, "result": None, "simulated": True}
        entry["sent"] = True
        msg = await self.caller(payload)
        entry["ok"] = bool(msg.get("success"))
        if not entry["ok"]:
            entry["error"] = msg.get("error")
        return msg

    async def read(self, payload: dict, calls: list):
        """Read-only command; returns result or raises RuntimeError."""
        assert payload["type"] not in MUTATING_TYPES
        msg = await self.call(payload, calls)
        if not msg.get("success"):
            raise RuntimeError(f"{payload['type']} failed: {msg.get('error')}")
        return msg.get("result")


def _step(name: str, status: str, reason: str | None = None, *, changed: bool = False,
          calls: list | None = None, **extra) -> dict:
    d = {"step": name, "status": status, "reason": reason, "changed": changed, "calls": calls or []}
    d.update(extra)
    return d


def _ph_reason(exc: ph.PlaceholderError) -> str:
    if exc.kind == "unresolved" and exc.token in ("area", "area_name"):
        return "area_unresolved"
    return f"{exc.kind}_placeholder:{{{exc.token}}}"


# --------------------------------------------------------------------------- context

class Ctx:
    def __init__(self, device: dict, entity_ids: list[str], areas: list[dict], recipe: dict,
                 registry_ids: set[str], state_ids: set[str] | None, opts: Options):
        self.device = copy.deepcopy(device)
        self.recipe = recipe
        self.areas = areas
        self.opts = opts
        self.area: dict | None = None
        self.entity_map = {e: e for e in entity_ids}  # original id -> current/planned id
        self.registry_ids = registry_ids
        self.state_ids = state_ids
        self.changed_any = False

    @property
    def device_name(self) -> str:
        return self.device.get("name_by_user") or self.device.get("name") or self.device["id"]

    def base_values(self) -> dict:
        v = {"device_name": self.device_name, "name": ph.slugify(self.device_name),
             "manufacturer": self.device.get("manufacturer"), "model": self.device.get("model"),
             "device_id": self.device["id"]}
        if self.area:
            v["area_name"] = self.area["name"]
            v["area"] = ph.slugify(self.area["name"])
        return v

    def entity_values(self) -> dict:
        out = {}
        for eid in sorted(self.entity_map.values()):
            out.setdefault("entity:" + eid.split(".", 1)[0], eid)
        return out


def _find_area(areas: list[dict], wanted: str) -> dict | None:
    w = wanted.strip().casefold()
    for a in areas:
        if str(a.get("name", "")).strip().casefold() == w or \
                any(str(x).strip().casefold() == w for x in a.get("aliases") or []):
            return a
    return None


def glob_regex(pattern: str) -> re.Pattern:
    """`*` and `?` capture (groups 1..n); everything else is literal. Case-insensitive."""
    parts = []
    for ch in pattern.casefold():
        parts.append("(.*)" if ch == "*" else "(.)" if ch == "?" else re.escape(ch))
    return re.compile("^" + "".join(parts) + "$", re.S)


# --------------------------------------------------------------------------- steps

async def step_device_name(sess: Session, ctx: Ctx) -> dict:
    tpl = ctx.recipe.get("name")
    if not tpl:
        return _step("device_name", "skipped", "no_name_in_recipe")
    try:
        target = ph.render(tpl, {**ctx.base_values()}, ph.NAME_ALLOWED)
    except ph.PlaceholderError as exc:
        return _step("device_name", "skipped", _ph_reason(exc))
    dev = ctx.device
    if dev.get("name_by_user") == target or (not dev.get("name_by_user") and dev.get("name") == target):
        return _step("device_name", "ok", "already_ok", target=target)
    calls: list = []
    msg = await sess.call({"type": "config/device_registry/update", "device_id": dev["id"],
                           "name_by_user": target}, calls)
    if not msg.get("success"):
        return _step("device_name", "failed", f"update_failed: {msg.get('error')}", calls=calls, target=target)
    dev["name_by_user"] = target
    ctx.changed_any = True
    return _step("device_name", "ok", None, changed=True, calls=calls, target=target)


async def step_device_area(sess: Session, ctx: Ctx) -> dict:
    wanted = ctx.recipe.get("area")
    if not wanted:
        return _step("device_area", "skipped", "area_unresolved", detail="recipe area is null; nothing changed")
    area = _find_area(ctx.areas, wanted)
    if area is None:
        return _step("device_area", "skipped", "area_unresolved",
                     detail=f"area {wanted!r} not found in the area registry (areas are never created)")
    ctx.area = area
    if ctx.device.get("area_id") == area["area_id"]:
        return _step("device_area", "ok", "already_ok", target=area["area_id"])
    calls: list = []
    msg = await sess.call({"type": "config/device_registry/update", "device_id": ctx.device["id"],
                           "area_id": area["area_id"]}, calls)
    if not msg.get("success"):
        return _step("device_area", "failed", f"update_failed: {msg.get('error')}", calls=calls,
                     target=area["area_id"])
    ctx.device["area_id"] = area["area_id"]
    ctx.changed_any = True
    return _step("device_area", "ok", None, changed=True, calls=calls, target=area["area_id"])


async def step_entity_cleanup(sess: Session, ctx: Ctx) -> dict:
    rules = ctx.recipe.get("entity_id_cleanup") or []
    if not rules:
        return _step("entity_id_cleanup", "skipped", "no_entity_id_cleanup_in_recipe")
    compiled = [(i, r, glob_regex(r["from"])) for i, r in enumerate(rules)]
    taken = set(ctx.registry_ids) | (ctx.state_ids or set())
    planned: dict[str, str] = {}  # new id -> old id, within this run
    actions, calls = [], []

    def render_for(current: str, rule: dict, m: re.Match) -> str:
        vals = ctx.base_values()
        dom, obj = current.split(".", 1)
        vals.update({"domain": dom, "object_id": obj})
        vals.update({str(n): g for n, g in enumerate(m.groups(), 1)})
        return ph.render(rule["to"], vals, ph.ID_ALLOWED, allow_digits=True, slug_values=True)

    for orig in sorted(ctx.entity_map):
        current = ctx.entity_map[orig]
        hit = next(((i, r, rx, rx.match(current.casefold())) for i, r, rx in compiled
                    if rx.match(current.casefold())), None)
        if hit is None:
            continue
        i, rule, rx, m = hit
        act = {"entity_id": current, "rule": i}
        actions.append(act)
        try:
            new = render_for(current, rule, m)
        except ph.PlaceholderError as exc:
            act.update(status="skipped", reason=_ph_reason(exc))
            continue
        act["new_entity_id"] = new
        if not VALID_ENTITY_ID.match(new) or len(new) > 255:
            act.update(status="skipped", reason="invalid_entity_id")
            continue
        if new.split(".", 1)[0] != current.split(".", 1)[0]:
            act.update(status="skipped", reason="domain_changed")
            continue
        m2 = rx.match(new.casefold())
        if m2 and new != current:
            try:
                again = render_for(new, rule, m2)
            except ph.PlaceholderError:
                again = None
            if again != new:
                act.update(status="skipped", reason="rule_not_idempotent",
                           detail="'from' also matches the new id and would rename it again; narrow 'from'")
                continue
        if new == current:
            act.update(status="ok", reason="already_ok", changed=False)
            continue
        if new in planned:
            act.update(status="skipped", reason="collision", detail=f"planned for {planned[new]} in this run")
            continue
        if new in taken:
            where = "entity registry" if new in ctx.registry_ids else "state machine"
            act.update(status="skipped", reason="collision", detail=f"{new} already exists ({where})")
            continue
        msg = await sess.call({"type": "config/entity_registry/update", "entity_id": current,
                               "new_entity_id": new}, calls)
        if not msg.get("success"):
            act.update(status="failed", reason=f"update_failed: {msg.get('error')}")
            continue
        planned[new] = current
        taken.add(new)
        ctx.entity_map[orig] = new
        ctx.changed_any = True
        act.update(status="ok", reason=None, changed=True)

    if not actions:
        return _step("entity_id_cleanup", "skipped", "no_entities_matched_rules", calls=calls, actions=actions)
    statuses = {a["status"] for a in actions}
    status = "failed" if "failed" in statuses else "ok" if "ok" in statuses else "skipped"
    reason = None if status == "ok" else ("; ".join(sorted({a["reason"] for a in actions
                                                           if a["status"] == status})))
    return _step("entity_id_cleanup", status, reason, changed=any(a.get("changed") for a in actions),
                 calls=calls, actions=actions)


async def step_dashboard(sess: Session, ctx: Ctx) -> dict:
    dc = ctx.recipe.get("dashboard_card")
    if not dc:
        return _step("dashboard_card", "skipped", "no_dashboard_card_in_recipe")
    if dc.get("card") is None:
        return _step("dashboard_card", "skipped", "no_card_defined")
    values = {**ctx.base_values(), **ctx.entity_values()}
    try:
        card = ph.render_deep(dc["card"], lambda t: ph.render(t, values, ph.CARD_ALLOWED, allow_entity=True))
    except ph.PlaceholderError as exc:
        return _step("dashboard_card", "skipped", _ph_reason(exc))
    dash = dc.get("dashboard")
    url_path = None if dash in (None, "lovelace") else dash
    path_kw = {} if url_path is None else {"url_path": url_path}
    calls: list = []

    # storage-mode check: YAML-mode dashboards are read-only and must never be written
    if url_path is None:
        msg = await sess.call({"type": "lovelace/info"}, calls)
        if not msg.get("success"):
            return _step("dashboard_card", "skipped", "dashboard_mode_unknown", calls=calls,
                         detail=str(msg.get("error")))
        mode = (msg.get("result") or {}).get("mode")
    else:
        dashboards = await sess.read({"type": "lovelace/dashboards/list"}, calls)
        found = next((d for d in dashboards or [] if d.get("url_path") == url_path), None)
        if found is None:
            return _step("dashboard_card", "skipped", "dashboard_not_found", calls=calls,
                         detail=f"no dashboard with url_path {url_path!r}")
        mode = found.get("mode")
    if mode != "storage":
        return _step("dashboard_card", "skipped", f"dashboard_not_storage_mode (mode={mode})", calls=calls)

    msg = await sess.call({"type": "lovelace/config", **path_kw}, calls)
    if not msg.get("success"):
        code = (msg.get("error") or {}).get("code")
        if code == "config_not_found":
            return _step("dashboard_card", "skipped", "dashboard_has_no_saved_config", calls=calls,
                         detail="auto-generated dashboard; take control of it in the UI first. Not overwriting.")
        return _step("dashboard_card", "failed", f"config_read_failed: {msg.get('error')}", calls=calls)
    config = copy.deepcopy(msg.get("result") or {})
    views = config.get("views") or []
    want_view = dc.get("view")
    if want_view is None:
        view = views[0] if views else None
    else:
        w = str(want_view).casefold()
        view = next((v for v in views if str(v.get("path", "")).casefold() == w
                     or str(v.get("title", "")).casefold() == w), None)
    if view is None:
        return _step("dashboard_card", "skipped", "view_not_found", calls=calls,
                     detail=f"view {want_view!r} not in dashboard" if want_view else "dashboard has no views")
    if view.get("type") not in CARD_VIEW_TYPES:
        return _step("dashboard_card", "skipped", f"view_type_unsupported ({view.get('type')})", calls=calls)
    cards = view.setdefault("cards", [])
    if any(c == card or (isinstance(c, dict) and card.get("entity") and c.get("type") == card.get("type")
                         and c.get("entity") == card.get("entity")) for c in cards):
        return _step("dashboard_card", "ok", "already_ok", calls=calls, card=card)
    cards.append(card)

    if ctx.opts.apply:  # backup the full previous config before overwriting it
        try:
            os.makedirs(ctx.opts.backup_dir, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            bpath = os.path.join(ctx.opts.backup_dir, f"lovelace_{url_path or 'default'}_{stamp}.json")
            with open(bpath, "w", encoding="utf-8") as fh:
                json.dump(msg.get("result") or {}, fh, indent=2)
        except OSError as exc:
            return _step("dashboard_card", "failed", f"backup_failed: {exc}", calls=calls)
    else:
        bpath = None
    save = await sess.call({"type": "lovelace/config/save", "config": config, **path_kw}, calls)
    if not save.get("success"):
        return _step("dashboard_card", "failed", f"save_failed: {save.get('error')}", calls=calls, card=card)
    ctx.changed_any = True
    return _step("dashboard_card", "ok", None, changed=True, calls=calls, card=card, backup=bpath)


async def step_welcome(sess: Session, ctx: Ctx) -> dict:
    target = ctx.recipe.get("welcome_automation")
    if not target:
        return _step("welcome_automation", "skipped", "no_welcome_automation_in_recipe")
    if not AUTOMATION_ID.match(str(target)):
        return _step("welcome_automation", "skipped", "unsupported_welcome_target",
                     detail=f"{target!r}: expected an automation entity id like automation.welcome_x")
    if not ctx.changed_any and not ctx.opts.force_welcome:
        return _step("welcome_automation", "skipped", "no_changes_this_run",
                     detail="welcome fires only when this run changed something (use --force-welcome)")
    calls: list = []
    if ctx.state_ids is not None and target not in ctx.state_ids:
        return _step("welcome_automation", "skipped", "automation_not_found", detail=target)
    msg = await sess.call({"type": "call_service", "domain": "automation", "service": "trigger",
                           "target": {"entity_id": target}}, calls)
    if not msg.get("success"):
        return _step("welcome_automation", "failed", f"trigger_failed: {msg.get('error')}", calls=calls)
    return _step("welcome_automation", "ok", None, changed=True, calls=calls, target=target)


STEP_FUNCS = (("device_area", step_device_area),  # area first: name/entity templates may use it
              ("device_name", step_device_name),
              ("entity_id_cleanup", step_entity_cleanup),
              ("dashboard_card", step_dashboard),
              ("welcome_automation", step_welcome))


# --------------------------------------------------------------------------- orchestration

def _summary(steps: list[dict]) -> dict:
    out = {"ok": 0, "skipped": 0, "failed": 0, "changed": 0}
    for s in steps:
        out[s["status"]] += 1
        out["changed"] += 1 if s.get("changed") else 0
    return out


def error_result(device_id: str, profile: str | None, mode: str, error: str) -> dict:
    return {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "device_id": device_id,
            "mode": mode, "profile": profile, "status": "error", "error": error, "steps": [],
            "summary": {"ok": 0, "skipped": 0, "failed": 1, "changed": 0}}


async def provision_device(caller: Caller, device_id: str, profiles: list[Profile], opts: Options,
                           profile_name: str | None = None) -> dict:
    """Plan (dry-run) or execute (apply) the matching profile's recipe for one registry device.

    Returns a JSON-serializable result dict. Never raises except asyncio.CancelledError.
    """
    mode = "apply" if opts.apply else "dry-run"
    result = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "device_id": device_id, "mode": mode,
              "profile": None, "profile_file": None, "matched_by": None, "status": "done",
              "setup_calls": [], "steps": []}
    sess = Session(caller, opts.apply)
    calls = result["setup_calls"]
    try:
        devices = await sess.read({"type": "config/device_registry/list"}, calls)
        device = next((d for d in devices or [] if d.get("id") == device_id), None)
        if device is None:
            result.update(status="device_not_found", summary=_summary([]))
            return result
        entities = await sess.read({"type": "config/entity_registry/list"}, calls)
        areas = await sess.read({"type": "config/area_registry/list"}, calls)
        smsg = await sess.call({"type": "get_states"}, calls)
        state_ids = {s.get("entity_id") for s in smsg.get("result") or []} if smsg.get("success") else None
        registry_ids = {e.get("entity_id") for e in entities or []}
        dev_entity_ids = sorted(e["entity_id"] for e in entities or []
                                if e.get("device_id") == device_id and e.get("entity_id"))

        if profile_name:
            profile = next((p for p in profiles if p.name == profile_name and p.active), None)
            result["matched_by"] = "explicit"
        else:
            profile = matcher.match_device(device, dev_entity_ids, profiles)
            result["matched_by"] = "matcher"
        if profile is None:
            result.update(status="no_match", summary=_summary([]),
                          reason=(f"no active profile named {profile_name!r}" if profile_name
                                  else "no active profile matches this device"))
            return result
        result.update(profile=profile.name, profile_file=profile.file)

        ctx = Ctx(device, dev_entity_ids, areas or [], profile.recipe, registry_ids, state_ids, opts)
        done = {}
        for name, fn in STEP_FUNCS:
            try:
                done[name] = await fn(sess, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                done[name] = _step(name, "failed", f"exception: {exc!r}")
        result["steps"] = [done[n] for n in STEPS]  # report in recipe order
        result["summary"] = _summary(result["steps"])
        result["entity_map"] = {k: v for k, v in ctx.entity_map.items() if k != v}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.update(status="error", error=repr(exc), summary={"ok": 0, "skipped": 0, "failed": 1, "changed": 0})
    return result


def summary_line(result: dict) -> str:
    if result["status"] != "done":
        return result["status"] + (f" ({result.get('error') or result.get('reason')})"
                                   if result.get("error") or result.get("reason") else "")
    s = result["summary"]
    return f"{s['ok']} ok, {s['skipped']} skipped, {s['failed']} failed, {s['changed']} changed"


def _brief(call: dict) -> str:
    p = dict(call["payload"])
    if "config" in p:
        p["config"] = f"<full dashboard config, {len((p['config'] or {}).get('views') or [])} view(s)>"
    state = "" if not call["mutating"] else (" [WOULD SEND]" if call.get("dry_run") else " [SENT]")
    ok = "" if "ok" not in call else (" -> ok" if call["ok"] else f" -> ERROR {call.get('error')}")
    return f"{call['type']} {json.dumps(p, separators=(',', ':'))}{state}{ok}"


def format_result(result: dict) -> str:
    out = [f"device {result['device_id']}  mode={result['mode'].upper()}  "
           f"profile={result.get('profile')} ({result.get('matched_by')})  -> {summary_line(result)}"]
    if result["status"] == "no_match":
        out.append(f"  hint: no active profile matches. Generate a draft: "
                   f"python -m ha_watcher.draft_profile --device-id {result['device_id']} --live")
    if result["status"] != "done":
        return "\n".join(out)
    for st in result["steps"]:
        out.append(f"  [{st['status'].upper():7}] {st['step']}" + (f"  ({st['reason']})" if st.get("reason") else "")
                   + ("  *changes*" if st.get("changed") else ""))
        if st.get("detail"):
            out.append(f"            {st['detail']}")
        for a in st.get("actions", []):
            out.append(f"            - {a['entity_id']} -> {a.get('new_entity_id')}: {a['status']}"
                       + (f" ({a['reason']})" if a.get("reason") else "")
                       + (f" {a['detail']}" if a.get("detail") else ""))
        for c in st["calls"]:
            if c["mutating"]:
                out.append("            " + _brief(c))
    if result["mode"] == "dry-run":
        out.append("  (dry-run: no mutating call was sent. Re-run with --apply to execute the calls marked WOULD SEND.)")
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ha_watcher.provision", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device-id", help="device registry ID to provision")
    p.add_argument("--apply", action="store_true",
                   help="actually send the mutating calls. WITHOUT this flag nothing is changed (dry-run)")
    p.add_argument("--profile", help="use this active profile by name instead of matching")
    p.add_argument("--profiles-dir", default=os.environ.get("HA_WATCHER_PROFILES", "profiles"))
    p.add_argument("--url", default=os.environ.get("HA_URL"), help="ws://host:8123/api/websocket (env HA_URL)")
    p.add_argument("--force-welcome", action="store_true", help="fire the welcome automation even if nothing changed")
    p.add_argument("--backup-dir", default=os.environ.get("HA_WATCHER_BACKUP_DIR", "logs/backups"))
    p.add_argument("--provision-log", default=os.environ.get("HA_WATCHER_PROVISION_LOG", "logs/device_provision.jsonl"))
    p.add_argument("--no-dismiss", action="store_true",
                   help="with --apply, do not dismiss the device's 'needs a profile' notification")
    p.add_argument("--no-log", action="store_true", help="do not append the result to the provision log")
    p.add_argument("--json", action="store_true", help="print the full result as JSON instead of text")
    p.add_argument("--check-profiles", action="store_true",
                   help="only load/validate profiles and print errors and warnings (no HA connection)")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def check_profiles(profiles_dir: str) -> int:
    res = load_profiles(profiles_dir)
    if not res.directory_found:
        print(f"profiles dir {profiles_dir!r} not found", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    for p in res.profiles:
        print(f"ok       {p.file}: {p.name} ({p.status})")
    for w in res.warnings:
        print(f"WARNING  {w}")
    for e in res.errors:
        print(f"INVALID  {e}")
    return 1 if res.errors else 0


async def _amain(args, token: str, profiles: list[Profile]) -> int:
    opts = Options(apply=args.apply, backup_dir=args.backup_dir, force_welcome=args.force_welcome)
    async with HAClient(args.url, token, args.timeout) as ha:
        result = await provision_device(ha.call, args.device_id, profiles, opts, profile_name=args.profile)
        if opts.apply and result["status"] == "done" and not args.no_dismiss:
            # the device now has an active profile applied: clear its 'needs a profile' notification
            await notify.dismiss(ha.call, args.device_id)
    print(json.dumps(result, indent=2) if args.json else format_result(result))
    if not args.no_log:
        try:
            emit(make_capture_logger(args.provision_log, 10 * 1024 * 1024, 10), result)
        except OSError as exc:
            print(f"WARNING: could not write provision log: {exc}", file=sys.stderr)
    if result["status"] in ("device_not_found", "no_match"):
        return EXIT_NO_TARGET
    if result["status"] == "error" or result["summary"]["failed"]:
        return EXIT_STEP_FAILED
    return EXIT_OK


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check_profiles:
        return check_profiles(args.profiles_dir)
    token = os.environ.get("HA_TOKEN")
    if not args.device_id or not args.url or not token:
        print("ERROR: --device-id, HA_URL (or --url) and HA_TOKEN are required", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    res = load_profiles(args.profiles_dir)
    for e in res.errors:
        print(f"WARNING: invalid profile skipped: {e}", file=sys.stderr)
    for w in res.warnings:
        print(f"WARNING: profile: {w}", file=sys.stderr)
    if args.apply:
        print("APPLY MODE: mutating calls will be sent to Home Assistant.", file=sys.stderr)
    try:
        return asyncio.run(_amain(args, token, res.profiles))
    except AuthFailed as exc:
        print(f"FATAL: Home Assistant rejected the token (auth_invalid): {exc}", file=sys.stderr)
        return EXIT_AUTH_FAILED
    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        print(f"ERROR: could not talk to Home Assistant: {exc!r}", file=sys.stderr)
        return EXIT_STEP_FAILED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
