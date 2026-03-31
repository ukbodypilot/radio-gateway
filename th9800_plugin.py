"""TH-9800 Radio Plugin — AIOC USB audio + HID PTT + CAT control.

The TH-9800 radio uses multiple hardware interfaces:
- AIOC USB device for audio I/O (PyAudio) and PTT (HID GPIO)
- RadioCATClient for radio control (TCP to external th9800-cat.service)
- Relay controllers for alternative PTT and radio power button
- AudioProcessor for RX processing (gate/HPF/LPF/notch)

This plugin owns all TH-9800 hardware. The gateway core just ticks busses
and delivers audio to sinks.

See docs/mixer-v2-design.md for architecture.
"""

import math
import os
import struct
import subprocess
import sys
import threading
import time
import queue as _queue_mod

import numpy as np

from audio_sources import AudioProcessor
from gateway_link import RadioPlugin
from cat_client import RadioCATClient
from ptt import RelayController, GPIORelayController


class TH9800Plugin(RadioPlugin):
    """TH-9800 radio plugin — AIOC + CAT + relays.

    Audio path:
      RX: AIOC USB mic → PyAudio callback → AIOCRadioSource → get_audio()
      TX: put_audio() → PyAudio output stream → AIOC USB speaker → radio mic

    PTT methods (configurable):
      - 'aioc': AIOC HID GPIO (default) — requires RTS relay switching
      - 'relay': CH340 USB relay module
      - 'software': CAT TCP !ptt command
    """

    name = "th9800"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
        "frequency": True,
        "ctcss": False,
        "power": False,
        "rx_gain": False,
        "tx_gain": False,
        "smeter": False,
        "status": True,
    }

    def __init__(self):
        super().__init__()
        self._config = None
        self._gateway = None  # reference for VAD, calculate_audio_level, etc.

        # AIOC hardware
        self._aioc_device = None
        self._pyaudio = None
        self._input_stream = None
        self._output_stream = None
        self._aioc_available = False

        # AIOCRadioSource (RX audio with callback buffering)
        self._radio_source = None

        # CAT control
        self._cat_client = None

        # Relay controllers
        self._relay_ptt = None
        self._relay_radio = None
        self._relay_charger = None

        # PTT state
        self._ptt_active = False
        self._ptt_method = 'aioc'
        self._ptt_channel = 3
        self._ptt_change_time = 0.0

        # Audio processing
        self._processor = None

        # Bus compat
        self.enabled = True
        self.ptt_control = False
        self.priority = 1
        self.volume = 1.0
        self.duck = False  # not duckable — it's the primary radio
        self.muted = False
        self.audio_level = 0
        self.audio_boost = 1.0

        # Stream health
        self._restarting_stream = False
        self._last_audio_capture_time = 0
        self._stream_restart_count = 0

    def setup(self, config, gateway=None):
        """Initialize all TH-9800 hardware: AIOC, CAT, relays, audio streams."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._gateway = gateway  # real gateway object for AIOCRadioSource
        self._ptt_method = str(getattr(config, 'PTT_METHOD', 'aioc')).lower()
        self._ptt_channel = int(getattr(config, 'AIOC_PTT_CHANNEL', 3))

        # Audio processing
        self._processor = AudioProcessor("radio", config)
        self._sync_processor()

        # Initialize AIOC HID device
        self._init_aioc()

        # Initialize PyAudio and audio streams (simple blocking reader, no callback)
        if not self._init_audio_streams():
            print("  TH-9800: audio stream init failed")
            return False

        # Start RX reader thread
        self._rx_queue = _queue_mod.Queue(maxsize=16)
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._rx_reader_loop, daemon=True, name="TH9800-rx")
        self._rx_thread.start()
        print("  TH-9800: RX reader started")
        self._radio_source = None  # not using AIOCRadioSource

        # Initialize CAT client
        self._init_cat()

        # Initialize relays
        self._init_relays()

        return True

    def teardown(self):
        """Clean up all hardware resources."""
        # Stop RX reader
        self._rx_running = False

        # Unkey PTT
        if self._ptt_active:
            self._set_ptt(False)

        # Close CAT
        if self._cat_client:
            try:
                self._cat_client.close()
            except Exception:
                pass

        # Close relays
        for relay in [self._relay_ptt, self._relay_radio, self._relay_charger]:
            if relay:
                try:
                    relay.close()
                except Exception:
                    pass

        # Close audio streams
        for stream in [self._input_stream, self._output_stream]:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

        # Close AIOC HID
        if self._aioc_device:
            try:
                self._aioc_device.close()
            except Exception:
                pass

        # Terminate PyAudio
        if self._pyaudio:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass

    # -- Standard plugin interface --

    def get_audio(self, chunk_size=None):
        """Get RX audio from the blocking reader queue."""
        if not self.enabled or self.muted:
            return None, False

        data = None
        try:
            data = self._rx_queue.get_nowait()
        except _queue_mod.Empty:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering
        try:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                level = max(0, min(100, (20.0 * math.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                level = 0
            if level > self.audio_level:
                self.audio_level = int(level)
            else:
                self.audio_level = int(self.audio_level * 0.7 + level * 0.3)
        except Exception:
            pass

        return data, False

    def put_audio(self, pcm):
        """Send TX audio to AIOC output stream (radio mic input)."""
        if not self._output_stream or self.muted:
            return
        try:
            if self._config.OUTPUT_VOLUME != 1.0:
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                pcm = np.clip(arr * self._config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
            try:
                self._output_stream.write(pcm, exception_on_overflow=False)
            except TypeError:
                self._output_stream.write(pcm)
            # TX level metering
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                db = 20.0 * math.log10(rms / 32767.0)
                level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                level = 0
            self.tx_audio_level = int(level) if level > getattr(self, 'tx_audio_level', 0) else int(getattr(self, 'tx_audio_level', 0) * 0.7 + level * 0.3)
        except Exception as e:
            if getattr(self._config, 'VERBOSE_LOGGING', False):
                print(f"  [TH-9800] TX write error: {e}")

    def execute(self, cmd):
        """Handle commands: ptt, mute, status, power_relay, cat_cmd."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'ptt':
            state = bool(cmd.get('state', False))
            self._set_ptt(state)
            return {"ok": True, "ptt": state}
        elif action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}
        elif action == 'status':
            return {"ok": True, "status": self.get_status()}
        elif action == 'power_relay':
            return self._pulse_power_relay()
        elif action == 'cat_cmd':
            cat_cmd = cmd.get('command', '')
            if self._cat_client:
                resp = self._cat_client.send_command(cat_cmd)
                return {"ok": True, "response": resp}
            return {"ok": False, "error": "CAT not connected"}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return status dict."""
        d = {
            'plugin': self.name,
            'aioc_available': self._aioc_available,
            'ptt_active': self._ptt_active,
            'ptt_method': self._ptt_method,
            'audio_level': self.audio_level,
            'tx_audio_level': getattr(self, 'tx_audio_level', 0),
            'muted': self.muted,
            'stream_restarts': self._stream_restart_count,
        }
        if self._cat_client:
            d['cat_connected'] = self._cat_client._connected
            d['cat_serial'] = getattr(self._cat_client, '_serial_connected', False)
            try:
                d.update(self._cat_client.get_radio_state())
            except Exception:
                pass
        return d

    # -- Backward compat --

    @property
    def input_stream(self):
        return self._input_stream

    @property
    def ptt_active(self):
        return self._ptt_active

    def ptt_on(self):
        self._set_ptt(True)

    def ptt_off(self):
        self._set_ptt(False)

    def check_watchdog(self):
        """Check AIOC stream health, restart if stalled."""
        if not self._input_stream or self._restarting_stream:
            return
        try:
            if not self._input_stream.is_active():
                print(f"  [TH-9800] Stream inactive — restarting")
                self._restart_audio_input()
            elif self._radio_source and self._radio_source._chunk_queue.qsize() == 0:
                # Stream reports active but no blobs — check if stuck
                last_capture = getattr(self._gateway, 'last_audio_capture_time', 0) if self._gateway else 0
                if last_capture > 0 and time.time() - last_capture > 10.0:
                    print(f"  [TH-9800] No audio blobs for 10s — restarting stream")
                    self._restart_audio_input()
        except Exception:
            pass

    def cleanup(self):
        self.teardown()

    # -- Internal: AIOC hardware init --

    def _init_aioc(self):
        """Open AIOC HID device for GPIO PTT."""
        try:
            import hid
            _vid = getattr(self._config, 'AIOC_VID', 0x1209)
            _pid = getattr(self._config, 'AIOC_PID', 0x7388)
            vid = int(str(_vid), 16) if isinstance(_vid, str) else int(_vid)
            pid = int(str(_pid), 16) if isinstance(_pid, str) else int(_pid)
            self._aioc_device = hid.Device(vid=vid, pid=pid)
            self._aioc_available = True
            print(f"  TH-9800: AIOC HID opened ({self._aioc_device.product})")
        except Exception as e:
            print(f"  TH-9800: AIOC HID not found ({e})")
            self._aioc_available = False

    def _init_audio_streams(self):
        """Initialize PyAudio and open AIOC input/output streams.

        Uses blocking mode (no callback) — the _rx_reader_loop thread reads
        from the input stream. This is simpler and more reliable than the
        PortAudio callback approach which silently dies on some USB devices.
        """
        try:
            import pyaudio
            self._pyaudio = pyaudio.PyAudio()

            input_idx, output_idx = self._find_aioc_device()
            if input_idx is None:
                print("  TH-9800: AIOC audio device not found")
                return False

            rate = self._config.AUDIO_RATE
            channels = self._config.AUDIO_CHANNELS
            chunk = self._config.AUDIO_CHUNK_SIZE
            fmt = pyaudio.paInt16

            # Output stream (gateway → radio mic)
            self._output_stream = self._pyaudio.open(
                format=fmt, channels=channels, rate=rate,
                output=True, output_device_index=output_idx,
                frames_per_buffer=chunk)

            # Input stream (radio → gateway) — blocking mode
            self._input_stream = self._pyaudio.open(
                format=fmt, channels=channels, rate=rate,
                input=True, input_device_index=input_idx,
                frames_per_buffer=chunk)

            print(f"  TH-9800: Audio streams opened (device {input_idx})")
            return True
        except Exception as e:
            print(f"  TH-9800: Audio init error: {e}")
            return False

    def _rx_reader_loop(self):
        """Read audio from AIOC input stream in a blocking loop.

        Reads 4× chunk_size (200ms) per call to match AIOC USB delivery,
        then slices into individual chunks and queues each one.
        This keeps the queue buffered ahead of the mixer's 50ms tick.
        """
        chunk_size = self._config.AUDIO_CHUNK_SIZE
        chunk_bytes = chunk_size * 2  # 16-bit mono
        read_size = chunk_size * 4  # 200ms block (matches AIOC USB period)

        while self._rx_running:
            if not self._input_stream or self._restarting_stream:
                time.sleep(0.05)
                continue
            try:
                data = self._input_stream.read(read_size, exception_on_overflow=False)
                if not data:
                    continue

                # Slice 200ms block into 4 × 50ms chunks
                for offset in range(0, len(data), chunk_bytes):
                    chunk = data[offset:offset + chunk_bytes]
                    if len(chunk) < chunk_bytes:
                        break

                    # Apply audio processing
                    if self._processor:
                        chunk = self._processor.process(chunk)

                    # Track level from reader thread (works even when not on a bus)
                    try:
                        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
                        if rms > 0:
                            _lv = max(0, min(100, (20.0 * math.log10(rms / 32767.0) + 60) * (100 / 60)))
                        else:
                            _lv = 0
                        if _lv > self.audio_level:
                            self.audio_level = int(_lv)
                        else:
                            self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
                    except Exception:
                        pass

                    # Queue for get_audio()
                    try:
                        self._rx_queue.put_nowait(chunk)
                    except _queue_mod.Full:
                        try:
                            self._rx_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._rx_queue.put_nowait(chunk)
                        except _queue_mod.Full:
                            pass

            except IOError as e:
                if hasattr(e, 'errno') and e.errno == -9981:
                    try:
                        self._input_stream.read(read_size, exception_on_overflow=False)
                    except Exception:
                        pass
                else:
                    time.sleep(0.1)
            except Exception:
                time.sleep(0.1)

    def _find_aioc_device(self):
        """Find AIOC audio device indices by name."""
        if not self._pyaudio:
            return None, None
        name_match = str(getattr(self._config, 'AIOC_DEVICE_NAME', 'All-In-One')).lower()
        for i in range(self._pyaudio.get_device_count()):
            try:
                info = self._pyaudio.get_device_info_by_index(i)
                if name_match in info.get('name', '').lower():
                    return i, i  # AIOC uses same device for input and output
            except Exception:
                continue
        return None, None

    # -- Internal: CAT client --

    def _init_cat(self):
        """Connect to TH-9800 CAT TCP server."""
        if not getattr(self._config, 'ENABLE_CAT_CONTROL', False):
            return
        try:
            host = str(getattr(self._config, 'CAT_HOST', 'localhost'))
            port = int(getattr(self._config, 'CAT_PORT', 9800))
            password = str(getattr(self._config, 'CAT_PASSWORD', ''))
            verbose = getattr(self._config, 'VERBOSE_LOGGING', False)
            self._cat_client = RadioCATClient(host, port, password, verbose=verbose)
            if self._cat_client.connect():
                self._cat_client.start_background_drain()
                print(f"  TH-9800: CAT connected ({host}:{port})")
            else:
                print(f"  TH-9800: CAT connection failed")
                self._cat_client = None
        except Exception as e:
            print(f"  TH-9800: CAT error: {e}")
            self._cat_client = None

    # -- Internal: relay controllers --

    def _init_relays(self):
        """Initialize relay controllers for PTT, radio power, charger."""
        # PTT relay (alternative to AIOC GPIO)
        if getattr(self._config, 'ENABLE_PTT_RELAY', False):
            try:
                device = str(self._config.PTT_RELAY_DEVICE)
                baud = int(getattr(self._config, 'PTT_RELAY_BAUD', 9600))
                self._relay_ptt = RelayController(device, baud)
                self._relay_ptt.open()
                print(f"  TH-9800: PTT relay on {device}")
            except Exception as e:
                print(f"  TH-9800: PTT relay error: {e}")

        # Radio power button relay
        if getattr(self._config, 'ENABLE_RELAY_RADIO', False):
            try:
                device = str(self._config.RELAY_RADIO_DEVICE)
                baud = int(getattr(self._config, 'RELAY_RADIO_BAUD', 9600))
                self._relay_radio = RelayController(device, baud)
                self._relay_radio.open()
                print(f"  TH-9800: Radio power relay on {device}")
            except Exception as e:
                print(f"  TH-9800: Radio relay error: {e}")

        # Charger relay (GPIO or serial)
        if getattr(self._config, 'ENABLE_RELAY_CHARGER', False):
            try:
                gpio = getattr(self._config, 'RELAY_CHARGER_GPIO', None)
                if gpio:
                    self._relay_charger = GPIORelayController(int(gpio))
                else:
                    device = str(self._config.RELAY_CHARGER_DEVICE)
                    baud = int(getattr(self._config, 'RELAY_CHARGER_BAUD', 9600))
                    self._relay_charger = RelayController(device, baud)
                self._relay_charger.open()
                print(f"  TH-9800: Charger relay ready")
            except Exception as e:
                print(f"  TH-9800: Charger relay error: {e}")

    # -- Internal: PTT --

    def _set_ptt(self, state_on):
        """Key/unkey using configured PTT method."""
        if state_on == self._ptt_active:
            return
        if self._ptt_method == 'relay':
            self._ptt_via_relay(state_on)
        elif self._ptt_method == 'software':
            self._ptt_via_software(state_on)
        else:
            self._ptt_via_aioc(state_on)
        self._ptt_active = state_on
        self._ptt_change_time = time.monotonic()

    def _ptt_via_aioc(self, state_on):
        """PTT via AIOC HID GPIO with RTS relay switching."""
        if not self._aioc_device:
            return
        try:
            if state_on and self._cat_client:
                self._cat_client._pause_drain()
                try:
                    self._cat_client.set_rts(False)  # Radio Controlled
                except Exception:
                    pass
            state = 1 if state_on else 0
            iomask = 1 << (self._ptt_channel - 1)
            iodata = state << (self._ptt_channel - 1)
            data = struct.pack("<BBBBB", 0, 0, iodata, iomask, 0)
            self._aioc_device.write(bytes(data))
            if not state_on and self._cat_client:
                try:
                    self._cat_client.set_rts(True)  # USB Controlled
                except Exception:
                    pass
                finally:
                    self._cat_client._drain_paused = False
        except Exception as e:
            print(f"  [TH-9800] PTT AIOC error: {e}")
            if self._cat_client and self._cat_client._drain_paused:
                self._cat_client._drain_paused = False

    def _ptt_via_relay(self, state_on):
        """PTT via CH340 USB relay."""
        if self._relay_ptt:
            self._relay_ptt.set_state(state_on)

    def _ptt_via_software(self, state_on):
        """PTT via CAT TCP !ptt command."""
        if not self._cat_client:
            return
        try:
            self._cat_client._pause_drain()
            try:
                self._cat_client._send_cmd("!ptt on" if state_on else "!ptt off")
            finally:
                self._cat_client._drain_paused = False
        except Exception as e:
            print(f"  [TH-9800] PTT CAT error: {e}")

    def _pulse_power_relay(self):
        """Momentary pulse on radio power relay."""
        if not self._relay_radio:
            return {"ok": False, "error": "no radio relay"}
        try:
            self._relay_radio.set_state(True)
            time.sleep(0.5)
            self._relay_radio.set_state(False)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- Internal: audio stream health --

    def _restart_audio_input(self):
        """Recover AIOC input stream."""
        if self._restarting_stream:
            return
        self._restarting_stream = True
        try:
            if self._input_stream:
                try:
                    self._input_stream.stop_stream()
                    self._input_stream.close()
                except Exception:
                    pass
                self._input_stream = None
            time.sleep(0.5)
            input_idx, _ = self._find_aioc_device()
            if input_idx is not None and self._pyaudio:
                import pyaudio
                cb = self._radio_source._audio_callback if self._radio_source else None
                self._input_stream = self._pyaudio.open(
                    format=pyaudio.paInt16,
                    channels=self._config.AUDIO_CHANNELS,
                    rate=self._config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self._config.AUDIO_CHUNK_SIZE,
                    stream_callback=cb)
                self._stream_restart_count += 1
                print(f"  [TH-9800] Audio input restarted (count: {self._stream_restart_count})")
        except Exception as e:
            print(f"  [TH-9800] Audio restart error: {e}")
        finally:
            self._restarting_stream = False

    # -- Internal: processing sync --

    def _sync_processor(self):
        """Sync config flags into the AudioProcessor."""
        if not self._processor or not self._config:
            return
        p = self._processor
        p.enable_noise_gate = getattr(self._config, 'ENABLE_NOISE_GATE', True)
        p.gate_threshold = getattr(self._config, 'NOISE_GATE_THRESHOLD', -50)
        p.gate_attack = getattr(self._config, 'NOISE_GATE_ATTACK', 0.01)
        p.gate_release = getattr(self._config, 'NOISE_GATE_RELEASE', 0.1)
        p.enable_hpf = getattr(self._config, 'ENABLE_HIGHPASS', True)
        p.hpf_cutoff = getattr(self._config, 'HIGHPASS_CUTOFF', 300)
        p.enable_lpf = getattr(self._config, 'ENABLE_LOWPASS', False)
        p.lpf_cutoff = getattr(self._config, 'LOWPASS_CUTOFF', 3000)
        p.enable_notch = getattr(self._config, 'ENABLE_NOTCH', False)
        p.notch_freq = getattr(self._config, 'NOTCH_FREQ', 1000)
        p.notch_q = getattr(self._config, 'NOTCH_Q', 10.0)

    # -- Internal: gateway shim for AIOCRadioSource --

    def _create_source_gateway_shim(self):
        """Create a shim object that AIOCRadioSource can use as 'self.gateway'.

        AIOCRadioSource expects self.gateway to have:
          - input_stream, restarting_stream, rx_muted, config
          - last_audio_capture_time, last_successful_read, audio_capture_active
          - process_audio_for_mumble(), check_vad(), calculate_audio_level()
          - tx_audio_level, ptt_active, vad_active
          - _ptt_change_time
        """
        plugin = self

        class _Shim:
            config = plugin._config

            @property
            def input_stream(self):
                return plugin._input_stream

            @property
            def restarting_stream(self):
                return plugin._restarting_stream

            @property
            def rx_muted(self):
                return plugin.muted

            @property
            def ptt_active(self):
                return plugin._ptt_active

            @property
            def _ptt_change_time(self):
                return plugin._ptt_change_time

            # Level tracking
            tx_audio_level = 0
            last_audio_capture_time = 0
            last_successful_read = 0
            audio_capture_active = False
            vad_active = False

            def process_audio_for_mumble(self, pcm_data):
                if plugin._processor:
                    plugin._sync_processor()
                    return plugin._processor.process(pcm_data)
                return pcm_data

            def check_vad(self, pcm_data):
                # Always pass — the bus system handles gating/ducking.
                # AIOCRadioSource uses this to decide whether to return audio.
                # In plugin mode, we always return audio and let the bus decide.
                return True

            def calculate_audio_level(self, pcm_data):
                arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
                if rms > 0:
                    db = 20.0 * math.log10(rms / 32767.0)
                    return max(0, min(100, int((db + 60) * (100 / 60))))
                return 0

            def notify(self, message, level='error'):
                print(f"  [TH-9800] {message}")

        return _Shim()
