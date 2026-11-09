#!/usr/bin/env python3
"""CPU metrics: process, per-thread, helper-process and host CPU on /metrics."""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

import metrics  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


for raw, want in [('LinkReader-192.168.2.141', 'LinkReader'),
                  ('Thread-53 (audio_loop)', 'audio_loop'),
                  ('Thread-7', 'anonymous'),
                  ('SDR1-reader', 'SDR1-reader'),
                  ('loop-enc-sdr2', 'loop-enc-sdr2'),
                  ('worker-12', 'worker')]:
    got = metrics.thread_label(raw)
    check(f'label {raw!r} -> {want!r}', got == want, got)

# A named thread that burns CPU shows up under its own label.
stop = threading.Event()


def _burn():
    while not stop.is_set():
        sum(range(1000))


t = threading.Thread(target=_burn, name='cpu-test-burner', daemon=True)
t.start()
time.sleep(0.3)
body = metrics.render()[0].decode()
stop.set()
t.join()

lines = body.splitlines()
check('process CPU exported', any(x.startswith('rg_process_cpu_seconds_total ') for x in lines))
burn = [x for x in lines if x.startswith('rg_thread_cpu_seconds_total{thread="cpu-test-burner"}')]
check('named thread exported with CPU > 0', burn and float(burn[0].split()[-1]) > 0, burn)
check('host idle exported', any(x.startswith('rg_host_cpu_seconds_total{mode="idle"}') for x in lines))
check('helper process gauge exported', any(x.startswith('rg_helper_processes{') for x in lines))
check('no raw Thread-N labels', not any('thread="Thread-' in x for x in lines))

# Dashboard queries only reference metrics that exist.
dash = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'docs/grafana/dashboards/radio-gateway.json')))
exprs = [tg['expr'] for p in dash['panels'] for tg in p.get('targets', [])]
for m in ('rg_process_cpu_seconds_total', 'rg_thread_cpu_seconds_total',
          'rg_helper_cpu_seconds_total', 'rg_host_cpu_seconds_total'):
    check(f'dashboard plots {m}', any(m in e for e in exprs))
ids = [p['id'] for p in dash['panels']]
check('dashboard panel ids unique', len(ids) == len(set(ids)), ids)

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
