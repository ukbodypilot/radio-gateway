#!/usr/bin/env python3
"""IIR filter state must not get stuck in subnormal floats on silence.

A squelched SDR delivers exact zeros. The filter state then decays into the
subnormal range and stays there (~5e-323), and every later chunk is filtered
in microcode: measured live 2026-11-09, sosfilt on silence was 60% of the
BusManager tick thread. `settle_filter_state` zeroes a state that has decayed
to nothing. These checks pin that it happens, and that the audio is unchanged.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402
from scipy.signal import sosfilt, sosfilt_zi  # noqa: E402

from audio_util import AudioProcessor, settle_filter_state  # noqa: E402
from plugins.sdr import _VoiceFilter  # noqa: E402

RATE = 48000
CHUNK = 2400
SPEC = (300.0, 3000.0)
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


rng = np.random.default_rng(1)
noise = (rng.standard_normal(CHUNK) * 3000).astype(np.int16)
silence = np.zeros(CHUNK, dtype=np.int16)
tiny = np.finfo(np.float64).tiny   # smallest normal float

print("Helper:")
z = np.array([[1e-300, 0.0], [5e-323, -2e-310]])
check("decayed state zeroed in place", settle_filter_state(z) is z and not z.any())
z = np.array([[1e-300, 0.5]])
check("live state left alone", settle_filter_state(z)[0, 1] == 0.5 and z[0, 0] == 1e-300)
check("None passes through", settle_filter_state(None) is None)

print("SDR voice filter, signal then silence:")
vf = _VoiceFilter(RATE)
ref_zi = None
ref_sos = None
outs, refs = [], []
seq = [noise] * 3 + [silence] * 40 + [noise] * 3
for chunk in seq:
    outs.append(vf.process(chunk, SPEC))
    if ref_sos is None:
        ref_sos = vf._sos.copy()
        ref_zi = sosfilt_zi(ref_sos) * 0.0
    y, ref_zi = sosfilt(ref_sos, chunk.astype(np.float64), zi=ref_zi)
    refs.append(np.clip(y, -32768, 32767).astype(np.int16))
check("output identical to an unbroken filter run",
      all(np.array_equal(a, b) for a, b in zip(outs, refs)))

vf2 = _VoiceFilter(RATE)
raw_zi = sosfilt_zi(ref_sos) * 0.0
for chunk in [noise] * 3 + [silence] * 40:
    vf2.process(chunk, SPEC)
    _, raw_zi = sosfilt(ref_sos, chunk.astype(np.float64), zi=raw_zi)
check("unguarded filter does get stuck subnormal (the bug is real here)",
      0 < np.abs(raw_zi).max() < tiny, str(np.abs(raw_zi).max()))
check("state is exactly zero after silence", not vf2._zi.any(), str(np.abs(vf2._zi).max()))
nz = np.abs(vf2._zi[vf2._zi != 0])
check("no subnormals left in state", nz.size == 0 or nz.min() >= tiny)
out = vf2.process(noise, SPEC)
check("still filters when audio returns", out.any())

print("AudioProcessor HPF / LPF:")
cfg = types.SimpleNamespace(AUDIO_RATE=RATE)
ap = AudioProcessor.__new__(AudioProcessor)
ap.config = cfg
ap.hpf_cutoff, ap.lpf_cutoff = 300, 3000
ap.highpass_state = ap.lowpass_state = None
for fn, attr in ((ap._apply_hpf, 'highpass_state'), (ap._apply_lpf, 'lowpass_state')):
    fn(noise.tobytes())
    live = getattr(ap, attr).copy()
    for _ in range(400):
        fn(silence.tobytes())
    st = getattr(ap, attr)
    check(f"{attr}: live after signal, exactly zero after silence",
          live.any() and not st.any(), f"max={np.abs(st).max()}")

print(f"\n{'ALL PASS' if _fails == 0 else f'{_fails} FAILED'}")
sys.exit(1 if _fails else 0)
