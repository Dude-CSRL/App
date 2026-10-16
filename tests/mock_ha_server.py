"""Minimal mock Home Assistant WebSocket server for testing the Phase 1 watcher.

Speaks just enough of the HA WebSocket API:
  auth_required -> auth -> auth_ok | auth_invalid
  subscribe_events (event_type=device_registry_updated) -> result
  config/device_registry/list -> result (devices emitted so far)
  config/entity_registry/list -> result (entities registered via emit_device_created)
and pushes device_registry_updated {action: create} events built from JSON fixtures
(each fixture is a device-registry entry, see tests/fixtures/).

CLI:
  python -m tests.mock_ha_server --port 8765 --token test-token --fixtures tests/fixtures
  python -m tests.mock_ha_server --bad-token     # reject every auth attempt
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from pathlib import Path

import websockets

HA_VERSION = "2026.10.0-mock"
log = logging.getLogger("mock_ha")


def load_fixture(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_entity_ids(fixtures_dir: str | Path, device_id: str) -> list[str]:
    """Entity IDs for a device from <fixtures_dir>/entities/<device_id>.json (list of strings), if present."""
    p = Path(fixtures_dir) / "entities" / f"{device_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def load_fixtures(directory: str | Path) -> list[dict]:
    return [load_fixture(p) for p in sorted(Path(directory).glob("*.json"))]


class MockHA:
    def __init__(self, token: str = "test-token", bad_token: bool = False,
                 host: str = "127.0.0.1", port: int = 0):
        self.token = token
        self.bad_token = bad_token
        self.host, self.port = host, port
        self.devices: dict[str, dict] = {}
        self.entities: list[dict] = []  # entity-registry entries
        self.subscribers: dict = {}  # ws -> subscription id
        self.subscribed = asyncio.Event()
        self.auth_attempts = 0
        self.connections_total = 0
        self._server = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/api/websocket"

    async def start(self):
        self._server = await websockets.serve(self._handler, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def drop_all(self):
        """Simulate an HA restart / network blip: close every client connection."""
        self.subscribed.clear()
        for ws in list(self.subscribers):
            await ws.close(code=1012, reason="mock restart")
        self.subscribers.clear()

    async def wait_subscribed(self, timeout: float = 5.0):
        await asyncio.wait_for(self.subscribed.wait(), timeout)

    async def emit_device_created(self, device: dict, entity_ids: list[str] | None = None) -> int:
        """Register a device (and optionally its entities) and push a create event to all
        subscribers. Returns #recipients."""
        self.devices[device["id"]] = device
        for eid in entity_ids or []:
            self.entities.append({"entity_id": eid, "device_id": device["id"], "platform": "mock",
                                  "disabled_by": None, "name": None})
        sent = 0
        for ws, sub_id in list(self.subscribers.items()):
            msg = {
                "id": sub_id,
                "type": "event",
                "event": {
                    "event_type": "device_registry_updated",
                    "data": {"action": "create", "device_id": device["id"]},
                    "origin": "LOCAL",
                    "time_fired": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "context": {"id": "01MOCKCONTEXT", "parent_id": None, "user_id": None},
                },
            }
            try:
                await ws.send(json.dumps(msg))
                sent += 1
            except websockets.exceptions.ConnectionClosed:
                self.subscribers.pop(ws, None)
        return sent

    async def _handler(self, ws):
        self.connections_total += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": HA_VERSION}))
        try:
            auth = json.loads(await ws.recv())
            self.auth_attempts += 1
            if auth.get("type") != "auth" or self.bad_token or auth.get("access_token") != self.token:
                await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token or password"}))
                await ws.close()
                return
            await ws.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))
            async for raw in ws:
                msg = json.loads(raw)
                mid, mtype = msg.get("id"), msg.get("type")
                if mtype == "subscribe_events":
                    self.subscribers[ws] = mid
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": True, "result": None}))
                    self.subscribed.set()
                elif mtype == "config/device_registry/list":
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": True,
                                              "result": list(self.devices.values())}))
                elif mtype == "config/entity_registry/list":
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": True,
                                              "result": list(self.entities)}))
                elif mtype == "ping":
                    await ws.send(json.dumps({"id": mid, "type": "pong"}))
                else:
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": False,
                                              "error": {"code": "unknown_command", "message": mtype}}))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.subscribers.pop(ws, None)


async def _amain(args):
    server = await MockHA(token=args.token, bad_token=args.bad_token, host=args.host, port=args.port).start()
    log.info("mock HA listening on %s (token=%r, bad_token=%s)", server.url, args.token, args.bad_token)
    fixtures = load_fixtures(args.fixtures)
    while True:
        await server.subscribed.wait()
        await asyncio.sleep(args.delay)
        for dev in fixtures:
            n = await server.emit_device_created(dev, load_entity_ids(args.fixtures, dev['id']))
            log.info("emitted create for %s (%s) to %d subscriber(s)", dev["id"], dev.get("name"), n)
            await asyncio.sleep(args.delay)
        if not args.repeat:
            log.info("all fixtures emitted; idling (Ctrl-C to stop)")
            await asyncio.Event().wait()
        server.subscribed.clear()  # --repeat: wait for the next (re)subscription


def main():
    p = argparse.ArgumentParser(description="Mock Home Assistant WebSocket server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default="test-token")
    p.add_argument("--bad-token", action="store_true", help="reject every auth attempt (auth_invalid)")
    p.add_argument("--fixtures", default=str(Path(__file__).parent / "fixtures"))
    p.add_argument("--delay", type=float, default=1.0, help="seconds between emitted fixtures")
    p.add_argument("--repeat", action="store_true", help="re-emit fixtures on each new subscription")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s mock_ha: %(message)s")
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
