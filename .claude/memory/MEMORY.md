# Radio Gateway — Project Memory

## Update this file
Update MEMORY.md and detail files at the end of every session and whenever a significant bug or pattern is discovered. Keep this file under 200 lines.

## Project Overview
Radio-to-Mumble gateway. AIOC USB device handles radio RX/TX audio and PTT. Optional SDR input via PipeWire virtual sink or ALSA loopback. Optional Broadcastify streaming via DarkIce. Python 3, runs on Raspberry Pi, Debian amd64, and Arch Linux.

**Main file:** `radio_gateway.py` (~15000+ lines)
**Installer:** `scripts/install.sh` (13 steps, targets Debian/Ubuntu/RPi/Arch Linux)
**Config:** `gateway_config.txt` (INI format with `[section]` headers, copied from `examples/` on install)
**Start script:** `start.sh` (11 steps: kill procs, Mumble GUI, TH-9800 CAT, Claude Code, CPU governor, loopback, AIOC USB reset, pipe, DarkIce, FFmpeg, gateway w/nice -10)
**Windows client:** `windows_audio_client.py` (server: send audio, client: receive audio, `m` to switch)

## SDR Input — PipeWire (preferred) or ALSA Loopback
- **PipeWire:** `SDR_DEVICE_NAME = pw:sdr_capture` — reads from virtual sink monitor via `parec` (native PulseAudio, replaced FFmpeg for lower latency)
- `PipeWireSDRSource` class: auto-creates sink via `pw-cli` if missing at startup
- WirePlumber persistence: `~/.config/wireplumber/wireplumber.conf.d/90-sdr-capture-sink.conf`
- Creates `sdr_capture` and `sdr_capture2` sinks; installer deploys this config (step 12)
- **CRITICAL:** WirePlumber null-sinks need `monitor.passthrough = true` or monitor output is silence
- **ALSA loopback:** `SDR_DEVICE_NAME = hw:4,1` — traditional method, 200ms blob delivery

## SDR Control Page (v1.5.0) — RTLSDR-Airband + SoapySDR + RSPduo Dual Tuner
- **`RTLAirbandManager` class** (~300 lines, before WebConfigServer): manages rtl_airband lifecycle
- **Audio chain (SDR1):** RSPduo Tuner 1 → SoapySDR → rtl_airband → PulseAudio → sdr_capture PipeWire sink → gateway
- **Audio chain (SDR2):** RSPduo Tuner 2 → SoapySDR → rtl_airband → PulseAudio → sdr_capture2 PipeWire sink → gateway
- **Web routes:** `/sdr` (control page), `/sdrstatus` (JSON, polled 1s), `/sdrcmd` (POST: tune/save/recall/delete/restart/stop)
- **Config files:** `/etc/rtl_airband/rspduo_gateway.conf` (SDR1), `/etc/rtl_airband/rspduo_gateway2.conf` (SDR2) — auto-generated
- **Settings persistence:** `sdr_channels.json` stores current settings + 10 channel slots; SDR2 settings (frequency2, modulation2, etc.) persisted in `current` block
- **Dependencies:** rtlsdr-airband-git (v5.1.6), soapysdr, soapysdrplay3 (fventuri dual-tuner branch), libsdrplay, sdrplay.service

### RSPduo Dual Tuner — Master/Slave Architecture (2026-03-23)
- **CRITICAL:** Uses Master/Slave API, NOT "Dual Tuner Independent RX" (mode=2)
  - Mode=2 (`rspduo_dual_tuner_independent_rx=true`) locks the device — second process cannot open it
  - Master/Slave is the only multi-process approach that works
- **SDR1 (Master):** `driver=sdrplay,rspduo_mode=4` → Tuner 1 → `sdr_capture`
- **SDR2 (Slave):** `driver=sdrplay,rspduo_mode=8` → Tuner 2 → `sdr_capture2`
- **Start order is critical:** SDR1 Master MUST be streaming BEFORE SDR2 Slave starts (Slave device only visible once Master is open)
  - `_restart_process()` starts SDR1, verifies alive, waits 3s, then starts SDR2
- **Plugin:** fventuri `dual-tuner-submodes` branch of SoapySDRPlay3
  - Old AUR build: `/usr/lib/SoapySDR/modules0.8/libsdrPlaySupport.so` (backed up as `.so.old`)
  - New build: `/opt/soapy-fventuri/lib/SoapySDR/modules0.8/libsdrPlaySupport.so`
  - **CRITICAL — AUR update risk:** `soapysdrplay3-git` pacman package will OVERWRITE the new plugin on next update
    - Either pin the package version (`IgnorePkg = soapysdrplay3-git` in `/etc/pacman.conf`) or manually reinstall from /opt/soapy-fventuri after each update
    - If plugin is accidentally overwritten, SDR2 will fail and SDR1 will revert to single-tuner mode
- **Sample rate:** Max 2 MSps per tuner in Master/Slave mode; `_write_config()` caps at 2.0
- **WirePlumber:** `90-sdr-capture-sink.conf` must have comma between the two object entries (WP 0.5.13 quirk)
- **RTLAirbandManager new methods:** `apply_settings_sdr2(**kwargs)` — update SDR2 frequency/modulation
- **New SDR2 settings in `_SETTING_KEYS`:** `frequency2` (default 462.550), `modulation2`, `squelch_threshold2`, `continuous2`

## ADS-B Aircraft Tracking (2026-03-21)
- **Hardware:** RTL2838/R820T USB SDR dongle — separate from RSPduo used by rtl_airband, no hardware conflict
- **dump1090-fa:** FlightAware ADS-B decoder; v7+ has NO built-in HTTP server, writes JSON to `/run/dump1090-fa/`
- **lighttpd:** serves dump1090-fa web UI on port 30080 (avoids conflict with gateway on 8080)
- **fr24feed** (Arch: `flightradar24` AUR): reads Beast data from dump1090 port 30002, uploads to FlightRadar24
- **Gateway reverse proxy:** `/adsb/*` → `http://127.0.0.1:{ADSB_PORT}` via `urllib.request` — single port via Cloudflare tunnel
- **Routes:** `/aircraft` (iframe wrapper page), `/adsb/*` (reverse proxy), `/adsbstatus` (JSON: services/aircraft/msg rate)
- **Dashboard panel:** `#adsb-panel` with service health dots + aircraft count/messages/rate, polled 3s
- **Config:** `ENABLE_ADSB` (default False), `ADSB_PORT` (default 30080)
- **Nav link:** `ADS-B` appears between SDR and Recordings when `ENABLE_ADSB=true`
- **Installer step 7b (Arch):** clone dump1090 from GitHub, strip `-Werror` from Makefile (`sed`), build; `flightradar24` AUR with `--nodeps --nodeps` (depends on generic `dump1090`, no pacman provider)
- **Installer step 7b (Debian/Pi):** FlightAware apt repo for dump1090-fa, FR24 apt repo for fr24feed, patch `/etc/default/dump1090-fa` for port 30080
- **Makefile quirk:** `-Werror` + `-Wunterminated-string-initialization` on modern GCC causes build failure — fix: `sed -i 's/-Werror //'`
- **Signup:** `sudo fr24feed --signup --config-file=/etc/fr24feed.ini` (interactive, cannot be automated)
- **Config sections:** reordered alphabetically by title (33 sections, Advanced/Diagnostics last)

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — listens on 9601, inbound TCP, length-prefixed PCM
- `ptt_control=True`, `priority=0` — mixer routes audio to radio TX and activates PTT
- Audio-gated PTT: discards silence below `ANNOUNCE_INPUT_THRESHOLD` (-45 dBFS)

## Browser Microphone PTT (2026-03-12)
- `WebMicSource` class: receives browser mic audio via WebSocket `/ws_mic`, routes to radio TX
- **CRITICAL:** AIOC GPIO PTT (`PTT_METHOD=aioc`) does NOT key this user's radio — PTT is wired via CAT serial cable only. WebMic uses CAT `!ptt` directly.
- Browser: `getUserMedia`, `ScriptProcessorNode` (buffer=2048, must be power of 2), Float32→Int16 PCM conversion
- Config: `ENABLE_WEB_MIC` (default True), `WEB_MIC_VOLUME` (default 25.0, raw multiplier)
- Single client only (409 Conflict if already connected); queue: 64 slots, drop-oldest on overflow

## Web Configuration UI & Live Dashboard
- `WebConfigServer` class: built-in HTTP server (Python `http.server`, no Flask)
- Pages: `/` shell, `/dashboard`, `/sdr`, `/radio` (TH-9800), `/d75` (TH-D75), `/aircraft` (ADS-B), `/recordings`, `/logs`
- Config: `ENABLE_WEB_CONFIG`, `WEB_CONFIG_PORT` (default 8080), `WEB_CONFIG_PASSWORD`
- **Shell/iframe structure (2026-03-18):** `/` serves persistent shell page with nav bar + iframe (`name="content"`). Shell caches across page loads (audio player, WebSocket PCM stay alive).
- **Shell nav bar:** Dashboard | TH-9800 | TH-D75 | SDR | [ADS-B] | Recordings | Config | Logs. TH-9800/D75 greyed when disabled. ADS-B visible only when `ENABLE_ADSB=true`.
- **Action bar (bottom):** Email Status / Restart / Exit buttons in fixed bar at bottom of shell page.
- Dashboard layout: Listen box, Status (audio bars/info/timers), System Status, Controls (Mute/Radio/SDR/Audio), bottom row (Playback/Smart Announce/Broadcastify/PTT/TTS/System/ADS-B panel)
- **System Status box:** `/sysinfo` endpoint (2s poll), CPU/load/RAM/swap/disk/net/TCP/temps/IPs
- **Soundboard:** auto-fills empty slots 1-9 with random Mixkit sound effects (~750 curated pool)

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- **CRITICAL: DISPLAY_TEXT vfo_byte** — must use vfo_byte from packet (0x40/0x60=LEFT, 0xC0/0xE0=RIGHT), NOT stale `_channel_vfo`. Fixed 2026-03-13.
- `_drain_paused` during set_channel, web commands, and RTS changes (prevents background drain race)
- `_drain()` must use single `_recv_line(0.1)` — loop version breaks all packet parsing
- **Auto serial connect (2026-03-21):** On startup, always sends `!serial disconnect` then `!serial connect` (unconditional cycle). Calls `set_rts(True)` after success.

## Auto RTS for Playback, TTS & Announcements (2026-03-13)
- Playback (keys 1-9, 0), TTS, Smart Announce all auto-set RTS to Radio Controlled before TX, restore after
- **CRITICAL:** RTS save/restore is SKIPPED when `PTT_METHOD=software` — causes VFO switching artifacts
- Display refresh (VFO dial press+release both sides) after RTS restore, also with drain paused

## Systemd Service & Process Management
- **Service:** `radio-gateway.service` — `KillMode=control-group`, `TimeoutStopSec=15`
- **CRITICAL:** Always restart gateway via start.sh, never `python3 radio_gateway.py` directly
- **Save & Restart** in web config launches `start.sh` via detached subprocess (`start_new_session=True`)
- Gateway `q` key uses `os.execv` (replaces process in-place, same PID)

## PTT Methods
- `PTT_METHOD`: `aioc` (default), `relay`, or `software`
- **CRITICAL:** Always use `!ptt on`/`!ptt off` (explicit state), never bare `!ptt` (blind toggle causes state inversion)
- Software PTT refuses to key if radio hasn't sent data in >5s (radio powered off)
- `_software_ptt_on` tracker removed (2026-03-21) — redundant after explicit on/off

## AIOC PTT — RTS Relay Coordination (CRITICAL, 2026-03-15)
- **RTS=USB Controlled:** serial → USB dongle (CAT works). **RTS=Radio Controlled:** serial → front panel (CAT blocked)
- **AIOC PTT REQUIRES Radio Controlled** — PTT fails without it due to mic wiring
- **Sequence:** pause drain → RTS Radio Controlled → key AIOC → [TX] → unkey AIOC → RTS USB Controlled → resume drain

## Smart Announcements (Modular AI Backend)
- `SmartAnnouncementManager`: scheduled AI-powered spoken announcements
- Backends: `google-scrape`, `claude-scrape`, `duckduckgo` (default), `claude` (API), `gemini`
- Claude-scrape runs Firefox on Xvfb `:99` (VNC port 5999) — does NOT touch desktop `:0`

## KV4P HT Radio (added 2026-03-19)
- `KV4PAudioSource` class: CP2102 USB-serial (10c4:ea60), kv4p-ht-python package, Opus codec
- Config: `KV4P_PORT = /dev/kv4p`, `ENABLE_KV4P`, `TX_RADIO = kv4p`
- **TX audio fix:** `_tx_buf` accumulation carries 960-byte remainder across ticks (else 20% audio dropout)
- **CRITICAL:** DRA818 uses 38 tones (no 69.3 Hz) — using TH-9800's 39-tone list causes off-by-one CTCSS errors
- **CRITICAL:** PTT_METHOD=aioc does NOT key KV4P — KV4P uses its own serial PTT (`_ptt_kv4p`)

## Audio Processing
- `AudioProcessor` class: HPF → LPF → Notch → Noise Gate filter chain, independent state per source
- **CRITICAL:** Requires `scipy` — filters silently return unmodified audio if scipy is missing
- HPF defaults to ON for AIOC radio audio

## Audio Mixer — Duck State Machine (aioc_vs_sdrs) (2026-03-23)
- `aioc_ducks_sdrs = ds['is_ducked'] or in_padding` — SDR suppressed while is_ducked=True or in transition padding
  - **Old gate removed:** was `and (non_ptt_audio is not None or _aioc_blob_recent)` — caused post-duck stutter: AIOC tail blobs would un-duck SDR briefly then re-duck → choppy
- **Re-duck inhibit** (`REDUCK_INHIBIT_TIME = 2.0s`): after duck-in fires, blocks new duck-out for 2s
  - Prevents AIOC tail audio (VoIP echo, tones, trailing blobs) from restarting a new duck+1s-padding cycle
  - `_duck_in_time` stored in duck state dict; `_reduck_inhibit` flag computed each tick; `ri` in trace
- **Fade-in at duck release**: `sdr_prev_included[name]` reset to False at duck-in for all SDRs
  - Without reset: first chunk after duck plays at full volume (prev_included=True skips fade-in) → click
- **1s hold** on `other_audio_active` (only when is_ducked=True): bridges AIOC inter-blob gaps (400–800ms)
- **Trace instrumentation:** `fi`=fade-in fired, `fo`=fade-out fired, `ri`=reduck-inhibit; DUCK RELEASE EVENTS summary section; anomaly flags for missing fade-in and `s1_q > 8` (queue not drained during duck)

## Other Features
- **Cloudflare Tunnel:** URL cached in `/tmp/cloudflare_tunnel_url`; existing process adopted on restart; retry 3× on code 1 exit
- **Email:** Gmail SMTP on startup/`@` key; includes tunnel URL + LAN link + status dump + last 200 log lines
- **Web UI fetch pileup fix:** all polling functions have in-flight guards (prevents 6-connection browser limit exhaustion)
- **Web UI toast notifications:** `gateway.notify()` → 20-entry ring buffer → color-coded popups (auto-dismiss 8s)
- **Edge TTS:** `TTS_ENGINE = edge` — Microsoft Neural voices; 9 voices; requires `edge-tts` pip package

## MCP Server (gateway_mcp.py) — AI Control Interface (2026-03-23)
- **File:** `gateway_mcp.py` — stdio MCP server; 19 tools; talks to gateway HTTP API on port 8080
- **Config:** `.claude/settings.json` (project-level) — Claude Code auto-loads it in this directory
- **Transport:** stdio (local Claude Code); future: SSE/HTTP via Cloudflare tunnel
- **Reads:** `gateway_config.txt` at startup to get WEB_CONFIG_PORT and WEB_CONFIG_PASSWORD
- **Tools:** gateway_status, sdr_status, cat_status, system_info, sdr_tune, sdr_restart, sdr_stop, radio_ptt, radio_tts, radio_cw, radio_ai_announce, radio_set_tx, radio_get_tx, recordings_list, recordings_delete, gateway_logs, gateway_key, automation_trigger, audio_trace_toggle
- **Install:** `pip install --break-system-packages mcp` (mcp v1.26.0 installed 2026-03-23)
- **Self-describing:** each tool has full docstring with args — AI can use cold without docs

## Planned Next Features
- [USBIP USB over TCP](project_usbip.md) — `USBIPManager` class to share USB devices (BT dongle, RTL-SDR) from a remote Pi to the gateway over TCP port 3240
- **MCP remote access** — expose gateway_mcp.py over SSE/HTTP via Cloudflare tunnel + OpenAPI spec for any AI (current impl is local stdio only)

## Known Bugs Fixed (details in bugs.md)
Key recent: DISPLAY_TEXT VFO misattribution (2026-03-13), RTS change corrupts display (2026-03-13),
WebSocket PCM double-push/latency, KV4P TX 20% audio dropout (2026-03-19), KV4P CTCSS DRA818 off-by-one (2026-03-19),
TH9800 PTT blind toggle state inversion (2026-03-21), setup_radio before serial connect (2026-03-21),
SDR manager circular import (2026-03-21), f-string double-brace in `_write_config_sdr2()` continuous line (2026-03-23),
SDR autostart crash `'RTLAirbandManager' has no attribute 'channels'` after channel memory removal (2026-03-23),
SDR post-duck stutter: aioc_ducks_sdrs gate removed + re-duck inhibit (2s) + fade-in reset at duck-in (2026-03-23).

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt is NOT committed** — repo is PUBLIC; config is in .gitignore
- Config file overrides code defaults — changing defaults in code has no effect if config has the old value

## Machine Setup — user-optiplex3020 (Arch Linux)
- Cloned to `/home/user/Downloads/radio-gateway`; Git user: ukbodypilot / robin.pengelly@gmail.com; token in remote URL
- Arch Linux (EndeavourOS), XFCE4, Python 3.14, sudo password: `user`
- Relay USB: `2-1.3` → `/dev/relay_radio`; FTDI CAT cable: `2-1.1` → `/dev/ttyUSB1`
- `:0` — User desktop (VNC 5900, xrdp 3389); `:99` — Xvfb (VNC 5999) for claude-scrape
- **Do NOT touch `:0` VNC/xrdp config** — user relies on them for remote access
