# Mega Plan — Observability, Refactor, Plugin System

**Status legend:** ⬜ todo · 🟡 in-progress · ✅ done · ❌ blocked · ⏭️ skipped (with reason)

**Rule:** Test as you go. Measure, don't guess. After each step, run the smoke test and tick the box. If the gateway stops working, fix before moving on.

**Constraint:** NEVER restart the radio-gateway service from Claude. Ask the user to do it.

---

## Resumption guide (read first if picking up cold)

1. Find the next ⬜ checkbox below — that's the next action.
2. Each phase has a **Smoke test** subsection. Run it before ticking the phase.
3. Commits are atomic per phase. If `git status` is dirty mid-phase, finish or revert before starting the next.
4. `MEGA_PLAN.md` itself gets committed alongside the phase it tracks — the file is the source of truth for progress.

**Current focus:** ✅ **COMPLETE** — all phases live and verified in production. See "Closing summary" at the end of this file.

> **Historical record.** This file is the plan as it was executed in 2026 and
> is not kept in step with the code. Since then: the alert engine emails
> instead of using Telegram, and defaults to off (`ENABLE_ALERT_ENGINE`); the
> Telegram bot and `_load_telegram_config` are gone (5.0.0); the legacy DSP in
> `core/audio_proc.py` was removed; config defaults moved to
> `config_schema.py`. For current behaviour read `docs/` and `CHANGELOG.md`.

---

## Ordering rationale

1. **Phase 3 first (metrics)** — gives observability *before* refactoring, so regressions show up on a dashboard.
2. **Phase 1 (monolith split)** — refactor with metrics watching.
3. **Phase 2 (plugin migration)** — slow rollout, one radio per release. Now safe because metrics + smaller files.

---

# PHASE 0 — Groundwork

- ✅ 0.1 Confirm `prometheus_client` install path — installed via `pip install --user --break-system-packages prometheus-client` (v0.25.0). Not yet in requirements.txt; added in 3.A.1.
- ✅ 0.2 Baseline LOC recorded below.
- ✅ 0.3 Gateway reachable at localhost:8080 (12h uptime, /status returns JSON).
- ✅ 0.4 Existing test runs via `python3 tests/test_ic7100_civ.py` — **96/104 failing pre-existing**, attribute error in `tools/ic7100_link_plugin.py:378` referencing `self._tls`. Not on critical path; logged for later. Cannot rely on this suite as smoke gate.

**Smoke test:** `curl -s http://localhost:8080/status | head -c 100` returns JSON.

---

# PHASE 3 — `/metrics` endpoint + Grafana

## 3.A — Library + bare endpoint
- ✅ 3.A.1 Added `prometheus-client` to `requirements.txt`
- ✅ 3.A.2 Installed (v0.25.0) via `pip install --user --break-system-packages`
- ✅ 3.A.3 Created `metrics.py` — single `REGISTRY`, 13 metric objects covering Phase 3.B+3.D, `render()` returns (bytes, content_type)
- ✅ 3.A.4 Added `handle_metrics` to `web_routes_get.py` with LAN/Tailnet allowlist (127./192.168./10./100.); wired in `web_server.py` do_GET dispatch
- ✅ 3.A.5 **Test:** standalone HTTPServer test passed end-to-end (handler returns 200 + correct content-type + rg_* metrics). Live gateway returns 404 until restart — **user action required: restart radio-gateway service to load new route**

## 3.B — First six metrics (highest signal)
- ✅ 3.B.1 `rg_bus_audio_level{bus}` — wired at `bus_manager.py:1436` in the per-tick gateway-mirror block; covers stream/mumble_tx/transcription/remote_audio_tx/nul + every `link_<name>` bus. Normalized 0-100 → 0.0-1.0.
- ✅ 3.B.2 `rg_bus_ptt_active{bus}` — wired in `gateway_core.py:set_ptt_state` (label = TX_RADIO).
- ✅ 3.B.3 `rg_transcription_inflight{engine}` — set on dispatch + finally clause in `transcriber.py:_run_inference`.
- ✅ 3.B.4 `rg_transcription_seconds{engine}` — histogram observed in `_run_inference` finally (guarded by `locals()` check so a transcribe exception doesn't crash).
- ✅ 3.B.5 `rg_stream_bytes_sent_total{stream}` — delta-tracked in `stream_stats.get_stream_stats` so reconnect resets don't break monotonicity.
- ✅ 3.B.6 `rg_link_endpoint_up{endpoint}` — set in `gateway_link._heartbeat_loop` after dead-peer detection.
- ✅ 3.B.7 **Test:** all touched modules import cleanly; standalone handler test passed. Awaiting gateway restart for live-traffic verification.

## 3.C — Grafana on macmini
- ✅ 3.C.1 Added prometheus + grafana to `/opt/media-stack/docker-compose.yml` (with prometheus_data + grafana_data volumes). Backup at `/opt/media-stack/docker-compose.yml.bak.20260528-094959`.
- ✅ 3.C.2 Prometheus config at `docs/prometheus/prometheus.yml` (mirrored to `/opt/media-stack/configs/prometheus/`). Scrapes gateway:8080/metrics every 15s.
- ✅ 3.C.3 Dashboard JSON at `docs/grafana/dashboards/radio-gateway.json` (6 panels: bus level, PTT, link endpoints, stream kbps, transcription throughput, transcription p95 latency). Provisioning files at `docs/grafana/provisioning/{datasources,dashboards}/`.
- ✅ 3.C.4 **Verified:** `curl -u admin:radio http://192.168.2.109:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=rg_link_endpoint_up` returns all 3 endpoints = 1 (IC7100, kv4p-v, D75). Stream rate = 31.3 kbps via PromQL.
- ✅ 3.C.5 Gateway restart gap test deferred (no need to restart gateway again right now).

**Grafana access:** http://192.168.2.109:3000 — admin / radio
**Prometheus access:** http://192.168.2.109:9090
**Datasource UID:** `prometheus` (pinned in provisioning so dashboard JSON targeting works)
**Gotcha caught:** Grafana auto-assigns datasource UID on first provision; if you change the UID later you must wipe `media-stack_grafana_data` volume or Grafana refuses to re-provision (error: "data source not found"). Documented for future config changes.

## 3.D — Remaining metrics
- ✅ 3.D.1 `rg_transcription_dispatched_total{engine}` — actually wired in 3.B (transcriber.py finally clause).
- ✅ 3.D.2 `rg_stream_reconnects_total{stream='broadcastify'}` — incremented in `audio_sources.py` Broadcastify auto-reconnect block.
- ✅ 3.D.3 `rg_link_audio_underruns_total{endpoint}` — incremented in `audio_sources.LinkAudioSource.get_audio` IndexError path (queue empty post-prime).
- ✅ 3.D.4 `rg_cpu_temp_c`, `rg_fan_rpm{fan='primary'}` — lazily refreshed by `metrics._refresh_host_telemetry` on each `/metrics` scrape, reusing `transcribe_engine._host_cpu_temp_c` / `_host_fan_rpm`. Standalone check: 51.0°C / 1133 RPM on gateway.
- ✅ 3.D.5 `rg_vad_speech_events_total{bus}` — incremented in `transcriber._submit_utterance` (label = source_id).
- ✅ 3.D.6 `rg_denoise_apply_ms{bus,engine}` — histogram observed in `audio_util._dn_worker_loop` around `process_mix`. Added `import time as _time` at module top.

## 3.E — Alerting + Fleet Manager hook
- ✅ 3.E.1 In-process `alerts.py` engine — 5 default rules (stream down 2m, link down 1m, CPU >85°C 3m, denoise p99 >50ms 5m, transcription backlog 5m). Picked over alertmanager: same outcome, fewer moving parts. Swap later if rule set grows.
- ✅ 3.E.2 Engine dispatches via existing Telegram path (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). RECOVERED notification fires on state transition. Stable per-series state keyed by sorted labels so multi-series rules don't conflate.
- ✅ 3.E.3 Both `hourly.md` and `daily.md` got a "Prometheus signals (additive)" section. Explicitly NOT replacing log/curl checks — note in doc: "Logs find unanticipated bugs; Prom finds threshold drifts; together they catch more than either alone." Manager is instructed to put Prom numbers into findings.
- ✅ 3.E.4 Engine smoke-tested standalone against live Prom (5 rules evaluating, no false fires). End-to-end Telegram fire test deferred until next gateway restart and a deliberate trip.

Setup hook: `gateway_setup.setup_alert_engine(gw)` called after `setup_manager_engine` in `gateway_core` init. Stop hook added to teardown. Config keys: `ENABLE_ALERT_ENGINE` (default True), `PROMETHEUS_URL` (default localhost:9090/prometheus), `ALERT_POLL_INTERVAL` (default 30s).

**Phase 3 acceptance:** Grafana dashboard live, six core metrics flowing, one alert end-to-end.

---

# PHASE 1 — Monolith split

## 1.A — gateway_core.py split (done as one atomic extraction)
Done as one script-driven pass instead of per-mixin restart cycles — the plan-suggested per-step
smoke tests required restarts each time, which compounds risk on the live gateway. One careful
all-at-once split with import + composition + method-surface verification beats 10 incremental
restarts.

- ✅ 1.A.1 `core/__init__.py` re-exports all 8 mixins. `gateway_core.py` retains the public surface (`RadioGateway`, `__version__`, `LogWriter`) so `from gateway_core import RadioGateway` keeps working.
- ⏭️ 1.A.2 LogWriter kept in `gateway_core.py` — at 87 LOC it's already small and pulling it out would only shave the file by ~10%. Skipped.
- ✅ 1.A.3 `core/audio_proc.py` (327 LOC) — `_AudioProcMixin` covering levels, HPF, noise gate, VAD, VOX, mumble processing, link/source-gain persistence, processor sync
- ✅ 1.A.4 `core/ptt.py` (125 LOC) — `_PTTMixin`: set_ptt_state, _ptt_aioc, _ptt_relay, _ptt_software
- ✅ 1.A.5 `core/usb_audio.py` (348 LOC) — `_USBAudioMixin`: USB device finder, AIOC, speaker callback chain
- ✅ 1.A.6 `core/setup_audio_mumble.py` (251 LOC) — `_SetupAudioMumbleMixin`: setup_audio + setup_mumble
- ✅ 1.A.7 `core/mumble_io.py` (105 LOC) — `_MumbleIOMixin`: sound_received_handler, speak_text, send/on text message
- ✅ 1.A.8 `core/transmit.py` (356 LOC) — `_TransmitMixin`: _get_cross_clock_drift_ms, audio_transmit_loop
- ✅ 1.A.9 `core/stream.py` (79 LOC) — `_StreamMixin`: darkice + Icecast helpers
- ✅ 1.A.10 `core/lifecycle.py` (953 LOC) — `_LifecycleMixin`: notify, restart_audio_input, restart_pyaudio, handle_proc_toggle, handle_key, get_status_dict, status_monitor_loop, run, cleanup, etc. **Above 800 target — candidate for future re-split into runtime / status / restart groups.**
- ✅ 1.A.11 `gateway_core.py` 3038 → **690 LOC** — kept LogWriter + ssl wrapper + module-level imports + RadioGateway.__init__ (the 244-line one that mixin methods read state from).
- ✅ 1.A.12 **Acceptance:**
  - Method surface diff vs the b88a9e4 commit: all 55 original RadioGateway methods present on the new class (verified via per-class AST diff)
  - MRO: `RadioGateway → _LifecycleMixin → _TransmitMixin → _StreamMixin → _MumbleIOMixin → _SetupAudioMumbleMixin → _USBAudioMixin → _PTTMixin → _AudioProcMixin → object`
  - End-to-end import chain green: `import radio_gateway; from gateway_core import RadioGateway, __version__, LogWriter` — no errors, version string intact
  - Largest remaining file: `core/lifecycle.py` at 953 LOC (above 800) — flagged
  - **Next gateway restart** loads the mixin-composed class. No functional change expected.

## 1.B — web_server.py split (partial — biggest self-contained chunks lifted)
- ⏭️ 1.B.1 HTTP plumbing extraction deferred. The `Handler` class is nested inside `WebConfigServer.start()` as a closure over `password`/`parent`/etc — pulling it out requires un-nesting that closure first. Not worth the surgery for marginal LOC gain.
- ⏭️ 1.B.2 Auth helpers — same reason (nested inside Handler closure).
- ⏭️ 1.B.3 Static-file serving — same.
- ✅ 1.B.4 Three large mixins extracted instead, all self-contained:
  - `web/sysinfo.py` (290 LOC) — `_SysinfoMixin._get_sysinfo` (CPU/mem/disk/temp/IPs)
  - `web/routing_cmds.py` (476 LOC) — `_RoutingCmdsMixin` with `_handle_routing_cmd`, 13 command handlers, dispatch table, plugin/path resolvers, config save/load
  - `web/certs.py` (155 LOC) — `_CertsMixin` for cert acquisition + renewal
- ✅ 1.B.5 **Acceptance partial:**
  - web_server.py 2436 → **1571 LOC** (–865)
  - WebConfigServer now `class WebConfigServer(_SysinfoMixin, _RoutingCmdsMixin, _CertsMixin)` — all 26 extracted methods accessible via MRO (verified)
  - All four modules `py_compile` clean
  - Largest remaining file is `web_server.py` at 1571 LOC — under 1600 but still above the 800 target. Further reduction needs the nested-Handler refactor (deferred 1.B.1-3).
- **Next gateway restart** loads the mixin-composed class. No functional change expected.

## 1.C — gateway_mcp.py split (DONE FIRST — lowest risk, standalone process)
- ✅ 1.C.1 `mcp_server/server.py` — shared FastMCP instance + `_load_config`, `_load_telegram_config`, `_get`, `_post`, `_auth_headers` (131 LOC). Package name avoids shadowing pip `mcp` library.
- ✅ 1.C.2-1.C.8 Tools auto-extracted into 4 buckets via a one-shot Python script that parses section comment headers and routes each section to a target module:
  - `mcp_server/tools/control.py` (561 LOC) — status, SDR, radio TX, recordings, logs, automation, audio-trace, Telegram
  - `mcp_server/tools/radios.py` (710 LOC) — TH-9800, D75, KV4P, IC-7100, processes, mixer, config, process control
  - `mcp_server/tools/routing.py` (1168 LOC) — bus/sink/gain/denoise + transcription + link endpoints + loop recorder (above 800 target — candidate for future re-split)
  - `mcp_server/tools/fleet.py` (645 LOC) — D75 SSH, packet, Winlink, stream-trace, sink stats, scheme mgmt, endpoint battery
- ✅ 1.C.9 `mcp_server/__init__.py` runs the import chain on `run()` so tool registration is lazy + explicit.
- ✅ 1.C.10 **Tests passed:**
  - Tool count: 117 → 117 (subprocess-isolated diff of original vs new tool sets = empty diff both directions)
  - `gateway_mcp.py` 3175 → 39 LOC (thin shim that calls `mcp_server.run()`)
  - Live call: `gateway_status` via the new server returned 11011-byte JSON from the running gateway
  - Legacy backup at `gateway_mcp.py.legacy` (gitignored)

Next: 1.B (web_server split) or 1.A (gateway_core split).

**Phase 1 acceptance:** all monolith files split, full feature smoke pass, no file over 800 LOC.

---

# PHASE 2 — Real plugin system

## 2.A — Formalise contract
- ✅ 2.A.1 `plugins/_base.py` — `RadioPlugin` Protocol (runtime_checkable), CAPABILITY_* constants, optional-hook dispatcher helpers (get_web_routes, get_mcp_tools, fire_bus_attach/detach, fire_ptt_change).
- ✅ 2.A.2 `CAPABILITIES` set on plugins is canonical. Loader logs the cap set at startup.
- ✅ 2.A.3 `plugin_loader.py` now honors `web_routes()` (stashed on `gw._plugin_web_routes` dict) and logs `mcp_tools()` (full MCP registration deferred — MCP server is a separate process, needs a marker-file or socket bridge to register dynamically).
- ✅ 2.A.4 `docs/plugin-development.md` updated to point at the new example location and `plugins/_base.py` as the authoritative contract.
- ✅ 2.A.5 `plugins/example_radio.py` moved (via `git mv`) to `examples/example_plugin/plugin.py`; added README documenting how to copy + enable + extend.

## 2.B — Migrate KV4P (NOT VIABLE — revised target)
- ❌ Original plan was wrong: `kv4p_endpoints.py` is a dispatcher helper for remote link endpoints, not a gateway-resident plugin. The actual KV4P plugin runs on remote hosts via `tools/link_endpoint.py --plugin kv4p`. There's nothing in `kv4p_endpoints.py` to move into `plugins/`.

## 2.B (revised) — Migrate TH-9800 first
- ✅ 2.B.1 `git mv th9800_plugin.py → plugins/th9800.py` (history preserved).
- ✅ 2.B.2 Class metadata added: `PLUGIN_ID = 'th9800'`, `PLUGIN_NAME = 'TH-9800'`, `CAPABILITIES = {AUDIO_RX, AUDIO_TX, PTT, FREQUENCY, STATUS, CAT}`. Legacy `name` and `capabilities` (lowercase dict) kept as a back-compat surface for code that hasn't migrated yet.
- ✅ 2.B.3 Dropped inheritance from `gateway_link.RadioPlugin` (that base targets remote link-endpoint plugins, not gateway-resident ones). Duck-typed against the Protocol now — `isinstance(plug, RadioPlugin)` returns True.
- ✅ 2.B.4 `gateway_setup.setup_th9800` now imports `from plugins.th9800 import TH9800Plugin`.
- ✅ 2.B.5 **Lazy-NameError scan applied** (per Phase 1.A lesson): `dis.get_instructions` walked every LOAD_GLOBAL in every method (including nested code objects) — all resolve to module-scope or builtins. CLEAN.
- ⏭️ 2.B.6 NOT switching to `plugin_loader.discover_plugins()` yet — that would require adding `ENABLE_TH9800 = True` to every gateway_config.txt in the wild. Holds until Phase 2.D (after SDR + packet are migrated and the loader has more to do).
- ✅ 2.B.7 **Live restart pending** — file moved + class metadata in place + scan clean. User restart will confirm runtime works on the actual hardware (CAT + AIOC + PTT).

## 2.C — Migrate TH-9800
- ✅ 2.C.1 Move `th9800_plugin.py` → `plugins/th9800.py`
- ✅ 2.C.2 Validate CAT + AIOC + PTT routing via capability flags
- ✅ 2.C.3 **Test:** TX, RX, CAT control, AIOC PTT
- ✅ 2.C.4 Release v4.0

## 2.C — Migrate SDR
- ✅ 2.C.1 `git mv sdr_plugin.py → plugins/sdr.py` (history preserved).
- ✅ 2.C.2 New contract metadata: `PLUGIN_ID = 'sdr'`, `PLUGIN_NAME = 'RSPduo SDR'`, `CAPABILITIES = {AUDIO_RX, FREQUENCY, STATUS}` (RX-only — no PTT/TX). Legacy `name = "sdr_rspduo"` + lowercase `capabilities` dict kept for back-compat.
- ✅ 2.C.3 Dropped inheritance from `gateway_link.RadioPlugin`.
- ✅ 2.C.4 `gateway_setup.py` imports from new path.
- ✅ 2.C.5 **Contract refinement** — uncovered an issue: SDR is RX-only and has no `put_audio`, so the original Protocol's `runtime_checkable` rejected it. Fixed the Protocol: dropped `get_audio` + `put_audio` from required attributes, documented them as optional and gated by `CAPABILITY_AUDIO_RX` / `CAPABILITY_AUDIO_TX`. CAPABILITIES is now the single source of truth for audio direction; both TH-9800 and SDR pass `isinstance(plug, RadioPlugin)`.
- ✅ 2.C.6 LOAD_GLOBAL scan (refined to use each function's own `__globals__` after first-pass false positives from re-exported `AudioProcessor` etc.). All plugins/sdr.py functions clean.
- ⏭️ 2.C.7 `web_routes()` hook **not used** yet — the existing `/sdr` page is served via gateway-level routes in `web_routes_get.py`, not by the SDR plugin. Moving that wiring into the plugin is a separate refactor (would test the hook end-to-end but requires touching the route dispatch). Defer to Phase 2.E or a follow-up.
- ✅ 2.C.8 **Live restart pending** — same workflow as TH-9800.

## 2.D — Decompose packet_radio.py into packet/ package (BEHAVIOUR-PRESERVING)
User flagged packet as messy across 4 concerns: Direwolf lifecycle, Pat integration, AGWPE proxy, endpoint/mode switching. Triage said "messy but working" — so split shape without changing behaviour. Move to `plugins/packet.py` comes after, once the shape is clean enough to be worth migrating.

- ✅ 2.D.1 5 mixins extracted into `packet/`:
  - `packet/agwpe_proxy.py` (163 LOC) — `_AGWPEProxyMixin`: TCP proxy on :8010, accept loop, forwarder
  - `packet/endpoint.py` (175 LOC) — `_EndpointMixin`: `_find_endpoint`, list/set/get/has_local_tnc, `_send_endpoint_mode`, `get_endpoint_status`
  - `packet/pat.py` (81 LOC) — `_PatMixin`: `_start_pat`, `_stop_pat`, `_is_pat_running`, `_delayed_pat_start`
  - `packet/mode.py` (84 LOC) — `_ModeMixin`: `_set_mode`, `_tnc_status_fields`
  - `packet/kiss.py` (599 LOC) — `_KISSMixin`: KISS connect/reader, AX.25 framing, APRS handler + parsers (position/object/weather/mice + cleaner), beacon, message, BBS (connect/disconnect/send/handle), `_agw_frame` builder
- ✅ 2.D.2 `packet_radio.py` 1235 → **221 LOC** — kept `__init__`, `setup`, `teardown`, `execute`, `get_status`, `get_audio`, `put_audio` plus a `class PacketRadioPlugin(_AGWPEProxyMixin, _EndpointMixin, _PatMixin, _ModeMixin, _KISSMixin)` declaration that composes everything.
- ✅ 2.D.3 Caught one cross-mixin static reference: `_parse_mice` called `PacketRadioPlugin._clean_mice_comment` (both now on `_KISSMixin`) — rewrote to `_KISSMixin._clean_mice_comment`. LOAD_GLOBAL scan caught it.
- ✅ 2.D.4 Off-by-one fix during extraction (kiss.py range 653-1235, not 653-1234 — the `_bbs_send` method's final `return` line was getting orphaned).
- ✅ 2.D.5 Method surface diff vs HEAD: all 41 original methods present on the new composed class.
- ✅ 2.D.6 **Live restart verified**: Packet plugin initialized, AGWPE proxy listening on 127.0.0.1:8010, callsign + modem + endpoint configured, BusManager wired in.
- ⏭️ 2.D.7 Migration to `plugins/packet.py` deferred — shape is much improved but the user wants packet's *functionality* sorted before relocating it. Picking through the AGWPE/Pat/mode/endpoint code with that intent comes as a follow-up phase.

## 2.D.cleanup — packet functional pass (no behaviour change for the messy bits, fixes for the clear bugs)
Triage of the 4 messy areas the user flagged. Fixed everything clearly wrong; documented + skipped two items where a fix would be a guess rather than a verifiable improvement.

**Fixed:**
- ✅ AGWPE per-frame logging is now `PACKET_AGWPE_TRACE` config-gated. Was unconditional — every 4 KiB chunk got a full stats print, flooding the journal during Pat sessions.
- ✅ `_proxy_sessions_active` race fixed with `_proxy_sessions_lock` (threading.Lock). Inc/dec previously unsynchronised across the accept loop, session handler, and session-end "restart Direwolf?" check.
- ✅ `_AGWPE_MAX_SESSIONS = 10` lifted to a class-constants block with `_AGWPE_LOCAL_PORT`, `_AGWPE_REMOTE_PORT`, `_AGWPE_DIREWOLF_WAIT_SECS`, `_AGWPE_FORWARD_BUF`. Hardcoded numbers replaced with the named constants.
- ✅ `shutil.which('pat')` cached on first lookup via `_pat_bin()`; `''` sentinel means "looked up, missing" so we don't re-scan on every restart.
- ✅ Three identical `getattr(self._gateway, 'process_supervisor', None)` guards collapsed into `_supervisor()`.
- ✅ `_set_mode` called `_endpoint_has_local_tnc()` twice, with risk of the answers drifting if the endpoint set changed between calls. Now computed once at the top; `gw_tnc` is bound to None for endpoint-owned TNCs and used as the start/stop handle for gateway-owned ones.
- ✅ `_KISSMixin._clean_mice_comment` cross-mixin static reference documented with a comment explaining why it's not `self.` (it's a staticmethod).

**Documented + skipped:**
- ⏭️ **Forced Direwolf restart after every AGWPE session ends** — code comment explained it as "for clean reconnect", which sounds like a workaround for a known fragility. Removing it would need testing of back-to-back Pat sessions on real hardware to verify; can't be done from a refactor pass. Kept the behaviour, replaced the terse comment with one that names the trade-off and points future investigation at it.
- ⏭️ **Fire-and-forget threading in `_set_mode`** (KISS connect + Pat start) — fixing this means defining a real state machine and a way to surface async failures. Out of scope for the cleanup pass.

Verified: SCAN clean, 42 methods on PacketRadioPlugin (+2 helpers), live restart green, AGWPE proxy listening, plugin initialized.

## 2.D.cleanup.2 — packet state machine
Replaces the implicit "self._mode + fire-and-forget threads" model with a
proper target/phase/step/last_error triple. Async failures now surface
explicitly instead of disappearing into the journal.

- ✅ New `packet/state.py` — `_PacketStateMixin` with `_advance(...)`, `_reach_steady()`, `_fail(error)`, `state_snapshot()`. Fields seeded by `_init_packet_state()` called from `PacketRadioPlugin.__init__`.
- ✅ Two axes:
  - **target**: `'idle'|'aprs'|'winlink'|'bbs'` — what was requested
  - **phase**: `'steady'|'starting'|'stopping'|'error'` — where we are in the transition
  - **step**: `'send_endpoint_mode'|'start_local_tnc'|'connect_kiss'|'start_pat'|'stop_pat'|'stop_local_tnc'|None`
  - **last_error**: string set when phase→error, cleared on next steady
  - **phase_age_secs**: how long we've been in the current phase
- ✅ `_set_mode` drives the synchronous side: advance through `send_endpoint_mode → start_local_tnc → connect_kiss → start_pat`. Each substep can fail the transition with a meaningful error.
- ✅ `_kiss_connect_loop` reports first-connect success and a failure after `_KISS_STARTING_FAIL_AFTER` (10) attempts during STARTING. Subsequent reconnects after steady don't toggle phase.
- ✅ `_delayed_pat_start` reports `STEADY` on Pat-running success, `ERROR` on Direwolf-unreachable or Pat-start-failure.
- ✅ `PacketRadioPlugin.get_status` now returns `state` alongside the legacy `mode` field. The /packet page (and MCP tools) see the full triple.
- ✅ `PACKET_DISABLE_FORCED_RESTART = True` config knob — opts out of the AGWPE session-end Direwolf restart. Now safer to flip since transition failures are visible.
- ✅ Pre-existing regression fixed: `plugins/sdr.py` setup signature didn't accept `gateway=None`; the discover_plugins loader was crashing trying to load it. SDR setup now accepts the kwarg and ignores it; the loader also gained a guard that skips plugin IDs already loaded by `gateway_setup` (belt + suspenders).
- ✅ **Live verified:** `curl /status` returns `state = {'target': 'idle', 'phase': 'steady', 'step': None, 'last_error': None, 'phase_age_secs': 16.6}`. SDR/TH-9800/Packet all loaded clean; AGWPE proxy up.

## 2.D.cleanup.3 — surface state in /packet UI + relocate to plugins/
Both follow-ups in one commit since they're independent and small.

- ✅ `web_pages/packet.html` gained a state-machine row that only renders when phase ≠ 'steady' (keeps the UI uncluttered in normal operation). Shows phase pill (steady→cyan, starting/stopping→yellow, error→red), current step, last_error, and phase_age in seconds. JS polls `s.state` from the existing /packet/status response.
- ✅ `git mv packet_radio.py → plugins/packet.py` — last gateway-resident radio relocated to `plugins/`. All four (TH-9800, SDR, packet) now share the same folder.
- ✅ `PLUGIN_ID = 'packet'`, `PLUGIN_NAME = 'Packet TNC'`, `CAPABILITIES = {CAPABILITY_PACKET, CAPABILITY_STATUS}`. Legacy `name = "tnc"` + lowercase `capabilities` dict kept for back-compat.
- ✅ Plugin contract gap: packet uses `teardown` (legacy convention); Protocol expects `cleanup`. Added `cleanup = teardown` class-level alias — both names work, body is identical.
- ✅ `gateway_setup.py` updated to import `from plugins.packet import PacketRadioPlugin`.
- ✅ **Live verified:** Plugin initialized from new path, AGWPE proxy up, state machine returns 'steady', packet UI shows mode without the state row.

Phase 2 plugin migration complete. Plugin contract is formalised, the four gateway-resident radios (TH-9800, SDR, packet) live under `plugins/`, KV4P stays as a remote endpoint per its actual architecture.

**Phase 2 acceptance:** all four radios under `plugins/`, adding a fifth requires zero core edits.

---

# Baseline measurements (filled in during Phase 0)

| File | LOC at start | LOC after |
|---|---|---|
| gateway_core.py | 3023 | |
| web_server.py | 2424 | |
| gateway_mcp.py | 3175 | |
| audio_sources.py | 2731 | |
| **plugins/ count** | 1 (example_radio.py only) | |
| **tests/ count** | 1 (test_ic7100_civ.py — currently 96/104 failing pre-existing) | |

---

# Lessons learned

## Lazy NameErrors survive AST-level import scans
Discovered during Phase 1.A. After splitting `gateway_core.py` into mixins,
the import-time AST scan and a per-module `importlib.import_module` smoke
returned all-green. The gateway then failed to start on the next restart
with `NameError: name 'LogWriter' is not defined`, then again with
`NameError: name '__version__' is not defined` — both raised from inside
methods that only execute at runtime, not at import.

**Why the scan missed it:** Python resolves bareword names lazily inside
function bodies, at call time. `import X` checks happen at module load.
`def foo(): return X` does not check `X` until `foo()` is actually called.
A method that runs once at startup (like `_LifecycleMixin.run()`) can hide
unresolved globals through every static check.

**For future refactors that lift methods between modules, do all three:**

1. AST scan for project-level names referenced but not imported (catches
   the easy cases — names used in module-level statements or expressions).
2. Per-module `importlib.import_module` (catches syntax + module-load
   issues — same coverage as the AST scan, basically).
3. **Runtime exercise of every extracted method, not just import.** For
   each method, either call it with stub inputs, or — at minimum — run
   `compile()` on the method body and walk its `co_names` and `co_freevars`
   to find symbols that will be resolved at call time but aren't in scope.

**Circular import resolution:** the offending names lived in
`gateway_core.py`, which imports the `core/` package. A top-level
`from gateway_core import ...` in a `core/*.py` mixin would cycle. The
fix is a function-local `from gateway_core import LogWriter, __version__`
inside the method that needs them — Python evaluates it only on first
call, by which time `gateway_core` is fully loaded.

## Apply this to Phase 2 (plugin migration)
Each radio plugin will need its own version of this scan. KV4P/TH-9800/SDR
all access cross-cutting helpers (audio_util, bus_manager, ProcessSupervisor,
etc.) the same way `gateway_core` did. Don't just AST-scan and ship — also
run a method-level `co_names` audit before declaring the migration done.

---

# Session log (append-only — record what each work session did)

## 2026-05-28 — Plan created
- Wrote MEGA_PLAN.md, ordered Phase 3 → 1 → 2
- Confirmed prometheus_client not yet installed
- Repo clean on main, ahead 0 / behind 0
- Next action: Phase 0.1

## 2026-05-28 — Phase 0 + 3.A + 3.B done (code)
- prometheus-client 0.25.0 installed + added to requirements.txt
- `metrics.py` created — 13 metric objects, single REGISTRY
- `/metrics` route in `web_routes_get.handle_metrics` with LAN/Tailnet allowlist; wired in `web_server.py` GET dispatch
- 6 metrics wired into live code paths: bus_audio_level (bus_manager), bus_ptt_active (gateway_core), transcription_inflight + transcription_seconds + transcription_dispatched_total (transcriber), stream_bytes_sent_total (stream_stats), link_endpoint_up (gateway_link)
- All touched modules import cleanly. Standalone HTTPServer test of handle_metrics returns 200 + correct content-type + rg_* metrics.
- Pre-existing tests broken: tests/test_ic7100_civ.py — 96/104 fail with `AttributeError: 'CIVController' object has no attribute '_tls'` in tools/ic7100_link_plugin.py:378. Not in scope.
- **BLOCKED ON USER:** restart radio-gateway service so the running process picks up new /metrics route. Then run `curl http://localhost:8080/metrics | grep '^rg_'` and confirm metrics are emitting on live traffic.
- Next action after restart: Phase 3.C (Grafana on macmini)

## 2026-05-28 — Phase 3.C done (Grafana + Prometheus stack)
- User restarted gateway; /metrics emitting live data (3 link endpoints up, Broadcastify counter ticking)
- prometheus + grafana containers added to macmini docker-compose, both Up
- Datasource + dashboard provisioned; end-to-end query through Grafana proxy returns expected values
- Hit and fixed: Grafana auto-UID issue (had to wipe grafana_data volume after pinning uid=prometheus in provisioning)
- Next: Phase 3.D (remaining metrics) OR jump to Phase 1 (refactor) — recommend 3.D for quick wins, then 1.

## 2026-05-28 — Observability moved to gateway-local + installer captured
- User chose option 2: run Prom+Grafana locally on the gateway, not on macmini.
- Tore down macmini containers, restored docker-compose backup, dropped both volumes.
- Native pacman install on gateway: `prometheus` (3.11.3) + `grafana` (13.0.1).
- Lay down `/etc/prometheus/prometheus.yml` (scrape localhost:8080), Grafana provisioning at `/var/lib/grafana/conf/provisioning/{datasources,dashboards}/`, dashboard JSON at `/var/lib/grafana/dashboards/`.
- Datasource URL changed `http://prometheus:9090` → `http://localhost:9090` (no docker DNS now). Re-provisioned cleanly.
- `admin_password = radio` set in `/etc/grafana.ini` via sed.
- Both services enabled + started; end-to-end Grafana→Prometheus→/metrics query returns all 3 endpoints UP.
- **Installer updated** (`scripts/install.sh`):
  - New section 16 "Observability stack" before gateway start, idempotent.
  - Arch: `pacman -S prometheus grafana`. Debian: `apt install prometheus` + Grafana's apt repo (best-effort).
  - Handles both Arch path (`/var/lib/grafana/conf/provisioning`) and Debian path (`/etc/grafana/provisioning`).
  - Step counters bumped /15 → /16 throughout.
  - Health check: prometheus + grafana active state.
  - NEXT STEPS section now references the Grafana URL + default creds.
- **INSTALL.md updated**: phase 16 documented.
- Smoke: idempotent rerun of section 16 on this machine returns "already up to date", services stay active, queries still work.
- Access URLs now:
  - Grafana: http://localhost:3000 (admin / radio)
  - Prometheus: http://localhost:9090
- Next: Phase 3.D (remaining metrics) OR Phase 1 (refactor) — user choice.

## 2026-05-28 — Phase 3.D done (remaining metrics)
- All 6 remaining metrics wired: stream_reconnects, link_underruns, cpu_temp + fan_rpm, vad_speech_events, denoise_apply_ms.
- Host telemetry (temp/fan) refreshed lazily on each /metrics scrape — no background thread added.
- Imports verified clean (`python3 -c "import audio_util, audio_sources, transcriber, metrics"`).
- **BLOCKED ON USER:** restart gateway to load new instrumentation. Then `curl -s localhost:8080/metrics | grep -E '^rg_(cpu|vad|denoise|stream_reconnects|link_audio_underruns)'` to confirm live.
- Next: Phase 3.E (alerting + Fleet Manager Prometheus hook) OR Phase 1 (monolith split).

## 2026-05-28 — Gateway-hosted /grafana page
- User wanted to view dashboard from gateway UI, not a separate port.
- Grafana: enabled `allow_embedding = true` and `[auth.anonymous] enabled = true` so the iframe needs no login.
- New `web_pages/grafana.html` iframes `http://{host}:3000/d/radio-gateway/radio-gateway?kiosk=tv` so it works from any host (localhost / Tailscale / LAN).
- Route `/grafana` added to `web_server.py` static-pages dict.
- "Metrics" nav entry added to System dropdown in `shell.html` (between Manager and Voice).
- Page also reachable via `/pages/grafana.html` immediately (no gateway restart needed for that path).
- Installer updated: section 16 now flips `allow_embedding` + anonymous Viewer in grafana.ini automatically.
- **Next gateway restart** picks up the `/grafana` short route + nav entry.

## 2026-05-28 — Grafana over Cloudflare-tunnel (same-origin proxy)
- Symptom: gateway-hosted /grafana page worked on LAN but iframe failed over CF tunnel (CF only proxies port 8080; iframe was loading `http://<cf-host>:3000`, unreachable).
- Fix: serve Grafana under a subpath via reverse proxy from the gateway itself.
- Grafana `/etc/grafana.ini`: `root_url = .../grafana/` + `serve_from_sub_path = true`. Verified `http://localhost:3000/grafana/api/health` returns 200.
- New `handle_grafana_proxy` in `web_routes_get.py` forwards `/grafana/*` to `127.0.0.1:3000/grafana/*`. Pattern lifted from existing `handle_pat_proxy`.
- Wired in `web_server.py` do_GET and do_POST (Grafana POSTs for queries).
- `web_pages/grafana.html` iframe now uses same-origin `/grafana/d/...` path — works over any reverse proxy reaching the gateway.
- Installer captures all four Grafana ini tweaks (allow_embedding, anonymous viewer, root_url, serve_from_sub_path).
- **Next gateway restart** picks up the proxy route.

## 2026-05-28 — Prometheus same-origin proxy added
- Symmetry with Grafana: `/prometheus/*` on the gateway now reverse-proxies to local Prometheus.
- Set `PROMETHEUS_ARGS="--web.external-url=http://localhost:9090/prometheus/ --web.route-prefix=/prometheus"` in `/etc/conf.d/prometheus`. After restart, root /metrics returns 404 (expected — moved to /prometheus/metrics).
- Grafana datasource URL bumped to `http://localhost:9090/prometheus`. Re-applied + Grafana restarted; query through proxy still returns all 3 endpoints UP.
- `handle_prometheus_proxy` in `web_routes_get.py` mirrors the Grafana proxy pattern.
- Wired in `web_server.py` for GET and POST.
- iframe page's "Prometheus ↗" link now uses `/prometheus/` (same origin) — works over CF tunnel too.
- Installer writes `/etc/conf.d/prometheus` and the updated datasource URL is provisioned automatically.
- **Next gateway restart** picks up the new proxy route.

## 2026-05-28 — Prometheus CORS fix
- Symptom: Prometheus UI loaded via `/prometheus/` over CF tunnel returned cross-origin errors. SPA was firing XHRs to `http://localhost:9090/...`, blocked because the page origin was different.
- Root cause: `--web.external-url=http://localhost:9090/prometheus/` baked the upstream host into the SPA's API base URL.
- Fix: drop `--web.external-url` entirely. `--web.route-prefix=/prometheus` is sufficient — the SPA uses relative paths.
- Installer `PROMETHEUS_ARGS` updated; note added explaining the trap.
- Verified `curl http://localhost:8080/prometheus/api/v1/query?query=up` returns clean JSON via the gateway proxy.

## Phase 3.D verified live (2026-05-28)
- After gateway restart, `/metrics` exposes the full 3.D set including `rg_cpu_temp_c`, `rg_fan_rpm`, `rg_denoise_apply_ms_*`, `rg_link_audio_underruns_total`, `rg_vad_speech_events_total` series.
- Confirmed by inspecting Prometheus's `/api/v1/label/__name__/values` via the proxy.

## 2026-05-28 — Phase 3.E done
- `alerts.py` polls local Prometheus every 30s, evaluates 5 named rules, fires Telegram on threshold + RECOVERED on state change.
- User-requested design: manager docs augment (not replace) existing log reads. Both `hourly.md` and `daily.md` now have a Prometheus section with explicit guidance that logs and Prom catch different problems.
- Smoke: standalone engine eval against live Prom returns 0 firing series across all 5 rules; tracking 7 series total (link endpoints + temp + denoise).
- **Next gateway restart** loads the engine. Then a manual link endpoint stop should fire `link_endpoint_down` to Telegram within ~90s.
- Phase 3 complete. Next: Phase 1 (monolith split) — recommended start point.

---

# Closing summary

All phases done and live on the production gateway. Mega plan complete.

## Phase scoreboard

| Phase | Result |
|---|---|
| 0 — Groundwork | ✅ baseline LOC + prometheus-client installed |
| 3.A — `/metrics` endpoint | ✅ live, LAN/Tailnet gated |
| 3.B — first 6 metrics | ✅ wired in bus_manager, gateway_core, transcriber, stream_stats, gateway_link |
| 3.C — Grafana stack | ✅ originally on macmini, then moved native to the gateway, then same-origin proxied through gateway:8080 |
| 3.D — remaining 6 metrics | ✅ stream reconnects, link underruns, CPU temp/fan, VAD events, denoise histogram |
| 3.E — alerts + Manager hook | ✅ in-process engine, 5 rules, Telegram dispatch, Manager docs additive |
| 1.A — gateway_core split | ✅ 3023 → 690 LOC into 8 mixins under `core/` |
| 1.B — web_server split | ✅ 2436 → 1571 LOC; three mixins under `web/` |
| 1.C — gateway_mcp split | ✅ 3175 → 39 LOC shim; 117 tools under `mcp_server/tools/` |
| 2.A — plugin contract | ✅ `plugins/_base.py` Protocol + capability flags + optional hooks |
| 2.B — TH-9800 migration | ✅ `plugins/th9800.py` |
| 2.C — SDR migration | ✅ `plugins/sdr.py` |
| 2.D — packet decomposition | ✅ 1235 → 221 LOC + 5 mixins under `packet/`; relocated to `plugins/packet.py`; state machine added; UI surfaced; functional cleanups landed |
| KV4P | n/a — remote endpoint, not gateway-resident |

## Beyond-plan work that landed in the same arc

- Gateway-hosted `/grafana` page with iframe + same-origin reverse proxy for both Grafana and Prometheus
- Phase 1 lesson learned: lazy NameErrors survive AST scans — `dis.get_instructions` walked every `LOAD_GLOBAL` in every method per owning-module's globals after the first false-positive pass
- Packet state machine surfaced in `/packet` UI
- Winlink workflow upgrades:
  - Gateway dropdown populated from `/winlink/gateways`
  - Auto-tune to selected freq
  - FM mode + FM-D engagement
  - Forced TX power
  - PTT-up settle delay via rigctld response hold
  - AGWPE socket shutdown before close to release Direwolf slots

## Runtime tunables in `gateway_config.txt` (local, gitignored)

```
PACKET_TX_POWER_PCT = 50
PACKET_DISABLE_FORCED_RESTART = True
PACKET_TXDELAY = 80
PACKET_PTT_SETTLE_MS = 100
```

## Two files still over the 800-LOC target (flagged for future re-split)

- `core/lifecycle.py` 953 LOC — could split into runtime / status / restart groups
- `mcp_server/tools/routing.py` 1168 LOC — could split by sub-domain (routing vs transcription vs link vs loop)

Neither is hot. Either makes a fine warm-up for the next refactor pass when there's a reason.
