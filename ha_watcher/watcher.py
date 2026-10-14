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

from .state import SeenDevices

EVENT_TYPE = "device_registry_updated"
CREATE_ACTION = "create"
EXIT_AUTH_FAILED = 2
EXIT_CONFIG_ERROR = 3
TOKEN_MAX_AGE_DAYS = 90

diag = logging.getLogger("ha_watcher")


class AuthFailed(Exception):
    """HA rejected the token (auth_invalid). Never retried."""


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


def backoff_delays(initial: float, cap: float, rng: random.Random | None = None):
    """Exponential backoff with cap and 'full jitter': uniform(0, min(cap, initial*2**n))."""
    rng = rng or random.Random()
    attempt = 0
    while True:
        ceiling = min(cap, initial * (2 ** attempt))
        yield rng.uniform(0, ceiling)
        attempt += 1


def make_capture_logger(path: str, max_bytes: int, backup_count: int) -> logging.Logger:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    logger = logging.getLogger(f"ha_watcher.capture.{os.path.abspath(path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


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

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _authenticate(self, ws) -> None:
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first message: {first!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.cfg.token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") == "auth_invalid":
            raise AuthFailed(reply.get("message", "auth_invalid"))
        if reply.get("type") != "auth_ok":
            raise ConnectionError(f"unexpected auth reply: {reply!r}")
        diag.info("authenticated to HA %s", reply.get("ha_version", "?"))

    async def _request(self, ws, payload: dict, pending: dict) -> int:
        mid = self._next_id()
        payload = {"id": mid, **payload}
        fut = asyncio.get_running_loop().create_future()
        pending[mid] = fut
        await ws.send(json.dumps(payload))
        return mid

    def handle_created(self, event_msg: dict, device: dict | None) -> bool:
        """Log a device-created event. Returns True if a line was written."""
        event = event_msg.get("event", {})
        data = event.get("data", {})
        device_id = data.get("device_id")
        if not device_id:
            diag.warning("create event without device_id: %r", event_msg)
            return False
        forced = device_id in self.cfg.force_device_ids
        if device_id in self.seen and not forced:
            diag.info("dedup: device %s already logged, skipping", device_id)
            return False
        record = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event_type": event.get("event_type", EVENT_TYPE),
            "action": data.get("action"),
            "device_id": device_id,
            "forced": forced,
            "raw_payload": event_msg,
            "device": device,
        }
        self.capture.info(json.dumps(record, sort_keys=False, separators=(",", ":")))
        for h in self.capture.handlers:
            h.flush()
        self.seen.add(device_id)
        diag.info("captured device %s%s", device_id, " (forced)" if forced else "")
        return True

    async def run_once(self) -> None:
        """One connection lifetime. Raises AuthFailed, or returns/raises on disconnect."""
        self._msg_id = 0
        async with websockets.connect(self.cfg.url, max_size=None) as ws:
            await self._authenticate(ws)
            self.connections += 1
            sub_id = self._next_id()
            await ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": EVENT_TYPE}))
            pending: dict[int, asyncio.Future] = {}
            waiting_events: dict[int, dict] = {}  # request id -> event message awaiting device details
            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "result" and msg.get("id") == sub_id:
                    if not msg.get("success"):
                        raise ConnectionError(f"subscribe failed: {msg!r}")
                    diag.info("subscribed to %s", EVENT_TYPE)
                elif mtype == "event" and msg.get("id") == sub_id:
                    data = msg.get("event", {}).get("data", {})
                    if data.get("action") != CREATE_ACTION:
                        continue
                    if self.cfg.fetch_device_details:
                        mid = await self._request(ws, {"type": "config/device_registry/list"}, pending)
                        waiting_events[mid] = msg
                    else:
                        self.handle_created(msg, None)
                elif mtype == "result" and msg.get("id") in waiting_events:
                    event_msg = waiting_events.pop(msg["id"])
                    pending.pop(msg["id"], None)
                    device = None
                    if msg.get("success"):
                        did = event_msg["event"]["data"].get("device_id")
                        device = next((d for d in msg.get("result") or [] if d.get("id") == did), None)
                    self.handle_created(event_msg, device)

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
    )
    try:
        return asyncio.run(Watcher(cfg).run())
    except KeyboardInterrupt:
        return 0
