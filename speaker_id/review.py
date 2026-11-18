"""Review queue: the questions where one human click is worth most.

Ranked by how much traffic the answer would settle:
  confirm   a voice the evidence points at a callsign for (named automatically,
            or a candidate below the naming bar): "is this K0XYZ?"
  conflict  a named voice whose own overs now point elsewhere, or that gives
            two different calls about as often
  same      two voices that sound alike (centroid cosine from SAME_LO up to
            the auto-merge threshold): "same person?". Centroids of different
            people reach 0.76 on one repeater, so this is a human call.
  name      a busy voice with no callsign evidence at all: "who is this?"
Human-labelled voices are settled and only appear as 'conflict'.
"""
import numpy as np

from speaker_id.callsign import misheard

MIN_OVERS_TO_ASK = 5
THIN_BELOW = 2          # a label given as a self-ID fewer times than this is "thin"
SAME_LO = 0.55


def build(store, identifier, model, limit=30, same_lo=SAME_LO, same_hi=0.80):
    profiles = {p['id']: p for p in store.list_profiles()}
    dismissed = dismissed_calls(store)
    items = []
    for pid, p in profiles.items():
        if p['overs'] < MIN_OVERS_TO_ASK:
            continue
        human = (p['label_source'] or '').startswith('human')
        v = identifier.verdict(pid) if identifier else {'candidates': [], 'callsign': None}
        cands = v['candidates']
        # A call one letter off the label (swapped or dropped) is the
        # transcriber mishearing it (KX7QZA heard as KX7QZB or KX7QZ), not a
        # second person: no card for that.
        others = [c for c in cands if p['callsign'] and c['weight'] > 0 and c['callsign'] != p['callsign']
                  and not misheard(c['callsign'], p['callsign'])
                  and c['weight'] > dismissed.get((pid, c['callsign']), -1.0)]
        if others:
            # Even ONE self-ID of another call in a labelled voice is worth a
            # look: 2026-11-15 a voice labelled from a single over was really
            # net control, who gave a different call twenty minutes later.
            o = others[0]
            own = int(next((c['weight'] for c in cands if c['callsign'] == p['callsign']), 0))
            thin = own < THIN_BELOW
            items.append({'type': 'conflict', 'profiles': [pid], 'thin': thin,
                          'score': p['overs'] * (1.0 + sum(c['weight'] for c in others)) * (1.5 if thin else 1.0),
                          'prompt': f"Voice #{pid} is labelled {p['callsign']}, but in its overs people say they are "
                                    + ', '.join(f"{c['callsign']}" + (f" ({c['first_name']})" if c['first_name'] else '')
                                                + (f" {c['weight']:.0f}x" if c['weight'] > 1 else '') for c in others)
                                    + ". Wrong label, or more than one person in this voice?"
                                    + (f" (The label rests on {own} self-ID{'' if own == 1 else 's'} of its own.)"
                                       if thin else ''),
                          'callsign': p['callsign'],
                          'candidates': ([{'callsign': p['callsign']}] + others)[:3]})
            continue
        if human:
            continue
        if len(cands) >= 2 and cands[1]['weight'] >= 2 and cands[1]['weight'] >= 0.5 * cands[0]['weight']:
            items.append({'type': 'conflict', 'profiles': [pid], 'score': p['overs'] * 1.5,
                          'prompt': f"Voice #{pid} gives two calls: {cands[0]['callsign']} and "
                                    f"{cands[1]['callsign']}. One person, or two voices merged?",
                          'candidates': cands[:3]})
        elif cands and not cands[0]['weight']:
            # Never gave a call, but introduced themselves with a name and a
            # place that fit exactly one FCC licensee.
            c, d = cands[0], cands[0]['intro']
            items.append({'type': 'confirm', 'profiles': [pid],
                          'score': p['overs'] * (1.0 + 0.5 * d['n']),
                          'prompt': f"Is voice #{pid} {c['callsign']}? Introduced as {d['name']} in {d['city']} "
                                    f"{d['n']}x; the FCC lists one {d['name']} there: {c['callsign']}",
                          'callsign': c['callsign'], 'candidates': cands[:3], 'intro': d})
        elif cands:
            c = cands[0]
            named = p['callsign'] == c['callsign']
            items.append({'type': 'confirm', 'profiles': [pid],
                          'score': p['overs'] * (1.0 + c['weight']) * (0.6 if named else 1.0),
                          'prompt': (f"Named automatically as {c['callsign']}" if named else
                                     f"Is voice #{pid} {c['callsign']}?") +
                                    (f" ({c['first_name']})" if c['first_name'] else '') +
                                    f" — said it {c['weight']:.0f}x on {c['days']} day(s)",
                          'callsign': c['callsign'], 'candidates': cands[:3]})
        elif not p['callsign']:
            items.append({'type': 'name', 'profiles': [pid], 'score': p['overs'] * 0.8,
                          'prompt': f"Voice #{pid}: {p['overs']} overs, never gave a callsign. Who is it?"})
    items.extend(_similar_pairs(store, model, profiles, same_lo, same_hi))
    homes = {}
    for p in sorted(profiles.values(), key=lambda p: p['overs']):
        if p['callsign']:
            homes[p['callsign']] = p['id']        # the busiest voice carrying each call
    items.sort(key=lambda i: -i['score'])
    for it in items[:limit]:
        it['samples'] = {pid: _samples(store, pid, it) for pid in it['profiles']}
        it['voices'] = {pid: _brief(profiles.get(pid)) for pid in it['profiles']}
        # The words each callsign was heard in, so a confirm is judged on
        # what was said, not on a bare count.
        calls = [c['callsign'] for c in it.get('candidates') or [] if c.get('callsign')]
        if calls:
            it['quotes'] = [dict(q, callsign=c) for c in calls
                            for q in store.callsign_quotes(it['profiles'][0], c, 2)]
        if it.get('intro'):
            it['quotes'] = [dict(q, callsign=it['intro']['name'] + ', ' + it['intro']['city'])
                            for v in it['intro']['values'] for q in store.evidence_quotes(it['profiles'][0], 'intro', v)]
        if it['type'] == 'conflict':
            keep = it.get('callsign') or (calls[0] if calls else None)
            it['groups'] = split_groups(store, it['profiles'][0], model, keep, calls)
            for g in it['groups']:
                # The other call already has a voice: move these there, not to a new one.
                home = homes.get(g['callsign'])
                if home and home != it['profiles'][0]:
                    g['home'] = home
    return items[:limit]


def split_groups(store, pid, model, keep, calls, limit=40):
    """A conflict card's suggested split: per call other than `keep`, the overs
    where the voice gave it (seeds) plus the overs that sound more like those
    than like the rest of the voice. An over that fits two groups goes to the
    one it scores higher with. [{'callsign', 'seeds': [id], 'overs': [...]}]
    """
    from speaker_id.curation import closer_set
    sids = store.self_id_overs(pid)
    others = [c for c in calls if c != keep and sids.get(c)]
    seeded = {u for c in others for u in sids[c]}
    groups, best = [], {}
    for c in others:
        g = closer_set(store, pid, model, sids[c], exclude=seeded - set(sids[c]), limit=limit)
        for r in g:
            if r['id'] not in best or r['score'] > best[r['id']][1]:
                best[r['id']] = (c, r['score'])
        groups.append({'callsign': c, 'seeds': list(sids[c]), 'overs': g})
    for grp in groups:
        grp['overs'] = [r for r in grp['overs'] if best[r['id']][0] == grp['callsign']]
    return groups


def dismissed_calls(store):
    """{(profile_id, callsign): weight} a human said are not that voice ("It's <label>"
    on a conflict card), not since undone."""
    rows = store.audit_rows(100_000)
    undone = {r['payload'].get('of') for r in rows if r['action'] == 'undo'}
    out = {}
    for r in reversed(rows):                      # oldest first: later dismissals win
        if r['action'] == 'keep_label' and r['id'] not in undone:
            for c, w in (r['payload'].get('dismissed') or {}).items():
                out[(r['payload']['profile_id'], c)] = float(w)
    return out


def _samples(store, pid, item, k=3):
    """Clips to listen to: for a callsign question, overs where the voice SAYS that call,
    each with `at` (seconds in) so playback can start just before it."""
    calls = [item.get('callsign')] if item.get('callsign') else \
        [c['callsign'] for c in item.get('candidates') or []]
    out, seen = [], set()
    for call in calls:
        for uid, at in store.callsign_overs(pid, call, k):
            if uid not in seen and len(out) < k:
                out.append({'id': uid, 'at': at, 'callsign': call})
                seen.add(uid)
    for o in store.profile_overs(pid, k, best=True):
        if len(out) >= k:
            break
        if o['id'] not in seen:
            out.append({'id': o['id'], 'at': None})
            seen.add(o['id'])
    return out


def _similar_pairs(store, model, profiles, lo, hi):
    rows = [(pid, c) for pid, c, n, _iid in store.load_profiles(model)
            if pid in profiles and profiles[pid]['overs'] >= MIN_OVERS_TO_ASK]
    if len(rows) < 2:
        return []
    ids = [r[0] for r in rows]
    C = np.stack([r[1] for r in rows])
    S = C @ C.T
    blocked = store.cannot_links()
    out = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            s = float(S[i, j])
            a, b = ids[i], ids[j]
            if s < lo or (min(a, b), max(a, b)) in blocked:
                continue
            pa, pb = profiles[a], profiles[b]
            if pa['callsign'] and pb['callsign'] and pa['callsign'] != pb['callsign']:
                continue          # two differently-named voices: not a question worth asking
            if pa['identity_id'] and pb['identity_id'] and pa['identity_id'] == pb['identity_id']:
                continue          # already the same person (two voice profiles of one identity)
            named = pa if pa['identity_id'] else pb if pb['identity_id'] else None
            if named is pb:
                a, b, pa, pb = b, a, pb, pa          # named voice first: "Yes" merges INTO it
            base = min(pa['overs'], pb['overs']) * (1 + 2 * (s - lo) / max(1e-6, hi - lo))
            if named:
                prompt = (f"Voice #{b} sounds {alike_words(s)} {_label(pa)} ({s:.2f}). Same person?")
            else:
                prompt = f"Voices #{a} and #{b} sound {alike_words(s)} each other ({s:.2f}). Same person?"
            out.append({'type': 'same', 'profiles': [a, b], 'similarity': round(s, 3),
                        'score': base * (2.0 if named else 1.0), 'prompt': prompt})
    return out


def alike_words(s):
    """Plain words for a centroid similarity on this channel (strangers reach ~0.76)."""
    return 'very like' if s >= 0.72 else 'like' if s >= 0.62 else 'a bit like'


def _label(p):
    who = ' '.join(x for x in (p.get('callsign'), f"({p['name']})" if p.get('name') else '') if x)
    return who or f"voice #{p['id']}"


def similar_to(store, model, pid, k=3):
    """The k voices that sound most like voice `pid`: [{id, callsign, name, overs, similarity, different}]."""
    rows = store.load_profiles(model)
    mine = next((c for p, c, _n, _i in rows if p == pid), None)
    if mine is None:
        return []
    profiles = {p['id']: p for p in store.list_profiles()}
    blocked = store.cannot_links()
    out = []
    for p, c, _n, _i in rows:
        if p == pid or p not in profiles:
            continue
        s = float(c @ mine)
        q = profiles[p]
        out.append({'id': p, 'callsign': q['callsign'], 'name': q['name'], 'overs': q['overs'],
                    'similarity': round(s, 3), 'words': alike_words(s),
                    'different': (min(p, pid), max(p, pid)) in blocked})
    out.sort(key=lambda x: -x['similarity'])
    return out[:k]


def _brief(p):
    if not p:
        return None
    return {k: p[k] for k in ('id', 'callsign', 'name', 'overs', 'buses', 'freqs', 'label_source', 'last')}
