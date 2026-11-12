"""Net Player: pre-recorded net introductions and network audio streams.

A ham net often opens with the same prepared material — preamble, rules,
check-in procedure — before going to open discussion. This source plays that
material so the net control operator does not have to read it live.

It is its own routing node (`netplayer`), like BGM and the Announcer, so it
goes wherever the operator wires it in /routing.

  * 5 FILE slots   — start / stop / seek. Files live in `<audio dir>/net/`.
  * 5 STREAM slots — start / stop. http(s) URLs (Icecast, HLS, plain MP3...).

One item plays at a time: there is a single audio output, and starting
another item swaps rather than stacks. Slot assignments persist to
~/.config/radio-gateway/netplayer.json.

Decoding is done by ffmpeg for both kinds. A file is decoded whole into PCM
(so seeking is exact and costs nothing); a stream is decoded continuously
into a small bounded buffer by a reader thread.
"""

import collections
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

import numpy as np

from atomic_json import save_json
from audio_util import pcm_level
from log_util import log_exc
from sources.base import AudioSource

FILE_SLOTS = 5
STREAM_SLOTS = 5

AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.m4a', '.aac')
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_FILE_SECS = 1200            # decode cap: 20 min of 48 kHz mono is ~115 MB
DECODE_TIMEOUT = 120.0

STREAM_PRIME_SECS = 0.6         # buffered before output starts
STREAM_MAX_BUF_SECS = 4.0       # reader blocks (backpressure) beyond this
STREAM_CONNECT_TIMEOUT = 20.0   # no audio at all by now -> give up
STREAM_STALL_TIMEOUT = 15.0     # audio stopped arriving -> give up

_PATH = os.path.expanduser('~/.config/radio-gateway/netplayer.json')


# ── Persistence ─────────────────────────────────────────────────────────────

def _blank():
    return {
        'files': [{'name': '', 'label': ''} for _ in range(FILE_SLOTS)],
        'streams': [{'url': '', 'label': ''} for _ in range(STREAM_SLOTS)],
    }


def load_settings(path=None):
    """Persisted slot assignments merged over blanks. Never raises."""
    st = _blank()
    try:
        with open(path or _PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return st
    if not isinstance(data, dict):
        return st
    for key, field, count in (('files', 'name', FILE_SLOTS), ('streams', 'url', STREAM_SLOTS)):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for i, row in enumerate(rows[:count]):
            if isinstance(row, dict):
                st[key][i] = {field: str(row.get(field) or ''),
                              'label': str(row.get('label') or '')[:40]}
    return st


def save_settings(st, path=None):
    p = path or _PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    save_json(p, st)


def valid_stream_url(url):
    """True for a plain http(s) URL. Nothing else is ever handed to ffmpeg:
    file:, concat:, subfile: etc. would let a URL read local files."""
    if not isinstance(url, str) or not url or len(url) > 2000:
        return False
    if re.search(r'[\s\x00-\x1f\x7f]', url):
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme in ('http', 'https') and bool(u.netloc)


def safe_filename(name):
    """A bare, boring filename or ''. No directories, no dotfiles."""
    base = os.path.basename(str(name or '').replace('\\', '/'))
    base = re.sub(r'[^A-Za-z0-9._ -]', '_', base).strip(' .')
    if not base or not base.lower().endswith(AUDIO_EXTS):
        return ''
    return base[:120]


# ── Source ──────────────────────────────────────────────────────────────────

class NetPlayerSource(AudioSource):

    def __init__(self, gateway, settings_path=None):
        super().__init__("NetPlayer", gateway.config)
        self.gateway = gateway
        self.priority = 2
        self.ptt_control = True
        self.volume = 1.0
        self.audio_level = 0
        self._settings_path = settings_path
        self._settings = load_settings(settings_path)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)   # stream buffer, shares _lock

        self._kind = None        # None | 'file' | 'stream'
        self._slot = None
        # file
        self._pcm = None
        self._pos = 0
        # stream
        self._gen = 0            # bumped on every start/stop; stale readers exit
        self._proc = None
        self._buf = bytearray()
        self._primed = False
        self._started_at = 0.0
        self._last_data = 0.0
        self._stream_state = 'idle'
        self._stream_bytes = 0
        self._underruns = 0
        self._stderr = collections.deque(maxlen=5)
        self._last_pull = 0.0    # last time a bus asked for audio

        self._last_error = ''

    # ── geometry ────────────────────────────────────────────────────────────

    @property
    def _rate(self):
        try:
            return int(getattr(self.config, 'AUDIO_RATE', 48000) or 48000)
        except (TypeError, ValueError):
            return 48000

    @property
    def _channels(self):
        try:
            return max(1, int(getattr(self.config, 'AUDIO_CHANNELS', 1) or 1))
        except (TypeError, ValueError):
            return 1

    @property
    def _frame(self):
        return 2 * self._channels

    @property
    def _bytes_per_sec(self):
        return self._rate * self._frame

    @property
    def _max_buf_bytes(self):
        return int(STREAM_MAX_BUF_SECS * self._bytes_per_sec)

    @property
    def audio_dir(self):
        ps = getattr(self.gateway, 'playback_source', None)
        base = (getattr(ps, 'announcement_directory', None)
                or getattr(self.config, 'PLAYBACK_DIRECTORY', './audio/'))
        return os.path.join(base, 'net')

    def file_path(self, name):
        n = safe_filename(name)
        return os.path.join(self.audio_dir, n) if n else ''

    def list_files(self):
        """Audio files available to assign to a slot."""
        try:
            return sorted(f for f in os.listdir(self.audio_dir)
                          if f.lower().endswith(AUDIO_EXTS) and not f.startswith('.'))
        except OSError:
            return []

    # ── settings ────────────────────────────────────────────────────────────

    def configure(self, kind, slot, value, label=''):
        """Assign a file name / stream URL (and label) to a slot and persist.

        An empty value clears the slot. Returns (ok, error).
        """
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False, f'bad slot {slot!r}'
        if kind not in ('file', 'stream'):
            return False, f'bad kind {kind!r}'
        count = FILE_SLOTS if kind == 'file' else STREAM_SLOTS
        if not 1 <= slot <= count:
            return False, f'{kind} slot must be 1-{count}'
        value = str(value or '').strip()
        label = str(label or '').strip()[:40]
        if kind == 'file':
            if value:
                value = safe_filename(value)
                if not value:
                    return False, 'file must be an audio file name (mp3, wav, ogg, flac, m4a)'
            row = {'name': value, 'label': label}
        else:
            if value and not valid_stream_url(value):
                return False, 'stream must be an http:// or https:// URL'
            row = {'url': value, 'label': label}
        key = 'files' if kind == 'file' else 'streams'
        with self._lock:
            field = 'name' if kind == 'file' else 'url'
            changed = self._settings[key][slot - 1][field] != row[field]   # a relabel is not a change
            self._settings[key][slot - 1] = row
            snapshot = json.loads(json.dumps(self._settings))
            # The setup dialog saves every slot; only stop playback when the
            # slot that is playing actually changed.
            was_active = changed and self._kind == kind and self._slot == slot
        try:
            save_settings(snapshot, self._settings_path)
        except Exception as e:
            log_exc()
            return False, f'could not save settings: {e}'
        if was_active:
            self.stop()          # the thing playing is no longer what the slot says
        return True, ''

    # ── state for the UI ────────────────────────────────────────────────────

    def state(self):
        self._check_health()
        with self._lock:
            files, streams = [], []
            bps = self._bytes_per_sec
            for i, row in enumerate(self._settings['files'], 1):
                playing = self._kind == 'file' and self._slot == i
                files.append({
                    'slot': i, 'name': row['name'], 'label': row['label'],
                    'available': bool(row['name']) and os.path.exists(self.file_path(row['name'])),
                    'playing': playing,
                    'position': round(self._pos / bps, 1) if playing else 0.0,
                    'duration': round(len(self._pcm) / bps, 1) if playing and self._pcm else None,
                })
            for i, row in enumerate(self._settings['streams'], 1):
                playing = self._kind == 'stream' and self._slot == i
                streams.append({
                    'slot': i, 'url': row['url'], 'label': row['label'],
                    'playing': playing,
                    'state': self._stream_state if playing else 'idle',
                    'underruns': self._underruns if playing else 0,
                })
            now = time.monotonic()
            # Nothing pulls audio until the node is wired to a bus in /routing,
            # so a source can be 'playing' and silent. Say so (after a short
            # grace so a normal start does not flash the warning).
            routed = (not self._kind or now - self._started_at < 3.0
                      or now - self._last_pull < 2.0)
            return {
                'files': files, 'streams': streams,
                'active': ({'kind': self._kind, 'slot': self._slot, 'routed': routed}
                           if self._kind else None),
                'error': self._last_error,
            }

    # ── control ─────────────────────────────────────────────────────────────

    def play(self, kind, slot):
        """Start a slot, replacing whatever is playing. Returns (ok, error)."""
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False, f'bad slot {slot!r}'
        with self._lock:
            self._last_error = ''
            files, streams = self._settings['files'], self._settings['streams']
        if kind == 'file':
            if not 1 <= slot <= FILE_SLOTS or not files[slot - 1]['name']:
                return False, f'file slot {slot} is empty'
            return self._play_file(slot, files[slot - 1]['name'])
        if kind == 'stream':
            if not 1 <= slot <= STREAM_SLOTS or not streams[slot - 1]['url']:
                return False, f'stream slot {slot} is empty'
            return self._play_stream(slot, streams[slot - 1]['url'])
        return False, f'bad kind {kind!r}'

    def toggle(self, kind, slot):
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False, f'bad slot {slot!r}'
        with self._lock:
            same = self._kind == kind and self._slot == slot
        if same:
            self.stop()
            return True, ''
        return self.play(kind, slot)

    def _fail(self, msg):
        with self._lock:
            self._last_error = msg
        print(f"[NetPlayer] {msg}")
        return False, msg

    def _play_file(self, slot, name):
        path = self.file_path(name)
        if not path or not os.path.exists(path):
            return self._fail(f'{name} not found in {self.audio_dir}')
        # Decode OUTSIDE the lock — it can take seconds, and get_audio runs on
        # the bus thread. The previous item keeps playing until it is ready.
        pcm = self._decode(path)
        if not pcm:
            return self._fail(f'could not decode {name}')
        self.stop()
        with self._lock:
            self._kind, self._slot = 'file', slot
            self._pcm, self._pos = pcm, 0
            self._started_at = time.monotonic()
            self._last_pull = 0.0
        print(f"[NetPlayer] File {slot} started: {name} ({len(pcm) / self._bytes_per_sec:.0f}s)")
        return True, ''

    def _decode(self, path):
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-i', path, '-t', str(MAX_FILE_SECS), '-vn',
               '-ac', str(self._channels), '-ar', str(self._rate), '-f', 's16le', '-']
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[NetPlayer] ffmpeg failed: {e}")
            return None
        if r.returncode != 0 or not r.stdout:
            tail = r.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:]
            print(f"[NetPlayer] ffmpeg rc={r.returncode} {tail}")
            return None
        data = r.stdout
        return data[:len(data) - len(data) % self._frame]

    def _play_stream(self, slot, url):
        if not valid_stream_url(url):
            return self._fail('stream URL must be http:// or https://')
        self.stop()
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
               # Only network protocols: a hostile playlist must not be able
               # to pull in file: or other local-reading protocols.
               '-protocol_whitelist', 'http,https,tcp,tls,crypto',
               '-reconnect', '1', '-reconnect_streamed', '1',
               '-reconnect_delay_max', '5', '-rw_timeout', '15000000',
               '-i', url, '-vn',
               '-ac', str(self._channels), '-ar', str(self._rate), '-f', 's16le', '-']
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0)
        except OSError as e:
            return self._fail(f'could not start ffmpeg: {e}')
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._kind, self._slot = 'stream', slot
            self._proc = proc
            self._buf = bytearray()
            self._primed = False
            self._stream_state = 'connecting'
            self._started_at = time.monotonic()
            self._last_pull = 0.0
            self._last_data = 0.0
            self._stream_bytes = 0
            self._underruns = 0
            self._stderr.clear()
        threading.Thread(target=self._read_stdout, args=(gen, proc),
                         name='netplayer-read', daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(gen, proc),
                         name='netplayer-err', daemon=True).start()
        print(f"[NetPlayer] Stream {slot} connecting")
        return True, ''

    def _read_stdout(self, gen, proc):
        """Feed ffmpeg's PCM into the buffer. Exits as soon as it is stale."""
        max_buf = self._max_buf_bytes
        try:
            while True:
                data = proc.stdout.read(8192)     # bufsize=0: may return short
                if not data:
                    break
                with self._cv:
                    # Backpressure rather than dropping: a full buffer stalls
                    # ffmpeg, which stalls the TCP read.
                    while self._gen == gen and len(self._buf) > max_buf:
                        self._cv.wait(0.2)
                    if self._gen != gen:
                        return
                    self._buf += data
                    self._stream_bytes += len(data)
                    self._last_data = time.monotonic()
        except (OSError, ValueError):
            pass
        finally:
            with self._cv:
                if self._gen == gen and self._kind == 'stream':
                    tail = ' / '.join(self._stderr)
                    self._fail_stream_locked(tail or 'stream ended')

    def _drain_stderr(self, gen, proc):
        try:
            for line in proc.stderr:
                text = line.decode('utf-8', 'replace').strip()
                if text:
                    self._stderr.append(text[:200])
        except (OSError, ValueError):
            pass

    def _fail_stream_locked(self, msg):
        """Stop the stream and record why. Caller holds _lock."""
        self._last_error = f'stream {self._slot}: {msg}'
        print(f"[NetPlayer] {self._last_error}")
        self._reset_locked()

    def _reset_locked(self):
        """Return to idle and kill any ffmpeg. Caller holds _lock."""
        proc = self._proc
        self._gen += 1                    # orphan the reader threads
        self._kind = self._slot = None
        self._pcm, self._pos = None, 0
        self._proc = None
        self._buf = bytearray()
        self._primed = False
        self._stream_state = 'idle'
        self.audio_level = 0
        self._cv.notify_all()
        if proc is not None:
            threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    @staticmethod
    def _reap(proc):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            log_exc()
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass

    def stop(self):
        with self._lock:
            was = (self._kind, self._slot)
            self._reset_locked()
            self._last_error = ''         # a user stop acknowledges any old error
        if was[0]:
            print(f"[NetPlayer] {was[0].capitalize()} {was[1]} stopped")

    def seek(self, position=None, delta=None):
        """Move within the playing FILE. `position` is absolute seconds,
        `delta` relative. Returns (ok, error)."""
        with self._lock:
            if self._kind != 'file' or not self._pcm:
                return False, 'seek needs a file to be playing'
            bps, frame = self._bytes_per_sec, self._frame
            try:
                if position is not None:
                    target = float(position) * bps
                else:
                    target = self._pos + float(delta or 0) * bps
            except (TypeError, ValueError):
                return False, 'bad seek position'
            target = int(max(0, min(target, len(self._pcm) - frame)))
            self._pos = target - target % frame
        return True, ''

    def _check_health(self):
        """Give up on a stream that never connects or has gone quiet.

        Runs from get_audio (bus thread) and from state() (UI poll): neither
        depends on the reader thread, which is what would be wedged.
        """
        with self._lock:
            if self._kind != 'stream':
                return
            now = time.monotonic()
            if not self._primed and self._stream_bytes == 0:
                if now - self._started_at > STREAM_CONNECT_TIMEOUT:
                    self._fail_stream_locked('no audio received (connection timed out)')
            elif (self._last_data and now - self._last_data > STREAM_STALL_TIMEOUT
                  and len(self._buf) < self._max_buf_bytes // 2):
                # Only a stall if the buffer has ROOM. A full buffer means the
                # reader is blocked because nothing is pulling audio (the node is
                # not routed yet) - the stream is fine, so leave it be.
                self._fail_stream_locked('audio stopped arriving')

    # ── bus interface ───────────────────────────────────────────────────────

    def is_active(self):
        return self._kind == 'file' or (self._kind == 'stream' and self._primed)

    def get_audio(self, chunk_size):
        self._check_health()
        want = chunk_size * self._channels * 2
        finished = None
        with self._lock:
            self._last_pull = time.monotonic()
            if self._kind == 'file':
                pcm = self._pcm
                if not pcm:
                    return None, False
                out = pcm[self._pos:self._pos + want]
                self._pos += len(out)
                if self._pos >= len(pcm):
                    finished = self._slot
                    self._reset_locked()
                if len(out) < want:
                    out = out.ljust(want, b'\x00')
            elif self._kind == 'stream':
                if not self._primed:
                    if len(self._buf) < int(STREAM_PRIME_SECS * self._bytes_per_sec):
                        return None, False
                    self._primed = True
                    self._stream_state = 'playing'
                out = bytes(self._buf[:want])
                del self._buf[:len(out)]
                self._cv.notify_all()
                if len(out) < want:
                    # Transient underrun: pad and keep going. Un-priming here
                    # would make every network hiccup a half-second dropout.
                    self._underruns += 1
                    out = out.ljust(want, b'\x00')
            else:
                return None, False
            if self.volume != 1.0:
                arr = np.frombuffer(out, dtype=np.int16).astype(np.float32)
                out = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()
            self.audio_level = pcm_level(out, self.audio_level)
        if finished is not None:
            print(f"[NetPlayer] File {finished} finished")
        return out, True

    def cleanup(self):
        self.stop()
