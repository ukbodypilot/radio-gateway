#!/usr/bin/env python3
"""Bus compressor (AudioProcessor._apply_comp), added 2026-11-13 after the AGC.

Settings were picked by ear from a level-matched A/B of 15 min of sdr1. The
compressor must: squeeze loud syllables, leave speech under the threshold
alone (apart from makeup), never lift the gaps between overs, never clip,
never step its gain (inside a chunk or across chunks), and with the AGC on
leave the clip guard much less to do.
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
CHUNK = 2400
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def make(agc=False, comp=True):
    p = AudioProcessor('bus_comp_test', types.SimpleNamespace(AUDIO_RATE=RATE))
    p.enable_agc = agc
    p.enable_comp = comp
    return p


_rng = np.random.default_rng(11)


def tone(dbfs, n=CHUNK, f=440.0, start=0):
    t = (np.arange(n) + start) / RATE
    x = np.sin(2 * np.pi * f * t) * 32767.0 * 10 ** (dbfs / 20.0) * math.sqrt(2)
    return np.clip(x, -32768, 32767).astype(np.int16)


def speech(dbfs, n=CHUNK):
    x = _rng.standard_normal(n)
    x *= (32767.0 * 10 ** (dbfs / 20.0)) / np.sqrt(np.mean(x * x))
    return np.clip(x, -32768, 32767).astype(np.int16)


def rms_db(a):
    a = np.asarray(a, dtype=np.float64)
    r = np.sqrt(np.mean(a * a))
    return 20 * math.log10(r / 32767.0) if r > 0 else -120.0


def run(p, chunks):
    return [np.frombuffer(p.process(c.tobytes()), dtype=np.int16) for c in chunks]


# --- off by default, and a no-op when off ---
q = make(comp=False)
check('compressor is off by default',
      AudioProcessor('x', types.SimpleNamespace(AUDIO_RATE=RATE)).enable_comp is False)
b = speech(-30).tobytes()
check('off: audio passes untouched', q.process(b) == b)

# --- quiet speech, under the threshold: only the makeup gain ---
p = make()
out = run(p, [tone(-30, start=i * CHUNK) for i in range(40)])
gain = rms_db(out[-1]) - (-30)
check('under threshold: just the +2 dB makeup', abs(gain - p.COMP_MAKEUP_DB) < 0.3, f'{gain:+.2f} dB')

# --- loud speech: squeezed toward 4:1 above -18 dBFS peak ---
p = make()
out = run(p, [tone(-6, start=i * CHUNK) for i in range(40)])
pk_in = 20 * math.log10(np.max(np.abs(tone(-6))) / 32767.0)          # -3 dBFS peak
pk_out = 20 * math.log10(np.max(np.abs(out[-1])) / 32767.0)
want = p.COMP_THRESHOLD_DBFS + (pk_in - p.COMP_THRESHOLD_DBFS) / p.COMP_RATIO + p.COMP_MAKEUP_DB
check('above threshold: peak follows 4:1 (+makeup) within 1.5 dB',
      abs(pk_out - want) < 1.5, f'out {pk_out:.1f} want {want:.1f}')
check('reduction reported', p.comp_reduction_db > 8, f'{p.comp_reduction_db:.1f} dB')

# --- gaps are never lifted ---
p = make()
run(p, [speech(-20) for _ in range(20)])                    # makeup engaged
gap_in = speech(-62)                                           # NR residue in a gap
outs = run(p, [gap_in for _ in range(10)])
check('gap residue is not lifted by makeup', rms_db(outs[-1]) <= rms_db(gap_in) + 0.2,
      f'{rms_db(gap_in):.1f} -> {rms_db(outs[-1]):.1f}')
z = np.zeros(CHUNK, dtype=np.int16)
check('exact silence stays silent', np.all(run(p, [z])[0] == 0))

# --- never clips, even on a full-scale burst ---
p = make(agc=True)
run(p, [speech(-40) for _ in range(160)])                   # AGC wound up
outs = run(p, [speech(-3) for _ in range(10)])
check('full-scale burst after a quiet station never passes the ceiling',
      max(int(np.max(np.abs(o.astype(np.int32)))) for o in outs) <= int(p.AGC_CEILING * 32767) + 1)

# --- no steps: a level jump inside the stream gives a smooth gain curve ---
p = make()
c1, c2 = tone(-30, start=0), tone(-3, start=CHUNK)
y = np.concatenate(run(p, [c1, c2, tone(-3, start=2 * CHUNK)])).astype(np.float64)
x = np.concatenate([c1, c2, tone(-3, start=2 * CHUNK)]).astype(np.float64)
nz = np.abs(x) > 2000
g = np.abs(y[nz] / x[nz])
check('gain never jumps more than 1.5 dB sample-to-sample (no click)',
      float(np.max(np.abs(np.diff(20 * np.log10(g))))) < 1.5)

# --- with the AGC: far fewer clip-guard trims than the AGC alone ---
seq = []
for i in range(120):                                           # overs at mixed levels
    lvl = (-38, -22, -12)[(i // 20) % 3]
    seq.append(speech(lvl) if i % 20 < 16 else np.zeros(CHUNK, dtype=np.int16))
a = make(agc=True, comp=False)
run(a, seq)
c = make(agc=True, comp=True)
run(c, seq)
check('AGC + compressor trims fewer chunks than the AGC alone',
      c.agc_limited < a.agc_limited, f'{c.agc_limited} vs {a.agc_limited}')

# --- reset and status ---
c.reset_state()
check('reset_state clears the compressor', c._comp_env == 0.0 and c._comp_g_db == 0.0)
check("get_active_list reports 'Comp'", 'Comp' in make().get_active_list())

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
