"""Periodic supervision: the once-a-second loop that watches everything.

Extracted from ``core/lifecycle.py`` (step 2 of its split). ``lifecycle`` had
grown to four unrelated concerns sharing nothing but ``self``; this one is the
supervisor thread, started by ``run()`` and running until shutdown.

It carries the PTT release timeout and the manual-PTT failsafe, RX/TX level
decay, the audio-stream and Broadcastify health checks, the SDR loopback
watchdog, the charger relay schedule, and Mumble connection-state detection.

Kept class-bound for the same reason as the rest of ``core/``: these methods
freely read and write ``self.*`` attributes initialised in
``RadioGateway.__init__``, so composing back via inheritance keeps the runtime
semantics identical without threading references through arguments.
"""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import threading
import time


class _MonitorMixin:
    def _charger_should_be_on(self):
        """Check if charger should be on based on current time and schedule.
        Handles overnight wrap (e.g. 23:00 → 06:00)."""
        if not self._charger_on_time or not self._charger_off_time:
            return False
        import datetime
        now = datetime.datetime.now()
        cur = (now.hour, now.minute)
        on_t = self._charger_on_time
        off_t = self._charger_off_time
        if on_t <= off_t:
            # Same-day window (e.g. 06:00 → 18:00)
            return on_t <= cur < off_t
        else:
            # Overnight wrap (e.g. 23:00 → 06:00)
            return cur >= on_t or cur < off_t
    def status_monitor_loop(self):
        """Monitor PTT release timeout and audio transmit status"""
        # Note: Priority scheduling removed - system manages all threads

        status_check_interval = self.config.STATUS_UPDATE_INTERVAL
        last_status_check = time.time()

        while self.running:
          try:
            current_time = time.time()

            # Check PTT timeout or if TX is muted
            # (_webmic_ptt_active was write-never dead state — removed)
            if self.ptt_active and not self.manual_ptt_mode:
                # Release PTT if timeout OR if TX is muted
                # (Don't keep PTT keyed when muted!)
                # But don't release if in manual PTT mode
                if current_time - self.last_sound_time > self.config.PTT_RELEASE_DELAY or self.tx_muted:
                    # Queue the HID write to the audio thread.  Clear ptt_active
                    # immediately so this block is not re-entered on the next tick.
                    self.ptt_active = False
                    self._pending_ptt_state = False
                    self._ptt_change_time = time.monotonic()

            # Manual PTT safety timeout. Manual mode deliberately bypasses
            # the auto-release above, but on a remotely-operated station a
            # dropped session mid-manual-TX means a stuck transmitter. The
            # bus auto-PTT path has a 180s failsafe; this is the manual
            # equivalent. MANUAL_PTT_MAX_SECS=0 disables it.
            _manual_max = float(getattr(self.config, 'MANUAL_PTT_MAX_SECS', 300))
            if self.ptt_active and self.manual_ptt_mode and _manual_max > 0:
                if not getattr(self, '_manual_ptt_keyed_at', 0):
                    self._manual_ptt_keyed_at = current_time
                elif current_time - self._manual_ptt_keyed_at > _manual_max:
                    print(f"\n[PTT] Manual PTT safety timeout after {_manual_max:.0f}s — forcing unkey")
                    self.notify(f"Manual PTT forced off after {_manual_max:.0f}s safety timeout", level='warning')
                    self._trace_events.append((time.monotonic(), 'ptt', 'manual_safety_timeout'))
                    self.manual_ptt_mode = False
                    self.ptt_active = False
                    self._pending_ptt_state = False
                    self._ptt_change_time = time.monotonic()
                    self._manual_ptt_keyed_at = 0
            else:
                self._manual_ptt_keyed_at = 0

            # Periodic status check and reporting (only if enabled)
            if status_check_interval > 0 and current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                
                # Decay RX level if no audio received recently
                time_since_rx_audio = current_time - self.last_rx_audio_time
                if time_since_rx_audio > 1.0:  # 1 second timeout
                    self.rx_audio_level = int(self.rx_audio_level * 0.5)  # Fast decay
                    if self.rx_audio_level < 5:
                        self.rx_audio_level = 0

                # Decay TX level (Radio → Mumble) — AIOC noise floor can
                # keep the bar stuck at a low level via 0.7/0.3 smoothing
                if self.tx_audio_level > 0:
                    self.tx_audio_level = int(self.tx_audio_level * 0.5)
                    if self.tx_audio_level < 3:
                        self.tx_audio_level = 0
                
                # Audio stream health — let plugin check its own watchdog
                if self.th9800_plugin:
                    self.th9800_plugin.check_watchdog()
            
            # Broadcastify stream health check (every 30s)
            so = getattr(self, 'stream_output', None)
            if (so and getattr(self.config, 'ENABLE_STREAM_OUTPUT', False) and
                    current_time - self._last_stream_health_check > 30):
                self._last_stream_health_check = current_time
                # `connected` alone is the handshake, not the stream: during
                # the 2026-08-21 uplink stall it stayed True through 2.5
                # minutes of zero throughput and this loop cheerfully printed
                # "Stream recovered" (and emailed it) five times. data_flowing
                # additionally requires that bytes actually reached the server
                # recently; it falls back to `connected` on an older stream
                # object that has no such property.
                _up = getattr(so, 'data_flowing', so.connected)
                if _up:
                    if not self._stream_was_connected:
                        self._stream_was_connected = True
                        print("  [Broadcastify] Stream healthy")
                    if self._stream_drop_alerted:
                        # Stream recovered after a drop
                        rc = getattr(so, '_reconnect_count', 0)
                        print(f"  [Broadcastify] Stream recovered (after {rc} reconnect attempts)")
                        self.notify("Broadcastify stream recovered", level='info')
                        self._send_stream_alert(
                            "Broadcastify stream recovered.",
                            subject="Broadcastify Stream Recovered")
                    self._stream_drop_alerted = False
                elif self._stream_was_connected and not self._stream_drop_alerted:
                    # Stream was connected but now it is not — which now
                    # includes a socket that is up but stalled. That is the
                    # point: a mount pushing nothing IS down.
                    self._stream_drop_alerted = True
                    rc = getattr(so, '_reconnect_count', 0)
                    print(f"  [Broadcastify] Stream dropped! (reconnect attempts: {rc})")
                    self.notify("Broadcastify stream dropped", level='error')
                    self._send_stream_alert(
                        "Broadcastify stream dropped and is reconnecting.",
                        subject="Broadcastify Stream Down")
                elif not so.connected and not so._was_connected and not getattr(so, '_reconnecting', False):
                    # Initial connection failed at startup (e.g. DNS was down) — keep retrying
                    print("  [Broadcastify] Retrying initial connection...")
                    so._reconnecting = True
                    def _retry_initial():
                        try:
                            so._connect()
                            if so.connected:
                                print("  [Broadcastify] Initial connection succeeded on retry")
                        except Exception as e:
                            print(f"  [Broadcastify] Retry failed: {e}")
                        finally:
                            so._reconnecting = False
                    threading.Thread(target=_retry_initial, daemon=True,
                                     name="Broadcastify-init-retry").start()

            # Charger relay schedule check
            # When manually overridden, wait until the schedule's *next* transition
            # (i.e. should_on flips to match the manual state) before resuming auto control
            if self.relay_charger:
                should_on = self._charger_should_be_on()
                if self._charger_manual:
                    # Manual override active — clear it once schedule agrees with current state
                    if should_on == self.relay_charger_on:
                        self._charger_manual = False
                elif should_on != self.relay_charger_on:
                    self.relay_charger.set_state(should_on)
                    self.relay_charger_on = should_on
                    on_str = str(self.config.RELAY_CHARGER_ON_TIME)
                    off_str = str(self.config.RELAY_CHARGER_OFF_TIME)
                    if should_on:
                        print(f"\n[Charger] CHARGING started (schedule {on_str}-{off_str})")
                    else:
                        print(f"\n[Charger] DRAINING started (schedule {on_str}-{off_str})")
                    self._trace_events.append((time.monotonic(), 'relay_charger', 'on' if should_on else 'off'))

            # SDR loopback watchdog check (covers both tuners in one call)
            if self.sdr_plugin and (self.sdr_plugin.tuner1_enabled
                                    or self.sdr_plugin.tuner2_enabled):
                self.sdr_plugin.check_watchdog()

            # Mumble Server health checks (every ~10 seconds)
            if not hasattr(self, '_ms_health_tick'):
                self._ms_health_tick = 0
            self._ms_health_tick += 1
            if self._ms_health_tick >= 100:  # ~10s at 0.1s sleep
                self._ms_health_tick = 0
                if self.mumble_server_1:
                    self.mumble_server_1.check_health()
                if self.mumble_server_2:
                    self.mumble_server_2.check_health()

            # Mumble client connection state change detection (debounced)
            mumble_alive = bool(self.mumble and self.mumble.is_alive()) if self.mumble else False
            if not hasattr(self, '_mumble_client_was_connected'):
                self._mumble_client_was_connected = mumble_alive
                self._mumble_state_since = time.monotonic()
            now_mono = time.monotonic()
            if mumble_alive != self._mumble_client_was_connected:
                # State changed — wait 3s before confirming (avoids flicker)
                if not hasattr(self, '_mumble_pending_state'):
                    self._mumble_pending_state = mumble_alive
                    self._mumble_state_since = now_mono
                elif self._mumble_pending_state != mumble_alive:
                    # Flickered back — cancel pending change
                    del self._mumble_pending_state
                elif now_mono - self._mumble_state_since >= 3.0:
                    # Stable for 3s — confirm the change
                    self._mumble_client_was_connected = mumble_alive
                    del self._mumble_pending_state
                    srv = getattr(self.config, 'MUMBLE_SERVER', '?')
                    port = getattr(self.config, 'MUMBLE_PORT', 64738)
                    if mumble_alive:
                        print(f"\n[Mumble] Connected to {srv}:{port}")
                    else:
                        print(f"\n[Mumble] Disconnected from {srv}:{port}")
            elif hasattr(self, '_mumble_pending_state'):
                # State went back to previous — cancel pending
                del self._mumble_pending_state

            time.sleep(0.1)
          except BaseException as _status_err:
            # Log crash so it's visible in the trace, then keep running.
            try:
                self._trace_events.append((time.monotonic(), 'STATUS_CRASH', str(_status_err)))
            except Exception:
                log_exc()  # trace deque itself failed — don't let that kill us
            time.sleep(1)
