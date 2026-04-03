---
name: FTM-150 link endpoint
description: FTM-150 radio connected via AIOC on Pi 192.168.2.121, working as generic link endpoint
type: project
---

## FTM-150 Link Endpoint (2026-04-02)

FTM-150 radio wired to an AIOC on Pi at 192.168.2.121, running as a generic link endpoint.

### Setup
- Pi: 192.168.2.121, AIOC on hw:2,0 (card 2), Python 3.13, pyaudio + hidapi installed
- Endpoint files: `~/link/gateway_link.py` + `~/link/link_endpoint.py` (copied from gateway)
- Start: `cd ~/link && python3 -u link_endpoint.py --server 192.168.2.140:9700 --name ftm-150 --plugin aioc`
- Endpoint name: `ftm-150` → sanitised ID: `ftm_150`, display: `Ftm 150 [RX]` / `Ftm 150 [TX]`

### Key details
- AIOC noise floor: ~-44.5 dB. Noise gate threshold: -48 dB with 3 dB hysteresis
- Noise gate runs on endpoint side (AudioPlugin.get_audio), not gateway
- AIOC HID PTT on channel 3 (same as default)
- RX/TX gain saved to `~/.config/radio-gateway/link_endpoints.json` (keys: rx_boost, tx_boost)
- Hot-swap on reconnect: `BusManager.update_radio_reference()` swaps bus radio pointers using routing config lookup

### What was built (commit 06d4554)
- Generic link endpoint source/sink tiles on /routing page (any endpoint, not just D75)
- RX/TX level bars in routing page (`/routinglevels` endpoint)
- Gain sliders (separate RX audio_boost / TX tx_audio_boost)
- Mute support via sanitised name lookup
- SoloBus tx_only fix: Phase 5 uses tx_audio instead of rx_audio for sink output
- BusManager registers _tx sinks for level display
- Bus radio delivery: generic fallback via `_get_radio_plugin()` 
- Hot-swap on reconnect instead of full bus reload (tested: both tester and monitor buses swap correctly)

**Why:** User has FTM-150 hardware wired to AIOC. Old FTMPlugin was removed (SCU-56 cable couldn't drive PTT). AIOC handles audio + PTT via HID GPIO.

**How to apply:** Start endpoint on Pi before or after gateway — hot-swap handles reconnection. No systemd service yet; manual start.
