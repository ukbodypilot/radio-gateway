"""Evidence -> identity. Files what each over says about who spoke it, and decides
when a voice has earned a name.

Like lan-inventory's identify.py: every clue is filed per over with a weight,
a source and a rule VERSION; the verdict for a voice is recomputed from all of
its overs' evidence, so better rules re-score the whole history (bump VERSION
and the backfill pass re-files every over). Human labels (source 'human',
batch 4) always win and are never re-filed or overridden.

Evidence kinds (on the over they describe):
  callsign_self  the speaker gave this call as their own        WEIGHT 1.0
  name_cue       a neighbouring over named the speaker           0.3 back / 0.2 fwd
                 ("thanks Jeff" -> previous over; "go ahead Jeff" -> next over),
                 checked against the ULS first name of candidate calls
  mention        a call addressed or heard in the over           0 (activity only)
  intro          the speaker's own first name, and place if   0.5 as a name cue
                 given ("this is Quentin ... in Pine Hollow" -> "Name|Place")
                 Name + place -> FCC licensees of that first name in that
                 city, in the states this channel's named voices hold
                 licences in. Exactly one -> a candidate (a confirm card, or
                 name support when it agrees with a self-ID). Never names a
                 voice on its own.

Regulars: calls heard on many days are the population a channel mostly has.
A self-ID one character away from a call heard >= 3x as often is folded onto
it: a consistent ASR slip is itself "regular", so the fold needs the
frequency gap, not just regular status, and the target must be established
(>= FOLD_MIN_MENTIONS on >= FOLD_MIN_DAYS days).

A voice is named automatically when its top call has >= MIN_SHARE of the
voice's self-ID weight and either >= MIN_SELF_IDS self-IDs on >= MIN_DAYS
days, or >= ONE_DAY_SELF_IDS self-IDs, or >= WITH_NAME_SELF_IDS plus a name
cue matching its ULS first name. One self-ID is never enough: ASR garble
often lands on a real licence. (On 24 h of SDR1 the two-day rule alone named
nobody, though one voice said the same call 15 times and was called by the
licensee's first name.)
"""
import collections
import time

from speaker_id import callsign as cs

VERSION = 5          # 2: evidence `at` (seconds into the over); 3: `ch` (char offset),
                     # last self-ID wins, folds only onto a call this voice has given;
                     # 4: a call in a question ("...? K0XYZ?") is not a self-ID;
                     # 5: 'intro' evidence (own name, and place)
SOURCE = 'rules'
WEIGHT = {'callsign_self': 1.0, 'name_cue_back': 0.3, 'name_cue_fwd': 0.2, 'mention': 0.0,
          'intro': 0.5}
HOME_STATE_SHARE = 0.15    # a state holding this share of the named voices' licences is "local"
HOME_STATES_SECS = 3600.0
MIN_SELF_IDS = 2
MIN_DAYS = 2
MIN_SHARE = 0.7
ONE_DAY_SELF_IDS = 5          # this many identical self-IDs from one voice in a day is no garble
WITH_NAME_SELF_IDS = 3        # ...or this many plus a matching ULS first-name cue
CUE_GAP_SECS = 30.0
FOLD_RATIO = 3
FOLD_MIN_MENTIONS = 10     # only fold onto an ESTABLISHED regular: on a thin history
FOLD_MIN_DAYS = 3          # 3-vs-1 would merge two real stations


class Regulars:
    """Callsign frequency over distinct days, seeded from the transcription history."""

    def __init__(self):
        self.mentions = collections.Counter()
        self.days = collections.defaultdict(set)

    def add(self, call, ts):
        self.mentions[call] += 1
        self.days[call].add(int(ts // 86400))

    def seed_from_log(self, tlog, valid, limit=200_000):
        conn = getattr(tlog, '_conn', None)
        lock = getattr(tlog, '_lock', None)
        if conn is None or lock is None:
            return 0
        with lock:
            rows = conn.execute('SELECT ts, text FROM transcriptions ORDER BY ts DESC LIMIT ?',
                                (limit,)).fetchall()
        n = 0
        for ts, text in rows:
            for c, _r, _i in cs.extract(text, valid):
                self.add(c, ts)
                n += 1
        return n

    def fold(self, call):
        """The regular this call is probably a mishearing of, else the call itself."""
        mine = self.mentions.get(call, 0)
        cand = [r for r in self.mentions
                if self.mentions[r] >= max(FOLD_MIN_MENTIONS, FOLD_RATIO * max(1, mine))
                and len(self.days[r]) >= FOLD_MIN_DAYS and cs.near_miss(call, r)]
        return cand[0] if len(cand) == 1 else call

    def regular_days(self, call):
        return len(self.days.get(call, ()))


class Identifier:
    def __init__(self, store, uls, regulars=None):
        self.store = store
        self.uls = uls
        self.regulars = regulars or Regulars()
        self._last = {}            # bus -> (utt_id, end_ts) of the previous over
        self._fwd = {}             # bus -> (name, ts) waiting for the next over
        self.counters = collections.Counter()
        self._home = (0.0, None)

    # ── per over ────────────────────────────────────────────────────────
    def file(self, utt_id, ts, duration, bus, text, count_regular=True, word_times=None,
             profile_id=None):
        """File the rule evidence for one over (and its neighbours' cues). Returns kinds filed.

        count_regular=False for history: the regulars were seeded from the
        transcription log, which already holds those overs' text.
        """
        if not self.uls.ready():
            self.counters['no_uls'] += 1
            return []
        # Re-filing history has no word timings: keep the positions measured
        # when the over was first filed (Parakeet's, or the rebuild's) rather
        # than replace them with the character-share estimate.
        kept = {} if word_times else self.store.evidence_positions(utt_id, SOURCE)
        self.store.clear_rule_evidence(utt_id, SOURCE)
        filed = []
        selfs = []
        starts = [m.start() for m in cs._TOK.finditer(text)]
        voice_calls = self.store.voice_calls(profile_id) if profile_id else set()
        for call, role, i in cs.extract(text, self.uls.valid):
            ch = starts[i] if i < len(starts) else None
            at = kept[ch] if ch in kept else self.position(text, starts, i, duration, word_times)
            if count_regular:
                self.regulars.add(call, ts)
            if role == 'self':
                # Fold a near-miss onto a regular only if THIS voice has given
                # that regular's call before: otherwise a rare but real
                # licence is absorbed into a common one.
                folded = self.regulars.fold(call)
                selfs.append((folded if folded in voice_calls else call, at, ch))
            else:
                self._add(utt_id, 'mention', call, at, ch)
        if selfs:
            # The LAST self-ID applies: an over with two is a fast turnaround,
            # and the speaker at the end is the one who gave the last call.
            call, at, ch = selfs[-1]
            self._add(utt_id, 'callsign_self', call, at, ch)
            filed.append('callsign_self')
            for c, a, h in selfs[:-1]:
                if c != call:
                    self._add(utt_id, 'mention', c, a, h)
        for name, place, ch in cs.self_intro(text):
            self._add(utt_id, 'intro', f"{name}|{place or ''}",
                      self.position(text, starts, self._tok_at(starts, ch), duration, word_times), ch)
            filed.append('intro')
        # turn-taking: this over's opening thanks names the PREVIOUS speaker,
        # a pending "go ahead X" from the previous over names THIS speaker.
        prev = self._last.get(bus)
        fwd = self._fwd.pop(bus, None)
        if fwd and ts - fwd[1] <= CUE_GAP_SECS:
            self._add(utt_id, 'name_cue_fwd', fwd[0])
            filed.append('name_cue_fwd')
        for direction, name in cs.name_cues(text):
            if direction == 'back' and prev and ts - prev[1] <= CUE_GAP_SECS:
                self._add(prev[0], 'name_cue_back', name)
                filed.append('name_cue_back')
            elif direction == 'fwd':
                self._fwd[bus] = (name, ts + duration)
        self._last[bus] = (utt_id, ts + duration)
        for k in filed:
            self.counters[k] += 1
        return filed

    @staticmethod
    def _tok_at(starts, ch):
        """Index of the token starting at or before char offset `ch`."""
        k = 0
        for i, a in enumerate(starts):
            if a > ch:
                break
            k = i
        return k

    def home_states(self):
        """States holding >= HOME_STATE_SHARE of the named voices' licences (None = no limit)."""
        ts, states = self._home
        if time.time() - ts < HOME_STATES_SECS:
            return states
        state_of = getattr(self.uls, 'state_of', None)
        got = collections.Counter(filter(None, (state_of(c) for c in self.store.labelled_calls()))) \
            if state_of else collections.Counter()
        total = sum(got.values())
        states = {s for s, n in got.items() if n >= HOME_STATE_SHARE * total} if total >= 3 else None
        self._home = (time.time(), states)
        return states

    def intro_matches(self, value):
        """'Name|Place' -> (name, city, [calls]) from the FCC table."""
        name, _, place = value.partition('|')
        look = getattr(self.uls, 'by_name_city', None)
        if not place or not look:
            return name, None, []
        city, calls = look(name, place, cs.name_matches, self.home_states())
        return name, city, calls

    def _add(self, utt_id, kind, value, at=None, ch=None):
        self.store.add_evidence(utt_id, kind, value, WEIGHT[kind], SOURCE, VERSION, at, ch)

    EST_BIAS = 2.0

    @staticmethod
    def position(text, starts, token_index, duration, word_times=None):
        """Seconds into the over where token `token_index` was said.

        Exact from the ASR's word timings when it sent them (Parakeet does).
        Otherwise (history, older workers, Moonshine) estimated as the token's
        share of the characters times the over's length, minus EST_BIAS:
        measured on 12 real self-IDs that estimate ran 0.4-2.5 s LATE
        (spelled-out calls take longer to say than their share of the text),
        so the uncorrected estimate plus a 1 s pre-roll started after the call
        9 times in 12; corrected, it starts before it every time.
        """
        if not text or token_index >= len(starts):
            return None
        c = starts[token_index]
        if word_times:
            best = None
            for off, t in word_times:
                if off <= c:
                    best = t
                else:
                    break
            if best is not None:
                return round(float(best), 2)
        if not duration:
            return None
        return round(max(0.0, duration * c / len(text) - Identifier.EST_BIAS), 2)

    # ── per voice ───────────────────────────────────────────────────────
    def verdict(self, profile_id):
        """What the evidence says about one voice.

        {'callsign', 'share', 'self_ids', 'days', 'name_support', 'candidates', 'why'}
        callsign is None until the auto-naming bar is met.
        """
        ev = self.store.profile_evidence(profile_id)
        self_w = collections.Counter()
        self_days = collections.defaultdict(set)
        names = collections.Counter()
        intros = collections.Counter()
        for kind, value, weight, source, ts in ev:
            if kind == 'callsign_self':
                self_w[value] += weight
                self_days[value].add(int(ts // 86400))
            elif kind.startswith('name_cue'):
                names[value] += weight
            elif kind == 'intro':
                names[value.split('|')[0]] += weight
                intros[value] += 1
        # "This is Quentin in Pine Hollow" -> the one FCC Quentin there, if there is one.
        by_intro = {}
        for value, n in intros.items():
            name, city, calls = self.intro_matches(value)
            if len(calls) == 1:
                d = by_intro.setdefault(calls[0], {'n': 0, 'name': name, 'city': city, 'values': []})
                d['n'] += n
                d['values'].append(value)
        total = sum(self_w.values())
        cands = []
        for call, w in self_w.most_common(5):
            fn = self.uls.first_name(call)
            support = sum(v for n, v in names.items() if cs.name_matches(n, fn))
            c = {'callsign': call, 'weight': round(w, 2), 'days': len(self_days[call]),
                 'first_name': fn, 'name_support': round(support, 2)}
            if call in by_intro:
                c['intro'] = by_intro.pop(call)
                c['name_support'] = round(support + c['intro']['n'], 2)
            cands.append(c)
        # Intro-only candidates go after every self-ID'd one (weight 0: they
        # raise a confirm card, never a conflict, never an automatic name).
        for call, d in sorted(by_intro.items(), key=lambda kv: -kv[1]['n']):
            cands.append({'callsign': call, 'weight': 0.0, 'days': 0, 'first_name': self.uls.first_name(call),
                          'name_support': 0.0, 'intro': d})
        out = {'callsign': None, 'share': 0.0, 'self_ids': 0, 'days': 0, 'name_support': 0.0,
               'candidates': cands, 'why': []}
        if not total:
            out['why'].append('no self-identification heard in this voice yet')
            for c in cands:
                d = c['intro']
                out['why'].append(f"introduced as {d['name']} in {d['city']} {d['n']}x: the only "
                                  f"{d['name']} licensed there is {c['callsign']}")
            return out
        top = cands[0]
        out.update(share=round(top['weight'] / total, 2), self_ids=int(top['weight']),
                   days=top['days'], name_support=top['name_support'])
        why = [f"said '{top['callsign']}' as their own call {int(top['weight'])}x "
               f"on {top['days']} day(s), {int(100 * out['share'])}% of this voice's self-IDs"]
        if top.get('intro'):
            d = top['intro']
            why.append(f"introduced as {d['name']} in {d['city']} {d['n']}x, and the only "
                       f"{d['name']} licensed there is {top['callsign']}")
        if top['name_support']:
            why.append(f"named '{top['first_name']}' (ULS name) by others or themselves, "
                       f"{top['name_support']:.1f} weight")
        w = top['weight']
        ok = out['share'] >= MIN_SHARE and (
            (w >= MIN_SELF_IDS and top['days'] >= MIN_DAYS)
            or w >= ONE_DAY_SELF_IDS
            or (w >= WITH_NAME_SELF_IDS and top['name_support'] > 0))
        if not ok:
            why.append(f'not named yet: needs >= {int(MIN_SHARE * 100)}% share and '
                       f'{MIN_SELF_IDS}+ self-IDs on {MIN_DAYS}+ days, or {ONE_DAY_SELF_IDS}+, '
                       f'or {WITH_NAME_SELF_IDS}+ with a matching name cue')
        else:
            out['callsign'] = top['callsign']
        out['why'] = why
        return out

    def maybe_name(self, profile_id):
        """Attach a voice to the identity its evidence points at. Never touches a human label."""
        p = self.store.profile(profile_id)
        if not p or p['label_source'].startswith('human'):
            return None
        v = self.verdict(profile_id)
        call = v['callsign']
        if not call:
            return None
        cur = None
        if p['identity_id']:
            cur = self.store.identity_call(p['identity_id'])
        if cur == call:
            return call
        u = self.uls.get(call)
        iid = self.store.identity_for_call(call, create_name=(u[1] if u else ''))
        self.store.set_profile_identity(profile_id, iid, f'auto:callsign v{VERSION}')
        self.store.audit('auto', 'name_profile', profile_id=profile_id, callsign=call,
                         previous=cur, share=v['share'], self_ids=v['self_ids'], days=v['days'])
        self.counters['auto_named'] += 1
        return call

    # ── history ─────────────────────────────────────────────────────────
    def backfill(self, budget_secs=2.0, ceiling=None):
        """Re-file evidence for stored overs not yet scored at this VERSION. Resumable.

        Processes in id order (= arrival order) so neighbour cues line up.
        Returns the number of overs filed this call.
        """
        if not self.uls.ready():
            return 0
        key = f'evidence_v{VERSION}_upto'
        upto = int(self.store.meta_get(key, 0) or 0)
        t0 = time.time()
        n = 0
        touched = set()
        saved_last, saved_fwd = self._last, self._fwd
        self._last, self._fwd = {}, {}           # history has its own neighbours
        try:
            while time.time() - t0 < budget_secs:
                rows = self.store.texted_utterances(upto, 200)
                if ceiling is not None:
                    rows = [r for r in rows if r[0] <= ceiling]   # live filing owns the rest
                if not rows:
                    break
                for uid, ts, bus, freq, text, pid, dur in rows:
                    self.file(uid, ts, dur or 0.0, bus, text, count_regular=False, profile_id=pid)
                    upto = uid
                    n += 1
                    if pid:
                        touched.add(pid)
                self.store.meta_set(key, upto)
        finally:
            self._last, self._fwd = saved_last, saved_fwd
        for pid in touched:
            self.maybe_name(pid)
        return n
