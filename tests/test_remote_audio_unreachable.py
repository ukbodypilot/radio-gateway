#!/usr/bin/env python3
"""RemoteAudioServer: an offline client is reported once per outage, not
logged as a swallowed exception on every retry."""

import io
import os
import socket
import sys
import time
import types
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

import sources.network as net  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


swallowed = []
net.log_exc = lambda *a, **k: swallowed.append(1)

# Grab a free port, then close it so connects are refused.
probe = socket.socket()
probe.bind(('127.0.0.1', 0))
port = probe.getsockname()[1]
probe.close()

cfg = types.SimpleNamespace(REMOTE_AUDIO_HOST='127.0.0.1', REMOTE_AUDIO_PORT=port,
                            REMOTE_AUDIO_RECONNECT_INTERVAL=0.05)
srv = net.RemoteAudioServer(cfg)
srv._RECONNECT_MAX = 0.1
out = io.StringIO()
with redirect_stdout(out):
    srv._running = True
    import threading
    t = threading.Thread(target=srv._connect_loop, daemon=True)
    t.start()
    time.sleep(0.8)
    fails_down = srv.connect_failures
    # Client comes up.
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(('127.0.0.1', port))
    ls.listen(1)
    deadline = time.time() + 3
    while not srv.connected and time.time() < deadline:
        time.sleep(0.02)
    up = srv.connected
    srv._running = False
    t.join(timeout=2)
    ls.close()

log = out.getvalue()
check('several failed attempts while down', fails_down >= 3, fails_down)
check('no swallowed-exception logs for an offline client', not swallowed, len(swallowed))
check('unreachable reported exactly once', log.count('unreachable') == 1, log)
check('reconnects when the client comes back', up)
check('recovery reported with outage length', 'Client back after' in log, log)

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
