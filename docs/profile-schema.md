# Device profile schema (Phase 2)

Profiles are YAML files in `profiles/` (`*.yaml` / `*.yml`). The watcher loads them at startup
(`--profiles-dir`, default `profiles`) and matches every captured device against them.
Per `docs/spikes/ha-native-device-triggers.md`, **matching is done in Python**, not in Home
Assistant. The `recipe` section is **parsed and validated only**; running it is Phase 3.

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

recipe:                        # optional; validated only in Phase 2
  name: "Hue Bulb"             # device name to set            (string | null)
  area: "Living Room"          # area to assign                (string | null)
  entity_id_cleanup:           # list of rename rules          (list | null)
    - from: "light.hue_*"      #   glob over entity IDs
      to: "light.living_room_*"   # target pattern (placeholder syntax is finalized in Phase 3)
  dashboard_card:              # (mapping | null)
    dashboard: lovelace        #   string | null
    view: lights               #   string | null
    card: {type: light, entity: light.x}   # Lovelace card mapping | null
  welcome_automation: automation.welcome_hue   # (string | null)

source:                        # optional free-form provenance (written by report stubs)
  device_id: ...
```

Unknown keys at the top level, in `match`, in `recipe`, in `dashboard_card`, or in a cleanup rule are
**errors** (they catch typos). All recipe fields may be `null`/omitted.

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
