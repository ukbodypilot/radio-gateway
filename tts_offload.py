"""Kokoro TTS that runs on a remote worker when one is registered.

OffloadKokoro has the one method the gateway calls on a Kokoro instance,
create(text, voice=, speed=, lang=) -> (float32 samples, sample_rate), so
text_commands.speak_text and the announcer use it unchanged.

Order: the freshest registered 'tts' worker (tools/tts_worker.py on the
macmini) -> the local Kokoro, loaded lazily on the first fallback so the
gateway doesn't hold ~300 MB of model it rarely uses. TTS has no hard
deadline, so a remote failure costs the local synthesis time and nothing
else.
"""

import json
import threading
import time
import urllib.request

import numpy as np

from log_util import log_exc

_stats = {'remote': 0, 'fallback': 0, 'local': 0, 'last_error': '',
          'last_remote_ms': None, 'last_worker': ''}
_stats_lock = threading.Lock()


def status():
    with _stats_lock:
        return dict(_stats)


def _count(result, **extra):
    with _stats_lock:
        _stats[result] += 1
        _stats.update(extra)
    try:
        import metrics as _m
        _m.tts_remote_total.labels(result=result).inc()
    except Exception:
        log_exc()


def _remote_timeout(text):
    # Kokoro on the mini runs ~3-5x realtime; ~15 chars per spoken second.
    # Generous: a slow answer still beats a local one on the Dell's fan.
    return 5.0 + len(text) / 15.0


def remote_create(url, text, voice, speed, lang, timeout):
    body = json.dumps({'text': text, 'voice': voice, 'speed': speed,
                       'lang': lang}).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + '/tts', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rate = int(r.headers.get('X-Sample-Rate', '0'))
        data = r.read()
    if rate <= 0 or len(data) % 4:
        raise RuntimeError(f'bad reply (rate={rate}, {len(data)} bytes)')
    return np.frombuffer(data, dtype='<f4').astype(np.float32), rate


class OffloadKokoro:
    def __init__(self, local_loader, registry=None, enabled=True):
        """local_loader() -> a kokoro_onnx.Kokoro (raises on failure)."""
        self._loader = local_loader
        self._local = None
        self._local_lock = threading.Lock()
        self.enabled = enabled
        if registry is None:
            import remote_workers
            registry = remote_workers.REGISTRY
        self._registry = registry

    def local(self):
        with self._local_lock:
            if self._local is None:
                self._local = self._loader()
            return self._local

    def create(self, text, voice='af_heart', speed=1.0, lang='en-us'):
        w = self._registry.pick('tts') if self.enabled else None
        if w is not None and w['meta'].get('model_loaded') is False:
            w = None   # registered but still loading its model
        if w is not None:
            t0 = time.monotonic()
            try:
                out = remote_create(w['url'], text, voice, speed, lang,
                                    _remote_timeout(text))
                _count('remote', last_remote_ms=round((time.monotonic() - t0) * 1000),
                       last_worker=w['name'])
                return out
            except Exception as e:
                print(f"  [TTS] remote worker {w['name']} failed ({e}) — using local Kokoro")
                _count('fallback', last_error=f'{w["name"]}: {e}')
        else:
            _count('local')
        return self.local().create(text, voice=voice, speed=speed, lang=lang)
