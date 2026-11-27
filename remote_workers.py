"""Registry of self-registering offload workers (TTS, denoise).

A worker on another box (the macmini) POSTs a heartbeat to
/worker/register every ~30 s: {"kind": "tts"|"denoise", "name": ..., "port": N}.
The gateway takes the worker's address from the socket peer, so nothing
about a worker is configured on the gateway and a DHCP move fixes itself
on the next heartbeat. Same pattern as the transcription pool (see
transcriber.register_worker); this one is shared by every other kind.

Module-level singleton REGISTRY so audio_util's remote denoise stream and
the TTS client reach it without a gateway reference.
"""

import threading
import time

KINDS = ('tts', 'denoise')
WORKER_TTL_SECS = 90.0


class WorkerRegistry:
    def __init__(self, ttl=WORKER_TTL_SECS, clock=time.monotonic):
        self._ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._workers = {k: {} for k in KINDS}   # kind -> url -> entry
        self.stats = {k: {'accepted': 0, 'refreshed': 0, 'rejected': 0,
                          'expired': 0} for k in KINDS}

    def register(self, kind, url, name='', meta=None):
        """Add or refresh a worker. Logs only state changes."""
        kind = str(kind or '').strip().lower()
        if kind not in KINDS:
            return {'ok': False, 'error': f"unknown worker kind '{kind}'"}
        url = str(url or '').strip()
        if not url.startswith(('http://', 'tcp://')):
            self.stats[kind]['rejected'] += 1
            return {'ok': False, 'error': f'bad worker url {url!r}'}
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            pool = self._workers[kind]
            entry = pool.get(url)
            if entry is None:
                # Same worker name at a new address (DHCP move): the old
                # address is dead, drop it now rather than at TTL.
                for _u, _e in list(pool.items()):
                    if name and _e['name'] == name:
                        del pool[_u]
                pool[url] = {'url': url, 'name': name or url, 'meta': dict(meta or {}),
                             'registered': now, 'last_seen': now}
                self.stats[kind]['accepted'] += 1
                print(f"  [Workers] {kind} worker registered: {name or url} at {url}")
            else:
                entry['last_seen'] = now
                entry['meta'] = dict(meta or {})
                if name:
                    entry['name'] = name
                self.stats[kind]['refreshed'] += 1
        return {'ok': True, 'ttl': self._ttl}

    def _expire_locked(self, now):
        for kind, pool in self._workers.items():
            for url, e in list(pool.items()):
                if now - e['last_seen'] > self._ttl:
                    del pool[url]
                    self.stats[kind]['expired'] += 1
                    print(f"  [Workers] {kind} worker expired: {e['name']} at {url} "
                          f"(no heartbeat for {self._ttl:.0f}s)")

    def pick(self, kind):
        """Freshest live worker of `kind` as a dict copy, or None."""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            pool = self._workers.get(kind) or {}
            if not pool:
                return None
            e = max(pool.values(), key=lambda x: x['last_seen'])
            return dict(e, meta=dict(e['meta']))

    def snapshot(self):
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            return {
                kind: {
                    'workers': [{'name': e['name'], 'url': e['url'],
                                 'age_s': round(now - e['last_seen'], 1),
                                 'meta': dict(e['meta'])}
                                for e in pool.values()],
                    'stats': dict(self.stats[kind]),
                }
                for kind, pool in self._workers.items()
            }


REGISTRY = WorkerRegistry()
