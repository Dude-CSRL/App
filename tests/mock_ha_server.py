"""Minimal mock Home Assistant WebSocket server for testing the Phase 1 watcher.

Speaks just enough of the HA WebSocket API:
  auth_required -> auth -> auth_ok | auth_invalid
  subscribe_events (event_type=device_registry_updated) -> result
  config/device_registry/list -> result (devices emitted so far)
  config/entity_registry/list -> result (entities registered via emit_device_created / add_device)
  config/device_registry/update (name_by_user, area_id), config/entity_registry/update (new_entity_id),
  config/area_registry/list, get_states, lovelace/info, lovelace/dashboards/list, lovelace/config,
  lovelace/config/save, call_service (recorded only)
Every post-auth command is recorded in `commands`; state-changing ones also in `mutations`.
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
        self.areas: list[dict] = [{"area_id": "living_room", "name": "Living Room", "aliases": []},
                                  {"area_id": "kitchen", "name": "Kitchen", "aliases": ["Cooking"]}]
        self.extra_states: set[str] = set()  # entity ids that exist as states but not in the registry
        # url_path -> {"mode": "storage"|"yaml", "config": dict | None}; None key = default dashboard
        self.lovelace: dict = {None: {"mode": "storage",
                                      "config": {"views": [{"title": "Lights", "path": "lights", "cards": []}]}}}
        self.commands: list[dict] = []
        self.mutations: list[dict] = []
        self.fail_types: set[str] = set()  # command types that answer success=false
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

    MUTATING = {"config/device_registry/update", "config/entity_registry/update",
                "lovelace/config/save", "call_service"}

    def add_device(self, device: dict, entity_ids: list[str] | None = None) -> None:
        """Put a device (and entities) in the registry WITHOUT firing a create event
        (simulates devices that existed before the watcher started)."""
        self.devices[device["id"]] = json.loads(json.dumps(device))
        for eid in entity_ids or []:
            self.entities.append({"entity_id": eid, "device_id": device["id"], "platform": "mock",
                                  "disabled_by": None, "name": None})

    @staticmethod
    def _err(mid, code, message):
        return {"id": mid, "type": "result", "success": False, "error": {"code": code, "message": message}}

    @staticmethod
    def _ok(mid, result=None):
        return {"id": mid, "type": "result", "success": True, "result": result}

    def _dispatch(self, msg: dict) -> dict | None:
        mid, t = msg.get("id"), msg.get("type")
        self.commands.append(msg)
        if t in self.MUTATING:
            self.mutations.append(msg)
        if t in self.fail_types:
            return self._err(mid, "mock_failure", f"{t} forced to fail")
        if t == "config/device_registry/list":
            return self._ok(mid, list(self.devices.values()))
        if t == "config/entity_registry/list":
            return self._ok(mid, list(self.entities))
        if t == "config/area_registry/list":
            return self._ok(mid, list(self.areas))
        if t == "get_states":
            ids = {e["entity_id"] for e in self.entities} | self.extra_states
            return self._ok(mid, [{"entity_id": i, "state": "unknown", "attributes": {}} for i in sorted(ids)])
        if t == "config/device_registry/update":
            dev = self.devices.get(msg.get("device_id"))
            if dev is None:
                return self._err(mid, "not_found", "Device not found")
            if "area_id" in msg and msg["area_id"] is not None and \
                    msg["area_id"] not in {a["area_id"] for a in self.areas}:
                return self._err(mid, "invalid_info", "Unknown area")
            for k in ("name_by_user", "area_id"):
                if k in msg:
                    dev[k] = msg[k]
            return self._ok(mid, dict(dev))
        if t == "config/entity_registry/update":
            ent = next((e for e in self.entities if e["entity_id"] == msg.get("entity_id")), None)
            if ent is None:
                return self._err(mid, "not_found", "Entity not found")
            new = msg.get("new_entity_id")
            if new:
                taken = {e["entity_id"] for e in self.entities} | self.extra_states
                if new in taken:
                    return self._err(mid, "invalid_info", "Entity with this ID is already registered")
                ent["entity_id"] = new
            return self._ok(mid, {"entity_entry": dict(ent)})
        if t == "lovelace/info":
            return self._ok(mid, {"mode": self.lovelace[None]["mode"]})
        if t == "lovelace/dashboards/list":
            return self._ok(mid, [{"id": k, "url_path": k, "mode": v["mode"], "title": k}
                                  for k, v in self.lovelace.items() if k is not None])
        if t in ("lovelace/config", "lovelace/config/save"):
            dash = self.lovelace.get(msg.get("url_path"))
            if dash is None:
                return self._err(mid, "not_found", "Unknown dashboard")
            if t == "lovelace/config":
                if dash["config"] is None:
                    return self._err(mid, "config_not_found", "No config found.")
                return self._ok(mid, json.loads(json.dumps(dash["config"])))
            if dash["mode"] != "storage":
                return self._err(mid, "unauthorized", "Cannot save YAML dashboard")
            dash["config"] = msg["config"]
            return self._ok(mid)
        if t == "call_service":
            return self._ok(mid, {"context": {"id": "01MOCKCTX"}})
        if t == "ping":
            return {"id": mid, "type": "pong"}
        return self._err(mid, "unknown_command", t)

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
        self.devices[device["id"]] = json.loads(json.dumps(device))
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
                else:
                    await ws.send(json.dumps(self._dispatch(msg)))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.subscribers.pop(ws, None)


async def _amain(args):
    server = await MockHA(token=args.token, bad_token=args.bad_token, host=args.host, port=args.port).start()
    log.info("mock HA listening on %s (token=%r, bad_token=%s)", server.url, args.token, args.bad_token)
    fixtures = load_fixtures(args.fixtures)
    if args.existing_only:
        for dev in fixtures:
            server.add_device(dev, load_entity_ids(args.fixtures, dev["id"]))
        log.info("registered %d existing device(s) without create events; idling", len(fixtures))
        await asyncio.Event().wait()
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
    p.add_argument("--existing-only", action="store_true",
                   help="put the fixtures in the registry as pre-existing devices and emit no create events "
                        "(for trying ha_watcher.snapshot / ha_watcher.provision)")
    p.add_argument("--repeat", action="store_true", help="re-emit fixtures on each new subscription")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s mock_ha: %(message)s")
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
