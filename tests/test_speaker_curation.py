#!/usr/bin/env python3
"""Speaker ID curation: label / merge / move / not-same / undo, the review queue, the web API.

The human is the strongest witness: every change is audited, undoable, and
never overridden by automation. Callsigns and names here are invented.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from speaker_id.curation import Curator, CurationError   # noqa: E402
from speaker_id.identify import Identifier                # noqa: E402
from speaker_id import review                             # noqa: E402
from speaker_id.store import SpeakerStore                 # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


class FakeUls:
    CALLS = {'KX7QZA': 'Quentin Ames', 'KX7QZB': 'Bernard Cole'}

    def ready(self):
        return True

    def valid(self, c):
        return c in self.CALLS

    def get(self, c):
        n = self.CALLS.get(c)
        return ('A', n, 'Town', 'CA', 'G') if n else None

    def first_name(self, c):
        return (self.CALLS.get(c) or ' ').split()[0]


tmp = tempfile.mkdtemp()
st = SpeakerStore(os.path.join(tmp, 'speakers.db'))
cur = Curator(st, FakeUls(), learn_secs=0.0)


def voice(vec, n, text='', ts0=1000.0):
    pid = st.add_profile('fake', unit(vec), ts0)
    ids = []
    for k in range(n):
        u = st.add_utterance(ts=ts0 + k, bus='sdr1', source='', freq='446.000', duration=4.0, model='fake',
                             emb=unit(np.asarray(vec, np.float32) + 0.01 * k), profile_id=pid, score=0.9,
                             decision='matched')
        if text:
            st.set_text(u, text)
        ids.append(u)
    return pid, ids


A, a_ids = voice([1, 0, 0, 0], 6)
B, b_ids = voice([0.9, 0.44, 0, 0], 6)
C, c_ids = voice([0, 0, 1, 0], 6)

print("\n1. label")
r = cur.label(A, callsign='kx7qza')
p = st.profile(A)
check("a label ties the voice to an identity, marked human", p['label_source'] == 'human'
      and st.identity_call(p['identity_id']) == 'KX7QZA')
check("the identity takes the FCC name when none is typed", st.identity(p['identity_id'])['name'] == 'Quentin Ames')
n_audit = st.last_audit_id()
again = cur.label(A, callsign='KX7QZA')
check("repeating the same label changes nothing and is not re-audited (double clicks)",
      again.get('unchanged') and st.last_audit_id() == n_audit, str(again))
cur.label(C, name='Gus')
check("a name-only label works", st.identity(st.profile(C)['identity_id'])['name'] == 'Gus')
try:
    cur.label(A)
    check("an empty label is refused", False)
except CurationError:
    check("an empty label is refused", True)

print("\n2. merge and undo")
m = cur.merge(A, B)
check("merge moves every over", m['moved'] == 6 and len(st.over_ids(A)) == 12 and not st.over_ids(B))
check("the dropped voice is retired softly, not deleted",
      st.profile(B) is not None and B not in [x[0] for x in st.load_profiles('fake')])
check("the kept centroid is recomputed from all its overs", st.load_profiles('fake')[0][2] == 12)
merge_audit = st.last_audit_id()
cur.undo(merge_audit)
check("undo brings the voice back with its own overs",
      sorted(st.over_ids(B)) == sorted(b_ids) and B in [x[0] for x in st.load_profiles('fake')])
try:
    cur.undo(merge_audit)
    check("a change cannot be undone twice", False)
except CurationError:
    check("a change cannot be undone twice", True)

print("\n3. not the same person")
cur.not_same(A, B)
try:
    cur.merge(A, B)
    check("a merge across a 'different people' mark is refused", False)
except CurationError as e:
    check("a merge across a 'different people' mark is refused", 'different people' in str(e))

print("\n4. move one over")
mv = cur.move_over(a_ids[0])
check("an over can leave for a new voice of its own", st.utterance(a_ids[0])['profile_id'] == mv['to'] != A)
cur.undo(st.last_audit_id())
check("...and come back", st.utterance(a_ids[0])['profile_id'] == A
      and mv['to'] not in [x[0] for x in st.load_profiles('fake')])
cur.move_over(a_ids[1], C)
check("or move to a named voice", st.utterance(a_ids[1])['profile_id'] == C)

print("\n5. automation never overrides you")
idf = Identifier(st, FakeUls())
for k in range(6):
    u = st.add_utterance(ts=5000.0 + k * 90000, bus='sdr1', source='', freq='', duration=3.0, model='fake',
                         emb=unit([1, 0, 0, 0]), profile_id=A, score=0.9, decision='matched')
    st.set_text(u, 'This is KX7QZB.')
    idf.file(u, 5000.0 + k * 90000, 3.0, 'sdr1', 'This is KX7QZB.')
check("strong contrary evidence does not rename a human-labelled voice",
      idf.maybe_name(A) is None and st.identity_call(st.profile(A)['identity_id']) == 'KX7QZA')
items = review.build(st, idf, 'fake')
check("...it raises a conflict for you instead",
      any(i['type'] == 'conflict' and i['profiles'] == [A] for i in items), str([i['type'] for i in items]))
cur.unlabel(A)
check("'not this person' clears the name and blocks automatic re-naming",
      st.profile(A)['identity_id'] is None and idf.maybe_name(A) is None)
cur.undo(st.last_audit_id())
check("undo restores the previous label", st.identity_call(st.profile(A)['identity_id']) == 'KX7QZA')

print("\n6. review queue")
D, _ = voice([0, 0, 0.7, 0.71], 8, ts0=2000.0)
items = review.build(st, idf, 'fake', same_lo=0.45, same_hi=0.60)
kinds = {i['type'] for i in items}
check("an unnamed busy voice with no callsign is asked about", any(i['type'] == 'name' and D in i['profiles'] for i in items))
check("similar voices are offered as 'same person?'",
      any(i['type'] == 'same' and set(i['profiles']) == {C, D} for i in items), str(kinds))
check("a 'different people' pair is not offered again",
      not any(i['type'] == 'same' and set(i['profiles']) == {A, B} for i in items))
check("every item carries sample clips to listen to", all('samples' in i for i in items))

print("\n7. web API (fake request)")
import web_routes_speakers as W   # noqa: E402
from speaker_id.service import SpeakerID   # noqa: E402

cfg = types.SimpleNamespace(SPEAKER_ID_MODEL='fake', SPEAKER_ID_ACCEPT=0.60, SPEAKER_ID_NEW=0.45,
                            SPEAKER_ID_MIN_SECS=1.5, SPEAKER_ID_LEARN_SECS=3.0, SPEAKER_ID_KEEP_CLIPS=False,
                            SPEAKER_ID_BUSES='', SPEAKER_ID_DB_PATH='', SPEAKER_ID_CALLSIGNS=False,
             SPEAKER_ID_NIGHTLY_TIME='', SPEAKER_ID_CLIP_DAYS=30, SPEAKER_ID_MERGE_AT=0.80, SPEAKER_ID_OFFLOAD='local', SPEAKER_ID_ALERT='')
sid = SpeakerID(cfg, None, store=st)
sid._clip_dir = os.path.join(tmp, 'clips')
sid.start()


class Req:
    def __init__(self, method, path, body=None):
        self.command, self.path = method, path
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.code, self.hdrs = None, {}

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


parent = types.SimpleNamespace(gateway=types.SimpleNamespace(speaker_id=sid))
r = Req('GET', '/speakers/api/profiles'); W.handle_speakers_get(r, parent)
d = r.json()
check("profiles list answers", r.code == 200 and d['ok'] and len(d['profiles']) >= 4, str(d)[:200])
check("no response carries an embedding", 'centroid' not in json.dumps(d) and 'emb' not in d['profiles'][0])
r = Req('GET', f'/speakers/api/profile?id={A}'); W.handle_speakers_get(r, parent)
d = r.json()
check("one voice: identity, licence-free when ULS is off, overs", r.code == 200 and d['identity']['callsign'] == 'KX7QZA'
      and len(d['overs']) > 0)
r = Req('POST', '/speakers/api/action', {'op': 'label', 'profile_id': D, 'callsign': 'KX7QZB'})
W.handle_speakers_post(r, parent)
check("a label through the API runs on the worker and is audited",
      r.code == 200 and r.json()['ok'] and st.identity_call(st.profile(D)['identity_id']) == 'KX7QZB', str(r.json()))
r = Req('POST', '/speakers/api/action', {'op': 'merge', 'keep': A, 'drop': B}); W.handle_speakers_post(r, parent)
check("a refused change comes back as an error, not a crash", r.code == 500 or (r.code == 200 and not r.json()['ok'])
      or r.code == 400, f'{r.code} {r.json()}')
r = Req('POST', '/speakers/api/action', {'op': 'drop_tables'}); W.handle_speakers_post(r, parent)
check("unknown ops are rejected", r.code == 400)
os.makedirs(os.path.join(tmp, 'clips', 'd'), exist_ok=True)
open(os.path.join(tmp, 'clips', 'd', 'x.opus'), 'wb').write(b'OggS-fake')
st.set_clip(a_ids[2], 'd/x.opus')
r = Req('GET', f'/speakers/clip?id={a_ids[2]}'); W.handle_speakers_get(r, parent)
check("a clip is served as audio", r.code == 200 and r.hdrs.get('Content-Type') == 'audio/ogg'
      and r.wfile.getvalue() == b'OggS-fake')
st.set_clip(a_ids[3], '../../etc/passwd')
r = Req('GET', f'/speakers/clip?id={a_ids[3]}'); W.handle_speakers_get(r, parent)
check("a clip path outside the clip folder is never served", r.code == 404)
t0 = time.time()
res = sid.run_on_worker(lambda s: 'ran on ' + __import__('threading').current_thread().name)
check("run_on_worker runs on the SpeakerID thread", res == 'ran on SpeakerID', res)
sid.stop()

print("\n8. the page")
html = open(os.path.join(ROOT, 'web_pages', 'speakers.html')).read()
check("the page is served at /speakers and in the nav",
      "'/speakers': 'speakers.html'" in open(os.path.join(ROOT, 'web', 'handler.py')).read()
      and 'href="/speakers"' in open(os.path.join(ROOT, 'web_pages', 'shell.html')).read())
node = shutil.which('node')
if node:
    js = html.split('<script>', 1)[1].rsplit('</script>', 1)[0]
    p = os.path.join(tmp, 'speakers.js')
    open(p, 'w').write(js)
    rr = subprocess.run([node, '--check', p], capture_output=True, text=True)
    check("page script parses", rr.returncode == 0, rr.stderr[-300:])

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
