#!/usr/bin/env python3
"""Speaker ID learning loop: nightly re-cluster, clip retention, backup, model swap.

The re-cluster may only do what it is sure of: different people's average
voices reached cosine 0.76 through one repeater (2026-11-15), so it re-assigns
at 0.70, auto-merges only at 0.80+, and never undoes a human decision.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_id import recluster, retention   # noqa: E402
from speaker_id.curation import Curator        # noqa: E402
from speaker_id.store import SpeakerStore      # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


A = unit([1, 0, 0, 0])
B = unit([0, 1, 0, 0])
tmp = tempfile.mkdtemp()


def fresh(name):
    return SpeakerStore(os.path.join(tmp, name + '.db'))


def add(st, emb, pid, ts=1000.0, dur=4.0, decision='matched', score=0.9, clip=''):
    return st.add_utterance(ts=ts, bus='sdr1', source='', freq='', duration=dur, model='m',
                            emb=emb, profile_id=pid, score=score, decision=decision, clip=clip)


print("\n1. re-assign")
st = fresh('r1')
pa = st.add_profile('m', A, 0)
pb = st.add_profile('m', B, 0)
for _ in range(5):
    add(st, A, pa)
    add(st, B, pb)
wrong = add(st, unit([0.05, 1, 0, 0]), pa)              # a B-voice over that landed on A
lost = add(st, unit([1, 0.02, 0, 0]), None, decision='unsure')
meh = add(st, unit([0.6, 0.8, 0, 0]), None, decision='unsure')   # 0.8 to B: adopted; 0.6 to A
rep = recluster.run(st, 'm', accept=0.70, merge_at=0.80, learn_secs=0.0, min_secs=0.0)
check("an over that clearly fits another voice moves there", st.utterance(wrong)['profile_id'] == pb, str(rep))
check("an 'unsure' over that now clears 0.70 is adopted", st.utterance(lost)['profile_id'] == pa
      and st.utterance(lost)['decision'] == 'adopted')
check("the report counts it", rep['reassigned'] == 1 and rep['adopted'] >= 1, str(rep))
Curator(st).undo(next(r['id'] for r in st.audit_rows() if r['action'] == 'reassign'))
check("the whole re-assignment can be undone", st.utterance(wrong)['profile_id'] == pa
      and st.utterance(lost)['profile_id'] is None)

print("\n2. it never undoes a human")
st = fresh('r2')
pa = st.add_profile('m', A, 0)
pb = st.add_profile('m', B, 0)
for _ in range(5):
    add(st, A, pa)
    add(st, B, pb)
cur = Curator(st, learn_secs=0.0)
cur.label(pa, name='Alice')
stray = add(st, unit([0.1, 1, 0, 0]), pa)
placed = add(st, unit([0.1, 1, 0, 0]), pb)
cur.move_over(placed, pa)                                # human says: this one IS Alice
recluster.run(st, 'm', accept=0.70, merge_at=0.80, learn_secs=0.0, min_secs=0.0)
check("nothing is pulled out of a human-labelled voice", st.utterance(stray)['profile_id'] == pa)
check("an over a human placed stays put", st.utterance(placed)['profile_id'] == pa)

print("\n3. merge only when sure, and never across a human 'no'")
st = fresh('r3')
p1 = st.add_profile('m', A, 0)
p2 = st.add_profile('m', unit([1, 0.3, 0, 0]), 0)        # 0.958 alike: one voice split in two
p3 = st.add_profile('m', unit([1, 1.0, 0, 0]), 0)        # 0.707 alike: a different person nearby
for p, v in ((p1, A), (p2, unit([1, 0.3, 0, 0])), (p3, unit([1, 1.0, 0, 0]))):
    for _ in range(4):
        add(st, v, p)
rep = recluster.run(st, 'm', accept=0.99, merge_at=0.80, learn_secs=0.0, min_secs=0.0)
live = {x[0] for x in st.load_profiles('m')}
check("a voice split in two (0.96) is merged", len({p1, p2} & live) == 1 and rep['merged'] == 1, str(rep))
check("a merely similar voice (0.71) is NOT merged", p3 in live)
st = fresh('r4')
q1 = st.add_profile('m', A, 0)
q2 = st.add_profile('m', unit([1, 0.3, 0, 0]), 0)
for p, v in ((q1, A), (q2, unit([1, 0.3, 0, 0]))):
    for _ in range(4):
        add(st, v, p)
Curator(st).not_same(q1, q2)
rep = recluster.run(st, 'm', accept=0.99, merge_at=0.80, learn_secs=0.0, min_secs=0.0)
check("'different people' blocks even a 0.96 merge", rep.get('merged', 0) == 0 and rep.get('merge_blocked_human') == 1)
st = fresh('r5')
n1 = st.add_profile('m', A, 0)
n2 = st.add_profile('m', unit([1, 0.3, 0, 0]), 0)
for p, v in ((n1, A), (n2, unit([1, 0.3, 0, 0]))):
    for _ in range(4):
        add(st, v, p)
c = Curator(st)
c.label(n1, name='Alice')
c.label(n2, name='Bob')
rep = recluster.run(st, 'm', accept=0.99, merge_at=0.80, learn_secs=0.0, min_secs=0.0)
check("two differently-named voices are never merged", rep.get('merged', 0) == 0
      and rep.get('merge_blocked_identity') == 1)

print("\n4. clip retention")
st = fresh('r6')
clips = os.path.join(tmp, 'clips6')
os.makedirs(os.path.join(clips, 'd'))
pk = st.add_profile('m', A, 0)
old = time.time() - 40 * 86400
ids = []
for k in range(25):
    path = f'd/{k}.opus'
    open(os.path.join(clips, path), 'wb').write(b'x' * 100)
    ids.append(add(st, A, pk, ts=old, score=k / 100.0, clip=path))
newer = add(st, A, pk, ts=time.time(), score=0.0, clip='d/new.opus')
open(os.path.join(clips, 'd/new.opus'), 'wb').write(b'x')
r = retention.prune(st, clips, days=30, keep_best=20)
kept = [i for i in ids if st.utterance(i)['clip']]
check("old clips beyond each voice's best 20 are deleted", r['clips_removed'] == 5 and len(kept) == 20, str(r))
check("...the lowest-scoring ones", all(st.utterance(i)['clip'] == '' for i in ids[:5]))
check("...from disk too", not os.path.exists(os.path.join(clips, 'd/0.opus')))
check("recent clips are untouched", st.utterance(newer)['clip'] == 'd/new.opus')
check("fingerprints stay when the clip goes", st.utterance_emb(ids[0])[0] is not None)

print("\n5. backup")
out = os.path.join(tmp, 'backup.tar.gz')
retention.backup(st.path, clips, out)
with tarfile.open(out) as t:
    names = t.getnames()
check("the backup holds the database and the clips", 'speakers.db' in names
      and any(n.startswith('speaker_clips') for n in names), str(names[:4]))

print("\n6. model swap re-fingerprints kept clips, then switches at once")
if shutil.which('ffmpeg'):
    from speaker_id.service import SpeakerID

    class Emb:
        def __init__(self, tag):
            self.tag, self.dim = tag, 4

        def embed(self, audio):
            # deterministic "new model": voice from the clip's loudness
            return unit([1, 0, 0, 0]) if float(np.abs(audio).mean()) > 0.05 else unit([0, 1, 0, 0])

    db = os.path.join(tmp, 'swap', 'speakers.db')
    os.makedirs(os.path.dirname(db))
    st = SpeakerStore(db)
    st.meta_set('embed_model', 'old')
    cdir = os.path.join(os.path.dirname(db), 'speaker_clips')
    os.makedirs(os.path.join(cdir, 'd'))
    pl = st.add_profile('old', unit([0, 0, 1, 0]), 0)
    pq = st.add_profile('old', unit([0, 0, 0, 1]), 0)
    st.set_profile_identity(pl, st.new_identity('', 'Loud'), 'human')
    for k in range(3):
        for pid, amp in ((pl, 0.4), (pq, 0.01)):
            t = np.arange(16000) / 16000.0
            pcm = (np.sin(2 * np.pi * 300 * t) * amp * 32767).astype('<i2').tobytes()
            path = f'd/{pid}_{k}.opus'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', '-',
                            '-c:a', 'libopus', '-b:a', '12k', '-y', os.path.join(cdir, path)],
                           input=pcm, check=True)
            u = st.add_utterance(ts=1000.0, bus='sdr1', source='', freq='', duration=1.0, model='old',
                                 emb=unit([0, 0, 1, 0]) if pid == pl else unit([0, 0, 0, 1]),
                                 profile_id=pid, score=0.9, decision='matched', clip=path)
    cfg = types.SimpleNamespace(SPEAKER_ID_MODEL='new', SPEAKER_ID_ACCEPT=0.6, SPEAKER_ID_LEARN_AT=0.6, SPEAKER_ID_NEW=0.45,
                                SPEAKER_ID_MIN_SECS=0.5, SPEAKER_ID_LEARN_SECS=0.5, SPEAKER_ID_KEEP_CLIPS=True,
                                SPEAKER_ID_BUSES='', SPEAKER_ID_DB_PATH=db, SPEAKER_ID_CALLSIGNS=False,
                                SPEAKER_ID_NIGHTLY_TIME='', SPEAKER_ID_CLIP_DAYS=30, SPEAKER_ID_MERGE_AT=0.8, SPEAKER_ID_OFFLOAD='local', SPEAKER_ID_ALERT='', SPEAKER_ID_SPLIT=False, SPEAKER_ID_SPLIT_AT=0.30)
    import speaker_id.embedder as E
    real = E.Embedder
    E.Embedder = Emb
    try:
        s = SpeakerID(cfg, None, store=st, embedder=Emb('old'))
        s.start()
        check("the old model stays live while the new one catches up", s.model_key == 'old' and s.target_model == 'new')
        end = time.time() + 30
        while s.model_key != 'new' and time.time() < end:
            s._evt.set()
            time.sleep(0.1)
        check("every kept clip was re-fingerprinted and the switch happened", s.model_key == 'new'
              and s.counters['reembedded'] == 6, str(dict(s.counters)))
        live = {p[0]: p for p in st.load_profiles('new')}
        check("the voices carried over, with their labels", set(live) == {pl, pq}
              and st.profile(pl)['label_source'] == 'human')
        check("their centroids are now in the new model's space",
              float(live[pl][1] @ unit([1, 0, 0, 0])) > 0.99 and float(live[pq][1] @ unit([0, 1, 0, 0])) > 0.99)
        check("the database records the switch", st.meta_get('embed_model') == 'new'
              and any(r['action'] == 'model_switch' for r in st.audit_rows()))
        s.stop()
    finally:
        E.Embedder = real
else:
    print("  SKIP  ffmpeg not installed")

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
