---
name: Listen bus unification (COMPLETED)
description: Primary listen bus moved from gateway_core main loop into BusManager — completed on branch 3.0
type: project
---

## Listen Bus Unification — COMPLETED (2026-04-06, branch: 3.0)

**What changed:**
- Primary listen bus is now created and ticked by BusManager alongside all other buses
- Single code path: all buses go through `_tick_loop()` → `_deliver_audio()`
- `sync_listen_bus()` in BusManager replaces `sync_mixer_sources()` in gateway_core

**Key implementation:**
- `_handle_listen_tick()`: post-tick handler for SDR rebroadcast queue, health flags, ducked states, VAD, click suppression, EchoLink, automation recorder
- `_deliver_audio()`: extended with VAD gating for mumble, link endpoint TX sending, broadcastify level tracking, level decay
- `drain_sdr_rebroadcast()`: queue-based cross-thread exchange for SDR-only audio + PTT state

**Main loop (gateway_core.audio_transmit_loop) now only:**
1. Self-clock (50ms tick)
2. Pending PTT handling
3. Drain BusManager queues (SDR rebroadcast, PCM, MP3)
4. SDR rebroadcast TX control (PTT, audio output)
5. WebSocket PCM/MP3 push
6. Trace recording

**Removed from gateway_core (~500 lines):**
- `self.mixer = ListenBus(...)` creation
- `_source_on_listen_bus()` method
- `sync_mixer_sources()` method
- All `mixer.add_source()` calls in source setup
- `_listen_bus_processor` setup
- All sink delivery code (mumble, speaker, broadcastify, transcription, remote audio, link endpoints, echolink, MP3)
- Click suppression, VAD gating, silence handling

**Net change:** -201 lines across 4 files (bus_manager.py, gateway_core.py, web_server.py, web_routes_get.py)
