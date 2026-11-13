#!/usr/bin/env python3
"""Denoise delay line: order-preserving, fixed-lag, off-tick denoise.

Before 2026-11-06 a worker that missed its one-tick slot made the tick emit
the CURRENT chunk dry and the stale denoised chunk next tick: an audible
skip-and-repeat ("drops" with both SDR buses on D). Now the tick emits the
chunk from DN_LAG ticks ago, denoised if ready, else THAT chunk dry, so
order never changes. Also: each denoise stream owns a 1-thread ONNX session
(a shared intra=2 session made two buses contend).
"""

import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402

import audio_util  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


N = 2400


def chunk(v):
    return np.full(N, v, dtype=np.int16).tobytes()


def val(b):
    return int(np.frombuffer(b, dtype=np.int16)[0])


class FakeStream:
    """'Denoise' = add 10000. Blocks on chunks whose value is in hold."""
    hold = set()
    release = threading.Event()

    def process_mix(self, samples, wet):
        if int(samples[0]) in FakeStream.hold:
            FakeStream.release.wait(timeout=5)
        return (samples.astype(np.int32) + 10000).astype(np.int16)

    def close(self):
        pass


audio_util.make_denoise_stream = lambda engine='dpdfnet': FakeStream()


def make_proc():
    cfg = types.SimpleNamespace(AUDIO_RATE=48000)
    p = audio_util.AudioProcessor('bus_test', cfg)
    p.enable_dfn = True
    p.dfn_mix = 1.0
    return p


def settle(p, timeout=2.0):
    """Wait until the worker has drained its input queue (an on-time tick)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        with p._dn_lock:
            if not p._dn_queue_in:
                time.sleep(0.01)
                return
        time.sleep(0.005)


# --- steady state: fixed lag, all denoised, in order ---
p = make_proc()
outs = []
for i in range(1, 9):
    outs.append(val(p.process(chunk(i * 100))))
    settle(p)
check('first DN_LAG ticks are silence (prefill)', outs[:2] == [0, 0], outs)
check('then chunk N-2, denoised, in order',
      outs[2:] == [10100, 10200, 10300, 10400, 10500, 10600], outs)
check('no late chunks on time', p.dn_late == 0)

# --- worker misses a slot: THAT chunk plays dry, order preserved ---
FakeStream.hold = {900}
FakeStream.release.clear()
seq_out = []
for v in (900, 1000, 1100):
    seq_out.append(val(p.process(chunk(v))))   # don't settle: worker is stuck on 900
seq_out.append(val(p.process(chunk(1200))))
FakeStream.release.set()
settle(p)
for v in (1300, 1400):
    seq_out.append(val(p.process(chunk(v))))
    settle(p)
# Expected emission: 700,800 (wet, done before the stall), then 900 and 1000
# dry (worker stuck on 900 when their slots came), then 1100 onwards wet.
check('late slot plays the SAME chunk dry, never reordered',
      seq_out == [10700, 10800, 900, 1000, 11100, 11200], seq_out)
check('late chunks counted', p.dn_late == 2, p.dn_late)
check('late wet results discarded, not replayed', p.dn_wet_discarded >= 1, p.dn_wet_discarded)
FakeStream.hold = set()

# --- silence bypass: goes through the line dry, not to the worker ---
p = make_proc()
o = []
for v in (500, 0, 0, 600, 700):
    o.append(val(p.process(chunk(v))))
    settle(p)
check('silent chunks keep the delay, pass dry', o == [0, 0, 10500, 0, 0], o)

# --- flush plays out the tail when the bus goes quiet ---
f1, f2, f3 = p.flush(), p.flush(), p.flush()
check('flush returns the held tail in order',
      f1 is not None and f2 is not None and (val(f1), val(f2)) == (10600, 10700),
      (f1 and val(f1), f2 and val(f2)))
check('flush returns None when empty', f3 is None)

# --- D toggled off then on: no stale audio replayed ---
p = make_proc()
for v in (100, 200, 300):
    p.process(chunk(v))
    settle(p)
p.enable_dfn = False
p.process(chunk(400))
p.enable_dfn = True
o = [val(p.process(chunk(500)))]
settle(p)
check('re-enable starts clean (prefill), no stale 200/300', o == [0], o)
check('flush is a no-op while D is off',
      (lambda q: (setattr(q, 'enable_dfn', False), q.flush())[1])(make_proc()) is None)

# --- long gap (bus muted) drops the stale line ---
p = make_proc()
for v in (100, 200, 300):
    p.process(chunk(v))
    settle(p)
p._dn_last_call -= 1.0
o = val(p.process(chunk(400)))
check('gap > DN_STALE_S resets the line', o == 0, o)

# --- block-in = block-out ---
check('output length == input length', len(p.process(chunk(1))) == N * 2)
p._dn_stop_worker()

# --- DPDFNet: private single-threaded session per stream ---
try:
    import onnxruntime  # noqa: F401
    have_ort = os.path.exists(audio_util._dpdfnet_model_path())
except Exception:
    have_ort = False
if have_ort:
    a, b = audio_util._DPDFNet8kStream(), audio_util._DPDFNet8kStream()
    check('DPDFNet streams do not share a session', a._sess is not b._sess)
    check('DPDFNet session is single-threaded',
          a._sess.get_session_options().intra_op_num_threads == 1)
    out = a.process(np.zeros(N, dtype=np.int16))
    check('DPDFNet stream still processes', out.size == N, out.size)
else:
    print('  SKIP  DPDFNet session checks (onnxruntime or bundled model missing)')

# --- removed engines fall back to the default, never to "denoise off" ---
check('rnnoise maps to dpdfnet',
      audio_util.normalize_denoise_engine('rnnoise') == 'dpdfnet')
check('deepfilternet maps to dpdfnet',
      audio_util.normalize_denoise_engine('deepfilternet') == 'dpdfnet')
check('missing engine maps to dpdfnet',
      audio_util.normalize_denoise_engine('') == 'dpdfnet')
q = audio_util.AudioProcessor('bus_legacy', types.SimpleNamespace(AUDIO_RATE=48000))
q.set_dfn_engine('rnnoise')
check('AudioProcessor accepts a legacy id as dpdfnet', q.dfn_engine == 'dpdfnet', q.dfn_engine)
q.set_dfn_engine('bogus')
check('AudioProcessor rejects an unknown id', q.dfn_engine == 'dpdfnet', q.dfn_engine)
check('only dpdfnet is registered', audio_util.DENOISE_ENGINE_IDS == ('dpdfnet',),
      audio_util.DENOISE_ENGINE_IDS)

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
