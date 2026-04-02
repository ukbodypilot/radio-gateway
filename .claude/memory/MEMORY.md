# Radio Gateway — Project Memory

## Project Overview
Radio-to-Mumble gateway with SDR, multiple radios, web UI, and AI features. Python 3, Arch Linux.

**Config:** `gateway_config.txt` (INI, `.gitignore` — NEVER commit, contains secrets)
**Start:** `sudo systemctl restart radio-gateway.service` (or start.sh)

## Codebase Structure (post-refactor 2026-04-02)
- `gateway_core.py` (3,694 lines) — RadioGateway class, main loop, audio setup, Mumble, status
- `web_server.py` (2,032 lines) — WebConfigServer, Handler dispatch, _CONFIG_LAYOUT, helpers
- `web_routes_get.py` (957) — 28 GET route handlers
- `web_routes_post.py` (1,390) — 27 POST route handlers
- `web_routes_stream.py` (379) — 4 WebSocket/streaming handlers
- `text_commands.py` (718) — Mumble chat commands, key dispatch, TTS
- `audio_trace.py` (846) — watchdog trace loop + HTML trace dump
- `stream_stats.py` (117) — DarkIce/Icecast stats
- `audio_sources.py` — AudioSource subclasses, StreamOutputSource
- `audio_bus.py` — ListenBus, SoloBus, DuplexRepeaterBus, SimplexRepeaterBus
- `bus_manager.py` — BusManager, sink delivery, routing config
- `sdr_plugin.py` — RSPduo dual tuner plugin
- `th9800_plugin.py` — TH-9800 AIOC plugin
- `kv4p_plugin.py` — KV4P HT radio plugin
- `gateway_link.py` — Link protocol, server, client, plugins
- `repeater_manager.py` — ARD repeater database, GPS proximity queries
- `transcriber.py` — Whisper voice-to-text (streaming + chunked modes)
- `smart_announce.py` — AI announcement engine (claude CLI backend)
- `radio_automation.py` — Automation engine (scheme parser, repeater DB, recorder)
- `ptt.py` — RelayController, GPIORelayController
- Utility modules (each one class): `ddns_updater.py`, `email_notifier.py`, `cloudflare_tunnel.py`, `mumble_server.py`, `usbip_manager.py`, `gps_manager.py`
- `gateway_utils.py` — re-export shim (backward compat for old imports)
- `gateway_mcp.py` — MCP server (stdio, 55+ tools, talks to HTTP API on port 8080)

## Web UI Pages
`/` shell, `/dashboard`, `/routing`, `/controls`, `/radio`, `/d75`, `/kv4p`, `/sdr`, `/gps`, `/repeaters`, `/aircraft`, `/telegram`, `/monitor`, `/recordings`, `/transcribe`, `/config`, `/logs`, `/voice`

## Key Subsystems

### GPS Receiver (2026-04-02)
- `gps_manager.py`: USB serial NMEA or `GPS_PORT = simulate` for fake DM13do data
- `/gps` page: Leaflet map, DOP probability ring, satellite SNR chart, SIM/LIVE toggle
- SIM/LIVE toggle: `switch_mode()` — no restart needed

### Repeater Database (2026-04-02)
- `repeater_manager.py`: ARD per-state JSON, GPS proximity, 24h cache
- `/repeaters` page: map + table, MASTER/SLAVE SDR assignment + SET, KV4P Tune button
- MCP: nearby_repeaters, repeater_info, repeater_tune, repeater_refresh

### Transcription Freq Tags (2026-04-02)
- `feed(audio, source_id=bus_id)` passes bus context to transcriber
- `_resolve_freq_tag()` maps bus→radio→frequency
- Results prefixed with [freq] in logs, Mumble, Telegram

### Broadcastify Streaming
- `StreamOutputSource` in audio_sources.py: direct PCM→ffmpeg→Icecast
- Silence keepalive thread: feeds 50ms silence frames when no real audio (prevents idle disconnect)
- Auto-reconnect in send_audio() when connection drops

### Cloudflare Tunnel
- 15-min health check: relaunches cloudflared if dead, emails new URL
- `on_url_changed` callback → EmailNotifier.send_tunnel_changed()

### KV4P Frequency Validation
- `_FREQ_RANGES`: SA818_VHF (134-174 MHz), SA818_UHF (400-480 MHz)
- Rejects out-of-band tunes, warns at startup. Unknown modules permissive.

## Config Safety (CRITICAL)
- `_CONFIG_LAYOUT` in web_server.py is master list — Save wipes keys not listed
- 20 missing keys were added in audit (2026-04-02): ptt, mumble, web, switching, sdr, transcription, telegram sections
- NEVER use `replace_all=true` on config file; use anchored sed patterns
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

## See Also
- [bugs.md](bugs.md) — bug history
- [bugs_2026_03_30.md](bugs_2026_03_30.md) — v2.0 routing bugs
- [bugs_2026_04_01.md](bugs_2026_04_01.md) — marathon session bugs
- [feedback_config_safety.md](feedback_config_safety.md) — config damage prevention
- [feedback_single_source_config.md](feedback_single_source_config.md) — GUI changes write to config file, not separate JSON
- [feedback_no_gateway_restart.md](feedback_no_gateway_restart.md) — Claude can restart gateway
- [project_d75_cleanup.md](project_d75_cleanup.md) — legacy D75 removal target ~2026-04-08
- [reference_gdrive_backup.md](reference_gdrive_backup.md) — rclone backup to Google Drive
