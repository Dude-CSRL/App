# HA Device Automation Roadmap

**Status:** In development (started 2026-10-14)
**Owner:** Jeremy (Dude-CSRL)
**Repo:** Dude-CSRL/App

## Goal

Automatically detect new devices added to Home Assistant, match them against known device profiles, and provision them (name, area, entity IDs, dashboard cards) with zero manual setup for known devices. Unknown devices trigger a fallback notification so a profile can be written and replayed.

## Architecture

- **Grokbot** = profile author AND code author. Reads HA config and WebSocket docs, generates YAML profiles from captured payloads, writes the watcher script, opens PRs for review.
- **Local Python watcher** = executor. Runs on the HA PC, subscribes to the HA WebSocket event bus, matches devices, runs provision recipes.
- **GitHub repo** = source of truth for profiles, code, and this roadmap. Versioned and reviewable.
- **Human-in-the-loop** = reviewer. Approves PRs, eyeballs entity ID mappings before they go live, runs and tests the watcher.

## Development Workflow

1. Roadmap and specs drafted in conversation (Eve/Grok).
2. Grokbot writes production code and YAML profiles against the spec and the real HA instance.
3. Jeremy runs the code, reports failures, iterates with Grokbot.
4. Grokbot opens a PR for each meaningful change; Jeremy approves before merge to main.

## Agent Persistence

Grokbot agents are not stored objects. What persists is the context fed to them each session:

- This roadmap file is the primary brain.
- Add an `AGENTS.md` (or phase-specific brief) in the repo root as a standing instruction file. Paste or point Grokbot at it at the start of every session so it starts from the same decisions, conventions, and open questions.
- Update that file whenever a design decision changes, so future sessions inherit the correction instead of re-litigating it.
- Profiles and code in the repo are also part of agent memory — Grokbot reads them to learn existing conventions before writing anything new.

## Design Decisions

- Standalone Python script first (not an HA add-on). Simpler to debug, clean stack traces, no Supervisor dependency. Containerize later only if HA moves to the same box.
- Long-lived access token stored in an env file with restricted permissions, never hardcoded.
- Use scoped tokens where possible to limit blast radius if one leaks.
- Profiles are generated from real captured device payloads, never hand-written from memory.
- Area inference from context (AP location, VLAN/zone) where possible; fallback asks one question: which area?
- Dry-run mode prints what would happen without doing it.
- Profiles versioned in Git; roll back if a tweak breaks something.
- **Watcher logs in JSON Lines format** (one JSON object per line) with fixed fields: timestamp, event type, device ID, plus the full raw payload. Chosen so the monitoring agent and phase-two matcher can parse programmatically from day one without retrofitting.
- Mock HA server lives in the same repo under a `tests/` folder (not a throwaway scratch dir) so it stays findable but out of the main tree.
- **Media and smart-home gear co-located on the default trusted network** with the mini PC and HA host: Sonos, Naboo/Kasa voice boxes, Nvidia Shield, Samsung TVs. mDNS, SSDP, and Chromecast discovery break across subnet boundaries — proven by the hours spent on Sonos VLAN exceptions that only resolved when everything was collapsed back to one network. IoT and guest VLANs remain for cameras, plugs, and devices that don't need discovery. Optional middle ground: per-device outbound internet firewall rules in UniFi without inter-VLAN routing.
- **Phase 7 default:** same VLAN as the HA host for anything discovery-dependent; add exceptions only when a device truly cannot live there. Codify the old Sonos exception set as versioned UniFi API config so future changes are diff reviews, not trial-and-error afternoons.
- Watcher supervised by **systemd** (not screen) with `RestartSec=10-15` to survive power flashes and brownouts common at this location; auto-starts on boot.
- **Device-ID dedup** via a JSON state file of seen IDs, loaded at startup and flushed on each new log entry; **--force-device-id** CLI flag bypasses dedup for one ID to force re-log.
- **Token rotation reminder:** startup check records token creation date and prints a non-blocking warning after 90 days.
- **Area detection:** UniFi AP signal strength as the instant first guess (require ≥5 dBm gap between top two APs before auto-assign). HA Companion app on phone provides quick-reply confirmation (kitchen/living room/etc.) as the tiebreaker and trainer. Grokbot writes the automation YAML.
- **Spike HA native device triggers** before building a full custom matcher in phase 2. HA can handle manufacturer-model detection natively; the script only runs the provision recipe.

## Phases

### Phase 1: Foundation (Capture)

Pure capture. No matching logic.

1. Create a long-lived access token in HA (profile settings) and store it in an env file.
2. Write the WebSocket client: connect to HA, authenticate, subscribe to device registry events.
3. On each `device_registry` created event, log the full raw JSON payload verbatim — device ID, manufacturer, model, name, integration domain, identifiers, connections, suggested area. Do not pick fields early.
4. Add reconnect logic with exponential backoff so an HA restart doesn't kill the watcher.
5. Run for a few days alongside normal device additions. Confirm every event shows up in the log before writing any matching logic.

**Deliverable:** a clean, complete capture log. If the log is clean, phase 2 is just plumbing.

**Monitoring agent:** same watcher, extra tail. After the capture window, a second pass (script or Grokbot) reads the log, dedupes by device ID, and produces a summary report — every device seen, its identifiers, and whether it matches an existing profile. That report seeds phase-two profile writing.

**Phase 1 gaps to fill:**

1. **Mock HA server for testing.** Tiny Python server speaking just enough of the HA WebSocket protocol to push a device-created event on demand. Point the watcher at localhost, fire a fixture, verify the log. Avoids unplugging real hardware to test. Est. ~1 hour to write. Grokbot authors it. Lives in `tests/`.
2. **Log rotation.** Python's `RotatingFileHandler` with a max size and backup count. Raw payloads over five days can balloon.
3. **Auth-failure handling (separate from network blips).** If the auth message is rejected (revoked/expired token), the watcher prints a loud message and exits non-zero instead of silently retrying forever. A systemd service or simple watchdog notices the crash and pings you. Dead process that won't restart is already a signal for v1 — no fancy alerting infrastructure needed.
4. **Structured logging.** JSON Lines output with fixed fields (timestamp, event type, device ID) plus raw payload, so downstream tools parse it without retrofit.
5. **Device-ID dedup across reconnects** via JSON state file + `--force-device-id` flag.
6. **systemd supervision** with RestartSec 10-15s for power-flash resilience; start on boot.
7. **Token rotation reminder** at startup (warn after 90 days, non-blocking).
8. **Area detection automation:** UniFi AP signal (5 dBm gap) + HA Companion phone quick-reply. Grokbot writes the YAML.

**Phase 1 effort estimate:** 2-3 hours total with Grokbot writing and Jeremy reviewing/running.

### Phase 2: Matcher

1. Define the YAML profile schema: manufacturer, model, optional entity patterns, provision recipe.
2. Load profiles at watcher startup.
3. On each new device event, match manufacturer + model (+ entity patterns) against loaded profiles.
4. Spike HA native device triggers first to see if custom matching can be reduced to just the recipe side.

### Phase 3: Provision

For a matched device, run the recipe:

- Set the device name
- Assign the area
- Drop a card on the right dashboard
- Clean up entity IDs
- Fire any welcome automation defined in the profile

### Phase 4: Fallback + Re-run

- Unmatched devices trigger a notification with the device's raw identifiers.
- A re-run command lets you fix a profile and replay it against a device already in the registry.
- Grokbot generates the initial profile from the captured payload; you eyeball entity ID mappings before it goes live.

### Phase 5: Nice-to-haves (post-v1)

- Git-backed profiles (already partially covered by the PR workflow)
- Dry-run mode
- Small web UI to browse unmatched devices

**Note:** The UI is a rabbit hole — auth, sessions, responsive layout, dark mode, inline profile editing. CLI + notifications already cover the workflow. Stop at phase 4 for v1.

### Phase 6: GitHub Agent

Semi-autonomous repo maintenance, not fully autonomous:

- Watch the repo, flag stale branches, close abandoned PRs
- Enforce branch protection rules
- Rotate deploy keys on a schedule
- Open issues for anything it finds; batch-approve weekly

**Hard rule:** no merging to main, no deletes, without human approval.

### Phase 7: UniFi Firewall / VLAN Management

- Default: same VLAN as HA host for all discovery-dependent gear (Sonos, Naboo, Shield, Samsung TVs, mini PCs).
- IoT/guest VLANs reserved for cameras, plugs, and non-discovery devices.
- Codify the legacy Sonos VLAN exception set as versioned config via the UniFi API — future changes are diff reviews, not trial-and-error.
- Add inter-VLAN exceptions only when a device truly cannot live on the trusted network.
- Optional: per-device outbound internet firewall rules in UniFi (no inter-VLAN routing changes needed).
- Do NOT mix into phase 1 — its own workstream.

## Open Questions

- [ ] Exact HA WebSocket URL and port for the local instance
- [ ] Which dashboard(s) receive new device cards by default
- [ ] Naming convention for auto-provisioned devices
- [ ] Whether to use one scoped token per integration domain or one broad token
- [ ] Capture window length for phase 1 (suggest 3-5 days)

## Session Log

- 2026-10-14: Roadmap drafted in conversation. Phases 1-7 defined. Phase 1 fleshed out to five tasks plus eight gaps (mock HA server in tests/, log rotation, auth-failure handling, structured JSONL logging, device-ID dedup with --force-device-id, systemd supervision with RestartSec 10-15s, token rotation reminder at 90 days, UniFi+HA Companion area detection). Development workflow and agent-persistence section added. Grokbot-as-code-author workflow confirmed. Same-repo decision for mock server confirmed. Media-gear co-location on trusted VLAN decided. Spike of HA native device triggers added before custom matcher. File created in repo.
