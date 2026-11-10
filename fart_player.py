"""Real-sample fart machine.

Plays real recorded fart samples (royalty-free Mixkit SFX, vendored under
audio/farts/) instead of synthesizing — guaranteed to sound like a fart because
it is one. Optional per-press tape-speed pitch/length variation keeps a small
sample set from sounding repetitive.

Public interface mirrors the playback path's needs:
    random_fart(variation) -> (mono int16 PCM bytes @ RATE, name)
    to_wav_bytes(pcm_bytes) -> WAV bytes   (browser preview)
"""

import io
import os
import glob
import random
import urllib.request

import numpy as np
import soundfile as sf

RATE = 48000
_BASE = os.path.dirname(os.path.abspath(__file__))
_FARTS_DIR = os.path.join(_BASE, 'audio', 'farts')

# Free Mixkit fart SFX ids (royalty-free, no attribution). Used to populate
# audio/farts/ on a fresh machine if it's empty (best-effort, bounded timeout).
_MIXKIT_FART_IDS = [3041, 3043, 3050, 3051, 3052, 3053, 3054, 3055, 3056,
                    2889, 2890, 2891]
_AUDIO_EXTS = ('*.mp3', '*.wav', '*.ogg', '*.flac', '*.m4a')


from log_util import log_exc

def list_farts():
    out = []
    for ext in _AUDIO_EXTS:
        out.extend(glob.glob(os.path.join(_FARTS_DIR, ext)))
    return sorted(out)


def ensure_samples():
    """Download the Mixkit fart pack into audio/farts/ if it's empty."""
    if list_farts():
        return
    os.makedirs(_FARTS_DIR, exist_ok=True)
    for sid in _MIXKIT_FART_IDS:
        dst = os.path.join(_FARTS_DIR, 'fart_%d.mp3' % sid)
        if os.path.exists(dst):
            continue
        url = 'https://assets.mixkit.co/active_storage/sfx/%d/%d-preview.mp3' % (sid, sid)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
            tmp = dst + '.partial'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, dst)
        except Exception:
            log_exc()
            continue


def _decode(path):
    audio, sr = sf.read(path, dtype='float32')
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != RATE:
        import resampy
        audio = resampy.resample(audio, sr, RATE)
    return audio


def random_fart(variation=0.12, rng=None):
    """Pick a random real fart, optionally tape-speed varied. Returns (pcm, name).

    `variation` is the max ± fractional speed/pitch shift (0.12 = ±12%). A speed
    change shifts pitch and length together, like a tape-speed wobble — natural
    variety for a fart, no spectral artefacts.
    """
    files = list_farts()
    if not files:
        ensure_samples()
        files = list_farts()
    if not files:
        raise RuntimeError("no fart samples in audio/farts/ (download failed)")

    rng = rng or random.Random()
    path = rng.choice(files)
    a = _decode(path)

    variation = max(0.0, min(0.6, float(variation)))
    if variation > 0 and len(a) > 4:
        factor = 1.0 + rng.uniform(-variation, variation)   # >1 = faster/higher
        idx = np.arange(0, len(a), factor)
        a = np.interp(idx, np.arange(len(a)), a)

    # light peak-normalise to ~-1 dBFS (parity with how the soundboard loads
    # files); the playback path applies the configured TX gain on top.
    a = a / (np.max(np.abs(a)) + 1e-9) * 0.9
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)
    return pcm.tobytes(), os.path.basename(path)


def to_wav_bytes(pcm_bytes, rate=RATE):
    a = np.frombuffer(pcm_bytes, dtype=np.int16)
    buf = io.BytesIO()
    sf.write(buf, a, rate, format='WAV', subtype='PCM_16')
    return buf.getvalue()


if __name__ == '__main__':
    pcm, name = random_fart()
    open('/tmp/fart_test.wav', 'wb').write(to_wav_bytes(pcm))
    print("wrote /tmp/fart_test.wav from", name,
          "(%d farts available)" % len(list_farts()))
