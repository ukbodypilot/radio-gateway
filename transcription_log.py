import os
import shutil
import sqlite3
import subprocess
import threading
import time

from log_util import log_exc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id       INTEGER PRIMARY KEY,
    ts       REAL    NOT NULL,
    source   TEXT    NOT NULL DEFAULT '',
    freq     TEXT    NOT NULL DEFAULT '',
    text     TEXT    NOT NULL,
    duration REAL    NOT NULL DEFAULT 0.0,
    bus      TEXT    NOT NULL DEFAULT '',
    model    TEXT    NOT NULL DEFAULT '',
    proc_time REAL   NOT NULL DEFAULT 0.0
);
CREATE VIRTUAL TABLE IF NOT EXISTS transcriptions_fts
    USING fts5(text, content=transcriptions, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS transcriptions_ai
    AFTER INSERT ON transcriptions BEGIN
        INSERT INTO transcriptions_fts(rowid, text) VALUES (new.id, new.text);
    END;
CREATE TRIGGER IF NOT EXISTS transcriptions_ad
    AFTER DELETE ON transcriptions BEGIN
        INSERT INTO transcriptions_fts(transcriptions_fts, rowid, text)
               VALUES ('delete', old.id, old.text);
    END;
"""

_SQL_SYSTEM = """\
You are a SQL query generator for a radio transcription database.

Schema:
  transcriptions(
    id       INTEGER PRIMARY KEY,
    ts       REAL,     -- Unix epoch float (seconds)
    source   TEXT,     -- radio source id, e.g. sdr1, sdr2, ftm, kv4p
    freq     TEXT,     -- frequency string, e.g. '446.760', '147.435'
    text     TEXT,     -- transcribed speech
    duration REAL,     -- audio clip duration in seconds
    model    TEXT,     -- ASR model key, e.g. 'parakeet/tdt-0.6b-v2' ('' before 2026-11-12)
    proc_time REAL     -- seconds the model took on this clip
  )

Rules:
- Respond with ONLY a single SQL SELECT statement. No markdown, no explanation, no code fences.
- Always include LIMIT (default 50, max 200).
- Only SELECT is allowed. Never use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, PRAGMA.
- Use strftime('%Y-%m-%d %H:%M:%S', ts, 'unixepoch', 'localtime') to display timestamps.
- For text search use: text LIKE '%keyword%'  (SQLite LIKE is case-insensitive by default).
- If the question cannot be answered from this schema, output exactly: SELECT 1 WHERE 0
"""

_ANSWER_SYSTEM = """\
You answer questions about a radio transcription log. \
Write 2-4 sentences of plain English. Be specific — mention frequencies, sources, \
times, or quoted speech where relevant. Do not mention SQL, databases, or technical details. \
Respond with only the answer text, no preamble.\
"""


class TranscriptionLog:
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._conn = None
        self._claude_bin = None

        _default = os.path.expanduser('~/.config/radio-gateway/transcriptions.db')
        self._db_path = str(getattr(config, 'TRANSCRIPTION_LOG_PATH', '') or _default)

        self._open()
        self._find_claude()

    def _open(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;')
        self._conn.executescript(_SCHEMA)
        # Migration: add `bus` column on databases predating it. Old rows get '',
        # which the UI treats as "no recording linkage available."
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(transcriptions)")}
            if 'bus' not in cols:
                self._conn.execute(
                    "ALTER TABLE transcriptions ADD COLUMN bus TEXT NOT NULL DEFAULT ''")
            # model/proc_time added 2026-11-12 so models can be compared from
            # the DB. Older rows get '' / 0.0 — the model is unknown for them.
            if 'model' not in cols:
                self._conn.execute(
                    "ALTER TABLE transcriptions ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            if 'proc_time' not in cols:
                self._conn.execute(
                    "ALTER TABLE transcriptions ADD COLUMN proc_time REAL NOT NULL DEFAULT 0.0")
            # One channel (bus + freq) at a time on /txlog: many repeaters
            # share the log since transcription takes several buses.
            self._conn.execute('CREATE INDEX IF NOT EXISTS transcriptions_channel '
                               'ON transcriptions(bus, freq, ts)')
        except sqlite3.Error as e:
            print(f'  [TxLog] migration error: {e}')
        self._conn.commit()

    def _find_claude(self):
        candidate = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
        if os.path.isfile(str(candidate)):
            self._claude_bin = candidate

    def close(self):
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    # Write path

    def insert(self, result: dict):
        """Store one result; returns its row id (speaker ID links overs to it) or None."""
        if not self._conn:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(
                    'INSERT INTO transcriptions(ts, source, freq, text, duration, bus, model, proc_time) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (result['timestamp'],
                     result.get('source', ''),
                     result.get('freq', ''),
                     result['text'],
                     result.get('duration', 0.0),
                     result.get('bus', ''),
                     result.get('model', ''),
                     result.get('proc_time', 0.0))
                )
                self._conn.commit()
                return cur.lastrowid
            except Exception as e:
                print(f'  [TxLog] insert error: {e}')
        return None

    def check_keywords(self, result: dict, keywords_override: str | None = None):
        """Return the first watched keyword found in the result, or None."""
        raw = keywords_override if keywords_override is not None else str(getattr(self._config, 'TRANSCRIPTION_ALERT_KEYWORDS', '') or '')
        keywords = [k.strip().lower() for k in raw.split(',') if k.strip()]
        if not keywords:
            return None
        text_lower = result['text'].lower()
        for kw in keywords:
            if kw in text_lower:
                return kw
        return None

    # ------------------------------------------------------------------
    # Read path

    @staticmethod
    def parse_channels(value):
        """'bus|freq,bus|freq' (URL-decoded) -> [(bus, freq)], or None for all.

        A channel is a bus plus a frequency tag: the frequency is the repeater,
        the bus is the receiver it came in on (one bus can be retuned, one
        frequency can arrive on two buses).
        """
        out = []
        for part in (value or '').split(','):
            if not part.strip():
                continue
            bus, _, freq = part.partition('|')
            out.append((bus.strip(), freq.strip()))
        return out or None

    @staticmethod
    def _channel_sql(channels):
        if not channels:
            return '', ()
        clause = ' OR '.join('(bus = ? AND freq = ?)' for _ in channels)
        return f'({clause})', tuple(v for ch in channels for v in ch)

    def get_channels(self, days: int = 30) -> list:
        """Channels heard in the `days` before the newest row, busiest first.

        [{'bus', 'freq', 'n', 'last'}]. Anchored on the newest row like
        get_hourly_stats, so an idle log still lists what it holds.
        """
        if not self._conn:
            return []
        with self._lock:
            try:
                newest = self._conn.execute('SELECT MAX(ts) FROM transcriptions').fetchone()[0]
                if not newest:
                    return []
                cur = self._conn.execute(
                    'SELECT bus, freq, COUNT(*), MAX(ts) FROM transcriptions WHERE ts >= ? '
                    'GROUP BY bus, freq ORDER BY COUNT(*) DESC',
                    (newest - max(1, int(days)) * 86400,))
                return [{'bus': b, 'freq': f, 'n': n, 'last': last} for b, f, n, last in cur.fetchall()]
            except Exception:
                log_exc()
                return []

    def get_recent(self, limit=100, offset=0, channels=None) -> list:
        if not self._conn:
            return []
        where, params = self._channel_sql(channels)
        with self._lock:
            try:
                cur = self._conn.execute(
                    'SELECT id, ts, source, freq, text, duration, bus '
                    'FROM transcriptions ' + (f'WHERE {where} ' if where else '') +
                    'ORDER BY ts DESC LIMIT ? OFFSET ?',
                    params + (limit, offset))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception:
                return []

    def get_hourly_stats(self, days: int = 30, channels=None) -> dict:
        """Per-hour transmission counts for the `days` ending at the newest row.

        The window is anchored on the newest stored row, not on the clock, so
        a log that stopped being written months ago still charts its last
        `days` of traffic instead of an empty window.

        Returns {'newest', 'total', 'bucket', 'buckets': [[epoch, n, secs]]}
        with one entry per non-empty hour, oldest first.
        """
        empty = {'newest': 0, 'total': 0, 'bucket': 3600, 'buckets': []}
        if not self._conn:
            return empty
        days = max(1, min(366, int(days)))
        where, params = self._channel_sql(channels)
        and_where = f' AND {where}' if where else ''
        with self._lock:
            try:
                newest, total = self._conn.execute(
                    'SELECT MAX(ts), COUNT(*) FROM transcriptions' +
                    (f' WHERE {where}' if where else ''), params).fetchone()
                if not newest:
                    return empty
                cur = self._conn.execute(
                    'SELECT CAST(ts / 3600 AS INTEGER) AS h, COUNT(*), '
                    'COALESCE(SUM(duration), 0) FROM transcriptions '
                    'WHERE ts >= ?' + and_where + ' GROUP BY h ORDER BY h',
                    (newest - days * 86400,) + params)
                buckets = [[h * 3600, n, round(secs, 1)] for h, n, secs in cur.fetchall()]
                return {'newest': newest, 'total': total, 'bucket': 3600, 'buckets': buckets}
            except Exception:
                log_exc()
                return empty

    def search_keyword(self, query: str, limit: int = 200, channels=None) -> dict:
        """FTS5 keyword search. Returns {'results': [...], 'error': str|None}.

        FTS5 syntax is passed straight through, so callers get OR / AND / NEAR /
        phrase-quoting for free. An empty query returns no rows (rather than the
        full table) — keep get_recent() for that case.
        """
        if not self._conn:
            return {'results': [], 'error': 'log unavailable'}
        q = (query or '').strip()
        if not q:
            return {'results': [], 'error': None}
        limit = max(1, min(int(limit), 500))
        where, params = self._channel_sql(channels)
        with self._lock:
            try:
                cur = self._conn.execute(
                    'SELECT id, ts, source, freq, text, duration, bus '
                    'FROM transcriptions WHERE id IN ('
                    '  SELECT rowid FROM transcriptions_fts WHERE text MATCH ?'
                    ')' + (f' AND {where}' if where else '') + ' ORDER BY ts DESC LIMIT ?',
                    (q,) + params + (limit,))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                return {'results': rows, 'error': None}
            except sqlite3.Error as e:
                # FTS5 throws on malformed match expressions (unbalanced quotes,
                # bare punctuation, etc.). Surface as a user-facing error rather
                # than 500ing the request.
                return {'results': [], 'error': f'invalid query: {e}'}

    def _get_context(self) -> dict:
        with self._lock:
            try:
                cur = self._conn.execute(
                    'SELECT COUNT(*), MIN(ts), MAX(ts) FROM transcriptions')
                count, min_ts, max_ts = cur.fetchone()
                return {'count': count or 0, 'min_ts': min_ts, 'max_ts': max_ts}
            except Exception:
                return {'count': 0, 'min_ts': None, 'max_ts': None}

    # ------------------------------------------------------------------
    # NL query

    def query(self, question: str) -> dict:
        if not self._conn:
            return {'error': 'Transcription log is not available.'}
        if not self._claude_bin:
            return {'error': 'Claude CLI not found. Install Claude Code.'}
        try:
            return self._do_query(question.strip())
        except Exception as e:
            return {'error': str(e)}

    def _do_query(self, question: str) -> dict:
        ctx = self._get_context()
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')

        if ctx['min_ts']:
            range_str = (
                f"Earliest record: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ctx['min_ts']))}. "
                f"Latest record: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ctx['max_ts']))}."
            )
        else:
            range_str = 'The database is empty — no records exist yet.'

        sql_prompt = (
            f'{_SQL_SYSTEM}\n'
            f'Current datetime: {now_str}\n'
            f'Row count: {ctx["count"]}. {range_str}\n\n'
            f'Question: {question}'
        )

        try:
            r1 = subprocess.run(
                [self._claude_bin, '-p', sql_prompt],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {'error': 'Query timed out. Try a simpler question.'}
        except Exception as e:
            return {'error': f'Claude CLI error: {e}'}

        if r1.returncode != 0 or not r1.stdout.strip():
            return {'error': 'Query timed out. Try a simpler question.'}

        raw_sql = r1.stdout.strip()
        # Strip markdown fences if Claude wrapped them despite instructions
        if '```' in raw_sql:
            raw_sql = '\n'.join(
                ln for ln in raw_sql.splitlines() if not ln.startswith('```')
            ).strip()

        first = raw_sql.split()[0].upper() if raw_sql.split() else ''
        if first != 'SELECT':
            return {'error': "Your question couldn't be translated to a valid query. Try rephrasing."}

        with self._lock:
            try:
                cur = self._conn.execute(raw_sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
            except sqlite3.Error:
                return {'error': "Your question couldn't be translated to a valid query. Try rephrasing."}

        if not rows:
            return {'answer': 'No transmissions found matching that question.'}

        rows_text = '\n'.join(
            str(dict(zip(cols, row))) for row in rows[:50]
        )

        answer_prompt = (
            f'{_ANSWER_SYSTEM}\n\n'
            f'User question: "{question}"\n\n'
            f'The log returned {len(rows)} transmission(s):\n{rows_text}'
        )

        try:
            r2 = subprocess.run(
                [self._claude_bin, '-p', answer_prompt],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {'error': 'Query timed out. Try a simpler question.'}
        except Exception as e:
            return {'error': f'Claude CLI error: {e}'}

        if r2.returncode != 0 or not r2.stdout.strip():
            return {'error': 'Query timed out. Try a simpler question.'}

        return {'answer': r2.stdout.strip()}
