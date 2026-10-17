"""One-shot snapshot of ALL devices currently in the HA device registry (plus their entities).

Writes JSON Lines in the same shape as capture lines (event_type "snapshot") to a SEPARATE file
(default logs/device_snapshot.jsonl, replaced atomically on each run). It never touches the live
capture log or the dedup state file, so it can be run at any time next to the running watcher.
`python -m ha_watcher.report --snapshot-file ...` accepts it as input.

    python -m ha_watcher.snapshot [--out logs/device_snapshot.jsonl] [--profiles-dir profiles]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys

from . import matcher
from .client import AuthFailed, HAClient
from .jsonl import write_jsonl_atomic
from .profiles import load_profiles

EVENT_TYPE = "snapshot"
EXIT_OK, EXIT_ERROR, EXIT_AUTH_FAILED, EXIT_CONFIG_ERROR = 0, 1, 2, 3


async def take_snapshot(caller, profiles, now: dt.datetime | None = None) -> list[dict]:
    """caller: async fn(payload) -> result message. Returns one record per registry device."""
    ts = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    dmsg = await caller({"type": "config/device_registry/list"})
    if not dmsg.get("success"):
        raise RuntimeError(f"config/device_registry/list failed: {dmsg.get('error')}")
    emsg = await caller({"type": "config/entity_registry/list"})
    if not emsg.get("success"):
        raise RuntimeError(f"config/entity_registry/list failed: {emsg.get('error')}")
    by_device: dict[str, list[dict]] = {}
    for ent in emsg.get("result") or []:
        if ent.get("device_id"):
            by_device.setdefault(ent["device_id"], []).append(ent)
    records = []
    for dev in dmsg.get("result") or []:
        did = dev.get("id")
        if not did:
            continue
        entries = sorted(by_device.get(did, []), key=lambda e: e.get("entity_id", ""))
        entity_ids = [e["entity_id"] for e in entries if e.get("entity_id")]
        prof = matcher.match_device(dev, entity_ids, profiles)
        match = ({"matched": True, "profile": prof.name, "profile_file": prof.file} if prof else
                 {"matched": False, "profile": None, "profile_file": None, "reason": "no_matching_profile"})
        records.append({
            "timestamp": ts,
            "event_type": EVENT_TYPE,
            "action": "snapshot",
            "device_id": did,
            "forced": False,
            "raw_payload": dev,          # the registry entry verbatim
            "device": dev,
            "entities": entity_ids,
            "entity_entries": entries,   # full entity-registry entries (additive, snapshot only)
            "match": match,
        })
    return records


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ha_watcher.snapshot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("HA_URL"), help="ws://host:8123/api/websocket (env HA_URL)")
    p.add_argument("--out", default=os.environ.get("HA_WATCHER_SNAPSHOT", "logs/device_snapshot.jsonl"))
    p.add_argument("--profiles-dir", default=os.environ.get("HA_WATCHER_PROFILES", "profiles"))
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("HA_TOKEN")
    if not args.url or not token:
        print("ERROR: HA_URL (or --url) and HA_TOKEN must be set", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    res = load_profiles(args.profiles_dir)
    for e in res.errors:
        print(f"WARNING: invalid profile skipped: {e}", file=sys.stderr)

    async def run() -> list[dict]:
        async with HAClient(args.url, token, args.timeout) as ha:
            return await take_snapshot(ha.call, res.profiles)

    try:
        records = asyncio.run(run())
    except AuthFailed as exc:
        print(f"FATAL: Home Assistant rejected the token (auth_invalid): {exc}", file=sys.stderr)
        return EXIT_AUTH_FAILED
    except (OSError, ConnectionError, RuntimeError, asyncio.TimeoutError) as exc:
        print(f"ERROR: snapshot failed: {exc!r}", file=sys.stderr)
        return EXIT_ERROR
    n = write_jsonl_atomic(args.out, records)
    matched = sum(1 for r in records if r["match"]["matched"])
    print(f"snapshot: {n} devices ({matched} matched an active profile) -> {args.out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
