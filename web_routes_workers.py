"""Routes for the offload worker pool (remote TTS + remote denoise)."""

import json as json_mod
import threading
import time
import urllib.request

from log_util import log_exc

# Heartbeat fields the gateway keeps (anything else in the body is dropped).
_META_KEYS = ('version', 'proto', 'engine', 'streams', 'max_streams', 'cpus',
              'model_loaded', 'cpu_temp_c', 'fan_rpm', 'loadavg', 'status_port',
              'requests', 'errors')


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
            meta = {k: data[k] for k in _META_KEYS if k in data}
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


# ── /denoise page ──────────────────────────────────────────────────────────

_cpu_prev = {}          # bus id -> (monotonic, dn_busy_s) for the cores-in-use readout
_cpu_lock = threading.Lock()
_wk_cache = {}          # worker url -> (monotonic, status dict or {'error'})
_WK_CACHE_S = 3.0
_WK_TIMEOUT_S = 1.0     # request thread, but bounded all the same


def _worker_detail(w):
    """The denoise worker's own /status (per-stream ms, temp, load). Cached
    3 s and capped at 1 s, so a page full of pollers can't pile requests on
    a slow worker or hold a web thread."""
    port = (w.get('meta') or {}).get('status_port')
    if not port:
        return None
    host = w['url'].split('://', 1)[1].rsplit(':', 1)[0]
    now = time.monotonic()
    hit = _wk_cache.get(w['url'])
    if hit and now - hit[0] < _WK_CACHE_S:
        return hit[1]
    try:
        with urllib.request.urlopen(f'http://{host}:{port}/status', timeout=_WK_TIMEOUT_S) as r:
            out = json_mod.loads(r.read().decode())
    except Exception as e:
        out = {'error': str(e) or type(e).__name__}
    _wk_cache[w['url']] = (now, out)
    return out


def _cores_in_use(bus_id, busy_s):
    now = time.monotonic()
    with _cpu_lock:
        prev = _cpu_prev.get(bus_id)
        _cpu_prev[bus_id] = (now, busy_s)
    if not prev or now - prev[0] < 0.5 or busy_s < prev[1]:
        return None
    return round((busy_s - prev[1]) / (now - prev[0]), 3)


def denoise_status(parent):
    """Everything the /denoise page shows, in one payload."""
    import remote_workers
    import remote_denoise
    from audio_util import DENOISE_ENGINE_IDS, DENOISE_ENGINE_LABELS

    gw = getattr(parent, 'gateway', None)
    bm = getattr(gw, 'bus_manager', None) if gw else None
    procs = getattr(bm, '_bus_processors', {}) if bm else {}
    try:
        busses, _conn, _lay = parent._load_routing_config()
    except Exception:
        log_exc()
        busses = []
    remote = {s['bus']: s for s in remote_denoise.status()}

    rows = []
    for b in busses:
        bid = b.get('id', '')
        pc = b.get('processing', {}) or {}
        p = procs.get(bid)
        engine = (getattr(p, 'dfn_engine', None) if p else None) or pc.get('dfn_engine') or 'dpdfnet'
        row = {
            'id': bid, 'name': b.get('name', bid), 'type': b.get('type', ''),
            'enabled': bool(pc.get('dfn', False)),
            'engine': engine,
            'mix': float(pc.get('dfn_mix', 0.5)),
            'bypass_db': float(pc.get('dfn_bypass_db', -60.0)),
            'live': p is not None,
        }
        if p is not None:
            row.update({
                'chunks': p.dn_chunks, 'sent': p.dn_sent, 'bypassed': p.dn_bypassed,
                'late': p.dn_late, 'discarded': p.dn_wet_discarded,
                'ms_avg': None if p.dn_ms_avg is None else round(p.dn_ms_avg, 1),
                'running': bool(getattr(p, '_dn_running', False)),
            })
            cores = _cores_in_use(bid, p.dn_busy_s)
            # Remote time is spent waiting on the network, not computing here.
            row['local_cores'] = cores if engine == 'dpdfnet' else 0.0
        rs = remote.get(f'bus_{bid}')
        if rs:
            row['remote'] = rs
        rows.append(row)

    pool = remote_workers.REGISTRY.snapshot()
    workers = []
    for w in pool.get('denoise', {}).get('workers', []):
        workers.append(dict(w, detail=_worker_detail(w)))
    local_cores = sum((r.get('local_cores') or 0.0) for r in rows)
    return {
        'busses': rows,
        'engines': [{'id': e, 'label': DENOISE_ENGINE_LABELS.get(e, e)} for e in DENOISE_ENGINE_IDS],
        'workers': workers,
        'registration': pool.get('denoise', {}).get('stats', {}),
        'local_cores': round(local_cores, 3),
        'cpus': __import__('os').cpu_count(),
    }


def handle_denoise_status(handler, parent):
    """GET /denoise/status — per-bus denoise settings + live counters + workers."""
    try:
        out = denoise_status(parent)
    except Exception as e:
        log_exc()
        out = {'error': str(e)}
    _send_json(handler, out)
