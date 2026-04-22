# Changelog

All notable changes to Radio Gateway.

## [Unreleased]

## [3.4.0] -- 2026-04-22

Deployability release. First version where a fresh clone on a clean Arch / Debian box can be driven to a working install without hand-holding.

### Added
- **`INSTALL.md`** — full fresh-install walkthrough: prereqs, AUR helper setup, credential acquisition (Mumble / Broadcastify / Telegram / GDrive), pre-flight checklist, expected first-start output, runtime-state map, troubleshooting, manual uninstall, companion-repo pointers (TH9800_CAT, kv4p-ht-python, D75-CAT-Control).
- **`requirements.txt`** covering the full Python dep set so `pip install -r requirements.txt` works alongside (or instead of) `scripts/install.sh`.
- **`scripts/install.sh` post-install health check** — surfaces the usual "why won't it start?" problems immediately: snd-aloop loaded, audio group membership, USB device detection (`/dev/kv4p`, `/dev/relay_*`, `/dev/gps`), config placeholders still in place, runtime-binary availability, Python import smoke test.
- **`scripts/install.sh` AUR-helper fail-fast** — detects `yay`/`paru` up front on Arch; prompts before continuing so users know which optional features will be skipped.
- **Annotated `examples/gateway_config.txt`** — "Fresh install minimum" block at the top lists REQUIRED / RECOMMENDED / HARDWARE fields and points at INSTALL.md for per-credential instructions.
- **Loop playback: source-owned clock + meter**, independent of routing. `LoopPlaybackSource` advances position and updates the meter in its reader thread now, so clicking play without wiring `loop_playback` to a bus still ticks the clock and shows activity. Wiring mid-play taps the ongoing stream. Reader paces itself at real-time via `time.sleep`; queue drops oldest on full instead of stalling ffmpeg.
- **Per-bus Export mode on the recorder page** — new Export button replaces the old "Export:" label. Click-drag selects a time range (populates start/end fields) and the cursor becomes a crosshair. Right-click drag still selects anywhere. Bare click in export mode clears rather than plays.
- **Explicit Play / Stop buttons** for loop playback, separate from the Playback mode toggle. Stop no longer exits mode; Play resumes from the last server position.
- **`tunnel_link_url` + `voice_view` MCP tools** — expose the cloudflared tunnel URL (with derived wss:// link target) and the live `claude-voice` tmux pane.
- **Routing page mouse-wheel zoom** on the drawflow canvas; respects Drawflow's `zoom_min`/`zoom_max`.
- **NUL sink** — drop-only bus destination; lets a bus exist (recording, routing anchor) without forwarding audio anywhere.

### Changed
- **CLAUDE.md no longer mandates `/home/user/Downloads/radio-gateway` as the clone path.** Memory sync snippet now derives the auto-memory path from `$(pwd)` so any clone location works.
- **`.mcp.json` uses repo-relative paths** (`cwd: "."`, `args: ["./gateway_mcp.py"]`). No hand-edit required on a new machine.
- **Audio level bars redesigned** across shell / dashboard / routing pages: 18 px track with inset shadow + 70/95 % zone ticks, 8 px centered fill with glow, asymmetric CSS transition (80 ms rise / 250 ms fall) driven by JS exponential smoothing (instant attack / 0.15 decay) for a VU-meter feel.
- **Dashboard layout** — PWRB and the Net/TCP/temps blocks no longer force row breaks; short items flow into the same auto-fill grid.
- **Routing page level-meter bars** ported the shell/dashboard VU-meter aesthetic in miniature (8 px track / 4 px fill).
- **Routing page: selected flowing connections** recolor to the accent stroke like selected inactive ones while keeping the dashed animation, so selection is visible on active lines.
- **TX / RX mute independence** — `kv4p_plugin` / `th9800_plugin` gain a separate `tx_muted` flag. Muting a radio's TX sink no longer silences its RX source.
- **Lazy MP3 encoder** — WS streaming encoder starts on first subscriber and stops when the last leaves, rather than running flat-out from gateway startup.
- **Richer CPU metrics** in `/sysinfo` — split into `cpu_critical_pct` (us+sy+hi+si, real-time pressure), `cpu_background_pct` (nice), `cpu_iowait_pct`, and `load_per_core`.
- **Denoise inference moved off the bus tick** — per-bus neural inference (RNNoise / DFN3) now runs on its own thread so a slow tick doesn't stall audio.
- **SDR: 2 s libusb grace period** between `killall -9 sdrplay_apiService` and `systemctl start sdrplay.service` so the next start doesn't re-claim still-pending USB handles and SEGV.

### Fixed
- **Broadcastify auto-reconnect latched off** after `_connect()` raised — `_reconnecting` stayed True forever. Now wrapped in `try/finally`; a DNS blip or Icecast refusal no longer kills reconnection indefinitely.
- **Routing page TX sink level leak** — client mirrored source RX level onto `<source>_tx` sink bars, poisoning the smoothed history with RX audio. TH9800 TX on a different bus than TH9800 RX now shows only what's actually flowing to the TX, not what RX is hearing.
- **Transcribe bus-id tag** on source → solo → sink routings now correctly identifies the upstream source.
- **`.gitignore`** covers `tools/*_trace.txt` so runtime diagnostic outputs don't keep reappearing as untracked.
- **`tools/kv4p_raw_capture.py`** output paths are now `__file__`-derived instead of hardcoded to `/home/user/Downloads/...`.

### Removed
- **`recording_playback` MCP tool** — was a "not yet implemented" stub with no route behind it.
- **Client-side RX → TX bar mirror** on the routing page (replaced by the server's explicit TX sink levels).

## [3.3.0] -- 2026-04-19

### Added
- **DeepFilterNet 3 denoise engine** — second neural denoiser alongside RNNoise, selectable per bus.
  - `_DFN3Stream` in `audio_util.py`: stateful streaming ONNX (16 MB) via existing onnxruntime. No new Python deps, no numpy conflict. ~40 dB cut on white noise. Model vendored in-repo at `tools/models/dfn3/denoiser_model.onnx` — no runtime download.
  - Engine abstraction: `DenoiseStream` duck-type, `make_denoise_stream(engine)` factory, shared `_mix_with_dry_delay()` helper. `_RNNoiseStream` conforms to the same interface.
  - Per-bus engine selector: routing-page pill (`RNN` / `DFN`) next to the mix slider, click to swap live. Hidden when denoise is off. Per-bus `dfn_atten_db` input (default 18 dB) to cap neural-gate pumping.
  - **Phase-aligned wet/dry mix** with engine-specific dry-path delay (RNN 960 samples / DFN3 1440 samples). Killed the chorus/reverb smear the naive add produced at any mix < 1.0.
  - HTTP: `set_dfn_engine`, `set_dfn_atten`, `set_dfn_mix` handlers. MCP: `bus_set_denoise_engine`, `bus_set_denoise_atten` tools.
  - ONNX session warmup (80 frames, ~350 ms) runs synchronously at startup — eliminates the cold-start bus-tick spikes that caused "first-few-minutes-of-stutters".
  - ORT `intra_op_num_threads = 2` (optimum per benchmark; 3+ regresses on DFN's sequential GRU graph).
- **Per-stream transcription feed workers** — each bus wired to the transcription sink gets its own worker thread (`TranscribeFeed-<bus_id>`). Two buses' audio no longer serialises through one worker. Combined with the D7 refactor, transcription feed-worker load dropped from 25.6 ms mean / 1185 ms max → **1.9 ms / 9 ms**.
- **Moonshine repetition-suppressed decoder** — custom greedy decoder wraps `MoonshineOnnxModel.generate()` with no-repeat-3-gram logit masking + low-diversity early exit. Eliminates the "Anno, Anno, Anno, Anno, …" loops that upstream's pure argmax produced on ambiguous audio.
- **Multi-radio TX on a single solo bus** — `SoloBus.add_extra_tx_radio()` + fan-out in `_fire_ptt` and Phase 3 audio push. Announce → grunge → ftm_tx + aioc_tx now keys both radios simultaneously. Caveat: slight lead/lag possible if the two radios have very different TX settle times.
- **Dominant-source attribution for transcripts** — when multiple sources feed one bus (e.g. SDR1 + SDR2 on the same listen bus), each utterance is now tagged with the actual upstream tuner's frequency rather than the bus id. Tracks per-frame RMS at mix time, picks the mode across the VAD window.
- **Shared `apply_gain()` with tanh soft-clip** — hoisted to `audio_util.py`. All five routing-path gain sites (listen ducker/duckee, solo TX mix, solo RX boost, per-sink gain) now route through it. Gain > 100% saturates smoothly instead of flat-topping into square-wave harmonics.
- **File playback peak-normalisation on decode** — `FilePlaybackSource` now brings quiet files up to −1 dBFS before the gain slider path. Solves "announcements too quiet" complaint without boosting noise.
- **Telemetry** — `transcription_status` exposes per-stream VAD state, per-queue depth, `proc_mean_ms / max_ms`, `worker_count`. Feed-health readout on the transcribe page surfaces it live.
- **Design pass** — phosphor/instrument-panel aesthetic across all pages:
  - Radial vignette + 3% fractal-noise grain overlay
  - 44 px tall level-meter strip in shell bar with inset channels + 70/95% zone ticks + per-channel glow
  - Identity plate: beacon LED (green/warn/dead), callsign, display-font clock
  - Dashboard 2-column layout ≥1400px
  - `.empty-live` scanning sweep + breathing glyph
  - Routing: widened bus nodes (230 → 290 px) + tightened padding; colour-coded sockets (green=source, cyan=bus, red=sink); `.flowing` animated signal flow on active connections
  - SkyAware (ADS-B) iframe styled to match (grey nav + buttons instead of PiAware blue)
  - Logs: Danger dropdown removed; Restart Gateway + Reboot Host buttons inline
  - Transcribe: fixed-width Audio / Speech meters; status-line 110 px pin to stop jitter

### Fixed
- **Chorus / volume pumping on denoise** — see "Phase-aligned wet/dry mix" above. Measured delays: RNN 960, DFN3 1440.
- **NameError in bus_manager transcription dispatch** — referenced `bus` where only `bus_id` was in scope. Silently caught by try/except, so feed() was never called. Transcription appeared dead.
- **Dual-tuner SDR2 not capturing when wired to a solo bus** — `sync_listen_bus` only counted listen-bus connections, so tuner2 got stopped as "not routed". Now splits into `tuner_needed` (any bus → keeps tuner alive) vs `should_be_on` (listen bus only → add to listen-bus mix).
- **Removed Recording sink stub** — was a v1 leftover that never got a v2.0 implementation (`pass` in the dispatcher, level hardcoded to 0). Loop Recorder's per-bus R button is the actual mechanism. One-time migration strips dangling `bus → recording` connections on load.
- **WCAG AA contrast** — `--t-text-mute` raised from `#4d5a68` (2.7:1) to `#6b7a8a` (4.5:1).
- **Concurrency** — feed-stats lock, non-blocking `_update_lock` in link_endpoint, GIL-safe deque docs.
- **Resource leaks** — ONNX session + per-stream denoise state released in `transcriber.stop()`.

### Removed
- **ASR-path denoise duplicate** — D7 refactor collapsed the two denoise paths into one. Transcription sink inherits whatever the bus already processed. One knob (per-bus D + engine + mix + cap). No more double-denoise footgun; ~200 lines of duplicate code gone.
- **Recording sink node + handlers** — see "Removed Recording sink stub" above.

## [3.2.0] -- 2026-04-19

### Added
- **Moonshine ASR** — replaced Whisper with Moonshine ONNX (`useful-moonshine-onnx`). English-only, CPU-efficient. Real-time on Haswell i5 at base model. `StreamingTranscriber` removed; single utterance-close path.
- **Silero VAD** — replaced dBFS envelope follower with Silero v5 ML speech classifier. Probability threshold (0.0–1.0, default 0.5) with hysteresis (exit = threshold − 0.15). Ignores squelch tails, DTMF, pilot tones, carrier noise. Smoothed probability bar for UI polling (fast-attack 0.5, slow-decay 0.05).
- **RNNoise neural denoise** — per-bus "D" toggle button in routing page with wet/dry mix slider. Shared singleton via pyrnnoise ctypes binding; per-bus stream state. Also available on ASR path via transcribe controls. Soft-clip (tanh) on audio boost path to prevent Silero detection regression.
- **Anti-aliased ASR resampling** — `scipy.signal.resample_poly(audio_48k, 1, 3)` replaces bare `audio_48k[::3]` decimation.
- **Hallucination blocklist** — post-transcription filter drops common no-speech outputs.
- **30-second utterance cap** — hard buffer limit independent of `TRANSCRIBE_MAX_BUFFER`; prevents OOM on stuck-open VAD.
- **Transcript source + frequency** — each entry shows radio name and tuned frequency (e.g. `SDR1 · 446.760 MHz`). TH-9800 reads left VFO from `cat_client._vfo_text['LEFT']`.
- **SDR single-tuner multi-channel mode** — RSPduo one tuner at configurable sample rate (up to 10.66 MHz BW) with up to 2 demodulated channels. Band overview visualisation. Auto-center. 57% CPU reduction vs dual-tuner at equivalent channel count.
- **SDR1/SDR2 as independent routing nodes** — each tuner channel independently routable to any bus.
- **Google Drive integration** — Cloudflare tunnel URL published to Drive as `tunnel_url.json`. Drive file list, storage stats, and publish button on `/gdrive`.
- **Packet auto-discovery** — Gateway Link AIOC endpoint discovered via mDNS. Internal AGWPE proxy eliminates per-endpoint Pat configuration.
- **Gateway Link** — endpoint self-update; internet WebSocket transport with auto-upgrade to LAN TCP; Pi Zero 2W support; jitter buffer; async TX sends.
- **Broadcastify health monitoring** — byte-rate and RTT tracking with alerts.
- **Bus rename** — double-click bus name on routing page for inline editing.
- **Gain slider reset** — double-click any gain slider to reset to 100%.
- **UI redesign** — phosphor/instrument-panel theme across all 20 pages. JetBrains Mono throughout, cyan reserved for live signals, green/amber/red signal vocabulary. See commit history `ui-redesign` series.

### Fixed
- **Routing: selected node background** — overrides Drawflow's bundled `background:red`.
- **Packet AGWPE session cap** — `_AGWPE_MAX_SESSIONS = 10` prevents unbounded sessions.
- **Loop recorder toggle-off** — `stop(bus_id)` called immediately; disabled buses filtered from API.
- **Link endpoint noise gate** — default threshold raised −48 → −40 dB; settings persist.
- **PCM WebSocket stutter** — audio pushed from bus tick thread; duplicate main-loop push removed.
- **Stuck PTT** — level threshold, bus reload cleanup, 60s safety timeout.

### Removed
- **Whisper / faster-whisper / ctranslate2** — fully replaced by Moonshine.
- **Streaming transcription mode** — `StreamingTranscriber` and `mode` config field removed.
- **Legacy D75 plugin** — `d75_plugin.py` deleted; D75 is link-endpoint-only.

## [3.1.0] -- 2026-04-09

### Added
- **SDR single-tuner mode** — RSPduo runs one tuner with multi-channel demodulation
  - Mode selector on `/sdr` page and `sdr_set_mode` MCP tool
  - Configurable sample rate (0.25–10.66 MHz) and center frequency
  - Bandwidth visualization showing channel positions within tunable band
  - Per-channel audio level bars in channel editor
  - Auto-center button calculates optimal center freq and sample rate
  - Max 2 channels with independent PipeWire sinks for per-channel routing
  - SDR CPU reduced 57% (31% → 13%) at 1 MHz sample rate
  - Mode, channels, and device settings persist across restarts
  - Closed-loop controls: every action verifies and reports outcome
  - Full stream trace instrumentation (overflow, underrun, slow drain, timing)
- **SDR1/SDR2 as separate routing nodes** — each tuner channel independently routable
  to any bus (no more internal-only ducking)
- **Bus rename** — double-click bus name on routing page for inline editing
- **Gain slider reset** — double-click any gain slider to reset to 100%
- **Alphabetical bus sort** in routing auto-arrange
- **`audio_util.py`** — shared level metering module (`pcm_level`, `pcm_db`, `pcm_rms`,
  `rms_to_level`, `update_level`), AudioProcessor, and CW generation extracted from
  audio_sources.py; used by all plugins
- **`_resolve_source()`** in web_routes_post.py — unified plugin + link endpoint
  attribute lookup for duck/boost/mute
- **Web UI shared code** — `common.js` expanded with `postJson`, `getJson`,
  `createPoller`, `sendKey`, `openTmux`, formatting helpers; `common.css` expanded
  with status colors, layout grid, level bars
- **Bus display names** shown in loop recorder dashboard and recorder page

### Fixed
- **Loop recorder toggle-off** — `stop(bus_id)` called immediately when loop flag
  toggled off; disabled buses filtered from API
- **Link endpoint noise gate** — default threshold raised -48 → -40 dB (AIOC noise
  floor was passing through); gate settings now persist in endpoint `settings.json`
- **LinkAudioSource TX metering** — `put_audio()` now updates `tx_audio_level` so
  TX nodes show activity on routing page (affects all link endpoints)
- **common.js load order** — fixed controls.html and recordings.html where common.js
  loaded after inline scripts that depend on it
- **Duplicate kv4p_plugin init** and no-op self-assignment in reconnect handler
- **Controls page responsive layout** — fixed-width tiles replaced with flex:1 tiles;
  inline container styles moved to CSS classes

### Removed
- **Legacy D75 plugin** — `d75_plugin.py` (730 lines) deleted; all d75_plugin
  references removed from 11 files (~1,136 lines total). D75 is now link-endpoint-only.
- **Duplicate level metering** — ~55 inline RMS→dB→level→decay patterns replaced
  with `audio_util` calls across 10 files (-282 lines)

## [3.0.0] -- 2026-04-07

### Architecture
- **Listen bus unified into BusManager** — single code path for all bus types
  - Primary listen bus moved from gateway_core main loop into BusManager
  - All buses (listen, solo, duplex, simplex) share one tick loop and delivery path
  - Main loop simplified to SDR rebroadcast TX and WebSocket push
  - Net reduction of ~500 lines from gateway_core.py

### Added
- **Loop Recorder** — per-bus continuous recording with visual waveform review
  - Enable with "R" button per bus in routing UI
  - Segmented MP3 storage (5-min chunks) with configurable retention (1h to 7d)
  - Canvas-based waveform viewer with zoom, pan, click-to-play
  - Right-click drag to select time range for export (MP3 or WAV)
  - Stacked multi-bus view with independent playback per bus
  - Dashboard panel with per-bus stats (segments, disk usage, write rate)
  - Real-time waveform from active segments (no delay for segment close)
  - HTTP Range support for native browser seeking
  - See [docs/loop-recorder.md](docs/loop-recorder.md) for full guide
- **Plugin auto-discovery** — drop a .py file in `plugins/`, add config flag, restart
  - No gateway code changes needed to add a new radio
  - Template at `plugins/example_radio.py` with detailed comments
  - Developer guide at [docs/plugin-development.md](docs/plugin-development.md)

### Fixed
- Status API: darkice_pid, darkice_restarts, stream_restarts were hardcoded
- TH9800: audio_level computed after processing (noise gate now squelches level bar)
- Shell nav bar: fixed-width buttons (no layout shift when streaming)

### Changed
- Shell nav bar: stream timer shown inside button, indicator dots removed
- Default volume sliders at 50% (was 100%)

## [2.0.0] -- 2026-03-31

### Architecture
- Bus-based audio routing replacing monolithic AudioMixer
  - 4 bus types: Listen, Solo, Duplex Repeater, Simplex Repeater
  - Per-bus audio processing, ducking, and stream controls
  - Bus mute, sink mute, source mute with visual feedback
- All radios refactored as plugins: SDRPlugin, TH9800Plugin, D75Plugin, KV4PPlugin
  - Standard `get_audio()`/`put_audio()` interface for bus routing
  - Hardware-specific methods for UI controls
  - Per-plugin processing chains (gate/HPF/LPF/notch/gain)
- All sinks gated by routing connections (no implicit audio flow)
- Visual routing UI with Drawflow node editor (sources | busses | sinks)
  - Live level bars in source/sink nodes
  - Mute buttons and gain sliders in nodes
  - Save/load routing configurations

### Added
- Full duplex Remote Audio (Windows client on ports 9600/9602)
- Direct Icecast streaming (replaced DarkIce/FFmpeg/ALSA loopback pipeline)
- Mumble as routable source and sink (MumbleSource with PTT control)
- Room Monitor as routable source with VAD
- Web Mic in nav bar (accessible from all pages)
- Speaker virtual mode (prevents PipeWire feedback loops)
- 14 new MCP tools for routing and automation
- BusManager: runs routing-configured busses alongside main loop

### Removed
- Console/terminal UI: StatusBar, keyboard handler, ANSI display (~650 lines)
- Old AudioMixer and AIOCRadioSource (~900 lines)
- 13 `_generate_*` web methods (~5400 lines from web_server.py)
- Dead PTT code and old AIOC audio paths
- Diagnostic trace prints
- Backward compatibility aliases (d75_cat, d75_audio_source, kv4p_cat, kv4p_audio_source)

### Changed
- Web pages extracted to static HTML (13 pages in web_pages/)
- Controls page streamlined
- 13 static page routes consolidated to single `_STATIC_PAGES` lookup
- Utility classes extracted to gateway_utils.py (DDNSUpdater, EmailNotifier, CloudflareTunnel)
- TH-9800 AIOC init replaced with TH9800Plugin
- SDR init simplified (~80 lines to ~15 lines via SDRPlugin)
- Main loop 8-tuple replaced with BusOutput consumption
- Blocking audio reader replaces PortAudio callback

## [1.7.0] -- 2026-03-27

### Added
- Gateway Link: duplex audio + command protocol with plugin architecture (`gateway_link.py`)
  - Framed TCP protocol: `[type 1B][length 2B][payload]` -- 5 frame types (AUDIO/COMMAND/STATUS/REGISTER/ACK)
  - `RadioPlugin` base class for hardware abstraction (setup/teardown/get_audio/put_audio/execute/get_status)
  - `AudioPlugin`: generic sound card via PyAudio (any ALSA/PipeWire device)
  - `AIOCPlugin`: finds AIOC device via `/proc/asound/cards` (not PyAudio)
  - `tools/link_endpoint.py`: standalone endpoint script with plugin registry, gain control, status reporter
  - `LinkAudioSource`: mixer integration with level metering, audio boost, duck support
  - Config: `ENABLE_GATEWAY_LINK`, `LINK_PORT`, `LINK_AUDIO_PRIORITY`, `LINK_AUDIO_DUCK`, `LINK_AUDIO_BOOST`, `LINK_AUDIO_DISPLAY_GAIN`
- Multi-endpoint support: N simultaneous connections, dict keyed by endpoint name
  - Dynamic `LinkAudioSource` creation/destruction per endpoint
  - Per-endpoint controls on `/controls` page (PTT button, RX/TX level bars, gain sliders, mute buttons)
  - Per-endpoint settings persisted to `~/.config/radio-gateway/link_endpoints.json`
  - RX/TX gain controls in dB (-10 to +10), persisted per endpoint
  - RX/TX mute (gateway-side, per-endpoint)
  - VAD-gated level bars
- Command language: `ptt`, `rx_gain`, `tx_gain`, `status` + ACK responses
- PTT safety timeout (60s auto-unkey)
- Bidirectional heartbeat (5s interval) with dead peer detection (15s)
- 10s socket timeout on both sides for cable-pull detection
- mDNS auto-discovery: gateway publishes `_radiogateway._tcp`, endpoint discovers via `avahi-browse`
- Zero-config endpoint usage: `python3 link_endpoint.py --name pi-aioc --plugin aioc`
- `docs/gateway_link.md`: comprehensive architecture and protocol documentation
- LINK audio bar (orange) on dashboard

### Fixed
- Client deadlock: `_send` calling `_close` while holding lock
- Reader cleanup: only calls `on_disconnect` if it owns the entry
- `/linkcmd` missing `return` caused config wipes on POST
- Config page `_CONFIG_LAYOUT` must include all sections or Save wipes unlisted ones

## [1.6.0] -- 2026-03-26

### Added
- D75 BT TX audio via SCO (48-byte frame splitting + paced TX thread at 3ms interval)
- D75 memory channel load via FO (ME-to-FO field mapping with lockout field skip)
- Room Monitor: browser page (`/monitor`) + Android APK (`tools/room-monitor-app/`)
  - `WebMonitorSource`: no PTT, priority 5, `/ws_monitor` WebSocket endpoint
  - Browser: getUserMedia with processing disabled, gain 1x-50x, client-side VAD
  - Wake Lock API + silent audio loop to prevent tab suspension
  - Android Kotlin app with foreground service, UNPROCESSED mic, partial wake lock
  - `/monitor-apk` route serves APK download
- SDR click suppressor (>800 sample jump interpolation, per-source + output)
- Broadcast-style additive audio mixing with soft tanh limiter (knee 24000, max 32767)
- Cloudflare tunnel URL displayed in System Status and startup email
- Broadcastify status panel on dashboard (uptime/sent/rate/RTT/health/PID)
- `/controls` page (control groups moved from dashboard)
- Audio level bars in shell frame (always visible across all pages)
- ADS-B dark mode with NEXRAD weather overlay, centered Santa Ana CA, US mil layers
- Telegram status checks bot process regardless of `ENABLE_TELEGRAM` flag
- D75 proxy: battery level, TNC status, beacon type status reporting
- 15s btstart retry loop for D75 BT reconnection
- TX Talkback config (`TX_TALKBACK` in `[ptt]` section, default off)

### Changed
- Web UI restructure: compact 0.8em nav, no page titles, no footer, inline MP3/PCM controls
- Audio bars reordered: RX, TX, KV4P, D75, SDR1, SDR2, SV, AN, SP, MON, LINK
- D75 PTT: fire-and-forget `sendall()` (no audio thread blocking via `_send_cmd`)
- D75 proxy SM poll: 0.5s to 3s with exponential backoff (up to 30s after failures)
- D75 proxy init: deferred FO/SM/PC queries (skip on fresh BT connect)
- KV4P logging gated behind `VERBOSE_LOGGING`
- Email linkifier supports `wss://` and `ws://` URLs
- README rewritten: 2822 to 1073 lines with collapsible detail sections
- Code defaults updated to match production config (ENABLE_ADSB, ENABLE_DDNS, etc.)

### Fixed
- D75 `connected` status showed TCP as radio-connected (now requires `serial_connected`)
- D75 `_recv_line` EOF did not set `_connected=False` (poll thread never reconnected)
- D75 `close()` killed poll thread reconnect loop (added `_disconnect_for_reconnect`)
- D75 reconnect handler: missing `D75CATClient` import in `web_server.py`
- D75 ME-to-FO lockout field shift (ME[14] not present in FO 21-field format)
- D75 ME field[2] dual meaning (offset vs TX freq for cross-band repeater)
- D75 TX audio silent (SCO SEQPACKET requires 48-byte frames, not arbitrary sizes)
- D75 TX stutter (burst delivery replaced by paced TX thread)
- D75 playback JS newline syntax error
- MON bar: float percentage values and stuck level after WebSocket disconnect
- ADS-B map broken by stray `false` in `layers.js` europe.push calls
- Config file damage from `Edit` tool `replace_all` on multi-line values
- `btstart` non-blocking: proxy returns immediately, BT connects in background
- `btstart` button shown during auto-connect (added `_btstart_in_progress` flag)

## [1.5.0] -- 2026-03-25

### Added
- MCP server (`gateway_mcp.py`): 31 stdio tools for AI control of the gateway
  - 20 core tools: gateway_status, sdr_status/tune/restart/stop, cat_status, radio_ptt/tts/cw/ai_announce/set_tx/get_tx, recordings_list/delete, gateway_logs/key, automation_trigger, audio_trace_toggle, telegram_reply, system_info
  - 11 additional tools: radio_frequency, d75_status/command/frequency, kv4p_status/command, mixer_control, recording_playback (stub), config_read, telegram_status, process_control
- `/mixer` HTTP endpoint: dedicated mixer control (7 actions: status/mute/unmute/toggle/volume/duck/boost/flag/processing)
- D75/KV4P per-source audio processing buttons (Gate/HPF/LPF/Notch) with live highlighting
- HEADLESS_MODE in start.sh (skips Mumble GUI launch)

### Changed
- Mixer sources list expanded: global, tx, rx, sdr1, sdr2, d75, kv4p, remote, announce, speaker

## [1.4.0] -- 2026-03-24

### Added
- TH-D75 Bluetooth radio integration (`D75CATClient` in `cat_client.py`)
  - Remote BT proxy (`scripts/remote_bt_proxy.py`) on ports 9750 (CAT) / 9751 (audio)
  - FO command support (21-field format, LA3QMA/Hamlib spec)
  - Channel load via `d75GoChannel` with band switching
- Telegram bot (`tools/telegram_bot.py`): phone control via text and voice
  - Voice notes: ffmpeg-to-PCM at real-time rate via ANNIN port 9601
  - Text messages injected into Claude tmux session for MCP processing
- Smart Announcements rewritten: single `claude -p` backend (replaced 5 old backends)
  - `smart_announce.py`: 300 lines (down from 1300), no external dependencies
- D75 starts muted by default (prevents SDR ducking from idle noise)

### Changed
- SDR_SIGNAL_THRESHOLD raised from -70 to -45 dBFS (D75 noise at -65 was permanently ducking SDRs)

### Fixed
- ANNIN level bar stuck at last value after voice note (reset to 0 when queue drains)
- D75 btstart blocking caused protocol desync (made non-blocking with background thread)
- D75 `_do_btstart` skipped `serial.connect()` step
- D75 poll thread self-join crash in `close()` (thread identity check added)
- D75 tone/shift/offset wrong FO indices (4 layered bugs: 11-field vs 21-field, wrong positions, gateway timeout crash)

## [1.3.0] -- 2026-03-23

### Added
- SDR post-duck audio handling improvements
- Re-duck inhibit timer (REDUCK_INHIBIT_TIME = 2.0s)

### Fixed
- SDR post-duck stutter: removed aioc_ducks_sdrs gate, added re-duck inhibit + fade-in reset
- MCP sdr_tune wrong payload keys

## [1.2.0] -- 2026-03-21

### Added
- ADS-B aircraft tracking (dump1090-fa + lighttpd + FlightRadar24 feed)
- Gateway reverse proxy for ADS-B (`/adsb/*`)
- TH-9800 auto serial connect on startup

### Fixed
- TH-9800 PTT blind toggle state inversion (switched to explicit `!ptt on`/`!ptt off`)

## [1.1.0] -- 2026-03-19

### Added
- KV4P HT radio support (`KV4PAudioSource`, CP2102 USB-serial, Opus codec)

### Fixed
- KV4P TX 20% audio dropout
- KV4P CTCSS off-by-one (DRA818 uses 38 tones, not TH-9800's 39-tone list)

## [1.0.0] -- 2026-03-13

### Fixed
- DISPLAY_TEXT VFO misattribution (vfo_byte from packet, not stale `_channel_vfo`)
- RTS change corrupts display text
