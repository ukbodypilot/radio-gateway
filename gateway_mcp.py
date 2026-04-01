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
    Retune an SDR receiver channel to a new frequency.  Restarts both tuners (~12s).

    Args:
        freq_mhz:    Frequency in MHz (e.g. 118.1 for aircraft, 162.55 for NOAA weather).
        channel:     SDR channel number — 1 or 2 (default 1).
        squelch_db:  Optional squelch threshold in dBFS (negative, e.g. -40.0).
                     Omit to keep current squelch.
    """
    freq_key = 'frequency' if channel == 1 else 'frequency2'
    squelch_key = 'squelch_threshold' if channel == 1 else 'squelch_threshold2'
    payload: dict = {'cmd': 'tune', freq_key: freq_mhz}
    if squelch_db is not None:
        payload[squelch_key] = squelch_db
    result = _post('/sdrcmd', payload, timeout=20)
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
        radio: One of 'th9800' (main HF/VHF transceiver),
               'd75' (Kenwood TH-D75 handheld), or
               'kv4p' (KV4P HT USB radio module).
    """
    radio = radio.lower().strip()
    if radio not in ('th9800', 'd75', 'kv4p'):
        return f"Error: unknown radio '{radio}' — must be th9800, d75, or kv4p"
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
              'btstart'    — Start Bluetooth connection
              'btstop'     — Stop Bluetooth connection
              'reconnect'  — Reconnect TCP + auto BT start
              'start_service' — Start d75-cat systemd service
              'cat'        — Send raw CAT command (put command in args)
              'vol'        — Set audio boost 0-500% (put value in args)
              'ptt'        — Toggle PTT on D75
        args: Arguments for the command (e.g. CAT command string like 'FO 0',
              or volume percentage like '200').
    """
    cmd = cmd.lower().strip()
    valid = ('btstart', 'btstop', 'reconnect', 'start_service', 'cat', 'vol', 'ptt')
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
        source: Audio source — one of:
                'global' (TX+RX), 'tx', 'rx', 'sdr1', 'sdr2', 'd75',
                'kv4p', 'remote', 'announce', 'speaker'
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
        valid = ('global', 'tx', 'rx', 'sdr1', 'sdr2', 'd75',
                 'kv4p', 'remote', 'announce', 'speaker')
        if source not in valid:
            return f"Error: source must be one of: {', '.join(valid)}"
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
                    'kv4p_tx', 'd75_tx', 'aioc_tx'}
        if bus_or_sink in sink_ids:
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
                    'kv4p_tx', 'd75_tx', 'aioc_tx'}
        if bus_or_sink in sink_ids:
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
        gain_percent: Gain as percentage (0-200, where 100 = unity)
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    mcp.run(transport='stdio')
