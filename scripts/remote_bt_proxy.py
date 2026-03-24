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
import queue as _queue_mod
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

    Architecture:
      - A dedicated read thread drains the RFCOMM socket into _rx_queue.
      - send_raw() holds _send_lock (one command at a time), drains any
        pending streaming messages, sends the command, then waits for a
        matching response from the queue.
      - A poll/stream thread drains _rx_queue when no command is in flight,
        processing streaming messages (FQ, SM, TX/RX, etc.).
      - AI 1 is sent after init to enable real-time push updates so the
        gateway sees live frequency, S-meter and TX state.
    """

    def __init__(self, mac):
        self._mac        = mac
        self._ser        = None
        self._connected  = False
        self._send_lock  = threading.Lock()
        self._rx_queue   = _queue_mod.Queue()
        self._state_lock = threading.Lock()

        # Cached radio state
        self.model_id      = ''
        self.fw_version    = ''
        self.serial_number = ''
        self.band          = [{}, {}]
        self.active_band   = 0
        self.dual_band     = 0
        self.bluetooth     = False
        self.transmitting  = False

        self._stop_evt    = threading.Event()
        self._read_thread = None
        self._poll_thread = None

    # ── public ─────────────────────────────────────────────────────────────────

    @property
    def connected(self):
        return self._connected

    def connect(self):
        """Connect via raw RFCOMM socket to D75 ch2 (SPP/CAT)."""
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
            sock.settimeout(8.0)
            sock.connect((self._mac, 2))
            sock.settimeout(1.0)
            self._ser = sock
            self._connected = True
            self._stop_evt.clear()
            # Read thread must start before any send_raw() calls
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="serial-read")
            self._read_thread.start()
            print(f"[Serial] Connected to {self._mac} ch2 (raw RFCOMM)")
            self._init_radio()
            # Enable real-time push updates (FQ, SM, TX/RX stream to us)
            self.send_raw("AI 0")   # disable first (D75_CAT.py pattern)
            time.sleep(0.2)
            r = self.send_raw("AI 1")
            print(f"[Serial] AI 1 response: {r!r}")
            self._poll_thread = threading.Thread(
                target=self._stream_loop, daemon=True, name="serial-stream")
            self._poll_thread.start()
            return True
        except Exception as e:
            print(f"[Serial] Connect failed: {e}")
            self._cleanup()
            return False

    def disconnect(self):
        self._stop_evt.set()
        for t in (self._poll_thread, self._read_thread):
            if t:
                t.join(timeout=3)
        self._cleanup()
        print("[Serial] Disconnected")

    def send_raw(self, cmd):
        """Send a CAT command and return the matching response line.

        Drains any pending streaming messages first (updating state from each),
        then waits for a response whose first token matches the command code.
        """
        cmd_code = cmd.strip().split()[0].upper()
        with self._send_lock:
            if not self._ser or not self._connected:
                return None
            # Drain stale streaming messages before sending
            while True:
                try:
                    self._process_message(self._rx_queue.get_nowait())
                except _queue_mod.Empty:
                    break
            # Send command
            try:
                self._ser.sendall((cmd.strip() + '\r').encode('ascii'))
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[Serial] Send error: {e}")
                self._connected = False
                return None
            # Wait for matching response
            deadline = time.time() + 2.0
            while time.time() < deadline:
                remaining = max(0.05, deadline - time.time())
                try:
                    line = self._rx_queue.get(timeout=remaining)
                    if line.upper().startswith(cmd_code):
                        return line
                    self._process_message(line)  # streaming message arrived first
                except _queue_mod.Empty:
                    break
            return None

    def to_dict(self):
        with self._state_lock:
            empty = {'frequency': '', 'mode': 0, 'squelch': 0,
                     'power': 0, 's_meter': 0, 'freq_info': None,
                     'memory_mode': 0, 'channel': ''}
            b0 = dict(self.band[0]) if self.band[0] else dict(empty)
            b1 = dict(self.band[1]) if self.band[1] else dict(empty)
            d = {
                'serial_connected': self._connected,
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
            print(f"[D75-TRACE] to_dict: serial_connected={self._connected} model_id={self.model_id!r}")
            return d

    # ── private ────────────────────────────────────────────────────────────────

    def _cleanup(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._connected = False

    def _init_radio(self):
        """Query model ID, firmware, serial number on connect."""
        for _ in range(3):
            r = self.send_raw("ID")
            if r and r.startswith("ID"):
                with self._state_lock:
                    self.model_id = r[2:].strip()
                break
            time.sleep(0.3)
        r = self.send_raw("FV")
        if r and r.startswith("FV"):
            with self._state_lock:
                self.fw_version = r[2:].strip()
        r = self.send_raw("AE")
        if r and r.startswith("AE"):
            with self._state_lock:
                self.serial_number = r[2:].strip()
        print(f"[Serial] Radio: model={self.model_id!r} fw={self.fw_version!r}")
        # Query initial frequency, s-meter, and power for both bands
        for band in (0, 1):
            r = self.send_raw(f"SM {band}")
            if r:
                self._process_message(r)
            r = self.send_raw(f"FO {band}")
            if r:
                self._process_message(r)
            else:
                print(f"[Serial] WARNING: FO {band} got no response — frequency will be blank until VFO moves")
            r = self.send_raw(f"PC {band}")
            if r:
                self._process_message(r)
        for cmd in ("DL", "BC"):
            r = self.send_raw(cmd)
            if r:
                self._process_message(r)
        with self._state_lock:
            for b in (0, 1):
                bd = self.band[b]
                print(f"[Serial] Band {'A' if b == 0 else 'B'}: freq={bd.get('frequency','?')} sm={bd.get('s_meter','?')}")

    def _read_loop(self):
        """Read RFCOMM bytes, split on \\r, put lines in _rx_queue."""
        buf = b''
        while not self._stop_evt.is_set() and self._connected:
            try:
                chunk = self._ser.recv(256)
                if not chunk:
                    self._connected = False
                    break
                buf += chunk
                while b'\r' in buf:
                    idx = buf.index(b'\r')
                    line = buf[:idx].decode('ascii', errors='ignore').strip()
                    buf  = buf[idx + 1:]
                    if line:
                        self._rx_queue.put(line)
            except socket.timeout:
                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                self._connected = False
                break
        print("[Serial] Read loop ended")

    def _stream_loop(self):
        """Drain _rx_queue between send_raw() calls; polls S-meter every 2s.

        AI 1 streaming pushes FQ/TX/RX/MD/DL but NOT SM — S-meter must be
        polled explicitly.
        """
        _last_sm_poll = 0.0
        _last_state_dump = 0.0
        SM_POLL_INTERVAL = 0.5
        STATE_DUMP_INTERVAL = 30.0
        while not self._stop_evt.is_set() and self._connected:
            # Drain queue (shorter timeout so SM poll fires promptly)
            try:
                line = self._rx_queue.get(timeout=0.5)
                # Only process if send_lock is free (otherwise send_raw handles it)
                if self._send_lock.acquire(blocking=False):
                    try:
                        self._process_message(line)
                    finally:
                        self._send_lock.release()
                else:
                    # send_raw is active — put it back so send_raw can pick it up
                    self._rx_queue.put(line)
                    time.sleep(0.05)
            except _queue_mod.Empty:
                pass
            # Periodic S-meter poll (AI 1 does NOT push SM — must query explicitly)
            now = time.time()
            if now - _last_sm_poll >= SM_POLL_INTERVAL and self._connected:
                _last_sm_poll = now
                r = self.send_raw("SM 0")
                if r:
                    self._process_message(r)
                else:
                    print("[Serial] SM 0 poll: no response (timeout)")
                r = self.send_raw("SM 1")
                if r:
                    self._process_message(r)
                else:
                    print("[Serial] SM 1 poll: no response (timeout)")
            # Periodic state dump for diagnosis
            if now - _last_state_dump >= STATE_DUMP_INTERVAL:
                _last_state_dump = now
                with self._state_lock:
                    print(f"[Serial] State dump — connected={self._connected} "
                          f"model={self.model_id!r} band0={self.band[0]} band1={self.band[1]}")

    def _process_message(self, line):
        """Update cached state from a streaming CAT message."""
        if not line:
            return
        if VERBOSE or line.startswith('FQ') or line.startswith('BY'):
            print(f"[Serial] << {line!r}")
        try:
            if line.startswith('FQ') and ',' in line:
                # FQ band,XXXXXXXXXX  (10-digit freq in Hz)
                parts = line.split(',')
                band  = int(line[2:].split(',')[0].strip())
                raw   = parts[-1].strip()
                if len(raw) >= 9:
                    freq = f"{int(raw[:4])}.{raw[4:7]}{raw[7:10]}"
                    with self._state_lock:
                        if 0 <= band <= 1:
                            self.band[band]['frequency'] = freq
            elif line.startswith('SM') and ',' in line:
                parts = line.split(',')
                band  = int(line[2:].split(',')[0].strip())
                level = int(parts[-1].strip())
                with self._state_lock:
                    if 0 <= band <= 1:
                        self.band[band]['s_meter'] = level
            elif line.startswith('MD') and ',' in line:
                parts = line.split(',')
                band  = int(line[2:].split(',')[0].strip())
                mode  = int(parts[-1].strip())
                with self._state_lock:
                    if 0 <= band <= 1:
                        self.band[band]['mode'] = mode
            elif line.startswith('BY') and ',' in line:
                # BY = SquelchOpen (busy indicator). BY band,0 = squelch closed → zero s_meter.
                # BY band,1 = squelch opened → SM poll at 0.5s will pick up the value.
                parts = line.split(',')
                band  = int(line[2:].split(',')[0].strip())
                sq_open = int(parts[-1].strip())
                if sq_open == 0:
                    with self._state_lock:
                        if 0 <= band <= 1:
                            self.band[band]['s_meter'] = 0
            elif line.startswith('FO') and ',' in line:
                # FO = FrequencyInfo — gives current frequency on init and after FQ push
                parts = line.split(',')
                try:
                    band = int(line[2:].split(',')[0].strip())
                    raw  = parts[1].strip()    # 10-digit frequency in Hz
                    if len(raw) >= 7:
                        freq = f"{int(raw[:4])}.{raw[4:7]}"
                        with self._state_lock:
                            if 0 <= band <= 1:
                                self.band[band]['frequency'] = freq
                except (ValueError, IndexError):
                    pass
            elif line.startswith('TX'):
                with self._state_lock:
                    self.transmitting = True
            elif line.startswith('RX'):
                with self._state_lock:
                    self.transmitting = False
            elif line.startswith('DL '):
                with self._state_lock:
                    self.dual_band = int(line.split()[-1].strip())
            elif line.startswith('BC '):
                with self._state_lock:
                    self.active_band = int(line.split()[-1].strip())
            elif line.startswith('PC') and ',' in line:
                parts = line.split(',')
                band = int(line[2:].split(',')[0].strip())
                pwr  = int(parts[-1].strip())
                with self._state_lock:
                    if 0 <= band <= 1:
                        self.band[band]['power'] = pwr
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
            self._rfcomm.settimeout(15.0)
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
            print(f"[Audio] Connect failed: {e}")
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

    def send_ckpd(self):
        """Send AT+CKPD=200 to activate D75 audio routing (call after serial is disconnected)."""
        if self._rfcomm:
            try:
                self._rfcomm.send(b'\r\nAT+CKPD=200\r\n')
                time.sleep(0.1)
                print("[Audio] CKPD sent")
            except Exception as e:
                print(f"[Audio] CKPD failed: {e}")

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
        self._btstart_thread = None  # tracks background btstart

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
            # Run BT connect in background so this command returns immediately.
            # Caller polls !status or !audio status to wait for completion.
            if self._btstart_thread and self._btstart_thread.is_alive():
                return 'btstart already in progress'
            def _do_btstart():
                # CKPD cannot be sent while CAT serial (RFCOMM ch2) is open.
                # Strategy: connect audio hardware (RFCOMM ch1 + SCO) WITHOUT CKPD first
                # so that if BT is unreachable we never touched serial.
                # Only disconnect serial briefly once we know BT is actually up.
                if not self._audio.connected:
                    print("[btstart] Connecting audio hardware (no CKPD yet)...")
                    audio_ok = self._audio.connect(send_ckpd=False)
                    if not audio_ok:
                        print("[btstart] Audio failed — serial untouched")
                        return
                    serial_was_up = self._serial.connected
                    if serial_was_up:
                        print("[btstart] Dropping serial briefly for CKPD...")
                        self._serial.disconnect()
                        time.sleep(0.5)
                    self._audio.send_ckpd()
                    time.sleep(0.3)
                    if serial_was_up:
                        print("[btstart] Reconnecting serial...")
                        self._serial.connect()
                else:
                    if not self._serial.connected:
                        self._serial.connect()
                print("[btstart] Done")
            self._btstart_thread = threading.Thread(target=_do_btstart, daemon=True, name="btstart")
            self._btstart_thread.start()
            return 'btstart initiated'

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
            if r:
                self._serial._process_message(r)  # Update state from set-command ACKs (e.g. PC, FQ)
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
