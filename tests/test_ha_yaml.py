"""Syntax/shape check for HA YAML in ha_config/ (does not run Home Assistant)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "ha_config"


class HALoader(yaml.SafeLoader):
    pass


HALoader.add_constructor("!input", lambda l, n: {"__input__": l.construct_scalar(n)})


def load(p):
    return yaml.load(p.read_text(encoding="utf-8"), Loader=HALoader)


def test_automation_yaml():
    autos = load(ROOT / "automations" / "new_device_detected.yaml")
    assert isinstance(autos, list) and autos
    for a in autos:
        assert {"id", "alias", "triggers", "actions"} <= a.keys()
        t = a["triggers"][0]
        assert t == {"trigger": "event", "event_type": "device_registry_updated",
                     "event_data": {"action": "create"}}


def test_blueprint_yaml():
    bp = load(ROOT / "blueprints" / "automation" / "ha_device_automation" / "new_device_by_model.yaml")
    assert bp["blueprint"]["domain"] == "automation"
    used = set()

    def walk(x):
        if isinstance(x, dict):
            if "__input__" in x:
                used.add(x["__input__"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk({k: v for k, v in bp.items() if k != "blueprint"})
    assert used == set(bp["blueprint"]["input"])
