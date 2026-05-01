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
            'TX_RADIO': 'th9800',              # 'th9800', 'd75', or 'kv4p' — which radio for playback/TTS/announce TX
            'TX_TALKBACK': False,              # When True, TX audio (TTS/CW/announce) is also sent to local outputs (speaker/Mumble/stream/PCM)
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
            'ENABLE_NOISE_GATE': False,
            'NOISE_GATE_THRESHOLD': -40,
            'NOISE_GATE_ATTACK': 0.01,  # float (seconds)
            'NOISE_GATE_RELEASE': 0.1,  # float (seconds)
            'ENABLE_HIGHPASS_FILTER': True,
            'HIGHPASS_CUTOFF_FREQ': 300,
            'ENABLE_ECHO_CANCELLATION': False,
            'ENABLE_LOWPASS_FILTER': False,
            'LOWPASS_CUTOFF_FREQ': 3000,
            'ENABLE_NOTCH_FILTER': False,
            'NOTCH_FREQ': 1000,
            'NOTCH_Q': 30.0,
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
            'INPUT_VOLUME': 1.0,
            'OUTPUT_VOLUME': 1.0,
            'MUMBLE_LOOP_RATE': 0.01,
            'MUMBLE_STEREO': False,
            'MUMBLE_RECONNECT': True,
            'MUMBLE_DEBUG': False,
            'NETWORK_TIMEOUT': 10,
            'HEADLESS_MODE': True,          # No console status bar, log to file + web UI
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
            'TTS_ENGINE': 'edge',  # gtts (Google, robotic) or edge (Microsoft Neural, natural)
            'TTS_VOLUME': 1.0,  # Volume multiplier for TTS audio (1.0 = normal, 2.0 = double, 3.0 = triple)
            'TTS_SPEED': 1.3,   # Speech speed (1.0 = normal, 1.3 = 30% faster, 0.8 = slower, requires ffmpeg)
            'TTS_DEFAULT_VOICE': 1, # Default voice (1=US, 2=British, 3=Australian, 4=Indian, 5=SA, 6=Canadian, 7=Irish, 8=French, 9=German)
            'PTT_TTS_DELAY': 0.5,   # Silence padding before TTS (seconds) to prevent cutoff
            'PTT_ANNOUNCEMENT_DELAY': 0.5,  # Seconds after PTT key-up before announcement audio starts
            # SDR Integration
            'SDR_MODE': 'dual',             # 'dual' (master+slave) or 'single' (one tuner, multi-channel)
            'ENABLE_SDR': True,
            'SDR_DEVICE_NAME': 'pw:sdr_capture',  # PipeWire sink (recommended) or ALSA device (e.g., 'hw:6,1')
            'SDR_DUCK': True,             # Duck SDR: silence SDR when higher priority source is active
            'SDR_MIX_RATIO': 1.0,        # Volume/mix ratio when ducking is disabled (1.0 = full volume)
            'SDR_DISPLAY_GAIN': 1.0,     # Display sensitivity multiplier (1.0 = normal, higher = more sensitive bar)
            'SDR_AUDIO_BOOST': 1.0,      # Actual audio volume boost (1.0 = no change, 2.0 = 2x louder)
            'SDR_BUFFER_MULTIPLIER': 4,  # Buffer size multiplier (4 = 4x normal buffer, ~200ms per ALSA read)
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
            'SDR2_WATCHDOG_TIMEOUT': 10,
            'SDR2_WATCHDOG_MAX_RESTARTS': 5,
            'SDR2_WATCHDOG_MODPROBE': False,
            'SDR_PRIORITY_ORDER': 'sdr1',    # which SDR ducks the other: sdr1 / sdr2 / equal
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
            'ENABLE_SPEAKER_OUTPUT': True,
            'SPEAKER_OUTPUT_DEVICE': '',   # '' = system default; or partial name e.g. 'USB Audio', 'hw:2,0'
            'SPEAKER_MODE': 'virtual',     # 'virtual' = metering only (no audio device), 'auto' = try device then fallback, 'real' = require device
            'SPEAKER_VOLUME': 1.0,         # float multiplier
            'SPEAKER_START_MUTED': False,  # Start with speaker unmuted
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
            # Web Monitor (browser mic → mixer via WebSocket, no PTT)
            'ENABLE_WEB_MONITOR': True,
            # Soundboard — auto-fill empty playback slots with random sound effects
            'ENABLE_SOUNDBOARD': True,
            # Relay Control — Radio Power
            'ENABLE_RELAY_RADIO': False,
            'RELAY_RADIO_DEVICE': '/dev/relay_radio',
            'RELAY_RADIO_BAUD': 9600,
            # Relay Control — Charger Schedule
            'ENABLE_RELAY_CHARGER': False,
            'RELAY_CHARGER_CONTROL': 'gpio',   # 'gpio' or 'serial'
            'CHARGER_RELAY_GPIO': 23,           # BCM pin when RELAY_CHARGER_CONTROL = gpio
            'RELAY_CHARGER_DEVICE': '/dev/relay_charger',
            'RELAY_CHARGER_BAUD': 9600,
            'RELAY_CHARGER_ON_TIME': '23:00',
            'RELAY_CHARGER_OFF_TIME': '06:00',
            # Smart Announcements (AI-powered via Claude CLI)
            'ENABLE_SMART_ANNOUNCE': True,
            'SMART_ANNOUNCE_TOP_TEXT': '',           # Global top text (used if slot has none)
            'SMART_ANNOUNCE_TAIL_TEXT': '',          # Global tail text (used if slot has none)
            'SMART_ANNOUNCE_START_TIME': '08:00',   # HH:MM — empty = no restriction
            'SMART_ANNOUNCE_END_TIME': '22:00',     # HH:MM — empty = no restriction
            # Smart Announcement Slots (1-3, match dashboard buttons)
            'SMART_ANNOUNCE_1_PROMPT': '',           # prompt/search text
            'SMART_ANNOUNCE_1_INTERVAL': 3600,       # seconds between announcements
            'SMART_ANNOUNCE_1_VOICE': 1,             # TTS voice (1-9)
            'SMART_ANNOUNCE_1_TARGET_SECS': 15,      # max speech length in seconds
            'SMART_ANNOUNCE_1_MODE': 'auto',         # auto = scheduled, manual = web UI only
            'SMART_ANNOUNCE_1_TOP_TEXT': '',          # text spoken before (blank = use global)
            'SMART_ANNOUNCE_1_TAIL_TEXT': '',         # text spoken after (blank = use global)
            'SMART_ANNOUNCE_2_PROMPT': '',
            'SMART_ANNOUNCE_2_INTERVAL': 3600,
            'SMART_ANNOUNCE_2_VOICE': 1,
            'SMART_ANNOUNCE_2_TARGET_SECS': 15,
            'SMART_ANNOUNCE_2_MODE': 'auto',
            'SMART_ANNOUNCE_2_TOP_TEXT': '',
            'SMART_ANNOUNCE_2_TAIL_TEXT': '',
            'SMART_ANNOUNCE_3_PROMPT': '',
            'SMART_ANNOUNCE_3_INTERVAL': 3600,
            'SMART_ANNOUNCE_3_VOICE': 1,
            'SMART_ANNOUNCE_3_TARGET_SECS': 15,
            'SMART_ANNOUNCE_3_MODE': 'auto',
            'SMART_ANNOUNCE_3_TOP_TEXT': '',
            'SMART_ANNOUNCE_3_TAIL_TEXT': '',
            # TH-9800 CAT Control
            'ENABLE_TH9800': False,       # Alias for ENABLE_CAT_CONTROL
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
            # D75 (link endpoint — legacy plugin config removed)
            'ENABLE_D75': False,
            # D75 audio processing
            'D75_PROC_ENABLE_NOISE_GATE': False,
            'D75_PROC_NOISE_GATE_THRESHOLD': -40,
            'D75_PROC_NOISE_GATE_ATTACK': 0.01,
            'D75_PROC_NOISE_GATE_RELEASE': 0.1,
            'D75_PROC_ENABLE_HPF': True,
            'D75_PROC_HPF_CUTOFF': 300,
            'D75_PROC_ENABLE_LPF': False,
            'D75_PROC_LPF_CUTOFF': 3000,
            'D75_PROC_ENABLE_NOTCH': False,
            'D75_PROC_NOTCH_FREQ': 1000,
            'D75_PROC_NOTCH_Q': 30.0,
            # KV4P HT Radio (USB serial, Opus audio)
            'ENABLE_KV4P': False,
            'KV4P_PORT': '/dev/ttyUSB0',       # Serial port for KV4P HT
            'KV4P_FREQ': 146.520,               # Default frequency (MHz)
            'KV4P_TX_FREQ': 0.0,                # TX frequency (0 = same as RX)
            'KV4P_SQUELCH': 4,                   # Squelch level 0-8
            'KV4P_CTCSS_TX': 0,                  # TX CTCSS tone code (0 = none)
            'KV4P_CTCSS_RX': 0,                  # RX CTCSS tone code (0 = none)
            'KV4P_BANDWIDTH': 1,                  # 0 = narrow (12.5 kHz), 1 = wide (25 kHz)
            'KV4P_HIGH_POWER': True,             # True = high power, False = low power
            'KV4P_AUDIO_DUCK': True,             # Duck KV4P when higher priority source active
            'KV4P_AUDIO_PRIORITY': 2,            # Priority for ducking
            'KV4P_AUDIO_DISPLAY_GAIN': 1.0,      # Display level sensitivity
            'KV4P_AUDIO_BOOST': 1.0,             # RX volume multiplier
            'KV4P_RECONNECT_INTERVAL': 5.0,      # Seconds between reconnect attempts
            'KV4P_SMETER': True,                  # Enable S-meter reporting
            # KV4P audio processing
            'KV4P_PROC_ENABLE_NOISE_GATE': False,
            'KV4P_PROC_NOISE_GATE_THRESHOLD': -40,
            'KV4P_PROC_NOISE_GATE_ATTACK': 0.01,
            'KV4P_PROC_NOISE_GATE_RELEASE': 0.1,
            'KV4P_PROC_ENABLE_HPF': True,
            'KV4P_PROC_HPF_CUTOFF': 300,
            'KV4P_PROC_ENABLE_LPF': False,
            'KV4P_PROC_LPF_CUTOFF': 3000,
            'KV4P_PROC_ENABLE_NOTCH': False,
            'KV4P_PROC_NOTCH_FREQ': 1000,
            'KV4P_PROC_NOTCH_Q': 30.0,
            # Packet Radio (Direwolf TNC)
            'ENABLE_PACKET': False,
            'PACKET_CALLSIGN': 'N0CALL',
            'PACKET_SSID': 0,
            'PACKET_MODEM': 1200,
            'PACKET_DIREWOLF_PATH': '/usr/bin/direwolf',
            'PACKET_REMOTE_TNC': '',
            'PACKET_UDP_RX_PORT': 7355,
            'PACKET_KISS_PORT': 8001,
            'PACKET_AGW_PORT': 8000,
            'PACKET_PAT_PORT': 8082,
            'PACKET_LOOPBACK_CARD': 'Loopback_1',
            'PACKET_APRS_COMMENT': 'Radio Gateway',
            'PACKET_APRS_SYMBOL': '/#',
            'PACKET_APRS_BEACON_INTERVAL': 600,
            'PACKET_DIGIPEAT': True,
            'PACKET_APRS_IS': False,
            'PACKET_APRS_IS_SERVER': 'noam.aprs2.net',
            'PACKET_APRS_IS_PASSCODE': '',
            # Dynamic DNS (No-IP compatible)
            # Web Configuration UI
            'ENABLE_WEB_CONFIG': True,
            'WEB_CONFIG_PORT': 8080,
            'WEB_CONFIG_PASSWORD': '',    # Basic auth password (user: admin), blank = no auth
            'WEB_CONFIG_HTTPS': False,    # false, self-signed, or letsencrypt
            'GATEWAY_NAME': '',           # Display name shown at top of dashboard (blank = none)
            'WEB_THEME': 'grey',          # Dashboard color theme: grey, blue, red, green, purple, amber, teal, pink
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
            'ENABLE_DDNS': True,
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
            # USB/IP — share USB devices from a remote machine over TCP
            'ENABLE_USBIP': True,
            'USBIP_SERVER': '',              # IP or hostname of the USB/IP server
            'USBIP_DEVICES': '',             # Comma-separated bus IDs to attach, e.g. "1-1.4,1-1.3"
            # ADS-B aircraft tracking (RTL-SDR via dump1090-fa + FlightRadar24)
            'ENABLE_ADSB': True,
            'ADSB_PORT': 30080,          # dump1090-fa HTTP port (30080 avoids conflict with gateway on 8080)
            # Automation Engine
            'ENABLE_AUTOMATION': False,
            'AUTOMATION_SCHEME_FILE': 'automation_scheme.txt',
            'AUTOMATION_REPEATER_FILE': '',       # RepeaterBook CSV path
            'AUTOMATION_REPEATER_LAT': 0.0,       # Home latitude for distance calc
            'AUTOMATION_REPEATER_LON': 0.0,       # Home longitude for distance calc
            'AUTOMATION_RECORDINGS_DIR': 'recordings',
            'AUTOMATION_START_TIME': '06:00',      # Daily start time (HH:MM)
            'AUTOMATION_END_TIME': '23:00',        # Daily end time (HH:MM)
            'AUTOMATION_MAX_TASK_DURATION': 600,   # Max seconds per task

            # Gateway Link (duplex audio + command protocol for remote endpoints)
            'ENABLE_GATEWAY_LINK': False,
            'LINK_PORT': 9700,
            'LINK_AUDIO_DUCK': False,
            'LINK_AUDIO_PRIORITY': 3,
            'LINK_AUDIO_BOOST': 1.0,
            'LINK_AUDIO_DISPLAY_GAIN': 1.0,

            # Telegram Bot
            'ENABLE_TELEGRAM': False,
            'TELEGRAM_BOT_TOKEN': '',
            'TELEGRAM_CHAT_ID': 0,
            'TELEGRAM_TMUX_SESSION': 'claude-gateway',

            # Advanced
            'START_CLAUDE_CODE': False,
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

        # Sync ENABLE_TH9800 / ENABLE_CAT_CONTROL aliases (either one enables TH-9800)
        if getattr(self, 'ENABLE_TH9800', False) and not getattr(self, 'ENABLE_CAT_CONTROL', False):
            self.ENABLE_CAT_CONTROL = True
        elif getattr(self, 'ENABLE_CAT_CONTROL', False) and not getattr(self, 'ENABLE_TH9800', False):
            self.ENABLE_TH9800 = True

from gateway_core import RadioGateway

_GATEWAY_LOCK = '/tmp/gateway.lock'
_STARTUP_LOG = '/tmp/gateway_startup.log'


def _pre_flight(config):
    """Pre-flight checks absorbed from start.sh.

    Runs before RadioGateway init:
    1. Kill stale processes from prior runs
    2. Start TH-9800 CAT systemd service (if enabled)
    3. Set CPU governor to performance
    4. Reset AIOC USB device
    """
    import glob

    script_dir = os.path.dirname(os.path.abspath(__file__))

    def _run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, **kw)

    def _sudo(cmd):
        try:
            return _run(['sudo', '-n'] + cmd)
        except Exception:
            return None

    print("=" * 42)
    print(f"[{time.strftime('%H:%M:%S')}] Starting Radio Gateway")
    print("=" * 42)

    # 1. Kill stale processes
    print(f"[{time.strftime('%H:%M:%S')}] [1/4] Checking for stale processes...")
    for proc in ['darkice']:
        try:
            _run(['pkill', '-9', proc])
        except Exception:
            pass
    # Kill stale gateway (but not us)
    try:
        result = _run(['pgrep', '-f', 'radio_gateway.py'])
        if result.returncode == 0:
            for pid_str in result.stdout.strip().split('\n'):
                pid = int(pid_str.strip())
                if pid != os.getpid():
                    try:
                        os.kill(pid, 9)
                        print(f"  Killed stale gateway PID {pid}")
                    except OSError:
                        pass
    except Exception:
        pass
    # Stop leftover mumble-server instances
    for svc in ['mumble-server-gw1', 'mumble-server-gw2']:
        try:
            result = _run(['systemctl', 'is-active', '--quiet', f'{svc}.service'])
            if result.returncode == 0:
                _sudo(['systemctl', 'stop', f'{svc}.service'])
                print(f"  Stopped {svc}")
        except Exception:
            pass

    # 2. Start TH-9800 CAT service
    print(f"[{time.strftime('%H:%M:%S')}] [2/4] Checking TH-9800 CAT control...")
    if getattr(config, 'ENABLE_TH9800', False) or getattr(config, 'ENABLE_CAT_CONTROL', False):
        try:
            result = _run(['systemctl', 'is-active', '--quiet', 'th9800-cat.service'])
            if result.returncode == 0:
                _sudo(['systemctl', 'restart', 'th9800-cat.service'])
            else:
                _sudo(['systemctl', 'start', 'th9800-cat.service'])
            # Wait for TCP port
            cat_port = int(getattr(config, 'CAT_PORT', 9800))
            for _ in range(20):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect(('127.0.0.1', cat_port))
                    s.close()
                    print(f"  TH-9800 CAT service ready (port {cat_port})")
                    break
                except (ConnectionRefusedError, OSError):
                    time.sleep(0.5)
            else:
                print(f"  TH-9800 CAT service failed to start")
        except Exception as e:
            print(f"  TH-9800 CAT error: {e}")
    else:
        print("  Disabled in config")

    # 3. CPU governor
    print(f"[{time.strftime('%H:%M:%S')}] [3/4] Setting CPU governor to performance...")
    for gov_path in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'):
        try:
            _sudo(['tee', gov_path]).input = 'performance'
            # Use shell for tee stdin
            subprocess.run(f'echo performance | sudo -n tee {gov_path} > /dev/null 2>&1',
                           shell=True, timeout=5)
        except Exception:
            pass
    print("  CPU governor set (or unsupported)")

    # 4. AIOC USB reset
    print(f"[{time.strftime('%H:%M:%S')}] [4/4] Resetting AIOC USB device...")
    aioc_path = None
    for product_file in glob.glob('/sys/bus/usb/devices/*/product'):
        try:
            with open(product_file) as f:
                if 'all-in-one' in f.read().lower():
                    aioc_path = os.path.dirname(product_file)
                    break
        except Exception:
            pass
    if aioc_path:
        auth_file = os.path.join(aioc_path, 'authorized')
        if os.path.exists(auth_file):
            print(f"  Found AIOC at {aioc_path}")
            try:
                subprocess.run(
                    f'echo 0 | sudo -n tee {auth_file} > /dev/null && sleep 1 && echo 1 | sudo -n tee {auth_file} > /dev/null',
                    shell=True, timeout=10)
                time.sleep(2)  # USB re-enumeration
                print("  AIOC USB reset complete")
            except Exception as e:
                print(f"  AIOC USB reset failed: {e}")
        else:
            print("  AIOC authorized file not found")
    else:
        print("  AIOC USB device not found (skipping reset)")

    print(f"[{time.strftime('%H:%M:%S')}] Pre-flight complete")


def _cleanup_services(config):
    """Post-shutdown cleanup — stop services started by pre-flight."""
    try:
        if getattr(config, 'ENABLE_TH9800', False) or getattr(config, 'ENABLE_CAT_CONTROL', False):
            subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'th9800-cat.service'],
                           capture_output=True, timeout=10)
    except Exception:
        pass
    # Kill any rtl_airband processes (SDR plugin children)
    try:
        subprocess.run(['sudo', '-n', 'killall', '-9', 'rtl_airband'],
                       capture_output=True, timeout=5)
    except Exception:
        pass

def _acquire_lock():
    """Write PID lockfile. Exit if another instance is already running."""
    if os.path.exists(_GATEWAY_LOCK):
        try:
            old_pid = int(open(_GATEWAY_LOCK).read().strip())
            os.kill(old_pid, 0)  # signal 0: just checks process exists
            print(f"ERROR: Gateway already running (PID {old_pid}).")
            print("       Stop the existing instance first.")
            sys.exit(1)
        except (ValueError, OSError):
            pass  # stale lock — overwrite
    try:
        with open(_GATEWAY_LOCK, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Warning: Could not write lockfile {_GATEWAY_LOCK}: {e}")

def _release_lock():
    """Remove lockfile if it belongs to this process."""
    try:
        if open(_GATEWAY_LOCK).read().strip() == str(os.getpid()):
            os.unlink(_GATEWAY_LOCK)
    except Exception:
        pass

def main():
    _acquire_lock()

    # Find config file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(script_dir, "gateway_config.txt")

    # Load configuration
    config = Config(config_file)

    # Pre-flight: kill stale procs, start CAT, set governor, reset AIOC
    _pre_flight(config)

    # Create and run gateway
    gateway = RadioGateway(config)

    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        gateway.running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    gateway.run()

    # Post-shutdown cleanup
    _cleanup_services(config)
    _release_lock()

    if gateway.restart_requested:
        print("\nRestarting gateway...")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

if __name__ == "__main__":
    main()

