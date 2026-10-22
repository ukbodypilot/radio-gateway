"""Packet Radio Plugin — Direwolf TNC integration for APRS, Winlink, and BBS.

Direwolf runs on the remote endpoint (Pi) reading the AIOC directly for
clean packet decode.  The gateway connects to Direwolf's KISS TCP port
and handles APRS parsing, station tracking, and UI.

The endpoint's AIOC plugin switches between audio mode (normal radio
streaming) and data mode (Direwolf owns the AIOC) via link protocol
commands.  Mode switching is triggered by the /packet page.
"""

import collections
import time


from packet.agwpe_proxy import _AGWPEProxyMixin
from packet.endpoint import _EndpointMixin
from packet.pat import _PatMixin
from packet.mode import _ModeMixin
from packet.kiss import _KISSMixin
from packet.state import _PacketStateMixin
from plugins._base import CAPABILITY_PACKET, CAPABILITY_STATUS


class PacketRadioPlugin(_AGWPEProxyMixin, _EndpointMixin, _PatMixin,
                        _ModeMixin, _KISSMixin, _PacketStateMixin):
    """Software TNC (Direwolf) plugin for the gateway routing system.

    Migrated to ``plugins/`` in Phase 2.D.relocate. Audio is owned by
    Direwolf on the endpoint or on the gateway box — the plugin itself
    is control plane only, hence ``audio_rx``/``audio_tx`` are False on
    both contract surfaces.
    """

    # ── New plugin contract metadata ────────────────────────────────
    PLUGIN_ID = 'packet'
    PLUGIN_NAME = 'Packet TNC'
    CAPABILITIES = {CAPABILITY_PACKET, CAPABILITY_STATUS}

    # Backward-compat surface for code that still reads ``name`` or
    # lowercase ``capabilities`` (Mixer v2 UI, status JSON consumers).
    name = "tnc"
    capabilities = {
        "audio_rx": False,
        "audio_tx": False,
        "ptt": False,
        "frequency": False,
        "ctcss": False,
        "power": False,
        "rx_gain": False,
        "tx_gain": False,
        "smeter": False,
        "status": True,
    }

    # ── Init ──────────────────────────────────────────────────────────

    def __init__(self):
        # Plugin contract attributes
        self.enabled = True
        self.ptt_control = True
        self.priority = 5
        self.sdr_priority = 5
        self.volume = 1.0
        self.duck = False
        self.muted = False
        self.audio_level = 0
        self.tx_audio_level = 0
        self.audio_boost = 1.0
        self.tx_audio_boost = 1.0
        self.server_connected = False

        # Internal state
        self._config = None
        self._gateway = None
        self._mode = 'idle'           # legacy alias for state.target; kept in sync by _advance.
        self._init_packet_state()     # seeds _target/_phase/_step/_last_error
        # direwolf log lives in gateway.packet_tnc.log_tail now.
        self._running = False

        # KISS connection to remote Direwolf
        self._kiss_sock = None
        self._kiss_connected = False

        # Pat Winlink client subprocess
        # pat lifecycle is owned by gateway's ProcessSupervisor under
        # name 'pat'. self._pat_proc is no longer used.

        # Packet data
        self._decoded_packets = collections.deque(maxlen=500)
        self._aprs_stations = {}      # callsign → {lat, lon, symbol, comment, last_heard, ...}
        self._bbs_buffer = collections.deque(maxlen=2000)
        self._bbs_connected = False
        self._bbs_callsign = ''
        self._bbs_agw_sock = None
        self._bbs_reader_thread = None
        self._packet_count = 0
        self._start_time = None

        # Config values (set in setup)
        # direwolf audio_level/peak live in gateway.packet_tnc now.
        self._callsign = 'N0CALL'
        self._ssid = 0
        self._modem_rate = 1200
        self._remote_tnc = ''           # Remote endpoint IP (required)
        self._endpoint_pref = ''        # PACKET_RADIO_ENDPOINT — name override, blank = auto
        self._cached_endpoint = None    # Cached endpoint name (avoid repeated getpeername)
        self._kiss_port = 8001
        self._pat_port = 8082
        self._aprs_comment = 'Radio Gateway'
        self._aprs_symbol = '/#'
        self._aprs_beacon_interval = 600
        self._digipeat = True

    # ── Setup / Teardown ──────────────────────────────────────────────

    def setup(self, config, gateway=None):
        """Initialize plugin — read config."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._gateway = gateway
        self._start_time = time.monotonic()

        # Read config
        self._callsign = str(getattr(config, 'PACKET_CALLSIGN', 'N0CALL')).strip().upper()
        self._ssid = int(getattr(config, 'PACKET_SSID', 0))
        self._modem_rate = int(getattr(config, 'PACKET_MODEM', 1200))
        self._remote_tnc = str(getattr(config, 'PACKET_REMOTE_TNC', '')).strip()
        self._endpoint_pref = str(getattr(config, 'PACKET_RADIO_ENDPOINT', '')).strip()
        self._kiss_port = int(getattr(config, 'PACKET_KISS_PORT', 8001))
        self._pat_port = int(getattr(config, 'PACKET_PAT_PORT', 8082))
        self._aprs_comment = str(getattr(config, 'PACKET_APRS_COMMENT', 'Radio Gateway'))
        self._aprs_symbol = str(getattr(config, 'PACKET_APRS_SYMBOL', '/#'))
        self._aprs_beacon_interval = int(getattr(config, 'PACKET_APRS_BEACON_INTERVAL', 600))
        self._digipeat = bool(getattr(config, 'PACKET_DIGIPEAT', True))

        if not self._remote_tnc:
            print(f"  [Packet] PACKET_REMOTE_TNC not set — will auto-discover AIOC endpoint")

        self._running = True
        self.server_connected = True
        self._agwpe_proxy_sock = None
        self._proxy_sessions_active = 0   # count of in-flight proxy sessions
        # Start AGWPE proxy so Pat always connects to 127.0.0.1:8010
        self._start_agwpe_proxy()
        print(f"  [Packet] Plugin initialized (callsign={self._callsign}-{self._ssid}, "
              f"modem={self._modem_rate}, endpoint={self._remote_tnc or 'auto-discover'})")
        return True



    def teardown(self):
        """Stop everything and clean up."""
        self._running = False
        self._disconnect_kiss()
        self._stop_pat()
        print("  [Packet] Teardown complete")

    # Plugin contract alias — the new RadioPlugin Protocol calls the
    # shutdown hook ``cleanup``; legacy callers (gateway_setup, MCP) still
    # call ``teardown``. Same body, two names.
    cleanup = teardown

    # ── Audio interface (stubs — audio handled by endpoint) ──────────

    def get_audio(self, chunk_size=None):
        return None, False

    def put_audio(self, pcm):
        pass

    # ── Commands ──────────────────────────────────────────────────────

    def execute(self, cmd):
        """Handle commands from the gateway."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        elif action == 'set_mode':
            return self._set_mode(cmd.get('mode', 'idle'))
        elif action == 'aprs_beacon':
            return self._send_aprs_beacon()
        elif action == 'aprs_send':
            return self._send_aprs_message(cmd.get('to', ''), cmd.get('message', ''))
        elif action == 'bbs_connect':
            return self._bbs_connect(cmd.get('callsign', ''))
        elif action == 'bbs_disconnect':
            return self._bbs_disconnect()
        elif action == 'bbs_send':
            return self._bbs_send(cmd.get('text', ''))
        elif action == 'force_audio':
            ok = self._send_endpoint_mode('audio')
            return {"ok": ok, "sent": "audio",
                    "error": None if ok else "failed to reach endpoint"}
        elif action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}

        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return current TNC state."""
        positioned = sum(1 for s in self._aprs_stations.values() if s.get('lat') is not None)
        ep = self.get_endpoint_status()
        state = self.state_snapshot()
        return {
            "plugin": self.name,
            "mode": self._mode,
            # State machine triple — target/phase/step/last_error/phase_age.
            # ``mode`` above stays as a legacy alias for state['target'].
            "state": state,
            "callsign": f"{self._callsign}-{self._ssid}",
            "modem": self._modem_rate,
            "direwolf_running": self._mode != 'idle',
            "remote_tnc": self._remote_tnc or None,
            "kiss_connected": self._kiss_connected,
            "packet_count": self._packet_count,
            "station_count": len(self._aprs_stations),
            "positioned_count": positioned,
            "bbs_connected": self._bbs_connected,
            "bbs_callsign": self._bbs_callsign,
            "uptime": round(time.monotonic() - self._start_time, 1) if self._start_time else 0,
            "rx_audio_level": 0,
            "tx_audio_level": 0,
            # Direwolf state pulled live from gateway-side packet_tnc
            **self._tnc_status_fields(),
            "pat_port": self._pat_port,
            "pat_running": self._is_pat_running(),
            "endpoint": ep,
            "endpoint_active": self._find_endpoint(),
            "endpoint_pref": self._endpoint_pref,
            "endpoints_available": self.list_packet_endpoints(),
        }

    # ── Mode switching ────────────────────────────────────────────────


