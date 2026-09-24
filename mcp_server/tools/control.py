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

from mcp_server.server import mcp, _get, _post, GW_BASE_URL, _auth_headers


# ---------------------------------------------------------------------------
# Tools — Status
# ---------------------------------------------------------------------------
@mcp.tool()
def gateway_status() -> str:
    """
    Get full gateway status: audio mixer state, connected radios, SDR receivers,
    Broadcastify stream health, active PTT, recording state, and duck/hold flags.
    This is the primary overview tool — call it first when diagnosing issues.
    """
    data = _get('/status')
    if 'error' in data and not data.get('ok', True):
        return f"Error reaching gateway: {data['error']}"
    return json.dumps(data, indent=2)


@mcp.tool()
def sdr_status() -> str:
    """
    Get SDR receiver status: whether rtl_airband is running, each channel's
    frequency and audio level, queue depth, and any error state.
    """
    return json.dumps(_get('/sdrstatus'), indent=2)


@mcp.tool()
def cat_status() -> str:
    """
    Get CAT (Computer-Aided Transceiver) radio status for the TH-9800 main radio:
    connected flag, current frequency, mode, VFO state, and serial link health.
    """
    return json.dumps(_get('/catstatus'), indent=2)


@mcp.tool()
def system_info() -> str:
    """
    Get host system info: CPU usage, memory, disk space, CPU temperature,
    and running service states (rtl_airband, liquidsoap, etc.).
    """
    return json.dumps(_get('/sysinfo'), indent=2)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — SDR control
# ---------------------------------------------------------------------------
@mcp.tool()
def sdr_tune(
    freq_mhz: float,
    channel: int = 1,
    squelch_db: float | None = None,
) -> str:
    """
    Retune an SDR receiver channel to a new frequency.  Restarts tuners (~8-12s).

    In dual mode: channel 1 or 2 tunes the corresponding tuner.
    In single mode: channel number is the 1-based index in the channel list.

    Args:
        freq_mhz:    Frequency in MHz (e.g. 118.1 for aircraft, 162.55 for NOAA weather).
        channel:     SDR channel number — 1 or 2 in dual mode, 1-8 in single mode (default 1).
        squelch_db:  Optional squelch threshold in dBFS (negative, e.g. -40.0).
                     Omit to keep current squelch.
    """
    # Check current mode
    status = _get('/sdrstatus')
    mode = status.get('sdr_mode', 'dual')

    if mode == 'single':
        channels = status.get('single_channels', [])
        idx = channel - 1
        if idx < 0 or idx >= len(channels):
            return json.dumps({'ok': False, 'error': f'Channel {channel} not found (have {len(channels)} channels)'})
        payload: dict = {'cmd': 'single_update_channel', 'index': idx, 'freq': freq_mhz}
        if squelch_db is not None:
            payload['squelch_threshold'] = int(squelch_db)
        result = _post('/sdrcmd', payload, timeout=20)
    else:
        freq_key = 'frequency' if channel == 1 else 'frequency2'
        squelch_key = 'squelch_threshold' if channel == 1 else 'squelch_threshold2'
        payload = {'cmd': 'tune', freq_key: freq_mhz}
        if squelch_db is not None:
            payload[squelch_key] = squelch_db
        result = _post('/sdrcmd', payload, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_set_mode(mode: str) -> str:
    """
    Switch SDR between dual-tuner and single-tuner mode.

    Dual mode: two independent tuners (master/slave), higher CPU, independent frequencies.
    Single mode: one tuner with multiple demodulated channels, lower CPU, frequencies
    must fit within the selected sample rate bandwidth.

    Args:
        mode: 'dual' for master/slave dual tuner, 'single' for one tuner with multiple channels.
    """
    result = _post('/sdrcmd', {'cmd': 'set_mode', 'mode': mode}, timeout=25)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_single_tune(
    centerfreq: float | None = None,
    sample_rate: float | None = None,
    channels: list | None = None,
) -> str:
    """
    Update single-mode SDR settings and restart. Only applies when SDR is in single mode.

    Args:
        centerfreq:   Center frequency in MHz (e.g. 446.70)
        sample_rate:  Sample rate / bandwidth in MHz (e.g. 0.5, 1.0, 2.0)
        channels:     List of channel dicts, each with 'freq' (MHz), 'modulation' ('nfm'/'am'),
                      'squelch_threshold' (dBFS, e.g. -26), and optional 'label'.
                      Example: [{"freq": 446.76, "modulation": "nfm", "squelch_threshold": -26, "label": "PMR 1"}]
    """
    payload: dict = {'cmd': 'single_tune'}
    if centerfreq is not None:
        payload['centerfreq'] = centerfreq
    if sample_rate is not None:
        payload['sample_rate'] = sample_rate
    if channels is not None:
        payload['channels'] = channels
    result = _post('/sdrcmd', payload, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_add_channel(
    freq: float,
    modulation: str = "nfm",
    squelch_db: int = -26,
    label: str = "",
) -> str:
    """
    Add a channel to single-mode SDR. Restarts the tuner.

    Args:
        freq:        Frequency in MHz (must fit within current bandwidth)
        modulation:  'nfm' or 'am' (default 'nfm')
        squelch_db:  Squelch threshold in dBFS (default -26)
        label:       Display label for the channel
    """
    payload: dict = {
        'cmd': 'single_add_channel',
        'freq': freq,
        'modulation': modulation,
        'squelch_threshold': squelch_db,
        'label': label,
    }
    result = _post('/sdrcmd', payload, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_remove_channel(index: int) -> str:
    """
    Remove a channel from single-mode SDR by index (0-based). Restarts the tuner.

    Args:
        index: Channel index to remove (0 = first channel). Use sdr_status to see channels.
    """
    result = _post('/sdrcmd', {'cmd': 'single_remove_channel', 'index': index}, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_restart() -> str:
    """
    Restart the rtl_airband SDR decoder process.  Use when SDR status shows
    STOPPED or audio has dropped out.  Restarts the sdrplay systemd service
    and relaunches rtl_airband.
    """
    result = _post('/sdrcmd', {'cmd': 'restart'}, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_stop() -> str:
    """
    Stop the rtl_airband SDR decoder process without restarting it.
    """
    result = _post('/sdrcmd', {'cmd': 'stop'})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Radio TX
# ---------------------------------------------------------------------------
@mcp.tool()
def radio_ptt(on: bool) -> str:
    """
    Key (on=True) or unkey (on=False) the transmitter.  Uses the currently
    configured TX radio and PTT method (AIOC, relay, or software).

    IMPORTANT: Always call radio_ptt(False) after transmitting to unkey the radio.
    """
    key_char = 'k' if on else 'u'
    result = _post('/key', {'key': key_char})
    state = 'ON (transmitting)' if on else 'OFF (receive)'
    if result.get('ok'):
        return f"PTT {state}"
    return f"PTT command failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def radio_tts(
    text: str,
    voice: int = 1,
) -> str:
    """
    Speak text over the air using text-to-speech synthesis.
    The gateway keys the radio, plays the TTS audio, then unkeys automatically.

    Args:
        text:   Text to speak over the air (max ~200 words recommended).
        voice:  TTS voice number (1=default male, 2=female, etc. — depends on
                available voices; use 1 if unsure).
    """
    if not text.strip():
        return 'Error: text cannot be empty'
    result = _post('/tts', {'text': text, 'voice': voice})
    if result.get('ok'):
        return f'TTS queued: "{text[:60]}{"..." if len(text) > 60 else ""}"'
    return f"TTS failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def radio_cw(
    text: str,
    wpm: int = 20,
    freq_hz: int = 700,
    volume: float = 1.0,
) -> str:
    """
    Send Morse code (CW) over the air.  The gateway keys the radio, plays the
    CW tones, then unkeys automatically.

    Args:
        text:     Text to encode as Morse code (letters, numbers, punctuation).
        wpm:      Words per minute — typical range 10-25 (default 20).
        freq_hz:  CW tone frequency in Hz (default 700 Hz).
        volume:   Volume multiplier 0.0-1.0 (default 1.0 = full volume).
    """
    if not text.strip():
        return 'Error: text cannot be empty'
    result = _post('/cw', {
        'text': text,
        'wpm': wpm,
        'freq': freq_hz,
        'vol': volume,
    })
    if result.get('ok'):
        return f'CW queued: "{text}" at {wpm} WPM, {freq_hz} Hz'
    return f"CW failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def radio_ai_announce(
    prompt: str,
    target_secs: int = 30,
    voice: int = 1,
    top_text: str = 'QST',
    tail_text: str = '',
) -> str:
    """
    Generate and transmit an AI-written radio announcement.  The gateway sends
    the prompt to the configured AI engine, synthesizes the result as TTS, and
    transmits it with optional callsign/identifier text.

    Args:
        prompt:      Natural-language description of what to announce
                     (e.g. "current weather conditions are foggy with low visibility").
        target_secs: Target duration in seconds (5-120, default 30).
        voice:       TTS voice number (default 1).
        top_text:    Text spoken/displayed at the start (default 'QST').
        tail_text:   Text spoken/displayed at the end (e.g. callsign).
    """
    if not prompt.strip():
        return 'Error: prompt cannot be empty'
    result = _post('/aitext', {
        'text': prompt,
        'target_secs': max(5, min(120, target_secs)),
        'voice': voice,
        'top_text': top_text,
        'tail_text': tail_text,
    })
    if result.get('ok'):
        return f'AI announcement queued: "{prompt[:80]}{"..." if len(prompt) > 80 else ""}"'
    return f"AI announce failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def radio_set_tx(radio: str) -> str:
    """
    Select which radio is used for transmit.

    Args:
        radio: Radio identifier — 'th9800', 'kv4p', or any link endpoint
               source_id (e.g. 'd75_pi', 'ftm_150', 'celeron_aioc').
    """
    radio = radio.lower().strip()
    result = _post('/catcmd', {'cmd': 'SET_TX_RADIO', 'radio': radio})
    if result.get('ok'):
        return f"TX radio set to: {radio}"
    return f"Failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def radio_get_tx() -> str:
    """
    Get the currently selected TX radio (th9800, d75, or kv4p).
    """
    result = _post('/catcmd', {'cmd': 'GET_TX_RADIO'})
    if result.get('ok'):
        return f"Current TX radio: {result.get('radio', 'unknown')}"
    return f"Failed: {result.get('error', 'unknown error')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Recordings
# ---------------------------------------------------------------------------
@mcp.tool()
def recordings_list() -> str:
    """
    List all saved audio recordings.  Returns filename, size, frequency,
    date/time, and label for each recording.  Recordings are WAV files
    saved by the automation engine.
    """
    files = _get('/recordingslist')
    if isinstance(files, dict) and 'error' in files:
        return f"Error: {files['error']}"
    if not files:
        return 'No recordings found.'
    lines = [f"{'Filename':<50} {'Size':>8}  {'Freq':>8}  Date       Time"]
    lines.append('-' * 90)
    for f in files:
        size_kb = f.get('size', 0) // 1024
        lines.append(
            f"{f.get('name', ''):<50} {size_kb:>7}K  "
            f"{f.get('freq', ''):>8}  "
            f"{f.get('date', ''):10} {f.get('time', '')}"
        )
    return '\n'.join(lines)


@mcp.tool()
def recordings_delete(filename: str) -> str:
    """
    Delete a recording file by filename (basename only, no path).
    Get the filename from recordings_list first.

    Args:
        filename: Exact filename as returned by recordings_list
                  (e.g. "SDR_118.100MHz_2025-01-15_14-30-00.wav").
    """
    if not filename or '/' in filename or '..' in filename:
        return 'Error: invalid filename'
    result = _post('/recordingsdelete', {'files': [filename]})
    if result.get('ok'):
        return f"Deleted: {filename}"
    return f"Delete failed: {result.get('error', 'unknown error')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Logs
# ---------------------------------------------------------------------------
@mcp.tool()
def gateway_logs(lines: int = 50) -> str:
    """
    Retrieve recent gateway log lines (console output).  Useful for diagnosing
    errors, checking connection state, or seeing what the gateway is doing.

    Args:
        lines: Number of recent log lines to return (default 50, max 500).
    """
    lines = max(1, min(500, lines))
    data = _get(f'/logdata?after=0')
    if 'error' in data and not data.get('ok', True):
        return f"Error: {data['error']}"
    all_lines = data.get('lines', [])
    # Return last N lines
    return '\n'.join(all_lines[-lines:]) if all_lines else 'No log lines available.'


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Raw control
# ---------------------------------------------------------------------------
@mcp.tool()
def gateway_key(key_char: str) -> str:
    """
    Send a raw single-character key command to the gateway — the same as
    pressing a key in the terminal UI.

    Common keys:
      'k' = PTT key-down (start transmitting)
      'u' = PTT unkey (stop transmitting)
      'r' = toggle recording
      'q' = quit gateway (use with caution)
      'm' = mute/unmute audio
      't' = force audio trace dump

    Args:
        key_char: Single character command to send.
    """
    if len(key_char) != 1:
        return 'Error: key_char must be exactly one character'
    result = _post('/key', {'key': key_char})
    if result.get('ok'):
        return f"Key '{key_char}' sent"
    return f"Key command failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def automation_status() -> str:
    """
    Get the automation engine status: configured tasks, schedules,
    time window, and whether the engine is active.
    """
    return json.dumps(_get('/automationstatus'), indent=2)


@mcp.tool()
def automation_history() -> str:
    """
    Get recent automation execution history — which tasks ran, when,
    and whether they succeeded or failed.
    """
    return json.dumps(_get('/automationhistory'), indent=2)


@mcp.tool()
def automation_reload() -> str:
    """
    Reload the automation scheme from the config file.
    Use after editing automation tasks in gateway_config.txt.
    """
    result = _post('/automationcmd', {'cmd': 'reload'})
    if result.get('ok'):
        return f"Reloaded: {result.get('tasks', 0)} tasks"
    return f"Failed: {result.get('error', 'unknown')}"


@mcp.tool()
def automation_trigger(task_name: str) -> str:
    """
    Manually trigger a named automation task (from the gateway's automation
    scheme).  Use gateway_status first to see available automation tasks.

    Args:
        task_name: Name of the automation task to trigger (e.g. 'weather_announce').
    """
    result = _post('/automationcmd', {'cmd': 'trigger', 'task': task_name})
    if result.get('ok'):
        return f"Triggered: {result.get('triggered', task_name)}"
    return f"Failed: {result.get('error', 'unknown error')}"


@mcp.tool()
def audio_trace_toggle() -> str:
    """
    Toggle the audio mixer trace recording on or off.  When active, the gateway
    records per-tick audio state for all sources.  When stopped, it dumps a
    human-readable trace file to disk and prints the path.  Useful for
    diagnosing duck/hold timing issues or audio dropout.
    """
    # tracecmd uses form-encoded body, not JSON — use raw post
    url = GW_BASE_URL + '/tracecmd'
    body = b'type=audio'
    headers = {**_auth_headers(), 'Content-Type': 'application/x-www-form-urlencoded'}
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return f"Error: {e}"
    active = result.get('active', False)
    return f"Audio trace {'STARTED' if active else 'STOPPED and dumped to disk'}"


# ---------------------------------------------------------------------------
# Speaker output + gateway lifecycle — moved from tools/routing.py 2026-05-30
# ---------------------------------------------------------------------------

@mcp.tool()
def speaker_mode(mode: str) -> str:
    """
    Set the speaker output mode.

    Args:
        mode: 'virtual' (metering only, no audio device),
              'auto' (use default output),
              'real' (use specific ALSA device)
    """
    result = _post('/routing/cmd', {'cmd': 'speaker_mode', 'mode': mode})
    if result.get('ok'):
        return f"Speaker mode: {result.get('mode', mode)}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def gateway_restart() -> str:
    """
    Restart the radio gateway service via systemd.
    """
    import subprocess
    try:
        r = subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'radio-gateway.service'],
                          capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return "Gateway restart initiated"
        return f"Restart failed: {r.stderr.strip()}"
    except Exception as e:
        return f"Restart error: {e}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Broadcastify / Icecast streaming
# ---------------------------------------------------------------------------
@mcp.tool()
def broadcastify_status() -> str:
    """
    Get Broadcastify / Icecast streaming status: connection state, the
    server/mount, encoder format (bitrate, sample rate, mono vs the
    dual-channel two-scanner feed), throughput, and the last error.

    The audio is encoded in-process and written straight to Icecast — there
    is no external DarkIce process involved.
    """
    data = _get('/status')
    if 'error' in data:
        return f"Error: {data['error']}"
    stats = data.get('encoder_stats') or {}

    lines = [
        f"streaming_enabled : {data.get('streaming_enabled', False)}",
        f"stream_connected  : {data.get('stream_connected', False)}",
        f"stream_restarts   : {data.get('stream_restarts', 0)}",
        f"stream_health     : {data.get('stream_health', False)}",
    ]

    if stats:
        dual = stats.get('dual_channel')
        # The dual-channel feed carries sdr1 on the left and sdr2 on the
        # right. Broadcastify's own web player sums to mono, so a channel
        # imbalance is invisible there — check with VLC.
        channels = ('stereo — sdr1 = LEFT, sdr2 = RIGHT' if dual
                    else 'mono' if dual is not None else '?')
        lines += [
            "",
            f"server            : {stats.get('server', '?')}",
            f"mount             : {stats.get('mount', '?')}",
            f"channels          : {channels}",
            f"format            : {stats.get('bitrate', '?')} kbps MP3 @ "
            f"{stats.get('sample_rate', '?')} Hz",
            f"uptime            : {stats.get('uptime', 0)} s",
            f"bytes_sent        : {stats.get('bytes_sent', 0)}",
            f"send_rate         : {stats.get('send_rate', '—')}",
        ]
        err = stats.get('last_error')
        if err:
            when = stats.get('last_error_time') or 0
            when_s = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))
                      if when else 'unknown time')
            lines.append(f"last_error        : {err}  (at {when_s})")
        else:
            lines.append("last_error        : none")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Smart Announcements
# ---------------------------------------------------------------------------
@mcp.tool()
def smart_announce_status() -> str:
    """
    Get smart announcement status: enabled state, per-slot countdown to next
    auto-fire, slot mode (auto/manual), and current activity step
    (idle/generating/transmitting).
    """
    data = _get('/status')
    if 'error' in data:
        return f"Error: {data['error']}"
    if not data.get('smart_announce_enabled'):
        return "Smart announcements not enabled (ENABLE_SMART_ANNOUNCE = false)"
    countdowns = data.get('smart_countdowns', [])
    activity = data.get('smart_activity', {})
    lines = ["Smart announcements ENABLED"]
    for slot_id, secs, mode in countdowns:
        act = activity.get(str(slot_id), {})
        step = act.get('step', 'idle')
        mins, s = divmod(int(secs), 60)
        lines.append(f"  slot {slot_id} [{mode}]: next in {mins}m{s:02d}s — {step}")
    if not countdowns:
        lines.append("  No slots configured")
    return '\n'.join(lines)


@mcp.tool()
def smart_announce_trigger(slot: int) -> str:
    """
    Manually trigger a smart announcement slot immediately, bypassing the schedule.

    Args:
        slot: Slot number — 1, 2, or 3.
    """
    if slot not in (1, 2, 3):
        return "Error: slot must be 1, 2, or 3"
    key_map = {1: '[', 2: ']', 3: '\\'}
    result = _post('/key', {'key': key_map[slot]})
    if result.get('ok'):
        return f"Smart announce slot {slot} triggered"
    return f"Failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Relay / GPIO control
# ---------------------------------------------------------------------------
@mcp.tool()
def relay_status() -> str:
    """
    Get relay state: charger relay on/off, radio power relay state,
    and whether each relay is enabled in config.
    """
    data = _get('/status')
    if 'error' in data:
        return f"Error: {data['error']}"
    lines = [
        f"relay_charger_enabled : {data.get('relay_charger_enabled', False)}",
        f"charger_state         : {data.get('charger', 'unknown')}",
        f"relay_radio_enabled   : {data.get('relay_radio_enabled', False)}",
        f"relay_radio_pressing  : {data.get('relay_pressing', False)}",
    ]
    return '\n'.join(lines)


@mcp.tool()
def relay_charger_toggle() -> str:
    """
    Toggle the charger relay on or off.  If charging, stops charging; if draining,
    starts charging.  Use relay_status first to check the current state.
    """
    result = _post('/key', {'key': 'h'})
    if not result.get('ok'):
        return f"Failed: {result.get('error', 'unknown')}"
    data = _get('/status')
    charger = data.get('charger', 'unknown')
    return f"Charger relay toggled — new state: {charger}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — ADS-B
# ---------------------------------------------------------------------------
@mcp.tool()
def adsb_status() -> str:
    """
    Get ADS-B aircraft tracking status: whether dump1090-fa is running,
    aircraft visible in the last 60 seconds, message rate, and FR24 feed state.
    """
    data = _get('/adsbstatus')
    if 'error' in data:
        return f"Error: {data['error']}"
    if not data.get('enabled'):
        return "ADS-B not enabled (ENABLE_ADSB = false)"
    lines = [
        f"dump1090  : {'running' if data.get('dump1090') else 'STOPPED'}",
        f"web       : {'up' if data.get('web') else 'down'}",
        f"fr24feed  : {'running' if data.get('fr24feed') else 'stopped'}",
        f"aircraft  : {data.get('aircraft', 0)} visible (last 60s)",
        f"messages  : {data.get('messages', 0)} total, {data.get('messages_rate', 0):.1f}/s",
    ]
    return '\n'.join(lines)


@mcp.tool()
def usbip_status() -> str:
    """
    Get USB/IP status: whether the remote USB server is reachable and which
    devices are currently attached over the network. Used to share a USB
    radio interface (e.g. an AIOC) from another machine on the LAN.
    """
    data = _get('/usbipstatus')
    if 'error' in data:
        return f"Error: {data['error']}"
    if not data.get('enabled'):
        return "USB/IP not enabled (ENABLE_USBIP = false)"
    # last_check is *age in seconds*, not a timestamp.
    age = data.get('last_check')
    lines = [
        f"server     : {data.get('server', '?')}",
        f"reachable  : {data.get('server_reachable', False)}",
        f"last check : {f'{age:g}s ago' if age is not None else 'never'}",
    ]
    if data.get('last_error'):
        lines.append(f"last error : {data['last_error']}")
    devices = data.get('devices', [])
    if not devices:
        lines.append("devices    : none attached")
    else:
        lines.append(f"devices    : {len(devices)} attached")
        for d in devices:
            if isinstance(d, dict):
                lines.append(f"   {d.get('busid', '?')}  {d.get('name', d.get('desc', ''))}")
            else:
                lines.append(f"   {d}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
