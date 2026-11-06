#!/usr/bin/env python3
"""SDR voice filter: the page's LPF/HPF fields must actually filter.

rtl_airband passes `lowpass`/`highpass` only to its LAME MP3 encoder; the
PulseAudio output the gateway reads is unfiltered, so the fields did
nothing and hiss above 2.5 kHz came straight through (2026-11-05). The
plugin now runs a Butterworth HPF/LPF on each tuner's audio.

Checks the frequency response when fed in gateway-sized chunks (filter
state must carry across chunk boundaries), that 0 disables a side, that a
changed setting rebuilds the filter, and which channel feeds which tuner.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402

from plugins.sdr import SDRPlugin, _VoiceFilter  # noqa: E402

RATE = 48000
CHUNK = 2400
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def gain_db(vf, spec, freq):
    """Steady-state gain of `vf` at `freq`, fed chunk by chunk."""
    t = np.arange(RATE) / RATE                     # 1 s
    x = (10000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    out = np.concatenate([vf.process(x[i:i + CHUNK], spec)
                          for i in range(0, len(x), CHUNK)]).astype(np.float64)
    tail = slice(RATE // 2, None)                  # skip the settling transient
    rin = np.sqrt(np.mean(x[tail].astype(np.float64) ** 2))
    rout = np.sqrt(np.mean(out[tail] ** 2))
    return 20 * np.log10(max(rout, 1e-9) / rin)


print("Voice filter response (100-2500 Hz):")
spec = (100.0, 2500.0)
g = {f: gain_db(_VoiceFilter(RATE), spec, f) for f in (50, 1000, 2500, 5000, 7000)}
check("1 kHz passband within 0.5 dB", abs(g[1000]) < 0.5, f"{g[1000]:.2f} dB")
check("2.5 kHz at the -3 dB corner", -4 < g[2500] < -2, f"{g[2500]:.2f} dB")
check("5 kHz down >= 20 dB (one octave, 24 dB/oct)", g[5000] < -20, f"{g[5000]:.2f} dB")
check("7 kHz down >= 30 dB", g[7000] < -30, f"{g[7000]:.2f} dB")
check("50 Hz down >= 10 dB (HPF)", g[50] < -10, f"{g[50]:.2f} dB")

print("Disable and rebuild:")
vf = _VoiceFilter(RATE)
check("LPF 0 = off: 7 kHz passes", abs(gain_db(vf, (0.0, 0.0), 7000)) < 0.1)
check("describe() reports off", vf.describe() == 'off', vf.describe())
r0 = vf.rebuilds
gain_db(vf, (100.0, 2000.0), 1000)
check("changed spec rebuilds once", vf.rebuilds == r0 + 1, f"{vf.rebuilds - r0}")
check("describe() reports band", vf.describe() == '100-2000 Hz', vf.describe())
check("no errors", vf.errors == 0, str(vf.errors))

print("Spec per tuner:")
p = SDRPlugin.__new__(SDRPlugin)
p._sdr_mode = 'single'
p._single_channels = [{'freq': 446.76, 'lowpass': 2200, 'highpass': 300},
                      {'freq': 446.64}]
check("single ch1 -> tuner 1", p._voice_filter_spec(1) == (300.0, 2200.0), str(p._voice_filter_spec(1)))
check("single ch2 defaults 100-2500", p._voice_filter_spec(2) == (100.0, 2500.0), str(p._voice_filter_spec(2)))
p._single_channels = p._single_channels[:1]
check("single missing ch2 -> None (unfiltered)", p._voice_filter_spec(2) is None)
p._sdr_mode = 'dual'
p.highpass, p.lowpass, p.highpass2, p.lowpass2 = 100, 2400, 200, 3000
check("dual tuner 1", p._voice_filter_spec(1) == (100.0, 2400.0))
check("dual tuner 2", p._voice_filter_spec(2) == (200.0, 3000.0))

print("Tuner capture applies it:")
from plugins.sdr import _TunerCapture  # noqa: E402
cfg = types.SimpleNamespace(AUDIO_RATE=RATE, AUDIO_CHUNK_SIZE=CHUNK)
tc = _TunerCapture('SDR1', cfg, 'sdr_capture', None, filter_spec=lambda: (100.0, 2500.0))
tc._reader_running = True
t = np.arange(CHUNK * 10) / RATE
tone = (8000 * np.sin(2 * np.pi * 6000 * t)).astype(np.int16)
stereo = np.repeat(tone, 2)
outs = []
for i in range(10):
    tc._chunk_queue.put_nowait(stereo[i * CHUNK * 2:(i + 1) * CHUNK * 2].tobytes())
    outs.append(np.frombuffer(tc.get_chunk(), dtype=np.int16))
last = outs[-1].astype(np.float64)
att = 20 * np.log10(np.sqrt(np.mean(last ** 2)) / (8000 / np.sqrt(2)))
check("6 kHz tone through get_chunk down >= 25 dB", att < -25, f"{att:.1f} dB")
check("status describe after use", tc.voice_filter.describe() == '100-2500 Hz', tc.voice_filter.describe())

print("Live set_voice_filter command:")
saves = []
p._save_settings = lambda: saves.append(1)
p._sdr_mode = 'single'
p._single_channels = [{'freq': 446.76}, {'freq': 446.64}]
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 2, 'highpass': 300, 'lowpass': 2200})
check("single ch2 updated live", r.get('ok') and p._voice_filter_spec(2) == (300.0, 2200.0), str(r))
check("persisted", len(saves) == 1)
check("no rtl_airband restart needed (returns, doesn't restart)", 'restart' not in str(r).lower())
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 1, 'highpass': 3000, 'lowpass': 2000})
check("rejects lowpass <= highpass", not r.get('ok'))
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 1, 'highpass': 100, 'lowpass': 0})
check("lowpass 0 (off) accepted", r.get('ok') and p._voice_filter_spec(1) == (100.0, 0.0), str(r))
p._single_channels = p._single_channels[:1]
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 2, 'highpass': 100, 'lowpass': 2500})
check("unsaved channel -> error", not r.get('ok') and 'no channel' in r.get('error', ''), str(r))
p._sdr_mode = 'dual'
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 2, 'highpass': 150, 'lowpass': 2700})
check("dual tuner 2 updated", r.get('ok') and p._voice_filter_spec(2) == (150.0, 2700.0), str(r))
r = p.execute({'cmd': 'set_voice_filter', 'tuner': 1})
check("missing fields -> error", not r.get('ok'))

print(f"\n{'ALL PASS' if _fails == 0 else f'{_fails} FAILED'}")
sys.exit(1 if _fails else 0)
