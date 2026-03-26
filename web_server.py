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
import select
import array as _array_mod
import math as _math_mod
import re
import numpy as np

from audio_sources import generate_cw_pcm, D75AudioSource
from smart_announce import SmartAnnouncementManager
from cat_client import RadioCATClient, D75CATClient

# ============================================================================
# WEB CONFIGURATION UI
# ============================================================================

class WebConfigServer:
    """Lightweight web UI for editing gateway_config.txt.

    Runs Python's built-in http.server on a daemon thread.  Serves a
    single-page form grouped by INI sections with Save and Save & Restart.
    """

    # Keys whose values should be masked in the UI
    _SENSITIVE_KEYS = set()  # Show all values in plain text

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
        'REMOTE_AUDIO_ROLE': ['disabled', 'server', 'client'],
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
            'SDR_INTERNAL_AUTOSTART',
            'ENABLE_SDR', 'SDR_DEVICE_NAME', 'SDR_DUCK', 'SDR_MIX_RATIO',
            'SDR_DISPLAY_GAIN', 'SDR_AUDIO_BOOST', 'SDR_BUFFER_MULTIPLIER',
            'SDR_WATCHDOG_TIMEOUT', 'SDR_WATCHDOG_MAX_RESTARTS',
            'ENABLE_SDR2', 'SDR2_DEVICE_NAME', 'SDR2_DUCK', 'SDR2_MIX_RATIO',
            'SDR2_DISPLAY_GAIN', 'SDR2_AUDIO_BOOST', 'SDR2_BUFFER_MULTIPLIER',
            'SDR2_WATCHDOG_TIMEOUT', 'SDR2_WATCHDOG_MAX_RESTARTS',
            'SDR_PRIORITY_ORDER',
        ]),
        ('switching', 'Signal Detection & Switching', [
            'SIGNAL_ATTACK_TIME', 'SIGNAL_RELEASE_TIME', 'SWITCH_PADDING_TIME',
            'SDR_DUCK_COOLDOWN', 'SDR_SIGNAL_THRESHOLD', 'SDR_REBROADCAST_PTT_HOLD',
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
            'ENABLE_SPEAKER_OUTPUT', 'SPEAKER_OUTPUT_DEVICE', 'SPEAKER_VOLUME',
            'SPEAKER_START_MUTED',
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
        ('usbip', 'USB/IP Remote Devices', [
            'ENABLE_USBIP', 'USBIP_SERVER', 'USBIP_DEVICES',
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
            'ENABLE_CLOUDFLARE_TUNNEL',
        ]),
        ('advanced', 'Advanced / Diagnostics', [
            'HEADLESS_MODE', 'LOG_BUFFER_LINES', 'LOG_FILE_DAYS',
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
        self._stream_lock = threading.Lock()
        self._mp3_buffer = []         # shared ring buffer of MP3 chunks
        self._mp3_seq = 0             # sequence number of next append
        self._encoder_proc = None     # shared FFmpeg process
        self._encoder_stdin = None    # stdin pipe for encoder
        self._last_audio_push = 0     # monotonic time of last real audio
        self.sdr_manager = None       # RTLAirbandManager instance
        self.usbip_manager = None     # USBIPManager instance
        # WebSocket PCM streaming (low-latency)
        self._ws_clients = []         # list of (socket, queue) tuples for WebSocket PCM clients
        self._ws_lock = threading.Lock()

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

        # Initialize SDR manager if rtl_airband is available
        if shutil.which('rtl_airband'):
            try:
                from gateway_core import RTLAirbandManager
                self.sdr_manager = RTLAirbandManager(os.path.dirname(
                    getattr(self.config, '_config_path', '') or os.path.join(os.path.dirname(__file__), 'gateway_config.txt')))
            except Exception as e:
                print(f"  [SDR] Manager init failed: {e}")
                self.sdr_manager = None

            # Auto-start internal SDR if configured
            if self.sdr_manager and getattr(self.config, 'SDR_INTERNAL_AUTOSTART', False):
                try:
                    result = self.sdr_manager.apply_settings()
                    print(f"  [SDR] Autostart: {'OK' if result.get('ok') else result.get('error', 'failed')}")
                except Exception as e:
                    print(f"  [SDR] Autostart failed: {e}")

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
                elif self.path == '/d75':
                    # D75 radio control page
                    html = parent._generate_d75_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/controls':
                    html = parent._generate_controls_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/d75status':
                    # D75 CAT state endpoint
                    data = {'connected': False, 'd75_enabled': False, 'tcp_connected': False,
                            'serial_connected': False, 'btstart_in_progress': False,
                            'service_running': False, 'status_detail': ''}
                    if parent.gateway:
                        data['d75_enabled'] = getattr(parent.gateway.config, 'ENABLE_D75', False)
                        data['d75_mode'] = str(getattr(parent.gateway.config, 'D75_CONNECTION', 'bluetooth')).lower().strip()
                        # Check if d75-cat systemd service is running
                        try:
                            _svc = subprocess.run(['systemctl', 'is-active', 'd75-cat'],
                                                  capture_output=True, text=True, timeout=2)
                            data['service_running'] = _svc.stdout.strip() == 'active'
                        except Exception:
                            data['service_running'] = False
                        if parent.gateway.d75_cat:
                            cat = parent.gateway.d75_cat
                            data.update(cat.get_radio_state())
                            data['d75_enabled'] = True
                            data['tcp_connected'] = cat._connected
                            # If TCP is down, serial status is unknown — show as disconnected
                            data['serial_connected'] = getattr(cat, '_serial_connected', False) if cat._connected else False
                            data['af_gain'] = getattr(cat, '_af_gain', -1)
                        # Build status detail message
                        _has_client = parent.gateway.d75_cat is not None
                        if not data['d75_enabled']:
                            data['status_detail'] = 'D75 disabled in config (ENABLE_D75)'
                        elif not data.get('tcp_connected', False):
                            if _has_client:
                                # Gateway has a client object — poll thread is trying to connect
                                data['status_detail'] = 'Connecting to D75 proxy...'
                            elif not data['service_running']:
                                data['status_detail'] = 'D75 CAT service not running'
                            else:
                                data['status_detail'] = 'Gateway cannot reach D75 CAT server (TCP)'
                        elif not data.get('serial_connected', False):
                            if data.get('btstart_in_progress', False):
                                data['status_detail'] = 'Connecting BT — please wait...'
                            else:
                                data['status_detail'] = 'TCP connected — radio not responding (BT/serial down)'
                        else:
                            data['status_detail'] = ''
                        # Include audio status
                        if parent.gateway.d75_audio_source:
                            data['audio_connected'] = parent.gateway.d75_audio_source.server_connected
                            data['audio_level'] = parent.gateway.d75_audio_source.audio_level
                            data['audio_boost'] = int(parent.gateway.d75_audio_source.audio_boost * 100)
                        else:
                            data['audio_connected'] = False
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(json_mod.dumps(data).encode('utf-8'))
                    except BrokenPipeError:
                        pass
                elif self.path == '/kv4p':
                    # KV4P HT radio control page
                    html = parent._generate_kv4p_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/kv4pstatus':
                    # KV4P status JSON endpoint
                    data = {'connected': False, 'kv4p_enabled': False}
                    if parent.gateway:
                        data['kv4p_enabled'] = getattr(parent.gateway.config, 'ENABLE_KV4P', False)
                        if parent.gateway.kv4p_cat:
                            data.update(parent.gateway.kv4p_cat.get_radio_state())
                        if parent.gateway.kv4p_audio_source:
                            data['audio_connected'] = parent.gateway.kv4p_audio_source.server_connected
                            data['audio_level'] = parent.gateway.kv4p_audio_source.audio_level
                            data['audio_boost'] = int(parent.gateway.kv4p_audio_source.audio_boost * 100)
                        else:
                            data['audio_connected'] = False
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
                elif self.path == '/d75memlist':
                    # D75 memory channel list — scans channels one at a time via !cat ME
                    import json as json_mod
                    channels = []
                    cat = parent.gateway.d75_cat if parent.gateway else None
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
                    # Add SDR audio levels from gateway sources
                    if parent.gateway:
                        try:
                            src = getattr(parent.gateway, 'sdr_source', None)
                            data['audio_level'] = src.audio_level if src and hasattr(src, 'audio_level') else 0
                        except Exception:
                            data['audio_level'] = 0
                        try:
                            src2 = getattr(parent.gateway, 'sdr2_source', None)
                            data['audio_level2'] = src2.audio_level if src2 and hasattr(src2, 'audio_level') else 0
                        except Exception:
                            data['audio_level2'] = 0
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
                elif self.path == '/telegram':
                    html = parent._generate_telegram_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
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
                    # Key PTT on the selected TX radio
                    _gw = parent.gateway
                    _cat_ptt_keyed = False
                    _ws_tx_radio = str(getattr(_gw.config, 'TX_RADIO', 'th9800')).lower() if _gw else 'th9800'
                    if _gw:
                        try:
                            if _ws_tx_radio == 'd75':
                                _gw._ptt_d75(True)
                            elif _ws_tx_radio == 'kv4p':
                                _gw._ptt_kv4p(True)
                            elif _gw.cat_client:
                                _gw.cat_client._send_cmd("!ptt on")
                            _cat_ptt_keyed = True
                            _gw.ptt_active = True
                            _gw._webmic_ptt_active = True
                            _gw.last_sound_time = time.time()
                            print(f"[WS-Mic] PTT keyed via {_ws_tx_radio}")
                        except Exception as _ce:
                            print(f"[WS-Mic] PTT key failed: {_ce}")
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
                        # Unkey PTT on the same radio that was keyed
                        if _gw:
                            _gw._webmic_ptt_active = False
                        if _gw and _cat_ptt_keyed:
                            try:
                                if _ws_tx_radio == 'd75':
                                    _gw._ptt_d75(False)
                                elif _ws_tx_radio == 'kv4p':
                                    _gw._ptt_kv4p(False)
                                elif _gw.cat_client:
                                    _gw.cat_client._send_cmd("!ptt off")
                                _gw.ptt_active = False
                                print(f"[WS-Mic] PTT unkeyed via {_ws_tx_radio}")
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
                elif self.path == '/':
                    # Persistent shell page with PCM player + iframe
                    html = parent._generate_shell_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
                elif self.path == '/dashboard':
                    # Live status dashboard (loaded in iframe)
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
                elif self.path == '/recordings':
                    # Recording manager page
                    html = parent._generate_recordings_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
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
                elif self.path == '/aircraft':
                    # ADS-B aircraft map page (wraps dump1090-fa in iframe)
                    html = parent._generate_aircraft_page()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))
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
                                'sdr2': gw.sdr2_source.duck if gw.sdr2_source and hasattr(gw.sdr2_source, 'duck') else False,
                                'd75': gw.d75_audio_source.duck if gw.d75_audio_source and hasattr(gw.d75_audio_source, 'duck') else False,
                                'kv4p': gw.kv4p_audio_source.duck if gw.kv4p_audio_source and hasattr(gw.kv4p_audio_source, 'duck') else False,
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
                                'd75': int(gw.d75_audio_source.audio_boost * 100) if gw.d75_audio_source and hasattr(gw.d75_audio_source, 'audio_boost') else 100,
                                'kv4p': int(gw.kv4p_audio_source.audio_boost * 100) if gw.kv4p_audio_source and hasattr(gw.kv4p_audio_source, 'audio_boost') else 100,
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
                                'sdr1':     ('sdr_muted', 'sdr_source'),
                                'sdr2':     ('sdr2_muted', 'sdr2_source'),
                                'd75':      ('d75_muted', 'd75_audio_source'),
                                'kv4p':     ('kv4p_muted', 'kv4p_audio_source'),
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
                                'sdr1': 'sdr_source', 'sdr2': 'sdr2_source',
                                'd75': 'd75_audio_source', 'kv4p': 'kv4p_audio_source',
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
                                'd75': 'd75_audio_source',
                                'kv4p': 'kv4p_audio_source',
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
                            import threading as _thr
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
                            import threading
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
                            threading.Thread(target=_do_cw, daemon=True, name="WebCW").start()
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
                            import threading
                            def _do_tts():
                                parent.gateway.speak_text(text, voice=voice)
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
                    # D75 CAT command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        args = data.get('args', '')
                        gw = parent.gateway
                        if cmd == 'start_service':
                            # Start d75-cat systemd service
                            try:
                                _r = subprocess.run(['sudo', 'systemctl', 'start', 'd75-cat'],
                                                    capture_output=True, text=True, timeout=10)
                                if _r.returncode == 0:
                                    result = {'ok': True, 'response': 'D75 CAT service started'}
                                else:
                                    result = {'ok': False, 'error': f'Service start failed: {_r.stderr.strip()}'}
                            except Exception as e:
                                result = {'ok': False, 'error': f'Service start error: {e}'}
                        elif cmd == 'reconnect':
                            # Try to (re)connect gateway to D75 CAT server
                            if gw:
                                try:
                                    d75_host = str(gw.config.D75_HOST)
                                    d75_port = int(gw.config.D75_PORT)
                                    d75_pass = str(gw.config.D75_PASSWORD)
                                    verbose = getattr(gw.config, 'VERBOSE_LOGGING', False)
                                    if gw.d75_cat:
                                        gw.d75_cat.close()
                                    gw.d75_cat = D75CATClient(d75_host, d75_port, d75_pass, verbose=verbose)
                                    if gw.d75_cat.connect():
                                        gw.d75_cat.start_polling()
                                        d75_mode = str(getattr(gw.config, 'D75_CONNECTION', 'bluetooth')).lower().strip()
                                        if d75_mode == 'bluetooth':
                                            if not gw.d75_audio_source:
                                                try:
                                                    gw.d75_audio_source = D75AudioSource(gw.config, gw)
                                                    if gw.d75_audio_source.setup_audio():
                                                        gw.d75_audio_source.enabled = True
                                                        gw.d75_audio_source.duck = gw.config.D75_AUDIO_DUCK
                                                        gw.d75_audio_source.sdr_priority = int(gw.config.D75_AUDIO_PRIORITY)
                                                        gw.mixer.add_source(gw.d75_audio_source)
                                                    else:
                                                        gw.d75_audio_source = None
                                                except Exception:
                                                    gw.d75_audio_source = None
                                            # Poll thread auto-triggers btstart if serial not connected
                                            # Just mark btstart_in_progress so UI shows "Connecting..."
                                            gw.d75_cat._btstart_in_progress = True
                                            gw.d75_cat._bt_stopped = False
                                        result = {'ok': True, 'response': 'Connected — poll thread will start BT link'}
                                    else:
                                        # Keep client with poll thread for auto-retry instead of nulling it
                                        gw.d75_cat.start_polling()
                                        result = {'ok': False, 'error': 'Could not connect to D75 CAT server — poll thread will retry'}
                                except Exception as e:
                                    print(f"  [D75 CAT] Reconnect handler error: {e}")
                                    # Don't null d75_cat — leave for poll thread retry
                                    result = {'ok': False, 'error': f'Reconnect failed: {e}'}
                            else:
                                result = {'ok': False, 'error': 'Gateway not initialized'}
                        elif gw and gw.d75_cat:
                            if cmd == 'cat':
                                resp = gw.d75_cat.send_command(f"!cat {args}")
                                result = {'ok': True, 'response': resp or ''}
                            elif cmd == 'btstart':
                                gw.d75_cat._bt_stopped = False
                                gw.d75_cat._btstart_in_progress = True
                                if not gw.d75_cat._connected:
                                    # TCP is down — poll thread will auto-btstart when it reconnects
                                    result = {'ok': True, 'response': 'TCP down — poll thread will connect and btstart'}
                                else:
                                    resp = gw.d75_cat.send_command("!btstart")
                                    result = {'ok': True, 'response': resp or 'sent (no response)'}
                            elif cmd == 'btstop':
                                # btstop takes several seconds — use longer timeout
                                gw.d75_cat._bt_stopped = True
                                resp = gw.d75_cat.send_command("!btstop", timeout=15.0)
                                gw.d75_cat._serial_connected = False
                                gw.d75_cat._btstart_in_progress = False
                                result = {'ok': True, 'response': resp or ''}
                            elif cmd == 'ptt':
                                resp = gw.d75_cat.send_command("!ptt on" if not getattr(gw, '_d75_ptt', False) else "!ptt off")
                                gw._d75_ptt = not getattr(gw, '_d75_ptt', False)
                                result = {'ok': True, 'response': resp or ''}
                            elif cmd == 'vol':
                                # Set gateway-side audio boost (percentage: 0-500)
                                try:
                                    pct = int(args)
                                    pct = max(0, min(500, pct))
                                    if gw.d75_audio_source:
                                        gw.d75_audio_source.audio_boost = pct / 100.0
                                    result = {'ok': True, 'response': f'boost={pct}%'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'usage: vol 0-500 (percentage)'}
                            else:
                                resp = gw.d75_cat.send_command(f"!{cmd} {args}".strip())
                                result = {'ok': True, 'response': resp or ''}
                        else:
                            result = {'ok': False, 'error': 'D75 not connected — try Start Service then Reconnect'}
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
                elif self.path == '/kv4pcmd':
                    # KV4P HT command endpoint
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8')
                    result = {'ok': False}
                    try:
                        data = json_mod.loads(body)
                        cmd = data.get('cmd', '')
                        args = data.get('args', '')
                        gw = parent.gateway
                        if gw and gw.kv4p_cat:
                            cat = gw.kv4p_cat
                            if cmd == 'freq':
                                try:
                                    freq = float(args)
                                    cat.set_frequency(freq)
                                    result = {'ok': True, 'response': f'Tuned to {freq:.4f} MHz'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'Invalid frequency'}
                            elif cmd == 'txfreq':
                                try:
                                    tx_freq = float(args)
                                    cat.set_frequency(cat._frequency, tx_freq)
                                    result = {'ok': True, 'response': f'TX freq set to {tx_freq:.4f} MHz'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'Invalid frequency'}
                            elif cmd == 'squelch':
                                try:
                                    cat.set_squelch(int(args))
                                    result = {'ok': True, 'response': f'Squelch set to {cat._squelch}'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'Invalid squelch level'}
                            elif cmd == 'ctcss':
                                try:
                                    # DRA818V codes 1-38 (0=none), no 69.3 Hz
                                    _ctcss_hz = ["67.0","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
                                        "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
                                        "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
                                        "173.8","179.9","186.2","192.8","203.5","210.7","218.1","225.7","233.6","241.8","250.3"]
                                    def _hz_to_code(s):
                                        s = str(s).strip()
                                        if s == '0' or s.lower() in ('none', ''):
                                            return 0
                                        try:
                                            # Try as Hz string first (e.g. "103.5")
                                            idx = _ctcss_hz.index(s)
                                            return idx + 1  # 1-based code
                                        except ValueError:
                                            pass
                                        # Try as raw integer code
                                        return int(float(s))
                                    parts = str(args).split()
                                    tx = _hz_to_code(parts[0]) if len(parts) > 0 else 0
                                    rx = _hz_to_code(parts[1]) if len(parts) > 1 else tx
                                    cat.set_ctcss(tx=tx, rx=rx)
                                    result = {'ok': True, 'response': f'CTCSS TX={cat._ctcss_tx} RX={cat._ctcss_rx}'}
                                except (ValueError, TypeError, IndexError):
                                    result = {'ok': False, 'error': 'Invalid CTCSS value'}
                            elif cmd == 'bandwidth':
                                wide = str(args).lower() in ('1', 'wide', 'true')
                                cat.set_bandwidth(wide)
                                result = {'ok': True, 'response': f'Bandwidth: {"wide" if wide else "narrow"}'}
                            elif cmd == 'power':
                                high = str(args).lower() in ('1', 'high', 'true', 'h')
                                cat.set_power(high)
                                result = {'ok': True, 'response': f'Power: {"high" if high else "low"}'}
                            elif cmd == 'ptt':
                                if cat._transmitting:
                                    cat.ptt_off()
                                else:
                                    cat.ptt_on()
                                result = {'ok': True, 'response': f'PTT {"ON" if cat._transmitting else "OFF"}'}
                            elif cmd == 'smeter':
                                enabled = str(args).lower() in ('1', 'true', 'on', '')
                                cat.enable_smeter(enabled)
                                result = {'ok': True, 'response': f'S-meter {"enabled" if enabled else "disabled"}'}
                            elif cmd == 'vol':
                                try:
                                    pct = int(args)
                                    pct = max(0, min(500, pct))
                                    if gw.kv4p_audio_source:
                                        gw.kv4p_audio_source.audio_boost = pct / 100.0
                                    result = {'ok': True, 'response': f'boost={pct}%'}
                                except (ValueError, TypeError):
                                    result = {'ok': False, 'error': 'usage: vol 0-500 (percentage)'}
                            elif cmd == 'testtone':
                                # Toggle a continuous test tone into the audio source.
                                # Runs in a background thread, feeding frames at real-time rate.
                                import math as _math
                                import struct
                                src = gw.kv4p_audio_source
                                if not src:
                                    result = {'ok': False, 'error': 'KV4P audio source not initialized'}
                                elif getattr(gw, '_kv4p_testtone_active', False):
                                    # Stop existing tone
                                    gw._kv4p_testtone_active = False
                                    result = {'ok': True, 'response': 'Test tone stopped'}
                                else:
                                    # Start tone — generate frames matching mixer chunk size
                                    try:
                                        tone_freq = float(args) if args else 400.0
                                    except (ValueError, TypeError):
                                        tone_freq = 400.0
                                    sr = gw.config.AUDIO_RATE
                                    chunk_samples = gw.config.AUDIO_CHUNK_SIZE  # 2400 samples = 50ms
                                    chunk_ms = chunk_samples / sr  # 0.05s
                                    # Pre-generate one second of frames (20 frames at 50ms each)
                                    tone_frames = []
                                    for f_idx in range(20):
                                        offset = f_idx * chunk_samples
                                        tone_frames.append(struct.pack(f'<{chunk_samples}h', *[
                                            int(_math.sin(2 * _math.pi * tone_freq * (offset + i) / sr) * 0.5 * 32767)
                                            for i in range(chunk_samples)
                                        ]))
                                    import threading as _threading_mod
                                    gw._kv4p_testtone_active = True
                                    def _tone_loop():
                                        idx = 0
                                        t0 = time.monotonic()
                                        while getattr(gw, '_kv4p_testtone_active', False):
                                            # Clear any radio audio to prevent mixing
                                            src._chunk_queue.clear()
                                            src._sub_buffer = b''
                                            # Feed tone directly into sub_buffer at exact chunk size
                                            src._sub_buffer = tone_frames[idx % len(tone_frames)]
                                            idx += 1
                                            target = t0 + idx * chunk_ms
                                            now = time.monotonic()
                                            if target > now:
                                                time.sleep(target - now)
                                        gw._kv4p_testtone_active = False
                                    _threading_mod.Thread(target=_tone_loop, daemon=True, name="KV4P-testtone").start()
                                    result = {'ok': True, 'response': f'{tone_freq:.0f}Hz tone started — press again to stop'}
                            elif cmd == 'record':
                                # Record get_audio output to WAV for analysis
                                import wave as _wave
                                import os as _os
                                _rec_src = gw.kv4p_audio_source
                                wav_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'tools', 'kv4p_capture.wav')
                                if getattr(_rec_src, '_recording_file', None):
                                    # Stop recording, finalize WAV
                                    _rec_src._recording_file.close()
                                    raw_path = wav_path + '.raw'
                                    raw_size = _os.path.getsize(raw_path)
                                    with open(raw_path, 'rb') as rf:
                                        raw_data = rf.read()
                                    with _wave.open(wav_path, 'w') as wf:
                                        wf.setnchannels(1)
                                        wf.setsampwidth(2)
                                        wf.setframerate(48000)
                                        wf.writeframes(raw_data)
                                    _os.remove(raw_path)
                                    _rec_src._recording_file = None
                                    dur = len(raw_data) / 96000.0
                                    result = {'ok': True, 'response': f'Saved {dur:.1f}s to {wav_path}'}
                                else:
                                    # Start recording
                                    _os.makedirs(_os.path.dirname(wav_path), exist_ok=True)
                                    raw_path = wav_path + '.raw'
                                    _rec_src._recording_file = open(raw_path, 'wb')
                                    result = {'ok': True, 'response': 'Recording started — send record again to stop'}
                            elif cmd == 'reconnect':
                                try:
                                    cat.close()
                                    kv4p_port = str(gw.config.KV4P_PORT)
                                    verbose = getattr(gw.config, 'VERBOSE_LOGGING', False)
                                    gw.kv4p_cat = KV4PCATClient(kv4p_port, gw.config, verbose=verbose)
                                    if gw.kv4p_cat.connect():
                                        if gw.kv4p_audio_source:
                                            gw.kv4p_cat.on_rx_audio = gw.kv4p_audio_source.on_opus_rx
                                        gw.kv4p_cat.start_polling()
                                        result = {'ok': True, 'response': 'Reconnected'}
                                    else:
                                        gw.kv4p_cat = None
                                        result = {'ok': False, 'error': 'Reconnect failed'}
                                except Exception as e:
                                    result = {'ok': False, 'error': str(e)}
                            else:
                                result = {'ok': False, 'error': f'Unknown command: {cmd}'}
                        elif cmd == 'reconnect':
                            # Reconnect even when kv4p_cat is None
                            try:
                                if gw.kv4p_cat:
                                    try: gw.kv4p_cat.close()
                                    except: pass
                                kv4p_port = str(gw.config.KV4P_PORT)
                                verbose = getattr(gw.config, 'VERBOSE_LOGGING', False)
                                gw.kv4p_cat = KV4PCATClient(kv4p_port, gw.config, verbose=verbose)
                                if gw.kv4p_audio_source:
                                    gw.kv4p_cat.on_rx_audio = gw.kv4p_audio_source.on_opus_rx
                                if gw.kv4p_cat.connect():
                                    gw.kv4p_cat.start_polling()
                                    result = {'ok': True, 'response': 'Reconnected'}
                                else:
                                    gw.kv4p_cat = None
                                    result = {'ok': False, 'error': 'Reconnect failed — check USB'}
                            except Exception as e:
                                gw.kv4p_cat = None
                                result = {'ok': False, 'error': f'Reconnect error: {e}'}
                        else:
                            result = {'ok': False, 'error': 'KV4P not connected — try Reconnect'}
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

                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                form = urllib.parse.parse_qs(body, keep_blank_values=True)
                # Flatten: parse_qs returns lists; for checkboxes with hidden fallback,
                # take the LAST value (checkbox 'true' comes after hidden 'false')
                values = {k: v[-1] for k, v in form.items() if k != '_action'}
                action = form.get('_action', ['save'])[0]

                # Checkboxes: unchecked boxes are absent from form data.
                # We need to detect boolean keys and set them to 'false' if missing.
                for key, default_val in parent._defaults.items():
                    if isinstance(default_val, bool) and key not in values:
                        values[key] = 'false'

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

    def _generate_shell_page(self):
        """Build the persistent shell page with PCM player + nav + iframe."""
        t = self._get_theme()
        gw_name = str(getattr(self.config, 'GATEWAY_NAME', '') or '').strip()
        _title_prefix = f'{gw_name} - ' if gw_name else ''
        has_radio = getattr(self.config, 'ENABLE_CAT_CONTROL', False) or getattr(self.config, 'ENABLE_TH9800', False)
        has_d75 = getattr(self.config, 'ENABLE_D75', False)
        has_kv4p = getattr(self.config, 'ENABLE_KV4P', False)
        has_adsb = getattr(self.config, 'ENABLE_ADSB', False)
        has_telegram = getattr(self.config, 'ENABLE_TELEGRAM', False)
        _radio_link = '<a href="/radio" target="content" onclick="setActive(this)">TH-9800</a>' if has_radio else '<a class="nav-disabled">TH-9800</a>'
        _d75_link = '<a href="/d75" target="content" onclick="setActive(this)">TH-D75</a>' if has_d75 else '<a class="nav-disabled">TH-D75</a>'
        _kv4p_link = '<a href="/kv4p" target="content" onclick="setActive(this)">KV4P</a>' if has_kv4p else '<a class="nav-disabled">KV4P</a>'
        _adsb_link = '<a href="/aircraft" target="content" onclick="setActive(this)">ADS-B</a>' if has_adsb else ''
        _telegram_link = '<a href="/telegram" target="content" onclick="setActive(this)">Telegram</a>' if has_telegram else ''
        return f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_title_prefix}Radio Gateway</title>
<style>
  :root {{
    --t-bg: {t['bg']}; --t-panel: {t['panel']}; --t-border: {t['border']};
    --t-accent: {t['accent']}; --t-btn: {t['btn']}; --t-btn-border: {t['btn_border']};
    --t-btn-hover: {t['btn_hover']}; --t-btn-active: {t['btn_active_bg']};
    --t-checkbox: {t['checkbox']};
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
         background: var(--t-bg); color: #e0e0e0; display: flex; flex-direction: column; height: 100vh; }}
  #shell-bar {{
    background: var(--t-panel); border-bottom: 1px solid var(--t-border);
    padding: 6px 14px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    flex-shrink: 0; min-height: 40px; z-index: 100;
  }}
  #shell-bar a {{ color: var(--t-accent); text-decoration: none; font-size: 0.9em; }}
  #shell-bar a:hover {{ text-decoration: underline; }}
  #shell-bar a.active {{ font-weight: bold; border-bottom: 2px solid var(--t-accent); }}
  #shell-bar a.nav-disabled {{ color: #555; cursor: default; pointer-events: none; text-decoration: none; }}
  .shell-nav {{ display: flex; flex-wrap: wrap; gap: 0; align-items: center; }}
  .shell-nav a {{ padding: 3px 10px; }}
  .shell-nav a + a {{ border-left: 1px solid #444; }}
  .shell-pcm {{ display: inline-flex; gap: 4px; align-items: center; padding-left: 6px; border-left: 1px solid #444; margin-left: 2px; }}
  .shell-pcm button {{
    background: var(--t-btn); border: 1px solid var(--t-btn-border); color: #e0e0e0;
    border-radius: 4px; padding: 3px 8px; cursor: pointer; font-size: 0.85em;
  }}
  .shell-pcm button:hover {{ background: var(--t-btn-hover); }}
  #shell-frame {{ flex: 1; border: none; width: 100%; }}
  #shell-bars {{
    background: var(--t-panel); border-bottom: 1px solid var(--t-border);
    padding: 2px 14px; display: flex; flex-wrap: wrap; gap: 2px 14px;
    align-items: center; flex-shrink: 0; font-family: monospace; font-size: 0.8em;
    min-height: 20px;
  }}
  #shell-bars .sb {{ display: flex; gap: 4px; align-items: center; white-space: nowrap; width: 190px; }}
  #shell-bars .sb-label {{ color: #888; width: 3.2em; text-align: right; flex-shrink: 0; }}
  #shell-bars .sb-pct {{ width: 3.2em; text-align: right; flex-shrink: 0; }}
  #shell-bars .sb-track {{ width: 100px; height: 10px; background: rgba(255,255,255,0.05); border-radius: 2px; flex-shrink: 0; overflow: hidden; }}
  #shell-bars .sb-bar {{ display: block; height: 100%; border-radius: 2px; }}
  .sb-rx {{ background: #2ecc71; }} .sb-tx {{ background: #e74c3c; }}
  .sb-sdr1 {{ background: var(--t-accent); }} .sb-sdr2 {{ background: #e056a0; }}
  .sb-sv {{ background: #f1c40f; }} .sb-cl {{ background: #2ecc71; }}
  .sb-sp {{ background: var(--t-accent); }} .sb-an {{ background: #e74c3c; }}
  .sb-d75 {{ background: #f39c12; }} .sb-kv4p {{ background: #1abc9c; }}
  #shell-status {{ color: #888; font-size: 0.75em; white-space: nowrap; }}
</style>
</head><body>
<div id="shell-bar">
  <div class="shell-nav">
    <a href="/dashboard" target="content" onclick="setActive(this)">Dashboard</a><a href="/controls" target="content" onclick="setActive(this)">Controls</a>{_radio_link}{_d75_link}{_kv4p_link}<a href="/sdr" target="content" onclick="setActive(this)">SDR</a>{_adsb_link}{_telegram_link}<a href="/recordings" target="content" onclick="setActive(this)">Recordings</a><a href="/config" target="content" onclick="setActive(this)">Config</a><a href="/logs" target="content" onclick="setActive(this)">Logs</a>
    <span class="shell-pcm">
      <button id="play-btn" onclick="toggleStream()" style="min-width:52px; text-align:center;">&#9654; MP3</button>
      <input id="vol-slider" type="range" min="0" max="100" value="100" style="width:40px; accent-color:var(--t-accent);" oninput="setVolume(this.value)">
      <span id="stream-indicator" style="display:none; width:8px; height:8px; border-radius:50%; background:var(--t-accent); box-shadow:0 0 6px var(--t-accent);"></span>
      <span id="stream-status" style="font-size:0.75em;"></span>
      <span style="color:var(--t-border);">|</span>
      <button id="ws-btn" onclick="toggleWS()" style="min-width:52px; text-align:center;">&#9654; PCM</button>
      <input id="ws-vol" type="range" min="0" max="100" value="100" style="width:40px; accent-color:var(--t-accent);" oninput="setWSVol(this.value)">
      <span id="ws-indicator" style="display:none; width:8px; height:8px; border-radius:50%; background:var(--t-accent); box-shadow:0 0 6px var(--t-accent);"></span>
      <span id="ws-status" style="font-size:0.75em;"></span>
    </span>
  </div>
</div>
<div id="shell-bars"></div>
<iframe id="shell-frame" name="content" src="/dashboard"></iframe>
<script>
var _T = {{accent:'{t['accent']}',btnBorder:'{t['btn_border']}'}};

function setActive(el) {{
  document.querySelectorAll('.shell-nav a').forEach(function(a){{ a.classList.remove('active'); }});
  el.classList.add('active');
}}
function shellCmd(key) {{
  fetch('/key', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{key:key}})}});
}}

// --- MP3 stream player ---
var _audio = null;
var _playing = false;
var _streamTimer = null;
var _streamStart = 0;
function toggleStream() {{
  if (_playing) {{ stopStream(); }} else {{ startStream(); }}
}}
function startStream() {{
  if (_audio) {{ try {{ _audio.onplaying = null; _audio.onerror = null; _audio.onended = null; _audio.pause(); _audio.src = ''; }} catch(e){{}} _audio = null; }}
  if (_streamTimer) {{ clearInterval(_streamTimer); _streamTimer = null; }}

  var btn = document.getElementById('play-btn');
  var ind = document.getElementById('stream-indicator');
  var st = document.getElementById('stream-status');
  _playing = true;
  btn.innerHTML = '&#9724; MP3'; btn.style.color = '#f39c12'; btn.style.borderColor = '#f39c12';
  st.innerHTML = '<span style="color:#f39c12">Buffering...</span>';

  _audio = new Audio('/stream');
  _audio.volume = document.getElementById('vol-slider').value / 100;

  _audio.onplaying = function() {{
    _playing = true;
    _streamStart = Date.now();
    btn.innerHTML = '&#9724; MP3'; btn.style.color = _T.accent; btn.style.borderColor = _T.accent;
    ind.style.display = 'inline-block';
    st.innerHTML = '<span style="color:' + _T.accent + '">0:00</span>';
    _streamTimer = setInterval(function() {{
      var s = Math.floor((Date.now() - _streamStart) / 1000);
      st.innerHTML = '<span style="color:' + _T.accent + '">' + Math.floor(s/60) + ':' + (s%60<10?'0':'') + s%60 + '</span>';
    }}, 1000);
  }};

  _audio.onerror = function() {{
    st.innerHTML = '<span style="color:#e74c3c">Stream error</span>';
    stopStream();
  }};

  _audio.onended = function() {{ stopStream(); }};

  _audio.play().catch(function(e) {{
    st.innerHTML = '<span style="color:#e74c3c">' + e.message + '</span>';
    stopStream();
  }});
}}
function stopStream() {{
  if (_streamTimer) {{ clearInterval(_streamTimer); _streamTimer = null; }}
  if (_audio) {{ try {{ _audio.pause(); _audio.src = ''; }} catch(e){{}} _audio = null; }}
  _playing = false;
  document.getElementById('play-btn').innerHTML = '&#9654; MP3';
  document.getElementById('play-btn').style.color = '#e0e0e0';
  document.getElementById('play-btn').style.borderColor = _T.btnBorder;
  document.getElementById('stream-indicator').style.display = 'none';
  document.getElementById('stream-status').innerHTML = '';
}}
function setVolume(v) {{
  if (_audio) _audio.volume = v / 100;
}}

// Highlight nav on iframe load
document.getElementById('shell-frame').addEventListener('load', function() {{
  try {{
    var path = this.contentWindow.location.pathname;
    document.querySelectorAll('.shell-nav a').forEach(function(a) {{
      a.classList.toggle('active', a.getAttribute('href') === path);
    }});
  }} catch(e) {{}}
}});

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

function toggleWS() {{
  if (_wsPlaying) {{ stopWS(); }} else {{ startWS(); }}
}}

function startWS() {{
  var btn = document.getElementById('ws-btn');
  var ind = document.getElementById('ws-indicator');
  var st = document.getElementById('ws-status');
  btn.innerHTML = '&#9724; PCM';
  btn.style.color = '#f39c12';
  btn.style.borderColor = '#f39c12';
  st.innerHTML = '<span style="color:#f39c12">Connecting...</span>';

  if (_wsCtx) {{ stopWS(); return; }}

  try {{
    _wsCtx = new (window.AudioContext || window.webkitAudioContext)({{sampleRate: 48000}});
  }} catch(e) {{
    st.innerHTML = '<span style="color:#e74c3c">No AudioContext</span>';
    btn.innerHTML = '&#9654; PCM'; btn.style.color = '#e0e0e0';
    return;
  }}
  if (_wsCtx.state === 'suspended') {{ _wsCtx.resume(); }}

  _wsGain = _wsCtx.createGain();
  _wsGain.gain.value = document.getElementById('ws-vol').value / 100;
  _wsGain.connect(_wsCtx.destination);

  var _pcmBuf = [];
  var _pcmPos = 0;
  var _pcmTotal = 0;
  var _pcmReady = false;

  function _drainPCM(output) {{
    var w = 0, need = output.length;
    if (!_pcmReady) {{
      if (_pcmTotal < 2400) {{ for (w = 0; w < need; w++) output[w] = 0; return; }}
      _pcmReady = true;
    }}
    while (_pcmTotal > 9600 && _pcmBuf.length > 1) {{ _pcmTotal -= _pcmBuf[0].length; _pcmBuf.shift(); _pcmPos = 0; }}
    while (w < need) {{
      if (!_pcmBuf.length) {{ for (; w < need; w++) output[w] = 0; break; }}
      var cur = _pcmBuf[0], avail = cur.length - _pcmPos;
      var take = Math.min(avail, need - w);
      for (var j = 0; j < take; j++) output[w++] = cur[_pcmPos++];
      if (_pcmPos >= cur.length) {{ _pcmTotal -= cur.length; _pcmBuf.shift(); _pcmPos = 0; }}
    }}
  }}

  function _connectWS() {{
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _ws = new WebSocket(proto + '//' + location.host + '/ws_audio');
    _ws.binaryType = 'arraybuffer';
    _ws.onopen = function() {{
      _wsPlaying = true;
      _wsStart = Date.now();
      _wsBytes = 0;
      if (navigator.wakeLock) {{ navigator.wakeLock.request('screen').then(function(wl) {{ _wakeLock = wl; }}).catch(function(){{}}); }}
      btn.innerHTML = '&#9724; PCM';
      btn.style.color = _T.accent;
      btn.style.borderColor = _T.accent;
      ind.style.display = 'inline-block';
      st.innerHTML = '<span style="color:' + _T.accent + '">0:00</span>';
      _wsTimer = setInterval(function() {{
        var secs = Math.floor((Date.now() - _wsStart) / 1000);
        var m = Math.floor(secs / 60);
        var s = secs % 60;
        var t = m + ':' + (s < 10 ? '0' : '') + s;
        var kb = (_wsBytes / 1024).toFixed(0);
        var unit = 'KB';
        if (_wsBytes >= 1048576) {{ kb = (_wsBytes / 1048576).toFixed(1); unit = 'MB'; }}
        st.innerHTML = '<span style="color:' + _T.accent + '">' + t + '</span> <span style="color:#666">' + kb + unit + '</span>';
      }}, 1000);
    }};

    var _srcRate = 48000;
    var _dstRate = _wsCtx.sampleRate;
    var _resample = (_dstRate !== _srcRate);

    _ws.onmessage = function(ev) {{
      if (ev.data instanceof ArrayBuffer) {{
        _wsBytes += ev.data.byteLength;
        var int16 = new Int16Array(ev.data);
        var float32 = new Float32Array(int16.length);
        for (var i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768.0;
        if (_resample) {{
          var ratio = _srcRate / _dstRate;
          var outLen = Math.round(float32.length / ratio);
          var resampled = new Float32Array(outLen);
          for (var i = 0; i < outLen; i++) {{
            var srcIdx = i * ratio;
            var idx0 = Math.floor(srcIdx);
            var frac = srcIdx - idx0;
            var s0 = float32[idx0] || 0;
            var s1 = float32[Math.min(idx0 + 1, float32.length - 1)] || 0;
            resampled[i] = s0 + frac * (s1 - s0);
          }}
          float32 = resampled;
        }}
        if (_wsWorklet && _wsWorklet.port) {{
          _wsWorklet.port.postMessage(float32);
        }} else {{
          _pcmBuf.push(float32);
          _pcmTotal += float32.length;
          while (_pcmBuf.length > 4) {{ _pcmTotal -= _pcmBuf[0].length; _pcmBuf.shift(); _pcmPos = 0; }}
        }}
      }}
    }};
    _ws.onerror = function() {{ stopWS(); }};
    _ws.onclose = function() {{ if (_wsPlaying) stopWS(); }};
  }}

  if (_wsCtx.audioWorklet) {{
    var workletCode = 'class P extends AudioWorkletProcessor{{constructor(){{super();this.b=[];this.p=0;this.tot=0;this.ready=false;this.port.onmessage=e=>{{this.b.push(e.data);this.tot+=e.data.length;if(!this.ready&&this.tot>=2400)this.ready=true;if(this.tot>9600){{while(this.tot>2400&&this.b.length>1){{this.tot-=this.b[0].length;this.b.shift()}}this.p=0}}}}}}process(i,o){{var c=o[0][0];if(!c)return true;var n=c.length,w=0;if(!this.ready){{for(w=0;w<n;w++)c[w]=0;return true}}while(w<n){{if(!this.b.length){{for(;w<n;w++)c[w]=0;break}}var r=this.b[0],a=r.length-this.p,t=Math.min(a,n-w);for(var j=0;j<t;j++)c[w++]=r[this.p++];if(this.p>=r.length){{this.b.shift();this.p=0;this.tot-=r.length}}}}return true}}}}registerProcessor("p",P)';
    var blob = new Blob([workletCode], {{type: 'application/javascript'}});
    var blobURL = URL.createObjectURL(blob);
    _wsCtx.audioWorklet.addModule(blobURL).then(function() {{
      URL.revokeObjectURL(blobURL);
      _wsWorklet = new AudioWorkletNode(_wsCtx, 'p', {{outputChannelCount:[1], numberOfOutputs:1}});
      _wsWorklet.connect(_wsGain);
      _connectWS();
    }}).catch(function(e) {{
      URL.revokeObjectURL(blobURL);
      var sp = _wsCtx.createScriptProcessor(2048, 0, 1);
      sp.onaudioprocess = function(ev) {{ _drainPCM(ev.outputBuffer.getChannelData(0)); }};
      sp.connect(_wsGain);
      _wsWorklet = sp;
      _connectWS();
    }});
  }} else {{
    var sp = _wsCtx.createScriptProcessor(2048, 0, 1);
    sp.onaudioprocess = function(ev) {{ _drainPCM(ev.outputBuffer.getChannelData(0)); }};
    sp.connect(_wsGain);
    _wsWorklet = sp;
    _connectWS();
  }}
}}

function stopWS() {{
  if (_wsTimer) {{ clearInterval(_wsTimer); _wsTimer = null; }}
  if (_ws) {{ try {{ _ws.close(); }} catch(e){{}} _ws = null; }}
  if (_wsWorklet) {{ try {{ _wsWorklet.disconnect(); }} catch(e){{}} _wsWorklet = null; }}
  if (_wsGain) {{ try {{ _wsGain.disconnect(); }} catch(e){{}} _wsGain = null; }}
  if (_wsCtx) {{ try {{ _wsCtx.close(); }} catch(e){{}} _wsCtx = null; }}
  _wsPlaying = false;
  if (_wakeLock) {{ try {{ _wakeLock.release(); }} catch(e){{}} _wakeLock = null; }}
  document.getElementById('ws-btn').innerHTML = '&#9654; PCM';
  document.getElementById('ws-btn').style.color = '#e0e0e0';
  document.getElementById('ws-btn').style.borderColor = _T.btnBorder;
  document.getElementById('ws-indicator').style.display = 'none';
  document.getElementById('ws-status').innerHTML = '';
}}

function setWSVol(v) {{
  if (_wsGain) _wsGain.gain.value = v / 100;
}}

// --- Audio level bars (always visible) ---
function _sbBar(pct, cls, ducked) {{
  var w = Math.round(Math.min(Math.max(pct, 0), 100));
  var col = ducked ? '#e74c3c' : '#2ecc71';
  return '<span class="sb-pct" style="color:'+col+'">'+pct+'%</span><span class="sb-track"><span class="sb-bar '+cls+'" style="width:'+w+'%"></span></span>';
}}
var _sbBusy = false;
function _updateBars() {{
  if (_sbBusy) return;
  _sbBusy = true;
  fetch('/status').then(function(r){{return r.json()}}).then(function(s){{
    var h = '';
    h += '<div class="sb"><span class="sb-label">RX:</span>'+_sbBar(s.radio_rx,'sb-rx')+'</div>';
    h += '<div class="sb"><span class="sb-label">TX:</span>'+_sbBar(s.radio_tx,'sb-tx')+'</div>';
    if(s.kv4p_enabled) h += '<div class="sb"><span class="sb-label">KV4P:</span>'+_sbBar(s.kv4p_level,'sb-kv4p')+(s.kv4p_muted?' <span style="color:#e74c3c;font-weight:bold;">M</span>':'')+'</div>';
    if(s.d75_enabled) h += '<div class="sb"><span class="sb-label">D75:</span>'+_sbBar(s.d75_level,'sb-d75')+(s.d75_muted?' <span style="color:#e74c3c;font-weight:bold;">M</span>':'')+'</div>';
    if(s.sdr1_enabled) h += '<div class="sb"><span class="sb-label">SDR1:</span>'+_sbBar(s.sdr1_level,'sb-sdr1',s.sdr1_ducked)+'</div>';
    if(s.sdr2_enabled) h += '<div class="sb"><span class="sb-label">SDR2:</span>'+_sbBar(s.sdr2_level,'sb-sdr2',s.sdr2_ducked)+'</div>';
    if(s.remote_enabled) h += '<div class="sb"><span class="sb-label">'+s.remote_mode+':</span>'+_sbBar(s.remote_level, s.remote_mode==='SV'?'sb-sv':'sb-cl', s.remote_mode==='CL'&&s.cl_ducked)+'</div>';
    if(s.announce_enabled) h += '<div class="sb"><span class="sb-label">AN:</span>'+_sbBar(s.an_level,'sb-an')+'</div>';
    if(s.speaker_enabled) h += '<div class="sb"><span class="sb-label">SP:</span>'+_sbBar(s.speaker_level,'sb-sp')+'</div>';
    document.getElementById('shell-bars').innerHTML = h;
  }}).catch(function(){{}}).finally(function(){{_sbBusy=false}});
}}
setInterval(_updateBars, 1000);
_updateBars();
</script>
</body></html>'''

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

    def _generate_logs_page(self):
        """Build the live log viewer HTML page."""
        body = '''
<h2 style="margin:0 0 10px;">Gateway Logs</h2>
<div style="margin-bottom:8px; display:flex; gap:10px; align-items:center;">
  <label style="color:#888; font-size:0.85em;">
    <input type="checkbox" id="auto-scroll" checked> Auto-scroll
  </label>
  <label style="color:#888; font-size:0.85em;">
    Filter: <input type="text" id="log-filter" placeholder="regex..." style="background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:3px; padding:2px 6px; width:200px; font-size:0.85em;">
  </label>
  <button class="rb rb-sm" onclick="clearLog()">Clear</button>
  <button class="rb rb-sm" id="btn-trace" onclick="toggleTrace('audio')">Audio Trace</button>
  <button class="rb rb-sm" id="btn-watchdog" onclick="toggleTrace('watchdog')">Watchdog Trace</button>
  <button class="rb rb-sm" id="btn-reboot" onclick="rebootHost()" style="background:#7f0000; border-color:#c0392b; color:#ffcccc; margin-left:auto;">Reboot Host</button>
  <span id="log-status" style="color:#888; font-size:0.8em;"></span>
</div>
<div id="log-box" style="background:#0a0a0a; border:1px solid var(--t-btn-border); border-radius:4px; padding:8px; height:calc(100vh - 160px); overflow-y:auto; font-family:'Courier New',monospace; font-size:0.82em; line-height:1.5; white-space:pre-wrap; word-break:break-all; color:#c0c0c0;">
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
        btn.style.background = _T.btnActive;
        btn.style.borderColor = _T.accent;
        btn.style.color = _T.accent;
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
    if (d.audio_trace) { ab.style.background=_T.btnActive; ab.style.borderColor=_T.accent; ab.style.color=_T.accent; }
    if (d.watchdog_trace) { wb.style.background=_T.btnActive; wb.style.borderColor=_T.accent; wb.style.color=_T.accent; }
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

function rebootHost() {
  if (!confirm('Reboot the host machine?')) return;
  var btn = document.getElementById('btn-reboot');
  btn.disabled = true;
  btn.textContent = 'Rebooting…';
  fetch('/reboothost', {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (d.ok) {
        btn.textContent = 'Rebooting…';
        document.getElementById('log-status').textContent = 'Host reboot initiated';
      } else {
        btn.disabled = false;
        btn.textContent = 'Reboot Host';
        alert('Reboot failed: ' + (d.error || 'unknown error'));
      }
    })
    .catch(function() {
      btn.textContent = 'Rebooting…';
      document.getElementById('log-status').textContent = 'Host reboot initiated';
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
<h1 style="font-size:1.8em">TH-9800 Control</h1>

<div id="cat-offline" style="display:none; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; margin-bottom:14px;">
  <span id="cat-offline-msg" style="color:#e74c3c; font-weight:bold;">CAT not connected</span>
  <button id="cat-connect-btn" onclick="catConnect()" class="rb" style="margin-left:14px;">Connect</button>
  <span id="cat-connect-status" style="color:#888; margin-left:10px; font-size:0.9em;"></span>
</div>

<div id="radio-panel" style="display:none;">

<!-- Connection + RTS Control -->
<div style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <span style="color:#888; font-size:0.85em;">TCP:</span>
  <button onclick="catDisconnect()" class="rb rb-sm" style="background:#c0392b; border-color:#e74c3c;">Disconnect</button>
  <button onclick="catReconnect()" class="rb rb-sm">Reconnect</button>
  <span style="color:#333;">|</span>
  <span style="color:#888; font-size:0.85em;">Serial:</span>
  <span id="serial-state" style="font-size:0.85em; color:#888; display:inline-block; width:110px; text-align:center;">—</span>
  <button onclick="serialDisconnect()" class="rb rb-sm" style="background:#c0392b; border-color:#e74c3c;">Disconnect</button>
  <button onclick="serialConnect()" class="rb rb-sm">Connect</button>
  <button onclick="setupRadio()" class="rb rb-sm" style="background:#2c3e50; border-color:#34495e;">Setup</button>
  <button id="radio-power-btn" onclick="radioPower()" class="rb rb-sm" style="background:#8e44ad; border-color:#9b59b6; display:''' + ('inline-block' if self.gateway and self.gateway.relay_radio else 'none') + ''';">Radio Power</button>
  <span style="color:#333;">|</span>
  <span style="color:#888; font-size:0.85em;">RTS TX:</span>
  <span id="rts-state" style="font-weight:bold;">—</span>
  <button onclick="catCmd('TOGGLE_RTS')" class="rb rb-sm">Toggle RTS</button>
</div>

<!-- Two-column VFO display -->
<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(400px, 1fr)); gap:14px; margin-bottom:14px;">

  <!-- LEFT VFO -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="color:var(--t-accent); font-weight:bold; font-size:1.1em;">LEFT VFO</span>
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
    <div id="l-freq-box" style="background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:4px; padding:10px; margin-bottom:10px; font-family:monospace; transition:background 0.2s, border-color 0.2s;">
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
        <div style="flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:3px; height:18px; position:relative; overflow:hidden;">
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
        <input id="l-vol" type="range" min="0" max="100" value="25" style="flex:1; accent-color:var(--t-accent);" oninput="catVol('LEFT',this.value)">
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="color:var(--t-accent); font-weight:bold; font-size:1.1em;">RIGHT VFO</span>
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
    <div id="r-freq-box" style="background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:4px; padding:10px; margin-bottom:10px; font-family:monospace; transition:background 0.2s, border-color 0.2s;">
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
        <div style="flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:3px; height:18px; position:relative; overflow:hidden;">
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
        <input id="r-vol" type="range" min="0" max="100" value="25" style="flex:1; accent-color:var(--t-accent);" oninput="catVol('RIGHT',this.value)">
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:8px;">Menu / SET</div>
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:8px;">PTT</div>
    <div style="display:flex; gap:18px; align-items:flex-start; justify-content:center;">
      <div style="display:flex; flex-direction:column; align-items:center;">
        <div style="height:10px;"></div>
        <button id="ptt-btn" class="rb" style="width:80px; height:80px; font-size:1.4em; font-weight:bold; border-radius:50%;"
          onclick="togglePTT()">PTT</button>
        <span style="color:#888; font-size:0.75em; margin-top:6px;">Toggle TX</span>
      </div>
      <div style="display:flex; flex-direction:column; align-items:center;">
        <div id="mic-level" style="width:80px; height:6px; background:var(--t-btn); border-radius:3px; margin-bottom:4px; overflow:hidden;">
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:8px;">Hyper Memories</div>
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:8px;">Mic Keypad</div>
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
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:8px;">Mic Controls</div>
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
  .rb { padding:8px 14px; border:1px solid var(--t-btn-border); border-radius:4px; background:var(--t-btn);
        color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.95em; min-width:44px; }
  .rb:hover { background:var(--t-btn-hover); border-color:var(--t-accent); }
  .rb:active { background:var(--t-border); }
  .rb-sm { font-size:0.8em; padding:5px 10px; }
  .icon-off { padding:2px 6px; border-radius:3px; background:var(--t-btn); color:#555; border:1px solid var(--t-btn-border); }
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

function radioPower() {
  fetch('/key', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:'j'})});
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
    btn.style.background = _T.btn;
    btn.style.borderColor = _T.btnBorder;
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

var _radioBusy = false;
function updateRadio() {
  if (_radioBusy) return;
  _radioBusy = true;
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
    else { lBox.style.background = _T.btn; lBox.style.borderColor = _T.btnBorder; }

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
    else { rBox.style.background = _T.btn; rBox.style.borderColor = _T.btnBorder; }

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

  }).catch(function(){}).finally(function(){ _radioBusy=false; });
}

setInterval(updateRadio, 1000);
updateRadio();
</script>
'''
        return self._wrap_html('TH-9800 Control', body)

    def _generate_d75_page(self):
        """Build the D75 radio control HTML page."""
        modes = {0: 'FM', 1: 'DV', 2: 'AM', 3: 'LSB', 4: 'USB', 5: 'CW', 6: 'NFM', 7: 'DR', 8: 'WFM', 9: 'R-CW'}
        powers = {0: 'High', 1: 'Med', 2: 'Low', 3: 'EL'}
        ctcss_tones = ["67.0","69.3","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
            "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
            "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
            "173.8","179.9","186.2","192.8","203.5","206.5","210.7","218.1","225.7",
            "229.1","233.6","241.8","250.3","254.1"]
        dcs_tones = ["023","025","026","031","032","036","043","047","051","053","054",
            "065","071","072","073","074","114","115","116","122","125","131","132","134",
            "143","145","152","155","156","162","165","172","174","205","212","223","225",
            "226","243","244","245","246","251","252","255","261","263","265","266","271",
            "274","306","311","315","325","331","332","343","346","351","356","364","365",
            "371","411","412","413","423","431","432","445","446","452","454","455","462",
            "464","465","466","503","506","516","523","526","532","546","565","606","612",
            "624","627","631","632","654","662","664","703","712","723","731","732","734","743","754"]
        ctcss_opts = ''.join(f'<option value="{t}">{t} Hz</option>' for t in ctcss_tones)
        dcs_opts = ''.join(f'<option value="{t}">{t}</option>' for t in dcs_tones)
        body = '''
<h1 style="font-size:1.8em">TH-D75 Control</h1>

<style>
.rb { padding:8px 14px; border:1px solid var(--t-btn-border); border-radius:4px; background:var(--t-btn);
  color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.95em; min-width:44px; }
.rb:hover { background:var(--t-btn-hover); border-color:var(--t-accent); }
.rb:active { background:var(--t-border); }
.rb-sm { font-size:0.8em; padding:5px 10px; }
.rb-active { background:var(--t-accent) !important; color:#fff !important; border-color:var(--t-accent) !important; }
.d75-band { background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; }
.d75-freq { color:#2ecc71; font-size:2.2em; text-align:center; letter-spacing:2px; margin:8px 0; font-family:monospace; }
.mem-row { cursor:pointer; }
.mem-row:hover { background:rgba(255,255,255,0.05); }
.d75-label { color:#888; font-size:0.85em; }
.d75-val { color:#e0e0e0; font-family:monospace; }
.d75-row { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
.d75-meter { flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:3px; height:18px; position:relative; overflow:hidden; }
.d75-meter-fill { height:100%; transition:width 0.3s; }
.d75-meter-text { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); font-size:0.75em; color:#fff; font-weight:bold; }
.d75-input { background:var(--t-btn); border:1px solid var(--t-btn-border); color:#2ecc71; padding:6px 10px;
  border-radius:4px; font-family:monospace; font-size:1.1em; width:140px; text-align:center; }
.d75-select { background:var(--t-btn); border:1px solid var(--t-btn-border); color:#e0e0e0; padding:5px 8px;
  border-radius:3px; font-family:monospace; font-size:0.9em; }
</style>

<div id="d75-offline" style="display:none; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; margin-bottom:14px;">
  <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
    <span style="color:#e74c3c; font-weight:bold;">D75 Not Connected</span>
    <span id="d75-status-detail" style="color:#f39c12; font-size:0.9em;"></span>
  </div>
  <div id="d75-status-steps" style="margin-top:10px; font-size:0.85em; font-family:monospace;">
    <div><span id="d75-chk-tcp" style="color:#888;">&#x25cf;</span> Gateway TCP Link: <span id="d75-chk-tcp-text">checking...</span>
      <button id="d75-reconnect-btn" class="rb rb-sm" onclick="d75svcAction('reconnect')" style="display:none; margin-left:8px;">Reconnect</button></div>
    <div><span id="d75-chk-serial" style="color:#888;">&#x25cf;</span> Radio BT Serial: <span id="d75-chk-serial-text">checking...</span>
      <button id="d75-offline-btstart-btn" class="rb rb-sm" onclick="d75svcAction('btstart_via_reconnect')" style="display:none; margin-left:8px;">BT Start</button></div>
    <div><span id="d75-chk-svc" style="color:#888;">&#x25cf;</span> Local CAT Service: <span id="d75-chk-svc-text">checking...</span>
      <button id="d75-start-svc-btn" class="rb rb-sm" onclick="d75svcAction('start_service')" style="display:none; margin-left:8px;">Start Service</button></div>
  </div>
</div>

<div id="d75-panel" style="display:none;">

<!-- Status bar -->
<div style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px; display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
  <span class="d75-label">Model:</span> <span id="d75-model" class="d75-val">—</span>
  <span style="color:#333;">|</span>
  <span class="d75-label">S/N:</span> <span id="d75-sn" class="d75-val">—</span>
  <span style="color:#333;">|</span>
  <span class="d75-label">FW:</span> <span id="d75-fw" class="d75-val">—</span>
  <span style="color:#333;">|</span>
  <span class="d75-label">Mode:</span> <span id="d75-mode" class="d75-val">—</span>
  <span id="d75-audio-row"><span style="color:#333;">|</span>
  <span class="d75-label">Audio:</span> <span id="d75-audio-status" class="d75-val">—</span></span>
  <span style="flex:1;"></span>
  <span id="d75-tx-badge" style="display:none; background:#c0392b; color:#fff; padding:2px 8px; border-radius:3px; font-weight:bold;">TX</span>
  <span style="flex:1;"></span>
  <span id="d75-feedback" style="font-family:monospace; font-size:0.85em; min-width:120px;"></span>
  <button id="d75-btstart-btn" class="rb rb-sm" onclick="d75cmd('btstart')" style="display:none;">BT Start</button>
  <button id="d75-btstop-btn" class="rb rb-sm" onclick="d75cmd('btstop')" style="display:none;">BT Stop</button>
  <button id="d75-audio-off-btn" class="rb rb-sm" onclick="d75cmd('audio','disconnect')" style="display:none;">Audio Off</button>
  <button id="d75-audio-on-btn" class="rb rb-sm" onclick="d75cmd('audio','connect')" style="display:none;">Audio On</button>
  <button class="rb rb-sm" onclick="d75cmd('ptt')" id="d75-ptt-btn">PTT</button>
</div>

<!-- Radio Controls Row -->
<div style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <span class="d75-label">Band:</span>
  <select id="d75-active-band" class="d75-select" onfocus="_ctrlEditUntil=Date.now()+5000" onchange="_ctrlEditUntil=Date.now()+3000;d75cmd('cat','BC '+this.value)">
    <option value="0">A</option><option value="1">B</option>
  </select>
  <span class="d75-label">Dual:</span>
  <select id="d75-dual" class="d75-select" onfocus="_ctrlEditUntil=Date.now()+5000" onchange="_ctrlEditUntil=Date.now()+3000;d75cmd('cat','DL '+this.value)">
    <option value="0">Dual</option><option value="1">Single</option>
  </select>
  <span style="color:#333;">|</span>
  <button class="rb rb-sm" onclick="d75cmd('cat','UP')">Up</button>
  <button class="rb rb-sm" onclick="d75cmd('cat','DN')">Down</button>
  <span style="color:#333;">|</span>
  <span class="d75-label">Battery:</span> <span id="d75-battery" class="d75-val">—</span>
  <span class="d75-label">BT:</span> <span id="d75-bt-state" class="d75-val">—</span>
  <span style="color:#333;">|</span>
  <span class="d75-label">TNC:</span> <span id="d75-tnc" class="d75-val">—</span>
  <span class="d75-label">Beacon:</span> <span id="d75-beacon" class="d75-val">—</span>
</div>

<!-- GPS Row -->
<div id="d75-gps-row" style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px; display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
  <span style="color:var(--t-accent); font-weight:bold;">GPS</span>
  <span class="d75-label">Lat:</span> <span id="d75-gps-lat" class="d75-val">—</span>
  <span class="d75-label">Lon:</span> <span id="d75-gps-lon" class="d75-val">—</span>
  <span class="d75-label">Alt:</span> <span id="d75-gps-alt" class="d75-val">—</span>
  <span class="d75-label">Speed:</span> <span id="d75-gps-spd" class="d75-val">—</span>
  <span class="d75-label">Sats:</span> <span id="d75-gps-sat" class="d75-val">—</span>
</div>

<!-- Memory Channels -->
<div style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
    <span style="color:var(--t-accent); font-weight:bold;">Memory Channels</span>
    <div style="display:flex; gap:6px; align-items:center;">
      <span id="d75-mem-count" style="color:#888; font-size:0.85em;"></span>
      <button class="rb rb-sm" onclick="d75LoadMemories()" id="d75-mem-scan-btn">Scan</button>
    </div>
  </div>
  <div id="d75-mem-list" style="max-height:300px; overflow-y:auto; font-family:monospace; font-size:0.85em;">
    <span style="color:#888;">Press Scan to read memory channels from radio</span>
  </div>
</div>

<!-- Volume control (full width) -->
<div style="margin-bottom:14px; background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
  <div class="d75-row">
    <span class="d75-label" style="min-width:50px;">Volume</span>
    <input id="d75-vol" type="range" min="0" max="500" value="100" step="10" style="flex:1; accent-color:var(--t-accent);"
      oninput="d75VolDebounce(this.value)">
    <span id="d75-vol-val" class="d75-val" style="min-width:3.5em;">100%</span>
  </div>
</div>

<!-- Two-column band display -->
<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(350px, 1fr)); gap:14px; margin-bottom:14px;">

  <!-- BAND A -->
  <div class="d75-band" id="d75-band-a">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
      <span style="display:flex; align-items:center; gap:8px;">
        <span style="color:var(--t-accent); font-weight:bold; font-size:1.1em;">Band A</span>
        <span id="d75-a-main" style="display:none; background:#c0392b; color:#fff; padding:1px 7px; border-radius:3px; font-size:0.8em; font-weight:bold;">MAIN</span>
      </span>
      <span id="d75-a-mode" style="color:#f39c12; font-weight:bold;">FM</span>
    </div>

    <!-- Frequency display -->
    <div style="background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:4px; padding:10px; margin-bottom:10px;">
      <div style="display:flex; justify-content:space-between; align-items:baseline;">
        <span id="d75-a-power" style="color:#f39c12; font-size:0.85em;">—</span>
        <span id="d75-a-tone-info" style="color:#888; font-size:0.8em;"></span>
        <span id="d75-a-shift-info" style="color:#888; font-size:0.8em;"></span>
      </div>
      <div id="d75-a-freq" class="d75-freq">———.———</div>
    </div>

    <!-- VFO/Memory mode + Channel -->
    <div class="d75-row">
      <select id="d75-a-vfomode" class="d75-select" onchange="d75cmd('cat','VM 0,'+this.value)">
        <option value="0">VFO</option><option value="1">Memory</option><option value="2">Call</option><option value="3">DV</option>
      </select>
      <span class="d75-label">CH:</span>
      <input id="d75-a-ch" class="d75-input" style="width:60px;" placeholder="001"
        onkeydown="if(event.key==='Enter')d75cmd('cat','MC 0,'+this.value)">
      <button class="rb rb-sm" onclick="d75cmd('cat','MC 0,'+document.getElementById('d75-a-ch').value)">Go</button>
    </div>

    <!-- S-Meter -->
    <div class="d75-row">
      <span class="d75-label">S:</span>
      <div class="d75-meter">
        <div id="d75-a-sig-bar" class="d75-meter-fill" style="background:#2ecc71; width:0%;"></div>
        <span id="d75-a-sig-text" class="d75-meter-text">S0</span>
      </div>
    </div>

    <!-- Squelch -->
    <div class="d75-row">
      <span class="d75-label" style="min-width:30px;">SQ</span>
      <input id="d75-a-sq" type="range" min="0" max="5" value="2" style="flex:1; accent-color:#f39c12;"
        oninput="d75sq(0,this.value)">
      <span id="d75-a-sq-val" class="d75-val" style="min-width:2em;">2</span>
    </div>

    <!-- Frequency input -->
    <div class="d75-row" style="margin-top:8px;">
      <span class="d75-label">Freq:</span>
      <input id="d75-a-freq-input" class="d75-input" placeholder="145.500" onkeydown="if(event.key==='Enter')d75setFreq(0,this.value)">
      <span class="d75-label">MHz</span>
      <button class="rb rb-sm" onclick="d75setFreq(0,document.getElementById('d75-a-freq-input').value)">Set</button>
    </div>

    <!-- Mode & Power -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Mode:</span>
      <select id="d75-a-mode-sel" class="d75-select" onfocus="_toneEditUntil[0]=Date.now()+5000" onchange="_toneEditUntil[0]=Date.now()+3000;d75setMode(0,this.value)">
        ''' + ''.join(f'<option value="{k}">{v}</option>' for k, v in modes.items()) + '''
      </select>
      <span class="d75-label" style="margin-left:10px;">Power:</span>
      <select id="d75-a-pwr-sel" class="d75-select" onfocus="_toneEditUntil[0]=Date.now()+5000" onchange="_toneEditUntil[0]=Date.now()+3000;d75setPower(0,this.value)">
        ''' + ''.join(f'<option value="{k}">{v}</option>' for k, v in powers.items()) + '''
      </select>
    </div>

    <!-- Tone -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Tone:</span>
      <select id="d75-a-tone-type" class="d75-select" onfocus="_toneTouched(0)" onchange="d75setTone(0)">
        <option value="off">Off</option><option value="tone">Tone</option>
        <option value="ctcss">CTCSS</option><option value="dcs">DCS</option>
      </select>
      <select id="d75-a-tone-freq" class="d75-select" onfocus="_toneTouched(0)" onchange="d75setTone(0)" style="display:none;">
        ''' + ctcss_opts + '''
      </select>
      <select id="d75-a-dcs-code" class="d75-select" onfocus="_toneTouched(0)" onchange="d75setTone(0)" style="display:none;">
        ''' + dcs_opts + '''
      </select>
    </div>

    <!-- Offset & Shift -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Shift:</span>
      <select id="d75-a-shift" class="d75-select" onfocus="_toneTouched(0)" onchange="d75setShift(0,this.value)">
        <option value="0">Off</option><option value="1">+</option><option value="2">-</option>
      </select>
      <span class="d75-label" style="margin-left:10px;">Offset:</span>
      <input id="d75-a-offset" class="d75-input" style="width:80px;" placeholder="0.600"
        onfocus="_toneTouched(0)" onkeydown="if(event.key==='Enter')d75setOffset(0,this.value)">
      <button class="rb rb-sm" onclick="d75setOffset(0,document.getElementById('d75-a-offset').value)">Set</button>
    </div>
  </div>

  <!-- BAND B -->
  <div class="d75-band" id="d75-band-b">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
      <span style="display:flex; align-items:center; gap:8px;">
        <span style="color:var(--t-accent); font-weight:bold; font-size:1.1em;">Band B</span>
        <span id="d75-b-main" style="display:none; background:#c0392b; color:#fff; padding:1px 7px; border-radius:3px; font-size:0.8em; font-weight:bold;">MAIN</span>
      </span>
      <span id="d75-b-mode" style="color:#f39c12; font-weight:bold;">FM</span>
    </div>

    <!-- Frequency display -->
    <div style="background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:4px; padding:10px; margin-bottom:10px;">
      <div style="display:flex; justify-content:space-between; align-items:baseline;">
        <span id="d75-b-power" style="color:#f39c12; font-size:0.85em;">—</span>
        <span id="d75-b-tone-info" style="color:#888; font-size:0.8em;"></span>
        <span id="d75-b-shift-info" style="color:#888; font-size:0.8em;"></span>
      </div>
      <div id="d75-b-freq" class="d75-freq">———.———</div>
    </div>

    <!-- VFO/Memory mode + Channel -->
    <div class="d75-row">
      <select id="d75-b-vfomode" class="d75-select" onchange="d75cmd('cat','VM 1,'+this.value)">
        <option value="0">VFO</option><option value="1">Memory</option><option value="2">Call</option><option value="3">DV</option>
      </select>
      <span class="d75-label">CH:</span>
      <input id="d75-b-ch" class="d75-input" style="width:60px;" placeholder="001"
        onkeydown="if(event.key==='Enter')d75cmd('cat','MC 1,'+this.value)">
      <button class="rb rb-sm" onclick="d75cmd('cat','MC 1,'+document.getElementById('d75-b-ch').value)">Go</button>
    </div>

    <!-- S-Meter -->
    <div class="d75-row">
      <span class="d75-label">S:</span>
      <div class="d75-meter">
        <div id="d75-b-sig-bar" class="d75-meter-fill" style="background:#2ecc71; width:0%;"></div>
        <span id="d75-b-sig-text" class="d75-meter-text">S0</span>
      </div>
    </div>

    <!-- Squelch -->
    <div class="d75-row">
      <span class="d75-label" style="min-width:30px;">SQ</span>
      <input id="d75-b-sq" type="range" min="0" max="5" value="2" style="flex:1; accent-color:#f39c12;"
        oninput="d75sq(1,this.value)">
      <span id="d75-b-sq-val" class="d75-val" style="min-width:2em;">2</span>
    </div>

    <!-- Frequency input -->
    <div class="d75-row" style="margin-top:8px;">
      <span class="d75-label">Freq:</span>
      <input id="d75-b-freq-input" class="d75-input" placeholder="446.000" onkeydown="if(event.key==='Enter')d75setFreq(1,this.value)">
      <span class="d75-label">MHz</span>
      <button class="rb rb-sm" onclick="d75setFreq(1,document.getElementById('d75-b-freq-input').value)">Set</button>
    </div>

    <!-- Mode & Power -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Mode:</span>
      <select id="d75-b-mode-sel" class="d75-select" onfocus="_toneEditUntil[1]=Date.now()+5000" onchange="_toneEditUntil[1]=Date.now()+3000;d75setMode(1,this.value)">
        ''' + ''.join(f'<option value="{k}">{v}</option>' for k, v in modes.items()) + '''
      </select>
      <span class="d75-label" style="margin-left:10px;">Power:</span>
      <select id="d75-b-pwr-sel" class="d75-select" onfocus="_toneEditUntil[1]=Date.now()+5000" onchange="_toneEditUntil[1]=Date.now()+3000;d75setPower(1,this.value)">
        ''' + ''.join(f'<option value="{k}">{v}</option>' for k, v in powers.items()) + '''
      </select>
    </div>

    <!-- Tone -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Tone:</span>
      <select id="d75-b-tone-type" class="d75-select" onfocus="_toneTouched(1)" onchange="d75setTone(1)">
        <option value="off">Off</option><option value="tone">Tone</option>
        <option value="ctcss">CTCSS</option><option value="dcs">DCS</option>
      </select>
      <select id="d75-b-tone-freq" class="d75-select" onfocus="_toneTouched(1)" onchange="d75setTone(1)" style="display:none;">
        ''' + ctcss_opts + '''
      </select>
      <select id="d75-b-dcs-code" class="d75-select" onfocus="_toneTouched(1)" onchange="d75setTone(1)" style="display:none;">
        ''' + dcs_opts + '''
      </select>
    </div>

    <!-- Offset & Shift -->
    <div class="d75-row" style="margin-top:6px;">
      <span class="d75-label">Shift:</span>
      <select id="d75-b-shift" class="d75-select" onfocus="_toneTouched(1)" onchange="d75setShift(1,this.value)">
        <option value="0">Off</option><option value="1">+</option><option value="2">-</option>
      </select>
      <span class="d75-label" style="margin-left:10px;">Offset:</span>
      <input id="d75-b-offset" class="d75-input" style="width:80px;" placeholder="0.600"
        onfocus="_toneTouched(1)" onkeydown="if(event.key==='Enter')d75setOffset(1,this.value)">
      <button class="rb rb-sm" onclick="d75setOffset(1,document.getElementById('d75-b-offset').value)">Set</button>
    </div>
  </div>

</div><!-- end grid -->

</div><!-- end d75-panel -->

<script>
var _modes = ''' + json_mod.dumps(modes) + ''';
var _powers = ''' + json_mod.dumps(powers) + ''';
var _d75Busy = false;
var _volTimer = null;

function fmtFreq(f) {
  if (!f) return '———.———';
  var s = String(f);
  // If raw number like "445.975", format to 3 decimal places
  var n = parseFloat(s);
  if (isNaN(n)) return s;
  return n.toFixed(3);
}

function d75cmd(cmd, args, feedbackEl) {
  var body = {cmd: cmd};
  if (args) body.args = args;
  fetch('/d75cmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.error) {
        _d75flash('Error: ' + d.error, '#e74c3c');
      } else {
        _d75flash(cmd + ': OK', '#2ecc71');
      }
    }).catch(function(e){
      _d75flash('Send failed', '#e74c3c');
    });
}
function _d75flash(msg, color) {
  var el = document.getElementById('d75-feedback');
  if (!el) return;
  el.textContent = msg;
  el.style.color = color || '#2ecc71';
  clearTimeout(el._timer);
  el._timer = setTimeout(function(){ el.textContent = ''; }, 3000);
}
function d75svcAction(action) {
  var detail = document.getElementById('d75-status-detail');
  if (action === 'btstart_via_reconnect') {
    // First reconnect, then btstart
    detail.textContent = 'Reconnecting...';
    fetch('/d75cmd', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd:'reconnect'})})
      .then(function(r){return r.json()}).then(function(d) {
        if (d.ok) {
          detail.textContent = 'Connected — starting BT...';
          setTimeout(function() {
            fetch('/d75cmd', {method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify({cmd:'btstart'})})
              .then(function(r){return r.json()}).then(function(d2) {
                detail.textContent = d2.ok ? 'BT start sent — waiting for connection...' : ('Error: ' + (d2.error||''));
                detail.style.color = d2.ok ? '#2ecc71' : '#e74c3c';
              });
          }, 1000);
        } else {
          detail.textContent = d.error || 'Reconnect failed';
          detail.style.color = '#e74c3c';
        }
      }).catch(function(e) { detail.textContent = 'Request failed'; detail.style.color = '#e74c3c'; });
    return;
  }
  var labels = {start_service: 'Starting service...', reconnect: 'Reconnecting...'};
  detail.textContent = labels[action] || 'Working...';
  detail.style.color = '#f39c12';
  fetch('/d75cmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd: action})})
    .then(function(r){return r.json()}).then(function(d) {
      if (d.ok) {
        detail.textContent = d.response || 'OK';
        detail.style.color = '#2ecc71';
        // After starting service, auto-attempt reconnect after a short delay
        if (action === 'start_service') {
          setTimeout(function() { d75svcAction('reconnect'); }, 2000);
        }
      } else {
        detail.textContent = d.error || 'Failed';
        detail.style.color = '#e74c3c';
      }
    }).catch(function(e) { detail.textContent = 'Request failed'; detail.style.color = '#e74c3c'; });
}

function d75setFreq(band, freq) {
  if (!freq) return;
  // Convert MHz to D75 format (10-digit Hz padded)
  var hz = Math.round(parseFloat(freq) * 1000000);
  var padded = ('0000000000' + hz).slice(-10);
  d75cmd('cat', 'FQ ' + band + ',' + padded);
}

function d75sq(band, val) {
  document.getElementById('d75-' + (band?'b':'a') + '-sq-val').textContent = val;
  d75cmd('cat', 'SQ ' + band + ',' + val);
}

function d75setMode(band, mode) {
  d75cmd('cat', 'MD ' + band + ',' + mode);
}

function d75setPower(band, pwr) {
  d75cmd('cat', 'PC ' + band + ',' + pwr);
}

// Edit locks: pause poll updates after user changes to prevent snap-back
var _toneEditUntil = [0, 0];  // per-band timestamps for tone/offset/shift
var _ctrlEditUntil = 0;       // timestamp for band/dual controls
var _volEditUntil = 0;        // timestamp for volume slider
function _toneTouched(band) { _toneEditUntil[band] = Date.now() + 5000; }
function _toneSent(band) { _toneEditUntil[band] = Date.now() + 20000; }

function d75setTone(band) {
  _toneTouched(band);
  var p = band ? 'b' : 'a';
  var type = document.getElementById('d75-' + p + '-tone-type').value;
  var freqSel = document.getElementById('d75-' + p + '-tone-freq');
  var dcsSel = document.getElementById('d75-' + p + '-dcs-code');
  // Show/hide appropriate selector
  freqSel.style.display = (type === 'tone' || type === 'ctcss') ? '' : 'none';
  dcsSel.style.display = (type === 'dcs') ? '' : 'none';
  // Build command
  var args = type;
  if (type === 'tone' || type === 'ctcss') args += ' ' + freqSel.value;
  else if (type === 'dcs') args += ' ' + dcsSel.value;
  _toneSent(band);
  d75cmd('tone', band + ' ' + args);
}

function d75setShift(band, val) {
  _toneSent(band);
  d75cmd('shift', band + ' ' + val);
}

function d75setOffset(band, val) {
  _toneSent(band);
  if (!val) return;
  d75cmd('offset', band + ' ' + val);
}

function _updateToneUI(band, fi) {
  // Skip poll updates while user is editing (5s grace period)
  if (Date.now() < _toneEditUntil[band]) return;
  // Update tone/offset/shift controls from freq_info
  var p = band ? 'b' : 'a';
  if (!fi) return;
  var typeSel = document.getElementById('d75-' + p + '-tone-type');
  var freqSel = document.getElementById('d75-' + p + '-tone-freq');
  var dcsSel = document.getElementById('d75-' + p + '-dcs-code');
  var shiftSel = document.getElementById('d75-' + p + '-shift');
  var offsetIn = document.getElementById('d75-' + p + '-offset');
  if (fi.ctcss_status) typeSel.value = 'ctcss';
  else if (fi.dcs_status) typeSel.value = 'dcs';
  else if (fi.tone_status) typeSel.value = 'tone';
  else typeSel.value = 'off';
  freqSel.style.display = (fi.tone_status || fi.ctcss_status) ? '' : 'none';
  dcsSel.style.display = fi.dcs_status ? '' : 'none';
  if (fi.ctcss_status && fi.ctcss_hz) freqSel.value = fi.ctcss_hz;
  else if (fi.tone_status && fi.tone_hz) freqSel.value = fi.tone_hz;
  if (fi.dcs_code) dcsSel.value = fi.dcs_code;
  shiftSel.value = fi.shift_direction || '0';
  if (fi.offset) offsetIn.value = fi.offset;
}

function _updateToneDisplay(band, fi) {
  // Update read-only tone/shift info on the frequency display row
  var p = band ? 'b' : 'a';
  var toneEl = document.getElementById('d75-' + p + '-tone-info');
  var shiftEl = document.getElementById('d75-' + p + '-shift-info');
  if (!fi) { toneEl.textContent = ''; shiftEl.textContent = ''; return; }
  // Tone info
  var t = '';
  if (fi.ctcss_status) {
    t = 'CTCSS ' + (fi.ctcss_hz || '?') + ' Hz';
    toneEl.style.color = '#2ecc71';
  } else if (fi.dcs_status) {
    t = 'DCS ' + (fi.dcs_code || '?');
    toneEl.style.color = '#2ecc71';
  } else if (fi.tone_status) {
    t = 'Tone ' + (fi.tone_hz || '?') + ' Hz';
    toneEl.style.color = '#2ecc71';
  } else {
    t = 'No Tone';
    toneEl.style.color = '#888';
  }
  toneEl.textContent = t;
  // Shift info
  var s = '';
  var sd = fi.shift_direction || '0';
  if (sd === '1' || sd === 1) { s = '+' + (fi.offset || ''); shiftEl.style.color = '#f39c12'; }
  else if (sd === '2' || sd === 2) { s = '-' + (fi.offset || ''); shiftEl.style.color = '#f39c12'; }
  else { s = 'Simplex'; shiftEl.style.color = '#888'; }
  shiftEl.textContent = s;
}

var _d75Channels = [];  // cached channel list for re-rendering
var _d75LastStatus = {};  // last polled status (dual_band, active_band, etc.)

function d75LoadMemories() {
  var btn = document.getElementById('d75-mem-scan-btn');
  var list = document.getElementById('d75-mem-list');
  var count = document.getElementById('d75-mem-count');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  list.innerHTML = '<span style="color:#888;">Scanning channels...</span>';
  fetch('/d75memlist').then(function(r){return r.json()}).then(function(channels) {
    _d75Channels = channels;
    if (!channels.length) {
      list.innerHTML = '<span style="color:#888;">No programmed channels found</span>';
      count.textContent = '';
      return;
    }
    count.textContent = channels.length + ' channels';
    _d75RenderMemList();
  }).catch(function(e) {
    list.innerHTML = '<span style="color:#e74c3c;">Scan failed: ' + e + '</span>';
  }).finally(function() {
    btn.disabled = false;
    btn.textContent = 'Scan';
  });
}

function _d75RenderMemList() {
  var list = document.getElementById('d75-mem-list');
  if (!_d75Channels.length) return;
  var ab = parseInt(document.getElementById('d75-active-band').value) || 0;
  var dual = parseInt(document.getElementById('d75-dual').value) || 0;
  // DL 0=dual (both bands active), DL 1=single (only active band)
  var aOk = (dual === 0 || ab === 0);
  var bOk = (dual === 0 || ab === 1);
  var h = '<table style="width:100%; border-collapse:collapse;">';
  h += '<tr style="color:#888; text-align:left;">';
  h += '<th style="padding:2px 6px;">CH</th><th style="padding:2px 6px;">Freq</th>';
  h += '<th style="padding:2px 6px;">Name</th><th style="padding:2px 6px;">Mode</th>';
  h += '<th style="padding:2px 6px;">Tone</th><th style="padding:2px 6px;">Shift</th>';
  h += '<th style="padding:2px 6px;">Offset</th><th style="padding:2px 6px;">Pwr</th>';
  h += '<th style="padding:2px 6px; text-align:center;">Load</th></tr>';
  _d75Channels.forEach(function(c) {
    h += '<tr class="mem-row">';
    h += '<td style="padding:3px 6px; color:var(--t-accent);">' + c.ch + '</td>';
    h += '<td style="padding:3px 6px; color:#fff; font-weight:bold;">' + c.freq.toFixed(4) + '</td>';
    h += '<td style="padding:3px 6px; color:#e0e0e0;">' + (c.name || '') + '</td>';
    h += '<td style="padding:3px 6px; color:#f39c12;">' + c.mode + '</td>';
    h += '<td style="padding:3px 6px; color:' + (c.tone ? '#2ecc71' : '#888') + ';">' + (c.tone || '—') + '</td>';
    var _sc = c.shift==='X'?'#e74c3c':c.shift==='S'?'#888':'#f39c12';
    h += '<td style="padding:3px 6px; color:'+_sc+';">' + (c.shift==='X'?'Xband':c.shift) + '</td>';
    h += '<td style="padding:3px 6px;">' + (c.offset || '') + '</td>';
    var _pn = {0:'H',1:'L',2:'EL',3:'EL2'}; h += '<td style="padding:3px 6px; color:#f39c12;">' + (_pn[c.power] || '') + '</td>';
    h += '<td style="padding:3px 6px; white-space:nowrap;">';
    h += '<button class="rb rb-sm" style="padding:2px 6px;' + (aOk ? '' : 'opacity:0.3;cursor:default;') + '" onclick="' + (aOk ? "d75GoChannel(0," + "'" + c.ch + "'" + ")" : '') + '"' + (aOk ? '' : ' disabled') + '>A</button> ';
    h += '<button class="rb rb-sm" style="padding:2px 6px;' + (bOk ? '' : 'opacity:0.3;cursor:default;') + '" onclick="' + (bOk ? "d75GoChannel(1," + "'" + c.ch + "'" + ")" : '') + '"' + (bOk ? '' : ' disabled') + '>B</button>';
    h += '</td></tr>';
  });
  h += '</table>';
  list.innerHTML = h;
}

function d75GoChannel(band, ch) {
  if (!_d75LastStatus.serial_connected) { _d75flash('D75 not connected', '#e74c3c'); return; }
  var chData = (_d75Channels || []).find(function(c) { return c.ch === ch; });
  if (!chData) { _d75flash('CH ' + ch + ' not in list — rescan first', '#e74c3c'); return; }
  var isDual = (_d75LastStatus.dual_band === 0);
  var activeBand = (_d75LastStatus.active_band !== undefined) ? _d75LastStatus.active_band : band;

  function _loadViaFO() {
    d75cmd('cat', 'VM ' + band + ',0');
    setTimeout(function() {
      if (chData.me_fields) {
        // ME field[2] has dual meaning:
        //   Small value (< 100 MHz) → already an offset (e.g. 600000 = 600 kHz)
        //   Large value (>= 100 MHz) → TX frequency (e.g. 437450000 for cross-band)
        // FO field[2] always wants offset in Hz
        var mf = chData.me_fields.split(',');
        var rxHz = parseInt(mf[0]) || 0;
        var field2 = parseInt(mf[1]) || 0;
        var shift = parseInt(mf[12]) || 0;
        var offsetHz;
        if (field2 >= 100000000) {
          // TX frequency — calculate offset
          offsetHz = Math.abs(field2 - rxHz);
          if (field2 > rxHz) shift = 1;
          else if (field2 < rxHz) shift = 2;
          else { shift = 0; offsetHz = 0; }
        } else {
          // Already an offset — use as-is
          offsetHz = field2;
        }
        mf[1] = ('0000000000' + offsetHz).slice(-10);
        mf[12] = String(shift);
        var fo = band + ',' + mf.join(',');
        d75cmd('cat', 'FO ' + fo);
      } else {
        var hz = Math.round(chData.freq * 1000000);
        var fqCmd = 'FQ ' + band + ',' + ('0000000000' + hz).slice(-10);
        d75cmd('cat', fqCmd);
      }
      if (chData.power >= 0) {
        setTimeout(function() {
          d75cmd('cat', 'PC ' + band + ',' + chData.power);
        }, 300);
      }
    }, 200);
  }

  if (!isDual && activeBand !== band) {
    d75cmd('cat', 'BC ' + band);
    setTimeout(_loadViaFO, 300);
  } else {
    _loadViaFO();
  }
  var info = chData.freq + ' MHz';
  if (chData.tone) info += ' ' + chData.tone;
  _d75flash('Band ' + (band ? 'B' : 'A') + ' → ' + info, '#2ecc71');
}

function d75VolDebounce(val) {
  _volEditUntil = Date.now() + 3000;
  document.getElementById('d75-vol-val').textContent = val + '%';
  clearTimeout(_volTimer);
  _volTimer = setTimeout(function() {
    _volEditUntil = Date.now() + 3000;
    d75cmd('vol', val);
  }, 150);
}

function updateD75() {
  if (_d75Busy) return;
  _d75Busy = true;
  fetch('/d75status').then(function(r){return r.json()}).then(function(d) {
    _d75LastStatus = d;
    var isFullyUp = d.connected && d.serial_connected;
    // Update status checklist in offline panel
    var _green = '#2ecc71', _red = '#e74c3c', _grey = '#888';
    function _chk(id, ok) {
      var el = document.getElementById(id);
      if (el) { el.style.color = ok ? _green : _red; }
    }
    _chk('d75-chk-svc', d.service_running);
    document.getElementById('d75-chk-svc-text').textContent = d.service_running ? 'Running' : 'Stopped';
    document.getElementById('d75-chk-svc-text').style.color = d.service_running ? _green : _red;
    document.getElementById('d75-start-svc-btn').style.display = d.service_running ? 'none' : '';

    _chk('d75-chk-tcp', d.tcp_connected);
    document.getElementById('d75-chk-tcp-text').textContent = d.tcp_connected ? 'Connected' : 'Disconnected';
    document.getElementById('d75-chk-tcp-text').style.color = d.tcp_connected ? _green : _red;
    document.getElementById('d75-reconnect-btn').style.display = !d.tcp_connected ? '' : 'none';

    _chk('d75-chk-serial', d.serial_connected);
    var isBTMode = (d.d75_mode || 'bluetooth') === 'bluetooth';
    var btPending = d.btstart_in_progress && !d.serial_connected;
    document.getElementById('d75-chk-serial-text').textContent = d.serial_connected ? 'Connected' : (btPending ? 'Connecting...' : 'Not responding');
    document.getElementById('d75-chk-serial-text').style.color = d.serial_connected ? _green : (btPending ? '#f39c12' : _red);
    document.getElementById('d75-offline-btstart-btn').style.display = (d.tcp_connected && !d.serial_connected && isBTMode && !btPending) ? '' : 'none';

    if (d.status_detail) {
      document.getElementById('d75-status-detail').textContent = d.status_detail;
      document.getElementById('d75-status-detail').style.color = btPending ? '#f39c12' : _red;
    }

    if (!d.d75_enabled) {
      document.getElementById('d75-offline').style.display = 'block';
      document.getElementById('d75-panel').style.display = 'none';
      _d75Busy = false;
      return;
    }
    document.getElementById('d75-offline').style.display = isFullyUp ? 'none' : 'block';
    document.getElementById('d75-panel').style.display = (d.tcp_connected || isFullyUp) ? 'block' : 'none';

    // Info bar
    document.getElementById('d75-model').textContent = d.model || '—';
    document.getElementById('d75-sn').textContent = d.serial_number || '—';
    document.getElementById('d75-fw').textContent = d.firmware || '—';

    // Connection mode
    var isBT = isBTMode;
    document.getElementById('d75-mode').textContent = isBT ? 'Bluetooth' : 'USB';
    document.getElementById('d75-mode').style.color = d.serial_connected ? '#2ecc71' : '#e74c3c';
    document.getElementById('d75-audio-row').style.display = isBT ? '' : 'none';
    document.getElementById('d75-btstart-btn').style.display = (isBT && d.tcp_connected && !d.serial_connected && !btPending) ? '' : 'none';
    document.getElementById('d75-btstop-btn').style.display = (isBT && d.tcp_connected && d.serial_connected) ? '' : 'none';
    document.getElementById('d75-audio-off-btn').style.display = (isBT && d.tcp_connected && d.serial_connected && d.audio_connected) ? '' : 'none';
    document.getElementById('d75-audio-on-btn').style.display = (isBT && d.tcp_connected && d.serial_connected && !d.audio_connected) ? '' : 'none';

    // Audio status (bluetooth only)
    if (isBT) {
      var audioEl = document.getElementById('d75-audio-status');
      if (d.audio_connected) {
        audioEl.textContent = 'Connected';
        audioEl.style.color = '#2ecc71';
      } else {
        audioEl.textContent = 'Disconnected';
        audioEl.style.color = '#e74c3c';
      }
    }

    // Radio-wide state
    document.getElementById('d75-tx-badge').style.display = d.transmitting ? '' : 'none';
    // Flash active band frequency display red during TX
    var ab = d.active_band || 0;
    var aDisp = document.getElementById('d75-a-freq').parentElement;
    var bDisp = document.getElementById('d75-b-freq').parentElement;
    aDisp.style.background = (d.transmitting && ab === 0) ? '#5c1a1a' : '';
    aDisp.style.borderColor = (d.transmitting && ab === 0) ? '#c0392b' : '';
    bDisp.style.background = (d.transmitting && ab === 1) ? '#5c1a1a' : '';
    bDisp.style.borderColor = (d.transmitting && ab === 1) ? '#c0392b' : '';
    if (Date.now() > _ctrlEditUntil) {
      var abSel = document.getElementById('d75-active-band');
      abSel.value = d.active_band || 0;
      var dlSel = document.getElementById('d75-dual');
      dlSel.value = d.dual_band || 0;
    }
    // Single/dual band display: DL 0=Dual, DL 1=Single
    var isDual = (d.dual_band === 0);
    var bandA = document.getElementById('d75-band-a');
    var bandB = document.getElementById('d75-band-b');
    if (isDual) {
      // Dual mode: show both bands, MAIN badge on active band
      bandA.style.opacity = ''; bandA.style.pointerEvents = '';
      bandB.style.opacity = ''; bandB.style.pointerEvents = '';
      document.getElementById('d75-a-main').style.display = (ab === 0) ? '' : 'none';
      document.getElementById('d75-b-main').style.display = (ab === 1) ? '' : 'none';
    } else {
      // Single mode: grey out inactive band
      var activeIsA = (ab === 0);
      bandA.style.opacity = activeIsA ? '' : '0.3'; bandA.style.pointerEvents = activeIsA ? '' : 'none';
      bandB.style.opacity = activeIsA ? '0.3' : ''; bandB.style.pointerEvents = activeIsA ? 'none' : '';
      document.getElementById('d75-a-main').style.display = 'none';
      document.getElementById('d75-b-main').style.display = 'none';
    }
    // Always re-render channel list buttons based on current band/dual state
    if (_d75Channels.length) _d75RenderMemList();
    // Battery
    var _batLvl = d.battery_level;
    var _batEl = document.getElementById('d75-battery');
    if (_batLvl >= 0) {
      var _batNames = ['Empty','Low','Med','Full'];
      var _batColors = ['#e74c3c','#f39c12','#f1c40f','#2ecc71'];
      _batEl.textContent = _batNames[_batLvl] || _batLvl;
      _batEl.style.color = _batColors[_batLvl] || '#888';
    } else {
      _batEl.textContent = '—';
      _batEl.style.color = '#888';
    }
    document.getElementById('d75-bt-state').textContent = d.bluetooth ? 'On' : 'Off';
    document.getElementById('d75-bt-state').style.color = d.bluetooth ? '#2ecc71' : '#888';
    document.getElementById('d75-tnc').textContent = d.tnc || 'Off';
    document.getElementById('d75-beacon').textContent = d.beacon_type || 'Manual';

    // GPS
    var gps = d.gps_data || {};
    document.getElementById('d75-gps-lat').textContent = gps.lat ? (gps.lat + ' ' + (gps.lat_dir||'')) : '—';
    document.getElementById('d75-gps-lon').textContent = gps.lon ? (gps.lon + ' ' + (gps.lon_dir||'')) : '—';
    document.getElementById('d75-gps-alt').textContent = gps.alt || '—';
    document.getElementById('d75-gps-spd').textContent = gps.speed || '—';
    document.getElementById('d75-gps-sat').textContent = gps.sat_num || '—';

    // Volume boost (skip update while user is adjusting)
    var volSlider = document.getElementById('d75-vol');
    if (d.audio_boost !== undefined && Date.now() > _volEditUntil) {
      volSlider.value = d.audio_boost;
      document.getElementById('d75-vol-val').textContent = d.audio_boost + '%';
    }

    // Band A
    var a = d.band_0 || {};
    document.getElementById('d75-a-freq').textContent = fmtFreq(a.frequency);
    document.getElementById('d75-a-mode').textContent = _modes[a.mode] || '?';
    document.getElementById('d75-a-power').textContent = _powers[a.power] || '';
    var aSig = a.signal || 0;
    document.getElementById('d75-a-sig-bar').style.width = (aSig / 5 * 100) + '%';
    document.getElementById('d75-a-sig-text').textContent = 'S' + aSig;
    // Update selects (skip while user is editing)
    var _pwrMap = {'H':0, 'M':1, 'L':2, 'EL':3};
    if (Date.now() > _toneEditUntil[0]) {
      document.getElementById('d75-a-mode-sel').value = a.mode || 0;
      document.getElementById('d75-a-pwr-sel').value = (_pwrMap[a.power] !== undefined) ? _pwrMap[a.power] : (parseInt(a.power) || 0);
    }
    var asq = document.getElementById('d75-a-sq');
    if (!asq.matches(':active')) { asq.value = a.squelch || 0; document.getElementById('d75-a-sq-val').textContent = a.squelch || 0; }
    if (a.freq_info) _updateToneUI(0, a.freq_info);
    _updateToneDisplay(0, a.freq_info);
    var avfm = document.getElementById('d75-a-vfomode');
    var _vmMap = {'VFO':0, 'Memory':1, 'Call':2, 'DV':3};
    if (avfm !== document.activeElement) avfm.value = _vmMap[a.memory_mode] !== undefined ? _vmMap[a.memory_mode] : 0;
    var ach = document.getElementById('d75-a-ch');
    if (ach !== document.activeElement && a.channel) ach.value = a.channel;

    // Band B
    var b = d.band_1 || {};
    document.getElementById('d75-b-freq').textContent = fmtFreq(b.frequency);
    document.getElementById('d75-b-mode').textContent = _modes[b.mode] || '?';
    document.getElementById('d75-b-power').textContent = _powers[b.power] || '';
    var bSig = b.signal || 0;
    document.getElementById('d75-b-sig-bar').style.width = (bSig / 5 * 100) + '%';
    document.getElementById('d75-b-sig-text').textContent = 'S' + bSig;
    if (Date.now() > _toneEditUntil[1]) {
      document.getElementById('d75-b-mode-sel').value = b.mode || 0;
      document.getElementById('d75-b-pwr-sel').value = (_pwrMap[b.power] !== undefined) ? _pwrMap[b.power] : (parseInt(b.power) || 0);
    }
    var bsq = document.getElementById('d75-b-sq');
    if (!bsq.matches(':active')) { bsq.value = b.squelch || 0; document.getElementById('d75-b-sq-val').textContent = b.squelch || 0; }
    if (b.freq_info) _updateToneUI(1, b.freq_info);
    _updateToneDisplay(1, b.freq_info);
    var bvfm = document.getElementById('d75-b-vfomode');
    if (bvfm !== document.activeElement) bvfm.value = _vmMap[b.memory_mode] !== undefined ? _vmMap[b.memory_mode] : 0;
    var bch = document.getElementById('d75-b-ch');
    if (bch !== document.activeElement && b.channel) bch.value = b.channel;

  }).catch(function(e){ console.error('D75 status error:', e); })
    .finally(function(){ _d75Busy = false; });
}

setInterval(updateD75, 1000);
updateD75();
</script>
'''
        return self._wrap_html('D75 Control', body)

    def _generate_kv4p_page(self):
        """Build the KV4P HT radio control HTML page."""
        # DRA818V CTCSS table: 38 tones, codes 1-38 (0=none). No 69.3 Hz.
        ctcss_tones = ["67.0","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
            "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
            "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
            "173.8","179.9","186.2","192.8","203.5","210.7","218.1","225.7","233.6","241.8","250.3"]
        body = '''
<h1 style="font-size:1.8em">KV4P HT Control</h1>

<style>
.rb { padding:8px 14px; border:1px solid var(--t-btn-border); border-radius:4px; background:var(--t-btn);
  color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.95em; min-width:44px; }
.rb:hover { background:var(--t-btn-hover); border-color:var(--t-accent); }
.rb:active { background:var(--t-border); }
.rb-sm { font-size:0.8em; padding:5px 10px; }
.rb-active { background:var(--t-accent) !important; color:#fff !important; border-color:var(--t-accent) !important; }
.kv-panel { background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; margin-bottom:14px; }
.kv-freq { color:#2ecc71; font-size:2.4em; text-align:center; letter-spacing:2px; margin:8px 0; font-family:monospace; }
.kv-label { color:#888; font-size:0.85em; }
.kv-val { color:#e0e0e0; font-family:monospace; }
.kv-row { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
.kv-meter { flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:3px; height:18px; position:relative; overflow:hidden; }
.kv-meter-fill { height:100%; transition:width 0.3s; }
.kv-meter-text { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); font-size:0.75em; color:#fff; font-weight:bold; }
.kv-input { background:var(--t-btn); border:1px solid var(--t-btn-border); color:#2ecc71; padding:6px 10px;
  border-radius:4px; font-family:monospace; font-size:1.1em; width:140px; text-align:center; }
.kv-select { background:var(--t-btn); border:1px solid var(--t-btn-border); color:#e0e0e0; padding:5px 8px;
  border-radius:3px; font-family:monospace; font-size:0.9em; }
</style>

<div id="kv-offline" style="display:none;" class="kv-panel">
  <span style="color:#e74c3c; font-weight:bold;">KV4P HT Not Connected</span>
  <span id="kv-status-detail" style="color:#f39c12; font-size:0.9em; margin-left:10px;"></span>
  <button class="rb rb-sm" onclick="kvCmd('reconnect')" style="margin-left:10px;">Reconnect</button>
</div>

<div id="kv-panel" style="display:none;">

<!-- Status bar -->
<div class="kv-panel" style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
  <span class="kv-label">FW:</span> <span id="kv-fw" class="kv-val">—</span>
  <span style="color:#333;">|</span>
  <span class="kv-label">Module:</span> <span id="kv-module" class="kv-val">—</span>
  <span style="color:#333;">|</span>
  <span class="kv-label">Audio:</span> <span id="kv-audio-status" class="kv-val">—</span>
  <span style="flex:1;"></span>
  <span id="kv-tx-badge" style="display:none; background:#c0392b; color:#fff; padding:2px 8px; border-radius:3px; font-weight:bold;">TX</span>
  <span style="flex:1;"></span>
  <span id="kv-feedback" style="font-family:monospace; font-size:0.85em; min-width:120px;"></span>
  <button class="rb rb-sm" onclick="kvCmd('ptt')" id="kv-ptt-btn">PTT</button>
  <button class="rb rb-sm" onclick="kvCmd('testtone','400')" id="kv-tone-btn">Test 400Hz</button>
  <button class="rb rb-sm" onclick="kvCmd('testtone','1000')">Test 1kHz</button>
  <button class="rb rb-sm" onclick="kvCmd('testtone')">Stop Tone</button>
  <button class="rb rb-sm" onclick="kvCmd('reconnect')">Reconnect</button>
</div>

<!-- Frequency display -->
<div class="kv-panel" id="kv-freq-box">
  <div class="kv-row" style="justify-content:center;">
    <span class="kv-label">RX</span>
    <span id="kv-rx-freq" class="kv-freq">—</span>
    <span class="kv-label">MHz</span>
  </div>
  <div id="kv-tx-freq-row" class="kv-row" style="justify-content:center; display:none;">
    <span class="kv-label">TX</span>
    <span id="kv-tx-freq" style="color:#e67e22; font-size:1.6em; font-family:monospace;">—</span>
    <span class="kv-label">MHz</span>
  </div>

  <!-- S-meter -->
  <div class="kv-row" style="margin-top:10px;">
    <span class="kv-label" style="min-width:55px;">S-meter</span>
    <div class="kv-meter">
      <div id="kv-meter-fill" class="kv-meter-fill" style="width:0%; background:linear-gradient(90deg,#27ae60,#2ecc71,#f1c40f,#e74c3c);"></div>
      <div id="kv-meter-text" class="kv-meter-text">S0</div>
    </div>
  </div>

  <!-- Audio level -->
  <div class="kv-row">
    <span class="kv-label" style="min-width:55px;">Audio</span>
    <div class="kv-meter">
      <div id="kv-audio-fill" class="kv-meter-fill" style="width:0%; background:var(--t-accent);"></div>
      <div id="kv-audio-text" class="kv-meter-text">0%</div>
    </div>
  </div>
</div>

<!-- Controls -->
<div class="kv-panel">
  <div class="kv-row" style="flex-wrap:wrap; gap:12px;">
    <div>
      <span class="kv-label">Frequency (MHz):</span><br>
      <input id="kv-freq-input" class="kv-input" type="text" placeholder="146.520" onfocus="_ctrlEditUntil=Date.now()+5000">
      <button class="rb rb-sm" onclick="kvCmd('freq',document.getElementById('kv-freq-input').value)">Set</button>
    </div>
    <div>
      <span class="kv-label">TX Freq (0=same):</span><br>
      <input id="kv-txfreq-input" class="kv-input" type="text" placeholder="0" onfocus="_ctrlEditUntil=Date.now()+5000">
      <button class="rb rb-sm" onclick="kvCmd('txfreq',document.getElementById('kv-txfreq-input').value)">Set</button>
    </div>
  </div>

  <div class="kv-row" style="margin-top:10px; flex-wrap:wrap; gap:12px;">
    <div>
      <span class="kv-label">Squelch (0-8):</span><br>
      <input id="kv-squelch" type="range" min="0" max="8" value="4" style="width:120px; accent-color:var(--t-accent);"
        oninput="_ctrlEditUntil=Date.now()+3000;document.getElementById('kv-sq-val').textContent=this.value"
        onchange="_ctrlEditUntil=Date.now()+3000;kvCmd('squelch',this.value)">
      <span id="kv-sq-val" class="kv-val">4</span>
    </div>
    <div>
      <span class="kv-label">Volume (0-500%):</span><br>
      <input id="kv-vol" type="range" min="0" max="500" value="100" style="width:120px; accent-color:var(--t-accent);"
        oninput="_ctrlEditUntil=Date.now()+3000;document.getElementById('kv-vol-val').textContent=this.value+'%'"
        onchange="_ctrlEditUntil=Date.now()+3000;kvCmd('vol',this.value)">
      <span id="kv-vol-val" class="kv-val">100%</span>
    </div>
    <div>
      <span class="kv-label">Power:</span><br>
      <select id="kv-power" class="kv-select" onfocus="_ctrlEditUntil=Date.now()+5000" onchange="_ctrlEditUntil=Date.now()+3000;kvCmd('power',this.value)">
        <option value="high">High</option><option value="low">Low</option>
      </select>
    </div>
    <div>
      <span class="kv-label">Bandwidth:</span><br>
      <select id="kv-bw" class="kv-select" onfocus="_ctrlEditUntil=Date.now()+5000" onchange="_ctrlEditUntil=Date.now()+3000;kvCmd('bandwidth',this.value)">
        <option value="wide">Wide (25kHz)</option><option value="narrow">Narrow (12.5kHz)</option>
      </select>
    </div>
  </div>

  <div class="kv-row" style="margin-top:10px; flex-wrap:wrap; gap:12px;">
    <div>
      <span class="kv-label">CTCSS TX:</span><br>
      <select id="kv-ctcss-tx" class="kv-input" onfocus="_ctrlEditUntil=Date.now()+30000">
        <option value="0">None</option>
        ''' + ''.join(f'<option value="{t}">{t} Hz</option>' for t in ctcss_tones) + '''
      </select>
    </div>
    <div>
      <span class="kv-label">CTCSS RX:</span><br>
      <select id="kv-ctcss-rx" class="kv-input" onfocus="_ctrlEditUntil=Date.now()+30000">
        <option value="0">None</option>
        ''' + ''.join(f'<option value="{t}">{t} Hz</option>' for t in ctcss_tones) + '''
      </select>
    </div>
    <button class="rb rb-sm" onclick="kvCmd('ctcss',document.getElementById('kv-ctcss-tx').value+' '+document.getElementById('kv-ctcss-rx').value)" style="align-self:flex-end;">Set CTCSS</button>
  </div>
</div>

</div><!-- end kv-panel -->

<script>
var _ctrlEditUntil = 0;
var _ctcss_tones = [''' + ','.join(f'"{t}"' for t in ctcss_tones) + '''];
function _ctcssCode2Hz(code) {
  // code 0 = none, code 1..N = tones[0..N-1]
  if (!code || code <= 0) return '0';
  var t = _ctcss_tones[code - 1];
  return t !== undefined ? t : '0';
}
function kvCmd(cmd, args) {
  var body = {cmd: cmd};
  if (args !== undefined) body.args = String(args);
  fetch('/kv4pcmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(r){return r.json()})
    .then(function(d){
      var fb = document.getElementById('kv-feedback');
      if (d.ok) {
        fb.style.color = '#2ecc71';
        fb.textContent = d.response || 'OK';
      } else {
        fb.style.color = '#e74c3c';
        fb.textContent = d.error || 'Error';
      }
      setTimeout(function(){ fb.textContent = ''; }, 4000);
    });
}

function kvPoll() {
  fetch('/kv4pstatus').then(function(r){return r.json()}).then(function(d) {
    var offline = document.getElementById('kv-offline');
    var panel = document.getElementById('kv-panel');

    if (!d.kv4p_enabled) {
      offline.style.display = 'block';
      panel.style.display = 'none';
      document.getElementById('kv-status-detail').textContent = 'KV4P disabled in config';
      return;
    }
    if (!d.connected) {
      offline.style.display = 'block';
      panel.style.display = 'none';
      document.getElementById('kv-status-detail').textContent = 'Not connected — check USB';
      return;
    }

    offline.style.display = 'none';
    panel.style.display = 'block';

    // Status bar
    document.getElementById('kv-fw').textContent = 'v' + (d.firmware_version || '?');
    document.getElementById('kv-module').textContent = d.rf_module || '?';
    document.getElementById('kv-audio-status').textContent = d.audio_connected ? 'Connected' : 'No codec';
    document.getElementById('kv-tx-badge').style.display = d.transmitting ? 'inline' : 'none';

    // Red background on freq display during TX
    var kvFreqBox = document.getElementById('kv-freq-box');
    kvFreqBox.style.background = d.transmitting ? '#5c1a1a' : '';
    kvFreqBox.style.borderColor = d.transmitting ? '#e74c3c' : '';

    // Frequency
    var rxf = d.frequency || '0.000000';
    document.getElementById('kv-rx-freq').textContent = parseFloat(rxf).toFixed(4);

    var txf = d.tx_frequency || rxf;
    if (txf !== rxf) {
      document.getElementById('kv-tx-freq-row').style.display = 'flex';
      document.getElementById('kv-tx-freq').textContent = parseFloat(txf).toFixed(4);
    } else {
      document.getElementById('kv-tx-freq-row').style.display = 'none';
    }

    // S-meter: raw 0-255 → S0-S9
    var raw = d.signal || 0;
    var s = Math.round(raw * 9 / 255);
    var pct = Math.min(100, Math.round(raw * 100 / 255));
    document.getElementById('kv-meter-fill').style.width = pct + '%';
    document.getElementById('kv-meter-text').textContent = 'S' + s + ' (' + raw + ')';

    // Audio level
    var aLvl = Math.round(d.audio_level || 0);
    document.getElementById('kv-audio-fill').style.width = Math.min(100, aLvl) + '%';
    document.getElementById('kv-audio-text').textContent = aLvl + '%';

    // Update controls — skip any field the user currently has focused
    var _af = document.activeElement ? document.activeElement.id : '';
    function _kvset(id, val) { if (id !== _af) document.getElementById(id).value = val; }
    if (Date.now() > _ctrlEditUntil) {
      _kvset('kv-squelch', d.squelch || 4);
      document.getElementById('kv-sq-val').textContent = d.squelch || 4;
      _kvset('kv-power', d.high_power ? 'high' : 'low');
      _kvset('kv-bw', (d.bandwidth === 0) ? 'narrow' : 'wide');
      _kvset('kv-ctcss-tx', _ctcssCode2Hz(d.ctcss_tx));
      _kvset('kv-ctcss-rx', _ctcssCode2Hz(d.ctcss_rx));
      if (d.audio_boost !== undefined) {
        _kvset('kv-vol', d.audio_boost);
        document.getElementById('kv-vol-val').textContent = d.audio_boost + '%';
      }
    }
  }).catch(function(){});
}

setInterval(kvPoll, 1500);
kvPoll();
</script>
'''
        return self._wrap_html('KV4P Control', body)

    def _generate_telegram_page(self):
        """Build the Telegram bot status and control page."""
        body = '''
<h1 style="font-size:1.8em">Telegram Bot</h1>

<style>
.tg-panel { background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; margin-bottom:14px; font-family:monospace; font-size:0.95em; }
.tg-row { display:flex; align-items:center; gap:14px; margin-bottom:8px; flex-wrap:wrap; }
.tg-label { color:#888; font-size:0.85em; min-width:90px; }
.tg-val { color:#e0e0e0; }
.tg-dot { font-size:1.2em; }
.tg-ok { color:#2ecc71; }
.tg-err { color:#e74c3c; }
.tg-warn { color:#f39c12; }
.rb { padding:8px 14px; border:1px solid var(--t-btn-border); border-radius:4px; background:var(--t-btn);
  color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.95em; }
.rb:hover { background:var(--t-btn-hover); border-color:var(--t-accent); }
.rb:active { background:var(--t-border); }
.rb-sm { font-size:0.8em; padding:5px 10px; }
.rb-danger { border-color:#e74c3c; }
.rb-danger:hover { background:#5c1a1a; }
.tg-msg { background:var(--t-btn); border:1px solid var(--t-btn-border); border-radius:4px; padding:10px 14px;
  margin-bottom:6px; font-size:0.9em; }
.tg-msg-in { border-left:3px solid var(--t-accent); }
.tg-msg-out { border-left:3px solid #2ecc71; }
.tg-msg-time { color:#888; font-size:0.8em; margin-right:8px; }
.tg-log-box { background:#111; border:1px solid var(--t-border); border-radius:4px; padding:10px;
  font-size:0.8em; color:#ccc; max-height:300px; overflow-y:auto; white-space:pre-wrap; word-break:break-all; display:none; }
</style>

<!-- Status Panel -->
<div class="tg-panel">
  <div class="tg-row">
    <span class="tg-label">Bot Service:</span>
    <span id="tg-bot-dot" class="tg-dot">&#x25cf;</span>
    <span id="tg-bot-state" class="tg-val">--</span>
    <span style="flex:1;"></span>
    <span id="tg-bot-user" style="color:var(--t-accent); font-size:0.9em;"></span>
  </div>
  <div class="tg-row">
    <span class="tg-label">Claude tmux:</span>
    <span id="tg-tmux-dot" class="tg-dot">&#x25cf;</span>
    <span id="tg-tmux-state" class="tg-val">--</span>
    <span style="margin-left:4px;">
      <button class="rb rb-sm" onclick="openTmux()" title="Open terminal attached to Claude tmux session">Open tmux</button>
    </span>
  </div>
  <div class="tg-row">
    <span class="tg-label">Session:</span>
    <span id="tg-session" class="tg-val">--</span>
  </div>
  <div class="tg-row">
    <span class="tg-label">Config:</span>
    <span id="tg-config-dot" class="tg-dot">&#x25cf;</span>
    <span id="tg-config-state" class="tg-val">--</span>
  </div>
  <div id="tg-feedback" style="font-size:0.85em; min-height:18px; margin-top:4px;"></div>
</div>

<!-- Stats Panel -->
<div class="tg-panel">
  <h3 style="margin:0 0 10px 0; color:var(--t-accent); font-size:1em;">Activity</h3>
  <div class="tg-row">
    <span class="tg-label">Messages today:</span>
    <span id="tg-msgs-today" class="tg-val" style="color:var(--t-accent); font-size:1.2em;">--</span>
  </div>
  <div class="tg-row">
    <span class="tg-label">Last incoming:</span>
    <span id="tg-last-in" class="tg-val">--</span>
  </div>
  <div class="tg-row">
    <span class="tg-label">Last reply:</span>
    <span id="tg-last-out" class="tg-val" style="color:#2ecc71;">--</span>
  </div>
  <div id="tg-last-msg" class="tg-msg tg-msg-in" style="display:none;">
    <span class="tg-msg-time" id="tg-last-msg-time"></span>
    <span id="tg-last-msg-text"></span>
  </div>
</div>

<!-- Service Controls -->
<div class="tg-panel">
  <h3 style="margin:0 0 10px 0; color:var(--t-accent); font-size:1em;">Service Controls</h3>
  <div class="tg-row">
    <button class="rb rb-sm" onclick="tgCmd('start')">Start</button>
    <button class="rb rb-sm" onclick="tgCmd('stop')" >Stop</button>
    <button class="rb rb-sm" onclick="tgCmd('restart')">Restart</button>
    <span style="color:#555;">|</span>
    <button class="rb rb-sm" onclick="tgCmd('enable')">Enable on boot</button>
    <button class="rb rb-sm rb-danger" onclick="tgCmd('disable')">Disable on boot</button>
  </div>
</div>

<!-- Logs -->
<div class="tg-panel">
  <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
    <h3 style="margin:0; color:var(--t-accent); font-size:1em;">Service Logs</h3>
    <button class="rb rb-sm" onclick="tgLogs()">Load logs</button>
    <button class="rb rb-sm" onclick="document.getElementById('tg-log-box').style.display='none'">Hide</button>
  </div>
  <div id="tg-log-box" class="tg-log-box"></div>
</div>

<script>
function tgCmd(cmd) {
  var fb = document.getElementById('tg-feedback');
  fb.style.color = '#f39c12'; fb.textContent = cmd + '...';
  fetch('/telegramcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:cmd})})
    .then(function(r){return r.json()})
    .then(function(d){
      fb.style.color = d.ok ? '#2ecc71' : '#e74c3c';
      fb.textContent = d.ok ? cmd + ' OK' : (d.error || 'failed');
      setTimeout(function(){ fb.textContent=''; }, 4000);
      tgPoll();
    })
    .catch(function(e){ fb.style.color='#e74c3c'; fb.textContent=String(e); });
}

function tgLogs() {
  fetch('/telegramcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'logs'})})
    .then(function(r){return r.json()})
    .then(function(d){
      var box = document.getElementById('tg-log-box');
      box.style.display = 'block';
      box.textContent = d.logs || 'No logs available';
      box.scrollTop = box.scrollHeight;
    });
}

function openTmux() {
  fetch('/open_tmux', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
}

function fmtTime(ts) {
  if (!ts) return '--';
  try { return new Date(ts).toLocaleTimeString(); } catch(e) { return ts; }
}

function tgPoll() {
  fetch('/telegramstatus').then(function(r){return r.json()}).then(function(d) {
    // Bot status
    var botDot = document.getElementById('tg-bot-dot');
    var botState = document.getElementById('tg-bot-state');
    botDot.className = 'tg-dot ' + (d.bot_running ? 'tg-ok' : 'tg-err');
    botState.textContent = d.bot_running ? 'Running' : 'Stopped';
    document.getElementById('tg-bot-user').textContent = d.bot_username || '';

    // tmux status
    var tmuxDot = document.getElementById('tg-tmux-dot');
    var tmuxState = document.getElementById('tg-tmux-state');
    tmuxDot.className = 'tg-dot ' + (d.tmux_active ? 'tg-ok' : 'tg-err');
    tmuxState.textContent = d.tmux_active ? 'Active' : 'Not found';
    document.getElementById('tg-session').textContent = d.tmux_session || '--';

    // Config status
    var cfgDot = document.getElementById('tg-config-dot');
    var cfgState = document.getElementById('tg-config-state');
    if (d.enabled && d.token_set) {
      cfgDot.className = 'tg-dot tg-ok';
      cfgState.textContent = 'Enabled, token set';
    } else if (d.enabled && !d.token_set) {
      cfgDot.className = 'tg-dot tg-warn';
      cfgState.textContent = 'Enabled but no token';
    } else if (!d.enabled && d.token_set) {
      cfgDot.className = 'tg-dot tg-warn';
      cfgState.textContent = 'Disabled (token set)';
    } else {
      cfgDot.className = 'tg-dot tg-err';
      cfgState.textContent = 'Disabled, no token';
    }

    // Activity
    document.getElementById('tg-msgs-today').textContent = d.messages_today != null ? d.messages_today : '--';
    document.getElementById('tg-last-in').textContent = fmtTime(d.last_message_time);
    document.getElementById('tg-last-out').textContent = fmtTime(d.last_reply_time);

    // Last message preview
    var msgBox = document.getElementById('tg-last-msg');
    if (d.last_message_text) {
      msgBox.style.display = 'block';
      document.getElementById('tg-last-msg-time').textContent = fmtTime(d.last_message_time);
      document.getElementById('tg-last-msg-text').textContent = d.last_message_text;
    } else {
      msgBox.style.display = 'none';
    }
  }).catch(function(){});
}

setInterval(tgPoll, 3000);
tgPoll();
</script>
'''
        return self._wrap_html('Telegram Bot', body)

    def _generate_aircraft_page(self):
        """Build the ADS-B aircraft map page — a full-height iframe proxying dump1090-fa."""
        t = self._get_theme()
        adsb_port = int(getattr(self.config, 'ADSB_PORT', 30080))
        return f'''<!DOCTYPE html>
<html style="height:100%;margin:0;padding:0"><head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; overflow: hidden; background: {t['bg']}; }}
  #adsb-frame {{ width: 100%; height: 100%; border: none; display: block; }}
  #adsb-error {{
    display: none; height: 100%; align-items: center; justify-content: center;
    flex-direction: column; gap: 12px; color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    background: {t['bg']};
  }}
  #adsb-error h2 {{ color: #e74c3c; }}
  #adsb-error code {{ color: {t['accent']}; }}
</style>
</head><body>
<div id="adsb-error">
  <h2>ADS-B Unavailable</h2>
  <p>dump1090-fa is not running on port {adsb_port}</p>
  <p style="color:#888">Start it with:</p>
  <code>sudo systemctl start dump1090-fa</code>
</div>
<iframe id="adsb-frame" src="/adsb/"
  onload="document.getElementById('adsb-error').style.display='none';this.style.display='block';"
  onerror="document.getElementById('adsb-error').style.display='flex';this.style.display='none';">
</iframe>
</body></html>'''

    def _generate_sdr_page(self):
        """Build the SDR control HTML page — RSPduo dual-tuner layout."""
        body = '''
<h1 style="font-size:1.8em">SDR Control — RSPduo Dual Tuner</h1>

<!-- Status row: SDR1 (Master) and SDR2 (Slave) side by side -->
<div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
  <!-- SDR1 status -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
      <span style="color:#00aaff; font-weight:bold; font-size:0.85em;">TUNER 1 (Master)</span>
      <span id="sdr-proc-badge" style="padding:2px 8px; border-radius:4px; font-weight:bold; font-size:0.8em;">--</span>
      <span id="sdr-freq-display" style="font-size:1.6em; font-weight:bold; color:#00ff88; font-family:monospace;">---.--- MHz</span>
      <span id="sdr-mod-badge" style="padding:2px 8px; border-radius:4px; background:var(--t-border); color:var(--t-accent); font-weight:bold; font-size:0.85em;">--</span>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin-top:6px;">
      <span style="color:#888; font-size:0.8em; min-width:55px;">Audio</span>
      <div style="flex:1; background:var(--t-btn); border-radius:3px; height:14px; overflow:hidden;">
        <div id="sdr-audio-bar" style="height:100%; width:0%; background:linear-gradient(90deg,#00ff88,#ffcc00,#ff4444); transition:width 0.3s;"></div>
      </div>
      <span id="sdr-audio-val" style="color:#888; font-size:0.8em; min-width:30px; text-align:right;">0</span>
    </div>
  </div>
  <!-- SDR2 status -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:10px 14px;">
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
      <span style="color:#ff8800; font-weight:bold; font-size:0.85em;">TUNER 2 (Slave)</span>
      <span id="sdr2-proc-badge" style="padding:2px 8px; border-radius:4px; font-weight:bold; font-size:0.8em;">--</span>
      <span id="sdr2-freq-display" style="font-size:1.6em; font-weight:bold; color:#00ff88; font-family:monospace;">---.--- MHz</span>
      <span id="sdr2-mod-badge" style="padding:2px 8px; border-radius:4px; background:var(--t-border); color:var(--t-accent); font-weight:bold; font-size:0.85em;">--</span>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin-top:6px;">
      <span style="color:#888; font-size:0.8em; min-width:55px;">Audio</span>
      <div style="flex:1; background:var(--t-btn); border-radius:3px; height:14px; overflow:hidden;">
        <div id="sdr2-audio-bar" style="height:100%; width:0%; background:linear-gradient(90deg,#ff8800,#ffcc00,#ff4444); transition:width 0.3s;"></div>
      </div>
      <span id="sdr2-audio-val" style="color:#888; font-size:0.8em; min-width:30px; text-align:right;">0</span>
    </div>
  </div>
</div>

<!-- Shared settings (full width) — parameters that are linked between both tuners -->
<div style="background:var(--t-panel); border:2px solid #555; border-radius:6px; padding:12px 16px; margin-bottom:10px;">
  <h3 style="color:#aaa; margin:0 0 10px; font-size:0.9em; text-transform:uppercase; letter-spacing:1px;">Shared Settings <span style="font-size:0.8em; color:#666; font-weight:normal;">(apply to both tuners)</span></h3>
  <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
    <div style="display:flex; align-items:center; gap:8px;">
      <label style="color:#b0b0b0; font-size:0.85em;">Sample Rate</label>
      <select id="sdr-sr" class="si" title="Both tuners use the same ADC clock — max 2.0 MHz in Master/Slave mode">
        <option value="0.5">500 kHz</option>
        <option value="1.0">1.0 MHz</option>
        <option value="1.536">1.536 MHz</option>
        <option value="2.0" selected>2.0 MHz (max)</option>
      </select>
    </div>
    <div style="display:flex; align-items:center; gap:8px;">
      <label style="color:#b0b0b0; font-size:0.85em;">Correction (ppm)</label>
      <input type="number" id="sdr-correction" step="0.1" min="-100" max="100" value="0" class="si" style="width:75px;" title="Frequency calibration — applies to Tuner 1 (Master)">
    </div>
    <div style="display:flex; align-items:center; gap:8px;">
      <label style="color:#b0b0b0; font-size:0.85em;">Antenna (T1)</label>
      <select id="sdr-ant" class="si">
        <option value="Tuner 1 50 ohm">Tuner 1 50 ohm</option>
        <option value="Tuner 1 Hi-Z">Tuner 1 Hi-Z</option>
      </select>
    </div>
  </div>
</div>

<!-- Dual-column tuner controls -->
<div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px;">

<!-- ===== SDR1 (Tuner 1 — Master) ===== -->
<div>
  <h2 style="color:#00aaff; font-size:1em; margin:0 0 8px; padding:6px 10px; background:rgba(0,170,255,0.1); border-left:3px solid #00aaff; border-radius:3px;">SDR1 — Tuner 1 (Master)</h2>

  <!-- SDR1: Frequency -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Frequency</h3>
    <div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;">
      <input type="number" id="sdr-freq" step="0.00125" min="0.001" max="2000" value="446.760"
             style="flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); color:#00ff88; padding:6px; border-radius:4px; font-family:monospace; font-size:1.2em; text-align:center;">
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

  <!-- SDR1: Modulation & Options -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Demodulation</h3>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Mode</label>
      <select id="sdr-mod" class="si"><option value="nfm">NFM</option><option value="am">AM</option></select>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">NFM Tau</label>
      <select id="sdr-tau" class="si">
        <option value="0">Off</option><option value="50">50 µs</option><option value="75">75 µs</option>
        <option value="200" selected>200 µs</option><option value="530">530 µs</option><option value="1000">1000 µs</option>
      </select>
    </div>
    <label class="tgl"><input type="checkbox" id="sdr-continuous" checked> Continuous output</label>
  </div>

  <!-- SDR1: Gain -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Gain</h3>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Mode</label>
      <select id="sdr-gain-mode" class="si" onchange="toggleGainSliders()">
        <option value="agc">AGC (Auto)</option><option value="manual">Manual</option>
      </select>
    </div>
    <div id="agc-settings">
      <div style="display:flex; align-items:center; gap:8px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Setpoint</label>
        <input type="range" id="sdr-agc-sp" min="-72" max="0" value="-30" style="flex:1;"
               oninput="document.getElementById('agc-sp-val').textContent=this.value+' dB'">
        <span id="agc-sp-val" style="color:#b0b0b0; font-size:0.85em; min-width:50px;">-30 dB</span>
      </div>
    </div>
    <div id="manual-gain-settings" style="display:none;">
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">RF (RFGR)</label>
        <input type="range" id="sdr-rfgr" min="0" max="9" value="4" style="flex:1;"
               oninput="document.getElementById('rfgr-val').textContent=this.value">
        <span id="rfgr-val" style="color:#b0b0b0; font-size:0.85em; min-width:25px;">4</span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">IF (IFGR)</label>
        <input type="range" id="sdr-ifgr" min="20" max="59" value="40" style="flex:1;"
               oninput="document.getElementById('ifgr-val').textContent=this.value">
        <span id="ifgr-val" style="color:#b0b0b0; font-size:0.85em; min-width:25px;">40</span>
      </div>
    </div>
  </div>

  <!-- SDR1: Squelch & Device Options -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Squelch &amp; Device</h3>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Squelch</label>
      <input type="range" id="sdr-squelch" min="-60" max="0" value="0" style="flex:1;"
             oninput="document.getElementById('sq-val').textContent=this.value==0?'Auto':this.value+' dBFS'">
      <span id="sq-val" style="color:#b0b0b0; font-size:0.85em; min-width:60px;">Auto</span>
    </div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:4px;">
      <label class="tgl"><input type="checkbox" id="sdr-biast"> Bias-T</label>
      <label class="tgl"><input type="checkbox" id="sdr-rfnotch"> RF Notch</label>
      <label class="tgl"><input type="checkbox" id="sdr-dabnotch"> DAB Notch</label>
      <label class="tgl"><input type="checkbox" id="sdr-iqcorr" checked> IQ Correction</label>
      <label class="tgl"><input type="checkbox" id="sdr-extref"> External Ref</label>
    </div>
  </div>

  <!-- SDR1: Audio Filters -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Audio Filters</h3>
    <div style="display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:6px 8px;">
      <label style="color:#b0b0b0; font-size:0.83em;">Amp</label>
      <input type="number" id="sdr-ampfactor" step="0.1" min="0" max="10" value="1.0" class="si">
      <span style="color:#666; font-size:0.75em;">×</span>
      <label style="color:#b0b0b0; font-size:0.83em;">HPF</label>
      <input type="number" id="sdr-highpass" step="10" min="0" max="1000" value="100" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">LPF</label>
      <input type="number" id="sdr-lowpass" step="100" min="500" max="8000" value="2500" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Ch BW</label>
      <input type="number" id="sdr-chbw" step="1000" min="0" max="25000" value="0" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Notch</label>
      <input type="number" id="sdr-notch" step="1" min="0" max="5000" value="0" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Notch Q</label>
      <input type="number" id="sdr-notchq" step="1" min="1" max="100" value="10" class="si">
      <span style="color:#666; font-size:0.75em;"></span>
    </div>
  </div>
</div>

<!-- ===== SDR2 (Tuner 2 — Slave) ===== -->
<div>
  <h2 style="color:#ff8800; font-size:1em; margin:0 0 8px; padding:6px 10px; background:rgba(255,136,0,0.1); border-left:3px solid #ff8800; border-radius:3px;">SDR2 — Tuner 2 (Slave)</h2>

  <!-- SDR2: Frequency -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Frequency</h3>
    <div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;">
      <input type="number" id="sdr2-freq" step="0.00125" min="0.001" max="2000" value="462.550"
             style="flex:1; background:var(--t-btn); border:1px solid var(--t-btn-border); color:#ff9944; padding:6px; border-radius:4px; font-family:monospace; font-size:1.2em; text-align:center;">
      <span style="color:#b0b0b0;">MHz</span>
    </div>
    <div style="display:flex; gap:4px; flex-wrap:wrap;">
      <button class="sb" onclick="stepFreq2(-0.025)">-25k</button>
      <button class="sb" onclick="stepFreq2(-0.0125)">-12.5k</button>
      <button class="sb" onclick="stepFreq2(-0.00625)">-6.25k</button>
      <button class="sb" onclick="stepFreq2(0.00625)">+6.25k</button>
      <button class="sb" onclick="stepFreq2(0.0125)">+12.5k</button>
      <button class="sb" onclick="stepFreq2(0.025)">+25k</button>
    </div>
  </div>

  <!-- SDR2: Modulation & Options -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Demodulation</h3>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Mode</label>
      <select id="sdr2-mod" class="si"><option value="nfm">NFM</option><option value="am">AM</option></select>
    </div>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">NFM Tau</label>
      <select id="sdr2-tau" class="si">
        <option value="0">Off</option><option value="50">50 µs</option><option value="75">75 µs</option>
        <option value="200" selected>200 µs</option><option value="530">530 µs</option><option value="1000">1000 µs</option>
      </select>
    </div>
    <label class="tgl"><input type="checkbox" id="sdr2-continuous" checked> Continuous output</label>
  </div>

  <!-- SDR2: Gain -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Gain</h3>
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Mode</label>
      <select id="sdr2-gain-mode" class="si" onchange="toggleGainSliders2()">
        <option value="agc">AGC (Auto)</option><option value="manual">Manual</option>
      </select>
    </div>
    <div id="agc-settings2">
      <div style="display:flex; align-items:center; gap:8px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Setpoint</label>
        <input type="range" id="sdr2-agc-sp" min="-72" max="0" value="-30" style="flex:1;"
               oninput="document.getElementById('agc-sp2-val').textContent=this.value+' dB'">
        <span id="agc-sp2-val" style="color:#b0b0b0; font-size:0.85em; min-width:50px;">-30 dB</span>
      </div>
    </div>
    <div id="manual-gain-settings2" style="display:none;">
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">RF (RFGR)</label>
        <input type="range" id="sdr2-rfgr" min="0" max="9" value="4" style="flex:1;"
               oninput="document.getElementById('rfgr2-val').textContent=this.value">
        <span id="rfgr2-val" style="color:#b0b0b0; font-size:0.85em; min-width:25px;">4</span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">IF (IFGR)</label>
        <input type="range" id="sdr2-ifgr" min="20" max="59" value="40" style="flex:1;"
               oninput="document.getElementById('ifgr2-val').textContent=this.value">
        <span id="ifgr2-val" style="color:#b0b0b0; font-size:0.85em; min-width:25px;">40</span>
      </div>
    </div>
  </div>

  <!-- SDR2: Squelch -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:8px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Squelch</h3>
    <div style="display:flex; align-items:center; gap:8px;">
      <label style="color:#b0b0b0; font-size:0.85em; min-width:70px;">Squelch</label>
      <input type="range" id="sdr2-squelch" min="-60" max="0" value="0" style="flex:1;"
             oninput="document.getElementById('sq2-val').textContent=this.value==0?'Auto':this.value+' dBFS'">
      <span id="sq2-val" style="color:#b0b0b0; font-size:0.85em; min-width:60px;">Auto</span>
    </div>
  </div>

  <!-- SDR2: Audio Filters -->
  <div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px;">
    <h3 style="color:var(--t-accent); margin:0 0 8px; font-size:0.9em;">Audio Filters</h3>
    <div style="display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:6px 8px;">
      <label style="color:#b0b0b0; font-size:0.83em;">Amp</label>
      <input type="number" id="sdr2-ampfactor" step="0.1" min="0" max="10" value="1.0" class="si">
      <span style="color:#666; font-size:0.75em;">×</span>
      <label style="color:#b0b0b0; font-size:0.83em;">HPF</label>
      <input type="number" id="sdr2-highpass" step="10" min="0" max="1000" value="100" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">LPF</label>
      <input type="number" id="sdr2-lowpass" step="100" min="500" max="8000" value="2500" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Ch BW</label>
      <input type="number" id="sdr2-chbw" step="1000" min="0" max="25000" value="0" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Notch</label>
      <input type="number" id="sdr2-notch" step="1" min="0" max="5000" value="0" class="si">
      <span style="color:#666; font-size:0.75em;">Hz</span>
      <label style="color:#b0b0b0; font-size:0.83em;">Notch Q</label>
      <input type="number" id="sdr2-notchq" step="1" min="1" max="100" value="10" class="si">
      <span style="color:#666; font-size:0.75em;"></span>
    </div>
  </div>
</div>

</div><!-- end dual-column grid -->

<!-- Apply & Stop — full width -->
<div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:12px; margin-bottom:10px;">
  <div style="font-size:0.8em; color:#888; margin-bottom:8px;">
    Restart sequence: SDR1 Master starts first — SDR2 Slave waits until Master is streaming (~3s). Total restart ~12s.
  </div>
  <div style="display:flex; gap:10px;">
    <button id="sdr-apply-btn" onclick="applySettings()"
            style="flex:1; padding:12px; background:#27ae60; color:#fff; border:none; border-radius:6px; font-size:1.1em; font-weight:bold; cursor:pointer;">
      Apply &amp; Restart Both Tuners
    </button>
    <button id="sdr-stop-btn" onclick="sdrCmd('stop')"
            style="padding:12px 22px; background:#c0392b; color:#fff; border:none; border-radius:6px; font-size:1.1em; font-weight:bold; cursor:pointer;">
      Stop Both
    </button>
  </div>
  <div id="sdr-apply-status" style="color:#888; font-size:0.9em; margin-top:8px; min-height:1.2em;"></div>
</div>


<style>
  .sb { padding:6px 12px; background:var(--t-border); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; cursor:pointer; font-size:0.85em; }
  .sb:hover { background:#1a4a7a; }
  .sb:active { background:#27ae60; }
  .si { background:var(--t-btn); border:1px solid var(--t-btn-border); color:#e0e0e0; padding:6px 8px; border-radius:4px; font-size:0.9em; width:100%; }
  .tgl { display:flex; align-items:center; gap:6px; color:#b0b0b0; font-size:0.85em; padding:3px 0; cursor:pointer; }
  .tgl input { width:16px; height:16px; accent-color:var(--t-accent); }
</style>

<script>
var pollTimer = null;
var initialLoad = true;

function toggleGainSliders() {
  var mode = document.getElementById('sdr-gain-mode').value;
  document.getElementById('agc-settings').style.display = mode === 'agc' ? '' : 'none';
  document.getElementById('manual-gain-settings').style.display = mode === 'manual' ? '' : 'none';
}

function toggleGainSliders2() {
  var mode = document.getElementById('sdr2-gain-mode').value;
  document.getElementById('agc-settings2').style.display = mode === 'agc' ? '' : 'none';
  document.getElementById('manual-gain-settings2').style.display = mode === 'manual' ? '' : 'none';
}

function stepFreq(delta) {
  var el = document.getElementById('sdr-freq');
  el.value = Math.max(0.001, Math.min(2000, parseFloat(el.value) + delta)).toFixed(5).replace(/0+$/, '').replace(/\\.$/, '');
}

function stepFreq2(delta) {
  var el = document.getElementById('sdr2-freq');
  el.value = Math.max(0.001, Math.min(2000, parseFloat(el.value) + delta)).toFixed(5).replace(/0+$/, '').replace(/\\.$/, '');
}

function setSelectByValue(id, val) {
  var sel = document.getElementById(id);
  var best = -1, bestDiff = 1e9;
  for (var i = 0; i < sel.options.length; i++) {
    var diff = Math.abs(parseFloat(sel.options[i].value) - parseFloat(val));
    if (diff < bestDiff) { bestDiff = diff; best = i; }
  }
  if (best >= 0) sel.selectedIndex = best;
}

function gatherSettings() {
  return {
    // SDR1
    frequency: parseFloat(document.getElementById('sdr-freq').value),
    modulation: document.getElementById('sdr-mod').value,
    gain_mode: document.getElementById('sdr-gain-mode').value,
    rfgr: parseInt(document.getElementById('sdr-rfgr').value),
    ifgr: parseInt(document.getElementById('sdr-ifgr').value),
    agc_setpoint: parseInt(document.getElementById('sdr-agc-sp').value),
    squelch_threshold: parseInt(document.getElementById('sdr-squelch').value),
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
    // Shared
    sample_rate: parseFloat(document.getElementById('sdr-sr').value),
    antenna: document.getElementById('sdr-ant').value,
    correction: parseFloat(document.getElementById('sdr-correction').value),
    // SDR2
    frequency2: parseFloat(document.getElementById('sdr2-freq').value),
    modulation2: document.getElementById('sdr2-mod').value,
    gain_mode2: document.getElementById('sdr2-gain-mode').value,
    rfgr2: parseInt(document.getElementById('sdr2-rfgr').value),
    ifgr2: parseInt(document.getElementById('sdr2-ifgr').value),
    agc_setpoint2: parseInt(document.getElementById('sdr2-agc-sp').value),
    squelch_threshold2: parseInt(document.getElementById('sdr2-squelch').value),
    tau2: parseInt(document.getElementById('sdr2-tau').value),
    ampfactor2: parseFloat(document.getElementById('sdr2-ampfactor').value),
    lowpass2: parseInt(document.getElementById('sdr2-lowpass').value),
    highpass2: parseInt(document.getElementById('sdr2-highpass').value),
    notch2: parseFloat(document.getElementById('sdr2-notch').value),
    notch_q2: parseFloat(document.getElementById('sdr2-notchq').value),
    channel_bw2: parseFloat(document.getElementById('sdr2-chbw').value),
    continuous2: document.getElementById('sdr2-continuous').checked,
  };
}

function loadSettingsToUI(d) {
  // SDR1
  if (d.frequency !== undefined) document.getElementById('sdr-freq').value = d.frequency;
  if (d.modulation) document.getElementById('sdr-mod').value = d.modulation;
  if (d.gain_mode) { document.getElementById('sdr-gain-mode').value = d.gain_mode; toggleGainSliders(); }
  if (d.rfgr !== undefined) { document.getElementById('sdr-rfgr').value = d.rfgr; document.getElementById('rfgr-val').textContent = d.rfgr; }
  if (d.ifgr !== undefined) { document.getElementById('sdr-ifgr').value = d.ifgr; document.getElementById('ifgr-val').textContent = d.ifgr; }
  if (d.agc_setpoint !== undefined) { document.getElementById('sdr-agc-sp').value = d.agc_setpoint; document.getElementById('agc-sp-val').textContent = d.agc_setpoint + ' dB'; }
  if (d.squelch_threshold !== undefined) { document.getElementById('sdr-squelch').value = d.squelch_threshold; document.getElementById('sq-val').textContent = d.squelch_threshold == 0 ? 'Auto' : d.squelch_threshold + ' dBFS'; }
  if (d.tau !== undefined) setSelectByValue('sdr-tau', d.tau);
  if (d.ampfactor !== undefined) document.getElementById('sdr-ampfactor').value = d.ampfactor;
  if (d.lowpass !== undefined) document.getElementById('sdr-lowpass').value = d.lowpass;
  if (d.highpass !== undefined) document.getElementById('sdr-highpass').value = d.highpass;
  if (d.notch !== undefined) document.getElementById('sdr-notch').value = d.notch;
  if (d.notch_q !== undefined) document.getElementById('sdr-notchq').value = d.notch_q;
  if (d.channel_bw !== undefined) document.getElementById('sdr-chbw').value = d.channel_bw;
  if (d.bias_t !== undefined) document.getElementById('sdr-biast').checked = d.bias_t;
  if (d.rf_notch !== undefined) document.getElementById('sdr-rfnotch').checked = d.rf_notch;
  if (d.dab_notch !== undefined) document.getElementById('sdr-dabnotch').checked = d.dab_notch;
  if (d.iq_correction !== undefined) document.getElementById('sdr-iqcorr').checked = d.iq_correction;
  if (d.external_ref !== undefined) document.getElementById('sdr-extref').checked = d.external_ref;
  if (d.continuous !== undefined) document.getElementById('sdr-continuous').checked = d.continuous;
  // Shared
  if (d.sample_rate !== undefined) setSelectByValue('sdr-sr', d.sample_rate);
  if (d.antenna) document.getElementById('sdr-ant').value = d.antenna;
  if (d.correction !== undefined) document.getElementById('sdr-correction').value = d.correction;
  // SDR2
  if (d.frequency2 !== undefined) document.getElementById('sdr2-freq').value = d.frequency2;
  if (d.modulation2) document.getElementById('sdr2-mod').value = d.modulation2;
  if (d.gain_mode2) { document.getElementById('sdr2-gain-mode').value = d.gain_mode2; toggleGainSliders2(); }
  if (d.rfgr2 !== undefined) { document.getElementById('sdr2-rfgr').value = d.rfgr2; document.getElementById('rfgr2-val').textContent = d.rfgr2; }
  if (d.ifgr2 !== undefined) { document.getElementById('sdr2-ifgr').value = d.ifgr2; document.getElementById('ifgr2-val').textContent = d.ifgr2; }
  if (d.agc_setpoint2 !== undefined) { document.getElementById('sdr2-agc-sp').value = d.agc_setpoint2; document.getElementById('agc-sp2-val').textContent = d.agc_setpoint2 + ' dB'; }
  if (d.squelch_threshold2 !== undefined) { document.getElementById('sdr2-squelch').value = d.squelch_threshold2; document.getElementById('sq2-val').textContent = d.squelch_threshold2 == 0 ? 'Auto' : d.squelch_threshold2 + ' dBFS'; }
  if (d.tau2 !== undefined) setSelectByValue('sdr2-tau', d.tau2);
  if (d.ampfactor2 !== undefined) document.getElementById('sdr2-ampfactor').value = d.ampfactor2;
  if (d.lowpass2 !== undefined) document.getElementById('sdr2-lowpass').value = d.lowpass2;
  if (d.highpass2 !== undefined) document.getElementById('sdr2-highpass').value = d.highpass2;
  if (d.notch2 !== undefined) document.getElementById('sdr2-notch').value = d.notch2;
  if (d.notch_q2 !== undefined) document.getElementById('sdr2-notchq').value = d.notch_q2;
  if (d.channel_bw2 !== undefined) document.getElementById('sdr2-chbw').value = d.channel_bw2;
  if (d.continuous2 !== undefined) document.getElementById('sdr2-continuous').checked = d.continuous2;
}

function applySettings() {
  var btn = document.getElementById('sdr-apply-btn');
  var status = document.getElementById('sdr-apply-status');
  btn.disabled = true;
  btn.textContent = 'Restarting...';
  status.textContent = 'Starting SDR1 Master... then SDR2 Slave (~12s total)';
  status.style.color = '#ffcc00';
  var settings = gatherSettings();
  settings.cmd = 'tune';
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(settings) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      btn.disabled = false;
      btn.textContent = 'Apply & Restart Both Tuners';
      if (d.ok) {
        status.textContent = 'Both tuners restarted successfully';
        status.style.color = '#00ff88';
        initialLoad = true;
      } else {
        status.textContent = 'Error: ' + (d.error || 'unknown');
        status.style.color = '#ff4444';
      }
      setTimeout(function() { status.textContent = ''; }, 6000);
    })
    .catch(function(e) {
      btn.disabled = false;
      btn.textContent = 'Apply & Restart Both Tuners';
      status.textContent = 'Network error';
      status.style.color = '#ff4444';
    });
}

function sdrCmd(cmd) {
  var status = document.getElementById('sdr-apply-status');
  fetch('/sdrcmd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: cmd}) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      status.textContent = d.ok ? cmd + ' OK' : 'Error: ' + (d.error || '');
      status.style.color = d.ok ? '#00ff88' : '#ff4444';
      setTimeout(function() { status.textContent = ''; }, 3000);
    });
}


var _sdrBusy = false;
function pollStatus() {
  if (_sdrBusy) return;
  _sdrBusy = true;
  fetch('/sdrstatus')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      // SDR1 status
      var badge = document.getElementById('sdr-proc-badge');
      if (d.process_alive) {
        badge.textContent = 'RUNNING'; badge.style.background = '#27ae60'; badge.style.color = '#fff';
        document.getElementById('sdr-freq-display').textContent = parseFloat(d.frequency).toFixed(3) + ' MHz';
        document.getElementById('sdr-freq-display').style.color = '#00ff88';
        document.getElementById('sdr-mod-badge').textContent = (d.modulation || '--').toUpperCase();
      } else {
        badge.textContent = 'STOPPED'; badge.style.background = '#c0392b'; badge.style.color = '#fff';
        document.getElementById('sdr-freq-display').textContent = '---.--- MHz';
        document.getElementById('sdr-freq-display').style.color = '#555';
        document.getElementById('sdr-mod-badge').textContent = '--';
      }
      // SDR2 status (process_alive covers both — both start/stop together)
      var badge2 = document.getElementById('sdr2-proc-badge');
      if (d.process_alive) {
        badge2.textContent = 'RUNNING'; badge2.style.background = '#27ae60'; badge2.style.color = '#fff';
        document.getElementById('sdr2-freq-display').textContent = parseFloat(d.frequency2 || 0).toFixed(3) + ' MHz';
        document.getElementById('sdr2-freq-display').style.color = '#ff9944';
        document.getElementById('sdr2-mod-badge').textContent = (d.modulation2 || '--').toUpperCase();
      } else {
        badge2.textContent = 'STOPPED'; badge2.style.background = '#c0392b'; badge2.style.color = '#fff';
        document.getElementById('sdr2-freq-display').textContent = '---.--- MHz';
        document.getElementById('sdr2-freq-display').style.color = '#555';
        document.getElementById('sdr2-mod-badge').textContent = '--';
      }
      // Stop button state
      var stopBtn = document.getElementById('sdr-stop-btn');
      stopBtn.disabled = !d.process_alive;
      stopBtn.style.opacity = d.process_alive ? '1' : '0.4';
      // Audio levels
      var lvl = d.process_alive ? (d.audio_level || 0) : 0;
      var pct = Math.min(100, Math.max(0, Math.round(lvl)));
      document.getElementById('sdr-audio-bar').style.width = pct + '%';
      document.getElementById('sdr-audio-val').textContent = pct + '%';
      // SDR2 audio level
      var lvl2 = d.process_alive ? (d.audio_level2 || 0) : 0;
      var pct2 = Math.min(100, Math.max(0, Math.round(lvl2)));
      document.getElementById('sdr2-audio-bar').style.width = pct2 + '%';
      document.getElementById('sdr2-audio-val').textContent = pct2 + '%';
      // Load current settings into form only on first load
      if (initialLoad) {
        loadSettingsToUI(d);
        initialLoad = false;
      }
    })
    .catch(function() {}).finally(function(){ _sdrBusy=false; });
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

            # Cloudflare tunnel URL for display in system status
            if self.gateway and self.gateway.cloudflare_tunnel:
                info['tunnel_url'] = self.gateway.cloudflare_tunnel.get_url() or ''
            else:
                info['tunnel_url'] = ''
        except Exception:
            info['ips'] = []
            info['hostname'] = ''

        return info

    def _generate_dashboard(self):
        """Build the live status dashboard HTML page."""
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        gw_name = str(getattr(self.config, 'GATEWAY_NAME', '') or '').strip()
        _name_html = '<span style="color:#e0e0e0">' + gw_name + '</span> &mdash; ' if gw_name else ''
        body = '<h1 style="font-size:1.8em; margin:0 0 10px">' + _name_html + 'Radio Gateway Dashboard</h1>'
        body += '''

<div id="status">Loading...</div>
<div id="toast-container" style="position:fixed;top:10px;right:10px;z-index:9999;max-width:400px;"></div>

<div id="sysinfo" style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; font-family:monospace; font-size:1.0em; margin-top:10px;">Loading...</div>

<div id="automation-panel" style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; font-family:monospace; font-size:0.95em; margin-top:10px; display:none;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
    <h3 style="margin:0; color:var(--t-accent); font-size:1.1em;">Automation Engine</h3>
    <div id="auto-header-status" style="font-size:0.85em;"></div>
  </div>
  <div id="auto-current" style="margin-bottom:8px; padding:8px; background:rgba(0,0,0,0.2); border-radius:4px; display:none;">
    <div style="color:var(--t-accent); font-weight:bold; margin-bottom:4px;">Now Running</div>
    <div id="auto-current-detail"></div>
  </div>
  <div id="auto-tasks" style="margin-bottom:8px;"></div>
  <details id="auto-history-details" style="background:transparent; border:none; margin:0;">
    <summary style="padding:4px 0; color:var(--t-accent); font-size:0.95em;">Recent History</summary>
    <div id="auto-history" style="max-height:300px; overflow-y:auto; font-size:0.85em; margin-top:4px;"></div>
  </details>
</div>

<div id="adsb-panel" style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; font-family:monospace; font-size:0.95em; margin-top:10px; display:none;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
    <h3 style="margin:0; color:var(--t-accent); font-size:1.1em;">ADS-B Aircraft Tracking</h3>
    <a href="/aircraft" target="content" onclick="setActive(document.querySelector('.shell-nav a[href=\\'/aircraft\\']'))"
       style="font-size:0.85em; color:var(--t-accent); text-decoration:none;">&rarr; Open Map</a>
  </div>
  <div class="st-row" style="margin-bottom:8px;">
    <div class="st-item"><span class="st-label">dump1090-fa:</span><span id="adsb-svc-d1090" class="st-val">&#x25cf;</span></div>
    <div class="st-item"><span class="st-label">Web:</span><span id="adsb-svc-web" class="st-val">&#x25cf;</span></div>
    <div class="st-item"><span class="st-label">fr24feed:</span><span id="adsb-svc-fr24" class="st-val">&#x25cf;</span></div>
  </div>
  <div class="st-row">
    <div class="st-item"><span class="st-label">Aircraft:</span><span id="adsb-aircraft" class="st-val cyan">--</span></div>
    <div class="st-item"><span class="st-label">Messages:</span><span id="adsb-messages" class="st-val white">--</span></div>
    <div class="st-item"><span class="st-label">Rate:</span><span id="adsb-rate" class="st-val green">--</span><span class="st-label"> msg/s</span></div>
  </div>
</div>

<div id="tg-panel" style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; font-family:monospace; font-size:0.95em; margin-top:10px; display:none;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
    <h3 style="margin:0; color:var(--t-accent); font-size:1.1em;">Telegram Bot</h3>
    <span id="tg-bot-name" style="font-size:0.85em; color:#aaa;"></span>
  </div>
  <div class="st-row" style="margin-bottom:8px;">
    <div class="st-item"><span class="st-label">Bot:</span><span id="tg-dot-bot" class="st-val">&#x25cf;</span></div>
    <div class="st-item"><span class="st-label">Claude tmux:</span><span id="tg-dot-tmux" class="st-val">&#x25cf;</span> <button onclick="openTmux()" style="font-size:0.75em; padding:2px 8px; margin-left:4px; cursor:pointer; background:var(--t-border); color:#fff; border:1px solid #555; border-radius:3px;" title="Open terminal attached to Claude tmux session">Open</button></div>
    <div class="st-item"><span class="st-label">Session:</span><span id="tg-session" class="st-val white">--</span></div>
  </div>
  <div class="st-row" style="margin-bottom:6px;">
    <div class="st-item"><span class="st-label">Today:</span><span id="tg-msgs-today" class="st-val cyan">--</span><span class="st-label"> msgs</span></div>
    <div class="st-item"><span class="st-label">Last in:</span><span id="tg-last-in" class="st-val white">--</span></div>
    <div class="st-item"><span class="st-label">Last out:</span><span id="tg-last-out" class="st-val green">--</span></div>
  </div>
  <div style="font-size:0.85em; color:#aaa; margin-top:4px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;" id="tg-last-text"></div>
</div>

<div id="usbip-panel" style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; padding:14px; font-family:monospace; font-size:0.95em; margin-top:10px; display:none;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
    <h3 style="margin:0; color:var(--t-accent); font-size:1.1em;">USB/IP Remote Devices</h3>
    <span id="usbip-server-label" style="font-size:0.85em; color:#aaa;"></span>
  </div>
  <div id="usbip-status-row" class="st-row" style="margin-bottom:8px;">
    <div class="st-item"><span class="st-label">Server:</span><span id="usbip-server-dot" class="st-val">&#x25cf;</span></div>
  </div>
  <div id="usbip-devices-list" style="margin-top:6px; font-size:0.9em; color:#ccc;">No devices</div>
</div>

<style>
  #status { background: var(--t-panel); border: 1px solid var(--t-border); border-radius: 6px; padding: 10px; font-family: monospace; font-size: 1.0em; }
  .st-row { display: grid; grid-template-columns: repeat(auto-fill, 240px); gap: 10px 16px; margin: 8px 0; }
  .st-item { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
  .st-label { color: #888; display: inline-block; width: 5.5em; text-align: right; margin-right: 16px; flex-shrink: 0; }
  .st-val { font-weight: bold; }
  .bar { display: inline-block; height: 18px; border-radius: 3px; min-width: 4px; vertical-align: middle; }
  .bar-rx { background: #2ecc71; } .bar-tx { background: #e74c3c; }
  .bar-sdr1 { background: var(--t-accent); } .bar-sdr2 { background: #e056a0; }
  .bar-sv { background: #f1c40f; } .bar-cl { background: #2ecc71; }
  .bar-sp { background: var(--t-accent); } .bar-an { background: #e74c3c; }
  .bar-d75 { background: #f39c12; }
  .bar-kv4p { background: #1abc9c; }
  .bar-pct { display: inline-block; width: 3.5em; text-align: right; color: #ccc; }
  .green { color: #2ecc71; } .red { color: #e74c3c; } .yellow { color: #f39c12; }
  @keyframes blink { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .cyan { color: var(--t-accent); } .white { color: #e0e0e0; }
  input[type="range"] { accent-color: var(--t-accent); }
  .tgl input { accent-color: var(--t-accent); }
</style>

<script>
function openTmux() {
  fetch('/open_tmux', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
}

var _lastNotifSeq = 0;
function showToast(msg, level) {
  var c = document.getElementById('toast-container');
  if (!c) return;
  var colors = {error:'#e74c3c',warning:'#f39c12',info:'#3498db'};
  var d = document.createElement('div');
  d.style.cssText = 'background:'+(colors[level]||colors.error)+';color:#fff;padding:10px 16px;margin-bottom:6px;border-radius:6px;font-size:0.9em;font-family:monospace;opacity:1;transition:opacity 0.5s;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,0.3);';
  d.textContent = msg;
  d.onclick = function(){ d.style.opacity='0'; setTimeout(function(){ d.remove(); }, 500); };
  c.appendChild(d);
  setTimeout(function(){ d.style.opacity='0'; setTimeout(function(){ d.remove(); }, 500); }, 8000);
}

var _statusBusy = false;
function updateStatus() {
  if (_statusBusy) return;
  _statusBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 10000);
  fetch('/status', {signal:_ac.signal}).then(r=>r.json()).then(function(s) {
    _lostCount = 0;
    // Audio levels now in shell bars (always visible above iframe)
    var h = '';

    h += '<div class="st-row info-row">';
    h += '<div class="st-item"><span class="st-label">Mumble:</span><span class="st-val '+(s.mumble?'green':'red')+'">'+(s.mumble?'OK':'DOWN')+'</span></div>';
    h += '<div class="st-item"><span class="st-label">PTT:</span><span class="st-val '+(s.ptt_active?'red':'green')+'">'+(s.ptt_active?'ON':'off')+'</span> <span class="st-label">('+s.ptt_method+')</span></div>';
    h += '<div class="st-item"><span class="st-label">VAD:</span><span class="st-val '+(s.vad_enabled?'green':'red')+'">'+(s.vad_enabled?'ON':'off')+'</span> <span class="st-val yellow">'+s.vad_db+'dB</span></div>';
    h += '<div class="st-item"><span class="st-label">Vol:</span><span class="st-val yellow">'+s.volume+'x</span></div>';
    if(s.radio_proc && s.radio_proc.length) h += '<div class="st-item"><span class="st-label">Radio:</span><span class="st-val yellow">['+s.radio_proc.join(',')+']</span></div>';
    if(s.sdr_proc && s.sdr_proc.length) h += '<div class="st-item"><span class="st-label">SDR:</span><span class="st-val cyan">['+s.sdr_proc.join(',')+']</span></div>';
    if(s.d75_proc && s.d75_proc.length) h += '<div class="st-item"><span class="st-label">D75:</span><span class="st-val yellow">['+s.d75_proc.join(',')+']</span></div>';
    if(s.kv4p_proc && s.kv4p_proc.length) h += '<div class="st-item"><span class="st-label">KV4P:</span><span class="st-val yellow">['+s.kv4p_proc.join(',')+']</span></div>';
    var mutes = [];
    if(s.tx_muted) mutes.push('TX');
    if(s.rx_muted) mutes.push('RX');
    if(s.sdr1_muted) mutes.push('SDR1');
    if(s.sdr2_muted) mutes.push('SDR2');
    if(s.remote_muted) mutes.push('Remote');
    if(s.announce_muted) mutes.push('Announce');
    if(s.speaker_muted && s.speaker_enabled) mutes.push('Speaker');
    if(s.d75_muted) mutes.push('D75');
    if(s.kv4p_muted) mutes.push('KV4P');
    h += '<div class="st-item"><span class="st-label">Muted:</span><span class="st-val '+(mutes.length?'red':'green')+'">'+(mutes.length?mutes.join(', '):'None')+'</span></div>';
    if(s.sdr1_enabled && s.sdr1_duck) h += '<div class="st-item"><span class="st-label">Duck:</span><span class="st-val green">ON</span></div>';
    if(s.sdr1_enabled && s.sdr_rebroadcast) h += '<div class="st-item"><span class="st-label">Rebroadcast:</span><span class="st-val yellow">ON</span></div>';
    h += '<div class="st-item"><span class="st-label">Manual PTT:</span><span class="st-val '+(s.manual_ptt?'red':'green')+'">'+(s.manual_ptt?'ON':'off')+'</span></div>';
    if(s.tx_talkback) h += '<div class="st-item"><span class="st-label">TX Talkback:</span><span class="st-val yellow">ON</span></div>';
    if(s.ms1_state) h += '<div class="st-item"><span class="st-label">MS1:</span><span class="st-val '+(s.ms1_state==='running'?'green':s.ms1_state==='error'?'red':'white')+'">'+(s.ms1_state==='running'?'ON':'OFF')+'</span></div>';
    if(s.ms2_state) h += '<div class="st-item"><span class="st-label">MS2:</span><span class="st-val '+(s.ms2_state==='running'?'green':s.ms2_state==='error'?'red':'white')+'">'+(s.ms2_state==='running'?'ON':'OFF')+'</span></div>';
    if(s.cat_enabled) h += '<div class="st-item"><span class="st-label">CAT:</span><span class="st-val '+(s.cat==='active'?'red':s.cat==='idle'?'green':'white')+'">'+(s.cat==='active'||s.cat==='idle'?'ON':'OFF')+'</span></div>';
    if(s.d75_enabled) { var _d75a = s.d75_mode==='bluetooth' ? (s.d75_audio_connected?' <span class="st-val green">BT Audio</span>':' <span class="st-val red">No BT Audio</span>') : ' <span class="st-val yellow">USB/AIOC</span>'; h += '<div class="st-item"><span class="st-label">D75:</span><span class="st-val '+(s.d75_connected?'green':'red')+'">'+(s.d75_connected?'ON':'OFF')+'</span>'+_d75a+'</div>'; }
    if(s.relay_radio_enabled) h += '<div class="st-item"><span class="st-label">PWRB:</span><span class="st-val '+(s.relay_pressing?'red':'green')+'">'+(s.relay_pressing?'ON':'off')+'</span></div>';
    h += '</div>';

    // Timers row: uptime + smart announce countdowns
    h += '<div class="st-row timer-row">';
    h += '<div class="st-item"><span class="st-label">Uptime:</span><span class="st-val cyan">'+s.uptime+'</span></div>';
    for(var i=0;i<s.smart_countdowns.length;i++) {
      var sc=s.smart_countdowns[i];
      var scClr=sc.mode==='manual'?'cyan':'yellow';
      h += '<div class="st-item"><span class="st-label">Smart#'+sc.id+':</span><span class="st-val '+scClr+'">'+sc.remaining+'</span></div>';
    }
    if(s.ddns) h += '<div class="st-item"><span class="st-label">DDNS:</span><span class="st-val green">'+s.ddns+'</span></div>';
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

    // Sync radio volume sliders with actual values from CAT
    if(s.cat_vol) {
      var lv=document.getElementById('l-vol'), rv=document.getElementById('r-vol');
      var lvt=document.getElementById('l-vol-val'), rvt=document.getElementById('r-vol-val');
      if(lv && !lv.matches(':active')) { lv.value=s.cat_vol.left; if(lvt) lvt.textContent=s.cat_vol.left; }
      if(rv && !rv.matches(':active')) { rv.value=s.cat_vol.right; if(rvt) rvt.textContent=s.cat_vol.right; }
    }
    // Show new notifications as toasts
    if (s.notifications && s.notifications.length) {
      for (var i=0; i<s.notifications.length; i++) {
        var n = s.notifications[i];
        if (n.seq > _lastNotifSeq) {
          _lastNotifSeq = n.seq;
          showToast(n.msg, n.level);
        }
      }
    }
  }).catch(function(){ _lostCount++; if(_lostCount>=5){_lost=true; document.getElementById('status').innerHTML='<span class="red">Gateway offline — waiting for restart...</span>';} }).finally(function(){ _statusBusy=false; });
}

var _lost = false;
var _lostCount = 0;
setInterval(function() {
  if(_lost) {
    fetch('/status').then(function(r){ if(r.ok){ _lost=false; _lostCount=0; window.location.reload(); } }).catch(function(){});
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
  document.getElementById('play-btn').style.borderColor = _T.btnBorder;
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
      btn.style.color = _T.accent;
      btn.style.borderColor = _T.accent;
      ind.style.display = 'inline-block';
      st.innerHTML = '<span style="color:var(--t-accent)">0:00</span>';
      _wsTimer = setInterval(function() {
        var secs = Math.floor((Date.now() - _wsStart) / 1000);
        var m = Math.floor(secs / 60);
        var s = secs % 60;
        var t = m + ':' + (s < 10 ? '0' : '') + s;
        var kb = (_wsBytes / 1024).toFixed(0);
        var unit = 'KB';
        if (_wsBytes >= 1048576) { kb = (_wsBytes / 1048576).toFixed(1); unit = 'MB'; }
        var kbps = secs > 0 ? ((_wsBytes * 8 / secs / 1000).toFixed(0) + 'kbps') : '';
        st.innerHTML = '<span style="color:var(--t-accent)">' + t + '</span> <span style="color:#666">' + kb + unit + (kbps ? ' ' + kbps : '') + '</span>';
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
  document.getElementById('ws-btn').style.borderColor = _T.btnBorder;
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
var _sysinfoBusy = false;
function updateSysInfo() {
  if (_sysinfoBusy) return;
  _sysinfoBusy = true;
  var _ac2 = new AbortController(); setTimeout(function(){_ac2.abort();}, 5000);
  fetch('/sysinfo', {signal:_ac2.signal}).then(function(r){return r.json()}).then(function(s) {
    var h = '<div class="st-row">';
    h += '<div class="st-item"><span class="st-label">CPU:</span>'+sysBar(s.cpu_pct)+'</div>';
    h += '<div class="st-item"><span class="st-label">Load:</span><span class="st-val cyan">'+s.load[0]+' '+s.load[1]+' '+s.load[2]+'</span></div>';
    h += '<div class="st-item"><span class="st-label">RAM:</span>'+sysBar(s.mem_pct, _T.accent)+'<span class="st-label">'+s.mem_used_mb+'/'+s.mem_total_mb+'MB</span></div>';
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
      if (s.tunnel_url) h += '<div class="st-item"><span class="st-label">CF:</span><span class="st-val green"><a href="'+s.tunnel_url+'" target="_blank" style="color:#2ecc71; text-decoration:none;">'+s.tunnel_url.replace('https://','').replace('.trycloudflare.com','')+'</a></span></div>';
      h += '</div>';
    }
    document.getElementById('sysinfo').innerHTML = h;
  }).catch(function(){}).finally(function(){ _sysinfoBusy=false; });
}
setInterval(updateSysInfo, 2000);
updateSysInfo();

// --- Automation Engine ---
var _autoBusy = false, _autoHistBusy = false;
function updateAutomation() {
  if (_autoBusy) return;
  _autoBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 5000);
  fetch('/automationstatus', {signal:_ac.signal}).then(r=>r.json()).then(function(a) {
    var panel = document.getElementById('automation-panel');
    if (!a.enabled) { panel.style.display='none'; return; }
    panel.style.display='';

    // Header status
    var hdr = '<span class="'+(a.running?'green':'red')+'">'+(a.running?'Running':'Stopped')+'</span>';
    hdr += ' &mdash; '+a.tasks.length+' tasks, '+a.repeater_count+' repeaters';
    if (a.radios.length) hdr += ', radios: '+a.radios.join(', ');
    else hdr += ', <span class="red">no radios</span>';
    if (a.recording) hdr += ' &mdash; <span class="red" style="animation:blink 1s infinite">REC</span>';
    document.getElementById('auto-header-status').innerHTML = hdr;

    // Current task
    var curDiv = document.getElementById('auto-current');
    var curDetail = document.getElementById('auto-current-detail');
    if (a.current_task) {
      curDiv.style.display = '';
      var ct = null;
      for (var i=0; i<a.tasks.length; i++) { if (a.tasks[i].name===a.current_task) { ct=a.tasks[i]; break; } }
      var d = '<span style="color:#fff">'+a.current_task+'</span>';
      if (ct) d += ' &mdash; <span class="yellow">'+ct.action+'</span> on <span class="cyan">'+ct.radio+'</span>';
      if (a.recording) d += ' <span class="red">[RECORDING]</span>';
      curDetail.innerHTML = d;
    } else {
      curDiv.style.display = 'none';
    }

    // Task queue
    var t = '<table style="width:100%; border-collapse:collapse; font-size:0.9em;">';
    t += '<tr style="color:#888; text-align:left;"><th style="padding:2px 8px;">Task</th><th style="padding:2px 8px;">Action</th><th style="padding:2px 8px;">Radio</th><th style="padding:2px 8px;">Next</th><th style="padding:2px 8px;">Last</th></tr>';
    for (var i=0; i<a.tasks.length; i++) {
      var tk = a.tasks[i];
      var active = (tk.name === a.current_task);
      var row_style = active ? 'background:rgba(46,204,113,0.15);' : '';
      var next = tk.schedule_type==='interval' ? fmtSecs(tk.next_run_secs) : ('at '+tk.at+(tk.last_run_date?' (done)':''));
      var last = tk.last_run_ago !== null ? fmtSecs(tk.last_run_ago)+' ago' : '--';
      t += '<tr style="'+row_style+'">';
      t += '<td style="padding:2px 8px; color:#fff;">'+(active?'&#9654; ':'')+tk.name+'</td>';
      t += '<td style="padding:2px 8px; color:#f39c12;">'+tk.action+'</td>';
      t += '<td style="padding:2px 8px; color:var(--t-accent);">'+tk.radio+'</td>';
      t += '<td style="padding:2px 8px;">'+next+'</td>';
      t += '<td style="padding:2px 8px; color:#888;">'+last+'</td>';
      t += '</tr>';
    }
    t += '</table>';
    document.getElementById('auto-tasks').innerHTML = t;
  }).catch(function(){}).finally(function(){ _autoBusy=false; });
}
function updateAutoHistory() {
  if (_autoHistBusy) return;
  _autoHistBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 5000);
  fetch('/automationhistory', {signal:_ac.signal}).then(r=>r.json()).then(function(hist) {
    if (!hist.length) { document.getElementById('auto-history').innerHTML='<span style="color:#888;">No completed tasks yet</span>'; return; }
    var h = '<table style="width:100%; border-collapse:collapse;">';
    h += '<tr style="color:#888; text-align:left;"><th style="padding:2px 6px;">Time</th><th style="padding:2px 6px;">Task</th><th style="padding:2px 6px;">Radio</th><th style="padding:2px 6px;">Duration</th><th style="padding:2px 6px;">Result</th></tr>';
    for (var i=0; i<Math.min(hist.length,20); i++) {
      var e = hist[i];
      var res = '';
      if (e.result) {
        if (e.result.error) res = '<span class="red">ERR: '+e.result.error+'</span>';
        else if (e.result.frequency) {
          res = e.result.frequency+' MHz';
          if (e.result.recording) res += ' <span class="green">recorded</span>';
          if (e.result.signal && e.result.signal.has_signal) res += ' <span class="green">signal</span>';
        }
        else if (e.result.scanned) res = e.result.scanned+' scanned, '+e.result.active+' active';
        else if (e.result.text) res = 'TTS: '+e.result.text.substring(0,30)+'...';
      }
      h += '<tr><td style="padding:2px 6px; color:#888;">'+e.time+'</td>';
      h += '<td style="padding:2px 6px; color:#fff;">'+e.task+'</td>';
      h += '<td style="padding:2px 6px; color:var(--t-accent);">'+e.radio+'</td>';
      h += '<td style="padding:2px 6px;">'+e.elapsed+'s</td>';
      h += '<td style="padding:2px 6px;">'+res+'</td></tr>';
    }
    h += '</table>';
    document.getElementById('auto-history').innerHTML = h;
  }).catch(function(){}).finally(function(){ _autoHistBusy=false; });
}
function fmtSecs(s) {
  if (s===null||s===undefined) return '--';
  if (s<60) return s+'s';
  if (s<3600) return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
setInterval(updateAutomation, 2000);
setInterval(updateAutoHistory, 5000);
updateAutomation();
updateAutoHistory();

// --- ADS-B Status ---
var _adsbBusy = false;
function updateAdsb() {
  if (_adsbBusy) return;
  _adsbBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 5000);
  fetch('/adsbstatus', {signal:_ac.signal}).then(function(r){return r.json();}).then(function(d) {
    var panel = document.getElementById('adsb-panel');
    if (!d.enabled) { panel.style.display='none'; return; }
    panel.style.display='';
    function svcDot(el, ok) {
      el.textContent = '\u25cf';
      el.style.color = ok ? '#2ecc71' : '#e74c3c';
      el.title = ok ? 'running' : 'stopped';
    }
    svcDot(document.getElementById('adsb-svc-d1090'), d.dump1090);
    svcDot(document.getElementById('adsb-svc-web'), d.web);
    svcDot(document.getElementById('adsb-svc-fr24'), d.fr24feed);
    document.getElementById('adsb-aircraft').textContent = d.aircraft;
    document.getElementById('adsb-messages').textContent = d.messages.toLocaleString();
    document.getElementById('adsb-rate').textContent = d.messages_rate > 0 ? d.messages_rate.toFixed(1) : '0.0';
  }).catch(function(){}).finally(function(){ _adsbBusy=false; });
}
setInterval(updateAdsb, 3000);
updateAdsb();

// --- USB/IP Status ---
var _usbipBusy = false;
function updateUsbip() {
  if (_usbipBusy) return;
  _usbipBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 5000);
  fetch('/usbipstatus', {signal:_ac.signal}).then(function(r){return r.json();}).then(function(d) {
    var panel = document.getElementById('usbip-panel');
    if (!d.enabled) { panel.style.display='none'; return; }
    panel.style.display='';
    document.getElementById('usbip-server-label').textContent = d.server || '';
    var dot = document.getElementById('usbip-server-dot');
    dot.textContent = '\u25cf';
    dot.style.color = d.server_reachable ? '#2ecc71' : '#e74c3c';
    dot.title = d.server_reachable ? 'reachable' : (d.last_error || 'unreachable');
    var list = document.getElementById('usbip-devices-list');
    if (!d.devices || d.devices.length === 0) {
      list.textContent = d.server_reachable ? 'No exported devices' : 'Server unreachable';
    } else {
      list.innerHTML = d.devices.map(function(dev) {
        var dot = '<span style="color:' + (dev.attached ? '#2ecc71' : '#e74c3c') + '">&#x25cf;</span>';
        var status = dev.attached ? 'attached' : 'not attached';
        return dot + ' <b>' + dev.bus_id + '</b> &nbsp;' + dev.description + ' <span style="color:#888;font-size:0.85em;">(' + status + ')</span>';
      }).join('<br>');
    }
  }).catch(function(){}).finally(function(){ _usbipBusy=false; });
}
setInterval(updateUsbip, 10000);
updateUsbip();

// --- Telegram Bot Status ---
var _tgBusy = false;
function updateTelegram() {
  if (_tgBusy) return;
  _tgBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 5000);
  fetch('/telegramstatus', {signal:_ac.signal}).then(function(r){return r.json();}).then(function(d) {
    var panel = document.getElementById('tg-panel');
    if (!d.enabled) { panel.style.display='none'; return; }
    panel.style.display='';
    function dot(id, ok, title) {
      var el = document.getElementById(id);
      el.textContent = '\u25cf';
      el.style.color = ok ? '#2ecc71' : '#e74c3c';
      el.title = title || (ok ? 'ok' : 'offline');
    }
    dot('tg-dot-bot', d.bot_running, d.bot_running ? 'bot running' : 'bot not running');
    dot('tg-dot-tmux', d.tmux_active, d.tmux_active ? 'session active' : 'session not found');
    document.getElementById('tg-bot-name').textContent = d.bot_username || '';
    document.getElementById('tg-session').textContent = d.tmux_session || '--';
    document.getElementById('tg-msgs-today').textContent = d.messages_today != null ? d.messages_today : '--';
    function fmtTime(ts) {
      if (!ts) return '--';
      try { return new Date(ts).toLocaleTimeString(); } catch(e) { return ts.slice(11,19) || '--'; }
    }
    document.getElementById('tg-last-in').textContent = fmtTime(d.last_message_time);
    document.getElementById('tg-last-out').textContent = fmtTime(d.last_reply_time);
    document.getElementById('tg-last-text').textContent = d.last_message_text ? '\u00bb ' + d.last_message_text : '';
  }).catch(function(){}).finally(function(){ _tgBusy=false; });
}
setInterval(updateTelegram, 5000);
updateTelegram();

</script>
'''
        return self._wrap_html('Dashboard', body)

    def _generate_controls_page(self):
        """Build the controls page HTML (moved from dashboard)."""
        body = '''
<h1 style="font-size:1.6em; margin:0 0 10px;">Controls</h1>

<div class="controls">
  <div class="ctrl-group" id="mute-group">
    <h3>Mute Controls</h3>
    <button onclick="sendKey('m')" id="btn-m">Global</button>
    <button onclick="sendKey('r')" id="btn-r">RX</button>
    <button onclick="sendKey('t')" id="btn-t">TX</button>
    <button onclick="sendKey('w')" id="btn-w">D75</button>
    <button onclick="sendKey('y')" id="btn-y">KV4P</button>
    <button onclick="sendKey('s')" id="btn-s">SDR1</button>
    <button onclick="sendKey('x')" id="btn-x">SDR2</button>
    <button onclick="sendKey('c')" id="btn-c">Remote</button>
    <button onclick="sendKey('a')" id="btn-a">Announce</button>
    <button onclick="sendKey('o')" id="btn-o">Speaker</button>
  </div>
  <div class="ctrl-group" id="radio-proc-group">
    <h3>Radio Processing</h3>
    <button onclick="togProc('radio','gate')" id="btn-rp-gate">Gate</button>
    <button onclick="togProc('radio','hpf')" id="btn-rp-hpf">HPF</button>
    <button onclick="togProc('radio','lpf')" id="btn-rp-lpf">LPF</button>
    <button onclick="togProc('radio','notch')" id="btn-rp-notch">Notch</button>
  </div>
  <div class="ctrl-group" id="audio-group">
    <h3>Audio</h3>
    <button onclick="sendKey('v')" id="btn-v">VAD Toggle</button>
    <button onclick="sendKey(',')">,  Vol-</button>
    <button onclick="sendKey('.')">. Vol+</button>
  </div>
  <div class="ctrl-group" id="sdr-proc-group">
    <h3>SDR Processing</h3>
    <button onclick="togProc('sdr','gate')" id="btn-sp-gate">Gate</button>
    <button onclick="togProc('sdr','hpf')" id="btn-sp-hpf">HPF</button>
    <button onclick="togProc('sdr','lpf')" id="btn-sp-lpf">LPF</button>
    <button onclick="togProc('sdr','notch')" id="btn-sp-notch">Notch</button>
  </div>
  <div class="ctrl-group" id="d75-proc-group">
    <h3>D75 Processing</h3>
    <button onclick="togProc('d75','gate')" id="btn-dp-gate">Gate</button>
    <button onclick="togProc('d75','hpf')" id="btn-dp-hpf">HPF</button>
    <button onclick="togProc('d75','lpf')" id="btn-dp-lpf">LPF</button>
    <button onclick="togProc('d75','notch')" id="btn-dp-notch">Notch</button>
  </div>
  <div class="ctrl-group" id="kv4p-proc-group">
    <h3>KV4P Processing</h3>
    <button onclick="togProc('kv4p','gate')" id="btn-kp-gate">Gate</button>
    <button onclick="togProc('kv4p','hpf')" id="btn-kp-hpf">HPF</button>
    <button onclick="togProc('kv4p','lpf')" id="btn-kp-lpf">LPF</button>
    <button onclick="togProc('kv4p','notch')" id="btn-kp-notch">Notch</button>
  </div>
  <div class="ctrl-group" id="sdr-ctrl-group">
    <h3>SDR</h3>
    <button onclick="sendKey('d')" id="btn-d">Duck Toggle</button>
    <button onclick="sendKey('b')" id="btn-b">Rebroadcast</button>
  </div>
</div>

<div id="playback-section" style="margin-top:10px; display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start;">
  <div class="ctrl-group" style="min-width:0; display:inline-block;" id="playback-group">
    <h3 style="margin:0 0 10px; color:var(--t-accent); font-size:1.1em;">Playback</h3>
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
  <div class="ctrl-group bottom-btns" style="min-width:0;" id="smart-announce-group">
    <h3>Smart Announce</h3>
    <div style="display:flex; flex-direction:column; gap:3px; margin-bottom:6px;">
      <button onclick="sendKey('[')">Smart #1</button>
      <button onclick="sendKey(']')">Smart #2</button>
      <button onclick="sendKey(String.fromCharCode(92))">Smart #3</button>
    </div>
    <div id="smart-status" style="font-family:monospace; font-size:0.85em; color:#888;">Idle</div>
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
  <div class="ctrl-group bottom-btns" style="width:fit-content;" id="ptt-relay-group">
    <h3>PTT</h3>
    <div style="display:flex; gap:10px; align-items:flex-start;">
      <div style="display:flex; flex-direction:column; gap:3px;">
        <button onclick="sendKey('p')" id="btn-p">Manual PTT</button>
        <button onclick="togFlag('talkback')" id="btn-talkback">TX Talkback</button>
        <button onclick="sendKey('j')" id="btn-j">Radio Power</button>
        <button onclick="sendKey('h')" id="btn-h">Charger Toggle</button>
      </div>
      <div style="display:flex; flex-direction:column; align-items:center; gap:6px;">
        <div style="display:flex; align-items:center; gap:6px; width:100%;">
          <label style="color:#888; font-size:0.8em; white-space:nowrap;">Radio</label>
          <select id="webmic-radio" onchange="setWebTxRadio(this.value)"
            style="flex:1; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:3px 5px; font-size:0.85em;">
            <option value="th9800">TH9800</option>
            <option value="d75">D75</option>
            <option value="kv4p">KV4P</option>
          </select>
        </div>
        <div id="db-mic-level" style="width:70px; height:5px; background:var(--t-btn); border-radius:3px; overflow:hidden;">
          <div id="db-mic-level-bar" style="height:100%; width:0%; background:#2ecc71; transition:width 0.1s;"></div>
        </div>
        <button id="db-mic-ptt-btn" style="width:70px; height:70px; font-size:1.1em; font-weight:bold; border-radius:50%; background:#1a3a1a; border:2px solid #2ecc71; color:#e0e0e0; cursor:pointer;"
          onclick="dbMicPTTToggle()">MIC<br>PTT</button>
        <span id="db-mic-status" style="color:#888; font-size:0.75em;">Ready</span>
      </div>
    </div>
  </div>
  <div class="ctrl-group" style="min-width:300px; width:300px;" id="aitext-group">
    <h3>Text to AI</h3>
    <div style="display:flex; flex-direction:column; gap:4px;">
      <textarea id="ai-text" rows="3" style="width:100%; box-sizing:border-box; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em; resize:vertical;" placeholder="Enter prompt for AI to research and speak..."></textarea>
      <div style="display:flex; gap:4px; align-items:center;">
        <label style="color:#888; font-size:0.85em; white-space:nowrap; width:4em;">Top text</label>
        <input id="ai-top" type="text" value="QST" style="flex:1; min-width:0; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:4px 6px; font-family:monospace; font-size:0.9em;">
      </div>
      <div style="display:flex; gap:4px; align-items:center;">
        <label style="color:#888; font-size:0.85em; white-space:nowrap; width:4em;">Tail</label>
        <input id="ai-tail" type="text" value="Callsign" style="flex:1; min-width:0; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:4px 6px; font-family:monospace; font-size:0.9em;">
      </div>
      <div style="display:flex; gap:4px; align-items:center; flex-wrap:wrap;">
        <label style="color:#888; font-size:0.85em; white-space:nowrap;">Secs</label>
        <input id="ai-secs" type="number" value="30" min="5" max="120" style="width:55px; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:4px 6px; font-family:monospace; font-size:0.9em;">
        <label style="color:#888; font-size:0.85em; white-space:nowrap;">Voice</label>
        <select id="ai-voice" style="flex:1; min-width:90px; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:4px 6px; font-family:monospace; font-size:0.9em;">
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
      </div>
      <button onclick="sendAIText()" id="btn-ai-send" style="width:100%;">Send to AI</button>
    </div>
    <div id="ai-status" style="font-family:monospace; font-size:0.85em; color:#888; margin-top:6px;">Ready</div>
  </div>
  <div class="ctrl-group" style="min-width:220px; width:220px;" id="cw-group">
    <h3>Text to CW</h3>
    <div style="display:flex; flex-direction:column; gap:4px;">
      <textarea id="cw-text" rows="3" style="width:100%; box-sizing:border-box; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em; resize:vertical;" placeholder="Enter text for CW..."></textarea>
      <div style="display:flex; gap:4px; align-items:center;">
        <label style="color:#888; font-size:0.8em; white-space:nowrap; width:3em;">WPM</label>
        <input id="cw-wpm" type="number" min="5" max="60" value="20" style="width:55px; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:3px 5px; font-size:0.9em;">
      </div>
      <div style="display:flex; gap:4px; align-items:center;">
        <label style="color:#888; font-size:0.8em; white-space:nowrap; width:3em;">Freq</label>
        <input id="cw-freq" type="number" min="200" max="1200" value="600" style="width:55px; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:3px 5px; font-size:0.9em;">
        <span style="color:#888; font-size:0.8em;">Hz</span>
      </div>
      <div style="display:flex; gap:4px; align-items:center;">
        <label style="color:#888; font-size:0.8em; white-space:nowrap; width:3em;">Vol</label>
        <input id="cw-vol" type="number" min="0.1" max="2.0" step="0.1" value="1.0" style="width:55px; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:3px 5px; font-size:0.9em;">
      </div>
      <button onclick="sendCW()" id="btn-cw-send" style="width:100%;">Send CW</button>
    </div>
    <div id="cw-status" style="font-family:monospace; font-size:0.85em; color:#888; margin-top:6px;">Ready</div>
  </div>
  <div class="ctrl-group" style="min-width:280px; width:280px;" id="tts-group">
    <h3>Text to Speech</h3>
    <div style="display:flex; flex-direction:column; gap:3px;">
      <textarea id="tts-text" rows="3" style="width:100%; box-sizing:border-box; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em; resize:vertical;" placeholder="Enter text to speak..."></textarea>
      <div style="display:flex; gap:3px; align-items:center;">
        <select id="tts-voice" style="flex:1; background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace; font-size:0.95em;">
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
</div>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
  .ctrl-group { background: var(--t-panel); border: 1px solid var(--t-border); border-radius: 6px; padding: 10px; min-width: 220px; }
  .ctrl-group h3 { margin: 0 0 10px; color: var(--t-accent); font-size: 1.1em; }
  .ctrl-group button { padding: 10px 18px; margin: 3px; border: 1px solid var(--t-btn-border); border-radius: 4px;
    background: var(--t-btn); color: #e0e0e0; cursor: pointer; font-family: monospace; font-size: 1.05em; }
  .ctrl-group button:hover { background: var(--t-btn-hover); }
  .ctrl-group button:active { background: var(--t-btn-active); }
  .ctrl-group button.active { background: var(--t-btn-active); border-color: var(--t-accent); color: var(--t-accent); }
  .ctrl-group button.muted { background: #5c1a1a; border-color: #c0392b; color: #ff6b6b; }
  #status { background: var(--t-panel); border: 1px solid var(--t-border); border-radius: 6px; padding: 10px; font-family: monospace; font-size: 1.0em; }
  .st-row { display: grid; grid-template-columns: repeat(auto-fill, 240px); gap: 10px 16px; margin: 8px 0; }
  .st-item { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
  .st-label { color: #888; display: inline-block; width: 5.5em; text-align: right; margin-right: 16px; flex-shrink: 0; }
  .st-val { font-weight: bold; }
  .bar { display: inline-block; height: 18px; border-radius: 3px; min-width: 4px; vertical-align: middle; }
  .bar-pct { display: inline-block; width: 3.5em; text-align: right; color: #ccc; }
  .green { color: #2ecc71; } .red { color: #e74c3c; } .yellow { color: #f39c12; }
  .cyan { color: var(--t-accent); } .white { color: #e0e0e0; }
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
function togFlag(flag) {
  fetch('/mixer', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'flag', flag:flag})});
}
function darkiceCmd(cmd) {
  fetch('/darkicecmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cmd:cmd})});
}
function openTmux() {
  fetch('/open_tmux', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
}
function sendAIText() {
  var text = document.getElementById('ai-text').value.trim();
  if (!text) return;
  var btn = document.getElementById('btn-ai-send');
  var st = document.getElementById('ai-status');
  btn.disabled = true;
  st.textContent = 'Submitted \\u2014 processing...';
  st.style.color = '#f1c40f';
  var payload = {
    text: text,
    target_secs: parseInt(document.getElementById('ai-secs').value) || 30,
    voice: parseInt(document.getElementById('ai-voice').value) || 1,
    top_text: document.getElementById('ai-top').value.trim(),
    tail_text: document.getElementById('ai-tail').value.trim(),
  };
  fetch('/aitext', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.ok) { st.textContent = 'Running \\u2014 check Smart Announce status'; st.style.color = '#2ecc71'; }
      else { st.textContent = 'Error: ' + (d.error||'failed'); st.style.color = '#e74c3c'; }
      btn.disabled = false;
      setTimeout(function(){ st.textContent = 'Ready'; st.style.color = '#888'; }, 15000);
    })
    .catch(function(e){ st.textContent = 'Network error'; st.style.color = '#e74c3c'; btn.disabled = false; });
  if(document.activeElement) document.activeElement.blur();
}
function sendCW() {
  var text = document.getElementById('cw-text').value.trim();
  if (!text) return;
  var btn = document.getElementById('btn-cw-send');
  var st = document.getElementById('cw-status');
  var wpm  = parseFloat(document.getElementById('cw-wpm').value)  || 20;
  var freq = parseFloat(document.getElementById('cw-freq').value) || 600;
  var vol  = parseFloat(document.getElementById('cw-vol').value)  || 1.0;
  btn.disabled = true;
  st.textContent = 'Sending...';
  st.style.color = '#f1c40f';
  fetch('/cw', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text, wpm:wpm, freq:freq, vol:vol})})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.ok) { st.textContent = 'Sent \\u2014 playing'; st.style.color = '#2ecc71'; }
      else { st.textContent = 'Error: ' + (d.error||'failed'); st.style.color = '#e74c3c'; }
      btn.disabled = false;
      setTimeout(function(){ st.textContent = 'Ready'; st.style.color = '#888'; }, 5000);
    })
    .catch(function(e){ st.textContent = 'Network error'; st.style.color = '#e74c3c'; btn.disabled = false; });
  if(document.activeElement) document.activeElement.blur();
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
      if(d.ok) { st.textContent = 'Sent \\u2014 playing'; st.style.color = '#2ecc71'; }
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

function bar(pct, cls, ducked) {
  var w = Math.round(Math.min(Math.max(pct, 0), 100));
  var p = pct < 10 ? '  '+pct : pct < 100 ? ' '+pct : ''+pct;
  var pctCol = ducked ? '#e74c3c' : '#2ecc71';
  return '<span class="bar-pct" style="color:'+pctCol+'">'+p+'%</span><span class="bar '+cls+'" style="width:'+w+'px"></span>';
}

function setWebTxRadio(radio) {
  fetch('/catcmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:'SET_TX_RADIO', radio:radio})}).catch(function(){});
}

var _dbMicWs=null, _dbMicStream=null, _dbMicCtx=null, _dbMicProc=null, _dbMicActive=false;

function dbMicPTTToggle() {
  if (_dbMicActive) { dbMicCleanup(); return; }
  _dbMicActive = true;
  var btn=document.getElementById('db-mic-ptt-btn'), st=document.getElementById('db-mic-status');
  btn.style.background='#c0392b'; btn.style.borderColor='#e74c3c'; btn.style.color='#fff';
  st.textContent='Connecting...'; st.style.color='#f39c12';
  navigator.mediaDevices.getUserMedia(
    {audio:{sampleRate:48000,channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}}
  ).then(function(stream) {
    _dbMicStream = stream;
    var proto = location.protocol==='https:' ? 'wss:' : 'ws:';
    _dbMicWs = new WebSocket(proto+'//'+location.host+'/ws_mic');
    _dbMicWs.binaryType = 'arraybuffer';
    _dbMicWs.onopen = function() {
      st.textContent='TX \\u2014 click to stop'; st.style.color='#e74c3c';
      _dbMicCtx = new AudioContext({sampleRate:48000});
      var src = _dbMicCtx.createMediaStreamSource(stream);
      var proc = _dbMicCtx.createScriptProcessor(2048,1,1);
      proc.onaudioprocess = function(e) {
        if (!_dbMicWs || _dbMicWs.readyState!==1) return;
        var f32=e.inputBuffer.getChannelData(0), buf=new ArrayBuffer(f32.length*2), i16=new Int16Array(buf), peak=0;
        for (var i=0;i<f32.length;i++) { var s=Math.max(-1,Math.min(1,f32[i])); i16[i]=s<0?s*32768:s*32767; if(Math.abs(f32[i])>peak)peak=Math.abs(f32[i]); }
        _dbMicWs.send(buf);
        document.getElementById('db-mic-level-bar').style.width=Math.min(100,Math.round(peak*100))+'%';
      };
      src.connect(proc); proc.connect(_dbMicCtx.destination); _dbMicProc=proc;
    };
    _dbMicWs.onerror=function(){dbMicCleanup();};
    _dbMicWs.onclose=function(){dbMicCleanup();};
  }).catch(function(){
    _dbMicActive=false;
    document.getElementById('db-mic-ptt-btn').style.background='#1a3a1a';
    document.getElementById('db-mic-ptt-btn').style.borderColor='#2ecc71';
    document.getElementById('db-mic-status').textContent='Mic denied';
    document.getElementById('db-mic-status').style.color='#e74c3c';
  });
}

function dbMicCleanup() {
  _dbMicActive=false;
  if(_dbMicProc){_dbMicProc.disconnect();_dbMicProc=null;}
  if(_dbMicCtx){_dbMicCtx.close().catch(function(){});_dbMicCtx=null;}
  if(_dbMicStream){_dbMicStream.getTracks().forEach(function(t){t.stop();});_dbMicStream=null;}
  if(_dbMicWs){try{_dbMicWs.close();}catch(e){}  _dbMicWs=null;}
  var btn=document.getElementById('db-mic-ptt-btn');
  btn.style.background='#1a3a1a'; btn.style.borderColor='#2ecc71'; btn.style.color='#e0e0e0';
  document.getElementById('db-mic-level-bar').style.width='0%';
  document.getElementById('db-mic-status').textContent='Ready';
  document.getElementById('db-mic-status').style.color='#888';
}

// Initialise radio dropdown from config TX_RADIO
fetch('/catcmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'GET_TX_RADIO'})})
  .then(function(r){return r.json();}).then(function(d){
    if(d.ok && d.radio){var s=document.getElementById('webmic-radio');if(s)s.value=d.radio;}
  }).catch(function(){});

// --- Status polling for button states ---
var _ctrlBusy = false;
function updateControls() {
  if (_ctrlBusy) return;
  _ctrlBusy = true;
  var _ac = new AbortController(); setTimeout(function(){_ac.abort();}, 10000);
  fetch('/status', {signal:_ac.signal}).then(r=>r.json()).then(function(s) {
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
    setBtn('btn-w', s.d75_muted, 'muted');
    setBtn('btn-y', s.kv4p_muted, 'muted');
    setBtn('btn-v', s.vad_enabled, 'active');
    setBtn('btn-p', s.manual_ptt, 'active');
    setBtn('btn-talkback', s.tx_talkback, 'active');
    setBtn('btn-d', s.sdr1_duck, 'active');
    setBtn('btn-b', s.sdr_rebroadcast, 'active');
    // Radio processing buttons
    if(s.radio_proc) {
      setBtn('btn-rp-gate', s.radio_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-rp-hpf', s.radio_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-rp-lpf', s.radio_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-rp-notch', s.radio_proc.indexOf('Notch')>=0, 'active');
    }
    // SDR processing buttons
    if(s.sdr_proc) {
      setBtn('btn-sp-gate', s.sdr_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-sp-hpf', s.sdr_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-sp-lpf', s.sdr_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-sp-notch', s.sdr_proc.indexOf('Notch')>=0, 'active');
    }
    // D75 processing buttons
    if(s.d75_proc) {
      setBtn('btn-dp-gate', s.d75_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-dp-hpf', s.d75_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-dp-lpf', s.d75_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-dp-notch', s.d75_proc.indexOf('Notch')>=0, 'active');
    }
    // KV4P processing buttons
    if(s.kv4p_proc) {
      setBtn('btn-kp-gate', s.kv4p_proc.indexOf('Gate')>=0, 'active');
      setBtn('btn-kp-hpf', s.kv4p_proc.indexOf('HPF')>=0, 'active');
      setBtn('btn-kp-lpf', s.kv4p_proc.indexOf('LPF')>=0, 'active');
      setBtn('btn-kp-notch', s.kv4p_proc.indexOf('Notch')>=0, 'active');
    }
    // Smart announce activity status
    var smSt = document.getElementById('smart-status');
    if(smSt && s.smart_activity) {
      var parts = [];
      for(var sk in s.smart_activity) {
        if(sk === '0') continue;
        var sv = s.smart_activity[sk];
        var sClr = sv==='Done'?'green':sv.startsWith('Error')||sv.startsWith('No ')||sv.startsWith('Dropped')?'red':'yellow';
        parts.push('<span class="'+sClr+'">#'+sk+': '+sv+'</span>');
      }
      smSt.innerHTML = parts.length ? parts.join(' ') : '<span style="color:#888">Idle</span>';
    }
    // Smart announce countdowns
    if(s.smart_countdowns) {
      var cdDiv = document.getElementById('smart-countdowns');
      if(cdDiv) {
        var cdh = '';
        for(var i=0;i<s.smart_countdowns.length;i++) {
          var sc=s.smart_countdowns[i];
          var scClr=sc.mode==='manual'?'cyan':'yellow';
          cdh += '<span class="'+scClr+'">#'+sc.id+': '+sc.remaining+'</span> ';
        }
        cdDiv.innerHTML = cdh || '';
      }
    }
    // Playback file slots
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
    // Hide/show sections based on enabled state
    function showIf(id, cond) { var e=document.getElementById(id); if(e) e.style.display=cond?'':'none'; }
    var hasSDR = s.sdr1_enabled || s.sdr2_enabled;
    showIf('sdr-proc-group', hasSDR);
    showIf('sdr-ctrl-group', hasSDR);
    showIf('playback-group', s.playback_enabled);
    showIf('smart-announce-group', s.smart_announce_enabled);
    showIf('tts-group', s.tts_enabled);
    showIf('btn-s', s.sdr1_enabled);
    showIf('btn-x', s.sdr2_enabled);
    showIf('btn-c', s.remote_enabled);
    showIf('btn-a', s.announce_enabled);
    showIf('btn-o', s.speaker_enabled);
    showIf('btn-j', s.relay_radio_enabled);
    showIf('btn-h', s.relay_charger_enabled);
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
  }).catch(function(){}).finally(function(){ _ctrlBusy=false; });
}
setInterval(updateControls, 1000);
updateControls();
</script>
'''
        return self._wrap_html('Controls', body)

    def _generate_recordings_page(self):
        """Build the recordings manager HTML page."""
        body = '''
<h1 style="font-size:1.6em; margin:0 0 10px;">Recording Manager</h1>

<div style="display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; margin-bottom:10px;">
  <div>
    <label style="color:#888; font-size:0.85em;">Radio</label><br>
    <select id="f-radio" onchange="applyFilter()" style="background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace;">
      <option value="">All</option>
    </select>
  </div>
  <div>
    <label style="color:#888; font-size:0.85em;">Date</label><br>
    <select id="f-date" onchange="applyFilter()" style="background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace;">
      <option value="">All</option>
    </select>
  </div>
  <div>
    <label style="color:#888; font-size:0.85em;">Frequency</label><br>
    <select id="f-freq" onchange="applyFilter()" style="background:var(--t-btn); color:#e0e0e0; border:1px solid var(--t-btn-border); border-radius:4px; padding:6px; font-family:monospace;">
      <option value="">All</option>
    </select>
  </div>
  <div style="margin-left:auto; display:flex; gap:6px;">
    <button onclick="selectAll()" class="rb">Select All</button>
    <button onclick="selectNone()" class="rb">Select None</button>
    <button onclick="downloadSelected()" class="rb" style="background:#1a5c3a;">Download Selected</button>
    <button onclick="deleteSelected()" class="rb" style="background:#5c1a1a;">Delete Selected</button>
    <button onclick="if(confirm('Delete ALL recordings?'))deleteAll()" class="rb" style="background:#8b0000;">Delete All</button>
  </div>
</div>

<div id="rec-summary" style="color:#888; font-size:0.9em; margin-bottom:8px;"></div>

<div style="background:var(--t-panel); border:1px solid var(--t-border); border-radius:6px; overflow:hidden;">
  <table id="rec-table" style="width:100%; border-collapse:collapse; font-family:monospace; font-size:0.9em;">
    <thead>
      <tr style="background:rgba(0,0,0,0.3); text-align:left;">
        <th style="padding:8px 10px; width:30px;"><input type="checkbox" id="check-all" onchange="toggleAll(this.checked)" style="accent-color:var(--t-accent);"></th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('name')">Filename</th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('radio')">Radio</th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('freq')">Freq</th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('date')">Date</th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('time')">Time</th>
        <th style="padding:8px 10px; cursor:pointer;" onclick="sortBy('size')">Size</th>
        <th style="padding:8px 10px;">Play</th>
      </tr>
    </thead>
    <tbody id="rec-body"></tbody>
  </table>
</div>
<div id="rec-empty" style="display:none; text-align:center; color:#888; padding:40px; font-size:1.1em;">No recordings found</div>

<div id="player-bar" style="display:none; position:fixed; bottom:0; left:0; right:0; background:var(--t-panel); border-top:2px solid var(--t-accent); padding:10px 16px; z-index:999; font-family:monospace;">
  <div style="display:flex; align-items:center; gap:12px;">
    <button onclick="playerPause()" id="pp-btn" class="rb" style="padding:6px 12px; min-width:50px;">Pause</button>
    <button onclick="playerStop()" class="rb" style="padding:6px 12px; background:#5c1a1a;">Stop</button>
    <span id="player-time" style="color:#e0e0e0; min-width:100px;">0:00 / 0:00</span>
    <input type="range" id="player-seek" min="0" max="1000" value="0" step="1" style="flex:1; accent-color:var(--t-accent); cursor:pointer;">
    <span style="color:#888; font-size:0.85em;">Vol</span>
    <input type="range" id="player-vol" min="0" max="100" value="100" style="width:80px; accent-color:var(--t-accent);" oninput="playerVol(this.value)">
    <span id="player-name" style="color:var(--t-accent); font-size:0.85em; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>
  </div>
  <audio id="audio-player"></audio>
</div>

<style>
  .rb { padding:8px 14px; border:1px solid var(--t-btn-border); border-radius:4px;
    background:var(--t-btn); color:#e0e0e0; cursor:pointer; font-family:monospace; font-size:0.9em; }
  .rb:hover { background:var(--t-btn-hover); }
  #rec-table tbody tr:hover { background:rgba(255,255,255,0.05); }
  #rec-table tbody tr.playing { background:rgba(46,204,113,0.15); }
  #rec-table td { padding:6px 10px; border-top:1px solid var(--t-border); }
</style>

<script>
var _allFiles = [], _filtered = [], _sortKey = 'name', _sortAsc = false, _playingFile = '';

function loadFiles() {
  fetch('/recordingslist').then(r=>r.json()).then(function(files) {
    _allFiles = files;
    buildFilters();
    applyFilter();
  });
}

function buildFilters() {
  var radios = {}, dates = {}, freqs = {};
  _allFiles.forEach(function(f) {
    if (f.radio) radios[f.radio] = 1;
    if (f.date) dates[f.date] = 1;
    if (f.freq) freqs[f.freq] = 1;
  });
  fillSelect('f-radio', Object.keys(radios).sort());
  fillSelect('f-date', Object.keys(dates).sort().reverse());
  fillSelect('f-freq', Object.keys(freqs).sort());
}

function fillSelect(id, vals) {
  var sel = document.getElementById(id);
  var cur = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  vals.forEach(function(v) {
    var o = document.createElement('option');
    o.value = v; o.textContent = v;
    sel.appendChild(o);
  });
  sel.value = cur;
}

function applyFilter() {
  var r = document.getElementById('f-radio').value;
  var d = document.getElementById('f-date').value;
  var q = document.getElementById('f-freq').value;
  _filtered = _allFiles.filter(function(f) {
    if (r && f.radio !== r) return false;
    if (d && f.date !== d) return false;
    if (q && f.freq !== q) return false;
    return true;
  });
  doSort();
  render();
}

function sortBy(key) {
  if (_sortKey === key) _sortAsc = !_sortAsc;
  else { _sortKey = key; _sortAsc = true; }
  doSort();
  render();
}

function doSort() {
  _filtered.sort(function(a, b) {
    var va = a[_sortKey] || '', vb = b[_sortKey] || '';
    if (_sortKey === 'size') { va = a.size; vb = b.size; }
    if (va < vb) return _sortAsc ? -1 : 1;
    if (va > vb) return _sortAsc ? 1 : -1;
    return 0;
  });
}

function render() {
  var tbody = document.getElementById('rec-body');
  var empty = document.getElementById('rec-empty');
  var summary = document.getElementById('rec-summary');
  if (!_filtered.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    summary.textContent = _allFiles.length ? 'No files match filter' : 'No recordings';
    return;
  }
  empty.style.display = 'none';
  var totalSize = 0;
  _filtered.forEach(function(f) { totalSize += f.size; });
  summary.textContent = _filtered.length + ' file' + (_filtered.length !== 1 ? 's' : '') +
    ' (' + fmtSize(totalSize) + ')' +
    (_filtered.length < _allFiles.length ? ' of ' + _allFiles.length + ' total' : '');

  var h = '';
  _filtered.forEach(function(f) {
    var isPlaying = (_playingFile === f.name);
    h += '<tr class="' + (isPlaying ? 'playing' : '') + '">';
    h += '<td><input type="checkbox" class="f-check" data-name="' + f.name + '" style="accent-color:var(--t-accent);"></td>';
    h += '<td style="color:#e0e0e0;">' + f.name + '</td>';
    h += '<td style="color:var(--t-accent);">' + f.radio + '</td>';
    h += '<td style="color:#f39c12;">' + f.freq + '</td>';
    h += '<td>' + f.date + '</td>';
    h += '<td>' + f.time + '</td>';
    h += '<td style="text-align:right;">' + fmtSize(f.size) + '</td>';
    h += '<td>';
    if (f.ext === 'mp3' || f.ext === 'wav') {
      if (isPlaying) {
        h += '<button onclick="stopPlay()" class="rb" style="padding:4px 8px; background:#5c1a1a;">Stop</button>';
      } else {
        h += '<button onclick="playFile(\\'' + f.name + '\\')" class="rb" style="padding:4px 8px;">Play</button>';
      }
    }
    h += '</td></tr>';
  });
  tbody.innerHTML = h;
}

function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

function toggleAll(checked) {
  document.querySelectorAll('.f-check').forEach(function(c) { c.checked = checked; });
}
function selectAll() { toggleAll(true); document.getElementById('check-all').checked = true; }
function selectNone() { toggleAll(false); document.getElementById('check-all').checked = false; }

function getSelected() {
  var sel = [];
  document.querySelectorAll('.f-check:checked').forEach(function(c) { sel.push(c.dataset.name); });
  return sel;
}

function downloadSelected() {
  var sel = getSelected();
  if (!sel.length) { alert('No files selected'); return; }
  sel.forEach(function(fname) {
    var a = document.createElement('a');
    a.href = '/recordingsdownload?file=' + encodeURIComponent(fname);
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });
}

function deleteSelected() {
  var sel = getSelected();
  if (!sel.length) { alert('No files selected'); return; }
  if (!confirm('Delete ' + sel.length + ' file(s)?')) return;
  fetch('/recordingsdelete', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({files:sel})}).then(r=>r.json()).then(function(d) {
    loadFiles();
  });
}

function deleteAll() {
  fetch('/recordingsdelete', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({delete_all:true})}).then(r=>r.json()).then(function(d) {
    loadFiles();
  });
}

function playFile(fname) {
  var player = document.getElementById('audio-player');
  var bar = document.getElementById('player-bar');
  player.src = '/recordingsdownload?file=' + encodeURIComponent(fname);
  player.play();
  _playingFile = fname;
  bar.style.display = '';
  document.getElementById('player-name').textContent = fname;
  document.getElementById('pp-btn').textContent = 'Pause';
  render();
  player.onended = function() { playerStop(); };
}

function playerPause() {
  var player = document.getElementById('audio-player');
  if (player.paused) {
    player.play();
    document.getElementById('pp-btn').textContent = 'Pause';
  } else {
    player.pause();
    document.getElementById('pp-btn').textContent = 'Play';
  }
}

function playerStop() {
  var player = document.getElementById('audio-player');
  player.pause();
  player.src = '';
  _playingFile = '';
  document.getElementById('player-bar').style.display = 'none';
  render();
}

function playerVol(val) {
  document.getElementById('audio-player').volume = val / 100;
}

function fmtTime(s) {
  if (!s || isNaN(s)) return '0:00';
  var m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

(function() {
  var player = document.getElementById('audio-player');
  var seek = document.getElementById('player-seek');
  var dragging = false;

  // Update slider and time display from audio position (only when not dragging)
  player.addEventListener('timeupdate', function() {
    if (dragging) return;
    if (player.duration) {
      seek.value = (player.currentTime / player.duration) * 1000;
    }
    document.getElementById('player-time').textContent = fmtTime(player.currentTime) + ' / ' + fmtTime(player.duration);
  });

  // User starts dragging — stop updating slider from audio
  seek.addEventListener('mousedown', function() { dragging = true; });
  seek.addEventListener('touchstart', function() { dragging = true; });

  // User releases — apply the new position
  seek.addEventListener('mouseup', function() {
    if (player.duration) player.currentTime = player.duration * seek.value / 1000;
    dragging = false;
  });
  seek.addEventListener('touchend', function() {
    if (player.duration) player.currentTime = player.duration * seek.value / 1000;
    dragging = false;
  });

  // Also handle click (no drag, just a single click on the track)
  seek.addEventListener('change', function() {
    if (player.duration) player.currentTime = player.duration * seek.value / 1000;
    dragging = false;
  });
})();

loadFiles();
setInterval(loadFiles, 10000);
</script>
'''
        return self._wrap_html('Recordings', body)

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
            '<h1>Radio Gateway Configuration</h1>'
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


