"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""


from log_util import log_exc
from struct import Struct


class _PTTMixin:
    def set_ptt_state(self, state_on):
        """Control PTT — routes to the configured TX radio plugin."""
        tx_radio = str(getattr(self.config, 'TX_RADIO', 'th9800')).lower()
        # kv4p TX_RADIO values accepted: bare 'kv4p' (first connected kv4p
        # endpoint) or specific 'kv4p-vhf' / 'kv4p-uhf'. Dispatch via the
        # kv4p_endpoints helper so the call goes to the right loopback.
        if tx_radio == 'kv4p' or tx_radio.startswith('kv4p-'):
            from kv4p_endpoints import execute as _kv4p_execute
            instance = None if tx_radio == 'kv4p' else tx_radio[len('kv4p-'):]
            _kv4p_execute(self, {'cmd': 'ptt', 'state': state_on},
                          instance=instance)
        elif self.th9800_plugin:
            self.th9800_plugin.execute({'cmd': 'ptt', 'state': state_on})
        self.ptt_active = state_on
        try:
            import metrics as _m
            _m.bus_ptt_active.labels(bus=tx_radio).set(1 if state_on else 0)
        except Exception:
            log_exc()

    def _ptt_aioc(self, state_on):
        """PTT via AIOC HID GPIO.

        RTS controls a relay that connects the radio's TX serial line to either
        the USB dongle (USB Controlled) or the radio front panel (Radio Controlled).
        AIOC PTT requires Radio Controlled mode or PTT fails due to mic wiring.
        While Radio Controlled, CAT commands cannot be sent/received.
        """
        if not self.aioc_device:
            if state_on:
                self.notify("PTT failed: AIOC device not found")
            return
        _cat = getattr(self, 'cat_client', None)
        try:
            if state_on:
                # Switch RTS to Radio Controlled and pause CAT drain before keying
                if _cat:
                    _cat._pause_drain()
                    try:
                        _cat.set_rts(False)  # Radio Controlled
                    except Exception as e:
                        print(f"\n[PTT] RTS switch failed: {e}")
                        # drain stays paused — will be resumed on unkey
            state = 1 if state_on else 0
            iomask = 1 << (self.config.AIOC_PTT_CHANNEL - 1)
            iodata = state << (self.config.AIOC_PTT_CHANNEL - 1)
            data = Struct("<BBBBB").pack(0, 0, iodata, iomask, 0)
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (AIOC GPIO{self.config.AIOC_PTT_CHANNEL})")
            self.aioc_device.write(bytes(data))
            if not state_on:
                # Unkeyed — restore RTS to USB Controlled and resume CAT drain
                if _cat:
                    try:
                        _cat.set_rts(True)  # USB Controlled
                    except Exception as e:
                        print(f"\n[PTT] RTS restore failed: {e}")
                    finally:
                        _cat._drain_paused = False
        except Exception as e:
            print(f"\n[PTT] AIOC error: {e}")
            self.notify(f"PTT error: {e}")
            # Restore the serial relay to USB Controlled as well as resuming
            # the drain. Resuming the drain alone (the old behaviour) left
            # RTS in Radio Controlled after a failed key-on, so CAT was
            # effectively dead until the next successful key cycle.
            if _cat:
                try:
                    _cat.set_rts(True)  # USB Controlled
                except Exception as _re:
                    print(f"\n[PTT] RTS restore failed: {_re}")
                finally:
                    _cat._drain_paused = False

    def _ptt_relay(self, state_on):
        """PTT via CH340 USB relay."""
        if not self.relay_ptt:
            return
        self.relay_ptt.set_state(state_on)
        if self.config.VERBOSE_LOGGING:
            print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (relay)")

    def _ptt_software(self, state_on):
        """PTT via CAT TCP !ptt on/off command."""
        if not self.cat_client:
            if state_on:
                self.notify("PTT failed: CAT not connected")
            return
        try:
            self.cat_client._pause_drain()
            try:
                resp = self.cat_client._send_cmd("!ptt on" if state_on else "!ptt off")
            finally:
                self.cat_client._drain_paused = False
            if resp and 'serial not connected' in resp.lower():
                self.notify("PTT failed: radio serial not connected")
                return
            if resp is None:
                self.notify("PTT failed: no response from CAT server")
                return
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (software/CAT)")
        except Exception as e:
            print(f"\n[PTT] CAT !ptt error: {e}")
            self.notify(f"PTT failed: {e}")
    
    # D75 PTT is handled by the link endpoint — no local PTT code needed
    # KV4P PTT is also endpoint-hosted now: set_ptt_state() dispatches via
    # kv4p_endpoints.execute() when TX_RADIO is 'kv4p' / 'kv4p-vhf' / 'kv4p-uhf'.

