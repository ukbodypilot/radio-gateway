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
`/` shell, `/dashboard`, `/routing`, `/controls`, `/radio`, `/d75`, `/kv4p`, `/sdr`, `/gps`, `/repeaters`, `/aircraft`, `/telegram`, `/monitor`, `/recordings`, `/transcribe`, `/packet`, `/config`, `/logs`, `/voice`

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

### Packet Radio + Winlink (2026-04-04)
- Remote Direwolf TNC on FTM-150 Pi endpoint (192.168.2.121), TX + CM108 PTT + AGW port
- Pat Winlink client on Pi, AGW connected-mode for Winlink (KISS only does UI frames)
- Web UI: compose, inbox/outbox/sent, connect & sync, live connection log
- Successful email exchange via KM6RTE-12 on 144.970 MHz (Loma Ridge, OC, CA)
- Pat config at ~/.config/pat/config.json — NEVER commit (contains Winlink password)
- See [project_packet_radio.md](project_packet_radio.md) for full details

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
- D75: link endpoint on 192.168.2.134 via BT proxy (crontab @reboot, autologin, MX Linux/Debian 12)
- FTM-150: AIOC link endpoint on 192.168.2.121 (systemd user service, audio/data mode)
- GPS: u-blox GNSS on `/dev/gps` (udev rule), real position for APRS/repeaters
- `packet_radio.py` — KISS client, APRS parser, Pat Winlink lifecycle (local Direwolf removed)

## Bus Processing (2026-04-05)
- AudioProcessor has stateful IIR filters — MUST process once per bus tick, not per-sink
- Primary listen bus processor: `_listen_bus_processor` on gateway_core, applied to `_early_audio`
- Passive sink gains (mumble, broadcastify, speaker, recording): `_sink_gains` dict, applied in bus_manager delivery
- PCM streams now get processed audio (was raw)
- SDR `continuous = false` hardcoded in rtl_airband config — silence keepalive handles Broadcastify

## Web UI Navigation (2026-04-05)
- Shell nav grouped into dropdowns: Audio, Radios, Data, System
- CSS hover/click dropdowns, no framework
- Active page highlights parent group label

## FTM-150 Reverse Engineering (2026-04-05, shelved)
- Proprietary PWM-modulated bus (~50kHz carrier), not UART/SPI/I2C
- Would need analog demodulator before digital capture is viable
- Files in `ftm150-re/` + `docs/ftm150-reverse-engineering.md`
- sigrok + PulseView installed on gateway machine

## Audio Quality (2026-04-04, branch: audio-quality)
- AIOC reader: `arecord` subprocess bypasses PipeWire (PyAudio/sounddevice read silence)
- BusManager: accumulative clock, fire-and-forget PTT, GC disabled
- Stream trace: `stream_trace.py` records per-chunk at every handoff, dumps to `tools/stream_trace.txt`
- Research: `docs/audio-quality-research.md` — Python limits, Rust/JACK/PipeWire alternatives
- Final metrics: all streams 50.0ms stdev <2ms, zero overflows/drops/clicks

## See Also
- [bugs.md](bugs.md) — bug history
- [bugs_2026_03_30.md](bugs_2026_03_30.md) — v2.0 routing bugs
- [bugs_2026_04_01.md](bugs_2026_04_01.md) — marathon session bugs
- [feedback_config_safety.md](feedback_config_safety.md) — config damage prevention
- [feedback_single_source_config.md](feedback_single_source_config.md) — GUI changes write to config file, not separate JSON
- [feedback_no_gateway_restart.md](feedback_no_gateway_restart.md) — Claude can restart gateway
- [feedback_instrument_not_guess.md](feedback_instrument_not_guess.md) — measure before fixing audio issues
- [project_audio_quality.md](project_audio_quality.md) — audio quality fixes, trace system, full details
- [project_d75_cleanup.md](project_d75_cleanup.md) — legacy D75 removal target ~2026-04-08
- [reference_gdrive_backup.md](reference_gdrive_backup.md) — rclone backup to Google Drive
- [project_ftm150_endpoint.md](project_ftm150_endpoint.md) — FTM-150 AIOC endpoint on Pi 192.168.2.121
- [project_packet_radio.md](project_packet_radio.md) — Packet Radio (Direwolf TNC) + Winlink email
- [project_ftm150_reverse_eng.md](project_ftm150_reverse_eng.md) — FTM-150 control head RE (shelved)
- [bugs_2026_04_05.md](bugs_2026_04_05.md) — bus processing, SDR noise, endpoint mode bugs
