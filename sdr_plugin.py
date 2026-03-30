"""SDR Plugin — RSPduo dual tuner as a single plugin.

Consolidates SDRSource/PipeWireSDRSource (audio capture), RTLAirbandManager
(tuning/process control), and per-tuner AudioProcessor instances into one
self-contained plugin.

The RSPduo dual tuner appears as a single source to the bus system. Internal
master/slave ducking ensures the master tuner's audio takes priority.

See docs/mixer-v2-design.md for architecture.
"""

import collections
import json
import math
import os
import queue as _queue_mod
import subprocess
import threading
import time

import numpy as np

from audio_bus import DuckGroup, check_signal_instant, mix_audio_streams
from audio_sources import AudioProcessor
from gateway_link import RadioPlugin

_math_mod = math


# ---------------------------------------------------------------------------
# _TunerCapture — internal audio plumbing for one PipeWire tuner
# ---------------------------------------------------------------------------

class _TunerCapture:
    """Audio capture for a single PipeWire tuner stream.

    Runs a parec subprocess reading from a PipeWire monitor, queues chunks,
    and provides processed mono PCM via get_chunk().
    """

    def __init__(self, name, config, sink_name, processor):
        self.name = name
        self.config = config
        self._pw_sink_name = sink_name
        self.processor = processor
        self.audio_level = 0
        self.muted = False
        self.enabled = True

        self._parec_proc = None
        self._reader_thread = None
        self._reader_running = False
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._last_successful_read = time.monotonic()
        self._last_serve_sample = 0
        self._serve_discontinuity = 0.0
        self._sub_buffer_after = 0
        self.total_reads = 0
        self.last_read_time = 0

        self._audio_rate = getattr(config, 'AUDIO_RATE', 48000)
        self._chunk_size = getattr(config, 'AUDIO_CHUNK_SIZE', 2400)
        self._chunk_bytes = self._chunk_size * 2 * 2  # stereo 16-bit = 4 bytes/sample

    def setup(self):
        """Start parec subprocess and reader thread. Returns True on success."""
        monitor_name = f"{self._pw_sink_name}.monitor"

        # Verify monitor exists, auto-create sink if missing
        try:
            result = subprocess.run(['pactl', 'list', 'short', 'sources'],
                                    capture_output=True, text=True, timeout=5)
            if monitor_name not in result.stdout:
                print(f"[{self.name}] PipeWire monitor '{monitor_name}' not found, creating sink...")
                create_result = subprocess.run([
                    'pw-cli', 'create-node', 'adapter',
                    '{ factory.name=support.null-audio-sink'
                    f' node.name={self._pw_sink_name}'
                    ' media.class=Audio/Sink'
                    ' object.linger=true'
                    ' audio.position=[FL,FR] }'
                ], capture_output=True, text=True, timeout=5)
                if create_result.returncode != 0:
                    print(f"[{self.name}] Failed to create sink: {create_result.stderr.strip()}")
                    return False
                time.sleep(1)
                result = subprocess.run(['pactl', 'list', 'short', 'sources'],
                                        capture_output=True, text=True, timeout=5)
                if monitor_name not in result.stdout:
                    print(f"[{self.name}] Sink created but monitor still not found")
                    return False
                print(f"[{self.name}] Sink '{self._pw_sink_name}' created")
        except Exception as e:
            print(f"[{self.name}] Failed to check PipeWire sources: {e}")
            return False

        # Start parec
        try:
            self._parec_proc = subprocess.Popen([
                'parec',
                '--device=' + monitor_name,
                '--format=s16le',
                '--rate=' + str(self._audio_rate),
                '--channels=2',
                '--latency-msec=50',
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            print(f"[{self.name}] parec not found")
            return False
        except Exception as e:
            print(f"[{self.name}] Failed to start parec: {e}")
            return False

        # Reader thread
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"{self.name}-reader")
        self._reader_thread.start()

        # Wait for first data
        deadline = time.monotonic() + 2.0
        while self._chunk_queue.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        if self._chunk_queue.qsize() > 0:
            print(f"[{self.name}] PipeWire capture active (monitor: {monitor_name})")
            return True
        else:
            print(f"[{self.name}] No audio received from PipeWire after 2s")
            self._reader_running = False
            if self._parec_proc:
                self._parec_proc.kill()
            return False

    def _reader_loop(self):
        """Read fixed-size chunks from parec stdout and queue them."""
        chunk_bytes = self._chunk_bytes
        proc = self._parec_proc
        while self._reader_running and proc and proc.poll() is None:
            try:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                if len(data) < chunk_bytes:
                    data += b'\x00' * (chunk_bytes - len(data))
                self._last_successful_read = time.monotonic()
                try:
                    self._chunk_queue.put_nowait(data)
                except _queue_mod.Full:
                    try:
                        self._chunk_queue.get_nowait()
                    except _queue_mod.Empty:
                        pass
                    try:
                        self._chunk_queue.put_nowait(data)
                    except _queue_mod.Full:
                        pass
            except Exception:
                if self._reader_running:
                    time.sleep(0.01)

    def get_chunk(self):
        """Get one processed mono PCM chunk. Returns bytes or None."""
        if not self.enabled or not self._reader_running:
            return None

        # Take one chunk
        data = None
        try:
            data = self._chunk_queue.get_nowait()
        except _queue_mod.Empty:
            pass

        # Cap latency if queue builds up
        if data is not None:
            qsz = self._chunk_queue.qsize()
            while qsz > 6:
                try:
                    data = self._chunk_queue.get_nowait()
                    qsz -= 1
                except _queue_mod.Empty:
                    break

        if data is None:
            return None

        # Muted: consume but discard
        if self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None

        self.total_reads += 1
        self.last_read_time = time.time()

        # Stereo→mono
        arr = np.frombuffer(data, dtype=np.int16)
        if len(arr) >= 2:
            stereo = arr.reshape(-1, 2).astype(np.int32)
            arr = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(np.int16)
            raw = arr.tobytes()
        else:
            raw = data

        # Discontinuity tracking
        if len(raw) >= 2:
            first_sample = int.from_bytes(raw[0:2], byteorder='little', signed=True)
            self._serve_discontinuity = float(abs(first_sample - self._last_serve_sample))
            self._last_serve_sample = int.from_bytes(raw[-2:], byteorder='little', signed=True)

        self._sub_buffer_after = self._chunk_queue.qsize() * self._chunk_bytes

        # Level metering
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20.0 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0

            display_gain_key = 'SDR2_DISPLAY_GAIN' if '2' in self.name else 'SDR_DISPLAY_GAIN'
            display_gain = getattr(self.config, display_gain_key, 1.0)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            # Audio boost
            boost_key = 'SDR2_AUDIO_BOOST' if '2' in self.name else 'SDR_AUDIO_BOOST'
            audio_boost = getattr(self.config, boost_key, 1.0)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        # Apply audio processing (HPF, LPF, notch, gate)
        if self.processor:
            raw = self.processor.process(raw)

        return raw

    def cleanup(self):
        """Stop parec and reader thread."""
        self._reader_running = False
        if self._parec_proc:
            try:
                self._parec_proc.kill()
                self._parec_proc.wait(timeout=2)
            except Exception:
                pass
            self._parec_proc = None

    @property
    def active(self):
        return self._reader_running and self._parec_proc is not None


# ---------------------------------------------------------------------------
# SDRPlugin — the main plugin class
# ---------------------------------------------------------------------------

class SDRPlugin(RadioPlugin):
    """RSPduo dual tuner plugin.

    Manages two tuners (master + slave) as a single audio source.
    Internal ducking: master audio always flows, slave only when master is quiet.
    Absorbs RTLAirbandManager (process control, config generation, tuning).
    """

    name = "sdr_rspduo"
    capabilities = {
        "audio_rx": True,
        "audio_tx": False,
        "ptt": False,
        "frequency": True,
        "ctcss": False,
        "power": False,
        "rx_gain": False,
        "tx_gain": False,
        "smeter": False,
        "status": True,
    }

    # RTL-Airband config paths
    CONFIG_PATH = '/etc/rtl_airband/rspduo_gateway.conf'
    CONFIG_PATH_SDR2 = '/etc/rtl_airband/rspduo_gateway2.conf'
    MASTER_DEVICE_STRING = "driver=sdrplay,rspduo_mode=4"
    SLAVE_DEVICE_STRING = "driver=sdrplay,rspduo_mode=8"

    ANTENNAS = ['Tuner 1 50 ohm', 'Tuner 1 Hi-Z', 'Tuner 2 50 ohm']
    MODULATIONS = ['nfm', 'am']
    SAMPLE_RATES = [0.5, 1.0, 2.0, 2.56, 6.0, 8.0, 10.66]

    # All tunable settings with (type, default)
    _SETTING_KEYS = {
        'frequency': (float, 446.64),
        'modulation': (str, 'nfm'),
        'sample_rate': (float, 2.56),
        'antenna': (str, 'Tuner 1 50 ohm'),
        'gain_mode': (str, 'agc'),
        'rfgr': (int, 4),
        'ifgr': (int, 40),
        'agc_setpoint': (int, -30),
        'squelch_threshold': (int, 0),
        'correction': (float, 0.0),
        'tau': (int, 200),
        'ampfactor': (float, 1.0),
        'lowpass': (int, 2500),
        'highpass': (int, 100),
        'notch': (float, 0.0),
        'notch_q': (float, 10.0),
        'channel_bw': (float, 0.0),
        'bias_t': (bool, False),
        'rf_notch': (bool, False),
        'dab_notch': (bool, False),
        'iq_correction': (bool, True),
        'external_ref': (bool, False),
        'continuous': (bool, True),
        # SDR2 (Tuner 2)
        'frequency2': (float, 462.550),
        'modulation2': (str, 'nfm'),
        'gain_mode2': (str, 'agc'),
        'rfgr2': (int, 4),
        'ifgr2': (int, 40),
        'agc_setpoint2': (int, -30),
        'squelch_threshold2': (int, 0),
        'tau2': (int, 200),
        'ampfactor2': (float, 1.0),
        'lowpass2': (int, 2500),
        'highpass2': (int, 100),
        'notch2': (float, 0.0),
        'notch_q2': (float, 10.0),
        'channel_bw2': (float, 0.0),
        'continuous2': (bool, True),
    }

    def __init__(self):
        super().__init__()
        self._config = None
        self._gateway_dir = None
        self._channels_path = None

        # Tuner captures
        self._tuner1 = None  # master
        self._tuner2 = None  # slave

        # Processing
        self._processor1 = None
        self._processor2 = None

        # Internal ducking (master ducks slave)
        self._duck_group = None
        self._signal_threshold = -60.0
        self._master_has_signal_hyst = False
        self._master_signal_continuous_start = 0.0
        self._master_last_signal_time = 0.0
        self._signal_attack_time = 2.0
        self._signal_release_time = 3.0

        # Bus compat attributes
        self.enabled = True
        self.ptt_control = False
        self.priority = 2
        self.volume = 1.0
        self.duck = True  # can be ducked by higher-priority sources
        self.sdr_priority = 1
        self.audio_level = 0

        # Set defaults for all tuning settings
        for key, (typ, default) in self._SETTING_KEYS.items():
            setattr(self, key, default)

    def setup(self, config):
        """Initialize the SDR plugin: load settings, start rtl_airband, open audio captures."""
        if isinstance(config, dict):
            # Called from link endpoint style — not our use case
            return False

        self._config = config
        self._gateway_dir = os.path.dirname(
            getattr(config, '_config_path', '') or os.path.join(os.getcwd(), 'gateway_config.txt'))
        self._channels_path = os.path.join(self._gateway_dir, 'sdr_channels.json')

        # Load persisted tuning
        self._load_settings()

        # Signal detection config
        self._signal_threshold = getattr(config, 'SDR_SIGNAL_THRESHOLD', -60.0)
        self._signal_attack_time = getattr(config, 'SIGNAL_ATTACK_TIME', 2.0)
        self._signal_release_time = getattr(config, 'SIGNAL_RELEASE_TIME', 3.0)

        # Create processing chains (isolated IIR filter state per tuner)
        self._processor1 = AudioProcessor("sdr", config)
        self._processor2 = AudioProcessor("sdr2", config)
        self._sync_processor(self._processor1, config)
        self._sync_processor(self._processor2, config)

        # Internal ducking
        self._duck_group = DuckGroup(
            switch_padding_time=0.0,  # no padding for internal SDR ducking
            reduck_inhibit_time=getattr(config, 'SDR_DUCK_COOLDOWN', 3.0),
            blob_gap_hold_time=0.5,
        )

        # Bus compat
        self.duck = getattr(config, 'SDR_DUCK', True)

        # Determine sink names
        sdr1_device = getattr(config, 'SDR_DEVICE_NAME', 'pw:sdr_capture')
        sdr2_device = getattr(config, 'SDR2_DEVICE_NAME', 'pw:sdr_capture2')
        sink1 = sdr1_device.split(':', 1)[1] if ':' in sdr1_device else sdr1_device
        sink2 = sdr2_device.split(':', 1)[1] if ':' in sdr2_device else sdr2_device

        # SDR priority order
        sdr_order = getattr(config, 'SDR_PRIORITY_ORDER', 'sdr1')
        if sdr_order == 'sdr2':
            self._master_name, self._slave_name = 'SDR2', 'SDR1'
            self._master_sink, self._slave_sink = sink2, sink1
            self._master_proc, self._slave_proc = self._processor2, self._processor1
        else:
            self._master_name, self._slave_name = 'SDR1', 'SDR2'
            self._master_sink, self._slave_sink = sink1, sink2
            self._master_proc, self._slave_proc = self._processor1, self._processor2

        # Start rtl_airband if not already running (lightweight — no sdrplay restart)
        try:
            _chk = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
            _already_running = _chk.returncode == 0
        except Exception:
            _already_running = False
        if _already_running:
            print("  rtl_airband already running (adopted)")
        else:
            print("  Starting rtl_airband processes...")
            try:
                self._write_config()
                self._write_config_sdr2()
                self._start_rtl_airband_only()
            except Exception as e:
                print(f"  Warning: rtl_airband start issue: {e}")

        # Create tuner captures
        enable_sdr1 = getattr(config, 'ENABLE_SDR', True)
        enable_sdr2 = getattr(config, 'ENABLE_SDR2', False)

        success = False

        if enable_sdr1:
            self._tuner1 = _TunerCapture('SDR1', config, sink1, self._processor1)
            if self._tuner1.setup():
                print(f"  SDR1: {self.frequency:.3f} MHz {self.modulation.upper()}")
                success = True
            else:
                print("  SDR1: audio capture failed")
                self._tuner1 = None

        if enable_sdr2:
            self._tuner2 = _TunerCapture('SDR2', config, sink2, self._processor2)
            if self._tuner2.setup():
                print(f"  SDR2: {self.frequency2:.3f} MHz {self.modulation2.upper()}")
                success = True
            else:
                print("  SDR2: audio capture failed")
                self._tuner2 = None

        # Set initial mute state
        if self._tuner1:
            self._tuner1.muted = getattr(config, 'SDR_MUTE_DEFAULT', False)
        if self._tuner2:
            self._tuner2.muted = getattr(config, 'SDR2_MUTE_DEFAULT', True)

        return success

    def teardown(self):
        """Stop rtl_airband and clean up audio captures."""
        if self._tuner1:
            self._tuner1.cleanup()
        if self._tuner2:
            self._tuner2.cleanup()
        self._stop_rtl_airband()

    # -- Standard plugin interface (bus calls these) --

    def get_audio(self, chunk_size=None):
        """Get one chunk of mixed/ducked audio from both tuners.

        Returns (pcm_bytes_or_none, False). SDR never triggers PTT.
        """
        if not self.enabled:
            return None, False

        current_time = time.monotonic()

        # Pull audio from both tuners
        master_audio = self._tuner1.get_chunk() if self._tuner1 else None
        slave_audio = self._tuner2.get_chunk() if self._tuner2 else None

        # Determine which is master/slave based on priority order
        if self._master_name == 'SDR2':
            master_audio, slave_audio = slave_audio, master_audio

        # Sync processors
        if self._config:
            self._sync_processor(self._processor1, self._config)
            self._sync_processor(self._processor2, self._config)

        # Master/slave ducking
        master_has_signal = check_signal_instant(master_audio, self._signal_threshold)
        master_has_signal_hyst = self._update_master_hysteresis(master_has_signal, current_time)

        if master_audio is not None and slave_audio is not None:
            # Both have audio — duck slave if master has signal
            if master_has_signal_hyst:
                # Master active — use master only
                output = master_audio
            else:
                # Master quiet — mix both
                output = mix_audio_streams(master_audio, slave_audio)
        elif master_audio is not None:
            output = master_audio
        elif slave_audio is not None:
            output = slave_audio
        else:
            # Update combined level
            self.audio_level = 0
            return None, False

        # Update combined audio level for bus status
        if self._tuner1 and self._tuner2:
            self.audio_level = max(self._tuner1.audio_level, self._tuner2.audio_level)
        elif self._tuner1:
            self.audio_level = self._tuner1.audio_level
        elif self._tuner2:
            self.audio_level = self._tuner2.audio_level

        return output, False

    def execute(self, cmd):
        """Handle commands: tune, restart, stop, mute, status."""
        if isinstance(cmd, dict):
            action = cmd.get('cmd', '')
        else:
            return {"ok": False, "error": "invalid command"}

        if action == 'tune':
            return self._apply_settings(**{k: v for k, v in cmd.items() if k != 'cmd'})
        elif action == 'tune2':
            return self._apply_settings_sdr2(**{k: v for k, v in cmd.items() if k != 'cmd'})
        elif action == 'restart':
            return self._restart_rtl_airband()
        elif action == 'stop':
            self._stop_rtl_airband()
            return {"ok": True}
        elif action == 'mute':
            tuner = cmd.get('tuner', 1)
            if tuner == 1 and self._tuner1:
                self._tuner1.muted = not self._tuner1.muted
                return {"ok": True, "muted": self._tuner1.muted}
            elif tuner == 2 and self._tuner2:
                self._tuner2.muted = not self._tuner2.muted
                return {"ok": True, "muted": self._tuner2.muted}
            return {"ok": False, "error": f"tuner {tuner} not available"}
        elif action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return full status dict for web UI and status bar."""
        alive = False
        try:
            result = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
            alive = result.returncode == 0
        except Exception:
            pass

        d = {key: getattr(self, key) for key in self._SETTING_KEYS}
        d['process_alive'] = alive
        d['plugin'] = self.name

        if self._tuner1:
            d['audio_level'] = self._tuner1.audio_level
            d['tuner1_muted'] = self._tuner1.muted
            d['tuner1_active'] = self._tuner1.active
        else:
            d['audio_level'] = 0
            d['tuner1_muted'] = True
            d['tuner1_active'] = False

        if self._tuner2:
            d['audio_level2'] = self._tuner2.audio_level
            d['tuner2_muted'] = self._tuner2.muted
            d['tuner2_active'] = self._tuner2.active
        else:
            d['audio_level2'] = 0
            d['tuner2_muted'] = True
            d['tuner2_active'] = False

        d['master_ducking_slave'] = self._master_has_signal_hyst
        return d

    # -- Per-tuner accessors --

    def get_tuner(self, n):
        """Get tuner 1 or 2. Returns _TunerCapture or None."""
        return self._tuner1 if n == 1 else self._tuner2

    @property
    def tuner1_level(self):
        return self._tuner1.audio_level if self._tuner1 else 0

    @property
    def tuner2_level(self):
        return self._tuner2.audio_level if self._tuner2 else 0

    @property
    def tuner1_muted(self):
        return self._tuner1.muted if self._tuner1 else True

    @tuner1_muted.setter
    def tuner1_muted(self, val):
        if self._tuner1:
            self._tuner1.muted = val

    @property
    def tuner2_muted(self):
        return self._tuner2.muted if self._tuner2 else True

    @tuner2_muted.setter
    def tuner2_muted(self, val):
        if self._tuner2:
            self._tuner2.muted = val

    @property
    def tuner1_enabled(self):
        return self._tuner1 is not None and self._tuner1.enabled

    @property
    def tuner2_enabled(self):
        return self._tuner2 is not None and self._tuner2.enabled

    @property
    def input_stream(self):
        """Truthy if any tuner is active."""
        return (self._tuner1 and self._tuner1.active) or (self._tuner2 and self._tuner2.active)

    @property
    def muted(self):
        """SDR1 muted state."""
        return self._tuner1.muted if self._tuner1 else True

    @muted.setter
    def muted(self, val):
        if self._tuner1:
            self._tuner1.muted = val

    def check_watchdog(self):
        """No-op — PipeWire tuners don't need ALSA watchdog."""
        pass

    def cleanup(self):
        """Clean up both tuners."""
        self.teardown()

    # -- Internal: master/slave hysteresis --

    def _update_master_hysteresis(self, signal_now, current_time):
        """Track whether master has sustained signal (for slave ducking)."""
        if signal_now:
            self._master_last_signal_time = current_time
            if self._master_signal_continuous_start == 0.0:
                self._master_signal_continuous_start = current_time
        else:
            self._master_signal_continuous_start = 0.0

        if not self._master_has_signal_hyst:
            if self._master_signal_continuous_start > 0.0:
                duration = current_time - self._master_signal_continuous_start
                if duration >= self._signal_attack_time:
                    self._master_has_signal_hyst = True
        else:
            time_since = current_time - self._master_last_signal_time
            if time_since >= self._signal_release_time:
                self._master_has_signal_hyst = False

        return self._master_has_signal_hyst

    # -- Internal: processor sync --

    @staticmethod
    def _sync_processor(proc, config):
        """Sync config flags into an AudioProcessor instance."""
        proc.enable_noise_gate = getattr(config, 'SDR_PROC_ENABLE_NOISE_GATE', False)
        proc.gate_threshold = getattr(config, 'SDR_PROC_NOISE_GATE_THRESHOLD', -40)
        proc.gate_attack = getattr(config, 'SDR_PROC_NOISE_GATE_ATTACK', 0.01)
        proc.gate_release = getattr(config, 'SDR_PROC_NOISE_GATE_RELEASE', 0.1)
        proc.enable_hpf = getattr(config, 'SDR_PROC_ENABLE_HPF', True)
        proc.hpf_cutoff = getattr(config, 'SDR_PROC_HPF_CUTOFF', 300)
        proc.enable_lpf = getattr(config, 'SDR_PROC_ENABLE_LPF', True)
        proc.lpf_cutoff = getattr(config, 'SDR_PROC_LPF_CUTOFF', 3000)
        proc.enable_notch = getattr(config, 'SDR_PROC_ENABLE_NOTCH', False)
        proc.notch_freq = getattr(config, 'SDR_PROC_NOTCH_FREQ', 1000)
        proc.notch_q = getattr(config, 'SDR_PROC_NOTCH_Q', 10.0)

    # -- Internal: tuning settings persistence --

    def _load_settings(self):
        """Load persisted tuning state from JSON."""
        try:
            if self._channels_path and os.path.exists(self._channels_path):
                with open(self._channels_path, 'r') as f:
                    data = json.load(f)
                saved = data.get('current', {})
                if 'bandwidth' in saved and 'sample_rate' not in saved:
                    saved['sample_rate'] = saved.pop('bandwidth')
                for key, (typ, default) in self._SETTING_KEYS.items():
                    if key in saved:
                        try:
                            setattr(self, key, typ(saved[key]))
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    def _save_settings(self):
        """Persist current tuning state to JSON."""
        try:
            if self._channels_path:
                with open(self._channels_path, 'w') as f:
                    json.dump({'current': {k: getattr(self, k) for k in self._SETTING_KEYS}}, f, indent=2)
        except Exception as e:
            print(f"  [SDR] Failed to save settings: {e}")

    # -- Internal: rtl_airband config generation --

    def _write_config(self):
        """Generate SDR1 (Master/Tuner 1) rtl_airband config file."""
        gain_line = ''
        if self.gain_mode == 'manual':
            gain_line = f'  gain = "RFGR={self.rfgr},IFGR={self.ifgr}";'

        settings_parts = [
            f'biasT_ctrl={str(self.bias_t).lower()}',
            f'rfnotch_ctrl={str(self.rf_notch).lower()}',
            f'dabnotch_ctrl={str(self.dab_notch).lower()}',
            f'iqcorr_ctrl={str(self.iq_correction).lower()}',
            f'extref_ctrl={str(self.external_ref).lower()}',
            f'agc_setpoint={self.agc_setpoint}',
        ]
        device_settings = ','.join(settings_parts)
        sample_rate = min(self.sample_rate, 2.0)

        ch_opts = ''
        if self.squelch_threshold != 0:
            ch_opts += f'      squelch_threshold = {self.squelch_threshold};\n'
        if self.ampfactor != 1.0:
            ch_opts += f'      ampfactor = {self.ampfactor};\n'
        if self.lowpass != 2500:
            ch_opts += f'      lowpass = {self.lowpass};\n'
        if self.highpass != 100:
            ch_opts += f'      highpass = {self.highpass};\n'
        if self.notch > 0:
            ch_opts += f'      notch = {self.notch};\n'
            if self.notch_q != 10.0:
                ch_opts += f'      notch_q = {self.notch_q};\n'
        if self.channel_bw > 0:
            ch_opts += f'      bandwidth = {self.channel_bw};\n'

        dev_opts = ''
        if self.correction != 0.0:
            dev_opts += f'  correction = {self.correction};\n'
        if self.tau != 200:
            dev_opts += f'  tau = {self.tau};\n'
        if self.antenna:
            dev_opts += f'  antenna = "{self.antenna}";\n'

        conf = f'''# Auto-generated by SDRPlugin (SDR1 — Master / Tuner 1)
devices:
({{
  type = "soapysdr";
  device_string = "{self.MASTER_DEVICE_STRING}";
  device_settings = "{device_settings}";
  mode = "multichannel";
  centerfreq = {self.frequency};
  sample_rate = {sample_rate};
{gain_line}
{dev_opts}
  channels:
  (
    {{
      freq = {self.frequency};
      modulation = "{self.modulation}";
{ch_opts}      outputs: (
        {{
          type = "pulse";
          stream_name = "SDR {self.frequency:.3f} MHz";
          sink = "sdr_capture";
          continuous = {'true' if self.continuous else 'false'};
        }}
      );
    }}
  );
}});
'''
        proc = subprocess.run(
            ['sudo', 'tee', self.CONFIG_PATH],
            input=conf.encode(), capture_output=True, timeout=5
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to write config: {proc.stderr.decode()}")

    def _write_config_sdr2(self):
        """Generate SDR2 (Slave/Tuner 2) rtl_airband config file."""
        gain_line = ''
        if self.gain_mode2 == 'manual':
            gain_line = f'  gain = "RFGR={self.rfgr2},IFGR={self.ifgr2}";\n'

        device_settings = f'agc_setpoint={self.agc_setpoint2}'
        sample_rate = min(self.sample_rate, 2.0)

        ch_opts = ''
        if self.squelch_threshold2 != 0:
            ch_opts += f'      squelch_threshold = {self.squelch_threshold2};\n'
        if self.ampfactor2 != 1.0:
            ch_opts += f'      ampfactor = {self.ampfactor2};\n'
        if self.lowpass2 != 2500:
            ch_opts += f'      lowpass = {self.lowpass2};\n'
        if self.highpass2 != 100:
            ch_opts += f'      highpass = {self.highpass2};\n'
        if self.notch2 > 0:
            ch_opts += f'      notch = {self.notch2};\n'
            if self.notch_q2 != 10.0:
                ch_opts += f'      notch_q = {self.notch_q2};\n'
        if self.channel_bw2 > 0:
            ch_opts += f'      bandwidth = {self.channel_bw2};\n'

        dev_opts = ''
        if self.tau2 != 200:
            dev_opts += f'  tau = {self.tau2};\n'

        conf = f'''# Auto-generated by SDRPlugin (SDR2 — Slave / Tuner 2)
devices:
({{
  type = "soapysdr";
  device_string = "{self.SLAVE_DEVICE_STRING}";
  device_settings = "{device_settings}";
  mode = "multichannel";
  centerfreq = {self.frequency2};
  sample_rate = {sample_rate};
{gain_line}{dev_opts}
  channels:
  (
    {{
      freq = {self.frequency2};
      modulation = "{self.modulation2}";
{ch_opts}      outputs: (
        {{
          type = "pulse";
          stream_name = "SDR2 {self.frequency2:.3f} MHz";
          sink = "sdr_capture2";
          continuous = {'true' if self.continuous2 else 'false'};
        }}
      );
    }}
  );
}});
'''
        proc = subprocess.run(
            ['sudo', 'tee', self.CONFIG_PATH_SDR2],
            input=conf.encode(), capture_output=True, timeout=5
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to write SDR2 config: {proc.stderr.decode()}")

    # -- Internal: rtl_airband process management --

    def _restart_rtl_airband(self):
        """Kill and restart rtl_airband processes."""
        try:
            self._write_config()
            self._write_config_sdr2()

            subprocess.run(['sudo', 'killall', '-9', 'rtl_airband'],
                           capture_output=True, timeout=5)
            time.sleep(1)

            # Restart SDRplay API
            try:
                subprocess.run(['sudo', 'systemctl', 'stop', 'sdrplay.service'],
                               capture_output=True, timeout=3)
            except subprocess.TimeoutExpired:
                pass
            subprocess.run(['sudo', 'killall', '-9', 'sdrplay_apiService'],
                           capture_output=True, timeout=3)
            time.sleep(1)
            subprocess.run(['sudo', 'systemctl', 'start', 'sdrplay.service'],
                           capture_output=True, timeout=10)
            time.sleep(5)

            # Start SDR1 (Master)
            subprocess.run(['rtl_airband', '-e', '-c', self.CONFIG_PATH],
                           capture_output=True, timeout=10)
            alive = False
            for _ in range(5):
                time.sleep(1)
                chk = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
                if chk.returncode == 0:
                    alive = True
                    break
            if not alive:
                return {'ok': False, 'error': 'rtl_airband (SDR1) failed to start'}

            # Start SDR2 (Slave)
            time.sleep(3)
            if os.path.exists(self.CONFIG_PATH_SDR2):
                subprocess.run(['rtl_airband', '-e', '-c', self.CONFIG_PATH_SDR2],
                               capture_output=True, timeout=10)
                time.sleep(2)

            self._save_settings()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _start_rtl_airband_only(self):
        """Start rtl_airband processes without restarting sdrplay API service.

        Used during initial setup — assumes sdrplay_apiService is already running.
        The heavy _restart_rtl_airband() (which kills sdrplay API) is only for
        explicit tune/restart commands from the UI.
        """
        # Start SDR1 (Master)
        subprocess.run(['rtl_airband', '-e', '-c', self.CONFIG_PATH],
                       capture_output=True, timeout=10)
        alive = False
        for _ in range(5):
            time.sleep(1)
            chk = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
            if chk.returncode == 0:
                alive = True
                break
        if not alive:
            print("  Warning: rtl_airband (SDR1) failed to start")
            return

        # Start SDR2 (Slave) — must start after Master
        time.sleep(3)
        if os.path.exists(self.CONFIG_PATH_SDR2):
            subprocess.run(['rtl_airband', '-e', '-c', self.CONFIG_PATH_SDR2],
                           capture_output=True, timeout=10)
            time.sleep(2)
            chk2 = subprocess.run(['pgrep', '-c', 'rtl_airband'], capture_output=True, timeout=2)
            count = int(chk2.stdout.decode().strip()) if chk2.returncode == 0 else 0
            if count < 2:
                print("  Warning: rtl_airband (SDR2) failed to start")

    def _stop_rtl_airband(self):
        """Stop all rtl_airband processes."""
        subprocess.run(['sudo', 'killall', '-9', 'rtl_airband'],
                       capture_output=True, timeout=5)

    def _apply_settings(self, **kwargs):
        """Update SDR1 tuning and restart."""
        for key, (typ, _default) in self._SETTING_KEYS.items():
            if key in kwargs:
                try:
                    setattr(self, key, typ(kwargs[key]))
                except (ValueError, TypeError):
                    pass
        return self._restart_rtl_airband()

    def _apply_settings_sdr2(self, **kwargs):
        """Update SDR2 tuning and restart."""
        sdr2_keys = {k for k in self._SETTING_KEYS if k.endswith('2')}
        for key in sdr2_keys:
            if key in kwargs:
                typ = self._SETTING_KEYS[key][0]
                try:
                    setattr(self, key, typ(kwargs[key]))
                except (ValueError, TypeError):
                    pass
        return self._restart_rtl_airband()
