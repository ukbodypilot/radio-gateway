#!/usr/bin/env python3
"""
Gateway Link — protocol, server, and client for remote radio endpoints.

This module is fully self-contained: ZERO imports from other gateway modules.
The endpoint script can import it standalone on a remote machine.

Frame format: [1 byte type][2 byte big-endian length][payload]

Dependencies: stdlib only (+ pyaudio inside AudioPlugin.setup only)
"""

import socket
import struct
import json
import threading
import time
import logging

log = logging.getLogger("GatewayLink")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class GatewayLinkProtocol:
    """Wire protocol for Gateway Link: framed messages over TCP."""

    AUDIO    = 0x01
    COMMAND  = 0x02
    STATUS   = 0x03
    REGISTER = 0x04
    ACK      = 0x05

    _HEADER = struct.Struct('>BH')  # type (1) + length (2) = 3 bytes

    @staticmethod
    def _recv_exact(sock, n):
        """Read exactly *n* bytes from *sock*.  Returns bytes or None on disconnect."""
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except (OSError, ConnectionError):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    @classmethod
    def send_frame(cls, sock, frame_type, payload):
        """Send a single framed message.  *payload* must be bytes."""
        header = cls._HEADER.pack(frame_type, len(payload))
        sock.sendall(header + payload)

    @classmethod
    def recv_frame(cls, sock):
        """Receive a single framed message.

        Returns ``(frame_type, payload)`` or ``None`` on disconnect.
        """
        raw = cls._recv_exact(sock, cls._HEADER.size)
        if raw is None:
            return None
        frame_type, length = cls._HEADER.unpack(raw)
        if length == 0:
            return (frame_type, b'')
        payload = cls._recv_exact(sock, length)
        if payload is None:
            return None
        return (frame_type, payload)

    # -- convenience senders ------------------------------------------------

    @classmethod
    def send_audio(cls, sock, pcm):
        """Send raw PCM audio bytes."""
        cls.send_frame(sock, cls.AUDIO, pcm)

    @classmethod
    def send_command(cls, sock, cmd_dict):
        """Send a JSON command dict."""
        cls.send_frame(sock, cls.COMMAND, json.dumps(cmd_dict).encode('utf-8'))

    @classmethod
    def send_status(cls, sock, status_dict):
        """Send a JSON status dict."""
        cls.send_frame(sock, cls.STATUS, json.dumps(status_dict).encode('utf-8'))

    @classmethod
    def send_register(cls, sock, info_dict):
        """Send a registration (endpoint → server) dict."""
        cls.send_frame(sock, cls.REGISTER, json.dumps(info_dict).encode('utf-8'))

    @classmethod
    def send_ack(cls, sock, cmd_id, result_dict):
        """Send an ACK for *cmd_id* with a result dict."""
        payload = {"cmd_id": cmd_id}
        payload.update(result_dict)
        cls.send_frame(sock, cls.ACK, json.dumps(payload).encode('utf-8'))


# ---------------------------------------------------------------------------
# Server (master gateway side)
# ---------------------------------------------------------------------------

class GatewayLinkServer:
    """Listens for a single endpoint connection and exchanges framed messages.

    Callbacks (all optional, called from reader thread):
        on_audio(pcm_bytes)
        on_command(cmd_dict)
        on_register(info_dict)
        on_disconnect()
    """

    def __init__(self, port=9700, on_audio=None, on_command=None,
                 on_register=None, on_disconnect=None, on_ack=None):
        self._port = port
        self._on_audio = on_audio
        self._on_command = on_command
        self._on_register = on_register
        self._on_disconnect = on_disconnect
        self._on_ack = on_ack

        self._server_sock = None
        self._client_sock = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._endpoint_info = None
        self._endpoint_capabilities = {}
        self._start_time = time.monotonic()
        self._last_endpoint_heartbeat = 0  # monotonic time of last received heartbeat
        self._DEAD_PEER_TIMEOUT = 15.0     # seconds without heartbeat before declaring dead

        self._accept_thread = None
        self._reader_thread = None
        self._heartbeat_thread = None

    # -- public API ---------------------------------------------------------

    def start(self):
        """Bind, listen, and start accept + heartbeat threads."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(('', self._port))
        self._server_sock.listen(1)
        print(f"  [Link] Server listening on port {self._port}")

        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="LinkAccept", daemon=True)
        self._accept_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="LinkHeartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        """Shut down server, close all connections."""
        self._stop.set()
        self._close_client()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self._accept_thread:
            self._accept_thread.join(timeout=3)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=3)
        print("  [Link] Server stopped")

    def send_audio(self, pcm):
        """Send PCM audio to connected endpoint (thread-safe)."""
        self._send(GatewayLinkProtocol.AUDIO, pcm)

    def send_command(self, cmd):
        """Send a command dict to the endpoint."""
        self._send(GatewayLinkProtocol.COMMAND,
                   json.dumps(cmd).encode('utf-8'))

    def send_status(self, status):
        """Send a status dict to the endpoint."""
        self._send(GatewayLinkProtocol.STATUS,
                   json.dumps(status).encode('utf-8'))

    @property
    def connected(self):
        return self._client_sock is not None

    @property
    def endpoint_info(self):
        """Registration dict from the connected endpoint, or None."""
        return self._endpoint_info

    @property
    def endpoint_capabilities(self):
        """Capabilities dict from the connected endpoint, or empty dict."""
        return self._endpoint_capabilities

    # -- internal -----------------------------------------------------------

    def _send(self, frame_type, payload):
        """Thread-safe send to the current client socket."""
        with self._send_lock:
            sock = self._client_sock
            if sock is None:
                return
            try:
                GatewayLinkProtocol.send_frame(sock, frame_type, payload)
            except (OSError, ConnectionError):
                # Don't close on send error — the reader thread handles disconnect.
                # Closing here races with accept thread swapping in a new socket.
                pass

    def _close_client(self):
        """Close the current client connection."""
        with self._send_lock:
            sock = self._client_sock
            self._client_sock = None
            self._endpoint_info = None
            self._endpoint_capabilities = {}
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        """Accept thread: wait for incoming connections."""
        while not self._stop.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue

            print(f"  [Link] Endpoint connected from {addr[0]}:{addr[1]}")
            # Close any previous connection
            self._close_client()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._send_lock:
                self._client_sock = conn

            self._reader_thread = threading.Thread(
                target=self._reader_loop, args=(conn,),
                name="LinkReader", daemon=True)
            self._reader_thread.start()

    def _reader_loop(self, sock):
        """Read frames from the connected endpoint until disconnect."""
        P = GatewayLinkProtocol
        try:
            while not self._stop.is_set():
                result = P.recv_frame(sock)
                if result is None:
                    break
                ftype, payload = result
                try:
                    if ftype == P.AUDIO:
                        if self._on_audio:
                            self._on_audio(payload)
                    elif ftype == P.COMMAND:
                        if self._on_command:
                            self._on_command(json.loads(payload))
                    elif ftype == P.REGISTER:
                        info = json.loads(payload)
                        self._endpoint_info = info
                        self._last_endpoint_heartbeat = time.monotonic()
                        caps = info.get('capabilities', {})
                        if isinstance(caps, dict):
                            self._endpoint_capabilities = caps
                        else:
                            self._endpoint_capabilities = {}
                        enabled = [k for k, v in self._endpoint_capabilities.items() if v]
                        print(f"  [Link] Endpoint registered: {info.get('name', '?')} "
                              f"plugin={info.get('plugin', '?')} "
                              f"v={info.get('version', '?')} "
                              f"caps={enabled}")
                        if self._on_register:
                            self._on_register(info)
                    elif ftype == P.ACK:
                        ack = json.loads(payload)
                        cmd_name = ack.get('cmd', ack.get('cmd_id', '?'))
                        ok = ack.get('ok', False)
                        print(f"  [Link] ACK received: cmd={cmd_name} ok={ok}")
                        if self._on_ack:
                            try:
                                self._on_ack(ack)
                            except Exception as e:
                                print(f"  [Link] ACK callback error: {e}")
                    elif ftype == P.STATUS:
                        # Track endpoint heartbeat for dead-peer detection
                        self._last_endpoint_heartbeat = time.monotonic()
                except json.JSONDecodeError as e:
                    print(f"  [Link] Bad JSON from endpoint: {e}")
                except Exception as e:
                    print(f"  [Link] Callback error: {e}")
        except Exception as e:
            if not self._stop.is_set():
                print(f"  [Link] Reader error: {e}")
        finally:
            # Only close our own socket — _client_sock may already point to a
            # newer connection if accept_loop replaced us.
            with self._send_lock:
                if self._client_sock is sock:
                    self._client_sock = None
                    self._endpoint_info = None
                    self._endpoint_capabilities = {}
            try:
                sock.close()
            except OSError:
                pass
            print("  [Link] Endpoint disconnected")
            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception:
                    pass

    def _heartbeat_loop(self):
        """Send heartbeat every 5s and check for dead peer (no heartbeat in 15s)."""
        while not self._stop.is_set():
            self._stop.wait(5.0)
            if self._stop.is_set():
                break
            if self._client_sock is not None:
                uptime = time.monotonic() - self._start_time
                self.send_status({"type": "heartbeat", "uptime": round(uptime, 1)})
                # Dead peer detection
                if self._last_endpoint_heartbeat > 0:
                    silence = time.monotonic() - self._last_endpoint_heartbeat
                    if silence > self._DEAD_PEER_TIMEOUT:
                        print(f"  [Link] Dead peer detected ({silence:.0f}s without heartbeat) — closing")
                        self._close_client()
                        self._last_endpoint_heartbeat = 0


# ---------------------------------------------------------------------------
# Client (endpoint side)
# ---------------------------------------------------------------------------

class GatewayLinkClient:
    """Connects to a GatewayLinkServer and exchanges framed messages.

    Automatically reconnects on disconnect (5 s backoff).

    Callbacks (all optional, called from reader thread):
        on_audio(pcm_bytes)
        on_command(cmd_dict)
        on_status(status_dict)
    """

    def __init__(self, host, port, name, capabilities, plugin_name="audio",
                 on_audio=None, on_command=None, on_status=None):
        self._host = host
        self._port = port
        self._name = name
        self._capabilities = capabilities
        self._plugin_name = plugin_name

        self._on_audio = on_audio
        self._on_command = on_command
        self._on_status = on_status

        self._sock = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._connect_thread = None
        self._reader_thread = None

    # -- public API ---------------------------------------------------------

    def start(self):
        """Start background connect thread (with auto-reconnect)."""
        self._stop.clear()
        self._connect_thread = threading.Thread(target=self._connect_loop,
                                                name="LinkConnect", daemon=True)
        self._connect_thread.start()

    def stop(self):
        """Shut down client and close connection."""
        self._stop.set()
        self._close()
        if self._connect_thread:
            self._connect_thread.join(timeout=8)
        print("  [Link] Client stopped")

    def send_audio(self, pcm):
        """Send PCM audio to the server (thread-safe)."""
        self._send(GatewayLinkProtocol.AUDIO, pcm)

    def send_command(self, cmd):
        """Send a command dict to the server."""
        self._send(GatewayLinkProtocol.COMMAND,
                   json.dumps(cmd).encode('utf-8'))

    def send_status(self, status):
        """Send a status dict to the server."""
        self._send(GatewayLinkProtocol.STATUS,
                   json.dumps(status).encode('utf-8'))

    def send_ack(self, cmd_name, result_dict):
        """Send an ACK frame back to the server with command result."""
        payload = {"cmd": cmd_name}
        if isinstance(result_dict, dict):
            payload["ok"] = result_dict.get("ok", False)
            payload["result"] = result_dict
        else:
            payload["ok"] = False
            payload["result"] = {}
        self._send(GatewayLinkProtocol.ACK,
                   json.dumps(payload).encode('utf-8'))

    @property
    def connected(self):
        return self._sock is not None

    # -- internal -----------------------------------------------------------

    def _send(self, frame_type, payload):
        """Thread-safe send to the server socket."""
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return
            try:
                GatewayLinkProtocol.send_frame(sock, frame_type, payload)
            except (OSError, ConnectionError) as e:
                print(f"  [Link] Client send error: {e}")
                self._close()

    def _close(self):
        """Close the connection."""
        with self._send_lock:
            sock = self._sock
            self._sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_loop(self):
        """Connect to the server, auto-reconnect on failure."""
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((self._host, self._port))
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, ConnectionError) as e:
                print(f"  [Link] Connect to {self._host}:{self._port} failed: {e}")
                sock.close()
                if self._stop.wait(5.0):
                    break
                continue

            print(f"  [Link] Connected to {self._host}:{self._port}")
            with self._send_lock:
                self._sock = sock

            # Send registration
            try:
                GatewayLinkProtocol.send_register(sock, {
                    "name": self._name,
                    "plugin": self._plugin_name,
                    "capabilities": self._capabilities,
                    "version": "1.0",
                })
            except (OSError, ConnectionError) as e:
                print(f"  [Link] Registration send failed: {e}")
                self._close()
                if self._stop.wait(5.0):
                    break
                continue

            # Start client heartbeat thread
            hb_stop = threading.Event()
            def _client_heartbeat():
                while not hb_stop.is_set():
                    hb_stop.wait(5.0)
                    if hb_stop.is_set():
                        break
                    self.send_status({"type": "heartbeat"})
            hb_thread = threading.Thread(target=_client_heartbeat,
                                         name="LinkClientHB", daemon=True)
            hb_thread.start()

            # Run reader until disconnect
            self._reader_loop(sock)
            hb_stop.set()

            # Disconnected — retry
            if not self._stop.is_set():
                print(f"  [Link] Reconnecting in 5s...")
                if self._stop.wait(5.0):
                    break

    def _reader_loop(self, sock):
        """Read frames from the server until disconnect."""
        P = GatewayLinkProtocol
        _frame_count = 0
        try:
            while not self._stop.is_set():
                result = P.recv_frame(sock)
                if result is None:
                    print(f"  [Link] recv_frame returned None after {_frame_count} frames")
                    break
                _frame_count += 1
                ftype, payload = result
                try:
                    if ftype == P.AUDIO:
                        if self._on_audio:
                            self._on_audio(payload)
                    elif ftype == P.COMMAND:
                        if self._on_command:
                            self._on_command(json.loads(payload))
                    elif ftype == P.STATUS:
                        if self._on_status:
                            self._on_status(json.loads(payload))
                    elif ftype == P.ACK:
                        ack = json.loads(payload)
                        print(f"  [Link] ACK from server: cmd_id={ack.get('cmd_id')}")
                    elif ftype == P.REGISTER:
                        # Server shouldn't send REGISTER, but handle gracefully
                        pass
                except json.JSONDecodeError as e:
                    print(f"  [Link] Bad JSON from server: {e}")
                except Exception as e:
                    print(f"  [Link] Client callback error: {e}")
        except Exception as e:
            if not self._stop.is_set():
                print(f"  [Link] Client reader error: {e}")
        finally:
            print("  [Link] Disconnected from server")
            self._close()


# ---------------------------------------------------------------------------
# RadioPlugin base class
# ---------------------------------------------------------------------------

class RadioPlugin:
    """Base class for link endpoint hardware plugins.

    Subclass this to add support for specific radio hardware.
    The endpoint loads a plugin by name and calls its methods.
    """

    name = "base"
    capabilities = {
        "audio_rx": False,
        "audio_tx": False,
        "ptt": False,
        "frequency": False,
        "ctcss": False,
        "power": False,
        "volume": False,
        "smeter": False,
        "status": True,  # all plugins support status
    }

    def setup(self, config):
        """Initialize hardware.  *config* is a dict from command-line args."""
        pass

    def teardown(self):
        """Clean shutdown of hardware."""
        pass

    def get_audio(self):
        """Read one chunk of PCM audio from hardware.

        Returns bytes (48 kHz 16-bit signed LE mono, 4800 bytes = 50 ms)
        or ``None`` if no data is available.
        """
        return None

    def put_audio(self, pcm):
        """Write PCM audio to hardware for playback / transmission."""
        pass

    def execute(self, cmd):
        """Handle a command from the master gateway.

        *cmd* is a dict like ``{"cmd": "ptt", "state": true}``.
        Returns a result dict.
        """
        action = cmd.get('cmd', '') if isinstance(cmd, dict) else ''
        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": "not implemented"}

    def get_status(self):
        """Return current hardware state as a dict."""
        return {"plugin": self.name}


# ---------------------------------------------------------------------------
# AudioPlugin — generic sound-card plugin
# ---------------------------------------------------------------------------

class AudioPlugin(RadioPlugin):
    """Generic audio device plugin — streams from any ALSA / PipeWire sound card.

    Uses PyAudio (portaudio).  ``pyaudio`` is imported lazily inside
    :meth:`setup` so this module has no hard dependency on it.
    """

    name = "audio"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": False,
        "frequency": False,
        "ctcss": False,
        "power": False,
        "volume": True,
        "smeter": False,
        "status": True,
    }

    RATE = 48000
    CHANNELS = 1
    FORMAT_WIDTH = 2          # 16-bit = 2 bytes
    CHUNK_BYTES = 4800        # 50 ms at 48 kHz mono 16-bit
    CHUNK_FRAMES = CHUNK_BYTES // FORMAT_WIDTH  # 2400 frames

    def __init__(self):
        super().__init__()
        self._pa = None
        self._in_stream = None
        self._out_stream = None
        self._device_name = ""
        self._output_volume = 1.0  # 0.0-1.0 gain multiplier for output

    def setup(self, config):
        """Open PyAudio input + output streams.

        *config* keys:
            device (str)   — device name substring or index (default '')
            rate (int)     — sample rate (default 48000)
            channels (int) — channel count (default 1)
        """
        import pyaudio

        self._pa = pyaudio.PyAudio()
        self._device_name = config.get('device', '')
        rate = int(config.get('rate', self.RATE))
        channels = int(config.get('channels', self.CHANNELS))
        fmt = pyaudio.paInt16

        dev_index = self._find_device(self._device_name)
        kwargs = {}
        if dev_index is not None:
            kwargs['input_device_index'] = dev_index
            kwargs['output_device_index'] = dev_index

        try:
            self._in_stream = self._pa.open(
                format=fmt, channels=channels, rate=rate,
                input=True, frames_per_buffer=self.CHUNK_FRAMES,
                **{k: v for k, v in kwargs.items() if 'input' in k})
            print(f"  [Link] AudioPlugin: input stream opened"
                  f" (device={self._device_name or 'default'}, rate={rate})")
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to open input stream: {e}")
            self._in_stream = None

        try:
            self._out_stream = self._pa.open(
                format=fmt, channels=channels, rate=rate,
                output=True, frames_per_buffer=self.CHUNK_FRAMES,
                **{k: v for k, v in kwargs.items() if 'output' in k})
            print(f"  [Link] AudioPlugin: output stream opened"
                  f" (device={self._device_name or 'default'}, rate={rate})")
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to open output stream: {e}")
            self._out_stream = None

    def teardown(self):
        """Close PyAudio streams and terminate."""
        for stream in (self._in_stream, self._out_stream):
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self._in_stream = None
        self._out_stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        print("  [Link] AudioPlugin: teardown complete")

    def get_audio(self):
        """Read one 50 ms chunk from the input stream."""
        if not self._in_stream:
            return None
        try:
            data = self._in_stream.read(self.CHUNK_FRAMES, exception_on_overflow=False)
            return data
        except Exception as e:
            print(f"  [Link] AudioPlugin: read error: {e}")
            return None

    def put_audio(self, pcm):
        """Write PCM audio to the output stream, applying output volume."""
        if not self._out_stream:
            return
        try:
            if self._output_volume != 1.0:
                pcm = self._apply_volume(pcm, self._output_volume)
            self._out_stream.write(pcm)
        except Exception as e:
            print(f"  [Link] AudioPlugin: write error: {e}")

    def execute(self, cmd):
        """Handle commands from master gateway."""
        action = cmd.get('cmd', '') if isinstance(cmd, dict) else ''
        if action == 'volume':
            level = cmd.get('level', 100)
            level = max(0, min(100, int(level)))
            self._output_volume = level / 100.0
            print(f"  [Link] AudioPlugin: volume set to {level}%")
            return {"ok": True, "volume": level}
        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        return {
            "plugin": self.name,
            "device": self._device_name or "default",
            "rate": self.RATE,
            "input_active": self._in_stream is not None,
            "output_active": self._out_stream is not None,
            "volume": round(self._output_volume * 100),
        }

    @staticmethod
    def _apply_volume(pcm, gain):
        """Apply a gain multiplier to 16-bit signed LE PCM audio."""
        import struct as _struct
        n_samples = len(pcm) // 2
        samples = _struct.unpack(f'<{n_samples}h', pcm)
        gained = []
        for s in samples:
            v = int(s * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            gained.append(v)
        return _struct.pack(f'<{n_samples}h', *gained)

    # -- helpers ------------------------------------------------------------

    def _find_device(self, name):
        """Find a PyAudio device index by name substring.

        Returns the index (int) or ``None`` to use the default device.
        """
        if not name:
            return None
        # Try as integer index first
        try:
            idx = int(name)
            return idx
        except ValueError:
            pass
        # Search by name substring (case-insensitive)
        if not self._pa:
            return None
        name_lower = name.lower()
        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
                if name_lower in info.get('name', '').lower():
                    print(f"  [Link] AudioPlugin: matched device {i}: {info['name']}")
                    return i
            except Exception:
                continue
        print(f"  [Link] AudioPlugin: device '{name}' not found, using default")
        return None


# ---------------------------------------------------------------------------
# AIOCPlugin — AIOC USB (All-In-One-Cable) with GPIO PTT
# ---------------------------------------------------------------------------

class AIOCPlugin(AudioPlugin):
    """AIOC USB device plugin — sound card audio + HID GPIO PTT.

    The AIOC is a USB device that presents as both a sound card and a HID
    device. Audio flows through the sound card (same as AudioPlugin).
    PTT is controlled via HID GPIO output (5-byte report).

    Config keys (in addition to AudioPlugin keys):
        vid (str)         — USB vendor ID hex (default '1209')
        pid (str)         — USB product ID hex (default '7388')
        ptt_channel (int) — GPIO channel for PTT (1-3, default 3)
    """

    name = "aioc"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
        "frequency": False,
        "ctcss": False,
        "power": False,
        "volume": True,
        "smeter": False,
        "status": True,
    }

    def __init__(self):
        super().__init__()
        self._hid = None
        self._vid = 0x1209
        self._pid = 0x7388
        self._ptt_channel = 3
        self._ptt_on = False
        self._ptt_timeout = 60  # seconds — safety auto-unkey
        self._ptt_timer = None
        self._ptt_timer_lock = threading.Lock()

    def setup(self, config):
        """Open AIOC audio device + HID for PTT."""
        self._vid = int(config.get('vid', '1209'), 16)
        self._pid = int(config.get('pid', '7388'), 16)
        self._ptt_channel = int(config.get('ptt_channel', 3))
        self._ptt_timeout = int(config.get('ptt_timeout', 60))

        # Find AIOC audio device by name if not specified
        if not config.get('device'):
            config = dict(config)
            config['device'] = 'All-In-One'  # AIOC shows as "All-In-One-Cable" in ALSA/PyAudio

        # Open audio streams via parent class
        super().setup(config)

        # Open HID for PTT
        try:
            import hid as _hid_mod
            self._hid = _hid_mod.Device(vid=self._vid, pid=self._pid)
            print(f"  [Link] AIOCPlugin: HID opened ({self._hid.product})")
        except Exception as e:
            print(f"  [Link] AIOCPlugin: HID open failed: {e}")
            print(f"         PTT will not work. Check USB connection and permissions.")
            self._hid = None

    def teardown(self):
        """Unkey PTT, cancel safety timer, and close HID + audio."""
        self._cancel_ptt_timer()
        if self._ptt_on:
            self._set_ptt(False)
        if self._hid:
            try:
                self._hid.close()
            except Exception:
                pass
            self._hid = None
        super().teardown()

    def execute(self, cmd):
        """Handle commands from master gateway."""
        action = cmd.get('cmd', '') if isinstance(cmd, dict) else ''
        if action == 'ptt':
            state = bool(cmd.get('state', False))
            result = self._set_ptt(state)
            if result.get('ok'):
                if state:
                    self._reset_ptt_timer()
                else:
                    self._cancel_ptt_timer()
            return result
        # volume and status handled by AudioPlugin.execute
        return super().execute(cmd)

    def get_status(self):
        status = super().get_status()
        status.update({
            "plugin": self.name,
            "hid_connected": self._hid is not None,
            "ptt_active": self._ptt_on,
            "ptt_channel": self._ptt_channel,
            "audio_input": self._in_stream is not None,
            "audio_output": self._out_stream is not None,
        })
        return status

    def _set_ptt(self, state_on):
        """Key or unkey the radio via AIOC HID GPIO."""
        if not self._hid:
            return {"ok": False, "error": "HID not connected"}
        try:
            import struct
            state = 1 if state_on else 0
            iomask = 1 << (self._ptt_channel - 1)
            iodata = state << (self._ptt_channel - 1)
            data = struct.pack("<BBBBB", 0, 0, iodata, iomask, 0)
            self._hid.write(bytes(data))
            self._ptt_on = state_on
            print(f"  [Link] AIOCPlugin: PTT {'ON' if state_on else 'OFF'}")
            return {"ok": True, "ptt": state_on}
        except Exception as e:
            print(f"  [Link] AIOCPlugin: PTT error: {e}")
            return {"ok": False, "error": str(e)}

    def _reset_ptt_timer(self):
        """Start or reset the PTT safety timeout timer."""
        with self._ptt_timer_lock:
            if self._ptt_timer:
                self._ptt_timer.cancel()
            self._ptt_timer = threading.Timer(self._ptt_timeout, self._ptt_timeout_fired)
            self._ptt_timer.daemon = True
            self._ptt_timer.start()

    def _cancel_ptt_timer(self):
        """Cancel the PTT safety timeout timer."""
        with self._ptt_timer_lock:
            if self._ptt_timer:
                self._ptt_timer.cancel()
                self._ptt_timer = None

    def _ptt_timeout_fired(self):
        """Called when PTT has been held too long — auto-unkey for safety."""
        print(f"  [Link] AIOCPlugin: WARNING — PTT safety timeout ({self._ptt_timeout}s), auto-unkey")
        self._set_ptt(False)
        with self._ptt_timer_lock:
            self._ptt_timer = None
