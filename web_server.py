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

from config_format import format_config_value
from audio_sources import generate_cw_pcm, AudioProcessor
from smart_announce import SmartAnnouncementManager
from cat_client import RadioCATClient

# ============================================================================
# WEB CONFIGURATION UI
# ============================================================================

from web.sysinfo import _SysinfoMixin
from web.routing_cmds import _RoutingCmdsMixin
from web.certs import _CertsMixin


class WebConfigServer(_SysinfoMixin, _RoutingCmdsMixin, _CertsMixin):
    """Lightweight web UI for editing gateway_config.txt.

    Runs Python's built-in http.server on a daemon thread.  Serves a
    single-page form grouped by INI sections with Save and Save & Restart.
    """

    # Keys whose values should be masked in the UI
    _SENSITIVE_KEYS = {'STREAM_PASSWORD', 'EMAIL_APP_PASSWORD', 'WEB_CONFIG_PASSWORD',
                       'USRP_AMI_SECRET', 'USRP2_AMI_SECRET', 'DDNS_PASSWORD'}

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
        'LINK_AUTO_PTT_THRESHOLD': 'level 0-100 (higher = needs louder audio to key)',
        'LINK_AUTO_PTT_HOLD': 'seconds (hold after last audio above threshold)',
        'PTT_PREKEY_BUFFER_MS': 'ms of TX audio buffered during key-up (0 = off)',
        'LINK_JITTER_PREFILL': '50ms chunks (4 = 200ms cushion; 2 ok on wired LAN)',
        'REMOTE_AUDIO_JITTER_PREFILL': '50ms chunks (8 = 400ms cushion)',
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
        'KOKORO_DEFAULT_VOICE': 'voice ID for Kokoro engine (e.g. af_heart, bf_emma, am_puck)',
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
        # AllStar USRP (node 1)
        'USRP_REMOTE_HOST': 'ASL bridge node IP',
        'USRP_REMOTE_PORT': 'port ASL node listens on (default 32001)',
        'USRP_LISTEN_PORT': 'port gateway listens on (default 34001)',
        'USRP_NODE': 'local ASL node number (e.g. 683971)',
        'USRP_AMI_HOST': 'AMI host — blank = same as USRP_REMOTE_HOST',
        'USRP_AMI_PORT': 'Asterisk Manager Interface port (default 5038)',
        'USRP_AMI_USER': 'AMI username (manager.conf on the node)',
        'USRP_AMI_SECRET': 'AMI password',
        # AllStar USRP2 (node 2)
        'USRP2_REMOTE_HOST': 'ASL bridge node IP',
        'USRP2_REMOTE_PORT': 'port ASL node listens on (default 32002)',
        'USRP2_LISTEN_PORT': 'port gateway listens on (default 34002)',
        'USRP2_NODE': 'local ASL node number (e.g. 683971)',
        'USRP2_AMI_HOST': 'AMI host — blank = same as USRP2_REMOTE_HOST',
        'USRP2_AMI_PORT': 'Asterisk Manager Interface port (default 5038)',
        'USRP2_AMI_USER': 'AMI username (manager.conf on the node)',
        'USRP2_AMI_SECRET': 'AMI password',
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
        'WEB_MIC_KEY_TIMEOUT': 'seconds — hold-to-talk dead-man',
        'WEB_MIC_MAX_TX': 'seconds — max single transmission (TOT)',
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
        'TTS_ENGINE': [('kokoro', 'kokoro — Kokoro ONNX (offline, high quality)'), ('edge', 'edge — Microsoft Neural (natural)'), ('gtts', 'gtts — Google Translate (robotic)')],
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
        ('announce', 'Remote Audio [PTT]', [
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
            'DDNS_UPDATE_INTERVAL', 'DDNS_UPDATE_URL', 'DDNS_CHECKIP_URL',
            'DDNS_FORCE_INTERVAL', 'DDNS_VERIFY_DNS', 'DDNS_MISMATCH_GRACE',
            'DDNS_ALERT_INTERVAL',
        ]),
        ('echolink', 'EchoLink', [
            'ENABLE_ECHOLINK', 'ECHOLINK_RX_PIPE', 'ECHOLINK_TX_PIPE',
            'ECHOLINK_TO_MUMBLE', 'ECHOLINK_TO_RADIO',
            'RADIO_TO_ECHOLINK', 'MUMBLE_TO_ECHOLINK',
        ]),
        ('email', 'Email Notifications', [
            'ENABLE_EMAIL', 'EMAIL_ADDRESS', 'EMAIL_APP_PASSWORD',
            'EMAIL_RECIPIENT', 'EMAIL_ON_STARTUP', 'ENABLE_ALERT_ENGINE',
        ]),
        ('playback', 'System Sounds', [
            'ENABLE_PLAYBACK', 'PLAYBACK_DIRECTORY',
            'PLAYBACK_ANNOUNCEMENT_FILE', 'PLAYBACK_ANNOUNCEMENT_INTERVAL',
            'PLAYBACK_VOLUME', 'PLAYBACK_SLOTS', 'BGM_FILES', 'BGM_DUCK_DB', 'BGM_DUCK_ATTACK', 'BGM_DUCK_HOLD',
            'BGM_DUCK_RELEASE', 'BGM_MAX_SECONDS', 'ANNOUNCER_INTERVAL',
            'ENABLE_SOUNDBOARD', 'SOUNDBOARD_CATEGORIES',
            'SOUNDBOARD_MAX_SECONDS',
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
            'LINK_AUTO_PTT_THRESHOLD', 'LINK_AUTO_PTT_HOLD', 'PTT_PREKEY_BUFFER_MS',
            'LINK_JITTER_PREFILL', 'REMOTE_AUDIO_JITTER_PREFILL',
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
            'SDR_DUCK_COOLDOWN', 'SDR_SIGNAL_THRESHOLD',
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
            'KOKORO_DEFAULT_VOICE', 'TTS_DEFAULT_VOICE',
        ]),
        ('cat', 'TH-9800 CAT Control', [
            'ENABLE_TH9800', 'CAT_STARTUP_COMMANDS',
            'CAT_HOST', 'CAT_PORT', 'CAT_PASSWORD',
            'CAT_LEFT_CHANNEL', 'CAT_RIGHT_CHANNEL',
            'CAT_LEFT_VOLUME', 'CAT_RIGHT_VOLUME',
            'CAT_LEFT_POWER', 'CAT_RIGHT_POWER',
        ]),
        ('usrp', 'AllStar Link (USRP node 1)', [
            'ENABLE_USRP',
            'USRP_REMOTE_HOST', 'USRP_REMOTE_PORT', 'USRP_LISTEN_PORT',
            'USRP_NODE',
            'USRP_AMI_HOST', 'USRP_AMI_PORT', 'USRP_AMI_USER', 'USRP_AMI_SECRET',
        ]),
        ('usrp2', 'AllStar Link (USRP node 2)', [
            'ENABLE_USRP2',
            'USRP2_REMOTE_HOST', 'USRP2_REMOTE_PORT', 'USRP2_LISTEN_PORT',
            'USRP2_NODE',
            'USRP2_AMI_HOST', 'USRP2_AMI_PORT', 'USRP2_AMI_USER', 'USRP2_AMI_SECRET',
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
            'WEB_MIC_KEY_TIMEOUT', 'WEB_MIC_MAX_TX',
            'ENABLE_WEB_MONITOR', 'WEB_MONITOR_VOLUME', 'MONITOR_VAD_THRESHOLD',
            'ENABLE_CLOUDFLARE_TUNNEL',
            'ENABLE_GDRIVE', 'GDRIVE_REMOTE', 'GDRIVE_FOLDER',
        ]),
        ('transcription', 'Transcription', [
            'ENABLE_TRANSCRIPTION',
            'TRANSCRIBE_MODEL',
            'TRANSCRIBE_VAD_THRESHOLD', 'TRANSCRIBE_VAD_HOLD',
            'TRANSCRIBE_MIN_DURATION',
            'TRANSCRIBE_FORWARD_MUMBLE',
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
        self._encoder_stdin = None    # stdin pipe for encoder (O_NONBLOCK)
        self._enc_backlog = b''       # unsent remainder of a partial stdin write
        self._enc_lock = _thr.Lock()  # guards _enc_backlog (audio + silence threads)
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
                    # compare_digest: constant-time, no timing side-channel
                    import hmac
                    if (hmac.compare_digest(user, 'admin')
                            and hmac.compare_digest(pw, password)):
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
                '/dashboard/endpoints': 'dash_endpoints.html',
                '/dashboard/services': 'dash_services.html',
                '/dashboard/operate': 'dash_operate.html',
                '/controls': 'controls.html',
                '/sdr': 'sdr.html',
                '/d75': 'd75.html',
                '/ic7100': 'ic7100.html',
                '/kv4p': 'kv4p.html',
                '/radio': 'radio.html',
                '/monitor': 'monitor.html',
                '/recordings': 'recordings.html',
                '/recorder': 'recorder.html',
                '/transcribe': 'transcribe.html',
                '/txlog': 'txlog.html',
                '/logs': 'logs.html',
                '/gps': 'gps.html',
                '/repeaters': 'repeaters.html',
                '/aircraft': 'aircraft.html',
                '/routing': 'routing.html',
                '/packet': 'packet.html',
                '/processes': 'processes.html',
                '/gdrive': 'gdrive.html',
                '/manager': 'manager.html',
                '/grafana': 'grafana.html',
                '/endpoints/logs': 'endpoint_logs.html',
            }

            # ── Route tables ─────────────────────────────────────────────
            # Values are 'module:function' — g=web_routes_get,
            # s=web_routes_stream, p=web_routes_post, l=web_routes_loop.
            # Modules resolve at request time (imports are cached), same as
            # the old elif chain. Lookup order: EXACT (full path) →
            # QEXACT (query string stripped) → PREFIX (ordered startswith;
            # first hit wins, so keep more-specific prefixes first).

            _GET_EXACT = {
                '/status':                'g:handle_status',
                '/soundboard/categories': 'p:handle_soundboard_categories',
                '/announcer':             'p:handle_announcer',
                '/tts/engine':            'p:handle_tts_engine',
                '/metrics':               'g:handle_metrics',
                '/sinkstats':             'g:handle_sinkstats',
                '/sourcestats':           'g:handle_sourcestats',
                '/theme':                 'g:handle_theme',
                '/sysinfo':               'g:handle_sysinfo',
                '/catstatus':             'g:handle_catstatus',
                '/monitor-apk':           'g:handle_monitor_apk',
                '/d75status':             'g:handle_d75status',
                '/ic7100status':          'g:handle_ic7100status',
                '/api/processes':         'g:handle_api_processes',
                '/d75memlist':            'g:handle_d75memlist',
                '/sdrstatus':             'g:handle_sdrstatus',
                '/automationstatus':      'g:handle_automationstatus',
                '/adsbstatus':            'g:handle_adsbstatus',
                '/usbipstatus':           'g:handle_usbipstatus',
                '/gpsstatus':             'g:handle_gpsstatus',
                '/automationhistory':     'g:handle_automationhistory',
                '/ws_audio':              's:handle_ws_audio',
                '/ws_mic':                's:handle_ws_mic',
                '/ws_monitor':            's:handle_ws_monitor',
                '/ws/link':               's:handle_ws_link',
                '/stream':                's:handle_stream',
                '/tracestatus':           'g:handle_tracestatus',
                '/recordingslist':        'g:handle_recordingslist',
                '/adsb':                  'g:handle_adsb_proxy',
                '/pat':                   'g:handle_pat_proxy',
                '/routing/status':        'g:handle_routing_status',
                '/routing/levels':        'g:handle_routing_levels',
                '/packet/status':         'g:handle_packet_status',
                '/packet/packets':        'g:handle_packet_packets',
                '/packet/aprs_stations':  'g:handle_packet_aprs_stations',
                '/packet/bbs_buffer':     'g:handle_packet_bbs_buffer',
                '/packet/log':            'g:handle_packet_log',
                '/api/endpoint/version':  'g:handle_endpoint_version',
                '/api/endpoint/files':    'g:handle_endpoint_files',
                '/api/winclient/version': 'g:handle_winclient_version',
                '/api/winclient/files':   'g:handle_winclient_files',
                '/api/tunnel/link-url':   'g:handle_tunnel_link_url',
                '/api/gdrive/status':     'g:handle_gdrive_status',
                '/api/gdrive/files':      'g:handle_gdrive_files',
                '/manager/status':        'g:handle_manager_status',
                '/manager/reports':       'g:handle_manager_reports',
            }
            _GET_QEXACT = {
                '/config':                'g:handle_config',
                # Accepts ?instance=vhf|uhf for multi-radio routing.
                '/kv4pstatus':            'g:handle_kv4pstatus',
            }
            _GET_PREFIX = (
                ('/pages/',               'g:handle_pages'),
                ('/transcription/log',    'g:handle_transcription_log'),
                ('/transcript_search',    'g:handle_transcript_search'),
                ('/loopaudio',            'g:handle_loopaudio'),
                ('/transcriptions',       'g:handle_transcriptions'),
                ('/repeaterstatus',       'g:handle_repeaterstatus'),
                ('/logdata',              'g:handle_logdata'),
                ('/recordingsdownload',   'g:handle_recordingsdownload'),
                ('/adsb/',                'g:handle_adsb_proxy'),
                ('/pat/',                 'g:handle_pat_proxy'),
                ('/grafana/',             'g:handle_grafana_proxy'),
                ('/prometheus/',          'g:handle_prometheus_proxy'),
                ('/loop/',                'g:handle_loop_api'),
                ('/api/endpoint_logs',    'g:handle_endpoint_logs'),
                ('/packet/winlink/',      'g:handle_winlink_api'),
                ('/manager/doc',          'g:handle_manager_doc'),
                ('/manager/view',         'g:handle_manager_view'),
                ('/manager/edit',         'g:handle_manager_edit'),
            )

            def _dispatch(self, exact, qexact, prefixes):
                """Look up self.path in the route tables and run the handler.
                Returns True when a route matched, False to let the caller
                try plugin routes / the method's fallback."""
                import web_routes_get as _rg
                import web_routes_stream as _rs
                import web_routes_post as _rp
                import web_routes_loop as _rl
                _mods = {'g': _rg, 's': _rs, 'p': _rp, 'l': _rl}
                route = exact.get(self.path) or qexact.get(self.path.split('?', 1)[0])
                if route is None:
                    for _pfx, _r in prefixes:
                        if self.path.startswith(_pfx):
                            route = _r
                            break
                if route is None:
                    return False
                _m, _fn = route.split(':', 1)
                getattr(_mods[_m], _fn)(self, parent)
                return True

            def _dispatch_plugin_route(self):
                """Plugin-contributed routes (web_routes() hook). Handler
                signature: handler(request_handler, parent). Same path map
                serves GET + POST; the handler inspects self.command."""
                _routes = getattr(parent.gateway, '_plugin_web_routes', None)
                _key = self.path.split('?', 1)[0]
                if _routes and _key in _routes:
                    _routes[_key](self, parent)
                    return True
                return False

            def do_GET(self):
                if not self._check_auth():
                    return
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
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(_body)
                    except Exception:
                        self.send_response(500)
                        self.end_headers()
                    return

                if self._dispatch(self._GET_EXACT, self._GET_QEXACT, self._GET_PREFIX):
                    return
                if self._dispatch_plugin_route():
                    return
                # Unmatched GET — send 404 so the browser doesn't hang
                # waiting for a response that never comes. Exact routes
                # don't match with a stray query string appended, and
                # that used to silently never respond.
                try:
                    self.send_response(404)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'Not Found')
                except Exception:
                    pass

            _POST_EXACT = {
                '/key':                        'p:handle_key',
                '/fart/preview':               'p:handle_fart_preview',
                '/fart/send':                  'p:handle_fart_send',
                '/transcription/query':        'p:handle_transcription_query',
                '/transcribe_config':          'p:handle_transcribe_config',
                '/transcribe_worker/register': 'p:handle_transcribe_worker_register',
                '/testloop':                   'p:handle_testloop',
                '/bgm':                        'p:handle_bgm',
                '/announcer':                  'p:handle_announcer',
                '/mixer':                      'p:handle_mixer',
                '/aitext':                     'p:handle_aitext',
                '/cw':                         'p:handle_cw',
                '/tts':                        'p:handle_tts',
                '/automationcmd':              'p:handle_automationcmd',
                '/proc_toggle':                'p:handle_proc_toggle',
                '/d75cmd':                     'p:handle_d75cmd',
                '/ic7100cmd':                  'p:handle_ic7100cmd',
                '/gpscmd':                     'p:handle_gpscmd',
                '/kv4pcmd':                    'p:handle_kv4pcmd',
                '/linkcmd':                    'p:handle_linkcmd',
                '/catcmd':                     'p:handle_catcmd',
                '/sdrcmd':                     'p:handle_sdrcmd',
                '/tracecmd':                   'p:handle_tracecmd',
                '/reboothost':                 'p:handle_reboothost',
                '/restartgateway':             'p:handle_restartgateway',
                '/refreshsounds':              'p:handle_refreshsounds',
                '/soundboard/categories':      'p:handle_soundboard_categories',
                '/tts/engine':                 'p:handle_tts_engine',
                '/recordingsdelete':           'p:handle_recordingsdelete',
                '/exit':                       'p:handle_exit',
                '/routing/cmd':                'p:handle_routing_cmd',
                # Exact match wins before the '/loop/' prefix below.
                '/loop/export':                'p:handle_loop_export',
                '/api/gdrive/publish-tunnel':  'p:handle_gdrive_publish_tunnel',
                '/pat':                        'g:handle_pat_proxy',
                '/manager/toggle':             'p:handle_manager_toggle',
                '/manager/config':             'p:handle_manager_config',
                '/manager/save':               'p:handle_manager_save',
                '/manager/run':                'p:handle_manager_run',
                '/manager/ack':                'p:handle_manager_ack',
            }
            _POST_QEXACT = {}
            _POST_PREFIX = (
                ('/loop/',       'l:handle_loop_post'),
                ('/pat/',        'g:handle_pat_proxy'),
                ('/grafana/',    'g:handle_grafana_proxy'),
                ('/prometheus/', 'g:handle_prometheus_proxy'),
                ('/packet/',     'p:handle_packet_cmd'),
            )

            def do_POST(self):
                if not self._check_auth():
                    return
                if self._dispatch(self._POST_EXACT, self._POST_QEXACT, self._POST_PREFIX):
                    return
                if self._dispatch_plugin_route():
                    return
                # Config form submission (fallback for /config POST)
                import web_routes_post as _rp
                _rp.handle_config_form(self, parent)


        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 32  # default 5 is too low for concurrent dashboard clients

            def handle_error(self, request, client_address):
                # Default socketserver.handle_error dumps a 20-line stack
                # trace to stderr for any uncaught exception in a handler.
                # The single most common one is BrokenPipeError when a
                # browser closes the tab mid-response — benign, but
                # unreadable noise. Swallow that one; defer everything
                # else to the base implementation.
                import sys
                _et, _ev, _ = sys.exc_info()
                if isinstance(_ev, (BrokenPipeError, ConnectionResetError)):
                    return
                super().handle_error(request, client_address)

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
            self._enc_write(pcm_data)
            self._last_audio_push = time.monotonic()

    def _enc_write(self, data):
        """Non-blocking write to the encoder stdin via the raw fd.

        A wedged ffmpeg must never block the calling audio thread (this
        runs on the SCHED_RR main loop). Partial writes park the remainder
        in a bounded backlog that is flushed FIRST on the next call — the
        remainder must be resumed, never dropped: s16le samples are 2
        bytes, so an odd-length drop would byte-shift every subsequent
        sample into static. While a large backlog stands (encoder stalled),
        new data is dropped whole to keep memory bounded.
        """
        stdin = self._encoder_stdin
        if stdin is None:
            return
        try:
            fd = stdin.fileno()
        except (ValueError, OSError):
            return  # encoder shutting down
        with self._enc_lock:
            buf = self._enc_backlog
            if buf:
                if len(buf) < 65536:
                    buf += data
                # else: stalled — flush backlog only, drop new data whole
            else:
                buf = data
            try:
                while buf:
                    n = os.write(fd, buf)
                    if n <= 0:
                        break
                    buf = buf[n:]
            except BlockingIOError:
                pass  # pipe full — keep remainder for next call
            except (BrokenPipeError, OSError, ValueError):
                buf = b''  # encoder died — reader/watchdog handles restart
            self._enc_backlog = buf

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
            proc = sp.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-f', 's16le', '-ar', '48000', '-ac', '1', '-i', 'pipe:0',
                '-c:a', 'libmp3lame', '-b:a', '96k',
                '-flush_packets', '1',
                '-fflags', '+nobuffer',
                '-f', 'mp3', 'pipe:1'
            ], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL)
            self._encoder_proc = proc
            # Non-blocking stdin: a wedged encoder must never block the
            # audio thread. All writes go through _enc_write (raw fd +
            # bounded backlog).
            import fcntl
            _fd = proc.stdin.fileno()
            _fl = fcntl.fcntl(_fd, fcntl.F_GETFL)
            fcntl.fcntl(_fd, fcntl.F_SETFL, _fl | os.O_NONBLOCK)
            self._enc_backlog = b''
            self._encoder_stdin = proc.stdin
            # Reader thread: reads MP3 from FFmpeg, pushes to ring buffer
            def _reader():
                while proc.poll() is None:
                    data = proc.stdout.read(4096)
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
            # Feed silence when no real audio is arriving — keeps encoder
            # producing output. Goes through _enc_write like push_audio:
            # stdin is O_NONBLOCK now, and a raw .write() would die on the
            # first BlockingIOError.
            def _silence_feed():
                _silence = b'\x00' * (self.config.AUDIO_CHUNK_SIZE * 2)  # 50 ms
                while proc.poll() is None:
                    time.sleep(0.05)
                    if time.monotonic() - self._last_audio_push > 0.2:
                        self._enc_write(_silence)
            t2 = _thr.Thread(target=_silence_feed, daemon=True, name='mp3-silence')
            t2.start()
            print(f"  [Stream] MP3 encoder started (PID {proc.pid})")
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
        """Register a new stream listener. Returns (event, seq).

        Starts the shared MP3 encoder on the first subscriber.
        """
        ev = _thr.Event()
        with self._stream_lock:
            first = len(self._stream_subscribers) == 0
            if first:
                self._start_encoder()
            seq = self._mp3_seq  # Start from current sequence number
            self._stream_events.append(ev)
            self._stream_subscribers.append(ev)
        return ev, seq

    def _unsubscribe_stream(self, ev):
        """Remove a stream listener. Stops the encoder when none remain."""
        with self._stream_lock:
            try:
                self._stream_events.remove(ev)
            except ValueError:
                pass
            try:
                self._stream_subscribers.remove(ev)
            except ValueError:
                pass
            last = len(self._stream_subscribers) == 0
        if last:
            self._stop_encoder()

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
                # Quote anything the reader would not give back unchanged --
                # a value containing ' #' would otherwise be truncated at the
                # next load, silently losing whatever followed. See
                # config_format.format_config_value.
                lines.append(f'{key} = {format_config_value(val)}\n')

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

        def _tx_sink_info(obj):
            """TX sinks share the plugin object with their RX source but have
            independent mute/gain (tx_muted, tx_audio_boost)."""
            return {
                'muted': getattr(obj, 'tx_muted', False),
                'gain': int(getattr(obj, 'tx_audio_boost', 1.0) * 100),
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
            # kv4p endpoints register themselves via the link-endpoints loop
            # below as 'kv4p_vhf' / 'kv4p_uhf' — no legacy bare-'kv4p' source
            # any more (would re-inject a phantom node into the routing UI).
            if getattr(gw, 'th9800_plugin', None):
                sources.append({**{'id': 'aioc', 'name': 'TH-9800 [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.th9800_plugin)})
            if getattr(gw, 'playback_source', None):
                sources.append({**{'id': 'playback', 'name': 'System Sounds', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.playback_source)})
            if getattr(gw, 'bgm_source', None):
                sources.append({**{'id': 'bgm', 'name': 'BGM', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.bgm_source)})
            if getattr(gw, 'announcer_source', None):
                sources.append({**{'id': 'announcer', 'name': 'Announcer', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.announcer_source)})
            if getattr(gw, 'loop_playback_source', None):
                sources.append({**{'id': 'loop_playback', 'name': 'Loop Playback', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.loop_playback_source)})
            if getattr(gw, 'web_mic_source', None):
                sources.append({**{'id': 'webmic', 'name': 'Web Mic', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True}, **_src_info(gw.web_mic_source)})
            if getattr(gw, 'announce_input_source', None):
                sources.append({**{'id': 'announce', 'name': 'Remote Audio [PTT]', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True,
                                'port': getattr(gw.announce_input_source, '_port', None) or int(getattr(gw.config, 'ANNOUNCE_INPUT_PORT', 9601))},
                                **_src_info(gw.announce_input_source)})
            if getattr(gw, 'web_monitor_source', None):
                sources.append({**{'id': 'monitor', 'name': 'Room Monitor', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(gw.web_monitor_source)})
            if gw.mumble:
                sources.append({'id': 'mumble_rx', 'name': 'Mumble [RX]', 'enabled': True,
                                'can_rx': False, 'can_tx': True, 'can_ptt': True,
                                'muted': False, 'gain': 100})
            if getattr(gw, 'remote_audio_source', None):
                sources.append({**{'id': 'remote_audio', 'name': 'Remote Audio [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False,
                                'port': getattr(gw.remote_audio_source, 'port', None) or int(getattr(gw.config, 'REMOTE_AUDIO_PORT', 9602))},
                                **_src_info(gw.remote_audio_source)})
            # Link endpoints — all dynamic, using pre-computed source_id
            for _ep_name, _ep_src in gw.link_endpoints.items():
                _ep_id = getattr(_ep_src, 'source_id', None)
                if not _ep_id:
                    continue
                _ep_label = _ep_name.replace('-', ' ').replace('_', ' ').title()
                sources.append({**{'id': _ep_id, 'name': f'{_ep_label} [RX]', 'enabled': True,
                                'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_ep_src)})
            # External plugins (auto-discovered from plugins/) — RX source node
            # for anything declaring the audio_rx capability.
            for _pid, _plg in getattr(gw, '_external_plugins', {}).items():
                _caps = getattr(_plg, 'CAPABILITIES', None) or set()
                if 'audio_rx' in _caps:
                    _label = getattr(_plg, 'PLUGIN_NAME', _pid)
                    sources.append({**{'id': _pid, 'name': f'{_label} [RX]', 'enabled': True,
                                    'can_rx': True, 'can_tx': False, 'can_ptt': False}, **_src_info(_plg)})

        # Build sink list (passive consumers + TX-capable radios)
        sinks = []
        sinks.append({'id': 'mumble', 'name': 'Mumble [TX]', 'type': 'VoIP',
                      'enabled': bool(gw and gw.mumble)})
        # Broadcastify exposes EITHER one mono node or an L/R pair, never both.
        # Making the channel mode an explicit config choice rather than
        # inferring it from which nodes happen to be wired means the invalid
        # combinations (mono AND left, or a "both" node fighting L/R) simply
        # cannot be represented in the graph, so there is nothing to validate
        # and nothing for the user to get wrong.
        _bcfy_on = bool(gw and getattr(gw, 'stream_output', None))
        if bool(getattr(gw.config, 'STREAM_DUAL_CHANNEL', True)) if gw else False:
            sinks.append({'id': 'broadcastify_l', 'name': 'Broadcastify [L]',
                          'type': 'Stream', 'enabled': _bcfy_on})
            sinks.append({'id': 'broadcastify_r', 'name': 'Broadcastify [R]',
                          'type': 'Stream', 'enabled': _bcfy_on})
        else:
            sinks.append({'id': 'broadcastify', 'name': 'Broadcastify',
                          'type': 'Stream', 'enabled': _bcfy_on})
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
        # NUL Sink — drops audio without using network/CPU. Always muted.
        sinks.append({'id': 'nul', 'name': 'NUL Sink', 'type': 'Null',
                      'enabled': True, 'muted': True})
        if gw and getattr(gw, 'remote_audio_server', None):
            sinks.append({'id': 'remote_audio_tx', 'name': 'Remote Audio [TX]', 'type': 'Network',
                          'enabled': bool(gw.remote_audio_server.connected)})
        # TX-capable radios as destinations
        if gw:
            # kv4p TX sinks come from the link-endpoints loop below as
            # 'kv4p_vhf_tx' / 'kv4p_uhf_tx'. Bare 'kv4p_tx' is gone.
            if getattr(gw, 'th9800_plugin', None):
                sinks.append({**{'id': 'aioc_tx', 'name': 'TH-9800 [TX]', 'type': 'Radio TX', 'enabled': True}, **_tx_sink_info(gw.th9800_plugin)})
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
            # External plugins (auto-discovered) — TX sink node for anything
            # declaring the audio_tx capability ('<pid>_tx', resolved by
            # bus_manager._get_radio_plugin).
            for _pid, _plg in getattr(gw, '_external_plugins', {}).items():
                _caps = getattr(_plg, 'CAPABILITIES', None) or set()
                if 'audio_tx' in _caps:
                    _label = getattr(_plg, 'PLUGIN_NAME', _pid)
                    sinks.append({**{'id': f'{_pid}_tx', 'name': f'{_label} [TX]',
                                  'type': 'Radio TX', 'enabled': True}, **_tx_sink_info(_plg)})

        # Load bus config
        busses, connections, saved_layout = self._load_routing_config()

        return {
            'sources': sources,
            'layout': saved_layout,
            'busses': busses,
            'sinks': sinks,
            'connections': connections,
        }

    # ── Routing command dispatch ──────────────────────────────────
    # Each command from the routing UI maps to a _routing_cmd_<name>
    # method below. The dispatcher just loads config, looks up the
    # handler, and calls it. Add a command by writing the method and
    # adding an entry to _ROUTING_CMD_DISPATCH at class level.


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
            # Never ship secrets to the browser. type="password" masked these
            # visually while the real value still sat in value="..." — readable
            # via View Source by anyone who could load /config.
            # Not type="password" either: that made Chrome treat /config as a
            # login page and fire password-reuse warnings on the rotating
            # trycloudflare hostname. There is no login here, so no password field.
            # Blank on submit means "keep the stored value" (handle_config_form).
            placeholder = 'set — leave blank to keep' if cur_val else 'not set'
            inp = (f'<input type="text" name="{key}" value="" autocomplete="off" '
                   f'placeholder="{html_mod.escape(placeholder)}">')
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


