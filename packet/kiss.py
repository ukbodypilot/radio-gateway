"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""

import re
import socket
import threading
import time


class _KISSMixin:
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

    # If KISS hasn't connected after this many attempts, the state machine
    # reports ERROR so the UI can show "KISS giving up — Direwolf?" instead
    # of leaving phase=='starting' forever. Reconnects after a steady
    # transition keep retrying — they don't toggle phase.
    _KISS_STARTING_FAIL_AFTER = 10

    def _kiss_connect_loop(self):
        """Connect to remote Direwolf's KISS TCP port with retries.

        State machine integration:
          * On the first successful connect while phase=STARTING, declare
            STEADY (for aprs/bbs) — winlink waits for Pat to mark steady.
          * If we burn through _KISS_STARTING_FAIL_AFTER attempts during
            STARTING, mark ERROR with a meaningful last_error.
          * Reconnects after a previous steady state stay quiet — they
            don't re-toggle phase.
        """
        from packet.state import PHASE_STARTING
        kiss_host = self._get_endpoint_ip()
        attempt = 0
        starting_fail_reported = False
        while self._running and self._mode != 'idle':
            attempt += 1
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((kiss_host, self._kiss_port))
                self._kiss_sock = sock
                self._kiss_connected = True
                print(f"  [Packet] KISS connected ({kiss_host}:{self._kiss_port})")
                # First-connect bookkeeping for the state machine.
                if self._phase == PHASE_STARTING and self._mode != 'winlink':
                    self._reach_steady()
                # winlink: stay in STARTING until _delayed_pat_start finishes.
                self._kiss_reader()
                # Reader returned = disconnected
                self._kiss_connected = False
                if self._running and self._mode != 'idle':
                    print(f"  [Packet] KISS disconnected, reconnecting in 5s...")
                    time.sleep(5)
                    continue
                return
            except Exception as e:
                if attempt % 10 == 1:
                    print(f"  [Packet] KISS connect to {kiss_host}:{self._kiss_port} attempt {attempt}...")
                # If we've been STARTING for too many failed tries, report
                # ERROR so the UI doesn't hide the situation. Only fire
                # this once per starting cycle.
                if (not starting_fail_reported
                        and self._phase == PHASE_STARTING
                        and attempt >= self._KISS_STARTING_FAIL_AFTER):
                    self._fail(f"KISS connect to {kiss_host}:{self._kiss_port} failed: {e}")
                    starting_fail_reported = True
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
            tnc = getattr(self._gateway, 'packet_tnc', None) if self._gateway else None
            if tnc:
                tnc._log_tail.append(f"[parse-err] {e}")

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
            # _clean_mice_comment is a @staticmethod; reach it through the
            # class (we don't have ``self`` in a staticmethod) — using
            # _KISSMixin here keeps it correct even if someone subclasses.
            comment = _KISSMixin._clean_mice_comment(info_str[9:])

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

    @staticmethod
    def _agw_frame(port, kind, call_from, call_to, data=b''):
        """Build a 36-byte AGW protocol frame."""
        import struct
        hdr = bytearray(36)
        hdr[0] = port & 0xFF
        hdr[4] = ord(kind[0])
        cf = call_from.encode()[:10]
        ct = call_to.encode()[:10]
        hdr[8:8+len(cf)] = cf
        hdr[18:18+len(ct)] = ct
        struct.pack_into('<I', hdr, 28, len(data))
        return bytes(hdr) + data

    def _bbs_connect(self, callsign):
        """Connect to a remote station via AGW connected mode."""
        import struct
        if not callsign:
            return {"ok": False, "error": "callsign required"}
        if self._bbs_connected:
            return {"ok": False, "error": f"already connected to {self._bbs_callsign}"}
        if not self._find_endpoint() and not self._remote_tnc:
            return {"ok": False, "error": "no AIOC endpoint connected"}

        callsign = callsign.upper().strip()
        mycall = f"{self._callsign}-{self._ssid}"
        self._bbs_buffer.clear()
        self._bbs_buffer.append(f"*** Connecting to {callsign} via AGW...")
        self._bbs_callsign = callsign

        def _session():
            import socket
            try:
                s = socket.socket()
                s.settimeout(60)
                s.connect((self._get_endpoint_ip(), 8010))
                self._bbs_agw_sock = s

                # Register callsign
                s.sendall(self._agw_frame(0, 'X', mycall, ''))
                time.sleep(0.3)

                # Send connect
                s.sendall(self._agw_frame(0, 'C', mycall, callsign))

                # Reader loop
                buf = b''
                while self._bbs_agw_sock:
                    try:
                        data = s.recv(4096)
                        if not data:
                            break
                        buf += data
                        while len(buf) >= 36:
                            data_len = struct.unpack('<I', buf[28:32])[0]
                            if len(buf) < 36 + data_len:
                                break
                            kind = chr(buf[4])
                            cf = buf[8:18].decode('ascii', errors='replace').strip('\x00')
                            ct = buf[18:28].decode('ascii', errors='replace').strip('\x00')
                            payload = buf[36:36+data_len]
                            buf = buf[36+data_len:]
                            text = payload.decode('ascii', errors='replace')

                            if kind == 'C' and text:
                                # Connection status
                                self._bbs_buffer.append(f"*** {text.strip()}")
                                if 'CONNECTED' in text.upper() and 'RETRYOUT' not in text.upper():
                                    self._bbs_connected = True
                            elif kind == 'D' and text:
                                # Data from remote
                                for line in text.splitlines():
                                    self._bbs_buffer.append(line)
                            elif kind == 'd':
                                # Disconnect
                                self._bbs_buffer.append(f"*** {text.strip()}" if text.strip() else "*** Disconnected")
                                self._bbs_connected = False
                                break
                    except socket.timeout:
                        continue
                    except Exception as e:
                        self._bbs_buffer.append(f"*** Error: {e}")
                        break

            except Exception as e:
                self._bbs_buffer.append(f"*** Connection failed: {e}")
            finally:
                self._bbs_connected = False
                self._bbs_agw_sock = None
                self._bbs_buffer.append("*** Session ended")

        self._bbs_reader_thread = threading.Thread(target=_session, daemon=True, name="BBS-session")
        self._bbs_reader_thread.start()
        return {"ok": True, "callsign": callsign}

    def _bbs_disconnect(self):
        """Disconnect the BBS AGW session."""
        if self._bbs_agw_sock and self._bbs_callsign:
            mycall = f"{self._callsign}-{self._ssid}"
            try:
                self._bbs_agw_sock.sendall(
                    self._agw_frame(0, 'd', mycall, self._bbs_callsign))
            except Exception:
                pass
        # Close socket to break reader loop
        s = self._bbs_agw_sock
        self._bbs_agw_sock = None
        if s:
            try:
                s.close()
            except Exception:
                pass
        self._bbs_connected = False
        self._bbs_buffer.append("*** Disconnected")
        self._bbs_callsign = ''
        return {"ok": True}

    def _bbs_send(self, text):
        """Send a line of text to the connected BBS via AGW data frame."""
        if not self._bbs_connected or not self._bbs_agw_sock:
            return {"ok": False, "error": "not connected"}
        if not text:
            return {"ok": False, "error": "text required"}
        mycall = f"{self._callsign}-{self._ssid}"
        try:
            payload = (text + '\r').encode('ascii', errors='replace')
            self._bbs_agw_sock.sendall(
                self._agw_frame(0, 'D', mycall, self._bbs_callsign, payload))
            self._bbs_buffer.append(f"> {text}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
