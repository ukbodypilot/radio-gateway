---
name: FTM-150 AIOC endpoint on Pi 192.168.2.121
description: FTM-150 radio via AIOC on Pi, audio/data mode switching, Direwolf TNC integration
type: project
---

## FTM-150 Endpoint — Status (2026-04-03)

**Machine:** Pi at 192.168.2.121, Debian 13 trixie, aarch64
**AIOC:** card 3 (`hw:3,0`), All-In-One-Cable USB
**Service:** `~/.config/systemd/user/ftm150-endpoint.service` (Restart=always)
**Old crontab+wrapper removed** — was spawning duplicate processes on kill failure

### Audio/Data Mode
- `audio` mode (default): PyAudio captures AIOC, streams to gateway. FTM-150 appears as routing source.
- `data` mode: PyAudio input closed, Direwolf subprocess started reading AIOC directly. KISS TCP on port 8001.
- Mode switched via link protocol command from gateway packet plugin.
- `get_audio()` returns None in data mode (no reopen attempts from watchdog).

### Stream Health
- `reopen_audio()`: terminates PyAudio + reinits (PipeWire loses device enum but name scan recovers)
- Gateway reconnect triggers `on_connect` → `reopen_audio()` (skips first connect)
- Zero-read watchdog: 200 consecutive zero-peak reads → reopen
- Error handler: 5 consecutive read errors → reopen, respects data mode
- Device scan: always scans for input/output capability separately

### AIOC Hardware
- Capture/Playback volume: max (0dB), can only attenuate
- FTM-150 data port: fixed level output (volume knob doesn't affect it)
- Noise gate: DISABLED for packet reception
- Radio set to 9600 baud data port mode for cleaner audio to Direwolf

### Direwolf on Pi
- v1.7 (Debian package), config at `/tmp/direwolf_endpoint.conf`
- `FIX_BITS 1` (v1.7 syntax), `AGWPORT 0` (disabled)
- Log forwarded to gateway via link protocol STATUS frames
- Standalone service DISABLED — replaced by endpoint-managed mode

### Key issue history
- Stale PyAudio streams after gateway restart → reopen_audio with full PyAudio terminate
- Duplicate processes from wrapper script → replaced with systemd service
- "Invalid number of channels" on reopen → fixed device index scanning
- "Device unavailable" on reopen → must terminate PyAudio to release ALSA handles
- Audio quality through gateway chain too degraded for packet decode → remote Direwolf

**Why:** FTM-150 is the packet radio, needs clean audio path to Direwolf.
**How to apply:** Deploy via `scp` to `192.168.2.121:/home/user/link/`. Restart with `systemctl --user restart ftm150-endpoint.service`.
