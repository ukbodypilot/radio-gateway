"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""

import threading


class _ModeMixin:
    def _tnc_status_fields(self):
        """Pull direwolf state from gateway.packet_tnc for status dicts."""
        tnc = getattr(self._gateway, 'packet_tnc', None) if self._gateway else None
        if not tnc:
            return {"dw_audio_level": 0, "dw_audio_peak": 0, "log_tail": []}
        s = tnc.status()
        return {
            "dw_audio_level": s.get('audio_level', 0),
            "dw_audio_peak": s.get('audio_peak', 0),
            "log_tail": s.get('log_tail', []),
        }

    def _set_mode(self, mode):
        """Switch TNC mode — tells endpoint to start/stop Direwolf.

        Targets: 'idle' | 'aprs' | 'winlink' | 'bbs'.

        Endpoint-side responsibilities:
          * AIOC-style endpoints: gateway owns Direwolf locally (the
            endpoint's audio is fed across the link to the gateway box).
          * IC-7100-style endpoints: endpoint runs Direwolf where the
            USB codec lives; gateway just connects KISS over the network.
            ``packet_local_tnc`` capability is the discriminator.

        Drives the state machine in ``packet/state.py``: target is set
        synchronously; phase moves STARTING → STEADY as KISS + Pat come
        up in the background, or → ERROR with ``last_error`` if a step
        fails. ``state_snapshot()`` exposes the full triple to the UI.
        """
        from packet.state import (
            TARGET_IDLE, PHASE_STARTING, PHASE_STOPPING,
            STEP_SEND_ENDPOINT_MODE, STEP_START_LOCAL_TNC,
            STEP_CONNECT_KISS, STEP_STOP_PAT, STEP_STOP_LOCAL_TNC,
        )

        if mode not in ('idle', 'aprs', 'winlink', 'bbs'):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if mode == self._mode:
            return {"ok": True, "mode": self._mode}

        print(f"  [Packet] Mode: {self._mode} -> {mode}")

        # Disconnect current KISS regardless of next state.
        self._disconnect_kiss()

        # Compute once — was called twice with risk of drifting answers if
        # the endpoint set changes mid-call.
        endpoint_owns_tnc = self._endpoint_has_local_tnc()
        gw_tnc = getattr(self._gateway, 'packet_tnc', None) if not endpoint_owns_tnc else None

        if mode == 'idle':
            self._advance(target=mode, phase=PHASE_STOPPING, step=STEP_STOP_PAT)
            self._stop_pat()
            # Stop gateway-side direwolf BEFORE releasing the endpoint's
            # ALSA — the order matters so direwolf doesn't briefly clutch
            # at a soon-to-be-shared device. Endpoint-owned TNCs (IC-7100)
            # reap their own direwolf when we tell them to go to audio.
            if gw_tnc is not None:
                self._advance(step=STEP_STOP_LOCAL_TNC)
                gw_tnc.stop()
            self._advance(step=STEP_SEND_ENDPOINT_MODE)
            if not self._send_endpoint_mode('audio'):
                self._fail("mode set to idle but failed to send audio command to endpoint")
                return {"ok": False, "mode": "idle",
                        "warning": "mode set to idle but failed to send audio command to endpoint"}
            self._reach_steady()
            return {"ok": True, "mode": "idle"}

        if not self._find_endpoint() and not self._remote_tnc:
            self._advance(target=TARGET_IDLE)
            self._fail("No AIOC endpoint connected for packet radio")
            return {"ok": False, "error": "No AIOC endpoint connected for packet radio"}

        # Synchronous startup steps — each updates the state machine.
        self._advance(target=mode, phase=PHASE_STARTING, step=STEP_SEND_ENDPOINT_MODE)
        if not self._send_endpoint_mode('data'):
            self._advance(target=TARGET_IDLE)
            self._fail("failed to send data mode command to endpoint")
            return {"ok": False, "error": "failed to send data mode command to endpoint"}
        if gw_tnc is not None:
            self._advance(step=STEP_START_LOCAL_TNC)
            gw_tnc.start(callsign=self._callsign, ssid=self._ssid,
                         modem=self._modem_rate, kiss_port=self._kiss_port,
                         ptt_channel=int(getattr(self._config, 'AIOC_PTT_CHANNEL', 3)))
        # Async startup steps. Threads update the state machine when they
        # succeed or fail — the connect loop calls _on_kiss_connected /
        # _on_kiss_failed; _delayed_pat_start calls _reach_steady or _fail.
        self._advance(step=STEP_CONNECT_KISS)
        threading.Thread(target=self._kiss_connect_loop, daemon=True,
                         name="KISSConnect").start()
        if mode == 'winlink':
            threading.Thread(target=self._delayed_pat_start, daemon=True,
                             name="PatStart").start()
        else:
            # APRS / BBS: success criterion is just KISS connected; Pat is
            # winlink-only. The KISS connect-loop will call _reach_steady
            # on the first successful connect.
            pass
        return {"ok": True, "mode": mode}

