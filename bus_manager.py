"""Bus Manager — loads routing config and runs non-primary busses.

The primary ListenBus (monitor) continues to be driven by gateway_core's
main audio loop. This manager handles additional busses (Solo, Duplex,
Simplex) configured via the routing UI, running them in a separate thread.

This allows testing new bus types with pluginized radios (KV4P, D75)
without modifying the AIOC-entangled main loop.
"""

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
        self._pcm_queue = []        # PCM audio chunks from non-listen busses (list, not single buffer)
        self._mp3_queue = []        # MP3 audio chunks from non-listen busses
        # Keep old names for backward compat with drain methods
        self._pcm_buffer = None
        self._mp3_buffer = None
        self._bus_levels = {}       # bus_id → audio level (0-100) for routing page

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
        chunks = list(self._pcm_queue)
        self._pcm_queue.clear()
        return additive_mix(chunks)

    def drain_mp3(self):
        """Return and clear the accumulated MP3 audio from non-listen busses."""
        if not self._mp3_queue:
            return None
        from audio_bus import additive_mix
        chunks = list(self._mp3_queue)
        self._mp3_queue.clear()
        return additive_mix(chunks)
        return mp3

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
                # Add non-radio sinks
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        sink_id = c['to']
                        if not sink_id.endswith('_tx'):
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

        for sink_id, audio in bus_output.audio.items():
            if audio is None:
                continue

            # Apply per-bus processing
            audio = self._apply_processing(audio, bus_id)
            if audio is None:
                continue

            # Passive sinks
            if sink_id == 'mumble' and gw.mumble:
                try:
                    _so = getattr(gw.mumble, 'sound_output', None)
                    _ef = getattr(_so, 'encoder_framesize', None) if _so else None
                    if _so is not None and _ef is not None:
                        # Split into pymumble-sized frames (encoder_framesize * 48000 * 2 bytes)
                        _frame_bytes = int(_ef * 48000 * 2)
                        for _fi in range(0, len(audio), _frame_bytes):
                            _frame = audio[_fi:_fi + _frame_bytes]
                            if len(_frame) == _frame_bytes:
                                gw.mumble.sound_output.add_sound(_frame)
                        _pcm_len = len(_so.pcm) if hasattr(_so, 'pcm') else -1
                        if not hasattr(self, '_mumble_send_count'):
                            self._mumble_send_count = 0
                        self._mumble_send_count += 1
                        if self._mumble_send_count <= 3 or self._mumble_send_count % 100 == 0:
                            print(f"  [Mumble-TX] add_sound #{self._mumble_send_count}: {len(audio)}B pcm_buf={_pcm_len} ef={_ef}")
                        _ml = gw.calculate_audio_level(audio)
                        if _ml > getattr(gw, 'mumble_tx_level', 0):
                            gw.mumble_tx_level = _ml
                        else:
                            gw.mumble_tx_level = int(getattr(gw, 'mumble_tx_level', 0) * 0.7 + _ml * 0.3)
                    else:
                        if not hasattr(self, '_mumble_skip_logged'):
                            self._mumble_skip_logged = True
                            print(f"  [Mumble-TX] SKIPPED: sound_output={_so is not None} encoder_framesize={_ef}")
                except Exception as _me:
                    if not hasattr(self, '_mumble_err_logged'):
                        self._mumble_err_logged = True
                        print(f"  [Mumble-TX] ERROR: {_me}")
            elif sink_id == 'speaker':
                gw._speaker_enqueue(audio)
            elif sink_id == 'broadcastify' and getattr(gw, 'stream_output', None):
                try:
                    gw.stream_output.send_audio(audio)
                except Exception:
                    pass
            elif sink_id == 'recording':
                pass  # TODO: recording sink

            # Radio TX sinks
            elif sink_id == 'kv4p_tx' and gw.kv4p_plugin:
                gw.kv4p_plugin.put_audio(audio)
            elif sink_id == 'd75_tx' and gw.d75_plugin:
                gw.d75_plugin.put_audio(audio)
            elif sink_id == 'aioc_tx' and getattr(gw, 'th9800_plugin', None):
                gw.th9800_plugin.put_audio(audio)

        # Per-bus PCM/MP3: deposit into shared buffer for main loop to mix & push.
        proc_cfg = bus_cfg
        mixed = bus_output.mixed_audio
        if mixed is not None:
            if not hasattr(self, '_pcm_deposit_count'):
                self._pcm_deposit_count = 0
            if proc_cfg.get('pcm', False):
                self._pcm_queue.append(mixed)
                self._pcm_deposit_count += 1
                if self._pcm_deposit_count <= 3 or self._pcm_deposit_count % 100 == 0:
                    print(f"  [PCM] deposit #{self._pcm_deposit_count}: {len(mixed)}B from {bus_id} q={len(self._pcm_queue)}")
            if proc_cfg.get('mp3', False):
                self._mp3_queue.append(mixed)

    def _tick_loop(self):
        """Main bus tick loop — runs all non-primary busses."""
        chunk_size = getattr(self.config, 'AUDIO_CHUNK_SIZE', 2400)
        next_tick = time.monotonic()
        _diag_counter = 0
        _diag_interval = 100  # every 5 seconds (100 ticks * 50ms)
        # Per-bus tick trace: tracks audio/silence transitions for gap analysis
        _bus_trace = {}  # bus_id → {had_audio_last: bool, gap_start: tick#, gaps: [(start,len)], audio_ticks: int, silence_ticks: int}

        while self._running:
            now = time.monotonic()
            if next_tick > now:
                time.sleep(next_tick - now)
            next_tick = time.monotonic() + self._tick_interval
            _diag_counter += 1

            for bus_id, bus in self._busses.items():
                try:
                    output = bus.tick(chunk_size)
                    # Track bus output level
                    _mixed = output.mixed_audio
                    if _mixed is not None:
                        _arr = np.frombuffer(_mixed, dtype=np.int16).astype(np.float32)
                        _rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
                        _lv = int(max(0, min(100, (20 * math.log10(_rms / 32767.0) + 60) * (100 / 60)))) if _rms > 0 else 0
                    else:
                        # Check TX audio for solo busses
                        _tx_active = output.status.get('tx_audio_active', False)
                        _lv = 50 if _tx_active else 0
                    _prev = self._bus_levels.get(bus_id, 0)
                    self._bus_levels[bus_id] = _lv if _lv > _prev else max(0, int(_prev * 0.7))

                    # Track audio/silence gaps per bus
                    _has_audio = _mixed is not None
                    if bus_id not in _bus_trace:
                        _bus_trace[bus_id] = {'had_audio': False, 'gap_start': 0, 'gaps': [], 'audio': 0, 'silence': 0}
                    _bt = _bus_trace[bus_id]
                    if _has_audio:
                        _bt['audio'] += 1
                        if not _bt['had_audio'] and _bt['gap_start'] > 0:
                            _gap_len = _diag_counter - _bt['gap_start']
                            if _gap_len <= 10:  # only track short gaps (glitches), not idle silence
                                _bt['gaps'].append((_bt['gap_start'], _gap_len))
                        _bt['had_audio'] = True
                    else:
                        if _bt['had_audio']:
                            _bt['gap_start'] = _diag_counter
                        _bt['silence'] += 1
                        _bt['had_audio'] = False

                    if _diag_counter % _diag_interval == 1:
                        _has_radio = getattr(bus, '_radio', None) is not None
                        _sinks = list(output.audio.keys())
                        _cfg = self._bus_config.get(bus_id, {})
                        _tx_srcs = len(getattr(bus, '_tx_sources', []))
                        _tx_only = getattr(bus, '_tx_only', False)
                        _ptt = getattr(bus, '_ptt_active', False)
                        _extra = f" tx_srcs={_tx_srcs} tx_only={_tx_only} ptt={_ptt}" if _tx_srcs > 0 or _tx_only else ""
                        _gap_info = ""
                        if bus_id in _bus_trace:
                            _bt = _bus_trace[bus_id]
                            _gap_info = f" a={_bt['audio']} s={_bt['silence']} gaps={len(_bt['gaps'])}"
                            if _bt['gaps']:
                                _gap_info += f" last_gap={_bt['gaps'][-1][1]}tks"
                        print(f"  [BusManager] {bus_id}: radio={_has_radio} sinks={_sinks} audio={_has_audio} pcm={_cfg.get('pcm')} mp3={_cfg.get('mp3')}{_extra}{_gap_info}")
                    self._deliver_audio(output, bus_id)
                except Exception as e:
                    print(f"  [BusManager] {bus_id} tick error: {e}")
                    import traceback; traceback.print_exc()
