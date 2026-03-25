# Radio Gateway — Project Memory

## Update this file
Update MEMORY.md and detail files at the end of every session and whenever a significant bug or pattern is discovered. Keep this file under 200 lines.

## Project Overview
Radio-to-Mumble gateway. AIOC USB device handles radio RX/TX audio and PTT. Optional SDR input via PipeWire virtual sink or ALSA loopback. Optional Broadcastify streaming via DarkIce. Python 3, runs on Raspberry Pi, Debian amd64, and Arch Linux.

**Main file:** `radio_gateway.py` (~15000+ lines)
**Installer:** `scripts/install.sh` (13 steps, targets Debian/Ubuntu/RPi/Arch Linux)
**Config:** `gateway_config.txt` (INI format with `[section]` headers, copied from `examples/` on install)
**Start script:** `start.sh` (11 steps: kill procs, Mumble GUI, TH-9800 CAT, Claude Code, CPU governor, loopback, AIOC USB reset, pipe, DarkIce, FFmpeg, gateway w/nice -10)
- **HEADLESS_MODE = true** (2026-03-25): skips Mumble GUI launch in start.sh — user no longer uses GUI client on gateway machine
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
- **Start order is critical:** SDR1 Master MUST be streaming BEFORE SDR2 Slave starts
- **Plugin:** fventuri `dual-tuner-submodes` branch of SoapySDRPlay3
  - **CRITICAL — AUR update risk:** `soapysdrplay3-git` pacman will OVERWRITE fventuri plugin on update
  - Pin: `IgnorePkg = soapysdrplay3-git` in `/etc/pacman.conf`
- **Sample rate:** Max 2 MSps per tuner in Master/Slave mode

## ADS-B Aircraft Tracking (2026-03-21)
- **Hardware:** RTL2838/R820T USB SDR dongle — separate from RSPduo, no hardware conflict
- **dump1090-fa + lighttpd** on port 30080; **fr24feed** uploads to FlightRadar24
- **Gateway reverse proxy:** `/adsb/*` → `http://127.0.0.1:{ADSB_PORT}`
- **Config:** `ENABLE_ADSB` (default False), `ADSB_PORT` (default 30080)

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — listens on 9601, inbound TCP, length-prefixed PCM
- `ptt_control=True`, `priority=0` — mixer routes audio to radio TX and activates PTT
- Audio-gated PTT: discards silence below `ANNOUNCE_INPUT_THRESHOLD` (-45 dBFS)
- **CRITICAL:** Send at real-time rate (one chunk per tick interval) — queue maxsize=16; flooding it drops 90% of audio
- `audio_level` resets to 0 when queue drains (fixed 2026-03-24 — was stuck at last value)

## Browser Microphone PTT (2026-03-12)
- `WebMicSource` class: receives browser mic audio via WebSocket `/ws_mic`, routes to radio TX
- **CRITICAL:** AIOC GPIO PTT (`PTT_METHOD=aioc`) does NOT key this user's radio — PTT is wired via CAT serial cable only. WebMic uses CAT `!ptt` directly.
- Config: `ENABLE_WEB_MIC` (default True), `WEB_MIC_VOLUME` (default 25.0, raw multiplier)

## Web Configuration UI & Live Dashboard
- `WebConfigServer` class: built-in HTTP server (Python `http.server`, no Flask)
- Pages: `/` shell, `/dashboard`, `/sdr`, `/radio` (TH-9800), `/d75` (TH-D75), `/aircraft` (ADS-B), `/recordings`, `/logs`
- Config: `ENABLE_WEB_CONFIG`, `WEB_CONFIG_PORT` (default 8080), `WEB_CONFIG_PASSWORD`
- Dashboard layout: Listen box, Status (audio bars/info/timers), System Status, Controls, bottom row (Playback/Smart Announce/Broadcastify/PTT/TTS/System/ADS-B/Telegram panels)
- **D75/KV4P processing buttons (2026-03-25):** Gate/HPF/LPF/Notch per source, with live highlighting
- **Telegram panel:** "Open" button launches xfce4-terminal attached to Claude tmux session via `/open_tmux` endpoint

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- **CRITICAL: DISPLAY_TEXT vfo_byte** — must use vfo_byte from packet (0x40/0x60=LEFT, 0xC0/0xE0=RIGHT), NOT stale `_channel_vfo`. Fixed 2026-03-13.
- **Auto serial connect (2026-03-21):** On startup, always sends `!serial disconnect` then `!serial connect`

## TH-D75 Bluetooth Radio (2026-03-24)
- `D75CATClient` class in `cat_client.py`; remote proxy: `scripts/remote_bt_proxy.py` on 192.168.2.134
- Proxy ports: 9750 (CAT text), 9751 (raw 8kHz PCM audio)
- **CRITICAL — btstart is non-blocking:** proxy returns "btstart initiated" immediately; BT connects in background thread. `poll_state()` clears `_btstart_in_progress` when `serial_connected=True` arrives in status.
- **CRITICAL — serial_connected field:** proxy `to_dict()` includes `serial_connected: self._connected` — don't use `model_id` (empty if `ID` query times out on init).
- **CRITICAL — poll thread self-join:** `close()` checks `self._poll_thread is not threading.current_thread()` before joining — poll thread calls `close()` on TCP drop, would crash otherwise.
- **Web UI:** `/d75` page; status checklist shows "Connecting..." (orange) while `_btstart_in_progress=True`; BT Start button hidden during pending connect.
- **Channel load:** `d75GoChannel` checks `_d75LastStatus.dual_band/active_band` — switches active band via `BC` (not `DL 0`) in single-band mode to avoid forcing dual-band.
- **Up/Down buttons:** send `!cat UP` / `!cat DN` via passthrough.

### D75 FO Command — 21-field format (LA3QMA / Hamlib thd74.c verified, 2026-03-24)
- `radio_automation.py` `_tune_d75()` already uses correct indices — use as reference
- **CRITICAL — field map (0-indexed from comma split):**
  - `[0]=band  [1]=rxfreq  [2]=offset_hz  [3]=rxstep  [4]=txstep`
  - `[5]=mode  [6]=fine_mode  [7]=fine_step`
  - `[8]=tone  [9]=ctcss  [10]=dcs  [11]=cross  [12]=reverse  [13]=shift`
  - `[14]=tone_idx  [15]=ctcss_idx  [16]=dcs_idx`
  - `[17]=cross_type  [18]=urcall  [19]=dsql_type  [20]=dsql_code`
- **Mode values:** 0=FM, 1=DV, 2=AM, 3=LSB, 4=USB, 5=CW, 6=NFM, 7=DR, 8=WFM
- **42-tone CTCSS list** (not 39): includes 206.5, 229.1, 254.1
- **FO SET:** radio gives no echo response (send_raw returns None) — use async readback
- **Proxy no SSH:** 192.168.2.134 has no SSH; deploy via `git pull` from remote desktop

## Systemd Service & Process Management
- **Service:** `radio-gateway.service` — `KillMode=control-group`, `TimeoutStopSec=15`
- **CRITICAL:** Always restart gateway via start.sh, never `python3 radio_gateway.py` directly
- **Telegram bot service:** `telegram-bot.service` — installed by installer, enable manually after config

## PTT Methods
- `PTT_METHOD`: `aioc` (default), `relay`, or `software`
- **CRITICAL:** Always use `!ptt on`/`!ptt off` (explicit state), never bare `!ptt` (blind toggle causes state inversion)
- Software PTT refuses to key if radio hasn't sent data in >5s (radio powered off)

## AIOC PTT — RTS Relay Coordination (CRITICAL, 2026-03-15)
- **AIOC PTT REQUIRES Radio Controlled** — PTT fails without it due to mic wiring
- **Sequence:** pause drain → RTS Radio Controlled → key AIOC → [TX] → unkey AIOC → RTS USB Controlled → resume drain

## Smart Announcements (Claude CLI backend)
- `SmartAnnouncementManager` in `smart_announce.py`: scheduled AI-powered spoken announcements via `claude -p`
- No API key, no external dependencies — uses existing Claude Code auth (Max subscription)
- `_init_claude_cli()` finds binary; `_call_claude_cli()` runs subprocess with 120s timeout

## KV4P HT Radio (added 2026-03-19)
- `KV4PAudioSource` class: CP2102 USB-serial (10c4:ea60), kv4p-ht-python package, Opus codec
- Config: `KV4P_PORT = /dev/kv4p`, `ENABLE_KV4P`, `TX_RADIO = kv4p`
- **CRITICAL:** DRA818 uses 38 tones (no 69.3 Hz) — using TH-9800's 39-tone list causes off-by-one CTCSS errors
- **CRITICAL:** PTT_METHOD=aioc does NOT key KV4P — KV4P uses its own serial PTT (`_ptt_kv4p`)

## Audio Mixer — Duck State Machine (aioc_vs_sdrs)
- `aioc_ducks_sdrs = ds['is_ducked'] or in_padding` — SDR suppressed while ducked or in transition padding
- **Re-duck inhibit** (`REDUCK_INHIBIT_TIME = 2.0s`): blocks new duck-out for 2s after duck-in
- **1s hold** on `other_audio_active` (only when is_ducked=True): bridges AIOC inter-blob gaps
- **SDR_SIGNAL_THRESHOLD = -45.0 dBFS** (raised from -70 on 2026-03-24): D75 idle noise at ~-65 dBFS
  was keeping `other_audio_active=True` 100% → SDRs permanently ducked. -45 matches VAD threshold.
- **D75 starts muted by default** (fixed 2026-03-24): prevents D75 background noise from ducking SDRs on startup

## MCP Server (gateway_mcp.py) — AI Control Interface (2026-03-25)
- **File:** `gateway_mcp.py` — stdio MCP server; 31 tools; talks to gateway HTTP API on port 8080
- **Config:** `.mcp.json` (project root); `.claude/settings.json`: `enableAllProjectMcpServers: true`
- **CRITICAL:** MCP server is a Claude Code child process — restarting the gateway does NOT restart MCP. Use `/mcp` in Claude Code to reconnect.
- **Original 20 tools:** gateway_status, sdr_status, cat_status, system_info, sdr_tune, sdr_restart, sdr_stop, radio_ptt, radio_tts, radio_cw, radio_ai_announce, radio_set_tx, radio_get_tx, recordings_list, recordings_delete, gateway_logs, gateway_key, automation_trigger, audio_trace_toggle, telegram_reply
- **11 new tools (2026-03-25):** radio_frequency, d75_status, d75_command, d75_frequency, kv4p_status, kv4p_command, mixer_control, recording_playback (stub), config_read, telegram_status, process_control

### /mixer HTTP Endpoint (2026-03-25)
- **POST `/mixer`** — dedicated mixer control, works headless (no stdin needed)
- **7 actions:** status, mute/unmute/toggle, volume (absolute 0.1-3.0), duck (per-source), boost (d75/kv4p/remote 0-500%), flag (vad/agc/echo_cancel/rebroadcast), processing (gate/hpf/lpf/notch per source)
- **Sources:** global, tx, rx, sdr1, sdr2, d75, kv4p, remote, announce, speaker
- Replaces `/key` workaround for MCP — supports explicit set not just toggle

## Telegram Bot — Phone Control (2026-03-24)
- **File:** `tools/telegram_bot.py` — stdlib only (no pip); service: `tools/telegram-bot.service`
- **Text messages:** injected into `claude-gateway` tmux session → Claude Code (MCP) → `telegram_reply()` MCP tool
- **Voice notes / audio files:** downloaded → ffmpeg → PCM s16le 48kHz mono → port 9601 (ANNIN) at real-time rate → radio TX with auto PTT
- **CRITICAL:** Send audio at real-time rate (one 50ms chunk per 50ms) — ANNIN queue maxsize=16; flooding drops audio
- **tmux session:** `TELEGRAM_TMUX_SESSION = claude-gateway`; start.sh creates it automatically when `START_CLAUDE_CODE = true`
- **start.sh:** uses `tmux new-session -d -s claude-gateway` with `--dangerously-skip-permissions`; falls back to xfce4-terminal if no tmux
- **Config:** `ENABLE_TELEGRAM`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TMUX_SESSION`
- **Installer:** installs tmux + mcp packages; copies telegram-bot.service to systemd (not enabled — needs config first)
- **Dashboard panel:** bot status, tmux liveness, messages today, last in/out times, last message preview

## Smart Announcements — claude-cli backend (2026-03-24)
- **Replaced** all old backends (google-scrape, claude-scrape, duckduckgo+Ollama, claude API, gemini) with single `claude -p` subprocess
- `smart_announce.py`: ~300 lines (down from ~1300); no external dependencies; uses `claude CLI` auth (Max subscription, no API key)
- `_init_claude_cli()`: finds binary via shutil.which or `~/.local/bin/claude`
- `_call_claude_cli()`: runs `claude -p "<system>+<prompt>"`, 120s timeout, returns stdout
- Old class in `gateway_core.py` deleted (lines 742-2024); import from `smart_announce.py` now active
- Config keys removed: `SMART_ANNOUNCE_AI_BACKEND`, `SMART_ANNOUNCE_OLLAMA_*`, `SMART_ANNOUNCE_API_KEY`, `SMART_ANNOUNCE_GEMINI_API_KEY`
- `_client` replaced by `_claude_bin` throughout gateway_core.py and web_server.py

## Planned Next Features
- [USBIP USB over TCP](project_usbip.md) — `USBIPManager` class to share USB devices over TCP port 3240
- **MCP remote access** — expose gateway_mcp.py over SSE/HTTP via Cloudflare tunnel

## Known Bugs Fixed (details in bugs.md)
Key recent: DISPLAY_TEXT VFO misattribution (2026-03-13), RTS change corrupts display (2026-03-13),
KV4P TX 20% audio dropout (2026-03-19), KV4P CTCSS DRA818 off-by-one (2026-03-19),
TH9800 PTT blind toggle state inversion (2026-03-21),
SDR post-duck stutter: aioc_ducks_sdrs gate removed + re-duck inhibit (2s) + fade-in reset (2026-03-23),
MCP sdr_tune wrong payload keys (2026-03-23),
SDR2 permanently ducked: D75 noise above SDR_SIGNAL_THRESHOLD=-70 kept other_audio_active=True (2026-03-24),
ANNIN level bar stuck after voice note transmission (2026-03-24),
D75 default unmuted caused noise + SDR ducking on startup (2026-03-24),
D75 BT Start button shown during auto-connect: added _btstart_in_progress flag (2026-03-24),
D75 serial never connects: btstart blocking caused protocol desync, _do_btstart skipped serial.connect(), poll thread self-join crash (2026-03-24),
D75 tone/shift/offset wrong FO indices: 4 layered bugs — 11-field vs 21-field, wrong flag/shift/mode positions, gateway timeout crash (2026-03-24).

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt is NOT committed** — repo is PUBLIC; config is in .gitignore
- Config file overrides code defaults — changing defaults in code has no effect if config has the old value

## Claude Access Methods (see feedback_access_methods.md)
Claude runs on the same machine as the gateway. Available methods: MCP tools (preferred for control), direct HTTP to port 8080, filesystem read/edit, shell commands. Do NOT assume MCP is the only option.

## Machine Setup — user-optiplex3020 (Arch Linux)
- Cloned to `/home/user/Downloads/radio-gateway`; Git user: ukbodypilot / robin.pengelly@gmail.com; token in remote URL
- Arch Linux (EndeavourOS), XFCE4, Python 3.14, sudo password: `user`
- Relay USB: `2-1.3` → `/dev/relay_radio`; FTDI CAT cable: `2-1.1` → `/dev/ttyUSB1`
- `:0` — User desktop (VNC 5900, xrdp 3389)
- **Do NOT touch `:0` VNC/xrdp config** — user relies on them for remote access
- KV4P HT: `/dev/kv4p` → ttyUSB0; TX_RADIO = kv4p; Telegram bot: @radio_gateway_bot, chat_id=6538333604
