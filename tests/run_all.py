#!/usr/bin/env python3
"""Run every tests/test_*.py, each in its own process, and summarise.

    python3 tests/run_all.py            # all tests
    python3 tests/run_all.py ptt config # only tests whose name contains these

Each test is a plain script that exits non-zero on failure. Hardware
libraries that are missing are stubbed (tests/_hwstubs.py), so this runs the
same on the gateway and in CI. Used by .github/workflows/ci.yml.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT = 300

_BOOT = ("import sys, runpy; sys.path.insert(0, {here!r}); import _hwstubs; "
         "sys.argv = [{path!r}]; runpy.run_path({path!r}, run_name='__main__')")


def main(filters):
    tests = sorted(f for f in os.listdir(HERE)
                   if f.startswith('test_') and f.endswith('.py')
                   and (not filters or any(x in f for x in filters)))
    failed = []
    for name in tests:
        path = os.path.join(HERE, name)
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, '-c', _BOOT.format(here=HERE, path=path)],
                               cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
            rc, out = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            rc, out = 'timeout', (e.stdout or b'').decode(errors='replace')
        took = time.monotonic() - t0
        ok = rc == 0
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({took:.1f}s)", flush=True)
        if not ok:
            failed.append(name)
            print('\n'.join('      ' + line for line in out.strip().splitlines()[-40:]))
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
