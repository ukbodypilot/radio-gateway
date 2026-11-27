#!/usr/bin/env python3
"""Remote real-time DP8 denoise worker for the radio gateway.

Runs on another box (the macmini) so every bus can have denoise without
loading the gateway's CPU. Self-registers with the gateway as kind
'denoise'; buses whose engine is 'dpdfnet_remote' open one TCP connection
each (remote_denoise.RemoteDP8Stream) and send 50 ms chunks.

One connection = one audio_util._DPDFNet8kStream (its own single-threaded
ONNX session and recurrent state), served by its own thread. The wire
format is in remote_denoise.py.

Priority: nice 0 by default. This is the only deadline-bound worker on the
box; TTS runs at nice 10 and transcription at 15, so a long transcription
never starves a live bus.

  GET http://<host>:<port+1>/status  — JSON counters (stdlib HTTP)
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TOOLS)
sys.path.insert(0, _TOOLS)
sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402

import worker_common as wc  # noqa: E402
import remote_denoise as rd  # noqa: E402

_started = time.time()
_lock = threading.Lock()
_conns = {}            # id -> per-connection stats dict
_totals = {'connections': 0, 'chunks': 0, 'errors': 0, 'refused': 0}
_next_id = 0
_max_streams = 6


def _make_stream():
    import audio_util
    return audio_util._DPDFNet8kStream()


def _serve(conn, addr, make_stream=_make_stream):
    global _next_id
    rd.tune_socket(conn)
    reader = rd.FrameReader(conn)
    stream = None
    with _lock:
        _next_id += 1
        cid = _next_id
    st = {'peer': f'{addr[0]}:{addr[1]}', 'bus': '?', 'since': time.time(),
          'chunks': 0, 'proc_ms_avg': None, 'proc_ms_max': 0.0}
    try:
        magic, seq, _v, payload = reader.read(time.monotonic() + 10)
        if magic != rd.M_HELLO:
            raise ConnectionError(f'expected hello, got {magic!r}')
        hello = json.loads(payload or b'{}')
        st['bus'] = str(hello.get('bus', '?'))
        err = None
        if seq != rd.PROTO:
            err = f'protocol {seq} not supported (worker speaks {rd.PROTO})'
        elif int(hello.get('rate', 48000)) != 48000:
            err = 'only 48 kHz is supported'
        with _lock:
            if err is None and len(_conns) >= _max_streams:
                err = f'worker full ({_max_streams} streams)'
        if err:
            with _lock:
                _totals['refused'] += 1
            conn.sendall(rd.pack(rd.M_ERR, rd.PROTO, 0.0, json.dumps({'error': err}).encode()))
            print(f'[denoise] refused {st["peer"]} ({st["bus"]}): {err}', flush=True)
            return
        t0 = time.monotonic()
        stream = make_stream()
        init_ms = (time.monotonic() - t0) * 1000.0
        with _lock:
            _conns[cid] = st
            _totals['connections'] += 1
        conn.sendall(rd.pack(rd.M_OK, rd.PROTO, init_ms, json.dumps(
            {'engine': 'dpdfnet', 'dry_delay_samples': stream.dry_delay_samples}).encode()))
        print(f'[denoise] stream for {st["bus"]} from {st["peer"]} ready '
              f'({init_ms:.0f} ms)', flush=True)
        while True:
            magic, seq, wet, payload = reader.read()
            if magic != rd.M_REQ:
                raise ConnectionError(f'unexpected frame {magic!r}')
            t0 = time.monotonic()
            x = np.frombuffer(payload, dtype='<i2').astype(np.int16)
            y = stream.process_mix(x, wet)
            if y.size < x.size:
                y = np.concatenate([y, x[y.size:]])
            elif y.size > x.size:
                y = y[:x.size]
            ms = (time.monotonic() - t0) * 1000.0
            conn.sendall(rd.pack(rd.M_RSP, seq, ms, y.astype('<i2').tobytes()))
            st['chunks'] += 1
            st['proc_ms_avg'] = ms if st['proc_ms_avg'] is None else 0.95 * st['proc_ms_avg'] + 0.05 * ms
            st['proc_ms_max'] = max(st['proc_ms_max'], ms)
            with _lock:
                _totals['chunks'] += 1
    except (ConnectionError, OSError) as e:
        if stream is not None:
            print(f'[denoise] stream for {st["bus"]} closed ({e}) after {st["chunks"]} chunks',
                  flush=True)
    except Exception as e:
        with _lock:
            _totals['errors'] += 1
        print(f'[denoise] stream for {st["bus"]} failed: {e!r}', flush=True)
    finally:
        with _lock:
            _conns.pop(cid, None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def status():
    with _lock:
        conns = [dict(c, proc_ms_avg=None if c['proc_ms_avg'] is None else round(c['proc_ms_avg'], 2),
                      proc_ms_max=round(c['proc_ms_max'], 2)) for c in _conns.values()]
        tot = dict(_totals)
    return {'streams': conns, 'totals': tot, 'max_streams': _max_streams,
            'uptime_s': round(time.time() - _started),
            'cpu_temp_c': wc.cpu_temp_c(), 'fan_rpm': wc.fan_rpm(),
            'loadavg': os.getloadavg()}


class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        body = json.dumps(status()).encode()
        self.send_response(200 if self.path.split('?')[0] == '/status' else 404)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global _max_streams
    p = argparse.ArgumentParser(description='Radio gateway remote DP8 denoise worker')
    wc.add_gateway_args(p, port=9802)
    p.add_argument('--max-streams', type=int, default=6,
                   help='Concurrent bus streams (one core-ish each at worst; default 6)')
    args = p.parse_args()
    _max_streams = max(1, args.max_streams)
    wc.nice_by(0 if args.nice is None else args.nice)

    # Fail at start, not at the first bus, if the model or ORT is missing.
    _make_stream().close()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(16)
    status_srv = HTTPServer((args.host, args.port + 1), StatusHandler)
    threading.Thread(target=status_srv.serve_forever, daemon=True, name='Status').start()
    wc.start_registration(args, 'denoise', tag='denoise', extra_fn=lambda: {
        'engine': 'dpdfnet', 'proto': rd.PROTO, 'streams': len(_conns),
        'model_loaded': True})
    print(f'[denoise] Listening on {args.host}:{args.port} (status on :{args.port + 1}), '
          f'max {_max_streams} streams', flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_serve, args=(conn, addr), daemon=True,
                         name=f'Stream-{addr[0]}:{addr[1]}').start()


if __name__ == '__main__':
    main()
