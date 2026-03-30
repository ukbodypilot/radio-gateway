# Mixer v2.0 — Progress & Tracking Log

## READ THIS FIRST
This file tracks the progress of the v2.0 mixer rewrite. If you are a new
Claude session, read this file AND `docs/mixer-v2-design.md` before doing
any work on the mixer.

**Branch:** `v2.0-mixer` (branched from `main` on 2026-03-29)
**Design doc:** `docs/mixer-v2-design.md`
**This file:** `docs/mixer-v2-progress.md`

## Current Status: LISTENBUS LIVE — PARITY VERIFIED

## Completed
- [x] Discussed use cases with user (2026-03-29)
- [x] Defined 4 bus types: duplex repeater, simplex repeater, listen, solo
- [x] Agreed source processing stays on sources (not in busses)
- [x] Agreed sources can be on multiple busses
- [x] Wrote design doc (`docs/mixer-v2-design.md`)
- [x] Branched `v2.0-mixer` from `main`
- [x] Built `audio_bus.py` — utilities, BusOutput, SourceSlot, DuckGroup, AudioBus base, ListenBus, stubs
- [x] Ported ducking state machine (DuckGroup) — generalized, no source names
- [x] Ported signal detection (hysteresis attack/release) to ListenBus
- [x] Ported fade-in/fade-out to module-level utilities
- [x] Ported `_mix_audio_streams` (additive + tanh limiter) to module-level utility
- [x] Wired ListenBus into gateway_core.py replacing AudioMixer
- [x] All 15 add_source call sites updated with bus_priority/duckable/deterministic
- [x] Main loop 8-tuple replaced with BusOutput consumption

- [x] Verified parity — gateway running on ListenBus, user confirmed working (2026-03-29)

## Next Steps (in order)
8. [x] Unified RadioPlugin.get_audio signature to return (bytes, bool) tuple (2026-03-29)
9. [x] Built SDRPlugin in sdr_plugin.py (RSPduo dual tuner, internal ducking, absorbed RTLAirbandManager) (2026-03-29)
9a. [x] Wired into gateway_core.py (replaces ~80 lines of SDR init with ~15 lines)
9b. [x] Wired web_server.py /sdrstatus and /sdrcmd to plugin
9c. [x] _SDRTunerView backward compat for sdr_source/sdr2_source references
9d. [x] FIXED — SIGSEGV/SIGABRT was caused by _SDRTunerView missing attribute proxying. Status bar code accessed _chunk_queue, _prebuffering, _watchdog_restarts etc. on the view, causing AttributeError in threads that corrupted PortAudio state. Fixed with __getattr__ proxy + safe defaults. Also moved SDR init before AIOC to avoid fork-after-Pa_Initialize. SDRPlugin live and working (2026-03-29).
10. [ ] Refactor TH-9800, D75, KV4P into plugins
11. [ ] Build SoloBus (takes a RadioPlugin)
12. [ ] Build DuplexRepeaterBus (connects two RadioPlugins)
13. [ ] Build routing UI page — column wiring view (sources | busses | sinks)
14. [ ] Build SimplexRepeaterBus (lower priority)
15. [ ] Merge to main when stable

## Design Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-29 | 4 bus types | Covers all current and planned use cases |
| 2026-03-29 | Source processing stays on source | Bus shouldn't care about HPF/LPF/gate — it just routes clean PCM |
| 2026-03-29 | Sources can be on multiple busses | Needed for e.g. D75 on solo + repeater |
| 2026-03-29 | Ducking is per-bus, priority-based | No more hardcoded "AIOC ducks SDRs" rules |
| 2026-03-29 | Duplex repeater is high priority | User needs it for cross-band linking |
| 2026-03-29 | Solo bus is the building block | Every radio starts solo, then connects to other busses |
| 2026-03-29 | Plugins replace source classes | Existing sources refactored (not wrapped) into RadioPlugin interface |
| 2026-03-29 | RSPduo = single plugin | Dual tuner master/slave ducking handled inside plugin, bus sees one source |
| 2026-03-29 | Plugins can be simple or complex | Standard interface for bus, hardware-specific methods for UI |
| 2026-03-29 | UI: column wiring view | 3-column layout (sources/busses/sinks) with visual connections, stacks on mobile |
| 2026-03-29 | Build plugins before busses | SDR plugin first, then SoloBus/DuplexRepeaterBus, then routing UI |

## Open Questions (from design doc)
1. Bus config format: INI or JSON? (not yet decided)
2. Web UI: own page or on Controls? (not yet decided)
3. Multiple listen busses? (not yet decided)
4. Link endpoint auto-creates solo bus? (not yet decided)
5. Announcement routing to specific bus or broadcast? (not yet decided)
6. SDR rebroadcast: becomes duplex repeater or stays special? (not yet decided)

## Test Log
| Date | Test | Result | Notes |
|------|------|--------|-------|
| 2026-03-29 | Gateway startup | PASS | No errors, all sources registered |
| 2026-03-29 | SDR audio flow | PASS | SDR1/SDR2 levels 61-68%, audio reaching sinks |
| 2026-03-29 | Mumble delivery | PASS | Connected, audio flowing |
| 2026-03-29 | Broadcastify stream | PASS | DarkIce running, 518KB sent, healthy RTT |
| 2026-03-29 | SDR peer ducking | PASS | SDR1 ducked by higher-priority SDR2 (correct) |
| 2026-03-29 | User acceptance | PASS | User confirmed normal operation |
| 2026-03-29 | SDRPlugin startup | PASS | rtl_airband starts, both tuners active |
| 2026-03-29 | SDRPlugin audio | PASS | SDR1 levels 60-73%, audio heard by user |
| 2026-03-29 | SDRPlugin + web UI | PASS | No crash when opening web UI (was SIGABRT) |
| 2026-03-29 | SDRPlugin stability | PASS | 150+ seconds stable with web UI polling |
| 2026-03-29 | SDR tuning (retune SDR2) | PASS | Retuned to 147.435, audio received |
| 2026-03-29 | SDR mute/unmute | PASS | SDR2 unmute via /sdrcmd worked |
| 2026-03-29 | Dead code cleanup | PASS | 1315 lines removed, gateway stable |
| 2026-03-29 | KV4PPlugin startup | PASS | Connected, fw v15, SA818_VHF, 146.400 MHz |
| 2026-03-29 | KV4PPlugin + web UI | PASS | /kv4pstatus returns full status, no crash |
| 2026-03-29 | KV4PPlugin stability | PASS | 150s stable with SDR + KV4P + Mumble |
| 2026-03-29 | KV4PPlugin PTT | PASS | Playback triggered PTT, audio transmitted OTA |
| 2026-03-29 | D75Plugin startup | PASS | TCP+serial+audio connected, TH-D75 identified |
| 2026-03-29 | D75Plugin commands | PASS | mute/ptt/btstart/CAT commands working |
| 2026-03-29 | D75Plugin audio RX | PASS | Audio confirmed working (BT range issue, not code) |
| 2026-03-29 | All 3 plugins stable | PASS | SDR+KV4P+D75 running simultaneously |
| 2026-03-29 | Dead code cleanup | PASS | D75AudioSource + D75CATClient removed (618 lines) |
| 2026-03-30 | Console removal | PASS | StatusBar, keyboard, ANSI display removed (649 lines) |
| 2026-03-30 | SoloBus | PASS | Built and smoke tested |
| 2026-03-30 | DuplexRepeaterBus | PASS | Built and smoke tested (full duplex cross-link) |
| 2026-03-30 | SimplexRepeaterBus | PASS | Built and smoke tested (store-and-forward) |
| 2026-03-30 | Console removal | PASS | StatusBar/keyboard/ANSI removed (-649 lines) |
| 2026-03-30 | Web modularization | PASS | 13 pages extracted to static HTML in web_pages/ |
| 2026-03-30 | Dead code cleanup | PASS | _generate_* methods removed (-5375 lines from web_server.py) |
| 2026-03-30 | Nav status fields | PASS | adsb_enabled + telegram_enabled added to /status |
| 2026-03-30 | Web dead code removal | PASS | 13 _generate_* methods removed (-5375 lines) |
| 2026-03-30 | Routing UI | PASS | Drawflow node editor, save/load, source/bus/sink wiring |
| 2026-03-30 | TX radio sinks | PASS | KV4P/D75/TH-9800 [TX] appear in sink column |
| 2026-03-30 | BusManager | PASS | Runs routing-configured busses alongside main loop |
| 2026-03-30 | SoloBus live audio | PASS | File Playback → Solo Bus → KV4P TX over the air |
| 2026-03-30 | Routing levels | PASS | RX/TX levels separated, all sources report levels |
| 2026-03-30 | Drawflow level bars | PASS | Live level bars in source/sink nodes |

## Known Issues
(none yet)

## Files Changed
- `audio_bus.py` — NEW: bus module (ListenBus, DuckGroup, SourceSlot, utilities, stubs)
- `gateway_core.py` — MODIFIED: imports ListenBus, replaces AudioMixer instantiation, 15 add_source calls updated, main loop 8-tuple replaced with BusOutput
- `docs/mixer-v2-design.md` — architecture design doc
- `docs/mixer-v2-progress.md` — this file
