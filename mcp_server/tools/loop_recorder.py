"""Loop recorder MCP tools — bus-by-bus continuous recording, playback,
summary, activity timeline, export, archive/delete-all, and the test loop.

Split out of mcp_server/tools/routing.py on 2026-05-30; tools registered
against the shared ``mcp`` instance via @mcp.tool() decorator side
effects on import.
"""

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def loop_recorder_status() -> str:
    """
    Get loop recorder status: which buses are recording, segment counts,
    disk usage, write rate, and retention settings.
    """
    result = _get('/loop/buses')
    if not result:
        return "Loop recorder: no buses recording"
    lines = ["Loop Recorder Status:", ""]
    total_mb = 0
    for b in result:
        segs = b.get('segments', 0)
        disk = b.get('disk_mb', 0)
        total_mb += disk
        active = "RECORDING" if b.get('active') else "stopped"
        ret = b.get('retention_hours', 24)
        dur = ''
        if segs > 0:
            dur_sec = b.get('latest', 0) - b.get('earliest', 0)
            h = int(dur_sec // 3600)
            m = int((dur_sec % 3600) // 60)
            dur = f" ({h}h {m}m)"
        disk_str = f"{disk:.1f} MB" if disk < 1024 else f"{disk/1024:.1f} GB"
        lines.append(f"  {b['id']}: {active}, {segs} segments{dur}, {disk_str}, retention {ret}h")
    total_str = f"{total_mb:.1f} MB" if total_mb < 1024 else f"{total_mb/1024:.1f} GB"
    lines.append(f"\n  Total disk: {total_str}")
    return "\n".join(lines)


@mcp.tool()
def loop_recorder_toggle(bus_id: str) -> str:
    """
    Toggle loop recording on/off for a bus.

    Args:
        bus_id: Bus ID (e.g., 'main', 'th9800', 'monitor')
    """
    result = _post('/routing/cmd', {'cmd': 'toggle_proc', 'bus': bus_id, 'filter': 'loop'})
    if result.get('ok'):
        state = "enabled" if result.get('state') else "disabled"
        return f"Loop recording {state} on bus '{bus_id}'"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def loop_recorder_retention(bus_id: str, hours: int) -> str:
    """
    Set loop recorder retention window for a bus.

    Args:
        bus_id: Bus ID (e.g., 'main', 'th9800')
        hours:  Retention in hours (1-168, i.e., 1 hour to 7 days)
    """
    result = _post('/routing/cmd', {'cmd': 'set_loop_hours', 'bus': bus_id, 'hours': hours})
    if result.get('ok'):
        return f"Retention set to {result.get('hours')}h for bus '{bus_id}'"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def loop_recorder_summary(bus_id: str, hours: float = 2.0) -> str:
    """
    Summarize loop recorder activity for a bus over a time window.
    Reports total activity time, silence time, peak moment, and
    average signal level.

    Args:
        bus_id: Bus ID (e.g., 'main', 'th9800')
        hours:  How many hours back to analyze (default 2)
    """
    import time as _time
    from datetime import datetime
    end = _time.time()
    start = end - (hours * 3600)
    wfm = _get(f'/loop/waveform?bus={bus_id}&start={start}&end={end}')
    if not wfm or not wfm.get('peaks'):
        return f"No loop recorder data for bus '{bus_id}' in the last {hours}h"

    peaks = wfm['peaks']
    rms = wfm['rms']
    total_secs = len(peaks)
    active_secs = sum(1 for r in rms if r > 3)  # >3/255 ≈ above noise floor
    silence_secs = total_secs - active_secs

    # Find peak moment
    max_peak = max(peaks) if peaks else 0
    max_idx = peaks.index(max_peak) if max_peak > 0 else 0
    peak_epoch = wfm['start'] + max_idx
    peak_time = datetime.fromtimestamp(peak_epoch).strftime('%H:%M:%S')
    peak_db = round(20 * (2.718281828 ** 0) * ((max_peak / 255) or 0.001), 1)  # rough

    # Average RMS of active periods
    active_rms = [r for r in rms if r > 3]
    avg_rms = sum(active_rms) / len(active_rms) if active_rms else 0
    avg_pct = round(avg_rms / 255 * 100, 1)

    def _fmt(secs):
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = int(secs % 60)
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    lines = [
        f"Loop Recorder Summary: {bus_id} (last {hours}h)",
        f"  Total time:    {_fmt(total_secs)}",
        f"  Active audio:  {_fmt(active_secs)} ({round(active_secs/max(total_secs,1)*100)}%)",
        f"  Silence:       {_fmt(silence_secs)}",
        f"  Peak signal:   {max_peak}/255 at {peak_time}",
        f"  Avg level:     {avg_pct}% (of active periods)",
    ]
    return "\n".join(lines)


@mcp.tool()
def loop_recorder_activity(bus_id: str, hours: float = 2.0) -> str:
    """
    Show activity timeline for a bus — which time ranges had signal vs silence.
    Returns a list of active periods with start time, end time, and duration.

    Args:
        bus_id: Bus ID (e.g., 'main', 'th9800')
        hours:  How many hours back to analyze (default 2)
    """
    import time as _time
    from datetime import datetime
    end = _time.time()
    start = end - (hours * 3600)
    wfm = _get(f'/loop/waveform?bus={bus_id}&start={start}&end={end}')
    if not wfm or not wfm.get('rms'):
        return f"No loop recorder data for bus '{bus_id}' in the last {hours}h"

    rms = wfm['rms']
    wfm_start = wfm['start']
    threshold = 3  # >3/255 ≈ above noise floor

    # Find contiguous active regions (merge gaps < 3 seconds)
    periods = []
    in_active = False
    region_start = 0
    gap = 0
    for i, r in enumerate(rms):
        if r > threshold:
            if not in_active:
                region_start = i
                in_active = True
            gap = 0
        else:
            if in_active:
                gap += 1
                if gap > 3:  # 3s gap ends a region
                    periods.append((region_start, i - gap))
                    in_active = False
                    gap = 0
    if in_active:
        periods.append((region_start, len(rms) - 1))

    if not periods:
        return f"No audio activity on bus '{bus_id}' in the last {hours}h"

    def _t(idx):
        return datetime.fromtimestamp(wfm_start + idx).strftime('%H:%M:%S')
    def _dur(s, e):
        d = e - s
        if d >= 60:
            return f"{d//60}m {d%60}s"
        return f"{d}s"

    lines = [f"Activity Timeline: {bus_id} (last {hours}h)", ""]
    for s, e in periods:
        dur = e - s
        peak = max(rms[s:e+1]) if e > s else 0
        lines.append(f"  {_t(s)} — {_t(e)}  ({_dur(s, e)})  peak {peak}/255")

    lines.append(f"\n  {len(periods)} active period(s), {sum(e-s for s,e in periods)}s total")
    return "\n".join(lines)


@mcp.tool()
def loop_recorder_export(bus_id: str, start_time: str, end_time: str, format: str = "mp3") -> str:
    """
    Export a time range from the loop recorder to a file on disk.
    Returns the file path for the user to access.

    Args:
        bus_id:     Bus ID (e.g., 'main', 'th9800')
        start_time: Start time as HH:MM:SS (today) or epoch seconds
        end_time:   End time as HH:MM:SS (today) or epoch seconds
        format:     'mp3' or 'wav'
    """
    import json
    from datetime import datetime

    # Parse times
    def _parse(t):
        try:
            return float(t)
        except ValueError:
            pass
        parts = t.split(':')
        if len(parts) >= 2:
            now = datetime.now()
            h = int(parts[0])
            m = int(parts[1])
            s = int(parts[2]) if len(parts) > 2 else 0
            return now.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
        return None

    start_epoch = _parse(start_time)
    end_epoch = _parse(end_time)
    if not start_epoch or not end_epoch:
        return "Error: could not parse times. Use HH:MM:SS or epoch seconds."
    if end_epoch <= start_epoch:
        return "Error: end time must be after start time."

    import urllib.request
    from mcp_server.server import GW_BASE_URL, GW_ROOT, _auth_headers
    try:
        req = urllib.request.Request(
            GW_BASE_URL + '/loop/export',
            data=json.dumps({
                'bus': bus_id, 'start': start_epoch,
                'end': end_epoch, 'format': format
            }).encode(),
            headers={**_auth_headers(), 'Content-Type': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        if resp.status != 200:
            return f"Error: server returned {resp.status}"

        # Save to recordings directory
        ext = 'wav' if format == 'wav' else 'mp3'
        st = datetime.fromtimestamp(start_epoch).strftime('%H%M%S')
        et = datetime.fromtimestamp(end_epoch).strftime('%H%M%S')
        import os
        out_dir = os.path.join(GW_ROOT, 'recordings')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'export_{bus_id}_{st}-{et}.{ext}')
        with open(out_path, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        size_kb = os.path.getsize(out_path) / 1024
        return f"Exported to: {out_path} ({size_kb:.0f} KB)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def loop_playback_control(action: str, bus_id: str = "", start_time: str = "") -> str:
    """
    Control server-side loop recorder playback through the routing system.
    Audio plays through the loop_playback source node to connected sinks.

    Args:
        action:     'play', 'stop', or 'status'
        bus_id:     Bus ID for play (e.g., 'main', 'th9800')
        start_time: Start time as HH:MM:SS (today) or epoch seconds (for play)
    """
    if action == 'status':
        result = _get('/loop/playback/status')
        if not result:
            return "Loop playback: not available"
        if result.get('playing'):
            import datetime
            pos = result.get('position', 0)
            t = datetime.datetime.fromtimestamp(pos).strftime('%H:%M:%S')
            return f"Loop playback: playing {result.get('bus')} @ {t}"
        return "Loop playback: stopped"

    if action == 'stop':
        result = _post('/loop/playback', {'action': 'stop'})
        return "Playback stopped" if result.get('ok') else f"Error: {result.get('error', 'unknown')}"

    if action == 'play':
        if not bus_id:
            return "Error: bus_id required for play"
        # Parse start time
        from datetime import datetime
        try:
            start_epoch = float(start_time)
        except (ValueError, TypeError):
            parts = start_time.split(':')
            if len(parts) >= 2:
                now = datetime.now()
                h, m = int(parts[0]), int(parts[1])
                s = int(parts[2]) if len(parts) > 2 else 0
                start_epoch = now.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
            else:
                return "Error: start_time must be HH:MM:SS or epoch seconds"
        result = _post('/loop/playback', {'action': 'play', 'bus': bus_id, 'start': start_epoch})
        if result.get('ok'):
            return f"Playing {bus_id} from {start_time}"
        return f"Error: {result.get('error', 'unknown')}"

    return f"Error: unknown action '{action}'"


@mcp.tool()
def loop_recorder_delete_all() -> str:
    """
    Delete ALL loop recordings across all buses. This is irreversible.
    """
    result = _post('/loop/delete_all', {})
    if result.get('ok'):
        return f"Deleted {result.get('deleted', 0)} files from all buses"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def loop_recorder_archive_all() -> str:
    """
    Archive all loop recordings to a timestamped folder under
    recordings/loop_archive/. Files are moved (not copied), clearing
    the live recorder.
    """
    result = _post('/loop/archive_all', {})
    if result.get('ok'):
        return f"Archived to: {result.get('path')}"
    return f"Error: {result.get('error', 'no recordings to archive')}"


@mcp.tool()
def loop_recorder_download_all() -> str:
    """
    Download all loop recordings as a single ZIP file.
    Saves to the recordings/ directory.
    """
    import urllib.request, os
    from datetime import datetime
    from mcp_server.server import GW_BASE_URL, GW_ROOT, _auth_headers
    try:
        req = urllib.request.Request(
            GW_BASE_URL + '/loop/download_all',
            method='POST',
            data=b'',
            headers=_auth_headers(),
        )
        resp = urllib.request.urlopen(req, timeout=300)
        if resp.status != 200:
            return f"Error: server returned {resp.status}"
        out_dir = os.path.join(GW_ROOT, 'recordings')
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_path = os.path.join(out_dir, f'loop_all_{ts}.zip')
        with open(out_path, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        return f"Downloaded to: {out_path} ({size_mb:.1f} MB)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def test_loop_toggle() -> str:
    """
    Toggle the test loop — plays audio/loop.mp3 on repeat with PTT.
    Call again to stop.
    """
    result = _post('/testloop', {})
    if result.get('ok'):
        if result.get('looping'):
            return f"Test loop started: {result.get('file', 'loop.mp3')}"
        return "Test loop stopped"
    return f"Error: {result.get('error', 'unknown')}"
