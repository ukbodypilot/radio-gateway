#!/usr/bin/env python3
"""Shared WebSocket framing (gateway_link.ws_*) and the handlers built on it.

Before, framing was hand-written four times (three browser handlers in
web_routes_stream.py plus the link bridge) and once more in the endpoint
client. The browser loops used bare recv(n), so a header or mask key that
arrived in two TCP segments desynchronised the stream; pongs over 125
bytes had a wrong length; the endpoint client sent unmasked pongs and close
frames (clients must mask); and fragmented messages were not supported.

Covers the codec on its own, then real sockets end to end: a browser-style
client on /ws_mic, and WebSocketTransport through the /ws/link bridge to a
fake link server.
"""

import os
import socket
import struct
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
from gateway_link import (  # noqa: E402
    GatewayLinkProtocol, WebSocketTransport, WsClosed, ws_encode, ws_mask,
    ws_read_frame, ws_read_message, WS_OP_BINARY, WS_OP_CLOSE, WS_OP_CONT,
    WS_OP_PING, WS_OP_PONG, WS_OP_TEXT,
)
import web_routes_stream  # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class Dribble:
    """A socket-like reader that hands out `step` bytes per recv, with an
    optional socket.timeout injected before chosen byte offsets."""

    def __init__(self, data, step=1, timeouts_at=()):
        self.data, self.pos, self.step = data, 0, step
        self.timeouts_at = set(timeouts_at)
        self.sent = b''

    def recv(self, n):
        if self.pos in self.timeouts_at:
            self.timeouts_at.discard(self.pos)
            raise socket.timeout()
        if self.pos >= len(self.data):
            return b''
        out = self.data[self.pos:self.pos + min(n, self.step)]
        self.pos += len(out)
        return out

    def sendall(self, b):
        self.sent += b


def frame(op, payload, fin=True, mask=True):
    """Hand-built frame, independent of ws_encode, for the decoder tests."""
    n = len(payload)
    b0 = (0x80 if fin else 0) | op
    m = 0x80 if mask else 0
    if n < 126:
        hdr = bytes((b0, m | n))
    elif n < 65536:
        hdr = bytes((b0, m | 126)) + struct.pack('>H', n)
    else:
        hdr = bytes((b0, m | 127)) + struct.pack('>Q', n)
    if mask:
        key = b'\x11\x22\x33\x44'
        return hdr + key + ws_mask(payload, key)
    return hdr + payload


def codec():
    print("1. encode / decode round trip at every length boundary")
    for n in (0, 1, 125, 126, 127, 65535, 65536, 70000):
        data = os.urandom(n)
        for mask in (False, True):
            fin, op, out = ws_read_frame(Dribble(ws_encode(WS_OP_BINARY, data, mask=mask), step=4096))
            if not (fin and op == WS_OP_BINARY and out == data):
                check(f"len {n} mask={mask}", False)
                break
        else:
            continue
        break
    else:
        check("0..70000 bytes, masked and not", True)
    check("server frames are unmasked", not (ws_encode(WS_OP_BINARY, b'x')[1] & 0x80))
    check("client frames are masked", bool(ws_encode(WS_OP_BINARY, b'x', mask=True)[1] & 0x80))

    print("\n2. a frame split across many TCP segments still decodes")
    data = os.urandom(3000)
    fin, op, out = ws_read_frame(Dribble(frame(WS_OP_BINARY, data), step=1))
    check("one byte per recv", out == data)

    print("\n3. ping, pong and close are answered and skipped")
    src = Dribble(frame(WS_OP_PING, b'hello') + frame(WS_OP_PONG, b'') +
                  frame(WS_OP_BINARY, b'audio'))
    msg = ws_read_message(src)
    check("data after ping returned", msg == (WS_OP_BINARY, b'audio'), str(msg))
    check("pong echoes the ping payload, unmasked",
          src.sent == bytes((0x8A, 5)) + b'hello', src.sent.hex())
    big = os.urandom(200)
    src = Dribble(frame(WS_OP_PING, big) + frame(WS_OP_TEXT, b'x'))
    ws_read_message(src, max_payload=1000)
    fin, op, out = ws_read_frame(Dribble(src.sent, step=4096))
    check("pong over 125 bytes is well-formed (old code wrote length 0)",
          op == WS_OP_PONG and out == big)
    src = Dribble(frame(WS_OP_PING, b'p', mask=False) + frame(WS_OP_TEXT, b'x', mask=False))
    ws_read_message(src, mask_replies=True)
    check("client side masks its pong", bool(src.sent[1] & 0x80), src.sent.hex())
    src = Dribble(frame(WS_OP_CLOSE, b'\x03\xe8'))
    check("close returns None", ws_read_message(src) is None)
    check("close is echoed with its code", src.sent == bytes((0x88, 2)) + b'\x03\xe8', src.sent.hex())

    print("\n4. fragmented messages are reassembled")
    src = Dribble(frame(WS_OP_BINARY, b'ab', fin=False) + frame(WS_OP_PING, b'') +
                  frame(WS_OP_CONT, b'cd', fin=False) + frame(WS_OP_CONT, b'ef'))
    check("3 fragments with a ping between", ws_read_message(src) == (WS_OP_BINARY, b'abcdef'))

    print("\n5. protocol errors and oversize frames close the connection")
    for name, data in (
        ("continuation with nothing to continue", frame(WS_OP_CONT, b'x')),
        ("new message inside a fragmented one",
         frame(WS_OP_TEXT, b'a', fin=False) + frame(WS_OP_TEXT, b'b')),
        ("unknown opcode", frame(0x3, b'x')),
        ("oversize frame", frame(WS_OP_BINARY, b'x' * 2000)),
        ("oversize across fragments",
         frame(WS_OP_BINARY, b'x' * 600, fin=False) + frame(WS_OP_CONT, b'x' * 600)),
        ("peer vanishes mid-frame", frame(WS_OP_BINARY, b'x' * 100)[:50]),
    ):
        try:
            ws_read_message(Dribble(data, step=64), max_payload=1000)
            check(name, False, 'no error')
        except WsClosed:
            check(name, True)

    print("\n6. timeouts: idle goes to the caller, mid-frame is retried")
    try:
        ws_read_message(Dribble(frame(WS_OP_TEXT, b'x'), timeouts_at={0}))
        check("idle timeout raised", False)
    except socket.timeout:
        check("idle timeout raised to the caller", True)
    data = frame(WS_OP_BINARY, b'y' * 300)
    msg = ws_read_message(Dribble(data, step=100, timeouts_at={2, 100}))
    check("two timeouts inside a frame are survived", msg == (WS_OP_BINARY, b'y' * 300))
    try:
        ws_read_message(Dribble(data, step=10, timeouts_at={18, 28, 38, 48}))
        check("endless mid-frame timeouts give up", False)
    except WsClosed:
        check("endless mid-frame timeouts give up", True)


# -- End to end ---------------------------------------------------------------

class FakeMic:
    def __init__(self):
        self.client_connected = False
        self.audio, self.events = [], []
        self.allow_key = True

    def key(self):
        self.events.append('key')
        return self.allow_key

    def unkey(self, why):
        self.events.append(f'unkey:{why}')

    def push_audio(self, pcm):
        self.audio.append(pcm)


def serve(parent):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == '/ws_mic':
                web_routes_stream.handle_ws_mic(self, parent)
            elif self.path == '/ws/link':
                web_routes_stream.handle_ws_link(self, parent)
            else:
                self.send_response(404)
                self.end_headers()
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def browser_connect(port, path):
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    s.sendall((f'GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n'
               'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
               'Sec-WebSocket-Version: 13\r\n\r\n').encode())
    resp = b''
    while b'\r\n\r\n' not in resp:
        resp += s.recv(1)
    return s, resp


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def end_to_end():
    print("\n7. /ws_mic with a browser-style (masking) client")
    mic = FakeMic()
    parent = types.SimpleNamespace(gateway=types.SimpleNamespace(web_mic_source=mic))
    srv = serve(parent)
    port = srv.server_address[1]
    s, resp = browser_connect(port, '/ws_mic')
    check("101 with the RFC 6455 accept key",
          b'101' in resp and b's3pPLMBiTxaQ9kYGzzhZRbK+xOo=' in resp, resp.decode(errors='replace'))
    s2, resp2 = browser_connect(port, '/ws_mic')
    check("second mic client refused with 409", b'409' in resp2.split(b'\r\n')[0], resp2[:40])
    s2.close()
    pcm = os.urandom(1920)
    # Header, mask and payload deliberately sent in separate segments.
    raw = ws_encode(WS_OP_BINARY, pcm, mask=True)
    for part in (raw[:1], raw[1:3], raw[3:6], raw[6:]):
        s.sendall(part)
        time.sleep(0.02)
    check("split binary frame delivered intact", wait_for(lambda: mic.audio == [pcm]),
          f"{len(mic.audio)} chunks")
    s.sendall(ws_encode(WS_OP_TEXT, b'KEY', mask=True))
    check("KEY reaches the mic source", wait_for(lambda: 'key' in mic.events), str(mic.events))
    mic.allow_key = False
    s.sendall(ws_encode(WS_OP_TEXT, b'KEY', mask=True))
    s.settimeout(3)
    check("refused key answers TOT", ws_read_message(s) == (WS_OP_TEXT, b'TOT'))
    s.sendall(frame(WS_OP_BINARY, pcm[:1000], fin=False) + frame(WS_OP_CONT, pcm[1000:]))
    check("fragmented audio reassembled", wait_for(lambda: len(mic.audio) == 2 and mic.audio[1] == pcm))
    s.sendall(ws_encode(WS_OP_PING, b'are you there', mask=True))
    fin, op, data = ws_read_frame(s)
    check("ping answered with pong", op == WS_OP_PONG and data == b'are you there')
    s.sendall(ws_encode(WS_OP_CLOSE, b'\x03\xe8', mask=True))
    fin, op, data = ws_read_frame(s)
    check("close answered", op == WS_OP_CLOSE)
    check("mic unkeyed and released on close",
          wait_for(lambda: not mic.client_connected and mic.events[-1] == 'unkey:socket closed'),
          str(mic.events))
    s.close()

    print("\n8. WebSocketTransport through the /ws/link bridge")
    P = GatewayLinkProtocol
    link = socket.socket()
    link.bind(('127.0.0.1', 0))
    link.listen(1)
    got = []

    def link_server():
        c, _ = link.accept()
        c.settimeout(5)
        while True:
            f = P.recv_frame(c)
            if f is None:
                break
            got.append(f)
            P.send_frame(c, f[0], f[1][::-1])   # echo, reversed
        c.close()
    threading.Thread(target=link_server, daemon=True).start()
    parent.gateway.config = types.SimpleNamespace(LINK_PORT=link.getsockname()[1])
    ws = WebSocketTransport()
    check("connects", ws.connect(f'ws://127.0.0.1:{port}/ws/link', timeout=5))
    big = os.urandom(9600)
    ws.send_frame(P.AUDIO, big)
    reply = ws.recv_frame()
    check("9.6 KB audio frame crosses both ways", reply == (P.AUDIO, big[::-1]),
          str(reply and (reply[0], len(reply[1]))))
    ws.send_frame(P.COMMAND, b'{"cmd": "ping"}')
    check("command frame crosses", ws.recv_frame() == (P.COMMAND, b'{"cmd": "ping"}'[::-1]))
    ws.close()
    check("link server saw both frames, in order",
          wait_for(lambda: [f[0] for f in got] == [P.AUDIO, P.COMMAND]), str([f[0] for f in got]))
    srv.shutdown()


def main():
    codec()
    end_to_end()
    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
