"""TH-D75 Radio Plugin — Bluetooth radio via remote proxy.

Consolidates D75CATClient (TCP CAT control with BT proxy) and D75AudioSource
(TCP audio with 8kHz→48kHz resampling) into a single plugin.

The remote BT proxy (scripts/remote_bt_proxy.py) runs on a separate machine
with a Bluetooth adapter. The plugin connects to it via two TCP ports:
- CAT port (default 9750): text commands and status polling
- Audio port (default 9751): raw 8kHz PCM streaming

See docs/mixer-v2-design.md for architecture.
"""

import json
import math
import os
import queue as _queue_mod
import socket
import threading
import time

import numpy as np

from audio_sources import AudioProcessor
from gateway_link import RadioPlugin

_math_mod = math
_json_mod = json


class D75Plugin(RadioPlugin):
    """TH-D75 Bluetooth radio plugin.

    Connects to a remote BT proxy via TCP for CAT control and audio streaming.
    Handles 8kHz→48kHz upsampling (6x linear interpolation), audio processing,
    and fire-and-forget PTT.
    """

    name = "d75"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
        "frequency": True,
        "ctcss": False,
        "power": True,
        "rx_gain": False,
        "tx_gain": False,
        "smeter": True,
        "status": True,
    }

    def __init__(self):
        super().__init__()
        self._config = None

        # CAT TCP connection
        self._host = 'localhost'
        self._cat_port = 9750
        self._audio_port = 9751
        self._password = ''
        self._verbose = False
        self._sock = None
        self._buf = b''
        self._sock_lock = threading.Lock()
        self._last_activity = 0
        self._stop = False
        self._poll_thread = None
        self._poll_paused = False
        self._bt_stopped = False
        self._btstart_in_progress = False

        # Radio state (from !status JSON)
        self._connected = False
        self._serial_connected = False
        self._frequency = {}
        self._mode = {}
        self._squelch = {}
        self._power = {}
        self._signal = {}
        self._freq_info = {}
        self._memory_mode = {}
        self._channel = {}
        self._active_band = 0
        self._dual_band = 0
        self._bluetooth = False
        self._backlight = 0
        self._transmitting = False
        self._tnc = [0, 0]
        self._beacon_type = 0
        self._gps_data = None
        self._model = ''
        self._serial_number = ''
        self._firmware = ''
        self._af_gain = -1
        self._battery_level = -1
        self._ptt_on_state = False

        # Audio RX state
        self._audio_sock = None
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = 4800  # set in setup
        self._reader_running = False
        self._reader_thread = None
        self.server_connected = False
        self.audio_level = 0      # RX level
        self.tx_audio_level = 0   # TX level
        self.audio_boost = 1.0
        self._reconnect_interval = 5.0

        # Processing
        self._processor = None

        # Bus compat
        self.enabled = True
        self.ptt_control = False
        self.priority = 2
        self.volume = 1.0
        self.duck = True
        self.sdr_priority = 2
        self.muted = False

    def setup(self, config):
        """Initialize D75 plugin: connect CAT, start audio reader, init processing."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._host = str(getattr(config, 'D75_HOST', 'localhost'))
        self._cat_port = int(getattr(config, 'D75_PORT', 9750))
        self._audio_port = int(getattr(config, 'D75_AUDIO_PORT', 9751))
        self._password = str(getattr(config, 'D75_PASSWORD', ''))
        self._verbose = getattr(config, 'VERBOSE_LOGGING', False)
        self._reconnect_interval = float(getattr(config, 'D75_RECONNECT_INTERVAL', 5.0))
        self._chunk_bytes = getattr(config, 'AUDIO_CHUNK_SIZE', 2400) * 2

        # Bus compat
        self.duck = getattr(config, 'D75_AUDIO_DUCK', True)
        self.sdr_priority = int(getattr(config, 'D75_AUDIO_PRIORITY', 2))
        self.audio_boost = float(getattr(config, 'D75_AUDIO_BOOST', 1.0))
        self.muted = False  # unmuted for bus routing

        # Processing
        self._processor = AudioProcessor("d75", config)
        self._sync_processor()

        # Connect CAT TCP
        if not self._connect_cat():
            print(f"  D75 CAT: TCP connection failed to {self._host}:{self._cat_port}")
            # Don't fail setup — polling thread will retry
        else:
            print(f"  D75 CAT: connected to {self._host}:{self._cat_port}")

        # Start polling thread (handles reconnect and btstart)
        self._start_polling()

        # Start audio reader thread
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._audio_reader_func, daemon=True, name="D75-audio")
        self._reader_thread.start()
        print(f"  D75 audio: connecting to {self._host}:{self._audio_port}")

        return True

    def teardown(self):
        self._stop = True
        self._reader_running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)
        if self._audio_sock:
            try:
                self._audio_sock.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._cleanup_socket()

    # -- Standard plugin interface --

    def get_audio(self, chunk_size=None):
        """Pull 48kHz PCM from the upsampled audio queue."""
        if not self.enabled:
            return None, False
        if not self.server_connected and not self._sub_buffer:
            return None, False

        cb = self._chunk_bytes
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        if self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering
        arr = np.frombuffer(raw, dtype=np.int16)
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20.0 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = float(getattr(self._config, 'D75_AUDIO_DISPLAY_GAIN', 1.0))
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            if self.audio_boost != 1.0:
                arr = np.clip(farr * self.audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        # Processing
        if self._processor:
            self._sync_processor()
            raw = self._processor.process(raw)

        return raw, False

    def put_audio(self, pcm_48k):
        """Downsample 48kHz→8kHz and send to D75 for BT TX."""
        if not self._audio_sock or not self.server_connected:
            return
        try:
            arr = np.frombuffer(pcm_48k, dtype=np.int16)
            # TX level metering
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr))) if len(farr) > 0 else 0.0
            if rms > 0:
                db = 20.0 * _math_mod.log10(rms / 32767.0)
                level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                level = 0
            if level > self.tx_audio_level:
                self.tx_audio_level = int(level)
            else:
                self.tx_audio_level = int(self.tx_audio_level * 0.7 + level * 0.3)
            arr_8k = arr[::6]
            self._audio_sock.sendall(arr_8k.tobytes())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # Backward compat alias
    def write_tx_audio(self, pcm):
        self.put_audio(pcm)

    def execute(self, cmd):
        """Handle commands."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        elif action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}
        elif action == 'ptt':
            state = bool(cmd.get('state', False))
            return self._set_ptt(state)
        elif action == 'btstart':
            return self._send_cat_cmd("!btstart")
        elif action == 'btstop':
            self._bt_stopped = True
            return self._send_cat_cmd("!btstop")
        elif action == 'reconnect':
            self._disconnect_for_reconnect()
            ok = self._connect_cat()
            return {"ok": ok}
        elif action in ('freq', 'memcall', 'beacon', 'tnc', 'vol', 'squelch'):
            # Pass through to CAT
            cat_cmd = cmd.get('cat_cmd', '')
            if cat_cmd:
                return self._send_cat_cmd(cat_cmd)
            return {"ok": False, "error": "no cat_cmd provided"}
        elif action == 'boost':
            self.audio_boost = max(0.0, min(5.0, float(cmd.get('value', 1.0))))
            return {"ok": True}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return full status dict."""
        mm_names = {0: 'VFO', 1: 'Memory', 2: 'Call', 3: 'DV'}
        tnc_names = {0: 'Off', 1: 'APRS', 2: 'KISS'}
        beacon_names = {0: 'Manual', 1: 'PTT', 2: 'Auto', 3: 'SmartBeacon'}
        d = {
            'plugin': self.name,
            'connected': self._connected and self._serial_connected,
            'tcp_connected': self._connected,
            'serial_connected': self._serial_connected,
            'btstart_in_progress': self._btstart_in_progress,
            'model': self._model,
            'serial_number': self._serial_number,
            'firmware': self._firmware,
            'active_band': self._active_band,
            'dual_band': self._dual_band,
            'bluetooth': self._bluetooth,
            'transmitting': self._transmitting,
            'backlight': self._backlight,
            'tnc': tnc_names.get(self._tnc[0] if self._tnc else 0, '?'),
            'tnc_band': self._tnc[1] if len(self._tnc) > 1 else 0,
            'beacon_type': beacon_names.get(self._beacon_type, '?'),
            'battery_level': self._battery_level,
            'gps_data': self._gps_data,
            'af_gain': self._af_gain,
            'audio_connected': self.server_connected,
            'audio_level': self.audio_level,
            'audio_boost': int(self.audio_boost * 100),
            'muted': self.muted,
        }
        for band in [0, 1]:
            d[f'band_{band}'] = {
                'frequency': self._frequency.get(band, ''),
                'mode': self._mode.get(band, ''),
                'squelch': self._squelch.get(band, 0),
                'power': self._power.get(band, ''),
                'signal': self._signal.get(band, 0),
                'freq_info': self._freq_info.get(band),
                'memory_mode': mm_names.get(self._memory_mode.get(band, 0), '?'),
                'channel': self._channel.get(band, ''),
            }
        return d

    # -- Backward compat for gateway_core --

    @property
    def input_stream(self):
        return self.server_connected

    def get_radio_state(self):
        """Backward compat — returns same dict as old D75CATClient."""
        return self.get_status()

    def send_command(self, cmd, timeout=3.0):
        """Backward compat — send CAT command with poll pausing."""
        if not self._sock or not self._connected:
            return None
        self._poll_paused = True
        time.sleep(0.3)
        with self._sock_lock:
            self._buf = b''
            if self._sock:
                self._sock.settimeout(0.1)
                try:
                    while True:
                        d = self._sock.recv(4096)
                        if not d:
                            break
                except (socket.timeout, BlockingIOError, OSError):
                    pass
        try:
            return self._send_cmd(cmd, timeout=timeout)
        finally:
            self._poll_paused = False

    def reset(self):
        """Force-close audio connection to trigger reconnect."""
        sock = self._audio_sock
        self._audio_sock = None
        self.server_connected = False
        self._sub_buffer = b''
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    # -- Internal: CAT TCP --

    def _connect_cat(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self._host, self._cat_port))
            self._sock.sendall(f"!pass {self._password}\n".encode())
            resp = self._recv_line(timeout=5.0)
            if resp and 'Login Successful' in resp:
                self._connected = True
                self._last_activity = time.monotonic()
                return True
            else:
                self._cleanup_socket()
                return False
        except Exception as e:
            if self._verbose:
                print(f"  [D75 CAT] Connect error: {e}")
            self._cleanup_socket()
            return False

    def _cleanup_socket(self):
        if self._sock:
            try:
                self._sock.sendall(b'!exit\n')
            except Exception:
                pass
            time.sleep(0.1)
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False
        self._buf = b''

    def _disconnect_for_reconnect(self):
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False
        self._buf = b''

    def _recv_line(self, timeout=2.0):
        if not self._sock:
            return None
        self._sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if b'\n' in self._buf:
                idx = self._buf.index(b'\n')
                line = self._buf[:idx].decode('utf-8', errors='ignore').strip()
                self._buf = self._buf[idx + 1:]
                self._last_activity = time.monotonic()
                return line
            try:
                data = self._sock.recv(4096)
                if not data:
                    self._connected = False
                    return None
                self._buf += data
            except socket.timeout:
                continue
            except Exception:
                self._connected = False
                return None
        return None

    def _send_cmd(self, cmd, timeout=3.0):
        if not self._sock:
            return None
        with self._sock_lock:
            try:
                self._buf = b''
                self._sock.sendall(f"{cmd}\n".encode())
                self._last_activity = time.monotonic()
                resp = self._recv_line(timeout=timeout)
                if resp and 'Unauthorized' in resp:
                    self._sock.sendall(f"!pass {self._password}\n".encode())
                    auth_resp = self._recv_line(timeout=2.0)
                    if auth_resp and 'Login Successful' in auth_resp:
                        self._sock.sendall(f"{cmd}\n".encode())
                        self._last_activity = time.monotonic()
                        resp = self._recv_line(timeout=3.0)
                return resp
            except (ConnectionResetError, BrokenPipeError, OSError):
                self._connected = False
                return None
            except Exception:
                self._connected = False
                return None

    def _send_cat_cmd(self, cmd):
        """Send a CAT command and return result dict."""
        resp = self.send_command(cmd)
        if resp is not None:
            return {"ok": True, "response": resp}
        return {"ok": False, "error": "no response"}

    # -- Internal: PTT --

    def _set_ptt(self, state_on):
        """Fire-and-forget PTT via raw socket write (avoids poll thread contention)."""
        if not self._sock or not self._connected:
            return {"ok": False, "error": "not connected"}
        if state_on == self._ptt_on_state:
            return {"ok": True}
        cmd = "!ptt on" if state_on else "!ptt off"
        try:
            self._sock.sendall(f"{cmd}\n".encode())
            self._ptt_on_state = state_on
            self._transmitting = state_on
            return {"ok": True, "ptt": state_on}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def ptt_on(self):
        """Backward compat."""
        self._set_ptt(True)

    def ptt_off(self):
        """Backward compat."""
        self._set_ptt(False)

    # -- Internal: polling --

    def _start_polling(self):
        self._stop = False
        self._poll_thread = threading.Thread(
            target=self._poll_thread_func, daemon=True, name="D75-CAT-poll")
        self._poll_thread.start()

    def _poll_thread_func(self):
        _reconnect_interval = 5.0
        _reconnect_count = 0
        _last_btstart_attempt = 0
        _BTSTART_RETRY_INTERVAL = 15.0
        while not self._stop:
            if not self._connected or not self._sock:
                _reconnect_count += 1
                if _reconnect_count <= 3 or _reconnect_count % 10 == 0:
                    print(f"\n[D75 CAT] Reconnecting to {self._host}:{self._cat_port} (#{_reconnect_count})...")
                self._disconnect_for_reconnect()
                if self._connect_cat():
                    _reconnect_count = 0
                    print(f"[D75 CAT] Reconnected TCP")
                    time.sleep(1)
                    try:
                        self._poll_state()
                    except Exception:
                        pass
                    if not self._serial_connected and not self._bt_stopped:
                        print(f"[D75 CAT] Serial not connected — requesting btstart...")
                        self._btstart_in_progress = True
                        self._btstart_time = time.monotonic()
                        _last_btstart_attempt = time.monotonic()
                        try:
                            resp = self._send_cmd("!btstart")
                            print(f"[D75 CAT] btstart response: {resp}")
                        except Exception as e:
                            print(f"[D75 CAT] btstart error: {e}")
                else:
                    for _ in range(int(_reconnect_interval * 10)):
                        if self._stop:
                            return
                        time.sleep(0.1)
                    continue

            if not self._poll_paused:
                try:
                    self._poll_state()
                except OSError:
                    self._connected = False
                except Exception:
                    pass
                if (self._connected and not self._serial_connected
                        and not self._bt_stopped and not self._btstart_in_progress
                        and time.monotonic() - _last_btstart_attempt >= _BTSTART_RETRY_INTERVAL):
                    _last_btstart_attempt = time.monotonic()
                    self._btstart_in_progress = True
                    self._btstart_time = time.monotonic()
                    try:
                        resp = self._send_cmd("!btstart")
                    except Exception:
                        pass

            for _ in range(20):
                if self._stop:
                    return
                time.sleep(0.1)

    def _poll_state(self):
        resp = self._send_cmd("!status")
        if not resp:
            return
        try:
            data = _json_mod.loads(resp)
            if 'serial_connected' in data:
                self._serial_connected = bool(data['serial_connected'])
            else:
                self._serial_connected = bool(data.get('model_id'))
            if self._serial_connected:
                self._btstart_in_progress = False
            elif self._btstart_in_progress and hasattr(self, '_btstart_time'):
                if time.monotonic() - self._btstart_time > 30.0:
                    self._btstart_in_progress = False
            self._model = data.get('model_id', '')
            self._serial_number = data.get('serial_number', '')
            self._firmware = data.get('fw_version', '')
            self._af_gain = data.get('af_gain', -1)
            self._active_band = data.get('active_band', 0)
            self._dual_band = data.get('dual_band', 0)
            self._bluetooth = data.get('bluetooth', False)
            self._backlight = data.get('backlight', 0)
            self._transmitting = data.get('transmitting', False)
            self._tnc = data.get('tnc', [0, 0])
            self._beacon_type = data.get('beacon_type', 0)
            self._battery_level = data.get('battery_level', -1)
            self._gps_data = data.get('gps_data')
            for band in [0, 1]:
                key = f'band_{band}'
                if key in data:
                    b = data[key]
                    self._frequency[band] = b.get('frequency', '')
                    self._mode[band] = b.get('mode', 0)
                    self._squelch[band] = b.get('squelch', 0)
                    self._power[band] = b.get('power', 0)
                    self._signal[band] = b.get('s_meter', 0)
                    self._freq_info[band] = b.get('freq_info')
                    self._memory_mode[band] = b.get('memory_mode', 0)
                    self._channel[band] = b.get('channel', '')
        except Exception:
            pass

    # -- Internal: audio reader --

    def _audio_reader_func(self):
        """Connect to D75 audio port, read 8kHz PCM, upsample to 48kHz."""
        samples_8k = (self._chunk_bytes // 2) // 6  # 400 samples
        bytes_8k = samples_8k * 2
        _out_len = samples_8k * 6
        _prev_last = np.float32(0)

        while self._reader_running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self._reconnect_interval)
                sock.connect((self._host, self._audio_port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(2.0)
                self._audio_sock = sock
                self.server_connected = True
                _prev_last = np.float32(0)
                print(f"\n[D75] Audio connected to {self._host}:{self._audio_port}")

                raw_buf = b''
                while self._reader_running:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    raw_buf += data

                    while len(raw_buf) >= bytes_8k:
                        chunk_8k = raw_buf[:bytes_8k]
                        raw_buf = raw_buf[bytes_8k:]

                        arr_8k = np.frombuffer(chunk_8k, dtype=np.int16).astype(np.float32)
                        extended = np.concatenate(([_prev_last], arr_8k))
                        _prev_last = arr_8k[-1]
                        idx_ext = np.linspace(0, len(extended) - 1, _out_len).astype(np.float32)
                        arr_48k = np.interp(idx_ext, np.arange(len(extended), dtype=np.float32), extended)
                        pcm_48k = np.clip(arr_48k, -32768, 32767).astype(np.int16).tobytes()

                        # Track level in reader thread (works without bus)
                        try:
                            _rms = float(np.sqrt(np.mean(arr_48k * arr_48k))) if len(arr_48k) > 0 else 0.0
                            _lv = int(max(0, min(100, (20.0 * math.log10(_rms / 32767.0) + 60) * (100 / 60)))) if _rms > 0 else 0
                            if _lv > self.audio_level:
                                self.audio_level = int(_lv)
                            else:
                                self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
                        except Exception:
                            pass
                        try:
                            self._chunk_queue.put_nowait(pcm_48k)
                        except _queue_mod.Full:
                            try:
                                self._chunk_queue.get_nowait()
                            except _queue_mod.Empty:
                                pass
                            try:
                                self._chunk_queue.put_nowait(pcm_48k)
                            except _queue_mod.Full:
                                pass

            except socket.timeout:
                pass
            except Exception as e:
                if self._reader_running and self._verbose:
                    print(f"\n[D75] Audio connection error: {e}")
            finally:
                self.server_connected = False
                self._audio_sock = None
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if self._reader_running:
                    time.sleep(self._reconnect_interval)

    # -- Internal: processing sync --

    def _sync_processor(self):
        if not self._processor or not self._config:
            return
        p = self._processor
        p.enable_noise_gate = getattr(self._config, 'D75_PROC_ENABLE_NOISE_GATE', False)
        p.gate_threshold = getattr(self._config, 'D75_PROC_NOISE_GATE_THRESHOLD', -40)
        p.gate_attack = getattr(self._config, 'D75_PROC_NOISE_GATE_ATTACK', 0.01)
        p.gate_release = getattr(self._config, 'D75_PROC_NOISE_GATE_RELEASE', 0.1)
        p.enable_hpf = getattr(self._config, 'D75_PROC_ENABLE_HPF', True)
        p.hpf_cutoff = getattr(self._config, 'D75_PROC_HPF_CUTOFF', 300)
        p.enable_lpf = getattr(self._config, 'D75_PROC_ENABLE_LPF', True)
        p.lpf_cutoff = getattr(self._config, 'D75_PROC_LPF_CUTOFF', 3000)
        p.enable_notch = getattr(self._config, 'D75_PROC_ENABLE_NOTCH', False)
        p.notch_freq = getattr(self._config, 'D75_PROC_NOTCH_FREQ', 1000)
        p.notch_q = getattr(self._config, 'D75_PROC_NOTCH_Q', 10.0)
