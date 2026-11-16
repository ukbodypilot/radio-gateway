"""Speaker ID service: listens to the transcriber's utterances, never blocks it.

The transcriber hands over each finished utterance (16 kHz audio + an id) and,
later, its transcript under the same id. Both go onto ONE bounded queue
consumed by ONE worker thread, so the text for an over can never be applied
before the over itself, and a slow or failing embedding model costs dropped
clips (counted), never transcription.

Live assignment (thresholds from the 2026-11-15 bake-off, ERes2Net on 24 h of
SDR1, simulated in time order):
  score >= accept           -> that profile; the centroid learns from the over
                               only if it is >= learn_secs long AND
                               score >= learn_at (no drift from borderline matches)
  score <  new  (and long)  -> start a new provisional profile
  otherwise                 -> 'unsure': stored, scored, but teaches nothing
accept 0.60 / learn 0.60 gave 88% right with 2 voices mixing two people;
accept 0.65 / learn 0.70 gave 100% right and no mixed voices, placing 24 of
40 self-ID'd overs live (33 before); the nightly pass adopts more at 0.70.
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
    def __init__(self, config, gateway=None, store=None, embedder=None, uls=None):
        self._cfg = config
        self._gateway = gateway
        self.model_key = config.SPEAKER_ID_MODEL
        self.accept = float(config.SPEAKER_ID_ACCEPT)
        self.learn_at = max(self.accept, float(config.SPEAKER_ID_LEARN_AT))
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
        self.callsigns = bool(config.SPEAKER_ID_CALLSIGNS)
        self.offload = str(config.SPEAKER_ID_OFFLOAD or 'auto').strip().lower()
        self.alert_for = {x.strip().upper() for x in str(config.SPEAKER_ID_ALERT or '').split(',') if x.strip()}
        from speaker_id import split as _split
        self._split_mod = _split
        self.split = bool(config.SPEAKER_ID_SPLIT)
        self.split_at = float(config.SPEAKER_ID_SPLIT_AT)
        self._alert_last = {}
        self._uls = uls
        self._ident = None
        self._seeded = False
        self._backfill_ceiling = 0
        self._backfill_done = False
        self.nightly_time = str(config.SPEAKER_ID_NIGHTLY_TIME or '').strip()
        self.clip_days = float(config.SPEAKER_ID_CLIP_DAYS)
        self.merge_at = float(config.SPEAKER_ID_MERGE_AT)
        self.target_model = self.model_key        # config; model_key = what the DB is in
        self._target_embedder = None
        self.last_nightly = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self):
        if self._store is None:
            self._store = SpeakerStore(self._db_path)
        # The database records which model its fingerprints are in. A changed
        # SPEAKER_ID_MODEL keeps that model live until every kept clip has been
        # re-fingerprinted with the new one (idle time), then switches at once.
        db_model = self._store.meta_get('embed_model')
        if db_model is None:
            self._store.meta_set('embed_model', self.model_key)
        elif db_model != self.target_model:
            self.model_key = db_model
            print(f'  [SpeakerID] model change {db_model} -> {self.target_model}: '
                  f'{db_model} stays live while kept clips are re-fingerprinted')
        self._profiles = [list(p) for p in self._store.load_profiles(self.model_key)]
        if self.callsigns:
            from speaker_id.identify import Identifier
            if self._uls is None:
                from speaker_id.uls import UlsLookup
                self._uls = UlsLookup()
            self._ident = Identifier(self._store, self._uls)
            # Overs stored before this start are re-filed by the idle backfill;
            # newer ones are filed live as their text arrives.
            self._backfill_ceiling = self._store.max_utterance_id()
        self._thread = threading.Thread(target=self._run, name='SpeakerID', daemon=True)
        self._thread.start()
        self.started = time.time()
        print(f'  [SpeakerID] started: model {self.model_key}, {len(self._profiles)} voice '
              f'profiles, buses {",".join(sorted(self.buses)) or "all transcribed"}')

    def stop(self):
        self._stop.set()
        self._evt.set()
        if self._uls is not None:
            self._uls.stop()
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
            try:
                self._idle_work()
            except Exception as e:          # idle work must never take the worker down
                self.counters['errors'] += 1
                self.last_error = f'idle: {type(e).__name__}: {e}'
                log_exc()
            while not self._stop.is_set():
                with self._lock:
                    job = self._q.popleft() if self._q else None
                if job is None:
                    break
                if job[0] == 'call':
                    self._handle_call(job)
                    continue
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

    # ── curation entry point (web / MCP threads) ────────────────────────
    def run_on_worker(self, fn, timeout=20.0):
        """Run fn(self) on the worker thread and return its result (or raise its error).

        Curation changes profiles the worker is assigning against; running
        them here serialises the two. Never dropped by the queue cap.
        """
        if not (self._thread and self._thread.is_alive()):
            raise RuntimeError('speaker ID is not running')
        box, done = {}, threading.Event()
        with self._lock:
            # Front of the queue: a click waits for at most the over in hand,
            # not for every over queued behind it (2026-11-15: a confirm sat
            # behind 7 overs, got clicked 4 times and applied 4 times).
            self._q.appendleft(('call', fn, box, done))
        self._evt.set()
        if not done.wait(timeout):
            raise TimeoutError('speaker ID worker is busy; try again')
        if 'error' in box:
            raise box['error']
        return box.get('result')

    def _handle_call(self, job):
        _, fn, box, done = job
        try:
            box['result'] = fn(self)
            self.reload_profiles()
            self.counters['curation'] += 1
        except Exception as e:            # returned to the caller, not swallowed
            box['error'] = e
        finally:
            done.set()

    def reload_profiles(self):
        self._profiles = [list(p) for p in self._store.load_profiles(self.model_key)]

    def curator(self):
        from speaker_id.curation import Curator
        return Curator(self._store, self._uls, self.learn_secs)

    @property
    def store(self):
        return self._store

    @property
    def identifier(self):
        return self._ident

    def _idle_work(self):
        """Seed the regulars, then re-file history, a slice at a time, only when idle."""
        if not self._ident or not self._uls.ready():
            self._idle_nightly_and_migration()
            return
        with self._lock:
            if self._q:
                return
        try:
            if not self._seeded:
                tl = getattr(self._gateway, 'transcription_log', None) if self._gateway else None
                n = self._ident.regulars.seed_from_log(tl, self._uls.valid) if tl else 0
                self._seeded = True
                print(f'  [SpeakerID] regulars seeded from the transcription log: {n} callsign '
                      f'mentions, {len(self._ident.regulars.mentions)} calls')
                return
            if not self._backfill_done:
                n = self._ident.backfill(budget_secs=0.5, ceiling=self._backfill_ceiling)
                self.counters['backfilled'] += n
                if n == 0:
                    self._backfill_done = True
                return
        except Exception as e:
            self.counters['errors'] += 1
            self.last_error = f'{type(e).__name__}: {e}'
            log_exc()
        self._idle_nightly_and_migration()

    def _gauges(self):
        """Voice counts for Grafana, at most once a minute (idle only)."""
        now = time.monotonic()
        if now - getattr(self, '_gauges_at', 0) < 60:
            return
        self._gauges_at = now
        try:
            import metrics as _m
            ps = self._store.list_profiles()
            _m.speaker_id_voices.labels(kind='voices').set(len(ps))
            _m.speaker_id_voices.labels(kind='named').set(sum(1 for p in ps if p['identity_id']))
            _m.speaker_id_voices.labels(kind='human').set(
                sum(1 for p in ps if p['identity_id'] and (p['label_source'] or '').startswith('human')))
        except Exception:
            log_exc()

    def _idle_nightly_and_migration(self):
        with self._lock:
            if self._q:
                return
        self._gauges()
        try:
            if self.target_model != self.model_key:
                self._migrate_slice()
            elif self._nightly_due():
                self.nightly()
        except Exception as e:
            self.counters['errors'] += 1
            self.last_error = f'{type(e).__name__}: {e}'
            log_exc()

    # ── nightly: re-cluster + clip retention ────────────────────────────
    def _nightly_due(self):
        if not self.nightly_time:
            return False
        try:
            hh, mm = (int(x) for x in self.nightly_time.split(':'))
        except ValueError:
            return False
        now = time.localtime()
        if (now.tm_hour, now.tm_min) < (hh, mm):
            return False
        return self._store.meta_get('last_nightly_day') != time.strftime('%Y-%m-%d', now)

    def nightly(self):
        """Re-cluster then prune clips. Runs on the worker thread (idle, or via run_on_worker)."""
        from speaker_id import recluster, retention
        self._store.meta_set('last_nightly_day', time.strftime('%Y-%m-%d'))
        rep = recluster.run(self._store, self.model_key, accept=max(self.accept, recluster.REASSIGN_AT),
                            merge_at=self.merge_at, learn_at=self.learn_at,
                            learn_secs=self.learn_secs, min_secs=self.min_secs, identifier=self._ident)
        if self.keep_clips and self.clip_days > 0:
            rep.update(retention.prune(self._store, self._clip_dir, self.clip_days))
        self.reload_profiles()
        self.last_nightly = {'at': time.time(), **rep}
        self.counters['nightly'] += 1
        print(f'  [SpeakerID] nightly: {rep}')
        return rep

    # ── model swap ──────────────────────────────────────────────────────
    def _migrate_slice(self, n=8):
        from speaker_id.embedder import Embedder
        if self._target_embedder is None:
            self._target_embedder = Embedder(self.target_model)
        todo = self._store.reembed_todo(self.target_model, n)
        for uid, clip in todo:
            audio = self._load_clip(clip)
            e = self._target_embedder.embed(audio) if audio is not None else None
            if e is not None:
                self._store.reembed_put(uid, self.target_model, e)
                self.counters['reembedded'] += 1
            else:
                self._store.reembed_put(uid, self.target_model, np.zeros(self._target_embedder.dim, np.float32))
                self.counters['reembed_failed'] += 1
        if todo:
            return
        old = self.model_key
        n_utt, n_prof = self._store.reembed_finish(self.target_model)
        self._store.meta_set('embed_model', self.target_model)
        self._store.audit('auto', 'model_switch', frm=old, to=self.target_model, overs=n_utt, voices=n_prof)
        self.model_key = self.target_model
        self._embedder, self._target_embedder = self._target_embedder, None
        self.reload_profiles()
        print(f'  [SpeakerID] switched {old} -> {self.model_key}: {n_utt} overs, {n_prof} voices carried over')

    def _load_clip(self, clip):
        path = os.path.join(self._clip_dir, clip)
        try:
            r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', '16000',
                                '-f', 'f32le', '-'], capture_output=True, timeout=20)
            if r.returncode != 0 or not r.stdout:
                return None
            return np.frombuffer(r.stdout, np.float32).copy()
        except Exception:
            log_exc()
            return None

    def _ensure_embedder(self):
        if self._embedder is None:
            from speaker_id.embedder import Embedder
            from speaker_id.remote import OffloadEmbedder, transcriber_workers
            key = self.model_key
            self._embedder = OffloadEmbedder(lambda: Embedder(key), key,
                                             transcriber_workers(self._gateway), self.offload)
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
        ts = float(item.get('start_time') or time.time())
        dur = float(item.get('duration') or len(audio) / 16000)
        cuts = self._find_cuts(audio)
        bounds = [0.0] + cuts + [len(audio) / 16000]
        parts = []
        for k in range(len(bounds) - 1):
            a0, a1 = bounds[k], bounds[k + 1]
            seg = audio[int(a0 * 16000):int(a1 * 16000)]
            t0 = time.perf_counter()
            emb = self._ensure_embedder().embed(seg)
            self._embed_ms.append((time.perf_counter() - t0) * 1000)
            if emb is None:
                self.counters['too_short'] += 1
                continue
            self.counters['embedded'] += 1
            self._metric('embedded')
            pdur = a1 - a0 if cuts else dur
            pid, score, decision = self.assign(emb, pdur, ts + a0)
            self.counters[decision] += 1
            self._metric(decision)
            row = self._store.add_utterance(
                ts=ts + a0, bus=item.get('source_id', ''), source=item.get('upstream_source') or '',
                freq='', duration=round(pdur, 2), model=self.model_key, emb=emb,
                profile_id=pid, score=None if score is None else round(float(score), 4),
                decision=decision, part=k, parts=len(bounds) - 1)
            if self.keep_clips:
                clip = self._save_clip(row, ts + a0, seg)
                if clip:
                    self._store.set_clip(row, clip)
            parts.append((row, ts + a0, pdur, item.get('source_id', ''), pid, a0, a1))
        if cuts:
            self.counters['split_overs'] += 1
            self._metric('split')
        uid = item.get('uid')
        if uid is not None and parts:
            self._uid_to_row[uid] = (parts, cuts, dur)
            while len(self._uid_to_row) > 500:
                self._uid_to_row.popitem(last=False)

    def _find_cuts(self, audio):
        """Speaker-change times inside one utterance ([] = one speaker). Never raises."""
        if not self.split or len(audio) < self._split_mod.MIN_SECS * 16000:
            return []
        try:
            cuts = self._split_mod.find_boundaries(audio, self._ensure_embedder().embed,
                                                   split_at=self.split_at)
            self.counters['split_checked'] += 1
            return cuts
        except Exception as e:
            self.counters['split_errors'] += 1
            self.last_error = f'split: {type(e).__name__}: {e}'
            log_exc()
            return []

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
            # Only a confident match moves the voice's average: borderline ones
            # dragged voices towards other people on busy nets (drift).
            if dur >= self.learn_secs and best >= self.learn_at:
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
        entry = self._uid_to_row.get(uid)
        if entry is None:
            self.counters['text_orphan'] += 1      # over was too short, dropped or skipped
            return
        parts, cuts, dur = entry
        text = result.get('text', '')
        wt = result.get('word_times')
        from speaker_id.split import split_text, part_word_times
        texts, starts = split_text(text, cuts, wt, dur)
        ends = starts[1:] + [len(text) + 1]
        by_start = {round(a0, 3): k for k, a0 in enumerate([0.0] + cuts)}
        for row, ts, pdur, bus, pid, a0, a1 in parts:
            k = by_start.get(round(a0, 3), 0)
            ptext = texts[k] if k < len(texts) else ''
            pwt = part_word_times(wt, starts[k], ends[k], a0) if cuts else wt
            self._store.set_text(row, ptext, tx_id=result.get('tx_id'),
                                 freq=result.get('freq') or None, source=result.get('source') or None)
            self.counters['texted'] += 1
            if self._ident:
                filed = self._ident.file(row, ts, pdur, bus, ptext, word_times=pwt, profile_id=pid)
                if filed:
                    self.counters['evidence_overs'] += 1
                # A self-ID on this voice may tip it over the naming bar.
                if pid and 'callsign_self' in filed:
                    if self._ident.maybe_name(pid):
                        self._refresh_identity(pid)
            if pid and self.alert_for:
                self._maybe_alert(pid, ts, bus, dict(result, text=ptext))

    ALERT_COOLDOWN = 1800.0

    def _maybe_alert(self, pid, ts, bus, result):
        """Email when a voice on SPEAKER_ID_ALERT is heard (once per person per cooldown)."""
        p = self._store.profile(pid)
        ident = self._store.identity(p['identity_id']) if p and p['identity_id'] else None
        if not ident:
            return
        keys = {(ident.get('callsign') or '').upper(), (ident.get('name') or '').upper()} - {''}
        hit = keys & self.alert_for
        if not hit:
            return
        now = time.monotonic()
        last = self._alert_last.get(ident['id'])
        if last is not None and now - last < self.ALERT_COOLDOWN:
            return
        notifier = getattr(self._gateway, 'email_notifier', None) if self._gateway else None
        if notifier is None or not notifier.is_configured():
            self.counters['alert_no_email'] += 1
            return
        self._alert_last[ident['id']] = now
        who = ' · '.join(x for x in (ident.get('callsign'), ident.get('name')) if x)
        freq = result.get('freq') or bus or '?'
        subject = f'[Gateway] {who} heard on {freq}'
        body = (f"{who} (voice #{pid}, {p['label_source'] or 'unlabelled'})\nChannel: {bus} {result.get('freq') or ''}\n"
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}\n\n"
                f"{result.get('text', '')}\n\n-- Radio Gateway speaker ID")
        threading.Thread(target=notifier.send, args=(subject, body), daemon=True, name='SpeakerAlert').start()
        self.counters['alerts'] += 1

    def _refresh_identity(self, pid):
        p = self._store.profile(pid)
        for entry in self._profiles:
            if entry[0] == pid and p:
                entry[3] = p['identity_id']

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
            'thresholds': {'accept': self.accept, 'learn_at': self.learn_at, 'new': self.new,
                           'min_secs': self.min_secs, 'learn_secs': self.learn_secs},
            'queue': qlen,
            'profiles_loaded': len(self._profiles),
            'counters': dict(self.counters),
            'embed_ms_avg': round(sum(ms) / len(ms), 1) if ms else None,
            'offload': (self._embedder.get_status() if hasattr(self._embedder, 'get_status')
                        else {'mode': 'local'}),
            'db': counts,
            'last_error': self.last_error,
            'running': bool(self._thread and self._thread.is_alive()),
            'nightly': {'time': self.nightly_time or None, 'last': self.last_nightly,
                        'last_day': self._store.meta_get('last_nightly_day') if self._store else None},
            'model_switch': (None if self.target_model == self.model_key else
                             dict(zip(('done', 'left'), self._store.reembed_count(self.target_model)),
                                  to=self.target_model)),
            'callsigns': {
                'enabled': bool(self._ident),
                'uls': self._uls.get_status() if self._uls else None,
                'regulars_seeded': self._seeded,
                'regulars': len(self._ident.regulars.mentions) if self._ident else 0,
                'backfill_done': self._backfill_done,
                'counters': dict(self._ident.counters) if self._ident else {},
            },
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
