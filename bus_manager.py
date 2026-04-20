"""Bus Manager — manages ALL audio buses including the primary listen bus.

All buses (listen, solo, duplex, simplex) are created and ticked by
BusManager in a dedicated daemon thread.  The main audio loop in
gateway_core handles SDR rebroadcast TX and WebSocket push, draining
audio from BusManager queues.
"""

import collections
import gc
import json
import math
import os
import threading
import time

import numpy as np

from audio_bus import SoloBus, DuplexRepeaterBus, SimplexRepeaterBus, ListenBus, mix_audio_streams
from audio_util import AudioProcessor, pcm_level, apply_gain


class BusManager:
    """Manages additional audio busses from routing_config.json."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.config = gateway.config
        self._busses = {}          # id → AudioBus instance
        self._bus_processors = {}  # id → AudioProcessor instance
        self._bus_config = {}      # id → bus config dict (processing, pcm, mp3, vad)
        self._running = False
        self._thread = None
        self._config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'routing_config.json')
        self._tick_interval = 0.05  # 50ms = 20 ticks/sec (matches main loop)
        # Shared PCM/MP3 buffer — BusManager deposits mixed audio here,
        # main loop picks it up and mixes with listen bus output before pushing.
        self._pcm_queue = collections.deque(maxlen=8)  # thread-safe bounded deque
        self._mp3_queue = collections.deque(maxlen=8)
        # Per-tick staging — collected during each bus's _deliver_audio, mixed
        # and flushed once at end of tick. Prevents multiple buses routed to
        # pcm/mp3 from interleaving their chunks into the downstream consumer.
        # Invariant: mutated only from the single bus-tick thread. Do not
        # append or clear from HTTP handlers, link readers, or any other thread.
        self._pcm_tick = []
        self._mp3_tick = []
        self._bus_levels = {}       # bus_id → audio level (0-100) for routing page

        # ── Primary listen bus state ──────────────────────────────────────
        self.listen_bus = None              # Primary ListenBus instance
        self._listen_bus_id = None          # Primary listen bus ID from routing config
        self._sdr_rebroadcast_queue = collections.deque(maxlen=4)  # (sdr_only_pcm, ptt_required)
        self._listen_vad_pass = True        # VAD state, computed each tick

        # ── Audio quality diagnostics ──────────────────────────────────────
        self._bm_tick_count = 0           # tick counter for cross-clock correlation
        self._bm_tick_mono = (0, 0.0)     # (tick_number, monotonic_time) at last tick
        self._tick_trace = collections.deque(maxlen=6000)  # per-tick timing records
        self._gc_events = collections.deque(maxlen=200)    # GC pause records

    def get_bus_sinks(self):
        """Return per-bus connected sink IDs from routing config.

        Returns dict: {bus_id: set of sink_ids}
        """
        sinks = {}
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return sinks
        for bus_cfg in data.get('busses', []):
            sinks[bus_cfg['id']] = set()
        for c in data.get('connections', []):
            if c['type'] == 'bus-sink':
                bus_id = c['from']
                if bus_id in sinks:
                    sinks[bus_id].add(c['to'])
        return sinks

    def drain_pcm(self):
        """Return and clear the accumulated PCM audio from non-listen busses."""
        if not self._pcm_queue:
            return None
        from audio_bus import additive_mix
        # Drain deque atomically — popleft is thread-safe on CPython
        chunks = []
        while self._pcm_queue:
            try:
                chunks.append(self._pcm_queue.popleft())
            except IndexError:
                break
        if not chunks:
            return None
        # DIAG: log when we drain multiple chunks (indicates clock drift)
        if not hasattr(self, '_pcm_drain_count'):
            self._pcm_drain_count = 0
            self._pcm_drain_multi = 0
            self._pcm_drain_zero = 0
            self._pcm_drain_diag = time.time()
        self._pcm_drain_count += 1
        if len(chunks) > 1:
            self._pcm_drain_multi += 1
        self._last_pcm_drain_n = len(chunks)  # exposed for trace
        if time.time() - self._pcm_drain_diag > 10.0:
            if getattr(self.config, 'VERBOSE_LOGGING', False):
                print(f"  [PCM-DIAG] drains={self._pcm_drain_count} multi={self._pcm_drain_multi} ({self._pcm_drain_multi*100//max(1,self._pcm_drain_count)}% double)")
            self._pcm_drain_count = 0
            self._pcm_drain_multi = 0
            self._pcm_drain_diag = time.time()
        return additive_mix(chunks)

    def drain_mp3(self):
        """Return and clear the accumulated MP3 audio from non-listen busses."""
        if not self._mp3_queue:
            return None
        from audio_bus import additive_mix
        chunks = []
        while self._mp3_queue:
            try:
                chunks.append(self._mp3_queue.popleft())
            except IndexError:
                break
        return additive_mix(chunks) if chunks else None

    def drain_sdr_rebroadcast(self):
        """Return (sdr_only_audio, ptt_required) for SDR rebroadcast, or (None, False)."""
        if not self._sdr_rebroadcast_queue:
            return None, False
        # Take the most recent entry (discard older ones)
        sdr_audio = None
        ptt = False
        while self._sdr_rebroadcast_queue:
            try:
                sdr_audio, ptt = self._sdr_rebroadcast_queue.popleft()
            except IndexError:
                break
        return sdr_audio, ptt

    def get_listen_bus_id(self):
        """Return the ID of the first listen-type bus in routing config."""
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return 'listen'
        for bus_cfg in data.get('busses', []):
            if bus_cfg.get('type') == 'listen':
                return bus_cfg['id']
        return 'listen'

    def is_bus_muted(self, bus_id):
        """Check if a bus is muted."""
        return self._bus_config.get(bus_id, {}).get('muted', False)

    def get_bus_processing(self, bus_id):
        """Return processing config dict for a bus from the routing JSON."""
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        for bus_cfg in data.get('busses', []):
            if bus_cfg.get('id') == bus_id:
                return bus_cfg.get('processing', {})
        return {}

    def get_bus_stream_flags(self):
        """Return per-bus stream flags from routing config.

        Returns dict: {bus_id: {'pcm': bool, 'mp3': bool, 'vad': bool}}
        Includes ALL busses (including 'listen' type).
        """
        flags = {}
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return flags
        for bus_cfg in data.get('busses', []):
            bus_id = bus_cfg['id']
            proc = bus_cfg.get('processing', {})
            flags[bus_id] = {
                'pcm': proc.get('pcm', False),
                'mp3': proc.get('mp3', False),
                'vad': proc.get('vad', False),
            }
        return flags

    def sync_listen_bus(self):
        """Reconcile primary listen bus sources with routing config.

        Called at startup, after routing config save, and when a new
        source becomes available (e.g. link endpoint connects).
        """
        if not self.listen_bus:
            return
        gw = self.gateway
        import re as _re

        # Build source_map: source_id → (plugin, priority, duckable)
        source_map = {}
        if gw.sdr_plugin:
            _sdr = gw.sdr_plugin
            _has_tuners = False
            # Register tuner captures as separate source nodes for independent routing
            if getattr(_sdr, '_tuner1', None):
                source_map['sdr1'] = (_sdr._tuner1, 11, getattr(gw.config, 'SDR_DUCK', True))
                _has_tuners = True
            if getattr(_sdr, '_tuner2', None):
                source_map['sdr2'] = (_sdr._tuner2, 11, getattr(gw.config, 'SDR_DUCK', True))
                _has_tuners = True
            # Only register combined 'sdr' if no individual tuners (backward compat)
            if not _has_tuners:
                source_map['sdr'] = (_sdr, 11, getattr(gw.config, 'SDR_DUCK', True))
        if getattr(gw, 'th9800_plugin', None):
            source_map['aioc'] = (gw.th9800_plugin, 1, False)
        if gw.kv4p_plugin:
            source_map['kv4p'] = (gw.kv4p_plugin,
                                  int(getattr(gw.config, 'KV4P_AUDIO_PRIORITY', 2)) + 10,
                                  getattr(gw.config, 'KV4P_AUDIO_DUCK', True))
        if getattr(gw, 'playback_source', None):
            source_map['playback'] = (gw.playback_source, 0, False)
        if getattr(gw, 'loop_playback_source', None):
            source_map['loop_playback'] = (gw.loop_playback_source, 10, True)
        if getattr(gw, 'web_mic_source', None):
            source_map['webmic'] = (gw.web_mic_source, 0, False)
        if getattr(gw, 'announce_input_source', None):
            source_map['announce'] = (gw.announce_input_source, 0, False)
        if getattr(gw, 'web_monitor_source', None):
            source_map['monitor'] = (gw.web_monitor_source, 5, False)
        if getattr(gw, 'mumble_source', None):
            source_map['mumble_rx'] = (gw.mumble_source, 0, False)
        if getattr(gw, 'remote_audio_source', None):
            source_map['remote_audio'] = (gw.remote_audio_source,
                                          int(getattr(gw.config, 'REMOTE_AUDIO_PRIORITY', 2)) + 10,
                                          getattr(gw.config, 'REMOTE_AUDIO_DUCK', True))
        if getattr(gw, 'echolink_source', None):
            source_map['echolink'] = (gw.echolink_source, 2, False)
        # Link endpoints — use pre-computed source_id from registration
        for name, src in gw.link_endpoints.items():
            _sid = getattr(src, 'source_id', None) or _re.sub(r'[^a-z0-9_]', '_', name.lower())
            source_map[_sid] = (src,
                                int(getattr(gw.config, 'LINK_AUDIO_PRIORITY', 3)) + 10,
                                getattr(gw.config, 'LINK_AUDIO_DUCK', False))
        # External plugins (auto-discovered from plugins/)
        for pid, plugin in getattr(gw, '_external_plugins', {}).items():
            _prio = getattr(plugin, 'priority', 5)
            _duck = getattr(plugin, 'duck', True) if not getattr(plugin, 'ptt_control', False) else False
            source_map[pid] = (plugin, _prio, _duck)

        # Read routing config to find which sources connect to listen bus
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        should_be_on = set()
        for c in data.get('connections', []):
            if c['type'] == 'source-bus' and c['to'] == self._listen_bus_id:
                if c['from'] in source_map:
                    should_be_on.add(c['from'])

        # Reconcile SDR tuner parec lifecycle with routing:
        # an unrouted tuner keeps parec running and overflows its queue forever,
        # wasting CPU and competing with routed tuners for pipewire scheduling.
        if gw.sdr_plugin:
            for _sid, _attr in (('sdr1', '_tuner1'), ('sdr2', '_tuner2')):
                _tuner = getattr(gw.sdr_plugin, _attr, None)
                if _tuner is None:
                    continue
                _needed = _sid in should_be_on
                if _needed and not _tuner.active:
                    if _tuner.setup():
                        _tuner._stream_trace = getattr(gw, '_stream_trace', None)
                        print(f"  [sync] Started SDR tuner capture: {_sid}")
                    else:
                        print(f"  [sync] Failed to start SDR tuner capture: {_sid}")
                elif not _needed and _tuner.active:
                    _tuner.cleanup()
                    print(f"  [sync] Stopped SDR tuner capture: {_sid} (not routed)")

        # Current sources on listen bus
        _before = {s.source.name for s in self.listen_bus.source_slots}

        # Add missing
        for sid in should_be_on:
            plugin, prio, duck = source_map[sid]
            if plugin.name not in _before:
                _det = getattr(plugin, 'ptt_control', False)
                self.listen_bus.add_source(plugin, bus_priority=prio, duckable=duck, deterministic=_det, routing_id=sid)
                print(f"  [sync] Added {sid} to listen bus (prio={prio} duck={duck} det={_det})")

        # Remove extras (only for sources we manage)
        for sid, (plugin, _, _) in source_map.items():
            if sid not in should_be_on and plugin.name in _before:
                self.listen_bus.remove_source(plugin.name)
                print(f"  [sync] Removed {sid} from listen bus")

        _after = {s.source.name for s in self.listen_bus.source_slots}
        if _before != _after:
            print(f"  [sync] Listen bus sources: {_after}")

    def start(self):
        """Load config and start the bus tick loop."""
        self._load_and_create_busses()
        self.sync_listen_bus()
        if not self._busses:
            print("  [BusManager] No busses configured")
            return
        self._running = True
        self._thread = threading.Thread(target=self._tick_loop, daemon=True,
                                        name="BusManager")
        self._thread.start()
        _types = ', '.join(f'{b.bus_type}:{bid}' for bid, b in self._busses.items())
        print(f"  [BusManager] Started with {len(self._busses)} bus(ses): {_types}")

    def stop(self):
        """Stop the tick loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def reload(self):
        """Reload config and recreate busses."""
        # Unkey all auto-PTT endpoints before tearing down buses
        self._release_all_ptt()
        self.stop()
        self._busses.clear()
        self.start()

    def _release_all_ptt(self):
        """Send PTT-off to all endpoints with active auto-PTT."""
        gw = self.gateway
        if not hasattr(self, '_sink_ptt_hold'):
            return
        for _ep, _until in list(self._sink_ptt_hold.items()):
            if gw._link_ptt_active.get(_ep, False):
                try:
                    gw.link_server.send_command_to(
                        _ep, {"cmd": "ptt", "state": False})
                    gw._link_ptt_active[_ep] = False
                except Exception:
                    pass
            self._sink_ptt_hold[_ep] = 0

    def update_radio_reference(self, source_id):
        """Hot-swap a bus's radio reference when a link endpoint reconnects.

        Finds any bus whose radio matches *source_id* (sanitised) and
        replaces it with the current object from gw.link_endpoints.
        Also re-checks the routing config to catch TX-only buses where
        the old radio object may have been garbage collected on disconnect.
        No bus teardown — just swaps the pointer.
        """
        import json as _json
        gw = self.gateway
        new_radio = None
        ep_name = None
        for name, src in gw.link_endpoints.items():
            if getattr(src, 'source_id', None) == source_id:
                new_radio = src
                ep_name = name
                break
        if not new_radio:
            return

        # Load routing config to find which buses use this endpoint
        _tx_sink_id = source_id + '_tx'
        try:
            with open(self._config_path) as f:
                cfg = _json.load(f)
            connections = cfg.get('connections', [])
        except Exception:
            connections = []

        # Find bus IDs that have this endpoint as source or TX sink
        _bus_ids_for_ep = set()
        for c in connections:
            if c['type'] == 'source-bus' and c['from'] == source_id:
                _bus_ids_for_ep.add(c['to'])
            if c['type'] == 'bus-sink' and c['to'] == _tx_sink_id:
                _bus_ids_for_ep.add(c['from'])

        for bus_id, bus in self._busses.items():
            # Match by endpoint_name on current radio (if it's still alive)
            if hasattr(bus, '_radio') and bus._radio is not None:
                old_id = getattr(bus._radio, 'endpoint_name', '')
                if old_id == ep_name:
                    bus.set_radio(new_radio)
                    print(f"  [BusManager] Hot-swapped radio on bus '{bus_id}': {ep_name}")
                    continue
            # Match by routing config (catches stale/None radio references)
            if bus_id in _bus_ids_for_ep and hasattr(bus, '_radio'):
                bus.set_radio(new_radio)
                print(f"  [BusManager] Hot-swapped radio on bus '{bus_id}': {ep_name} (from routing)")
            # Also check TX source slots
            for slot in getattr(bus, '_tx_sources', []):
                old_id = getattr(slot.source, 'endpoint_name', '')
                if old_id == ep_name:
                    slot.source = new_radio
                    print(f"  [BusManager] Hot-swapped TX source on bus '{bus_id}': {ep_name}")

    def _load_and_create_busses(self):
        """Read routing_config.json and create bus instances."""
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        busses = data.get('busses', [])
        connections = data.get('connections', [])

        for bus_cfg in busses:
            bus_id = bus_cfg['id']
            bus_type = bus_cfg['type']
            bus_name = bus_cfg.get('name', bus_id)

            # Create the bus
            if bus_type == 'solo':
                bus = SoloBus(bus_name, self.config)
                _solo_radio = None
                # Find the radio from RX sources (e.g. aioc → bus)
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        radio = self._get_radio_plugin(c['from'])
                        if radio:
                            _solo_radio = radio
                            bus.set_radio(radio, routing_id=c['from'])
                            print(f"  [BusManager] {bus_name}: radio from source {c['from']}")
                            break
                # TX-only sinks (e.g. aioc_tx) — set first as primary radio,
                # register remaining *_tx sinks as extras so PTT + audio fan
                # out to every wired radio.
                if not _solo_radio:
                    for c in connections:
                        if c['type'] == 'bus-sink' and c['from'] == bus_id and c['to'].endswith('_tx'):
                            radio = self._get_radio_plugin(c['to'])
                            if radio:
                                _solo_radio = radio
                                bus.set_radio(radio, routing_id=c['to'])
                                bus._tx_only = True  # Don't call get_audio()
                                print(f"  [BusManager] {bus_name}: TX-only radio from sink {c['to']}")
                                break
                # Register additional *_tx sinks as extra TX radios (simulcast).
                # Skips the one already set as the primary radio.
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id and c['to'].endswith('_tx'):
                        radio = self._get_radio_plugin(c['to'])
                        if radio and radio is not _solo_radio:
                            bus.add_extra_tx_radio(radio, routing_id=c['to'])
                            print(f"  [BusManager] {bus_name}: +extra TX radio {c['to']}")
                # Add TX sources (skip the radio itself)
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        source = self._get_source(c['from'])
                        if source and source is not _solo_radio:
                            bus.add_tx_source(source)
                # Add non-radio sinks (and radio TX sink for level display)
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        sink_id = c['to']
                        bus.add_sink(sink_id)

            elif bus_type == 'duplex':
                bus = DuplexRepeaterBus(bus_name, self.config)
                sides = []
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        radio = self._get_radio_plugin(c['from'])
                        if radio:
                            sides.append(radio)
                if len(sides) >= 2:
                    bus.set_side_a(sides[0])
                    bus.set_side_b(sides[1])
                elif len(sides) == 1:
                    bus.set_side_a(sides[0])
                # Add sinks
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        bus.add_sink(c['to'])

            elif bus_type == 'simplex':
                bus = SimplexRepeaterBus(bus_name, self.config)
                sides = []
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        radio = self._get_radio_plugin(c['from'])
                        if radio:
                            sides.append(radio)
                if len(sides) >= 2:
                    bus.set_side_a(sides[0])
                    bus.set_side_b(sides[1])
                elif len(sides) == 1:
                    bus.set_side_a(sides[0])
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        bus.add_sink(c['to'])

            elif bus_type == 'listen':
                bus = ListenBus(bus_name, self.config)
                # Primary listen bus: sources are added by sync_listen_bus()
                # which uses the priority map for correct values.
                # Secondary listen buses: add sources from routing config directly.
                _primary_id = self.get_listen_bus_id()
                if bus_id == _primary_id:
                    self.listen_bus = bus
                    self._listen_bus_id = bus_id
                else:
                    for c in connections:
                        if c['type'] == 'source-bus' and c['to'] == bus_id:
                            source = self._get_source(c['from'])
                            if source:
                                _duck = getattr(source, 'duck', True)
                                _prio = getattr(source, 'sdr_priority', getattr(source, 'priority', 5))
                                _det = getattr(source, 'ptt_control', False)
                                bus.add_source(source, bus_priority=_prio, duckable=_duck, deterministic=_det, routing_id=c['from'])
                # Add sinks for all listen buses
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        bus.add_sink(c['to'])
            else:
                continue

            self._busses[bus_id] = bus

            # Create per-bus AudioProcessor from processing config
            proc_cfg = bus_cfg.get('processing', {})
            proc_cfg['muted'] = bus_cfg.get('muted', False)
            self._bus_config[bus_id] = proc_cfg
            if any(proc_cfg.get(k) for k in ('gate', 'hpf', 'lpf', 'notch', 'dfn')):
                proc = AudioProcessor(f"bus_{bus_id}", self.config)
                proc.enable_noise_gate = proc_cfg.get('gate', False)
                proc.enable_hpf = proc_cfg.get('hpf', False)
                proc.enable_lpf = proc_cfg.get('lpf', False)
                proc.enable_notch = proc_cfg.get('notch', False)
                proc.enable_dfn = proc_cfg.get('dfn', False)
                proc.dfn_mix = max(0.0, min(1.0, float(proc_cfg.get('dfn_mix', 0.5))))
                self._bus_processors[bus_id] = proc
                print(f"  [BusManager] {bus_name}: processing [{' '.join(k.upper() if k != 'dfn' else 'DFN' for k in ('gate','hpf','lpf','notch','dfn') if proc_cfg.get(k))}]")

            print(f"  [BusManager] Created {bus_type} bus: {bus_name}")

    def _get_radio_plugin(self, sink_id):
        """Get a radio plugin by its sink ID (e.g. 'kv4p_tx' → kv4p_plugin)."""
        gw = self.gateway
        if sink_id == 'kv4p_tx' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif sink_id in ('aioc_tx', 'aioc') and getattr(gw, 'th9800_plugin', None):
            return gw.th9800_plugin
        elif sink_id == 'kv4p' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        # Link endpoint lookup by source_id or sink_id
        for name, src in gw.link_endpoints.items():
            if getattr(src, 'sink_id', None) == sink_id:
                return src
            if getattr(src, 'source_id', None) == sink_id:
                return src
        return None

    def _get_source(self, source_id):
        """Get a source object by its ID."""
        gw = self.gateway
        if source_id == 'sdr1' and gw.sdr_plugin and getattr(gw.sdr_plugin, '_tuner1', None):
            return gw.sdr_plugin._tuner1
        elif source_id == 'sdr2' and gw.sdr_plugin and getattr(gw.sdr_plugin, '_tuner2', None):
            return gw.sdr_plugin._tuner2
        elif source_id == 'sdr' and gw.sdr_plugin:
            # Legacy: return tuner1 if available, else combined plugin
            if getattr(gw.sdr_plugin, '_tuner1', None):
                return gw.sdr_plugin._tuner1
            return gw.sdr_plugin
        elif source_id == 'kv4p' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif source_id == 'aioc' and getattr(gw, 'th9800_plugin', None):
            return gw.th9800_plugin
        elif source_id == 'playback' and getattr(gw, 'playback_source', None):
            return gw.playback_source
        elif source_id == 'loop_playback' and getattr(gw, 'loop_playback_source', None):
            return gw.loop_playback_source
        elif source_id == 'webmic' and getattr(gw, 'web_mic_source', None):
            return gw.web_mic_source
        elif source_id == 'announce' and getattr(gw, 'announce_input_source', None):
            return gw.announce_input_source
        elif source_id == 'monitor' and getattr(gw, 'web_monitor_source', None):
            return gw.web_monitor_source
        elif source_id == 'mumble_rx' and getattr(gw, 'mumble_source', None):
            return gw.mumble_source
        elif source_id == 'remote_audio' and getattr(gw, 'remote_audio_source', None):
            return gw.remote_audio_source
        # External plugins (auto-discovered from plugins/ directory)
        _ext = getattr(gw, '_external_plugins', {})
        if source_id in _ext:
            return _ext[source_id]
        # Link endpoint lookup by source_id
        for name, src in gw.link_endpoints.items():
            if getattr(src, 'source_id', None) == source_id:
                return src
        return None

    def _apply_processing(self, audio, bus_id):
        """Apply audio processing (gate/HPF/LPF/notch) based on bus config."""
        if audio is None:
            return None
        proc = self._bus_processors.get(bus_id)
        if proc:
            return proc.process(audio)
        return audio

    def _deliver_audio(self, bus_output, bus_id):
        """Deliver a bus's audio output to connected sinks + PCM/MP3 streams."""
        _t_deliver_start = time.monotonic()
        gw = self.gateway
        bus_cfg = self._bus_config.get(bus_id, {})
        _st = getattr(gw, '_stream_trace', None)
        _is_listen = (bus_id == self._listen_bus_id)

        _muted_sinks = getattr(gw, '_muted_sinks', set())
        _sink_gains = getattr(gw, '_sink_gains', {})
        _audio_level = None  # cached: all sinks get same processed audio

        # Apply bus processing ONCE (IIR filters are stateful — must not run per-sink).
        _proc = self._bus_processors.get(bus_id)
        _processed_audio = None
        if _proc and bus_output.mixed_audio is not None:
            _t_proc = time.monotonic()
            _processed_audio = _proc.process(bus_output.mixed_audio)
            _proc_ms = (time.monotonic() - _t_proc) * 1000
            if _st and _proc_ms > 5:
                _st.record(f'{bus_id}_deliver', 'processing', bus_output.mixed_audio,
                           -1, f'{_proc_ms:.1f}ms')
            # Replace per-sink audio refs (most busses send same mixed audio to all sinks)
            for sink_id in list(bus_output.audio):
                if bus_output.audio[sink_id] is not None:
                    bus_output.audio[sink_id] = _processed_audio

        # Listen bus: decay sink levels when no audio
        if _is_listen and bus_output.mixed_audio is None:
            gw.stream_audio_level = max(0, int(getattr(gw, 'stream_audio_level', 0) * 0.7))
            gw.mumble_tx_level = max(0, int(getattr(gw, 'mumble_tx_level', 0) * 0.7))
            gw.transcription_audio_level = max(0, int(getattr(gw, 'transcription_audio_level', 0) * 0.7))

        for sink_id, audio in bus_output.audio.items():
            if audio is None:
                continue
            if sink_id in _muted_sinks:
                continue

            # Apply per-sink gain (passive sinks like mumble, broadcastify, speaker).
            # Tanh soft-clip for gain > 1 so pushing sliders past 100% rolls
            # off cleanly instead of flat-topping into square-wave harmonics.
            _sg = _sink_gains.get(sink_id)
            if _sg is not None and _sg != 1.0:
                audio = apply_gain(audio, _sg)

            # Compute level once for level-tracking sinks
            if _audio_level is None:
                _audio_level = gw.calculate_audio_level(audio)

            _t_sink = time.monotonic()

            # Passive sinks
            if sink_id == 'mumble' and gw.mumble:
                # Listen bus: gate mumble delivery with VAD
                if _is_listen and not self._listen_vad_pass:
                    gw.mumble_tx_level = max(0, int(getattr(gw, 'mumble_tx_level', 0) * 0.7))
                    continue
                try:
                    _so = getattr(gw.mumble, 'sound_output', None)
                    _ef = getattr(_so, 'encoder_framesize', None) if _so else None
                    if _so is not None and _ef is not None:
                        # Feed in frame-aligned chunks (20ms = 960 samples = 1920 bytes)
                        # to prevent fractional frame accumulation in pymumble's buffer
                        _frame_bytes = int(_ef * getattr(gw.config, 'AUDIO_RATE', 48000) * 2)
                        if not hasattr(self, '_mumble_buf'):
                            self._mumble_buf = b''
                        self._mumble_buf += audio
                        while len(self._mumble_buf) >= _frame_bytes:
                            _frame = self._mumble_buf[:_frame_bytes]
                            self._mumble_buf = self._mumble_buf[_frame_bytes:]
                            _so.add_sound(_frame)
                        if _audio_level > getattr(gw, 'mumble_tx_level', 0):
                            gw.mumble_tx_level = _audio_level
                        else:
                            gw.mumble_tx_level = int(getattr(gw, 'mumble_tx_level', 0) * 0.7 + _audio_level * 0.3)
                    else:
                        if not hasattr(self, '_mumble_skip_logged'):
                            self._mumble_skip_logged = True
                            print(f"  [Mumble-TX] SKIPPED: sound_output={_so is not None} encoder_framesize={_ef}")
                except Exception as _me:
                    if not hasattr(self, '_mumble_err_logged'):
                        self._mumble_err_logged = True
                        print(f"  [Mumble-TX] ERROR: {_me}")
                _mumble_ms = (time.monotonic() - _t_sink) * 1000
                if _st:
                    _extra = f'mumble {_mumble_ms:.1f}ms' if _mumble_ms > 5 else ''
                    _st.record(f'{bus_id}_deliver', 'mumble', audio, -1, _extra)
            elif sink_id == 'speaker':
                gw._speaker_enqueue(audio)
                if _st:
                    _st.record(f'{bus_id}_deliver', 'speaker', audio)
            elif sink_id == 'broadcastify' and getattr(gw, 'stream_output', None):
                try:
                    gw.stream_output.send_audio(audio)
                    if _is_listen and gw.stream_output.connected:
                        gw.stream_audio_level = _audio_level
                except Exception:
                    pass
                _bcast_ms = (time.monotonic() - _t_sink) * 1000
                if _st and _bcast_ms > 5:
                    _st.record(f'{bus_id}_deliver', 'broadcastify', audio, -1, f'{_bcast_ms:.1f}ms')
            elif sink_id == 'recording':
                pass  # TODO: recording sink
            elif sink_id == 'transcription' and getattr(gw, 'transcriber', None):
                try:
                    _bus_obj = self._busses.get(bus_id)
                    _upstream = getattr(_bus_obj, 'last_dominant_source', None) if _bus_obj else None
                    gw.transcriber.feed(audio, source_id=bus_id, upstream_source=_upstream)
                    if _audio_level > getattr(gw, 'transcription_audio_level', 0):
                        gw.transcription_audio_level = _audio_level
                    else:
                        gw.transcription_audio_level = int(getattr(gw, 'transcription_audio_level', 0) * 0.7 + _audio_level * 0.3)
                except Exception as _te:
                    # Log once so future regressions are visible instead of silent.
                    if not hasattr(self, '_trans_err_logged'):
                        self._trans_err_logged = True
                        print(f"  [Transcribe] feed error: {_te}")
            elif sink_id == 'remote_audio_tx' and getattr(gw, 'remote_audio_server', None):
                if gw.remote_audio_server.connected:
                    try:
                        _t_ra = time.monotonic()
                        gw.remote_audio_server.send_audio(audio)
                        _ra_ms = (time.monotonic() - _t_ra) * 1000
                        if _st:
                            _extra = f'remote_tx {_ra_ms:.1f}ms' if _ra_ms > 5 else ''
                            _st.record(f'{bus_id}_deliver', 'remote_audio_tx', audio, -1, _extra)
                        if _audio_level > getattr(gw, 'remote_audio_tx_level', 0):
                            gw.remote_audio_tx_level = _audio_level
                        else:
                            gw.remote_audio_tx_level = int(getattr(gw, 'remote_audio_tx_level', 0) * 0.7 + _audio_level * 0.3)
                    except Exception:
                        pass

            # Radio TX sinks — SoloBus Phase 3 already calls put_audio(),
            # so here we only track TX level for the routing page display.
            # Listen bus: actually send audio to link endpoints via link_server.
            elif sink_id in ('kv4p_tx', 'aioc_tx') or self._get_radio_plugin(sink_id):
                for _eln, _els in gw.link_endpoints.items():
                    if getattr(_els, 'sink_id', None) == sink_id:
                        # Send audio to link endpoint for TX
                        # Skip if the bus already sent via put_audio (solo bus Phase 3)
                        _bus_obj = self._busses.get(bus_id)
                        _already_sent = (hasattr(_bus_obj, '_tx_only') and _bus_obj._tx_only
                                         and hasattr(_bus_obj, '_ptt_active') and _bus_obj._ptt_active)
                        if not _already_sent and getattr(gw, 'link_server', None):
                            _ep_settings = gw.link_endpoint_settings.get(_eln, {})
                            if not _ep_settings.get('tx_muted', False):
                                try:
                                    gw.link_server.send_audio_to(_eln, audio)
                                    if _st and _st.active:
                                        _st.record(f'{bus_id}_deliver', f'link_tx:{_eln}', audio)
                                except Exception:
                                    pass
                                # Auto-PTT: track hold timer (actual send deferred to tick loop)
                                if not hasattr(self, '_sink_ptt_hold'):
                                    self._sink_ptt_hold = {}
                                _ptt_threshold = 10
                                if _audio_level is not None and _audio_level >= _ptt_threshold:
                                    if not hasattr(self, '_sink_ptt_start'):
                                        self._sink_ptt_start = {}
                                    if not hasattr(self, '_sink_ptt_pending'):
                                        self._sink_ptt_pending = {}
                                    self._sink_ptt_hold[_eln] = time.monotonic() + 0.5
                                    if not gw._link_ptt_active.get(_eln, False):
                                        self._sink_ptt_pending[_eln] = True
                                        self._sink_ptt_start[_eln] = time.monotonic()
                        # Track TX level for routing display
                        if _audio_level and _audio_level > gw._link_tx_levels.get(_eln, 0):
                            gw._link_tx_levels[_eln] = _audio_level
                        else:
                            gw._link_tx_levels[_eln] = int(gw._link_tx_levels.get(_eln, 0) * 0.7 + (_audio_level or 0) * 0.3)
                        break

        # Per-bus PCM/MP3: deposit processed audio into shared buffer.
        proc_cfg = bus_cfg
        mixed = _processed_audio if _processed_audio is not None else bus_output.mixed_audio
        if mixed is not None:
            if proc_cfg.get('pcm', False):
                # Stage for per-tick mixing — actual queue.append + WS push
                # happens once at end of tick after all buses have delivered.
                self._pcm_tick.append(mixed)
                if _st and _st.active:
                    _st.record(f'{bus_id}_pcm', 'stage', mixed,
                               len(self._pcm_tick))
            if proc_cfg.get('mp3', False):
                self._mp3_tick.append(mixed)
            # Loop recording: feed processed audio to LoopRecorder
            if proc_cfg.get('loop', False):
                _lr = getattr(gw, 'loop_recorder', None)
                if _lr:
                    # Sync per-bus retention from routing config
                    _lh = proc_cfg.get('loop_hours', 0)
                    if _lh and _lh != _lr.get_retention(bus_id):
                        _lr.set_retention(bus_id, _lh)
                    _t_lr = time.monotonic()
                    _lr.feed(bus_id, mixed)
                    _lr_ms = (time.monotonic() - _t_lr) * 1000
                    if _st and _lr_ms > 5:
                        _st.record(f'{bus_id}_deliver', 'loop_rec', mixed,
                                   -1, f'{_lr_ms:.1f}ms')

        # Total deliver timing
        _deliver_total = (time.monotonic() - _t_deliver_start) * 1000
        if _st and _deliver_total > 10:
            _st.record(f'{bus_id}_deliver', 'total', bus_output.mixed_audio,
                       -1, f'{_deliver_total:.1f}ms')

    def _handle_listen_tick(self, output, chunk_size):
        """Handle listen-bus-specific post-tick work.

        Called from _tick_loop after the primary listen bus tick, before
        _deliver_audio.  Handles: SDR rebroadcast queue, health flags,
        ducked states, click suppression, VAD, EchoLink, automation.
        """
        gw = self.gateway
        data = output.mixed_audio

        # Queue duckee_only_audio + ptt for SDR rebroadcast
        sdr_only = output.status.get('duckee_only_audio')
        ptt_required = output.ptt.get('_ptt_required', False)
        self._sdr_rebroadcast_queue.append((sdr_only, ptt_required))

        # Health flags
        if data is not None:
            gw.last_audio_capture_time = time.time()
            gw.audio_capture_active = True
        else:
            gw.audio_capture_active = False

        # Ducked states for status bar
        gw.sdr_ducked = 'SDR1' in output.ducked_sources
        gw.sdr2_ducked = 'SDR2' in output.ducked_sources
        gw.remote_audio_ducked = 'SDRSV' in output.ducked_sources

        # Mixer trace state (read by status monitor and trace dump)
        if hasattr(self.listen_bus, '_last_trace_state'):
            gw._last_mixer_trace_state = self.listen_bus._last_trace_state.copy()

        # VAD (computed here, used by _deliver_audio for mumble gating)
        _audio_for_vad = data
        _proc = self._bus_processors.get(self._listen_bus_id)
        # VAD should run on processed audio if a processor exists,
        # but processing hasn't been applied yet (done in _deliver_audio).
        # For now, run on raw mixer output — matches old behavior.
        self._listen_vad_pass = (
            gw.check_vad(_audio_for_vad)
            if (getattr(gw.config, 'ENABLE_VAD', False) and _audio_for_vad)
            else True
        )

        # Click suppression on mixer output
        if data and len(data) >= 16:
            _arr = np.frombuffer(data, dtype=np.int16)
            _diffs = np.abs(np.diff(_arr.astype(np.int32)))
            _clicks = np.where(_diffs > 8000)[0]
            if len(_clicks) > 0:
                _farr = _arr.astype(np.float32)
                for _idx in _clicks:
                    _lo = max(0, _idx - 2)
                    _hi = min(len(_farr) - 1, _idx + 3)
                    if _hi - _lo >= 2:
                        _farr[_lo:_hi+1] = np.linspace(_farr[_lo], _farr[_hi], _hi - _lo + 1)
                _fixed = np.clip(_farr, -32768, 32767).astype(np.int16).tobytes()
                for sink_id in list(output.audio):
                    if output.audio[sink_id] is not None:
                        output.audio[sink_id] = _fixed

        # Automation recorder
        if data is not None:
            ae = getattr(gw, 'automation_engine', None)
            if ae and ae.recorder.is_recording():
                ae.recorder.feed(data)

        # EchoLink (legacy — not in routing config, checked by config flag)
        if data is not None:
            _el = getattr(gw, 'echolink_source', None)
            if _el and getattr(gw.config, 'RADIO_TO_ECHOLINK', False):
                try:
                    _el.send_audio(data)
                except Exception:
                    pass

    def _gc_callback(self, phase, info):
        """Record GC pause events for diagnostics."""
        if phase == 'start':
            self._gc_start = time.monotonic()
        elif phase == 'stop' and hasattr(self, '_gc_start'):
            dur_ms = (time.monotonic() - self._gc_start) * 1000
            self._gc_events.append((time.monotonic(), info.get('generation', -1), dur_ms))

    def dump_tick_trace(self):
        """Return tick trace data for analysis.  Called by audio_trace dump."""
        return list(self._tick_trace)

    def _tick_loop(self):
        """Main bus tick loop — runs all non-primary busses.

        Uses accumulative timing (matching the main audio loop) to prevent
        systematic clock drift.  Previous code reset next_tick after each
        iteration, causing period = tick_interval + processing_time.
        """
        chunk_size = getattr(self.config, 'AUDIO_CHUNK_SIZE', 2400)

        # ── GC control: disable automatic collection in this hot path ──
        gc.disable()
        gc.callbacks.append(self._gc_callback)
        print("  [BusManager] GC disabled in tick loop, manual gen-0 every 5s")

        # ── Accumulative self-clock (matches gateway_core.audio_transmit_loop) ──
        _next_tick = time.monotonic()
        _prev_tick_time = _next_tick
        _tick_num = 0

        while self._running:
            _tick_num += 1
            # ── Timing: sleep until next tick, accumulate (never reset) ──
            _now = time.monotonic()
            if _next_tick > _now:
                time.sleep(_next_tick - _now)
            elif _now - _next_tick > self._tick_interval:
                # Snap forward: skip ALL missed ticks, don't play catch-up.
                # Without this, after a 600ms stall the loop runs 12 ticks
                # back-to-back with 0ms gaps, causing TX stutter.
                _missed = int((_now - _next_tick) / self._tick_interval)
                _next_tick = _now
                if self._tick_trace is not None:
                    self._tick_trace.append((
                        _now, 0.0, 0.0, _tick_num,
                        len(self._pcm_queue),
                        {'_stall_skip': _missed},
                    ))
            _next_tick += self._tick_interval

            _tick_start = time.monotonic()
            _tick_dt = (_tick_start - _prev_tick_time) * 1000  # ms since last tick
            _prev_tick_time = _tick_start

            # Cross-clock correlation: main loop reads this to measure drift
            self._bm_tick_count = _tick_num
            self._bm_tick_mono = (_tick_num, _tick_start)

            # ── Per-bus tick + deliver ──────────────────────────────────────
            _bus_timings = {}
            gw = self.gateway
            for bus_id, bus in self._busses.items():
                try:
                    # Skip muted busses
                    if self._bus_config.get(bus_id, {}).get('muted', False):
                        self._bus_levels[bus_id] = 0
                        continue
                    _t0 = time.monotonic()
                    output = bus.tick(chunk_size)
                    _t_tick = (time.monotonic() - _t0) * 1000

                    # ── Listen bus specific handling ──
                    if bus_id == self._listen_bus_id:
                        self._handle_listen_tick(output, chunk_size)

                    # Track bus output level
                    _mixed = output.mixed_audio
                    if _mixed is not None:
                        _prev = self._bus_levels.get(bus_id, 0)
                        _lv = pcm_level(_mixed, current=_prev)
                    else:
                        _tx_active = output.status.get('tx_audio_active', False)
                        _lv = 50 if _tx_active else 0
                        _prev = self._bus_levels.get(bus_id, 0)
                    self._bus_levels[bus_id] = _lv

                    _t1 = time.monotonic()
                    self._deliver_audio(output, bus_id)
                    _t_deliver = (time.monotonic() - _t1) * 1000

                    _bus_timings[bus_id] = (_t_tick, _t_deliver, _lv)

                    # Stream trace: record bus tick + deliver with timing
                    _st = getattr(gw, '_stream_trace', None)
                    if _st and (_t_tick > 5 or _t_deliver > 5):
                        _st.record(f'{bus_id}_bus', 'tick_slow',
                                   output.mixed_audio, -1,
                                   f'tick={_t_tick:.1f}ms deliver={_t_deliver:.1f}ms')

                except Exception as e:
                    print(f"  [BusManager] {bus_id} tick error: {e}")
                    import traceback; traceback.print_exc()

            # ── Flush per-tick PCM/MP3: mix contributions from all buses ──
            # Multiple buses routed to pcm/mp3 are mixed (summed with soft
            # limiter) into one chunk per tick rather than interleaved.
            if self._pcm_tick:
                if len(self._pcm_tick) == 1:
                    _pcm_out = self._pcm_tick[0]
                else:
                    _pcm_out = self._pcm_tick[0]
                    for _extra in self._pcm_tick[1:]:
                        _pcm_out = mix_audio_streams(_pcm_out, _extra)
                self._pcm_queue.append(_pcm_out)
                _wcs = getattr(gw, 'web_config_server', None)
                if _wcs and _wcs._ws_clients:
                    _wcs.push_ws_audio(_pcm_out)
                self._pcm_tick.clear()
            if self._mp3_tick:
                if len(self._mp3_tick) == 1:
                    _mp3_out = self._mp3_tick[0]
                else:
                    _mp3_out = self._mp3_tick[0]
                    for _extra in self._mp3_tick[1:]:
                        _mp3_out = mix_audio_streams(_mp3_out, _extra)
                self._mp3_queue.append(_mp3_out)
                self._mp3_tick.clear()

            # ── Auto-PTT key/release for link endpoint TX sinks ─────────
            # PTT commands sent here (outside deliver) to avoid blocking tick
            if not hasattr(self, '_sink_ptt_start'):
                self._sink_ptt_start = {}
            if not hasattr(self, '_sink_ptt_pending'):
                self._sink_ptt_pending = {}
            # Send pending PTT-on commands
            for _ptt_ep in list(self._sink_ptt_pending):
                if self._sink_ptt_pending.pop(_ptt_ep, False):
                    try:
                        gw.link_server.send_command_to(
                            _ptt_ep, {"cmd": "ptt", "state": True})
                        gw._link_ptt_active[_ptt_ep] = True
                    except Exception:
                        pass
            _PTT_SAFETY_TIMEOUT = 180.0
            if hasattr(self, '_sink_ptt_hold'):
                _now_ptt = time.monotonic()
                for _ptt_ep, _ptt_until in list(self._sink_ptt_hold.items()):
                    # Safety timeout — force unkey after 3 minutes
                    _ptt_start = self._sink_ptt_start.get(_ptt_ep, 0)
                    if _ptt_start > 0 and _now_ptt - _ptt_start > _PTT_SAFETY_TIMEOUT:
                        if gw._link_ptt_active.get(_ptt_ep, False):
                            print(f"  [BusManager] PTT safety timeout on {_ptt_ep} — forcing unkey")
                            try:
                                gw.link_server.send_command_to(
                                    _ptt_ep, {"cmd": "ptt", "state": False})
                                gw._link_ptt_active[_ptt_ep] = False
                            except Exception:
                                pass
                        self._sink_ptt_hold[_ptt_ep] = 0
                        self._sink_ptt_start[_ptt_ep] = 0
                        continue
                    # Normal release after hold timer
                    if _ptt_until > 0 and _now_ptt > _ptt_until:
                        if gw._link_ptt_active.get(_ptt_ep, False):
                            try:
                                gw.link_server.send_command_to(
                                    _ptt_ep, {"cmd": "ptt", "state": False})
                                gw._link_ptt_active[_ptt_ep] = False
                            except Exception:
                                pass
                        self._sink_ptt_hold[_ptt_ep] = 0
                        self._sink_ptt_start[_ptt_ep] = 0

            _tick_total = (time.monotonic() - _tick_start) * 1000

            # ── Record trace row ───────────────────────────────────────────
            self._tick_trace.append((
                _tick_start,          # 0: monotonic timestamp
                _tick_dt,             # 1: actual interval (ms), target=50.0
                _tick_total,          # 2: total processing time (ms)
                _tick_num,            # 3: tick number
                len(self._pcm_queue), # 4: PCM queue depth after deposit
                _bus_timings,         # 5: {bus_id: (tick_ms, deliver_ms, level)}
            ))

            # ── Manual GC: gen-0 only, every 100 ticks (5s) in sleep window ──
            if _tick_num % 100 == 0:
                gc.collect(0)
