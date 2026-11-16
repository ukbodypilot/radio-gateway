"""Nightly re-cluster: undo early mistakes with everything learned since.

Live assignment decides each over once, against whatever voices existed at
that moment; the first overs of a new voice are judged against a centroid of
one. This pass revisits everything at the voice level, so it scales with
voices x overs (not overs^2: a full over-by-over clustering would need tens
of GB after a few months):

  1. centroids   recomputed from every over of each voice (>= learn_secs)
  2. reassign    an over moves to the voice it now clearly fits better
                 (best >= REASSIGN_AT and >= MARGIN above its current voice);
                 'unsure' overs that now clear REASSIGN_AT are adopted.
                 Never pulled OUT of a human-labelled voice, never moved
                 if a human moved it.
  3. merge       voices whose centroids are >= merge_at alike are merged
                 (complete linkage: every pair across two groups must clear it),
                 unless a human said "different people" (cannot_link) or
                 they carry two different identities. Closer calls go to the
                 review queue ("same person?") for a human to decide.
  4. naming      identify.maybe_name on every voice that changed

Each step is audited ('reassign' and 'merge' rows, actor 'recluster'), so
any of it can be undone from /speakers.

Thresholds, measured 2026-11-15 on 24 h of SDR1 (ERes2Net, 40 self-ID'd
overs): through one repeater and one processing chain, the CENTROIDS of two
different callsigns reached cosine 0.76 (p90 of all voice pairs 0.49), so
auto-merging at 0.60-0.70 joined different people (88% -> 47-53% right).
merge_at 0.80 merged nothing there, correctly. Reassigning at the live
accept (0.60) adopted too much (84%); at 0.70 it beat live (89% right,
36/40 assigned vs 88%, 33/40).
"""
import collections
import time

import numpy as np

MARGIN = 0.05
REASSIGN_AT = 0.70
ACTOR = 'recluster'


def _human_moved(store):
    """Overs a human placed by hand (not since undone): the re-cluster leaves them alone."""
    rows = store.audit_rows(100_000)
    undone = {r['payload'].get('of') for r in rows if r['action'] == 'undo'}
    return {r['payload']['utt_id'] for r in rows
            if r['action'] == 'move_over' and r['actor'] == 'human' and r['id'] not in undone}


def run(store, model, accept=REASSIGN_AT, merge_at=0.80, learn_secs=3.0, min_secs=1.5,
        identifier=None, max_overs=200_000, learn_at=None):
    """learn_at: rebuild each voice only from overs that matched at least this well
    (the live learning rule), so old borderline matches stop shaping it."""
    t0 = time.time()
    report = collections.Counter()
    # 1. centroids
    profiles = {pid: (c, n, iid) for pid, c, n, iid in store.load_profiles(model)}
    for pid in profiles:
        store.recompute_centroid(pid, learn_secs, model=model, min_score=learn_at)
    profiles = {pid: (c, n, iid) for pid, c, n, iid in store.load_profiles(model)}
    if not profiles:
        return dict(report, secs=round(time.time() - t0, 1))
    labels = {pid: store.profile(pid) for pid in profiles}
    human = {pid for pid, p in labels.items() if (p['label_source'] or '').startswith('human')}
    pinned = _human_moved(store)
    ids = list(profiles)
    C = np.stack([profiles[p][0] for p in ids])
    index = {pid: i for i, pid in enumerate(ids)}

    # 2. reassign
    moves = []
    for uid, pid, emb in store.embeddings(model, min_secs, max_overs):
        if uid in pinned:
            continue
        sc = C @ emb
        b = int(sc.argmax())
        best_pid, best = ids[b], float(sc[b])
        if best < accept or best_pid == pid:
            continue
        if pid is None:
            moves.append((uid, None, best_pid))
            continue
        if pid in human or pid not in index:
            continue
        if best - float(sc[index[pid]]) >= MARGIN:
            moves.append((uid, pid, best_pid))
    by_target = collections.defaultdict(list)
    for uid, frm, to in moves:
        by_target[to].append(uid)
    for to, uids in by_target.items():
        store.move_overs(uids, to)
    if moves:
        store.set_decisions([m[0] for m in moves if m[1] is None], 'adopted')
        store.audit(ACTOR, 'reassign', moves=moves)
    report['reassigned'] = sum(1 for m in moves if m[1] is not None)
    report['adopted'] = sum(1 for m in moves if m[1] is None)
    touched = {m[1] for m in moves if m[1]} | {m[2] for m in moves}
    for pid in touched:
        store.recompute_centroid(pid, learn_secs, model=model, min_score=learn_at)

    # 3. merge (greedy, most alike first, every constraint checked per group)
    profiles = {pid: (c, n, iid) for pid, c, n, iid in store.load_profiles(model)}
    ids = list(profiles)
    if len(ids) > 1:
        C = np.stack([profiles[p][0] for p in ids])
        S = C @ C.T
        pos = {pid: i for i, pid in enumerate(ids)}
        blocked = store.cannot_links()
        group = {pid: {pid} for pid in ids}
        root = {pid: pid for pid in ids}
        ident = {pid: profiles[pid][2] for pid in ids}
        pairs = [(float(S[i, j]), ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))
                 if S[i, j] >= merge_at]
        for s, a, b in sorted(pairs, reverse=True):
            ra, rb = root[a], root[b]
            if ra == rb:
                continue
            ga, gb = group[ra], group[rb]
            # Complete linkage: EVERY voice in one group must clear merge_at
            # against every voice in the other, or A joins C through B.
            if min(float(S[pos[x], pos[y]]) for x in ga for y in gb) < merge_at:
                report['merge_blocked_chain'] += 1
                continue
            if any((min(x, y), max(x, y)) in blocked for x in ga for y in gb):
                report['merge_blocked_human'] += 1
                continue
            ia = {ident[x] for x in ga if ident[x]}
            ib = {ident[x] for x in gb if ident[x]}
            if ia and ib and ia != ib:
                report['merge_blocked_identity'] += 1
                continue
            # keep the bigger (or the labelled) voice
            keep, drop = (ra, rb) if (bool(ia), len(store.over_ids(ra))) >= (bool(ib), len(store.over_ids(rb))) else (rb, ra)
            moved = store.over_ids(drop)
            store.move_overs(moved, keep)
            store.set_merged(drop, keep)
            took = None
            if not store.profile(keep)['identity_id'] and store.profile(drop)['identity_id']:
                d = store.profile(drop)
                store.set_profile_identity(keep, d['identity_id'], d['label_source'])
                took = d['identity_id']
            store.recompute_centroid(keep, learn_secs, model=model, min_score=learn_at)
            store.audit(ACTOR, 'merge', keep=keep, drop=drop, moved=moved, took_identity=took,
                        keep_prev_source=labels.get(keep, {}).get('label_source', '') if labels.get(keep) else '',
                        similarity=round(s, 3))
            group[keep] = ga | gb
            for x in group[keep]:
                root[x] = keep
            ident[keep] = ident[keep] or ident[drop]
            touched.add(keep)
            report['merged'] += 1

    # 4. naming
    if identifier is not None:
        for pid in touched:
            if store.profile(pid) and identifier.maybe_name(pid):
                report['named'] += 1
    report['voices'] = len(store.load_profiles(model))
    report['secs'] = round(time.time() - t0, 1)
    store.meta_set('last_recluster', time.time())
    store.audit(ACTOR, 'recluster_done', **{k: v for k, v in report.items()})
    return dict(report)


def undo_reassign(store, payload):
    """Put every over a 'reassign' row moved back where it was."""
    back = collections.defaultdict(list)
    for uid, frm, _to in payload['moves']:
        back[frm].append(uid)
    for frm, uids in back.items():
        store.move_overs(uids, frm)
