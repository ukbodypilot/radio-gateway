"""Routes for the offload worker pool (remote TTS + remote denoise)."""

import json as json_mod

from log_util import log_exc


def _send_json(handler, obj, code=200):
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(obj).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_worker_register(handler, parent):
    """POST /worker/register  {"kind": "tts"|"denoise", "name": ..., "port": N, ...}

    The address comes from the socket peer, never from the body, so a
    worker can't misreport its own interface. The URL scheme follows the
    kind: TTS is HTTP, denoise is a raw TCP stream.
    """
    import remote_workers
    result = {'ok': False}
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json_mod.loads(handler.rfile.read(length).decode('utf-8') if length else '{}')
        kind = str(data.get('kind', '')).strip().lower()
        port = int(data.get('port', 0) or 0)
        if not 0 < port < 65536:
            result = {'ok': False, 'error': 'port required'}
        else:
            peer = handler.client_address[0]
            if ':' in peer:
                peer = f'[{peer}]'
            scheme = 'tcp' if kind == 'denoise' else 'http'
            url = f'{scheme}://{peer}:{port}'
            meta = {k: data[k] for k in ('version', 'proto', 'engine', 'streams',
                                         'cpus', 'model_loaded', 'cpu_temp_c')
                    if k in data}
            result = remote_workers.REGISTRY.register(
                kind, url, str(data.get('name', '') or '').strip(), meta)
            result['url'] = url
    except Exception as e:
        log_exc()
        result = {'ok': False, 'error': str(e)}
    _send_json(handler, result)


def handle_workers_status(handler, parent):
    """GET /workers — pool, per-stream remote denoise counters, TTS counters."""
    import remote_workers
    out = {'pool': remote_workers.REGISTRY.snapshot()}
    try:
        import remote_denoise
        out['denoise'] = remote_denoise.status()
    except Exception:
        log_exc()
    try:
        import tts_offload
        out['tts'] = tts_offload.status()
    except Exception:
        log_exc()
    _send_json(handler, out)
