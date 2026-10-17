"""Raise (or dismiss) unmatched-device notifications from a snapshot / capture log. Opt-in.

DEFAULT IS A DRY RUN: it prints which notifications it would create. Nothing is sent without --send.
Devices are matched against the CURRENT profiles, so a device that gained a profile since the
snapshot was taken counts as matched.

    python -m ha_watcher.notify_unmatched --snapshot-file logs/device_snapshot.jsonl \\
        [--device-id ID ...] [--send] [--dismiss-matched]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import matcher, notify
from .client import AuthFailed, HAClient
from .profiles import load_profiles
from .report import iter_records, log_files

EXIT_OK, EXIT_FAILED, EXIT_AUTH_FAILED, EXIT_CONFIG_ERROR = 0, 1, 2, 3


def collect(paths: list[str], profiles) -> dict[str, dict]:
    """Latest record per device_id -> {"device", "entities", "match"} using the current profiles."""
    errors: list = []
    latest: dict[str, dict] = {}
    for rec in iter_records(paths, errors):
        latest[rec["device_id"]] = rec
    out = {}
    for did, rec in latest.items():
        dev, ents = rec.get("device"), rec.get("entities")
        prof = matcher.match_device(dev, ents, profiles)
        out[did] = {"device": dev, "entities": ents, "profile": prof.name if prof else None,
                    "reason": None if dev else "no_device_details"}
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ha_watcher.notify_unmatched", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snapshot-file", action="append", default=[], metavar="FILE")
    p.add_argument("--log-file", help="capture log (live file plus rotated backups) to read as well")
    p.add_argument("--profiles-dir", default=os.environ.get("HA_WATCHER_PROFILES", "profiles"))
    p.add_argument("--device-id", action="append", default=[], help="only this device (repeatable)")
    p.add_argument("--url", default=os.environ.get("HA_URL"))
    p.add_argument("--send", action="store_true", help="actually create/dismiss notifications (default: print only)")
    p.add_argument("--dismiss-matched", action="store_true",
                   help="also dismiss the unmatched notification of devices that now match a profile")
    p.add_argument("--timeout", type=float, default=30.0)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    paths = (log_files(args.log_file) if args.log_file else []) + [f for f in args.snapshot_file if os.path.exists(f)]
    if not paths:
        print("ERROR: give --snapshot-file and/or --log-file pointing at existing files", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    profiles = load_profiles(args.profiles_dir)
    for e in profiles.errors:
        print(f"WARNING: invalid profile skipped: {e}", file=sys.stderr)
    devices = collect(paths, profiles.profiles)
    if args.device_id:
        devices = {d: v for d, v in devices.items() if d in set(args.device_id)}
        for missing in sorted(set(args.device_id) - set(devices)):
            print(f"WARNING: device {missing} not found in the input files", file=sys.stderr)
    unmatched = {d: v for d, v in devices.items() if v["profile"] is None}
    matched = [d for d, v in devices.items() if v["profile"] is not None]
    print(f"{len(devices)} device(s): {len(unmatched)} unmatched, {len(matched)} matched "
          f"({'SENDING' if args.send else 'dry-run, nothing sent'})")
    for did, v in sorted(unmatched.items()):
        data = notify.build_notification(did, v["device"], v["entities"], v["reason"], args.profiles_dir)
        print(f"  would create {data['notification_id']}: {data['title']}")
    if args.dismiss_matched:
        for did in sorted(matched):
            print(f"  would dismiss {notify.notification_id(did)}")
    if not args.send:
        print("re-run with --send to create these notifications")
        return EXIT_OK

    token = os.environ.get("HA_TOKEN")
    if not args.url or not token:
        print("ERROR: --send needs HA_URL (or --url) and HA_TOKEN", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    async def run() -> int:
        failed = 0
        async with HAClient(args.url, token, args.timeout) as ha:
            for did, v in sorted(unmatched.items()):
                r = await notify.notify_unmatched(ha.call, did, v["device"], v["entities"], v["reason"],
                                                  args.profiles_dir)
                failed += not r["ok"]
            if args.dismiss_matched:
                for did in sorted(matched):
                    failed += not (await notify.dismiss(ha.call, did))["ok"]
        return failed

    try:
        failed = asyncio.run(run())
    except AuthFailed as exc:
        print(f"FATAL: Home Assistant rejected the token (auth_invalid): {exc}", file=sys.stderr)
        return EXIT_AUTH_FAILED
    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        print(f"ERROR: could not talk to Home Assistant: {exc!r}", file=sys.stderr)
        return EXIT_FAILED
    print(f"sent: {len(unmatched)} created" + (f", {len(matched)} dismissed" if args.dismiss_matched else "")
          + (f"; {failed} FAILED (see warnings above)" if failed else ""))
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
