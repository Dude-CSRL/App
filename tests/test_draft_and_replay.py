import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from ha_watcher import draft_profile, provision, snapshot
from ha_watcher.client import HAClient
from ha_watcher.jsonl import write_jsonl_atomic
from ha_watcher.profiles import load_profiles
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
HUE_ENTS = load_entity_ids(FIX, HUE["id"])
NID_HUE = "ha_watcher_unmatched_" + HUE["id"]
DRAFT_NAME = "signify_netherlands_b_v__hue_white_and_color_ambiance_a19"


@pytest.fixture
async def mock():
    srv = await MockHA(token="good").start()
    srv.add_device(HUE, HUE_ENTS)
    srv.extra_states.add("automation.welcome_hue")
    yield srv
    await srv.stop()


async def run_cli(mod, mock, monkeypatch, *args):
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "good")
    return await asyncio.to_thread(mod.main, list(args))


def assert_valid_draft(pdir: Path, name=DRAFT_NAME):
    res = load_profiles(pdir)
    assert not res.errors and not res.warnings, (res.errors, res.warnings)
    (p,) = res.profiles
    assert p.name == name and p.status == "draft"
    assert provision.main(["--check-profiles", "--profiles-dir", str(pdir)]) == 0
    return p


# ---------------------------------------------------------------- draft generation

async def test_draft_live(mock, tmp_path, monkeypatch, capsys):
    pdir = tmp_path / "profiles"
    rc = await run_cli(draft_profile, mock, monkeypatch, "--device-id", HUE["id"], "--live", "--profiles-dir", str(pdir))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(pdir / f"{DRAFT_NAME}.yaml")          # prints the path
    p = assert_valid_draft(pdir)
    assert p.manufacturer == "Signify Netherlands B.V." and p.model == "Hue white and color ambiance A19"
    assert p.recipe["name"] is None and p.recipe["area"] is None and p.recipe["entity_id_cleanup"] == []
    text = (pdir / f"{DRAFT_NAME}.yaml").read_text()
    # commented hints use the final placeholder syntax
    assert '#   - "light.*"' in text and '#     to: "light.{area}_{name}"' in text
    assert "{entity:light}" in text and "{device_name}" in text
    data = yaml.safe_load(text)
    assert data["source"]["device_id"] == HUE["id"] and data["source"]["entity_ids"] == sorted(HUE_ENTS)


async def test_draft_is_default_live_and_never_matches(mock, tmp_path, monkeypatch):
    pdir = tmp_path / "profiles"
    assert await run_cli(draft_profile, mock, monkeypatch, "--device-id", HUE["id"], "--profiles-dir", str(pdir)) == 0
    res = await _replay(mock, pdir)
    assert res["status"] == "no_match"                       # status: draft is never matched


async def _replay(mock, pdir, apply=False, **kw):
    async with HAClient(mock.url, "good") as ha:
        return await provision.provision_device(ha.call, HUE["id"], load_profiles(pdir).profiles,
                                                provision.Options(apply=apply, backup_dir=str(pdir.parent / "b")), **kw)


def capture_line(device, entities, ts="2026-10-15T10:00:00+00:00"):
    return json.dumps({"timestamp": ts, "event_type": "device_registry_updated", "action": "create",
                       "device_id": device["id"], "forced": False, "raw_payload": {}, "device": device,
                       "entities": entities, "match": {"matched": False}})


async def test_draft_from_log_and_snapshot(mock, tmp_path, monkeypatch, capsys):
    log = tmp_path / "cap.jsonl"
    log.write_text(capture_line(HUE, None) + "\n{broken\n")  # entities not captured; malformed line tolerated
    pdir = tmp_path / "p1"
    assert draft_profile.main(["--device-id", HUE["id"], "--from-log", str(log), "--profiles-dir", str(pdir)]) == 0
    assert_valid_draft(pdir)
    text = (pdir / f"{DRAFT_NAME}.yaml").read_text()
    assert "entity IDs were not in this source" in text and "capture log cap.jsonl" in text
    # snapshot has entities -> hints present
    async with HAClient(mock.url, "good") as ha:
        write_jsonl_atomic(str(tmp_path / "snap.jsonl"), await snapshot.take_snapshot(ha.call, []))
    pdir2 = tmp_path / "p2"
    assert draft_profile.main(["--device-id", HUE["id"], "--from-snapshot", str(tmp_path / "snap.jsonl"),
                               "--profiles-dir", str(pdir2)]) == 0
    assert_valid_draft(pdir2)
    assert '#   - "light.*"' in (pdir2 / f"{DRAFT_NAME}.yaml").read_text()


async def test_draft_never_overwrites_and_out_override(mock, tmp_path, monkeypatch, capsys):
    pdir = tmp_path / "profiles"
    args = ["--device-id", HUE["id"], "--profiles-dir", str(pdir)]
    assert await run_cli(draft_profile, mock, monkeypatch, *args) == 0
    f = pdir / f"{DRAFT_NAME}.yaml"
    f.write_text("# my edits\n" + f.read_text())
    assert await run_cli(draft_profile, mock, monkeypatch, *args) == draft_profile.EXIT_EXISTS
    assert f.read_text().startswith("# my edits")
    assert "not overwriting" in capsys.readouterr().err
    out = tmp_path / "elsewhere" / "mine.yaml"
    assert await run_cli(draft_profile, mock, monkeypatch, *args, "--out", str(out)) == 0 and out.exists()
    assert await run_cli(draft_profile, mock, monkeypatch, *args, "--out", str(out)) == draft_profile.EXIT_EXISTS


async def test_draft_errors(mock, tmp_path, monkeypatch):
    base = ["--profiles-dir", str(tmp_path / "p")]
    assert await run_cli(draft_profile, mock, monkeypatch, "--device-id", "nope", *base) == draft_profile.EXIT_ERROR
    assert draft_profile.main(["--device-id", "x", "--from-log", str(tmp_path / "missing"), *base]) == draft_profile.EXIT_CONFIG_ERROR
    monkeypatch.setenv("HA_URL", mock.url)
    monkeypatch.setenv("HA_TOKEN", "wrong")
    assert await asyncio.to_thread(draft_profile.main, ["--device-id", HUE["id"], *base]) == draft_profile.EXIT_AUTH_FAILED
    monkeypatch.delenv("HA_TOKEN")
    assert draft_profile.main(["--device-id", HUE["id"], *base]) == draft_profile.EXIT_CONFIG_ERROR


async def test_draft_with_null_manufacturer_still_validates(mock, tmp_path, monkeypatch):
    bare = {"id": "ffff0000ffff0000ffff0000ffff0000", "name": "Mystery", "manufacturer": None, "model": None,
            "identifiers": [], "connections": []}
    mock.add_device(bare, [])
    pdir = tmp_path / "p"
    assert await run_cli(draft_profile, mock, monkeypatch, "--device-id", bare["id"], "--profiles-dir", str(pdir)) == 0
    assert_valid_draft(pdir, name="unknown__unknown")


def test_entity_hints_suffixes():
    pats, rules, card = draft_profile.entity_hints(
        ["light.x_lamp", "sensor.x_lamp_signal", "sensor.x_lamp_battery", "sensor.other"], "X Lamp")
    assert pats == ["light.*", "sensor.*"]
    tos = {r["from"]: r["to"] for r in rules}
    assert tos["light.x_lamp"] == "light.{area}_{name}"
    assert tos["sensor.x_lamp_signal"] == "sensor.{area}_{name}_signal"
    assert tos["sensor.other"] == "sensor.{area}_{name}_other"
    assert card["entities"] == ["{entity:light}", "{entity:sensor}"]
    assert draft_profile.entity_hints(None, "x") == ([], [], None)


# ---------------------------------------------------------------- fix profile, then replay

def activate(text: str) -> str:
    """What a human does to a draft: fill name/area, uncomment the hints, set status: active."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s in ("entity_id_cleanup: []", "dashboard_card: null"):
            continue
        if s.startswith("# entity_id_cleanup:") or s.startswith("#   - from:") or s.startswith("#     to:") \
                or s.startswith("# dashboard_card:") or s.startswith("#   dashboard:") or s.startswith("#   view:") \
                or s.startswith("#   card:"):
            line = line.replace("# ", "", 1)
        out.append(line)
    t = "\n".join(out) + "\n"
    return (t.replace("status: draft", "status: active").replace("  name: null ", '  name: "Hue Bulb" ')
             .replace("  area: null ", "  area: Living Room ").replace("<view path or title>", "lights")
             .replace("welcome_automation: null ", "welcome_automation: automation.welcome_hue "))


async def test_fix_profile_then_replay_flow(mock, tmp_path, monkeypatch, capsys):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    common = ["--device-id", HUE["id"], "--profiles-dir", str(pdir), "--no-log", "--backup-dir", str(tmp_path / "b")]
    mock.notifications[NID_HUE] = {"notification_id": NID_HUE}          # what the watcher raised earlier

    # 1. no profile: replay says so (exit 4) and points at the draft generator
    assert await run_cli(provision, mock, monkeypatch, *common) == provision.EXIT_NO_TARGET
    assert "draft_profile --device-id " + HUE["id"] in capsys.readouterr().out

    # 2. generate a draft; it is still not matched
    assert await run_cli(draft_profile, mock, monkeypatch, "--device-id", HUE["id"], "--profiles-dir", str(pdir)) == 0
    f = pdir / f"{DRAFT_NAME}.yaml"
    assert await run_cli(provision, mock, monkeypatch, *common) == provision.EXIT_NO_TARGET

    # 3. human fixes the profile (fill in, uncomment hints, activate); validator accepts it with no warnings
    f.write_text(activate(f.read_text()))
    res = load_profiles(pdir)
    assert not res.errors and not res.warnings and res.profiles[0].active
    assert res.profiles[0].recipe["entity_id_cleanup"][0] == {"from": "light.hue_color_lamp_1", "to": "light.{area}_{name}"}

    # 4. replay as DRY-RUN: plan only, nothing changes, notification untouched
    assert await run_cli(provision, mock, monkeypatch, *common) == 0
    out = capsys.readouterr().out
    assert "mode=DRY-RUN" in out and "[WOULD SEND]" in out
    assert mock.mutations == [] and NID_HUE in mock.notifications

    # 5. replay with --apply: provisions the already-registered device and dismisses the notification
    assert await run_cli(provision, mock, monkeypatch, *common, "--apply") == 0
    dev = mock.devices[HUE["id"]]
    assert dev["name_by_user"] == "Hue Bulb" and dev["area_id"] == "living_room"
    assert "light.living_room_hue_bulb" in {e["entity_id"] for e in mock.entities}
    assert NID_HUE not in mock.notifications and mock.notification_log[-1] == ("dismiss", NID_HUE)

    # 6. a further replay is a no-op (idempotent; the draft has no entity_patterns so it still matches)
    n = len(mock.mutations)
    assert await run_cli(provision, mock, monkeypatch, *common, "--apply", "--no-dismiss") == 0
    assert [m for m in mock.mutations[n:] if m["type"] != "call_service"] == []


async def test_apply_dry_run_does_not_dismiss(mock, tmp_path, monkeypatch):
    pdir = tmp_path / "profiles"
    shutil.copytree(FIX / "provision_profiles", pdir)
    mock.notifications[NID_HUE] = {}
    assert await run_cli(provision, mock, monkeypatch, "--device-id", HUE["id"], "--profiles-dir", str(pdir), "--no-log") == 0
    assert NID_HUE in mock.notifications and mock.notification_log == []
