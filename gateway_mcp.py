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


def _post(path: str, data: dict) -> dict:
    url = GW_BASE_URL + path
    body = json.dumps(data).encode()
    headers = {**_auth_headers(), 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
    label: str | None = None,
) -> str:
    """
    Retune an SDR receiver channel to a new frequency.

    Args:
        freq_mhz:    Frequency in MHz (e.g. 118.1 for aircraft, 162.55 for NOAA weather).
        channel:     SDR channel number — 1 or 2 (default 1).
        squelch_db:  Optional squelch threshold in dBFS (negative, e.g. -40.0).
                     Omit to keep current squelch.
        label:       Optional human-readable label for the channel (e.g. "Tower").
    """
    payload: dict = {'cmd': 'tune', 'channel': channel, 'freq': freq_mhz}
    if squelch_db is not None:
        payload['squelch'] = squelch_db
    if label is not None:
        payload['label'] = label
    result = _post('/sdrcmd', payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def sdr_restart() -> str:
    """
    Restart the rtl_airband SDR decoder process.  Use when SDR status shows
    STOPPED or audio has dropped out.  Restarts the sdrplay systemd service
    and relaunches rtl_airband.
    """
    result = _post('/sdrcmd', {'cmd': 'restart'})
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    mcp.run(transport='stdio')
