# Bug History — Radio Gateway

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
