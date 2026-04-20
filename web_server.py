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

from audio_sources import generate_cw_pcm, AudioProcessor
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
        'SDR_PRIORITY_ORDER': [
            ('sdr1', 'SDR1 first — SDR1 ducks SDR2 when active'),
            ('sdr2', 'SDR2 first — SDR2 ducks SDR1 when active'),
            ('equal', 'Equal — both play simultaneously'),
        ],
        'KV4P_AUDIO_PRIORITY': [('0', '0 — ducks all'), ('1', '1 — high'), ('2', '2 — low')],
        'REMOTE_AUDIO_PRIORITY': [('0', '0 — ducks all'), ('1', '1 — high'), ('2', '2 — low')],
        'KV4P_BANDWIDTH': [('0', '0 — Narrow'), ('1', '1 — Wide')],
        'AUDIO_CHANNELS': [('1', '1 — Mono'), ('2', '2 — Stereo')],
        'AIOC_PTT_CHANNEL': [('1', '1'), ('2', '2'), ('3', '3')],
        'REMOTE_AUDIO_ROLE': [('disabled', 'disabled'), ('server', 'enabled — connect to remote client')],
        'SPEAKER_MODE': [('virtual', 'virtual — metering only'), ('auto', 'auto — try device, fallback virtual'), ('real', 'real — require audio device')],
        'RELAY_CHARGER_CONTROL': ['gpio', 'serial'],
        'TTS_ENGINE': [('edge', 'edge — Microsoft Neural (natural)'), ('gtts', 'gtts — Google Translate (robotic)')],
        'WEB_CONFIG_HTTPS': ['false', 'self-signed', 'letsencrypt'],
        'WEB_THEME': ['grey', 'blue', 'red', 'green', 'purple', 'amber', 'teal', 'pink'],
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
        ('packet', 'Packet Radio (Direwolf TNC)', [
            'ENABLE_PACKET', 'PACKET_CALLSIGN', 'PACKET_SSID', 'PACKET_MODEM',
            'PACKET_REMOTE_TNC', 'PACKET_DIREWOLF_PATH',
            'PACKET_KISS_PORT', 'PACKET_AGW_PORT',
            'PACKET_APRS_COMMENT', 'PACKET_APRS_SYMBOL',
            'PACKET_APRS_BEACON_INTERVAL', 'PACKET_DIGIPEAT',
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
            'ENABLE_D75',
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
            'ENABLE_GDRIVE', 'GDRIVE_REMOTE', 'GDRIVE_FOLDER',
        ]),
        ('telegram', 'Telegram Bot', [
            'ENABLE_TELEGRAM', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
            'TELEGRAM_TMUX_SESSION',
            'TELEGRAM_STATUS_FILE', 'TELEGRAM_PROMPT_SUFFIX',
        ]),
        ('transcription', 'Transcription', [
            'ENABLE_TRANSCRIPTION',
            'TRANSCRIBE_MODEL',
            'TRANSCRIBE_VAD_THRESHOLD', 'TRANSCRIBE_VAD_HOLD',
            'TRANSCRIBE_MIN_DURATION',
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

    # Color themes — values here override common.css defaults via /theme endpoint.
    # Default 'blue' is the phosphor palette (matches common.css).
    # Non-blue themes only tint the chrome (bg/panel/border/accent/btn); text
    # and ok/warn/err fall back to common.css defaults so status colors remain
    # legible and the neutral greys stay neutral across themes.
    THEMES = {
        'grey':   {'bg': '#0b1014', 'panel': '#121820', 'border': '#1e2a38', 'accent': '#4fd6e6',
                   'btn': '#0e131a', 'btn_border': '#1e2a38', 'btn_hover': '#1a2230',
                   'btn_active_bg': '#2c3e52', 'checkbox': '#4fd6e6',
                   'panel_hi': '#1a2230', 'border_hi': '#2c3e52'},
        'blue':   {'bg': '#0b1014', 'panel': '#121820', 'border': '#1e2a38', 'accent': '#4fd6e6',
                   'btn': '#0e131a', 'btn_border': '#1e2a38', 'btn_hover': '#1a2230',
                   'btn_active_bg': '#2c3e52', 'checkbox': '#4fd6e6',
                   'panel_hi': '#1a2230', 'border_hi': '#2c3e52'},
        'red':    {'bg': '#1a1212', 'panel': '#2e1616', 'border': '#601010', 'accent': '#ff4444',
                   'btn': '#1e0d0d', 'btn_border': '#5c1b1b', 'btn_hover': '#3a1a1a',
                   'btn_active_bg': '#601010', 'checkbox': '#ff4444',
                   'panel_hi': '#3a1a1a', 'border_hi': '#7a1818'},
        'green':  {'bg': '#121a14', 'panel': '#162e1a', 'border': '#0f6020', 'accent': '#2ecc71',
                   'btn': '#0d1e10', 'btn_border': '#1b5c2a', 'btn_hover': '#1a3a20',
                   'btn_active_bg': '#0f6020', 'checkbox': '#2ecc71',
                   'panel_hi': '#1a3a20', 'border_hi': '#18781f'},
        'purple': {'bg': '#1a1226', 'panel': '#261638', 'border': '#3d0f60', 'accent': '#b56eff',
                   'btn': '#160d24', 'btn_border': '#3d1b5c', 'btn_hover': '#2a1a44',
                   'btn_active_bg': '#3d0f60', 'checkbox': '#b56eff',
                   'panel_hi': '#2a1a44', 'border_hi': '#4e1878'},
        'amber':  {'bg': '#1a1710', 'panel': '#2e2616', 'border': '#60480f', 'accent': '#ffb830',
                   'btn': '#1e1a0d', 'btn_border': '#5c481b', 'btn_hover': '#3a301a',
                   'btn_active_bg': '#60480f', 'checkbox': '#ffb830',
                   'panel_hi': '#3a301a', 'border_hi': '#78591c'},
        'teal':   {'bg': '#101a1a', 'panel': '#162e2e', 'border': '#0f6060', 'accent': '#2ed8d8',
                   'btn': '#0d1e1e', 'btn_border': '#1b5c5c', 'btn_hover': '#1a3a3a',
                   'btn_active_bg': '#0f6060', 'checkbox': '#2ed8d8',
                   'panel_hi': '#1a3a3a', 'border_hi': '#187878'},
        'pink':   {'bg': '#1a1018', 'panel': '#2e1628', 'border': '#600f50', 'accent': '#ff69b4',
                   'btn': '#1e0d1a', 'btn_border': '#5c1b4a', 'btn_hover': '#3a1a32',
                   'btn_active_bg': '#600f50', 'checkbox': '#ff69b4',
                   'panel_hi': '#3a1a32', 'border_hi': '#78186a'},
    }

    def _get_theme(self):
        """Return the current theme color dict."""
        name = str(getattr(self.config, 'WEB_THEME', 'grey')).lower().strip()
        return self.THEMES.get(name, self.THEMES['grey'])

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
                from usbip_manager import USBIPManager
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
                '/recorder': 'recorder.html',
                '/transcribe': 'transcribe.html',
                '/logs': 'logs.html',
                '/gps': 'gps.html',
                '/repeaters': 'repeaters.html',
                '/aircraft': 'aircraft.html',
                '/voice': 'voice.html',
                '/routing': 'routing.html',
                '/packet': 'packet.html',
                '/gdrive': 'gdrive.html',
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

                # Route dispatch — handlers in web_routes_get.py and web_routes_stream.py
                import web_routes_get as _rg
                import web_routes_stream as _rs

                if self.path == '/status':
                    _rg.handle_status(self, parent)
                elif self.path == '/theme':
                    _rg.handle_theme(self, parent)
                elif self.path.startswith('/pages/'):
                    _rg.handle_pages(self, parent)
                elif self.path == '/sysinfo':
                    _rg.handle_sysinfo(self, parent)
                elif self.path == '/catstatus':
                    _rg.handle_catstatus(self, parent)
                elif self.path == '/monitor-apk':
                    _rg.handle_monitor_apk(self, parent)
                elif self.path.startswith('/transcriptions'):
                    _rg.handle_transcriptions(self, parent)
                elif self.path == '/d75status':
                    _rg.handle_d75status(self, parent)
                elif self.path == '/kv4pstatus':
                    _rg.handle_kv4pstatus(self, parent)
                elif self.path == '/d75memlist':
                    _rg.handle_d75memlist(self, parent)
                elif self.path == '/sdrstatus':
                    _rg.handle_sdrstatus(self, parent)
                elif self.path == '/automationstatus':
                    _rg.handle_automationstatus(self, parent)
                elif self.path == '/adsbstatus':
                    _rg.handle_adsbstatus(self, parent)
                elif self.path == '/telegramstatus':
                    _rg.handle_telegramstatus(self, parent)
                elif self.path == '/usbipstatus':
                    _rg.handle_usbipstatus(self, parent)
                elif self.path == '/gpsstatus':
                    _rg.handle_gpsstatus(self, parent)
                elif self.path.startswith('/repeaterstatus'):
                    _rg.handle_repeaterstatus(self, parent)
                elif self.path == '/automationhistory':
                    _rg.handle_automationhistory(self, parent)
                elif self.path == '/ws_audio':
                    _rs.handle_ws_audio(self, parent)
                elif self.path == '/ws_mic':
                    _rs.handle_ws_mic(self, parent)
                elif self.path == '/ws_monitor':
                    _rs.handle_ws_monitor(self, parent)
                elif self.path == '/ws/link':
                    _rs.handle_ws_link(self, parent)
                elif self.path == '/stream':
                    _rs.handle_stream(self, parent)
                elif self.path == '/tracestatus':
                    _rg.handle_tracestatus(self, parent)
                elif self.path.startswith('/logdata'):
                    _rg.handle_logdata(self, parent)
                elif self.path == '/recordingslist':
                    _rg.handle_recordingslist(self, parent)
                elif self.path.startswith('/recordingsdownload'):
                    _rg.handle_recordingsdownload(self, parent)
                elif self.path == '/adsb' or self.path.startswith('/adsb/'):
                    _rg.handle_adsb_proxy(self, parent)
                elif self.path == '/pat' or self.path.startswith('/pat/'):
                    _rg.handle_pat_proxy(self, parent)
                elif self.path == '/config':
                    _rg.handle_config(self, parent)
                elif self.path == '/routing/status':
                    _rg.handle_routing_status(self, parent)
                elif self.path == '/routing/levels':
                    _rg.handle_routing_levels(self, parent)
                elif self.path == '/voice/status':
                    _rg.handle_voice_status(self, parent)
                elif self.path == '/voice/view':
                    _rg.handle_voice_view(self, parent)
                elif self.path == '/packet/status':
                    _rg.handle_packet_status(self, parent)
                elif self.path == '/packet/packets':
                    _rg.handle_packet_packets(self, parent)
                elif self.path == '/packet/aprs_stations':
                    _rg.handle_packet_aprs_stations(self, parent)
                elif self.path == '/packet/bbs_buffer':
                    _rg.handle_packet_bbs_buffer(self, parent)
                elif self.path == '/packet/log':
                    _rg.handle_packet_log(self, parent)
                elif self.path.startswith('/loop/'):
                    _rg.handle_loop_api(self, parent)
                elif self.path == '/api/endpoint/version':
                    _rg.handle_endpoint_version(self, parent)
                elif self.path == '/api/endpoint/files':
                    _rg.handle_endpoint_files(self, parent)
                elif self.path == '/api/tunnel/link-url':
                    _rg.handle_tunnel_link_url(self, parent)
                elif self.path == '/api/gdrive/status':
                    _rg.handle_gdrive_status(self, parent)
                elif self.path == '/api/gdrive/files':
                    _rg.handle_gdrive_files(self, parent)
                elif self.path.startswith('/packet/winlink/'):
                    _rg.handle_winlink_api(self, parent)

            def do_POST(self):
                if not self._check_auth():
                    return
                import urllib.parse
                import json as json_mod

                # Route dispatch — handlers in web_routes_post.py
                import web_routes_post as _rp
                import web_routes_loop as _rl

                if self.path == '/key':
                    _rp.handle_key(self, parent)
                elif self.path == '/transcribe_config':
                    _rp.handle_transcribe_config(self, parent)
                elif self.path == '/testloop':
                    _rp.handle_testloop(self, parent)
                elif self.path == '/mixer':
                    _rp.handle_mixer(self, parent)
                elif self.path == '/aitext':
                    _rp.handle_aitext(self, parent)
                elif self.path == '/cw':
                    _rp.handle_cw(self, parent)
                elif self.path == '/tts':
                    _rp.handle_tts(self, parent)
                elif self.path == '/automationcmd':
                    _rp.handle_automationcmd(self, parent)
                elif self.path == '/proc_toggle':
                    _rp.handle_proc_toggle(self, parent)
                elif self.path == '/d75cmd':
                    _rp.handle_d75cmd(self, parent)
                elif self.path == '/gpscmd':
                    _rp.handle_gpscmd(self, parent)
                elif self.path == '/kv4pcmd':
                    _rp.handle_kv4pcmd(self, parent)
                elif self.path == '/linkcmd':
                    _rp.handle_linkcmd(self, parent)
                elif self.path == '/catcmd':
                    _rp.handle_catcmd(self, parent)
                elif self.path == '/sdrcmd':
                    _rp.handle_sdrcmd(self, parent)
                elif self.path == '/tracecmd':
                    _rp.handle_tracecmd(self, parent)
                elif self.path == '/reboothost':
                    _rp.handle_reboothost(self, parent)
                elif self.path == '/restartgateway':
                    _rp.handle_restartgateway(self, parent)
                elif self.path == '/refreshsounds':
                    _rp.handle_refreshsounds(self, parent)
                elif self.path == '/darkicecmd':
                    _rp.handle_darkicecmd(self, parent)
                elif self.path == '/recordingsdelete':
                    _rp.handle_recordingsdelete(self, parent)
                elif self.path == '/telegramcmd':
                    _rp.handle_telegramcmd(self, parent)
                elif self.path == '/open_tmux':
                    _rp.handle_open_tmux(self, parent)
                elif self.path == '/exit':
                    _rp.handle_exit(self, parent)
                elif self.path == '/routing/cmd':
                    _rp.handle_routing_cmd(self, parent)
                elif self.path == '/voice/send':
                    _rp.handle_voice_send(self, parent)
                elif self.path == '/voice/session':
                    _rp.handle_voice_session(self, parent)
                elif self.path == '/loop/export':
                    _rp.handle_loop_export(self, parent)
                elif self.path.startswith('/loop/'):
                    _rl.handle_loop_post(self, parent)
                elif self.path == '/api/gdrive/publish-tunnel':
                    _gw = parent.gateway if parent else None
                    if _gw and _gw.gdrive:
                        import threading as _gd_t
                        _gd_t.Thread(target=_gw._publish_tunnel_url,
                                     daemon=True).start()
                        _body = json_mod.dumps({'ok': True}).encode()
                    else:
                        _body = json_mod.dumps({'ok': False, 'error': 'GDrive not configured'}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(_body)))
                    self.end_headers()
                    self.wfile.write(_body)
                elif self.path == '/pat' or self.path.startswith('/pat/'):
                    _rg.handle_pat_proxy(self, parent)
                elif self.path.startswith('/packet/'):
                    _rp.handle_packet_cmd(self, parent)
                else:
                    # Config form submission (fallback for /config POST)
                    _rp.handle_config_form(self, parent)


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
    --t-panel-hi: {t.get('panel_hi', t['btn_hover'])};
    --t-border-hi: {t.get('border_hi', t['btn_active_bg'])};
    --t-text: {t.get('text', '#d6dee6')};
    --t-text-dim: {t.get('text_dim', '#7a8a99')};
    --t-text-mute: {t.get('text_mute', '#6b7a8a')};
    --t-ok: {t.get('ok', '#5dc47a')};
    --t-warn: {t.get('warn', '#e89d3c')};
    --t-err: {t.get('err', '#e04848')};
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
                _sdr = gw.sdr_plugin
                if getattr(_sdr, '_tuner1', None):
                    sources.append({**{'id': 'sdr1', 'name': 'SDR1 [RX]', 'enabled': True,
                                    'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_sdr._tuner1)})
                if getattr(_sdr, '_tuner2', None):
                    sources.append({**{'id': 'sdr2', 'name': 'SDR2 [RX]', 'enabled': True,
                                    'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_sdr._tuner2)})
                if not getattr(_sdr, '_tuner1', None) and not getattr(_sdr, '_tuner2', None):
                    # Fallback: plugin has no captures yet
                    sources.append({**{'id': 'sdr', 'name': 'SDR [RX]', 'enabled': True,
                                    'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_sdr)})
            if gw.kv4p_plugin:
                sources.append({**{'id': 'kv4p', 'name': 'KV4P [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.kv4p_plugin)})
            if getattr(gw, 'th9800_plugin', None):
                sources.append({**{'id': 'aioc', 'name': 'TH-9800 [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.th9800_plugin)})
            if getattr(gw, 'playback_source', None):
                sources.append({**{'id': 'playback', 'name': 'File Playback', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.playback_source)})
            if getattr(gw, 'loop_playback_source', None):
                sources.append({**{'id': 'loop_playback', 'name': 'Loop Playback', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.loop_playback_source)})
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
            # Link endpoints — all dynamic, using pre-computed source_id
            for _ep_name, _ep_src in gw.link_endpoints.items():
                _ep_id = getattr(_ep_src, 'source_id', None)
                if not _ep_id:
                    continue
                _ep_label = _ep_name.replace('-', ' ').replace('_', ' ').title()
                sources.append({**{'id': _ep_id, 'name': f'{_ep_label} [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_ep_src)})

        # Build sink list (passive consumers + TX-capable radios)
        sinks = []
        sinks.append({'id': 'mumble', 'name': 'Mumble [TX]', 'type': 'VoIP',
                      'enabled': bool(gw and gw.mumble)})
        sinks.append({'id': 'broadcastify', 'name': 'Broadcastify', 'type': 'Stream',
                      'enabled': bool(gw and getattr(gw, 'stream_output', None))})
        _spk_mode = str(getattr(gw.config, 'SPEAKER_MODE', 'virtual')).lower() if gw else 'virtual'
        sinks.append({'id': 'speaker', 'name': 'Speaker', 'type': 'Local',
                      'enabled': True, 'speaker_mode': _spk_mode})
        # 'recording' sink removed — it was a v1 stub that never got a
        # v2.0 implementation. The Loop Recorder's per-bus "R" button is
        # the real recording mechanism now. Stale nodes in existing
        # routing_config.json are stripped by bus_manager on load.
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
            if getattr(gw, 'th9800_plugin', None):
                sinks.append({**{'id': 'aioc_tx', 'name': 'TH-9800 [TX]', 'type': 'Radio TX', 'enabled': True}, **_src_info(gw.th9800_plugin)})
            # Link endpoint TX sinks — all dynamic, using pre-computed sink_id
            for _ep_name, _ep_src in gw.link_endpoints.items():
                _sink_id = getattr(_ep_src, 'sink_id', None)
                if not _sink_id:
                    continue
                _ep_label = _ep_name.replace('-', ' ').replace('_', ' ').title()
                _caps = getattr(_ep_src, '_endpoint_caps', {})
                if _caps.get('ptt') or _caps.get('audio_tx', True):
                    _tx_gain = int(getattr(_ep_src, 'tx_audio_boost', 1.0) * 100)
                    sinks.append({'id': _sink_id, 'name': f'{_ep_label} [TX]', 'type': 'Radio TX',
                                  'enabled': True, 'muted': getattr(_ep_src, 'muted', False), 'gain': _tx_gain})

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

        elif cmd == 'rename_bus':
            bus_id = data.get('id', '')
            new_name = data.get('name', '').strip()
            if not new_name:
                return {'ok': False, 'error': 'name required'}
            for b in busses:
                if b['id'] == bus_id:
                    b['name'] = new_name
                    self._save_routing_config(busses, connections)
                    return {'ok': True, 'name': new_name}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

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
            if self.gateway and getattr(self.gateway, 'bus_manager', None):
                try:
                    self.gateway.bus_manager.sync_listen_bus()
                except Exception as e:
                    print(f"  [routing] sync_listen_bus error: {e}")
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
            if filt not in ('gate', 'hpf', 'lpf', 'notch', 'dfn', 'pcm', 'mp3', 'vad', 'loop'):
                return {'ok': False, 'error': f'invalid filter: {filt}'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc[filt] = not proc.get(filt, False)
                    self._save_routing_config(busses, connections)
                    # Update cached stream flags on gateway + BusManager
                    if filt in ('pcm', 'mp3', 'vad', 'loop') and self.gateway:
                        flags = getattr(self.gateway, '_bus_stream_flags', {})
                        bus_flags = flags.setdefault(bus_id, {'pcm': False, 'mp3': False, 'vad': False})
                        bus_flags[filt] = proc[filt]
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    # Stop loop recorder when toggled off
                    if filt == 'loop' and not proc[filt] and self.gateway:
                        _lr = getattr(self.gateway, 'loop_recorder', None)
                        if _lr:
                            _lr.stop(bus_id)
                    if bm:
                        if bus_id in bm._bus_config:
                            bm._bus_config[bus_id][filt] = proc[filt]
                        # Update the live AudioProcessor (create if needed)
                        if filt in ('gate', 'hpf', 'lpf', 'notch', 'dfn'):
                            _bp = bm._bus_processors.get(bus_id)
                            if not _bp:
                                _bp = AudioProcessor(f"bus_{bus_id}", self.gateway.config)
                                bm._bus_processors[bus_id] = _bp
                            setattr(_bp, 'enable_noise_gate' if filt == 'gate' else f'enable_{filt}', proc[filt])
                    return {'ok': True, 'state': proc[filt]}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'set_dfn_mix':
            bus_id = data.get('bus', '')
            try:
                mix = max(0.0, min(1.0, float(data.get('mix', 0.5))))
            except (ValueError, TypeError):
                return {'ok': False, 'error': 'invalid mix value'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc['dfn_mix'] = mix
                    self._save_routing_config(busses, connections)
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm:
                        if bus_id in bm._bus_config:
                            bm._bus_config[bus_id]['dfn_mix'] = mix
                        _bp = bm._bus_processors.get(bus_id)
                        if _bp is not None:
                            _bp.dfn_mix = mix
                    return {'ok': True, 'mix': mix}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'set_dfn_atten':
            # DFN attenuation cap in dB. 0 = model decides (can pump);
            # 15–25 is typical real-world range. Clamped to [0, 60].
            bus_id = data.get('bus', '')
            try:
                atten = max(0.0, min(60.0, float(data.get('atten_db', 18.0))))
            except (ValueError, TypeError):
                return {'ok': False, 'error': 'invalid atten_db value'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc['dfn_atten_db'] = atten
                    self._save_routing_config(busses, connections)
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm:
                        if bus_id in bm._bus_config:
                            bm._bus_config[bus_id]['dfn_atten_db'] = atten
                        _bp = bm._bus_processors.get(bus_id)
                        if _bp is not None:
                            _bp.dfn_atten_db = atten
                    return {'ok': True, 'atten_db': atten}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'set_dfn_engine':
            # Per-bus denoise engine selection — 'rnnoise' | 'deepfilternet'.
            # Persists to routing_config.json and applies live via
            # AudioProcessor.set_dfn_engine (drops the current stream so the
            # next audio chunk rebuilds with the new engine).
            from audio_util import DENOISE_ENGINE_IDS
            bus_id = data.get('bus', '')
            engine = str(data.get('engine', ''))
            if engine not in DENOISE_ENGINE_IDS:
                return {'ok': False,
                        'error': f'invalid engine; must be one of {list(DENOISE_ENGINE_IDS)}'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc['dfn_engine'] = engine
                    self._save_routing_config(busses, connections)
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm:
                        if bus_id in bm._bus_config:
                            bm._bus_config[bus_id]['dfn_engine'] = engine
                        _bp = bm._bus_processors.get(bus_id)
                        if _bp is not None:
                            _bp.set_dfn_engine(engine)
                    return {'ok': True, 'engine': engine}
            return {'ok': False, 'error': f'bus not found: {bus_id}'}

        elif cmd == 'set_loop_hours':
            bus_id = data.get('bus', '')
            hours = data.get('hours', 24)
            try:
                hours = max(1, min(168, int(hours)))  # 1h to 7 days
            except (ValueError, TypeError):
                return {'ok': False, 'error': 'invalid hours value'}
            for b in busses:
                if b['id'] == bus_id:
                    proc = b.setdefault('processing', {})
                    proc['loop_hours'] = hours
                    self._save_routing_config(busses, connections)
                    bm = getattr(self.gateway, 'bus_manager', None) if self.gateway else None
                    if bm and bus_id in bm._bus_config:
                        bm._bus_config[bus_id]['loop_hours'] = hours
                    lr = getattr(self.gateway, 'loop_recorder', None)
                    if lr:
                        lr.set_retention(bus_id, hours)
                    return {'ok': True, 'hours': hours}
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
            _sink_ids = ('speaker', 'broadcastify', 'mumble', 'remote_audio_tx')
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
            _gw = self.gateway
            plugin = self._get_plugin_by_id(target_id)
            if plugin:
                _is_tx = target_id.endswith('_tx')
                if _is_tx and hasattr(plugin, 'tx_audio_boost'):
                    plugin.tx_audio_boost = value / 100.0
                else:
                    plugin.audio_boost = value / 100.0
                # Persist link endpoint gains
                _ep_name = getattr(plugin, 'endpoint_name', '')
                if _ep_name and _gw:
                    _key = 'tx_boost' if _is_tx else 'rx_boost'
                    settings = _gw.link_endpoint_settings.setdefault(_ep_name, {})
                    settings[_key] = value
                    _gw._save_link_settings()
                # Persist source gains
                if _gw:
                    _gw._source_gains[target_id] = value
                    _gw._save_source_gains()
                return {'ok': True, 'gain': value}
            # Passive sinks (mumble, broadcastify, speaker, etc.)
            _passive_sinks = ('mumble', 'broadcastify', 'speaker',
                              'transcription', 'remote_audio_tx')
            if target_id in _passive_sinks and _gw:
                _gw._sink_gains[target_id] = value / 100.0
                _gw._source_gains[target_id] = value
                _gw._save_source_gains()
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
        _sdr = gw.sdr_plugin
        _map = {
            'sdr': _sdr,
            'sdr1': getattr(_sdr, '_tuner1', None) if _sdr else None,
            'sdr2': getattr(_sdr, '_tuner2', None) if _sdr else None,
            'kv4p': gw.kv4p_plugin,
            'kv4p_tx': gw.kv4p_plugin,
            'aioc': getattr(gw, 'th9800_plugin', None),
            'aioc_tx': getattr(gw, 'th9800_plugin', None),
            'playback': getattr(gw, 'playback_source', None),
            'loop_playback': getattr(gw, 'loop_playback_source', None),
            'webmic': getattr(gw, 'web_mic_source', None),
            'announce': getattr(gw, 'announce_input_source', None),
            'monitor': getattr(gw, 'web_monitor_source', None),
            'mumble_rx': getattr(gw, 'mumble_source', None),
            'remote_audio': getattr(gw, 'remote_audio_source', None),
        }
        result = _map.get(id)
        # Link endpoint lookup by source_id or sink_id
        if result is None:
            for name, src in gw.link_endpoints.items():
                if getattr(src, 'source_id', None) == id or getattr(src, 'sink_id', None) == id:
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
            # Callsign (from PACKET_CALLSIGN) — shown in the shell identity plate.
            cs = str(getattr(self.config, 'PACKET_CALLSIGN', '') or '').strip().upper()
            info['callsign'] = cs if cs and cs != 'N0CALL' else ''

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


