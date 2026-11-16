"""Speaker-ID database: every over, every piece of evidence, every human label.

Design (2026-11-15): identity is a long-term optimisation driven by data
collection. Nothing is thrown away and every derived row says which version
of which rule or model produced it, so when a model or rule improves the whole
history can be re-scored (like lan-inventory's identify.VERSION).

  utterances   one row per over seen by speaker ID: when/where, the transcript,
               the embedding (model-tagged), the live assignment and its score
  profiles     a voice: centroid of the overs assigned to it, per model.
               A person may have several (handheld vs mobile vs repeater)
  identities   a person: callsign, name, notes. Human-curated; never deleted
               by automation
  evidence     everything that points at who spoke an over: voice match,
               callsign self-ID, turn-taking cue, human label... with weight,
               source and rule version
  audit        every human or automatic change to identities/profiles, so any
               merge or label can be undone
  cannot_link  "these two profiles are NOT the same voice" (human), obeyed by
               the nightly re-cluster

Private by nature (voices, callsigns, names): lives in ~/.config/radio-gateway,
never in the repo.
"""
import json
import os
import sqlite3
import threading
import time

import numpy as np

SCHEMA_VERSION = 1

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY,
    callsign TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS identities_call ON identities(callsign);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    identity_id INTEGER,
    model TEXT NOT NULL,
    centroid BLOB NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    label_source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS profiles_model ON profiles(model);
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    bus TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    freq TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    tx_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    emb BLOB,
    profile_id INTEGER,
    score REAL,
    decision TEXT NOT NULL DEFAULT '',
    clip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS utterances_ts ON utterances(ts);
CREATE INDEX IF NOT EXISTS utterances_profile ON utterances(profile_id);
CREATE INDEX IF NOT EXISTS utterances_tx ON utterances(tx_id);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    utt_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_utt ON evidence(utt_id);
CREATE INDEX IF NOT EXISTS evidence_kind_value ON evidence(kind, value);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cannot_link (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (a, b)
);
'''


def _blob(v):
    return None if v is None else np.asarray(v, np.float32).tobytes()


def _vec(b):
    return None if b is None else np.frombuffer(b, np.float32).copy()


def default_path():
    return os.path.expanduser('~/.config/radio-gateway/speakers.db')


class SpeakerStore:
    def __init__(self, path=None):
        self.path = path or default_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute('INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)',
                               ('schema_version', str(SCHEMA_VERSION)))
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ── writes ──────────────────────────────────────────────────────────
    def add_utterance(self, *, ts, bus, source, freq, duration, model, emb,
                      profile_id, score, decision, clip=''):
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO utterances(ts, bus, source, freq, duration, model, emb, '
                'profile_id, score, decision, clip) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (ts, bus, source or '', freq or '', duration, model, _blob(emb),
                 profile_id, score, decision, clip))
            self._conn.commit()
            return cur.lastrowid

    def set_text(self, utt_id, text, tx_id=None, freq=None, source=None):
        with self._lock:
            self._conn.execute(
                'UPDATE utterances SET text=?, tx_id=COALESCE(?, tx_id), '
                'freq=COALESCE(?, freq), source=COALESCE(?, source) WHERE id=?',
                (text, tx_id, freq, source, utt_id))
            self._conn.commit()

    def set_clip(self, utt_id, clip):
        with self._lock:
            self._conn.execute('UPDATE utterances SET clip=? WHERE id=?', (clip, utt_id))
            self._conn.commit()

    def add_profile(self, model, centroid, ts):
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO profiles(model, centroid, n, created, last_seen) VALUES (?,?,?,?,?)',
                (model, _blob(centroid), 1, ts, ts))
            self._conn.commit()
            return cur.lastrowid

    def update_profile(self, profile_id, centroid, n, last_seen):
        with self._lock:
            self._conn.execute('UPDATE profiles SET centroid=?, n=?, last_seen=? WHERE id=?',
                               (_blob(centroid), n, last_seen, profile_id))
            self._conn.commit()

    def touch_profile(self, profile_id, last_seen):
        with self._lock:
            self._conn.execute('UPDATE profiles SET last_seen=MAX(last_seen, ?) WHERE id=?',
                               (last_seen, profile_id))
            self._conn.commit()

    def add_evidence(self, utt_id, kind, value, weight, source, version=1):
        with self._lock:
            self._conn.execute(
                'INSERT INTO evidence(utt_id, kind, value, weight, source, version, ts) '
                'VALUES (?,?,?,?,?,?,?)',
                (utt_id, kind, str(value), float(weight), source, int(version), time.time()))
            self._conn.commit()

    def audit(self, actor, action, **payload):
        with self._lock:
            self._conn.execute('INSERT INTO audit(ts, actor, action, payload) VALUES (?,?,?,?)',
                               (time.time(), actor, action, json.dumps(payload, sort_keys=True)))
            self._conn.commit()

    # ── reads ───────────────────────────────────────────────────────────
    def load_profiles(self, model):
        """[(id, centroid, n, identity_id)] for one model."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT id, centroid, n, identity_id FROM profiles WHERE model=? ORDER BY id',
                (model,)).fetchall()
        return [(r[0], _vec(r[1]), r[2], r[3]) for r in rows]

    def profile_labels(self):
        """{profile_id: (callsign, name)} for profiles tied to an identity."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT p.id, i.callsign, i.name FROM profiles p '
                'JOIN identities i ON i.id = p.identity_id').fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def utterance(self, utt_id):
        with self._lock:
            r = self._conn.execute(
                'SELECT id, ts, bus, source, freq, duration, text, tx_id, model, profile_id, '
                'score, decision, clip FROM utterances WHERE id=?', (utt_id,)).fetchone()
        if not r:
            return None
        keys = ('id', 'ts', 'bus', 'source', 'freq', 'duration', 'text', 'tx_id', 'model',
                'profile_id', 'score', 'decision', 'clip')
        return dict(zip(keys, r))

    def evidence_for(self, utt_id):
        with self._lock:
            rows = self._conn.execute(
                'SELECT kind, value, weight, source, version FROM evidence WHERE utt_id=? ORDER BY id',
                (utt_id,)).fetchall()
        return [dict(zip(('kind', 'value', 'weight', 'source', 'version'), r)) for r in rows]

    def counts(self):
        with self._lock:
            c = self._conn
            return {
                'utterances': c.execute('SELECT COUNT(*) FROM utterances').fetchone()[0],
                'profiles': c.execute('SELECT COUNT(*) FROM profiles').fetchone()[0],
                'identities': c.execute('SELECT COUNT(*) FROM identities').fetchone()[0],
                'evidence': c.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            }

    def by_tx_ids(self, tx_ids):
        """{tx_id: {profile_id, score, decision}} for transcription rows (for the UI chip)."""
        ids = [int(i) for i in tx_ids if i is not None]
        if not ids:
            return {}
        q = ','.join('?' * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT tx_id, profile_id, score, decision FROM utterances WHERE tx_id IN ({q})',
                ids).fetchall()
        return {r[0]: {'profile_id': r[1], 'score': r[2], 'decision': r[3]} for r in rows}
