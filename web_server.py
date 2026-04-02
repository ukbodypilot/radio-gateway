#!/usr/bin/env python3
"""Web configuration and dashboard server for radio-gateway."""

import sys
import os
import time
import signal
import threading
import threading as _thr
import subprocess
import shutil
import json as json_mod
import collections
import queue as _queue_mod
from struct import Struct
import socket
import array as _array_mod
import math as _math_mod
import re
import numpy as np

from audio_sources import generate_cw_pcm
from smart_announce import SmartAnnouncementManager
from cat_client import RadioCATClient

# ============================================================================
# WEB CONFIGURATION UI
# ============================================================================

class WebConfigServer:
    """Lightweight web UI for editing gateway_config.txt.

    Runs Python's built-in http.server on a daemon thread.  Serves a
    single-page form grouped by INI sections with Save and Save & Restart.
    """

    # Keys whose values should be masked in the UI
    _SENSITIVE_KEYS = {'TELEGRAM_BOT_TOKEN', 'STREAM_PASSWORD', 'EMAIL_APP_PASSWORD', 'WEB_CONFIG_PASSWORD'}

    # Keys that store hex integers
    _HEX_KEYS = {'AIOC_VID', 'AIOC_PID'}

    # Keys that get a visual separator line above them in the config UI
    _GROUP_SEPARATOR_KEYS = {
        'SMART_ANNOUNCE_1_PROMPT',
        'SMART_ANNOUNCE_2_PROMPT',
        'SMART_ANNOUNCE_3_PROMPT',
    }

    # Hint text for parameters — shows units, ranges, and format info
    _FIELD_HINTS = {
        # Mumble
        'MUMBLE_SERVER': 'IP address or hostname',
        'MUMBLE_PORT': 'port (1–65535)',
        'MUMBLE_CHANNEL': 'blank = root channel',
        'MUMBLE_BITRATE': 'bps (typical: 32000–128000)',
        'MUMBLE_JITTER_BUFFER': 'ms',
        'MUMBLE_LOOP_RATE': 'seconds',
        # Radio
        'AIOC_VID': 'hex USB vendor ID',
        'AIOC_PID': 'hex USB product ID',
        'AIOC_INPUT_DEVICE': '-1 = auto-detect',
        'AIOC_OUTPUT_DEVICE': '-1 = auto-detect',
        # Audio
        'AUDIO_RATE': 'Hz (samples/sec)',
        'AUDIO_CHUNK_SIZE': 'samples (rate ÷ 20 = 50ms)',
        'MAX_MUMBLE_BUFFER_SECONDS': 'seconds',
        # Levels
        'INPUT_VOLUME': 'multiplier (0.1–3.0, 1.0 = normal)',
        'OUTPUT_VOLUME': 'multiplier (1.0 = normal)',
        # PTT
        'PTT_RELAY_DEVICE': 'device path',
        'PTT_RELAY_BAUD': 'bps',
        'PTT_RELEASE_DELAY': 'seconds',
        'PTT_ACTIVATION_DELAY': 'seconds',
        'PTT_TTS_DELAY': 'seconds (silence before TTS)',
        'PTT_ANNOUNCEMENT_DELAY': 'seconds (silence after PTT key-up)',
        # VAD
        'VAD_THRESHOLD': 'dBFS (−60 to 0, lower = more sensitive)',
        'VAD_ATTACK': 'seconds',
        'VAD_RELEASE': 'seconds',
        'VAD_MIN_DURATION': 'seconds',
        # VOX
        'VOX_THRESHOLD': 'dBFS (−60 to 0, lower = more sensitive)',
        'VOX_ATTACK_TIME': 'seconds',
        'VOX_RELEASE_TIME': 'seconds',
        # Processing
        'NOISE_GATE_THRESHOLD': 'dBFS (−60 to 0)',
        'NOISE_GATE_ATTACK': 'seconds',
        'NOISE_GATE_RELEASE': 'seconds',
        'HIGHPASS_CUTOFF_FREQ': 'Hz',
        'LOWPASS_CUTOFF_FREQ': 'Hz',
        'NOTCH_FREQ': 'Hz',
        'NOTCH_Q': 'quality factor (higher = narrower)',
        # SDR Processing
        'SDR_PROC_NOISE_GATE_THRESHOLD': 'dBFS (−60 to 0)',
        'SDR_PROC_NOISE_GATE_ATTACK': 'seconds',
        'SDR_PROC_NOISE_GATE_RELEASE': 'seconds',
        'SDR_PROC_HPF_CUTOFF': 'Hz',
        'SDR_PROC_LPF_CUTOFF': 'Hz',
        'SDR_PROC_NOTCH_FREQ': 'Hz',
        'SDR_PROC_NOTCH_Q': 'quality factor (higher = narrower)',
        # D75 Processing
        'D75_PROC_NOISE_GATE_THRESHOLD': 'dBFS (−60 to 0)',
        'D75_PROC_NOISE_GATE_ATTACK': 'seconds',
        'D75_PROC_NOISE_GATE_RELEASE': 'seconds',
        'D75_PROC_HPF_CUTOFF': 'Hz',
        'D75_PROC_LPF_CUTOFF': 'Hz',
        'D75_PROC_NOTCH_FREQ': 'Hz',
        'D75_PROC_NOTCH_Q': 'quality factor (higher = narrower)',
        # KV4P HT
        'KV4P_PORT': 'serial port (e.g. /dev/ttyUSB0)',
        'KV4P_FREQ': 'MHz',
        'KV4P_TX_FREQ': 'MHz (0 = same as RX)',
        'KV4P_SQUELCH': '0-8',
        'KV4P_CTCSS_TX': 'TX CTCSS tone',
        'KV4P_CTCSS_RX': 'RX CTCSS tone',
        'KV4P_BANDWIDTH': 'FM bandwidth mode',
        'KV4P_RECONNECT_INTERVAL': 'seconds',
        'KV4P_PROC_NOISE_GATE_THRESHOLD': 'dBFS (-60 to 0)',
        'KV4P_PROC_NOISE_GATE_ATTACK': 'seconds',
        'KV4P_PROC_NOISE_GATE_RELEASE': 'seconds',
        'KV4P_PROC_HPF_CUTOFF': 'Hz',
        'KV4P_PROC_LPF_CUTOFF': 'Hz',
        'KV4P_PROC_NOTCH_FREQ': 'Hz',
        'KV4P_PROC_NOTCH_Q': 'quality factor (higher = narrower)',
        # ADS-B
        'ADSB_PORT': 'port dump1090-fa web server listens on (default 30080, avoids conflict with gateway on 8080)',
        'USBIP_SERVER': 'IP address or hostname of the USB/IP server (usbipd)',
        'USBIP_DEVICES': 'comma-separated bus IDs to attach, e.g. 1-1.4,1-1.3 — leave empty to attach all exported devices',
        # SDR — RSPduo Dual Tuner (Tuner 1 = Master, Tuner 2 = Slave)
        'SDR_DEVICE_NAME': 'PipeWire monitor for Tuner 1 (default: pw:sdr_capture)',
        'SDR_MIX_RATIO': 'multiplier (when ducking disabled)',
        'SDR_DISPLAY_GAIN': 'multiplier (display sensitivity)',
        'SDR_AUDIO_BOOST': 'multiplier (1.0 = normal, 2.0 = 2× louder)',
        'SDR_BUFFER_MULTIPLIER': '× normal buffer (~50ms per unit)',
        'SDR_WATCHDOG_TIMEOUT': 'seconds',
        'SDR_WATCHDOG_MAX_RESTARTS': 'attempts',
        'SDR2_DEVICE_NAME': 'PipeWire monitor for Tuner 2 (default: pw:sdr_capture2)',
        'SDR2_MIX_RATIO': 'multiplier (when ducking disabled)',
        'SDR2_DISPLAY_GAIN': 'multiplier (display sensitivity)',
        'SDR2_AUDIO_BOOST': 'multiplier (1.0 = normal, 2.0 = 2× louder)',
        'SDR2_BUFFER_MULTIPLIER': '× normal buffer (~50ms per unit)',
        'SDR2_WATCHDOG_TIMEOUT': 'seconds',
        'SDR2_WATCHDOG_MAX_RESTARTS': 'attempts',
        'SDR_PRIORITY_ORDER': 'which SDR ducks the other when both have signal',
        # Switching
        'SIGNAL_ATTACK_TIME': 'seconds (signal needed before switch)',
        'SIGNAL_RELEASE_TIME': 'seconds (silence needed before revert)',
        'SWITCH_PADDING_TIME': 'seconds (silence at transitions)',
        'SDR_DUCK_COOLDOWN': 'seconds (hold after unduck)',
        'SDR_SIGNAL_THRESHOLD': 'dBFS (lower = more sensitive)',
        'SDR_REBROADCAST_PTT_HOLD': 'seconds',
        # Remote
        'REMOTE_AUDIO_HOST': 'IP address or hostname',
        'REMOTE_AUDIO_PORT': 'port (1–65535)',
        'REMOTE_AUDIO_PRIORITY': 'audio mix priority',
        'REMOTE_AUDIO_DISPLAY_GAIN': 'multiplier',
        'REMOTE_AUDIO_AUDIO_BOOST': 'multiplier',
        'REMOTE_AUDIO_RECONNECT_INTERVAL': 'seconds',
        # Announce
        'ANNOUNCE_INPUT_PORT': 'port (1–65535)',
        'ANNOUNCE_INPUT_HOST': 'IP address (blank = all interfaces)',
        'ANNOUNCE_INPUT_THRESHOLD': 'dBFS (below = silence)',
        'ANNOUNCE_INPUT_VOLUME': 'multiplier',
        # Playback
        'PLAYBACK_DIRECTORY': 'directory path',
        'PLAYBACK_ANNOUNCEMENT_FILE': 'file path (blank = disabled)',
        'PLAYBACK_ANNOUNCEMENT_INTERVAL': 'seconds (0 = disabled)',
        'PLAYBACK_VOLUME': 'multiplier (1.0 = normal)',
        'CW_WPM': 'words per minute',
        'CW_FREQUENCY': 'Hz (tone frequency)',
        'CW_VOLUME': 'multiplier',
        # TTS
        'TTS_VOLUME': 'multiplier (1.0 = normal)',
        'TTS_SPEED': 'ratio (1.0 = normal, 1.3 = 30% faster)',
        # Speaker
        'SPEAKER_OUTPUT_DEVICE': 'device name (blank = system default)',
        'SPEAKER_VOLUME': 'multiplier',
        # Streaming
        'STREAM_SERVER': 'hostname or IP',
        'STREAM_PORT': 'port (1–65535)',
        'STREAM_BITRATE': 'kbps',
        'STREAM_MOUNT': 'mount point path',
        'STREAM_RESTART_INTERVAL': 'seconds',
        'STREAM_RESTART_IDLE_TIME': 'seconds',
        # Echolink
        'ECHOLINK_RX_PIPE': 'named pipe path',
        'ECHOLINK_TX_PIPE': 'named pipe path',
        # Relay
        'RELAY_RADIO_DEVICE': 'device path',
        'RELAY_RADIO_BAUD': 'bps',
        'CHARGER_RELAY_GPIO': 'BCM pin number (0–27)',
        'RELAY_CHARGER_DEVICE': 'device path',
        'RELAY_CHARGER_BAUD': 'bps',
        'RELAY_CHARGER_ON_TIME': 'HH:MM (24-hour)',
        'RELAY_CHARGER_OFF_TIME': 'HH:MM (24-hour)',
        # Smart
        'SMART_ANNOUNCE_START_TIME': 'HH:MM (blank = no restriction)',
        'SMART_ANNOUNCE_END_TIME': 'HH:MM (blank = no restriction)',
        'SMART_ANNOUNCE_1_PROMPT': 'search/prompt text (blank = disabled)',
        'SMART_ANNOUNCE_1_INTERVAL': 'seconds between announcements',
        'SMART_ANNOUNCE_1_TARGET_SECS': 'max speech length (seconds, max 60)',
        'SMART_ANNOUNCE_1_MODE': 'auto = on schedule, manual = web UI trigger only',
        'SMART_ANNOUNCE_1_TOP_TEXT': 'spoken before (blank = use global)',
        'SMART_ANNOUNCE_1_TAIL_TEXT': 'spoken after (blank = use global)',
        'SMART_ANNOUNCE_2_PROMPT': 'search/prompt text (blank = disabled)',
        'SMART_ANNOUNCE_2_INTERVAL': 'seconds between announcements',
        'SMART_ANNOUNCE_2_TARGET_SECS': 'max speech length (seconds, max 60)',
        'SMART_ANNOUNCE_2_MODE': 'auto = on schedule, manual = web UI trigger only',
        'SMART_ANNOUNCE_2_TOP_TEXT': 'spoken before (blank = use global)',
        'SMART_ANNOUNCE_2_TAIL_TEXT': 'spoken after (blank = use global)',
        'SMART_ANNOUNCE_3_PROMPT': 'search/prompt text (blank = disabled)',
        'SMART_ANNOUNCE_3_INTERVAL': 'seconds between announcements',
        'SMART_ANNOUNCE_3_TARGET_SECS': 'max speech length (seconds, max 60)',
        'SMART_ANNOUNCE_3_MODE': 'auto = on schedule, manual = web UI trigger only',
        'SMART_ANNOUNCE_3_TOP_TEXT': 'spoken before (blank = use global)',
        'SMART_ANNOUNCE_3_TAIL_TEXT': 'spoken after (blank = use global)',
        # CAT
        'CAT_HOST': 'IP address',
        'CAT_PORT': 'port (1–65535)',
        'CAT_LEFT_CHANNEL': 'channel number (−1 = don\'t change)',
        'CAT_RIGHT_CHANNEL': 'channel number (−1 = don\'t change)',
        'CAT_LEFT_VOLUME': '0–100 (−1 = don\'t change)',
        'CAT_RIGHT_VOLUME': '0–100 (−1 = don\'t change)',
        # Web
        'WEB_CONFIG_PORT': 'port (1–65535)',
        'WEB_CONFIG_PASSWORD': 'blank = no auth (user: admin)',
        'GATEWAY_NAME': 'shown at top of dashboard (blank = none)',
        'WEB_MIC_VOLUME': 'multiplier',
        # Email
        'EMAIL_ADDRESS': 'Gmail address (sender)',
        'EMAIL_APP_PASSWORD': 'Gmail app password',
        'EMAIL_RECIPIENT': 'blank = same as sender',
        # DDNS
        'DDNS_HOSTNAME': 'dynamic hostname',
        'DDNS_UPDATE_INTERVAL': 'seconds',
        'DDNS_UPDATE_URL': 'update endpoint URL',
        # Mumble servers
        'MUMBLE_SERVER_1_PORT': 'port (1–65535)',
        'MUMBLE_SERVER_1_MAX_USERS': 'users',
        'MUMBLE_SERVER_1_MAX_BANDWIDTH': 'bps',
        'MUMBLE_SERVER_2_PORT': 'port (1–65535)',
        'MUMBLE_SERVER_2_MAX_USERS': 'users',
        'MUMBLE_SERVER_2_MAX_BANDWIDTH': 'bps',
        # Advanced
        'LOG_BUFFER_LINES': 'lines (web log viewer)',
        'LOG_FILE_DAYS': 'days (log retention)',
        'STATUS_UPDATE_INTERVAL': 'seconds',
        'NETWORK_TIMEOUT': 'seconds',
    }

    # Keys with a fixed set of valid values — rendered as dropdowns
    _SELECT_OPTIONS = {
        'TX_RADIO': ['th9800', 'd75', 'kv4p'],
        'PTT_METHOD': ['aioc', 'relay', 'software'],
        'D75_CONNECTION': [('bluetooth', 'bluetooth — BT audio + CAT'), ('usb', 'usb — CAT only, no audio')],
        'SDR_PRIORITY_ORDER': [
            ('sdr1', 'SDR1 first — SDR1 ducks SDR2 when active'),
            ('sdr2', 'SDR2 first — SDR2 ducks SDR1 when active'),
            ('equal', 'Equal — both play simultaneously'),
        ],
        'KV4P_AUDIO_PRIORITY': [('0', '0 — ducks all'), ('1', '1 — high'), ('2', '2 — low')],
        'D75_AUDIO_PRIORITY': [('0', '0 — ducks all'), ('1', '1 — high'), ('2', '2 — low')],
        'REMOTE_AUDIO_PRIORITY': [('0', '0 — ducks all'), ('1', '1 — high'), ('2', '2 — low')],
        'KV4P_BANDWIDTH': [('0', '0 — Narrow'), ('1', '1 — Wide')],
        'AUDIO_CHANNELS': [('1', '1 — Mono'), ('2', '2 — Stereo')],
        'AIOC_PTT_CHANNEL': [('1', '1'), ('2', '2'), ('3', '3')],
        'REMOTE_AUDIO_ROLE': [('disabled', 'disabled'), ('server', 'enabled — connect to remote client')],
        'SPEAKER_MODE': [('virtual', 'virtual — metering only'), ('auto', 'auto — try device, fallback virtual'), ('real', 'real — require audio device')],
        'RELAY_CHARGER_CONTROL': ['gpio', 'serial'],
        'TTS_ENGINE': [('edge', 'edge — Microsoft Neural (natural)'), ('gtts', 'gtts — Google Translate (robotic)')],
        'WEB_CONFIG_HTTPS': ['false', 'self-signed', 'letsencrypt'],
        'WEB_THEME': ['blue', 'red', 'green', 'purple', 'amber', 'teal', 'pink'],
        'STREAM_FORMAT': ['mp3'],
        'CAT_LEFT_POWER': ['', 'L', 'M', 'H'],
        'CAT_RIGHT_POWER': ['', 'L', 'M', 'H'],
        'KV4P_CTCSS_TX': [('0', 'None')] + [(str(i+1), f'{t} Hz') for i, t in enumerate([
            '67.0','71.9','74.4','77.0','79.7','82.5','85.4','88.5',
            '91.5','94.8','97.4','100.0','103.5','107.2','110.9','114.8','118.8','123.0',
            '127.3','131.8','136.5','141.3','146.2','151.4','156.7','162.2','167.9',
            '173.8','179.9','186.2','192.8','203.5','210.7','218.1','225.7','233.6','241.8','250.3',
        ])],
        'KV4P_CTCSS_RX': [('0', 'None')] + [(str(i+1), f'{t} Hz') for i, t in enumerate([
            '67.0','71.9','74.4','77.0','79.7','82.5','85.4','88.5',
            '91.5','94.8','97.4','100.0','103.5','107.2','110.9','114.8','118.8','123.0',
            '127.3','131.8','136.5','141.3','146.2','151.4','156.7','162.2','167.9',
            '173.8','179.9','186.2','192.8','203.5','210.7','218.1','225.7','233.6','241.8','250.3',
        ])],
        'TTS_DEFAULT_VOICE': [
            ('1', '1 — US'), ('2', '2 — British'), ('3', '3 — Australian'),
            ('4', '4 — Indian'), ('5', '5 — South African'), ('6', '6 — Canadian'),
            ('7', '7 — Irish'), ('8', '8 — French'), ('9', '9 — German'),
        ],
        'SMART_ANNOUNCE_1_MODE': [('auto', 'auto — scheduled'), ('manual', 'manual — web UI only')],
        'SMART_ANNOUNCE_1_VOICE': [
            ('1', '1 — US'), ('2', '2 — British'), ('3', '3 — Australian'),
            ('4', '4 — Indian'), ('5', '5 — South African'), ('6', '6 — Canadian'),
            ('7', '7 — Irish'), ('8', '8 — French'), ('9', '9 — German'),
        ],
        'SMART_ANNOUNCE_2_MODE': [('auto', 'auto — scheduled'), ('manual', 'manual — web UI only')],
        'SMART_ANNOUNCE_2_VOICE': [
            ('1', '1 — US'), ('2', '2 — British'), ('3', '3 — Australian'),
            ('4', '4 — Indian'), ('5', '5 — South African'), ('6', '6 — Canadian'),
            ('7', '7 — Irish'), ('8', '8 — French'), ('9', '9 — German'),
        ],
        'SMART_ANNOUNCE_3_MODE': [('auto', 'auto — scheduled'), ('manual', 'manual — web UI only')],
        'SMART_ANNOUNCE_3_VOICE': [
            ('1', '1 — US'), ('2', '2 — British'), ('3', '3 — Australian'),
            ('4', '4 — Indian'), ('5', '5 — South African'), ('6', '6 — Canadian'),
            ('7', '7 — Irish'), ('8', '8 — French'), ('9', '9 — German'),
        ],
    }

    # Section display names
    # Canonical config layout — this is the single source of truth for
    # which settings exist, what section they belong to, and their order.
    # Both the web config UI and the config file writer use this.
    _CONFIG_LAYOUT = [
        ('adsb', 'ADS-B Aircraft Tracking', [
            'ENABLE_ADSB', 'ADSB_PORT',
        ]),
        ('announce', 'Announcement Input', [
            'ENABLE_ANNOUNCE_INPUT', 'ANNOUNCE_INPUT_PORT', 'ANNOUNCE_INPUT_HOST',
            'ANNOUNCE_INPUT_THRESHOLD', 'ANNOUNCE_INPUT_VOLUME',
        ]),
        ('audio', 'Audio Format & Buffering', [
            'AUDIO_RATE', 'AUDIO_CHUNK_SIZE', 'AUDIO_CHANNELS', 'AUDIO_BITS',
            'MAX_MUMBLE_BUFFER_SECONDS',
        ]),
        ('levels', 'Audio Levels', [
            'INPUT_VOLUME', 'OUTPUT_VOLUME',
        ]),
        ('processing', 'Audio Processing', [
            'ENABLE_AGC', 'ENABLE_NOISE_GATE', 'NOISE_GATE_THRESHOLD',
            'NOISE_GATE_ATTACK', 'NOISE_GATE_RELEASE',
            'ENABLE_HIGHPASS_FILTER', 'HIGHPASS_CUTOFF_FREQ',
            'ENABLE_LOWPASS_FILTER', 'LOWPASS_CUTOFF_FREQ',
            'ENABLE_NOTCH_FILTER', 'NOTCH_FREQ', 'NOTCH_Q',
            'ENABLE_ECHO_CANCELLATION',
        ]),
        ('automation', 'Automation Engine', [
            'ENABLE_AUTOMATION', 'AUTOMATION_SCHEME_FILE',
            'AUTOMATION_REPEATER_FILE', 'AUTOMATION_REPEATER_LAT', 'AUTOMATION_REPEATER_LON',
            'AUTOMATION_RECORDINGS_DIR',
            'AUTOMATION_START_TIME', 'AUTOMATION_END_TIME',
            'AUTOMATION_MAX_TASK_DURATION',
        ]),
        ('streaming', 'Broadcastify Streaming', [
            'ENABLE_STREAM_OUTPUT', 'STREAM_SERVER', 'STREAM_PORT',
            'STREAM_PASSWORD', 'STREAM_MOUNT', 'STREAM_NAME',
            'STREAM_DESCRIPTION', 'STREAM_BITRATE', 'STREAM_FORMAT',
            'ENABLE_STREAM_HEALTH', 'STREAM_RESTART_INTERVAL',
            'STREAM_RESTART_IDLE_TIME',
        ]),
        ('ddns', 'Dynamic DNS', [
            'ENABLE_DDNS', 'DDNS_USERNAME', 'DDNS_PASSWORD', 'DDNS_HOSTNAME',
            'DDNS_UPDATE_INTERVAL', 'DDNS_UPDATE_URL',
        ]),
        ('echolink', 'EchoLink', [
            'ENABLE_ECHOLINK', 'ECHOLINK_RX_PIPE', 'ECHOLINK_TX_PIPE',
            'ECHOLINK_TO_MUMBLE', 'ECHOLINK_TO_RADIO',
            'RADIO_TO_ECHOLINK', 'MUMBLE_TO_ECHOLINK',
        ]),
        ('email', 'Email Notifications', [
            'ENABLE_EMAIL', 'EMAIL_ADDRESS', 'EMAIL_APP_PASSWORD',
            'EMAIL_RECIPIENT', 'EMAIL_ON_STARTUP',
        ]),
        ('playback', 'File Playback', [
            'ENABLE_PLAYBACK', 'PLAYBACK_DIRECTORY',
            'PLAYBACK_ANNOUNCEMENT_FILE', 'PLAYBACK_ANNOUNCEMENT_INTERVAL',
            'PLAYBACK_VOLUME', 'ENABLE_SOUNDBOARD',
        ]),
        ('kv4p', 'KV4P HT Radio', [
            'ENABLE_KV4P', 'KV4P_PORT', 'KV4P_FREQ', 'KV4P_TX_FREQ',
            'KV4P_SQUELCH', 'KV4P_CTCSS_TX', 'KV4P_CTCSS_RX', 'KV4P_BANDWIDTH',
            'KV4P_HIGH_POWER', 'KV4P_SMETER',
            'KV4P_AUDIO_DUCK', 'KV4P_AUDIO_PRIORITY',
            'KV4P_AUDIO_DISPLAY_GAIN', 'KV4P_AUDIO_BOOST', 'KV4P_RECONNECT_INTERVAL',
            'KV4P_PROC_ENABLE_HPF', 'KV4P_PROC_HPF_CUTOFF',
            'KV4P_PROC_ENABLE_LPF', 'KV4P_PROC_LPF_CUTOFF',
            'KV4P_PROC_ENABLE_NOTCH', 'KV4P_PROC_NOTCH_FREQ', 'KV4P_PROC_NOTCH_Q',
            'KV4P_PROC_ENABLE_NOISE_GATE', 'KV4P_PROC_NOISE_GATE_THRESHOLD',
            'KV4P_PROC_NOISE_GATE_ATTACK', 'KV4P_PROC_NOISE_GATE_RELEASE',
        ]),
        ('mumble', 'Mumble Server', [
            'MUMBLE_SERVER', 'MUMBLE_PORT', 'MUMBLE_USERNAME', 'MUMBLE_PASSWORD',
            'MUMBLE_CHANNEL', 'MUMBLE_BITRATE', 'MUMBLE_VBR',
            'MUMBLE_JITTER_BUFFER', 'MUMBLE_LOOP_RATE', 'MUMBLE_STEREO',
            'MUMBLE_RECONNECT', 'MUMBLE_DEBUG',
            'MUMBLE_VAD_THRESHOLD',
        ]),
        ('mumble-server-1', 'Mumble Server 1', [
            'ENABLE_MUMBLE_SERVER_1', 'MUMBLE_SERVER_1_PORT',
            'MUMBLE_SERVER_1_PASSWORD', 'MUMBLE_SERVER_1_MAX_USERS',
            'MUMBLE_SERVER_1_MAX_BANDWIDTH', 'MUMBLE_SERVER_1_WELCOME',
            'MUMBLE_SERVER_1_REGISTER_NAME', 'MUMBLE_SERVER_1_ALLOW_HTML',
            'MUMBLE_SERVER_1_OPUS_THRESHOLD', 'MUMBLE_SERVER_1_AUTOSTART',
        ]),
        ('mumble-server-2', 'Mumble Server 2', [
            'ENABLE_MUMBLE_SERVER_2', 'MUMBLE_SERVER_2_PORT',
            'MUMBLE_SERVER_2_PASSWORD', 'MUMBLE_SERVER_2_MAX_USERS',
            'MUMBLE_SERVER_2_MAX_BANDWIDTH', 'MUMBLE_SERVER_2_WELCOME',
            'MUMBLE_SERVER_2_REGISTER_NAME', 'MUMBLE_SERVER_2_ALLOW_HTML',
            'MUMBLE_SERVER_2_OPUS_THRESHOLD', 'MUMBLE_SERVER_2_AUTOSTART',
        ]),
        ('ptt', 'PTT (Push-to-Talk)', [
            'TX_RADIO', 'PTT_METHOD', 'PTT_RELAY_DEVICE', 'PTT_RELAY_BAUD',
            'PTT_RELEASE_DELAY', 'PTT_ACTIVATION_DELAY',
            'PTT_TTS_DELAY', 'PTT_ANNOUNCEMENT_DELAY',
            'TX_TALKBACK',
        ]),
        ('radio', 'Radio Interface (AIOC)', [
            'AIOC_VID', 'AIOC_PID', 'AIOC_INPUT_DEVICE', 'AIOC_OUTPUT_DEVICE',
            'AIOC_PTT_CHANNEL',
        ]),
        ('relay', 'Relay Control', [
            'ENABLE_RELAY_RADIO', 'RELAY_RADIO_DEVICE', 'RELAY_RADIO_BAUD',
            'ENABLE_RELAY_CHARGER', 'RELAY_CHARGER_CONTROL', 'CHARGER_RELAY_GPIO',
            'RELAY_CHARGER_DEVICE', 'RELAY_CHARGER_BAUD',
            'RELAY_CHARGER_ON_TIME', 'RELAY_CHARGER_OFF_TIME',
        ]),
        ('remote', 'Remote Audio Link', [
            'REMOTE_AUDIO_ROLE', 'REMOTE_AUDIO_HOST', 'REMOTE_AUDIO_PORT',
            'REMOTE_AUDIO_RX_PORT',
            'REMOTE_AUDIO_DUCK', 'REMOTE_AUDIO_PRIORITY',
            'REMOTE_AUDIO_DISPLAY_GAIN', 'REMOTE_AUDIO_AUDIO_BOOST',
            'REMOTE_AUDIO_RECONNECT_INTERVAL',
        ]),
        ('sdr_processing', 'SDR Audio Processing', [
            'SDR_PROC_ENABLE_NOISE_GATE', 'SDR_PROC_NOISE_GATE_THRESHOLD',
            'SDR_PROC_NOISE_GATE_ATTACK', 'SDR_PROC_NOISE_GATE_RELEASE',
            'SDR_PROC_ENABLE_HPF', 'SDR_PROC_HPF_CUTOFF',
            'SDR_PROC_ENABLE_LPF', 'SDR_PROC_LPF_CUTOFF',
            'SDR_PROC_ENABLE_NOTCH', 'SDR_PROC_NOTCH_FREQ', 'SDR_PROC_NOTCH_Q',
        ]),
        ('sdr', 'SDR — RSPduo Dual Tuner', [
            'SDR_INTERNAL_AUTOSTART', 'SDR_INTERNAL_AUTOSTART_CHANNEL',
            'ENABLE_SDR', 'SDR_DEVICE_NAME', 'SDR_DUCK', 'SDR_MIX_RATIO',
            'SDR_DISPLAY_GAIN', 'SDR_AUDIO_BOOST', 'SDR_BUFFER_MULTIPLIER',
            'SDR_WATCHDOG_TIMEOUT', 'SDR_WATCHDOG_MAX_RESTARTS', 'SDR_WATCHDOG_MODPROBE',
            'SDR_MUTE_DEFAULT',
            'ENABLE_SDR2', 'SDR2_DEVICE_NAME', 'SDR2_DUCK', 'SDR2_MIX_RATIO',
            'SDR2_DISPLAY_GAIN', 'SDR2_AUDIO_BOOST', 'SDR2_BUFFER_MULTIPLIER',
            'SDR2_WATCHDOG_TIMEOUT', 'SDR2_WATCHDOG_MAX_RESTARTS', 'SDR2_WATCHDOG_MODPROBE',
            'SDR2_MUTE_DEFAULT',
            'SDR_PRIORITY_ORDER',
        ]),
        ('switching', 'Signal Detection & Switching', [
            'SIGNAL_ATTACK_TIME', 'SIGNAL_RELEASE_TIME', 'SWITCH_PADDING_TIME',
            'SDR_DUCK_COOLDOWN', 'SDR_SIGNAL_THRESHOLD', 'SDR_REBROADCAST_PTT_HOLD',
            'REDUCK_INHIBIT_TIME',
            'REPEATER_PTT_HOLD', 'SIMPLEX_TAIL_TIME', 'SIMPLEX_MAX_BUFFER',
        ]),
        ('smart', 'Smart Announcements', [
            'ENABLE_SMART_ANNOUNCE',
            'SMART_ANNOUNCE_TOP_TEXT', 'SMART_ANNOUNCE_TAIL_TEXT',
            'SMART_ANNOUNCE_START_TIME', 'SMART_ANNOUNCE_END_TIME',
            'SMART_ANNOUNCE_1_PROMPT', 'SMART_ANNOUNCE_1_INTERVAL',
            'SMART_ANNOUNCE_1_VOICE', 'SMART_ANNOUNCE_1_TARGET_SECS',
            'SMART_ANNOUNCE_1_MODE', 'SMART_ANNOUNCE_1_TOP_TEXT', 'SMART_ANNOUNCE_1_TAIL_TEXT',
            'SMART_ANNOUNCE_2_PROMPT', 'SMART_ANNOUNCE_2_INTERVAL',
            'SMART_ANNOUNCE_2_VOICE', 'SMART_ANNOUNCE_2_TARGET_SECS',
            'SMART_ANNOUNCE_2_MODE', 'SMART_ANNOUNCE_2_TOP_TEXT', 'SMART_ANNOUNCE_2_TAIL_TEXT',
            'SMART_ANNOUNCE_3_PROMPT', 'SMART_ANNOUNCE_3_INTERVAL',
            'SMART_ANNOUNCE_3_VOICE', 'SMART_ANNOUNCE_3_TARGET_SECS',
            'SMART_ANNOUNCE_3_MODE', 'SMART_ANNOUNCE_3_TOP_TEXT', 'SMART_ANNOUNCE_3_TAIL_TEXT',
        ]),
        ('speaker', 'Speaker Output', [
            'ENABLE_SPEAKER_OUTPUT', 'SPEAKER_MODE', 'SPEAKER_OUTPUT_DEVICE',
            'SPEAKER_VOLUME', 'SPEAKER_START_MUTED',
        ]),
        ('cw', 'Text to CW', [
            'CW_WPM', 'CW_FREQUENCY', 'CW_VOLUME',
        ]),
        ('tts', 'Text-to-Speech', [
            'ENABLE_TTS', 'TTS_ENGINE', 'ENABLE_TEXT_COMMANDS', 'TTS_VOLUME', 'TTS_SPEED',
            'TTS_DEFAULT_VOICE',
        ]),
        ('cat', 'TH-9800 CAT Control', [
            'ENABLE_TH9800', 'CAT_STARTUP_COMMANDS',
            'CAT_HOST', 'CAT_PORT', 'CAT_PASSWORD',
            'CAT_LEFT_CHANNEL', 'CAT_RIGHT_CHANNEL',
            'CAT_LEFT_VOLUME', 'CAT_RIGHT_VOLUME',
            'CAT_LEFT_POWER', 'CAT_RIGHT_POWER',
        ]),
        ('d75', 'TH-D75 Control', [
            'ENABLE_D75', 'D75_CONNECTION', 'D75_HOST', 'D75_PORT', 'D75_AUDIO_PORT',
            'D75_PASSWORD', 'D75_AUDIO_DUCK', 'D75_AUDIO_PRIORITY',
            'D75_AUDIO_DISPLAY_GAIN', 'D75_AUDIO_BOOST', 'D75_RECONNECT_INTERVAL',
            'D75_PROC_ENABLE_HPF', 'D75_PROC_HPF_CUTOFF',
            'D75_PROC_ENABLE_LPF', 'D75_PROC_LPF_CUTOFF',
            'D75_PROC_ENABLE_NOTCH', 'D75_PROC_NOTCH_FREQ', 'D75_PROC_NOTCH_Q',
            'D75_PROC_ENABLE_NOISE_GATE', 'D75_PROC_NOISE_GATE_THRESHOLD',
            'D75_PROC_NOISE_GATE_ATTACK', 'D75_PROC_NOISE_GATE_RELEASE',
        ]),
        ('gps', 'GPS Receiver', [
            'ENABLE_GPS', 'GPS_PORT', 'GPS_BAUD',
        ]),
        ('repeaters', 'Repeater Database', [
            'ENABLE_REPEATER_DB', 'REPEATER_RADIUS_KM',
        ]),
        ('usbip', 'USB/IP Remote Devices', [
            'ENABLE_USBIP', 'USBIP_SERVER', 'USBIP_DEVICES',
        ]),
        ('link', 'Gateway Link', [
            'ENABLE_GATEWAY_LINK', 'LINK_PORT',
            'LINK_AUDIO_DUCK', 'LINK_AUDIO_PRIORITY',
            'LINK_AUDIO_BOOST', 'LINK_AUDIO_DISPLAY_GAIN',
            'LINK_RX_MUTED', 'LINK_TX_MUTED',
        ]),
        ('vad', 'Voice Activity Detection', [
            'ENABLE_VAD', 'VAD_THRESHOLD', 'VAD_ATTACK', 'VAD_RELEASE',
            'VAD_MIN_DURATION',
        ]),
        ('vox', 'VOX', [
            'ENABLE_VOX', 'VOX_THRESHOLD', 'VOX_ATTACK_TIME', 'VOX_RELEASE_TIME',
        ]),
        ('web', 'Web Configuration', [
            'ENABLE_WEB_CONFIG', 'WEB_CONFIG_PORT', 'WEB_CONFIG_PASSWORD',
            'WEB_CONFIG_HTTPS', 'GATEWAY_NAME', 'WEB_THEME',
            'ENABLE_WEB_MIC', 'WEB_MIC_VOLUME',
            'ENABLE_WEB_MONITOR', 'WEB_MONITOR_VOLUME', 'MONITOR_VAD_THRESHOLD',
            'ENABLE_CLOUDFLARE_TUNNEL',
        ]),
        ('telegram', 'Telegram Bot', [
            'ENABLE_TELEGRAM', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
            'TELEGRAM_TMUX_SESSION',
            'TELEGRAM_STATUS_FILE', 'TELEGRAM_PROMPT_SUFFIX',
        ]),
        ('transcription', 'Transcription', [
            'ENABLE_TRANSCRIPTION', 'TRANSCRIBE_MODE',
            'TRANSCRIBE_MODEL', 'TRANSCRIBE_LANGUAGE',
            'TRANSCRIBE_VAD_THRESHOLD', 'TRANSCRIBE_VAD_HOLD',
            'TRANSCRIBE_MIN_DURATION', 'TRANSCRIBE_STREAM_INTERVAL',
            'TRANSCRIBE_MAX_BUFFER',
            'TRANSCRIBE_FORWARD_MUMBLE', 'TRANSCRIBE_FORWARD_TELEGRAM',
        ]),
        ('advanced', 'Advanced / Diagnostics', [
            'HEADLESS_MODE', 'START_CLAUDE_CODE', 'LOG_BUFFER_LINES', 'LOG_FILE_DAYS',
            'VERBOSE_LOGGING', 'STATUS_UPDATE_INTERVAL',
            'NETWORK_TIMEOUT', 'BUFFER_MANAGEMENT_VERBOSE',
        ]),
    ]

    def __init__(self, config, gateway=None):
        self.config = config
        self.gateway = gateway
        self._server = None
        self._thread = None
        self._defaults = getattr(config, '_defaults', {})
        self._stream_subscribers = []  # list of events for audio stream listeners
        self._stream_events = []      # events to notify listeners of new data
        self._stream_lock = _thr.Lock()
        self._mp3_buffer = []         # shared ring buffer of MP3 chunks
        self._mp3_seq = 0             # sequence number of next append
        self._encoder_proc = None     # shared FFmpeg process
        self._encoder_stdin = None    # stdin pipe for encoder
        self._last_audio_push = 0     # monotonic time of last real audio
        self.sdr_manager = None       # RTLAirbandManager instance
        self.usbip_manager = None     # USBIPManager instance
        # WebSocket PCM streaming (low-latency)
        self._ws_clients = []         # list of (socket, queue) tuples for WebSocket PCM clients
        self._ws_lock = _thr.Lock()

    # Color themes — all dark backgrounds with colored accents
    THEMES = {
        'blue':   {'bg': '#1a1a2e', 'panel': '#16213e', 'border': '#0f3460', 'accent': '#00d4ff',
                   'btn': '#0d1b2a', 'btn_border': '#1b3a5c', 'btn_hover': '#1a2744',
                   'btn_active_bg': '#0f3460', 'checkbox': '#00d4ff'},
        'red':    {'bg': '#1a1212', 'panel': '#2e1616', 'border': '#601010', 'accent': '#ff4444',
                   'btn': '#1e0d0d', 'btn_border': '#5c1b1b', 'btn_hover': '#3a1a1a',
                   'btn_active_bg': '#601010', 'checkbox': '#ff4444'},
        'green':  {'bg': '#121a14', 'panel': '#162e1a', 'border': '#0f6020', 'accent': '#2ecc71',
                   'btn': '#0d1e10', 'btn_border': '#1b5c2a', 'btn_hover': '#1a3a20',
                   'btn_active_bg': '#0f6020', 'checkbox': '#2ecc71'},
        'purple': {'bg': '#1a1226', 'panel': '#261638', 'border': '#3d0f60', 'accent': '#b56eff',
                   'btn': '#160d24', 'btn_border': '#3d1b5c', 'btn_hover': '#2a1a44',
                   'btn_active_bg': '#3d0f60', 'checkbox': '#b56eff'},
        'amber':  {'bg': '#1a1710', 'panel': '#2e2616', 'border': '#60480f', 'accent': '#ffb830',
                   'btn': '#1e1a0d', 'btn_border': '#5c481b', 'btn_hover': '#3a301a',
                   'btn_active_bg': '#60480f', 'checkbox': '#ffb830'},
        'teal':   {'bg': '#101a1a', 'panel': '#162e2e', 'border': '#0f6060', 'accent': '#2ed8d8',
                   'btn': '#0d1e1e', 'btn_border': '#1b5c5c', 'btn_hover': '#1a3a3a',
                   'btn_active_bg': '#0f6060', 'checkbox': '#2ed8d8'},
        'pink':   {'bg': '#1a1018', 'panel': '#2e1628', 'border': '#600f50', 'accent': '#ff69b4',
                   'btn': '#1e0d1a', 'btn_border': '#5c1b4a', 'btn_hover': '#3a1a32',
                   'btn_active_bg': '#600f50', 'checkbox': '#ff69b4'},
    }

    def _get_theme(self):
        """Return the current theme color dict."""
        name = str(getattr(self.config, 'WEB_THEME', 'blue')).lower().strip()
        return self.THEMES.get(name, self.THEMES['blue'])

    def start(self):
        """Start the HTTP server on a daemon thread."""
        import http.server
        import socketserver

        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        password = str(getattr(self.config, 'WEB_CONFIG_PASSWORD', '') or '')
        parent = self

        # SDR manager: use gateway's sdr_plugin (set after gateway init)
        self.sdr_manager = None  # will be set to gateway.sdr_plugin

        # Initialize USB/IP manager
        if getattr(self.config, 'ENABLE_USBIP', False):
            try:
                from gateway_core import USBIPManager
                self.usbip_manager = USBIPManager(self.config)
                self.usbip_manager.start()
            except Exception as e:
                print(f"  [USBIP] Manager init failed: {e}")
                self.usbip_manager = None

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

            # Static page routes — path → filename in web_pages/
            _STATIC_PAGES = {
                '/': 'shell.html',
                '/dashboard': 'dashboard.html',
                '/controls': 'controls.html',
                '/sdr': 'sdr.html',
                '/d75': 'd75.html',
                '/kv4p': 'kv4p.html',
                '/radio': 'radio.html',
                '/telegram': 'telegram.html',
                '/monitor': 'monitor.html',
                '/recordings': 'recordings.html',
                '/transcribe': 'transcribe.html',
                '/logs': 'logs.html',
                '/gps': 'gps.html',
                '/repeaters': 'repeaters.html',
                '/aircraft': 'aircraft.html',
                '/voice': 'voice.html',
                '/routing': 'routing.html',
            }

            def do_GET(self):
                if not self._check_auth():
                    return
                import json as json_mod
                import os

                # Serve static HTML pages
                if self.path in self._STATIC_PAGES:
                    _fname = self._STATIC_PAGES[self.path]
                    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_pages', _fname)
                    try:
                        with open(_p, 'rb') as _f:
                            _body = _f.read()
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write(_body)
                    except Exception:
                        self.send_response(500)
                        self.end_headers()
                    return

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
                elif self.path == '/theme':
                    # Theme config JSON — used by static HTML pages
                    t = parent._get_theme()
                    gw_name = str(getattr(parent.config, 'GATEWAY_NAME', '') or '').strip()
                    data = {**t, 'gateway_name': gw_name}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'max-age=60')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path.startswith('/pages/'):
                    # Serve static HTML files from web_pages/ directory
                    import os as _os
                    _page_name = self.path[7:]  # strip '/pages/'
                    if '..' in _page_name or '/' in _page_name:
                        self.send_response(403)
                        self.end_headers()
                        return
                    _page_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'web_pages')
                    _page_path = _os.path.join(_page_dir, _page_name)
                    if _os.path.isfile(_page_path):
                        _ct = 'text/html; charset=utf-8'
                        if _page_name.endswith('.css'):
                            _ct = 'text/css'
                        elif _page_name.endswith('.js'):
                            _ct = 'application/javascript'
                        try:
                            with open(_page_path, 'rb') as _f:
                                _body = _f.read()
                            self.send_response(200)
                            self.send_header('Content-Type', _ct)
                            self.send_header('Content-Length', str(len(_body)))
                            self.end_headers()
                            self.wfile.write(_body)
                        except BrokenPipeError:
                            pass
                    else:
                        self.send_response(404)
                        self.end_headers()

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

                elif self.path == '/monitor-apk':
                    import os
                    apk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'room-monitor.apk')
                    if os.path.exists(apk_path):
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/vnd.android.package-archive')
                        self.send_header('Content-Disposition', 'attachment; filename="room-monitor.apk"')
                        self.send_header('Content-Length', str(os.path.getsize(apk_path)))
                        self.end_headers()
                        with open(apk_path, 'rb') as f:
                            self.wfile.write(f.read())
                    else:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b'APK not built yet')
                elif self.path.startswith('/transcriptions'):
                    # Return recent transcriptions as JSON
                    since = 0
                    try:
                        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
                        for part in qs.split('&'):
                            if part.startswith('since='):
                                since = float(part[6:])
                    except Exception:
                        pass
                    data = {'results': [], 'status': {}}
                    if parent.gateway and parent.gateway.transcriber:
                        data['results'] = parent.gateway.transcriber.get_results(since=since)
                        data['status'] = parent.gateway.transcriber.get_status()
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/d75status':
                    # D75 CAT state endpoint — reads from link endpoint or legacy plugin
                    data = {'connected': False, 'd75_enabled': False, 'tcp_connected': False,
                            'serial_connected': False, 'btstart_in_progress': False,
                            'service_running': False, 'status_detail': ''}
                    if parent.gateway:
                        gw = parent.gateway
                        data['d75_enabled'] = getattr(gw.config, 'ENABLE_D75', False)
                        data['d75_mode'] = str(getattr(gw.config, 'D75_CONNECTION', 'bluetooth')).lower().strip()

                        # Check for D75 link endpoint first
                        _link = getattr(gw, 'link_server', None)
                        _d75_ep = None
                        if _link:
                            for ep_name in _link.get_endpoint_names():
                                if 'd75' in ep_name.lower():
                                    _d75_ep = ep_name
                                    break

                        if _d75_ep:
                            # D75 is a link endpoint
                            data['d75_enabled'] = True
                            data['connected'] = True
                            data['tcp_connected'] = True
                            data['serial_connected'] = True
                            data['d75_mode'] = 'link_endpoint'
                            data['service_running'] = True
                            data['status_detail'] = ''
                            data['link_endpoint'] = _d75_ep
                            # Forward all endpoint status fields into data
                            _ep_status = getattr(gw, '_link_last_status', {}).get(_d75_ep, {})
                            for _k, _v in _ep_status.items():
                                if _k not in ('band', 'plugin', 'mac', 'uptime'):
                                    data[_k] = _v
                            # Band info: convert from array to band_0/band_1 with fixups
                            _mm_names = {0: 'VFO', 1: 'Memory', 2: 'Call', 3: 'DV'}
                            _bands = _ep_status.get('band', [{}, {}])
                            for _bi, _bkey in enumerate(('band_0', 'band_1')):
                                if _bi < len(_bands) and isinstance(_bands[_bi], dict):
                                    _bd = dict(_bands[_bi])
                                    if 'memory_mode' in _bd and isinstance(_bd['memory_mode'], int):
                                        _bd['memory_mode'] = _mm_names.get(_bd['memory_mode'], '?')
                                    # Map s_meter → signal (D75 page expects 'signal')
                                    if 's_meter' in _bd and 'signal' not in _bd:
                                        _bd['signal'] = _bd['s_meter']
                                    data[_bkey] = _bd
                            # Audio level from the link audio source
                            for _src_name, _src in getattr(gw, 'link_endpoints', {}).items():
                                if 'd75' in _src_name.lower():
                                    data['audio_connected'] = True
                                    data['audio_level'] = getattr(_src, 'audio_level', 0)
                                    data['audio_boost'] = int(getattr(_src, 'audio_boost', 1.0) * 100)
                                    break

                        elif gw.d75_plugin:
                            # Legacy path: direct D75Plugin
                            cat = gw.d75_plugin
                            data.update(cat.get_radio_state())
                            data['d75_enabled'] = True
                            data['tcp_connected'] = cat._connected
                            data['serial_connected'] = getattr(cat, '_serial_connected', False) if cat._connected else False
                            data['af_gain'] = getattr(cat, '_af_gain', -1)
                            data['audio_connected'] = cat.server_connected
                            data['audio_level'] = cat.audio_level
                            data['audio_boost'] = int(cat.audio_boost * 100)

                        # Build status detail
                        if not _d75_ep and not gw.d75_plugin:
                            if not data['d75_enabled']:
                                data['status_detail'] = 'D75 disabled in config'
                            else:
                                data['status_detail'] = 'D75 not connected (no link endpoint or plugin)'
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/kv4pstatus':
                    # KV4P status JSON endpoint — served by KV4PPlugin
                    data = {'connected': False, 'kv4p_enabled': False}
                    if parent.gateway:
                        data['kv4p_enabled'] = getattr(parent.gateway.config, 'ENABLE_KV4P', False)
                        _kv4p_p = getattr(parent.gateway, 'kv4p_plugin', None)
                        if _kv4p_p:
                            data.update(_kv4p_p.get_status())
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/d75memlist':
                    # D75 memory channel list — scans channels via endpoint or legacy plugin
                    import json as json_mod
                    channels = []
                    gw = parent.gateway
                    cat = gw.d75_plugin if gw else None
                    # Check for D75 link endpoint first
                    _link = getattr(gw, 'link_server', None) if gw else None
                    _d75_ep = None
                    if _link:
                        for ep_name in _link.get_endpoint_names():
                            if 'd75' in ep_name.lower():
                                _d75_ep = ep_name
                                break
                    if _d75_ep and _link:
                        # Send memscan command and wait for ACK with results
                        _scan_result = [None]
                        _scan_evt = _thr.Event()
                        _orig_ack = getattr(gw, '_link_scan_ack', None)
                        def _scan_ack(name, ack):
                            if name == _d75_ep and ack.get('cmd') == 'memscan':
                                _scan_result[0] = ack.get('result', {})
                                _scan_evt.set()
                        # Temporarily hook the ACK callback
                        gw._link_scan_ack = _scan_ack
                        # Store original on_ack and wrap it
                        _orig_on_ack = _link._on_ack
                        def _wrapped_ack(name, ack):
                            if _orig_on_ack:
                                _orig_on_ack(name, ack)
                            if gw._link_scan_ack:
                                gw._link_scan_ack(name, ack)
                        _link._on_ack = _wrapped_ack
                        try:
                            print(f"  [D75 Scan] Sending memscan to {_d75_ep}...")
                            _link.send_command_to(_d75_ep, {'cmd': 'memscan'})
                            _scan_evt.wait(timeout=60)  # scan can take a while
                            print(f"  [D75 Scan] Got result: {len(_scan_result[0].get('channels', [])) if _scan_result[0] else 'timeout'}")
                            if _scan_result[0] and _scan_result[0].get('ok'):
                                channels = _scan_result[0].get('channels', [])
                        finally:
                            _link._on_ack = _orig_on_ack
                            gw._link_scan_ack = None
                        data = channels
                        try:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.send_header('Cache-Control', 'no-cache')
                            self.end_headers()
                            self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                        except BrokenPipeError:
                            pass
                        return
                    _modes = {0: 'FM', 1: 'AM', 2: 'LSB', 3: 'USB', 4: 'CW', 5: 'DV'}
                    _shifts = {0: 'S', 1: '+', 2: '-'}
                    _ctcss = ["67.0","69.3","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
                        "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
                        "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
                        "173.8","179.9","186.2","192.8","203.5","210.7","218.1","225.7","233.6","241.8","250.3"]
                    _dcs = ["023","025","026","031","032","036","043","047","051","053","054",
                        "065","071","072","073","074","114","115","116","122","125","131",
                        "132","134","143","145","152","155","156","162","165","172","174",
                        "205","212","223","225","226","243","244","245","246","251","252",
                        "255","261","263","265","266","271","274","306","311","315","325",
                        "331","332","343","346","351","356","364","365","371","411","412",
                        "413","423","431","432","445","446","452","454","455","462","464",
                        "465","466","503","506","516","523","526","532","546","565","606",
                        "612","624","627","631","632","654","662","664","703","712","723",
                        "731","732","734","743","754"]
                    if cat:
                        _empty_streak = 0
                        for ch_num in range(1000):
                            ch_str = str(ch_num).zfill(3)
                            resp = cat.send_command(f"!cat ME {ch_str}")
                            if not resp or ',' not in str(resp):
                                _empty_streak += 1
                                if _empty_streak >= 5:
                                    break  # Stop after 5 consecutive empty channels

                                continue
                            # Find ME data line in response
                            me_line = ''
                            for line in str(resp).split('\n'):
                                line = line.strip()
                                if line.startswith('ME ') and ',' in line:
                                    me_line = line[3:]
                                    break
                            if not me_line:
                                continue
                            fields = me_line.split(',')
                            if len(fields) < 14:
                                continue
                            try:
                                freq_hz = int(fields[1])
                                if freq_hz < 1000000:
                                    continue
                                freq = freq_hz / 1_000_000
                                field2 = int(fields[2])  # ME[2]: offset if small, TX freq if large
                                mode = int(fields[5])
                                tone_on = fields[8] == '1'
                                ctcss_on = fields[9] == '1'
                                dcs_on = fields[10] == '1'
                                shift = int(fields[13])  # 0=simplex, 1=+, 2=-
                                # Determine shift display
                                # ME field[2] < 100 MHz → offset; >= 100 MHz → TX frequency
                                if field2 >= 100_000_000:
                                    # TX frequency — derive offset and shift
                                    tx_freq = field2 / 1_000_000
                                    diff = tx_freq - freq
                                    if abs(diff) < 0.001:
                                        shift_str = 'S'
                                        offset_str = ''
                                    elif abs(diff) > 50:
                                        shift_str = 'X'
                                        offset_str = f'{tx_freq:.4f}'
                                    elif diff > 0:
                                        shift_str = '+'
                                        offset_str = f'{diff:.4f}'
                                    else:
                                        shift_str = '-'
                                        offset_str = f'{abs(diff):.4f}'
                                elif field2 > 0 and shift != 0:
                                    # Small value = offset in Hz
                                    offset_mhz = field2 / 1_000_000
                                    shift_str = '+' if shift == 1 else '-'
                                    offset_str = f'{offset_mhz:.4f}'
                                else:
                                    shift_str = 'S'
                                    offset_str = ''
                                tone_str = ''
                                # ME field[14]=lockout, [15]=tone_idx, [16]=ctcss_idx, [17]=dcs_idx
                                tone_idx = int(fields[15])
                                ctcss_idx = int(fields[16])
                                if ctcss_on:
                                    if ctcss_idx < len(_ctcss): tone_str = _ctcss[ctcss_idx]
                                elif tone_on:
                                    idx = ctcss_idx if tone_idx == 0 and ctcss_idx > 0 else tone_idx
                                    if idx < len(_ctcss): tone_str = _ctcss[idx]
                                elif dcs_on:
                                    idx = int(fields[17])
                                    if idx < len(_dcs): tone_str = 'D' + _dcs[idx]
                                name = fields[20].strip() if len(fields) > 20 else ''
                                # Power: ME field[21] on D75 if present (0=H,1=L,2=EL)
                                power = int(fields[21]) if len(fields) > 21 and fields[21].strip().isdigit() else -1
                                channels.append({
                                    'ch': ch_str, 'freq': round(freq, 4),
                                    'offset': offset_str,
                                    'mode': _modes.get(mode, '?'),
                                    'shift': shift_str,
                                    'tone': tone_str, 'name': name,
                                    # ME→FO field mapping: ME has lockout field at [14] that FO lacks
                                    # FO = band + ME[1:14] + ME[15:22] (skip ME[14]=lockout)
                                    'me_fields': ','.join(fields[1:14] + fields[15:22]) if len(fields) >= 22 else '',
                                    'power': power,
                                })
                                _empty_streak = 0
                            except (ValueError, IndexError):
                                continue
                    data = channels
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/sdrstatus':
                    # SDR status JSON endpoint — served by SDRPlugin
                    data = {}
                    _sdr_p = getattr(parent.gateway, 'sdr_plugin', None) if parent.gateway else None
                    if _sdr_p:
                        data = _sdr_p.get_status()
                    else:
                        data = {'error': 'SDR plugin not available', 'process_alive': False}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/automationstatus':
                    # Automation engine status JSON
                    data = {}
                    if parent.gateway and parent.gateway.automation_engine:
                        data = parent.gateway.automation_engine.get_status()
                    else:
                        data = {'enabled': False}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/adsbstatus':
                    # ADS-B component status and live aircraft data
                    import json as json_mod
                    data = {'enabled': False, 'dump1090': False, 'web': False, 'fr24feed': False,
                            'aircraft': 0, 'messages': 0, 'messages_rate': 0.0}
                    data['enabled'] = bool(getattr(parent.config, 'ENABLE_ADSB', False)) if parent.config else False
                    if data['enabled']:
                        # Service liveness checks
                        for _svc, _key in [('dump1090-fa', 'dump1090'), ('dump1090-fa-web', 'web'), ('fr24feed', 'fr24feed')]:
                            try:
                                _r = subprocess.run(['systemctl', 'is-active', _svc],
                                                    capture_output=True, text=True, timeout=2)
                                data[_key] = (_r.stdout.strip() == 'active')
                            except Exception:
                                data[_key] = False
                        # Live aircraft data from dump1090 JSON output
                        try:
                            import json as _jm
                            with open('/run/dump1090-fa/aircraft.json') as _af:
                                _ac = _jm.load(_af)
                            _now = _ac.get('now', 0)
                            data['aircraft'] = sum(1 for a in _ac.get('aircraft', []) if a.get('seen', 999) < 60)
                            _msgs = _ac.get('messages', 0)
                            # Compute message rate using previous sample
                            _prev = getattr(parent, '_adsb_prev_msgs', None)
                            _prev_t = getattr(parent, '_adsb_prev_t', 0)
                            import time as _time_mod
                            _now_t = _time_mod.monotonic()
                            if _prev is not None and _now_t > _prev_t:
                                _dt = _now_t - _prev_t
                                data['messages_rate'] = round((_msgs - _prev) / _dt, 1)
                            data['messages'] = _msgs
                            parent._adsb_prev_msgs = _msgs
                            parent._adsb_prev_t = _now_t
                        except Exception:
                            pass
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/telegramstatus':
                    import json as json_mod, os as _os
                    data = {'enabled': False, 'bot_running': False, 'bot_username': '',
                            'tmux_session': '', 'tmux_active': False,
                            'messages_today': 0, 'last_message_time': None,
                            'last_message_text': '', 'last_reply_time': None}
                    data['enabled'] = bool(getattr(parent.config, 'ENABLE_TELEGRAM', False)) if parent.config else False
                    # Always check bot process and token — even if disabled in config
                    try:
                        import subprocess as _sp
                        _r = _sp.run(['systemctl', 'is-active', 'telegram-bot'],
                                     capture_output=True, text=True, timeout=2)
                        data['bot_running'] = _r.stdout.strip() == 'active'
                    except Exception:
                        data['bot_running'] = False
                    _token = str(getattr(parent.config, 'TELEGRAM_BOT_TOKEN', '')) if parent.config else ''
                    data['token_set'] = bool(_token and len(_token) > 10)
                    if data['enabled']:
                        status_file = getattr(parent.config, 'TELEGRAM_STATUS_FILE', '/tmp/tg_status.json')
                        try:
                            with open(status_file) as _sf:
                                _sd = json_mod.load(_sf)
                            data.update({k: _sd[k] for k in data if k in _sd and k != 'bot_running'})
                        except Exception:
                            pass
                    # Always check tmux session
                    session = getattr(parent.config, 'TELEGRAM_TMUX_SESSION', 'claude-gateway') if parent.config else 'claude-gateway'
                    data['tmux_session'] = session
                    try:
                        _r = _sp.run(['tmux', 'has-session', '-t', session],
                                     capture_output=True, timeout=2)
                        data['tmux_active'] = (_r.returncode == 0)
                    except Exception:
                        data['tmux_active'] = False
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/usbipstatus':
                    import json as json_mod
                    if parent.usbip_manager:
                        data = parent.usbip_manager.get_status()
                    else:
                        data = {'enabled': bool(getattr(parent.config, 'ENABLE_USBIP', False)),
                                'server': str(getattr(parent.config, 'USBIP_SERVER', '')),
                                'server_reachable': False, 'devices': [],
                                'last_error': '', 'last_check': None}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/gpsstatus':
                    import json as json_mod
                    gw = parent.gateway
                    if gw and gw.gps_manager:
                        data = gw.gps_manager.get_status()
                    else:
                        data = {'enabled': bool(getattr(parent.config, 'ENABLE_GPS', False)),
                                'connected': False, 'fix': 0, 'satellites': []}
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path.startswith('/repeaterstatus'):
                    import json as json_mod
                    from urllib.parse import urlparse, parse_qs
                    gw = parent.gateway
                    if gw and gw.repeater_manager:
                        qs = parse_qs(urlparse(self.path).query)
                        band = qs.get('band', [''])[0]
                        radius = float(qs.get('radius', [0])[0] or 0) or None
                        operational = qs.get('operational', ['true'])[0].lower() != 'false'
                        reps = gw.repeater_manager.get_nearby(
                            band=band or None, radius_km=radius, operational_only=operational)
                        status = gw.repeater_manager.get_status()
                        data = {'ok': True, 'status': status, 'repeaters': reps}
                    else:
                        data = {'ok': True, 'status': {'enabled': False}, 'repeaters': []}
                    try:
                        resp = json_mod.dumps(data).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(resp)
                    except BrokenPipeError:
                        pass
                elif self.path == '/automationhistory':
                    # Automation task history JSON
                    data = []
                    if parent.gateway and parent.gateway.automation_engine:
                        data = parent.gateway.automation_engine.get_history()
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

                    _send_thread = _thr.Thread(target=_ws_sender, args=(_sock, _send_q), daemon=True)
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
                    # PTT is handled by the bus system — WebMicSource has ptt_control=True,
                    # so any SoloBus with webmic as a TX source will auto-key its radio.
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
                        # PTT release handled by bus system — SoloBus releases PTT
                        # after ptt_release_delay when WebMicSource stops producing audio.
                        print(f"[WS-Mic] Disconnected {_client_ip}")
                    return
                elif self.path == '/ws_monitor':
                    # WebSocket endpoint for room monitor — audio into mixer, NO PTT
                    self._upgrading_ws = True
                    import hashlib, base64
                    ws_key = self.headers.get('Sec-WebSocket-Key', '')
                    if not ws_key or self.headers.get('Upgrade', '').lower() != 'websocket':
                        self._upgrading_ws = False
                        self.send_response(400)
                        self.end_headers()
                        return
                    _mon_src = parent.gateway.web_monitor_source if parent.gateway else None
                    if not _mon_src:
                        self._upgrading_ws = False
                        self.send_response(503)
                        self.end_headers()
                        return
                    if _mon_src.client_connected:
                        self._upgrading_ws = False
                        self.send_response(409)
                        self.end_headers()
                        return
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
                    _mon_src.client_connected = True
                    print(f"\n[WS-Monitor] Room monitor connected from {_client_ip}")
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
                                    _mon_src.push_audio(payload)
                            except socket.timeout:
                                continue
                            except (ConnectionResetError, BrokenPipeError, OSError):
                                break
                    finally:
                        _mon_src.client_connected = False
                        _mon_src._sub_buffer = b''
                        print(f"[WS-Monitor] Disconnected {_client_ip}")
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

                elif self.path == '/recordingslist':
                    # JSON list of recording files
                    import json as json_mod
                    rec_dir = ''
                    if parent.gateway and parent.gateway.automation_engine:
                        rec_dir = parent.gateway.automation_engine.recorder._dir
                    files = []
                    if rec_dir and os.path.isdir(rec_dir):
                        for fname in sorted(os.listdir(rec_dir), reverse=True):
                            fpath = os.path.join(rec_dir, fname)
                            if not os.path.isfile(fpath):
                                continue
                            stat = os.stat(fpath)
                            # Parse metadata from filename: RADIO_FREQ_DATE_TIME_LABEL.ext
                            parts = fname.rsplit('.', 1)
                            ext = parts[1] if len(parts) > 1 else ''
                            name_parts = parts[0].split('_')
                            radio = name_parts[0] if name_parts else ''
                            freq = name_parts[1].replace('MHz', '') if len(name_parts) > 1 else ''
                            # Date is YYYY-MM-DD format
                            date_str = name_parts[2] if len(name_parts) > 2 else ''
                            time_str = name_parts[3] if len(name_parts) > 3 else ''
                            label = '_'.join(name_parts[4:]) if len(name_parts) > 4 else ''
                            files.append({
                                'name': fname,
                                'size': stat.st_size,
                                'radio': radio,
                                'freq': freq,
                                'date': date_str,
                                'time': time_str,
                                'label': label,
                                'ext': ext,
                            })
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(files).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path.startswith('/recordingsdownload'):
                    # Download a recording file
                    import urllib.parse
                    qs = urllib.parse.urlparse(self.path).query
                    params = urllib.parse.parse_qs(qs)
                    fname = params.get('file', [''])[0]
                    rec_dir = ''
                    if parent.gateway and parent.gateway.automation_engine:
                        rec_dir = parent.gateway.automation_engine.recorder._dir
                    if not fname or not rec_dir:
                        self.send_response(400)
                        self.end_headers()
                        return
                    # Sanitize filename — no path traversal
                    fname = os.path.basename(fname)
                    fpath = os.path.join(rec_dir, fname)
                    if not os.path.isfile(fpath):
                        self.send_response(404)
                        self.end_headers()
                        return
                    try:
                        ext = fname.rsplit('.', 1)[-1].lower()
                        ctype = {'mp3': 'audio/mpeg', 'wav': 'audio/wav'}.get(ext, 'application/octet-stream')
                        self.send_response(200)
                        self.send_header('Content-Type', ctype)
                        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
                        self.send_header('Content-Length', str(os.path.getsize(fpath)))
                        self.end_headers()
                        with open(fpath, 'rb') as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                    except BrokenPipeError:
                        pass

                elif self.path == '/adsb' or self.path.startswith('/adsb/'):
                    # Reverse proxy to dump1090-fa web interface
                    import urllib.request as _ureq
                    import urllib.error as _uerr
                    _adsb_port = int(getattr(parent.config, 'ADSB_PORT', 30080)) if parent.config else 30080
                    # Strip /adsb prefix — /adsb → /, /adsb/foo → /foo
                    _proxy_path = self.path[5:] or '/'
                    _target = f'http://127.0.0.1:{_adsb_port}{_proxy_path}'
                    try:
                        _req = _ureq.Request(_target)
                        # Forward useful request headers
                        for _h in ('Accept', 'Accept-Language', 'If-Modified-Since', 'If-None-Match', 'Accept-Encoding'):
                            _v = self.headers.get(_h)
                            if _v:
                                _req.add_header(_h, _v)
                        with _ureq.urlopen(_req, timeout=10) as _resp:
                            _body = _resp.read()
                            _ctype = _resp.headers.get('Content-Type', 'application/octet-stream')
                            _etag = _resp.headers.get('ETag', '')
                            _lmod = _resp.headers.get('Last-Modified', '')
                            self.send_response(200)
                            self.send_header('Content-Type', _ctype)
                            self.send_header('Content-Length', str(len(_body)))
                            if _etag:
                                self.send_header('ETag', _etag)
                            if _lmod:
                                self.send_header('Last-Modified', _lmod)
                            self.end_headers()
                            self.wfile.write(_body)
                    except _uerr.HTTPError as _e:
                        try:
                            self.send_response(_e.code)
                            self.end_headers()
                        except BrokenPipeError:
                            pass
                    except Exception:
                        _adsb_err = (
                            f'<html><head><meta charset="utf-8"></head><body style="background:#1a1a1a;color:#e0e0e0;'
                            f'font-family:-apple-system,sans-serif;text-align:center;padding-top:80px">'
                            f'<h2 style="color:#e74c3c">ADS-B Unavailable</h2>'
                            f'<p style="margin-top:12px">dump1090-fa is not running on port {_adsb_port}</p>'
                            f'<p style="margin-top:8px;color:#888">Start it with:</p>'
                            f'<code style="display:block;margin-top:8px;color:#2ecc71">sudo systemctl start dump1090-fa</code>'
                            f'</body></html>'
                        ).encode('utf-8')
                        try:
                            self.send_response(503)
                            self.send_header('Content-Type', 'text/html; charset=utf-8')
                            self.send_header('Content-Length', str(len(_adsb_err)))
                            self.end_headers()
                            self.wfile.write(_adsb_err)
                        except BrokenPipeError:
                            pass
                elif self.path == '/config':
                    # Config editor
                    html = parent._generate_html()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))



                elif self.path == '/routing/status':
                    # Return current routing state for the UI
                    data = parent._get_routing_status()
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/routing/levels':
                    # Return RX and TX audio levels separately
                    data = {}
                    gw = parent.gateway
                    if gw:
                        # RX levels (sources)
                        if gw.sdr_plugin:
                            data['sdr'] = gw.sdr_plugin.audio_level
                        if gw.kv4p_plugin:
                            data['kv4p'] = gw.kv4p_plugin.audio_level
                        if gw.d75_plugin:
                            data['d75'] = getattr(gw.d75_plugin, 'audio_level', 0)
                        if not data.get('d75'):
                            for _ln, _ls in gw.link_endpoints.items():
                                if 'd75' in _ln.lower():
                                    data['d75'] = getattr(_ls, 'audio_level', 0)
                                    break
                        if getattr(gw, 'th9800_plugin', None):
                            data['aioc'] = gw.th9800_plugin.audio_level
                        if getattr(gw, 'playback_source', None):
                            data['playback'] = getattr(gw.playback_source, 'audio_level', 0)
                        if getattr(gw, 'announce_input_source', None):
                            data['announce'] = getattr(gw.announce_input_source, 'audio_level', 0)
                        if getattr(gw, 'web_mic_source', None):
                            data['webmic'] = gw.web_mic_source.audio_level if gw.web_mic_source.client_connected else 0
                        if getattr(gw, 'web_monitor_source', None):
                            data['monitor'] = gw.web_monitor_source.audio_level
                        if getattr(gw, 'mumble_source', None):
                            data['mumble_rx'] = gw.mumble_source.audio_level
                        else:
                            data['mumble_rx'] = getattr(gw, 'rx_audio_level', 0)
                        if getattr(gw, 'remote_audio_source', None):
                            data['remote_audio'] = gw.remote_audio_source.audio_level
                        # TX levels (radio destinations)
                        if gw.kv4p_plugin:
                            data['kv4p_tx'] = getattr(gw.kv4p_plugin, 'tx_audio_level', 0)
                        if gw.d75_plugin:
                            data['d75_tx'] = getattr(gw.d75_plugin, 'tx_audio_level', 0)
                        elif not data.get('d75_tx'):
                            for _ln, _ls in gw.link_endpoints.items():
                                if 'd75' in _ln.lower():
                                    data['d75_tx'] = getattr(_ls, 'tx_audio_level', 0)
                                    break
                        if getattr(gw, 'th9800_plugin', None):
                            data['aioc_tx'] = getattr(gw.th9800_plugin, 'tx_audio_level', 0)
                        # Passive sinks — only show level if connected to a bus
                        _all_sinks = getattr(gw, '_bus_sinks', {})
                        _all_connected = set()
                        for _sinks in _all_sinks.values():
                            _all_connected.update(_sinks)
                        # Decay all sink/source levels on each poll (200ms interval)
                        gw.speaker_audio_level = max(0, int(getattr(gw, 'speaker_audio_level', 0) * 0.8))
                        gw.stream_audio_level = max(0, int(getattr(gw, 'stream_audio_level', 0) * 0.8))
                        gw.mumble_tx_level = max(0, int(getattr(gw, 'mumble_tx_level', 0) * 0.8))
                        if getattr(gw, 'mumble_source', None):
                            gw.mumble_source.audio_level = max(0, int(gw.mumble_source.audio_level * 0.8))
                        if gw.kv4p_plugin:
                            gw.kv4p_plugin.tx_audio_level = max(0, int(getattr(gw.kv4p_plugin, 'tx_audio_level', 0) * 0.8))
                        if gw.d75_plugin:
                            gw.d75_plugin.tx_audio_level = max(0, int(getattr(gw.d75_plugin, 'tx_audio_level', 0) * 0.8))
                        if getattr(gw, 'th9800_plugin', None):
                            gw.th9800_plugin.tx_audio_level = max(0, int(getattr(gw.th9800_plugin, 'tx_audio_level', 0) * 0.8))
                        # Report sink levels — 0 when disconnected so bars clear
                        data['speaker'] = gw.speaker_audio_level if 'speaker' in _all_connected else 0
                        data['broadcastify'] = gw.stream_audio_level if 'broadcastify' in _all_connected else 0
                        data['mumble'] = gw.mumble_tx_level if 'mumble' in _all_connected else 0
                        data['recording'] = 0
                        data['transcription'] = getattr(gw, 'transcription_audio_level', 0) if 'transcription' in _all_connected else 0
                        gw.transcription_audio_level = max(0, int(getattr(gw, 'transcription_audio_level', 0) * 0.8))
                        gw.remote_audio_tx_level = max(0, int(getattr(gw, 'remote_audio_tx_level', 0) * 0.8))
                        data['remote_audio_tx'] = getattr(gw, 'remote_audio_tx_level', 0) if 'remote_audio_tx' in _all_connected else 0
                        # Bus output levels
                        _bm = getattr(gw, 'bus_manager', None)
                        if _bm:
                            for _bid, _blv in _bm._bus_levels.items():
                                data['bus_' + _bid] = _blv
                        # Primary listen bus level from mixer
                        if gw.mixer:
                            _listen_id = getattr(gw, '_listen_bus_id', 'listen')
                            _mix_audio = getattr(gw, '_last_mixer_level', 0)
                            data['bus_' + _listen_id] = _mix_audio
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass

                elif self.path == '/voice/status':
                    _vr_target = os.environ.get('TMUX_TARGET', 'claude-voice')
                    result = subprocess.run(
                        ['tmux', 'has-session', '-t', _vr_target],
                        capture_output=True,
                    )
                    alive = result.returncode == 0
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps({'tmux_target': _vr_target, 'session_alive': alive}).encode())

                elif self.path == '/voice/view':
                    tmux_target = os.environ.get('TMUX_TARGET', 'claude-voice')
                    result = subprocess.run(
                        ['tmux', 'capture-pane', '-t', tmux_target, '-p'],
                        capture_output=True, text=True,
                    )
                    if result.returncode != 0:
                        self.send_response(503)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps({'error': f"tmux session '{tmux_target}' not found"}).encode())
                    else:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps({'content': result.stdout}).encode())

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
                elif self.path == '/transcribe_config':
                    # Transcriber runtime config
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        key = data.get('key', '')
                        value = data.get('value', '')
                        tx = parent.gateway.transcriber if parent.gateway else None
                        if not tx:
                            result = {'ok': False, 'error': 'transcriber not running'}
                        elif key == 'enabled':
                            tx._enabled = bool(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'vad_threshold':
                            tx._vad_threshold = float(value)
                            if hasattr(tx, '_silence_threshold'):
                                tx._silence_threshold = float(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'vad_hold':
                            tx._vad_hold_time = float(value)
                            if hasattr(tx, '_silence_duration'):
                                tx._silence_duration = float(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'min_duration':
                            tx._min_duration = float(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'language':
                            tx._language = str(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'forward_mumble':
                            tx._forward_mumble = bool(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'forward_telegram':
                            tx._forward_telegram = bool(value)
                            tx._save(); result = {'ok': True}
                        elif key == 'audio_boost':
                            tx._audio_boost = float(value) / 100.0
                            tx._save(); result = {'ok': True}
                        elif key == 'clear':
                            with tx._results_lock:
                                tx._results.clear()
                            result = {'ok': True}
                        elif key == 'model':
                            tx._model_size = str(value)
                            tx._save()
                            result = {'ok': True, 'note': 'model change takes effect on restart'}
                        elif key == 'mode':
                            if parent.gateway:
                                parent.gateway.config.TRANSCRIBE_MODE = str(value)
                                # Save mode to settings file so restart picks it up
                                from transcriber import _load_saved_settings, _save_settings
                                _s = _load_saved_settings()
                                _s['mode'] = str(value)
                                _save_settings(_s)
                            result = {'ok': True, 'note': 'mode change takes effect on restart'}
                        elif key == 'restart':
                            # Restart transcriber with current settings
                            gw = parent.gateway
                            if gw:
                                if gw.transcriber:
                                    gw.transcriber.stop()
                                from transcriber import _load_saved_settings
                                _saved = _load_saved_settings()
                                _mode = _saved.get('mode', str(getattr(gw.config, 'TRANSCRIBE_MODE', 'chunked'))).lower()
                                try:
                                    if _mode == 'streaming':
                                        from transcriber import StreamingTranscriber
                                        gw.transcriber = StreamingTranscriber(gw.config, gw)
                                    else:
                                        from transcriber import RadioTranscriber
                                        gw.transcriber = RadioTranscriber(gw.config, gw)
                                    gw.transcriber.start()
                                    result = {'ok': True, 'mode': _mode}
                                except Exception as _re:
                                    result = {'ok': False, 'error': str(_re)}
                            else:
                                result = {'ok': False, 'error': 'gateway not ready'}
                        else:
                            result = {'ok': False, 'error': f'unknown key: {key}'}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    return
                elif self.path == '/testloop':
                    # Toggle test loop playback
                    result = {'ok': False, 'error': 'playback not available'}
                    if parent.gateway and parent.gateway.playback_source:
                        result = parent.gateway.playback_source.toggle_test_loop()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    return
                elif self.path == '/mixer':
                    # Mixer control endpoint — explicit mute/unmute/volume/duck/boost/toggles
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'no gateway'}
                    try:
                        data = json_mod.loads(body)
                        action = data.get('action', '')
                        source = data.get('source', '')
                        gw = parent.gateway
                        if not gw:
                            pass
                        elif action == 'status':
                            # Return full mixer state
                            s = gw.get_status_dict()
                            result = {'ok': True, 'mutes': {
                                'tx': s.get('tx_muted', False),
                                'rx': s.get('rx_muted', False),
                                'sdr1': s.get('sdr1_muted', False),
                                'sdr2': s.get('sdr2_muted', False),
                                'd75': s.get('d75_muted', False),
                                'kv4p': s.get('kv4p_muted', False),
                                'remote': s.get('remote_muted', False),
                                'announce': s.get('announce_muted', False),
                                'speaker': s.get('speaker_muted', False),
                            }, 'levels': {
                                'radio_rx': s.get('radio_rx', 0),
                                'radio_tx': s.get('radio_tx', 0),
                                'sdr1': s.get('sdr1_level', 0),
                                'sdr2': s.get('sdr2_level', 0),
                                'd75': s.get('d75_level', 0),
                                'kv4p': s.get('kv4p_level', 0),
                                'remote': s.get('remote_level', 0),
                                'announce': s.get('an_level', 0),
                                'speaker': s.get('speaker_level', 0),
                            }, 'volume': s.get('volume', 1.0),
                            'duck': {
                                'sdr1': s.get('sdr1_duck', False),
                                'sdr2': gw.sdr_plugin.duck if gw.sdr_plugin else False,
                                'd75': gw.d75_plugin.duck if gw.d75_plugin and hasattr(gw.d75_plugin, 'duck') else False,
                                'kv4p': gw.kv4p_plugin.duck if gw.kv4p_plugin and hasattr(gw.kv4p_plugin, 'duck') else False,
                                'remote': gw.remote_audio_source.duck if gw.remote_audio_source and hasattr(gw.remote_audio_source, 'duck') else False,
                            }, 'ducked': {
                                'sdr1': s.get('sdr1_ducked', False),
                                'sdr2': s.get('sdr2_ducked', False),
                                'remote': s.get('cl_ducked', False),
                            }, 'flags': {
                                'vad': s.get('vad_enabled', False),
                                'agc': getattr(gw.config, 'ENABLE_AGC', False),
                                'echo_cancel': getattr(gw.config, 'ENABLE_ECHO_CANCELLATION', False),
                                'rebroadcast': s.get('sdr_rebroadcast', False),
                                'talkback': getattr(gw, 'tx_talkback', False),
                                'manual_ptt': s.get('manual_ptt', False),
                            }, 'boost': {
                                'd75': int(gw.d75_plugin.audio_boost * 100) if gw.d75_plugin and hasattr(gw.d75_plugin, 'audio_boost') else 100,
                                'kv4p': int(gw.kv4p_plugin.audio_boost * 100) if gw.kv4p_plugin and hasattr(gw.kv4p_plugin, 'audio_boost') else 100,
                                'remote': int(gw.remote_audio_source.audio_boost * 100) if gw.remote_audio_source and hasattr(gw.remote_audio_source, 'audio_boost') else 100,
                            }, 'processing': {
                                'radio': gw.radio_processor.get_active_list() if hasattr(gw, 'radio_processor') else [],
                                'sdr': gw.sdr_processor.get_active_list() if hasattr(gw, 'sdr_processor') else [],
                                'd75': gw.d75_processor.get_active_list() if hasattr(gw, 'd75_processor') else [],
                                'kv4p': gw.kv4p_processor.get_active_list() if hasattr(gw, 'kv4p_processor') else [],
                            }}

                        elif action in ('mute', 'unmute', 'toggle'):
                            # Mute control for a specific source
                            _mute_map = {
                                'tx':       ('tx_muted', None),
                                'rx':       ('rx_muted', None),
                                'sdr1':     ('sdr_muted', 'sdr_plugin'),
                                'sdr2':     ('sdr2_muted', 'sdr_plugin'),
                                'd75':      ('d75_muted', 'd75_plugin'),
                                'kv4p':     ('kv4p_muted', 'kv4p_plugin'),
                                'remote':   ('remote_audio_muted', 'remote_audio_source'),
                                'announce': ('announce_input_muted', 'announce_input_source'),
                                'speaker':  ('speaker_muted', None),
                            }
                            if source == 'global':
                                current = gw.tx_muted and gw.rx_muted
                                if action == 'toggle':
                                    want = not current
                                elif action == 'mute':
                                    want = True
                                else:
                                    want = False
                                gw.tx_muted = want
                                gw.rx_muted = want
                                result = {'ok': True, 'muted': want}
                            elif source in _mute_map:
                                attr, src_attr = _mute_map[source]
                                current = getattr(gw, attr, False)
                                if action == 'toggle':
                                    want = not current
                                elif action == 'mute':
                                    want = True
                                else:
                                    want = False
                                setattr(gw, attr, want)
                                # Sync to source object if it has .muted
                                if src_attr:
                                    src_obj = getattr(gw, src_attr, None)
                                    if src_obj:
                                        src_obj.muted = want
                                result = {'ok': True, 'source': source, 'muted': want}
                            elif source.startswith('link_rx:') or source.startswith('link_tx:'):
                                parts = source.split(':', 1)
                                direction = parts[0]  # 'link_rx' or 'link_tx'
                                ep_name = parts[1] if len(parts) > 1 else ''
                                if not ep_name:
                                    result = {'ok': False, 'error': 'missing endpoint name'}
                                else:
                                    settings = gw.link_endpoint_settings.setdefault(ep_name, {})
                                    mute_key = 'rx_muted' if direction == 'link_rx' else 'tx_muted'
                                    current = settings.get(mute_key, False)
                                    want = not current if action == 'toggle' else (action == 'mute')
                                    settings[mute_key] = want
                                    if direction == 'link_rx':
                                        src = gw.link_endpoints.get(ep_name)
                                        if src:
                                            src.muted = want
                                    gw._save_link_settings()
                                    result = {'ok': True, 'muted': want}
                            else:
                                result = {'ok': False, 'error': f'unknown source: {source}'}

                        elif action == 'volume':
                            # Set absolute INPUT_VOLUME
                            val = data.get('value')
                            if val is not None:
                                gw.config.INPUT_VOLUME = max(0.1, min(3.0, float(val)))
                                result = {'ok': True, 'volume': round(gw.config.INPUT_VOLUME, 2)}
                            else:
                                result = {'ok': True, 'volume': round(gw.config.INPUT_VOLUME, 2)}

                        elif action == 'duck':
                            # Enable/disable duck on a source
                            state = data.get('state')  # true/false or omit for toggle
                            _duck_map = {
                                'sdr1': 'sdr_plugin', 'sdr2': 'sdr_plugin',
                                'd75': 'd75_plugin', 'kv4p': 'kv4p_plugin',
                                'remote': 'remote_audio_source',
                            }
                            if source in _duck_map:
                                src_obj = getattr(gw, _duck_map[source], None)
                                if src_obj and hasattr(src_obj, 'duck'):
                                    if state is None:
                                        src_obj.duck = not src_obj.duck
                                    else:
                                        src_obj.duck = bool(state)
                                    result = {'ok': True, 'source': source, 'duck': src_obj.duck}
                                else:
                                    result = {'ok': False, 'error': f'{source} not available'}
                            else:
                                result = {'ok': False, 'error': f'duck not supported for: {source}'}

                        elif action == 'boost':
                            # Set per-source audio boost (percentage 0-500)
                            pct = data.get('value', 100)
                            _boost_map = {
                                'd75': 'd75_plugin',
                                'kv4p': 'kv4p_plugin',
                                'remote': 'remote_audio_source',
                            }
                            if source in _boost_map:
                                src_obj = getattr(gw, _boost_map[source], None)
                                if src_obj and hasattr(src_obj, 'audio_boost'):
                                    src_obj.audio_boost = max(0, min(5.0, float(pct) / 100.0))
                                    result = {'ok': True, 'source': source, 'boost_pct': int(src_obj.audio_boost * 100)}
                                else:
                                    result = {'ok': False, 'error': f'{source} not available'}
                            else:
                                result = {'ok': False, 'error': f'boost not supported for: {source}'}

                        elif action == 'flag':
                            # Toggle or set a mixer flag (vad, agc, echo_cancel, rebroadcast)
                            flag = data.get('flag', '')
                            state = data.get('state')  # true/false or omit for toggle
                            if flag == 'vad':
                                if state is None:
                                    gw.config.ENABLE_VAD = not gw.config.ENABLE_VAD
                                else:
                                    gw.config.ENABLE_VAD = bool(state)
                                result = {'ok': True, 'flag': 'vad', 'enabled': gw.config.ENABLE_VAD}
                            elif flag == 'agc':
                                if state is None:
                                    gw.config.ENABLE_AGC = not gw.config.ENABLE_AGC
                                else:
                                    gw.config.ENABLE_AGC = bool(state)
                                result = {'ok': True, 'flag': 'agc', 'enabled': gw.config.ENABLE_AGC}
                            elif flag == 'echo_cancel':
                                if state is None:
                                    gw.config.ENABLE_ECHO_CANCELLATION = not gw.config.ENABLE_ECHO_CANCELLATION
                                else:
                                    gw.config.ENABLE_ECHO_CANCELLATION = bool(state)
                                result = {'ok': True, 'flag': 'echo_cancel', 'enabled': gw.config.ENABLE_ECHO_CANCELLATION}
                            elif flag == 'rebroadcast':
                                if state is None:
                                    new_state = not gw.sdr_rebroadcast
                                else:
                                    new_state = bool(state)
                                gw.sdr_rebroadcast = new_state
                                if not new_state:
                                    # Clean up PTT if disabling rebroadcast
                                    if getattr(gw, '_rebroadcast_ptt_active', False):
                                        gw._rebroadcast_ptt_active = False
                                    if gw.radio_source:
                                        gw.radio_source.enabled = True
                                result = {'ok': True, 'flag': 'rebroadcast', 'enabled': gw.sdr_rebroadcast}
                            elif flag == 'talkback':
                                if state is None:
                                    gw.tx_talkback = not gw.tx_talkback
                                else:
                                    gw.tx_talkback = bool(state)
                                result = {'ok': True, 'flag': 'talkback', 'enabled': gw.tx_talkback}
                            else:
                                result = {'ok': False, 'error': f'unknown flag: {flag}'}

                        elif action == 'processing':
                            # Toggle or set audio processing filter
                            # source: radio, sdr, d75, kv4p
                            # filter: gate, hpf, lpf, notch
                            filt = data.get('filter', '')
                            proc_state = data.get('state')  # true/false or omit for toggle
                            valid_sources = ('radio', 'sdr', 'd75', 'kv4p')
                            valid_filters = ('gate', 'hpf', 'lpf', 'notch')
                            if source not in valid_sources:
                                result = {'ok': False, 'error': f'source must be one of: {", ".join(valid_sources)}'}
                            elif filt not in valid_filters:
                                result = {'ok': False, 'error': f'filter must be one of: {", ".join(valid_filters)}'}
                            else:
                                gw.handle_proc_toggle(source, filt, state=proc_state)
                                # Read back the current state
                                _proc_map = {
                                    'radio': gw.radio_processor,
                                    'sdr': gw.sdr_processor,
                                    'd75': gw.d75_processor,
                                    'kv4p': gw.kv4p_processor,
                                }
                                proc_obj = _proc_map.get(source)
                                active = proc_obj.get_active_list() if proc_obj else []
                                result = {'ok': True, 'source': source, 'active': active}

                        else:
                            result = {'ok': False, 'error': f'unknown action: {action}'}

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
                elif self.path == '/aitext':
                    # Text-to-AI announcement endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    ok = False
                    error = None
                    try:
                        data = json_mod.loads(body)
                        prompt = data.get('text', '').strip()
                        target_secs = int(data.get('target_secs', 30))
                        voice = int(data.get('voice', 1))
                        top_text = data.get('top_text', 'QST').strip()
                        tail_text = data.get('tail_text', 'Callsign').strip()
                        if not prompt:
                            error = 'no text provided'
                        elif not parent.gateway:
                            error = 'gateway not ready'
                        elif not parent.gateway.smart_announce:
                            error = 'smart announce not available'
                        else:
                            sa = parent.gateway.smart_announce
                            # Build a synthetic entry for ad-hoc prompt
                            entry = {
                                'id': 0,
                                'prompt': prompt,
                                'voice': voice,
                                'target_secs': min(max(target_secs, 5), 120),
                                'interval': 0,
                                'mode': 'manual',
                                'top_text': top_text,
                                'tail_text': tail_text,
                            }
                            _thr.Thread(target=sa._run_announcement, args=(entry, True),
                                        daemon=True, name="WebAIText").start()
                            ok = True
                    except Exception as e:
                        error = str(e)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
                    self.wfile.write(resp.encode())
                    return
                elif self.path == '/cw':
                    # CW (Morse code) endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    ok = False
                    error = None
                    try:
                        data = json_mod.loads(body)
                        text = data.get('text', '').strip()
                        if not text:
                            error = 'no text provided'
                        elif not parent.gateway:
                            error = 'gateway not ready'
                        elif not parent.gateway.playback_source:
                            error = 'playback not available'
                        else:
                            gw = parent.gateway
                            _wpm  = int(data.get('wpm',  gw.config.CW_WPM))
                            _freq = int(data.get('freq', gw.config.CW_FREQUENCY))
                            _vol  = float(data.get('vol', gw.config.CW_VOLUME))
                            def _do_cw():
                                pcm = generate_cw_pcm(text, _wpm, _freq, 48000)
                                if _vol != 1.0:
                                    import numpy as _np
                                    pcm = _np.clip(pcm.astype(_np.float32) * _vol,
                                                   -32768, 32767).astype(_np.int16)
                                import wave as _wave, tempfile as _tmp
                                tf = _tmp.NamedTemporaryFile(suffix='.wav', delete=False, prefix='cw_')
                                tf.close()
                                with _wave.open(tf.name, 'wb') as wf:
                                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                                    wf.writeframes(pcm.tobytes())
                                if not gw.playback_source.queue_file(tf.name):
                                    import os as _os
                                    _os.unlink(tf.name)
                            _thr.Thread(target=_do_cw, daemon=True, name="WebCW").start()
                            ok = True
                    except Exception as e:
                        error = str(e)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
                    self.wfile.write(resp.encode())
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
                            def _do_tts():
                                parent.gateway.speak_text(text, voice=voice)
                            _thr.Thread(target=_do_tts, daemon=True, name="WebTTS").start()
                            ok = True
                    except Exception as e:
                        error = str(e)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
                    self.wfile.write(resp.encode())
                    return
                elif self.path == '/automationcmd':
                    # Automation engine command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        engine = parent.gateway.automation_engine if parent.gateway else None
                        if not engine:
                            result = {'ok': False, 'error': 'Automation not enabled'}
                        elif cmd == 'trigger':
                            task_name = data.get('task', '')
                            if engine.trigger(task_name):
                                result = {'ok': True, 'triggered': task_name}
                            else:
                                result = {'ok': False, 'error': f'Task not found: {task_name}'}
                        elif cmd == 'reload':
                            engine.reload_scheme()
                            result = {'ok': True, 'tasks': len(engine._tasks)}
                        elif cmd == 'stop_recording':
                            path = engine.recorder.stop()
                            result = {'ok': True, 'path': path}
                        else:
                            result = {'ok': False, 'error': f'Unknown command: {cmd}'}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode())
                    return
                elif self.path == '/proc_toggle':
                    # Per-source audio processing toggle endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    try:
                        data = json_mod.loads(body)
                        source = data.get('source', '')  # "radio" or "sdr"
                        filt = data.get('filter', '')    # "gate", "hpf", "lpf", "notch"
                        if source and filt and parent.gateway:
                            parent.gateway.handle_proc_toggle(source, filt)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                elif self.path == '/d75cmd':
                    # D75 CAT command endpoint — routes through link endpoint or legacy plugin
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        args = data.get('args', '')
                        gw = parent.gateway

                        # Check for D75 link endpoint first (new path)
                        _link = getattr(gw, 'link_server', None) if gw else None
                        _d75_ep = None
                        if _link:
                            for ep_name in _link.get_endpoint_names():
                                if 'd75' in ep_name.lower():
                                    _d75_ep = ep_name
                                    break

                        if _d75_ep and _link:
                            # Route through Gateway Link endpoint
                            if cmd == 'cat':
                                _link.send_command_to(_d75_ep, {'cmd': 'cat', 'raw': args})
                                result = {'ok': True, 'response': f'sent via link endpoint'}
                            elif cmd == 'ptt':
                                # Toggle: check current state from cached endpoint status
                                _ptt_now = getattr(gw, '_link_ptt_active', {}).get(_d75_ep, False)
                                _link.send_command_to(_d75_ep, {'cmd': 'ptt', 'state': not _ptt_now})
                                result = {'ok': True, 'response': f'PTT {"off" if _ptt_now else "on"} via link'}
                            elif cmd == 'freq':
                                _link.send_command_to(_d75_ep, {'cmd': 'frequency', 'freq': args})
                                result = {'ok': True, 'response': f'freq set via link'}
                            elif cmd == 'status':
                                _link.send_command_to(_d75_ep, {'cmd': 'status'})
                                result = {'ok': True, 'response': 'status requested'}
                            elif cmd in ('btstart', 'btstop', 'reconnect', 'start_service'):
                                # BT lifecycle managed by the remote endpoint — not applicable
                                result = {'ok': True, 'response': f'{cmd}: managed by link endpoint'}
                            elif cmd == 'mute':
                                # Mute the link audio source on the gateway side
                                _link_src = None
                                for _src_name, _src in getattr(gw, 'link_endpoints', {}).items():
                                    if 'd75' in _src_name.lower():
                                        _link_src = _src
                                        break
                                if _link_src:
                                    _link_src.muted = not _link_src.muted
                                    result = {'ok': True, 'muted': _link_src.muted}
                                else:
                                    result = {'ok': True, 'response': 'mute toggled'}
                            elif cmd == 'vol':
                                try:
                                    pct = int(args)
                                    pct = max(0, min(500, pct))
                                    _link.send_command_to(_d75_ep, {'cmd': 'rx_gain', 'gain': pct / 100.0})
                                    result = {'ok': True, 'response': f'boost={pct}%'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'usage: vol 0-500'}
                            elif cmd in ('tone', 'shift', 'offset'):
                                # High-level FO-modify commands
                                _link.send_command_to(_d75_ep, {'cmd': cmd, 'raw': args})
                                result = {'ok': True, 'response': f'{cmd} sent via link'}
                            else:
                                # Pass through as raw CAT
                                _link.send_command_to(_d75_ep, {'cmd': 'cat', 'raw': f'{cmd} {args}'.strip()})
                                result = {'ok': True, 'response': f'sent via link'}

                        elif gw and gw.d75_plugin:
                            # Legacy path: direct D75Plugin
                            if cmd == 'cat':
                                resp = gw.d75_plugin.send_command(f"!cat {args}")
                                result = {'ok': True, 'response': resp or ''}
                            elif cmd == 'ptt':
                                result = gw.d75_plugin.execute({'cmd': 'ptt', 'state': not gw.d75_plugin._ptt_on_state})
                            elif cmd == 'mute':
                                result = gw.d75_plugin.execute({'cmd': 'mute'})
                                gw.d75_muted = gw.d75_plugin.muted
                            elif cmd == 'vol':
                                try:
                                    pct = int(args)
                                    gw.d75_plugin.audio_boost = max(0, min(500, pct)) / 100.0
                                    result = {'ok': True, 'response': f'boost={pct}%'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'usage: vol 0-500'}
                            else:
                                resp = gw.d75_plugin.send_command(f"!{cmd} {args}".strip())
                                result = {'ok': True, 'response': resp or ''}
                        else:
                            result = {'ok': False, 'error': 'D75 not connected (no link endpoint or plugin)'}
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
                elif self.path == '/gpscmd':
                    # GPS command endpoint — set simulated position
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'GPS manager not available'}
                    try:
                        data = json_mod.loads(body)
                        gps = getattr(parent.gateway, 'gps_manager', None) if parent.gateway else None
                        if gps:
                            cmd = data.get('cmd', '')
                            if cmd == 'set_position':
                                ok = gps.set_simulated_position(
                                    lat=data.get('lat'), lon=data.get('lon'),
                                    alt=data.get('alt'), speed=data.get('speed'),
                                    heading=data.get('heading'))
                                result = {'ok': ok, 'error': '' if ok else 'Not in simulate mode'}
                            elif cmd == 'switch_mode':
                                mode = data.get('mode', '')
                                ok, msg = gps.switch_mode(mode)
                                result = {'ok': ok, 'message': msg}
                            elif cmd == 'status':
                                result = {'ok': True, 'status': gps.get_status()}
                            else:
                                result = {'ok': False, 'error': f'Unknown command: {cmd}'}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    try:
                        resp = json_mod.dumps(result).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(resp)))
                        self.end_headers()
                        self.wfile.write(resp)
                    except BrokenPipeError:
                        pass
                elif self.path == '/kv4pcmd':
                    # KV4P command endpoint — routed to KV4PPlugin
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'KV4P plugin not available'}
                    try:
                        data = json_mod.loads(body)
                        _kv4p_p = getattr(parent.gateway, 'kv4p_plugin', None) if parent.gateway else None
                        if _kv4p_p:
                            cmd = data.get('cmd', '')
                            args = data.get('args', '')
                            # Map web UI command format to plugin execute format
                            if cmd == 'freq':
                                result = _kv4p_p.execute({'cmd': 'freq', 'frequency': float(args)})
                            elif cmd == 'txfreq':
                                result = _kv4p_p.execute({'cmd': 'freq', 'frequency': _kv4p_p._frequency, 'tx_frequency': float(args)})
                            elif cmd == 'squelch':
                                result = _kv4p_p.execute({'cmd': 'squelch', 'level': int(args)})
                            elif cmd == 'ctcss':
                                _ctcss_hz = ["67.0","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
                                    "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
                                    "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
                                    "173.8","179.9","186.2","192.8","203.5","210.7","218.1","225.7","233.6","241.8","250.3"]
                                def _hz_to_code(s):
                                    s = str(s).strip()
                                    if s == '0' or s.lower() in ('none', ''):
                                        return 0
                                    try:
                                        return _ctcss_hz.index(s) + 1
                                    except ValueError:
                                        return int(float(s))
                                parts = str(args).split()
                                tx = _hz_to_code(parts[0]) if len(parts) > 0 else 0
                                rx = _hz_to_code(parts[1]) if len(parts) > 1 else tx
                                result = _kv4p_p.execute({'cmd': 'ctcss', 'tx': tx, 'rx': rx})
                            elif cmd == 'bandwidth':
                                result = _kv4p_p.execute({'cmd': 'bandwidth', 'wide': str(args).lower() in ('1', 'wide', 'true')})
                            elif cmd == 'power':
                                result = _kv4p_p.execute({'cmd': 'power', 'high': str(args).lower() in ('1', 'high', 'true', 'h')})
                            elif cmd == 'ptt':
                                result = _kv4p_p.execute({'cmd': 'ptt', 'state': not _kv4p_p._transmitting})
                            elif cmd == 'smeter':
                                if _kv4p_p._radio:
                                    _kv4p_p._radio.enable_smeter(str(args).lower() in ('1', 'true', 'on', ''))
                                result = {'ok': True}
                            elif cmd == 'vol':
                                result = _kv4p_p.execute({'cmd': 'boost', 'value': int(args) / 100.0})
                            elif cmd == 'testtone':
                                result = _kv4p_p.execute({'cmd': 'testtone', 'frequency': float(args) if args else 440})
                            elif cmd == 'record':
                                result = _kv4p_p.execute({'cmd': 'capture'})
                            elif cmd == 'reconnect':
                                result = _kv4p_p.execute({'cmd': 'reconnect'})
                            else:
                                result = _kv4p_p.execute(data)
                        elif data.get('cmd') == 'reconnect' and parent.gateway:
                            # Reconnect even when plugin is None — recreate it
                            try:
                                from kv4p_plugin import KV4PPlugin
                                parent.gateway.kv4p_plugin = KV4PPlugin()
                                if parent.gateway.kv4p_plugin.setup(parent.gateway.config):
                                    parent.gateway.kv4p_plugin = parent.gateway.kv4p_plugin
                                    parent.gateway.kv4p_plugin = parent.gateway.kv4p_plugin
                                    result = {'ok': True, 'response': 'Reconnected'}
                                else:
                                    parent.gateway.kv4p_plugin = None
                                    result = {'ok': False, 'error': 'Reconnect failed'}
                            except Exception as e:
                                result = {'ok': False, 'error': str(e)}
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
                elif self.path == '/linkcmd':
                    # Gateway Link command endpoint — send commands to remote endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        gw = parent.gateway
                        endpoint_name = data.get('endpoint', '')
                        if not endpoint_name:
                            result = {'ok': False, 'error': 'missing endpoint name'}
                        elif not gw or not gw.link_server:
                            result = {'ok': False, 'error': 'link server not running'}
                        elif endpoint_name not in gw.link_endpoints:
                            result = {'ok': False, 'error': f'endpoint not connected: {endpoint_name}'}
                        else:
                            cmd_name = data.get('cmd', '')
                            if cmd_name == 'status':
                                gw._link_last_status[endpoint_name] = {}
                            gw.link_server.send_command_to(endpoint_name, data)
                            if cmd_name == 'status':
                                import time as _time
                                for _ in range(10):  # 1 second max
                                    _time.sleep(0.1)
                                    if gw._link_last_status.get(endpoint_name):
                                        break
                                result = {'ok': True, 'status': gw._link_last_status.get(endpoint_name, {})}
                            else:
                                result = {'ok': True, 'sent': data}
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
                elif self.path == '/catcmd':
                    # CAT radio command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        gw = parent.gateway
                        if cmd == 'SET_TX_RADIO' and gw:
                            radio = data.get('radio', '').lower()
                            if radio in ('th9800', 'd75', 'kv4p'):
                                gw.config.TX_RADIO = radio
                                result = {'ok': True, 'radio': radio}
                            else:
                                result = {'ok': False, 'error': 'unknown radio'}
                        elif cmd == 'GET_TX_RADIO' and gw:
                            result = {'ok': True, 'radio': str(getattr(gw.config, 'TX_RADIO', 'th9800')).lower()}
                        elif cmd == 'CAT_DISCONNECT' and gw and gw.cat_client:
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
                                try:
                                    cat.set_rts(True)
                                except Exception:
                                    pass

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
                        elif cmd == 'MIC_PTT' and gw:
                            # Key/unkey TH-9800 via configured PTT_METHOD, regardless of TX_RADIO
                            gw._web_th9800_ptt = not getattr(gw, '_web_th9800_ptt', False)
                            state = gw._web_th9800_ptt
                            method = str(getattr(gw.config, 'PTT_METHOD', 'aioc')).lower()
                            if method == 'relay':
                                gw._ptt_relay(state)
                            elif method == 'software':
                                gw._ptt_software(state)
                            else:
                                gw._ptt_aioc(state)
                            result = {'ok': True}
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
                    # SDR command endpoint — routed to SDRPlugin
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'SDR plugin not available'}
                    try:
                        data = json_mod.loads(body)
                        _sdr_p = getattr(parent.gateway, 'sdr_plugin', None) if parent.gateway else None
                        if _sdr_p:
                            result = _sdr_p.execute(data)
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
                            import time as _trace_time
                            _gw._audio_trace_t0 = _trace_time.monotonic()
                            print(f"\n[Trace] Recording STARTED (via web UI)")
                        else:
                            print(f"\n[Trace] Recording STOPPED ({len(_gw._audio_trace)} ticks captured)")
                            _gw._dump_audio_trace()
                        import time as _trace_time2
                        _gw._trace_events.append((_trace_time2.monotonic(), 'trace', 'on' if _gw._trace_recording else 'off'))
                        result = {'ok': True, 'active': _gw._trace_recording}
                    elif _gw and trace_type == 'watchdog':
                        _gw._watchdog_active = not _gw._watchdog_active
                        if _gw._watchdog_active:
                            _gw._watchdog_t0 = time.monotonic()
                            _gw._watchdog_thread = _thr.Thread(
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
                elif self.path == '/reboothost':
                    import subprocess as _sp
                    result = {'ok': False}
                    try:
                        _sp.Popen(['sudo', 'reboot'])
                        result = {'ok': True}
                    except Exception as _e:
                        result = {'ok': False, 'error': str(_e)}
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
                elif self.path == '/recordingsdelete':
                    # Delete selected recording files
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    data = json_mod.loads(body)
                    filenames = data.get('files', [])
                    delete_all = data.get('delete_all', False)
                    rec_dir = ''
                    if parent.gateway and parent.gateway.automation_engine:
                        rec_dir = parent.gateway.automation_engine.recorder._dir
                    deleted = 0
                    if rec_dir and os.path.isdir(rec_dir):
                        if delete_all:
                            for fname in os.listdir(rec_dir):
                                fpath = os.path.join(rec_dir, fname)
                                if os.path.isfile(fpath):
                                    try:
                                        os.remove(fpath)
                                        deleted += 1
                                    except OSError:
                                        pass
                        else:
                            for fname in filenames:
                                fname = os.path.basename(fname)  # no path traversal
                                fpath = os.path.join(rec_dir, fname)
                                if os.path.isfile(fpath):
                                    try:
                                        os.remove(fpath)
                                        deleted += 1
                                    except OSError:
                                        pass
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps({'deleted': deleted}).encode('utf-8'))
                    return

                elif self.path == '/telegramcmd':
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'unknown command'}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        if cmd in ('start', 'stop', 'restart'):
                            _r = subprocess.run(['sudo', 'systemctl', cmd, 'telegram-bot'],
                                                capture_output=True, text=True, timeout=10)
                            result = {'ok': _r.returncode == 0,
                                      'output': (_r.stdout + _r.stderr).strip()}
                        elif cmd == 'enable':
                            _r = subprocess.run(['sudo', 'systemctl', 'enable', 'telegram-bot'],
                                                capture_output=True, text=True, timeout=10)
                            result = {'ok': _r.returncode == 0}
                        elif cmd == 'disable':
                            _r = subprocess.run(['sudo', 'systemctl', 'disable', 'telegram-bot'],
                                                capture_output=True, text=True, timeout=10)
                            result = {'ok': _r.returncode == 0}
                        elif cmd == 'logs':
                            _r = subprocess.run(['journalctl', '-u', 'telegram-bot', '--no-pager', '-n', '50'],
                                                capture_output=True, text=True, timeout=5)
                            result = {'ok': True, 'logs': _r.stdout}
                        else:
                            result = {'ok': False, 'error': f'unknown command: {cmd}'}
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode('utf-8'))
                    return

                elif self.path == '/open_tmux':
                    # Open local terminal attached to Claude tmux session
                    session = getattr(parent.config, 'TELEGRAM_TMUX_SESSION', 'claude-gateway') if parent.config else 'claude-gateway'
                    try:
                        subprocess.Popen(
                            ['xfce4-terminal', '-e', f'tmux attach-session -t {session}'],
                            env={**os.environ, 'DISPLAY': ':0'},
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        ok = True
                    except Exception:
                        ok = False
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps({'ok': ok}).encode('utf-8'))
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

                elif self.path == '/routing/cmd':
                    import json as json_mod
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False, 'error': 'invalid'}
                    try:
                        data = json_mod.loads(body)
                        result = parent._handle_routing_cmd(data)
                    except Exception as e:
                        result = {'ok': False, 'error': str(e)}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode())
                    return

                elif self.path == '/voice/send':
                    import json as json_mod
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    try:
                        data = json_mod.loads(body)
                    except Exception:
                        data = {}
                    text = data.get('text', '').strip()
                    tmux_target = os.environ.get('TMUX_TARGET', 'claude-voice')
                    if not text:
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(b'{"error":"empty text"}')
                        return
                    chk = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
                    if chk.returncode != 0:
                        self.send_response(503)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps({'error': f"tmux session '{tmux_target}' not found"}).encode())
                        return
                    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', text])
                    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps({'ok': True, 'sent': text}).encode())
                    return

                elif self.path == '/voice/session':
                    import json as json_mod
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    try:
                        data = json_mod.loads(body)
                    except Exception:
                        data = {}
                    action = data.get('action', '')
                    tmux_target = 'claude-voice'
                    result = {'ok': False}

                    if action == 'start':
                        # Create session if it doesn't exist, then launch claude
                        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
                        if has.returncode == 0:
                            result = {'ok': True, 'msg': 'session already exists'}
                        else:
                            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions'])
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                            # Auto-confirm workspace trust prompt after startup
                            import time; time.sleep(3)
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                            result = {'ok': True, 'msg': 'session created, claude started'}

                    elif action == 'restart':
                        # Send Ctrl+C to stop current process, wait, then start claude again
                        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
                        if has.returncode != 0:
                            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
                        else:
                            # Send Ctrl+C twice to kill any running process
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
                            import time; time.sleep(0.5)
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
                            import time; time.sleep(1)
                            # Clear the screen before starting fresh
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                            import time; time.sleep(0.3)
                        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions'])
                        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                        # Auto-confirm workspace trust prompt after startup
                        import time; time.sleep(3)
                        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                        result = {'ok': True, 'msg': 'claude restarted'}

                    elif action == 'stop':
                        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
                        if has.returncode == 0:
                            # Send Ctrl+C to stop Claude, clear screen, leave the shell running
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
                            import time; time.sleep(0.5)
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
                            import time; time.sleep(0.5)
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
                            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
                            result = {'ok': True, 'msg': 'claude stopped'}
                        else:
                            result = {'ok': True, 'msg': 'session not running'}
                    else:
                        result = {'ok': False, 'error': 'unknown action'}

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json_mod.dumps(result).encode())
                    return

                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                form = urllib.parse.parse_qs(body, keep_blank_values=True)
                # Flatten: parse_qs returns lists; for checkboxes with hidden fallback,
                # take the LAST value (checkbox 'true' comes after hidden 'false')
                values = {k: v[-1] for k, v in form.items() if k != '_action'}
                action = form.get('_action', ['save'])[0]

                # Checkboxes: the hidden fallback field ensures unchecked boxes
                # submit 'false'. If a boolean key is completely absent from the
                # form (page not fully loaded, truncated POST), do NOT force it
                # to false — use the current running value instead.
                # Only force false if we received a reasonable number of keys
                # (full form submission has 200+ keys).
                if len(values) > 100:
                    for key, default_val in parent._defaults.items():
                        if isinstance(default_val, bool) and key not in values:
                            values[key] = 'false'
                else:
                    print(f"  [Config] WARNING: partial form ({len(values)} keys) — merging with current config")

                parent._save_config(values)
                # Reload config from file so the config page reflects saved values
                parent.config.load_config()
                self.send_response(303)
                self.send_header('Location', '/config?saved=1')
                self.end_headers()

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 32  # default 5 is too low for concurrent dashboard clients

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

            self._thread = _thr.Thread(target=self._server.serve_forever,
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
            t = _thr.Thread(target=_reader, daemon=True, name='mp3-reader')
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
            t2 = _thr.Thread(target=_silence_feed, daemon=True, name='mp3-silence')
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
        ev = _thr.Event()
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

        t = _thr.Thread(target=_renewal_loop, name='CertRenewal', daemon=True)
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
        """Write ALL parameters to config file using _CONFIG_LAYOUT as the master structure.

        The gateway controls what's in the file — every known parameter is written,
        organized by the canonical section ordering."""
        print(f"  [Config] Saving {len(new_values)} keys")
        config_path = self.config.config_file

        lines = []
        for section, display_name, keys in self._CONFIG_LAYOUT:
            lines.append(f'\n[{section}]\n\n')
            for key in keys:
                # Use submitted value if present, else current config value, else default
                if key in new_values:
                    val = new_values[key]
                elif hasattr(self.config, key):
                    val = getattr(self.config, key)
                else:
                    val = self._defaults.get(key, '')
                # Format hex keys
                if key in self._HEX_KEYS:
                    try:
                        val = hex(int(val))
                    except (ValueError, TypeError):
                        pass
                # Format booleans consistently
                if isinstance(val, bool):
                    val = str(val).lower()
                lines.append(f'{key} = {val}\n')

        # Write atomically via temp file
        tmp_path = config_path + '.tmp'
        with open(tmp_path, 'w') as f:
            f.writelines(lines)
        os.replace(tmp_path, config_path)

    def _radio_nav_links(self, style='inline'):
        """Build conditional radio nav links based on enabled radios."""
        links = []
        if getattr(self.config, 'ENABLE_CAT_CONTROL', False) or getattr(self.config, 'ENABLE_TH9800', False):
            links.append('<a href="/radio">TH-9800</a>')
        if getattr(self.config, 'ENABLE_D75', False):
            links.append('<a href="/d75">TH-D75</a>')
        if getattr(self.config, 'ENABLE_KV4P', False):
            links.append('<a href="/kv4p">KV4P HT</a>')
        return ' | '.join(links)

    def _radio_nav_buttons(self):
        """Build conditional radio nav buttons for logs page."""
        html = ''
        if getattr(self.config, 'ENABLE_CAT_CONTROL', False) or getattr(self.config, 'ENABLE_TH9800', False):
            html += '    <a href="/radio" class="rb rb-sm" style="text-decoration:none;">TH-9800</a>\n'
        if getattr(self.config, 'ENABLE_D75', False):
            html += '    <a href="/d75" class="rb rb-sm" style="text-decoration:none;">D75</a>\n'
        if getattr(self.config, 'ENABLE_KV4P', False):
            html += '    <a href="/kv4p" class="rb rb-sm" style="text-decoration:none;">KV4P</a>\n'
        return html


    def _wrap_html(self, title, body):
        """Wrap body content in the standard HTML shell."""
        t = self._get_theme()
        gw_name = str(getattr(self.config, 'GATEWAY_NAME', '') or '').strip()
        _title_prefix = f'{gw_name} - ' if gw_name else ''
        return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_title_prefix}Radio Gateway - {title}</title>
<style>
  :root {{
    --t-bg: {t['bg']}; --t-panel: {t['panel']}; --t-border: {t['border']};
    --t-accent: {t['accent']}; --t-btn: {t['btn']}; --t-btn-border: {t['btn_border']};
    --t-btn-hover: {t['btn_hover']}; --t-btn-active: {t['btn_active_bg']};
    --t-checkbox: {t['checkbox']};
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
         background: var(--t-bg); color: #e0e0e0; margin: 0; padding: 20px; }}
  h1 {{ color: var(--t-accent); margin: 0 0 20px; font-size: 1.4em; }}
  h2 {{ color: var(--t-accent); margin: 10px 0; font-size: 1.2em; }}
  a {{ color: var(--t-accent); }}
  details {{ background: var(--t-panel); border: 1px solid var(--t-border); border-radius: 6px;
            margin: 8px 0; }}
  summary {{ cursor: pointer; padding: 10px 14px; font-weight: bold; color: var(--t-accent);
            font-size: 0.95em; user-select: none; }}
  summary:hover {{ background: var(--t-btn-hover); }}
  .fields {{ padding: 8px 14px 14px; }}
  .field {{ display: flex; align-items: center; margin: 4px 0; gap: 8px; }}
  .field label {{ min-width: 320px; font-size: 0.85em; color: #b0b0b0; }}
  .field input[type="text"], .field input[type="number"], .field input[type="password"], .field select {{
    flex: 1; background: var(--t-btn); border: 1px solid var(--t-btn-border); color: #e0e0e0;
    padding: 5px 8px; border-radius: 3px; font-family: monospace; font-size: 0.85em;
    max-width: 500px; }}
  .field input[type="checkbox"] {{ width: 18px; height: 18px; accent-color: var(--t-checkbox); }}
  .field .default {{ font-size: 0.75em; color: #ffffff; margin-left: 8px; }}
  .buttons {{ position: sticky; top: 0; background: var(--t-bg); padding: 10px 0;
             z-index: 10; border-bottom: 1px solid var(--t-border); margin-bottom: 10px;
             display: flex; gap: 10px; }}
  button {{ padding: 8px 20px; border: none; border-radius: 4px; cursor: pointer;
           font-size: 0.9em; font-weight: bold; }}
  .btn-save {{ background: var(--t-border); color: #e0e0e0; }}
  .btn-save:hover {{ background: var(--t-btn-hover); }}
  .btn-restart {{ background: #c0392b; color: #fff; }}
  .btn-restart:hover {{ background: #e74c3c; }}
  .btn-exit {{ background: #7d3c98; color: #fff; margin-left: auto; }}
  .btn-exit:hover {{ background: #9b59b6; }}
</style>
<script>var _T={{bg:'{t['bg']}',panel:'{t['panel']}',border:'{t['border']}',accent:'{t['accent']}',btn:'{t['btn']}',btnBorder:'{t['btn_border']}',btnHover:'{t['btn_hover']}',btnActive:'{t['btn_active_bg']}'}}</script>
</head><body>{body}</body></html>'''








    # ── Routing API ──────────────────────────────────────────────────────

    def _get_routing_status(self):
        """Return current routing state for the web UI."""
        import json as _json
        gw = self.gateway

        # Build source list from available plugins/sources
        sources = []
        def _src_info(obj):
            """Get muted and gain from a source/plugin object."""
            return {
                'muted': getattr(obj, 'muted', False),
                'gain': int(getattr(obj, 'audio_boost', 1.0) * 100),
            }

        if gw:
            if gw.sdr_plugin:
                sources.append({**{'id': 'sdr', 'name': 'SDR [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.sdr_plugin)})
            if gw.kv4p_plugin:
                sources.append({**{'id': 'kv4p', 'name': 'KV4P [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.kv4p_plugin)})
            if gw.d75_plugin:
                sources.append({**{'id': 'd75', 'name': 'TH-D75 [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.d75_plugin)})
            elif any('d75' in n.lower() for n in gw.link_endpoints):
                _d75_src = next((s for n, s in gw.link_endpoints.items() if 'd75' in n.lower()), None)
                if _d75_src:
                    sources.append({**{'id': 'd75', 'name': 'TH-D75 [RX]', 'enabled': True,
                                    'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_d75_src)})
            if getattr(gw, 'th9800_plugin', None):
                sources.append({**{'id': 'aioc', 'name': 'TH-9800 [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.th9800_plugin)})
            if getattr(gw, 'playback_source', None):
                sources.append({**{'id': 'playback', 'name': 'File Playback', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.playback_source)})
            if getattr(gw, 'web_mic_source', None):
                sources.append({**{'id': 'webmic', 'name': 'Web Mic', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.web_mic_source)})
            if getattr(gw, 'announce_input_source', None):
                sources.append({**{'id': 'announce', 'name': 'Announcements', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.announce_input_source)})
            if getattr(gw, 'web_monitor_source', None):
                sources.append({**{'id': 'monitor', 'name': 'Room Monitor', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.web_monitor_source)})
            if gw.mumble:
                sources.append({'id': 'mumble_rx', 'name': 'Mumble [RX]', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True,
                                'muted': False, 'gain': 100})
            if getattr(gw, 'remote_audio_source', None):
                sources.append({**{'id': 'remote_audio', 'name': 'Remote Audio [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.remote_audio_source)})

        # Build sink list (passive consumers + TX-capable radios)
        sinks = []
        sinks.append({'id': 'mumble', 'name': 'Mumble [TX]', 'type': 'VoIP',
                      'enabled': bool(gw and gw.mumble)})
        sinks.append({'id': 'broadcastify', 'name': 'Broadcastify', 'type': 'Stream',
                      'enabled': bool(gw and getattr(gw, 'stream_output', None))})
        _spk_mode = str(getattr(gw.config, 'SPEAKER_MODE', 'virtual')).lower() if gw else 'virtual'
        sinks.append({'id': 'speaker', 'name': 'Speaker', 'type': 'Local',
                      'enabled': True, 'speaker_mode': _spk_mode})
        sinks.append({'id': 'recording', 'name': 'Recording', 'type': 'File', 'enabled': True})
        if gw and getattr(gw, 'transcriber', None):
            sinks.append({'id': 'transcription', 'name': 'Transcription', 'type': 'AI',
                          'enabled': True})
        if gw and getattr(gw, 'remote_audio_server', None):
            sinks.append({'id': 'remote_audio_tx', 'name': 'Remote Audio [TX]', 'type': 'Network',
                          'enabled': bool(gw.remote_audio_server.connected)})
        # TX-capable radios as destinations
        if gw:
            if gw.kv4p_plugin:
                sinks.append({**{'id': 'kv4p_tx', 'name': 'KV4P [TX]', 'type': 'Radio TX', 'enabled': True}, **_src_info(gw.kv4p_plugin)})
            if gw.d75_plugin:
                sinks.append({**{'id': 'd75_tx', 'name': 'TH-D75 [TX]', 'type': 'Radio TX', 'enabled': True}, **_src_info(gw.d75_plugin)})
            elif any('d75' in n.lower() for n in gw.link_endpoints):
                _d75_src = next((s for n, s in gw.link_endpoints.items() if 'd75' in n.lower()), None)
                if _d75_src:
                    sinks.append({**{'id': 'd75_tx', 'name': 'TH-D75 [TX]', 'type': 'Radio TX', 'enabled': True}, **_src_info(_d75_src)})
            if getattr(gw, 'th9800_plugin', None):
                sinks.append({**{'id': 'aioc_tx', 'name': 'TH-9800 [TX]', 'type': 'Radio TX', 'enabled': True}, **_src_info(gw.th9800_plugin)})

        # Load bus config
        busses, connections, saved_layout = self._load_routing_config()

        return {
            'sources': sources,
            'layout': saved_layout,
            'busses': busses,
            'sinks': sinks,
            'connections': connections,
        }

    def _handle_routing_cmd(self, data):
        """Handle routing commands from the web UI."""
        cmd = data.get('cmd', '')
        busses, connections, _layout = self._load_routing_config()

        if cmd == 'add_bus':
            name = data.get('name', '').strip()
            bus_type = data.get('type', 'listen').strip()
            if not name:
                return {'ok': False, 'error': 'name required'}
            if bus_type not in ('listen', 'solo', 'duplex', 'simplex'):
                return {'ok': False, 'error': f'invalid type: {bus_type}'}
            bus_id = name.lower().replace(' ', '_')
            if any(b['id'] == bus_id for b in busses):
                return {'ok': False, 'error': f'bus "{bus_id}" already exists'}
            busses.append({'id': bus_id, 'name': name, 'type': bus_type, 'sources': [], 'sinks': []})
            self._save_routing_config(busses, connections)
            return {'ok': True}

        elif cmd == 'delete_bus':
            bus_id = data.get('id', '')
            busses = [b for b in busses if b['id'] != bus_id]
            connections = [c for c in connections if c.get('from') != bus_id and c.get('to') != bus_id]
            self._save_routing_config(busses, connections)
            return {'ok': True}

        elif cmd == 'connect':
            source = data.get('source')
            bus = data.get('bus')
            sink = data.get('sink')

            if source and bus:
                # Source → Bus connection
                conn = {'type': 'source-bus', 'from': source, 'to': bus}
                if conn not in connections:
                    connections.append(conn)
                    # Update bus sources list
                    for b in busses:
                        if b['id'] == bus and source not in b.get('sources', []):
                            b.setdefault('sources', []).append(source)
                self._save_routing_config(busses, connections)
                return {'ok': True}

            elif bus and sink:
                # Bus → Sink connection
                conn = {'type': 'bus-sink', 'from': bus, 'to': sink}
                if conn not in connections:
                    connections.append(conn)
                    for b in busses:
                        if b['id'] == bus and sink not in b.get('sinks', []):
                            b.setdefault('sinks', []).append(sink)
                self._save_routing_config(busses, connections)
                return {'ok': True}

            return {'ok': False, 'error': 'specify source+bus or bus+sink'}

        elif cmd == 'disconnect':
            source = data.get('source')
            bus = data.get('bus')
            sink = data.get('sink')

            if source and bus:
                connections = [c for c in connections if not (c['type'] == 'source-bus' and c['from'] == source and c['to'] == bus)]
                for b in busses:
                    if b['id'] == bus:
                        b['sources'] = [s for s in b.get('sources', []) if s != source]
            elif bus and sink:
                connections = [c for c in connections if not (c['type'] == 'bus-sink' and c['from'] == bus and c['to'] == sink)]
                for b in busses:
                    if b['id'] == bus:
                        b['sinks'] = [s for s in b.get('sinks', []) if s != sink]

            self._save_routing_config(busses, connections)
            return {'ok': True}

        elif cmd == 'save_all':
            # Full save from Drawflow — replace connections + update bus sources/sinks + layout
            new_connections = data.get('connections', [])
            bus_updates = data.get('bus_updates', {})
            layout = data.get('layout')
            for b in busses:
                upd = bus_updates.get(b['id'], {})
                b['sources'] = upd.get('sources', [])
                b['sinks'] = upd.get('sinks', [])
            self._save_routing_config(busses, new_connections, layout=layout)
            # Reload bus manager, refresh cached maps, sync mixer sources
            if self.gateway and hasattr(self.gateway, 'bus_manager') and self.gateway.bus_manager:
                try:
                    self.gateway.bus_manager.reload()
                    self.gateway._bus_stream_flags = self.gateway.bus_manager.get_bus_stream_flags()
                    self.gateway._bus_sinks = self.gateway.bus_manager.get_bus_sinks()
                    self.gateway._listen_bus_id = self.gateway.bus_manager.get_listen_bus_id()
                except Exception as e:
                    return {'ok': True, 'warning': f'saved but reload failed: {e}'}
            # Reset stale sink audio levels
            if self.gateway:
                self.gateway.speaker_audio_level = 0
                self.gateway.stream_audio_level = 0
                self.gateway.mumble_tx_level = 0
                if getattr(self.gateway, 'th9800_plugin', None):
                    self.gateway.th9800_plugin.tx_audio_level = 0
                if self.gateway.kv4p_plugin:
                    self.gateway.kv4p_plugin.tx_audio_level = 0
                if self.gateway.d75_plugin:
                    self.gateway.d75_plugin.tx_audio_level = 0
            if self.gateway and hasattr(self.gateway, 'sync_mixer_sources'):
                try:
                    self.gateway.sync_mixer_sources()
                except Exception as e:
                    print(f"  [routing] sync_mixer_sources error: {e}")
            # Log reload confirmation
            _bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
            if _bm:
                _bus_ids = list(_bm._busses.keys())
                _sink_map = getattr(self.gateway, '_bus_sinks', {})
                print(f"  [routing] Saved & reloaded: busses={_bus_ids} sinks={dict(_sink_map)}")
            return {'ok': True}

        elif cmd == 'toggle_proc':
            bus_id = data.get('bus', '')
            filt = data.get('filter', '')
            if filt not in ('gate', 'hpf', 'lpf', 'notch', 'pcm', 'mp3', 'vad'):
                return {'ok': False, 'error': f'invalid filter: {filt}'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc[filt] = not proc.get(filt, False)
                    self._save_routing_config(busses, connections)
                    # Update cached stream flags on gateway + BusManager
                    if filt in ('pcm', 'mp3', 'vad') and self.gateway:
                        flags = getattr(self.gateway, '_bus_stream_flags', {})
                        bus_flags = flags.setdefault(bus_id, {'pcm': False, 'mp3': False, 'vad': False})
                        bus_flags[filt] = proc[filt]
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm and bus_id in bm._bus_config:
                        bm._bus_config[bus_id][filt] = proc[filt]
                    return {'ok': True, 'state': proc[filt]}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'bus_mute':
            bus_id = data.get('bus', '')
            for b in busses:
                if b['id'] == bus_id:
                    b['muted'] = not b.get('muted', False)
                    self._save_routing_config(busses, connections)
                    # Update live BusManager state
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm and bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['muted'] = b['muted']
                    # Update primary listen bus mute cache
                    if self.gateway and bus_id == getattr(self.gateway, '_listen_bus_id', None):
                        self.gateway._listen_bus_muted = b['muted']
                    return {'ok': True, 'muted': b['muted']}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'mute':
            target_id = data.get('id', '')
            # Check if it's a sink without a plugin object
            _sink_ids = ('speaker', 'broadcastify', 'mumble', 'recording', 'remote_audio_tx')
            if target_id in _sink_ids and self.gateway:
                muted_sinks = getattr(self.gateway, '_muted_sinks', set())
                if target_id in muted_sinks:
                    muted_sinks.discard(target_id)
                    muted = False
                else:
                    muted_sinks.add(target_id)
                    muted = True
                self.gateway._muted_sinks = muted_sinks
                return {'ok': True, 'muted': muted}
            plugin = self._get_plugin_by_id(target_id)
            if plugin:
                plugin.muted = not getattr(plugin, 'muted', False)
                return {'ok': True, 'muted': plugin.muted}
            return {'ok': False, 'error': f'unknown source/sink: {target_id}'}

        elif cmd == 'gain':
            target_id = data.get('id', '')
            value = int(data.get('value', 100))
            plugin = self._get_plugin_by_id(target_id)
            if plugin:
                plugin.audio_boost = value / 100.0
                return {'ok': True, 'gain': value}
            return {'ok': False, 'error': f'unknown source/sink: {target_id}'}

        elif cmd == 'speaker_mode':
            mode = data.get('mode', 'virtual').lower()
            if mode not in ('virtual', 'auto', 'real'):
                return {'ok': False, 'error': f'invalid mode: {mode}'}
            gw = self.gateway
            if not gw:
                return {'ok': False, 'error': 'gateway not ready'}
            gw.config.SPEAKER_MODE = mode
            # Close existing real stream if switching to virtual
            if mode == 'virtual':
                if gw.speaker_stream:
                    try:
                        if gw.speaker_stream.is_active():
                            gw.speaker_stream.stop_stream()
                        gw.speaker_stream.close()
                    except Exception:
                        pass
                    gw.speaker_stream = None
                    gw.speaker_queue = None
                    print(f"  [Speaker] Switched to virtual (metering only)")
                return {'ok': True, 'mode': mode, 'device': None}
            else:
                # Try to open real device
                if not gw.speaker_stream:
                    gw.open_speaker_output()
                _dev = 'connected' if gw.speaker_stream else 'virtual (fallback)'
                return {'ok': True, 'mode': mode if gw.speaker_stream else 'virtual', 'device': _dev}

        return {'ok': False, 'error': f'unknown command: {cmd}'}

    def _get_plugin_by_id(self, id):
        """Resolve a source/sink ID to the corresponding plugin/source object."""
        gw = self.gateway
        if not gw:
            return None
        _map = {
            'sdr': gw.sdr_plugin,
            'kv4p': gw.kv4p_plugin,
            'd75': gw.d75_plugin,
            'kv4p_tx': gw.kv4p_plugin,
            'd75_tx': gw.d75_plugin,
            'aioc': getattr(gw, 'th9800_plugin', None),
            'aioc_tx': getattr(gw, 'th9800_plugin', None),
            'playback': getattr(gw, 'playback_source', None),
            'webmic': getattr(gw, 'web_mic_source', None),
            'announce': getattr(gw, 'announce_input_source', None),
            'monitor': getattr(gw, 'web_monitor_source', None),
            'mumble_rx': getattr(gw, 'mumble_source', None),
            'remote_audio': getattr(gw, 'remote_audio_source', None),
        }
        result = _map.get(id)
        # Fallback to link endpoints for D75 when plugin is None
        if result is None and id in ('d75', 'd75_tx'):
            for name, src in gw.link_endpoints.items():
                if 'd75' in name.lower():
                    return src
        return result

    _ROUTING_CONFIG_PATH = None

    def _routing_config_path(self):
        if not self._ROUTING_CONFIG_PATH:
            import os
            self._ROUTING_CONFIG_PATH = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'routing_config.json')
        return self._ROUTING_CONFIG_PATH

    def _load_routing_config(self):
        """Load bus config from JSON file. Returns (busses, connections, layout)."""
        import json, os
        path = self._routing_config_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get('busses', []), data.get('connections', []), data.get('layout')
            except Exception:
                pass
        return [], [], None

    def _save_routing_config(self, busses, connections, layout=None):
        """Save bus config to JSON file."""
        import json
        path = self._routing_config_path()
        try:
            data = {'busses': busses, 'connections': connections}
            if layout:
                data['layout'] = layout
            else:
                # Preserve existing layout if not provided
                try:
                    with open(path) as f:
                        old = json.load(f)
                    if 'layout' in old:
                        data['layout'] = old['layout']
                except Exception:
                    pass
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  [Routing] Failed to save config: {e}")

    def _get_sysinfo(self):
        """Gather system status: CPU, memory, disk I/O, network, temps, IPs."""
        import os
        info = {}
        try:
            # CPU usage — average across cores from /proc/stat delta
            # Cache result for 1s minimum to prevent near-zero deltas from rapid polls
            if not hasattr(self, '_prev_cpu'):
                self._prev_cpu = None
                self._prev_cpu_time = 0
                self._cached_cpu_pct = 0.0
            now = time.monotonic()
            if now - self._prev_cpu_time < 1.0:
                info['cpu_pct'] = self._cached_cpu_pct
            else:
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                parts = line.split()
                cur = [int(x) for x in parts[1:8]]
                if self._prev_cpu:
                    d = [c - p for c, p in zip(cur, self._prev_cpu)]
                    total = sum(d) or 1
                    idle = d[3] + d[4]  # idle + iowait
                    self._cached_cpu_pct = round(100.0 * (total - idle) / total, 1)
                info['cpu_pct'] = self._cached_cpu_pct
                self._prev_cpu = cur
                self._prev_cpu_time = now

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
            info['gateway_name'] = str(getattr(self.config, 'GATEWAY_NAME', '') or '').strip() if self.gateway else ''

            # Cloudflare tunnel URL for display in system status
            if self.gateway and self.gateway.cloudflare_tunnel:
                info['tunnel_url'] = self.gateway.cloudflare_tunnel.get_url() or ''
            else:
                info['tunnel_url'] = ''
        except Exception:
            info['ips'] = []
            info['hostname'] = ''

        return info






    def _generate_html(self):
        """Build the full HTML page with form inputs grouped by section.

        Uses _CONFIG_LAYOUT as the single source of truth for sections and key order."""
        # Reload config from file to pick up any external edits
        self.config.load_config()

        # Build form HTML from canonical layout
        form_parts = []
        for idx, (section, display_name, keys) in enumerate(self._CONFIG_LAYOUT):
            fields_html = []
            for key in keys:
                cur_val = getattr(self.config, key, '')
                default_val = self._defaults.get(key, None)
                field = self._render_field(key, cur_val, default_val)
                fields_html.append(field)

            open_attr = ''
            form_parts.append(
                f'<details{open_attr}><summary>{display_name}</summary>'
                f'<div class="fields">{"".join(fields_html)}</div></details>')

        body = (
            ''
            '<form method="POST" action="/config">'
            '<div class="buttons">'
            '<button type="submit" name="_action" value="save" class="btn-save">Save</button>'
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
        select_opts = self._SELECT_OPTIONS.get(key)

        if is_bool:
            checked = ' checked' if cur_val else ''
            # Hidden field ensures unchecked boxes submit 'false'
            inp = (f'<input type="hidden" name="{key}" value="false">'
                   f'<input type="checkbox" name="{key}" value="true"{checked}>')
            default_str = str(default_val).lower() if default_val is not None else ''
        elif select_opts is not None:
            # Dropdown for fixed-value parameters
            cur_str = str(cur_val).lower().strip() if cur_val is not None else ''
            # Handle int values (e.g. TTS_DEFAULT_VOICE)
            if isinstance(cur_val, int):
                cur_str = str(cur_val)
            options = []
            for opt in select_opts:
                if isinstance(opt, tuple):
                    val, label = opt
                else:
                    val = label = str(opt)
                selected = ' selected' if str(val) == cur_str else ''
                options.append(f'<option value="{html_mod.escape(val)}"{selected}>{html_mod.escape(label)}</option>')
            inp = f'<select name="{key}">{"".join(options)}</select>'
            default_str = str(default_val) if default_val is not None else ''
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

        # Build hint: field-specific hint + default value
        hint_parts = []
        field_hint = self._FIELD_HINTS.get(key)
        if field_hint:
            hint_parts.append(field_hint)
        if default_str:
            hint_parts.append(f'default: {default_str}')
        hint_text = ' | '.join(hint_parts)
        hint_html = f'<span class="default">{hint_text}</span>' if hint_text else ''

        # Add visual separator before the first key of each smart announce slot
        sep = ' style="margin-top:18px; border-top:1px solid var(--t-border); padding-top:12px"' if key in self._GROUP_SEPARATOR_KEYS else ''
        return (f'<div class="field"{sep}>'
                f'<label for="{key}">{key}</label>{inp}{hint_html}'
                f'</div>')


