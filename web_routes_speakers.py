"""Speaker ID web API: voices, review queue, clips, and human curation.

GET  /speakers/api/profiles          every voice, busiest first, with its verdict
GET  /speakers/api/profile?id=N      one voice: verdict + why, licence, recent overs
GET  /speakers/api/review            the review queue (speaker_id.review)
GET  /speakers/api/audit             recent changes (undo targets)
GET  /speakers/clip?id=N             one over's Opus clip
POST /speakers/api/action            {op: label|unlabel|merge|move_over|not_same|note|undo, ...}

Behind the same auth as every page. Embeddings never leave the box: no
response carries one.
"""
import json as json_mod
import os
import urllib.parse as _up

try:
    from log_util import log_exc
except Exception:  # pragma: no cover
    def log_exc(*_a, **_k):
        pass


def _send(handler, code, obj):
    body = json_mod.dumps(obj).encode('utf-8')
    try:
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except BrokenPipeError:
        pass


def _sid(parent):
    gw = parent.gateway if parent else None
    return getattr(gw, 'speaker_id', None) if gw else None


def _qs(handler):
    return _up.parse_qs(_up.urlparse(handler.path).query)


def _int(params, key):
    try:
        return int(params.get(key, [''])[0])
    except ValueError:
        return None


def handle_speakers_get(handler, parent):
    path = handler.path.split('?', 1)[0]
    sid = _sid(parent)
    if sid is None or sid.store is None:
        _send(handler, 503, {'ok': False, 'error': 'speaker ID is off (ENABLE_SPEAKER_ID)'})
        return
    try:
        if path == '/speakers/api/profiles':
            _send(handler, 200, {'ok': True, 'profiles': _profiles(sid), 'status': sid.get_status()})
        elif path == '/speakers/api/profile':
            pid = _int(_qs(handler), 'id')
            d = _profile(sid, pid) if pid else None
            _send(handler, 200 if d else 404, {'ok': bool(d), **(d or {'error': 'no such voice'})})
        elif path == '/speakers/api/review':
            from speaker_id import review
            items = review.build(sid.store, sid.identifier, sid.model_key, same_hi=sid.merge_at)
            _send(handler, 200, {'ok': True, 'items': items})
        elif path == '/speakers/api/audit':
            _send(handler, 200, {'ok': True, 'rows': sid.store.audit_rows(100)})
        elif path == '/speakers/clip':
            _clip(handler, sid, _int(_qs(handler), 'id'))
        else:
            _send(handler, 404, {'ok': False, 'error': 'not found'})
    except Exception as e:
        log_exc()
        _send(handler, 500, {'ok': False, 'error': f'{type(e).__name__}: {e}'})


def _verdict(sid, pid):
    if sid.identifier is None:
        return None
    try:
        return sid.identifier.verdict(pid)
    except Exception:
        log_exc()
        return None


def _profiles(sid):
    out = []
    for p in sid.store.list_profiles():
        v = _verdict(sid, p['id'])
        p['candidate'] = v['candidates'][0] if v and v['candidates'] else None
        out.append(p)
    return out


def _profile(sid, pid):
    p = sid.store.profile(pid)
    if not p:
        return None
    ident = sid.store.identity(p['identity_id']) if p['identity_id'] else None
    licence = None
    uls = getattr(sid, '_uls', None)
    if ident and ident.get('callsign') and uls is not None and uls.ready():
        r = uls.get(ident['callsign'])
        if r:
            licence = dict(zip(('status', 'name', 'city', 'state', 'class'), r))
    brief = next((x for x in sid.store.list_profiles() if x['id'] == pid), {})
    return {'profile': {**p, **{k: brief.get(k) for k in ('overs', 'first', 'last', 'seconds',
                                                          'buses', 'freqs')}},
            'identity': ident, 'licence': licence, 'verdict': _verdict(sid, pid),
            'overs': sid.store.profile_overs(pid, 60),
            'samples': [o['id'] for o in sid.store.profile_overs(pid, 5, best=True)]}


def _clip(handler, sid, utt_id):
    u = sid.store.utterance(utt_id) if utt_id else None
    root = os.path.realpath(sid._clip_dir)
    path = os.path.realpath(os.path.join(root, u['clip'])) if u and u['clip'] else ''
    if not path or not path.startswith(root + os.sep) or not os.path.isfile(path):
        _send(handler, 404, {'ok': False, 'error': 'no clip for that over'})
        return
    size = os.path.getsize(path)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'audio/ogg')
        handler.send_header('Content-Length', str(size))
        handler.send_header('Cache-Control', 'private, max-age=3600')
        handler.end_headers()
        with open(path, 'rb') as f:
            handler.wfile.write(f.read())
    except BrokenPipeError:
        pass


_OPS = {
    'label': lambda c, b: c.label(b['profile_id'], b.get('callsign', ''), b.get('name', ''), b.get('notes')),
    'unlabel': lambda c, b: c.unlabel(b['profile_id']),
    'merge': lambda c, b: c.merge(b['keep'], b['drop']),
    'move_over': lambda c, b: c.move_over(b['utt_id'], b.get('to_profile')),
    'not_same': lambda c, b: c.not_same(b['a'], b['b']),
    'note': lambda c, b: c.note(b['profile_id'], b.get('notes', '')),
    'undo': lambda c, b: c.undo(b['audit_id']),
}


def handle_speakers_post(handler, parent):
    """POST /speakers/api/action"""
    sid = _sid(parent)
    if sid is None or sid.store is None:
        _send(handler, 503, {'ok': False, 'error': 'speaker ID is off (ENABLE_SPEAKER_ID)'})
        return
    if handler.path.split('?', 1)[0] != '/speakers/api/action':
        _send(handler, 404, {'ok': False, 'error': 'not found'})
        return
    try:
        length = min(int(handler.headers.get('Content-Length', 0)), 64 * 1024)
        body = json_mod.loads(handler.rfile.read(length) or b'{}')
        if body.get('op') == 'nightly':
            result = sid.run_on_worker(lambda s: s.nightly(), timeout=600)
            _send(handler, 200, {'ok': True, 'result': result, 'audit_id': sid.store.last_audit_id()})
            return
        op = _OPS.get(body.get('op'))
        if op is None:
            _send(handler, 400, {'ok': False, 'error': f"unknown op {body.get('op')!r}"})
            return
        result = sid.run_on_worker(lambda s: op(s.curator(), body))
        _send(handler, 200, {'ok': True, 'result': result, 'audit_id': sid.store.last_audit_id()})
    except (KeyError, ValueError, TypeError) as e:
        _send(handler, 400, {'ok': False, 'error': str(e) or type(e).__name__})
    except TimeoutError as e:
        _send(handler, 503, {'ok': False, 'error': str(e)})
    except Exception as e:
        log_exc()
        _send(handler, 500, {'ok': False, 'error': f'{type(e).__name__}: {e}'})
