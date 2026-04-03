"""Packet Radio Plugin — Direwolf TNC integration for APRS, Winlink, and BBS.

Appears as both a source (TNC [RX] — modulated TX audio from Direwolf) and
a sink (TNC [TX] — radio RX audio fed to Direwolf for decoding) on the
routing page.  The user wires the TNC to any radio via buses.

Audio flow:
  RX: Radio audio → bus → put_audio() → UDP → Direwolf → decoded packets
  TX: Direwolf → ALSA loopback → capture thread → get_audio(ptt=True) → bus → radio

Direwolf runs as a child process on the gateway machine.  Config is generated
dynamically based on the current mode (APRS / Winlink / BBS / idle).
"""

import collections
import math
import os
import queue
import signal
import socket
import struct
import subprocess
import threading
import time

import numpy as np

# Optional — imported lazily to avoid hard dependency at import time
_kiss3 = None
_aprs3 = None


class PacketRadioPlugin:
    """Software TNC (Direwolf) plugin for the gateway routing system."""

    name = "tnc"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
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
        self.ptt_control = True       # TNC source triggers PTT when transmitting
        self.priority = 5
        self.sdr_priority = 5
        self.volume = 1.0
        self.duck = False
        self.muted = False
        self.audio_level = 0          # RX level (audio from Direwolf TX)
        self.tx_audio_level = 0       # TX level (audio into Direwolf)
        self.audio_boost = 1.0
        self.tx_audio_boost = 1.0
        self.server_connected = False

        # Internal state
        self._config = None
        self._gateway = None
        self._mode = 'idle'           # idle / aprs / winlink / bbs
        self._direwolf_proc = None
        self._direwolf_log = collections.deque(maxlen=200)

        # Audio bridge
        self._udp_rx_sock = None      # Sends audio TO Direwolf (RX port)
        self._udp_tx_capture = None   # PyAudio stream capturing Direwolf TX from loopback
        self._pa = None               # PyAudio instance
        self._rx_queue = collections.deque(maxlen=32)   # Direwolf TX audio → source
        self._running = False

        # Direwolf connection
        self._kiss_sock = None
        self._kiss_connected = False

        # Packet data
        self._decoded_packets = collections.deque(maxlen=500)
        self._aprs_stations = {}      # callsign → {lat, lon, symbol, comment, last_heard, raw}
        self._bbs_buffer = collections.deque(maxlen=2000)
        self._bbs_connected = False
        self._bbs_callsign = ''
        self._packet_count = 0
        self._start_time = None

        # Config values (set in setup)
        self._callsign = 'N0CALL'
        self._ssid = 0
        self._modem_rate = 1200
        self._direwolf_path = '/usr/bin/direwolf'
        self._udp_rx_port = 7355
        self._kiss_port = 8001
        self._agw_port = 8000
        self._pat_port = 8082
        self._loopback_card = 'Loopback_1'
        self._aprs_comment = 'Radio Gateway'
        self._aprs_symbol = '/#'
        self._aprs_beacon_interval = 600
        self._digipeat = True
        self._aprs_is = False
        self._aprs_is_server = 'noam.aprs2.net'
        self._aprs_is_passcode = ''
        self._lat = ''
        self._lon = ''

        # Resampling state (48kHz ↔ 44100Hz)
        self._resample_48_to_44_pos = 0.0
        self._resample_44_to_48_pos = 0.0
        self._resample_ratio_48_to_44 = 44100.0 / 48000.0  # ~0.91875
        self._resample_ratio_44_to_48 = 48000.0 / 44100.0  # ~1.08844

    # ── Setup / Teardown ──────────────────────────────────────────────

    def setup(self, config, gateway=None):
        """Initialize plugin — read config, create UDP socket. Don't start Direwolf yet."""
        if isinstance(config, dict):
            return False

        self._config = config
        self._gateway = gateway
        self._start_time = time.monotonic()

        # Read config
        self._callsign = str(getattr(config, 'PACKET_CALLSIGN', 'N0CALL')).strip().upper()
        self._ssid = int(getattr(config, 'PACKET_SSID', 0))
        self._modem_rate = int(getattr(config, 'PACKET_MODEM', 1200))
        self._direwolf_path = str(getattr(config, 'PACKET_DIREWOLF_PATH', '/usr/bin/direwolf'))
        self._udp_rx_port = int(getattr(config, 'PACKET_UDP_RX_PORT', 7355))
        self._kiss_port = int(getattr(config, 'PACKET_KISS_PORT', 8001))
        self._agw_port = int(getattr(config, 'PACKET_AGW_PORT', 8000))
        self._pat_port = int(getattr(config, 'PACKET_PAT_PORT', 8082))
        self._loopback_card = str(getattr(config, 'PACKET_LOOPBACK_CARD', 'Loopback_1'))
        self._aprs_comment = str(getattr(config, 'PACKET_APRS_COMMENT', 'Radio Gateway'))
        self._aprs_symbol = str(getattr(config, 'PACKET_APRS_SYMBOL', '/#'))
        self._aprs_beacon_interval = int(getattr(config, 'PACKET_APRS_BEACON_INTERVAL', 600))
        self._digipeat = bool(getattr(config, 'PACKET_DIGIPEAT', True))
        self._aprs_is = bool(getattr(config, 'PACKET_APRS_IS', False))
        self._aprs_is_server = str(getattr(config, 'PACKET_APRS_IS_SERVER', 'noam.aprs2.net'))
        self._aprs_is_passcode = str(getattr(config, 'PACKET_APRS_IS_PASSCODE', ''))

        # GPS position (from gateway GPS manager if available)
        if gateway and hasattr(gateway, 'gps_manager') and gateway.gps_manager:
            gps = gateway.gps_manager
            self._lat = getattr(gps, 'latitude', '')
            self._lon = getattr(gps, 'longitude', '')

        # Verify direwolf binary exists
        if not os.path.isfile(self._direwolf_path):
            print(f"  [Packet] WARNING: direwolf not found at {self._direwolf_path}")
            print(f"  [Packet] Install with: yay -S direwolf")

        # Create UDP socket for sending audio TO Direwolf
        self._udp_rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._running = True
        self.server_connected = True
        print(f"  [Packet] Plugin initialized (callsign={self._callsign}-{self._ssid}, modem={self._modem_rate})")
        return True

    def teardown(self):
        """Stop everything and clean up."""
        self._running = False
        self._stop_direwolf()
        if self._udp_rx_sock:
            try:
                self._udp_rx_sock.close()
            except Exception:
                pass
        if self._pa:
            try:
                if self._udp_tx_capture:
                    self._udp_tx_capture.stop_stream()
                    self._udp_tx_capture.close()
                self._pa.terminate()
            except Exception:
                pass
        self._pa = None
        self._udp_tx_capture = None
        print("  [Packet] Teardown complete")

    # ── Audio interface (RadioPlugin contract) ────────────────────────

    def get_audio(self, chunk_size=None):
        """Return modulated TX audio from Direwolf (for transmission via radio).

        Returns (pcm_bytes, True) when Direwolf is transmitting — the True
        flag tells the SoloBus to key PTT on the connected radio.
        """
        if not self.enabled or self.muted or not self._running:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        try:
            chunk = self._rx_queue.popleft()
        except IndexError:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering
        try:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                level = max(0, min(100, (20.0 * math.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                level = 0
            if level > self.audio_level:
                self.audio_level = int(level)
            else:
                self.audio_level = int(self.audio_level * 0.7 + level * 0.3)
        except Exception:
            pass

        # Apply boost
        if self.audio_boost != 1.0:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            chunk = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        return chunk, True  # True = trigger PTT

    def put_audio(self, pcm):
        """Receive radio RX audio from the bus and forward to Direwolf via UDP."""
        if not self._running or not self._direwolf_proc or not self._udp_rx_sock:
            return
        if not hasattr(self, '_put_count'):
            self._put_count = 0
        self._put_count += 1
        if self._put_count <= 5 or self._put_count % 200 == 0:
            print(f"  [Packet] put_audio #{self._put_count}, {len(pcm)} bytes, mode={self._mode}, dw={self._direwolf_proc is not None}")

        # TX level metering (audio going into Direwolf)
        try:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                level = max(0, min(100, (20.0 * math.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                level = 0
            if level > self.tx_audio_level:
                self.tx_audio_level = int(level)
            else:
                self.tx_audio_level = int(self.tx_audio_level * 0.7 + level * 0.3)
        except Exception:
            pass

        # Apply TX boost
        if self.tx_audio_boost != 1.0:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            pcm = np.clip(arr * self.tx_audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Send to Direwolf via UDP (48kHz native — no resample needed)
        try:
            self._udp_rx_sock.sendto(pcm, ('127.0.0.1', self._udp_rx_port))
        except Exception:
            pass

    def execute(self, cmd):
        """Handle commands from the gateway."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}
        action = cmd.get('cmd', '')

        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        elif action == 'set_mode':
            mode = cmd.get('mode', 'idle')
            return self._set_mode(mode)
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
        return {
            "plugin": self.name,
            "mode": self._mode,
            "callsign": f"{self._callsign}-{self._ssid}",
            "modem": self._modem_rate,
            "direwolf_running": self._direwolf_proc is not None and self._direwolf_proc.poll() is None,
            "kiss_connected": self._kiss_connected,
            "packet_count": self._packet_count,
            "station_count": len(self._aprs_stations),
            "bbs_connected": self._bbs_connected,
            "bbs_callsign": self._bbs_callsign,
            "uptime": round(time.monotonic() - self._start_time, 1) if self._start_time else 0,
            "rx_audio_level": self.tx_audio_level,   # audio going INTO Direwolf (TNC RX)
            "tx_audio_level": self.audio_level,       # audio coming FROM Direwolf (TNC TX)
        }

    # ── Direwolf lifecycle ────────────────────────────────────────────

    def _set_mode(self, mode):
        """Switch TNC mode — stops/restarts Direwolf with new config."""
        if mode not in ('idle', 'aprs', 'winlink', 'bbs'):
            return {"ok": False, "error": f"invalid mode: {mode}"}

        if mode == self._mode:
            return {"ok": True, "mode": self._mode}

        print(f"  [Packet] Mode: {self._mode} → {mode}")

        # Stop current
        self._stop_direwolf()
        self._mode = mode

        if mode == 'idle':
            return {"ok": True, "mode": "idle"}

        # Start Direwolf with mode-specific config
        ok = self._start_direwolf(mode)
        if not ok:
            self._mode = 'idle'
            return {"ok": False, "error": "failed to start direwolf"}

        return {"ok": True, "mode": mode}

    def _generate_config(self, mode):
        """Generate direwolf.conf content for the given mode."""
        mycall = f"{self._callsign}-{self._ssid}" if self._ssid else self._callsign
        loopback_out = f"plughw:{self._loopback_card},0,0"

        lines = [
            f"ADEVICE udp:{self._udp_rx_port} null",
            f"ARATE 48000",
            f"ACHANNELS 1",
            f"",
            f"CHANNEL 0",
            f"MYCALL {mycall}",
            f"MODEM {self._modem_rate}",
            f"",
            f"KISSPORT {self._kiss_port}",
            f"AGWPORT {self._agw_port}",
            f"",
            f"# PTT handled externally by gateway",
        ]

        if mode == 'aprs':
            if self._digipeat:
                lines.append(f"DIGIPEAT 0 0 ^WIDE[3-7]-[1-7]$|^TEST$ ^WIDE[12]-[12]$")
            if self._lat and self._lon:
                lines.append(f"PBEACON DELAY=0:30 EVERY={self._aprs_beacon_interval // 60}:{self._aprs_beacon_interval % 60:02d}"
                             f" LAT={self._lat} LONG={self._lon}"
                             f" SYMBOL={self._aprs_symbol}"
                             f" COMMENT=\"{self._aprs_comment}\"")
            if self._aprs_is and self._aprs_is_passcode:
                lines.append(f"IGSERVER {self._aprs_is_server}")
                lines.append(f"IGLOGIN {self._callsign} {self._aprs_is_passcode}")

        return '\n'.join(lines) + '\n'

    def _start_direwolf(self, mode):
        """Start Direwolf process and connect KISS TCP."""
        # Generate config
        conf_path = '/tmp/direwolf_gateway.conf'
        try:
            with open(conf_path, 'w') as f:
                f.write(self._generate_config(mode))
            print(f"  [Packet] Config written to {conf_path}")
        except Exception as e:
            print(f"  [Packet] Config write error: {e}")
            return False

        # Start loopback capture thread (reads Direwolf TX audio)
        self._start_loopback_capture()

        # Spawn Direwolf
        try:
            self._direwolf_proc = subprocess.Popen(
                [self._direwolf_path, '-c', conf_path, '-t', '0', '-d', 'o'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            )
            print(f"  [Packet] Direwolf started (PID {self._direwolf_proc.pid})")
        except Exception as e:
            print(f"  [Packet] Direwolf start error: {e}")
            return False

        # Start log reader thread
        threading.Thread(target=self._direwolf_log_reader, daemon=True, name="DirewolfLog").start()

        # Connect KISS TCP (with retries)
        threading.Thread(target=self._kiss_connect_loop, daemon=True, name="KISSConnect").start()

        return True

    def _stop_direwolf(self):
        """Stop Direwolf process and clean up connections."""
        # Close KISS
        if self._kiss_sock:
            try:
                self._kiss_sock.close()
            except Exception:
                pass
            self._kiss_sock = None
            self._kiss_connected = False

        # Stop loopback capture
        if self._udp_tx_capture and self._pa:
            try:
                self._udp_tx_capture.stop_stream()
                self._udp_tx_capture.close()
            except Exception:
                pass
            self._udp_tx_capture = None

        # Kill Direwolf
        if self._direwolf_proc:
            try:
                self._direwolf_proc.send_signal(signal.SIGTERM)
                self._direwolf_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._direwolf_proc.kill()
                self._direwolf_proc.wait(timeout=2)
            except Exception:
                pass
            print(f"  [Packet] Direwolf stopped")
            self._direwolf_proc = None

        self._rx_queue.clear()

    # ── ALSA loopback capture (reads Direwolf TX audio) ───────────────

    def _start_loopback_capture(self):
        """Open PyAudio capture on the loopback mirror to read Direwolf TX audio."""
        try:
            import pyaudio
            if not self._pa:
                self._pa = pyaudio.PyAudio()

            # Find the loopback capture device (mirror side: subdevice 1)
            loopback_name = f"plughw:{self._loopback_card},1,0"
            dev_idx = None
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if self._loopback_card in info.get('name', '') and info.get('maxInputChannels', 0) > 0:
                    # Look for subdevice 1 (the capture mirror)
                    if ',1' in info.get('name', ''):
                        dev_idx = i
                        break

            # Open capture stream at 44100Hz (Direwolf's output rate)
            kwargs = {}
            if dev_idx is not None:
                kwargs['input_device_index'] = dev_idx

            self._udp_tx_capture = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=48000,
                input=True,
                frames_per_buffer=2400,  # 50ms at 48kHz
                stream_callback=self._loopback_capture_callback,
                **kwargs,
            )
            self._udp_tx_capture.start_stream()
            print(f"  [Packet] Loopback capture started ({loopback_name}, idx={dev_idx})")
        except Exception as e:
            print(f"  [Packet] Loopback capture error: {e}")

    def _loopback_capture_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback — receives Direwolf TX audio from loopback at 48kHz."""
        import pyaudio
        if in_data and self._running:
            self._rx_queue.append(in_data)
        return (None, pyaudio.paContinue)

    # ── Resampling ────────────────────────────────────────────────────

    def _resample(self, pcm, ratio, pos_attr):
        """Resample 16-bit mono PCM by the given ratio using linear interpolation."""
        try:
            in_samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            n_in = len(in_samples)
            if n_in < 2:
                return pcm

            n_out = int(n_in * ratio)
            pos = getattr(self, pos_attr)

            positions = pos + np.arange(n_out) * (1.0 / ratio)
            indices = positions.astype(np.intp)
            fracs = positions - indices
            np.clip(indices, 0, n_in - 2, out=indices)

            out = in_samples[indices] * (1.0 - fracs) + in_samples[indices + 1] * fracs

            consumed = int(positions[-1]) + 1
            setattr(self, pos_attr, positions[-1] + (1.0 / ratio) - consumed)

            return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm

    # ── KISS TCP client ───────────────────────────────────────────────

    def _kiss_connect_loop(self):
        """Connect to Direwolf's KISS TCP port with retries."""
        for attempt in range(30):
            if not self._running or not self._direwolf_proc:
                return
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                sock.connect(('127.0.0.1', self._kiss_port))
                self._kiss_sock = sock
                self._kiss_connected = True
                print(f"  [Packet] KISS connected (port {self._kiss_port})")
                # Start reader
                self._kiss_reader()
                return
            except Exception:
                time.sleep(1)
        print(f"  [Packet] KISS connect failed after 30 attempts")

    def _kiss_reader(self):
        """Read KISS frames from Direwolf and dispatch to mode handler."""
        FEND = 0xC0
        FESC = 0xDB
        TFEND = 0xDC
        TFESC = 0xDD

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
                            # buf[0] is KISS command byte (0x00 = data frame channel 0)
                            cmd_byte = buf[0]
                            if (cmd_byte & 0x0F) == 0:  # Data frame
                                ax25_frame = bytes(buf[1:])
                                self._handle_ax25_frame(ax25_frame)
                        buf = bytearray()
                        in_frame = True
                    elif in_frame:
                        if byte == FESC:
                            pass  # Next byte is escaped
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

    def _handle_ax25_frame(self, frame):
        """Parse and dispatch an AX.25 frame."""
        self._packet_count += 1

        try:
            # Parse AX.25 header (minimum 14 bytes for src+dst addresses)
            if len(frame) < 14:
                return

            # Destination (7 bytes: 6 callsign + 1 SSID)
            dst_call = ''.join(chr(b >> 1) for b in frame[0:6]).strip()
            dst_ssid = (frame[6] >> 1) & 0x0F

            # Source (7 bytes)
            src_call = ''.join(chr(b >> 1) for b in frame[7:13]).strip()
            src_ssid = (frame[13] >> 1) & 0x0F

            # Info field (after control + PID bytes)
            info_start = 14
            # Skip digipeater addresses
            if not (frame[13] & 0x01):  # More addresses follow
                pos = 14
                while pos + 7 <= len(frame):
                    if frame[pos + 6] & 0x01:  # Last address
                        info_start = pos + 7
                        break
                    pos += 7
                else:
                    info_start = pos

            # Control + PID (2 bytes typically)
            if info_start + 2 <= len(frame):
                info = frame[info_start + 2:]
            else:
                info = b''

            src = f"{src_call}-{src_ssid}" if src_ssid else src_call
            dst = f"{dst_call}-{dst_ssid}" if dst_ssid else dst_call

            packet_str = f"{src}>{dst}: {info.decode('ascii', errors='replace')}"
            self._decoded_packets.append({
                'time': time.time(),
                'src': src,
                'dst': dst,
                'info': info.decode('ascii', errors='replace'),
                'raw': packet_str,
            })

            # Mode-specific handling
            if self._mode == 'aprs':
                self._handle_aprs_packet(src, dst, info)
            elif self._mode == 'bbs':
                self._handle_bbs_packet(src, info)

        except Exception as e:
            self._direwolf_log.append(f"[parse-err] {e}")

    # ── APRS handling ─────────────────────────────────────────────────

    def _handle_aprs_packet(self, src, dst, info):
        """Parse APRS position/status from info field and track station."""
        try:
            info_str = info.decode('ascii', errors='replace')
            # Basic APRS position parsing (! or / or = or @ formats)
            lat, lon = None, None

            if len(info_str) > 0 and info_str[0] in '!/=@':
                # Uncompressed position: !DDMM.MMN/DDDMM.MMW
                # or compressed position (base91)
                try:
                    if len(info_str) >= 20 and info_str[0] in '!/=@':
                        lat_str = info_str[1:9]   # DDMM.MMN
                        lon_str = info_str[10:19]  # DDDMM.MMW
                        if lat_str[-1] in 'NS' and lon_str[-1] in 'EW':
                            lat_deg = int(lat_str[0:2]) + float(lat_str[2:7]) / 60.0
                            if lat_str[-1] == 'S':
                                lat_deg = -lat_deg
                            lon_deg = int(lon_str[0:3]) + float(lon_str[3:8]) / 60.0
                            if lon_str[-1] == 'W':
                                lon_deg = -lon_deg
                            lat, lon = lat_deg, lon_deg
                except (ValueError, IndexError):
                    pass

            comment = info_str[20:] if len(info_str) > 20 else ''

            self._aprs_stations[src] = {
                'lat': lat,
                'lon': lon,
                'symbol': info_str[9] + info_str[19] if len(info_str) >= 20 else '',
                'comment': comment.strip(),
                'last_heard': time.time(),
                'raw': info_str,
            }
        except Exception:
            pass

    def _send_aprs_beacon(self):
        """Trigger an APRS position beacon via KISS."""
        # Direwolf handles beaconing via config — this forces an immediate one
        # by sending a UI frame via KISS
        if not self._kiss_connected or not self._kiss_sock:
            return {"ok": False, "error": "KISS not connected"}

        # Build simple beacon frame
        # For now, rely on Direwolf's built-in PBEACON
        return {"ok": True, "note": "beacon sent via Direwolf config timer"}

    def _send_aprs_message(self, to_call, message):
        """Send an APRS message via KISS."""
        if not to_call or not message:
            return {"ok": False, "error": "to and message required"}
        if not self._kiss_connected or not self._kiss_sock:
            return {"ok": False, "error": "KISS not connected"}

        # TODO: build and send APRS message frame via KISS
        return {"ok": False, "error": "not yet implemented"}

    # ── BBS handling ──────────────────────────────────────────────────

    def _handle_bbs_packet(self, src, info):
        """Append incoming BBS data to the terminal buffer."""
        try:
            text = info.decode('ascii', errors='replace')
            self._bbs_buffer.append(text)
        except Exception:
            pass

    def _bbs_connect(self, callsign):
        """Initiate AX.25 connection to a BBS."""
        if not callsign:
            return {"ok": False, "error": "callsign required"}
        if not self._kiss_connected:
            return {"ok": False, "error": "KISS not connected"}
        self._bbs_callsign = callsign.upper()
        self._bbs_connected = True
        self._bbs_buffer.clear()
        self._bbs_buffer.append(f"*** Connecting to {self._bbs_callsign}...")
        # TODO: send AX.25 SABM frame via KISS
        return {"ok": True, "callsign": self._bbs_callsign}

    def _bbs_disconnect(self):
        """Disconnect from BBS."""
        self._bbs_connected = False
        self._bbs_buffer.append("*** Disconnected")
        self._bbs_callsign = ''
        # TODO: send AX.25 DISC frame via KISS
        return {"ok": True}

    def _bbs_send(self, text):
        """Send text to connected BBS."""
        if not self._bbs_connected:
            return {"ok": False, "error": "not connected"}
        if not text:
            return {"ok": False, "error": "text required"}
        self._bbs_buffer.append(f"> {text}")
        # TODO: send as AX.25 I-frame via KISS
        return {"ok": True}

    # ── Direwolf log reader ───────────────────────────────────────────

    def _direwolf_log_reader(self):
        """Read Direwolf stdout/stderr and store in log buffer."""
        proc = self._direwolf_proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip('\n')
                self._direwolf_log.append(line)
        except Exception:
            pass
