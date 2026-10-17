import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ha_watcher import provision
from ha_watcher.client import AuthFailed, HAClient
from ha_watcher.profiles import load_profiles
from tests.mock_ha_server import MockHA, load_entity_ids, load_fixture

FIX = Path(__file__).parent / "fixtures"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
HUE_ENTS = load_entity_ids(FIX, HUE["id"])  # light.hue_color_lamp_1, sensor.hue_color_lamp_1_signal_level
PROFILES = load_profiles(FIX / "provision_profiles").profiles
NEW_LIGHT = "light.living_room_hue_bulb"
NEW_SENSOR = "sensor.living_room_hue_bulb_signal_level"


@pytest.fixture
async def mock():
    srv = await MockHA(token="good").start()
    srv.add_device(HUE, HUE_ENTS)
    srv.extra_states.add("automation.welcome_hue")
    yield srv
    await srv.stop()


async def run(mock, apply=False, profiles=PROFILES, tmp=None, profile_name=None, **kw):
    opts = provision.Options(apply=apply, backup_dir=str(tmp / "backups") if tmp else "/tmp/ha_test_backups", **kw)
    async with HAClient(mock.url, "good") as ha:
        return await provision.provision_device(ha.call, HUE["id"], profiles, opts, profile_name=profile_name)


def steps(res):
    return {s["step"]: s for s in res["steps"]}


def mutating_types(mock):
    return [m["type"] for m in mock.mutations]


# ---------------------------------------------------------------- dry-run

async def test_dry_run_sends_no_mutating_call_and_plans_exact_calls(mock):
    before = json.dumps([mock.devices, mock.entities, mock.lovelace], sort_keys=True)
    res = await run(mock, apply=False)
    assert res["mode"] == "dry-run" and res["status"] == "done" and res["profile"] == "hue_full"
    assert mock.mutations == []                      # nothing mutating reached the server
    assert json.dumps([mock.devices, mock.entities, mock.lovelace], sort_keys=True) == before
    s = steps(res)
    assert all(v["status"] == "ok" and v["changed"] for v in s.values()), s
    planned = [c for st in res["steps"] for c in st["calls"] if c["mutating"]]
    assert all(c["sent"] is False and c["dry_run"] for c in planned)
    by_type = [(c["type"], c["payload"]) for c in planned]
    assert ("config/device_registry/update", {"device_id": HUE["id"], "area_id": "living_room"}) in by_type
    assert ("config/device_registry/update", {"device_id": HUE["id"], "name_by_user": "Hue Bulb"}) in by_type
    assert ("config/entity_registry/update",
            {"entity_id": "light.hue_color_lamp_1", "new_entity_id": NEW_LIGHT}) in by_type
    assert ("config/entity_registry/update",
            {"entity_id": "sensor.hue_color_lamp_1_signal_level", "new_entity_id": NEW_SENSOR}) in by_type
    assert ("call_service", {"domain": "automation", "service": "trigger",
                             "target": {"entity_id": "automation.welcome_hue"}}) in by_type
    save = next(p for t, p in by_type if t == "lovelace/config/save")
    card = save["config"]["views"][0]["cards"][0]
    # dry-run plan already uses the PLANNED entity id and renders placeholders; Jinja stays literal
    assert card == {"type": "light", "entity": NEW_LIGHT, "name": "Hue Bulb (Living Room)",
                    "note": "{{ states('sensor.x') }}"}
    assert provision.format_result(res).count("[WOULD SEND]") == len(planned)


async def test_dry_run_is_default_options():
    assert provision.Options().apply is False


async def test_session_blocks_all_mutating_types_without_apply():
    sent = []

    async def caller(p):
        sent.append(p)
        return {"success": True, "result": []}
    sess = provision.Session(caller, apply=False)
    for t in provision.MUTATING_TYPES:
        await sess.call({"type": t}, [])
    assert sent == []
    await sess.call({"type": "config/device_registry/list"}, [])
    assert [p["type"] for p in sent] == ["config/device_registry/list"]


# ---------------------------------------------------------------- apply

async def test_apply_executes_and_mutates(mock, tmp_path):
    res = await run(mock, apply=True, tmp=tmp_path)
    assert res["summary"] == {"ok": 5, "skipped": 0, "failed": 0, "changed": 5}
    dev = mock.devices[HUE["id"]]
    assert dev["name_by_user"] == "Hue Bulb" and dev["area_id"] == "living_room"
    assert sorted(e["entity_id"] for e in mock.entities) == sorted([NEW_LIGHT, NEW_SENSOR])
    cards = mock.lovelace[None]["config"]["views"][0]["cards"]
    assert cards[0]["entity"] == NEW_LIGHT
    assert any(m["type"] == "call_service" and m["target"]["entity_id"] == "automation.welcome_hue"
               for m in mock.mutations)
    backup = Path(steps(res)["dashboard_card"]["backup"])
    assert json.loads(backup.read_text())["views"][0]["cards"] == []   # previous config saved first
    assert all(c["sent"] for st in res["steps"] for c in st["calls"] if c["mutating"])


async def test_apply_is_idempotent(mock, tmp_path):
    await run(mock, apply=True, tmp=tmp_path)
    n = len(mock.mutations)
    # NOTE: entity_patterns ('light.hue_*') no longer match after the rename, so automatic matching
    # reports no_match on a re-run (documented); the explicit profile replays safely.
    assert (await run(mock, apply=True, tmp=tmp_path))["status"] == "no_match"
    res = await run(mock, apply=True, tmp=tmp_path, profile_name="hue_full")
    assert len(mock.mutations) == n                  # second run changes nothing
    s = steps(res)
    assert res["summary"]["changed"] == 0 and res["summary"]["failed"] == 0
    assert s["device_name"]["reason"] == "already_ok" and s["device_area"]["reason"] == "already_ok"
    assert s["dashboard_card"]["reason"] == "already_ok"
    assert s["welcome_automation"]["reason"] == "no_changes_this_run"
    assert all(a["reason"] == "already_ok" for a in s["entity_id_cleanup"]["actions"])
    assert len(mock.lovelace[None]["config"]["views"][0]["cards"]) == 1


async def test_force_welcome_fires_even_without_changes(mock, tmp_path):
    await run(mock, apply=True, tmp=tmp_path)
    n = len(mock.mutations)
    res = await run(mock, apply=True, tmp=tmp_path, force_welcome=True, profile_name="hue_full")
    assert steps(res)["welcome_automation"]["status"] == "ok"
    assert len(mock.mutations) == n + 1


# ---------------------------------------------------------------- area unresolved

def with_recipe(tmp_path, recipe_yaml):
    d = tmp_path / "profiles"
    d.mkdir(exist_ok=True)
    (d / "p.yaml").write_text(
        "name: p\nstatus: active\nmatch: {manufacturer: Signify Netherlands B.V., "
        "model: Hue white and color ambiance A19}\nrecipe:\n" + recipe_yaml)
    return load_profiles(d).profiles


async def test_area_null_is_unresolved_and_nothing_area_related_happens(mock, tmp_path):
    profs = with_recipe(tmp_path, "  name: 'Hue {area_name}'\n  area: null\n"
                                  "  entity_id_cleanup: [{from: 'light.hue_*', to: 'light.{area}_{1}'}]\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    s = steps(res)
    assert s["device_area"]["status"] == "skipped" and s["device_area"]["reason"] == "area_unresolved"
    assert s["device_name"]["reason"] == "area_unresolved"          # name template needs the area
    assert s["entity_id_cleanup"]["actions"][0]["reason"] == "area_unresolved"
    assert mock.mutations == []
    assert mock.devices[HUE["id"]].get("area_id") is None


async def test_area_name_not_in_registry_is_unresolved_never_created(mock, tmp_path):
    profs = with_recipe(tmp_path, "  area: Garage\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    st = steps(res)["device_area"]
    assert st["reason"] == "area_unresolved" and "Garage" in st["detail"]
    assert mock.mutations == [] and len(mock.areas) == 2


async def test_area_matches_alias_case_insensitively(mock, tmp_path):
    profs = with_recipe(tmp_path, "  area: cooking\n")
    await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    assert mock.devices[HUE["id"]]["area_id"] == "kitchen"


# ---------------------------------------------------------------- entity cleanup edge cases

async def test_collision_with_registry_or_state_is_skipped(mock, tmp_path):
    mock.extra_states.add(NEW_LIGHT)                       # exists only as a state
    mock.entities.append({"entity_id": NEW_SENSOR, "device_id": None, "platform": "other"})  # other registry entry
    res = await run(mock, apply=True, tmp=tmp_path)
    acts = steps(res)["entity_id_cleanup"]["actions"]
    assert [a["reason"] for a in acts] == ["collision", "collision"]
    assert "state machine" in acts[0]["detail"] and "entity registry" in acts[1]["detail"]
    assert steps(res)["entity_id_cleanup"]["status"] == "skipped"
    assert not [m for m in mock.mutations if m["type"] == "config/entity_registry/update"]
    # dashboard falls back to the unrenamed entity id
    assert mock.lovelace[None]["config"]["views"][0]["cards"][0]["entity"] == "light.hue_color_lamp_1"


async def test_two_entities_planned_to_same_id_second_skipped(mock, tmp_path):
    mock.entities.clear()
    mock.add_device(HUE, ["light.hue_color_lamp_1", "light.hue_second"])
    profs = with_recipe(tmp_path, "  area: Living Room\n  entity_id_cleanup: [{from: 'light.hue_*', to: 'light.{area}'}]\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    acts = steps(res)["entity_id_cleanup"]["actions"]
    assert sorted(a["status"] for a in acts) == ["ok", "skipped"]
    assert [a for a in acts if a["status"] == "skipped"][0]["reason"] == "collision"


@pytest.mark.parametrize("to,reason", [
    ("light.{area}__x", "invalid_entity_id"),
    ("light.Bad Id", "invalid_entity_id"),
    ("switch.{area}_x", "domain_changed"),
    ("light.{area}_*", "invalid_entity_id"),
    ("light.{nope}", "unknown_placeholder:{nope}"),
])
async def test_invalid_results_are_skipped(mock, tmp_path, to, reason):
    profs = with_recipe(tmp_path, f"  area: Living Room\n  entity_id_cleanup: [{{from: 'light.hue_*', to: \"{to}\"}}]\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    act = steps(res)["entity_id_cleanup"]["actions"][0]
    assert act["status"] == "skipped" and act["reason"] == reason
    assert not [m for m in mock.mutations if m["type"] == "config/entity_registry/update"]


async def test_non_idempotent_rule_is_refused(mock, tmp_path):
    profs = with_recipe(tmp_path, "  entity_id_cleanup: [{from: 'light.*', to: 'light.x_{1}'}]\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    assert steps(res)["entity_id_cleanup"]["actions"][0]["reason"] == "rule_not_idempotent"
    assert not mock.mutations


# ---------------------------------------------------------------- dashboard skips

DASH = "  dashboard_card:\n    dashboard: {dash}\n    view: {view}\n    card: {{type: light, entity: '{{entity:light}}'}}\n"


@pytest.mark.parametrize("setup,dash,view,reason", [
    (lambda m: m.lovelace[None].update(mode="yaml"), "lovelace", "lights", "dashboard_not_storage_mode (mode=yaml)"),
    (lambda m: m.lovelace[None].update(config=None), "lovelace", "lights", "dashboard_has_no_saved_config"),
    (lambda m: None, "lovelace", "nope", "view_not_found"),
    (lambda m: m.lovelace.__setitem__("kiosk", {"mode": "yaml", "config": {"views": []}}), "kiosk", "x",
     "dashboard_not_storage_mode (mode=yaml)"),
    (lambda m: None, "missing", "x", "dashboard_not_found"),
    (lambda m: m.lovelace[None]["config"]["views"][0].update(type="sections"), "lovelace", "lights",
     "view_type_unsupported (sections)"),
])
async def test_dashboard_skipped_with_reason_and_never_saved(mock, tmp_path, setup, dash, view, reason):
    setup(mock)
    profs = with_recipe(tmp_path, DASH.format(dash=dash, view=view))
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    st = steps(res)["dashboard_card"]
    assert st["status"] == "skipped" and st["reason"] == reason
    assert not [m for m in mock.mutations if m["type"] == "lovelace/config/save"]


async def test_dashboard_named_storage_dashboard_and_default_view(mock, tmp_path):
    mock.lovelace["tablet"] = {"mode": "storage", "config": {"views": [{"title": "Home", "cards": []}]}}
    profs = with_recipe(tmp_path, "  dashboard_card:\n    dashboard: tablet\n    card: {type: light, entity: light.hue_color_lamp_1}\n")
    res = await run(mock, apply=True, profiles=profs, tmp=tmp_path)
    assert steps(res)["dashboard_card"]["status"] == "ok"
    assert mock.lovelace["tablet"]["config"]["views"][0]["cards"][0]["entity"] == "light.hue_color_lamp_1"
    save = next(m for m in mock.mutations if m["type"] == "lovelace/config/save")
    assert save["url_path"] == "tablet"


async def test_dashboard_unresolved_entity_placeholder_skipped(mock, tmp_path):
    profs = with_recipe(tmp_path, DASH.format(dash="lovelace", view="lights").replace("entity:light", "entity:climate"))
    res = await run(mock, profiles=profs)
    assert steps(res)["dashboard_card"]["reason"] == "unresolved_placeholder:{entity:climate}"


# ---------------------------------------------------------------- welcome

async def test_welcome_null_and_missing_and_wrong_target(mock, tmp_path):
    r = await run(mock, apply=True, profiles=with_recipe(tmp_path, "  area: Kitchen\n  welcome_automation: null\n"), tmp=tmp_path)
    assert steps(r)["welcome_automation"]["reason"] == "no_welcome_automation_in_recipe"
    mock.extra_states.discard("automation.welcome_hue")
    r = await run(mock, apply=True, profiles=with_recipe(tmp_path, "  area: Living Room\n  welcome_automation: automation.welcome_hue\n"), tmp=tmp_path)
    assert steps(r)["welcome_automation"]["reason"] == "automation_not_found"
    r = await run(mock, apply=True, profiles=with_recipe(tmp_path, "  area: Kitchen\n  welcome_automation: script.hello\n"), tmp=tmp_path)
    assert steps(r)["welcome_automation"]["reason"] == "unsupported_welcome_target"
    assert not [m for m in mock.mutations if m["type"] == "call_service"]


# ---------------------------------------------------------------- failures never crash

async def test_failed_call_marks_step_failed_and_run_continues(mock, tmp_path):
    mock.fail_types.add("config/device_registry/update")
    res = await run(mock, apply=True, tmp=tmp_path)
    s = steps(res)
    assert s["device_name"]["status"] == "failed" and "update_failed" in s["device_name"]["reason"]
    assert s["device_area"]["status"] == "failed"
    assert s["entity_id_cleanup"]["status"] == "ok" and s["dashboard_card"]["status"] == "ok"
    assert res["summary"]["failed"] == 2
    assert s["device_name"]["calls"][0]["ok"] is False
    # area failed -> its templates can't resolve, but the run still completed
    assert res["status"] == "done"


async def test_exception_inside_step_is_contained(mock, monkeypatch):
    async def boom(sess, ctx):
        raise ValueError("kaboom")
    monkeypatch.setattr(provision, "STEP_FUNCS", (("device_area", boom),) + provision.STEP_FUNCS[1:])
    res = await run(mock)
    assert steps(res)["device_area"]["status"] == "failed" and "kaboom" in steps(res)["device_area"]["reason"]
    assert steps(res)["device_name"]["status"] in ("ok", "skipped")


async def test_transport_errors_become_error_result():
    async def caller(p):
        raise ConnectionError("down")
    res = await provision.provision_device(caller, "x", PROFILES, provision.Options())
    assert res["status"] == "error" and "down" in res["error"] and res["summary"]["failed"] == 1


async def test_no_match_and_unknown_device_and_explicit_profile(mock):
    res = await run(mock, profiles=[])
    assert res["status"] == "no_match" and mock.mutations == []
    async with HAClient(mock.url, "good") as ha:
        res = await provision.provision_device(ha.call, "nope", PROFILES, provision.Options())
        assert res["status"] == "device_not_found"
        res = await provision.provision_device(ha.call, HUE["id"], PROFILES, provision.Options(), profile_name="hue_full")
        assert res["matched_by"] == "explicit" and res["status"] == "done"
        res = await provision.provision_device(ha.call, HUE["id"], PROFILES, provision.Options(), profile_name="zzz")
        assert res["status"] == "no_match"


async def test_draft_profile_not_used(mock, tmp_path):
    d = tmp_path / "pd"
    d.mkdir()
    shutil.copy(FIX / "provision_profiles" / "hue_full.yaml", d / "hue_full.yaml")
    (d / "hue_full.yaml").write_text((d / "hue_full.yaml").read_text().replace("status: active", "status: draft"))
    res = await run(mock, profiles=load_profiles(d).profiles)
    assert res["status"] == "no_match"


# ---------------------------------------------------------------- CLI

async def cli(mock, *args, env=None, monkeypatch=None):
    monkeypatch.setenv("HA_TOKEN", "good")
    monkeypatch.setenv("HA_URL", mock.url)
    return await asyncio.to_thread(provision.main, list(args))


async def test_cli_dry_run_default_then_apply(mock, tmp_path, monkeypatch, capsys):
    common = ["--device-id", HUE["id"], "--profiles-dir", str(FIX / "provision_profiles"),
              "--provision-log", str(tmp_path / "prov.jsonl"), "--backup-dir", str(tmp_path / "b")]
    assert await cli(mock, *common, monkeypatch=monkeypatch) == 0
    assert mock.mutations == []
    out = capsys.readouterr().out
    assert "mode=DRY-RUN" in out and "[WOULD SEND]" in out
    assert await cli(mock, *common, "--apply", monkeypatch=monkeypatch) == 0
    assert mock.devices[HUE["id"]]["name_by_user"] == "Hue Bulb"
    lines = [json.loads(l) for l in (tmp_path / "prov.jsonl").read_text().splitlines()]
    assert [l["mode"] for l in lines] == ["dry-run", "apply"]


async def test_cli_exit_codes(mock, tmp_path, monkeypatch, capsys):
    pd = str(FIX / "provision_profiles")
    base = ["--profiles-dir", pd, "--no-log"]
    assert await cli(mock, "--device-id", "nope", *base, monkeypatch=monkeypatch) == provision.EXIT_NO_TARGET
    assert await cli(mock, "--device-id", HUE["id"], "--profiles-dir", str(tmp_path), "--no-log",
                     monkeypatch=monkeypatch) == provision.EXIT_NO_TARGET
    mock.fail_types.add("config/device_registry/update")
    assert await cli(mock, "--device-id", HUE["id"], "--apply", *base, monkeypatch=monkeypatch) == provision.EXIT_STEP_FAILED
    monkeypatch.setenv("HA_TOKEN", "wrong")
    monkeypatch.setenv("HA_URL", mock.url)
    assert await asyncio.to_thread(provision.main, ["--device-id", HUE["id"], *base]) == provision.EXIT_AUTH_FAILED
    monkeypatch.delenv("HA_TOKEN")
    assert provision.main(["--device-id", "x"]) == provision.EXIT_CONFIG_ERROR


def test_cli_check_profiles(capsys):
    assert provision.main(["--check-profiles", "--profiles-dir", str(FIX / "profiles")]) == 1  # has invalid files
    out = capsys.readouterr().out
    assert "INVALID  bad_yaml.yaml" in out and "WARNING  hue_bulb.yaml" in out
    assert provision.main(["--check-profiles", "--profiles-dir", str(FIX / "provision_profiles")]) == 0


async def test_client_raises_auth_failed():
    srv = await MockHA(bad_token=True).start()
    try:
        with pytest.raises(AuthFailed):
            async with HAClient(srv.url, "x"):
                pass
    finally:
        await srv.stop()
