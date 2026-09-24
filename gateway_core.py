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

    def __init__(self, original, buffer_lines=2000, log_file=None,
                 log_dir=None, keep_days=7, **_kwargs):
        self._orig = original
        self._lock = threading.Lock()
        self._at_line_start = True
        import collections
        self._log_buffer = collections.deque(maxlen=buffer_lines)
        self._log_seq = 0
        self._log_file = log_file
        # Mid-run daily rotation state. The old behaviour opened
        # gateway-<startday>.log once and wrote to it for the entire
        # (multi-week) run — rotation and retention only happened on
        # restart. When log_dir is set, _append_log rolls to a new
        # dated file at midnight and prunes files beyond keep_days.
        self._log_dir = log_dir
        self._log_keep_days = int(keep_days)
        import datetime as _dt
        self._log_date = _dt.date.today()
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
            self._maybe_roll_log()
            try:
                self._log_file.write(timestamped_line + '\n')
                self._log_file.flush()
            except Exception:
                pass

    def _maybe_roll_log(self):
        """Roll to a new dated log file when the calendar day changes."""
        if not self._log_dir:
            return
        import datetime as _dt
        today = _dt.date.today()
        if today == self._log_date:
            return
        self._log_date = today
        try:
            new_path = os.path.join(self._log_dir,
                                    f"gateway-{today.strftime('%Y-%m-%d')}.log")
            new_file = open(new_path, 'a', encoding='utf-8')
        except Exception:
            return  # keep writing to the old file rather than lose logs
        old_file = self._log_file
        self._log_file = new_file
        try:
            old_file.close()
        except Exception:
            pass
        # Retention: prune dated logs beyond keep_days
        try:
            import glob as _glob
            for old_log in sorted(_glob.glob(os.path.join(self._log_dir, 'gateway-*.log'))):
                try:
                    date_str = os.path.basename(old_log)[len('gateway-'):-len('.log')]
                    log_date = _dt.datetime.strptime(date_str, '%Y-%m-%d').date()
                    if (today - log_date).days > self._log_keep_days:
                        os.remove(old_log)
                except (ValueError, OSError):
                    pass
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


from core.audio_proc import _AudioProcMixin
from core.ptt import _PTTMixin
from core.usb_audio import _USBAudioMixin
from core.setup_audio_mumble import _SetupAudioMumbleMixin
from core.mumble_io import _MumbleIOMixin
from core.transmit import _TransmitMixin
from core.stream import _StreamMixin
from core.audio_restart import _AudioRestartMixin
from core.monitor import _MonitorMixin
from core.lifecycle import _LifecycleMixin


class RadioGateway(_LifecycleMixin, _MonitorMixin, _AudioRestartMixin,
                   _TransmitMixin, _StreamMixin,
                   _MumbleIOMixin, _SetupAudioMumbleMixin, _USBAudioMixin,
                   _PTTMixin, _AudioProcMixin):
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
        self.endpoint_log_store = None    # EndpointLogStore — per-endpoint log files
        self.link_endpoints = {}          # {name: LinkAudioSource}
        self.link_endpoint_settings = {}  # {name: {rx_muted, tx_muted}} — persisted
        self._link_ptt_active = {}        # {name: bool}
        self._link_last_status = {}       # {name: dict}
        self._link_tx_levels = {}         # {name: int}
        self._link_settings_path = os.path.expanduser('~/.config/radio-gateway/link_endpoints.json')
        self._source_gains = {}     # {source_id: gain_pct} — persisted
        self._source_gains_path = os.path.expanduser('~/.config/radio-gateway/source_gains.json')
        self._sink_gains = {}       # {sink_id: float} — runtime only for now
        self.aioc_available = False  # Track if AIOC is connected

        # Legacy SDR rebroadcast state removed 2026-07-27 — the feature is
        # retired in favour of routing (SDR source -> solo bus -> <radio>_tx).

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

        # Fleet Manager Engine
        self.manager_engine = None  # ManagerEngine instance

        # Alert engine (Prom polling → email)
        self.alert_engine = None

        # Automation Engine
        self.automation_engine = None  # AutomationEngine instance

        # Transcriber
        self.transcriber = None
        self.transcription_log = None
        self.transcription_audio_level = 0

        # NUL Sink: drop-only destination; tracks activity level but never
        # delivers audio. Always presented as muted in the UI.
        self.nul_audio_level = 0

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

        # Shared supervisor for long-running child processes (kv4p loopback
        # endpoints, cloudflared, pat, mDNS, direwolf, etc.)
        from process_supervisor import ProcessSupervisor
        self.process_supervisor = ProcessSupervisor()

        # KV4P endpoints — live entries arrive in self.link_endpoints as the
        # supervised loopback endpoints connect. gw.kv4p_plugin below is a
        # live proxy that quacks like the old in-core KV4PPlugin and routes
        # to the first connected kv4p endpoint; legacy callers can keep
        # using it. New code should use the helpers in kv4p_endpoints.py.
        from kv4p_endpoints import install_proxy as _install_kv4p_proxy
        _install_kv4p_proxy(self)

        # Stub processor object for web_routes_audio.py which still asks
        # for `gw.kv4p_processor.get_active_list()`. Processing is now
        # owned by the endpoint plugin, so the gateway-side view is empty.
        class _NullProc:
            def get_active_list(self): return []
        self.kv4p_processor = _NullProc()

        # PacketTNC owns direwolf via the ProcessSupervisor. Constructed
        # here so packet_radio.py / web handlers can reach it via gw.packet_tnc.
        from packet_tnc import PacketTNC
        self.packet_tnc = PacketTNC(self.process_supervisor)

        self.packet_plugin = None         # PacketRadioPlugin instance
        self.bus_manager = None           # BusManager (created in _setup_routing)
        self._bus_sinks = {}              # {bus_id: set(sink_ids)} — populated by _setup_routing
        self._bus_stream_flags = {}       # {bus_id: {pcm, mp3, vad}} — populated by _setup_routing
        self._listen_bus_id = 'listen'    # Primary listen bus ID — set by _setup_routing
        self._listen_bus_muted = False    # Primary listen bus mute — set by _setup_routing
        self._muted_sinks = set()         # Set of muted sink IDs

        # Mumble Server instances (local mumble-server/murmurd)
        self.mumble_server_1 = None  # MumbleServerManager instance
        self.mumble_server_2 = None  # MumbleServerManager instance


        # Broadcastify stream health monitoring
        self._stream_was_connected = False  # Set True once stream connects
        self._stream_drop_alerted = False   # Prevent repeated alerts
        self._last_stream_health_check = 0

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
    
    def _get_tts_voices(self):
        """Return voice list for the active TTS backend as [{value, label}]."""
        backend = getattr(self, '_tts_backend', 'edge')
        if backend == 'kokoro':
            return [{'value': k, 'label': v} for k, v in self.KOKORO_VOICES.items()]
        elif backend == 'edge':
            return [{'value': str(k), 'label': v[1]} for k, v in self.EDGE_TTS_VOICES.items()]
        else:
            return [{'value': str(k), 'label': v[2]} for k, v in self.TTS_VOICES.items()]

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
        # AllStar (USRP) plugins — discovered, live in _external_plugins
        _usrp = getattr(self, '_external_plugins', {}).get('usrp')
        usrp_rx = getattr(_usrp, 'audio_level', 0) if _usrp else 0
        usrp_tx = getattr(_usrp, 'tx_audio_level', 0) if _usrp else 0
        _usrp2 = getattr(self, '_external_plugins', {}).get('usrp2')
        usrp2_rx = getattr(_usrp2, 'audio_level', 0) if _usrp2 else 0
        usrp2_tx = getattr(_usrp2, 'tx_audio_level', 0) if _usrp2 else 0

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
        ddns_stats = {}
        if self.ddns_updater:
            ddns_status = self.ddns_updater.get_status() or '...'
            # Counters, so a stalled DDNS is diagnosed by which one stopped
            # moving rather than inferred from log silence (a suppressed update
            # and a successful 'nochg' both print nothing).
            if hasattr(self.ddns_updater, 'get_stats'):
                try:
                    ddns_stats = self.ddns_updater.get_stats()
                except Exception:
                    ddns_stats = {}

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

        # TH-9800 TX path health. The plugin's own get_status() is reachable
        # only through execute({'cmd': 'status'}), which no web route or MCP
        # tool calls -- counters added there alone would have been unreadable,
        # which is how instrumentation quietly becomes decoration. Pulled in
        # here so /status carries them.
        th9800_tx = {}
        _th = getattr(self, 'th9800_plugin', None)
        if _th is not None:
            # Plain counters, copied as-is. getattr with a default keeps this
            # working against a plugin build that predates them.
            for _k in ('tx_enqueued', 'tx_drops', 'tx_written', 'tx_depth_max',
                       'tx_write_errors', 'ptt_confirmed', 'ptt_failures',
                       'ptt_last_error'):
                _v = getattr(_th, '_' + _k, None)
                if _v is not None:
                    th9800_tx[_k] = _v
            # Derived: current depth, and the write timings rounded for display.
            _q = getattr(_th, '_tx_queue', None)
            th9800_tx['tx_depth_now'] = len(_q) if _q is not None else 0
            th9800_tx['tx_write_ms_max'] = round(
                getattr(_th, '_tx_write_ms_max', 0.0), 2)
            _w = th9800_tx.get('tx_written') or 0
            _tot = getattr(_th, '_tx_write_ms_total', 0.0)
            th9800_tx['tx_write_ms_avg'] = round(_tot / _w, 3) if _w else 0.0

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
        _d75_link = next((src for src in self.link_endpoints.values() if getattr(src, 'plugin_type', None) == 'd75'), None)

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
            'tx_talkback': getattr(self, 'tx_talkback', False),
            'remote_muted': getattr(self, 'remote_audio_muted', False),
            'announce_muted': getattr(self, 'announce_input_muted', False),
            'speaker_muted': getattr(self, 'speaker_muted', True),
            'radio_rx': radio_rx,
            'radio_tx': radio_tx,
            'usrp_enabled': bool(_usrp and getattr(_usrp, 'enabled', False)),
            'usrp_level': usrp_rx,
            'usrp_tx_level': usrp_tx,
            'usrp_node': getattr(_usrp, 'node', '') if _usrp else '',
            'usrp2_enabled': bool(_usrp2 and getattr(_usrp2, 'enabled', False)),
            'usrp2_level': usrp2_rx,
            'usrp2_tx_level': usrp2_tx,
            'usrp2_node': getattr(_usrp2, 'node', '') if _usrp2 else '',
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
            # kv4p_proc empty: processing moved to endpoint plugins, which
            # apply their own filters internally. Web UI checkboxes for kv4p
            # filters still toggle config keys but no longer reflect live
            # state until the endpoint is restarted.
            'kv4p_proc': [],
            'smart_countdowns': sa_countdowns,
            'smart_activity': self.smart_announce.get_activity() if self.smart_announce and hasattr(self.smart_announce, 'get_activity') else {},
            'ddns': ddns_status,
            'ddns_stats': ddns_stats,
            'tunnel_url': self.cloudflare_tunnel.get_url() if self.cloudflare_tunnel else '',
            'charger': charger_state,
            'cat': cat_state,
            'cat_reliability': cat_reliability,
            'cat_vol': cat_vol,
            'th9800_tx': th9800_tx,
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
            # kv4p_* aggregate fields reflect the first connected kv4p
            # endpoint (back-compat with single-instance consumers). For
            # multi-instance data see status['kv4p_endpoints'] below.
            **__import__('kv4p_endpoints').aggregate_status(self),
            'gps_enabled': bool(self.gps_manager),
            'repeater_db_enabled': bool(self.repeater_manager),
            'adsb_enabled': getattr(self.config, 'ENABLE_ADSB', False),
            'monitor_enabled': bool(self.web_monitor_source),
            'monitor_level': self.web_monitor_source.audio_level if self.web_monitor_source else 0,
            'link_enabled': bool(self.link_server),
            'link_endpoints': [
                {
                    'name': name,
                    'connected': True,
                    'plugin': _ep_info.get('plugin', ''),
                    'via_tunnel': _ep_info.get('via_tunnel', False),
                    'addr': _ep_info.get('addr', ''),
                    'ping_ms': _ep_info.get('ping_ms', -1),
                    'source_id': getattr(src, 'source_id', ''),
                    'sink_id': getattr(src, 'sink_id', ''),
                    'capabilities': _ep_info.get('capabilities', {}),
                    'level': src.meter_level() if hasattr(src, 'meter_level') else src.audio_level,
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
            # Loop state must be in /status: it is the only way the Loop
            # button can re-sync after the loop is stopped by something
            # other than that button (Stop, a queued announcement, a
            # restart). Without it the button stayed lit for ever.
            'loop_active': bool(getattr(self.playback_source, 'loop_active', False)),
            'bgm': (self.bgm_source.bgm_state() if getattr(self, 'bgm_source', None) else []),
            'playback_slots': int(getattr(self.playback_source, 'slot_count', 9))
                              if self.playback_source else 0,
            'tts_enabled': bool(getattr(self, 'tts_engine', None)),
            'tts_voices': self._get_tts_voices(),
            'tts_backend': getattr(self, '_tts_backend', 'edge'),
            'smart_announce_enabled': bool(self.smart_announce),
            # Broadcastify / Icecast streaming
            'streaming_enabled': bool(getattr(self.config, 'ENABLE_STREAM_OUTPUT', False)),
            'stream_connected': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'stream_pipe_ok': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'stream_restarts': getattr(getattr(self, 'stream_output', None), '_reconnect_count', 0),
            # Reconnect-storm instrumentation. A rising 'superseded' count is
            # the fix working (late workers retiring instead of clobbering a
            # live connection); a rising 'wedged' count means connects are
            # hanging — historically DNS. Both flat is the healthy steady state.
            'stream_reconnect_superseded': getattr(getattr(self, 'stream_output', None), '_reconnect_superseded', 0),
            'stream_reconnect_wedged': getattr(getattr(self, 'stream_output', None), '_reconnect_wedged', 0),
            'stream_health': bool(getattr(self, 'stream_output', None) and getattr(self.stream_output, 'connected', False)),
            'encoder_stats': self._get_stream_stats(),
            'notifications': list(self._notifications),
            'automation_enabled': bool(self.automation_engine),
            'automation_task': self.automation_engine._current_task if self.automation_engine else None,
            'automation_recording': self.automation_engine.recorder.is_recording() if self.automation_engine else False,
        }

    def _publish_tunnel_url(self):
        """Write current tunnel URL to Google Drive for endpoint discovery."""
        if not self.gdrive or not self.cloudflare_tunnel:
            return
        # Wait for tunnel URL if not yet available
        for _ in range(30):
            url = self.cloudflare_tunnel.get_url()
            if url:
                break
            time.sleep(1)
        if not url:
            print("  [GDrive] No tunnel URL to publish")
            return
        # Dedupe: two callers (one-shot in setup_gdrive + on_url_changed
        # callback) can race; skip if the URL on GDrive is already this one.
        if getattr(self, '_last_published_tunnel_url', None) == url:
            return
        import datetime
        data = {
            'url': url,
            'ws_link': url.replace('https://', 'wss://').replace('http://', 'ws://').rstrip('/') + '/ws/link',
            'updated': datetime.datetime.utcnow().isoformat() + 'Z',
        }
        try:
            self.gdrive.write_json(data, 'tunnel_url.json')
            self._last_published_tunnel_url = url
            print(f"  [GDrive] Published tunnel URL: {url}")
        except Exception as e:
            print(f"  [GDrive] Failed to publish tunnel URL: {e}")


