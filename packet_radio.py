"""Packet Radio Plugin — Direwolf TNC integration for APRS, Winlink, and BBS.

Direwolf runs on the remote endpoint (Pi) reading the AIOC directly for
clean packet decode.  The gateway connects to Direwolf's KISS TCP port
and handles APRS parsing, station tracking, and UI.

The endpoint's AIOC plugin switches between audio mode (normal radio
streaming) and data mode (Direwolf owns the AIOC) via link protocol
commands.  Mode switching is triggered by the /packet page.
"""

import collections
import math
import re
import socket
import threading
import time


class PacketRadioPlugin:
    """Software TNC (Direwolf) plugin for the gateway routing system."""

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
        self.ptt_control = False
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
        self._mode = 'idle'           # idle / aprs / winlink / bbs
        self._direwolf_log = collections.deque(maxlen=200)
        self._running = False

        # KISS connection to remote Direwolf
        self._kiss_sock = None
        self._kiss_connected = False

        # Packet data
        self._decoded_packets = collections.deque(maxlen=500)
        self._aprs_stations = {}      # callsign → {lat, lon, symbol, comment, last_heard, ...}
        self._bbs_buffer = collections.deque(maxlen=2000)
        self._bbs_connected = False
        self._bbs_callsign = ''
        self._packet_count = 0
        self._start_time = None

        # Config values (set in setup)
        self._dw_audio_level = 0        # Direwolf's reported audio level
        self._dw_audio_peak = ''
        self._callsign = 'N0CALL'
        self._ssid = 0
        self._modem_rate = 1200
        self._remote_tnc = ''           # Remote endpoint IP (required)
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
        self._kiss_port = int(getattr(config, 'PACKET_KISS_PORT', 8001))
        self._pat_port = int(getattr(config, 'PACKET_PAT_PORT', 8082))
        self._aprs_comment = str(getattr(config, 'PACKET_APRS_COMMENT', 'Radio Gateway'))
        self._aprs_symbol = str(getattr(config, 'PACKET_APRS_SYMBOL', '/#'))
        self._aprs_beacon_interval = int(getattr(config, 'PACKET_APRS_BEACON_INTERVAL', 600))
        self._digipeat = bool(getattr(config, 'PACKET_DIGIPEAT', True))

        if not self._remote_tnc:
            print(f"  [Packet] WARNING: PACKET_REMOTE_TNC not set — no endpoint for Direwolf")

        self._running = True
        self.server_connected = True
        print(f"  [Packet] Plugin initialized (callsign={self._callsign}-{self._ssid}, "
              f"modem={self._modem_rate}, endpoint={self._remote_tnc or 'NONE'})")
        return True

    def _send_endpoint_mode(self, mode):
        """Send mode command to the remote AIOC endpoint via the link server."""
        if not self._gateway or not self._gateway.link_server:
            return
        target = None
        for name in self._gateway.link_endpoints:
            ep = self._gateway.link_server._endpoints.get(name)
            if ep:
                try:
                    peer = ep.sock.getpeername()[0]
                    if peer == self._remote_tnc:
                        target = name
                        break
                except Exception:
                    pass
        if not target:
            for name in self._gateway.link_endpoints:
                if 'ftm' in name.lower() or 'aioc' in name.lower():
                    target = name
                    break
        if not target:
            print(f"  [Packet] No endpoint found matching {self._remote_tnc}")
            return
        cmd = {
            'cmd': 'mode', 'mode': mode,
            'callsign': self._callsign, 'ssid': self._ssid,
            'modem': self._modem_rate, 'kiss_port': self._kiss_port,
        }
        try:
            self._gateway.link_server.send_command_to(target, cmd)
            print(f"  [Packet] Sent mode={mode} to endpoint '{target}'")
        except Exception as e:
            print(f"  [Packet] Failed to send mode to '{target}': {e}")

    def teardown(self):
        """Stop everything and clean up."""
        self._running = False
        self._disconnect_kiss()
        print("  [Packet] Teardown complete")

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
        elif action == 'mute':
            self.muted = not self.muted
            return {"ok": True, "muted": self.muted}

        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return current TNC state."""
        positioned = sum(1 for s in self._aprs_stations.values() if s.get('lat') is not None)
        return {
            "plugin": self.name,
            "mode": self._mode,
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
            "dw_audio_level": self._dw_audio_level,
            "dw_audio_peak": self._dw_audio_peak,
            "log_tail": list(self._direwolf_log)[-15:],
        }

    # ── Mode switching ────────────────────────────────────────────────

    def _set_mode(self, mode):
        """Switch TNC mode — tells endpoint to start/stop Direwolf."""
        if mode not in ('idle', 'aprs', 'winlink', 'bbs'):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if mode == self._mode:
            return {"ok": True, "mode": self._mode}

        print(f"  [Packet] Mode: {self._mode} -> {mode}")

        # Disconnect current KISS
        self._disconnect_kiss()
        self._mode = mode

        if mode == 'idle':
            self._send_endpoint_mode('audio')
            return {"ok": True, "mode": "idle"}

        if not self._remote_tnc:
            self._mode = 'idle'
            return {"ok": False, "error": "PACKET_REMOTE_TNC not configured"}

        # Tell endpoint to switch to data mode (starts Direwolf)
        self._send_endpoint_mode('data')
        # Connect KISS TCP to remote Direwolf
        threading.Thread(target=self._kiss_connect_loop, daemon=True,
                         name="KISSConnect").start()
        return {"ok": True, "mode": mode}

    def _disconnect_kiss(self):
        """Close KISS TCP connection."""
        if self._kiss_sock:
            try:
                self._kiss_sock.close()
            except Exception:
                pass
            self._kiss_sock = None
            self._kiss_connected = False

    # ── KISS TCP client ───────────────────────────────────────────────

    def _kiss_connect_loop(self):
        """Connect to remote Direwolf's KISS TCP port with retries."""
        kiss_host = self._remote_tnc
        attempt = 0
        while self._running and self._mode != 'idle':
            attempt += 1
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((kiss_host, self._kiss_port))
                self._kiss_sock = sock
                self._kiss_connected = True
                print(f"  [Packet] KISS connected ({kiss_host}:{self._kiss_port})")
                self._kiss_reader()
                # Reader returned = disconnected
                self._kiss_connected = False
                if self._running and self._mode != 'idle':
                    print(f"  [Packet] KISS disconnected, reconnecting in 5s...")
                    time.sleep(5)
                    continue
                return
            except Exception:
                if attempt % 10 == 1:
                    print(f"  [Packet] KISS connect to {kiss_host}:{self._kiss_port} attempt {attempt}...")
                time.sleep(2)

    def _kiss_reader(self):
        """Read KISS frames from Direwolf and dispatch to mode handler."""
        FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
        buf = bytearray()
        in_frame = False

        while self._running and self._kiss_sock:
            try:
                data = self._kiss_sock.recv(4096)
                if not data:
                    break
                for byte in data:
                    if byte == FEND:
                        if in_frame and len(buf) > 1:
                            if (buf[0] & 0x0F) == 0:  # Data frame
                                self._handle_ax25_frame(bytes(buf[1:]))
                        buf = bytearray()
                        in_frame = True
                    elif in_frame:
                        if byte == FESC:
                            pass
                        elif len(buf) > 0 and buf[-1] == FESC:
                            buf[-1] = TFEND if byte == TFEND else (TFESC if byte == TFESC else byte)
                        else:
                            buf.append(byte)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"  [Packet] KISS read error: {e}")
                break

        self._kiss_connected = False
        print(f"  [Packet] KISS disconnected")

    # ── AX.25 frame parsing ───────────────────────────────────────────

    def _handle_ax25_frame(self, frame):
        """Parse and dispatch an AX.25 frame."""
        self._packet_count += 1
        try:
            if len(frame) < 14:
                return

            dst_call = ''.join(chr(b >> 1) for b in frame[0:6]).strip()
            dst_ssid = (frame[6] >> 1) & 0x0F
            src_call = ''.join(chr(b >> 1) for b in frame[7:13]).strip()
            src_ssid = (frame[13] >> 1) & 0x0F

            # Digipeater path
            path = []
            info_start = 14
            if not (frame[13] & 0x01):
                pos = 14
                while pos + 7 <= len(frame):
                    digi_call = ''.join(chr(b >> 1) for b in frame[pos:pos+6]).strip()
                    digi_ssid = (frame[pos + 6] >> 1) & 0x0F
                    h_bit = bool(frame[pos + 6] & 0x80)
                    digi = f"{digi_call}-{digi_ssid}" if digi_ssid else digi_call
                    path.append({'call': digi, 'used': h_bit})
                    if frame[pos + 6] & 0x01:
                        info_start = pos + 7
                        break
                    pos += 7
                else:
                    info_start = pos

            if info_start + 2 <= len(frame):
                info = frame[info_start + 2:]
            else:
                info = b''

            src = f"{src_call}-{src_ssid}" if src_ssid else src_call
            dst = f"{dst_call}-{dst_ssid}" if dst_ssid else dst_call
            path_str = ','.join(p['call'] + ('*' if p['used'] else '') for p in path)

            pkt = {
                'time': time.time(),
                'src': src, 'dst': dst,
                'path': path_str,
                'info': info.decode('ascii', errors='replace'),
            }

            if self._mode == 'aprs':
                self._handle_aprs_packet(src, dst, info, path)
                st = self._aprs_stations.get(src, {})
                if st.get('type') and st['type'] != 'unknown':
                    summary = st['type']
                    if st.get('lat') is not None:
                        summary += f" [{st['lat']:.3f},{st['lon']:.3f}]"
                    if st.get('comment'):
                        summary += f" {st['comment']}"
                    pkt['info'] = summary
            elif self._mode == 'bbs':
                self._handle_bbs_packet(src, info)

            self._decoded_packets.append(pkt)
        except Exception as e:
            self._direwolf_log.append(f"[parse-err] {e}")

    # ── APRS handling ─────────────────────────────────────────────────

    def _handle_aprs_packet(self, src, dst, info, path=None):
        """Parse APRS position from info field."""
        try:
            info_str = info.decode('latin-1', errors='replace')
            lat, lon, symbol, comment = None, None, '', ''
            ptype = 'unknown'

            if not info_str:
                return

            dtype = info_str[0]

            if dtype in '`\x1c\x1d\'':
                try:
                    lat, lon, symbol, comment = self._parse_mice(dst, info)
                    if lat is not None:
                        ptype = 'mic-e'
                except Exception:
                    pass
            elif dtype in '!/=@':
                lat, lon, symbol, comment, ptype = self._parse_position(info_str)
            elif dtype == '>':
                comment = info_str[1:].strip()
                ptype = 'status'
            elif dtype == ':':
                comment = info_str[1:].strip()
                ptype = 'message'
            elif dtype == 'T':
                comment = info_str[1:].strip()
                ptype = 'telemetry'
            elif dtype == ';':
                lat, lon, symbol, comment, ptype = self._parse_object(info_str)
            elif dtype == ')':
                comment = info_str[1:].strip()
                ptype = 'item'
            elif dtype == '}':
                comment = info_str[1:].strip()
                ptype = 'third-party'

            # Parse weather data
            if comment and ptype in ('position', 'weather'):
                wx = self._parse_weather(comment)
                if wx:
                    comment = wx
                    ptype = 'weather'

            # Clean encoded junk from comments
            if comment and ptype not in ('weather',):
                comment = re.sub(r'\|[!-{]{2,}?\|', '', comment)
                comment = re.sub(r'![!-{]{2}[!-{]?!', '', comment)
                comment = comment.strip()

            relayed_by = [p['call'] for p in (path or []) if p.get('used')]

            self._aprs_stations[src] = {
                'lat': lat, 'lon': lon, 'symbol': symbol,
                'comment': comment[:120] if comment else '',
                'last_heard': time.time(),
                'type': ptype,
                'raw': info_str[:120],
                'path': relayed_by,
            }

            for digi_call in relayed_by:
                if digi_call not in self._aprs_stations:
                    self._aprs_stations[digi_call] = {
                        'lat': None, 'lon': None, 'symbol': '/#',
                        'comment': 'digipeater (heard relaying)',
                        'last_heard': time.time(),
                        'type': 'digi', 'raw': '', 'path': [],
                    }
                else:
                    self._aprs_stations[digi_call]['last_heard'] = time.time()
        except Exception:
            pass

    @staticmethod
    def _parse_position(info_str):
        """Parse APRS position from ! / = @ data types."""
        lat, lon, symbol, comment = None, None, '', ''
        dtype = info_str[0]

        if dtype in '@/':
            pos_str = info_str[8:]
        else:
            pos_str = info_str[1:]

        if not pos_str:
            return lat, lon, symbol, comment, 'unknown'

        # Compressed format
        if len(pos_str) >= 13 and pos_str[0] in '/\\' and not pos_str[1].isdigit():
            try:
                sym_table = pos_str[0]
                y = sum((ord(pos_str[1 + i]) - 33) * (91 ** (3 - i)) for i in range(4))
                x = sum((ord(pos_str[5 + i]) - 33) * (91 ** (3 - i)) for i in range(4))
                lat = 90.0 - y / 380926.0
                lon = -180.0 + x / 190463.0
                sym_code = pos_str[9]
                symbol = sym_table + sym_code
                comment = pos_str[13:].strip()
                return lat, lon, symbol, comment, 'position'
            except (ValueError, IndexError):
                pass

        # Uncompressed format
        if len(pos_str) >= 19:
            try:
                lat_str = pos_str[0:8]
                sym_table = pos_str[8]
                lon_str = pos_str[9:18]
                sym_code = pos_str[18]
                if lat_str[-1] in 'NS' and lon_str[-1] in 'EW':
                    lat = int(lat_str[0:2]) + float(lat_str[2:7]) / 60.0
                    if lat_str[-1] == 'S': lat = -lat
                    lon = int(lon_str[0:3]) + float(lon_str[3:8]) / 60.0
                    if lon_str[-1] == 'W': lon = -lon
                    symbol = sym_table + sym_code
                    comment = pos_str[19:].strip()
                    return lat, lon, symbol, comment, 'position'
            except (ValueError, IndexError):
                pass

        return lat, lon, symbol, comment, 'unknown'

    @staticmethod
    def _parse_object(info_str):
        """Parse APRS object report."""
        lat, lon, symbol, comment = None, None, '', ''
        try:
            if len(info_str) < 27:
                return lat, lon, symbol, comment, 'object'
            after_name = info_str[11:]
            if len(after_name) >= 7 and after_name[6] in 'zh/':
                pos_str = after_name[7:]
            else:
                pos_str = after_name
            if len(pos_str) >= 19:
                lat_str = pos_str[0:8]
                sym_table = pos_str[8]
                lon_str = pos_str[9:18]
                sym_code = pos_str[18]
                if lat_str[-1] in 'NS' and lon_str[-1] in 'EW':
                    lat = int(lat_str[0:2]) + float(lat_str[2:7]) / 60.0
                    if lat_str[-1] == 'S': lat = -lat
                    lon = int(lon_str[0:3]) + float(lon_str[3:8]) / 60.0
                    if lon_str[-1] == 'W': lon = -lon
                    symbol = sym_table + sym_code
                    comment = pos_str[19:].strip()
        except (ValueError, IndexError):
            pass
        return lat, lon, symbol, comment, 'object'

    @staticmethod
    def _parse_weather(comment):
        """Try to parse APRS weather data from a position comment."""
        if not comment or len(comment) < 10:
            return None
        s = comment
        if s[0] == '_':
            s = s[1:]
        if len(s) < 7 or s[3] != '/' or not s[0:3].isdigit() or not s[4:7].isdigit():
            return None
        wx_fields = sum(1 for tag in ('g', 't', 'r', 'p', 'P', 'h', 'b', 'L', 'l', 's') if tag in s[7:])
        if wx_fields < 2:
            return None
        parts = [f"wind {s[0:3]}/{s[4:7]}mph"]
        rest = s[7:]
        idx = 0
        while idx < len(rest):
            c = rest[idx]
            if c == 'g' and idx + 3 <= len(rest):
                parts.append(f"gust {rest[idx+1:idx+4]}mph"); idx += 4
            elif c == 't' and idx + 3 <= len(rest):
                val = rest[idx+1:idx+4]
                if val.strip('.'): parts.append(f"temp {val}F")
                idx += 4
            elif c == 'r' and idx + 3 <= len(rest):
                parts.append(f"rain/1h {rest[idx+1:idx+4]}"); idx += 4
            elif c == 'p' and idx + 3 <= len(rest):
                parts.append(f"rain/24h {rest[idx+1:idx+4]}"); idx += 4
            elif c == 'P' and idx + 3 <= len(rest):
                parts.append(f"rain/mid {rest[idx+1:idx+4]}"); idx += 4
            elif c == 'h' and idx + 2 <= len(rest):
                parts.append(f"hum {rest[idx+1:idx+3]}%"); idx += 3
            elif c == 'b' and idx + 5 <= len(rest):
                try: parts.append(f"baro {float(rest[idx+1:idx+6]) / 10.0:.1f}mb")
                except ValueError: pass
                idx += 6
            else:
                tail = rest[idx:].strip()
                if tail: parts.append(tail)
                break
        return ' '.join(parts)

    @staticmethod
    def _parse_mice(dst, info):
        """Parse MIC-E encoded position from destination + info fields."""
        info_str = info.decode('latin-1', errors='replace')
        dst_str = dst.split('-')[0]

        if len(dst_str) < 6 or len(info_str) < 9:
            return None, None, '', ''

        _mice_digits = {
            '0': (0, False, False), '1': (1, False, False), '2': (2, False, False),
            '3': (3, False, False), '4': (4, False, False), '5': (5, False, False),
            '6': (6, False, False), '7': (7, False, False), '8': (8, False, False),
            '9': (9, False, False),
            'A': (0, True, False), 'B': (1, True, False), 'C': (2, True, False),
            'D': (3, True, False), 'E': (4, True, False), 'F': (5, True, False),
            'G': (6, True, False), 'H': (7, True, False), 'I': (8, True, False),
            'J': (9, True, False),
            'K': (0, True, True), 'L': (1, True, True), 'P': (0, True, True),
            'Q': (1, True, True), 'R': (2, True, True), 'S': (3, True, True),
            'T': (4, True, True), 'U': (5, True, True), 'V': (6, True, True),
            'W': (7, True, True), 'X': (8, True, True), 'Y': (9, True, True),
            'Z': (0, True, True),
        }

        digits = []
        north = True
        west = True
        lon_offset = 0
        for i, c in enumerate(dst_str[:6]):
            if c not in _mice_digits:
                return None, None, '', ''
            d, custom, msg_bit = _mice_digits[c]
            digits.append(d)
            if i == 3: north = custom
            if i == 4: lon_offset = 100 if custom else 0
            if i == 5: west = custom

        lat_deg = digits[0] * 10 + digits[1]
        lat_min = digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100.0
        lat = lat_deg + lat_min / 60.0
        if not north: lat = -lat

        d28 = ord(info_str[1]) - 28
        m28 = ord(info_str[2]) - 28
        h28 = ord(info_str[3]) - 28

        lon_deg = d28 + lon_offset
        if 180 <= lon_deg <= 189: lon_deg -= 80
        elif 190 <= lon_deg <= 199: lon_deg -= 190

        lon_min = m28
        if lon_min >= 60: lon_min -= 60

        lon = lon_deg + (lon_min + h28 / 100.0) / 60.0
        if west: lon = -lon

        symbol = ''
        if len(info_str) >= 9:
            symbol = info_str[8] + info_str[7]

        comment = ''
        if len(info_str) > 9:
            comment = PacketRadioPlugin._clean_mice_comment(info_str[9:])

        return lat, lon, symbol, comment

    @staticmethod
    def _clean_mice_comment(tail):
        """Strip MIC-E type bytes, radio codes, telemetry, and binary junk."""
        if not tail:
            return ''
        s = tail
        # Remove leading MIC-E type/status byte (NOT " which starts Kenwood codes)
        if s and s[0] in '`\'>=]\x1c\x1d':
            s = s[1:]
        # Remove Kenwood/Yaesu radio type codes: "XX} pattern
        s = re.sub(r'^"[^"]{1,3}\}', '', s)
        # Remove Base91 telemetry blocks: |....|
        s = re.sub(r'\|[!-{]{2,}?\|', '', s)
        # Remove DAO precision extensions: !xx!
        s = re.sub(r'![!-{]{2}[!-{]?!', '', s)
        # Remove trailing MIC-E device suffixes
        s = re.sub(r'_[0-9#"()]+$', '', s)
        # Remove orphan pipe-delimited fragments
        s = re.sub(r'\|[^|]{0,6}$', '', s)
        # Strip non-printable chars
        s = ''.join(c for c in s if ' ' <= c < '\x7f')
        s = s.strip()
        if len(s) <= 2 and not any(c.isalnum() for c in s):
            s = ''
        return s

    # ── APRS TX (stubs) ──────────────────────────────────────────────

    def _send_aprs_beacon(self):
        if not self._kiss_connected:
            return {"ok": False, "error": "KISS not connected"}
        return {"ok": True, "note": "beacon sent via Direwolf config timer"}

    def _send_aprs_message(self, to_call, message):
        if not to_call or not message:
            return {"ok": False, "error": "to and message required"}
        if not self._kiss_connected:
            return {"ok": False, "error": "KISS not connected"}
        return {"ok": False, "error": "not yet implemented"}

    # ── BBS handling ──────────────────────────────────────────────────

    def _handle_bbs_packet(self, src, info):
        try:
            self._bbs_buffer.append(info.decode('ascii', errors='replace'))
        except Exception:
            pass

    def _bbs_connect(self, callsign):
        if not callsign:
            return {"ok": False, "error": "callsign required"}
        if not self._kiss_connected:
            return {"ok": False, "error": "KISS not connected"}
        self._bbs_callsign = callsign.upper()
        self._bbs_connected = True
        self._bbs_buffer.clear()
        self._bbs_buffer.append(f"*** Connecting to {self._bbs_callsign}...")
        return {"ok": True, "callsign": self._bbs_callsign}

    def _bbs_disconnect(self):
        self._bbs_connected = False
        self._bbs_buffer.append("*** Disconnected")
        self._bbs_callsign = ''
        return {"ok": True}

    def _bbs_send(self, text):
        if not self._bbs_connected:
            return {"ok": False, "error": "not connected"}
        if not text:
            return {"ok": False, "error": "text required"}
        self._bbs_buffer.append(f"> {text}")
        return {"ok": True}
