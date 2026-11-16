"""Kaldi-compatible log-mel filterbank features in numpy.

Matches what 3D-Speaker / WeSpeaker speaker-embedding ONNX models were trained
on (kaldi fbank: 25 ms povey window, 10 ms shift, pre-emphasis 0.97, DC
removal, power spectrum, 80 mel bins from 20 Hz to Nyquist, no dither,
snip_edges). Written here instead of pulling in sherpa-onnx, which bundles a
second copy of onnxruntime into the gateway process.
tests/test_speaker_features.py pins the output against reference values.
"""
import numpy as np

SR = 16000
FRAME = 400          # 25 ms
SHIFT = 160          # 10 ms
NFFT = 512
PREEMPH = 0.97
LOW_HZ = 20.0
EPS = np.finfo(np.float32).eps


def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


def _mel_banks(n_bins, sr=SR, nfft=NFFT, low=LOW_HZ, high=None):
    """Kaldi's triangular mel filters (computed in mel space, not Slaney)."""
    high = sr / 2 if not high else high
    n_fft_bins = nfft // 2
    fft_bin_width = sr / nfft
    mel_lo, mel_hi = _mel(low), _mel(high)
    delta = (mel_hi - mel_lo) / (n_bins + 1)
    mels = _mel(fft_bin_width * np.arange(n_fft_bins))
    banks = np.zeros((n_bins, n_fft_bins + 1), np.float32)
    for b in range(n_bins):
        left, center, right = mel_lo + b * delta, mel_lo + (b + 1) * delta, mel_lo + (b + 2) * delta
        up = (mels - left) / (center - left)
        down = (right - mels) / (right - center)
        banks[b, :n_fft_bins] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = {}
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME) / (FRAME - 1))) ** 0.85  # povey


def fbank(samples, n_bins=80):
    """samples: float32 mono 16 kHz in [-1, 1]. Returns (frames, n_bins) float32."""
    x = np.asarray(samples, np.float32)
    if len(x) < FRAME:
        return np.zeros((0, n_bins), np.float32)
    n = 1 + (len(x) - FRAME) // SHIFT
    idx = np.arange(FRAME)[None, :] + SHIFT * np.arange(n)[:, None]
    fr = x[idx].astype(np.float64)
    fr -= fr.mean(axis=1, keepdims=True)                     # remove DC
    fr[:, 1:] -= PREEMPH * fr[:, :-1]                         # pre-emphasis (kaldi order:
    fr[:, 0] -= PREEMPH * fr[:, 0]                            #  first sample uses itself)
    fr *= _WINDOW
    spec = np.abs(np.fft.rfft(fr, n=NFFT)) ** 2
    banks = _BANKS.get(n_bins)
    if banks is None:
        banks = _BANKS[n_bins] = _mel_banks(n_bins)
    mel = spec @ banks.T.astype(np.float64)
    return np.log(np.maximum(mel, EPS)).astype(np.float32)
