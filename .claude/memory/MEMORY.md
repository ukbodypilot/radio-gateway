# Radio Gateway — Project Memory

## Update this file
Update MEMORY.md and detail files at the end of every session and whenever a significant bug or pattern is discovered. Keep this file under 200 lines.

## Project Overview
Radio-to-Mumble gateway. AIOC USB device handles radio RX/TX audio and PTT. Optional SDR input via PipeWire virtual sink or ALSA loopback. Optional Broadcastify streaming via DarkIce. Python 3, runs on Raspberry Pi, Debian amd64, and Arch Linux.

**Main file:** `radio_gateway.py` (~5000+ lines)
**Installer:** `scripts/install.sh` (13 steps, targets Debian/Ubuntu/RPi/Arch Linux)
**Config:** `gateway_config.txt` (copied from `examples/gateway_config.txt` on install)
**Start script:** `start.sh` (11 steps: kill procs, Mumble GUI, TH-9800 CAT, Claude Code, CPU governor, loopback, AIOC USB reset, pipe, DarkIce, FFmpeg, gateway w/nice -10)
**Windows client:** `windows_audio_client.py` (server: send audio, client: receive audio, `m` to switch)

## SDR Input — PipeWire (preferred) or ALSA Loopback
- **PipeWire:** `SDR_DEVICE_NAME = pw:sdr_capture` — reads from virtual sink monitor via FFmpeg
- `PipeWireSDRSource` class: auto-creates sink via `pw-cli` if missing at startup
- WirePlumber persistence: `~/.config/wireplumber/wireplumber.conf.d/90-sdr-capture-sink.conf`
- Creates `sdr_capture` and `sdr_capture2` sinks; installer deploys this config (step 12)
- Route SDR app output to sink in pavucontrol or app settings
- **ALSA loopback:** `SDR_DEVICE_NAME = hw:4,1` — traditional method, 200ms blob delivery
- 3 cards pinned to hw:4, hw:5, hw:6 via `enable=1,1,1 index=4,5,6`

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — listens on 9601, inbound TCP, length-prefixed PCM
- `ptt_control=True`, `priority=0` — mixer routes audio to radio TX and activates PTT
- Audio-gated PTT: discards silence below `ANNOUNCE_INPUT_THRESHOLD` (-45 dBFS)
- 2s PTT hold, `ANNOUNCE_INPUT_VOLUME = 4.0`, Mute key: `a`

## Windows Audio Client
- `windows_audio_client.py` — send or receive audio (role-based)
- **server role**: captures from input device, sends length-prefixed PCM
- **client role**: listens on TCP port, plays received audio
- Keyboard: `l` = LIVE/IDLE or LIVE/MUTE, `m` = switch role
- Config: `windows_audio_client.json` (in .gitignore)

## Key Architecture
- `AIOCRadioSource` — reads from AIOC ALSA device (radio RX audio)
- `SDRSource` — reads from ALSA loopback via background reader thread
- `PipeWireSDRSource` — reads from PipeWire virtual sink monitor via FFmpeg subprocess
- `RemoteAudioServer` / `RemoteAudioSource` — TCP audio link
- `AudioMixer` — mixes SDR + AIOC with duck-out logic; returns 8-tuple
- `audio_transmit_loop()` — feeds Mumble encoder
- pymumble/pymumble_py3 — Mumble protocol; SSL shim for Python 3.12+

## Critical Settings (current defaults)
- `MUMBLE_BITRATE = 72000`, `MUMBLE_VBR = true` (VBR)
- `VAD_THRESHOLD = -45`, `VAD_ATTACK = 0.02`, `VAD_RELEASE = 1.0`, `VAD_MIN_DURATION = 0.1`
- `AUDIO_CHUNK_SIZE = 2400` (50ms), `SDR_BUFFER_MULTIPLIER = 4`
- SDR: `pw:sdr_capture` / `pw:sdr_capture2` (PipeWire default)
- AIOC pre-buffer: 3 blobs / 600ms; SDR pre-buffer: 2 blobs / 400ms
- `PLAYBACK_VOLUME = 2.0`, `ANNOUNCE_INPUT_VOLUME = 4.0`, `ENABLE_ANNOUNCE_INPUT = True`
- `SDR_AUDIO_BOOST = 1.0`, `SDR2_AUDIO_BOOST = 1.5`
- `SDR_DUCK_COOLDOWN = 3.0`, `SDR_SIGNAL_THRESHOLD = -70.0`, `SIGNAL_ATTACK_TIME = 0.25`
- `TTS_SPEED = 1.0` (speech speed, requires ffmpeg for != 1.0)
- `CW_WPM = 20`, `CW_FREQUENCY = 600`, `CW_VOLUME = 1.5`, `PTT_TTS_DELAY = 0.5`
- `REMOTE_AUDIO_PRIORITY = 0`, `ENABLE_PLAYBACK = True`, EchoLink: full bridge

## Keyboard Controls
- MUTE: `t`=TX `r`=RX `m`=Global `s`=SDR1 `x`=SDR2 `c`=Remote `a`=Announce `o`=Speaker
- AUDIO: `v`=VAD toggle `,`=Vol- `.`=Vol+
- PROC: `n`=Gate `f`=HPF `g`=AGC `w`=Wiener `e`=Echo
- SDR: `d`=SDR1 Duck toggle `b`=SDR Rebroadcast toggle
- PTT: `p`=Manual PTT toggle
- PLAY: `1-9`=Announcements `0`=StationID `-`=Stop
- RELAY: `j`=Radio power button `h`=Charger toggle
- SMART: `[`=Smart#1 `]`=Smart#2 `\`=Smart#3
- TRACE: `i`=Start/stop audio trace `u`=Start/stop watchdog trace
- MISC: `q`=Restart gateway (re-exec, reloads config) `z`=Clear console

## Python / pymumble
- Install `hid` (not `hidapi`) — gateway uses `hid.Device`
- pymumble: try `pymumble-py3` first, fall back to `pymumble`
- SSL shim patches `ssl.wrap_socket` and `ssl.PROTOCOL_TLSv1_2` before import (Python 3.12+)

## WirePlumber Issues (Debian with PipeWire)
- WirePlumber grabs ALSA loopback (locks to S32_LE, blocks DarkIce S16_LE)
- WirePlumber grabs AIOC (hides it from PyAudio)
- Fix: `~/.config/wireplumber/wireplumber.conf.d/99-disable-loopback.conf`

## Terminal Settings Bug (fixed)
- Keyboard listener runs as daemon thread using tty raw mode (setcbreak)
- Daemon threads killed before their `finally` blocks run on process exit
- Fix: save terminal settings on instance (`self._terminal_settings`), restore in `cleanup()`
- Safety net: `start.sh` cleanup runs `stty sane` to guarantee terminal restored on any exit

## DarkIce Notes
- DarkIce 1.5 parser bug: crashes if "password" appears before first `[section]` header
- Config template: `scripts/darkice.cfg.example`
- udev: needs BOTH `SUBSYSTEM=="usb"` AND `SUBSYSTEM=="hidraw"` rules for AIOC

## Audio Processing — Vectorised (commit a41a0bc)
- `_mix_audio_streams()` → `np.frombuffer` + `np.clip` (~10× faster)
- `apply_highpass_filter()` → `scipy.signal.lfilter` with `zi` state carry
- `apply_spectral_noise_suppression()` → `scipy.ndimage.uniform_filter1d`

## Text-to-Speech (gTTS)
- `!speak <text>` or `!speak <voice#> <text>` — voice 1-9 via gTTS lang/tld combos
- Mumble text messages arrive as HTML — stripped with `re.sub` + `html.unescape()`

## Relay Control (CH340 USB Relays)
- `RelayController` class: 4-byte serial commands, lazy `import serial`
- Radio power relay: `j` key, Charger relay: automatic schedule
- Dependency: `pyserial` (added to installer core packages)

## SDR Rebroadcast
- Toggle: `b` key. Routes mixed SDR-only audio to AIOC radio TX
- `SDR_REBROADCAST_PTT_HOLD = 3.0`
- AIOC TX feedback fix: `radio_source.enabled = False` while rebroadcast PTT active

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- Config: `ENABLE_CAT_CONTROL`, `CAT_STARTUP_COMMANDS`, `CAT_HOST`, `CAT_PORT`, `CAT_PASSWORD`
- `CAT_STARTUP_COMMANDS = false` → connect TCP but skip channel/volume/power setup
- `_logmsg` defaults to log-only (console=False); verbose shows on screen
- `setup_radio` prints concise summary, not per-step spam
- `_with_usb_rts()` — wraps setup tasks: sets RTS=USB, runs tasks, restores RTS
- `set_channel()` auto-detects VFO mode and presses V/M to switch to channel mode
- RTS set/toggle via TCP: `!rts`, `!rts True`, `!rts False`
- `set_rts()` parses response from TH9800 to track actual state
- TH9800_CAT.py `bool("False")` bug fixed → string comparison now

## Smart Announcements (Modular AI Backend)
- `SmartAnnouncementManager`: scheduled AI-powered spoken announcements
- Config: `ENABLE_SMART_ANNOUNCE`, `SMART_ANNOUNCE_AI_BACKEND`, `SMART_ANNOUNCE_N`
- Entry format: `interval_secs, voice, target_secs, {prompt text in braces}`
- **Backends** (`SMART_ANNOUNCE_AI_BACKEND`):
  - `google-scrape`: free. Drives real Firefox via xdotool, clicks AI Mode, scrapes Google AI Overview. Requires Firefox logged into Google on DISPLAY=:0, xdotool, xclip.
  - `duckduckgo` (default): free, no key. DuckDuckGo web search + Ollama local LLM. Falls back to search snippets if no Ollama.
  - `claude`: Anthropic API + web search. Key: `SMART_ANNOUNCE_API_KEY`
  - `gemini`: Google Gemini API + Google Search. Key: `SMART_ANNOUNCE_GEMINI_API_KEY`
- google-scrape flow: open JS console → `window.location.href=URL` → wait → paste JS to click AI Mode (link before "All" in toolbar) → Ctrl+A/C → parse AI content → Ollama or direct TTS
- Ollama params: `SMART_ANNOUNCE_OLLAMA_MODEL`, `_TEMPERATURE`, `_TOP_P`
- Word limit: ~2.5 words/sec × target_secs; max 60s
- Feeds text to existing gTTS → AIOC PTT pipeline (no CAT/TCP RTS switching)
- Keyboard: `[`=Smart#1, `]`=Smart#2, `\`=Smart#3
- Mumble commands: `!smart` (list), `!smart N` (trigger)
- Manual triggers (keyboard/Mumble) skip time window check
- Waits for radio to be free (VAD/playback) up to ~8min before dropping
- `SMART_ANNOUNCE_TOP_TEXT` / `SMART_ANNOUNCE_TAIL_TEXT` — optional spoken prefix/suffix
- Dependencies: `xdotool`+`xclip` (google-scrape), `ddgs` (duckduckgo), `anthropic` (claude), `google-genai` (gemini), Ollama (optional)
- Installer step 7: installs Ollama + pulls model (llama3.2:3b on PC, llama3.2:1b on Pi)

## Desktop Shortcut
- Template: `scripts/radio-gateway.desktop.template`
- Installer step 12: detects terminal emulator, substitutes `__TERMINAL__` and `__GATEWAY_DIR__`
- Opens terminal, cd to gateway dir, runs `start.sh`, keeps shell open on exit

## Known Bugs Fixed (details in bugs.md)
See bugs.md for full history. Key fixes: SDR burst audio, Mumble encoder starvation,
duck-out regression, config parser crash, DarkIce/WirePlumber issues, terminal raw mode
not restored on exit, SDR periodic gaps, rebroadcast bugs, SV status bar.

## Deployment Notes
- WirePlumber config must be in `~/.config/wireplumber/wireplumber.conf.d/`
- start.sh: reads config first (`read_config` helper), sudo keepalive loop, renice approach
- `START_TH9800_CAT`, `START_CLAUDE_CODE` config options control optional launches
- DarkIce/FFmpeg only started if `ENABLE_STREAM_OUTPUT = true`
- TH9800 output redirected to `/tmp/th9800_cat.log`
- DarkIce runs FIFO RT 4; gateway runs nice -10 (SCHED_OTHER)

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt is NOT committed** — repo is PUBLIC; config is in .gitignore
- Fixed-width status bar is important

## Machine Setup — user-optiplex3020 (Arch Linux, 2026-03-04)
- Cloned to `/home/user/Downloads/radio-gateway`
- Git user: ukbodypilot / robin.pengelly@gmail.com; token in remote URL
- Arch Linux (EndeavourOS), XFCE4, RDP via xrdp+x11vnc
- Python 3.14 on this machine
- sudo password: `user`
- Relay USB port: `2-1.3` → `/dev/relay_radio` (CH340 "USB Serial")
- FTDI CAT cable: `2-1.1` → `/dev/ttyUSB1` (FT232R)
