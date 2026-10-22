"""KV4P HT radio — endpoint-hosted plugin.

Hosted by tools/link_endpoint.py. The legacy gateway-side variant lived at
the repo root as kv4p_plugin.py and was tightly coupled to gateway_core,
the [kv4p] config section, and gateway_config.txt write-back. This version
takes a plain dict config from the endpoint, persists nothing locally, and
lets the gateway own state (endpoints_state.json) via the status channel.

Run multiple kv4ps by starting multiple endpoints with distinct --name:
  link_endpoint.py --server 127.0.0.1:9700 --name kv4p-vhf --plugin kv4p \\
    --device /dev/kv4p --plugin-config-json '{"default_freq":147.435}'
"""

import collections
import os
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

# Endpoint imports — gateway_link.py defines RadioPlugin and is import-safe
# both on the gateway and on an endpoint (it conditionally pulls server bits).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from audio_util import AudioProcessor, pcm_level  # noqa: E402
from gateway_link import RadioPlugin                       # noqa: E402


_FREQ_RANGES = {
    'SA818_VHF': (134.0, 174.0),
    'SA818_UHF': (400.0, 480.0),
    # No detection AND no config override — allow anything and let the
    # radio decide. Avoids the silent-fallback bug where a missing
    # handshake masquerades as a positive VHF detection and blocks UHF
    # commands.
    'UNKNOWN':   (0.0, 99999.0),
}


def _cfg(d, key, default=None):
    """Read either an unprefixed key ('port') or KV4P_-prefixed legacy key."""
    if key in d:
        return d[key]
    legacy = f'KV4P_{key.upper()}'
    if legacy in d:
        return d[legacy]
    return default


class KV4PPlugin(RadioPlugin):
    """KV4P HT radio plugin (endpoint-hosted, multi-instance capable)."""

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
        self._config = SimpleNamespace()
        self._port = None
        self._verbose = False

        self._radio = None
        self._connected = False
        self._serial_connected = False
        self._lock = threading.Lock()
        self._stop = False
        self._poll_thread = None

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
        self._rf_module = 'UNKNOWN'   # until handshake or config override
        self._rf_module_override = None  # populated in setup() from config
        self._smeter_enabled = False
        self._ptt_on_state = False

        self._decoder = None
        self._encoder = None
        self._dc_remover = None
        self._dc_remover_frame = None
        self._vol_ramp = None

        self._chunk_queue = collections.deque(maxlen=16)
        self._sub_buffer = b''
        self._chunk_bytes = 4800
        self._resample_ratio = 1.132
        self._resample_pos = 0.0
        self._buf_max = 0
        self.server_connected = False
        self.audio_level = 0
        self.tx_audio_level = 0
        self.audio_boost = 1.0

        self._tx_buf = b''
        self._processor = None

        # Bus compat (used by RadioPlugin base; harmless on endpoint)
        self.enabled = True
        self.ptt_control = True
        self.priority = 2
        self.volume = 1.0
        self.duck = True
        self.sdr_priority = 2
        self.muted = False
        self.tx_muted = False

        self._recording_file = None

        # Marks status as dirty so StatusReporter pushes an update immediately.
        # Gateway listens for these to persist freq/CTCSS/power to its
        # endpoints_state.json keyed by endpoint name.
        self._status_dirty = False

    # ── Setup / teardown ────────────────────────────────────────────

    def setup(self, config):
        # Endpoint passes a dict; convert to attribute access for code reuse.
        if isinstance(config, dict):
            cfg_dict = dict(config)
        else:
            # Allow object/namespace input (used by unit tests)
            cfg_dict = {k: getattr(config, k) for k in dir(config)
                        if not k.startswith('_')}

        self._cfg_dict = cfg_dict
        self._config = SimpleNamespace(**cfg_dict)
        self._verbose = bool(_cfg(cfg_dict, 'verbose', False))
        self._port = str(_cfg(cfg_dict, 'port', _cfg(cfg_dict, 'device', '/dev/ttyUSB0')))

        chunk_size = int(_cfg(cfg_dict, 'audio_chunk_size', 2400))
        channels = int(_cfg(cfg_dict, 'audio_channels', 1))
        self._chunk_bytes = chunk_size * channels * 2
        self._buf_max = self._chunk_bytes * 6

        self.duck = bool(_cfg(cfg_dict, 'audio_duck', True))
        self.sdr_priority = int(_cfg(cfg_dict, 'audio_priority', 2))
        self.audio_boost = float(_cfg(cfg_dict, 'audio_boost', 1.0))
        self.muted = False

        # Initial radio state from config
        self._frequency = float(_cfg(cfg_dict, 'default_freq',
                                     _cfg(cfg_dict, 'freq', 146.520)))
        tx_freq = float(_cfg(cfg_dict, 'tx_freq', 0))
        self._tx_frequency = tx_freq if tx_freq > 0 else self._frequency
        self._squelch = int(_cfg(cfg_dict, 'squelch', 4))
        self._bandwidth = int(_cfg(cfg_dict, 'bandwidth', 1))
        self._ctcss_tx = int(_cfg(cfg_dict, 'ctcss_tx', 0))
        self._ctcss_rx = int(_cfg(cfg_dict, 'ctcss_rx', 0))
        self._high_power = bool(_cfg(cfg_dict, 'high_power', True))

        # Optional config override for the RF module. Use when the firmware
        # handshake doesn't reply (e.g. KV4P-HT v2.0e UHF boards) so we
        # don't get stuck in the 'UNKNOWN' fallback. Recognised values:
        # 'SA818_VHF', 'SA818_UHF'. Sets _rf_module immediately so freq
        # validation works before the radio is even connected.
        _ovr = str(_cfg(cfg_dict, 'rf_module', '') or '').strip().upper()
        if _ovr in _FREQ_RANGES and _ovr != 'UNKNOWN':
            self._rf_module_override = _ovr
            self._rf_module = _ovr
            print(f"  [KV4P] rf_module override from config: {_ovr}",
                  flush=True)

        # Reuse the gateway audio processing chain — the AudioProcessor class
        # is plain library code and works equally well endpoint-side.
        self._processor = AudioProcessor("kv4p", self._config)
        self._sync_processor()

        if not self._setup_codec():
            return False
        if not self._connect_radio():
            return False
        self._start_polling()
        return True

    def teardown(self):
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

    # ── Standard plugin interface ───────────────────────────────────

    def get_audio(self, chunk_size=None):
        if not self.enabled or not self.server_connected:
            return None, False

        while self._chunk_queue:
            self._sub_buffer += self._chunk_queue.popleft()

        # Adaptive PLL: keep buffer near target
        buf_target = self._chunk_bytes * 3
        buf_now = len(self._sub_buffer)
        buf_error = (buf_now - buf_target) / buf_target if buf_target > 0 else 0
        self._resample_ratio = max(
            0.95, min(1.25, self._resample_ratio + buf_error * 0.002))

        n_input_samples = len(self._sub_buffer) // 2
        out_samples_needed = self._chunk_bytes // 2
        input_needed = int(self._resample_pos
                           + out_samples_needed * self._resample_ratio) + 2
        if n_input_samples < input_needed:
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

        if len(self._sub_buffer) > self._buf_max:
            excess = (len(self._sub_buffer) - self._buf_max + 1) & ~1
            self._sub_buffer = self._sub_buffer[excess:]
            self._resample_pos = 0.0

        pcm_data = np.clip(out, -32768, 32767).astype(np.int16).tobytes()

        if self.muted:
            self.audio_level = int(self.audio_level * 0.7)
            return None, False

        if self._dc_remover:
            pcm_data = self._dc_remover.process(pcm_data)

        try:
            display_gain = float(_cfg(self._cfg_dict, 'audio_display_gain', 1.0))
            self.audio_level = pcm_level(pcm_data, self.audio_level, gain=display_gain)
        except Exception:
            pass

        if self.audio_boost != 1.0:
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            pcm_data = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        if self._processor:
            self._sync_processor()
            pcm_data = self._processor.process(pcm_data)

        if self._recording_file:
            try:
                self._recording_file.write(pcm_data)
            except Exception:
                pass

        return pcm_data, False

    def put_audio(self, pcm_48k):
        if not self._encoder or not self._radio or self.tx_muted:
            return
        try:
            frame_bytes = 1920 * 2  # 40 ms at 48 kHz mono
            self._tx_buf += pcm_48k
            buf = self._tx_buf
            try:
                self.tx_audio_level = pcm_level(pcm_48k, self.tx_audio_level)
            except Exception:
                pass
            while len(buf) >= frame_bytes:
                try:
                    opus_frame = self._encoder.encode(buf[:frame_bytes], 1920)
                    self._radio.send_audio(opus_frame)
                except Exception:
                    pass
                buf = buf[frame_bytes:]
            self._tx_buf = buf
        except Exception:
            pass

    def _validate_freq(self, freq):
        lo, hi = _FREQ_RANGES.get(self._rf_module, (0, 9999))
        if not (lo <= freq <= hi):
            return f"{freq:.4f} MHz out of range for {self._rf_module} ({lo:.0f}-{hi:.0f} MHz)"
        return None

    def execute(self, cmd):
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'freq' or action == 'frequency':
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
            self._status_dirty = True
            return {"ok": True}
        if action == 'squelch':
            self._squelch = max(0, min(8, int(cmd.get('level', self._squelch))))
            self._apply_group(); self._status_dirty = True
            return {"ok": True}
        if action == 'ctcss':
            if 'tx' in cmd: self._ctcss_tx = int(cmd['tx'])
            if 'rx' in cmd: self._ctcss_rx = int(cmd['rx'])
            self._apply_group(); self._status_dirty = True
            return {"ok": True}
        if action == 'bandwidth':
            self._bandwidth = 1 if cmd.get('wide', True) else 0
            self._apply_group(); self._status_dirty = True
            return {"ok": True}
        if action == 'power':
            self._high_power = bool(cmd.get('high', True))
            if self._radio:
                try: self._radio.set_power(self._high_power)
                except Exception: pass
            self._status_dirty = True
            return {"ok": True}
        if action == 'boost':
            self.audio_boost = max(0.0, min(5.0, float(cmd.get('value', 1.0))))
            self._status_dirty = True
            return {"ok": True}
        if action == 'ptt':
            # Explicit state if provided; otherwise toggle from current TX
            # state. The web GUI's PTT button sends a bare {cmd:'ptt'} and
            # expects toggle — defaulting to False made every click mean
            # "PTT off" so the radio never actually keyed.
            if 'state' in cmd:
                return self._set_ptt(bool(cmd['state']))
            return self._set_ptt(not self._ptt_on_state)
        if action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}
        if action == 'connect':
            return {"ok": self._connect_radio()}
        if action == 'reconnect':
            if self._radio:
                try: self._radio.close()
                except Exception: pass
            self._connected = False
            self._serial_connected = False
            return {"ok": self._connect_radio()}
        if action == 'testtone':
            self._send_test_tone(cmd)
            return {"ok": True, "msg": "test tone started"}
        if action == 'capture':
            return self._handle_capture(cmd)
        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        return {
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

    # ── Radio internals ─────────────────────────────────────────────

    def _connect_radio(self):
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

            # Three sources for _rf_module, in priority order:
            #   1. Config override (rf_module = SA818_UHF/VHF) — always wins
            #      so a known-good UHF board with a silent firmware still works
            #   2. Real handshake response (ver.is_valid True)
            #   3. Fallback 'UNKNOWN' — _validate_freq then allows anything
            #      and we let the radio accept/reject. Better than silently
            #      pretending we detected SA818_VHF.
            if ver and getattr(ver, 'is_valid', False):
                self._firmware_version = ver.firmware_version
                if not self._rf_module_override:
                    self._rf_module = (ver.rf_module_type.name
                                       if hasattr(ver.rf_module_type, 'name')
                                       else 'UNKNOWN')
            else:
                # No real handshake. Keep firmware_version=0 honestly.
                # _rf_module stays at the override (if any) or 'UNKNOWN'.
                print(f"  [KV4P] No VERSION handshake; rf_module={self._rf_module} "
                      f"(override={self._rf_module_override!r})", flush=True)

            for _label, _f in [('RX', self._frequency), ('TX', self._tx_frequency)]:
                _err = self._validate_freq(_f)
                if _err:
                    print(f"  [KV4P] WARNING: {_label} {_err}")

            self._apply_group()
            time.sleep(0.3)

            from kv4p.protocol import FiltersConfig
            self._radio.set_filters(FiltersConfig(
                pre_emphasis=True, highpass=True, lowpass=True))
            self._radio.set_power(self._high_power)

            if bool(_cfg(self._cfg_dict, 'smeter', True)):
                self._radio.enable_smeter(True)
                self._smeter_enabled = True

            print(f"  [KV4P] Connected: fw v{self._firmware_version}, {self._rf_module}")
            print(f"  [KV4P] Tuned to {self._frequency:.4f} MHz")
            return True
        except Exception as e:
            print(f"  [KV4P] connect error: {e}")
            self._connected = False
            self._serial_connected = False
            return False

    def _setup_codec(self):
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
            print("  [KV4P] Opus codec + DSP initialized")
            return True
        except ImportError:
            print("  [KV4P] opuslib not installed — audio disabled")
            return False
        except Exception as e:
            print(f"  [KV4P] codec init error: {e}")
            return False

    def _on_rx_audio(self, opus_data):
        if not self._decoder or not self.enabled:
            return
        try:
            pcm = self._decoder.decode(opus_data, 1920)
            try:
                self.audio_level = pcm_level(pcm, self.audio_level)
            except Exception:
                pass
            if len(self._chunk_queue) >= self._chunk_queue.maxlen:
                self._chunk_queue.popleft()
            self._chunk_queue.append(pcm)
        except Exception:
            pass

    def _on_smeter(self, rssi):
        self._signal = rssi

    def _on_phys_ptt(self, pressed):
        if self._verbose:
            print(f"[KV4P] Physical PTT {'pressed' if pressed else 'released'}")

    def _apply_group(self):
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
        try:
            self._radio.tune(group)
            time.sleep(0.2)
            self._radio.tune(group)
        except Exception as e:
            print(f"  [KV4P] tune error: {e}")

    def _set_ptt(self, state_on):
        if not self._radio:
            return {"ok": False, "error": "not connected"}
        if state_on == self._ptt_on_state:
            return {"ok": True}
        try:
            if state_on:
                self._radio.ptt_on()
                self._tx_buf = b''
            else:
                self._radio.ptt_off()
            self._ptt_on_state = state_on
            self._transmitting = state_on
            self._status_dirty = True
            return {"ok": True, "ptt": state_on}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _start_polling(self):
        self._stop = False
        self._poll_thread = threading.Thread(
            target=self._poll_func, daemon=True, name="KV4P-poll")
        self._poll_thread.start()

    def _poll_func(self):
        reconnect_interval = float(_cfg(self._cfg_dict, 'reconnect_interval', 5.0))
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
                time.sleep(reconnect_interval)
                self._connect_radio()

    def _sync_processor(self):
        if not self._processor or not self._config:
            return
        p = self._processor
        c = self._cfg_dict
        p.enable_noise_gate = bool(_cfg(c, 'proc_enable_noise_gate', False))
        p.gate_threshold = float(_cfg(c, 'proc_noise_gate_threshold', -40))
        p.gate_attack = float(_cfg(c, 'proc_noise_gate_attack', 0.01))
        p.gate_release = float(_cfg(c, 'proc_noise_gate_release', 0.1))
        p.enable_hpf = bool(_cfg(c, 'proc_enable_hpf', True))
        p.hpf_cutoff = float(_cfg(c, 'proc_hpf_cutoff', 300))
        p.enable_lpf = bool(_cfg(c, 'proc_enable_lpf', False))
        p.lpf_cutoff = float(_cfg(c, 'proc_lpf_cutoff', 3000))
        p.enable_notch = bool(_cfg(c, 'proc_enable_notch', False))
        p.notch_freq = float(_cfg(c, 'proc_notch_freq', 1000))
        p.notch_q = float(_cfg(c, 'proc_notch_q', 30.0))

    def _send_test_tone(self, cmd):
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
        if self._recording_file:
            try:
                self._recording_file.close()
            except Exception:
                pass
            self._recording_file = None
            return {"ok": True, "recording": False}
        try:
            path = cmd.get('path', '/tmp/kv4p_capture.raw')
            self._recording_file = open(path, 'wb')
            return {"ok": True, "recording": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}
