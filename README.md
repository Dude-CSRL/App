# App — HA Device Automation

See [HA_Device_Automation_Roadmap.md](HA_Device_Automation_Roadmap.md) (source of truth) and [AGENTS.md](AGENTS.md).

## Phase 1: capture watcher

`ha_watcher` connects to the Home Assistant WebSocket API, authenticates with a long-lived
token, subscribes to `device_registry_updated`, and for every `create` action appends one JSON
line to the capture log (`timestamp`, `event_type`, `device_id`, `raw_payload`, plus the full
`device` registry entry). No matching logic.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

### Options
| Flag / env | Default | Meaning |
|---|---|---|
| `--url` / `HA_URL` | — | e.g. `ws://homeassistant.local:8123/api/websocket` |
| `HA_TOKEN` (env only) | — | long-lived access token |
| `HA_TOKEN_CREATED` | — | `YYYY-MM-DD`; warn if older than 90 days |
| `--log-file` | `logs/device_capture.jsonl` | rotating JSONL capture log |
| `--max-bytes` / `--backup-count` | 10 MiB / 10 | `RotatingFileHandler` settings |
| `--state-file` | `state/seen_devices.json` | dedup state |
| `--force-device-id ID` | — | re-log ID even if seen (repeatable) |
| `--backoff-initial` / `--backoff-max` | 1 / 60 s | reconnect backoff (full jitter) |
| `--profiles-dir` / `HA_WATCHER_PROFILES` | `profiles` | profile YAMLs used for matching (Phase 2) |
| `--match-settle-delay` | 10 s | wait for entities to register before matching `entity_patterns` |
| `--provision {off,dry-run,apply}` / `HA_WATCHER_PROVISION` | `off` | run the matched profile's recipe for newly created devices (Phase 3) |
| `--provision-log` | `logs/device_provision.jsonl` | rotating JSONL of provision plans/results |
| `--backup-dir` | `logs/backups` | dashboard config backups taken before any dashboard save |
| `--no-device-details` | off | skip device-registry lookup (everything logs as unmatched) |

Exit codes: `0` normal, `2` auth rejected (`auth_invalid`, not retried), `3` missing config.

### Running the mock server and watcher together

Terminal 1 — mock HA (emits every `tests/fixtures/*.json` once a client subscribes):
```bash
python -m tests.mock_ha_server --port 8765 --token test-token --delay 1
```
Terminal 2 — watcher pointed at it:
```bash
HA_URL=ws://127.0.0.1:8765/api/websocket HA_TOKEN=test-token \
  python -m ha_watcher --log-file logs/mock_capture.jsonl --state-file state/mock_seen.json -v
tail -f logs/mock_capture.jsonl
```
- Re-run the watcher: nothing new is logged (dedup). Add `--force-device-id a1b2c3d4e5f60718293a4b5c6d7e8f90` to re-log the Hue fixture.
- Restart the mock with `--repeat` and Ctrl-C/restart it while the watcher runs to watch reconnect/backoff.
- Auth failure: start the mock with `--bad-token`; the watcher prints a FATAL banner and exits `2`.

Fixtures are device-registry entries (JSON); drop a new `*.json` into `tests/fixtures/` to emit it.

### systemd
See `deploy/systemd/ha-watcher.service` (`Restart=on-failure`, `RestartSec=12`,
`EnvironmentFile=/etc/ha-watcher/ha-watcher.env`) and `deploy/ha-watcher.env.example`.

### Monitoring report (capture summary)

After a capture window, summarize the log (live file + rotated backups `.1`, `.2`, ...):
```bash
python -m ha_watcher.report --log-file logs/device_capture.jsonl --out-dir reports \
  --profiles-dir profiles --emit-stubs reports/profile_stubs
```
- Writes `reports/device_report.md` and `reports/device_report.json`: one row per device ID with
  manufacturer, model, name, integration domain (first `identifiers` entry), identifiers,
  connections, suggested area, first/last seen, times seen, and matching profile.
- Profile matching is a placeholder until Phase 2: a `profiles/*.yaml` file matches on
  case-insensitive `manufacturer` + `model`; files with `status: draft` are ignored. No profiles dir means all devices are unmatched.
- `--emit-stubs DIR` writes one `status: draft` YAML stub per unmatched manufacturer+model (existing files are never overwritten).
- Malformed/truncated lines are skipped and listed in the report. Exit 1 if no log exists.

## Phase 2: profile matcher

Profiles live in `profiles/*.yaml` (schema: [docs/profile-schema.md](docs/profile-schema.md)). At startup the
watcher loads and validates them, logs any invalid file at ERROR level (and keeps running), and every captured
device line gets a `match` field: `{"matched": true, "profile": "<name>"}` or `{"matched": false, ...}`.
Matching is case-insensitive exact manufacturer + model, then optional glob `entity_patterns`; `status: draft`
profiles are never matched. The `recipe` section is executed by the Phase 3 provision runner below.
`profiles/example_hue_bulb.yaml` is a draft example built from mock data.

Try it against the mock server (the fixture profiles in `tests/fixtures/profiles/` match both mock devices and
include two deliberately invalid files so you can see the startup errors):
```bash
python -m tests.mock_ha_server --port 8765 --token test-token --delay 1      # terminal 1
HA_URL=ws://127.0.0.1:8765/api/websocket HA_TOKEN=test-token \
  python -m ha_watcher --profiles-dir tests/fixtures/profiles --match-settle-delay 1 \
  --log-file logs/mock_capture.jsonl --state-file state/mock_seen.json -v    # terminal 2
tail -f logs/mock_capture.jsonl | python -c "import sys,json; [print(json.loads(l)['device']['name'], json.loads(l)['match']) for l in sys.stdin]"
```
The monitoring report (`python -m ha_watcher.report`) uses the same matcher; `--emit-stubs` now writes draft
stubs in the profile schema (`name`, `status: draft`, `match`, `recipe`, `source`).

## Phase 3: snapshot and provisioning

**Safety:** provisioning is **dry-run by default**. A mutating Home Assistant call (device/entity registry update,
dashboard save, `automation.trigger`) is only sent with `--apply` (CLI) or `--provision apply` (watcher). Dry-run still
*reads* HA (registries, states, dashboards) so the plan reflects reality, then prints/logs the exact calls it would make.
Each step reports `ok` / `skipped` / `failed`; failures never crash the watcher; re-running is a no-op once things match.
Placeholder syntax, per-step rules and caveats: [docs/profile-schema.md](docs/profile-schema.md).

### Snapshot of existing devices
`ha_watcher.snapshot` dumps every device (plus entity-registry entries) currently in HA as capture-shaped JSONL
(`event_type: snapshot`) to a **separate** file, replaced atomically per run. It never touches the live capture log or
the dedup state, so it is safe next to the running service. This covers devices that were never "created" while the
watcher ran (Sonos etc.); the report accepts it:
```bash
python -m ha_watcher.snapshot --out logs/device_snapshot.jsonl --profiles-dir profiles
python -m ha_watcher.report --log-file logs/device_capture.jsonl --snapshot-file logs/device_snapshot.jsonl \
  --out-dir reports --profiles-dir profiles --emit-stubs reports/profile_stubs
```

### Provision one existing device (dry-run, then apply)
```bash
python -m ha_watcher.provision --check-profiles --profiles-dir profiles     # validate; prints warnings, no HA needed
python -m ha_watcher.provision --device-id <registry id> --profiles-dir profiles            # DRY-RUN (default)
python -m ha_watcher.provision --device-id <registry id> --profiles-dir profiles --apply    # executes the plan
```
Exit codes: `0` ok (skips are fine), `1` a step failed, `2` auth_invalid, `3` config error, `4` device not found / no
matching active profile. Useful flags: `--profile NAME` (skip matching), `--force-welcome`, `--json`, `--backup-dir`.
Each run appends its result to `--provision-log`.

### Provision newly created devices from the watcher
`--provision dry-run` logs a plan to the provision log after each matched create event (nothing changes);
`--provision apply` executes it. The watcher waits `--match-settle-delay` for entities to register first.
Start with `dry-run`, read `device_provision.jsonl`, and only then switch to `apply`.

### Running on the mini PC
Paths below assume the app lives in `/home/dude/ha-watcher-app` (adjust). Use the same `--log-file`, `--state-file`
and `--profiles-dir` your service uses. Load the service's env file into your shell without printing it:
```bash
cd /home/dude/ha-watcher-app
set -a; . /path/to/ha-watcher.env; set +a        # exports HA_URL and HA_TOKEN
.venv/bin/python -m ha_watcher.snapshot --out logs/device_snapshot.jsonl --profiles-dir profiles
.venv/bin/python -m ha_watcher.provision --check-profiles --profiles-dir profiles
DEVICE_ID=$(.venv/bin/python - <<'PY'
import json
for l in open("logs/device_capture.jsonl"):
    r = json.loads(l)
    if r.get("match", {}).get("matched"): print(r["device_id"], r["device"]["name"], r["match"]["profile"])
PY
)
echo "$DEVICE_ID"        # pick an id, then:
.venv/bin/python -m ha_watcher.provision --device-id <id> --profiles-dir profiles        # DRY-RUN
```
To let the service plan for new devices, add `--provision dry-run --provision-log ... --backup-dir ...` to `ExecStart`
(see `deploy/systemd/ha-watcher.service`) and `sudo systemctl daemon-reload && sudo systemctl restart ha-watcher`.

Try it all against the mock (devices pre-registered, no create events):
```bash
python -m tests.mock_ha_server --port 8765 --token test-token --existing-only          # terminal 1
export HA_URL=ws://127.0.0.1:8765/api/websocket HA_TOKEN=test-token                    # terminal 2
python -m ha_watcher.snapshot --out logs/mock_snapshot.jsonl --profiles-dir tests/fixtures/provision_profiles
python -m ha_watcher.provision --device-id a1b2c3d4e5f60718293a4b5c6d7e8f90 --profiles-dir tests/fixtures/provision_profiles
```
