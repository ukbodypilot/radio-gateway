#!/usr/bin/env python3
"""Offload workers: registry, remote DP8 denoise stream, remote Kokoro TTS.

The denoise tests run a real tools/denoise_worker.py serve loop on
localhost (with a fake stream, plus one pass with the real DP8 model) and
check the rules in remote_denoise.py: a miss is DRY, never local; no
blocking connect; a late reply is discarded, never played in a later slot.
"""

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402

import remote_workers  # noqa: E402
import remote_denoise  # noqa: E402
import tts_offload  # noqa: E402
import audio_util  # noqa: E402
import denoise_worker  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ── Registry ───────────────────────────────────────────────────────────────
print('registry')
clock = [100.0]
reg = remote_workers.WorkerRegistry(ttl=90, clock=lambda: clock[0])
check('unknown kind rejected', not reg.register('bogus', 'http://x:1')['ok'])
check('bad url rejected', not reg.register('tts', 'ftp://x:1')['ok'])
check('register ok', reg.register('tts', 'http://10.0.0.5:9801', 'mini')['ok'])
check('pick returns it', (reg.pick('tts') or {}).get('url') == 'http://10.0.0.5:9801')
check('other kind empty', reg.pick('denoise') is None)
reg.register('tts', 'http://10.0.0.9:9801', 'mini')   # same name, new address (DHCP / wired move)
snap = reg.snapshot()['tts']['workers']
check('same name at a new address replaces the old', [w['url'] for w in snap] == ['http://10.0.0.9:9801'], snap)
clock[0] += 91
check('expires after TTL', reg.pick('tts') is None)
check('expiry counted', reg.stats['tts']['expired'] == 1, reg.stats)


# ── Remote denoise ─────────────────────────────────────────────────────────
print('remote denoise')


class AddStream:
    """'Denoise' = add 1000 (x wet). Sleeps for chunks whose first sample is in slow."""
    dry_delay_samples = 0
    slow = {}

    def process_mix(self, x, wet):
        d = AddStream.slow.get(int(x[0]))
        if d:
            time.sleep(d)
        return (x.astype(np.int32) + int(1000 * wet)).astype(np.int16)

    def close(self):
        pass


def start_worker(make_stream):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(4)

    def loop():
        while True:
            try:
                c, a = srv.accept()
            except OSError:
                return
            threading.Thread(target=denoise_worker._serve, args=(c, a, make_stream),
                             daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv


def chunk(v, n=2400):
    return np.full(n, v, dtype=np.int16)


srv = start_worker(AddStream)
port = srv.getsockname()[1]
reg = remote_workers.WorkerRegistry()
st = remote_denoise.RemoteDP8Stream(registry=reg, bus='t1')

out = st.process_mix(chunk(5), 1.0)
check('no worker registered -> dry, immediately', int(out[0]) == 5 and st.counts['down'] == 1)

reg.register('denoise', f'tcp://127.0.0.1:{port}', 'local')
st._next_try = 0
t0 = time.monotonic()
out = st.process_mix(chunk(6), 1.0)
check('first chunk after a worker appears is dry and does not block on connect',
      int(out[0]) == 6 and time.monotonic() - t0 < 0.05)
check('connects in the background', wait_for(lambda: st.stats()['connected']), st.last_error)
out = st.process_mix(chunk(7), 0.5)
check('connected -> denoised by the worker (wet mix carried)', int(out[0]) == 7 + 500, int(out[0]))
check('ok counted', st.counts['ok'] == 1, st.counts)

AddStream.slow[20] = 0.2
t0 = time.monotonic()
out = st.process_mix(chunk(20), 1.0)
el = time.monotonic() - t0
check('late reply -> dry within the deadline', int(out[0]) == 20 and el < 0.15, (int(out[0]), el))
time.sleep(0.25)
out = st.process_mix(chunk(21), 1.0)
check('stale reply to the timed-out chunk is discarded, next chunk gets its own',
      int(out[0]) == 1021 and st.counts['stale'] == 1, (int(out[0]), st.counts))
check('timeout counted', st.counts['timeout'] == 1, st.counts)

# Worker dies -> dry, then reconnect when it comes back.
srv.close()
for t in threading.enumerate():
    pass
st._sock.shutdown(socket.SHUT_RDWR)
out = st.process_mix(chunk(30), 1.0)
check('link lost -> dry', int(out[0]) == 30 and not st.stats()['connected'], st.counts)
check('error counted', st.counts['error'] >= 1, st.counts)

srv2 = start_worker(AddStream)
reg.register('denoise', f'tcp://127.0.0.1:{srv2.getsockname()[1]}', 'local')
st._next_try = 0
st.process_mix(chunk(31), 1.0)
check('reconnects to the worker at its new address', wait_for(lambda: st.stats()['connected']), st.last_error)
out = st.process_mix(chunk(32), 1.0)
check('denoised again after reconnect', int(out[0]) == 1032, int(out[0]))
check('status() lists the stream', any(s['bus'] == 't1' for s in remote_denoise.status()))
st.close()
check('close drops the link', not st.stats()['connected'])

# Worker refuses an unsupported protocol.
probe = socket.create_connection(('127.0.0.1', srv2.getsockname()[1]))
probe.sendall(remote_denoise.pack(remote_denoise.M_HELLO, 99, 0, b'{}'))
m, _s, _v, p = remote_denoise.FrameReader(probe).read(time.monotonic() + 2)
check('worker refuses an unknown protocol version', m == remote_denoise.M_ERR, (m, p))
probe.close()

# Engine registry wiring.
check('dpdfnet_remote is a registered engine', 'dpdfnet_remote' in audio_util.DENOISE_ENGINE_IDS)
s2 = audio_util.make_denoise_stream('dpdfnet_remote')
check('make_denoise_stream builds a RemoteDP8Stream', isinstance(s2, remote_denoise.RemoteDP8Stream))
s2.close()

# Real DP8 through the worker: same output as the local stream, bit for bit.
srv3 = start_worker(denoise_worker._make_stream)
reg3 = remote_workers.WorkerRegistry()
reg3.register('denoise', f'tcp://127.0.0.1:{srv3.getsockname()[1]}', 'local')
rs = remote_denoise.RemoteDP8Stream(registry=reg3, bus='t3')
rs.REPLY_TIMEOUT_S = 1.0
rs.process_mix(chunk(0), 1.0)
check('real DP8 worker connects', wait_for(lambda: rs.stats()['connected'], 15), rs.last_error)
loc = audio_util._DPDFNet8kStream()
rng = np.random.default_rng(1)
same = True
for i in range(10):
    x = (rng.standard_normal(2400) * 3000).astype(np.int16)
    a = rs.process_mix(x, 0.7)
    b = loc.process_mix(x, 0.7)
    same = same and np.array_equal(a, b)
check('remote DP8 output == local DP8 output', same and rs.counts['ok'] == 10, rs.counts)
check('round trip measured', rs.stats()['rtt_ms_avg'] is not None)
rs.close()


# ── Remote TTS ─────────────────────────────────────────────────────────────
print('remote tts')
calls = []


class TTSHandler(BaseHTTPRequestHandler):
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(req)
        if TTSHandler.fail:
            self.send_response(500)
            self.end_headers()
            return
        pcm = np.linspace(-0.5, 0.5, 240, dtype='<f4').tobytes()
        self.send_response(200)
        self.send_header('X-Sample-Rate', '24000')
        self.send_header('Content-Length', str(len(pcm)))
        self.end_headers()
        self.wfile.write(pcm)


httpd = HTTPServer(('127.0.0.1', 0), TTSHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


class FakeLocal:
    n = 0

    def create(self, text, voice, speed, lang):
        FakeLocal.n += 1
        return np.zeros(10, dtype=np.float32), 24000


loads = []
treg = remote_workers.WorkerRegistry()
ok = tts_offload.OffloadKokoro(lambda: loads.append(1) or FakeLocal(), registry=treg)
samples, rate = ok.create('hello', voice='bf_emma', speed=1.0, lang='en-gb')
check('no worker -> local', FakeLocal.n == 1 and len(samples) == 10)
treg.register('tts', f'http://127.0.0.1:{httpd.server_port}', 'mini', {'model_loaded': False})
ok.create('hello', voice='bf_emma', speed=1.0, lang='en-gb')
check('worker still loading its model -> local', FakeLocal.n == 2 and not calls)
treg.register('tts', f'http://127.0.0.1:{httpd.server_port}', 'mini', {'model_loaded': True})
samples, rate = ok.create('hello there', voice='bf_emma', speed=1.1, lang='en-gb')
check('worker up -> remote', len(samples) == 240 and rate == 24000 and FakeLocal.n == 2)
check('voice/speed/lang passed through',
      calls and calls[-1] == {'text': 'hello there', 'voice': 'bf_emma', 'speed': 1.1, 'lang': 'en-gb'},
      calls[-1:])
check('local model loaded once, lazily', loads == [1])
TTSHandler.fail = True
samples, rate = ok.create('hello', voice='af_heart', speed=1.0, lang='en-us')
check('worker error -> local fallback', FakeLocal.n == 3 and len(samples) == 10)
st = tts_offload.status()
check('tts counters', st['remote'] >= 1 and st['fallback'] >= 1 and st['local'] >= 2, st)
httpd.shutdown()

print(f"\n{'ALL PASS' if not _fails else f'{_fails} FAILED'}")
sys.exit(1 if _fails else 0)
