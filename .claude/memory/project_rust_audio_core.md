---
name: Rust audio core (future path, not started)
description: Architecture notes for optional Rust replacement of BusManager audio core — documented but deferred
type: project
---

## Rust Audio Core — Future Path (documented 2026-04-07, not started)

**Decision:** Defer. Python audio core works well. Document the path for if/when it's needed.

### Why it's viable after 3.0
- All audio mixing/processing/delivery in one place (BusManager thread)
- Consistent PCM format (16-bit 48kHz mono)
- Queue-based output already exists (PCM/MP3/SDR rebroadcast)
- Clean separation: BusManager = audio core, gateway_core = control plane

### What a Rust core would replace
The "hot path" inside BusManager: bus ticks (ListenBus ducking/mixing, SoloBus TX, repeater cross-link), AudioProcessor (IIR filters, gate), click suppression, fade ramps.

### Prerequisites (Python-side, ~2-3 sessions)
1. **Source input queues** — sources push PCM into shared ring buffers instead of BusManager calling `get_audio()` directly
2. **Sink output queues** — BusManager deposits to per-sink output queues instead of calling `gw.mumble.add_sound()` etc. directly; Python consumer threads drain them
3. Result: middle becomes pure function `input buffers -> output buffers`, replaceable via PyO3

### Benefits
- Timing precision: no GIL/GC jitter, sub-microsecond tick consistency
- CPU: 10-50x faster for mixing/DSP math, frees headroom for more buses or cheaper hardware
- Latency floor: could tick at 5-10ms instead of 50ms for tighter PTT response
- Removes need for SCHED_RR, GC disable, nice hacks

### Downsides
- Two languages, two build systems, harder debugging (segfaults vs tracebacks)
- Slower iteration (compile step vs edit-restart)
- Harder for others to contribute (Rust learning curve)
- Risk of reintroducing ducking/timing bugs during port
- Diminishing returns: current Python core already achieves <2ms stdev on all streams

### When to reconsider
- Running many simultaneous buses and hitting CPU limits
- Targeting Raspberry Pi or lower-powered hardware
- Needing sub-10ms tick latency for real-time repeater modes
- Someone with Rust experience joins the project
