# Audio Routing

Bus-based audio mixer with a visual drag-and-drop editor at `/routing`. Every audio path in the gateway — radio RX, SDR audio, file playback, browser mic, transcription input, Mumble TX, stream output, link endpoint audio — is wired by connecting sources to buses and buses to sinks. No hard-coded paths in Python.

## Overview

Pre-v2 the gateway had a hand-written routing matrix in `gateway_core.py`. Adding a new source or sink meant editing Python. The v2 mixer replaces that with **buses** — named lanes that accept any number of sources and feed any number of sinks. Routing is saved as JSON in `routing_config.json` and edited in the browser. Code change to add a route: zero.

## Bus types

| Type | Behaviour |
|------|-----------|
| **Listen** | One-way receive. Sources mix into the bus, bus drives sinks. The default and most common type. |
| **Solo** | One-way receive, but only one source is "active" at a time — the first to break squelch holds the bus until it falls silent + tail. Used when several sources share one sink and you don't want them stepping on each other. |
| **Duplex Repeater** | Two solo buses paired — input-side (RX) and output-side (TX-with-PTT). Audio crossing in triggers PTT on the output radio. |
| **Simplex Repeater** | Single bus that records-then-plays-back with mic-tail handling. Half-duplex repeat through one radio. |

## Routing UI

`/routing` page — Drawflow canvas, mouse-wheel zoom, drag nodes to position. Click a connection to delete; drag from a source's right port to a bus's left port to create. Active connections animate. Each node has a live VU-style level bar.

**NUL sink** — drops audio without playing it. Use when you want to record a bus (`R` button) without also routing it to anywhere audible.

## Per-bus processing

Every bus has a processing chain you can toggle from its node:

| Button | Effect |
|--------|--------|
| `G` | Noise gate (numba-jit, < 10µs/chunk) |
| `H` | High-pass filter (300 Hz default) |
| `L` | Low-pass filter (3 kHz default) |
| `N` | Notch filter (60 Hz default, Q=30) |
| **`D`** | Neural denoise — RNNoise *or* DeepFilterNet 3 (selectable per bus). Phase-aligned wet/dry mix; per-bus attenuation cap. Inference runs off the bus tick so a slow chunk doesn't stall audio. |

The `D` button has a sub-popover with the engine select (`rnnoise` / `deepfilternet`), wet/dry mix slider (0–100 %), and attenuation cap in dB (DFN only — caps how much the model can pull down; 18 dB default).

## Per-bus streaming

Each bus can independently expose:

- **PCM WebSocket** — raw 16-bit mono at 48 kHz to the browser, for the routing-page audio cues and monitor pages
- **MP3 stream** — lazy-started ffmpeg encoder, only launches when a browser first subscribes
- **VAD-gated transcription** — feeds the per-bus VAD + ASR worker (see [transcription-pool.md](transcription-pool.md))
- **Loop recording** — per-bus continuous capture (see [loop-recorder.md](loop-recorder.md))

Toggle from the bus's node. Disabled streams cost nothing — no encoder is started until at least one consumer.

## Broadcastify uplink health: connected is not flowing

`stream_output.connected` means exactly one thing — **the Icecast SOURCE
handshake was accepted**. It has never meant "audio is reaching listeners", and
three places used to read it as if it did.

On 2026-08-21 the difference cost ten minutes of dead air. The uplink stalled;
ten reconnect attempts each completed their handshake and each logged
`Reconnected successfully`, while `rg_stream_bytes_sent_total` sat at a flat
**+0 for 2.5 of those minutes**. TCP connected, Icecast accepted, not one
payload byte moved. The 30 s health check agreed, printed `Stream recovered`
and emailed it five times. The incident was invisible in the log and obvious in
Prometheus — and only Prometheus was right.

Two claims now require evidence:

- **`_confirm_bytes_moving()`** holds a freshly established connection for up
  to `STREAM_CONNECT_CONFIRM` waiting for `_bytes_sent` to leave 0 before
  calling the reconnect a success. `_connect()` zeroes that counter, so the
  advance cannot be inherited from the previous connection. It logs the byte
  count and how long it took, or reports `connected but no data moved in Ns —
  NOT counting this as recovered`.
- **`data_flowing`** requires a byte to have reached the server within
  `STREAM_FLOW_STALE_AFTER`. The health check in `core/lifecycle.py` uses it in
  place of bare `connected`, so a mount pushing nothing is treated as down and
  alerts as down.

| Key | Default | Purpose |
|-----|---------|---------|
| `STREAM_CONNECT_CONFIRM` | `5.0` | Seconds a new connection has to move its first byte before the reconnect is called a failure |
| `STREAM_FLOW_STALE_AFTER` | `15.0` | Seconds without a byte on the wire after which `data_flowing` goes false |

Reconnects are also **serialised**. Concurrent workers previously raced, and a
reader bound to shared state would tear down its own *successor's* connection —
2026-08-19 logged 1850 reconnects in 3 h 42 m, with out-of-order attempt
numbers as the tell.

**Diagnosing:** a reconnect counter stuck at `0` means nothing ever *tried* —
the recovery path itself is wedged, which is a different bug from a failing
reconnect. Plot the raw byte counter, not a rate, when you need to tell a stall
from a slow feed.

## Speaker mode

Three modes — set in the routing UI:

- **virtual** — speaker is a metering-only sink. No audio actually plays on the host. Useful when the gateway runs headless and you don't want PortAudio holding a device handle.
- **auto** — try to open the default output device; fall back to virtual if it fails.
- **real** — try to open the default output device; warn if unavailable.

The mode persists across restarts (`SPEAKER_MODE` config key).

## Independent TX/RX muting

A radio plugin is one Python object but exposes itself to the router as **two** nodes: a source (RX, e.g. `th9800`) and a sink (TX, e.g. `th9800_tx`). Each has its own mute toggle. Muting the TX sink does NOT silence the RX source, and vice versa. Important for repeater chains where you want to keep listening even while transmit is muted.

## Rebroadcasting a receiver onto a radio

To put one receiver's audio out over a transmitter (e.g. an SDR onto the
TH-9800 — what the old "SDR rebroadcast" toggle did), wire it as routing:

1. On `/routing`, create a **solo** bus.
2. Connect the receiver you want to rebroadcast (`sdr1`, `sdr2`, a link
   endpoint, …) to that bus as a **source**.
3. Connect the transmitting radio's **TX sink** (`aioc_tx`, `<plugin>_tx`, …)
   to the same bus.

`SoloBus` then keys PTT whenever the source produces audio, holds it for
`PTT_RELEASE_DELAY`, buffers the audio produced during the key-up window so
the first syllable isn't clipped, and unkeys on release. Add more `*_tx`
sinks to the same bus to simulcast onto several radios.

The pre-v2 `sdr_rebroadcast` toggle (the `b` key, `/mixer` `flag=rebroadcast`,
and `SDR_REBROADCAST_PTT_HOLD`) was **removed in 2026-07**. It keyed the radio
from the main loop and wrote audio to a PortAudio stream the gateway stopped
opening in 2026-03 when AIOC TX moved into `TH9800Plugin` — so from then on it
transmitted a dead carrier. Routing supersedes it entirely.

## One bus per sink

**A sink may be fed by at most one bus.** The routing UI refuses the second
edge and the HTTP API rejects a config that contains one; `routing_rules.py`
holds the check, shared by the UI, the API and `BusManager` so all three agree.

This is not a stylistic rule. Two buses into one sink never mix — what happens
instead depends on the sink kind, and none of the outcomes are what the graph
suggests:

| Sink kind | What actually happens |
|-----------|----------------------|
| Queue sinks (`mumble`, `broadcastify`, `transcription`, `remote_audio_tx`, `speaker`) | Both buses append into **one** bounded deque keyed by `sink_id`. The drain thread sends each payload in turn, so the far end receives 50 ms fragments of the two sources **alternating**, at twice real-time, until the queue backs up and drops |
| `broadcastify_l` / `broadcastify_r` | A per-tick slot, not a queue. The second bus **overwrites** the first within the same tick and its audio is silently discarded |
| Radio `*_tx` sinks | The worst case. Every `SoloBus` builds its own `_PttWorker`, but `_get_radio_plugin` returns the **shared** plugin object — two PTT threads driving one radio with private `_desired`/`_applied` state. When the first bus unkeys the radio drops carrier; the second worker still believes it is keyed, and its loop only acts on a *change*, so it never re-keys. **The second bus then transmits into an unkeyed radio for the rest of its transmission**, logging nothing, with every meter still moving |

Mixing several sources is a **within-bus** operation (`mix_audio_streams`, with
the ducking and priorities that go with it). Put the sources on one bus instead.

The null sink (`nul`) is exempt — it discards audio and exists to park a bus,
so any number of buses may point at it.

## PTT: the sink kind decides the keying mechanism

Two different mechanisms key a transmitter, and which one applies depends on
what kind of sink you wired:

- **Link endpoints** are keyed by `bus_manager`'s deliver loop from **audio
  level** alone, once the bus exceeds `LINK_AUTO_PTT_THRESHOLD`.
- **Plugin radios** (TH-9800, KV4P, D75, …) are keyed from `SoloBus.tick()`
  Phase 2, historically from the source's **PTT flag** only.

That asymmetry was a silent trap. A source that never asserts the flag —
`RemoteAudioSource` ends every path with `return raw, False` — would happily
key a link endpoint while a plugin radio on the identical wiring never keyed at
all. The audio reached the bus, every meter moved, and the radio sat there.
Same bus type, same graph, different behaviour depending only on the sink.

Since v4.6.0 `SoloBus` applies a **level trigger as a fallback** for plugin
radios: if the source asserts no PTT flag but the mixed TX audio exceeds
`AUTO_PTT_THRESHOLD` (default: whatever `LINK_AUTO_PTT_THRESHOLD` is, so both
sink kinds behave alike), the bus keys anyway. Set it to `0` to restore
flag-only keying.

Two guards apply:

- It is gated on `_tx_only`. A radio that came from a **source** is RX+TX, and
  keying it on the level of audio it just received would key it from its own
  receiver.
- `tx_muted` is honoured, so the fallback can never newly key a radio the
  operator has muted.

The trigger is implemented in the bus rather than in the deliver loop
deliberately: the bus already owns `_PttWorker` for its radios, and a second
keyer on the same radio is the two-owners bug above all over again.

**Diagnosing:** no `PTT ON` line at all means the key was never *requested* —
look here. A `PTT ON` that produces no carrier is a hardware or plugin problem
instead.

## Transmit time-out

Whatever keys a radio (a bus, the web UI, the keyboard, an MCP tool), the
TH-9800 plugin and every link endpoint's `ptt` command pass through a
`PttTimeOutTimer` (`gateway_link.py`). After `PTT_TIMEOUT_SECONDS` keyed
(default 180, `0` = off) the radio is forced unkeyed and the log shows
`PTT time-out (180s) -- forcing unkey`. Because a bus that is still pushing
audio re-requests the key every tick, re-keying is then refused until the
requests **pause for 5 s**; the next real over keys normally.

So a repeater or rebroadcast bus fed by a source that never goes quiet (a stuck
squelch, a carrier, a looping file) transmits for at most three minutes, then
waits for a gap. Raise the limit, or set `0`, only if you have a reason to
transmit longer than that without a break. The browser mic keeps its own
120 s time-out on top (see [web-ui.md](web-ui.md)).

## Bus tick architecture

A single BusManager tick thread drives every bus's mix step every **50 ms**
(2400 samples at 48 kHz, the gateway's chunk size). It is the thread doing the
real audio work (source reads, mixing, ducking, sink delivery), so it is the
one that asks for realtime scheduling (`SCHED_RR`, priority 10; `nice -10` if
that is refused). The older `audio_transmit_loop` in `core/transmit.py` runs
at **normal priority** since 5.0.0: it only applies queued PTT changes, hands
audio to the MP3 stream encoder and records the trace, and at equal realtime
priority it could delay the tick.

The tick thread also owns Python's garbage collector. `gc.disable()` is
process-wide, so BusManager disables automatic collection and runs the
collections itself on the tick, at points where a pause is cheap (and
`gc.freeze()` once startup objects have settled). Before 5.0.0 only the tick
re-enabled collection, so a gateway with no busses never collected at all.

Sinks that would block on the tick (Broadcastify encoder, automation recorder, Mumble TX, EchoLink) run **off-tick** on per-sink drain threads — `_enqueue_sink` stages a sink call into a bounded deque (maxlen 8, drop-oldest); a `SinkDrain-<id>` daemon thread drains it. This keeps the bus tick at sub-millisecond jitter even when one sink is slow.

The transcriber, speaker, link TX, loop recorder, and remote audio TX were off-tick by design from v2 onward.

A tick that raises keeps running. The traceback is printed once, then a count
once a minute while the same error repeats, instead of 20 tracebacks a second.

## Configuration

The routing topology lives in **`routing_config.json`** (created on first save from the UI; not committed). Per-bus processing settings (filter toggles, denoise mix, denoise engine, loop hours) live inside the same file.

Static config keys that affect routing live in `[processing]` and `[switching]` sections of `gateway_config.txt`. See [config-reference.md](config-reference.md).

## Source pointers

- [`audio_bus.py`](../audio_bus.py) — `ListenBus`, `SoloBus`, `DuplexBus`, `SimplexBus` classes
- [`bus_manager.py`](../bus_manager.py) — bus lifecycle, routing reload, sink dispatch, per-bus AudioProcessor management
- [`sources/`](../sources/) — every source/sink class (FilePlayback, LoopPlayback, RemoteAudio*, NetworkAnnouncement, WebMic, WebMonitor, LinkAudio, EchoLink, StreamOutput)
- [`web_pages/routing.html`](../web_pages/routing.html) — Drawflow editor + node renderers
- [`web_server.py`](../web_server.py) — `_handle_routing_cmd` dispatcher and per-command handlers (now table-driven; one method per command)
