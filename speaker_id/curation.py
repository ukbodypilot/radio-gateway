"""Human curation: label, merge, move, "not the same person", undo.

The human is the strongest witness. Every change here:
  * runs on the speaker-ID worker thread (SpeakerID.run_on_worker), so it can
    never race live assignment updating the same voice;
  * is written to the audit table with enough detail to undo it exactly;
  * marks the voice label_source 'human...', which automation never overrides
    (identify.maybe_name skips it; the nightly re-cluster keeps it).

A merge is soft (profiles.merged_into), so undo can bring the voice back with
its own id and overs.
"""
import time

import numpy as np

ACTOR = 'human'


class CurationError(ValueError):
    pass


class Curator:
    def __init__(self, store, uls=None, learn_secs=3.0):
        self.store = store
        self.uls = uls
        self.learn_secs = learn_secs

    def _profile(self, pid):
        p = self.store.profile(int(pid))
        if not p:
            raise CurationError(f'no voice #{pid}')
        return p

    def _recompute(self, *pids):
        for pid in pids:
            if pid:
                self.store.recompute_centroid(pid, self.learn_secs)

    # ── operations ──────────────────────────────────────────────────────
    def label(self, profile_id, callsign='', name='', notes=None):
        """This voice is <callsign / name>. Voices with the same callsign share one identity."""
        p = self._profile(profile_id)
        callsign = (callsign or '').strip().upper()
        name = (name or '').strip()
        if not callsign and not name:
            raise CurationError('give a callsign or a name')
        cur_ident = self.store.identity(p['identity_id']) if p['identity_id'] else None
        if (cur_ident and p['label_source'] == 'human' and notes is None
                and cur_ident['callsign'] == callsign and (not name or cur_ident['name'] == name)):
            return {'profile_id': p['id'], 'identity_id': p['identity_id'], 'unchanged': True}
        if callsign:
            uls_name = ''
            if self.uls is not None and self.uls.ready():
                r = self.uls.get(callsign)
                uls_name = r[1] if r else ''
            iid = self.store.identity_for_call(callsign, create_name=name or uls_name)
        elif p['identity_id'] and not self.store.identity_call(p['identity_id']):
            iid = p['identity_id']                      # renaming a name-only identity
        else:
            iid = self.store.new_identity('', name)
        if name:
            self.store.update_identity(iid, name=name)
        if notes is not None:
            self.store.update_identity(iid, notes=notes)
        self.store.set_profile_identity(p['id'], iid, 'human')
        self.store.audit(ACTOR, 'label', profile_id=p['id'], identity_id=iid,
                         prev_identity=p['identity_id'], prev_source=p['label_source'],
                         callsign=callsign, name=name)
        return {'profile_id': p['id'], 'identity_id': iid}

    def unlabel(self, profile_id):
        """Not this name. Stays unnamed: automation will not re-name a human-cleared voice."""
        p = self._profile(profile_id)
        self.store.set_profile_identity(p['id'], None, 'human:cleared')
        self.store.audit(ACTOR, 'unlabel', profile_id=p['id'],
                         prev_identity=p['identity_id'], prev_source=p['label_source'])
        return {'profile_id': p['id']}

    def merge(self, keep, drop):
        """Two voice profiles are one voice. `drop`'s overs move to `keep`."""
        k, d = self._profile(keep), self._profile(drop)
        if k['id'] == d['id']:
            raise CurationError('cannot merge a voice with itself')
        if (min(k['id'], d['id']), max(k['id'], d['id'])) in self.store.cannot_links():
            raise CurationError(f'voices #{k["id"]} and #{d["id"]} were marked as different people')
        moved = self.store.over_ids(d['id'])
        self.store.move_overs(moved, k['id'])
        self.store.set_merged(d['id'], k['id'])
        took = None
        if not k['identity_id'] and d['identity_id']:
            self.store.set_profile_identity(k['id'], d['identity_id'], d['label_source'])
            took = d['identity_id']
        self._recompute(k['id'])
        self.store.audit(ACTOR, 'merge', keep=k['id'], drop=d['id'], moved=moved,
                         took_identity=took, keep_prev_source=k['label_source'])
        return {'keep': k['id'], 'moved': len(moved)}

    def move_over(self, utt_id, to_profile=None):
        """This one over belongs to another voice (or, with no target, to a new voice of its own)."""
        u = self.store.utterance(int(utt_id))
        if not u:
            raise CurationError(f'no over #{utt_id}')
        frm = u['profile_id']
        if to_profile in (None, '', 'new'):
            emb, model, ts = self.store.utterance_emb(u['id'])
            if emb is None:
                raise CurationError('that over has no fingerprint')
            to = self.store.add_profile(model, emb, ts)
            created = True
        else:
            to = self._profile(to_profile)['id']
            created = False
        self.store.move_overs([u['id']], to)
        self._recompute(frm, to)
        self.store.audit(ACTOR, 'move_over', utt_id=u['id'], frm=frm, to=to, created=created)
        return {'utt_id': u['id'], 'to': to}

    def keep_label(self, profile_id, dismiss):
        """The voice IS who it is labelled: the other calls heard in its overs are not
        this person (mentions, mishearings), so stop raising them as a conflict.

        dismiss: {callsign: weight seen}. A call is raised again only if more
        of it turns up later (weight above what was dismissed).
        """
        p = self._profile(profile_id)
        call = self.store.identity_call(p['identity_id']) if p['identity_id'] else None
        if not call:
            raise CurationError(f'voice #{p["id"]} has no callsign to keep')
        if p['label_source'] != 'human':
            self.label(p['id'], call)
        dis = {str(c).strip().upper(): float(w) for c, w in (dismiss or {}).items() if str(c).strip()}
        self.store.audit(ACTOR, 'keep_label', profile_id=p['id'], callsign=call, dismissed=dis)
        return {'profile_id': p['id'], 'callsign': call, 'dismissed': sorted(dis)}

    def split_off(self, profile_id, utt_ids, callsign='', name=''):
        """These overs of a voice are someone else: they become a voice of their own.

        The new voice starts from just these overs, is marked "different
        people" from the old one (so no re-cluster or card merges them back),
        and is labelled if a callsign or name is given. The rest stay put.
        """
        p = self._profile(profile_id)
        ids = sorted({int(u) for u in utt_ids})
        if not ids:
            raise CurationError('tick the overs that are someone else')
        rows = []
        for uid in ids:
            u = self.store.utterance(uid)
            if not u or u['profile_id'] != p['id']:
                raise CurationError(f'over #{uid} is not in voice #{p["id"]}')
            emb, model, ts = self.store.utterance_emb(uid)
            if emb is None:
                raise CurationError(f'over #{uid} has no fingerprint')
            rows.append((emb, model, ts))
        if len(ids) >= len(self.store.over_ids(p['id'])):
            raise CurationError('that is every over in the voice: label it instead')
        m = np.mean([r[0] for r in rows], axis=0)
        to = self.store.add_profile(rows[0][1], (m / np.linalg.norm(m)).astype(np.float32),
                                    min(r[2] for r in rows))
        self.store.move_overs(ids, to)
        self._recompute(p['id'], to)
        self.store.add_cannot_link(p['id'], to)
        self.store.audit(ACTOR, 'split_off', frm=p['id'], to=to, utt_ids=ids)
        out = {'from': p['id'], 'to': to, 'moved': len(ids)}
        if (callsign or '').strip() or (name or '').strip():
            out['label'] = self.label(to, callsign, name)
        return out

    def not_same(self, a, b):
        """These two voices are different people: never merged, by hand or by the re-cluster."""
        pa, pb = self._profile(a), self._profile(b)
        self.store.add_cannot_link(pa['id'], pb['id'])
        self.store.audit(ACTOR, 'not_same', a=pa['id'], b=pb['id'])
        return {'a': pa['id'], 'b': pb['id']}

    def note(self, profile_id, notes):
        p = self._profile(profile_id)
        if not p['identity_id']:
            raise CurationError('label the voice before adding notes')
        prev = (self.store.identity(p['identity_id']) or {}).get('notes', '')
        self.store.update_identity(p['identity_id'], notes=notes)
        self.store.audit(ACTOR, 'note', identity_id=p['identity_id'], prev=prev)
        return {'identity_id': p['identity_id']}

    # ── undo ────────────────────────────────────────────────────────────
    def undo(self, audit_id):
        row = self.store.audit_row(int(audit_id))
        if not row:
            raise CurationError(f'no change #{audit_id}')
        if any(r['action'] == 'undo' and r['payload'].get('of') == row['id']
               for r in self.store.audit_rows(500)):
            raise CurationError(f'change #{audit_id} was already undone')
        a, p = row['action'], row['payload']
        if a in ('label', 'unlabel'):
            self.store.set_profile_identity(p['profile_id'], p.get('prev_identity'),
                                            p.get('prev_source', ''))
        elif a == 'name_profile':
            # Undoing an automatic name = "that name is wrong": clear it and
            # keep automation from putting it straight back.
            self.store.set_profile_identity(p['profile_id'], None, 'human:cleared')
        elif a == 'merge':
            self.store.move_overs(p['moved'], p['drop'])
            self.store.set_merged(p['drop'], None)
            if p.get('took_identity'):
                self.store.set_profile_identity(p['keep'], None, p.get('keep_prev_source', ''))
            self._recompute(p['keep'], p['drop'])
        elif a == 'move_over':
            self.store.move_overs([p['utt_id']], p['frm'])
            if p.get('created'):
                self.store.set_merged(p['to'], p['frm'])     # retire the voice it created
            self._recompute(p['frm'], p['to'])
        elif a == 'not_same':
            self.store.remove_cannot_link(p['a'], p['b'])
        elif a == 'keep_label':
            pass                                   # only the dismissal; undone = raised again
        elif a == 'split_off':
            self.store.move_overs(p['utt_ids'], p['frm'])
            self.store.remove_cannot_link(p['frm'], p['to'])
            self.store.set_merged(p['to'], p['frm'])          # retire the voice it made
            self._recompute(p['frm'])
        elif a == 'reassign':
            from speaker_id.recluster import undo_reassign
            undo_reassign(self.store, p)
            self._recompute(*{m[1] for m in p['moves'] if m[1]}, *{m[2] for m in p['moves']})
        elif a == 'release':
            from speaker_id.recluster import undo_release
            undo_release(self.store, p)
            self._recompute(*{r[1] for r in p['releases']})
        elif a == 'note':
            self.store.update_identity(p['identity_id'], notes=p.get('prev', ''))
        else:
            raise CurationError(f"'{a}' cannot be undone")
        self.store.audit(ACTOR, 'undo', of=row['id'], undid=a, at=time.time())
        return {'undone': row['id'], 'undid': a}
