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
        self._running = False
        self._thread = None
        self._config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'routing_config.json')
        self._tick_interval = 0.05  # 50ms = 20 ticks/sec (matches main loop)

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

            # Skip 'listen' type — the primary ListenBus handles that
            if bus_type == 'listen':
                continue

            # Create the bus
            if bus_type == 'solo':
                bus = SoloBus(bus_name, self.config)
                # Find the radio (first TX-capable sink connected)
                for c in connections:
                    if c['type'] == 'bus-sink' and c['from'] == bus_id:
                        radio = self._get_radio_plugin(c['to'])
                        if radio:
                            bus.set_radio(radio)
                            break
                # Add TX sources
                for c in connections:
                    if c['type'] == 'source-bus' and c['to'] == bus_id:
                        source = self._get_source(c['from'])
                        if source:
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
            else:
                continue

            self._busses[bus_id] = bus
            print(f"  [BusManager] Created {bus_type} bus: {bus_name}")

    def _get_radio_plugin(self, sink_id):
        """Get a radio plugin by its sink ID (e.g. 'kv4p_tx' → kv4p_plugin)."""
        gw = self.gateway
        if sink_id == 'kv4p_tx' and gw.kv4p_plugin:
            return gw.kv4p_plugin
        elif sink_id == 'd75_tx' and gw.d75_plugin:
            return gw.d75_plugin
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
        elif source_id == 'aioc' and getattr(gw, 'radio_source', None):
            return gw.radio_source
        elif source_id == 'playback' and getattr(gw, 'playback_source', None):
            return gw.playback_source
        elif source_id == 'webmic' and getattr(gw, 'web_mic_source', None):
            return gw.web_mic_source
        elif source_id == 'announce' and getattr(gw, 'announce_input_source', None):
            return gw.announce_input_source
        elif source_id == 'monitor' and getattr(gw, 'web_monitor_source', None):
            return gw.web_monitor_source
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
        """Deliver a bus's audio output to connected sinks."""
        gw = self.gateway
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
                    gw.mumble.sound_output.add_sound(audio)
                except Exception:
                    pass
            elif sink_id == 'speaker' and getattr(gw, 'speaker_queue', None):
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

    def _tick_loop(self):
        """Main bus tick loop — runs all non-primary busses."""
        chunk_size = getattr(self.config, 'AUDIO_CHUNK_SIZE', 2400)
        next_tick = time.monotonic()

        while self._running:
            now = time.monotonic()
            if next_tick > now:
                time.sleep(next_tick - now)
            next_tick = time.monotonic() + self._tick_interval

            for bus_id, bus in self._busses.items():
                try:
                    output = bus.tick(chunk_size)
                    self._deliver_audio(output, bus_id)
                except Exception as e:
                    if getattr(self.config, 'VERBOSE_LOGGING', False):
                        print(f"  [BusManager] {bus_id} tick error: {e}")
