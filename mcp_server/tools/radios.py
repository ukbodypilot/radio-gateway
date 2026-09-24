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

from mcp_server.server import mcp, _get, _post, GW_BASE_URL, GW_ROOT


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
# ---------------------------------------------------------------------------
# Tools — KV4P Radio
# ---------------------------------------------------------------------------
@mcp.tool()
def kv4p_status(instance: str = '') -> str:
    """
    Get KV4P HT radio status: USB connection state, frequency, squelch,
    CTCSS tones, power level, bandwidth, and audio levels.

    Args:
        instance: which kv4p endpoint to query (e.g. 'vhf', 'uhf'). Empty
                  returns the first connected kv4p endpoint.
    """
    qs = f'?instance={instance}' if instance else ''
    return json.dumps(_get(f'/kv4pstatus{qs}'), indent=2)


@mcp.tool()
def kv4p_command(
    cmd: str,
    args: str = '',
    instance: str = '',
) -> str:
    """
    Send a command to a KV4P HT radio.

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
        instance: which kv4p endpoint to target (e.g. 'vhf', 'uhf'). Empty
                  routes to the first connected kv4p endpoint.
    """
    cmd = cmd.lower().strip()
    valid = ('freq', 'txfreq', 'squelch', 'ctcss', 'bandwidth', 'power',
             'ptt', 'vol', 'reconnect')
    if cmd not in valid:
        return f"Error: cmd must be one of: {', '.join(valid)}"
    payload = {'cmd': cmd, 'args': args}
    if instance:
        payload['instance'] = instance
    result = _post('/kv4pcmd', payload, timeout=10)
    if result.get('ok'):
        resp = result.get('response', '')
        return f"KV4P {cmd} OK" + (f': {resp}' if resp else '')
    return f"KV4P {cmd} failed: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — IC-7100 HF/VHF/UHF Radio
# ---------------------------------------------------------------------------
@mcp.tool()
def ic7100_status() -> str:
    """
    Get IC-7100 status: link-endpoint connection, frequency, mode, S-meter,
    TX/RX state, RF/mic gain, CTCSS, TX antenna-port interlocks, audio levels.
    """
    return json.dumps(_get('/ic7100status'), indent=2)


@mcp.tool()
def ic7100_command(cmd: str, args: str = '') -> str:
    """
    Send a command to the IC-7100. The radio runs as a link endpoint on MX
    (192.168.2.134); this tool forwards via the gateway's link server.

    Args:
        cmd: one of:
          'ptt'           — toggle PTT (or send state via panel UI)
          'freq'          — tune RX/TX frequency in MHz (put MHz in args, e.g. '14.234')
          'mode'          — set mode (args: 'USB' / 'LSB' / 'CW' / 'CW-R' / 'AM' / 'FM' / 'WFM' / 'RTTY' / 'RTTY-R' / 'DV')
          'ctcss'         — set CTCSS tones (panel format — use ic7100_command via JSON for advanced)
          'tx_interlock'  — toggle TX antenna-port safety interlocks (hf/vu)
          'rx_gain'       — set RX gain in dB (put dB in args)
          'tx_gain'       — set TX gain / mic gain in dB (put dB in args)
          'mute'          — toggle RX mute
          'vol'           — set RX listening volume 0-500% (put percent in args)
          'cat'           — raw CI-V hex passthrough (put hex string in args)
          'status'        — request a fresh status push
        args: value for the command (frequency, gain, hex bytes, etc.).
    """
    cmd = cmd.lower().strip()
    valid = ('ptt', 'freq', 'mode', 'ctcss', 'tx_interlock', 'rx_gain',
             'tx_gain', 'mute', 'vol', 'cat', 'status')
    if cmd not in valid:
        return f"Error: cmd must be one of: {', '.join(valid)}"
    payload = {'cmd': cmd, 'args': args}
    # Many of the IC-7100 commands expect typed fields in the JSON payload
    # rather than a string 'args'. Map the common ones for convenience.
    if cmd == 'freq':
        # already handled server-side via 'args' parsing
        pass
    elif cmd == 'mode':
        payload['mode'] = args
    elif cmd in ('rx_gain', 'tx_gain'):
        try:
            payload['db'] = float(args)
        except (TypeError, ValueError):
            return f"Error: {cmd} needs a number in dB"
    elif cmd == 'vol':
        try:
            payload['value'] = int(args)
        except (TypeError, ValueError):
            return "Error: vol needs an integer 0-500"
    result = _post('/ic7100cmd', payload, timeout=10)
    if result.get('ok'):
        resp = result.get('response', '')
        return f"IC-7100 {cmd} OK" + (f': {resp}' if resp else '')
    return f"IC-7100 {cmd} failed: {result.get('error', 'unknown')}"


@mcp.tool()
def ic7100_frequency(freq_mhz: float, mode: str = '') -> str:
    """
    Tune the IC-7100 to a specific frequency.

    Args:
        freq_mhz: frequency in MHz (e.g. 14.234, 146.520, 446.000).
        mode:     optional mode (USB/LSB/CW/CW-R/AM/FM/WFM/RTTY/RTTY-R/DV).
                  Default: leave mode unchanged. Common bands:
                    HF SSB phone   — USB above 10 MHz, LSB below
                    2m/70cm voice  — FM
                    digital voice  — DV
    """
    r = _post('/ic7100cmd', {'cmd': 'freq', 'args': str(freq_mhz)}, timeout=10)
    if not r.get('ok'):
        return f"IC-7100 tune failed: {r.get('error', 'unknown')}"
    out = f"IC-7100 tuned to {freq_mhz:.4f} MHz"
    if mode:
        m = _post('/ic7100cmd', {'cmd': 'mode', 'mode': mode.upper(),
                                 'args': mode.upper()}, timeout=10)
        if m.get('ok'):
            out += f" ({mode.upper()})"
        else:
            out += f" — mode change failed: {m.get('error', 'unknown')}"
    return out


@mcp.tool()
def ic7100_vfo(action: str = 'select', vfo: str = 'A') -> str:
    """
    VFO control on the IC-7100.

    Args:
        action: 'select' | 'swap' | 'equalize'
                  - select: switch to VFO A or B (also leaves memory mode)
                  - swap:   exchange A <-> B
                  - equalize: copy active VFO to inactive (A=B)
        vfo:    'A' or 'B' (used only when action='select').
    """
    action = action.lower().strip()
    if action == 'select':
        payload = {'cmd': 'vfo', 'vfo': vfo.upper().strip()}
    elif action == 'swap':
        payload = {'cmd': 'vfo_swap'}
    elif action == 'equalize':
        payload = {'cmd': 'vfo_equalize'}
    else:
        return "Error: action must be one of: select, swap, equalize"
    r = _post('/ic7100cmd', payload, timeout=10)
    if r.get('ok'):
        return f"IC-7100 VFO {action} OK (active={r.get('active_vfo', '?')})"
    return f"IC-7100 VFO {action} failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_recall(channel: int) -> str:
    """
    Switch the IC-7100 to memory mode and recall channel (1..99).
    The radio retunes to whatever is stored in that channel.
    """
    r = _post('/ic7100cmd', {'cmd': 'memory_select', 'channel': int(channel)},
              timeout=10)
    if r.get('ok'):
        return f"IC-7100 recalled memory channel {channel}"
    return f"IC-7100 memory recall failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_mode(on: bool = True) -> str:
    """
    Switch the IC-7100 between VFO mode (on=False) and memory mode (on=True).
    Memory mode recalls whatever channel was last selected.
    """
    r = _post('/ic7100cmd', {'cmd': 'memory_mode', 'on': bool(on)}, timeout=10)
    if r.get('ok'):
        return f"IC-7100 memory_mode={r.get('memory_mode', on)}"
    return f"IC-7100 memory_mode failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_call_channel(which: str = '') -> str:
    """Select an IC-7100 CALL channel. The IC-7100 has FOUR call channels:
    144-C1, 144-C2, 430-C1, 430-C2 (two per VU band).

    Args:
        which: '144-C1', '144-C2', '430-C1', or '430-C2'. Empty/omitted
               picks the band-1 call channel for the current frequency
               (144-C1 on V/U below 300 MHz, 430-C1 above).
    """
    r = _post('/ic7100cmd', {'cmd': 'call_channel', 'which': which}, timeout=10)
    if r.get('ok'):
        return f"IC-7100 CALL channel selected (mem={r.get('memory_channel')})"
    return f"IC-7100 call channel failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_store() -> str:
    """
    Write the IC-7100's current VFO contents into the currently-selected
    memory channel. DESTRUCTIVE — overwrites whatever was in that channel.
    Use ic7100_memory_recall first to select the target channel, then
    ic7100_vfo+ic7100_frequency to set the contents to write, then this.
    """
    r = _post('/ic7100cmd', {'cmd': 'memory_write'}, timeout=10)
    if r.get('ok'):
        return "IC-7100 wrote VFO into current memory channel"
    return f"IC-7100 memory write failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_clear() -> str:
    """Clear the IC-7100's currently-selected memory channel. DESTRUCTIVE."""
    r = _post('/ic7100cmd', {'cmd': 'memory_clear'}, timeout=10)
    if r.get('ok'):
        return "IC-7100 cleared current memory channel"
    return f"IC-7100 memory clear failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_to_vfo() -> str:
    """
    Copy the IC-7100's current memory channel contents into the active VFO,
    so you can edit and (optionally) write back into the same or another slot.
    """
    r = _post('/ic7100cmd', {'cmd': 'memory_to_vfo'}, timeout=10)
    if r.get('ok'):
        return "IC-7100 memory copied to VFO"
    return f"IC-7100 memory_to_vfo failed: {r.get('error', 'unknown')}"


@mcp.tool()
def ic7100_memory_read(channel: int) -> str:
    """
    Read raw contents of an IC-7100 memory channel (1..99) without
    switching to it. Returns a hex blob of the channel data — split flag,
    mode, filter, frequency, tone settings, name. Decoding is up to the
    caller; the byte layout is the IC-7100 manual's memory-data format.
    """
    r = _post('/ic7100cmd',
              {'cmd': 'memory_read', 'channel': int(channel)}, timeout=10)
    if not r.get('ok'):
        return f"IC-7100 memory_read failed: {r.get('error', 'unknown')}"
    raw = r.get('raw_hex', '')
    if not raw:
        return f"Channel {channel}: empty / not allocated"
    return f"Channel {channel}: raw={raw}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tools — Process Supervisor (gateway-side daemons)
# ---------------------------------------------------------------------------
@mcp.tool()
def processes_status() -> str:
    """
    List every long-running process supervised by the gateway: kv4p loopback
    endpoints, cloudflared, pat, mDNS publisher, direwolf (when packet is in
    data mode), mumble (if SUPERVISE_MUMBLE is on). Each entry
    has state/pid/uptime/restart_count/last_exit/adopted.
    """
    return json.dumps(_get('/api/processes'), indent=2)


# ---------------------------------------------------------------------------
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
    Control the legacy per-source audio mixer — mute/unmute sources, adjust
    volume/boost, toggle duck, and control per-source processing flags
    (gate/HPF/LPF/notch). Operates on sources themselves (sdr1, d75, kv4p, …).

    For bus-level routing and mute in the v2 mixer, use bus_mute / sink_mute /
    routing_connect / routing_disconnect instead.

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
            return 'Error: flag required (vad, agc, echo_cancel, talkback)'
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
                 'radio').  Omit to return all settings.
    """
    cfg_path = os.path.join(GW_ROOT, 'gateway_config.txt')
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
# ---------------------------------------------------------------------------
# Tools — Process Control
# ---------------------------------------------------------------------------
@mcp.tool()
def process_control(service: str, action: str) -> str:
    """
    Start, stop, or restart a gateway sub-process.

    Args:
        service: Service to control — one of:
                 'sdr'      — SDR receiver (rtl_airband)
                 'd75'      — D75 CAT proxy service (d75-cat.service)
        action:  One of 'start', 'stop', 'restart'.
    """
    service = service.lower().strip()
    action = action.lower().strip()

    if action not in ('start', 'stop', 'restart'):
        return "Error: action must be 'start', 'stop', or 'restart'"

    if service == 'sdr':
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
        return f"Error: unknown service '{service}' — use sdr or d75"


# ---------------------------------------------------------------------------
# TH-D75 memory scan — moved from tools/routing.py 2026-05-30
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tools — TH-9800 CAT link recovery
# ---------------------------------------------------------------------------
@mcp.tool()
def cat_serial_status() -> str:
    """
    Ask the TH-9800 CAT bridge whether its serial link to the radio is up.
    Read-only. Use this to diagnose before cat_reconnect / cat_serial_connect.
    """
    result = _post('/catcmd', {'cmd': 'SERIAL_STATUS'}, timeout=10)
    if not result.get('ok'):
        return f"Failed: {result.get('error') or 'CAT client not connected (try cat_reconnect)'}"
    return f"CAT serial: {result.get('status', 'unknown')}"


@mcp.tool()
def cat_reconnect() -> str:
    """
    Reconnect the gateway's TCP link to the TH-9800 CAT bridge, creating a
    fresh client if there is none. Does not touch the radio itself.
    """
    result = _post('/catcmd', {'cmd': 'CAT_RECONNECT'}, timeout=20)
    if result.get('ok'):
        return "CAT link reconnected"
    return f"Failed: {result.get('error') or 'reconnect returned false'}"


@mcp.tool()
def cat_serial_connect() -> str:
    """
    Open the CAT bridge's serial connection to the TH-9800 (runs its ~4 s
    startup sequence and raises RTS). Use when cat_serial_status shows the
    serial side is down but the CAT link itself is up.
    """
    result = _post('/catcmd', {'cmd': 'SERIAL_CONNECT'}, timeout=25)
    if result.get('ok'):
        return f"CAT serial connected: {result.get('status', '')}"
    return f"Failed: {result.get('status') or result.get('error') or 'CAT client not connected'}"


@mcp.tool()
def cat_setup_radio() -> str:
    """
    Push the configured channels, volume and power settings to the TH-9800
    (the gateway's setup_radio routine). This CHANGES the radio's settings to
    match gateway_config.txt, so any manual front-panel changes are overwritten.
    It does not key the transmitter.
    """
    result = _post('/catcmd', {'cmd': 'SETUP_RADIO'}, timeout=90)
    if result.get('ok'):
        return "Radio setup from config complete"
    return f"Failed: {result.get('status') or result.get('error') or 'CAT client not connected'}"
