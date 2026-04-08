#!/usr/bin/env python3
"""CAT control clients for TH-9800 and D75."""

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

class RadioCATClient:
    """TCP client for TH-9800 CAT control via TH9800_CAT.py server.

    IMPORTANT — TH9800 radio protocol quirks (hard-won knowledge, do NOT change
    without reading this):

    1. PRESS RESPONSE IS UNRELIABLE: When you press a VFO dial, the radio sends
       back a CHANNEL_TEXT packet containing the OTHER VFO's channel, not the
       pressed VFO's channel.  DO NOT use the press response to read the current
       channel.  Instead, press the dial (activates for editing), then step right
       + step left (net zero movement) and read _channel_text[vfo] from the step
       response, which is always correct.

    2. STEP RESPONSE IS RELIABLE: After a dial step (right/left), _channel_text[vfo]
       always contains the stepped VFO's actual channel number.

    3. BACKGROUND DRAIN MUST BE PAUSED: set_channel() and send_web_command() must
       set _drain_paused = True for the duration of the operation.  The background
       drain thread will otherwise populate _channel_text concurrently, causing
       stale data to overwrite the response we're trying to read.

    4. _drain() MUST USE SINGLE _recv_line(): Using a loop (while self._buf:
       _recv_line()) breaks ALL packet parsing — state dicts end up empty.  The
       drain method reads raw socket data in a loop, then calls _recv_line() once
       to process buffered packets.

    5. NEVER PRESS V/M BUTTON: set_channel() must not attempt to detect or switch
       VFO/memory mode.  The V/M detection via _channel_vfo is unreliable
       (DISPLAY_CHANGE 0x03 packets always report the opposite VFO after a press).
       If the radio is in VFO mode, set_channel() returns False and the user must
       switch manually.

    6. RTS: Set once at startup to USB Controlled.  Do not use save/restore
       patterns (_with_usb_rts) as they can disrupt the serial connection.

    7. SOCKET CONTENTION: The background drain thread and command senders share
       one TCP socket.  Before sending any command, call _pause_drain() which
       sets _drain_paused AND waits for _drain_active to go False.  Just setting
       the flag is not enough — the drain thread may already be inside _drain()
       reading from the socket and will consume command responses.

    8. AUTH PER-CONNECTION: TH9800_CAT.py uses per-connection auth (conn_loggedin
       local variable).  If _send_cmd gets 'Unauthorized', it auto-re-auths and
       retries.  This handles cases where the server resets auth unexpectedly.
    """

    START_BYTES = b'\xAA\xFD'

    # 12-byte default payload template (button release / return control to body)
    DEFAULT_PAYLOAD = bytearray([0x84,0xFF,0xFF,0xFF,0xFF,0x81,0xFF,0xFF,0x82,0xFF,0xFF,0x00])

    # VFO identifiers
    LEFT = 'LEFT'
    RIGHT = 'RIGHT'

    def __init__(self, host, port, password='', verbose=False):
        self._host = host
        self._port = port
        self._password = password
        self._verbose = verbose
        self._sock = None
        self._buf = b''
        # Radio state parsed from forwarded packets
        self._channel = ''       # Latest channel text (3-char, e.g. "001")
        self._channel_vfo = ''   # VFO from last CHANNEL_TEXT (UNRELIABLE after press — see class docstring)
        self._vfo_text = {}      # {'LEFT': '...', 'RIGHT': '...'} display text per VFO
        self._channel_text = {}  # {'LEFT': '001', 'RIGHT': '002'} channel number per VFO
        self._power = {}         # {'LEFT': 'H', 'RIGHT': 'L'}
        self._volume = {}        # {'LEFT': 62, 'RIGHT': 0} last-set volume per VFO
        self._signal = {}        # {'LEFT': 0-9, 'RIGHT': 0-9} S-meter
        self._icons = {'LEFT': {}, 'RIGHT': {}, 'COMMON': {}}  # icon states
        self._drain_paused = False  # pause drain thread during command sequences
        self._drain_active = False  # True while drain thread is inside _drain()
        self._sock_lock = threading.Lock()  # serialize all socket reads (drain vs commands)
        self._last_activity = 0  # monotonic timestamp of last send/recv (for status bar)
        self._stop = False       # set True to abort loops (ctrl+c)
        self._log = None         # file handle for debug log
        self._rts_usb = None     # True = USB Controlled, False = Radio Controlled, None = unknown
        self._serial_connected = False  # Cached serial state (set by connect/disconnect handlers)
        self._cmd_sent = 0       # total commands sent
        self._cmd_no_response = 0  # commands with no radio response
        self._last_no_response = ''  # description of last no-response event
        self._last_radio_rx = 0  # monotonic time of last radio packet received

    def _logmsg(self, msg, console=False):
        """Write debug message to cat_debug.log. Only prints to console if verbose or console=True."""
        if console or self._verbose:
            print(msg)
        if self._log:
            self._log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            self._log.flush()

    def connect(self):
        """Connect to CAT TCP server and authenticate."""
        try:
            self._log = open('cat_debug.log', 'w')
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self._host, self._port))
            # Authenticate
            self._sock.sendall(f"!pass {self._password}\n".encode())
            resp = self._recv_line(timeout=5.0)
            if resp and 'Login Successful' in resp:
                return True
            else:
                print(f"  CAT auth failed: {resp}")
                self.close()
                return False
        except Exception as e:
            print(f"  CAT connect error: {e}")
            self.close()
            return False

    def close(self):
        """Gracefully close TCP connection to CAT server."""
        # Stop drain thread first so it doesn't race with socket close
        self._stop = True

        if self._sock:
            # Send !exit so the server closes its end cleanly
            try:
                self._sock.sendall(b'!exit\n')
            except Exception:
                pass
            # Brief pause to let the server process the exit command
            time.sleep(0.1)
            # Shut down socket (signals EOF to server even if !exit was lost)
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        # Give drain thread time to notice _stop and exit
        time.sleep(0.15)

        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

    def _send_cmd(self, cmd):
        """Send text command and return response line.
        Auto-re-authenticates if server returns 'Unauthorized'.
        Acquires _sock_lock to prevent drain thread from reading concurrently."""
        if not self._sock:
            return None
        with self._sock_lock:
            try:
                self._sock.sendall(f"{cmd}\n".encode())
                self._last_activity = time.monotonic()
                resp = self._recv_line(timeout=2.0)
                if resp and 'Unauthorized' in resp:
                    self._logmsg(f"  CAT: session lost auth, re-authenticating...", console=True)
                    self._sock.sendall(f"!pass {self._password}\n".encode())
                    auth_resp = self._recv_line(timeout=2.0)
                    if auth_resp and 'Login Successful' in auth_resp:
                        # Retry the original command
                        self._sock.sendall(f"{cmd}\n".encode())
                        self._last_activity = time.monotonic()
                        resp = self._recv_line(timeout=2.0)
                    else:
                        self._logmsg(f"  CAT: re-auth failed: {auth_resp}", console=True)
                return resp
            except Exception as e:
                self._logmsg(f"  CAT send error: {e}")
                return None

    def _with_usb_rts(self, func):
        """Run func() with RTS in USB Controlled mode, restore afterwards if changed."""
        rts_was = self._rts_usb
        if rts_was is not True:
            self.set_rts(True)
        try:
            return func()
        finally:
            if rts_was is not True and rts_was is not None:
                self.set_rts(rts_was)

    def _recv_line(self, timeout=2.0):
        """Read from socket until newline, with timeout. Parses binary radio packets inline."""
        if not self._sock:
            return None
        self._sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check for binary packet in buffer
            while self.START_BYTES in self._buf:
                idx = self._buf.index(self.START_BYTES)
                if idx > 0:
                    # Text data before binary — check for newline
                    text_part = self._buf[:idx]
                    if b'\n' in text_part:
                        line_end = text_part.index(b'\n')
                        line = self._buf[:line_end].decode('ascii', errors='replace').strip()
                        self._buf = self._buf[line_end+1:]
                        return line
                    self._buf = self._buf[idx:]
                # Need at least 3 bytes for start + length
                if len(self._buf) < 3:
                    break
                pkt_len = self._buf[2]
                total = 3 + pkt_len + 1  # start(2) + len(1) + payload + checksum(1)
                if len(self._buf) < total:
                    break
                pkt_data = self._buf[3:3+pkt_len]
                self._buf = self._buf[total:]
                # Skip any trailing newline the server appends
                if self._buf.startswith(b'\n'):
                    self._buf = self._buf[1:]
                self._parse_radio_packet(pkt_data)

            # Check for text line in buffer
            if b'\n' in self._buf:
                line_end = self._buf.index(b'\n')
                line = self._buf[:line_end].decode('ascii', errors='replace').strip()
                self._buf = self._buf[line_end+1:]
                return line

            # Read more data
            try:
                remaining = max(0.1, deadline - time.time())
                self._sock.settimeout(remaining)
                data = self._sock.recv(1024)
                if not data:
                    return None
                self._buf += data
                self._last_activity = time.monotonic()
            except socket.timeout:
                break
            except Exception:
                return None
        return None

    def _drain(self, duration=0.3):
        """Drain any pending data from socket, parsing packets along the way.

        IMPORTANT: Must end with a SINGLE _recv_line() call, NOT a loop.
        Using 'while self._buf: _recv_line()' breaks all packet parsing and
        leaves state dicts (_channel_text, _power, etc.) empty.  See bugs.md.
        Acquires _sock_lock to prevent concurrent reads with _send_cmd."""
        if not self._sock:
            return
        with self._sock_lock:
            end = time.time() + duration
            self._sock.settimeout(0.05)
            while time.time() < end:
                try:
                    data = self._sock.recv(4096)
                    if not data:
                        break
                    self._buf += data
                except socket.timeout:
                    pass
                except Exception:
                    break
            # Process any buffered packets
            self._recv_line(timeout=0.1)

    def set_rts(self, usb_controlled):
        """Set RTS state. True = USB Controlled, False = Radio Controlled."""
        resp = self._send_cmd(f"!rts {usb_controlled}")
        # Response format: "CMD{rts[True]} True" — check for 'true' anywhere
        if resp:
            self._rts_usb = 'true' in resp.lower()
        else:
            self._rts_usb = usb_controlled
        self._logmsg(f"  CAT RTS set to {'USB' if self._rts_usb else 'Radio'} Controlled: {resp}")
        return resp

    def query_rts(self):
        """Query current RTS state from TH9800 (toggle then toggle back would be destructive,
        so just return cached state)."""
        return self._rts_usb

    def get_rts(self):
        """Return last known RTS state. True = USB Controlled, False = Radio Controlled."""
        return self._rts_usb

    def get_serial_status(self):
        """Return cached serial connection state (set by connect/disconnect commands).
        Does NOT poll TH9800_CAT — polling over TCP steals radio packets and
        causes lock contention that stalls button commands."""
        return self._serial_connected

    def get_radio_state(self):
        """Return full radio state dict for web dashboard."""
        return {
            'connected': self._sock is not None,
            'serial_connected': self._serial_connected,
            'rts_usb': self._rts_usb,
            'volume': {
                'left': self._volume.get(self.LEFT, -1),
                'right': self._volume.get(self.RIGHT, -1),
            },
            'left': {
                'display': self._vfo_text.get(self.LEFT, ''),
                'channel': self._channel_text.get(self.LEFT, ''),
                'power': self._power.get(self.LEFT, ''),
                'signal': self._signal.get(self.LEFT, 0),
                'icons': dict(self._icons.get(self.LEFT, {})),
            },
            'right': {
                'display': self._vfo_text.get(self.RIGHT, ''),
                'channel': self._channel_text.get(self.RIGHT, ''),
                'power': self._power.get(self.RIGHT, ''),
                'signal': self._signal.get(self.RIGHT, 0),
                'icons': dict(self._icons.get(self.RIGHT, {})),
            },
            'common': dict(self._icons.get('COMMON', {})),
        }

    # Command lookup table for web buttons — maps label to (cmd_bytes, start, end)
    # Mirrors TH9800_Enums.RADIO_TX_CMD
    WEB_COMMANDS = {
        # Left VFO buttons
        'L_LOW': ([0x00, 0x21], 3, 5), 'L_LOW_HOLD': ([0x01, 0x21], 3, 5),
        'L_VM': ([0x00, 0x22], 3, 5), 'L_VM_HOLD': ([0x01, 0x22], 3, 5),
        'L_HM': ([0x00, 0x23], 3, 5), 'L_HM_HOLD': ([0x01, 0x23], 3, 5),
        'L_SCN': ([0x00, 0x24], 3, 5), 'L_SCN_HOLD': ([0x01, 0x24], 3, 5),
        'L_DIAL_LEFT': ([0x01], 2, 3), 'L_DIAL_RIGHT': ([0x02], 2, 3),
        'L_DIAL_PRESS': ([0x00, 0x25], 3, 5), 'L_DIAL_HOLD': ([0x01, 0x25], 3, 5),
        'L_SET_VFO': ([0x23, 0x24], 3, 5),
        # Right VFO buttons
        'R_LOW': ([0x00, 0xA1], 3, 5), 'R_LOW_HOLD': ([0x01, 0xA1], 3, 5),
        'R_VM': ([0x00, 0xA2], 3, 5), 'R_VM_HOLD': ([0x01, 0xA2], 3, 5),
        'R_HM': ([0x00, 0xA3], 3, 5), 'R_HM_HOLD': ([0x01, 0xA3], 3, 5),
        'R_SCN': ([0x00, 0xA4], 3, 5), 'R_SCN_HOLD': ([0x01, 0xA4], 3, 5),
        'R_DIAL_LEFT': ([0x81], 2, 3), 'R_DIAL_RIGHT': ([0x82], 2, 3),
        'R_DIAL_PRESS': ([0x00, 0xA5], 3, 5), 'R_DIAL_HOLD': ([0x01, 0xA5], 3, 5),
        'R_SET_VFO': ([0x24, 0x23], 3, 5),
        # Menu / SET
        'N_SET': ([0x00, 0x20], 3, 5), 'N_SET_HOLD': ([0x01, 0x20], 3, 5),
        # Mic keypad
        'MIC_0': ([0x00, 0x00], 3, 5), 'MIC_1': ([0x00, 0x01], 3, 5),
        'MIC_2': ([0x00, 0x02], 3, 5), 'MIC_3': ([0x00, 0x03], 3, 5),
        'MIC_4': ([0x00, 0x04], 3, 5), 'MIC_5': ([0x00, 0x05], 3, 5),
        'MIC_6': ([0x00, 0x06], 3, 5), 'MIC_7': ([0x00, 0x07], 3, 5),
        'MIC_8': ([0x00, 0x08], 3, 5), 'MIC_9': ([0x00, 0x09], 3, 5),
        'MIC_A': ([0x00, 0x0A], 3, 5), 'MIC_B': ([0x00, 0x0B], 3, 5),
        'MIC_C': ([0x00, 0x0C], 3, 5), 'MIC_D': ([0x00, 0x0D], 3, 5),
        'MIC_STAR': ([0x00, 0x0E], 3, 5), 'MIC_POUND': ([0x00, 0x0F], 3, 5),
        'MIC_P1': ([0x00, 0x10], 3, 5), 'MIC_P2': ([0x00, 0x11], 3, 5),
        'MIC_P3': ([0x00, 0x12], 3, 5), 'MIC_P4': ([0x00, 0x13], 3, 5),
        'MIC_UP': ([0x00, 0x14], 3, 5), 'MIC_DOWN': ([0x00, 0x15], 3, 5),
        'MIC_PTT': ([0x00], 1, 2),
        # Hyper memories
        'HYPER_A': ([0x00, 0x27], 3, 5), 'HYPER_B': ([0x00, 0x28], 3, 5),
        'HYPER_C': ([0x00, 0x29], 3, 5), 'HYPER_D': ([0x00, 0xAA], 3, 5),
        'HYPER_E': ([0x00, 0xAB], 3, 5), 'HYPER_F': ([0x00, 0xAC], 3, 5),
        # Single VFO (L_VOLUME_HOLD)
        'L_VOLUME_HOLD': ([0x00, 0x26], 3, 5),
    }

    def send_web_command(self, cmd_name):
        """Send a named button command from web UI.
        Returns True on success, False on no-response, or string error message."""
        if cmd_name == 'DEFAULT':
            resp = self._send_button_release()
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            return True
        if cmd_name == 'TOGGLE_RTS':
            self._pause_drain()
            try:
                resp = self._send_cmd("!rts")
                if resp and 'serial not connected' in resp:
                    return 'serial not connected'
                if resp:
                    self._rts_usb = 'true' in resp.lower()
            finally:
                self._drain_paused = False
            return True
        if cmd_name == 'MIC_PTT':
            self._pause_drain()
            try:
                resp = self._send_cmd("!ptt")
                if resp and 'serial not connected' in resp:
                    return 'serial not connected'
            finally:
                self._drain_paused = False
            return True
        entry = self.WEB_COMMANDS.get(cmd_name)
        if not entry:
            return False
        cmd_bytes, start, end = entry
        is_dial = cmd_name in ('L_DIAL_RIGHT', 'L_DIAL_LEFT', 'R_DIAL_RIGHT', 'R_DIAL_LEFT',
                               'L_DIAL_PRESS', 'R_DIAL_PRESS')
        pre_channel = dict(self._channel_text) if is_dial else None
        self._cmd_sent += 1
        self._pause_drain()
        try:
            resp1 = self._send_button(cmd_bytes, start, end)
            # Check if serial is not connected before continuing
            if resp1 and 'serial not connected' in resp1:
                return 'serial not connected'
            time.sleep(0.15)
            resp2 = self._send_button_release()
            time.sleep(0.15)
            # Read response while drain is still paused — if we unpause first,
            # the drain thread races us for the radio's binary response packets
            pre_buf = len(self._buf)
            self._drain(0.3)
            post_buf = len(self._buf)
        finally:
            self._drain_paused = False
        # For dial commands, verify radio responded
        if is_dial:
            post_channel = dict(self._channel_text)
            if post_channel == pre_channel:
                self._cmd_no_response += 1
                self._last_no_response = f"web {cmd_name} @ {time.strftime('%H:%M:%S')}"
                self._logmsg(f"    Web {cmd_name}: no response (sent={self._cmd_sent} missed={self._cmd_no_response}) "
                             f"resp1={resp1!r} resp2={resp2!r} buf={pre_buf}->{post_buf} ch={pre_channel}", console=True)
                return False
            else:
                # Reset consecutive failure counter on success
                self._cmd_no_response = 0
        return True

    def reconnect(self):
        """Close and reopen the TCP connection to CAT server."""
        self._stop = True
        self.close()
        time.sleep(0.5)
        self._stop = False
        self._buf = b''
        if self.connect():
            self.start_background_drain()
            return True
        return False

    def serial_reconnect(self):
        """Disconnect and reconnect the radio serial via CAT server.
        Returns True if reconnect succeeded."""
        self._logmsg("  CAT: Auto-recovering serial (disconnect/reconnect)...", console=True)
        self._pause_drain()
        try:
            with self._sock_lock:
                self._sock.sendall(b"!serial disconnect\n")
                resp = self._recv_line(timeout=3.0)
            self._logmsg(f"  CAT: Serial disconnect: {resp}", console=True)
            time.sleep(1.0)
            with self._sock_lock:
                self._sock.sendall(b"!serial connect\n")
                # connect takes ~3s (startup sequence + sleeps)
                resp = self._recv_line(timeout=10.0)
            if resp and 'connected' in resp:
                self._logmsg("  CAT: Serial reconnected successfully", console=True)
                self._cmd_no_response = 0
                return True
            else:
                self._logmsg(f"  CAT: Serial reconnect failed: {resp}", console=True)
                return False
        except Exception as e:
            self._logmsg(f"  CAT: Serial reconnect error: {e}", console=True)
            return False
        finally:
            self._drain_paused = False

    def _pause_drain(self):
        """Pause background drain and wait for it to actually stop reading."""
        self._drain_paused = True
        # Wait for drain thread to exit _drain() (up to 1s)
        for _ in range(20):
            if not self._drain_active:
                break
            time.sleep(0.05)

    def start_background_drain(self):
        """Start background thread that continuously reads radio packets for live state updates."""
        def _drain_loop():
            _was_paused = False
            while self._sock and not self._stop:
                if self._drain_paused:
                    self._drain_active = False
                    _was_paused = True
                    time.sleep(0.05)
                    continue
                # After resuming from pause (e.g. RTS switch), reset the radio
                # activity timestamp so software PTT doesn't think radio is offline
                if _was_paused and self._last_radio_rx > 0:
                    self._last_radio_rx = time.monotonic()
                    _was_paused = False
                try:
                    self._drain_active = True
                    self._drain(0.5)
                    self._drain_active = False
                except Exception:
                    self._drain_active = False
                time.sleep(0.1)
        t = threading.Thread(target=_drain_loop, daemon=True, name='cat-drain')
        t.start()

    def send_web_volume(self, vfo, level):
        """Set volume from web UI. vfo='LEFT'/'RIGHT', level=0-100.
        Returns response string or 'serial not connected'."""
        level = max(0, min(100, int(level)))
        vfo_letter = 'LEFT' if vfo == self.LEFT else 'RIGHT'
        self._pause_drain()
        try:
            resp = self._send_cmd(f"!vol {vfo_letter} {level}")
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            self._volume[vfo] = level
            return resp
        finally:
            self._drain_paused = False

    def send_web_squelch(self, vfo, level):
        """Set squelch from web UI via raw packet. vfo='LEFT'/'RIGHT', level=0-100.
        Returns 'serial not connected' on failure."""
        level = max(0, min(100, int(level)))
        if vfo == self.LEFT:
            cmd_bytes = [0x02, 0xEB, level & 0xFF]
        else:
            cmd_bytes = [0x82, 0xEB, level & 0xFF]
        self._pause_drain()
        try:
            resp = self._send_button(cmd_bytes, 8, 11)
            if resp and 'serial not connected' in resp:
                return 'serial not connected'
            return resp
        finally:
            self._drain_paused = False

    def _parse_radio_packet(self, data):
        """Parse forwarded binary radio packets to update internal state."""
        self._last_radio_rx = time.monotonic()
        if len(data) < 2:
            return
        pkt_type = data[0]  # First byte is packet type
        vfo_byte = data[1]  # Second byte is VFO indicator

        if pkt_type == 0x03:  # DISPLAY_CHANGE
            if vfo_byte == 0x43:
                self._channel_vfo = self.LEFT
            elif vfo_byte == 0xC3:
                self._channel_vfo = self.RIGHT
            self._logmsg(f"    [pkt] DISPLAY_CHANGE vfo=0x{vfo_byte:02X} -> {self._channel_vfo}", console=False)

        elif pkt_type == 0x02:  # CHANNEL_TEXT
            # NOTE: vfo_byte mapping is correct for STEP responses but misleading
            # for PRESS responses (press returns the OTHER VFO's channel).
            # Do NOT use press response to determine the pressed VFO's channel.
            # See class docstring and set_channel() for the full explanation.
            if vfo_byte in (0x40, 0x60):
                self._channel_vfo = self.LEFT
            elif vfo_byte in (0xC0, 0xE0):
                self._channel_vfo = self.RIGHT
            if len(data) >= 6:
                try:
                    ch = data[3:6].decode('ascii', errors='replace').strip()
                    if ch:  # Don't blank channel with empty radio packet
                        self._channel = ch
                        self._channel_text[self._channel_vfo] = self._channel
                    self._logmsg(f"    [pkt] CHANNEL_TEXT vfo_byte=0x{vfo_byte:02X} -> {self._channel_vfo} ch='{ch}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x01:  # DISPLAY_TEXT
            if len(data) >= 9:
                try:
                    text = data[3:9].decode('ascii', errors='replace').strip()
                    # Determine VFO from vfo_byte (same mapping as DISPLAY_ICONS)
                    # Fall back to _channel_vfo only if vfo_byte is unknown
                    if vfo_byte in (0x40, 0x60):
                        dt_vfo = self.LEFT
                    elif vfo_byte in (0xC0, 0xE0):
                        dt_vfo = self.RIGHT
                    else:
                        dt_vfo = self._channel_vfo
                    # Don't overwrite with empty text (radio sends blank packets during refresh)
                    if dt_vfo and text:
                        self._vfo_text[dt_vfo] = text
                    self._logmsg(f"    [pkt] DISPLAY_TEXT vfo_byte=0x{vfo_byte:02X} vfo={dt_vfo} text='{text}'", console=False)
                except Exception:
                    pass

        elif pkt_type == 0x04:  # DISPLAY_ICONS (full icon state)
            if vfo_byte == 0x40:
                vfo = self.LEFT
            elif vfo_byte == 0xC0:
                vfo = self.RIGHT
            else:
                self._logmsg(f"    [pkt] DISPLAY_ICONS unknown vfo=0x{vfo_byte:02X}", console=False)
                return
            if len(data) >= 8:
                # Parse all icon bytes
                icons = self._icons[vfo]
                # Index 2: APO, LOCK, KEY2, SET
                if len(data) > 3:
                    b = data[3]
                    self._icons['COMMON']['APO'] = bool(b & 0x02)
                    self._icons['COMMON']['LOCK'] = bool(b & 0x08)
                    self._icons['COMMON']['KEY2'] = bool(b & 0x20)
                    self._icons['COMMON']['SET'] = bool(b & 0x80)
                # Index 3: NEG, POS, TX, MAIN
                if len(data) > 4:
                    b = data[4]
                    icons['NEG'] = bool(b & 0x02)
                    icons['POS'] = bool(b & 0x08)
                    icons['TX'] = bool(b & 0x20)
                    icons['MAIN'] = bool(b & 0x80)
                # Index 4: PREF, SKIP, ENC, DEC
                if len(data) > 5:
                    b = data[5]
                    icons['PREF'] = bool(b & 0x02)
                    icons['SKIP'] = bool(b & 0x08)
                    icons['ENC'] = bool(b & 0x20)
                    icons['DEC'] = bool(b & 0xA0 == 0xA0)
                # Index 5: DCS, MUTE, MT, BUSY
                if len(data) > 6:
                    b = data[6]
                    icons['DCS'] = bool(b & 0x02)
                    icons['MUTE'] = bool(b & 0x08)
                    icons['MT'] = bool(b & 0x20)
                    icons['BUSY'] = bool(b & 0x80)
                # Index 6: power (L/M/H), AM
                if len(data) > 7:
                    b = data[7]
                    icons['AM'] = bool(b & 0x80)
                    if b & 0x08:
                        self._power[vfo] = 'L'
                    elif b & 0x02:
                        self._power[vfo] = 'M'
                    else:
                        self._power[vfo] = 'H'
                self._logmsg(f"    [pkt] DISPLAY_ICONS vfo={vfo} power={self._power.get(vfo,'')} icons={icons}", console=False)

        elif pkt_type == 0x1D:  # ICON_SIG_BARS
            sig_val = vfo_byte
            if sig_val >= 0x80:
                vfo = self.RIGHT
                sig_val -= 0x80
            else:
                vfo = self.LEFT
            self._signal[vfo] = min(sig_val, 9)
            self._logmsg(f"    [pkt] SIG_BARS vfo={vfo} S{self._signal[vfo]}", console=False)

        elif 0x10 <= pkt_type <= 0x27:  # Individual icon commands
            # Determine VFO from vfo_byte
            if vfo_byte >= 0x80:
                vfo = self.RIGHT
            else:
                vfo = self.LEFT
            icon_on = bool(vfo_byte & 0x01) if pkt_type not in (0x1D,) else True
            icon_names = {
                0x10: 'SET', 0x11: 'KEY2', 0x12: 'LOCK', 0x13: 'APO',
                0x14: 'MAIN', 0x15: 'TX', 0x16: 'POS', 0x17: 'NEG',
                0x18: 'ENCDEC', 0x19: 'ENC', 0x1A: 'SKIP', 0x1B: 'PREF',
                0x1C: 'BUSY', 0x1E: 'MT', 0x1F: 'MUTE', 0x20: 'DCS',
                0x21: 'AM', 0x23: 'PWR_LOW', 0x24: 'PWR_MED',
            }
            name = icon_names.get(pkt_type)
            if name:
                target = 'COMMON' if name in ('SET', 'KEY2', 'LOCK', 'APO') else vfo
                self._icons[target][name] = icon_on
                # Update power from individual icon commands
                if name == 'PWR_LOW' and icon_on:
                    self._power[vfo] = 'L'
                elif name == 'PWR_MED' and icon_on:
                    self._power[vfo] = 'M'
                elif name in ('PWR_LOW', 'PWR_MED') and not icon_on:
                    # If both off, it's high
                    if not self._icons.get(vfo, {}).get('PWR_LOW') and not self._icons.get(vfo, {}).get('PWR_MED'):
                        self._power[vfo] = 'H'
            self._logmsg(f"    [pkt] ICON 0x{pkt_type:02X} vfo={vfo} on={icon_on} name={name}", console=False)
        else:
            self._logmsg(f"    [pkt] type=0x{pkt_type:02X} vfo=0x{vfo_byte:02X} data={data.hex()}", console=False)

    def _build_packet(self, payload):
        """Build full AA FD <len> <payload> <checksum> packet, return hex string."""
        length = len(payload)
        checksum = length
        for b in payload:
            checksum ^= b
        pkt = bytearray([0xAA, 0xFD, length]) + bytearray(payload) + bytearray([checksum & 0xFF])
        return pkt.hex()

    def _build_button_payload(self, cmd_bytes, start, end):
        """Insert cmd_bytes into DEFAULT_PAYLOAD at positions start:end."""
        payload = bytearray(self.DEFAULT_PAYLOAD)
        payload[start:end] = bytearray(cmd_bytes)
        return payload

    def _send_button(self, cmd_bytes, start, end):
        """Build button payload, send as !data command."""
        payload = self._build_button_payload(cmd_bytes, start, end)
        hex_str = self._build_packet(payload)
        return self._send_cmd(f"!data {hex_str}")

    def _send_button_release(self):
        """Send button release (DEFAULT_PAYLOAD)."""
        hex_str = self._build_packet(self.DEFAULT_PAYLOAD)
        return self._send_cmd(f"!data {hex_str}")

    def _send_button_checked(self, cmd_bytes, start, end, label='button'):
        """Send button press + release and verify radio responded.
        Returns True if _channel_text changed, False if no response detected."""
        pre_state = dict(self._channel_text)
        self._cmd_sent += 1
        self._send_button(cmd_bytes, start, end)
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)
        post_state = dict(self._channel_text)
        if post_state == pre_state:
            self._cmd_no_response += 1
            self._last_no_response = f"{label} @ {time.strftime('%H:%M:%S')}"
            self._logmsg(f"    {label}: no radio response (sent={self._cmd_sent} missed={self._cmd_no_response})", console=True)
            return False
        return True

    def _channel_matches(self, target_int):
        """Compare current channel to target as integers, tolerant of padding/spaces."""
        try:
            return int(self._channel) == target_int
        except (ValueError, TypeError):
            return False

    def set_channel(self, vfo, target_channel):
        """Set channel on specified VFO by stepping the dial. Returns True on success.

        The press response is UNRELIABLE (returns the other VFO's channel).
        To read the current channel, we press then step-right + step-left (net
        zero) and read _channel_text[vfo] from the step response, which is
        always correct.  Background drain is paused for the entire operation.
        Never presses V/M — returns False if radio is in VFO mode."""
        target_int = int(target_channel)
        other_vfo = self.RIGHT if vfo == self.LEFT else self.LEFT
        self._logmsg(f"  CAT: Setting {vfo} channel to {target_int}...")

        # Pause background drain so it doesn't race with our reads
        self._pause_drain()
        try:
            return self._set_channel_inner(vfo, target_int, other_vfo)
        finally:
            self._drain_paused = False

    def _set_channel_inner(self, vfo, target_int, other_vfo):
        """Inner channel-setting logic (called with drain paused).

        Press response is unreliable — it returns the OTHER VFO's channel, not
        the pressed VFO's.  To read the current channel reliably, we press the
        dial (activates it for editing), then step right + step left (net zero
        movement) and read from the step response which is always correct."""

        # Press the VFO dial to activate it for editing
        self._drain()
        self._channel_text.clear()
        if vfo == self.LEFT:
            self._send_button([0x00, 0x25], 3, 5)  # L_DIAL_PRESS
        else:
            self._send_button([0x00, 0xA5], 3, 5)  # R_DIAL_PRESS
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)

        # Step right then left (net zero) to read current channel from step response
        step_r = [0x02] if vfo == self.LEFT else [0x82]  # DIAL_RIGHT
        step_l = [0x01] if vfo == self.LEFT else [0x81]  # DIAL_LEFT
        for step_cmd in (step_r, step_l):
            self._channel_text.pop(vfo, None)
            self._send_button(step_cmd, 2, 3)
            time.sleep(0.05)
            self._send_button_release()
            time.sleep(0.15)
            self._drain(0.3)

        # After step-left, _channel_text[vfo] = current channel (back to original)
        ch = self._channel_text.get(vfo, '').strip()
        self._logmsg(f"    {vfo} current: ch='{ch}'", console=True)

        if not ch or not ch.isdigit():
            self._logmsg(f"    {vfo}: no channel data, skipping (VFO mode or radio unresponsive)")
            return False

        if int(ch) == target_int:
            self._logmsg(f"    {vfo} already on channel {target_int}", console=True)
            return True

        start_channel = ch

        # Step through channels
        no_response_count = 0
        for i in range(200):
            if self._stop:
                self._logmsg(f"    Aborted")
                return False

            # Save channel text before step to detect if radio responded
            pre_step = self._channel_text.get(vfo, '').strip()

            if vfo == self.LEFT:
                self._send_button([0x02], 2, 3)  # L_DIAL_RIGHT
            else:
                self._send_button([0x82], 2, 3)  # R_DIAL_RIGHT
            time.sleep(0.05)
            self._send_button_release()
            time.sleep(0.15)
            self._drain(0.3)

            # Step response maps to _channel_text[vfo] (correct — opposite of press)
            ch = self._channel_text.get(vfo, '').strip()
            self._logmsg(f"    {vfo} step {i+1}: ch='{ch}'")

            # Detect no response — channel should always change on a step
            if ch == pre_step:
                no_response_count += 1
                if no_response_count <= 3:
                    self._logmsg(f"    Step {i+1}: no response (ch unchanged '{ch}'), retrying...")
                    time.sleep(0.3)
                    self._drain(0.3)
                    continue
                else:
                    self._logmsg(f"    Radio unresponsive after {no_response_count} retries, aborting")
                    return False
            else:
                no_response_count = 0  # reset on successful response

            if ch.isdigit() and int(ch) == target_int:
                self._logmsg(f"    Channel set to {target_int} (stepped {i+1})")
                return True
            if start_channel and ch == start_channel and i > 0:
                self._logmsg(f"    Channel {target_int} not found (looped around after {i+1} steps)")
                return False

        self._logmsg(f"    Channel {target_int} not found (max iterations)")
        return False

    def set_volume(self, vfo, target_level):
        """Set volume on specified VFO. level=0-100.
        Sends a minor nudge first to wake the radio's volume control,
        then sets the actual target value."""
        target_level = max(0, min(100, target_level))
        vfo_letter = 'LEFT' if vfo == self.LEFT else 'RIGHT'
        self._logmsg(f"  CAT: Setting {vfo} volume to {target_level}%...")

        # Nudge: set volume slightly off-target to force the radio to
        # actually process a volume change (avoids stale/assumed values)
        nudge = target_level + 1 if target_level < 100 else target_level - 1
        resp = self._send_cmd(f"!vol {vfo_letter} {nudge}")
        self._logmsg(f"    Volume nudge: {nudge}", console=False)
        time.sleep(0.1)

        # Now set the real target
        resp = self._send_cmd(f"!vol {vfo_letter} {target_level}")
        self._logmsg(f"    Volume set to {target_level}%")
        time.sleep(0.05)

        self._volume[vfo] = target_level
        return True

    def set_power(self, vfo, target):
        """Set power level on specified VFO. target='L','M','H'. Returns True on success."""
        target = target.upper()
        if target not in ('L', 'M', 'H'):
            self._logmsg(f"    Invalid power level: {target}")
            return False
        self._logmsg(f"  CAT: Setting {vfo} power to {target}...")

        self._drain()

        # Trigger display refresh with dial press
        if vfo == self.LEFT:
            self._send_button([0x00, 0x25], 3, 5)
        else:
            self._send_button([0x00, 0xA5], 3, 5)
        time.sleep(0.15)
        self._send_button_release()
        time.sleep(0.3)
        self._drain(0.5)

        current = self._power.get(vfo, '')
        self._logmsg(f"    Current power: {current}", console=False)
        if current == target:
            self._logmsg(f"    Already at power {target}")
            return True

        start_power = current

        # Cycle power: L→M→H→L (press LOW button)
        for i in range(4):
            if self._stop:
                self._logmsg(f"    Aborted")
                return False
            if vfo == self.LEFT:
                self._send_button([0x00, 0x21], 3, 5)  # L_LOW
            else:
                self._send_button([0x00, 0xA1], 3, 5)  # R_LOW
            time.sleep(0.15)
            self._send_button_release()
            time.sleep(0.3)
            self._drain(0.5)

            current = self._power.get(vfo, '')
            self._logmsg(f"    Power cycle {i+1}: power='{current}'", console=False)
            if current == target:
                self._logmsg(f"    Power set to {target} (cycled {i+1})")
                return True
            if start_power and current == start_power and i > 0:
                self._logmsg(f"    Power {target} not reached (looped back)")
                return False

        self._logmsg(f"    Power {target} not reached (max iterations)")
        return False

    def setup_radio(self, config):
        """Run full radio setup sequence from config. Prints concise summary."""
        left_ch = getattr(config, 'CAT_LEFT_CHANNEL', -1)
        right_ch = getattr(config, 'CAT_RIGHT_CHANNEL', -1)
        left_vol = getattr(config, 'CAT_LEFT_VOLUME', -1)
        right_vol = getattr(config, 'CAT_RIGHT_VOLUME', -1)
        left_pwr = str(getattr(config, 'CAT_LEFT_POWER', '')).strip()
        right_pwr = str(getattr(config, 'CAT_RIGHT_POWER', '')).strip()

        # Set RTS to USB controlled once (no restore — simpler, avoids serial disruption)
        print("  CAT: Setting RTS to USB Controlled...")
        self.set_rts(True)
        time.sleep(0.5)

        # Build list of tasks to run (default args capture values, not references)
        tasks = []
        if int(left_ch) != -1:
            tasks.append(('L ch', lambda c=int(left_ch): self.set_channel(self.LEFT, c)))
        if int(right_ch) != -1:
            tasks.append(('R ch', lambda c=int(right_ch): self.set_channel(self.RIGHT, c)))
        if int(left_vol) != -1:
            tasks.append(('L vol', lambda v=int(left_vol): self.set_volume(self.LEFT, v)))
        if int(right_vol) != -1:
            tasks.append(('R vol', lambda v=int(right_vol): self.set_volume(self.RIGHT, v)))
        if left_pwr:
            tasks.append(('L pwr', lambda p=left_pwr: self.set_power(self.LEFT, p)))
        if right_pwr:
            tasks.append(('R pwr', lambda p=right_pwr: self.set_power(self.RIGHT, p)))

        if not tasks:
            print("  CAT: No setup tasks configured")
            return

        print(f"  CAT: Sending {len(tasks)} setup commands...")
        results = []
        for name, func in tasks:
            if self._stop:
                results.append((name, 'interrupted'))
                break
            try:
                ok = func()
                results.append((name, 'ok' if ok else 'failed'))
            except Exception as e:
                results.append((name, f'error: {e}'))
                print(f"  CAT: {name} error: {e}")

        # Send final button release, re-confirm RTS, and settle — rapid setup
        # commands can leave the radio serial in a state where subsequent commands
        # are ignored until RTS is reasserted
        self._send_button_release()
        time.sleep(0.3)
        self.set_rts(True)
        time.sleep(0.3)
        self._drain(0.5)

        # Print concise summary
        ok_count = sum(1 for _, r in results if r == 'ok')
        summary_parts = [f"{name}={status}" for name, status in results]
        if ok_count == len(results):
            print(f"  CAT: Setup complete ({ok_count}/{len(tasks)} ok)")
        else:
            print(f"  CAT: Setup done ({ok_count}/{len(tasks)} ok) — {', '.join(summary_parts)}")


# D75CATClient removed — D75 now uses link endpoint
