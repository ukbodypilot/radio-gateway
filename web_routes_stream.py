"""WebSocket and streaming route handlers extracted from web_server.py."""

from log_util import log_exc
import base64
import hashlib
import socket
import threading as _thr
import queue as _q_mod

from gateway_link import (
    GatewayLinkProtocol, WsClosed, ws_encode, ws_read_message,
    WS_MAX_PAYLOAD as _WS_MAX_PAYLOAD, WS_OP_BINARY, WS_OP_CLOSE, WS_OP_TEXT,
)

_WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _ws_key(handler):
    """The client's Sec-WebSocket-Key, or None after answering 400."""
    handler._upgrading_ws = True
    ws_key = handler.headers.get('Sec-WebSocket-Key', '')
    if not ws_key or handler.headers.get('Upgrade', '').lower() != 'websocket':
        _ws_refuse(handler, 400)
        return None
    return ws_key


def _ws_refuse(handler, code):
    handler._upgrading_ws = False
    handler.send_response(code)
    handler.end_headers()


def _ws_handshake(handler, ws_key):
    """Send the 101 and return the raw socket.

    Written straight to the socket: BaseHTTPRequestHandler's send_response
    adds Server/Date headers that can confuse strict WS clients, and the
    BufferedWriter is flushed first so nothing is left behind it.
    """
    accept = base64.b64encode(
        hashlib.sha1((ws_key + _WS_MAGIC).encode()).digest()
    ).decode()
    handler.wfile.flush()
    handshake = (
        'HTTP/1.1 101 Switching Protocols\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Accept: {accept}\r\n'
        '\r\n'
    )
    handler.request.sendall(handshake.encode('ascii'))
    handler.close_connection = True  # prevent handler loop after do_GET returns
    return handler.request


def _ws_read_loop(sock, on_message):
    """Hand each client message to on_message(opcode, payload) until the
    client closes, the socket dies, or on_message returns False.

    Pings and close frames are answered by ws_read_message. A recv timeout
    between messages is normal (browsers don't ping); keepalive reaps peers
    that vanish.
    """
    while True:
        try:
            msg = ws_read_message(sock, _WS_MAX_PAYLOAD)
        except socket.timeout:
            continue
        except (WsClosed, OSError):
            return
        if msg is None or on_message(*msg) is False:
            return


def _enable_ws_keepalive(sock):
    """TCP keepalive + user-timeout so silently-dead WS clients get reaped.

    Browsers don't ping and a client that vanishes without FIN/RST (WiFi
    drop, phone sleep) never errors recv() — the reader loop's
    `timeout → continue` then spins forever, leaking the handler thread,
    the socket and (for /ws_audio) the sender thread + _ws_clients entry.
    With keepalive, the kernel kills the connection after ~60s of dead
    peer and recv()/sendall() raise OSError, unwinding the handler.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        _TCP_USER_TIMEOUT = 18  # Linux socket option number
        sock.setsockopt(socket.IPPROTO_TCP, _TCP_USER_TIMEOUT, 60000)
    except (AttributeError, OSError):
        pass  # not supported — behaviour falls back to the old one


def handle_ws_audio(handler, parent):
    """GET /ws_audio -- Low-latency PCM WebSocket.

    Upgrades the HTTP connection to a WebSocket, then streams PCM audio
    frames from the gateway to the browser via a dedicated send thread.
    Blocks the handler thread for the lifetime of the connection.
    """
    # WebSocket upgrade for low-latency PCM audio streaming
    ws_key = _ws_key(handler)
    if not ws_key:
        return
    _sock = _ws_handshake(handler, ws_key)  # raw TCP socket for binary frames
    _client_ip = handler.client_address[0]
    print(f"\n[WS-Audio] Low-latency client connected from {_client_ip}")
    _sock.settimeout(30)  # 30s recv timeout
    _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _enable_ws_keepalive(_sock)
    _send_q = _q_mod.Queue(maxsize=6)  # ~300ms buffer at 50ms chunks
    _ws_entry = (_sock, _send_q)
    _ws_closed = _thr.Event()  # reader sets on exit; sender polls it

    def _ws_sender(_s, _q):
        """Dedicated send thread -- drains queue, never blocks audio loop."""
        while True:
            try:
                frame = _q.get(timeout=5)
                if frame is None:
                    break
                _s.sendall(frame)
            except (_q_mod.Empty):
                if _ws_closed.is_set():
                    break  # reader gone and nothing queued — exit
                continue
            except (BrokenPipeError, ConnectionResetError, OSError):
                break

    _send_thread = _thr.Thread(target=_ws_sender, args=(_sock, _send_q), daemon=True)
    _send_thread.start()

    with parent._ws_lock:
        parent._ws_clients.append(_ws_entry)
    try:
        # We only send, but must keep reading to answer pings and close.
        _ws_read_loop(_sock, lambda _op, _data: None)
    finally:
        # Signal sender thread to exit. put_nowait, never put: if the
        # sender already died with the queue full, a blocking put() would
        # hang this handler thread forever. _ws_closed covers the case
        # where the sentinel doesn't fit — the sender exits on its next
        # 5s idle wakeup.
        _ws_closed.set()
        try:
            _send_q.put_nowait(None)
        except _q_mod.Full:
            pass
        with parent._ws_lock:
            try:
                parent._ws_clients.remove(_ws_entry)
            except ValueError:
                pass
        print(f"[WS-Audio] Disconnected {_client_ip}")
    return


def handle_ws_mic(handler, parent):
    """GET /ws_mic -- Browser microphone WebSocket.

    Upgrades the HTTP connection to a WebSocket that receives PCM audio
    from the browser microphone and pushes it into the WebMicSource for
    radio TX.  Blocks the handler thread for the lifetime of the connection.
    """
    # WebSocket endpoint for browser microphone -> radio TX
    ws_key = _ws_key(handler)
    if not ws_key:
        return
    # Check if web mic source is available
    _mic_src = parent.gateway.web_mic_source if parent.gateway else None
    if not _mic_src:
        _ws_refuse(handler, 503)
        return
    # Reject if another mic client is already connected
    if _mic_src.client_connected:
        _ws_refuse(handler, 409)  # Conflict
        return
    _sock = _ws_handshake(handler, ws_key)
    _client_ip = handler.client_address[0]
    _sock.settimeout(30)
    _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _enable_ws_keepalive(_sock)
    _mic_src.client_connected = True
    _mic_src.unkey('new connection')  # never inherit a previous client's key
    print(f"\n[WS-Mic] Browser mic connected from {_client_ip}")
    # Connecting does NOT transmit. The browser holds the socket open
    # between overs to avoid re-paying handshake latency, and keys with
    # explicit KEY/UNKEY text frames while the operator holds the button.
    # PTT itself is the bus system's: WebMicSource has ptt_control=True, so
    # a SoloBus with webmic as a TX source keys its radio while the source
    # asks for PTT -- which it only does while keyed and inside both
    # watchdogs (see WebMicSource).
    def _on_message(opcode, payload):
        if opcode == WS_OP_TEXT:  # PTT control
            cmd = payload.decode('utf-8', 'replace').strip().upper()
            if cmd == 'KEY':
                # Sent on press and repeated ~2x/sec while held.
                # Refused once the TOT has tripped, until release.
                if not _mic_src.key():
                    try:
                        _sock.sendall(ws_encode(WS_OP_TEXT, b'TOT'))
                    except OSError:
                        return False
            elif cmd == 'UNKEY':
                _mic_src.unkey('release')
        elif opcode == WS_OP_BINARY:  # PCM audio data
            _mic_src.push_audio(payload)

    try:
        _ws_read_loop(_sock, _on_message)
    finally:
        # Order matters: unkey BEFORE clearing client_connected, so the
        # unkey is logged and counted rather than silently masked by the
        # disconnected check in get_audio.
        _mic_src.unkey('socket closed')
        _mic_src.client_connected = False
        print(f"[WS-Mic] Disconnected {_client_ip}")
    return


def handle_ws_monitor(handler, parent):
    """GET /ws_monitor -- Room monitor WebSocket.

    Upgrades the HTTP connection to a WebSocket that receives PCM audio
    from the browser room-monitor mic and pushes it into the
    WebMonitorSource.  Blocks the handler thread for the lifetime of the
    connection.
    """
    # WebSocket endpoint for room monitor -- audio into mixer, NO PTT
    ws_key = _ws_key(handler)
    if not ws_key:
        return
    _mon_src = parent.gateway.web_monitor_source if parent.gateway else None
    if not _mon_src:
        _ws_refuse(handler, 503)
        return
    if _mon_src.client_connected:
        _ws_refuse(handler, 409)
        return
    _sock = _ws_handshake(handler, ws_key)
    _client_ip = handler.client_address[0]
    _sock.settimeout(30)
    _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _enable_ws_keepalive(_sock)
    _mon_src.client_connected = True
    print(f"\n[WS-Monitor] Room monitor connected from {_client_ip}")
    def _on_message(opcode, payload):
        if opcode == WS_OP_BINARY:  # PCM audio data
            _mon_src.push_audio(payload)

    try:
        _ws_read_loop(_sock, _on_message)
    finally:
        _mon_src.client_connected = False
        _mon_src._sub_buffer = b''
        print(f"[WS-Monitor] Disconnected {_client_ip}")
    return


def handle_stream(handler, parent):
    """GET /stream -- MP3 audio stream.

    Subscribes to the shared MP3 encoder and streams chunks to the HTTP
    client as ``audio/mpeg``.  Blocks the handler thread for the lifetime
    of the connection.
    """
    # MP3 audio stream from shared encoder
    _client_ip = handler.client_address[0]
    print(f"\n[Stream] Connection from {_client_ip}")
    ev, seq = parent._subscribe_stream()
    _bytes_sent = 0
    try:
        # Wait for encoder to produce initial MP3 data
        for _wait in range(50):  # up to 5 seconds
            ev.wait(timeout=0.1)
            ev.clear()
            with parent._stream_lock:
                if parent._mp3_seq > seq:
                    break
        with parent._stream_lock:
            if parent._mp3_seq <= seq:
                print(f"[Stream] No encoder data for {_client_ip} -- aborting")
                handler.send_response(503)
                handler.end_headers()
                return

        handler.send_response(200)
        handler.send_header('Content-Type', 'audio/mpeg')
        handler.send_header('Cache-Control', 'no-cache, no-store')
        handler.send_header('Connection', 'close')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.send_header('icy-name', 'Radio Gateway')
        handler.end_headers()
        print(f"[Stream] Streaming to {_client_ip}")

        while True:
            ev.wait(timeout=5)
            ev.clear()
            with parent._stream_lock:
                buf = parent._mp3_buffer
                cur_seq = parent._mp3_seq
                # How many new chunks since our last read
                available = cur_seq - seq
                if available > 0:
                    # Clamp to buffer size (in case we fell behind)
                    available = min(available, len(buf))
                    chunks = buf[-available:] if available < len(buf) else list(buf)
                    seq = cur_seq
                else:
                    chunks = []
            for chunk in chunks:
                handler.wfile.write(chunk)
                _bytes_sent += len(chunk)
            if chunks:
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        print(f"\n[Stream] Error for {_client_ip}: {e}")
    finally:
        _kb = _bytes_sent // 1024
        print(f"[Stream] Disconnected {_client_ip} ({_kb}KB sent)")
        parent._unsubscribe_stream(ev)
    return


# ---------------------------------------------------------------------------
# WebSocket Link Bridge — proxies endpoint connections through CF tunnel
# ---------------------------------------------------------------------------

def handle_ws_link(handler, parent):
    """GET /ws/link -- WebSocket bridge to the Gateway Link server (TCP 9700).

    Allows remote endpoints to connect through the Cloudflare tunnel.
    Each WS binary message = one complete link protocol frame.
    The bridge opens a local TCP connection to the link server and
    bidirectionally forwards frames between WS and TCP.
    """
    ws_key = _ws_key(handler)
    if not ws_key:
        return

    gw = parent.gateway if parent else None
    link_port = int(getattr(gw.config, 'LINK_PORT', 9700)) if gw else 9700

    ws_sock = _ws_handshake(handler, ws_key)
    _client_ip = handler.client_address[0]
    ws_sock.settimeout(15)
    ws_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # Open local TCP connection to link server
    try:
        tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_sock.settimeout(5)
        tcp_sock.connect(('127.0.0.1', link_port))
        tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp_sock.settimeout(15)
    except Exception as e:
        print(f"[WS-Link] Failed to connect to link server: {e}")
        try:
            ws_sock.sendall(ws_encode(WS_OP_CLOSE))
        except Exception:
            log_exc()
        return

    print(f"[WS-Link] Bridge connected from {_client_ip}")
    _stop = _thr.Event()

    def _tcp_to_ws():
        """Read link frames from TCP, send as WS binary messages."""
        import struct as _st
        _hdr_struct = _st.Struct('>BH')
        try:
            while not _stop.is_set():
                # Read 3-byte link frame header
                hdr = _recv_exact(tcp_sock, 3)
                if hdr is None:
                    break
                _type, _length = _hdr_struct.unpack(hdr)
                payload = _recv_exact(tcp_sock, _length) if _length else b''
                if payload is None and _length > 0:
                    break
                # Send complete frame as one WS binary message
                ws_sock.sendall(ws_encode(WS_OP_BINARY, hdr + (payload or b'')))
        except (OSError, ConnectionError):
            pass
        finally:
            _stop.set()

    def _ws_to_tcp():
        """Read WS binary messages, write raw bytes to TCP."""
        try:
            while not _stop.is_set():
                # A recv timeout (15 s) ends the bridge, as it always has.
                msg = ws_read_message(ws_sock, _WS_MAX_PAYLOAD)
                if msg is None:
                    break
                tcp_sock.sendall(msg[1])
        except (OSError, ConnectionError, WsClosed):
            pass
        finally:
            _stop.set()

    tcp_thread = _thr.Thread(target=_tcp_to_ws, daemon=True,
                              name="ws-link-tcp2ws")
    tcp_thread.start()

    try:
        _ws_to_tcp()  # runs in handler thread
    finally:
        _stop.set()
        try:
            tcp_sock.close()
        except Exception:
            pass
        try:
            ws_sock.sendall(ws_encode(WS_OP_CLOSE))
        except OSError:
            pass  # client already gone
        tcp_thread.join(timeout=5)
        print(f"[WS-Link] Bridge disconnected {_client_ip}")


# -- Link bridge helper -----------------------------------------------------

_recv_exact = GatewayLinkProtocol._recv_exact
