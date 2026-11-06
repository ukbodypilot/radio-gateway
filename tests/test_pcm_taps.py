#!/usr/bin/env python3
"""PCM monitor taps: AS1/AS2/SDR1/SDR2 shell-bar buttons.

An SDR tap must carry exactly what the bus is served (after boost/filter/
processor) WITHOUT a second get_chunk() call stealing from the bus queue.
Taps are exclusive across kinds: enabling an SDR tap clears an AllStar tap and
vice versa — before pcm_taps.py the AllStar endpoint only walked
_external_plugins and could never have cleared an SDR tap. A tap on a tuner
whose capture isn't running is refused rather than lighting a silent button.
"""

import collections
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402

import pcm_taps  # noqa: E402
from plugins.sdr import SDRPlugin, _TunerCapture  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class FakeNode:
    """Stands in for a usrp plugin instance."""
    def __init__(self):
        self.pcm_tap = False
        self._tap_queue = collections.deque([b'x'], maxlen=4)

    def take_tap(self):
        return self._tap_queue.popleft() if self._tap_queue else None


def make_tuner(name):
    cfg = types.SimpleNamespace(AUDIO_RATE=48000, AUDIO_CHUNK_SIZE=2400, SDR_DUCK=True)
    passthrough = types.SimpleNamespace(process=lambda raw: raw)
    t = _TunerCapture(name, cfg, 'sink', passthrough)
    t._reader_running = True               # pretend parec is up
    t._parec_proc = object()
    return t


def stereo_chunk(val):
    return (np.full(2400 * 2, val, dtype=np.int16)).tobytes()


# --- SDR tuner tap fill ---
t1 = make_tuner('SDR1')
t1._chunk_queue.put_nowait(stereo_chunk(1000))
out = t1.get_chunk()
check('tap off: nothing queued', len(t1._tap_queue) == 0)

t1.pcm_tap = True
t1._chunk_queue.put_nowait(stereo_chunk(2000))
out = t1.get_chunk()
tapped = t1.take_tap()
check('tap on: bus chunk is mono 2400 samples', out is not None and len(out) == 4800)
check('tap carries the exact chunk the bus got', tapped == out)
check('tap did not consume the bus queue', t1._chunk_queue.qsize() == 0 and t1.take_tap() is None)

out = t1.get_chunk()                       # underrun -> zero-fill
check('zero-fill is tapped too (stream keeps cadence)', t1.take_tap() == out and out == b'\x00' * 4800)

for _ in range(10):
    t1.get_chunk()
check('tap queue bounded at 4', len(t1._tap_queue) == 4)
check('tap_chunks counter counts', t1.tap_chunks >= 12)

# --- exclusivity across kinds ---
node1, node2 = FakeNode(), FakeNode()
sdr = SDRPlugin()
t2 = make_tuner('SDR2')
t1.pcm_tap = False
t1._tap_queue.clear()
sdr._tuner1, sdr._tuner2 = t1, t2
gw = types.SimpleNamespace(_external_plugins={'usrp': node1, 'usrp2': node2}, sdr_plugin=sdr)
sdr._gateway = gw
check('tap_sources lists nodes + both tuners', len(pcm_taps.tap_sources(gw)) == 4)

pcm_taps.set_tap(gw, node1, True)
r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 1, 'on': '1'})
check('SDR1 tap ok', r.get('ok') and r.get('pcm_tap') is True, r)
check('enabling SDR1 cleared AS1', node1.pcm_tap is False and not node1._tap_queue)

r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 2, 'on': '1'})
check('enabling SDR2 cleared SDR1', t2.pcm_tap and not t1.pcm_tap)
pcm_taps.set_tap(gw, node2, True)
check('enabling AS2 cleared SDR2', node2.pcm_tap and not t2.pcm_tap)

st = sdr.get_status()
check('status reports per-tuner tap', st['tuner1_pcm_tap'] is False and st['tuner2_pcm_tap'] is False)

r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 2, 'on': '0'})
check('turning off is ok', r.get('ok') and r.get('pcm_tap') is False)

# --- refusals ---
t2._reader_running = False
r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 2, 'on': '1'})
check('refused when capture not running', r.get('ok') is False and not t2.pcm_tap, r)
check('refusal did not clear the active AS2 tap', node2.pcm_tap)
sdr._tuner2 = None
r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 2, 'on': '1'})
check('refused when tuner missing', r.get('ok') is False)
r = sdr.execute({'cmd': 'pcm_tap', 'tuner': 'x', 'on': '1'})
check('refused bad tuner', r.get('ok') is False)

check('parse_on', pcm_taps.parse_on('1') and pcm_taps.parse_on(True)
      and not pcm_taps.parse_on('0') and not pcm_taps.parse_on('') and not pcm_taps.parse_on(None))

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
