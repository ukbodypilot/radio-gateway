"""Speaker-fingerprint offload to the transcription workers, with local fallback.

The workers already receive every over's audio for ASR; /embed takes the same
16 kHz float32 bytes and runs the same features + model code, so a remote
fingerprint is the same vector a local one would be (tests pin it).

Picks the least-busy registered worker whose /status lists the model in
embed_models. Any failure (timeout, HTTP error, worker without the model)
falls back to the local model for that over and is counted; a worker that
fails is skipped for BACKOFF_SECS. Mode 'local' never offloads.
"""
import collections
import json
import threading
import time
import urllib.request

import numpy as np

try:
    from log_util import log_exc
except Exception:  # pragma: no cover
    def log_exc(*_a, **_k):
        pass

TIMEOUT = 15.0
BACKOFF_SECS = 120.0


class OffloadEmbedder:
    def __init__(self, local_factory, model_key, workers, mode='auto'):
        """workers() -> [(name, url, status_dict, inflight)] of reachable workers."""
        self._local_factory = local_factory
        self._local = None
        self.model_key = model_key
        self._workers = workers
        self.mode = mode
        self.dim = None
        self._bad = {}              # url -> time it last failed
        self._lock = threading.Lock()
        self.counters = collections.Counter()

    def _local_embedder(self):
        if self._local is None:
            self._local = self._local_factory()
            self.dim = self._local.dim
        return self._local

    def _candidates(self):
        now = time.time()
        out = []
        try:
            for name, url, st, inflight in self._workers():
                if self.model_key not in (st.get('embed_models') or []):
                    continue
                if now - self._bad.get(url, 0) < BACKOFF_SECS:
                    continue
                out.append((inflight, name, url))
        except Exception:
            log_exc()
        return [c[1:] for c in sorted(out)]

    def embed(self, audio_16k):
        if self.mode != 'local':
            for name, url in self._candidates()[:1]:
                v = self._remote(name, url, audio_16k)
                if v is not False:
                    return v
        self.counters['local'] += 1
        return self._local_embedder().embed(audio_16k)

    def _remote(self, name, url, audio):
        """The embedding (or None for too short), or False to fall back."""
        req = urllib.request.Request(
            f"{url.rstrip('/')}/embed?model={self.model_key}",
            data=np.asarray(audio, np.float32).tobytes(),
            headers={'Content-Type': 'application/octet-stream'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
            if d.get('model') != self.model_key:
                raise ValueError(f"worker answered model {d.get('model')!r}")
            self.counters[f'remote:{name}'] += 1
            self.counters['remote'] += 1
            e = d.get('embedding')
            if e is None:
                return None
            v = np.asarray(e, np.float32)
            n = float(np.linalg.norm(v))
            return v / n if n > 0 else None
        except Exception as e:
            with self._lock:
                self._bad[url] = time.time()
            self.counters['remote_failed'] += 1
            self.last_error = f'{name}: {type(e).__name__}: {e}'
            return False

    def get_status(self):
        return {'mode': self.mode, 'counters': dict(self.counters),
                'backing_off': [u for u, t in self._bad.items() if time.time() - t < BACKOFF_SECS]}


def transcriber_workers(gateway):
    """The registered transcription workers, from the live transcriber's pool."""
    def workers():
        tr = getattr(gateway, 'transcriber', None) if gateway else None
        if tr is None:
            return []
        lock = getattr(tr, '_pool_lock', None)
        if lock is None:
            return []
        with lock:
            pool = list(getattr(tr, '_pool', []))
        out = []
        for e in pool:
            if not hasattr(e, '_url'):
                continue                      # local engine
            st = e.get_status()
            if st.get('reachable'):
                out.append((e.name, e._url, st, getattr(e, '_inflight', 0)))
        return out
    return workers
