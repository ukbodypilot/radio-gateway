#!/usr/bin/env python3
"""Shared audio utilities — level metering, AudioProcessor, CW generation.

Extracted from audio_sources.py to eliminate duplicate level-metering code
across plugins and sources.
"""

import math as _math
import numpy as np


# ── Neural denoise — engine-abstracted ──────────────────────────────────────
#
# Two engines are available:
#   - 'rnnoise'       : tiny (~100 KB model), fast (<1 ms/frame), aggressive
#                       broadband hiss cut. Default for existing deployments.
#   - 'deepfilternet' : DFN3 (~9 MB ONNX), better speech preservation and
#                       narrowband noise removal, ~2–4 ms/frame on Haswell.
#
# Both implement the DenoiseStream duck-type:
#     native_rate: int                              (48000 for both)
#     process(samples_i16: np.ndarray) -> np.ndarray
#     close() -> None
#
# Use make_denoise_stream('rnnoise'|'deepfilternet') to construct.
# The JSON/API surface still uses the legacy key 'dfn' — it's no longer a
# literal acronym but kept for config compatibility.

_RNN_MOD = None   # lazy-loaded module handle (shared across streams)


def _load_rnnoise():
    """Load pyrnnoise.rnnoise as a standalone module, bypassing its package
    __init__.py. Returns the module on success, None if unavailable."""
    global _RNN_MOD
    if _RNN_MOD is not None:
        return _RNN_MOD
    try:
        import importlib.util, os
        spec = importlib.util.find_spec('pyrnnoise')
        if spec is None or not spec.submodule_search_locations:
            return None
        sub_path = os.path.join(spec.submodule_search_locations[0], 'rnnoise.py')
        sub_spec = importlib.util.spec_from_file_location('_rnnoise_lowlevel', sub_path)
        mod = importlib.util.module_from_spec(sub_spec)
        sub_spec.loader.exec_module(mod)
        _RNN_MOD = mod
        return mod
    except Exception as e:
        print(f"[DFN] rnnoise unavailable: {e}")
        return None


class _RNNoiseStream:
    """Per-source denoise state. Feed int16 PCM bytes in any length — the
    stream buffers to the native 480-sample (10 ms @ 48 kHz) frame size.

    Conforms to the DenoiseStream duck-type used by make_denoise_stream().
    """

    native_rate = 48000
    # Algorithmic delay of the denoised output relative to the input. Used
    # by process_mix() to delay the dry signal before blending so wet+dry
    # stay phase-aligned (otherwise the mix acts as a comb filter and
    # introduces an audible chorus/reverb smear). RNNoise processes per
    # 480-sample frame without STFT lookahead, so delay ≈ 0.
    dry_delay_samples = 0

    def __init__(self):
        mod = _load_rnnoise()
        if mod is None:
            raise RuntimeError("rnnoise library not available")
        self._mod = mod
        self._state = mod.create()
        self._buf = np.empty(0, dtype=np.int16)
        self._dry_delay_buf = np.empty(0, dtype=np.int16)  # used by process_mix
        self.last_prob = 0.0

    def close(self):
        if self._state is not None:
            try:
                self._mod.destroy(self._state)
            except Exception:
                pass
            self._state = None

    def __del__(self):
        self.close()

    def process(self, samples_i16):
        """Process a numpy int16 mono array. Returns denoised int16 array
        of the same length. Residual (<1 frame) is carried over internally."""
        if samples_i16.size == 0:
            return samples_i16

        frame_size = self._mod.FRAME_SIZE
        buf = np.concatenate([self._buf, samples_i16]) if self._buf.size else samples_i16
        n_frames = buf.size // frame_size

        if n_frames == 0:
            self._buf = buf.copy()
            return np.empty(0, dtype=np.int16)

        out_chunks = []
        last_prob = self.last_prob
        for i in range(n_frames):
            frame = buf[i * frame_size : (i + 1) * frame_size]
            denoised, prob = self._mod.process_mono_frame(self._state, frame)
            out_chunks.append(denoised)
            last_prob = float(prob)

        self._buf = buf[n_frames * frame_size :].copy()
        self.last_prob = last_prob
        return np.concatenate(out_chunks)

    def process_mix(self, samples_i16, mix):
        """Process + wet/dry blend with dry-path delay compensation.

        Calls process() to get the wet output, delays the dry path by
        self.dry_delay_samples so the two are phase-aligned, and returns
        a length-matched int16 blend. mix: 0.0 dry, 1.0 wet.
        """
        return _mix_with_dry_delay(self, samples_i16, mix)


def _mix_with_dry_delay(stream, samples_i16, mix):
    """Shared wet/dry blender used by every DenoiseStream subclass.

    Keeps a persistent dry-delay buffer sized by stream.dry_delay_samples
    so the wet stream's algorithmic latency (DFN3 ≈ 10 ms) is matched
    on the dry path — without this, the blend is literally a comb filter
    and the signal gets a chorus/reverb smear.
    """
    denoised = stream.process(samples_i16)
    if mix <= 0.001:
        # All dry — but the wet path still needs to advance (it's stateful),
        # which stream.process() already did. Just return the (undelayed) dry.
        return samples_i16

    # Maintain a dry FIFO that mirrors the wet path's latency. New input in,
    # delayed samples out, 1:1 correspondence with the denoised output we
    # just got back.
    delay_n = getattr(stream, 'dry_delay_samples', 0)
    if delay_n <= 0:
        dry_aligned = samples_i16
    else:
        if not hasattr(stream, '_dry_delay_buf') or stream._dry_delay_buf.size == 0:
            # First call: prime with silence so we emit something the same
            # length as the wet stream's residue handling.
            stream._dry_delay_buf = np.zeros(delay_n, dtype=np.int16)
        combined = np.concatenate([stream._dry_delay_buf, samples_i16])
        # Take the OLDEST `len(denoised)` samples off the front as the
        # dry-aligned slice (those correspond to the wet frames we just
        # emitted). Retain the remaining delay-sized tail for next call.
        n_out = denoised.size
        if n_out == 0:
            # Wet produced nothing (sub-frame residue); keep full tail.
            stream._dry_delay_buf = combined
            return np.empty(0, dtype=np.int16)
        if n_out > combined.size:
            # Can't happen with a properly-primed buffer, but be defensive.
            dry_aligned = combined
            stream._dry_delay_buf = np.empty(0, dtype=np.int16)
        else:
            dry_aligned = combined[:n_out]
            stream._dry_delay_buf = combined[n_out:]

    if mix >= 0.999:
        return denoised
    n = min(dry_aligned.size, denoised.size)
    mixed = (dry_aligned[:n].astype(np.int32) * int((1.0 - mix) * 65536)
             + denoised[:n].astype(np.int32) * int(mix * 65536)) >> 16
    return np.clip(mixed, -32768, 32767).astype(np.int16)


# ── DeepFilterNet 3 (DFN3) streaming via onnxruntime ────────────────────────
#
# Uses the stateful single-file ONNX export from yuyun2000/SpeechDenoiser
# (48k/denoiser_model.onnx, 16 MB). Model weights are MIT-licensed
# DeepFilterNet3; the re-exported ONNX wraps all STFT/ERB/GRU recurrence into
# one graph with a flat 45304-float state vector carried across calls.
#
# Signature:
#   inputs:  input_frame[480]  states[45304]  atten_lim_db[]
#   outputs: enhanced_audio_frame[480]  new_states[45304]  lsnr[1]
#
# Runs on existing onnxruntime — no new Python deps, no torch wheel bloat,
# no numpy conflict. ~2–4 ms per 10 ms frame on Haswell i5.

_DFN3_MODEL_URL = (
    'https://github.com/yuyun2000/SpeechDenoiser/raw/main/48k/denoiser_model.onnx'
)
_DFN3_MODEL_SHA256 = (
    'fe5eb64fa2e4154c83f8e4935e82871c850c154387ee892e0ab65fe179e7d8c9'
)
_DFN3_MODEL_SIZE = 16_104_687       # bytes — paired with SHA for sanity
_DFN3_STATE_SIZE = 45304            # fixed by the ONNX graph
_DFN3_FRAME_SIZE = 480              # 10 ms @ 48 kHz (model hop_size)


def _dfn3_bundled_path():
    """Path to the model bundled in the repo (preferred — no network)."""
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'tools', 'models', 'dfn3', 'denoiser_model.onnx')


def _dfn3_model_path():
    """Path to the cached ONNX; does NOT download (see _dfn3_ensure_model).

    Returns the bundled copy if it exists, otherwise the user cache path
    (which may or may not exist yet).
    """
    import os
    bundled = _dfn3_bundled_path()
    if os.path.exists(bundled) and os.path.getsize(bundled) == _DFN3_MODEL_SIZE:
        return bundled
    cache_dir = os.path.expanduser('~/.cache/radio-gateway/dfn3')
    return os.path.join(cache_dir, 'denoiser_model.onnx')


def _dfn3_ensure_model():
    """Return a path to a usable DFN3 ONNX.

    Preference order:
      1. Bundled copy in repo (tools/models/dfn3/denoiser_model.onnx) —
         always preferred, no network, no cache side-effects.
      2. User cache (~/.cache/radio-gateway/dfn3/) if already downloaded.
      3. Download from GitHub into the user cache as a last resort (for
         install paths that didn't ship the bundled model).

    Raises on network failure or SHA mismatch so construction propagates
    an error the factory can catch."""
    import os, hashlib, urllib.request
    # 1. Bundled.
    bundled = _dfn3_bundled_path()
    if os.path.exists(bundled) and os.path.getsize(bundled) == _DFN3_MODEL_SIZE:
        return bundled
    # 2. Cache.
    path = os.path.expanduser('~/.cache/radio-gateway/dfn3/denoiser_model.onnx')
    if os.path.exists(path):
        if os.path.getsize(path) == _DFN3_MODEL_SIZE:
            return path
        print(f"  [DFN3] Cached model size mismatch; redownloading")
        try:
            os.remove(path)
        except Exception:
            pass
    # 3. Download.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.part'
    print(f"  [DFN3] Downloading model ({_DFN3_MODEL_SIZE//1024//1024} MB) from {_DFN3_MODEL_URL}")
    try:
        urllib.request.urlretrieve(_DFN3_MODEL_URL, tmp)
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        raise RuntimeError(f"DFN3 download failed: {e}")
    # Verify SHA256 — model artefacts may change upstream; failing loud is
    # better than silently running on an unexpected graph.
    h = hashlib.sha256()
    with open(tmp, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    if h.hexdigest() != _DFN3_MODEL_SHA256:
        try: os.remove(tmp)
        except Exception: pass
        raise RuntimeError(
            f"DFN3 SHA mismatch: got {h.hexdigest()} expected {_DFN3_MODEL_SHA256}. "
            f"The upstream model may have been replaced — update _DFN3_MODEL_SHA256."
        )
    os.rename(tmp, path)
    print(f"  [DFN3] Model cached at {path}")
    return path


class _DFN3Stream:
    """Per-source DeepFilterNet 3 denoise stream.

    Feed int16 PCM arrays of any length; internally buffers to 480-sample
    frames, runs each through the ONNX graph with a persistent state vector,
    emits 480-sample enhanced frames back. ~10 ms intrinsic algorithmic delay.

    The ONNX session is shared class-wide; each instance carries its own
    state vector so multiple buses / the ASR path don't bleed into each other.
    """

    native_rate = 48000
    # DFN3 uses a 960-sample STFT with 480-sample hop. The first `fft-hop`
    # samples of the model's output are the "look-behind" from the analysis
    # window, so the output frame at time T corresponds to the INPUT from
    # T - 480 samples. Dry must be delayed by the same 480 samples before
    # blending; without that the mix becomes a comb filter (chorus/reverb
    # smear on the dry component).
    dry_delay_samples = 480

    _sess = None          # shared onnxruntime InferenceSession
    _lock = None          # init lock — build session once across threads
    _download_thread = None   # background download worker (one-shot)

    @classmethod
    def _kick_background_download(cls):
        """Start the model download on a background thread if not already
        running or cached. Non-blocking — safe to call from the feed worker.
        Returns True if the model file is ready NOW, False if we're still
        downloading (caller should treat DFN as unavailable for the moment).

        Bundled copy in the repo short-circuits this entirely.
        """
        import os, threading
        bundled = _dfn3_bundled_path()
        if os.path.exists(bundled) and os.path.getsize(bundled) == _DFN3_MODEL_SIZE:
            return True
        cached = os.path.expanduser('~/.cache/radio-gateway/dfn3/denoiser_model.onnx')
        if os.path.exists(cached) and os.path.getsize(cached) == _DFN3_MODEL_SIZE:
            return True
        if cls._lock is None:
            cls._lock = threading.Lock()
        with cls._lock:
            if cls._download_thread is not None and cls._download_thread.is_alive():
                return False
            def _bg():
                try:
                    _dfn3_ensure_model()
                except Exception as e:
                    print(f"  [DFN3] Background download failed: {e}")
            cls._download_thread = threading.Thread(
                target=_bg, daemon=True, name='DFN3-download')
            cls._download_thread.start()
        return False

    @classmethod
    def _ensure_session(cls):
        if cls._sess is not None:
            return
        # Refuse to block the caller on download — that's the bus tick's
        # nightmare. Kick off a background fetch instead and fail fast so
        # the denoise engine reports "unavailable yet" until the file lands.
        if not cls._kick_background_download():
            raise RuntimeError('DFN3 model downloading — try again shortly')
        import threading
        if cls._lock is None:
            cls._lock = threading.Lock()
        with cls._lock:
            if cls._sess is not None:
                return
            path = _dfn3_ensure_model()
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            cls._sess = ort.InferenceSession(
                path, sess_options=opts, providers=['CPUExecutionProvider'])
            print(f"  [DFN3] ONNX session ready")

    def __init__(self):
        self._ensure_session()
        self._state = np.zeros(_DFN3_STATE_SIZE, dtype=np.float32)
        # atten_lim_db is a 0-d tensor — ORT requires an ndarray, not a
        # bare numpy scalar. 0 = no attenuation cap.
        self._atten_lim = np.array(0.0, dtype=np.float32)
        self._buf = np.empty(0, dtype=np.int16)
        # FIFO for the dry path used by process_mix() — keeps wet/dry in
        # phase so the blend doesn't comb-filter. Primed with silence on
        # first call (see _mix_with_dry_delay).
        self._dry_delay_buf = np.empty(0, dtype=np.int16)
        self.last_lsnr = 0.0

    def close(self):
        # Nothing to release per-instance — the shared session stays alive.
        # The state array and buffer are normal numpy objects, GC handles them.
        self._state = None
        self._buf = np.empty(0, dtype=np.int16)

    def __del__(self):
        self.close()

    def process(self, samples_i16):
        """Same contract as _RNNoiseStream.process — int16 in, int16 out,
        residue <1 frame buffered internally for the next call."""
        if samples_i16.size == 0 or self._state is None:
            return np.empty(0, dtype=np.int16)
        buf = np.concatenate([self._buf, samples_i16]) if self._buf.size else samples_i16
        n_frames = buf.size // _DFN3_FRAME_SIZE
        if n_frames == 0:
            self._buf = buf.copy()
            return np.empty(0, dtype=np.int16)

        out_chunks = []
        last_lsnr = self.last_lsnr
        _sess = type(self)._sess
        _run = _sess.run
        _state = self._state
        _atten = self._atten_lim
        for i in range(n_frames):
            frame_i16 = buf[i * _DFN3_FRAME_SIZE : (i + 1) * _DFN3_FRAME_SIZE]
            frame_f32 = frame_i16.astype(np.float32) / 32768.0
            enhanced, _state, lsnr = _run(
                None,
                {'input_frame': frame_f32, 'states': _state, 'atten_lim_db': _atten},
            )
            # Back to int16 with clip — the model output is float32 in roughly
            # [-1, 1] but nothing stops occasional overshoot.
            out_chunks.append(np.clip(enhanced * 32768.0, -32768, 32767).astype(np.int16))
            last_lsnr = float(lsnr[0])

        self._state = _state
        self._buf = buf[n_frames * _DFN3_FRAME_SIZE :].copy()
        self.last_lsnr = last_lsnr
        return np.concatenate(out_chunks) if out_chunks else np.empty(0, dtype=np.int16)

    def process_mix(self, samples_i16, mix):
        """Process + wet/dry blend with DFN's 480-sample dry-path delay
        compensation. See _mix_with_dry_delay for mechanics."""
        return _mix_with_dry_delay(self, samples_i16, mix)


# Engine registry. Missing entries fall back cleanly via
# make_denoise_stream() returning None.
_DENOISE_ENGINES = {
    'rnnoise': _RNNoiseStream,
    'deepfilternet': _DFN3Stream,
}

# Legal engine identifiers — consumers should validate user input against
# this set before calling make_denoise_stream so typos become explicit errors
# rather than silent "denoise turned off".
DENOISE_ENGINE_IDS = ('rnnoise', 'deepfilternet')


def make_denoise_stream(engine='rnnoise'):
    """Construct a denoise stream for the requested engine.

    Returns a DenoiseStream instance, or None if the engine is unknown or
    the underlying library failed to load. Callers should handle None by
    skipping denoise (pass-through) rather than erroring — the audio path
    should never die because a denoise lib went missing.
    """
    cls = _DENOISE_ENGINES.get(engine)
    if cls is None:
        print(f"  [Denoise] unknown engine '{engine}' — skipping")
        return None
    try:
        return cls()
    except Exception as e:
        print(f"  [Denoise] {engine} unavailable: {e}")
        return None


# ── Level metering ──────────────────────────────────────────────────────────

def pcm_rms(pcm_bytes):
    """Compute RMS of 16-bit signed PCM bytes. Returns float."""
    arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    if len(arr) == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr * arr)))


def rms_to_level(rms, gain=1.0):
    """Convert RMS to 0-100 display level (dB-scaled, 60 dB range).

    *gain* is an optional display-gain multiplier (1.0 = unity).
    """
    if rms <= 0:
        return 0
    db = 20.0 * _math.log10(rms / 32767.0)
    raw = max(0, min(100, (db + 60) * (100 / 60)))
    return min(100, int(raw * gain))


def update_level(current, new_level, attack=1.0, decay=0.3):
    """Smooth a level value: instant attack, exponential decay.

    Default behaviour (attack=1.0, decay=0.3):
      - If *new_level* > *current*, jump to *new_level* immediately.
      - Otherwise blend: current * (1-decay) + new_level * decay.
    Returns int.
    """
    if new_level > current * attack:
        return int(new_level)
    return int(current * (1 - decay) + new_level * decay)


def pcm_level(pcm_bytes, current=0, gain=1.0):
    """One-call convenience: pcm → rms → level → smoothed.

    Returns updated level (int 0-100).
    """
    rms = pcm_rms(pcm_bytes)
    lv = rms_to_level(rms, gain)
    return update_level(current, lv)


def pcm_db(pcm_bytes):
    """Return dB level of PCM data (for threshold checks, not display)."""
    rms = pcm_rms(pcm_bytes)
    if rms <= 0:
        return -100.0
    return 20.0 * _math.log10(rms / 32767.0)


def apply_gain(audio, gain):
    """Apply gain with tanh soft-clipping when gain > 1.

    Below unity: pure linear multiply (no distortion, full dynamics).
    Above unity: input normalised to [-1, 1], scaled by gain, passed through
    tanh, then scaled back. Soft-saturating — peaks approach ±1 asymptotically
    instead of flat-topping. Small-signal region stays near-linear so quiet
    passages are still boosted; only the loudest samples round off.

    Accepts `bytes` (int16 little-endian PCM) or a numpy int16 array; returns
    the same type. No-op when gain == 1.0 (caller shouldn't need to check).
    """
    if gain == 1.0:
        return audio
    _from_bytes = isinstance(audio, (bytes, bytearray, memoryview))
    arr_f32 = (np.frombuffer(audio, dtype=np.int16).astype(np.float32)
               if _from_bytes else audio.astype(np.float32))
    if gain > 1.0:
        out = np.tanh(arr_f32 / 32768.0 * gain) * 32768.0
    else:
        out = arr_f32 * gain
    out_i16 = np.clip(out, -32768, 32767).astype(np.int16)
    return out_i16.tobytes() if _from_bytes else out_i16


# ── AudioProcessor ──────────────────────────────────────────────────────────

class AudioProcessor:
    """Per-source audio processing chain with independent filter state.

    Each audio source (Radio, SDR1, SDR2, etc.) gets its own AudioProcessor
    instance so filters run independently with their own state (envelope,
    filter memory, etc.) and can be toggled per-source.
    """

    def __init__(self, name, config):
        self.name = name          # e.g. "radio", "sdr"
        self.config = config      # gateway Config object (for AUDIO_RATE, etc.)

        # Per-source enable flags (set from config or toggled at runtime)
        self.enable_hpf = False
        self.hpf_cutoff = 300         # Hz
        self.enable_lpf = False
        self.lpf_cutoff = 3000        # Hz
        self.enable_notch = False
        self.notch_freq = 1000        # Hz — target frequency
        self.notch_q = 30.0           # Q factor (higher = narrower notch)
        self.enable_noise_gate = False
        self.gate_threshold = -40     # dB
        self.gate_attack = 0.01       # seconds
        self.gate_release = 0.1       # seconds
        self.enable_dfn = False       # Neural denoise toggle. "dfn" is kept
                                      # as the config/API key for back-compat;
                                      # actual engine is picked via dfn_engine.
        self.dfn_engine = 'rnnoise'   # 'rnnoise' (default, aggressive) or
                                      # 'deepfilternet' (speech-preserving).
                                      # Swap via set_dfn_engine().
        self.dfn_mix = 0.5            # Wet/dry mix — 1.0 = fully denoised,
                                      # 0.0 = pass-through. RNNoise over-cuts
                                      # on radio audio, so default to 50%.
                                      # Per-bus override via routing cmd
                                      # 'dfn_mix' (see web_server.py).

        # Filter state (persists across audio chunks for continuity)
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        self.dfn_stream = None

    def set_dfn_engine(self, engine):
        """Switch denoise engine. Drops the existing stream so the next
        process() call reinitialises with the new backend."""
        if engine == self.dfn_engine:
            return
        if engine not in DENOISE_ENGINE_IDS:
            print(f"  [AudioProcessor] rejecting unknown dfn_engine '{engine}'")
            return
        self.dfn_engine = engine
        if self.dfn_stream is not None:
            try:
                self.dfn_stream.close()
            except Exception:
                pass
            self.dfn_stream = None

    def reset_state(self):
        """Reset all filter states (e.g. when source restarts)."""
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        if self.dfn_stream is not None:
            self.dfn_stream.close()
            self.dfn_stream = None

    def process(self, pcm_data):
        """Run the full processing chain on PCM data. Order:
        HPF → DFN → LPF → Notch → Noise Gate

        HPF runs before DFN so subsonic rumble is removed cheaply; LPF/notch
        run after DFN so the denoiser sees the full band.
        """
        if not pcm_data:
            return pcm_data

        processed = pcm_data

        if self.enable_hpf:
            processed = self._apply_hpf(processed)

        if self.enable_dfn:
            processed = self._apply_dfn(processed)

        if self.enable_lpf:
            processed = self._apply_lpf(processed)

        if self.enable_notch:
            processed = self._apply_notch(processed)

        if self.enable_noise_gate:
            processed = self._apply_noise_gate(processed)

        return processed

    def get_active_list(self):
        """Return list of active filter names for status display."""
        active = []
        if self.enable_noise_gate: active.append('Gate')
        if self.enable_hpf: active.append('HPF')
        if self.enable_dfn: active.append('DFN')
        if self.enable_lpf: active.append('LPF')
        if self.enable_notch: active.append('Notch')
        return active

    # --- Filter implementations ---

    def _apply_hpf(self, pcm_data):
        """First-order IIR high-pass filter."""
        try:
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.hpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * _math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b = np.array([alpha, -alpha], dtype=np.float64)
            a = np.array([1.0, -alpha], dtype=np.float64)

            if self.highpass_state is None:
                self.highpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.highpass_state = lfilter(b, a, samples, zi=self.highpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_lpf(self, pcm_data):
        """First-order IIR low-pass filter — cuts high-frequency hiss above cutoff."""
        try:
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.lpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * _math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = dt / (rc + dt)

            b = np.array([alpha], dtype=np.float64)
            a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)

            if self.lowpass_state is None:
                self.lowpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.lowpass_state = lfilter(b, a, samples, zi=self.lowpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_notch(self, pcm_data):
        """Second-order IIR notch (band-stop) filter — removes a specific frequency."""
        try:
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            sample_rate = self.config.AUDIO_RATE
            w0 = 2.0 * _math.pi * self.notch_freq / sample_rate
            bw = w0 / self.notch_q
            r = 1.0 - (bw / 2.0)
            r = max(0.0, min(r, 0.9999))  # clamp for stability

            cos_w0 = _math.cos(w0)
            b = np.array([1.0, -2.0 * cos_w0, 1.0], dtype=np.float64)
            a = np.array([1.0, -2.0 * r * cos_w0, r * r], dtype=np.float64)
            b = b / (1.0 + abs(1.0 - r))

            if self.notch_state is None:
                self.notch_state = lfilter_zi(b, a) * 0.0

            filtered, self.notch_state = lfilter(b, a, samples, zi=self.notch_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_dfn(self, pcm_data):
        """Neural denoise — engine picked by self.dfn_engine.

        Assumes 48 kHz mono int16 input — the gateway-wide AUDIO_RATE. If
        the chosen engine can't be loaded, silently pass-through so a missing
        dep doesn't kill the audio path.

        Output is a wet/dry mix via `dfn_mix` (1.0 = fully denoised, 0.0 =
        pass-through). RNNoise over-cuts radio audio at full wet so 0.5 is
        a safe default; DFN3 tolerates higher wet values.
        """
        try:
            if self.dfn_stream is None:
                self.dfn_stream = make_denoise_stream(self.dfn_engine)
                if self.dfn_stream is None:
                    # Factory already logged why; downgrade and stop trying.
                    self.enable_dfn = False
                    return pcm_data

            samples = np.frombuffer(pcm_data, dtype=np.int16)
            if samples.size == 0:
                return pcm_data

            wet = float(getattr(self, 'dfn_mix', 0.5))
            wet = max(0.0, min(1.0, wet))
            # process_mix handles the wet/dry blend with engine-specific
            # dry-path delay compensation so DFN3's 10 ms algorithmic delay
            # doesn't comb-filter the signal.
            mixed = self.dfn_stream.process_mix(samples, wet)
            if mixed.size == 0:
                # Sub-frame residue; emit dry so we don't drop audio.
                return pcm_data

            # Block-in = block-out contract for downstream sinks.
            if mixed.size < samples.size:
                mixed = np.concatenate([mixed, samples[mixed.size:]])
            elif mixed.size > samples.size:
                mixed = mixed[: samples.size]

            return mixed.astype(np.int16).tobytes()
        except Exception as e:
            print(f"[DFN] {self.name}: process error — {e}")
            return pcm_data

    def _apply_noise_gate(self, pcm_data):
        """Noise gate with attack/release envelope."""
        try:
            import array as _arr

            samples = _arr.array('h', pcm_data)
            if len(samples) == 0:
                return pcm_data

            threshold_db = self.gate_threshold
            threshold = 32767.0 * pow(10.0, threshold_db / 20.0)

            attack_samples = self.gate_attack * self.config.AUDIO_RATE
            release_samples = self.gate_release * self.config.AUDIO_RATE

            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1

            gated = []
            for sample in samples:
                level = abs(sample)

                if level > self.gate_envelope:
                    self.gate_envelope += (level - self.gate_envelope) * attack_coef
                else:
                    self.gate_envelope += (level - self.gate_envelope) * release_coef

                if self.gate_envelope > threshold:
                    gain = 1.0
                else:
                    ratio = self.gate_envelope / threshold if threshold > 0 else 0
                    gain = ratio * ratio

                gated.append(int(sample * gain))

            return _arr.array('h', gated).tobytes()
        except Exception:
            return pcm_data


# ── CW generation ───────────────────────────────────────────────────────────

_MORSE_TABLE = {
    'A': '.-',   'B': '-...', 'C': '-.-.', 'D': '-..',  'E': '.',
    'F': '..-.', 'G': '--.',  'H': '....', 'I': '..',   'J': '.---',
    'K': '-.-',  'L': '.-..', 'M': '--',   'N': '-.',   'O': '---',
    'P': '.--.', 'Q': '--.-', 'R': '.-.',  'S': '...',  'T': '-',
    'U': '..-',  'V': '...-', 'W': '.--',  'X': '-..-', 'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', '/': '-..-.', '-': '-....-',
}


def generate_cw_pcm(text, wpm=15, freq=700, sample_rate=48000):
    """Return int16 numpy array of CW audio for text. Standard PARIS timing."""
    dit_n = int(sample_rate * 1.2 / wpm)
    t = np.arange(dit_n) / sample_rate
    dit_tone = (np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    dah_tone = np.tile(dit_tone, 3)
    dit_sil  = np.zeros(dit_n,     dtype=np.int16)
    char_sil = np.zeros(3 * dit_n, dtype=np.int16)
    word_sil = np.zeros(7 * dit_n, dtype=np.int16)

    chunks = []
    for wi, word in enumerate(text.upper().split()):
        if wi:
            chunks.append(word_sil)
        for ci, ch in enumerate(word):
            if ci:
                chunks.append(char_sil)
            pattern = _MORSE_TABLE.get(ch, '')
            if not pattern:
                print(f"[CW] Warning: skipping unknown character {ch!r}")
                continue
            for ei, el in enumerate(pattern):
                if ei:
                    chunks.append(dit_sil)
                chunks.append(dit_tone if el == '.' else dah_tone)

    return np.concatenate(chunks) if chunks else np.zeros(dit_n, dtype=np.int16)
