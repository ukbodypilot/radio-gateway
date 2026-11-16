#!/usr/bin/env python3
"""Splitting an utterance at a speaker change (fast turnarounds on nets).

The boundary search is tested with a crafted VAD trace and a fake fingerprint
(the "voice" is which half of the clip the audio came from), so the logic is
pinned without real speech. The service test stubs the detector and checks
that each part becomes its own over with its own words and callsign.
Callsigns here are invented.
"""

import os
import sys
import tempfile
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_id import split as S   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


SR, F = 16000, 512
A, B = unit([1, 0, 0]), unit([0, 1, 0])


def probs_with_gaps(secs, gaps):
    p = np.full(int(secs * SR / F), 0.9, np.float32)
    for g0, g1 in gaps:
        p[int(g0 * SR / F):int(g1 * SR / F)] = 0.02
    return p


def audio_two_voices(secs, change_at):
    """Sample value encodes the voice: +0.1 before change_at, -0.1 after."""
    a = np.full(int(secs * SR), 0.1, np.float32)
    a[int(change_at * SR):] = -0.1
    return a


def fake_embed(seg):
    m = float(np.mean(seg))
    return A if m > 0.05 else B if m < -0.05 else unit([1, 1, 0])


print("\n1. pauses")
p = probs_with_gaps(12, [(0.5, 0.9), (6.0, 6.4), (11.5, 11.9)])
g = S.pauses(p)
check("a short gap inside the utterance is a candidate", any(abs(a - 6.0) < 0.05 for a, b in g), str(g))
check("gaps near either end are not (each side needs speech to fingerprint)",
      not any(a < 1.0 or a > 11.0 for a, b in g), str(g))
check("a 0.1 s flicker is not a gap", S.pauses(probs_with_gaps(12, [(6.0, 6.1)])) == [])

print("\n2. boundaries")
audio = audio_two_voices(12, 6.2)
cuts = S.find_boundaries(audio, fake_embed, probs=p, split_at=0.3)
check("a change of voice at a pause is found, mid-gap", len(cuts) == 1 and 6.0 <= cuts[0] <= 6.4, str(cuts))
same = np.full(12 * SR, 0.1, np.float32)
check("the same voice either side of a pause is not split",
      S.find_boundaries(same, fake_embed, probs=p, split_at=0.3) == [])
check("no pause, no split (never cuts mid-speech)",
      S.find_boundaries(audio, fake_embed, probs=probs_with_gaps(12, []), split_at=0.3) == [])
check("short utterances are left alone", S.find_boundaries(audio[:4 * SR], fake_embed,
                                                           probs=p[:int(4 * SR / F)], split_at=0.3) == [])
three = np.full(18 * SR, 0.1, np.float32)
three[int(6.2 * SR):int(12.2 * SR)] = -0.1
p3 = probs_with_gaps(18, [(6.0, 6.4), (12.0, 12.4)])
c3 = S.find_boundaries(three, fake_embed, probs=p3, split_at=0.3)
check("A -> B -> A finds both changes", len(c3) == 2 and 6.0 <= c3[0] <= 6.4 and 12.0 <= c3[1] <= 12.4, str(c3))
calls = []
S.find_boundaries(audio, lambda x: (calls.append(1), fake_embed(x))[1],
                  probs=probs_with_gaps(12, [(t, t + 0.3) for t in (2, 3.5, 5, 6.2, 7.5, 9)]), split_at=0.3)
check("cost is bounded: at most MAX_CANDIDATES gaps tested per segment",
      len(calls) <= 2 * S.MAX_CANDIDATES * S.MAX_PARTS, str(len(calls)))

print("\n3. splitting the transcript")
text = 'Copy that, go ahead. Good evening, this is KX7QZA in town.'
wt = [[0, 0.2], [5, 0.5], [11, 0.9], [14, 1.2], [21, 6.5], [26, 6.8], [35, 7.1], [40, 7.3], [43, 7.5], [50, 8.4], [53, 8.6]]
parts, starts = S.split_text(text, [6.2], wt, 9.0)
check("with word timings each word goes to the part it was said in",
      parts == ['Copy that, go ahead.', 'Good evening, this is KX7QZA in town.'], str(parts))
check("...and each part knows where it starts in the text", starts == [0, 21], str(starts))
pw = S.part_word_times(wt, 21, len(text) + 1, 6.2)
check("word timings are re-based onto the part", pw[0] == [0, 0.3] and pw[3] == [19, 1.1], str(pw))
parts2, _ = S.split_text(text, [4.5], None, 9.0)
check("without timings the split is proportional, at a word boundary",
      len(parts2) == 2 and ' '.join(parts2) == text and not parts2[1].startswith(' '), str(parts2))

print("\n4. the service stores each part as its own over")
from speaker_id.service import SpeakerID          # noqa: E402
from speaker_id.store import SpeakerStore         # noqa: E402
from speaker_id.identify import Identifier        # noqa: E402


class Uls:
    def ready(self):
        return True

    def valid(self, c):
        return c in ('KX7QZA', 'KX7QZB')

    def get(self, c):
        return ('A', 'Quentin Ames', 'Town', 'CA', 'G') if self.valid(c) else None

    def first_name(self, c):
        return 'Quentin' if self.valid(c) else ''

    def stop(self):
        pass


class Emb:
    dim = 3

    def embed(self, seg):
        return fake_embed(seg) if len(seg) >= 0.5 * SR else None


cfg = types.SimpleNamespace(SPEAKER_ID_MODEL='fake', SPEAKER_ID_ACCEPT=0.65, SPEAKER_ID_LEARN_AT=0.70,
                            SPEAKER_ID_NEW=0.45, SPEAKER_ID_MIN_SECS=1.5, SPEAKER_ID_LEARN_SECS=3.0,
                            SPEAKER_ID_KEEP_CLIPS=False, SPEAKER_ID_BUSES='', SPEAKER_ID_DB_PATH='',
                            SPEAKER_ID_CALLSIGNS=False, SPEAKER_ID_NIGHTLY_TIME='', SPEAKER_ID_CLIP_DAYS=30,
                            SPEAKER_ID_MERGE_AT=0.80, SPEAKER_ID_OFFLOAD='local', SPEAKER_ID_ALERT='',
                            SPEAKER_ID_SPLIT=True, SPEAKER_ID_SPLIT_AT=0.30)
tmp = tempfile.mkdtemp()
st = SpeakerStore(os.path.join(tmp, 'speakers.db'))
s = SpeakerID(cfg, None, store=st, embedder=Emb())
s._split_mod = types.SimpleNamespace(MIN_SECS=5.0, find_boundaries=lambda audio, embed, split_at: [6.2])
s._ident = Identifier(st, Uls())
s.start()
s._uls = Uls()
s._ident = Identifier(st, Uls())
item = {'uid': 1, 'audio_16k': audio_two_voices(9, 6.2), 'audio_boost': 1.0, 'duration': 9.0,
        'source_id': 'sdr1', 'start_time': 1000.0, 'upstream_source': None}
s.on_utterance(item)
s.on_result(1, {'text': text, 'tx_id': 77, 'word_times': wt})
end = time.time() + 5
while time.time() < end and s.counters['processed'] < s.counters['queued']:
    time.sleep(0.02)
rows = st._conn.execute('SELECT id, ts, duration, part, parts, text, profile_id FROM utterances ORDER BY id').fetchall()
check("one utterance with a speaker change becomes two overs", len(rows) == 2
      and [r[3] for r in rows] == [0, 1] and all(r[4] == 2 for r in rows), str(rows))
check("each over gets its own time and length", abs(rows[1][1] - 1006.2) < 0.01 and abs(rows[0][2] - 6.2) < 0.01, str(rows))
check("...its own voice", rows[0][6] != rows[1][6])
check("...and its own words", rows[0][5] == 'Copy that, go ahead.' and rows[1][5].startswith('Good evening'))
ev = st.evidence_for(rows[1][0])
check("the callsign lands on the speaker who said it, not on the first speaker",
      [e['value'] for e in ev if e['kind'] == 'callsign_self'] == ['KX7QZA']
      and not [e for e in st.evidence_for(rows[0][0]) if e['kind'] == 'callsign_self'], str(ev))
sp = st.speakers_for_tx([77])[77]
check("the TX Log row lists both speakers, the last as its headline",
      len(sp['parts']) == 2 and sp['said'] == 'KX7QZA' and sp['profile_id'] == rows[1][6], str(sp))
check("the split is counted", s.counters['split_overs'] == 1)
check("the worker survived the whole run", s._thread.is_alive())
s.stop()

print("\n5. an exception in idle work never kills the worker")
s2 = SpeakerID(cfg, None, store=SpeakerStore(os.path.join(tmp, 'b.db')), embedder=Emb())
s2.start()
s2._ident, s2._uls = object(), None            # deliberately broken idle-work state
s2._evt.set()
time.sleep(1.5)
check("the worker thread is still running after an idle-work error",
      s2._thread.is_alive() and s2.counters['errors'] >= 1, str(dict(s2.counters)))
s2.stop()

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
