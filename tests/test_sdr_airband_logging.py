#!/usr/bin/env python3
"""rtl_airband must be launched so that its log goes somewhere.

In rtl_airband, '-e' only means "log to stderr" together with '-F'
(foreground). On its own it sets the log destination to NONE, and the SDR
plugin ran it that way for its whole life: pulse underflows, SoapySDR device
errors and buffer overflows were all silently dropped (found 2026-11-14).
Every rtl_airband command list in plugins/sdr.py must either leave '-e' out
(daemon, logs to syslog) or pair it with '-F'.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


src = open(os.path.join(ROOT, 'plugins', 'sdr.py')).read()
cmds = []
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.List) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and first.value == 'rtl_airband':
            args = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            cmds.append((node.lineno, args))

check('found the rtl_airband launches', len(cmds) >= 4, f'{len(cmds)} found')
bad = [ln for ln, a in cmds if '-e' in a and '-F' not in a]
check("no launch uses '-e' without '-F' (that logs to NONE)", not bad, f'lines {bad}')

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
