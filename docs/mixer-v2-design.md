# Mixer v2.0 — Bus Architecture Reference

## Status: COMPLETE — v2.0 released 2026-03-31

This page is the design as built for v2.0, kept as the architecture
reference. Where the code has moved on since, the current state is noted
inline; the operator-facing description is [audio-routing.md](audio-routing.md).

## Overview

The v2.0 architecture replaced the monolithic AudioMixer with a bus-based
routing system. All radios are plugins with a standard interface. All audio
flow is controlled by routing connections in a visual node editor.

## Bus Types

### 1. Listen Bus

**Purpose:** Monitor one or more sources, optionally route to sinks.

- Any number of sources, each with a priority number
- Audio from all sources is mixed (additive with soft tanh limiter)
- Ducking is priority-based: higher priority source ducks lower priority
- Signal detection with hysteresis (attack/release) prevents chatter
- Fade-in/fade-out at transitions prevents clicks
- Zero or more sinks: Broadcastify, Mumble, Speaker, Recording, etc.
- Replaces the old "simultaneous" mixing mode

### 2. Solo Bus

**Purpose:** Standalone control of a single radio with source/sink routing.

- One primary radio endpoint
- Additional sources can be attached (Web Mic, File Playback, Announcements)
- Additional sinks can be attached (Speaker, Recording)
- Sources route to the radio's TX
- Radio's RX routes to the sinks
- PTT control comes from attached sources with ptt_control=True
- Building block: every radio starts as a solo bus, then connects to other
  busses for cross-radio functionality

### 3. Duplex Repeater Bus

**Purpose:** Full duplex cross-link between two radios.

- Two radio endpoints assigned: Side A and Side B
- A's RX routes to B's TX and B's RX routes to A's TX simultaneously
- Both directions active at the same time (full duplex)
- Each side has independent PTT control
- Radios must be on different frequencies

### 4. Simplex Repeater Bus

**Purpose:** Half-duplex store-and-forward between two radios.

- Two radio endpoints assigned
- When Side A receives, audio is buffered
- When A's RX ends, the buffered audio is transmitted on Side B
- Then the link reverses: B's RX buffers, sent to A when B finishes
- Only one direction active at a time
- Configurable tail timer and courtesy tone

## Plugin Model

All radios are plugins with a standard interface. Existing source classes
were refactored into plugins (not wrapped).

**Standard interface** (bus talks to these):
```python
class RadioPlugin:
    name: str
    capabilities: set          # {rx, tx, ptt, frequency, ctcss, ...}
    processing: ProcessChain   # gate -> HPF -> LPF -> notch -> gain
    enabled: bool

    def setup(self) -> bool
    def teardown(self)
    def get_audio(self, chunk_size) -> (bytes, bool)  # (pcm, ptt_needed)
    def put_audio(self, pcm: bytes)                   # TX audio in
    def get_status(self) -> dict
```

**Implemented plugins (v2.0):**
- `SDRPlugin` — RSPduo dual tuner with internal master/slave ducking.
  Bus sees one source. Each tuner has its own frequency, processing chain,
  and UI controls within the plugin.
- `TH9800Plugin` — AIOC sound card + CAT serial + relay PTT
- `D75Plugin` — Bluetooth audio + TCP CAT proxy
- `KV4PPlugin` — CP2102 USB serial + Opus codec

**Since then:** local plugins live in `plugins/` and are auto-discovered
(`th9800.py`, `sdr.py`, `packet.py`, `usrp.py`, `usrp2.py`). The D75, KV4P and
IC-7100 became **link endpoints** (`tools/*_link_plugin.py`) and reach the bus
as one `LinkAudioSource` each; see [gateway_link.md](gateway_link.md).

Processing is owned by the plugin. The bus receives clean, processed PCM.

## Sink Model

A sink is anything that consumes audio. All sinks are gated by routing
connections -- no audio flows to a sink unless it is wired in the routing UI.

Implemented sinks: Broadcastify (Icecast direct), Mumble, Speaker, Recording,
Radio TX (KV4P/D75/TH-9800), Remote Audio.

## Routing

The routing table is the single source of truth for audio flow. It is
configured via the Drawflow visual node editor on the Routing page.

Sources, busses, and sinks appear as nodes in a 3-column layout. Connections
between nodes define audio flow. Live level bars show audio activity in
each node. Mute buttons and gain sliders are available per-node.

Routing configuration is saved/loaded as JSON.

A source can appear on multiple busses (e.g., D75 on both a solo bus and
a repeater bus).

**Changed since v2.0:** a **sink takes at most one bus**. The original plan
mixed TX audio from several busses before delivery, but the implementation
never did; two busses into one radio gave it two PTT owners and stranded the
transmitter. `routing_rules.py` now refuses the second edge. See
[audio-routing.md](audio-routing.md#one-bus-per-sink).

## Bus API

```python
class AudioBus:
    name: str
    bus_type: str  # 'listen', 'duplex_repeater', 'simplex_repeater', 'solo'

    def tick(self, chunk_size) -> BusOutput:
        """Called once per audio cycle."""

class BusOutput:
    audio: dict          # {sink_name: pcm_bytes}
    ptt: dict            # {radio_name: bool}
    active_sources: list
    status: dict         # ducking state, levels, etc.
```

The BusManager runs all routing-configured busses on one tick thread (50 ms,
`SCHED_RR`), which does every source read, mix and sink delivery and owns the
garbage collector. See
[audio-routing.md](audio-routing.md#bus-tick-architecture).

## Key Files (current)

- `audio_bus.py` — Bus classes, DuckGroup, SourceSlot, mixing utilities
- `bus_manager.py` — bus lifecycle, tick thread, routing reload, sink dispatch, PTT workers
- `routing_rules.py` — routing validation (one bus per sink), shared by UI, API and BusManager
- `plugins/sdr.py`, `plugins/th9800.py`, `plugins/packet.py`, `plugins/usrp.py`, `plugins/usrp2.py` — local plugins
- `plugin_loader.py` — plugin auto-discovery (`ENABLE_<PLUGIN_ID>`)
- `sources/` — every non-plugin source and sink, including `LinkAudioSource`
- `gateway_core.py`, `core/` — gateway object, lifecycle, monitor and transmit loops
- `web_pages/routing.html` — the Drawflow routing editor
