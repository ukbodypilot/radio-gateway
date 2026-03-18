# Bug History — Radio Gateway

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

## Save & Restart UnboundLocalError (2026-03-08)
**Symptom:** `UnboundLocalError: cannot access local variable 'port'` on Save & Restart from web UI.
**Fix:** Use `window.location.port` in JavaScript instead of Python-side `port` variable.
