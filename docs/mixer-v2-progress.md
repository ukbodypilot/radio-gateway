# Mixer v2.0 — Progress & Tracking Log

## READ THIS FIRST
This file tracked the progress of the v2.0 mixer rewrite. The rewrite is
now complete and merged.

**Design doc:** `docs/mixer-v2-design.md`
**This file:** `docs/mixer-v2-progress.md`

## Current Status: COMPLETE — v2.0 released 2026-03-31

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
- [x] Unified RadioPlugin.get_audio signature to return (bytes, bool) tuple (2026-03-29)
- [x] Built SDRPlugin in sdr_plugin.py (RSPduo dual tuner, internal ducking, absorbed RTLAirbandManager) (2026-03-29)
- [x] Wired SDRPlugin into gateway_core.py (replaces ~80 lines of SDR init with ~15 lines)
- [x] Wired web_server.py /sdrstatus and /sdrcmd to plugin
- [x] Fixed SIGSEGV/SIGABRT from _SDRTunerView missing attribute proxying (2026-03-29)
- [x] Refactored TH-9800 into TH9800Plugin (AIOC + CAT + relays + PTT) (2026-03-30)
- [x] Refactored D75 into D75Plugin (2026-03-29)
- [x] Refactored KV4P into KV4PPlugin (2026-03-29)
- [x] Built SoloBus (2026-03-30)
- [x] Built DuplexRepeaterBus (2026-03-30)
- [x] Built SimplexRepeaterBus (2026-03-30)
- [x] Built routing UI with Drawflow node editor (2026-03-30)
- [x] Built BusManager to run routing-configured busses (2026-03-30)
- [x] Live level bars in Drawflow source/sink nodes (2026-03-30)
- [x] Mute buttons and gain sliders in Drawflow nodes (2026-03-30)
- [x] TX radio sinks (KV4P/D75/TH-9800 [TX]) in sink column (2026-03-30)
- [x] MumbleSource as routable source with PTT control (2026-03-30)
- [x] Removed console/terminal UI: StatusBar, keyboard, ANSI display (-649 lines) (2026-03-30)
- [x] Removed old AudioMixer and AIOCRadioSource (-900 lines)
- [x] Removed 13 _generate_* web methods (-5375 lines from web_server.py) (2026-03-30)
- [x] Extracted 13 web pages to static HTML in web_pages/ (2026-03-30)
- [x] Consolidated 13 static page routes to single _STATIC_PAGES lookup (2026-03-30)
- [x] Extracted utility classes to gateway_utils.py (2026-03-30)
- [x] Eliminated all backward compat aliases (2026-03-30)
- [x] Removed dead PTT code and old AIOC audio paths
- [x] Removed diagnostic trace prints (2026-03-31)
- [x] Full duplex Remote Audio (Windows client, ports 9600/9602)
- [x] Direct Icecast streaming (replaced DarkIce/FFmpeg/ALSA loopback)
- [x] Room Monitor as routable source with VAD
- [x] Web Mic in nav bar
- [x] Speaker virtual mode (prevents PipeWire feedback)
- [x] 14 new MCP tools for routing and automation (2026-03-31)
- [x] Config page updates (2026-03-31)
- [x] Merged to main (2026-03-31)

## Design Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-29 | 4 bus types | Covers all current and planned use cases |
| 2026-03-29 | Source processing stays on source | Bus shouldn't care about HPF/LPF/gate -- it just routes clean PCM |
| 2026-03-29 | Sources can be on multiple busses | Needed for e.g. D75 on solo + repeater |
| 2026-03-29 | Ducking is per-bus, priority-based | No more hardcoded "AIOC ducks SDRs" rules |
| 2026-03-29 | Duplex repeater is high priority | User needs it for cross-band linking |
| 2026-03-29 | Solo bus is the building block | Every radio starts solo, then connects to other busses |
| 2026-03-29 | Plugins replace source classes | Existing sources refactored (not wrapped) into RadioPlugin interface |
| 2026-03-29 | RSPduo = single plugin | Dual tuner master/slave ducking handled inside plugin, bus sees one source |
| 2026-03-29 | Plugins can be simple or complex | Standard interface for bus, hardware-specific methods for UI |
| 2026-03-29 | UI: column wiring view | 3-column layout (sources/busses/sinks) with visual connections |
| 2026-03-29 | Build plugins before busses | SDR plugin first, then SoloBus/DuplexRepeaterBus, then routing UI |
| 2026-03-30 | Drawflow for routing | Visual node editor with drag-and-drop wiring, live level bars |
| 2026-03-30 | All sinks gated by routing | No implicit audio flow -- everything must be wired |
| 2026-03-30 | Direct Icecast streaming | Eliminated DarkIce/FFmpeg/ALSA loopback chain |
| 2026-03-30 | Mumble as routable source/sink | Full integration with bus routing |
| 2026-03-31 | Routing config as JSON | More flexible than INI for nested bus/connection structures |

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
| 2026-03-30 | Web modularization | PASS | 13 pages extracted to static HTML in web_pages/ |
| 2026-03-30 | Dead code cleanup | PASS | _generate_* methods removed (-5375 lines from web_server.py) |
| 2026-03-30 | Nav status fields | PASS | adsb_enabled + telegram_enabled added to /status |
| 2026-03-30 | Routing UI | PASS | Drawflow node editor, save/load, source/bus/sink wiring |
| 2026-03-30 | TX radio sinks | PASS | KV4P/D75/TH-9800 [TX] appear in sink column |
| 2026-03-30 | BusManager | PASS | Runs routing-configured busses alongside main loop |
| 2026-03-30 | SoloBus live audio | PASS | File Playback -> Solo Bus -> KV4P TX over the air |
| 2026-03-30 | Routing levels | PASS | RX/TX levels separated, all sources report levels |
| 2026-03-30 | Drawflow level bars | PASS | Live level bars in source/sink nodes |
| 2026-03-30 | Source level metering | PASS | File Playback, Announce, WebMic report levels |
| 2026-03-30 | Alias cleanup | PASS | d75_cat/d75_audio_source/kv4p_cat/kv4p_audio_source removed |
| 2026-03-30 | Utility extraction | PASS | DDNSUpdater/EmailNotifier/CloudflareTunnel etc -> gateway_utils.py |
| 2026-03-30 | Route consolidation | PASS | 13 static page routes -> single _STATIC_PAGES lookup |
| 2026-03-30 | Routing UI controls | PASS | Mute buttons + gain sliders in Drawflow nodes |
| 2026-03-30 | TH9800Plugin skeleton | PASS | AIOC + CAT + relays + PTT in plugin |
| 2026-03-30 | MumbleSource | PASS | Mumble RX as bus source with PTT control |
| 2026-03-30 | TH9800Plugin wired | PASS | AIOC init replaced with plugin, clean audio flowing |
| 2026-03-30 | Audio rewrite | PASS | Blocking reader replaces PortAudio callback (reliable) |
| 2026-03-30 | Main loop fix | PASS | AttributeError on _chunk_queue was crashing every tick |
| 2026-03-30 | SDR autostart fix | PASS | Wait for sdrplay_apiService before rtl_airband |
| 2026-03-30 | All mute defaults | PASS | D75/KV4P/SDR2 default to unmuted |
| 2026-03-30 | All 4 radios as plugins | PASS | All audio flowing through plugin interface |
| 2026-03-31 | Diagnostic removal | PASS | Trace prints removed |
| 2026-03-31 | MCP tools | PASS | 14 new routing/automation tools added |
| 2026-03-31 | Final v2.0 verification | PASS | All busses, plugins, routing, sinks operational |

## Files Changed
- `audio_bus.py` — NEW: bus module (ListenBus, SoloBus, DuplexRepeaterBus, SimplexRepeaterBus, DuckGroup, SourceSlot, BusManager, utilities)
- `sdr_plugin.py` — NEW: SDRPlugin (RSPduo dual tuner, absorbed RTLAirbandManager)
- `th9800_plugin.py` — NEW: TH9800Plugin (AIOC + CAT + relays)
- `d75_plugin.py` — NEW: D75Plugin (BT audio + TCP CAT proxy)
- `kv4p_plugin.py` — NEW: KV4PPlugin (CP2102 USB serial + Opus)
- `gateway_utils.py` — NEW: extracted utility classes
- `web_pages/` — NEW: 13 static HTML pages including routing UI
- `gateway_core.py` — MODIFIED: bus-based routing, plugin instantiation, BusOutput main loop
- `web_server.py` — MODIFIED: _generate_* methods removed, static page serving, plugin API wiring
- `gateway_mcp.py` — MODIFIED: 14 new routing/automation tools
- `docs/mixer-v2-design.md` — UPDATED: architecture reference (status: COMPLETE)
- `docs/mixer-v2-progress.md` — this file

## Bugs Fixed During v2.0 Development
| Bug | Root Cause | Fix |
|-----|-----------|-----|
| All sources fell through to BusManager | `json` vs `json_mod` NameError silently caught in `_source_on_listen_bus()` | Use `json_mod.load()` |
| Double PCM push per tick | Main loop pushed PCM at two points | Remove duplicate push |
| Sinks not receiving audio | Hardcoded `'listen'` bus ID failed when bus renamed to `'sdr_mix'` | Use `self._listen_bus_id` |
| TH-9800 not routable | `aioc_tx`/`aioc` missing from `_get_radio_plugin` and `_get_source` maps | Add entries |
| Queue competition | TH-9800 on both primary mixer AND solo bus simultaneously | `_source_on_listen_bus()` guards |
| TX sink calling get_audio | `aioc_tx` resolved as SoloBus radio causing get_audio on TX-only path | `_tx_only` flag |
| Broadcastify/Mumble gated by VAD | Delivery placed after VAD gate | Move to early delivery path |
| Mumble RX 2fps choppy | `PTT_ACTIVATION_DELAY=0.5` sleep blocked pymumble callback thread | Remove sleep, bus handles PTT |
| PipeWire feedback loop | Speaker PortAudio auto-linked to SDR capture by WirePlumber | Virtual speaker + link guard |
| Old PTT routing to wrong radio | `ptt_required` triggered old TX path to KV4P | Disable entire old PTT block |
| `_padded` variable crash | Stale reference after code change crashed bus tick every 10s | Remove reference |
| Python 3.14 scoping | Local `import threading` in nested function shadowed module-level | Remove local import |
| Audio trace crash | `radio_source._serve_discontinuity` doesn't exist on TH9800Plugin | `getattr()` with defaults |
| Mumble Speak permission | Server ACL didn't grant Speak to 'all' group | Update SQLite ACL |
| Mumble TX stutter | GIL starvation — 33 threads + SCHED_RR starved pymumble to ~20 sends/sec | `audio_per_packet=0.06` (3 frames) |
| PCM buffer race | Single-value `_pcm_buffer` lost data on same-tick drain+deposit | List-based `_pcm_queue` |
| Stale sink level bars | Sink levels never decayed | 0.8x decay per poll |
| WebMonitorSource no audio | Returned None when sub-buffer < 4800B | Accumulation fix |
| Room Monitor too quiet | Default gain 1.0x vs Web Mic 25.0x | Default gain 25x |
| Remote audio source init crash | `bus_manager` not yet created, `not self.bus_manager` threw AttributeError | `getattr(self, 'bus_manager', None)` |
| RX level bleeding to TX bar | JS auto-mapping appended `_tx` to every level key | Skip for independent TX sinks |
| TX sink level not updating | Main loop updated `sv_audio_level` but routing reads `remote_audio_tx_level` | Update both |
| Remote audio cross-contamination | Source unconditionally added to mixer (bus_manager=None at init) | Defer to `sync_mixer_sources()` |
| Sink mute not working | `_get_plugin_by_id` didn't handle passive sinks | `_muted_sinks` set |
| Bus mute not blocking PCM/MP3 | Primary listen bus PCM/MP3 path not checking mute flag | Add `_listen_bus_muted` check |
| Mumble TX level bar stuck | Level not reported as 0 when sink disconnected | Always report 0 for disconnected sinks |
| Windows client WDM-KS crash | Blocking RawInputStream not supported | Callback-based streams with queues |

## Code Removed (Total: ~7,200+ lines)
| What | Lines |
|------|-------|
| Console/terminal UI (StatusBar, keyboard, ANSI) | ~650 |
| Old AudioMixer class | ~700 |
| AIOCRadioSource class | ~200 |
| `_generate_*` web methods (13 pages) | ~5,400 |
| Dead PTT code block | ~150 |
| Old fallback AIOC path | ~50 |
| `_create_source_gateway_shim` | ~70 |
| Diagnostic trace prints + counters | ~100 |
| Backward compat aliases, stale imports | ~30 |
| docs/MANUAL.txt (stale v1.5 manual) | ~1,600 |
| docs/img/ (duplicate screenshots) | 12 files |
