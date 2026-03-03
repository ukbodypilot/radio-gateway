# Mumble Radio Gateway — Project Memory

## Update this file
Update MEMORY.md and detail files at the end of every session and whenever a significant bug or pattern is discovered. Keep this file under 200 lines.

## Project Overview
Radio-to-Mumble gateway. AIOC USB device handles radio RX/TX audio and PTT. Optional SDR input via ALSA loopback. Optional Broadcastify streaming via DarkIce. Python 3, runs on Raspberry Pi and Debian amd64.

**Main file:** `mumble_radio_gateway.py` (~5000+ lines)
**Installer:** `scripts/install.sh` (8 steps, targets Debian/Ubuntu/RPi)
**Config:** `gateway_config.txt` (copied from `examples/gateway_config.txt` on install)
**Start script:** `start.sh` (8 steps: kill procs, CPU governor→performance, loopback, AIOC USB reset, pipe, DarkIce, FFmpeg, gateway w/nice -10; `sudo -v` cached at top)
**Windows client:** `windows_audio_client.py` (SDR input on 9600 or Announcement on 9601)

## Announcement Input (port 9601)
- `NetworkAnnouncementSource` — listens on 9601, inbound TCP, length-prefixed PCM
- `ptt_control=True`, `priority=0` — mixer routes audio to radio TX and activates PTT
- Audio-gated PTT: discards silence below `ANNOUNCE_INPUT_THRESHOLD` (-45 dBFS)
- 2s PTT hold (`_ptt_hold_time`) — returns silence+PTT=True through speech pauses
- `ANNOUNCE_INPUT_VOLUME = 4.0` — volume multiplier (clipped to int16)
- Mute key: `a` (mute toggle), status bar shows muted state on AN bar
- Config: `ENABLE_ANNOUNCE_INPUT`, `ANNOUNCE_INPUT_PORT`, `ANNOUNCE_INPUT_HOST`, `ANNOUNCE_INPUT_THRESHOLD`, `ANNOUNCE_INPUT_VOLUME`

## Windows Audio Client
- `windows_audio_client.py` — captures from local input device, sends length-prefixed PCM
- Mode selection on first run: SDR input (port 9600) or Announcement (port 9601)
- Config saved to `windows_audio_client.json` (in .gitignore)
- Same wire format as RemoteAudioSource (4-byte BE length + PCM payload)
- Keyboard: `l` = toggle LIVE/IDLE (LIVE sends real audio in red, IDLE sends silence in green)
- Cross-platform keyboard listener: msvcrt (Windows) / tty+termios (Unix)

## Key Architecture
- `AIOCRadioSource` — reads from AIOC ALSA device (radio RX audio)
- `SDRSource` — reads from ALSA loopback via background reader thread (non-blocking)
- `RemoteAudioServer` — TCP server, sends mixed audio to one connected client (length-prefixed PCM)
- `RemoteAudioSource` — TCP client, receives audio from RemoteAudioServer; name="SDRSV"
- `AudioMixer` — mixes SDR + AIOC with duck-out logic and fade in/out; returns 8-tuple (8th = sdr_only_audio)
- `audio_transmit_loop()` — feeds Mumble encoder; sends silence to keep Opus encoder fed
- pymumble/pymumble_py3 — Mumble protocol; SSL shim applied before import for Python 3.12+

## Critical Settings (current defaults)
- `MUMBLE_BITRATE = 72000`, `MUMBLE_VBR = false` (CBR)
- `VAD_THRESHOLD = -45`, `VAD_ATTACK = 0.05`, `VAD_RELEASE = 1.0`, `VAD_MIN_DURATION = 0.25`
- `AUDIO_CHUNK_SIZE = 9600` (200ms at 48kHz)
- SDR loopback: `hw:4,1` / `hw:5,1` / `hw:6,1` (capture side)
- `SDR_BUFFER_MULTIPLIER = 4`
- AIOC pre-buffer: 3 blobs / 600ms; SDR pre-buffer: 2 blobs / 400ms (1 blob during rebroadcast)
- `PLAYBACK_VOLUME = 4.0`, `ANNOUNCE_INPUT_VOLUME = 4.0`
- `SDR_AUDIO_BOOST = 2.0`, `SDR2_AUDIO_BOOST = 2.0` — default 2x volume boost
- `SDR_DUCK_COOLDOWN = 3.0` — symmetric cooldown after SDR-to-SDR unduck
- `SDR_SIGNAL_THRESHOLD = -60.0` — dBFS threshold for SDR signal detection (was hardcoded -50)

## Keyboard Controls
- MUTE: `t`=TX `r`=RX `m`=Global `s`=SDR1 `x`=SDR2 `c`=Remote `a`=Announce `o`=Speaker
- AUDIO: `v`=VAD toggle `,`=Vol- `.`=Vol+
- PROC: `n`=Gate `f`=HPF `g`=AGC `w`=Wiener `e`=Echo
- SDR: `d`=SDR1 Duck toggle `b`=SDR Rebroadcast toggle
- PTT: `p`=Manual PTT toggle
- PLAY: `1-9`=Announcements `0`=StationID `-`=Stop
- RELAY: `j`=Radio power button (momentary pulse)
- TRACE: `i`=Start/stop audio trace
- NOTE: AGC moved from 'a' to 'g'; proc flag changed from A to G

## ALSA Loopback Setup
- 3 cards pinned to hw:4, hw:5, hw:6 via `enable=1,1,1 index=4,5,6`
- Config: `/etc/modprobe.d/snd-aloop.conf` → `options snd-aloop enable=1,1,1 index=4,5,6`
- Each card: hw:N,0 (SDR app writes) / hw:N,1 (gateway reads)

## Python / pymumble
- Install `hid` (not `hidapi`) — gateway uses `hid.Device`
- pymumble: try `pymumble-py3` first, fall back to `pymumble`
- SSL shim patches `ssl.wrap_socket` and `ssl.PROTOCOL_TLSv1_2` before import (Python 3.12+)

## WirePlumber Issues (Debian with PipeWire)
- WirePlumber grabs ALSA loopback (locks to S32_LE, blocks DarkIce S16_LE)
- WirePlumber grabs AIOC (hides it from PyAudio)
- Fix: `~/.config/wireplumber/wireplumber.conf.d/99-disable-loopback.conf`
- **PyAudio uses PipeWire backend** — disabled devices don't appear in PyAudio enumeration
  even though `aplay -l` sees them via raw ALSA. Gateway must open AIOC before
  WirePlumber disables it, or use manual device index.

## AIOC USB Issues
- AIOC audio output can get stuck in stale state — PTT keys radio but no audio transmitted
- Symptom: `aplay -l` shows AIOC, `/proc/asound/cardN/stream0` Playback shows `Stop`,
  `speaker-test -D hw:N,0` produces no audio on radio
- Fix: USB reset (unplug/replug or sysfs authorized cycle)
- `start.sh` now does AIOC USB reset at step 4 before gateway launch
- Reset method: `echo 0 > /sys/bus/usb/devices/X-Y/authorized; sleep 1; echo 1 > ...`

## DarkIce Notes
- DarkIce 1.5 parser bug: crashes if "password" appears before first `[section]` header
- Config template: `scripts/darkice.cfg.example` (NOT examples/)
- Needs audio group + realtime limits
- udev: needs BOTH `SUBSYSTEM=="usb"` AND `SUBSYSTEM=="hidraw"` rules for AIOC

## Audio Trace Instrumentation
- PTT branch now has its own RMS measurement (was blind before — RMS always showed 0)
- PTT outcomes: `ptt_ok` (wrote to AIOC), `ptt_nostr` (output_stream None),
  `ptt_txm` (TX muted), `ptt_err` (write failed)
- Previous traces showing RMS=0 for all PTT ticks were misleading — the measurement
  point was after `continue` so it never ran for PTT

## Audio Processing — Vectorised (commit a41a0bc)
All three pure-Python per-sample loops replaced with numpy/scipy:
- `_mix_audio_streams()` — list comprehension → `np.frombuffer` + `np.clip` (~10× faster; always runs)
- `apply_highpass_filter()` — IIR for-loop → `scipy.signal.lfilter` with `zi` state carry;
  also fixed latent bug (old code reset `prev_output=0` each chunk, now both states carried)
  `self.highpass_state` reused: `None` on first call → `lfilter_zi(b,a)*0`; then zi array (shape (1,))
- `apply_spectral_noise_suppression()` — O(n·w) sliding window → `scipy.ndimage.uniform_filter1d` O(n)
- Noise gate left as-is (serial carry, acceptable ~2.4ms, not worth complexity)
- scipy already present on RPi OS — no new deps

## Text-to-Speech (gTTS)
- `!speak <text>` or `!speak <voice#> <text>` — voice 1-9 via gTTS lang/tld combos
- Voices: 1=US 2=British 3=Australian 4=Indian 5=South African 6=Canadian 7=Irish 8=French 9=German
- `TTS_DEFAULT_VOICE = 1` in config; `TTS_VOLUME`, `PTT_TTS_DELAY`
- Mumble text messages arrive as HTML — stripped with `re.sub(r'<[^>]+>', '', msg)` + `html.unescape()`
- Voice map is class-level `TTS_VOICES` dict on the gateway class

## Relay Control (CH340 USB Relays)
- `RelayController` class: 4-byte serial commands, `CMD_ON`/`CMD_OFF`, lazy `import serial`
- Radio power relay: `j` key momentary pulse (ON 0.5s then OFF — simulates button press), `ENABLE_RELAY_RADIO`
- Charger relay: automatic schedule, `ENABLE_RELAY_CHARGER`, `RELAY_CHARGER_ON_TIME`/`OFF_TIME`
- Schedule handles overnight wrap (e.g. 23:00→06:00); only sends command on state change
- Status bar: `PWRB` (white idle, yellow during pulse) + `CHG:CHRGE/DRAIN` (green/red, 5 chars)
- Udev template: `scripts/99-relay-udev.rules` — maps physical USB port to persistent symlinks
- Cleanup: closes serial ports but leaves relays in current state (no power-cycle on restart)
- Dependency: `pyserial` (imported inside `open()` — only fails if relay enabled but pkg missing)

## SDR Rebroadcast
- Toggle: `b` key. Routes mixed SDR-only audio (no AIOC/PTT) to AIOC radio TX
- `SDR_REBROADCAST_PTT_HOLD = 3.0` — seconds PTT holds after SDR signal stops
- State vars: `sdr_rebroadcast`, `_rebroadcast_ptt_active`, `_rebroadcast_sending`, `_rebroadcast_ptt_hold_until`
- AIOC TX feedback fix: `radio_source.enabled = False` while rebroadcast PTT active (prevents ducking loop)
- PTT release timer guard: `status_monitor_loop()` skips PTT timeout when `_rebroadcast_ptt_active`
- Status bar: SDR labels white (off), green (rebroadcast idle), red (rebroadcast sending)
- PTT indicator: `B-ON` (cyan) when rebroadcast PTT active
- SDR prebuffer reduced to 1 blob during rebroadcast (halves gap duration)
- Trace: element 22 `_tr_rebro` (sig/hold/idle), `rebro_ptt` events, `rb` column in detail

## SDR Mixer — sole_source Logic
- `sole_source` = no AIOC/PTT audio present (SDRs are the only source type)
- Refined: an SDR with no signal is NOT force-included if another SDR has instant signal
- Pre-scan pass checks `check_signal_instant()` for all SDRs before main inclusion loop
- Prevents loopback noise from unused SDR polluting the mix

## Known Bugs Fixed (details in bugs.md)
- SDR burst audio, Mumble encoder starvation, duck-out regression
- Config parser crash on decimal, global_muted UnboundLocalError
- DarkIce hidraw udev, WirePlumber AIOC/loopback grab
- SDR2 duck-through on SDR1 buffer gaps
- Announcement/PTT keys spam errors without AIOC
- Status bar width shift on mute/duck (fixed-width padding)
- AIOC audio output stale state (USB reset fix)
- HPF prev_output reset bug (fixed by lfilter zi carry)
- SDR-to-SDR ducking inconsistency (commit 3808066)
- SDR periodic audio gaps (commit 053b351): SDRSource lacked the _prebuffering gate that AIOC has. Fixed: always-drain every tick + gate refuses to serve until 3 blobs buffered after any depletion. Other SDR covers during rebuild. Zero silence gaps in verified trace.
- SDR volume 6dB step when second SDR joins/exits mix: crossfade `_mix_audio_streams(ratio=0.5)` attenuated SDR1 by 6dB when SDR2 present. Fixed: sum-and-clip in second pass (each SDR at full gain). Commit 69577ac.
- AIOC duck hold causing 1s dead air: `_hold_fired` kept `aioc_ducks_sdrs=True` for 1s after AIOC VAD released, outputting silence. Fixed: gate on `non_ptt_audio is not None`.
- SDR prebuffering + stale hysteresis ducking SDR2: when SDR1 returns None, `has_actual_audio` still True during release hold → `sig=True` → SDR2 ducked. Fixed: clear `sig=False` before continue.
- Sub-buffer latency buildup under CPU load: cap at 5×blob_bytes after eager drain
- SDR-to-SDR rapid switching: added SDR_DUCK_COOLDOWN (3.0s) symmetric cooldown
- No-signal SDR polluting mix: sole_source refined to exclude no-signal SDRs when another has audio
- SDR prebuffer gap too long (400ms): reduced from 3 blobs to 2 blobs; AIOC keeps 3
- Mumble HTML in TTS: text messages arrive as HTML, gTTS read tags/entities aloud. Fixed: strip+unescape
- SDR Rebroadcast bugs: AIOC TX feedback ducking, PTT release timer, TX bar level, prebuffer gaps (see bugs.md)

## Deployment Notes
- WirePlumber config must be in `~/.config/wireplumber/wireplumber.conf.d/`
- Local Mumble server can interfere — disable if present
- pymumble sends voice via TCP tunnel (UDPTUNNEL), not actual UDP
- start.sh sets CPU governor to `performance` (step 2) and launches gateway with `nice -n -10` (step 8)
- DarkIce runs FIFO RT 4; gateway runs nice -10 (SCHED_OTHER); competing apps at NI=0 stay below it

## SDR Loopback Watchdog
- Config: `SDR_WATCHDOG_TIMEOUT` (10s), `SDR_WATCHDOG_MAX_RESTARTS` (5), `SDR_WATCHDOG_MODPROBE` (false)
- Staged recovery: stage 1=reopen, stage 2=reinit PyAudio, stage 3=reload snd-aloop

## TH-9800 CAT Control
- `RadioCATClient` class: TCP client for TH9800_CAT.py server
- Config: `ENABLE_CAT_CONTROL`, `CAT_HOST`, `CAT_PORT`, `CAT_PASSWORD`
- Setup: channel (L/R), volume (L/R 0-100), power (L/R L/M/H)
- Channel set: steps per-VFO dial (L_DIAL_RIGHT/R_DIAL_RIGHT), no VFO switch needed
- Volume set: uses `!vol LEFT|RIGHT N` TCP command (added to TH9800_CAT.py), steps by 2 from default 25
- Power set: cycles L_LOW/R_LOW button, reads DISPLAY_ICONS for current power
- **Packet format**: byte 0 = packet type, byte 1 = VFO indicator (NOT reversed)
- Channel text at data[3:6], display text at data[3:9], power byte at data[7]
- TH9800 config.txt: `auto_start_server=true` auto-starts TCP server on GUI launch
- Gateway sends `!rts True` after connect to ensure USB TX control mode
- Debug log: `cat_debug.log` (all packet parsing, steps, commands — console shows summary only)
- Status bar: CAT indicator — white=enabled/unconnected, green=connected, red=active (1s min visibility)
- SIGINT handler during setup for clean ctrl+c abort

## Status Bar
- format_level_bar() returns fixed-width (11 visible chars: 6-char bar + space + 4-char suffix)
- Status icon only (no ACTIVE/IDLE/STOP text label, no colon prefix)
- Bar display order: TX → RX → SP → SDR1 → SDR2 → SV/CL → AN → relay → CAT

## audio/ Folder — Local Only
- `audio/` is in `.gitignore` — never committed (sensitive/copyrighted audio clips)
- Files live at `~/Downloads/mumble-radio-gateway/audio/` on each machine; backup at `~/audio_stash/` on the Pi
- **New machine setup:** `scp -r user@pi-ip:~/audio_stash/ ~/Downloads/mumble-radio-gateway/audio`
- Old git history still contains audio files — scrubbing requires force-push + re-clone on all machines (deferred)

## User Preferences
- CBR Opus (not VBR), commits requested explicitly, concise responses, no emojis
- **gateway_config.txt IS committed** (repo is private); bak/ is not
- Fixed-width status bar is important
