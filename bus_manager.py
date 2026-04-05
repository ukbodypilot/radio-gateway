"""Bus Manager — loads routing config and runs non-primary busses.

The primary ListenBus (monitor) continues to be driven by gateway_core's
main audio loop. This manager handles additional busses (Solo, Duplex,
Simplex) configured via the routing UI, running them in a separate thread.

This allows testing new bus types with pluginized radios (KV4P, D75)
without modifying the AIOC-entangled main loop.
"""

import collections
import gc
import json
import math
import os
import threading
import time

import numpy as np

from audio_bus import SoloBus, DuplexRepeaterBus, SimplexRepeaterBus, ListenBus
from audio_sources import AudioProcessor


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
        self._bus_levels = {}       # bus_id → audio level (0-100) for routing page

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

    def start(self):
        """Load config and start the bus tick loop."""
        self._load_and_create_busses()
        if not self._busses:
            print("  [BusManager] No additional busses configured")
            return
        self._running = True
        self._thread = threading.Thread(target=self._tick_loop, daemon=True,
                                        name="BusManager")
        self._thread.start()
        print(f"  [BusManager] Started with {len(self._busses)} bus(ses)")

    def stop(self):
        """Stop the tick loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def reload(self):
        """Reload config and recreate busses."""
        self.stop()
        self._busses.clear()
        self.start()

    def update_radio_reference(self, source_id):
        """Hot-swap a bus's radio reference when a link endpoint reconnects.

        Finds any bus whose radio matches *source_id* (sanitised) and
        replaces it with the current object from gw.link_endpoints.
        Also re-checks the routing config to catch TX-only buses where
        the old radio object may have been garbage collected on disconnect.
        No bus teardown — just swaps the pointer.
        """
        import re as _re, json as _json
        gw = self.gateway
        new_radio = None
        ep_name = None
        for name, src in gw.link_endpoints.items():
            if _re.sub(r'[^a-z0-9_]', '_', name.lower()) == source_id:
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

            # Skip the PRIMARY listen bus — the main loop's mixer handles that.
            # Secondary listen busses are created and ticked by BusManager.
            _primary_listen_id = self.get_listen_bus_id()
            if bus_type == 'listen' and bus_id == _primary_listen_id:
                continue

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
                            bus.set_radio(radio)
                            print(f"  [BusManager] {bus_name}: radio from source {c['from']}")
                            break
                # TX-only sinks (e.g. aioc_tx) — set as radio for TX but skip RX
                if not _solo_radio:
                    for c in connections:
                        if c['type'] == 'bus-sink' and c['from'] == bus_id and c['to'].endswith('_tx'):
                            radio = self._get_radio_plugin(c['to'])
                            if radio:
                                _solo_radio = radio
                                bus.set_radio(radio)
                                bus._tx_only = True  # Don't call get_audio()
                                print(f"  [BusManager] {bus_name}: TX-only radio from sink {c['to']}")
                                break
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
                # Secondary listen bus (not the primary)
                bus = ListenBus(bus_name, self.config)
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        source = self._get_source(c['from'])
                        if source:
                            _duck = getattr(source, 'duck', True)
                            _prio = getattr(source, 'sdr_priority', getattr(source, 'priority', 5))
                            _det = getattr(source, 'ptt_control', False)  # deterministic if PTT-capable
                            bus.add_source(source, bus_priority=_prio, duckable=_duck, deterministic=_det)
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
            if any(proc_cfg.get(k) for k in ('gate', 'hpf', 'lpf', 'notch')):
                proc = AudioProcessor(f"bus_{bus_id}", self.config)
                proc.enable_noise_gate = proc_cfg.get('gate', False)
                proc.enable_hpf = proc_cfg.get('hpf', False)
                proc.enable_lpf = proc_cfg.get('lpf', False)
                proc.enable_notch = proc_cfg.get('notch', False)
                self._bus_processors[bus_id] = proc
                print(f"  [BusManager] {bus_name}: processing [{' '.join(k.upper() for k in ('gate','hpf','lpf','notch') if proc_cfg.get(k))}]")

            print(f"  [BusManager] Created {bus_type} bus: {bus_name}")

    def _get_radio_plugin(self, sink_id):
        """Get a radio plugin by its sink ID (e.g. 'kv4p_tx' → kv4p_plugin)."""
        gw = self.gateway
        if sink_id == 'kv4p_tx' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif sink_id == 'd75_tx' and gw.d75_plugin:
            return gw.d75_plugin
        elif sink_id in ('aioc_tx', 'aioc') and getattr(gw, 'th9800_plugin', None):
            return gw.th9800_plugin
        elif sink_id == 'kv4p' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif sink_id == 'd75' and gw.d75_plugin:
            return gw.d75_plugin
        # Check link endpoints for D75 (when using link endpoint instead of plugin)
        if sink_id in ('d75_tx', 'd75') and not gw.d75_plugin:
            for name, src in gw.link_endpoints.items():
                if 'd75' in name.lower():
                    return src
        # Generic link endpoint lookup by sanitised name
        import re as _re
        _base = sink_id[:-3] if sink_id.endswith('_tx') else sink_id
        for name, src in gw.link_endpoints.items():
            if _re.sub(r'[^a-z0-9_]', '_', name.lower()) == _base:
                return src
        return None

    def _get_source(self, source_id):
        """Get a source object by its ID."""
        gw = self.gateway
        if source_id == 'sdr' and gw.sdr_plugin:
            return gw.sdr_plugin
        elif source_id == 'kv4p' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif source_id == 'd75' and gw.d75_plugin:
            return gw.d75_plugin
        elif source_id == 'aioc' and getattr(gw, 'th9800_plugin', None):
            return gw.th9800_plugin
        elif source_id == 'playback' and getattr(gw, 'playback_source', None):
            return gw.playback_source
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
        # Generic link endpoint lookup by sanitised name
        import re as _re
        for name, src in gw.link_endpoints.items():
            if _re.sub(r'[^a-z0-9_]', '_', name.lower()) == source_id:
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
        gw = self.gateway
        bus_cfg = self._bus_config.get(bus_id, {})
        _st = getattr(gw, '_stream_trace', None)

        _muted_sinks = getattr(gw, '_muted_sinks', set())
        _audio_level = None  # cached: all sinks get same processed audio
        for sink_id, audio in bus_output.audio.items():
            if audio is None:
                continue
            if sink_id in _muted_sinks:
                continue

            # Apply per-bus processing
            audio = self._apply_processing(audio, bus_id)
            if audio is None:
                continue

            # Compute level once for level-tracking sinks
            if _audio_level is None:
                _audio_level = gw.calculate_audio_level(audio)

            _t_sink = time.monotonic()

            # Passive sinks
            if sink_id == 'mumble' and gw.mumble:
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
                except Exception:
                    pass
                _bcast_ms = (time.monotonic() - _t_sink) * 1000
                if _st and _bcast_ms > 5:
                    _st.record(f'{bus_id}_deliver', 'broadcastify', audio, -1, f'{_bcast_ms:.1f}ms')
            elif sink_id == 'recording':
                pass  # TODO: recording sink
            elif sink_id == 'transcription' and getattr(gw, 'transcriber', None):
                try:
                    gw.transcriber.feed(audio, source_id=bus_id)
                    if _audio_level > getattr(gw, 'transcription_audio_level', 0):
                        gw.transcription_audio_level = _audio_level
                    else:
                        gw.transcription_audio_level = int(getattr(gw, 'transcription_audio_level', 0) * 0.7 + _audio_level * 0.3)
                except Exception:
                    pass
            elif sink_id == 'remote_audio_tx' and getattr(gw, 'remote_audio_server', None):
                if gw.remote_audio_server.connected:
                    try:
                        gw.remote_audio_server.send_audio(audio)
                        _ra_ms = (time.monotonic() - _t_sink) * 1000
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
            elif sink_id in ('kv4p_tx', 'd75_tx', 'aioc_tx') or self._get_radio_plugin(sink_id):
                # Update link endpoint TX level for routing display
                import re as _re2
                _base2 = sink_id[:-3] if sink_id.endswith('_tx') else sink_id
                for _eln, _els in gw.link_endpoints.items():
                    if _re2.sub(r'[^a-z0-9_]', '_', _eln.lower()) == _base2:
                        if _audio_level and _audio_level > gw._link_tx_levels.get(_eln, 0):
                            gw._link_tx_levels[_eln] = _audio_level
                        else:
                            gw._link_tx_levels[_eln] = int(gw._link_tx_levels.get(_eln, 0) * 0.7 + (_audio_level or 0) * 0.3)
                        break

        # Per-bus PCM/MP3: deposit into shared buffer for main loop to mix & push.
        proc_cfg = bus_cfg
        mixed = bus_output.mixed_audio
        if mixed is not None:
            if proc_cfg.get('pcm', False):
                self._pcm_queue.append(mixed)
            if proc_cfg.get('mp3', False):
                self._mp3_queue.append(mixed)

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
            for bus_id, bus in self._busses.items():
                try:
                    # Skip muted busses
                    if self._bus_config.get(bus_id, {}).get('muted', False):
                        self._bus_levels[bus_id] = 0
                        continue
                    _t0 = time.monotonic()
                    output = bus.tick(chunk_size)
                    _t_tick = (time.monotonic() - _t0) * 1000

                    # Track bus output level
                    _mixed = output.mixed_audio
                    if _mixed is not None:
                        _arr = np.frombuffer(_mixed, dtype=np.int16).astype(np.float32)
                        _rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
                        _lv = int(max(0, min(100, (20 * math.log10(_rms / 32767.0) + 60) * (100 / 60)))) if _rms > 0 else 0
                    else:
                        _tx_active = output.status.get('tx_audio_active', False)
                        _lv = 50 if _tx_active else 0
                    _prev = self._bus_levels.get(bus_id, 0)
                    self._bus_levels[bus_id] = _lv if _lv > _prev else max(0, int(_prev * 0.7))

                    _t1 = time.monotonic()
                    self._deliver_audio(output, bus_id)
                    _t_deliver = (time.monotonic() - _t1) * 1000

                    _bus_timings[bus_id] = (_t_tick, _t_deliver, _lv)

                    # Stream trace: record bus tick + deliver with timing
                    _st = getattr(self.gateway, '_stream_trace', None)
                    if _st and (_t_tick > 5 or _t_deliver > 5):
                        _st.record(f'{bus_id}_bus', 'tick_slow',
                                   output.mixed_audio, -1,
                                   f'tick={_t_tick:.1f}ms deliver={_t_deliver:.1f}ms')

                except Exception as e:
                    print(f"  [BusManager] {bus_id} tick error: {e}")
                    import traceback; traceback.print_exc()

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
