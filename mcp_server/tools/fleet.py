"""Auto-extracted from gateway_mcp.py — tools registered against the shared
``mcp`` instance via @mcp.tool() decorator side effects on import.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from mcp_server.server import mcp, _get, _post, GW_BASE_URL, GW_ROOT, _auth_headers


# ---------------------------------------------------------------------------
# Tools — D75 Endpoint Management (SSH)
# ---------------------------------------------------------------------------
def _ssh_cmd(host: str, user: str, password: str, cmd: str, timeout: int = 15) -> str:
    """Run a command on a remote host via sshpass + SSH."""
    import subprocess
    try:
        r = subprocess.run(
            ['sshpass', '-p', password, 'ssh',
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'ConnectTimeout=5',
             f'{user}@{host}', cmd],
            capture_output=True, text=True, timeout=timeout)
        out = (r.stdout.strip() + '\n' + r.stderr.strip()).strip()
        if r.returncode == 0:
            return out or 'OK'
        return f"Exit {r.returncode}: {out}"
    except subprocess.TimeoutExpired:
        return f"SSH timeout ({timeout}s)"
    except FileNotFoundError:
        return "Error: sshpass not installed"
    except Exception as e:
        return f"SSH error: {e}"


def _resolve_endpoint_host(name: str) -> tuple:
    """Resolve endpoint name to (ip, user, password) from connected endpoints.
    Returns (host, 'user', 'user') or raises ValueError."""
    result = _get('/status')
    endpoints = result.get('link_endpoints', [])
    name_lower = name.lower().strip()
    for ep in endpoints:
        ep_name = ep.get('name', '').lower()
        ep_sid = ep.get('source_id', '').lower()
        if name_lower in (ep_name, ep_sid, ep_name.replace('-', '_'), ep_sid.replace('_', '-')):
            addr = ep.get('addr', '')
            if addr:
                host = addr.split(':')[0]
                return (host, 'user', 'user')
    # List available endpoints for error message
    names = [ep.get('name', '?') for ep in endpoints]
    raise ValueError(f"Endpoint '{name}' not found. Connected: {', '.join(names) or 'none'}")


@mcp.tool()
def endpoint_reboot(endpoint: str) -> str:
    """
    Reboot a remote endpoint Pi via SSH. Uses the IP from the active
    link connection — works with any connected endpoint.

    Args:
        endpoint: Endpoint name (e.g. 'd75-pi', 'ftm-150', 'celeron-aioc').
    """
    try:
        host, user, pw = _resolve_endpoint_host(endpoint)
    except ValueError as e:
        return str(e)
    result = _ssh_cmd(host, user, pw, 'sudo -n reboot', timeout=10)
    return f"Reboot sent to {endpoint} ({host}): {result}"


@mcp.tool()
def endpoint_ssh(
    command: str,
    endpoint: str,
) -> str:
    """
    Run a shell command on a remote endpoint Pi via SSH. Resolves the
    endpoint IP dynamically from the active link connection.

    Args:
        command:  Shell command to execute (e.g. 'uptime', 'free -h').
        endpoint: Endpoint name (e.g. 'd75-pi', 'ftm-150', 'celeron-aioc').
    """
    try:
        host, user, pw = _resolve_endpoint_host(endpoint)
    except ValueError as e:
        return str(e)
    return _ssh_cmd(host, user, pw, command)


@mcp.tool()
def endpoint_ping(endpoint: str) -> str:
    """
    Ping a remote endpoint to check if it's reachable. Resolves the
    endpoint IP dynamically from the active link connection.

    Args:
        endpoint: Endpoint name (e.g. 'd75-pi', 'ftm-150', 'celeron-aioc').
    """
    import subprocess
    try:
        host, _, _ = _resolve_endpoint_host(endpoint)
    except ValueError as e:
        return str(e)
    try:
        r = subprocess.run(['ping', '-c', '3', '-W', '2', host],
                          capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else f"Unreachable: {r.stdout.strip()}"
    except Exception as e:
        return f"Ping error: {e}"


@mcp.tool()
def endpoint_logs(endpoint: str = '', lines: int = 50) -> str:
    """
    Read recent stdout/stderr lines from a link endpoint, captured at the
    gateway via the in-protocol log shipper.

    Persists across endpoint reboots — useful for "why did the BT serial
    drop overnight?" investigations that the endpoint's local journal
    would have lost. See docs/endpoint_logs_design.md.

    Args:
        endpoint: Endpoint name (e.g. 'D75'). Omit to list endpoint names
                  that have logs available.
        lines:    How many recent lines to return (default 50, max 2000).
    """
    if not endpoint:
        data = _get('/api/endpoint_logs')
        names = data.get('endpoints', [])
        if not names:
            return "No endpoint logs yet — log shipper not yet running, or no endpoints connected since startup."
        return "Endpoints with logs:\n  " + "\n  ".join(names)
    n = max(1, min(2000, int(lines)))
    data = _get(f'/api/endpoint_logs?name={endpoint}&lines={n}')
    content = data.get('content', '')
    if not content:
        return f"No log content for endpoint '{endpoint}' (file empty or missing)."
    return content


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Packet Radio
# ---------------------------------------------------------------------------
@mcp.tool()
def packet_status() -> str:
    """
    Get packet radio (Direwolf TNC) status: mode, KISS connection,
    packet count, APRS stations heard, BBS state, and Pat Winlink status.
    """
    return json.dumps(_get('/packet/status'), indent=2)


@mcp.tool()
def packet_mode(mode: str) -> str:
    """
    Switch the packet radio TNC mode. This tells the remote endpoint to
    start/stop Direwolf and switches between audio and data mode.

    Args:
        mode: One of 'idle' (audio mode, no TNC), 'aprs' (APRS decode/beacon),
              'winlink' (Winlink email via Pat), 'bbs' (BBS connect mode).
    """
    mode = mode.lower().strip()
    if mode not in ('idle', 'aprs', 'winlink', 'bbs'):
        return f"Error: mode must be idle, aprs, winlink, or bbs"
    result = _post('/packet/mode', {'mode': mode})
    if result.get('ok'):
        return f"Packet mode set to: {mode}"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_aprs_stations() -> str:
    """
    List APRS stations heard by the packet radio TNC.
    Returns callsign, position, symbol, comment, and last-heard time.
    """
    data = _get('/packet/aprs_stations')
    stations = data.get('stations', {})
    if not stations:
        return "No APRS stations heard (is packet mode set to 'aprs'?)"
    lines = [f"{len(stations)} stations heard:"]
    for call, info in sorted(stations.items(), key=lambda x: x[1].get('last_heard', 0), reverse=True):
        lat = info.get('lat', '')
        lon = info.get('lon', '')
        pos = f" ({lat:.4f}, {lon:.4f})" if lat and lon else ""
        comment = info.get('comment', '')
        lines.append(f"  {call}{pos} — {comment[:60]}")
    return '\n'.join(lines)


@mcp.tool()
def packet_send_aprs(to: str, message: str) -> str:
    """
    Send an APRS message to a station.

    Args:
        to:      Destination callsign (e.g. 'N6ABC-7').
        message: Message text (max 67 characters for APRS).
    """
    if not to.strip():
        return "Error: destination callsign required"
    if not message.strip():
        return "Error: message required"
    result = _post('/packet/aprs_send', {
        'cmd': 'aprs_send', 'to': to.strip().upper(), 'message': message[:67]
    })
    if result.get('ok'):
        return f"APRS message sent to {to}: {message[:67]}"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_log(lines: int = 30) -> str:
    """
    Get recent Direwolf TNC log output.

    Args:
        lines: Number of recent lines to return (default 30).
    """
    data = _get('/packet/log')
    log_lines = data.get('lines', [])
    if not log_lines:
        return "No TNC log lines (packet mode may be idle)"
    return '\n'.join(log_lines[-lines:])


@mcp.tool()
def packet_decoded() -> str:
    """
    Get recently decoded packets from the TNC.
    Returns raw decoded APRS/AX.25 frames.
    """
    data = _get('/packet/packets')
    packets = data.get('packets', [])
    if not packets:
        return "No decoded packets"
    lines = [f"{len(packets)} decoded packets (newest last):"]
    for p in packets[-20:]:
        if isinstance(p, dict):
            lines.append(f"  {p.get('from', '?')}>{p.get('to', '?')}: {p.get('info', '')[:80]}")
        else:
            lines.append(f"  {str(p)[:100]}")
    if len(packets) > 20:
        lines.append(f"  ... {len(packets) - 20} older packets not shown")
    return '\n'.join(lines)


@mcp.tool()
def packet_aprs_beacon() -> str:
    """
    Send an APRS position beacon immediately, outside the scheduled beacon
    interval. Requires packet mode 'aprs'.
    """
    result = _post('/packet/aprs_beacon', {'cmd': 'aprs_beacon'})
    if result.get('ok'):
        return "APRS beacon sent"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_bbs_connect(callsign: str) -> str:
    """
    Open an AX.25 connection to a packet BBS. Requires packet mode 'bbs'
    (set it with packet_mode('bbs') first).

    Args:
        callsign: BBS callsign with SSID (e.g. 'N6ABC-1').
    """
    if not callsign.strip():
        return "Error: BBS callsign required"
    result = _post('/packet/bbs_connect',
                   {'cmd': 'bbs_connect', 'callsign': callsign.strip().upper()})
    if result.get('ok'):
        return (f"Connecting to BBS {callsign.strip().upper()} — "
                f"read the session with packet_bbs_buffer()")
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_bbs_disconnect() -> str:
    """Close the current packet BBS connection."""
    result = _post('/packet/bbs_disconnect', {'cmd': 'bbs_disconnect'})
    if result.get('ok'):
        return "BBS disconnected"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_bbs_send(text: str) -> str:
    """
    Send a line of text to the connected packet BBS — a command, a menu
    selection, or message body text, depending on where the session is.

    Args:
        text: The line to send. Read the reply with packet_bbs_buffer().
    """
    if not text.strip():
        return "Error: text required"
    result = _post('/packet/bbs_send', {'cmd': 'bbs_send', 'text': text})
    if result.get('ok'):
        return f"Sent to BBS: {text}"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_bbs_buffer(lines: int = 40) -> str:
    """
    Read the packet BBS session buffer — everything the BBS has sent back
    since the connection opened.

    Args:
        lines: Number of recent lines to return (default 40).
    """
    data = _get('/packet/bbs_buffer')
    buf = data.get('lines', [])
    if not buf:
        return "BBS buffer empty (not connected, or nothing received yet)"
    return '\n'.join(buf[-lines:])


@mcp.tool()
def packet_force_audio() -> str:
    """
    Force the packet endpoint back to audio mode. Recovery escape hatch for
    when the endpoint is stuck in a data mode with Direwolf holding the
    sound card — packet_mode('idle') is the graceful path; this one tells
    the endpoint directly.
    """
    result = _post('/packet/force_audio', {'cmd': 'force_audio'})
    if result.get('ok'):
        return "Endpoint forced back to audio mode"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def packet_set_endpoint(name: str = '') -> str:
    """
    Choose which connected link endpoint carries packet audio + PTT.

    Args:
        name: Endpoint name (e.g. 'celeron_aioc'). Pass '' to clear the
              preference and go back to auto-select.

    Any endpoint advertising the 'packet' capability flag is eligible —
    selection is a capability choice, not a rewiring.
    """
    result = _post('/packet/setendpoint', {'name': name.strip()})
    if result.get('ok'):
        active = result.get('endpoint_active')
        pref = result.get('endpoint_pref') or '(auto)'
        if not active:
            return (f"Preference set to {pref}, but no packet-capable endpoint "
                    f"is currently connected")
        return f"Packet endpoint preference: {pref} → active: {active}"
    return f"Failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Winlink Email
# ---------------------------------------------------------------------------
@mcp.tool()
def winlink_messages(folder: str = 'in') -> str:
    """
    List Winlink email messages.

    Args:
        folder: Mailbox folder — 'in' (inbox), 'out' (outbox/drafts),
                'sent' (sent messages). Default 'in'.
    """
    folder = folder.lower().strip()
    if folder not in ('in', 'out', 'sent'):
        return "Error: folder must be 'in', 'out', or 'sent'"
    data = _get(f'/packet/winlink/messages?folder={folder}')
    messages = data.get('messages', [])
    if not messages:
        return f"No messages in {folder}"
    lines = [f"{len(messages)} messages in '{folder}':"]
    for m in messages:
        lines.append(
            f"  {m.get('date', '?'):16s}  {m.get('from', '?'):12s} → {m.get('to', '?'):12s}  "
            f"{m.get('subject', '(no subject)')[:50]}"
        )
    return '\n'.join(lines)


@mcp.tool()
def winlink_read(message_id: str, folder: str = 'in') -> str:
    """
    Read a specific Winlink email message.

    Args:
        message_id: Message ID (from winlink_messages output).
        folder:     Folder the message is in — 'in', 'out', 'sent'.
    """
    data = _get(f'/packet/winlink/read?folder={folder}&id={message_id}')
    if data.get('error'):
        return f"Error: {data['error']}"
    msg = data.get('message', data)
    lines = [
        f"From:    {msg.get('from', '?')}",
        f"To:      {msg.get('to', '?')}",
        f"Date:    {msg.get('date', '?')}",
        f"Subject: {msg.get('subject', '(none)')}",
        f"",
        msg.get('body', '(empty)'),
    ]
    return '\n'.join(lines)


@mcp.tool()
def winlink_compose(to: str, subject: str, body: str) -> str:
    """
    Compose and queue a Winlink email message. The message is saved to the
    outbox and will be sent on the next winlink_connect sync.

    Args:
        to:      Recipient email or callsign (e.g. 'user@example.com' or 'N6ABC').
        subject: Email subject line.
        body:    Email body text.
    """
    if not to.strip() or not subject.strip():
        return "Error: 'to' and 'subject' are required"
    result = _post('/packet/winlink/compose', {
        'to': to.strip(),
        'subject': subject.strip(),
        'body': body,
    })
    if result.get('ok'):
        return f"Message queued to {to}: \"{subject}\" — sync with winlink_connect to send"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def winlink_connect(gateway_callsign: str = '') -> str:
    """
    Connect to a Winlink gateway via packet radio to send/receive email.
    Uses Pat CLI with AGW protocol through the remote Direwolf TNC.

    Args:
        gateway_callsign: Winlink gateway callsign to connect to
                          (e.g. 'KM6RTE-12'). Leave empty to use the
                          last-used or nearest gateway.
    """
    payload = {}
    if gateway_callsign.strip():
        payload['gateway'] = gateway_callsign.strip().upper()
    result = _post('/packet/winlink/connect', payload, timeout=30)
    if result.get('ok'):
        return f"Winlink connect initiated" + (f" to {gateway_callsign}" if gateway_callsign else "")
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def winlink_gateways() -> str:
    """
    List nearby Winlink packet radio gateways from Pat's cached RMS list.
    Shows callsign, frequency, distance, and grid square.
    """
    data = _get('/packet/winlink/gateways')
    if data.get('error'):
        return f"Error: {data['error']}"
    gws = data.get('gateways', [])
    if not gws:
        return "No Winlink gateways found"
    lines = [f"{len(gws)} nearby Winlink packet gateways:"]
    for g in gws[:20]:
        lines.append(
            f"  {g.get('callsign', '?'):12s} {g.get('frequency', '?'):>10s} MHz  "
            f"{g.get('distance', '?'):>5s} km  {g.get('grid', '')}"
        )
    return '\n'.join(lines)


@mcp.tool()
def winlink_log() -> str:
    """
    Get the Winlink connection log from the most recent Pat sync session.
    Shows connect progress, message transfer, and any errors.
    """
    data = _get('/packet/winlink/log')
    log = data.get('log', '')
    if not log:
        return "No Winlink connection log available"
    return log


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Stream Trace (Audio Quality Diagnostics)
# ---------------------------------------------------------------------------
@mcp.tool()
def stream_trace_toggle() -> str:
    """
    Toggle the per-stream audio chunk trace on or off. When active, records
    every audio handoff with timing, RMS, queue depth, and anomalies.
    When stopped, dumps analysis to tools/stream_trace.txt.
    Separate from the main audio trace (audio_trace_toggle).
    """
    url = GW_BASE_URL + '/tracecmd'
    body = b'type=stream'
    headers = {**_auth_headers(), 'Content-Type': 'application/x-www-form-urlencoded'}
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return f"Error: {e}"
    active = result.get('active', False)
    return f"Stream trace {'STARTED' if active else 'STOPPED and dumped to tools/stream_trace.txt'}"


@mcp.tool()
def stream_trace_read(lines: int = 100) -> str:
    """
    Read the most recent stream trace dump from tools/stream_trace_*.txt.
    Shows per-stream timing statistics, interval analysis, and anomalies.

    Args:
        lines: Number of lines to return from the trace file (default 100).
    """
    import glob
    # tools/ lives two levels above mcp_server/tools/ (i.e. at the gateway root)
    gateway_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    tools_dir = os.path.join(gateway_root, 'tools')
    matches = sorted(glob.glob(os.path.join(tools_dir, 'stream_trace_*.txt')))
    if not matches:
        return "No stream trace file found — run stream_trace_toggle to capture one"
    trace_path = matches[-1]  # most recent by filename timestamp
    try:
        with open(trace_path) as f:
            all_lines = f.readlines()
        if not all_lines:
            return f"Stream trace file is empty: {os.path.basename(trace_path)}"
        header = f"[{os.path.basename(trace_path)}]\n"
        return header + ''.join(all_lines[:lines])
    except Exception as e:
        return f"Error reading trace: {e}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Sink drain stats (v3.5-A off-tick sinks)
# ---------------------------------------------------------------------------
@mcp.tool()
def bus_sink_stats() -> str:
    """
    Per-sink off-tick drain queue stats. Use when audio drops on a converted
    sink (broadcastify, mumble, automation_recorder, echolink_legacy) — the
    bus tick won't show the stall because the call now runs on a peer thread.

    Returned per sink:
    - enqueued / drops / drained / errors: counters since process start
    - depth_now / depth_max: current and peak queue depth (max queue size 8)
    - drain_avg_ms / drain_max_ms: drain-thread timing of the actual sink call
    - idle_s: seconds since last successful send (None if never sent)
    - thread_alive: whether the SinkDrain-* thread is still running

    Smoking guns:
    - drops > 0 → consumer too slow; oldest audio discarded
    - drain_max_ms in seconds → the sink stalled (network, pipe, etc.)
    - idle_s rising while enqueued keeps growing → drain thread is stuck
    - thread_alive False → BusManager.stop() didn't recreate the thread
    """
    data = _get('/sinkstats')
    if not data:
        return "No sink stats — bus_manager not running or no sinks active"
    lines = []
    for sink_id in sorted(data.keys()):
        s = data[sink_id]
        idle_s = s.get('idle_s')
        idle_str = f"{idle_s:.1f}s" if isinstance(idle_s, (int, float)) else "—"
        lines.append(
            f"{sink_id}: "
            f"enq={s['enqueued']} drained={s['drained']} drops={s['drops']} "
            f"err={s['errors']} depth={s['depth_now']}/{s['depth_max']} "
            f"avg={s['drain_avg_ms']:.2f}ms max={s['drain_max_ms']:.1f}ms "
            f"idle={idle_str} alive={s['thread_alive']}"
        )
    return "\n".join(lines) if lines else "No sinks active"


@mcp.tool()
def bus_source_stats() -> str:
    """
    Per-source get_audio() timing across the bus tick (v3.5-E.1).

    Use to identify which sources are spending real time in get_audio()
    per tick. KV4P (Opus decode + adaptive PLL resampler), file playback
    (decode-on-demand), web mic, mumble RX and EchoLink RX all currently
    do work inline; SDR / LoopPlayback / RemoteAudio already have async
    reader threads internally and just dequeue.

    Returned per source name:
    - calls / no_audio: total calls, fraction returning None
    - avg_ms / max_ms / total_ms: per-call mean, worst-single, cumulative
    - idle_s: seconds since last call

    Smoking guns:
    - max_ms in tens of ms → that source can stall the bus tick
    - avg_ms × calls/sec × buses_consuming → real tick CPU contributor
    """
    data = _get('/sourcestats')
    if not data:
        return "No source stats — bus_manager not running or no sources active"
    lines = []
    for name in sorted(data.keys()):
        s = data[name]
        idle_s = s.get('idle_s')
        idle_str = f"{idle_s:.1f}s" if isinstance(idle_s, (int, float)) else "—"
        lines.append(
            f"{name}: "
            f"calls={s['calls']} no_audio={s['no_audio']} "
            f"avg={s['avg_ms']:.3f}ms max={s['max_ms']:.1f}ms "
            f"total={s['total_ms']:.0f}ms idle={idle_str}"
        )
    return "\n".join(lines) if lines else "No sources active"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Automation Scheme Management
# ---------------------------------------------------------------------------
@mcp.tool()
def automation_scheme_read() -> str:
    """
    Read the current automation scheme file. Shows all configured tasks
    with their schedules, actions, and options. The scheme file uses a
    simple text format: one task per line with schedule, action, and options.
    """
    # Find scheme file path from config
    cfg_path = os.path.join(GW_ROOT, 'gateway_config.txt')
    scheme_file = 'automation_scheme.txt'
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('AUTOMATION_SCHEME_FILE'):
                    _, _, v = line.partition('=')
                    v = v.strip()
                    if v:
                        scheme_file = v
                    break
    if not os.path.isabs(scheme_file):
        scheme_file = os.path.join(GW_ROOT, scheme_file)
    if not os.path.isfile(scheme_file):
        return f"Scheme file not found: {scheme_file}"
    try:
        with open(scheme_file) as f:
            content = f.read()
        return f"Scheme file: {scheme_file}\n{'='*60}\n{content}"
    except Exception as e:
        return f"Error reading scheme: {e}"


@mcp.tool()
def automation_scheme_edit(content: str) -> str:
    """
    Replace the automation scheme file with new content, then reload.
    Use automation_scheme_read first to see the current scheme.

    The scheme format is one task per line:
      <name> <schedule> <action> [options...]

    Schedule types: every <duration>, at <HH:MM>, cron <expr>
    Actions: tune, record, announce, scan, beacon, sleep

    Example:
      weather  every 30m  announce  prompt="current weather" voice=1
      scan_2m  every 1h   scan      band=2m duration=60s

    Args:
        content: The complete new scheme file content.
    """
    cfg_path = os.path.join(GW_ROOT, 'gateway_config.txt')
    scheme_file = 'automation_scheme.txt'
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('AUTOMATION_SCHEME_FILE'):
                    _, _, v = line.partition('=')
                    v = v.strip()
                    if v:
                        scheme_file = v
                    break
    if not os.path.isabs(scheme_file):
        scheme_file = os.path.join(GW_ROOT, scheme_file)
    try:
        with open(scheme_file, 'w') as f:
            f.write(content)
    except Exception as e:
        return f"Error writing scheme: {e}"
    # Reload via gateway API
    result = _post('/automationcmd', {'cmd': 'reload'})
    if result.get('ok'):
        return f"Scheme saved and reloaded: {result.get('tasks', 0)} tasks"
    return f"Scheme saved to {scheme_file} but reload failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Endpoint Management
# ---------------------------------------------------------------------------
@mcp.tool()
def endpoint_battery(endpoint: str = '') -> str:
    """
    Get battery status for a link endpoint (if it has a battery monitor).

    Args:
        endpoint: Endpoint name (e.g. 'd75-pi'). Omit to show all endpoints.
    """
    result = _get('/status')
    endpoints = result.get('link_endpoints', [])
    if not endpoints:
        return "No link endpoints connected"
    lines = []
    for ep in endpoints:
        name = ep.get('name', '?')
        if endpoint and endpoint.lower() not in (name.lower(), name.lower().replace('-', '_')):
            continue
        status = ep.get('endpoint_status', {})
        cpu = status.get('cpu_pct', '?')
        ram = status.get('ram_pct', '?')
        temp = status.get('cpu_temp_c', '?')
        disk = status.get('disk_pct', '?')
        ver = status.get('code_version', '?')
        uptime = status.get('uptime', 0)
        h, m = int(uptime) // 3600, (int(uptime) % 3600) // 60
        lines.append(f"[{name}] CPU:{cpu}% RAM:{ram}% Temp:{temp}C Disk:{disk}% "
                     f"Up:{h}h{m:02d}m v={ver}")
    return '\n'.join(lines) if lines else f"Endpoint '{endpoint}' not found"


@mcp.tool()
def endpoint_version() -> str:
    """
    Show code version for the gateway and all connected endpoints.
    Highlights version mismatches.
    """
    gw = _get('/api/endpoint/version')
    gw_ver = gw.get('version', '?')
    lines = [f"Gateway: v={gw_ver}"]
    result = _get('/status')
    for ep in result.get('link_endpoints', []):
        name = ep.get('name', '?')
        ep_ver = ep.get('endpoint_status', {}).get('code_version', '?')
        match = 'OK' if ep_ver == gw_ver else 'MISMATCH'
        lines.append(f"  {name}: v={ep_ver} [{match}]")
    return '\n'.join(lines)


@mcp.tool()
def pihole_status() -> str:
    """
    Get Pi-hole DNS ad-blocker status: queries, blocked, top clients.
    Pi-hole runs on the gateway at port 8089.
    """
    import urllib.request
    try:
        req = urllib.request.Request('http://127.0.0.1:8089/admin/api/stats/summary')
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        queries = data.get('queries', {})
        total = queries.get('total', 0)
        blocked = queries.get('blocked', 0)
        pct = queries.get('percent_blocked', 0)
        clients = data.get('clients', {}).get('total', 0)
        return (f"Pi-hole: {total} queries, {blocked} blocked ({pct:.1f}%), "
                f"{clients} clients")
    except Exception as e:
        return f"Pi-hole status unavailable: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    mcp.run(transport='stdio')


@mcp.tool()
def trace_status() -> str:
    """
    Report whether the audio trace and the watchdog trace are currently
    recording. Read this before audio_trace_toggle / stream_trace_toggle,
    which only flip the state and cannot tell you which way it went.
    """
    data = _get('/tracestatus')
    if 'audio_trace' not in data:
        return f"Error: {data.get('error', 'unexpected response')}"
    return (f"audio trace   : {'RECORDING' if data['audio_trace'] else 'off'}\n"
            f"watchdog trace: {'RECORDING' if data.get('watchdog_trace') else 'off'}")
