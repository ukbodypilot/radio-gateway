#!/usr/bin/env python3
"""SDR_PRIORITY_ORDER decides which tuner ducks the other on the listen bus.

The config form offered sdr1 / sdr2 / equal, but BusManager registered both
tuners as separate listen-bus sources at the same priority (11). Peer ducking
only applies to a strictly lower priority, so both tuners always played
together whatever the setting said. SDRPlugin's own master/slave ducking read
the key, but it only runs on the legacy combined 'sdr' source.

Pins: each order gives the tuners the right sdr_priority; sync_listen_bus
turns that into 11 / 11.5 bus priorities; and on a real ListenBus the master
tuner ducks the slave, the slave never ducks the master, and 'equal' ducks
neither.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                    # noqa: E402

from audio_bus import ListenBus                       # noqa: E402
from bus_manager import BusManager                    # noqa: E402
from plugins.sdr import SDRPlugin                     # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


TONE = (np.sin(np.arange(2400) * 0.1) * 12000).astype(np.int16).tobytes()


class FakeTuner:
    """Stands in for _TunerCapture: always has loud audio."""

    def __init__(self, name):
        self.name = name
        self.enabled = True
        self.ptt_control = True
        self.duck = True
        self.sdr_priority = 1

    def get_audio(self, chunk_size=None):
        return TONE, False


def plugin_with_order(order):
    """An SDRPlugin with fake tuners, priorities applied as setup() does."""
    p = SDRPlugin()
    p._sdr_order = order
    p._master_name = 'SDR2' if order == 'sdr2' else 'SDR1'
    p._tuner1, p._tuner2 = FakeTuner('SDR1'), FakeTuner('SDR2')
    p._apply_tuner_priorities()
    return p


def bus_priorities(plugin):
    """Run BusManager.sync_listen_bus's source_map for the SDR tuners."""
    config = types.SimpleNamespace(SDR_DUCK=True)
    bm = BusManager.__new__(BusManager)
    bm.gateway = types.SimpleNamespace(sdr_plugin=plugin, config=config, link_endpoints={})
    added = {}

    class _Bus:
        source_slots = []

        def add_source(self, source, bus_priority, **kw):
            added[kw.get('routing_id')] = bus_priority

        def remove_source(self, name):
            pass

    bm.listen_bus = _Bus()
    bm._listen_bus_id = 'listen'
    import json
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump({'connections': [
            {'type': 'source-bus', 'from': 'sdr1', 'to': 'listen'},
            {'type': 'source-bus', 'from': 'sdr2', 'to': 'listen'},
        ]}, f)
    bm._config_path = f.name
    # Tuners are already "active" so sync doesn't try to start parec.
    plugin._tuner1.active = plugin._tuner2.active = True
    try:
        bm.sync_listen_bus()
    finally:
        os.unlink(f.name)
    return added


def ducked_after_ticks(plugin, ticks=5):
    """Feed both tuners into a real ListenBus; return names ducked on the last tick."""
    config = types.SimpleNamespace(SIGNAL_ATTACK_TIME=0.0, SIGNAL_RELEASE_TIME=3.0,
                                   SDR_SIGNAL_THRESHOLD=-60.0, SDR_DUCK_COOLDOWN=0.0,
                                   SWITCH_PADDING_TIME=0.0, REDUCK_INHIBIT_TIME=0.0)
    bus = ListenBus('listen', config)
    for t in (plugin._tuner1, plugin._tuner2):
        bus.add_source(t, bus_priority=10 + t.sdr_priority, duckable=True,
                       deterministic=True, routing_id=t.name.lower())
    out = None
    for _ in range(ticks):
        out = bus.tick(2400)
    return set(out.ducked_sources)


def main():
    print("1. Tuner sdr_priority per order")
    for order, want in (('sdr1', (1, 1.5)), ('sdr2', (1.5, 1)), ('equal', (1, 1))):
        p = plugin_with_order(order)
        got = (p._tuner1.sdr_priority, p._tuner2.sdr_priority)
        check(f"{order}: SDR1/SDR2 = {want}", got == want, str(got))

    print("\n2. sync_listen_bus uses them")
    for order, want in (('sdr1', {'sdr1': 11, 'sdr2': 11.5}),
                        ('sdr2', {'sdr1': 11.5, 'sdr2': 11}),
                        ('equal', {'sdr1': 11, 'sdr2': 11})):
        got = bus_priorities(plugin_with_order(order))
        check(f"{order}: bus priorities {want}", got == want, str(got))

    print("\n3. ListenBus ducking with both tuners active")
    for order, want in (('sdr1', {'SDR2'}), ('sdr2', {'SDR1'}), ('equal', set())):
        got = ducked_after_ticks(plugin_with_order(order))
        check(f"{order}: ducked = {want or '{}'}", got == want, str(got))

    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
