"""BGM beds, per-bed announcer, TTS engine and soundboard tools.

Tools registered against the shared ``mcp`` instance via @mcp.tool() side
effects on import. All of these are thin wrappers over the web routes in
web_routes_audio.py / web_routes_automation.py.
"""

import json

from mcp_server.server import mcp, _get, _post


def _fail(result: dict) -> str:
    return f"Failed: {result.get('error') or result.get('message') or 'unknown error'}"


# ---------------------------------------------------------------------------
# Tools — BGM beds
# ---------------------------------------------------------------------------
@mcp.tool()
def bgm_status() -> str:
    """
    Get the background-music (BGM) beds: which slot is playing and the state
    of each bed. Read-only.
    """
    data = _post('/bgm', {})
    if not data.get('ok'):
        return _fail(data)
    return json.dumps(data.get('bgm', data), indent=2)


@mcp.tool()
def bgm_control(slot: int, action: str = "toggle") -> str:
    """
    Start, stop or toggle a BGM bed. Only one bed loops at a time (they share
    the System Sounds playback source), and the audio follows that node's
    routing — if it is routed to a radio sink this transmits over the air.

    Args:
        slot:   Bed number — 1, 2, or 3.
        action: 'start', 'stop', or 'toggle' (default).
    """
    if slot not in (1, 2, 3):
        return "Error: slot must be 1, 2, or 3"
    if action not in ('start', 'stop', 'toggle'):
        return "Error: action must be 'start', 'stop' or 'toggle'"
    result = _post('/bgm', {'slot': slot, 'action': action})
    if not result.get('ok'):
        return _fail(result)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tools — Per-bed announcer
# ---------------------------------------------------------------------------
@mcp.tool()
def announcer_status() -> str:
    """
    Get the announcer state: master enable, interval, per-bed messages and
    voices, which bed is playing, and whether synthesised audio is live.
    The announcer speaks the message belonging to whichever BGM bed is playing.
    """
    data = _get('/announcer')
    if not data.get('ok'):
        return _fail(data)
    return json.dumps(data, indent=2)


@mcp.tool()
def announcer_configure(
    messages: dict = None,
    voices: dict = None,
    interval: float = None,
    max_seconds: float = None,
    enabled: bool = None,
) -> str:
    """
    Update the announcer. Only the fields you pass are changed. The text is
    saved even if synthesis fails, so a TTS outage never loses what was typed.

    Args:
        messages:    {bed_slot: text}, e.g. {"1": "Net starts at 8pm"}. Max 500 chars each.
        voices:      {bed_slot: voice_name}; use tts_engine_status for the active engine.
        interval:    Seconds between announcements (minimum 2).
        max_seconds: Cap on a single announcement's length (0 = no cap).
        enabled:     Master enable.
    """
    body = {}
    if messages is not None:
        body['messages'] = {str(k): v for k, v in messages.items()}
    if voices is not None:
        body['voices'] = {str(k): v for k, v in voices.items()}
    if interval is not None:
        body['interval'] = interval
    if max_seconds is not None:
        body['max_seconds'] = max_seconds
    if enabled is not None:
        body['enabled'] = enabled
    if not body:
        return "Error: nothing to change — pass at least one field"
    result = _post('/announcer', body, timeout=45)
    if result.get('ok') is False and result.get('error'):
        return f"Saved, but synthesis reported: {result['error']}"
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tools — TTS engine
# ---------------------------------------------------------------------------
@mcp.tool()
def tts_engine_status() -> str:
    """
    List the TTS engines, which one is active, and whether each is importable
    on this host. Read-only.
    """
    data = _get('/tts/engine')
    if not data.get('ok'):
        return _fail(data)
    lines = [f"active: {data.get('active') or '(none)'}   TTS enabled: {data.get('enabled')}"]
    for e in data.get('engines', []):
        flags = ('ACTIVE ' if e.get('active') else '') + ('' if e.get('available') else 'NOT INSTALLED')
        lines.append(f"  {e['value']:8s} {e.get('label', '')}  {flags}".rstrip())
    return '\n'.join(lines)


@mcp.tool()
def tts_engine_set(engine: str) -> str:
    """
    Hot-swap the TTS engine and persist it to config (no gateway restart).
    Voice names differ per engine, so per-bed announcer voices may need
    updating afterwards.

    Args:
        engine: An engine name from tts_engine_status (e.g. 'kokoro', 'edge', 'gtts').
    """
    result = _post('/tts/engine', {'engine': engine}, timeout=60)
    if not result.get('ok'):
        return _fail(result)
    return f"TTS engine now '{result.get('active')}'. {result.get('message', '')}".strip()


# ---------------------------------------------------------------------------
# Tools — Soundboard
# ---------------------------------------------------------------------------
@mcp.tool()
def soundboard_categories() -> str:
    """
    List soundboard categories with clip counts, which are selected, the pool
    size, and the per-clip length cap. Read-only.
    """
    data = _get('/soundboard/categories')
    if not data.get('ok'):
        return _fail(data)
    return json.dumps(data, indent=2)


@mcp.tool()
def soundboard_set_categories(categories: list) -> str:
    """
    Choose which soundboard categories feed the random pool and persist it.
    Unknown names are ignored. Selecting every category (or passing all valid
    names) stores a blank filter, meaning "all", so new categories are picked
    up automatically.

    Args:
        categories: Category names, e.g. ["comedy", "scifi"].
    """
    if not isinstance(categories, list):
        return "Error: categories must be a list of names"
    result = _post('/soundboard/categories', {'categories': categories})
    if not result.get('ok'):
        return _fail(result)
    return (f"Saved filter: {result.get('saved') or '(all categories)'} — "
            f"pool size {result.get('pool_size')}")


@mcp.tool()
def soundboard_refresh() -> str:
    """
    Clear the cached soundboard clips and re-fill the playback slots. Downloads
    finish in the background, so 'pending' slots are normal immediately after.
    """
    result = _post('/refreshsounds', {}, timeout=30)
    if not result.get('ok'):
        return _fail(result)
    return f"Refreshed: {result.get('count', 0)} cached, {result.get('pending', 0)} still filling"
