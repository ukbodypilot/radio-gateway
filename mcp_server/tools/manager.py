"""Fleet Manager MCP tools — scheduled Claude-driven fleet health checks.

The manager engine (manager_engine.py) runs hourly and daily checks by
collecting a system snapshot and handing it to a one-shot `claude -p` run;
each run appends a report to manager_reports.jsonl and can raise an unread
alert. The /manager web page has driven all of this since 2026-05-18 with
no MCP coverage at all — these tools close that gap.

Note the recursion hazard on manager_run: it starts a *Claude* task. If you
are yourself a Claude session, prefer reading the reports over triggering
new runs, and never call it in a loop.
"""

import json
import time

from mcp_server.server import mcp, _get, _post


def _fmt_ts(ts):
    """Format a timestamp for display.

    Two formats are in play: manager_state.json carries epoch seconds
    (last_run_ts), while the report lines in manager_reports.jsonl carry
    ISO8601 Zulu strings ('2026-07-28T12:00:01Z'). Accept either.
    """
    if ts in (None, '', 0):
        return '—'
    if isinstance(ts, str):
        # ISO8601 — show it as-is minus the T/Z noise.
        return ts.replace('T', ' ').replace('Z', ' UTC')
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return '—'
    if ts <= 0:
        return '—'
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


@mcp.tool()
def manager_status() -> str:
    """
    Get Fleet Manager status: whether scheduled checks are enabled, the
    check interval, the daily run time, whether a run is in flight, when
    the last hourly/daily run happened, and whether alerts are unread.
    """
    data = _get('/manager/status')
    if 'error' in data:
        return f"Error: {data['error']}"
    lines = [
        f"enabled          : {data.get('enabled', False)}",
        f"running now      : {data.get('running', False)}",
        f"check interval   : every {data.get('check_interval_hours', '?')} h",
        f"daily run time   : {data.get('daily_time', '?')}",
        f"unread alerts    : {data.get('unread_alerts', False)}",
        f"last run type    : {data.get('last_run_type') or '—'}",
        f"last run at      : {_fmt_ts(data.get('last_run_ts'))}",
        f"last hourly slot : {data.get('last_check') or '—'}",
        f"last daily       : {data.get('last_daily') or '—'}",
    ]
    return '\n'.join(lines)


@mcp.tool()
def manager_reports(limit: int = 10, full: bool = False) -> str:
    """
    Read recent Fleet Manager run reports (newest last).

    Args:
        limit: How many recent reports to return (default 10, max 100).
        full:  False (default) returns a one-line summary per report;
               True returns the complete JSON including the report body.
    """
    data = _get('/manager/reports')
    if isinstance(data, dict) and 'error' in data:
        return f"Error: {data['error']}"
    if not isinstance(data, list):
        return "Error: unexpected response from /manager/reports"
    if not data:
        return "No manager reports yet."

    n = max(1, min(100, int(limit)))
    recent = data[-n:]
    if full:
        return json.dumps(recent, indent=2)

    lines = [f"{len(recent)} of {len(data)} reports (newest last):"]
    for r in recent:
        ts = _fmt_ts(r.get('ts'))
        task = r.get('task') or '?'
        sev = r.get('severity') or '?'
        summary = ' '.join(str(r.get('summary') or '').split())[:100]
        n_find = len(r.get('findings') or [])
        lines.append(f"  [{ts}] {task:6} {sev:8} ({n_find} findings) {summary}")
    lines.append("Use full=True for complete report bodies including findings.")
    return '\n'.join(lines)


@mcp.tool()
def manager_doc_read(name: str) -> str:
    """
    Read one of the Fleet Manager's task documents.

    Args:
        name: 'constitution' (SYSTEM_MANIFEST.md — the standing description
              of the fleet), 'hourly' (the hourly check task list), or
              'daily' (the daily check task list).

    These are the prompts the manager feeds to Claude on each run, so they
    are the place to look when a scheduled check is doing the wrong thing.
    """
    name = name.strip().lower()
    if name not in ('constitution', 'hourly', 'daily'):
        return "Error: name must be 'constitution', 'hourly', or 'daily'"
    data = _get(f'/manager/doc?name={name}')
    # /manager/doc returns raw text, not JSON — _get will report a parse
    # failure, so fall back to a direct fetch for this one route.
    if isinstance(data, dict) and 'error' in data:
        import urllib.request
        from mcp_server.server import GW_BASE_URL, _auth_headers
        req = urllib.request.Request(f'{GW_BASE_URL}/manager/doc?name={name}',
                                     headers=_auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            return f"Error reading '{name}': {e}"
    return json.dumps(data, indent=2)


@mcp.tool()
def manager_doc_write(name: str, content: str) -> str:
    """
    Overwrite one of the Fleet Manager's task documents.

    Args:
        name:    'constitution', 'hourly', or 'daily'.
        content: The complete new contents — this REPLACES the file, it does
                 not append. Read it with manager_doc_read first.
    """
    name = name.strip().lower()
    if name not in ('constitution', 'hourly', 'daily'):
        return "Error: name must be 'constitution', 'hourly', or 'daily'"
    result = _post('/manager/save', {'doc': name, 'content': content})
    if result.get('ok'):
        return f"Manager doc '{name}' saved ({len(content)} chars)"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def manager_toggle(enabled: bool) -> str:
    """
    Enable or disable scheduled Fleet Manager checks.

    Args:
        enabled: True to start the scheduler, False to stop it.

    Enabling resets the hourly/daily slot markers to now, so the first run
    lands one full interval later rather than firing immediately.
    """
    result = _post('/manager/toggle', {'enabled': bool(enabled)})
    if result.get('ok'):
        return f"Fleet Manager scheduled checks: {'ENABLED' if result.get('enabled') else 'DISABLED'}"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def manager_config(daily_time: str = '', check_interval_hours: int = 0) -> str:
    """
    Change the Fleet Manager schedule.

    Args:
        daily_time:           Time of the daily run as 'HH:MM' (24h). Omit
                              to leave unchanged.
        check_interval_hours: Hours between hourly-style checks — one of
                              1, 2, 4, 6, 8, 12. Omit to leave unchanged.
    """
    payload = {}
    if daily_time.strip():
        t = daily_time.strip()
        parts = t.split(':')
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return "Error: daily_time must be 'HH:MM'"
        if not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            return "Error: daily_time out of range"
        payload['daily_time'] = t
    if check_interval_hours:
        if check_interval_hours not in (1, 2, 4, 6, 8, 12):
            return "Error: check_interval_hours must be 1, 2, 4, 6, 8, or 12"
        payload['check_interval_hours'] = check_interval_hours
    if not payload:
        return "Nothing to change — pass daily_time and/or check_interval_hours"
    result = _post('/manager/config', payload)
    if result.get('ok'):
        return f"Fleet Manager schedule updated: {json.dumps(payload)}"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def manager_ack() -> str:
    """
    Acknowledge Fleet Manager alerts — clears the unread_alerts flag that
    lights up the /manager nav badge. Does not delete any reports.
    """
    result = _post('/manager/ack', {})
    if result.get('ok'):
        return "Fleet Manager alerts acknowledged"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def manager_run(task: str = 'hourly') -> str:
    """
    Trigger a Fleet Manager run immediately, outside the schedule.

    Args:
        task: 'hourly' (default) or 'daily'.

    CAUTION — this spawns a one-shot `claude -p` run and hands it the fleet
    snapshot; it is not a cheap status read. The call
    returns as soon as the run is dispatched, not when it finishes: poll
    manager_status() for running=False, then manager_reports() for the
    result. Do not call this from an automated loop.
    """
    task = task.strip().lower()
    if task not in ('hourly', 'daily'):
        return "Error: task must be 'hourly' or 'daily'"
    result = _post('/manager/run', {'task': task})
    if result.get('ok'):
        return (f"Fleet Manager '{task}' run dispatched. It runs in the "
                f"background — poll manager_status() for running=False, then "
                f"manager_reports() for the outcome.")
    return f"Failed: {result.get('error', 'unknown')}"
