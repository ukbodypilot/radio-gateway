"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""



class _EndpointMixin:
    def _find_endpoint(self, force=False):
        """Find the target endpoint name for mode commands.

        Selection order:
          1. PACKET_RADIO_ENDPOINT (exact name match), if set and present
          2. First link endpoint advertising capability 'packet': True
          3. Legacy fallback: first endpoint with plugin_type == 'aioc'
          4. Legacy fallback: endpoint matching PACKET_REMOTE_TNC by peer IP

        Caches result; pass force=True to re-scan.
        """
        if not force and self._cached_endpoint:
            if self._gateway and self._cached_endpoint in self._gateway.link_endpoints:
                return self._cached_endpoint
            self._cached_endpoint = None
        if not self._gateway or not self._gateway.link_server:
            return None
        endpoints = self._gateway.link_endpoints

        # 1. Explicit name override
        if self._endpoint_pref and self._endpoint_pref in endpoints:
            self._cached_endpoint = self._endpoint_pref
            return self._endpoint_pref

        # 2. Capability-based: any endpoint declaring it can host packet
        for name, src in endpoints.items():
            caps = getattr(src, '_endpoint_caps', {}) or {}
            if caps.get('packet'):
                self._cached_endpoint = name
                return name

        # 3. Legacy: plugin_type match (pre-capability endpoints)
        for name, src in endpoints.items():
            if getattr(src, 'plugin_type', None) == 'aioc':
                self._cached_endpoint = name
                return name

        # 4. Fallback: match by config IP if set
        if self._remote_tnc:
            for name in endpoints:
                ep = self._gateway.link_server._endpoints.get(name)
                if ep:
                    try:
                        if ep.sock.getpeername()[0] == self._remote_tnc:
                            self._cached_endpoint = name
                            return name
                    except Exception:
                        pass
        return None

    def list_packet_endpoints(self):
        """Return list of endpoint names that advertise packet capability.

        Used by the /packet page dropdown. Falls back to legacy AIOC detection
        for endpoints registered before the 'packet' capability existed.
        """
        if not self._gateway:
            return []
        names = []
        for name, src in self._gateway.link_endpoints.items():
            caps = getattr(src, '_endpoint_caps', {}) or {}
            if caps.get('packet') or getattr(src, 'plugin_type', None) == 'aioc':
                names.append(name)
        return names

    def set_endpoint_pref(self, name):
        """Update PACKET_RADIO_ENDPOINT preference at runtime and re-resolve.

        Pass '' to clear (auto-select). Returns the new resolved endpoint name
        (may be None if no packet-capable endpoint is connected).
        """
        self._endpoint_pref = (name or '').strip()
        self._cached_endpoint = None
        if self._config is not None:
            try:
                setattr(self._config, 'PACKET_RADIO_ENDPOINT', self._endpoint_pref)
            except Exception:
                pass
        return self._find_endpoint(force=True)

    def _get_endpoint_ip(self):
        """Get the IP address of the packet endpoint dynamically."""
        target = self._find_endpoint()
        if not target or not self._gateway or not self._gateway.link_server:
            return self._remote_tnc  # fallback to config
        ep = self._gateway.link_server._endpoints.get(target)
        if ep:
            try:
                return ep.sock.getpeername()[0]
            except Exception:
                pass
        return self._remote_tnc

    def _endpoint_has_local_tnc(self):
        """True if the selected packet endpoint advertises 'packet_local_tnc'.

        Such endpoints (e.g. IC-7100) run direwolf on the endpoint box and
        expose KISS over the network; the gateway-side packet_tnc must stay
        out of the way. AIOC endpoints leave this False; gateway-side
        direwolf claims the local hw:N,0.
        """
        target = self._find_endpoint()
        if not target or not self._gateway:
            return False
        src = self._gateway.link_endpoints.get(target)
        caps = getattr(src, '_endpoint_caps', {}) or {}
        return bool(caps.get('packet_local_tnc'))

    def _send_endpoint_mode(self, mode):
        """Send mode command to the remote AIOC endpoint via the link server.
        Returns True on success, False on failure.

        For mode='data' we include a TXDELAY value (Direwolf TXDELAY is in
        10ms units — 120 = 1200 ms of leading tone). 800 ms (the legacy
        hardcoded value on the IC-7100 endpoint) wasn't enough to give
        the radio's TX stage time to come up before the actual frame
        bits, so the start of the first frame in a session was clipped.
        Tune via PACKET_TXDELAY in gateway_config.txt.
        """
        target = self._find_endpoint(force=True)
        if not target:
            print(f"  [Packet] No AIOC endpoint found for packet radio")
            return False
        cmd = {
            'cmd': 'mode', 'mode': mode,
            'callsign': self._callsign, 'ssid': self._ssid,
            'modem': self._modem_rate, 'kiss_port': self._kiss_port,
        }
        if mode == 'data':
            cmd['txdelay'] = int(getattr(self._config, 'PACKET_TXDELAY', 120))
            # PTT-up settle delay: holds back the rigctld response so
            # Direwolf doesn't start sending audio until the radio's TX
            # stage is fully up. Defaults to 200 ms; tune via
            # PACKET_PTT_SETTLE_MS in gateway_config.txt.
            cmd['ptt_settle_ms'] = int(getattr(self._config, 'PACKET_PTT_SETTLE_MS', 200))
        try:
            self._gateway.link_server.send_command_to(target, cmd)
            print(f"  [Packet] Sent mode={mode} to endpoint '{target}'")
            return True
        except Exception as e:
            print(f"  [Packet] Failed to send mode to '{target}': {e}")
            return False

    def get_endpoint_status(self):
        """Return the actual endpoint mode/direwolf status from link layer."""
        target = self._find_endpoint()
        if not target or not self._gateway:
            return {"endpoint_name": None, "connected": False}
        st = self._gateway._link_last_status.get(target, {})
        # Plugins that own their own audio/data switch (e.g. IC-7100) expose
        # it as 'tnc_mode' so the radio's own operating-mode field can stay
        # in 'mode'. Older endpoints (AIOC) only have 'mode'.
        endpoint_mode = st.get('tnc_mode', st.get('mode', 'unknown'))
        src = self._gateway.link_endpoints.get(target)
        caps = getattr(src, '_endpoint_caps', {}) or {}
        local_tnc = bool(caps.get('packet_local_tnc'))
        return {
            "endpoint_name": target,
            "connected": True,
            "endpoint_mode": endpoint_mode,
            "direwolf_running": st.get('direwolf_running', False),
            "audio_input": st.get('audio_input', False),
            "audio_output": st.get('audio_output', False),
            "hid_connected": st.get('hid_connected', False),
            "ptt_active": st.get('ptt_active', False),
            # New fields surfaced for the UI to distinguish endpoints that
            # own their own TNC (IC-7100) from the AIOC pattern.
            "local_tnc": local_tnc,
            "ptt_method": ("rigctld (CI-V)" if local_tnc else "CM108"),
            "rigctld_listening": st.get('rigctld_listening', False),
            "rigctld_stats": st.get('rigctld_stats', {}),
            "direwolf_kiss_port": st.get('direwolf_kiss_port'),
        }

