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
import subprocess
import shutil
import json as json_mod
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
            'MUMBLE_BITRATE': 72000,
            'MUMBLE_VBR': True,
            'MUMBLE_JITTER_BUFFER': 10,
            'PTT_METHOD': 'aioc',              # 'aioc', 'relay', or 'software'
            'PTT_RELAY_DEVICE': '/dev/relay_ptt',
            'PTT_RELAY_BAUD': 9600,
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
            'ENABLE_LOWPASS_FILTER': False,
            'LOWPASS_CUTOFF_FREQ': 3000,
            'ENABLE_NOTCH_FILTER': False,
            'NOTCH_FREQ': 1000,
            'NOTCH_Q': 30.0,
            'ENABLE_DEESSER': False,
            'DEESSER_FREQ': 5000,
            'DEESSER_STRENGTH': 0.6,
            # Per-source processing overrides (SDR).
            # When set, SDR sources use these instead of the global settings.
            # When not set (empty string / default), SDR processing is disabled
            # to preserve backwards compatibility.
            'SDR_PROC_ENABLE_NOISE_GATE': False,
            'SDR_PROC_NOISE_GATE_THRESHOLD': -40,
            'SDR_PROC_NOISE_GATE_ATTACK': 0.01,
            'SDR_PROC_NOISE_GATE_RELEASE': 0.1,
            'SDR_PROC_ENABLE_HPF': True,
            'SDR_PROC_HPF_CUTOFF': 300,
            'SDR_PROC_ENABLE_LPF': False,
            'SDR_PROC_LPF_CUTOFF': 3000,
            'SDR_PROC_ENABLE_NOTCH': False,
            'SDR_PROC_NOTCH_FREQ': 1000,
            'SDR_PROC_NOTCH_Q': 30.0,
            'SDR_PROC_ENABLE_NS': False,
            'SDR_PROC_NS_METHOD': 'spectral',
            'SDR_PROC_NS_STRENGTH': 0.5,
            'SDR_PROC_ENABLE_DEESSER': False,
            'SDR_PROC_DEESSER_FREQ': 5000,
            'SDR_PROC_DEESSER_STRENGTH': 0.6,
            'INPUT_VOLUME': 1.0,
            'OUTPUT_VOLUME': 1.0,
            'MUMBLE_LOOP_RATE': 0.01,
            'MUMBLE_STEREO': False,
            'MUMBLE_RECONNECT': True,
            'MUMBLE_DEBUG': False,
            'NETWORK_TIMEOUT': 10,
            'TCP_NODELAY': True,
            'HEADLESS_MODE': False,         # No console status bar, log to file + web UI
            'LOG_BUFFER_LINES': 2000,      # Lines kept in memory for web /logs viewer
            'LOG_FILE_DAYS': 7,            # Days to keep rolling log files
            'VERBOSE_LOGGING': False,
            'STATUS_UPDATE_INTERVAL': 1.0,  # seconds
            'MAX_MUMBLE_BUFFER_SECONDS': 1.0,
            'BUFFER_MANAGEMENT_VERBOSE': False,
            'ENABLE_VAD': True,
            'VAD_THRESHOLD': -45,
            'VAD_ATTACK': 0.02,  # float (seconds)
            'VAD_RELEASE': 1.0,  # float (seconds)
            'VAD_MIN_DURATION': 0.1,  # float (seconds)
            'ENABLE_STREAM_HEALTH': False,
            'STREAM_RESTART_INTERVAL': 60,
            'STREAM_RESTART_IDLE_TIME': 3,
            'ENABLE_VOX': False,
            'VOX_THRESHOLD': -30,
            'VOX_ATTACK_TIME': 0.05,  # float (seconds)
            'VOX_RELEASE_TIME': 0.5,  # float (seconds)
            # File Playback
            'ENABLE_PLAYBACK': True,
            'PLAYBACK_DIRECTORY': './audio/',
            'PLAYBACK_ANNOUNCEMENT_FILE': '',
            'PLAYBACK_ANNOUNCEMENT_INTERVAL': 0,  # seconds, 0 = disabled
            'PLAYBACK_VOLUME': 1.0,               # float (multiplier; >1.0 boosts, audio is clipped to int16 range)
            # Morse Code (CW)
            'CW_WPM': 20,          # Morse code words per minute
            'CW_FREQUENCY': 600,   # Tone frequency in Hz
            'CW_VOLUME': 1.0,      # Volume multiplier (applied before WAV write; PLAYBACK_VOLUME also applies)
            # Text-to-Speech and Text Commands (Phase 4)
            'ENABLE_TTS': True,
            'ENABLE_TEXT_COMMANDS': True,
            'TTS_VOLUME': 1.0,  # Volume multiplier for TTS audio (1.0 = normal, 2.0 = double, 3.0 = triple)
            'TTS_SPEED': 1.3,   # Speech speed (1.0 = normal, 1.3 = 30% faster, 0.8 = slower, requires ffmpeg)
            'TTS_DEFAULT_VOICE': 1, # Default voice (1=US, 2=British, 3=Australian, 4=Indian, 5=SA, 6=Canadian, 7=Irish, 8=French, 9=German)
            'PTT_TTS_DELAY': 0.5,   # Silence padding before TTS (seconds) to prevent cutoff
            'PTT_ANNOUNCEMENT_DELAY': 0.5,  # Seconds after PTT key-up before announcement audio starts
            # SDR Integration
            'ENABLE_SDR': True,
            'SDR_DEVICE_NAME': 'pw:sdr_capture',  # PipeWire sink (recommended) or ALSA device (e.g., 'hw:6,1')
            'SDR_DUCK': True,             # Duck SDR: silence SDR when higher priority source is active
            'SDR_MIX_RATIO': 1.0,        # Volume/mix ratio when ducking is disabled (1.0 = full volume)
            'SDR_DISPLAY_GAIN': 1.0,     # Display sensitivity multiplier (1.0 = normal, higher = more sensitive bar)
            'SDR_AUDIO_BOOST': 1.0,      # Actual audio volume boost (1.0 = no change, 2.0 = 2x louder)
            'SDR_BUFFER_MULTIPLIER': 4,  # Buffer size multiplier (4 = 4x normal buffer, ~200ms per ALSA read)
            'SDR_PRIORITY': 1,           # SDR priority for ducking (1 = higher priority, 2 = lower priority)
            'SDR_WATCHDOG_TIMEOUT': 10,        # seconds with no successful read before recovery
            'SDR_WATCHDOG_MAX_RESTARTS': 5,    # max recovery attempts before giving up
            'SDR_WATCHDOG_MODPROBE': False,    # enable kernel module reload (requires sudoers entry)
            # SDR2 Integration (second SDR receiver)
            'ENABLE_SDR2': False,
            'SDR2_DEVICE_NAME': 'pw:sdr_capture2',
            'SDR2_DUCK': True,
            'SDR2_MIX_RATIO': 1.0,
            'SDR2_DISPLAY_GAIN': 1.0,
            'SDR2_AUDIO_BOOST': 1.5,
            'SDR2_BUFFER_MULTIPLIER': 4,
            'SDR2_PRIORITY': 2,          # SDR2 priority for ducking (1 = higher, 2 = lower)
            'SDR2_WATCHDOG_TIMEOUT': 10,
            'SDR2_WATCHDOG_MAX_RESTARTS': 5,
            'SDR2_WATCHDOG_MODPROBE': False,
            # Signal Detection Hysteresis (prevents stuttering from rapid on/off)
            'SIGNAL_ATTACK_TIME': 0.25,  # Seconds of CONTINUOUS signal required before a source switch is allowed
            'SIGNAL_RELEASE_TIME': 3.0,  # Seconds of continuous silence required before switching back
            'SWITCH_PADDING_TIME': 1.0,  # Seconds of silence inserted at each transition (duck-out and duck-in)
            'SDR_DUCK_COOLDOWN': 3.0,   # After lower-priority SDR unducks, seconds before higher-priority SDR can re-duck it
            'SDR_SIGNAL_THRESHOLD': -70.0,  # dBFS threshold for SDR signal detection (inclusion + ducking); lower = more sensitive
            'SDR_REBROADCAST_PTT_HOLD': 3.0,  # Seconds to hold PTT after SDR audio stops during rebroadcast
            # EchoLink Integration (Phase 3B)
            'ENABLE_ECHOLINK': False,
            'ECHOLINK_RX_PIPE': '/tmp/echolink_rx',
            'ECHOLINK_TX_PIPE': '/tmp/echolink_tx',
            'ECHOLINK_TO_MUMBLE': True,
            'ECHOLINK_TO_RADIO': True,
            'RADIO_TO_ECHOLINK': True,
            'MUMBLE_TO_ECHOLINK': True,
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
            'REMOTE_AUDIO_PRIORITY': 0,            # sdr_priority for ducking (0 = ducks all local SDRs)
            'REMOTE_AUDIO_DISPLAY_GAIN': 1.0,
            'REMOTE_AUDIO_AUDIO_BOOST': 1.0,
            'REMOTE_AUDIO_RECONNECT_INTERVAL': 5.0,
            # Announcement Input (port 9601 — inbound PCM stream, PTT to radio)
            'ENABLE_ANNOUNCE_INPUT': True,
            'ANNOUNCE_INPUT_PORT': 9601,
            'ANNOUNCE_INPUT_HOST': '',
            'ANNOUNCE_INPUT_THRESHOLD': -45.0,  # dBFS — below this is treated as silence
            'ANNOUNCE_INPUT_VOLUME': 4.0,       # volume multiplier for announcement audio
            # Web Microphone PTT (browser mic → radio TX via WebSocket)
            'ENABLE_WEB_MIC': True,
            'WEB_MIC_VOLUME': 4.0,              # volume multiplier for browser mic audio
            # Soundboard — auto-fill empty playback slots with random sound effects
            'ENABLE_SOUNDBOARD': True,
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
            # Smart Announcements (AI-powered)
            'ENABLE_SMART_ANNOUNCE': True,
            'SMART_ANNOUNCE_AI_BACKEND': 'google-scrape',  # google-scrape, duckduckgo, claude, or gemini
            'SMART_ANNOUNCE_OLLAMA_MODEL': 'llama3.2:1b',  # Ollama model (blank = auto-detect)
            'SMART_ANNOUNCE_OLLAMA_TEMPERATURE': 0.5,  # 0.0=focused, 1.0=creative
            'SMART_ANNOUNCE_OLLAMA_TOP_P': 0.5,        # nucleus sampling (0.0-1.0)
            'SMART_ANNOUNCE_OLLAMA_NUM_CTX': 1024,     # context window (lower = less RAM/CPU)
            'SMART_ANNOUNCE_OLLAMA_NUM_THREAD': 2,     # CPU threads (0 = all cores)
            'SMART_ANNOUNCE_API_KEY': '',            # Claude API key
            'SMART_ANNOUNCE_GEMINI_API_KEY': '',     # Gemini API key
            'SMART_ANNOUNCE_TOP_TEXT': '',           # Text spoken before announcement (empty = none)
            'SMART_ANNOUNCE_TAIL_TEXT': '',          # Text spoken after announcement (empty = none)
            'SMART_ANNOUNCE_START_TIME': '08:00',   # HH:MM — empty = no restriction
            'SMART_ANNOUNCE_END_TIME': '22:00',     # HH:MM — empty = no restriction
            # TH-9800 CAT Control
            'ENABLE_CAT_CONTROL': False,
            'CAT_STARTUP_COMMANDS': True,
            'CAT_HOST': '127.0.0.1',
            'CAT_PORT': 9800,
            'CAT_PASSWORD': '',
            'CAT_LEFT_CHANNEL': -1,     # -1 = don't change
            'CAT_RIGHT_CHANNEL': -1,    # -1 = don't change
            'CAT_LEFT_VOLUME': -1,      # 0-100, -1 = don't change
            'CAT_RIGHT_VOLUME': -1,     # 0-100, -1 = don't change
            'CAT_LEFT_POWER': '',       # L/M/H or blank = don't change
            'CAT_RIGHT_POWER': '',      # L/M/H or blank = don't change
            # Dynamic DNS (No-IP compatible)
            # Web Configuration UI
            'ENABLE_WEB_CONFIG': False,
            'WEB_CONFIG_PORT': 8080,
            'WEB_CONFIG_PASSWORD': '',    # Basic auth password (user: admin), blank = no auth
            'WEB_CONFIG_HTTPS': False,    # false, self-signed, or letsencrypt
            # Cloudflare Tunnel (free public HTTPS access, no port forwarding needed)
            'SDR_INTERNAL_AUTOSTART': True,    # Auto-start internal SDR (rtl_airband) on gateway startup
            'SDR_INTERNAL_AUTOSTART_CHANNEL': 1,   # Channel slot to recall on autostart (-1 = use last settings)
            'ENABLE_CLOUDFLARE_TUNNEL': False,
            # Email notifications (Gmail SMTP)
            'ENABLE_EMAIL': False,
            'EMAIL_ADDRESS': '',          # Gmail address (sender)
            'EMAIL_APP_PASSWORD': '',     # Gmail app password (not regular password)
            'EMAIL_RECIPIENT': '',        # Where to send notifications (blank = same as EMAIL_ADDRESS)
            'EMAIL_ON_STARTUP': True,     # Send status email on startup
            # Dynamic DNS (No-IP compatible)
            'ENABLE_DDNS': False,
            'DDNS_USERNAME': '',
            'DDNS_PASSWORD': '',
            'DDNS_HOSTNAME': '',
            'DDNS_UPDATE_INTERVAL': 300,   # seconds between updates (default 5 min)
            'DDNS_UPDATE_URL': 'https://dynupdate.no-ip.com/nic/update',  # No-IP protocol
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
            'MUMBLE_SERVER_2_AUTOSTART': False,
        }
        
        # Store defaults for type inference (used by WebConfigServer)
        self._defaults = dict(defaults)

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
                    # Skip comments, empty lines, and INI section headers
                    if not line or line.startswith('#') or line.startswith('['):
                        continue
                    
                    # Parse key = value
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()

                        # Strip surrounding quotes from string values
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                            value = value[1:-1]

                        # Strip inline comments (everything after #),
                        # but preserve # inside {braces} (used by smart announce prompts)
                        if '#' in value:
                            brace_start = value.find('{')
                            if brace_start != -1 and value.rfind('}') > brace_start:
                                # Has braces — only strip comments outside the braces
                                before_brace = value[:brace_start]
                                if '#' in before_brace:
                                    value = before_brace.split('#')[0].strip()
                                # else: # is inside braces, keep it
                            else:
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


class AudioProcessor:
    """Per-source audio processing chain with independent filter state.

    Each audio source (Radio, SDR1, SDR2, etc.) gets its own AudioProcessor
    instance so filters run independently with their own state (envelope,
    filter memory, etc.) and can be toggled per-source.
    """

    def __init__(self, name, config):
        self.name = name          # e.g. "radio", "sdr"
        self.config = config      # gateway Config object (for AUDIO_RATE, etc.)

        # Per-source enable flags (set from config or toggled at runtime)
        self.enable_hpf = False
        self.hpf_cutoff = 300         # Hz
        self.enable_lpf = False
        self.lpf_cutoff = 3000        # Hz
        self.enable_notch = False
        self.notch_freq = 1000        # Hz — target frequency
        self.notch_q = 30.0           # Q factor (higher = narrower notch)
        self.enable_noise_gate = False
        self.gate_threshold = -40     # dB
        self.gate_attack = 0.01       # seconds
        self.gate_release = 0.1       # seconds
        self.enable_noise_suppression = False
        self.noise_suppression_method = 'spectral'
        self.noise_suppression_strength = 0.5
        self.enable_deesser = False
        self.deesser_freq = 5000      # Hz — sibilance target
        self.deesser_strength = 0.6   # reduction amount

        # Filter state (persists across audio chunks for continuity)
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        self.noise_profile = None
        self.deesser_state = None

    def reset_state(self):
        """Reset all filter states (e.g. when source restarts)."""
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0
        self.noise_profile = None
        self.deesser_state = None

    def process(self, pcm_data):
        """Run the full processing chain on PCM data. Order:
        HPF → LPF → Notch → De-esser → Spectral NS → Noise Gate
        """
        if not pcm_data:
            return pcm_data

        processed = pcm_data

        if self.enable_hpf:
            processed = self._apply_hpf(processed)

        if self.enable_lpf:
            processed = self._apply_lpf(processed)

        if self.enable_notch:
            processed = self._apply_notch(processed)

        if self.enable_deesser:
            processed = self._apply_deesser(processed)

        if self.enable_noise_suppression:
            if self.noise_suppression_method == 'spectral':
                processed = self._apply_spectral_ns(processed)

        if self.enable_noise_gate:
            processed = self._apply_noise_gate(processed)

        return processed

    def get_active_list(self):
        """Return list of active filter names for status display."""
        active = []
        if self.enable_noise_gate: active.append('Gate')
        if self.enable_hpf: active.append('HPF')
        if self.enable_lpf: active.append('LPF')
        if self.enable_notch: active.append(f'Notch')
        if self.enable_deesser: active.append('DeEss')
        if self.enable_noise_suppression: active.append(self.noise_suppression_method.title())
        return active

    # --- Filter implementations ---

    def _apply_hpf(self, pcm_data):
        """First-order IIR high-pass filter."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.hpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b = np.array([alpha, -alpha], dtype=np.float64)
            a = np.array([1.0, -alpha], dtype=np.float64)

            if self.highpass_state is None:
                self.highpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.highpass_state = lfilter(b, a, samples, zi=self.highpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_lpf(self, pcm_data):
        """First-order IIR low-pass filter — cuts high-frequency hiss above cutoff."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.lpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = dt / (rc + dt)

            b = np.array([alpha], dtype=np.float64)
            a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)

            if self.lowpass_state is None:
                self.lowpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.lowpass_state = lfilter(b, a, samples, zi=self.lowpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_notch(self, pcm_data):
        """Second-order IIR notch (band-stop) filter — removes a specific frequency."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            sample_rate = self.config.AUDIO_RATE
            w0 = 2.0 * math.pi * self.notch_freq / sample_rate
            bw = w0 / self.notch_q
            r = 1.0 - (bw / 2.0)
            r = max(0.0, min(r, 0.9999))  # clamp for stability

            # Transfer function: H(z) = (1 - 2cos(w0)z^-1 + z^-2) / (1 - 2r*cos(w0)z^-1 + r^2*z^-2)
            cos_w0 = math.cos(w0)
            b = np.array([1.0, -2.0 * cos_w0, 1.0], dtype=np.float64)
            a = np.array([1.0, -2.0 * r * cos_w0, r * r], dtype=np.float64)
            # Normalize so passband gain = 1
            b = b / (1.0 + abs(1.0 - r))

            if self.notch_state is None:
                self.notch_state = lfilter_zi(b, a) * 0.0

            filtered, self.notch_state = lfilter(b, a, samples, zi=self.notch_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_deesser(self, pcm_data):
        """Simple de-esser — attenuates sibilance around the target frequency.
        Works by detecting energy in the sibilance band and applying gain reduction.
        """
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            sample_rate = self.config.AUDIO_RATE
            # HPF to isolate sibilance band
            cutoff = self.deesser_freq
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b_hpf = np.array([alpha, -alpha], dtype=np.float64)
            a_hpf = np.array([1.0, -alpha], dtype=np.float64)

            if self.deesser_state is None:
                self.deesser_state = lfilter_zi(b_hpf, a_hpf) * 0.0

            sibilance, self.deesser_state = lfilter(b_hpf, a_hpf, samples, zi=self.deesser_state)

            # Calculate per-sample gain reduction based on sibilance energy
            # Use a simple envelope follower on the sibilance band
            strength = self.deesser_strength
            sib_abs = np.abs(sibilance)
            sig_abs = np.maximum(np.abs(samples), 1.0)
            ratio = sib_abs / sig_abs
            # Where sibilance dominates, reduce gain
            gain = np.where(ratio > 0.3, 1.0 - strength * np.minimum(ratio, 1.0), 1.0)
            processed = samples * gain

            return np.clip(processed, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_spectral_ns(self, pcm_data):
        """Spectral subtraction noise suppression."""
        try:
            from scipy.ndimage import uniform_filter1d

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            window_size = 32
            strength = self.noise_suppression_strength

            noise_estimate = uniform_filter1d(np.abs(samples), size=window_size, mode='nearest')

            above_threshold = np.abs(samples) > noise_estimate * (1.0 + strength)
            reduction = strength * noise_estimate
            reduced = np.where(samples > 0,
                               np.maximum(0.0, samples - reduction),
                               np.minimum(0.0, samples + reduction))

            processed = np.where(above_threshold, samples, reduced)
            return np.clip(processed, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_noise_gate(self, pcm_data):
        """Noise gate with attack/release envelope."""
        try:
            import array as _arr
            import math

            samples = _arr.array('h', pcm_data)
            if len(samples) == 0:
                return pcm_data

            threshold_db = self.gate_threshold
            threshold = 32767.0 * pow(10.0, threshold_db / 20.0)

            attack_samples = self.gate_attack * self.config.AUDIO_RATE
            release_samples = self.gate_release * self.config.AUDIO_RATE

            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1

            gated = []
            for sample in samples:
                level = abs(sample)

                if level > self.gate_envelope:
                    self.gate_envelope += (level - self.gate_envelope) * attack_coef
                else:
                    self.gate_envelope += (level - self.gate_envelope) * release_coef

                if self.gate_envelope > threshold:
                    gain = 1.0
                else:
                    ratio = self.gate_envelope / threshold if threshold > 0 else 0
                    gain = ratio * ratio

                gated.append(int(sample * gain))

            return _arr.array('h', gated).tobytes()
        except Exception:
            return pcm_data


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
        # WARNING: Do NOT reduce blob_mult below 4 or pre-buffer below 3.
        # AIOC USB audio has significant jitter; smaller values cause clicks,
        # robot sounds, and volume discontinuities. Tested 2× and 1× — both
        # produced artifacts. These values are the proven minimum for clean audio.
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._blob_mult = 4  # ALSA period = 4×AUDIO_CHUNK_SIZE — DO NOT REDUCE
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
            # (600ms cushion) before serving.  Absorbs USB delivery jitter.
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
        
        # Step 3: Fill empty slots with random online sound effects
        if getattr(self.config, 'ENABLE_SOUNDBOARD', True):
            self._fill_soundboard_slots(file_map)

        # Step 4: Update file_status with found files
        for key in '0123456789':
            if key in file_map:
                filepath, filename = file_map[key]
                self.file_status[key]['exists'] = True
                self.file_status[key]['path'] = filepath
                self.file_status[key]['filename'] = filename

        # Step 5: Print file mapping (will be displayed before status bar)
        self.file_mapping_display = self._generate_file_mapping_display(file_map, station_id_found)

    # Curated pool of 429 free sound effects from Mixkit (royalty-free, no attribution)
    # URL pattern: https://assets.mixkit.co/active_storage/sfx/{id}/{id}-preview.mp3
    # Categories: animals, applause, arcade, bells, boing, buzzer, cartoon, crowd,
    #             drums, explosion, funny, game, horns, impact, sirens, transition,
    #             whistles, whoosh
    SOUNDBOARD_POOL = [
        # Animals (50)
        ('animals', 1), ('animals', 6), ('animals', 7), ('animals', 13), ('animals', 17),
        ('animals', 20), ('animals', 23), ('animals', 45), ('animals', 51), ('animals', 54),
        ('animals', 59), ('animals', 60), ('animals', 61), ('animals', 76), ('animals', 83),
        ('animals', 85), ('animals', 87), ('animals', 91), ('animals', 92), ('animals', 93),
        ('animals', 96), ('animals', 105), ('animals', 108), ('animals', 309), ('animals', 1212),
        ('animals', 1744), ('animals', 1751), ('animals', 1770), ('animals', 1775), ('animals', 1776),
        ('animals', 1780), ('animals', 2458), ('animals', 2462), ('animals', 2466), ('animals', 2467),
        ('animals', 2485), ('animals', 2469), ('animals', 2471), ('animals', 2474), ('animals', 2476),
        ('animals', 2479), ('animals', 2481), ('animals', 2483), ('animals', 2486), ('animals', 2488),
        ('animals', 2490), ('animals', 2492), ('animals', 2494), ('animals', 2496), ('animals', 2498),
        # Applause (35)
        ('applause', 103), ('applause', 362), ('applause', 439), ('applause', 442), ('applause', 475),
        ('applause', 476), ('applause', 477), ('applause', 478), ('applause', 482), ('applause', 484),
        ('applause', 485), ('applause', 500), ('applause', 501), ('applause', 502), ('applause', 504),
        ('applause', 505), ('applause', 507), ('applause', 508), ('applause', 509), ('applause', 510),
        ('applause', 512), ('applause', 513), ('applause', 515), ('applause', 516), ('applause', 517),
        ('applause', 518), ('applause', 519), ('applause', 521), ('applause', 522), ('applause', 523),
        ('applause', 3035), ('applause', 3036), ('applause', 3039), ('applause', 480), ('applause', 486),
        # Arcade (45)
        ('arcade', 210), ('arcade', 211), ('arcade', 212), ('arcade', 213), ('arcade', 216),
        ('arcade', 217), ('arcade', 220), ('arcade', 221), ('arcade', 223), ('arcade', 234),
        ('arcade', 235), ('arcade', 236), ('arcade', 237), ('arcade', 240), ('arcade', 253),
        ('arcade', 254), ('arcade', 257), ('arcade', 272), ('arcade', 277), ('arcade', 278),
        ('arcade', 470), ('arcade', 767), ('arcade', 866), ('arcade', 1084), ('arcade', 1698),
        ('arcade', 1699), ('arcade', 1933), ('arcade', 1953), ('arcade', 2027), ('arcade', 2803),
        ('arcade', 2810), ('arcade', 2811), ('arcade', 2852), ('arcade', 2854), ('arcade', 2859),
        ('arcade', 2973), ('arcade', 214), ('arcade', 218), ('arcade', 219), ('arcade', 222),
        ('arcade', 224), ('arcade', 238), ('arcade', 239), ('arcade', 241), ('arcade', 271),
        # Bells (30)
        ('bells', 109), ('bells', 110), ('bells', 111), ('bells', 113), ('bells', 587),
        ('bells', 591), ('bells', 592), ('bells', 595), ('bells', 600), ('bells', 601),
        ('bells', 603), ('bells', 621), ('bells', 765), ('bells', 931), ('bells', 933),
        ('bells', 937), ('bells', 938), ('bells', 939), ('bells', 1046), ('bells', 1569),
        ('bells', 1743), ('bells', 1791), ('bells', 2256), ('bells', 3109), ('bells', 112),
        ('bells', 588), ('bells', 593), ('bells', 596), ('bells', 598), ('bells', 602),
        # Boing (10)
        ('boing', 2895), ('boing', 2896), ('boing', 2897), ('boing', 2898), ('boing', 2899),
        ('boing', 2893), ('boing', 2894), ('boing', 2892), ('boing', 2891), ('boing', 2890),
        # Buzzer (25)
        ('buzzer', 31), ('buzzer', 932), ('buzzer', 941), ('buzzer', 948), ('buzzer', 950),
        ('buzzer', 954), ('buzzer', 955), ('buzzer', 992), ('buzzer', 1647), ('buzzer', 2131),
        ('buzzer', 2132), ('buzzer', 2133), ('buzzer', 2591), ('buzzer', 2961), ('buzzer', 2962),
        ('buzzer', 2963), ('buzzer', 2964), ('buzzer', 2966), ('buzzer', 2967), ('buzzer', 2968),
        ('buzzer', 2969), ('buzzer', 3090), ('buzzer', 940), ('buzzer', 949), ('buzzer', 951),
        # Cartoon (20)
        ('cartoon', 107), ('cartoon', 741), ('cartoon', 2151), ('cartoon', 2195), ('cartoon', 2257),
        ('cartoon', 2363), ('cartoon', 742), ('cartoon', 743), ('cartoon', 745), ('cartoon', 747),
        ('cartoon', 2153), ('cartoon', 2193), ('cartoon', 2196), ('cartoon', 2258), ('cartoon', 2259),
        ('cartoon', 2360), ('cartoon', 2361), ('cartoon', 2362), ('cartoon', 2364), ('cartoon', 2365),
        # Cinematic (20)
        ('cinematic', 2838), ('cinematic', 2839), ('cinematic', 2840), ('cinematic', 2841),
        ('cinematic', 2842), ('cinematic', 2843), ('cinematic', 2844), ('cinematic', 2845),
        ('cinematic', 2846), ('cinematic', 2847), ('cinematic', 2848), ('cinematic', 2849),
        ('cinematic', 2850), ('cinematic', 2851), ('cinematic', 2853), ('cinematic', 2855),
        ('cinematic', 2856), ('cinematic', 2857), ('cinematic', 2858), ('cinematic', 2860),
        # Click (15)
        ('click', 546), ('click', 547), ('click', 548), ('click', 549), ('click', 550),
        ('click', 551), ('click', 552), ('click', 553), ('click', 554), ('click', 555),
        ('click', 556), ('click', 557), ('click', 2568), ('click', 2570), ('click', 2571),
        # Crowd (30)
        ('crowd', 360), ('crowd', 363), ('crowd', 368), ('crowd', 376), ('crowd', 377),
        ('crowd', 423), ('crowd', 424), ('crowd', 429), ('crowd', 432), ('crowd', 444),
        ('crowd', 448), ('crowd', 458), ('crowd', 459), ('crowd', 460), ('crowd', 461),
        ('crowd', 462), ('crowd', 469), ('crowd', 520), ('crowd', 531), ('crowd', 974),
        ('crowd', 1573), ('crowd', 1958), ('crowd', 2111), ('crowd', 3022), ('crowd', 364),
        ('crowd', 370), ('crowd', 378), ('crowd', 425), ('crowd', 433), ('crowd', 449),
        # Drums (30)
        ('drums', 487), ('drums', 488), ('drums', 492), ('drums', 546), ('drums', 558),
        ('drums', 559), ('drums', 560), ('drums', 562), ('drums', 563), ('drums', 565),
        ('drums', 566), ('drums', 567), ('drums', 570), ('drums', 573), ('drums', 576),
        ('drums', 577), ('drums', 2295), ('drums', 2299), ('drums', 2300), ('drums', 2426),
        ('drums', 2569), ('drums', 2909), ('drums', 489), ('drums', 490), ('drums', 491),
        ('drums', 564), ('drums', 568), ('drums', 571), ('drums', 574), ('drums', 575),
        # Explosion (40)
        ('explosion', 351), ('explosion', 782), ('explosion', 1278), ('explosion', 1300),
        ('explosion', 1338), ('explosion', 1343), ('explosion', 1562), ('explosion', 1616),
        ('explosion', 1687), ('explosion', 1689), ('explosion', 1690), ('explosion', 1693),
        ('explosion', 1694), ('explosion', 1696), ('explosion', 1700), ('explosion', 1702),
        ('explosion', 1703), ('explosion', 1704), ('explosion', 1705), ('explosion', 1722),
        ('explosion', 2599), ('explosion', 2758), ('explosion', 2759), ('explosion', 2772),
        ('explosion', 2773), ('explosion', 2777), ('explosion', 2780), ('explosion', 2782),
        ('explosion', 2800), ('explosion', 2801), ('explosion', 2804), ('explosion', 2806),
        ('explosion', 2809), ('explosion', 2994), ('explosion', 1688), ('explosion', 1691),
        ('explosion', 1695), ('explosion', 1697), ('explosion', 1701), ('explosion', 2760),
        # Funny (45)
        ('funny', 343), ('funny', 391), ('funny', 395), ('funny', 414), ('funny', 422),
        ('funny', 424), ('funny', 429), ('funny', 471), ('funny', 473), ('funny', 527),
        ('funny', 528), ('funny', 578), ('funny', 579), ('funny', 616), ('funny', 715),
        ('funny', 744), ('funny', 746), ('funny', 923), ('funny', 959), ('funny', 2194),
        ('funny', 2209), ('funny', 2358), ('funny', 2364), ('funny', 2813), ('funny', 2873),
        ('funny', 2880), ('funny', 2881), ('funny', 2882), ('funny', 2885), ('funny', 2886),
        ('funny', 2889), ('funny', 2890), ('funny', 2891), ('funny', 2894), ('funny', 2955),
        ('funny', 3050), ('funny', 392), ('funny', 393), ('funny', 396), ('funny', 415),
        ('funny', 472), ('funny', 474), ('funny', 577), ('funny', 580), ('funny', 581),
        # Game (35)
        ('game', 226), ('game', 231), ('game', 265), ('game', 266), ('game', 276),
        ('game', 689), ('game', 2042), ('game', 2043), ('game', 2045), ('game', 2047),
        ('game', 2058), ('game', 2059), ('game', 2061), ('game', 2062), ('game', 2063),
        ('game', 2065), ('game', 2066), ('game', 2067), ('game', 2069), ('game', 2073),
        ('game', 2324), ('game', 2361), ('game', 2821), ('game', 2837), ('game', 3154),
        ('game', 227), ('game', 228), ('game', 232), ('game', 233), ('game', 264),
        ('game', 267), ('game', 275), ('game', 2044), ('game', 2046), ('game', 2060),
        # Horns (30)
        ('horns', 529), ('horns', 530), ('horns', 713), ('horns', 714), ('horns', 716),
        ('horns', 717), ('horns', 718), ('horns', 719), ('horns', 720), ('horns', 722),
        ('horns', 724), ('horns', 727), ('horns', 973), ('horns', 1565), ('horns', 1632),
        ('horns', 1654), ('horns', 2289), ('horns', 2291), ('horns', 2785), ('horns', 3111),
        ('horns', 715), ('horns', 721), ('horns', 723), ('horns', 725), ('horns', 726),
        ('horns', 728), ('horns', 972), ('horns', 1566), ('horns', 1633), ('horns', 2290),
        # Impact (35)
        ('impact', 263), ('impact', 772), ('impact', 773), ('impact', 774), ('impact', 781),
        ('impact', 784), ('impact', 788), ('impact', 833), ('impact', 1143), ('impact', 2150),
        ('impact', 2152), ('impact', 2182), ('impact', 2198), ('impact', 2199), ('impact', 2589),
        ('impact', 2600), ('impact', 2655), ('impact', 2778), ('impact', 2779), ('impact', 2784),
        ('impact', 2900), ('impact', 2901), ('impact', 2902), ('impact', 2905), ('impact', 2937),
        ('impact', 3046), ('impact', 775), ('impact', 776), ('impact', 783), ('impact', 785),
        ('impact', 786), ('impact', 834), ('impact', 835), ('impact', 2153), ('impact', 2183),
        # Laser (15)
        ('laser', 1554), ('laser', 1555), ('laser', 1556), ('laser', 1557), ('laser', 1558),
        ('laser', 1559), ('laser', 1560), ('laser', 1561), ('laser', 2810), ('laser', 2811),
        ('laser', 2812), ('laser', 2814), ('laser', 2815), ('laser', 2816), ('laser', 2817),
        # Notifications (20)
        ('notifications', 2309), ('notifications', 2310), ('notifications', 2311),
        ('notifications', 2312), ('notifications', 2313), ('notifications', 2314),
        ('notifications', 2315), ('notifications', 2316), ('notifications', 2317),
        ('notifications', 2318), ('notifications', 2319), ('notifications', 2320),
        ('notifications', 2321), ('notifications', 2322), ('notifications', 2323),
        ('notifications', 2325), ('notifications', 2326), ('notifications', 2327),
        ('notifications', 2328), ('notifications', 2329),
        # Sirens (25)
        ('sirens', 445), ('sirens', 1008), ('sirens', 1640), ('sirens', 1641), ('sirens', 1642),
        ('sirens', 1643), ('sirens', 1644), ('sirens', 1645), ('sirens', 1646), ('sirens', 1649),
        ('sirens', 1650), ('sirens', 1651), ('sirens', 1652), ('sirens', 1653), ('sirens', 1655),
        ('sirens', 1656), ('sirens', 1657), ('sirens', 1929), ('sirens', 1648), ('sirens', 1654),
        ('sirens', 1658), ('sirens', 1659), ('sirens', 1930), ('sirens', 1931), ('sirens', 1932),
        # Swoosh (20)
        ('swoosh', 1461), ('swoosh', 1462), ('swoosh', 1463), ('swoosh', 1464), ('swoosh', 1466),
        ('swoosh', 1467), ('swoosh', 1468), ('swoosh', 1469), ('swoosh', 1470), ('swoosh', 1471),
        ('swoosh', 1472), ('swoosh', 1473), ('swoosh', 1475), ('swoosh', 1476), ('swoosh', 1477),
        ('swoosh', 1478), ('swoosh', 1479), ('swoosh', 1480), ('swoosh', 1481), ('swoosh', 1482),
        # Transition (35)
        ('transition', 166), ('transition', 175), ('transition', 1146), ('transition', 1287),
        ('transition', 1465), ('transition', 1474), ('transition', 2282), ('transition', 2290),
        ('transition', 2412), ('transition', 2608), ('transition', 2615), ('transition', 2630),
        ('transition', 2638), ('transition', 2639), ('transition', 2719), ('transition', 2907),
        ('transition', 2908), ('transition', 2919), ('transition', 3057), ('transition', 3089),
        ('transition', 3114), ('transition', 3115), ('transition', 3120), ('transition', 3121),
        ('transition', 3146), ('transition', 3161), ('transition', 167), ('transition', 168),
        ('transition', 176), ('transition', 177), ('transition', 2283), ('transition', 2609),
        ('transition', 2616), ('transition', 2631), ('transition', 2640),
        # Water (15)
        ('water', 523), ('water', 524), ('water', 525), ('water', 526), ('water', 2401),
        ('water', 2402), ('water', 2403), ('water', 2404), ('water', 2405), ('water', 2406),
        ('water', 2407), ('water', 2409), ('water', 2410), ('water', 2411), ('water', 2413),
        # Whistles (30)
        ('whistles', 406), ('whistles', 506), ('whistles', 605), ('whistles', 606), ('whistles', 607),
        ('whistles', 608), ('whistles', 609), ('whistles', 610), ('whistles', 611), ('whistles', 612),
        ('whistles', 613), ('whistles', 614), ('whistles', 615), ('whistles', 738), ('whistles', 1631),
        ('whistles', 2049), ('whistles', 2050), ('whistles', 2587), ('whistles', 2647), ('whistles', 2657),
        ('whistles', 3103), ('whistles', 3105), ('whistles', 604), ('whistles', 616), ('whistles', 617),
        ('whistles', 739), ('whistles', 740), ('whistles', 2051), ('whistles', 2588), ('whistles', 2648),
        # Whoosh (30)
        ('whoosh', 787), ('whoosh', 1485), ('whoosh', 1486), ('whoosh', 1489), ('whoosh', 1490),
        ('whoosh', 1491), ('whoosh', 1492), ('whoosh', 1493), ('whoosh', 1714), ('whoosh', 1721),
        ('whoosh', 2350), ('whoosh', 2408), ('whoosh', 2596), ('whoosh', 2623), ('whoosh', 2650),
        ('whoosh', 2651), ('whoosh', 2903), ('whoosh', 2918), ('whoosh', 3005), ('whoosh', 3024),
        ('whoosh', 1487), ('whoosh', 1488), ('whoosh', 1494), ('whoosh', 1715), ('whoosh', 1716),
        ('whoosh', 1717), ('whoosh', 1718), ('whoosh', 1719), ('whoosh', 1720), ('whoosh', 2351),
    ]

    def _fill_soundboard_slots(self, file_map):
        """Download random sound effects from Mixkit to fill empty playback slots."""
        import os, random, urllib.request

        empty_slots = [str(k) for k in range(1, 10) if str(k) not in file_map]
        if not empty_slots:
            return

        cache_dir = os.path.join(self.announcement_directory, '.cache')
        os.makedirs(cache_dir, exist_ok=True)

        # Pick random sounds from pool (without replacement)
        pool = list(self.SOUNDBOARD_POOL)
        random.shuffle(pool)
        picks = pool[:len(empty_slots)]

        for slot, (category, sfx_id) in zip(empty_slots, picks):
            filename = f"{category}_{sfx_id}.mp3"
            filepath = os.path.join(cache_dir, filename)

            # Download if not already cached
            if not os.path.exists(filepath):
                url = f"https://assets.mixkit.co/active_storage/sfx/{sfx_id}/{sfx_id}-preview.mp3"
                try:
                    urllib.request.urlretrieve(url, filepath)
                    print(f"  [Soundboard] Downloaded: {filename}")
                except Exception as e:
                    print(f"  [Soundboard] Failed to download {filename}: {e}")
                    continue

            if os.path.exists(filepath):
                file_map[slot] = (filepath, filename)
    
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

        # Restore RTS state
        self._restore_playback_rts()

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
                    self._restore_playback_rts()
                    return None, False
                # Continue with the new file
            else:
                self._restore_playback_rts()
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
    
    def _restore_playback_rts(self):
        """Restore RTS to saved state after playback finishes (runs in background thread)."""
        _saved = getattr(self.gateway, '_playback_rts_saved', None)
        if _saved is not None:
            self.gateway._playback_rts_saved = None
            _cat = getattr(self.gateway, 'cat_client', None)
            if _cat and (_saved is True or _saved is None):
                def _do_restore():
                    try:
                        _cat.set_rts(True if _saved else False)
                        print(f"\n[Playback] RTS restored to {'USB' if _saved else 'Radio'} Controlled")
                        # Refresh display after RTS change to prevent VFO display corruption
                        time.sleep(0.3)
                        _cat._pause_drain()
                        try:
                            _cat._send_button([0x00, 0x25], 3, 5)  # Left dial press
                            time.sleep(0.15)
                            _cat._send_button_release()
                            time.sleep(0.3)
                            _cat._drain(0.5)
                            _cat._send_button([0x00, 0xA5], 3, 5)  # Right dial press
                            time.sleep(0.15)
                            _cat._send_button_release()
                            time.sleep(0.3)
                            _cat._drain(0.5)
                        finally:
                            _cat._drain_paused = False
                    except Exception:
                        pass
                import threading
                threading.Thread(target=_do_restore, daemon=True, name="RTS-Restore").start()

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
            except Exception:
                pass
        if self.tx_pipe:
            try:
                self.tx_pipe.close()
            except Exception:
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
            import os as _os
            if not self.config.VERBOSE_LOGGING:
                _saved = _os.dup(2)
                try:
                    _dn = _os.open(_os.devnull, _os.O_WRONLY)
                    _os.dup2(_dn, 2); _os.close(_dn)
                    self.pyaudio = pyaudio.PyAudio()
                finally:
                    _os.dup2(_saved, 2); _os.close(_saved)
            else:
                self.pyaudio = pyaudio.PyAudio()

            if self.config.VERBOSE_LOGGING:
                config_device_attr = 'SDR2_DEVICE_NAME' if self.name == "SDR2" else 'SDR_DEVICE_NAME'
                target_name = getattr(self.config, config_device_attr, '')
                print(f"[{self.name}] Searching for device matching: {target_name}")
                print(f"[{self.name}] Available input devices:")
                for i in range(self.pyaudio.get_device_count()):
                    info = self.pyaudio.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        print(f"[{self.name}]   [{i}] {info['name']} (in:{info['maxInputChannels']})")

            device_index, device_name = self._find_device()

            if device_index is None:
                config_device_attr = 'SDR2_DEVICE_NAME' if self.name == "SDR2" else 'SDR_DEVICE_NAME'
                print(f"[{self.name}] ✗ SDR device not found")
                target = getattr(self.config, config_device_attr, '')
                if target:
                    print(f"[{self.name}]   Looked for: {target}")
                    print(f"[{self.name}]   Try one of these formats:")
                    print(f"[{self.name}]     {config_device_attr} = Loopback")
                    print(f"[{self.name}]     {config_device_attr} = hw:2,0")
                    print(f"[{self.name}]   Or enable VERBOSE_LOGGING to see all devices")
                return False

            if not self._open_stream(device_index):
                raise Exception(f"Failed to open audio stream on device {device_index}")

            # Start the stream explicitly (callback mode)
            if not self.input_stream.is_active():
                self.input_stream.start_stream()

            if self.config.VERBOSE_LOGGING:
                config_buffer_attr = 'SDR2_BUFFER_MULTIPLIER' if self.name == "SDR2" else 'SDR_BUFFER_MULTIPLIER'
                buffer_multiplier = getattr(self.config, config_buffer_attr, 4)
                buffer_size = self.config.AUDIO_CHUNK_SIZE * buffer_multiplier
                print(f"[{self.name}] ✓ Audio input configured: {device_name}")
                print(f"[{self.name}]   Channels: {self.sdr_channels} ({'stereo' if self.sdr_channels == 2 else 'mono'})")
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
                        tail_arr = np.frombuffer(self._sub_buffer[-_SMOOTH_BYTES:], dtype=np.int16).astype(np.float32)
                        w = np.linspace(0.0, 1.0, len(tail_arr), dtype=np.float32)
                        tail_arr = tail_arr * (1.0 - w) + mid * w
                        self._sub_buffer = self._sub_buffer[:-_SMOOTH_BYTES] + np.clip(tail_arr, -32768, 32767).astype(np.int16).tobytes()
                        # Taper head of blob from midpoint
                        head_arr = np.frombuffer(blob[:_SMOOTH_BYTES], dtype=np.int16).astype(np.float32)
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

        # Apply per-source audio processing (HPF, LPF, notch, gate, etc.)
        raw = self.gateway.process_audio_for_sdr(raw)

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
    monitor via parec subprocess.  PipeWire delivers a continuous, low-jitter
    stream — no blob boundaries, no prebuffering gaps, no crossfade needed.

    Config: set SDR_DEVICE_NAME = pw:<sink_name> (e.g. pw:sdr_capture)
    The sink must exist (created via pw-cli or startup script) and the SDR
    app's output must be routed to it.
    """

    def __init__(self, config, gateway, name="SDR1", sdr_priority=1):
        super().__init__(config, gateway, name=name, sdr_priority=sdr_priority)
        self._parec_proc = None
        self._reader_thread = None
        self._reader_running = False
        self._pw_sink_name = None  # set in setup_audio

    def setup_audio(self):
        """Start parec subprocess reading from PipeWire monitor."""
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

        # Start parec reading from PipeWire monitor (native PulseAudio, no FFmpeg overhead)
        try:
            self._parec_proc = _sp.Popen([
                'parec',
                '--device=' + monitor_name,
                '--format=s16le',
                '--rate=' + str(self.config.AUDIO_RATE),
                '--channels=2',
                '--latency-msec=50',
            ], stdout=_sp.PIPE, stderr=_sp.PIPE)
        except FileNotFoundError:
            print(f"[{self.name}] parec not found — required for PipeWire SDR source")
            return False
        except Exception as e:
            print(f"[{self.name}] Failed to start parec: {e}")
            return False

        # Reader thread: reads fixed-size chunks from parec stdout and queues them
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
            if self._parec_proc:
                self._parec_proc.kill()
            return False

    def _pw_reader_loop(self):
        """Read fixed-size chunks from parec stdout and queue them."""
        chunk_bytes = self._chunk_bytes  # 50ms stereo = 9600 bytes
        proc = self._parec_proc
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

        # If queue is building up (>6), drain extras to cap latency at ~300ms
        if data is not None:
            qsz = self._chunk_queue.qsize()
            while qsz > 6:
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

        # Apply SDR audio processing (HPF, LPF, notch, de-esser, spectral NS, gate)
        raw = self.gateway.process_audio_for_sdr(raw)

        return raw, False

    def cleanup(self):
        """Stop parec and reader thread."""
        self._reader_running = False
        self.input_stream = None
        if self._parec_proc:
            try:
                self._parec_proc.kill()
                self._parec_proc.wait(timeout=2)
            except Exception:
                pass
            self._parec_proc = None

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
        if self._parec_proc and self._parec_proc.poll() is None:
            self._reader_running = True
            if not self._reader_thread or not self._reader_thread.is_alive():
                self._reader_thread = threading.Thread(
                    target=self._pw_reader_loop, daemon=True, name=f"{self.name}-pw-reader")
                self._reader_thread.start()

    def _close_stream(self):
        """Close the parec stream."""
        self.cleanup()

    def _find_device(self):
        """Not used for PipeWire source."""
        return None, None

    def _watchdog_recover(self, max_restarts):
        """Restart parec if it died."""
        if self._parec_proc and self._parec_proc.poll() is not None:
            print(f"[{self.name}] PipeWire: parec process died, restarting...")
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
            if self._reader_thread.is_alive():
                print(f"[RemoteAudioSource] Warning: reader thread did not stop within 2s")
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
            if self._reader_thread.is_alive():
                print(f"[ANNIN] Warning: reader thread did not stop within 2s")
        self._sub_buffer = b''


class WebMicSource(AudioSource):
    """Receives browser microphone audio via WebSocket and routes to radio TX.

    PTT is explicitly controlled by the user's button toggle — active for the
    entire duration of the WebSocket connection, not gated by audio level.
    """
    def __init__(self, config, gateway):
        super().__init__("WEBMIC", config)
        self.gateway = gateway
        self.priority = 0
        self.ptt_control = True
        self.volume = float(getattr(config, 'WEB_MIC_VOLUME', 25.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono

    def setup_audio(self):
        return True  # WebSocket handler manages connections

    def push_audio(self, pcm_bytes):
        """Called by WebSocket handler to push raw PCM into the queue."""
        try:
            self._chunk_queue.put_nowait(pcm_bytes)
        except _queue_mod.Full:
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(pcm_bytes)
            except _queue_mod.Full:
                pass

    def get_audio(self, chunk_size):
        if not self.enabled or self.muted or not self.client_connected:
            return None, False

        cb = self._chunk_bytes

        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                # No audio in queue but client is connected — send silence, keep PTT keyed
                return b'\x00' * cb, True

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering (for UI display only)
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        raw_level = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60))) if rms > 0 else 0
        self.audio_level = raw_level if raw_level > self.audio_level else int(self.audio_level * 0.7 + raw_level * 0.3)

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, True

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "WEBMIC: Disabled"
        elif self.client_connected:
            return f"WEBMIC: TX ({self.audio_level}%)"
        else:
            return "WEBMIC: Idle"

    def cleanup(self):
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
            except Exception:
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
    """TCP client for TH-9800 CAT control via TH9800_CAT.py server.

    IMPORTANT — TH9800 radio protocol quirks (hard-won knowledge, do NOT change
    without reading this):

    1. PRESS RESPONSE IS UNRELIABLE: When you press a VFO dial, the radio sends
       back a CHANNEL_TEXT packet containing the OTHER VFO's channel, not the
       pressed VFO's channel.  DO NOT use the press response to read the current
       channel.  Instead, press the dial (activates for editing), then step right
       + step left (net zero movement) and read _channel_text[vfo] from the step
       response, which is always correct.

    2. STEP RESPONSE IS RELIABLE: After a dial step (right/left), _channel_text[vfo]
       always contains the stepped VFO's actual channel number.

    3. BACKGROUND DRAIN MUST BE PAUSED: set_channel() and send_web_command() must
       set _drain_paused = True for the duration of the operation.  The background
       drain thread will otherwise populate _channel_text concurrently, causing
       stale data to overwrite the response we're trying to read.

    4. _drain() MUST USE SINGLE _recv_line(): Using a loop (while self._buf:
       _recv_line()) breaks ALL packet parsing — state dicts end up empty.  The
       drain method reads raw socket data in a loop, then calls _recv_line() once
       to process buffered packets.

    5. NEVER PRESS V/M BUTTON: set_channel() must not attempt to detect or switch
       VFO/memory mode.  The V/M detection via _channel_vfo is unreliable
       (DISPLAY_CHANGE 0x03 packets always report the opposite VFO after a press).
       If the radio is in VFO mode, set_channel() returns False and the user must
       switch manually.

    6. RTS: Set once at startup to USB Controlled.  Do not use save/restore
       patterns (_with_usb_rts) as they can disrupt the serial connection.

    7. SOCKET CONTENTION: The background drain thread and command senders share
       one TCP socket.  Before sending any command, call _pause_drain() which
       sets _drain_paused AND waits for _drain_active to go False.  Just setting
       the flag is not enough — the drain thread may already be inside _drain()
       reading from the socket and will consume command responses.

    8. AUTH PER-CONNECTION: TH9800_CAT.py uses per-connection auth (conn_loggedin
       local variable).  If _send_cmd gets 'Unauthorized', it auto-re-auths and
       retries.  This handles cases where the server resets auth unexpectedly.
    """

    START_BYTES = b'\xAA\xFD'

    # 12-byte default payload template (button release / return control to body)
    DEFAULT_PAYLOAD = bytearray([0x84,0xFF,0xFF,0xFF,0xFF,0x81,0xFF,0xFF,0x82,0xFF,0xFF,0x00])

    # VFO identifiers
    LEFT = 'LEFT'
    RIGHT = 'RIGHT'

    def __init__(self, host, port, password='', verbose=False):
        self._host = host
        self._port = port
        self._password = password
        self._verbose = verbose
        self._sock = None
        self._buf = b''
        # Radio state parsed from forwarded packets
        self._channel = ''       # Latest channel text (3-char, e.g. "001")
        self._channel_vfo = ''   # VFO from last CHANNEL_TEXT (UNRELIABLE after press — see class docstring)
        self._vfo_text = {}      # {'LEFT': '...', 'RIGHT': '...'} display text per VFO
        self._channel_text = {}  # {'LEFT': '001', 'RIGHT': '002'} channel number per VFO
        self._power = {}         # {'LEFT': 'H', 'RIGHT': 'L'}
        self._volume = {}        # {'LEFT': 62, 'RIGHT': 0} last-set volume per VFO
        self._signal = {}        # {'LEFT': 0-9, 'RIGHT': 0-9} S-meter
        self._icons = {'LEFT': {}, 'RIGHT': {}, 'COMMON': {}}  # icon states
        self._drain_paused = False  # pause drain thread during command sequences
        self._drain_active = False  # True while drain thread is inside _drain()
        self._sock_lock = threading.Lock()  # serialize all socket reads (drain vs commands)
        self._last_activity = 0  # monotonic timestamp of last send/recv (for status bar)
        self._stop = False       # set True to abort loops (ctrl+c)
        self._log = None         # file handle for debug log
        self._rts_usb = None     # True = USB Controlled, False = Radio Controlled, None = unknown
        self._serial_connected = False  # Cached serial state (set by connect/disconnect handlers)
        self._cmd_sent = 0       # total commands sent
        self._cmd_no_response = 0  # commands with no radio response
        self._last_no_response = ''  # description of last no-response event

    def _logmsg(self, msg, console=False):
        """Write debug message to cat_debug.log. Only prints to console if verbose or console=True."""
        if console or self._verbose:
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
        """Gracefully close TCP connection to CAT server."""
        # Stop drain thread first so it doesn't race with socket close
        self._stop = True

        if self._sock:
            # Send !exit so the server closes its end cleanly
            try:
                self._sock.sendall(b'!exit\n')
            except Exception:
                pass
            # Brief pause to let the server process the exit command
            time.sleep(0.1)
            # Shut down socket (signals EOF to server even if !exit was lost)
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        # Give drain thread time to notice _stop and exit
        time.sleep(0.15)

        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

    def _send_cmd(self, cmd):
        """Send text command and return response line.
        Auto-re-authenticates if server returns 'Unauthorized'.
        Acquires _sock_lock to prevent drain thread from reading concurrently."""
        if not self._sock:
            return None
        with self._sock_lock:
            try:
                self._sock.sendall(f"{cmd}\n".encode())
                self._last_activity = time.monotonic()
                resp = self._recv_line(timeout=2.0)
                if resp and 'Unauthorized' in resp:
                    self._logmsg(f"  CAT: session lost auth, re-authenticating...", console=True)
                    self._sock.sendall(f"!pass {self._password}\n".encode())
                    auth_resp = self._recv_line(timeout=2.0)
                    if auth_resp and 'Login Successful' in auth_resp:
                        # Retry the original command
                        self._sock.sendall(f"{cmd}\n".encode())
                        self._last_activity = time.monotonic()
                        resp = self._recv_line(timeout=2.0)
                    else:
                        self._logmsg(f"  CAT: re-auth failed: {auth_resp}", console=True)
                return resp
            except Exception as e:
                self._logmsg(f"  CAT send error: {e}")
                return None

    def _with_usb_rts(self, func):
        """Run func() with RTS in USB Controlled mode, restore afterwards if changed."""
        rts_was = self._rts_usb
        if rts_was is not True:
            self.set_rts(True)
        try:
            return func()
        finally:
            if rts_was is not True and rts_was is not None:
                self.set_rts(rts_was)

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
        """Drain any pending data from socket, parsing packets along the way.

        IMPORTANT: Must end with a SINGLE _recv_line() call, NOT a loop.
        Using 'while self._buf: _recv_line()' breaks all packet parsing and
        leaves state dicts (_channel_text, _power, etc.) empty.  See bugs.md.
        Acquires _sock_lock to prevent concurrent reads with _send_cmd."""
        if not self._sock:
            return
        with self._sock_lock:
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

    def set_rts(self, usb_controlled):
        """Set RTS state. True = USB Controlled, False = Radio Controlled."""
        resp = self._send_cmd(f"!rts {usb_controlled}")
        # Response format: "CMD{rts[True]} True" — check for 'true' anywhere
        if resp:
            self._rts_usb = 'true' in resp.lower()
        else:
            self._rts_usb = usb_controlled
        self._logmsg(f"  CAT RTS set to {'USB' if self._rts_usb else 'Radio'} Controlled: {resp}")
        return resp

    def query_rts(self):
        """Query current RTS state from TH9800 (toggle then toggle back would be destructive,
        so just return cached state)."""
        return self._rts_usb

    def get_rts(self):
        """Return last known RTS state. True = USB Controlled, False = Radio Controlled."""
        return self._rts_usb

    def get_serial_status(self):
        """Return cached serial connection state (set by connect/disconnect commands).
        Does NOT poll TH9800_CAT — polling over TCP steals radio packets and
        causes lock contention that stalls button commands."""
        return self._serial_connected

    def get_radio_state(self):
        """Return full radio state dict for web dashboard."""
        return {
            'connected': self._sock is not None,
            'serial_connected': self._serial_connected,
            'rts_usb': self._rts_usb,
            'volume': {
                'left': self._volume.get(self.LEFT, -1),
                'right': self._volume.get(self.RIGHT, -1),
            },
            'left': {
                'display': self._vfo_text.get(self.LEFT, ''),
                'channel': self._channel_text.get(self.LEFT, ''),
                'power': self._power.get(self.LEFT, ''),
                'signal': self._signal.get(self.LEFT, 0),
                'icons': dict(self._icons.get(self.LEFT, {})),
            },
            'right': {
                'display': self._vfo_text.get(self.RIGHT, ''),
                'channel': self._channel_text.get(self.RIGHT, ''),
                'power': self._power.get(self.RIGHT, ''),
                'signal': self._signal.get(self.RIGHT, 0),
                'icons': dict(self._icons.get(self.RIGHT, {})),
            },
            'common': dict(self._icons.get('COMMON', {})),
        }

    # Command lookup table for web buttons — maps label to (cmd_bytes, start, end)
    # Mirrors TH9800_Enums.RADIO_TX_CMD
    WEB_COMMANDS = {
        # Left VFO buttons
        'L_LOW': ([0x00, 0x21], 3, 5), 'L_LOW_HOLD': ([0x01, 0x21], 3, 5),
        'L_VM': ([0x00, 0x22], 3, 5), 'L_VM_HOLD': ([0x01, 0x22], 3, 5),
        'L_HM': ([0x00, 0x23], 3, 5), 'L_HM_HOLD': ([0x01, 0x23], 3, 5),
        'L_SCN': ([0x00, 0x24], 3, 5), 'L_SCN_HOLD': ([0x01, 0x24], 3, 5),
        'L_DIAL_LEFT': ([0x01], 2, 3), 'L_DIAL_RIGHT': ([0x02], 2, 3),
        'L_DIAL_PRESS': ([0x00, 0x25], 3, 5), 'L_DIAL_HOLD': ([0x01, 0x25], 3, 5),
        'L_SET_VFO': ([0x23, 0x24], 3, 5),
        # Right VFO buttons
        'R_LOW': ([0x00, 0xA1], 3, 5), 'R_LOW_HOLD': ([0x01, 0xA1], 3, 5),
        'R_VM': ([0x00, 0xA2], 3, 5), 'R_VM_HOLD': ([0x01, 0xA2], 3, 5),
        'R_HM': ([0x00, 0xA3], 3, 5), 'R_HM_HOLD': ([0x01, 0xA3], 3, 5),
        'R_SCN': ([0x00, 0xA4], 3, 5), 'R_SCN_HOLD': ([0x01, 0xA4], 3, 5),
        'R_DIAL_LEFT': ([0x81], 2, 3), 'R_DIAL_RIGHT': ([0x82], 2, 3),
        'R_DIAL_PRESS': ([0x00, 0xA5], 3, 5), 'R_DIAL_HOLD': ([0x01, 0xA5], 3, 5),
        'R_SET_VFO': ([0x24, 0x23], 3, 5),
        # Menu / SET
        'N_SET': ([0x00, 0x20], 3, 5), 'N_SET_HOLD': ([0x01, 0x20], 3, 5),
        # Mic keypad
        'MIC_0': ([0x00, 0x00], 3, 5), 'MIC_1': ([0x00, 0x01], 3, 5),
        'MIC_2': ([0x00, 0x02], 3, 5), 'MIC_3': ([0x00, 0x03], 3, 5),
        'MIC_4': ([0x00, 0x04], 3, 5), 'MIC_5': ([0x00, 0x05], 3, 5),
        'MIC_6': ([0x00, 0x06], 3, 5), 'MIC_7': ([0x00, 0x07], 3, 5),
        'MIC_8': ([0x00, 0x08], 3, 5), 'MIC_9': ([0x00, 0x09], 3, 5),
        'MIC_A': ([0x00, 0x0A], 3, 5), 'MIC_B': ([0x00, 0x0B], 3, 5),
        'MIC_C': ([0x00, 0x0C], 3, 5), 'MIC_D': ([0x00, 0x0D], 3, 5),
        'MIC_STAR': ([0x00, 0x0E], 3, 5), 'MIC_POUND': ([0x00, 0x0F], 3, 5),
        'MIC_P1': ([0x00, 0x10], 3, 5), 'MIC_P2': ([0x00, 0x11], 3, 5),
        'MIC_P3': ([0x00, 0x12], 3, 5), 'MIC_P4': ([0x00, 0x13], 3, 5),
        'MIC_UP': ([0x00, 0x14], 3, 5), 'MIC_DOWN': ([0x00, 0x15], 3, 5),
        'MIC_PTT': ([0x00], 1, 2),
        # Hyper memories
        'HYPER_A': ([0x00, 0x27], 3, 5), 'HYPER_B': ([0x00, 0x28], 3, 5),
        'HYPER_C': ([0x00, 0x29], 3, 5), 'HYPER_D': ([0x00, 0xAA], 3, 5),
        'HYPER_E': ([0x00, 0xAB], 3, 5), 'HYPER_F': ([0x00, 0xAC], 3, 5),
        # Single VFO (L_VOLUME_HOLD)
        'L_VOLUME_HOLD': ([0x00, 0x26], 3, 5),
    }

    def send_web_command(self, cmd_name):
        """Send a named button command from web UI.
        Returns True on success, False on no-response, or string error message."""
        if cmd_name == 'DEFAULT':
            resp = self._send_button_release()
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            return True
        if cmd_name == 'TOGGLE_RTS':
            self._pause_drain()
            try:
                resp = self._send_cmd("!rts")
                if resp and 'serial not connected' in resp:
                    return 'serial not connected'
                if resp:
                    self._rts_usb = 'true' in resp.lower()
            finally:
                self._drain_paused = False
            return True
        if cmd_name == 'MIC_PTT':
            self._pause_drain()
            try:
                resp = self._send_cmd("!ptt")
                if resp and 'serial not connected' in resp:
                    return 'serial not connected'
            finally:
                self._drain_paused = False
            return True
        entry = self.WEB_COMMANDS.get(cmd_name)
        if not entry:
            return False
        cmd_bytes, start, end = entry
        is_dial = cmd_name in ('L_DIAL_RIGHT', 'L_DIAL_LEFT', 'R_DIAL_RIGHT', 'R_DIAL_LEFT',
                               'L_DIAL_PRESS', 'R_DIAL_PRESS')
        pre_channel = dict(self._channel_text) if is_dial else None
        self._cmd_sent += 1
        self._pause_drain()
        try:
            resp1 = self._send_button(cmd_bytes, start, end)
            # Check if serial is not connected before continuing
            if resp1 and 'serial not connected' in resp1:
                return 'serial not connected'
            time.sleep(0.15)
            resp2 = self._send_button_release()
            time.sleep(0.15)
            # Read response while drain is still paused — if we unpause first,
            # the drain thread races us for the radio's binary response packets
            pre_buf = len(self._buf)
            self._drain(0.3)
            post_buf = len(self._buf)
        finally:
            self._drain_paused = False
        # For dial commands, verify radio responded
        if is_dial:
            post_channel = dict(self._channel_text)
            if post_channel == pre_channel:
                self._cmd_no_response += 1
                self._last_no_response = f"web {cmd_name} @ {time.strftime('%H:%M:%S')}"
                self._logmsg(f"    Web {cmd_name}: no response (sent={self._cmd_sent} missed={self._cmd_no_response}) "
                             f"resp1={resp1!r} resp2={resp2!r} buf={pre_buf}->{post_buf} ch={pre_channel}", console=True)
                return False
            else:
                # Reset consecutive failure counter on success
                self._cmd_no_response = 0
        return True

    def reconnect(self):
        """Close and reopen the TCP connection to CAT server."""
        self._stop = True
        self.close()
        time.sleep(0.5)
        self._stop = False
        self._buf = b''
        if self.connect():
            self.start_background_drain()
            return True
        return False

    def serial_reconnect(self):
        """Disconnect and reconnect the radio serial via CAT server.
        Returns True if reconnect succeeded."""
        self._logmsg("  CAT: Auto-recovering serial (disconnect/reconnect)...", console=True)
        self._pause_drain()
        try:
            with self._sock_lock:
                self._sock.sendall(b"!serial disconnect\n")
                resp = self._recv_line(timeout=3.0)
            self._logmsg(f"  CAT: Serial disconnect: {resp}", console=True)
            time.sleep(1.0)
            with self._sock_lock:
                self._sock.sendall(b"!serial connect\n")
                # connect takes ~3s (startup sequence + sleeps)
                resp = self._recv_line(timeout=10.0)
            if resp and 'connected' in resp:
                self._logmsg("  CAT: Serial reconnected successfully", console=True)
                self._cmd_no_response = 0
                return True
            else:
                self._logmsg(f"  CAT: Serial reconnect failed: {resp}", console=True)
                return False
        except Exception as e:
            self._logmsg(f"  CAT: Serial reconnect error: {e}", console=True)
            return False
        finally:
            self._drain_paused = False

    def _pause_drain(self):
        """Pause background drain and wait for it to actually stop reading."""
        self._drain_paused = True
        # Wait for drain thread to exit _drain() (up to 1s)
        for _ in range(20):
            if not self._drain_active:
                break
            time.sleep(0.05)

    def start_background_drain(self):
        """Start background thread that continuously reads radio packets for live state updates."""
        def _drain_loop():
            while self._sock and not self._stop:
                if self._drain_paused:
                    self._drain_active = False
                    time.sleep(0.05)
                    continue
                try:
                    self._drain_active = True
                    self._drain(0.5)
                    self._drain_active = False
                except Exception:
                    self._drain_active = False
                time.sleep(0.1)
        t = threading.Thread(target=_drain_loop, daemon=True, name='cat-drain')
        t.start()

    def send_web_volume(self, vfo, level):
        """Set volume from web UI. vfo='LEFT'/'RIGHT', level=0-100.
        Returns response string or 'serial not connected'."""
        level = max(0, min(100, int(level)))
        vfo_letter = 'LEFT' if vfo == self.LEFT else 'RIGHT'
        self._pause_drain()
        try:
            resp = self._send_cmd(f"!vol {vfo_letter} {level}")
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            self._volume[vfo] = level
            return resp
        finally:
            self._drain_paused = False

    def send_web_squelch(self, vfo, level):
        """Set squelch from web UI via raw packet. vfo='LEFT'/'RIGHT', level=0-100.
        Returns 'serial not connected' on failure."""
        level = max(0, min(100, int(level)))
        if vfo == self.LEFT:
            cmd_bytes = [0x02, 0xEB, level & 0xFF]
        else:
            cmd_bytes = [0x82, 0xEB, level & 0xFF]
        self._pause_drain()
        try:
            resp = self._send_button(cmd_bytes, 8, 11)
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            return resp
        finally:
            self._drain_paused = False

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
            # NOTE: vfo_byte mapping is correct for STEP responses but misleading
            # for PRESS responses (press returns the OTHER VFO's channel).
            # Do NOT use press response to determine the pressed VFO's channel.
            # See class docstring and set_channel() for the full explanation.
            if vfo_byte in (0x40, 0x60):
                self._channel_vfo = self.LEFT
            elif vfo_byte in (0xC0, 0xE0):
                self._channel_vfo = self.RIGHT
            if len(data) >= 6:
                try:
                    ch = data[3:6].decode('ascii', errors='replace').strip()
                    if ch:  # Don't blank channel with empty radio packet
                        self._channel = ch
                        self._channel_text[self._channel_vfo] = self._channel
                    self._logmsg(f"    [pkt] CHANNEL_TEXT vfo_byte=0x{vfo_byte:02X} -> {self._channel_vfo} ch='{ch}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x01:  # DISPLAY_TEXT
            if len(data) >= 9:
                try:
                    text = data[3:9].decode('ascii', errors='replace').strip()
                    # Determine VFO from vfo_byte (same mapping as DISPLAY_ICONS)
                    # Fall back to _channel_vfo only if vfo_byte is unknown
                    if vfo_byte in (0x40, 0x60):
                        dt_vfo = self.LEFT
                    elif vfo_byte in (0xC0, 0xE0):
                        dt_vfo = self.RIGHT
                    else:
                        dt_vfo = self._channel_vfo
                    # Don't overwrite with empty text (radio sends blank packets during refresh)
                    if dt_vfo and text:
                        self._vfo_text[dt_vfo] = text
                    self._logmsg(f"    [pkt] DISPLAY_TEXT vfo_byte=0x{vfo_byte:02X} vfo={dt_vfo} text='{text}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x04:  # DISPLAY_ICONS (full icon state)
            if vfo_byte == 0x40:
                vfo = self.LEFT
            elif vfo_byte == 0xC0:
                vfo = self.RIGHT
            else:
                self._logmsg(f"    [pkt] DISPLAY_ICONS unknown vfo=0x{vfo_byte:02X}", console=False)
                return
            if len(data) >= 8:
                # Parse all icon bytes
                icons = self._icons[vfo]
                # Index 2: APO, LOCK, KEY2, SET
                if len(data) > 3:
                    b = data[3]
                    self._icons['COMMON']['APO'] = bool(b & 0x02)
                    self._icons['COMMON']['LOCK'] = bool(b & 0x08)
                    self._icons['COMMON']['KEY2'] = bool(b & 0x20)
                    self._icons['COMMON']['SET'] = bool(b & 0x80)
                # Index 3: NEG, POS, TX, MAIN
                if len(data) > 4:
                    b = data[4]
                    icons['NEG'] = bool(b & 0x02)
                    icons['POS'] = bool(b & 0x08)
                    icons['TX'] = bool(b & 0x20)
                    icons['MAIN'] = bool(b & 0x80)
                # Index 4: PREF, SKIP, ENC, DEC
                if len(data) > 5:
                    b = data[5]
                    icons['PREF'] = bool(b & 0x02)
                    icons['SKIP'] = bool(b & 0x08)
                    icons['ENC'] = bool(b & 0x20)
                    icons['DEC'] = bool(b & 0xA0 == 0xA0)
                # Index 5: DCS, MUTE, MT, BUSY
                if len(data) > 6:
                    b = data[6]
                    icons['DCS'] = bool(b & 0x02)
                    icons['MUTE'] = bool(b & 0x08)
                    icons['MT'] = bool(b & 0x20)
                    icons['BUSY'] = bool(b & 0x80)
                # Index 6: power (L/M/H), AM
                if len(data) > 7:
                    b = data[7]
                    icons['AM'] = bool(b & 0x80)
                    if b & 0x08:
                        self._power[vfo] = 'L'
                    elif b & 0x02:
                        self._power[vfo] = 'M'
                    else:
                        self._power[vfo] = 'H'
                self._logmsg(f"    [pkt] DISPLAY_ICONS vfo={vfo} power={self._power.get(vfo,'')} icons={icons}", console=False)

        elif pkt_type == 0x1D:  # ICON_SIG_BARS
            sig_val = vfo_byte
            if sig_val >= 0x80:
                vfo = self.RIGHT
                sig_val -= 0x80
            else:
                vfo = self.LEFT
            self._signal[vfo] = min(sig_val, 9)
            self._logmsg(f"    [pkt] SIG_BARS vfo={vfo} S{self._signal[vfo]}", console=False)

        elif 0x10 <= pkt_type <= 0x27:  # Individual icon commands
            # Determine VFO from vfo_byte
            if vfo_byte >= 0x80:
                vfo = self.RIGHT
            else:
                vfo = self.LEFT
            icon_on = bool(vfo_byte & 0x01) if pkt_type not in (0x1D,) else True
            icon_names = {
                0x10: 'SET', 0x11: 'KEY2', 0x12: 'LOCK', 0x13: 'APO',
                0x14: 'MAIN', 0x15: 'TX', 0x16: 'POS', 0x17: 'NEG',
                0x18: 'ENCDEC', 0x19: 'ENC', 0x1A: 'SKIP', 0x1B: 'PREF',
                0x1C: 'BUSY', 0x1E: 'MT', 0x1F: 'MUTE', 0x20: 'DCS',
                0x21: 'AM', 0x23: 'PWR_LOW', 0x24: 'PWR_MED',
            }
            name = icon_names.get(pkt_type)
            if name:
                target = 'COMMON' if name in ('SET', 'KEY2', 'LOCK', 'APO') else vfo
                self._icons[target][name] = icon_on
                # Update power from individual icon commands
                if name == 'PWR_LOW' and icon_on:
                    self._power[vfo] = 'L'
                elif name == 'PWR_MED' and icon_on:
                    self._power[vfo] = 'M'
                elif name in ('PWR_LOW', 'PWR_MED') and not icon_on:
                    # If both off, it's high
                    if not self._icons.get(vfo, {}).get('PWR_LOW') and not self._icons.get(vfo, {}).get('PWR_MED'):
                        self._power[vfo] = 'H'
            self._logmsg(f"    [pkt] ICON 0x{pkt_type:02X} vfo={vfo} on={icon_on} name={name}", console=False)
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

    def _send_button_checked(self, cmd_bytes, start, end, label='button'):
        """Send button press + release and verify radio responded.
        Returns True if _channel_text changed, False if no response detected."""
        pre_state = dict(self._channel_text)
        self._cmd_sent += 1
        self._send_button(cmd_bytes, start, end)
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)
        post_state = dict(self._channel_text)
        if post_state == pre_state:
            self._cmd_no_response += 1
            self._last_no_response = f"{label} @ {time.strftime('%H:%M:%S')}"
            self._logmsg(f"    {label}: no radio response (sent={self._cmd_sent} missed={self._cmd_no_response})", console=True)
            return False
        return True

    def _channel_matches(self, target_int):
        """Compare current channel to target as integers, tolerant of padding/spaces."""
        try:
            return int(self._channel) == target_int
        except (ValueError, TypeError):
            return False

    def set_channel(self, vfo, target_channel):
        """Set channel on specified VFO by stepping the dial. Returns True on success.

        The press response is UNRELIABLE (returns the other VFO's channel).
        To read the current channel, we press then step-right + step-left (net
        zero) and read _channel_text[vfo] from the step response, which is
        always correct.  Background drain is paused for the entire operation.
        Never presses V/M — returns False if radio is in VFO mode."""
        target_int = int(target_channel)
        other_vfo = self.RIGHT if vfo == self.LEFT else self.LEFT
        self._logmsg(f"  CAT: Setting {vfo} channel to {target_int}...")

        # Pause background drain so it doesn't race with our reads
        self._pause_drain()
        try:
            return self._set_channel_inner(vfo, target_int, other_vfo)
        finally:
            self._drain_paused = False

    def _set_channel_inner(self, vfo, target_int, other_vfo):
        """Inner channel-setting logic (called with drain paused).

        Press response is unreliable — it returns the OTHER VFO's channel, not
        the pressed VFO's.  To read the current channel reliably, we press the
        dial (activates it for editing), then step right + step left (net zero
        movement) and read from the step response which is always correct."""

        # Press the VFO dial to activate it for editing
        self._drain()
        self._channel_text.clear()
        if vfo == self.LEFT:
            self._send_button([0x00, 0x25], 3, 5)  # L_DIAL_PRESS
        else:
            self._send_button([0x00, 0xA5], 3, 5)  # R_DIAL_PRESS
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)

        # Step right then left (net zero) to read current channel from step response
        step_r = [0x02] if vfo == self.LEFT else [0x82]  # DIAL_RIGHT
        step_l = [0x01] if vfo == self.LEFT else [0x81]  # DIAL_LEFT
        for step_cmd in (step_r, step_l):
            self._channel_text.pop(vfo, None)
            self._send_button(step_cmd, 2, 3)
            time.sleep(0.05)
            self._send_button_release()
            time.sleep(0.15)
            self._drain(0.3)

        # After step-left, _channel_text[vfo] = current channel (back to original)
        ch = self._channel_text.get(vfo, '').strip()
        self._logmsg(f"    {vfo} current: ch='{ch}'", console=True)

        if not ch or not ch.isdigit():
            self._logmsg(f"    {vfo}: no channel data, skipping (VFO mode or radio unresponsive)")
            return False

        if int(ch) == target_int:
            self._logmsg(f"    {vfo} already on channel {target_int}", console=True)
            return True

        start_channel = ch

        # Step through channels
        no_response_count = 0
        for i in range(200):
            if self._stop:
                self._logmsg(f"    Aborted")
                return False

            # Save channel text before step to detect if radio responded
            pre_step = self._channel_text.get(vfo, '').strip()

            if vfo == self.LEFT:
                self._send_button([0x02], 2, 3)  # L_DIAL_RIGHT
            else:
                self._send_button([0x82], 2, 3)  # R_DIAL_RIGHT
            time.sleep(0.05)
            self._send_button_release()
            time.sleep(0.15)
            self._drain(0.3)

            # Step response maps to _channel_text[vfo] (correct — opposite of press)
            ch = self._channel_text.get(vfo, '').strip()
            self._logmsg(f"    {vfo} step {i+1}: ch='{ch}'")

            # Detect no response — channel should always change on a step
            if ch == pre_step:
                no_response_count += 1
                if no_response_count <= 3:
                    self._logmsg(f"    Step {i+1}: no response (ch unchanged '{ch}'), retrying...")
                    time.sleep(0.3)
                    self._drain(0.3)
                    continue
                else:
                    self._logmsg(f"    Radio unresponsive after {no_response_count} retries, aborting")
                    return False
            else:
                no_response_count = 0  # reset on successful response

            if ch.isdigit() and int(ch) == target_int:
                self._logmsg(f"    Channel set to {target_int} (stepped {i+1})")
                return True
            if start_channel and ch == start_channel and i > 0:
                self._logmsg(f"    Channel {target_int} not found (looped around after {i+1} steps)")
                return False

        self._logmsg(f"    Channel {target_int} not found (max iterations)")
        return False

    def set_volume(self, vfo, target_level):
        """Set volume on specified VFO. level=0-100.
        Sends a minor nudge first to wake the radio's volume control,
        then sets the actual target value."""
        target_level = max(0, min(100, target_level))
        vfo_letter = 'LEFT' if vfo == self.LEFT else 'RIGHT'
        self._logmsg(f"  CAT: Setting {vfo} volume to {target_level}%...")

        # Nudge: set volume slightly off-target to force the radio to
        # actually process a volume change (avoids stale/assumed values)
        nudge = target_level + 1 if target_level < 100 else target_level - 1
        resp = self._send_cmd(f"!vol {vfo_letter} {nudge}")
        self._logmsg(f"    Volume nudge: {nudge}", console=False)
        time.sleep(0.1)

        # Now set the real target
        resp = self._send_cmd(f"!vol {vfo_letter} {target_level}")
        self._logmsg(f"    Volume set to {target_level}%")
        time.sleep(0.05)

        self._volume[vfo] = target_level
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
        self._logmsg(f"    Current power: {current}", console=False)
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
        """Run full radio setup sequence from config. Prints concise summary."""
        left_ch = getattr(config, 'CAT_LEFT_CHANNEL', -1)
        right_ch = getattr(config, 'CAT_RIGHT_CHANNEL', -1)
        left_vol = getattr(config, 'CAT_LEFT_VOLUME', -1)
        right_vol = getattr(config, 'CAT_RIGHT_VOLUME', -1)
        left_pwr = str(getattr(config, 'CAT_LEFT_POWER', '')).strip()
        right_pwr = str(getattr(config, 'CAT_RIGHT_POWER', '')).strip()

        # Set RTS to USB controlled once (no restore — simpler, avoids serial disruption)
        print("  CAT: Setting RTS to USB Controlled...")
        self.set_rts(True)
        time.sleep(0.5)

        # Build list of tasks to run (default args capture values, not references)
        tasks = []
        if int(left_ch) != -1:
            tasks.append(('L ch', lambda c=int(left_ch): self.set_channel(self.LEFT, c)))
        if int(right_ch) != -1:
            tasks.append(('R ch', lambda c=int(right_ch): self.set_channel(self.RIGHT, c)))
        if int(left_vol) != -1:
            tasks.append(('L vol', lambda v=int(left_vol): self.set_volume(self.LEFT, v)))
        if int(right_vol) != -1:
            tasks.append(('R vol', lambda v=int(right_vol): self.set_volume(self.RIGHT, v)))
        if left_pwr:
            tasks.append(('L pwr', lambda p=left_pwr: self.set_power(self.LEFT, p)))
        if right_pwr:
            tasks.append(('R pwr', lambda p=right_pwr: self.set_power(self.RIGHT, p)))

        if not tasks:
            print("  CAT: No setup tasks configured")
            return

        print(f"  CAT: Sending {len(tasks)} setup commands...")
        results = []
        for name, func in tasks:
            if self._stop:
                results.append((name, 'interrupted'))
                break
            try:
                ok = func()
                results.append((name, 'ok' if ok else 'failed'))
            except Exception as e:
                results.append((name, f'error: {e}'))
                print(f"  CAT: {name} error: {e}")

        # Send final button release, re-confirm RTS, and settle — rapid setup
        # commands can leave the radio serial in a state where subsequent commands
        # are ignored until RTS is reasserted
        self._send_button_release()
        time.sleep(0.3)
        self.set_rts(True)
        time.sleep(0.3)
        self._drain(0.5)

        # Print concise summary
        ok_count = sum(1 for _, r in results if r == 'ok')
        summary_parts = [f"{name}={status}" for name, status in results]
        if ok_count == len(results):
            print(f"  CAT: Setup complete ({ok_count}/{len(tasks)} ok)")
        else:
            print(f"  CAT: Setup done ({ok_count}/{len(tasks)} ok) — {', '.join(summary_parts)}")


class DDNSUpdater:
    """Dynamic DNS updater (No-IP compatible protocol).

    Runs a background thread that periodically updates a DDNS hostname
    with the machine's current public IP via the No-IP update API.
    """

    def __init__(self, config):
        self.config = config
        self._stop = False
        self._thread = None
        self._last_ip = None
        self._last_status = None   # 'good', 'nochg', or error string
        self._last_update = 0      # time.time() of last update attempt

    def start(self):
        username = str(getattr(self.config, 'DDNS_USERNAME', '') or '')
        password = str(getattr(self.config, 'DDNS_PASSWORD', '') or '')
        hostname = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '')
        if not username or not password or not hostname:
            print("  [DDNS] Missing username, password, or hostname — skipping")
            return
        self._stop = False
        self._thread = threading.Thread(target=self._update_loop, daemon=True,
                                        name="ddns-updater")
        self._thread.start()
        print(f"  [DDNS] Updater started for {hostname} "
              f"(every {self.config.DDNS_UPDATE_INTERVAL}s)")

    def stop(self):
        self._stop = True

    def get_status(self):
        """Return compact status string for the status bar."""
        if self._last_ip and self._last_status in ('good', 'nochg'):
            return self._last_ip
        elif self._last_status:
            return 'ERR'
        return '...'

    def _update_loop(self):
        import urllib.request
        import base64

        username = str(self.config.DDNS_USERNAME)
        password = str(self.config.DDNS_PASSWORD)
        hostname = str(self.config.DDNS_HOSTNAME)
        url_base = str(getattr(self.config, 'DDNS_UPDATE_URL',
                                'https://dynupdate.no-ip.com/nic/update') or
                       'https://dynupdate.no-ip.com/nic/update')
        interval = max(60, int(getattr(self.config, 'DDNS_UPDATE_INTERVAL', 300)))
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()

        while not self._stop:
            try:
                url = f"{url_base}?hostname={hostname}"
                req = urllib.request.Request(url)
                req.add_header('Authorization', f'Basic {creds}')
                req.add_header('User-Agent', 'RadioGateway/1.0 radio_gateway.py')
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = resp.read().decode().strip()
            except Exception as e:
                result = f"error: {e}"

            # Parse response: "good IP", "nochg IP", or error codes
            parts = result.split()
            code = parts[0] if parts else result
            ip = parts[1] if len(parts) > 1 else ''

            self._last_update = time.time()
            self._last_status = code
            if code in ('good', 'nochg'):
                if code == 'good' or self._last_ip is None:
                    print(f"\n[DDNS] {hostname} → {ip}")
                self._last_ip = ip
            else:
                print(f"\n[DDNS] Update failed: {result}")

            # Sleep in small increments so stop is responsive
            for _ in range(int(interval)):
                if self._stop:
                    return
                time.sleep(1)


class EmailNotifier:
    """Gmail SMTP email sender for gateway notifications."""

    def __init__(self, config, gateway=None):
        self.config = config
        self.gateway = gateway
        self._address = str(getattr(config, 'EMAIL_ADDRESS', '') or '').strip()
        self._password = str(getattr(config, 'EMAIL_APP_PASSWORD', '') or '').strip()
        self._recipient = str(getattr(config, 'EMAIL_RECIPIENT', '') or '').strip() or self._address

    def is_configured(self):
        return bool(self._address and self._password)

    def send(self, subject, body):
        """Send an email. Returns True on success."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        if not self.is_configured():
            print("  [Email] Not configured (missing EMAIL_ADDRESS or EMAIL_APP_PASSWORD)")
            return False

        msg = MIMEMultipart('alternative')
        msg['From'] = self._address
        msg['To'] = self._recipient
        msg['Subject'] = subject

        # Plain text version
        msg.attach(MIMEText(body, 'plain'))

        # HTML version (makes URLs clickable)
        # Linkify URLs BEFORE inserting <br> tags, otherwise <br> gets captured in the URL
        import re
        html_body = re.sub(r'(https?://\S+)', r'<a href="\1">\1</a>', body)
        html_body = html_body.replace('\n', '<br>\n')
        msg.attach(MIMEText(f'<html><body style="font-family:monospace;font-size:14px">{html_body}</body></html>', 'html'))

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
                server.login(self._address, self._password)
                server.sendmail(self._address, self._recipient, msg.as_string())
            print(f"  [Email] Sent to {self._recipient}: {subject}")
            return True
        except Exception as e:
            print(f"  [Email] Failed: {e}")
            return False

    def send_startup_status(self):
        """Send a status email with gateway info and tunnel URL."""
        import datetime
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        lines = [f"Radio Gateway started at {now}", ""]

        # Tunnel URL
        if self.gateway and self.gateway.cloudflare_tunnel:
            url = self.gateway.cloudflare_tunnel.get_url()
            if url:
                lines.append(f"Dashboard: {url}/dashboard")
                lines.append(f"Config:    {url}")
                lines.append("")

        # LAN link
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            lan_ip = s.getsockname()[0]
            s.close()
            lines.append(f"LAN:       http://{lan_ip}:{port}/dashboard")
        except Exception:
            pass
        lines.append(f"Local:     http://localhost:{port}/dashboard")
        lines.append("")

        # Mumble server
        mumble_srv = str(getattr(self.config, 'MUMBLE_SERVER', '') or '')
        mumble_port = int(getattr(self.config, 'MUMBLE_PORT', 64738))
        if mumble_srv:
            lines.append(f"Mumble:    {mumble_srv}:{mumble_port}")

        # DDNS
        ddns_host = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '')
        if ddns_host:
            lines.append(f"DDNS:      {ddns_host}")

        lines.append("")
        lines.append("-- Radio Gateway")

        hostname = ''
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            pass

        subject = f"Gateway Online{' — ' + hostname if hostname else ''}"
        self.send(subject, '\n'.join(lines))

    def send_startup_delayed(self):
        """Wait for tunnel URL (up to 15s) then send startup email."""
        def _delayed():
            # Wait for tunnel URL if tunnel is enabled
            if self.gateway and self.gateway.cloudflare_tunnel:
                for _ in range(15):
                    if self.gateway.cloudflare_tunnel.get_url():
                        break
                    time.sleep(1)
            self.send_startup_status()

        t = threading.Thread(target=_delayed, daemon=True, name="email-startup")
        t.start()


class CloudflareTunnel:
    """Cloudflare quick tunnel — free public HTTPS access with no port forwarding.

    Launches `cloudflared tunnel --url http://localhost:PORT` as a subprocess.
    Parses the assigned *.trycloudflare.com URL from stderr.

    The tunnel process is kept alive across gateway restarts so the URL stays
    stable. On start(), if an existing cloudflared is already running for the
    same port, we adopt it and read the cached URL from /tmp/cloudflare_tunnel_url.
    """

    URL_FILE = '/tmp/cloudflare_tunnel_url'

    def __init__(self, config):
        self.config = config
        self._process = None  # only set if WE launched it
        self._url = None
        self._thread = None
        self._adopted = False  # True if we reused an existing process

    def start(self):
        import subprocess
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))

        # Check if cloudflared is already running
        try:
            result = subprocess.run(
                ['pgrep', '-x', 'cloudflared'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Existing cloudflared found — adopt it
                self._adopted = True
                try:
                    with open(self.URL_FILE, 'r') as f:
                        self._url = f.read().strip()
                except FileNotFoundError:
                    pass
                if self._url:
                    print(f"  [Tunnel] Reusing existing cloudflared (URL: {self._url})")
                else:
                    print(f"  [Tunnel] Reusing existing cloudflared (URL not yet cached)")
                return
        except Exception:
            pass

        # No existing process — launch a new one
        try:
            self._process = subprocess.Popen(
                ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except FileNotFoundError:
            print("  [Tunnel] cloudflared not found — install with: sudo pacman -S cloudflared")
            return
        except Exception as e:
            print(f"  [Tunnel] Failed to start: {e}")
            return

        # Read stderr in background to capture the URL
        self._thread = threading.Thread(target=self._read_output, daemon=True,
                                        name="cf-tunnel")
        self._thread.start()
        print(f"  [Tunnel] Starting Cloudflare tunnel for port {port}...")

    def stop(self):
        # Don't kill cloudflared — leave it running so the URL survives gateway restarts
        pass

    def get_url(self):
        return self._url

    def _read_output(self):
        """Parse cloudflared stderr to find the assigned URL."""
        import re
        try:
            for line in self._process.stderr:
                line = line.decode('utf-8', errors='replace').strip()
                # Look for the trycloudflare.com URL
                match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
                if match:
                    self._url = match.group(1)
                    # Cache URL so restarts can read it
                    try:
                        with open(self.URL_FILE, 'w') as f:
                            f.write(self._url)
                    except Exception:
                        pass
                    print(f"  [Tunnel] Public URL: {self._url}")
                if self._process.poll() is not None:
                    break
        except Exception:
            pass
        if self._process and self._process.poll() is not None and self._process.returncode != 0:
            print(f"\n[Tunnel] cloudflared exited (code {self._process.returncode})")


class SmartAnnouncementManager:
    """AI-powered announcements with pluggable backend (Claude or Gemini).

    Reads SMART_ANNOUNCE_N entries from config. Each entry has:
        interval (seconds), voice (1-9), target_seconds (max speech length), {prompt}

    The selected AI backend composes a spoken message based on the prompt.
    The result is fed to the existing gTTS pipeline for broadcast.
    """

    # ~2.5 words/second for gTTS speech
    WORDS_PER_SECOND = 2.5

    def __init__(self, gateway):
        self.gateway = gateway
        self.config = gateway.config
        self._entries = []  # list of dicts: {id, interval, voice, target_secs, prompt, last_run}
        self._thread = None
        self._stop = False
        self._lock = threading.Lock()  # protects _entries mutations
        self._client = None  # AI client instance (anthropic.Anthropic or genai model)
        self._backend = str(getattr(self.config, 'SMART_ANNOUNCE_AI_BACKEND', 'duckduckgo')).strip().lower()
        self._activity = {}  # {entry_id: {'step': str, 'time': float}} — live status for web UI
        self._parse_entries()

    def _parse_entries(self):
        """Find all SMART_ANNOUNCE_N entries in config."""
        for i in range(1, 20):
            key = f'SMART_ANNOUNCE_{i}'
            raw = getattr(self.config, key, None)
            if raw is None:
                continue
            try:
                entry = self._parse_entry(i, str(raw))
                if entry:
                    self._entries.append(entry)
            except Exception as e:
                print(f"  [SmartAnnounce] Error parsing {key}: {e}")

    def _parse_entry(self, entry_id, raw):
        """Parse: interval, voice, target_seconds, {prompt text here}"""
        # Find the prompt in braces
        brace_start = raw.find('{')
        brace_end = raw.rfind('}')
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            print(f"  [SmartAnnounce] Entry {entry_id}: missing {{prompt}} in braces")
            return None
        prompt = raw[brace_start + 1:brace_end].strip()
        prefix = raw[:brace_start]
        parts = [p.strip() for p in prefix.split(',') if p.strip()]
        if len(parts) < 3:
            print(f"  [SmartAnnounce] Entry {entry_id}: need interval, voice, target_seconds before {{prompt}}")
            return None
        return {
            'id': entry_id,
            'interval': int(parts[0]),
            'voice': int(parts[1]),
            'target_secs': min(int(parts[2]), 60),
            'prompt': prompt,
            'last_run': 0,
        }

    def _init_ollama(self, verbose=True):
        """Detect Ollama and select a model. Sets _ollama_available and _ollama_model."""
        self._ollama_available = False
        configured_model = str(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_MODEL', '') or '').strip()
        try:
            import urllib.request, json
            req = urllib.request.Request('http://127.0.0.1:11434/api/tags', method='GET')
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                data = json.loads(resp.read())
                models = [m.get('name', '') for m in data.get('models', [])]
                if configured_model:
                    if configured_model in models or any(m.startswith(configured_model) for m in models):
                        self._ollama_model = configured_model
                        self._ollama_available = True
                    elif verbose:
                        print(f"  [SmartAnnounce] Ollama model '{configured_model}' not found (available: {', '.join(models)})")
                        print(f"    Pull it with: ollama pull {configured_model}")
                elif models:
                    self._ollama_model = models[0]
                    self._ollama_available = True
                elif verbose:
                    print("  [SmartAnnounce] Ollama running but no models pulled")
                    print("    Pull a model with: ollama pull llama3.1:8b")
        except Exception:
            pass
        if self._ollama_available and verbose:
            print(f"  [SmartAnnounce] Ollama — using model '{self._ollama_model}'")

    def _init_claude(self):
        """Initialize Claude (Anthropic) backend."""
        api_key = getattr(self.config, 'SMART_ANNOUNCE_API_KEY', '')
        if not api_key:
            print("  [SmartAnnounce] No API key configured (SMART_ANNOUNCE_API_KEY)")
            return False
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            return True
        except ImportError:
            print("  [SmartAnnounce] anthropic package not installed")
            print("    Install with: pip3 install anthropic --break-system-packages")
            return False

    def _init_gemini(self):
        """Initialize Google Gemini backend."""
        api_key = getattr(self.config, 'SMART_ANNOUNCE_GEMINI_API_KEY', '')
        if not api_key:
            print("  [SmartAnnounce] No Gemini API key configured (SMART_ANNOUNCE_GEMINI_API_KEY)")
            return False
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            return True
        except ImportError:
            print("  [SmartAnnounce] google-genai package not installed")
            print("    Install with: pip3 install google-genai --break-system-packages")
            return False

    def _init_duckduckgo(self):
        """Initialize DuckDuckGo search + Ollama backend (free, no API key needed).
        Uses ddgs for web search and Ollama (if running) for natural speech composition.
        Falls back to reading search snippets directly if Ollama is unavailable."""
        try:
            from ddgs import DDGS
            self._client = DDGS()
        except ImportError:
            try:
                from duckduckgo_search import DDGS
                self._client = DDGS()
            except ImportError:
                print("  [SmartAnnounce] ddgs package not installed")
                print("    Install with: pip3 install ddgs --break-system-packages")
                return False
        self._init_ollama()
        if not self._ollama_available:
            print("  [SmartAnnounce] Ollama not available — using search snippets directly")
            print("    For better results, install Ollama: curl -fsSL https://ollama.com/install.sh | sh")
        return True

    def _init_google_scrape(self):
        """Initialize Google AI Overview scrape backend.
        Uses xdotool to drive the user's real Firefox browser on the desktop,
        performs a Google search, clicks 'Show more' to expand the AI Overview,
        then copies the page text and extracts the AI Overview section.
        Requires: xdotool, xclip, Firefox running on DISPLAY=:0."""
        import shutil, subprocess
        missing = []
        for tool in ('xdotool', 'xclip'):
            if not shutil.which(tool):
                missing.append(tool)
        if missing:
            print(f"  [SmartAnnounce] google-scrape requires: {', '.join(missing)}")
            print(f"    Install with: sudo pacman -S {' '.join(missing)}")
            return False
        # Ensure DISPLAY is set (needed for xdotool even if started from a non-GUI shell)
        if not os.environ.get('DISPLAY'):
            os.environ['DISPLAY'] = ':0'
            print("  [SmartAnnounce] Set DISPLAY=:0")
        # Check Firefox at init (non-fatal — it may start later)
        try:
            result = subprocess.run(['xdotool', 'search', '--name', 'Mozilla Firefox'],
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':0')})
            windows = [w.strip() for w in result.stdout.strip().split('\n') if w.strip()]
            if windows:
                print(f"  [SmartAnnounce] Firefox detected ({len(windows)} windows)")
            else:
                print("  [SmartAnnounce] Firefox not detected yet — will check again at announcement time")
        except Exception as e:
            print(f"  [SmartAnnounce] Cannot check Firefox: {e} — will retry at announcement time")
        self._init_ollama()
        if not self._ollama_available:
            print("  [SmartAnnounce] Ollama not available — AI Overview text sent directly to TTS")
        self._client = True  # marker that backend is ready
        return True

    def start(self):
        """Start the background timer thread."""
        if not self._entries:
            return
        try:
            if self._backend == 'duckduckgo':
                ok = self._init_duckduckgo()
            elif self._backend == 'google-scrape':
                ok = self._init_google_scrape()
            elif self._backend == 'gemini':
                ok = self._init_gemini()
            else:
                ok = self._init_claude()
            if not ok:
                return
            print(f"  [SmartAnnounce] Backend: {self._backend}")
            _sa_start = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
            _sa_end = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
            if _sa_start and _sa_end:
                print(f"  [SmartAnnounce] Time window: {_sa_start}-{_sa_end}")
            else:
                print(f"  [SmartAnnounce] Time window: unrestricted")
            print(f"  [SmartAnnounce] Initialized with {len(self._entries)} scheduled announcement(s)")
            for e in self._entries:
                print(f"    #{e['id']}: every {e['interval']}s, voice {e['voice']}, "
                      f"~{e['target_secs']}s, prompt: {e['prompt'][:60]}...")
        except Exception as e:
            print(f"  [SmartAnnounce] Init error: {e}")
            return

        self._stop = False
        self._thread = threading.Thread(target=self._timer_loop, daemon=True,
                                        name="SmartAnnounce")
        self._thread.start()

    def get_countdowns(self):
        """Return list of (id, seconds_remaining) for each entry."""
        now = time.time()
        result = []
        with self._lock:
            for e in self._entries:
                remaining = max(0, e['interval'] - (now - e['last_run']))
                result.append((e['id'], int(remaining)))
        return result

    def stop(self):
        """Stop the timer thread."""
        self._stop = True

    def _in_time_window(self):
        """Check if current time is within the configured start/end window.
        Returns True if no window configured or current time is inside it."""
        import datetime
        start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
        end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
        if not start_str or not end_str:
            return True
        try:
            now = datetime.datetime.now().time()
            sh, sm = map(int, start_str.split(':'))
            eh, em = map(int, end_str.split(':'))
            start = datetime.time(sh, sm)
            end = datetime.time(eh, em)
            if start <= end:
                return start <= now <= end
            else:
                # Overnight wrap (e.g. 22:00 - 06:00)
                return now >= start or now <= end
        except (ValueError, AttributeError):
            return True  # invalid format → don't restrict

    def _timer_loop(self):
        """Check intervals and trigger announcements."""
        # Stagger first runs: don't all fire at t=0
        now = time.time()
        with self._lock:
            for e in self._entries:
                e['last_run'] = now
        _was_in_window = self._in_time_window()
        if _was_in_window:
            start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
            end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
            if start_str and end_str:
                print(f"[SmartAnnounce] Active — time window {start_str}-{end_str}")
        while not self._stop:
            time.sleep(5)
            _in_window = self._in_time_window()
            if _in_window != _was_in_window:
                start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
                end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
                if _in_window:
                    print(f"\n[SmartAnnounce] Active — time window {start_str}-{end_str} started")
                else:
                    print(f"\n[SmartAnnounce] Paused — time window {start_str}-{end_str} ended")
                _was_in_window = _in_window
            if not _in_window:
                continue
            now = time.time()
            with self._lock:
                due = [(e, now) for e in self._entries if now - e['last_run'] >= e['interval']]
                for e, t in due:
                    e['last_run'] = t
            for e, _ in due:
                try:
                    self._run_announcement(e)
                except Exception as ex:
                    print(f"\n[SmartAnnounce] Error on #{e['id']}: {ex}")

    def _set_activity(self, entry_id, step):
        """Update live activity status for web UI."""
        self._activity[entry_id] = {'step': step, 'time': time.time()}

    def _clear_activity(self, entry_id):
        """Clear activity status after completion."""
        self._activity.pop(entry_id, None)

    def get_activity(self):
        """Return current activity dict for all entries. Auto-expires after 120s."""
        now = time.time()
        expired = [k for k, v in self._activity.items() if now - v['time'] > 120]
        for k in expired:
            del self._activity[k]
        return {k: v['step'] for k, v in self._activity.items()}

    def _run_announcement(self, entry, manual=False):
        """Call AI API, get text, speak it. manual=True skips time window check."""
        eid = entry['id']
        try:
            if not self._client:
                self._set_activity(eid, 'No API client')
                print(f"\n[SmartAnnounce] #{eid}: No API client (missing key?)")
                return
            if not manual and not self._in_time_window():
                print(f"\n[SmartAnnounce] #{eid}: Skipped — outside time window")
                return
        except Exception as e:
            self._set_activity(eid, f'Error: {e}')
            print(f"\n[SmartAnnounce] #{eid}: Pre-check error: {e}")
            return

        max_words = int(entry['target_secs'] * self.WORDS_PER_SECOND)
        system_prompt = (
            f"Summarize the search results as spoken text in {max_words} words or fewer. "
            "Start directly with facts. No greetings, no sign-offs, no intros, no station names, "
            "no website names. Write numbers as words. Only include facts from the provided text."
        )

        try:
            self._set_activity(eid, f'Searching ({self._backend})')
            if self._backend == 'duckduckgo':
                text = self._call_duckduckgo(entry, system_prompt, max_words)
            elif self._backend == 'google-scrape':
                text = self._call_google_scrape(entry, system_prompt, max_words)
            elif self._backend == 'gemini':
                text = self._call_gemini(entry, system_prompt, max_words)
            else:
                text = self._call_claude(entry, system_prompt, max_words)
            if not text:
                self._set_activity(eid, 'No results')
                return

            # Truncate if over word limit (safety net)
            words = text.split()
            if len(words) > max_words + 10:
                text = ' '.join(words[:max_words])

            # Add optional top/tail text with pauses
            top_text = str(getattr(self.config, 'SMART_ANNOUNCE_TOP_TEXT', '') or '').strip()
            tail_text = str(getattr(self.config, 'SMART_ANNOUNCE_TAIL_TEXT', '') or '').strip()
            if top_text:
                text = f"{top_text} ... {text}"
            if tail_text:
                text = f"{text} ... {tail_text}"

            print(f"[SmartAnnounce] #{entry['id']}: ── SENDING TO gTTS ({len(text.split())} words, voice {entry['voice']}) ──")
            print(f"  {text}")

            # Wait for radio to be free before transmitting
            self._set_activity(eid, 'Waiting for radio')
            for attempt in range(100):
                vad_busy = getattr(self.gateway, 'vad_active', False)
                pb_busy = (self.gateway.playback_source and
                           self.gateway.playback_source.current_file)
                if not vad_busy and not pb_busy:
                    break
                if attempt == 0:
                    print(f"[SmartAnnounce] #{entry['id']}: Waiting for radio to be free...")
                time.sleep(5)
            else:
                self._set_activity(eid, 'Dropped (radio busy)')
                print(f"[SmartAnnounce] #{entry['id']}: Radio busy too long, dropping announcement")
                return

            # Playback triggers AIOC PTT automatically via the audio loop
            # (set_ptt_state writes HID GPIO to key the radio).
            # Auto-set RTS to Radio Controlled for TX, restore after.
            _rts_saved = None
            _cat = getattr(self.gateway, 'cat_client', None)
            print(f"[SmartAnnounce] #{eid}: CAT client: {_cat}, RTS: {_cat.get_rts() if _cat else 'N/A'}")
            if _cat:
                _rts_saved = _cat.get_rts()
                if _rts_saved is None or _rts_saved is True:
                    try:
                        _cat._pause_drain()
                        try:
                            _cat.set_rts(False)  # Radio Controlled
                            time.sleep(0.3)
                            _cat._drain(0.5)
                        finally:
                            _cat._drain_paused = False
                        print(f"[SmartAnnounce] #{eid}: RTS changed to Radio Controlled (was {'USB' if _rts_saved else 'unknown'})")
                    except Exception as _re:
                        print(f"[SmartAnnounce] #{eid}: RTS set failed: {_re}")
                else:
                    print(f"[SmartAnnounce] #{eid}: RTS already Radio Controlled, no change needed")

            self._set_activity(eid, f'Speaking ({len(text.split())}w)')
            self.gateway.speak_text(text, voice=entry['voice'])

            # Wait for playback to finish before restoring RTS
            for _w in range(600):
                if not (self.gateway.playback_source and self.gateway.playback_source.current_file):
                    break
                time.sleep(0.1)

            if _cat and _rts_saved is not None and _rts_saved is not False:
                try:
                    _cat.set_rts(_rts_saved)
                    print(f"[SmartAnnounce] #{eid}: RTS restored to {'USB' if _rts_saved else 'Radio'} Controlled")
                    # Refresh display after RTS change to prevent VFO display corruption
                    time.sleep(0.3)
                    _cat._pause_drain()
                    try:
                        _cat._send_button([0x00, 0x25], 3, 5)
                        time.sleep(0.15)
                        _cat._send_button_release()
                        time.sleep(0.3)
                        _cat._drain(0.5)
                        _cat._send_button([0x00, 0xA5], 3, 5)
                        time.sleep(0.15)
                        _cat._send_button_release()
                        time.sleep(0.3)
                        _cat._drain(0.5)
                    finally:
                        _cat._drain_paused = False
                except Exception as _re:
                    print(f"[SmartAnnounce] #{eid}: RTS restore failed: {_re}")

            self._set_activity(eid, 'Done')

        except Exception as e:
            self._set_activity(eid, f'Error: {e}')
            print(f"\n[SmartAnnounce] #{entry['id']}: API error: {e}")

    def _call_claude(self, entry, system_prompt, max_words):
        """Call Claude API with web search, return announcement text or None."""
        print(f"\n[SmartAnnounce] #{entry['id']}: Calling Claude API...")
        response = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": entry['prompt']}],
        )
        text_parts = []
        for block in response.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)
        text = ' '.join(text_parts).strip()
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Claude")
            return None
        return text

    def _call_gemini(self, entry, system_prompt, max_words):
        """Call Gemini API with Google Search grounding, return announcement text or None."""
        from google.genai import types
        print(f"\n[SmartAnnounce] #{entry['id']}: Calling Gemini API (Google Search)...")
        google_search_tool = types.Tool(google_search=types.GoogleSearch())
        response = self._client.models.generate_content(
            model="gemini-2.0-flash",
            contents=entry['prompt'],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[google_search_tool],
                max_output_tokens=1024,
            ),
        )
        text = response.text.strip() if response.text else ''
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Gemini")
            return None
        return text

    def _call_duckduckgo(self, entry, system_prompt, max_words):
        """Free web search via DuckDuckGo + Ollama for speech composition.
        Falls back to formatted search snippets if Ollama is unavailable."""
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        import re

        search_query = entry['prompt']
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        print(f"\n[SmartAnnounce] #{entry['id']}: Searching: {search_query}")
        ddgs = DDGS()

        # Use both web search and news search for richer results
        web_results = []
        news_results = []
        try:
            web_results = ddgs.text(search_query, max_results=5) or []
        except Exception as e:
            print(f"[SmartAnnounce] #{entry['id']}: web search error: {e}")
        try:
            news_results = ddgs.news(search_query, max_results=5) or []
        except Exception as e:
            print(f"[SmartAnnounce] #{entry['id']}: news search error: {e}")

        if not web_results and not news_results:
            print(f"[SmartAnnounce] #{entry['id']}: no search results")
            return None

        # Build context — news results first (more relevant for current events)
        context_parts = []
        if news_results:
            context_parts.append("NEWS HEADLINES:")
            for r in news_results:
                context_parts.append(f"- {r.get('title', '')}: {r.get('body', '')}")
        if web_results:
            context_parts.append("WEB RESULTS:")
            for r in web_results:
                context_parts.append(f"- {r.get('title', '')}: {r.get('body', '')}")
        search_context = "\n".join(context_parts)

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── SEARCH RESULTS ({len(news_results)} news, {len(web_results)} web) ──")
            if news_results:
                print(f"  NEWS:")
                for r in news_results:
                    print(f"    {r.get('title', '')}: {r.get('body', '')[:120]}")
            if web_results:
                print(f"  WEB:")
                for r in web_results:
                    print(f"    {r.get('title', '')}: {r.get('body', '')[:120]}")

        # If Ollama is available, use it to compose natural speech
        if getattr(self, '_ollama_available', False):
            return self._ollama_compose(entry, system_prompt, max_words, search_context)

        # Fallback: format search snippets directly for TTS
        print(f"[SmartAnnounce] #{entry['id']}: Composing from search snippets (no Ollama)...")
        snippets = []
        for r in (news_results + web_results)[:3]:
            body = r.get('body', '').strip()
            if body:
                # Clean up for speech: remove URLs, extra whitespace
                body = re.sub(r'https?://\S+', '', body)
                body = re.sub(r'\s+', ' ', body).strip()
                snippets.append(body)
        text = '. '.join(snippets)
        # Trim to word limit
        words = text.split()
        if len(words) > max_words:
            text = ' '.join(words[:max_words])
        return text if text else None

    def _scrape_google_ai_overview(self, search_query):
        """Drive the real Firefox browser via xdotool to Google search and extract AI Overview.
        Returns the AI Overview text or None."""
        import subprocess, urllib.parse
        display_env = {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':0')}

        def xdo(*args, timeout=5):
            return subprocess.run(['xdotool'] + list(args),
                                  capture_output=True, text=True, timeout=timeout, env=display_env)

        def xclip_get():
            r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                               capture_output=True, text=True, timeout=5, env=display_env)
            return r.stdout if r.returncode == 0 else ''

        # Find the main Firefox window (largest one with "Mozilla Firefox" in title)
        def _find_firefox_window():
            """Return (wid, area) of the largest Firefox window, or (None, 0)."""
            r = xdo('search', '--name', 'Mozilla Firefox')
            if r.returncode != 0 or not r.stdout.strip():
                return None, 0
            wids = [w.strip() for w in r.stdout.strip().split('\n') if w.strip()]
            best_wid, best_area = None, 0
            for wid in wids:
                try:
                    geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                                         capture_output=True, text=True, timeout=3, env=display_env)
                    w = h = 0
                    for line in geo.stdout.strip().split('\n'):
                        if line.startswith('WIDTH='): w = int(line.split('=')[1])
                        if line.startswith('HEIGHT='): h = int(line.split('=')[1])
                    area = w * h
                    if area > best_area:
                        best_area = area
                        best_wid = wid
                except Exception:
                    continue
            return best_wid, best_area

        best_wid, best_area = _find_firefox_window()

        if not best_wid or best_area < 10000:
            # No usable Firefox window — try to launch it
            launched = False
            if best_wid is None:
                print(f"[SmartAnnounce] google-scrape: Firefox not running, launching...")
                try:
                    subprocess.Popen(['firefox'], env=display_env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launched = True
                except Exception as e:
                    print(f"[SmartAnnounce] google-scrape: failed to launch Firefox: {e}")
                    return None
            else:
                print(f"[SmartAnnounce] google-scrape: Firefox window too small, waiting for it to load...")
                launched = True

            if launched:
                # Wait up to 30s for a fully-rendered window (area >= 10000)
                for _wait in range(30):
                    time.sleep(1)
                    best_wid, best_area = _find_firefox_window()
                    if best_wid and best_area >= 10000:
                        print(f"[SmartAnnounce] google-scrape: Firefox ready after {_wait + 1}s")
                        # Extra settle time for freshly launched Firefox to finish
                        # loading its home page so it doesn't interfere with navigation
                        time.sleep(5)
                        # Re-find window in case IDs changed during load
                        best_wid, best_area = _find_firefox_window()
                        break
                else:
                    print(f"[SmartAnnounce] google-scrape: Firefox not ready within 30s")
                    return None

        # Save currently active window to restore later
        active_result = xdo('getactivewindow')
        prev_wid = active_result.stdout.strip() if active_result.returncode == 0 else None

        try:
            # Activate Firefox
            xdo('windowactivate', '--sync', best_wid)
            time.sleep(0.2)

            # Navigate via URL bar (Ctrl+L) — dev console doesn't work reliably
            # when Firefox is showing a page with keyboard event handlers.
            # Use udm=50 to go directly to Google AI Mode (no JS click needed).
            encoded_q = urllib.parse.quote_plus(search_query)
            url = f'https://www.google.com/search?q={encoded_q}&hl=en&udm=50'
            print(f"[SmartAnnounce] google-scrape: navigating Firefox to AI Mode...")
            xdo('key', 'ctrl+l')
            time.sleep(0.3)
            xdo('key', 'ctrl+a')
            time.sleep(0.1)
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=url.encode(), env=display_env, timeout=3)
            xdo('key', 'ctrl+v')
            time.sleep(0.1)
            xdo('key', 'Return')
            print(f"[SmartAnnounce] google-scrape: waiting for AI Mode response...")
            time.sleep(10)

            # Re-find and re-activate Firefox (in case an ad/popup stole focus)
            best_wid2, _ = _find_firefox_window()
            if not best_wid2:
                best_wid2 = best_wid
            xdo('windowactivate', '--sync', best_wid2)
            time.sleep(0.3)

            # Clear clipboard so stale data from prior scrape can't leak through
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=b'', env=display_env, timeout=3)

            # Click near the top-left of the page content (avoids ads which are
            # typically in the center/right) to focus the page, then select all + copy
            xdo('mousemove', '--window', best_wid2, '150', '300')
            time.sleep(0.1)
            xdo('click', '1')
            time.sleep(0.2)
            xdo('key', 'ctrl+a')
            time.sleep(0.2)
            xdo('key', 'ctrl+c')
            time.sleep(0.3)

            # Get clipboard
            page_text = xclip_get()
            if not page_text:
                print(f"[SmartAnnounce] google-scrape: clipboard empty")
                return None

            # Extract AI content — two formats:
            # 1. "AI Overview" section in regular search results
            # 2. AI Mode page (content starts after search query, ends at sources/footer)
            import re
            ai_start = page_text.find('AI Overview')
            if ai_start != -1:
                # Format 1: regular AI Overview
                ai_text = page_text[ai_start:]
                for end_marker in ['Dive deeper in AI Mode', 'AI can make mistakes', 'Dive deeper']:
                    end_pos = ai_text.find(end_marker)
                    if end_pos > 0:
                        ai_text = ai_text[:end_pos]
                        break
                ai_text = ai_text.replace('AI Overview', '', 1).strip()
                ai_text = re.sub(r'^\+\d+\s*', '', ai_text).strip()
            else:
                # Format 2: AI Mode — content is between the search query and footer/sources
                # Page starts with: "Skip to main content...AI Mode\nAll\nNews...\n<search query>\n<AI content>"
                lines = page_text.split('\n')
                # Find the search query line, content starts after it
                query_lower = search_query.lower().strip()
                content_start = -1
                for i, line in enumerate(lines):
                    if line.strip().lower() == query_lower:
                        content_start = i + 1
                        break
                if content_start == -1:
                    # Try partial match
                    for i, line in enumerate(lines):
                        if query_lower[:30] in line.strip().lower():
                            content_start = i + 1
                            break
                if content_start == -1:
                    # Check for CAPTCHA
                    if 'unusual traffic' in page_text.lower() or 'captcha' in page_text.lower():
                        print(f"[SmartAnnounce] google-scrape: Google CAPTCHA detected")
                    else:
                        print(f"[SmartAnnounce] google-scrape: could not find AI content in {len(page_text)} chars")
                    return None
                # Content runs until sources/footer markers
                ai_lines = []
                for line in lines[content_start:]:
                    lt = line.strip()
                    # Stop at footer/source markers
                    if lt in ('Sources', 'Related searches', 'People also search for',
                              'HelpSend feedbackPrivacyTerms') or lt.startswith('Results are personalized'):
                        break
                    ai_lines.append(lt)
                ai_text = '\n'.join(ai_lines).strip()

            return ai_text if ai_text else None

        finally:
            # Restore previous window focus
            if prev_wid:
                try:
                    xdo('windowactivate', prev_wid)
                except Exception:
                    pass

    def _call_google_scrape(self, entry, system_prompt, max_words):
        """Scrape Google AI Overview via Firefox, pre-clean, then summarize with Ollama."""
        import re
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        search_query = entry['prompt']
        print(f"\n[SmartAnnounce] #{entry['id']}: Searching: {search_query}")

        ai_text = self._scrape_google_ai_overview(search_query)
        if not ai_text:
            print(f"[SmartAnnounce] #{entry['id']}: no AI Overview found")
            return None

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── AI OVERVIEW ({len(ai_text)} chars) ──")
            for line in ai_text.split('\n'):
                print(f"  {line}")

        # Pre-clean: strip junk so Ollama processes less text
        lines = ai_text.split('\n')
        cleaned = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip short header-only lines
            if line.endswith(':') and len(line.split()) <= 6:
                continue
            # Remove bullet markers
            line = re.sub(r'^[\s•·\-\*]+', '', line).strip()
            # Remove URLs
            line = re.sub(r'https?://\S+', '', line).strip()
            # Remove citation markers like [1], [2], +1, +2
            line = re.sub(r'\[\d+\]', '', line)
            line = re.sub(r'^\+\d+\s*', '', line).strip()
            # Remove source attributions
            line = re.sub(r'^Source:.*$', '', line, flags=re.IGNORECASE).strip()
            if line and len(line.split()) >= 3:
                cleaned.append(line)

        pre_cleaned = ' '.join(cleaned)
        # Trim input to keep Ollama fast
        words = pre_cleaned.split()
        if len(words) > 200:
            pre_cleaned = ' '.join(words[:200])

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── PRE-CLEANED ({len(pre_cleaned.split())} words) ──")
            print(f"  {pre_cleaned[:300]}...")

        # Send pre-cleaned text through Ollama for natural spoken summary
        return self._ollama_compose(entry, system_prompt, max_words, pre_cleaned)

    def _ollama_compose(self, entry, system_prompt, max_words, search_context):
        """Use local Ollama to compose natural speech from search results."""
        import urllib.request, json
        prompt = (
            f"{system_prompt}\n\n"
            f"Web search results:\n{search_context}\n\n"
            f"Based on the above, compose a summary in not more than "
            f"{max_words} words. No intro, no date or time, just the content."
        )
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        print(f"[SmartAnnounce] #{entry['id']}: Sending to LLM ({self._ollama_model})...")
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── LLM PROMPT ──")
            for line in prompt.split('\n'):
                print(f"  {line}")
        temperature = float(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_TEMPERATURE', 0.7))
        top_p = float(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_TOP_P', 0.9))
        num_ctx = int(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_NUM_CTX', 1024))
        num_thread = int(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_NUM_THREAD', 0))
        options = {
            "num_predict": max_words * 3,
            "temperature": temperature,
            "top_p": top_p,
            "num_ctx": num_ctx,
        }
        if num_thread > 0:
            options["num_thread"] = num_thread
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: Ollama options: temp={temperature}, top_p={top_p}, ctx={num_ctx}, threads={num_thread or 'all'}, max_tokens={max_words * 3}")
        payload = json.dumps({
            "model": self._ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": options,
            "context": [],  # fresh context — don't carry over from previous calls
        }).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:11434/api/generate',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        text = data.get('response', '').strip()
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Ollama")
            return None
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── LLM RESPONSE ──")
            print(f"  {text}")
        return text

    def trigger(self, entry_id):
        """Manually trigger a specific announcement. Returns True if found."""
        with self._lock:
            entry = next((e for e in self._entries if e['id'] == entry_id), None)
        if entry:
            threading.Thread(target=self._run_announcement, args=(entry, True),
                             daemon=True, name=f"SmartAnnounce-{entry_id}").start()
            return True
        return False

    def get_entries(self):
        """Return list of configured entries for status display."""
        return self._entries


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
            '# Auto-generated by Radio Gateway',
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
            '# Disable autoban (gateway pymumble reconnects trigger it)',
            'autobanAttempts=0',
            '',
            '# Long client timeout (pymumble protocol 1.2.4 ping may not satisfy newer murmur)',
            'timeout=300',
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


# ============================================================================
# RTL-AIRBAND / SDR MANAGER
# ============================================================================

class RTLAirbandManager:
    """Manage RTLSDR-Airband process, config generation, and channel memory."""

    ANTENNAS = ['Tuner 1 50 ohm', 'Tuner 1 Hi-Z', 'Tuner 2 50 ohm']
    BANDWIDTHS = [0.2, 0.3, 0.6, 1.536, 5, 6, 7, 8]
    SAMPLE_RATES = [0.5, 1.0, 2.0, 2.56, 6.0, 8.0, 10.66]
    MODULATIONS = ['nfm', 'am']
    CONFIG_PATH = '/etc/rtl_airband/rspduo_gateway.conf'

    # All tunable setting keys with their types and defaults
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
    }

    def __init__(self, gateway_dir):
        self._gateway_dir = gateway_dir
        self._channels_path = os.path.join(gateway_dir, 'sdr_channels.json')
        self._process = None

        # Set defaults for all settings
        for key, (typ, default) in self._SETTING_KEYS.items():
            setattr(self, key, default)

        # Channel memory (10 slots)
        self.channels = [None] * 10
        self._load_channels()

    def _load_channels(self):
        """Load channel memory and current settings from JSON file."""
        try:
            if os.path.exists(self._channels_path):
                with open(self._channels_path, 'r') as f:
                    data = json_mod.load(f)
                self.channels = data.get('channels', [None] * 10)
                # Pad to 10 slots
                while len(self.channels) < 10:
                    self.channels.append(None)
                self.channels = self.channels[:10]
                # Restore current tuning state
                saved = data.get('current', {})
                # Migrate: old 'bandwidth' key was actually sample_rate
                if 'bandwidth' in saved and 'sample_rate' not in saved:
                    saved['sample_rate'] = saved.pop('bandwidth')
                for key, (typ, default) in self._SETTING_KEYS.items():
                    if key in saved:
                        try:
                            setattr(self, key, typ(saved[key]))
                        except (ValueError, TypeError):
                            pass
        except Exception:
            self.channels = [None] * 10

    def _save_channels(self):
        """Persist channel memory and current settings to JSON file."""
        try:
            with open(self._channels_path, 'w') as f:
                json_mod.dump({'channels': self.channels, 'current': self._current_settings()}, f, indent=2)
        except Exception as e:
            print(f"  [SDR] Failed to save channels: {e}")

    def _current_settings(self):
        """Return current tuning state as a dict."""
        return {key: getattr(self, key) for key in self._SETTING_KEYS}

    def get_status(self):
        """Return status dict for the /sdrstatus endpoint."""
        alive = False
        # Always check via pgrep — rtl_airband daemonizes so self._process may show exited
        try:
            result = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
            alive = result.returncode == 0
        except Exception:
            pass
        d = self._current_settings()
        d['process_alive'] = alive
        d['channels'] = []
        for i, ch in enumerate(self.channels):
            if ch:
                d['channels'].append({'slot': i, 'name': ch.get('name', ''), 'frequency': ch.get('frequency', 0), 'modulation': ch.get('modulation', '')})
            else:
                d['channels'].append(None)
        return d

    def apply_settings(self, **kwargs):
        """Update tuning state, rewrite config, restart rtl_airband."""
        for key, (typ, _default) in self._SETTING_KEYS.items():
            if key in kwargs:
                try:
                    setattr(self, key, typ(kwargs[key]))
                except (ValueError, TypeError):
                    pass
        try:
            self._write_config()
            self._restart_process()
            self._save_channels()  # Persist current settings to disk
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def save_channel(self, slot, name=''):
        """Save current settings to a channel slot."""
        if not (0 <= slot <= 9):
            return {'ok': False, 'error': 'Invalid slot'}
        settings = self._current_settings()
        settings['name'] = name or f"CH {slot}"
        self.channels[slot] = settings
        self._save_channels()
        return {'ok': True}

    def recall_channel(self, slot):
        """Recall and apply settings from a channel slot."""
        if not (0 <= slot <= 9):
            return {'ok': False, 'error': 'Invalid slot'}
        ch = self.channels[slot]
        if not ch:
            return {'ok': False, 'error': 'Empty slot'}
        return self.apply_settings(**ch)

    def delete_channel(self, slot):
        """Clear a channel slot."""
        if not (0 <= slot <= 9):
            return {'ok': False, 'error': 'Invalid slot'}
        self.channels[slot] = None
        self._save_channels()
        return {'ok': True}

    def _write_config(self):
        """Generate and write the rtl_airband config file."""
        # Build device_string with SoapySDR settings
        ds_parts = ['driver=sdrplay']
        # Antenna selection is set at device level in rtl_airband, not device_string

        # Build gain line (omit entirely for AGC)
        gain_line = ''
        if self.gain_mode == 'manual':
            gain_line = f'  gain = "RFGR={self.rfgr},IFGR={self.ifgr}";'

        # Build device settings string for SoapySDR kwargs
        settings_parts = []
        settings_parts.append(f'biasT_ctrl={str(self.bias_t).lower()}')
        settings_parts.append(f'rfnotch_ctrl={str(self.rf_notch).lower()}')
        settings_parts.append(f'dabnotch_ctrl={str(self.dab_notch).lower()}')
        settings_parts.append(f'iqcorr_ctrl={str(self.iq_correction).lower()}')
        settings_parts.append(f'extref_ctrl={str(self.external_ref).lower()}')
        settings_parts.append(f'agc_setpoint={self.agc_setpoint}')

        device_string = ','.join(ds_parts)
        device_settings = ','.join(settings_parts)

        # Build optional channel-level lines
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

        # Build optional device-level lines
        dev_opts = ''
        if self.correction != 0.0:
            dev_opts += f'  correction = {self.correction};\n'
        if self.tau != 200:
            dev_opts += f'  tau = {self.tau};\n'
        if self.antenna:
            dev_opts += f'  antenna = "{self.antenna}";\n'

        conf = f'''# Auto-generated by Radio Gateway SDR Manager
# Do not edit manually — changes will be overwritten on next tune.

devices:
({{
  type = "soapysdr";
  device_string = "{device_string}";
  device_settings = "{device_settings}";
  mode = "multichannel";
  centerfreq = {self.frequency};
  sample_rate = {self.sample_rate};
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
        # Write via sudo tee
        proc = subprocess.run(
            ['sudo', 'tee', self.CONFIG_PATH],
            input=conf.encode(), capture_output=True, timeout=5
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to write config: {proc.stderr.decode()}")

    def _restart_process(self):
        """Kill existing rtl_airband and start a new one."""
        # Kill all existing
        # Kill all existing rtl_airband — use SIGKILL since it may ignore SIGTERM
        subprocess.run(['sudo', 'killall', '-9', 'rtl_airband'], capture_output=True, timeout=5)
        self._process = None
        time.sleep(1)

        # Restart SDRplay API service — kill hard then start fresh
        # (systemctl restart hangs because sdrplay_apiService ignores SIGTERM)
        subprocess.run(['sudo', 'systemctl', 'stop', 'sdrplay.service'],
                       capture_output=True, timeout=3)
        subprocess.run(['sudo', 'killall', '-9', 'sdrplay_apiService'],
                       capture_output=True, timeout=3)
        time.sleep(1)
        subprocess.run(['sudo', 'systemctl', 'start', 'sdrplay.service'],
                       capture_output=True, timeout=10)
        time.sleep(2)

        # Start rtl_airband (daemon mode — it forks into background)
        proc = subprocess.run(
            ['rtl_airband', '-e', '-c', self.CONFIG_PATH],
            capture_output=True, timeout=10
        )
        time.sleep(1)
        # Verify it's running (it daemonizes, so check via pgrep)
        chk = subprocess.run(['pgrep', 'rtl_airband'], capture_output=True, timeout=2)
        if chk.returncode != 0:
            raise RuntimeError(f"rtl_airband failed to start: {proc.stderr.decode()[:200]}")

    def stop(self):
        """Stop rtl_airband."""
        subprocess.run(['sudo', 'killall', '-9', 'rtl_airband'], capture_output=True, timeout=5)
        self._process = None


# ============================================================================
# WEB CONFIGURATION UI
# ============================================================================

class WebConfigServer:
    """Lightweight web UI for editing gateway_config.txt.

    Runs Python's built-in http.server on a daemon thread.  Serves a
    single-page form grouped by INI sections with Save and Save & Restart.
    """

    # Keys whose values should be masked in the UI
    _SENSITIVE_KEYS = {
        'STREAM_PASSWORD', 'MUMBLE_PASSWORD', 'CAT_PASSWORD', 'DDNS_PASSWORD',
        'SMART_ANNOUNCE_API_KEY', 'SMART_ANNOUNCE_GEMINI_API_KEY',
        'WEB_CONFIG_PASSWORD', 'MUMBLE_SERVER_1_PASSWORD', 'MUMBLE_SERVER_2_PASSWORD',
        'EMAIL_APP_PASSWORD',
    }

    # Keys that store hex integers
    _HEX_KEYS = {'AIOC_VID', 'AIOC_PID'}

    # Section display names
    _SECTION_NAMES = {
        'startup': 'Startup Script',
        'mumble': 'Mumble Server',
        'radio': 'Radio Interface (AIOC)',
        'audio': 'Audio Format & Buffering',
        'levels': 'Audio Levels',
        'ptt': 'PTT (Push-to-Talk)',
        'vad': 'Voice Activity Detection',
        'vox': 'VOX',
        'processing': 'Audio Processing',
        'sdr1': 'SDR Receiver 1',
        'sdr2': 'SDR Receiver 2',
        'switching': 'Signal Detection & Switching',
        'remote': 'Remote Audio Link',
        'announce': 'Announcement Input',
        'playback': 'File Playback',
        'tts': 'Text-to-Speech',
        'speaker': 'Speaker Output',
        'streaming': 'Broadcastify Streaming',
        'echolink': 'EchoLink',
        'relay': 'Relay Control',
        'smart': 'Smart Announcements',
        'ddns': 'Dynamic DNS',
        'web': 'Web Configuration',
        'cat': 'TH-9800 CAT Control',
        'mumble-server-1': 'Mumble Server 1',
        'mumble-server-2': 'Mumble Server 2',
        'advanced': 'Advanced / Diagnostics',
    }

    def __init__(self, config, gateway=None):
        self.config = config
        self.gateway = gateway
        self._server = None
        self._thread = None
        self._defaults = getattr(config, '_defaults', {})
        self._stream_subscribers = []  # list of events for audio stream listeners
        self._stream_events = []      # events to notify listeners of new data
        self._stream_lock = threading.Lock()
        self._mp3_buffer = []         # shared ring buffer of MP3 chunks
        self._mp3_seq = 0             # sequence number of next append
        self._encoder_proc = None     # shared FFmpeg process
        self._encoder_stdin = None    # stdin pipe for encoder
        self._last_audio_push = 0     # monotonic time of last real audio
        self.sdr_manager = None       # RTLAirbandManager instance
        # WebSocket PCM streaming (low-latency)
        self._ws_clients = []         # list of (socket, queue) tuples for WebSocket PCM clients
        self._ws_lock = threading.Lock()

    def start(self):
        """Start the HTTP server on a daemon thread."""
        import http.server
        import socketserver

        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        password = str(getattr(self.config, 'WEB_CONFIG_PASSWORD', '') or '')
        parent = self

        # Initialize SDR manager if rtl_airband is available
        if shutil.which('rtl_airband'):
            try:
                self.sdr_manager = RTLAirbandManager(os.path.dirname(
                    getattr(self.config, '_config_path', '') or os.path.join(os.path.dirname(__file__), 'gateway_config.txt')))
            except Exception as e:
                print(f"  [SDR] Manager init failed: {e}")
                self.sdr_manager = None

            # Auto-start internal SDR if configured
            if self.sdr_manager and getattr(self.config, 'SDR_INTERNAL_AUTOSTART', False):
                try:
                    ch = int(getattr(self.config, 'SDR_INTERNAL_AUTOSTART_CHANNEL', -1))
                    if 0 <= ch <= 9 and self.sdr_manager.channels[ch]:
                        result = self.sdr_manager.recall_channel(ch)
                        print(f"  [SDR] Autostart: recalled CH {ch} — {'OK' if result.get('ok') else result.get('error', 'failed')}")
                    else:
                        # Use last saved settings (already loaded by _load_channels)
                        result = self.sdr_manager.apply_settings()
                        print(f"  [SDR] Autostart: using last settings — {'OK' if result.get('ok') else result.get('error', 'failed')}")
                except Exception as e:
                    print(f"  [SDR] Autostart failed: {e}")

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # Required for WebSocket upgrade

            def end_headers(self):
                # For non-WebSocket responses, close connection to avoid
                # HTTP/1.1 keep-alive issues (no Content-Length on dynamic responses)
                if not self._upgrading_ws:
                    self.send_header('Connection', 'close')
                    self.close_connection = True
                super().end_headers()

            def setup(self):
                super().setup()
                self._upgrading_ws = False

            def log_message(self, format, *args):
                pass  # Suppress request logging

            def _check_auth(self):
                if not password:
                    return True
                import base64
                auth = self.headers.get('Authorization', '')
                if not auth.startswith('Basic '):
                    self._send_auth_required()
                    return False
                try:
                    decoded = base64.b64decode(auth[6:]).decode('utf-8')
                    user, pw = decoded.split(':', 1)
                    if user == 'admin' and pw == password:
                        return True
                except Exception:
                    pass
                self._send_auth_required()
                return False

            def _send_auth_required(self):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Gateway Config"')
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<h1>401 Unauthorized</h1>')

            def do_GET(self):
                if not self._check_auth():
                    return
                import json as json_mod

                if self.path == '/status':
                    # JSON status endpoint for live dashboard
                    data = parent.gateway.get_status_dict() if parent.gateway else {}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/sysinfo':
                    # System status JSON endpoint
                    data = parent._get_sysinfo()
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/catstatus':
                    # JSON radio CAT state endpoint
                    data = {'connected': False, 'cat_enabled': False}
                    if parent.gateway:
                        data['cat_enabled'] = getattr(parent.gateway.config, 'ENABLE_CAT_CONTROL', False)
                        if parent.gateway.cat_client:
                            data = parent.gateway.cat_client.get_radio_state()
                            data['cat_enabled'] = True
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/radio':
                    # Radio control page
                    html = parent._generate_radio_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/sdr':
                    # SDR control page
                    html = parent._generate_sdr_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/sdrstatus':
                    # SDR status JSON endpoint
                    data = {}
                    if parent.sdr_manager:
                        data = parent.sdr_manager.get_status()
                    else:
                        data = {'error': 'SDR manager not available', 'process_alive': False}
                    # Add SDR audio level from gateway's SDR source
                    if parent.gateway:
                        try:
                            src = getattr(parent.gateway, 'sdr_source', None)
                            data['audio_level'] = src.audio_level if src and hasattr(src, 'audio_level') else 0
                        except Exception:
                            data['audio_level'] = 0
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/ws_audio':
                    # WebSocket upgrade for low-latency PCM audio streaming
                    self._upgrading_ws = True
                    import hashlib, base64
                    ws_key = self.headers.get('Sec-WebSocket-Key', '')
                    if not ws_key or self.headers.get('Upgrade', '').lower() != 'websocket':
                        self._upgrading_ws = False
                        self.send_response(400)
                        self.end_headers()
                        return
                    # WebSocket handshake — write raw bytes to bypass
                    # BaseHTTPRequestHandler's send_response which adds
                    # Server/Date headers that can confuse strict WS clients
                    _WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + _WS_MAGIC).encode()).digest()
                    ).decode()
                    # Flush any buffered wfile data, then write handshake
                    # directly to raw socket to avoid BufferedWriter issues
                    self.wfile.flush()
                    handshake = (
                        'HTTP/1.1 101 Switching Protocols\r\n'
                        'Upgrade: websocket\r\n'
                        'Connection: Upgrade\r\n'
                        f'Sec-WebSocket-Accept: {accept}\r\n'
                        '\r\n'
                    )
                    self.request.sendall(handshake.encode('ascii'))
                    self.close_connection = True  # prevent handler loop after do_GET returns
                    _sock = self.request  # raw TCP socket for binary frames
                    _client_ip = self.client_address[0]
                    print(f"\n[WS-Audio] Low-latency client connected from {_client_ip}")
                    _sock.settimeout(30)  # 30s recv timeout for keepalive
                    _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    import queue as _q_mod
                    _send_q = _q_mod.Queue(maxsize=6)  # ~300ms buffer at 50ms chunks
                    _ws_entry = (_sock, _send_q)

                    def _ws_sender(_s, _q):
                        """Dedicated send thread — drains queue, never blocks audio loop."""
                        while True:
                            try:
                                frame = _q.get(timeout=5)
                                if frame is None:
                                    break
                                _s.sendall(frame)
                            except (_q_mod.Empty):
                                continue
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                break

                    _send_thread = threading.Thread(target=_ws_sender, args=(_sock, _send_q), daemon=True)
                    _send_thread.start()

                    with parent._ws_lock:
                        parent._ws_clients.append(_ws_entry)
                    try:
                        # Keep connection alive — read and discard client frames
                        # (we only send, but must handle pings/close)
                        while True:
                            try:
                                hdr = _sock.recv(2)
                                if not hdr or len(hdr) < 2:
                                    break
                                opcode = hdr[0] & 0x0F
                                masked = (hdr[1] & 0x80) != 0
                                payload_len = hdr[1] & 0x7F
                                if payload_len == 126:
                                    ext = _sock.recv(2)
                                    payload_len = int.from_bytes(ext, 'big')
                                elif payload_len == 127:
                                    ext = _sock.recv(8)
                                    payload_len = int.from_bytes(ext, 'big')
                                mask_key = _sock.recv(4) if masked else b''
                                payload = b''
                                while len(payload) < payload_len:
                                    chunk = _sock.recv(payload_len - len(payload))
                                    if not chunk:
                                        break
                                    payload += chunk
                                if opcode == 0x8:  # Close
                                    # Send close frame back
                                    _sock.sendall(b'\x88\x00')
                                    break
                                elif opcode == 0x9:  # Ping → Pong
                                    if masked and mask_key:
                                        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                                    pong = bytearray()
                                    pong.append(0x8A)  # FIN + Pong
                                    if len(payload) < 126:
                                        pong.append(len(payload))
                                    pong.extend(payload)
                                    _sock.sendall(bytes(pong))
                            except socket.timeout:
                                continue  # recv timeout is normal, keep waiting
                            except (ConnectionResetError, BrokenPipeError, OSError):
                                break
                    finally:
                        _send_q.put(None)  # signal sender thread to exit
                        with parent._ws_lock:
                            try:
                                parent._ws_clients.remove(_ws_entry)
                            except ValueError:
                                pass
                        print(f"[WS-Audio] Disconnected {_client_ip}")
                    return
                elif self.path == '/ws_mic':
                    # WebSocket endpoint for browser microphone → radio TX
                    self._upgrading_ws = True
                    import hashlib, base64
                    ws_key = self.headers.get('Sec-WebSocket-Key', '')
                    if not ws_key or self.headers.get('Upgrade', '').lower() != 'websocket':
                        self._upgrading_ws = False
                        self.send_response(400)
                        self.end_headers()
                        return
                    # Check if web mic source is available
                    _mic_src = parent.gateway.web_mic_source if parent.gateway else None
                    if not _mic_src:
                        self._upgrading_ws = False
                        self.send_response(503)
                        self.end_headers()
                        return
                    # Reject if another mic client is already connected
                    if _mic_src.client_connected:
                        self._upgrading_ws = False
                        self.send_response(409)  # Conflict
                        self.end_headers()
                        return
                    # WebSocket handshake
                    _WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + _WS_MAGIC).encode()).digest()
                    ).decode()
                    self.wfile.flush()
                    handshake = (
                        'HTTP/1.1 101 Switching Protocols\r\n'
                        'Upgrade: websocket\r\n'
                        'Connection: Upgrade\r\n'
                        f'Sec-WebSocket-Accept: {accept}\r\n'
                        '\r\n'
                    )
                    self.request.sendall(handshake.encode('ascii'))
                    self.close_connection = True
                    _sock = self.request
                    _client_ip = self.client_address[0]
                    _sock.settimeout(30)
                    _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    _mic_src.client_connected = True
                    print(f"\n[WS-Mic] Browser mic connected from {_client_ip}")
                    # Key PTT via CAT !ptt command (same as regular PTT button)
                    _gw = parent.gateway
                    _cat_ptt_keyed = False
                    if _gw and _gw.cat_client:
                        try:
                            _gw.cat_client._send_cmd("!ptt")
                            _cat_ptt_keyed = True
                            _gw.ptt_active = True
                            _gw._webmic_ptt_active = True
                            _gw.last_sound_time = time.time()
                            print(f"[WS-Mic] PTT keyed via CAT !ptt")
                        except Exception as _ce:
                            print(f"[WS-Mic] CAT PTT key failed: {_ce}")
                    try:
                        while True:
                            try:
                                hdr = _sock.recv(2)
                                if not hdr or len(hdr) < 2:
                                    break
                                opcode = hdr[0] & 0x0F
                                masked = (hdr[1] & 0x80) != 0
                                payload_len = hdr[1] & 0x7F
                                if payload_len == 126:
                                    ext = _sock.recv(2)
                                    payload_len = int.from_bytes(ext, 'big')
                                elif payload_len == 127:
                                    ext = _sock.recv(8)
                                    payload_len = int.from_bytes(ext, 'big')
                                mask_key = _sock.recv(4) if masked else b''
                                payload = b''
                                while len(payload) < payload_len:
                                    chunk = _sock.recv(payload_len - len(payload))
                                    if not chunk:
                                        break
                                    payload += chunk
                                if masked and mask_key:
                                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                                if opcode == 0x8:  # Close
                                    _sock.sendall(b'\x88\x00')
                                    break
                                elif opcode == 0x9:  # Ping → Pong
                                    pong = bytearray([0x8A, len(payload) if len(payload) < 126 else 0])
                                    if len(payload) < 126:
                                        pong[1] = len(payload)
                                    pong.extend(payload)
                                    _sock.sendall(bytes(pong))
                                elif opcode == 0x2:  # Binary — PCM audio data
                                    _mic_src.push_audio(payload)
                            except socket.timeout:
                                continue
                            except (ConnectionResetError, BrokenPipeError, OSError):
                                break
                    finally:
                        _mic_src.client_connected = False
                        _mic_src._sub_buffer = b''
                        # Unkey PTT via CAT !ptt toggle
                        if _gw:
                            _gw._webmic_ptt_active = False
                        if _gw and _gw.cat_client and _cat_ptt_keyed:
                            try:
                                _gw.cat_client._send_cmd("!ptt")
                                _gw.ptt_active = False
                                print(f"[WS-Mic] PTT unkeyed via CAT !ptt")
                            except Exception:
                                pass
                        print(f"[WS-Mic] Disconnected {_client_ip}")
                    return
                elif self.path == '/stream':
                    # MP3 audio stream from shared encoder
                    _client_ip = self.client_address[0]
                    print(f"\n[Stream] Connection from {_client_ip}")
                    ev, seq = parent._subscribe_stream()
                    _bytes_sent = 0
                    try:
                        # Wait for encoder to produce initial MP3 data
                        for _wait in range(50):  # up to 5 seconds
                            ev.wait(timeout=0.1)
                            ev.clear()
                            with parent._stream_lock:
                                if parent._mp3_seq > seq:
                                    break
                        with parent._stream_lock:
                            if parent._mp3_seq <= seq:
                                print(f"[Stream] No encoder data for {_client_ip} — aborting")
                                self.send_response(503)
                                self.end_headers()
                                return

                        self.send_response(200)
                        self.send_header('Content-Type', 'audio/mpeg')
                        self.send_header('Cache-Control', 'no-cache, no-store')
                        self.send_header('Connection', 'close')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.send_header('icy-name', 'Radio Gateway')
                        self.end_headers()
                        print(f"[Stream] Streaming to {_client_ip}")

                        while True:
                            ev.wait(timeout=5)
                            ev.clear()
                            with parent._stream_lock:
                                buf = parent._mp3_buffer
                                cur_seq = parent._mp3_seq
                                # How many new chunks since our last read
                                available = cur_seq - seq
                                if available > 0:
                                    # Clamp to buffer size (in case we fell behind)
                                    available = min(available, len(buf))
                                    chunks = buf[-available:] if available < len(buf) else list(buf)
                                    seq = cur_seq
                                else:
                                    chunks = []
                            for chunk in chunks:
                                self.wfile.write(chunk)
                                _bytes_sent += len(chunk)
                            if chunks:
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except Exception as e:
                        print(f"\n[Stream] Error for {_client_ip}: {e}")
                    finally:
                        _kb = _bytes_sent // 1024
                        print(f"[Stream] Disconnected {_client_ip} ({_kb}KB sent)")
                        parent._unsubscribe_stream(ev)
                    return
                elif self.path == '/dashboard' or self.path == '/':
                    # Live status dashboard (default page)
                    html = parent._generate_dashboard()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/logs':
                    # Log viewer page
                    html = parent._generate_logs_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/tracestatus':
                    _gw = parent.gateway
                    _ts = {'audio_trace': False, 'watchdog_trace': False}
                    if _gw:
                        _ts['audio_trace'] = getattr(_gw, '_trace_recording', False)
                        _ts['watchdog_trace'] = getattr(_gw, '_watchdog_active', False)
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(_ts).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path.startswith('/logdata'):
                    # Log data API — returns JSON lines after given sequence number
                    import urllib.parse as _up
                    qs = _up.parse_qs(_up.urlparse(self.path).query)
                    after = int(qs.get('after', ['0'])[0])
                    writer = parent.gateway._status_writer if parent.gateway else None
                    lines = []
                    last_seq = after
                    if writer:
                        for seq, text in writer.get_log_lines(after_seq=after, limit=500):
                            lines.append(text)
                            last_seq = seq
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps({'seq': last_seq, 'lines': lines}).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/config':
                    # Config editor
                    html = parent._generate_html()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))

            def do_POST(self):
                if not self._check_auth():
                    return
                import urllib.parse
                import json as json_mod

                if self.path == '/key':
                    # Key command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    try:
                        data = json_mod.loads(body)
                        key_char = data.get('key', '')
                        if key_char and parent.gateway:
                            parent.gateway.handle_key(key_char)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                elif self.path == '/tts':
                    # Text-to-speech endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    ok = False
                    error = None
                    try:
                        data = json_mod.loads(body)
                        text = data.get('text', '').strip()
                        voice = data.get('voice', None)
                        if not text:
                            error = 'no text provided'
                        elif not parent.gateway:
                            error = 'gateway not ready'
                        elif not parent.gateway.tts_engine:
                            error = 'TTS not available'
                        else:
                            import threading
                            def _do_tts():
                                print(f"[WebTTS] Speaking: {text[:80]}...")
                                try:
                                    result = parent.gateway.speak_text(text, voice=voice)
                                    print(f"[WebTTS] Result: {result}")
                                except Exception as e:
                                    print(f"[WebTTS] Error: {e}")
                            threading.Thread(target=_do_tts, daemon=True, name="WebTTS").start()
                            ok = True
                    except Exception as e:
                        error = str(e)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
                    self.wfile.write(resp.encode())
                    return
                elif self.path == '/proc_toggle':
                    # Per-source audio processing toggle endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    try:
                        data = json_mod.loads(body)
                        source = data.get('source', '')  # "radio" or "sdr"
                        filt = data.get('filter', '')    # "gate", "hpf", "lpf", "notch", "deesser", "spectral"
                        if source and filt and parent.gateway:
                            parent.gateway.handle_proc_toggle(source, filt)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                elif self.path == '/catcmd':
                    # CAT radio command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        gw = parent.gateway
                        if cmd == 'CAT_DISCONNECT' and gw and gw.cat_client:
                            gw.cat_client._stop = True
                            gw.cat_client.close()
                            gw.cat_client = None
                            print("\n  [CAT] Disconnected via web")
                            result = {'ok': True}
                        elif cmd == 'SERIAL_DISCONNECT' and gw and gw.cat_client:
                            gw.cat_client._pause_drain()
                            try:
                                resp = gw.cat_client._send_cmd("!serial disconnect")
                            finally:
                                gw.cat_client._drain_paused = False
                            if resp and 'disconnected' in resp:
                                gw.cat_client._serial_connected = False
                            print(f"\n  [CAT] Serial disconnect: {resp}")
                            result = {'ok': resp and 'disconnected' in resp, 'status': resp or ''}
                        elif cmd == 'SERIAL_CONNECT' and gw and gw.cat_client:
                            # serial connect takes ~4s (startup sequence with sleeps)
                            cat = gw.cat_client
                            cat._pause_drain()
                            try:
                                with cat._sock_lock:
                                    cat._sock.sendall(b"!serial connect\n")
                                    cat._last_activity = time.monotonic()
                                    resp = cat._recv_line(timeout=10.0)
                            finally:
                                cat._drain_paused = False
                            ok = resp and 'connected' in resp
                            already = ok and 'already' in resp
                            if ok:
                                cat._serial_connected = True
                            if ok and not already:
                                # Display refresh: press+release each VFO dial
                                time.sleep(0.3)
                                cat._pause_drain()
                                try:
                                    cat._send_button([0x00, 0x25], 3, 5)  # L_DIAL_PRESS
                                    time.sleep(0.15)
                                    cat._send_button_release()
                                    time.sleep(0.3)
                                    cat._drain(0.5)
                                    cat._send_button([0x00, 0xA5], 3, 5)  # R_DIAL_PRESS
                                    time.sleep(0.15)
                                    cat._send_button_release()
                                    time.sleep(0.3)
                                    cat._drain(0.5)
                                finally:
                                    cat._drain_paused = False
                                # Read RTS state from saved file (TH9800 persists it)
                                try:
                                    with open('/tmp/th9800_rts_state', 'r') as f:
                                        cat._rts_usb = f.read().strip() == '1'
                                except Exception:
                                    cat._rts_usb = None

                            print(f"\n  [CAT] Serial connect: {resp}")
                            result = {'ok': ok, 'status': resp or ''}
                        elif cmd == 'SETUP_RADIO' and gw and gw.cat_client:
                            # Run setup_radio (channels, volume, power) from config
                            cat = gw.cat_client
                            try:
                                cat.setup_radio(gw.config)
                                result = {'ok': True, 'status': 'setup complete'}
                            except Exception as e:
                                print(f"\n  [CAT] Setup error: {e}")
                                result = {'ok': False, 'status': str(e)}
                        elif cmd == 'SERIAL_STATUS' and gw and gw.cat_client:
                            resp = gw.cat_client._send_cmd("!serial status")
                            result = {'ok': True, 'status': resp or 'unknown'}
                        elif cmd == 'CAT_RECONNECT' and gw:
                            if gw.cat_client:
                                ok = gw.cat_client.reconnect()
                                print(f"\n  [CAT] Reconnected via web: {'ok' if ok else 'failed'}")
                                result = {'ok': ok}
                            else:
                                # Create fresh client
                                host = str(getattr(gw.config, 'CAT_HOST', '127.0.0.1'))
                                port = int(getattr(gw.config, 'CAT_PORT', 9800))
                                pw = str(getattr(gw.config, 'CAT_PASSWORD', '') or '')
                                cat = RadioCATClient(host, port, pw)
                                if cat.connect():
                                    cat.start_background_drain()
                                    gw.cat_client = cat
                                    print("\n  [CAT] Connected via web")
                                    result = {'ok': True}
                                else:
                                    print("\n  [CAT] Connect failed via web")
                                    result = {'ok': False, 'error': 'Connection failed'}
                        elif cmd and gw and gw.cat_client:
                            cat = gw.cat_client
                            if cmd == 'VOL_LEFT':
                                ret = cat.send_web_volume(cat.LEFT, data.get('value', 50))
                                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
                            elif cmd == 'VOL_RIGHT':
                                ret = cat.send_web_volume(cat.RIGHT, data.get('value', 50))
                                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
                            elif cmd == 'SQ_LEFT':
                                ret = cat.send_web_squelch(cat.LEFT, data.get('value', 25))
                                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
                            elif cmd == 'SQ_RIGHT':
                                ret = cat.send_web_squelch(cat.RIGHT, data.get('value', 25))
                                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
                            else:
                                ret = cat.send_web_command(cmd)
                                if isinstance(ret, str):
                                    if 'serial not connected' in ret:
                                        cat._serial_connected = False
                                    result = {'ok': False, 'error': ret}
                                else:
                                    result = {'ok': bool(ret)}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                    return
                elif self.path == '/sdrcmd':
                    # SDR command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'SDR manager not available'}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        mgr = parent.sdr_manager
                        if mgr:
                            if cmd == 'tune':
                                result = mgr.apply_settings(**{k: v for k, v in data.items() if k != 'cmd'})
                            elif cmd == 'save_channel':
                                result = mgr.save_channel(int(data.get('slot', 0)), data.get('name', ''))
                            elif cmd == 'recall_channel':
                                result = mgr.recall_channel(int(data.get('slot', 0)))
                            elif cmd == 'delete_channel':
                                result = mgr.delete_channel(int(data.get('slot', 0)))
                            elif cmd == 'restart':
                                try:
                                    mgr._restart_process()
                                    result = {'ok': True}
                                except Exception as e:
                                    result = {'ok': False, 'error': str(e)}
                            elif cmd == 'stop':
                                mgr.stop()
                                result = {'ok': True}
                            else:
                                result = {'ok': False, 'error': f'Unknown command: {cmd}'}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                    return
                elif self.path == '/tracecmd':
                    content_length = int(self.headers.get('Content-Length', 0))
                    post_data = self.rfile.read(content_length).decode('utf-8')
                    import urllib.parse as _up
                    params = _up.parse_qs(post_data)
                    trace_type = params.get('type', [''])[0]
                    _gw = parent.gateway
                    result = {'ok': False}
                    if _gw and trace_type == 'audio':
                        _gw._trace_recording = not _gw._trace_recording
                        if _gw._trace_recording:
                            _gw._audio_trace.clear()
                            _gw._spk_trace.clear()
                            _gw._trace_events.clear()
                            _gw._audio_trace_t0 = time.monotonic()
                            print(f"\n[Trace] Recording STARTED (via web UI)")
                        else:
                            print(f"\n[Trace] Recording STOPPED ({len(_gw._audio_trace)} ticks captured)")
                            _gw._dump_audio_trace()
                        _gw._trace_events.append((time.monotonic(), 'trace', 'on' if _gw._trace_recording else 'off'))
                        result = {'ok': True, 'active': _gw._trace_recording}
                    elif _gw and trace_type == 'watchdog':
                        _gw._watchdog_active = not _gw._watchdog_active
                        if _gw._watchdog_active:
                            _gw._watchdog_t0 = time.monotonic()
                            _gw._watchdog_thread = threading.Thread(
                                target=_gw._watchdog_trace_loop, daemon=True)
                            _gw._watchdog_thread.start()
                            print(f"\n[Watchdog] Trace STARTED (via web UI)")
                        else:
                            print(f"\n[Watchdog] Trace STOPPED (via web UI)")
                        result = {'ok': True, 'active': _gw._watchdog_active}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                    return
                elif self.path == '/refreshsounds':
                    # Re-randomize soundboard slots
                    result = {'ok': False, 'count': 0}
                    gw = parent.gateway
                    if gw and gw.playback_source:
                        try:
                            # Clear cached soundboard files
                            _cache_dir = os.path.join(gw.playback_source.announcement_directory, '.cache')
                            if os.path.isdir(_cache_dir):
                                import shutil
                                shutil.rmtree(_cache_dir)
                            # Re-scan files (local files stay, new random fills)
                            gw.playback_source.check_file_availability()
                            _count = sum(1 for k in '123456789' if gw.playback_source.file_status[k]['exists']
                                         and gw.playback_source.file_status[k].get('path', '').find('.cache') >= 0)
                            result = {'ok': True, 'count': _count}
                        except Exception as _e:
                            result = {'ok': False, 'error': str(_e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    try:
                        self.wfile.write(json_mod.dumps(result).encode())
                    except BrokenPipeError:
                        pass
                    return
                elif self.path == '/darkicecmd':
                    # DarkIce / Broadcastify feeder control
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        gw = parent.gateway
                        if gw:
                            if cmd == 'start':
                                if not gw._find_darkice_pid():
                                    gw._restart_darkice()
                                    result = {'ok': True, 'msg': 'DarkIce started'}
                                else:
                                    result = {'ok': True, 'msg': 'DarkIce already running'}
                            elif cmd == 'stop':
                                pid = gw._find_darkice_pid()
                                if pid:
                                    import signal as sig_mod
                                    try:
                                        os.kill(pid, sig_mod.SIGTERM)
                                        time.sleep(1)
                                        # Check if still alive
                                        if gw._find_darkice_pid():
                                            os.kill(pid, sig_mod.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    gw._darkice_pid = None
                                    gw._darkice_was_running = False  # Prevent auto-restart
                                    result = {'ok': True, 'msg': 'DarkIce stopped'}
                                else:
                                    result = {'ok': True, 'msg': 'DarkIce not running'}
                            elif cmd == 'restart':
                                pid = gw._find_darkice_pid()
                                if pid:
                                    import signal as sig_mod
                                    try:
                                        os.kill(pid, sig_mod.SIGTERM)
                                        time.sleep(1)
                                        if gw._find_darkice_pid():
                                            os.kill(pid, sig_mod.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    gw._darkice_pid = None
                                    time.sleep(1)
                                gw._restart_darkice()
                                gw._darkice_was_running = True  # Re-enable auto-restart
                                result = {'ok': True, 'msg': 'DarkIce restarted'}
                    except Exception as e:
                        result = {'ok': False, 'msg': str(e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    return
                elif self.path == '/exit':
                    # Graceful full shutdown (no restart)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    if parent.gateway:
                        parent.gateway.restart_requested = False
                        parent.gateway.running = False
                    return

                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                form = urllib.parse.parse_qs(body, keep_blank_values=True)
                # Flatten: parse_qs returns lists; for checkboxes with hidden fallback,
                # take the LAST value (checkbox 'true' comes after hidden 'false')
                values = {k: v[-1] for k, v in form.items() if k != '_action'}
                action = form.get('_action', ['save'])[0]

                # Checkboxes: unchecked boxes are absent from form data.
                # We need to detect boolean keys and set them to 'false' if missing.
                section_map = parent._build_section_map()
                for key, default_val in parent._defaults.items():
                    if isinstance(default_val, bool) and key not in values:
                        if key in section_map or hasattr(parent.config, key):
                            values[key] = 'false'

                parent._save_config(values)
                if action == 'restart' and parent.gateway:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    msg = parent._wrap_html('Restarting...',
                        '<h2>Configuration saved</h2>'
                        '<p>Gateway is restarting... this page will reload in 5 seconds.</p>'
                        f'<script>setTimeout(function(){{window.location="http://"+window.location.hostname+":"+window.location.port+"/"}},5000)</script>')
                    self.wfile.write(msg.encode('utf-8'))
                    # Signal restart via main loop
                    parent.gateway.restart_requested = True
                    parent.gateway.running = False
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    msg = parent._wrap_html('Saved',
                        '<h2>Configuration saved</h2>'
                        '<p>Settings saved to gateway_config.txt.</p>'
                        '<p>Restart the gateway for changes to take effect.</p>'
                        '<p><a href="/">Back to config</a></p>')
                    self.wfile.write(msg.encode('utf-8'))

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            self._server = ThreadedServer(('0.0.0.0', port), Handler)

            # HTTPS: false, self-signed, or letsencrypt
            https_mode = str(getattr(self.config, 'WEB_CONFIG_HTTPS', 'false')).lower().strip()
            if https_mode in ('true', '1', 'yes', 'self-signed'):
                https_mode = 'self-signed'
            elif https_mode in ('letsencrypt', 'lets-encrypt', 'le'):
                https_mode = 'letsencrypt'
            else:
                https_mode = 'false'

            scheme = 'http'
            if https_mode != 'false':
                import ssl
                cert_file, key_file = self._get_cert(https_mode)
                if cert_file and key_file:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(cert_file, key_file)
                    self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
                    scheme = 'https'
                    self._https_mode = https_mode
                    if https_mode == 'letsencrypt':
                        self._start_renewal_thread(cert_file)
                else:
                    print(f"  [WebConfig] HTTPS failed, falling back to HTTP")

            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name='WebConfig', daemon=True)
            self._thread.start()
            print(f"  [WebConfig] Listening on {scheme}://0.0.0.0:{port}/")
            self._start_encoder()
        except Exception as e:
            print(f"  [WebConfig] Failed to start: {e}")

    def stop(self):
        """Shut down the HTTP server and encoder."""
        self._stop_encoder()
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass

    def _ws_build_frame(self, data):
        """Build a WebSocket binary frame (opcode 0x02). Returns bytes."""
        header = bytearray()
        header.append(0x82)  # FIN + binary opcode
        dlen = len(data)
        if dlen < 126:
            header.append(dlen)
        elif dlen < 65536:
            header.append(126)
            header.extend(dlen.to_bytes(2, 'big'))
        else:
            header.append(127)
            header.extend(dlen.to_bytes(8, 'big'))
        return bytes(header) + data

    def _ws_send_binary(self, sock, data):
        """Send a WebSocket binary frame directly (used for pong responses)."""
        sock.sendall(self._ws_build_frame(data))

    def push_audio(self, pcm_data):
        """Push PCM audio to the shared MP3 encoder (called after VAD gate)."""
        if self._encoder_stdin:
            try:
                self._encoder_stdin.write(pcm_data)
                self._last_audio_push = time.monotonic()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def push_ws_audio(self, pcm_data):
        """Push raw PCM to WebSocket clients via per-client send queues (non-blocking)."""
        # Pre-build the WebSocket binary frame once for all clients
        frame = self._ws_build_frame(pcm_data)
        with self._ws_lock:
            for sock, send_q in self._ws_clients:
                try:
                    send_q.put_nowait(frame)
                except Exception:
                    pass  # queue full — drop frame rather than block audio loop

    def _start_encoder(self):
        """Start the shared FFmpeg MP3 encoder and reader thread."""
        import subprocess as sp
        if self._encoder_proc:
            return
        try:
            self._encoder_proc = sp.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-f', 's16le', '-ar', '48000', '-ac', '1', '-i', 'pipe:0',
                '-c:a', 'libmp3lame', '-b:a', '96k',
                '-flush_packets', '1',
                '-fflags', '+nobuffer',
                '-f', 'mp3', 'pipe:1'
            ], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL)
            self._encoder_stdin = self._encoder_proc.stdin
            # Reader thread: reads MP3 from FFmpeg, pushes to ring buffer
            def _reader():
                while self._encoder_proc and self._encoder_proc.poll() is None:
                    data = self._encoder_proc.stdout.read(4096)
                    if not data:
                        break
                    with self._stream_lock:
                        self._mp3_buffer.append(data)
                        self._mp3_seq += 1
                        # Keep ~30 seconds of buffered MP3 (~360KB at 96kbps)
                        while len(self._mp3_buffer) > 90:
                            self._mp3_buffer.pop(0)
                        # Notify all waiting listeners
                        for ev in self._stream_events:
                            ev.set()
            t = threading.Thread(target=_reader, daemon=True, name='mp3-reader')
            t.start()
            # Feed silence when no real audio is arriving — keeps encoder producing output
            def _silence_feed():
                _silence = b'\x00' * 4800  # 50ms
                while self._encoder_proc and self._encoder_proc.poll() is None:
                    time.sleep(0.05)
                    if (self._encoder_stdin
                            and time.monotonic() - self._last_audio_push > 0.2):
                        try:
                            self._encoder_stdin.write(_silence)
                        except (BrokenPipeError, OSError, ValueError):
                            break
            t2 = threading.Thread(target=_silence_feed, daemon=True, name='mp3-silence')
            t2.start()
            print(f"  [Stream] MP3 encoder started (PID {self._encoder_proc.pid})")
        except FileNotFoundError:
            print(f"  [Stream] FFmpeg not found")
        except Exception as e:
            print(f"  [Stream] Encoder start error: {e}")

    def _stop_encoder(self):
        """Stop the shared FFmpeg encoder."""
        self._encoder_stdin = None
        if self._encoder_proc:
            try:
                self._encoder_proc.stdin.close()
            except Exception:
                pass
            try:
                self._encoder_proc.terminate()
                self._encoder_proc.wait(timeout=3)
            except Exception:
                try:
                    self._encoder_proc.kill()
                except Exception:
                    pass
            self._encoder_proc = None

    def _subscribe_stream(self):
        """Register a new stream listener. Returns (event, seq)."""
        ev = threading.Event()
        with self._stream_lock:
            seq = self._mp3_seq  # Start from current sequence number
            self._stream_events.append(ev)
            self._stream_subscribers.append(ev)
        return ev, seq

    def _unsubscribe_stream(self, ev):
        """Remove a stream listener."""
        with self._stream_lock:
            try:
                self._stream_events.remove(ev)
            except ValueError:
                pass
            try:
                self._stream_subscribers.remove(ev)
            except ValueError:
                pass

    def _get_cert(self, mode):
        """Get SSL cert/key paths. Returns (cert_path, key_path) or (None, None)."""
        cert_dir = os.path.join(os.path.dirname(os.path.abspath(self.config.config_file)), 'certs')
        os.makedirs(cert_dir, exist_ok=True)

        if mode == 'letsencrypt':
            domain = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '').strip()
            if not domain:
                print(f"  [WebConfig] Let's Encrypt requires DDNS_HOSTNAME to be set")
                return None, None
            cert_file = os.path.join(cert_dir, 'fullchain.pem')
            key_file = os.path.join(cert_dir, 'privkey.pem')
            # Check if cert exists and is not expiring within 30 days
            if os.path.exists(cert_file) and os.path.exists(key_file):
                if not self._cert_expiring_soon(cert_file, 30):
                    print(f"  [WebConfig] Using existing Let's Encrypt cert for {domain}")
                    return cert_file, key_file
                print(f"  [WebConfig] Certificate expiring soon, renewing...")
            # Obtain/renew via certbot
            if self._run_certbot(domain, cert_dir):
                return cert_file, key_file
            # Existing cert still valid enough? Use it even if renewal failed
            if os.path.exists(cert_file) and os.path.exists(key_file):
                print(f"  [WebConfig] Certbot failed but existing cert still present, using it")
                return cert_file, key_file
            print(f"  [WebConfig] Let's Encrypt failed, falling back to self-signed")
            mode = 'self-signed'

        # self-signed
        cert_file = os.path.join(cert_dir, 'self_signed.pem')
        key_file = os.path.join(cert_dir, 'self_signed_key.pem')
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
            print(f"  [WebConfig] Generating self-signed certificate...")
            import subprocess
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', key_file, '-out', cert_file,
                '-days', '3650', '-nodes',
                '-subj', '/CN=RadioGateway'
            ], capture_output=True)
            print(f"  [WebConfig] Self-signed certificate saved")
        return cert_file, key_file

    def _run_certbot(self, domain, cert_dir):
        """Run certbot standalone to obtain/renew a certificate."""
        import subprocess
        email = str(getattr(self.config, 'DDNS_USERNAME', '') or '').strip()
        email_args = ['--email', email, '--no-eff-email'] if email and '@' in email else ['--register-unsafely-without-email']
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        cmd = [
            'certbot', 'certonly', '--standalone',
            '--preferred-challenges', 'http',
            '--http-01-port', '80',
            '-d', domain,
            '--cert-path', os.path.join(cert_dir, 'fullchain.pem'),
            '--key-path', os.path.join(cert_dir, 'privkey.pem'),
            '--fullchain-path', os.path.join(cert_dir, 'fullchain.pem'),
            '--config-dir', os.path.join(cert_dir, 'certbot_config'),
            '--work-dir', os.path.join(cert_dir, 'certbot_work'),
            '--logs-dir', os.path.join(cert_dir, 'certbot_logs'),
            '--non-interactive', '--agree-tos',
        ] + email_args
        print(f"  [WebConfig] Running certbot for {domain}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                # certbot may put certs in its own live/ dir; copy them to our cert_dir
                live_dir = os.path.join(cert_dir, 'certbot_config', 'live', domain)
                target_cert = os.path.join(cert_dir, 'fullchain.pem')
                target_key = os.path.join(cert_dir, 'privkey.pem')
                if os.path.isdir(live_dir):
                    # certbot uses symlinks into archive/; resolve and copy
                    import shutil
                    live_cert = os.path.join(live_dir, 'fullchain.pem')
                    live_key = os.path.join(live_dir, 'privkey.pem')
                    if os.path.exists(live_cert):
                        shutil.copy2(os.path.realpath(live_cert), target_cert)
                    if os.path.exists(live_key):
                        shutil.copy2(os.path.realpath(live_key), target_key)
                print(f"  [WebConfig] Let's Encrypt certificate obtained for {domain}")
                return True
            else:
                print(f"  [WebConfig] Certbot failed (exit {result.returncode})")
                stderr = result.stderr.strip()
                if stderr:
                    for line in stderr.split('\n')[-3:]:
                        print(f"  [WebConfig]   {line}")
                return False
        except FileNotFoundError:
            print(f"  [WebConfig] certbot not found — install with: sudo apt install certbot")
            return False
        except subprocess.TimeoutExpired:
            print(f"  [WebConfig] Certbot timed out")
            return False
        except Exception as e:
            print(f"  [WebConfig] Certbot error: {e}")
            return False

    def _cert_expiring_soon(self, cert_path, days=30):
        """Check if certificate expires within N days."""
        try:
            import subprocess
            result = subprocess.run(
                ['openssl', 'x509', '-enddate', '-noout', '-in', cert_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return True
            # Parse "notAfter=Mar  8 12:00:00 2026 GMT"
            date_str = result.stdout.strip().split('=', 1)[1]
            from email.utils import parsedate_to_datetime
            import datetime
            # openssl format: "Mar  8 12:00:00 2026 GMT"
            from datetime import datetime as dt, timedelta, timezone
            expiry = dt.strptime(date_str.strip(), '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
            remaining = expiry - dt.now(timezone.utc)
            return remaining < timedelta(days=days)
        except Exception:
            return True  # if we can't check, assume it needs renewal

    def _start_renewal_thread(self, cert_path):
        """Background thread to check/renew Let's Encrypt cert every 12 hours."""
        def _renewal_loop():
            import time
            while True:
                time.sleep(12 * 3600)  # 12 hours
                try:
                    if self._cert_expiring_soon(cert_path, 30):
                        domain = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '').strip()
                        cert_dir = os.path.dirname(cert_path)
                        if domain and self._run_certbot(domain, cert_dir):
                            print(f"\n[WebConfig] Certificate renewed — restart gateway to use new cert")
                except Exception as e:
                    print(f"\n[WebConfig] Renewal check error: {e}")

        t = threading.Thread(target=_renewal_loop, name='CertRenewal', daemon=True)
        t.start()

    def _build_section_map(self):
        """Parse config file to map KEY -> section_name."""
        section_map = {}
        current_section = 'default'
        config_path = self.config.config_file
        if not os.path.exists(config_path):
            return section_map
        try:
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('[') and ']' in line:
                        current_section = line[1:line.index(']')].strip()
                    elif '=' in line and not line.startswith('#'):
                        key = line.split('=', 1)[0].strip()
                        section_map[key] = current_section
        except Exception:
            pass
        return section_map

    def _save_config(self, new_values):
        """Write updated values to config file, preserving comments and structure."""
        print(f"  [Config] Saving {len(new_values)} keys")
        config_path = self.config.config_file
        if not os.path.exists(config_path):
            return

        lines = []
        written_keys = set()

        with open(config_path, 'r') as f:
            raw_lines = f.readlines()

        for raw_line in raw_lines:
            stripped = raw_line.strip()
            # Check if this is a key=value line (not comment, not section, not blank)
            if stripped and not stripped.startswith('#') and not stripped.startswith('[') and '=' in stripped:
                key = stripped.split('=', 1)[0].strip()
                if key in new_values:
                    new_val = new_values[key]
                    # Format the value
                    if key in self._HEX_KEYS:
                        try:
                            new_val = hex(int(new_val))
                        except (ValueError, TypeError):
                            pass
                    # Preserve inline comment if any (but not for brace-containing values)
                    old_after_eq = stripped.split('=', 1)[1]
                    inline_comment = ''
                    if '#' in old_after_eq and '{' not in old_after_eq:
                        val_part = old_after_eq.split('#')[0].strip()
                        comment_part = old_after_eq[old_after_eq.index('#'):]
                        inline_comment = '  ' + comment_part
                    lines.append(f"{key} = {new_val}{inline_comment}\n")
                    written_keys.add(key)
                else:
                    lines.append(raw_line)
            else:
                lines.append(raw_line)

        # Write atomically via temp file
        tmp_path = config_path + '.tmp'
        with open(tmp_path, 'w') as f:
            f.writelines(lines)
        os.replace(tmp_path, config_path)

    def _wrap_html(self, title, body):
        """Wrap body content in the standard HTML shell."""
        return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radio Gateway - {title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
         background: #1a1a2e; color: #e0e0e0; margin: 0; padding: 20px; }}
  h1 {{ color: #00d4ff; margin: 0 0 20px; font-size: 1.4em; }}
  h2 {{ color: #00d4ff; margin: 10px 0; font-size: 1.2em; }}
  a {{ color: #00d4ff; }}
  details {{ background: #16213e; border: 1px solid #0f3460; border-radius: 6px;
            margin: 8px 0; }}
  summary {{ cursor: pointer; padding: 10px 14px; font-weight: bold; color: #00d4ff;
            font-size: 0.95em; user-select: none; }}
  summary:hover {{ background: #1a2744; }}
  .fields {{ padding: 8px 14px 14px; }}
  .field {{ display: flex; align-items: center; margin: 4px 0; gap: 8px; }}
  .field label {{ min-width: 320px; font-size: 0.85em; color: #b0b0b0; }}
  .field input[type="text"], .field input[type="number"], .field input[type="password"] {{
    flex: 1; background: #0d1b2a; border: 1px solid #1b3a5c; color: #e0e0e0;
    padding: 5px 8px; border-radius: 3px; font-family: monospace; font-size: 0.85em;
    max-width: 500px; }}
  .field input[type="checkbox"] {{ width: 18px; height: 18px; accent-color: #00d4ff; }}
  .field .default {{ font-size: 0.75em; color: #666; margin-left: 8px; }}
  .buttons {{ position: sticky; top: 0; background: #1a1a2e; padding: 10px 0;
             z-index: 10; border-bottom: 1px solid #0f3460; margin-bottom: 10px;
             display: flex; gap: 10px; }}
  button {{ padding: 8px 20px; border: none; border-radius: 4px; cursor: pointer;
           font-size: 0.9em; font-weight: bold; }}
  .btn-save {{ background: #0f3460; color: #e0e0e0; }}
  .btn-save:hover {{ background: #1a4a7a; }}
  .btn-restart {{ background: #c0392b; color: #fff; }}
  .btn-restart:hover {{ background: #e74c3c; }}
  .btn-exit {{ background: #7d3c98; color: #fff; margin-left: auto; }}
  .btn-exit:hover {{ background: #9b59b6; }}
</style>
</head><body>{body}</body></html>'''

    def _generate_logs_page(self):
        """Build the live log viewer HTML page."""
        body = '''
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
  <h2 style="margin:0;">Gateway Logs</h2>
  <div>
    <a href="/" class="rb rb-sm" style="text-decoration:none;">Config</a>
    <a href="/" class="rb rb-sm" style="text-decoration:none;">Dashboard</a>
    <a href="/radio" class="rb rb-sm" style="text-decoration:none;">Radio</a>
    <a href="/sdr" class="rb rb-sm" style="text-decoration:none;">SDR</a>
  </div>
</div>
<div style="margin-bottom:8px; display:flex; gap:10px; align-items:center;">
  <label style="color:#888; font-size:0.85em;">
    <input type="checkbox" id="auto-scroll" checked> Auto-scroll
  </label>
  <label style="color:#888; font-size:0.85em;">
    Filter: <input type="text" id="log-filter" placeholder="regex..." style="background:#0d1b2a; color:#e0e0e0; border:1px solid #1b3a5c; border-radius:3px; padding:2px 6px; width:200px; font-size:0.85em;">
  </label>
  <button class="rb rb-sm" onclick="clearLog()">Clear</button>
  <button class="rb rb-sm" id="btn-trace" onclick="toggleTrace('audio')">Audio Trace</button>
  <button class="rb rb-sm" id="btn-watchdog" onclick="toggleTrace('watchdog')">Watchdog Trace</button>
  <span id="log-status" style="color:#888; font-size:0.8em;"></span>
</div>
<div id="log-box" style="background:#0a0a0a; border:1px solid #1b3a5c; border-radius:4px; padding:8px; height:calc(100vh - 160px); overflow-y:auto; font-family:'Courier New',monospace; font-size:0.82em; line-height:1.5; white-space:pre-wrap; word-break:break-all; color:#c0c0c0;">
</div>

<script>
var _seq = 0;
var _paused = false;

function clearLog() {
  document.getElementById('log-box').innerHTML = '';
}

function toggleTrace(type) {
  fetch('/tracecmd', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'type='+type})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      var btn = document.getElementById(type==='audio'?'btn-trace':'btn-watchdog');
      if (d.active) {
        btn.style.background = '#0f3460';
        btn.style.borderColor = '#00d4ff';
        btn.style.color = '#00d4ff';
      } else {
        btn.style.background = '';
        btn.style.borderColor = '';
        btn.style.color = '';
      }
    });
}

function updateTraceButtons() {
  fetch('/tracestatus').then(function(r){ return r.json(); }).then(function(d) {
    var ab = document.getElementById('btn-trace');
    var wb = document.getElementById('btn-watchdog');
    if (d.audio_trace) { ab.style.background='#0f3460'; ab.style.borderColor='#00d4ff'; ab.style.color='#00d4ff'; }
    if (d.watchdog_trace) { wb.style.background='#0f3460'; wb.style.borderColor='#00d4ff'; wb.style.color='#00d4ff'; }
  }).catch(function(){});
}
updateTraceButtons();

function escHtml(s) {
  var d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

function pollLogs() {
  fetch('/logdata?after=' + _seq)
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.seq) _seq = d.seq;
      if (d.lines && d.lines.length > 0) {
        var box = document.getElementById('log-box');
        var filter = document.getElementById('log-filter').value;
        var re = null;
        try { if (filter) re = new RegExp(filter, 'i'); } catch(e) {}
        var frag = document.createDocumentFragment();
        for (var i = 0; i < d.lines.length; i++) {
          if (re && !re.test(d.lines[i])) continue;
          var div = document.createElement('div');
          div.textContent = d.lines[i];
          // Color-code errors/warnings
          var t = d.lines[i];
          if (t.indexOf('error') >= 0 || t.indexOf('Error') >= 0 || t.indexOf('ERROR') >= 0)
            div.style.color = '#e74c3c';
          else if (t.indexOf('Warning') >= 0 || t.indexOf('warning') >= 0)
            div.style.color = '#f39c12';
          else if (t.indexOf('[CAT]') >= 0 || t.indexOf('CAT') >= 0)
            div.style.color = '#3498db';
          frag.appendChild(div);
        }
        box.appendChild(frag);
        // Cap DOM nodes to prevent memory growth
        while (box.childNodes.length > 5000) box.removeChild(box.firstChild);
        if (document.getElementById('auto-scroll').checked) {
          box.scrollTop = box.scrollHeight;
        }
      }
      document.getElementById('log-status').textContent = d.lines ? d.lines.length + ' new' : '';
      setTimeout(pollLogs, 1000);
    })
    .catch(function() {
      document.getElementById('log-status').textContent = 'offline';
      setTimeout(pollLogs, 3000);
    });
}

// Load initial backlog
fetch('/logdata?after=0')
  .then(function(r){ return r.json(); })
  .then(function(d) {
    if (d.seq) _seq = d.seq;
    if (d.lines) {
      var box = document.getElementById('log-box');
      for (var i = 0; i < d.lines.length; i++) {
        var div = document.createElement('div');
        div.textContent = d.lines[i];
        box.appendChild(div);
      }
      box.scrollTop = box.scrollHeight;
    }
    setTimeout(pollLogs, 1000);
  })
  .catch(function(){ setTimeout(pollLogs, 1000); });
</script>
'''
        return self._wrap_html('Gateway Logs', body)

    def _generate_radio_page(self):
        """Build the TH-9800 radio control HTML page."""
        body = '''
<h1 style="font-size:1.8em">TH-9800 Radio Control</h1>
<p><a href="/">Dashboard</a> | <a href="/config">Config Editor</a> | <a href="/sdr">SDR</a> | <a href="/logs">Logs</a></p>

<div id="cat-offline" style="display:none; background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px; margin-bottom:14px;">
  <span id="cat-offline-msg" style="color:#e74c3c; font-weight:bold;">CAT not connected</span>
  <button id="cat-connect-btn" onclick="catConnect()" class="rb" style="margin-left:14px;">Connect</button>
  <span id="cat-connect-status" style="color:#888; margin-left:10px; font-size:0.9em;"></span>
</div>

<div id="radio-panel" style="display:none;">

<!-- Connection + RTS Control -->
<div style="margin-bottom:14px; background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <span style="color:#888; font-size:0.85em;">TCP:</span>
  <button onclick="catDisconnect()" class="rb rb-sm" style="background:#c0392b; border-color:#e74c3c;">Disconnect</button>
  <button onclick="catReconnect()" class="rb rb-sm">Reconnect</button>
  <span style="color:#333;">|</span>
  <span style="color:#888; font-size:0.85em;">Serial:</span>
  <span id="serial-state" style="font-size:0.85em; color:#888; display:inline-block; width:110px; text-align:center;">—</span>
  <button onclick="serialDisconnect()" class="rb rb-sm" style="background:#c0392b; border-color:#e74c3c;">Disconnect</button>
  <button onclick="serialConnect()" class="rb rb-sm">Connect</button>
  <button onclick="setupRadio()" class="rb rb-sm" style="background:#2c3e50; border-color:#34495e;">Setup</button>
  <span style="color:#333;">|</span>
  <span style="color:#888; font-size:0.85em;">RTS TX:</span>
  <span id="rts-state" style="font-weight:bold;">—</span>
  <button onclick="catCmd('TOGGLE_RTS')" class="rb rb-sm">Toggle RTS</button>
</div>

<!-- Two-column VFO display -->
<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(400px, 1fr)); gap:14px; margin-bottom:14px;">

  <!-- LEFT VFO -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="color:#00d4ff; font-weight:bold; font-size:1.1em;">LEFT VFO</span>
      <span id="l-main" class="icon-badge" style="display:none;">MAIN</span>
    </div>

    <!-- Icons row -->
    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; font-size:0.8em;">
      <span id="l-enc" class="icon-off">ENC</span>
      <span id="l-dec" class="icon-off">DEC</span>
      <span id="l-pos" class="icon-off">+</span>
      <span id="l-neg" class="icon-off">-</span>
      <span id="l-tx" class="icon-off">TX</span>
      <span id="l-pref" class="icon-off">PREF</span>
      <span id="l-skip" class="icon-off">SKIP</span>
      <span id="l-dcs" class="icon-off">DCS</span>
      <span id="l-mute" class="icon-off">MUTE</span>
      <span id="l-mt" class="icon-off">MT</span>
      <span id="l-busy" class="icon-off">BUSY</span>
      <span id="l-am" class="icon-off">AM</span>
    </div>

    <!-- Channel & Frequency display -->
    <div id="l-freq-box" style="background:#0d1b2a; border:1px solid #1b3a5c; border-radius:4px; padding:10px; margin-bottom:10px; font-family:monospace; transition:background 0.2s, border-color 0.2s;">
      <div style="display:flex; justify-content:space-between; align-items:baseline;">
        <span style="color:#888; font-size:0.85em;">CH: <span id="l-ch" style="color:#2ecc71;">—</span></span>
        <span id="l-power" style="color:#f39c12; font-size:0.85em;">—</span>
      </div>
      <div id="l-freq" style="color:#2ecc71; font-size:1.8em; text-align:center; letter-spacing:2px; margin:6px 0;">———</div>
    </div>

    <!-- Signal meter -->
    <div style="margin-bottom:10px;">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="color:#888; font-size:0.85em;">S:</span>
        <div style="flex:1; background:#0d1b2a; border:1px solid #1b3a5c; border-radius:3px; height:18px; position:relative; overflow:hidden;">
          <div id="l-sig-bar" style="height:100%; background:#2ecc71; width:0%; transition:width 0.3s;"></div>
          <span id="l-sig-text" style="position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); font-size:0.75em; color:#fff; font-weight:bold;">S0</span>
        </div>
      </div>
    </div>

    <!-- Dial controls -->
    <div style="display:flex; gap:6px; justify-content:center; margin-bottom:8px;">
      <button class="rb" onclick="catCmd('L_DIAL_LEFT')">&#9664; Down</button>
      <button class="rb" onclick="catCmd('L_DIAL_PRESS')">SEL</button>
      <button class="rb" onclick="catCmd('L_DIAL_RIGHT')">Up &#9654;</button>
    </div>

    <!-- Volume & Squelch sliders -->
    <div style="margin-bottom:8px;">
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <span style="color:#888; font-size:0.85em; min-width:30px;">VOL</span>
        <input id="l-vol" type="range" min="0" max="100" value="25" style="flex:1; accent-color:#00d4ff;" oninput="catVol('LEFT',this.value)">
        <span id="l-vol-val" style="color:#ccc; font-family:monospace; font-size:0.85em; min-width:3em;">25</span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="color:#888; font-size:0.85em; min-width:30px;">SQ</span>
        <input id="l-sq" type="range" min="0" max="100" value="25" style="flex:1; accent-color:#f39c12;" oninput="catVol('LEFT_SQ',this.value)">
        <span id="l-sq-val" style="color:#ccc; font-family:monospace; font-size:0.85em; min-width:3em;">25</span>
      </div>
    </div>

    <!-- Function buttons -->
    <div style="display:flex; gap:6px; justify-content:center; flex-wrap:wrap;">
      <button class="rb" onclick="catCmd('L_LOW')">LOW</button>
      <button class="rb" onclick="catCmd('L_VM')">V/M</button>
      <button class="rb" onclick="catCmd('L_HM')">HM</button>
      <button class="rb" onclick="catCmd('L_SCN')">SCN</button>
    </div>
    <div style="display:flex; gap:6px; justify-content:center; flex-wrap:wrap; margin-top:4px; opacity:0.7;">
      <button class="rb rb-sm" onclick="catCmd('L_LOW_HOLD')">LOW2</button>
      <button class="rb rb-sm" onclick="catCmd('L_VM_HOLD')">V/M2</button>
      <button class="rb rb-sm" onclick="catCmd('L_HM_HOLD')">HM2</button>
      <button class="rb rb-sm" onclick="catCmd('L_SCN_HOLD')">SCN2</button>
    </div>
  </div>

  <!-- RIGHT VFO -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="color:#00d4ff; font-weight:bold; font-size:1.1em;">RIGHT VFO</span>
      <span id="r-main" class="icon-badge" style="display:none;">MAIN</span>
    </div>

    <!-- Icons row -->
    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; font-size:0.8em;">
      <span id="r-enc" class="icon-off">ENC</span>
      <span id="r-dec" class="icon-off">DEC</span>
      <span id="r-pos" class="icon-off">+</span>
      <span id="r-neg" class="icon-off">-</span>
      <span id="r-tx" class="icon-off">TX</span>
      <span id="r-pref" class="icon-off">PREF</span>
      <span id="r-skip" class="icon-off">SKIP</span>
      <span id="r-dcs" class="icon-off">DCS</span>
      <span id="r-mute" class="icon-off">MUTE</span>
      <span id="r-mt" class="icon-off">MT</span>
      <span id="r-busy" class="icon-off">BUSY</span>
      <span id="r-am" class="icon-off">AM</span>
    </div>

    <!-- Channel & Frequency display -->
    <div id="r-freq-box" style="background:#0d1b2a; border:1px solid #1b3a5c; border-radius:4px; padding:10px; margin-bottom:10px; font-family:monospace; transition:background 0.2s, border-color 0.2s;">
      <div style="display:flex; justify-content:space-between; align-items:baseline;">
        <span style="color:#888; font-size:0.85em;">CH: <span id="r-ch" style="color:#2ecc71;">—</span></span>
        <span id="r-power" style="color:#f39c12; font-size:0.85em;">—</span>
      </div>
      <div id="r-freq" style="color:#2ecc71; font-size:1.8em; text-align:center; letter-spacing:2px; margin:6px 0;">———</div>
    </div>

    <!-- Signal meter -->
    <div style="margin-bottom:10px;">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="color:#888; font-size:0.85em;">S:</span>
        <div style="flex:1; background:#0d1b2a; border:1px solid #1b3a5c; border-radius:3px; height:18px; position:relative; overflow:hidden;">
          <div id="r-sig-bar" style="height:100%; background:#2ecc71; width:0%; transition:width 0.3s;"></div>
          <span id="r-sig-text" style="position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); font-size:0.75em; color:#fff; font-weight:bold;">S0</span>
        </div>
      </div>
    </div>

    <!-- Dial controls -->
    <div style="display:flex; gap:6px; justify-content:center; margin-bottom:8px;">
      <button class="rb" onclick="catCmd('R_DIAL_LEFT')">&#9664; Down</button>
      <button class="rb" onclick="catCmd('R_DIAL_PRESS')">SEL</button>
      <button class="rb" onclick="catCmd('R_DIAL_RIGHT')">Up &#9654;</button>
    </div>

    <!-- Volume & Squelch sliders -->
    <div style="margin-bottom:8px;">
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <span style="color:#888; font-size:0.85em; min-width:30px;">VOL</span>
        <input id="r-vol" type="range" min="0" max="100" value="25" style="flex:1; accent-color:#00d4ff;" oninput="catVol('RIGHT',this.value)">
        <span id="r-vol-val" style="color:#ccc; font-family:monospace; font-size:0.85em; min-width:3em;">25</span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="color:#888; font-size:0.85em; min-width:30px;">SQ</span>
        <input id="r-sq" type="range" min="0" max="100" value="25" style="flex:1; accent-color:#f39c12;" oninput="catVol('RIGHT_SQ',this.value)">
        <span id="r-sq-val" style="color:#ccc; font-family:monospace; font-size:0.85em; min-width:3em;">25</span>
      </div>
    </div>

    <!-- Function buttons -->
    <div style="display:flex; gap:6px; justify-content:center; flex-wrap:wrap;">
      <button class="rb" onclick="catCmd('R_LOW')">LOW</button>
      <button class="rb" onclick="catCmd('R_VM')">V/M</button>
      <button class="rb" onclick="catCmd('R_HM')">HM</button>
      <button class="rb" onclick="catCmd('R_SCN')">SCN</button>
    </div>
    <div style="display:flex; gap:6px; justify-content:center; flex-wrap:wrap; margin-top:4px; opacity:0.7;">
      <button class="rb rb-sm" onclick="catCmd('R_LOW_HOLD')">LOW2</button>
      <button class="rb rb-sm" onclick="catCmd('R_VM_HOLD')">V/M2</button>
      <button class="rb rb-sm" onclick="catCmd('R_HM_HOLD')">HM2</button>
      <button class="rb rb-sm" onclick="catCmd('R_SCN_HOLD')">SCN2</button>
    </div>
  </div>
</div>

<!-- Common controls: Menu/SET + Single VFO | PTT -->
<div style="display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px;">
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <div style="color:#00d4ff; font-weight:bold; margin-bottom:8px;">Menu / SET</div>
    <div style="display:flex; gap:6px; justify-content:center; flex-wrap:wrap;">
      <button class="rb" onclick="catCmd('N_SET')">SET</button>
      <button class="rb" onclick="catCmd('N_SET_HOLD')">SET (Hold)</button>
      <button class="rb" onclick="catCmd('L_SET_VFO')">VFO&#8594;L</button>
      <button class="rb" onclick="catCmd('R_SET_VFO')">VFO&#8594;R</button>
      <button class="rb" onclick="catCmd('L_VOLUME_HOLD')">Single VFO</button>
    </div>
    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; font-size:0.85em;">
      <span id="c-apo" class="icon-off">APO</span>
      <span id="c-lock" class="icon-off">LOCK</span>
      <span id="c-set" class="icon-off">SET</span>
      <span id="c-key2" class="icon-off">KEY2</span>
    </div>
  </div>

  <!-- PTT -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <div style="color:#00d4ff; font-weight:bold; margin-bottom:8px;">PTT</div>
    <div style="display:flex; gap:18px; align-items:flex-start; justify-content:center;">
      <div style="display:flex; flex-direction:column; align-items:center;">
        <div style="height:10px;"></div>
        <button id="ptt-btn" class="rb" style="width:80px; height:80px; font-size:1.4em; font-weight:bold; border-radius:50%;"
          onclick="togglePTT()">PTT</button>
        <span style="color:#888; font-size:0.75em; margin-top:6px;">Toggle TX</span>
      </div>
      <div style="display:flex; flex-direction:column; align-items:center;">
        <div id="mic-level" style="width:80px; height:6px; background:#0d1b2a; border-radius:3px; margin-bottom:4px; overflow:hidden;">
          <div id="mic-level-bar" style="height:100%; width:0%; background:#2ecc71; transition:width 0.1s;"></div>
        </div>
        <button id="mic-ptt-btn" class="rb" style="width:80px; height:80px; font-size:1.4em; font-weight:bold; border-radius:50%; background:#1a3a1a; border-color:#2ecc71;"
          onclick="micPTTToggle()">MIC<br>PTT</button>
        <span id="mic-status" style="color:#888; font-size:0.75em; margin-top:6px;">Ready</span>
      </div>
    </div>
  </div>
</div>

<!-- Hyper Memories + Mic Keypad + P1-P4/UP/DOWN -->
<div style="display:grid; grid-template-columns:auto 1fr auto; gap:14px; margin-bottom:14px;">

  <!-- Hyper Memories -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 14px;">
    <div style="color:#00d4ff; font-weight:bold; margin-bottom:8px;">Hyper Memories</div>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
      <button class="rb" onclick="catCmd('HYPER_A')">A</button>
      <button class="rb" onclick="catCmd('HYPER_B')">B</button>
      <button class="rb" onclick="catCmd('HYPER_C')">C</button>
      <button class="rb" onclick="catCmd('HYPER_D')">D</button>
      <button class="rb" onclick="catCmd('HYPER_E')">E</button>
      <button class="rb" onclick="catCmd('HYPER_F')">F</button>
    </div>
  </div>

  <!-- Mic Keypad (number grid only) -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 14px;">
    <div style="color:#00d4ff; font-weight:bold; margin-bottom:8px;">Mic Keypad</div>
    <div style="display:grid; grid-template-columns:repeat(4, 50px); gap:6px; justify-content:center;">
      <button class="rb" onclick="catCmd('MIC_1')">1</button>
      <button class="rb" onclick="catCmd('MIC_2')">2</button>
      <button class="rb" onclick="catCmd('MIC_3')">3</button>
      <button class="rb" onclick="catCmd('MIC_A')">A</button>

      <button class="rb" onclick="catCmd('MIC_4')">4</button>
      <button class="rb" onclick="catCmd('MIC_5')">5</button>
      <button class="rb" onclick="catCmd('MIC_6')">6</button>
      <button class="rb" onclick="catCmd('MIC_B')">B</button>

      <button class="rb" onclick="catCmd('MIC_7')">7</button>
      <button class="rb" onclick="catCmd('MIC_8')">8</button>
      <button class="rb" onclick="catCmd('MIC_9')">9</button>
      <button class="rb" onclick="catCmd('MIC_C')">C</button>

      <button class="rb" onclick="catCmd('MIC_STAR')">*</button>
      <button class="rb" onclick="catCmd('MIC_0')">0</button>
      <button class="rb" onclick="catCmd('MIC_POUND')">#</button>
      <button class="rb" onclick="catCmd('MIC_D')">D</button>
    </div>
  </div>

  <!-- Mic Controls -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 14px;">
    <div style="color:#00d4ff; font-weight:bold; margin-bottom:8px;">Mic Controls</div>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
      <button class="rb" onclick="catCmd('MIC_P1')">P1</button>
      <button class="rb" onclick="catCmd('MIC_P2')">P2</button>
      <button class="rb" onclick="catCmd('MIC_P3')">P3</button>
      <button class="rb" onclick="catCmd('MIC_P4')">P4</button>
      <button class="rb" onclick="catCmd('MIC_UP')">&#9650; UP</button>
      <button class="rb" onclick="catCmd('MIC_DOWN')">&#9660; DN</button>
    </div>
  </div>

</div>

</div><!-- /radio-panel -->

<style>
  .rb { padding:8px 14px; border:1px solid #1b3a5c; border-radius:4px; background:#0d1b2a;
        color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.95em; min-width:44px; }
  .rb:hover { background:#1a2744; border-color:#00d4ff; }
  .rb:active { background:#0f3460; }
  .rb-sm { font-size:0.8em; padding:5px 10px; }
  .icon-off { padding:2px 6px; border-radius:3px; background:#0d1b2a; color:#555; border:1px solid #1b3a5c; }
  .icon-on { padding:2px 6px; border-radius:3px; background:#c0392b; color:#fff; border:1px solid #e74c3c; }
  .icon-badge { padding:2px 8px; border-radius:3px; background:#c0392b; color:#fff; font-size:0.85em; font-weight:bold; }
</style>

<script>
var _volTimer = {};

var _pttActive = false;

function showError(msg) {
  var el = document.getElementById('cmd-error');
  if (!el) { el = document.createElement('div'); el.id='cmd-error';
    el.style.cssText='position:fixed;top:0;left:0;right:0;padding:8px;background:#c0392b;color:#fff;text-align:center;font-weight:bold;z-index:9999;cursor:pointer';
    el.onclick=function(){el.style.display='none'};
    document.body.appendChild(el); }
  el.textContent = msg; el.style.display = 'block';
  setTimeout(function(){ el.style.display='none'; }, 5000);
}

function catCmd(cmd) {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:cmd})})
    .then(function(r){return r.json()}).then(function(d) {
      if (!d.ok && d.error) showError(d.error === 'serial not connected' ?
        'Serial not connected — press Connect first' : d.error);
    }).catch(function(){});
}

function togglePTT() {
  var btn = document.getElementById('ptt-btn');
  catCmd('MIC_PTT');
  _pttActive = !_pttActive;
  if (_pttActive) {
    btn.style.background = '#c0392b';
    btn.style.borderColor = '#e74c3c';
    btn.style.color = '#fff';
  } else {
    btn.style.background = '#0d1b2a';
    btn.style.borderColor = '#1b3a5c';
    btn.style.color = '#e0e0e0';
  }
}

// --- Browser Mic PTT ---
var _micWs = null;
var _micStream = null;
var _micCtx = null;
var _micWorklet = null;
var _micActive = false;

function micPTTToggle() {
  if (_micActive) {
    micPTTCleanup();
    return;
  }
  _micActive = true;
  var btn = document.getElementById('mic-ptt-btn');
  var st = document.getElementById('mic-status');
  btn.style.background = '#c0392b';
  btn.style.borderColor = '#e74c3c';
  btn.style.color = '#fff';
  st.textContent = 'Connecting...';
  st.style.color = '#f39c12';

  navigator.mediaDevices.getUserMedia({
    audio: {sampleRate:48000, channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}
  }).then(function(stream) {
    _micStream = stream;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _micWs = new WebSocket(proto + '//' + location.host + '/ws_mic');
    _micWs.binaryType = 'arraybuffer';
    _micWs.onopen = function() {
      st.textContent = 'TX — click to stop';
      st.style.color = '#e74c3c';
      _micCtx = new AudioContext({sampleRate: 48000});
      var source = _micCtx.createMediaStreamSource(stream);
      var proc = _micCtx.createScriptProcessor(2048, 1, 1);
      proc.onaudioprocess = function(e) {
        if (!_micWs || _micWs.readyState !== 1) return;
        var f32 = e.inputBuffer.getChannelData(0);
        var buf = new ArrayBuffer(f32.length * 2);
        var i16 = new Int16Array(buf);
        for (var i = 0; i < f32.length; i++) {
          var s = Math.max(-1, Math.min(1, f32[i]));
          i16[i] = s < 0 ? s * 32768 : s * 32767;
        }
        _micWs.send(buf);
        var peak = 0;
        for (var i = 0; i < f32.length; i++) {
          var a = Math.abs(f32[i]);
          if (a > peak) peak = a;
        }
        var pct = Math.min(100, Math.round(peak * 100));
        document.getElementById('mic-level-bar').style.width = pct + '%';
      };
      source.connect(proc);
      proc.connect(_micCtx.destination);
      _micWorklet = proc;
    };
    _micWs.onerror = function() {
      st.textContent = 'Error';
      st.style.color = '#e74c3c';
      micPTTCleanup();
    };
    _micWs.onclose = function() {
      micPTTCleanup();
    };
  }).catch(function(e) {
    st.textContent = 'Mic denied';
    st.style.color = '#e74c3c';
    _micActive = false;
    btn.style.background = '#1a3a1a';
    btn.style.borderColor = '#2ecc71';
    btn.style.color = '#e0e0e0';
  });
}

function micPTTCleanup() {
  _micActive = false;
  if (_micWorklet) { _micWorklet.disconnect(); _micWorklet = null; }
  if (_micCtx) { _micCtx.close().catch(function(){}); _micCtx = null; }
  if (_micStream) { _micStream.getTracks().forEach(function(t){t.stop();}); _micStream = null; }
  if (_micWs) { try { _micWs.close(); } catch(e){} _micWs = null; }
  var btn = document.getElementById('mic-ptt-btn');
  btn.style.background = '#1a3a1a';
  btn.style.borderColor = '#2ecc71';
  btn.style.color = '#e0e0e0';
  document.getElementById('mic-level-bar').style.width = '0%';
  var st = document.getElementById('mic-status');
  st.textContent = 'Ready';
  st.style.color = '#888';
}

function catConnect() {
  var btn = document.getElementById('cat-connect-btn');
  var st = document.getElementById('cat-connect-status');
  btn.disabled = true; btn.textContent = 'Connecting...';
  st.textContent = '';
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'CAT_RECONNECT'})})
    .then(function(r){return r.json()}).then(function(d) {
      btn.disabled = false; btn.textContent = 'Connect';
      if (!d.ok) st.textContent = d.error || 'Failed';
    }).catch(function(){ btn.disabled = false; btn.textContent = 'Connect'; });
}

function catDisconnect() {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'CAT_DISCONNECT'})});
}

function catReconnect() {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'CAT_RECONNECT'})});
}

function serialDisconnect() {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'SERIAL_DISCONNECT'})});
}

function serialConnect() {
  var el = document.getElementById('serial-state');
  el.textContent = 'connecting'; el.style.color = '#f39c12';
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'SERIAL_CONNECT'})});
}

function setupRadio() {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'SETUP_RADIO'})})
    .then(function(r){return r.json()}).then(function(d) {
      if (!d.ok) showError(d.status || 'setup failed');
    });
}

function catVol(target, value) {
  // Update display immediately
  var id = target.replace('_SQ','').toLowerCase().charAt(0);
  var isSq = target.indexOf('_SQ') >= 0;
  document.getElementById(id + (isSq ? '-sq-val' : '-vol-val')).textContent = value;
  // Debounce: send after 100ms of no change
  clearTimeout(_volTimer[target]);
  _volTimer[target] = setTimeout(function() {
    var cmd = isSq ? 'SQ_' + target.replace('_SQ','') : 'VOL_' + target;
    fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd:cmd, value:parseInt(value)})})
      .then(function(r){return r.json()}).then(function(d) {
        if (!d.ok && d.error) showError(d.error === 'serial not connected' ?
          'Serial not connected — press Connect first' : d.error);
      }).catch(function(){});
  }, 100);
}

function fmtFreq(s) {
  if (!s || s.indexOf('\\u2014') >= 0) return s;
  // Strip existing periods/spaces, keep only digits
  var d = s.replace(/[^0-9]/g, '');
  if (d.length > 3) return d.slice(0, d.length - 3) + '.' + d.slice(d.length - 3);
  return s;
}

function setIcon(id, on) {
  var el = document.getElementById(id);
  if (el) el.className = on ? 'icon-on' : 'icon-off';
}

function updateRadio() {
  fetch('/catstatus').then(function(r){return r.json()}).then(function(d) {
    if (!d.connected) {
      document.getElementById('cat-offline').style.display = 'block';
      document.getElementById('radio-panel').style.display = 'none';
      return;
    }
    document.getElementById('cat-offline').style.display = 'none';
    document.getElementById('radio-panel').style.display = 'block';

    // Serial state
    var sel = document.getElementById('serial-state');
    if (d.serial_connected) { sel.textContent = 'connected'; sel.style.color = '#2ecc71'; }
    else { sel.textContent = 'disconnected'; sel.style.color = '#e74c3c'; }

    // RTS
    var rts = document.getElementById('rts-state');
    if (d.rts_usb === true) { rts.textContent = 'USB Controlled'; rts.style.color = '#2ecc71'; }
    else if (d.rts_usb === false) { rts.textContent = 'Radio Controlled'; rts.style.color = '#e74c3c'; }
    else { rts.textContent = 'Unknown'; rts.style.color = '#888'; }

    // LEFT VFO
    var L = d.left;
    document.getElementById('l-freq').textContent = fmtFreq(L.display) || '\\u2014\\u2014\\u2014';
    document.getElementById('l-ch').textContent = L.channel || '\\u2014';
    document.getElementById('l-power').textContent = L.power ? 'PWR: ' + L.power : '';
    document.getElementById('l-sig-bar').style.width = (L.signal / 9 * 100) + '%';
    document.getElementById('l-sig-text').textContent = 'S' + L.signal;
    document.getElementById('l-main').style.display = (L.icons && L.icons.MAIN) ? 'inline' : 'none';
    var li = L.icons || {};
    setIcon('l-enc', li.ENC); setIcon('l-dec', li.DEC); setIcon('l-pos', li.POS);
    setIcon('l-neg', li.NEG); setIcon('l-tx', li.TX); setIcon('l-pref', li.PREF);
    setIcon('l-skip', li.SKIP); setIcon('l-dcs', li.DCS); setIcon('l-mute', li.MUTE);
    setIcon('l-mt', li.MT); setIcon('l-busy', li.BUSY); setIcon('l-am', li.AM);

    // PTT indication — red background on freq display when TX active
    var lBox = document.getElementById('l-freq-box');
    if (li.TX) { lBox.style.background = '#5c1a1a'; lBox.style.borderColor = '#e74c3c'; }
    else { lBox.style.background = '#0d1b2a'; lBox.style.borderColor = '#1b3a5c'; }

    // RIGHT VFO
    var R = d.right;
    document.getElementById('r-freq').textContent = fmtFreq(R.display) || '\\u2014\\u2014\\u2014';
    document.getElementById('r-ch').textContent = R.channel || '\\u2014';
    document.getElementById('r-power').textContent = R.power ? 'PWR: ' + R.power : '';
    document.getElementById('r-sig-bar').style.width = (R.signal / 9 * 100) + '%';
    document.getElementById('r-sig-text').textContent = 'S' + R.signal;
    document.getElementById('r-main').style.display = (R.icons && R.icons.MAIN) ? 'inline' : 'none';
    var ri = R.icons || {};
    setIcon('r-enc', ri.ENC); setIcon('r-dec', ri.DEC); setIcon('r-pos', ri.POS);
    setIcon('r-neg', ri.NEG); setIcon('r-tx', ri.TX); setIcon('r-pref', ri.PREF);
    setIcon('r-skip', ri.SKIP); setIcon('r-dcs', ri.DCS); setIcon('r-mute', ri.MUTE);
    setIcon('r-mt', ri.MT); setIcon('r-busy', ri.BUSY); setIcon('r-am', ri.AM);

    // PTT indication — red background on freq display when TX active
    var rBox = document.getElementById('r-freq-box');
    if (ri.TX) { rBox.style.background = '#5c1a1a'; rBox.style.borderColor = '#e74c3c'; }
    else { rBox.style.background = '#0d1b2a'; rBox.style.borderColor = '#1b3a5c'; }

    // Common icons
    var ci = d.common || {};
    setIcon('c-apo', ci.APO); setIcon('c-lock', ci.LOCK);
    setIcon('c-set', ci.SET); setIcon('c-key2', ci.KEY2);

    // Sync volume sliders from actual radio state
    if (d.volume) {
      var lv=document.getElementById('l-vol'), rv=document.getElementById('r-vol');
      var lvt=document.getElementById('l-vol-val'), rvt=document.getElementById('r-vol-val');
      if (lv && d.volume.left >= 0 && !lv.matches(':active')) { lv.value=d.volume.left; if(lvt) lvt.textContent=d.volume.left; }
      if (rv && d.volume.right >= 0 && !rv.matches(':active')) { rv.value=d.volume.right; if(rvt) rvt.textContent=d.volume.right; }
    }

  }).catch(function(){});
}

setInterval(updateRadio, 1000);
updateRadio();
</script>
'''
        return self._wrap_html('Radio Control', body)

    def _generate_sdr_page(self):
        """Build the SDR control HTML page."""
        body = '''
<h1 style="font-size:1.8em">SDR Control</h1>
<p><a href="/">Dashboard</a> | <a href="/radio">Radio</a> | <a href="/config">Config</a> | <a href="/logs">Logs</a></p>

<!-- Status bar -->
<div id="sdr-status-bar" style="display:flex; align-items:center; gap:14px; background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 16px; margin-bottom:14px;">
  <span id="sdr-proc-badge" style="padding:3px 10px; border-radius:4px; font-weight:bold; font-size:0.85em;">--</span>
  <span id="sdr-freq-display" style="font-size:2.2em; font-weight:bold; color:#00ff88; font-family:monospace; letter-spacing:2px;">---.--- MHz</span>
  <span id="sdr-mod-badge" style="padding:3px 10px; border-radius:4px; background:#0f3460; color:#00d4ff; font-weight:bold; font-size:0.9em;">--</span>
  <span id="sdr-sr-badge" style="padding:3px 10px; border-radius:4px; background:#0f3460; color:#ccc; font-size:0.85em;">SR: --</span>
  <span id="sdr-ant-badge" style="padding:3px 10px; border-radius:4px; background:#0f3460; color:#ccc; font-size:0.85em;">ANT: --</span>
</div>

<!-- Audio level bar -->
<div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:10px 16px; margin-bottom:14px;">
  <div style="display:flex; align-items:center; gap:10px;">
    <span style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Audio Level</span>
    <div style="flex:1; background:#0d1b2a; border-radius:3px; height:20px; overflow:hidden;">
      <div id="sdr-audio-bar" style="height:100%; width:0%; background:linear-gradient(90deg,#00ff88,#ffcc00,#ff4444); transition:width 0.3s;"></div>
    </div>
    <span id="sdr-audio-val" style="color:#b0b0b0; font-size:0.85em; min-width:40px; text-align:right;">0</span>
  </div>
</div>

<!-- Main controls grid -->
<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:14px; margin-bottom:14px;">

  <!-- Frequency -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Frequency</h3>
    <div style="display:flex; align-items:center; gap:6px; margin-bottom:8px;">
      <input type="number" id="sdr-freq" step="0.00125" min="0.001" max="2000" value="446.760"
             style="flex:1; background:#0d1b2a; border:1px solid #1b3a5c; color:#00ff88; padding:8px; border-radius:4px; font-family:monospace; font-size:1.3em; text-align:center;">
      <span style="color:#b0b0b0;">MHz</span>
    </div>
    <div style="display:flex; gap:4px; flex-wrap:wrap;">
      <button class="sb" onclick="stepFreq(-0.025)">-25k</button>
      <button class="sb" onclick="stepFreq(-0.0125)">-12.5k</button>
      <button class="sb" onclick="stepFreq(-0.00625)">-6.25k</button>
      <button class="sb" onclick="stepFreq(0.00625)">+6.25k</button>
      <button class="sb" onclick="stepFreq(0.0125)">+12.5k</button>
      <button class="sb" onclick="stepFreq(0.025)">+25k</button>
    </div>
  </div>

  <!-- Modulation & Sample Rate -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Modulation & Sampling</h3>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">Mode</label>
      <select id="sdr-mod" class="si">
        <option value="nfm">NFM</option>
        <option value="am">AM</option>
      </select>
    </div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">Sample Rate</label>
      <select id="sdr-sr" class="si">
        <option value="0.2">200 kHz</option>
        <option value="0.3">300 kHz</option>
        <option value="0.6">600 kHz</option>
        <option value="1.0">1.0 MHz</option>
        <option value="1.536">1.536 MHz</option>
        <option value="2.0">2.0 MHz</option>
        <option value="2.048">2.048 MHz</option>
        <option value="2.4">2.4 MHz</option>
        <option value="2.56">2.56 MHz</option>
        <option value="3.0">3.0 MHz</option>
        <option value="4.0">4.0 MHz</option>
        <option value="5.0">5.0 MHz</option>
        <option value="6.0">6.0 MHz</option>
        <option value="7.0">7.0 MHz</option>
        <option value="8.0">8.0 MHz</option>
        <option value="10.0">10.0 MHz</option>
        <option value="10.66">10.66 MHz</option>
      </select>
    </div>
    <div style="display:flex; align-items:center; gap:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">Antenna</label>
      <select id="sdr-ant" class="si">
        <option value="Tuner 1 50 ohm">Tuner 1 50 ohm</option>
        <option value="Tuner 1 Hi-Z">Tuner 1 Hi-Z</option>
        <option value="Tuner 2 50 ohm">Tuner 2 50 ohm</option>
      </select>
    </div>
  </div>

  <!-- Gain Control -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Gain Control</h3>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">Mode</label>
      <select id="sdr-gain-mode" class="si" onchange="toggleGainSliders()">
        <option value="agc">AGC (Auto)</option>
        <option value="manual">Manual</option>
      </select>
    </div>
    <div id="agc-settings" style="margin-bottom:6px;">
      <div style="display:flex; align-items:center; gap:10px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">AGC Setpoint</label>
        <input type="range" id="sdr-agc-sp" min="-72" max="0" value="-30" style="flex:1;"
               oninput="document.getElementById('agc-sp-val').textContent=this.value+' dB'">
        <span id="agc-sp-val" style="color:#b0b0b0; font-size:0.85em; min-width:55px;">-30 dB</span>
      </div>
    </div>
    <div id="manual-gain-settings" style="display:none;">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">RF Gain (RFGR)</label>
        <input type="range" id="sdr-rfgr" min="0" max="9" value="4" style="flex:1;"
               oninput="document.getElementById('rfgr-val').textContent=this.value">
        <span id="rfgr-val" style="color:#b0b0b0; font-size:0.85em; min-width:30px;">4</span>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">IF Gain (IFGR)</label>
        <input type="range" id="sdr-ifgr" min="20" max="59" value="40" style="flex:1;"
               oninput="document.getElementById('ifgr-val').textContent=this.value">
        <span id="ifgr-val" style="color:#b0b0b0; font-size:0.85em; min-width:30px;">40</span>
      </div>
    </div>
  </div>

  <!-- Squelch & Options -->
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Squelch & Device Options</h3>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:80px;">Squelch</label>
      <input type="range" id="sdr-squelch" min="-60" max="0" value="0" style="flex:1;"
             oninput="document.getElementById('sq-val').textContent=this.value==0?'Auto':this.value+' dBFS'">
      <span id="sq-val" style="color:#b0b0b0; font-size:0.85em; min-width:65px;">Auto</span>
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
      <label class="tgl"><input type="checkbox" id="sdr-biast"> Bias-T</label>
      <label class="tgl"><input type="checkbox" id="sdr-rfnotch"> RF Notch</label>
      <label class="tgl"><input type="checkbox" id="sdr-dabnotch"> DAB Notch</label>
      <label class="tgl"><input type="checkbox" id="sdr-iqcorr" checked> IQ Correction</label>
      <label class="tgl"><input type="checkbox" id="sdr-extref"> External Ref</label>
      <label class="tgl"><input type="checkbox" id="sdr-continuous" checked> Continuous</label>
    </div>
  </div>
</div>

<!-- Audio Processing -->
<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:14px; margin-bottom:14px;">
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Audio Filters</h3>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Amp Factor</label>
      <input type="number" id="sdr-ampfactor" step="0.1" min="0" max="10" value="1.0" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">1.0 = unity</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Highpass</label>
      <input type="number" id="sdr-highpass" step="10" min="0" max="1000" value="100" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">Hz (default 100)</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Lowpass</label>
      <input type="number" id="sdr-lowpass" step="100" min="500" max="8000" value="2500" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">Hz (default 2500)</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Channel BW</label>
      <input type="number" id="sdr-chbw" step="1000" min="0" max="25000" value="0" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">Hz (0 = off)</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Notch</label>
      <input type="number" id="sdr-notch" step="1" min="0" max="5000" value="0" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">Hz (0 = off)</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Notch Q</label>
      <input type="number" id="sdr-notchq" step="1" min="1" max="100" value="10" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">selectivity (default 10)</span>
    </div>
  </div>
  <div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px;">
    <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Device Tuning</h3>
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">Correction</label>
      <input type="number" id="sdr-correction" step="0.1" min="-100" max="100" value="0" class="si" style="width:80px;">
      <span style="color:#666; font-size:0.75em;">ppm</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:90px;">NFM Tau</label>
      <select id="sdr-tau" class="si">
        <option value="0">Off (no deemph)</option>
        <option value="50">50 &micro;s (US)</option>
        <option value="75">75 &micro;s (EU FM)</option>
        <option value="200" selected>200 &micro;s (default)</option>
        <option value="530">530 &micro;s</option>
        <option value="1000">1000 &micro;s</option>
      </select>
    </div>
  </div>
</div>

<!-- Apply button -->
<div style="display:flex; gap:10px; margin-bottom:14px;">
  <button id="sdr-apply-btn" onclick="applySettings()" style="flex:1; padding:12px; background:#27ae60; color:#fff; border:none; border-radius:6px; font-size:1.1em; font-weight:bold; cursor:pointer;">
    Apply & Restart SDR
  </button>
  <button onclick="sdrCmd('stop')" style="padding:12px 20px; background:#c0392b; color:#fff; border:none; border-radius:6px; font-size:1.1em; font-weight:bold; cursor:pointer;">
    Stop
  </button>
</div>
<div id="sdr-apply-status" style="color:#888; font-size:0.9em; margin-bottom:14px; min-height:1.2em;"></div>

<!-- Channel Memory -->
<div style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:14px; margin-bottom:14px;">
  <h3 style="color:#00d4ff; margin:0 0 10px; font-size:0.95em;">Channel Memory</h3>
  <div id="ch-grid" style="display:grid; grid-template-columns:repeat(5,1fr); gap:6px; margin-bottom:10px;">
  </div>
  <div style="display:flex; gap:8px; align-items:center;">
    <input type="text" id="ch-name" placeholder="Channel name" style="flex:1; background:#0d1b2a; border:1px solid #1b3a5c; color:#e0e0e0; padding:6px 10px; border-radius:4px; font-size:0.9em;">
    <select id="ch-slot" class="si" style="width:70px;">
      <option value="0">CH 0</option><option value="1">CH 1</option><option value="2">CH 2</option>
      <option value="3">CH 3</option><option value="4">CH 4</option><option value="5">CH 5</option>
      <option value="6">CH 6</option><option value="7">CH 7</option><option value="8">CH 8</option>
      <option value="9">CH 9</option>
    </select>
    <button class="sb" onclick="saveChannel()" style="background:#27ae60;">Save</button>
    <button class="sb" onclick="deleteChannel()" style="background:#c0392b;">Del</button>
  </div>
</div>

<style>
  .sb { padding:6px 12px; background:#0f3460; color:#e0e0e0; border:1px solid #1b3a5c; border-radius:4px; cursor:pointer; font-size:0.85em; }
  .sb:hover { background:#1a4a7a; }
  .sb:active { background:#27ae60; }
  .si { background:#0d1b2a; border:1px solid #1b3a5c; color:#e0e0e0; padding:6px 8px; border-radius:4px; font-size:0.9em; }
  .tgl { display:flex; align-items:center; gap:6px; color:#b0b0b0; font-size:0.85em; padding:4px 0; cursor:pointer; }
  .tgl input { width:16px; height:16px; accent-color:#00d4ff; }
  .ch-btn { width:100%; padding:10px 4px; border:1px solid #1b3a5c; border-radius:6px; cursor:pointer; font-size:0.8em; text-align:center; background:#0d1b2a; color:#888; transition:all 0.2s; }
  .ch-btn.active { background:#0f3460; color:#00ff88; border-color:#00d4ff; }
  .ch-btn.current { background:#27ae60; color:#fff; border-color:#27ae60; }
  .ch-btn:hover { border-color:#00d4ff; }
  .ch-btn .ch-freq { font-family:monospace; font-size:0.95em; }
  .ch-btn .ch-name { font-size:0.75em; color:#aaa; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
</style>

<script>
var pollTimer = null;
var currentSlot = -1;
var initialLoad = true;

function toggleGainSliders() {
  var mode = document.getElementById('sdr-gain-mode').value;
  document.getElementById('agc-settings').style.display = mode === 'agc' ? '' : 'none';
  document.getElementById('manual-gain-settings').style.display = mode === 'manual' ? '' : 'none';
}

function stepFreq(delta) {
  var el = document.getElementById('sdr-freq');
  var v = parseFloat(el.value) + delta;
  el.value = Math.max(0.001, Math.min(2000, v)).toFixed(5).replace(/0+$/, '').replace(/\\.$/, '');
}

function gatherSettings() {
  return {
    frequency: parseFloat(document.getElementById('sdr-freq').value),
    modulation: document.getElementById('sdr-mod').value,
    sample_rate: parseFloat(document.getElementById('sdr-sr').value),
    antenna: document.getElementById('sdr-ant').value,
    gain_mode: document.getElementById('sdr-gain-mode').value,
    rfgr: parseInt(document.getElementById('sdr-rfgr').value),
    ifgr: parseInt(document.getElementById('sdr-ifgr').value),
    agc_setpoint: parseInt(document.getElementById('sdr-agc-sp').value),
    squelch_threshold: parseInt(document.getElementById('sdr-squelch').value),
    correction: parseFloat(document.getElementById('sdr-correction').value),
    tau: parseInt(document.getElementById('sdr-tau').value),
    ampfactor: parseFloat(document.getElementById('sdr-ampfactor').value),
    lowpass: parseInt(document.getElementById('sdr-lowpass').value),
    highpass: parseInt(document.getElementById('sdr-highpass').value),
    notch: parseFloat(document.getElementById('sdr-notch').value),
    notch_q: parseFloat(document.getElementById('sdr-notchq').value),
    channel_bw: parseFloat(document.getElementById('sdr-chbw').value),
    bias_t: document.getElementById('sdr-biast').checked,
    rf_notch: document.getElementById('sdr-rfnotch').checked,
    dab_notch: document.getElementById('sdr-dabnotch').checked,
    iq_correction: document.getElementById('sdr-iqcorr').checked,
    external_ref: document.getElementById('sdr-extref').checked,
    continuous: document.getElementById('sdr-continuous').checked,
  };
}

function setSelectByValue(id, val) {
  // Match select option by closest numeric value (avoids "2.0" vs "2" mismatch)
  var sel = document.getElementById(id);
  var best = -1, bestDiff = 1e9;
  for (var i = 0; i < sel.options.length; i++) {
    var diff = Math.abs(parseFloat(sel.options[i].value) - parseFloat(val));
    if (diff < bestDiff) { bestDiff = diff; best = i; }
  }
  if (best >= 0 && bestDiff < 0.001) sel.selectedIndex = best;
}

function loadSettingsToUI(d) {
  if (d.frequency !== undefined) document.getElementById('sdr-freq').value = d.frequency;
  if (d.modulation) { document.getElementById('sdr-mod').value = d.modulation; }
  if (d.sample_rate !== undefined) setSelectByValue('sdr-sr', d.sample_rate);
  if (d.antenna) document.getElementById('sdr-ant').value = d.antenna;
  if (d.gain_mode) {
    document.getElementById('sdr-gain-mode').value = d.gain_mode;
    toggleGainSliders();
  }
  if (d.rfgr !== undefined) { document.getElementById('sdr-rfgr').value = d.rfgr; document.getElementById('rfgr-val').textContent = d.rfgr; }
  if (d.ifgr !== undefined) { document.getElementById('sdr-ifgr').value = d.ifgr; document.getElementById('ifgr-val').textContent = d.ifgr; }
  if (d.agc_setpoint !== undefined) { document.getElementById('sdr-agc-sp').value = d.agc_setpoint; document.getElementById('agc-sp-val').textContent = d.agc_setpoint + ' dB'; }
  if (d.squelch_threshold !== undefined) { document.getElementById('sdr-squelch').value = d.squelch_threshold; document.getElementById('sq-val').textContent = d.squelch_threshold == 0 ? 'Auto' : d.squelch_threshold + ' dBFS'; }
  if (d.bias_t !== undefined) document.getElementById('sdr-biast').checked = d.bias_t;
  if (d.rf_notch !== undefined) document.getElementById('sdr-rfnotch').checked = d.rf_notch;
  if (d.dab_notch !== undefined) document.getElementById('sdr-dabnotch').checked = d.dab_notch;
  if (d.iq_correction !== undefined) document.getElementById('sdr-iqcorr').checked = d.iq_correction;
  if (d.external_ref !== undefined) document.getElementById('sdr-extref').checked = d.external_ref;
  if (d.continuous !== undefined) document.getElementById('sdr-continuous').checked = d.continuous;
  if (d.correction !== undefined) document.getElementById('sdr-correction').value = d.correction;
  if (d.tau !== undefined) document.getElementById('sdr-tau').value = d.tau;
  if (d.ampfactor !== undefined) document.getElementById('sdr-ampfactor').value = d.ampfactor;
  if (d.lowpass !== undefined) document.getElementById('sdr-lowpass').value = d.lowpass;
  if (d.highpass !== undefined) document.getElementById('sdr-highpass').value = d.highpass;
  if (d.notch !== undefined) document.getElementById('sdr-notch').value = d.notch;
  if (d.notch_q !== undefined) document.getElementById('sdr-notchq').value = d.notch_q;
  if (d.channel_bw !== undefined) document.getElementById('sdr-chbw').value = d.channel_bw;
}

function applySettings() {
  var btn = document.getElementById('sdr-apply-btn');
  var status = document.getElementById('sdr-apply-status');
  btn.disabled = true;
  btn.textContent = 'Applying...';
  status.textContent = 'Restarting SDR (takes ~5s)...';
  status.style.color = '#ffcc00';
  var settings = gatherSettings();
  settings.cmd = 'tune';
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(settings) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.disabled = false;
      btn.textContent = 'Apply & Restart SDR';
      if (d.ok) {
        status.textContent = 'Applied successfully';
        status.style.color = '#00ff88';
        initialLoad = true;  // Refresh form from server state
      } else {
        status.textContent = 'Error: ' + (d.error || 'unknown');
        status.style.color = '#ff4444';
      }
      setTimeout(function() { status.textContent = ''; }, 5000);
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = 'Apply & Restart SDR';
      status.textContent = 'Network error';
      status.style.color = '#ff4444';
    });
}

function sdrCmd(cmd) {
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: cmd}) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var status = document.getElementById('sdr-apply-status');
      status.textContent = d.ok ? cmd + ' OK' : 'Error: ' + (d.error || '');
      status.style.color = d.ok ? '#00ff88' : '#ff4444';
      setTimeout(function() { status.textContent = ''; }, 3000);
    });
}

function buildChannelGrid(channels) {
  var grid = document.getElementById('ch-grid');
  grid.innerHTML = '';
  for (var i = 0; i < 10; i++) {
    var ch = channels && channels[i] ? channels[i] : null;
    var btn = document.createElement('div');
    btn.className = 'ch-btn' + (ch ? ' active' : '') + (i === currentSlot ? ' current' : '');
    btn.setAttribute('data-slot', i);
    if (ch) {
      btn.innerHTML = '<div class="ch-freq">' + parseFloat(ch.frequency).toFixed(3) + '</div><div class="ch-name">' + (ch.name || 'CH ' + i) + '</div><div style="font-size:0.7em;color:#888;">' + (ch.modulation || '').toUpperCase() + '</div>';
    } else {
      btn.innerHTML = '<div style="color:#555;">CH ' + i + '</div><div style="font-size:0.7em;color:#444;">Empty</div>';
    }
    btn.onclick = (function(slot, data) {
      return function() {
        if (data) {
          recallChannel(slot);
        } else {
          document.getElementById('ch-slot').value = slot;
        }
      };
    })(i, ch);
    grid.appendChild(btn);
  }
}

function saveChannel() {
  var slot = parseInt(document.getElementById('ch-slot').value);
  var name = document.getElementById('ch-name').value || ('CH ' + slot);
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: 'save_channel', slot: slot, name: name}) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var status = document.getElementById('sdr-apply-status');
      if (d.ok) {
        status.textContent = 'Saved to CH ' + slot;
        status.style.color = '#00ff88';
        currentSlot = slot;
      } else {
        status.textContent = 'Save failed: ' + (d.error || '');
        status.style.color = '#ff4444';
      }
      setTimeout(function() { status.textContent = ''; }, 3000);
    });
}

function recallChannel(slot) {
  var status = document.getElementById('sdr-apply-status');
  status.textContent = 'Recalling CH ' + slot + '...';
  status.style.color = '#ffcc00';
  currentSlot = slot;
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: 'recall_channel', slot: slot}) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        status.textContent = 'Recalled CH ' + slot;
        status.style.color = '#00ff88';
        initialLoad = true;  // Force reload of form from server
      } else {
        status.textContent = 'Recall failed: ' + (d.error || '');
        status.style.color = '#ff4444';
      }
      setTimeout(function() { status.textContent = ''; }, 3000);
    });
}

function deleteChannel() {
  var slot = parseInt(document.getElementById('ch-slot').value);
  if (!confirm('Delete CH ' + slot + '?')) return;
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: 'delete_channel', slot: slot}) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var status = document.getElementById('sdr-apply-status');
      status.textContent = d.ok ? 'Deleted CH ' + slot : 'Error';
      status.style.color = d.ok ? '#00ff88' : '#ff4444';
      if (currentSlot === slot) currentSlot = -1;
      setTimeout(function() { status.textContent = ''; }, 3000);
    });
}

function pollStatus() {
  fetch('/sdrstatus')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      // Process badge
      var badge = document.getElementById('sdr-proc-badge');
      if (d.process_alive) {
        badge.textContent = 'RUNNING';
        badge.style.background = '#27ae60';
        badge.style.color = '#fff';
      } else {
        badge.textContent = 'STOPPED';
        badge.style.background = '#c0392b';
        badge.style.color = '#fff';
      }
      // Frequency display
      if (d.frequency !== undefined) {
        document.getElementById('sdr-freq-display').textContent = parseFloat(d.frequency).toFixed(3) + ' MHz';
      }
      // Badges
      if (d.modulation) document.getElementById('sdr-mod-badge').textContent = d.modulation.toUpperCase();
      if (d.sample_rate !== undefined) document.getElementById('sdr-sr-badge').textContent = 'SR: ' + d.sample_rate + ' MHz';
      if (d.antenna) document.getElementById('sdr-ant-badge').textContent = d.antenna;
      // Audio level
      var lvl = d.audio_level || 0;
      var pct = Math.min(100, Math.max(0, Math.round(lvl)));
      document.getElementById('sdr-audio-bar').style.width = pct + '%';
      document.getElementById('sdr-audio-val').textContent = pct + '%';
      // Load current settings into form only on first load
      if (initialLoad) {
        loadSettingsToUI(d);
        initialLoad = false;
      }
      // Channel grid
      buildChannelGrid(d.channels);
      // Error display
      if (d.error) {
        document.getElementById('sdr-apply-status').textContent = d.error;
        document.getElementById('sdr-apply-status').style.color = '#ff4444';
      }
    })
    .catch(function() {});
}

// Initial poll and start timer
pollStatus();
pollTimer = setInterval(pollStatus, 1000);
</script>
'''
        return self._wrap_html('SDR Control', body)

    def _get_sysinfo(self):
        """Gather system status: CPU, memory, disk I/O, network, temps, IPs."""
        import os
        info = {}
        try:
            # CPU usage — average across cores from /proc/stat delta
            if not hasattr(self, '_prev_cpu'):
                self._prev_cpu = None
            with open('/proc/stat', 'r') as f:
                line = f.readline()  # cpu  user nice system idle iowait irq softirq ...
            parts = line.split()
            cur = [int(x) for x in parts[1:8]]
            if self._prev_cpu:
                d = [c - p for c, p in zip(cur, self._prev_cpu)]
                total = sum(d) or 1
                idle = d[3] + d[4]  # idle + iowait
                info['cpu_pct'] = round(100.0 * (total - idle) / total, 1)
            else:
                info['cpu_pct'] = 0.0
            self._prev_cpu = cur

            # Per-core CPU count
            info['cpu_cores'] = os.cpu_count() or 1

            # Load average
            load1, load5, load15 = os.getloadavg()
            info['load'] = [round(load1, 2), round(load5, 2), round(load15, 2)]
        except Exception:
            info['cpu_pct'] = 0.0
            info['cpu_cores'] = 1
            info['load'] = [0, 0, 0]

        try:
            # Memory from /proc/meminfo
            mem = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts[0].rstrip(':') in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                        mem[parts[0].rstrip(':')] = int(parts[1])  # kB
            total = mem.get('MemTotal', 0)
            avail = mem.get('MemAvailable', 0)
            used = total - avail
            info['mem_total_mb'] = round(total / 1024)
            info['mem_used_mb'] = round(used / 1024)
            info['mem_pct'] = round(100.0 * used / total, 1) if total else 0
            swap_total = mem.get('SwapTotal', 0)
            swap_free = mem.get('SwapFree', 0)
            info['swap_total_mb'] = round(swap_total / 1024)
            info['swap_used_mb'] = round((swap_total - swap_free) / 1024)
        except Exception:
            info['mem_total_mb'] = 0
            info['mem_used_mb'] = 0
            info['mem_pct'] = 0
            info['swap_total_mb'] = 0
            info['swap_used_mb'] = 0

        try:
            # Disk I/O from /proc/diskstats delta
            if not hasattr(self, '_prev_disk'):
                self._prev_disk = None
                self._prev_disk_time = 0
            now = time.monotonic()
            disk_r = 0
            disk_w = 0
            cur_disk = {}
            import re as _re
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    parts = line.split()
                    name = parts[2]
                    # Only count whole disks (sda, nvme0n1, mmcblk0) not partitions
                    if name.startswith('loop') or name.startswith('ram'):
                        continue
                    # Skip partitions: sdXN, nvme0n1pN, mmcblk0pN
                    if _re.match(r'^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)$', name):
                        # sectors read (field 5, idx 5), sectors written (field 9, idx 9)
                        rd = int(parts[5])
                        wr = int(parts[9])
                        cur_disk[name] = (rd, wr)
            if self._prev_disk and (now - self._prev_disk_time) > 0:
                dt = now - self._prev_disk_time
                for name in cur_disk:
                    if name in self._prev_disk:
                        dr = cur_disk[name][0] - self._prev_disk[name][0]
                        dw = cur_disk[name][1] - self._prev_disk[name][1]
                        disk_r += dr * 512  # sectors are 512 bytes
                        disk_w += dw * 512
                disk_r = disk_r / dt
                disk_w = disk_w / dt
            self._prev_disk = cur_disk
            self._prev_disk_time = now
            info['disk_read_bps'] = round(disk_r)
            info['disk_write_bps'] = round(disk_w)
        except Exception:
            info['disk_read_bps'] = 0
            info['disk_write_bps'] = 0

        try:
            # Disk usage for root filesystem
            st = os.statvfs('/')
            total_bytes = st.f_frsize * st.f_blocks
            free_bytes = st.f_frsize * st.f_bavail
            used_bytes = total_bytes - free_bytes
            info['disk_total_gb'] = round(total_bytes / (1024**3), 1)
            info['disk_used_gb'] = round(used_bytes / (1024**3), 1)
            info['disk_pct'] = round(100.0 * used_bytes / total_bytes, 1) if total_bytes else 0
        except Exception:
            info['disk_total_gb'] = 0
            info['disk_used_gb'] = 0
            info['disk_pct'] = 0

        try:
            # Network I/O from /proc/net/dev delta
            if not hasattr(self, '_prev_net'):
                self._prev_net = None
                self._prev_net_time = 0
            now = time.monotonic()
            cur_net = {}
            with open('/proc/net/dev', 'r') as f:
                for line in f:
                    if ':' not in line:
                        continue
                    iface, rest = line.split(':', 1)
                    iface = iface.strip()
                    if iface == 'lo':
                        continue
                    parts = rest.split()
                    rx_bytes = int(parts[0])
                    tx_bytes = int(parts[8])
                    cur_net[iface] = (rx_bytes, tx_bytes)
            net_rx = 0
            net_tx = 0
            if self._prev_net and (now - self._prev_net_time) > 0:
                dt = now - self._prev_net_time
                for iface in cur_net:
                    if iface in self._prev_net:
                        net_rx += cur_net[iface][0] - self._prev_net[iface][0]
                        net_tx += cur_net[iface][1] - self._prev_net[iface][1]
                net_rx = net_rx / dt
                net_tx = net_tx / dt
            self._prev_net = cur_net
            self._prev_net_time = now
            info['net_rx_bps'] = round(net_rx)
            info['net_tx_bps'] = round(net_tx)
        except Exception:
            info['net_rx_bps'] = 0
            info['net_tx_bps'] = 0

        try:
            # TCP connection count
            count = 0
            with open('/proc/net/tcp', 'r') as f:
                for line in f:
                    if line.strip().startswith('sl'):
                        continue
                    count += 1
            with open('/proc/net/tcp6', 'r') as f:
                for line in f:
                    if line.strip().startswith('sl'):
                        continue
                    count += 1
            info['tcp_connections'] = count
        except Exception:
            info['tcp_connections'] = 0

        try:
            # Temperatures from /sys/class/thermal or /sys/class/hwmon
            temps = []
            # thermal zones
            import glob as _glob
            for tz in sorted(_glob.glob('/sys/class/thermal/thermal_zone*/temp')):
                try:
                    zone_dir = os.path.dirname(tz)
                    with open(tz, 'r') as f:
                        val = int(f.read().strip()) / 1000.0
                    label = 'CPU'
                    type_file = os.path.join(zone_dir, 'type')
                    if os.path.exists(type_file):
                        with open(type_file, 'r') as f:
                            label = f.read().strip()
                    if val > 0:
                        temps.append({'label': label, 'temp': round(val, 1)})
                except Exception:
                    pass
            # hwmon sensors (for GPU, NVMe, etc.)
            for hwmon in sorted(_glob.glob('/sys/class/hwmon/hwmon*')):
                try:
                    name_file = os.path.join(hwmon, 'name')
                    hw_name = ''
                    if os.path.exists(name_file):
                        with open(name_file, 'r') as f:
                            hw_name = f.read().strip()
                    for tf in sorted(_glob.glob(os.path.join(hwmon, 'temp*_input'))):
                        with open(tf, 'r') as f:
                            val = int(f.read().strip()) / 1000.0
                        # Try to find a label
                        label_file = tf.replace('_input', '_label')
                        lbl = hw_name
                        if os.path.exists(label_file):
                            with open(label_file, 'r') as f:
                                lbl = f.read().strip()
                        if val > 0 and not any(t['label'] == lbl and t['temp'] == round(val, 1) for t in temps):
                            temps.append({'label': lbl, 'temp': round(val, 1)})
                except Exception:
                    pass
            info['temps'] = temps
        except Exception:
            info['temps'] = []

        try:
            # IP addresses
            import socket
            ips = []
            # Get all interface addresses via /proc/net/if_inet6 and ip command
            import subprocess
            result = subprocess.run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True, text=True, timeout=2)
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split()
                # Format: idx iface inet addr/prefix ...
                iface = parts[1]
                if iface == 'lo':
                    continue
                addr = parts[3].split('/')[0]
                ips.append({'iface': iface, 'addr': addr})
            info['ips'] = ips

            # Hostname
            info['hostname'] = socket.gethostname()
        except Exception:
            info['ips'] = []
            info['hostname'] = ''

        return info

    def _generate_dashboard(self):
        """Build the live status dashboard HTML page."""
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        body = '''
<h1 style="font-size:1.8em">Radio Gateway Dashboard</h1>
<p style="margin:0 0 14px;font-size:1.1em"><a href="/config">Config Editor</a> | <a href="/radio">Radio Control</a> | <a href="/sdr">SDR</a> | <a href="/logs">Logs</a></p>

<div class="ctrl-group" id="listen-top">
  <h3>Listen</h3>
  <div style="display:flex; gap:16px; flex-wrap:nowrap;">
    <div style="width:140px;">
      <div style="display:flex; align-items:center; gap:4px;">
        <button id="play-btn" onclick="toggleStream()" style="width:62px; text-align:center;">&#9654; MP3</button>
        <input id="vol-slider" type="range" min="0" max="100" value="100" style="width:50px; accent-color:#00d4ff;" oninput="setVolume(this.value)">
        <span id="stream-indicator" style="display:none; width:8px; height:8px; border-radius:50%; background:#2ecc71; box-shadow:0 0 6px #2ecc71;"></span>
      </div>
      <div id="stream-status" style="color:#888; font-size:0.75em; margin-top:3px; min-height:1.1em;"></div>
    </div>
    <div style="width:140px;">
      <div style="display:flex; align-items:center; gap:4px;">
        <button id="ws-btn" onclick="toggleWS()" style="width:62px; text-align:center;">&#9654; PCM</button>
        <input id="ws-vol" type="range" min="0" max="100" value="100" style="width:50px; accent-color:#00d4ff;" oninput="setWSVol(this.value)">
        <span id="ws-indicator" style="display:none; width:8px; height:8px; border-radius:50%; background:#00d4ff; box-shadow:0 0 6px #00d4ff;"></span>
      </div>
      <div id="ws-status" style="color:#888; font-size:0.75em; margin-top:3px; min-height:1.1em;"></div>
    </div>
  </div>
</div>

<div id="status">Loading...</div>

<div id="sysinfo" style="background:#16213e; border:1px solid #0f3460; border-radius:6px; padding:18px; font-family:monospace; font-size:1.0em; margin-top:10px;">Loading...</div>

<div class="controls">
  <div class="ctrl-group">
    <h3>Mute Controls</h3>
    <button onclick="sendKey('t')" id="btn-t">TX</button>
    <button onclick="sendKey('r')" id="btn-r">RX</button>
    <button onclick="sendKey('m')" id="btn-m">Global</button>
    <button onclick="sendKey('s')" id="btn-s">SDR1</button>
    <button onclick="sendKey('x')" id="btn-x">SDR2</button>
    <button onclick="sendKey('c')" id="btn-c">Remote</button>
    <button onclick="sendKey('a')" id="btn-a">Announce</button>
    <button onclick="sendKey('o')" id="btn-o">Speaker</button>
  </div>
  <div class="ctrl-group">
    <h3>Radio Processing</h3>
    <button onclick="togProc('radio','gate')" id="btn-rp-gate">Gate</button>
    <button onclick="togProc('radio','hpf')" id="btn-rp-hpf">HPF</button>
    <button onclick="togProc('radio','lpf')" id="btn-rp-lpf">LPF</button>
    <button onclick="togProc('radio','notch')" id="btn-rp-notch">Notch</button>
    <button onclick="togProc('radio','deesser')" id="btn-rp-deesser">DeEss</button>
    <button onclick="togProc('radio','spectral')" id="btn-rp-spectral">Spectral</button>
  </div>
  <div class="ctrl-group">
    <h3>Audio</h3>
    <button onclick="sendKey('v')" id="btn-v">VAD Toggle</button>
    <button onclick="sendKey(',')">,  Vol-</button>
    <button onclick="sendKey('.')">. Vol+</button>
  </div>
  <!-- Broadcastify moved to bottom row -->
  <div class="ctrl-group">
    <h3>SDR Processing</h3>
    <button onclick="togProc('sdr','gate')" id="btn-sp-gate">Gate</button>
    <button onclick="togProc('sdr','hpf')" id="btn-sp-hpf">HPF</button>
    <button onclick="togProc('sdr','lpf')" id="btn-sp-lpf">LPF</button>
    <button onclick="togProc('sdr','notch')" id="btn-sp-notch">Notch</button>
    <button onclick="togProc('sdr','deesser')" id="btn-sp-deesser">DeEss</button>
    <button onclick="togProc('sdr','spectral')" id="btn-sp-spectral">Spectral</button>
  </div>
  <div class="ctrl-group">
    <h3>SDR</h3>
    <button onclick="sendKey('d')" id="btn-d">Duck Toggle</button>
    <button onclick="sendKey('b')" id="btn-b">Rebroadcast</button>
  </div>
  <div class="ctrl-group" style="display:none;">
    <h3>Playback (moved)</h3>
  </div>
  <div class="ctrl-group" style="display:none;">
    <h3>Smart Announce (moved)</h3>
  </div>
  </div>

<div id="playback-section" style="margin-top:18px; display:flex; flex-wrap:wrap; gap:14px; align-items:flex-start;">
  <div class="ctrl-group" style="min-width:0; display:inline-block;">
    <h3 style="margin:0 0 10px; color:#00d4ff; font-size:1.1em;">Playback</h3>
    <div style="display:flex; gap:18px; flex-wrap:nowrap;">
      <div>
        <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:3px;">
          <button onclick="sendKey('1')" id="btn-f1">1</button>
          <button onclick="sendKey('2')" id="btn-f2">2</button>
          <button onclick="sendKey('3')" id="btn-f3">3</button>
          <button onclick="sendKey('4')" id="btn-f4">4</button>
          <button onclick="sendKey('5')" id="btn-f5">5</button>
          <button onclick="sendKey('6')" id="btn-f6">6</button>
          <button onclick="sendKey('7')" id="btn-f7">7</button>
          <button onclick="sendKey('8')" id="btn-f8">8</button>
          <button onclick="sendKey('9')" id="btn-f9">9</button>
          <button onclick="sendKey('0')" id="btn-f0">ID</button>
          <button onclick="sendKey('-')">Stop</button>
          <button onclick="refreshSounds()" title="Refresh random sounds">&#x21bb;</button>
        </div>
      </div>
      <div style="margin-left:6px; margin-right:6px;">
        <div id="playback-status" style="font-family:monospace; font-size:0.85em;">Loading...</div>
      </div>
    </div>
  </div>
  <div class="ctrl-group bottom-btns" style="min-width:0;">
    <h3>Smart Announce</h3>
    <div style="display:flex; flex-direction:column; gap:3px; margin-bottom:6px;">
      <button onclick="sendKey('[')">Smart #1</button>
      <button onclick="sendKey(']')">Smart #2</button>
      <button onclick="sendKey(String.fromCharCode(92))">Smart #3</button>
    </div>
    <div id="smart-status" style="font-family:monospace; font-size:0.85em; color:#888;">Idle</div>
  </div>
  <div class="ctrl-group" style="min-width:280px; width:280px;">
    <h3>Text to Speech</h3>
    <div style="display:flex; flex-direction:column; gap:3px;">
      <textarea id="tts-text" rows="3" style="width:100%; box-sizing:border-box; background:#0d1b2a; color:#e0e0e0; border:1px solid #1b3a5c; border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em; resize:vertical;" placeholder="Enter text to speak..."></textarea>
      <div style="display:flex; gap:3px; align-items:center;">
        <select id="tts-voice" style="flex:1; background:#0d1b2a; color:#e0e0e0; border:1px solid #1b3a5c; border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em;">
          <option value="1">US English</option>
          <option value="2">British</option>
          <option value="3">Australian</option>
          <option value="4">Indian</option>
          <option value="5">South African</option>
          <option value="6">Canadian</option>
          <option value="7">Irish</option>
          <option value="8">French</option>
          <option value="9">German</option>
        </select>
        <button onclick="sendTTS()" id="btn-tts-send" style="flex:1;">Send</button>
      </div>
    </div>
    <div id="tts-status" style="font-family:monospace; font-size:0.85em; color:#888; margin-top:6px;">Ready</div>
  </div>
  <div class="ctrl-group bottom-btns" style="min-width:0;" id="broadcastify-group">
    <h3>Broadcastify</h3>
    <div style="display:flex; flex-direction:column; gap:3px;">
      <button onclick="darkiceCmd('start')" id="btn-bc-start">Start</button>
      <button onclick="darkiceCmd('stop')" id="btn-bc-stop">Stop</button>
      <button onclick="darkiceCmd('restart')" id="btn-bc-restart">Restart</button>
    </div>
    <div style="margin-top:8px;">
      <span id="bc-status" style="font-family:monospace; font-size:0.85em;">...</span>
    </div>
  </div>
  <div class="ctrl-group bottom-btns" style="min-width:0;">
    <h3>PTT &amp; Relay</h3>
    <div style="display:flex; flex-direction:column; gap:3px;">
      <button onclick="sendKey('p')" id="btn-p">Manual PTT</button>
      <button onclick="sendKey('j')" id="btn-j">Radio Power</button>
      <button onclick="sendKey('h')" id="btn-h">Charger Toggle</button>
    </div>
  </div>
  <div class="ctrl-group bottom-btns" style="min-width:0;">
    <h3>System</h3>
    <div style="display:flex; flex-direction:column; gap:3px;">
      <button onclick="sendKey('@')">Send Email</button>
      <button onclick="if(confirm('Restart gateway?'))sendKey('q')" class="btn-restart">Restart</button>
      <button onclick="if(confirm('Exit the gateway server? This will stop all services.')){fetch('/exit',{method:'POST'}).then(()=>{document.body.innerHTML='<h1 style=&quot;color:#e0e0e0;text-align:center;margin-top:40vh&quot;>Gateway stopped.</h1>';});}" class="btn-exit">Exit Server</button>
    </div>
  </div>
</div>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 18px; }
  .ctrl-group { background: #16213e; border: 1px solid #0f3460; border-radius: 6px; padding: 14px; min-width: 220px; }
  .ctrl-group h3 { margin: 0 0 10px; color: #00d4ff; font-size: 1.1em; }
  .ctrl-group button { padding: 10px 18px; margin: 3px; border: 1px solid #1b3a5c; border-radius: 4px;
    background: #0d1b2a; color: #e0e0e0; cursor: pointer; font-family: monospace; font-size: 1.05em; }
  .ctrl-group button:hover { background: #1a2744; }
  .ctrl-group button:active { background: #0f3460; }
  .ctrl-group button.active { background: #0f3460; border-color: #00d4ff; color: #00d4ff; }
  .ctrl-group button.muted { background: #5c1a1a; border-color: #c0392b; color: #ff6b6b; }
  #status { background: #16213e; border: 1px solid #0f3460; border-radius: 6px; padding: 18px; font-family: monospace; font-size: 1.0em; }
  .st-row { display: grid; grid-template-columns: repeat(auto-fill, 240px); gap: 10px 16px; margin: 8px 0; }
  .st-item { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
  .st-label { color: #888; display: inline-block; width: 5.5em; text-align: right; margin-right: 16px; flex-shrink: 0; }
  .st-val { font-weight: bold; }
  .bar { display: inline-block; height: 18px; border-radius: 3px; min-width: 4px; vertical-align: middle; }
  .bar-rx { background: #2ecc71; } .bar-tx { background: #e74c3c; }
  .bar-sdr1 { background: #00d4ff; } .bar-sdr2 { background: #e056a0; }
  .bar-sv { background: #f1c40f; } .bar-cl { background: #2ecc71; }
  .bar-sp { background: #00d4ff; } .bar-an { background: #e74c3c; }
  .bar-pct { display: inline-block; width: 3.5em; text-align: right; color: #ccc; }
  .green { color: #2ecc71; } .red { color: #e74c3c; } .yellow { color: #f39c12; }
  .cyan { color: #00d4ff; } .white { color: #e0e0e0; }
  #playback-section button { margin: 0; }
  .bottom-btns { min-width: 160px; width: 160px; }
  .bottom-btns button { width: 100%; box-sizing: border-box; }
  .pb-slot { display: block; margin: 1px 0; white-space: nowrap; }
  .pb-key { font-weight: bold; display: inline-block; width: 1.5em; }
</style>

<script>
function sendKey(k) {
  fetch('/key', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:k})});
  if(document.activeElement) document.activeElement.blur();
}
function togProc(source, filter) {
  fetch('/proc_toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source:source, filter:filter})});
}
function darkiceCmd(cmd) {
  fetch('/darkicecmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cmd:cmd})});
}
function sendTTS() {
  var text = document.getElementById('tts-text').value.trim();
  if (!text) return;
  var voice = document.getElementById('tts-voice').value;
  var btn = document.getElementById('btn-tts-send');
  var st = document.getElementById('tts-status');
  btn.disabled = true;
  st.textContent = 'Generating...';
  st.style.color = '#f1c40f';
  fetch('/tts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:text, voice:parseInt(voice)})})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.ok) { st.textContent = 'Sent — playing'; st.style.color = '#2ecc71'; }
      else { st.textContent = 'Error: ' + (d.error||'failed'); st.style.color = '#e74c3c'; }
      btn.disabled = false;
      setTimeout(function(){ st.textContent = 'Ready'; st.style.color = '#888'; }, 5000);
    })
    .catch(function(e){ st.textContent = 'Network error'; st.style.color = '#e74c3c'; btn.disabled = false; });
  if(document.activeElement) document.activeElement.blur();
}
function refreshSounds() {
  fetch('/refreshsounds', {method:'POST'}).then(function(r){return r.json()}).then(function(d){
    if(d.ok) { var n=d.count; alert('Refreshed ' + n + ' sound' + (n!=1?'s':'')); }
  });
}

function bar(pct, cls) {
  var w = Math.round(Math.min(Math.max(pct, 0), 100));
  var p = pct < 10 ? '  '+pct : pct < 100 ? ' '+pct : ''+pct;
  return '<span class="bar-pct">'+p+'%</span><span class="bar '+cls+'" style="width:'+w+'px"></span>';
}

function updateStatus() {
  fetch('/status').then(r=>r.json()).then(function(s) {
    // Audio levels — same order as console: TX RX SP SDR1 SDR2 SV/CL AN
    var h = '<div class="st-row audio-row">';
    h += '<div class="st-item"><span class="st-label">TX:</span>'+bar(s.radio_tx,'bar-tx')+'</div>';
    h += '<div class="st-item"><span class="st-label">RX:</span>'+bar(s.radio_rx,'bar-rx')+'</div>';
    if(s.speaker_enabled) h += '<div class="st-item"><span class="st-label">SP:</span>'+bar(s.speaker_level,'bar-sp')+'</div>';
    if(s.sdr1_enabled) h += '<div class="st-item"><span class="st-label">SDR1:</span>'+bar(s.sdr1_level,'bar-sdr1')+'</div>';
    if(s.sdr2_enabled) h += '<div class="st-item"><span class="st-label">SDR2:</span>'+bar(s.sdr2_level,'bar-sdr2')+'</div>';
    if(s.remote_enabled) h += '<div class="st-item"><span class="st-label">'+s.remote_mode+':</span>'+bar(s.remote_level, s.remote_mode==='SV'?'bar-sv':'bar-cl')+'</div>';
    if(s.announce_enabled) h += '<div class="st-item"><span class="st-label">AN:</span>'+bar(s.an_level,'bar-an')+'</div>';
    h += '</div>';

    h += '<div class="st-row info-row">';
    h += '<div class="st-item"><span class="st-label">Mumble:</span><span class="st-val '+(s.mumble?'green':'red')+'">'+(s.mumble?'OK':'DOWN')+'</span></div>';
    h += '<div class="st-item"><span class="st-label">PTT:</span><span class="st-val '+(s.ptt_active?'red':'green')+'">'+(s.ptt_active?'ON':'off')+'</span> <span class="st-label">('+s.ptt_method+')</span></div>';
    h += '<div class="st-item"><span class="st-label">VAD:</span><span class="st-val '+(s.vad_enabled?'green':'red')+'">'+(s.vad_enabled?'ON':'off')+'</span> <span class="st-val yellow">'+s.vad_db+'dB</span></div>';
    h += '<div class="st-item"><span class="st-label">Vol:</span><span class="st-val yellow">'+s.volume+'x</span></div>';
    if(s.radio_proc && s.radio_proc.length) h += '<div class="st-item"><span class="st-label">Radio:</span><span class="st-val yellow">['+s.radio_proc.join(',')+']</span></div>';
    if(s.sdr_proc && s.sdr_proc.length) h += '<div class="st-item"><span class="st-label">SDR:</span><span class="st-val cyan">['+s.sdr_proc.join(',')+']</span></div>';
    var mutes = [];
    if(s.tx_muted) mutes.push('TX');
    if(s.rx_muted) mutes.push('RX');
    if(s.sdr1_muted) mutes.push('SDR1');
    if(s.sdr2_muted) mutes.push('SDR2');
    if(s.remote_muted) mutes.push('Remote');
    if(s.announce_muted) mutes.push('Announce');
    if(s.speaker_muted && s.speaker_enabled) mutes.push('Speaker');
    h += '<div class="st-item"><span class="st-label">Muted:</span><span class="st-val '+(mutes.length?'red':'green')+'">'+(mutes.length?mutes.join(', '):'None')+'</span></div>';
    if(s.sdr1_duck) h += '<div class="st-item"><span class="st-label">Duck:</span><span class="st-val green">ON</span></div>';
    if(s.sdr_rebroadcast) h += '<div class="st-item"><span class="st-label">Rebroadcast:</span><span class="st-val yellow">ON</span></div>';
    h += '<div class="st-item"><span class="st-label">Manual PTT:</span><span class="st-val '+(s.manual_ptt?'red':'green')+'">'+(s.manual_ptt?'ON':'off')+'</span></div>';
    if(s.ms1_state) h += '<div class="st-item"><span class="st-label">MS1:</span><span class="st-val '+(s.ms1_state==='running'?'green':s.ms1_state==='error'?'red':'white')+'">'+(s.ms1_state==='running'?'ON':'OFF')+'</span></div>';
    if(s.ms2_state) h += '<div class="st-item"><span class="st-label">MS2:</span><span class="st-val '+(s.ms2_state==='running'?'green':s.ms2_state==='error'?'red':'white')+'">'+(s.ms2_state==='running'?'ON':'OFF')+'</span></div>';
    if(s.cat_enabled) h += '<div class="st-item"><span class="st-label">CAT:</span><span class="st-val '+(s.cat==='active'?'red':s.cat==='idle'?'green':'white')+'">'+(s.cat==='active'||s.cat==='idle'?'ON':'OFF')+'</span></div>';
    h += '<div class="st-item"><span class="st-label">PWRB:</span><span class="st-val '+(s.relay_pressing?'red':'green')+'">'+(s.relay_pressing?'ON':'off')+'</span></div>';
    h += '</div>';

    // Timers row: uptime + smart announce countdowns
    h += '<div class="st-row timer-row">';
    h += '<div class="st-item"><span class="st-label">Uptime:</span><span class="st-val cyan">'+s.uptime+'</span></div>';
    for(var i=0;i<s.smart_countdowns.length;i++) {
      h += '<div class="st-item"><span class="st-label">Smart#'+s.smart_countdowns[i].id+':</span><span class="st-val yellow">'+s.smart_countdowns[i].remaining+'</span></div>';
    }
    if(s.ddns) h += '<div class="st-item"><span class="st-label">DNS:</span><span class="st-val green">'+s.ddns+'</span></div>';
    if(s.charger) h += '<div class="st-item"><span class="st-label">Charger:</span><span class="st-val '+(s.charger.startsWith("CHARGING")?'green':'red')+'">'+s.charger+'</span></div>';
    if(s.cat_reliability && s.cat_reliability.sent) { var r=s.cat_reliability; var missClr=r.missed>0?'red':'green'; h += '<div class="st-item"><span class="st-label">CMD:</span><span class="st-val green">'+r.sent+'</span>/<span class="st-val '+missClr+'">'+r.missed+' miss</span></div>'; }
    if(s.cat_reliability && s.cat_reliability.last_miss) { h += '<div class="st-item"><span class="st-val red" style="font-size:11px">'+s.cat_reliability.last_miss+'</span></div>'; }
    if(s.streaming_enabled) {
      var bcRun = s.darkice_running;
      var bcPipe = s.stream_pipe_ok;
      var ds = s.darkice_stats || {};
      var conn = ds.connected;
      var stClr = bcRun&&conn?'green':bcRun?'yellow':'red';
      var stTxt = bcRun?(conn?'LIVE':'NO CONN'):'OFF';
      h += '<div class="st-item"><span class="st-label">Stream:</span><span class="st-val '+stClr+'">'+stTxt+'</span></div>';
      if(ds.uptime) { var u=ds.uptime; var uh=Math.floor(u/3600); var um=Math.floor((u%3600)/60); var us=u%60; h += '<div class="st-item"><span class="st-label">Age:</span><span class="st-val white">'+uh+'h '+('0'+um).slice(-2)+'m '+('0'+us).slice(-2)+'s</span></div>'; }
      if(ds.bytes_sent) { var kb=ds.bytes_sent/1024; var mb=kb/1024; h += '<div class="st-item"><span class="st-label">Sent:</span><span class="st-val cyan">'+(mb>=1?mb.toFixed(1)+' MB':kb.toFixed(0)+' KB')+'</span></div>'; }
      if(ds.send_rate) h += '<div class="st-item"><span class="st-label">Rate:</span><span class="st-val cyan">'+ds.send_rate+'</span></div>';
      if(ds.rtt) h += '<div class="st-item"><span class="st-label">RTT:</span><span class="st-val '+(ds.rtt<100?'green':ds.rtt<500?'yellow':'red')+'">'+ds.rtt.toFixed(0)+'ms</span></div>';
      var rTot = (s.darkice_restarts||0) + (s.stream_restarts||0);
      if(rTot > 0) h += '<div class="st-item"><span class="st-label">Restarts:</span><span class="st-val yellow">'+(s.darkice_restarts||0)+'d/'+(s.stream_restarts||0)+'s</span></div>';
      h += '<div class="st-item"><span class="st-label">Health:</span><span class="st-val '+(s.stream_health?'green':'red')+'">'+(s.stream_health?'ON':'off')+'</span></div>';
    }
    h += '</div>';



    document.getElementById('status').innerHTML = h;

    // Playback file slots — separate section below controls
    var pbDiv = document.getElementById('playback-status');
    if(pbDiv && s.files) {
      var ph = '';
      var order = ['1','2','3','4','5','6','7','8','9','0'];
      for(var fi=0;fi<order.length;fi++) {
        var fk = order[fi];
        var f = s.files[fk];
        if(!f || !f.loaded) continue;
        var fClr = f.playing?'red':'green';
        var fName = f.name.length > 28 ? f.name.substring(0,28)+'...' : f.name;
        ph += '<div class="pb-slot"><span class="pb-key '+(f.playing?'red':'cyan')+'">'+(fk==='0'?'ID':fk)+'</span> <span class="'+fClr+'">'+fName+'</span>'+(f.playing?' <span class="red">&#9654; Playing</span>':'')+'</div>';
      }
      pbDiv.innerHTML = ph || '<span class="white">No files loaded</span>';
    }

    // Update button states
    function setBtn(id, active, cls) { var b=document.getElementById(id); if(b) { b.className=active?(cls||'active'):''; } }
    setBtn('btn-t', s.tx_muted, 'muted');
    setBtn('btn-r', s.rx_muted, 'muted');
    setBtn('btn-m', s.tx_muted && s.rx_muted, 'muted');
    setBtn('btn-s', s.sdr1_muted, 'muted');
    setBtn('btn-x', s.sdr2_muted, 'muted');
    setBtn('btn-c', s.remote_muted, 'muted');
    setBtn('btn-a', s.announce_muted, 'muted');
    setBtn('btn-o', s.speaker_muted, 'muted');
    setBtn('btn-v', s.vad_enabled, 'active');
    setBtn('btn-p', s.manual_ptt, 'active');
    setBtn('btn-d', s.sdr1_duck, 'active');
    setBtn('btn-b', s.sdr_rebroadcast, 'active');
    // Radio processing buttons
    if(s.radio_proc) {
      setBtn('btn-rp-gate', s.radio_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-rp-hpf', s.radio_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-rp-lpf', s.radio_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-rp-notch', s.radio_proc.indexOf('Notch')>=0, 'active');
      setBtn('btn-rp-deesser', s.radio_proc.indexOf('DeEss')>=0, 'active');
      setBtn('btn-rp-spectral', s.radio_proc.indexOf('Spectral')>=0, 'active');
    }
    // SDR processing buttons
    if(s.sdr_proc) {
      setBtn('btn-sp-gate', s.sdr_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-sp-hpf', s.sdr_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-sp-lpf', s.sdr_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-sp-notch', s.sdr_proc.indexOf('Notch')>=0, 'active');
      setBtn('btn-sp-deesser', s.sdr_proc.indexOf('DeEss')>=0, 'active');
      setBtn('btn-sp-spectral', s.sdr_proc.indexOf('Spectral')>=0, 'active');
    }
    // Smart announce activity status
    var smSt = document.getElementById('smart-status');
    if(smSt && s.smart_activity) {
      var parts = [];
      for(var sk in s.smart_activity) {
        var sv = s.smart_activity[sk];
        var sClr = sv==='Done'?'green':sv.startsWith('Error')||sv.startsWith('No ')||sv.startsWith('Dropped')?'red':'yellow';
        parts.push('<span class="'+sClr+'">#'+sk+': '+sv+'</span>');
      }
      smSt.innerHTML = parts.length ? parts.join(' ') : '<span style="color:#888">Idle</span>';
    }
    // Playback button states
    if(s.files) {
      for(var fk in s.files) {
        var fb = document.getElementById('btn-f'+fk);
        if(fb) {
          var fi = s.files[fk];
          if(fi.playing) { fb.className='muted'; }
          else if(fi.loaded) { fb.className=''; }
          else { fb.className=''; fb.style.opacity='0.4'; }
          if(fi.name) fb.title=fi.name;
        }
      }
    }
    // Broadcastify buttons & status text
    var bcGrp = document.getElementById('broadcastify-group');
    if(bcGrp) {
      if(!s.streaming_enabled) { bcGrp.style.display='none'; } else { bcGrp.style.display=''; }
      var bcSt = document.getElementById('bc-status');
      if(bcSt) {
        var ds = s.darkice_stats || {};
        if(s.darkice_running && ds.connected) {
          var extra = '';
          if(ds.send_rate) extra += ' '+ds.send_rate;
          if(ds.rtt) extra += ' RTT:'+ds.rtt.toFixed(0)+'ms';
          bcSt.innerHTML='<span class="green">&#9679; LIVE'+extra+'</span>';
        } else if(s.darkice_running) {
          bcSt.innerHTML='<span class="yellow">&#9679; Running (no connection)</span>';
        } else {
          bcSt.innerHTML='<span class="red">&#9679; Stopped</span>';
        }
      }
      setBtn('btn-bc-start', s.darkice_running, 'active');
      setBtn('btn-bc-stop', !s.darkice_running && s.streaming_enabled, 'muted');
    }
    // Sync radio volume sliders with actual values from CAT
    if(s.cat_vol) {
      var lv=document.getElementById('l-vol'), rv=document.getElementById('r-vol');
      var lvt=document.getElementById('l-vol-val'), rvt=document.getElementById('r-vol-val');
      if(lv && !lv.matches(':active')) { lv.value=s.cat_vol.left; if(lvt) lvt.textContent=s.cat_vol.left; }
      if(rv && !rv.matches(':active')) { rv.value=s.cat_vol.right; if(rvt) rvt.textContent=s.cat_vol.right; }
    }
  }).catch(function(){ _lost=true; document.getElementById('status').innerHTML='<span class="red">Gateway offline — waiting for restart...</span>'; });
}

var _lost = false;
setInterval(function() {
  if(_lost) {
    fetch('/status').then(function(r){ if(r.ok){ _lost=false; window.location.reload(); } }).catch(function(){});
  } else {
    updateStatus();
  }
}, 1000);
updateStatus();

var _audio = null;
var _playing = false;
var _streamTimer = null;
var _streamStart = 0;

function toggleStream() {
  if (_playing) {
    stopStream();
  } else {
    startStream();
  }
}

function startStream() {
  // Kill any existing audio first to prevent orphaned streams
  if (_audio) {
    _audio.onplaying = null;
    _audio.onerror = null;
    _audio.onended = null;
    _audio.pause();
    _audio.src = '';
    _audio = null;
  }
  if (_streamTimer) { clearInterval(_streamTimer); _streamTimer = null; }

  var btn = document.getElementById('play-btn');
  var ind = document.getElementById('stream-indicator');
  var st = document.getElementById('stream-status');
  _playing = true;  // Set immediately to prevent double-click restart
  btn.innerHTML = '&#9724; MP3';
  btn.style.color = '#f39c12';
  btn.style.borderColor = '#f39c12';
  st.innerHTML = '<span style="color:#f39c12">Buffering...</span>';

  _audio = new Audio('/stream');
  _audio.volume = document.getElementById('vol-slider').value / 100;

  _audio.onplaying = function() {
    _playing = true;
    _streamStart = Date.now();
    btn.innerHTML = '&#9724; MP3';
    btn.style.color = '#2ecc71';
    btn.style.borderColor = '#2ecc71';
    ind.style.display = 'inline-block';
    st.innerHTML = '<span style="color:#2ecc71">0:00</span> <span style="color:#666">96kbps</span>';
    _streamTimer = setInterval(function() {
      var secs = Math.floor((Date.now() - _streamStart) / 1000);
      var m = Math.floor(secs / 60);
      var s = secs % 60;
      var t = m + ':' + (s < 10 ? '0' : '') + s;
      st.innerHTML = '<span style="color:#2ecc71">' + t + '</span> <span style="color:#666">96kbps</span>';
    }, 1000);
  };

  _audio.onerror = function() {
    st.innerHTML = '<span style="color:#e74c3c">Stream error</span>';
    stopStream();
  };

  _audio.onended = function() { stopStream(); };

  _audio.play().catch(function(e) {
    st.innerHTML = '<span style="color:#e74c3c">Error: ' + e.message + '</span>';
    stopStream();
  });
}

function stopStream() {
  if (_streamTimer) { clearInterval(_streamTimer); _streamTimer = null; }
  if (_audio) {
    _audio.pause();
    _audio.src = '';
    _audio = null;
  }
  _playing = false;
  document.getElementById('play-btn').innerHTML = '&#9654; MP3';
  document.getElementById('play-btn').style.color = '#e0e0e0';
  document.getElementById('play-btn').style.borderColor = '#1b3a5c';
  document.getElementById('stream-indicator').style.display = 'none';
  document.getElementById('stream-status').innerHTML = '';
}

function setVolume(v) {
  if (_audio) _audio.volume = v / 100;
}

// --- Low-latency WebSocket PCM player ---
var _ws = null;
var _wsCtx = null;
var _wsGain = null;
var _wsPlaying = false;
var _wsTimer = null;
var _wsStart = 0;
var _wsBytes = 0;
var _wsWorklet = null;
var _wakeLock = null;

function toggleWS() {
  if (_wsPlaying) { stopWS(); } else { startWS(); }
}

function startWS() {
  var btn = document.getElementById('ws-btn');
  var ind = document.getElementById('ws-indicator');
  var st = document.getElementById('ws-status');
  btn.innerHTML = '&#9724; PCM';
  btn.style.color = '#f39c12';
  btn.style.borderColor = '#f39c12';
  st.innerHTML = '<span style="color:#f39c12">Connecting...</span>';

  // Prevent double-click race
  if (_wsCtx) { stopWS(); return; }

  try {
    _wsCtx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 48000});
    console.log('[WS-Audio] AudioContext created, state:', _wsCtx.state, 'requested: 48000, actual sampleRate:', _wsCtx.sampleRate);
    if (_wsCtx.sampleRate !== 48000) console.warn('[WS-Audio] SAMPLE RATE MISMATCH — audio may play at wrong speed!');
  } catch(e) {
    st.innerHTML = '<span style="color:#e74c3c">No AudioContext</span>';
    btn.innerHTML = '&#9654; PCM';
    btn.style.color = '#e0e0e0';
    return;
  }

  // Resume AudioContext (browsers require user gesture)
  if (_wsCtx.state === 'suspended') {
    _wsCtx.resume().then(function() { console.log('[WS-Audio] AudioContext resumed'); });
  }

  // Create gain node for volume control
  _wsGain = _wsCtx.createGain();
  _wsGain.gain.value = document.getElementById('ws-vol').value / 100;
  _wsGain.connect(_wsCtx.destination);

  // PCM ring buffer shared between WS onmessage and audio output
  var _pcmBuf = [];
  var _pcmPos = 0;
  var _pcmTotal = 0;
  var _pcmReady = false;

  function _drainPCM(output) {
    var w = 0, need = output.length;
    // Pre-buffer 50ms (2400 samples at 48kHz) before starting playback
    if (!_pcmReady) {
      if (_pcmTotal < 2400) { for (w = 0; w < need; w++) output[w] = 0; return; }
      _pcmReady = true;
    }
    // Cap buffer at 200ms (9600 samples) — skip ahead if falling behind
    while (_pcmTotal > 9600 && _pcmBuf.length > 1) { _pcmTotal -= _pcmBuf[0].length; _pcmBuf.shift(); _pcmPos = 0; }
    while (w < need) {
      if (!_pcmBuf.length) { for (; w < need; w++) output[w] = 0; break; }
      var cur = _pcmBuf[0], avail = cur.length - _pcmPos;
      var take = Math.min(avail, need - w);
      for (var j = 0; j < take; j++) output[w++] = cur[_pcmPos++];
      if (_pcmPos >= cur.length) { _pcmTotal -= cur.length; _pcmBuf.shift(); _pcmPos = 0; }
    }
  }

  function _connectWS() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _ws = new WebSocket(proto + '//' + location.host + '/ws_audio');
    _ws.binaryType = 'arraybuffer';

    _ws.onopen = function() {
      console.log('[WS-Audio] WebSocket connected');
      _wsPlaying = true;
      _wsStart = Date.now();
      _wsBytes = 0;
      if (navigator.wakeLock) { navigator.wakeLock.request('screen').then(function(wl) { _wakeLock = wl; }).catch(function(){}); }
      btn.innerHTML = '&#9724; PCM';
      btn.style.color = '#00d4ff';
      btn.style.borderColor = '#00d4ff';
      ind.style.display = 'inline-block';
      st.innerHTML = '<span style="color:#00d4ff">0:00</span>';
      _wsTimer = setInterval(function() {
        var secs = Math.floor((Date.now() - _wsStart) / 1000);
        var m = Math.floor(secs / 60);
        var s = secs % 60;
        var t = m + ':' + (s < 10 ? '0' : '') + s;
        var kb = (_wsBytes / 1024).toFixed(0);
        var unit = 'KB';
        if (_wsBytes >= 1048576) { kb = (_wsBytes / 1048576).toFixed(1); unit = 'MB'; }
        var kbps = secs > 0 ? ((_wsBytes * 8 / secs / 1000).toFixed(0) + 'kbps') : '';
        st.innerHTML = '<span style="color:#00d4ff">' + t + '</span> <span style="color:#666">' + kb + unit + (kbps ? ' ' + kbps : '') + '</span>';
      }, 1000);
    };

    // Resample ratio: server sends 48kHz, AudioContext may run at different rate
    var _srcRate = 48000;
    var _dstRate = _wsCtx.sampleRate;
    var _resample = (_dstRate !== _srcRate);
    if (_resample) console.log('[WS-Audio] Resampling', _srcRate, '→', _dstRate);

    _ws.onmessage = function(ev) {
      if (ev.data instanceof ArrayBuffer) {
        _wsBytes += ev.data.byteLength;
        var int16 = new Int16Array(ev.data);
        var float32 = new Float32Array(int16.length);
        for (var i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768.0;
        // Resample if AudioContext rate differs from source
        if (_resample) {
          var ratio = _srcRate / _dstRate;
          var outLen = Math.round(float32.length / ratio);
          var resampled = new Float32Array(outLen);
          for (var i = 0; i < outLen; i++) {
            var srcIdx = i * ratio;
            var idx0 = Math.floor(srcIdx);
            var frac = srcIdx - idx0;
            var s0 = float32[idx0] || 0;
            var s1 = float32[Math.min(idx0 + 1, float32.length - 1)] || 0;
            resampled[i] = s0 + frac * (s1 - s0);
          }
          float32 = resampled;
        }
        if (_wsWorklet && _wsWorklet.port) {
          _wsWorklet.port.postMessage(float32);
        } else {
          _pcmBuf.push(float32);
          _pcmTotal += float32.length;
          // Cap buffer to ~200ms to prevent buildup
          while (_pcmBuf.length > 4) { _pcmTotal -= _pcmBuf[0].length; _pcmBuf.shift(); _pcmPos = 0; }
        }
      }
    };

    _ws.onerror = function(ev) {
      console.log('[WS-Audio] WebSocket error:', ev);
      st.innerHTML = '<span style="color:#e74c3c">WS error</span>';
      stopWS();
    };

    _ws.onclose = function(ev) {
      console.log('[WS-Audio] WebSocket closed, code:', ev.code, 'reason:', ev.reason);
      if (_wsPlaying) stopWS();
    };
  }

  // Try AudioWorklet first (requires secure context), fall back to ScriptProcessor
  if (_wsCtx.audioWorklet) {
    var workletCode = 'class P extends AudioWorkletProcessor{constructor(){super();this.b=[];this.p=0;this.tot=0;this.ready=false;this.port.onmessage=e=>{this.b.push(e.data);this.tot+=e.data.length;if(!this.ready&&this.tot>=2400)this.ready=true;if(this.tot>9600){while(this.tot>2400&&this.b.length>1){this.tot-=this.b[0].length;this.b.shift()}this.p=0}}}process(i,o){var c=o[0][0];if(!c)return true;var n=c.length,w=0;if(!this.ready){for(w=0;w<n;w++)c[w]=0;return true}while(w<n){if(!this.b.length){for(;w<n;w++)c[w]=0;break}var r=this.b[0],a=r.length-this.p,t=Math.min(a,n-w);for(var j=0;j<t;j++)c[w++]=r[this.p++];if(this.p>=r.length){this.b.shift();this.p=0;this.tot-=r.length}}return true}}registerProcessor("p",P)';
    var blob = new Blob([workletCode], {type: 'application/javascript'});
    var blobURL = URL.createObjectURL(blob);
    _wsCtx.audioWorklet.addModule(blobURL).then(function() {
      URL.revokeObjectURL(blobURL);
      console.log('[WS-Audio] AudioWorklet loaded OK');
      _wsWorklet = new AudioWorkletNode(_wsCtx, 'p', {outputChannelCount:[1], numberOfOutputs:1});
      _wsWorklet.connect(_wsGain);
      _connectWS();
    }).catch(function(e) {
      // AudioWorklet failed — use ScriptProcessor fallback
      URL.revokeObjectURL(blobURL);
      console.log('[WS-Audio] AudioWorklet failed:', e.message, '— using ScriptProcessor');
      var sp = _wsCtx.createScriptProcessor(2048, 0, 1);
      sp.onaudioprocess = function(ev) { _drainPCM(ev.outputBuffer.getChannelData(0)); };
      sp.connect(_wsGain);
      _wsWorklet = sp;  // store for cleanup
      _connectWS();
    });
  } else {
    // No AudioWorklet at all — ScriptProcessor
    var sp = _wsCtx.createScriptProcessor(2048, 0, 1);
    sp.onaudioprocess = function(ev) { _drainPCM(ev.outputBuffer.getChannelData(0)); };
    sp.connect(_wsGain);
    _wsWorklet = sp;
    _connectWS();
  }
}

function stopWS() {
  if (_wsTimer) { clearInterval(_wsTimer); _wsTimer = null; }
  if (_ws) { try { _ws.close(); } catch(e){} _ws = null; }
  if (_wsWorklet) { try { _wsWorklet.disconnect(); } catch(e){} _wsWorklet = null; }
  if (_wsGain) { try { _wsGain.disconnect(); } catch(e){} _wsGain = null; }
  if (_wsCtx) { try { _wsCtx.close(); } catch(e){} _wsCtx = null; }
  _wsPlaying = false;
  if (_wakeLock) { try { _wakeLock.release(); } catch(e){} _wakeLock = null; }
  document.getElementById('ws-btn').innerHTML = '&#9654; PCM';
  document.getElementById('ws-btn').style.color = '#e0e0e0';
  document.getElementById('ws-btn').style.borderColor = '#1b3a5c';
  document.getElementById('ws-indicator').style.display = 'none';
  document.getElementById('ws-status').innerHTML = '';
  _wsBytes = 0;
}

function setWSVol(v) {
  if (_wsGain) _wsGain.gain.value = v / 100;
}

// --- System Status ---
function fmtBytes(b) {
  if (b >= 1048576) return (b/1048576).toFixed(1) + ' MB/s';
  if (b >= 1024) return (b/1024).toFixed(1) + ' KB/s';
  return b + ' B/s';
}
function sysBar(pct, color) {
  var c = pct > 80 ? '#e74c3c' : pct > 60 ? '#f39c12' : (color || '#2ecc71');
  var w = Math.round(Math.min(Math.max(pct, 0), 100));
  var p = pct < 10 ? '  '+pct : pct < 100 ? ' '+pct : ''+pct;
  return '<span class="bar-pct">'+p+'%</span><span class="bar" style="width:'+w+'px;background:'+c+'"></span>';
}
function updateSysInfo() {
  fetch('/sysinfo').then(function(r){return r.json()}).then(function(s) {
    var h = '<div class="st-row">';
    h += '<div class="st-item"><span class="st-label">CPU:</span>'+sysBar(s.cpu_pct)+'</div>';
    h += '<div class="st-item"><span class="st-label">Load:</span><span class="st-val cyan">'+s.load[0]+' '+s.load[1]+' '+s.load[2]+'</span></div>';
    h += '<div class="st-item"><span class="st-label">RAM:</span>'+sysBar(s.mem_pct, '#00d4ff')+'<span class="st-label">'+s.mem_used_mb+'/'+s.mem_total_mb+'MB</span></div>';
    if (s.swap_total_mb > 0) {
      var swPct = Math.round(100*s.swap_used_mb/s.swap_total_mb);
      h += '<div class="st-item"><span class="st-label">Swap:</span>'+sysBar(swPct, '#e056a0')+'<span class="st-label">'+s.swap_used_mb+'/'+s.swap_total_mb+'MB</span></div>';
    }
    h += '<div class="st-item"><span class="st-label">Disk:</span>'+sysBar(s.disk_pct, '#f1c40f')+'<span class="st-label">'+s.disk_used_gb+'/'+s.disk_total_gb+'GB</span></div>';
    h += '<div class="st-item"><span class="st-label">Disk I/O:</span><span class="st-val green">R:'+fmtBytes(s.disk_read_bps)+'</span> <span class="st-val yellow">W:'+fmtBytes(s.disk_write_bps)+'</span></div>';
    h += '</div>';
    h += '<div class="st-row">';
    h += '<div class="st-item"><span class="st-label">Net:</span><span class="st-val green">RX:'+fmtBytes(s.net_rx_bps)+'</span> <span class="st-val cyan">TX:'+fmtBytes(s.net_tx_bps)+'</span></div>';
    h += '<div class="st-item"><span class="st-label">TCP:</span><span class="st-val white">'+s.tcp_connections+'</span></div>';
    if (s.temps && s.temps.length) {
      for (var i=0; i<s.temps.length; i++) {
        var t = s.temps[i];
        var tc = t.temp > 80 ? 'red' : t.temp > 60 ? 'yellow' : 'green';
        h += '<div class="st-item"><span class="st-label">'+t.label+':</span><span class="st-val '+tc+'">'+t.temp+'&deg;C</span></div>';
      }
    }
    h += '</div>';
    if (s.ips && s.ips.length) {
      h += '<div class="st-row">';
      if (s.hostname) h += '<div class="st-item"><span class="st-label">Host:</span><span class="st-val cyan">'+s.hostname+'</span></div>';
      for (var i=0; i<s.ips.length; i++) {
        h += '<div class="st-item"><span class="st-label">'+s.ips[i].iface+':</span><span class="st-val white">'+s.ips[i].addr+'</span></div>';
      }
      h += '</div>';
    }
    document.getElementById('sysinfo').innerHTML = h;
  }).catch(function(){});
}
setInterval(updateSysInfo, 2000);
updateSysInfo();
</script>
'''
        return self._wrap_html('Dashboard', body)

    def _generate_html(self):
        """Build the full HTML page with form inputs grouped by section."""
        section_map = self._build_section_map()

        # Build ordered list of (section, [(key, current_value, default_value)])
        sections = {}  # section -> [(key, cur_val, default_val)]
        section_order = []

        # Walk config file to get key order and sections
        config_path = self.config.config_file
        current_section = 'default'
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('[') and ']' in line:
                        current_section = line[1:line.index(']')].strip()
                        if current_section not in sections:
                            sections[current_section] = []
                            section_order.append(current_section)
                    elif '=' in line and not line.startswith('#'):
                        key = line.split('=', 1)[0].strip()
                        cur_val = getattr(self.config, key, '')
                        default_val = self._defaults.get(key, None)
                        if current_section not in sections:
                            sections[current_section] = []
                            section_order.append(current_section)
                        sections[current_section].append((key, cur_val, default_val))

        # Build form HTML
        form_parts = []
        for section in section_order:
            display_name = self._SECTION_NAMES.get(section, section)
            fields_html = []
            for key, cur_val, default_val in sections[section]:
                field = self._render_field(key, cur_val, default_val)
                fields_html.append(field)

            # Default open for first 3 sections, collapsed for rest
            open_attr = ' open' if section_order.index(section) < 3 else ''
            form_parts.append(
                f'<details{open_attr}><summary>{display_name}</summary>'
                f'<div class="fields">{"".join(fields_html)}</div></details>')

        body = (
            '<h1>Radio Gateway Configuration</h1>'
            '<p style="margin:0 0 10px"><a href="/">Live Dashboard</a> | <a href="/sdr">SDR</a> | <a href="/logs">Logs</a></p>'
            '<form method="POST" action="/">'
            '<div class="buttons">'
            '<button type="submit" name="_action" value="save" class="btn-save">Save</button>'
            '<button type="submit" name="_action" value="restart" class="btn-restart"'
            ' onclick="return confirm(\'Save and restart the gateway?\')">Save &amp; Restart</button>'
            '<button type="button" class="btn-exit"'
            ' onclick="if(confirm(\'Exit the gateway server? This will stop all services.\')){fetch(\'/exit\',{method:\'POST\'}).then(()=>{document.body.innerHTML=\'<h1 style=color:#e0e0e0;text-align:center;margin-top:40vh>Gateway stopped.</h1>\'});}">Exit Server</button>'
            '</div>'
            + ''.join(form_parts) +
            '</form>'
            '<script>'
            'var _dirty=false;'
            'document.querySelector("form").addEventListener("input",function(){_dirty=true;});'
            'document.querySelector("form").addEventListener("submit",function(){_dirty=false;});'
            'window.addEventListener("beforeunload",function(e){if(_dirty){e.preventDefault();e.returnValue="";}});'
            '</script>'
        )
        return self._wrap_html('Config', body)

    def _render_field(self, key, cur_val, default_val):
        """Render a single config field as HTML."""
        import html as html_mod

        is_bool = isinstance(default_val, bool) if default_val is not None else isinstance(cur_val, bool)
        is_sensitive = key in self._SENSITIVE_KEYS

        if is_bool:
            checked = ' checked' if cur_val else ''
            # Hidden field ensures unchecked boxes submit 'false'
            inp = (f'<input type="hidden" name="{key}" value="false">'
                   f'<input type="checkbox" name="{key}" value="true"{checked}>')
            default_str = str(default_val).lower() if default_val is not None else ''
        elif is_sensitive:
            val = html_mod.escape(str(cur_val)) if cur_val else ''
            inp = f'<input type="password" name="{key}" value="{val}" autocomplete="off">'
            default_str = '(hidden)'
        elif isinstance(cur_val, (int, float)) and not isinstance(cur_val, bool):
            val = str(cur_val)
            # Use text input for numbers to support hex
            if key in self._HEX_KEYS:
                val = hex(int(cur_val)) if isinstance(cur_val, int) else val
            inp = f'<input type="text" name="{key}" value="{html_mod.escape(val)}">'
            default_str = str(default_val) if default_val is not None else ''
            if key in self._HEX_KEYS and isinstance(default_val, int):
                default_str = hex(default_val)
        else:
            val = html_mod.escape(str(cur_val)) if cur_val else ''
            inp = f'<input type="text" name="{key}" value="{val}">'
            default_str = html_mod.escape(str(default_val)) if default_val is not None else ''

        default_hint = f'<span class="default">default: {default_str}</span>' if default_str else ''

        return (f'<div class="field">'
                f'<label for="{key}">{key}</label>{inp}{default_hint}'
                f'</div>')


class StatusBarWriter:
    """Wraps sys.stdout so that any print() clears the status bar first.

    The status monitor loop calls draw_status() to paint the bar on the
    last terminal line.  When any other thread calls print() (which goes
    through write()), this wrapper:
      1. Clears the current status bar line (\r + spaces + \r)
      2. Writes the log text (which scrolls the terminal up)
      3. Lets the next draw_status() tick repaint the bar below

    In headless mode, the status bar is suppressed but all output is still
    captured in the ring buffer and written to the log file.
    """

    def __init__(self, original, headless=False, buffer_lines=2000, log_file=None):
        self._orig = original
        self._lock = threading.Lock()
        self._last_status = ""   # last status bar text (for redraw)
        self._bar_drawn = False  # True when status bar is on screen
        self._bar_lines = 1     # how many lines the status bar occupies
        self._at_line_start = True  # track whether next write starts a new line
        self._headless = headless
        # Ring buffer for web log viewer
        import collections
        self._log_buffer = collections.deque(maxlen=buffer_lines)
        self._log_seq = 0  # monotonic sequence number for polling
        # Rolling log file
        self._log_file = log_file  # file object (set up externally)
        # Forward all attributes that print() and other code might check
        for attr in ('encoding', 'errors', 'mode', 'name', 'newlines',
                     'fileno', 'isatty', 'readable', 'seekable', 'writable'):
            if hasattr(original, attr):
                try:
                    setattr(self, attr, getattr(original, attr))
                except (AttributeError, TypeError):
                    pass

    def _append_log(self, timestamped_line):
        """Add a line to the ring buffer and log file."""
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
            if not self._headless and self._bar_drawn and text and text != '\n':
                # Clear status bar lines before printing log text
                try:
                    import shutil as _sh
                    cols = _sh.get_terminal_size().columns
                except Exception:
                    cols = 120
                blank = ' ' * cols
                if self._bar_lines == 3:
                    self._orig.write(f"\n\n\r{blank}\r\033[A\r{blank}\r\033[A\r{blank}\r")
                elif self._bar_lines == 2:
                    self._orig.write(f"\n\r{blank}\r\033[A\r{blank}\r")
                else:
                    self._orig.write(f"\r{blank}\r")
                self._bar_drawn = False
                # Strip leading \n — it was only there to push past the old
                # status bar; the wrapper now clears the bar instead.
                if text.startswith('\n'):
                    text = text[1:]
            # Prepend system time at the start of each new line
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
                # If text ended with \n, next write starts a new line
                if text.endswith('\n'):
                    self._at_line_start = True
                if not self._headless:
                    self._orig.write(''.join(out_parts))
            else:
                if not self._headless:
                    self._orig.write(text)
        return len(text)

    def draw_status(self, status_line, line2=None, line3=None):
        """Called by the status monitor to paint the bar (no newline).

        Supports up to 3 lines.  The cursor is left on line 1 so that
        the next draw_status or write() can overwrite cleanly.
        In headless mode, status bar is suppressed (web dashboard shows status).
        """
        if self._headless:
            self._last_status = status_line
            return
        with self._lock:
            if line3 and line2:
                self._orig.write(f"\r{status_line}\n\r{line2}\n\r{line3}\033[2A\r")
                self._bar_lines = 3
            elif line2:
                self._orig.write(f"\r{status_line}\n\r{line2}\033[A\r")
                self._bar_lines = 2
            else:
                self._orig.write(f"\r{status_line}")
                self._bar_lines = 1
            self._orig.flush()
            self._last_status = status_line
            self._bar_drawn = True

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
        
        # Audio processing state (legacy — kept for backwards compat)
        self.noise_profile = None  # For spectral subtraction
        self.gate_envelope = 0.0  # For noise gate smoothing
        self.highpass_state = None  # For high-pass filter state

        # Per-source audio processors
        self.radio_processor = AudioProcessor("radio", config)
        self.sdr_processor = AudioProcessor("sdr", config)
        self._sync_radio_processor()
        self._sync_sdr_processor()
        
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
        self.web_mic_source = None        # WebMicSource (browser mic → radio TX)
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

        # Web configuration UI
        self.web_config_server = None

        # Dynamic DNS updater
        self.ddns_updater = None  # DDNSUpdater instance
        self.cloudflare_tunnel = None  # CloudflareTunnel instance
        self.email_notifier = None  # EmailNotifier instance

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
        p.enable_noise_suppression = self.config.ENABLE_NOISE_SUPPRESSION
        p.noise_suppression_method = self.config.NOISE_SUPPRESSION_METHOD
        p.noise_suppression_strength = self.config.NOISE_SUPPRESSION_STRENGTH
        p.enable_deesser = self.config.ENABLE_DEESSER
        p.deesser_freq = self.config.DEESSER_FREQ
        p.deesser_strength = self.config.DEESSER_STRENGTH

    def _sync_sdr_processor(self):
        """Sync SDR-specific config flags into the SDR AudioProcessor instance."""
        p = self.sdr_processor
        p.enable_noise_gate = self.config.SDR_PROC_ENABLE_NOISE_GATE
        p.gate_threshold = self.config.SDR_PROC_NOISE_GATE_THRESHOLD
        p.gate_attack = self.config.SDR_PROC_NOISE_GATE_ATTACK
        p.gate_release = self.config.SDR_PROC_NOISE_GATE_RELEASE
        p.enable_hpf = self.config.SDR_PROC_ENABLE_HPF
        p.hpf_cutoff = self.config.SDR_PROC_HPF_CUTOFF
        p.enable_lpf = self.config.SDR_PROC_ENABLE_LPF
        p.lpf_cutoff = self.config.SDR_PROC_LPF_CUTOFF
        p.enable_notch = self.config.SDR_PROC_ENABLE_NOTCH
        p.notch_freq = self.config.SDR_PROC_NOTCH_FREQ
        p.notch_q = self.config.SDR_PROC_NOTCH_Q
        p.enable_noise_suppression = self.config.SDR_PROC_ENABLE_NS
        p.noise_suppression_method = self.config.SDR_PROC_NS_METHOD
        p.noise_suppression_strength = self.config.SDR_PROC_NS_STRENGTH
        p.enable_deesser = self.config.SDR_PROC_ENABLE_DEESSER
        p.deesser_freq = self.config.SDR_PROC_DEESSER_FREQ
        p.deesser_strength = self.config.SDR_PROC_DEESSER_STRENGTH

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

    def process_audio_for_sdr(self, pcm_data):
        """Apply SDR-specific audio processing chain."""
        self._sync_sdr_processor()
        return self.sdr_processor.process(pcm_data)
    
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
        """Control PTT via configured method (aioc, relay, or software)."""
        method = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
        if method == 'relay':
            self._ptt_relay(state_on)
        elif method == 'software':
            self._ptt_software(state_on)
        else:
            self._ptt_aioc(state_on)
        self.ptt_active = state_on

    def _ptt_aioc(self, state_on):
        """PTT via AIOC HID GPIO."""
        if not self.aioc_device:
            return
        try:
            state = 1 if state_on else 0
            iomask = 1 << (self.config.AIOC_PTT_CHANNEL - 1)
            iodata = state << (self.config.AIOC_PTT_CHANNEL - 1)
            data = Struct("<BBBBB").pack(0, 0, iodata, iomask, 0)
            if self.config.VERBOSE_LOGGING:
                print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (AIOC GPIO{self.config.AIOC_PTT_CHANNEL})")
            self.aioc_device.write(bytes(data))
        except Exception as e:
            print(f"\n[PTT] AIOC error: {e}")

    def _ptt_relay(self, state_on):
        """PTT via CH340 USB relay."""
        if not self.relay_ptt:
            return
        self.relay_ptt.set_state(state_on)
        if self.config.VERBOSE_LOGGING:
            print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (relay)")

    def _ptt_software(self, state_on):
        """PTT via CAT TCP RTS command."""
        if not self.cat_client:
            return
        self.cat_client.set_rts(state_on)
        if self.config.VERBOSE_LOGGING:
            print(f"\n[PTT] {'KEYING' if state_on else 'UNKEYING'} radio (software/CAT)")
    
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

        # Only suppress if not verbose
        if not self.config.VERBOSE_LOGGING:
            # Hardcode fd 2 — sys.stderr may be StatusBarWriter (fileno→stdout)
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
            # Hardcode fd 2 — sys.stderr may be StatusBarWriter (fileno→stdout)
            saved_stderr = os.dup(2)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.close(devnull)
                self.pyaudio_instance = pyaudio.PyAudio()
            finally:
                os.dup2(saved_stderr, 2)
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

            # Initialize web microphone source (browser mic → radio TX)
            if getattr(self.config, 'ENABLE_WEB_MIC', True):
                try:
                    self.web_mic_source = WebMicSource(self.config, self)
                    if self.web_mic_source.setup_audio():
                        self.mixer.add_source(self.web_mic_source)
                        print("✓ Web microphone source (WEBMIC) added to mixer")
                except Exception as e:
                    print(f"⚠ Warning: Could not initialize web mic source: {e}")
                    self.web_mic_source = None

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

            # Initialize PTT relay (when PTT_METHOD = relay)
            ptt_method = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
            if ptt_method == 'relay':
                try:
                    dev = self.config.PTT_RELAY_DEVICE
                    print(f"Initializing PTT relay ({dev})...")
                    self.relay_ptt = RelayController(dev, self.config.PTT_RELAY_BAUD)
                    if self.relay_ptt.open():
                        self.relay_ptt.set_state(False)  # Ensure PTT released on startup
                        print(f"  PTT relay: ready")
                    else:
                        print(f"  PTT relay: FAILED to open — PTT will not work!")
                        self.relay_ptt = None
                except Exception as e:
                    print(f"  Warning: Could not initialize PTT relay: {e}")
                    self.relay_ptt = None

            # Initialize TH-9800 CAT control
            if getattr(self.config, 'ENABLE_CAT_CONTROL', False):
                try:
                    host = self.config.CAT_HOST
                    port = int(self.config.CAT_PORT)
                    password = str(self.config.CAT_PASSWORD)
                    verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
                    print(f"Connecting to TH-9800 CAT server ({host}:{port})...")
                    self.cat_client = RadioCATClient(host, port, password, verbose=verbose)
                    if self.cat_client.connect():
                        print("  Connected to CAT server")
                        # Check if serial is already connected, or auto-connect if not
                        try:
                            serial_resp = self.cat_client._send_cmd("!serial status")
                            if serial_resp and 'connected' in serial_resp and 'disconnected' not in serial_resp:
                                self.cat_client._serial_connected = True
                                print("  Serial already connected")
                            else:
                                # Serial not connected — send connect command
                                print("  Connecting serial...")
                                with self.cat_client._sock_lock:
                                    self.cat_client._sock.sendall(b"!serial connect\n")
                                    self.cat_client._last_activity = time.monotonic()
                                    connect_resp = self.cat_client._recv_line(timeout=10.0)
                                if connect_resp and 'connected' in connect_resp:
                                    self.cat_client._serial_connected = True
                                    print(f"  Serial connected: {connect_resp}")
                                else:
                                    print(f"  Serial connect failed: {connect_resp}")
                        except Exception as e:
                            print(f"  Serial status/connect error: {e}")
                        # If serial is connected, refresh display and read RTS state
                        if self.cat_client._serial_connected:
                            try:
                                with open('/tmp/th9800_rts_state', 'r') as f:
                                    self.cat_client._rts_usb = f.read().strip() == '1'
                            except Exception:
                                pass
                            # Display refresh: press+release each VFO dial to populate freq/channel
                            try:
                                cat = self.cat_client
                                cat._send_button([0x00, 0x25], 3, 5)  # L_DIAL_PRESS
                                time.sleep(0.15)
                                cat._send_button_release()
                                time.sleep(0.3)
                                cat._drain(0.5)
                                cat._send_button([0x00, 0xA5], 3, 5)  # R_DIAL_PRESS
                                time.sleep(0.15)
                                cat._send_button_release()
                                time.sleep(0.3)
                                cat._drain(0.5)
                                print("  Display refreshed")
                            except Exception as e:
                                print(f"  Display refresh failed: {e}")
                        # Pre-set volume from config so dashboard sliders show
                        # correct values even if setup_radio is disabled or fails
                        left_vol = getattr(self.config, 'CAT_LEFT_VOLUME', -1)
                        right_vol = getattr(self.config, 'CAT_RIGHT_VOLUME', -1)
                        if int(left_vol) != -1:
                            self.cat_client._volume[self.cat_client.LEFT] = int(left_vol)
                        if int(right_vol) != -1:
                            self.cat_client._volume[self.cat_client.RIGHT] = int(right_vol)
                        # Start background drain (setup_radio runs later, near end of init)
                        self.cat_client.start_background_drain()
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

            # Validate PTT method setup
            if ptt_method == 'relay' and not self.relay_ptt:
                print("WARNING: PTT_METHOD = relay but PTT relay not available — PTT will not work!")
            elif ptt_method == 'software' and not self.cat_client:
                print("WARNING: PTT_METHOD = software but CAT client not connected — PTT will not work!")
            elif ptt_method not in ('aioc', 'relay', 'software'):
                print(f"WARNING: Unknown PTT_METHOD '{ptt_method}' — defaulting to AIOC")

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

            # CAT startup commands — run late so everything else is settled
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
            
            # Auto-switch RTS to Radio Controlled for TX (same as playback keys)
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
                if not self.smart_announce or not self.smart_announce._client:
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
                        _silence = b'\x00' * (self.config.AUDIO_CHUNK_SIZE * 2)
                        self._speaker_enqueue(_silence)
                        # Keep WebSocket clients fed with silence so they stay connected
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

                        # Push to WebSocket clients before any VAD/PTT routing
                        if self.web_config_server and self.web_config_server._ws_clients:
                            self.web_config_server.push_ws_audio(data)

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

                    # Push to WebSocket clients BEFORE VAD gate so low-latency
                    # listeners always get continuous audio (like speaker output).
                    if self.web_config_server and self.web_config_server._ws_clients:
                        self.web_config_server.push_ws_audio(data)

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

                # Push to web audio stream listeners (MP3 only — WebSocket PCM
                # is already pushed earlier in the mixer path (line ~11849) and
                # direct-AIOC path (line ~12087) to avoid double-pushing.)
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
        # In headless mode, all controls come from web UI — skip terminal input
        if getattr(self.config, 'HEADLESS_MODE', False):
            print("  Headless mode — keyboard controls disabled (use web UI)")
            return

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

                    # Keys handled locally (need terminal access)
                    if char == 'i':
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
                        self._watchdog_active = not self._watchdog_active
                        if self._watchdog_active:
                            self._watchdog_t0 = time.monotonic()
                            self._watchdog_thread = threading.Thread(
                                target=self._watchdog_trace_loop, daemon=True)
                            self._watchdog_thread.start()
                            print(f"\n[Watchdog] Trace STARTED — sampling every 5s, flushing to tools/watchdog_trace.txt every 60s")
                        else:
                            print(f"\n[Watchdog] Trace STOPPED")

                    elif char == 'z':
                        writer = getattr(sys.stdout, '_orig', sys.stdout)
                        writer.write("\033[2J\033[H")
                        writer.flush()
                        if hasattr(sys.stdout, '_bar_drawn'):
                            sys.stdout._bar_drawn = False
                        self._print_banner()

                    else:
                        # All other keys go through shared handler
                        self.handle_key(char)

                time.sleep(0.05)

        finally:
            # Restore terminal settings
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except:
                pass

    def handle_proc_toggle(self, source, filt):
        """Toggle a processing filter for a specific source (radio or sdr).
        Called from the /proc_toggle API endpoint.
        """
        # Map source to config prefix and processor
        if source == 'radio':
            toggle_map = {
                'gate':     'ENABLE_NOISE_GATE',
                'hpf':      'ENABLE_HIGHPASS_FILTER',
                'lpf':      'ENABLE_LOWPASS_FILTER',
                'notch':    'ENABLE_NOTCH_FILTER',
                'deesser':  'ENABLE_DEESSER',
                'spectral': 'ENABLE_NOISE_SUPPRESSION',
            }
            key = toggle_map.get(filt)
            if key:
                current = getattr(self.config, key, False)
                setattr(self.config, key, not current)
                # For spectral, also set the method
                if filt == 'spectral' and not current:
                    self.config.NOISE_SUPPRESSION_METHOD = 'spectral'
                self._sync_radio_processor()
        elif source == 'sdr':
            toggle_map = {
                'gate':     'SDR_PROC_ENABLE_NOISE_GATE',
                'hpf':      'SDR_PROC_ENABLE_HPF',
                'lpf':      'SDR_PROC_ENABLE_LPF',
                'notch':    'SDR_PROC_ENABLE_NOTCH',
                'deesser':  'SDR_PROC_ENABLE_DEESSER',
                'spectral': 'SDR_PROC_ENABLE_NS',
            }
            key = toggle_map.get(filt)
            if key:
                current = getattr(self.config, key, False)
                setattr(self.config, key, not current)
                if filt == 'spectral' and not current:
                    self.config.SDR_PROC_NS_METHOD = 'spectral'
                self._sync_sdr_processor()

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
            if self.sdr_source:
                self.sdr_muted = not self.sdr_muted
                self.sdr_source.muted = self.sdr_muted
                self._trace_events.append((time.monotonic(), 'sdr_mute', 'on' if self.sdr_muted else 'off'))
        elif char == 'd':
            if self.sdr_source:
                self.sdr_source.duck = not self.sdr_source.duck
        elif char == 'x':
            if self.sdr2_source:
                self.sdr2_muted = not self.sdr2_muted
                self.sdr2_source.muted = self.sdr2_muted
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
        elif char == 'y':
            if self.config.ENABLE_NOISE_SUPPRESSION and self.config.NOISE_SUPPRESSION_METHOD == 'spectral':
                self.config.ENABLE_NOISE_SUPPRESSION = False
            else:
                self.config.ENABLE_NOISE_SUPPRESSION = True
                self.config.NOISE_SUPPRESSION_METHOD = 'spectral'
            self._sync_radio_processor()
        elif char == 'w':
            if self.config.ENABLE_NOISE_SUPPRESSION and self.config.NOISE_SUPPRESSION_METHOD == 'wiener':
                self.config.ENABLE_NOISE_SUPPRESSION = False
            else:
                self.config.ENABLE_NOISE_SUPPRESSION = True
                self.config.NOISE_SUPPRESSION_METHOD = 'wiener'
            self._sync_radio_processor()
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
                    # Auto-set RTS to Radio Controlled for TX playback
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
                    self.playback_source.queue_file(stored_path)
        elif char == '-':
            if self.playback_source:
                self.playback_source.stop_playback()
        elif char in ('[', ']', '\\'):
            slot = {'[': 1, ']': 2, '\\': 3}[char]
            if self.smart_announce and self.smart_announce._client:
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

        # Audio levels (note: rx_audio_level = Mumble→Radio TX, tx_audio_level = Radio→Mumble RX)
        radio_tx = getattr(self, 'rx_audio_level', 0)
        # Match console: show 0% when VAD is blocking (not actually transmitting)
        if self.config.ENABLE_VAD and not self.vad_active:
            radio_rx = 0
        else:
            radio_rx = getattr(self, 'tx_audio_level', 0)
        sdr1_level = self.sdr_source.audio_level if self.sdr_source and hasattr(self.sdr_source, 'audio_level') else 0
        sdr2_level = self.sdr2_source.audio_level if self.sdr2_source and hasattr(self.sdr2_source, 'audio_level') else 0
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
            for sa_id, sa_secs in self.smart_announce.get_countdowns():
                sd, sr = divmod(int(sa_secs), 86400)
                sh, sr2 = divmod(sr, 3600)
                sm, ss = divmod(sr2, 60)
                sa_countdowns.append({'id': sa_id, 'remaining': f"{sd}d {sh:02d}:{sm:02d}:{ss:02d}"})

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
            'sdr1_duck': self.sdr_source.duck if self.sdr_source and hasattr(self.sdr_source, 'duck') else False,
            'sdr_rebroadcast': getattr(self, 'sdr_rebroadcast', False),
            'remote_muted': getattr(self, 'remote_audio_muted', False),
            'announce_muted': getattr(self, 'announce_input_muted', False),
            'speaker_muted': getattr(self, 'speaker_muted', True),
            'radio_rx': radio_rx,
            'radio_tx': radio_tx,
            'sdr1_level': sdr1_level,
            'sdr2_level': sdr2_level,
            'remote_level': sv_level if self.remote_audio_server else cl_level,
            'remote_mode': 'SV' if self.remote_audio_server else 'CL',
            'speaker_level': speaker_level,
            'an_level': an_level,
            'volume': round(self.config.INPUT_VOLUME, 1),
            'processing': proc,
            'radio_proc': proc,
            'sdr_proc': sdr_proc,
            'smart_countdowns': sa_countdowns,
            'smart_activity': self.smart_announce.get_activity() if self.smart_announce and hasattr(self.smart_announce, 'get_activity') else {},
            'ddns': ddns_status,
            'charger': charger_state,
            'cat': cat_state,
            'cat_reliability': cat_reliability,
            'cat_vol': cat_vol,
            'relay_pressing': getattr(self, '_relay_radio_pressing', False),
            'sdr1_enabled': bool(self.sdr_source),
            'sdr2_enabled': bool(self.sdr2_source),
            'speaker_enabled': bool(self.speaker_stream),
            'remote_enabled': bool(self.remote_audio_source or self.remote_audio_server),
            'announce_enabled': bool(self.announce_input_source),
            'relay_radio_enabled': bool(self.relay_radio),
            'relay_charger_enabled': bool(self.relay_charger),
            'ms1_state': self.mumble_server_1.state if self.mumble_server_1 else None,
            'ms2_state': self.mumble_server_2.state if self.mumble_server_2 else None,
            'cat_enabled': bool(self.cat_client) or getattr(self.config, 'ENABLE_CAT_CONTROL', False),
            'files': file_slots,
            # Broadcastify / DarkIce streaming
            'streaming_enabled': bool(getattr(self.config, 'ENABLE_STREAM_OUTPUT', False)),
            'stream_pipe_ok': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'darkice_running': self._darkice_pid is not None,
            'darkice_pid': self._darkice_pid,
            'darkice_restarts': self._darkice_restart_count,
            'stream_restarts': self.stream_restart_count,
            'stream_health': bool(getattr(self.config, 'ENABLE_STREAM_HEALTH', False)),
            'darkice_stats': self._get_darkice_stats_cached(),
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
                BLUE = '\033[94m'
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
                # PTT method tag for status label
                _ptt_m = str(getattr(self.config, 'PTT_METHOD', 'aioc')).lower()
                _ptt_tag = {'aioc': '', 'relay': 'R', 'software': 'S'}.get(_ptt_m, '?')
                _ptt_label = f"PTT{_ptt_tag}" if _ptt_tag else "PTT"
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
                    manual_tag = "*" if self._charger_manual else ""
                    if self.relay_charger_on:
                        relay_bar += f" {WHITE}CHG:{GREEN}CHRGE{manual_tag}{RESET}"
                    else:
                        relay_bar += f" {WHITE}CHG:{RED}DRAIN{manual_tag}{RESET}"

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

                # Line 1: audio indicators + file slots
                _file_bar = ""
                if self.playback_source:
                    _file_bar = f" {self.playback_source.get_file_status_string()}"
                status_line = f"{status_symbol} {WHITE}M:{RESET}{mumble_status} {WHITE}{_ptt_label}:{RESET}{ptt_status} {WHITE}VAD:{RESET}{vad_status}{vad_info} {WHITE}TX:{RESET}{radio_tx_bar} {WHITE}RX:{RESET}{radio_rx_bar}{sp_bar}{sdr_bar}{sdr2_bar}{remote_bar}{annin_bar}{_file_bar}     "

                # Line 2: uptime, files, relays, CAT, servers, vol, proc, diag, smart countdowns
                def _fmt_hms(secs):
                    d, rem = divmod(int(secs), 86400)
                    h, rem = divmod(rem, 3600)
                    m, s = divmod(rem, 60)
                    return f"{d}d {h:02d}:{m:02d}:{s:02d}"

                uptime_s = current_time - self.start_time
                line2_parts = [f"{WHITE}UP:{CYAN}{_fmt_hms(uptime_s)}{RESET}"]

                if self.smart_announce:
                    for sa_id, sa_rem in self.smart_announce.get_countdowns():
                        line2_parts.append(f"{WHITE}S{sa_id}:{MAGENTA}{_fmt_hms(sa_rem)}{RESET}")

                if self.web_config_server:
                    _web_port = getattr(self.config, 'WEB_CONFIG_PORT', 8080)
                    _https_val = str(getattr(self.config, 'WEB_CONFIG_HTTPS', 'false')).lower().strip()
                    _web_s = 'S' if _https_val not in ('false', '0', 'no', '') else ''
                    line2_parts.append(f"{WHITE}WEB{_web_s}:{GREEN}{_web_port}{RESET}")

                # Line 3: network info (DNS IP + Cloudflare tunnel URL)
                line3_parts = []
                if self.ddns_updater:
                    _dns_st = self.ddns_updater.get_status()
                    _dns_color = YELLOW if self.ddns_updater._last_status in ('good', 'nochg') else (RED if self.ddns_updater._last_status else YELLOW)
                    _dns_host = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '')
                    line3_parts.append(f"{WHITE}DNS:{_dns_color}{_dns_host} → {_dns_st}{RESET}")

                if self.cloudflare_tunnel:
                    _cf_url = self.cloudflare_tunnel.get_url()
                    if _cf_url:
                        line3_parts.append(f"{WHITE}CF:{YELLOW}{_cf_url}{RESET}")
                    else:
                        line3_parts.append(f"{WHITE}CF:{YELLOW}starting...{RESET}")

                line2_tail = f"{relay_bar}{cat_bar}{msrv_bar}{vol_info}{proc_info}{diag}"
                if line2_tail.strip():
                    line2_parts.append(line2_tail.strip())

                status_line2 = "  ".join(line2_parts) + "     "
                status_line3 = "  ".join(line3_parts) + "     " if line3_parts else None

                # Truncate all lines to terminal width to prevent line wrapping
                try:
                    import shutil as _shutil
                    _term_cols = _shutil.get_terminal_size().columns
                    import re as _re

                    def _truncate_ansi(s, maxw):
                        vlen = len(_re.sub(r'\033\[[0-9;]*m', '', s))
                        if vlen <= maxw:
                            return s
                        out = []
                        vc = 0
                        i = 0
                        while i < len(s) and vc < maxw:
                            if s[i] == '\033':
                                j = s.find('m', i)
                                if j != -1:
                                    out.append(s[i:j+1])
                                    i = j + 1
                                    continue
                            out.append(s[i])
                            vc += 1
                            i += 1
                        return ''.join(out) + RESET

                    status_line = _truncate_ansi(status_line, _term_cols - 1)
                    status_line2 = _truncate_ansi(status_line2, _term_cols - 1)
                    if status_line3:
                        status_line3 = _truncate_ansi(status_line3, _term_cols - 1)
                except Exception:
                    pass

                self._status_writer.draw_status(status_line, line2=status_line2, line3=status_line3)
            
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
        print("  Relay: 'j'=Radio power button  'h'=Charger toggle  'l'=Send CAT config")
        print("  Smart: '['=Smart#1  ']'=Smart#2  '\\'=Smart#3")
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
        headless = getattr(self.config, 'HEADLESS_MODE', False)
        buf_lines = int(getattr(self.config, 'LOG_BUFFER_LINES', 2000))
        self._status_writer = StatusBarWriter(
            sys.stdout, headless=headless, buffer_lines=buf_lines, log_file=log_file
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
    gateway = RadioGateway(config)
    
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
