#!/usr/bin/env python3
"""speaker_id.features.fbank matches Kaldi fbank (what the embedding models were trained on).

Reference values come from kaldi-native-fbank 1.x (dither=0, 80 bins, defaults
otherwise) on the fixed signal below. That library is not a gateway dependency,
so the numbers are pinned here instead of computed.

Why it matters: a feature mismatch does not fail loudly. Every embedding is
still a unit vector and every score still lands in [-1, 1]; voices just stop
separating. sherpa-onnx's own defaults (7.6 kHz top edge, snip_edges=False)
differ from training and cost nothing measurable on FM audio (bake-off
2026-11-15: 6.3% vs 6.3% EER), but a real slip, such as a missing mean
subtraction, took cosine-to-reference from 1.0 to 0.33.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_id.features import fbank   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


t = np.arange(16000) / 16000.0
rng = np.random.default_rng(7)
x = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 1234 * t)
     + 0.02 * rng.standard_normal(16000)).astype(np.float32)

F = fbank(x)
check('1 s at 16 kHz -> 98 frames x 80 bins (25 ms / 10 ms, snip_edges)', F.shape == (98, 80), str(F.shape))

REF = {(0, 0): -10.5912, (0, 10): -4.6325, (10, 20): -5.472, (50, 40): -3.6976,
       (97, 79): 0.5042, (30, 5): -8.4797}
for (f, b), v in REF.items():
    got = float(F[f, b])
    check(f'frame {f} bin {b} = {v}', abs(got - v) < 2e-3, f'got {got:.4f}')
check('overall mean matches', abs(float(F.mean()) - (-2.531)) < 1e-3, f'{F.mean():.4f}')

check('shorter than one frame -> empty, not an error', fbank(np.zeros(100, np.float32)).shape == (0, 80))
check('digital silence stays finite (log floor)', np.isfinite(fbank(np.zeros(1600, np.float32))).all())

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
