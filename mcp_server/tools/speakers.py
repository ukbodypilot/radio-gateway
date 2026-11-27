"""Speaker ID MCP tools: who is talking, the review queue, and curating voices (label, merge, undo).

Read through the same /speakers/api endpoints as the web page. Fingerprints
(embeddings) never leave the gateway; these tools return names, callsigns,
counts, evidence and transcripts only.
"""

import time

from mcp_server.server import mcp, _get, _post


def _ago(ts):
    if not ts:
        return '?'
    s = time.time() - ts
    return f'{s / 60:.0f} min ago' if s < 5400 else f'{s / 3600:.0f} h ago' if s < 129600 else f'{s / 86400:.0f} d ago'


def _who(p):
    return ' · '.join(x for x in (p.get('callsign'), p.get('name')) if x) or f"Voice #{p['id']}"


@mcp.tool()
def speakers_list(limit: int = 30, unnamed_only: bool = False) -> str:
    """
    List the voices speaker ID has learned from transcribed overs, busiest
    first: who each is (callsign/name if known, and whether a human or the
    callsign evidence named it), overs heard, channels, last heard, and what
    the callsign evidence currently points at.
    """
    d = _get('/speakers/api/profiles')
    if not d.get('ok'):
        return f"Speaker ID unavailable: {d.get('error', 'unknown')}"
    rows = [p for p in d.get('profiles', []) if not (unnamed_only and p.get('identity_id'))]
    st = d.get('status') or {}
    named = sum(1 for p in d.get('profiles', []) if p.get('identity_id'))
    out = [f"{len(d.get('profiles', []))} voices, {named} named · model {st.get('model', '?')}"]
    for p in rows[:max(1, min(200, limit))]:
        src = (p.get('label_source') or '')
        tag = ' [you]' if src.startswith('human') and p.get('identity_id') else ' [auto]' if p.get('identity_id') else ''
        c = p.get('candidate')
        ev = f" · evidence: {c['callsign']} {c['weight']}x/{c['days']}d" if c else ''
        out.append(f"#{p['id']} {_who(p)}{tag} — {p['overs']} overs, {', '.join(p.get('buses') or [])}"
                   f", last {_ago(p.get('last'))}{ev}")
    return '\n'.join(out)


@mcp.tool()
def speaker_whois(query: str) -> str:
    """
    Look up one voice by callsign, name or voice number ("#12" or "12"):
    identity, FCC licence details, why the system thinks so (evidence), and
    its most recent transcribed overs.
    """
    d = _get('/speakers/api/profiles')
    if not d.get('ok'):
        return f"Speaker ID unavailable: {d.get('error', 'unknown')}"
    q = query.strip().lstrip('#').lower()
    hits = [p for p in d.get('profiles', [])
            if q == str(p['id']) or q == (p.get('callsign') or '').lower()
            or (q and q in (p.get('name') or '').lower())
            or (p.get('candidate') and q == p['candidate']['callsign'].lower())]
    if not hits:
        return f"No voice matches {query!r}."
    out = []
    for p in hits[:3]:
        v = _get(f"/speakers/api/profile?id={p['id']}")
        if not v.get('ok'):
            continue
        out.append(f"#{p['id']} {_who(p)} ({p.get('label_source') or 'unnamed'}) — {p['overs']} overs, "
                   f"{round((p.get('seconds') or 0) / 60)} min of speech, last heard {_ago(p.get('last'))}")
        lic = v.get('licence')
        if lic:
            out.append(f"  FCC: {lic['name']}, {lic['city']} {lic['state']}, class {lic.get('class') or '?'}"
                       f"{'' if lic.get('status') == 'A' else ' (NOT active)'}")
        for w in (v.get('verdict') or {}).get('why', []):
            out.append(f"  why: {w}")
        for o in (v.get('overs') or [])[:5]:
            out.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(o['ts']))} [{o['bus']}] {o.get('text') or '(no text)'}")
    return '\n'.join(out)


@mcp.tool()
def speaker_review(limit: int = 10) -> str:
    """
    The speaker-ID review queue: the questions where one answer settles the
    most traffic (confirm a callsign, resolve a conflict, "same person?",
    "who is this?"). Each line names the voices involved.
    """
    d = _get('/speakers/api/review')
    if not d.get('ok'):
        return f"Speaker ID unavailable: {d.get('error', 'unknown')}"
    items = d.get('items', [])
    if not items:
        return 'Nothing to review.'
    return '\n'.join(f"[{i['type']}] {i['prompt']}" for i in items[:max(1, min(50, limit))])


@mcp.tool()
def speaker_label(voice: int, callsign: str = '', name: str = '') -> str:
    """
    Label a voice with a callsign and/or name. Recorded exactly like a label
    made on the Speakers page: authoritative (automation never overrides it),
    audited, and undoable from /speakers. Only use when the user says who the
    voice is.
    """
    r = _post('/speakers/api/action', {'op': 'label', 'profile_id': int(voice),
                                       'callsign': callsign, 'name': name}, timeout=30)
    if r.get('ok'):
        return f"Voice #{voice} labelled {' · '.join(x for x in (callsign.upper(), name) if x)} (change #{r.get('audit_id')}, undo on /speakers)."
    return f"Failed: {r.get('error', 'unknown')}"


def _action(op, body, done):
    """POST one curation op; every op is audited and undoable (speaker_audit / speaker_undo)."""
    r = _post('/speakers/api/action', {'op': op, **body}, timeout=30)
    if r.get('ok'):
        return f"{done} (change #{r.get('audit_id')}; speaker_undo({r.get('audit_id')}) reverses it)."
    return f"Failed: {r.get('error', 'unknown')}"


@mcp.tool()
def speaker_unlabel(voice: int) -> str:
    """
    Remove a voice's callsign/name label (the voice and its overs stay). Use when
    the user says a label is wrong and doesn't know the right one.
    """
    return _action('unlabel', {'profile_id': int(voice)}, f"Voice #{voice} unlabelled")


@mcp.tool()
def speaker_merge(keep: int, drop: int) -> str:
    """
    Merge two voices that are the same person: every over of `drop` moves to
    `keep`, and `drop` is removed. Only when the user says they are the same.
    """
    return _action('merge', {'keep': int(keep), 'drop': int(drop)}, f"Voice #{drop} merged into #{keep}")


@mcp.tool()
def speaker_not_same(a: int, b: int) -> str:
    """
    Record that voices `a` and `b` are different people, so automatic
    clustering never merges them and the "same person?" card stops asking.
    """
    return _action('not_same', {'a': int(a), 'b': int(b)}, f"Voices #{a} and #{b} marked as different people")


@mcp.tool()
def speaker_note(voice: int, notes: str) -> str:
    """Set the free-text note on a voice (shown on the Speakers page). Empty clears it."""
    return _action('note', {'profile_id': int(voice), 'notes': notes}, f"Note on voice #{voice} saved")


@mcp.tool()
def speaker_audit(limit: int = 15) -> str:
    """
    Recent Speaker ID changes, newest first: user labels/merges and the automatic
    nightly/recluster work. Each row's id can be passed to speaker_undo.
    """
    r = _get('/speakers/api/audit')
    if not r.get('ok', True) or 'error' in r:
        return f"Error: {r.get('error', 'unknown')}"
    rows = r.get('rows') or []
    if not rows:
        return "No Speaker ID changes recorded"
    out = []
    for row in rows[:max(1, min(int(limit), 100))]:
        p = row.get('payload') or {}
        bits = ', '.join(f"{k}={v}" for k, v in p.items() if not isinstance(v, (list, dict)))[:140]
        out.append(f"#{row.get('id')} {_ago(row.get('ts'))} [{row.get('actor')}] {row.get('action')}: {bits}")
    return '\n'.join(out)


@mcp.tool()
def speaker_undo(change_id: int) -> str:
    """
    Undo one Speaker ID change by its id (from speaker_audit, or the id a label/
    merge/unlabel reported). Only when the user asks to reverse it.
    """
    return _action('undo', {'audit_id': int(change_id)}, f"Change #{change_id} undone")
