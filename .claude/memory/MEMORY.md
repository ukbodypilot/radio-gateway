# Radio Gateway — Project Memory

## Update this file
Update MEMORY.md and detail files at the end of every session and whenever a significant bug or pattern is discovered. Keep this file under 200 lines.

## Project Overview
Radio-to-Mumble gateway. AIOC USB device handles radio RX/TX audio and PTT. Optional SDR input via PipeWire virtual sink or ALSA loopback. Optional Broadcastify streaming via DarkIce. Python 3, runs on Raspberry Pi, Debian amd64, and Arch Linux.

**Main file:** `radio_gateway.py` (~15000+ lines)
**Installer:** `scripts/install.sh` (13 steps, targets Debian/Ubuntu/RPi/Arch Linux)
**Config:** `gateway_config.txt` (INI format with `[section]` headers, copied from `examples/` on install)
**Start script:** `start.sh` (11 steps: kill procs, Mumble GUI, TH-9800 CAT, Claude Code, CPU governor, loopback, AIOC USB reset, pipe, DarkIce, FFmpeg, gateway w/nice -10)
- **HEADLESS_MODE = true** (2026-03-25): skips Mumble GUI launch in start.sh
**Windows client:** `windows_audio_client.py` (server: send audio, client: receive audio, `m` to switch)

## SDR Input — PipeWire (preferred) or ALSA Loopback
- **PipeWire:** `SDR_DEVICE_NAME = pw:sdr_capture` — reads from virtual sink monitor via `parec`
- `PipeWireSDRSource` class: auto-creates sink via `pw-cli` if missing at startup
- WirePlumber persistence: `~/.config/wireplumber/wireplumber.conf.d/90-sdr-capture-sink.conf`
- Creates `sdr_capture` and `sdr_capture2` sinks; installer deploys this config (step 12)
- **CRITICAL:** WirePlumber null-sinks need `monitor.passthrough = true` or monitor output is silence
- **ALSA loopback:** `SDR_DEVICE_NAME = hw:4,1` — traditional method, 200ms blob delivery

## SDR Control Page (v1.5.0) — RTLSDR-Airband + SoapySDR + RSPduo Dual Tuner
- **`RTLAirbandManager` class** (~300 lines): manages rtl_airband lifecycle
- **Audio chain:** RSPduo Tuner → SoapySDR → rtl_airband → PulseAudio → sdr_capture PipeWire sink → gateway
- **Web routes:** `/sdr` (control page), `/sdrstatus` (JSON, polled 1s), `/sdrcmd` (POST: tune/save/recall/delete/restart/stop)
- **RSPduo Master/Slave:** SDR1 `rspduo_mode=4` (Master), SDR2 `rspduo_mode=8` (Slave). Start order critical.
- **Plugin:** fventuri `dual-tuner-submodes` branch — pin `IgnorePkg = soapysdrplay3-git` in `/etc/pacman.conf`

## ADS-B Aircraft Tracking (2026-03-21)
- RTL2838/R820T USB dongle; dump1090-fa + lighttpd on port 30080; fr24feed → FlightRadar24
- Gateway reverse proxy: `/adsb/*` → `http://127.0.0.1:{ADSB_PORT}`

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — TCP, length-prefixed PCM, `ptt_control=True`, `priority=0`
- **CRITICAL:** Send at real-time rate (one chunk per tick) — queue maxsize=16; flooding drops 90% of audio

## Browser Microphone PTT (2026-03-12)
- `WebMicSource` class: browser mic via WebSocket `/ws_mic`, routes to radio TX
- **CRITICAL:** AIOC GPIO PTT does NOT key this user's radio — PTT wired via CAT serial only

## Web Configuration UI & Live Dashboard
- `WebConfigServer` class: built-in HTTP server (Python `http.server`, no Flask)
- Pages: `/` shell, `/dashboard`, `/sdr`, `/radio` (TH-9800), `/d75` (TH-D75), `/aircraft`, `/recordings`, `/logs`
- **D75/KV4P processing buttons (2026-03-25):** Gate/HPF/LPF/Notch per source, with live highlighting
- **Telegram panel:** "Open" button launches xfce4-terminal attached to Claude tmux session

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- **CRITICAL: DISPLAY_TEXT vfo_byte** — must use vfo_byte from packet, NOT stale `_channel_vfo`
- **Auto serial connect (2026-03-21):** On startup, always sends `!serial disconnect` then `!serial connect`

## TH-D75 Bluetooth Radio (2026-03-24, updated 2026-03-26)
- `D75CATClient` class in `cat_client.py`; remote proxy: `scripts/remote_bt_proxy.py` on 192.168.2.134
- Proxy ports: 9750 (CAT text), 9751 (raw 8kHz PCM audio)
- **CRITICAL — btstart is non-blocking:** proxy returns "btstart initiated" immediately; BT connects in background thread
- **`connected` status (2026-03-26):** requires BOTH TCP AND serial_connected (was TCP-only)
- **Reconnect (2026-03-26):** `_disconnect_for_reconnect()` avoids `close()` killing poll thread; `_recv_line` EOF sets `_connected=False`; btstart retries every 15s if serial stays down
- **CRITICAL — poll thread self-join:** `close()` checks `self._poll_thread is not threading.current_thread()` before joining
- **PTT (2026-03-26):** fire-and-forget `_sock.sendall()` — must NOT use `_send_cmd` (blocks audio thread, competes with poll for responses)
- **Channel load via FO:** uses ME fields with lockout field skip + TX freq→offset conversion
- **`d75GoChannel`:** checks `serial_connected` before sending commands; checks `dual_band/active_band` for band switching
- **Up/Down buttons:** send `!cat UP` / `!cat DN` via passthrough

### D75 FO Command — 21-field format (LA3QMA / Hamlib thd74.c verified)
- **Field map (0-indexed):** `[0]=band [1]=rxfreq [2]=offset_hz [3]=rxstep [4]=txstep [5]=mode [6]=fine_mode [7]=fine_step [8]=tone [9]=ctcss [10]=dcs [11]=cross [12]=reverse [13]=shift [14]=tone_idx [15]=ctcss_idx [16]=dcs_idx [17]=cross_type [18]=urcall [19]=dsql_type [20]=dsql_code`
- **Mode values:** 0=FM, 1=DV, 2=AM, 3=LSB, 4=USB, 5=CW, 6=NFM, 7=DR, 8=WFM
- **42-tone CTCSS list** (not 39): includes 206.5, 229.1, 254.1
- **FO SET:** radio gives no echo response — use async readback
- **Proxy no SSH:** 192.168.2.134 has no SSH; deploy via `git pull` from remote desktop

### D75 ME→FO Mapping (2026-03-26)
- ME has 23 fields, FO has 21. ME[14] = lockout (not in FO). ME[22] = name/flags
- **ME→FO conversion:** `fields[1:14] + fields[15:22]` (skip lockout at ME[14])
- **ME field[2] dual meaning:** small (<100MHz) = offset Hz, large (>=100MHz) = TX frequency
- **Convert large values:** `offset = abs(TX_freq - RX_freq)` — cross-band channels had bogus 437MHz offset without this
- Documented in D75-CAT-Control fork: `docs/cat_over_bluetooth.md`

### D75 TX Audio via BT SCO (2026-03-26)
- **Path:** gateway `write_tx_audio` → downsample 48k→8k → TCP port 9751 → proxy `_rx_loop` → `write_sco` → SCO to radio
- **CRITICAL:** SCO is SEQPACKET with 48-byte frames — must split data into frame-sized chunks
- Dedicated `_tx_loop` thread paces frame delivery at 3ms intervals from a buffer
- Without frame splitting: silent. Without pacing: 10Hz stutter
- Announcement pre-TX delay applies to ALL radios (gives radio time to key up)

### D75 BT Proxy Reliability (2026-03-26)
- **SM poll interval:** 3s (was 0.5s — killed BT link). Exponential backoff after 3 failures, up to 30s
- **Init:** only ID/FV/AE on connect; FO/SM/PC/DL/BC/BL/TN/PT deferred to stream loop (FO times out during early connect)
- **Proxy queries:** BL (battery), TN (TNC mode/band), PT (beacon type)
- **Stale socket cleanup** before btstart retry

## Systemd Service & Process Management
- **Service:** `radio-gateway.service` — `KillMode=control-group`, `TimeoutStopSec=15`
- **CRITICAL:** Always restart gateway via start.sh, never `python3 radio_gateway.py` directly
- **Telegram bot service:** `telegram-bot.service` — installed by installer, enable manually after config

## PTT Methods
- `PTT_METHOD`: `aioc` (default), `relay`, or `software`
- **CRITICAL:** Always use `!ptt on`/`!ptt off` (explicit state), never bare `!ptt` (blind toggle causes state inversion)
- Software PTT refuses to key if radio hasn't sent data in >5s (radio powered off)

## AIOC PTT — RTS Relay Coordination (CRITICAL, 2026-03-15)
- **Sequence:** pause drain → RTS Radio Controlled → key AIOC → [TX] → unkey AIOC → RTS USB Controlled → resume drain

## KV4P HT Radio (added 2026-03-19)
- `KV4PAudioSource` class: CP2102 USB-serial, kv4p-ht-python package, Opus codec
- **CRITICAL:** DRA818 uses 38 tones (no 69.3 Hz) — off-by-one CTCSS errors with TH-9800's 39-tone list
- **CRITICAL:** PTT_METHOD=aioc does NOT key KV4P — KV4P uses its own serial PTT (`_ptt_kv4p`)

## Audio Mixer — Duck State Machine (aioc_vs_sdrs)
- `aioc_ducks_sdrs = ds['is_ducked'] or in_padding` — SDR suppressed while ducked or in transition padding
- **Re-duck inhibit** (`REDUCK_INHIBIT_TIME = 2.0s`): blocks new duck-out for 2s after duck-in
- **SDR_SIGNAL_THRESHOLD = -45.0 dBFS**: D75 idle noise at ~-65 dBFS was permanently ducking SDRs
- **D75 starts muted by default**: prevents D75 background noise from ducking SDRs on startup

## TX Talkback (2026-03-26)
- **Config:** `TX_TALKBACK = False` (default off) — in `[ptt]` section
- When off, TX audio goes ONLY to radio; local outputs receive concurrent RX audio instead
- **Dashboard:** "TX Talkback" button in PTT group; **MCP:** `mixer_control(action='flag', flag='talkback')`

## MCP Server (gateway_mcp.py) — AI Control Interface (2026-03-25)
- **File:** `gateway_mcp.py` — stdio MCP server; 31 tools; talks to gateway HTTP API on port 8080
- **CRITICAL:** MCP server is a Claude Code child process — restarting gateway does NOT restart MCP

### /mixer HTTP Endpoint (2026-03-25)
- **POST `/mixer`** — 7 actions: status, mute/unmute/toggle, volume, duck, boost, flag, processing
- **Sources:** global, tx, rx, sdr1, sdr2, d75, kv4p, remote, announce, speaker

## Telegram Bot — Phone Control (2026-03-24)
- **File:** `tools/telegram_bot.py` — stdlib only; service: `tools/telegram-bot.service`
- Voice notes: ffmpeg → PCM → port 9601 (ANNIN) at real-time rate → radio TX with auto PTT
- **CRITICAL:** Send audio at real-time rate — ANNIN queue maxsize=16; flooding drops audio

## Smart Announcements — claude-cli backend (2026-03-24)
- `SmartAnnouncementManager` in `smart_announce.py`: `claude -p` subprocess, 120s timeout
- No API key — uses Claude Code auth (Max subscription)

## Planned Next Features
- [USBIP USB over TCP](project_usbip.md) — `USBIPManager` class to share USB devices over TCP port 3240
- **MCP remote access** — expose gateway_mcp.py over SSE/HTTP via Cloudflare tunnel

## Known Bugs Fixed (details in bugs.md)
Key recent: SDR post-duck stutter + re-duck inhibit (2026-03-23), MCP sdr_tune wrong payload keys (2026-03-23),
SDR2 permanently ducked by D75 noise (2026-03-24), D75 serial never connects: 3 layered bugs (2026-03-24),
D75 tone/shift/offset wrong FO indices: 4 layered bugs (2026-03-24),
D75 `connected` TCP-only status (2026-03-26), D75 `_recv_line` EOF no reconnect (2026-03-26),
D75 `close()` killed poll thread reconnect (2026-03-26), D75 reconnect crash: missing import (2026-03-26),
D75 SM poll 0.5s killed BT RFCOMM (2026-03-26), D75 ME→FO lockout field shift (2026-03-26),
D75 ME field[2] TX freq vs offset (2026-03-26), D75 TX audio silent: SCO 48-byte frames (2026-03-26),
D75 TX stutter: paced TX thread (2026-03-26), D75 PTT blocked audio thread (2026-03-26),
D75 playback JS newline syntax error (2026-03-26), config file damage from replace_all (2026-03-26).

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt is NOT committed** — repo is PUBLIC; config is in .gitignore
- Config file overrides code defaults — changing defaults in code has no effect if config has the old value
- Pre-TX announcement delay wanted for ALL radios (not just relay/AIOC)
- Instrument the code rather than guess at bugs

## Claude Access Methods (see feedback_access_methods.md)
Claude runs on the same machine as the gateway. Available methods: MCP tools (preferred for control), direct HTTP to port 8080, filesystem read/edit, shell commands.

## Machine Setup — user-optiplex3020 (Arch Linux)
- Cloned to `/home/user/Downloads/radio-gateway`; Git user: ukbodypilot / robin.pengelly@gmail.com; token in remote URL
- Arch Linux (EndeavourOS), XFCE4, Python 3.14, sudo password: `user`
- Relay USB: `2-1.3` → `/dev/relay_radio`; FTDI CAT cable: `2-1.1` → `/dev/ttyUSB1`
- `:0` — User desktop (VNC 5900, xrdp 3389) — **Do NOT touch VNC/xrdp config**
- KV4P HT: `/dev/kv4p` → ttyUSB0; TX_RADIO = kv4p; Telegram bot: @radio_gateway_bot, chat_id=6538333604
- `claude-desktop-appimage` (replaced `claude-desktop-bin`)
