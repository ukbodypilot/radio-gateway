"""Offline FCC ULS amateur licence lookup: callsign -> status, name, city, state, class.

The FCC publishes the whole amateur database weekly as one public ZIP
(~200 MB, no key, no rate limit). It is read member-by-member straight from
the ZIP, never extracted (the .dat files are ~730 MB), and boiled down to a
small SQLite table (~100 MB, 1.6M calls, ~820k active).

Refresh: a background thread checks daily and, when the table is missing or
older than REFRESH_DAYS, runs `python -m speaker_id.uls refresh <db>` as a
CHILD process at nice 19. The build holds ~1.7M dict entries; inside the
gateway that memory would never go back to the OS, in a child it is freed on
exit. Every network call has a timeout, the child has a wall-clock limit, and
a failed refresh leaves the old table in place. Until the
first build finishes, lookups return None and callsign evidence is not
filed (structure alone is too weak, see callsign.py).
"""
import io
import os
import subprocess
import sys
import sqlite3
import threading
import time
import urllib.request
import zipfile

try:
    from log_util import log_exc
except Exception:  # pragma: no cover
    def log_exc(*_a, **_k):
        pass

URL = 'https://data.fcc.gov/download/pub/uls/complete/l_amat.zip'
REFRESH_DAYS = 7
CHUNK = 1 << 20


def default_path():
    return os.path.expanduser('~/.config/radio-gateway/uls.db')


def build_from_zip(zip_path, db_path):
    """Build the lookup table from an l_amat.zip. Returns (calls, active)."""
    status, klass = {}, {}
    with zipfile.ZipFile(zip_path) as z:
        def lines(member):
            with z.open(member) as f:
                for raw in io.TextIOWrapper(f, encoding='latin-1', newline=''):
                    yield raw.split('|')
        for f in lines('HD.dat'):            # licence header: status A=active
            if len(f) > 5:
                status[f[1]] = f[5]
        for f in lines('AM.dat'):            # amateur record: operator class
            if len(f) > 5:
                klass[f[1]] = f[5]
        tmp = db_path + '.building'
        if os.path.exists(tmp):
            os.unlink(tmp)
        db = sqlite3.connect(tmp)
        db.execute('CREATE TABLE calls(call TEXT PRIMARY KEY, status TEXT, name TEXT, '
                   'city TEXT, state TEXT, class TEXT)')
        rows = {}
        for f in lines('EN.dat'):            # entity: name and address
            if len(f) < 18:
                continue
            usi, call = f[1], f[4]
            st = status.get(usi, '')
            name = ' '.join(x for x in (f[8], f[10]) if x) or f[7]
            r = (call, st, name.title(), f[16].title(), f[17], klass.get(usi, ''))
            if call not in rows or st == 'A':      # prefer the active licence
                rows[call] = r
        db.executemany('INSERT INTO calls VALUES (?,?,?,?,?,?)', rows.values())
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('built', ?)", (str(time.time()),))
        db.commit()
        active = db.execute("SELECT COUNT(*) FROM calls WHERE status='A'").fetchone()[0]
        db.close()
    os.replace(tmp, db_path)
    return len(rows), active


class UlsLookup:
    def __init__(self, path=None, auto_refresh=True):
        self.path = path or default_path()
        self._conn = None
        self._lock = threading.Lock()
        self._cache = {}
        self.state = 'missing'
        self.last_error = ''
        self.built = 0.0
        self._stop = threading.Event()
        self._open()
        if auto_refresh:
            threading.Thread(target=self._refresh_loop, name='ULS-refresh', daemon=True).start()

    def _open(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
            self._cache.clear()
            if not os.path.exists(self.path):
                return
            try:
                c = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True, check_same_thread=False)
                r = c.execute("SELECT value FROM meta WHERE key='built'").fetchone()
                self.built = float(r[0]) if r else os.path.getmtime(self.path)
                self._conn = c
                self.state = 'ready'
            except sqlite3.Error as e:
                self.last_error = str(e)
                self.state = 'error'

    def ready(self):
        return self._conn is not None

    def get(self, call):
        """(status, name, city, state, class) or None."""
        call = (call or '').upper()
        if call in self._cache:
            return self._cache[call]
        with self._lock:
            if not self._conn:
                return None
            r = self._conn.execute('SELECT status, name, city, state, class FROM calls WHERE call=?',
                                   (call,)).fetchone()
        if len(self._cache) > 50_000:
            self._cache.clear()
        self._cache[call] = r
        return r

    def valid(self, call):
        return self.get(call) is not None

    def first_name(self, call):
        r = self.get(call)
        return r[1].split()[0] if r and r[1] else ''

    def stop(self):
        self._stop.set()

    # ── weekly refresh ──────────────────────────────────────────────────
    def _stale(self):
        return not self.ready() or time.time() - self.built > REFRESH_DAYS * 86400

    def _refresh_loop(self):
        try:
            os.nice(19)
        except Exception:
            log_exc()
        delay = 60          # let the gateway finish starting first
        while not self._stop.wait(delay):
            delay = 86400
            if not self._stale():
                continue
            try:
                self.refresh_in_child()
            except Exception as e:
                self.last_error = f'{type(e).__name__}: {e}'
                self.state = 'ready' if self.ready() else 'error'
                print(f'  [ULS] refresh failed: {self.last_error}')
                delay = 6 * 3600

    def refresh_in_child(self, timeout=7200):
        self.state = 'refreshing'
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(['nice', '-n', '19', sys.executable, '-m', 'speaker_id.uls',
                            'refresh', self.path],
                           cwd=root, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip().splitlines()
        if r.returncode != 0:
            raise RuntimeError(out[-1] if out else f'exit {r.returncode}')
        self._open()
        self.last_error = ''
        print(f'  [ULS] {out[-1] if out else "refreshed"}')

    def refresh(self):
        self.state = 'downloading'
        part = self.path + '.zip.part'
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        req = urllib.request.Request(URL, headers={'User-Agent': 'radio-gateway'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(part, 'wb') as out:
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    out.write(b)
                    if time.time() - t0 > 3600:
                        raise TimeoutError('download took over an hour')
                    if self._stop.is_set():
                        raise InterruptedError('stopping')
            self.state = 'building'
            n, active = build_from_zip(part, self.path)
        finally:
            if os.path.exists(part):
                os.unlink(part)
        self._open()
        print(f'callsign table built: {n} calls, {active} active ({time.time()-t0:.0f}s)')

    def get_status(self):
        return {'state': self.state, 'built': self.built or None, 'last_error': self.last_error}


if __name__ == '__main__':
    # python -m speaker_id.uls refresh <db>      download + build (child process)
    # python -m speaker_id.uls build <zip> <db>  build from a downloaded zip
    if len(sys.argv) >= 3 and sys.argv[1] == 'refresh':
        UlsLookup(sys.argv[2], auto_refresh=False).refresh()
    elif len(sys.argv) >= 4 and sys.argv[1] == 'build':
        n, a = build_from_zip(sys.argv[2], sys.argv[3])
        print(f'callsign table built: {n} calls, {a} active')
    else:
        sys.exit(__doc__)
