"""POST handlers for transcription: VAD/model settings, query, model swap."""

"""POST route handlers extracted from web_server.py."""

import json as json_mod
import os
import time
import subprocess
import threading as _thr

from audio_sources import generate_cw_pcm
from cat_client import RadioCATClient


def handle_transcription_query(handler, parent):
    """POST /transcription/query  body: {"question": "..."}"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'unknown error'}
    try:
        data = json_mod.loads(body)
        question = str(data.get('question', '')).strip()
        if not question:
            result = {'ok': False, 'error': 'No question provided.'}
        else:
            tl = getattr(parent.gateway, 'transcription_log', None) if parent.gateway else None
            if not tl:
                result = {'ok': False, 'error': 'Transcription log not available.'}
            else:
                r = tl.query(question)
                if 'answer' in r:
                    result = {'ok': True, 'answer': r['answer']}
                else:
                    result = {'ok': False, 'error': r.get('error', 'Query failed.')}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass

def handle_transcribe_worker_register(handler, parent):
    """POST /transcribe_worker/register

    Body: {"name": "macmini", "port": 9800, "url": "http://host:9800",
           "model": "whisper/medium.en", ...}

    `url` is optional — when absent it is derived from the connecting
    socket's address plus `port`, so a worker never has to know or
    configure its own IP. This is the transcription-side equivalent of a
    link endpoint's REGISTER on :9700: the worker dials in, the gateway
    learns where it lives, and a DHCP move fixes itself on the next
    heartbeat instead of breaking the pool until someone edits config.
    """
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8') if length else '{}'
    result = {'ok': False}
    try:
        data = json_mod.loads(body or '{}')
        tx = parent.gateway.transcriber if parent.gateway else None
        if not tx:
            result = {'ok': False, 'error': 'transcriber not running'}
        else:
            _url = str(data.get('url', '') or '').strip()
            if not _url:
                # client_address is the socket peer — the authoritative
                # answer to "where did this worker come from", and immune to
                # the worker guessing the wrong interface.
                _peer = handler.client_address[0]
                _port = int(data.get('port', 9800) or 9800)
                # IPv6 literals need brackets in a URL.
                if ':' in _peer:
                    _peer = f'[{_peer}]'
                _url = f'http://{_peer}:{_port}'
            _name = str(data.get('name', '') or '').strip()
            _meta = {k: data[k] for k in ('model', 'engine', 'version', 'model_loaded')
                     if k in data}
            result = tx.register_worker(_url, _name, _meta)
            result['url'] = _url
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_transcribe_config(handler, parent):
    """POST /transcribe_config"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        key = data.get('key', '')
        value = data.get('value', '')
        tx = parent.gateway.transcriber if parent.gateway else None
        if not tx:
            result = {'ok': False, 'error': 'transcriber not running'}
        elif key == 'enabled':
            tx._enabled = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'vad_threshold':
            tx._vad_threshold = float(value)
            if hasattr(tx, '_silence_threshold'):
                tx._silence_threshold = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'vad_hold':
            tx._vad_hold_time = float(value)
            if hasattr(tx, '_silence_duration'):
                tx._silence_duration = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'min_duration':
            tx._min_duration = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'forward_mumble':
            tx._forward_mumble = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'audio_boost':
            tx._audio_boost = float(value) / 100.0
            tx._save(); result = {'ok': True}
        elif key in ('denoise', 'denoise_mix', 'denoise_engine'):
            # Denoise moved to per-bus "D" filter. Keep the endpoint
            # friendly with a short-lived no-op so old UI / saved settings
            # don't error out, but steer users to the routing page.
            result = {'ok': True, 'note': 'denoise is now a per-bus setting — use the routing page'}
        elif key == 'log_results':
            tx._log_results = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'alert_keywords':
            tx._alert_keywords = str(value)
            tx._save(); result = {'ok': True}
        elif key == 'clear':
            with tx._results_lock:
                tx._results.clear()
            result = {'ok': True}
        elif key == 'model':
            _v = str(value)
            # Accept bare legacy keys and normalise them
            if _v in ('tiny', 'base'):
                _v = f'moonshine/{_v}'
            from transcriber import _VALID_MODELS
            if _v not in _VALID_MODELS:
                result = {'ok': False, 'error': f'unknown model: {_v}'}
            else:
                _parts = _v.split('/', 1)
                tx._model_key = _v
                tx._engine = _parts[0]
                tx._model_size = _parts[1]
                tx._save()
                result = {'ok': True, 'note': 'model change takes effect on restart'}
        elif key == 'split_threshold_secs':
            try:
                _v = float(value)
                if _v < 0 or _v > 60:
                    result = {'ok': False, 'error': 'must be 0-60 seconds'}
                else:
                    tx._split_threshold = _v
                    tx._save()
                    result = {'ok': True}
            except (TypeError, ValueError) as e:
                result = {'ok': False, 'error': str(e)}
        elif key == 'mode':
            _v = str(value).lower()
            if _v not in ('off', 'local', 'remote', 'pool'):
                result = {'ok': False, 'error': f'unknown mode: {_v}'}
            else:
                tx._mode = _v
                tx._save()
                result = {'ok': True, 'note': 'restart required'}
        elif key == 'remote_model':
            from transcribe_engine import RemoteEngine
            _remotes = [e for e in tx._pool if isinstance(e, RemoteEngine)]
            if not _remotes:
                result = {'ok': False, 'error': 'no remote engines in pool'}
            else:
                _v = str(value)
                from transcriber import _VALID_MODELS
                if _v not in _VALID_MODELS:
                    result = {'ok': False, 'error': f'unknown model: {_v}'}
                else:
                    import urllib.request as _ur
                    _body = json_mod.dumps({'model': _v}).encode()
                    for _re in _remotes:
                        try:
                            _req = _ur.Request(
                                f'{_re._url}/model', data=_body,
                                headers={'Content-Type': 'application/json'}, method='POST')
                            _ur.urlopen(_req, timeout=10)
                        except Exception:
                            pass
                    with tx._stats_lock:
                        tx._stats.clear()
                    result = {'ok': True}
        elif key == 'restart':
            gw = parent.gateway
            if gw:
                # The transcriber is constructed before BusManager.start(), so
                # its object graph (Silero VAD + Moonshine ONNX) is inside the
                # permanent generation. Frozen objects are never scanned for
                # cycles again, so swapping models without unfreezing would
                # leak the outgoing session's cyclic garbage for the life of
                # the process. Unfreeze, swap, then re-freeze the new steady
                # state — the collect below is what actually reclaims the old
                # model. This runs on an operator action, never on a hot path,
                # so the ~65 ms full collection is free here.
                import gc as _gc
                _gc.unfreeze()
                if gw.transcriber:
                    gw.transcriber.stop()
                try:
                    from transcriber import RadioTranscriber
                    gw.transcriber = RadioTranscriber(gw.config, gw)
                    gw.transcriber.start()
                    result = {'ok': True}
                except Exception as _re:
                    result = {'ok': False, 'error': str(_re)}
                finally:
                    # Re-freeze even if the swap failed — leaving the heap
                    # unfrozen would put every long-lived startup object back
                    # into every gen-2 sweep, which is the exact 65 ms tick
                    # overrun this ordering exists to avoid.
                    _gc.collect()
                    _gc.freeze()
            else:
                result = {'ok': False, 'error': 'gateway not ready'}
        else:
            result = {'ok': False, 'error': f'unknown key: {key}'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    return
