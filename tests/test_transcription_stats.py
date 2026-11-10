#!/usr/bin/env python3
"""TranscriptionLog.get_hourly_stats(): the data behind the TX Log charts.

The window is anchored on the newest row, not the clock: on the live box the
log had not been written for three months, and a clock-anchored window would
have charted nothing.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcription_log import TranscriptionLog   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class Cfg:
    TRANSCRIPTION_ALERT_KEYWORDS = ''


with tempfile.TemporaryDirectory() as d:
    cfg = Cfg()
    cfg.TRANSCRIPTION_LOG_PATH = os.path.join(d, 'tx.db')
    tl = TranscriptionLog(cfg)

    check('empty log returns no buckets', tl.get_hourly_stats(30)['buckets'] == [])

    # Newest row is far in the past relative to the wall clock.
    base = 1_700_000_000 - (1_700_000_000 % 3600)       # an exact hour
    rows = [(base + 10, 5.0), (base + 20, 7.5),         # hour 0: two rows
            (base + 3600 + 5, 1.0),                     # hour 1: one row
            (base - 40 * 86400, 9.0)]                   # 40 days older
    for ts, dur in rows:
        tl.insert({'timestamp': ts, 'text': 'x', 'duration': dur, 'source': 's'})

    s = tl.get_hourly_stats(30)
    check('newest is the newest row', s['newest'] == base + 3605, s['newest'])
    check('total counts every row', s['total'] == 4, s['total'])
    check('window anchored on newest row, old row excluded',
          s['buckets'] == [[base, 2, 12.5], [base + 3600, 1, 1.0]], s['buckets'])
    check('wider window includes the old row', len(tl.get_hourly_stats(60)['buckets']) == 3)
    check('days is clamped, never raises', len(tl.get_hourly_stats(0)['buckets']) == 2
          and len(tl.get_hourly_stats(10 ** 6)['buckets']) == 3)
    tl.close()
    check('closed log returns empty', tl.get_hourly_stats(30)['buckets'] == [])

print('ALL PASS' if not failed else f'{failed} FAILED')
sys.exit(1 if failed else 0)
