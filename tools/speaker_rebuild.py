#!/usr/bin/env python3
"""Rebuild the speaker-ID database from the loop recordings with the current code.

Use after a change to speaker-ID thresholds, splitting or evidence rules, so
the history is filed exactly as the new code would have filed it live. The
result is a NEW database; the live one is never touched.

  1. Segments each bus's loop mp3s (recordings/loop/<bus>/*.mp3, 24 h kept)
     with the transcriber's own VAD state machine and its saved settings
     (.transcribe_settings.json: threshold, hold, min duration, boost).
  2. Keeps only segments that overlap a TX Log row on the same bus (2 s
     slack): exactly the buses and times that were being transcribed, and
     each over keeps its tx_id and start time, so the TX Log, /transcribe and
     /speakers agree.
  3. Re-transcribes each over with Parakeet for word timings (callsign
     positions, transcript splitting).
  4. Feeds them, in time order across buses, through a fresh SpeakerID
     service exactly as the gateway does (fingerprint, split + veto, callsign
     evidence against the offline FCC table, naming). The nightly pass runs at
     each 03:30 crossed and once at the end.

Heavy (ASR + fingerprints for a day of audio, ~1 h on a 4-core i7): run it on
a transcription worker, not the gateway box. Copy there: this repo, the loop
mp3s, a snapshot of transcriptions.db and uls.db (sqlite3 .backup, never a
raw copy of a live WAL database). Needs onnxruntime, onnx-asr, numpy, ffmpeg.

  python3 tools/speaker_rebuild.py --loop DIR --tx-db FILE --uls FILE --out DIR
        [--dry]     segment + match only; prints the TX Log timing offsets
        [--topup]   add only what was recorded after the last over in --out

Then swap in the result (gateway stopped or about to restart):
  copy OUT/ to ~/.config/radio-gateway/speakers-v2/ and set
  SPEAKER_ID_DB_PATH = <that>/speakers.db in [transcription]. The clips sit
  beside the database. Rollback: blank the key and restart.

Human curation (labels, merges, "different people") is NOT carried over: a
rebuild starts the voices from scratch, so labels have to be given again.
"""
import argparse
import bisect
import glob
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SR, FRAME, CTX = 16000, 512, 64
SLACK = 2.0                 # seconds either side when matching a TX Log row
FILE_SECS = 300             # loop recorder segment length


def transcriber_settings():
    """The transcriber's VAD state machine constants and its saved settings."""
    import re
    src = open(os.path.join(ROOT, 'transcriber.py')).read()
    const = lambda name: float(re.search(rf'^{name}\s*=\s*([0-9.]+)', src, re.M).group(1))
    try:
        saved = json.load(open(os.path.join(ROOT, '.transcribe_settings.json')))
    except (OSError, ValueError):
        saved = {}
    return {
        'thr': float(saved.get('vad_threshold', 0.5)),
        'hyst': const('_VAD_HYSTERESIS'),
        'hold': float(saved.get('vad_hold', 1.0)),
        'min_dur': float(saved.get('min_duration', 0.5)),
        'soft': const('_SOFT_CAP_SECS'),
        'hard': const('_MAX_UTTERANCE_SECS'),
        'pause': int(const('_SOFT_CAP_PAUSE_FRAMES')),
        'boost': float(saved.get('audio_boost', 100)) / 100.0,
    }


def load_mp3(path):
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(SR),
                        '-f', 'f32le', '-'], capture_output=True, check=True, timeout=120)
    return np.frombuffer(r.stdout, np.float32).copy()


def segments(vad, audio, st):
    """(start, end) samples, cut like the live transcriber (hold measured in audio time)."""
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, CTX), np.float32)
    srt = np.array(SR, dtype=np.int64)
    boosted = np.tanh(audio * st['boost']).astype(np.float32)
    exit_t = max(0.0, st['thr'] - st['hyst'])
    hold_frames = st['hold'] * SR / FRAME
    open_, start, n, low_run, close_at = False, 0, 0, 0, None
    for i in range(len(boosted) // FRAME):
        x = np.concatenate([ctx, boosted[i * FRAME:(i + 1) * FRAME][None, :]], axis=1)
        out, state = vad.run(None, {'input': x, 'state': state, 'sr': srt})
        ctx = x[:, -CTX:]
        p = float(out[0, 0])
        if open_:
            n += 1
            low_run = low_run + 1 if p < exit_t else 0
            cut = (n * FRAME >= st['hard'] * SR
                   or (n * FRAME >= st['soft'] * SR and low_run >= st['pause']))
            if not cut and p < exit_t:
                if close_at is None:
                    close_at = i
                elif i - close_at > hold_frames:
                    cut = True
            elif p >= exit_t:
                close_at = None
            if cut:
                if n * FRAME / SR >= st['min_dur']:
                    yield start, start + n * FRAME
                open_ = False
        elif p >= st['thr']:
            open_, start, n, low_run, close_at = True, i * FRAME, 1, 0, None
    if open_ and n * FRAME / SR >= st['min_dur']:
        yield start, start + n * FRAME


class TxRows:
    """TX Log rows per bus, for matching a segment to the row the live system logged."""

    def __init__(self, conn):
        self.rows = {}
        for rid, ts, dur, bus in conn.execute('SELECT id, ts, duration, bus FROM transcriptions ORDER BY ts'):
            self.rows.setdefault(bus, []).append((ts, ts + max(dur, 0.1), rid))
        self.starts = {b: [r[0] for r in v] for b, v in self.rows.items()}

    def match(self, bus, t0, t1, used):
        """(overlap, row id, row start) of the best unused row overlapping [t0, t1), or None."""
        v = self.rows.get(bus) or []
        i = bisect.bisect_left(self.starts.get(bus, []), t0 - 130)
        best = None
        while i < len(v) and v[i][0] <= t1 + SLACK:
            a, b, rid = v[i]
            ov = min(b, t1 + SLACK) - max(a, t0 - SLACK)
            if ov > 0 and rid not in used and (best is None or ov > best[0]):
                best = (ov, rid, a)
            i += 1
        return best


def loop_files(loop_dir, buses):
    out = []
    for bus in sorted(os.listdir(loop_dir)):
        if buses and bus not in buses:
            continue
        for p in sorted(glob.glob(os.path.join(loop_dir, bus, '*.mp3'))):
            t0 = time.mktime(time.strptime(os.path.basename(p)[:13], '%Y%m%d_%H%M'))
            out.append((t0, bus, p))
    return sorted(out)


def silero_session(path):
    import onnxruntime as ort
    if not path:
        import importlib.util
        spec = importlib.util.find_spec('silero_vad')       # locate only: importing pulls torch
        for base in (spec.submodule_search_locations or []) if spec else []:
            cand = os.path.join(base, 'data', 'silero_vad.onnx')
            if os.path.exists(cand):
                path = cand
    if not path:
        sys.exit('Silero VAD model not found: pass --silero PATH')
    os.environ.setdefault('SILERO_ONNX_PATH', path)         # speaker_id.split uses it too
    so = ort.SessionOptions()
    so.intra_op_num_threads = so.inter_op_num_threads = 1
    return ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--loop', default=os.path.join(ROOT, 'recordings', 'loop'))
    ap.add_argument('--tx-db', required=True, help='snapshot of transcriptions.db')
    ap.add_argument('--uls', required=True, help='uls.db (offline FCC table)')
    ap.add_argument('--out', required=True, help='directory for the new speakers.db + speaker_clips/')
    ap.add_argument('--buses', default='', help='comma-separated; default every bus with TX Log rows')
    ap.add_argument('--silero', default='')
    ap.add_argument('--threads', type=int, default=3, help='ASR threads')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--topup', action='store_true')
    a = ap.parse_args()

    st = transcriber_settings()
    print('transcriber settings', st, flush=True)
    vad = silero_session(a.silero)
    tl = sqlite3.connect(a.tx_db, check_same_thread=False)
    tx = TxRows(tl)
    buses = {b.strip() for b in a.buses.split(',') if b.strip()}
    db_path = os.path.join(a.out, 'speakers.db')

    since, used, uid = 0.0, set(), 0
    if a.topup:
        o = sqlite3.connect(db_path)
        since = o.execute('SELECT max(ts) FROM utterances').fetchone()[0] or 0.0
        used = {r[0] for r in o.execute('SELECT tx_id FROM utterances WHERE tx_id IS NOT NULL')}
        o.close()
        uid = 10_000_000
        print('top-up after', time.ctime(since), flush=True)
    elif not a.dry and os.path.exists(db_path):
        sys.exit(f'{db_path} exists: pick an empty --out, or pass --topup')

    if not a.dry:
        import onnxruntime as ort
        import onnx_asr
        import config_schema
        from transcribe_engine import word_times
        from speaker_id.service import SpeakerID
        from speaker_id.store import SpeakerStore
        from speaker_id.uls import UlsLookup
        so = ort.SessionOptions()
        so.intra_op_num_threads = a.threads
        asr = onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v2', quantization='int8',
                                  sess_options=so).with_timestamps()
        cfg = types.SimpleNamespace(**{k: v for k, v in config_schema.DEFAULTS.items()
                                       if k.startswith('SPEAKER_ID')})
        cfg.SPEAKER_ID_OFFLOAD = 'local'
        cfg.SPEAKER_ID_NIGHTLY_TIME = ''          # run below, at replayed 03:30s
        cfg.SPEAKER_ID_ALERT = ''
        cfg.SPEAKER_ID_DB_PATH = db_path
        os.makedirs(a.out, exist_ok=True)
        gw = types.SimpleNamespace(transcription_log=types.SimpleNamespace(_conn=tl, _lock=threading.Lock()))
        svc = SpeakerID(cfg, gw, store=SpeakerStore(db_path), uls=UlsLookup(a.uls, auto_refresh=False))
        svc.start()

        def drain():
            while svc.counters['processed'] < svc.counters['queued'] or svc._q:
                time.sleep(0.05)

        def nightly(at):
            drain()
            rep = svc.run_on_worker(lambda s: s.nightly(), timeout=600)
            print(time.strftime('%H:%M:%S'), 'nightly for', time.ctime(at), rep, flush=True)

    offs, n_seg, n_kept, next_night = [], 0, 0, None
    for t0, bus, path in loop_files(a.loop, buses):
        if bus not in tx.rows or (a.topup and t0 + FILE_SECS < since):
            continue
        try:
            audio = load_mp3(path)
        except Exception as e:                       # the file being written right now
            print('skip', path, e, flush=True)
            continue
        for s0, s1 in segments(vad, audio, st):
            n_seg += 1
            ts, dur = t0 + s0 / SR, (s1 - s0) / SR
            if ts <= since:
                continue
            m = tx.match(bus, ts, ts + dur, used)
            if not m:
                continue
            used.add(m[1])
            offs.append(ts - m[2])
            n_kept += 1
            if a.dry:
                continue
            if next_night is None:
                lt = time.localtime(ts)
                next_night = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 3, 30, 0, 0, 0, -1))
                if next_night <= ts:
                    next_night += 86400
            if ts >= next_night and not a.topup:
                nightly(next_night)
                next_night += 86400
            boosted = np.tanh(audio[s0:s1] * st['boost']).astype(np.float32)
            r = asr.recognize(boosted, sample_rate=SR)
            text = (r.text if r else '') or ''
            if not text.strip():
                continue
            try:
                wt = word_times(r.tokens, r.timestamps, text)
            except Exception:
                wt = None
            uid += 1
            while len(svc._q) > 32:                  # the service queue drops past 64
                time.sleep(0.05)
            # Start time from the TX Log row: the loop file name only gives the minute
            # (measured 0-3 s off), the row is what the live system stamped.
            svc.on_utterance({'uid': uid, 'audio_16k': boosted, 'audio_boost': st['boost'],
                              'duration': dur, 'source_id': bus, 'start_time': m[2],
                              'upstream_source': None})
            svc.on_result(uid, {'text': text, 'tx_id': m[1], 'word_times': wt})
        o = np.array(offs[-200:]) if offs else np.zeros(1)
        print(f'{time.strftime("%H:%M:%S")} {bus}/{os.path.basename(path)} segments {n_seg} '
              f'matched {n_kept} | offset vs TX Log p50 {np.median(o):+.2f}s', flush=True)

    if not a.dry:
        if not a.topup:
            nightly(time.time())
        drain()
        svc.stop()
        c = sqlite3.connect(db_path)
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        c.close()
        print('counters', dict(svc.counters))
    print(f'DONE: {n_kept} of {n_seg} segments matched a TX Log row')


if __name__ == '__main__':
    main()
