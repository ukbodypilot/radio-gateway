"""Audio routing MCP tools — bus/sink wiring, connect/disconnect, mute,
processing filters, per-source gain, per-bus denoise tuning.

Originally a 1168-LOC catch-all; on 2026-05-30 the transcription,
link-endpoint, loop-recorder, cloud/drive, and repeater tools were
moved to their own modules under mcp_server/tools/. What remains is
pure audio routing.

Tools are registered against the shared ``mcp`` instance via
@mcp.tool() decorator side effects on import.
"""

import json

from mcp_server.server import mcp, _get, _post


# ---------------------------------------------------------------------------
# Routing status & live levels
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


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------
def _classify(a, b):
    """Work out whether a→b is source→bus or bus→sink, from the live graph.

    This used to be a hardcoded sink-id set plus an endswith('_tx') fallback.
    It had already drifted -- 'transcription' was missing, so connecting a bus
    to it was classified source-bus and silently did the wrong thing -- and a
    literal list of sink ids in an MCP tool is guaranteed to drift again every
    time a plugin adds one. /routing/status already publishes the real
    sources / busses / sinks, so ask.

    Returns (connection_type, error_or_None).
    """
    try:
        st = _get('/routing/status') or {}
    except Exception as e:
        return None, f"could not read the routing graph: {e}"
    ids = {kind: {str(x.get('id')) for x in (st.get(kind) or []) if isinstance(x, dict)}
           for kind in ('sources', 'busses', 'sinks')}
    if b in ids['sinks'] and a in ids['busses']:
        return 'bus-sink', None
    if b in ids['busses'] and a in ids['sources']:
        return 'source-bus', None
    # Say what is actually wrong rather than guessing and failing downstream.
    if a not in ids['sources'] | ids['busses']:
        return None, (f"'{a}' is not a known source or bus. "
                      f"Use routing_status() to list them.")
    if b not in ids['busses'] | ids['sinks']:
        return None, (f"'{b}' is not a known bus or sink. "
                      f"Use routing_status() to list them.")
    return None, (f"'{a}' → '{b}' is not a legal edge: connect a source to a "
                  f"bus, or a bus to a sink.")


def _edge_payload(cmd, connection_type, a, b):
    """Build the /routing/cmd body for one edge.

    The handler takes the roles by NAME -- source/bus/sink -- not from/to.
    These tools sent {'from': ..., 'to': ...} instead, so every call landed on
    the handler's final `return {'ok': False, 'error': 'specify source+bus or
    bus+sink'}`: routing_connect and routing_disconnect had never once worked,
    and they are the only callers of those two commands.
    """
    if connection_type == 'bus-sink':
        return {'cmd': cmd, 'bus': a, 'sink': b}
    return {'cmd': cmd, 'source': a, 'bus': b}


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
        connection_type, err = _classify(source_or_bus, bus_or_sink)
        if err:
            return f"Error: {err}"

    result = _post('/routing/cmd',
                   _edge_payload('connect', connection_type,
                                 source_or_bus, bus_or_sink))
    if result.get('ok'):
        return f"Connected {source_or_bus} → {bus_or_sink} ({connection_type})"
    # A refusal here is usually the one-bus-per-sink rule; the server's
    # message already names the sink and the bus that holds it.
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
        connection_type, err = _classify(source_or_bus, bus_or_sink)
        if err:
            return f"Error: {err}"

    result = _post('/routing/cmd',
                   _edge_payload('disconnect', connection_type,
                                 source_or_bus, bus_or_sink))
    if result.get('ok'):
        return f"Disconnected {source_or_bus} → {bus_or_sink}"
    return f"Error: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Bus lifecycle (create / delete / rename / mute)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Per-bus DSP toggles + gain
# ---------------------------------------------------------------------------
@mcp.tool()
def bus_toggle_processing(bus_id: str, filter_name: str) -> str:
    """
    Toggle an audio processing filter or stream output on a bus.

    Args:
        bus_id: The bus ID
        filter_name: One of:
                     'hpf'   — high-pass filter
                     'lpf'   — low-pass filter
                     'notch' — notch filter
                     'dfn'   — neural denoise (DPDFNet 8 kHz)
                     'agc'   — automatic gain control, last in the chain:
                               levels quiet and loud stations toward
                               -20 dBFS, holds between overs
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
# Per-bus neural denoise tuning
# ---------------------------------------------------------------------------
@mcp.tool()
def bus_set_denoise_mix(bus_id: str, mix: float) -> str:
    """
    Set the dry/wet mix of a bus's neural denoise filter.

    Args:
        bus_id: Bus id (e.g. 'main'). Run routing_status to list buses.
        mix:    0.0 = fully dry (denoise bypassed in the blend), 1.0 = fully
                wet (denoised only). Bounded to [0, 1].

    Useful when full denoise sounds over-processed on weak signals — a mix
    of 0.6–0.8 keeps some of the original texture. The filter must still be
    enabled via bus_toggle_processing('denoise') for this to have any effect.
    """
    result = _post('/routing/cmd',
                   {'cmd': 'set_dfn_mix', 'bus': bus_id, 'mix': mix})
    if result.get('ok'):
        return f"Bus {bus_id}: denoise mix → {result.get('mix')}"
    return f"Error: {result.get('error', 'unknown')}"


@mcp.tool()
def bus_set_denoise_bypass(bus_id: str, bypass_db: float) -> str:
    """
    Set the denoise bypass threshold for a bus, in dBFS. Audio chunks whose
    RMS falls below this level skip the denoise worker entirely.

    Args:
        bus_id:    Bus id (e.g. 'main'). Run routing_status to list buses.
        bypass_db: Threshold in dBFS, bounded to [-90, -20]. -60 is the
                   historical default.

    This is a CPU knob, not a quality knob: on a mostly-idle bus the squelch
    tail is silence, and denoising silence costs the same as denoising speech.
    Raising the threshold (e.g. -50) skips more chunks and saves more CPU, at
    the risk of clipping the quiet onset of a weak signal.
    """
    result = _post('/routing/cmd',
                   {'cmd': 'set_dfn_bypass', 'bus': bus_id, 'bypass_db': bypass_db})
    if result.get('ok'):
        return f"Bus {bus_id}: denoise bypass → {result.get('bypass_db')} dBFS"
    return f"Error: {result.get('error', 'unknown')}"


# ---------------------------------------------------------------------------
# Per-bus delay
# ---------------------------------------------------------------------------
@mcp.tool()
def bus_set_delay(bus_id: str, delay_ms: int) -> str:
    """
    Set a fixed output delay on a bus, in milliseconds (0-5000).

    Used to time-align a bus against another path — e.g. holding a local
    speaker bus back so it lines up with the latency of a streamed or
    linked copy of the same audio. 0 disables the delay.
    """
    result = _post('/routing/cmd',
                   {'cmd': 'set_bus_delay', 'bus': bus_id, 'delay_ms': delay_ms})
    if result.get('ok'):
        return f"Bus {bus_id}: delay → {result.get('delay_ms')} ms"
    return f"Error: {result.get('error', 'unknown')}"
