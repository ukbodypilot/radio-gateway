import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id       INTEGER PRIMARY KEY,
    ts       REAL    NOT NULL,
    source   TEXT    NOT NULL DEFAULT '',
    freq     TEXT    NOT NULL DEFAULT '',
    text     TEXT    NOT NULL,
    duration REAL    NOT NULL DEFAULT 0.0
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
    duration REAL      -- audio clip duration in seconds
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
        if not self._conn:
            return
        with self._lock:
            try:
                self._conn.execute(
                    'INSERT INTO transcriptions(ts, source, freq, text, duration) '
                    'VALUES (?,?,?,?,?)',
                    (result['timestamp'],
                     result.get('source', ''),
                     result.get('freq', ''),
                     result['text'],
                     result.get('duration', 0.0))
                )
                self._conn.commit()
            except Exception as e:
                print(f'  [TxLog] insert error: {e}')

    def check_keywords(self, result: dict):
        raw = str(getattr(self._config, 'TRANSCRIPTION_ALERT_KEYWORDS', '') or '')
        keywords = [k.strip().lower() for k in raw.split(',') if k.strip()]
        if not keywords:
            return
        text_lower = result['text'].lower()
        for kw in keywords:
            if kw in text_lower:
                try:
                    freq = result.get('freq', '?')
                    ts = result.get('time_str', '?')
                    msg = (f"Transcription alert: [{kw}] heard on {freq} "
                           f"at {ts}: {result['text']}")
                    _data = json.dumps({'text': msg}).encode()
                    req = urllib.request.Request(
                        'http://127.0.0.1:8080/telegram_send', data=_data,
                        headers={'Content-Type': 'application/json'})
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass
                break

    # ------------------------------------------------------------------
    # Read path

    def get_recent(self, limit=100, offset=0) -> list:
        if not self._conn:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    'SELECT id, ts, source, freq, text, duration '
                    'FROM transcriptions ORDER BY ts DESC LIMIT ? OFFSET ?',
                    (limit, offset))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception:
                return []

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
