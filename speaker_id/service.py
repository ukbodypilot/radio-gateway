"""Speaker ID service: listens to the transcriber's utterances, never blocks it.

The transcriber hands over each finished utterance (16 kHz audio + an id) and,
later, its transcript under the same id. Both go onto ONE bounded queue
consumed by ONE worker thread, so the text for an over can never be applied
before the over itself, and a slow or failing embedding model costs dropped
clips (counted), never transcription.

Live assignment (thresholds from the 2026-11-15 bake-off, ERes2Net on 24 h of
SDR1, simulated in time order):
  score >= accept           -> that profile; the centroid learns from the over
                               only if it is >= learn_secs long
  score <  new  (and long)  -> start a new provisional profile
  otherwise                 -> 'unsure': stored, scored, but teaches nothing
accept 0.60 / new 0.45 gave 88% correct on self-ID'd overs; the nightly
re-cluster (batch 5) is expected to do better (92-94% in the simulation).
"""
import collections
import os
import subprocess
import threading
import time

import numpy as np

try:
    from log_util import log_exc
except Exception:  # pragma: no cover - only outside the gateway tree
    def log_exc(*_a, **_k):
        pass

from speaker_id.store import SpeakerStore

CLIP_BITRATE = '12k'


class SpeakerID:
    def __init__(self, config, gateway=None, store=None, embedder=None):
        self._cfg = config
        self._gateway = gateway
        self.model_key = config.SPEAKER_ID_MODEL
        self.accept = float(config.SPEAKER_ID_ACCEPT)
        self.new = float(config.SPEAKER_ID_NEW)
        self.min_secs = float(config.SPEAKER_ID_MIN_SECS)
        self.learn_secs = float(config.SPEAKER_ID_LEARN_SECS)
        self.keep_clips = bool(config.SPEAKER_ID_KEEP_CLIPS)
        _b = str(config.SPEAKER_ID_BUSES or '').strip()
        self.buses = {b.strip() for b in _b.split(',') if b.strip()}   # empty = every bus
        self._store = store
        self._embedder = embedder
        self._db_path = str(config.SPEAKER_ID_DB_PATH or '') or None
        self._clip_dir = os.path.join(os.path.dirname(self._db_path) if self._db_path else
                                      os.path.expanduser('~/.config/radio-gateway'), 'speaker_clips')
        self._q = collections.deque()
        self._q_max = 64
        self._evt = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._uid_to_row = collections.OrderedDict()     # uid -> utterances.id (bounded)
        self._profiles = []        # [id, centroid, n, identity_id]
        self._lock = threading.Lock()
        self.counters = collections.Counter()
        self._embed_ms = collections.deque(maxlen=200)
        self.last_error = ''
        self.started = 0.0

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self):
        if self._store is None:
            self._store = SpeakerStore(self._db_path)
        self._profiles = [list(p) for p in self._store.load_profiles(self.model_key)]
        self._thread = threading.Thread(target=self._run, name='SpeakerID', daemon=True)
        self._thread.start()
        self.started = time.time()
        print(f'  [SpeakerID] started: model {self.model_key}, {len(self._profiles)} voice '
              f'profiles, buses {",".join(sorted(self.buses)) or "all transcribed"}')

    def stop(self):
        self._stop.set()
        self._evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._store:
            self._store.close()

    # ── transcriber listener interface (called on transcriber threads) ──
    def on_utterance(self, item):
        """item: the transcriber's pending dict (+ 'uid', 'audio_boost'). Must not block."""
        if self.buses and item.get('source_id') not in self.buses:
            self.counters['skipped_bus'] += 1
            return
        if item.get('duration', 0) < self.min_secs:
            self.counters['too_short'] += 1
            return
        self._put(('utt', item))

    def on_result(self, uid, result):
        """The transcript for a previously handed-over utterance."""
        if uid is None:
            return
        self._put(('text', uid, result))

    def _put(self, job):
        with self._lock:
            if len(self._q) >= self._q_max:
                self.counters['dropped'] += 1
                self._metric('dropped')
                return
            self._q.append(job)
            self.counters['queued'] += 1
        self._evt.set()

    # ── worker ──────────────────────────────────────────────────────────
    def _run(self):
        try:
            os.nice(5)          # below ASR and well below the RT bus tick
        except Exception:
            log_exc()
        while not self._stop.is_set():
            self._evt.wait(1.0)
            self._evt.clear()
            while not self._stop.is_set():
                with self._lock:
                    job = self._q.popleft() if self._q else None
                if job is None:
                    break
                try:
                    if job[0] == 'utt':
                        self._handle_utterance(job[1])
                    else:
                        self._handle_text(job[1], job[2])
                except Exception as e:
                    self.counters['errors'] += 1
                    self.last_error = f'{type(e).__name__}: {e}'
                    self._metric('errors')
                    log_exc()
                finally:
                    self.counters['processed'] += 1

    def _ensure_embedder(self):
        if self._embedder is None:
            from speaker_id.embedder import Embedder
            self._embedder = Embedder(self.model_key)
        return self._embedder

    @staticmethod
    def raw_audio(item):
        """Undo the transcriber's tanh audio boost (exact below saturation).

        Boost 2x tops out at tanh(2) ~ 0.96, far from 1.0, so arctanh recovers
        the bus audio. The bake-off measured the boosted copy at 7.7% EER vs
        6.3% raw.
        """
        a = np.asarray(item['audio_16k'], np.float32)
        boost = float(item.get('audio_boost') or 1.0)
        if boost == 1.0:
            return a
        return (np.arctanh(np.clip(a, -0.999999, 0.999999)) / boost).astype(np.float32)

    def _handle_utterance(self, item):
        audio = self.raw_audio(item)
        t0 = time.perf_counter()
        emb = self._ensure_embedder().embed(audio)
        self._embed_ms.append((time.perf_counter() - t0) * 1000)
        if emb is None:
            self.counters['too_short'] += 1
            return
        self.counters['embedded'] += 1
        self._metric('embedded')
        ts = float(item.get('start_time') or time.time())
        dur = float(item.get('duration') or len(audio) / 16000)
        pid, score, decision = self.assign(emb, dur, ts)
        self.counters[decision] += 1
        self._metric(decision)
        row = self._store.add_utterance(
            ts=ts, bus=item.get('source_id', ''), source=item.get('upstream_source') or '',
            freq='', duration=round(dur, 2), model=self.model_key, emb=emb,
            profile_id=pid, score=None if score is None else round(float(score), 4),
            decision=decision)
        if self.keep_clips:
            clip = self._save_clip(row, ts, audio)
            if clip:
                self._store.set_clip(row, clip)
        uid = item.get('uid')
        if uid is not None:
            self._uid_to_row[uid] = row
            while len(self._uid_to_row) > 500:
                self._uid_to_row.popitem(last=False)

    def assign(self, emb, dur, ts):
        """Online assignment. Returns (profile_id or None, best score or None, decision)."""
        best_i, best = -1, None
        if self._profiles:
            C = np.stack([p[1] for p in self._profiles])
            sc = C @ emb
            best_i = int(sc.argmax())
            best = float(sc[best_i])
        if best is not None and best >= self.accept:
            p = self._profiles[best_i]
            if dur >= self.learn_secs:
                v = p[1] * p[2] + emb
                p[1] = (v / np.linalg.norm(v)).astype(np.float32)
                p[2] += 1
                self._store.update_profile(p[0], p[1], p[2], ts)
                return p[0], best, 'matched_learn'
            self._store.touch_profile(p[0], ts)
            return p[0], best, 'matched'
        if (best is None or best < self.new) and dur >= self.learn_secs:
            pid = self._store.add_profile(self.model_key, emb, ts)
            self._profiles.append([pid, emb.astype(np.float32), 1, None])
            return pid, best, 'new_profile'
        return None, best, 'unsure'

    def _handle_text(self, uid, result):
        row = self._uid_to_row.get(uid)
        if row is None:
            self.counters['text_orphan'] += 1      # over was too short, dropped or skipped
            return
        self._store.set_text(row, result.get('text', ''), tx_id=result.get('tx_id'),
                             freq=result.get('freq') or None, source=result.get('source') or None)
        self.counters['texted'] += 1

    def _save_clip(self, row, ts, audio):
        """Opus at 12 kb/s (~1.5 KB/s of speech). Bounded: 10 s timeout."""
        try:
            day = time.strftime('%Y%m%d', time.localtime(ts))
            d = os.path.join(self._clip_dir, day)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f'{row}.opus')
            pcm = (np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes()
            r = subprocess.run(
                ['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', '-',
                 '-c:a', 'libopus', '-b:a', CLIP_BITRATE, '-application', 'voip', '-y', path],
                input=pcm, capture_output=True, timeout=10)
            if r.returncode != 0:
                self.counters['clip_errors'] += 1
                self.last_error = r.stderr.decode(errors='replace')[-200:]
                return ''
            self.counters['clips'] += 1
            return os.path.relpath(path, self._clip_dir)
        except Exception:
            self.counters['clip_errors'] += 1
            log_exc()
            return ''

    # ── status / metrics ────────────────────────────────────────────────
    def _metric(self, event):
        try:
            import metrics as _m
            _m.speaker_id_events_total.labels(event=event).inc()
        except Exception:
            log_exc()

    def get_status(self):
        ms = list(self._embed_ms)
        with self._lock:
            qlen = len(self._q)
        try:
            counts = self._store.counts() if self._store else {}
        except Exception:
            log_exc()
            counts = {}
        return {
            'enabled': True,
            'model': self.model_key,
            'buses': sorted(self.buses),
            'thresholds': {'accept': self.accept, 'new': self.new,
                           'min_secs': self.min_secs, 'learn_secs': self.learn_secs},
            'queue': qlen,
            'profiles_loaded': len(self._profiles),
            'counters': dict(self.counters),
            'embed_ms_avg': round(sum(ms) / len(ms), 1) if ms else None,
            'db': counts,
            'last_error': self.last_error,
            'running': bool(self._thread and self._thread.is_alive()),
        }

    def chips_for(self, results):
        """Annotate transcription result dicts with the speaker verdict, for the UI."""
        if not self._store:
            return results
        try:
            m = self._store.by_tx_ids([r.get('tx_id') for r in results])
            labels = self._store.profile_labels()
        except Exception:
            log_exc()
            return results
        for r in results:
            v = m.get(r.get('tx_id'))
            if not v:
                continue
            pid = v['profile_id']
            call, name = labels.get(pid, ('', ''))
            r['speaker'] = {'profile_id': pid, 'score': v['score'], 'decision': v['decision'],
                            'callsign': call, 'name': name}
        return results
