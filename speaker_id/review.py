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

MIN_OVERS_TO_ASK = 5
SAME_LO = 0.55


def build(store, identifier, model, limit=30, same_lo=SAME_LO, same_hi=0.80):
    profiles = {p['id']: p for p in store.list_profiles()}
    items = []
    for pid, p in profiles.items():
        if p['overs'] < MIN_OVERS_TO_ASK:
            continue
        human = (p['label_source'] or '').startswith('human')
        v = identifier.verdict(pid) if identifier else {'candidates': [], 'callsign': None}
        cands = v['candidates']
        if p['callsign'] and cands and cands[0]['callsign'] != p['callsign'] and cands[0]['weight'] >= 2:
            items.append({'type': 'conflict', 'profiles': [pid], 'score': p['overs'] * 2.0,
                          'prompt': f"Voice #{pid} is labelled {p['callsign']}, but its own overs "
                                    f"say {cands[0]['callsign']} ({cands[0]['weight']:.0f}x)",
                          'candidates': cands[:3]})
            continue
        if human:
            continue
        if len(cands) >= 2 and cands[1]['weight'] >= 2 and cands[1]['weight'] >= 0.5 * cands[0]['weight']:
            items.append({'type': 'conflict', 'profiles': [pid], 'score': p['overs'] * 1.5,
                          'prompt': f"Voice #{pid} gives two calls: {cands[0]['callsign']} and "
                                    f"{cands[1]['callsign']}. One person, or two voices merged?",
                          'candidates': cands[:3]})
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
    items.sort(key=lambda i: -i['score'])
    for it in items[:limit]:
        it['samples'] = {pid: _samples(store, pid, it) for pid in it['profiles']}
        it['voices'] = {pid: _brief(profiles.get(pid)) for pid in it['profiles']}
    return items[:limit]


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
            out.append({'type': 'same', 'profiles': [a, b], 'similarity': round(s, 3),
                        'score': min(pa['overs'], pb['overs']) * (1 + 2 * (s - lo) / max(1e-6, hi - lo)),
                        'prompt': f"Voices #{a} and #{b} sound alike ({s:.2f}). Same person?"})
    return out


def _brief(p):
    if not p:
        return None
    return {k: p[k] for k in ('id', 'callsign', 'name', 'overs', 'buses', 'freqs', 'label_source', 'last')}
