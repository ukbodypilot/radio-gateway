# Bug History — Radio Gateway

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
3. **High latency (~2s):** Three sources: (a) PipeWire SDR source used FFmpeg which buffers heavily by default. Fix: Replaced FFmpeg with native `parec --latency-msec=20`. (b) AIOC ALSA period was 200ms (4× chunk) with 3-blob pre-buffer (600ms). Fix: Reduced to 100ms period (2×) with 2-blob pre-buffer (200ms). (c) Client-side buffer caps too high (500ms). Fix: Reduced to 150ms.

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

**What didn't work:**
- Reading press response from `_channel_text[other_vfo]` (returns other VFO's channel, not pressed)
- Reading press response from `_channel_text[vfo]` (empty after press — only other key populated)
- Using `_channel_vfo` (always wrong after DISPLAY_CHANGE 0x03)
- Using `_capture_vfo` to force all packets under one key (overwrites)
- Using `self._channel` directly (both VFOs respond; last wins)
- Swapping CHANNEL_TEXT vfo_byte mapping (fixes press, breaks step)
- `_drain()` loop (`while self._buf: _recv_line()`) — breaks ALL packet parsing

**Fix:** Don't trust press response at all. Read current channel via step-right + step-left (net zero):
```python
self._drain_paused = True  # MUST pause background drain
# 1. Press dial (activates for editing, response ignored)
# 2. Step right, step left (net zero movement)
# 3. Read _channel_text[vfo] from step-left response (always correct)
# 4. Step toward target using _channel_text[vfo]
```

**Also fixed:**
- `_pause_drain()` during entire `set_channel` (prevents background drain race)
- `setup_radio` simplified: RTS set once (no `_with_usb_rts` save/restore)
- Never presses V/M button (was causing mode toggles)
- Response tracking: `_cmd_sent`, `_cmd_no_response`, `_last_no_response` counters
- Web dashboard shows CAT reliability stats (CMD sent/missed)

## CAT Socket Contention — Web UI Commands Ignored (2026-03-09)
**Symptom:** Web UI radio buttons (dial up/down) had no effect. All commands showed "no response".
Setup commands worked, but subsequent web commands silently failed.
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

## Audio Streaming Ring Buffer (2026-03-08)
**Symptom:** "No encoder data" errors, gaps in browser audio playback.
**Root cause:** `pop(0)` shifted list indices but `pos` was absolute sequence number.
**Fix:** Sequence-number ring buffer (`_mp3_seq`) immune to index shifting.

## Save & Restart UnboundLocalError (2026-03-08)
**Symptom:** `UnboundLocalError: cannot access local variable 'port'` on Save & Restart from web UI.
**Fix:** Use `window.location.port` in JavaScript instead of Python-side `port` variable.
