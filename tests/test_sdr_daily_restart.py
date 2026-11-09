#!/usr/bin/env python3
"""SDR daily restart scheduling (SDR_DAILY_RESTART_TIME)."""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

from plugins.sdr import daily_restart_due, parse_hhmm  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


check('parse 04:00', parse_hhmm('04:00') == (4, 0))
check('parse 23:59', parse_hhmm(' 23:59 ') == (23, 59))
for bad in ('', None, '24:00', '4', 'ab:cd', '12:60'):
    check(f'parse {bad!r} -> off', parse_hhmm(bad) is None)

d = datetime.date(2026, 11, 9)
at = lambda h, m: datetime.datetime(2026, 11, 9, h, m)  # noqa: E731
yesterday = d - datetime.timedelta(days=1)
check('before time: not due', not daily_restart_due(at(3, 59), (4, 0), yesterday))
check('at time: due', daily_restart_due(at(4, 0), (4, 0), yesterday))
check('later same day, missed slot: due', daily_restart_due(at(9, 0), (4, 0), yesterday))
check('already done today: not due', not daily_restart_due(at(4, 1), (4, 0), d))
check('disabled: never due', not daily_restart_due(at(4, 0), None, yesterday))
check('never run yet: due', daily_restart_due(at(4, 0), (4, 0), None))

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
