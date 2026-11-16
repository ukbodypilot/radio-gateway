#!/usr/bin/env python3
"""Speaker fingerprint offload: the worker's /embed gives the same vector as local, and
every failure falls back to local for that over.

Runs the real transcribe_worker HTTP handler in-process with the real
ERes2Net model from tools/models/speaker (skipped if onnxruntime is absent).
"""

import importlib.util
import os
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


if importlib.util.find_spec('onnxruntime') is None:
    print("  SKIP  onnxruntime not installed")
    sys.exit(0)

from speaker_id.embedder import Embedder            # noqa: E402
from speaker_id.remote import OffloadEmbedder       # noqa: E402

spec = importlib.util.spec_from_file_location('tw', os.path.join(ROOT, 'tools', 'transcribe_worker.py'))
tw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tw)

srv = tw.ThreadingHTTPServer(('127.0.0.1', 0), tw.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f'http://127.0.0.1:{port}'

rng = np.random.default_rng(3)
t = np.arange(16000 * 4) / 16000.0
audio = (0.2 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
         + 0.02 * rng.standard_normal(len(t))).astype(np.float32)

print("\n1. the worker advertises and serves the speaker model")
import json  # noqa: E402
import urllib.request  # noqa: E402
st = json.loads(urllib.request.urlopen(URL + '/status', timeout=10).read())
check("/status lists the speaker models it has", 'eres2net' in st.get('embed_models', []), str(st.get('embed_models')))

local = Embedder('eres2net')
ref = local.embed(audio)


def workers_ok():
    return [('box', URL, {'embed_models': ['eres2net'], 'reachable': True}, 0)]


off = OffloadEmbedder(lambda: Embedder('eres2net'), 'eres2net', workers_ok, 'auto')
v = off.embed(audio)
check("a remote fingerprint is the same vector as a local one", v is not None and float(v @ ref) > 0.9999,
      f'cos {float(v @ ref) if v is not None else None}')
check("...and was really computed remotely", off.counters['remote'] == 1 and off.counters['local'] == 0,
      str(dict(off.counters)))
check("too-short audio comes back as None, not an error", off.embed(audio[:1000]) is None)

print("\n2. fallback")
dead = OffloadEmbedder(lambda: Embedder('eres2net'), 'eres2net',
                       lambda: [('gone', 'http://127.0.0.1:9', {'embed_models': ['eres2net']}, 0)], 'auto')
t0 = time.time()
v = dead.embed(audio)
check("an unreachable worker falls back to local for that over", v is not None and float(v @ ref) > 0.9999
      and dead.counters['remote_failed'] == 1 and dead.counters['local'] == 1, str(dict(dead.counters)))
dead.embed(audio)
check("...and is skipped (backoff) instead of retried every over", dead.counters['remote_failed'] == 1,
      str(dict(dead.counters)))
nomodel = OffloadEmbedder(lambda: Embedder('eres2net'), 'eres2net',
                          lambda: [('old', URL, {'embed_models': []}, 0)], 'auto')
nomodel.embed(audio)
check("a worker without the model is never asked", nomodel.counters['local'] == 1 and not nomodel.counters['remote'])
wrong = OffloadEmbedder(lambda: Embedder('campplus'), 'campplus', workers_ok, 'auto')
wrong.embed(audio)
check("a worker that lacks the requested model is not used", wrong.counters['remote'] == 0
      and wrong.counters['local'] == 1, str(dict(wrong.counters)))
loc = OffloadEmbedder(lambda: Embedder('eres2net'), 'eres2net', workers_ok, 'local')
loc.embed(audio)
check("mode 'local' never offloads", loc.counters['local'] == 1 and not loc.counters['remote'])

srv.shutdown()
print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
