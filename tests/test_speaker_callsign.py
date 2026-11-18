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

print("\n9. a call that is asked about is not a self-ID")
check("'Who is that? KX7QZA?' asks, it does not identify",
      calls('Who is that on frequency? KX7QZA?') == [('KX7QZA', 'other')], str(calls('Who is that on frequency? KX7QZA?')))
check("a tag question after a call is not a self-ID ('KX7QZA, okay?')",
      calls("That's just old school, I guess. KX7QZA, okay?") == [('KX7QZA', 'other')])
check("'Is this KX7QZA?' asks", calls('Is this KX7QZA?') == [('KX7QZA', 'other')])
check("'This is KX7QZA, who's on frequency?' still identifies",
      calls("This is KX7QZA, who's on frequency?") == [('KX7QZA', 'self')])
check("'Yeah, it's KX7QZA, you there?' still identifies",
      calls("Yeah, it's KX7QZA, you there?") == [('KX7QZA', 'self')])
check("a question in ANOTHER sentence does not stop a self-ID",
      calls('This is KX7QZA. You hanging out there?') == [('KX7QZA', 'self')])
# Re-filing history (no word timings) keeps the position measured first time.
idf3.file(u3, 1.0, 7.0, 'sdr1', txt)
check("re-filing without word timings keeps the measured position",
      st3.callsign_at([u3]) == {u3: ('KX7QZA', 4.0)}, str(st3.callsign_at([u3])))

print("\n10. introductions: own name, and place, against the FCC table")
check("'this is NAME in PLACE'", cs.self_intro('Thanks Jeff, this is Quentin in Pine Hollow, Kilo X-ray.')
      == [('Quentin', 'Pine Hollow', 13)], str(cs.self_intro('Thanks Jeff, this is Quentin in Pine Hollow, Kilo X-ray.')))
check("'my name is NAME and I'm in PLACE'",
      [x[:2] for x in cs.self_intro("My name is Nora and I'm in Cedar Flat, California")] == [('Nora', 'Cedar Flat')])
check("a place in the NEXT sentence counts only after 'I live / I'm in'",
      [x[:2] for x in cs.self_intro('My name is Nora. I live in Cedar Flat.')] == [('Nora', 'Cedar Flat')]
      and [x[:2] for x in cs.self_intro('This is Nora. Big news from Cedar Flat.')] == [('Nora', None)])
check("phonetics are not names ('this is Kilo X-ray ...')", cs.self_intro('This is Kilo X-ray seven.') == [])
import sqlite3  # noqa: E402
from speaker_id.uls import UlsLookup  # noqa: E402
up = os.path.join(tmp, 'uls.db')
db = sqlite3.connect(up)
db.execute('CREATE TABLE calls(call TEXT PRIMARY KEY, status TEXT, name TEXT, city TEXT, state TEXT, class TEXT)')
db.executemany('INSERT INTO calls VALUES (?,?,?,?,?,?)', [
    ('KX7QZA', 'A', 'Quentin Ames', 'Pine Hollow', 'CA', 'G'),
    ('KX7QZB', 'A', 'Bernard Cole', 'Pine Hollow', 'CA', 'T'),
    ('NX6MND', 'A', 'Nora Gale', 'Cedar Flat', 'CA', 'T'),
    ('KX9QQQ', 'A', 'Nora Hill', 'Cedar Flat', 'MI', 'T'),       # same town name, other state
    ('KX0QFD', 'A', 'Austin Fry', 'Pine Hollow', 'CA', 'E'),
    ('KX0QFE', 'A', 'Austin Gray', 'Pine Hollow', 'CA', 'E'),
    ('KX7OLD', 'E', 'Quentin Old', 'Pine Hollow', 'CA', 'G')])  # expired: not counted
db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
db.execute("INSERT INTO meta VALUES ('built', '1')")
db.commit()
db.close()
uls = UlsLookup(up, auto_refresh=False)
check("name + city -> the licensees of that first name there (active only)",
      uls.by_name_city('Quentin', 'Pine Hollow', cs.name_matches) == ('Pine Hollow', ['KX7QZA']))
check("the place may run on past the city", uls.by_name_city('Nora', 'Cedar Flat California', cs.name_matches,
                                                            {'CA'}) == ('Cedar Flat', ['NX6MND']))
check("...and only in the home states (a Cedar Flat in Michigan too)",
      len(uls.by_name_city('Nora', 'Cedar Flat', cs.name_matches)[1]) == 2)
st5 = SpeakerStore(os.path.join(tmp, 'intro.db'))
idf5 = Identifier(st5, uls)
for c in ('KX7QZB', 'KX0QFD', 'KX0QFE'):           # three CA voices already named here
    q = st5.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
    st5.set_profile_identity(q, st5.identity_for_call(c, create_name='x'), 'human')
check("home states come from the named voices' licences", idf5.home_states() == {'CA'}, str(idf5.home_states()))
pn = st5.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)


def over5(ts, text, profile):
    u = st5.add_utterance(ts=ts, bus='sdr1', source='', freq='', duration=4.0, model='fake', emb=None,
                          profile_id=profile, score=0.9, decision='matched', clip='i/%d.opus' % int(ts))
    st5.set_text(u, text)
    idf5.file(u, ts, 4.0, 'sdr1', text, profile_id=profile)
    return u


for k in range(5):
    over5(100.0 + k, 'Good evening all.', pn)
over5(200.0, "My name is Nora and I'm in Cedar Flat. Glad to be on.", pn)
v5 = idf5.verdict(pn)
check("a voice that never gave a call gets the one licensee its introduction fits",
      [c['callsign'] for c in v5['candidates']] == ['NX6MND'] and v5['callsign'] is None, str(v5))
check("...and the why list says so", any('Nora in Cedar Flat' in w for w in v5['why']), str(v5['why']))
from speaker_id import review  # noqa: E402
cards = [i for i in review.build(st5, idf5, 'fake') if i['profiles'] == [pn]]
check("it becomes a confirm card quoting the introduction",
      cards and cards[0]['type'] == 'confirm' and cards[0]['callsign'] == 'NX6MND'
      and any('Cedar Flat' in q['quote'] for q in cards[0].get('quotes', [])), str(cards))
check("an introduction alone never names a voice", idf5.maybe_name(pn) is None)
amb = st5.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
for k in range(5):
    over5(300.0 + k, 'Good evening all.', amb)
over5(400.0, 'This is Austin in Pine Hollow.', amb)
check("two Austins in Pine Hollow: no candidate", idf5.verdict(amb)['candidates'] == [])
pq = st5.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
over5(500.0, 'This is Quentin in Pine Hollow, KX7QZA.', pq)
vq = idf5.verdict(pq)
check("an introduction agreeing with the self-ID is name support for it",
      vq['candidates'][0]['callsign'] == 'KX7QZA' and vq['candidates'][0]['name_support'] >= 1.0
      and len(vq['candidates']) == 1, str(vq['candidates']))
lab = st5.add_profile('fake', np.ones(4, np.float32) / 2, 0.0)
st5.set_profile_identity(lab, st5.identity_for_call('KX7QZB', create_name='x'), 'human')
for k in range(5):
    over5(600.0 + k, 'Good evening all.', lab)
over5(700.0, "My name is Nora and I'm in Cedar Flat.", lab)
check("an introduction never raises a conflict on a labelled voice",
      not [i for i in review.build(st5, idf5, 'fake') if i['profiles'] == [lab] and i['type'] == 'conflict'])

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
