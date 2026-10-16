from pathlib import Path

import yaml

from ha_watcher import matcher, report
from ha_watcher.profiles import load_profiles, validate_profile
from tests.mock_ha_server import load_fixture

FIX = Path(__file__).parent / "fixtures"
PROFILES = FIX / "profiles"
HUE = load_fixture(FIX / "device_created_hue_bulb.json")
KASA = load_fixture(FIX / "device_created_kasa_plug.json")
HUE_ENTS = ["light.hue_color_lamp_1", "sensor.hue_color_lamp_1_signal_level"]
ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


# ---- loading / validation -------------------------------------------------

def test_load_fixture_dir_reports_invalid_without_raising():
    res = load_profiles(PROFILES)
    assert sorted(p.name for p in res.profiles) == ["hue_bulb", "kasa_draft_only", "kasa_kp115"]
    assert [p.name for p in res.active] == ["hue_bulb", "kasa_kp115"]
    assert [p.name for p in res.drafts] == ["kasa_draft_only"]
    errs = {e.file: e.message for e in res.errors}
    assert set(errs) == {"bad_yaml.yaml", "bad_schema.yaml"}
    assert "YAML syntax error" in errs["bad_yaml.yaml"]
    msg = errs["bad_schema.yaml"]
    assert "match.model is required" in msg and "unknown top-level key 'colour'" in msg
    assert "recipe.area must be a non-empty string" in msg and "entity_id_cleanup[0]" in msg


def test_hue_profile_recipe_parsed():
    hue = next(p for p in load_profiles(PROFILES).profiles if p.name == "hue_bulb")
    assert hue.entity_patterns == ("light.hue_*",)
    assert hue.recipe["area"] == "Living Room"
    assert hue.recipe["entity_id_cleanup"] == [{"from": "light.hue_*", "to": "light.living_room_*"}]


def test_missing_dir_and_empty_file(tmp_path):
    res = load_profiles(tmp_path / "nope")
    assert res.profiles == [] and res.errors == [] and not res.directory_found
    write(tmp_path, "empty.yaml", "")
    res = load_profiles(tmp_path)
    assert res.directory_found and [e.file for e in res.errors] == ["empty.yaml"]


def test_duplicate_names_second_file_rejected(tmp_path):
    body = "name: dup\nstatus: active\nmatch: {manufacturer: A, model: B}\n"
    write(tmp_path, "a.yaml", body)
    write(tmp_path, "b.yml", body)
    res = load_profiles(tmp_path)
    assert [p.file for p in res.profiles] == ["a.yaml"]
    assert "duplicate profile name" in res.errors[0].message


def test_name_defaults_to_file_stem_and_status_required(tmp_path):
    p, problems = validate_profile({"status": "active", "match": {"manufacturer": "A", "model": "B"}}, "thing.yaml")
    assert p.name == "thing" and not problems
    p, problems = validate_profile({"match": {"manufacturer": "A", "model": "B"}}, "thing.yaml")
    assert p is None and any("status is required" in x for x in problems)


def test_active_requires_manufacturer_and_model_draft_does_not():
    _, problems = validate_profile({"status": "active", "match": {"manufacturer": "A"}}, "x.yaml")
    assert any("match.model is required" in x for x in problems)
    p, problems = validate_profile({"status": "draft", "match": {"manufacturer": None, "model": None}}, "x.yaml")
    assert p is not None and not problems


def test_repo_example_profile_is_valid_draft():
    res = load_profiles(ROOT / "profiles")
    assert not res.errors
    ex = next(p for p in res.profiles if p.name == "example_hue_bulb")
    assert ex.status == "draft" and ex.recipe["dashboard_card"]["dashboard"] == "lovelace"
    assert matcher.match_device(HUE, HUE_ENTS, res.profiles) is None  # drafts never match


# ---- matcher --------------------------------------------------------------

def profiles():
    return load_profiles(PROFILES).profiles


def test_match_case_insensitive_exact_with_entity_pattern():
    p = matcher.match_device(HUE, HUE_ENTS, profiles())
    assert p and p.name == "hue_bulb"


def test_match_without_patterns():
    assert matcher.match_device(KASA, None, profiles()).name == "kasa_kp115"


def test_draft_skipped_even_if_it_is_the_only_candidate(tmp_path):
    write(tmp_path, "d.yaml", "status: draft\nmatch: {manufacturer: TP-Link, model: KP115}\n")
    assert matcher.match_device(KASA, [], load_profiles(tmp_path).profiles) is None


def test_non_match_other_model():
    other = dict(KASA, model="KP125")
    assert matcher.match_device(other, [], profiles()) is None
    assert matcher.match_device(dict(KASA, manufacturer="Other"), [], profiles()) is None


def test_exact_not_substring():
    assert matcher.match_device(dict(KASA, model="KP115 v2"), [], profiles()) is None
    assert matcher.match_device(dict(KASA, model=None), [], profiles()) is None
    assert matcher.match_device(None, None, profiles()) is None


def test_entity_patterns_all_must_match():
    ps = profiles()
    assert matcher.match_device(HUE, ["switch.other"], ps) is None          # no light.hue_*
    assert matcher.match_device(HUE, [], ps) is None                        # entities not registered yet
    assert matcher.match_device(HUE, None, ps) is None                      # entities unknown
    assert matcher.match_device(HUE, ["LIGHT.HUE_X"], ps).name == "hue_bulb"  # case-insensitive glob


def test_multiple_patterns_and(tmp_path):
    write(tmp_path, "p.yaml", "status: active\nmatch:\n  manufacturer: TP-Link\n  model: KP115\n"
          "  entity_patterns: ['switch.*', 'sensor.*_current_consumption']\n")
    ps = load_profiles(tmp_path).profiles
    assert matcher.match_device(KASA, ["switch.kitchen_plug"], ps) is None
    assert matcher.match_device(KASA, ["switch.kitchen_plug", "sensor.kitchen_plug_current_consumption"], ps)


def test_more_specific_profile_wins(tmp_path):
    write(tmp_path, "a_generic.yaml", "status: active\nmatch: {manufacturer: TP-Link, model: KP115}\n")
    write(tmp_path, "z_specific.yaml",
          "status: active\nmatch: {manufacturer: TP-Link, model: KP115, entity_patterns: ['switch.*']}\n")
    ps = load_profiles(tmp_path).profiles
    assert matcher.match_device(KASA, ["switch.x"], ps).name == "z_specific"
    assert matcher.match_device(KASA, [], ps).name == "a_generic"


def test_needs_entities():
    ps = profiles()
    assert matcher.needs_entities(HUE, ps) and not matcher.needs_entities(KASA, ps)


# ---- report uses the same matcher ----------------------------------------

def test_report_uses_matcher_and_reports_invalid_profiles(tmp_path):
    import shutil
    for f in ("capture.jsonl", "capture.jsonl.1"):
        shutil.copy(FIX / "report" / f, tmp_path / f)
    rep = report.summarize(str(tmp_path / "capture.jsonl"), str(PROFILES))
    by = {d["device_id"]: d for d in rep["devices"]}
    assert by[KASA["id"]]["profile_match"] == "kasa_kp115.yaml" and by[KASA["id"]]["profile_name"] == "kasa_kp115"
    # report fixture has no logged entities, so the Hue profile (needs light.hue_*) cannot match
    assert by[HUE["id"]]["matched"] is False
    assert {e["file"] for e in rep["profile_errors"]} == {"bad_yaml.yaml", "bad_schema.yaml"}
    assert "Invalid profiles" in report.render_markdown(rep)


def test_report_matches_entity_patterns_from_logged_entities(tmp_path):
    import json
    rec = {"timestamp": "2026-10-15T00:00:00+00:00", "device_id": HUE["id"], "device": HUE, "entities": HUE_ENTS}
    (tmp_path / "c.jsonl").write_text(json.dumps(rec) + "\n")
    rep = report.summarize(str(tmp_path / "c.jsonl"), str(PROFILES))
    assert rep["devices"][0]["profile_name"] == "hue_bulb"


def test_generated_stub_is_a_valid_draft_profile(tmp_path):
    import shutil
    for f in ("capture.jsonl", "capture.jsonl.1"):
        shutil.copy(FIX / "report" / f, tmp_path / f)
    stubs = tmp_path / "stubs"
    report.main(["--log-file", str(tmp_path / "capture.jsonl"), "--out-dir", str(tmp_path / "o"),
                 "--profiles-dir", str(tmp_path / "none"), "--emit-stubs", str(stubs)])
    res = load_profiles(stubs)
    assert not res.errors and len(res.profiles) == 2 and all(p.status == "draft" for p in res.profiles)
