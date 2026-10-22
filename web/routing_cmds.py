"""Extracted from web_server.py during Phase 1.B.

These methods stay class-bound (the original code reads/writes plenty of
self.* state); composed back into ``WebConfigServer`` via inheritance.
Module-level helpers can land here too as the surface gets carved up
further.
"""

import os

from audio_util import AudioProcessor
from routing_rules import find_sink_conflicts, describe_conflicts


class _RoutingCmdsMixin:
    def _handle_routing_cmd(self, data):
        cmd = data.get('cmd', '')
        handler = self._ROUTING_CMD_DISPATCH.get(cmd)
        if not handler:
            return {'ok': False, 'error': f'unknown command: {cmd}'}
        busses, connections, _layout = self._load_routing_config()
        return handler(self, data, busses, connections)

    # ── Bus CRUD ──────────────────────────────────────────────────

    def _routing_cmd_add_bus(self, data, busses, connections):
        name = data.get('name', '').strip()
        bus_type = data.get('type', 'listen').strip()
        if not name:
            return {'ok': False, 'error': 'name required'}
        if bus_type not in ('listen', 'solo', 'duplex', 'simplex'):
            return {'ok': False, 'error': f'invalid type: {bus_type}'}
        bus_id = name.lower().replace(' ', '_')
        if any(b['id'] == bus_id for b in busses):
            return {'ok': False, 'error': f'bus "{bus_id}" already exists'}
        busses.append({'id': bus_id, 'name': name, 'type': bus_type, 'sources': [], 'sinks': []})
        self._save_routing_config(busses, connections)
        return {'ok': True}

    def _routing_cmd_rename_bus(self, data, busses, connections):
        bus_id = data.get('id', '')
        new_name = data.get('name', '').strip()
        if not new_name:
            return {'ok': False, 'error': 'name required'}
        for b in busses:
            if b['id'] == bus_id:
                b['name'] = new_name
                self._save_routing_config(busses, connections)
                return {'ok': True, 'name': new_name}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_delete_bus(self, data, busses, connections):
        bus_id = data.get('id', '')
        busses = [b for b in busses if b['id'] != bus_id]
        connections = [c for c in connections if c.get('from') != bus_id and c.get('to') != bus_id]
        self._save_routing_config(busses, connections)
        return {'ok': True}

    # ── Connections ───────────────────────────────────────────────

    def _routing_cmd_connect(self, data, busses, connections):
        source = data.get('source')
        bus = data.get('bus')
        sink = data.get('sink')

        if source and bus:
            conn = {'type': 'source-bus', 'from': source, 'to': bus}
            if conn not in connections:
                connections.append(conn)
                for b in busses:
                    if b['id'] == bus and source not in b.get('sources', []):
                        b.setdefault('sources', []).append(source)
            self._save_routing_config(busses, connections)
            return {'ok': True}

        elif bus and sink:
            conn = {'type': 'bus-sink', 'from': bus, 'to': sink}
            if conn not in connections:
                # Refuse BEFORE appending: a sink fed by two buses does not
                # mix, it interleaves or silently drops, and on a *_tx sink it
                # strands the radio unkeyed mid-transmission. See
                # routing_rules for the full mechanism.
                _conflicts = find_sink_conflicts(connections + [conn])
                if _conflicts:
                    return {'ok': False, 'error': describe_conflicts(_conflicts)}
                connections.append(conn)
                for b in busses:
                    if b['id'] == bus and sink not in b.get('sinks', []):
                        b.setdefault('sinks', []).append(sink)
            self._save_routing_config(busses, connections)
            return {'ok': True}

        return {'ok': False, 'error': 'specify source+bus or bus+sink'}

    def _routing_cmd_disconnect(self, data, busses, connections):
        source = data.get('source')
        bus = data.get('bus')
        sink = data.get('sink')

        if source and bus:
            connections = [c for c in connections
                           if not (c['type'] == 'source-bus' and c['from'] == source and c['to'] == bus)]
            for b in busses:
                if b['id'] == bus:
                    b['sources'] = [s for s in b.get('sources', []) if s != source]
        elif bus and sink:
            connections = [c for c in connections
                           if not (c['type'] == 'bus-sink' and c['from'] == bus and c['to'] == sink)]
            for b in busses:
                if b['id'] == bus:
                    b['sinks'] = [s for s in b.get('sinks', []) if s != sink]

        self._save_routing_config(busses, connections)
        return {'ok': True}

    def _routing_cmd_save_all(self, data, busses, connections):
        """Wholesale save from Drawflow — replace connections, update bus
        sources/sinks, reload bus manager, reset stale sink levels."""
        new_connections = data.get('connections', [])
        bus_updates = data.get('bus_updates', {})
        layout = data.get('layout')
        # The authoritative gate. The editor blocks these at drag time, but
        # this path is also reachable from the HTTP API and a hand-edited
        # graph, and nothing downstream survives the conflict gracefully.
        _conflicts = find_sink_conflicts(new_connections)
        if _conflicts:
            print(f"  [routing] REFUSED save: {describe_conflicts(_conflicts)}")
            return {'ok': False, 'error': describe_conflicts(_conflicts)}
        for b in busses:
            upd = bus_updates.get(b['id'], {})
            b['sources'] = upd.get('sources', [])
            b['sinks'] = upd.get('sinks', [])
        self._save_routing_config(busses, new_connections, layout=layout)

        if self.gateway and hasattr(self.gateway, 'bus_manager') and self.gateway.bus_manager:
            try:
                self.gateway.bus_manager.reload()
                self.gateway._bus_stream_flags = self.gateway.bus_manager.get_bus_stream_flags()
                self.gateway._bus_sinks = self.gateway.bus_manager.get_bus_sinks()
                self.gateway._listen_bus_id = self.gateway.bus_manager.get_listen_bus_id()
            except Exception as e:
                return {'ok': True, 'warning': f'saved but reload failed: {e}'}

        # Reset stale sink levels so muted/disconnected sinks read 0
        if self.gateway:
            self.gateway.speaker_audio_level = 0
            self.gateway.stream_audio_level = 0
            self.gateway.mumble_tx_level = 0
            if getattr(self.gateway, 'th9800_plugin', None):
                self.gateway.th9800_plugin.tx_audio_level = 0
            if self.gateway.kv4p_plugin:
                self.gateway.kv4p_plugin.tx_audio_level = 0
        if self.gateway and getattr(self.gateway, 'bus_manager', None):
            try:
                self.gateway.bus_manager.sync_listen_bus()
            except Exception as e:
                print(f"  [routing] sync_listen_bus error: {e}")

        _bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
        if _bm:
            _bus_ids = list(_bm._busses.keys())
            _sink_map = getattr(self.gateway, '_bus_sinks', {})
            print(f"  [routing] Saved & reloaded: busses={_bus_ids} sinks={dict(_sink_map)}")
        return {'ok': True}

    # ── Per-bus processing settings (D-stack toggles + params) ────────

    def _routing_cmd_toggle_proc(self, data, busses, connections):
        bus_id = data.get('bus', '')
        filt = data.get('filter', '')
        if filt not in ('gate', 'hpf', 'lpf', 'notch', 'dfn', 'pcm', 'mp3', 'vad', 'loop'):
            return {'ok': False, 'error': f'invalid filter: {filt}'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc[filt] = not proc.get(filt, False)
                self._save_routing_config(busses, connections)
                # Update cached stream flags on gateway + BusManager
                if filt in ('pcm', 'mp3', 'vad', 'loop') and self.gateway:
                    flags = getattr(self.gateway, '_bus_stream_flags', {})
                    bus_flags = flags.setdefault(bus_id, {'pcm': False, 'mp3': False, 'vad': False})
                    bus_flags[filt] = proc[filt]
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                # Stop loop recorder when toggled off
                if filt == 'loop' and not proc[filt] and self.gateway:
                    _lr = getattr(self.gateway, 'loop_recorder', None)
                    if _lr:
                        _lr.stop(bus_id)
                if bm:
                    if bus_id in bm._bus_config:
                        bm._bus_config[bus_id][filt] = proc[filt]
                    # Update the live AudioProcessor (create if needed)
                    if filt in ('gate', 'hpf', 'lpf', 'notch', 'dfn'):
                        _bp = bm._bus_processors.get(bus_id)
                        if not _bp:
                            _bp = AudioProcessor(f"bus_{bus_id}", self.gateway.config)
                            bm._bus_processors[bus_id] = _bp
                        setattr(_bp, 'enable_noise_gate' if filt == 'gate' else f'enable_{filt}', proc[filt])
                return {'ok': True, 'state': proc[filt]}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_dfn_mix(self, data, busses, connections):
        bus_id = data.get('bus', '')
        try:
            mix = max(0.0, min(1.0, float(data.get('mix', 0.5))))
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'invalid mix value'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc['dfn_mix'] = mix
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm:
                    if bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['dfn_mix'] = mix
                    _bp = bm._bus_processors.get(bus_id)
                    if _bp is not None:
                        _bp.dfn_mix = mix
                return {'ok': True, 'mix': mix}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_dfn_atten(self, data, busses, connections):
        # DFN attenuation cap in dB. 0 = model decides (can pump);
        # 15–25 is typical real-world range. Clamped to [0, 60].
        bus_id = data.get('bus', '')
        try:
            atten = max(0.0, min(60.0, float(data.get('atten_db', 18.0))))
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'invalid atten_db value'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc['dfn_atten_db'] = atten
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm:
                    if bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['dfn_atten_db'] = atten
                    _bp = bm._bus_processors.get(bus_id)
                    if _bp is not None:
                        _bp.dfn_atten_db = atten
                return {'ok': True, 'atten_db': atten}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_dfn_bypass(self, data, busses, connections):
        # Denoise bypass threshold in dBFS: chunks below this RMS skip the
        # denoise worker (CPU saver). Clamped to [-90, -20]; -60 is the
        # historical default.
        bus_id = data.get('bus', '')
        try:
            bypass = max(-90.0, min(-20.0, float(data.get('bypass_db', -60.0))))
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'invalid bypass_db value'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc['dfn_bypass_db'] = bypass
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm:
                    if bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['dfn_bypass_db'] = bypass
                    _bp = bm._bus_processors.get(bus_id)
                    if _bp is not None:
                        _bp.dfn_bypass_db = bypass
                return {'ok': True, 'bypass_db': bypass}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_bus_delay(self, data, busses, connections):
        bus_id = data.get('bus', '')
        try:
            delay_ms = max(0, min(5000, int(round(float(data.get('delay_ms', 0))))))
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'invalid delay_ms'}
        for b in busses:
            if b['id'] == bus_id:
                b.setdefault('processing', {})['delay_ms'] = delay_ms
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm and bus_id in bm._bus_config:
                    bm._bus_config[bus_id]['delay_ms'] = delay_ms
                return {'ok': True, 'delay_ms': delay_ms}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_dfn_engine(self, data, busses, connections):
        # Per-bus denoise engine — 'rnnoise' | 'deepfilternet'.
        # AudioProcessor.set_dfn_engine drops the current stream so the
        # next audio chunk rebuilds with the new engine.
        from audio_util import DENOISE_ENGINE_IDS
        bus_id = data.get('bus', '')
        engine = str(data.get('engine', ''))
        if engine not in DENOISE_ENGINE_IDS:
            return {'ok': False,
                    'error': f'invalid engine; must be one of {list(DENOISE_ENGINE_IDS)}'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc['dfn_engine'] = engine
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm:
                    if bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['dfn_engine'] = engine
                    _bp = bm._bus_processors.get(bus_id)
                    if _bp is not None:
                        _bp.set_dfn_engine(engine)
                return {'ok': True, 'engine': engine}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_set_loop_hours(self, data, busses, connections):
        bus_id = data.get('bus', '')
        hours = data.get('hours', 24)
        try:
            hours = max(1, min(168, int(hours)))  # 1h to 7 days
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'invalid hours value'}
        for b in busses:
            if b['id'] == bus_id:
                proc = b.setdefault('processing', {})
                proc['loop_hours'] = hours
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm and bus_id in bm._bus_config:
                    bm._bus_config[bus_id]['loop_hours'] = hours
                lr = getattr(self.gateway, 'loop_recorder', None)
                if lr:
                    lr.set_retention(bus_id, hours)
                return {'ok': True, 'hours': hours}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    def _routing_cmd_bus_mute(self, data, busses, connections):
        bus_id = data.get('bus', '')
        for b in busses:
            if b['id'] == bus_id:
                b['muted'] = not b.get('muted', False)
                self._save_routing_config(busses, connections)
                bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                if bm and bus_id in bm._bus_config:
                    bm._bus_config[bus_id]['muted'] = b['muted']
                # Update primary listen bus mute cache
                if self.gateway and bus_id == getattr(self.gateway, '_listen_bus_id', None):
                    self.gateway._listen_bus_muted = b['muted']
                return {'ok': True, 'muted': b['muted']}
        return {'ok': False, 'error': f'bus not found: {bus_id}'}

    # ── Source/sink mute, gain, speaker ───────────────────────────

    def _routing_cmd_mute(self, data, busses, connections):
        target_id = data.get('id', '')
        # NUL sink is permanently muted — ignore toggle.
        if target_id == 'nul':
            return {'ok': True, 'muted': True}
        # Sinks without a plugin object — toggle a separate gateway flag
        _sink_ids = ('speaker', 'broadcastify', 'broadcastify_l', 'broadcastify_r',
                      'mumble', 'remote_audio_tx')
        if target_id in _sink_ids and self.gateway:
            muted_sinks = getattr(self.gateway, '_muted_sinks', set())
            if target_id in muted_sinks:
                muted_sinks.discard(target_id)
                muted = False
            else:
                muted_sinks.add(target_id)
                muted = True
            self.gateway._muted_sinks = muted_sinks
            return {'ok': True, 'muted': muted}
        plugin = self._get_plugin_by_id(target_id)
        if plugin:
            # TX and RX share one plugin object but have independent
            # signal paths — toggle a separate flag for *_tx sinks so
            # muting TX doesn't silence RX (and vice versa).
            if target_id.endswith('_tx'):
                plugin.tx_muted = not getattr(plugin, 'tx_muted', False)
                return {'ok': True, 'muted': plugin.tx_muted}
            plugin.muted = not getattr(plugin, 'muted', False)
            return {'ok': True, 'muted': plugin.muted}
        return {'ok': False, 'error': f'unknown source/sink: {target_id}'}

    def _routing_cmd_gain(self, data, busses, connections):
        target_id = data.get('id', '')
        value = int(data.get('value', 100))
        _gw = self.gateway
        plugin = self._get_plugin_by_id(target_id)
        if plugin:
            _is_tx = target_id.endswith('_tx')
            if _is_tx and hasattr(plugin, 'tx_audio_boost'):
                plugin.tx_audio_boost = value / 100.0
            else:
                plugin.audio_boost = value / 100.0
            # Persist link endpoint gains
            _ep_name = getattr(plugin, 'endpoint_name', '')
            if _ep_name and _gw:
                _key = 'tx_boost' if _is_tx else 'rx_boost'
                settings = _gw.link_endpoint_settings.setdefault(_ep_name, {})
                settings[_key] = value
                _gw._save_link_settings()
            if _gw:
                _gw._source_gains[target_id] = value
                _gw._save_source_gains()
            return {'ok': True, 'gain': value}
        # Passive sinks (mumble, broadcastify, speaker, etc.) — no plugin
        _passive_sinks = ('mumble', 'broadcastify', 'broadcastify_l', 'broadcastify_r', 'speaker',
                          'transcription', 'remote_audio_tx')
        if target_id in _passive_sinks and _gw:
            _gw._sink_gains[target_id] = value / 100.0
            _gw._source_gains[target_id] = value
            _gw._save_source_gains()
            return {'ok': True, 'gain': value}
        return {'ok': False, 'error': f'unknown source/sink: {target_id}'}

    def _routing_cmd_speaker_mode(self, data, busses, connections):
        mode = data.get('mode', 'virtual').lower()
        if mode not in ('virtual', 'auto', 'real'):
            return {'ok': False, 'error': f'invalid mode: {mode}'}
        gw = self.gateway
        if not gw:
            return {'ok': False, 'error': 'gateway not ready'}
        gw.config.SPEAKER_MODE = mode
        if mode == 'virtual':
            # Close existing real stream
            if gw.speaker_stream:
                try:
                    if gw.speaker_stream.is_active():
                        gw.speaker_stream.stop_stream()
                    gw.speaker_stream.close()
                except Exception:
                    pass
                gw.speaker_stream = None
                gw.speaker_queue = None
                print(f"  [Speaker] Switched to virtual (metering only)")
            return {'ok': True, 'mode': mode, 'device': None}
        # auto / real — try to open device
        if not gw.speaker_stream:
            gw.open_speaker_output()
        _dev = 'connected' if gw.speaker_stream else 'virtual (fallback)'
        return {'ok': True, 'mode': mode if gw.speaker_stream else 'virtual', 'device': _dev}

    # ── Dispatch table — add a command by writing a method above ──
    _ROUTING_CMD_DISPATCH = {
        'add_bus':         _routing_cmd_add_bus,
        'rename_bus':      _routing_cmd_rename_bus,
        'delete_bus':      _routing_cmd_delete_bus,
        'connect':         _routing_cmd_connect,
        'disconnect':      _routing_cmd_disconnect,
        'save_all':        _routing_cmd_save_all,
        'toggle_proc':     _routing_cmd_toggle_proc,
        'set_dfn_mix':     _routing_cmd_set_dfn_mix,
        'set_dfn_atten':   _routing_cmd_set_dfn_atten,
        'set_dfn_bypass':  _routing_cmd_set_dfn_bypass,
        'set_dfn_engine':  _routing_cmd_set_dfn_engine,
        'set_bus_delay':   _routing_cmd_set_bus_delay,
        'set_loop_hours':  _routing_cmd_set_loop_hours,
        'bus_mute':        _routing_cmd_bus_mute,
        'mute':            _routing_cmd_mute,
        'gain':            _routing_cmd_gain,
        'speaker_mode':    _routing_cmd_speaker_mode,
    }

    def _get_plugin_by_id(self, id):
        """Resolve a source/sink ID to the corresponding plugin/source object."""
        gw = self.gateway
        if not gw:
            return None
        _sdr = gw.sdr_plugin
        _map = {
            'sdr': _sdr,
            'sdr1': getattr(_sdr, '_tuner1', None) if _sdr else None,
            'sdr2': getattr(_sdr, '_tuner2', None) if _sdr else None,
            # kv4p endpoints are looked up via gw.link_endpoints (kv4p_vhf,
            # kv4p_uhf, ...) — the legacy bare 'kv4p' id is gone.
            'aioc': getattr(gw, 'th9800_plugin', None),
            'aioc_tx': getattr(gw, 'th9800_plugin', None),
            'playback': getattr(gw, 'playback_source', None),
            'bgm': getattr(gw, 'bgm_source', None),
            'announcer': getattr(gw, 'announcer_source', None),
            'loop_playback': getattr(gw, 'loop_playback_source', None),
            'webmic': getattr(gw, 'web_mic_source', None),
            'announce': getattr(gw, 'announce_input_source', None),
            'monitor': getattr(gw, 'web_monitor_source', None),
            'mumble_rx': getattr(gw, 'mumble_source', None),
            'remote_audio': getattr(gw, 'remote_audio_source', None),
        }
        result = _map.get(id)
        # Link endpoint lookup by source_id or sink_id
        if result is None:
            for name, src in gw.link_endpoints.items():
                if getattr(src, 'source_id', None) == id or getattr(src, 'sink_id', None) == id:
                    return src
        return result

    _ROUTING_CONFIG_PATH = None

    def _routing_config_path(self):
        if not self._ROUTING_CONFIG_PATH:
            import os
            self._ROUTING_CONFIG_PATH = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'routing_config.json')
        return self._ROUTING_CONFIG_PATH

    def _load_routing_config(self):
        """Load bus config from JSON file. Returns (busses, connections, layout)."""
        import json
        path = self._routing_config_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get('busses', []), data.get('connections', []), data.get('layout')
            except Exception as e:
                print(f"  [Routing] Failed to load config from {path}: {e}")
        else:
            print(f"  [Routing] Config not found at {path} — returning empty routing")
        return [], [], None

    def _save_routing_config(self, busses, connections, layout=None):
        """Save bus config to JSON file."""
        import json
        path = self._routing_config_path()
        try:
            data = {'busses': busses, 'connections': connections}
            if layout:
                data['layout'] = layout
            else:
                # Preserve existing layout if not provided
                try:
                    with open(path) as f:
                        old = json.load(f)
                    if 'layout' in old:
                        data['layout'] = old['layout']
                except Exception:
                    pass
            # Atomic write — a crash mid-save (or a reader hitting the file
            # between truncate and write) would otherwise corrupt the config
            # and BusManager would boot with zero buses.
            from atomic_json import save_json
            save_json(path, data)
        except Exception as e:
            print(f"  [Routing] Failed to save config: {e}")
