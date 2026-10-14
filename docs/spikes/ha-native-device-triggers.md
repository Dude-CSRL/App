# Spike: Home Assistant native device triggers for new-device detection

Roadmap: Phase 2, item 4 ("Spike HA native device triggers first to see if custom
matching can be reduced to just the recipe side"). Date: 2026-10-14.

## Question
Can Home Assistant automations detect a newly added device and decide which profile it
belongs to, so that our Python code only has to run the provision recipe?

## Findings

### 1. Detection: yes, natively
- HA fires `device_registry_updated` on the event bus when a device is added, updated or
  removed. `data.action` is `create` / `update` / `remove` and `data.device_id` is the
  device ID. For `create` there is **no device payload** (only `remove` includes `device`,
  and only `update` includes `changes`). [Events]
- The **Manual event received** trigger (`trigger: event`) listens to any bus event and can
  filter on `event_data`, so `event_type: device_registry_updated` +
  `event_data: {action: create}` fires once per new device. [Event trigger]
- This is the same signal the Phase 1 watcher captures, so the capture log is a faithful
  preview of what an HA automation would see.

### 2. Matching on manufacturer/model: yes, via templates
- `device_attr(device_id, attr)` reads any device-registry attribute (manufacturer, model,
  sw_version, hw_version, serial_number, identifiers, ...); returns `None` when missing. [device_attr]
- `is_device_attr(device_id, attr, value)` is the boolean form. [is_device_attr]
- `device_entities(device_id)` lists the device's entity IDs (empty list if none yet). [device_entities]
- So a `template` condition, or a `choose:` block with one branch per profile, does the
  manufacturer + model (+ entity-pattern) matching in HA.

### 3. Blueprints: good fit for "one automation per profile"
- Blueprints take inputs (text, number, action selectors) and `!input` substitutions,
  and support `homeassistant.min_version`. [Blueprint schema] A blueprint with inputs
  manufacturer, model, settle delay and a recipe (action selector) lets each profile be one
  blueprint instance. See `ha_config/blueprints/automation/ha_device_automation/new_device_by_model.yaml`.
- Trigger-level templates are *limited templates* (no state/registry access) and
  `trigger_variables` are evaluated once at load, so matching must live in conditions or
  actions, not in the trigger. [Triggers]

### 4. Limits
1. **Provisioning is the gap.** There is no built-in automation action to rename a device,
   assign its area, or rename entity IDs. Those are registry edits done through the
   WebSocket API (`config/device_registry/update`, `config/entity_registry/update`) or the UI.
   Dashboard edits (adding a card) are also outside automations. This is exactly the
   Phase 3 recipe, so the external script is still needed for it. *(Inferred: no such
   action appears in the trigger, events or template docs fetched; confirm against the
   actions reference before Phase 3.)*
2. **Timing.** On `create`, the integration may not have registered entities yet and some
   attributes may be filled in later (via `update`). The example waits 10s before
   evaluating; robust handling should also watch `update` events or retry.
3. **Exact strings.** `device_attr` returns raw registry strings ("Signify Netherlands B.V.",
   "Hue white and color ambiance A19"). Profiles must use the exact captured values, which
   is the roadmap's "generate from real captured payloads" rule anyway.
4. **Restart gaps.** Automations only see events while HA is running; devices added during
   startup before automations load can be missed (the docs note the automation engine
   starts last, e.g. for `component_loaded`). [Events] The watcher has the same gap; a
   periodic `config/device_registry/list` diff would close it for both.
5. **Profiles live in YAML automations** if done natively: harder to unit-test and version
   as data than the repo's `profiles/*.yaml`, and every new profile needs an automation
   reload.
6. Not exercised against a live HA instance in this spike (see Validation).

## Recommendation
**Hybrid: HA does detection (and optionally coarse matching), the Python script does the
recipe.** Keep the watcher's subscription to `device_registry_updated`/`create` as the
single detection path, and keep matching in Python against repo `profiles/*.yaml`, because
the matcher is just manufacturer + model + entity-pattern equality (a few dozen lines) and
the provision steps (rename, area, entity IDs, dashboard) need the WebSocket API anyway.
Use native HA automations/blueprints only for the fallback notification and for
per-device "welcome" automations. Phase 2 custom matching can therefore stay **minimal**:
no rules engine, just exact field matching plus a settle/retry for late entities.

Alternative if we want to minimize custom code: the example automation does the matching in HA and fires
`ha_device_automation_provision` with `{device_id, profile}`; the watcher subscribes to
that event and only runs the recipe. This works, but it splits profiles between HA YAML
and the repo, so it is not the default recommendation.

## Artifacts
- `ha_config/automations/new_device_detected.yaml`: event trigger on create, 10s settle,
  `choose:` per profile on `device_attr` manufacturer/model, hand-off event for matched
  devices, persistent notification for unknown ones.
- `ha_config/blueprints/automation/ha_device_automation/new_device_by_model.yaml`: per-profile blueprint.

## Validation
- `tests/test_ha_yaml.py` parses both files (with a `!input` constructor) and checks shape:
  triggers/actions present, the trigger filters `action: create`, and every blueprint input is used.
- Not validated by a running Home Assistant (no HA instance on the build box). Before
  enabling: paste into HA, run **Developer tools > YAML > Check configuration**, and test
  the template lines in **Developer tools > Template** with a real device ID.

## Sources (fetched 2026-10-14)
- [Events] https://www.home-assistant.io/docs/configuration/events/
- [Triggers] https://www.home-assistant.io/docs/automation/trigger/
- [Event trigger] https://www.home-assistant.io/triggers/event/
- [device_attr] https://www.home-assistant.io/template-functions/device_attr/
- [is_device_attr] https://www.home-assistant.io/template-functions/is_device_attr/
- [device_entities] https://www.home-assistant.io/template-functions/device_entities/
- [Blueprint schema] https://www.home-assistant.io/docs/blueprint/schema/
- [Scripts: run in sequence] https://www.home-assistant.io/docs/scripts/
- [Templating] https://www.home-assistant.io/docs/configuration/templating/
