#!/usr/bin/env python3
"""
Gateway Link — protocol, server, and client for remote radio endpoints.

This module is fully self-contained: ZERO imports from other gateway modules.
The endpoint script can import it standalone on a remote machine.

Frame format: [1 byte type][2 byte big-endian length][payload]

Dependencies: stdlib only (+ pyaudio inside AudioPlugin.setup only)
"""

import math
import os
import socket
import struct
import json
import threading
import time
import logging

log = logging.getLogger("GatewayLink")

# ---------------------------------------------------------------------------
# PCM helpers
# ---------------------------------------------------------------------------
# This module ships to every endpoint, some of them minimal installs, so it
# has deliberately carried no third-party imports. numpy is therefore
# OPTIONAL: when present these run vectorised, otherwise they fall back to
# the original stdlib loops. Both paths produce byte-identical output.
#
# It matters because the audio thread ran three per-sample Python loops on
# every 50 ms chunk (diagnostic RMS, gate RMS, and the gain multiply) — real
# sustained CPU on a CM5/Pi that is also running direwolf.
try:
    import numpy as _np
except Exception:                                    # pragma: no cover
    _np = None


def ws_mask(data, mask_key):
    """XOR *data* with the 4-byte WebSocket *mask_key* (masking == unmasking).

    One big-int XOR instead of a per-byte generator: ~40x faster on a
    4800-byte audio frame (0.012 ms vs 0.48 ms), with no numpy needed, so it
    also runs on endpoints that ship this module standalone.
    """
    n = len(data)
    if not n:
        return b''
    key = (bytes(mask_key) * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(key, 'big')).to_bytes(n, 'big')


# ---------------------------------------------------------------------------
# WebSocket framing (RFC 6455), shared by the gateway's browser and link
# handlers (web_routes_stream.py, web_server.py) and by WebSocketTransport
# below. It lives here, not in its own module, because endpoints ship this
# file on its own.
# ---------------------------------------------------------------------------

WS_OP_CONT, WS_OP_TEXT, WS_OP_BINARY = 0x0, 0x1, 0x2
WS_OP_CLOSE, WS_OP_PING, WS_OP_PONG = 0x8, 0x9, 0xA

# Largest message accepted. Audio frames are a few KB; without a cap one
# 64-bit length header made the reader try to buffer that many bytes.
WS_MAX_PAYLOAD = 1 << 20

# A socket timeout while a frame is half read is retried this many times
# before the connection is treated as dead. Between messages a timeout is
# just idleness and goes to the caller.
_WS_MID_FRAME_TIMEOUTS = 2


class WsClosed(Exception):
    """The peer closed the connection, or broke the protocol."""


def ws_encode(opcode, payload=b'', mask=False):
    """One complete (FIN) frame. Clients must mask; servers must not."""
    n = len(payload)
    b0 = 0x80 | opcode
    mbit = 0x80 if mask else 0
    if n < 126:
        hdr = bytes((b0, mbit | n))
    elif n < 65536:
        hdr = bytes((b0, mbit | 126)) + n.to_bytes(2, 'big')
    else:
        hdr = bytes((b0, mbit | 127)) + n.to_bytes(8, 'big')
    if mask:
        key = os.urandom(4)
        return hdr + key + ws_mask(payload, key)
    return hdr + bytes(payload)


def _ws_recv_exact(sock, n, idle=False):
    """Exactly n bytes. With idle=True a timeout before the first byte is
    re-raised (nothing is lost); any other short read raises WsClosed."""
    buf = bytearray()
    timeouts = 0
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            if idle and not buf:
                raise
            timeouts += 1
            if timeouts > _WS_MID_FRAME_TIMEOUTS:
                raise WsClosed('timed out mid-frame')
            continue
        except OSError as e:
            raise WsClosed(str(e)) from e
        if not chunk:
            raise WsClosed('peer closed')
        buf += chunk
    return bytes(buf)


def ws_read_frame(sock, max_payload=WS_MAX_PAYLOAD, idle=True):
    """(fin, opcode, unmasked payload) for the next frame."""
    hdr = _ws_recv_exact(sock, 2, idle=idle)
    fin = bool(hdr[0] & 0x80)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    n = hdr[1] & 0x7F
    if n == 126:
        n = int.from_bytes(_ws_recv_exact(sock, 2), 'big')
    elif n == 127:
        n = int.from_bytes(_ws_recv_exact(sock, 8), 'big')
    if n > max_payload:
        raise WsClosed(f'{n}-byte frame exceeds {max_payload}')
    key = _ws_recv_exact(sock, 4) if masked else None
    data = _ws_recv_exact(sock, n) if n else b''
    if key:
        data = ws_mask(data, key)
    return fin, opcode, data


def ws_read_message(sock, max_payload=WS_MAX_PAYLOAD, mask_replies=False):
    """Next data message as (opcode, payload), or None once the peer closes.

    Answers pings and close frames itself (masked when mask_replies, i.e. on
    the client side) and reassembles fragmented messages. socket.timeout is
    raised only when idle between messages; every other failure, including
    a malformed or oversize frame, raises WsClosed.
    """
    parts = []
    first_op = None
    total = 0
    mid_timeouts = 0
    while True:
        try:
            fin, op, data = ws_read_frame(sock, max_payload)
        except socket.timeout:
            if first_op is None:
                raise
            mid_timeouts += 1     # idle between fragments of one message
            if mid_timeouts > _WS_MID_FRAME_TIMEOUTS:
                raise WsClosed('timed out mid-message')
            continue
        if op == WS_OP_CLOSE:
            try:
                sock.sendall(ws_encode(WS_OP_CLOSE, data[:2], mask=mask_replies))
            except OSError:
                pass
            return None
        if op == WS_OP_PING:
            try:
                sock.sendall(ws_encode(WS_OP_PONG, data, mask=mask_replies))
            except OSError as e:
                raise WsClosed(str(e)) from e
            continue
        if op == WS_OP_PONG:
            continue
        if op == WS_OP_CONT:
            if first_op is None:
                raise WsClosed('continuation frame with no message to continue')
        elif op in (WS_OP_TEXT, WS_OP_BINARY):
            if first_op is not None:
                raise WsClosed('new message started inside a fragmented one')
            first_op = op
        else:
            raise WsClosed(f'unknown opcode 0x{op:x}')
        total += len(data)
        if total > max_payload:
            raise WsClosed(f'{total}-byte message exceeds {max_payload}')
        parts.append(data)
        if fin:
            return first_op, b''.join(parts)


def pcm_rms(data):
    """RMS of signed 16-bit little-endian PCM. 0.0 for an empty buffer."""
    n = len(data) // 2
    if n == 0:
        return 0.0
    if _np is not None:
        arr = _np.frombuffer(data, dtype='<i2', count=n).astype(_np.float32)
        return float(_np.sqrt(_np.mean(arr * arr)))
    samples = struct.unpack(f'<{n}h', data)
    return math.sqrt(sum(s * s for s in samples) / n)


def pcm_db(data):
    """dBFS of int16 PCM, floored at -100 dB for silence."""
    rms = pcm_rms(data)
    return 20.0 * math.log10(rms / 32767.0) if rms > 0 else -100.0


def pcm_apply_gain(pcm, gain):
    """Multiply int16 PCM by *gain*.

    This is the endpoint's own RX/TX gain knob (AudioPlugin._write_output
    et al) — the last gain stage before hardware playback, downstream of
    any gateway-side mixer gain, so nothing upstream can rescue a clipped
    sample here. Above unity, soft-clips via tanh (same shape as the
    gateway mixer's audio_util.apply_gain — this module ships standalone
    to remote endpoints so can't import that directly) so pushing the
    slider past 0 dB rolls peaks off smoothly instead of flat-topping into
    square-wave harmonics. At or below unity it's a plain multiply, same
    as before.
    """
    if gain == 1.0:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return pcm
    if _np is not None:
        arr = _np.frombuffer(pcm, dtype='<i2', count=n).astype(_np.float32)
        if gain > 1.0:
            out = _np.tanh(arr / 32768.0 * gain) * 32768.0
        else:
            out = arr * gain
        return _np.clip(out, -32768, 32767).astype('<i2').tobytes()
    samples = struct.unpack(f'<{n}h', pcm)
    gained = []
    for s in samples:
        if gain > 1.0:
            v = int(math.tanh(s / 32768.0 * gain) * 32768.0)
        else:
            v = int(s * gain)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        gained.append(v)
    return struct.pack(f'<{n}h', *gained)


# ---------------------------------------------------------------------------
# Commands whose successful ACKs are worth logging — usually one-shot
# user actions (mode change, PTT toggle, memory ops). High-volume routine
# commands (knob chase: freq/squelch/vol/af_level/mic_gain/power) only log
# when they FAILED, so the log stays readable during normal GUI use.
# Failures of any command are always logged. Mirrors _LOUD_OK_CMDS in
# tools/link_endpoint.py so endpoint + gateway emit a consistent picture.
_LOUD_OK_CMDS = {
    'mode', 'reconnect', 'memory_write', 'memory_clear', 'memory_to_vfo',
    'call_channel', 'vfo', 'vfo_swap', 'vfo_equalize',
    'tx_interlock', 'ptt', 'cat',
}


# Protocol
# ---------------------------------------------------------------------------

class GatewayLinkProtocol:
    """Wire protocol for Gateway Link: framed messages over TCP."""

    AUDIO    = 0x01
    COMMAND  = 0x02
    STATUS   = 0x03
    REGISTER = 0x04
    ACK      = 0x05
    LOG      = 0x06
    CHALLENGE = 0x07  # server -> endpoint, before REGISTER, only with LINK_SECRET

    _HEADER = struct.Struct('>BH')  # type (1) + length (2) = 3 bytes
    MAX_PAYLOAD = 0xFFFF            # the 16-bit length field's limit

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
        if len(payload) > cls.MAX_PAYLOAD:
            raise ValueError(f"link frame payload {len(payload)} bytes exceeds "
                             f"{cls.MAX_PAYLOAD}; split it")
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

    @classmethod
    def send_log(cls, sock, lines):
        """Send a batch of log lines (endpoint → server).

        *lines* is a list of dicts ``{'ts': float, 'stream': 'stdout'|'stderr',
        'text': str}``. The server appends them to the per-endpoint rotating
        log file under ``logs/endpoints/``. See ``docs/endpoint_logs_design.md``.
        """
        payload = {'type': 'log', 'lines': lines}
        cls.send_frame(sock, cls.LOG, json.dumps(payload).encode('utf-8'))


# ---------------------------------------------------------------------------
# Link authentication
# ---------------------------------------------------------------------------
# The gateway holds a master secret (LINK_SECRET, or one it generates and
# keeps in ~/.config/radio-gateway/link_secret). Each endpoint gets its own
# key, derived from the master and its name (endpoint_key), so the key one
# endpoint holds can't be used to pose as another.
#
# The server opens every connection with a CHALLENGE frame carrying a fresh
# nonce. An endpoint with a key answers in REGISTER with
# auth = HMAC-SHA256(key, "radio-gateway-link-v1|<nonce>|<name>"). The nonce
# is per connection, so a captured REGISTER can't be replayed, and no clock
# sync is needed (Pis without an RTC boot with the wrong time).
#
# Keys are delivered over the link itself, trust on first use: an endpoint
# name the gateway has not enrolled yet may REGISTER without auth; the
# gateway then sends it {"cmd": "link_key_set", "key": ...}, the endpoint
# stores it and ACKs, and from then on that name must authenticate. An
# endpoint that couldn't store its key durably ACKs ok=false and stays
# unenrolled, so a reboot that loses the key can't lock it out.
# LINK_ENROLL_NEW = false stops enrolling new names.
#
# The endpoint's key also verifies its signed code updates.

LINK_SECRET_ENV = 'RADIO_GATEWAY_LINK_SECRET'
LINK_SECRET_FILE = 'link_secret'
BUNDLE_SIG_HEADER = 'X-Bundle-Signature'
# Endpoint name on an update request, so the gateway signs with its key.
# A header rather than ?name= because older gateways route the path exactly.
ENDPOINT_NAME_HEADER = 'X-Endpoint-Name'


def load_link_secret(directory=None):
    """The endpoint-side secret: env var first, then <directory>/link_secret.
    Returns '' when neither is set."""
    secret = os.environ.get(LINK_SECRET_ENV, '').strip()
    if secret or not directory:
        return secret
    try:
        with open(os.path.join(directory, LINK_SECRET_FILE)) as f:
            return f.read().strip()
    except OSError:
        return ''


def endpoint_key(master, name):
    """The per-endpoint key for *name* under the gateway's *master* secret."""
    import hmac
    import hashlib
    return hmac.new(master.encode('utf-8'),
                    f"radio-gateway-endpoint-key-v1|{name}".encode('utf-8'),
                    hashlib.sha256).hexdigest()


def load_or_create_master_secret(path):
    """Read the gateway's generated master secret from *path*, creating it
    (0600) on first use. Used when LINK_SECRET is blank."""
    try:
        with open(path) as f:
            secret = f.read().strip()
        if secret:
            return secret
    except OSError:
        pass
    import secrets as _secrets
    secret = _secrets.token_hex(32)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secret + '\n')
    os.replace(path + '.tmp', path)
    return secret


class LinkEnrollment:
    """Endpoint names that have stored their key, persisted as JSON.

    The file records a fingerprint of the master secret; if the master
    changes, every endpoint's key is invalid, so the list starts empty and
    each endpoint re-enrolls instead of being refused forever."""

    def __init__(self, path, master):
        import hashlib
        self._path = path
        self._fp = hashlib.sha256(('fp|' + master).encode('utf-8')).hexdigest()[:16]
        self._lock = threading.Lock()
        self._names = set()
        if not path:
            return   # in memory only
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get('fingerprint') == self._fp:
                self._names = set(d.get('names') or [])
        except (OSError, ValueError, AttributeError):
            pass

    def __contains__(self, name):
        with self._lock:
            return name in self._names

    def add(self, name):
        with self._lock:
            if name in self._names:
                return
            self._names.add(name)
            data = {'fingerprint': self._fp, 'names': sorted(self._names)}
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or '.', exist_ok=True)
            with open(self._path + '.tmp', 'w') as f:
                json.dump(data, f)
            os.replace(self._path + '.tmp', self._path)
        except OSError as e:
            print(f"  [Link] Could not save enrolled endpoints: {e}")

    def names(self):
        with self._lock:
            return sorted(self._names)


def link_register_mac(secret, nonce, name):
    """HMAC a REGISTER for *name* answers the server's *nonce* with."""
    import hmac
    import hashlib
    msg = f"radio-gateway-link-v1|{nonce}|{name}".encode('utf-8')
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()


def bundle_signature(secret, files):
    """HMAC over a code-update bundle. *files* is {name: bytes}. Each file
    is hashed, so the signature covers names and contents."""
    import hmac
    import hashlib
    parts = ['radio-gateway-bundle-v1']
    for name in sorted(files):
        parts.append(f"{name}:{hashlib.sha256(files[name]).hexdigest()}")
    return hmac.new(secret.encode('utf-8'), '\n'.join(parts).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def verify_bundle(secret, files, signature):
    """True if *signature* (hex, from BUNDLE_SIG_HEADER) matches *files*."""
    import hmac
    if not signature:
        return False
    return hmac.compare_digest(bundle_signature(secret, files),
                               signature.strip().lower())


# ---------------------------------------------------------------------------
# Server (master gateway side)
# ---------------------------------------------------------------------------

class _EndpointConn:
    """State for one connected endpoint."""
    __slots__ = ('name', 'sock', 'send_lock', 'reader_thread', 'info',
                 'capabilities', 'last_heartbeat', 'audio_sink', 'addr',
                 'via_tunnel', 'ping_ms', '_ping_sent',
                 '_send_queue', '_sender_thread', '_sender_running',
                 'authenticated', 'key_pending')

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
        self.ping_ms = -1       # last measured round-trip time (-1 = no data)
        self._ping_sent = 0.0   # monotonic time when last ping was sent
        self.authenticated = False  # REGISTER carried a valid HMAC
        self.key_pending = False    # link_key_set sent, waiting for its ACK
        # Async send queue — audio frames queued here, sender thread drains
        import queue as _q
        self._send_queue = _q.Queue(maxsize=20)  # ~1s buffer at 50ms chunks
        self._sender_running = True
        self._sender_thread = threading.Thread(
            target=self._sender_loop, daemon=True, name=f"LinkSend-{name}")
        self._sender_thread.start()

    def _sender_loop(self):
        """Drain send queue to socket (dedicated thread, never blocks tick)."""
        import queue as _q
        while self._sender_running:
            try:
                frame_data = self._send_queue.get(timeout=1.0)
            except _q.Empty:
                continue
            if frame_data is None:
                break
            with self.send_lock:
                try:
                    self.sock.sendall(frame_data)
                except (OSError, ConnectionError):
                    break

    def queue_frame(self, frame_data, evict=False):
        """Queue a frame for the sender thread. Never blocks the caller.

        evict=False (audio): a full queue drops the NEW frame — losing
        50ms of audio is fine. evict=True (control): displace the oldest
        queued frame instead — the queue only fills when the socket is
        already stalled, and dropping a PTT-off there means a stuck
        transmitter.
        """
        import queue as _q
        if not evict:
            try:
                self._send_queue.put_nowait(frame_data)
            except Exception:
                pass
            return
        for _ in range(self._send_queue.maxsize + 1):
            try:
                self._send_queue.put_nowait(frame_data)
                return
            except _q.Full:
                try:
                    self._send_queue.get_nowait()  # evict oldest frame
                except _q.Empty:
                    pass

    def stop_sender(self):
        """Stop the sender thread."""
        self._sender_running = False
        try:
            self._send_queue.put_nowait(None)
        except Exception:
            pass


def _set_keepalive(sock, idle=8, interval=4, count=3):
    """Enable TCP keepalives + user timeout so dead connections are detected in ~idle+interval*count seconds."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
        # TCP_USER_TIMEOUT: kill connection after this many ms of unacked data.
        # Prevents retransmit storm (minutes) when source interface disappears.
        # 30s is long enough to survive brief WiFi congestion but short enough to
        # avoid spinning for minutes when an interface is pulled.
        _TCP_USER_TIMEOUT = 18  # Linux socket option number
        sock.setsockopt(socket.IPPROTO_TCP, _TCP_USER_TIMEOUT, 30000)
    except (AttributeError, OSError):
        pass  # not supported on all platforms


class PttTimeOutTimer:
    """Transmit time-out timer (TOT) for one or more keyed radios.

    allow_key(key) is called on every key-on request. The first one arms a
    timer; when it expires, on_expire(key) must unkey the radio. After that
    the key stays locked out while key-on requests keep arriving (a bus that
    is still pushing audio re-requests every tick) and unlocks once they
    have paused for `relock_gap` seconds -- a fresh press, not a held one.
    released(key) is called on every key-off and clears both timer and lock.
    A limit of 0 disables the timer entirely.
    """

    def __init__(self, limit, on_expire, relock_gap=5.0):
        try:
            self.limit = float(limit or 0)
        except (TypeError, ValueError):
            self.limit = 0.0
        self._on_expire = on_expire
        self._relock_gap = relock_gap
        self._timers = {}       # key -> threading.Timer
        self._locked = {}       # key -> monotonic time of the last refused key-on
        self._lock = threading.Lock()

    def allow_key(self, key):
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._locked.get(key)
            if last is not None:
                if now - last < self._relock_gap:
                    self._locked[key] = now
                    return False
                del self._locked[key]
            timer = self._timers.get(key)
            if timer is None or not timer.is_alive():
                timer = threading.Timer(self.limit, self._expired, args=(key,))
                timer.daemon = True
                timer.name = f'PttTOT-{key}'
                self._timers[key] = timer
                timer.start()
        return True

    def released(self, key):
        with self._lock:
            timer = self._timers.pop(key, None)
            self._locked.pop(key, None)
        if timer:
            timer.cancel()

    def _expired(self, key):
        with self._lock:
            if self._timers.get(key) is not threading.current_thread():
                return  # released or re-armed meanwhile
            self._timers.pop(key, None)
            self._locked[key] = time.monotonic()
        self._on_expire(key)


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
                 on_endpoint_status=None, on_log_lines=None,
                 supervisor=None, ptt_timeout=0, on_ptt_timeout=None,
                 bind='', secret='', enroll_new=True, enroll_path=None):
        self._port = port
        self._bind = bind or ''
        # Master secret (see "Link authentication"). Blank turns challenge
        # and enrollment off: any client may REGISTER (pre-5.1 behaviour).
        self._secret = secret or ''
        self._enroll_new = bool(enroll_new)
        self._enrolled = LinkEnrollment(enroll_path, self._secret) if self._secret else None
        self._on_command = on_command
        self._on_register = on_register
        self._on_disconnect = on_disconnect
        self._on_ack = on_ack
        self._on_endpoint_status = on_endpoint_status
        # on_log_lines(endpoint_name: str, lines: list[dict]) — invoked from
        # the reader thread when an endpoint ships a P.LOG batch. See
        # docs/endpoint_logs_design.md.
        self._on_log_lines = on_log_lines
        self._supervisor = supervisor

        self._server_sock = None
        self._stop = threading.Event()
        self._start_time = time.monotonic()
        self._DEAD_PEER_TIMEOUT = 90.0     # seconds without any frame before declaring dead
        self._REGISTER_TIMEOUT = 10.0      # seconds to wait for REGISTER after connect

        # dict keyed by endpoint name -> _EndpointConn
        self._endpoints = {}
        self._endpoints_lock = threading.RLock()

        self._accept_thread = None
        self._heartbeat_thread = None

        # Pending-ACK correlation for send_command_to_and_wait().
        # cmd_id is monotonically increasing per server instance; the endpoint
        # echoes it back inside the ACK frame and we fulfill the matching Event.
        # Older endpoints that don't echo cmd_id simply time out the waiter,
        # which then falls through to the fire-and-forget behaviour.
        import itertools as _it
        self._cmd_id_counter = _it.count(1)
        self._pending_acks = {}      # cmd_id -> (threading.Event, result_holder)
        self._pending_acks_lock = threading.Lock()

        # Transmit time-out timer for every endpoint-hosted radio. Every PTT
        # command to an endpoint (web toggles, MCP tools, bus auto-PTT) goes
        # through send_command_to, so one timer per endpoint here covers all
        # of them. on_ptt_timeout(name) lets the gateway clear its own PTT
        # bookkeeping after a forced unkey. ptt_timeout=0 disables.
        self._on_ptt_timeout = on_ptt_timeout
        self._ptt_tot = PttTimeOutTimer(ptt_timeout, self._ptt_timeout_fired)

    # -- public API ---------------------------------------------------------

    def key_for(self, name):
        """*name*'s endpoint key, or '' when link auth is off."""
        return endpoint_key(self._secret, name) if self._secret else ''

    def is_enrolled(self, name):
        return bool(self._enrolled is not None and name in self._enrolled)

    def start(self):
        """Bind, listen, and start accept + heartbeat threads."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind((self._bind, self._port))
        self._server_sock.listen(8)
        print(f"  [Link] Server listening on {self._bind or '*'}:{self._port}")
        if not self._secret:
            print("  [Link] WARNING: link auth is off — any host that can "
                  "reach this port can register as any endpoint")
        elif self._enroll_new:
            print(f"  [Link] Endpoint auth on; new endpoint names enroll on first "
                  f"connect (enrolled: {', '.join(self._enrolled.names()) or 'none yet'})")
        else:
            print("  [Link] Endpoint auth on; enrolling new names is off "
                  "(LINK_ENROLL_NEW = false)")

        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="LinkAccept", daemon=True)
        self._accept_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  name="LinkHeartbeat", daemon=True)
        self._heartbeat_thread.start()

        # Publish mDNS service for auto-discovery via the gateway's
        # ProcessSupervisor (attached after init by setup_gateway_link).
        if self._supervisor is not None:
            try:
                self._supervisor.add(
                    'mdns-radiogateway',
                    ['avahi-publish-service', 'RadioGateway',
                     '_radiogateway._tcp', str(self._port)],
                    restart=True, backoff=(5, 60),
                )
                print(f"  [Link] mDNS: published _radiogateway._tcp on port {self._port}")
            except ValueError:
                pass  # already registered (server restarted)
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
        # mDNS publisher is owned by ProcessSupervisor; gateway shutdown
        # reaps it via supervisor.shutdown_all().
        if self._accept_thread:
            self._accept_thread.join(timeout=3)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=3)
        print("  [Link] Server stopped")

    def send_audio_to_all(self, pcm, exclude=None):
        """Send PCM audio to all connected endpoints (async, never blocks).

        *exclude* is an optional set of endpoint names to skip.
        """
        header = GatewayLinkProtocol._HEADER.pack(GatewayLinkProtocol.AUDIO, len(pcm))
        frame_data = header + pcm
        with self._endpoints_lock:
            snapshot = list(self._endpoints.values())
        for ep in snapshot:
            if exclude and ep.name in exclude:
                continue
            try:
                ep._send_queue.put_nowait(frame_data)
            except Exception:
                pass  # queue full — drop

    def send_audio_to(self, name, pcm):
        """Send PCM audio to a specific endpoint by name."""
        self._send_to(name, GatewayLinkProtocol.AUDIO, pcm)

    def send_command_to(self, name, cmd):
        """Send a command dict to a specific endpoint by name (fire-and-forget)."""
        if isinstance(cmd, dict) and cmd.get('cmd') == 'ptt' and 'state' in cmd:
            if cmd.get('state'):
                if not self._ptt_tot.allow_key(name):
                    return  # timed out and still being held: stay unkeyed
            else:
                self._ptt_tot.released(name)
        self._send_to(name, GatewayLinkProtocol.COMMAND,
                      json.dumps(cmd).encode('utf-8'))

    def _ptt_timeout_fired(self, name):
        print(f"  [Link] {name}: PTT time-out ({self._ptt_tot.limit:g}s) -- forcing unkey")
        try:
            self._send_to(name, GatewayLinkProtocol.COMMAND,
                          json.dumps({'cmd': 'ptt', 'state': False}).encode('utf-8'))
        except Exception as e:
            print(f"  [Link] {name}: time-out unkey failed: {e}")
        if self._on_ptt_timeout:
            try:
                self._on_ptt_timeout(name)
            except Exception as e:
                print(f"  [Link] {name}: on_ptt_timeout callback error: {e}")

    def send_command_to_and_wait(self, name, cmd, timeout=5.0):
        """Send a command and wait up to *timeout* seconds for the endpoint's
        ACK. Returns the endpoint's result dict, or a {'ok': False, 'error':
        ...} dict on timeout / unknown endpoint. The cmd is decorated with a
        unique _cmd_id; the endpoint must echo it in the ACK for correlation.
        Endpoints that don't echo cmd_id will simply time out — the caller
        gets {'ok': False, 'error': 'timeout'} and the command still went.

        Default timeout is 5 s because the CIVController can hold its serial
        lock for several seconds at a stretch (settings poll across ~16 CI-V
        reads, each up to 1 s on a no-response opcode). Commands queue behind
        the poll and need that headroom; 2 s defaulted falsely-timed-out under
        normal load. Callers can override for known-fast commands."""
        cmd_id = next(self._cmd_id_counter)
        cmd = dict(cmd)
        cmd['_cmd_id'] = cmd_id
        ev = threading.Event()
        holder = [None]
        with self._pending_acks_lock:
            self._pending_acks[cmd_id] = (ev, holder)
        try:
            self.send_command_to(name, cmd)
            if ev.wait(timeout):
                return holder[0] if holder[0] is not None else {'ok': False, 'error': 'empty ACK'}
            return {'ok': False, 'error': f'no ACK from {name} within {timeout}s'}
        finally:
            with self._pending_acks_lock:
                self._pending_acks.pop(cmd_id, None)

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
            info['ping_ms'] = ep.ping_ms
            info['enrolled'] = self.is_enrolled(name)
            return info

    # -- internal -----------------------------------------------------------

    def _send_to(self, name, frame_type, payload):
        """Thread-safe send to a specific endpoint by name.

        ALL frames go through the per-endpoint send queue so the caller
        never blocks. Control frames used to be sent inline with a blocking
        sendall — called from the BusManager tick thread for auto-PTT, a
        wedged endpoint TCP connection could stall every bus for up to the
        30s TCP_USER_TIMEOUT. When the queue is full (which means the
        socket is already stalled), control frames evict the oldest queued
        frame instead of being dropped: losing 50ms of audio is fine,
        losing a PTT-off is a stuck transmitter.
        """
        with self._endpoints_lock:
            ep = self._endpoints.get(name)
        if ep is None:
            return
        if len(payload) > GatewayLinkProtocol.MAX_PAYLOAD:
            print(f"  [Link] {name}: dropped {len(payload)}-byte frame (type {frame_type}); "
                  f"link frames are limited to {GatewayLinkProtocol.MAX_PAYLOAD} bytes")
            return
        header = GatewayLinkProtocol._HEADER.pack(frame_type, len(payload))
        frame_data = header + payload
        ep.queue_frame(frame_data,
                       evict=(frame_type != GatewayLinkProtocol.AUDIO))

    def _remove_endpoint(self, name, reason=""):
        """Remove an endpoint from the dict, close socket, notify callback."""
        with self._endpoints_lock:
            ep = self._endpoints.pop(name, None)
        self._ptt_tot.released(name)
        if ep:
            ep.stop_sender()
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
            # --- Challenge, then wait for REGISTER as first frame ---
            sock.settimeout(self._REGISTER_TIMEOUT)
            nonce = None
            if self._secret:
                nonce = os.urandom(16).hex()
                P.send_frame(sock, P.CHALLENGE,
                             json.dumps({'nonce': nonce, 'v': 1}).encode('utf-8'))
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

            sock.settimeout(120.0)  # must exceed DEAD_PEER_TIMEOUT (90s) so dead-peer check fires first
            _set_keepalive(sock)

            import datetime as _dt
            _conn_ts = _dt.datetime.now().strftime('%H:%M:%S')

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

            # Authenticate before anything else, so a client without its
            # key can't evict the real endpoint below.
            _authed = False
            _enrolling = False
            if nonce is not None:
                import hmac as _hmac
                _mac = str(info.get('auth') or '')
                _want = link_register_mac(self.key_for(ep_name), nonce, ep_name)
                if _mac and _hmac.compare_digest(_mac, _want):
                    _authed = True
                elif ep_name not in self._enrolled and self._enroll_new:
                    # Trust on first use. A wrong key for a name we never
                    # enrolled (e.g. the master secret changed) re-enrolls too.
                    _enrolling = True
                else:
                    _why = ('enrolled name, ' + ('bad' if _mac else 'no') + ' key'
                            if ep_name in self._enrolled
                            else 'not enrolled and LINK_ENROLL_NEW is off')
                    print(f"  [Link] {addr[0]}:{addr[1]} REGISTER as '{ep_name}' "
                          f"refused: {_why}")
                    try:
                        P.send_frame(sock, P.COMMAND, json.dumps(
                            {"error": f"link auth failed ({_why})"}).encode('utf-8'))
                    except (OSError, ConnectionError):
                        pass
                    ep_name = None  # nothing registered; finally has nothing to remove
                    return

            # Check for duplicate name — evict the stale entry and accept the new one
            with self._endpoints_lock:
                if ep_name in self._endpoints:
                    old = self._endpoints[ep_name]
                    print(f"  [Link] Endpoint '{ep_name}' reconnected from "
                          f"{addr[0]}:{addr[1]}, replacing stale connection")
                    # Stop the old sender thread too — closing only the socket
                    # left it alive polling an orphaned queue at 1 Hz forever
                    # (one leaked LinkSend thread per reconnect-with-same-name).
                    old.stop_sender()
                    try:
                        old.sock.close()
                    except OSError:
                        pass

                # Build endpoint and store
                ep = _EndpointConn(ep_name, sock, addr)
                info.pop('auth', None)
                ep.info = info
                ep.authenticated = _authed
                ep.key_pending = _enrolling
                ep.reader_thread = threading.current_thread()
                caps = info.get('capabilities', {})
                ep.capabilities = caps if isinstance(caps, dict) else {}
                self._endpoints[ep_name] = ep

            enabled = [k for k, v in ep.capabilities.items() if v]
            print(f"  [{_conn_ts}] [Link] Endpoint registered: {ep_name} "
                  f"from={addr[0]}:{addr[1]} "
                  f"plugin={info.get('plugin', '?')} "
                  f"caps={enabled}")

            if _authed:
                self._enrolled.add(ep_name)
            elif _enrolling:
                print(f"  [Link] {ep_name}: not enrolled yet — sending its key "
                      f"(trust on first use)")
                self.send_command_to(ep_name, {'cmd': 'link_key_set',
                                               'key': self.key_for(ep_name)})

            # Call on_register — return value is the audio sink for this endpoint
            if self._on_register:
                audio_sink = self._on_register(info)
                ep.audio_sink = audio_sink

            # --- Main frame dispatch loop ---
            _frame_count = 0
            _last_frame_time = time.monotonic()
            while not self._stop.is_set():
                try:
                    result = P.recv_frame(sock)
                except socket.timeout:
                    _silence = time.monotonic() - _last_frame_time
                    _now = _dt.datetime.now().strftime('%H:%M:%S')
                    print(f"  [{_now}] [Link] {ep_name}: DISCONNECT reason=socket_timeout "
                          f"frames={_frame_count} silence={_silence:.1f}s peer={addr[0]}")
                    result = None
                if result is None:
                    _silence = time.monotonic() - _last_frame_time
                    _now = _dt.datetime.now().strftime('%H:%M:%S')
                    print(f"  [{_now}] [Link] {ep_name}: DISCONNECT reason=recv_none "
                          f"frames={_frame_count} silence={_silence:.1f}s peer={addr[0]}")
                    break
                _frame_count += 1
                _last_frame_time = time.monotonic()
                ftype, payload = result
                # Any frame is proof of life
                ep.last_heartbeat = _last_frame_time
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
                        if cmd_name == 'link_key_set' and getattr(ep, 'key_pending', False):
                            ep.key_pending = False
                            if ok:
                                self._enrolled.add(ep_name)
                                print(f"  [Link] {ep_name}: stored its key — enrolled; "
                                      f"it must authenticate from now on")
                            else:
                                print(f"  [Link] {ep_name}: could not store its key "
                                      f"durably ({(ack.get('result') or {}).get('error', '?')}); "
                                      f"left unenrolled so a reboot can't lock it out")
                        # Fulfill any pending send_command_to_and_wait() waiter
                        # before invoking on_ack — the HTTP caller is blocked.
                        _cid = ack.get('_cmd_id')
                        if _cid is not None:
                            with self._pending_acks_lock:
                                _waiter = self._pending_acks.get(_cid)
                            if _waiter is not None:
                                _ev, _holder = _waiter
                                _holder[0] = ack.get('result', {})
                                _ev.set()
                        if cmd_name == 'ping' and ep._ping_sent > 0:
                            ep.ping_ms = round((time.monotonic() - ep._ping_sent) * 1000, 1)
                        elif cmd_name not in ('status', 'ping'):
                            # Suppress per-cmd ACK lines for high-volume
                            # routine commands (knob chase, periodic state)
                            # unless they failed. _LOUD_OK_CMDS get logged
                            # even on success so one-shot user actions
                            # leave a breadcrumb.
                            if (not ok) or cmd_name in _LOUD_OK_CMDS:
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
                    elif ftype == P.LOG:
                        # Endpoint stdout/stderr line batch — append to the
                        # per-endpoint rotating log file. Failure here must
                        # not kill the link reader.
                        if self._on_log_lines:
                            try:
                                msg = json.loads(payload)
                                _lines = msg.get('lines') or []
                                if _lines:
                                    self._on_log_lines(ep_name, _lines)
                            except json.JSONDecodeError as e:
                                print(f"  [Link] Bad JSON in LOG from {ep_name}: {e}")
                            except Exception as e:
                                print(f"  [Link] LOG handler error for {ep_name}: {e}")
                except json.JSONDecodeError as e:
                    print(f"  [Link] Bad JSON from {ep_name}: {e}")
                except Exception as e:
                    print(f"  [Link] Callback error for {ep_name}: {e}")

        except socket.timeout:
            import datetime as _dt
            print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] {addr[0]}:{addr[1]} REGISTER timeout, closing")
        except Exception as e:
            if not self._stop.is_set():
                import traceback
                import datetime as _dt
                print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Reader error for {ep_name or addr}: {e}")
                traceback.print_exc()
        finally:
            # Remove from endpoints dict (only if this reader owns the entry)
            _reader_removed = False
            if ep_name:
                with self._endpoints_lock:
                    existing = self._endpoints.get(ep_name)
                    if existing is not None and existing.sock is sock:
                        del self._endpoints[ep_name]
                        _reader_removed = True
            try:
                sock.close()
            except OSError:
                pass
            if ep_name and _reader_removed:
                import datetime as _dt
                print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Endpoint disconnected: {ep_name} peer={addr[0]}")
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
            _ping_payload = json.dumps({"cmd": "ping"}).encode('utf-8')
            for ep in snapshot:
                # Send heartbeat + ping via the send queue — an inline
                # sendall to one wedged endpoint used to block this loop
                # (and dead-peer detection for every other endpoint) while
                # holding the send_lock the audio sender also needs.
                _hdr = GatewayLinkProtocol._HEADER
                ep.queue_frame(
                    _hdr.pack(GatewayLinkProtocol.STATUS, len(hb_payload)) + hb_payload,
                    evict=True)
                ep._ping_sent = time.monotonic()
                ep.queue_frame(
                    _hdr.pack(GatewayLinkProtocol.COMMAND, len(_ping_payload)) + _ping_payload,
                    evict=True)
                # Dead peer detection
                if ep.last_heartbeat > 0:
                    silence = now - ep.last_heartbeat
                    if silence > self._DEAD_PEER_TIMEOUT:
                        dead.append((ep.name, silence))

            for name, silence in dead:
                print(f"  [Link] Dead peer detected: {name} — {silence:.1f}s silent, closing")
                self._remove_endpoint(name, reason="dead_peer")

            try:
                import metrics as _m
                _dead_names = {n for n, _ in dead}
                for ep in snapshot:
                    _m.link_endpoint_up.labels(endpoint=ep.name).set(
                        0 if ep.name in _dead_names else 1)
            except Exception:
                pass


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

        # Read response (look for 101). One byte at a time so nothing after
        # the blank line is consumed: the gateway's first link frame (the
        # LINK_SECRET CHALLENGE) can follow in the same segment, and a
        # bigger recv here used to throw it away.
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = sock.recv(1)
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
                sock.sendall(ws_encode(WS_OP_CLOSE, b'\x03\xe8', mask=True))  # 1000 normal
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _ws_send(self, data):
        """Send a masked WS binary frame (clients MUST mask)."""
        self._sock.sendall(ws_encode(WS_OP_BINARY, data, mask=True))

    def _ws_recv(self):
        """Receive one WS message. Returns payload bytes or None.

        A socket timeout counts as a disconnect here, as it did before the
        shared framing code.
        """
        sock = self._sock
        if not sock:
            return None
        try:
            msg = ws_read_message(sock, mask_replies=True)
        except (WsClosed, OSError):
            return None
        return msg[1] if msg else None


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
                 ws_url=None, url_resolver=None, secret='', on_link_key=None):
        self._host = host
        self._secret = secret or ''      # this endpoint's key, to answer CHALLENGE
        # on_link_key(key) -> (stored_durably: bool, error: str). Called when
        # the gateway delivers this endpoint's key (trust on first use).
        self._on_link_key = on_link_key
        # Whether the gateway challenged the last connection: True/False,
        # None until a connection with a key has been made.
        self.gateway_challenged = None
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

    def send_ack(self, cmd_name, result_dict, cmd_id=None):
        """Send an ACK frame back to the server with command result.
        If *cmd_id* is provided it is echoed in the ACK so the server can
        match it against a pending send_command_to_and_wait() waiter."""
        payload = {"cmd": cmd_name}
        if cmd_id is not None:
            payload["_cmd_id"] = cmd_id
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
        import datetime
        def _ts():
            return datetime.datetime.now().strftime('%H:%M:%S')

        _backoff = 2.0
        _MAX_BACKOFF = 60.0
        _ws_failures = 0
        _connect_count = 0

        while not self._stop.is_set():
            connected_via = None
            _connect_count += 1

            # ── Attempt 1: Direct TCP ──
            if self._host and self._port:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect((self._host, self._port))
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(120.0)  # generous timeout for lossy WiFi
                    _set_keepalive(sock)
                    _local_addr = sock.getsockname()
                    print(f"  [{_ts()}] [Link] Connected to {self._host}:{self._port} (TCP) "
                          f"local={_local_addr[0]}:{_local_addr[1]} [#{_connect_count}]")
                    with self._send_lock:
                        self._sock = sock
                        self._ws_transport = None
                    connected_via = 'tcp'
                    _backoff = 2.0
                except (OSError, ConnectionError) as e:
                    print(f"  [{_ts()}] [Link] TCP {self._host}:{self._port} failed: {e}")
                    try:
                        sock.close()
                    except Exception:
                        pass

            # ── Attempt 2: WebSocket via tunnel ──
            if not connected_via and self._ws_url:
                ws = WebSocketTransport()
                try:
                    if ws.connect(self._ws_url, timeout=15):
                        print(f"  [{_ts()}] [Link] Connected via WebSocket tunnel [#{_connect_count}]")
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
            if self._secret:
                _nonce = self._await_challenge(connected_via)
                self.gateway_challenged = bool(_nonce)
                if _nonce:
                    reg_info["auth"] = link_register_mac(
                        self._secret, _nonce, self._name)
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

            # Heartbeat thread + LAN upgrade check
            hb_stop = threading.Event()
            _upgrade_check_interval = 30  # seconds between LAN checks when on WS
            def _client_heartbeat():
                _hb_count = 0
                while not hb_stop.is_set():
                    hb_stop.wait(5.0)
                    if hb_stop.is_set():
                        break
                    try:
                        if _hb_count % 6 == 0:
                            # Every 30s send a full status update (net_iface, cpu, etc.)
                            try:
                                _full = self.get_status()
                                _full['type'] = 'heartbeat'
                                self.send_status(_full)
                            except Exception:
                                self.send_status({"type": "heartbeat"})
                        else:
                            self.send_status({"type": "heartbeat"})
                    except (OSError, ConnectionError, BrokenPipeError) as _hb_err:
                        print(f"  [{_ts()}] [Link] Heartbeat send failed ({_hb_err}), closing connection")
                        self._close()
                        break
                    _hb_count += 1
                    # If on WS tunnel, periodically check if LAN is available
                    if (connected_via == 'ws' and self._host and self._port
                            and _hb_count % (_upgrade_check_interval // 5) == 0):
                        try:
                            _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            _probe.settimeout(3.0)
                            _probe.connect((self._host, self._port))
                            _probe.close()
                            # LAN is reachable — force reconnect via TCP
                            print(f"  [Link] LAN available — upgrading from WS to TCP")
                            self._close()
                            break
                        except (OSError, ConnectionError):
                            pass  # LAN not available, stay on WS

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
            print(f"  [{_ts()}] [Link] Connection closed ({connected_via}) [#{_connect_count}]")

            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception as e:
                    print(f"  [{_ts()}] [Link] on_disconnect callback error: {e}")

            if not self._stop.is_set():
                print(f"  [{_ts()}] [Link] Reconnecting in 2s...")
                if self._stop.wait(2.0):
                    break

    def _store_link_key(self, cmd):
        """The gateway sent this endpoint its key: store it, use it from now
        on, and ACK whether it was stored durably (only then does the gateway
        require it)."""
        key = str(cmd.get('key') or '').strip()
        ok, err = False, 'no key in command'
        if key:
            if self._on_link_key is None:
                err = 'endpoint has nowhere to store a key'
            else:
                try:
                    ok, err = self._on_link_key(key)
                except Exception as e:
                    ok, err = False, f'{type(e).__name__}: {e}'
            # Use it this session either way: it authenticates the next
            # reconnect even if it didn't survive to disk.
            self._secret = key
        print(f"  [Link] Received link key from gateway: "
              f"{'stored' if ok else 'NOT stored durably (' + str(err) + ')'}")
        self.send_ack('link_key_set', {'ok': bool(ok), 'error': '' if ok else str(err)})

    def _await_challenge(self, connected_via, timeout=3.0):
        """Wait briefly for the server's CHALLENGE and return its nonce.

        A gateway without LINK_SECRET sends nothing before REGISTER, so a
        timeout means "register without auth", not an error."""
        import select
        P = GatewayLinkProtocol
        raw = self._ws_transport._sock if connected_via == 'ws' else self._sock
        try:
            # A TLS socket may already hold the frame decrypted in its own
            # buffer, which select() on the fd can't see.
            pending = raw.pending() if hasattr(raw, 'pending') else 0
            ready = [raw] if pending else select.select([raw], [], [], timeout)[0]
            if not ready:
                print("  [Link] No CHALLENGE from server (gateway has no "
                      "LINK_SECRET) — registering without auth")
                return None
            if connected_via == 'ws':
                result = self._ws_transport.recv_frame()
            else:
                result = P.recv_frame(raw)
        except (OSError, ValueError):
            return None
        if result and result[0] == P.CHALLENGE:
            try:
                return str(json.loads(result[1]).get('nonce') or '') or None
            except (ValueError, AttributeError):
                return None
        return None

    def _reader_loop(self, sock):
        """Read frames from the server until disconnect."""
        P = GatewayLinkProtocol
        _frame_count = 0
        _last_frame_time = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    result = P.recv_frame(sock)
                except socket.timeout:
                    _silence = time.monotonic() - _last_frame_time
                    import datetime as _dt
                    print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Client: socket timeout "
                          f"({_silence:.1f}s since last frame, {_frame_count} frames total)")
                    break
                if result is None:
                    _silence = time.monotonic() - _last_frame_time
                    import datetime as _dt
                    print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Disconnected from server "
                          f"(after {_frame_count} frames, {_silence:.1f}s since last frame)")
                    break
                _frame_count += 1
                _last_frame_time = time.monotonic()
                ftype, payload = result
                try:
                    if ftype == P.AUDIO:
                        if self._on_audio:
                            self._on_audio(payload)
                    elif ftype == P.COMMAND:
                        # Fast-path: respond to ping immediately without plugin
                        cmd = json.loads(payload)
                        if cmd.get('cmd') == 'ping':
                            self.send_ack('ping', {'ok': True})
                        elif 'error' in cmd and 'cmd' not in cmd:
                            print(f"  [Link] Server refused us: {cmd['error']}")
                        elif cmd.get('cmd') == 'link_key_set':
                            self._store_link_key(cmd)
                        elif self._on_command:
                            self._on_command(cmd)
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
                import datetime as _dt
                print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Client reader error: {type(e).__name__}: {e}")
        finally:
            import datetime as _dt
            print(f"  [{_dt.datetime.now().strftime('%H:%M:%S')}] [Link] Client reader exiting")
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
                        cmd = json.loads(payload)
                        if cmd.get('cmd') == 'ping':
                            self.send_ack('ping', {'ok': True})
                        elif 'error' in cmd and 'cmd' not in cmd:
                            print(f"  [Link] Server refused us: {cmd['error']}")
                        elif cmd.get('cmd') == 'link_key_set':
                            self._store_link_key(cmd)
                        elif self._on_command:
                            self._on_command(cmd)
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
        "packet": False,  # endpoint can host Direwolf via 'mode' command
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
        if action == 'ping':
            return {"ok": True}
        return {"ok": False, "error": "not implemented"}

    def get_status(self):
        """Return current hardware state as a dict."""
        return {"plugin": self.name}

    @classmethod
    def _get_system_stats(cls):
        """Return CPU, RAM, disk, temp, and network interface stats."""
        stats = {}
        try:
            with open('/proc/stat') as f:
                parts = f.readline().split()
            vals = [int(v) for v in parts[1:]]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
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
            mem = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    parts = line.split()
                    if parts[0] in ('MemTotal:', 'MemAvailable:'):
                        mem[parts[0]] = int(parts[1]) * 1024
            total = mem.get('MemTotal:', 0)
            avail = mem.get('MemAvailable:', 0)
            if total > 0:
                stats['ram_pct'] = round((1 - avail / total) * 100, 1)
                stats['ram_mb'] = round((total - avail) / 1048576)
                stats['ram_total_mb'] = round(total / 1048576)
        except Exception:
            pass
        try:
            st = os.statvfs('/')
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total > 0:
                stats['disk_pct'] = round((1 - free / total) * 100, 1)
                stats['disk_free_gb'] = round(free / 1073741824, 1)
        except Exception:
            pass
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                stats['cpu_temp_c'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            pass
        try:
            with open('/proc/net/route') as f:
                for line in f:
                    fields = line.strip().split()
                    if fields[1] == '00000000':
                        stats['net_iface'] = fields[0]
                        break
            if 'net_iface' in stats:
                import subprocess
                out = subprocess.check_output(
                    ['ip', '-4', 'addr', 'show', stats['net_iface']],
                    stderr=subprocess.DEVNULL, timeout=2).decode()
                for line in out.split('\n'):
                    line = line.strip()
                    if line.startswith('inet '):
                        stats['net_ip'] = line.split()[1].split('/')[0]
                        break
        except Exception:
            pass
        try:
            import socket
            stats['hostname'] = socket.gethostname()
        except Exception:
            pass
        return stats

    _prev_cpu = None


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
        self._in_stream = None   # arecord subprocess (not PyAudio)
        self._out_stream = None  # PyAudio output stream
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
        # arecord reader thread
        self._rx_queue = None
        self._rx_thread = None
        self._rx_running = False
        self._stream_restart_count = 0
        self._last_config = None          # saved config for reopen

    def setup(self, config):
        """Open arecord for capture + PyAudio for output.

        Input uses arecord (raw ALSA) in a dedicated reader thread for
        reliable timing.  Output uses PyAudio for playback to the radio.

        *config* keys:
            device (str)   — ALSA device (e.g. 'hw:1,0') or name substring
            rate (int)     — sample rate (default 48000)
            channels (int) — channel count (default 1)
        """
        self._last_config = dict(config)

        # Load saved settings (gains + gate)
        saved = self._load_settings()
        if saved:
            self._rx_gain_db = max(-20, min(20, float(saved.get('rx_gain_db', 0))))
            self._tx_gain_db = max(-20, min(20, float(saved.get('tx_gain_db', 0))))
            if 'gate_threshold_db' in saved:
                self._gate_threshold_db = max(-60, min(-10, float(saved['gate_threshold_db'])))
            if 'gate_enabled' in saved:
                self._gate_enabled = bool(saved['gate_enabled'])
            print(f"  [Link] AudioPlugin: restored settings RX={self._rx_gain_db:+.1f} dB, "
                  f"TX={self._tx_gain_db:+.1f} dB, gate={'on' if self._gate_enabled else 'off'} "
                  f"@ {self._gate_threshold_db:.0f} dB")

        self._device_name = config.get('device', '')
        rate = int(config.get('rate', self.RATE))
        channels = int(config.get('channels', self.CHANNELS))

        # Resolve ALSA device — if device looks like hw:N,M use directly,
        # otherwise scan /proc/asound/cards for a name match
        alsa_dev = self._device_name
        if not alsa_dev.startswith(('hw:', 'plughw:')):
            card = self._find_alsa_card(alsa_dev or 'All-In-One')
            if card is not None:
                alsa_dev = f'plughw:{card},0'
                print(f"  [Link] AudioPlugin: matched ALSA card {card} → {alsa_dev}")
            else:
                print(f"  [Link] AudioPlugin: no ALSA card matched '{alsa_dev}', using default")
                alsa_dev = 'default'

        # Start arecord reader thread for input
        import queue as _q
        self._rx_queue = _q.Queue(maxsize=8)
        self._rx_running = True
        self._alsa_dev = alsa_dev
        self._rate = rate
        self._channels = channels
        self._rx_thread = threading.Thread(
            target=self._arecord_reader, daemon=True, name="arecord-reader")
        self._rx_thread.start()

        # Output via aplay subprocess (same device as arecord — plughw: allows both)
        try:
            import subprocess
            self._out_stream = subprocess.Popen(
                ['aplay', '-D', alsa_dev, '-f', 'S16_LE',
                 '-r', str(rate), '-c', str(channels), '-t', 'raw',
                 '--buffer-size', str(self.CHUNK_FRAMES * 4)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            print(f"  [Link] AudioPlugin: aplay output opened on {alsa_dev} pid={self._out_stream.pid}")
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to open aplay output: {e}")
            self._out_stream = None

    def _find_alsa_card(self, name_match):
        """Find ALSA card number by name substring in /proc/asound/cards."""
        try:
            with open('/proc/asound/cards') as f:
                for line in f:
                    line = line.strip()
                    if line and line[0].isdigit() and name_match.lower() in line.lower():
                        return int(line.split()[0])
        except Exception:
            pass
        return None

    def _arecord_reader(self):
        """Read audio from ALSA via arecord subprocess (dedicated thread)."""
        import subprocess
        chunk_bytes = self.CHUNK_BYTES
        while self._rx_running:
            proc = None
            try:
                proc = subprocess.Popen(
                    ['arecord', '-D', self._alsa_dev, '-f', 'S16_LE',
                     '-r', str(self._rate), '-c', str(self._channels),
                     '-t', 'raw', '--buffer-size', str(self.CHUNK_FRAMES * 4)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._in_stream = proc
                self._stream_restart_count += 1
                print(f"  [Link] AudioPlugin: arecord opened on {self._alsa_dev} "
                      f"pid={proc.pid} (restart #{self._stream_restart_count})")
            except Exception as e:
                print(f"  [Link] AudioPlugin: arecord failed: {e} — retrying in 2s")
                time.sleep(2)
                continue

            # Read loop with diagnostics
            _diag_time = time.monotonic()
            _diag_reads = 0
            _diag_gated = 0
            _diag_short = 0
            _diag_overflow = 0
            _diag_max_read_ms = 0.0
            _diag_rms_sum = 0.0
            _DIAG_INTERVAL = 10.0

            while self._rx_running:
                try:
                    _t0 = time.monotonic()
                    data = proc.stdout.read(chunk_bytes)
                    _read_ms = (time.monotonic() - _t0) * 1000
                    if _read_ms > _diag_max_read_ms:
                        _diag_max_read_ms = _read_ms

                    if not data:
                        break
                    _diag_reads += 1

                    if len(data) < chunk_bytes:
                        _diag_short += 1
                        data += b'\x00' * (chunk_bytes - len(data))

                    # Compute RMS for diagnostics (on the raw chunk)
                    rms = pcm_rms(data)
                    _diag_rms_sum += rms

                    # RX gain
                    if self._rx_gain_db != 0.0:
                        data = self._apply_volume(data, self._db_to_linear(self._rx_gain_db))
                        _gate_rms = None      # data changed — gate must recompute
                    else:
                        _gate_rms = rms       # unchanged — reuse, skip a whole pass

                    # Noise gate — when closed, DROP the chunk rather than
                    # queue silence. The gateway treats missing audio as
                    # silence naturally, so sending zero bytes wastes link
                    # bandwidth and, on the gateway side, keeps the receive
                    # queue saturated (overflow on every push).
                    if self._gate_enabled:
                        data = self._apply_gate(data, rms=_gate_rms)
                        if self._gate_open is False:
                            _diag_gated += 1
                            continue

                    # Queue for get_audio
                    try:
                        self._rx_queue.put_nowait(data)
                    except Exception:
                        _diag_overflow += 1
                        try:
                            self._rx_queue.get_nowait()
                        except Exception:
                            pass
                        try:
                            self._rx_queue.put_nowait(data)
                        except Exception:
                            pass

                    # Periodic diagnostic dump
                    _now = time.monotonic()
                    if _now - _diag_time >= _DIAG_INTERVAL:
                        _avg_rms = _diag_rms_sum / max(_diag_reads, 1)
                        _db = 20 * math.log10(_avg_rms / 32767.0) if _avg_rms > 0 else -100.0
                        print(f"  [RX-DIAG] {_DIAG_INTERVAL:.0f}s: reads={_diag_reads} "
                              f"gated={_diag_gated} short={_diag_short} overflow={_diag_overflow} "
                              f"max_read={_diag_max_read_ms:.0f}ms avg_rms={_avg_rms:.0f} "
                              f"({_db:.1f}dB) gate={'open' if self._gate_open else 'closed'} "
                              f"qd={self._rx_queue.qsize()}")
                        _diag_time = _now
                        _diag_reads = _diag_gated = _diag_short = _diag_overflow = 0
                        _diag_max_read_ms = 0.0
                        _diag_rms_sum = 0.0

                except Exception as e:
                    print(f"  [Link] AudioPlugin: arecord read error: {e}")
                    break

            # Kill and retry
            self._in_stream = None
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if self._rx_running:
                print(f"  [Link] AudioPlugin: arecord died — restarting in 2s")
                time.sleep(2)

    def _apply_gate(self, data, rms=None):
        """Apply noise gate to PCM data. Returns data or silence.

        *rms* lets the caller pass a value it already computed for this exact
        buffer — the reader thread computes one for diagnostics and, when no
        RX gain was applied, the buffer is unchanged so the gate can reuse it
        instead of running a second full pass over every sample.
        """
        if rms is None:
            rms = pcm_rms(data)
        db = 20 * math.log10(rms / 32767.0) if rms > 0 else -100.0
        if db > self._gate_threshold_db:
            self._gate_envelope += self._gate_attack * (1.0 - self._gate_envelope)
        else:
            self._gate_envelope *= (1.0 - self._gate_release)
        self._gate_open = self._gate_envelope > 0.1
        if not self._gate_open:
            return b'\x00' * len(data)
        return data

    def teardown(self):
        """Stop arecord reader and aplay output, save settings."""
        self._save_settings()
        self._rx_running = False
        if self._in_stream:
            try:
                self._in_stream.kill()
                self._in_stream.wait(timeout=2)
            except Exception:
                pass
            self._in_stream = None
        if self._rx_thread:
            self._rx_thread.join(timeout=3)
        if self._out_stream:
            try:
                self._out_stream.stdin.close()
                self._out_stream.kill()
                self._out_stream.wait(timeout=2)
            except Exception:
                pass
            self._out_stream = None
        print("  [Link] AudioPlugin: teardown complete")

    def get_audio(self, chunk_size=None):
        """Read one 50 ms chunk from the arecord queue."""
        if getattr(self, '_mode', 'audio') == 'data':
            return None, False
        if not self._rx_queue:
            return None, False
        try:
            import queue as _q
            data = self._rx_queue.get(timeout=0.06)  # slightly > 50ms to avoid busy spin
            return data, False
        except _q.Empty:
            return None, False

    def reopen_audio(self):
        """Restart arecord reader (called on gateway reconnect)."""
        # arecord reader thread auto-restarts — just log
        print(f"  [Link] AudioPlugin: reopen requested (arecord auto-restarts)")

    def put_audio(self, pcm):
        """Write PCM audio to aplay stdin, applying TX gain."""
        if not self._out_stream or self._out_stream.poll() is not None:
            return
        try:
            if self._tx_gain_db != 0.0:
                pcm = self._apply_volume(pcm, self._db_to_linear(self._tx_gain_db))
            self._out_stream.stdin.write(pcm)
        except (BrokenPipeError, OSError) as e:
            print(f"  [Link] AudioPlugin: aplay write error: {e}")

    def execute(self, cmd):
        """Handle commands from master gateway."""
        action = cmd.get('cmd', '') if isinstance(cmd, dict) else ''
        if action == 'rx_gain':
            self._rx_gain_db = max(-20, min(20, float(cmd.get('db', 0))))
            self._save_settings()
            print(f"  [Link] AudioPlugin: RX gain set to {self._rx_gain_db:+.1f} dB")
            return {"ok": True, "rx_gain_db": self._rx_gain_db}
        if action == 'tx_gain':
            self._tx_gain_db = max(-20, min(20, float(cmd.get('db', 0))))
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
            "input_active": self._in_stream is not None and (not hasattr(self._in_stream, 'poll') or self._in_stream.poll() is None),
            "output_active": self._out_stream is not None and (not hasattr(self._out_stream, 'poll') or self._out_stream.poll() is None),
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

    @staticmethod
    def _apply_volume(pcm, gain):
        """Apply a gain multiplier to 16-bit signed LE PCM audio."""
        return pcm_apply_gain(pcm, gain)

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
            # Atomic write, inlined (this file ships to endpoints via
            # _ENDPOINT_FILES which does not include atomic_json.py).
            _tmp = self._settings_file + '.tmp'
            with open(_tmp, 'w') as f:
                json.dump({"rx_gain_db": self._rx_gain_db,
                           "tx_gain_db": self._tx_gain_db,
                           "gate_threshold_db": self._gate_threshold_db,
                           "gate_enabled": self._gate_enabled}, f)
            os.replace(_tmp, self._settings_file)
        except Exception as e:
            print(f"  [Link] AudioPlugin: failed to save settings: {e}")

    def _load_settings(self):
        """Load gain settings from JSON file. Returns dict or None."""
        try:
            with open(self._settings_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
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
        "packet": True,  # implements 'mode' command to run Direwolf locally
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
        # Serialises the HID write + _ptt_on update. Two threads key this
        # radio: the link reader (execute 'ptt') and the safety Timer
        # (_ptt_timeout_fired). Interleaved, the last hardware write could
        # disagree with _ptt_on — and because the fired timer has already
        # cleared itself, a resulting stuck key had nothing left to correct
        # it. Never taken while holding _ptt_timer_lock (no nesting).
        self._ptt_hw_lock = threading.Lock()
        # ── Pre-key TX buffer ──────────────────────────────────────────────
        # The gateway starts streaming TX audio at the same moment it sends
        # the PTT command, but the radio isn't transmitting until the HID
        # write lands here — so those first chunks used to be played into an
        # unkeyed radio and lost (the first syllable of every transmission).
        # They wait here instead and are flushed, in order, the moment PTT
        # goes on. Bounded so a key that never arrives can't grow it or add
        # unbounded latency; oldest chunks drop first.
        import collections as _collections
        self._prekey_buf = _collections.deque()
        self._prekey_bytes = 0
        self._prekey_max_bytes = int(0.5 * self.RATE * self.FORMAT_WIDTH)  # 500 ms
        self._prekey_lock = threading.Lock()
        # Data mode (Direwolf TNC) — TNC runs on the gateway now
        # (packet_tnc.py). AIOCPlugin's job is to release ALSA when the
        # mode is 'data' so direwolf can claim hw:N,0 exclusively.
        self._mode_lock = threading.Lock()   # serialise _set_mode calls
        self._mode = 'audio'             # 'audio' or 'data'
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
        # Pre-key TX buffer depth in ms (0 disables the buffer entirely and
        # restores the old drop-while-unkeyed behaviour).
        _pk_ms = max(0, int(config.get('prekey_buffer_ms', 500)))
        self._prekey_max_bytes = int(_pk_ms / 1000.0 * self.RATE * self.FORMAT_WIDTH)

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
                            aioc_hw = f'plughw:{card_num},0'
                            break
            except Exception:
                pass
            config['device'] = aioc_hw or 'All-In-One'
            if aioc_hw:
                print(f"  [Link] AIOCPlugin: found AIOC at {aioc_hw}")
                self._aioc_hw = aioc_hw
        else:
            self._aioc_hw = config.get('device', '')

        # Open HID BEFORE audio — arecord on hw: kills /dev/hidraw but an
        # already-open file descriptor survives and stays usable for writes
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

        # Open audio streams AFTER HID
        super().setup(config)

    def teardown(self):
        """Unkey PTT, cancel safety timer, and close HID + audio.

        Direwolf is gateway-side now (packet_tnc.py) — gateway shutdown reaps
        it via ProcessSupervisor.shutdown_all().
        """
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
            # direwolf_running now reflects the gateway-side TNC; endpoints
            # report False for compatibility, gateway-side packet_radio.py
            # surfaces the real state via gw.packet_tnc.status().
            "direwolf_running": False,
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
        with self._mode_lock:
            return self._set_mode_locked(cmd, new_mode)

    def _set_mode_locked(self, cmd, new_mode):
        # Same-mode is a no-op; the gateway-side TNC owns direwolf health,
        # so there's no plugin-local "is direwolf still running?" check.
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
            # Release ALSA so the gateway-side direwolf can claim exclusive
            # access to hw:N,0. Same goes for the HID — direwolf uses CM108
            # GPIO PTT via /dev/hidraw, which collides with libusb.
            self._rx_running = False
            if self._in_stream:
                try:
                    self._in_stream.kill()
                    self._in_stream.wait(timeout=2)
                except Exception:
                    pass
                self._in_stream = None
            if self._out_stream:
                try:
                    self._out_stream.stdin.close()
                    self._out_stream.kill()
                    self._out_stream.wait(timeout=2)
                except Exception:
                    pass
                self._out_stream = None
            if self._hid:
                try:
                    self._hid.close()
                except Exception:
                    pass
                self._hid = None
                self._rebind_usbhid()
            time.sleep(0.5)
        else:
            # Re-acquire ALSA + HID for voice mode.
            time.sleep(0.5)
            self._rx_running = True
            if self._last_config:
                self.setup(self._last_config)
            else:
                self.reopen_audio()
            # Reopen HID (was closed for data mode)
            if not self._hid:
                try:
                    import hid as _hid_mod
                    if hasattr(_hid_mod, 'Device'):
                        self._hid = _hid_mod.Device(vid=self._vid, pid=self._pid)
                    else:
                        self._hid = _hid_mod.device()
                        self._hid.open(self._vid, self._pid)
                    print("  [Link] AIOCPlugin: HID reopened after data mode", flush=True)
                except Exception as e:
                    print(f"  [Link] AIOCPlugin: HID reopen failed: {e}", flush=True)

        return {"ok": True, "mode": new_mode}

    def _rebind_usbhid(self):
        """Bind usbhid to the AIOC HID interface so Direwolf gets a hidraw device.

        The Python hid module uses libusb which claims the interface exclusively,
        preventing the kernel from creating /dev/hidrawX.  After closing the hid
        device we rebind usbhid via sysfs (requires passwordless sudo rule for tee).
        """
        import subprocess as _sp, os as _os, time as _t
        # Find the HID interface path for the AIOC (vid=1209, class=03)
        iface_path = None
        try:
            base = '/sys/bus/usb/devices'
            for dev in sorted(_os.listdir(base)):
                dev_path = f'{base}/{dev}'
                class_file = f'{dev_path}/bInterfaceClass'
                uevent_file = f'{dev_path}/uevent'
                if not _os.path.exists(class_file):
                    continue
                with open(class_file) as f:
                    if f.read().strip() != '03':
                        continue
                if _os.path.exists(uevent_file):
                    with open(uevent_file) as f:
                        content = f.read()
                    if '1209' in content and '7388' in content:
                        iface_path = dev
                        break
        except Exception as e:
            print(f'  [Link] AIOCPlugin: HID iface scan error: {e}', flush=True)

        if not iface_path:
            print('  [Link] AIOCPlugin: could not find AIOC HID interface for usbhid rebind', flush=True)
            return

        try:
            result = _sp.run(
                ['sudo', '-n', 'tee', '/sys/bus/usb/drivers/usbhid/bind'],
                input=iface_path.encode(), capture_output=True, timeout=3)
            if result.returncode == 0:
                print(f'  [Link] AIOCPlugin: usbhid bound to {iface_path}', flush=True)
                # Wait for /dev/hidrawX to appear
                for _ in range(10):
                    _t.sleep(0.2)
                    if _os.path.exists('/dev/hidraw0') or any(
                            f.startswith('hidraw') for f in _os.listdir('/dev')):
                        break
            else:
                print(f'  [Link] AIOCPlugin: usbhid bind failed: {result.stderr.decode().strip()}', flush=True)
        except Exception as e:
            print(f'  [Link] AIOCPlugin: usbhid bind error: {e}', flush=True)

    # Direwolf launch/teardown moved to gateway-side packet_tnc.py. AIOCPlugin
    # still releases its ALSA streams when entering 'data' mode so direwolf
    # (running on the gateway) has exclusive device access; orchestration is
    # owned by packet_radio.py.

    def put_audio(self, pcm):
        """Play gateway TX audio, holding it back until the radio is keyed.

        See _prekey_buf in __init__. When keyed, any held audio goes out
        ahead of the live chunk so ordering is preserved.
        """
        if not pcm:
            return
        if not self._ptt_on:
            if self._prekey_max_bytes <= 0:
                return  # buffering disabled — old behaviour (chunk is lost)
            with self._prekey_lock:
                self._prekey_buf.append(pcm)
                self._prekey_bytes += len(pcm)
                while (self._prekey_bytes > self._prekey_max_bytes
                       and len(self._prekey_buf) > 1):
                    self._prekey_bytes -= len(self._prekey_buf.popleft())
            return
        pending = self._take_prekey()
        if pending:
            super().put_audio(pending)
        super().put_audio(pcm)

    def _take_prekey(self):
        """Atomically remove and return everything held (b'' if nothing)."""
        with self._prekey_lock:
            if not self._prekey_buf:
                return b''
            data = b''.join(self._prekey_buf)
            self._prekey_buf.clear()
            self._prekey_bytes = 0
        return data

    def _set_ptt(self, state_on):
        """Key or unkey the radio via AIOC HID GPIO.

        Serialised on _ptt_hw_lock so the command path and the safety-timer
        path can't interleave their write + state update.
        """
        if not self._hid:
            return {"ok": False, "error": "HID not connected"}
        try:
            import struct
            state = 1 if state_on else 0
            iomask = 1 << (self._ptt_channel - 1)
            iodata = state << (self._ptt_channel - 1)
            data = struct.pack("<BBBBB", 0, 0, iodata, iomask, 0)
            with self._ptt_hw_lock:
                self._hid.write(bytes(data))
                self._ptt_on = state_on
            # Release held audio as soon as the radio is actually keyed. Done
            # outside the HW lock (the aplay write can block) and also here
            # rather than only in put_audio, so a transmission that ended
            # inside the key-up window isn't stranded in the buffer.
            if state_on:
                pending = self._take_prekey()
                if pending:
                    super().put_audio(pending)
            else:
                self._take_prekey()  # discard — belongs to a finished TX
            self._status_dirty = True  # trigger immediate status report
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
