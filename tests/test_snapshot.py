import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ha_watcher import report, snapshot
from ha_watcher.client import HAClient
from ha_watcher.profiles import load_profiles
from ha_watcher.watcher import Config, Watcher
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture
from tests.test_watcher import lines, start, stop, wait_for

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
SONOS = {"id": "5050505050505050505050505050abcd", "name": "Living Room Sonos", "manufacturer": "Sonos, Inc.",
         "model": "Era 100", "identifiers": [["sonos", "RINCON_AAA"]], "connections": [["mac", "00:11:22:33:44:55"]],
         "area_id": None, "name_by_user": None, "suggested_area": None, "disabled_by": None}


@pytest.fixture
async def mock():
    srv = await MockHA(token="good").start()
    # three devices that already existed: no create events are ever emitted for them
    srv.add_device(HUE, load_entity_ids(FIX, HUE["id"]))
    srv.add_device(KASA, load_entity_ids(FIX, KASA["id"]))
    srv.add_device(SONOS, ["media_player.living_room_sonos"])
    yield srv
    await srv.stop()


async def snap(mock, profiles):
    async with HAClient(mock.url, "good") as ha:
        return await snapshot.take_snapshot(ha.call, profiles)


async def test_snapshot_covers_all_existing_devices_in_capture_shape(mock):
    profiles = load_profiles(FIX / "profiles").profiles
    recs = {r["device_id"]: r for r in await snap(mock, profiles)}
    assert set(recs) == {HUE["id"], KASA["id"], SONOS["id"]}
    r = recs[HUE["id"]]
    assert r["event_type"] == "snapshot" and r["action"] == "snapshot" and r["timestamp"]
    assert r["raw_payload"] == r["device"] == HUE
    assert r["entities"] == ["light.hue_color_lamp_1", "sensor.hue_color_lamp_1_signal_level"]
    assert [e["entity_id"] for e in r["entity_entries"]] == r["entities"]
    assert r["match"] == {"matched": True, "profile": "hue_bulb", "profile_file": "hue_bulb.yaml"}
    assert recs[KASA["id"]]["match"]["profile"] == "kasa_kp115"
    assert recs[SONOS["id"]]["match"]["matched"] is False
    # capture-line keys all present
    assert {"timestamp", "event_type", "device_id", "raw_payload", "device", "entities", "match", "forced"} <= r.keys()


async def test_snapshot_cli_writes_separate_file_and_leaves_live_state_alone(mock, tmp_path, monkeypatch, capsys):
    # live watcher artifacts that must not change
    live_log, state = tmp_path / "cap.jsonl", tmp_path / "seen.json"
    live_log.write_text('{"keep":"me"}\n')
    state.write_text('{"seen_device_ids": ["x"]}')
    before = (live_log.read_bytes(), state.read_bytes())
    out = tmp_path / "logs" / "device_snapshot.jsonl"
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "good")
    rc = await asyncio.to_thread(snapshot.main, ["--out", str(out), "--profiles-dir", str(FIX / "profiles")])
    assert rc == 0 and "3 devices (2 matched" in capsys.readouterr().out
    assert len(out.read_text().splitlines()) == 3
    assert (live_log.read_bytes(), state.read_bytes()) == before
    assert sorted(p.name for p in out.parent.iterdir()) == ["device_snapshot.jsonl"]  # no temp files left
    # re-running replaces (does not append)
    await asyncio.to_thread(snapshot.main, ["--out", str(out), "--profiles-dir", str(FIX / "profiles")])
    assert len(out.read_text().splitlines()) == 3
    assert mock.mutations == []                       # read-only


async def test_snapshot_does_not_use_dedup_state(mock, tmp_path, monkeypatch):
    """Devices already in the watcher's seen-state still appear in the snapshot (no dedup)."""
    from ha_watcher.state import SeenDevices
    SeenDevices(tmp_path / "seen.json").add(HUE["id"])
    recs = await snap(mock, [])
    assert HUE["id"] in {r["device_id"] for r in recs}
    assert json.loads((tmp_path / "seen.json").read_text())["seen_device_ids"] == [HUE["id"]]


async def test_snapshot_cli_errors(mock, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "wrong")
    out = tmp_path / "s.jsonl"
    assert await asyncio.to_thread(snapshot.main, ["--out", str(out)]) == snapshot.EXIT_AUTH_FAILED
    assert not out.exists()
    monkeypatch.delenv("HA_TOKEN")
    assert snapshot.main(["--out", str(out)]) == snapshot.EXIT_CONFIG_ERROR
    monkeypatch.setenv("HA_TOKEN", "good")
    monkeypatch.setenv("HA_URL", "ws://127.0.0.1:1/api/websocket")
    assert await asyncio.to_thread(snapshot.main, ["--out", str(out)]) == snapshot.EXIT_ERROR


async def test_report_accepts_snapshot_alone_and_merged_with_capture(mock, tmp_path, monkeypatch):
    snap_file = tmp_path / "device_snapshot.jsonl"
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "good")
    await asyncio.to_thread(snapshot.main, ["--out", str(snap_file), "--profiles-dir", str(FIX / "profiles")])
    # snapshot only (no capture log at all)
    rc = report.main(["--log-file", str(tmp_path / "missing.jsonl"), "--snapshot-file", str(snap_file),
                      "--out-dir", str(tmp_path / "r"), "--profiles-dir", str(FIX / "profiles")])
    assert rc == 0
    data = json.loads((tmp_path / "r" / "device_report.json").read_text())
    by = {d["device_id"]: d for d in data["devices"]}
    assert set(by) == {HUE["id"], KASA["id"], SONOS["id"]}
    assert by[SONOS["id"]]["manufacturer"] == "Sonos, Inc." and by[SONOS["id"]]["seen_via"] == ["snapshot"]
    assert by[HUE["id"]]["profile_name"] == "hue_bulb"        # entity patterns evaluated from snapshot entities
    assert "snapshot" in (tmp_path / "r" / "device_report.md").read_text()
    # merged with a capture log: Hue is deduped across both
    for f in ("capture.jsonl", "capture.jsonl.1"):
        shutil.copy(FIX / "report" / f, tmp_path / f)
    rep = report.summarize(str(tmp_path / "capture.jsonl"), str(FIX / "profiles"), [str(snap_file)])
    assert rep["device_count"] == 3
    hue = next(d for d in rep["devices"] if d["device_id"] == HUE["id"])
    assert hue["occurrences"] == 3 and hue["seen_via"] == ["capture", "snapshot"]
    # missing everything -> error
    assert report.main(["--log-file", str(tmp_path / "no.jsonl"), "--out-dir", str(tmp_path / "x")]) == 1


async def test_snapshot_alongside_running_watcher_does_not_affect_dedup(mock, tmp_path):
    from tests.test_watcher import cfg
    w = Watcher(cfg(tmp_path, mock.url))
    t = await start(w)
    await mock.wait_subscribed()
    recs = await snap(mock, [])
    assert len(recs) == 3 and lines(tmp_path) == []
    await mock.emit_device_created(HUE)               # genuine create still logged afterwards
    await wait_for(lambda: len(lines(tmp_path)) == 1)
    await stop(t)
