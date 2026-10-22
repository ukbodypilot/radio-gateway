#!/usr/bin/env python3
"""log_exc(): one line per call site, then quiet for the interval.

It replaced 176 `except Exception: pass` blocks, several inside the 50 ms
tick loop, so it must not flood the log and must never raise.
"""

import contextlib
import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_util            # noqa: E402
from log_util import log_exc   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def boom(interval=300.0):
    try:
        raise ValueError('bad value')
    except Exception:
        log_exc(interval=interval)


def captured(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


def main():
    print("1. first failure is logged with its site")
    out = captured(boom)
    check("one line", out.count('\n') == 1, repr(out))
    check("names file, line and function", 'test_log_util.py:' in out and 'in boom' in out, out)
    check("names the exception", 'ValueError: bad value' in out, out)

    print("\n2. repeats inside the interval are counted, not printed")
    out = ''.join(captured(boom) for _ in range(500))
    check("silent", out == '', repr(out[:200]))

    print("\n3. after the interval it reports again, with the count")
    log_util._last.clear(); log_util._suppressed.clear()
    captured(boom, interval=0.05)
    for _ in range(3):
        captured(boom, interval=0.05)
    time.sleep(0.06)
    out = captured(boom, interval=0.05)
    check("reported again", 'ValueError' in out, repr(out))
    check("says how many were skipped", '+3 more' in out, repr(out))

    print("\n4. explicit keys are independent sites")
    log_util._last.clear()

    def keyed(k):
        try:
            1 / 0
        except Exception:
            log_exc(key=k)
    out = captured(keyed, 'a') + captured(keyed, 'b') + captured(keyed, 'a')
    check("a and b each once", out.count('[swallowed] a:') == 1 and out.count('[swallowed] b:') == 1, out)

    print("\n5. never raises, from any thread")
    errs = []

    def worker():
        try:
            for _ in range(2000):
                boom()
            log_exc()   # no active exception
        except Exception as e:   # pragma: no cover
            errs.append(e)
    with contextlib.redirect_stdout(io.StringIO()):
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    check("no errors", not errs, str(errs))

    print("\n6. cheap enough for the tick loop")
    t0 = time.perf_counter()
    for _ in range(20000):
        boom()
    per = (time.perf_counter() - t0) / 20000 * 1e6
    check(f"suppressed call under 20 us ({per:.1f} us incl. raise)", per < 20)

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
