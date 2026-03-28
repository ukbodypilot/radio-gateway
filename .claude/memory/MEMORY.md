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
- **CRITICAL:** WirePlumber null-sinks need `monitor.passthrough = true` or monitor output is silence

## SDR Control Page (v1.5.0) — RTLSDR-Airband + SoapySDR + RSPduo Dual Tuner
- **`RTLAirbandManager` class** (~300 lines): manages rtl_airband lifecycle
- **Audio chain:** RSPduo Tuner → SoapySDR → rtl_airband → PulseAudio → sdr_capture PipeWire sink → gateway
- **RSPduo Master/Slave:** SDR1 `rspduo_mode=4` (Master), SDR2 `rspduo_mode=8` (Slave). Start order critical.
- **Plugin:** fventuri `dual-tuner-submodes` branch — pin `IgnorePkg = soapysdrplay3-git` in `/etc/pacman.conf`

## SDR Click Suppressor (2026-03-26)
- Detects sample-to-sample jumps >800, interpolates over 4-sample window
- Runs after audio boost, before HPF/LPF/notch in SDR get_audio

## ADS-B Aircraft Tracking (2026-03-21)
- RTL2838/R820T USB dongle; dump1090-fa + lighttpd on port 30080; fr24feed → FlightRadar24
- Gateway reverse proxy: `/adsb/*` → `http://127.0.0.1:{ADSB_PORT}`
- **Dark mode CSS injected**, NEXRAD enabled, centered on Santa Ana CA, US mil layers

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — TCP, length-prefixed PCM, `ptt_control=True`, `priority=0`
- **CRITICAL:** Send at real-time rate (one chunk per tick) — queue maxsize=16; flooding drops 90% of audio

## Browser Microphone PTT (2026-03-12)
- `WebMicSource` class: browser mic via WebSocket `/ws_mic`, routes to radio TX
- **CRITICAL:** AIOC GPIO PTT does NOT key this user's radio — PTT wired via CAT serial only

## Room Monitor (2026-03-26)
- `WebMonitorSource` class: `ptt_control=False`, `priority=5` — feeds mixer without keying radio
- **Browser:** getUserMedia with echoCancellation/noiseSuppression/autoGainControl disabled
- Gain 1x-50x, client-side VAD (-60 to -20 dB threshold)
- Wake Lock API + silent audio loop to prevent tab suspension
- WebSocket endpoint: `/ws_monitor`; config: `ENABLE_WEB_MONITOR`
- **Android APK:** `tools/room-monitor-app/` (Kotlin), `tools/room-monitor.apk` (built)
  - Foreground service with UNPROCESSED mic source, partial wake lock
  - Auto-converts pasted `https://` → `wss://` and `http://` → `ws://`
  - Built with Android SDK cmdline-tools + JDK 17
- `/monitor-apk` route serves the APK for download

## Web Configuration UI & Live Dashboard
- `WebConfigServer` class: built-in HTTP server (Python `http.server`, no Flask)
- Pages: `/` shell, `/dashboard`, `/controls`, `/monitor`, `/sdr`, `/radio` (TH-9800), `/d75` (TH-D75), `/aircraft`, `/recordings`, `/logs`
- **Shell page (`/`):** nav bar + MP3/PCM controls (inline) + audio level bars (always visible) + iframe
- **Audio bars:** RX, TX, KV4P, D75, SDR1, SDR2, SV, AN, SP, MON (purple) — fixed 190px width, 10px tracks
- **Controls page (`/controls`):** all control groups moved from dashboard (2026-03-26)
- **Monitor page (`/monitor`):** browser-based room mic monitoring, `/ws_monitor` WebSocket (no PTT)
- **Dashboard:** status info + Broadcastify status panel (uptime/sent/rate/RTT/health/PID) + Telegram + USB/IP
- **Page titles removed** — implicit from nav bar active underline; gateway name in System Status block
- **Nav font 0.8em**, MP3/PCM inline with nav; footer action bar removed
- **CF tunnel URL:** shown as short clickable link in System Status IPs row
- **D75 page:** no-cache header to prevent stale JS
- **D75/KV4P processing buttons (2026-03-25):** Gate/HPF/LPF/Notch per source, with live highlighting

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- **CRITICAL: DISPLAY_TEXT vfo_byte** — must use vfo_byte from packet, NOT stale `_channel_vfo`
- **Auto serial connect (2026-03-21):** On startup, sends `!serial disconnect` then `!serial connect`

## TH-D75 Bluetooth Radio (2026-03-24, updated 2026-03-26)
- `D75CATClient` class in `cat_client.py`; remote proxy: `scripts/remote_bt_proxy.py` on 192.168.2.134
- Proxy ports: 9750 (CAT text), 9751 (raw 8kHz PCM audio)
- **CRITICAL — btstart non-blocking:** proxy returns "btstart initiated" immediately; BT connects in background
- **`connected` status:** requires BOTH TCP AND serial_connected (was TCP-only)
- **Reconnect:** `_disconnect_for_reconnect()` avoids `close()` killing poll thread; btstart retries every 15s
- **PTT:** fire-and-forget `_sock.sendall()` — must NOT use `_send_cmd` (blocks audio thread)
- **Channel load via FO:** ME fields with lockout field skip + TX freq→offset conversion

### D75 FO Command — 21-field format (LA3QMA / Hamlib thd74.c verified)
- **Field map (0-indexed):** `[0]=band [1]=rxfreq [2]=offset_hz ... [8]=tone [9]=ctcss [10]=dcs ... [13]=shift [14]=tone_idx [15]=ctcss_idx [16]=dcs_idx [17]=cross_type [18]=urcall [19]=dsql_type [20]=dsql_code`
- **42-tone CTCSS list** (not 39); **FO SET:** no echo response — use async readback

### D75 TX Audio via BT SCO (2026-03-26)
- **Path:** gateway → downsample 48k→8k → TCP 9751 → proxy → SCO → radio
- **CRITICAL:** SCO is SEQPACKET with 48-byte frames; dedicated `_tx_loop` sends every 3ms

### D75 BT Proxy Reliability (2026-03-26)
- **SM poll interval:** 3s (was 0.5s — killed BT). Exponential backoff after 3 failures, up to 30s
- **Init:** only ID/FV/AE on connect; heavy queries deferred to stream loop

## Audio Mixer — Duck State Machine (aioc_vs_sdrs)
- **Broadcast-style additive mixing** with soft tanh limiter (replaced ratio=0.5)
  - Knee at 24000, max 32767, single source full volume, overlapping sources compress peaks
  - All 3 `_mix_audio_streams` call sites updated
- `aioc_ducks_sdrs = ds['is_ducked'] or in_padding` — SDR suppressed while ducked or in transition padding
- **Re-duck inhibit** (`REDUCK_INHIBIT_TIME = 2.0s`): blocks new duck-out for 2s after duck-in
- **SDR_SIGNAL_THRESHOLD = -45.0 dBFS**: D75 idle noise at ~-65 dBFS was permanently ducking SDRs
- **D75 starts muted by default**: prevents D75 background noise from ducking SDRs on startup

## TX Talkback (2026-03-26)
- **Config:** `TX_TALKBACK = False` (default off) — in `[ptt]` section
- When off, TX audio goes ONLY to radio; local outputs receive concurrent RX audio instead

## PTT Methods
- `PTT_METHOD`: `aioc` (default), `relay`, or `software`
- **CRITICAL:** Always use `!ptt on`/`!ptt off` (explicit state), never bare `!ptt` (blind toggle)

## AIOC PTT — RTS Relay Coordination (CRITICAL, 2026-03-15)
- **Sequence:** pause drain → RTS Radio Controlled → key AIOC → [TX] → unkey AIOC → RTS USB Controlled → resume drain

## KV4P HT Radio (added 2026-03-19)
- `KV4PAudioSource` class: CP2102 USB-serial, kv4p-ht-python package, Opus codec
- **CRITICAL:** DRA818 38 tones (no 69.3 Hz) — off-by-one CTCSS with TH-9800's 39-tone list
- **KV4P logging gated behind VERBOSE_LOGGING** (2026-03-26)

## Gateway Link v1.7.0 — Multi-Endpoint Duplex Audio Protocol (2026-03-27)
- **File:** `gateway_link.py` — protocol, server, client, plugin base, AudioPlugin, AIOCPlugin
- **Endpoint:** `tools/link_endpoint.py` — standalone, no gateway deps, zero-config with mDNS
- **Protocol:** TCP framed `[type 1B][length 2B][payload]` — AUDIO/COMMAND/STATUS/REGISTER/ACK
- **Plugin arch:** `RadioPlugin` base class → subclass per hardware type
- **Multi-endpoint (v1.7.0):** N simultaneous connections, dict keyed by endpoint name
  - Dynamic `LinkAudioSource` creation/destruction per endpoint
  - Per-endpoint controls on `/controls` page (PTT button, RX/TX level bars, gain sliders, mute buttons)
  - Per-endpoint settings persisted to `~/.config/radio-gateway/link_endpoints.json`
- **AIOCPlugin:** finds AIOC device via `/proc/asound/cards` (not PyAudio)
- **RX/TX gain:** dB controls (-10 to +10), persisted per endpoint
- **RX/TX mute:** gateway-side, per-endpoint
- **VAD-gated level bars** on controls page
- **Command language:** `ptt`, `rx_gain`, `tx_gain`, `status` + ACK responses
- **PTT safety timeout:** 60s auto-unkey
- **Heartbeat:** bidirectional 5s interval, dead peer detection at 15s
- **Cable-pull detection:** 10s socket timeout on both sides
- **mDNS auto-discovery:** gateway publishes `_radiogateway._tcp`, endpoint discovers via `avahi-browse`
- **Zero-config:** `python3 link_endpoint.py --name pi-aioc --plugin aioc`
- **Config:** ENABLE_GATEWAY_LINK (default false), LINK_PORT=9700
- **Integration:** LinkAudioSource in mixer, LINK bar (orange), status dict fields
- **CRITICAL — /linkcmd bug:** missing `return` in handler caused config wipes on Save
- **CRITICAL — _CONFIG_LAYOUT:** config page must include ALL sections or Save wipes unlisted ones
- **Client deadlock fix:** `_send` calling `_close` while holding lock
- **Reader cleanup:** only calls `on_disconnect` if it owns the entry
- **See:** `docs/gateway_link.md` for full architecture and roadmap; `CHANGELOG.md` for release history

## MCP Server (gateway_mcp.py) — AI Control Interface (2026-03-25)
- **File:** `gateway_mcp.py` — stdio MCP server; 31 tools; talks to gateway HTTP API on port 8080
- **CRITICAL:** MCP server is a Claude Code child process — restarting gateway does NOT restart MCP

### /mixer HTTP Endpoint (2026-03-25)
- **POST `/mixer`** — 7 actions: status, mute/unmute/toggle, volume, duck, boost, flag, processing
- **Sources:** global, tx, rx, sdr1, sdr2, d75, kv4p, remote, announce, speaker

## Telegram Bot — Phone Control (2026-03-24)
- **File:** `tools/telegram_bot.py` — stdlib only; service: `tools/telegram-bot.service`
- Voice notes: ffmpeg → PCM → port 9601 (ANNIN) at real-time rate → radio TX with auto PTT

## Smart Announcements — claude-cli backend (2026-03-24)
- `SmartAnnouncementManager` in `smart_announce.py`: `claude -p` subprocess, 120s timeout

## Email Notification
- Includes Gateway, Config, Monitor, Monitor App (LAN ws://), Monitor App (internet wss://) URLs
- Linkifier regex matches `https?`, `wss?` protocols

## Systemd Service & Process Management
- **Service:** `radio-gateway.service` — `KillMode=control-group`, `TimeoutStopSec=15`
- **CRITICAL:** Always restart gateway via start.sh, never `python3 radio_gateway.py` directly

## Known Bugs Fixed (details in bugs.md)
Key recent: D75 serial/reconnect/PTT/SCO/ME→FO (6 bugs, 2026-03-26), config damage from replace_all (2026-03-26),
D75 playback JS newline syntax (2026-03-26), MON bar float % and stuck level after disconnect (2026-03-26),
ADS-B map broken: stray `, false)` in layers.js europe.push calls (2026-03-26),
Missing D75CATClient/D75AudioSource imports in web_server.py (2026-03-26).

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt is NOT committed** — repo is PUBLIC; config is in .gitignore
- Config file overrides code defaults — changing defaults in code has no effect if config has the old value
- **Code defaults updated (2026-03-26):** ENABLE_ADSB, ENABLE_DDNS, ENABLE_USBIP, ENABLE_SPEAKER_OUTPUT match production
- Pre-TX announcement delay wanted for ALL radios (not just relay/AIOC)
- Instrument the code rather than guess at bugs
- README should be concise with punchy summary up top, detail in collapsible sections
- Show me the list before asking me to review it

## Claude Access Methods (see feedback_access_methods.md)
Claude runs on the same machine as the gateway. Available methods: MCP tools (preferred for control), direct HTTP to port 8080, filesystem read/edit, shell commands.

## Machine Setup — user-optiplex3020 (Arch Linux)
- Cloned to `/home/user/Downloads/radio-gateway`; Git user: ukbodypilot / robin.pengelly@gmail.com
- Arch Linux (EndeavourOS), XFCE4, Python 3.14, sudo password: `user`
- Relay USB: `2-1.3` → `/dev/relay_radio`; FTDI CAT cable: `2-1.1` → `/dev/ttyUSB1`
- `:0` — User desktop (VNC 5900, xrdp 3389) — **Do NOT touch VNC/xrdp config**
- KV4P HT: `/dev/kv4p` → ttyUSB0; TX_RADIO = kv4p; Telegram bot: @radio_gateway_bot
- `claude-desktop-appimage` (replaced `claude-desktop-bin`)
- **Android SDK:** `ANDROID_HOME=/home/user/Android/Sdk`, `JAVA_HOME=/usr/lib/jvm/java-17-openjdk`
