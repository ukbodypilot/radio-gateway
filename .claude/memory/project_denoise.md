---
name: Radio Gateway v3.3 denoise architecture + empirical constants
description: Per-bus selectable denoise engines (RNNoise / DeepFilterNet 3) with off-tick worker threads, measured algorithmic delays, vendored ONNX model, and the install-environment constraints that ruled out the PyPI route.
type: project
originSessionId: 0f1aa1d1-941b-4fe3-bef5-83a416b4760d
---
**Shipped 2026-04-19** as v3.3.0 (commit `67b813f`; tag `v3.3.0` on GitHub).

## Architecture
- `audio_util.DenoiseStream` duck-type with two implementations: `_RNNoiseStream` (pyrnnoise, ~1 ms/frame) and `_DFN3Stream` (ONNX via onnxruntime, ~22 ms/frame).
- Factory: `make_denoise_stream(engine)`; registry `DENOISE_ENGINE_IDS = ('rnnoise', 'deepfilternet')`.
- Per-bus selection lives on `AudioProcessor.dfn_engine` + `dfn_mix` + `dfn_atten_db`. Swappable live via routing-page pill + HTTP `set_dfn_engine`/`set_dfn_mix`/`set_dfn_atten` or MCP `bus_set_denoise_engine`/`bus_set_denoise_atten`.
- **Off-tick processing (D13)** — AudioProcessor owns a daemon thread `Denoise-<bus_id>` that drains `_dn_queue_in`, runs `process_mix`, pushes to `_dn_queue_out`. `_apply_dfn` is a queue shuttle on the bus tick (0.01 ms/call). One tick (~50 ms) extra latency on listener audio in exchange for tick-time stability.
- **Transcription ASR path is NOT double-denoised** — D7 collapsed the duplicate path. Transcription sink inherits whatever the bus already produced.

## Empirically measured constants
Measured with impulse probes (2026-04-19). DO NOT trust upstream defaults:
- **RNNoise algorithmic delay = 960 samples (20 ms)** — not 0 as I'd assumed.
- **DFN3 algorithmic delay = 1440 samples (30 ms)** — not `fft-hop = 480` as the reference impl's trim suggests. The DeepFiltering stage adds its own latency on top of the STFT analysis.
- These are `DenoiseStream.dry_delay_samples` class attrs. `_mix_with_dry_delay` uses them to phase-align wet/dry so the mix doesn't comb-filter (the "chorus/reverb smear" symptom).
- **DFN3 ORT intra_op_num_threads optimum = 2**. Benchmarked on Haswell i5 (4-core): `intra=1: 4.07 ms/frame; intra=2: 3.54 ms; intra=3: 4.60 ms; intra=4: 7.30 ms`. Sequential GRU graph regresses past 2.
- **DFN3 RTF ≈ 0.4** on Haswell i5 (500 ms processing time per 1 s of audio), well inside real-time.
- **DFN3 attenuation cap default = 18 dB** (was `atten_lim_db=0` = unlimited). 0 causes audible pumping; 15–25 dB is the usable range.

## Model vendoring
- `tools/models/dfn3/denoiser_model.onnx` (16 MB, SHA256 `fe5eb64fa2e4154c83f8e4935e82871c850c154387ee892e0ab65fe179e7d8c9`). Shipped in the repo — no runtime download.
- Source: `yuyun2000/SpeechDenoiser` repo's 48k/denoiser_model.onnx, which is a stateful re-export of Rikorose's DFN3-LL. Model weights are MIT-licensed from DeepFilterNet.
- Signature: `input_frame[480] + states[45304] + atten_lim_db[]` → `enhanced_audio_frame[480] + new_states[45304] + lsnr[1]`.
- `_dfn3_ensure_model()` order: (1) bundled repo copy, (2) user cache `~/.cache/radio-gateway/dfn3/`, (3) async background download from upstream (fallback only).

## Install-environment constraints (DO NOT retry these)
Dell OptiPlex 3020 runs **Python 3.14 / numpy 2.x / onnxruntime 1.24 / torch 2.11 / silero-vad 6.2 / scipy 1.17**. All confirmed working together.

- ❌ **`deepfilternet` PyPI package** — pins `numpy<2.0`. Downgrading would break silero-vad, moonshine, scipy, torch. **Hard block**. This is what ate a prior session before the ONNX route was found.
- ❌ **`deep-filter` Rust binary** — file-in → file-out only, no stdin streaming mode. LADSPA plugin variant exists but requires per-bus PipeWire filter-chain wiring, wrong integration layer.
- ✅ **Stateful DFN3 ONNX via onnxruntime** — zero new runtime deps, no numpy conflict.

## Telemetry
- `transcription_status` MCP tool surfaces `feed` block: `queue_depth`, `peak_qd`, `dropped_full`, `proc_mean_ms`, `proc_max_ms`, `per_stream_mean_ms`, `worker_count`.
- Transcribe page feed-health row mirrors this live.
- Per-bus denoise timing is NOT currently surfaced — if regression diagnosis is needed, add `denoise_proc_ms` to AudioProcessor stats.

## Critical files
- `audio_util.py` — engine classes, `_mix_with_dry_delay`, `AudioProcessor`, off-tick worker.
- `transcriber.py` — `_StreamState`, per-stream feed workers, Moonshine repetition-suppressed decoder (`_moonshine_generate_no_repeat`).
- `bus_manager.py` — `_load_and_create_busses` eager-constructs denoise streams at startup (triggers ONNX warmup synchronously); `sync_listen_bus` distinguishes `tuner_needed` (any bus) vs `should_be_on` (listen bus only).
- `web_server.py` — `set_dfn_engine`/`set_dfn_atten`/`set_dfn_mix` handlers; 'recording' sink removed.
- `web_pages/routing.html` — engine pill + atten cap input per bus.
- `gateway_mcp.py` — `bus_set_denoise_engine` / `bus_set_denoise_atten` tools.
