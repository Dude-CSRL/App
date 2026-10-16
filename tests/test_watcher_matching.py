import asyncio
import json
import shutil
from pathlib import Path

from ha_watcher.watcher import Config, Watcher
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture
from tests.test_watcher import lines, start, stop, wait_for

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
HUE_ENTS = load_entity_ids(FIX, HUE["id"])
KASA_ENTS = load_entity_ids(FIX, KASA["id"])


def cfg(tmp_path, url, profiles_dir=FIX / "profiles", **kw):
    return Config(url=url, token="good", log_file=str(tmp_path / "cap.jsonl"),
                  state_file=str(tmp_path / "seen.json"), backoff_initial=0.05, backoff_max=0.2,
                  profiles_dir=str(profiles_dir), match_settle_delay=0.05, **kw)


async def run_case(tmp_path, devices, expect=None, **kw):
    srv = await MockHA(token="good").start()
    try:
        w = Watcher(cfg(tmp_path, srv.url, **kw))
        t = await start(w)
        await srv.wait_subscribed()
        for dev, ents in devices:
            await srv.emit_device_created(dev, ents)
        await wait_for(lambda: len(lines(tmp_path)) == (expect or len(devices)))
        await stop(t)
        return {r["device_id"]: r for r in lines(tmp_path)}
    finally:
        await srv.stop()


async def test_matched_with_and_without_entity_patterns(tmp_path):
    recs = await run_case(tmp_path, [(HUE, HUE_ENTS), (KASA, KASA_ENTS)])
    assert recs[HUE["id"]]["match"] == {"matched": True, "profile": "hue_bulb", "profile_file": "hue_bulb.yaml"}
    assert recs[KASA["id"]]["match"]["profile"] == "kasa_kp115"
    assert recs[HUE["id"]]["entities"] == HUE_ENTS
    assert recs[KASA["id"]]["entities"] is None  # no entity patterns needed -> not fetched


async def test_existing_fields_unchanged(tmp_path):
    recs = await run_case(tmp_path, [(HUE, HUE_ENTS)])
    r = recs[HUE["id"]]
    assert r["raw_payload"]["event"]["data"] == {"action": "create", "device_id": HUE["id"]}
    assert r["device"] == HUE and r["event_type"] == "device_registry_updated"
    assert {"timestamp", "action", "forced"} <= r.keys()


async def test_unmatched_when_entities_do_not_fit_pattern(tmp_path):
    recs = await run_case(tmp_path, [(HUE, ["switch.unrelated"])])
    m = recs[HUE["id"]]["match"]
    assert m["matched"] is False and m["profile"] is None and m["reason"] == "no_matching_profile"


async def test_unmatched_without_profiles_dir_or_missing_dir(tmp_path):
    for i, pd in enumerate((None, tmp_path / "missing")):
        d = tmp_path / str(i)
        d.mkdir()
        recs = await run_case(d, [(KASA, KASA_ENTS)], profiles_dir=pd)
        assert recs[KASA["id"]]["match"]["matched"] is False


async def test_draft_not_matched_in_watcher(tmp_path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    shutil.copy(FIX / "profiles" / "kasa_kp115_draft.yaml", pdir)
    recs = await run_case(tmp_path, [(KASA, KASA_ENTS)], profiles_dir=pdir)
    assert recs[KASA["id"]]["match"]["matched"] is False


async def test_invalid_profiles_do_not_crash_watcher(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.ERROR, logger="ha_watcher"):
        recs = await run_case(tmp_path, [(KASA, KASA_ENTS)])
    assert recs[KASA["id"]]["match"]["matched"] is True
    text = caplog.text
    assert "INVALID PROFILE" in text and "bad_yaml.yaml" in text and "bad_schema.yaml" in text
    assert "2 profile file(s) skipped" in text


async def test_no_details_flag_logs_unmatched(tmp_path):
    recs = await run_case(tmp_path, [(KASA, KASA_ENTS)], fetch_device_details=False)
    assert recs[KASA["id"]]["match"]["reason"] == "no_device_details"


async def test_concurrent_events_each_logged_once(tmp_path):
    recs = await run_case(tmp_path, [(HUE, HUE_ENTS), (HUE, HUE_ENTS), (KASA, KASA_ENTS)], expect=2)
    await asyncio.sleep(0.2)
    assert len(recs) == 2 and len(lines(tmp_path)) == 2


def test_cli_profiles_dir_default():
    from ha_watcher.watcher import parse_args
    assert parse_args([]).profiles_dir == "profiles"
    assert parse_args(["--profiles-dir", "x"]).profiles_dir == "x"
