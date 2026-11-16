#!/usr/bin/env python3
"""Callsign evidence -> voice names (speaker_id.callsign + speaker_id.identify).

Every case here is one that misled a rule on real transcripts during the
2026-11-15 bake-off, rewritten with invented calls and names. A fake ULS
stands in for the FCC table: only calls in it are "licensed".
"""

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_id import callsign as cs                  # noqa: E402
from speaker_id.identify import Identifier, Regulars   # noqa: E402
from speaker_id.store import SpeakerStore              # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class FakeUls:
    CALLS = {'KX7QZA': 'Quentin Ames', 'KX7QZB': 'Bernard Cole', 'WX9YYY': 'Jefferson Doe',
             'AK7Q': 'Christopher Ell', 'KX0QFD': 'Austin Fry', 'NX6MND': 'Nora Gale'}

    def ready(self):
        return True

    def valid(self, c):
        return c in self.CALLS

    def get(self, c):
        n = self.CALLS.get(c)
        return ('A', n, 'Town', 'CA', 'G') if n else None

    def first_name(self, c):
        n = self.CALLS.get(c)
        return n.split()[0] if n else ''


U = FakeUls()


def calls(text):
    return [(c, r) for c, r, _ in cs.extract(text, U.valid)]


print("\n1. reading callsigns out of ASR text")
check("written-out call after 'this is' is the speaker's",
      calls('Have a good day. This is KX7QZA on the repeater.') == [('KX7QZA', 'self')])
check("phonetics, with ASR's '6th'-style digits",
      calls('Kilo X-ray 7th Quebec Zulu Alpha, clear.') == [('KX7QZA', 'self')], str(calls('Kilo X-ray 7th Quebec Zulu Alpha, clear.')))
check("'K06'-style O/0 confusion and a suffix said as a word",
      calls('This is KX0 QFD.') == [('KX0QFD', 'self')], str(calls('This is KX0 QFD.')))
check("thanking a station at the end of an over is addressing it, not self-ID",
      calls('So anyway, but thank you for the input, a k seven Q.') == [('AK7Q', 'other')],
      str(calls('So anyway, but thank you for the input, a k seven Q.')))
check("a match may not cut a word in half (no short call out of a misheard long one)",
      calls('EXN, this is KX7QZAB. Sounds good.') == [], str(calls('EXN, this is KX7QZAB. Sounds good.')))
check("an unlicensed letter run is not a call", calls('This is KQ9ZZZ.') == [])
check("'chat with X' addresses X",
      calls('This is KX7QZA. I would like to chat with KX7QZB.') == [('KX7QZA', 'self'), ('KX7QZB', 'other')])

print("\n2. turn-taking name cues")
check("'thanks Greg' names the previous speaker", cs.name_cues('Thanks, Greg. Well, if') == [('back', 'Greg')])
check("'thank you for' is not a name", cs.name_cues('Thank you for chipping in') == [])
check("'go ahead Jamie' names the next speaker", cs.name_cues('okay, go ahead Jamie') == [('fwd', 'Jamie')])
check("nicknames meet ULS first names", cs.name_matches('Jeff', 'Jeffrey') and cs.name_matches('Chris', 'Christopher'))
check("a different name does not", cs.name_matches('Bob', 'Jefferson') is False)

print("\n3. regulars fold a consistent mishearing onto the real call")
r = Regulars()
for d in range(10):
    for _ in range(9):
        r.add('KX7QZA', d * 86400.0)
r.add('KX7QZB', 0.0)
check("a rare near-miss folds onto the far commoner regular", r.fold('KX7QZB') == 'KX7QZA')
thin = Regulars()
for _ in range(3):
    thin.add('KX7QZA', 0.0)
thin.add('KX7QZB', 0.0)
check("no fold onto a call with a thin history (3 vs 1 is two stations, not a slip)",
      thin.fold('KX7QZB') == 'KX7QZB')
for d in range(5):
    for _ in range(8):
        r.add('KX7QZB', d * 86400.0)
check("...but not once it is heard about as often (it may be a real second station)",
      r.fold('KX7QZB') == 'KX7QZB')

print("\n4. evidence -> a named voice")
tmp = tempfile.mkdtemp()
st = SpeakerStore(os.path.join(tmp, 'speakers.db'))
idf = Identifier(st, U)
pid = st.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
other = st.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)


def over(ts, text, profile, bus='sdr1', dur=4.0):
    u = st.add_utterance(ts=ts, bus=bus, source=bus, freq='', duration=dur, model='fake',
                         emb=None, profile_id=profile, score=0.9, decision='matched')
    st.set_text(u, text)
    idf.file(u, ts, dur, bus, text)
    return u


DAY = 86400.0
over(1 * DAY, 'This is KX7QZA, good morning.', pid)
check("one self-ID is never enough to name a voice", idf.maybe_name(pid) is None)
v = idf.verdict(pid)
check("...and the verdict says why", any('not named yet' in w for w in v['why']), str(v['why']))
over(1 * DAY + 60, 'KX7QZA clear.', pid)
check("two self-IDs on ONE day are still not enough", idf.maybe_name(pid) is None)
over(2 * DAY, 'This is KX7QZA mobile.', pid)
check("self-IDs on two days name the voice", idf.maybe_name(pid) == 'KX7QZA')
p = st.profile(pid)
check("the voice is tied to an identity carrying the ULS name",
      p['identity_id'] and st.identity_call(p['identity_id']) == 'KX7QZA'
      and p['label_source'].startswith('auto:'), str(p))
check("the change is audited", st._conn.execute("SELECT COUNT(*) FROM audit WHERE action='name_profile'").fetchone()[0] == 1)
check("the UI label follows", st.profile_labels().get(pid) == ('KX7QZA', 'Quentin Ames'))

amb = over(2 * DAY + 100, 'Thanks, this is KX7QZA. Good evening, this is KX7QZB.', other)
check("two self-IDs in one over: the LAST applies (fast turnaround)",
      [e['value'] for e in st.evidence_for(amb) if e['kind'] == 'callsign_self'] == ['KX7QZB']
      and [e['value'] for e in st.evidence_for(amb) if e['kind'] == 'mention'] == ['KX7QZA'],
      str(st.evidence_for(amb)))

one = st.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
for k in range(4):
    over(9 * DAY + k * 60, 'This is KX0QFD.', one, bus='sdr3')
check("four self-IDs on one day: not yet", idf.maybe_name(one) is None)
over(9 * DAY + 300, 'KX0QFD clear.', one, bus='sdr3')
check("five on one day from one voice: named", idf.maybe_name(one) == 'KX0QFD')

print("\n5. cues land on the neighbouring over")
a = over(3 * DAY, 'Just checking in on the net tonight.', other, bus='sdr2')
over(3 * DAY + 10, 'Thanks, Quentin. Next station?', pid, bus='sdr2')
check("'thanks Quentin' was filed on the PREVIOUS over on that bus",
      [e['value'] for e in st.evidence_for(a) if e['kind'] == 'name_cue_back'] == ['Quentin'])
over(3 * DAY + 20, 'Okay, go ahead Bernard.', pid, bus='sdr2')
b = over(3 * DAY + 30, 'Yes, I have a comment.', other, bus='sdr2')
check("'go ahead Bernard' was filed on the NEXT over on that bus",
      [e['value'] for e in st.evidence_for(b) if e['kind'] == 'name_cue_fwd'] == ['Bernard'])
c = over(3 * DAY + 40, 'Something unrelated.', other, bus='sdr1')
check("cues do not cross buses", not [e for e in st.evidence_for(c) if e['kind'].startswith('name_cue')])

print("\n6. a human label is never overridden")
st.set_profile_identity(other, st.identity_for_call('WX9YYY', create_name='Jefferson Doe'), 'human')
for d in (5, 6, 7):
    over(d * DAY, 'This is KX7QZB.', other)
check("auto-naming leaves a human-labelled voice alone", idf.maybe_name(other) is None
      and st.identity_call(st.profile(other)['identity_id']) == 'WX9YYY')

print("\n7. backfill re-files history once, resumably")
st2 = SpeakerStore(os.path.join(tmp, 'hist.db'))
p2 = st2.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
for i, t in enumerate(['This is KX7QZA.', 'hello', 'KX7QZA clear.']):
    u = st2.add_utterance(ts=(i + 1) * DAY, bus='sdr1', source='', freq='', duration=3.0, model='fake',
                          emb=None, profile_id=p2, score=0.9, decision='matched')
    st2.set_text(u, t)
idf2 = Identifier(st2, U)
n = idf2.backfill(budget_secs=5, ceiling=st2.max_utterance_id())
check("every stored over is filed", n == 3, str(n))
check("history alone can name a voice", st2.profile_labels().get(p2, ('',))[0] == 'KX7QZA')
check("a second pass files nothing (resumable, no duplicates)", idf2.backfill(5, st2.max_utterance_id()) == 0
      and st2._conn.execute("SELECT COUNT(*) FROM evidence WHERE kind='callsign_self'").fetchone()[0] == 2)

print("\n7b. folding only onto a call this voice has given")
stf = SpeakerStore(os.path.join(tmp, 'fold.db'))
idff = Identifier(stf, U)
for d in range(10):
    for _ in range(9):
        idff.regulars.add('KX7QZA', d * DAY)
pf = stf.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
pg = stf.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)


def fover(pid, text, ts):
    u = stf.add_utterance(ts=ts, bus='sdr1', source='', freq='', duration=3.0, model='fake', emb=None,
                          profile_id=pid, score=0.9, decision='matched')
    stf.set_text(u, text)
    idff.file(u, ts, 3.0, 'sdr1', text, profile_id=pid)
    return [e['value'] for e in stf.evidence_for(u) if e['kind'] == 'callsign_self']


check("a rare near-miss from a voice that never gave the regular call stays as heard",
      fover(pg, 'This is KX7QZB.', 1.0) == ['KX7QZB'])
fover(pf, 'This is KX7QZA.', 2.0)
check("...but folds once the voice has given the regular call itself",
      fover(pf, 'This is KX7QZB.', 3.0) == ['KX7QZA'])

print("\n8. where in the over the call was said (play from just before it)")
from transcribe_engine import word_times  # noqa: E402
toks = [' This', ' is', ' K', 'X', '7', 'Q', 'Z', 'A', ' clear', '.']
times = [0.5, 0.8, 4.0, 4.1, 4.3, 4.6, 4.8, 5.0, 6.2, 6.5]
txt = ''.join(toks).strip()
wt = word_times(toks, times, txt)
check("Parakeet tokens -> word start offsets and times", wt == [[0, 0.5], [5, 0.8], [8, 4.0], [15, 6.2]], str(wt))
check("tokens that don't rebuild the text give no timings (never wrong ones)",
      word_times(toks, times, 'something else') is None)
starts = [m.start() for m in cs._TOK.finditer(txt)]
i = [x for x in cs.extract(txt, U.valid) if x[1] == 'self'][0][2]
check("with word timings the position is exact", Identifier.position(txt, starts, i, 7.0, wt) == 4.0)
est = Identifier.position(txt, starts, i, 7.0, None)
check("without them it is estimated, shifted early by the measured bias",
      est == round(max(0.0, 7.0 * 8 / len(txt) - Identifier.EST_BIAS), 2), str(est))
st3 = SpeakerStore(os.path.join(tmp, 'pos.db'))
idf3 = Identifier(st3, U)
p3 = st3.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
u3 = st3.add_utterance(ts=1.0, bus='sdr1', source='', freq='', duration=7.0, model='fake', emb=None,
                       profile_id=p3, score=0.9, decision='matched', clip='d/1.opus')
st3.set_text(u3, txt)
idf3.file(u3, 1.0, 7.0, 'sdr1', txt, word_times=wt)
check("the self-ID evidence carries its position", st3.callsign_at([u3]) == {u3: ('KX7QZA', 4.0)},
      str(st3.callsign_at([u3])))
from speaker_id.review import _samples  # noqa: E402
u4 = st3.add_utterance(ts=2.0, bus='sdr1', source='', freq='', duration=3.0, model='fake', emb=None,
                       profile_id=p3, score=0.99, decision='matched', clip='d/2.opus')
sm = _samples(st3, p3, {'callsign': 'KX7QZA'})
check("callsign questions play overs where the call is SAID first, with the position",
      sm[0] == {'id': u3, 'at': 4.0, 'callsign': 'KX7QZA'} and sm[1]['id'] == u4, str(sm))

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
