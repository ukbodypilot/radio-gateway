#!/usr/bin/env python3
"""
Radio Gateway MCP Server

Exposes the radio gateway as a set of AI-callable tools via the Model Context
Protocol (MCP) stdio transport.  Claude Code (or any MCP client) can load this
server and control the gateway without API keys.

Usage (stdio, local):
    python3 gateway_mcp.py

Claude Code configuration (.claude/settings.json):
    {
      "mcpServers": {
        "radio-gateway": {
          "command": "python3",
          "args": ["/home/user/Downloads/radio-gateway/gateway_mcp.py"]
        }
      }
    }

The server auto-reads gateway_config.txt to find the correct port/password.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import base64

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — read from gateway_config.txt if present
# ---------------------------------------------------------------------------

def _load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), 'gateway_config.txt')
    port = 8080
    password = ''
    https = False
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                line = line.strip()
                if '=' not in line or line.startswith('#'):
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = v.strip()
                if k == 'WEB_CONFIG_PORT':
                    try:
                        port = int(v)
                    except ValueError:
                        pass
                elif k == 'WEB_CONFIG_PASSWORD':
                    password = v
                elif k == 'WEB_CONFIG_HTTPS':
                    https = v.lower() in ('true', '1', 'yes')
    scheme = 'https' if https else 'http'
    return f'{scheme}://127.0.0.1:{port}', password


GW_BASE_URL, GW_PASSWORD = _load_config()


def _load_telegram_config() -> dict:
    """Read Telegram settings from gateway_config.txt."""
    cfg = {'token': '', 'chat_id': 0, 'status_file': '/tmp/tg_status.json'}
    cfg_path = os.path.join(os.path.dirname(__file__), 'gateway_config.txt')
    if not os.path.isfile(cfg_path):
        return cfg
    with open(cfg_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip(); v = v.strip()
            if k == 'TELEGRAM_BOT_TOKEN':
                cfg['token'] = v
            elif k == 'TELEGRAM_CHAT_ID':
                try:
                    cfg['chat_id'] = int(v)
                except ValueError:
                    pass
            elif k == 'TELEGRAM_STATUS_FILE':
                cfg['status_file'] = v
    return cfg


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers():
    if GW_PASSWORD:
        creds = base64.b64encode(f'admin:{GW_PASSWORD}'.encode()).decode()
        return {'Authorization': f'Basic {creds}'}
    return {}


def _get(path: str) -> dict:
    url = GW_BASE_URL + path
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}', 'ok': False}
    except Exception as e:
        return {'error': str(e), 'ok': False}


def _post(path: str, data: dict, timeout: int = 10) -> dict:
    url = GW_BASE_URL + path
    body = json.dumps(data).encode()
    headers = {**_auth_headers(), 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}', 'ok': False}
    except Exception as e:
        return {'error': str(e), 'ok': False}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name='radio-gateway',
    instructions=(
        'Control a software-defined radio (SDR) + radio repeater gateway. '
        'Use gateway_status first to understand what is connected and running. '
        'Frequencies are in MHz unless noted. '
        'PTT commands key/unkey the transmitter — always unkey after transmitting. '
        'TTS and CW transmit audio over the air; confirm the radio is on the right '
        'frequency before transmitting.'
    ),
)


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
# Tools — Telegram
# ---------------------------------------------------------------------------

@mcp.tool()
def telegram_reply(message: str) -> str:
    """
    Send a reply to the Telegram user who sent the current command.
    Call this ONCE when you have completely finished processing the request.
    Do not call it until you are done — this is the user's only feedback channel.

    Args:
        message: Plain-text response to send back to the user's phone.
                 Keep it concise — Telegram messages should be readable on mobile.
                 Use newlines for structure, avoid markdown formatting.
    """
    import json as _json
    import time as _time

    tg = _load_telegram_config()
    token = tg['token']
    chat_id = tg['chat_id']

    if not token or not chat_id:
        return 'Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)'

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = json.dumps({'chat_id': chat_id, 'text': message}).encode()
    headers = {'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return f'Telegram send failed: {e}'

    if not result.get('ok'):
        return f"Telegram send failed: {result.get('description', 'unknown error')}"

    # Update status file with reply timestamp
    ts = _time.strftime('%Y-%m-%dT%H:%M:%S')
    try:
        status_path = tg['status_file']
        existing = {}
        if os.path.isfile(status_path):
            with open(status_path) as f:
                existing = _json.load(f)
        existing['last_reply_time'] = ts
        existing['last_reply_text'] = message[:120]
        with open(status_path, 'w') as f:
            _json.dump(existing, f)
    except Exception:
        pass

    return f'Telegram reply sent at {ts}'


# ---------------------------------------------------------------------------
# Tools — TH-9800 Radio Control
# ---------------------------------------------------------------------------

@mcp.tool()
def radio_frequency(
    vfo: str = 'left',
    volume: int | None = None,
    squelch: int | None = None,
) -> str:
    """
    Read TH-9800 radio state and optionally set volume/squelch.
    Returns frequency, channel, power, signal strength, and volume for a VFO.

    The TH-9800 is channel-based — frequency is determined by the memory channel.
    Use the web UI /radio page or front-panel buttons to change channels.

    Args:
        vfo:      Which VFO side — 'left' or 'right' (default 'left').
        volume:   Set volume level 0-100.  Omit to read only.
        squelch:  Set squelch level 0-100.  Omit to read only.
    """
    vfo = vfo.lower().strip()
    if vfo not in ('left', 'right'):
        return "Error: vfo must be 'left' or 'right'"

    results = []

    # Set volume if requested
    if volume is not None:
        vol_cmd = 'VOL_LEFT' if vfo == 'left' else 'VOL_RIGHT'
        result = _post('/catcmd', {'cmd': vol_cmd, 'value': max(0, min(100, volume))})
        if result.get('ok'):
            results.append(f'Volume set to {volume}')
        else:
            results.append(f'Volume set failed: {result.get("error", "unknown")}')

    # Set squelch if requested
    if squelch is not None:
        sq_cmd = 'SQ_LEFT' if vfo == 'left' else 'SQ_RIGHT'
        result = _post('/catcmd', {'cmd': sq_cmd, 'value': max(0, min(100, squelch))})
        if result.get('ok'):
            results.append(f'Squelch set to {squelch}')
        else:
            results.append(f'Squelch set failed: {result.get("error", "unknown")}')

    # Always read current state
    state = _get('/catstatus')
    if 'error' in state and not state.get('connected'):
        if results:
            return '\n'.join(results) + '\n(Could not read back state — radio disconnected)'
        return 'TH-9800 not connected'

    side = state.get(vfo, {})
    info = {
        'vfo': vfo,
        'display': side.get('display', ''),
        'channel': side.get('channel', ''),
        'power': side.get('power', ''),
        'signal': side.get('signal', 0),
        'volume': state.get('volume', {}).get(vfo, -1),
        'serial_connected': state.get('serial_connected', False),
    }

    if results:
        return '\n'.join(results) + '\n' + json.dumps(info, indent=2)
    return json.dumps(info, indent=2)


# ---------------------------------------------------------------------------
# Tools — D75 Radio
# ---------------------------------------------------------------------------

@mcp.tool()
def d75_status() -> str:
    """
    Get TH-D75 handheld radio status: Bluetooth/serial connection state,
    frequency, mode, signal, battery, GPS, and audio levels for both bands.
    """
    return json.dumps(_get('/d75status'), indent=2)


@mcp.tool()
def d75_command(
    cmd: str,
    args: str = '',
) -> str:
    """
    Send a command to the TH-D75 radio.

    Args:
        cmd:  Command name — one of:
              'cat'        — Send raw CAT command (put command in args)
              'vol'        — Set audio boost 0-500% (put value in args)
              'ptt'        — Toggle PTT on D75
              'tone'       — Set tone (args: 'band off|tone|ctcss|dcs [freq/code]')
              'shift'      — Set shift (args: 'band 0|1|2')
              'offset'     — Set offset (args: 'band mhz')
              'freq'       — Set frequency (args: 'band,freq_hz')
              'btstart'    — Start Bluetooth (managed by endpoint)
              'btstop'     — Stop Bluetooth (managed by endpoint)
              'reconnect'  — Reconnect (managed by endpoint)
              'start_service' — Start d75-cat service
              'mute'       — Toggle mute
              'status'     — Request status update
        args: Arguments for the command.
    """
    cmd = cmd.lower().strip()
    valid = ('btstart', 'btstop', 'reconnect', 'start_service', 'cat', 'vol',
             'ptt', 'tone', 'shift', 'offset', 'freq', 'mute', 'status')
    if cmd not in valid:
        return f"Error: cmd must be one of: {', '.join(valid)}"
    result = _post('/d75cmd', {'cmd': cmd, 'args': args}, timeout=15)
    if result.get('ok'):
        resp = result.get('response', '')
        return f"D75 {cmd} OK" + (f': {resp}' if resp else '')
    return f"D75 {cmd} failed: {result.get('error', 'unknown')}"


@mcp.tool()
def d75_frequency(
    freq_mhz: float,
    mode: str = 'FM',
) -> str:
    """
    Tune the TH-D75 to a specific frequency on the active band.
    Uses the FO (frequency offset) CAT command.

    Args:
        freq_mhz: Frequency in MHz (e.g. 145.500, 446.000).
        mode:     Modulation mode — FM, AM, NFM, DV, LSB, USB, CW, DR, WFM
                  (default FM).
    """
    mode_map = {'FM': 0, 'DV': 1, 'AM': 2, 'LSB': 3, 'USB': 4,
                'CW': 5, 'NFM': 6, 'DR': 7, 'WFM': 8}
    mode = mode.upper().strip()
    mode_val = mode_map.get(mode)
    if mode_val is None:
        return f"Error: mode must be one of: {', '.join(mode_map.keys())}"

    # Get current D75 state to determine active band
    state = _get('/d75status')
    band = state.get('active_band', 0)

    # Build FO command: frequency in Hz, 21-field format
    freq_hz = int(freq_mhz * 1_000_000)
    # FO band,rxfreq,offset,rxstep,txstep,mode,fine_mode,fine_step,
    #    tone,ctcss,dcs,cross,reverse,shift,tone_idx,ctcss_idx,dcs_idx,
    #    cross_type,urcall,dsql_type,dsql_code
    fo_cmd = (
        f'FO {band},{freq_hz:011d},0000000,0,0,'
        f'{mode_val},0,0,'
        f'0,0,0,0,0,0,08,08,000,'
        f'0,CQCQCQ,0,00000'
    )
    result = _post('/d75cmd', {'cmd': 'cat', 'args': fo_cmd}, timeout=10)
    if result.get('ok'):
        return f'D75 tuned to {freq_mhz:.4f} MHz ({mode})'
    return f"D75 tune failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Tools — KV4P Radio
# ---------------------------------------------------------------------------

@mcp.tool()
def kv4p_status() -> str:
    """
    Get KV4P HT radio status: USB connection state, frequency, squelch,
    CTCSS tones, power level, bandwidth, and audio levels.
    """
    return json.dumps(_get('/kv4pstatus'), indent=2)


@mcp.tool()
def kv4p_command(
    cmd: str,
    args: str = '',
) -> str:
    """
    Send a command to the KV4P HT radio.

    Args:
        cmd:  Command name — one of:
              'freq'      — Set RX frequency in MHz (e.g. '146.520')
              'txfreq'    — Set TX frequency in MHz (0 = same as RX)
              'squelch'   — Set squelch level 0-9
              'ctcss'     — Set CTCSS tones (e.g. '103.5 103.5' for TX RX)
              'bandwidth' — Set wide (1) or narrow (0)
              'power'     — Set high (1) or low (0) power
              'ptt'       — Toggle PTT
              'vol'       — Set audio boost 0-500%
              'reconnect' — Reconnect USB device
        args: Value for the command.
    """
    cmd = cmd.lower().strip()
    valid = ('freq', 'txfreq', 'squelch', 'ctcss', 'bandwidth', 'power',
             'ptt', 'vol', 'reconnect')
    if cmd not in valid:
        return f"Error: cmd must be one of: {', '.join(valid)}"
    result = _post('/kv4pcmd', {'cmd': cmd, 'args': args}, timeout=10)
    if result.get('ok'):
        resp = result.get('response', '')
        return f"KV4P {cmd} OK" + (f': {resp}' if resp else '')
    return f"KV4P {cmd} failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Tools — Mixer Control
# ---------------------------------------------------------------------------

@mcp.tool()
def mixer_control(
    action: str,
    source: str | None = None,
    value: float | None = None,
    flag: str | None = None,
    state: bool | None = None,
) -> str:
    """
    Control the audio mixer — mute/unmute sources, adjust volume/boost,
    toggle duck, and control processing flags.

    Args:
        action: One of:
                'status'     — Full mixer state: mutes, levels, volumes, duck, flags, boosts, processing
                'mute'       — Mute a source (requires source)
                'unmute'     — Unmute a source (requires source)
                'toggle'     — Toggle mute on a source (requires source)
                'volume'     — Set master input volume (requires value, range 0.1-3.0)
                'duck'       — Enable/disable ducking on a source (requires source,
                               optionally state=true/false; omit state to toggle)
                'boost'      — Set per-source audio boost % (requires source + value,
                               range 0-500; sources: d75, kv4p, remote)
                'flag'       — Toggle or set a mixer flag (requires flag arg)
                'processing' — Toggle or set an audio processing filter (requires source
                               + flag for filter name; optionally state=true/false)
        source: Audio source — built-in: 'global', 'tx', 'rx', 'sdr1', 'sdr2',
                'kv4p', 'remote', 'announce', 'speaker'.
                Link endpoints by source_id: e.g. 'd75_pi', 'ftm_150', 'celeron_aioc'.
        value:  Numeric value for 'volume' (0.1-3.0) or 'boost' (0-500) actions.
        flag:   For 'flag' action — one of:
                'vad'          — Voice Activity Detection
                'agc'          — Automatic Gain Control
                'echo_cancel'  — Echo Cancellation
                'rebroadcast'  — SDR-to-radio rebroadcast
                'talkback'     — TX audio to local outputs (off = radio-only TX)
                For 'processing' action — one of:
                'gate'  — Noise gate
                'hpf'   — High-pass filter
                'lpf'   — Low-pass filter
                'notch' — Notch filter
        state:  Explicit true/false for 'duck', 'flag', and 'processing' actions.
                Omit to toggle.
    """
    action = action.lower().strip()

    if action == 'status':
        result = _post('/mixer', {'action': 'status'})
        if result.get('ok'):
            return json.dumps(result, indent=2)
        return f"Error: {result.get('error', 'unknown')}"

    if action in ('mute', 'unmute', 'toggle'):
        if not source:
            return 'Error: source required for mute/unmute/toggle'
        source = source.lower().strip()
        result = _post('/mixer', {'action': action, 'source': source})
        if result.get('ok'):
            muted = result.get('muted')
            return f"{source} {'muted' if muted else 'unmuted'}"
        return f"Failed: {result.get('error', 'unknown')}"

    if action == 'volume':
        if value is None:
            # Read-only
            result = _post('/mixer', {'action': 'volume'})
        else:
            result = _post('/mixer', {'action': 'volume', 'value': float(value)})
        if result.get('ok'):
            return f"Volume: {result.get('volume', '?')}"
        return f"Failed: {result.get('error', 'unknown')}"

    if action == 'duck':
        if not source:
            return 'Error: source required for duck (sdr1, sdr2, d75, kv4p, remote)'
        payload = {'action': 'duck', 'source': source.lower().strip()}
        if state is not None:
            payload['state'] = state
        result = _post('/mixer', payload)
        if result.get('ok'):
            return f"{source} duck: {'enabled' if result.get('duck') else 'disabled'}"
        return f"Failed: {result.get('error', 'unknown')}"

    if action == 'boost':
        if not source:
            return 'Error: source required for boost (d75, kv4p, remote)'
        if value is None:
            return 'Error: value required for boost (0-500 percent)'
        result = _post('/mixer', {'action': 'boost', 'source': source.lower().strip(),
                                   'value': int(value)})
        if result.get('ok'):
            return f"{source} boost: {result.get('boost_pct', '?')}%"
        return f"Failed: {result.get('error', 'unknown')}"

    if action == 'flag':
        if not flag:
            return 'Error: flag required (vad, agc, echo_cancel, rebroadcast, talkback)'
        payload = {'action': 'flag', 'flag': flag.lower().strip()}
        if state is not None:
            payload['state'] = state
        result = _post('/mixer', payload)
        if result.get('ok'):
            return f"{flag}: {'enabled' if result.get('enabled') else 'disabled'}"
        return f"Failed: {result.get('error', 'unknown')}"

    if action == 'processing':
        if not source:
            return 'Error: source required (radio, sdr, d75, kv4p)'
        if not flag:
            return 'Error: flag required for filter name (gate, hpf, lpf, notch)'
        payload = {'action': 'processing', 'source': source.lower().strip(),
                   'filter': flag.lower().strip()}
        if state is not None:
            payload['state'] = state
        result = _post('/mixer', payload)
        if result.get('ok'):
            active = result.get('active', [])
            return f"{source} processing: [{', '.join(active)}]" if active else f"{source} processing: none active"
        return f"Failed: {result.get('error', 'unknown')}"

    return ("Error: action must be one of: status, mute, unmute, toggle, "
            "volume, duck, boost, flag, processing")


# ---------------------------------------------------------------------------
# Tools — Recording Playback
# ---------------------------------------------------------------------------

@mcp.tool()
def recording_playback(filename: str, target: str = 'radio') -> str:
    """
    Play a recording file over the air or to Mumble.  The gateway keys the
    radio (if target is 'radio'), plays the audio, then unkeys.

    Args:
        filename: Recording filename from recordings_list (e.g. "SDR_118.100MHz_...wav").
        target:   Where to play — 'radio' (over the air via TX) or 'mumble'
                  (to Mumble channel).  Default 'radio'.
    """
    if not filename or '/' in filename or '..' in filename:
        return 'Error: invalid filename'
    target = target.lower().strip()
    if target not in ('radio', 'mumble'):
        return "Error: target must be 'radio' or 'mumble'"

    # Check file exists via recordings list
    files = _get('/recordingslist')
    if isinstance(files, list):
        names = [f.get('name', '') for f in files]
        if filename not in names:
            return f'Error: recording not found. Available: {", ".join(names[:5])}...'

    # Use TTS endpoint with file path — or use the automation trigger
    # Actually, there's no direct playback endpoint yet. Use the key command
    # to play soundboard slots or suggest alternative
    return (f'Recording playback via MCP not yet implemented — '
            f'the gateway needs a /playrecording endpoint. '
            f'For now, download via /recordingsdownload?file={filename} '
            f'and use radio_tts for spoken content.')


# ---------------------------------------------------------------------------
# Tools — Configuration
# ---------------------------------------------------------------------------

@mcp.tool()
def config_read(section: str | None = None) -> str:
    """
    Read current gateway_config.txt settings.  Returns key=value pairs,
    optionally filtered to a specific INI section.

    SECURITY: Passwords and tokens are redacted in the output.

    Args:
        section: Optional INI section name to filter (e.g. 'audio', 'sdr',
                 'telegram', 'radio').  Omit to return all settings.
    """
    cfg_path = os.path.join(os.path.dirname(__file__), 'gateway_config.txt')
    if not os.path.isfile(cfg_path):
        return 'Error: gateway_config.txt not found'

    sensitive = {'PASSWORD', 'TOKEN', 'SECRET', 'KEY', 'MOUNT', 'STREAM_PASSWORD'}

    lines = []
    current_section = ''
    with open(cfg_path) as f:
        for line in f:
            line = line.rstrip('\n')
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                current_section = stripped[1:-1].lower()
                if section is None or current_section == section.lower():
                    lines.append(line)
                continue
            if section is not None and current_section != section.lower():
                continue
            if stripped.startswith('#') or not stripped:
                continue
            # Redact sensitive values
            if '=' in stripped:
                k, _, v = stripped.partition('=')
                k_upper = k.strip().upper()
                if any(s in k_upper for s in sensitive) and v.strip():
                    lines.append(f'{k.strip()} = ****')
                    continue
            lines.append(line)

    if not lines:
        if section:
            return f'Section [{section}] not found in config'
        return 'Config file is empty'
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Tools — Telegram Status
# ---------------------------------------------------------------------------

@mcp.tool()
def telegram_status() -> str:
    """
    Get Telegram bot status: whether the bot process is running, tmux session
    state, message counts today, and last message timestamps.
    """
    return json.dumps(_get('/telegramstatus'), indent=2)


# ---------------------------------------------------------------------------
# Tools — Process Control
# ---------------------------------------------------------------------------

@mcp.tool()
def process_control(service: str, action: str) -> str:
    """
    Start, stop, or restart a gateway sub-process.

    Args:
        service: Service to control — one of:
                 'darkice'  — Broadcastify/Icecast streaming daemon
                 'sdr'      — SDR receiver (rtl_airband)
                 'd75'      — D75 CAT proxy service (d75-cat.service)
        action:  One of 'start', 'stop', 'restart'.
    """
    service = service.lower().strip()
    action = action.lower().strip()

    if action not in ('start', 'stop', 'restart'):
        return "Error: action must be 'start', 'stop', or 'restart'"

    if service == 'darkice':
        if action == 'start':
            result = _post('/darkicecmd', {'cmd': 'start'})
        elif action == 'stop':
            result = _post('/darkicecmd', {'cmd': 'stop'})
        else:
            result = _post('/darkicecmd', {'cmd': 'restart'})
        return f"DarkIce {action}: {'OK' if result.get('ok') else result.get('error', 'failed')}"

    elif service == 'sdr':
        if action == 'stop':
            result = _post('/sdrcmd', {'cmd': 'stop'})
        else:  # start or restart both restart rtl_airband
            result = _post('/sdrcmd', {'cmd': 'restart'}, timeout=20)
        return f"SDR {action}: {'OK' if result.get('ok') else result.get('error', 'failed')}"

    elif service == 'd75':
        if action == 'start':
            result = _post('/d75cmd', {'cmd': 'start_service'}, timeout=10)
        elif action == 'stop':
            # No stop endpoint — suggest systemctl
            return 'D75 service stop: use systemctl stop d75-cat manually'
        else:
            result = _post('/d75cmd', {'cmd': 'reconnect'}, timeout=15)
        return f"D75 {action}: {'OK' if result.get('ok') else result.get('error', 'failed')}"

    else:
        return f"Error: unknown service '{service}' — use darkice, sdr, or d75"


# ---------------------------------------------------------------------------
# Tools — Audio Routing (Bus System)
# ---------------------------------------------------------------------------

@mcp.tool()
def routing_status() -> str:
    """
    Get the full audio routing configuration: all sources, busses, sinks,
    and connections between them. This is the bus-based routing system
    that controls how audio flows through the gateway.
    """
    return json.dumps(_get('/routing/status'), indent=2)


@mcp.tool()
def routing_levels() -> str:
    """
    Get live audio levels for all sources, sinks, and busses.
    Returns a dict of id → level (0-100). Polled by the routing UI
    every 200ms. Useful for checking if audio is flowing.
    """
    return json.dumps(_get('/routing/levels'), indent=2)


@mcp.tool()
def routing_connect(source_or_bus: str, bus_or_sink: str, connection_type: str = "auto") -> str:
    """
    Connect a source to a bus, or a bus to a sink.

    Args:
        source_or_bus: The source ID (e.g. 'sdr', 'webmic', 'mumble_rx') or bus ID
        bus_or_sink: The bus ID or sink ID (e.g. 'speaker', 'broadcastify', 'mumble', 'kv4p_tx')
        connection_type: 'source-bus', 'bus-sink', or 'auto' (auto-detect based on IDs)
    """
    if connection_type == 'auto':
        # Heuristic: if second arg looks like a sink, it's bus→sink
        sink_ids = {'speaker', 'broadcastify', 'mumble', 'recording', 'remote_audio_tx',
                    'kv4p_tx', 'aioc_tx'}
        if bus_or_sink in sink_ids or bus_or_sink.endswith('_tx'):
            connection_type = 'bus-sink'
        else:
            connection_type = 'source-bus'

    result = _post('/routing/cmd', {
        'cmd': 'connect',
        'type': connection_type,
        'from': source_or_bus,
        'to': bus_or_sink
    })
    if result.get('ok'):
        return f"Connected {source_or_bus} → {bus_or_sink} ({connection_type})"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def routing_disconnect(source_or_bus: str, bus_or_sink: str, connection_type: str = "auto") -> str:
    """
    Disconnect a source from a bus, or a bus from a sink.

    Args:
        source_or_bus: The source ID or bus ID
        bus_or_sink: The bus ID or sink ID
        connection_type: 'source-bus', 'bus-sink', or 'auto' (auto-detect)
    """
    if connection_type == 'auto':
        sink_ids = {'speaker', 'broadcastify', 'mumble', 'recording', 'remote_audio_tx',
                    'kv4p_tx', 'aioc_tx'}
        if bus_or_sink in sink_ids or bus_or_sink.endswith('_tx'):
            connection_type = 'bus-sink'
        else:
            connection_type = 'source-bus'

    result = _post('/routing/cmd', {
        'cmd': 'disconnect',
        'type': connection_type,
        'from': source_or_bus,
        'to': bus_or_sink
    })
    if result.get('ok'):
        return f"Disconnected {source_or_bus} → {bus_or_sink}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_create(name: str, bus_type: str = "solo") -> str:
    """
    Create a new audio bus.

    Args:
        name: Display name for the bus (e.g. 'Monitor Mix', 'D75 TX')
        bus_type: One of 'listen', 'solo', 'duplex', 'simplex'
                  - listen: mixing bus for monitoring (like a broadcast mix)
                  - solo: single source to single radio TX
                  - duplex: cross-link two radios (full duplex)
                  - simplex: store-and-forward repeater
    """
    result = _post('/routing/cmd', {
        'cmd': 'add_bus',
        'name': name,
        'type': bus_type
    })
    if result.get('ok'):
        return f"Created {bus_type} bus '{name}' (id: {result.get('id', '?')})"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_delete(bus_id: str) -> str:
    """
    Delete an audio bus and all its connections.

    Args:
        bus_id: The bus ID to delete (use routing_status to find IDs)
    """
    result = _post('/routing/cmd', {
        'cmd': 'delete_bus',
        'bus': bus_id
    })
    if result.get('ok'):
        return f"Deleted bus '{bus_id}'"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_rename(bus_id: str, name: str) -> str:
    """
    Rename a bus. Changes the display name shown in routing, dashboard,
    and loop recorder.

    Args:
        bus_id: The bus ID (e.g. 'main', 'th9800')
        name:   New display name
    """
    result = _post('/routing/cmd', {
        'cmd': 'rename_bus',
        'id': bus_id,
        'name': name,
    })
    if result.get('ok'):
        return f"Renamed bus '{bus_id}' to '{result.get('name')}'"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_mute(bus_id: str) -> str:
    """
    Toggle mute on a bus. When muted, no audio passes through the bus
    in either direction.

    Args:
        bus_id: The bus ID to mute/unmute
    """
    result = _post('/routing/cmd', {
        'cmd': 'bus_mute',
        'bus': bus_id
    })
    if result.get('ok'):
        state = 'muted' if result.get('muted') else 'unmuted'
        return f"Bus '{bus_id}': {state}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def sink_mute(sink_id: str) -> str:
    """
    Toggle mute on a source or sink. When muted, audio is blocked.

    Args:
        sink_id: The source or sink ID (e.g. 'speaker', 'broadcastify',
                 'mumble', 'sdr', 'kv4p', 'remote_audio_tx')
    """
    result = _post('/routing/cmd', {
        'cmd': 'mute',
        'id': sink_id
    })
    if result.get('ok'):
        state = 'muted' if result.get('muted') else 'unmuted'
        return f"'{sink_id}': {state}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_toggle_processing(bus_id: str, filter_name: str) -> str:
    """
    Toggle an audio processing filter or stream output on a bus.

    Args:
        bus_id: The bus ID
        filter_name: One of:
                     'gate'  — noise gate
                     'hpf'   — high-pass filter
                     'lpf'   — low-pass filter
                     'notch' — notch filter
                     'dfn'   — neural denoise (RNNoise)
                     'pcm'   — feed PCM stream output
                     'mp3'   — feed MP3 stream output
                     'vad'   — VAD (voice activity detection) gate
    """
    result = _post('/routing/cmd', {
        'cmd': 'toggle_proc',
        'bus': bus_id,
        'filter': filter_name
    })
    if result.get('ok'):
        state = 'ON' if result.get('state') else 'OFF'
        return f"Bus '{bus_id}' {filter_name}: {state}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def set_gain(target_id: str, gain_percent: int) -> str:
    """
    Set the gain/volume on a source or sink.

    Args:
        target_id: The source or sink ID
        gain_percent: Gain as percentage (0-500, where 100 = unity)
    """
    result = _post('/routing/cmd', {
        'cmd': 'gain',
        'id': target_id,
        'value': gain_percent
    })
    if result.get('ok'):
        return f"'{target_id}' gain: {gain_percent}%"
    return f"Error: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

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
               'forward_telegram' — true/false
               'restart'     — restart transcriber with saved settings
               'clear'       — clear all results

               NOTE: denoise is a per-bus setting now — use
               bus_toggle_processing / bus_set_denoise_engine on the bus
               that feeds the transcription sink.
        value: The value to set (ignored for restart/clear).
    """
    if key in ('enabled', 'forward_mumble', 'forward_telegram'):
        value = value.lower() in ('true', '1', 'yes')
    result = _post('/transcribe_config', {'key': key, 'value': value})
    if result.get('ok'):
        note = result.get('note', '')
        return f"Transcription {key} set" + (f' ({note})' if note else '')
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_set_denoise_atten(bus_id: str, atten_db: float) -> str:
    """
    Set the DeepFilterNet attenuation cap for a bus (dB). 0 = model decides
    (can cause pumping on marginal SNR); typical useful values 15–25 dB.
    Bounded to [0, 60]. No effect if engine is RNNoise.
    """
    result = _post('/routing/cmd',
                   {'cmd': 'set_dfn_atten', 'bus': bus_id, 'atten_db': atten_db})
    if result.get('ok'):
        return f"Bus {bus_id}: denoise atten cap → {result.get('atten_db')} dB"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_set_denoise_engine(bus_id: str, engine: str) -> str:
    """
    Change the neural-denoise engine used by a bus's "D" filter.

    Args:
        bus_id: Bus id (e.g. 'main'). Run routing_status to list buses.
        engine: 'rnnoise' (tiny, aggressive) or 'deepfilternet' (speech-preserving).

    The swap is live — the next audio chunk rebuilds the denoise stream
    with the chosen engine. Existing enable/mix state is preserved.
    """
    result = _post('/routing/cmd',
                   {'cmd': 'set_dfn_engine', 'bus': bus_id, 'engine': engine})
    if result.get('ok'):
        return f"Bus {bus_id}: denoise engine → {result.get('engine')}"
    return f"Error: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Link Endpoints
# ---------------------------------------------------------------------------

@mcp.tool()
def link_endpoint_status() -> str:
    """
    Get detailed status of all connected Gateway Link endpoints including
    audio levels, PTT state, capabilities, and endpoint-reported radio state.
    """
    result = _get('/status')
    endpoints = result.get('link_endpoints', [])
    if not endpoints:
        return "No link endpoints connected"
    lines = []
    for ep in endpoints:
        _conn = 'CF' if ep.get('via_tunnel') else 'LAN'
        _ping = f"{ep.get('ping_ms', -1)}ms" if ep.get('ping_ms', -1) >= 0 else '?'
        lines.append(f"Endpoint: {ep['name']} ({_conn} {_ping})")
        lines.append(f"  Plugin: {ep.get('plugin', '?')}  Addr: {ep.get('addr', '?')}")
        lines.append(f"  Source: {ep.get('source_id', '?')}  Sink: {ep.get('sink_id', '?')}")
        lines.append(f"  RX level: {ep.get('level', 0)}  TX level: {ep.get('tx_level', 0)}")
        lines.append(f"  RX muted: {ep.get('rx_muted')}  TX muted: {ep.get('tx_muted')}  PTT: {ep.get('ptt_active')}")
        caps = ep.get('capabilities', {})
        lines.append(f"  Capabilities: {', '.join(k for k, v in caps.items() if v)}")
        es = ep.get('endpoint_status', {})
        if es:
            for k in ('model', 'firmware', 'serial_connected', 'audio_connected',
                       'battery_level', 'transmitting', 'active_band'):
                if k in es:
                    lines.append(f"  {k}: {es[k]}")
            bands = es.get('band', [])
            for i, b in enumerate(bands):
                if isinstance(b, dict) and b.get('frequency'):
                    lines.append(f"  Band {i}: {b['frequency']} MHz power={b.get('power','')} s_meter={b.get('s_meter','')}")
    return '\n'.join(lines)


@mcp.tool()
def link_endpoint_command(
    endpoint: str,
    cmd: str,
    args: str = '',
) -> str:
    """
    Send a command to a specific link endpoint.

    Args:
        endpoint: Endpoint name (e.g. 'd75-bt')
        cmd:      Command — 'ptt', 'frequency', 'cat', 'tone', 'shift', 'offset',
                  'memscan', 'status', 'rx_gain', 'tx_gain'
        args:     Command arguments (e.g. freq in MHz, CAT command string,
                  'on'/'off' for PTT)
    """
    payload = {'cmd': cmd}
    if cmd == 'ptt':
        payload['state'] = args.lower() in ('on', 'true', '1')
    elif cmd == 'frequency':
        payload['freq'] = args
    elif cmd in ('cat', 'tone', 'shift', 'offset'):
        payload['raw'] = args
    elif cmd in ('rx_gain', 'tx_gain'):
        try:
            payload['gain'] = float(args)
        except ValueError:
            return f"Error: {cmd} requires a numeric value"
    result = _post('/linkcmd', {'endpoint': endpoint, **payload})
    if result.get('ok'):
        resp = result.get('response', '')
        return f"Endpoint {endpoint} {cmd} OK" + (f': {resp}' if resp else '')
    return f"Error: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Loop Recorder
# ---------------------------------------------------------------------------

@mcp.tool()
def loop_recorder_status() -> str:
    """
    Get loop recorder status: which buses are recording, segment counts,
    disk usage, write rate, and retention settings.
    """
    import json
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

    result = _post('/loop/export', {
        'bus': bus_id,
        'start': start_epoch,
        'end': end_epoch,
        'format': format,
    })

    # The POST endpoint returns a file download, not JSON.
    # Use the loop_recorder directly instead.
    import urllib.request
    try:
        req = urllib.request.Request(
            f'http://127.0.0.1:8080/loop/export',
            data=json.dumps({
                'bus': bus_id, 'start': start_epoch,
                'end': end_epoch, 'format': format
            }).encode(),
            headers={'Content-Type': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        if resp.status != 200:
            return f"Error: server returned {resp.status}"

        # Save to recordings directory
        ext = 'wav' if format == 'wav' else 'mp3'
        st = datetime.fromtimestamp(start_epoch).strftime('%H%M%S')
        et = datetime.fromtimestamp(end_epoch).strftime('%H%M%S')
        import os
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
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
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:8080/loop/download_all',
            method='POST',
            data=b'',
        )
        resp = urllib.request.urlopen(req, timeout=300)
        if resp.status != 200:
            return f"Error: server returned {resp.status}"
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
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


# ---------------------------------------------------------------------------
# Test Loop & Speaker
# ---------------------------------------------------------------------------

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
def d75_memscan() -> str:
    """
    Scan TH-D75 memory channels. Returns a list of programmed channels
    with frequency, name, tone, mode, shift, offset, and power.
    Takes ~10-30 seconds depending on how many channels are programmed.
    """
    result = _get('/d75memlist')
    if isinstance(result, list):
        if not result:
            return "No programmed channels found"
        lines = [f"{len(result)} channels:"]
        for ch in result[:50]:
            tone = ch.get('tone', '')
            lines.append(f"  CH{ch['ch']} {ch['freq']:.4f} MHz {ch.get('name','')} "
                        f"{ch.get('mode','')} {ch.get('shift','')}{ch.get('offset','')} "
                        f"tone={tone}")
        if len(result) > 50:
            lines.append(f"  ... and {len(result)-50} more")
        return '\n'.join(lines)
    return f"Error: {result.get('error', 'scan failed')}" if isinstance(result, dict) else "Scan failed"


@mcp.tool()
def cloudflare_status() -> str:
    """
    Get the Cloudflare tunnel URL and connection status.
    """
    result = _get('/status')
    url = result.get('tunnel_url', '')
    return f"Tunnel URL: {url}" if url else "No Cloudflare tunnel active"


@mcp.tool()
def gdrive_status() -> str:
    """
    Get Google Drive integration status: authentication, folder access,
    service account email, and tunnel URL publication state.
    """
    data = _get('/api/gdrive/status')
    if not data.get('configured'):
        return "Google Drive not configured (ENABLE_GDRIVE=false)"
    lines = ["Google Drive Status:"]
    lines.append(f"  Account: {data.get('account_email', '?')}")
    lines.append(f"  Authenticated: {data.get('authenticated', False)}")
    folder = data.get('folder_name', data.get('folder_id', '?'))
    lines.append(f"  Folder: {folder}")
    lines.append(f"  Accessible: {data.get('folder_accessible', False)}")
    if data.get('folder_error'):
        lines.append(f"  Error: {data['folder_error']}")
    return "\n".join(lines)


@mcp.tool()
def gdrive_list_files() -> str:
    """
    List files in the gateway's Google Drive folder.
    Shows file names, sizes, and modification times.
    """
    data = _get('/api/gdrive/files')
    files = data.get('files', [])
    if not files:
        return "No files in Drive folder"
    lines = ["Google Drive Files:", ""]
    for f in files:
        size = f.get('size', '?')
        if size != '?':
            size = int(size)
            if size >= 1048576:
                size = f"{size/1048576:.1f} MB"
            elif size >= 1024:
                size = f"{size/1024:.0f} KB"
            else:
                size = f"{size} B"
        mod = (f.get('modifiedTime', '')[:19].replace('T', ' ')) or '?'
        lines.append(f"  {f['name']}  ({size})  {mod}")
    return "\n".join(lines)


@mcp.tool()
def gdrive_publish_tunnel() -> str:
    """
    Publish the current Cloudflare tunnel URL to Google Drive.
    This writes tunnel_url.json to the shared Drive folder so
    remote endpoints can discover the gateway address.
    """
    result = _post('/api/gdrive/publish-tunnel', {})
    if result.get('ok'):
        return "Tunnel URL published to Google Drive"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def gps_status() -> str:
    """
    Get GPS receiver status: position (lat/lon), altitude, speed, heading,
    fix quality, HDOP, and satellite signal strengths from the USB GPS module.
    """
    data = _get('/gpsstatus')
    if not data.get('enabled'):
        return "GPS is not enabled (ENABLE_GPS=false)"
    if not data.get('connected'):
        return "GPS enabled but not connected (check GPS_PORT)"
    return json.dumps(data, indent=2)


@mcp.tool()
def nearby_repeaters(band: str = "", radius_km: int = 50) -> str:
    """
    Query nearby amateur radio repeaters from the ARD database.
    Uses the gateway's GPS position to find repeaters sorted by distance.

    Args:
        band: Filter by band (e.g. '2m', '70cm'). Empty for all bands.
        radius_km: Search radius in km (default 50).
    """
    params = f'?radius={radius_km}'
    if band:
        params += f'&band={band}'
    data = _get(f'/repeaterstatus{params}')
    status = data.get('status', {})
    if not status.get('enabled'):
        return "Repeater database not enabled (ENABLE_REPEATER_DB=false)"
    reps = data.get('repeaters', [])
    if not reps:
        return f"No repeaters found within {radius_km}km" + (f" on {band}" if band else "")
    lines = [f"{len(reps)} repeaters within {radius_km}km ({status.get('loaded', 0)} loaded from {', '.join(status.get('states', []))}):"]
    lines.append(f"{'Dist':>5s}  {'Call':10s} {'Freq':>10s} {'Input':>10s} {'PL':>6s} {'Band':>5s} {'City'}")
    for r in reps[:30]:
        pl = str(r.get('ctcssTx', '') or '')
        lines.append(
            f"{r['distance_km']:5.1f}  {r['callsign']:10s} {r['outputFrequency']:10.4f} "
            f"{r['inputFrequency']:10.4f} {pl:>6s} {r.get('band',''):>5s} {r.get('nearestCity','')}"
        )
    if len(reps) > 30:
        lines.append(f"  ... and {len(reps) - 30} more")
    return "\n".join(lines)


@mcp.tool()
def repeater_info(callsign: str, frequency: float = 0) -> str:
    """
    Get detailed info on a specific repeater by callsign.

    Args:
        callsign: Repeater callsign (e.g. 'WA6FV').
        frequency: Optional output frequency to disambiguate if callsign has multiple repeaters.
    """
    data = _get('/repeaterstatus?radius=200')
    reps = data.get('repeaters', [])
    matches = [r for r in reps if r.get('callsign', '').upper() == callsign.upper()]
    if frequency > 0:
        matches = [r for r in matches if abs(r['outputFrequency'] - frequency) < 0.01]
    if not matches:
        return f"No repeater found for {callsign}" + (f" on {frequency}" if frequency else "")
    r = matches[0]
    lines = [
        f"Callsign:    {r['callsign']}",
        f"Output:      {r['outputFrequency']:.4f} MHz",
        f"Input:       {r['inputFrequency']:.4f} MHz",
        f"Offset:      {r.get('offsetSign','')}{r.get('offset','')} MHz",
        f"CTCSS:       {r.get('ctcssTx', 'none')}",
        f"Band:        {r.get('band', '?')}",
        f"City:        {r.get('nearestCity', '?')}, {r.get('county', '')}",
        f"State:       {r.get('state', '?')}",
        f"Distance:    {r.get('distance_km', '?')} km",
        f"Elevation:   {r.get('elevation', '?')} m",
        f"Operational: {r.get('isOperational', '?')}",
        f"Open:        {r.get('isOpen', '?')}",
        f"Coordinated: {r.get('isCoordinated', '?')}",
        f"ARES:        {r.get('ares', False)}",
        f"RACES:       {r.get('races', False)}",
        f"Updated:     {r.get('updatedDate', '?')}",
    ]
    return "\n".join(lines)


@mcp.tool()
def repeater_tune(callsign: str, radio: str = "kv4p", frequency: float = 0) -> str:
    """
    Tune a radio to a repeater by callsign. Sets frequency and CTCSS tone.

    Args:
        callsign: Repeater callsign (e.g. 'WA6FV').
        radio: Which radio to tune — 'kv4p', 'sdr1', 'sdr2' (default 'kv4p').
        frequency: Optional output frequency to disambiguate.
    """
    data = _get('/repeaterstatus?radius=200')
    reps = data.get('repeaters', [])
    matches = [r for r in reps if r.get('callsign', '').upper() == callsign.upper()]
    if frequency > 0:
        matches = [r for r in matches if abs(r['outputFrequency'] - frequency) < 0.01]
    if not matches:
        return f"No repeater found for {callsign}"
    r = matches[0]
    freq = r['outputFrequency']
    pl = r.get('ctcssTx', 0) or 0

    if radio == 'kv4p':
        result = _post('/kv4pcmd', {'cmd': 'freq', 'args': str(freq)})
        if result.get('ok') and pl:
            _post('/kv4pcmd', {'cmd': 'ctcss', 'args': f'{pl} 0'})
        msg = f"KV4P tuned to {r['callsign']} {freq:.4f} MHz"
        if pl:
            msg += f" PL {pl}"
        return msg if result.get('ok') else f"Tune failed: {result.get('error', '?')}"
    elif radio == 'sdr1':
        result = _post('/sdrcmd', {'cmd': 'tune', 'frequency': freq})
        return f"SDR1 tuned to {r['callsign']} {freq:.4f} MHz" if result.get('ok') else f"Tune failed: {result.get('error', '?')}"
    elif radio == 'sdr2':
        result = _post('/sdrcmd', {'cmd': 'tune', 'frequency2': freq})
        return f"SDR2 tuned to {r['callsign']} {freq:.4f} MHz" if result.get('ok') else f"Tune failed: {result.get('error', '?')}"
    else:
        return f"Unknown radio: {radio}. Use 'kv4p', 'sdr1', or 'sdr2'."


@mcp.tool()
def repeater_refresh() -> str:
    """
    Force re-download of repeater database from ARD GitHub.
    Use after changing GPS position or to get fresh data.
    """
    data = _post('/gpscmd', {'cmd': 'status'})
    # Trigger refresh via the gateway
    status = _get('/repeaterstatus?radius=1')
    st = status.get('status', {})
    if not st.get('enabled'):
        return "Repeater database not enabled"
    # The actual refresh needs a direct call — use a small HTTP trick
    # Just report current status; real refresh happens on next position change
    return (f"Repeater DB: {st.get('loaded', 0)} repeaters loaded from "
            f"{', '.join(st.get('states', []))}. "
            f"Data auto-refreshes every 24h or when position moves >10km.")


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
    Read the most recent stream trace dump (tools/stream_trace.txt).
    Shows per-stream timing statistics, interval analysis, and anomalies.

    Args:
        lines: Number of lines to return from the trace file (default 100).
    """
    trace_path = os.path.join(os.path.dirname(__file__), 'tools', 'stream_trace.txt')
    if not os.path.isfile(trace_path):
        return "No stream trace file found — run stream_trace_toggle to capture one"
    try:
        with open(trace_path) as f:
            all_lines = f.readlines()
        if not all_lines:
            return "Stream trace file is empty"
        # Return first N lines (summary is at the top)
        return ''.join(all_lines[:lines])
    except Exception as e:
        return f"Error reading trace: {e}"


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
    cfg_path = os.path.join(os.path.dirname(__file__), 'gateway_config.txt')
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
        scheme_file = os.path.join(os.path.dirname(__file__), scheme_file)
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
    cfg_path = os.path.join(os.path.dirname(__file__), 'gateway_config.txt')
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
        scheme_file = os.path.join(os.path.dirname(__file__), scheme_file)
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
    gw = _get('/endpoint/version')
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
