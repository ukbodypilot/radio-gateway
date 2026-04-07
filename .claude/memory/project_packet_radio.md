---
name: Packet Radio (Direwolf TNC) + Winlink integration
description: Remote Direwolf TNC on FTM-150 Pi endpoint, APRS decode, Winlink email via Pat
type: project
---

## Packet Radio — Status (2026-04-04)

**Architecture:** Direwolf runs on the FTM-150 Pi endpoint (192.168.2.121), reading the AIOC directly. Gateway connects via KISS TCP for decoded packets. The endpoint's AIOC plugin has audio/data mode switching — gateway sends mode command via link protocol.

### What's done

#### Phase 1-3: APRS decode + remote TNC (2026-04-03)
- `packet_radio.py` — cleaned up, KISS client + APRS parser only (722 lines, no local Direwolf)
- Remote TNC: endpoint manages Direwolf lifecycle (start/stop via mode command)
- AIOC plugin audio/data mode: `execute({'cmd':'mode','mode':'data'})` closes PyAudio, starts Direwolf; `'audio'` reverses
- APRS parsing: uncompressed, compressed, timestamped (@), MIC-E, weather, objects, status, messages
- MIC-E comment cleaner: strips Kenwood/Yaesu radio codes, Base91 telemetry, DAO extensions
- Weather data parsed to human-readable format
- Digipeater path extraction from AX.25 frames, relay lines on map
- Direwolf log forwarded from endpoint to gateway via link protocol STATUS frames
- `/packet` web page: Status tab (DW log default open), APRS map with path lines, Winlink/BBS tabs
- GPS: real u-blox receiver on /dev/gps (udev rule), multi-constellation GSV parsing
- Config: `[packet]` section in `_CONFIG_LAYOUT` so web save doesn't wipe it

#### Phase 4: Winlink email via Pat (2026-04-04)

**Direwolf TX + PTT:**
- Added TX audio output: `ADEVICE plughw:N,0 plughw:N,0` (was `ADEVICE plughw:N,0 null`)
- Added CM108 PTT: `PTT CM108 /dev/hidraw0 3` in Direwolf config generation
- AIOC PTT channel 3 = Direwolf CM108 GPIO 3 (NOT 2 — 0-indexed in Direwolf, GPIO 2 didn't work, GPIO 3 works)
- Both PyAudio input AND output streams must be closed before Direwolf starts (exclusive ALSA access)

**AGW port enabled:**
- Changed AGWPORT from 0 (disabled) to 8010 in Direwolf config
- KISS doesn't transmit AX.25 connected-mode frames (SABM/UA), only UI frames
- AGW is required for Winlink connections (connected mode)

**Pat Winlink client:**
- `/usr/local/bin/pat` v0.19.2 installed on Pi endpoint (192.168.2.121)
- Config at `~/.config/pat/config.json` (NOT in repo — contains Winlink password)
- CRITICAL: Top-level `"AGWPE": {"addr": "192.168.2.121:8010"}` key required (NOT nested under ax25)
- Pat started/stopped by `packet_radio.py` via `_start_pat()` / `_stop_pat()`
- `_delayed_pat_start()` — waits for Direwolf to be ready before launching Pat

**Winlink web UI (packet.html):**
- Native compose form (To, CC, Subject, Body) calls `pat compose` CLI
- Connect & Sync button calls `pat connect ax25+agwpe:///GATEWAY` CLI
- Inbox/Outbox/Sent folders read Pat's mailbox at `~/.local/share/pat/mailbox/`
- Message viewer for reading received emails
- Live connection log panel (polls `/packet/winlink/log` every 500ms during sync)
- Pat reverse proxy at `/pat/*` kept for potential future use
- Font size increased for readability

**Successful Winlink exchange:**
- Connected to KM6RTE-12 on 144.970 MHz (Loma Ridge, Orange County, CA)
- Sent and received email over 1200 baud packet radio through Winlink CMS
- Full round-trip: compose on web UI, connect via AGW, relay through RMS gateway, CMS delivery

### Removed (local Direwolf — audio quality was unusable)
- Local Direwolf subprocess management, stdin audio pipe
- ALSA loopback capture, PyAudio, resampling, UDP sockets, numpy dependency
- TNC source/sink routing nodes, packet_rx bus
- All removed from: packet_radio.py, bus_manager.py, web_server.py, web_routes_get.py, routing_config.json

### Key files
- `packet_radio.py` — KISS client, APRS parser, endpoint mode controller, Pat lifecycle
- `gateway_link.py` — AIOCPlugin with audio/data mode, Direwolf subprocess (TX+PTT+AGW), log forwarding
- `web_pages/packet.html` — UI with Direwolf log panel, APRS map, Winlink compose/inbox/connect
- `web_routes_get.py` — Pat proxy handler, Winlink mailbox/read/log API endpoints
- `web_routes_post.py` — Winlink compose and connect handlers (pat CLI wrappers)
- `web_server.py` — /pat proxy routes (GET+POST), /packet/winlink/* routes
- `gateway_core.py` — Direwolf log forwarding in `_link_on_endpoint_status`
- Pi: `/home/user/link/gateway_link.py`, `/home/user/link/link_endpoint.py`
- Pi: `~/.config/systemd/user/ftm150-endpoint.service` (Restart=always)
- Pi: `~/.config/pat/config.json` (Winlink password — NEVER commit)
- Pi: direwolf-tnc.service DISABLED (replaced by endpoint-managed mode)

### Endpoint resilience (2026-04-03)
- Endpoint survives gateway restarts: `on_connect` callback -> `reopen_audio()`
- `reopen_audio()` terminates PyAudio and reinits (PipeWire loses enumeration on terminate, but name scan recovers)
- `get_audio()` returns None in data mode (no reopen attempts)
- Error handler respects data mode (no reopen on expected stream closure)
- Systemd user service replaces crontab+wrapper script (no duplicate processes)
- Zero-read watchdog (200 consecutive zero-peak reads -> reopen)

### TH-9800 fixes (2026-04-03)
- `tx_audio_boost` separated from `audio_boost` (TX slider was changing RX gain)
- Non-blocking TX: `put_audio` queues to deque, `_tx_writer_loop` thread writes to AIOC
- PyAudio reinit on "PortAudio not initialized" error in RX reader
- Minor PCM stutter remains (~10ms gaps at 50ms period) -- bus tick timing, not blocking

### Bugs found and fixed (2026-04-04)
- **Endpoint mode switch race:** AIOCPlugin._set_mode() closed PyAudio before setting self._mode='data', causing get_audio() to see audio mode + no stream, triggering reopen which crashed. Fix: set _mode before closing streams. Also close BOTH input AND output streams.
- See bugs.md for full details.

### Bugs found and fixed (2026-04-05)
- **Endpoint stuck in data mode:** `_send_endpoint_mode('audio')` silently failed. Fix: return True/False, report errors, added endpoint status to UI + Force Audio button.
- **ALSA device busy on mode switch:** PyAudio instance not terminated (only streams closed). Fix: terminate `self._pa` + increase delay to 1.0s before Direwolf start.
- **Endpoint status in UI:** New row on /packet page shows EP mode, DW process, audio I/O, HID. Mismatch warning banner when gateway idle but endpoint stuck in data.
- **`_find_endpoint()` cached:** Avoids `getpeername()` syscall on every status poll.

### Config keys
```ini
[packet]
ENABLE_PACKET = true
PACKET_CALLSIGN = WA6NKR
PACKET_SSID = 1
PACKET_MODEM = 1200
PACKET_REMOTE_TNC = 192.168.2.121
PACKET_KISS_PORT = 8001
```

### Critical knowledge
- KM6RTE-12 on 144.970 MHz is a working Winlink RMS gateway (Loma Ridge, OC, CA)
- AIOC PTT channel 3 = Direwolf CM108 GPIO 3 (NOT 2)
- Pat AGWPE config must be top-level key in config.json, not nested under ax25
- Pat config at ~/.config/pat/config.json contains Winlink password — NEVER commit
- KISS protocol only supports UI frames — must use AGW for Winlink (connected-mode SABM/UA)
- Direwolf needs BOTH input AND output ADEVICE entries for TX capability
- Both PyAudio input AND output streams must be closed before Direwolf starts (exclusive ALSA)

### Next steps
- Phase 5: BBS terminal (AX.25 connected mode via AGW)
- APRS map not rendering for user — needs JS debugging (data is correct, Leaflet issue)
- Routing page: source->sink direct connections now blocked (validation on connectionCreated)

**Why:** User wants APRS map, Winlink email, BBS access via packet radio on FTM-150.
**How to apply:** Remote TNC is the only supported path. Local Direwolf code is removed.
