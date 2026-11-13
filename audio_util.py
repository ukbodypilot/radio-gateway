#!/usr/bin/env python3
"""Shared audio utilities — level metering, AudioProcessor, CW generation.

Extracted from audio_sources.py to eliminate duplicate level-metering code
across plugins and sources.
"""

from log_util import log_exc
import math as _math
import time as _time
import numpy as np

# v3.5-B: numba JIT for the noise-gate inner loop. Numba is a transitive
# dep via moonshine (transcription), so it's already installed everywhere
# the gateway runs. Fall back to a slow Python loop if it isn't available
# for any reason (output is bit-identical either way).
try:
    from numba import njit as _njit
    _HAVE_NUMBA = True
except Exception:
    _HAVE_NUMBA = False
    def _njit(*args, **kwargs):
        # Pass-through decorator so the function is still callable; first
        # call paths through pure Python at much lower throughput.
        if len(args) == 1 and callable(args[0]):
            return args[0]
        def _wrap(fn):
            return fn
        return _wrap


@_njit(cache=True)
def _gate_loop(samples, threshold, attack_coef, release_coef, env_in):
    """JIT-compiled inner loop for AudioProcessor._apply_noise_gate.

    Same per-sample envelope follower + soft-knee gain as the pre-v3.5
    Python loop, but compiled. Returns (gated_int16_array, new_env)
    so the caller can roll the envelope into self.gate_envelope.
    """
    n = samples.shape[0]
    out = np.empty(n, dtype=np.int16)
    env = env_in
    for i in range(n):
        s = samples[i]
        level = abs(s)
        if level > env:
            env += (level - env) * attack_coef
        else:
            env += (level - env) * release_coef
        if env > threshold:
            gain = 1.0
        elif threshold > 0.0:
            ratio = env / threshold
            gain = ratio * ratio
        else:
            gain = 0.0
        # int16 conversion via int() preserves the original Python loop's
        # truncation-toward-zero semantics. Saturation isn't needed: gain
        # is in [0, 1] and s is already int16, so |s * gain| ≤ |s|.
        out[i] = np.int16(s * gain)
    return out, env


def _warmup_gate_loop():
    """Pay the numba JIT cost at gateway startup, not on the first audio tick.

    With `cache=True`, the compiled artefact lives in `__pycache__/`
    (.nbi + .nbc beside the .pyc). Cold compile is ~550 ms; cached load
    is ~290 ms. Either way we want to absorb it during init rather than
    on the first time a user toggles the noise gate, where it would
    show up as a multi-hundred-ms tick stall.

    Run in a background daemon thread so module import (which happens
    early in radio_gateway.py startup) doesn't block on it.

    EXPECT TO SEE SEVERAL THREADS NAMED AFTER THIS ONE, AND DON'T CHASE THEM.
    The Python thread below finishes and exits in well under a second, but
    numba brings up its native worker pool while compiling, and on Linux a
    freshly created thread inherits the creating thread's `comm` name until
    something renames it. So `top`/`/proc/<pid>/task` show ~3 threads with
    this name sitting idle for the life of the process while
    `threading.enumerate()` shows none — they are numba's workers, not leaked
    warmup threads (verified 2026-07-27). Naming it after numba rather than
    the caller keeps that obvious.
    """
    if not _HAVE_NUMBA:
        return
    import threading
    def _go():
        try:
            _samples = np.zeros(48, dtype=np.int16)
            _gate_loop(_samples, 327.0, 0.01, 0.001, 0.0)
        except Exception:
            log_exc()
    threading.Thread(
        target=_go, name='numba-jit-warmup', daemon=True,
    ).start()


_warmup_gate_loop()


# ── Neural denoise ──────────────────────────────────────────────────────────
#
# One engine: 'dpdfnet' (DPDFNet 8 kHz, ~10 MB ONNX, ~2.6 ms per 10 ms). It is
# the only model that keeps narrowband FM radio voice; RNNoise and the 48 kHz
# DeepFilterNet3 gated the speech out on our audio (2026-11-12) and were
# removed on 2026-11-13. See _DPDFNet8kStream.
#
# A DenoiseStream implements:
#     native_rate: int                              (48000)
#     dry_delay_samples: int                        (wet-path latency)
#     process(samples_i16: np.ndarray) -> np.ndarray
#     process_mix(samples_i16, mix) -> np.ndarray
#     close() -> None
#
# The JSON/API surface still uses the legacy key 'dfn' and stores
# 'dfn_engine'; the names are kept for config compatibility.


def _mix_with_dry_delay(stream, samples_i16, mix):
    """Shared wet/dry blender used by every DenoiseStream subclass.

    Keeps a persistent dry-delay buffer sized by stream.dry_delay_samples
    so the wet stream's algorithmic latency (DPDFNet ≈ 52 ms) is matched
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


# ── DPDFNet 8 kHz (narrowband) streaming via onnxruntime ────────────────────
#
# Ceva's DPDFNet (DeepFilterNet2 + dual-path RNN blocks), the 2-block 8 kHz
# model, Apache-2.0, bundled at tools/models/dpdfnet/ (see its README).
# Why an 8 kHz model on a 48 kHz bus: measured 2026-11-12 on our own radio
# audio, RNNoise and every 48 kHz model treated narrowband FM voice as noise
# and gated it out (RNNoise's own VAD sat at 0.02-0.1 through clear speech),
# while this telephone-band model kept speech level and still took the gaps
# between overs to ~-93 dB. Radio voice has nothing above ~3 kHz, so running
# the model at 8 kHz loses nothing. Fully wet sounded "really very good" by
# ear (Robin, 2026-11-12). A level count flags ~10% of >-40 dB windows as cut
# >20 dB, but that count can't tell inter-word hiss from speech; the drops
# heard in the pip package's output were its per-chunk resampling, which this
# wrapper doesn't have. Lower the mix only if a bus audibly clips words.
#
# Signature (one 10 ms hop per call, state carried across calls):
#   inputs:  spec[1,1,81,2] (rfft of a 160-sample Vorbis-windowed frame,
#            real/imag)   state_in[37860]
#   outputs: spec_e[1,1,81,2]   state_out[37860]
# The initial state is not zeros: the ERB/spectral normaliser seeds live in
# the model metadata. The pip `dpdfnet` package is NOT used — it needs
# librosa and resamples each chunk statelessly, which ticks at every chunk
# boundary. Here 48k<->8k is a pair of stateful FIRs.

_DPDFNET_MODEL = 'dpdfnet2_8khz.onnx'
_DPDFNET_MODEL_SHA256 = (
    '6218f1dbd6e4bac5768c63b7d899fe7b84b3788f2a35c4e246d4ab0946165c5d'
)
_DPDFNET_MODEL_SIZE = 10_188_357
_DPDFNET_RATIO = 6                    # 48 kHz bus / 8 kHz model


def _dpdfnet_model_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'tools', 'models', 'dpdfnet', _DPDFNET_MODEL)


class _DPDFNet8kStream:
    """Per-source DPDFNet 8 kHz denoise stream on a 48 kHz bus.

    int16 in, int16 out. Output is the same length as the input whenever the
    input is a multiple of 480 samples (every bus chunk is), because the
    analysis buffer is primed with one hop of silence.
    """

    native_rate = 48000
    # 2520 samples (52.5 ms), measured 2026-11-12 by cross-correlating real
    # radio speech in vs out (identical in three windows, r = 0.92-0.99; an
    # impulse is useless here, the model removes it as noise). It adds up:
    # FIR group delays 2 x 60 + primed hop 480 + 160-sample window 960 + the
    # model's 2-hop lookahead 960, all at 48 kHz. Dry path is delayed to match.
    dry_delay_samples = 2520

    _taps = None

    def __init__(self):
        import os
        import onnxruntime as ort
        from scipy.signal import firwin
        path = _dpdfnet_model_path()
        if not os.path.exists(path) or os.path.getsize(path) != _DPDFNET_MODEL_SIZE:
            raise RuntimeError(f'DPDFNet model missing or wrong size at {path}')
        opts = ort.SessionOptions()
        # One core per stream: concurrent buses must not contend
        # on a shared session's thread pool inside the 50 ms tick.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._sess = ort.InferenceSession(
            path, sess_options=opts, providers=['CPUExecutionProvider'])
        meta = self._sess.get_modelmeta().custom_metadata_map
        self._win = int(meta['window_length'])       # 160
        self._hop = int(meta['hop_length'])          # 80
        n_erb = int(meta['erb_norm_state_size'])
        n_spec = int(meta['spec_norm_state_size'])
        init = np.zeros(int(meta['state_size']), dtype=np.float32)
        init[:n_erb] = [float(v) for v in meta['erb_norm_init'].split(',')]
        init[n_erb:n_erb + n_spec] = [float(v) for v in meta['spec_norm_init'].split(',')]
        self._init_state = init
        self._in_names = [i.name for i in self._sess.get_inputs()]
        self._out_names = [o.name for o in self._sess.get_outputs()]
        # Vorbis window — COLA at 50% overlap (w[n]^2 + w[n+hop]^2 == 1).
        n = np.arange(self._win)
        s = np.sin(0.5 * np.pi * (n + 0.5) / (self._win / 2))
        self._window = np.sin(0.5 * np.pi * s * s).astype(np.float32)
        if _DPDFNet8kStream._taps is None:
            # resample_poly's own design for a factor of 6.
            _DPDFNet8kStream._taps = firwin(
                2 * 10 * _DPDFNET_RATIO + 1, 1.0 / _DPDFNET_RATIO,
                window=('kaiser', 5.0)).astype(np.float32)
        self._h = _DPDFNet8kStream._taps
        self._dry_delay_buf = np.empty(0, dtype=np.int16)
        self.reset()
        # Warm the session so the first real frames don't stall the worker.
        for _ in range(20):
            self._frame(np.zeros(self._win, dtype=np.float32))
        self.reset()

    def reset(self):
        self._state = self._init_state.copy()
        self._dec_zi = np.zeros(len(self._h) - 1, dtype=np.float32)
        self._int_zi = np.zeros(len(self._h) - 1, dtype=np.float32)
        self._phase = 0
        self._in8 = np.zeros(self._hop, dtype=np.float32)    # primed hop
        self._ola = np.zeros(self._win, dtype=np.float32)

    def close(self):
        self._sess = None

    def __del__(self):
        self.close()

    def _frame(self, frame8):
        spec = np.fft.rfft(frame8 * self._window)
        spec_in = np.stack([spec.real, spec.imag], axis=-1).astype(np.float32)[None, None]
        spec_e, self._state = self._sess.run(
            self._out_names, {self._in_names[0]: spec_in, self._in_names[1]: self._state})
        ri = spec_e[0, 0]
        return (np.fft.irfft(ri[:, 0] + 1j * ri[:, 1], n=self._win)
                * self._window).astype(np.float32)

    def process(self, samples_i16):
        if samples_i16.size == 0 or self._sess is None:
            return np.empty(0, dtype=np.int16)
        from scipy.signal import lfilter
        x = samples_i16.astype(np.float32) / 32768.0
        # 48k -> 8k: anti-alias FIR with carried state, keep every 6th sample
        # at a phase that carries across calls.
        y, self._dec_zi = lfilter(self._h, 1.0, x, zi=self._dec_zi)
        y8 = y[self._phase::_DPDFNET_RATIO].astype(np.float32)
        self._phase = (self._phase - len(x)) % _DPDFNET_RATIO
        buf = np.concatenate([self._in8, y8])
        out8 = []
        pos = 0
        win, hop = self._win, self._hop
        while buf.size - pos >= win:
            self._ola += self._frame(buf[pos:pos + win])
            out8.append(self._ola[:hop].copy())
            self._ola[:win - hop] = self._ola[hop:]
            self._ola[win - hop:] = 0.0
            pos += hop
        self._in8 = buf[pos:].copy()
        if not out8:
            return np.empty(0, dtype=np.int16)
        o8 = np.concatenate(out8)
        # 8k -> 48k: zero-stuff, same FIR (x6 gain), carried state.
        up = np.zeros(o8.size * _DPDFNET_RATIO, dtype=np.float32)
        up[::_DPDFNET_RATIO] = o8 * _DPDFNET_RATIO
        o48, self._int_zi = lfilter(self._h, 1.0, up, zi=self._int_zi)
        return np.clip(o48 * 32768.0, -32768, 32767).astype(np.int16)

    def process_mix(self, samples_i16, mix):
        return _mix_with_dry_delay(self, samples_i16, mix)


# Engine registry. make_denoise_stream() returns None for an unknown id.
_DENOISE_ENGINES = {
    'dpdfnet': _DPDFNet8kStream,
}

# Legal engine identifiers — consumers validate user input against these.
DENOISE_ENGINE_IDS = tuple(_DENOISE_ENGINES)
DEFAULT_DENOISE_ENGINE = 'dpdfnet'

# Engines removed 2026-11-13. Saved bus configs may still name them; they
# map to the default instead of silently switching denoise off.
_LEGACY_DENOISE_ENGINES = ('rnnoise', 'deepfilternet')


def normalize_denoise_engine(engine):
    """Map a stored engine id to a live one. Removed engines become the
    default; anything else unknown is returned unchanged (and rejected)."""
    engine = str(engine or DEFAULT_DENOISE_ENGINE)
    if engine in _LEGACY_DENOISE_ENGINES:
        return DEFAULT_DENOISE_ENGINE
    return engine


def make_denoise_stream(engine=DEFAULT_DENOISE_ENGINE):
    """Construct a denoise stream for the requested engine.

    Returns a DenoiseStream instance, or None if the engine is unknown or
    its model failed to load. Callers handle None by passing audio through
    dry — the audio path must never die because denoise is unavailable.
    """
    engine = normalize_denoise_engine(engine)
    cls = _DENOISE_ENGINES.get(engine)
    if cls is None:
        print(f"  [Denoise] unknown engine '{engine}' — skipping")
        return None
    try:
        return cls()
    except Exception as e:
        print(f"  [Denoise] {engine} unavailable: {e}")
        return None


# ── IIR filter state ────────────────────────────────────────────────────────

# Anything this small is far below one int16 step and will never be heard.
_FILTER_STATE_FLOOR = 1e-20


def settle_filter_state(zi):
    """Zero an IIR filter state that has decayed to nothing.

    Fed exact digital silence (a squelched SDR), a filter's state decays
    into subnormal floats and then sticks there (~5e-323) instead of
    reaching zero. Arithmetic on subnormals is done in microcode, so every
    later silent chunk costs many times a normal one: measured live
    2026-11-09, sosfilt on silence was 60% of the BusManager tick thread.
    Output is unchanged; the discarded state is hundreds of dB down.
    """
    if zi is not None and zi.size and float(np.abs(zi).max()) < _FILTER_STATE_FLOOR:
        zi.fill(0.0)
    return zi


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


def update_level(current, new_level, attack=1.0, decay=0.5):
    """Smooth a level value: instant attack, exponential decay.

    Default behaviour (attack=1.0, decay=0.5):
      - If *new_level* > *current*, jump to *new_level* immediately.
      - Otherwise blend: current * (1-decay) + new_level * decay.
    Returns int.

    This is the canonical meter-smoothing entry point. Do not roll your
    own `prev * X + new * Y` blends in callers — use this so the meter
    response stays consistent project-wide.
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
        self.enable_agc = False       # Automatic gain control, last in the
                                      # chain. See _apply_agc.
        self.agc_gain_db = 0.0        # current AGC gain (state)
        self.agc_limited = 0          # chunks where the clip guard cut gain
        self.enable_dfn = False       # Neural denoise toggle. "dfn" is kept
                                      # as the config/API key for back-compat;
                                      # actual engine is picked via dfn_engine.
        self.dfn_engine = DEFAULT_DENOISE_ENGINE  # set via set_dfn_engine()
        self.dfn_mix = 0.5            # Wet/dry mix — 1.0 = fully denoised,
                                      # 0.0 = pass-through.
                                      # Per-bus override via routing cmd
                                      # 'dfn_mix' (see web_server.py).
        self.dfn_bypass_db = -60.0    # Denoise bypass threshold (dBFS).
                                      # Chunks below this RMS skip the
                                      # denoise worker entirely and pass
                                      # dry — feeding squelch hiss / noise
                                      # floor to the engine is pure CPU
                                      # waste. Raise per-bus (routing cmd
                                      # 'set_dfn_bypass') for sources whose
                                      # idle floor sits above -60 dBFS.

        # Filter state (persists across audio chunks for continuity)
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        self.dfn_stream = None
        # Off-tick denoise worker + fixed-lag delay line (see _apply_dfn).
        # The tick hands chunk N to the worker and emits chunk N-DN_LAG:
        # the denoised copy if the worker finished it, else the DRY copy of
        # that same chunk. Output order never changes, the worker gets
        # DN_LAG ticks (100 ms) instead of one, and a late chunk is a brief
        # un-denoised chunk rather than a skip-and-repeat.
        import collections as _c
        self._dn_queue_in = _c.deque(maxlen=8)   # (seq, pcm, mix, engine)
        self._dn_wet = {}                        # seq -> denoised pcm (worker writes)
        self._dn_line = _c.deque()               # (seq, dry pcm, sent_to_worker)
        self._dn_seq = 0
        self._dn_last_call = 0.0
        self._dn_was_enabled = False
        self._dn_lock = None                     # threading.Lock — lazy init
        self._dn_evt = None                      # threading.Event — lazy init
        self._dn_thread = None
        self._dn_running = False
        # Instrumentation (also rg_denoise_late_total).
        self.dn_chunks = 0          # chunks emitted from the delay line
        self.dn_late = 0            # ...of which played dry: worker missed the slot
        self.dn_wet_discarded = 0   # denoised results that arrived after their slot

    def set_dfn_engine(self, engine):
        """Switch denoise engine. Signals the worker to rebuild the stream
        with the new backend on its next loop iteration. Removed engine ids
        (rnnoise, deepfilternet) map to the default."""
        engine = normalize_denoise_engine(engine)
        if engine == self.dfn_engine:
            return
        if engine not in DENOISE_ENGINE_IDS:
            print(f"  [AudioProcessor] rejecting unknown dfn_engine '{engine}'")
            return
        self.dfn_engine = engine
        # Worker owns the stream — signal it to rebuild. We can't just
        # close the stream here since we don't hold the worker's lock.
        self._dn_engine_dirty = True

    def reset_state(self):
        """Reset all filter states (e.g. when source restarts)."""
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        self.agc_gain_db = 0.0
        self._dn_stop_worker()

    def _dn_stop_worker(self):
        """Signal the denoise worker to exit and wait for it. Called on
        reset or DFN disable."""
        self._dn_running = False
        if self._dn_evt is not None:
            self._dn_evt.set()
        if self._dn_thread is not None:
            try:
                self._dn_thread.join(timeout=2)
            except Exception:
                pass
            self._dn_thread = None
        self._dn_queue_in.clear()
        self._dn_reset_line()

    # Ticks of delay the denoise worker is given (2 x 50 ms). One tick was
    # the original budget; a chunk over 50 ms then fell back to dry OUT OF
    # ORDER (current chunk dry, stale wet chunk next tick).
    DN_LAG = 2
    # A gap this long between calls (bus muted, D toggled off, bus idle
    # without flush) means the delay line holds stale audio — drop it.
    DN_STALE_S = 0.5

    def _dn_reset_line(self):
        """Discard the delay line and any pending denoised results."""
        self._dn_line.clear()
        if self._dn_lock is not None:
            with self._dn_lock:
                self._dn_wet.clear()
        else:
            self._dn_wet.clear()

    def process(self, pcm_data):
        """Run the full processing chain on PCM data. Order:
        HPF → DFN → LPF → Notch → Noise Gate → AGC

        HPF runs before DFN so subsonic rumble is removed cheaply; LPF/notch
        run after DFN so the denoiser sees the full band. AGC runs last so it
        levels the cleaned signal: boosting a quiet station BEFORE denoise
        would raise the hiss with it.
        """
        if not pcm_data:
            return pcm_data

        processed = pcm_data

        if self.enable_hpf:
            processed = self._apply_hpf(processed)

        if self.enable_dfn:
            if not self._dn_was_enabled:
                # D just switched on: start clean, and get the worker
                # building its engine now rather than on the first chunk.
                self._dn_reset_line()
                self._dn_ensure_worker()
            processed = self._apply_dfn(processed)
        self._dn_was_enabled = self.enable_dfn

        return self._post_dfn(processed)

    def _post_dfn(self, processed):
        """The stages after DFN: LPF → Notch → Noise Gate → AGC."""
        if self.enable_lpf:
            processed = self._apply_lpf(processed)

        if self.enable_notch:
            processed = self._apply_notch(processed)

        if self.enable_noise_gate:
            processed = self._apply_noise_gate(processed)

        if self.enable_agc:
            processed = self._apply_agc(processed)

        return processed

    def flush(self):
        """Next chunk still held in the denoise delay line, run through the
        post-DFN stages — or None. The bus manager calls this on ticks where
        the bus produced no audio, so the last DN_LAG chunks of a burst
        still play instead of being stranded (and later discarded)."""
        if not (self.enable_dfn and self._dn_line):
            return None
        self._dn_last_call = _time.monotonic()
        return self._post_dfn(self._dn_take())

    def get_active_list(self):
        """Return list of active filter names for status display."""
        active = []
        if self.enable_noise_gate: active.append('Gate')
        if self.enable_hpf: active.append('HPF')
        if self.enable_dfn: active.append('DFN')
        if self.enable_lpf: active.append('LPF')
        if self.enable_notch: active.append('Notch')
        if self.enable_agc: active.append('AGC')
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
            settle_filter_state(self.highpass_state)
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
            settle_filter_state(self.lowpass_state)
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
            settle_filter_state(self.notch_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_dfn(self, pcm_data):
        """Hand this tick's chunk to the off-tick denoise worker and return
        the chunk from DN_LAG ticks ago — denoised if the worker finished
        it, else that same chunk dry.

        The heavy neural inference happens on self._dn_thread so the bus
        tick never blocks on ONNX. Cost: DN_LAG ticks (~100 ms) of latency
        on a bus with D on. In exchange the worker has 100 ms per chunk, and
        a miss can never reorder audio: the old one-tick design returned the
        CURRENT chunk dry on a miss and the stale wet one next tick — an
        audible skip-and-repeat (seen as "drops" with two DFN buses on).

        Contract preserved: block-in = block-out, same pcm_data type. Until
        the line fills (start, after a reset) it emits silence, DN_LAG ticks.

        Silence short-circuit: chunks below dfn_bypass_db (default -60
        dBFS) are not sent to the worker — the model would pay a full
        frame cost on squelch zero-fill. They still go through the delay
        line (dry), so timing stays constant across squelch transitions.
        """
        now = _time.monotonic()
        if now - self._dn_last_call > self.DN_STALE_S:
            self._dn_reset_line()
        self._dn_last_call = now

        send = True
        try:
            _bypass_rms = 32767.0 * (10.0 ** (
                float(getattr(self, 'dfn_bypass_db', -60.0)) / 20.0))
            if pcm_rms(pcm_data) < _bypass_rms:
                send = False
        except Exception:
            log_exc()

        seq = self._dn_seq
        self._dn_seq += 1
        if send:
            self._dn_ensure_worker()
            # Snapshot params — mix and engine can change mid-flight
            # from the HTTP/MCP threads; capturing at enqueue time keeps the
            # tick's decision stable through the denoise pipeline.
            wet = max(0.0, min(1.0, float(getattr(self, 'dfn_mix', 0.5))))
            try:
                with self._dn_lock:
                    self._dn_queue_in.append((seq, pcm_data, wet, self.dfn_engine))
                self._dn_evt.set()
            except Exception as e:
                print(f"[DFN] {self.name}: enqueue error — {e}")
                send = False
        self._dn_line.append((seq, pcm_data, send))

        if len(self._dn_line) <= self.DN_LAG:
            return b'\x00' * len(pcm_data)
        return self._dn_take()

    def _dn_take(self):
        """Pop the oldest delay-line chunk: denoised if ready, else dry."""
        seq, dry, sent = self._dn_line.popleft()
        self.dn_chunks += 1
        if not sent:
            return dry
        out = None
        if self._dn_lock is not None:
            with self._dn_lock:
                out = self._dn_wet.pop(seq, None)
                # Anything older than this slot can never be played.
                stale = [k for k in self._dn_wet if k < seq]
                for k in stale:
                    del self._dn_wet[k]
            self.dn_wet_discarded += len(stale)
        if out is not None:
            return out
        self.dn_late += 1
        try:
            import metrics as _m
            _m.denoise_late_total.labels(
                bus=getattr(self, 'name', 'unknown'),
                engine=self.dfn_engine or 'unknown').inc()
        except Exception:
            log_exc()
        return dry

    def _dn_ensure_worker(self):
        """Lazy-start the per-bus denoise worker thread on first use."""
        if self._dn_thread is not None and self._dn_thread.is_alive():
            return
        import threading
        if self._dn_lock is None:
            self._dn_lock = threading.Lock()
            self._dn_evt = threading.Event()
        self._dn_engine_dirty = False
        self._dn_running = True
        self._dn_thread = threading.Thread(
            target=self._dn_worker_loop, daemon=True,
            name=f'Denoise-{self.name}')
        self._dn_thread.start()

    def _dn_worker_loop(self):
        """Per-bus denoise worker: pulls (seq, audio) from _dn_queue_in,
        runs the engine, stores the result in _dn_wet[seq].

        Owns its denoise stream (and that stream's private ONNX
        session) — only the worker touches the stream state, so there's no
        cross-thread synchronisation needed on the recurrent model history.
        The stream is built up front for the current engine, so session
        build + warmup happens here before the first real chunk arrives.
        """
        stream = None
        current_engine = None
        try:
            if self.enable_dfn:
                stream = make_denoise_stream(self.dfn_engine)
                current_engine = self.dfn_engine if stream is not None else None
        except Exception:
            log_exc()
        while self._dn_running:
            self._dn_evt.wait(timeout=0.5)
            self._dn_evt.clear()
            while self._dn_running:
                with self._dn_lock:
                    if not self._dn_queue_in:
                        break
                    item = self._dn_queue_in.popleft()
                seq, pcm_data, wet, engine = item
                # Rebuild stream if engine changed (or first call).
                if stream is None or engine != current_engine:
                    if stream is not None:
                        try: stream.close()
                        except Exception: pass
                    stream = make_denoise_stream(engine)
                    current_engine = engine
                    if stream is None:
                        # Engine unavailable — emit dry so the bus keeps
                        # flowing; the pass-through mirrors the old
                        # "downgrade and stop trying" behaviour.
                        with self._dn_lock:
                            self._dn_wet[seq] = pcm_data
                        continue
                _t0 = _time.monotonic()
                try:
                    samples = np.frombuffer(pcm_data, dtype=np.int16)
                    mixed = stream.process_mix(samples, wet)
                    if mixed.size == 0:
                        out_bytes = pcm_data  # sub-frame residue
                    else:
                        if mixed.size < samples.size:
                            mixed = np.concatenate([mixed, samples[mixed.size:]])
                        elif mixed.size > samples.size:
                            mixed = mixed[: samples.size]
                        out_bytes = mixed.astype(np.int16).tobytes()
                except Exception as e:
                    print(f"[DFN] {self.name}: worker process error — {e}")
                    out_bytes = pcm_data
                try:
                    import metrics as _m
                    _m.denoise_apply_ms.labels(
                        bus=getattr(self, 'name', 'unknown'),
                        engine=engine or 'unknown',
                    ).observe((_time.monotonic() - _t0) * 1000.0)
                except Exception:
                    log_exc()
                with self._dn_lock:
                    self._dn_wet[seq] = out_bytes
        # Shutdown — release the stream.
        if stream is not None:
            try: stream.close()
            except Exception: pass

    # AGC tuning. Repeaters arrive at full RF strength but with whatever
    # audio level each station put into them, so overs differ by 10-20 dB.
    AGC_TARGET_DBFS = -20.0   # chunk RMS the AGC steers speech towards
    AGC_MAX_GAIN_DB = 24.0    # most it will lift a quiet station
    AGC_MIN_GAIN_DB = -12.0   # most it will pull down a hot one
    AGC_FLOOR_DBFS = -50.0    # below this a chunk is a gap, not speech:
                              # the gain HOLDS, so silence between overs
                              # (or denoise residue) is never pumped up
    AGC_ATTACK_S = 0.1        # time constant for turning down
    AGC_RELEASE_S = 1.0       # time constant for turning up. Simulated on
                              # 70 min of sdr1 overs (2026-11-13, mean 6.2 s):
                              # 2 s left station spread at 16.8 dB, 1 s at 12.8,
                              # with 7 dB in-over gain swing (0.5 s: 9.6, pumps)
    AGC_CEILING = 0.89        # peak limit after gain (-1 dBFS)

    def _apply_agc(self, pcm_data):
        """Speech-gated AGC with a clip guard.

        Gain moves in dB toward (target - chunk RMS), fast down and slow up,
        and is ramped linearly across the chunk so it never steps (a step is
        a click). Chunks below AGC_FLOOR_DBFS leave the gain where it was.
        If a peak would pass AGC_CEILING the gain drops at once for this
        chunk, counted in agc_limited / rg_agc_limited_total.
        """
        x = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        n = x.size
        if n == 0:
            return pcm_data
        g0 = self.agc_gain_db
        rms = float(np.sqrt(np.mean(x * x)))
        rms_db = 20.0 * _math.log10(rms / 32767.0) if rms > 0 else -120.0
        g1 = g0
        if rms_db > self.AGC_FLOOR_DBFS:
            want = max(self.AGC_MIN_GAIN_DB,
                       min(self.AGC_MAX_GAIN_DB, self.AGC_TARGET_DBFS - rms_db))
            dt = n / float(getattr(self.config, 'AUDIO_RATE', 48000) or 48000)
            tc = self.AGC_ATTACK_S if want < g0 else self.AGC_RELEASE_S
            g1 = g0 + (want - g0) * (1.0 - _math.exp(-dt / tc))
        # Clip guard: the louder end of the ramp must keep the peak under
        # the ceiling. If not, both ends drop to the safe gain now.
        peak = float(np.max(np.abs(x)))
        if peak > 0:
            safe_db = 20.0 * _math.log10(self.AGC_CEILING * 32767.0 / peak)
            if max(g0, g1) > safe_db:
                g0 = g1 = min(g1, safe_db)
                self.agc_limited += 1
                try:
                    import metrics as _m
                    _m.agc_limited_total.labels(
                        bus=getattr(self, 'name', 'unknown')).inc()
                except Exception:
                    log_exc()
        self.agc_gain_db = g1
        try:
            import metrics as _m
            _m.agc_gain_db.labels(bus=getattr(self, 'name', 'unknown')).set(g1)
        except Exception:
            log_exc()
        if g0 == 0.0 and g1 == 0.0:
            return pcm_data
        ramp = np.power(10.0, np.linspace(g0, g1, n, dtype=np.float32) / 20.0)
        y = np.clip(x * ramp, -32768.0, 32767.0)
        return y.astype(np.int16).tobytes()

    def _apply_noise_gate(self, pcm_data):
        """Noise gate with attack/release envelope.

        v3.5-B: inner loop runs through numba-jit'd `_gate_loop` for ~50-
        100× speed over the pre-v3.5 per-sample Python loop. First call
        per process pays a one-shot JIT compile (~50-200 ms); cached on
        disk via `cache=True`. Behaviour is bit-identical to the prior
        loop — same threshold formula, same envelope coefficients, same
        soft-knee `(env/thr)^2` below threshold.
        """
        try:
            samples = np.frombuffer(pcm_data, dtype=np.int16)
            if samples.size == 0:
                return pcm_data

            threshold = 32767.0 * pow(10.0, self.gate_threshold / 20.0)
            attack_samples = self.gate_attack * self.config.AUDIO_RATE
            release_samples = self.gate_release * self.config.AUDIO_RATE
            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1

            gated, env = _gate_loop(
                samples, threshold, attack_coef, release_coef,
                float(self.gate_envelope),
            )
            self.gate_envelope = float(env)
            return gated.tobytes()
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
    # Generate the dah as one continuous 3-dit sine rather than tiling the dit
    # three times. Tiling restarts the sine's phase at each copy, and unless
    # freq*1.2/wpm happens to be a whole number of cycles that step is an
    # audible click one third and two thirds of the way through every dah.
    t_dah = np.arange(dit_n * 3) / sample_rate
    dah_tone = (np.sin(2 * np.pi * freq * t_dah) * 32767).astype(np.int16)
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
