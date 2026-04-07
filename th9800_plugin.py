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
      RX: AIOC USB mic → PyAudio blocking read → _rx_reader_loop → get_audio()
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
        self._tx_queue = None           # TX audio queue for non-blocking writes
        self._tx_thread = None

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
        self.tx_audio_level = 0
        self.audio_boost = 1.0
        self.tx_audio_boost = 1.0

        # Stream health (reader thread manages lifecycle)
        self._last_audio_capture_time = 0
        self._stream_restart_count = 0
        self._stream_trace = None  # set by gateway after setup

    def setup(self, config, gateway=None):
        """Initialize all TH-9800 hardware: AIOC, CAT, relays, audio streams."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._gateway = gateway
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
        # Small queue (3 chunks = 150ms) keeps latency tight.
        # Reader discards oldest on overflow, consumer always gets fresh audio.
        self._rx_queue = _queue_mod.Queue(maxsize=3)
        self._rx_queue_primed = False  # flush stale data on first consumer read
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._rx_reader_loop, daemon=True, name="TH9800-rx")
        self._rx_thread.start()
        print("  TH-9800: RX reader started")
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

        # First consumer read: flush stale chunks that accumulated before
        # the BusManager started ticking.
        if not self._rx_queue_primed:
            self._rx_queue_primed = True
            _flushed = 0
            while self._rx_queue.qsize() > 1:
                try:
                    self._rx_queue.get_nowait()
                    _flushed += 1
                except _queue_mod.Empty:
                    break
            if _flushed:
                print(f"  [TH9800-RX] Flushed {_flushed} stale chunks from queue")

        data = None
        _qd = self._rx_queue.qsize()
        try:
            data = self._rx_queue.get_nowait()
        except _queue_mod.Empty:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            _st = self._stream_trace
            if _st:
                _st.record('aioc_rx', 'queue_get', None, _qd, 'empty')
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

        _st = self._stream_trace
        if _st:
            _st.record('aioc_rx', 'queue_get', data, _qd)

        return data, False

    def put_audio(self, pcm):
        """Queue TX audio for non-blocking write to AIOC output stream."""
        if not self._output_stream or self.muted or self._tx_queue is None:
            return
        try:
            # Apply TX boost from routing page slider
            if self.tx_audio_boost != 1.0:
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                pcm = np.clip(arr * self.tx_audio_boost, -32768, 32767).astype(np.int16).tobytes()
            if self._config.OUTPUT_VOLUME != 1.0:
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                pcm = np.clip(arr * self._config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
            # Queue for writer thread — never blocks the caller
            _qd = len(self._tx_queue)
            self._tx_queue.append(pcm)
            _st = self._stream_trace
            if _st:
                _st.record('aioc_tx', 'queue_put', pcm, _qd)
            # TX level metering
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                db = 20.0 * math.log10(rms / 32767.0)
                level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                level = 0
            self.tx_audio_level = int(level) if level > self.tx_audio_level else int(self.tx_audio_level * 0.7 + level * 0.3)
        except Exception as e:
            if getattr(self._config, 'VERBOSE_LOGGING', False):
                print(f"  [TH-9800] TX write error: {e}")

    def _tx_writer_loop(self):
        """Dedicated thread for writing TX audio to AIOC output stream.

        Decouples put_audio() from the blocking stream.write() so the main
        audio loop isn't stalled by USB I/O contention with the RX reader.
        """
        while self._output_stream:
            try:
                pcm = self._tx_queue.popleft()
            except IndexError:
                time.sleep(0.005)  # 5ms idle poll
                continue
            _st = self._stream_trace
            if _st:
                _st.record('aioc_tx', 'hw_write', pcm, len(self._tx_queue))
            try:
                try:
                    self._output_stream.write(pcm, exception_on_overflow=False)
                except TypeError:
                    self._output_stream.write(pcm)
            except Exception:
                pass

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
        """No-op — stream lifecycle is fully managed by _rx_reader_loop."""
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
        """Initialize PyAudio and open AIOC output stream.

        The input stream is opened by _rx_reader_loop which owns its
        full lifecycle (open, read, close, restart).  Only the output
        stream (gateway → radio mic) is opened here.
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

            # Start non-blocking TX writer thread
            import collections as _col
            self._tx_queue = _col.deque(maxlen=16)
            self._tx_thread = threading.Thread(target=self._tx_writer_loop, daemon=True, name="TH9800-tx")
            self._tx_thread.start()

            # Input stream opened by reader thread — not here
            print(f"  TH-9800: Audio output opened (device {output_idx})")
            return True
        except Exception as e:
            print(f"  TH-9800: Audio init error: {e}")
            return False

    def _rx_reader_loop(self):
        """Read audio from AIOC via arecord subprocess (raw ALSA).

        WirePlumber disables the AIOC so PipeWire/PyAudio/sounddevice all
        read DC silence even when specifying hw:N,0.  The only reliable
        path is arecord which uses raw ALSA without the PipeWire ALSA plugin.

        This thread owns the subprocess lifecycle: start, read stdout, kill,
        restart.  The only interface to the rest of the system is self._rx_queue.
        """
        import subprocess

        chunk_size = self._config.AUDIO_CHUNK_SIZE
        chunk_bytes = chunk_size * 2  # 16-bit mono
        rate = self._config.AUDIO_RATE
        channels = self._config.AUDIO_CHANNELS
        name_match = str(getattr(self._config, 'AIOC_DEVICE_NAME', 'All-In-One')).lower()

        while self._rx_running:
            # ── Phase 1: Find ALSA device and start arecord ──
            proc = None
            alsa_card = self._find_alsa_card(name_match)
            if alsa_card is None:
                print("  [TH9800-RX] AIOC not found in /proc/asound/cards — retrying in 5s")
                time.sleep(5)
                continue

            alsa_dev = f"hw:{alsa_card},0"
            try:
                proc = subprocess.Popen(
                    ['arecord', '-D', alsa_dev, '-f', 'S16_LE',
                     '-r', str(rate), '-c', str(channels), '-t', 'raw',
                     '--buffer-size', str(chunk_size * 4)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._input_stream = proc  # expose for is_active() checks
                self._stream_restart_count += 1
                print(f"  [TH9800-RX] arecord opened on {alsa_dev} pid={proc.pid} (restart #{self._stream_restart_count})")
            except Exception as e:
                print(f"  [TH9800-RX] Failed to start arecord on {alsa_dev}: {e} — retrying in 2s")
                time.sleep(2)
                continue

            # ── Phase 2: Read loop — read fixed-size chunks from stdout ──
            _consecutive_errors = 0
            while self._rx_running:
                try:
                    data = proc.stdout.read(chunk_bytes)
                    if not data or len(data) == 0:
                        break  # arecord died

                    self._last_audio_capture_time = time.time()
                    _consecutive_errors = 0

                    chunk = data
                    if len(chunk) < chunk_bytes:
                        chunk = chunk + b'\x00' * (chunk_bytes - len(chunk))

                    _st = self._stream_trace
                    if _st:
                        _st.record('aioc_rx', 'arecord_read', chunk)

                    # Track level on RAW audio (before processing)
                    # Apply audio processing (HPF, gate, etc.)
                    if self._processor:
                        chunk = self._processor.process(chunk)

                    # Compute level AFTER processing so gate squelches noise to zero
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

                    if _st:
                        _st.record('aioc_rx', 'post_proc', chunk)

                    # Queue for get_audio()
                    _qd = self._rx_queue.qsize()
                    _overflow = False
                    try:
                        self._rx_queue.put_nowait(chunk)
                    except _queue_mod.Full:
                        _overflow = True
                        try:
                            self._rx_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._rx_queue.put_nowait(chunk)
                        except _queue_mod.Full:
                            pass

                    if _st:
                        _st.record('aioc_rx', 'queue_put', chunk, _qd,
                                   'overflow' if _overflow else '')

                except Exception as e:
                    _consecutive_errors += 1
                    if _consecutive_errors <= 3:
                        print(f"  [TH9800-RX] Read error: {e} [{_consecutive_errors}]")
                    if _consecutive_errors >= 5:
                        print(f"  [TH9800-RX] {_consecutive_errors} consecutive errors — killing arecord")
                        break
                    time.sleep(0.1)

            # ── Phase 3: Kill arecord and retry ──
            self._input_stream = None
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if self._rx_running:
                print("  [TH9800-RX] arecord stopped — reopening in 1s")
                time.sleep(1)

    def _find_aioc_device(self):
        """Find AIOC audio device indices.

        WirePlumber disables the AIOC (99-disable-loopback.conf) so
        PipeWire/PyAudio can't see it by name.  We scan /proc/asound/cards
        for the ALSA card number, then match PyAudio devices by 'hw:N'.
        Falls back to PyAudio name search if ALSA scan fails.
        """
        if not self._pyaudio:
            return None, None
        name_match = str(getattr(self._config, 'AIOC_DEVICE_NAME', 'All-In-One')).lower()

        # Pass 1: find ALSA card number from /proc/asound/cards.
        # This is the reliable path — immune to PipeWire/WirePlumber state.
        alsa_card = self._find_alsa_card(name_match)
        if alsa_card is not None:
            hw_prefix = f"(hw:{alsa_card},"
            for i in range(self._pyaudio.get_device_count()):
                try:
                    info = self._pyaudio.get_device_info_by_index(i)
                    if hw_prefix in info.get('name', ''):
                        print(f"  [TH9800] AIOC found: ALSA card {alsa_card} → PyAudio device {i}: {info['name']}")
                        return i, i
                except Exception:
                    continue
            # hw: device not in PyAudio list — reinitialize to force rescan
            try:
                import pyaudio as _pa_mod
                self._pyaudio.terminate()
                self._pyaudio = _pa_mod.PyAudio()
                print(f"  [TH9800] PyAudio reinitialized — rescanning for AIOC hw:{alsa_card}")
                for i in range(self._pyaudio.get_device_count()):
                    try:
                        info = self._pyaudio.get_device_info_by_index(i)
                        if hw_prefix in info.get('name', '') or name_match in info.get('name', '').lower():
                            print(f"  [TH9800] AIOC found after rescan → device {i}: {info['name']}")
                            return i, i
                    except Exception:
                        continue
            except Exception as e:
                print(f"  [TH9800] PyAudio reinit failed: {e}")

        # Pass 2: fallback — PyAudio name search (works if WirePlumber hasn't disabled it)
        for i in range(self._pyaudio.get_device_count()):
            try:
                info = self._pyaudio.get_device_info_by_index(i)
                if name_match in info.get('name', '').lower():
                    return i, i
            except Exception:
                continue

        return None, None

    @staticmethod
    def _find_alsa_card(name_match):
        """Find ALSA card number by scanning /proc/asound/cards."""
        try:
            with open('/proc/asound/cards') as f:
                for line in f:
                    if name_match in line.lower():
                        # Line format: " 2 [AllInOneCable  ]: USB-Audio - ..."
                        card_num = line.strip().split()[0]
                        if card_num.isdigit():
                            return int(card_num)
        except Exception:
            pass
        return None

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

    # _restart_audio_input removed — stream lifecycle is fully owned by _rx_reader_loop

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

