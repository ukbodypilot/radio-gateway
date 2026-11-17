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

SCHEMA_VERSION = 5   # 2: profiles.merged_into; 3: evidence.at; 4: evidence.ch; 5: utterances.part/parts (split overs)

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
CREATE TABLE IF NOT EXISTS reembed (
    utt_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    emb BLOB NOT NULL
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
            cols = {r[1] for r in self._conn.execute('PRAGMA table_info(profiles)')}
            if 'merged_into' not in cols:
                self._conn.execute('ALTER TABLE profiles ADD COLUMN merged_into INTEGER')
            ecols = {r[1] for r in self._conn.execute('PRAGMA table_info(evidence)')}
            if 'at' not in ecols:
                self._conn.execute('ALTER TABLE evidence ADD COLUMN at REAL')
            if 'ch' not in ecols:
                self._conn.execute('ALTER TABLE evidence ADD COLUMN ch INTEGER')
            ucols = {r[1] for r in self._conn.execute('PRAGMA table_info(utterances)')}
            if 'part' not in ucols:
                self._conn.execute('ALTER TABLE utterances ADD COLUMN part INTEGER NOT NULL DEFAULT 0')
                self._conn.execute('ALTER TABLE utterances ADD COLUMN parts INTEGER NOT NULL DEFAULT 1')
            self._conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)',
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
                      profile_id, score, decision, clip='', part=0, parts=1):
        """part/parts: one utterance split at a speaker change is stored as `parts` rows."""
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO utterances(ts, bus, source, freq, duration, model, emb, '
                'profile_id, score, decision, clip, part, parts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ts, bus, source or '', freq or '', duration, model, _blob(emb),
                 profile_id, score, decision, clip, part, parts))
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

    def add_evidence(self, utt_id, kind, value, weight, source, version=1, at=None, ch=None):
        """at: seconds into the over the clue was said; ch: its char offset in the transcript."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO evidence(utt_id, kind, value, weight, source, version, ts, at, ch) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (utt_id, kind, str(value), float(weight), source, int(version), time.time(), at, ch))
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
                'SELECT id, centroid, n, identity_id FROM profiles '
                'WHERE model=? AND merged_into IS NULL ORDER BY id',
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

    def quality(self, since):
        """How well the voices are doing over overs since `since` (epoch seconds).

        placed_share   overs put in a voice (vs unsure / released / too short to place)
        named_share    overs in a voice that has a name
        selfid_purity  self-IDs ("this is <call>") that agree with the most-given
                       call in their voice: < 1.0 means voices still mix people
        selfid_agree   self-IDs in NAMED voices that equal the voice's name
        selfids        how many self-IDs that rests on (small = noisy)
        Shares are None when there is nothing to divide by.
        """
        with self._lock:
            c = self._conn
            n, placed, named = c.execute(
                'SELECT count(*), count(u.profile_id), count(p.identity_id) FROM utterances u '
                'LEFT JOIN profiles p ON p.id = u.profile_id WHERE u.ts >= ?', (since,)).fetchone()
            ids = c.execute(
                "SELECT u.profile_id, e.value, i.callsign FROM evidence e "
                "JOIN utterances u ON u.id = e.utt_id "
                "LEFT JOIN profiles p ON p.id = u.profile_id "
                "LEFT JOIN identities i ON i.id = p.identity_id "
                "WHERE e.kind = 'callsign_self' AND u.profile_id IS NOT NULL AND u.ts >= ?",
                (since,)).fetchall()
        by = {}
        for pid, val, _ in ids:
            by.setdefault(pid, {}).setdefault(val, 0)
            by[pid][val] += 1
        maj = sum(max(v.values()) for v in by.values())
        in_named = [(val, call) for _pid, val, call in ids if call]
        share = lambda a, b: round(a / b, 4) if b else None
        return {
            'overs': n, 'placed_share': share(placed, n), 'named_share': share(named, n),
            'selfids': len(ids), 'selfid_purity': share(maj, len(ids)),
            'selfid_agree': share(sum(1 for v, cl in in_named if v == cl), len(in_named)),
            'selfids_named': len(in_named),
        }

    def counts(self):
        with self._lock:
            c = self._conn
            return {
                'utterances': c.execute('SELECT COUNT(*) FROM utterances').fetchone()[0],
                'profiles': c.execute('SELECT COUNT(*) FROM profiles').fetchone()[0],
                'identities': c.execute('SELECT COUNT(*) FROM identities').fetchone()[0],
                'evidence': c.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            }

    # ── evidence / identity ─────────────────────────────────────────────
    def clear_rule_evidence(self, utt_id, source):
        """Drop one rule-source's evidence on an over before re-scoring it (never human)."""
        with self._lock:
            self._conn.execute('DELETE FROM evidence WHERE utt_id=? AND source=?', (utt_id, source))
            self._conn.commit()

    def profile_evidence(self, profile_id):
        """[(kind, value, weight, source, utt_ts)] over every over assigned to a profile."""
        with self._lock:
            return self._conn.execute(
                'SELECT e.kind, e.value, e.weight, e.source, u.ts FROM evidence e '
                'JOIN utterances u ON u.id = e.utt_id WHERE u.profile_id=?',
                (profile_id,)).fetchall()

    def profile(self, profile_id):
        with self._lock:
            r = self._conn.execute(
                'SELECT id, identity_id, model, n, created, last_seen, label_source '
                'FROM profiles WHERE id=?', (profile_id,)).fetchone()
        if not r:
            return None
        return dict(zip(('id', 'identity_id', 'model', 'n', 'created', 'last_seen',
                         'label_source'), r))

    def identity_for_call(self, callsign, create_name=None):
        """Existing identity id for a callsign, or a new one if create_name is given."""
        with self._lock:
            r = self._conn.execute('SELECT id FROM identities WHERE callsign=? ORDER BY id LIMIT 1',
                                   (callsign,)).fetchone()
            if r:
                return r[0]
            if create_name is None:
                return None
            cur = self._conn.execute('INSERT INTO identities(callsign, name, created) VALUES (?,?,?)',
                                     (callsign, create_name or '', time.time()))
            self._conn.commit()
            return cur.lastrowid

    def identity_call(self, identity_id):
        with self._lock:
            r = self._conn.execute('SELECT callsign FROM identities WHERE id=?', (identity_id,)).fetchone()
        return r[0] if r else None

    def max_utterance_id(self):
        with self._lock:
            return self._conn.execute('SELECT COALESCE(MAX(id), 0) FROM utterances').fetchone()[0]

    def set_profile_identity(self, profile_id, identity_id, label_source):
        with self._lock:
            self._conn.execute('UPDATE profiles SET identity_id=?, label_source=? WHERE id=?',
                               (identity_id, label_source, profile_id))
            self._conn.commit()

    def texted_utterances(self, after_id=0, limit=500):
        """[(id, ts, bus, freq, text, profile_id, duration)] with a transcript, in id order."""
        with self._lock:
            return self._conn.execute(
                "SELECT id, ts, bus, freq, text, profile_id, duration FROM utterances "
                "WHERE id > ? AND text <> '' ORDER BY id LIMIT ?", (after_id, limit)).fetchall()

    def callsign_at(self, utt_ids):
        """{utt_id: (callsign, at)} for overs where the speaker gave their call."""
        ids = [int(i) for i in utt_ids]
        if not ids:
            return {}
        q = ','.join('?' * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT utt_id, value, MIN(COALESCE(at, 0)) FROM evidence WHERE kind='callsign_self' "
                f"AND utt_id IN ({q}) GROUP BY utt_id", ids).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def voice_calls(self, profile_id):
        """Callsigns this voice has given as its own."""
        with self._lock:
            return {r[0] for r in self._conn.execute(
                "SELECT DISTINCT e.value FROM evidence e JOIN utterances u ON u.id = e.utt_id "
                "WHERE u.profile_id=? AND e.kind='callsign_self'", (profile_id,))}

    def callsign_quotes(self, profile_id, callsign, limit=3, width=(45, 70)):
        """[{id, at, quote}] where this voice said `callsign`: the words around it, to judge by."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, e.at, e.ch, u.text, u.clip <> '' FROM utterances u "
                "JOIN evidence e ON e.utt_id = u.id WHERE u.profile_id=? AND e.kind='callsign_self' "
                "AND e.value=? ORDER BY u.ts DESC LIMIT ?", (profile_id, callsign, limit)).fetchall()
        out = []
        for uid, at, ch, text, has_clip in rows:
            text = text or ''
            if ch is None:
                q = text[:width[0] + width[1]]
            else:
                a, b = max(0, ch - width[0]), min(len(text), ch + width[1])
                q = ('…' if a else '') + text[a:b].strip() + ('…' if b < len(text) else '')
            out.append({'id': uid, 'at': at, 'quote': q, 'clip': bool(has_clip)})
        return out

    def callsign_overs(self, profile_id, callsign, limit=3):
        """[(utt_id, at)] overs of this voice where it said `callsign`, clip kept, best first."""
        with self._lock:
            return self._conn.execute(
                "SELECT u.id, MIN(COALESCE(e.at, 0)) FROM utterances u JOIN evidence e ON e.utt_id = u.id "
                "WHERE u.profile_id=? AND e.kind='callsign_self' AND e.value=? AND u.clip <> '' "
                "GROUP BY u.id ORDER BY u.score DESC LIMIT ?", (profile_id, callsign, limit)).fetchall()

    def meta_get(self, key, default=None):
        with self._lock:
            r = self._conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key, value):
        with self._lock:
            self._conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)', (key, str(value)))
            self._conn.commit()

    # ── curation (human) ────────────────────────────────────────────────
    def list_profiles(self):
        """Every live voice with its counts and label, busiest first."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT p.id, p.model, p.identity_id, p.label_source, i.callsign, i.name, '
                '  COUNT(u.id), MIN(u.ts), MAX(u.ts), COALESCE(SUM(u.duration), 0), '
                "  GROUP_CONCAT(DISTINCT u.bus), GROUP_CONCAT(DISTINCT NULLIF(u.freq, '')) "
                'FROM profiles p LEFT JOIN identities i ON i.id = p.identity_id '
                'LEFT JOIN utterances u ON u.profile_id = p.id '
                'WHERE p.merged_into IS NULL GROUP BY p.id ORDER BY COUNT(u.id) DESC').fetchall()
        keys = ('id', 'model', 'identity_id', 'label_source', 'callsign', 'name', 'overs',
                'first', 'last', 'seconds', 'buses', 'freqs')
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d['buses'] = sorted(filter(None, (d['buses'] or '').split(',')))
            d['freqs'] = sorted(filter(None, (d['freqs'] or '').split(',')))
            d['seconds'] = round(d['seconds'] or 0, 1)
            out.append(d)
        return out

    def profile_overs(self, profile_id, limit=50, best=False):
        """Overs of one voice: newest first, or best-matching first (for sample clips)."""
        order = 'u.score DESC, u.duration DESC' if best else 'u.ts DESC'
        cond = " AND u.clip <> ''" if best else ''
        with self._lock:
            rows = self._conn.execute(
                'SELECT u.id, u.ts, u.bus, u.freq, u.duration, u.text, u.score, u.decision, u.clip '
                f'FROM utterances u WHERE u.profile_id=?{cond} ORDER BY {order} LIMIT ?',
                (profile_id, limit)).fetchall()
        keys = ('id', 'ts', 'bus', 'freq', 'duration', 'text', 'score', 'decision', 'clip')
        return [dict(zip(keys, r)) for r in rows]

    def over_ids(self, profile_id):
        with self._lock:
            return [r[0] for r in self._conn.execute(
                'SELECT id FROM utterances WHERE profile_id=?', (profile_id,))]

    def move_overs(self, utt_ids, to_profile):
        ids = list(utt_ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany('UPDATE utterances SET profile_id=? WHERE id=?',
                                   [(to_profile, i) for i in ids])
            self._conn.commit()

    def set_merged(self, profile_id, into):
        with self._lock:
            self._conn.execute('UPDATE profiles SET merged_into=? WHERE id=?', (into, profile_id))
            self._conn.commit()

    def recompute_centroid(self, profile_id, min_secs=0.0, model=None, min_score=None):
        """Centroid = normalised mean of the voice's over embeddings (of `model`). Returns n used.

        Falls back to every over when none is long enough, so a voice built
        from short overs still has a centroid.
        """
        mcond, margs = ('AND model=? ', (model,)) if model else ('', ())
        if min_score is not None:
            # Only confident overs shape the voice (the over that started it counts).
            mcond += "AND (score >= ? OR decision = 'new_profile') "
            margs += (min_score,)
        with self._lock:
            rows = self._conn.execute(
                'SELECT emb FROM utterances WHERE profile_id=? AND emb IS NOT NULL AND duration>=? '
                + mcond, (profile_id, min_secs) + margs).fetchall()
            if not rows:
                rows = self._conn.execute(
                    'SELECT emb FROM utterances WHERE profile_id=? AND emb IS NOT NULL ' + mcond,
                    (profile_id,) + margs).fetchall()
            if not rows:
                return 0
            m = np.mean([_vec(r[0]) for r in rows], axis=0)
            n = float(np.linalg.norm(m))
            if n > 0:
                self._conn.execute('UPDATE profiles SET centroid=?, n=? WHERE id=?',
                                   (_blob(m / n), len(rows), profile_id))
                self._conn.commit()
            return len(rows)

    def embeddings(self, model, min_secs=0.0, limit=200_000):
        """[(utt_id, profile_id, emb)] for every over of `model`, newest first, bounded."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT id, profile_id, emb FROM utterances WHERE model=? AND emb IS NOT NULL '
                'AND duration>=? ORDER BY id DESC LIMIT ?', (model, min_secs, limit)).fetchall()
        return [(r[0], r[1], _vec(r[2])) for r in rows]

    # ── model swap ──────────────────────────────────────────────────────
    def reembed_todo(self, target, limit=10):
        """Overs with a clip that have no `target`-model fingerprint yet."""
        with self._lock:
            return self._conn.execute(
                "SELECT u.id, u.clip FROM utterances u LEFT JOIN reembed r ON r.utt_id = u.id "
                "WHERE u.clip <> '' AND u.model <> ? AND r.utt_id IS NULL ORDER BY u.id LIMIT ?",
                (target, limit)).fetchall()

    def reembed_put(self, utt_id, model, emb):
        with self._lock:
            self._conn.execute('INSERT OR REPLACE INTO reembed(utt_id, model, emb) VALUES (?,?,?)',
                               (utt_id, model, _blob(emb)))
            self._conn.commit()

    def reembed_finish(self, target):
        """Swap every re-fingerprinted over to `target` and move the voices with them."""
        with self._lock:
            c = self._conn
            n = c.execute('UPDATE utterances SET emb=(SELECT emb FROM reembed r WHERE r.utt_id=utterances.id), '
                          'model=? WHERE id IN (SELECT utt_id FROM reembed WHERE model=?)',
                          (target, target)).rowcount
            c.execute('DELETE FROM reembed')
            pids = [r[0] for r in c.execute('SELECT DISTINCT profile_id FROM utterances '
                                            'WHERE model=? AND profile_id IS NOT NULL', (target,))]
            c.commit()
        moved = 0
        for pid in pids:
            if self.recompute_centroid(pid, 0.0, model=target):
                with self._lock:
                    self._conn.execute('UPDATE profiles SET model=? WHERE id=?', (target, pid))
                    self._conn.commit()
                moved += 1
        return n, moved

    def reembed_count(self, target):
        with self._lock:
            done = self._conn.execute('SELECT COUNT(*) FROM reembed WHERE model=?', (target,)).fetchone()[0]
            left = self._conn.execute(
                "SELECT COUNT(*) FROM utterances u LEFT JOIN reembed r ON r.utt_id=u.id "
                "WHERE u.clip <> '' AND u.model <> ? AND r.utt_id IS NULL", (target,)).fetchone()[0]
        return done, left

    def decisions(self, utt_ids):
        """{utt_id: decision} for these overs."""
        ids = list(utt_ids)
        out = {}
        with self._lock:
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                q = ','.join('?' * len(chunk))
                out.update(self._conn.execute(
                    f'SELECT id, decision FROM utterances WHERE id IN ({q})', chunk).fetchall())
        return out

    def set_decisions(self, utt_ids, decision):
        ids = list(utt_ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany('UPDATE utterances SET decision=? WHERE id=?', [(decision, i) for i in ids])
            self._conn.commit()

    def utterance_emb(self, utt_id):
        with self._lock:
            r = self._conn.execute('SELECT emb, model, ts FROM utterances WHERE id=?', (utt_id,)).fetchone()
        return (_vec(r[0]), r[1], r[2]) if r and r[0] is not None else (None, None, None)

    def update_identity(self, identity_id, name=None, notes=None):
        with self._lock:
            if name is not None:
                self._conn.execute('UPDATE identities SET name=? WHERE id=?', (name, identity_id))
            if notes is not None:
                self._conn.execute('UPDATE identities SET notes=? WHERE id=?', (notes, identity_id))
            self._conn.commit()

    def new_identity(self, callsign='', name=''):
        with self._lock:
            cur = self._conn.execute('INSERT INTO identities(callsign, name, created) VALUES (?,?,?)',
                                     (callsign, name, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def identity(self, identity_id):
        with self._lock:
            r = self._conn.execute('SELECT id, callsign, name, notes FROM identities WHERE id=?',
                                   (identity_id,)).fetchone()
        return dict(zip(('id', 'callsign', 'name', 'notes'), r)) if r else None

    def add_cannot_link(self, a, b):
        a, b = sorted((int(a), int(b)))
        with self._lock:
            self._conn.execute('INSERT OR IGNORE INTO cannot_link(a, b, ts) VALUES (?,?,?)',
                               (a, b, time.time()))
            self._conn.commit()

    def remove_cannot_link(self, a, b):
        a, b = sorted((int(a), int(b)))
        with self._lock:
            self._conn.execute('DELETE FROM cannot_link WHERE a=? AND b=?', (a, b))
            self._conn.commit()

    def cannot_links(self):
        with self._lock:
            return {(r[0], r[1]) for r in self._conn.execute('SELECT a, b FROM cannot_link')}

    def audit_rows(self, limit=50):
        with self._lock:
            rows = self._conn.execute('SELECT id, ts, actor, action, payload FROM audit '
                                      'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{'id': r[0], 'ts': r[1], 'actor': r[2], 'action': r[3],
                 'payload': json.loads(r[4] or '{}')} for r in rows]

    def audit_row(self, audit_id):
        with self._lock:
            r = self._conn.execute('SELECT id, ts, actor, action, payload FROM audit WHERE id=?',
                                   (audit_id,)).fetchone()
        return {'id': r[0], 'ts': r[1], 'actor': r[2], 'action': r[3],
                'payload': json.loads(r[4] or '{}')} if r else None

    def last_audit_id(self):
        with self._lock:
            return self._conn.execute('SELECT COALESCE(MAX(id), 0) FROM audit').fetchone()[0]

    def speakers_for_tx(self, tx_ids):
        """{tx_id: {profile_id, callsign, name, label_source, said}} for transcription rows.

        said = the callsign the speaker gave in that over (rule evidence), if any.
        """
        ids = [int(i) for i in tx_ids if i is not None]
        if not ids:
            return {}
        q = ','.join('?' * len(ids))
        with self._lock:
            rows = self._conn.execute(
                'SELECT u.tx_id, u.profile_id, i.callsign, i.name, p.label_source, '
                "  (SELECT e.value FROM evidence e WHERE e.utt_id = u.id AND e.kind = 'callsign_self' LIMIT 1) "
                'FROM utterances u LEFT JOIN profiles p ON p.id = u.profile_id '
                'LEFT JOIN identities i ON i.id = p.identity_id '
                f'WHERE u.tx_id IN ({q}) ORDER BY u.tx_id, u.part', ids).fetchall()
        out = {}
        for r in rows:
            d = {'profile_id': r[1], 'callsign': r[2] or '', 'name': r[3] or '',
                 'label_source': r[4] or '', 'said': r[5] or ''}
            prev = out.get(r[0])
            parts = (prev['parts'] if prev else []) + [d]
            # The row's headline is the LAST speaker (the one handing over next);
            # 'parts' lists everyone when one utterance held a speaker change.
            out[r[0]] = dict(d, parts=parts)
        return out

    def by_tx_ids(self, tx_ids):
        """{tx_id: {profile_id, score, decision}} for transcription rows (for the UI chip)."""
        ids = [int(i) for i in tx_ids if i is not None]
        if not ids:
            return {}
        q = ','.join('?' * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f'SELECT tx_id, profile_id, score, decision FROM utterances WHERE tx_id IN ({q}) '
                'ORDER BY tx_id, part',
                ids).fetchall()
        return {r[0]: {'profile_id': r[1], 'score': r[2], 'decision': r[3]} for r in rows}
