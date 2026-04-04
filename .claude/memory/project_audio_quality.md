---
name: Audio Quality Work (2026-04-04)
description: Comprehensive audio quality fixes on audio-quality branch — AIOC reader, BusManager timing, PTT stalls, stream tracing
type: project
---

## Branch: `audio-quality` (from main)

## Problems Found and Fixed

### 1. AIOC Reader — PyAudio reads silence through PipeWire (FIXED)
**Root cause:** WirePlumber config (`99-disable-loopback.conf`) disables the AIOC device so the gateway can use it directly. But PyAudio (and sounddevice) go through PipeWire's ALSA plugin, which returns DC silence for disabled devices — even when specifying `hw:N,0`.
**Fix:** Replaced PyAudio reader with `arecord` subprocess that uses raw ALSA. `_find_aioc_device()` now scans `/proc/asound/cards` instead of PyAudio name search.
**File:** `th9800_plugin.py` — `_rx_reader_loop()` now spawns `arecord -D hw:N,0`
**How to verify:** AIOC level shows ~18 (noise floor) at idle, jumps to 50+ on signal.

### 2. BusManager Clock Drift (FIXED)
**Root cause:** `_tick_loop()` used reset-based timing (`next_tick = time.monotonic() + interval`) causing actual period = interval + processing_time. ~40ms/second drift.
**Fix:** Changed to accumulative timing matching the main loop. Added snap-forward that skips ALL missed ticks (prevents back-to-back burst after stall).
**File:** `bus_manager.py:554-567`

### 3. SoloBus PTT Blocking BusManager for 150-600ms (FIXED)
**Root cause:** SoloBus.tick() called `radio.execute({'cmd': 'ptt', 'state': True/False})` synchronously. For AIOC, this does CAT `_pause_drain()` + `set_rts()` + HID write = 150-600ms blocking. Stalled ALL buses on every file play/stop.
**Fix:** `_fire_ptt()` runs PTT in a background thread (fire-and-forget), same pattern as D75 PTT.
**File:** `audio_bus.py` — SoloBus class
**Trace proof:** monitor_bus tick_slow events went from 9 (max 601ms) to 0.

### 4. Station ID Decode in Audio Thread (FIXED)
**Root cause:** `check_periodic_announcement()` called `queue_file()` → `_decode_file()` (disk I/O) inside `get_audio()` on the BusManager thread. Not the main stall source (PTT was), but still wrong.
**Fix:** Station ID pre-decoded at startup, cached. Periodic announcements use cached PCM.
**File:** `audio_sources.py` — FilePlaybackSource

### 5. AIOC RX Queue Latency (FIXED)
**Root cause:** Queue `maxsize=16` filled during startup before BusManager started consuming. Permanent 15-16 depth = 800ms stale audio latency.
**Fix:** Reduced to `maxsize=3` (150ms max). Flush stale chunks on first consumer read.
**File:** `th9800_plugin.py:130` and `get_audio()`
**Result:** Queue depth now mean=2.0, latency ~250ms (includes arecord ALSA buffer).

### 6. GC in Audio Hot Paths (FIXED)
**What:** `gc.disable()` in both main loop and BusManager tick loop. Manual gen-0 collection every 5s during sleep window.
**Result:** GC events are 0.1-0.3ms, not causing stalls.
**Files:** `gateway_core.py`, `bus_manager.py`

### 7. Thread-Safe PCM Queue (FIXED)
**What:** Replaced plain list `_pcm_queue` with `collections.deque(maxlen=8)`.
**File:** `bus_manager.py`

## Instrumentation Added

### Stream-Level Trace (`stream_trace.py`)
New file. Records per-chunk events at every handoff point when trace is active. Toggled by same button as main audio trace. Dumps to `tools/stream_trace.txt`.

**Instrumented streams:**
- **aioc_rx:** `arecord_read` → `post_proc` → `queue_put` → `queue_get`
- **aioc_tx:** `queue_put` → `hw_write`
- **sdr1_rx / sdr2_rx:** `parec_read` → `get_chunk`
- **kv4p_tx:** `put_audio`
- **th9800_deliver:** `mumble` (with timing if >5ms)
- **monitor_bus / th9800_bus:** `tick_slow` (any tick+deliver >5ms)
- **BusManager stall skips:** recorded with count of missed ticks

### Extended Main Trace (`audio_trace.py`)
New columns 50-53: speaker drops, PCM drain count, cross-clock drift (wall-clock), GC events.
BusManager timing analysis section in dump.

### Cross-Clock Drift Measurement
`_get_cross_clock_drift_ms()` compares wall-clock timestamps of most recent tick from each thread. Previous version was broken (compared tick counts).

## Final Trace Results (2026-04-04)
- Main loop tick: stdev=0.0ms, max=50.6ms
- BusManager tick: stdev=1.4ms, max=91.8ms, zero >100ms
- BusManager processing: mean=0.21ms, max=8.24ms
- AIOC RX queue: depth mean=2.0, zero overflows
- All stream intervals: 50.0ms stdev <2ms
- Cross-clock drift: stdev=0.1ms
- Zero speaker drops, zero output clicks, zero stalls

## Research Document
Full research into Python real-time audio limits, alternative approaches (Rust, Cython, PipeWire/JACK delegation), and external project comparisons preserved in `docs/audio-quality-research.md`.

## How to Diagnose Future Audio Issues
1. Start trace via Logs page button (or 'i' key)
2. Reproduce the issue
3. Stop trace
4. Read `tools/stream_trace.txt` — summary shows per-stream health at a glance
5. Look for: overflow flags, intervals >80ms, repeated chunks, SILENT counts
6. Read `tools/audio_trace.txt` — BusManager timing section shows stall sources
7. If stalls appear: check `tick_slow` events to identify which bus/source blocks
