#!/usr/bin/env python3
"""GPS reader reopens its port after the receiver is unplugged (2026-11-22).

Moving the USB devices re-enumerated the u-blox receiver. The inner read loop's
`except Exception: log_exc()` swallowed the SerialException, so readline() on the
dead fd failed instantly forever: ~6M exceptions/min, 84% of a core, and no GPS
until a gateway restart. Pins: a SerialException closes the dead port and the
outer loop opens a fresh one; a bad NMEA line is still just logged and skipped.
"""

import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial                    # noqa: E402
import gps_manager               # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


GGA = b'$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n'


def run(script):
    """script: one list of readline() results per port open; an Exception item is raised."""
    opened, closed = [], []
    stop = threading.Event()
    reads = {'n': 0}

    class FakeSerial:
        def __init__(self, *a, **k):
            self.lines = list(script[len(opened)]) if len(opened) < len(script) else []
            opened.append(self)

        def readline(self):
            reads['n'] += 1
            if reads['n'] > 200:          # a runaway loop: stop the test, don't hang
                stop.set()
                raise serial.SerialException('runaway')
            if not self.lines:
                stop.set()
                return b''
            item = self.lines.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def close(self):
            closed.append(self)

    cfg = types.SimpleNamespace(GPS_PORT='/dev/gps', GPS_BAUD=9600)
    g = gps_manager.GPSManager(cfg)
    g._stop = stop
    real_serial, real_wait = serial.Serial, stop.wait
    serial.Serial = FakeSerial
    stop.wait = lambda t=None: stop.is_set()   # no 5 s retry sleep in the test
    try:
        t = threading.Thread(target=g._run, daemon=True)
        t.start()
        t.join(5)
    finally:
        serial.Serial, stop.wait = real_serial, real_wait
    return g, opened, closed, reads['n']


dead = serial.SerialException('device reports readiness to read but returned no data')
g, opened, closed, n = run([[GGA, dead, dead, dead], [GGA]])
check('unplug closes the dead port', len(closed) >= 1 and closed[0] is opened[0])
check('then reopens the port', len(opened) == 2, f'opened {len(opened)}x')
check('no runaway read loop on the dead fd', n < 200, f'{n} reads')
check('position parsed after the reopen', g.lat is not None and abs(g.lat - 48.1173) < 1e-3, f'lat={g.lat}')

g, opened, closed, n = run([[b'$GPGGA,garbage*00\r\n', GGA]])
check('bad NMEA line does not drop the port', len(opened) == 1, f'opened {len(opened)}x')

print('FAIL' if failed else 'PASS')
sys.exit(1 if failed else 0)
