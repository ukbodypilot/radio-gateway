#!/usr/bin/env python3
"""Shared audio utilities — level metering, AudioProcessor, CW generation.

Extracted from audio_sources.py to eliminate duplicate level-metering code
across plugins and sources.
"""

import math as _math
import numpy as np


# ── Neural denoise (RNNoise) ────────────────────────────────────────────────
#
# We bind to pyrnnoise's ctypes wrapper directly and skip its __init__.py,
# which imports audiolab/matplotlib/tqdm — none of which we want in the
# gateway's dependency tree. The C library ships inside the wheel.

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
    stream buffers to the native 480-sample (10 ms @ 48 kHz) frame size."""

    def __init__(self):
        mod = _load_rnnoise()
        if mod is None:
            raise RuntimeError("rnnoise library not available")
        self._mod = mod
        self._state = mod.create()
        self._buf = np.empty(0, dtype=np.int16)
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
        self.enable_dfn = False       # Neural denoise (RNNoise). Kept as
                                      # "dfn" in the API surface so a future
                                      # DeepFilterNet swap is a model swap
                                      # rather than a rename.
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
        """Neural denoise (RNNoise).

        Assumes 48 kHz mono int16 input — the gateway-wide AUDIO_RATE. If
        the library can't be loaded, silently pass-through so a missing dep
        doesn't kill the audio path.

        Output is a wet/dry mix — RNNoise is aggressive on radio audio (its
        training data was consumer noise, not FM carrier hiss / squelch
        tails), so a full-wet output kills too much signal. `dfn_mix` is the
        wet fraction: 1.0 = fully denoised, 0.0 = pass-through. The default
        of 0.5 leaves voice clearly audible while still knocking the noise
        floor down by ~6 dB.
        """
        try:
            if self.dfn_stream is None:
                try:
                    self.dfn_stream = _RNNoiseStream()
                except Exception as e:
                    # Downgrade to pass-through and stop trying.
                    print(f"[DFN] {self.name}: disabling — {e}")
                    self.enable_dfn = False
                    return pcm_data

            samples = np.frombuffer(pcm_data, dtype=np.int16)
            if samples.size == 0:
                return pcm_data

            denoised = self.dfn_stream.process(samples)
            if denoised.size == 0:
                # All samples buffered as <1 frame residue; emit the dry
                # signal so we don't drop audio at startup.
                return pcm_data

            # Align lengths — block-in = block-out contract for downstream sinks.
            if denoised.size < samples.size:
                denoised = np.concatenate([
                    denoised,
                    samples[denoised.size:],  # dry fill, not silence
                ])
            elif denoised.size > samples.size:
                denoised = denoised[: samples.size]

            wet = float(getattr(self, 'dfn_mix', 0.5))
            wet = max(0.0, min(1.0, wet))
            if wet >= 0.999:
                out = denoised
            elif wet <= 0.001:
                out = samples
            else:
                mixed = (samples.astype(np.int32) * (1.0 - wet)
                         + denoised.astype(np.int32) * wet)
                out = np.clip(mixed, -32768, 32767).astype(np.int16)

            return out.astype(np.int16).tobytes()
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
