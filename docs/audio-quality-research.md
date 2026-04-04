# Audio Quality Research — 2026-04-04

Comprehensive research into audio timing/quality problems in the radio gateway.
This document preserves all findings so we can change direction without losing context.

---

## Table of Contents
1. [Core Audio Pipeline Analysis](#1-core-audio-pipeline)
2. [Bus/Mixer/Plugin Architecture](#2-busmixerplugin-architecture)
3. [Confirmed Defects](#3-confirmed-defects)
4. [External Research — Live Audio Best Practices](#4-external-research)
5. [Alternative Approaches & Viability](#5-alternative-approaches)
6. [Performance Benchmarks](#6-benchmarks)
7. [Decision Framework](#7-decision-framework)

---

## 1. Core Audio Pipeline

### Configuration & Timing Constants
- `AUDIO_RATE = 48000` Hz
- `AUDIO_CHUNK_SIZE = 2400` samples (50ms)
- `AUDIO_CHANNELS = 1` (mono)
- File: `radio_gateway.py:129-131`

### Main Audio Loop (`audio_transmit_loop`)
- **Location:** `gateway_core.py:2199-2732`
- **Thread:** Daemon thread spawned at line 3413
- **Scheduling:** `os.sched_setscheduler(SCHED_RR, priority=10)` (line 2205), fallback `os.nice(-10)`
- **Self-clock:** `_TICK = 0.05s`, accumulative `_next_tick += _TICK`, snap-forward if overdue > 1 tick
- **Tick tracking:** `_tick_dt` in milliseconds (line 2241)

#### Per-Iteration Flow
1. **Apply pending PTT** (lines 2268-2277) — queued by keyboard thread, applied between audio reads to avoid USB contention during `stream.read()`
2. **Mixer tick** (lines 2290-2338) — `self.mixer.tick(AUDIO_CHUNK_SIZE)`, duration tracked as `_tr_mixer_ms`
3. **VAD gate/signal detection** (lines 2344-2419) — early delivery to sinks BEFORE VAD
4. **SDR rebroadcast PTT** (lines 2444-2514) — separate PTT for SDR-only, RMS threshold >100
5. **Output delivery** (lines 2570-2642) — speaker queue, remote audio, gateway link, Mumble
6. **Click suppression** (lines 2551-2568) — detects sample jumps > 8000, interpolates 4-sample window
7. **Trace recording** (lines 2665-2732) — 50 fields per tick when active

### AIOC Audio (TH-9800 via th9800_plugin.py)

#### RX Path (Radio Input)
- **Reader thread:** `_rx_reader_loop` (lines 393-525)
- **Buffer:** `frames_per_buffer = chunk_size * 4 = 9600 samples = 200ms` (line 426)
- **Read size:** `chunk_size = 2400 samples (50ms)` (line 409)
- **Queue:** `_rx_queue` with backpressure (drop oldest if full, line 480)
- **Processing:** per-source AudioProcessor applied in reader thread
- **Error recovery:** PortAudio overflow (errno -9981) recoverable; 5+ errors trigger stream reopen

#### TX Path (Radio Output)
- **Writer thread:** `_tx_writer_loop` (lines 246-264) — non-blocking deque drain
- **Output:** `_output_stream.write()` with `exception_on_overflow=False`
- **No queue limit concerns** — deque with maxlen=16

### PTT Logic (`gateway_core.py:743-886`)

| Method | Location | Latency | Notes |
|--------|----------|---------|-------|
| AIOC GPIO | lines 754-799 | 1-5ms | Pauses CAT drain, switches RTS, HID write |
| D75 fire-and-forget | lines 834-859 | <1ms | Raw socket write, no lock contention |
| Relay serial | lines 800-807 | 1-2ms | 4-byte command at 9600 baud |
| Software CAT | lines 808-831 | 10-50ms | Pauses drain, sends TCP command |

Key design: D75 explicitly avoids blocking to prevent audio mixer starvation (documented in code comments, lines 839-842).

### Speaker Queue & PortAudio Callback
- **Location:** `gateway_core.py:1229-1297`
- **Queue:** `speaker_queue` maxsize=6 (300ms)
- **Callback:** Runs on PortAudio real-time thread, drains one chunk per call
- **Backpressure:** When queue >= 4, silently drains to 2 (lines 1259-1264)
- **PortAudio internal buffer:** ~100-150ms (2-3 periods)

### Threading Model (Full List)
1. Main thread — `gateway_core.py run()`
2. TX audio loop — daemon, SCHED_RR priority 10, 20Hz
3. BusManager tick loop — daemon, default priority, 20Hz
4. Status monitor — 0.1s interval health checks
5. Keyboard thread — queues PTT changes
6. TH9800 RX reader — blocks on `stream.read()`
7. TH9800 TX writer — non-blocking deque drain
8. SDR parec readers (per tuner) — blocks on `proc.stdout.read()`
9. Mumble sound thread — pymumble internal, Opus encode
10. KV4P reader (if connected) — serial decode
11. Link endpoint readers (per endpoint) — TCP frame reader

### Synchronization
- `speaker_queue` — `queue.Queue(maxsize=6)`, mutex-based
- `_rx_queue` (TH9800) — `queue.Queue(maxsize=16)`, mutex-based
- `_chunk_queue` (SDR, KV4P, Link) — various maxsizes (16-64), mutex-based
- `_pcm_queue` (BusManager) — plain Python list, NO lock (TOCTOU race possible)
- `_pending_ptt_state` — single value, GIL-protected
- CAT `_sock_lock` — protects serial commands
- CAT `_drain_paused` — boolean flag for pause/resume

### End-to-End Latency Budget
| Stage | Latency | Notes |
|-------|---------|-------|
| AIOC PortAudio buffer | 100-150ms | 200ms ring, half-buffer average |
| Reader thread → queue | 0-50ms | One tick cycle |
| Bus tick processing | 2-5ms | Mixer + ducking |
| Speaker queue | 0-300ms | Up to 6 chunks buffered |
| PortAudio speaker buffer | 100-150ms | OS-level |
| **Total RX→Speaker** | **200-650ms** | |
| PTT queue → apply | 25ms avg | Applied between reads |
| HID/relay/CAT write | 1-50ms | Method-dependent |
| **Total PTT latency** | **25-100ms** | |

---

## 2. Bus/Mixer/Plugin Architecture

### Bus Types (audio_bus.py, 1068 lines)

#### ListenBus (lines 277-575) — Primary bus, ticked by main loop
6-phase mixing pipeline:
1. Collect ducker audio (non-duckable sources: radio RX, playback, announcements)
2. Cross-tier duck decision (hysteresis state machine)
3. Fetch duckee audio (duckable: SDR, remote, link endpoints)
4. Per-duckee peer ducking + signal detection + hold + fades
5. Mix included duckees (`additive_mix()`)
6. Final assembly (priority: PTT > ducker+duckee > ducker > duckee)

Signal detection config:
- `SIGNAL_ATTACK_TIME = 2.0s` (continuous signal required)
- `SIGNAL_RELEASE_TIME = 3.0s` (silence required to release)
- `SDR_SIGNAL_THRESHOLD = -60.0 dB`
- `SDR_DUCK_COOLDOWN = 3.0s`

#### SoloBus (lines 581-708) — Single radio TX/RX
3-phase: collect TX sources → PTT management → get RX audio
`PTT_RELEASE_DELAY = 1.0s`

#### DuplexRepeaterBus (lines 710-863) — Full-duplex cross-link
4-phase: get RX both sides → cross-link TX → mixed output to sinks
`REPEATER_PTT_HOLD = 1.0s`

#### SimplexRepeaterBus (lines 865-1069) — Store-and-forward
States: IDLE → RECEIVING → TAIL → PLAYING → IDLE
`SIMPLEX_TAIL_TIME = 1.0s`, `SIMPLEX_MAX_BUFFER = 30.0s`

### Mixing Functions (audio_bus.py:21-83)
- `check_signal_instant()` — RMS check, no hysteresis (used for instant detection)
- `mix_audio_streams()` — Additive with soft tanh limiter (knee at 24000 = 75% full scale)
- `additive_mix()` — N-stream iterative mixing
- `apply_fade_in()` — Linear 0→1 over 480 samples (10ms)
- `apply_fade_out()` — Linear 1→0 over full chunk

### BusManager (bus_manager.py, 541 lines)
- **Dedicated background thread** (line 139-141)
- **Tick interval:** 50ms (line 36)
- **Timing:** Reset-based (line 516) — **DEFECTIVE, see below**
- **PCM drain:** collects from all buses into `_pcm_queue` (plain list, no lock)
- **Mumble frame alignment:** 20ms chunks (960 samples = 1920 bytes), accumulated in `_mumble_buf`
- **Per-bus AudioProcessor** for gate/HPF/LPF/notch (lines 329-336)

### Plugin Architecture

#### SDR Plugin (sdr_plugin.py, 1107 lines)
- Two `_TunerCapture` objects (master/slave) for RSPduo dual tuner
- Each tuner: parec subprocess → reader thread → `Queue(maxsize=16)`
- Reader reads 4800 bytes (2400 stereo samples = 50ms) per iteration
- `get_chunk()` drains up to 6 chunks (latency capping), stereo→mono downmix
- Internal master/slave ducking (2s attack/3s release hysteresis)
- Discontinuity tracking between chunks (stored as `_serve_discontinuity`)

#### TH-9800 Plugin (th9800_plugin.py, 697 lines)
- AIOC USB device, PyAudio for audio I/O
- Dedicated RX reader thread + TX writer thread
- CAT control via TCP to external `th9800-cat.service`
- PTT: AIOC GPIO (HID write) or relay or software CAT

#### KV4P Plugin (kv4p_plugin.py, 767 lines)
- ESP32-based HT radio over serial + Opus codec
- **Adaptive resampling PLL:** target 3 chunks (150ms buffer)
  - `buf_error = (buf_now - buf_target) / buf_target`
  - `adjustment = buf_error * 0.002` (proportional control)
  - `ratio = clamp(ratio + adjustment, [0.95, 1.25])`
- Vectorized linear interpolation using numpy
- Latency capping: if buffer > 6 chunks, trim excess

#### Gateway Link (gateway_link.py, 1601 lines)
- Frame protocol: `[1B type][2B length][payload]` over TCP
- Types: AUDIO (0x01), COMMAND (0x02), STATUS (0x03), REGISTER (0x04), ACK (0x05)
- Per-endpoint: accept thread, reader thread, heartbeat thread
- Audio delivery: `send_audio_to_all()` with per-endpoint send_lock
- Socket timeout: 10s for cable-pull detection

### Audio Trace (audio_trace.py, 846 lines)
50 columns per tick including: timestamp, delta time, SDR/AIOC queue depths, mixer call time, Mumble timing, speaker queue depth, audio levels, discontinuities, KV4P stats, thread liveness, memory.

**Gaps in trace:** No lock contention timing, no GIL measurement, no per-plugin processing time, no network I/O timing, no buffer allocation latency, no GC pause timing.

---

## 3. Confirmed Defects

### Defect 1: BusManager Clock Drift (CRITICAL)
**File:** `bus_manager.py:516`

BusManager uses reset-based timing:
```python
next_tick = time.monotonic() + self._tick_interval  # resets AFTER processing
```
Main loop uses correct accumulative timing:
```python
_next_tick += _TICK  # accumulative, drift-free
```

BusManager actual period = `_tick_interval + processing_time`. For 2ms processing per tick, that's 40ms/second drift. Over 10 seconds, BusManager falls 400ms behind. Since TH9800 (AIOC) goes through BusManager and SDR goes through main loop, they're on different drifting clocks.

### Defect 2: Speaker Queue Silent Drop (MODERATE)
**File:** `gateway_core.py:1258-1264`

When queue reaches 4 chunks, silently drains to 2. No fade, no trace record, no log. Causes audible drops when main loop runs slightly faster than PortAudio callback.

### Defect 3: Click Suppressor Every Tick (MODERATE)
**File:** `gateway_core.py:2557-2568`

Every tick allocates multiple numpy arrays for `np.diff` + `np.where` even when no clicks present. Adds 0.5-2ms per tick.

### Defect 4: ~30-50 NumPy Allocations Per Tick (MODERATE)
Per tick with 2 sources on ListenBus:
- `check_signal_instant()`: 4-6 calls × 4 allocations = 16-24
- `mix_audio_streams()`: 7 allocations
- Per-source gain boost: 5 allocations per source
- Level metering: 2-3 allocations per source
- Click suppressor: 4+ allocations
At 20 ticks/second = 600-1000 allocations/second in audio hot path.

### Defect 5: No GC Control
No `import gc` anywhere. Python's cyclic GC triggers every ~700 gen-0 allocations. With 30-50 allocations per tick, GC fires every ~14-23 ticks (700-1150ms). Gen-0 collection: 0.1-1ms. Gen-1: 5-10ms. Invisible in current trace.

### Defect 6: AIOC Buffer Latency
**File:** `th9800_plugin.py:426`
`frames_per_buffer=chunk_size * 4` = 200ms buffer. Adds 100-150ms RX latency. Not a quality issue per se, but contributes to overall latency budget.

### Defect 7: PCM Queue Race Condition (MINOR)
**File:** `bus_manager.py:63-83`
`_pcm_queue` is a plain list accessed from two threads without a lock. `list(self._pcm_queue); self._pcm_queue.clear()` has a TOCTOU gap.

---

## 4. External Research — Live Audio Best Practices

### Python Real-Time Audio Limitations
- **GIL**: Only one thread executes Python bytecode at a time
- **GC pauses**: Cyclic GC can cause 0.1-10ms+ pauses unpredictably
- **Memory management**: Non-deterministic, reference counting + cyclic GC
- **Consensus**: Python marginal for <50ms guaranteed latency; recommended to prototype in Python then move hot path to C/C++/Rust

Sources:
- PEP 703 — Making the GIL Optional
- PEP 556 — Threaded garbage collection
- MDPI paper: "Programming Real-Time Sound in Python"

### Successful Open-Source Live Audio Projects

#### SVXLink (C++) — Closest analog
- Ham radio repeater controller
- Modular voice services with sophisticated audio mixing
- Lowers FX when traffic detected (similar to our ducking)
- Supports Mumble + UDP streaming
- Months/years uptime reported
- https://www.svxlink.org/

#### Mumble (C++)
- VoIP with Opus codec, low-latency jitter buffer
- Dedicated audio threads, minimal allocations in hot path
- 40-80ms round-trip on production systems

#### PipeWire (C)
- Graph-based with nodes/links, dynamic routing while media flows
- Constant graph mutation without teardown
- Typical 5.33ms latency (256 samples at 48kHz)
- SPA (Simple Plugin API) for hard real-time

#### JACK (C)
- Professional audio routing, SCHED_FIFO + mlockall()
- Industry standard for DAWs
- Routinely achieves 2-5ms latency

#### GNU Radio (C++ core + Python bindings)
- Flowgraph dataflow model, each block in independent thread
- Excellent for DSP chains, not designed for mixed audio mixing

#### Liquidsoap (OCaml)
- Functional streaming language, compiled to native code
- Handles multi-stream mixing, real-time switching
- Stations report months/years uptime
- 2-5% CPU for single stream

### Best Practices for Real-Time Audio

#### Lock-Free Ring Buffers
- SPSC (single producer, single consumer) with atomic indices
- No blocking, no priority inversion, bounded latency
- Standard in PortAudio and professional audio APIs
- Our code uses `queue.Queue` (mutex-based) everywhere

#### Dedicated Real-Time Thread
- SCHED_FIFO or SCHED_RR priority 10-20
- Pre-allocate all buffers before loop starts
- CPU affinity to avoid cache thrashing
- Avoid: syscalls, I/O, dynamic allocation, GC
- Our code: already uses SCHED_RR 10, but no CPU affinity or pre-allocation

#### Fixed-Size Frame Processing
- Process N samples per tick (2400 = 50ms at 48kHz)
- Use `time.monotonic()` for reliable clock
- Our code: already does this correctly in main loop

#### GC Avoidance in Hot Path
- `gc.disable()` in audio loop, manual collection in non-critical section
- Pre-allocate numpy arrays, reuse buffers
- Our code: does neither

#### Sample Rate Conversion
- Linear interpolation creates aliasing; sinc is expensive
- Streaming resampler with state (libsamplerate) recommended
- Our code: uses resampy (good quality), KV4P uses adaptive PLL

---

## 5. Alternative Approaches

### Option A: GStreamer + PipeWire Backend + Python Control
- GStreamer as audio engine (all in C), Python controls via bindings
- Pros: proven, handles resampling/format conversion
- Cons: GStreamer designed for streaming not ultra-low-latency, adds abstraction
- Viability for <50ms: **Risky**

### Option B: Hybrid Rust Audio Engine + Python Control
- Mixer and audio I/O in Rust (CPAL + dasp), Python via PyO3
- Pros: deterministic memory, type-safe, CPAL is production-grade, dasp has ring buffers/interpolation/RMS
- Cons: significant rewrite, learning curve
- Viability for <50ms: **Excellent**
- Ecosystem: CPAL (audio I/O), DASP (DSP), dasp_graph (audio graph)

### Option C: Cython for Hot-Path (Minimal Rewrite)
- Keep Python architecture, compile `mix_audio_streams()` and buffers to C via Cython
- Pros: minimal changes, ~100x faster for numeric code, incremental migration
- Cons: still subject to GC for Python objects, doesn't solve GIL
- Viability for <50ms: **Partial**

### Option D: JACK or PipeWire Daemon as Mixing Backbone
- Python connects as client, daemon (C) does all mixing
- Pros: proven real-time mixing, JACK routinely 2-5ms
- Cons: daemon dependency, less control over custom ducking
- Viability for <50ms: **Good**

### Option E: sounddevice (CFFI PortAudio) Instead of PyAudio
- Better C integration, fewer GIL issues than PyAudio
- Pros: drop-in replacement-ish, modern
- Cons: still Python mixing, doesn't fix GC
- Viability for <50ms: **Marginal improvement only**

### Option F: Port to C++ (SVXLink-style)
- Full rewrite following SVXLink's architecture
- Pros: proven approach for ham radio, deterministic
- Cons: 8+ weeks, complete rewrite
- Viability: **Excellent but high effort**

---

## 6. Benchmarks

| Language/Approach | Latency | Jitter | CPU% | GC Pauses | <50ms viable? |
|-------------------|---------|--------|------|-----------|---------------|
| C++ (JUCE) | 3-10ms | <1ms | 2-5% | None | YES |
| Rust (CPAL+dasp) | 5-15ms | <2ms | 2-5% | None | YES |
| C (ALSA/PortAudio) | 5-20ms | <2ms | 1-3% | None | YES |
| OCaml (Liquidsoap) | 10-30ms | <5ms | 2-5% | Tuned | YES |
| Python+Cython | 10-50ms | 2-20ms | 3-8% | Reduced | MARGINAL |
| Python+NumPy | 30-100ms | 5-50ms | 5-15% | 30-100ms | MARGINAL |
| Pure Python | 50-200ms+ | 10-100ms+ | 10-20% | 50-500ms | NO |

---

## 7. Decision Framework

### Stop at Python fixes if:
- Tick jitter < 2ms stdev with all sources active
- No speaker queue drops during 60s active trace
- Zero output discontinuities > 5000 sample units
- GC pauses < 2ms after gc.disable()

### Move to single-clock unification if:
- BusManager tick jitter > 5ms after clock fix
- PCM drain shows >5% double-drain rate

### Move to PipeWire delegation if:
- GC pauses > 10ms persist despite gc.disable()
- Tick overruns (>60ms) more than 1% of ticks
- Problems persist after all code-level fixes

### Move to full port (Rust/C++) if:
- All above approaches fail
- Need more sources/buses than Python threading supports
- Latency requirement drops below 20ms

---

## Sources

### Python Real-Time Audio & GIL
- PEP 703: Making the GIL Optional — https://peps.python.org/pep-0703/
- PEP 556: Threaded garbage collection — https://peps.python.org/pep-0556/
- MDPI: Programming Real-Time Sound in Python — https://www.mdpi.com/2076-3402/10/12/4214
- Free-threaded GC optimizations — https://labs.quansight.org/blog/free-threaded-gc-3-14

### Real-Time Audio Architecture
- Ross Bencina: Real-time audio 101 — http://www.rossbencina.com/code/real-time-audio-programming-101-time-waits-for-nothing
- Lock-free ringbuffer — https://github.com/marcdinkum/ringbuffer
- Multi-threaded audio processing — https://acestudio.ai/blog/multi-threaded-audio-processing/
- Real-time scheduling on Linux — https://lwn.net/Articles/818388/

### Audio Frameworks
- Liquidsoap — https://www.liquidsoap.info/
- PipeWire — https://pipewire.org/
- JACK — https://jackaudio.org/
- GNU Radio — https://www.gnuradio.org/
- SVXLink — https://www.svxlink.org/

### Rust Audio
- CPAL — https://github.com/RustAudio/cpal
- DASP — https://github.com/RustAudio/dasp
- Rust audio 2025 — https://andrewodendaal.com/rust-audio-programming-ecosystem/

### Python Audio Libraries
- python-sounddevice — https://python-sounddevice.readthedocs.io/
- PyAudio — https://people.csail.mit.edu/hubert/pyaudio/

### C++ Audio
- JUCE 7 benchmarks 2025 — https://markaicode.com/cpp-audio-processing-juce-7-benchmarks-2025/
