#!/usr/bin/env python3
"""BusManager.reload() must not interleave with another reload.

Link endpoints register on their own threads and each calls reload(); the
routing page calls it from an HTTP thread. Two endpoints on one host
reconnect together, so concurrent reloads are normal. Unserialised, one
reload cleared the bus dict while the other's fresh tick thread iterated it
("Bus reload error: dictionary changed size during iteration", 2026-11-08)
and two start() calls could leave two tick threads running.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

import metrics  # noqa: E402
from bus_manager import BusManager  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class _Probe(BusManager):
    """reload() as shipped, with stop/start replaced by overlap detectors."""

    def __init__(self):
        self._reload_lock = threading.RLock()
        self.reloads = 0
        self._bus_processors = {}
        self._busses = {}
        self.inside = 0
        self.max_inside = 0
        self.order = []
        self._g = threading.Lock()

    def _release_all_ptt(self):
        pass

    def stop(self):
        with self._g:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
            self.order.append('stop')
        time.sleep(0.01)

    def start(self):
        time.sleep(0.01)
        with self._g:
            self.order.append('start')
            self.inside -= 1


def _counter(name):
    for line in metrics.render()[0].decode().splitlines():
        if line.startswith(name + ' '):
            return float(line.split()[-1])
    return None


before = _counter('rg_bus_reloads_total')
p = _Probe()
threads = [threading.Thread(target=p.reload) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(5)

check('all reloads finished', not any(t.is_alive() for t in threads))
check('never two reloads between stop and start', p.max_inside == 1, p.max_inside)
check('stop/start strictly alternate', p.order == ['stop', 'start'] * 8, p.order)
check('reload counter', p.reloads == 8, p.reloads)
after = _counter('rg_bus_reloads_total')
check('rg_bus_reloads_total exported and counted', before is not None and after == before + 8,
      f'{before} -> {after}')
check('rg_bus_stale_tick_exits_total exported',
      _counter('rg_bus_stale_tick_exits_total') is not None)

# The lock is re-entrant: a reload triggered from inside a reload (a start()
# hook that asks for one) must not deadlock.
q = _Probe()
orig = q.start
state = {'n': 0}


def _nested_start():
    orig()
    if state['n'] == 0:
        state['n'] = 1
        q.reload()


q.start = _nested_start
t = threading.Thread(target=q.reload)
t.start()
t.join(3)
check('nested reload does not deadlock', not t.is_alive())

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
