# Radio Gateway — Project Memory

## Project Overview
Radio-to-Mumble gateway with SDR, multiple radios, web UI, and AI features. Python 3, Arch Linux.

**Config:** `gateway_config.txt` (INI, `.gitignore` — NEVER commit, contains secrets)
**Start:** `sudo systemctl restart radio-gateway.service` (or start.sh)
**Version:** 3.0 (released 2026-04-07)

## Codebase Structure (post-3.0, 2026-04-07)
- `gateway_core.py` (~3,200) — RadioGateway class, simplified main loop, audio setup, Mumble, status
- `bus_manager.py` (~820) — BusManager: ALL bus ticks + sink delivery (listen, solo, duplex, simplex)
- `audio_bus.py` — ListenBus, SoloBus (auto-switches endpoint data→audio on TX), DuplexRepeaterBus, SimplexRepeaterBus
- `audio_sources.py` — AudioSource subclasses, StreamOutputSource
- `loop_recorder.py` (~480) — per-bus continuous recording, segmented MP3, waveform data
- `plugin_loader.py` (~80) — auto-discovers plugins from `plugins/` directory
- `plugins/example_radio.py` — template for external radio plugins
- `web_server.py` (~2,050) — WebConfigServer, Handler dispatch, _CONFIG_LAYOUT
- `web_routes_get.py` (~1,040) — core GET route handlers
- `web_routes_post.py` (~1,390) — POST route handlers
- `web_routes_stream.py` (379) — WebSocket/streaming handlers
- `web_routes_loop.py` (~120) — Loop recorder API handlers
- `web_routes_packet.py` (~200) — Packet radio + Winlink API handlers
- `text_commands.py` (718) — Mumble chat commands, key dispatch, TTS
- `audio_trace.py` (846) — watchdog trace loop + HTML trace dump
- `stream_stats.py` (117) — DarkIce/Icecast stats
- `sdr_plugin.py` — RSPduo dual tuner plugin
- `th9800_plugin.py` — TH-9800 AIOC plugin (audio_level computed post-gate)
- `kv4p_plugin.py` — KV4P HT radio plugin
- `gateway_link.py` — Link protocol, server, client, RadioPlugin base class
- `gateway_mcp.py` — MCP server (stdio, 60+ tools including 6 loop recorder tools)
- `repeater_manager.py`, `transcriber.py`, `smart_announce.py`, `radio_automation.py`, `ptt.py`
- Utility modules: `ddns_updater.py`, `email_notifier.py`, `cloudflare_tunnel.py`, `mumble_server.py`, `usbip_manager.py`, `gps_manager.py`

## Web UI Pages
`/` `/dashboard` `/routing` `/controls` `/radio` `/d75` `/kv4p` `/sdr` `/gps` `/repeaters` `/aircraft` `/telegram` `/monitor` `/recordings` `/recorder` `/transcribe` `/packet` `/config` `/logs` `/voice`

## Key Subsystems

### 3.0 Architecture (2026-04-07)
- ALL buses managed by BusManager in a daemon thread
- Main loop: drains BusManager queues, SDR rebroadcast TX, WebSocket push
- `sync_listen_bus()` manages source add/remove from routing config
- `self.mixer = bus_manager.listen_bus` for backward compat

### Loop Recorder (2026-04-07)
- Enable via "R" button per bus in routing UI
- Segmented MP3 (5-min chunks), `.wfm` sidecar (peak+RMS per second)
- Silence padding from segment boundary for cross-bus alignment
- Live waveform from active segment in memory
- Canvas viewer: zoom/pan, click-to-play, right-click-drag select, export MP3/WAV
- Stacked multi-bus view with independent playback
- Configurable retention per bus (1h-7d), dashboard stats panel
- HTTP Range support on `/loop/play` for seeking
- MCP tools: status, toggle, retention, summary, activity, export
- API: `/loop/buses`, `/loop/waveform`, `/loop/play`, `/loop/export`

### Plugin Auto-Discovery (2026-04-07)
- Drop `.py` in `plugins/`, set `ENABLE_X = True`, restart — zero code changes
- `plugin_loader.py` scans for classes with `PLUGIN_ID` attribute
- Auto-registered in BusManager `_get_source()` and `sync_listen_bus()`
- Template: `plugins/example_radio.py`
- Docs: `docs/plugin-development.md` (local plugins + link endpoints)

### FTM-150 Auto Mode Switch (2026-04-07)
- SoloBus._fire_ptt checks endpoint mode before keying
- If mode='data' (Direwolf), auto-sends mode switch to audio, waits 500ms, then keys
- Logs warning so user knows it happened
- Sink ID fix: strip trailing underscore (`ftm_150_tx` → `ftm_150`)

### Shell Nav Bar (2026-04-07)
- MP3/PCM/MIC: fixed-width buttons, timer inside button text, no indicator dots
- Play buttons turn red when active, default volume 50%

## Config Safety (CRITICAL)
- `_CONFIG_LAYOUT` in web_server.py is master list — Save wipes keys not listed
- NEVER use `replace_all=true` on config file
- `gateway_config.txt` is NOT in git — repo is PUBLIC

## User Preferences
- Commits requested explicitly, no auto-push, concise responses, no emojis
- Instrument code rather than guess at bugs
- Separate files for new features (not monolith)
- Config file is master for startup state; runtime controls reset on restart

## Machine — user-optiplex3020 (Arch Linux)
- Python 3.14, sudo password: `user`, Git user: ukbodypilot
- AIOC: `/dev/ttyACM0`, KV4P: `/dev/kv4p`, Relay: `/dev/relay_radio`
- D75: link endpoint on 192.168.2.134 via BT proxy
- FTM-150: AIOC link endpoint on 192.168.2.121
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
- [project_d75_cleanup.md](project_d75_cleanup.md) — legacy D75 removal target ~2026-04-08
- [reference_gdrive_backup.md](reference_gdrive_backup.md) — rclone backup to Google Drive
- [project_ftm150_endpoint.md](project_ftm150_endpoint.md) — FTM-150 AIOC endpoint
- [project_packet_radio.md](project_packet_radio.md) — Packet Radio + Winlink email
- [project_ftm150_reverse_eng.md](project_ftm150_reverse_eng.md) — FTM-150 control head RE (shelved)
- [project_listen_bus_unify.md](project_listen_bus_unify.md) — listen bus unification (COMPLETED)
- [project_rust_audio_core.md](project_rust_audio_core.md) — Rust audio core (future, deferred)
- [project_loop_recorder.md](project_loop_recorder.md) — loop recorder details + bugs fixed
