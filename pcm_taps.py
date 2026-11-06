"""Browser PCM monitor taps — the one place that knows every tappable source.

A tap feeds one source's audio straight into the browser PCM stream (the
AS1/AS2/SDR1/SDR2 buttons in the shell bar). Taps are EXCLUSIVE: the stream
carries the routing-selected buses, or exactly one tapped source, never a mix.

A tappable object has:
    pcm_tap       bool, set by its control endpoint
and EITHER
    tap_from_bus  True — the tap carries the processed output of the bus the
                  source is routed to (SDR tuners; see tap_bus_id). The bus
                  manager sets .tap_bus and counts .tap_chunks.
OR
    _tap_queue    bounded deque the source fills in its own audio path
    take_tap()    pops one chunk or returns None   (AllStar nodes)

Both the tick loop (which drains taps) and every control endpoint (which
enforces exclusivity) go through tap_sources(), so adding a new kind of
tappable source means adding it here once — not in each endpoint. Before this
module the AllStar endpoint walked only _external_plugins, so it could never
have cleared an SDR tap.
"""


def tap_sources(gw):
    """Every object that can be a PCM tap: plugin instances + SDR tuners."""
    if gw is None:
        return []
    out = list((getattr(gw, '_external_plugins', None) or {}).values())
    sdr = getattr(gw, 'sdr_plugin', None)
    if sdr is not None:
        for attr in ('_tuner1', '_tuner2'):
            t = getattr(sdr, attr, None)
            if t is not None:
                out.append(t)
    return [s for s in out if hasattr(s, 'pcm_tap')]


def set_tap(gw, src, want):
    """Turn src's tap on or off. Turning one on turns every other one off."""
    src.pcm_tap = bool(want)
    if not want:
        _clear(src)
        return
    for other in tap_sources(gw):
        if other is not src and getattr(other, 'pcm_tap', False):
            other.pcm_tap = False
            _clear(other)


def _clear(src):
    try:
        src._tap_queue.clear()
    except Exception:
        pass


def parse_on(value):
    """'1'/'true'/'on'/True → True; '0'/'false'/'off'/''/None → False."""
    return str(value).lower() not in ('0', 'false', 'off', '', 'none')


def tap_bus_id(busses, src):
    """Bus id whose output a bus-following tap on src should carry.

    busses is the BusManager's ordered {bus_id: bus}. A source routed to more
    than one bus follows the FIRST in routing order. None if src isn't on any
    bus (tap stays silent).

    Bus types hold sources in different places: ListenBus in source_slots,
    SoloBus in _tx_sources (a source->solo connection becomes a TX source,
    e.g. SDR1 -> PAPA) or as its _radio, repeater buses as _side_a/_side_b.
    Checking only source_slots made every solo-bus tap silent.
    """
    for bus_id, bus in (busses or {}).items():
        if _bus_holds(bus, src):
            return bus_id
    return None


def _bus_holds(bus, src):
    for attr in ('source_slots', '_tx_sources'):
        for slot in getattr(bus, attr, None) or ():
            if getattr(slot, 'source', None) is src:
                return True
    for attr in ('_radio', '_side_a', '_side_b'):
        if getattr(bus, attr, None) is src:
            return True
    return False
