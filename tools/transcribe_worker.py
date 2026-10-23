#!/usr/bin/env python3
"""
Remote transcription worker.

Loads Moonshine or Whisper, serves inference over HTTP so the gateway can
offload ASR to a separate machine.  VAD always runs on the gateway; only
the ASR inference step is remote.

Endpoints:
  POST /transcribe   body: raw float32 LE bytes at 16 kHz
                     response: {"text": "...", "proc_time": 1.23}
  GET  /status       response: model info + health stats

With --gateway the worker also dials in and announces itself, the same way
link endpoints REGISTER on :9700, so the gateway never has to be told the
worker's address. The gateway reads the address off the socket, so DHCP
moves resolve themselves on the next heartbeat.

Usage:
  python3 transcribe_worker.py --model moonshine/base --port 9800
  python3 transcribe_worker.py --model whisper/medium.en --port 9800
  python3 transcribe_worker.py --model whisper/medium.en \
      --gateway http://192.168.2.140:8080 --name macmini
"""

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

# Locate transcribe_engine.py — works both when this file lives in tools/
# alongside a gateway checkout, and when deployed standalone in the same dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _search in (_HERE, _PARENT):
    if os.path.exists(os.path.join(_search, 'transcribe_engine.py')):
        sys.path.insert(0, _search)
        break

from transcribe_engine import LocalInferenceEngine, _VALID_MODELS  # noqa: E402

# ---------------------------------------------------------------------------
# Global state (set up in main, read-only after model loaded)
# ---------------------------------------------------------------------------

_engine: LocalInferenceEngine | None = None
_engine_lock = threading.Lock()   # guards _engine during load
_last_switch_error: str | None = None  # surfaces switch failures via /status
_stats_lock = threading.Lock()
_stats = {
    'total': 0,
    'errors': 0,
    'total_proc_secs': 0.0,
    'total_audio_secs': 0.0,
    'start_time': time.time(),
}


def _get_rss_mb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return 0.0


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _get_cpu_temp_c():
    """Highest reading across all thermal zones, in °C. None if unavailable."""
    import glob
    temps = []
    for f in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        v = _read_int(f)
        if v is not None and v > 0:
            temps.append(v / 1000.0)
    return round(max(temps), 1) if temps else None


def _get_fan_rpm():
    """First fan's current RPM via applesmc. None if not Apple hardware."""
    import glob
    for f in sorted(glob.glob('/sys/devices/platform/applesmc.*/fan*_input')):
        v = _read_int(f)
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log; errors still go to stderr

    # -- GET /status --

    def do_GET(self):
        if self.path.rstrip('/') != '/status':
            self._send(404, {'error': 'not found'})
            return
        with _stats_lock:
            st = dict(_stats)
        with _engine_lock:
            eng = _engine
        avg_ratio = (
            round(st['total_proc_secs'] / st['total_audio_secs'], 3)
            if st['total_audio_secs'] > 0 else None
        )
        payload = {
            'model_loaded': eng is not None and eng.is_loaded,
            'model_key': eng.model_key if eng else None,
            'engine': eng.engine if eng else None,
            'total_transcriptions': st['total'],
            'errors': st['errors'],
            'avg_ratio': avg_ratio,
            'uptime_secs': round(time.time() - st['start_time']),
            'ram_mb': _get_rss_mb(),
            'cpu_temp_c': _get_cpu_temp_c(),
            'fan_rpm': _get_fan_rpm(),
            'last_switch_error': _last_switch_error,
        }
        self._send(200, payload)

    # -- POST /model  (switch model at runtime) --

    def _handle_model_switch(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode())
        except Exception:
            self._send(400, {'error': 'invalid JSON'})
            return
        model_key = data.get('model', '')
        if model_key not in _VALID_MODELS:
            self._send(400, {'error': f'unknown model: {model_key}'})
            return
        t = threading.Thread(target=_switch_model, args=(model_key,), daemon=True)
        t.start()
        self._send(202, {'ok': True, 'model': model_key, 'status': 'loading'})

    # -- POST /transcribe --

    def do_POST(self):
        if self.path.rstrip('/') == '/model':
            self._handle_model_switch()
            return
        if self.path.rstrip('/') != '/transcribe':
            self._send(404, {'error': 'not found'})
            return

        with _engine_lock:
            eng = _engine
        if eng is None or not eng.is_loaded:
            self._send(503, {'error': 'model not loaded'})
            return

        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            self._send(400, {'error': 'empty body'})
            return

        raw = self.rfile.read(length)
        audio_16k = np.frombuffer(raw, dtype=np.float32)
        audio_secs = len(audio_16k) / 16000.0

        t0 = time.monotonic()
        try:
            text = eng.transcribe(audio_16k)
            proc_time = round(time.monotonic() - t0, 3)
            with _stats_lock:
                _stats['total'] += 1
                _stats['total_proc_secs'] += proc_time
                _stats['total_audio_secs'] += audio_secs
            self._send(200, {'text': text, 'proc_time': proc_time})
        except Exception as e:
            with _stats_lock:
                _stats['errors'] += 1
            self._send(500, {'error': str(e)})

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Gateway registration — the worker dials in, not the other way round
# ---------------------------------------------------------------------------

_REGISTER_PATH = '/transcribe_worker/register'


def _register_once(gateway_url, payload, auth_header=None, timeout=5):
    """POST one registration/heartbeat. Returns the parsed response dict,
    or None if the gateway was unreachable or answered badly."""
    import urllib.request
    import urllib.error
    body = json.dumps(payload).encode()
    headers = {'Content-Type': 'application/json'}
    if auth_header:
        headers['Authorization'] = auth_header
    req = urllib.request.Request(
        gateway_url.rstrip('/') + _REGISTER_PATH,
        data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _register_loop(gateway_url, name, port, interval, auth_header=None):
    """Announce to the gateway on a fixed heartbeat.

    Deliberately dumb and unconditional: it re-announces forever rather than
    registering once and trusting it stuck. A one-shot register would go
    stale the moment the gateway restarts — and a probe with no retry lies
    permanently. Only state *changes* are logged, so a healthy worker stays
    quiet in the journal.
    """
    last_ok = None
    last_error_log = 0.0
    while True:
        with _engine_lock:
            eng = _engine
        payload = {
            'name': name,
            'port': port,
            'model': getattr(eng, 'model_key', None) if eng else None,
            'engine': getattr(eng, 'engine', None) if eng else None,
            'model_loaded': bool(eng is not None and eng.is_loaded),
        }
        resp = _register_once(gateway_url, payload, auth_header)
        ok = bool(resp and resp.get('ok'))
        if ok != last_ok:
            if ok:
                print(f'[worker] Registered with gateway {gateway_url} '
                      f'as {resp.get("name", name)} '
                      f'(status={resp.get("status", "?")}, '
                      f'url={resp.get("url", "?")}, ttl={resp.get("ttl", "?")}s)',
                      flush=True)
            else:
                _why = (resp or {}).get('error', 'gateway unreachable')
                print(f'[worker] Registration with {gateway_url} failed: {_why}',
                      flush=True)
                last_error_log = time.time()
            last_ok = ok
        elif not ok and time.time() - last_error_log > 600:
            # Still down after 10 min — one line so a permanently broken
            # registration doesn't look like a healthy worker in the log.
            _why = (resp or {}).get('error', 'gateway unreachable')
            print(f'[worker] Still not registered with {gateway_url}: {_why}',
                  flush=True)
            last_error_log = time.time()
        # Heartbeat well inside the gateway's TTL. If the gateway told us its
        # TTL, aim for a third of it so two lost heartbeats don't expire us.
        _ttl = (resp or {}).get('ttl')
        _sleep = interval
        if isinstance(_ttl, (int, float)) and _ttl > 0:
            _sleep = max(5.0, min(interval, _ttl / 3.0))
        time.sleep(_sleep)


# ---------------------------------------------------------------------------
# Model loader thread
# ---------------------------------------------------------------------------

def _nice_down():
    try:
        import os as _os
        _os.nice(15)
    except Exception:
        pass


def _load_model(model_key):
    global _engine
    eng = LocalInferenceEngine(model_key)
    print(f'[worker] Loading {eng.model_key}...', flush=True)
    try:
        eng.load()
        print(f'[worker] Model ready', flush=True)
    except Exception as e:
        print(f'[worker] Failed to load model: {e}', flush=True)
        return
    with _engine_lock:
        _engine = eng


def _switch_model(model_key):
    """Load a new model then atomically swap it in, keeping old model serving meanwhile."""
    global _engine, _last_switch_error
    eng = LocalInferenceEngine(model_key)
    print(f'[worker] Switching to {eng.model_key}...', flush=True)
    try:
        eng.load()
    except Exception as e:
        msg = f'switch to {model_key} failed: {e}'
        print(f'[worker] {msg}', flush=True)
        _last_switch_error = msg
        return
    _last_switch_error = None
    with _engine_lock:
        old = _engine
        _engine = eng
    # Release native allocations. Two-step: clear Python refs + gc, then
    # call malloc_trim() so glibc returns the freed heap to the OS instead
    # of keeping it in its internal arena (without this RSS stays high).
    import gc
    if old is not None:
        old._model = None
        old._tokenizer = None
        del old
        gc.collect()
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass
    with _stats_lock:
        _stats['total'] = 0
        _stats['errors'] = 0
        _stats['total_proc_secs'] = 0.0
        _stats['total_audio_secs'] = 0.0
    print(f'[worker] Switched to {eng.model_key}', flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Radio gateway transcription worker')
    parser.add_argument('--model', default='moonshine/base',
                        help=f'Model key: {", ".join(sorted(_VALID_MODELS))}')
    parser.add_argument('--port', type=int, default=9800,
                        help='HTTP port to listen on (default 9800)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind address (default 0.0.0.0)')
    parser.add_argument('--gateway', default=os.environ.get('GATEWAY_URL', ''),
                        help='Gateway base URL, e.g. http://192.168.2.140:8080. '
                             'When set, the worker registers itself and '
                             'heartbeats, so the gateway needs no worker '
                             'address in config. Env: GATEWAY_URL')
    parser.add_argument('--name', default='',
                        help='Name shown on the gateway /transcribe page '
                             '(default: this host\'s hostname)')
    parser.add_argument('--register-interval', type=float, default=30.0,
                        help='Seconds between registration heartbeats '
                             '(default 30; clamped to the gateway TTL/3)')
    parser.add_argument('--gateway-username', default=os.environ.get('GATEWAY_USERNAME', 'user'),
                        help="The gateway's WEB_CONFIG_USERNAME (default 'user'). "
                             "Env: GATEWAY_USERNAME")
    parser.add_argument('--gateway-password', default=os.environ.get('GATEWAY_PASSWORD', 'user'),
                        help="The gateway's WEB_CONFIG_PASSWORD (default 'user', "
                             "the gateway's default). Env: GATEWAY_PASSWORD")
    args = parser.parse_args()

    model_key = args.model
    if model_key not in _VALID_MODELS:
        print(f'[worker] Unknown model {model_key!r}. '
              f'Valid: {", ".join(sorted(_VALID_MODELS))}', flush=True)
        sys.exit(1)

    _nice_down()

    loader = threading.Thread(target=_load_model, args=(model_key,), daemon=True)
    loader.start()

    if args.gateway:
        import socket as _socket
        _name = args.name or _socket.gethostname()
        _auth = None
        if args.gateway_password:
            import base64 as _b64
            _auth = 'Basic ' + _b64.b64encode(
                f'{args.gateway_username}:{args.gateway_password}'.encode()).decode()
        # Started before serve_forever so the first heartbeat goes out while
        # the model is still loading — the gateway sees the worker as present
        # but not ready, rather than missing entirely for the ~30s load.
        threading.Thread(
            target=_register_loop,
            args=(args.gateway, _name, args.port, args.register_interval, _auth),
            daemon=True, name='GatewayRegister').start()
        print(f'[worker] Registering with gateway {args.gateway} as {_name}', flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'[worker] Listening on {args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[worker] Stopping', flush=True)


if __name__ == '__main__':
    main()
