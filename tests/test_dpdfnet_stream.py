#!/usr/bin/env python3
"""DPDFNet 8 kHz denoise stream (audio_util._DPDFNet8kStream).

Added 2026-11-12 after RNNoise and the 48 kHz models were found to gate
narrowband FM radio voice out entirely. Checks the contract the bus relies
on: registered engine id, output the same length as every bus chunk, no
dependence on how the input is chunked (all resampling and STFT state is
carried), silence stays silent, and the dry-path delay used for the wet/dry
blend is the measured one. Needs onnxruntime and the bundled model; SKIPs
cleanly without them (CI does not install onnxruntime).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402
import scipy.signal  # noqa: E402,F401  -- load before audio_util's warm-up thread

import audio_util  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


check('dpdfnet is a registered engine id', 'dpdfnet' in audio_util.DENOISE_ENGINE_IDS)
check('dpdfnet maps to _DPDFNet8kStream',
      audio_util._DENOISE_ENGINES.get('dpdfnet') is audio_util._DPDFNet8kStream)

try:
    import onnxruntime  # noqa: F401
    _model_ok = os.path.exists(audio_util._dpdfnet_model_path())
except ImportError:
    _model_ok = False

if not _model_ok:
    print('  SKIP  DPDFNet stream checks (onnxruntime or bundled model missing)')
else:
    import hashlib
    with open(audio_util._dpdfnet_model_path(), 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    check('bundled model matches the recorded SHA-256',
          digest == audio_util._DPDFNET_MODEL_SHA256, digest)

    rng = np.random.default_rng(7)
    # Speech-band noise bursts with pauses — something the model passes in
    # part, so the chunking comparison is not trivially all-zeros.
    n = 48000 * 3
    sig = np.zeros(n, dtype=np.float32)
    b, a = scipy.signal.butter(4, [300, 3000], btype='band', fs=48000)
    burst = scipy.signal.lfilter(b, a, rng.standard_normal(n)).astype(np.float32)
    env = (np.sin(np.arange(n) / 48000 * 2 * np.pi * 1.5) > 0).astype(np.float32)
    sig = np.clip(burst * env * 0.3, -1, 1)
    x = (sig * 32767).astype(np.int16)

    st = audio_util.make_denoise_stream('dpdfnet')
    check('factory builds the stream', st is not None)

    lens = []
    out_a = []
    for i in range(0, n, 2400):
        y = st.process(x[i:i + 2400])
        lens.append(len(y))
        out_a.append(y)
    check('every 2400-sample bus chunk returns 2400 samples',
          set(lens) == {2400}, f'lengths seen: {sorted(set(lens))}')
    out_a = np.concatenate(out_a)

    st2 = audio_util.make_denoise_stream('dpdfnet')
    out_b = []
    i = 0
    for size in [480, 960, 1440, 2400, 4800] * 100:
        if i >= n:
            break
        out_b.append(st2.process(x[i:i + size]))
        i += size
    out_b = np.concatenate(out_b)[:len(out_a)]
    diff = int(np.abs(out_a[:len(out_b)].astype(np.int32) - out_b.astype(np.int32)).max())
    check('output does not depend on chunking', diff <= 1, f'max diff {diff}')

    st3 = audio_util.make_denoise_stream('dpdfnet')
    sil = np.concatenate([st3.process(np.zeros(2400, dtype=np.int16)) for _ in range(20)])
    check('silence in -> silence out', int(np.abs(sil).max()) <= 1, f'peak {int(np.abs(sil).max())}')

    check('dry-path delay is the measured 2520 samples',
          audio_util._DPDFNet8kStream.dry_delay_samples == 2520)
    mixed = st.process_mix(x[:2400], 0.5)
    check('process_mix returns int16 of chunk length',
          mixed.dtype == np.int16 and len(mixed) == 2400, f'{mixed.dtype} {len(mixed)}')

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
