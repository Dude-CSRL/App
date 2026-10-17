"""Monitoring pass: summarize the Phase 1 capture log(s).

Reads the JSONL capture log plus rotated backups (log, log.1, log.2, ...), dedupes by
device ID, and writes a Markdown + JSON report. Optionally emits draft profile stubs
(YAML) to seed Phase 2 profiles. Matching uses ha_watcher.matcher.

    python -m ha_watcher.report --log-file logs/device_capture.jsonl --out-dir reports \
        [--profiles-dir profiles] [--emit-stubs reports/profile_stubs] [--snapshot-file logs/device_snapshot.jsonl]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from pathlib import Path

import yaml

from . import matcher
from .profiles import load_profiles

FIELDS = ("manufacturer", "model", "name", "integration_domain", "identifiers",
          "connections", "suggested_area")


def log_files(log_file: str) -> list[str]:
    """Rotated backups oldest-first (highest suffix first), then the live file."""
    backups = [p for p in glob.glob(glob.escape(log_file) + ".*") if re.fullmatch(r"\d+", p.rsplit(".", 1)[1])]
    backups.sort(key=lambda p: int(p.rsplit(".", 1)[1]), reverse=True)
    return backups + ([log_file] if os.path.exists(log_file) else [])


def iter_records(paths, errors: list):
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict) or not rec.get("device_id"):
                        raise ValueError("missing device_id")
                except (ValueError, json.JSONDecodeError) as exc:
                    errors.append({"file": os.path.basename(path), "line": n, "error": str(exc)[:200]})
                    continue
                yield rec


def integration_domain(device: dict) -> str | None:
    for ident in device.get("identifiers") or []:
        if isinstance(ident, (list, tuple)) and ident:
            return ident[0]
    return None


def summarize(log_file: str, profiles_dir: str | None = None, snapshot_files=()) -> dict:
    errors: list = []
    paths = log_files(log_file) + [f for f in snapshot_files if os.path.exists(f)]
    devices: dict[str, dict] = {}
    for rec in iter_records(paths, errors):
        did = rec["device_id"]
        ts = rec.get("timestamp")
        reg = rec.get("device") or {}
        d = devices.get(did)
        if d is None:
            d = devices[did] = {"device_id": did, "first_seen": ts, "last_seen": ts, "occurrences": 0}
            d.update({f: None for f in FIELDS})
            d["entity_ids"] = None
            d["seen_via"] = []
        d["occurrences"] += 1
        via = rec.get("event_type") == "snapshot" and "snapshot" or "capture"
        if via not in d["seen_via"]:
            d["seen_via"].append(via)
        if ts and (not d["first_seen"] or ts < d["first_seen"]):
            d["first_seen"] = ts
        if ts and (not d["last_seen"] or ts > d["last_seen"]):
            d["last_seen"] = ts
        if rec.get("entities"):
            d["entity_ids"] = rec["entities"]
        if reg:  # latest non-empty registry snapshot wins
            for f in FIELDS:
                v = integration_domain(reg) if f == "integration_domain" else reg.get(f)
                if v not in (None, [], ""):
                    d[f] = v
    loaded = load_profiles(profiles_dir)
    for d in devices.values():
        prof = matcher.match_device(d, d.get("entity_ids"), loaded.profiles)
        d["profile_match"] = prof.file if prof else None
        d["profile_name"] = prof.name if prof else None
        d["matched"] = prof is not None
    devs = sorted(devices.values(), key=lambda d: (d["first_seen"] or "", d["device_id"]))
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "log_files": [os.path.basename(p) for p in paths],
        "profiles_dir": profiles_dir if loaded.directory_found else None,
        "profiles_loaded": len(loaded.profiles),
        "profiles_active": len(loaded.active),
        "profile_errors": [{"file": e.file, "error": e.message} for e in loaded.errors],
        "device_count": len(devs),
        "matched_count": sum(d["matched"] for d in devs),
        "malformed_lines": errors,
        "devices": devs,
    }


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        v = "; ".join(":".join(map(str, x)) if isinstance(x, (list, tuple)) else str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def render_markdown(rep: dict) -> str:
    out = [
        "# HA device capture report", "",
        f"- Generated: {rep['generated_at']}",
        f"- Log files: {', '.join(rep['log_files']) or '(none)'}",
        f"- Devices: {rep['device_count']} ({rep['matched_count']} matched, "
        f"{rep['device_count'] - rep['matched_count']} unmatched)",
        f"- Profiles loaded: {rep['profiles_loaded']} ({rep['profiles_active']} active; drafts are never matched)",
        f"- Invalid profile files: {len(rep['profile_errors'])}",
        f"- Malformed lines skipped: {len(rep['malformed_lines'])}", "",
        "| Device ID | Manufacturer | Model | Name | Domain | Identifiers | Connections | Suggested area | First seen | Last seen | Seen | Profile | Via |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in rep["devices"]:
        out.append("| " + " | ".join(_fmt(x) for x in (
            d["device_id"], d["manufacturer"], d["model"], d["name"], d["integration_domain"],
            d["identifiers"], d["connections"], d["suggested_area"], d["first_seen"], d["last_seen"],
            d["occurrences"], d["profile_match"] or "unmatched", d["seen_via"])) + " |")
    if rep["profile_errors"]:
        out += ["", "## Invalid profiles (skipped)", ""]
        out += [f"- {e['file']}: {e['error']}" for e in rep["profile_errors"]]
    if rep["malformed_lines"]:
        out += ["", "## Malformed lines", ""]
        out += [f"- {e['file']}:{e['line']}: {e['error']}" for e in rep["malformed_lines"]]
    return "\n".join(out) + "\n"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "unknown"


def write_stubs(rep: dict, stub_dir: str) -> list[str]:
    """One draft profile per unmatched manufacturer+model. Never overwrites existing files."""
    os.makedirs(stub_dir, exist_ok=True)
    written, done = [], set()
    for d in rep["devices"]:
        if d["matched"]:
            continue
        key = (d["manufacturer"] or "unknown", d["model"] or "unknown")
        if key in done:
            continue
        done.add(key)
        path = os.path.join(stub_dir, f"{slug(key[0])}__{slug(key[1])}.yaml")
        if os.path.exists(path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        stub = {
            "name": stem,
            "status": "draft",
            "match": {"manufacturer": d["manufacturer"], "model": d["model"], "entity_patterns": []},
            "recipe": {"name": None, "area": None, "entity_id_cleanup": [],
                       "dashboard_card": None, "welcome_automation": None},
            "source": {"device_id": d["device_id"], "integration_domain": d["integration_domain"],
                       "identifiers": d["identifiers"], "connections": d["connections"],
                       "suggested_area": d["suggested_area"], "entity_ids": d.get("entity_ids")},
        }
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# DRAFT profile stub generated by ha_watcher.report. Review (see docs/profile-schema.md) and set status: active to enable.\n")
            yaml.safe_dump(stub, fh, sort_keys=False)
        written.append(path)
    return written


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ha_watcher.report", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log-file", default=os.environ.get("HA_WATCHER_LOG", "logs/device_capture.jsonl"))
    p.add_argument("--snapshot-file", action="append", default=[], metavar="FILE",
                   help="also read a snapshot written by ha_watcher.snapshot (repeatable)")
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--profiles-dir", default="profiles")
    p.add_argument("--emit-stubs", metavar="DIR", help="write draft profile stubs for unmatched devices")
    a = p.parse_args(argv)
    rep = summarize(a.log_file, a.profiles_dir, a.snapshot_file)
    if not rep["log_files"]:
        print(f"ERROR: no capture log found at {a.log_file} and no snapshot file given", file=sys.stderr)
        return 1
    os.makedirs(a.out_dir, exist_ok=True)
    jp, mp = os.path.join(a.out_dir, "device_report.json"), os.path.join(a.out_dir, "device_report.md")
    Path(jp).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    Path(mp).write_text(render_markdown(rep), encoding="utf-8")
    print(f"{rep['device_count']} devices ({rep['matched_count']} matched), "
          f"{len(rep['malformed_lines'])} malformed lines -> {mp}, {jp}")
    if a.emit_stubs:
        for s in write_stubs(rep, a.emit_stubs):
            print(f"stub: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
