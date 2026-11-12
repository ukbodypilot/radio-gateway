"""Net Player: slot persistence, file play/seek, stream play, failure paths, HTTP handlers.

Uses real ffmpeg (tone files, a local realtime MP3 stream) so the decode and
reader-thread paths are exercised for real, not mocked. Settings go to a temp
dir; nothing touches ~/.config.
"""
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from _tmpdirs import mkdtemp  # noqa: E402
import audio_sources  # noqa: E402
import web_routes_audio  # noqa: E402
from sources import netplayer as npmod  # noqa: E402

FAIL = []
RATE = 48000
CHUNK = 2400                    # 50 ms
CHUNK_BYTES = CHUNK * 2


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def make_gw(tmp):
    cfg = types.SimpleNamespace(AUDIO_CHANNELS=1, AUDIO_RATE=RATE,
                                PLAYBACK_DIRECTORY=os.path.join(tmp, 'audio'))
    return types.SimpleNamespace(config=cfg, playback_source=None)


def make_src(tmp):
    gw = make_gw(tmp)
    src = audio_sources.NetPlayerSource(gw, settings_path=os.path.join(tmp, 'np.json'))
    gw.netplayer_source = src
    return gw, src


def tone(path, secs, freq=440):
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', f'sine=frequency={freq}:duration={secs}', '-ac', '1', path],
                   check=True, timeout=60)


def wait_for(cond, secs=10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class FakeHandler:
    def __init__(self, body=b'', path='/netplayer', length=None):
        self.headers = {'Content-Length': str(len(body) if length is None else length)}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.path = path
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, *_):
        pass

    def end_headers(self):
        pass

    def reply(self):
        return json.loads(self.wfile.getvalue().decode())


tmp = mkdtemp('netplayer')
net_dir = os.path.join(tmp, 'audio', 'net')
os.makedirs(net_dir)

print("\n1. URL and filename validation")
for url, ok in (('http://example.com/s.mp3', True), ('https://a.b:8000/x?y=1', True),
                ('file:///etc/passwd', False), ('concat:a|b', False), ('ftp://x/y', False),
                ('http://', False), ('http://a b/c', False), ('', False), (None, False),
                ('http://x/' + 'a' * 2100, False), ('-i http://x', False)):
    check(f"valid_stream_url({str(url)[:30]!r}) is {ok}", npmod.valid_stream_url(url) is ok)
for raw, want in (('intro.mp3', 'intro.mp3'), ('../../etc/passwd.mp3', 'passwd.mp3'),
                  ('a/b\\c.WAV', 'c.WAV'), ('.hidden.mp3', 'hidden.mp3'), ('notes.txt', ''),
                  ('', ''), ('we ird$name!.mp3', 'we ird_name_.mp3')):
    check(f"safe_filename({raw!r}) -> {want!r}", npmod.safe_filename(raw) == want,
          repr(npmod.safe_filename(raw)))

print("\n2. Slot settings persist and validate")
gw, src = make_src(tmp)
check("fresh settings have 5 + 5 empty slots",
      len(src._settings['files']) == 5 and len(src._settings['streams']) == 5)
ok, err = src.configure('file', 1, 'intro.mp3', 'Preamble')
check("assign file slot", ok, err)
ok, err = src.configure('stream', 3, 'http://127.0.0.1:1/x.mp3', 'Other net')
check("assign stream slot", ok, err)
check("reject file slot 6", not src.configure('file', 6, 'a.mp3')[0])
check("reject stream slot 0", not src.configure('stream', 0, 'http://x/y')[0])
check("reject non-audio file", not src.configure('file', 2, 'notes.txt')[0])
check("reject file: stream URL", not src.configure('stream', 2, 'file:///etc/passwd')[0])
check("reject bad kind", not src.configure('video', 1, 'x')[0])
gw2, src2 = make_src(tmp)           # a "restart": new object, same settings file
check("file assignment survives a restart",
      src2._settings['files'][0] == {'name': 'intro.mp3', 'label': 'Preamble'})
check("stream assignment survives a restart",
      src2._settings['streams'][2] == {'url': 'http://127.0.0.1:1/x.mp3', 'label': 'Other net'})
src2.configure('file', 1, '')
check("empty value clears a slot", src2._settings['files'][0]['name'] == '')
with open(os.path.join(tmp, 'np.json'), 'w') as f:
    f.write('{ torn')
check("a torn settings file loads as blanks", npmod.load_settings(os.path.join(tmp, 'np.json'))['files'][0]['name'] == '')

print("\n3. File playback, seek, end of file")
gw, src = make_src(tmp)
tone(os.path.join(net_dir, 'intro.mp3'), 3)
src.configure('file', 1, 'intro.mp3')
ok, err = src.play('file', 1)
check("play file 1", ok, err)
st = src.state()
check("active is file 1", st['active']['kind'] == 'file' and st['active']['slot'] == 1)
check("duration is ~3 s", st['files'][0]['duration'] is not None and 2.5 < st['files'][0]['duration'] < 3.6,
      str(st['files'][0]['duration']))
out, ptt = src.get_audio(CHUNK)
check("full chunk and keys PTT", len(out) == CHUNK_BYTES and ptt is True)
check("is_active while playing", src.is_active())
check("audio_level moves", src.audio_level > 0, str(src.audio_level))
p0 = src.state()['files'][0]['position']
ok, err = src.seek(position=2.0)
check("absolute seek", ok and abs(src.state()['files'][0]['position'] - 2.0) < 0.1, err)
src.seek(delta=-1.0)
check("relative seek back", abs(src.state()['files'][0]['position'] - 1.0) < 0.1)
src.seek(delta=-100)
check("seek clamps at the start", src.state()['files'][0]['position'] == 0.0)
src.seek(position=9999)
check("seek clamps before the end", src.state()['files'][0]['position'] <= src.state()['files'][0]['duration'])
check("bad seek value rejected", not src.seek(position='abc')[0])
chunks = 0
while src.is_active() and chunks < 200:
    out, _ = src.get_audio(CHUNK)
    check("every chunk is whole-frame sized", len(out) == CHUNK_BYTES) if chunks == 0 else None
    chunks += 1
check("playing past the end returns to idle", src.state()['active'] is None and not src.is_active())
check("silent once finished", src.get_audio(CHUNK)[0] is None)
check("seek with nothing playing is refused", not src.seek(position=1)[0])
src.play('file', 1)
src.stop()
check("stop returns to idle", src.state()['active'] is None)
check("play of an empty slot is refused", not src.play('file', 2)[0])
src.configure('file', 4, 'missing.mp3')
ok, err = src.play('file', 4)
check("play of a missing file is refused with a reason", not ok and 'not found' in err, err)
check("...and the error is reported in state", 'not found' in src.state()['error'])
open(os.path.join(net_dir, 'broken.mp3'), 'wb').write(b'this is not audio')
src.configure('file', 5, 'broken.mp3')
ok, err = src.play('file', 5)
check("undecodable file is refused", not ok and 'decode' in err, err)
src.play('file', 1)
src.configure('file', 1, 'intro.mp3', 'relabelled')
check("relabel does not stop the playing slot", src.state()['active'] is not None)
src.configure('file', 1, '')
check("clearing the playing slot stops it", src.state()['active'] is None)
src.stop()

print("\n4. Stream playback")
mp3 = os.path.join(tmp, 'live.mp3')
tone(mp3, 40, 330)
mp3_bytes = open(mp3, 'rb').read()


class Live(http.server.BaseHTTPRequestHandler):
    """Serves MP3 at about real time, like an Icecast mount."""
    mode = 'live'

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.end_headers()
        try:
            if Live.mode == 'stall':
                self.wfile.write(mp3_bytes[:6000])
                self.wfile.flush()
                time.sleep(60)
                return
            rate = 16000                      # bytes/s of a 128 kbps stream
            for i in range(0, len(mp3_bytes), 1600):
                self.wfile.write(mp3_bytes[i:i + 1600])
                self.wfile.flush()
                time.sleep(1600 / rate)
        except (BrokenPipeError, ConnectionResetError):
            pass


httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Live)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{httpd.server_address[1]}/live.mp3'

gw, src = make_src(tmp)
src.configure('stream', 1, url, 'Local')
ok, err = src.play('stream', 1)
check("play stream 1", ok, err)
check("state is connecting/playing, not idle", src.state()['streams'][0]['state'] in ('connecting', 'playing'))
check("silent until prefilled, then audio arrives",
      wait_for(lambda: (src.get_audio(CHUNK)[0] is not None) or False, 15))
got = 0
end = time.monotonic() + 3
while time.monotonic() < end:
    out, ptt = src.get_audio(CHUNK)
    if out is not None:
        got += 1
        if len(out) != CHUNK_BYTES:
            check("stream chunks are always full size", False, str(len(out)))
            break
    time.sleep(0.05)
check("stream keeps delivering audio", got > 20, str(got))
check("stream is_active once primed", src.is_active())
check("stream state is playing", src.state()['streams'][0]['state'] == 'playing')
proc = src._proc
src.stop()
check("stop kills ffmpeg", wait_for(lambda: proc.poll() is not None, 5))
check("stop returns to idle", src.state()['active'] is None)
check("seek is refused for a stream", not src.seek(position=1)[0])

print("\n4b. An UNROUTED stream is not mistaken for a dead one")
# Nothing calls get_audio until the node is wired to a bus. The reader then
# blocks on a full buffer, which the stall watchdog used to read as a dead
# stream and kill (seen live: dropped 16 s after start, routed 27 s after).
_stall, _conn = npmod.STREAM_STALL_TIMEOUT, npmod.STREAM_CONNECT_TIMEOUT
npmod.STREAM_STALL_TIMEOUT, npmod.STREAM_CONNECT_TIMEOUT = 2.0, 20.0
Live.mode = 'live'
src.play('stream', 1)
check("unrouted: buffer fills to its cap", wait_for(lambda: len(src._buf) > src._max_buf_bytes * 0.9, 15))
time.sleep(4)                                   # well past the 2 s stall timeout
check("unrouted stream survives past the stall timeout", src.state()['active'] is not None, src.state()['error'])
check("...and is reported as not routed", src.state()['active']['routed'] is False)
out, _ = src.get_audio(CHUNK)
check("routing it afterwards delivers audio at once", out is not None and len(out) == CHUNK_BYTES)
check("...and it is now reported as routed", src.state()['active']['routed'] is True)
src.stop()
npmod.STREAM_STALL_TIMEOUT, npmod.STREAM_CONNECT_TIMEOUT = _stall, _conn

print("\n5. Stream failure paths")
src.configure('stream', 2, 'http://127.0.0.1:1/nothing.mp3')
src.play('stream', 2)
check("refused connection ends in an error, not a hang",
      wait_for(lambda: src.state()['active'] is None, 20))
check("...with a reason", 'stream 2' in src.state()['error'], src.state()['error'])
src.stop()
check("a user stop clears the old error", src.state()['error'] == '')

Live.mode = 'stall'
npmod.STREAM_STALL_TIMEOUT = 2.0
npmod.STREAM_CONNECT_TIMEOUT = 6.0
src.play('stream', 1)
wait_for(lambda: src.get_audio(CHUNK)[0] is not None, 10)
# Drain what was buffered, then the supply stops and the stall watchdog fires.
end = time.monotonic() + 15
while time.monotonic() < end and src.state()['active'] is not None:
    src.get_audio(CHUNK)
    time.sleep(0.02)
check("a stalled stream is abandoned", src.state()['active'] is None)
check("...and says why", 'stopped arriving' in src.state()['error'] or 'timed out' in src.state()['error'],
      src.state()['error'])
src.stop()
src.configure('stream', 4, 'http://127.0.0.1:1/x')
Live.mode = 'live'

print("\n6. Starting one item replaces another")
src.configure('file', 1, 'intro.mp3')
src.play('file', 1)
src.play('stream', 1)
check("stream replaced the file", src.state()['active']['kind'] == 'stream')
proc = src._proc
src.play('file', 1)
check("file replaced the stream", src.state()['active']['kind'] == 'file')
check("...and the stream's ffmpeg was killed", wait_for(lambda: proc.poll() is not None, 5))
src.toggle('file', 1)
check("toggle on the playing slot stops it", src.state()['active'] is None)
src.cleanup()

print("\n7. HTTP handlers")
gw, src = make_src(tmp)
parent = types.SimpleNamespace(gateway=gw)


def call(body):
    h = FakeHandler(json.dumps(body).encode())
    web_routes_audio.handle_netplayer(h, parent)
    return h.reply()


r = call({})
check("status works with no body fields", r['ok'] and len(r['netplayer']['files']) == 5)
r = call({'action': 'set', 'kind': 'file', 'slot': 1, 'value': 'intro.mp3', 'label': 'Intro'})
check("set via HTTP", r['ok'] and r['netplayer']['files'][0]['label'] == 'Intro', str(r.get('error')))
check("available_files lists the net folder", 'intro.mp3' in r['available_files'])
r = call({'action': 'play', 'kind': 'file', 'slot': 1})
check("play via HTTP", r['ok'] and r['netplayer']['active']['kind'] == 'file')
r = call({'action': 'seek', 'position': 1.5})
check("seek via HTTP", r['ok'])
r = call({'action': 'stop'})
check("stop via HTTP", r['ok'] and r['netplayer']['active'] is None)
r = call({'action': 'play', 'kind': 'file', 'slot': 3})
check("playing an empty slot reports an error", not r['ok'] and r['error'])
r = call({'action': 'explode'})
check("unknown action is an error", not r['ok'])
h = FakeHandler(b'not json')
web_routes_audio.handle_netplayer(h, parent)
check("garbage body does not crash", h.reply()['ok'] is True)
h = FakeHandler(b'')
web_routes_audio.handle_netplayer(h, types.SimpleNamespace(gateway=types.SimpleNamespace()))
check("no netplayer source -> clean error", h.reply()['ok'] is False)

up = open(mp3, 'rb').read()[:5000]
h = FakeHandler(up, path='/netplayer/upload?name=net%20intro.mp3')
web_routes_audio.handle_netplayer_upload(h, parent)
r = h.reply()
check("upload saves a file", r['ok'] and r['name'] == 'net intro.mp3'
      and os.path.getsize(os.path.join(net_dir, 'net intro.mp3')) == 5000, str(r))
check("...and lists it", 'net intro.mp3' in r['available_files'])
check("...with no .partial left behind", not [f for f in os.listdir(net_dir) if f.endswith('.partial')])
h = FakeHandler(up, path='/netplayer/upload?name=..%2F..%2Fevil.mp3')
web_routes_audio.handle_netplayer_upload(h, parent)
check("path traversal lands inside the net folder",
      h.reply()['name'] == 'evil.mp3' and os.path.exists(os.path.join(net_dir, 'evil.mp3'))
      and not os.path.exists(os.path.join(tmp, 'evil.mp3')))
h = FakeHandler(up, path='/netplayer/upload?name=x.txt')
web_routes_audio.handle_netplayer_upload(h, parent)
check("non-audio name rejected", h.code == 400 and not h.reply()['ok'])
h = FakeHandler(b'', path='/netplayer/upload?name=a.mp3')
web_routes_audio.handle_netplayer_upload(h, parent)
check("empty upload rejected", h.code == 413)
h = FakeHandler(b'x', path='/netplayer/upload?name=a.mp3', length=npmod.MAX_UPLOAD_BYTES + 1)
web_routes_audio.handle_netplayer_upload(h, parent)
check("oversize upload rejected before reading", h.code == 413)
h = FakeHandler(b'short', path='/netplayer/upload?name=trunc.mp3', length=500)
web_routes_audio.handle_netplayer_upload(h, parent)
check("a dropped upload leaves no file", h.code == 500
      and not os.path.exists(os.path.join(net_dir, 'trunc.mp3'))
      and not os.path.exists(os.path.join(net_dir, 'trunc.mp3.partial')))

httpd.shutdown()
print()
if FAIL:
    print(f"FAILED: {len(FAIL)}")
    sys.exit(1)
print("ALL PASSED")
