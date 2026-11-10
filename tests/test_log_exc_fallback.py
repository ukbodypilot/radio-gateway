#!/usr/bin/env python3
"""gateway_link must report swallowed exceptions on an endpoint too.

Endpoints receive gateway_link.py without the gateway's log_util, so the
module carries a minimal log_exc of its own. An endpoint plugin deployed
beside an OLDER gateway_link (no log_exc at all) must still start.
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def run(cwd, code):
    r = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, ".")\n' + code],
                       cwd=cwd, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


PROBE = '''
import gateway_link as g
print("FALLBACK" if g.log_exc.__module__ == "gateway_link" else "LOG_UTIL")
def boom():
    try:
        raise ValueError("kaput")
    except Exception:
        g.log_exc()
for _ in range(5):
    boom()
try:
    g.log_exc()          # no active exception: must not raise
except Exception as e:
    print("RAISED", e)
'''

with tempfile.TemporaryDirectory() as d:
    shutil.copy(os.path.join(ROOT, 'gateway_link.py'), d)
    rc, out, err = run(d, PROBE)
    lines = [x for x in out.splitlines() if x.startswith('[swallowed]')]
    check('imports without log_util', rc == 0, err[-300:])
    check('uses its own fallback', 'FALLBACK' in out, out[:200])
    check('reports the exception with its site',
          any('ValueError: kaput' in x and 'in boom' in x for x in lines), out[:300])
    check('rate-limited to one line per site', sum('in boom' in x for x in lines) == 1, len(lines))
    check('never raises', 'RAISED' not in out)

    # Plugin beside an older gateway_link that has no log_exc.
    with open(os.path.join(d, 'gateway_link.py'), 'w') as f:
        f.write('class RadioPlugin:\n    pass\n')
    guard = open(os.path.join(ROOT, 'tools', 'd75_link_plugin.py')).read()
    start = guard.index('try:\n    from gateway_link import log_exc')
    end = guard.index('\n\n', guard.index('def log_exc', start))
    rc, out, err = run(d, guard[start:end] + '\ntry:\n    1/0\nexcept Exception:\n    log_exc()\nprint("STARTED")\n')
    check('plugin guard survives an older gateway_link', rc == 0 and 'STARTED' in out, err[-300:])

rc, out, err = run(ROOT, 'import gateway_link as g; print(g.log_exc.__module__)')
check('on the gateway it is log_util.log_exc', out.strip() == 'log_util', out + err[-200:])

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
