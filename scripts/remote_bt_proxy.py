#!/usr/bin/env python3
"""
remote_bt_proxy.py — Remote Bluetooth proxy for TH-D75.

Runs on a machine with a local Bluetooth adapter near the radio.
Implements the D75_CAT.py TCP protocol so radio-gateway connects to
this machine transparently — no changes needed on the gateway.

Ports:
  9750 — CAT text protocol (compatible with D75_CAT.py / D75CATClient)
  9751 — Raw PCM audio (8 kHz, 16-bit signed LE, mono — same as AudioServer)

Usage:
  python3 remote_bt_proxy.py

Requirements:
  pip3 install pyserial

Bluetooth setup (run once on this machine):
  sudo bluetoothctl pair 90:CE:B8:D6:55:0A
  sudo bluetoothctl trust 90:CE:B8:D6:55:0A

Gateway config (on the gateway machine):
  D75_HOST = <this-machine-ip>
  D75_PORT = 9750
  D75_AUDIO_PORT = 9751
"""

import ctypes
import json
import os
import socket
import struct
import subprocess
import threading
import time

# ── Configuration — edit these ─────────────────────────────────────────────────
D75_MAC      = "90:CE:B8:D6:55:0A"
SERVER_HOST  = "0.0.0.0"
CAT_PORT     = 9750
AUDIO_PORT   = 9751
PASSWORD     = ""           # Match gateway's D75_CAT_PASSWORD — leave empty for none
VERBOSE      = False

# ── Bluetooth socket constants (linux/bluetooth.h) ─────────────────────────────
BTPROTO_RFCOMM      = 3
BTPROTO_SCO         = 2
SOL_BLUETOOTH       = 274
BT_VOICE            = 11
BT_VOICE_CVSD_16BIT = 0x0060
SOL_SCO             = 17
SCO_OPTIONS         = 1
AUDIO_FRAME_SIZE    = 48     # bytes per SCO frame (D75 uses 48-byte SCO frames)


# ── SCO connect workaround ─────────────────────────────────────────────────────
# Python's socket.connect() for BTPROTO_SCO has inconsistent address parsing
# across distros/versions. Call C connect() directly with a hand-built sockaddr.

_AF_BLUETOOTH = 31   # same as socket.AF_BLUETOOTH

def _sco_connect(sock, mac, timeout=8.0):
    """Connect a BTPROTO_SCO socket by calling libc connect() via ctypes.

    Builds sockaddr_sco { sa_family_t(2), bdaddr_t(6) } manually.
    bdaddr is stored little-endian (reversed MAC octets).
    Handles EINPROGRESS (non-blocking socket) by waiting with select().
    """
    import select as _select
    bdaddr = bytes(reversed([int(x, 16) for x in mac.split(':')]))
    sockaddr = struct.pack('=H6s', _AF_BLUETOOTH, bdaddr)
    libc = ctypes.CDLL(None, use_errno=True)
    ret = libc.connect(sock.fileno(), sockaddr, ctypes.c_uint32(len(sockaddr)))
    if ret < 0:
        errno = ctypes.get_errno()
        if errno == 115:  # EINPROGRESS — non-blocking connect in progress
            # Wait for the socket to become writable (connect complete)
            _, writable, _ = _select.select([], [sock], [], timeout)
            if not writable:
                raise OSError(110, "Connection timed out")  # ETIMEDOUT
            # Check for connect error
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                raise OSError(err, os.strerror(err))
        else:
            raise OSError(errno, os.strerror(errno))


# ═══════════════════════════════════════════════════════════════════════════════
# BT PAIRING HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_paired(mac):
    """Return True if already paired; print warning if not."""
    try:
        r = subprocess.run(['bluetoothctl', 'info', mac],
                           capture_output=True, text=True, timeout=5)
        return 'Paired: yes' in r.stdout
    except Exception as e:
        print(f"[BT] pair-check error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# SERIAL MANAGER  (RFCOMM channel 2 = SPP / CAT)
# ═══════════════════════════════════════════════════════════════════════════════

class SerialManager:
    """RFCOMM ch2 serial connection to D75 for CAT control.

    Binds /dev/rfcomm0 via the `rfcomm` utility (avoids conflict with SCO
    audio on ch1, matching the pattern used by D75_CAT.py).
    Maintains a background poll thread that keeps radio state fresh.
    All sends are serialised via a lock so the poll thread and CAT server
    can both call send_raw() safely.
    """

    def __init__(self, mac):
        self._mac = mac
        self._ser = None
        self._lock = threading.Lock()
        self._connected = False

        # Cached radio state (updated by _poll_loop)
        self._state_lock = threading.Lock()
        self.model_id     = ''
        self.fw_version   = ''
        self.serial_number = ''
        self.band         = [{}, {}]
        self.active_band  = 0
        self.dual_band    = 0
        self.bluetooth    = False
        self.transmitting = False

        self._poll_stop   = threading.Event()
        self._poll_thread = None

    # ── public ─────────────────────────────────────────────────────────────────

    @property
    def connected(self):
        return self._connected

    def connect(self):
        """Connect via raw RFCOMM socket to D75 ch2 (SPP/CAT). No sudo needed."""
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
            sock.settimeout(8.0)
            sock.connect((self._mac, 2))   # channel 2 = SPP
            sock.settimeout(1.0)
            self._ser = sock
            self._connected = True
            print(f"[Serial] Connected to {self._mac} ch2 (raw RFCOMM)")
            self._init_radio()
            self._start_poll()
            return True
        except Exception as e:
            print(f"[Serial] Connect failed: {e}")
            self._cleanup()
            return False

    def disconnect(self):
        self._poll_stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        self._cleanup()
        print("[Serial] Disconnected")

    def send_raw(self, cmd):
        """Send a raw CAT command (no trailing \\r needed), return stripped response."""
        with self._lock:
            if not self._ser:
                return None
            try:
                self._ser.sendall((cmd.strip() + '\r').encode('ascii'))
                # Read until \r with a 2-second timeout
                buf = b''
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    try:
                        chunk = self._ser.recv(256)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if b'\r' in buf:
                        break
                return buf.decode('ascii', errors='ignore').strip() if buf else None
            except Exception as e:
                if VERBOSE:
                    print(f"[Serial] send_raw error: {e}")
                self._connected = False
                return None

    def to_dict(self):
        """Return status dict in the same shape as D75_CAT.py RadioState.to_dict()."""
        with self._state_lock:
            empty_band = {'frequency': '', 'mode': 0, 'squelch': 0,
                          'power': 'H', 's_meter': 0, 'freq_info': None,
                          'memory_mode': 0, 'channel': ''}
            b0 = dict(self.band[0]) if self.band[0] else dict(empty_band)
            b1 = dict(self.band[1]) if self.band[1] else dict(empty_band)
            return {
                'model_id':      self.model_id if self._connected else '',
                'fw_version':    self.fw_version,
                'serial_number': self.serial_number,
                'active_band':   self.active_band,
                'dual_band':     self.dual_band,
                'bluetooth':     self.bluetooth,
                'transmitting':  self.transmitting,
                'af_gain':       -1,
                'battery_level': -1,
                'band_0':        b0,
                'band_1':        b1,
            }

    # ── private ────────────────────────────────────────────────────────────────

    def _cleanup(self):
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
            self._connected = False

    def _init_radio(self):
        """Query model ID, firmware, serial number."""
        for _ in range(3):
            r = self.send_raw("ID")
            if r and r.startswith("ID"):
                with self._state_lock:
                    self.model_id = r[2:].strip().lstrip()
                break
            time.sleep(0.3)

        r = self.send_raw("FV")
        if r and r.startswith("FV"):
            with self._state_lock:
                self.fw_version = r[2:].strip().lstrip()

        r = self.send_raw("AE")
        if r and r.startswith("AE"):
            with self._state_lock:
                self.serial_number = r[2:].strip().lstrip()

        print(f"[Serial] Radio: model={self.model_id!r} fw={self.fw_version!r}")

    def _start_poll(self):
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="serial-poll")
        self._poll_thread.start()

    def _poll_loop(self):
        while not self._poll_stop.is_set() and self._connected:
            try:
                self._poll_state()
            except Exception:
                pass
            self._poll_stop.wait(2.0)

    def _poll_state(self):
        """Query frequency, S-meter, squelch, mode, power for both bands."""
        _parse_int_last = lambda s: int(s.split(',')[-1]) if s and ',' in s else 0

        for b in [0, 1]:
            fq = self.send_raw(f"FQ {b}")
            sm = self.send_raw(f"SM {b}")
            sq = self.send_raw(f"SQ {b}")
            md = self.send_raw(f"MD {b}")
            pc = self.send_raw(f"PC {b}")

            # FQ response: "FQ 0,0145500000" — 10-digit frequency in Hz
            freq = ''
            if fq and fq.startswith("FQ") and ',' in fq:
                try:
                    parts = fq.split(',')
                    raw = parts[-1].strip()         # "0145500000"
                    if len(raw) >= 9:
                        mhz_part  = str(int(raw[:4]))   # "145"
                        khz_part  = raw[4:7]            # "500"
                        hz_part   = raw[7:10]           # "000"
                        freq = f"{mhz_part}.{khz_part}{hz_part}"  # "145.500000"
                except Exception:
                    pass

            with self._state_lock:
                self.band[b] = {
                    'frequency':   freq,
                    'mode':        _parse_int_last(md) if md and md.startswith("MD") else 0,
                    'squelch':     _parse_int_last(sq) if sq and sq.startswith("SQ") else 0,
                    'power':       (pc.split(',')[-1].strip() if pc and ',' in pc else 'H'),
                    's_meter':     _parse_int_last(sm) if sm and sm.startswith("SM") else 0,
                    'freq_info':   None,
                    'memory_mode': 0,
                    'channel':     '',
                }

        dl = self.send_raw("DL")
        if dl and dl.startswith("DL") and ',' in dl:
            try:
                with self._state_lock:
                    self.dual_band = int(dl.split(',')[0].replace('DL', '').strip())
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO MANAGER  (RFCOMM channel 1 = HSP + SCO)
# ═══════════════════════════════════════════════════════════════════════════════

class AudioManager:
    """Manages BT HSP audio link: RFCOMM ch1 + SCO socket.

    Follows the same connect sequence as D75_CAT.py AudioManager:
      1. RFCOMM ch1 connect (HSP control channel)
      2. SCO connect with CVSD 16-bit voice parameter
      3. AT+CKPD=200 AFTER SCO connect to activate D75 audio routing
         (skipped when CAT serial is already open — cross-channel issue)
    """

    def __init__(self, mac):
        self._mac     = mac
        self._rfcomm  = None
        self._sco     = None
        self._running = False
        self._connected = False

        self._clients      = []
        self._clients_lock = threading.Lock()
        self._read_thread  = None

    @property
    def connected(self):
        return self._connected and self._sco is not None

    def connect(self, send_ckpd=True):
        """Connect HSP RFCOMM + SCO. send_ckpd=False when CAT serial is open."""
        try:
            # RFCOMM ch1 = Headset Audio Gateway
            print(f"[Audio] Connecting RFCOMM ch1 to {self._mac}...")
            self._rfcomm = socket.socket(socket.AF_BLUETOOTH,
                                         socket.SOCK_STREAM, BTPROTO_RFCOMM)
            self._rfcomm.settimeout(5.0)
            self._rfcomm.connect((self._mac, 1))
            print(f"[Audio] RFCOMM ch1 connected")
            time.sleep(0.3)

            # SCO socket — must be BEFORE CKPD
            print(f"[Audio] Connecting SCO...")
            self._sco = socket.socket(socket.AF_BLUETOOTH,
                                      socket.SOCK_SEQPACKET, BTPROTO_SCO)
            opt = struct.pack("H", BT_VOICE_CVSD_16BIT)
            self._sco.setsockopt(SOL_BLUETOOTH, BT_VOICE, opt)
            self._sco.settimeout(5.0)
            _sco_connect(self._sco, self._mac)
            print(f"[Audio] SCO connected")

            if send_ckpd:
                time.sleep(0.1)
                self._rfcomm.send(b'\r\nAT+CKPD=200\r\n')
                time.sleep(0.1)
                print(f"[Audio] CKPD sent")

            self._sco.settimeout(1.0)
            self._connected = True
            self._running   = True

            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="sco-read")
            self._read_thread.start()

            ckpd = " (+CKPD)" if send_ckpd else ""
            print(f"[Audio] Connected to {self._mac}{ckpd}")
            return True

        except Exception as e:
            import traceback
            print(f"[Audio] Connect failed at step above: {e}")
            traceback.print_exc()
            self._close_sockets()
            return False

    def disconnect(self):
        self._running = False
        if self._read_thread:
            self._read_thread.join(timeout=2)
        with self._clients_lock:
            for s in list(self._clients):
                try:
                    s.close()
                except Exception:
                    pass
            self._clients.clear()
        self._close_sockets()
        print("[Audio] Disconnected")

    def add_stream_client(self, sock):
        with self._clients_lock:
            self._clients.append(sock)

    def write_sco(self, data):
        """Write TX audio data to SCO (called from AudioServer TX reader)."""
        if self._sco and self._connected:
            try:
                self._sco.send(data)
            except Exception:
                pass

    # ── private ────────────────────────────────────────────────────────────────

    def _close_sockets(self):
        for s in (self._sco, self._rfcomm):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._sco     = None
        self._rfcomm  = None
        self._connected = False

    def _read_loop(self):
        """Read SCO frames and broadcast raw PCM to all TCP clients."""
        while self._running and self._sco:
            try:
                data = self._sco.recv(AUDIO_FRAME_SIZE * 8)
                if not data:
                    break
                with self._clients_lock:
                    dead = []
                    for client in self._clients:
                        try:
                            client.sendall(data)
                        except Exception:
                            dead.append(client)
                    for d in dead:
                        try:
                            d.close()
                        except Exception:
                            pass
                        self._clients.remove(d)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[Audio] SCO read error: {e}")
                break
        self._connected = False
        print("[Audio] SCO read loop ended")


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO TCP SERVER  (port 9751)
# ═══════════════════════════════════════════════════════════════════════════════

class AudioServer:
    """Streams raw 8 kHz 16-bit mono PCM to gateway on AUDIO_PORT.

    Clients that connect receive a continuous byte stream with no framing —
    identical to what D75_CAT.py's AudioServer produces.
    Any bytes received from the client are forwarded to the SCO link as TX audio.
    """

    def __init__(self, audio_mgr):
        self._audio   = audio_mgr
        self._sock    = None
        self._running = False

    def start(self, host=SERVER_HOST, port=AUDIO_PORT):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self._running = True
        threading.Thread(target=self._accept_loop,
                         daemon=True, name="audio-accept").start()
        print(f"[AudioTCP] Listening on :{port}")

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print(f"[AudioTCP] Client: {addr}")
                if not self._audio.connected:
                    try:
                        conn.sendall(b"ERROR: audio not connected\n")
                    except Exception:
                        pass
                    conn.close()
                    continue
                self._audio.add_stream_client(conn)
                threading.Thread(target=self._rx_loop, args=(conn, addr),
                                 daemon=True, name=f"audio-rx-{addr[1]}").start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[AudioTCP] Accept error: {e}")
                break

    def _rx_loop(self, conn, addr):
        """Read TX audio from gateway → SCO."""
        try:
            conn.settimeout(1.0)
            while self._running:
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    self._audio.write_sco(data)
                except socket.timeout:
                    continue
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
        except Exception:
            pass
        print(f"[AudioTCP] Client disconnected: {addr}")


# ═══════════════════════════════════════════════════════════════════════════════
# CAT TCP SERVER  (port 9750)
# ═══════════════════════════════════════════════════════════════════════════════

class CATServer:
    """Implements the D75_CAT.py text protocol subset used by D75CATClient.

    Protocol:
      Commands:  !<command> [args]\\n
      Responses: one text line per command\\n

    Commands implemented:
      !pass <pw>            — authenticate
      !exit                 — close connection
      !status               — return RadioState JSON (matches D75_CAT.py shape)
      !serial connect|disconnect|status
      !btstart              — connect serial + audio (serial first, audio no CKPD)
      !btstop               — disconnect serial + audio
      !ptt on|off|status    — PTT control via TX/RX CAT commands
      !cat <raw_cmd>        — pass raw CAT command to radio
      !audio connect|disconnect|status
      !freq <band> [freq]   — get/set frequency
    """

    def __init__(self, serial_mgr, audio_mgr):
        self._serial  = serial_mgr
        self._audio   = audio_mgr
        self._sock    = None
        self._running = False

    def start(self, host=SERVER_HOST, port=CAT_PORT):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind((host, port))
        self._sock.listen(10)
        self._running = True
        threading.Thread(target=self._accept_loop,
                         daemon=True, name="cat-accept").start()
        print(f"[CAT] Listening on :{port}")

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
                print(f"[CAT] Client: {addr}")
                threading.Thread(target=self._handle,
                                 args=(conn, addr),
                                 daemon=True, name=f"cat-{addr[1]}").start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[CAT] Accept error: {e}")
                break

    def _handle(self, conn, addr):
        logged_in = not PASSWORD
        buf = b''
        conn.settimeout(5.0)
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    nl = buf.index(b'\n')
                    line = buf[:nl].decode('utf-8', errors='ignore').strip()
                    buf  = buf[nl + 1:]
                    if not line:
                        continue
                    if not line.startswith('!'):
                        try:
                            conn.sendall(b"Error: commands must start with !\n")
                        except Exception:
                            return
                        continue
                    parts    = line[1:].split(None, 1)
                    cmd      = parts[0].lower() if parts else ''
                    data_arg = parts[1] if len(parts) > 1 else ''

                    if cmd == 'exit':
                        return
                    if not logged_in and cmd != 'pass':
                        try:
                            conn.sendall(b"Unauthorized\n")
                        except Exception:
                            return
                        continue

                    resp = self._process(cmd, data_arg)
                    try:
                        conn.sendall(f"{resp}\n".encode('utf-8'))
                    except Exception:
                        return
        except Exception as e:
            if VERBOSE:
                print(f"[CAT] Client {addr} error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            print(f"[CAT] Client disconnected: {addr}")

    def _process(self, cmd, data):
        # ── auth ──────────────────────────────────────────────────────────────
        if cmd == 'pass':
            if data == PASSWORD:
                return 'Login Successful'
            return 'Login Failed'

        # ── status ─────────────────────────────────────────────────────────────
        elif cmd == 'status':
            d = self._serial.to_dict()
            if self._audio:
                d['audio'] = {'connected': self._audio.connected}
            return json.dumps(d)

        # ── serial management ──────────────────────────────────────────────────
        elif cmd == 'serial':
            action = (data or '').strip().lower()
            if action == 'connect':
                if self._serial.connected:
                    return 'already connected'
                return 'connected' if self._serial.connect() else 'connect failed'
            elif action == 'disconnect':
                self._serial.disconnect()
                return 'disconnected'
            elif action == 'status':
                return 'connected' if self._serial.connected else 'disconnected'
            return 'usage: !serial connect|disconnect|status'

        # ── btstart / btstop ───────────────────────────────────────────────────
        elif cmd == 'btstart':
            ok = True
            if not self._serial.connected:
                ok = self._serial.connect()
            if ok and not self._audio.connected:
                # Audio without CKPD since CAT serial is now open
                self._audio.connect(send_ckpd=False)
            return 'started' if ok else 'btstart failed'

        elif cmd == 'btstop':
            self._audio.disconnect()
            self._serial.disconnect()
            return 'stopped'

        # ── PTT ────────────────────────────────────────────────────────────────
        elif cmd == 'ptt':
            action = (data or '').strip().lower()
            if action == 'on':
                r = self._serial.send_raw("TX")
                return r or 'TX'
            elif action == 'off':
                r = self._serial.send_raw("RX")
                return r or 'RX'
            elif action == 'status':
                return 'TX' if self._serial.transmitting else 'RX'
            return 'usage: !ptt on|off|status'

        # ── raw CAT passthrough ────────────────────────────────────────────────
        elif cmd == 'cat':
            if not self._serial.connected:
                return 'serial not connected'
            r = self._serial.send_raw(data)
            return r or 'no response'

        # ── audio management ───────────────────────────────────────────────────
        elif cmd == 'audio':
            action = (data or '').strip().lower()
            if action == 'connect':
                send_ckpd = not self._serial.connected
                return 'connected' if self._audio.connect(send_ckpd=send_ckpd) else 'connect failed'
            elif action == 'disconnect':
                self._audio.disconnect()
                return 'disconnected'
            elif action == 'status':
                return 'connected' if self._audio.connected else 'disconnected'
            return 'usage: !audio connect|disconnect|status'

        # ── frequency ──────────────────────────────────────────────────────────
        elif cmd == 'freq':
            if not self._serial.connected:
                return 'serial not connected'
            parts = (data or '').split()
            if len(parts) == 2:
                band, freq = parts[0], parts[1]
                fa = freq.split('.')
                freq_str = fa[0].rjust(4, '0') + (fa[1] if len(fa) > 1 else '').ljust(6, '0')
                r = self._serial.send_raw(f"FQ {band},{freq_str}")
                return r or 'ok'
            elif len(parts) == 1:
                r = self._serial.send_raw(f"FQ {parts[0]}")
                return r or 'no response'
            return 'usage: !freq <band> [mhz]'

        return f'unknown command: {cmd}'


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  Remote BT Proxy for TH-D75")
    print(f"  D75 MAC:     {D75_MAC}")
    print(f"  CAT port:    {CAT_PORT}")
    print(f"  Audio port:  {AUDIO_PORT}")
    print("=" * 60)

    if not ensure_paired(D75_MAC):
        print(f"\n[BT] WARNING: {D75_MAC} not paired — pairing before starting...")
        r = subprocess.run(['bluetoothctl', 'pair', D75_MAC],
                           timeout=20, capture_output=False)
        if r.returncode != 0:
            print(f"[BT] Pair failed. Run manually and retry:")
            print(f"     sudo bluetoothctl")
            print(f"     [bluetoothctl] pair {D75_MAC}")
            print(f"     [bluetoothctl] trust {D75_MAC}")

    serial_mgr   = SerialManager(D75_MAC)
    audio_mgr    = AudioManager(D75_MAC)
    cat_server   = CATServer(serial_mgr, audio_mgr)
    audio_server = AudioServer(audio_mgr)

    cat_server.start()
    audio_server.start()

    print(f"\nReady. Gateway config:")
    print(f"  ENABLE_D75     = true")
    print(f"  D75_HOST       = <this machine IP>")
    print(f"  D75_PORT       = {CAT_PORT}")
    print(f"  D75_AUDIO_PORT = {AUDIO_PORT}")
    print("\nPress Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Main] Stopping...")
        serial_mgr.disconnect()
        audio_mgr.disconnect()


if __name__ == '__main__':
    main()
