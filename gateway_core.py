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
    AudioSource, AudioProcessor, FilePlaybackSource, LoopPlaybackSource,
    EchoLinkSource,
    RemoteAudioServer, RemoteAudioSource,
    NetworkAnnouncementSource,
    WebMicSource, WebMonitorSource, LinkAudioSource, StreamOutputSource, generate_cw_pcm,
)
from audio_util import pcm_level, pcm_rms, rms_to_level, update_level, pcm_db
# ListenBus now created by BusManager (bus_manager.py)
from gateway_utils import DDNSUpdater, EmailNotifier, CloudflareTunnel, MumbleServerManager, USBIPManager, GPSManager
from repeater_manager import RepeaterManager
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
        # Per-stream chunk-level trace (all 4 streams)
        from stream_trace import StreamTrace
        self._stream_trace = StreamTrace(maxlen=60000)
        
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
        self.mixer = None  # Managed by BusManager
        self.radio_source = None  # Will be initialized after AIOC setup
        # sdr_source removed — use sdr_plugin  # SDR1 receiver audio source
        self.sdr_muted = False  # SDR1-specific mute
        self.sdr_ducked = False  # Is SDR1 currently being ducked (status display)
        self.sdr_audio_level = 0  # SDR1 audio level for status bar
        # sdr2_source removed — use sdr_plugin  # SDR2 receiver audio source
        self.sdr2_muted = False  # SDR2-specific mute
        self.sdr2_ducked = False  # Is SDR2 currently being ducked (status display)
        self.sdr2_audio_level = 0  # SDR2 audio level for status bar
        self.stream_audio_level = 0  # Broadcastify stream level for status bar
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

        # Transcriber
        self.transcriber = None
        self.transcription_audio_level = 0

        # Web configuration UI
        self.web_config_server = None

        # Dynamic DNS updater
        self.ddns_updater = None  # DDNSUpdater instance
        self.cloudflare_tunnel = None  # CloudflareTunnel instance
        self.email_notifier = None  # EmailNotifier instance
        self.gps_manager = None  # GPSManager instance
        self.repeater_manager = None  # RepeaterManager instance

        # TH-9800 CAT control
        self.cat_client = None  # RadioCATClient instance

        # KV4P HT Radio
        self.kv4p_plugin = None           # KV4PPlugin instance
        self.packet_plugin = None         # PacketRadioPlugin instance
        self.bus_manager = None           # BusManager (created in _setup_routing)
        self._bus_sinks = {}              # {bus_id: set(sink_ids)} — populated by _setup_routing
        self._bus_stream_flags = {}       # {bus_id: {pcm, mp3, vad}} — populated by _setup_routing
        self._listen_bus_id = 'listen'    # Primary listen bus ID — set by _setup_routing
        self._listen_bus_muted = False    # Primary listen bus mute — set by _setup_routing
        self._muted_sinks = set()         # Set of muted sink IDs
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
            return rms_to_level(pcm_rms(pcm_data))
        except Exception:
            return 0

    def _update_sv_level(self, pcm_data):
        """Update sv_audio_level from PCM data sent to remote client."""
        self.sv_audio_level = pcm_level(pcm_data, self.sv_audio_level)

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

    # D75 processing is handled by the link endpoint — no local sync needed

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

            db_level = pcm_db(pcm_data)

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

            db = pcm_db(pcm_data)

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
        if tx_radio == 'kv4p' and self.kv4p_plugin:
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
    
    # D75 PTT is handled by the link endpoint — no local PTT code needed

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
        self.rx_audio_level = pcm_level(soundchunk.pcm, self.rx_audio_level)

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
            self.speaker_audio_level = pcm_level(spk, self.speaker_audio_level)
        except Exception:
            return

        # Virtual speaker — metering only, no real audio output
        if not self.speaker_queue:
            return

        try:
            # Absorb hw/sw clock drift: drain excess when queue gets deep.
            _spk_qd = self.speaker_queue.qsize()
            if _spk_qd >= 4:
                _dropped = 0
                while self.speaker_queue.qsize() > 2:
                    try:
                        self.speaker_queue.get_nowait()
                        _dropped += 1
                    except Exception:
                        break
                # Track drops for audio quality trace
                if not hasattr(self, '_spk_drop_count'):
                    self._spk_drop_count = 0
                    self._spk_drop_total = 0
                self._spk_drop_count += _dropped
                self._spk_drop_total += _dropped
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
                        # Wire stream trace to SDR tuner captures
                        for _t in [self.sdr_plugin.get_tuner(1), self.sdr_plugin.get_tuner(2)]:
                            if _t:
                                _t._stream_trace = self._stream_trace
                        print("✓ SDR plugin initialized (routing managed by BusManager)")
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
                    self.th9800_plugin._stream_trace = self._stream_trace
                    print("✓ TH-9800 plugin initialized (routing managed by BusManager)")
                else:
                    print("⚠ TH-9800 plugin setup failed")
                    self.th9800_plugin = None
            except Exception as e:
                print(f"⚠ TH-9800 plugin error: {e}")
                import traceback; traceback.print_exc()
                self.th9800_plugin = None

            # Backward compat: expose plugin internals for code that still uses them.
            # input_stream is NOT copied — it's managed by the reader thread and
            # may be closed/reopened at any time.  Code that needs the stream must
            # go through th9800_plugin._input_stream (live reference).
            if self.th9800_plugin:
                self.radio_source = self.th9800_plugin
                self.pyaudio_instance = self.th9800_plugin._pyaudio
                self.input_stream = None  # DO NOT cache — reader thread owns lifecycle
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

            # Loop playback source (always available if loop recorder exists)
            self.loop_playback_source = LoopPlaybackSource(self)
            self.loop_playback_source._stream_trace = self._stream_trace

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

            # Initialize Remote Audio Link (full duplex — TX server + RX source)
            remote_enabled = getattr(self.config, 'REMOTE_AUDIO_ROLE', 'disabled').lower().strip("'\"") != 'disabled'
            if remote_enabled:
                # TX: RemoteAudioServer connects out to Windows client on REMOTE_AUDIO_PORT (9600)
                try:
                    host = self.config.REMOTE_AUDIO_HOST
                    if not host:
                        print("⚠ Warning: REMOTE_AUDIO_HOST not set — TX server needs a destination IP")
                    else:
                        self.remote_audio_server = RemoteAudioServer(self.config)
                        self.remote_audio_server.start()
                except Exception as e:
                    print(f"⚠ Warning: Could not start remote audio TX server: {e}")
                    self.remote_audio_server = None

                # RX: RemoteAudioSource listens on REMOTE_AUDIO_RX_PORT (9602) for Windows client
                try:
                    rx_port = int(getattr(self.config, 'REMOTE_AUDIO_RX_PORT', 9602))
                    print(f"Initializing remote audio RX source (listening on 0.0.0.0:{rx_port})...")
                    self.remote_audio_source = RemoteAudioSource(self.config, self)
                    if self.remote_audio_source.setup_audio(port_override=rx_port):
                        self.remote_audio_source.enabled = True
                        self.remote_audio_source.duck = self.config.REMOTE_AUDIO_DUCK
                        self.remote_audio_source.sdr_priority = int(self.config.REMOTE_AUDIO_PRIORITY)
                        # Routing managed by BusManager via sync_listen_bus
                        print(f"✓ Remote audio RX source initialized (routing managed by BusManager)")
                    else:
                        print("⚠ Warning: Could not initialize remote audio RX source")
                        self.remote_audio_source = None
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize remote audio RX source: {e}")
                    self.remote_audio_source = None

            # Initialize announcement input (port 9601) if enabled
            if getattr(self.config, 'ENABLE_ANNOUNCE_INPUT', False):
                try:
                    bind_host = self.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
                    port = self.config.ANNOUNCE_INPUT_PORT
                    print(f"Initializing announcement input (listening on {bind_host}:{port})...")
                    self.announce_input_source = NetworkAnnouncementSource(self.config, self)
                    if self.announce_input_source.setup_audio():
                        print(f"✓ Announcement input (ANNIN) initialized (routing managed by BusManager)")
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
                        print("✓ Web microphone source (WEBMIC) initialized (routing managed by BusManager)")
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize web mic source: {e}")
                    self.web_mic_source = None

            # Initialize web monitor source (browser mic → mixer, no PTT)
            if getattr(self.config, 'ENABLE_WEB_MONITOR', True):
                try:
                    self.web_monitor_source = WebMonitorSource(self.config, self)
                    if self.web_monitor_source.setup_audio():
                        print("✓ Web monitor source (MONITOR) initialized (routing managed by BusManager)")
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize web monitor source: {e}")
                    self.web_monitor_source = None

            # Relay controllers now owned by TH9800Plugin
            if self.th9800_plugin:
                self.relay_radio = self.th9800_plugin._relay_radio
                self.relay_ptt = self.th9800_plugin._relay_ptt
                self.relay_charger = self.th9800_plugin._relay_charger

            # CAT client now owned by TH9800Plugin (backward compat alias set above)

            # D75 is now a link endpoint — no local plugin init needed

            # Initialize KV4P HT Radio (plugin)
            self.kv4p_plugin = None
            if getattr(self.config, 'ENABLE_KV4P', False):
                try:
                    from kv4p_plugin import KV4PPlugin
                    print(f"Initializing KV4P plugin...")
                    self.kv4p_plugin = KV4PPlugin()
                    if self.kv4p_plugin.setup(self.config):
                        self.kv4p_plugin._stream_trace = self._stream_trace
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

            # Initialize Packet Radio (Direwolf TNC)
            self.packet_plugin = None
            if getattr(self.config, 'ENABLE_PACKET', False):
                try:
                    from packet_radio import PacketRadioPlugin
                    print("Initializing Packet Radio plugin...")
                    self.packet_plugin = PacketRadioPlugin()
                    if self.packet_plugin.setup(self.config, gateway=self):
                        print("✓ Packet Radio plugin initialized (routed via bus manager)")
                    else:
                        print("⚠ Warning: Packet Radio plugin setup failed")
                        self.packet_plugin = None
                except Exception as e:
                    print(f"⚠ Packet Radio plugin error: {e}")
                    import traceback; traceback.print_exc()
                    self.packet_plugin = None

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
                        if 'rx_boost' in saved:
                            src.audio_boost = saved['rx_boost'] / 100.0
                        if 'tx_boost' in saved:
                            src.tx_audio_boost = saved['tx_boost'] / 100.0
                        src.server_connected = True
                        # Store endpoint capabilities for routing UI
                        src._endpoint_caps = info.get('capabilities', {})
                        self.link_endpoints[name] = src
                        print(f"  [Link] {name} registered (routing managed by BusManager)")
                        self._link_ptt_active[name] = False
                        self._link_last_status[name] = {}
                        self._link_tx_levels[name] = 0
                        print(f"  [Link] Endpoint registered: {name} ({info.get('plugin', '?')})")
                        # Reload bus manager so buses pick up the new LinkAudioSource
                        if hasattr(self, 'bus_manager') and self.bus_manager:
                            try:
                                self.bus_manager.reload()
                                print(f"  [Link] Bus manager reloaded for {name}")
                            except Exception as _bme:
                                print(f"  [Link] Bus reload error: {_bme}")
                        return src  # server stores src.push_audio as audio callback

                    def _link_on_disconnect(name):
                        """Called when an endpoint disconnects — remove its audio source."""
                        src = self.link_endpoints.pop(name, None)
                        if src:
                            src.server_connected = False
                            if self.bus_manager and self.bus_manager.listen_bus:
                                self.bus_manager.listen_bus.remove_source(src.name)
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
                            if name not in self._link_last_status:
                                self._link_last_status[name] = {}
                            self._link_last_status[name].update(result.get('status', result))
                        elif cmd in ('rx_gain', 'tx_gain') and isinstance(result, dict):
                            if name not in self._link_last_status:
                                self._link_last_status[name] = {}
                            for k in ('rx_gain_db', 'tx_gain_db'):
                                if k in result:
                                    self._link_last_status[name][k] = result[k]

                    def _link_on_endpoint_status(name, status):
                        """Called when an endpoint sends a STATUS frame."""
                        if isinstance(status, dict) and status.get('type') != 'heartbeat':
                            # Forward Direwolf log lines to packet plugin
                            if status.get('type') == 'direwolf_log' and self.packet_plugin:
                                self.packet_plugin._direwolf_log.append(status.get('line', ''))
                                # Parse audio level from Direwolf log
                                line = status.get('line', '')
                                if 'audio level' in line:
                                    import re as _dw_re
                                    m = _dw_re.search(r'audio level\s*=\s*(\d+)', line)
                                    if m:
                                        self.packet_plugin._dw_audio_level = int(m.group(1))
                                return
                            if name not in self._link_last_status:
                                self._link_last_status[name] = {}
                            self._link_last_status[name].update(status)

                    self.link_server = GatewayLinkServer(
                        port=link_port,
                        on_register=_link_on_register,
                        on_disconnect=_link_on_disconnect,
                        on_ack=_link_on_ack,
                        on_endpoint_status=_link_on_endpoint_status,
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
                    self.cloudflare_tunnel = CloudflareTunnel(
                        self.config, on_url_changed=self._on_tunnel_url_changed)
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

            # Initialize GPS receiver
            if getattr(self.config, 'ENABLE_GPS', False):
                try:
                    self.gps_manager = GPSManager(self.config)
                    self.gps_manager.start()
                except Exception as e:
                    print(f"  [GPS] Init error: {e}")

            # Initialize Repeater Database (depends on GPS)
            if getattr(self.config, 'ENABLE_REPEATER_DB', False):
                try:
                    self.repeater_manager = RepeaterManager(self.config, self.gps_manager)
                    self.repeater_manager.start()
                except Exception as e:
                    print(f"  [Repeaters] Init error: {e}")

            # Initialize EchoLink source if enabled (Phase 3B)
            if self.config.ENABLE_ECHOLINK:
                try:
                    print("Initializing EchoLink integration...")
                    self.echolink_source = EchoLinkSource(self.config, self)
                    if self.echolink_source.connected:
                        print("✓ EchoLink source initialized (routing managed by BusManager)")
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
                            
                            self.input_stream = self.pyaudio_instance.open(
                                format=audio_format,
                                channels=self.config.AUDIO_CHANNELS,
                                rate=self.config.AUDIO_RATE,
                                input=True,
                                input_device_index=input_idx,
                                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
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

            # Increase audio_per_packet to bundle more frames per Mumble packet.
            # Default 0.02 (20ms = 1 frame/packet) causes stutter when pymumble's
            # loop is GIL-starved (only fires ~20x/sec instead of 50x/sec).
            # At 0.06 (60ms = 3 frames/packet), 20 sends/sec × 60ms = 1200ms/sec.
            try:
                self.mumble.sound_output.set_audio_per_packet(0.06)
                print(f"  Mumble audio_per_packet set to 0.06 (60ms, 3 frames/packet)")
            except Exception as e:
                print(f"  ⚠ Could not set audio_per_packet: {e}")

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
        from text_commands import speak_text as _speak_text
        return _speak_text(self, text, voice=voice)
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
        from text_commands import on_text_message as _on_text_message
        _on_text_message(self, text_message)
    def _get_cross_clock_drift_ms(self):
        """Measure drift between main loop and BusManager clocks.

        Compares wall-clock timestamps of the most recent tick from each.
        Positive = BM ticked after main (BM lagging).
        """
        if not self.bus_manager:
            return 0.0
        bm_tick, bm_mono = self.bus_manager._bm_tick_mono
        if bm_tick == 0 or bm_mono == 0.0:
            return 0.0
        # Both clocks target 50ms ticks. Compare when they last ticked.
        main_mono = time.monotonic()  # we're inside the main tick right now
        return (main_mono - bm_mono) * 1000  # ms since BM last ticked

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

        # ── GC control: disable automatic collection in this hot path ──
        import gc as _gc
        _gc.disable()
        self._gc_events_main = []  # GC pause records for trace
        def _gc_cb(phase, info):
            if phase == 'start':
                _gc_cb._t0 = time.monotonic()
            elif phase == 'stop' and hasattr(_gc_cb, '_t0'):
                dur_ms = (time.monotonic() - _gc_cb._t0) * 1000
                self._gc_events_main.append((time.monotonic(), info.get('generation', -1), dur_ms))
        _gc.callbacks.append(_gc_cb)
        print("  Audio thread: GC disabled, manual gen-0 every 5s")

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

                # ── All bus ticks + sink delivery handled by BusManager ──
                # Main loop drains queues for SDR rebroadcast TX and WebSocket push.
                data = None  # no longer produced here; kept for trace compat
                if not self.bus_manager:
                    self.audio_capture_active = False
                    continue

                # Drain SDR rebroadcast queue (duckee_only_audio + ptt flag)
                sdr_only_audio, ptt_required = self.bus_manager.drain_sdr_rebroadcast()

                # Drain PCM/MP3 for WebSocket push
                _bm_pcm = self.bus_manager.drain_pcm()
                _bm_mp3 = self.bus_manager.drain_mp3()
                self._last_pcm_drain_n = getattr(self.bus_manager, '_last_pcm_drain_n', 0)

                # Read listen bus state for trace
                _lbid = getattr(self.bus_manager, '_listen_bus_id', None)
                if _lbid:
                    _tr_mixer_got = self.bus_manager._bus_levels.get(_lbid, 0) > 0
                    _tr_mixer_state = getattr(self, '_last_mixer_trace_state', {})

                # SDR rebroadcast: route SDR-only mix to AIOC radio TX
                if self.sdr_rebroadcast and not ptt_required and sdr_only_audio is not None:
                    sdr_has_signal = pcm_db(sdr_only_audio) > -50.3  # was rms > 100

                    if sdr_has_signal:
                        self._rebroadcast_ptt_hold_until = time.monotonic() + self.config.SDR_REBROADCAST_PTT_HOLD
                        self._rebroadcast_sending = True
                        self.last_sound_time = time.time()
                    else:
                        self._rebroadcast_sending = False

                    rebroadcast_ptt_needed = time.monotonic() < self._rebroadcast_ptt_hold_until

                    if rebroadcast_ptt_needed:
                        self.last_sound_time = time.time()

                        if not self._rebroadcast_ptt_active and not self.tx_muted and not self.manual_ptt_mode:
                            self.set_ptt_state(True)
                            self._ptt_change_time = time.monotonic()
                            self._rebroadcast_ptt_active = True
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

                        tx_level_pcm = pcm if sdr_has_signal else sdr_only_audio
                        self.rx_audio_level = pcm_level(tx_level_pcm, self.rx_audio_level)
                        self.last_rx_audio_time = time.time()

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

                # WebSocket PCM push (all buses mixed by BusManager)
                if _bm_pcm is not None:
                    if self.web_config_server and self.web_config_server._ws_clients:
                        self.web_config_server.push_ws_audio(_bm_pcm)

                # MP3 stream push
                if _bm_mp3 is not None:
                    if self.web_config_server and self.web_config_server._stream_subscribers:
                        self.web_config_server.push_audio(_bm_mp3)

                consecutive_errors = 0
                _tr_outcome = 'bus_ok'

            except Exception as e:
                consecutive_errors += 1
                self.audio_capture_active = False
                _tr_outcome = 'exception'

                error_type = type(e).__name__
                error_msg = str(e)

                # Always log first occurrence of each error burst
                if consecutive_errors <= 2:
                    print(f"  [MainLoop] Exception #{consecutive_errors}: {error_type}: {error_msg}")
                    if consecutive_errors == 1:
                        import traceback; traceback.print_exc()

                self.last_stream_error = f"{error_type}: {error_msg}"

                if consecutive_errors >= max_consecutive_errors:
                    # Stream lifecycle is managed by TH9800Plugin reader thread.
                    # Just reset the counter — don't call the old restart_audio_input().
                    consecutive_errors = 0
                # else: self-clock at top of loop handles pacing
            finally:
                # ── Trace record (toggled by 'i' key) ──
                if self._trace_recording:
                    # Snapshot enhanced instrumentation from sources
                    _sdr1_disc = 0.0
                    _sdr1_sb_after = -1
                    _sdr1_cb_ovf = 0
                    _sdr1_cb_drop = 0
                    _aioc_disc = 0.0
                    _aioc_sb_after = -1
                    _aioc_cb_ovf = 0
                    _aioc_cb_drop = 0
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
                        0.0,  # 10: AIOC blocked (ms) — legacy field, always 0
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
                        # === Audio quality diagnostics (50+) ===
                        getattr(self, '_spk_drop_count', 0),        # 50: speaker queue drops this tick
                        getattr(self, '_last_pcm_drain_n', 0),      # 51: PCM drain chunk count (1=good, 2+=drift)
                        self._get_cross_clock_drift_ms(),            # 52: BusManager cross-clock drift (ms)
                        len(self._gc_events_main),                   # 53: cumulative GC events (main loop)
                    ))
                # Reset per-tick counters
                self._spk_drop_count = 0
                self._last_pcm_drain_n = 0
                # Manual GC: gen-0 only, every 100 ticks (~5s), during sleep window
                if self._tx_loop_tick % 100 == 0:
                    _gc.collect(0)

    def _find_darkice_pid(self):
        from stream_stats import find_darkice_pid
        return find_darkice_pid(self)
    def _get_darkice_stats(self):
        from stream_stats import get_darkice_stats
        return get_darkice_stats(self)
    def _get_stream_stats(self):
        from stream_stats import get_stream_stats
        return get_stream_stats(self)
    def _get_darkice_stats_cached(self):
        from stream_stats import get_darkice_stats_cached
        return get_darkice_stats_cached(self)
    def _restart_darkice(self):
        from stream_stats import restart_darkice
        restart_darkice(self)
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
            
            try:
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
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
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
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
            }, None),  # link endpoint manages its own processing
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
            if sync_method:
                getattr(self, sync_method)()

    def handle_key(self, char):
        from text_commands import handle_key as _handle_key
        _handle_key(self, char)
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

        # Pre-scan for D75 link endpoint (avoid repeated scans in dict below)
        _d75_link = next((src for n, src in self.link_endpoints.items() if 'd75' in n.lower()), None)

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
            'd75_enabled': getattr(self.config, 'ENABLE_D75', False) or bool(_d75_link),
            'd75_connected': bool(_d75_link),
            'd75_audio_connected': bool(_d75_link),
            'd75_mode': 'link_endpoint' if _d75_link else 'disabled',
            'd75_level': _d75_link.audio_level if _d75_link else 0,
            'd75_muted': getattr(_d75_link, 'muted', False) if _d75_link else False,
            'kv4p_enabled': bool(self.kv4p_plugin),
            'kv4p_level': self.kv4p_plugin.audio_level if self.kv4p_plugin else 0,
            'kv4p_muted': getattr(self, 'kv4p_muted', False),
            'gps_enabled': bool(self.gps_manager),
            'repeater_db_enabled': bool(self.repeater_manager),
            'adsb_enabled': getattr(self.config, 'ENABLE_ADSB', False),
            'telegram_enabled': getattr(self.config, 'ENABLE_TELEGRAM', False),
            'monitor_enabled': bool(self.web_monitor_source),
            'monitor_level': self.web_monitor_source.audio_level if self.web_monitor_source else 0,
            'link_enabled': bool(self.link_server),
            'link_endpoints': [
                {
                    'name': name,
                    'connected': True,
                    'plugin': _ep_info.get('plugin', ''),
                    'capabilities': _ep_info.get('capabilities', {}),
                    'level': src.audio_level,
                    'rx_muted': src.muted,
                    'tx_muted': self.link_endpoint_settings.get(name, {}).get('tx_muted', False),
                    'ptt_active': self._link_ptt_active.get(name, False),
                    'tx_level': self._link_tx_levels.get(name, 0),
                    'endpoint_status': self._link_last_status.get(name, {}),
                }
                for name, src in list(self.link_endpoints.items())
                for _ep_info in [(self.link_server.get_endpoint_info(name) or {}) if self.link_server else {}]
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
            'darkice_pid': self._darkice_pid,
            'darkice_restarts': self._darkice_restart_count,
            'stream_restarts': getattr(self.th9800_plugin, '_stream_restart_count', 0) if self.th9800_plugin else 0,
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
            self.mixer = self.bus_manager.listen_bus  # Backward compat for trace access
            # Cache bus metadata for web UI (refreshed after routing saves)
            self._bus_stream_flags = self.bus_manager.get_bus_stream_flags()
            self._bus_sinks = self.bus_manager.get_bus_sinks()
            self._listen_bus_id = self.bus_manager.get_listen_bus_id()
            self._listen_bus_muted = self.bus_manager.is_bus_muted(self._listen_bus_id)
        except Exception as e:
            print(f"  [BusManager] Failed to start: {e}")
            self.bus_manager = None

        # Load external plugins from plugins/ directory
        self._external_plugins = {}
        try:
            from plugin_loader import discover_plugins
            self._external_plugins = discover_plugins(self.config, self)
            if self._external_plugins:
                print(f"✓ Loaded {len(self._external_plugins)} external plugin(s)")
                # Re-sync listen bus to pick up new sources
                if self.bus_manager:
                    self.bus_manager.sync_listen_bus()
        except Exception as e:
            print(f"  [Plugins] Discovery failed: {e}")

        # Initialize Loop Recorder (per-bus continuous recording)
        try:
            from loop_recorder import LoopRecorder
            self.loop_recorder = LoopRecorder()
            print("✓ Loop Recorder initialized")
        except Exception as e:
            print(f"  [LoopRec] Failed to initialize: {e}")
            self.loop_recorder = None

        # Start Automation Engine if enabled
        if getattr(self.config, 'ENABLE_AUTOMATION', False):
            try:
                from radio_automation import AutomationEngine
                self.automation_engine = AutomationEngine(self)
                self.automation_engine.start()
            except Exception as e:
                print(f"[Automation] Failed to start: {e}")
                self.automation_engine = None

        # Start Transcriber if enabled
        if getattr(self.config, 'ENABLE_TRANSCRIPTION', False):
            try:
                from transcriber import _load_saved_settings as _load_tx_settings
                _tx_saved = _load_tx_settings()
                _tx_mode = _tx_saved.get('mode', str(getattr(self.config, 'TRANSCRIBE_MODE', 'chunked'))).lower()
                if _tx_mode == 'streaming':
                    from transcriber import StreamingTranscriber
                    self.transcriber = StreamingTranscriber(self.config, self)
                else:
                    from transcriber import RadioTranscriber
                    self.transcriber = RadioTranscriber(self.config, self)
                self.transcriber.start()
            except Exception as e:
                print(f"[Transcribe] Failed to start: {e}")
                self.transcriber = None

        # Main loop
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
        finally:
            self.cleanup()
    
    def _watchdog_trace_loop(self):
        from audio_trace import watchdog_trace_loop
        watchdog_trace_loop(self)
    def _dump_audio_trace(self):
        from audio_trace import dump_audio_trace
        dump_audio_trace(self)
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

        # Stop loop recorder
        if getattr(self, 'loop_recorder', None):
            self.loop_recorder.stop()

        # Cleanup external plugins
        for pid, plugin in getattr(self, '_external_plugins', {}).items():
            try:
                plugin.cleanup()
            except Exception:
                pass

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

        if self.transcriber:
            try:
                self.transcriber.stop()
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

        if self.gps_manager:
            try:
                self.gps_manager.stop()
            except Exception:
                pass

        if self.repeater_manager:
            try:
                self.repeater_manager.stop()
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

        # D75 cleanup removed — D75 is now a link endpoint

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

    def _on_tunnel_url_changed(self, new_url):
        """Called by CloudflareTunnel when the tunnel is relaunched with a new URL."""
        print(f"  [Gateway] Tunnel URL changed: {new_url}")
        if self.email_notifier:
            try:
                self.email_notifier.send_tunnel_changed(new_url)
            except Exception as e:
                print(f"  [Gateway] Failed to send tunnel change email: {e}")


