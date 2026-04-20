# Changelog

All notable changes to Radio Gateway.

## [Unreleased]

### Added
- **DeepFilterNet 3 denoise engine** — second neural denoiser option alongside RNNoise, selectable per bus and on the ASR path.
  - New `_DFN3Stream` in `audio_util.py`: streaming ONNX (16 MB, DFN3 stateful export), runs via existing onnxruntime, no new Python deps. ~40 dB cut on white noise, RTF ≈ 0.4 on Haswell i5 smoke test.
  - Model lazy-downloaded on first enable into `~/.cache/radio-gateway/dfn3/`, SHA-256-pinned.
  - Engine abstraction: `make_denoise_stream(engine)` factory, `DENOISE_ENGINE_IDS` tuple. `_RNNoiseStream` conforms to the same duck-type.
  - Routing page: small `RNN` / `DFN` pill next to the per-bus mix slider, click to swap engine (live — next audio frame rebuilds the stream).
  - Transcribe page: Engine `<select>` below the denoise checkbox; checkbox label updates to match.
  - New HTTP cmd `set_dfn_engine`, MCP tool `bus_set_denoise_engine`, `transcription_config denoise_engine` key.
  - Per-engine timing telemetry: `denoise_engine_mean_ms` + `denoise_engine_calls` in `feed` block of `transcription_status`. Shown on transcribe page feed-health row as `RNN=…ms×N  DFN=…ms×N`.
  - Back-compat: existing `routing_config.json` / `.transcribe_settings.json` without `dfn_engine` key default to `rnnoise` → zero behaviour change for current users.
- **Design pass** — phosphor/instrument-panel aesthetic applied across all pages via `common.css`:
  - Radial vignette + 3% fractal-noise grain overlay on body
  - Elevated level-meter strip in shell bar: 44px tall, inset recessed channels, 70%/95% zone ticks, per-channel coloured glow
  - Identity plate redesigned: beacon LED (green pulse / warn / dead), callsign from `PACKET_CALLSIGN`, display-font stack for clock + call
  - `--font-display` CSS variable with fallback chain; phosphor text-shadow on hero numerics
  - `.hero` block: gradient + top-edge accent line + drop shadow; one hero per page enforced
  - Dashboard 2-column layout at ≥1400px
  - `.empty-live` scanning sweep + breathing glyph; transcribe + recordings updated
  - `.rb-caps` button modifier added to `common.css`; shell-pcm buttons migrated
  - Animated `.flowing` dashed stroke on routing connections when source level > 3
  - Routing node title stripes widened (3px → 6px) with hue-tinted gradient; sockets colour-coded by node role
  - `.dim`, `.mute`, `.dim-op` utility classes hoisted to `common.css`

### Fixed
- **WCAG AA contrast** — `--t-text-mute` raised from `#4d5a68` (2.7:1) to `#6b7a8a` (4.5:1)
- **Concurrency** — `_denoise_lock` in transcriber; non-blocking `_update_lock` in link_endpoint; GIL-safe deque doc comments
- **Resource leaks** — ONNX session + RNNoise stream released in `transcriber.stop()`

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
