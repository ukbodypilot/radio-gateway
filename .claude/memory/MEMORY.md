# Radio Gateway — Project Memory

## Project Overview
Radio-to-Mumble gateway with SDR, multiple radios, web UI, and AI features. Python 3, Arch Linux.

**Config:** `gateway_config.txt` (INI, `.gitignore` — NEVER commit, contains secrets)
**Start:** `sudo systemctl restart radio-gateway.service` (or start.sh)
**Version:** 3.1.0 (released 2026-04-09)

## Codebase Structure (post-cleanup, 2026-04-09)
- `gateway_core.py` (~3,200) — RadioGateway class, simplified main loop, audio setup, Mumble, status
- `bus_manager.py` (~900) — BusManager: ALL bus ticks + sink delivery; SDR1/SDR2 as separate source nodes
- `audio_bus.py` — ListenBus, SoloBus, DuplexRepeaterBus, SimplexRepeaterBus
- `audio_sources.py` (~2,200) — AudioSource subclasses, StreamOutputSource, LinkAudioSource
- `audio_util.py` (300) — shared level metering (pcm_level, pcm_rms, pcm_db, rms_to_level, update_level), AudioProcessor, CW generation
- `loop_recorder.py` (~480) — per-bus continuous recording, segmented MP3, waveform data
- `plugin_loader.py` (~80) — auto-discovers plugins from `plugins/` directory
- `web_server.py` (~2,100) — WebConfigServer, Handler dispatch, _CONFIG_LAYOUT, bus rename
- `web_routes_get.py` (~920) — core GET route handlers
- `web_routes_post.py` (~1,350) — POST route handlers, _resolve_source() helper
- `web_routes_stream.py` (379) — WebSocket/streaming handlers
- `web_routes_loop.py` (~130) — Loop recorder API handlers (includes bus display names)
- `web_routes_packet.py` (~200) — Packet radio + Winlink API handlers
- `text_commands.py` (714) — Mumble chat commands, key dispatch, TTS
- `audio_trace.py` (846) — watchdog trace loop + HTML trace dump
- `stream_trace.py` (117) — per-stream trace with overflow/underrun/slow_drain events
- `sdr_plugin.py` (~1,700) — RSPduo dual + single tuner modes, per-channel PipeWire sinks
- `th9800_plugin.py` — TH-9800 AIOC plugin
- `kv4p_plugin.py` — KV4P HT radio plugin
- `gateway_link.py` — Link protocol, server, client, RadioPlugin base, AudioPlugin noise gate
- `gateway_mcp.py` — MCP server (stdio, 88 tools including sdr_set_mode, sdr_single_tune, sdr_add/remove_channel, bus_rename)
- `repeater_manager.py`, `transcriber.py`, `smart_announce.py`, `radio_automation.py`, `ptt.py`

## Web UI
- Pages: `/dashboard` `/routing` `/controls` `/radio` `/d75` `/kv4p` `/sdr` `/gps` `/repeaters` `/aircraft` `/telegram` `/monitor` `/recordings` `/recorder` `/transcribe` `/packet` `/config` `/logs` `/voice`
- `common.js` (124 lines) — postJson, getJson, createPoller, sendKey, openTmux, fmtSecs, fmtTimestamp, fmtDuration, fmtBytes
- `common.css` (68 lines) — theme variables, status colors, layout grid, level bars, buttons
- Shell nav: home icon for dashboard, fixed-width MP3/PCM/MIC buttons, group labels 0.78em
- Routing page: bus rename (double-click), gain slider reset (double-click), alphabetical auto-arrange
- Loop recorder: red local-time clock during playback, bus display names

## Key Subsystems

### SDR Single-Tuner Mode (2026-04-09)
- `SDR_MODE`: 'dual' (master/slave rspduo_mode=4/8) or 'single' (rspduo_mode=1, multi-channel)
- Single mode: one rtl_airband process, multiple channels in config, lower sample rates
- CPU reduction: 31% → 13% (57% less) at 1 MHz sample rate
- Per-channel PipeWire sinks: ch1 → sdr_capture, ch2 → sdr_capture2
- SDR1/SDR2 registered as separate source nodes in bus_manager for independent routing
- Settings persisted in `sdr_channels.json` (both dual and single sections, mode field)
- Bandwidth viz on SDR page shows channels relative to tunable band
- Mode switch: stop → verify → reconfigure → restart → verify → report (with rollback)
- Max 2 channels in single mode (maps to sdr1/sdr2 ducking)
- Queue: maxsize=16, slow drain above 4 chunks (target ~150-200ms latency)
- RSPduo dual-tuner mode locked at 2 MS/s (hardware constraint) — single mode allows 0.25-10.66 MS/s
- Instrumented: overflow, underrun, slow_drain in stream trace; mode switch timing in console
- 500 kHz has parec jitter — use 1 MHz minimum for clean audio
- D75 endpoint host (192.168.2.134): Intel Celeron N2807, sysbench 1157 evt/s — Pi Zero 2W (~1228 evt/s) is viable replacement

### D75 Link Endpoint (cleaned 2026-04-08)
- D75 is link-endpoint-only — all legacy d75_plugin.py code removed (~1,136 lines deleted)
- `scripts/remote_bt_proxy.py` kept (used by `tools/d75_link_plugin.py`)
- Link endpoint on 192.168.2.134 via BT proxy

### AudioPlugin Noise Gate (2026-04-08)
- Default threshold raised -48 → -40 dB (AIOC noise floor ~-45 dB)
- Gate threshold + enabled state persisted in endpoint `settings.json`
- FTM-150 endpoint on 192.168.2.121, files at `/home/user/link/`

### Loop Recorder
- Toggle off calls `loop_recorder.stop(bus_id)` to close active segment immediately
- Disabled buses filtered from `/loop/buses` API
- Bus display names from routing config shown in dashboard + recorder page

### audio_util.py (2026-04-08)
- Extracted from audio_sources.py: AudioProcessor, CW generation, level metering
- `pcm_level(pcm, current, gain)` — one-call RMS→dB→0-100→smoothed
- `pcm_db(pcm)` — dB level for threshold checks
- `pcm_rms(pcm)` — raw RMS value
- Used by all plugins (kv4p, th9800, sdr, link sources) — replaces ~55 inline metering sites

## Config Safety (CRITICAL)
- `_CONFIG_LAYOUT` in web_server.py is master list — Save wipes keys not listed
- NEVER use `replace_all=true` on config file
- `gateway_config.txt` is NOT in git — repo is PUBLIC

## User Preferences
- Commits requested explicitly, no auto-push, concise responses, no emojis
- Instrument code rather than guess at bugs — measure before fixing audio issues
- Separate files for new features (not monolith)
- Config file is master for startup state; runtime controls reset on restart
- Every control must be closed-loop (confirm success/failure)
- Protect working code — don't damage existing dual-tuner SDR mode

## Machine — user-optiplex3020 (Arch Linux)
- Intel i5-4590 4-core, 16 GB RAM, Python 3.14, sudo password: `user`, Git user: ukbodypilot
- AIOC: `/dev/ttyACM0`, KV4P: `/dev/kv4p`, Relay: `/dev/relay_radio`
- D75: link endpoint on 192.168.2.134 via BT proxy
- FTM-150: AIOC link endpoint on 192.168.2.121 (`/home/user/link/`)
- GPS: u-blox GNSS on `/dev/gps` (udev rule)

## See Also
- [bugs.md](bugs.md) — bug history
- [bugs_2026_03_30.md](bugs_2026_03_30.md) — v2.0 routing bugs
- [bugs_2026_04_01.md](bugs_2026_04_01.md) — marathon session bugs
- [bugs_2026_04_05.md](bugs_2026_04_05.md) — bus processing, SDR noise, endpoint mode bugs
- [feedback_config_safety.md](feedback_config_safety.md) — config damage prevention
- [feedback_single_source_config.md](feedback_single_source_config.md) — GUI changes write to config file
- [feedback_no_gateway_restart.md](feedback_no_gateway_restart.md) — Claude can restart gateway
- [feedback_instrument_not_guess.md](feedback_instrument_not_guess.md) — measure before fixing audio issues
- [project_audio_quality.md](project_audio_quality.md) — audio quality fixes, trace system
- [project_d75_cleanup.md](project_d75_cleanup.md) — D75 cleanup COMPLETED 2026-04-08
- [reference_gdrive_backup.md](reference_gdrive_backup.md) — rclone backup to Google Drive
- [project_ftm150_endpoint.md](project_ftm150_endpoint.md) — FTM-150 AIOC endpoint
- [project_packet_radio.md](project_packet_radio.md) — Packet Radio + Winlink email
- [project_ftm150_reverse_eng.md](project_ftm150_reverse_eng.md) — FTM-150 control head RE (shelved)
- [project_listen_bus_unify.md](project_listen_bus_unify.md) — listen bus unification (COMPLETED)
- [project_rust_audio_core.md](project_rust_audio_core.md) — Rust audio core (future, deferred)
- [project_loop_recorder.md](project_loop_recorder.md) — loop recorder details + bugs fixed
- [project_sdr_single_mode.md](project_sdr_single_mode.md) — SDR single-tuner mode details
