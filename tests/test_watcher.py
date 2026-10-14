import asyncio
import json
import logging
import random
from pathlib import Path

import pytest

from ha_watcher.watcher import (EXIT_AUTH_FAILED, Config, Watcher, backoff_delays,
                                check_token_age, make_capture_logger)
from ha_watcher.state import SeenDevices
from tests.mock_ha_server import MockHA, load_fixture

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")


@pytest.fixture
async def mock():
    srv = await MockHA(token="good").start()
    yield srv
    await srv.stop()


def cfg(tmp_path, url, token="good", **kw):
    return Config(url=url, token=token, log_file=str(tmp_path / "cap.jsonl"),
                  state_file=str(tmp_path / "seen.json"), backoff_initial=0.05, backoff_max=0.2, **kw)


def lines(tmp_path):
    p = tmp_path / "cap.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


async def wait_for(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timeout waiting for condition")
        await asyncio.sleep(0.02)


async def start(w):
    return asyncio.create_task(w.run())


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_auth_success_and_capture_jsonl(tmp_path, mock):
    w = Watcher(cfg(tmp_path, mock.url))
    t = await start(w)
    await mock.wait_subscribed()
    assert w.connections == 1
    await mock.emit_device_created(HUE)
    await wait_for(lambda: len(lines(tmp_path)) == 1)
    rec = lines(tmp_path)[0]
    assert set(["timestamp", "event_type", "device_id", "raw_payload"]) <= rec.keys()
    assert rec["event_type"] == "device_registry_updated"
    assert rec["device_id"] == HUE["id"]
    assert rec["raw_payload"]["event"]["data"] == {"action": "create", "device_id": HUE["id"]}
    assert rec["device"] == HUE  # full registry entry verbatim
    await stop(t)


async def test_non_create_actions_ignored(tmp_path, mock):
    w = Watcher(cfg(tmp_path, mock.url))
    t = await start(w)
    await mock.wait_subscribed()
    ws, sub = next(iter(mock.subscribers.items()))
    await ws.send(json.dumps({"id": sub, "type": "event", "event": {
        "event_type": "device_registry_updated", "data": {"action": "update", "device_id": "x"}}}))
    await mock.emit_device_created(KASA)
    await wait_for(lambda: len(lines(tmp_path)) == 1)
    assert lines(tmp_path)[0]["device_id"] == KASA["id"]
    await stop(t)


async def test_auth_failure_exits_nonzero_no_retry(tmp_path, capsys):
    srv = await MockHA(bad_token=True).start()
    try:
        rc = await asyncio.wait_for(Watcher(cfg(tmp_path, srv.url)).run(), 5)
        assert rc == EXIT_AUTH_FAILED and rc != 0
        assert srv.auth_attempts == 1  # did not retry
        assert "auth_invalid" in capsys.readouterr().err
    finally:
        await srv.stop()


async def test_wrong_token_exits(tmp_path, mock):
    rc = await asyncio.wait_for(Watcher(cfg(tmp_path, mock.url, token="nope")).run(), 5)
    assert rc == EXIT_AUTH_FAILED


async def test_dedup_across_restart(tmp_path, mock):
    w = Watcher(cfg(tmp_path, mock.url))
    t = await start(w)
    await mock.wait_subscribed()
    await mock.emit_device_created(HUE)
    await mock.emit_device_created(HUE)
    await mock.emit_device_created(KASA)
    await wait_for(lambda: len(lines(tmp_path)) == 2)
    await asyncio.sleep(0.2)
    assert [r["device_id"] for r in lines(tmp_path)] == [HUE["id"], KASA["id"]]
    await stop(t)
    assert json.loads((tmp_path / "seen.json").read_text())["seen_device_ids"] == sorted([HUE["id"], KASA["id"]])
    # new process: state loaded from disk, still deduped
    w2 = Watcher(cfg(tmp_path, mock.url))
    t2 = await start(w2)
    await wait_for(lambda: w2.connections == 1)
    await mock.emit_device_created(HUE)
    await asyncio.sleep(0.3)
    assert len(lines(tmp_path)) == 2
    await stop(t2)


async def test_force_device_id_bypasses_dedup(tmp_path, mock):
    SeenDevices(tmp_path / "seen.json").add(HUE["id"])
    SeenDevices(tmp_path / "seen.json").add(KASA["id"])
    w = Watcher(cfg(tmp_path, mock.url, force_device_ids={HUE["id"]}))
    t = await start(w)
    await mock.wait_subscribed()
    await mock.emit_device_created(KASA)  # still deduped
    await mock.emit_device_created(HUE)   # forced
    await wait_for(lambda: len(lines(tmp_path)) == 1)
    await asyncio.sleep(0.2)
    recs = lines(tmp_path)
    assert len(recs) == 1 and recs[0]["device_id"] == HUE["id"] and recs[0]["forced"] is True
    await stop(t)


async def test_reconnect_after_server_drop(tmp_path, mock):
    w = Watcher(cfg(tmp_path, mock.url))
    t = await start(w)
    await mock.wait_subscribed()
    await mock.drop_all()
    await wait_for(lambda: w.connections == 2)
    await mock.wait_subscribed()
    await mock.emit_device_created(KASA)
    await wait_for(lambda: len(lines(tmp_path)) == 1)
    await stop(t)


async def test_reconnect_when_server_down_at_start(tmp_path, unused_tcp_port):
    w = Watcher(cfg(tmp_path, f"ws://127.0.0.1:{unused_tcp_port}/api/websocket"))
    t = await start(w)
    await asyncio.sleep(0.3)  # several failed attempts with backoff
    srv = await MockHA(token="good", port=unused_tcp_port).start()
    try:
        await srv.wait_subscribed()
        await srv.emit_device_created(HUE)
        await wait_for(lambda: len(lines(tmp_path)) == 1)
    finally:
        await stop(t)
        await srv.stop()


def test_backoff_exponential_capped_with_jitter():
    class Max(random.Random):
        def uniform(self, a, b):
            return b
    g = backoff_delays(1, 30, Max())
    assert [next(g) for _ in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    g = backoff_delays(1, 30, random.Random(0))
    vals = [next(g) for _ in range(50)]
    assert all(0 <= v <= 30 for v in vals) and len(set(vals)) > 40


def test_log_rotation(tmp_path):
    path = tmp_path / "rot.jsonl"
    lg = make_capture_logger(str(path), max_bytes=500, backup_count=2)
    for i in range(100):
        lg.info(json.dumps({"i": i, "pad": "x" * 50}))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["rot.jsonl", "rot.jsonl.1", "rot.jsonl.2"]


def test_state_file_atomic_and_no_tmp_left(tmp_path):
    s = SeenDevices(tmp_path / "sub" / "seen.json")
    s.add("a"); s.add("b")
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["seen.json"]
    assert "a" in SeenDevices(tmp_path / "sub" / "seen.json")


def test_token_age_warning():
    import datetime as dt
    assert check_token_age("2026-01-01", dt.date(2026, 10, 14))
    assert check_token_age("2026-10-01", dt.date(2026, 10, 14)) is None
    assert check_token_age(None) is None


def test_cli_missing_env_exits(monkeypatch):
    from ha_watcher.watcher import main
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.delenv("HA_URL", raising=False)
    assert main([]) != 0
