#!/usr/bin/env python3
"""Bus AGC (AudioProcessor._apply_agc), added 2026-11-13 in place of the bus
noise gate.

The SDRs hear nearby repeaters at full RF strength, but each station's audio
level into the repeater varies, so overs differ by 10-20 dB. The AGC runs
last, after denoise, and must: lift a quiet station toward the target, pull a
hot one down fast, HOLD its gain through the gaps between overs (never pump
silence up), never clip, and never step the gain mid-chunk.
"""

import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402

from audio_util import AudioProcessor  # noqa: E402

RATE = 48000
CHUNK = 2400   # one 50 ms bus tick
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def make():
    p = AudioProcessor('bus_agc_test', types.SimpleNamespace(AUDIO_RATE=RATE))
    p.enable_agc = True
    return p


_rng = np.random.default_rng(7)


def speech(dbfs, n=CHUNK):
    """Noise burst at an RMS of `dbfs` (crest ~4x, like speech)."""
    x = _rng.standard_normal(n)
    x *= (32767.0 * 10 ** (dbfs / 20.0)) / np.sqrt(np.mean(x * x))
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def rms_db(b):
    x = np.frombuffer(b, dtype=np.int16).astype(np.float64)
    r = np.sqrt(np.mean(x * x))
    return 20 * math.log10(r / 32767.0) if r > 0 else -120.0


def run(p, dbfs, secs):
    out = None
    for _ in range(int(secs * RATE / CHUNK)):
        out = p.process(speech(dbfs))
    return out


# --- off by default, and a no-op when off ---
q = AudioProcessor('bus_off', types.SimpleNamespace(AUDIO_RATE=RATE))
b = speech(-40)
check('AGC is off by default', q.enable_agc is False)
check('off: audio passes untouched', q.process(b) == b)

# --- quiet station is lifted toward the target ---
p = make()
out = run(p, -40, 8)
check('quiet -40 dBFS station lifted to within 3 dB of -20',
      abs(rms_db(out) + 20) < 3, f'{rms_db(out):.1f} dBFS, gain {p.agc_gain_db:.1f}')
check('output length == input length', len(out) == CHUNK * 2)

# --- gain HOLDS through the gap between overs ---
g = p.agc_gain_db
silence = bytes(CHUNK * 2)
for _ in range(60):                     # 3 s of exact silence
    s_out = p.process(silence)
check('silence leaves the gain where it was', abs(p.agc_gain_db - g) < 1e-9,
      f'{g:.2f} -> {p.agc_gain_db:.2f}')
check('silence stays silent', s_out == silence)
residue = speech(-70)                   # denoise residue between overs
r_out = p.process(residue)
check('sub-floor residue holds the gain too', abs(p.agc_gain_db - g) < 1e-9)

# --- a hot station is pulled down FAST and never clips ---
p = make()
run(p, -40, 8)                          # gain wound up high (~+20 dB)
peaks = []
limited0 = p.agc_limited
for _ in range(10):                     # 0.5 s of a loud over
    o = p.process(speech(-8))
    peaks.append(int(np.max(np.abs(np.frombuffer(o, dtype=np.int16)))))
check('loud over after a quiet one never clips',
      max(peaks) <= int(p.AGC_CEILING * 32767) + 1, f'max peak {max(peaks)}')
check('clip guard engaged and was counted', p.agc_limited > limited0)
check('gain came down within 0.5 s', p.agc_gain_db < -5, f'{p.agc_gain_db:.1f} dB')

# --- limits ---
p = make()
run(p, -80 + 31, 20)                    # -49 dBFS: just above the floor
check('gain capped at AGC_MAX_GAIN_DB', p.agc_gain_db <= p.AGC_MAX_GAIN_DB + 1e-6,
      f'{p.agc_gain_db:.1f}')

# --- no step inside a chunk: the ramp is smooth ---
p = make()
p.agc_gain_db = 0.0
x = np.full(CHUNK, 1000, dtype=np.int16).tobytes()   # constant, -30 dBFS
y = np.frombuffer(p.process(x), dtype=np.int16).astype(np.int32)
check('gain ramps monotonically across the chunk (no click)',
      bool(np.all(np.diff(y) >= 0)), 'non-monotonic')

# --- reset_state clears the gain ---
p.reset_state()
check('reset_state returns the gain to 0 dB', p.agc_gain_db == 0.0)

# --- shows in the active list ---
check("get_active_list reports 'AGC'", 'AGC' in make().get_active_list())

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
