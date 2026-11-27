#!/usr/bin/env python3
"""Remote Kokoro TTS worker for the radio gateway.

Runs on another box (the macmini) so synthesis doesn't spin up the
gateway's fan. Self-registers with the gateway as kind 'tts'; the gateway's
tts_offload.OffloadKokoro sends it work and falls back to a local Kokoro
when it's missing or fails.

  POST /tts   {"text", "voice", "speed", "lang"}
              -> 200 raw little-endian float32 mono PCM, header X-Sample-Rate
  GET  /status

ONNX runs with --threads intra-op threads (default 4 of the mini's 8) and
at nice 10: below the real-time denoise worker, above transcription.
"""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker_common as wc  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_MODEL_DIR = os.path.join(_REPO, 'tools', 'models', 'kokoro')

_engine = None
_engine_lock = threading.Lock()   # Kokoro/ORT session is used one job at a time
_load_error = ''
_stats = {'requests': 0, 'ok': 0, 'errors': 0, 'chars': 0,
          'audio_s': 0.0, 'synth_s': 0.0, 'last_ms': None, 'last_error': ''}
_stats_lock = threading.Lock()
_started = time.time()


def _load(model_dir, threads):
    global _engine, _load_error
    try:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        model = os.path.join(model_dir, 'kokoro-v1.0.onnx')
        voices = os.path.join(model_dir, 'voices-v1.0.bin')
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(model, sess_options=opts,
                                    providers=['CPUExecutionProvider'])
        eng = Kokoro.from_session(sess, voices)
        t0 = time.monotonic()
        eng.create('Ready.', voice='af_heart', speed=1.0, lang='en-us')  # warm
        print(f'[tts] Kokoro loaded from {model_dir} ({threads} threads, '
              f'warmup {1000 * (time.monotonic() - t0):.0f} ms)', flush=True)
        _engine = eng
    except Exception as e:
        _load_error = str(e)
        print(f'[tts] Kokoro load FAILED: {e}', flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] != '/status':
            return self._json(404, {'error': 'not found'})
        with _stats_lock:
            st = dict(_stats)
        st.update(model_loaded=_engine is not None, load_error=_load_error,
                  uptime_s=round(time.time() - _started),
                  cpu_temp_c=wc.cpu_temp_c(), fan_rpm=wc.fan_rpm())
        self._json(200, st)

    def do_POST(self):
        if self.path.split('?')[0] != '/tts':
            return self._json(404, {'error': 'not found'})
        with _stats_lock:
            _stats['requests'] += 1
        try:
            n = int(self.headers.get('Content-Length', 0))
            req = json.loads(self.rfile.read(n) or b'{}')
            text = str(req.get('text', '')).strip()
            if not text:
                raise ValueError('empty text')
            if _engine is None:
                raise RuntimeError('model not loaded' + (f': {_load_error}' if _load_error else ''))
            t0 = time.monotonic()
            with _engine_lock:
                samples, rate = _engine.create(
                    text, voice=str(req.get('voice') or 'af_heart'),
                    speed=float(req.get('speed') or 1.0),
                    lang=str(req.get('lang') or 'en-us'))
            dt = time.monotonic() - t0
            import numpy as np
            pcm = np.asarray(samples, dtype='<f4').tobytes()
        except Exception as e:
            with _stats_lock:
                _stats['errors'] += 1
                _stats['last_error'] = str(e)
            return self._json(500, {'error': str(e)})
        with _stats_lock:
            _stats['ok'] += 1
            _stats['chars'] += len(text)
            _stats['audio_s'] += len(pcm) / 4 / rate
            _stats['synth_s'] += dt
            _stats['last_ms'] = round(dt * 1000)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('X-Sample-Rate', str(int(rate)))
        self.send_header('Content-Length', str(len(pcm)))
        self.end_headers()
        self.wfile.write(pcm)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    p = argparse.ArgumentParser(description='Radio gateway remote Kokoro TTS worker')
    wc.add_gateway_args(p, port=9801)
    p.add_argument('--model-dir', default=_DEFAULT_MODEL_DIR)
    p.add_argument('--threads', type=int, default=4, help='ONNX intra-op threads (default 4)')
    args = p.parse_args()
    wc.nice_by(10 if args.nice is None else args.nice)

    threading.Thread(target=_load, args=(args.model_dir, args.threads),
                     daemon=True, name='KokoroLoad').start()
    wc.start_registration(args, 'tts', tag='tts', extra_fn=lambda: {
        'model_loaded': _engine is not None, 'engine': 'kokoro', 'proto': 1})
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'[tts] Listening on {args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
