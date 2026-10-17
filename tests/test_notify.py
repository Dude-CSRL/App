import asyncio
import json
import logging
from pathlib import Path

import pytest

from ha_watcher import notify, notify_unmatched
from ha_watcher.watcher import Config, Watcher, parse_args
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture
from tests.test_watcher import lines, start, stop, wait_for

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
HUE_ENTS = load_entity_ids(FIX, HUE["id"])
KASA_ENTS = load_entity_ids(FIX, KASA["id"])
NID_HUE = "ha_watcher_unmatched_" + HUE["id"]
NID_KASA = "ha_watcher_unmatched_" + KASA["id"]


def cfg(tmp_path, url, profiles=FIX / "profiles", notify_mode="persistent", **kw):
    return Config(url=url, token="good", log_file=str(tmp_path / "cap.jsonl"),
                  state_file=str(tmp_path / "seen.json"), backoff_initial=0.05, backoff_max=0.2,
                  profiles_dir=str(profiles), match_settle_delay=0.05, notify_unmatched=notify_mode,
                  provision_log_file=str(tmp_path / "prov.jsonl"), backup_dir=str(tmp_path / "b"), **kw)


@pytest.fixture
async def mock():
    srv = await MockHA(token="good").start()
    yield srv
    await srv.stop()


def empty_profiles(tmp_path):
    d = tmp_path / "noprofiles"
    d.mkdir()
    return d


async def run_watcher(tmp_path, srv, devices, settle=0.3, **kw):
    w = Watcher(cfg(tmp_path, srv.url, **kw))
    t = await start(w)
    await srv.wait_subscribed()
    for d, e in devices:
        await srv.emit_device_created(d, e)
    await wait_for(lambda: len(lines(tmp_path)) >= len({d["id"] for d, _ in devices}))
    await asyncio.sleep(settle)
    await stop(t)


# ---------------------------------------------------------------- message

def test_message_has_everything_needed():
    data = notify.build_notification(HUE["id"], HUE, HUE_ENTS, None, "profiles")
    msg = data["message"]
    assert data["notification_id"] == NID_HUE
    assert "Hue color lamp 1" in data["title"] and "Signify Netherlands B.V." in data["title"]
    for needle in (HUE["id"], "Signify Netherlands B.V.", "Hue white and color ambiance A19",
                   "hue:00:17:88:01:0c:aa:bb:cc-0b", "zigbee:00:17:88:01:0c:aa:bb:cc", "light.hue_color_lamp_1",
                   f"python -m ha_watcher.draft_profile --device-id {HUE['id']} --live --profiles-dir profiles",
                   f"python -m ha_watcher.provision --device-id {HUE['id']} --profiles-dir profiles"):
        assert needle in msg, needle


def test_message_without_device_details():
    data = notify.build_notification("abc", None, None, "no_device_details", "/p")
    assert data["title"] == "New device needs a profile: abc"
    assert "could not be read" in data["message"] and "--profiles-dir /p" in data["message"]


def test_cli_default_is_persistent_but_config_default_off():
    assert parse_args([]).notify_unmatched == "persistent"
    assert parse_args(["--notify-unmatched", "off"]).notify_unmatched == "off"
    assert Config(url="x", token="y").notify_unmatched == "off"


# ---------------------------------------------------------------- watcher

async def test_unmatched_creates_notification_matched_does_not(mock, tmp_path):
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS), (KASA, KASA_ENTS)])
    # fixture profiles match Hue (with entity pattern) and Kasa -> nothing to notify
    assert mock.notifications == {}
    mock2 = await MockHA(token="good").start()
    try:
        (tmp_path / "b").mkdir(exist_ok=True)
        t2 = tmp_path / "second"
        t2.mkdir()
        await run_watcher(t2, mock2, [(HUE, HUE_ENTS), (KASA, KASA_ENTS)], profiles=empty_profiles(tmp_path))
        assert set(mock2.notifications) == {NID_HUE, NID_KASA}
        n = mock2.notifications[NID_KASA]
        assert "TP-Link" in n["message"] and KASA["id"] in n["message"] and "tplink:8006ABCDEF0123456789" in n["message"]
        assert "mac:54:af:97:12:34:56" in n["message"] and "ha_watcher.draft_profile" in n["message"]
    finally:
        await mock2.stop()


async def test_partially_matched_only_unmatched_notified(mock, tmp_path):
    only_kasa = tmp_path / "pk"
    only_kasa.mkdir()
    (only_kasa / "k.yaml").write_text("status: active\nmatch: {manufacturer: TP-Link, model: KP115}\n")
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS), (KASA, KASA_ENTS)], profiles=only_kasa)
    assert set(mock.notifications) == {NID_HUE}


async def test_stable_id_updates_instead_of_stacking(mock, tmp_path):
    np = empty_profiles(tmp_path)
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS)], profiles=np, force_device_ids={HUE["id"]})
    # watcher restarts with --force-device-id and the device is "created" again
    mock.subscribed.clear()
    w = Watcher(cfg(tmp_path, mock.url, profiles=np, force_device_ids={HUE["id"]}))
    t = await start(w)
    await mock.wait_subscribed()
    mock.devices[HUE["id"]]["name"] = "Renamed lamp"
    await mock.emit_device_created(mock.devices[HUE["id"]], [])
    await wait_for(lambda: len([x for x in mock.notification_log if x[0] == "create"]) == 2)
    await stop(t)
    assert [x for x in mock.notification_log if x[0] == "create"] == [("create", NID_HUE)] * 2
    assert list(mock.notifications) == [NID_HUE]
    assert "Renamed lamp" in mock.notifications[NID_HUE]["message"]   # updated in place


async def test_off_sends_nothing(mock, tmp_path):
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS)], profiles=empty_profiles(tmp_path), notify_mode="off")
    assert mock.notification_log == [] and mock.mutations == []


async def test_sent_regardless_of_provision_mode(mock, tmp_path):
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS)], profiles=empty_profiles(tmp_path), provision_mode="dry-run")
    assert NID_HUE in mock.notifications
    # dry-run provisioning changed nothing else: only the notification call is mutating
    assert [m["domain"] for m in mock.mutations] == ["persistent_notification"]


async def test_no_device_details_still_notifies(mock, tmp_path):
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS)], fetch_device_details=False)
    msg = mock.notifications[NID_HUE]["message"]
    assert "could not be read" in msg and HUE["id"] in msg
    assert lines(tmp_path)[0]["match"]["reason"] == "no_device_details"


async def test_notify_failure_is_logged_and_watcher_keeps_running(mock, tmp_path, caplog):
    mock.fail_services.add("persistent_notification.create")
    w = Watcher(cfg(tmp_path, mock.url, profiles=empty_profiles(tmp_path)))
    t = await start(w)
    await mock.wait_subscribed()
    with caplog.at_level(logging.WARNING, logger="ha_watcher"):
        await mock.emit_device_created(HUE, HUE_ENTS)
        await wait_for(lambda: "notification create" in caplog.text)
    mock.fail_services.clear()
    await mock.emit_device_created(KASA, KASA_ENTS)       # next device still captured and notified
    await wait_for(lambda: NID_KASA in mock.notifications)
    assert not t.done() and len(lines(tmp_path)) == 2
    await stop(t)


async def test_safe_call_never_raises():
    async def boom(p):
        raise ConnectionError("gone")
    r = await notify.notify_unmatched(boom, "d1", None)
    assert r["ok"] is False and "gone" in r["error"]
    assert (await notify.dismiss(boom, "d1"))["ok"] is False


async def test_dismiss_when_forced_relog_now_matches(mock, tmp_path):
    np = empty_profiles(tmp_path)
    await run_watcher(tmp_path, mock, [(HUE, HUE_ENTS)], profiles=np)
    assert NID_HUE in mock.notifications
    # a profile was added; watcher restarted with --force-device-id; create seen again -> matched -> dismissed
    mock.subscribed.clear()
    w = Watcher(cfg(tmp_path, mock.url, force_device_ids={HUE["id"]}))
    t = await start(w)
    await mock.wait_subscribed()
    await mock.emit_device_created(HUE, HUE_ENTS)
    await wait_for(lambda: NID_HUE not in mock.notifications)
    await stop(t)
    assert mock.notification_log[-1] == ("dismiss", NID_HUE)


async def test_matched_normal_create_sends_no_dismiss(mock, tmp_path):
    await run_watcher(tmp_path, mock, [(KASA, KASA_ENTS)])
    assert mock.notification_log == []


# ---------------------------------------------------------------- notify_unmatched CLI

def make_snapshot(tmp_path):
    from ha_watcher import snapshot
    from ha_watcher.jsonl import write_jsonl_atomic
    from ha_watcher.client import HAClient

    async def go(srv):
        async with HAClient(srv.url, "good") as ha:
            recs = await snapshot.take_snapshot(ha.call, [])
        write_jsonl_atomic(str(tmp_path / "snap.jsonl"), recs)
    return go


async def cli(mock, monkeypatch, *args):
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "good")
    return await asyncio.to_thread(notify_unmatched.main, list(args))


async def test_notify_unmatched_cli_dry_run_then_send(mock, tmp_path, monkeypatch, capsys):
    mock.add_device(HUE, HUE_ENTS)
    mock.add_device(KASA, KASA_ENTS)
    await make_snapshot(tmp_path)(mock)
    base = ["--snapshot-file", str(tmp_path / "snap.jsonl"), "--profiles-dir", str(FIX / "profiles")]
    assert await cli(mock, monkeypatch, *base) == 0              # profiles match both -> nothing unmatched
    assert "0 unmatched" in capsys.readouterr().out
    base = ["--snapshot-file", str(tmp_path / "snap.jsonl"), "--profiles-dir", str(empty_profiles(tmp_path))]
    assert await cli(mock, monkeypatch, *base) == 0
    out = capsys.readouterr().out
    assert "dry-run, nothing sent" in out and out.count("would create") == 2
    assert mock.notification_log == []                           # DEFAULT SENDS NOTHING
    assert await cli(mock, monkeypatch, *base, "--device-id", KASA["id"], "--send") == 0
    assert list(mock.notifications) == [NID_KASA]                # filter honoured
    assert await cli(mock, monkeypatch, *base, "--send") == 0
    assert set(mock.notifications) == {NID_HUE, NID_KASA}        # repeat updates, doesn't stack
    assert [x for x in mock.notification_log if x[0] == "create"].count(("create", NID_KASA)) == 2


async def test_notify_unmatched_cli_dismiss_matched(mock, tmp_path, monkeypatch):
    mock.add_device(HUE, HUE_ENTS)
    mock.add_device(KASA, KASA_ENTS)
    await make_snapshot(tmp_path)(mock)
    mock.notifications[NID_KASA] = {"notification_id": NID_KASA}
    args = ["--snapshot-file", str(tmp_path / "snap.jsonl"), "--profiles-dir", str(FIX / "profiles"),
            "--send", "--dismiss-matched"]
    assert await cli(mock, monkeypatch, *args) == 0
    assert NID_KASA not in mock.notifications


async def test_notify_unmatched_cli_errors(mock, tmp_path, monkeypatch):
    assert notify_unmatched.main(["--snapshot-file", str(tmp_path / "none")]) == notify_unmatched.EXIT_CONFIG_ERROR
    mock.add_device(KASA, KASA_ENTS)
    await make_snapshot(tmp_path)(mock)
    args = ["--snapshot-file", str(tmp_path / "snap.jsonl"), "--profiles-dir", str(empty_profiles(tmp_path)), "--send"]
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "wrong")
    assert await asyncio.to_thread(notify_unmatched.main, args) == notify_unmatched.EXIT_AUTH_FAILED
    monkeypatch.setenv("HA_TOKEN", "good")
    mock.fail_services.add("persistent_notification.create")
    assert await asyncio.to_thread(notify_unmatched.main, args) == notify_unmatched.EXIT_FAILED
    monkeypatch.delenv("HA_TOKEN")
    assert notify_unmatched.main(args) == notify_unmatched.EXIT_CONFIG_ERROR
