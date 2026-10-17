# Device profile schema (Phase 2)

Profiles are YAML files in `profiles/` (`*.yaml` / `*.yml`). The watcher loads them at startup
(`--profiles-dir`, default `profiles`) and matches every captured device against them.
Per `docs/spikes/ha-native-device-triggers.md`, **matching is done in Python**, not in Home
Assistant. The `recipe` section is executed by `ha_watcher/provision.py` (Phase 3): **dry-run by default**,
changes only with `--apply` (see "Recipe execution" below).

Profiles should be generated from real captured payloads (see `python -m ha_watcher.report
--emit-stubs`), never written from memory.

## Fields

```yaml
name: hue_bulb                 # optional, [A-Za-z0-9_.-]; defaults to the file name without extension; unique
description: free text         # optional
status: active                 # REQUIRED: draft | active   (drafts are loaded but never matched)

match:                         # REQUIRED
  manufacturer: "Signify Netherlands B.V."   # required unless status: draft
  model: "Hue white and color ambiance A19"  # required unless status: draft
  entity_patterns:             # optional list of globs
    - "light.hue_*"

recipe:                        # optional; executed by ha_watcher.provision
  name: "Hue Bulb"             # device name to set            (string | null)
  area: "Living Room"          # area to assign                (string | null)
  entity_id_cleanup:           # list of rename rules          (list | null)
    - from: "light.hue_*"      #   glob over the device's entity IDs; * and ? capture -> {1}, {2}, ...
      to: "light.{area}_{name}"   #   template; see "Placeholders"
  dashboard_card:              # (mapping | null)
    dashboard: lovelace        #   string | null
    view: lights               #   string | null
    card: {type: light, entity: "{entity:light}"}   # Lovelace card mapping | null (placeholders allowed)
  welcome_automation: automation.welcome_hue   # (string | null)

source:                        # optional free-form provenance (written by report stubs)
  device_id: ...
```

Placeholders are finalized as described below. Unknown keys at the top level, in `match`, in `recipe`, in `dashboard_card`, or in a cleanup rule are
**errors** (they catch typos). All recipe fields may be `null`/omitted.

## Placeholders (final syntax)

A placeholder is `{token}` where token is a lowercase identifier, `entity:<domain>`, or a digit. Anything else in
braces is **literal**: Home Assistant Jinja such as `{{ states('sensor.x') }}` in a card is left untouched.
Single braces only; there is no escape syntax.

| Placeholder | Meaning | `recipe.name` | `entity_id_cleanup.to` | `dashboard_card.card` (every string value) |
|---|---|:-:|:-:|:-:|
| `{area_name}` | display name of the resolved area, e.g. `Living Room` | yes | yes | yes |
| `{area}` | slug of the area name, e.g. `living_room` | yes | yes | yes |
| `{manufacturer}`, `{model}` | device registry values, verbatim | yes | yes | yes |
| `{device_name}` | final device name (recipe name once set, else the device's current name) | no | yes | yes |
| `{name}` | slug of `{device_name}`, e.g. `hue_bulb` | no | yes | yes |
| `{domain}`, `{object_id}` | the entity's current domain and object id (`light`, `hue_color_lamp_1`) | no | yes | no |
| `{1}`, `{2}`, ... | text matched by the 1st, 2nd, ... `*` or `?` in the rule's `from` | no | yes | no |
| `{entity:<domain>}` | the device's first entity (sorted) in that domain, **after** planned renames, e.g. `{entity:light}` | no | no | yes |
| `{device_id}` | device registry id | no | no | yes |

- Slug = lowercase ASCII, runs of other characters become `_`, no leading/trailing `_`.
- In `to`, every substituted value is slugified, so the result is a valid object id. Wildcards (`*`, `?`) are
  **not** supported in `to`; use `{1}` etc. (`from` supports only `*` and `?`; `[]` is literal there.)
- `{area}`/`{area_name}` need `recipe.area` to resolve to an existing area. Otherwise the step that uses them is
  skipped with reason `area_unresolved`.
- Unknown or out-of-range placeholders do **not** invalidate the profile (it still loads and matches). They are logged
  as `PROFILE WARNING` at load and the affected step is skipped with `invalid_placeholder` / `unknown_placeholder`.
  Check profiles with `python -m ha_watcher.provision --check-profiles`.

## Recipe execution (Phase 3)

`python -m ha_watcher.provision --device-id <id> [--apply]` and `ha_watcher --provision {off,dry-run,apply}`.
Steps always report `ok`, `skipped` or `failed`; a failure never stops later steps or the watcher.

| Step | API call | Behaviour |
|---|---|---|
| `device_area` (runs first) | `config/area_registry/list`, `config/device_registry/update {area_id}` | area name or alias matched case-insensitively. Null/unknown area: `skipped`, `area_unresolved`; areas are never created |
| `device_name` | `config/device_registry/update {name_by_user}` | skipped if the effective name already equals the target |
| `entity_id_cleanup` | `config/entity_registry/update {new_entity_id}` | first matching rule per entity; skipped if the id is invalid (`^(?!_)` / no `__` / lowercase / same domain), already correct, already taken in the registry or state machine (`collision`), planned twice in one run, or if the rule would match its own output (`rule_not_idempotent`) |
| `dashboard_card` | `lovelace/info` or `lovelace/dashboards/list`, `lovelace/config`, `lovelace/config/save` | only storage-mode dashboards that already have a saved config and a masonry/sidebar view; the previous config is written to `--backup-dir` before saving; skipped (with reason) otherwise or if the card is already present |
| `welcome_automation` | `call_service automation.trigger` | only when this run changed something (or `--force-welcome`), only for `automation.<id>` that exists |

**Caveats**
- Renaming an entity id does not update dashboards, automations or scripts that reference the old id.
- `match.entity_patterns` are evaluated against the *current* entity ids. After `entity_id_cleanup` renames
  entities, a pattern that only fit the old names (`light.hue_*`) stops matching, so automatic matching of that device
  (CLI re-run, report) fails. Use patterns that also fit the new ids (e.g. `light.*`), or replay with `--profile <name>`.
- A recipe with no changes to make is a no-op, so replays are safe; the dashboard is only written when the card is new.

## Matching rules (`ha_watcher/matcher.py`)

1. Only `status: active` profiles are considered; drafts are skipped.
2. `match.manufacturer` and `match.model` must each equal the device's value: case-insensitive,
   exact (leading/trailing whitespace ignored, no substring or regex).
3. If `match.entity_patterns` is present, **every** pattern (shell-style glob, case-insensitive,
   `fnmatch`) must match at least one entity ID belonging to the device. A device whose entities are
   not yet registered, or unknown, does not match a profile with patterns.
4. Several profiles can match: the one with more entity patterns wins, then the first by file name.

Entity IDs are read from HA (`config/entity_registry/list`, filtered by device) only when a manufacturer/model
candidate has `entity_patterns`. Because entities register shortly after the device, the watcher then waits
`--match-settle-delay` seconds (default 10) and re-reads the device and entities before logging.

## Result in the capture log

Each captured JSONL line gets two additive fields (existing fields are untouched):

```json
"entities": ["light.hue_color_lamp_1", "..."],     // null unless entity patterns were needed
"match": {"matched": true, "profile": "hue_bulb", "profile_file": "hue_bulb.yaml"}
"match": {"matched": false, "profile": null, "profile_file": null, "reason": "no_matching_profile"}  // or "no_device_details"
```

## Invalid files

A file that is not valid YAML, fails validation, is empty, or reuses another profile's `name` is
skipped and logged at ERROR level at startup (`INVALID PROFILE (skipped) <file>: <problems>`).
The watcher keeps running with the remaining profiles. `ha_watcher.report` lists them too.
