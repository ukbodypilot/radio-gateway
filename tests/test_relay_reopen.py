#!/usr/bin/env python3
"""CH340 relay survives the module re-enumerating (2026-11-22).

Moving the USB devices gave the relay module a new ttyUSB node. udev moved the
/dev/relay_radio symlink, but RelayController kept its fd to the old node, every
write failed, and the radio power relay stayed dead until a gateway restart.
Pins: a failed write closes, reopens once and retries; no port at all also
reopens; a failed reopen returns False instead of raising.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ptt                       # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class Port:
    def __init__(self, dead=False):
        self.dead, self.writes, self.closed = dead, [], False

    def write(self, data):
        if self.dead:
            raise OSError(5, 'Input/output error')
        self.writes.append(data)

    def close(self):
        self.closed = True


def with_serial(factory):
    fake = types.ModuleType('serial')
    fake.Serial = factory
    real = sys.modules.get('serial')
    sys.modules['serial'] = fake
    return real


def restore(real):
    if real is not None:
        sys.modules['serial'] = real
    else:
        sys.modules.pop('serial', None)


# 1. dead fd -> reopen -> retry succeeds
fresh = Port()
real = with_serial(lambda *a, **k: fresh)
try:
    r = ptt.RelayController('/dev/relay_radio')
    old = Port(dead=True)
    r._port = old
    ok = r.set_state(True)
finally:
    restore(real)
check('write after replug succeeds', ok is True)
check('dead port closed', old.closed)
check('command landed on the fresh port', fresh.writes == [ptt.RelayController.CMD_ON])
check('state recorded', r.state is True)

# 2. never opened -> opens on first use
fresh = Port()
real = with_serial(lambda *a, **k: fresh)
try:
    r = ptt.RelayController('/dev/relay_radio')
    ok = r.set_state(False)
finally:
    restore(real)
check('no port: opens on demand', ok is True and fresh.writes == [ptt.RelayController.CMD_OFF])


# 3. device gone for good -> False, no exception
def gone(*a, **k):
    raise OSError(2, 'No such file or directory')


real = with_serial(gone)
try:
    r = ptt.RelayController('/dev/relay_radio')
    r._port = Port(dead=True)
    try:
        ok = r.set_state(True)
        raised = False
    except Exception:
        raised = True
finally:
    restore(real)
check('unplugged for good: returns False', ok is False and not raised)

print('FAIL' if failed else 'PASS')
sys.exit(1 if failed else 0)
