"""Offload worker MCP tools: the remote denoise / TTS worker pool and per-bus denoise.

Workers (docs/offload-workers.md) register themselves with the gateway every
~30 s (ttl 90 s); nothing about them is in the config. A worker missing here is
a registration problem on the worker's machine. A bus whose worker is down runs
DRY (unprocessed), not silent.
"""

from mcp_server.server import mcp, _get


def _ms(v):
    return f"{v:.0f}ms" if isinstance(v, (int, float)) else '-'


@mcp.tool()
def offload_workers_status() -> str:
    """
    Remote offload workers (denoise DP8, Kokoro TTS) registered with the gateway:
    machine, engine, model loaded, load/temperature, registration age, plus the
    per-bus remote-denoise link counters (ok / timeout / down, round trip).
    Transcription workers are separate: see transcription_workers.
    """
    d = _get('/workers')
    if 'error' in d and 'pool' not in d:
        return f"Error: {d['error']}"
    lines = []
    for kind, pool in (d.get('pool') or {}).items():
        ws = pool.get('workers') or []
        st = pool.get('stats') or {}
        lines.append(f"{kind}: {len(ws)} worker(s)  (registrations accepted={st.get('accepted', 0)} "
                     f"rejected={st.get('rejected', 0)} expired={st.get('expired', 0)})")
        for w in ws:
            m = w.get('meta') or {}
            extra = ''
            if 'max_streams' in m:
                extra = f" streams={m.get('streams', 0)}/{m['max_streams']}"
            if 'requests' in m:
                extra += f" requests={m['requests']} errors={m.get('errors', 0)}"
            lines.append(f"  {w.get('name')} {w.get('url')}  engine={m.get('engine', '?')} "
                         f"loaded={m.get('model_loaded')} load={m.get('loadavg', '?')} "
                         f"temp={m.get('cpu_temp_c', '?')}C seen {w.get('age_s', 0):.0f}s ago{extra}")
        if not ws:
            lines.append("  (none registered: check the worker service on its machine)")
    links = d.get('denoise') or []
    if links:
        lines.append("Remote denoise per bus:")
        for b in links:
            c = b.get('counts') or {}
            state = 'connected' if b.get('connected') else 'not connected'
            lines.append(f"  {b.get('bus')}: {b.get('worker') or '-'} {state}  ok={c.get('ok', 0)} "
                         f"timeout={c.get('timeout', 0)} down={c.get('down', 0)} err={c.get('error', 0)}  "
                         f"rtt avg {_ms(b.get('rtt_ms_avg'))} max {_ms(b.get('rtt_ms_max'))}"
                         + (f"  last error: {b['last_error']}" if b.get('last_error') else ''))
    tts = d.get('tts')
    if isinstance(tts, dict) and tts:
        lines.append("TTS offload: " + ', '.join(f"{k}={v}" for k, v in tts.items()
                                                 if not isinstance(v, (dict, list))))
    return '\n'.join(lines) or "No offload workers"


@mcp.tool()
def denoise_status() -> str:
    """
    Per-bus neural denoise (DP8) as on the Audio -> Denoise page: on/off, engine
    (local or remote), wet mix, bypass level, chunks sent vs bypassed (quiet
    audio below bypass_db skips the model), late/discarded chunks, processing
    time, local CPU cores used, and the remote worker link.
    Change settings with bus_toggle_processing / bus_set_denoise_mix /
    bus_set_denoise_bypass.
    """
    d = _get('/denoise/status')
    if 'error' in d and 'busses' not in d:
        return f"Error: {d['error']}"
    rows = d.get('busses') or []
    if not rows:
        return "No buses"
    lines = []
    for b in rows:
        if not b.get('enabled'):
            lines.append(f"{b.get('id')}: denoise off")
            continue
        r = b.get('remote') or {}
        where = ''
        if r:
            where = f"  worker={r.get('worker') or '-'} {'connected' if r.get('connected') else 'NOT connected'}"
        lines.append(f"{b.get('id')}: {b.get('engine')} mix={b.get('mix')} bypass<{b.get('bypass_db')}dB  "
                     f"sent={b.get('sent', 0)} bypassed={b.get('bypassed', 0)} late={b.get('late', 0)} "
                     f"discarded={b.get('discarded', 0)} avg {_ms(b.get('ms_avg'))} "
                     f"local cores={b.get('local_cores', 0)}{where}")
    return '\n'.join(lines)
