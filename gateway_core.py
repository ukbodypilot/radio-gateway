#!/usr/bin/env python3
"""Core gateway services and main RadioGateway class."""

import sys
import os

def _get_version():
    try:
        import subprocess
        v = subprocess.check_output(
            ['git', 'describe', '--tags', '--always'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True).strip()
        return v.lstrip('v')
    except Exception:
        return "unknown"

__version__ = _get_version()
import time
import signal
import threading
import subprocess
import json as json_mod
import collections
import queue as _queue_mod
from struct import Struct
import socket
import array as _array_mod
import math as _math_mod
import re
import numpy as np

import ssl as _ssl
if not hasattr(_ssl, 'wrap_socket'):
    def _ssl_wrap_compat(sock, keyfile=None, certfile=None, server_side=False,
                         cert_reqs=None, ssl_version=None, ca_certs=None,
                         do_handshake_on_connect=True, suppress_ragged_eofs=True,
                         ciphers=None, **_):
        ctx = _ssl.SSLContext(
            _ssl.PROTOCOL_TLS_SERVER if server_side else _ssl.PROTOCOL_TLS_CLIENT
        )
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        ctx.minimum_version = _ssl.TLSVersion.MINIMUM_SUPPORTED
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
        if certfile:
            ctx.load_cert_chain(certfile, keyfile)
        if ca_certs:
            ctx.load_verify_locations(ca_certs)
        if ciphers:
            ctx.set_ciphers(ciphers)
        return ctx.wrap_socket(sock, server_side=server_side,
                               do_handshake_on_connect=do_handshake_on_connect,
                               suppress_ragged_eofs=suppress_ragged_eofs)
    _ssl.wrap_socket = _ssl_wrap_compat
if not hasattr(_ssl, 'PROTOCOL_TLSv1_2'):
    _ssl.PROTOCOL_TLSv1_2 = _ssl.PROTOCOL_TLS_CLIENT

try:
    from pymumble_py3 import Mumble
    from pymumble_py3.callbacks import PYMUMBLE_CLBK_SOUNDRECEIVED, PYMUMBLE_CLBK_TEXTMESSAGERECEIVED
    import pymumble_py3.constants as mumble_constants
    import pymumble_py3.mumble as _pymumble_mod
except ImportError:
    try:
        from pymumble import Mumble
        from pymumble.callbacks import PYMUMBLE_CLBK_SOUNDRECEIVED, PYMUMBLE_CLBK_TEXTMESSAGERECEIVED
        import pymumble.constants as mumble_constants
        import pymumble.mumble as _pymumble_mod
    except ImportError:
        print("ERROR: pymumble library not found!")
        sys.exit(1)

def _wrap_socket_compat(sock, keyfile=None, certfile=None,
                        verify_mode=_ssl.CERT_NONE, server_hostname=None):
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    ctx.minimum_version = _ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    if certfile:
        ctx.load_cert_chain(certfile, keyfile)
    return ctx.wrap_socket(sock, server_hostname=server_hostname)
_pymumble_mod._wrap_socket = _wrap_socket_compat

try:
    import pyaudio
except ImportError:
    print("ERROR: pyaudio library not found!")
    print("Install it with: sudo apt-get install python3-pyaudio")
    sys.exit(1)

try:
    import hid
except ImportError:
    print("ERROR: hidapi library not found!")
    print("Install it with: pip3 install hidapi --break-system-packages")
    sys.exit(1)

from audio_sources import (
    AudioSource, AudioProcessor, AIOCRadioSource, FilePlaybackSource,
    EchoLinkSource,
    RemoteAudioServer, RemoteAudioSource,
    NetworkAnnouncementSource,
    WebMicSource, WebMonitorSource, LinkAudioSource, StreamOutputSource, generate_cw_pcm,
)
from audio_bus import ListenBus
from gateway_utils import DDNSUpdater, EmailNotifier, CloudflareTunnel, MumbleServerManager, USBIPManager
from ptt import RelayController, GPIORelayController
from cat_client import RadioCATClient
from smart_announce import SmartAnnouncementManager
from web_server import WebConfigServer

class LogWriter:
    """Wraps sys.stdout to capture all output into a ring buffer for the web log viewer.

    Timestamps each line and stores in a deque. No terminal status bar —
    all status display is via the web UI.
    """

    def __init__(self, original, buffer_lines=2000, log_file=None, **_kwargs):
        self._orig = original
        self._lock = threading.Lock()
        self._at_line_start = True
        import collections
        self._log_buffer = collections.deque(maxlen=buffer_lines)
        self._log_seq = 0
        self._log_file = log_file
        for attr in ('encoding', 'errors', 'mode', 'name', 'newlines',
                     'fileno', 'isatty', 'readable', 'seekable', 'writable'):
            if hasattr(original, attr):
                try:
                    setattr(self, attr, getattr(original, attr))
                except (AttributeError, TypeError):
                    pass

    def _append_log(self, timestamped_line):
        """Add a line to the ring buffer and log file."""
        # Filter out status bar lines that leak into the log buffer.
        # Status bar contains dense ANSI color sequences with PTT/VAD/TX/RX markers.
        _t = timestamped_line
        if '\033[' in _t and ('PTT:' in _t or 'VAD:' in _t or 'UP:' in _t or '\033[A' in _t):
            return
        self._log_seq += 1
        self._log_buffer.append((self._log_seq, timestamped_line))
        if self._log_file:
            try:
                self._log_file.write(timestamped_line + '\n')
                self._log_file.flush()
            except Exception:
                pass

    def get_log_lines(self, after_seq=0, limit=200):
        """Return log lines with seq > after_seq. For web polling."""
        result = []
        for seq, line in self._log_buffer:
            if seq > after_seq:
                result.append((seq, line))
                if len(result) >= limit:
                    break
        return result

    def get_recent_lines(self, count=200):
        """Return the most recent N log lines."""
        items = list(self._log_buffer)
        return items[-count:] if len(items) > count else items

    def write(self, text):
        with self._lock:
            if text:
                import datetime as _dt
                lines = text.split('\n')
                out_parts = []
                for i, line in enumerate(lines):
                    if i > 0:
                        out_parts.append('\n')
                        self._at_line_start = True
                    if line:
                        if self._at_line_start:
                            _ts = _dt.datetime.now().strftime("%H:%M:%S")
                            stamped = f"[{_ts}] {line}"
                            out_parts.append(stamped)
                            self._append_log(stamped)
                        else:
                            out_parts.append(line)
                        self._at_line_start = False
                if text.endswith('\n'):
                    self._at_line_start = True
                self._orig.write(''.join(out_parts))
            else:
                self._orig.write(text)
        return len(text)

    def flush(self):
        self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


class RadioGateway:
    def __init__(self, config):
        self.config = config
        self.start_time = time.time()  # Track gateway start time for uptime
        self.aioc_device = None
        self.mumble = None
        self.secondary_mode = os.environ.get('GATEWAY_FEED_OCCUPIED') == '1'
        self.pyaudio_instance = None
        self.input_stream = None
        self.output_stream = None
        self.ptt_active = False
        self.running = True
        self.last_sound_time = 0
        self.last_audio_capture_time = 0
        self.audio_capture_active = False
        self.last_status_print = 0
        self.rx_audio_level = 0  # Received audio level (Mumble → Radio)
        self.tx_audio_level = 0  # Transmitted audio level (Radio → Mumble)
        self.sv_audio_level = 0  # Audio level sent to remote client (SV bar)
        self.last_rx_audio_time = 0  # When we last received audio
        self.stream_restart_count = 0
        self.last_stream_error = "None"
        self.restarting_stream = False  # Flag to prevent read during restart
        self.mumble_buffer_full_count = 0  # Track buffer full warnings
        self.last_buffer_clear = 0  # Last time we cleared the buffer
        
        # VOX (Voice Operated Switch) state for Radio → Mumble
        self.vox_active = False
        self.vox_level = 0.0
        self.last_vox_active_time = 0
        
        # VAD (Voice Activity Detection) state
        self.vad_active = False
        self.vad_envelope = 0.0
        self.vad_open_time = 0  # When VAD opened
        self.vad_close_time = 0  # When VAD closed
        self.vad_transmissions = 0  # Count of transmissions
        
        # Stream health monitoring
        self.last_successful_read = time.time()
        self.stream_age = 0  # How long current stream has been alive
        
        # Mute controls (keyboard toggle)
        self.tx_muted = False  # Mute Mumble → Radio (press 't')
        self.rx_muted = False  # Mute Radio → Mumble (press 'r')
        self.tx_talkback = getattr(self.config, 'TX_TALKBACK', False)  # TX audio to local outputs
        
        # Manual PTT control (keyboard toggle)
        self.manual_ptt_mode = False  # Manual PTT control (press 'p')
        self._pending_ptt_state = None  # Queued PTT change (applied between audio reads)
        self._ptt_change_time = 0.0  # Monotonic time of last PTT state change (for click suppression)
        self.announcement_delay_active = False   # True while waiting for PTT relay to settle before announcing
        self._announcement_ptt_delay_until = 0.0  # time.time() deadline for announcement delay

        # Speaker output (local monitoring)
        self.speaker_stream = None
        self.speaker_muted = self.config.SPEAKER_START_MUTED
        self.speaker_queue = None   # queue.Queue fed by main loop, drained by PortAudio callback
        self.speaker_audio_level = 0  # Tracks actual speaker output level for status bar

        # Restart flag (set by !restart command, checked in main() after run() exits)
        self.restart_requested = False

        # Web UI notification queue — recent warnings/errors shown as toasts
        import collections as _coll
        self._notifications = _coll.deque(maxlen=20)
        self._notif_seq = 0

        # Audio trace instrumentation — lightweight per-tick records written on shutdown.
        # Press 'i' to start/stop recording.  Data is dumped to tools/audio_trace.txt
        # on Ctrl+C shutdown.
        import collections as _collections_mod
        self._audio_trace = _collections_mod.deque(maxlen=12000)  # ~10 minutes at 20Hz
        self._audio_trace_t0 = 0.0  # set when recording starts
        self._trace_recording = False  # toggled by 'i' key
        self._spk_trace = _collections_mod.deque(maxlen=12000)  # speaker thread trace
        self._trace_events = _collections_mod.deque(maxlen=500)  # key presses / mode changes
        
        # Audio processing state (legacy — kept for backwards compat)
        self.gate_envelope = 0.0  # For noise gate smoothing
        self.highpass_state = None  # For high-pass filter state

        # Per-source audio processors
        self.radio_processor = AudioProcessor("radio", config)
        self.sdr_processor = AudioProcessor("sdr", config)    # placeholder, replaced by SDRPlugin's processor
        self.sdr2_processor = AudioProcessor("sdr2", config)  # placeholder, replaced by SDRPlugin's processor
        self.d75_processor = AudioProcessor("d75", config)
        self._sync_radio_processor()
        
        # Initialize audio bus (v2.0 mixer replacement) and sources
        self.mixer = ListenBus("monitor", config)
        self.radio_source = None  # Will be initialized after AIOC setup
        # sdr_source removed — use sdr_plugin  # SDR1 receiver audio source
        self.sdr_muted = False  # SDR1-specific mute
        self.sdr_ducked = False  # Is SDR1 currently being ducked (status display)
        self.sdr_audio_level = 0  # SDR1 audio level for status bar
        # sdr2_source removed — use sdr_plugin  # SDR2 receiver audio source
        self.sdr2_muted = False  # SDR2-specific mute
        self.sdr2_ducked = False  # Is SDR2 currently being ducked (status display)
        self.sdr2_audio_level = 0  # SDR2 audio level for status bar
        self.remote_audio_server = None   # RemoteAudioServer (role=server)
        self.remote_audio_source = None   # RemoteAudioSource (role=client)
        self.remote_audio_muted = False   # Client: mute toggle
        self.remote_audio_ducked = False  # Client: ducked state for status bar
        self.announce_input_source = None  # NetworkAnnouncementSource (port 9601)
        self.announce_input_muted = False # Announcement input: mute toggle
        self.web_mic_source = None        # WebMicSource (browser mic → radio TX)
        self.web_monitor_source = None    # WebMonitorSource (room monitor, no PTT)
        self.link_server = None           # GatewayLinkServer (multi-endpoint)
        self.link_endpoints = {}          # {name: LinkAudioSource}
        self.link_endpoint_settings = {}  # {name: {rx_muted, tx_muted}} — persisted
        self._link_ptt_active = {}        # {name: bool}
        self._link_last_status = {}       # {name: dict}
        self._link_tx_levels = {}         # {name: int}
        self._link_settings_path = os.path.expanduser('~/.config/radio-gateway/link_endpoints.json')
        self.aioc_available = False  # Track if AIOC is connected

        # SDR rebroadcast — route mixed SDR audio to AIOC radio TX
        self.sdr_rebroadcast = False              # Toggle state (press 'b')
        self._rebroadcast_ptt_hold_until = 0      # monotonic deadline for PTT hold
        self._rebroadcast_ptt_active = False       # whether rebroadcast currently has PTT keyed
        self._webmic_ptt_active = False             # whether browser mic has PTT keyed via CAT
        self._rebroadcast_sending = False           # SDR audio actively being sent (for status bar)

        # Relay control — radio power button (momentary pulse with 'j' key)
        self.relay_radio = None              # RelayController instance
        self._relay_radio_pressing = False   # True during 0.5s button pulse

        # Relay control — PTT relay (when PTT_METHOD = relay)
        self.relay_ptt = None          # RelayController instance

        # Relay control — charger schedule
        self.relay_charger = None      # RelayController instance
        self.relay_charger_on = False  # Current charge state
        self._charger_manual = False   # True when user manually overrode schedule
        self._charger_on_time = None   # (hour, minute) tuple
        self._charger_off_time = None  # (hour, minute) tuple

        # Smart Announcements (AI-powered, Claude or Gemini)
        self.smart_announce = None  # SmartAnnouncementManager instance

        # Automation Engine
        self.automation_engine = None  # AutomationEngine instance

        # Web configuration UI
        self.web_config_server = None

        # Dynamic DNS updater
        self.ddns_updater = None  # DDNSUpdater instance
        self.cloudflare_tunnel = None  # CloudflareTunnel instance
        self.email_notifier = None  # EmailNotifier instance

        # TH-9800 CAT control
        self.cat_client = None  # RadioCATClient instance

        # D75 CAT Control + Audio
        self.d75_plugin = None           # D75CATClient instance
        self.d75_plugin = None  # D75AudioSource instance
        self.d75_muted = False        # D75 audio mute toggle

        # KV4P HT Radio
        self.kv4p_plugin = None           # KV4PCATClient instance
        self.kv4p_plugin = None  # KV4PAudioSource instance
        self.kv4p_muted = False        # KV4P audio mute toggle
        self.kv4p_processor = AudioProcessor("kv4p", config)

        # Mumble Server instances (local mumble-server/murmurd)
        self.mumble_server_1 = None  # MumbleServerManager instance
        self.mumble_server_2 = None  # MumbleServerManager instance

        # DarkIce process monitoring (auto-restart if it dies)
        self._darkice_pid = None          # PID when initially detected
        self._darkice_was_running = False  # True if DarkIce was alive at startup
        self._darkice_restart_count = 0
        self._last_darkice_check = 0
        self._darkice_stats_cache = None   # Cached stats dict
        self._darkice_stats_time = 0       # Last stats fetch timestamp

        # Watchdog trace — low-fidelity long-running diagnostics (press 'u')
        # Samples every 5s into memory, flushes to disk every 60s.
        # Designed to run overnight/multi-day to catch freezes.
        self._watchdog_active = False
        self._watchdog_thread = None
        self._watchdog_t0 = 0.0           # start monotonic time
        self._tx_loop_tick = 0            # incremented every transmit loop tick

        # Thread references for watchdog health checks
        self._tx_thread = None
        self._status_thread = None
        self._keyboard_thread = None

        # Status bar writer — wraps stdout so print() clears the bar first
        self._status_writer = None
    
    def notify(self, message, level='error'):
        """Push a notification to the web UI. level: 'error', 'warning', 'info'."""
        self._notif_seq += 1
        self._notifications.append({
            'seq': self._notif_seq,
            'msg': message,
            'level': level,
            'ts': time.time(),
        })

    def _charger_should_be_on(self):
        """Check if charger should be on based on current time and schedule.
        Handles overnight wrap (e.g. 23:00 → 06:00)."""
        if not self._charger_on_time or not self._charger_off_time:
            return False
        import datetime
        now = datetime.datetime.now()
        cur = (now.hour, now.minute)
        on_t = self._charger_on_time
        off_t = self._charger_off_time
        if on_t <= off_t:
            # Same-day window (e.g. 06:00 → 18:00)
            return on_t <= cur < off_t
        else:
            # Overnight wrap (e.g. 23:00 → 06:00)
            return cur >= on_t or cur < off_t

    def calculate_audio_level(self, pcm_data):
        """Calculate RMS audio level from PCM data (0-100 scale)"""
        try:
            if not pcm_data:
                return 0
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(arr) == 0:
                return 0
            rms = float(np.sqrt(np.mean(arr * arr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                level = max(0, min(100, (db + 60) * (100/60)))
                return int(level)
            return 0
        except Exception:
            return 0

    def _update_sv_level(self, pcm_data):
        """Update sv_audio_level from PCM data sent to remote client."""
        current = self.calculate_audio_level(pcm_data)
        if current > self.sv_audio_level:
            self.sv_audio_level = current
        else:
            self.sv_audio_level = int(self.sv_audio_level * 0.7 + current * 0.3)

    def apply_highpass_filter(self, pcm_data):
        """Apply high-pass filter to remove low-frequency rumble"""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            # First-order IIR high-pass: H(z) = alpha*(1 - z^-1) / (1 - alpha*z^-1)
            cutoff = self.config.HIGHPASS_CUTOFF_FREQ
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b = np.array([alpha, -alpha], dtype=np.float64)
            a = np.array([1.0, -alpha], dtype=np.float64)

            # Initialize state on first call (zi shape: (1,))
            if self.highpass_state is None:
                self.highpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.highpass_state = lfilter(b, a, samples, zi=self.highpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()

        except Exception:
            return pcm_data
    
    def apply_noise_gate(self, pcm_data):
        """Apply noise gate with attack/release to reduce background hiss"""
        try:
            import array
            import math
            
            samples = array.array('h', pcm_data)
            if len(samples) == 0:
                return pcm_data
            
            # Convert threshold from dB to linear
            threshold_db = self.config.NOISE_GATE_THRESHOLD
            threshold = 32767.0 * pow(10.0, threshold_db / 20.0)
            
            # Attack and release times in samples
            attack_samples = (self.config.NOISE_GATE_ATTACK / 1000.0) * self.config.AUDIO_RATE
            release_samples = (self.config.NOISE_GATE_RELEASE / 1000.0) * self.config.AUDIO_RATE
            
            # Attack and release coefficients
            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1
            
            # Apply gate with envelope follower
            gated = []
            for sample in samples:
                # Calculate signal level (absolute value)
                level = abs(sample)
                
                # Update envelope with attack/release
                if level > self.gate_envelope:
                    self.gate_envelope += (level - self.gate_envelope) * attack_coef
                else:
                    self.gate_envelope += (level - self.gate_envelope) * release_coef
                
                # Calculate gain based on envelope vs threshold
                if self.gate_envelope > threshold:
                    gain = 1.0
                else:
                    # Smooth transition below threshold
                    ratio = self.gate_envelope / threshold if threshold > 0 else 0
                    gain = ratio * ratio  # Quadratic for smooth fade
                
                gated.append(int(sample * gain))
            
            return array.array('h', gated).tobytes()
            
        except Exception:
            return pcm_data
    
    def _sync_radio_processor(self):
        """Sync global config flags into the radio AudioProcessor instance."""
        p = self.radio_processor
        p.enable_hpf = self.config.ENABLE_HIGHPASS_FILTER
        p.hpf_cutoff = self.config.HIGHPASS_CUTOFF_FREQ
        p.enable_lpf = self.config.ENABLE_LOWPASS_FILTER
        p.lpf_cutoff = self.config.LOWPASS_CUTOFF_FREQ
        p.enable_notch = self.config.ENABLE_NOTCH_FILTER
        p.notch_freq = self.config.NOTCH_FREQ
        p.notch_q = self.config.NOTCH_Q
        p.enable_noise_gate = self.config.ENABLE_NOISE_GATE
        p.gate_threshold = self.config.NOISE_GATE_THRESHOLD
        p.gate_attack = self.config.NOISE_GATE_ATTACK
        p.gate_release = self.config.NOISE_GATE_RELEASE

    def _sync_sdr_plugin_processors(self):
        """Sync SDR processing config into the SDRPlugin's processor instances."""
        if self.sdr_plugin:
            from sdr_plugin import SDRPlugin
            SDRPlugin._sync_processor(self.sdr_plugin._processor1, self.config)
            SDRPlugin._sync_processor(self.sdr_plugin._processor2, self.config)

    def _sync_d75_plugin_processor(self):
        """Sync D75 processing config into the D75Plugin's processor."""
        if self.d75_plugin and self.d75_plugin._processor:
            self.d75_plugin._sync_processor()

    # process_audio_for_d75 removed — D75Plugin handles processing internally

    def _sync_kv4p_plugin_processor(self):
        """Sync KV4P processing config into the KV4PPlugin's processor."""
        if self.kv4p_plugin and self.kv4p_plugin._processor:
            self.kv4p_plugin._sync_processor()

    # process_audio_for_kv4p removed — KV4PPlugin handles processing internally

    def process_audio_for_mumble(self, pcm_data):
        """Apply all enabled audio processing to clean up radio audio before sending to Mumble.
        Now delegates to the radio AudioProcessor instance.
        """
        # Keep legacy state in sync (old code may read self.gate_envelope etc.)
        self._sync_radio_processor()
        result = self.radio_processor.process(pcm_data)
        self.gate_envelope = self.radio_processor.gate_envelope
        self.highpass_state = self.radio_processor.highpass_state
        return result

    def _load_link_settings(self):
        """Load saved per-endpoint settings (rx_muted, tx_muted) from JSON."""
        try:
            with open(self._link_settings_path) as f:
                import json as _json
                self.link_endpoint_settings = _json.load(f)
        except (FileNotFoundError, ValueError):
            self.link_endpoint_settings = {}

    def _save_link_settings(self):
        """Persist per-endpoint settings to JSON."""
        import json as _json
        try:
            os.makedirs(os.path.dirname(self._link_settings_path), exist_ok=True)
            with open(self._link_settings_path, 'w') as f:
                _json.dump(self.link_endpoint_settings, f, indent=2)
        except Exception as e:
            print(f"  [Link] Failed to save settings: {e}")

    # process_audio_for_sdr removed — SDRPlugin handles processing internally

    def check_vad(self, pcm_data):
        """Voice Activity Detection - determines if audio should be sent to Mumble"""
        if not self.config.ENABLE_VAD:
            return True  # VAD disabled, always send

        try:
            if not pcm_data:
                return False
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(arr) == 0:
                return False

            # Calculate RMS level
            rms = float(np.sqrt(np.mean(arr * arr)))

            # Convert to dB
            if rms > 0:
                db_level = 20 * _math_mod.log10(rms / 32767.0)
            else:
                db_level = -100
            
            # Attack and release coefficients (samples per second)
            chunks_per_second = self.config.AUDIO_RATE / self.config.AUDIO_CHUNK_SIZE
            attack_coef = 1.0 / (self.config.VAD_ATTACK * chunks_per_second)
            release_coef = 1.0 / (self.config.VAD_RELEASE * chunks_per_second)
            
            # Update envelope follower
            if db_level > self.vad_envelope:
                # Attack: fast rise
                self.vad_envelope += (db_level - self.vad_envelope) * min(1.0, attack_coef)
            else:
                # Release: slow decay
                self.vad_envelope += (db_level - self.vad_envelope) * min(1.0, release_coef)
            
            current_time = time.time()
            
            # Check if signal exceeds threshold
            if self.vad_envelope > self.config.VAD_THRESHOLD:
                if not self.vad_active:
                    # VAD opening
                    self.vad_active = True
                    self.vad_open_time = current_time
                    self.vad_transmissions += 1
                return True
            else:
                # Below threshold
                if self.vad_active:
                    # Check minimum duration
                    open_duration = current_time - self.vad_open_time  # seconds
                    if open_duration < self.config.VAD_MIN_DURATION:
                        # Haven't met minimum duration yet, stay open
                        return True
                    
                    # Check release time
                    if self.vad_close_time == 0:
                        self.vad_close_time = current_time
                    
                    release_duration = current_time - self.vad_close_time  # seconds
                    if release_duration < self.config.VAD_RELEASE:
                        # Still in release tail
                        return True
                    else:
                        # Release complete, close VAD
                        self.vad_active = False
                        self.vad_close_time = 0
                        return False
                else:
                    # VAD is closed and staying closed
                    self.vad_close_time = 0
                    return False
                    
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"\n[VAD] Error: {e}")
            return True  # On error, allow transmission
    
    def check_vox(self, pcm_data):
        """Check if audio level exceeds VOX threshold (indicates radio is receiving)"""
        if not self.config.ENABLE_VOX:
            return True  # VOX disabled, always transmit
        
        try:
            if not pcm_data:
                return False
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(arr) == 0:
                return False

            # Calculate RMS level
            rms = float(np.sqrt(np.mean(arr * arr)))

            # Convert to dB
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
            else:
                db = -100  # Very quiet

            # Attack and release timing
            attack_time = self.config.VOX_ATTACK_TIME / 1000.0  # ms to seconds
            release_time = self.config.VOX_RELEASE_TIME / 1000.0
            
            # Update VOX level with attack/release envelope
            if db > self.vox_level:
                # Attack: fast rise
                self.vox_level = db
            else:
                # Release: slow decay
                # Calculate decay rate to reach threshold in release_time
                decay_rate = abs(self.config.VOX_THRESHOLD - db) / (release_time * (self.config.AUDIO_RATE / self.config.AUDIO_CHUNK_SIZE))
                self.vox_level = max(db, self.vox_level - decay_rate)
            
            # Check if above threshold
            if self.vox_level > self.config.VOX_THRESHOLD:
                if not self.vox_active:
                    if self.config.VERBOSE_LOGGING:
                        print(f"\n[VOX] Radio receiving (level: {self.vox_level:.1f} dB)")
                self.vox_active = True
                self.last_vox_active_time = time.time()
                return True
            else:
                # Check if we're still in release period
                time_since_active = time.time() - self.last_vox_active_time
                if time_since_active < release_time:
                    return True  # Still in tail
                else:
                    if self.vox_active:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n[VOX] Radio silent (level: {self.vox_level:.1f} dB)")
                    self.vox_active = False
                    return False
                    
        except Exception:
            return True  # On error, allow transmission
        
    def set_ptt_state(self, state_on):
        """Control PTT — routes to the configured TX radio plugin."""
        tx_radio = str(getattr(self.config, 'TX_RADIO', 'th9800')).lower()
        if tx_radio == 'd75' and self.d75_plugin:
            self.d75_plugin.execute({'cmd': 'ptt', 'state': state_on})
        elif tx_radio == 'kv4p' and self.kv4p_plugin:
            self.kv4p_plugin.execute({'cmd': 'ptt', 'state': state_on})
        elif self.th9800_plugin:
            self.th9800_plugin.execute({'cmd': 'ptt', 'state': state_on})
        self.ptt_active = state_on

    def _ptt_aioc(self, state_on):
        """PTT via AIOC HID GPIO.

        RTS controls a relay that connects the radio's TX serial line to either
        the USB dongle (USB Controlled) or the radio front panel (Radio Controlled).
        AIOC PTT requires Radio Controlled mode or PTT fails due to mic wiring.
        While Radio Controlled, CAT commands cannot be sent/received.
        """
        if not self.aioc_device:
            if state_on:
                self.notify("PTT failed: AIOC device not found")
            return
        _cat = getattr(self, 'cat_client', None)
        try:
            if state_on:
                # Switch RTS to Radio Controlled and pause CAT drain before keying
                if _cat:
                    _cat._pause_drain()
                    try:
                        _cat.set_rts(False)  # Radio Controlled
                    except Exception as e:
                        print(f"\n[PTT] RTS switch failed: {e}")
                        # drain stays paused — will be resumed on unkey
            state = 1 if state_on else 0
            iomask = 1 << (self.config.AIOC_PTT_CHANNEL - 1)
            iodata = state << (self.config.AIOC_PTT_CHANNEL - 1)
            data = Struct("<BBBBB").pack(0, 0, iodata, iomask, 0)
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (AIOC GPIO{self.config.AIOC_PTT_CHANNEL})")
            self.aioc_device.write(bytes(data))
            if not state_on:
                # Unkeyed — restore RTS to USB Controlled and resume CAT drain
                if _cat:
                    try:
                        _cat.set_rts(True)  # USB Controlled
                    except Exception as e:
                        print(f"\n[PTT] RTS restore failed: {e}")
                    finally:
                        _cat._drain_paused = False
        except Exception as e:
            print(f"\n[PTT] AIOC error: {e}")
            self.notify(f"PTT error: {e}")
            # Ensure drain is resumed on any error
            if _cat and _cat._drain_paused:
                _cat._drain_paused = False

    def _ptt_relay(self, state_on):
        """PTT via CH340 USB relay."""
        if not self.relay_ptt:
            return
        self.relay_ptt.set_state(state_on)
        if self.config.VERBOSE_LOGGING:
            print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (relay)")

    def _ptt_software(self, state_on):
        """PTT via CAT TCP !ptt on/off command."""
        if not self.cat_client:
            if state_on:
                self.notify("PTT failed: CAT not connected")
            return
        try:
            self.cat_client._pause_drain()
            try:
                resp = self.cat_client._send_cmd("!ptt on" if state_on else "!ptt off")
            finally:
                self.cat_client._drain_paused = False
            if resp and 'serial not connected' in resp.lower():
                self.notify("PTT failed: radio serial not connected")
                return
            if resp is None:
                self.notify("PTT failed: no response from CAT server")
                return
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (software/CAT)")
        except Exception as e:
            print(f"\n[PTT] CAT !ptt error: {e}")
            self.notify(f"PTT failed: {e}")
    
    _d75_ptt_on = False  # Track D75 PTT state

    def _ptt_d75(self, state_on):
        """PTT via D75 CAT TCP !ptt on/off command.

        D75 uses explicit on/off (not toggle like TH-9800).
        No RTS switching needed — D75 doesn't use the RTS relay.
        CRITICAL: fire-and-forget — write bytes to socket without waiting
        for response. Using _send_cmd would compete with the poll thread
        for the socket lock and response parsing, causing 1-5s delays
        that starve the audio mixer.
        """
        d75 = getattr(self, 'd75_plugin', None)
        if not d75 or not d75._connected:
            if state_on:
                self.notify("PTT failed: D75 not connected")
            return
        if state_on == self._d75_ptt_on:
            return
        self._d75_ptt_on = state_on
        cmd = "!ptt on" if state_on else "!ptt off"
        try:
            if d75._sock:
                d75._sock.sendall(f"{cmd}\n".encode())
                print(f"  [PTT] {'KEYED' if state_on else 'UNKEYED'} D75 (fire-and-forget)")
        except Exception as e:
            print(f"\n[PTT] D75 !ptt send error: {e}")
            self._d75_ptt_on = False

    _kv4p_ptt_on = False  # Track KV4P PTT state

    def _ptt_kv4p(self, state_on):
        """PTT via KV4P HT serial — direct ptt_on/ptt_off."""
        cat = getattr(self, 'kv4p_plugin', None)
        if not cat:
            if state_on:
                self.notify("PTT failed: KV4P not connected")
            return
        if state_on == self._kv4p_ptt_on:
            return
        try:
            if state_on:
                cat.ptt_on()
            else:
                cat.ptt_off()
                # Discard any partial Opus frame so it doesn't bleed into next TX
                if self.kv4p_plugin:
                    self.kv4p_plugin._tx_buf = b''
            self._kv4p_ptt_on = state_on
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (KV4P)")
        except Exception as e:
            print(f"\n[PTT] KV4P ptt error: {e}")
            self.notify(f"PTT failed: {e}")

    def sound_received_handler(self, user, soundchunk):
        """Called when audio is received from Mumble server"""
        _t0 = time.monotonic()

        # Feed MumbleSource for routing system
        if hasattr(self, 'mumble_source') and self.mumble_source:
            self.mumble_source.push_audio(soundchunk.pcm)

        _t1 = time.monotonic()

        # Track when we last received audio
        self.last_rx_audio_time = time.time()

        # Calculate audio level (with smoothing)
        current_level = self.calculate_audio_level(soundchunk.pcm)
        # Smooth the level display (fast attack, slow decay)
        if current_level > self.rx_audio_level:
            self.rx_audio_level = current_level
        else:
            self.rx_audio_level = int(self.rx_audio_level * 0.7 + current_level * 0.3)

        _t2 = time.monotonic()

        # Update last sound time
        self.last_sound_time = time.time()

        _t3 = time.monotonic()
        # Timing diagnostic
        if not hasattr(self, '_srh_count'):
            self._srh_count = 0
            self._srh_max_ms = 0.0
            self._srh_total_ms = 0.0
        self._srh_count += 1
        _elapsed = (_t3 - _t0) * 1000
        _push_ms = (_t1 - _t0) * 1000
        _level_ms = (_t2 - _t1) * 1000
        self._srh_total_ms += _elapsed
        if _elapsed > self._srh_max_ms:
            self._srh_max_ms = _elapsed
        if self._srh_count <= 3 or self._srh_count % 50 == 0:
            _avg = self._srh_total_ms / self._srh_count
            print(f"  [SRH] #{self._srh_count}: {_elapsed:.2f}ms (push={_push_ms:.2f} level={_level_ms:.2f}) avg={_avg:.2f}ms max={self._srh_max_ms:.2f}ms")
        # MumbleSource.push_audio() feeds the queue, SoloBus drains it
        # and calls put_audio() + PTT on the radio plugin.
        # Legacy direct path disabled to avoid double-writing to output stream.
    
    def find_usb_device_path(self):
        """Find the USB device path for the AIOC"""
        try:
            import subprocess
            # Find USB device using VID:PID
            result = subprocess.run(
                ['lsusb', '-d', f'{self.config.AIOC_VID:04x}:{self.config.AIOC_PID:04x}'],
                capture_output=True, text=True
            )
            
            if result.returncode == 0 and result.stdout:
                # Parse output like: "Bus 001 Device 003: ID 1209:7388"
                parts = result.stdout.split()
                if len(parts) >= 4:
                    bus = parts[1]
                    device = parts[3].rstrip(':')
                    return f"/sys/bus/usb/devices/{bus}-*"
            return None
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"  [Diagnostic] Could not find USB device path: {e}")
            return None
    
    def reset_usb_device(self):
        """Attempt to reset the AIOC USB device by power cycling"""
        if self.config.VERBOSE_LOGGING:
            print("  [Diagnostic] Attempting USB device reset...")
        
        try:
            import subprocess
            import glob
            
            # Method 1: Try using usbreset if available
            try:
                result = subprocess.run(
                    ['which', 'usbreset'],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    # usbreset is available
                    result = subprocess.run(
                        ['sudo', 'usbreset', f'{self.config.AIOC_VID:04x}:{self.config.AIOC_PID:04x}'],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        if self.config.VERBOSE_LOGGING:
                            print("  ✓ USB device reset via usbreset")
                        time.sleep(2)  # Wait for device to re-enumerate
                        return True
            except:
                pass
            
            # Method 2: Try sysfs unbind/bind
            try:
                # Find the device in sysfs
                usb_devices = glob.glob(f'/sys/bus/usb/devices/*')
                for dev_path in usb_devices:
                    try:
                        # Read vendor and product IDs
                        with open(f'{dev_path}/idVendor', 'r') as f:
                            vid = f.read().strip()
                        with open(f'{dev_path}/idProduct', 'r') as f:
                            pid = f.read().strip()
                        
                        if vid == f'{self.config.AIOC_VID:04x}' and pid == f'{self.config.AIOC_PID:04x}':
                            # Found our device
                            device_name = os.path.basename(dev_path)
                            
                            # Try to unbind
                            with open('/sys/bus/usb/drivers/usb/unbind', 'w') as f:
                                f.write(device_name)
                            
                            time.sleep(1)
                            
                            # Rebind
                            with open('/sys/bus/usb/drivers/usb/bind', 'w') as f:
                                f.write(device_name)
                            
                            if self.config.VERBOSE_LOGGING:
                                print("  ✓ USB device reset via sysfs unbind/bind")
                            time.sleep(2)
                            return True
                            
                    except (IOError, PermissionError):
                        continue
            except Exception as e:
                if self.config.VERBOSE_LOGGING:
                    print(f"  [Diagnostic] sysfs method failed: {e}")
            
            # Method 3: Try autoreset (no sudo needed)
            try:
                result = subprocess.run(
                    ['lsusb', '-d', f'{self.config.AIOC_VID:04x}:{self.config.AIOC_PID:04x}', '-v'],
                    capture_output=True, text=True, timeout=5
                )
                # Sometimes just querying the device helps
                time.sleep(1)
            except:
                pass
                
            if self.config.VERBOSE_LOGGING:
                print("  ⚠ USB reset methods require sudo permissions")
                print("  Please run: sudo chmod 666 /sys/bus/usb/drivers/usb/unbind")
                print("             sudo chmod 666 /sys/bus/usb/drivers/usb/bind")
                print("  Or manually unplug and replug the AIOC device")
            
            return False
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"  ✗ USB reset failed: {type(e).__name__}: {e}")
            return False
    
    def setup_aioc(self):
        """Initialize AIOC device"""
        if self.config.VERBOSE_LOGGING:
            print("Initializing AIOC device...")
        try:
            # Use hid.Device (capital D) - this is what's available
            self.aioc_device = hid.Device(vid=self.config.AIOC_VID, pid=self.config.AIOC_PID)
            print(f"✓ AIOC: {self.aioc_device.product}")
            return True
        except Exception as e:
            print(f"✗ Could not open AIOC: {e}")
            return False
    
    def find_aioc_audio_device(self):
        """Find AIOC audio device index"""
        # Suppress ALSA warnings if not in verbose mode
        import os

        # Only suppress if not verbose
        if not self.config.VERBOSE_LOGGING:
            # Hardcode fd 2 — sys.stderr may be LogWriter (fileno→stdout)
            saved_stderr = os.dup(2)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.close(devnull)
                p = pyaudio.PyAudio()
            finally:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
        else:
            p = pyaudio.PyAudio()
        
        aioc_input_index = None
        aioc_output_index = None
        
        # Check if manually specified
        if self.config.AIOC_INPUT_DEVICE >= 0:
            aioc_input_index = self.config.AIOC_INPUT_DEVICE
        if self.config.AIOC_OUTPUT_DEVICE >= 0:
            aioc_output_index = self.config.AIOC_OUTPUT_DEVICE
        
        # Auto-detect if not specified
        if aioc_input_index is None or aioc_output_index is None:
            if self.config.VERBOSE_LOGGING:
                print("\nSearching for AIOC audio device...")
                print("Available audio devices:")
            
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                name = info['name'].lower()
                
                if self.config.VERBOSE_LOGGING:
                    print(f"  [{i}] {info['name']} (in:{info['maxInputChannels']}, out:{info['maxOutputChannels']})")
                
                # Look for AIOC device by various names
                if any(keyword in name for keyword in ['aioc', 'all-in-one', 'cm108', 'usb audio', 'usb sound']):
                    if self.config.VERBOSE_LOGGING:
                        print(f"    → Potential AIOC device!")
                    if info['maxInputChannels'] > 0 and aioc_input_index is None:
                        aioc_input_index = i
                        if self.config.VERBOSE_LOGGING:
                            print(f"    → Using as INPUT device")
                    if info['maxOutputChannels'] > 0 and aioc_output_index is None:
                        aioc_output_index = i
                        if self.config.VERBOSE_LOGGING:
                            print(f"    → Using as OUTPUT device")
        
        p.terminate()
        return aioc_input_index, aioc_output_index
    
    def find_speaker_device(self, p):
        """Resolve SPEAKER_OUTPUT_DEVICE string to a (index, name) tuple (index may be None for default)."""
        spec = self.config.SPEAKER_OUTPUT_DEVICE.strip()
        if not spec:
            return None, 'system default'
        if spec.isdigit():
            idx = int(spec)
            try:
                name = p.get_device_info_by_index(idx)['name']
            except Exception:
                name = spec
            return idx, name
        # Name search
        if self.config.VERBOSE_LOGGING:
            print("\nSearching for speaker output device...")
            print("Available output devices:")
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if self.config.VERBOSE_LOGGING:
                print(f"  [{i}] {info['name']} (out:{info['maxOutputChannels']})")
            if info['maxOutputChannels'] > 0 and spec.lower() in info['name'].lower():
                return i, info['name']
        print(f"Warning: Speaker output device '{spec}' not found -- using system default")
        return None, 'system default'

    def _source_on_listen_bus(self, source_id):
        """Check if a source is connected to the PRIMARY listen bus in routing config.

        Only the first listen bus is the primary (handled by main loop mixer).
        Secondary listen busses are handled by BusManager.
        """
        try:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'routing_config.json')
            with open(config_path) as f:
                data = json_mod.load(f)
            # Find the primary listen bus (first one)
            primary_id = None
            for b in data.get('busses', []):
                if b.get('type') == 'listen':
                    primary_id = b['id']
                    break
            if not primary_id:
                print(f"  [routing] {source_id}: no listen bus found")
                return False
            for c in data.get('connections', []):
                if c['type'] == 'source-bus' and c['from'] == source_id and c['to'] == primary_id:
                    print(f"  [routing] {source_id} → primary listen bus '{primary_id}' ✓")
                    return True
            print(f"  [routing] {source_id} not on primary listen bus '{primary_id}'")
        except Exception as e:
            print(f"  [routing] _source_on_listen_bus error for {source_id}: {e}")
        return False

    def sync_mixer_sources(self):
        """Reconcile primary ListenBus sources with routing config.

        Called after routing save to add/remove sources from the mixer
        so reality matches what the routing UI shows.
        """
        if not self.mixer:
            return
        _before = {s.source.name for s in self.mixer.source_slots}
        print(f"  [sync] BEFORE: primary mixer sources = {_before}")
        # Map source_id → (plugin, priority, duckable)
        source_map = {}
        if self.sdr_plugin:
            source_map['sdr'] = (self.sdr_plugin, 11, getattr(self.config, 'SDR_DUCK', True))
        if self.th9800_plugin:
            source_map['aioc'] = (self.th9800_plugin, 1, False)
        if self.kv4p_plugin:
            source_map['kv4p'] = (self.kv4p_plugin, int(getattr(self.config, 'KV4P_AUDIO_PRIORITY', 2)) + 10, getattr(self.config, 'KV4P_AUDIO_DUCK', True))
        if self.d75_plugin:
            source_map['d75'] = (self.d75_plugin, int(getattr(self.config, 'D75_AUDIO_PRIORITY', 2)) + 10, getattr(self.config, 'D75_AUDIO_DUCK', True))
        if getattr(self, 'playback_source', None):
            source_map['playback'] = (self.playback_source, 0, False)
        if getattr(self, 'web_mic_source', None):
            source_map['webmic'] = (self.web_mic_source, 0, False)
        if getattr(self, 'announce_input_source', None):
            source_map['announce'] = (self.announce_input_source, 0, False)
        if getattr(self, 'web_monitor_source', None):
            source_map['monitor'] = (self.web_monitor_source, 5, False)
        if getattr(self, 'mumble_source', None):
            source_map['mumble_rx'] = (self.mumble_source, 0, False)

        # Which sources should be on the listen bus?
        should_be_on = set()
        for sid in source_map:
            if self._source_on_listen_bus(sid):
                should_be_on.add(sid)

        # Current sources on mixer
        current_names = {s.source.name for s in self.mixer.source_slots}

        # Add missing
        for sid in should_be_on:
            plugin, prio, duck = source_map[sid]
            if plugin.name not in current_names:
                _det = getattr(plugin, 'ptt_control', False)
                self.mixer.add_source(plugin, bus_priority=prio, duckable=duck, deterministic=_det)
                print(f"  [sync] Added {sid} to listen bus (det={_det})")

        # Remove extras (only for sources we manage)
        for sid, (plugin, _, _) in source_map.items():
            if sid not in should_be_on and plugin.name in current_names:
                self.mixer.remove_source(plugin.name)
                print(f"  [sync] Removed {sid} from listen bus")

        _final = {s.source.name for s in self.mixer.source_slots}
        print(f"  [sync] Primary mixer sources: {_final}")

    def _speaker_enqueue(self, data):
        """Route audio to speaker — either real PortAudio or virtual (metering only).

        Virtual mode: just track audio level for the routing page, no actual output.
        Real mode: apply volume, queue for PortAudio callback.
        """
        if self.speaker_muted or not data:
            return

        # Level metering (always, regardless of mode)
        try:
            spk = data
            if self.config.SPEAKER_VOLUME != 1.0:
                arr = np.frombuffer(spk, dtype=np.int16).astype(np.float32)
                spk = np.clip(arr * self.config.SPEAKER_VOLUME, -32768, 32767).astype(np.int16).tobytes()
            current_level = self.calculate_audio_level(spk)
            if current_level > self.speaker_audio_level:
                self.speaker_audio_level = current_level
            else:
                self.speaker_audio_level = int(self.speaker_audio_level * 0.7 + current_level * 0.3)
        except Exception:
            return

        # Virtual speaker — metering only, no real audio output
        if not self.speaker_queue:
            return

        try:
            # Absorb hw/sw clock drift: drain excess when queue gets deep.
            _spk_qd = self.speaker_queue.qsize()
            if _spk_qd >= 4:
                while self.speaker_queue.qsize() > 2:
                    try:
                        self.speaker_queue.get_nowait()
                    except Exception:
                        break
            self.speaker_queue.put_nowait(spk)
        except Exception:
            pass

    def _speaker_callback(self, in_data, frame_count, time_info, status):
        """PortAudio callback for speaker output.  Runs on PortAudio's own
        real-time audio thread.  PortAudio maintains an internal buffer
        (typically 2-3 periods ≈ 100-150ms) so brief GIL delays from other
        Python threads (e.g. status bar) are absorbed without underruns."""
        _cb_t0 = time.monotonic()
        _expected_bytes = frame_count * self.config.AUDIO_CHANNELS * 2
        try:
            chunk = self.speaker_queue.get_nowait()
            if self.speaker_muted:
                chunk = b'\x00' * _expected_bytes
            elif len(chunk) < _expected_bytes:
                chunk = chunk + b'\x00' * (_expected_bytes - len(chunk))
        except Exception:
            chunk = b'\x00' * _expected_bytes  # silence on underrun

        if self._trace_recording:
            _qd = self.speaker_queue.qsize()
            self._spk_trace.append((
                _cb_t0 - self._audio_trace_t0,          # 0: time (s)
                0.0,                                      # 1: wait_ms (n/a for callback)
                (time.monotonic() - _cb_t0) * 1000,      # 2: callback_ms
                _qd,                                      # 3: queue depth after get
                len(chunk),                               # 4: data_len
                False,                                    # 5: was_empty
                self.speaker_muted,                       # 6: was_muted
            ))

        return (chunk, pyaudio.paContinue)

    def open_speaker_output(self):
        """Open speaker output — real PortAudio device or virtual (metering only).

        Virtual mode: no PortAudio stream opened, no PipeWire link to mess with.
        Audio level is still tracked for the routing page. Use SPEAKER_MODE=virtual
        in config, or it auto-falls back to virtual if the device isn't found.
        """
        if not self.config.ENABLE_SPEAKER_OUTPUT:
            return

        speaker_mode = str(getattr(self.config, 'SPEAKER_MODE', 'auto')).lower()

        if speaker_mode == 'virtual':
            # Virtual speaker — metering only, no real audio
            print(f"✓ Speaker output: virtual (metering only, no audio device)")
            return

        # Try to open a real PortAudio device
        try:
            device_index, device_name = self.find_speaker_device(self.pyaudio_instance)
            if device_index is None and speaker_mode == 'auto':
                # No matching device — fall back to virtual
                print(f"✓ Speaker output: virtual (no device found, metering only)")
                return
            import queue
            self.speaker_queue = queue.Queue(maxsize=6)
            self.speaker_stream = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=self.config.AUDIO_CHANNELS,
                rate=self.config.AUDIO_RATE,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE,
                stream_callback=self._speaker_callback,
            )
            print(f"✓ Speaker output initialized OK")
            print(f"  Device: {device_name}")
        except Exception as e:
            print(f"✓ Speaker output: virtual (device open failed: {e})")
            self.speaker_stream = None

    def setup_audio(self):
        """Initialize PyAudio streams"""
        if self.config.VERBOSE_LOGGING:
            print("Initializing audio...")
        
        try:
            # Initialize SDR plugin BEFORE TH-9800/PyAudio — rtl_airband subprocess
            # forks must happen before PortAudio initializes (Pa_Initialize is not
            # fork-safe and will SIGSEGV if a fork happens after init).
            self.sdr_plugin = None
            if self.config.ENABLE_SDR or getattr(self.config, 'ENABLE_SDR2', False):
                try:
                    from sdr_plugin import SDRPlugin
                    print("Initializing SDR plugin (RSPduo dual tuner)...")
                    self.sdr_plugin = SDRPlugin()
                    if self.sdr_plugin.setup(self.config):
                        if self._source_on_listen_bus('sdr'):
                            self.mixer.add_source(self.sdr_plugin, bus_priority=11, duckable=getattr(self.config, 'SDR_DUCK', True))
                            print("✓ SDR plugin added to mixer (listen bus)")
                        else:
                            print("✓ SDR plugin initialized (routed via bus manager)")
                    else:
                        self.sdr_plugin = None
                except Exception as sdr_err:
                    print(f"⚠ Warning: SDR plugin: {sdr_err}")
                    self.sdr_plugin = None

            if self.sdr_plugin:
                self.sdr_processor = self.sdr_plugin._processor1
                self.sdr2_processor = self.sdr_plugin._processor2

            # Initialize TH-9800 plugin (AIOC + CAT + relays + audio streams)
            self.th9800_plugin = None
            try:
                from th9800_plugin import TH9800Plugin
                print("Initializing TH-9800 plugin...")
                self.th9800_plugin = TH9800Plugin()
                if self.th9800_plugin.setup(self.config, gateway=self):
                    # Only add to primary ListenBus if routed there (not to a solo/other bus)
                    if self._source_on_listen_bus('aioc'):
                        self.mixer.add_source(self.th9800_plugin, bus_priority=1, duckable=False)
                        print("✓ TH-9800 plugin added to mixer (listen bus)")
                    else:
                        print("✓ TH-9800 plugin initialized (routed via bus manager)")
                else:
                    print("⚠ TH-9800 plugin setup failed")
                    self.th9800_plugin = None
            except Exception as e:
                print(f"⚠ TH-9800 plugin error: {e}")
                import traceback; traceback.print_exc()
                self.th9800_plugin = None

            # Backward compat: expose plugin internals for code that still uses them
            if self.th9800_plugin:
                self.radio_source = self.th9800_plugin
                self.pyaudio_instance = self.th9800_plugin._pyaudio
                self.input_stream = self.th9800_plugin._input_stream
                self.output_stream = self.th9800_plugin._output_stream
                self.aioc_device = self.th9800_plugin._aioc_device
                self.aioc_available = self.th9800_plugin._aioc_available
                self.cat_client = self.th9800_plugin._cat_client
                self.radio_processor = self.th9800_plugin._processor
            else:
                self.radio_source = None

            self.stream_age = time.time()

            self.open_speaker_output()

            # Initialize file playback source if enabled
            if self.config.ENABLE_PLAYBACK:
                try:
                    self.playback_source = FilePlaybackSource(self.config, self)
                    # NOT added to primary ListenBus — routed via BusManager
                    print("✓ File playback source initialized (routed via bus manager)")
                    
                    # Show available audio files
                    import os
                    import glob
                    audio_dir = self.playback_source.announcement_directory
                    # File scanning and mapping happens in FilePlaybackSource.__init__
                    # Mapping will be displayed later (just before status bar)
                    
                except Exception as playback_err:
                    print(f"⚠ Warning: Could not initialize playback source: {playback_err}")
                    self.playback_source = None
            else:
                self.playback_source = None
            
            # Initialize text-to-speech if enabled
            self.tts_engine = None
            self._tts_backend = str(getattr(self.config, 'TTS_ENGINE', 'edge')).lower().strip()
            if self.config.ENABLE_TTS:
                try:
                    print("Initializing text-to-speech...")
                    if self._tts_backend == 'edge':
                        import edge_tts
                        self.tts_engine = edge_tts
                        print("✓ Text-to-speech (Edge TTS / Microsoft Neural) initialized")
                    else:
                        from gtts import gTTS
                        self.tts_engine = gTTS
                        print("✓ Text-to-speech (gTTS / Google) initialized")
                    print("  Use !speak <text> in Mumble to generate TTS")
                except ImportError:
                    pkg = 'edge-tts' if self._tts_backend == 'edge' else 'gtts'
                    print(f"⚠ {pkg} not installed")
                    print(f"  Install with: pip3 install {pkg} --break-system-packages")
                    self.tts_engine = None
                except Exception as tts_err:
                    print(f"⚠ Warning: Could not initialize TTS: {tts_err}")
                    self.tts_engine = None
            else:
                print("  Text-to-speech: DISABLED (set ENABLE_TTS = true to enable)")
            
            # (SDR init moved above AIOC init to avoid Pa_Initialize fork crash)

            # Initialize Remote Audio Link
            remote_role = getattr(self.config, 'REMOTE_AUDIO_ROLE', 'disabled').lower().strip("'\"")
            if remote_role == 'server':
                try:
                    host = self.config.REMOTE_AUDIO_HOST
                    if not host:
                        print("⚠ Warning: REMOTE_AUDIO_HOST not set — server needs a destination IP")
                    else:
                        self.remote_audio_server = RemoteAudioServer(self.config)
                        self.remote_audio_server.start()
                except Exception as e:
                    print(f"⚠ Warning: Could not start remote audio server: {e}")
                    self.remote_audio_server = None
            elif remote_role == 'client':
                try:
                    bind_host = self.config.REMOTE_AUDIO_HOST or '0.0.0.0'
                    port = self.config.REMOTE_AUDIO_PORT
                    print(f"Initializing remote audio client (listening on {bind_host}:{port})...")
                    self.remote_audio_source = RemoteAudioSource(self.config, self)
                    if self.remote_audio_source.setup_audio():
                        self.remote_audio_source.enabled = True
                        self.remote_audio_source.duck = self.config.REMOTE_AUDIO_DUCK
                        self.remote_audio_source.sdr_priority = int(self.config.REMOTE_AUDIO_PRIORITY)
                        if self._source_on_listen_bus('monitor') or not self.bus_manager:
                            self.mixer.add_source(self.remote_audio_source, bus_priority=int(self.config.REMOTE_AUDIO_PRIORITY) + 10, duckable=self.config.REMOTE_AUDIO_DUCK)
                            print(f"✓ Remote audio source (SDRSV) added to mixer")
                        else:
                            print(f"✓ Remote audio source initialized (routed via bus manager)")
                        print(f"  Priority: {self.config.REMOTE_AUDIO_PRIORITY}")
                        print(f"  Press 'c' to mute/unmute remote audio")
                    else:
                        print("⚠ Warning: Could not initialize remote audio source")
                        self.remote_audio_source = None
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize remote audio client: {e}")
                    self.remote_audio_source = None

            # Initialize announcement input (port 9601) if enabled
            if getattr(self.config, 'ENABLE_ANNOUNCE_INPUT', False):
                try:
                    bind_host = self.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
                    port = self.config.ANNOUNCE_INPUT_PORT
                    print(f"Initializing announcement input (listening on {bind_host}:{port})...")
                    self.announce_input_source = NetworkAnnouncementSource(self.config, self)
                    if self.announce_input_source.setup_audio():
                        if self._source_on_listen_bus('announce'):
                            self.mixer.add_source(self.announce_input_source, bus_priority=0, duckable=False, deterministic=True)
                            print(f"✓ Announcement input (ANNIN) added to mixer (listen bus)")
                        else:
                            print(f"✓ Announcement input initialized (routed via bus manager)")
                        if not self.aioc_available:
                            print("  ⚠ No AIOC — PTT will not activate (audio discarded)")
                    else:
                        print("⚠ Warning: Could not initialize announcement input")
                        self.announce_input_source = None
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize announcement input: {e}")
                    self.announce_input_source = None

            # Initialize web microphone source (browser mic → radio TX)
            if getattr(self.config, 'ENABLE_WEB_MIC', True):
                try:
                    self.web_mic_source = WebMicSource(self.config, self)
                    if self.web_mic_source.setup_audio():
                        if self._source_on_listen_bus('webmic'):
                            self.mixer.add_source(self.web_mic_source, bus_priority=0, duckable=False, deterministic=True)
                            print("✓ Web microphone source (WEBMIC) added to mixer (listen bus)")
                        else:
                            print("✓ Web microphone source initialized (routed via bus manager)")
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize web mic source: {e}")
                    self.web_mic_source = None

            # Initialize web monitor source (browser mic → mixer, no PTT)
            if getattr(self.config, 'ENABLE_WEB_MONITOR', True):
                try:
                    self.web_monitor_source = WebMonitorSource(self.config, self)
                    if self.web_monitor_source.setup_audio():
                        if self._source_on_listen_bus('monitor'):
                            self.mixer.add_source(self.web_monitor_source, bus_priority=5, duckable=False)
                            print("✓ Web monitor source (MONITOR) added to mixer (listen bus)")
                        else:
                            print("✓ Web monitor source initialized (routed via bus manager)")
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize web monitor source: {e}")
                    self.web_monitor_source = None

            # Relay controllers now owned by TH9800Plugin
            if self.th9800_plugin:
                self.relay_radio = self.th9800_plugin._relay_radio
                self.relay_ptt = self.th9800_plugin._relay_ptt
                self.relay_charger = self.th9800_plugin._relay_charger

            # CAT client now owned by TH9800Plugin (backward compat alias set above)

            # Initialize D75 (plugin)
            self.d75_plugin = None
            if getattr(self.config, 'ENABLE_D75', False):
                try:
                    from d75_plugin import D75Plugin
                    print("Initializing D75 plugin...")
                    self.d75_plugin = D75Plugin()
                    if self.d75_plugin.setup(self.config):
                        if self._source_on_listen_bus('d75'):
                            self.mixer.add_source(self.d75_plugin, bus_priority=int(getattr(self.config, 'D75_AUDIO_PRIORITY', 2)) + 10, duckable=getattr(self.config, 'D75_AUDIO_DUCK', True))
                            print("✓ D75 plugin added to mixer (listen bus)")
                        else:
                            print("✓ D75 plugin initialized (routed via bus manager)")
                    else:
                        print("⚠ Warning: D75 plugin setup failed")
                        self.d75_plugin = None
                except Exception as e:
                    print(f"⚠ D75 plugin error: {e}")
                    import traceback; traceback.print_exc()
                    self.d75_plugin = None
            if self.d75_plugin and self.d75_plugin._processor:
                self.d75_processor = self.d75_plugin._processor

            # Initialize KV4P HT Radio (plugin)
            self.kv4p_plugin = None
            if getattr(self.config, 'ENABLE_KV4P', False):
                try:
                    from kv4p_plugin import KV4PPlugin
                    print(f"Initializing KV4P plugin...")
                    self.kv4p_plugin = KV4PPlugin()
                    if self.kv4p_plugin.setup(self.config):
                        # NOT added to primary ListenBus — routed via BusManager
                        print("✓ KV4P plugin initialized (routed via bus manager)")
                    else:
                        print("⚠ Warning: KV4P plugin setup failed")
                        self.kv4p_plugin = None
                except Exception as e:
                    print(f"⚠ KV4P plugin error: {e}")
                    import traceback; traceback.print_exc()
                    self.kv4p_plugin = None
            if self.kv4p_plugin and self.kv4p_plugin._processor:
                self.kv4p_processor = self.kv4p_plugin._processor

            # Initialize Gateway Link (duplex audio + command protocol)
            if getattr(self.config, 'ENABLE_GATEWAY_LINK', False):
                try:
                    from gateway_link import GatewayLinkServer
                    link_port = int(getattr(self.config, 'LINK_PORT', 9700))
                    print(f"Initializing Gateway Link server (port {link_port})...")
                    self._load_link_settings()

                    def _link_on_register(info):
                        """Called when an endpoint registers — create its audio source."""
                        name = info.get('name', '')
                        if not name:
                            return None
                        src = LinkAudioSource(self.config, self, endpoint_name=name)
                        src.setup_audio()
                        src.enabled = True
                        # Restore saved settings
                        saved = self.link_endpoint_settings.get(name, {})
                        src.muted = saved.get('rx_muted', False)
                        src.server_connected = True
                        self.mixer.add_source(src, bus_priority=int(getattr(self.config, 'LINK_AUDIO_PRIORITY', 3)) + 10, duckable=getattr(self.config, 'LINK_AUDIO_DUCK', False))
                        self.link_endpoints[name] = src
                        self._link_ptt_active[name] = False
                        self._link_last_status[name] = {}
                        self._link_tx_levels[name] = 0
                        print(f"  [Link] Endpoint registered: {name} ({info.get('plugin', '?')})")
                        return src  # server stores src.push_audio as audio callback

                    def _link_on_disconnect(name):
                        """Called when an endpoint disconnects — remove its audio source."""
                        src = self.link_endpoints.pop(name, None)
                        if src:
                            src.server_connected = False
                            self.mixer.remove_source(src.name)
                        self._link_ptt_active.pop(name, None)
                        self._link_last_status.pop(name, None)
                        self._link_tx_levels.pop(name, None)
                        print(f"  [Link] Endpoint disconnected: {name}")

                    def _link_on_ack(name, ack):
                        """Called when an endpoint sends an ACK."""
                        cmd = ack.get('cmd', '')
                        result = ack.get('result', {})
                        if cmd == 'ptt' and isinstance(result, dict):
                            self._link_ptt_active[name] = result.get('ptt', False)
                        elif cmd == 'status' and isinstance(result, dict):
                            self._link_last_status[name] = result.get('status', result)
                        elif cmd in ('rx_gain', 'tx_gain') and isinstance(result, dict):
                            if name not in self._link_last_status:
                                self._link_last_status[name] = {}
                            for k in ('rx_gain_db', 'tx_gain_db'):
                                if k in result:
                                    self._link_last_status[name][k] = result[k]

                    self.link_server = GatewayLinkServer(
                        port=link_port,
                        on_register=_link_on_register,
                        on_disconnect=_link_on_disconnect,
                        on_ack=_link_on_ack,
                    )
                    self.link_server.start()
                    print(f"  Gateway Link listening on port {link_port}")
                except Exception as e:
                    print(f"  Gateway Link error: {e}")
                    import traceback; traceback.print_exc()
                    self.link_server = None
                    self.link_audio_source = None

            # Initialize Mumble Server instances (local mumble-server/murmurd)
            if getattr(self.config, 'ENABLE_MUMBLE_SERVER_1', False):
                try:
                    print("Initializing Mumble Server 1...")
                    self.mumble_server_1 = MumbleServerManager(1, self.config)
                    self.mumble_server_1.start()
                    state, port = self.mumble_server_1.get_status()
                    if state == MumbleServerManager.STATE_RUNNING:
                        print(f"  Mumble Server 1: running on port {port}")
                    elif state == MumbleServerManager.STATE_CONFIGURED:
                        print(f"  Mumble Server 1: configured on port {port} (autostart=false)")
                    elif state == MumbleServerManager.STATE_ERROR:
                        print(f"  Mumble Server 1: ERROR — {self.mumble_server_1.error_msg}")
                except Exception as e:
                    print(f"  Warning: Mumble Server 1 init failed: {e}")
                    if self.mumble_server_1:
                        self.mumble_server_1.state = MumbleServerManager.STATE_ERROR
                        self.mumble_server_1.error_msg = str(e)

            if getattr(self.config, 'ENABLE_MUMBLE_SERVER_2', False):
                try:
                    print("Initializing Mumble Server 2...")
                    self.mumble_server_2 = MumbleServerManager(2, self.config)
                    self.mumble_server_2.start()
                    state, port = self.mumble_server_2.get_status()
                    if state == MumbleServerManager.STATE_RUNNING:
                        print(f"  Mumble Server 2: running on port {port}")
                    elif state == MumbleServerManager.STATE_CONFIGURED:
                        print(f"  Mumble Server 2: configured on port {port} (autostart=false)")
                    elif state == MumbleServerManager.STATE_ERROR:
                        print(f"  Mumble Server 2: ERROR — {self.mumble_server_2.error_msg}")
                except Exception as e:
                    print(f"  Warning: Mumble Server 2 init failed: {e}")
                    if self.mumble_server_2:
                        self.mumble_server_2.state = MumbleServerManager.STATE_ERROR
                        self.mumble_server_2.error_msg = str(e)

            # PTT validation now handled by TH9800Plugin

            # Initialize Smart Announcements (AI-powered)
            if getattr(self.config, 'ENABLE_SMART_ANNOUNCE', False):
                try:
                    self.smart_announce = SmartAnnouncementManager(self)
                    self.smart_announce.start()
                except Exception as e:
                    print(f"  [SmartAnnounce] Init error: {e}")

            # Initialize web configuration UI
            if getattr(self.config, 'ENABLE_WEB_CONFIG', False):
                try:
                    self.web_config_server = WebConfigServer(self.config, gateway=self)
                    self.web_config_server.start()
                except Exception as e:
                    print(f"  [WebConfig] Init error: {e}")

            # Initialize DDNS updater
            if getattr(self.config, 'ENABLE_DDNS', False):
                try:
                    self.ddns_updater = DDNSUpdater(self.config)
                    self.ddns_updater.start()
                except Exception as e:
                    print(f"  [DDNS] Init error: {e}")

            # Initialize Cloudflare Tunnel
            if getattr(self.config, 'ENABLE_CLOUDFLARE_TUNNEL', False):
                try:
                    self.cloudflare_tunnel = CloudflareTunnel(self.config)
                    self.cloudflare_tunnel.start()
                except Exception as e:
                    print(f"  [Tunnel] Init error: {e}")

            # Initialize Email notifier
            if getattr(self.config, 'ENABLE_EMAIL', False):
                try:
                    self.email_notifier = EmailNotifier(self.config, self)
                    if self.email_notifier.is_configured():
                        print(f"  [Email] Notifier ready ({self.email_notifier._recipient})")
                        if getattr(self.config, 'EMAIL_ON_STARTUP', True):
                            self.email_notifier.send_startup_delayed()
                    else:
                        print(f"  [Email] Missing credentials — skipping")
                        self.email_notifier = None
                except Exception as e:
                    print(f"  [Email] Init error: {e}")

            # Initialize EchoLink source if enabled (Phase 3B)
            if self.config.ENABLE_ECHOLINK:
                try:
                    print("Initializing EchoLink integration...")
                    self.echolink_source = EchoLinkSource(self.config, self)
                    if self.echolink_source.connected:
                        self.mixer.add_source(self.echolink_source, bus_priority=2, duckable=False)
                        print("✓ EchoLink source added to mixer")
                        print("  Audio routing:")
                        if self.config.ECHOLINK_TO_MUMBLE:
                            print("    EchoLink → Mumble: ON")
                        if self.config.ECHOLINK_TO_RADIO:
                            print("    EchoLink → Radio TX: ON")
                        if self.config.RADIO_TO_ECHOLINK:
                            print("    Radio RX → EchoLink: ON")
                        if self.config.MUMBLE_TO_ECHOLINK:
                            print("    Mumble → EchoLink: ON")
                    else:
                        print("  ✗ EchoLink IPC not available")
                        print("    Make sure TheLinkBox is running")
                        self.echolink_source = None
                except Exception as echolink_err:
                    print(f"⚠ Warning: Could not initialize EchoLink: {echolink_err}")
                    self.echolink_source = None
            else:
                self.echolink_source = None
            
            # Initialize Icecast streaming if enabled (Phase 3A)
            if self.config.ENABLE_STREAM_OUTPUT:
                try:
                    print("Connecting to Icecast server...")
                    self.stream_output = StreamOutputSource(self.config, self)
                    if self.stream_output.connected:
                        print("✓ Icecast streaming active")
                        print(f"  Listen at: http://{self.config.STREAM_SERVER}:{self.config.STREAM_PORT}{self.config.STREAM_MOUNT}")
                    else:
                        print("  ✗ Icecast connection failed")
                        self.stream_output = None
                except Exception as stream_err:
                    print(f"⚠ Warning: Could not initialize streaming: {stream_err}")
                    self.stream_output = None
            else:
                self.stream_output = None

            # DarkIce no longer needed — streaming handled directly by StreamOutputSource

            # Serial connect — must happen before setup_radio so commands reach the radio
            if self.cat_client:
                print("Connecting TH-9800 serial...")
                try:
                    with self.cat_client._sock_lock:
                        self.cat_client._sock.sendall(b"!serial disconnect\n")
                        self.cat_client._recv_line(timeout=3.0)
                    time.sleep(2)
                    with self.cat_client._sock_lock:
                        self.cat_client._sock.sendall(b"!serial connect\n")
                        self.cat_client._last_activity = time.monotonic()
                        connect_resp = self.cat_client._recv_line(timeout=10.0)
                    if connect_resp and 'serial connected' in connect_resp:
                        self.cat_client._serial_connected = True
                        print(f"  Serial connected: {connect_resp}")
                        # Set RTS to USB Controlled so dashboard shows correct state
                        try:
                            self.cat_client.set_rts(True)
                        except Exception:
                            pass
                    else:
                        print(f"  Serial connect failed: {connect_resp}")
                except Exception as e:
                    print(f"  Serial connect error: {e}")

            # CAT startup commands — run after serial is connected so commands reach the radio
            if self.cat_client and self.config.CAT_STARTUP_COMMANDS:
                print("Sending CAT startup commands...")
                _cat_ref = self.cat_client
                _prev_handler = signal.getsignal(signal.SIGINT)
                def _cat_sigint(sig, frame):
                    _cat_ref._stop = True
                signal.signal(signal.SIGINT, _cat_sigint)
                try:
                    self.cat_client.setup_radio(self.config)
                except KeyboardInterrupt:
                    self.cat_client._stop = True
                finally:
                    signal.signal(signal.SIGINT, _prev_handler)
                if self.cat_client._stop:
                    print("\n  CAT setup interrupted")
            elif self.cat_client:
                print("  CAT startup commands disabled (CAT_STARTUP_COMMANDS = false)")

            return True
            
        except Exception as e:
            error_msg = str(e)
            print(f"✗ Could not initialize audio: {e}")
            
            # Check if this is the "Invalid output device" error that requires USB reset
            if "Invalid output device" in error_msg or "-9996" in error_msg:
                print("\n⚠ Detected USB device initialization error")
                print("  This typically requires unplugging and replugging the AIOC")
                print("  Attempting automatic USB reset...\n")
                
                if self.reset_usb_device():
                    print("\n  ✓ USB reset successful, retrying audio initialization...\n")
                    time.sleep(2)
                    
                    # Retry audio initialization
                    try:
                        input_idx, output_idx = self.find_aioc_audio_device()
                        
                        if input_idx is not None and output_idx is not None:
                            self.output_stream = self.pyaudio_instance.open(
                                format=audio_format,
                                channels=self.config.AUDIO_CHANNELS,
                                rate=self.config.AUDIO_RATE,
                                output=True,
                                output_device_index=output_idx,
                                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4  # Larger buffer for smoother output
                            )
                            
                            # Initialize radio source first to get callback
                            try:
                                self.radio_source = AIOCRadioSource(self.config, self)
                                self.mixer.add_source(self.radio_source, bus_priority=1, duckable=False)
                                if self.config.VERBOSE_LOGGING:
                                    print("✓ Radio audio source added to mixer")
                            except Exception as source_err:
                                print(f"⚠ Warning: Could not initialize radio source: {source_err}")
                                self.radio_source = None

                            aioc_callback = getattr(self.radio_source, '_audio_callback', None) if self.radio_source else None
                            self.input_stream = self.pyaudio_instance.open(
                                format=audio_format,
                                channels=self.config.AUDIO_CHANNELS,
                                rate=self.config.AUDIO_RATE,
                                input=True,
                                input_device_index=input_idx,
                                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                                stream_callback=aioc_callback
                            )

                            print("✓ Audio initialized successfully after USB reset")
                            
                            # Initialize file playback source if enabled
                            if self.config.ENABLE_PLAYBACK:
                                try:
                                    self.playback_source = FilePlaybackSource(self.config, self)
                                    # NOT added to primary ListenBus — routed via BusManager
                                    print("✓ File playback source initialized (routed via bus manager)")
                                    # File mapping will be displayed later
                                    
                                except Exception as playback_err:
                                    print(f"⚠ Warning: Could not initialize playback source: {playback_err}")
                                    self.playback_source = None
                            else:
                                self.playback_source = None
                            
                            return True
                    except Exception as retry_error:
                        print(f"✗ Retry failed: {retry_error}")
                        print("\nPlease manually unplug and replug the AIOC device, then restart")
                else:
                    print("\n✗ Automatic USB reset failed")
                    print("Please manually unplug and replug the AIOC device, then restart")
            
            return False
    
    def setup_mumble(self):
        """Initialize Mumble connection"""

        if self.secondary_mode:
            print()
            print("=" * 60)
            print("  SECONDARY MODE — this machine is not the active gateway")
            print("  Reason: Broadcastify feed already live on another server")
            print("  Mumble: DISABLED (username would conflict)")
            print("  DarkIce: DISABLED (mountpoint already occupied)")
            print("  Audio bridge (FFmpeg/loopback) still running.")
            print("=" * 60)
            return True

        # Create MumbleSource for routing system
        from audio_sources import MumbleSource
        self.mumble_source = MumbleSource(self.config, gateway=self)
        print(f"\nConnecting to Mumble: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}...")

        try:
            # Create Mumble client
            print(f"  Creating Mumble client...")
            self.mumble = Mumble(
                self.config.MUMBLE_SERVER, 
                self.config.MUMBLE_USERNAME,
                port=self.config.MUMBLE_PORT,
                password=self.config.MUMBLE_PASSWORD if self.config.MUMBLE_PASSWORD else '',
                reconnect=False,  # pymumble reconnect causes ghost cycling on local servers
                stereo=self.config.MUMBLE_STEREO,
                debug=self.config.MUMBLE_DEBUG
            )
            
            # Set loop rate for low latency
            self.mumble.set_loop_rate(self.config.MUMBLE_LOOP_RATE)
            
            # Set up callback for received audio
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self.sound_received_handler)
            
            # Set up callback for text messages
            if self.config.ENABLE_TEXT_COMMANDS:
                try:
                    self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self.on_text_message)
                    print("✓ Text message callback registered")
                    print("  Send text commands in Mumble chat (e.g., !status, !help)")
                except Exception as callback_err:
                    print(f"⚠ Text callback registration failed: {callback_err}")
            else:
                print("  Text commands: DISABLED (set ENABLE_TEXT_COMMANDS = true to enable)")
            
            # Enable receiving sound
            self.mumble.set_receive_sound(True)
            
            # Connect
            print(f"  Starting Mumble connection...")
            self.mumble.start()
            
            print(f"  Waiting for Mumble to be ready...")
            self.mumble.is_ready()
            
            print(f"✓ Connected as '{self.config.MUMBLE_USERNAME}'")
            
            # Wait for codec to initialize
            print("  Waiting for audio codec to initialize...")
            max_wait = 5  # seconds
            wait_start = time.time()
            while time.time() - wait_start < max_wait:
                if hasattr(self.mumble.sound_output, 'encoder_framesize') and self.mumble.sound_output.encoder_framesize is not None:
                    print(f"  ✓ Audio codec ready (framesize: {self.mumble.sound_output.encoder_framesize})")
                    break
                time.sleep(0.1)
            else:
                print("  ⚠ Audio codec not initialized after 5s")
                print("    Audio may not work until codec is ready")
                print("    This usually resolves itself within 10-30 seconds")

            # Apply audio quality settings now that the codec is ready.
            # set_bandwidth() was never called before — the library default is 50kbps.
            # complexity=10: max Opus quality (marginal CPU cost on Pi)
            # signal=3001: OPUS_SIGNAL_VOICE — tunes psychoacoustic model for speech
            try:
                self.mumble.set_bandwidth(self.config.MUMBLE_BITRATE)
                enc = getattr(self.mumble.sound_output, 'encoder', None)
                if enc is not None:
                    enc.vbr = 1 if self.config.MUMBLE_VBR else 0
                    enc.complexity = 10
                    enc.signal = 3001  # OPUS_SIGNAL_VOICE
                    print(f"  ✓ Opus encoder: {self.config.MUMBLE_BITRATE//1000}kbps, "
                          f"VBR={'on' if self.config.MUMBLE_VBR else 'off'}, "
                          f"complexity=10, signal=voice")
                else:
                    print(f"  ✓ Mumble bandwidth set to {self.config.MUMBLE_BITRATE//1000}kbps "
                          f"(VBR will apply when codec negotiates)")
            except Exception as qe:
                print(f"  ⚠ Could not apply audio quality settings: {qe}")

            # Join channel if specified
            if self.config.MUMBLE_CHANNEL:
                try:
                    print(f"  Joining channel: {self.config.MUMBLE_CHANNEL}")
                    channel = self.mumble.channels.find_by_name(self.config.MUMBLE_CHANNEL)
                    if channel:
                        channel.move_in()
                        print(f"  ✓ Joined channel: {self.config.MUMBLE_CHANNEL}")
                    else:
                        print(f"  ⚠ Channel '{self.config.MUMBLE_CHANNEL}' not found")
                        print(f"    Staying in root channel")
                except Exception as ch_err:
                    print(f"  ✗ Could not join channel: {ch_err}")
            
            if self.config.VERBOSE_LOGGING:
                print(f"  Loop rate: {self.config.MUMBLE_LOOP_RATE}s ({1/self.config.MUMBLE_LOOP_RATE:.0f} Hz)")
            
            return True
            
        except Exception as e:
            if 'already in use' in str(e).lower() or 'username already' in str(e).lower():
                self.secondary_mode = True
                print()
                print("=" * 60)
                print("  SECONDARY MODE — this machine is not the active gateway")
                print(f"  Reason: Mumble username '{self.config.MUMBLE_USERNAME}' already connected")
                print("  Mumble: DISABLED (username conflict)")
                print("  Hint: DarkIce may also fail if the Broadcastify feed is already live.")
                print("=" * 60)
                return True
            print(f"\n✗ MUMBLE CONNECTION FAILED: {e}")
            print(f"\n  Configuration:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            print(f"    Username: {self.config.MUMBLE_USERNAME}")
            print(f"\n  Please check:")
            print(f"  1. Is the Mumble server running?")
            print(f"  2. Is the IP address correct in gateway_config.txt?")
            print(f"  3. Is the port correct? (default: 64738)")
            print(f"  4. Can you connect with the official Mumble client?")
            print(f"\n  Test with Mumble client first:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            return False
    
    # gTTS voice map: number → (lang, tld, description)
    # gTTS voices (Google Translate, robotic but reliable)
    TTS_VOICES = {
        1: ('en', 'com',    'US English'),
        2: ('en', 'co.uk',  'British English'),
        3: ('en', 'com.au', 'Australian English'),
        4: ('en', 'co.in',  'Indian English'),
        5: ('en', 'co.za',  'South African English'),
        6: ('en', 'ca',     'Canadian English'),
        7: ('en', 'ie',     'Irish English'),
        8: ('fr', 'fr',     'French'),
        9: ('de', 'de',     'German'),
    }

    # Edge TTS voices (Microsoft Neural, natural sounding)
    EDGE_TTS_VOICES = {
        1: ('en-US-AndrewNeural',    'US English (Andrew)'),
        2: ('en-GB-RyanNeural',      'British English (Ryan)'),
        3: ('en-AU-WilliamMultilingualNeural', 'Australian English (William)'),
        4: ('en-IN-PrabhatNeural',   'Indian English (Prabhat)'),
        5: ('en-US-GuyNeural',       'US English (Guy)'),
        6: ('en-CA-LiamNeural',      'Canadian English (Liam)'),
        7: ('en-IE-ConnorNeural',    'Irish English (Connor)'),
        8: ('en-US-AvaNeural',       'US English (Ava)'),
        9: ('en-US-EmmaNeural',      'US English (Emma)'),
    }

    def speak_text(self, text, voice=None):
        """
        Generate TTS audio from text and play it on radio

        Args:
            text: Text to convert to speech
            voice: Optional voice number (1-9), defaults to TTS_DEFAULT_VOICE config

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.tts_engine:
            self.notify("TTS not available (install edge-tts or gtts)")
            return False

        if not self.playback_source:
            self.notify("TTS failed: playback source not available")
            return False

        try:
            import tempfile
            import os

            if self.config.VERBOSE_LOGGING:
                print(f"\n[TTS] Generating speech: {text[:50]}...")

            # Create temporary file
            temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
            temp_path = temp_file.name
            temp_file.close()

            voice_num = voice or int(getattr(self.config, 'TTS_DEFAULT_VOICE', 1))

            if self._tts_backend == 'edge':
                # Edge TTS — Microsoft Neural voices (natural sounding)
                edge_voice, voice_desc = self.EDGE_TTS_VOICES.get(voice_num, self.EDGE_TTS_VOICES[1])
                if self.config.VERBOSE_LOGGING:
                    print(f"[TTS] Calling Edge TTS (voice {voice_num}: {voice_desc})...")
                try:
                    import asyncio
                    communicate = self.tts_engine.Communicate(text, edge_voice)
                    asyncio.run(communicate.save(temp_path))
                    if self.config.VERBOSE_LOGGING:
                        print(f"[TTS] ✓ Audio file saved")
                except Exception as tts_error:
                    print(f"[TTS] ✗ Edge TTS generation failed: {tts_error}")
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
                    return False
            else:
                # gTTS — Google Translate voices (robotic but reliable)
                lang, tld, voice_desc = self.TTS_VOICES.get(voice_num, self.TTS_VOICES[1])
                if self.config.VERBOSE_LOGGING:
                    print(f"[TTS] Calling gTTS (voice {voice_num}: {voice_desc})...")
                try:
                    tts = self.tts_engine(text, lang=lang, tld=tld, slow=False)
                    if self.config.VERBOSE_LOGGING:
                        print(f"[TTS] Saving to {temp_path}...")
                    tts.save(temp_path)
                    if self.config.VERBOSE_LOGGING:
                        print(f"[TTS] ✓ Audio file saved")
                except Exception as tts_error:
                    print(f"[TTS] ✗ gTTS generation failed: {tts_error}")
                    print(f"[TTS] Check internet connection (gTTS requires internet)")
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
                    return False

            # Apply speed adjustment if configured
            tts_speed = float(getattr(self.config, 'TTS_SPEED', 1.0))
            if tts_speed != 1.0 and 0.5 <= tts_speed <= 3.0:
                try:
                    import subprocess as sp
                    speed_path = temp_path + '.speed.mp3'
                    # ffmpeg atempo range is 0.5-2.0; chain filters for values outside
                    filters = []
                    remaining = tts_speed
                    while remaining > 2.0:
                        filters.append('atempo=2.0')
                        remaining /= 2.0
                    filters.append(f'atempo={remaining:.4f}')
                    sp.run(['ffmpeg', '-y', '-i', temp_path, '-filter:a',
                            ','.join(filters), speed_path],
                           capture_output=True, timeout=30)
                    if os.path.exists(speed_path) and os.path.getsize(speed_path) > 500:
                        os.replace(speed_path, temp_path)
                        if self.config.VERBOSE_LOGGING:
                            print(f"[TTS] Speed adjusted to {tts_speed}x")
                    else:
                        print(f"[TTS] ⚠ Speed adjustment failed, using original")
                        try:
                            os.unlink(speed_path)
                        except Exception:
                            pass
                except Exception as speed_err:
                    print(f"[TTS] ⚠ Speed adjustment error: {speed_err}")
                    try:
                        os.unlink(speed_path)
                    except Exception:
                        pass

            # Verify file exists and has valid content
            if not os.path.exists(temp_path):
                print(f"[TTS] ✗ File not created!")
                return False
            
            size = os.path.getsize(temp_path)
            if self.config.VERBOSE_LOGGING:
                print(f"[TTS] File size: {size} bytes")
            
            # Validate it's actually an MP3 file, not an HTML error page
            # MP3 files start with ID3 tag or MPEG frame sync
            try:
                with open(temp_path, 'rb') as f:
                    header = f.read(10)
                    
                    # Check for ID3 tag (ID3v2)
                    is_mp3 = header.startswith(b'ID3')
                    
                    # Check for MPEG frame sync (0xFF 0xFB or 0xFF 0xF3)
                    if not is_mp3 and len(header) >= 2:
                        is_mp3 = (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)
                    
                    # Check if it's HTML (error page)
                    is_html = header.startswith(b'<!DOCTYPE') or header.startswith(b'<html')
                    
                    if is_html:
                        print(f"[TTS] ✗ gTTS returned HTML error page, not MP3")
                        print(f"[TTS] This usually means:")
                        print(f"  - Rate limiting from Google")
                        print(f"  - Network/firewall blocking")
                        print(f"  - Invalid characters in text")
                        # Read first 200 chars to show error
                        f.seek(0)
                        error_preview = f.read(200).decode('utf-8', errors='ignore')
                        print(f"[TTS] Error preview: {error_preview[:100]}")
                        os.unlink(temp_path)
                        return False
                    
                    if not is_mp3:
                        print(f"[TTS] ✗ File doesn't appear to be valid MP3")
                        print(f"[TTS] Header: {header.hex()}")
                        os.unlink(temp_path)
                        return False
                    
                    if self.config.VERBOSE_LOGGING:
                        print(f"[TTS] ✓ Validated MP3 file format")
                        
            except Exception as val_err:
                print(f"[TTS] ✗ Could not validate file: {val_err}")
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                return False
            
            # File is valid MP3
            if size < 1000:
                # Suspiciously small - probably an error
                print(f"[TTS] ✗ File too small ({size} bytes) - likely an error")
                os.unlink(temp_path)
                return False
            
            # Skip padding for now - it was causing corruption
            # The MP3 file is ready to play as-is
            if self.config.VERBOSE_LOGGING:
                print(f"[TTS] MP3 file ready for playback")
            
            if self.config.VERBOSE_LOGGING:
                print(f"[TTS] Queueing for playback...")
            
            # Auto-switch RTS to Radio Controlled for TX — RTS relay must route
            # mic wiring through front panel for AIOC PTT to work.
            # No CAT commands while Radio Controlled (serial disconnected from USB).
            # Software PTT uses !ptt directly and doesn't need RTS switching.
            _ptt_method = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
            if _ptt_method != 'software':
                _cat = getattr(self, 'cat_client', None)
                if _cat and not getattr(self, '_playback_rts_saved', None):
                    self._playback_rts_saved = _cat.get_rts()
                    if self._playback_rts_saved is None or self._playback_rts_saved is True:
                        _cat._pause_drain()
                        try:
                            _cat.set_rts(False)  # Radio Controlled
                            import time as _time
                            _time.sleep(0.3)
                            _cat._drain(0.5)
                        finally:
                            _cat._drain_paused = False

            # Queue for playback (will go to radio TX)
            if self.playback_source:
                if self.config.VERBOSE_LOGGING:
                    print(f"[TTS] Playback source exists, queueing file...")

                # Temporarily boost playback volume for TTS
                # Volume will be reset to 1.0 when file finishes playing
                original_volume = self.playback_source.volume
                self.playback_source.volume = self.config.TTS_VOLUME
                if self.config.VERBOSE_LOGGING:
                    print(f"[TTS] Boosting volume from {original_volume}x to {self.config.TTS_VOLUME}x for TTS playback")
                    print(f"[TTS] Volume will auto-reset to 1.0x when TTS finishes")

                result = self.playback_source.queue_file(temp_path)

                if self.config.VERBOSE_LOGGING:
                    print(f"[TTS] Queue result: {result}")
                if not result:
                    print(f"[TTS] ✗ Failed to queue file")
                    self.playback_source.volume = original_volume  # Restore on failure
                    return False
            else:
                print(f"[TTS] ✗ No playback source available!")
                return False
            
            return True
            
        except Exception as e:
            print(f"\n[TTS] Error: {e}")
            return False
    
    def send_text_message(self, message):
        """
        Send text message to current Mumble channel
        
        Args:
            message: Text message to send
        """
        try:
            if self.config.VERBOSE_LOGGING:
                print(f"\n[Mumble Text] Attempting to send: {message[:100]}...")
            if self.mumble and hasattr(self.mumble, 'users') and hasattr(self.mumble.users, 'myself'):
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] Mumble object exists, calling send_message...")
                # Try the send_message method (might be the correct one)
                self.mumble.users.myself.send_message(message)
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✓ Message sent successfully")
            else:
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✗ Mumble not ready")
        except AttributeError as ae:
            # Try alternate method
            try:
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] Trying alternate method...")
                self.mumble.my_channel().send_text_message(message)
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✓ Message sent via channel method")
            except Exception as e2:
                print(f"\n[Mumble Text] ✗ Both methods failed: {ae}, {e2}")
        except Exception as e:
            print(f"\n[Mumble Text] ✗ Error sending: {e}")
            import traceback
            traceback.print_exc()
    
    def on_text_message(self, text_message):
        """
        Handle incoming text messages from Mumble users
        
        Supports commands:
            !speak [voice#] <text>  - Generate TTS and broadcast on radio (voices 1-9)
            !play <0-9>    - Play announcement file by slot number
            !files         - List loaded announcement files
            !stop          - Stop playback and clear queue
            !mute          - Mute TX (Mumble → Radio)
            !unmute        - Unmute TX
            !id            - Play station ID (shortcut for !play 0)
            !status        - Show gateway status
            !help          - Show available commands
        """
        try:
            # Debug: Print when text is received (if verbose)
            if self.config.VERBOSE_LOGGING:
                print(f"\n[Mumble Text] Message received from user {text_message.actor}")
            
            # Get sender info
            sender = self.mumble.users[text_message.actor]
            sender_name = sender['name']
            # Mumble sends messages as HTML — strip tags and decode entities
            import re
            from html import unescape
            raw_msg = text_message.message
            message = unescape(re.sub(r'<[^>]+>', '', raw_msg)).strip()
            
            if self.config.VERBOSE_LOGGING:
                print(f"[Mumble Text] {sender_name}: {message}")
            
            # Ignore if not a command
            if not message.startswith('!'):
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] Not a command (doesn't start with !), ignoring")
                return
            
            # Parse command
            parts = message.split(None, 1)  # Split on first space
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            
            # Handle commands
            if command == '!speak':
                if args:
                    # Parse optional voice number: !speak 3 Hello world
                    voice = None
                    speak_text = args
                    speak_parts = args.split(None, 1)
                    if len(speak_parts) == 2 and speak_parts[0].isdigit():
                        v = int(speak_parts[0])
                        if v in self.TTS_VOICES:
                            voice = v
                            speak_text = speak_parts[1]
                    if self.speak_text(speak_text, voice=voice):
                        v_info = f" (voice {voice})" if voice else ""
                        self.send_text_message(f"Speaking{v_info}: {speak_text[:50]}...")
                    else:
                        self.send_text_message("TTS not available")
                else:
                    voices = " | ".join(f"{k}={v[2]}" for k, v in self.TTS_VOICES.items())
                    self.send_text_message(f"Usage: !speak [voice#] <text> — Voices: {voices}")
            
            elif command == '!play':
                if args and args in '0123456789':
                    key = args
                    if self.playback_source:
                        path = self.playback_source.file_status[key]['path']
                        filename = self.playback_source.file_status[key].get('filename', '')
                        if path:
                            self.playback_source.queue_file(path)
                            self.send_text_message(f"Playing: {filename}")
                        else:
                            self.send_text_message(f"No file on key {key}")
                    else:
                        self.send_text_message("Playback not available")
                else:
                    self.send_text_message("Usage: !play <0-9>")
            
            elif command == '!cw':
                if not args:
                    self.send_text_message("Usage: !cw &lt;text&gt;")
                else:
                    pcm = generate_cw_pcm(args, self.config.CW_WPM,
                                          self.config.CW_FREQUENCY, 48000)
                    if self.config.CW_VOLUME != 1.0:
                        pcm = np.clip(pcm.astype(np.float32) * self.config.CW_VOLUME,
                                      -32768, 32767).astype(np.int16)
                    import wave, tempfile
                    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, prefix='cw_')
                    tmp.close()
                    with wave.open(tmp.name, 'wb') as wf:
                        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                        wf.writeframes(pcm.tobytes())
                    if self.playback_source and self.playback_source.queue_file(tmp.name):
                        self.send_text_message(f"CW: {args}")
                    else:
                        self.send_text_message("CW: playback unavailable")
                        os.unlink(tmp.name)

            elif command == '!status':
                import psutil

                s = []
                s.append("━━━ GATEWAY STATUS ━━━")

                # Uptime
                uptime_s = int(time.time() - self.start_time)
                days, rem = divmod(uptime_s, 86400)
                hours, rem = divmod(rem, 3600)
                mins, _ = divmod(rem, 60)
                uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
                s.append(f"Uptime: {uptime_str}")

                # Host — CPU, RAM, Disk
                cpu = psutil.cpu_percent(interval=0.1)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                load1, load5, load15 = os.getloadavg()
                s.append(f"\n📊 HOST:")
                s.append(f"  CPU: {cpu:.0f}%  Load: {load1:.1f} / {load5:.1f} / {load15:.1f}")
                try:
                    temps = psutil.sensors_temperatures()
                    cpu_temp = temps.get('cpu_thermal', temps.get('coretemp', [{}]))[0]
                    s.append(f"  Temp: {cpu_temp.current:.0f}°C")
                except Exception:
                    pass
                s.append(f"  RAM: {mem.used // (1024**2)}M / {mem.total // (1024**2)}M ({mem.percent:.0f}%)")
                s.append(f"  Disk: {disk.used // (1024**3)}G / {disk.total // (1024**3)}G ({disk.percent:.0f}%)")

                # Radio
                mutes = []
                if self.tx_muted: mutes.append("TX")
                if self.rx_muted: mutes.append("RX")
                if self.tx_muted and self.rx_muted: mutes.append("ALL")
                ptt = "TX" if self.ptt_active else "Idle"
                if self.manual_ptt_mode: ptt += " (manual)"
                s.append(f"\n📻 RADIO:")
                s.append(f"  PTT: {ptt}  Muted: {', '.join(mutes) if mutes else 'None'}")
                if self.sdr_rebroadcast:
                    s.append(f"  Rebroadcast: ON")

                # Sources
                sources = []
                if self.radio_source:
                    sources.append(f"AIOC ({'muted' if self.tx_muted else 'active'})")
                if self.sdr_plugin:
                    sources.append(f"SDR1 ({'muted' if self.sdr_muted else 'active'})")
                if self.sdr_plugin:
                    sources.append(f"SDR2 ({'muted' if self.sdr2_muted else 'active'})")
                if self.remote_audio_source:
                    sources.append(f"Remote ({'muted' if self.remote_audio_muted else 'active'})")
                if hasattr(self, 'announce_source') and self.announce_source:
                    ann_muted = getattr(self, 'announce_muted', False)
                    sources.append(f"Announce ({'muted' if ann_muted else 'active'})")
                if sources:
                    s.append(f"  Sources: {', '.join(sources)}")

                # Mumble
                ch = self.config.MUMBLE_CHANNEL if self.config.MUMBLE_CHANNEL else "Root"
                users = len(self.mumble.users) if self.mumble else 0
                s.append(f"\n💬 MUMBLE:")
                s.append(f"  Channel: {ch}  Users: {users}")

                # Processing — compact, per-source
                proc = []
                if self.config.ENABLE_VAD: proc.append("VAD")
                radio_active = self.radio_processor.get_active_list()
                if radio_active:
                    proc.append(f"Radio[{','.join(radio_active)}]")
                sdr_active = self.sdr_processor.get_active_list()
                if sdr_active:
                    proc.append(f"SDR[{','.join(sdr_active)}]")
                if proc:
                    s.append(f"\n🎛️ Processing: {' | '.join(proc)}")

                # Network
                s.append(f"\n🌐 NETWORK:")
                for iface_name, addrs in psutil.net_if_addrs().items():
                    for addr in addrs:
                        if addr.family.name == 'AF_INET' and addr.address != '127.0.0.1':
                            s.append(f"  {iface_name}: {addr.address}")

                s.append("━━━━━━━━━━━━━━━━━━━━━━")
                self.send_text_message("\n".join(s))
            
            elif command == '!files':
                if self.playback_source:
                    lines = ["=== Announcement Files ==="]
                    found = False
                    for key in '0123456789':
                        info = self.playback_source.file_status[key]
                        if info['exists']:
                            label = "Station ID" if key == '0' else f"Slot {key}"
                            playing = " [PLAYING]" if info['playing'] else ""
                            lines.append(f"  {label}: {info['filename']}{playing}")
                            found = True
                    if not found:
                        lines.append("  No files loaded")
                    self.send_text_message("\n".join(lines))
                else:
                    self.send_text_message("Playback not available")

            elif command == '!stop':
                if self.playback_source:
                    self.playback_source.stop_playback()
                    self.send_text_message("Playback stopped")
                else:
                    self.send_text_message("Playback not available")

            elif command == '!restart':
                self.send_text_message("Gateway restarting...")
                self.restart_requested = True
                self.running = False

            elif command == '!mute':
                self.tx_muted = True
                self.send_text_message("TX muted (Mumble → Radio)")

            elif command == '!unmute':
                self.tx_muted = False
                self.send_text_message("TX unmuted")

            elif command == '!id':
                if self.playback_source:
                    info = self.playback_source.file_status['0']
                    if info['path']:
                        self.playback_source.queue_file(info['path'])
                        self.send_text_message(f"Playing station ID: {info['filename']}")
                    else:
                        self.send_text_message("No station ID file on slot 0")
                else:
                    self.send_text_message("Playback not available")

            elif command == '!smart':
                if not self.smart_announce or not self.smart_announce._claude_bin:
                    self.send_text_message("Smart announcements not configured")
                elif args and args.isdigit():
                    entry_id = int(args)
                    if self.smart_announce.trigger(entry_id):
                        self.send_text_message(f"Triggering smart announcement #{entry_id}...")
                    else:
                        self.send_text_message(f"No smart announcement #{entry_id}")
                else:
                    entries = self.smart_announce.get_entries()
                    if entries:
                        lines = [f"#{e['id']}: every {e['interval']}s, voice {e['voice']}, "
                                 f"~{e['target_secs']}s — {e['prompt'][:50]}" for e in entries]
                        self.send_text_message("Smart announcements:\n" + "\n".join(lines)
                                               + "\n\nUsage: !smart <N> to trigger")
                    else:
                        self.send_text_message("No smart announcements configured")

            elif command == '!help':
                help_text = [
                    "=== Gateway Commands ===",
                    "!speak [voice#] <text> - TTS broadcast (voices 1-9)",
                    "!smart [N]    - List or trigger smart announcement",
                    "!cw <text>    - Send Morse code on radio",
                    "!play <0-9>   - Play announcement by slot",
                    "!files        - List loaded announcement files",
                    "!stop         - Stop playback and clear queue",
                    "!mute         - Mute TX (Mumble → Radio)",
                    "!unmute       - Unmute TX",
                    "!id           - Play station ID (slot 0)",
                    "!restart      - Restart the gateway",
                    "!status       - Show gateway status",
                    "!help         - Show this help"
                ]
                self.send_text_message("\n".join(help_text))

            else:
                self.send_text_message(f"Unknown command. Try !help")
        
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"\n[Text Command] Error: {e}")
    
    def audio_transmit_loop(self):
        """Continuously capture audio from sources and send to Mumble via mixer"""
        # Elevate this thread to realtime scheduling so the 50ms tick isn't
        # delayed when the terminal window loses desktop focus.  Only this
        # thread needs it — it feeds both Mumble and the speaker callback.
        try:
            os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(10))
            print("  Audio thread: SCHED_RR (realtime, priority 10)")
        except (PermissionError, OSError):
            try:
                os.nice(-10)
                print("  Audio thread: nice -10")
            except (PermissionError, OSError):
                pass  # best-effort
        if self.config.VERBOSE_LOGGING:
            print("✓ Audio transmit thread started (with mixer)")
        
        consecutive_errors = 0
        max_consecutive_errors = 10

        # 50ms self-clock: the main loop runs at this cadence regardless of
        # whether sources return data.  Sources are non-blocking; this tick
        # replaces the old pacing that was inside AIOCRadioSource.get_audio().
        _TICK = self.config.AUDIO_CHUNK_SIZE / self.config.AUDIO_RATE  # 0.05s
        _next_tick = time.monotonic()
        _prev_tick_time = time.monotonic()
        _trace = self._audio_trace  # local ref for speed
        _out_last_sample = 0  # output-side discontinuity tracking
        _out_disc = 0.0       # output-side sample jump at chunk boundary

        while self.running:
            self._tx_loop_tick += 1
            # ── 50ms self-clock ──────────────────────────────────────────────
            _now = time.monotonic()
            _slept = 0.0
            if _next_tick > _now:
                _slept = _next_tick - _now
                time.sleep(_slept)
            elif _now - _next_tick > _TICK:
                _next_tick = _now  # snap forward after stall
            _next_tick += _TICK
            _tick_start = time.monotonic()
            _tick_dt = (_tick_start - _prev_tick_time) * 1000  # ms since last tick
            _prev_tick_time = _tick_start

            # Trace defaults — overwritten inside the try body as we progress
            _tr_outcome = '?'
            _tr_mumble_ms = 0.0
            _tr_spk_ok = False
            _tr_spk_qd = -1
            _tr_data_rms = 0.0
            _tr_mixer_got = False
            _tr_mixer_ms = 0.0
            _tr_mixer_state = {}
            _tr_sdr_q = -1
            _tr_sdr_sb = -1
            _tr_sdr2_q = -1
            _tr_sdr2_sb = -1
            _tr_aioc_q = -1
            _tr_aioc_sb = -1
            _tr_sdr_prebuf = False
            _tr_sdr2_prebuf = False
            _out_disc = 0.0  # reset per tick
            _tr_rebro = ''  # rebroadcast state: ''=off, 'sig'=sending, 'hold'=PTT hold, 'idle'=on but no signal
            _tr_sv_ms = 0.0   # RemoteAudioServer send_audio cumulative time (ms)
            _tr_sv_sent = 0   # number of send_audio calls this tick
            active_sources = []

            try:
                # ── Apply pending PTT state change ───────────────────────────────
                # The keyboard thread queues PTT changes here instead of calling
                # set_ptt_state() directly.  Applying it now (between audio reads)
                # keeps the HID write off the USB bus while input_stream.read() is
                # blocking, eliminating the USB contention that causes an audio click.
                pending_ptt = self._pending_ptt_state
                if pending_ptt is not None:
                    self._pending_ptt_state = None
                    self.set_ptt_state(pending_ptt)
                    self._ptt_change_time = time.monotonic()  # Tell get_audio to fade in next chunk

                # AIOC stream health now handled by TH9800Plugin.check_watchdog()

                # Safety: clear announcement delay if its timer has expired.
                # This handles the case where stop_playback() is called during
                # the delay window — the PTT branch (which normally clears the
                # flag) never runs when ptt_required is False, so without this
                # check the flag stays True and the next announcement skips its
                # first chunk on load.
                if self.announcement_delay_active and time.time() >= self._announcement_ptt_delay_until:
                    self.announcement_delay_active = False

                # ── Mixer path: runs whenever the mixer exists (SDR-only is valid) ──
                if self.mixer:
                    # Snapshot source state BEFORE mixer call
                    _tr_sdr_q = -1
                    _tr_sdr_sb = -1
                    _tr_sdr_prebuf = False
                    _tr_sdr2_q = -1
                    _tr_sdr2_sb = -1
                    _tr_sdr2_prebuf = False
                    _tr_aioc_q = -1  # TH9800Plugin doesn't use chunk_queue
                    _tr_aioc_sb = -1

                    _tr_mixer_t0 = time.monotonic()
                    _bus_out = self.mixer.tick(self.config.AUDIO_CHUNK_SIZE)
                    data = _bus_out.mixed_audio
                    ptt_required = _bus_out.ptt.get('_ptt_required', False)

                    # Periodic bus state diagnostic (every 10s)
                    if not hasattr(self, '_bus_diag_counter'):
                        self._bus_diag_counter = 0
                    self._bus_diag_counter += 1
                    if self._bus_diag_counter % 200 == 1:
                        _pm_srcs = [s.source.name for s in self.mixer.source_slots]
                        _pm_active = _bus_out.active_sources
                        print(f"  [DIAG] Primary mixer: sources={_pm_srcs} active={_pm_active} data={'YES' if data else 'NO'} ptt={ptt_required}")
                        if self.bus_manager:
                            for _bid, _bus in self.bus_manager._busses.items():
                                _bsrcs = [s.source.name for s in getattr(_bus, 'source_slots', getattr(_bus, '_tx_sources', []))]
                                _bradio = getattr(_bus, '_radio', None)
                                _btxonly = getattr(_bus, '_tx_only', False)
                                print(f"  [DIAG] Bus {_bid}: type={_bus.bus_type} sources={_bsrcs} radio={_bradio.name if _bradio else None} tx_only={_btxonly}")
                    active_sources = _bus_out.active_sources
                    sdr1_was_ducked = 'SDR1' in _bus_out.ducked_sources
                    sdr2_was_ducked = 'SDR2' in _bus_out.ducked_sources
                    sdrsv_was_ducked = 'SDRSV' in _bus_out.ducked_sources
                    rx_audio = _bus_out.status.get('rx_audio')
                    sdr_only_audio = _bus_out.status.get('duckee_only_audio')
                    _tr_mixer_ms = (time.monotonic() - _tr_mixer_t0) * 1000

                    # Store SDR ducked states for status bar display
                    self.sdr_ducked = sdr1_was_ducked
                    self.sdr2_ducked = sdr2_was_ducked
                    self.remote_audio_ducked = sdrsv_was_ducked

                    # Capture mixer internal state for trace
                    if self._trace_recording and hasattr(self.mixer, '_last_trace_state'):
                        _tr_mixer_state = self.mixer._last_trace_state.copy()
                        _tr_mixer_state['rx_m'] = getattr(self, 'rx_muted', False)
                        _tr_mixer_state['tx_m'] = getattr(self, 'tx_muted', False)
                        _tr_mixer_state['sp_m'] = getattr(self, 'speaker_muted', False)
                        _tr_mixer_state['gl_m'] = getattr(self, 'global_muted', False)

                    _tr_mixer_got = data is not None

                    # Track listen bus output level for routing page
                    if data is not None:
                        _mlv = self.calculate_audio_level(data)
                        if _mlv > getattr(self, '_last_mixer_level', 0):
                            self._last_mixer_level = _mlv
                        else:
                            self._last_mixer_level = max(0, int(getattr(self, '_last_mixer_level', 0) * 0.7))
                    else:
                        self._last_mixer_level = max(0, int(getattr(self, '_last_mixer_level', 0) * 0.7))

                    # Drain BusManager PCM/MP3 once per tick (don't call twice!)
                    _bm_pcm = self.bus_manager.drain_pcm() if self.bus_manager else None
                    _bm_mp3 = self.bus_manager.drain_mp3() if self.bus_manager else None

                    # Early sink delivery — before VAD/signal gates so monitoring
                    # sinks always receive audio (SDR scanner feeds etc.)
                    _early_audio = data if data is not None else sdr_only_audio
                    _listen_sinks = self._bus_sinks.get(self._listen_bus_id, set())
                    # Decay sink levels when no audio
                    if _early_audio is None:
                        self.stream_audio_level = max(0, int(self.stream_audio_level * 0.7))
                        self.mumble_tx_level = max(0, int(getattr(self, 'mumble_tx_level', 0) * 0.7))
                    if _early_audio is not None:
                        if 'broadcastify' in _listen_sinks:
                            if self.stream_output and self.stream_output.connected:
                                try:
                                    self.stream_output.send_audio(_early_audio)
                                    self.stream_audio_level = self.calculate_audio_level(_early_audio)
                                except Exception:
                                    pass
                        if 'mumble' in _listen_sinks:
                            if (self.mumble and
                                    hasattr(self.mumble, 'sound_output') and
                                    self.mumble.sound_output is not None and
                                    getattr(self.mumble.sound_output, 'encoder_framesize', None) is not None):
                                try:
                                    # Split into pymumble-sized frames
                                    _ef = self.mumble.sound_output.encoder_framesize
                                    _fb = int(_ef * 48000 * 2)
                                    for _fi in range(0, len(_early_audio), _fb):
                                        _fr = _early_audio[_fi:_fi + _fb]
                                        if len(_fr) == _fb:
                                            self.mumble.sound_output.add_sound(_fr)
                                    # Track Mumble TX level for routing page
                                    _ml = self.calculate_audio_level(_early_audio)
                                    if _ml > getattr(self, 'mumble_tx_level', 0):
                                        self.mumble_tx_level = _ml
                                    else:
                                        self.mumble_tx_level = int(getattr(self, 'mumble_tx_level', 0) * 0.7 + _ml * 0.3)
                                except Exception:
                                    pass

                    if data is None:
                        # No audio from any source — nothing to send.
                        # Feed silence to speaker so its PortAudio buffer stays primed,
                        # but skip the Mumble/remote-audio send path entirely.
                        self.audio_capture_active = False
                        _silence = b'\x00' * (self.config.AUDIO_CHUNK_SIZE * 2)
                        if 'speaker' in self._bus_sinks.get(self._listen_bus_id, set()):
                            self._speaker_enqueue(_silence)
                        # Use already-drained BusManager PCM
                        if _bm_pcm is not None:
                            if not hasattr(self, '_bm_pcm_push_count'):
                                self._bm_pcm_push_count = 0
                            self._bm_pcm_push_count += 1
                            if self._bm_pcm_push_count <= 3 or self._bm_pcm_push_count % 100 == 0:
                                _has_ws = bool(self.web_config_server and self.web_config_server._ws_clients)
                                print(f"  [PCM-drain] #{self._bm_pcm_push_count}: {len(_bm_pcm)}B ws_clients={_has_ws}")
                            if self.web_config_server and self.web_config_server._ws_clients:
                                self.web_config_server.push_ws_audio(_bm_pcm)
                        elif self._bus_stream_flags.get(self._listen_bus_id, {}).get('pcm', False):
                            if self.web_config_server and self.web_config_server._ws_clients:
                                self.web_config_server.push_ws_audio(_silence)
                        _tr_outcome = 'vad_gate'
                        continue
                    else:
                        # Mixer produced audio (from any source: AIOC, SDR, file).
                        # Update health flags so the status monitor doesn't think
                        # audio capture has stopped and trigger restart_audio_input().
                        self.last_audio_capture_time = time.time()
                        self.audio_capture_active = True

                        # Feed audio to automation recorder if active
                        if self.automation_engine and self.automation_engine.recorder.is_recording():
                            self.automation_engine.recorder.feed(data)

                        # Push to WebSocket PCM clients — mix listen bus + other busses.
                        _listen_flags = self._bus_stream_flags.get(self._listen_bus_id, {})
                        _listen_pcm_on = _listen_flags.get('pcm', False)
                        # _bm_pcm already drained above — reuse it
                        if _listen_pcm_on or _bm_pcm is not None:
                            _pcm_out = None
                            if _listen_pcm_on and data is not None:
                                _pcm_out = data
                            if _bm_pcm is not None:
                                if _pcm_out is None:
                                    _pcm_out = _bm_pcm
                                else:
                                    from audio_bus import mix_audio_streams
                                    _pcm_out = mix_audio_streams(_pcm_out, _bm_pcm)
                            if _pcm_out is not None:
                                if self.web_config_server and self.web_config_server._ws_clients:
                                    self.web_config_server.push_ws_audio(_pcm_out)

                    _tr_outcome = 'mix'  # will be updated to sent/no_mumble/etc below

                    # SDR rebroadcast: route SDR-only mix to AIOC radio TX
                    if self.sdr_rebroadcast and not ptt_required and sdr_only_audio is not None:
                        sdr_arr = np.frombuffer(sdr_only_audio, dtype=np.int16).astype(np.float32)
                        sdr_rms = float(np.sqrt(np.mean(sdr_arr * sdr_arr))) if len(sdr_arr) > 0 else 0.0
                        sdr_has_signal = sdr_rms > 100  # ~-50 dBFS threshold

                        if sdr_has_signal:
                            self._rebroadcast_ptt_hold_until = time.monotonic() + self.config.SDR_REBROADCAST_PTT_HOLD
                            self._rebroadcast_sending = True
                            self.last_sound_time = time.time()  # prevent PTT release timer
                        else:
                            self._rebroadcast_sending = False

                        rebroadcast_ptt_needed = time.monotonic() < self._rebroadcast_ptt_hold_until

                        if rebroadcast_ptt_needed:
                            self.last_sound_time = time.time()  # keep PTT release timer at bay during hold

                            if not self._rebroadcast_ptt_active and not self.tx_muted and not self.manual_ptt_mode:
                                self.set_ptt_state(True)
                                self._ptt_change_time = time.monotonic()
                                self._rebroadcast_ptt_active = True
                                # Disable AIOC source so TX feedback doesn't trigger ducking
                                if self.radio_source:
                                    self.radio_source.enabled = False
                                self._trace_events.append((time.monotonic(), 'rebro_ptt', 'on'))

                            pcm = sdr_only_audio if sdr_has_signal else b'\x00' * len(sdr_only_audio)
                            if self.output_stream and not self.tx_muted:
                                if self.config.OUTPUT_VOLUME != 1.0:
                                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                    pcm = np.clip(arr * self.config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
                                try:
                                    self.output_stream.write(pcm, exception_on_overflow=False)
                                except TypeError:
                                    self.output_stream.write(pcm)

                            # Update TX bar — measure after OUTPUT_VOLUME to reflect actual transmitted level
                            tx_level_pcm = pcm if sdr_has_signal else sdr_only_audio
                            current_level = self.calculate_audio_level(tx_level_pcm)
                            if current_level > self.rx_audio_level:
                                self.rx_audio_level = current_level
                            else:
                                self.rx_audio_level = int(self.rx_audio_level * 0.7 + current_level * 0.3)
                            self.last_rx_audio_time = time.time()  # prevent level decay

                            _tr_rebro = 'sig' if sdr_has_signal else 'hold'
                        else:
                            if self._rebroadcast_ptt_active and self.ptt_active:
                                self.set_ptt_state(False)
                                self._ptt_change_time = time.monotonic()
                                self._rebroadcast_ptt_active = False
                                if self.radio_source:
                                    self.radio_source.enabled = True
                                self._trace_events.append((time.monotonic(), 'rebro_ptt', 'off'))
                            self._rebroadcast_sending = False
                            _tr_rebro = 'idle'
                    elif self.sdr_rebroadcast and not ptt_required and sdr_only_audio is None:
                        # No SDR audio this tick — check if hold expired
                        self._rebroadcast_sending = False
                        if time.monotonic() >= self._rebroadcast_ptt_hold_until:
                            if self._rebroadcast_ptt_active and self.ptt_active:
                                self.set_ptt_state(False)
                                self._ptt_change_time = time.monotonic()
                                self._rebroadcast_ptt_active = False
                                if self.radio_source:
                                    self.radio_source.enabled = True
                                self._trace_events.append((time.monotonic(), 'rebro_ptt', 'off'))
                            _tr_rebro = 'idle'
                        else:
                            _tr_rebro = 'hold'

                    # Old PTT path disabled — TX routing handled by bus system.
                    # Sources with ptt_control=True on a listen bus just mix normally.
                    # SoloBus handles PTT keying when sources are on a solo bus.
                    if False and ptt_required:
                        # PTT required (file playback / announcement input)

                        # Trace: measure RMS inside PTT branch (the common
                        # trace point after `continue` never runs for PTT)
                        if self._trace_recording and data:
                            _tr_arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                            _tr_data_rms = float(np.sqrt(np.mean(_tr_arr * _tr_arr))) if len(_tr_arr) > 0 else 0.0

                        # Update last sound time so PTT release timer works
                        self.last_sound_time = time.time()

                        # Calculate audio level for TX bar
                        current_level = self.calculate_audio_level(data)
                        # Smooth the level display (fast attack, slow decay)
                        if current_level > self.rx_audio_level:
                            self.rx_audio_level = current_level
                        else:
                            self.rx_audio_level = int(self.rx_audio_level * 0.7 + current_level * 0.3)

                        # Update last RX audio time to prevent decay during file playback
                        self.last_rx_audio_time = time.time()

                        # Activate PTT if not already active and not muted.
                        if not self.ptt_active and not self.tx_muted and not self.manual_ptt_mode:
                            self.set_ptt_state(True)
                            self._ptt_change_time = time.monotonic()  # arm click suppression in AIOCRadioSource
                            self._announcement_ptt_delay_until = time.time() + self.config.PTT_ANNOUNCEMENT_DELAY
                            self.announcement_delay_active = True

                        # Clear the delay flag once the window has passed.
                        if self.announcement_delay_active and time.time() >= self._announcement_ptt_delay_until:
                            self.announcement_delay_active = False

                        # Send audio to radio output
                        _ptt_wrote = False
                        _tx_radio_cfg = str(getattr(self.config, 'TX_RADIO', 'th9800')).lower()
                        _use_d75_tx = (_tx_radio_cfg == 'd75' and self.d75_plugin)
                        _use_kv4p_tx = (_tx_radio_cfg == 'kv4p' and self.kv4p_plugin)
                        if (_use_d75_tx or _use_kv4p_tx or self.output_stream) and not self.tx_muted:
                            try:
                                # Suppress audio while the PTT relay is settling.
                                # announcement_delay_active is set in the same iteration
                                # that PTT first activates, so data already holds a real
                                # audio chunk — replace it with silence here too.
                                # KV4P uses serial audio with no physical relay, so no
                                # settle delay is needed — send real audio immediately.
                                _needs_delay = self.announcement_delay_active and not _use_kv4p_tx and not _use_d75_tx
                                pcm = b'\x00' * len(data) if _needs_delay else data
                                if self.config.OUTPUT_VOLUME != 1.0:
                                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                    pcm = np.clip(arr * self.config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
                                if _use_d75_tx:
                                    self.d75_plugin.write_tx_audio(pcm)
                                elif _use_kv4p_tx:
                                    self.kv4p_plugin.write_tx_audio(pcm)
                                else:
                                    try:
                                        self.output_stream.write(pcm, exception_on_overflow=False)
                                    except TypeError:
                                        self.output_stream.write(pcm)
                                _ptt_wrote = True
                            except IOError as io_err:
                                if self.config.VERBOSE_LOGGING:
                                    print(f"\n[Warning] Output stream buffer issue: {io_err}")
                            except Exception as tx_err:
                                if self.config.VERBOSE_LOGGING:
                                    print(f"\n[Error] Failed to send to radio TX: {tx_err}")

                        # EchoLink can optionally receive PTT audio directly
                        if self.echolink_source and self.config.RADIO_TO_ECHOLINK:
                            try:
                                self.echolink_source.send_audio(data)
                            except Exception as el_err:
                                if self.config.VERBOSE_LOGGING:
                                    print(f"\n[EchoLink] Send error: {el_err}")

                        # Local output routing during PTT:
                        # talkback OFF (default): local outputs get concurrent RX
                        #   only (so user can monitor on a separate radio)
                        # talkback ON: local outputs get TX audio (for local monitoring)
                        if self.tx_talkback:
                            # Talkback: send TX audio to all local outputs
                            _local_audio = data
                        else:
                            # No talkback: forward concurrent radio RX (if any)
                            _local_audio = (
                                getattr(self.radio_source, '_rx_cache', None)
                                if self.radio_source else rx_audio
                            )
                        _ws_local = _local_audio if _local_audio is not None else b'\x00' * len(data)
                        if _local_audio is not None:
                            if 'mumble' in self._bus_sinks.get(self._listen_bus_id, set()):
                                if (self.mumble and
                                        hasattr(self.mumble, 'sound_output') and
                                        self.mumble.sound_output is not None and
                                        getattr(self.mumble.sound_output, 'encoder_framesize', None) is not None):
                                    try:
                                        self.mumble.sound_output.add_sound(_local_audio)
                                    except Exception:
                                        pass
                            if 'speaker' in self._bus_sinks.get(self._listen_bus_id, set()):
                                if self.speaker_stream and not self.speaker_muted:
                                    self._speaker_enqueue(_local_audio)
                        _listen_flags_ptt = self._bus_stream_flags.get(self._listen_bus_id, {})
                        _bm_pcm_ptt = self.bus_manager.drain_pcm() if self.bus_manager else None
                        _pcm_ptt_out = _ws_local if _listen_flags_ptt.get('pcm', False) else None
                        if _bm_pcm_ptt is not None:
                            if _pcm_ptt_out is None:
                                _pcm_ptt_out = _bm_pcm_ptt
                            else:
                                from audio_bus import mix_audio_streams as _mix_ptt
                                _pcm_ptt_out = _mix_ptt(_pcm_ptt_out, _bm_pcm_ptt)
                        if _pcm_ptt_out is not None:
                            if self.web_config_server and self.web_config_server._ws_clients:
                                self.web_config_server.push_ws_audio(_pcm_ptt_out)

                        # Push to MP3 stream during PTT (talkback=TX audio, else concurrent RX)
                        if _listen_flags_ptt.get('mp3', False):
                            if self.web_config_server and self.web_config_server._stream_subscribers:
                                _mp3_audio = data if self.tx_talkback else _local_audio
                                if _mp3_audio is not None:
                                    self.web_config_server.push_audio(_mp3_audio)

                        # Send ONE frame to remote client during PTT — the mixed
                        # playback data.  Previously both rx_for_mumble AND data were
                        # sent, doubling the frame rate and causing client-side stutter.
                        if self.remote_audio_server and self.remote_audio_server.connected:
                            try:
                                _sv_t0 = time.monotonic()
                                self.remote_audio_server.send_audio(data)
                                _tr_sv_ms += (time.monotonic() - _sv_t0) * 1000
                                _tr_sv_sent += 1
                                self._update_sv_level(data)
                            except Exception:
                                pass

                        # Skip the normal RX→Mumble path below - this is TX audio
                        # Trace: encode PTT write status into outcome
                        # ptt_ok = wrote to AIOC, ptt_nostream = output_stream is None,
                        # ptt_txm = tx_muted, ptt_delay = in announcement delay
                        if _ptt_wrote:
                            _tr_outcome = 'ptt_ok'
                        elif not self.output_stream:
                            _tr_outcome = 'ptt_nostr'
                        elif self.tx_muted:
                            _tr_outcome = 'ptt_txm'
                        else:
                            _tr_outcome = 'ptt_err'
                        continue

                    # No PTT required (radio RX / SDR) — deliver to sinks that bypass VAD, then gate
                    if data and not self.check_vad(data):
                        if 'speaker' in self._bus_sinks.get(self._listen_bus_id, set()):
                            self._speaker_enqueue(data)
                        _tr_outcome = 'vad_gate'
                        continue

                elif False:
                    # Fallback AIOC path disabled — TH9800Plugin handles audio
                    try:
                        data = self.input_stream.read(
                            self.config.AUDIO_CHUNK_SIZE,
                            exception_on_overflow=False
                        )
                    except IOError as io_err:
                        if io_err.errno == -9981:  # Input overflow
                            if self.config.VERBOSE_LOGGING and consecutive_errors == 0:
                                print("\n[Diagnostic] Input overflow, clearing buffer...")
                            try:
                                self.input_stream.read(self.config.AUDIO_CHUNK_SIZE * 2, exception_on_overflow=False)
                            except:
                                pass
                            time.sleep(0.05)
                            continue
                        else:
                            raise

                    # Calculate audio level for TX
                    current_level = self.calculate_audio_level(data)
                    if current_level > self.tx_audio_level:
                        self.tx_audio_level = current_level
                    else:
                        self.tx_audio_level = int(self.tx_audio_level * 0.7 + current_level * 0.3)

                    self.last_audio_capture_time = time.time()
                    self.last_successful_read = time.time()
                    self.audio_capture_active = True

                    if self.config.INPUT_VOLUME != 1.0 and data:
                        try:
                            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                            data = np.clip(arr * self.config.INPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
                        except Exception:
                            pass

                    data = self.process_audio_for_mumble(data)

                    # PCM already pushed earlier (after mixer.tick) — don't double-push

                    if not self.check_vad(data):
                        # Speaker bypasses VAD — monitor even when Mumble is gated
                        if 'speaker' in self._bus_sinks.get(self._listen_bus_id, set()):
                            self._speaker_enqueue(data)
                        continue

                else:
                    # No mixer and no AIOC stream available — self-clock still paces us
                    self.audio_capture_active = False
                    continue

                # ── Common: reset error count and send to Mumble ─────────────────
                consecutive_errors = 0

                # Trace: compute data RMS and output-side sample discontinuity
                if self._trace_recording and data:
                    _tr_arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    _tr_data_rms = float(np.sqrt(np.mean(_tr_arr * _tr_arr))) if len(_tr_arr) > 0 else 0.0
                    # Output discontinuity: jump between last sample of previous
                    # chunk and first sample of this chunk (clicks if large)
                    _i16arr = np.frombuffer(data, dtype=np.int16)
                    if len(_i16arr) > 0:
                        _out_disc = float(abs(int(_i16arr[0]) - _out_last_sample))
                        _out_last_sample = int(_i16arr[-1])

                # Output click suppressor: detect sharp sample-to-sample jumps
                # in the mixed output and interpolate over a 4-sample window.
                # The mixer's additive summing can create boundary jumps larger
                # than any individual source when waveforms combine.
                if data and len(data) >= 16:
                    _arr = np.frombuffer(data, dtype=np.int16)
                    _diffs = np.abs(np.diff(_arr.astype(np.int32)))
                    _clicks = np.where(_diffs > 800)[0]
                    if len(_clicks) > 0:
                        _farr = _arr.astype(np.float32)
                        for _idx in _clicks:
                            _lo = max(0, _idx - 2)
                            _hi = min(len(_farr) - 1, _idx + 3)
                            if _hi - _lo >= 2:
                                _farr[_lo:_hi+1] = np.linspace(_farr[_lo], _farr[_hi], _hi - _lo + 1)
                        data = np.clip(_farr, -32768, 32767).astype(np.int16).tobytes()

                # Speaker output — only if connected as a sink on the listen bus.
                if 'speaker' in self._bus_sinks.get(self._listen_bus_id, set()):
                    if self.speaker_queue and not self.speaker_muted:
                        _tr_spk_qd = self.speaker_queue.qsize()
                    self._speaker_enqueue(data)
                _tr_spk_ok = True

                # Remote audio server send — must be BEFORE Mumble checks so it
                # works even when Mumble is not connected (e.g. secondary mode).
                if self.remote_audio_server and self.remote_audio_server.connected:
                    try:
                        _sv_t0 = time.monotonic()
                        self.remote_audio_server.send_audio(data)
                        _tr_sv_ms += (time.monotonic() - _sv_t0) * 1000
                        _tr_sv_sent += 1
                        self._update_sv_level(data)
                    except Exception:
                        pass

                # Gateway Link: send mixed audio to all connected endpoints
                if self.link_server and self.link_endpoints:
                    # Compute TX level once for all endpoints
                    _la = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    _lr = float(np.sqrt(np.mean(_la * _la))) if len(_la) > 0 else 0.0
                    _ldb = 20 * _math_mod.log10(_lr / 32767.0) if _lr > 0 else -100.0
                    _vad_t = getattr(self.config, 'VAD_THRESHOLD', -40)
                    for _ep_name in list(self.link_endpoints.keys()):
                        _ep_settings = self.link_endpoint_settings.get(_ep_name, {})
                        if _ep_settings.get('tx_muted', False):
                            self._link_tx_levels[_ep_name] = max(0, int(self._link_tx_levels.get(_ep_name, 0) * 0.7))
                            continue
                        try:
                            self.link_server.send_audio_to(_ep_name, data)
                            if _ldb > _vad_t:
                                _ll = int(max(0, min(100, (_ldb + 60) * (100 / 60))))
                                _prev = self._link_tx_levels.get(_ep_name, 0)
                                self._link_tx_levels[_ep_name] = _ll if _ll > _prev else int(_prev * 0.7 + _ll * 0.3)
                            else:
                                self._link_tx_levels[_ep_name] = max(0, int(self._link_tx_levels.get(_ep_name, 0) * 0.7))
                        except Exception:
                            pass

                # Mumble delivery handled early (before VAD gate) — skip here
                _tr_outcome = 'sent' if 'mumble' in self._bus_sinks.get(self._listen_bus_id, set()) else 'no_mumble_sink'

                if self.echolink_source and self.config.RADIO_TO_ECHOLINK:
                    try:
                        self.echolink_source.send_audio(data)
                    except Exception as el_err:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n[EchoLink] Send error: {el_err}")

                # Broadcastify already delivered early (after mixer.tick, before gates)

                # Push to web audio stream listeners (MP3 only, gated by bus M toggle)
                _listen_flags_mp3 = self._bus_stream_flags.get(self._listen_bus_id, {})
                if _listen_flags_mp3.get('mp3', False):
                    if self.web_config_server:
                        if self.web_config_server._stream_subscribers:
                            self.web_config_server.push_audio(data)

            except Exception as e:
                consecutive_errors += 1
                self.audio_capture_active = False
                _tr_outcome = 'exception'

                error_type = type(e).__name__
                error_msg = str(e)

                if "-9999" in error_msg or "Unanticipated host error" in error_msg:
                    if consecutive_errors == 1 and self.config.VERBOSE_LOGGING:
                        print(f"\n[Diagnostic] ALSA Error -9999: {error_type}: {error_msg}")
                        try:
                            if self.input_stream:
                                print(f"  Stream state: active={self.input_stream.is_active()}, stopped={self.input_stream.is_stopped()}")
                        except:
                            pass
                else:
                    if consecutive_errors == 1 and self.config.VERBOSE_LOGGING:
                        print(f"\n[Diagnostic] Audio error #{consecutive_errors}: {error_type}: {error_msg}")

                self.last_stream_error = f"{error_type}: {error_msg}"

                if consecutive_errors >= max_consecutive_errors:
                    if self.config.VERBOSE_LOGGING:
                        print(f"\n✗ Audio capture failed {consecutive_errors} times, restarting AIOC stream...")
                    self.restart_audio_input()
                    self.stream_restart_count += 1
                    consecutive_errors = 0
                    time.sleep(1)
                # else: self-clock at top of loop handles pacing
            finally:
                # ── Trace record (toggled by 'i' key) ──
                if self._trace_recording:
                    # Snapshot enhanced instrumentation from sources
                    _sdr1_disc = 0.0
                    _sdr1_sb_after = -1
                    _sdr1_cb_ovf = 0
                    _sdr1_cb_drop = 0
                    _aioc_disc = getattr(self.radio_source, '_serve_discontinuity', 0.0) if self.radio_source else 0.0
                    _aioc_sb_after = getattr(self.radio_source, '_sub_buffer_after', -1) if self.radio_source else -1
                    _aioc_cb_ovf = getattr(self.radio_source, '_cb_overflow_count', 0) if self.radio_source else 0
                    _aioc_cb_drop = getattr(self.radio_source, '_cb_drop_count', 0) if self.radio_source else 0
                    _kv4p_snap = self.kv4p_plugin.get_trace_snapshot() if self.kv4p_plugin else {}

                    _trace.append((
                        _tick_start - self._audio_trace_t0,  # 0: time (s)
                        _tick_dt,                             # 1: tick interval (ms)
                        _tr_sdr_q,                            # 2: SDR1 queue depth before
                        _tr_sdr_sb,                           # 3: SDR1 sub-buffer bytes before
                        _tr_aioc_q,                           # 4: AIOC queue depth before
                        _tr_aioc_sb,                          # 5: AIOC sub-buffer bytes before
                        _tr_mixer_got,                        # 6: mixer returned audio?
                        ','.join(active_sources) if active_sources else '',  # 7: active sources
                        _tr_mixer_ms,                         # 8: mixer call duration (ms)
                        0.0,  # 9: SDR blocked (ms)
                        getattr(self.radio_source, '_last_blocked_ms', 0.0) if self.radio_source else 0.0,  # 10: AIOC blocked (ms)
                        _tr_outcome,                          # 11: outcome (sent/no_mumble/no_sndout/no_codec/ptt/exception)
                        _tr_mumble_ms,                        # 12: Mumble add_sound time (ms)
                        _tr_spk_ok,                           # 13: speaker enqueue attempted?
                        _tr_spk_qd,                           # 14: speaker queue depth before enqueue
                        _tr_data_rms,                         # 15: RMS of data sent
                        len(data) if data else 0,              # 16: data length (bytes)
                        _tr_mixer_state,                       # 17: mixer internal state dict
                        _tr_sdr2_q,                           # 18: SDR2 queue depth before
                        _tr_sdr2_sb,                          # 19: SDR2 sub-buffer bytes before
                        _tr_sdr_prebuf,                       # 20: SDR1 _prebuffering flag
                        _tr_sdr2_prebuf,                      # 21: SDR2 _prebuffering flag
                        _tr_rebro,                            # 22: rebroadcast state (''=off, sig/hold/idle)
                        _tr_sv_ms,                            # 23: RemoteAudioServer send_audio time (ms)
                        _tr_sv_sent,                          # 24: number of SV send_audio calls this tick
                        # === Enhanced instrumentation (25+) ===
                        _sdr1_disc,                           # 25: SDR1 sample discontinuity (abs delta)
                        _sdr1_sb_after,                       # 26: SDR1 sub-buffer bytes AFTER serve
                        _sdr1_cb_ovf,                         # 27: SDR1 cumulative callback overflow count
                        _sdr1_cb_drop,                        # 28: SDR1 cumulative callback queue drops
                        _aioc_disc,                           # 29: AIOC sample discontinuity (abs delta)
                        _aioc_sb_after,                       # 30: AIOC sub-buffer bytes AFTER serve
                        _aioc_cb_ovf,                         # 31: AIOC cumulative callback overflow count
                        _aioc_cb_drop,                        # 32: AIOC cumulative callback queue drops
                        _out_disc,                            # 33: output-side sample discontinuity (mixer output)
                        # === KV4P trace fields (34+) ===
                        _kv4p_snap.get('rx_frames', 0),       # 34: KV4P Opus frames received this tick
                        _kv4p_snap.get('rx_bytes', 0),        # 35: KV4P Opus bytes received this tick
                        _kv4p_snap.get('queue_drops', 0),     # 36: KV4P queue overflow drops this tick
                        _kv4p_snap.get('sub_buf_before', 0),  # 37: KV4P sub_buffer bytes before get_audio
                        _kv4p_snap.get('sub_buf_after', 0),   # 38: KV4P sub_buffer bytes after get_audio
                        _kv4p_snap.get('returned_data', False),  # 39: KV4P returned audio this tick?
                        _kv4p_snap.get('pcm_rms', 0.0),      # 40: KV4P output PCM RMS
                        _kv4p_snap.get('queue_len', 0),       # 41: KV4P queue length at snapshot
                        _kv4p_snap.get('decode_errors', 0),   # 42: KV4P Opus decode errors this tick
                        # === KV4P TX trace fields (43+) ===
                        _kv4p_snap.get('tx_frames', 0),       # 43: Opus frames encoded+sent to radio
                        _kv4p_snap.get('tx_dropped', 0),      # 44: PCM bytes dropped (partial-frame remainder)
                        _kv4p_snap.get('tx_input_rms', 0.0),  # 45: RMS of PCM fed to encoder
                        _kv4p_snap.get('tx_errors', 0),       # 46: encoder exceptions
                        self.announcement_delay_active and not (str(getattr(self.config, 'TX_RADIO', '')).lower() == 'kv4p' and bool(self.kv4p_plugin)),  # 47: TX to KV4P silenced by PTT settle delay (False when TX_RADIO=kv4p, fix in place)
                        0.0,  # 48: SDR2 sample discontinuity (abs delta)
                        -1,  # 49: SDR2 sub-buffer bytes after serve
                    ))
    
    def _find_darkice_pid(self):
        """Find a running DarkIce process. Returns PID (int) or None."""
        import subprocess
        try:
            result = subprocess.run(['pgrep', '-x', 'darkice'],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                # pgrep may return multiple PIDs — take the first
                return int(result.stdout.strip().splitlines()[0])
        except Exception:
            pass
        return None

    def _get_darkice_stats(self):
        """Get live DarkIce streaming statistics. Returns dict or None."""
        pid = self._darkice_pid
        if not pid:
            return None
        stats = {}
        try:
            # Process uptime from /proc/pid/stat (field 22 = start time in ticks)
            with open('/proc/uptime') as f:
                sys_uptime = float(f.read().split()[0])
            with open(f'/proc/{pid}/stat') as f:
                start_ticks = int(f.read().split()[21])
            clk_tck = os.sysconf('SC_CLK_TCK')
            stats['uptime'] = int(sys_uptime - start_ticks / clk_tck)
        except Exception:
            stats['uptime'] = 0
        try:
            # TCP connection stats via ss -ti to Broadcastify server
            import subprocess
            server = str(getattr(self.config, 'STREAM_SERVER', '')).strip()
            if not server or server == 'localhost':
                # Find remote IP from /proc/pid/net/tcp
                with open(f'/proc/{pid}/net/tcp') as f:
                    for line in f.readlines()[1:]:
                        parts = line.split()
                        if parts[3] == '01':  # ESTABLISHED
                            remote_hex = parts[2].split(':')[0]
                            rip = '.'.join(str(int(remote_hex[i:i+2], 16)) for i in (6, 4, 2, 0))
                            if rip not in ('127.0.0.1', '0.0.0.0'):
                                server = rip
                                break
            if server:
                result = subprocess.run(['ss', '-ti', 'dst', server],
                                        capture_output=True, text=True, timeout=3)
                out = result.stdout
                # Parse key=value pairs from ss extended info
                import re
                for key in ('bytes_sent', 'bytes_acked', 'bytes_received',
                            'segs_out', 'segs_in', 'data_segs_out'):
                    m = re.search(rf'{key}:(\d+)', out)
                    if m:
                        stats[key] = int(m.group(1))
                m = re.search(r'rtt:([\d.]+)/([\d.]+)', out)
                if m:
                    stats['rtt'] = float(m.group(1))
                m = re.search(r'send ([\d.]+)(\w+)', out)
                if m:
                    stats['send_rate'] = m.group(1) + m.group(2)
                m = re.search(r'busy:(\d+)ms', out)
                if m:
                    stats['busy_ms'] = int(m.group(1))
                # TCP connection established = connected
                stats['connected'] = 'ESTAB' in out
            else:
                stats['connected'] = False
        except Exception:
            stats['connected'] = False
        return stats

    def _get_stream_stats(self):
        """Get live streaming statistics from direct Icecast connection."""
        so = getattr(self, 'stream_output', None)
        if not so or not so.connected:
            return {}
        uptime_s = int(so.uptime)
        return {
            'connected': True,
            'uptime': uptime_s,
            'bytes_sent': int(so._bytes_sent),
            'send_rate': f"{so._bytes_sent * 8 / max(uptime_s, 1) / 1000:.1f} kbps" if uptime_s > 0 else '—',
            'server': getattr(self.config, 'STREAM_SERVER', ''),
            'mount': getattr(self.config, 'STREAM_MOUNT', ''),
            'bitrate': int(getattr(self.config, 'STREAM_BITRATE', 16)),
        }

    def _get_darkice_stats_cached(self):
        """Return cached DarkIce stats, refreshing every 5 seconds."""
        now = time.time()
        if now - self._darkice_stats_time > 5:
            self._darkice_stats_cache = self._get_darkice_stats()
            self._darkice_stats_time = now
        return self._darkice_stats_cache

    def _restart_darkice(self):
        """Restart DarkIce after it has died."""
        import subprocess
        try:
            subprocess.Popen(
                ['darkice', '-c', '/etc/darkice.cfg'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
            pid = self._find_darkice_pid()
            self._darkice_restart_count += 1
            if pid:
                self._darkice_pid = pid
                print(f"\n  DarkIce restarted (PID {pid}), total restarts: {self._darkice_restart_count}")
            else:
                print(f"\n  DarkIce restart failed — process not found after launch")
        except Exception as e:
            self._darkice_restart_count += 1
            print(f"\n  DarkIce restart error: {e}")

    def restart_audio_input(self):
        """Attempt to restart the audio input stream"""
        # Suppress ALL stderr during restart (ALSA is very noisy)
        import sys
        import os as restart_os

        # Use the original stderr fd (may have been redirected by LogWriter)
        _orig_stderr = getattr(self, '_orig_stderr', sys.stderr)
        stderr_fd = _orig_stderr.fileno() if hasattr(_orig_stderr, 'fileno') else 2
        saved_stderr_fd = restart_os.dup(stderr_fd)
        devnull_fd = restart_os.open(restart_os.devnull, restart_os.O_WRONLY)
        
        try:
            # Redirect stderr to suppress ALSA messages
            restart_os.dup2(devnull_fd, stderr_fd)
            
            # Signal audio loop to stop reading
            self.restarting_stream = True
            
            # Give current read operation time to complete
            time.sleep(0.15)
            
            if self.config.VERBOSE_LOGGING:
                # Temporarily restore stderr for our diagnostic messages
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print("  [Diagnostic] Closing input stream...")
                restart_os.dup2(devnull_fd, stderr_fd)
            
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                    self.input_stream.close()
                except:
                    pass  # Ignore all errors during close
            
            # Small delay to let ALSA settle
            time.sleep(0.2)
            
            if self.config.VERBOSE_LOGGING:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print("  [Diagnostic] Re-finding AIOC device...")
                # Keep stderr suppressed for device enumeration
            
            # Re-find AIOC device (with stderr still suppressed)
            input_idx, _ = self.find_aioc_audio_device()
            
            if input_idx is None:
                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print("  ✗ Could not find AIOC input device")
                return
            
            # Determine format
            if self.config.AUDIO_BITS == 16:
                audio_format = pyaudio.paInt16
            elif self.config.AUDIO_BITS == 24:
                audio_format = pyaudio.paInt24
            elif self.config.AUDIO_BITS == 32:
                audio_format = pyaudio.paInt32
            else:
                audio_format = pyaudio.paInt16
            
            if self.config.VERBOSE_LOGGING:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print(f"  [Diagnostic] Opening new input stream (device {input_idx})...")
                restart_os.dup2(devnull_fd, stderr_fd)
            
            aioc_callback = getattr(self.radio_source, '_audio_callback', None) if self.radio_source else None
            try:
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                    stream_callback=aioc_callback
                )

                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print("  ✓ Audio input stream restarted")
                    restart_os.dup2(devnull_fd, stderr_fd)
                
                # Give USB/ALSA time to stabilize after restart
                time.sleep(0.1)
                
                # Update stream age
                self.stream_age = time.time()
                
                # Re-enable audio loop
                self.restarting_stream = False
                
            except Exception as stream_error:
                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print(f"  ✗ Failed to open stream: {type(stream_error).__name__}: {stream_error}")
                    print("  [Diagnostic] Attempting full PyAudio restart...")
                
                # If stream creation fails, try restarting entire PyAudio instance
                self.restart_pyaudio()
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                try:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                except:
                    pass
                print(f"  ✗ Failed to restart audio input: {type(e).__name__}: {e}")
        finally:
            # Always restore stderr and cleanup
            try:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                restart_os.close(saved_stderr_fd)
                restart_os.close(devnull_fd)
            except:
                pass
            # Always re-enable audio loop
            self.restarting_stream = False
    
    def restart_pyaudio(self):
        """Restart the entire PyAudio instance (for serious ALSA errors)"""
        try:
            if self.config.VERBOSE_LOGGING:
                print("  [Diagnostic] Terminating PyAudio instance...")
            
            # Close all streams
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                    self.input_stream.close()
                except:
                    pass
            
            if self.output_stream:
                try:
                    self.output_stream.stop_stream()
                    self.output_stream.close()
                except:
                    pass
            
            # Terminate PyAudio
            if self.pyaudio_instance:
                try:
                    self.pyaudio_instance.terminate()
                except:
                    pass
            
            time.sleep(0.5)  # Give ALSA time to clean up
            
            if self.config.VERBOSE_LOGGING:
                print("  [Diagnostic] Reinitializing PyAudio...")
            
            # Reinitialize PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()
            
            # Find devices
            input_idx, output_idx = self.find_aioc_audio_device()
            
            # Determine format
            if self.config.AUDIO_BITS == 16:
                audio_format = pyaudio.paInt16
            else:
                audio_format = pyaudio.paInt16
            
            # Recreate streams
            if output_idx is not None:
                self.output_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    output=True,
                    output_device_index=output_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4  # Larger buffer for smoother output
                )
            
            if input_idx is not None:
                aioc_callback = getattr(self.radio_source, '_audio_callback', None) if self.radio_source else None
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                    stream_callback=aioc_callback
                )

            if self.config.VERBOSE_LOGGING:
                print("  ✓ PyAudio fully restarted")
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"  ✗ Failed to restart PyAudio: {type(e).__name__}: {e}")
    
    def handle_proc_toggle(self, source, filt, state=None):
        """Toggle or set a processing filter for a specific source.
        Called from the /proc_toggle and /mixer API endpoints.
        If state is None, toggles; if True/False, sets explicitly.
        """
        # Map source to config keys and sync method
        _source_map = {
            'radio': ({
                'gate':  'ENABLE_NOISE_GATE',
                'hpf':   'ENABLE_HIGHPASS_FILTER',
                'lpf':   'ENABLE_LOWPASS_FILTER',
                'notch': 'ENABLE_NOTCH_FILTER',
            }, '_sync_radio_processor'),
            'sdr': ({
                'gate':  'SDR_PROC_ENABLE_NOISE_GATE',
                'hpf':   'SDR_PROC_ENABLE_HPF',
                'lpf':   'SDR_PROC_ENABLE_LPF',
                'notch': 'SDR_PROC_ENABLE_NOTCH',
            }, '_sync_sdr_plugin_processors'),
            'd75': ({
                'gate':  'D75_PROC_ENABLE_NOISE_GATE',
                'hpf':   'D75_PROC_ENABLE_HPF',
                'lpf':   'D75_PROC_ENABLE_LPF',
                'notch': 'D75_PROC_ENABLE_NOTCH',
            }, '_sync_d75_plugin_processor'),
            'kv4p': ({
                'gate':  'KV4P_PROC_ENABLE_NOISE_GATE',
                'hpf':   'KV4P_PROC_ENABLE_HPF',
                'lpf':   'KV4P_PROC_ENABLE_LPF',
                'notch': 'KV4P_PROC_ENABLE_NOTCH',
            }, '_sync_kv4p_plugin_processor'),
        }
        entry = _source_map.get(source)
        if not entry:
            return
        toggle_map, sync_method = entry
        key = toggle_map.get(filt)
        if key:
            if state is None:
                current = getattr(self.config, key, False)
                setattr(self.config, key, not current)
            else:
                setattr(self.config, key, bool(state))
            getattr(self, sync_method)()

    def handle_key(self, char):
        """Process a key command (called by keyboard loop and web UI)."""
        char = char.lower()

        if char == 't':
            self.tx_muted = not self.tx_muted
            self._trace_events.append((time.monotonic(), 'tx_mute', 'on' if self.tx_muted else 'off'))
        elif char == 'r':
            self.rx_muted = not self.rx_muted
            self._trace_events.append((time.monotonic(), 'rx_mute', 'on' if self.rx_muted else 'off'))
        elif char == 'm':
            if self.tx_muted and self.rx_muted:
                self.tx_muted = False
                self.rx_muted = False
            else:
                self.tx_muted = True
                self.rx_muted = True
            self._trace_events.append((time.monotonic(), 'global_mute', f'tx={self.tx_muted} rx={self.rx_muted}'))
        elif char == 's':
            if self.sdr_plugin:
                self.sdr_muted = not self.sdr_muted
                self.sdr_plugin.tuner1_muted = self.sdr_muted
                self._trace_events.append((time.monotonic(), 'sdr_mute', 'on' if self.sdr_muted else 'off'))
        elif char == 'd':
            if self.sdr_plugin:
                self.sdr_plugin.duck = not self.sdr_plugin.duck
        elif char == 'x':
            if self.sdr_plugin:
                self.sdr2_muted = not self.sdr2_muted
                self.sdr_plugin.tuner2_muted = self.sdr2_muted
                self._trace_events.append((time.monotonic(), 'sdr2_mute', 'on' if self.sdr2_muted else 'off'))
        elif char == 'c':
            if self.remote_audio_source:
                self.remote_audio_muted = not self.remote_audio_muted
                self.remote_audio_source.muted = self.remote_audio_muted
                self._trace_events.append((time.monotonic(), 'remote_mute', 'on' if self.remote_audio_muted else 'off'))
        elif char == 'k':
            if self.remote_audio_server:
                self.remote_audio_server.reset()
                self._trace_events.append((time.monotonic(), 'remote_reset', 'server'))
            elif self.remote_audio_source:
                self.remote_audio_source.reset()
                self._trace_events.append((time.monotonic(), 'remote_reset', 'client'))
        elif char == 'v':
            self.config.ENABLE_VAD = not self.config.ENABLE_VAD
        elif char == ',':
            self.config.INPUT_VOLUME = max(0.1, self.config.INPUT_VOLUME - 0.1)
        elif char == '.':
            self.config.INPUT_VOLUME = min(3.0, self.config.INPUT_VOLUME + 0.1)
        elif char == 'n':
            self.config.ENABLE_NOISE_GATE = not self.config.ENABLE_NOISE_GATE
            self._sync_radio_processor()
        elif char == 'f':
            self.config.ENABLE_HIGHPASS_FILTER = not self.config.ENABLE_HIGHPASS_FILTER
            self._sync_radio_processor()
        elif char == 'a':
            if self.announce_input_source:
                self.announce_input_muted = not self.announce_input_muted
                self.announce_input_source.muted = self.announce_input_muted
        elif char == 'g':
            self.config.ENABLE_AGC = not self.config.ENABLE_AGC
        elif char == 'e':
            self.config.ENABLE_ECHO_CANCELLATION = not self.config.ENABLE_ECHO_CANCELLATION
        elif char == 'p':
            if self.aioc_device or str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower() != 'aioc':
                self.manual_ptt_mode = not self.manual_ptt_mode
                self._pending_ptt_state = self.manual_ptt_mode
                self._trace_events.append((time.monotonic(), 'ptt', 'on' if self.manual_ptt_mode else 'off'))
        elif char == 'b':
            self.sdr_rebroadcast = not self.sdr_rebroadcast
            if not self.sdr_rebroadcast:
                if self._rebroadcast_ptt_active and self.ptt_active:
                    self.set_ptt_state(False)
                    self._ptt_change_time = time.monotonic()
                    self._rebroadcast_ptt_active = False
                if self.radio_source:
                    self.radio_source.enabled = True
                self._rebroadcast_sending = False
                self._rebroadcast_ptt_hold_until = 0
            self._trace_events.append((time.monotonic(), 'sdr_rebroadcast', 'on' if self.sdr_rebroadcast else 'off'))
        elif char == 'j':
            if self.relay_radio and not self._relay_radio_pressing:
                def _pulse_power():
                    self._relay_radio_pressing = True
                    self.relay_radio.set_state(True)
                    self._trace_events.append((time.monotonic(), 'relay_radio', 'press'))
                    time.sleep(1.0)
                    self.relay_radio.set_state(False)
                    self._relay_radio_pressing = False
                    self._trace_events.append((time.monotonic(), 'relay_radio', 'release'))
                threading.Thread(target=_pulse_power, daemon=True).start()
        elif char == 'h':
            if self.relay_charger:
                new_state = not self.relay_charger_on
                self.relay_charger.set_state(new_state)
                self.relay_charger_on = new_state
                self._charger_manual = True
                self._trace_events.append((time.monotonic(), 'relay_charger', f'manual_{"on" if new_state else "off"}'))
        elif char == 'o':
            if self.speaker_stream:
                self.speaker_muted = not self.speaker_muted
                self._trace_events.append((time.monotonic(), 'spk_mute', 'on' if self.speaker_muted else 'off'))
        elif char == 'w':
            if self.d75_plugin:
                self.d75_muted = not self.d75_muted
                self.d75_plugin.muted = self.d75_muted
                self._trace_events.append((time.monotonic(), 'd75_mute', 'on' if self.d75_muted else 'off'))
        elif char == 'y':
            if self.kv4p_plugin:
                self.kv4p_muted = not self.kv4p_muted
                self.kv4p_plugin.muted = self.kv4p_muted
                self._trace_events.append((time.monotonic(), 'kv4p_mute', 'on' if self.kv4p_muted else 'off'))
        elif char == 'l':
            if self.cat_client:
                def _send_cat_config():
                    try:
                        self.cat_client._stop = False
                        self.cat_client.setup_radio(self.config)
                    except Exception:
                        pass
                threading.Thread(target=_send_cat_config, daemon=True, name="CAT-ManualConfig").start()
        elif char in '0123456789':
            if self.playback_source:
                stored_path = self.playback_source.file_status[char]['path']
                if stored_path:
                    # Auto-set RTS to Radio Controlled for TX playback — RTS relay
                    # must route mic wiring through front panel for AIOC PTT.
                    # No CAT commands while Radio Controlled (serial disconnected).
                    # Software PTT and D75 TX don't need RTS switching.
                    _ptt_method = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
                    _tx_radio = str(getattr(self.config, 'TX_RADIO', 'th9800')).lower()
                    if _ptt_method != 'software' and _tx_radio != 'd75':
                        _cat = self.cat_client
                        if _cat and not getattr(self, '_playback_rts_saved', None):
                            self._playback_rts_saved = _cat.get_rts()
                            if self._playback_rts_saved is None or self._playback_rts_saved is True:
                                try:
                                    _cat._pause_drain()
                                    try:
                                        _cat.set_rts(False)  # Radio Controlled
                                        time.sleep(0.3)
                                        _cat._drain(0.5)
                                    finally:
                                        _cat._drain_paused = False
                                    print(f"\n[Playback] RTS → Radio Controlled")
                                except Exception:
                                    pass
                    # Stop current playback immediately, then decode+queue
                    # in a background thread so the HTTP handler returns fast.
                    # The lock serializes concurrent decodes; the sequence
                    # counter lets later presses discard earlier in-flight decodes.
                    pb = self.playback_source
                    pb._play_seq += 1
                    my_seq = pb._play_seq
                    pb.stop_playback()
                    def _bg_play(_pb=pb, _path=stored_path, _seq=my_seq, _gw=self):
                        try:
                            with _pb._play_lock:
                                if _pb._play_seq != _seq:
                                    return  # A newer button press superseded this one
                                if not _pb.queue_file(_path):
                                    _gw.notify(f"Playback failed: {os.path.basename(_path)}")
                        except Exception as e:
                            print(f"\n[Playback] Error in background decode: {e}")
                            _gw.notify(f"Playback error: {e}")
                    threading.Thread(target=_bg_play, daemon=True, name="Playback-Queue").start()
        elif char == '-':
            if self.playback_source:
                self.playback_source.stop_playback()
        elif char in ('[', ']', '\\'):
            slot = {'[': 1, ']': 2, '\\': 3}[char]
            if self.smart_announce and self.smart_announce._claude_bin:
                self.smart_announce.trigger(slot)
        elif char == '@':
            if self.email_notifier:
                print(f"\n[Email] Sending status email...")
                threading.Thread(target=self.email_notifier.send_startup_status,
                                 daemon=True, name="email-manual").start()
            else:
                print(f"\n[Email] Not configured")
        elif char == 'q':
            print(f"\n[WebUI] Restarting gateway...")
            self.restart_requested = True
            self.running = False

    def get_status_dict(self):
        """Return current gateway status as a dict for the web UI."""
        import json
        uptime_s = time.time() - self.start_time if hasattr(self, 'start_time') else 0
        d, rem = divmod(int(uptime_s), 86400)
        h, rem2 = divmod(rem, 3600)
        mi, s = divmod(rem2, 60)
        uptime_str = f"{d}d {h:02d}:{mi:02d}:{s:02d}"

        mumble_ok = getattr(self, 'mumble', None) and getattr(self.mumble, 'is_alive', lambda: False)()

        # Audio levels — use plugin levels directly
        radio_rx = self.th9800_plugin.audio_level if self.th9800_plugin else 0
        radio_tx = getattr(self.th9800_plugin, 'tx_audio_level', 0) if self.th9800_plugin else 0
        sdr1_level = self.sdr_plugin.tuner1_level if self.sdr_plugin else 0
        sdr2_level = self.sdr_plugin.tuner2_level if self.sdr_plugin else 0
        sv_level = getattr(self, 'sv_audio_level', 0)
        speaker_level = getattr(self, 'speaker_audio_level', 0)
        an_level = self.announce_input_source.audio_level if self.announce_input_source and hasattr(self.announce_input_source, 'audio_level') else 0
        cl_level = self.remote_audio_source.audio_level if self.remote_audio_source and hasattr(self.remote_audio_source, 'audio_level') else 0

        # PTT method tag
        _ptt_m = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
        _ptt_tag = {'aioc': 'AIOC', 'relay': 'Relay', 'software': 'Software'}.get(_ptt_m, _ptt_m)

        # Processing flags (per-source)
        proc = self.radio_processor.get_active_list()
        sdr_proc = self.sdr_processor.get_active_list()

        # Smart announce countdowns
        sa_countdowns = []
        if self.smart_announce and hasattr(self.smart_announce, 'get_countdowns'):
            for sa_id, sa_secs, sa_mode in self.smart_announce.get_countdowns():
                if sa_mode == 'manual':
                    sa_countdowns.append({'id': sa_id, 'remaining': 'Manual', 'mode': 'manual'})
                else:
                    sd, sr = divmod(int(sa_secs), 86400)
                    sh, sr2 = divmod(sr, 3600)
                    sm, ss = divmod(sr2, 60)
                    sa_countdowns.append({'id': sa_id, 'remaining': f"{sd}d {sh:02d}:{sm:02d}:{ss:02d}", 'mode': 'auto'})

        # DDNS
        ddns_status = ''
        if self.ddns_updater:
            ddns_status = self.ddns_updater.get_status() or '...'

        # Charger
        charger_state = ''
        if self.relay_charger:
            charger_state = 'CHARGING' if self.relay_charger_on else 'DRAINING'
            if self._charger_manual:
                charger_state += '*'

        # CAT
        cat_state = ''
        cat_reliability = {}
        cat_vol = {}
        if self.cat_client:
            cat_state = 'active' if time.monotonic() - self.cat_client._last_activity < 1.0 else 'idle'
            cat_reliability = {
                'sent': self.cat_client._cmd_sent,
                'missed': self.cat_client._cmd_no_response,
                'last_miss': self.cat_client._last_no_response,
            }
            cat_vol = {
                'left': self.cat_client._volume.get(self.cat_client.LEFT, 25),
                'right': self.cat_client._volume.get(self.cat_client.RIGHT, 25),
            }
        elif getattr(self.config, 'ENABLE_CAT_CONTROL', False):
            cat_state = 'disconnected'

        # Build file status
        file_slots = {}
        if self.playback_source:
            for k, v in self.playback_source.file_status.items():
                file_slots[k] = {
                    'name': v.get('filename', ''),
                    'loaded': bool(v.get('path')),
                    'playing': v.get('playing', False),
                }

        return {
            'uptime': uptime_str,
            'mumble': mumble_ok,
            'ptt_active': getattr(self, 'ptt_active', False),
            'ptt_method': _ptt_tag,
            'manual_ptt': getattr(self, 'manual_ptt_mode', False),
            'vad_enabled': self.config.ENABLE_VAD,
            'vad_db': round(getattr(self, 'vad_envelope', -100), 1),
            'tx_muted': self.tx_muted,
            'rx_muted': self.rx_muted,
            'sdr1_muted': getattr(self, 'sdr_muted', False),
            'sdr2_muted': getattr(self, 'sdr2_muted', False),
            'sdr1_duck': self.sdr_plugin.duck if self.sdr_plugin else False,
            'sdr_rebroadcast': getattr(self, 'sdr_rebroadcast', False),
            'tx_talkback': getattr(self, 'tx_talkback', False),
            'remote_muted': getattr(self, 'remote_audio_muted', False),
            'announce_muted': getattr(self, 'announce_input_muted', False),
            'speaker_muted': getattr(self, 'speaker_muted', True),
            'radio_rx': radio_rx,
            'radio_tx': radio_tx,
            'sdr1_level': sdr1_level,
            'sdr2_level': sdr2_level,
            'sdr1_ducked': getattr(self, 'sdr_ducked', False),
            'sdr2_ducked': getattr(self, 'sdr2_ducked', False),
            'cl_ducked': getattr(self, 'remote_audio_ducked', False),
            'remote_level': sv_level if self.remote_audio_server else cl_level,
            'remote_mode': 'SV' if self.remote_audio_server else 'CL',
            'speaker_level': speaker_level,
            'an_level': an_level,
            'volume': round(self.config.INPUT_VOLUME, 1),
            'processing': proc,
            'radio_proc': proc,
            'sdr_proc': sdr_proc,
            'd75_proc': self.d75_processor.get_active_list(),
            'kv4p_proc': self.kv4p_processor.get_active_list(),
            'smart_countdowns': sa_countdowns,
            'smart_activity': self.smart_announce.get_activity() if self.smart_announce and hasattr(self.smart_announce, 'get_activity') else {},
            'ddns': ddns_status,
            'tunnel_url': self.cloudflare_tunnel.get_url() if self.cloudflare_tunnel else '',
            'charger': charger_state,
            'cat': cat_state,
            'cat_reliability': cat_reliability,
            'cat_vol': cat_vol,
            'relay_pressing': getattr(self, '_relay_radio_pressing', False),
            'sdr1_enabled': bool(self.sdr_plugin and self.sdr_plugin.tuner1_enabled),
            'sdr2_enabled': bool(self.sdr_plugin and self.sdr_plugin.tuner2_enabled),
            'speaker_enabled': bool(self.speaker_stream),
            'remote_enabled': bool(self.remote_audio_source or self.remote_audio_server),
            'announce_enabled': bool(self.announce_input_source),
            'relay_radio_enabled': bool(self.relay_radio),
            'relay_charger_enabled': bool(self.relay_charger),
            'ms1_state': self.mumble_server_1.state if self.mumble_server_1 else None,
            'ms2_state': self.mumble_server_2.state if self.mumble_server_2 else None,
            'cat_enabled': bool(self.cat_client) or getattr(self.config, 'ENABLE_CAT_CONTROL', False),
            'd75_enabled': bool(self.d75_plugin) or getattr(self.config, 'ENABLE_D75', False),
            'd75_connected': bool(self.d75_plugin and getattr(self.d75_plugin, '_serial_connected', False)),
            'd75_audio_connected': bool(self.d75_plugin and self.d75_plugin.server_connected),
            'd75_mode': str(getattr(self.config, 'D75_CONNECTION', 'bluetooth')).lower().strip(),
            'd75_level': self.d75_plugin.audio_level if self.d75_plugin else 0,
            'd75_muted': getattr(self, 'd75_muted', False),
            'kv4p_enabled': bool(self.kv4p_plugin),
            'kv4p_level': self.kv4p_plugin.audio_level if self.kv4p_plugin else 0,
            'kv4p_muted': getattr(self, 'kv4p_muted', False),
            'adsb_enabled': getattr(self.config, 'ENABLE_ADSB', False),
            'telegram_enabled': getattr(self.config, 'ENABLE_TELEGRAM', False),
            'monitor_enabled': bool(self.web_monitor_source),
            'monitor_level': self.web_monitor_source.audio_level if self.web_monitor_source else 0,
            'link_enabled': bool(self.link_server),
            'link_endpoints': [
                {
                    'name': name,
                    'connected': True,
                    'plugin': (self.link_server.get_endpoint_info(name) or {}).get('plugin', '') if self.link_server else '',
                    'capabilities': (self.link_server.get_endpoint_info(name) or {}).get('capabilities', {}) if self.link_server else {},
                    'level': src.audio_level,
                    'rx_muted': src.muted,
                    'tx_muted': self.link_endpoint_settings.get(name, {}).get('tx_muted', False),
                    'ptt_active': self._link_ptt_active.get(name, False),
                    'tx_level': self._link_tx_levels.get(name, 0),
                    'endpoint_status': self._link_last_status.get(name, {}),
                }
                for name, src in list(self.link_endpoints.items())
            ],
            'files': file_slots,
            'playback_enabled': bool(self.playback_source),
            'tts_enabled': bool(getattr(self, 'tts_engine', None)),
            'smart_announce_enabled': bool(self.smart_announce),
            # Broadcastify / Icecast streaming
            'streaming_enabled': bool(getattr(self.config, 'ENABLE_STREAM_OUTPUT', False)),
            'stream_connected': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'stream_pipe_ok': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'darkice_running': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'darkice_pid': None,
            'darkice_restarts': 0,
            'stream_restarts': self.stream_restart_count,
            'stream_health': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'darkice_stats': self._get_stream_stats(),
            'notifications': list(self._notifications),
            'automation_enabled': bool(self.automation_engine),
            'automation_task': self.automation_engine._current_task if self.automation_engine else None,
            'automation_recording': self.automation_engine.recorder.is_recording() if self.automation_engine else False,
        }

    def status_monitor_loop(self):
        """Monitor PTT release timeout and audio transmit status"""
        # Note: Priority scheduling removed - system manages all threads

        status_check_interval = self.config.STATUS_UPDATE_INTERVAL
        last_status_check = time.time()

        while self.running:
          try:
            current_time = time.time()

            # Check PTT timeout or if TX is muted
            if self.ptt_active and not self.manual_ptt_mode and not self._rebroadcast_ptt_active and not self._webmic_ptt_active:
                # Release PTT if timeout OR if TX is muted
                # (Don't keep PTT keyed when muted!)
                # But don't release if in manual PTT mode or rebroadcast mode
                if current_time - self.last_sound_time > self.config.PTT_RELEASE_DELAY or self.tx_muted:
                    # Queue the HID write to the audio thread.  Clear ptt_active
                    # immediately so this block is not re-entered on the next tick.
                    self.ptt_active = False
                    self._pending_ptt_state = False
                    self._ptt_change_time = time.monotonic()

            # Periodic status check and reporting (only if enabled)
            if status_check_interval > 0 and current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                
                # Decay RX level if no audio received recently
                time_since_rx_audio = current_time - self.last_rx_audio_time
                if time_since_rx_audio > 1.0:  # 1 second timeout
                    self.rx_audio_level = int(self.rx_audio_level * 0.5)  # Fast decay
                    if self.rx_audio_level < 5:
                        self.rx_audio_level = 0

                # Decay TX level (Radio → Mumble) — AIOC noise floor can
                # keep the bar stuck at a low level via 0.7/0.3 smoothing
                if self.tx_audio_level > 0:
                    self.tx_audio_level = int(self.tx_audio_level * 0.5)
                    if self.tx_audio_level < 3:
                        self.tx_audio_level = 0
                
                # Audio stream health — let plugin check its own watchdog
                if self.th9800_plugin:
                    self.th9800_plugin.check_watchdog()
            
            # DarkIce health check (every 10s — pgrep spawns a process)
            if (self._darkice_was_running and
                    self.config.ENABLE_STREAM_OUTPUT and
                    current_time - self._last_darkice_check > 10):
                self._last_darkice_check = current_time
                pid = self._find_darkice_pid()
                if not pid:
                    print("\n\u26a0 DarkIce has stopped — restarting...")
                    self._restart_darkice()
                elif pid != self._darkice_pid:
                    self._darkice_pid = pid  # PID changed (external restart)

            # Charger relay schedule check
            # When manually overridden, wait until the schedule's *next* transition
            # (i.e. should_on flips to match the manual state) before resuming auto control
            if self.relay_charger:
                should_on = self._charger_should_be_on()
                if self._charger_manual:
                    # Manual override active — clear it once schedule agrees with current state
                    if should_on == self.relay_charger_on:
                        self._charger_manual = False
                elif should_on != self.relay_charger_on:
                    self.relay_charger.set_state(should_on)
                    self.relay_charger_on = should_on
                    on_str = str(self.config.RELAY_CHARGER_ON_TIME)
                    off_str = str(self.config.RELAY_CHARGER_OFF_TIME)
                    if should_on:
                        print(f"\n[Charger] CHARGING started (schedule {on_str}-{off_str})")
                    else:
                        print(f"\n[Charger] DRAINING started (schedule {on_str}-{off_str})")
                    self._trace_events.append((time.monotonic(), 'relay_charger', 'on' if should_on else 'off'))

            # SDR loopback watchdog checks
            if self.sdr_plugin and self.sdr_plugin.tuner1_enabled:
                self.sdr_plugin.check_watchdog()
            if self.sdr_plugin and self.sdr_plugin.tuner2_enabled:
                self.sdr_plugin.check_watchdog()

            # Mumble Server health checks (every ~10 seconds)
            if not hasattr(self, '_ms_health_tick'):
                self._ms_health_tick = 0
            self._ms_health_tick += 1
            if self._ms_health_tick >= 100:  # ~10s at 0.1s sleep
                self._ms_health_tick = 0
                if self.mumble_server_1:
                    self.mumble_server_1.check_health()
                if self.mumble_server_2:
                    self.mumble_server_2.check_health()

            # Mumble client connection state change detection (debounced)
            mumble_alive = bool(self.mumble and self.mumble.is_alive()) if self.mumble else False
            if not hasattr(self, '_mumble_client_was_connected'):
                self._mumble_client_was_connected = mumble_alive
                self._mumble_state_since = time.monotonic()
            now_mono = time.monotonic()
            if mumble_alive != self._mumble_client_was_connected:
                # State changed — wait 3s before confirming (avoids flicker)
                if not hasattr(self, '_mumble_pending_state'):
                    self._mumble_pending_state = mumble_alive
                    self._mumble_state_since = now_mono
                elif self._mumble_pending_state != mumble_alive:
                    # Flickered back — cancel pending change
                    del self._mumble_pending_state
                elif now_mono - self._mumble_state_since >= 3.0:
                    # Stable for 3s — confirm the change
                    self._mumble_client_was_connected = mumble_alive
                    del self._mumble_pending_state
                    srv = getattr(self.config, 'MUMBLE_SERVER', '?')
                    port = getattr(self.config, 'MUMBLE_PORT', 64738)
                    if mumble_alive:
                        print(f"\n[Mumble] Connected to {srv}:{port}")
                    else:
                        print(f"\n[Mumble] Disconnected from {srv}:{port}")
            elif hasattr(self, '_mumble_pending_state'):
                # State went back to previous — cancel pending
                del self._mumble_pending_state

            time.sleep(0.1)
          except BaseException as _status_err:
            # Log crash so it's visible in the trace, then keep running.
            try:
                self._trace_events.append((time.monotonic(), 'STATUS_CRASH', str(_status_err)))
            except Exception:
                pass  # trace deque itself failed — don't let that kill us
            time.sleep(1)

    def run(self):
        """Main application"""
        # Set up rolling log file (daily rotation, keeps LOG_FILE_DAYS days)
        log_file = None
        try:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            # Open today's log file (append mode)
            import datetime as _dt
            today = _dt.date.today().strftime('%Y-%m-%d')
            log_path = os.path.join(log_dir, f'gateway-{today}.log')
            log_file = open(log_path, 'a', encoding='utf-8')
            # Clean up old log files beyond retention
            keep_days = int(getattr(self.config, 'LOG_FILE_DAYS', 7))
            import glob
            for old_log in sorted(glob.glob(os.path.join(log_dir, 'gateway-*.log'))):
                try:
                    fname = os.path.basename(old_log)
                    date_str = fname.replace('gateway-', '').replace('.log', '')
                    log_date = _dt.datetime.strptime(date_str, '%Y-%m-%d').date()
                    if (_dt.date.today() - log_date).days > keep_days:
                        os.remove(old_log)
                except (ValueError, OSError):
                    pass
            self._log_dir = log_dir
        except Exception as e:
            print(f"  [Warning] Could not set up log file: {e}", file=sys.stderr)

        # Clean up stale /tmp log files from previous runs
        for tmp_log in ['/tmp/th9800_cat.log', '/tmp/darkice.log', '/tmp/ffmpeg.log']:
            try:
                if os.path.exists(tmp_log):
                    sz = os.path.getsize(tmp_log)
                    if sz > 10 * 1024 * 1024:  # >10MB, truncate
                        open(tmp_log, 'w').close()
            except Exception:
                pass

        # Install stdout/stderr wrapper early so ALL messages get timestamps
        buf_lines = int(getattr(self.config, 'LOG_BUFFER_LINES', 2000))
        self._status_writer = LogWriter(
            sys.stdout, buffer_lines=buf_lines, log_file=log_file
        )
        sys.stdout = self._status_writer
        self._orig_stderr = sys.stderr
        sys.stderr = self._status_writer

        # Pre-populate log buffer with start.sh output so web /logs shows full boot sequence
        try:
            startup_log = '/tmp/gateway_startup.log'
            if os.path.exists(startup_log):
                with open(startup_log, 'r') as f:
                    for line in f:
                        line = line.rstrip('\n')
                        if line:
                            self._status_writer._append_log(line)
        except Exception:
            pass

        print("=" * 60)
        print("Radio Gateway")
        print(f"Version {__version__}")
        print("=" * 60)
        print()
        
        # AIOC init now handled by TH9800Plugin in setup_audio()

        # Initialize Audio
        if not self.setup_audio():
            self.cleanup()
            return False
        
        # Initialize Mumble
        self._mumble_ok = self.setup_mumble()
        mumble_ok = self._mumble_ok
        if not mumble_ok:
            print("\n  ⚠ Mumble connection failed — continuing without Mumble.")
            print("  Radio audio, SDR, and other features will still work.")

        # Redirect OS-level fd 2 (C stderr) through a pipe that feeds back
        # into the LogWriter.  This catches output from external
        # processes (murmurd, Mumble GUI Qt warnings) that share our terminal.
        try:
            self._stderr_pipe_r, self._stderr_pipe_w = os.pipe()
            os.dup2(self._stderr_pipe_w, 2)
            os.close(self._stderr_pipe_w)
            def _stderr_reader():
                buf = b''
                while self.running:
                    try:
                        data = os.read(self._stderr_pipe_r, 4096)
                        if not data:
                            break
                        buf += data
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            text = line.decode('utf-8', errors='replace').rstrip()
                            if text:
                                self._status_writer.write(text + '\n')
                                self._status_writer.flush()
                    except OSError:
                        break
            self._stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
            self._stderr_thread.start()
        except Exception:
            pass  # Non-fatal — stderr just won't be captured

        # Start audio transmit thread
        self._tx_thread = threading.Thread(target=self.audio_transmit_loop, daemon=True)
        self._tx_thread.start()

        # Start status monitor thread (handles PTT timeout and status reporting)
        self._status_thread = threading.Thread(target=self.status_monitor_loop, daemon=True)
        self._status_thread.start()

        # Start Bus Manager (additional busses from routing config)
        try:
            from bus_manager import BusManager
            self.bus_manager = BusManager(self)
            self.bus_manager.start()
            # Cache per-bus stream flags (pcm/mp3/vad), sink connections, and listen bus ID
            self._bus_stream_flags = self.bus_manager.get_bus_stream_flags()
            self._bus_sinks = self.bus_manager.get_bus_sinks()
            self._listen_bus_id = self.bus_manager.get_listen_bus_id()
        except Exception as e:
            print(f"  [BusManager] Failed to start: {e}")
            self.bus_manager = None
        if not hasattr(self, '_bus_stream_flags'):
            self._bus_stream_flags = {}
        if not hasattr(self, '_bus_sinks'):
            self._bus_sinks = {}
        if not hasattr(self, '_listen_bus_id'):
            self._listen_bus_id = 'listen'

        # Start Automation Engine if enabled
        if getattr(self.config, 'ENABLE_AUTOMATION', False):
            try:
                from radio_automation import AutomationEngine
                self.automation_engine = AutomationEngine(self)
                self.automation_engine.start()
            except Exception as e:
                print(f"[Automation] Failed to start: {e}")
                self.automation_engine = None

        # Main loop
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
        finally:
            self.cleanup()
    
    def _watchdog_trace_loop(self):
        """Low-fidelity long-running trace.  Samples every 5s, flushes to disk every 60s.
        Designed to run overnight to diagnose freezes."""
        import os, datetime, resource
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'watchdog_trace.txt')
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        SAMPLE_INTERVAL = 5     # seconds between samples
        FLUSH_INTERVAL = 60     # seconds between disk writes
        buffer = []
        last_flush = time.monotonic()
        prev_tick = self._tx_loop_tick

        # Write/append header
        hdr = ("timestamp\tuptime_s\ttx_ticks\ttick_rate"
               "\tth_tx\tth_stat\tth_kb\tth_aioc\tth_sdr1\tth_sdr2\tth_remote\tth_announce"
               "\tmumble"
               "\ten_aioc\ten_sdr1\ten_sdr2\ten_remote\ten_announce"
               "\tmu_tx\tmu_rx\tmu_sdr1\tmu_sdr2\tmu_remote\tmu_announce\tmu_spk"
               "\tlvl_tx\tlvl_rx\tlvl_sdr1\tlvl_sdr2\tlvl_sv"
               "\tq_aioc\tq_sdr1\tq_sdr2"
               "\tptt\tvad\trebro_ptt\trss_mb\n")
        import platform
        try:
            with open(out_path, 'a') as f:
                f.write(f"\n# Watchdog started {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        f"  v{__version__}"
                        f"  {platform.node()} {platform.system()} {platform.release()} {platform.machine()}"
                        f"  py{platform.python_version()}\n")
                f.write(hdr)
        except Exception:
            pass

        while self._watchdog_active and self.running:
            time.sleep(SAMPLE_INTERVAL)
            if not self._watchdog_active:
                break

            now_mono = time.monotonic()
            uptime = now_mono - self._watchdog_t0

            # Tick rate (ticks per second since last sample)
            cur_tick = self._tx_loop_tick
            tick_rate = (cur_tick - prev_tick) / SAMPLE_INTERVAL
            prev_tick = cur_tick

            # Thread alive checks
            def _alive(t):
                return 1 if (t and t.is_alive()) else 0

            th_tx = _alive(self._tx_thread)
            th_stat = _alive(self._status_thread)
            th_kb = _alive(self._keyboard_thread)
            th_aioc = _alive(self.radio_source._reader_thread if self.radio_source and hasattr(self.radio_source, '_reader_thread') else None)
            th_sdr1 = _alive(None)
            th_sdr2 = _alive(None)
            th_remote = _alive(self.remote_audio_source._reader_thread if self.remote_audio_source and hasattr(self.remote_audio_source, '_reader_thread') else None)
            th_announce = _alive(self.announce_input_source._reader_thread if self.announce_input_source and hasattr(self.announce_input_source, '_reader_thread') else None)

            # Mumble connection
            mumble_ok = 0
            try:
                if self.mumble and self.mumble.is_alive():
                    mumble_ok = 1
            except Exception:
                pass

            # Source enabled flags
            en_aioc = 1 if (self.radio_source and self.radio_source.enabled) else 0
            en_sdr1 = 1 if (self.sdr_plugin and self.sdr_plugin.tuner1_enabled) else 0
            en_sdr2 = 1 if (self.sdr_plugin and self.sdr_plugin.tuner2_enabled) else 0
            en_remote = 1 if (self.remote_audio_source and self.remote_audio_source.enabled) else 0
            en_announce = 1 if (self.announce_input_source and self.announce_input_source.enabled) else 0

            # Mute flags
            mu_tx = 1 if self.tx_muted else 0
            mu_rx = 1 if self.rx_muted else 0
            mu_sdr1 = 1 if self.sdr_muted else 0
            mu_sdr2 = 1 if self.sdr2_muted else 0
            mu_remote = 1 if self.remote_audio_muted else 0
            mu_announce = 1 if self.announce_input_muted else 0
            mu_spk = 1 if self.speaker_muted else 0

            # Audio levels
            lvl_tx = self.tx_audio_level
            lvl_rx = self.rx_audio_level
            lvl_sdr1 = self.sdr_audio_level
            lvl_sdr2 = self.sdr2_audio_level
            lvl_sv = self.sv_audio_level

            # Queue depths
            def _qsize(src):
                try:
                    if src and hasattr(src, '_chunk_queue'):
                        return src._chunk_queue.qsize()
                except Exception:
                    pass
                return -1

            q_aioc = _qsize(self.radio_source)
            q_sdr1 = 0
            q_sdr2 = 0

            # PTT / VAD / rebroadcast
            ptt = 1 if self.ptt_active else 0
            vad = 1 if self.vad_active else 0
            rebro = 1 if self._rebroadcast_ptt_active else 0

            # RSS memory (KB → MB)
            try:
                rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            except Exception:
                rss_mb = -1

            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = (f"{ts}\t{uptime:.0f}\t{cur_tick}\t{tick_rate:.1f}"
                    f"\t{th_tx}\t{th_stat}\t{th_kb}\t{th_aioc}\t{th_sdr1}\t{th_sdr2}\t{th_remote}\t{th_announce}"
                    f"\t{mumble_ok}"
                    f"\t{en_aioc}\t{en_sdr1}\t{en_sdr2}\t{en_remote}\t{en_announce}"
                    f"\t{mu_tx}\t{mu_rx}\t{mu_sdr1}\t{mu_sdr2}\t{mu_remote}\t{mu_announce}\t{mu_spk}"
                    f"\t{lvl_tx}\t{lvl_rx}\t{lvl_sdr1}\t{lvl_sdr2}\t{lvl_sv}"
                    f"\t{q_aioc}\t{q_sdr1}\t{q_sdr2}"
                    f"\t{ptt}\t{vad}\t{rebro}\t{rss_mb:.1f}\n")
            buffer.append(line)

            # Flush to disk periodically
            if now_mono - last_flush >= FLUSH_INTERVAL and buffer:
                try:
                    with open(out_path, 'a') as f:
                        f.writelines(buffer)
                    buffer.clear()
                    last_flush = now_mono
                except Exception:
                    pass

        # Final flush on stop
        if buffer:
            try:
                with open(out_path, 'a') as f:
                    f.write(f"# Watchdog stopped {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.writelines(buffer)
            except Exception:
                pass

    def _dump_audio_trace(self):
        """Write audio trace to tools/audio_trace.txt on shutdown."""
        trace = list(self._audio_trace)
        if not trace:
            return
        import os, statistics
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'audio_trace.txt')
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
        except Exception:
            pass

        # Column indices
        T, DT, SQ, SSB, AQ, ASB, MGOT, MSRC, MMS, SBLK, ABLK, \
            OUTCOME, MUMMS, SPKOK, SPKQD, DRMS, DLEN, MXST, \
            SQ2, SSB2, SPREBUF, S2PREBUF, REBRO, SVMS, SVSENT, \
            SDR1_DISC, SDR1_SBA, SDR1_OVF, SDR1_DROP, \
            AIOC_DISC, AIOC_SBA, AIOC_OVF, AIOC_DROP, \
            OUT_DISC, \
            KV4P_RXF, KV4P_RXB, KV4P_QDROP, KV4P_SBB, KV4P_SBA, \
            KV4P_GOT, KV4P_RMS, KV4P_QLEN, KV4P_DECERR, \
            KV4P_TXF, KV4P_TXDROP, KV4P_TXRMS, KV4P_TXERR, KV4P_TXANN, \
            SDR2_DISC, SDR2_SBA = range(50)

        with open(out_path, 'w') as f:
            dur = trace[-1][T] - trace[0][T] if len(trace) > 1 else 0
            f.write(f"Audio Trace: {len(trace)} ticks, {dur:.1f}s\n")
            f.write(f"{'='*90}\n\n")

            # ── System info ──
            import platform
            sdr_mode = "SDRPlugin" if self.sdr_plugin else "none"
            f.write("SYSTEM\n")
            f.write(f"  version={__version__}\n")
            f.write(f"  os={platform.system()} {platform.release()} arch={platform.machine()}\n")
            f.write(f"  python={platform.python_version()} sdr_mode={sdr_mode}\n")
            f.write(f"  host={platform.node()}\n\n")

            # ── Summary statistics ──
            dts = [r[DT] for r in trace]
            mixer_got = sum(1 for r in trace if r[MGOT])
            mixer_none = len(trace) - mixer_got
            mixer_ms = [r[MMS] for r in trace]
            sdr_blocked = [r[SBLK] for r in trace if r[SBLK] > 0]
            aioc_blocked = [r[ABLK] for r in trace if r[ABLK] > 0]

            f.write("TICK TIMING (target: 50.0ms)\n")
            f.write(f"  count={len(dts)}  mean={statistics.mean(dts):.1f}ms  "
                    f"stdev={statistics.stdev(dts):.1f}ms  min={min(dts):.1f}ms  max={max(dts):.1f}ms\n")
            over_60 = sum(1 for d in dts if d > 60)
            over_80 = sum(1 for d in dts if d > 80)
            over_100 = sum(1 for d in dts if d > 100)
            f.write(f"  >60ms: {over_60}  >80ms: {over_80}  >100ms: {over_100}\n\n")

            f.write("MIXER OUTPUT\n")
            f.write(f"  audio: {mixer_got} ({100*mixer_got/len(trace):.1f}%)  "
                    f"silence: {mixer_none} ({100*mixer_none/len(trace):.1f}%)\n")
            f.write(f"  call time: mean={statistics.mean(mixer_ms):.2f}ms  max={max(mixer_ms):.2f}ms\n\n")

            # Source breakdown
            src_counts = {}
            for r in trace:
                key = r[MSRC] if r[MSRC] else '(none)'
                src_counts[key] = src_counts.get(key, 0) + 1
            f.write("SOURCE BREAKDOWN\n")
            for src, cnt in sorted(src_counts.items(), key=lambda x: -x[1]):
                f.write(f"  {src}: {cnt} ({100*cnt/len(trace):.1f}%)\n")
            f.write("\n")

            # ── Downstream outcome ──
            outcome_counts = {}
            for r in trace:
                o = r[OUTCOME]
                outcome_counts[o] = outcome_counts.get(o, 0) + 1
            f.write("DOWNSTREAM OUTCOME\n")
            for o, cnt in sorted(outcome_counts.items(), key=lambda x: -x[1]):
                f.write(f"  {o}: {cnt} ({100*cnt/len(trace):.1f}%)\n")
            f.write("\n")

            # Mumble send timing
            mumble_ms = [r[MUMMS] for r in trace if r[OUTCOME] == 'sent' and r[MUMMS] > 0]
            if mumble_ms:
                f.write(f"MUMBLE add_sound()\n")
                f.write(f"  count={len(mumble_ms)}  mean={statistics.mean(mumble_ms):.2f}ms  max={max(mumble_ms):.2f}ms\n\n")

            sv_ms_vals = [r[SVMS] for r in trace if len(r) > SVMS and r[SVSENT] > 0]
            if sv_ms_vals:
                f.write(f"REMOTE AUDIO SERVER send_audio()\n")
                f.write(f"  ticks with sends={len(sv_ms_vals)}/{len(trace)}  "
                        f"mean={statistics.mean(sv_ms_vals):.2f}ms  max={max(sv_ms_vals):.2f}ms\n")
                sv_slow = sum(1 for v in sv_ms_vals if v > 5.0)
                sv_vslow = sum(1 for v in sv_ms_vals if v > 50.0)
                f.write(f"  >5ms: {sv_slow}  >50ms: {sv_vslow}\n\n")

            # Speaker queue
            spk_qds = [r[SPKQD] for r in trace if r[SPKQD] >= 0]
            if spk_qds:
                f.write(f"SPEAKER QUEUE DEPTH (at enqueue time)\n")
                f.write(f"  min={min(spk_qds)}  mean={statistics.mean(spk_qds):.1f}  max={max(spk_qds)}\n")
                spk_full = sum(1 for q in spk_qds if q >= 8)
                f.write(f"  full (>=8): {spk_full} ({100*spk_full/len(spk_qds):.1f}%)\n\n")

            # Data RMS
            rms_vals = [r[DRMS] for r in trace if r[DRMS] > 0]
            if rms_vals:
                f.write(f"DATA RMS (non-zero only)\n")
                f.write(f"  count={len(rms_vals)}/{len(trace)}  mean={statistics.mean(rms_vals):.0f}  "
                        f"min={min(rms_vals):.0f}  max={max(rms_vals):.0f}\n")
                zero_rms = sum(1 for r in trace if r[OUTCOME] == 'sent' and r[DRMS] == 0)
                f.write(f"  sent with RMS=0 (silence): {zero_rms}\n\n")

            # Data length
            dlens = [r[DLEN] for r in trace if r[DLEN] > 0]
            if dlens:
                expected = self.config.AUDIO_CHUNK_SIZE * 2  # 4800 bytes for 50ms mono
                wrong = sum(1 for d in dlens if d != expected)
                f.write(f"DATA LENGTH (expected: {expected} bytes = {self.config.AUDIO_CHUNK_SIZE} frames)\n")
                f.write(f"  count={len(dlens)}/{len(trace)}  min={min(dlens)}  max={max(dlens)}\n")
                if wrong:
                    f.write(f"  *** WRONG SIZE: {wrong} chunks ({100*wrong/len(dlens):.1f}%) ***\n")
                    sizes = {}
                    for d in dlens:
                        sizes[d] = sizes.get(d, 0) + 1
                    f.write(f"  size distribution: {dict(sorted(sizes.items()))}\n")
                f.write("\n")

            if sdr_blocked:
                f.write(f"SDR BLOB FETCH (blocked {len(sdr_blocked)}/{len(trace)} ticks)\n")
                f.write(f"  mean={statistics.mean(sdr_blocked):.1f}ms  max={max(sdr_blocked):.1f}ms\n\n")
            else:
                f.write("SDR BLOB FETCH: never blocked\n\n")

            if aioc_blocked:
                f.write(f"AIOC BLOB FETCH (blocked {len(aioc_blocked)}/{len(trace)} ticks)\n")
                f.write(f"  mean={statistics.mean(aioc_blocked):.1f}ms  max={max(aioc_blocked):.1f}ms\n\n")
            else:
                f.write("AIOC BLOB FETCH: never blocked\n\n")

            # SDR1 queue depth
            sq_vals = [r[SQ] for r in trace if r[SQ] >= 0]
            if sq_vals:
                f.write(f"SDR1 QUEUE DEPTH\n")
                f.write(f"  min={min(sq_vals)}  mean={statistics.mean(sq_vals):.1f}  max={max(sq_vals)}\n")
                n = len(sq_vals)
                q1 = statistics.mean(sq_vals[:n//4]) if n >= 4 else 0
                q4 = statistics.mean(sq_vals[-n//4:]) if n >= 4 else 0
                f.write(f"  first quarter={q1:.1f}  last quarter={q4:.1f}\n")
                pb_ticks = sum(1 for r in trace if len(r) > SPREBUF and r[SPREBUF])
                f.write(f"  prebuffering: {pb_ticks}/{len(trace)} ticks\n")
                plc_total = 0
                f.write(f"  PLC repeats: {plc_total} (gap concealment)\n\n")

            # SDR2 queue depth
            sq2_vals = [r[SQ2] for r in trace if len(r) > SQ2 and r[SQ2] >= 0]
            if sq2_vals:
                f.write(f"SDR2 QUEUE DEPTH\n")
                f.write(f"  min={min(sq2_vals)}  mean={statistics.mean(sq2_vals):.1f}  max={max(sq2_vals)}\n")
                pb2_ticks = sum(1 for r in trace if len(r) > S2PREBUF and r[S2PREBUF])
                f.write(f"  prebuffering: {pb2_ticks}/{len(trace)} ticks\n")
                plc2_total = 0
                f.write(f"  PLC repeats: {plc2_total} (gap concealment)\n\n")

            # AIOC queue depth
            aq_vals = [r[AQ] for r in trace if r[AQ] >= 0]
            if aq_vals:
                f.write(f"AIOC QUEUE DEPTH\n")
                f.write(f"  min={min(aq_vals)}  mean={statistics.mean(aq_vals):.1f}  max={max(aq_vals)}\n\n")

            # ── PortAudio callback health ──
            has_enhanced = len(trace[0]) > SDR1_DISC if trace else False
            if has_enhanced:
                # SDR1 callback stats (cumulative — use last tick's values)
                last = trace[-1]
                sdr1_ovf_total = last[SDR1_OVF] if last[SDR1_OVF] else 0
                sdr1_drop_total = last[SDR1_DROP] if last[SDR1_DROP] else 0
                aioc_ovf_total = last[AIOC_OVF] if last[AIOC_OVF] else 0
                aioc_drop_total = last[AIOC_DROP] if last[AIOC_DROP] else 0

                f.write("PORTAUDIO CALLBACK HEALTH\n")
                f.write(f"  SDR1: overflows={sdr1_ovf_total}  queue_drops={sdr1_drop_total}\n")
                f.write(f"  AIOC: overflows={aioc_ovf_total}  queue_drops={aioc_drop_total}\n")
                if sdr1_ovf_total or sdr1_drop_total:
                    f.write(f"  *** SDR1 callback issues detected — data may be lost ***\n")
                if aioc_ovf_total or aioc_drop_total:
                    f.write(f"  *** AIOC callback issues detected — data may be lost ***\n")
                f.write("\n")

                # Sample discontinuities
                sdr1_discs = [r[SDR1_DISC] for r in trace if r[SDR1_DISC] > 0]
                sdr2_discs = [r[SDR2_DISC] for r in trace if len(r) > SDR2_DISC and r[SDR2_DISC] > 0]
                aioc_discs = [r[AIOC_DISC] for r in trace if r[AIOC_DISC] > 0]

                f.write("SAMPLE DISCONTINUITIES (inter-chunk boundary jumps)\n")
                f.write("  (Large jumps between last sample of chunk N and first sample of chunk N+1 cause clicks)\n")
                if sdr1_discs:
                    big_jumps = [d for d in sdr1_discs if d > 1000]
                    huge_jumps = [d for d in sdr1_discs if d > 5000]
                    f.write(f"  SDR1: count={len(sdr1_discs)}/{len(trace)}  "
                            f"mean={statistics.mean(sdr1_discs):.0f}  max={max(sdr1_discs):.0f}  "
                            f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
                else:
                    f.write("  SDR1: no discontinuities (all chunks zero or no audio)\n")
                if sdr2_discs:
                    big_jumps = [d for d in sdr2_discs if d > 1000]
                    huge_jumps = [d for d in sdr2_discs if d > 5000]
                    f.write(f"  SDR2: count={len(sdr2_discs)}/{len(trace)}  "
                            f"mean={statistics.mean(sdr2_discs):.0f}  max={max(sdr2_discs):.0f}  "
                            f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
                else:
                    f.write("  SDR2: no discontinuities (all chunks zero or no audio)\n")
                if aioc_discs:
                    big_jumps = [d for d in aioc_discs if d > 1000]
                    huge_jumps = [d for d in aioc_discs if d > 5000]
                    f.write(f"  AIOC: count={len(aioc_discs)}/{len(trace)}  "
                            f"mean={statistics.mean(aioc_discs):.0f}  max={max(aioc_discs):.0f}  "
                            f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
                else:
                    f.write("  AIOC: no discontinuities (all chunks zero or no audio)\n")

                # Output-side discontinuities (after mixer — what Mumble actually gets)
                out_discs = [r[OUT_DISC] for r in trace if len(r) > OUT_DISC and r[OUT_DISC] > 0]
                if out_discs:
                    big_jumps = [d for d in out_discs if d > 1000]
                    huge_jumps = [d for d in out_discs if d > 5000]
                    f.write(f"  OUTPUT (mixer→Mumble): count={len(out_discs)}/{len(trace)}  "
                            f"mean={statistics.mean(out_discs):.0f}  max={max(out_discs):.0f}  "
                            f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
                    if huge_jumps:
                        f.write(f"  *** {len(huge_jumps)} output clicks detected (>5000 sample jump) ***\n")
                else:
                    f.write("  OUTPUT: no discontinuities\n")
                f.write("\n")

                # Sub-buffer after-serve levels
                sdr1_sba = [r[SDR1_SBA] for r in trace if r[SDR1_SBA] >= 0]
                aioc_sba = [r[AIOC_SBA] for r in trace if r[AIOC_SBA] >= 0]
                if sdr1_sba:
                    near_empty = sum(1 for s in sdr1_sba if s < self.config.AUDIO_CHUNK_SIZE * 2)
                    f.write(f"SDR1 SUB-BUFFER AFTER SERVE\n")
                    f.write(f"  min={min(sdr1_sba)}  mean={statistics.mean(sdr1_sba):.0f}  max={max(sdr1_sba)}\n")
                    f.write(f"  near-empty (<1 chunk): {near_empty}/{len(sdr1_sba)} "
                            f"({100*near_empty/len(sdr1_sba):.1f}%) — next tick may deplete\n\n")
                if aioc_sba:
                    near_empty = sum(1 for s in aioc_sba if s < self.config.AUDIO_CHUNK_SIZE * 2)
                    f.write(f"AIOC SUB-BUFFER AFTER SERVE\n")
                    f.write(f"  min={min(aioc_sba)}  mean={statistics.mean(aioc_sba):.0f}  max={max(aioc_sba)}\n")
                    f.write(f"  near-empty (<1 chunk): {near_empty}/{len(aioc_sba)} "
                            f"({100*near_empty/len(aioc_sba):.1f}%) — next tick may deplete\n\n")

            # ── Gap analysis ──
            gaps = []
            g = 0
            for r in trace:
                if not r[MGOT]:
                    g += 1
                else:
                    if g > 0:
                        gaps.append(g)
                    g = 0
            if g > 0:
                gaps.append(g)
            if gaps:
                gap_ms = [x * 50 for x in gaps]
                f.write(f"SILENCE GAPS (mixer): {len(gaps)} gaps\n")
                f.write(f"  sizes (ticks): {gaps[:50]}\n")
                f.write(f"  max gap: {max(gap_ms)}ms\n\n")
            else:
                f.write("SILENCE GAPS (mixer): none\n\n")

            # ── Mixer state summary ──
            has_state = any(r[MXST] for r in trace)
            if has_state:
                ducked_count = sum(1 for r in trace if r[MXST].get('dk', False))
                hold_count = sum(1 for r in trace if r[MXST].get('hold', False))
                pad_count = sum(1 for r in trace if r[MXST].get('pad', False))
                tOut_count = sum(1 for r in trace if r[MXST].get('tOut', False))
                ducks_count = sum(1 for r in trace if r[MXST].get('ducks', False))
                radio_sig_count = sum(1 for r in trace if r[MXST].get('radioSig', False))
                oaa_count = sum(1 for r in trace if r[MXST].get('oaa', False))
                n = len(trace)
                f.write("MIXER STATE\n")
                f.write(f"  ducked: {ducked_count}/{n} ({100*ducked_count/n:.1f}%)  "
                        f"hold_fired: {hold_count}/{n} ({100*hold_count/n:.1f}%)  "
                        f"padding: {pad_count}/{n} ({100*pad_count/n:.1f}%)\n")
                f.write(f"  trans_out: {tOut_count}/{n} ({100*tOut_count/n:.1f}%)  "
                        f"aioc_ducks: {ducks_count}/{n} ({100*ducks_count/n:.1f}%)  "
                        f"radio_signal: {radio_sig_count}/{n} ({100*radio_sig_count/n:.1f}%)\n")
                f.write(f"  other_audio_active: {oaa_count}/{n} ({100*oaa_count/n:.1f}%)\n")

                # Per-SDR state summary
                sdr_names = set()
                for r in trace:
                    sdr_names.update(r[MXST].get('sdrs', {}).keys())
                for sname in sorted(sdr_names):
                    s_ducked = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('ducked', False))
                    s_inc = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('inc', False))
                    s_sig = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('sig', False))
                    s_hold = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('hold', False))
                    s_sole = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('sole', False))
                    f.write(f"  {sname}: ducked={s_ducked}  included={s_inc}  signal={s_sig}  "
                            f"hold={s_hold}  sole_source={s_sole}\n")

                # Mute state (usually constant, just show if any were active)
                rx_m = sum(1 for r in trace if r[MXST].get('rx_m', False))
                gl_m = sum(1 for r in trace if r[MXST].get('gl_m', False))
                sp_m = sum(1 for r in trace if r[MXST].get('sp_m', False))
                mutes = []
                if rx_m: mutes.append(f"rx_muted={rx_m}")
                if gl_m: mutes.append(f"global_muted={gl_m}")
                if sp_m: mutes.append(f"speaker_muted={sp_m}")
                if mutes:
                    f.write(f"  mutes: {', '.join(mutes)}\n")
                f.write("\n")

            # ── Duck-release analysis ──
            # For each SDR: find every tick where the SDR went from ducked→not-ducked.
            # Check: did fade-in fire on the release tick (or next tick)?
            # Also check: was the queue depth reasonable at release?
            duck_release_events = []
            for i in range(1, len(trace)):
                prev_r = trace[i-1]
                curr_r = trace[i]
                if not (len(prev_r) > MXST and len(curr_r) > MXST):
                    continue
                prev_st = prev_r[MXST] or {}
                curr_st = curr_r[MXST] or {}
                for sname in sorted((prev_st.get('sdrs', {}) | curr_st.get('sdrs', {})).keys()):
                    prev_s = prev_st.get('sdrs', {}).get(sname, {})
                    curr_s = curr_st.get('sdrs', {}).get(sname, {})
                    if prev_s.get('ducked') and not curr_s.get('ducked'):
                        # Duck just released for this SDR
                        sdr_q = curr_r[SQ] if sname == 'SDR1' else (curr_r[SQ2] if len(curr_r) > SQ2 else -1)
                        fi_fired = curr_s.get('fi', False)
                        inc = curr_s.get('inc', False)
                        # Missing fade-in: included on release tick but prev_included was True
                        # (fade-in should always fire after a duck due to our reset fix)
                        missing_fi = inc and not fi_fired
                        duck_release_events.append({
                            'tick': i, 't': curr_r[T], 'sdr': sname,
                            'q': sdr_q, 'fi': fi_fired, 'inc': inc, 'missing_fi': missing_fi,
                        })

            if duck_release_events:
                f.write("DUCK RELEASE EVENTS\n")
                for ev in duck_release_events:
                    fi_str = 'fade-in=YES' if ev['fi'] else ('fade-in=MISSING!' if ev['missing_fi'] else 'fade-in=no(not-inc)')
                    f.write(f"  tick {ev['tick']:4d}  t={ev['t']:.3f}s  {ev['sdr']}  "
                            f"q={ev['q']}  inc={ev['inc']}  {fi_str}\n")
                missing = [ev for ev in duck_release_events if ev['missing_fi']]
                if missing:
                    f.write(f"  *** {len(missing)} duck release(s) WITHOUT fade-in — SDR resumed at full volume → click risk ***\n")
                else:
                    f.write(f"  All {len(duck_release_events)} duck release(s) had correct fade-in.\n")
                f.write("\n")
            else:
                f.write("DUCK RELEASE EVENTS: none (no duck→unduck transitions observed)\n\n")

            # ── Gap-stutter analysis ──
            gap_stutter_ticks = [
                (i, r) for i, r in enumerate(trace)
                if len(r) > MXST and r[MXST]
                and r[MXST].get('dk') and not r[MXST].get('ducks')
                and r[MXST].get('oaa') and r[MXST].get('nptt_none')
            ]
            if gap_stutter_ticks:
                f.write(f"GAP-STUTTER EVENTS (is_ducked=T, aioc_ducks=F, oaa=T, aioc_gap=T)\n")
                f.write(f"  *** {len(gap_stutter_ticks)} ticks where AIOC blob gap briefly un-ducked SDR ***\n")
                f.write(f"  These are the cause of SDR stutter during AIOC transmission.\n")
                f.write(f"  First occurrence: tick {gap_stutter_ticks[0][0]}  t={gap_stutter_ticks[0][1][0]:.3f}s\n")
                # Show run-lengths (how many consecutive gap-stutter ticks)
                runs = []
                run_start = gap_stutter_ticks[0][0]
                run_len = 1
                for k in range(1, len(gap_stutter_ticks)):
                    if gap_stutter_ticks[k][0] == gap_stutter_ticks[k-1][0] + 1:
                        run_len += 1
                    else:
                        runs.append((run_start, run_len))
                        run_start = gap_stutter_ticks[k][0]
                        run_len = 1
                runs.append((run_start, run_len))
                f.write(f"  Gap bursts (tick, length): {runs[:20]}\n")
                f.write(f"  Total gap-stutter ticks: {len(gap_stutter_ticks)} (~{len(gap_stutter_ticks)*50}ms of SDR bleed-through)\n\n")
            else:
                f.write("GAP-STUTTER EVENTS: none detected\n\n")

            # ── Rebroadcast summary ──
            rebro_vals = [r[REBRO] for r in trace if len(r) > REBRO and r[REBRO]]
            if rebro_vals:
                n = len(trace)
                r_sig = sum(1 for v in rebro_vals if v == 'sig')
                r_hold = sum(1 for v in rebro_vals if v == 'hold')
                r_idle = sum(1 for v in rebro_vals if v == 'idle')
                f.write("SDR REBROADCAST\n")
                f.write(f"  active: {len(rebro_vals)}/{n} ticks  "
                        f"sig={r_sig} ({100*r_sig/n:.1f}%)  "
                        f"hold={r_hold} ({100*r_hold/n:.1f}%)  "
                        f"idle={r_idle} ({100*r_idle/n:.1f}%)\n\n")

            # ── Per-tick detail (first 200 + any anomalies) ──
            #
            # Mixer state column legend:
            #   D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active N=aioc_gap(nptt_none)
            #   Per SDR: D=ducked S=signal H=hold X=sole .=excluded I=included(no signal)
            #   GAP-STUTTER: D=True, A=False, O=True, N=True → is_ducked but AIOC gap un-ducked SDR
            def _fmt_mxst(st):
                """Format mixer state dict into compact string."""
                if not st:
                    return ''
                flags = ''
                flags += 'D' if st.get('dk') else '-'
                flags += 'H' if st.get('hold') else '-'
                flags += 'P' if st.get('pad') else '-'
                flags += 'T' if st.get('tOut') else '-'
                flags += 'A' if st.get('ducks') else '-'
                flags += 'R' if st.get('radioSig') else '-'
                flags += 'O' if st.get('oaa') else '-'
                flags += 'N' if st.get('nptt_none') else '-'
                flags += 'I' if st.get('ri') else '-'
                sdrs = st.get('sdrs', {})
                for sname in sorted(sdrs.keys()):
                    s = sdrs[sname]
                    flags += ' '
                    if s.get('ducked'):
                        flags += 'D'
                    elif s.get('fi'):
                        flags += 'F'  # fade-in fired (first inclusion after silence/duck)
                    elif s.get('fo'):
                        flags += 'O'  # fade-out fired (last frame before going silent)
                    elif s.get('inc'):
                        if s.get('sig'):
                            flags += 'S'
                        elif s.get('hold'):
                            flags += 'H'
                        elif s.get('sole'):
                            flags += 'X'
                        else:
                            flags += 'I'
                    else:
                        flags += '.'
                return flags

            # ── Reader blob delivery intervals ──
            for src_name, src_obj in [('SDR1', self.sdr_plugin.get_tuner(1) if self.sdr_plugin else None), ('SDR2', self.sdr_plugin.get_tuner(2) if self.sdr_plugin else None)]:
                if src_obj and getattr(src_obj, '_blob_times', None):
                    btimes = list(src_obj._blob_times)
                    if len(btimes) > 1:
                        intervals = [(btimes[k+1] - btimes[k]) * 1000 for k in range(len(btimes)-1)]
                        f.write(f"\n{src_name} READER BLOB DELIVERY INTERVALS ({len(intervals)} gaps)\n")
                        f.write(f"  mean={statistics.mean(intervals):.0f}ms  "
                                f"stdev={statistics.stdev(intervals):.0f}ms  "
                                f"min={min(intervals):.0f}ms  max={max(intervals):.0f}ms\n")
                        late = [iv for iv in intervals if iv > 500]
                        if late:
                            f.write(f"  >500ms stalls: {len(late)} — max={max(late):.0f}ms\n")
                    else:
                        f.write(f"\n{src_name} READER BLOB DELIVERY: too few samples\n")

            f.write(f"\n{'='*140}\n")
            f.write("PER-TICK DETAIL (all ticks; * = anomaly)\n")
            f.write(f"{'='*140}\n")
            f.write("  State: D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active N=aioc_gap I=reduck_inhibit\n")
            f.write("  * GAP-STUTTER tick: D=T A=F O=T N=T → is_ducked but AIOC blob gap caused SDR to briefly un-duck\n")
            f.write("  SDR:   D=ducked F=fade-in(first-inc) O=fade-out(going-silent) S=signal H=hold_inc X=sole_src I=inc(other) .=excluded\n")
            f.write("  * MISSING-FADE-IN tick: SDR included at duck-release without fade-in → click risk\n")
            f.write("  PB: B=prebuffering (waiting to rebuild cushion) .=normal\n")
            f.write("  RB: sig=rebroadcast sending  hold=PTT hold  idle=on but no signal\n")
            f.write("  s1_disc/s2_disc/a_disc/o_disc: sample discontinuity at chunk boundary (abs delta, >5000=click)\n")
            f.write("  s1_sba/a_sba: sub-buffer bytes remaining AFTER serving this chunk\n")
            f.write("  kv_txf: TX Opus frames sent | kv_txdrop: TX PCM bytes dropped (partial frame) | kv_txrms: TX input RMS | kv_ann: TX silenced by PTT settle delay\n\n")
            _missing_fi_ticks = {ev['tick'] for ev in duck_release_events if ev['missing_fi']}
            _duck_release_ticks = {ev['tick'] for ev in duck_release_events}

            hdr = (f"{'tick':>6} {'t(s)':>7} {'dt':>6} "
                   f"{'s1_q':>4} {'s1_sb':>6} {'s1_sba':>6} {'s2_q':>4} {'s2_sb':>6} {'pb':>2} "
                   f"{'aioc_q':>6} {'aioc_sb':>7} {'a_sba':>6} {'mixer':>5} {'mix_ms':>6} "
                   f"{'outcome':>10} {'m_ms':>5} {'spk_q':>5} {'rms':>7} {'dlen':>5} "
                   f"{'sv_ms':>6} {'sv#':>3} "
                   f"{'s1_disc':>7} {'s2_disc':>7} {'a_disc':>7} {'o_disc':>7} "
                   f"{'kv_rxf':>6} {'kv_rxB':>6} {'kv_qd':>5} {'kv_sbb':>7} {'kv_sba':>7} {'kv_got':>6} {'kv_rms':>7} {'kv_q':>4} "
                   f"{'kv_txf':>6} {'kv_txdrop':>9} {'kv_txrms':>8} {'kv_txerr':>8} {'kv_ann':>6} "
                   f"{'sources':>14} {'state':>14} {'rb':>4}\n")
            f.write(hdr)
            f.write('-' * len(hdr) + '\n')
            for i, r in enumerate(trace):
                expected_len = self.config.AUDIO_CHUNK_SIZE * 2
                _has_enh = len(r) > SDR1_DISC
                _st = r[MXST] if len(r) > MXST and r[MXST] else {}
                # Gap-stutter event: is_ducked=True but aioc_ducks_sdrs=False because
                # AIOC had a blob gap this tick (nptt_none=True) — SDR briefly un-ducked
                _gap_stutter = (_st.get('dk') and not _st.get('ducks')
                                and _st.get('oaa') and _st.get('nptt_none'))
                # SDR queue unexpectedly large: means get_audio() was not draining
                # it during a duck, so stale buffered audio will play at release.
                _sdr_q_spike = r[SQ] > 8 or (len(r) > SQ2 and r[SQ2] > 8)
                is_anomaly = (r[DT] > 80 or not r[MGOT] or r[MMS] > 20
                              or r[OUTCOME] not in ('sent', 'mix')
                              or r[MUMMS] > 5 or r[SPKQD] >= 7 or r[DRMS] == 0
                              or (r[DLEN] > 0 and r[DLEN] != expected_len)
                              or (len(r) > SPREBUF and (r[SPREBUF] or r[S2PREBUF]))
                              or (len(r) > SVMS and r[SVMS] > 5.0)
                              or (_has_enh and r[SDR1_DISC] > 5000)
                              or (_has_enh and r[AIOC_DISC] > 5000)
                              or (len(r) > OUT_DISC and r[OUT_DISC] > 5000)
                              or (len(r) > SDR2_DISC and r[SDR2_DISC] > 5000)
                              or _gap_stutter
                              or i in _missing_fi_ticks
                              or _sdr_q_spike)
                flag = '*' if is_anomaly else ' '
                st = _fmt_mxst(r[MXST]) if len(r) > MXST else ''
                sq2 = r[SQ2] if len(r) > SQ2 else -1
                ssb2 = r[SSB2] if len(r) > SSB2 else -1
                pb1 = 'B' if (len(r) > SPREBUF and r[SPREBUF]) else '.'
                pb2 = 'B' if (len(r) > S2PREBUF and r[S2PREBUF]) else '.'
                rb = r[REBRO] if len(r) > REBRO else ''
                sv_ms = r[SVMS] if len(r) > SVMS else 0.0
                sv_n = r[SVSENT] if len(r) > SVSENT else 0
                s1_disc = r[SDR1_DISC] if _has_enh else 0.0
                s1_sba = r[SDR1_SBA] if _has_enh else -1
                a_disc = r[AIOC_DISC] if _has_enh else 0.0
                a_sba = r[AIOC_SBA] if _has_enh else -1
                o_disc = r[OUT_DISC] if (len(r) > OUT_DISC) else 0.0
                s2_disc = r[SDR2_DISC] if len(r) > SDR2_DISC else 0.0
                _kv = len(r) > KV4P_RXF
                kv_rxf = r[KV4P_RXF] if _kv else 0
                kv_rxB = r[KV4P_RXB] if _kv else 0
                kv_qd = r[KV4P_QDROP] if _kv else 0
                kv_sbb = r[KV4P_SBB] if _kv else 0
                kv_sba = r[KV4P_SBA] if _kv else 0
                kv_got = r[KV4P_GOT] if _kv else False
                kv_rms = r[KV4P_RMS] if _kv else 0.0
                kv_q = r[KV4P_QLEN] if _kv else 0
                _kv_tx = len(r) > KV4P_TXF
                kv_txf = r[KV4P_TXF] if _kv_tx else 0
                kv_txdrop = r[KV4P_TXDROP] if _kv_tx else 0
                kv_txrms = r[KV4P_TXRMS] if _kv_tx else 0.0
                kv_txerr = r[KV4P_TXERR] if _kv_tx else 0
                kv_ann = 'Y' if (_kv_tx and r[KV4P_TXANN]) else '.'
                f.write(f"{i:>5}{flag} {r[T]:7.3f} {r[DT]:6.1f} "
                        f"{r[SQ]:4} {r[SSB]:6} {s1_sba:6} {sq2:4} {ssb2:6} {pb1}{pb2} "
                        f"{r[AQ]:6} {r[ASB]:7} {a_sba:6} {'audio' if r[MGOT] else 'NONE':>5} "
                        f"{r[MMS]:6.1f} "
                        f"{r[OUTCOME]:>10} {r[MUMMS]:5.1f} {r[SPKQD]:5} {r[DRMS]:7.0f} "
                        f"{r[DLEN]:5} {sv_ms:6.1f} {sv_n:3} "
                        f"{s1_disc:7.0f} {s2_disc:7.0f} {a_disc:7.0f} {o_disc:7.0f} "
                        f"{kv_rxf:6} {kv_rxB:6} {kv_qd:5} {kv_sbb:7} {kv_sba:7} {'yes' if kv_got else 'no':>6} {kv_rms:7.0f} {kv_q:4} "
                        f"{kv_txf:6} {kv_txdrop:9} {kv_txrms:8.0f} {kv_txerr:8} {kv_ann:>6} "
                        f"{r[MSRC]:>14} {st} {rb:>4}\n")

            # ── KV4P summary ──
            kv4p_ticks = [r for r in trace if len(r) > KV4P_RXF]
            if kv4p_ticks:
                kv_got = sum(1 for r in kv4p_ticks if r[KV4P_GOT])
                kv_none = len(kv4p_ticks) - kv_got
                kv_drops = sum(r[KV4P_QDROP] for r in kv4p_ticks)
                kv_rxf_total = sum(r[KV4P_RXF] for r in kv4p_ticks)
                kv_rxB_total = sum(r[KV4P_RXB] for r in kv4p_ticks)
                kv_sbb_vals = [r[KV4P_SBB] for r in kv4p_ticks]
                kv_rms_vals = [r[KV4P_RMS] for r in kv4p_ticks if r[KV4P_GOT]]
                kv_decerr = sum(r[KV4P_DECERR] for r in kv4p_ticks)

                f.write(f"\n{'='*90}\n")
                f.write("KV4P AUDIO\n")
                f.write(f"{'='*90}\n")
                f.write(f"  ticks={len(kv4p_ticks)}  data={kv_got} ({kv_got*100//max(1,len(kv4p_ticks))}%)  "
                        f"underrun={kv_none} ({kv_none*100//max(1,len(kv4p_ticks))}%)\n")
                f.write(f"  opus_frames={kv_rxf_total}  opus_bytes={kv_rxB_total}  "
                        f"queue_drops={kv_drops}  decode_errors={kv_decerr}\n")
                if kv_sbb_vals:
                    f.write(f"  sub_buf: mean={statistics.mean(kv_sbb_vals):.0f}B  "
                            f"min={min(kv_sbb_vals)}B  max={max(kv_sbb_vals)}B\n")
                if kv_rms_vals:
                    f.write(f"  rms: mean={statistics.mean(kv_rms_vals):.0f}  "
                            f"min={min(kv_rms_vals):.0f}  max={max(kv_rms_vals):.0f}\n")
                # Identify gap patterns: consecutive underruns
                gaps = []
                gap_len = 0
                for r in kv4p_ticks:
                    if not r[KV4P_GOT]:
                        gap_len += 1
                    else:
                        if gap_len > 0:
                            gaps.append(gap_len)
                        gap_len = 0
                if gap_len > 0:
                    gaps.append(gap_len)
                if gaps:
                    f.write(f"  gap_runs={len(gaps)}  gap_ticks: mean={statistics.mean(gaps):.1f}  "
                            f"max={max(gaps)}  total={sum(gaps)}\n")

                # TX summary
                tx_ticks = [r for r in kv4p_ticks if len(r) > KV4P_TXF and r[KV4P_TXF] > 0]
                ann_ticks = sum(1 for r in kv4p_ticks if len(r) > KV4P_TXANN and r[KV4P_TXANN])
                tx_frames_total = sum(r[KV4P_TXF] for r in kv4p_ticks if len(r) > KV4P_TXF)
                tx_drop_total = sum(r[KV4P_TXDROP] for r in kv4p_ticks if len(r) > KV4P_TXDROP)
                tx_err_total = sum(r[KV4P_TXERR] for r in kv4p_ticks if len(r) > KV4P_TXERR)
                tx_rms_vals = [r[KV4P_TXRMS] for r in tx_ticks if r[KV4P_TXRMS] > 0]
                f.write(f"\n  TX (gateway→radio):\n")
                f.write(f"    ticks_with_tx={len(tx_ticks)}  frames_sent={tx_frames_total}  "
                        f"buf_carry={tx_drop_total}  encode_errors={tx_err_total}  ann_delay_ticks={ann_ticks}\n")
                if tx_rms_vals:
                    f.write(f"    input_rms: mean={statistics.mean(tx_rms_vals):.0f}  "
                            f"min={min(tx_rms_vals):.0f}  max={max(tx_rms_vals):.0f}\n")
                if tx_frames_total > 0:
                    audio_sent = tx_frames_total * 3840
                    audio_in = len(tx_ticks) * 4800
                    sent_pct = audio_sent * 100 // max(1, audio_in)
                    f.write(f"    audio_sent={audio_sent}B ({sent_pct}% of {audio_in}B input)  "
                            f"buf_carry is bytes held across ticks, not dropped\n")
                f.write("\n")

            # ── Events (key presses / mode changes) ──
            events = list(self._trace_events)
            if events:
                f.write(f"\n{'='*90}\n")
                f.write(f"EVENTS ({len(events)})\n")
                f.write(f"{'='*90}\n")
                for ts, etype, evalue in events:
                    rel = ts - self._audio_trace_t0 if self._audio_trace_t0 > 0 else 0
                    f.write(f"  {rel:8.3f}s  {etype:<15} {evalue}\n")

            # ── Speaker thread trace ──
            spk = list(self._spk_trace)
            if spk:
                ST, SWAIT, SWR, SQD, SDLEN, SEMPTY, SMUTED = range(7)
                writes = [r for r in spk if not r[SEMPTY]]
                empties = [r for r in spk if r[SEMPTY]]
                f.write(f"\n{'='*90}\n")
                f.write(f"SPEAKER THREAD ({len(spk)} iterations, {len(writes)} writes, {len(empties)} empty waits)\n")
                f.write(f"{'='*90}\n")
                if writes:
                    wait_ms = [r[SWAIT] for r in writes]
                    write_ms = [r[SWR] for r in writes if r[SWR] >= 0]
                    intervals = [spk[i+1][ST] - spk[i][ST] for i in range(len(spk)-1)
                                 if not spk[i][SEMPTY] and not spk[i+1][SEMPTY]] if len(spk) > 1 else [0.05]
                    f.write(f"\n  WRITE TIMING\n")
                    if write_ms:
                        f.write(f"    stream.write(): mean={statistics.mean(write_ms):.1f}ms  "
                                f"min={min(write_ms):.1f}ms  max={max(write_ms):.1f}ms\n")
                    f.write(f"    queue.get() wait: mean={statistics.mean(wait_ms):.1f}ms  "
                            f"max={max(wait_ms):.1f}ms\n")
                    if intervals:
                        int_ms = [i * 1000 for i in intervals]
                        f.write(f"    write interval: mean={statistics.mean(int_ms):.1f}ms  "
                                f"stdev={statistics.stdev(int_ms):.1f}ms  max={max(int_ms):.1f}ms\n")
                    dlens = set(r[SDLEN] for r in writes)
                    f.write(f"    data lengths: {sorted(dlens)}\n")
                    # Gaps: consecutive empties
                    spk_gaps = []
                    g = 0
                    for r in spk:
                        if r[SEMPTY]:
                            g += 1
                        else:
                            if g > 0:
                                spk_gaps.append(g)
                            g = 0
                    if g > 0:
                        spk_gaps.append(g)
                    if spk_gaps:
                        f.write(f"    empty gaps: {len(spk_gaps)} (max {max(spk_gaps)} consecutive empties = "
                                f"{max(spk_gaps) * 100:.0f}ms)\n")
                    else:
                        f.write(f"    empty gaps: none\n")

                    # Per-write detail (first 100 + anomalies)
                    f.write(f"\n  {'idx':>5} {'t(s)':>7} {'wait':>6} {'write':>6} {'qd':>3} {'len':>5} {'notes':>10}\n")
                    f.write(f"  {'-'*50}\n")
                    for i, r in enumerate(writes):
                        is_early = i < 100
                        is_anomaly = (r[SWR] > 80 or r[SWAIT] > 80 or r[SQD] >= 7 or r[SWR] < 0)
                        if is_early or is_anomaly:
                            notes = ''
                            if r[SWR] < 0:
                                notes = 'ERR'
                            elif r[SWR] > 60:
                                notes = 'SLOW'
                            elif r[SQD] >= 7:
                                notes = 'FULL'
                            flag = '*' if is_anomaly and not is_early else ' '
                            f.write(f"  {i:>4}{flag} {r[ST]:7.3f} {r[SWAIT]:6.1f} {r[SWR]:6.1f} "
                                    f"{r[SQD]:3} {r[SDLEN]:5} {notes:>10}\n")

            f.write(f"\n{'='*90}\n")
            f.write(f"End of trace ({len(trace)} main ticks, {len(spk) if spk else 0} speaker iterations)\n")

        print(f"\n  Audio trace written to: {out_path}")

    def cleanup(self):
        """Clean up resources"""
        # Restore terminal settings (keyboard thread is daemon and may not
        # reach its own finally block before the process exits)
        if hasattr(self, '_terminal_settings'):
            try:
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._terminal_settings)
            except Exception:
                pass

        # Restore original stdout/stderr before cleanup prints
        if hasattr(self, '_orig_stderr') and self._orig_stderr:
            sys.stderr = self._orig_stderr
            # Restore fd 2 if we piped it
            try:
                os.dup2(self._orig_stderr.fileno(), 2)
            except Exception:
                pass
            # Close the pipe read end to unblock the reader thread
            if hasattr(self, '_stderr_pipe_r'):
                try:
                    os.close(self._stderr_pipe_r)
                except Exception:
                    pass
        if self._status_writer:
            sys.stdout = self._status_writer._orig
            self._status_writer = None

        # Stop watchdog trace and flush remaining samples
        if self._watchdog_active:
            self._watchdog_active = False

        # Dump audio trace before anything else
        try:
            self._dump_audio_trace()
        except Exception as e:
            print(f"\n  [Warning] Failed to write audio trace: {e}")

        if self.config.VERBOSE_LOGGING:
            print("\nCleaning up...")

        # Signal threads to stop
        self.running = False

        # Give threads time to finish current operations
        time.sleep(0.2)

        # Close stream output pipe first (before stopping other things)
        if hasattr(self, 'stream_output') and self.stream_output:
            try:
                self.stream_output.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  Stream output closed")
            except:
                pass
        
        # Release PTT
        if self.ptt_active:
            self.set_ptt_state(False)
        
        # Close Mumble connection first (stops audio callbacks)
        if self.mumble:
            try:
                self.mumble.stop()
            except:
                pass
        
        # Small delay to let Mumble fully stop
        time.sleep(0.1)
        
        # Now close audio streams (with better error handling for ALSA)
        if self.sdr_plugin:
            try:
                self.sdr_plugin.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  SDR audio closed")
            except Exception as e:
                pass
            except Exception as e:
                pass  # Suppress ALSA errors during shutdown

        if self.remote_audio_source:
            try:
                self.remote_audio_source.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  Remote audio source closed")
            except Exception:
                pass

        if self.remote_audio_server:
            try:
                self.remote_audio_server.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  Remote audio server closed")
            except Exception:
                pass

        if self.announce_input_source:
            try:
                self.announce_input_source.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  Announcement input closed")
            except Exception:
                pass

        # Close relay serial ports (leave relays in current state — don't power-cycle on restart)
        if self.relay_radio:
            try:
                self.relay_radio.close()
                if self.config.VERBOSE_LOGGING:
                    print("  Radio relay port closed")
            except Exception:
                pass
        if self.relay_charger:
            try:
                self.relay_charger.close()
                if self.config.VERBOSE_LOGGING:
                    print("  Charger relay port closed")
            except Exception:
                pass

        if self.automation_engine:
            try:
                self.automation_engine.stop()
            except Exception:
                pass

        if self.smart_announce:
            try:
                self.smart_announce.stop()
            except Exception:
                pass

        if self.web_config_server:
            try:
                self.web_config_server.stop()
            except Exception:
                pass

        if self.ddns_updater:
            try:
                self.ddns_updater.stop()
            except Exception:
                pass

        if self.cloudflare_tunnel:
            try:
                self.cloudflare_tunnel.stop()
            except Exception:
                pass

        if self.relay_ptt:
            try:
                self.relay_ptt.set_state(False)
                self.relay_ptt.close()
            except Exception:
                pass

        if self.cat_client:
            try:
                self.cat_client.close()
                if self.config.VERBOSE_LOGGING:
                    print("  CAT client closed")
            except Exception:
                pass

        if self.d75_plugin:
            try:
                self.d75_plugin.close()
                if self.config.VERBOSE_LOGGING:
                    print("  D75 CAT client closed")
            except Exception:
                pass

        if self.d75_plugin:
            try:
                self.d75_plugin.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  D75 audio source closed")
            except Exception:
                pass

        # Stop local Mumble Server instances on gateway exit
        if self.mumble_server_1:
            try:
                self.mumble_server_1.stop()
                if self.config.VERBOSE_LOGGING:
                    print("  Mumble Server 1 stopped")
            except Exception:
                pass
        if self.mumble_server_2:
            try:
                self.mumble_server_2.stop()
                if self.config.VERBOSE_LOGGING:
                    print("  Mumble Server 2 stopped")
            except Exception:
                pass

        if self.input_stream:
            try:
                # Stop stream first (prevents ALSA mmap errors)
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                time.sleep(0.05)  # Give ALSA time to clean up
                self.input_stream.close()
            except Exception as e:
                pass  # Suppress ALSA errors during shutdown
        
        if self.speaker_stream:
            try:
                if self.speaker_stream.is_active():
                    self.speaker_stream.stop_stream()
                self.speaker_stream.close()
            except Exception:
                pass

        if self.output_stream:
            try:
                # Stop stream first
                if self.output_stream.is_active():
                    self.output_stream.stop_stream()
                time.sleep(0.05)  # Give ALSA time to clean up
                self.output_stream.close()
            except Exception as e:
                pass  # Suppress ALSA errors during shutdown
        
        if self.pyaudio_instance:
            try:
                self.pyaudio_instance.terminate()
            except Exception as e:
                pass  # Suppress errors
        
        # Close AIOC device
        if self.aioc_device:
            try:
                self.aioc_device.close()
            except:
                pass
        
        print("Shutdown complete")


