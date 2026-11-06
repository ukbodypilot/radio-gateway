#!/usr/bin/env python3
"""PCM monitor taps: AS1/AS2/SDR1/SDR2 shell-bar buttons.

An SDR tap carries the PROCESSED output of the bus its tuner is routed to
(the bus's gate/HPF/LPF/notch/denoise), never a second get_chunk() call.
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


# --- SDR tuner taps follow their bus ---
t1 = make_tuner('SDR1')
t1.pcm_tap = True
t1._chunk_queue.put_nowait(stereo_chunk(2000))
out = t1.get_chunk()
check('tuner is bus-following', t1.tap_from_bus is True)
check('get_chunk serves the bus a mono chunk', out is not None and len(out) == 4800)
check('get_chunk no longer self-queues a tap', not hasattr(t1, '_tap_queue'))

class FakeSlot:
    def __init__(self, src): self.source = src

class FakeBus:
    def __init__(self, *srcs): self.source_slots = [FakeSlot(x) for x in srcs]

t_other = make_tuner('SDR2')
busses = {'a': FakeBus(t_other), 'b': FakeBus(t1), 'c': FakeBus(t1)}
check('tap_bus_id finds the bus the tuner is on', pcm_taps.tap_bus_id(busses, t1) == 'b')
check('routed to two buses -> first in order', pcm_taps.tap_bus_id(busses, t1) == 'b')
check('not routed -> None', pcm_taps.tap_bus_id({'a': FakeBus(t_other)}, make_tuner('X')) is None)
check('no buses -> None', pcm_taps.tap_bus_id(None, t1) is None)

# Real bus classes, wired the way bus_manager wires them. SDR1 -> PAPA (solo)
# is a TX source on a SoloBus, not a source_slot — the bug that made the SDR1
# tap silent while SDR2 (listen bus) worked.
from audio_bus import SoloBus, ListenBus  # noqa: E402
_bcfg = types.SimpleNamespace()
solo = SoloBus('PAPA', _bcfg)
t1.ptt_control = False
solo.add_tx_source(t1, routing_id='sdr1')
check('solo bus: TX source found', pcm_taps.tap_bus_id({'allstar': solo}, t1) == 'allstar')
solo_r = SoloBus('R', _bcfg)
solo_r.set_radio(t_other, routing_id='x')
check('solo bus: radio found', pcm_taps.tap_bus_id({'r': solo_r}, t_other) == 'r')
try:
    lb = ListenBus('SDR2', _bcfg)
    lb.add_source(t_other, bus_priority=1, routing_id='sdr2')
    check('listen bus: source found', pcm_taps.tap_bus_id({'sdr2': lb}, t_other) == 'sdr2')
except Exception as e:
    check('listen bus constructed', False, repr(e))

# BusManager tap section: tap carries the bus's PROCESSED output, not the raw
# tuner chunk. Drive the real _deliver_audio with a processor that inverts.
import bus_manager  # noqa: E402
bm = bus_manager.BusManager.__new__(bus_manager.BusManager)
bm._bus_final_tick = {}
bm._pcm_tick, bm._mp3_tick = [], []
bm._bus_processors = {'b': types.SimpleNamespace(process=lambda raw: b'P' + raw)}
bm._bus_config = {'b': {}}
bm._bus_delay_bufs = {}
bm._listen_bus_id = None
bm._meters = collections.defaultdict(int)
bm._link_tx_meters = {}
bm.gateway = types.SimpleNamespace()
bm.config = types.SimpleNamespace()
bo = types.SimpleNamespace(mixed_audio=out, audio={})
ctx = types.SimpleNamespace(stream_trace=None, loop_recorder=None)
try:
    bm._deliver_audio(bo, 'b', ctx)
    check('_deliver_audio records processed bus output', bm._bus_final_tick.get('b') == b'P' + out)
    check('pcm flag off: nothing staged for the stream by the bus itself', bm._pcm_tick == [])
except Exception as e:
    check('_deliver_audio ran', False, repr(e))

# --- exclusivity across kinds ---
node1, node2 = FakeNode(), FakeNode()
sdr = SDRPlugin()
t2 = make_tuner('SDR2')
t1.pcm_tap = False
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
