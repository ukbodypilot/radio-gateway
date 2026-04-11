#!/usr/bin/env python3
"""
Gateway Link — protocol, server, and client for remote radio endpoints.

This module is fully self-contained: ZERO imports from other gateway modules.
The endpoint script can import it standalone on a remote machine.

Frame format: [1 byte type][2 byte big-endian length][payload]

Dependencies: stdlib only (+ pyaudio inside AudioPlugin.setup only)
"""

import os
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

class _EndpointConn:
    """State for one connected endpoint."""
    __slots__ = ('name', 'sock', 'send_lock', 'reader_thread', 'info',
                 'capabilities', 'last_heartbeat', 'audio_sink', 'addr',
                 'via_tunnel')

    def __init__(self, name, sock, addr=None):
        self.name = name
        self.sock = sock
        self.addr = addr  # (ip, port) tuple
        self.via_tunnel = (addr[0] == '127.0.0.1') if addr else False
        self.send_lock = threading.Lock()
        self.reader_thread = None
        self.info = {}
        self.capabilities = {}
        self.last_heartbeat = time.monotonic()
        self.audio_sink = None  # set by on_register callback return value


class GatewayLinkServer:
    """Listens for multiple simultaneous endpoint connections and exchanges
    framed messages.

    Each endpoint is identified by a unique name from its REGISTER message.
    Duplicate names are rejected.

    Callbacks (all optional, called from reader thread):
        on_register(info_dict) -> object with .push_audio(pcm) method (or None)
        on_command(name, cmd_dict)
        on_disconnect(name)
        on_ack(name, ack_dict)
    """

    def __init__(self, port=9700, on_command=None,
                 on_register=None, on_disconnect=None, on_ack=None,
                 on_endpoint_status=None):
        self._port = port
        self._on_command = on_command
        self._on_register = on_register
        self._on_disconnect = on_disconnect
        self._on_ack = on_ack
        self._on_endpoint_status = on_endpoint_status

        self._server_sock = None
        self._stop = threading.Event()
        self._start_time = time.monotonic()
        self._DEAD_PEER_TIMEOUT = 30.0     # seconds without any frame before declaring dead
        self._REGISTER_TIMEOUT = 10.0      # seconds to wait for REGISTER after connect

        # dict keyed by endpoint name -> _EndpointConn
        self._endpoints = {}
        self._endpoints_lock = threading.RLock()

        self._accept_thread = None
        self._heartbeat_thread = None

    # -- public API ---------------------------------------------------------

    def start(self):
        """Bind, listen, and start accept + heartbeat threads."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(('', self._port))
        self._server_sock.listen(8)
        print(f"  [Link] Server listening on port {self._port}")

        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="LinkAccept", daemon=True)
        self._accept_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="LinkHeartbeat", daemon=True)
        self._heartbeat_thread.start()

        # Publish mDNS service for auto-discovery
        self._mdns_proc = None
        try:
            import subprocess
            self._mdns_proc = subprocess.Popen(
                ['avahi-publish-service', 'RadioGateway', '_radiogateway._tcp', str(self._port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  [Link] mDNS: published _radiogateway._tcp on port {self._port}")
        except Exception as e:
            print(f"  [Link] mDNS: publish failed ({e}) — endpoints must use --server")

    def stop(self):
        """Shut down server, close all connections."""
        self._stop.set()
        # Close all endpoint connections
        with self._endpoints_lock:
            names = list(self._endpoints.keys())
        for name in names:
            self._remove_endpoint(name, reason="stop")
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._mdns_proc:
            try:
                self._mdns_proc.terminate()
            except Exception:
                pass
            self._mdns_proc = None
            self._server_sock = None
        if self._accept_thread:
            self._accept_thread.join(timeout=3)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=3)
        print("  [Link] Server stopped")

    def send_audio_to_all(self, pcm, exclude=None):
        """Send PCM audio to all connected endpoints (thread-safe).

        *exclude* is an optional set of endpoint names to skip.
        """
        with self._endpoints_lock:
            snapshot = list(self._endpoints.values())
        for ep in snapshot:
            if exclude and ep.name in exclude:
                continue
            with ep.send_lock:
                try:
                    GatewayLinkProtocol.send_frame(ep.sock, GatewayLinkProtocol.AUDIO, pcm)
                except (OSError, ConnectionError):
                    pass  # reader thread handles disconnect

    def send_audio_to(self, name, pcm):
        """Send PCM audio to a specific endpoint by name."""
        self._send_to(name, GatewayLinkProtocol.AUDIO, pcm)

    def send_command_to(self, name, cmd):
        """Send a command dict to a specific endpoint by name."""
        self._send_to(name, GatewayLinkProtocol.COMMAND,
                      json.dumps(cmd).encode('utf-8'))

    def send_status_to(self, name, status):
        """Send a status dict to a specific endpoint by name."""
        self._send_to(name, GatewayLinkProtocol.STATUS,
                      json.dumps(status).encode('utf-8'))

    @property
    def connected_count(self):
        """Number of currently connected endpoints."""
        with self._endpoints_lock:
            return len(self._endpoints)

    def get_endpoint_names(self):
        """Return list of connected endpoint names."""
        with self._endpoints_lock:
            return list(self._endpoints.keys())

    def get_endpoint_info(self, name):
        """Return info dict for a specific endpoint, or None."""
        with self._endpoints_lock:
            ep = self._endpoints.get(name)
            if not ep:
                return None
            info = dict(ep.info)
            info['via_tunnel'] = ep.via_tunnel
            info['addr'] = f"{ep.addr[0]}:{ep.addr[1]}" if ep.addr else None
            return info

    # -- internal -----------------------------------------------------------

    def _send_to(self, name, frame_type, payload):
        """Thread-safe send to a specific endpoint by name."""
        with self._endpoints_lock:
            ep = self._endpoints.get(name)
        if ep is None:
            return
        with ep.send_lock:
            try:
                GatewayLinkProtocol.send_frame(ep.sock, frame_type, payload)
            except (OSError, ConnectionError):
                pass  # reader thread handles disconnect

    def _remove_endpoint(self, name, reason=""):
        """Remove an endpoint from the dict, close socket, notify callback."""
        with self._endpoints_lock:
            ep = self._endpoints.pop(name, None)
        if ep:
            print(f"  [Link] _remove_endpoint({name}) reason={reason}")
        if ep:
            try:
                ep.sock.close()
            except OSError:
                pass
            if self._on_disconnect:
                try:
                    self._on_disconnect(name)
                except Exception:
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
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            # Start a reader thread for this new socket — it will wait for
            # REGISTER as its first frame.
            t = threading.Thread(
                target=self._reader_loop, args=(conn, addr),
                name=f"LinkReader-{addr[0]}:{addr[1]}", daemon=True)
            t.start()

    def _reader_loop(self, sock, addr):
        """Read frames from a connected endpoint until disconnect.

        The first frame must be REGISTER (within _REGISTER_TIMEOUT seconds).
        After registration, frames are dispatched by type.
        """
        P = GatewayLinkProtocol
        ep_name = None
        try:
            # --- Wait for REGISTER as first frame ---
            sock.settimeout(self._REGISTER_TIMEOUT)
            result = P.recv_frame(sock)
            if result is None:
                print(f"  [Link] {addr[0]}:{addr[1]} disconnected before REGISTER")
                return
            ftype, payload = result
            if ftype != P.REGISTER:
                print(f"  [Link] {addr[0]}:{addr[1]} first frame was not REGISTER (type={ftype}), closing")
                try:
                    P.send_frame(sock, P.COMMAND,
                                 json.dumps({"error": "first frame must be REGISTER"}).encode('utf-8'))
                except (OSError, ConnectionError):
                    pass
                return

            sock.settimeout(20.0)  # 20s timeout for send+recv — detects cable pull

            info = json.loads(payload)
            ep_name = info.get('name', '')
            if not ep_name:
                print(f"  [Link] {addr[0]}:{addr[1]} REGISTER missing name, closing")
                try:
                    P.send_frame(sock, P.COMMAND,
                                 json.dumps({"error": "REGISTER must include 'name'"}).encode('utf-8'))
                except (OSError, ConnectionError):
                    pass
                return

            # Check for duplicate name
            with self._endpoints_lock:
                if ep_name in self._endpoints:
                    print(f"  [Link] Duplicate endpoint name '{ep_name}' from "
                          f"{addr[0]}:{addr[1]}, rejecting")
                    try:
                        P.send_frame(sock, P.COMMAND,
                                     json.dumps({"error": f"name '{ep_name}' already connected"}).encode('utf-8'))
                    except (OSError, ConnectionError):
                        pass
                    return

                # Build endpoint and store
                ep = _EndpointConn(ep_name, sock, addr)
                ep.info = info
                ep.reader_thread = threading.current_thread()
                caps = info.get('capabilities', {})
                ep.capabilities = caps if isinstance(caps, dict) else {}
                self._endpoints[ep_name] = ep

            enabled = [k for k, v in ep.capabilities.items() if v]
            print(f"  [Link] Endpoint registered: {ep_name} "
                  f"plugin={info.get('plugin', '?')} "
                  f"v={info.get('version', '?')} "
                  f"caps={enabled}")

            # Call on_register — return value is the audio sink for this endpoint
            if self._on_register:
                audio_sink = self._on_register(info)
                ep.audio_sink = audio_sink

            # --- Main frame dispatch loop ---
            while not self._stop.is_set():
                result = P.recv_frame(sock)
                if result is None:
                    break
                ftype, payload = result
                # Any frame is proof of life
                ep.last_heartbeat = time.monotonic()
                try:
                    if ftype == P.AUDIO:
                        if ep.audio_sink:
                            ep.audio_sink.push_audio(payload)
                    elif ftype == P.COMMAND:
                        if self._on_command:
                            self._on_command(ep_name, json.loads(payload))
                    elif ftype == P.ACK:
                        ack = json.loads(payload)
                        cmd_name = ack.get('cmd', ack.get('cmd_id', '?'))
                        ok = ack.get('ok', False)
                        print(f"  [Link] ACK received from {ep_name}: cmd={cmd_name} ok={ok}")
                        if self._on_ack:
                            try:
                                self._on_ack(ep_name, ack)
                            except Exception as e:
                                print(f"  [Link] ACK callback error: {e}")
                    elif ftype == P.STATUS:
                        ep.last_heartbeat = time.monotonic()
                        if self._on_endpoint_status:
                            try:
                                status = json.loads(payload)
                                self._on_endpoint_status(ep_name, status)
                            except (json.JSONDecodeError, Exception):
                                pass
                    elif ftype == P.REGISTER:
                        # Re-registration not allowed; ignore
                        print(f"  [Link] Ignoring duplicate REGISTER from {ep_name}")
                except json.JSONDecodeError as e:
                    print(f"  [Link] Bad JSON from {ep_name}: {e}")
                except Exception as e:
                    print(f"  [Link] Callback error for {ep_name}: {e}")

        except socket.timeout:
            print(f"  [Link] {addr[0]}:{addr[1]} REGISTER timeout, closing")
        except Exception as e:
            if not self._stop.is_set():
                print(f"  [Link] Reader error for {ep_name or addr}: {e}")
        finally:
            # Remove from endpoints dict (only if this reader owns the entry)
            _reader_removed = False
            if ep_name:
                with self._endpoints_lock:
                    existing = self._endpoints.get(ep_name)
                    if existing is not None and existing.sock is sock:
                        del self._endpoints[ep_name]
                        _reader_removed = True
                print(f"  [Link] Reader cleanup: {ep_name} removed={_reader_removed} (entry={'exists' if existing else 'gone'})")
            try:
                sock.close()
            except OSError:
                pass
            if ep_name and _reader_removed:
                print(f"  [Link] Endpoint disconnected: {ep_name}")
                if self._on_disconnect:
                    try:
                        self._on_disconnect(ep_name)
                    except Exception:
                        pass

    def _heartbeat_loop(self):
        """Send heartbeat every 5s to all endpoints; detect dead peers."""
        while not self._stop.is_set():
            self._stop.wait(5.0)
            if self._stop.is_set():
                break
            uptime = time.monotonic() - self._start_time
            hb_payload = json.dumps({"type": "heartbeat", "uptime": round(uptime, 1)}).encode('utf-8')
            now = time.monotonic()

            with self._endpoints_lock:
                snapshot = list(self._endpoints.values())

            dead = []
            for ep in snapshot:
                # Send heartbeat
                with ep.send_lock:
                    try:
                        GatewayLinkProtocol.send_frame(ep.sock, GatewayLinkProtocol.STATUS, hb_payload)
                    except (OSError, ConnectionError):
                        pass  # reader thread handles disconnect
                # Dead peer detection
                if ep.last_heartbeat > 0:
                    silence = now - ep.last_heartbeat
                    if silence > self._DEAD_PEER_TIMEOUT:
                        dead.append(ep.name)

            for name in dead:
                print(f"  [Link] Dead peer detected: {name} — closing")
                self._remove_endpoint(name, reason="dead_peer")


# ---------------------------------------------------------------------------
# mDNS Discovery
# ---------------------------------------------------------------------------

def discover_gateway(timeout=5):
    """Discover a RadioGateway on the local network via mDNS.

    Returns (host, port) or None if not found.
    Requires avahi-browse to be installed.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['avahi-browse', '-t', '-r', '-p', '_radiogateway._tcp'],
            capture_output=True, text=True, timeout=timeout)
        for line in result.stdout.strip().split('\n'):
            if not line or line.startswith('+'):
                continue
            # Resolved line format: =;iface;protocol;name;type;domain;hostname;address;port;txt
            parts = line.split(';')
            if len(parts) >= 9 and parts[0] == '=' and parts[2] == 'IPv4':
                host = parts[7]
                port = int(parts[8])
                print(f"  [Link] mDNS: discovered gateway at {host}:{port}")
                return (host, port)
    except FileNotFoundError:
        print("  [Link] mDNS: avahi-browse not installed — use --server")
    except subprocess.TimeoutExpired:
        print("  [Link] mDNS: no gateway found on local network")
    except Exception as e:
        print(f"  [Link] mDNS: discovery error: {e}")
    return None


# ---------------------------------------------------------------------------
# Client (endpoint side)
# ---------------------------------------------------------------------------

class WebSocketTransport:
    """WebSocket client transport using stdlib only (ssl + http.client).

    Wraps a WebSocket connection to look like a socket for the link protocol.
    Each link protocol frame is sent/received as one WS binary message.
    """

    def __init__(self):
        self._sock = None
        self._lock = threading.Lock()

    def connect(self, ws_url, timeout=10):
        """Connect to a WebSocket URL (ws:// or wss://).  Returns True on success."""
        import ssl
        import hashlib
        import base64

        # Parse URL
        if ws_url.startswith('wss://'):
            host_path = ws_url[6:]
            use_ssl = True
            default_port = 443
        elif ws_url.startswith('ws://'):
            host_path = ws_url[5:]
            use_ssl = False
            default_port = 80
        else:
            raise ValueError(f"Invalid WS URL: {ws_url}")

        if '/' in host_path:
            host_port, path = host_path.split('/', 1)
            path = '/' + path
        else:
            host_port = host_path
            path = '/'

        if ':' in host_port:
            host, port = host_port.rsplit(':', 1)
            port = int(port)
        else:
            host = host_port
            port = default_port

        # TCP connect
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(timeout)
        raw.connect((host, port))
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if use_ssl:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw

        # WS handshake
        import os as _os
        ws_key = base64.b64encode(_os.urandom(16)).decode()
        handshake = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {ws_key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'\r\n'
        )
        sock.sendall(handshake.encode())

        # Read response (look for 101)
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                sock.close()
                return False
            resp += chunk

        if b'101' not in resp.split(b'\r\n')[0]:
            sock.close()
            return False

        sock.settimeout(15)  # match link protocol timeout
        self._sock = sock
        return True

    def send_frame(self, frame_type, payload):
        """Send a link protocol frame as a WS binary message."""
        frame_data = struct.pack('>BH', frame_type, len(payload)) + payload
        self._ws_send(frame_data)

    def recv_frame(self):
        """Receive a link protocol frame from a WS binary message.

        Returns (frame_type, payload) or None on disconnect.
        """
        data = self._ws_recv()
        if data is None or len(data) < 3:
            return None
        frame_type, length = struct.unpack('>BH', data[:3])
        payload = data[3:]
        if len(payload) < length:
            return None
        return (frame_type, payload[:length])

    def close(self):
        """Close the WebSocket connection."""
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.sendall(b'\x88\x02\x03\xe8')  # WS close frame
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _ws_send(self, data):
        """Send a masked WS binary frame (clients MUST mask)."""
        import os as _os
        frame = bytearray()
        frame.append(0x82)  # FIN + binary
        mask_key = _os.urandom(4)
        length = len(data)
        if length < 126:
            frame.append(0x80 | length)  # masked
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, 'big'))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, 'big'))
        frame.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        frame.extend(masked)
        self._sock.sendall(bytes(frame))

    def _ws_recv(self):
        """Receive one WS message. Returns payload bytes or None."""
        sock = self._sock
        if not sock:
            return None
        try:
            hdr = self._recv_exact(2)
            if not hdr:
                return None
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            plen = hdr[1] & 0x7F
            if plen == 126:
                ext = self._recv_exact(2)
                if not ext:
                    return None
                plen = int.from_bytes(ext, 'big')
            elif plen == 127:
                ext = self._recv_exact(8)
                if not ext:
                    return None
                plen = int.from_bytes(ext, 'big')
            mask_key = self._recv_exact(4) if masked else None
            payload = self._recv_exact(plen) if plen else b''
            if payload is None and plen > 0:
                return None
            if masked and mask_key and payload:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x08:  # Close
                return None
            if opcode == 0x09:  # Ping → Pong
                pong = bytearray([0x8A, len(payload) if len(payload) < 126 else 0])
                pong.extend(payload)
                try:
                    sock.sendall(bytes(pong))
                except Exception:
                    pass
                return self._ws_recv()
            return payload
        except (OSError, ConnectionError):
            return None

    def _recv_exact(self, n):
        """Read exactly n bytes."""
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except (OSError, ConnectionError):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)


class GatewayLinkClient:
    """Connects to a GatewayLinkServer and exchanges framed messages.

    Automatically reconnects on disconnect (5 s backoff).
    Supports both direct TCP and WebSocket (for tunnel) connections.

    Callbacks (all optional, called from reader thread):
        on_audio(pcm_bytes)
        on_command(cmd_dict)
        on_status(status_dict)
    """

    def __init__(self, host, port, name, capabilities, plugin_name="audio",
                 on_audio=None, on_command=None, on_status=None,
                 on_connect=None, on_disconnect=None,
                 ws_url=None, url_resolver=None):
        self._host = host
        self._port = port
        self._name = name
        self._capabilities = capabilities
        self._plugin_name = plugin_name
        self._ws_url = ws_url            # WebSocket URL for tunnel mode
        self._url_resolver = url_resolver  # callable() → ws_url (e.g. Drive lookup)

        self._on_audio = on_audio
        self._on_command = on_command
        self._on_status = on_status
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._sock = None
        self._ws_transport = None  # WebSocketTransport when using tunnel
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
        return self._sock is not None or self._ws_transport is not None

    # -- internal -----------------------------------------------------------

    def _send(self, frame_type, payload):
        """Thread-safe send to the server (TCP or WS)."""
        _need_close = False
        with self._send_lock:
            ws = self._ws_transport
            sock = self._sock
            if ws:
                try:
                    ws.send_frame(frame_type, payload)
                except (OSError, ConnectionError) as e:
                    print(f"  [Link] Client WS send error: {e}")
                    self._ws_transport = None
                    _need_close = True
            elif sock:
                try:
                    GatewayLinkProtocol.send_frame(sock, frame_type, payload)
                except (OSError, ConnectionError) as e:
                    print(f"  [Link] Client send error: {e}")
                    self._sock = None
                    _need_close = True
            else:
                return
        if _need_close:
            self._close()

    def _close(self):
        """Close the connection (TCP or WS)."""
        with self._send_lock:
            sock = self._sock
            ws = self._ws_transport
            self._sock = None
            self._ws_transport = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _connect_loop(self):
        """Connect to the server, auto-reconnect on failure.

        Connection strategy:
        1. Try direct TCP to host:port (LAN mode)
        2. If TCP fails and ws_url is available: try WebSocket (tunnel mode)
        3. If WS fails: call url_resolver to fetch fresh URL from Google Drive
        4. Retry with exponential backoff (5s → 10s → 30s → 60s max)
        """
        _backoff = 5.0
        _MAX_BACKOFF = 60.0
        _ws_failures = 0

        while not self._stop.is_set():
            connected_via = None

            # ── Attempt 1: Direct TCP ──
            if self._host and self._port:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect((self._host, self._port))
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    print(f"  [Link] Connected to {self._host}:{self._port} (TCP)")
                    with self._send_lock:
                        self._sock = sock
                        self._ws_transport = None
                    connected_via = 'tcp'
                    _backoff = 5.0
                except (OSError, ConnectionError) as e:
                    print(f"  [Link] TCP {self._host}:{self._port} failed: {e}")
                    try:
                        sock.close()
                    except Exception:
                        pass

            # ── Attempt 2: WebSocket via tunnel ──
            if not connected_via and self._ws_url:
                ws = WebSocketTransport()
                try:
                    if ws.connect(self._ws_url, timeout=15):
                        print(f"  [Link] Connected via WebSocket tunnel")
                        with self._send_lock:
                            self._ws_transport = ws
                            self._sock = None
                        connected_via = 'ws'
                        _backoff = 5.0
                        _ws_failures = 0
                    else:
                        print(f"  [Link] WS handshake failed (URL may be expired)")
                        _ws_failures += 1
                except Exception as e:
                    print(f"  [Link] WS connect failed: {e}")
                    _ws_failures += 1

            # ── Attempt 3: Resolve fresh URL from Google Drive ──
            if not connected_via and _ws_failures >= 2 and self._url_resolver:
                print(f"  [Link] Fetching fresh tunnel URL from Drive...")
                try:
                    new_url = self._url_resolver()
                    if new_url and new_url != self._ws_url:
                        print(f"  [Link] New tunnel URL obtained")
                        self._ws_url = new_url
                        _ws_failures = 0  # retry with new URL immediately
                        continue
                    elif new_url:
                        print(f"  [Link] Same URL from Drive (still stale)")
                    else:
                        print(f"  [Link] No URL from Drive")
                except Exception as e:
                    print(f"  [Link] Drive URL resolve error: {e}")

            # ── No connection — backoff and retry ──
            if not connected_via:
                print(f"  [Link] Retrying in {_backoff:.0f}s...")
                if self._stop.wait(_backoff):
                    break
                _backoff = min(_backoff * 1.5, _MAX_BACKOFF)
                continue

            # ── Connected — send registration ──
            reg_info = {
                "name": self._name,
                "plugin": self._plugin_name,
                "capabilities": self._capabilities,
                "version": "1.0",
            }
            try:
                if connected_via == 'ws':
                    self._ws_transport.send_frame(
                        GatewayLinkProtocol.REGISTER,
                        json.dumps(reg_info).encode('utf-8'))
                else:
                    GatewayLinkProtocol.send_register(self._sock, reg_info)
            except (OSError, ConnectionError) as e:
                print(f"  [Link] Registration send failed: {e}")
                self._close()
                if self._stop.wait(5.0):
                    break
                continue

            # Notify caller
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception as e:
                    print(f"  [Link] on_connect callback error: {e}")

            # Heartbeat thread
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

            # Reader loop (works with both TCP sock and WS transport)
            if connected_via == 'ws':
                self._reader_loop_ws(self._ws_transport)
            else:
                self._reader_loop(self._sock)

            hb_stop.set()
            self._close()
            print(f"  [Link] Connection closed ({connected_via})")

            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception as e:
                    print(f"  [Link] on_disconnect callback error: {e}")

            if not self._stop.is_set():
                print(f"  [Link] Reconnecting in 5s...")
                if self._stop.wait(5.0):
                    break

    def _reader_loop(self, sock):
        """Read frames from the server until disconnect."""
        P = GatewayLinkProtocol
        _frame_count = 0
        # Socket already has 10s timeout from _connect_loop.
        # Server heartbeat every 5s — 10s timeout means dead connection.
        try:
            while not self._stop.is_set():
                result = P.recv_frame(sock)
                if result is None:
                    print(f"  [Link] Disconnected from server (after {_frame_count} frames)")
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

    def _reader_loop_ws(self, ws):
        """Read frames from the server via WebSocket until disconnect."""
        P = GatewayLinkProtocol
        _frame_count = 0
        try:
            while not self._stop.is_set():
                result = ws.recv_frame()
                if result is None:
                    print(f"  [Link] WS disconnected (after {_frame_count} frames)")
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
                except json.JSONDecodeError as e:
                    print(f"  [Link] Bad JSON from server: {e}")
                except Exception as e:
                    print(f"  [Link] Client callback error: {e}")
        except Exception as e:
            if not self._stop.is_set():
                print(f"  [Link] WS reader error: {e}")
        finally:
            print("  [Link] WS disconnected from server")
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
        "rx_gain": False,
        "tx_gain": False,
        "smeter": False,
        "status": True,  # all plugins support status
    }

    def setup(self, config):
        """Initialize hardware.  *config* is a dict from command-line args."""
        pass

    def teardown(self):
        """Clean shutdown of hardware."""
        pass

    def get_audio(self, chunk_size=4800):
        """Read one chunk of PCM audio from hardware.

        Returns (bytes_or_none, should_trigger_ptt) to match AudioSource contract.
        Default chunk: 48 kHz 16-bit signed LE mono, 4800 bytes = 50 ms.
        """
        return None, False

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
        "rx_gain": True,
        "tx_gain": True,
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
        self._rx_gain_db = 0.0
        self._tx_gain_db = 0.0
        self._settings_file = os.path.expanduser('~/.config/link-endpoint/settings.json')
        self._start_time = time.monotonic()
        # Noise gate — squelch AIOC noise floor when radio squelch is closed
        self._gate_enabled = True
        self._gate_threshold_db = -40.0   # dB below full-scale
        self._gate_envelope = 0.0         # smoothed RMS level
        self._gate_open = False
        self._gate_attack = 0.3           # envelope rise speed (0-1)
        self._gate_release = 0.05         # envelope fall speed (0-1)
        # Stream health — auto-reopen on stale/dead PyAudio stream
        self._read_errors = 0             # consecutive read exceptions
        self._zero_reads = 0              # consecutive zero-level reads (peak < 5)
        self._reopen_count = 0            # total reopens for logging
        self._last_config = None          # saved config for reopen

    def setup(self, config):
        """Open PyAudio input + output streams.

        *config* keys:
            device (str)   — device name substring or index (default '')
            rate (int)     — sample rate (default 48000)
            channels (int) — channel count (default 1)
        """
        import pyaudio
        self._last_config = dict(config)  # save for stream reopen

        # Load saved settings (gains + gate)
        saved = self._load_settings()
        if saved:
            self._rx_gain_db = max(-10, min(10, float(saved.get('rx_gain_db', 0))))
            self._tx_gain_db = max(-10, min(10, float(saved.get('tx_gain_db', 0))))
            if 'gate_threshold_db' in saved:
                self._gate_threshold_db = max(-60, min(-10, float(saved['gate_threshold_db'])))
            if 'gate_enabled' in saved:
                self._gate_enabled = bool(saved['gate_enabled'])
            print(f"  [Link] AudioPlugin: restored settings RX={self._rx_gain_db:+.1f} dB, TX={self._tx_gain_db:+.1f} dB, gate={'on' if self._gate_enabled else 'off'} @ {self._gate_threshold_db:.0f} dB")

        self._pa = pyaudio.PyAudio()
        self._device_name = config.get('device', '')
        rate = int(config.get('rate', self.RATE))
        channels = int(config.get('channels', self.CHANNELS))
        fmt = pyaudio.paInt16

        dev_index = self._find_device(self._device_name)
        # Find separate input/output indices — some backends (PipeWire)
        # enumerate different indices for input vs output capability.
        # dev_index may point to a device with only input OR only output,
        # so always scan for the correct capability.
        in_index = None
        out_index = None
        if self._pa and self._device_name:
            _dn = self._device_name.lower()
            for i in range(self._pa.get_device_count()):
                try:
                    info = self._pa.get_device_info_by_index(i)
                    if _dn in info.get('name', '').lower():
                        if info.get('maxInputChannels', 0) > 0 and in_index is None:
                            in_index = i
                        if info.get('maxOutputChannels', 0) > 0 and out_index is None:
                            out_index = i
                except Exception:
                    continue
        # Fall back to generic find if name scan didn't match
        if in_index is None and dev_index is not None:
            in_index = dev_index
        if out_index is None and dev_index is not None:
            out_index = dev_index
        if in_index != out_index:
            print(f"  [Link] AudioPlugin: separate I/O indices: in={in_index} out={out_index}")

        try:
            kw_in = {'input_device_index': in_index} if in_index is not None else {}
            self._in_stream = self._pa.open(
                format=fmt, channels=channels, rate=rate,
                input=True, frames_per_buffer=self.CHUNK_FRAMES,
                **kw_in)
            print(f"  [Link] AudioPlugin: input stream opened"
                  f" (device={self._device_name or 'default'}, idx={in_index}, rate={rate})")
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to open input stream: {e}")
            self._in_stream = None

        try:
            kw_out = {'output_device_index': out_index} if out_index is not None else {}
            self._out_stream = self._pa.open(
                format=fmt, channels=channels, rate=rate,
                output=True, frames_per_buffer=self.CHUNK_FRAMES,
                **kw_out)
            print(f"  [Link] AudioPlugin: output stream opened"
                  f" (device={self._device_name or 'default'}, idx={out_index}, rate={rate})")
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to open output stream: {e}")
            self._out_stream = None

    def teardown(self):
        """Close PyAudio streams, save gains, and terminate."""
        self._save_settings()
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

    def get_audio(self, chunk_size=None):
        """Read one 50 ms chunk from the input stream, applying RX gain and noise gate."""
        # In data mode, Direwolf owns the capture device — don't read or reopen
        if getattr(self, '_mode', 'audio') == 'data':
            return None, False
        if not self._in_stream:
            # Stream is dead — try to reopen if we have config
            if self._last_config and self._pa:
                self.reopen_audio()
            return None, False
        try:
            data = self._in_stream.read(self.CHUNK_FRAMES, exception_on_overflow=False)
            self._read_errors = 0  # reset on successful read

            # Detect dead stream: all-zero audio for 10s (200 reads at 50ms)
            # AIOC always produces noise, so sustained silence = dead hardware
            if data and not self._gate_enabled:
                import struct as _st2
                _pk = max(abs(x) for x in _st2.unpack(f'<{len(data)//2}h', data))
                if _pk < 5:
                    self._zero_reads += 1
                    if self._zero_reads >= 200:
                        print(f"  [Link] AudioPlugin: 200 consecutive zero reads — reopening stream", flush=True)
                        self.reopen_audio()
                        return None, False
                else:
                    self._zero_reads = 0
            elif data:
                self._zero_reads = 0

            if data and self._rx_gain_db != 0.0:
                data = self._apply_volume(data, self._db_to_linear(self._rx_gain_db))
            # Noise gate — suppress AIOC noise floor
            if data and self._gate_enabled:
                import struct as _st, math as _m
                n = len(data) // 2
                samples = _st.unpack(f'<{n}h', data)
                rms = _m.sqrt(sum(s * s for s in samples) / n) if n else 0
                db = 20 * _m.log10(rms / 32767.0) if rms > 0 else -100.0
                # Smooth envelope
                if db > self._gate_envelope:
                    self._gate_envelope += (db - self._gate_envelope) * self._gate_attack
                else:
                    self._gate_envelope += (db - self._gate_envelope) * self._gate_release
                if self._gate_envelope > self._gate_threshold_db:
                    self._gate_open = True
                elif self._gate_envelope < self._gate_threshold_db - 3:  # 3 dB hysteresis
                    self._gate_open = False
                if not self._gate_open:
                    return None, False
            return data, False
        except Exception as e:
            self._read_errors += 1
            if getattr(self, '_mode', 'audio') == 'data':
                return None, False  # expected — stream was closed for data mode
            if self._read_errors <= 3 or self._read_errors % 50 == 0:
                print(f"  [Link] AudioPlugin: read error: {e} [{self._read_errors}]", flush=True)
            if self._read_errors >= 5:
                print(f"  [Link] AudioPlugin: {self._read_errors} consecutive errors — reopening stream", flush=True)
                self.reopen_audio()
            return None, False

    def reopen_audio(self):
        """Close and reopen audio streams on the existing PyAudio instance.

        Called on gateway reconnect or stale stream detection.
        Does NOT terminate PyAudio — PipeWire loses device enumeration on re-init.
        """
        import pyaudio
        print(f"  [Link] AudioPlugin: reopening audio streams", flush=True)
        self._reopen_count += 1
        self._read_errors = 0
        self._zero_reads = 0
        # Close existing streams
        for stream in (self._in_stream, self._out_stream):
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self._in_stream = None
        self._out_stream = None
        # Terminate and reinit PyAudio to fully release ALSA device handles.
        # PipeWire may lose some device enumeration, but we search by name anyway.
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        time.sleep(1)  # pause for ALSA to release device

        if not self._pa:
            self._pa = pyaudio.PyAudio()

        config = self._last_config or {}
        rate = int(config.get('rate', self.RATE))
        channels = int(config.get('channels', self.CHANNELS))

        # Find device indices — scan for correct input/output capability
        in_index = None
        out_index = None
        if self._device_name:
            _dn = self._device_name.lower()
            for i in range(self._pa.get_device_count()):
                try:
                    info = self._pa.get_device_info_by_index(i)
                    if _dn in info.get('name', '').lower():
                        if info.get('maxInputChannels', 0) > 0 and in_index is None:
                            in_index = i
                        if info.get('maxOutputChannels', 0) > 0 and out_index is None:
                            out_index = i
                except Exception:
                    continue

        try:
            kw_in = {'input_device_index': in_index} if in_index is not None else {}
            self._in_stream = self._pa.open(
                format=pyaudio.paInt16, channels=channels, rate=rate,
                input=True, frames_per_buffer=self.CHUNK_FRAMES, **kw_in)
            print(f"  [Link] AudioPlugin: input stream reopened (idx={in_index})", flush=True)
        except Exception as e:
            print(f"  [Link] AudioPlugin: input reopen failed: {e}", flush=True)
            self._in_stream = None

        try:
            kw_out = {'output_device_index': out_index} if out_index is not None else {}
            self._out_stream = self._pa.open(
                format=pyaudio.paInt16, channels=channels, rate=rate,
                output=True, frames_per_buffer=self.CHUNK_FRAMES, **kw_out)
            print(f"  [Link] AudioPlugin: output stream reopened (idx={out_index})", flush=True)
        except Exception as e:
            print(f"  [Link] AudioPlugin: output reopen failed: {e}", flush=True)
            self._out_stream = None

    def put_audio(self, pcm):
        """Write PCM audio to the output stream, applying TX gain."""
        if not self._out_stream:
            return
        try:
            if self._tx_gain_db != 0.0:
                pcm = self._apply_volume(pcm, self._db_to_linear(self._tx_gain_db))
            self._out_stream.write(pcm)
        except Exception as e:
            print(f"  [Link] AudioPlugin: write error: {e}")

    def execute(self, cmd):
        """Handle commands from master gateway."""
        action = cmd.get('cmd', '') if isinstance(cmd, dict) else ''
        if action == 'rx_gain':
            self._rx_gain_db = max(-10, min(10, float(cmd.get('db', 0))))
            self._save_settings()
            print(f"  [Link] AudioPlugin: RX gain set to {self._rx_gain_db:+.1f} dB")
            return {"ok": True, "rx_gain_db": self._rx_gain_db}
        if action == 'tx_gain':
            self._tx_gain_db = max(-10, min(10, float(cmd.get('db', 0))))
            self._save_settings()
            print(f"  [Link] AudioPlugin: TX gain set to {self._tx_gain_db:+.1f} dB")
            return {"ok": True, "tx_gain_db": self._tx_gain_db}
        if action == 'gate':
            if 'enabled' in cmd:
                self._gate_enabled = bool(cmd['enabled'])
            if 'threshold' in cmd:
                self._gate_threshold_db = max(-60, min(-10, float(cmd['threshold'])))
            self._save_settings()
            return {"ok": True, "gate_enabled": self._gate_enabled,
                    "gate_threshold_db": self._gate_threshold_db}
        if action == 'status':
            return {"ok": True, "status": self.get_status()}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        status = {
            "plugin": self.name,
            "device": self._device_name or "default",
            "rate": self.RATE,
            "input_active": self._in_stream is not None,
            "output_active": self._out_stream is not None,
            "rx_gain_db": self._rx_gain_db,
            "tx_gain_db": self._tx_gain_db,
            "gate_enabled": self._gate_enabled,
            "gate_threshold_db": self._gate_threshold_db,
            "gate_open": self._gate_open,
        }
        status['uptime'] = round(time.monotonic() - self._start_time, 1)
        # System stats — CPU, RAM, disk for endpoint machine health
        status.update(self._get_system_stats())
        return status

    _prev_cpu = None  # class-level: (total, idle) from last sample

    @classmethod
    def _get_system_stats(cls):
        """Get CPU, RAM, and disk usage for the endpoint machine."""
        stats = {}
        try:
            # CPU usage from /proc/stat delta (all cores combined)
            with open('/proc/stat') as f:
                parts = f.readline().split()  # cpu user nice system idle iowait irq softirq ...
            vals = [int(v) for v in parts[1:]]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            if cls._prev_cpu:
                dt = total - cls._prev_cpu[0]
                di = idle - cls._prev_cpu[1]
                stats['cpu_pct'] = round((1.0 - di / dt) * 100, 1) if dt > 0 else 0.0
            else:
                stats['cpu_pct'] = 0.0
            cls._prev_cpu = (total, idle)
        except Exception:
            pass
        try:
            # RAM usage from /proc/meminfo
            mem = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    parts = line.split()
                    if parts[0] in ('MemTotal:', 'MemAvailable:'):
                        mem[parts[0]] = int(parts[1]) * 1024  # kB to bytes
            total = mem.get('MemTotal:', 0)
            avail = mem.get('MemAvailable:', 0)
            if total > 0:
                stats['ram_pct'] = round((1 - avail / total) * 100, 1)
                stats['ram_mb'] = round((total - avail) / 1048576)
                stats['ram_total_mb'] = round(total / 1048576)
        except Exception:
            pass
        try:
            # Disk usage for root filesystem
            st = os.statvfs('/')
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total > 0:
                stats['disk_pct'] = round((1 - free / total) * 100, 1)
                stats['disk_free_gb'] = round(free / 1073741824, 1)
        except Exception:
            pass
        try:
            # CPU temperature (RPi / thermal zone)
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                stats['cpu_temp_c'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            pass
        return stats

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

    @staticmethod
    def _db_to_linear(db):
        """Convert dB gain to linear multiplier."""
        return 10 ** (db / 20.0)

    def _save_settings(self):
        """Save current gain settings to JSON file."""
        try:
            d = os.path.dirname(self._settings_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self._settings_file, 'w') as f:
                json.dump({"rx_gain_db": self._rx_gain_db,
                           "tx_gain_db": self._tx_gain_db,
                           "gate_threshold_db": self._gate_threshold_db,
                           "gate_enabled": self._gate_enabled}, f)
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to save settings: {e}")

    def _load_settings(self):
        """Load gain settings from JSON file. Returns dict or None."""
        try:
            with open(self._settings_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

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

    Supports two modes:
        audio — (default) streams RX audio to gateway via link protocol
        data  — runs Direwolf TNC locally, AIOC capture goes directly to
                Direwolf for packet decode. KISS TCP on port 8001.

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
        "rx_gain": True,
        "tx_gain": True,
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
        # Data mode (Direwolf TNC)
        self._mode = 'audio'             # 'audio' or 'data'
        self._direwolf_proc = None
        self._direwolf_conf_path = '/tmp/direwolf_endpoint.conf'
        self._direwolf_path = '/usr/bin/direwolf'
        self._dw_callsign = 'N0CALL'
        self._dw_modem = 1200
        self._dw_kiss_port = 8001
        self._aioc_hw = None             # ALSA device name (e.g. 'hw:3,0')

    def setup(self, config):
        """Open AIOC audio device + HID for PTT."""
        self._vid = int(config.get('vid', '1209'), 16)
        self._pid = int(config.get('pid', '7388'), 16)
        self._ptt_channel = int(config.get('ptt_channel', 3))
        self._ptt_timeout = int(config.get('ptt_timeout', 60))

        # Find AIOC audio device by ALSA card name if not specified.
        # PyAudio via PipeWire doesn't enumerate ALSA hardware devices,
        # so we find the hw:N,0 device name from /proc/asound/cards.
        if not config.get('device'):
            config = dict(config)
            aioc_hw = None
            try:
                with open('/proc/asound/cards') as f:
                    for line in f:
                        line = line.strip()
                        if 'AllInOneCable' in line or 'All-In-One' in line:
                            card_num = line.split()[0]
                            aioc_hw = f'hw:{card_num},0'
                            break
            except Exception:
                pass
            config['device'] = aioc_hw or 'All-In-One'
            if aioc_hw:
                print(f"  [Link] AIOCPlugin: found AIOC at {aioc_hw}")
                self._aioc_hw = aioc_hw
        else:
            self._aioc_hw = config.get('device', '')

        # Open audio streams via parent class
        super().setup(config)

        # Open HID for PTT (support both hid.Device and hid.device APIs)
        try:
            import hid as _hid_mod
            if hasattr(_hid_mod, 'Device'):
                self._hid = _hid_mod.Device(vid=self._vid, pid=self._pid)
            else:
                self._hid = _hid_mod.device()
                self._hid.open(self._vid, self._pid)
            _prod = getattr(self._hid, 'product', '') or getattr(self._hid, 'get_product_string', lambda: '')()
            print(f"  [Link] AIOCPlugin: HID opened ({_prod})")
        except Exception as e:
            print(f"  [Link] AIOCPlugin: HID open failed: {e}")
            print(f"         PTT will not work. Check USB connection and permissions.")
            self._hid = None

    def teardown(self):
        """Unkey PTT, cancel safety timer, stop Direwolf, and close HID + audio."""
        self._cancel_ptt_timer()
        if self._ptt_on:
            self._set_ptt(False)
        self._stop_direwolf()
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
        if action == 'mode':
            return self._set_mode(cmd)
        # rx_gain, tx_gain, and status handled by AudioPlugin.execute
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
            "mode": self._mode,
            "direwolf_running": self._direwolf_proc is not None and self._direwolf_proc.poll() is None,
            "direwolf_kiss_port": self._dw_kiss_port if self._mode == 'data' else None,
        })
        return status

    def _set_mode(self, cmd):
        """Switch between audio and data (Direwolf TNC) mode.

        In data mode, PyAudio input is closed and Direwolf reads the AIOC
        directly for clean packet decode. KISS TCP is exposed on the
        configured port for the gateway to connect.

        cmd keys: mode ('audio'/'data'), callsign, ssid, modem, kiss_port
        """
        new_mode = cmd.get('mode', 'audio')
        if new_mode not in ('audio', 'data'):
            return {"ok": False, "error": f"invalid mode: {new_mode}"}
        if new_mode == self._mode:
            return {"ok": True, "mode": self._mode}

        # Read optional TNC config from command
        if 'callsign' in cmd:
            self._dw_callsign = str(cmd['callsign']).strip().upper()
        if 'ssid' in cmd:
            ssid = int(cmd['ssid'])
            if ssid:
                self._dw_callsign = self._dw_callsign.split('-')[0] + f'-{ssid}'
        if 'modem' in cmd:
            self._dw_modem = int(cmd['modem'])
        if 'kiss_port' in cmd:
            self._dw_kiss_port = int(cmd['kiss_port'])

        print(f"  [Link] AIOCPlugin: mode {self._mode} -> {new_mode}", flush=True)

        # Set mode FIRST to prevent get_audio() race — it checks _mode
        # before reading _in_stream.  Without this, the main loop sees
        # _mode='audio' + _in_stream=None and tries to reopen, crashing.
        self._mode = new_mode

        if new_mode == 'data':
            # Close streams AND terminate PyAudio — Direwolf needs exclusive ALSA access.
            # Just closing streams isn't enough; PyAudio holds ALSA handles until terminated.
            for _sa in ('_in_stream', '_out_stream'):
                _s = getattr(self, _sa, None)
                if _s:
                    try:
                        _s.stop_stream()
                        _s.close()
                    except Exception:
                        pass
                    setattr(self, _sa, None)
            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
            time.sleep(1.0)
            # Start Direwolf
            ok = self._start_direwolf()
            if not ok:
                # Reopen audio on failure
                self.reopen_audio()
                return {"ok": False, "error": "failed to start direwolf"}
        else:
            # Stop Direwolf, reopen PyAudio
            self._stop_direwolf()
            time.sleep(0.5)
            self.reopen_audio()

        return {"ok": True, "mode": new_mode}

    def _start_direwolf(self):
        """Start Direwolf as a subprocess reading directly from AIOC."""
        import subprocess as _sp, signal as _sig
        # Find AIOC ALSA device
        aioc_dev = self._aioc_hw
        if not aioc_dev:
            try:
                with open('/proc/asound/cards') as f:
                    for line in f:
                        if 'AllInOneCable' in line or 'All-In-One' in line:
                            aioc_dev = f'plughw:{line.strip().split()[0]},0'
                            break
            except Exception:
                pass
        if not aioc_dev:
            print("  [Link] AIOCPlugin: AIOC device not found for Direwolf", flush=True)
            return False
        if not aioc_dev.startswith('plughw:'):
            aioc_dev = f'plughw:{aioc_dev.replace("hw:", "")}'

        # Auto-detect AIOC HID for CM108 PTT (VID 1209 = AIOC)
        _ptt_line = ''
        try:
            import os as _os
            for hid in sorted(_os.listdir('/sys/class/hidraw')):
                uevent = f'/sys/class/hidraw/{hid}/device/uevent'
                if _os.path.exists(uevent):
                    with open(uevent) as _f:
                        if '00001209' in _f.read():
                            _hid_path = f'/dev/{hid}'
                            _gpio = self._ptt_channel  # AIOC channel maps directly to CM108 GPIO
                            _ptt_line = f'PTT CM108 {_hid_path} {_gpio}\n'
                            print(f'  [Link] Direwolf PTT: CM108 {_hid_path} GPIO {_gpio}', flush=True)
                            break
        except Exception:
            pass

        # Generate config — TX audio + PTT + AGW for Winlink connected mode
        conf = (
            f"ADEVICE {aioc_dev} {aioc_dev}\n"
            f"ARATE 48000\n"
            f"ACHANNELS 1\n\n"
            f"CHANNEL 0\n"
            f"MYCALL {self._dw_callsign}\n"
            f"MODEM {self._dw_modem}\n\n"
            f"{_ptt_line}"
            f"FIX_BITS 1\n\n"
            f"KISSPORT {self._dw_kiss_port}\n"
            f"AGWPORT 8010\n\n"
            f"DIGIPEAT 0 0 ^WIDE[3-7]-[1-7]$|^TEST$ ^WIDE[12]-[12]$\n"
        )
        try:
            with open(self._direwolf_conf_path, 'w') as f:
                f.write(conf)
        except Exception as e:
            print(f"  [Link] AIOCPlugin: config write error: {e}", flush=True)
            return False

        # Spawn Direwolf
        try:
            self._direwolf_proc = _sp.Popen(
                [self._direwolf_path, '-c', self._direwolf_conf_path, '-t', '0'],
                stdout=_sp.PIPE, stderr=_sp.STDOUT, bufsize=0,
            )
            print(f"  [Link] AIOCPlugin: Direwolf started (PID {self._direwolf_proc.pid}, "
                  f"KISS port {self._dw_kiss_port})", flush=True)
            # Start log reader thread
            threading.Thread(target=self._direwolf_log_reader, daemon=True,
                             name="DirewolfLog").start()
            return True
        except Exception as e:
            print(f"  [Link] AIOCPlugin: Direwolf start error: {e}", flush=True)
            return False

    def _stop_direwolf(self):
        """Stop Direwolf subprocess if running."""
        import signal as _sig
        if self._direwolf_proc:
            try:
                self._direwolf_proc.send_signal(_sig.SIGTERM)
                self._direwolf_proc.wait(timeout=5)
            except Exception:
                try:
                    self._direwolf_proc.kill()
                    self._direwolf_proc.wait(timeout=2)
                except Exception:
                    pass
            print(f"  [Link] AIOCPlugin: Direwolf stopped", flush=True)
            self._direwolf_proc = None

    def _direwolf_log_reader(self):
        """Read Direwolf stdout, print locally, and forward to gateway."""
        proc = self._direwolf_proc
        if not proc or not proc.stdout:
            return
        try:
            for line in iter(proc.stdout.readline, b''):
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').rstrip()
                if text:
                    print(f"  [Direwolf] {text}", flush=True)
                    # Forward to gateway via link protocol
                    client = getattr(self, '_link_client', None)
                    if client and client.connected:
                        try:
                            client.send_status({"type": "direwolf_log", "line": text})
                        except Exception:
                            pass
        except Exception:
            pass

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
