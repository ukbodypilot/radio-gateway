"""Transcription MCP tools — status, runtime config, log query/recent.

Split out of mcp_server/tools/routing.py on 2026-05-30; tools registered
against the shared ``mcp`` instance via @mcp.tool() decorator side
effects on import.
"""

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def transcription_status() -> str:
    """
    Get live transcription status: model, mode, VAD state, performance stats,
    and recent transcription results.
    """
    result = _get('/transcriptions?since=0')
    status = result.get('status', {})
    results = result.get('results', [])
    lines = []
    lines.append(f"Mode: {status.get('mode', '?')}  Model: {status.get('model', '?')}  Enabled: {status.get('enabled', '?')}")
    lines.append(f"Loaded: {status.get('model_loaded', False)}  VAD: {status.get('vad_db', -100):.0f}dB (thresh {status.get('vad_threshold', '?')})")
    lines.append(f"Total: {status.get('total_transcriptions', 0)}  Pending: {status.get('pending', 0)}")
    stats = status.get('stats', {})
    if stats.get('count', 0) > 0:
        lines.append(f"Perf: avg {stats.get('avg_ratio', '?')}x realtime, {stats.get('realtime_pct', '?')}% under realtime")
    # Per-bus stream health — vad_prob/vad_db, so you can see which bus is firing
    _streams = status.get('streams') or []
    if _streams:
        lines.append("Streams:")
        for s in _streams:
            _open = 'OPEN' if s.get('vad_open') else 'idle'
            lines.append(f"  {s.get('id','?'):<12} {_open:<4}  vad_prob={s.get('vad_prob',0):.2f}  "
                         f"vad_db={s.get('vad_db',-100):.0f}  upstream={s.get('upstream') or '-'}")
    # Feed-worker health: queue depth, drops, processing time distribution.
    # High dropped_full or enqueue_blocks means the worker can't keep up with
    # the bus tick rate — expect audio attribution jitter or missed utterances.
    _feed = status.get('feed') or {}
    if _feed:
        lines.append(f"Feed: qd={_feed.get('queue_depth',0)}/{_feed.get('queue_max',0)}  "
                     f"peak={_feed.get('peak_qd',0)}  enq={_feed.get('enqueued',0)}  "
                     f"proc={_feed.get('processed',0)}  drops={_feed.get('dropped_full',0)}  "
                     f"blocks>5ms={_feed.get('enqueue_blocks_gt_5ms',0)}  err={_feed.get('worker_errors',0)}")
        lines.append(f"Feed timing: last={_feed.get('proc_last_ms',0):.1f}ms  "
                     f"mean={_feed.get('proc_mean_ms',0):.1f}ms  max={_feed.get('proc_max_ms',0):.1f}ms")
        _ps = _feed.get('per_stream_mean_ms') or {}
        if _ps:
            lines.append("Per-bus mean proc time: " +
                         ', '.join(f"{k}={v:.1f}ms" for k, v in _ps.items()))
    if results:
        lines.append(f"\nRecent ({len(results)}):")
        for r in results[-10:]:
            p = ' [partial]' if r.get('partial') else ''
            lines.append(f"  [{r.get('time_str','')}] ({r.get('duration',0)}s) {r.get('text','')[:80]}{p}")
    return '\n'.join(lines)


@mcp.tool()
def transcription_workers() -> str:
    """
    List the transcription worker pool — local engine plus every remote
    worker, whether it was pinned in config or dialled in on its own, and
    when each self-registered worker was last heard from.

    Use this to answer "is macmini in the pool?" without SSHing anywhere.
    A worker that moved address should reappear here within its heartbeat
    interval with no config change.
    """
    result = _get('/transcriptions?since=0')
    status = result.get('status', {})
    workers = status.get('workers') or []
    reg = status.get('registration') or {}
    lines = [f"Mode: {status.get('mode', '?')}  Pool: {len(workers)} worker(s)"]
    if not workers:
        lines.append("  (empty)" + ("  — registration is enabled, waiting for workers"
                                    if reg.get('allowed') else ""))
    for w in workers:
        _kind = 'local' if w.get('type') == 'local' else (
            'self-reg' if w.get('registered') else 'config')
        _state = ('ready' if w.get('model_loaded') else 'loading')
        if w.get('type') == 'remote' and not w.get('reachable'):
            _state = 'UNREACHABLE'
        lines.append(
            f"  {w.get('name') or w.get('url') or 'local':<28} {_kind:<9} {_state:<12} "
            f"{w.get('model_key', '?')}  done={w.get('dispatched', 0)} "
            f"inflight={w.get('inflight', 0)}")
    lines.append(
        f"\nRegistration: {'enabled' if reg.get('allowed') else 'DISABLED'}  "
        f"ttl={reg.get('ttl_secs', '?')}s  registered={reg.get('count', 0)}")
    for rw in reg.get('workers') or []:
        lines.append(f"  {rw.get('name', '?'):<28} {rw.get('url', '?'):<32} "
                     f"last seen {rw.get('last_seen_secs', '?')}s ago")
    _c = reg.get('counters') or {}
    if _c:
        lines.append("Counters: " + '  '.join(f"{k}={v}" for k, v in _c.items()))
    return '\n'.join(lines)


@mcp.tool()
def transcription_config(
    key: str,
    value: str,
) -> str:
    """
    Change transcription settings at runtime.

    Args:
        key:   Setting to change — one of:
               'enabled'     — true/false (pause/resume without restart)
               'model'       — tiny/base (requires restart)
               'vad_threshold' — Silero probability 0.0–1.0 (default 0.5)
               'vad_hold'    — seconds, e.g. 1.0
               'min_duration' — seconds, e.g. 0.5
               'audio_boost' — percentage, e.g. 200
               'forward_mumble' — true/false
               'restart'     — restart transcriber with saved settings
               'clear'       — clear all results

               NOTE: denoise is a per-bus setting now — use
               bus_toggle_processing / bus_set_denoise_engine on the bus
               that feeds the transcription sink.
        value: The value to set (ignored for restart/clear).
    """
    if key in ('enabled', 'forward_mumble'):
        value = value.lower() in ('true', '1', 'yes')
    result = _post('/transcribe_config', {'key': key, 'value': value})
    if result.get('ok'):
        note = result.get('note', '')
        return f"Transcription {key} set" + (f' ({note})' if note else '')
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def transcription_log_query(question: str) -> str:
    """
    Search the persistent transcription log using plain English.

    Ask anything about what has been said on-air — e.g. "What was said on
    446.76 today?", "Any emergency traffic this week?", "Did anyone mention
    APRS?". The gateway translates the question to SQL, runs it against the
    SQLite log, then returns a plain-English summary.

    Args:
        question: Plain English question about radio traffic.
    """
    result = _post('/transcription/query', {'question': question})
    if result.get('ok'):
        return result.get('answer', 'No answer returned.')
    return f"Query failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def transcription_log_recent(limit: int = 20) -> str:
    """
    Return the most recent transcriptions from the persistent log.

    Args:
        limit: Number of entries to return (default 20, max 100).
    """
    limit = max(1, min(limit, 100))
    result = _get(f'/transcription/log?limit={limit}&offset=0')
    rows = result.get('rows', [])
    if not rows:
        return 'No transcriptions in log yet.'
    lines = []
    for r in rows:
        import datetime as _dt
        t = _dt.datetime.fromtimestamp(r['ts']).strftime('%H:%M:%S')
        src = r.get('source', '?').upper()
        freq = r.get('freq', '?')
        dur = f"{r['duration']:.1f}s" if r.get('duration') else '?'
        lines.append(f"[{t}] {src} {freq} ({dur}) {r.get('text', '')}")
    return '\n'.join(lines)


@mcp.tool()
def transcription_search(query: str, limit: int = 50) -> str:
    """
    Full-text search of the transcription log (SQLite FTS5). Newest first.
    FTS5 syntax works: quoted phrases, AND / OR / NOT, NEAR(a b), prefix*.
    Each hit says whether the loop recorder still holds the audio.

    Args:
        query: Search expression, e.g. 'weather AND warning' or '"net control"'.
        limit: Max hits, 1-500 (default 50).
    """
    import time
    import urllib.parse
    if not query.strip():
        return "Error: query is empty (use transcription_log_recent for the latest)"
    limit = max(1, min(500, int(limit)))
    data = _get('/transcript_search?' + urllib.parse.urlencode({'q': query, 'limit': limit}))
    if data.get('error'):
        return f"Search failed: {data['error']}"
    rows = data.get('results') or []
    if not rows:
        return f"No matches for {query!r}"
    lines = [f"{len(rows)} match(es) for {query!r}:"]
    for r in rows:
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.get('ts', 0)))
        audio = 'audio' if r.get('loop_available') else 'no audio'
        lines.append(f"  [{when}] {r.get('bus') or r.get('source', '?')} "
                     f"({r.get('duration', 0):.0f}s, {audio}): {r.get('text', '')}")
    return '\n'.join(lines)
