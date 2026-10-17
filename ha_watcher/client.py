"""Shared Home Assistant WebSocket helpers: auth handshake and a tiny request/response client."""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

diag = logging.getLogger("ha_watcher")


class AuthFailed(Exception):
    """HA rejected the token (auth_invalid). Never retried."""


async def authenticate(ws, token: str) -> str:
    """Run the HA auth handshake. Returns the HA version. Raises AuthFailed / ConnectionError."""
    first = json.loads(await ws.recv())
    if first.get("type") != "auth_required":
        raise ConnectionError(f"unexpected first message: {first!r}")
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    reply = json.loads(await ws.recv())
    if reply.get("type") == "auth_invalid":
        raise AuthFailed(reply.get("message", "auth_invalid"))
    if reply.get("type") != "auth_ok":
        raise ConnectionError(f"unexpected auth reply: {reply!r}")
    version = reply.get("ha_version", "?")
    diag.info("authenticated to HA %s", version)
    return version


class HAClient:
    """One-shot sequential client for CLIs (snapshot, provision). Not used by the watcher loop.

        async with HAClient(url, token) as ha:
            msg = await ha.call({"type": "config/device_registry/list"})
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url, self.token, self.timeout = url, token, timeout
        self._ws = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "HAClient":
        self._ws = await websockets.connect(self.url, max_size=None, open_timeout=self.timeout)
        try:
            await asyncio.wait_for(authenticate(self._ws, self.token), self.timeout)
        except BaseException:
            await self._ws.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def call(self, payload: dict) -> dict:
        """Send one command and return its `result` message (success true or false)."""
        async with self._lock:
            self._id += 1
            mid = self._id
            await self._ws.send(json.dumps({"id": mid, **payload}))

            async def _wait() -> dict:
                while True:
                    msg = json.loads(await self._ws.recv())
                    if msg.get("id") == mid and msg.get("type") == "result":
                        return msg
            return await asyncio.wait_for(_wait(), self.timeout)
