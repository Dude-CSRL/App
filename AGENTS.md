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
- `recipe` is executed by `ha_watcher/provision.py` (Phase 3). Keep `profiles/example_hue_bulb.yaml` as `draft`.
- Profiles must come from real captured payloads; tests use `tests/fixtures/profiles/`.

## Conventions (Phase 3)
- **Dry-run is the default everywhere.** Mutating commands are listed in `provision.MUTATING_TYPES` and only sent
  when `Options.apply` is true (`--apply` / `--provision apply`); the check lives in `Session.call` only. Any new
  mutating API call MUST be added to that set.
- Steps return ok / skipped / failed with a `reason`; never raise into the watcher. Steps compare current vs desired
  state first (idempotent). Placeholders are specified in `docs/profile-schema.md`; unknown ones warn at load and skip
  the step at run time, they never invalidate a profile (do not tighten profile validation: deployed profiles must keep loading).
- Provision results go to a separate rotating JSONL (`--provision-log`); capture lines are not modified.
- `ha_watcher.snapshot` writes `event_type: snapshot` lines to its own file (atomic replace); it must never touch the
  capture log or the dedup state. `ha_watcher.report --snapshot-file` reads it.
- `entity_patterns` match current entity ids, so patterns that only fit pre-cleanup names stop matching after a rename;
  prefer patterns that survive the cleanup, or replay with `--profile`.
- Not verified against a live HA: entity-registry/lovelace command shapes (`lovelace/info`, `lovelace/config/save`,
  `config/entity_registry/update` response). Treat the first `--apply` on real HA as a test; check dry-run output first.
- Mock server supports all commands used (`tests/mock_ha_server.py`); `--existing-only` registers fixtures without create events.

## Conventions (Phase 4)
- Unmatched newly created devices -> `persistent_notification.create` with id `ha_watcher_unmatched_<device_id>`
  (`ha_watcher/notify.py`). Notifications are the one `call_service` that does NOT go through `provision.Session`
  (non-destructive; sent regardless of `--provision` mode). `notify.safe_call` never raises; failures only log.
- `Config.notify_unmatched` defaults to `off`; the CLI flag defaults to `persistent`. Dismissal: `provision --apply`
  (not dry-run), forced re-log that matches, `notify_unmatched --dismiss-matched`.
- `ha_watcher.notify_unmatched` is dry-run by default (`--send` to act). `ha_watcher.draft_profile` never overwrites
  (`open(..., "x")`), writes `status: draft`, and shares `report.build_stub`/`stub_name` with `report --emit-stubs`.
  Drafts must pass `provision --check-profiles` with no warnings; keep hints commented and quoted (JSON-style).
- Not verified against live HA: `persistent_notification.create/dismiss` via the WebSocket `call_service` command
  (dismiss of a missing id), and Markdown rendering of the message.

## Open items
- Area-detection automation (Phase 1 gap 8) not yet written.
- See roadmap "Open Questions".
