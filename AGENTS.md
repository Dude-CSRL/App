# AGENTS.md — standing brief for Grokbot sessions

Read `HA_Device_Automation_Roadmap.md` first; it is the source of truth.

## Conventions (Phase 1)
- Package: `ha_watcher/` (run with `python -m ha_watcher`). Mock HA server + fixtures: `tests/`.
- Capture log: JSON Lines, one object per line, fixed fields `timestamp`, `event_type`,
  `device_id`, plus `raw_payload` (the HA event message verbatim) and `device`
  (full registry entry from `config/device_registry/list`, verbatim). Also `action`, `forced`.
- Only `device_registry_updated` events with `data.action == "create"` are logged.
- Dedup state: JSON file `{"seen_device_ids": [...]}`, atomic write (tmp + fsync + os.replace).
- `auth_invalid` => exit code 2, loud stderr, never retried in-process. Config error => 3.
- Reconnect: exponential backoff, full jitter, capped (`--backoff-max`, default 60s); resets after a successful auth.
- Secrets live only in an env file (`deploy/ha-watcher.env.example` is the template). Never commit tokens.
- No matching logic in Phase 1 (done); Phase 2 matching below.

## Conventions (Phase 2)
- Matching is minimal Python (per `docs/spikes/ha-native-device-triggers.md`); HA does not match.
- Profiles: `profiles/*.yaml`, schema in `docs/profile-schema.md`; loader `ha_watcher/profiles.py`, matcher
  `ha_watcher/matcher.py`. Only `status: active` profiles match; case-insensitive exact manufacturer+model, then
  glob `entity_patterns` (all must match). Invalid profile files are logged and skipped, never fatal.
- Capture lines carry additive `entities` and `match` fields; never alter `raw_payload`/`device`.
- `recipe` is parsed/validated only. Executing it is Phase 3. Keep `profiles/example_hue_bulb.yaml` as `draft`.
- Profiles must come from real captured payloads; tests use `tests/fixtures/profiles/`.

## Open items
- Area-detection automation (Phase 1 gap 8) not yet written.
- See roadmap "Open Questions".
