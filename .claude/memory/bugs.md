# Bug History — Radio Gateway

## TH9800 CAT CHANNEL_TEXT VFO Mapping (2026-03-09) — MAJOR
**Symptom:** `set_channel()` set channels on the wrong VFO, detected wrong mode, or failed entirely.
**Root cause:** The TH9800 radio protocol has inconsistent VFO byte mapping in CHANNEL_TEXT (0x02) packets:
- Dial PRESS response: CHANNEL_TEXT vfo_byte maps to the OPPOSITE VFO in `_channel_text` dict
- Dial STEP response: CHANNEL_TEXT vfo_byte maps to the CORRECT VFO in `_channel_text` dict
- `_channel_vfo` (set by DISPLAY_CHANGE 0x03 packets) always ends up as the OPPOSITE VFO after a press

**What didn't work:**
- Using `_channel_vfo` to determine which VFO responded (always wrong after DISPLAY_CHANGE)
- Using `_capture_vfo` to force all packets under one key (other VFO's packet comes last, overwrites)
- Using `self._channel` directly (both VFOs respond; last packet wins, unreliable)
- Swapping CHANNEL_TEXT vfo_byte mapping (fixes press, breaks step or vice versa)
- Swapping dial command bytes (breaks other things)
- `_drain()` loop (`while self._buf: _recv_line()`) — breaks ALL packet parsing; must use single `_recv_line(0.1)`

**Fix:** Read from different dict keys for press vs step:
```python
other_vfo = self.RIGHT if vfo == self.LEFT else self.LEFT
# After press: ch = _channel_text.get(other_vfo, '')  # SWAPPED
# After step:  ch = _channel_text.get(vfo, '')         # CORRECT
```

**Also fixed:**
- `setup_radio` simplified: RTS set once (no `_with_usb_rts` save/restore that could disrupt serial)
- Never presses V/M button (was causing mode toggles when `_channel_vfo` misdetected VFO mode)
- Background drain thread uses `_drain_paused` flag instead of locks (RLock caused timing issues)

## Audio Streaming Ring Buffer (2026-03-08)
**Symptom:** "No encoder data" errors, gaps in browser audio playback.
**Root cause:** `pop(0)` shifted list indices but `pos` was absolute sequence number.
**Fix:** Sequence-number ring buffer (`_mp3_seq`) immune to index shifting.

## Save & Restart UnboundLocalError (2026-03-08)
**Symptom:** `UnboundLocalError: cannot access local variable 'port'` on Save & Restart from web UI.
**Fix:** Use `window.location.port` in JavaScript instead of Python-side `port` variable.
