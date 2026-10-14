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
| `--no-device-details` | off | skip `config/device_registry/list` lookup |

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
