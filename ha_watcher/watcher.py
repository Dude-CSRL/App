"""Phase 1 watcher: capture Home Assistant device-registry 'create' events to JSON Lines.

Pure capture, no matching logic (see HA_Device_Automation_Roadmap.md, Phase 1).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import os
import random
import sys
from dataclasses import dataclass, field

import websockets

from . import matcher, notify, provision
from .client import AuthFailed, authenticate
from .jsonl import emit, make_capture_logger
from .profiles import LoadResult, load_profiles
from .state import SeenDevices

EVENT_TYPE = "device_registry_updated"
CREATE_ACTION = "create"
EXIT_AUTH_FAILED = 2
EXIT_CONFIG_ERROR = 3
TOKEN_MAX_AGE_DAYS = 90

diag = logging.getLogger("ha_watcher")


@dataclass
class Config:
    url: str
    token: str
    log_file: str = "logs/device_capture.jsonl"
    state_file: str = "state/seen_devices.json"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10
    force_device_ids: set[str] = field(default_factory=set)
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    token_created: str | None = None
    fetch_device_details: bool = True
    profiles_dir: str | None = None  # None disables matching (CLI default: profiles)
    match_settle_delay: float = 10.0  # wait before reading entities when a profile needs entity patterns
    request_timeout: float = 30.0
    notify_unmatched: str = "off"  # off | persistent (CLI default: persistent)
    provision_mode: str = "off"  # off | dry-run | apply (Phase 3)
    provision_log_file: str = "logs/device_provision.jsonl"
    backup_dir: str = "logs/backups"  # lovelace config backups taken before any dashboard save


def backoff_delays(initial: float, cap: float, rng: random.Random | None = None):
    """Exponential backoff with cap and 'full jitter': uniform(0, min(cap, initial*2**n))."""
    rng = rng or random.Random()
    attempt = 0
    while True:
        ceiling = min(cap, initial * (2 ** attempt))
        yield rng.uniform(0, ceiling)
        attempt += 1


def check_token_age(token_created: str | None, today: dt.date | None = None) -> str | None:
    """Non-blocking token rotation reminder. Returns the warning text (also logged) or None."""
    if not token_created:
        return None
    try:
        created = dt.date.fromisoformat(token_created)
    except ValueError:
        msg = f"HA_TOKEN_CREATED={token_created!r} is not YYYY-MM-DD; skipping token-age check"
        diag.warning(msg)
        return msg
    age = ((today or dt.date.today()) - created).days
    if age > TOKEN_MAX_AGE_DAYS:
        msg = f"HA token is {age} days old (> {TOKEN_MAX_AGE_DAYS}); consider rotating it"
        diag.warning(msg)
        return msg
    return None


class Watcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.seen = SeenDevices(cfg.state_file)
        self.capture = make_capture_logger(cfg.log_file, cfg.max_bytes, cfg.backup_count)
        self._msg_id = 0
        self.connections = 0  # successful authenticated sessions (useful for tests)
        self.profiles: LoadResult = load_profiles(cfg.profiles_dir)
        self.provision_log = (make_capture_logger(cfg.provision_log_file, cfg.max_bytes, cfg.backup_count)
                              if cfg.provision_mode != "off" else None)
        self._report_profiles()

    def _report_profiles(self) -> None:
        res = self.profiles
        if not self.cfg.profiles_dir:
            diag.info("profile matching disabled (no profiles dir)")
        elif not res.directory_found:
            diag.warning("profiles dir %r not found; every device will be logged as unmatched", self.cfg.profiles_dir)
        else:
            diag.info("loaded %d profile(s) from %s: %d active, %d draft (drafts are not matched)",
                      len(res.profiles), self.cfg.profiles_dir, len(res.active), len(res.drafts))
        for warn in res.warnings:
            diag.warning("PROFILE WARNING %s", warn)
        for err in res.errors:
            diag.error("INVALID PROFILE (skipped) %s", err)
        if res.errors:
            diag.error("%d profile file(s) skipped; fix them and restart the watcher", len(res.errors))
        if self.cfg.notify_unmatched != "off":
            diag.info("unmatched devices raise a persistent notification in HA (%s*)", notify.ID_PREFIX)
        mode = self.cfg.provision_mode
        if mode == "apply":
            diag.warning("PROVISIONING IS ENABLED (apply): matched devices will be MODIFIED in Home Assistant")
        elif mode == "dry-run":
            diag.info("provisioning in dry-run mode: plans are logged to %s, nothing is changed",
                      self.cfg.provision_log_file)

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _authenticate(self, ws) -> None:
        await authenticate(ws, self.cfg.token)

    async def _call(self, ws, payload: dict, pending: dict) -> dict:
        """Send a command and wait for its result message (resolved by the receive loop)."""
        mid = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        pending[mid] = fut
        try:
            await ws.send(json.dumps({"id": mid, **payload}))
            return await asyncio.wait_for(fut, self.cfg.request_timeout)
        finally:
            pending.pop(mid, None)

    def match_result(self, device: dict | None, entity_ids: list[str] | None) -> dict:
        """Match a captured device against loaded profiles. Returned dict is logged as `match`."""
        if device is None:
            return {"matched": False, "profile": None, "profile_file": None, "reason": "no_device_details"}
        prof = matcher.match_device(device, entity_ids, self.profiles.profiles)
        if prof is None:
            return {"matched": False, "profile": None, "profile_file": None, "reason": "no_matching_profile"}
        return {"matched": True, "profile": prof.name, "profile_file": prof.file}

    def _would_log(self, device_id: str) -> bool:
        return device_id not in self.seen or device_id in self.cfg.force_device_ids

    def handle_created(self, event_msg: dict, device: dict | None,
                       entity_ids: list[str] | None = None) -> bool:
        """Log a device-created event. Returns True if a line was written."""
        return self._log_created(event_msg, device, entity_ids) is not None

    def _log_created(self, event_msg: dict, device: dict | None,
                     entity_ids: list[str] | None = None) -> dict | None:
        """Log a device-created event. Returns the record written, or None if skipped."""
        event = event_msg.get("event", {})
        data = event.get("data", {})
        device_id = data.get("device_id")
        if not device_id:
            diag.warning("create event without device_id: %r", event_msg)
            return None
        forced = device_id in self.cfg.force_device_ids
        if device_id in self.seen and not forced:
            diag.info("dedup: device %s already logged, skipping", device_id)
            return None
        match = self.match_result(device, entity_ids)
        record = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event_type": event.get("event_type", EVENT_TYPE),
            "action": data.get("action"),
            "device_id": device_id,
            "forced": forced,
            "raw_payload": event_msg,
            "device": device,
            "entities": entity_ids,
            "match": match,
        }
        emit(self.capture, record)
        self.seen.add(device_id)
        diag.info("captured device %s%s -> %s", device_id, " (forced)" if forced else "",
                  f"matched profile {match['profile']}" if match["matched"] else "unmatched")
        return record

    async def _fetch_device(self, ws, pending, device_id: str) -> dict | None:
        res = await self._call(ws, {"type": "config/device_registry/list"}, pending)
        if not res.get("success"):
            return None
        return next((d for d in res.get("result") or [] if d.get("id") == device_id), None)

    async def _process_created(self, ws, pending: dict, event_msg: dict) -> None:
        data = event_msg.get("event", {}).get("data", {})
        device_id = data.get("device_id")
        if not device_id or not self._would_log(device_id):
            self.handle_created(event_msg, None)  # logs the dedup/skip decision
            return
        device = entity_ids = None
        waited = False
        if self.cfg.fetch_device_details:
            device = await self._fetch_device(ws, pending, device_id)
            if device is not None and matcher.needs_entities(device, self.profiles.profiles):
                # Entities are registered shortly after the device; wait, then re-read both.
                await asyncio.sleep(self.cfg.match_settle_delay)
                waited = True
                device = await self._fetch_device(ws, pending, device_id) or device
                res = await self._call(ws, {"type": "config/entity_registry/list"}, pending)
                if res.get("success"):
                    entity_ids = [e["entity_id"] for e in res.get("result") or []
                                  if e.get("device_id") == device_id and e.get("entity_id")]
        record = self._log_created(event_msg, device, entity_ids)
        if record is None:
            return
        if self.cfg.notify_unmatched != "off":
            if not record["match"]["matched"]:
                await notify.notify_unmatched(self._caller(ws, pending), record["device_id"], device, entity_ids,
                                              record["match"].get("reason"), self.cfg.profiles_dir)
            elif record["forced"]:  # a forced re-log that now matches: clear the old notification
                await notify.dismiss(self._caller(ws, pending), record["device_id"])
        if record["match"]["matched"] and self.cfg.provision_mode != "off":
            await self._provision(ws, pending, record, waited)

    def _caller(self, ws, pending: dict):
        async def caller(payload: dict) -> dict:
            return await self._call(ws, payload, pending)
        return caller

    async def _provision(self, ws, pending: dict, record: dict, waited: bool) -> None:
        """Run the matched profile's recipe (dry-run or apply). Never raises (except cancellation)."""
        device_id, profile = record["device_id"], record["match"]["profile"]
        try:
            if not waited:  # entities register shortly after the device; let them settle
                await asyncio.sleep(self.cfg.match_settle_delay)

            caller = self._caller(ws, pending)
            opts = provision.Options(apply=self.cfg.provision_mode == "apply", backup_dir=self.cfg.backup_dir)
            # re-match on fresh registry data (entities have had time to register)
            result = await provision.provision_device(caller, device_id, self.profiles.profiles, opts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # provisioning must never take the watcher down
            diag.error("provisioning %s crashed: %r", device_id, exc)
            result = provision.error_result(device_id, profile, self.cfg.provision_mode, repr(exc))
        emit(self.provision_log, result)
        diag.info("provision[%s] %s profile=%s: %s", result["mode"], device_id, profile,
                  provision.summary_line(result))

    async def run_once(self) -> None:
        """One connection lifetime. Raises AuthFailed, or returns/raises on disconnect."""
        self._msg_id = 0
        tasks: set[asyncio.Task] = set()
        pending: dict[int, asyncio.Future] = {}

        def _done(t: asyncio.Task) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                if not isinstance(exc, (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError)):
                    diag.error("error processing created device: %r", exc)
                else:
                    diag.warning("created-device processing interrupted: %r", exc)

        try:
            async with websockets.connect(self.cfg.url, max_size=None) as ws:
                await self._authenticate(ws)
                self.connections += 1
                sub_id = self._next_id()
                await ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": EVENT_TYPE}))
                async for raw in ws:
                    msg = json.loads(raw)
                    mtype, mid = msg.get("type"), msg.get("id")
                    if mtype == "result" and mid == sub_id:
                        if not msg.get("success"):
                            raise ConnectionError(f"subscribe failed: {msg!r}")
                        diag.info("subscribed to %s", EVENT_TYPE)
                    elif mtype == "event" and mid == sub_id:
                        if msg.get("event", {}).get("data", {}).get("action") != CREATE_ACTION:
                            continue
                        t = asyncio.create_task(self._process_created(ws, pending, msg))
                        tasks.add(t)
                        t.add_done_callback(_done)
                    elif mtype == "result" and mid in pending:
                        if not pending[mid].done():
                            pending[mid].set_result(msg)
        finally:
            for t in list(tasks):
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> int:
        check_token_age(self.cfg.token_created)
        delays = None
        while True:
            before = self.connections
            try:
                await self.run_once()
                diag.warning("connection closed by server")
            except AuthFailed as exc:
                print(
                    "\n" + "!" * 70 + "\n"
                    f"FATAL: Home Assistant rejected the access token (auth_invalid): {exc}\n"
                    "The token is revoked, expired, or wrong. NOT retrying.\n"
                    "Create a new long-lived token and update the env file (HA_TOKEN).\n"
                    + "!" * 70,
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_AUTH_FAILED
            except (OSError, websockets.exceptions.WebSocketException, ConnectionError, asyncio.TimeoutError) as exc:
                diag.warning("connection error: %s", exc)
            if self.connections > before:
                delays = None  # reset backoff after a successful authenticated session
            if delays is None:
                delays = backoff_delays(self.cfg.backoff_initial, self.cfg.backoff_max)
            delay = next(delays)
            diag.info("reconnecting in %.2fs", delay)
            await asyncio.sleep(delay)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ha_watcher", description=__doc__)
    p.add_argument("--url", default=os.environ.get("HA_URL"), help="ws://host:8123/api/websocket (env HA_URL)")
    p.add_argument("--log-file", default=os.environ.get("HA_WATCHER_LOG", "logs/device_capture.jsonl"))
    p.add_argument("--state-file", default=os.environ.get("HA_WATCHER_STATE", "state/seen_devices.json"))
    p.add_argument("--max-bytes", type=int, default=int(os.environ.get("HA_WATCHER_MAX_BYTES", 10 * 1024 * 1024)))
    p.add_argument("--backup-count", type=int, default=int(os.environ.get("HA_WATCHER_BACKUP_COUNT", 10)))
    p.add_argument("--force-device-id", action="append", default=[], metavar="ID",
                   help="bypass dedup and re-log this device ID (repeatable)")
    p.add_argument("--backoff-initial", type=float, default=1.0)
    p.add_argument("--backoff-max", type=float, default=60.0)
    p.add_argument("--profiles-dir", default=os.environ.get("HA_WATCHER_PROFILES", "profiles"),
                   help="directory of profile YAML files used to match devices (default: profiles)")
    p.add_argument("--match-settle-delay", type=float, default=10.0,
                   help="seconds to wait for entities to register before matching entity_patterns")
    p.add_argument("--notify-unmatched", choices=list(notify.MODES),
                   default=os.environ.get("HA_WATCHER_NOTIFY_UNMATCHED", "persistent"),
                   help="raise a persistent HA notification for each newly created device that matches no "
                        "profile (default: persistent). Sent regardless of --provision mode; failures only log.")
    p.add_argument("--provision", choices=["off", "dry-run", "apply"],
                   default=os.environ.get("HA_WATCHER_PROVISION", "off"),
                   help="run the matched profile's recipe for newly created devices. off (default) | "
                        "dry-run (log the plan, change nothing) | apply (MODIFIES Home Assistant)")
    p.add_argument("--provision-log", default=os.environ.get("HA_WATCHER_PROVISION_LOG", "logs/device_provision.jsonl"))
    p.add_argument("--backup-dir", default=os.environ.get("HA_WATCHER_BACKUP_DIR", "logs/backups"),
                   help="where dashboard config backups go before any dashboard save")
    p.add_argument("--no-device-details", action="store_true",
                   help="don't fetch config/device_registry/list for created devices")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("HA_TOKEN")
    if not args.url or not token:
        print("ERROR: HA_URL (or --url) and HA_TOKEN must be set", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    cfg = Config(
        url=args.url, token=token, log_file=args.log_file, state_file=args.state_file,
        max_bytes=args.max_bytes, backup_count=args.backup_count,
        force_device_ids=set(args.force_device_id), backoff_initial=args.backoff_initial,
        backoff_max=args.backoff_max, token_created=os.environ.get("HA_TOKEN_CREATED"),
        fetch_device_details=not args.no_device_details,
        profiles_dir=args.profiles_dir, match_settle_delay=args.match_settle_delay,
        notify_unmatched=args.notify_unmatched, provision_mode=args.provision, provision_log_file=args.provision_log, backup_dir=args.backup_dir,
    )
    try:
        return asyncio.run(Watcher(cfg).run())
    except KeyboardInterrupt:
        return 0
