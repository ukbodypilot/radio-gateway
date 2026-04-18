# Bug History — Radio Gateway

## Multi-bus PCM/MP3 routing interleaved instead of mixed (2026-04-18)
**Symptom:** Routing two buses (e.g. main + th9800) to the PCM/MP3 sink sounded like the streams were chopped together — chunks from each bus played sequentially, not mixed.
**Root cause:** `_deliver_audio()` called `self._pcm_queue.append(mixed)` and `push_ws_audio(mixed)` independently for each bus during its own tick. WS client received `busA_chunk, busB_chunk, busA_chunk, busB_chunk…` at 25ms intervals — playback was temporal interleave.
**Fix:** Staged per-tick contributions into `self._pcm_tick` / `self._mp3_tick` lists during `_deliver_audio`. After all buses deliver each tick, mix staged chunks through `mix_audio_streams` (additive + soft-tanh limiter) and push ONE mixed chunk per tick. Single-bus fast path preserved.
**Files:** `bus_manager.py`
**Trace proof:** dual-bus ticks (both buses contributing in same 50ms slot) = 195; all mixed via the new flush path.

## SDR1 parec jitter caused choppy audio (2026-04-18)
**Symptom:** SDR1 audio choppy/stuttery even when signal was clean. parec_read stdev=16ms, max=88ms, 110/637 events >80ms.
**Root cause:** rtl_airband runs with `continuous = false` (deliberate — see 2026-04-05 noise-floor bug). When squelch is closed, rtl_airband stops writing to its PulseAudio null sink, so parec reads stall and then burst. The bus consumer pulls at steady 50ms but the upstream queue drains/refills irregularly, so some ticks see an empty queue and the bus sees a gap.
**Fix:** `_TunerCapture.get_chunk()` now zero-fills on underrun — returns a mono silence buffer instead of `None`, keeping bus cadence continuous. Zero samples → RMS=0 → level meter stays at 0, so the 2026-04-05 noise-floor regression does NOT reappear (zero-fill is gateway-internal, not rtl_airband output).
**Files:** `sdr_plugin.py` (_TunerCapture.get_chunk)
**Trace proof:** `get_chunk intervals: stdev=1.8ms min=48.3ms` vs upstream `parec_read stdev=16.1ms` — buffer fully absorbs the jitter.

## Celeron-FTM150 endpoint flooding gateway with silence (2026-04-18)
**Symptom:** `celeron-ftm150_rx push_audio: 1121 events  SILENT=1121  qd: mean=16.0 max=16  flags={'overflow': 1121}` — endpoint pushed silent zero-chunks to the gateway every 50ms even when radio was idle, saturating the RX queue at max depth with overflow on every push.
**Root cause:** `AudioPlugin._apply_gate()` returned `b'\x00' * len(data)` when the gate was closed. The reader loop then queued that silence into `_rx_queue`, which the link protocol transmitted to the gateway continuously regardless of signal.
**Fix:** Added `continue` in the reader loop after gate-close detection — drop the chunk entirely instead of queueing silence. `get_audio()` already handles empty-queue as "no audio this tick" so this is safe.
**Files:** `gateway_link.py` (AudioPlugin reader loop ~line 1443). Deployed to celeron-ftm150 endpoint separately.
**Trace proof:** after deploy, `push_audio` dropped to 307 events in 232s (only during real TX), RMS mean=231 (non-silent), no overflow. RX `get_audio UNDERRUN=94%` is the correct idle behavior.

## Bus processing per-sink IIR filter corruption (2026-04-05)
**Symptom:** Audio processing buttons (G/H/L/N) on routing page had no audible effect or corrupted audio.
**Root cause:** `_deliver_audio()` called `AudioProcessor.process()` once per sink inside the loop. IIR filters are stateful — running N times per tick advanced filter state N times, corrupting output for all but the first sink.
**Fix:** Process mixed audio ONCE before the sink loop, replace all sink audio entries with the processed result.
**File:** `bus_manager.py` (_deliver_audio)

## Listen bus processing buttons non-functional (2026-04-05)
**Symptom:** G/H/L/N buttons on the primary listen bus ("Main") toggled visually but had zero audio effect.
**Root cause:** Primary listen bus is skipped by BusManager (handled by gateway_core main loop). The toggle saved to routing_config.json but no processing path existed in the main loop. Also, the toggle handler updated the config dict but never set the actual AudioProcessor's enable flags.
**Fix:** Added `_listen_bus_processor` to gateway_core, applied in the `_early_audio` path before sink delivery. Toggle handler now creates/updates the live AudioProcessor object. Startup loads saved processing state from routing config.
**Files:** `gateway_core.py`, `web_server.py`, `bus_manager.py`

## Passive sink gain sliders non-functional (2026-04-05)
**Symptom:** Gain slider on mumble/broadcastify/speaker/recording nodes did nothing.
**Root cause:** `_get_plugin_by_id('mumble')` returned None — passive sinks have no plugin object. Gain command only worked for plugin-backed sources/sinks.
**Fix:** Added `_sink_gains` dict on gateway, populated by gain command for passive sinks. Applied as numpy multiply in bus_manager delivery path.
**Files:** `web_server.py`, `bus_manager.py`

## Missing AudioProcessor import in web_server.py (2026-04-05)
**Symptom:** Processing toggle buttons stopped responding — no visual feedback, no error shown in UI.
**Root cause:** Toggle handler created `AudioProcessor()` but the import was missing from web_server.py. Error returned `{"ok": false, "error": "name 'AudioProcessor' is not defined"}` but JS only checked `d.ok` so button stayed unchanged.
**Fix:** Added `AudioProcessor` to the import from `audio_sources`.
**File:** `web_server.py`

## Endpoint ALSA device busy on mode switch (2026-04-05)
**Symptom:** Direwolf failed to start when switching to data/winlink mode: "Could not open audio device plughw:3,0 for input — Device or resource busy"
**Root cause:** Mode switch closed PyAudio streams but didn't terminate the PyAudio instance itself. PyAudio held ALSA device handles even after streams were closed. 0.5s delay was also insufficient.
**Fix:** Terminate `self._pa` (PyAudio instance) after closing streams, increase delay to 1.0s before starting Direwolf.
**File:** `gateway_link.py` (AIOCPlugin._set_mode), deployed to Pi endpoint.

## SDR continuous mode polluting routing levels (2026-04-05)
**Symptom:** SDR source and Main bus showed constant level ~71 even when SDRs were idle (no signal). Noise floor visible on all connected sinks.
**Root cause:** rtl_airband `continuous = true` sends receiver noise to PipeWire even when squelch is closed. StreamOutputSource silence keepalive handles Broadcastify independently — continuous mode was unnecessary.
**Fix:** Hardcode `continuous = false` in rtl_airband config generation. Default in `_SETTING_KEYS` also changed to False. Noise gate approach was tried first but abandoned — can't distinguish noise from weak signal in audio domain.
**Files:** `sdr_plugin.py` (_write_config, _write_config_sdr2, _SETTING_KEYS)

## Packet endpoint stuck in data mode on disable (2026-04-05)
**Symptom:** After disabling packet mode, FTM-150 endpoint stayed in data mode with Direwolf running, blocking TX audio.
**Root cause:** `_send_endpoint_mode('audio')` silently failed when no matching endpoint was found. Gateway set mode to idle but endpoint never received the audio command.
**Fix:** `_send_endpoint_mode()` returns True/False. `_set_mode()` reports failures. Added endpoint status (mode, DW process, audio I/O, HID) to packet status API and UI. Added Force Audio button for manual recovery. Mismatch warning banner when gateway idle but endpoint stuck in data.
**Files:** `packet_radio.py`, `web_routes_post.py`, `web_pages/packet.html`

## Endpoint mode switch race — PyAudio reopen crash (2026-04-04)
**Symptom:** Switching AIOC endpoint from audio to data mode crashed the endpoint. PyAudio stream reopened immediately after being closed, conflicting with Direwolf's exclusive ALSA access.
**Root cause:** `AIOCPlugin._set_mode()` closed PyAudio streams before setting `self._mode = 'data'`. The `get_audio()` method saw audio mode + no stream and triggered `reopen_audio()`, which crashed because Direwolf already had exclusive ALSA access.
**Fix:** Set `self._mode = 'data'` BEFORE closing streams, so `get_audio()` returns None instead of trying to reopen. Also close BOTH input AND output PyAudio streams (Direwolf needs exclusive ALSA access to both directions for TX audio).
**File:** `gateway_link.py` (AIOCPlugin._set_mode), also deployed to Pi at `/home/user/link/gateway_link.py`.

## AIOC reader gets silence through PipeWire (2026-04-04)
**Symptom:** `aioc` level permanently 0 despite radio receiving. Stream opened successfully but read DC silence (RMS ~116, all samples negative).
**Root cause:** WirePlumber disables AIOC (`device.disabled=true` in `99-disable-loopback.conf`). PyAudio and sounddevice both use PipeWire's ALSA plugin which returns silence for disabled devices, even with explicit `hw:N,0`. Only raw `arecord` bypasses PipeWire.
**Fix:** Replaced PyAudio reader with `arecord` subprocess. Device discovery via `/proc/asound/cards` instead of PyAudio name search.

## SoloBus PTT blocks BusManager 150-600ms (2026-04-04)
**Symptom:** Audio stutters on file play/stop. BusManager stalls up to 601ms. AIOC RX queue overflows (45-60 per trace). Cross-clock drift spikes to 590ms.
**Root cause:** SoloBus.tick() called `radio.execute({'cmd': 'ptt'})` synchronously. AIOC PTT does CAT `_pause_drain()` + `set_rts()` + HID write = 150-600ms blocking the entire BusManager thread.
**Fix:** `_fire_ptt()` runs PTT in background thread (fire-and-forget). Same pattern as D75 PTT.
**Trace proof:** monitor_bus tick_slow went from 9 events (max 601ms) to 0. BusManager max interval from 601ms to 91ms.

## BusManager clock drift (2026-04-04)
**Symptom:** PCM drain showed occasional multi-chunk events. BusManager systematically slower than main loop.
**Root cause:** `_tick_loop()` used reset timing (`next_tick = monotonic() + interval`) instead of accumulative (`next_tick += interval`). Actual period = interval + processing_time.
**Fix:** Accumulative timing with snap-forward that skips all missed ticks.

## AIOC RX 800ms stale audio latency (2026-04-04)
**Symptom:** Audio from TH-9800 heard in Mumble ~800ms after it happened.
**Root cause:** RX queue `maxsize=16` filled during 130s startup before BusManager began consuming. Queue never drained below 15.
**Fix:** Reduced to `maxsize=3`. Flush stale chunks on first consumer read. Latency now ~250ms.

## Gateway main loop crash: missing early attribute inits (2026-04-03)
**Symptom:** All audio levels 0, gateway silently dead. Main loop crashed on first tick.
**Root cause:** `bus_manager`, `_bus_sinks`, `_bus_stream_flags`, `_listen_bus_id` accessed in main audio loop before `_setup_routing` initialized them. `self.bus_manager` raised `AttributeError` even though `if self.bus_manager` was used — attribute didn't exist at all.
**Fix:** Added early `None`/empty defaults for all 6 attributes in `RadioGateway.__init__`.

## Link endpoint stale PyAudio stream (2026-04-03)
**Symptom:** FTM-150 endpoint connected, responding to commands, audio level permanently 0.
**Root cause:** PyAudio stream went stale. Wrapper script's pkill via SSH failed silently, stale process persisted.
**Fix:** reopen_audio() with full PyAudio terminate+reinit, systemd service replaces wrapper, on_connect callback reopens on gateway reconnect, zero-read watchdog, data mode check prevents reopen during Direwolf use.

## TH-9800 TX/RX gain crosstalk (2026-04-03)
**Symptom:** TH-9800 TX slider on routing page changed RX audio volume.
**Root cause:** `tx_audio_boost` attribute didn't exist on TH9800Plugin. Gain handler fell through to `audio_boost` (RX gain) for both sliders.
**Fix:** Added `tx_audio_boost` attribute, applied in `put_audio()` before AIOC write.

## TH-9800 TX blocking caused RX PCM stutter (2026-04-03)
**Symptom:** PCM stream and TX audio stuttered during file playback through TH-9800.
**Root cause:** `put_audio()` called `stream.write()` synchronously on the bus tick thread. Blocked until ALSA accepted data, stalling the entire tick cycle including RX.
**Fix:** `put_audio` queues to deque, dedicated `_tx_writer_loop` thread does blocking writes independently.

## Config section wiped by web Save (2026-04-03)
**Symptom:** `[packet]` section disappeared from config after using web Save button.
**Root cause:** `_CONFIG_LAYOUT` is the master list — Save only writes keys listed there. Packet section wasn't registered.
**Fix:** Added `[packet]` section with all keys to `_CONFIG_LAYOUT` in web_server.py.

## ADS-B map broken: stray `, false)` in layers.js (2026-03-26)
**Symptom:** ADS-B map page failed to load — JavaScript syntax error in layers.js.
**Root cause:** `europe.push(...)` calls in layers.js had stray `, false)` appended, creating invalid JS syntax.
**Fix:** Removed the stray text from all affected lines in the ADS-B layers.js overlay.

## MON bar float % and stuck level after disconnect (2026-03-26)
**Symptom:** Monitor audio bar showed floating-point percentage (e.g., "12.345%") and level stayed stuck at last value after WebSocket disconnect.
**Root cause:** (1) Level value not rounded to integer before display. (2) No cleanup handler to reset level to 0 when monitor WebSocket closes.
**Fix:** Rounded level to integer. Added disconnect handler that resets monitor level to 0.

## Missing D75CATClient/D75AudioSource imports in web_server.py (2026-03-26)
**Symptom:** D75 reconnect and other handlers crashed with NameError — classes not imported.
**Root cause:** `web_server.py` referenced `D75CATClient` and `D75AudioSource` but never imported them (circular import avoidance pattern).
**Fix:** Added lazy imports (`from cat_client import D75CATClient`) inside the handler functions that need them.

## Config file damage from replace_all Edit (2026-03-26)
**Symptom:** After using Edit tool with `replace_all` to update a config value, multiple unrelated config values were changed to the wrong value.
**Root cause:** `replace_all` replaces ALL occurrences of the old_string in the file. Config values like `False` or numeric values appeared multiple times. Replacing one changed all of them.
**Fix:** Restored config file from backup. Updated code defaults instead of editing config directly where possible.
**Lesson:** Never use `replace_all` on config files with repeated values. Use targeted edits with enough surrounding context to be unique.

## D75 playback JS newline syntax error (2026-03-26)
**Symptom:** D75 page JavaScript crashed on load, recording playback broken.
**Root cause:** Python string `'\n'` was interpolated into JavaScript string literal as a literal newline, creating a syntax error (unterminated string).
**Fix:** Used `\\n` in the Python template to produce `\n` in the JS output.

## D75 PTT blocked audio thread (2026-03-26)
**Symptom:** TX audio stuttered badly during PTT — audio thread stalled for 1-5s each time PTT state changed.
**Root cause:** PTT used `_send_cmd()` which acquires a lock and waits for response. Audio thread called PTT, blocking itself. Also competed with poll thread for response parsing.
**Fix:** PTT now uses fire-and-forget `_sock.sendall()` — no lock, no response wait. Safe because PTT commands have no meaningful response.

## D75 TX audio stutter: burst frame delivery (2026-03-26)
**Symptom:** TX audio played at ~10Hz stutter on the radio — bursts of audio then silence.
**Root cause:** All SCO frames for a chunk were sent in a tight loop with no pacing. BT SCO expects real-time delivery (~3ms per 48-byte frame). Burst delivery caused the radio to play/skip/play/skip.
**Fix:** Dedicated `_tx_loop` thread reads from a buffer and sends one frame every 3ms via `time.sleep(0.003)`.

## D75 TX audio silent: SCO SEQPACKET needs 48-byte frames (2026-03-26)
**Symptom:** TX audio sent to proxy but radio produced no sound. Proxy showed data arriving on port 9751.
**Root cause:** SCO socket is SEQPACKET (not STREAM). Each `send()` creates one SCO packet. Sending 800 bytes in one call created one oversized packet that was silently dropped. SCO requires exactly 48-byte frames.
**Fix:** Split incoming data into 48-byte chunks before writing to SCO socket. Each chunk becomes one valid SCO frame.
**Lesson:** BT SCO is not a stream socket — frame boundaries matter. Always check socket type (SEQPACKET vs STREAM) when sending audio.

## D75 ME field[2] dual meaning: offset vs TX freq (2026-03-26)
**Symptom:** Loading cross-band repeater channels from memory set a bogus ~437MHz offset, making the radio transmit on wrong frequency.
**Root cause:** ME field[2] has dual meaning: small values (<100MHz) are offset in Hz, large values (>=100MHz) are the TX frequency itself. Code passed the TX freq directly as offset.
**Fix:** `if field2 >= 100000000: offset = abs(field2 - rx_freq)` — converts TX freq to offset.

## D75 ME→FO lockout field shift (2026-03-26)
**Symptom:** Channel load from memory produced wrong tone settings. Radio silently accepted bad FO command but tone/CTCSS was wrong.
**Root cause:** ME has 23 fields, FO has 21. ME[14] is lockout (not present in FO). Code sent all ME fields to FO, shifting tone_idx/ctcss_idx/dcs_idx by +1 position.
**Fix:** ME→FO conversion: `fields[1:14] + fields[15:22]` — skip ME[14] lockout and ME[22] name.

## D75 SM poll 0.5s killed BT RFCOMM (2026-03-26)
**Symptom:** D75 BT connection dropped every 30-60 seconds. Proxy log showed RFCOMM errors.
**Root cause:** SM (signal meter) was polled every 0.5s. Combined with other status queries, this overwhelmed the BT serial link. RFCOMM has limited bandwidth and the radio's serial parser couldn't keep up.
**Fix:** SM poll interval raised to 3s. Added exponential backoff after 3 consecutive failures (up to 30s). Init defers heavy queries (FO/SM/PC/DL/BC/BL/TN/PT) to stream loop — only ID/FV/AE on connect.

## D75 reconnect handler crash: D75CATClient not imported (2026-03-26)
**Symptom:** D75 reconnect always failed with NameError in web_server.py.
**Root cause:** `web_server.py` reconnect handler referenced `D75CATClient` but the class was never imported (same circular import pattern as RTLAirbandManager).
**Fix:** Lazy import `from cat_client import D75CATClient` inside the reconnect handler.

## D75 close() killed poll thread reconnect loop (2026-03-26)
**Symptom:** After BT drop, poll thread detected disconnect and tried to reconnect, but the reconnect attempt killed the poll thread itself.
**Root cause:** Reconnect called `close()` to clean up, which called `_poll_thread.join()` — but the current thread WAS the poll thread. Thread tried to join itself → RuntimeError (or silent deadlock depending on Python version).
**Fix:** Added `_disconnect_for_reconnect()` method that cleans up socket/state without joining the poll thread. Poll thread uses this instead of `close()`.

## D75 _recv_line EOF didn't set _connected=False (2026-03-26)
**Symptom:** After proxy TCP drop, poll thread kept running but never triggered reconnect. Gateway showed stale "connected" status.
**Root cause:** `_recv_line()` returned `None` on EOF but didn't set `_connected = False`. Poll thread checked `_connected` to decide whether to reconnect — it was still True.
**Fix:** `_recv_line()` now sets `self._connected = False` on empty recv (EOF).

## D75 connected status showed TCP as radio-connected (2026-03-26)
**Symptom:** Dashboard showed D75 as "connected" when only TCP to proxy was up but BT serial to radio was down.
**Root cause:** `connected` property returned `True` if TCP socket existed, regardless of `serial_connected` state.
**Fix:** `connected` now requires both `_sock is not None` AND `_serial_connected`. Status accurately reflects radio reachability.

## D75 tone/shift/offset wrong FO field indices (2026-03-24)
**Symptom:** Tone display always showed "DCS ON" (false), setting tone changed mode to DV, FO SET silently rejected (resp=None), proxy crashed on tone command (gateway timeout → broken pipe).
**Root causes (4 layered bugs, each masked by the next):**
1. **Wrong FO field count:** Assumed 11-field FO format; TH-D75 has 21 fields. FO SET with 11 fields silently rejected by radio (no response). Fixed: send all 21 fields via `','.join(fp)`.
2. **Wrong flag indices:** Tone/CTCSS/DCS flags at fp[5/6/7] → actually fp[8/9/10]. fp[7] (fine_step=1) was read as DCS ON. Mode at fp[5] was overwritten as "tone flag" → corrupted mode to DV.
3. **Wrong shift/mode indices:** fp[3] used as shift (actually rxstep), fp[4] used as mode (actually txstep). Real shift=fp[13], real mode=fp[5].
4. **Gateway timeout crash:** 3 serial operations (FO read + FO SET 2s timeout + FO readback) exceeded gateway's 3s send_command timeout → broken pipe. Fixed: async readback in background thread.
**Fix:** Complete rewrite using LA3QMA/Hamlib-verified 21-field layout. Also updated CTCSS list to 42 tones (was 39). `radio_automation.py` `_tune_d75()` already had correct indices — should have been used as reference from the start.
**Lesson:** Always check existing codebase for reference implementations before guessing field layouts. Instrument and verify with real data immediately rather than iterating on assumptions.

## D75 serial never connects on startup (2026-03-24)
**Symptom:** UI stuck at "Connecting..." forever. Proxy showed btstart completing but `serial_connected=False` permanently.
**Root causes (3 separate bugs):**
1. `_send_cmd` has 3s timeout but `!btstart` blocked the proxy for 15-30s → response desync corrupted all subsequent poll_state JSON parses → `serial_connected` never became True.
2. After making btstart non-blocking in proxy, `_do_btstart` connected audio+CKPD but skipped `serial.connect()` when serial wasn't already up at btstart time — only reconnected serial if it was previously connected.
3. When TCP dropped, poll thread called `close()` which tried to `join()` the current thread → `RuntimeError: cannot join current thread` → poll thread died → no more CAT activity.
**Fixes:**
1. `remote_bt_proxy.py`: made `!btstart` non-blocking (background thread, returns "btstart initiated" immediately).
2. `remote_bt_proxy.py`: `_do_btstart` always calls `serial.connect()` at the end regardless of prior serial state.
3. `cat_client.py`: `close()` checks `self._poll_thread is not threading.current_thread()` before joining.
4. `remote_bt_proxy.py`: added `serial_connected` field to `to_dict()` (not derived from `model_id` which is empty if `ID` query times out on init).
**Lesson:** Any proxy command that does BT I/O must be non-blocking or use a long timeout. TCP protocol desync is hard to debug without trace logging.

## D75 BT Start button appears during auto-connect (2026-03-24)
**Symptom:** After gateway auto-connects to D75 proxy (via retry loop), the web UI shows "Radio BT Serial: Not responding" and a BT Start button even though btstart was already triggered automatically. User sees this and thinks they need to press the button.
**Root cause:** No "in progress" state was tracked. The UI showed `serial_connected=False` with the BT Start button immediately, with no indication that btstart was already running.
**Fix:** Added `_btstart_in_progress` flag to `D75CATClient`. Set True when btstart is sent (retry loop, reconnect handler, btstart web command, polling loop auto-reconnect). Cleared when `poll_state` sets `serial_connected=True`, or when btstop runs. Reported in `/d75status`. UI shows "Connecting..." (orange) instead of "Not responding" (red) and hides the BT Start buttons when pending.

## DISPLAY_TEXT VFO Misattribution — Wrong Frequency on Wrong VFO (2026-03-13)
**Symptom:** Left VFO web display showed the right VFO's frequency (both showed 146400 instead of left=147435, right=146400). Display corruption happened during RTS changes for playback/announcements.

**Root cause:** `DISPLAY_TEXT` (pkt_type 0x01) used `self._channel_vfo` (set by the last `CHANNEL_TEXT` packet) to decide which VFO to assign the frequency text to. But `_channel_vfo` is stale — it reflects whichever CHANNEL_TEXT the drain thread last processed, not the VFO of the current DISPLAY_TEXT packet. During RTS changes, the radio sends a burst of display packets, and the drain thread processes them with the wrong VFO assignment.

**Fix:** DISPLAY_TEXT now reads the VFO directly from its own `vfo_byte` (0x40/0x60=LEFT, 0xC0/0xE0=RIGHT), same mapping as DISPLAY_ICONS. Falls back to `_channel_vfo` only for unknown vfo_byte values.

**Also fixed:** All `set_rts()` calls now pause the drain thread to prevent it from racing for display update packets triggered by the RTS change.

**Lesson:** Never rely on stale cross-packet state (`_channel_vfo`) when the packet itself contains the VFO identifier. This was the root cause of display corruption that appeared related to RTS/serial timing but was actually a packet parsing issue.

## Browser Mic PTT — No Audio Transmitted (2026-03-12)
**Symptom:** MIC PTT button keyed radio but no audio was heard over the air. Two separate bugs.

**Bug 1: ScriptProcessorNode buffer size not power of 2**
`createScriptProcessor(2400, 1, 1)` — 2400 is invalid. ScriptProcessorNode requires power-of-2 buffer sizes (256, 512, 1024, 2048, etc.). Browser silently fails and `onaudioprocess` never fires, so no PCM data is sent over WebSocket. Debug log confirmed `push_audio` was never called.
**Fix:** Changed to `createScriptProcessor(2048, 1, 1)` (~42ms at 48kHz).

**Bug 2: Browser mic levels extremely low**
`getUserMedia` was called with `autoGainControl:false, noiseSuppression:false`. Raw mic levels were RMS ~4-35 (out of 32767). Even with 25x volume multiplier, output was barely audible.
**Fix:** Changed to `autoGainControl:true, noiseSuppression:true, echoCancellation:true`. Dramatically boosted input levels.

**Bug 3 (earlier): AIOC GPIO PTT doesn't key radio**
`PTT_METHOD=aioc` uses AIOC HID GPIO for PTT. But the user's radio PTT is wired through the CAT serial cable (FTDI), not the AIOC data port. `set_ptt_state(True)` wrote to AIOC GPIO which isn't connected to PTT.
**Fix:** WebMic handler keys PTT via CAT `!ptt` command directly (same as the working regular PTT button). Added `_webmic_ptt_active` flag to prevent PTT release timer interference.

**Bug 4 (earlier, reverted): `set_rts(True/False)` broke all PTT**
First attempted fix used `cat_client.set_rts(True)` to key PTT. This put the serial RTS into "USB Controlled" mode, which interfered with the normal `!ptt` command. Both PTT buttons stopped working. Reverted to using `!ptt` toggle instead.

**Lesson:** `ScriptProcessorNode` buffer size MUST be power of 2 — browsers silently fail. Browser mic `autoGainControl` should generally be enabled for web-to-radio audio. Don't use `!rts` for PTT control — it changes RTS mode and interferes with `!ptt`.

## CAT Serial Not Cleaned Up on Restart — Orphaned Processes (2026-03-12)
**Symptom:** After each gateway restart, orphaned `start.sh`, `cloudflared`, `ffmpeg` processes accumulated. After several restarts, 13+ orphaned start.sh, 10+ cloudflared, 15+ ffmpeg lingering.

**Root causes (three issues):**
1. **`KillMode=process`** in `radio-gateway.service` — systemd only sent SIGTERM to the main PID (start.sh), not its children. Cloudflared, ffmpeg, TH9800_CAT all survived as orphans.
2. **`RadioCATClient.close()` not graceful** — slammed socket shut without setting `_stop` flag, sending `!exit`, or waiting for drain thread. Left TH9800_CAT server with stale client state.
3. **TH9800_CAT.py no SIGTERM handler** — headless mode used `while True: await asyncio.sleep(10)` which didn't run the serial cleanup `finally` block on SIGTERM.

**Fix:**
1. Changed to `KillMode=control-group` — kills entire cgroup (all children) on stop/restart.
2. `close()` now: sets `_stop=True`, sends `!exit\n`, `shutdown(SHUT_RDWR)`, waits 150ms for drain thread.
3. TH9800_CAT.py: registered SIGTERM/SIGINT handlers, replaced sleep loop with `asyncio.Event.wait()`, cleanup sets DTR low and closes serial+TCP.

## CAT Serial Shows Disconnected on Startup (2026-03-12)
**Symptom:** Radio control web page showed serial "Disconnected" after gateway restart, even though TH9800_CAT had serial connected (channels changeable via buttons).

**Root cause:** `_serial_connected` initialized to `False` and only set `True` by the web UI's SERIAL_CONNECT button handler. Gateway startup never queried serial state. Additionally, `SERIAL_CONNECT` handler rejected "already connected" response (`'already' not in resp` check).

**Fix:**
1. On CAT TCP connect, gateway sends `!serial status` — if disconnected, auto-sends `!serial connect`.
2. If serial is connected (fresh or already), refreshes display via VFO dial press+release and reads RTS state.
3. Web UI SERIAL_CONNECT: "already connected" now treated as success (`_serial_connected = True`), but skips display refresh (already populated).

## Cloudflare Tunnel Dies on Gateway Restart — Email Sent With No URL (2026-03-18)
**Symptom:** On gateway restart, email sent with no Cloudflare link. Log shows `[Tunnel] cloudflared exited (code 1)` immediately at startup.

**Root cause:** cloudflared was started with `stderr=subprocess.PIPE`. When the gateway was killed with `pkill -9`, the pipe read-end closed. cloudflared received SIGPIPE on its next stderr write and exited. There was then a race: pgrep found no cloudflared (just died), launched a new one, but the old one was still releasing port 20241 (cloudflared metrics port). New cloudflared failed to bind 20241 → code 1 → email waited 60s → sent without URL.

**Fix:**
1. cloudflared now launched with `stdout=log_f, stderr=log_f` (writes to `/tmp/cloudflared_output.log`) + `start_new_session=True`. No pipes → no SIGPIPE → cloudflared survives gateway restarts. pgrep always finds it on restart → adoption path → email uses URL immediately.
2. On fresh launch, `/tmp/cloudflare_tunnel_url` is cleared so the email waits for the new URL instead of sending with a stale cached URL.
3. `_run_thread()` retries up to 3 times (5s delay) if cloudflared exits code 1 immediately — safety net for port-conflict race conditions.
4. `_tail_log()` reads from the log file instead of from a pipe (compatible with detached process).
5. Adoption now also scans the log file for URL if URL_FILE is missing.
6. Email now includes a detailed gateway + system status dump (`_build_status_dump()`) below the links.

**Lesson:** Never use `subprocess.PIPE` for long-lived child processes that should outlive the parent. Use log files + `start_new_session=True` for true process independence.

## Email URL Corruption — `%3Cbr%3E` in Cloudflare Links (2026-03-12)
**Symptom:** Cloudflare tunnel links in emails didn't work. URLs ended with `%3Cbr%3E`.

**Root cause:** `body.replace('\n', '<br>\n')` ran BEFORE the URL regex `re.sub(r'(https?://\S+)', ...)`. The `<br>` was captured as part of the URL.

**Fix:** Swapped order — linkify URLs first, then insert `<br>` tags.

## Audio Processing Has No Audible Effect (2026-03-11)
**Symptom:** All audio processing buttons (HPF, LPF, notch, de-esser, spectral NS, noise gate) had zero audible effect when toggled via dashboard or keyboard, for both radio and SDR sources.

**Root causes (two bugs):**
1. **`scipy` not installed** — All filter methods (`_apply_hpf`, `_apply_lpf`, `_apply_notch`, etc.) import `scipy.signal` inside `try/except Exception` blocks. With scipy missing, every filter silently caught the `ImportError` and returned unmodified audio. The broad `except Exception` masked the real error completely.
2. **`PipeWireSDRSource.get_audio()` missing processing call** — overrides `SDRSource.get_audio()` but omitted the `process_audio_for_sdr(raw)` call. Even with scipy installed, PipeWire SDR audio would bypass all filters.

**Fix:**
1. Installed `scipy` (`pip install scipy`) and added it to `scripts/install.sh` `CORE_PKGS`.
2. Added `raw = self.gateway.process_audio_for_sdr(raw)` in `PipeWireSDRSource.get_audio()`.

**Lesson:** Silent `except Exception` on imports can hide missing dependencies for months. Consider logging a warning on first failure, or checking at startup.

## Google AI Scrape Navigation Failure (2026-03-11)
**Symptom:** Smart Announce `google-scrape` backend always returned "no AI Overview found".

**Root cause:** `_scrape_google_ai_overview()` used Firefox dev console (`Ctrl+Shift+K`) to navigate and execute JS. When Firefox was showing the gateway dashboard, the dashboard's keyboard event handler intercepted `Ctrl+Shift+K` before Firefox could open the console. The JS navigation command never ran, so the clipboard copy returned dashboard text instead of Google results.

**Fix:** Replaced dev console approach with URL bar (`Ctrl+L`) for navigation and `udm=50` Google URL parameter for direct AI Mode access. Eliminates all console JS (AI Mode click heuristic was also broken). Much simpler and more reliable.

## WebSocket PCM Audio — Stuttering, Half-Speed, and High Latency (2026-03-11)
**Symptom:** Low-latency WebSocket audio had three issues: (1) constant small gaps/stuttering, (2) audio playing at half speed, (3) ~2 second delay.

**Root causes & fixes:**
1. **Stuttering:** No pre-buffering + Nagle's algorithm buffering small writes. Fix: Added 50ms pre-buffer, TCP_NODELAY, per-client send queues with dedicated sender threads.
2. **Half-speed playback:** `push_ws_audio()` called twice per audio loop iteration — once early in mixer path (line ~11849) and again at end of common path (line ~12178). Doubled the data rate. Fix: Removed duplicate push at end; keep only early pushes in mixer and direct-AIOC paths.
3. **High latency (~2s):** Three sources: (a) PipeWire SDR source used FFmpeg which buffers heavily by default. Fix: Replaced FFmpeg with native `parec --latency-msec=20`. (b) AIOC ALSA period was 200ms (4x chunk) with 3-blob pre-buffer (600ms). Fix: Reduced to 100ms period (2x) with 2-blob pre-buffer (200ms). (c) Client-side buffer caps too high (500ms). Fix: Reduced to 150ms.

## /status BrokenPipeError (2026-03-11)
**Symptom:** Stack trace logged when browser disconnected during `/status` JSON response.

**Root cause:** `/status` endpoint was missing `BrokenPipeError` handling that all other endpoints already had.

**Fix:** Wrapped `/status` write in try/except BrokenPipeError: pass.

## SDR Control Page — Multiple Init Bugs (2026-03-10)
**Symptom:** Web UI crashed entirely (no pages served), SDR commands returned NameError, settings lost on restart, sample rate dropdown showed blank.

**Root causes & fixes:**
1. **`shutil` not imported at module level** — `shutil.which('rtl_airband')` in `WebConfigServer.start()` crashed the entire web server. Fix: added `import shutil` to top-level imports.
2. **`subprocess` not imported at module level** — all `RTLAirbandManager` operations failed with NameError. Fix: added `import subprocess` to top-level imports.
3. **`json` not imported at module level** — `_save_channels()` silently wrote empty file (0 bytes). Fix: added `import json as json_mod` to top-level imports.
4. **rtl_airband daemonizes** — `Popen.poll()` always showed exited (parent forks and exits). Fix: always use `pgrep` for status, use `subprocess.run()` instead of `Popen` to start.
5. **sdrplay_apiService ignores SIGTERM** — `systemctl restart sdrplay.service` hung for 30s+. Fix: `killall -9 sdrplay_apiService` then `systemctl start`.
6. **rtl_airband ignores SIGTERM** — `killall rtl_airband` didn't kill old instances. Fix: `killall -9`.
7. **Audio level bar scaling wrong** — divided by 327.68 (raw PCM), but `audio_level` is already 0-100%. Fix: use value directly.
8. **`squelch_threshold` wrong type** — wrote positive integers, rtl_airband v5 requires negative dBFS. Fix: slider range -60 to 0, 0 = auto.
9. **Sample rate dropdown blank** — JS `select.value = 2.0` becomes `"2"`, doesn't match option `"2.0"`. Fix: `setSelectByValue()` matches by closest numeric value.
10. **Settings lost on restart** — no persistence. Fix: save `current` dict alongside channels in `sdr_channels.json`.
11. **Misleading Bandwidth dropdown** — rtl_airband `sample_rate` IS the bandwidth. Removed separate bandwidth dropdown, expanded sample rate options to all RSPduo-supported values.

## TH9800 CAT CHANNEL_TEXT VFO Mapping (2026-03-09) — MAJOR
**Symptom:** `set_channel()` set channels on the wrong VFO, detected wrong mode, or failed entirely.

**Root cause:** The TH9800 radio press response is unreliable for reading the pressed VFO's channel:
- Dial PRESS response: returns the OTHER VFO's channel (not the pressed VFO's)
- Dial STEP response: `_channel_text[vfo]` correctly holds the stepped VFO's channel
- Background drain thread races with `set_channel` reads if not paused

**Fix:** Don't trust press response at all. Read current channel via step-right + step-left (net zero).

**Also fixed:**
- `_pause_drain()` during entire `set_channel` (prevents background drain race)
- `setup_radio` simplified: RTS set once (no `_with_usb_rts` save/restore)
- Never presses V/M button (was causing mode toggles)
- Response tracking: `_cmd_sent`, `_cmd_no_response`, `_last_no_response` counters
- Web dashboard shows CAT reliability stats (CMD sent/missed)

## CAT Socket Contention — Web UI Commands Ignored (2026-03-09)
**Symptom:** Web UI radio buttons (dial up/down) had no effect. All commands showed "no response".
**Root cause:** Background drain thread and `send_web_command` share one TCP socket.
Setting `_drain_paused = True` is not enough — the drain thread may already be inside
`_drain(0.5)` reading from the socket. It consumes the `!data` command response ("data sent")
meant for `_send_cmd`, so the button press appears to have no effect.
**Fix:** `_pause_drain()` sets the flag AND waits for `_drain_active` to go False (up to 1s),
ensuring the drain thread has actually stopped reading before any command is sent.

## TH9800_CAT Auth Per-Connection (2026-03-09)
**Symptom:** Gateway loses auth after any second TCP client connects then disconnects.
**Root cause:** `tcpserver_loggedin` was a shared instance variable on the TCP class.
When ANY connection closed, it reset `loggedin = False` for all connections.
**Fix (TH9800_CAT.py):** Made auth per-connection using `nonlocal conn_loggedin` in
`handle_tcpserver_stream`. Also added auto-re-auth in gateway's `_send_cmd` on "Unauthorized".

## Web UI Freeze — Fetch Pileup Exhausts Browser Connection Pool (2026-03-14)
**Symptom:** Dashboard stops updating, buttons become unresponsive. Closing and reopening the browser fixes it.

**Root cause:** `setInterval` fires `fetch('/status')` every 1s and `fetch('/sysinfo')` every 2s with no guard against overlapping requests. If any fetch takes >1s (server busy, network latency), requests pile up. Browsers limit concurrent connections per origin to ~6. With MP3 stream and/or WebSocket also holding connections, the remaining slots fill with queued fetches, blocking all further requests.

**Fix:** Added in-flight guards (`_statusBusy`, `_sysinfoBusy`, `_radioBusy`, `_sdrBusy`) to all four polling functions across all pages. `.finally()` clears the flag regardless of success/failure.

## Software PTT — `_ptt_software()` Called `set_rts()` Instead of `!ptt` (2026-03-14)
**Symptom:** PTT_METHOD=software caused VFO selection to flip back and forth instead of keying the radio.

**Root cause:** `_ptt_software()` called `self.cat_client.set_rts(state_on)` which sends `!rts True/False` — toggling between USB-controlled and radio-controlled mode. That's not PTT. The VFO display switching was a side effect of RTS mode changes.

**Fix:** Changed to send `!ptt` toggle command. Added `_software_ptt_on` state tracker (independent of `ptt_active` which is set in multiple places before `set_ptt_state()` runs). Also gated RTS save/restore and VFO dial-press refresh to skip in software PTT mode.

## Software PTT — Stuck Keyed After Playback (2026-03-14)
**Symptom:** PTT keyed successfully for file playback but never unkeyed. Radio stayed transmitting.

**Root cause:** PTT release timer (line 14593) and `stop_playback()` (line 1365) both set `self.ptt_active = False` *before* `set_ptt_state(False)` runs via `_pending_ptt_state`. When `_ptt_software()` checked `if state_on == self.ptt_active`, both were False, so it returned early without sending `!ptt` to unkey.

**Fix:** Track actual CAT PTT state in `_software_ptt_on` (class variable), independent of `self.ptt_active`. Only send `!ptt` toggle when `state_on != _software_ptt_on`.

## Software PTT — No Feedback When Radio Powered Off (2026-03-14)
**Symptom:** Pressing playback button with radio powered off showed TX activity (audio bars) but no PTT and no error message to user.

**Root cause:** When radio is off but USB-serial adapter is still connected, the CAT server's serial transport is "open" — `!ptt` returns `"True"/"False"` (toggled state) without knowing the radio ignored the command. The gateway never checked the response.

**Fix:** (1) Check `_send_cmd("!ptt")` response for `"serial not connected"`. (2) Track `_last_radio_rx` timestamp (set when binary radio packets arrive via `_parse_radio_packet`). If no radio data received for >5 seconds, refuse to key and push notification: "PTT failed: radio not responding". (3) Added web UI toast notification system for all error feedback.

## Audio Streaming Ring Buffer (2026-03-08)
**Symptom:** "No encoder data" errors, gaps in browser audio playback.
**Root cause:** `pop(0)` shifted list indices but `pos` was absolute sequence number.
**Fix:** Sequence-number ring buffer (`_mp3_seq`) immune to index shifting.

## Save & Restart Leaves External Components in Restart Loops (2026-03-20)
**Symptom:** After pressing "Save & Restart" in web config, external components (AIOC, ALSA loopback, Darkice/FFmpeg, CAT service) end up in restart loops or bad state. Works fine when restarting via desktop icon.

**Root cause:** Web UI restart used `os.execv(sys.executable, [sys.executable] + sys.argv)` — a bare Python process replacement. This skips all 11 start.sh setup steps: no ALSA loopback reset (`modprobe -r snd-aloop`), no AIOC USB reset, no Mumble restart, no CAT service management, no CPU governor. New Python process tries to reopen ALSA/AIOC in stale state → watchdogs fire → restart loops.

**Fix:** Web UI restart now runs `start.sh` as a detached subprocess (`start_new_session=True`, stdout/stderr → `/tmp/gateway_startup.log`) then exits. start.sh kills the old process via `pkill -9 -f radio_gateway.py` and does the full reset sequence.

**Lesson:** Any restart path that bypasses start.sh will be broken. The `q` key still uses `os.execv` (in-process restart) — fine for config-only changes but will also break if hardware state is disrupted.

## Save & Restart UnboundLocalError (2026-03-08)
**Symptom:** `UnboundLocalError: cannot access local variable 'port'` on Save & Restart from web UI.
**Fix:** Use `window.location.port` in JavaScript instead of Python-side `port` variable.

## KV4P TX 20% Audio Dropout (2026-03-19)
**Symptom:** KV4P TX audio sounded choppy/incomplete. Audio trace confirmed 960 bytes dropped per tick.

**Root cause:** Mixer outputs 4800 bytes (50ms @ 48kHz stereo int16). Opus encoder requires exactly 3840 bytes (1920 samples × 2 bytes = 40ms frames). 4800 mod 3840 = 960 bytes discarded every tick = 20% loss.

**Fix:** `_tx_buf` accumulation in `write_tx_audio` — carry the remainder across calls. `_tx_buf` cleared on PTT drop to prevent stale audio bleeding into next transmission. Verified via audio trace: `frames_sent` went from 1.0/tick to 1.25/tick (25% more audio delivered).

**Lesson:** Always check frame alignment when bridging fixed-size audio chunks to a codec with different frame size.

## KV4P Announcement Delay Applied to Serial Radio (2026-03-19)
**Symptom:** First 0.5s of each KV4P announcement silent (×3 = 1.65s total per 3-announcement run).

**Root cause:** `PTT_ANNOUNCEMENT_DELAY` is designed for relay-switched PTT that needs settle time. Was unconditionally applied to all TX paths including KV4P, which uses serial audio with no physical relay.

**Fix:** `_needs_delay = self.announcement_delay_active and not _use_kv4p_tx` — skip silence substitution when KV4P is the TX radio.

## KV4P CTCSS Wrong Tone Transmitted (2026-03-19)
**Symptom:** Setting CTCSS 103.5 Hz in web UI caused radio to transmit 107.2 Hz instead.

**Root cause:** KV4P CTCSS dropdown was built from the TH-9800's 39-tone list which includes 69.3 Hz at index 1. The DRA818V module used in KV4P has 38 tones (no 69.3 Hz). Every tone above 67.0 was off by one code: 103.5 Hz → index 13 → code 14 → DRA818 maps code 14 to 107.2 Hz.

**Fix:** KV4P page now uses the correct 38-tone DRA818 list (no 69.3 Hz). With correct list: 103.5 Hz → index 12 → code 13 → DRA818 transmits 103.5 Hz.

**Lesson:** DRA818V CTCSS codes 1–38 map to 67.0, 71.9, 74.4 … 250.3 Hz (no 69.3). The Kenwood/ICOM standard includes 69.3; DRA818 does not. Never reuse tone lists across different radio hardware.

## TH9800 PTT State Inversion — Blind Toggle Race (2026-03-21)
**Symptom:** After switching TX_RADIO from kv4p to th9800, first PTT press showed button on but radio didn't key; second press showed button off but radio keyed up. Classic state inversion.

**Root cause:** `!ptt` in TH9800_CAT.py was a blind toggle — it flipped `radio.mic_ptt` regardless of current state. If gateway state and radio state diverged (e.g. previous session, radio reboot, or any missed command), the states were inverted and stayed inverted until another toggle.

**Fix:** Added `!ptt on` / `!ptt off` explicit-state commands to TH9800_CAT.py. Gateway now always sends `!ptt on` or `!ptt off` instead of the bare toggle. Bare `!ptt` still works for backwards compat. Removed `_software_ptt_on` tracker from gateway_core.py (was attempting to work around the broken toggle; now redundant).

**Also:** ws_mic PTT routing updated to use `!ptt on`/`!ptt off` via `cat_client._send_cmd()` directly, rather than going through `set_ptt_state()` which would double-key.

**Lesson:** Never use a stateful toggle for safety-critical control (PTT, relay, etc.). Always send an explicit desired state so the command is idempotent and safe to retry.

## RTS "Unknown" When CAT_STARTUP_COMMANDS=false (2026-03-21)
**Symptom:** After serial connect, RTS TX state in dashboard showed "Unknown" instead of "USB Controlled".

**Root cause:** `_rts_usb` is `None` at startup and only set by `set_rts()`. With `CAT_STARTUP_COMMANDS = false`, `setup_radio()` is skipped — and `setup_radio()` was the only caller of `set_rts(True)` at startup. So the dashboard never learned the RTS state.

**Fix:** Call `set_rts(True)` explicitly after successful `!serial connect`, both at gateway startup and in the web UI's SERIAL_CONNECT button handler. No dependency on CAT_STARTUP_COMMANDS.

## SDR Manager Not Available — Circular Import (2026-03-21)
**Symptom:** Dashboard SDR section showed "SDR manager not available" even when SDR was enabled in config.

**Root cause:** `web_server.py` used `RTLAirbandManager` (defined in `gateway_core.py`) but never imported it. Couldn't add a top-level import: `gateway_core.py` imports `web_server.py` → circular import crash.

**Fix:** Lazy import inside the `if shutil.which('rtl_airband'):` guard block: `from gateway_core import RTLAirbandManager`. Import happens once at runtime when the SDR branch is actually taken.

**Lesson:** Circular imports between web_server.py and gateway_core.py require lazy imports for anything crossing from gateway_core into web_server.

## setup_radio Commands Silently Dropped (2026-03-21)
**Symptom:** On gateway startup with `CAT_STARTUP_COMMANDS = true`, channel/volume/power commands had no effect on the radio.

**Root cause:** `setup_radio()` was called BEFORE the serial connect block in gateway startup. The serial port was not yet open, so all CAT commands were silently discarded.

**Fix:** Moved the `!serial disconnect` + `!serial connect` block to run BEFORE `setup_radio()` call. Added explicit comment: "Serial connect — must happen before setup_radio so commands reach the radio."

## TH9800 Serial Controls Dead After Gateway Restart (2026-03-21)
**Symptom:** After any gateway restart, TH9800 VFO dials/buttons in web UI had no effect until user manually pressed TCP Disconnect then Connect in the dashboard.

**Root cause (three layers):**
1. **Double-STARTUP in TH9800_CAT login handler** — `handle_tcpserver_stream` called `exe_cmd(STARTUP)` on login if serial wasn't connected yet. Command got queued. When gateway then sent `!serial connect`, the connect handler ALSO sent STARTUP. Two back-to-back STARTUPs overwhelmed the radio's serial interface → RIGHT VFO went dead.
2. **False-positive serial status check** — `_send_cmd("!serial status")` used 2s timeout, but TH9800_CAT sleeps 3s after login. Timeout expired, stale "not connected" response sat in socket, next read consumed it as "connected" (substring `'connected' in 'not connected'`). Gateway skipped `!serial connect` thinking serial was up when it wasn't.
3. **No STARTUP on reconnect** — Even after fixing (1) and (2), when gateway restarted and serial was already connected from the prior session, no STARTUP was sent. Radio never re-broadcast its display state to the new gateway session → controls appeared dead.

**Fix (three parts):**
1. **TH9800_CAT.py login handler** — removed `exe_cmd(STARTUP)` entirely. The `!serial connect` handler is the sole owner of the STARTUP sequence.
2. **gateway_core.py serial startup** — replaced status-check-then-maybe-connect with unconditional `!serial disconnect` + `!serial connect` cycle. Disconnect is a no-op if serial isn't connected; connect always runs STARTUP exactly once.
3. **start.sh** — changed TH9800_CAT service handling from "start if not running" to "restart if running, start if stopped". Ensures serial is always in a clean disconnected state when the gateway comes up.

**Lesson:** Never assume shared hardware state survives a process restart. Always cycle to a known clean state on startup rather than trying to detect and preserve prior state. The "skip if already connected" optimisation caused more problems than the brief reconnect delay it saved.

## Shared SDR AudioProcessor — 5Hz IIR Filter Contamination (2026-03-23)
**Symptom:** SDR audio had tiny but audible stutters at ~5Hz. Audio trace showed `o_disc` values of 5000–10208 (output clicks) correlating with SDR activity. `s1_disc` was low (mean=209, max=1180) but output had 22 clicks.

**Root cause:** `sdr_processor` was a single shared `AudioProcessor` instance used by BOTH `SDR1` and `SDR2` sources. IIR filters (HPF, LPF, notch, noise gate) maintain state between calls. SDR1 processed its 50ms chunk → updated filter state with SDR1's signal. SDR2 then called `process_audio_for_sdr()` with the same processor → first output sample was calculated using SDR1's previous filter state → large boundary jump (effectively a click) at every 50ms chunk boundary. The shared state also contaminated SDR1 on the next tick. Manifested as clicks at ~2.7Hz average (perceived as 5Hz due to burst clustering and coincidence with 5Hz AIOC blob rhythm).

**Fix:** Created separate `sdr2_processor = AudioProcessor("sdr2", config)` with its own IIR state. `process_audio_for_sdr(pcm_data, source_name='SDR1')` now routes SDR2 to `sdr2_processor` and SDR1 to `sdr_processor`. Both sources pass `self.name` when calling `process_audio_for_sdr()`. Also added `s2_disc` column to audio trace.

**Secondary bug fixed:** Both `SDRSource.get_audio()` and `PipeWireSDRSource.get_audio()` were always looking up `SDR_DISPLAY_GAIN`/`SDR_AUDIO_BOOST` (SDR1 keys) even for the SDR2 instance. Fixed to use `SDR2_DISPLAY_GAIN`/`SDR2_AUDIO_BOOST` when `self.name == 'SDR2'`.

**Lesson:** Any stateful audio processor (IIR filters, noise gate envelopes) must never be shared between independent audio streams. A single instance shared between two sources interleaves their per-call state updates, corrupting both.

## SDR Post-Duck Stutter — Flapping + Missing Fade-In (2026-03-23)
**Symptom:** After AIOC (radio RX) transmission ends, SDR audio plays with big gaps and choppy stuttering until there is a natural break in audio, then resumes normally.

**Root cause 1 — aioc_ducks_sdrs gate caused flapping:**
Old formula: `aioc_ducks_sdrs = (is_ducked or in_padding) and (non_ptt_audio is not None or _aioc_blob_recent)`
When AIOC stopped, `non_ptt_audio=None` and `_aioc_blob_recent` expired (150ms) → `aioc_ducks_sdrs=False` → SDR started playing immediately even though `is_ducked=True` (1s hold still active). Any AIOC tail blob (VoIP echo, trailing audio) then set `non_ptt_audio != None` → `aioc_ducks_sdrs=True` → SDR abruptly cut off. Each tail blob restarted this cycle. Trace confirmed: tick 62 duck releases, tick 63 AIOC tail re-ducks (`o_disc=5605` click), repeat.
**Fix:** `aioc_ducks_sdrs = ds['is_ducked'] or in_padding` — SDR suppressed for entire hold period, no `non_ptt_audio` gate. Hold expires 1s after last AIOC blob → single clean duck-in → no flapping.

**Root cause 2 — AIOC tail blobs caused immediate re-duck after hold expires:**
After duck-in (`is_ducked=False`), if AIOC sent any blob, `other_audio_active=True`, `prev_signal=False` (just cleared at duck-in) → new duck-out fired immediately → new 1s padding silence → new duck cycle.
**Fix:** `REDUCK_INHIBIT_TIME=2.0s` — after duck-in, new duck-out blocked for 2s. `_duck_in_time` stored in duck state dict. Trace shows `I` flag while inhibit active.

**Root cause 3 — missing fade-in at duck release:**
`sdr_prev_included` was never updated during a duck (duck path sets `should_duck=True` and skips the else branch). After any duck, `sdr_prev_included=True` from before the duck → first SDR chunk after duck played at full volume (no 10ms onset fade-in) → audible click.
**Fix:** At duck-in transition, reset `sdr_prev_included[name]=False` for all SDRs. Fade-in now always fires on the duck-release tick. Trace `F` flag and DUCK RELEASE EVENTS section confirm.

**Lesson:** When suppressing audio via a high-level gate (`aioc_ducks_sdrs`), the gate must be stable for the full duration the audio should be suppressed — don't poke holes in it based on input data availability. Use a separate hold timer. Re-entry into a duck cycle after release should require a cooldown.

## stream_health Always False in /status API (2026-03-23)
**Symptom:** Broadcastify stream health always reported False in `/status` JSON even when DarkIce was running and stream was connected.

**Root cause:** `stream_health` was set to `bool(getattr(self.config, 'ENABLE_STREAM_HEALTH', False))` — returning whether a (non-existent) config option was enabled, not whether the stream was actually alive. `ENABLE_STREAM_HEALTH` doesn't exist in config, so `getattr` always returned False.

**Fix:** `stream_health` now checks all three conditions: `self._darkice_pid is not None and getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)` — DarkIce process running AND StreamOutputSource connected. This matches the existing `stream_pipe_ok` field logic (line 6981).

**Lesson:** When adding a new status field, copy the pattern from an existing similar field rather than inventing a new attribute name.

## MCP sdr_tune Wrong Payload Keys (2026-03-23)
**Symptom:** `sdr_tune` MCP tool sent `freq` and `squelch` keys in the POST body, but the gateway `/sdrcmd` handler expected `frequency` / `frequency2` and `squelch_threshold` / `squelch_threshold2`. Commands silently had no effect.

**Root cause:** MCP tool was written before the SDR2 dual-tuner channel key naming was finalised. Old single-channel keys (`freq`, `squelch`) were never updated. The `label` parameter was also vestigial (gateway ignores it).

**Fix:** `sdr_tune` now uses channel-specific keys: channel 1 → `frequency`/`squelch_threshold`, channel 2 → `frequency2`/`squelch_threshold2`. Removed `label` param. Timeout raised from 10s to 20s (tuning requires SDR restart which takes ~12s). `sdr_restart` timeout also raised to 20s. `_post()` now accepts a configurable `timeout` parameter.

**Lesson:** When the gateway API changes, audit all MCP tool payloads that hit affected endpoints — they won't throw errors, they just silently do nothing.

## KV4P Web UI Poll Overwriting User Input (2026-03-19)
**Symptom:** CTCSS and other fields reset while user was trying to type/select a value.

**Root cause:** Status poll fires every 1.5s and unconditionally overwrites all control fields. `_ctrlEditUntil` timer (set on `onfocus`) only protects for 5 seconds — expires before slow user finishes.

**Fix:** `_kvset(id, val)` helper skips update if `document.activeElement.id === id`. Field is never overwritten while focused, regardless of timing. Timer bumped to 30s for dropdowns.

## SDR2 Permanently Ducked — D75 Noise Above SDR_SIGNAL_THRESHOLD (2026-03-24)
**Symptom:** SDR2 level bar shows red on dashboard; only clicks heard; audio trace shows `ducked: 489/489 (100%)`, `other_audio_active: 489/489 (100%)`, source breakdown `D75: 466 (95.3%)`.

**Root cause:** `SDR_SIGNAL_THRESHOLD = -70.0` dBFS was too sensitive. D75 Bluetooth audio source continuously produces background audio at ~-65 dBFS (below VAD threshold -40 dBFS, but above -70 dBFS). `has_actual_audio(non_ptt_audio, "Radio")` mixed all non-PTT sources including D75, so D75 idle noise kept `other_audio_active=True` 100% of the time → `is_ducked` never released → SDRs suppressed permanently.

**Fix:** Raised `SDR_SIGNAL_THRESHOLD` from -70.0 to -45.0 in `gateway_config.txt`. Now matches VAD threshold — audio that can't pass the VAD gate won't duck the SDRs either.

**Lesson:** SDR_SIGNAL_THRESHOLD must be above the noise floor of ALL non-PTT sources (AIOC + D75 + Remote), not just AIOC. Use audio trace to check `other_audio_active` percentage — should be near 0% when radio is quiet.

## ANNIN Level Bar Stuck After Voice Note Transmission (2026-03-24)
**Symptom:** AN: bar on dashboard stays at non-zero % indefinitely after voice note finishes transmitting.

**Root cause:** `NetworkAnnouncementSource.get_audio()` only updates `audio_level` on the "happy path" (when audio is above threshold and returned). The early-return paths (`Queue.Empty` → return None, below-threshold hold expired → return None) left `audio_level` at its last value.

**Fix:** Added `self.audio_level = 0` before both `return None, False` exits in `get_audio()` (`audio_sources.py`). Level now resets when the queue drains and the PTT hold expires.

## Telegram Voice Note — Only Brief PTT, No Audio (2026-03-24)
**Symptom:** Radio keys PTT for ~0.5s then releases; voice not heard; bot reports success.

**Root cause:** `_transmit_audio()` sent all PCM chunks as fast as TCP would allow. ANNIN queue is `maxsize=16` (drop-oldest). A 5.3s voice note = ~106 chunks at 50ms each. First 16 queued, rest dropped. ~0.8s of audio played then silence; PTT released.

**Fix:** Added real-time pacing in `_transmit_audio()`: after each chunk, sleep until `next_send` time (one chunk interval = `chunk_size / sample_rate` seconds). Queue stays full but never overflows.

## AGWPE Proxy 2-Second Session Death (2026-04-16)
**Symptom:** Winlink Connect & Sync dies after ~2s with exit code -15. Only one TX burst heard.
**Root cause:** `socket.settimeout(2.0)` set for the connect phase to Direwolf persisted into `recv()` calls in the `_fwd` thread. Any 2-second gap in RF data (normal for AX.25) caused a timeout exception, killing the proxy session. Then `_restart_after_session` ran `pkill -f 'pat connect'`, killing Pat with SIGTERM (-15).
**Fix:** Added `r.settimeout(None)` after successful connect. Removed `pkill`. Made `_agwpe_proxy_session` block on `done.wait()` so `_proxy_sessions_active` counter stays > 0 while session is live.
**File:** `packet_radio.py` (_agwpe_proxy_session)

## Spurious AGWPE Session from Pat Startup Test (2026-04-16)
**Symptom:** Every time winlink mode is entered, Direwolf immediately restarts. Pat can't connect.
**Root cause:** `_delayed_pat_start` connected to `127.0.0.1:8010` (the AGWPE proxy) to test readiness. The proxy forwarded this to Direwolf, opening a real AGWPE session. The test socket closed immediately → session ended → Direwolf restarted.
**Fix:** Changed test to connect to the remote KISS port (8001) instead of the local AGWPE proxy.
**File:** `packet_radio.py` (_delayed_pat_start)

## Zombie Endpoint Process Causing Duplicate Rejections (2026-04-16)
**Symptom:** Gateway logs flooded with "Duplicate endpoint name 'celeron-ftm150'" every 5 seconds.
**Root cause:** Manual `nohup` start of endpoint during debugging left a second process running alongside the systemd user service. Both tried to register the same name.
**Fix:** Killed manual process, confirmed systemd service is sole manager. All endpoints now use consistent systemd user services with linger enabled.
