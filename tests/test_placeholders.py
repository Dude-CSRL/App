import pytest

from ha_watcher import placeholders as ph
from ha_watcher.profiles import load_profiles, recipe_warnings


def test_slugify():
    assert ph.slugify("Living Room") == "living_room"
    assert ph.slugify("  Café  Bar!! ") == "cafe_bar"
    assert ph.slugify("a__b") == "a_b"


def test_render_known_and_slug_values():
    v = {"area": "living_room", "area_name": "Living Room", "1": "Foo Bar"}
    assert ph.render("light.{area}_{1}", v, ph.ID_ALLOWED, allow_digits=True, slug_values=True) == \
        "light.living_room_foo_bar"
    assert ph.render("{area_name} lamp", v, ph.NAME_ALLOWED) == "Living Room lamp"


def test_render_unknown_vs_unresolved():
    with pytest.raises(ph.PlaceholderError) as e:
        ph.render("{bogus}", {}, ph.NAME_ALLOWED)
    assert e.value.kind == "unknown"
    with pytest.raises(ph.PlaceholderError) as e:
        ph.render("{area}", {}, ph.NAME_ALLOWED)
    assert e.value.kind == "unresolved"


def test_non_identifier_braces_are_literal():
    s = "{{ states('sensor.x') }} and {a b} and {}"
    assert ph.render(s, {}, ph.CARD_ALLOWED) == s
    assert ph.tokens(s) == []


def test_entity_placeholder():
    assert ph.render("{entity:light}", {"entity:light": "light.x"}, ph.CARD_ALLOWED, allow_entity=True) == "light.x"
    with pytest.raises(ph.PlaceholderError):
        ph.render("{entity:light}", {"entity:light": "light.x"}, ph.NAME_ALLOWED)


def test_validate():
    assert ph.validate("{area}_{name}", ph.ID_ALLOWED) == []
    assert ph.validate("{aera}", ph.ID_ALLOWED) == ["unknown placeholder {aera}"]
    assert ph.validate("{2}", ph.ID_ALLOWED, max_capture=1)
    assert ph.validate("{1}", ph.ID_ALLOWED, max_capture=1) == []
    assert ph.validate("{1}", ph.ID_ALLOWED)  # no wildcard in from


def test_recipe_warnings_do_not_invalidate_profile(tmp_path):
    (tmp_path / "p.yaml").write_text(
        "status: active\nmatch: {manufacturer: A, model: B}\nrecipe:\n  name: '{aera} x'\n"
        "  entity_id_cleanup:\n    - {from: 'light.a_*', to: 'light.b_*_{2}'}\n"
        "  dashboard_card: {card: {entity: '{entity:light}', x: '{nope}'}}\n")
    res = load_profiles(tmp_path)
    assert [p.name for p in res.profiles] == ["p"] and not res.errors
    text = " | ".join(str(w) for w in res.warnings)
    assert "unknown placeholder {aera}" in text and "wildcards are not supported" in text
    assert "{2} refers to a wildcard" in text and "unknown placeholder {nope}" in text
    assert "{entity:light}" not in text


def test_fixture_profile_has_no_warnings():
    res = load_profiles("tests/fixtures/provision_profiles")
    assert not res.errors and not res.warnings


def test_phase2_fixture_with_old_star_syntax_only_warns():
    res = load_profiles("tests/fixtures/profiles")
    assert any("wildcards are not supported" in w.message for w in res.warnings)
    assert "hue_bulb" in [p.name for p in res.profiles]
