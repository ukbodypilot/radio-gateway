#!/usr/bin/env python3
"""
Mumble to Radio Gateway via AIOC
Reads configuration from gateway_config.txt
Optimized for low latency and high quality audio
"""

import sys
import os

def _get_version():
    """Build version from git: tag-based (e.g. 1.0.0, 1.0.0-3-g87ba23a) or commit hash."""
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
import collections
import queue as _queue_mod
from struct import Struct
import socket
import select  # For non-blocking keyboard input
import array as _array_mod
import math as _math_mod
import numpy as np

# Check for required libraries
try:
    import hid
except ImportError:
    print("ERROR: hidapi library not found!")
    print("Install it with: pip3 install hidapi --break-system-packages")
    sys.exit(1)

# SSL compatibility shim — pymumble uses ssl.wrap_socket() (removed in Python
# 3.12) and ssl.PROTOCOL_TLSv1_2 (deprecated). Patch before importing pymumble.
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
        print("Install with: pip3 install pymumble --break-system-packages")
        print("          or: pip3 install pymumble-py3 --break-system-packages")
        sys.exit(1)

# Patch pymumble's _wrap_socket to allow old Murmur servers with SHA-1 certs.
# ssl.create_default_context() enforces SECLEVEL=1+ which rejects SHA-1 signatures
# (e.g. Murmur 1.2.x auto-generated certificates). Lower to SECLEVEL=0.
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

class Config:
    """Configuration loaded from gateway_config.txt"""
    def __init__(self, config_file="gateway_config.txt"):
        self.config_file = config_file
        self.load_config()
    
    def load_config(self):
        """Load configuration from file"""
        # Default values
        defaults = {
            'MUMBLE_SERVER': '192.168.2.126',
            'MUMBLE_PORT': 64738,
            'MUMBLE_USERNAME': 'RadioGateway',
            'MUMBLE_PASSWORD': '',
            'MUMBLE_CHANNEL': '',
            'AUDIO_RATE': 48000,
            'AUDIO_CHUNK_SIZE': 2400,
            'AUDIO_CHANNELS': 1,
            'AUDIO_BITS': 16,
            'MUMBLE_BITRATE': 96000,
            'MUMBLE_VBR': False,
            'MUMBLE_JITTER_BUFFER': 10,
            'AIOC_PTT_CHANNEL': 3,
            'PTT_RELEASE_DELAY': 0.5,
            'PTT_ACTIVATION_DELAY': 0.1,
            'AIOC_VID': 0x1209,
            'AIOC_PID': 0x7388,
            'AIOC_INPUT_DEVICE': -1,
            'AIOC_OUTPUT_DEVICE': -1,
            'ENABLE_AGC': False,
            'ENABLE_NOISE_SUPPRESSION': False,
            'NOISE_SUPPRESSION_METHOD': 'spectral',
            'NOISE_SUPPRESSION_STRENGTH': 0.5,
            'ENABLE_NOISE_GATE': False,
            'NOISE_GATE_THRESHOLD': -40,
            'NOISE_GATE_ATTACK': 0.01,  # float (seconds)
            'NOISE_GATE_RELEASE': 0.1,  # float (seconds)
            'ENABLE_HIGHPASS_FILTER': False,
            'HIGHPASS_CUTOFF_FREQ': 300,
            'ENABLE_ECHO_CANCELLATION': False,
            'INPUT_VOLUME': 1.0,
            'OUTPUT_VOLUME': 1.0,
            'MUMBLE_LOOP_RATE': 0.01,
            'MUMBLE_STEREO': False,
            'MUMBLE_RECONNECT': True,
            'MUMBLE_DEBUG': False,
            'NETWORK_TIMEOUT': 10,
            'TCP_NODELAY': True,
            'VERBOSE_LOGGING': False,
            'STATUS_UPDATE_INTERVAL': 1.0,  # seconds
            'MAX_MUMBLE_BUFFER_SECONDS': 1.0,
            'BUFFER_MANAGEMENT_VERBOSE': False,
            'ENABLE_VAD': True,
            'VAD_THRESHOLD': -45,
            'VAD_ATTACK': 0.05,  # float (seconds)
            'VAD_RELEASE': 2.0,  # float (seconds)
            'VAD_MIN_DURATION': 0.25,  # float (seconds)
            'ENABLE_STREAM_HEALTH': False,
            'STREAM_RESTART_INTERVAL': 60,
            'STREAM_RESTART_IDLE_TIME': 3,
            'ENABLE_VOX': False,
            'VOX_THRESHOLD': -30,
            'VOX_ATTACK_TIME': 0.05,  # float (seconds)
            'VOX_RELEASE_TIME': 0.5,  # float (seconds)
            # File Playback
            'ENABLE_PLAYBACK': False,
            'PLAYBACK_DIRECTORY': './audio/',
            'PLAYBACK_ANNOUNCEMENT_FILE': '',
            'PLAYBACK_ANNOUNCEMENT_INTERVAL': 0,  # seconds, 0 = disabled
            'PLAYBACK_VOLUME': 4.0,               # float (multiplier; >1.0 boosts, audio is clipped to int16 range)
            # Morse Code (CW)
            'CW_WPM': 15,          # Morse code words per minute
            'CW_FREQUENCY': 700,   # Tone frequency in Hz
            'CW_VOLUME': 1.0,      # Volume multiplier (applied before WAV write; PLAYBACK_VOLUME also applies)
            # Text-to-Speech and Text Commands (Phase 4)
            'ENABLE_TTS': True,
            'ENABLE_TEXT_COMMANDS': True,
            'TTS_VOLUME': 1.0,  # Volume multiplier for TTS audio (1.0 = normal, 2.0 = double, 3.0 = triple)
            'TTS_DEFAULT_VOICE': 1, # Default voice (1=US, 2=British, 3=Australian, 4=Indian, 5=SA, 6=Canadian, 7=Irish, 8=French, 9=German)
            'PTT_TTS_DELAY': 1.0,   # Silence padding before TTS (seconds) to prevent cutoff
            'PTT_ANNOUNCEMENT_DELAY': 0.5,  # Seconds after PTT key-up before announcement audio starts
            # SDR Integration
            'ENABLE_SDR': True,
            'SDR_DEVICE_NAME': 'hw:6,1',  # ALSA device name (e.g., 'Loopback', 'hw:5,1')
            'SDR_DUCK': True,             # Duck SDR: silence SDR when higher priority source is active
            'SDR_MIX_RATIO': 1.0,        # Volume/mix ratio when ducking is disabled (1.0 = full volume)
            'SDR_DISPLAY_GAIN': 1.0,     # Display sensitivity multiplier (1.0 = normal, higher = more sensitive bar)
            'SDR_AUDIO_BOOST': 2.0,      # Actual audio volume boost (1.0 = no change, 2.0 = 2x louder)
            'SDR_BUFFER_MULTIPLIER': 4,  # Buffer size multiplier (4 = 4x normal buffer, ~200ms per ALSA read)
            'SDR_PRIORITY': 1,           # SDR priority for ducking (1 = higher priority, 2 = lower priority)
            'SDR_WATCHDOG_TIMEOUT': 10,        # seconds with no successful read before recovery
            'SDR_WATCHDOG_MAX_RESTARTS': 5,    # max recovery attempts before giving up
            'SDR_WATCHDOG_MODPROBE': False,    # enable kernel module reload (requires sudoers entry)
            # SDR2 Integration (second SDR receiver)
            'ENABLE_SDR2': False,
            'SDR2_DEVICE_NAME': 'hw:4,1',
            'SDR2_DUCK': True,
            'SDR2_MIX_RATIO': 1.0,
            'SDR2_DISPLAY_GAIN': 1.0,
            'SDR2_AUDIO_BOOST': 2.0,
            'SDR2_BUFFER_MULTIPLIER': 4,
            'SDR2_PRIORITY': 2,          # SDR2 priority for ducking (1 = higher, 2 = lower)
            'SDR2_WATCHDOG_TIMEOUT': 10,
            'SDR2_WATCHDOG_MAX_RESTARTS': 5,
            'SDR2_WATCHDOG_MODPROBE': False,
            # Signal Detection Hysteresis (prevents stuttering from rapid on/off)
            'SIGNAL_ATTACK_TIME': 0.15,  # Seconds of CONTINUOUS signal required before a source switch is allowed
            'SIGNAL_RELEASE_TIME': 3.0,  # Seconds of continuous silence required before switching back
            'SWITCH_PADDING_TIME': 1.0,  # Seconds of silence inserted at each transition (duck-out and duck-in)
            'SDR_DUCK_COOLDOWN': 3.0,   # After lower-priority SDR unducks, seconds before higher-priority SDR can re-duck it
            'SDR_SIGNAL_THRESHOLD': -60.0,  # dBFS threshold for SDR signal detection (inclusion + ducking); lower = more sensitive
            'SDR_REBROADCAST_PTT_HOLD': 3.0,  # Seconds to hold PTT after SDR audio stops during rebroadcast
            # EchoLink Integration (Phase 3B)
            'ENABLE_ECHOLINK': False,
            'ECHOLINK_RX_PIPE': '/tmp/echolink_rx',
            'ECHOLINK_TX_PIPE': '/tmp/echolink_tx',
            'ECHOLINK_TO_MUMBLE': True,
            'ECHOLINK_TO_RADIO': False,
            'RADIO_TO_ECHOLINK': True,
            'MUMBLE_TO_ECHOLINK': False,
            # Streaming Output (Phase 3A)
            'ENABLE_STREAM_OUTPUT': False,
            'STREAM_SERVER': 'localhost',
            'STREAM_PORT': 8000,
            'STREAM_PASSWORD': 'hackme',
            'STREAM_MOUNT': '/radio',
            'STREAM_NAME': 'Radio Gateway',
            'STREAM_DESCRIPTION': 'Radio to Mumble Gateway',
            'STREAM_BITRATE': 16,
            'STREAM_FORMAT': 'mp3',
            # Speaker Output (local monitoring)
            'ENABLE_SPEAKER_OUTPUT': False,
            'SPEAKER_OUTPUT_DEVICE': '',   # '' = system default; or partial name e.g. 'USB Audio', 'hw:2,0'
            'SPEAKER_VOLUME': 1.0,         # float multiplier
            'SPEAKER_START_MUTED': True,   # Start with speaker muted (toggle with 's' key)
            # Remote Audio Link (server sends mixed audio to client over TCP)
            'REMOTE_AUDIO_ROLE': 'disabled',       # 'server', 'client', or 'disabled'
            'REMOTE_AUDIO_HOST': '',               # Server: bind addr; Client: server IP
            'REMOTE_AUDIO_PORT': 9600,
            'REMOTE_AUDIO_DUCK': True,
            'REMOTE_AUDIO_PRIORITY': 3,            # sdr_priority for ducking (configurable)
            'REMOTE_AUDIO_DISPLAY_GAIN': 1.0,
            'REMOTE_AUDIO_AUDIO_BOOST': 1.0,
            'REMOTE_AUDIO_RECONNECT_INTERVAL': 5.0,
            # Announcement Input (port 9601 — inbound PCM stream, PTT to radio)
            'ENABLE_ANNOUNCE_INPUT': False,
            'ANNOUNCE_INPUT_PORT': 9601,
            'ANNOUNCE_INPUT_HOST': '',
            'ANNOUNCE_INPUT_THRESHOLD': -45.0,  # dBFS — below this is treated as silence
            'ANNOUNCE_INPUT_VOLUME': 4.0,       # volume multiplier for announcement audio
            # Relay Control — Radio Power
            'ENABLE_RELAY_RADIO': False,
            'RELAY_RADIO_DEVICE': '/dev/relay_radio',
            'RELAY_RADIO_BAUD': 9600,
            # Relay Control — Charger Schedule
            'ENABLE_RELAY_CHARGER': False,
            'RELAY_CHARGER_DEVICE': '/dev/relay_charger',
            'RELAY_CHARGER_BAUD': 9600,
            'RELAY_CHARGER_ON_TIME': '23:00',
            'RELAY_CHARGER_OFF_TIME': '06:00',
            # TH-9800 CAT Control
            'ENABLE_CAT_CONTROL': False,
            'CAT_HOST': '127.0.0.1',
            'CAT_PORT': 9800,
            'CAT_PASSWORD': '',
            'CAT_LEFT_CHANNEL': -1,     # -1 = don't change
            'CAT_RIGHT_CHANNEL': -1,    # -1 = don't change
            'CAT_LEFT_VOLUME': -1,      # 0-100, -1 = don't change
            'CAT_RIGHT_VOLUME': -1,     # 0-100, -1 = don't change
            'CAT_LEFT_POWER': '',       # L/M/H or blank = don't change
            'CAT_RIGHT_POWER': '',      # L/M/H or blank = don't change
            # Mumble Server 1 (local mumble-server instance)
            'ENABLE_MUMBLE_SERVER_1': False,
            'MUMBLE_SERVER_1_PORT': 64738,
            'MUMBLE_SERVER_1_PASSWORD': '',
            'MUMBLE_SERVER_1_MAX_USERS': 10,
            'MUMBLE_SERVER_1_MAX_BANDWIDTH': 72000,
            'MUMBLE_SERVER_1_WELCOME': 'Welcome to Radio Gateway Server 1',
            'MUMBLE_SERVER_1_REGISTER_NAME': '',
            'MUMBLE_SERVER_1_ALLOW_HTML': True,
            'MUMBLE_SERVER_1_OPUS_THRESHOLD': 0,
            'MUMBLE_SERVER_1_AUTOSTART': True,
            # Mumble Server 2 (second local mumble-server instance)
            'ENABLE_MUMBLE_SERVER_2': False,
            'MUMBLE_SERVER_2_PORT': 64739,
            'MUMBLE_SERVER_2_PASSWORD': '',
            'MUMBLE_SERVER_2_MAX_USERS': 10,
            'MUMBLE_SERVER_2_MAX_BANDWIDTH': 72000,
            'MUMBLE_SERVER_2_WELCOME': 'Welcome to Radio Gateway Server 2',
            'MUMBLE_SERVER_2_REGISTER_NAME': '',
            'MUMBLE_SERVER_2_ALLOW_HTML': True,
            'MUMBLE_SERVER_2_OPUS_THRESHOLD': 0,
            'MUMBLE_SERVER_2_AUTOSTART': True,
        }
        
        # Set defaults
        for key, value in defaults.items():
            setattr(self, key, value)
        
        # Try to load from file
        if not os.path.exists(self.config_file):
            print(f"WARNING: Config file '{self.config_file}' not found, using defaults")
            return
        
        try:
            with open(self.config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    
                    # Parse key = value
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()

                        # Strip surrounding quotes from string values
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                            value = value[1:-1]

                        # Strip inline comments (everything after #)
                        if '#' in value:
                            value = value.split('#')[0].strip()
                        
                        # Skip if value is empty after stripping comments
                        if not value:
                            continue
                        
                        # Convert to appropriate type
                        if key in defaults:
                            default_type = type(defaults[key])
                            
                            if default_type == bool:
                                value = value.lower() in ('true', 'yes', '1', 'on')
                            elif default_type == int:
                                # Handle hex values for VID/PID
                                if value.startswith('0x'):
                                    value = int(value, 16)
                                else:
                                    try:
                                        value = int(value)
                                    except ValueError:
                                        value = float(value)  # allow decimal values for int-defaulted settings
                            elif default_type == float:
                                value = float(value)
                            # else keep as string
                            
                            setattr(self, key, value)
                        else:
                            # Key not in defaults - try to infer type
                            # Try float first (works for both int and float strings)
                            try:
                                if '.' in value:
                                    value = float(value)
                                else:
                                    value = int(value)
                            except ValueError:
                                # Not a number, check for boolean
                                if value.lower() in ('true', 'false', 'yes', 'no', 'on', 'off'):
                                    value = value.lower() in ('true', 'yes', 'on')
                                # else keep as string
                            
                            setattr(self, key, value)
            
            print(f"✓ Configuration loaded from '{self.config_file}'")
            
        except Exception as e:
            print(f"WARNING: Error loading config file: {e}")
            print("Using default values")

# ============================================================================
# AUDIO SOURCE SYSTEM - Multi-Source Support
# ============================================================================

class AudioSource:
    """Base class for all audio sources"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.enabled = True
        self.priority = 0  # Lower = higher priority
        self.volume = 1.0
        self.ptt_control = False  # Can this source trigger PTT?
        
    def initialize(self):
        """Initialize the audio source. Return True on success."""
        return True
    
    def cleanup(self):
        """Clean up resources"""
        pass
    
    def get_audio(self, chunk_size):
        """
        Get audio chunk from this source.
        Returns: (audio_bytes, should_trigger_ptt)
        audio_bytes: PCM audio data or None
        should_trigger_ptt: True if this audio should key PTT
        """
        return None, False
    
    def is_active(self):
        """Return True if source currently has audio to transmit"""
        return False
    
    def get_status(self):
        """Return status string for display"""
        return f"{self.name}: {'ON' if self.enabled else 'OFF'}"


class AIOCRadioSource(AudioSource):
    """Radio audio source via AIOC device"""
    def __init__(self, config, gateway):
        super().__init__("Radio", config)
        self.gateway = gateway  # Reference to main gateway for shared resources
        self.priority = 1  # Lower priority than file playback
        self.ptt_control = False  # Radio RX doesn't control PTT
        self.volume = config.INPUT_VOLUME

        # Queue for audio blobs delivered by PortAudio's callback thread.
        # The ALSA period is opened at 4×AUDIO_CHUNK_SIZE so each callback
        # delivers one 200ms blob.  get_audio() pre-buffers 3 blobs (600ms)
        # before first serve, then slices into 50ms sub-chunks (non-blocking).
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._blob_mult = 4  # ALSA period = 4×AUDIO_CHUNK_SIZE
        self._blob_bytes = config.AUDIO_CHUNK_SIZE * self._blob_mult * config.AUDIO_CHANNELS * 2
        # Pre-compute sizes for the hot callback path and get_audio() slicer.
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * config.AUDIO_CHANNELS * 2  # 16-bit
        self._chunk_secs = config.AUDIO_CHUNK_SIZE / config.AUDIO_RATE           # ~0.05 s
        # Sub-chunk slicing state (accessed only from the get_audio() call site).
        self._sub_buffer = b''
        self._prebuffering = True   # Wait for 3 blobs before first serve
        self._last_blocked_ms = 0.0  # instrumentation: how long get_audio blocked on blob fetch

        # Enhanced trace instrumentation
        self._cb_overflow_count = 0
        self._cb_underflow_count = 0
        self._cb_drop_count = 0
        self._last_cb_status = 0
        self._last_serve_sample = 0
        self._serve_discontinuity = 0.0
        self._sub_buffer_after = 0

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """PortAudio input callback — invoked at each ALSA period (4×AUDIO_CHUNK_SIZE frames).

        Each callback delivers a 200ms blob.  get_audio() pre-buffers 3 blobs
        (600ms cushion) before starting to serve, then slices into 50ms sub-chunks.

        Keep this method minimal — it runs in PortAudio's audio thread."""
        if status:
            self._last_cb_status = status
            if status & 0x2:  # paInputOverflow
                self._cb_overflow_count += 1
            if status & 0x1:  # paInputUnderflow
                self._cb_underflow_count += 1
        if in_data:
            try:
                self._chunk_queue.put_nowait(in_data)
            except _queue_mod.Full:
                self._cb_drop_count += 1
        return (None, pyaudio.paContinue)

    def cleanup(self):
        pass  # No resources to release; PortAudio stream is owned by the gateway

    def get_audio(self, chunk_size):
        """Get audio from radio via AIOC input stream"""
        # Reset the full-duplex cache every call so stale data is never forwarded
        self._rx_cache = None

        if not self.gateway.input_stream or self.gateway.restarting_stream:
            return None, False

        # Mute check BEFORE blob fetch — avoids blocking the main loop for
        # 60ms to get data that would be thrown away.  Flush stale data so
        # the sub-buffer is fresh when unmuted.
        if self.gateway.rx_muted:
            self._sub_buffer = b''
            self._prebuffering = True
            while not self._chunk_queue.empty():
                try:
                    self._chunk_queue.get_nowait()
                except _queue_mod.Empty:
                    break
            self._last_blocked_ms = 0.0
            return None, False

        try:
            # Eagerly drain all available blobs from queue into the sub-buffer.
            # This is critical for the pre-buffer gate: the old loop only fetched
            # when sub_buffer < chunk_bytes, which starved the pre-buffer check.
            cb = self._chunk_bytes
            _t0 = time.monotonic()
            _fetched = False
            while True:
                try:
                    blob = self._chunk_queue.get_nowait()
                    self._sub_buffer += blob
                    _fetched = True
                except _queue_mod.Empty:
                    break
            self._last_blocked_ms = (time.monotonic() - _t0) * 1000 if _fetched else 0.0

            # Cap sub-buffer to prevent stale audio buildup under CPU load.
            if self._blob_bytes > 0 and len(self._sub_buffer) > self._blob_bytes * 5:
                self._sub_buffer = self._sub_buffer[-(self._blob_bytes * 5):]

            # Pre-buffer gate: after the sub-buffer empties, accumulate 3 blobs
            # (600ms cushion) before serving.  This absorbs USB delivery jitter
            # and periodic missed blob deliveries from the AIOC.
            if self._prebuffering:
                if len(self._sub_buffer) < self._blob_bytes * 3:
                    return None, False  # still accumulating
                self._prebuffering = False

            if len(self._sub_buffer) < cb:
                self._prebuffering = True  # depleted — re-enter prebuffer
                return None, False

            data = self._sub_buffer[:cb]
            self._sub_buffer = self._sub_buffer[cb:]
            self._sub_buffer_after = len(self._sub_buffer)

            # Sample discontinuity detection
            if len(data) >= 2:
                first_sample = int.from_bytes(data[0:2], byteorder='little', signed=True)
                self._serve_discontinuity = float(abs(first_sample - self._last_serve_sample))
                self._last_serve_sample = int.from_bytes(data[-2:], byteorder='little', signed=True)

            # Update capture time so stream-health checks stay happy
            self.gateway.last_audio_capture_time = time.time()
            self.gateway.last_successful_read = time.time()
            self.gateway.audio_capture_active = True

            # Calculate audio level (for status display)
            current_level = self.gateway.calculate_audio_level(data)
            if current_level > self.gateway.tx_audio_level:
                self.gateway.tx_audio_level = current_level
            else:
                self.gateway.tx_audio_level = int(self.gateway.tx_audio_level * 0.7 + current_level * 0.3)

            # Apply volume if needed
            if self.volume != 1.0 and data:
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                data = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

            # Apply audio processing
            data = self.gateway.process_audio_for_mumble(data)

            # Apply click-suppression envelope for 150ms after any PTT state change.
            # We use a time-based window (not a one-shot flag) because the sub-chunk
            # slicing in get_audio() means the AIOC transient from the HID write can
            # appear 1-3 sub-chunks after the flag would otherwise be cleared.
            # Gain: 0 for first 30 ms, linearly ramps 0→1 from 30 ms to 130 ms.
            t_since_ptt = time.monotonic() - self.gateway._ptt_change_time
            if t_since_ptt < 0.130 and data:
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                n = len(arr)
                t_samples = t_since_ptt + np.arange(n, dtype=np.float32) / self.config.AUDIO_RATE
                gain = np.clip((t_samples - 0.030) / 0.100, 0.0, 1.0)
                data = (arr * gain).astype(np.int16).tobytes()

            # Cache the processed audio for full-duplex forwarding during PTT.
            # The transmit loop reads this directly so RX → Mumble works even if
            # VAD is blocking and regardless of ptt_active timing in the mixer.
            self._rx_cache = data

            # Check VAD - always call to keep the envelope/state current.
            should_transmit = self.gateway.check_vad(data)

            # Full-duplex: when the gateway is transmitting (PTT active), bypass
            # the VAD gate so radio RX still flows to Mumble via the normal path.
            if self.gateway.ptt_active:
                should_transmit = True

            # During the PTT click-suppression window, force the muted/faded audio
            # through to Mumble even if VAD says no.  Without this, Mumble skips the
            # ~130ms of silence while the speaker plays it, causing a permanent
            # speaker/Mumble sync offset after every PTT event.
            if time.monotonic() - self.gateway._ptt_change_time < 0.130:
                should_transmit = True

            if should_transmit:
                return data, False  # Don't trigger PTT (radio RX)
            else:
                return None, False
                
        except Exception as e:
            # Log the error so we can see what's wrong
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[RadioSource] Error reading audio: {type(e).__name__}: {e}")
            return None, False
    
    def is_active(self):
        """Radio is active if VAD is detecting signal"""
        return self.gateway.vad_active


class FilePlaybackSource(AudioSource):
    """Audio file playback source"""
    def __init__(self, config, gateway):
        super().__init__("FilePlayback", config)
        self.gateway = gateway
        self.priority = 0  # HIGHEST priority - announcements interrupt radio
        self.ptt_control = True  # File playback triggers PTT
        self.volume = getattr(config, 'PLAYBACK_VOLUME', 4.0)
        
        # Playback state
        self.current_file = None
        self.file_data = None
        self.file_position = 0
        self.playlist = []  # Queue of files to play
        
        # Periodic announcement - auto-detect station_id file
        self.last_announcement_time = 0
        self.announcement_interval = config.PLAYBACK_ANNOUNCEMENT_INTERVAL if hasattr(config, 'PLAYBACK_ANNOUNCEMENT_INTERVAL') else 0
        self.announcement_directory = config.PLAYBACK_DIRECTORY if hasattr(config, 'PLAYBACK_DIRECTORY') else './audio/'
        
        # File status tracking for status line indicators (0-9 = 10 files)
        self.file_status = {
            '0': {'exists': False, 'playing': False, 'path': None},  # station_id
            '1': {'exists': False, 'playing': False, 'path': None},
            '2': {'exists': False, 'playing': False, 'path': None},
            '3': {'exists': False, 'playing': False, 'path': None},
            '4': {'exists': False, 'playing': False, 'path': None},
            '5': {'exists': False, 'playing': False, 'path': None},
            '6': {'exists': False, 'playing': False, 'path': None},
            '7': {'exists': False, 'playing': False, 'path': None},
            '8': {'exists': False, 'playing': False, 'path': None},
            '9': {'exists': False, 'playing': False, 'path': None}
        }
        self.check_file_availability()
    
    def check_file_availability(self):
        """Scan audio directory and intelligently load files"""
        import os
        import glob
        
        if not os.path.exists(self.announcement_directory):
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Audio directory not found: {self.announcement_directory}")
            return
        
        # Storage for found files
        file_map = {}  # key -> (filepath, filename)
        
        # Step 1: Look for station_id (key 0)
        # Priority: station_id.mp3 > station_id.wav > station_id.*
        station_id_found = False
        for ext in ['.mp3', '.wav', '.ogg', '.flac', '.m4a']:
            path = os.path.join(self.announcement_directory, f'station_id{ext}')
            if os.path.exists(path):
                file_map['0'] = (path, os.path.basename(path))
                station_id_found = True
                break
        
        # Step 2: Look for numbered files (1_ through 9_)
        # Example: 1_welcome.mp3, 2_emergency.wav, etc.
        all_files = []
        for ext in ['*.mp3', '*.wav', '*.ogg', '*.flac', '*.m4a']:
            all_files.extend(glob.glob(os.path.join(self.announcement_directory, ext)))
        
        # Sort files alphabetically for consistent loading
        all_files.sort()
        
        # First pass: Look for files with number prefixes (1_ through 9_)
        for filepath in all_files:
            filename = os.path.basename(filepath)
            
            # Skip station_id files
            if filename.startswith('station_id'):
                continue
            
            # Check for number prefix (1_ through 9_)
            if len(filename) >= 2 and filename[0].isdigit() and filename[1] == '_':
                key = filename[0]
                if key in '123456789' and key not in file_map:
                    file_map[key] = (filepath, filename)
        
        # Second pass: If slots still empty, fill with any remaining files
        unassigned_files = [f for f in all_files 
                           if os.path.basename(f) not in [v[1] for v in file_map.values()]
                           and not os.path.basename(f).startswith('station_id')]
        
        # Fill empty slots in order (1-9)
        for filepath in unassigned_files:
            # Find next empty slot
            assigned = False
            for slot in range(1, 10):
                key = str(slot)
                if key not in file_map:
                    file_map[key] = (filepath, os.path.basename(filepath))
                    assigned = True
                    break
            
            if not assigned:
                # All slots 1-9 are full
                break
        
        # Step 3: Update file_status with found files
        for key in '0123456789':
            if key in file_map:
                filepath, filename = file_map[key]
                self.file_status[key]['exists'] = True
                self.file_status[key]['path'] = filepath
                self.file_status[key]['filename'] = filename
        
        # Step 4: Print file mapping (will be displayed before status bar)
        self.file_mapping_display = self._generate_file_mapping_display(file_map, station_id_found)
    
    def _generate_file_mapping_display(self, file_map, station_id_found):
        """Generate the file mapping display string"""
        lines = []
        lines.append("=" * 60)
        lines.append("FILE PLAYBACK MAPPING")
        lines.append("=" * 60)
        
        if not file_map:
            lines.append("No audio files found in: " + self.announcement_directory)
            lines.append("Supported formats: .mp3, .wav, .ogg, .flac, .m4a")
            lines.append("")
            lines.append("Naming conventions:")
            lines.append("  station_id.mp3 or station_id.wav  → Key [0]")
            lines.append("  1_filename.mp3                    → Key [1]")
            lines.append("  2_filename.wav                    → Key [2]")
            lines.append("  Or place any audio files and they'll auto-assign to keys 1-9")
            lines.append("=" * 60)
            return "\n".join(lines)
        
        # Show all keys 1-9 then 0 (matching status bar order)
        # Format: "Key [N]: filename.mp3" or "Key [N]: <none>"
        
        # Keys 1-9 - Announcements
        for key in '123456789':
            if key in file_map:
                lines.append(f"Key [{key}]: {file_map[key][1]}")
            else:
                lines.append(f"Key [{key}]: <none>")
        
        # Key 0 - Station ID (at end, matching status bar)
        if '0' in file_map:
            lines.append(f"Key [0]: {file_map['0'][1]}")
        else:
            lines.append(f"Key [0]: <none>")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def print_file_mapping(self):
        """Print the file mapping (call this just before status bar starts)"""
        if hasattr(self, 'file_mapping_display'):
            print(self.file_mapping_display)
    
    def get_file_status_string(self):
        """Get status indicator string for display"""
        # ANSI color codes
        WHITE = '\033[97m'
        GREEN = '\033[92m'
        RED = '\033[91m'
        RESET = '\033[0m'
        
        status_str = ""
        # Show all 10 slots: 1-9 then 0 (station_id at end) - no brackets to save space
        for key in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']:
            if self.file_status[key]['playing']:
                # Red when playing
                status_str += f"{RED}{key}{RESET}"
            elif self.file_status[key]['exists']:
                # Green when file exists
                status_str += f"{GREEN}{key}{RESET}"
            else:
                # White when no file
                status_str += f"{WHITE}{key}{RESET}"
        
        return status_str
        
    def queue_file(self, filepath):
        """Pre-decode an audio file and add it to the playback queue.
        Decoding happens here (caller's thread) so the audio transmit loop
        never blocks on file I/O."""
        import os

        # Check if file exists
        full_path = filepath
        if not os.path.exists(filepath):
            # Try with announcement directory prefix
            alt_path = os.path.join(self.announcement_directory, filepath)
            if os.path.exists(alt_path):
                full_path = alt_path
            else:
                # File not found
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] File not found: {filepath}")
                    print(f"  Looked in: {os.path.abspath(filepath)}")
                    print(f"  Looked in: {os.path.abspath(alt_path)}")
                return False

        # Pre-decode the file now (runs in keyboard/callback thread, not audio thread)
        pcm_bytes = self._decode_file(full_path)
        if pcm_bytes is None:
            return False

        self.playlist.append((full_path, pcm_bytes))
        if self.gateway.config.VERBOSE_LOGGING:
            print(f"\n[Playback] ✓ Queued: {os.path.basename(full_path)} ({len(self.playlist)} in queue)")
        return True

    def load_next_file(self):
        """Activate the next pre-decoded file from the queue (no I/O)."""
        if not self.playlist:
            return False

        filepath, pcm_bytes = self.playlist.pop(0)
        self.file_data = pcm_bytes
        self.file_position = 0
        self.current_file = filepath

        # Mark file as playing in status display
        for key, info in self.file_status.items():
            if info['path'] == filepath:
                self.file_status[key]['playing'] = True
                break

        return True
    
    def stop_playback(self):
        """Stop current playback and clear queue"""
        # Mark current file as not playing
        if self.current_file:
            # Find which key this file belongs to
            for key, info in self.file_status.items():
                if info['path'] == self.current_file:
                    self.file_status[key]['playing'] = False
                    break
        
        # Clear current playback
        self.current_file = None
        self.file_data = None
        self.file_position = 0
        
        # Clear queue
        self.playlist.clear()
        
        if self.gateway.config.VERBOSE_LOGGING:
            print("\n[Playback] ✓ Stopped playback and cleared queue")
    
    def _decode_file(self, filepath):
        """Decode an audio file to PCM bytes.  Returns bytes on success, None on failure.
        Called from queue_file() in the caller's thread so the audio loop never blocks."""
        try:
            import os

            # Get file extension
            file_ext = os.path.splitext(filepath)[1].lower()

            # Try soundfile first (best option for Python 3.13)
            try:
                import soundfile as sf
                import numpy as np

                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] Decoding {os.path.basename(filepath)} (using soundfile)...")

                # Read audio file - soundfile handles MP3 via libsndfile + ffmpeg
                audio_data, sample_rate = sf.read(filepath, dtype='int16')

                # Get file info
                channels = 1 if len(audio_data.shape) == 1 else audio_data.shape[1]
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  Format: {sample_rate}Hz, {channels}ch, 16-bit")

                # Convert stereo to mono if needed
                if channels == 2:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Converting stereo to mono...")
                    audio_data = audio_data.mean(axis=1).astype('int16')
                elif channels > 2:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Converting {channels} channels to mono...")
                    audio_data = audio_data.mean(axis=1).astype('int16')

                # Resample if needed
                if sample_rate != self.config.AUDIO_RATE:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Resampling: {sample_rate}Hz → {self.config.AUDIO_RATE}Hz")
                    try:
                        import resampy
                        # resampy works with float data
                        audio_float = audio_data.astype('float32') / 32768.0
                        audio_resampled = resampy.resample(audio_float, sample_rate, self.config.AUDIO_RATE)
                        audio_data = (audio_resampled * 32768.0).astype('int16')
                    except ImportError:
                        # Fallback: simple linear interpolation
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"    (using basic resampling - install resampy for better quality)")
                        ratio = self.config.AUDIO_RATE / sample_rate
                        new_length = int(len(audio_data) * ratio)
                        indices = (np.arange(new_length) / ratio).astype(int)
                        audio_data = audio_data[indices]

                duration_sec = len(audio_data) / self.config.AUDIO_RATE
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  ✓ Decoded {duration_sec:.1f}s of audio")

                return audio_data.tobytes()

            except ImportError:
                # soundfile not available, try wave module (WAV only)
                if file_ext != '.wav':
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"\n[Playback] Error: {file_ext.upper()} not supported without soundfile")
                        print(f"  Install soundfile for multi-format support:")
                        print(f"    pip install soundfile resampy --break-system-packages")
                        print(f"  Also install system library:")
                        print(f"    sudo apt-get install libsndfile1")
                        print(f"\n  Or convert to WAV:")
                        print(f"    ffmpeg -i {os.path.basename(filepath)} -ar 48000 -ac 1 output.wav")
                    return None

                # Fall back to wave module for WAV files
                import wave

                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] Decoding {os.path.basename(filepath)} (WAV only)...")

                with wave.open(filepath, 'rb') as wf:
                    # Get file info
                    channels = wf.getnchannels()
                    rate = wf.getframerate()
                    width = wf.getsampwidth()
                    frames = wf.getnframes()

                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Format: {rate}Hz, {channels}ch, {width*8}-bit")

                    # Check format compatibility
                    needs_conversion = False

                    if channels != self.config.AUDIO_CHANNELS:
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {channels} channel(s), expected {self.config.AUDIO_CHANNELS}")
                            print(f"    File may not play correctly")
                        needs_conversion = True

                    if rate != self.config.AUDIO_RATE:
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {rate}Hz, expected {self.config.AUDIO_RATE}Hz")
                            print(f"    Audio will play at wrong speed!")
                        needs_conversion = True

                    if width != 2:  # 16-bit = 2 bytes
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {width*8}-bit, expected 16-bit")
                        needs_conversion = True

                    if needs_conversion and self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Convert with: ffmpeg -i {os.path.basename(filepath)} -ar 48000 -ac 1 -sample_fmt s16 output.wav")
                        print(f"  Or install soundfile for automatic conversion")

                    pcm_bytes = wf.readframes(frames)
                    duration_sec = frames / rate
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  ✓ Decoded {duration_sec:.1f}s of audio")

                    return pcm_bytes

        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Error decoding {filepath}: {e}")
            return None
    
    def check_periodic_announcement(self):
        """Check if it's time for a periodic announcement"""
        # Use auto-detected station_id file (key 0)
        if self.announcement_interval <= 0 or not self.file_status['0']['exists']:
            return
        
        current_time = time.time()
        if self.last_announcement_time == 0:
            self.last_announcement_time = current_time
            return
        
        # Check if enough time has passed
        elapsed = current_time - self.last_announcement_time
        if elapsed >= self.announcement_interval:
            # Check if radio is idle
            if not self.gateway.vad_active:
                # Queue the station_id file
                station_id_path = self.file_status['0']['path']
                if station_id_path:
                    self.queue_file(station_id_path)
                    self.last_announcement_time = current_time
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"\n[Playback] Periodic station ID triggered (every {self.announcement_interval}s)")
    
    def get_audio(self, chunk_size):
        """Get audio chunk from file playback"""
        import os
        
        # Check for periodic announcements
        self.check_periodic_announcement()
        
        # If no file is playing, try to load next from queue
        if not self.current_file and self.playlist:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[FilePlayback] Loading file from queue (queue length: {len(self.playlist)})")
            if not self.load_next_file():
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"[FilePlayback] Failed to load file from queue")
                return None, False
            else:
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"[FilePlayback] Successfully loaded: {os.path.basename(self.current_file)}")
        
        # No file playing
        if not self.file_data:
            return None, False

        # Calculate chunk size in bytes (16-bit = 2 bytes per sample)
        chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2

        # During the PTT announcement delay the radio is keying up.  Return silence
        # without advancing the file position so no audio is lost.
        if getattr(self.gateway, 'announcement_delay_active', False):
            return b'\x00' * chunk_bytes, True
        
        # Check if we have enough data left
        if self.file_position >= len(self.file_data):
            # File finished
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Finished: {os.path.basename(self.current_file) if self.current_file else 'unknown'}")
            
            # Reset volume to configured level (in case TTS boosted it)
            self.volume = getattr(self.gateway.config, 'PLAYBACK_VOLUME', 4.0)
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"[Playback] Volume reset to {self.volume}x")
            
            # Mark file as not playing by matching path
            if self.current_file:
                for key, info in self.file_status.items():
                    if info['path'] == self.current_file:
                        self.file_status[key]['playing'] = False
                        break
            
            self.current_file = None
            self.file_data = None
            self.file_position = 0
            
            # Try to load next file
            if self.playlist:
                if not self.load_next_file():
                    return None, False
                # Continue with the new file
            else:
                return None, False
        
        # Get chunk from file
        end_pos = min(self.file_position + chunk_bytes, len(self.file_data))
        chunk = self.file_data[self.file_position:end_pos]
        self.file_position = end_pos
        
        # Pad with silence if chunk is too short
        if len(chunk) < chunk_bytes:
            chunk += b'\x00' * (chunk_bytes - len(chunk))
        
        # Apply volume
        if self.volume != 1.0:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            chunk = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

        # Small yield to prevent file playback from overwhelming other threads
        # (especially important now that we removed priority scheduling)
        import time
        time.sleep(0.001)  # 1ms - negligible latency but helps system balance
        
        # File playback triggers PTT - ALWAYS
        return chunk, True
    
    def is_active(self):
        """Playback is active if file is currently playing"""
        return self.current_file is not None
    
    def get_status(self):
        """Return status string for display"""
        if self.current_file:
            import os
            filename = os.path.basename(self.current_file)
            progress = (self.file_position / len(self.file_data)) * 100 if self.file_data else 0
            return f"{self.name}: Playing {filename} ({progress:.0f}%)"
        elif self.playlist:
            return f"{self.name}: {len(self.playlist)} queued"
        else:
            return f"{self.name}: Idle"


class EchoLinkSource(AudioSource):
    """EchoLink audio input via TheLinkBox IPC"""
    def __init__(self, config, gateway):
        super().__init__("EchoLink", config)
        self.gateway = gateway
        self.priority = 2  # After Radio (1), before Files (0)
        self.ptt_control = False  # EchoLink doesn't trigger radio PTT
        self.volume = 1.0
        
        # IPC state
        self.rx_pipe = None
        self.tx_pipe = None
        self.connected = False
        self.last_audio_time = 0
        
        # Try to setup IPC
        if config.ENABLE_ECHOLINK:
            self.setup_ipc()
    
    def setup_ipc(self):
        """Setup named pipes for TheLinkBox IPC"""
        import os
        import errno
        
        try:
            rx_path = self.config.ECHOLINK_RX_PIPE
            tx_path = self.config.ECHOLINK_TX_PIPE
            
            # Create named pipes if they don't exist
            for pipe_path in [rx_path, tx_path]:
                if not os.path.exists(pipe_path):
                    try:
                        os.mkfifo(pipe_path)
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  Created FIFO: {pipe_path}")
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
            
            # Open pipes (non-blocking mode)
            import fcntl
            
            # RX pipe (read from TheLinkBox)
            self.rx_pipe = open(rx_path, 'rb', buffering=0)
            flags = fcntl.fcntl(self.rx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.rx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            # TX pipe (write to TheLinkBox)
            self.tx_pipe = open(tx_path, 'wb', buffering=0)
            flags = fcntl.fcntl(self.tx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.tx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            self.connected = True
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"  ✓ EchoLink IPC connected via named pipes")
                print(f"    RX: {rx_path}")
                print(f"    TX: {tx_path}")
            
        except Exception as e:
            print(f"  ⚠ EchoLink IPC setup failed: {e}")
            print(f"    Make sure TheLinkBox is running and configured")
            self.connected = False
    
    def get_audio(self, chunk_size):
        """Get audio from EchoLink via named pipe"""
        if not self.connected or not self.rx_pipe:
            return None, False
        
        try:
            chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2  # 16-bit
            data = self.rx_pipe.read(chunk_bytes)
            
            if data and len(data) == chunk_bytes:
                self.last_audio_time = time.time()
                
                # Apply volume
                if self.volume != 1.0:
                    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    data = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

                return data, False  # No PTT control
            else:
                return None, False
                
        except BlockingIOError:
            # No data available (non-blocking read)
            return None, False
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Read error: {e}")
            return None, False
    
    def send_audio(self, audio_data):
        """Send audio to EchoLink via named pipe"""
        if not self.connected or not self.tx_pipe:
            return
        
        try:
            self.tx_pipe.write(audio_data)
            self.tx_pipe.flush()
        except BlockingIOError:
            # Pipe full, skip this chunk
            pass
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Write error: {e}")
    
    def is_active(self):
        """EchoLink is active if we've received audio recently"""
        if not self.connected:
            return False
        return (time.time() - self.last_audio_time) < 2.0
    
    def cleanup(self):
        """Close IPC connections"""
        if self.rx_pipe:
            try:
                self.rx_pipe.close()
            except:
                pass
        if self.tx_pipe:
            try:
                self.tx_pipe.close()
            except:
                pass


class SDRSource(AudioSource):
    """SDR receiver audio input via ALSA loopback"""
    def __init__(self, config, gateway, name="SDR1", sdr_priority=1):
        super().__init__(name, config)
        self.gateway = gateway
        self.priority = 2  # Audio mixer priority (lower than radio/files)
        self.sdr_priority = sdr_priority  # Priority for SDR-to-SDR ducking (1=higher, 2=lower)
        self.ptt_control = False  # SDR doesn't trigger PTT
        self.volume = 1.0
        self.mix_ratio = 1.0  # Volume applied when ducking is disabled
        self.duck = True      # When True: silence SDR if higher priority source is active
        self.enabled = True   # Start enabled by default
        self.muted = False    # Can be muted independently
        
        # Audio stream
        self.input_stream = None
        self.pyaudio = None
        self.audio_level = 0
        self.last_read_time = 0
        
        # Dropout tracking
        self.dropout_count = 0
        self.overflow_count = 0
        self.total_reads = 0
        self.last_stats_time = time.time()

        # Loopback watchdog — detects stalled ALSA reads and attempts recovery
        self._last_successful_read = time.monotonic()
        self._watchdog_restarts = 0
        self._watchdog_stage = 0      # 0=healthy, 1=reopen, 2=reinit pyaudio, 3=reload module
        self._recovering = False
        self._watchdog_gave_up = False

        # PortAudio callback mode — same proven pattern as AIOCRadioSource.
        # The callback fires at each ALSA period (~200ms), queues the blob.
        # get_audio() drains blobs into a sub-buffer and slices into 50ms
        # consumer chunks.  3-blob prebuffer (600ms) absorbs delivery jitter.
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(self, 'sdr_channels', 1) * 2
        self._blob_bytes = 0       # set in setup_audio() once channels/multiplier known
        self._prebuffering = True   # gate: wait for 3 blobs before first serve
        self._last_blocked_ms = 0.0  # instrumentation: how long get_audio blocked on blob fetch
        self._blob_times = collections.deque(maxlen=64)  # instrumentation: reader blob timestamps
        self._plc_total = 0        # instrumentation: kept for trace compatibility

        # Enhanced trace instrumentation
        self._cb_overflow_count = 0   # PortAudio callback reported input overflow
        self._cb_underflow_count = 0  # PortAudio callback reported input underflow
        self._cb_drop_count = 0       # blobs dropped because queue was full
        self._last_cb_status = 0      # last callback status flags
        self._last_serve_sample = 0   # last sample value served (for discontinuity detection)
        self._serve_discontinuity = 0.0  # abs delta between last sample of prev chunk and first of current
        self._sub_buffer_after = 0    # sub-buffer bytes after serving chunk

        if self.config.VERBOSE_LOGGING:
            print(f"[{self.name}] Initializing SDR audio source...")
    
    def setup_audio(self):
        """Initialize SDR audio input from ALSA loopback"""
        try:
            import pyaudio
            self.pyaudio = pyaudio.PyAudio()
            
            # Find the SDR loopback device
            device_index = None
            device_name = None
            
            # Determine which config parameter to use based on SDR name
            if self.name == "SDR2":
                config_device_attr = 'SDR2_DEVICE_NAME'
                config_buffer_attr = 'SDR2_BUFFER_MULTIPLIER'
            else:  # SDR1 or legacy "SDR"
                config_device_attr = 'SDR_DEVICE_NAME'
                config_buffer_attr = 'SDR_BUFFER_MULTIPLIER'
            
            if hasattr(self.config, config_device_attr) and getattr(self.config, config_device_attr):
                # User specified a device name
                target_name = getattr(self.config, config_device_attr)
                
                if self.config.VERBOSE_LOGGING:
                    print(f"[{self.name}] Searching for device matching: {target_name}")
                    print(f"[{self.name}] Available input devices:")
                
                # Search for matching device
                for i in range(self.pyaudio.get_device_count()):
                    info = self.pyaudio.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        if self.config.VERBOSE_LOGGING:
                            print(f"[{self.name}]   [{i}] {info['name']} (in:{info['maxInputChannels']})")
                        
                        # Match by name substring OR by hw device number
                        # Examples:
                        #   "Loopback" matches "Loopback: PCM (hw:2,0)"
                        #   "hw:2,0" matches "Loopback: PCM (hw:2,0)"
                        #   "hw:Loopback,2,0" extracts "hw:2,0" and matches
                        name_lower = info['name'].lower()
                        
                        # Extract hw device from target if format is hw:Name,X,Y
                        if target_name.startswith('hw:') and ',' in target_name:
                            # Extract just the hw:X,Y part (skip the name)
                            parts = target_name.split(',')
                            if len(parts) >= 2:
                                # hw:Loopback,2,0 -> look for hw:2,0
                                hw_device = f"hw:{parts[-2]},{parts[-1]}"
                                if hw_device in name_lower:
                                    device_index = i
                                    device_name = info['name']
                                    break
                        
                        # Simple substring match
                        if target_name.lower() in name_lower:
                            device_index = i
                            device_name = info['name']
                            break
            
            if device_index is None:
                print(f"[{self.name}] ✗ SDR device not found")
                if hasattr(self.config, config_device_attr):
                    print(f"[{self.name}]   Looked for: {getattr(self.config, config_device_attr)}")
                    print(f"[{self.name}]   Try one of these formats:")
                    print(f"[{self.name}]     {config_device_attr} = Loopback")
                    print(f"[{self.name}]     {config_device_attr} = hw:2,0")
                    print(f"[{self.name}]   Or enable VERBOSE_LOGGING to see all devices")
                return False
            
            # Open input stream in CALLBACK mode — same pattern as AIOC.
            # PortAudio fires _sdr_callback at each ALSA period (~200ms),
            # delivering one blob.  No reader thread, no blocking reads.
            # get_audio() slices blobs into 50ms chunks via sub-buffer.
            buffer_multiplier = getattr(self.config, config_buffer_attr, 4)
            buffer_size = self.config.AUDIO_CHUNK_SIZE * buffer_multiplier

            # Auto-detect supported channel count
            # Try stereo first, fall back to mono
            device_info = self.pyaudio.get_device_info_by_index(device_index)
            max_channels = device_info['maxInputChannels']

            # Use 2 channels if supported (stereo), otherwise use 1 (mono)
            sdr_channels = min(2, max_channels)

            try:
                self.input_stream = self.pyaudio.open(
                    format=pyaudio.paInt16,
                    channels=sdr_channels,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=buffer_size,
                    stream_callback=self._sdr_callback
                )
                self.sdr_channels = sdr_channels  # Store for later use
                self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
                self._blob_bytes = self._chunk_bytes * buffer_multiplier
            except Exception as e:
                # If 2 channels failed, try 1 channel
                if sdr_channels == 2:
                    if self.config.VERBOSE_LOGGING:
                        print(f"[{self.name}] Stereo failed, trying mono...")
                    sdr_channels = 1
                    self.input_stream = self.pyaudio.open(
                        format=pyaudio.paInt16,
                        channels=sdr_channels,
                        rate=self.config.AUDIO_RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=buffer_size,
                        stream_callback=self._sdr_callback
                    )
                    self.sdr_channels = sdr_channels
                    self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
                    self._blob_bytes = self._chunk_bytes * buffer_multiplier
                else:
                    raise

            # Start the stream explicitly (callback mode)
            if not self.input_stream.is_active():
                self.input_stream.start_stream()

            if self.config.VERBOSE_LOGGING:
                print(f"[{self.name}] ✓ Audio input configured: {device_name}")
                print(f"[{self.name}]   Channels: {sdr_channels} ({'stereo' if sdr_channels == 2 else 'mono'})")
                period_ms = buffer_size / self.config.AUDIO_RATE * 1000
                print(f"[{self.name}]   Callback mode: {buffer_size} frames ({period_ms:.0f}ms per period)")

            # Flush any stale data
            while not self._chunk_queue.empty():
                try:
                    self._chunk_queue.get_nowait()
                except _queue_mod.Empty:
                    break

            # Wait for initial blobs to arrive via callback (3 blobs = 600ms)
            prefill_deadline = time.monotonic() + 2.0
            while self._chunk_queue.qsize() < 3 and time.monotonic() < prefill_deadline:
                time.sleep(0.01)

            if self.config.VERBOSE_LOGGING:
                print(f"[{self.name}] ✓ Callback stream active (queue: {self._chunk_queue.qsize()} blobs)")

            return True
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"[{self.name}] ✗ Failed to setup audio: {e}")
            return False
    
    def _sdr_callback(self, in_data, frame_count, time_info, status):
        """PortAudio input callback — fires at each ALSA period.

        Identical pattern to AIOCRadioSource._audio_callback.
        Keep minimal — runs in PortAudio's audio thread."""
        if status:
            self._last_cb_status = status
            if status & 0x2:  # paInputOverflow
                self._cb_overflow_count += 1
            if status & 0x1:  # paInputUnderflow
                self._cb_underflow_count += 1
        if in_data:
            _now = time.monotonic()
            self._last_successful_read = _now
            self._blob_times.append(_now)
            try:
                self._chunk_queue.put_nowait(in_data)
            except _queue_mod.Full:
                self._cb_drop_count += 1
        return (None, pyaudio.paContinue)

    def get_audio(self, chunk_size):
        """Get processed audio from SDR receiver.

        Same proven pattern as AIOCRadioSource:
        1. Eagerly drain all blobs from reader queue into sub-buffer
        2. Cap sub-buffer to prevent latency buildup
        3. Pre-buffer gate: wait for 3 blobs (600ms) before first serve
        4. Serve one 50ms chunk; if depleted, re-enter prebuffer
        """
        if not self.enabled:
            return None, False

        if not self.input_stream:
            return None, False

        cb = self._chunk_bytes

        # Eagerly drain all blobs from queue into sub-buffer, smoothing
        # the junction to eliminate sample discontinuity clicks.
        # IMPORTANT: No data is removed — both sub-buffer tail and blob
        # head are modified in-place so the buffer doesn't shrink over time.
        _t0 = time.monotonic()
        _fetched = False
        _SMOOTH = 16  # samples to taper on each side of junction (~0.33ms)
        _SMOOTH_BYTES = _SMOOTH * 2  # int16 = 2 bytes per sample
        while True:
            try:
                blob = self._chunk_queue.get_nowait()
                # Smooth junction: taper last N samples of sub-buffer and
                # first N samples of new blob toward their shared midpoint.
                # This eliminates clicks without removing any data.
                if self._sub_buffer and len(blob) >= _SMOOTH_BYTES and len(self._sub_buffer) >= _SMOOTH_BYTES:
                    # Get the boundary samples
                    last_sample = int.from_bytes(self._sub_buffer[-2:], 'little', signed=True)
                    first_sample = int.from_bytes(blob[0:2], 'little', signed=True)
                    jump = abs(last_sample - first_sample)
                    # Only smooth if there's a significant discontinuity
                    if jump > 500:
                        mid = (last_sample + first_sample) / 2.0
                        # Taper tail of sub-buffer toward midpoint
                        tail_arr = np.frombuffer(self._sub_buffer[-_SMOOTH_BYTES:], dtype=np.int16).copy().astype(np.float32)
                        w = np.linspace(0.0, 1.0, len(tail_arr), dtype=np.float32)
                        tail_arr = tail_arr * (1.0 - w) + mid * w
                        self._sub_buffer = self._sub_buffer[:-_SMOOTH_BYTES] + np.clip(tail_arr, -32768, 32767).astype(np.int16).tobytes()
                        # Taper head of blob from midpoint
                        head_arr = np.frombuffer(blob[:_SMOOTH_BYTES], dtype=np.int16).copy().astype(np.float32)
                        w = np.linspace(0.0, 1.0, len(head_arr), dtype=np.float32)
                        head_arr = mid * (1.0 - w) + head_arr * w
                        blob = np.clip(head_arr, -32768, 32767).astype(np.int16).tobytes() + blob[_SMOOTH_BYTES:]
                self._sub_buffer += blob
                _fetched = True
            except _queue_mod.Empty:
                break
        self._last_blocked_ms = (time.monotonic() - _t0) * 1000 if _fetched else 0.0

        # Cap sub-buffer to prevent stale audio buildup under CPU load.
        if self._blob_bytes > 0 and len(self._sub_buffer) > self._blob_bytes * 5:
            self._sub_buffer = self._sub_buffer[-(self._blob_bytes * 5):]

        # Pre-buffer gate: after depletion, accumulate 1 full blob worth
        # of data before serving.  This provides ~200ms cushion (4 consumer
        # chunks) which absorbs normal ALSA delivery jitter.  The crossfade
        # can leave a partial-blob residue, so using blob_bytes (not 2×) as
        # the threshold avoids over-waiting.
        if self._prebuffering:
            if self._blob_bytes > 0 and len(self._sub_buffer) < self._blob_bytes:
                return None, False  # still accumulating
            self._prebuffering = False

        if len(self._sub_buffer) < cb:
            self._prebuffering = True  # depleted — re-enter prebuffer
            return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]
        self._sub_buffer_after = len(self._sub_buffer)

        # Sample discontinuity detection: compare last sample of previous
        # chunk to first sample of this chunk.  Large jumps cause clicks.
        if len(raw) >= 2:
            first_sample = int.from_bytes(raw[0:2], byteorder='little', signed=True)
            delta = abs(first_sample - self._last_serve_sample)
            self._serve_discontinuity = float(delta)
            # Update last sample (last 2 bytes of raw, which is stereo or mono)
            self._last_serve_sample = int.from_bytes(raw[-2:], byteorder='little', signed=True)

        # Muted: chunk was sliced (keeps sub-buffer fresh), discard it.
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        self.total_reads += 1
        self.last_read_time = time.time()

        # Stereo→mono (all numpy processing happens here, not in reader thread)
        arr = np.frombuffer(raw, dtype=np.int16)
        if hasattr(self, 'sdr_channels') and self.sdr_channels == 2 and len(arr) >= 2:
            stereo = arr.reshape(-1, 2).astype(np.int32)
            arr = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(np.int16)
            raw = arr.tobytes()

        # Level metering and audio boost
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = getattr(self.gateway.config, 'SDR_DISPLAY_GAIN', 1.0)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            audio_boost = getattr(self.gateway.config, 'SDR_AUDIO_BOOST', 1.0)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        return raw, False  # SDR never triggers PTT

    def is_active(self):
        """SDR is active if enabled and receiving audio"""
        return self.enabled and not self.muted and self.input_stream is not None
    
    def get_status(self):
        """Return status string"""
        if not self.enabled:
            return "SDR: Disabled"
        elif self.muted:
            return "SDR: Muted"
        else:
            return f"SDR: Active ({self.audio_level}%)"
    
    def cleanup(self):
        """Close SDR audio stream"""
        self._sub_buffer = b''

        if self.input_stream:
            try:
                # Stop stream first to prevent ALSA errors
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                time.sleep(0.05)  # Give ALSA time to clean up buffers
                self.input_stream.close()
            except Exception:
                pass  # Suppress ALSA errors during shutdown
        if self.pyaudio:
            try:
                self.pyaudio.terminate()
            except Exception:
                pass  # Suppress errors

    def _stop_reader(self):
        """Stop the callback stream and clear buffers."""
        # Callback stops automatically when stream is stopped/closed
        self._sub_buffer = b''
        self._prebuffering = True  # rebuild cushion on next start
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break

    def _close_stream(self):
        """Close the ALSA input stream safely."""
        if self.input_stream:
            try:
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                time.sleep(0.05)
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None

    def _start_reader(self):
        """Start the callback stream and wait for initial blobs."""
        # Callback fires automatically once stream is active
        if self.input_stream and not self.input_stream.is_active():
            self.input_stream.start_stream()

        # Wait for 3 blobs (600ms) — matches get_audio() prebuffer gate
        prefill_deadline = time.monotonic() + 2.0
        while self._chunk_queue.qsize() < 3 and time.monotonic() < prefill_deadline:
            time.sleep(0.01)

    def _find_device(self):
        """Find the SDR ALSA device. Returns (device_index, device_name) or (None, None)."""
        if self.name == "SDR2":
            config_device_attr = 'SDR2_DEVICE_NAME'
        else:
            config_device_attr = 'SDR_DEVICE_NAME'

        target_name = getattr(self.config, config_device_attr, '')
        if not target_name:
            return None, None

        for i in range(self.pyaudio.get_device_count()):
            info = self.pyaudio.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                name_lower = info['name'].lower()
                # Extract hw device from target if format is hw:Name,X,Y
                if target_name.startswith('hw:') and ',' in target_name:
                    parts = target_name.split(',')
                    if len(parts) >= 2:
                        hw_device = f"hw:{parts[-2]},{parts[-1]}"
                        if hw_device in name_lower:
                            return i, info['name']
                if target_name.lower() in name_lower:
                    return i, info['name']

        return None, None

    def _open_stream(self, device_index):
        """Open ALSA input stream on given device. Returns True on success."""
        import pyaudio
        if self.name == "SDR2":
            config_buffer_attr = 'SDR2_BUFFER_MULTIPLIER'
        else:
            config_buffer_attr = 'SDR_BUFFER_MULTIPLIER'
        buffer_multiplier = getattr(self.config, config_buffer_attr, 4)
        buffer_size = self.config.AUDIO_CHUNK_SIZE * buffer_multiplier

        device_info = self.pyaudio.get_device_info_by_index(device_index)
        max_channels = device_info['maxInputChannels']
        sdr_channels = min(2, max_channels)

        try:
            self.input_stream = self.pyaudio.open(
                format=pyaudio.paInt16,
                channels=sdr_channels,
                rate=self.config.AUDIO_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=buffer_size,
                stream_callback=self._sdr_callback
            )
            self.sdr_channels = sdr_channels
            self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
            self._blob_bytes = self._chunk_bytes * buffer_multiplier
            return True
        except Exception:
            if sdr_channels == 2:
                try:
                    sdr_channels = 1
                    self.input_stream = self.pyaudio.open(
                        format=pyaudio.paInt16,
                        channels=sdr_channels,
                        rate=self.config.AUDIO_RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=buffer_size,
                        stream_callback=self._sdr_callback
                    )
                    self.sdr_channels = sdr_channels
                    self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
                    self._blob_bytes = self._chunk_bytes * buffer_multiplier
                    return True
                except Exception:
                    return False
            return False

    def _restart_stream(self, stage):
        """Attempt staged recovery of the ALSA loopback.

        Stage 1: Reopen stream (close + reopen ALSA device)
        Stage 2: Reinitialize PyAudio entirely
        Stage 3: Reload snd-aloop kernel module (requires SDR_WATCHDOG_MODPROBE=true)

        Returns True on success, False on failure.
        """
        import pyaudio as _pyaudio_mod

        if stage == 1:
            print(f"\n[{self.name}] Watchdog: stage 1 recovery — reopening ALSA stream")
            try:
                self._stop_reader()
                self._close_stream()
                time.sleep(0.2)  # ALSA settle
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 1 failed — device not found")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 1 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 1 success — stream reopened ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 1 failed — {e}")
                return False

        elif stage == 2:
            print(f"\n[{self.name}] Watchdog: stage 2 recovery — reinitializing PyAudio")
            try:
                self._stop_reader()
                self._close_stream()
                if self.pyaudio:
                    try:
                        self.pyaudio.terminate()
                    except Exception:
                        pass
                time.sleep(0.5)
                self.pyaudio = _pyaudio_mod.PyAudio()
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 2 failed — device not found")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 2 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 2 success — PyAudio reinitialized ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 2 failed — {e}")
                return False

        elif stage == 3:
            if self.name == "SDR2":
                modprobe_enabled = getattr(self.config, 'SDR2_WATCHDOG_MODPROBE', False)
            else:
                modprobe_enabled = getattr(self.config, 'SDR_WATCHDOG_MODPROBE', False)
            if not modprobe_enabled:
                return False

            print(f"\n[{self.name}] Watchdog: stage 3 recovery — reloading snd-aloop kernel module")
            try:
                import subprocess
                self._stop_reader()
                self._close_stream()
                if self.pyaudio:
                    try:
                        self.pyaudio.terminate()
                    except Exception:
                        pass
                    self.pyaudio = None

                result = subprocess.run(['sudo', 'modprobe', '-r', 'snd-aloop'],
                                        timeout=10, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"[{self.name}] Watchdog: modprobe -r failed: {result.stderr.strip()}")
                    # Continue anyway — module may not have been loaded
                time.sleep(1.0)

                result = subprocess.run(['sudo', 'modprobe', 'snd-aloop'],
                                        timeout=10, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"[{self.name}] Watchdog: modprobe load failed: {result.stderr.strip()}")
                    return False
                time.sleep(1.0)  # Wait for devices to re-appear

                self.pyaudio = _pyaudio_mod.PyAudio()
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 3 failed — device not found after reload")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 3 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 3 success — module reloaded ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 3 failed — {e}")
                return False

        return False

    def check_watchdog(self):
        """Check for stalled ALSA reads and attempt staged recovery.

        Called from status_monitor_loop (~once per second).
        """
        if self._recovering or self._watchdog_gave_up:
            return

        if self.name == "SDR2":
            timeout = getattr(self.config, 'SDR2_WATCHDOG_TIMEOUT', 10)
            max_restarts = getattr(self.config, 'SDR2_WATCHDOG_MAX_RESTARTS', 5)
        else:
            timeout = getattr(self.config, 'SDR_WATCHDOG_TIMEOUT', 10)
            max_restarts = getattr(self.config, 'SDR_WATCHDOG_MAX_RESTARTS', 5)

        elapsed = time.monotonic() - self._last_successful_read
        if elapsed < timeout:
            self._watchdog_stage = 0  # healthy
            return

        if self._watchdog_restarts >= max_restarts:
            if not self._watchdog_gave_up:
                print(f"\n[{self.name}] Watchdog: gave up after {max_restarts} recovery attempts")
                self._watchdog_gave_up = True
            return

        self._recovering = True
        try:
            # Try stages in order until one succeeds
            for stage in (1, 2, 3):
                if self._restart_stream(stage):
                    self._watchdog_restarts += 1
                    self._last_successful_read = time.monotonic()
                    self._watchdog_stage = 0
                    return
            # All stages failed
            self._watchdog_restarts += 1
            print(f"[{self.name}] Watchdog: all recovery stages failed (attempt {self._watchdog_restarts}/{max_restarts})")
        finally:
            self._recovering = False


class PipeWireSDRSource(SDRSource):
    """SDR audio input via PipeWire virtual sink monitor.

    Instead of reading from an ALSA loopback device (which delivers audio in
    high-jitter 200ms blobs), this source reads from a PipeWire virtual sink's
    monitor via FFmpeg subprocess.  PipeWire delivers a continuous, low-jitter
    stream — no blob boundaries, no prebuffering gaps, no crossfade needed.

    Config: set SDR_DEVICE_NAME = pw:<sink_name> (e.g. pw:sdr_capture)
    The sink must exist (created via pw-cli or startup script) and the SDR
    app's output must be routed to it.
    """

    def __init__(self, config, gateway, name="SDR1", sdr_priority=1):
        super().__init__(config, gateway, name=name, sdr_priority=sdr_priority)
        self._ffmpeg_proc = None
        self._reader_thread = None
        self._reader_running = False
        self._pw_sink_name = None  # set in setup_audio

    def setup_audio(self):
        """Start FFmpeg subprocess reading from PipeWire monitor."""
        import subprocess as _sp

        # Determine sink name from config
        if self.name == "SDR2":
            device_cfg = getattr(self.config, 'SDR2_DEVICE_NAME', '')
        else:
            device_cfg = getattr(self.config, 'SDR_DEVICE_NAME', '')

        # Strip pw: or pipewire: prefix
        if device_cfg.lower().startswith('pw:'):
            self._pw_sink_name = device_cfg[3:]
        elif device_cfg.lower().startswith('pipewire:'):
            self._pw_sink_name = device_cfg[9:]
        else:
            self._pw_sink_name = device_cfg

        monitor_name = f"{self._pw_sink_name}.monitor"

        # Verify the monitor source exists, auto-create sink if missing
        try:
            result = _sp.run(['pactl', 'list', 'short', 'sources'],
                             capture_output=True, text=True, timeout=5)
            if monitor_name not in result.stdout:
                print(f"[{self.name}] PipeWire monitor '{monitor_name}' not found, creating sink '{self._pw_sink_name}'...")
                create_result = _sp.run([
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
                # Wait for PipeWire to register the new monitor
                time.sleep(1)
                result = _sp.run(['pactl', 'list', 'short', 'sources'],
                                 capture_output=True, text=True, timeout=5)
                if monitor_name not in result.stdout:
                    print(f"[{self.name}] Sink created but monitor '{monitor_name}' still not found")
                    print(f"[{self.name}]   Available sources:")
                    for line in result.stdout.strip().split('\n'):
                        if 'monitor' in line.lower():
                            print(f"[{self.name}]     {line}")
                    return False
                print(f"[{self.name}] Sink '{self._pw_sink_name}' created successfully")
        except Exception as e:
            print(f"[{self.name}] Failed to check PipeWire sources: {e}")
            return False

        # Set channel info — PipeWire sink is stereo
        self.sdr_channels = 2
        self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * self.sdr_channels * 2
        self._blob_bytes = self._chunk_bytes  # no blob concept, but keep for trace compat

        # Start FFmpeg reading from PipeWire monitor
        try:
            self._ffmpeg_proc = _sp.Popen([
                'ffmpeg', '-loglevel', 'error',
                '-f', 'pulse', '-i', monitor_name,
                '-f', 's16le', '-ar', str(self.config.AUDIO_RATE),
                '-ac', '2',
                'pipe:1'
            ], stdout=_sp.PIPE, stderr=_sp.PIPE)
        except FileNotFoundError:
            print(f"[{self.name}] ffmpeg not found — required for PipeWire SDR source")
            return False
        except Exception as e:
            print(f"[{self.name}] Failed to start FFmpeg: {e}")
            return False

        # Reader thread: reads fixed-size chunks from FFmpeg stdout and queues them
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._pw_reader_loop, daemon=True, name=f"{self.name}-pw-reader")
        self._reader_thread.start()

        # Wait briefly for first data
        _deadline = time.monotonic() + 2.0
        while self._chunk_queue.qsize() < 2 and time.monotonic() < _deadline:
            time.sleep(0.01)

        # Set input_stream to a truthy sentinel so the rest of the gateway
        # knows this source is active (many checks do `if source.input_stream:`)
        self.input_stream = True  # sentinel, not a real stream object

        if self._chunk_queue.qsize() > 0:
            self._prebuffering = False  # no prebuffering needed for PipeWire
            print(f"[{self.name}] PipeWire source active (monitor: {monitor_name})")
            return True
        else:
            print(f"[{self.name}] No audio received from PipeWire after 2s")
            self._reader_running = False
            if self._ffmpeg_proc:
                self._ffmpeg_proc.kill()
            return False

    def _pw_reader_loop(self):
        """Read fixed-size chunks from FFmpeg stdout and queue them."""
        chunk_bytes = self._chunk_bytes  # 50ms stereo = 9600 bytes
        proc = self._ffmpeg_proc
        while self._reader_running and proc and proc.poll() is None:
            try:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                if len(data) < chunk_bytes:
                    # Short read — pad with silence (shouldn't happen normally)
                    data += b'\x00' * (chunk_bytes - len(data))
                self._last_successful_read = time.monotonic()
                self._blob_times.append(time.monotonic())
                try:
                    self._chunk_queue.put_nowait(data)
                except _queue_mod.Full:
                    # Drop oldest to keep buffer fresh
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

    def get_audio(self, chunk_size):
        """Get one chunk from PipeWire stream.

        Takes exactly ONE chunk per call. The reader thread delivers ~1 chunk
        per 50ms and the transmit loop consumes ~1 per 50ms. Taking only one
        keeps a small queue cushion so the next tick always has data.
        Only drain extras if queue grows beyond 4 (latency cap).
        """
        if not self.enabled or not self._reader_running:
            return None, False

        # Take one chunk (leave the rest as cushion for next tick)
        data = None
        try:
            data = self._chunk_queue.get_nowait()
        except _queue_mod.Empty:
            pass

        # If queue is building up (>4), drain extras to cap latency at ~200ms
        if data is not None:
            qsz = self._chunk_queue.qsize()
            while qsz > 4:
                try:
                    data = self._chunk_queue.get_nowait()
                    qsz -= 1
                except _queue_mod.Empty:
                    break

        self._last_blocked_ms = 0.0  # no blocking in PipeWire mode

        if data is None:
            return None, False

        cb = self._chunk_bytes

        # Muted: consume but discard
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        self.total_reads += 1
        self.last_read_time = time.time()

        raw = data

        # Stereo→mono FIRST (before discontinuity tracking)
        arr = np.frombuffer(raw, dtype=np.int16)
        if self.sdr_channels == 2 and len(arr) >= 2:
            stereo = arr.reshape(-1, 2).astype(np.int32)
            arr = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(np.int16)
            raw = arr.tobytes()

        # Sample discontinuity tracking (on mono data)
        if len(raw) >= 2:
            first_sample = int.from_bytes(raw[0:2], byteorder='little', signed=True)
            delta = abs(first_sample - self._last_serve_sample)
            self._serve_discontinuity = float(delta)
            self._last_serve_sample = int.from_bytes(raw[-2:], byteorder='little', signed=True)

        self._sub_buffer_after = self._chunk_queue.qsize() * cb  # approx remaining

        # Level metering and audio boost
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = getattr(self.gateway.config, 'SDR_DISPLAY_GAIN', 1.0)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            audio_boost = getattr(self.gateway.config, 'SDR_AUDIO_BOOST', 1.0)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        return raw, False

    def cleanup(self):
        """Stop FFmpeg and reader thread."""
        self._reader_running = False
        self.input_stream = None
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait(timeout=2)
            except Exception:
                pass
            self._ffmpeg_proc = None

    def _stop_reader(self):
        """Stop the reader and clear queue."""
        self._reader_running = False
        self._prebuffering = True
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break

    def _start_reader(self):
        """Restart reader after stop."""
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            self._reader_running = True
            if not self._reader_thread or not self._reader_thread.is_alive():
                self._reader_thread = threading.Thread(
                    target=self._pw_reader_loop, daemon=True, name=f"{self.name}-pw-reader")
                self._reader_thread.start()

    def _close_stream(self):
        """Close the FFmpeg stream."""
        self.cleanup()

    def _find_device(self):
        """Not used for PipeWire source."""
        return None, None

    def _watchdog_recover(self, max_restarts):
        """Restart FFmpeg if it died."""
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is not None:
            print(f"[{self.name}] PipeWire: FFmpeg process died, restarting...")
            self.cleanup()
            if self.setup_audio():
                print(f"[{self.name}] PipeWire: recovered")
            else:
                print(f"[{self.name}] PipeWire: recovery failed")


class RemoteAudioServer:
    """Connects out to a remote client and sends mixed audio over TCP.

    REMOTE_AUDIO_HOST = destination IP of the client machine.
    The server initiates the TCP connection and pushes length-prefixed PCM.
    Reconnects automatically if the link drops.
    """
    def __init__(self, config):
        self.config = config
        self.host = config.REMOTE_AUDIO_HOST
        self.port = int(config.REMOTE_AUDIO_PORT)
        self.connected = False
        self.client_address = None  # "host:port" when connected
        self._socket = None
        self._connect_thread = None
        self._running = False
        self._reconnect_interval = float(getattr(config, 'REMOTE_AUDIO_RECONNECT_INTERVAL', 5.0))

    def start(self):
        """Spawn connection thread that connects out to the client."""
        if not self.host:
            print("⚠ REMOTE_AUDIO_HOST not set — server has no destination to connect to")
            return
        self._running = True
        self._connect_thread = threading.Thread(
            target=self._connect_loop, name="RemoteAudio-connect", daemon=True
        )
        self._connect_thread.start()
        print(f"✓ Remote audio server will connect to {self.host}:{self.port}")

    def _connect_loop(self):
        """Connect to the client, reconnect on failure."""
        import socket
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((self.host, self.port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setblocking(False)  # non-blocking so send_audio never stalls audio loop
                self._socket = sock
                self.client_address = f"{self.host}:{self.port}"
                self.connected = True
                print(f"\n[RemoteAudio] Connected to client {self.client_address}")
                # Stay in this loop until disconnect is detected.
                # Probe the socket every 0.5s with select() — if the remote
                # end closes, the socket becomes readable (recv returns b'').
                # This catches disconnects even when send_audio() isn't called
                # (e.g. VAD gating all audio as silence).
                while self._running and self.connected:
                    try:
                        import select as _sel
                        readable, _, _ = _sel.select([sock], [], [], 0.5)
                        if readable:
                            # Socket readable on a send-only link = remote closed
                            probe = sock.recv(1)
                            if not probe:
                                break  # clean close
                    except Exception:
                        break  # error = dead
            except Exception:
                pass
            finally:
                self.connected = False
                self.client_address = None
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
            if self._running:
                time.sleep(self._reconnect_interval)

    def send_audio(self, pcm_data):
        """Send length-prefixed PCM to connected client.
        Uses non-blocking send to avoid stalling the audio transmit loop
        if the TCP buffer is full (e.g. slow client or network hiccup)."""
        sock = self._socket
        if not sock:
            return
        import struct
        try:
            frame = struct.pack('>I', len(pcm_data)) + pcm_data
            total = len(frame)
            sent = 0
            while sent < total:
                try:
                    n = sock.send(frame[sent:])
                    if n == 0:
                        raise ConnectionError("send returned 0")
                    sent += n
                except BlockingIOError:
                    # Socket buffer full — drop the rest of this frame
                    # rather than blocking the audio loop
                    break
        except Exception:
            # Link broken — trigger reconnect
            self.connected = False
            self._socket = None
            try:
                sock.close()
            except Exception:
                pass

    def reset(self):
        """Force-close the current connection so _connect_loop reconnects."""
        sock = self._socket
        self._socket = None
        self.connected = False
        self.client_address = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def cleanup(self):
        """Close socket."""
        self._running = False
        self.connected = False
        sock = self._socket
        self._socket = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


class RemoteAudioSource(AudioSource):
    """Listens for a TCP connection from a RemoteAudioServer and receives audio.

    REMOTE_AUDIO_HOST = bind address ('' or unset → 0.0.0.0, all interfaces).
    The server connects in; this end accepts and reads length-prefixed PCM.

    Name starts with 'SDR' so the mixer's duck system automatically handles it
    the same way it handles SDR1/SDR2 sources.
    """
    def __init__(self, config, gateway):
        super().__init__("SDRSV", config)
        self.gateway = gateway
        self.priority = 2  # Same as SDR sources in the mixer
        self.sdr_priority = int(config.REMOTE_AUDIO_PRIORITY)
        self.ptt_control = False
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = config.REMOTE_AUDIO_DUCK
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.server_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None
        self._conn = None  # current accepted connection (for reset)

    def setup_audio(self):
        """Bind listen socket and start the reader/accept thread."""
        import socket
        bind_host = self.config.REMOTE_AUDIO_HOST or '0.0.0.0'
        port = int(self.config.REMOTE_AUDIO_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="SDRSV-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Remote audio client listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept connections from the server and read length-prefixed PCM."""
        import socket, struct

        while self._reader_running:
            # Wait for the server to connect in
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self._conn = conn
                self.server_connected = True
                print(f"\n[SDRSV] Server connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    # Read 4-byte length header
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break  # sanity check
                    # Read PCM payload
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break
                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        # Drop oldest to keep queue fresh
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[SDRSV] Connection error: {e}")
            finally:
                self.server_connected = False
                self._conn = None
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def reset(self):
        """Force-close the current connection so the reader thread re-accepts."""
        conn = self._conn
        self._conn = None
        self.server_connected = False
        self._sub_buffer = b''
        # Drain the queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes from socket, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Drain queue, slice sub-buffer, level metering, audio boost."""
        if not self.enabled:
            return None, False

        # Skip queue lock entirely when not connected — nothing to drain
        if not self.server_connected and not self._sub_buffer:
            return None, False

        cb = self._chunk_bytes

        # Fill sub-buffer from queue
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Muted: keep draining but discard
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering and audio boost
        arr = np.frombuffer(raw, dtype=np.int16)
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = float(self.config.REMOTE_AUDIO_DISPLAY_GAIN)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            audio_boost = float(self.config.REMOTE_AUDIO_AUDIO_BOOST)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        return raw, False  # Never triggers PTT

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "SDRSV: Disabled"
        elif self.muted:
            return "SDRSV: Muted"
        elif self.server_connected:
            return f"SDRSV: Connected ({self.audio_level}%)"
        else:
            return "SDRSV: Disconnected"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._sub_buffer = b''


class NetworkAnnouncementSource(AudioSource):
    """Listens for an inbound TCP connection on port 9601 and receives PCM
    audio to transmit over the radio.

    Same wire format as RemoteAudioSource (length-prefixed 16-bit mono PCM at
    the configured sample rate).  Unlike RemoteAudioSource, ptt_control=True so
    the mixer routes the audio to radio TX and activates PTT.  PTT is released
    automatically by the gateway's PTT_RELEASE_DELAY timeout once the queue
    drains after the sender disconnects.
    """
    def __init__(self, config, gateway):
        super().__init__("ANNIN", config)
        self.gateway = gateway
        self.priority = 0           # Same highest priority as FilePlayback
        self.ptt_control = True     # Routes to radio TX and activates PTT
        self.volume = float(getattr(config, 'ANNOUNCE_INPUT_VOLUME', 4.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2   # 16-bit mono
        self._ptt_hold_time = 2.0   # seconds of silence before releasing PTT
        self._last_above_threshold = 0.0  # monotonic time of last above-threshold chunk
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None

    def setup_audio(self):
        """Bind listen socket and start accept/reader thread."""
        import socket
        bind_host = self.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
        port = int(self.config.ANNOUNCE_INPUT_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="ANNIN-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Announcement input listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept one client at a time and read length-prefixed PCM."""
        import socket, struct

        while self._reader_running:
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self.client_connected = True
                print(f"\n[ANNIN] Client connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break
                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Connection error: {e}")
            finally:
                self.client_connected = False
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Client disconnected")

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Return (pcm, True) when above-threshold audio is available.

        Silence frames are consumed from the queue but discarded (return
        (None, False)) so PTT is not triggered by idle stream packets.
        A 2-second hold keeps PTT active through brief pauses in speech
        so the radio doesn't drop and re-key between sentences.
        """
        if not self.enabled or self.muted:
            return None, False

        cb = self._chunk_bytes
        now = time.monotonic()

        # Fill sub-buffer from queue — always drain so idle silence doesn't
        # back up the queue while the connection is held open.
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                # No data in queue — check if PTT hold is still active
                if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                    return b'\x00' * cb, True  # silence but keep PTT keyed
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering + threshold gate
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        if rms > 0:
            db = 20 * _math_mod.log10(rms / 32767.0)
            raw_level = max(0, min(100, (db + 60) * (100 / 60)))
        else:
            db = -100.0
            raw_level = 0

        if raw_level > self.audio_level:
            self.audio_level = raw_level
        else:
            self.audio_level = int(self.audio_level * 0.7 + raw_level * 0.3)

        threshold_db = float(getattr(self.config, 'ANNOUNCE_INPUT_THRESHOLD', -45.0))
        if db < threshold_db:
            # Below threshold — hold PTT with silence for up to 2s
            if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                return b'\x00' * cb, True  # silence but keep PTT keyed
            return None, False  # Hold expired: let PTT release

        # Above threshold — update hold timer
        self._last_above_threshold = now

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, True   # Above threshold: route to radio TX and activate PTT

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "ANNIN: Disabled"
        elif self.client_connected:
            return f"ANNIN: Connected ({self.audio_level}%)"
        else:
            return "ANNIN: Waiting"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._sub_buffer = b''


class StreamOutputSource:
    """Stream audio output to named pipe for Darkice"""
    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        self.connected = False
        self.pipe = None
        
        # Try to open pipe if enabled
        if config.ENABLE_STREAM_OUTPUT:
            self.setup_stream()
    
    def setup_stream(self):
        """Open named pipe for Darkice"""
        import os
        
        try:
            pipe_path = '/tmp/darkice_audio'
            
            # Create pipe if it doesn't exist
            if not os.path.exists(pipe_path):
                os.mkfifo(pipe_path)
                os.chmod(pipe_path, 0o666)
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  Created pipe: {pipe_path}")
            
            # Open pipe for writing (non-blocking)
            import fcntl
            self.pipe = open(pipe_path, 'wb', buffering=0)
            
            # Make non-blocking
            fd = self.pipe.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            self.connected = True
            
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"  ✓ Streaming via Darkice pipe")
                print(f"    Pipe: {pipe_path}")
                print(f"    Format: PCM 48kHz mono 16-bit")
                print(f"    Make sure Darkice is running:")
                print(f"      darkice -c /etc/darkice.cfg")
                
        except Exception as e:
            print(f"  ⚠ Darkice pipe setup failed: {e}")
            print(f"    Install: sudo apt-get install darkice")
            print(f"    Configure: /etc/darkice.cfg")
            print(f"    Start: darkice -c /etc/darkice.cfg")
            self.connected = False
    
    def send_audio(self, audio_data):
        """Send raw PCM audio to Darkice via pipe"""
        if not self.connected or not self.pipe:
            return
        
        try:
            self.pipe.write(audio_data)
                
        except BlockingIOError:
            # Pipe full - skip this chunk
            pass
        except BrokenPipeError:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Stream] Darkice pipe broken - Darkice may have stopped")
            self.connected = False
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Stream] Pipe error: {e}")
            self.connected = False
    
    def cleanup(self):
        """Close pipe"""
        if self.pipe:
            try:
                self.pipe.close()
            except:
                pass


class AudioMixer:
    """Mix audio from multiple sources with priority handling"""
    def __init__(self, config):
        self.config = config
        self.sources = []
        self.mixing_mode = 'simultaneous'  # Mix all sources together
        self.call_count = 0  # Debug counter
        
        # Per-source signal state for attack/release hysteresis
        self.signal_state = {}

        # Hysteresis + transition timing
        self.SIGNAL_ATTACK_TIME  = config.SIGNAL_ATTACK_TIME
        self.SIGNAL_RELEASE_TIME = config.SIGNAL_RELEASE_TIME
        self.SWITCH_PADDING_TIME = getattr(config, 'SWITCH_PADDING_TIME', 1.0)

        # Duck state machines — one entry per duck-group (e.g. 'aioc_vs_sdrs')
        # Tracks current duck state and active padding windows
        self.duck_state = {}

        # Per-SDR hold timers: instant attack, held release for smooth audio
        self.sdr_hold_until = {}      # {sdr_name: float timestamp}
        self.sdr_prev_included = {}   # {sdr_name: bool} - for fade-in detection

        # SDR-to-SDR duck cooldown: once a lower-priority SDR unducks (starts
        # playing because the higher-priority SDR's signal hold expired), it
        # gets SDR_DUCK_COOLDOWN seconds of immunity before a higher-priority
        # SDR can re-duck it.  This prevents rapid toggling when a higher-
        # priority SDR has intermittent signal or noise near the threshold.
        # SIGNAL_RELEASE_TIME already provides the same hold in the other
        # direction (higher-priority keeps playing 3s after signal stops), so
        # this makes the behaviour symmetric.
        self.SDR_DUCK_COOLDOWN = getattr(config, 'SDR_DUCK_COOLDOWN', 3.0)
        self._sdr_duck_cooldown_until = {}   # {sdr_name: float} earliest time re-duck allowed
        self._sdr_prev_ducked_by_sdr = {}    # {sdr_name: bool} Rule 2 ducked last tick
        
    def add_source(self, source):
        """Add an audio source to the mixer"""
        self.sources.append(source)
        # Sort by priority (lower number = higher priority)
        self.sources.sort(key=lambda s: s.priority)
        
    def remove_source(self, name):
        """Remove a source by name"""
        self.sources = [s for s in self.sources if s.name != name]
    
    def get_source(self, name):
        """Get a source by name"""
        for source in self.sources:
            if source.name == name:
                return source
        return None
    
    def get_mixed_audio(self, chunk_size):
        """
        Get mixed audio from all enabled sources.
        Returns: (mixed_audio, ptt_required, active_sources, sdr1_was_ducked, sdr2_was_ducked, rx_audio, sdrsv_was_ducked, sdr_only_audio)
        """
        self.call_count += 1

        # Debug output every 100 calls
        if self.call_count % 100 == 0 and self.config.VERBOSE_LOGGING:
            print(f"\n[Mixer Debug] Called {self.call_count} times, {len(self.sources)} sources")
            for src in self.sources:
                print(f"  Source: {src.name}, enabled={src.enabled}, priority={src.priority}")

        if not self.sources:
            return None, False, [], False, False, None, False, None

        # Priority mode: only use highest priority active source
        if self.mixing_mode == 'priority':
            for source in self.sources:
                if not source.enabled:
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] Skipping {source.name} (disabled)")
                    continue

                # Try to get audio from this source
                audio, ptt = source.get_audio(chunk_size)

                # Debug what each source returns
                if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                    if audio is not None:
                        print(f"  [Mixer] {source.name} returned audio ({len(audio)} bytes), PTT={ptt}")
                    else:
                        print(f"  [Mixer] {source.name} returned None (no audio)")

                if audio is not None:
                    return audio, ptt and source.ptt_control, [source.name], False, False, None, False, None

            # No sources had audio
            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] No sources returned audio")
            return None, False, [], False, False, None, False, None

        # Simultaneous mode: mix all active sources
        elif self.mixing_mode == 'simultaneous':
            return self._mix_simultaneous(chunk_size)

        # Duck mode: reduce volume of lower priority when higher priority active
        elif self.mixing_mode == 'duck':
            return self._mix_with_ducking(chunk_size)

        return None, False, [], False, False, None, False, None
    
    def _mix_simultaneous(self, chunk_size):
        """Mix all active sources together with SDR priority-based ducking"""
        mixed_audio = None
        ptt_required = False
        active_sources = []
        ptt_audio = None      # Separate PTT audio
        non_ptt_audio = None  # Non-PTT, non-SDR audio (Radio RX etc)
        sdr_sources = {}      # Dictionary of SDR sources: name -> (audio, source_obj)

        # Phase 1: Non-SDR sources (Radio, FilePlayback, etc.)
        # Get their audio first so we can compute the duck state before
        # touching SDR ring buffers.
        for source in self.sources:
            if source.name.startswith("SDR"):
                continue
            if not source.enabled:
                if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                    print(f"  [Mixer-Simultaneous] Skipping {source.name} (disabled)")
                continue

            audio, ptt = source.get_audio(chunk_size)

            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                if audio is not None:
                    print(f"  [Mixer-Simultaneous] {source.name} returned audio ({len(audio)} bytes), PTT={ptt}")
                else:
                    print(f"  [Mixer-Simultaneous] {source.name} returned None")

            if audio is None:
                continue

            active_sources.append(source.name)

            # Separate PTT and non-PTT sources
            if ptt and source.ptt_control:
                ptt_required = True
                if ptt_audio is None:
                    ptt_audio = audio
                else:
                    ptt_audio = self._mix_audio_streams(ptt_audio, audio, 0.5)
            else:
                if non_ptt_audio is None:
                    non_ptt_audio = audio
                else:
                    non_ptt_audio = self._mix_audio_streams(non_ptt_audio, audio, 0.5)

        # Collect SDR source names for duck state machine (audio fetched in Phase 2)
        _sdr_source_names = [s.name for s in self.sources if s.name.startswith("SDR") and s.enabled]

        # --- SDR priority-based ducking decision ---
        # AIOC audio (Radio RX) and PTT audio always take priority over all SDRs
        # Between SDRs: lower sdr_priority number = higher priority (ducks others)
        # BUT: Only duck if there's actual audio signal (not just silence/zeros)
        # Uses hysteresis to prevent rapid on/off switching (stuttering)
        
        import time
        current_time = time.time()

        # Capture configurable threshold for the nested function
        _sdr_signal_threshold = getattr(self.config, 'SDR_SIGNAL_THRESHOLD', -60.0)

        # Helper function to check if audio has actual signal (instantaneous)
        def check_signal_instant(audio_data):
            """Check if audio contains actual signal above noise floor (instant check, no hysteresis)"""
            if not audio_data:
                return False
            try:
                arr = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                if len(arr) == 0:
                    return False
                rms = float(np.sqrt(np.mean(arr * arr)))
                if rms > 0:
                    db = 20 * _math_mod.log10(rms / 32767.0)
                    return db > _sdr_signal_threshold
                return False
            except:
                return False
        
        # Helper function with hysteresis for stable signal detection
        def has_actual_audio(audio_data, source_name):
            """
            Check if audio has actual signal with attack/release hysteresis.

            Attack: signal must be CONTINUOUSLY present for SIGNAL_ATTACK_TIME before
                    a switch is allowed.  Any chunk of silence resets the attack timer,
                    so brief transients never trigger a source switch.

            Release: once active, the source must be continuously silent for
                     SIGNAL_RELEASE_TIME before it is declared inactive again.
            """
            if source_name not in self.signal_state:
                self.signal_state[source_name] = {
                    'has_signal': False,
                    'signal_continuous_start': 0.0,  # start of current unbroken signal run
                    'last_signal_time': 0.0,
                    'last_silence_time': current_time,
                }

            state = self.signal_state[source_name]
            signal_present_now = check_signal_instant(audio_data)

            if signal_present_now:
                state['last_signal_time'] = current_time
                if state['signal_continuous_start'] == 0.0:
                    # First chunk of a new continuous signal run — start the attack timer
                    state['signal_continuous_start'] = current_time
            else:
                state['last_silence_time'] = current_time
                # Any silence breaks continuity — reset the attack timer
                state['signal_continuous_start'] = 0.0

            if not state['has_signal']:
                # Inactive — fire attack only when signal has been unbroken for ATTACK_TIME
                if state['signal_continuous_start'] > 0.0:
                    continuous_duration = current_time - state['signal_continuous_start']
                    if continuous_duration >= self.SIGNAL_ATTACK_TIME:
                        state['has_signal'] = True
                        if self.config.VERBOSE_LOGGING:
                            print(f"  [Mixer] {source_name} ACTIVATED "
                                  f"(continuous signal for {continuous_duration:.2f}s)")
            else:
                # Active — release only after RELEASE_TIME of continuous silence
                time_since_signal = current_time - state['last_signal_time']
                if time_since_signal >= self.SIGNAL_RELEASE_TIME:
                    state['has_signal'] = False
                    if self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {source_name} RELEASED "
                              f"(silent for {time_since_signal:.2f}s)")

            return state['has_signal']
        
        other_audio_active = (ptt_audio is not None) or (non_ptt_audio is not None)

        # Trace state tracking
        _hold_fired = False
        _radio_has_signal = False
        _sdr_trace = {}

        # Check if other_audio actually has signal (not just zeros) with hysteresis.
        # PTT audio (file playback) is deterministic — when FilePlaybackSource returns data
        # it IS playing.  Applying attack hysteresis to it would delay SDR ducking AND would
        # trigger a duck-out transition that inserts SWITCH_PADDING_TIME of silence, cutting
        # the start of every announcement and dropping PTT.
        # Only apply hysteresis to non-PTT radio RX to suppress noise/squelch-tail transients.
        if other_audio_active:
            ptt_is_active = ptt_audio is not None  # Deterministic: treat as active immediately
            non_ptt_has_signal = has_actual_audio(non_ptt_audio, "Radio") if non_ptt_audio else False
            other_audio_active = ptt_is_active or non_ptt_has_signal
            _radio_has_signal = non_ptt_has_signal

            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                if non_ptt_audio and not non_ptt_has_signal:
                    print(f"  [Mixer] Non-PTT audio present but only silence - not ducking SDRs")

        # --- Duck state machine with transition padding ---
        # Manages the AIOC/Radio/PTT vs SDR duck relationship.
        # When a transition occurs (ducking starts or stops), SWITCH_PADDING_TIME
        # seconds of silence are inserted so the changeover is never abrupt:
        #   duck-out: both SDR and radio are silenced → then radio takes over
        #   duck-in:  SDR resumes immediately (fade-in handles onset click)
        ds = self.duck_state.setdefault('aioc_vs_sdrs', {
            'is_ducked': False,
            'prev_signal': False,
            'padding_end_time': 0.0,
            'transition_type': None,   # 'out' = duck starting, 'in' = duck ending
            '_radio_last_audio_time': 0.0,
        })

        # Track when Radio/PTT last had audio.  AIOC delivers 200ms blobs with
        # brief gaps between them — without a hold, each gap triggers a spurious
        # duck-in/duck-out transition cycle (2 × SWITCH_PADDING_TIME of silence).
        if other_audio_active:
            ds['_radio_last_audio_time'] = current_time
        elif ds.get('is_ducked', False):
            # Radio was ducking SDRs — hold it stable through AIOC blob gaps.
            # 1000ms covers two full blob periods (2 × 400ms) with margin.
            # AIOC blob gaps can reach 800-850ms; 500ms was too short and
            # caused spurious duck-in/duck-out transitions (SDR breakthrough).
            if current_time - ds.get('_radio_last_audio_time', 0.0) < 1.0:
                other_audio_active = True
                _hold_fired = True

        prev_signal = ds['prev_signal']
        ds['prev_signal'] = other_audio_active

        if not ds['is_ducked'] and other_audio_active and not prev_signal:
            # Transition: other audio just became active → start ducking SDRs.
            # Record whether SDR had actual signal now so we know whether the
            # transition-silence is needed (SDR→radio handoff) or not (radio-only).
            ds['is_ducked'] = True
            ds['padding_end_time'] = current_time + self.SWITCH_PADDING_TIME
            ds['transition_type'] = 'out'
            # Only count SDR as "active" if it had genuine signal recently
            # (hold timer still running).  SDR included only via sdr_is_sole_source
            # with no real signal doesn't warrant transition silence — there's
            # nothing audible to "clean break" from.
            ds['sdr_active_at_transition'] = any(
                self.sdr_prev_included.get(name, False)
                and current_time < self.sdr_hold_until.get(name, 0.0)
                for name in _sdr_source_names
            )
            if self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] SDR duck-OUT: {self.SWITCH_PADDING_TIME:.2f}s transition silence "
                      f"(SDR active: {ds['sdr_active_at_transition']})")
        elif ds['is_ducked'] and not other_audio_active and prev_signal:
            # Transition: other audio just went inactive → stop ducking SDRs.
            # No padding on duck-in: SDR resumes immediately with fade-in
            # (onset fade at line 1882 prevents click).  Duck-in padding would
            # add a needless 1s gap between Radio stopping and SDR resuming.
            ds['is_ducked'] = False
            ds['padding_end_time'] = 0.0
            ds['transition_type'] = None
            if self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] SDR duck-IN: immediate (no padding)")

        in_padding = current_time < ds['padding_end_time']
        # Effective duck: only suppress SDRs when AIOC is actually delivering
        # audio this tick.  The hold above keeps is_ducked stable through AIOC
        # inter-blob gaps so no spurious duck-in/duck-out transitions fire —
        # but if AIOC returned None (VAD released / reader stall), SDRs can
        # play through immediately rather than sitting silent for the full 1s
        # hold window.  If an inter-blob gap occurs during transmission and SDR
        # briefly plays, the 10ms onset fade-in keeps it inaudible.
        aioc_ducks_sdrs = (ds['is_ducked'] or in_padding) and non_ptt_audio is not None
        # During duck-out padding: silence ALL output so the switch is a clean break
        in_transition_out = in_padding and ds['transition_type'] == 'out'

        # Phase 2: Fetch SDR audio.  Always call get_audio() to drain the
        # ring buffer — ducked audio is stale and must be discarded so SDR
        # starts with fresh/current audio when the duck releases.
        for source in self.sources:
            if not source.name.startswith("SDR"):
                continue
            if not source.enabled:
                continue
            audio, _ptt = source.get_audio(chunk_size)
            sdr_duck = source.duck if hasattr(source, 'duck') else True
            if aioc_ducks_sdrs and sdr_duck:
                # Ducked — discard audio, pass None so ducking logic tracks state
                sdr_sources[source.name] = (None, source)
            else:
                if audio is not None:
                    active_sources.append(source.name)
                sdr_sources[source.name] = (audio, source)

        sdr1_was_ducked = False
        sdr2_was_ducked = False
        sdrsv_was_ducked = False

        # First pass: determine which SDRs should be ducked
        sdrs_to_include = {}  # SDRs that will actually be mixed

        # Sort SDR sources by priority (lower number = higher priority)
        sorted_sdrs = sorted(
            sdr_sources.items(),
            key=lambda x: getattr(x[1][1], 'sdr_priority', 99)
        )

        # Pre-scan: check which SDRs have instant signal this tick.
        # Used to refine sole_source: an SDR with no signal should not be
        # force-included when another SDR already has real audio, because
        # the no-signal SDR would just add loopback noise to the mix.
        _sdrs_with_signal = set()
        for _pre_name, (_pre_audio, _pre_src) in sorted_sdrs:
            if _pre_audio is not None and check_signal_instant(_pre_audio):
                _sdrs_with_signal.add(_pre_name)

        for sdr_name, (sdr_audio, sdr_source) in sorted_sdrs:
            sdr_duck = sdr_source.duck if hasattr(sdr_source, 'duck') else True
            sdr_priority = getattr(sdr_source, 'sdr_priority', 99)

            should_duck = False

            ducked_by_sdr = False  # Rule 2 specifically (not Rule 1)

            if sdr_duck:
                # Rule 1: AIOC/PTT/Radio audio ducks ALL SDRs (with padding on transitions)
                if aioc_ducks_sdrs:
                    should_duck = True
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} ducked by AIOC/Radio/PTT audio")
                else:
                    # Rule 2: Higher priority SDR (lower number) ducks lower priority SDRs
                    # Only duck if the higher-priority SDR has actual signal —
                    # not when it's included merely because it's the sole source type.
                    # Uses 'sig' which is hysteresis-based (requires SIGNAL_ATTACK_TIME
                    # seconds of continuous signal) so brief noise spikes from a higher-
                    # priority SDR don't immediately mute a lower-priority one.
                    # 'hold' is intentionally excluded here: it is for audio inclusion
                    # (fade-out) only, not for ducking decisions.
                    for other_name, (_, other_source_obj) in sorted_sdrs:
                        if other_name == sdr_name:
                            break  # only check sources processed before this one
                        other_priority = getattr(other_source_obj, 'sdr_priority', 99)
                        other_trace = _sdr_trace.get(other_name, {})
                        other_has_signal = other_trace.get('sig')  # hysteresis-based only
                        if other_priority < sdr_priority and other_has_signal:
                            ducked_by_sdr = True
                            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                                print(f"  [Mixer] {sdr_name} (priority {sdr_priority}) ducked by {other_name} (priority {other_priority})")
                            break

                    # Cooldown: after this SDR unducks from a Rule 2 duck, it gets
                    # SDR_DUCK_COOLDOWN seconds of immunity before it can be re-ducked.
                    # This prevents rapid toggling when the higher-priority SDR has
                    # intermittent signal near the threshold.
                    if ducked_by_sdr:
                        cooldown_until = self._sdr_duck_cooldown_until.get(sdr_name, 0.0)
                        if current_time < cooldown_until:
                            ducked_by_sdr = False  # cooldown active — keep playing

                    should_duck = ducked_by_sdr

            # Track Rule 2 transitions for cooldown timer
            prev_ducked_by_sdr = self._sdr_prev_ducked_by_sdr.get(sdr_name, False)
            if prev_ducked_by_sdr and not ducked_by_sdr and not aioc_ducks_sdrs:
                # Transition: was ducked by higher-priority SDR, now unducked.
                # Start cooldown — this SDR gets guaranteed play time.
                self._sdr_duck_cooldown_until[sdr_name] = current_time + self.SDR_DUCK_COOLDOWN
                if self.config.VERBOSE_LOGGING:
                    print(f"  [Mixer] {sdr_name} unduck cooldown started ({self.SDR_DUCK_COOLDOWN:.1f}s)")
            self._sdr_prev_ducked_by_sdr[sdr_name] = ducked_by_sdr

            # Track ducking state for status bar
            if should_duck:
                _sdr_trace[sdr_name] = {'ducked': True, 'inc': False, 'sig': False, 'hold': False, 'sole': False}
                if sdr_name == "SDR1":
                    sdr1_was_ducked = True
                elif sdr_name == "SDR2":
                    sdr2_was_ducked = True
                elif sdr_name == "SDRSV":
                    sdrsv_was_ducked = True
            else:
                # Instant attack + held release for SDR inclusion.
                #
                # The old has_actual_audio() approach used a 0.1s attack timer which
                # dropped the first 200ms chunk (one full AUDIO_CHUNK_SIZE period) and
                # then switched to full volume abruptly → missing audio + pop/click.
                #
                # New approach:
                #   - Include immediately on any detectable signal (no attack delay)
                #   - Hold inclusion for SIGNAL_RELEASE_TIME after signal stops so brief
                #     pauses don't cause dropouts and the tail fades away naturally
                #   - Apply a short linear fade-in at the moment of first inclusion to
                #     prevent the onset click when SDR activates after silence
                has_instant = check_signal_instant(sdr_audio)
                if has_instant:
                    self.sdr_hold_until[sdr_name] = current_time + self.SIGNAL_RELEASE_TIME
                hold_active = current_time < self.sdr_hold_until.get(sdr_name, 0.0)
                # has_sig_hyst: attack-hysteresis version used for SDR-to-SDR ducking
                # decisions (Rule 2).  Requires SIGNAL_ATTACK_TIME seconds of continuous
                # signal before firing so brief noise spikes don't immediately duck
                # lower-priority SDRs.  Release mirrors SIGNAL_RELEASE_TIME via the
                # has_actual_audio() state machine, so ducking lasts 3s after signal stops
                # (same as hold_active, but with the attack guard on the front end).
                has_sig_hyst = has_actual_audio(sdr_audio, sdr_name)
                # When SDR is the only source type (no radio RX or PTT audio),
                # force-include so we don't gate out the only audio available.
                # BUT: if this SDR has no signal and another SDR does, don't
                # force-include — it would just add loopback noise to the mix.
                no_aioc = non_ptt_audio is None and ptt_audio is None
                other_sdrs_have_signal = bool(_sdrs_with_signal - {sdr_name})
                sdr_is_sole_source = no_aioc and (has_instant or hold_active)
                include_sdr = has_instant or hold_active or sdr_is_sole_source
                _sdr_trace[sdr_name] = {'ducked': False, 'inc': include_sdr, 'sig': has_sig_hyst, 'inst': has_instant, 'hold': hold_active, 'sole': sdr_is_sole_source}

                if sdr_audio is None:
                    # No data this cycle (reader thread momentarily behind).
                    # Preserve sdr_prev_included so that when audio returns we
                    # resume cleanly: if it was True, the next chunk continues
                    # without a spurious fade-in click; if it was False, it stays
                    # False and the normal onset fade-in fires as expected.
                    # Clear sig so this SDR's stale hysteresis hold does not
                    # duck lower-priority SDRs while it has no audio to offer.
                    _sdr_trace[sdr_name]['sig'] = False
                    continue

                prev_included = self.sdr_prev_included.get(sdr_name, False)

                if include_sdr:
                    audio_to_include = sdr_audio
                    if not prev_included:
                        # Onset: fade-in from 0→1 over first 10ms (480 samples)
                        arr = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.float32)
                        fade_len = min(480, len(arr))
                        arr[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
                        audio_to_include = arr.astype(np.int16).tobytes()
                    self.sdr_prev_included[sdr_name] = True
                    sdrs_to_include[sdr_name] = (audio_to_include, sdr_source)
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} included (instant={'yes' if has_instant else 'hold'})")
                elif prev_included:
                    # Transition frame: was included last chunk, not now.
                    # Apply fade-out so the cutoff is always smooth regardless of
                    # how much time elapsed since the last iteration (avoids the
                    # timing-window bug where a slow AIOC read skips the fade).
                    arr = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.float32)
                    arr *= np.linspace(1.0, 0.0, len(arr))
                    audio_to_include = arr.astype(np.int16).tobytes()
                    self.sdr_prev_included[sdr_name] = False
                    sdrs_to_include[sdr_name] = (audio_to_include, sdr_source)
                    if self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} fade-out (hold expired)")
                else:
                    self.sdr_prev_included[sdr_name] = False
        
        # Second pass: actually mix the non-ducked SDRs.
        # Use sum-and-clip instead of crossfade: each SDR contributes at full
        # gain regardless of how many are active.  Crossfade (ratio=0.5) caused
        # a 6 dB step on SDR1 every time SDR2 entered or exited the mix.
        sdr_only_audio = None
        for sdr_name, (sdr_audio, sdr_source) in sdrs_to_include.items():
            # Build SDR-only mix for rebroadcast (before merging into non_ptt_audio)
            if sdr_only_audio is None:
                sdr_only_audio = sdr_audio
            else:
                s1 = np.frombuffer(sdr_only_audio, dtype=np.int16).astype(np.int32)
                s2 = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.int32)
                smin = min(len(s1), len(s2))
                sdr_only_audio = np.clip(
                    s1[:smin] + s2[:smin], -32768, 32767
                ).astype(np.int16).tobytes()

            if non_ptt_audio is None:
                non_ptt_audio = sdr_audio
            else:
                arr1 = np.frombuffer(non_ptt_audio, dtype=np.int16).astype(np.int32)
                arr2 = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.int32)
                min_len = min(len(arr1), len(arr2))
                non_ptt_audio = np.clip(
                    arr1[:min_len] + arr2[:min_len], -32768, 32767
                ).astype(np.int16).tobytes()
        
        # Priority: PTT audio always wins (full volume, no mixing with radio)
        if ptt_audio is not None:
            mixed_audio = ptt_audio
            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                print(f"  [Mixer-Simultaneous] Using PTT audio at FULL VOLUME (not mixing with radio)")
        elif non_ptt_audio is not None:
            mixed_audio = non_ptt_audio

        # Duck-out transition: SDRs are already silenced by aioc_ducks_sdrs.
        # Do NOT silence mixed_audio here — that would throw away Radio audio
        # for the entire SWITCH_PADDING_TIME (1s), causing a silence gap every
        # time Radio returns after SDR was playing.

        # When PTT (file playback) wins the mix, non_ptt_audio (radio RX) is not
        # included in mixed_audio.  Carry it out separately so the transmit loop
        # can still forward it to Mumble — listeners hear the radio channel even
        # while an announcement is being transmitted.
        rx_audio = non_ptt_audio if ptt_required else None

        # Store trace state for audio_trace instrumentation
        self._last_trace_state = {
            'dk': ds['is_ducked'],
            'hold': _hold_fired,
            'pad': in_padding,
            'tOut': in_transition_out,
            'sdrAT': ds.get('sdr_active_at_transition', False),
            'oaa': other_audio_active,
            'radioSig': _radio_has_signal,
            'ducks': aioc_ducks_sdrs,
            'ptt': ptt_required,
            'sdrs': _sdr_trace,
        }

        if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
            print(f"  [Mixer-Simultaneous] Result: {len(active_sources)} active sources, PTT={ptt_required}")

        return mixed_audio, ptt_required, active_sources, sdr1_was_ducked, sdr2_was_ducked, rx_audio, sdrsv_was_ducked, sdr_only_audio
    
    def _mix_with_ducking(self, chunk_size):
        """Mix with ducking: reduce lower priority sources"""
        # Find highest priority active source
        high_priority_active = False
        for source in self.sources:
            if source.enabled:
                audio, _ = source.get_audio(chunk_size)
                if audio is not None:
                    high_priority_active = True
                    break
        
        # If high priority is active, duck the others
        mixed_audio = None
        ptt_required = False
        active_sources = []
        
        for i, source in enumerate(self.sources):
            if not source.enabled:
                continue
            
            audio, ptt = source.get_audio(chunk_size)
            if audio is None:
                continue
            
            active_sources.append(source.name)
            
            # Duck lower priority sources
            if i > 0 and high_priority_active:
                audio = self._apply_volume(audio, 0.3)  # 30% volume
            
            if ptt and source.ptt_control:
                ptt_required = True
            
            if mixed_audio is None:
                mixed_audio = audio
            else:
                mixed_audio = self._mix_audio_streams(mixed_audio, audio, 0.5)
        
        return mixed_audio, ptt_required, active_sources, False, False, None, False, None

    def _mix_audio_streams(self, audio1, audio2, ratio=0.5):
        """Mix two audio streams together"""
        arr1 = np.frombuffer(audio1, dtype=np.int16).astype(np.float32)
        arr2 = np.frombuffer(audio2, dtype=np.int16).astype(np.float32)

        # Ensure same length
        min_len = min(len(arr1), len(arr2))
        arr1 = arr1[:min_len]
        arr2 = arr2[:min_len]

        mixed = np.clip(arr1 * ratio + arr2 * (1.0 - ratio), -32768, 32767).astype(np.int16)
        return mixed.tobytes()
    
    def _apply_volume(self, audio, volume):
        """Apply volume multiplier to audio"""
        arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
        return np.clip(arr * volume, -32768, 32767).astype(np.int16).tobytes()
    
    def get_status(self):
        """Get status of all sources"""
        status = []
        for source in self.sources:
            status.append(source.get_status())
        return status


class RelayController:
    """Controls a CH340 USB relay module via serial (4-byte commands)."""

    CMD_ON  = bytes([0xA0, 0x01, 0x01, 0xA2])
    CMD_OFF = bytes([0xA0, 0x01, 0x00, 0xA1])

    def __init__(self, device, baud=9600):
        self._device = device
        self._baud = baud
        self._port = None
        self._state = None  # None=unknown, True=on, False=off

    def open(self):
        try:
            import serial
            self._port = serial.Serial(self._device, self._baud, timeout=1)
            return True
        except Exception as e:
            print(f"  [Relay] Failed to open {self._device}: {e}")
            return False

    def close(self):
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    def set_state(self, on):
        """Set relay on (True) or off (False). Returns True on success."""
        if not self._port:
            return False
        try:
            self._port.write(self.CMD_ON if on else self.CMD_OFF)
            self._state = on
            return True
        except Exception as e:
            print(f"  [Relay] Write error on {self._device}: {e}")
            return False

    @property
    def state(self):
        return self._state


class RadioCATClient:
    """TCP client for TH-9800 CAT control via TH9800_CAT.py server."""

    START_BYTES = b'\xAA\xFD'

    # 12-byte default payload template (button release / return control to body)
    DEFAULT_PAYLOAD = bytearray([0x84,0xFF,0xFF,0xFF,0xFF,0x81,0xFF,0xFF,0x82,0xFF,0xFF,0x00])

    # VFO identifiers
    LEFT = 'LEFT'
    RIGHT = 'RIGHT'

    def __init__(self, host, port, password=''):
        self._host = host
        self._port = port
        self._password = password
        self._sock = None
        self._buf = b''
        # Radio state parsed from forwarded packets
        self._channel = ''       # Latest channel text (3-char, e.g. "001")
        self._channel_vfo = ''   # Which VFO the channel belongs to ('LEFT' or 'RIGHT')
        self._vfo_text = ''      # Display text (6-char name)
        self._power = {}         # {'LEFT': 'H', 'RIGHT': 'L'}
        self._lock = threading.Lock()
        self._last_activity = 0  # monotonic timestamp of last send/recv (for status bar)
        self._stop = False       # set True to abort loops (ctrl+c)
        self._log = None         # file handle for debug log

    def _logmsg(self, msg, console=True):
        """Write debug message to cat_debug.log and optionally print."""
        if console:
            print(msg)
        if self._log:
            self._log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            self._log.flush()

    def connect(self):
        """Connect to CAT TCP server and authenticate."""
        try:
            self._log = open('cat_debug.log', 'w')
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self._host, self._port))
            # Authenticate
            self._sock.sendall(f"!pass {self._password}\n".encode())
            resp = self._recv_line(timeout=5.0)
            if resp and 'Login Successful' in resp:
                # Ensure RTS is set to USB Controlled (required for CAT TX)
                rts_resp = self._send_cmd("!rts True")
                if rts_resp:
                    print(f"  CAT RTS: {rts_resp}")
                return True
            else:
                print(f"  CAT auth failed: {resp}")
                self.close()
                return False
        except Exception as e:
            print(f"  CAT connect error: {e}")
            self.close()
            return False

    def close(self):
        """Close TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

    def _send_cmd(self, cmd):
        """Send text command and return response line."""
        if not self._sock:
            return None
        try:
            self._sock.sendall(f"{cmd}\n".encode())
            self._last_activity = time.monotonic()
            return self._recv_line(timeout=2.0)
        except Exception as e:
            self._logmsg(f"  CAT send error: {e}")
            return None

    def _recv_line(self, timeout=2.0):
        """Read from socket until newline, with timeout. Parses binary radio packets inline."""
        if not self._sock:
            return None
        self._sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check for binary packet in buffer
            while self.START_BYTES in self._buf:
                idx = self._buf.index(self.START_BYTES)
                if idx > 0:
                    # Text data before binary — check for newline
                    text_part = self._buf[:idx]
                    if b'\n' in text_part:
                        line_end = text_part.index(b'\n')
                        line = self._buf[:line_end].decode('ascii', errors='replace').strip()
                        self._buf = self._buf[line_end+1:]
                        return line
                    self._buf = self._buf[idx:]
                # Need at least 3 bytes for start + length
                if len(self._buf) < 3:
                    break
                pkt_len = self._buf[2]
                total = 3 + pkt_len + 1  # start(2) + len(1) + payload + checksum(1)
                if len(self._buf) < total:
                    break
                pkt_data = self._buf[3:3+pkt_len]
                self._buf = self._buf[total:]
                # Skip any trailing newline the server appends
                if self._buf.startswith(b'\n'):
                    self._buf = self._buf[1:]
                self._parse_radio_packet(pkt_data)

            # Check for text line in buffer
            if b'\n' in self._buf:
                line_end = self._buf.index(b'\n')
                line = self._buf[:line_end].decode('ascii', errors='replace').strip()
                self._buf = self._buf[line_end+1:]
                return line

            # Read more data
            try:
                remaining = max(0.1, deadline - time.time())
                self._sock.settimeout(remaining)
                data = self._sock.recv(1024)
                if not data:
                    return None
                self._buf += data
                self._last_activity = time.monotonic()
            except socket.timeout:
                break
            except Exception:
                return None
        return None

    def _drain(self, duration=0.3):
        """Drain any pending data from socket, parsing packets along the way."""
        if not self._sock:
            return
        end = time.time() + duration
        self._sock.settimeout(0.05)
        while time.time() < end:
            try:
                data = self._sock.recv(4096)
                if not data:
                    break
                self._buf += data
            except socket.timeout:
                pass
            except Exception:
                break
        # Process any buffered packets
        self._recv_line(timeout=0.1)

    def _parse_radio_packet(self, data):
        """Parse forwarded binary radio packets to update internal state."""
        if len(data) < 2:
            return
        pkt_type = data[0]  # First byte is packet type
        vfo_byte = data[1]  # Second byte is VFO indicator

        if pkt_type == 0x03:  # DISPLAY_CHANGE
            if vfo_byte == 0x43:
                self._channel_vfo = self.LEFT
            elif vfo_byte == 0xC3:
                self._channel_vfo = self.RIGHT
            self._logmsg(f"    [pkt] DISPLAY_CHANGE vfo=0x{vfo_byte:02X} -> {self._channel_vfo}", console=False)

        elif pkt_type == 0x02:  # CHANNEL_TEXT
            if vfo_byte in (0x40, 0x60):
                self._channel_vfo = self.LEFT
            elif vfo_byte in (0xC0, 0xE0):
                self._channel_vfo = self.RIGHT
            if len(data) >= 6:
                try:
                    self._channel = data[3:6].decode('ascii', errors='replace').strip()
                    self._logmsg(f"    [pkt] CHANNEL_TEXT vfo={self._channel_vfo} ch='{self._channel}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x01:  # DISPLAY_TEXT
            if len(data) >= 9:
                try:
                    self._vfo_text = data[3:9].decode('ascii', errors='replace').strip()
                    self._logmsg(f"    [pkt] DISPLAY_TEXT text='{self._vfo_text}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x04:  # DISPLAY_ICONS
            if vfo_byte == 0x40:
                vfo = self.LEFT
            elif vfo_byte == 0xC0:
                vfo = self.RIGHT
            else:
                self._logmsg(f"    [pkt] DISPLAY_ICONS unknown vfo=0x{vfo_byte:02X}", console=False)
                return
            if len(data) >= 8:
                power_byte = data[7]
                if power_byte & 0x08:
                    self._power[vfo] = 'L'
                elif power_byte & 0x02:
                    self._power[vfo] = 'M'
                else:
                    self._power[vfo] = 'H'
                self._logmsg(f"    [pkt] DISPLAY_ICONS vfo={vfo} power={self._power[vfo]}", console=False)
        else:
            self._logmsg(f"    [pkt] type=0x{pkt_type:02X} vfo=0x{vfo_byte:02X} data={data.hex()}", console=False)

    def _build_packet(self, payload):
        """Build full AA FD <len> <payload> <checksum> packet, return hex string."""
        length = len(payload)
        checksum = length
        for b in payload:
            checksum ^= b
        pkt = bytearray([0xAA, 0xFD, length]) + bytearray(payload) + bytearray([checksum & 0xFF])
        return pkt.hex()

    def _build_button_payload(self, cmd_bytes, start, end):
        """Insert cmd_bytes into DEFAULT_PAYLOAD at positions start:end."""
        payload = bytearray(self.DEFAULT_PAYLOAD)
        payload[start:end] = bytearray(cmd_bytes)
        return payload

    def _send_button(self, cmd_bytes, start, end):
        """Build button payload, send as !data command."""
        payload = self._build_button_payload(cmd_bytes, start, end)
        hex_str = self._build_packet(payload)
        return self._send_cmd(f"!data {hex_str}")

    def _send_button_release(self):
        """Send button release (DEFAULT_PAYLOAD)."""
        hex_str = self._build_packet(self.DEFAULT_PAYLOAD)
        return self._send_cmd(f"!data {hex_str}")

    def _channel_matches(self, target_int):
        """Compare current channel to target as integers, tolerant of padding/spaces."""
        try:
            return int(self._channel) == target_int
        except (ValueError, TypeError):
            return False

    def set_channel(self, vfo, target_channel):
        """Set channel on specified VFO by stepping the dial. Returns True on success."""
        target_int = int(target_channel)
        self._logmsg(f"  CAT: Setting {vfo} channel to {target_int}...")

        self._drain()

        # Press the VFO dial to trigger a display update
        if vfo == self.LEFT:
            self._send_button([0x00, 0x25], 3, 5)  # L_DIAL_PRESS
        else:
            self._send_button([0x00, 0xA5], 3, 5)  # R_DIAL_PRESS
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)

        # Drain and read channel
        self._drain(0.5)
        self._logmsg(f"    Current: vfo={self._channel_vfo} ch='{self._channel}'", console=False)
        if self._channel_vfo == vfo and self._channel_matches(target_int):
            self._logmsg(f"    Already on channel {target_int}")
            return True

        start_channel = self._channel if self._channel_vfo == vfo else ''

        # Step through channels
        for i in range(200):
            if self._stop:
                self._logmsg(f"    Aborted")
                return False
            if vfo == self.LEFT:
                self._send_button([0x02], 2, 3)  # L_DIAL_RIGHT
            else:
                self._send_button([0x82], 2, 3)  # R_DIAL_RIGHT
            time.sleep(0.05)
            self._send_button_release()
            time.sleep(0.15)

            # Read response
            self._drain(0.2)
            self._logmsg(f"    Step {i+1}: vfo={self._channel_vfo} ch='{self._channel}'", console=False)
            if self._channel_vfo == vfo and self._channel_matches(target_int):
                self._logmsg(f"    Channel set to {target_int} (stepped {i+1})")
                return True
            if start_channel and self._channel_vfo == vfo and self._channel == start_channel and i > 0:
                self._logmsg(f"    Channel {target_int} not found (looped around after {i+1} steps)")
                return False

        self._logmsg(f"    Channel {target_int} not found (max iterations)")
        return False

    def set_volume(self, vfo, target_level):
        """Set volume on specified VFO by stepping toward target. level=0-100."""
        target_level = max(0, min(100, target_level))
        vfo_letter = 'LEFT' if vfo == self.LEFT else 'RIGHT'
        # Start from radio default (25) — radio resets volume on power cycle
        current = 25
        step = 2
        self._logmsg(f"  CAT: Setting {vfo} volume to {target_level}% (from {current})...")

        if current == target_level:
            self._logmsg(f"    Already at volume {target_level}")
            return True

        # Step toward target
        iterations = 0
        while current != target_level:
            if self._stop:
                self._logmsg(f"    Aborted")
                return False
            if current < target_level:
                current = min(current + step, target_level)
            else:
                current = max(current - step, target_level)
            resp = self._send_cmd(f"!vol {vfo_letter} {current}")
            self._logmsg(f"    Volume step: {current}", console=False)
            time.sleep(0.02)
            iterations += 1
            if iterations > 100:
                self._logmsg(f"    Volume max iterations")
                break

        self._logmsg(f"    Volume set to {target_level}%")
        return True

    def set_power(self, vfo, target):
        """Set power level on specified VFO. target='L','M','H'. Returns True on success."""
        target = target.upper()
        if target not in ('L', 'M', 'H'):
            self._logmsg(f"    Invalid power level: {target}")
            return False
        self._logmsg(f"  CAT: Setting {vfo} power to {target}...")

        self._drain()

        # Trigger display refresh with dial press
        if vfo == self.LEFT:
            self._send_button([0x00, 0x25], 3, 5)
        else:
            self._send_button([0x00, 0xA5], 3, 5)
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)

        current = self._power.get(vfo, '')
        self._logmsg(f"    Current power: vfo={vfo} power='{current}' target='{target}'", console=False)
        if current == target:
            self._logmsg(f"    Already at power {target}")
            return True

        start_power = current

        # Cycle power: L→M→H→L (press LOW button)
        for i in range(4):
            if self._stop:
                self._logmsg(f"    Aborted")
                return False
            if vfo == self.LEFT:
                self._send_button([0x00, 0x21], 3, 5)  # L_LOW
            else:
                self._send_button([0x00, 0xA1], 3, 5)  # R_LOW
            time.sleep(0.15)
            self._send_button_release()
            time.sleep(0.3)
            self._drain(0.5)

            current = self._power.get(vfo, '')
            self._logmsg(f"    Power cycle {i+1}: power='{current}'", console=False)
            if current == target:
                self._logmsg(f"    Power set to {target} (cycled {i+1})")
                return True
            if start_power and current == start_power and i > 0:
                self._logmsg(f"    Power {target} not reached (looped back)")
                return False

        self._logmsg(f"    Power {target} not reached (max iterations)")
        return False

    def setup_radio(self, config):
        """Run full radio setup sequence from config."""
        left_ch = getattr(config, 'CAT_LEFT_CHANNEL', -1)
        right_ch = getattr(config, 'CAT_RIGHT_CHANNEL', -1)
        left_vol = getattr(config, 'CAT_LEFT_VOLUME', -1)
        right_vol = getattr(config, 'CAT_RIGHT_VOLUME', -1)
        left_pwr = str(getattr(config, 'CAT_LEFT_POWER', '')).strip()
        right_pwr = str(getattr(config, 'CAT_RIGHT_POWER', '')).strip()

        if self._stop:
            return

        if int(left_ch) != -1:
            try:
                self.set_channel(self.LEFT, int(left_ch))
            except Exception as e:
                self._logmsg(f"  CAT: Left channel error: {e}")

        if self._stop:
            return

        if int(right_ch) != -1:
            try:
                self.set_channel(self.RIGHT, int(right_ch))
            except Exception as e:
                self._logmsg(f"  CAT: Right channel error: {e}")

        if self._stop:
            return

        if int(left_vol) != -1:
            try:
                self.set_volume(self.LEFT, int(left_vol))
            except Exception as e:
                self._logmsg(f"  CAT: Left volume error: {e}")

        if self._stop:
            return

        if int(right_vol) != -1:
            try:
                self.set_volume(self.RIGHT, int(right_vol))
            except Exception as e:
                self._logmsg(f"  CAT: Right volume error: {e}")

        if self._stop:
            return

        if left_pwr:
            try:
                self.set_power(self.LEFT, left_pwr)
            except Exception as e:
                self._logmsg(f"  CAT: Left power error: {e}")

        if self._stop:
            return

        if right_pwr:
            try:
                self.set_power(self.RIGHT, right_pwr)
            except Exception as e:
                self._logmsg(f"  CAT: Right power error: {e}")


class MumbleServerManager:
    """Manages local mumble-server (murmurd) instances.

    Each instance gets its own config file and systemd service override.
    Config files are written to /etc/mumble-server-gw{n}.ini and managed
    via systemd (mumble-server-gw{n}.service).
    """

    # State constants
    STATE_DISABLED = 'disabled'
    STATE_CONFIGURED = 'configured'
    STATE_RUNNING = 'running'
    STATE_ERROR = 'error'

    def __init__(self, instance_num, config):
        self.num = instance_num
        self.prefix = f'MUMBLE_SERVER_{instance_num}'
        self.config = config
        self.state = self.STATE_DISABLED
        self.error_msg = ''
        self._service_name = f'mumble-server-gw{instance_num}'
        self._config_path = f'/etc/mumble-server-gw{instance_num}.ini'
        self._db_path = f'/var/lib/mumble-server/mumble-server-gw{instance_num}.sqlite'
        self._log_path = f'/var/log/mumble-server/mumble-server-gw{instance_num}.log'
        self._pid_path = f'/var/run/mumble-server/mumble-server-gw{instance_num}.pid'

    def _get_cfg(self, key):
        """Get a config value for this instance."""
        return getattr(self.config, f'{self.prefix}_{key}', None)

    def is_enabled(self):
        return getattr(self.config, f'ENABLE_{self.prefix}', False)

    def write_config(self):
        """Write the mumble-server .ini file for this instance."""
        port = int(self._get_cfg('PORT') or 64738)
        password = str(self._get_cfg('PASSWORD') or '')
        max_users = int(self._get_cfg('MAX_USERS') or 10)
        max_bw = int(self._get_cfg('MAX_BANDWIDTH') or 72000)
        welcome = str(self._get_cfg('WELCOME') or '')
        reg_name = str(self._get_cfg('REGISTER_NAME') or '')
        allow_html = self._get_cfg('ALLOW_HTML')
        opus_thresh = int(self._get_cfg('OPUS_THRESHOLD') or 0)

        lines = [
            '# Auto-generated by Mumble Radio Gateway',
            f'# Instance: Mumble Server {self.num}',
            f'# Do not edit — regenerated on each gateway start',
            '',
            f'port={port}',
            f'serverpassword={password}',
            f'bandwidth={max_bw}',
            f'users={max_users}',
            f'opusthreshold={opus_thresh}',
            f'allowhtml={"true" if allow_html else "false"}',
            f'welcometext={welcome}',
            f'registerName={reg_name}',
            f'bonjour=false',
            '',
            f'database={self._db_path}',
            f'logfile={self._log_path}',
            f'pidfile={self._pid_path}',
            '',
            '# Auto-generated SSL (mumble-server creates self-signed on first run)',
            '',
        ]

        try:
            import subprocess
            content = '\n'.join(lines) + '\n'
            result = subprocess.run(
                ['sudo', 'tee', self._config_path],
                input=content, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = f'Failed to write config: {result.stderr.strip()}'
                return False
            return True
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Config write error: {e}'
            return False

    def _setup_systemd_service(self):
        """Create a systemd service override for this instance."""
        import subprocess

        service_file = f'/etc/systemd/system/{self._service_name}.service'
        murmurd_bin = None
        for candidate in ['/usr/sbin/murmurd', '/usr/bin/murmurd',
                          '/usr/sbin/mumble-server', '/usr/bin/mumble-server']:
            try:
                result = subprocess.run(['test', '-x', candidate],
                                        capture_output=True, timeout=2)
                if result.returncode == 0:
                    murmurd_bin = candidate
                    break
            except Exception:
                pass

        if not murmurd_bin:
            # Try 'which' as fallback
            try:
                result = subprocess.run(['which', 'murmurd'], capture_output=True,
                                        text=True, timeout=2)
                if result.returncode == 0:
                    murmurd_bin = result.stdout.strip()
            except Exception:
                pass
            if not murmurd_bin:
                try:
                    result = subprocess.run(['which', 'mumble-server'],
                                            capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        murmurd_bin = result.stdout.strip()
                except Exception:
                    pass

        if not murmurd_bin:
            self.state = self.STATE_ERROR
            self.error_msg = 'murmurd/mumble-server binary not found'
            return False

        # Detect the service user: Arch uses '_mumble-server', Debian uses 'mumble-server'
        import pwd
        svc_user = None
        for candidate_user in ['_mumble-server', 'mumble-server']:
            try:
                pwd.getpwnam(candidate_user)
                svc_user = candidate_user
                break
            except KeyError:
                pass
        if not svc_user:
            self.state = self.STATE_ERROR
            self.error_msg = 'mumble-server system user not found (need _mumble-server or mumble-server)'
            return False

        unit = '\n'.join([
            '[Unit]',
            f'Description=Mumble Server (Gateway Instance {self.num})',
            'After=network.target',
            '',
            '[Service]',
            'Type=simple',
            f'ExecStart={murmurd_bin} -fg -ini {self._config_path}',
            f'User={svc_user}',
            f'Group={svc_user}',
            'Restart=on-failure',
            'RestartSec=5',
            '',
            '[Install]',
            'WantedBy=multi-user.target',
            '',
        ])

        try:
            result = subprocess.run(
                ['sudo', 'tee', service_file],
                input=unit, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = f'Failed to write service: {result.stderr.strip()}'
                return False
            subprocess.run(['sudo', 'systemctl', 'daemon-reload'],
                           capture_output=True, timeout=5)
            return True
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Service setup error: {e}'
            return False

    def start(self):
        """Write config, set up service, and start the mumble-server instance."""
        import subprocess

        if not self.is_enabled():
            self.state = self.STATE_DISABLED
            return

        self.state = self.STATE_CONFIGURED
        self.error_msg = ''

        # Check if mumble-server package is installed
        try:
            result = subprocess.run(['which', 'murmurd'], capture_output=True,
                                    text=True, timeout=2)
            if result.returncode != 0:
                result = subprocess.run(['which', 'mumble-server'],
                                        capture_output=True, text=True, timeout=2)
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = 'mumble-server not installed (run scripts/install.sh)'
                return
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Cannot check for mumble-server: {e}'
            return

        # Stop any existing instance first so config changes (especially port)
        # take effect.  systemctl start is a no-op if the service is already
        # running, so we must explicitly stop+start (restart) every time.
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'stop', f'{self._service_name}.service'],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

        # Ensure directories exist
        for d in ['/var/lib/mumble-server', '/var/log/mumble-server',
                  '/var/run/mumble-server']:
            try:
                subprocess.run(['sudo', 'mkdir', '-p', d],
                               capture_output=True, timeout=3)
            except Exception:
                pass

        # Write config file
        if not self.write_config():
            return

        # Set up systemd service
        if not self._setup_systemd_service():
            return

        autostart = self._get_cfg('AUTOSTART')
        if autostart is False:
            # Configured but not auto-started
            print(f"  Mumble Server {self.num}: configured (autostart=false)")
            return

        # Start the service
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'start', f'{self._service_name}.service'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = result.stderr.strip() or 'systemctl start failed'
                return
            # Brief pause then verify
            time.sleep(0.5)
            self._check_running()
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Start error: {e}'

    def stop(self):
        """Stop the mumble-server instance."""
        import subprocess
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'stop', f'{self._service_name}.service'],
                capture_output=True, timeout=10
            )
        except Exception:
            pass
        self.state = self.STATE_CONFIGURED if self.is_enabled() else self.STATE_DISABLED

    def _check_running(self):
        """Check if the service is actively running."""
        import subprocess
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', f'{self._service_name}.service'],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() == 'active':
                self.state = self.STATE_RUNNING
            elif self.state != self.STATE_ERROR:
                self.state = self.STATE_ERROR
                # Try to get reason from journal
                try:
                    jr = subprocess.run(
                        ['journalctl', '-u', f'{self._service_name}.service',
                         '-n', '3', '--no-pager', '-q'],
                        capture_output=True, text=True, timeout=3
                    )
                    last_line = jr.stdout.strip().split('\n')[-1] if jr.stdout.strip() else ''
                    self.error_msg = last_line[:80] if last_line else 'service not active'
                except Exception:
                    self.error_msg = 'service not active'
        except Exception as e:
            if self.state != self.STATE_ERROR:
                self.state = self.STATE_ERROR
                self.error_msg = f'status check failed: {e}'

    def check_health(self):
        """Periodic health check — call from status_monitor_loop."""
        if not self.is_enabled():
            self.state = self.STATE_DISABLED
            return
        if self.state == self.STATE_DISABLED:
            return
        self._check_running()

    def get_status(self):
        """Return (state, port) tuple for status bar."""
        port = int(self._get_cfg('PORT') or 64738)
        return self.state, port


class StatusBarWriter:
    """Wraps sys.stdout so that any print() clears the status bar first.

    The status monitor loop calls draw_status() to paint the bar on the
    last terminal line.  When any other thread calls print() (which goes
    through write()), this wrapper:
      1. Clears the current status bar line (\r + spaces + \r)
      2. Writes the log text (which scrolls the terminal up)
      3. Lets the next draw_status() tick repaint the bar below
    """

    def __init__(self, original):
        self._orig = original
        self._lock = threading.Lock()
        self._last_status = ""   # last status bar text (for redraw)
        self._bar_drawn = False  # True when status bar is on screen
        # Forward all attributes that print() and other code might check
        for attr in ('encoding', 'errors', 'mode', 'name', 'newlines',
                     'fileno', 'isatty', 'readable', 'seekable', 'writable'):
            if hasattr(original, attr):
                try:
                    setattr(self, attr, getattr(original, attr))
                except (AttributeError, TypeError):
                    pass

    def write(self, text):
        with self._lock:
            if self._bar_drawn and text and text != '\n':
                # Clear the status bar line before printing log text
                try:
                    import shutil as _sh
                    cols = _sh.get_terminal_size().columns
                except Exception:
                    cols = 120
                self._orig.write(f"\r{' ' * cols}\r")
                self._bar_drawn = False
                # Strip leading \n — it was only there to push past the old
                # status bar; the wrapper now clears the bar instead.
                if text.startswith('\n'):
                    text = text[1:]
            self._orig.write(text)
        return len(text)

    def draw_status(self, status_line):
        """Called by the status monitor to paint the bar (no newline)."""
        with self._lock:
            self._orig.write(f"\r{status_line}")
            self._orig.flush()
            self._last_status = status_line
            self._bar_drawn = True

    def flush(self):
        self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


class MumbleRadioGateway:
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

        # Audio trace instrumentation — lightweight per-tick records written on shutdown.
        # Press 'i' to start/stop recording.  Data is dumped to tools/audio_trace.txt
        # on Ctrl+C shutdown.
        import collections as _collections_mod
        self._audio_trace = _collections_mod.deque(maxlen=12000)  # ~10 minutes at 20Hz
        self._audio_trace_t0 = 0.0  # set when recording starts
        self._trace_recording = False  # toggled by 'i' key
        self._spk_trace = _collections_mod.deque(maxlen=12000)  # speaker thread trace
        self._trace_events = _collections_mod.deque(maxlen=500)  # key presses / mode changes
        
        # Audio processing state
        self.noise_profile = None  # For spectral subtraction
        self.gate_envelope = 0.0  # For noise gate smoothing
        self.highpass_state = None  # For high-pass filter state
        
        # Initialize audio mixer and sources
        self.mixer = AudioMixer(config)
        self.radio_source = None  # Will be initialized after AIOC setup
        self.sdr_source = None  # SDR1 receiver audio source
        self.sdr_muted = False  # SDR1-specific mute
        self.sdr_ducked = False  # Is SDR1 currently being ducked (status display)
        self.sdr_audio_level = 0  # SDR1 audio level for status bar
        self.sdr2_source = None  # SDR2 receiver audio source
        self.sdr2_muted = False  # SDR2-specific mute
        self.sdr2_ducked = False  # Is SDR2 currently being ducked (status display)
        self.sdr2_audio_level = 0  # SDR2 audio level for status bar
        self.remote_audio_server = None   # RemoteAudioServer (role=server)
        self.remote_audio_source = None   # RemoteAudioSource (role=client)
        self.remote_audio_muted = False   # Client: mute toggle
        self.remote_audio_ducked = False  # Client: ducked state for status bar
        self.announce_input_source = None  # NetworkAnnouncementSource (port 9601)
        self.announce_input_muted = False # Announcement input: mute toggle
        self.aioc_available = False  # Track if AIOC is connected

        # SDR rebroadcast — route mixed SDR audio to AIOC radio TX
        self.sdr_rebroadcast = False              # Toggle state (press 'b')
        self._rebroadcast_ptt_hold_until = 0      # monotonic deadline for PTT hold
        self._rebroadcast_ptt_active = False       # whether rebroadcast currently has PTT keyed
        self._rebroadcast_sending = False           # SDR audio actively being sent (for status bar)

        # Relay control — radio power button (momentary pulse with 'j' key)
        self.relay_radio = None              # RelayController instance
        self._relay_radio_pressing = False   # True during 0.5s button pulse

        # Relay control — charger schedule
        self.relay_charger = None      # RelayController instance
        self.relay_charger_on = False  # Current charge state
        self._charger_on_time = None   # (hour, minute) tuple
        self._charger_off_time = None  # (hour, minute) tuple

        # TH-9800 CAT control
        self.cat_client = None  # RadioCATClient instance

        # Mumble Server instances (local mumble-server/murmurd)
        self.mumble_server_1 = None  # MumbleServerManager instance
        self.mumble_server_2 = None  # MumbleServerManager instance

        # DarkIce process monitoring (auto-restart if it dies)
        self._darkice_pid = None          # PID when initially detected
        self._darkice_was_running = False  # True if DarkIce was alive at startup
        self._darkice_restart_count = 0
        self._last_darkice_check = 0

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

    def format_level_bar(self, level, muted=False, ducked=False, color='green'):
        """Format audio level as a visual bar (0-100 scale) with optional color
        
        Args:
            level: Audio level 0-100
            muted: Whether this channel is muted
            ducked: Whether this channel is being ducked (SDR only)
            color: 'green' for RX, 'red' for TX, 'cyan' for SDR
        
        Returns a fixed-width string (same width regardless of muted/ducked/normal state)
        """
        # ANSI color codes
        YELLOW = '\033[93m'
        GREEN = '\033[92m'
        RED = '\033[91m'
        CYAN = '\033[96m'
        MAGENTA = '\033[95m'
        WHITE = '\033[97m'
        RESET = '\033[0m'
        
        # Choose bar color
        if color == 'red':
            bar_color = RED
        elif color == 'cyan':
            bar_color = CYAN
        elif color == 'magenta':
            bar_color = MAGENTA
        elif color == 'yellow':
            bar_color = YELLOW
        else:
            bar_color = GREEN
        
        # All return paths have EXACTLY the same visible character width:
        # 6-char bar + space + 4 chars = 11 visible characters total

        # Show MUTE if muted (fixed width, colored)
        if muted:
            return f"{bar_color}-MUTE-{RESET} {bar_color}M   {RESET}"

        # Show DUCK if ducked (fixed width, colored) - for SDR only
        if ducked:
            return f"{bar_color}-DUCK-{RESET} {bar_color}D   {RESET}"

        # Create a 6-character bar graph
        bar_length = 6
        filled = int((level / 100.0) * bar_length)

        bar = '█' * filled + '-' * (bar_length - filled)
        return f"{bar_color}{bar}{RESET} {YELLOW}{level:3d}%{RESET}"
    
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
    
    def apply_spectral_noise_suppression(self, pcm_data):
        """Apply spectral subtraction to reduce constant background noise"""
        try:
            from scipy.ndimage import uniform_filter1d

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            window_size = 32
            strength = self.config.NOISE_SUPPRESSION_STRENGTH

            # Moving average of absolute values as noise estimate (O(n) vs O(n*w))
            noise_estimate = uniform_filter1d(np.abs(samples), size=window_size, mode='nearest')

            # Where signal exceeds noise threshold: keep as-is; otherwise reduce
            above_threshold = np.abs(samples) > noise_estimate * (1.0 + strength)
            reduction = strength * noise_estimate
            reduced = np.where(samples > 0,
                               np.maximum(0.0, samples - reduction),
                               np.minimum(0.0, samples + reduction))

            processed = np.where(above_threshold, samples, reduced)
            return np.clip(processed, -32768, 32767).astype(np.int16).tobytes()

        except Exception:
            return pcm_data
    
    def process_audio_for_mumble(self, pcm_data):
        """Apply all enabled audio processing to clean up radio audio before sending to Mumble"""
        if not pcm_data:
            return pcm_data
        
        processed = pcm_data
        
        # Apply high-pass filter first (removes low-frequency rumble from radio)
        if self.config.ENABLE_HIGHPASS_FILTER:
            processed = self.apply_highpass_filter(processed)
        
        # Apply noise suppression (removes constant hiss/static from radio)
        if self.config.ENABLE_NOISE_SUPPRESSION:
            if self.config.NOISE_SUPPRESSION_METHOD == 'spectral':
                processed = self.apply_spectral_noise_suppression(processed)
            # Can add other methods here (wiener, etc.)
        
        # Apply noise gate last (cuts residual RF noise/hiss)
        if self.config.ENABLE_NOISE_GATE:
            processed = self.apply_noise_gate(processed)
        
        return processed
    
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
        """Control AIOC PTT"""
        if not self.aioc_device:
            return
        
        try:
            state = 1 if state_on else 0
            iomask = 1 << (self.config.AIOC_PTT_CHANNEL - 1)
            iodata = state << (self.config.AIOC_PTT_CHANNEL - 1)
            data = Struct("<BBBBB").pack(0, 0, iodata, iomask, 0)
            
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio")
                print(f"[PTT] Channel: GPIO{self.config.AIOC_PTT_CHANNEL}")
                print(f"[PTT] Data: {data.hex()}")
            
            self.aioc_device.write(bytes(data))
            
            if self.config.VERBOSE_LOGGING:
                print(f"[PTT] ✓ HID write successful")
            
            # Update PTT state (status line will show it)
            self.ptt_active = state_on
            
        except Exception as e:
            print(f"\n[PTT] ✗ Error: {e}")
            import traceback
            traceback.print_exc()
    
    def sound_received_handler(self, user, soundchunk):
        """Called when audio is received from Mumble server"""
        # Track when we last received audio
        self.last_rx_audio_time = time.time()
        
        # Calculate audio level (with smoothing)
        current_level = self.calculate_audio_level(soundchunk.pcm)
        # Smooth the level display (fast attack, slow decay)
        if current_level > self.rx_audio_level:
            self.rx_audio_level = current_level  # Fast attack
        else:
            self.rx_audio_level = int(self.rx_audio_level * 0.7 + current_level * 0.3)  # Slow decay
        
        # Apply activation delay if configured
        if self.config.PTT_ACTIVATION_DELAY > 0 and not self.ptt_active:
            time.sleep(self.config.PTT_ACTIVATION_DELAY)
        
        # Update last sound time
        self.last_sound_time = time.time()
        
        # Key PTT if not already active AND TX is not muted
        # Don't key the radio if we're muted - that would broadcast silence!
        # Also don't auto-key if manual PTT mode is active
        if not self.ptt_active and not self.tx_muted and not self.manual_ptt_mode:
            # Queue the HID write to the audio thread (between audio reads) to
            # avoid concurrent USB HID + isochronous audio on the AIOC device.
            # Set ptt_active immediately so repeated Mumble callbacks don't
            # queue a second activation before the first is processed.
            self.ptt_active = True
            self._pending_ptt_state = True
            self._ptt_change_time = time.monotonic()
        
        # Play sound to AIOC output (to radio mic input)
        # But only if TX is not muted
        if self.output_stream and not self.tx_muted:
            try:
                # Apply output volume
                pcm = soundchunk.pcm
                if self.config.OUTPUT_VOLUME != 1.0:
                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    pcm = np.clip(arr * self.config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
                
                self.output_stream.write(pcm)
            except Exception as e:
                if self.config.VERBOSE_LOGGING:
                    print(f"\nError playing audio: {e}")
    
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
        import sys
        
        # Only suppress if not verbose
        if not self.config.VERBOSE_LOGGING:
            # Save stderr
            stderr_fd = sys.stderr.fileno()
            saved_stderr = os.dup(stderr_fd)
            
            try:
                # Redirect stderr to /dev/null
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stderr_fd)
                os.close(devnull)
                
                p = pyaudio.PyAudio()
                
            finally:
                # Restore stderr
                os.dup2(saved_stderr, stderr_fd)
                os.close(saved_stderr)
        else:
            # Verbose mode - show ALSA messages
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
        """Apply SPEAKER_VOLUME and enqueue audio for the speaker output thread.
        Non-blocking: if queue is full, drop oldest chunk to absorb clock drift
        between software timer and speaker hardware clock."""
        if not self.speaker_queue or self.speaker_muted or not data:
            return
        try:
            spk = data
            if self.config.SPEAKER_VOLUME != 1.0:
                arr = np.frombuffer(spk, dtype=np.int16).astype(np.float32)
                spk = np.clip(arr * self.config.SPEAKER_VOLUME, -32768, 32767).astype(np.int16).tobytes()
            # Update speaker level for status bar (fast attack, slow decay)
            current_level = self.calculate_audio_level(spk)
            if current_level > self.speaker_audio_level:
                self.speaker_audio_level = current_level
            else:
                self.speaker_audio_level = int(self.speaker_audio_level * 0.7 + current_level * 0.3)
            # Absorb hw/sw clock drift: drain excess when queue gets deep.
            # USB audio clocks can drift ~0.7% from software clock, accumulating
            # ~1 extra chunk per 3.5s. Drain at 4 to keep latency bounded.
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
        """Open optional local speaker monitoring output stream."""
        if not self.config.ENABLE_SPEAKER_OUTPUT:
            return
        try:
            device_index, device_name = self.find_speaker_device(self.pyaudio_instance)
            import queue
            # 6 chunks × 50ms = 300ms of buffer headroom to absorb timing jitter
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
            print(f"Warning: Speaker output failed to open: {e}")
            self.speaker_stream = None

    def setup_audio(self):
        """Initialize PyAudio streams"""
        if self.config.VERBOSE_LOGGING:
            print("Initializing audio...")
        
        # Find AIOC device
        input_idx, output_idx = self.find_aioc_audio_device()
        
        if input_idx is None or output_idx is None:
            print("✗ Could not find AIOC audio device")
            if self.config.AIOC_INPUT_DEVICE < 0 or self.config.AIOC_OUTPUT_DEVICE < 0:
                print("  Using default audio device instead")
        
        # Suppress ALSA warnings during PyAudio initialization if not verbose
        if not self.config.VERBOSE_LOGGING:
            import os
            import sys
            stderr_fd = sys.stderr.fileno()
            saved_stderr = os.dup(stderr_fd)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stderr_fd)
                os.close(devnull)
                self.pyaudio_instance = pyaudio.PyAudio()
            finally:
                os.dup2(saved_stderr, stderr_fd)
                os.close(saved_stderr)
        else:
            self.pyaudio_instance = pyaudio.PyAudio()
        
        # Determine format based on bit depth
        if self.config.AUDIO_BITS == 16:
            audio_format = pyaudio.paInt16
        elif self.config.AUDIO_BITS == 24:
            audio_format = pyaudio.paInt24
        elif self.config.AUDIO_BITS == 32:
            audio_format = pyaudio.paInt32
        else:
            audio_format = pyaudio.paInt16
        
        try:
            # Output stream (Mumble → AIOC → Radio)
            self.output_stream = self.pyaudio_instance.open(
                format=audio_format,
                channels=self.config.AUDIO_CHANNELS,
                rate=self.config.AUDIO_RATE,
                output=True,
                output_device_index=output_idx,
                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 2  # 2x buffer for smooth playback
            )
            if self.config.VERBOSE_LOGGING:
                latency_ms = (self.config.AUDIO_CHUNK_SIZE * 2 / self.config.AUDIO_RATE) * 1000
                print(f"✓ Audio output configured ({latency_ms:.1f}ms buffer)")
            else:
                print("✓ Audio configured")
            
            # Initialize radio source FIRST so we can pass its PortAudio callback
            # to the input stream.  Callback mode lets PortAudio deliver audio in
            # its own real-time thread (SCHED_FIFO when rtprio limits allow) rather
            # than a Python reader thread that can be preempted by the OS scheduler.
            # frames_per_buffer must stay at 1x AUDIO_CHUNK_SIZE (see note below).
            if self.aioc_available:
                try:
                    self.radio_source = AIOCRadioSource(self.config, self)
                    self.mixer.add_source(self.radio_source)
                    if self.config.VERBOSE_LOGGING:
                        print("✓ Radio audio source added to mixer")
                except Exception as source_err:
                    print(f"⚠ Warning: Could not initialize radio source: {source_err}")
                    print("  Continuing without radio audio")
                    self.radio_source = None
            else:
                print("  Radio audio: DISABLED (AIOC not available)")
                self.radio_source = None

            # Input stream (Radio → AIOC → Mumble).
            # frames_per_buffer=4×AUDIO_CHUNK_SIZE sets the ALSA period to 200ms.
            # _audio_callback queues each 200ms blob; get_audio() pre-buffers 3
            # blobs (600ms cushion) then slices into 50ms sub-chunks.
            aioc_callback = self.radio_source._audio_callback if self.radio_source else None
            self.input_stream = self.pyaudio_instance.open(
                format=audio_format,
                channels=self.config.AUDIO_CHANNELS,
                rate=self.config.AUDIO_RATE,
                input=True,
                input_device_index=input_idx,
                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                stream_callback=aioc_callback
            )

            # Start the stream explicitly
            if not self.input_stream.is_active():
                self.input_stream.start_stream()

            # Initialize stream age
            self.stream_age = time.time()

            if self.config.VERBOSE_LOGGING:
                mode = "callback" if aioc_callback else "blocking"
                print(f"✓ Audio input configured ({mode} mode)")

            self.open_speaker_output()

            # Initialize file playback source if enabled
            if self.config.ENABLE_PLAYBACK:
                try:
                    self.playback_source = FilePlaybackSource(self.config, self)
                    self.mixer.add_source(self.playback_source)
                    print("✓ File playback source added to mixer")
                    
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
            if self.config.ENABLE_TTS:
                try:
                    print("Initializing text-to-speech...")
                    from gtts import gTTS
                    self.tts_engine = gTTS  # Store class reference
                    print("✓ Text-to-speech (gTTS) initialized")
                    print("  Use !speak <text> in Mumble to generate TTS")
                except ImportError:
                    print("⚠ gTTS not installed")
                    print("  Install with: pip3 install gtts --break-system-packages")
                    self.tts_engine = None
                except Exception as tts_err:
                    print(f"⚠ Warning: Could not initialize TTS: {tts_err}")
                    self.tts_engine = None
            else:
                print("  Text-to-speech: DISABLED (set ENABLE_TTS = true to enable)")
            
            # Initialize SDR1 source if enabled
            if self.config.ENABLE_SDR:
                try:
                    print("Initializing SDR1 audio source...")
                    _sdr1_cls = PipeWireSDRSource if self.config.SDR_DEVICE_NAME.startswith(('pw:', 'pipewire:')) else SDRSource
                    self.sdr_source = _sdr1_cls(self.config, self, name="SDR1", sdr_priority=self.config.SDR_PRIORITY)
                    if self.sdr_source.setup_audio():
                        # Set initial state from config
                        self.sdr_source.enabled = True
                        self.sdr_source.duck = self.config.SDR_DUCK
                        self.sdr_source.mix_ratio = self.config.SDR_MIX_RATIO
                        self.sdr_source.sdr_priority = self.config.SDR_PRIORITY
                        self.mixer.add_source(self.sdr_source)
                        print("✓ SDR1 audio source added to mixer")
                        print(f"  Device: {self.config.SDR_DEVICE_NAME}")
                        print(f"  Priority: {self.config.SDR_PRIORITY} (1=higher, 2=lower)")
                        if self.config.SDR_DUCK:
                            print(f"  Ducking: ENABLED (SDR silenced when higher priority audio active)")
                        else:
                            print(f"  Ducking: DISABLED (SDR mixed at {self.config.SDR_MIX_RATIO:.1f}x ratio)")
                        print(f"  Press 's' to mute/unmute SDR1")
                    else:
                        print("⚠ Warning: Could not initialize SDR1 audio")
                        self.sdr_source = None
                except Exception as sdr_err:
                    print(f"⚠ Warning: Could not initialize SDR1 source: {sdr_err}")
                    self.sdr_source = None
            else:
                self.sdr_source = None
                if self.config.VERBOSE_LOGGING:
                    print("  SDR1 audio: DISABLED (set ENABLE_SDR = true to enable)")
            
            # Initialize SDR2 source if enabled
            if self.config.ENABLE_SDR2:
                try:
                    print("Initializing SDR2 audio source...")
                    _sdr2_cls = PipeWireSDRSource if self.config.SDR2_DEVICE_NAME.startswith(('pw:', 'pipewire:')) else SDRSource
                    self.sdr2_source = _sdr2_cls(self.config, self, name="SDR2", sdr_priority=self.config.SDR2_PRIORITY)
                    if self.sdr2_source.setup_audio():
                        # Set initial state from config
                        self.sdr2_source.enabled = True
                        self.sdr2_source.duck = self.config.SDR2_DUCK
                        self.sdr2_source.mix_ratio = self.config.SDR2_MIX_RATIO
                        self.sdr2_source.sdr_priority = self.config.SDR2_PRIORITY
                        self.mixer.add_source(self.sdr2_source)
                        print("✓ SDR2 audio source added to mixer")
                        print(f"  Device: {self.config.SDR2_DEVICE_NAME}")
                        print(f"  Priority: {self.config.SDR2_PRIORITY} (1=higher, 2=lower)")
                        if self.config.SDR2_DUCK:
                            print(f"  Ducking: ENABLED (SDR silenced when higher priority audio active)")
                        else:
                            print(f"  Ducking: DISABLED (SDR mixed at {self.config.SDR2_MIX_RATIO:.1f}x ratio)")
                        print(f"  Press 'x' to mute/unmute SDR2")
                    else:
                        print("⚠ Warning: Could not initialize SDR2 audio")
                        print(f"  Device {self.config.SDR2_DEVICE_NAME} not found or already in use")
                        print(f"  Try: arecord -l | grep Loopback")
                        print(f"  SDR2 will show as disabled in status bar")
                        # Keep the source object but disable it so status bar shows
                        self.sdr2_source.enabled = False
                except Exception as sdr2_err:
                    print(f"⚠ Warning: Could not initialize SDR2 source: {sdr2_err}")
                    # Create disabled source object so status bar still shows it
                    try:
                        self.sdr2_source = _sdr2_cls(self.config, self, name="SDR2", sdr_priority=self.config.SDR2_PRIORITY)
                        self.sdr2_source.enabled = False
                    except:
                        self.sdr2_source = None
            else:
                self.sdr2_source = None
                if self.config.VERBOSE_LOGGING:
                    print("  SDR2 audio: DISABLED (set ENABLE_SDR2 = true to enable)")

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
                        self.mixer.add_source(self.remote_audio_source)
                        print(f"✓ Remote audio source (SDRSV) added to mixer")
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
                        self.mixer.add_source(self.announce_input_source)
                        print(f"✓ Announcement input (ANNIN) added to mixer")
                        if not self.aioc_available:
                            print("  ⚠ No AIOC — PTT will not activate (audio discarded)")
                    else:
                        print("⚠ Warning: Could not initialize announcement input")
                        self.announce_input_source = None
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize announcement input: {e}")
                    self.announce_input_source = None

            # Initialize relay controllers
            if getattr(self.config, 'ENABLE_RELAY_RADIO', False):
                try:
                    dev = self.config.RELAY_RADIO_DEVICE
                    print(f"Initializing radio power relay ({dev})...")
                    self.relay_radio = RelayController(dev, self.config.RELAY_RADIO_BAUD)
                    if self.relay_radio.open():
                        self.relay_radio.set_state(False)  # Ensure relay off on startup
                        print(f"  Relay radio: ready (press 'j' to pulse power button)")
                    else:
                        self.relay_radio = None
                except Exception as e:
                    print(f"  Warning: Could not initialize radio relay: {e}")
                    self.relay_radio = None

            if getattr(self.config, 'ENABLE_RELAY_CHARGER', False):
                try:
                    dev = self.config.RELAY_CHARGER_DEVICE
                    print(f"Initializing charger relay ({dev})...")
                    self.relay_charger = RelayController(dev, self.config.RELAY_CHARGER_BAUD)
                    if self.relay_charger.open():
                        # Parse schedule times
                        on_str = str(self.config.RELAY_CHARGER_ON_TIME)
                        off_str = str(self.config.RELAY_CHARGER_OFF_TIME)
                        oh, om = int(on_str.split(':')[0]), int(on_str.split(':')[1])
                        fh, fm = int(off_str.split(':')[0]), int(off_str.split(':')[1])
                        self._charger_on_time = (oh, om)
                        self._charger_off_time = (fh, fm)
                        # Set initial state based on current time
                        should_be_on = self._charger_should_be_on()
                        self.relay_charger.set_state(should_be_on)
                        self.relay_charger_on = should_be_on
                        state_str = "CHARGING" if should_be_on else "DRAINING"
                        print(f"  Charger relay: {state_str} (schedule {on_str}-{off_str})")
                    else:
                        self.relay_charger = None
                except Exception as e:
                    print(f"  Warning: Could not initialize charger relay: {e}")
                    self.relay_charger = None

            # Initialize TH-9800 CAT control
            if getattr(self.config, 'ENABLE_CAT_CONTROL', False):
                try:
                    host = self.config.CAT_HOST
                    port = int(self.config.CAT_PORT)
                    password = str(self.config.CAT_PASSWORD)
                    print(f"Connecting to TH-9800 CAT server ({host}:{port})...")
                    self.cat_client = RadioCATClient(host, port, password)
                    if self.cat_client.connect():
                        print("  Connected to CAT server")
                        # Install SIGINT handler to stop CAT loops
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
                    else:
                        print("  Failed to connect to CAT server")
                        self.cat_client = None
                except Exception as e:
                    print(f"  CAT control error: {e}")
                    self.cat_client = None

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

            # Initialize EchoLink source if enabled (Phase 3B)
            if self.config.ENABLE_ECHOLINK:
                try:
                    print("Initializing EchoLink integration...")
                    self.echolink_source = EchoLinkSource(self.config, self)
                    if self.echolink_source.connected:
                        self.mixer.add_source(self.echolink_source)
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

            # Detect running DarkIce for process monitoring
            if self.config.ENABLE_STREAM_OUTPUT:
                pid = self._find_darkice_pid()
                if pid:
                    self._darkice_pid = pid
                    self._darkice_was_running = True
                    print(f"  DarkIce detected (PID {pid})")

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
                                self.mixer.add_source(self.radio_source)
                                if self.config.VERBOSE_LOGGING:
                                    print("✓ Radio audio source added to mixer")
                            except Exception as source_err:
                                print(f"⚠ Warning: Could not initialize radio source: {source_err}")
                                self.radio_source = None

                            aioc_callback = self.radio_source._audio_callback if self.radio_source else None
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
                                    self.mixer.add_source(self.playback_source)
                                    print("✓ File playback source added to mixer")
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

        print(f"\nConnecting to Mumble: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}...")

        try:
            # Test if server is reachable first
            import socket
            print(f"  Testing connection to {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}...")
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(3)
            try:
                test_sock.connect((self.config.MUMBLE_SERVER, self.config.MUMBLE_PORT))
                test_sock.close()
                print(f"  ✓ Server is reachable")
            except socket.timeout:
                print(f"\n✗ CONNECTION FAILED: Server connection timed out")
                print(f"  Server: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}")
                print(f"\n  Possible causes:")
                print(f"  • Server is not running")
                print(f"  • Wrong IP address in gateway_config.txt")
                print(f"  • Firewall blocking connection")
                print(f"  • Network connectivity issue")
                print(f"\n  Check your config:")
                print(f"    MUMBLE_SERVER = {self.config.MUMBLE_SERVER}")
                print(f"    MUMBLE_PORT = {self.config.MUMBLE_PORT}")
                return False
            except socket.error as e:
                print(f"\n✗ CONNECTION FAILED: {e}")
                print(f"  Server: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}")
                print(f"\n  Possible causes:")
                print(f"  • Wrong IP address (check MUMBLE_SERVER in config)")
                print(f"  • Wrong port (check MUMBLE_PORT in config)")
                print(f"  • Server not running")
                print(f"\n  Current config:")
                print(f"    MUMBLE_SERVER = {self.config.MUMBLE_SERVER}")
                print(f"    MUMBLE_PORT = {self.config.MUMBLE_PORT}")
                return False
            
            # Create Mumble client
            print(f"  Creating Mumble client...")
            self.mumble = Mumble(
                self.config.MUMBLE_SERVER, 
                self.config.MUMBLE_USERNAME,
                port=self.config.MUMBLE_PORT,
                password=self.config.MUMBLE_PASSWORD if self.config.MUMBLE_PASSWORD else '',
                reconnect=self.config.MUMBLE_RECONNECT,
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
            if self.config.VERBOSE_LOGGING:
                print("\n[TTS] Text-to-speech not available")
            return False
        
        if not self.playback_source:
            if self.config.VERBOSE_LOGGING:
                print("\n[TTS] Playback source not available")
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
            
            # Generate TTS audio using gTTS
            voice_num = voice or int(getattr(self.config, 'TTS_DEFAULT_VOICE', 1))
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
                return False
            
            # Verify file exists and has valid content
            import os
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
                if self.sdr_source:
                    sources.append(f"SDR1 ({'muted' if self.sdr_muted else 'active'})")
                if self.sdr2_source:
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

                # Processing — compact
                proc = []
                if self.config.ENABLE_VAD: proc.append("VAD")
                if self.config.ENABLE_NOISE_GATE: proc.append("Gate")
                if self.config.ENABLE_HIGHPASS_FILTER: proc.append("HPF")
                if self.config.ENABLE_AGC: proc.append("AGC")
                if self.config.ENABLE_NOISE_SUPPRESSION: proc.append("NR")
                if self.config.ENABLE_ECHO_CANCELLATION: proc.append("Echo")
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

            elif command == '!help':
                help_text = [
                    "=== Gateway Commands ===",
                    "!speak [voice#] <text> - TTS broadcast (voices 1-9)",
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

                # ── AIOC stream health management ────────────────────────────────
                # These checks are AIOC-specific and must NOT block SDR audio.
                # The mixer runs unconditionally below so SDR always reaches Mumble.
                if self.input_stream and not self.restarting_stream:
                    current_time = time.time()
                    time_since_creation = current_time - self.stream_age
                    time_since_vad_active = current_time - self.last_vox_active_time if hasattr(self, 'last_vox_active_time') else 999

                    # Proactive AIOC restart (optional feature, brief gap acceptable)
                    if (self.config.ENABLE_STREAM_HEALTH and
                            self.config.STREAM_RESTART_INTERVAL > 0 and
                            time_since_creation > self.config.STREAM_RESTART_INTERVAL):
                        if not self.vad_active and time_since_vad_active > self.config.STREAM_RESTART_IDLE_TIME:
                            if self.config.VERBOSE_LOGGING:
                                print(f"\n[Maintenance] Proactive stream restart (age: {time_since_creation:.0f}s, idle: {time_since_vad_active:.0f}s)")
                            self.restart_audio_input()
                            self.stream_age = time.time()
                            time.sleep(0.2)
                            continue

                    # AIOC stream inactive: restart it but do NOT raise or skip the
                    # mixer.  AIOCRadioSource.get_audio() returns None while
                    # restarting_stream is True, so only SDR audio flows until AIOC
                    # recovers — which is exactly what the user wants.
                    if not self.input_stream.is_active():
                        if self.config.VERBOSE_LOGGING:
                            print("\n[Diagnostic] AIOC stream inactive, restarting...")
                        self.restart_audio_input()
                        # Fall through — SDR still runs below

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
                    _tr_sdr_q = self.sdr_source._chunk_queue.qsize() if self.sdr_source and self.sdr_source.input_stream else -1
                    _tr_sdr_sb = len(self.sdr_source._sub_buffer) if self.sdr_source and self.sdr_source.input_stream else -1
                    _tr_sdr_prebuf = self.sdr_source._prebuffering if self.sdr_source else False
                    _tr_sdr2_q = self.sdr2_source._chunk_queue.qsize() if self.sdr2_source and getattr(self.sdr2_source, 'enabled', False) and self.sdr2_source.input_stream else -1
                    _tr_sdr2_sb = len(self.sdr2_source._sub_buffer) if self.sdr2_source and getattr(self.sdr2_source, 'enabled', False) and self.sdr2_source.input_stream else -1
                    _tr_sdr2_prebuf = self.sdr2_source._prebuffering if self.sdr2_source and getattr(self.sdr2_source, 'enabled', False) else False
                    _tr_aioc_q = self.radio_source._chunk_queue.qsize() if self.radio_source else -1
                    _tr_aioc_sb = len(self.radio_source._sub_buffer) if self.radio_source else -1

                    _tr_mixer_t0 = time.monotonic()
                    data, ptt_required, active_sources, sdr1_was_ducked, sdr2_was_ducked, rx_audio, sdrsv_was_ducked, sdr_only_audio = self.mixer.get_mixed_audio(self.config.AUDIO_CHUNK_SIZE)
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
                    if data is None:
                        # No audio from any source — nothing to send.
                        # Feed silence to speaker so its PortAudio buffer stays primed,
                        # but skip the Mumble/remote-audio send path entirely.
                        self.audio_capture_active = False
                        self._speaker_enqueue(b'\x00' * (self.config.AUDIO_CHUNK_SIZE * 2))
                        _tr_outcome = 'vad_gate'
                        continue
                    else:
                        # Mixer produced audio (from any source: AIOC, SDR, file).
                        # Update health flags so the status monitor doesn't think
                        # audio capture has stopped and trigger restart_audio_input().
                        self.last_audio_capture_time = time.time()
                        self.audio_capture_active = True

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

                    # Route audio based on PTT requirement
                    if ptt_required:
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
                        if self.output_stream and not self.tx_muted:
                            try:
                                # Suppress audio while the PTT relay is settling.
                                # announcement_delay_active is set in the same iteration
                                # that PTT first activates, so data already holds a real
                                # audio chunk — replace it with silence here too.
                                pcm = b'\x00' * len(data) if self.announcement_delay_active else data
                                if self.config.OUTPUT_VOLUME != 1.0:
                                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                    pcm = np.clip(arr * self.config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
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

                        # Forward concurrent radio RX to Mumble/stream even during
                        # announcement playback (full-duplex monitoring).
                        rx_for_mumble = (
                            getattr(self.radio_source, '_rx_cache', None)
                            if self.radio_source else rx_audio
                        )
                        if rx_for_mumble is not None:
                            if (self.mumble and
                                    hasattr(self.mumble, 'sound_output') and
                                    self.mumble.sound_output is not None and
                                    getattr(self.mumble.sound_output, 'encoder_framesize', None) is not None):
                                try:
                                    self.mumble.sound_output.add_sound(rx_for_mumble)
                                except Exception:
                                    pass
                            if self.stream_output and self.stream_output.connected:
                                try:
                                    self.stream_output.send_audio(rx_for_mumble)
                                except Exception:
                                    pass
                            if self.speaker_stream and not self.speaker_muted:
                                self._speaker_enqueue(rx_for_mumble)

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

                    # No PTT required (radio RX / SDR) — falls through to Mumble send

                elif self.input_stream and not self.restarting_stream:
                    # Fallback: direct AIOC read only (no mixer / no SDR)
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

                    if not self.check_vad(data):
                        # Speaker bypasses VAD — monitor even when Mumble is gated
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

                # Speaker output — send whatever Mumble gets (real audio or silence).
                # The speaker's PortAudio hardware buffer (~200ms) smooths timing.
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

                if not self.mumble:
                    _tr_outcome = 'no_mumble'
                    continue

                if not hasattr(self.mumble, 'sound_output') or self.mumble.sound_output is None:
                    _tr_outcome = 'no_sndout'
                    continue

                if not hasattr(self.mumble.sound_output, 'encoder_framesize') or self.mumble.sound_output.encoder_framesize is None:
                    if self.mixer and self.mixer.call_count % 500 == 1:
                        print(f"\n⚠ Mumble codec still not ready (encoder_framesize is None)")
                        print(f"   Waiting for server negotiation to complete...")
                        print(f"   Check that MUMBLE_SERVER = {self.config.MUMBLE_SERVER} is correct")
                    _tr_outcome = 'no_codec'
                    continue

                try:
                    _tr_m_t0 = time.monotonic()
                    self.mumble.sound_output.add_sound(data)
                    _tr_mumble_ms = (time.monotonic() - _tr_m_t0) * 1000
                    _tr_outcome = 'sent'
                except Exception as send_err:
                    _tr_outcome = 'send_err'
                    print(f"\n[Error] Failed to send to Mumble: {send_err}")
                    import traceback
                    traceback.print_exc()

                if self.echolink_source and self.config.RADIO_TO_ECHOLINK:
                    try:
                        self.echolink_source.send_audio(data)
                    except Exception as el_err:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n[EchoLink] Send error: {el_err}")

                if self.stream_output and self.stream_output.connected:
                    try:
                        self.stream_output.send_audio(data)
                    except Exception as stream_err:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n[Stream] Send error: {stream_err}")

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
                    _sdr1_disc = self.sdr_source._serve_discontinuity if self.sdr_source and self.sdr_source.input_stream else 0.0
                    _sdr1_sb_after = self.sdr_source._sub_buffer_after if self.sdr_source and self.sdr_source.input_stream else -1
                    _sdr1_cb_ovf = self.sdr_source._cb_overflow_count if self.sdr_source else 0
                    _sdr1_cb_drop = self.sdr_source._cb_drop_count if self.sdr_source else 0
                    _aioc_disc = self.radio_source._serve_discontinuity if self.radio_source else 0.0
                    _aioc_sb_after = self.radio_source._sub_buffer_after if self.radio_source else -1
                    _aioc_cb_ovf = self.radio_source._cb_overflow_count if self.radio_source else 0
                    _aioc_cb_drop = self.radio_source._cb_drop_count if self.radio_source else 0

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
                        self.sdr_source._last_blocked_ms if self.sdr_source and self.sdr_source.input_stream else 0.0,  # 9: SDR blocked (ms)
                        self.radio_source._last_blocked_ms if self.radio_source else 0.0,  # 10: AIOC blocked (ms)
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

        # Use the original stderr fd (may have been redirected by StatusBarWriter)
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
            
            aioc_callback = self.radio_source._audio_callback if self.radio_source else None
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
                aioc_callback = self.radio_source._audio_callback if self.radio_source else None
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
    
    def keyboard_listener_loop(self):
        """Listen for keyboard input to toggle mute states"""
        import sys
        import tty
        import termios
        
        # Note: Priority scheduling removed - system manages all threads
        
        # Save terminal settings
        try:
            old_settings = termios.tcgetattr(sys.stdin)
        except:
            # Not running in a terminal, can't capture keyboard
            if self.config.VERBOSE_LOGGING:
                print("  [Warning] Keyboard controls not available (not in terminal)")
            return

        # Store on instance so cleanup() can restore if this daemon thread is killed
        self._terminal_settings = old_settings

        try:
            # Set terminal to raw mode for character-by-character input
            tty.setcbreak(sys.stdin.fileno())
            
            while self.running:
                # Check if input is available (non-blocking)
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    char = sys.stdin.read(1).lower()
                    
                    if char == 't':
                        # Toggle TX mute (Mumble → Radio)
                        self.tx_muted = not self.tx_muted
                        self._trace_events.append((time.monotonic(), 'tx_mute', 'on' if self.tx_muted else 'off'))

                    elif char == 'r':
                        # Toggle RX mute (Radio → Mumble)
                        self.rx_muted = not self.rx_muted
                        self._trace_events.append((time.monotonic(), 'rx_mute', 'on' if self.rx_muted else 'off'))
                    
                    elif char == 'm':
                        # Global mute toggle
                        if self.tx_muted and self.rx_muted:
                            # Both muted → unmute both
                            self.tx_muted = False
                            self.rx_muted = False
                        else:
                            # One or both unmuted → mute both
                            self.tx_muted = True
                            self.rx_muted = True
                        self._trace_events.append((time.monotonic(), 'global_mute', f'tx={self.tx_muted} rx={self.rx_muted}'))
                    
                    elif char == 's':
                        # Toggle SDR mute
                        if self.sdr_source:
                            self.sdr_muted = not self.sdr_muted
                            self.sdr_source.muted = self.sdr_muted
                            self._trace_events.append((time.monotonic(), 'sdr_mute', 'on' if self.sdr_muted else 'off'))
                            if self.config.VERBOSE_LOGGING:
                                state = "MUTED" if self.sdr_muted else "UNMUTED"
                                print(f"\n[SDR] {state}")
                    
                    elif char == 'd':
                        # Toggle SDR ducking on/off
                        if self.sdr_source:
                            self.sdr_source.duck = not self.sdr_source.duck
                            if self.config.VERBOSE_LOGGING:
                                if self.sdr_source.duck:
                                    print(f"\n[SDR1] Ducking ENABLED (SDR silenced when higher priority audio active)")
                                else:
                                    print(f"\n[SDR1] Ducking DISABLED (SDR mixed at {self.sdr_source.mix_ratio:.1f}x ratio)")
                    
                    elif char == 'x':
                        # Toggle SDR2 mute
                        if self.sdr2_source:
                            self.sdr2_muted = not self.sdr2_muted
                            self.sdr2_source.muted = self.sdr2_muted
                            self._trace_events.append((time.monotonic(), 'sdr2_mute', 'on' if self.sdr2_muted else 'off'))
                            if self.config.VERBOSE_LOGGING:
                                state = "MUTED" if self.sdr2_muted else "UNMUTED"
                                print(f"\n[SDR2] {state}")
                    
                    elif char == 'c':
                        # Toggle remote audio mute (client only)
                        if self.remote_audio_source:
                            self.remote_audio_muted = not self.remote_audio_muted
                            self.remote_audio_source.muted = self.remote_audio_muted
                            self._trace_events.append((time.monotonic(), 'remote_mute', 'on' if self.remote_audio_muted else 'off'))
                            if self.config.VERBOSE_LOGGING:
                                state = "MUTED" if self.remote_audio_muted else "UNMUTED"
                                print(f"\n[SDRSV] {state}")

                    elif char == 'k':
                        # Reset remote audio TCP connection
                        if self.remote_audio_server:
                            self.remote_audio_server.reset()
                            print(f"\n[RemoteAudio] Server connection reset — reconnecting to {self.remote_audio_server.host}:{self.remote_audio_server.port}")
                            self._trace_events.append((time.monotonic(), 'remote_reset', 'server'))
                        elif self.remote_audio_source:
                            self.remote_audio_source.reset()
                            print(f"\n[SDRSV] Client connection reset — waiting for reconnect")
                            self._trace_events.append((time.monotonic(), 'remote_reset', 'client'))

                    elif char == 'v':
                        # Toggle VAD on/off
                        self.config.ENABLE_VAD = not self.config.ENABLE_VAD
                    
                    elif char == ',':
                        # Decrease RX volume (Radio → Mumble)
                        self.config.INPUT_VOLUME = max(0.1, self.config.INPUT_VOLUME - 0.1)
                    
                    elif char == '.':
                        # Increase RX volume (Radio → Mumble)
                        self.config.INPUT_VOLUME = min(3.0, self.config.INPUT_VOLUME + 0.1)
                    
                    elif char == 'n':
                        # Toggle noise gate
                        self.config.ENABLE_NOISE_GATE = not self.config.ENABLE_NOISE_GATE
                    
                    elif char == 'f':
                        # Toggle high-pass filter
                        self.config.ENABLE_HIGHPASS_FILTER = not self.config.ENABLE_HIGHPASS_FILTER
                    
                    elif char == 'a':
                        # Toggle announcement input mute
                        if self.announce_input_source:
                            self.announce_input_muted = not self.announce_input_muted
                            self.announce_input_source.muted = self.announce_input_muted
                            if self.config.VERBOSE_LOGGING:
                                state = "MUTED" if self.announce_input_muted else "UNMUTED"
                                print(f"\n[ANNIN] {state}")

                    elif char == 'g':
                        # Toggle AGC
                        self.config.ENABLE_AGC = not self.config.ENABLE_AGC
                    
                    elif char == 'y':
                        # Toggle spectral noise suppression
                        if self.config.ENABLE_NOISE_SUPPRESSION and self.config.NOISE_SUPPRESSION_METHOD == 'spectral':
                            # Currently on with spectral → turn off
                            self.config.ENABLE_NOISE_SUPPRESSION = False
                        else:
                            # Turn on with spectral
                            self.config.ENABLE_NOISE_SUPPRESSION = True
                            self.config.NOISE_SUPPRESSION_METHOD = 'spectral'
                    
                    elif char == 'w':
                        # Toggle Wiener noise suppression
                        if self.config.ENABLE_NOISE_SUPPRESSION and self.config.NOISE_SUPPRESSION_METHOD == 'wiener':
                            # Currently on with wiener → turn off
                            self.config.ENABLE_NOISE_SUPPRESSION = False
                        else:
                            # Turn on with wiener
                            self.config.ENABLE_NOISE_SUPPRESSION = True
                            self.config.NOISE_SUPPRESSION_METHOD = 'wiener'
                    
                    elif char == 'e':
                        # Toggle echo cancellation
                        self.config.ENABLE_ECHO_CANCELLATION = not self.config.ENABLE_ECHO_CANCELLATION
                    
                    elif char == 'p':
                        # Toggle manual PTT mode (requires AIOC)
                        if not self.aioc_device:
                            if self.config.VERBOSE_LOGGING:
                                print(f"\n[Keyboard] PTT disabled — no AIOC device")
                        else:
                            # Queue the HID write so it runs between audio reads in the
                            # audio thread rather than concurrently with input_stream.read().
                            # This prevents simultaneous USB HID + isochronous audio
                            # transfers on the same AIOC composite device, which can
                            # cause a brief audio click.
                            self.manual_ptt_mode = not self.manual_ptt_mode
                            self._pending_ptt_state = self.manual_ptt_mode
                            self._trace_events.append((time.monotonic(), 'ptt', 'on' if self.manual_ptt_mode else 'off'))

                    elif char == 'b':
                        # Toggle SDR rebroadcast (route SDR mix to radio TX)
                        self.sdr_rebroadcast = not self.sdr_rebroadcast
                        if not self.sdr_rebroadcast:
                            if self._rebroadcast_ptt_active and self.ptt_active:
                                self.set_ptt_state(False)
                                self._ptt_change_time = time.monotonic()
                                self._rebroadcast_ptt_active = False
                            # Re-enable AIOC source if it was disabled during rebroadcast TX
                            if self.radio_source:
                                self.radio_source.enabled = True
                            self._rebroadcast_sending = False
                            self._rebroadcast_ptt_hold_until = 0
                        self._trace_events.append((time.monotonic(), 'sdr_rebroadcast', 'on' if self.sdr_rebroadcast else 'off'))

                    elif char == 'j':
                        # Pulse radio power button (relay ON 0.5s then OFF)
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

                    elif char == 'i':
                        # Toggle audio trace recording
                        self._trace_recording = not self._trace_recording
                        if self._trace_recording:
                            self._audio_trace.clear()
                            self._spk_trace.clear()
                            self._trace_events.clear()
                            self._audio_trace_t0 = time.monotonic()
                            print(f"\n[Trace] Recording STARTED (press 'i' again to stop)")
                        else:
                            print(f"\n[Trace] Recording STOPPED ({len(self._audio_trace)} ticks captured)")
                        self._trace_events.append((time.monotonic(), 'trace', 'on' if self._trace_recording else 'off'))

                    elif char == 'u':
                        # Toggle watchdog trace (long-running diagnostics)
                        self._watchdog_active = not self._watchdog_active
                        if self._watchdog_active:
                            self._watchdog_t0 = time.monotonic()
                            self._watchdog_thread = threading.Thread(
                                target=self._watchdog_trace_loop, daemon=True)
                            self._watchdog_thread.start()
                            print(f"\n[Watchdog] Trace STARTED — sampling every 5s, flushing to tools/watchdog_trace.txt every 60s")
                        else:
                            print(f"\n[Watchdog] Trace STOPPED")

                    elif char == 'o':
                        # Toggle speaker output mute
                        if self.speaker_stream:
                            self.speaker_muted = not self.speaker_muted
                            self._trace_events.append((time.monotonic(), 'spk_mute', 'on' if self.speaker_muted else 'off'))

                    elif char in '0123456789':
                        # Play announcement 0-9 (requires AIOC for radio TX)
                        if not self.aioc_device:
                            if self.config.VERBOSE_LOGGING:
                                print(f"\n[Keyboard] Announcement keys disabled — no AIOC device")
                        elif self.playback_source:
                            # Use the stored path from file_status
                            stored_path = self.playback_source.file_status[char]['path']
                            stored_filename = self.playback_source.file_status[char].get('filename', '')
                            
                            if stored_path:
                                # File exists, queue it directly
                                if self.config.VERBOSE_LOGGING:
                                    print(f"\n[Keyboard] Key '{char}' pressed - queueing {stored_filename}")
                                self.playback_source.queue_file(stored_path)
                            else:
                                # File not found
                                if self.config.VERBOSE_LOGGING:
                                    if char == '0':
                                        print(f"\n[Playback] Station ID not found (looked for station_id.mp3 or station_id.wav)")
                                    else:
                                        print(f"\n[Playback] No file assigned to key '{char}'")
                        else:
                            if self.config.VERBOSE_LOGGING:
                                print("\n[Keyboard] File playback not enabled")
                    
                    elif char == '-':
                        # Stop playback
                        if self.playback_source:
                            if self.config.VERBOSE_LOGGING:
                                print("\n[Keyboard] Key '-' pressed - stopping playback")
                            self.playback_source.stop_playback()
                        else:
                            if self.config.VERBOSE_LOGGING:
                                print("\n[Keyboard] File playback not enabled")

                    elif char == 'q':
                        # Restart gateway (re-exec Python process, reloads config)
                        print(f"\n[Keyboard] Restarting gateway...")
                        self.restart_requested = True
                        self.running = False

                    elif char == 'z':
                        # Clear console and reprint banner
                        writer = getattr(sys.stdout, '_orig', sys.stdout)
                        writer.write("\033[2J\033[H")
                        writer.flush()
                        if hasattr(sys.stdout, '_bar_drawn'):
                            sys.stdout._bar_drawn = False
                        self._print_banner()

                time.sleep(0.05)
        
        finally:
            # Restore terminal settings
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except:
                pass
    
    def status_monitor_loop(self):
        """Monitor PTT release timeout and audio transmit status"""
        # Note: Priority scheduling removed - system manages all threads

        status_check_interval = self.config.STATUS_UPDATE_INTERVAL
        last_status_check = time.time()

        while self.running:
          try:
            current_time = time.time()

            # Check PTT timeout or if TX is muted
            if self.ptt_active and not self.manual_ptt_mode and not self._rebroadcast_ptt_active:
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
                
                # Check audio transmit status
                time_since_last_capture = current_time - self.last_audio_capture_time
                
                # ANSI color codes
                YELLOW = '\033[93m'
                GREEN = '\033[92m'
                RED = '\033[91m'
                ORANGE = '\033[33m'
                WHITE = '\033[97m'
                GRAY = '\033[90m'
                CYAN = '\033[96m'
                MAGENTA = '\033[95m'
                RESET = '\033[0m'
                
                # Format status with color-coded symbols (fixed width for alignment)
                if self.audio_capture_active and time_since_last_capture < 2.0:
                    status_label = "  "  # padding to keep fixed width
                    status_symbol = f"{GREEN}✓{RESET}"
                elif time_since_last_capture < 10.0:
                    status_label = "  "
                    status_symbol = f"{ORANGE}⚠{RESET}"
                else:
                    status_label = "  "
                    status_symbol = f"{RED}✗{RESET}"
                    # Attempt recovery only if AIOC is expected
                    if self.aioc_available:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n{WHITE}{status_label}:{RESET} {status_symbol}")
                            print("  Attempting to restart audio input...")
                        self.restart_audio_input()
                        continue
                
                # Print status
                # Status symbols with colors
                mumble_status = f"{GREEN}✓{RESET}" if self.mumble else f"{RED}✗{RESET}"
                # PTT status: Always 4 chars wide for alignment
                if self.manual_ptt_mode:
                    ptt_status = f"{YELLOW}M-{GREEN}ON{RESET}" if self.ptt_active else f"{YELLOW}M-{GRAY}--{RESET}"
                elif self._rebroadcast_ptt_active:
                    ptt_status = f"{CYAN}B-{GREEN}ON{RESET}" if self.ptt_active else f"{CYAN}B-{GRAY}--{RESET}"
                else:
                    # Pad normal mode to 4 chars to match manual mode width
                    ptt_status = f"  {GREEN}ON{RESET}" if self.ptt_active else f"  {GRAY}--{RESET}"
                
                # VAD status: Always 2 chars wide for alignment
                if not self.config.ENABLE_VAD:
                    vad_status = f"{RED}✗ {RESET}"  # VAD disabled (red X + space) - 2 chars
                elif self.vad_active:
                    vad_status = f"{GREEN}🔊{RESET}"  # VAD active (green speaker) - 2 chars (emoji width)
                else:
                    vad_status = f"{GRAY}--{RESET}"  # VAD silent (gray) - 2 chars
                
                # Format audio levels with bar graphs
                # Note: From radio's perspective:
                #   - rx_audio_level = Mumble → Radio (Radio TX) - RED
                #   - tx_audio_level = Radio → Mumble (Radio RX) - GREEN
                radio_tx_bar = self.format_level_bar(self.rx_audio_level, muted=self.tx_muted, color='red')
                
                # RX bar: Show 0% if VAD is blocking (not actually transmitting to Mumble)
                # Only show level when VAD is active (actually sending to Mumble)
                if self.config.ENABLE_VAD and not self.vad_active:
                    radio_rx_bar = self.format_level_bar(0, muted=self.rx_muted, color='green')  # Not transmitting = 0%
                else:
                    radio_rx_bar = self.format_level_bar(self.tx_audio_level, muted=self.rx_muted, color='green')
                
                # SDR bar: Show SDR audio level (CYAN color)
                # Calculate once so it is always defined regardless of which SDR sources are present
                global_muted = self.tx_muted and self.rx_muted

                # Determine SDR label color based on rebroadcast state
                if self.sdr_rebroadcast:
                    sdr_label_color = RED if self._rebroadcast_sending else GREEN
                else:
                    sdr_label_color = WHITE

                sdr_bar = ""
                if self.sdr_source:
                    # Always read current level directly from source
                    # Don't cache in self.sdr_audio_level to prevent freezing
                    if hasattr(self.sdr_source, 'audio_level'):
                        current_sdr_level = self.sdr_source.audio_level
                    else:
                        current_sdr_level = 0

                    # Determine display state
                    # Mirror SDRSource.get_audio(): discard when individually muted OR globally muted
                    sdr_muted = self.sdr_muted or global_muted
                    # Only show DUCK when SDR has actual signal; silence on an idle
                    # loopback looks the same as "ducked signal" but means nothing.
                    sdr_ducked = self.sdr_ducked if not sdr_muted and current_sdr_level > 0 else False
                    
                    # Format: SDR1: (no mode indicator here - it goes in proc_flags)
                    sdr_bar = f" {sdr_label_color}SDR1:{RESET}" + self.format_level_bar(current_sdr_level, muted=sdr_muted, ducked=sdr_ducked, color='cyan')
                    sdr_bar += f"{RED}P{RESET}" if self.sdr_source._prebuffering else " "
                    if self.sdr_source._watchdog_restarts > 0:
                        sdr_bar += f"{YELLOW}W{self.sdr_source._watchdog_restarts}{RESET}"

                # SDR2 bar: Show SDR2 audio level (MAGENTA color for differentiation)
                sdr2_bar = ""
                if self.sdr2_source and self.sdr2_source.enabled:
                    # Always read current level directly from source
                    if hasattr(self.sdr2_source, 'audio_level'):
                        current_sdr2_level = self.sdr2_source.audio_level
                    else:
                        current_sdr2_level = 0
                    
                    # Determine display state
                    sdr2_muted = self.sdr2_muted or global_muted
                    sdr2_ducked = self.sdr2_ducked if not sdr2_muted and current_sdr2_level > 0 else False
                    
                    # Format: SDR2: with magenta color
                    sdr2_bar = f" {sdr_label_color}SDR2:{RESET}" + self.format_level_bar(current_sdr2_level, muted=sdr2_muted, ducked=sdr2_ducked, color='magenta')
                    sdr2_bar += f"{RED}P{RESET}" if self.sdr2_source._prebuffering else " "
                    if self.sdr2_source._watchdog_restarts > 0:
                        sdr2_bar += f"{YELLOW}W{self.sdr2_source._watchdog_restarts}{RESET}"

                # Remote audio bar (server: SV with tx level; client: CL with rx level)
                remote_bar = ""
                if self.remote_audio_server:
                    # This machine is the server — show audio level being sent to client
                    sv_level = self.sv_audio_level if self.remote_audio_server.connected else 0
                    # Decay toward zero so the bar doesn't stick when no audio is sent
                    if self.sv_audio_level > 0:
                        self.sv_audio_level = int(self.sv_audio_level * 0.7)
                    remote_bar = f" {WHITE}SV:{RESET}" + self.format_level_bar(sv_level, color='yellow')
                elif self.remote_audio_source:
                    # This machine is the client — show audio level received from server
                    current_cl_level = getattr(self.remote_audio_source, 'audio_level', 0)
                    cl_muted = self.remote_audio_muted or global_muted
                    cl_ducked = self.remote_audio_ducked if not cl_muted and current_cl_level > 0 else False
                    remote_bar = f" {WHITE}CL:{RESET}" + self.format_level_bar(current_cl_level, muted=cl_muted, ducked=cl_ducked, color='green')

                # Announcement input bar (AN: — red like TX, shown when enabled)
                annin_bar = ""
                if self.announce_input_source:
                    an_level = getattr(self.announce_input_source, 'audio_level', 0)
                    an_connected = getattr(self.announce_input_source, 'client_connected', False)
                    an_muted = self.announce_input_muted or global_muted
                    annin_bar = f" {WHITE}AN:{RESET}" + self.format_level_bar(
                        an_level if an_connected else 0, muted=an_muted, color='red'
                    )

                # Add diagnostics if there have been restarts (fixed width: always 6 chars like " R:123" or "      ")
                # This prevents the status line from jumping when restarts occur
                if self.stream_restart_count > 0:
                    diag = f" {WHITE}R:{YELLOW}{self.stream_restart_count}{RESET}"
                else:
                    diag = "      "  # 6 spaces to match " R:XX" width
                # DarkIce restart count
                if self._darkice_restart_count > 0:
                    diag += f" {WHITE}S:{YELLOW}{self._darkice_restart_count}{RESET}"
                
                # Show VAD level in dB if enabled (white label, yellow numbers, fixed width: always 6 chars like " -100dB" or "      ")
                vad_info = f" {YELLOW}{self.vad_envelope:4.0f}{RESET}{WHITE}dB{RESET}" if self.config.ENABLE_VAD else "       "
                
                # Show RX volume (white label, yellow number, always 3 chars for number)
                vol_info = f" {WHITE}Vol:{YELLOW}{self.config.INPUT_VOLUME:3.1f}{RESET}{WHITE}x{RESET}"
                
                # Show audio processing status (compact single-letter flags)
                # This now appears AFTER file status, so width changes don't matter
                proc_flags = []
                if self.config.ENABLE_NOISE_GATE: proc_flags.append("N")
                if self.config.ENABLE_HIGHPASS_FILTER: proc_flags.append("F")
                if self.config.ENABLE_AGC: proc_flags.append("G")
                if self.config.ENABLE_NOISE_SUPPRESSION:
                    if self.config.NOISE_SUPPRESSION_METHOD == 'spectral': proc_flags.append("S")
                    elif self.config.NOISE_SUPPRESSION_METHOD == 'wiener': proc_flags.append("W")
                if self.config.ENABLE_ECHO_CANCELLATION: proc_flags.append("E")
                if not self.config.ENABLE_STREAM_HEALTH: proc_flags.append("X")  # X shows stream health is OFF
                # D flag: SDR ducking enabled (only show if SDR is present)
                if self.sdr_source and hasattr(self.sdr_source, 'duck') and self.sdr_source.duck:
                    proc_flags.append("D")
                
                # Only show brackets if there are flags (saves space)
                proc_info = f" {WHITE}[{YELLOW}{','.join(proc_flags)}{WHITE}]{RESET}" if proc_flags else ""
                
                # File status indicators (if playback enabled)
                file_status_info = ""
                if self.playback_source:
                    file_status_info = " " + self.playback_source.get_file_status_string()

                # Speaker output indicator
                sp_bar = ""
                if self.config.ENABLE_SPEAKER_OUTPUT and self.speaker_stream:
                    sp_bar = f" {WHITE}SP:{RESET}" + self.format_level_bar(
                        self.speaker_audio_level, muted=self.speaker_muted, color='cyan'
                    )

                # Relay status indicators (fixed width, only shown when enabled)
                relay_bar = ""
                if self.relay_radio:
                    if self._relay_radio_pressing:
                        relay_bar += f" {RED}PWRB{RESET}"
                    else:
                        relay_bar += f" {WHITE}PWRB{RESET}"
                if self.relay_charger:
                    if self.relay_charger_on:
                        relay_bar += f" {WHITE}CHG:{GREEN}CHRGE{RESET}"
                    else:
                        relay_bar += f" {WHITE}CHG:{RED}DRAIN{RESET}"

                # CAT control status indicator
                cat_bar = ""
                if self.cat_client:
                    if time.monotonic() - self.cat_client._last_activity < 1.0:
                        cat_bar = f" {RED}CAT{RESET}"
                    else:
                        cat_bar = f" {GREEN}CAT{RESET}"
                elif getattr(self.config, 'ENABLE_CAT_CONTROL', False):
                    cat_bar = f" {WHITE}CAT{RESET}"

                # Mumble Server status indicators
                msrv_bar = ""
                for _ms_inst, _ms_label in [(self.mumble_server_1, 'MS1'), (self.mumble_server_2, 'MS2')]:
                    if _ms_inst and _ms_inst.is_enabled():
                        _ms_state = _ms_inst.state
                        if _ms_state == MumbleServerManager.STATE_RUNNING:
                            msrv_bar += f" {GREEN}{_ms_label}{RESET}"
                        elif _ms_state == MumbleServerManager.STATE_ERROR:
                            msrv_bar += f" {RED}{_ms_label}{RESET}"
                        else:
                            msrv_bar += f" {WHITE}{_ms_label}{RESET}"

                # Extra padding to clear any orphaned text when line shortens
                # Order: ...Vol → FileStatus → ProcessingFlags → Diagnostics
                status_line = f"{status_symbol} {WHITE}M:{RESET}{mumble_status} {WHITE}PTT:{RESET}{ptt_status} {WHITE}VAD:{RESET}{vad_status}{vad_info} {WHITE}TX:{RESET}{radio_tx_bar} {WHITE}RX:{RESET}{radio_rx_bar}{sp_bar}{sdr_bar}{sdr2_bar}{remote_bar}{annin_bar}{relay_bar}{cat_bar}{msrv_bar}{vol_info}{file_status_info}{proc_info}{diag}     "
                # Truncate to terminal width to prevent line wrapping (which
                # breaks \r-based single-line updates).  Count only visible
                # chars (not ANSI escapes) and cut at terminal_width - 1.
                try:
                    import shutil as _shutil
                    _term_cols = _shutil.get_terminal_size().columns
                    import re as _re
                    _visible_len = len(_re.sub(r'\033\[[0-9;]*m', '', status_line))
                    if _visible_len > _term_cols - 1:
                        _out = []
                        _vcount = 0
                        _i = 0
                        while _i < len(status_line) and _vcount < _term_cols - 1:
                            if status_line[_i] == '\033':
                                _j = status_line.find('m', _i)
                                if _j != -1:
                                    _out.append(status_line[_i:_j+1])
                                    _i = _j + 1
                                    continue
                            _out.append(status_line[_i])
                            _vcount += 1
                            _i += 1
                        status_line = ''.join(_out) + RESET
                except Exception:
                    pass

                self._status_writer.draw_status(status_line)
            
            # Always check for stuck audio (even if status reporting is disabled)
            elif status_check_interval == 0:
                time_since_last_capture = current_time - self.last_audio_capture_time
                if time_since_last_capture > 30.0:  # 30 seconds with no audio = stuck
                    if self.config.VERBOSE_LOGGING:
                        print(f"\n✗ Audio TX stuck (no audio for {int(time_since_last_capture)}s)")
                        print("  Attempting to restart audio input...")
                    self.restart_audio_input()
                    time.sleep(5)  # Wait before checking again
            
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

            # Charger relay schedule check (only on state change)
            if self.relay_charger:
                should_on = self._charger_should_be_on()
                if should_on != self.relay_charger_on:
                    self.relay_charger.set_state(should_on)
                    self.relay_charger_on = should_on
                    self._trace_events.append((time.monotonic(), 'relay_charger', 'on' if should_on else 'off'))

            # SDR loopback watchdog checks
            if self.sdr_source and self.sdr_source.enabled:
                self.sdr_source.check_watchdog()
            if self.sdr2_source and self.sdr2_source.enabled:
                self.sdr2_source.check_watchdog()

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

            time.sleep(0.1)
          except BaseException as _status_err:
            # Log crash so it's visible in the trace, then keep running.
            try:
                self._trace_events.append((time.monotonic(), 'STATUS_CRASH', str(_status_err)))
            except Exception:
                pass  # trace deque itself failed — don't let that kill us
            time.sleep(1)

    def _print_banner(self):
        """Print the Gateway Active banner, status info, and keyboard controls."""
        mumble_ok = getattr(self, '_mumble_ok', True)
        print()
        print("=" * 60)
        if self.secondary_mode:
            print("Gateway Active! (SECONDARY / STANDBY MODE)")
            print("  Mumble: DISABLED — username already connected on primary")
            print("  DarkIce: DISABLED — Broadcastify feed already live on primary")
            print("  Radio RX/TX and SDR sources still active locally.")
        elif not mumble_ok:
            print("Gateway Active! (MUMBLE OFFLINE)")
            print("  Mumble: DISABLED — server unreachable")
            print("  Radio RX/TX and SDR sources still active locally.")
        else:
            print("Gateway Active!")
            print("  Mumble → AIOC output → Radio TX (auto PTT)")
            print("  Radio RX → AIOC input → Mumble (VOX)")

        # Show audio processing status
        processing_enabled = []
        if self.config.ENABLE_HIGHPASS_FILTER:
            processing_enabled.append(f"HPF@{self.config.HIGHPASS_CUTOFF_FREQ}Hz")
        if self.config.ENABLE_NOISE_SUPPRESSION:
            processing_enabled.append(f"NS({self.config.NOISE_SUPPRESSION_METHOD})")
        if self.config.ENABLE_NOISE_GATE:
            processing_enabled.append(f"Gate@{self.config.NOISE_GATE_THRESHOLD}dB")

        if processing_enabled:
            print(f"  Audio Processing: {', '.join(processing_enabled)}")

        # Show VAD status
        if self.config.ENABLE_VAD:
            print(f"  Voice Activity Detection: ON (threshold: {self.config.VAD_THRESHOLD}dB)")
            print(f"    → Only sends audio to Mumble when radio signal detected")
        else:
            print(f"  Voice Activity Detection: OFF (continuous transmission)")

        # Show stream health management
        if self.config.ENABLE_STREAM_HEALTH and self.config.STREAM_RESTART_INTERVAL > 0:
            print(f"  Stream Health: Auto-restart every {self.config.STREAM_RESTART_INTERVAL}s (when idle {self.config.STREAM_RESTART_IDLE_TIME}s+)")
        else:
            print(f"  Stream Health: DISABLED (may experience -9999 errors if streams get stuck)")

        # Show SDR watchdog status
        if self.sdr_source and self.sdr_source.enabled:
            wt = self.config.SDR_WATCHDOG_TIMEOUT
            wm = self.config.SDR_WATCHDOG_MAX_RESTARTS
            mp = self.config.SDR_WATCHDOG_MODPROBE
            print(f"  SDR1 Watchdog: {wt}s timeout, {wm} max restarts, modprobe={'ON' if mp else 'OFF'}")
        if self.sdr2_source and self.sdr2_source.enabled:
            wt = self.config.SDR2_WATCHDOG_TIMEOUT
            wm = self.config.SDR2_WATCHDOG_MAX_RESTARTS
            mp = self.config.SDR2_WATCHDOG_MODPROBE
            print(f"  SDR2 Watchdog: {wt}s timeout, {wm} max restarts, modprobe={'ON' if mp else 'OFF'}")

        # Show Mumble Server status
        for _ms, _ms_num in [(getattr(self, 'mumble_server_1', None), 1),
                              (getattr(self, 'mumble_server_2', None), 2)]:
            if _ms and _ms.is_enabled():
                state, port = _ms.get_status()
                if state == MumbleServerManager.STATE_RUNNING:
                    print(f"  Mumble Server {_ms_num}: RUNNING on port {port}")
                elif state == MumbleServerManager.STATE_ERROR:
                    print(f"  Mumble Server {_ms_num}: ERROR — {_ms.error_msg}")
                elif state == MumbleServerManager.STATE_CONFIGURED:
                    print(f"  Mumble Server {_ms_num}: configured (port {port})")
                else:
                    print(f"  Mumble Server {_ms_num}: {state}")

        # Print file mapping if playback is enabled
        if self.config.ENABLE_PLAYBACK and hasattr(self, 'playback_source') and self.playback_source:
            print()  # Blank line
            self.playback_source.print_file_mapping()
            print()  # Blank line before keyboard controls

        print("Press Ctrl+C to exit")
        print("Keyboard Controls:")
        print("  Mute:  't'=TX  'r'=RX  'm'=Global  's'=SDR1  'x'=SDR2  'c'=Remote  'a'=Announce  'o'=Speaker")
        print("  Audio: 'v'=VAD toggle  ','=Vol-  '.'=Vol+")
        print("  Proc:  'n'=Gate  'f'=HPF  'g'=AGC  'y'=Spectral  'w'=Wiener  'e'=Echo")
        print("  SDR:   'd'=SDR1 Duck toggle  'b'=SDR Rebroadcast toggle")
        print("  PTT:   'p'=Manual PTT toggle")
        print("  Play:  '1-9'=Announcements  '0'=StationID  '-'=Stop")
        print("  Net:   'k'=Reset remote audio connection")
        print("  Relay: 'j'=Radio power button")
        print("  Trace: 'i'=Start/stop audio trace  'u'=Start/stop watchdog trace")
        print("  Misc:  'q'=Restart gateway  'z'=Clear and reprint console")
        print("=" * 60)
        print()

        # Print status line legend (only in verbose mode)
        if self.config.VERBOSE_LOGGING:
            print("Status Line Legend:")
            print("  [✓/⚠/✗]  = Audio capture status (active/idle/stopped)")
            print("  M:✓/✗    = Mumble connected/disconnected")
            print("  PTT:ON/M-ON/B-ON/-- = Push-to-talk (auto/manual-on/rebroadcast/off)")
            print("  VAD:✗/🔊/-- = VAD disabled/active/silent (dB = current level)")
            print("  TX:[bar] = Mumble → Radio audio level")
            print("  RX:[bar] = Radio → Mumble audio level")
            print("  SDR1:[bar] = SDR1 receiver audio level (cyan)")
            print("  SDR2:[bar] = SDR2 receiver audio level (magenta)")
            print("  Vol:X.Xx = RX volume multiplier (Radio → Mumble gain)")
            print("  1234567890 = File status (green=loaded, red=playing, white=empty)")
            print("  [N,F,G,W,E,D] = Processing: N=NoiseGate F=HPF G=AGC W=Wiener E=Echo D=SDR1Duck")
            print("  R:n      = Stream restart count (only if >0)")
            print()

    def run(self):
        """Main application"""
        print("=" * 60)
        print("Mumble-to-Radio Gateway via AIOC")
        print(f"Version {__version__}")
        print("=" * 60)
        print()
        
        # Initialize AIOC (optional - gateway can work without it)
        self.aioc_available = self.setup_aioc()
        if not self.aioc_available:
            print("⚠ AIOC not found - continuing without radio interface")
            print("  Gateway will operate in Mumble + SDR mode")
        
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

        self._print_banner()
        
        # Install stdout wrapper so print() clears the status bar first
        self._status_writer = StatusBarWriter(sys.stdout)
        sys.stdout = self._status_writer
        # Redirect Python stderr through the same wrapper so warnings from
        # libraries clear the status bar before printing.
        self._orig_stderr = sys.stderr
        sys.stderr = self._status_writer
        # Redirect OS-level fd 2 (C stderr) through a pipe that feeds back
        # into the StatusBarWriter.  This catches output from external
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

        # Start keyboard listener thread
        self._keyboard_thread = threading.Thread(target=self.keyboard_listener_loop, daemon=True)
        self._keyboard_thread.start()
        
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
            th_sdr1 = _alive(self.sdr_source._reader_thread if self.sdr_source and hasattr(self.sdr_source, '_reader_thread') else None)
            th_sdr2 = _alive(self.sdr2_source._reader_thread if self.sdr2_source and hasattr(self.sdr2_source, '_reader_thread') else None)
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
            en_sdr1 = 1 if (self.sdr_source and self.sdr_source.enabled) else 0
            en_sdr2 = 1 if (self.sdr2_source and self.sdr2_source.enabled) else 0
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
            q_sdr1 = _qsize(self.sdr_source)
            q_sdr2 = _qsize(self.sdr2_source)

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
            OUT_DISC = range(34)

        with open(out_path, 'w') as f:
            dur = trace[-1][T] - trace[0][T] if len(trace) > 1 else 0
            f.write(f"Audio Trace: {len(trace)} ticks, {dur:.1f}s\n")
            f.write(f"{'='*90}\n\n")

            # ── System info ──
            import platform
            sdr_mode = "PipeWire" if any(
                isinstance(s, PipeWireSDRSource)
                for s in [self.sdr_source, self.sdr2_source] if s) else "ALSA"
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
                plc_total = self.sdr_source._plc_total if self.sdr_source else 0
                f.write(f"  PLC repeats: {plc_total} (gap concealment)\n\n")

            # SDR2 queue depth
            sq2_vals = [r[SQ2] for r in trace if len(r) > SQ2 and r[SQ2] >= 0]
            if sq2_vals:
                f.write(f"SDR2 QUEUE DEPTH\n")
                f.write(f"  min={min(sq2_vals)}  mean={statistics.mean(sq2_vals):.1f}  max={max(sq2_vals)}\n")
                pb2_ticks = sum(1 for r in trace if len(r) > S2PREBUF and r[S2PREBUF])
                f.write(f"  prebuffering: {pb2_ticks}/{len(trace)} ticks\n")
                plc2_total = self.sdr2_source._plc_total if self.sdr2_source else 0
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
            #   D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active
            #   Per SDR: D=ducked S=signal H=hold X=sole .=excluded I=included(no signal)
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
                sdrs = st.get('sdrs', {})
                for sname in sorted(sdrs.keys()):
                    s = sdrs[sname]
                    flags += ' '
                    if s.get('ducked'):
                        flags += 'D'
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
            for src_name, src_obj in [('SDR1', self.sdr_source), ('SDR2', self.sdr2_source)]:
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
            f.write("  State: D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active\n")
            f.write("  SDR:   D=ducked S=signal H=hold_inc X=sole_src I=inc(other) .=excluded\n")
            f.write("  PB: B=prebuffering (waiting to rebuild cushion) .=normal\n")
            f.write("  RB: sig=rebroadcast sending  hold=PTT hold  idle=on but no signal\n")
            f.write("  s1_disc/a_disc/o_disc: sample discontinuity at chunk boundary (abs delta, >5000=click)\n")
            f.write("  s1_sba/a_sba: sub-buffer bytes remaining AFTER serving this chunk\n\n")
            hdr = (f"{'tick':>6} {'t(s)':>7} {'dt':>6} "
                   f"{'s1_q':>4} {'s1_sb':>6} {'s1_sba':>6} {'s2_q':>4} {'s2_sb':>6} {'pb':>2} "
                   f"{'aioc_q':>6} {'aioc_sb':>7} {'a_sba':>6} {'mixer':>5} {'mix_ms':>6} "
                   f"{'outcome':>10} {'m_ms':>5} {'spk_q':>5} {'rms':>7} {'dlen':>5} "
                   f"{'sv_ms':>6} {'sv#':>3} "
                   f"{'s1_disc':>7} {'a_disc':>7} {'o_disc':>7} "
                   f"{'sources':>14} {'state':>14} {'rb':>4}\n")
            f.write(hdr)
            f.write('-' * len(hdr) + '\n')
            for i, r in enumerate(trace):
                expected_len = self.config.AUDIO_CHUNK_SIZE * 2
                _has_enh = len(r) > SDR1_DISC
                is_anomaly = (r[DT] > 80 or not r[MGOT] or r[MMS] > 20
                              or r[OUTCOME] not in ('sent', 'mix')
                              or r[MUMMS] > 5 or r[SPKQD] >= 7 or r[DRMS] == 0
                              or (r[DLEN] > 0 and r[DLEN] != expected_len)
                              or (len(r) > SPREBUF and (r[SPREBUF] or r[S2PREBUF]))
                              or (len(r) > SVMS and r[SVMS] > 5.0)
                              or (_has_enh and r[SDR1_DISC] > 5000)
                              or (_has_enh and r[AIOC_DISC] > 5000)
                              or (len(r) > OUT_DISC and r[OUT_DISC] > 5000))
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
                f.write(f"{i:>5}{flag} {r[T]:7.3f} {r[DT]:6.1f} "
                        f"{r[SQ]:4} {r[SSB]:6} {s1_sba:6} {sq2:4} {ssb2:6} {pb1}{pb2} "
                        f"{r[AQ]:6} {r[ASB]:7} {a_sba:6} {'audio' if r[MGOT] else 'NONE':>5} "
                        f"{r[MMS]:6.1f} "
                        f"{r[OUTCOME]:>10} {r[MUMMS]:5.1f} {r[SPKQD]:5} {r[DRMS]:7.0f} "
                        f"{r[DLEN]:5} {sv_ms:6.1f} {sv_n:3} "
                        f"{s1_disc:7.0f} {a_disc:7.0f} {o_disc:7.0f} "
                        f"{r[MSRC]:>14} {st} {rb:>4}\n")

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
        if self.sdr_source:
            try:
                self.sdr_source.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  SDR1 audio closed")
            except Exception as e:
                pass  # Suppress ALSA errors during shutdown
        
        if self.sdr2_source:
            try:
                self.sdr2_source.cleanup()
                if self.config.VERBOSE_LOGGING:
                    print("  SDR2 audio closed")
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

        if self.cat_client:
            try:
                self.cat_client.close()
                if self.config.VERBOSE_LOGGING:
                    print("  CAT client closed")
            except Exception:
                pass

        # Stop local Mumble Server instances (leave them running — they are services)
        # We don't stop them on gateway exit so users stay connected between restarts.
        # The services are managed by systemd independently.

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

_MORSE_TABLE = {
    'A': '.-',   'B': '-...', 'C': '-.-.', 'D': '-..',  'E': '.',
    'F': '..-.', 'G': '--.',  'H': '....', 'I': '..',   'J': '.---',
    'K': '-.-',  'L': '.-..', 'M': '--',   'N': '-.',   'O': '---',
    'P': '.--.', 'Q': '--.-', 'R': '.-.',  'S': '...',  'T': '-',
    'U': '..-',  'V': '...-', 'W': '.--',  'X': '-..-', 'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', '/': '-..-.', '-': '-....-',
}

def generate_cw_pcm(text, wpm=15, freq=700, sample_rate=48000):
    """Return int16 numpy array of CW audio for text. Standard PARIS timing."""
    dit_n = int(sample_rate * 1.2 / wpm)
    t = np.arange(dit_n) / sample_rate
    dit_tone = (np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    dah_tone = np.tile(dit_tone, 3)
    dit_sil  = np.zeros(dit_n,     dtype=np.int16)
    char_sil = np.zeros(3 * dit_n, dtype=np.int16)
    word_sil = np.zeros(7 * dit_n, dtype=np.int16)

    chunks = []
    for wi, word in enumerate(text.upper().split()):
        if wi:
            chunks.append(word_sil)
        for ci, ch in enumerate(word):
            if ci:
                chunks.append(char_sil)
            for ei, el in enumerate(_MORSE_TABLE.get(ch, '')):
                if ei:
                    chunks.append(dit_sil)
                chunks.append(dit_tone if el == '.' else dah_tone)

    return np.concatenate(chunks) if chunks else np.zeros(dit_n, dtype=np.int16)


def main():
    # Find config file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(script_dir, "gateway_config.txt")
    
    # Load configuration
    config = Config(config_file)
    
    # Create and run gateway
    gateway = MumbleRadioGateway(config)
    
    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        gateway.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    gateway.run()

    if gateway.restart_requested:
        print("\nRestarting gateway...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

if __name__ == "__main__":
    main()
