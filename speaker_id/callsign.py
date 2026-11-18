"""Callsigns and names in ASR text: who an over says it is from, and who it addresses.

extract(text, valid) -> [(call, role, token_index)]
  role 'self'    the speaker identifying themselves ("this is K0XYZ", call at
                 the end of an over, "... K0XYZ clear")
       'other'   a station being addressed ("thank you for that, K0XYZ",
                 "K0XYZ this is ...") or asked about: a call in a sentence
                 ending '?' is never a self-ID ("Who is that on frequency?
                 K0XYZ?", "Is this K0XYZ?") unless the sentence OPENS with
                 the self cue ("This is K0XYZ, who's on frequency?")
       'unknown'
  valid(call) -> bool is required (ULS lookup): Parakeet mangles phonetics
  ("Killer Quad 6" for KO6, "6th" for six, "K06" for KO6, "Ocean" for Oscar),
  so structure alone is not precise. Even ULS-valid is weak on its own: with
  ~820k active US calls, a garbled letter run often IS somebody's licence.
  Repetition across days and agreement with one voice are what make it real.

self_intro(text) -> [(name, place or None, char offset)]
  "this is Quentin ... in Pine Hollow", "my name is Nora and I'm in Cedar Flat": the
  speaker's own first name, and a place only when it follows the name in the
  same sentence (or a next sentence opening "I live / I'm in"). Measured on
  34k transcripts (2026-11-17): 33 overs give a name and a California licence
  city; 9 of those match exactly one FCC licensee by first name in that city,
  and where the over also spelled a call the two agreed 4 of 4. A place
  taken from elsewhere in the over was the one miss, hence the tie to the name.

name_cues(text) -> [(direction, name)]
  'back' "thanks Jeff" opening an over -> the PREVIOUS speaker was Jeff
  'fwd'  "go ahead Jeff"                 -> the NEXT speaker is Jeff
  Measured on 32k transcripts (2026-11-15): back 30%, fwd 20% right vs 2%
  chance. Weak per over, strong when many land on one voice.

Ported from the bake-off prototype; tests/test_speaker_callsign.py pins the
cases that drove each rule.
"""
import itertools
import re

US_CALL = re.compile(r'^(?:[KNW][A-Z]?|A[A-L])[0-9][A-Z]{1,3}$')

NATO = {
    'alpha': 'A', 'alfa': 'A', 'bravo': 'B', 'charlie': 'C', 'delta': 'D', 'echo': 'E',
    'foxtrot': 'F', 'fox': 'F', 'golf': 'G', 'hotel': 'H', 'india': 'I', 'juliet': 'J',
    'juliett': 'J', 'kilo': 'K', 'lima': 'L', 'mike': 'M', 'november': 'N', 'oscar': 'O',
    'papa': 'P', 'quebec': 'Q', 'romeo': 'R', 'sierra': 'S', 'tango': 'T', 'uniform': 'U',
    'victor': 'V', 'whiskey': 'W', 'whisky': 'W', 'xray': 'X', 'x-ray': 'X', 'yankee': 'Y',
    'zulu': 'Z',
    # Seen in Parakeet output on this gateway's audio
    'killer': 'K', 'killa': 'K', 'kill': 'K', 'kilos': 'K', 'uniforms': 'U', 'tangos': 'T',
    'oscars': 'O', 'novembers': 'N', 'juliette': 'J', 'gulf': 'G', 'lema': 'L', 'leema': 'L',
}
DIGITS = {'zero': '0', 'oh': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
          'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'niner': '9',
          '6th': '6', '1st': '1', '2nd': '2', '3rd': '3', '4th': '4', '5th': '5', '7th': '7',
          '8th': '8', '9th': '9', 'sixth': '6', 'first': '1', 'second': '2', 'third': '3',
          'fourth': '4', 'fifth': '5', 'seventh': '7', 'eighth': '8', 'ninth': '9'}
SELF_BEFORE = {'this', 'de', 'here', "it's", 'its', 'callsign', 'call'}
OTHER_BEFORE = {'calling', 'copy', 'thanks', 'thank', 'qsl', 'roger', 'hello', 'hi', 'hey',
                'morning', 'evening', 'afternoon', 'welcome', 'to'}
SELF_AFTER = {'clear', 'clearing', 'listening', 'monitoring', 'mobile', 'portable',
              'standing', 'out'}

ASKING = {'is', 'was', 'who', "who's", 'whos', 'are', 'were', 'what', "what's", 'did', 'do',
          'does', 'which', 'where', 'and'}

_TOK = re.compile(r"[A-Za-z0-9']+(?:-[A-Za-z0-9]+)*")


def _chars(tok):
    """Characters a token contributes to a callsign, or None if it breaks a run."""
    t = tok.lower()
    if t in NATO:
        return NATO[t]
    if t in DIGITS:
        return DIGITS[t]
    if '-' in t and all(len(p) == 1 for p in t.split('-')):   # "W-A-6-A-T-I"
        return t.replace('-', '').upper()
    if len(t) == 1 and t.isalnum():
        return t.upper()
    if re.fullmatch(r'[a-z0-9]{2,7}', t) and any(c.isdigit() for c in t) \
            and any(c.isalpha() for c in t):                   # "ko6nko", "k06"
        return t.upper()
    return None


def _variants(s):
    """O/0 confusion both ways; capped so long runs don't explode."""
    pos = [i for i, c in enumerate(s) if c in 'O0'][:4]
    for combo in itertools.product('O0', repeat=len(pos)):
        out = list(s)
        for p, c in zip(pos, combo):
            out[p] = c
        yield ''.join(out)


def _question_sentences(text, spans):
    """Per token: (is its sentence a question, index of the sentence's first token)."""
    out, first, j = [], 0, 0
    ends = [(m.start(), m.group()) for m in re.finditer(r'[.?!]+', text)]
    for k, (a, _b) in enumerate(spans):
        while j < len(ends) and ends[j][0] < a:
            j += 1
            first = k                       # a terminator before this token opens a sentence
        out.append(first)
    res = []
    for k, (a, b) in enumerate(spans):
        nxt = next((e for e in ends if e[0] >= b), None)
        res.append(('?' in nxt[1]) if nxt else False)
    return list(zip(res, out))


def extract(text, valid):
    spans = [m.span() for m in _TOK.finditer(text)]
    toks = [text[a:b] for a, b in spans]
    sent = _question_sentences(text, spans)
    low = [t.lower() for t in toks]
    runs, cur = [], []
    for i, t in enumerate(toks):
        if t.lower() == 'double' and cur is not None:
            cur.append((i, '*'))            # repeat marker
            continue
        c = _chars(t)
        if c is None and cur and any(d.isdigit() for _, ch in cur for d in ch) \
                and re.fullmatch(r'[A-Za-z]{2,3}', t):
            c = t.upper()                   # "K06 FDK": suffix said as a word
        if c is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append((i, c))
    if cur:
        runs.append(cur)

    found = []
    for run in runs:
        s, idx, starts, dbl = '', [], set(), False
        for i, c in run:
            if c == '*':
                dbl = True
                continue
            if dbl:
                c, dbl = c[0] + c, False
            starts.add(len(s))
            s += c
            idx.extend([i] * len(c))
        starts.add(len(s))                  # token boundaries in s
        best = None
        for a in sorted(starts):
            for b in sorted((x for x in starts if x > a + 3), reverse=True):
                if best and b - a <= len(best[0]):
                    break
                for v in _variants(s[a:b]):
                    if US_CALL.match(v) and valid(v):
                        best = (v, idx[a], idx[b - 1])
                        break
        if not best:
            continue
        call, i0, i1 = best
        after = set(low[i1 + 1:i1 + 3])
        at_end = i1 >= len(toks) - 3
        at_start = i0 <= 1

        def nearest(words, span):
            for d in range(1, span + 1):
                if i0 - d >= 0 and low[i0 - d] in words:
                    return d
            return None
        d_self = nearest(SELF_BEFORE, 4)
        d_other = nearest(OTHER_BEFORE, 8)       # "thank you for the input, K0XYZ"
        question, s0 = sent[i1]
        cue = i0 - d_self if d_self else None
        opens = cue is not None and (cue == s0 or (cue == s0 + 1 and low[s0] not in ASKING))
        if question and not opens:
            # Asked, not given: 2026-11-16 "Who is that on frequency? KX7QZA?"
            # and "KX7QZB, okay?" were filed as self-IDs and put a false
            # conflict card on a voice. Only "This is K0XYZ, ...?" (the cue
            # opening the sentence: first word, or second after "yeah"/"ok",
            # never after "is"/"was") is the speaker's own call.
            role = 'other'
        elif d_self and (not d_other or d_self < d_other):
            role = 'self'
        elif d_other or (at_start and {'this'} & set(low[i1 + 1:i1 + 3])):
            role = 'other'
        elif after & SELF_AFTER:
            role = 'self'
        elif at_end and not at_start:
            role = 'self'
        else:
            role = 'unknown'
        found.append((call, role, i0))
    return found


def self_call(text, valid):
    """The callsign the speaker gave as their own, or None.

    When an over holds more than one self-ID, the LAST one applies: on a fast
    turnaround (no 1 s gap, so one utterance) the earlier call belongs to the
    station handing over and the last to whoever is speaking at the end.
    """
    calls = [c for c, r, _ in extract(text, valid) if r == 'self']
    return calls[-1] if calls else None


# ── turn-taking name cues ──────────────────────────────────────────────────
NICK = {'bob': 'robert', 'rob': 'robert', 'bobby': 'robert', 'bill': 'william', 'will': 'william',
        'billy': 'william', 'rick': 'richard', 'rich': 'richard', 'dick': 'richard', 'mike': 'michael',
        'jim': 'james', 'jimmy': 'james', 'jeff': 'jeffrey', 'joe': 'joseph', 'tom': 'thomas',
        'dave': 'david', 'dan': 'daniel', 'danny': 'daniel', 'steve': 'steven', 'chris': 'christopher',
        'matt': 'matthew', 'tony': 'anthony', 'ed': 'edward', 'eddie': 'edward', 'ted': 'edward',
        'ken': 'kenneth', 'larry': 'lawrence', 'jerry': 'gerald', 'greg': 'gregory', 'pat': 'patrick',
        'ron': 'ronald', 'don': 'donald', 'doug': 'douglas', 'chuck': 'charles', 'charlie': 'charles',
        'al': 'albert', 'andy': 'andrew', 'drew': 'andrew', 'nick': 'nicholas', 'sam': 'samuel',
        'ben': 'benjamin', 'fred': 'frederick', 'frank': 'francis', 'hank': 'henry', 'jack': 'john',
        'johnny': 'john', 'jon': 'jonathan', 'kathy': 'katherine', 'kate': 'katherine',
        'liz': 'elizabeth', 'sue': 'susan', 'tim': 'timothy', 'walt': 'walter', 'wes': 'wesley',
        'phil': 'phillip', 'ray': 'raymond', 'russ': 'russell', 'stan': 'stanley', 'vic': 'victor',
        'zach': 'zachary'}

_NOT_NAMES = set('''I The Yeah Okay Ok Go Sir Yes No Well So And But Alright All Right Good Thanks
Thank You Roger Copy Hey Hi Hello Uh Um Oh Now Then Here There This That It Ahead Everybody
Everyone Guys Folks Net Control Again Please Over Back Buddy Man Brother Bro Kilo Whiskey
November Alpha Mike Papa Sierra Echo Oscar Lima Delta Charlie Tango Victor Romeo Yankee Bravo
Golf Hotel India Juliet Uniform Zulu Quebec Foxtrot Monday Tuesday Wednesday Thursday Friday
Saturday Sunday'''.split())
_NAME = r"([A-Z][a-z]{2,})"
_FWD = [re.compile(p) for p in (
    r"\b[Gg]o ahead,? (?:to )?" + _NAME + r"\b",
    r"\b[Oo]ver to " + _NAME + r"\b",
    r"\b[Bb]ack to (?:you,? )?" + _NAME + r"\b",
    _NAME + r",? (?:you're up|go ahead|your turn)\b",
)]
# Prefix case-insensitive, the name itself must be capitalised ("thank you for" is not a name).
_BACK = re.compile(r"^\W*(?i:(?:(?:okay|ok|yeah|well|alright|all right|and)[, ]+)?"
                   r"(?:thanks|thank you|roger|copy|good (?:morning|evening|afternoon)|hey|hi|hello)"
                   r"(?: (?:so much|very much|that|you|for that))?[, ]+)" + _NAME + r"\b")


_INTRO = re.compile(r"\b(?:[Tt]his is|[Mm]y name(?:'s| is))\s+([A-Z][a-z]{2,})\b")
_PLACE = re.compile(r"\b(?:in|from|out of)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")
_NEXT_PLACE = re.compile(r"^\W*I(?:'m| am|\s+live)\b[^.?!]{0,40}?\b(?:in|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")


def self_intro(text):
    out = []
    text = text or ''
    for m in _INTRO.finditer(text):
        name = m.group(1)
        if name in _NOT_NAMES or name.lower() in NATO or name.lower() in DIGITS:
            continue
        tail = text[m.end():m.end() + 90]
        end = re.search(r'[.?!]', tail)
        sentence = tail[:end.start()] if end else tail
        p = _PLACE.search(sentence)
        if not p and end:
            p = _NEXT_PLACE.search(text[m.end() + end.end():])
        out.append((name, p.group(1) if p else None, m.start()))
    return out


def name_cues(text):
    out = []
    for p in _FWD:
        for m in p.finditer(text or ''):
            if m.group(1) not in _NOT_NAMES:
                out.append(('fwd', m.group(1)))
    m = _BACK.match(text or '')
    if m and m.group(1) not in _NOT_NAMES:
        out.append(('back', m.group(1)))
    return out


def name_matches(spoken, first_name):
    """Does a spoken name fit a licensee's first name? None if unknown."""
    if not first_name:
        return None
    s, fn = spoken.lower(), first_name.lower()
    return fn == s or fn == NICK.get(s) or (len(s) >= 3 and fn.startswith(s))


def near_miss(a, b):
    """Same length, one character different: the commonest ASR callsign slip."""
    return len(a) == len(b) and a != b and sum(x != y for x, y in zip(a, b)) == 1


def misheard(a, b):
    """near_miss, or one character dropped (KX7QZA heard as KX7QZ).

    For the review queue only: a labelled voice whose overs give its own call
    with a letter missing is the ASR, not a second person (2026-11-17, a
    six-character label heard as its first five raised a conflict card).
    Not used for folding evidence: a short call one letter shorter than a
    regular's can be a real, different licence.
    """
    if near_miss(a, b):
        return True
    if abs(len(a) - len(b)) != 1:
        return False
    long_, short = (a, b) if len(a) > len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))
