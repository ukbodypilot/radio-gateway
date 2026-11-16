"""Routing-graph rules shared by the UI, the HTTP API and BusManager.

One rule so far: a sink may be fed by at most one bus — except the sinks in
MULTI_BUS_SINKS, which keep a separate stream per feeding bus.

Nothing used to enforce it, and the graph editor happily draws the second
connection. What happens next depends entirely on the sink, and none of the
outcomes are "the two mix":

- Queue sinks (mumble, broadcastify, remote_audio_tx, speaker)
  share ONE bounded deque keyed by sink_id in BusManager._enqueue_sink. Two
  buses append into it and the drain thread sends each payload in turn, so the
  far end gets 50 ms fragments of the two sources ALTERNATING, at twice the
  real-time rate, until the queue backs up and starts dropping.
- broadcastify_l / broadcastify_r are a per-tick slot, not a queue. The second
  bus overwrites the first within the same tick and its audio is silently
  discarded. (The two SIDES are different sink ids, so the normal dual-channel
  feed is unaffected — that is two buses into two sinks.)
- Radio *_tx sinks are the worst. Every SoloBus builds its own _PttWorker
  (audio_bus.py), while _get_radio_plugin returns the SHARED plugin object, so
  two buses give two PTT threads driving one radio with private _desired /
  _applied state. When the first bus unkeys, the radio drops carrier; the
  second worker still believes it is keyed and its loop only acts on a CHANGE,
  so it never re-keys. The second bus then transmits into an unkeyed radio for
  the rest of its transmission, logging nothing, with every meter still moving.

Mixing several sources is a within-bus operation (mix_audio_streams, with the
ducking and priorities that go with it). Put the sources on one bus instead.
"""

# Sinks that no longer exist. 'recording' was a v1 stub; 'nul' (a drop-only
# sink buses were parked on so they "had a destination") was retired
# 2026-11-14: a bus with no sink already runs, meters, records (R) and streams
# (P/M) — the routing page now shows that on the bus itself. Old configs and
# stale browser tabs may still carry edges to these; they are stripped on
# load and on save, and never count as a conflict.
RETIRED_SINKS = frozenset({'recording', 'nul'})

# Sinks that handle each feeding bus as its own stream, so several buses do
# not interleave. 'transcription': BusManager calls transcriber.feed(audio,
# source_id=bus_id) directly (no shared _enqueue_sink deque), and
# RadioTranscriber keeps a feed queue, Silero VAD state, utterance buffer and
# worker thread PER bus (_get_or_create_stream), so every result carries the
# bus it came from. Before this rule existed (fd4812d, 2026-08) th9800, sdr2
# and main were transcribed at once. Speaker ID rides on the same streams.
# web_pages/routing.html carries the same list; tests/test_routing_sink_conflicts.py
# keeps the two in step.
MULTI_BUS_SINKS = frozenset({'transcription'})


def strip_retired_sinks(connections):
    """Return `connections` without bus->sink edges to a retired sink."""
    return [c for c in connections or []
            if not (c.get('type') == 'bus-sink' and c.get('to') in RETIRED_SINKS)]


def find_sink_conflicts(connections):
    """Find sinks fed by more than one bus.

    `connections` is the routing config list of {'type','from','to'} dicts.
    Returns a list of {'sink': id, 'busses': [bus_id, ...]}, sorted, empty
    when the graph is legal. Duplicate identical connections are collapsed
    first: they are the same edge saved twice, not two buses.
    """
    by_sink = {}
    for c in connections or []:
        if c.get('type') != 'bus-sink':
            continue
        sink = c.get('to')
        bus = c.get('from')
        if not sink or not bus or sink in RETIRED_SINKS or sink in MULTI_BUS_SINKS:
            continue
        by_sink.setdefault(sink, [])
        if bus not in by_sink[sink]:
            by_sink[sink].append(bus)
    return [{'sink': s, 'busses': b}
            for s, b in sorted(by_sink.items()) if len(b) > 1]


def describe_conflicts(conflicts):
    """One-line human summary, for an API error or a startup warning."""
    parts = [f"{c['sink']} ← {' + '.join(c['busses'])}" for c in conflicts]
    return ("A sink can only be fed by one bus (put the sources on one bus "
            "and let it mix them): " + "; ".join(parts))
