import asyncio
import json
import logging
from pathlib import Path

from ha_watcher import provision
from ha_watcher.watcher import Config, Watcher, parse_args
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture
from tests.test_watcher import lines, start, stop, wait_for

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
HUE_ENTS = load_entity_ids(FIX, HUE["id"])


def cfg(tmp_path, url, mode, **kw):
    return Config(url=url, token="good", log_file=str(tmp_path / "cap.jsonl"),
                  state_file=str(tmp_path / "seen.json"), backoff_initial=0.05, backoff_max=0.2,
                  profiles_dir=str(FIX / "provision_profiles"), match_settle_delay=0.05,
                  provision_mode=mode, provision_log_file=str(tmp_path / "prov.jsonl"),
                  backup_dir=str(tmp_path / "backups"), **kw)


def prov_lines(tmp_path):
    p = tmp_path / "prov.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


async def scenario(tmp_path, mode, expect_prov=True, setup=None, devices=((HUE, HUE_ENTS),)):
    srv = await MockHA(token="good").start()
    srv.extra_states.add("automation.welcome_hue")
    if setup:
        setup(srv)
    try:
        w = Watcher(cfg(tmp_path, srv.url, mode))
        t = await start(w)
        await srv.wait_subscribed()
        for d, e in devices:
            await srv.emit_device_created(d, e)
        await wait_for(lambda: len(lines(tmp_path)) == len(devices))
        if expect_prov:
            await wait_for(lambda: len(prov_lines(tmp_path)) >= 1)
        await asyncio.sleep(0.3)
        await stop(t)
        return srv
    except BaseException:
        await srv.stop()
        raise


async def test_default_is_off_no_provision_no_mutations(tmp_path):
    assert parse_args([]).provision == "off"
    srv = await scenario(tmp_path, "off", expect_prov=False)
    try:
        assert srv.mutations == [] and prov_lines(tmp_path) == []
        assert not (tmp_path / "prov.jsonl").exists()
    finally:
        await srv.stop()


async def test_dry_run_logs_plan_and_mutates_nothing(tmp_path):
    srv = await scenario(tmp_path, "dry-run")
    try:
        assert srv.mutations == []
        (rec,) = prov_lines(tmp_path)
        assert rec["mode"] == "dry-run" and rec["device_id"] == HUE["id"] and rec["profile"] == "hue_full"
        assert rec["summary"]["ok"] == 5 and rec["summary"]["failed"] == 0
        planned = [c for s in rec["steps"] for c in s["calls"] if c["mutating"]]
        assert planned and all(not c["sent"] for c in planned)
        # capture line is unchanged in shape and still carries the match
        assert lines(tmp_path)[0]["match"]["profile"] == "hue_full"
        assert "provision" not in lines(tmp_path)[0]
    finally:
        await srv.stop()


async def test_apply_mutates_after_match(tmp_path):
    srv = await scenario(tmp_path, "apply")
    try:
        (rec,) = prov_lines(tmp_path)
        assert rec["mode"] == "apply" and rec["summary"]["failed"] == 0
        assert srv.devices[HUE["id"]]["name_by_user"] == "Hue Bulb"
        assert srv.devices[HUE["id"]]["area_id"] == "living_room"
        assert "light.living_room_hue_bulb" in {e["entity_id"] for e in srv.entities}
        assert srv.lovelace[None]["config"]["views"][0]["cards"]
    finally:
        await srv.stop()


async def test_unmatched_device_is_not_provisioned(tmp_path):
    srv = await scenario(tmp_path, "apply", expect_prov=False, devices=((KASA, []),))
    try:
        assert srv.mutations == [] and prov_lines(tmp_path) == []
    finally:
        await srv.stop()


async def test_failures_are_logged_and_watcher_keeps_running(tmp_path):
    srv = await scenario(tmp_path, "apply", setup=lambda s: s.fail_types.update(
        {"config/device_registry/update", "config/entity_registry/update"}))
    try:
        (rec,) = prov_lines(tmp_path)
        assert rec["summary"]["failed"] >= 2 and rec["status"] == "done"
    finally:
        await srv.stop()


async def test_provision_crash_does_not_kill_watcher(tmp_path, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("provisioning exploded")
    monkeypatch.setattr(provision, "provision_device", boom)
    srv = await MockHA(token="good").start()
    try:
        w = Watcher(cfg(tmp_path, srv.url, "apply"))
        t = await start(w)
        await srv.wait_subscribed()
        with caplog.at_level(logging.ERROR, logger="ha_watcher"):
            await srv.emit_device_created(HUE, HUE_ENTS)
            await wait_for(lambda: len(prov_lines(tmp_path)) == 1)
        assert prov_lines(tmp_path)[0]["status"] == "error"
        assert "exploded" in caplog.text
        await srv.emit_device_created(KASA, [])        # watcher still alive and capturing
        await wait_for(lambda: len(lines(tmp_path)) == 2)
        assert not t.done()
        await stop(t)
    finally:
        await srv.stop()


async def test_apply_mode_announces_itself(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="ha_watcher"):
        Watcher(cfg(tmp_path, "ws://x", "apply"))
        Watcher(cfg(tmp_path, "ws://x", "dry-run"))
    assert "PROVISIONING IS ENABLED (apply)" in caplog.text and "dry-run mode" in caplog.text
