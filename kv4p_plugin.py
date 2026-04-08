"""KV4P HT Radio Plugin — USB serial radio with Opus codec.

Consolidates KV4PCATClient (serial control) and KV4PAudioSource (Opus RX/TX,
adaptive PLL resampler, DSP chain) into a single plugin.

See docs/mixer-v2-design.md for architecture.
"""

import collections
import os
import sys
import threading
import time

def _update_config_key(key, value):
    """Update a single key in gateway_config.txt using anchored sed."""
    import subprocess
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gateway_config.txt')
    if not os.path.exists(config_path):
        return
    # Escape value for sed
    val_str = str(value)
    try:
        subprocess.run(
            ['sed', '-i', f's/^{key} = .*/{key} = {val_str}/', config_path],
            capture_output=True, timeout=5)
    except Exception:
        pass

import numpy as np

from audio_util import AudioProcessor, pcm_level, pcm_rms, pcm_db
from gateway_link import RadioPlugin


# ---------------------------------------------------------------------------
# KV4PPlugin — the main plugin class
# ---------------------------------------------------------------------------

class KV4PPlugin(RadioPlugin):
    """KV4P HT radio plugin.

    USB serial radio with Opus-encoded audio. Handles RX decoding (Opus→PCM
    with adaptive PLL resampling for ESP32 clock offset), TX encoding
    (PCM→Opus), serial control (frequency, CTCSS, squelch, power, PTT),
    and audio processing (gate, HPF, LPF, notch).
    """

    name = "kv4p"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
        "frequency": True,
        "ctcss": True,
        "power": True,
        "rx_gain": False,
        "tx_gain": False,
        "smeter": True,
        "status": True,
    }

    def __init__(self):
        super().__init__()
        self._config = None
        self._port = None
        self._verbose = False

        # Serial control (KV4PRadio instance)
        self._radio = None
        self._connected = False
        self._serial_connected = False
        self._lock = threading.Lock()
        self._stop = False
        self._poll_thread = None

        # Radio state
        self._frequency = 146.520
        self._tx_frequency = 146.520
        self._squelch = 4
        self._bandwidth = 1
        self._ctcss_tx = 0
        self._ctcss_rx = 0
        self._high_power = True
        self._signal = 0
        self._transmitting = False
        self._firmware_version = 0
        self._rf_module = 'VHF'
        self._smeter_enabled = False
        self._ptt_on_state = False

        # Audio codec
        self._decoder = None
        self._encoder = None
        self._dc_remover = None
        self._dc_remover_frame = None
        self._vol_ramp = None

        # Stream trace (set by gateway)
        self._stream_trace = None

        # Audio RX state
        self._chunk_queue = collections.deque(maxlen=16)
        self._sub_buffer = b''
        self._chunk_bytes = 4800  # 2400 samples × 2 bytes, set properly in setup
        self._resample_ratio = 1.132
        self._resample_pos = 0.0
        self._buf_max = 0
        self._was_active = False
        self.server_connected = False
        self.audio_level = 0      # RX level
        self.tx_audio_level = 0   # TX level
        self.audio_boost = 1.0

        # TX state
        self._tx_buf = b''

        # Processing
        self._processor = None

        # Bus compat attributes
        self.enabled = True
        self.ptt_control = False
        self.priority = 2
        self.volume = 1.0
        self.duck = True
        self.sdr_priority = 2
        self.muted = False

        # Trace instrumentation
        self._trace_rx_frames = 0
        self._trace_rx_bytes = 0
        self._trace_decode_errors = 0
        self._trace_queue_drops = 0
        self._trace_sub_buf_before = 0
        self._trace_sub_buf_after = 0
        self._trace_returned_data = False
        self._trace_pcm_rms = 0.0
        self._trace_tx_frames = 0
        self._trace_tx_dropped = 0
        self._trace_tx_input_rms = 0.0
        self._trace_tx_errors = 0

        # Instrumentation
        self._inst_count = 0
        self._inst_returns = 0
        self._inst_nones = 0
        self._inst_trims = 0
        self._inst_t0 = 0
        self._inst_intervals = []
        self._inst_sub_sizes = []

        # Recording hook
        self._recording_file = None

    def setup(self, config):
        """Initialize KV4P plugin: open serial, init Opus codec, start polling."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._verbose = getattr(config, 'VERBOSE_LOGGING', False)
        self._port = str(getattr(config, 'KV4P_PORT', '/dev/ttyUSB0'))

        # Audio config
        chunk_size = getattr(config, 'AUDIO_CHUNK_SIZE', 2400)
        channels = getattr(config, 'AUDIO_CHANNELS', 1)
        self._chunk_bytes = chunk_size * channels * 2
        self._buf_max = self._chunk_bytes * 6

        # Bus compat from config
        self.duck = getattr(config, 'KV4P_AUDIO_DUCK', True)
        self.sdr_priority = int(getattr(config, 'KV4P_AUDIO_PRIORITY', 2))
        self.audio_boost = float(getattr(config, 'KV4P_AUDIO_BOOST', 1.0))
        self.muted = False  # unmuted for bus routing

        # Processing chain
        self._processor = AudioProcessor("kv4p", config)
        self._sync_processor()

        # Init Opus codec + DSP
        if not self._setup_codec():
            return False

        # Open serial connection
        if not self._connect_radio():
            return False

        # Start health polling
        self._start_polling()

        return True

    def teardown(self):
        """Stop polling, close serial."""
        self._stop = True
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)
        if self._radio:
            try:
                self._radio.close()
            except Exception:
                pass
        self._connected = False
        self._serial_connected = False

    # -- Standard plugin interface (bus calls these) --

    def get_audio(self, chunk_size=None):
        """Pull decoded, resampled, processed PCM from the Opus RX stream."""
        now = time.monotonic()
        self._inst_count += 1
        if self._inst_t0 > 0:
            self._inst_intervals.append(now - self._inst_t0)
        self._inst_t0 = now

        if not self.enabled or not self.server_connected:
            self._trace_returned_data = False
            return None, False

        # Drain queue into sub-buffer
        while self._chunk_queue:
            self._sub_buffer += self._chunk_queue.popleft()

        self._trace_sub_buf_before = len(self._sub_buffer)
        self._inst_sub_sizes.append(len(self._sub_buffer))

        # Adaptive PLL: adjust resample ratio to keep buffer near target
        buf_target = self._chunk_bytes * 3
        buf_now = len(self._sub_buffer)
        buf_error = (buf_now - buf_target) / buf_target if buf_target > 0 else 0
        adjustment = buf_error * 0.002
        self._resample_ratio = max(0.95, min(1.25, self._resample_ratio + adjustment))

        self._inst_returns += 1

        # Streaming resampler: vectorized linear interpolation
        n_input_samples = len(self._sub_buffer) // 2
        out_samples_needed = self._chunk_bytes // 2

        input_needed = int(self._resample_pos + out_samples_needed * self._resample_ratio) + 2
        if n_input_samples < input_needed:
            self._inst_nones += 1
            self._trace_returned_data = False
            self._trace_sub_buf_after = len(self._sub_buffer)
            self._trace_pcm_rms = 0.0
            self.audio_level = int(self.audio_level * 0.9)
            return None, False

        in_samples = np.frombuffer(self._sub_buffer, dtype=np.int16).astype(np.float32)
        positions = self._resample_pos + np.arange(out_samples_needed) * self._resample_ratio
        indices = positions.astype(np.intp)
        fracs = positions - indices
        np.clip(indices, 0, n_input_samples - 2, out=indices)
        out = in_samples[indices] * (1.0 - fracs) + in_samples[indices + 1] * fracs

        consumed_samples = int(positions[-1]) + 1
        self._resample_pos = positions[-1] + self._resample_ratio - consumed_samples
        self._sub_buffer = self._sub_buffer[consumed_samples * 2:]

        # Cap buffer to bound latency
        if len(self._sub_buffer) > self._buf_max:
            excess = len(self._sub_buffer) - self._buf_max
            excess = (excess + 1) & ~1
            self._sub_buffer = self._sub_buffer[excess:]
            self._resample_pos = 0.0
            self._inst_trims += 1

        self._trace_sub_buf_after = len(self._sub_buffer)
        self._trace_returned_data = True
        pcm_data = np.clip(out, -32768, 32767).astype(np.int16).tobytes()

        # Mute check
        if self.muted:
            self.audio_level = int(self.audio_level * 0.7)
            return None, False

        # DC offset removal
        if self._dc_remover:
            pcm_data = self._dc_remover.process(pcm_data)

        # Level metering
        try:
            display_gain = float(getattr(self._config, 'KV4P_AUDIO_DISPLAY_GAIN', 1.0))
            self.audio_level = pcm_level(pcm_data, self.audio_level, gain=display_gain)
        except Exception:
            pass

        # Audio boost
        if self.audio_boost != 1.0:
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            pcm_data = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Audio processing (gate, HPF, LPF, notch)
        if self._processor:
            self._sync_processor()
            pcm_data = self._processor.process(pcm_data)

        # Trace RMS
        try:
            self._trace_pcm_rms = pcm_rms(pcm_data)
        except Exception:
            self._trace_pcm_rms = 0.0

        # Recording hook
        if self._recording_file:
            try:
                self._recording_file.write(pcm_data)
            except Exception:
                pass

        return pcm_data, False

    def put_audio(self, pcm_48k):
        """Encode 48kHz PCM to Opus and send to radio for TX."""
        if not self._encoder or not self._radio:
            return
        _st = self._stream_trace
        if _st:
            _st.record('kv4p_tx', 'put_audio', pcm_48k, len(self._tx_buf))
        try:
            frame_bytes = 1920 * 2  # 40ms at 48kHz mono
            self._tx_buf += pcm_48k
            buf = self._tx_buf
            try:
                self._trace_tx_input_rms = pcm_rms(pcm_48k)
                self.tx_audio_level = pcm_level(pcm_48k, self.tx_audio_level)
            except Exception:
                pass
            frames_sent = 0
            while len(buf) >= frame_bytes:
                try:
                    opus_frame = self._encoder.encode(buf[:frame_bytes], 1920)
                    self._radio.send_audio(opus_frame)
                    frames_sent += 1
                except Exception:
                    self._trace_tx_errors += 1
                buf = buf[frame_bytes:]
            self._tx_buf = buf
            self._trace_tx_frames += frames_sent
            self._trace_tx_dropped += len(buf)
        except Exception:
            pass

    # Frequency ranges per RF module type
    _FREQ_RANGES = {
        'SA818_VHF': (134.0, 174.0),
        'SA818_UHF': (400.0, 480.0),
    }

    def _validate_freq(self, freq):
        """Check if frequency is within the RF module's range. Returns error string or None."""
        lo, hi = self._FREQ_RANGES.get(self._rf_module, (0, 9999))
        if not (lo <= freq <= hi):
            return f"{freq:.4f} MHz out of range for {self._rf_module} ({lo:.0f}-{hi:.0f} MHz)"
        return None

    def execute(self, cmd):
        """Handle commands: freq, squelch, ctcss, bandwidth, power, boost, ptt, mute, status."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'freq':
            freq = float(cmd.get('frequency', self._frequency))
            tx_freq = float(cmd.get('tx_frequency', 0))
            err = self._validate_freq(freq)
            if err:
                return {"ok": False, "error": err}
            if tx_freq > 0:
                err = self._validate_freq(tx_freq)
                if err:
                    return {"ok": False, "error": f"TX {err}"}
            self._frequency = freq
            self._tx_frequency = tx_freq if tx_freq > 0 else freq
            self._apply_group()
            self._persist()
            return {"ok": True}
        elif action == 'squelch':
            self._squelch = max(0, min(8, int(cmd.get('level', self._squelch))))
            self._apply_group()
            self._persist()
            return {"ok": True}
        elif action == 'ctcss':
            if 'tx' in cmd:
                self._ctcss_tx = int(cmd['tx'])
            if 'rx' in cmd:
                self._ctcss_rx = int(cmd['rx'])
            self._apply_group()
            self._persist()
            return {"ok": True}
        elif action == 'bandwidth':
            self._bandwidth = 1 if cmd.get('wide', True) else 0
            self._apply_group()
            self._persist()
            return {"ok": True}
        elif action == 'power':
            self._high_power = bool(cmd.get('high', True))
            if self._radio:
                self._radio.set_power(self._high_power)
            self._persist()
            return {"ok": True}
        elif action == 'boost':
            self.audio_boost = max(0.0, min(5.0, float(cmd.get('value', 1.0))))
            self._persist()
            return {"ok": True}
        elif action == 'ptt':
            state = bool(cmd.get('state', False))
            return self._set_ptt(state)
        elif action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}
        elif action == 'connect':
            return {"ok": self._connect_radio()}
        elif action == 'reconnect':
            if self._radio:
                try:
                    self._radio.close()
                except Exception:
                    pass
            self._connected = False
            self._serial_connected = False
            return {"ok": self._connect_radio()}
        elif action == 'testtone':
            self._send_test_tone(cmd)
            return {"ok": True, "msg": "test tone started"}
        elif action == 'capture':
            return self._handle_capture(cmd)
        elif action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return full status dict for web UI."""
        d = {
            'plugin': self.name,
            'connected': self._connected,
            'serial_connected': self._serial_connected,
            'frequency': f'{self._frequency:.6f}',
            'tx_frequency': f'{self._tx_frequency:.6f}',
            'squelch': self._squelch,
            'bandwidth': self._bandwidth,
            'ctcss_tx': self._ctcss_tx,
            'ctcss_rx': self._ctcss_rx,
            'high_power': self._high_power,
            'signal': self._signal,
            'transmitting': self._transmitting,
            'firmware_version': self._firmware_version,
            'rf_module': self._rf_module,
            'smeter_enabled': self._smeter_enabled,
            'audio_connected': self.server_connected,
            'audio_level': self.audio_level,
            'audio_boost': int(self.audio_boost * 100),
            'muted': self.muted,
        }
        return d

    # -- Backward compat for gateway_core --

    @property
    def input_stream(self):
        return self.server_connected and self._connected

    def get_trace_snapshot(self):
        """Return trace state dict and reset per-tick counters."""
        snap = {
            'rx_frames': self._trace_rx_frames,
            'rx_bytes': self._trace_rx_bytes,
            'decode_errors': self._trace_decode_errors,
            'queue_drops': self._trace_queue_drops,
            'sub_buf_before': self._trace_sub_buf_before,
            'sub_buf_after': self._trace_sub_buf_after,
            'returned_data': self._trace_returned_data,
            'pcm_rms': self._trace_pcm_rms,
            'queue_len': len(self._chunk_queue),
            'tx_frames': self._trace_tx_frames,
            'tx_dropped': self._trace_tx_dropped,
            'tx_input_rms': self._trace_tx_input_rms,
            'tx_errors': self._trace_tx_errors,
        }
        self._trace_rx_frames = 0
        self._trace_rx_bytes = 0
        self._trace_decode_errors = 0
        self._trace_queue_drops = 0
        self._trace_tx_frames = 0
        self._trace_tx_dropped = 0
        self._trace_tx_input_rms = 0.0
        self._trace_tx_errors = 0
        return snap

    # Alias for gateway_core TX routing
    def write_tx_audio(self, pcm):
        """Alias for put_audio (backward compat with gateway_core TX routing)."""
        self.put_audio(pcm)

    # -- Internal: serial connection --

    def _connect_radio(self):
        """Open serial connection to KV4P HT."""
        try:
            sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
            from kv4p.radio import KV4PRadio
            self._radio = KV4PRadio(self._port)
            self._radio.on_rx_audio = self._on_rx_audio
            self._radio.on_smeter = self._on_smeter
            self._radio.on_phys_ptt = self._on_phys_ptt

            ver = self._radio.open(handshake_timeout=10)
            self._connected = True
            self._serial_connected = True

            if ver:
                self._firmware_version = ver.firmware_version
                self._rf_module = ver.rf_module_type.name if hasattr(ver.rf_module_type, 'name') else 'VHF'

            # Apply initial config
            self._frequency = float(getattr(self._config, 'KV4P_FREQ', 146.520))
            tx_freq = float(getattr(self._config, 'KV4P_TX_FREQ', 0))
            self._tx_frequency = tx_freq if tx_freq > 0 else self._frequency
            for _label, _f in [('RX', self._frequency), ('TX', self._tx_frequency)]:
                _err = self._validate_freq(_f)
                if _err:
                    print(f"  [KV4P] WARNING: {_label} {_err}")
            self._squelch = int(getattr(self._config, 'KV4P_SQUELCH', 4))
            self._bandwidth = int(getattr(self._config, 'KV4P_BANDWIDTH', 1))
            self._ctcss_tx = int(getattr(self._config, 'KV4P_CTCSS_TX', 0))
            self._ctcss_rx = int(getattr(self._config, 'KV4P_CTCSS_RX', 0))
            self._high_power = bool(getattr(self._config, 'KV4P_HIGH_POWER', True))

            self._apply_group()
            time.sleep(0.3)

            from kv4p.protocol import FiltersConfig
            self._radio.set_filters(FiltersConfig(pre_emphasis=True, highpass=True, lowpass=True))
            self._radio.set_power(self._high_power)

            if getattr(self._config, 'KV4P_SMETER', True):
                self._radio.enable_smeter(True)
                self._smeter_enabled = True

            print(f"  Connected: fw v{self._firmware_version}, {self._rf_module}")
            print(f"  Tuned to {self._frequency:.4f} MHz")
            return True
        except Exception as e:
            print(f"  KV4P connect error: {e}")
            self._connected = False
            self._serial_connected = False
            return False

    def _setup_codec(self):
        """Initialize Opus codec and DSP chain."""
        try:
            sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
            import opuslib
            from kv4p.audio import DCOffsetRemover, VolumeRamp
            self._decoder = opuslib.Decoder(48000, 1)
            self._encoder = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)
            self._dc_remover_frame = DCOffsetRemover(decay_time=0.02, sample_rate=48000)
            self._dc_remover = DCOffsetRemover(decay_time=0.25, sample_rate=48000)
            self._vol_ramp = VolumeRamp(alpha=0.05, threshold=0.7)
            self.server_connected = True
            print("  KV4P Opus codec + DSP initialized")
            return True
        except ImportError:
            print("  opuslib not installed — KV4P audio disabled")
            return False
        except Exception as e:
            print(f"  KV4P codec init error: {e}")
            return False

    # -- Internal: audio callbacks --

    def _on_rx_audio(self, opus_data):
        """Called by KV4PRadio reader thread with Opus RX audio."""
        if not self._decoder or not self.enabled:
            return
        self._trace_rx_frames += 1
        self._trace_rx_bytes += len(opus_data)
        try:
            pcm = self._decoder.decode(opus_data, 1920)
            # Track level in callback (works without bus)
            try:
                self.audio_level = pcm_level(pcm, self.audio_level)
            except Exception:
                pass
            if len(self._chunk_queue) >= self._chunk_queue.maxlen:
                self._chunk_queue.popleft()
                self._trace_queue_drops += 1
            self._chunk_queue.append(pcm)
        except Exception:
            self._trace_decode_errors += 1

    def _on_smeter(self, rssi):
        self._signal = rssi

    def _on_phys_ptt(self, pressed):
        if self._verbose:
            print(f"\n[KV4P] Physical PTT {'pressed' if pressed else 'released'}")

    # -- Internal: persist + radio control --

    def _persist(self):
        """Save current settings back to gateway_config.txt."""
        _update_config_key('KV4P_FREQ', f'{self._frequency:.6f}')
        _update_config_key('KV4P_TX_FREQ', f'{self._tx_frequency:.6f}')
        _update_config_key('KV4P_SQUELCH', self._squelch)
        _update_config_key('KV4P_BANDWIDTH', self._bandwidth)
        _update_config_key('KV4P_CTCSS_TX', self._ctcss_tx)
        _update_config_key('KV4P_CTCSS_RX', self._ctcss_rx)
        _update_config_key('KV4P_HIGH_POWER', str(self._high_power).lower())

    def _apply_group(self):
        """Send current frequency/tone/squelch config to radio."""
        if not self._radio:
            return
        sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
        from kv4p.protocol import GroupConfig
        group = GroupConfig(
            tx_freq=self._tx_frequency,
            rx_freq=self._frequency,
            bandwidth=self._bandwidth,
            ctcss_tx=self._ctcss_tx,
            squelch=self._squelch,
            ctcss_rx=self._ctcss_rx,
        )
        self._radio.tune(group)
        time.sleep(0.2)
        self._radio.tune(group)

    def ptt_on(self):
        """Backward compat: key the transmitter."""
        self._set_ptt(True)

    def ptt_off(self):
        """Backward compat: unkey the transmitter."""
        self._set_ptt(False)

    def _set_ptt(self, state_on):
        """Key/unkey the transmitter."""
        if not self._radio:
            return {"ok": False, "error": "not connected"}
        if state_on == self._ptt_on_state:
            return {"ok": True}
        try:
            if state_on:
                self._radio.ptt_on()
                self._tx_buf = b''  # Clear stale TX audio
            else:
                self._radio.ptt_off()
            self._ptt_on_state = state_on
            self._transmitting = state_on
            return {"ok": True, "ptt": state_on}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _start_polling(self):
        """Start background health-check thread."""
        self._stop = False
        self._poll_thread = threading.Thread(target=self._poll_func, daemon=True, name="KV4P-poll")
        self._poll_thread.start()

    def _poll_func(self):
        """Monitor connection health, auto-reconnect."""
        while not self._stop:
            for _ in range(20):
                if self._stop:
                    return
                time.sleep(0.1)
            if self._radio and not self._radio._running:
                print("\n[KV4P] Radio connection lost, attempting reconnect...")
                self._connected = False
                self._serial_connected = False
                try:
                    self._radio.close()
                except Exception:
                    pass
                time.sleep(float(getattr(self._config, 'KV4P_RECONNECT_INTERVAL', 5.0)))
                self._connect_radio()

    # -- Internal: processing sync --

    def _sync_processor(self):
        """Sync config flags into the AudioProcessor instance."""
        if not self._processor or not self._config:
            return
        p = self._processor
        p.enable_noise_gate = getattr(self._config, 'KV4P_PROC_ENABLE_NOISE_GATE', False)
        p.gate_threshold = getattr(self._config, 'KV4P_PROC_NOISE_GATE_THRESHOLD', -40)
        p.gate_attack = getattr(self._config, 'KV4P_PROC_NOISE_GATE_ATTACK', 0.01)
        p.gate_release = getattr(self._config, 'KV4P_PROC_NOISE_GATE_RELEASE', 0.1)
        p.enable_hpf = getattr(self._config, 'KV4P_PROC_ENABLE_HPF', True)
        p.hpf_cutoff = getattr(self._config, 'KV4P_PROC_HPF_CUTOFF', 300)
        p.enable_lpf = getattr(self._config, 'KV4P_PROC_ENABLE_LPF', False)
        p.lpf_cutoff = getattr(self._config, 'KV4P_PROC_LPF_CUTOFF', 3000)
        p.enable_notch = getattr(self._config, 'KV4P_PROC_ENABLE_NOTCH', False)
        p.notch_freq = getattr(self._config, 'KV4P_PROC_NOTCH_FREQ', 1000)
        p.notch_q = getattr(self._config, 'KV4P_PROC_NOTCH_Q', 30.0)

    # -- Internal: test tone and capture --

    def _send_test_tone(self, cmd):
        """Generate and send a test tone in a background thread."""
        freq = int(cmd.get('frequency', 440))
        duration = float(cmd.get('duration', 2.0))
        def _run():
            try:
                t = np.arange(int(48000 * duration)) / 48000.0
                tone = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)
                frame_samples = 1920
                self._set_ptt(True)
                time.sleep(0.3)
                for i in range(0, len(tone), frame_samples):
                    if not self._ptt_on_state:
                        break
                    frame = tone[i:i+frame_samples]
                    if len(frame) < frame_samples:
                        frame = np.pad(frame, (0, frame_samples - len(frame)))
                    opus = self._encoder.encode(frame.tobytes(), frame_samples)
                    self._radio.send_audio(opus)
                    time.sleep(0.038)
                time.sleep(0.3)
                self._set_ptt(False)
            except Exception as e:
                print(f"[KV4P] Test tone error: {e}")
                self._set_ptt(False)
        threading.Thread(target=_run, daemon=True, name="KV4P-testtone").start()

    def _handle_capture(self, cmd):
        """Start/stop audio capture to WAV file."""
        action = cmd.get('action', 'toggle')
        if self._recording_file:
            try:
                self._recording_file.close()
            except Exception:
                pass
            self._recording_file = None
            return {"ok": True, "recording": False}
        else:
            try:
                path = cmd.get('path', '/tmp/kv4p_capture.raw')
                self._recording_file = open(path, 'wb')
                return {"ok": True, "recording": True, "path": path}
            except Exception as e:
                return {"ok": False, "error": str(e)}
