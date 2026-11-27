"""DP8 denoise run on a remote worker (tools/denoise_worker.py on the macmini).

RemoteDP8Stream is a denoise *stream* (the interface make_denoise_stream
returns: process / process_mix / close / native_rate / dry_delay_samples),
registered in audio_util as engine 'dpdfnet_remote'. It slots into
AudioProcessor's existing delay line unchanged: the per-bus denoise thread
calls process_mix() for each chunk, and that call does one round trip to
the worker over a persistent TCP connection instead of running ONNX here.

The whole DPDFNet stream (48k<->8k resampling, model, dry-delay mix) runs
on the worker, so the gateway's cost is two socket calls per chunk. One TCP
connection is one stream state on the worker, so a reconnect is a reset.

Rules:
  * A miss is DRY, never local. A late reply, no worker, or a broken link
    returns the input unchanged. Falling back to local DP8 would put the
    load back on the gateway the moment the mini blinks.
  * process_mix() never blocks on a connect. Connecting (and the worker
    building its ONNX session) happens on a helper thread while chunks pass
    dry; the call itself waits at most REPLY_TIMEOUT_S for its reply.
  * Replies carry the chunk's sequence number; a reply that arrives after
    its chunk gave up is discarded, never played in the wrong slot.

Wire format (little-endian), one header + payload per frame:
    <4s magic> <u32 seq> <f32 value> <u32 payload_len> <payload>
  client DNHI  seq=PROTO  value=0        payload JSON {bus, rate, engine}
  server DNOK  seq=PROTO  value=init_ms  payload JSON {engine, dry_delay_samples}
  server DNER  seq=PROTO  value=0        payload JSON {error}
  client DNRQ  seq=n      value=wet mix  payload int16 PCM, 48 kHz mono
  server DNRS  seq=n      value=proc_ms  payload int16 PCM, same length
"""

import json
import socket
import struct
import threading
import time
import weakref

import numpy as np

PROTO = 1
HDR = struct.Struct('<4sIfI')
MAX_PAYLOAD = 1 << 20

M_HELLO, M_OK, M_ERR, M_REQ, M_RSP = b'DNHI', b'DNOK', b'DNER', b'DNRQ', b'DNRS'


def tune_socket(sock):
    """Low latency + dead-peer detection. A bus that is squelched sends
    nothing for minutes, so silence can't mean "dead"; the kernel's
    keepalive probes (10 s idle, 3 x 3 s) and a 5 s unacked-data timeout
    find a vanished peer instead. Without this a link that died mid-stream
    stayed ESTABLISHED on the worker forever and its stream slot leaked."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (('TCP_KEEPIDLE', 10), ('TCP_KEEPINTVL', 3), ('TCP_KEEPCNT', 3),
                     ('TCP_USER_TIMEOUT', 5000)):
        if hasattr(socket, opt):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass


def pack(magic, seq, value, payload=b''):
    return HDR.pack(magic, seq & 0xFFFFFFFF, float(value), len(payload)) + payload


class FrameReader:
    """Accumulates bytes from a socket and yields whole frames."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def read(self, deadline=None):
        """Next (magic, seq, value, payload). Raises socket.timeout at the
        deadline (monotonic), ConnectionError on EOF."""
        while True:
            if len(self.buf) >= HDR.size:
                magic, seq, value, n = HDR.unpack_from(self.buf)
                if n > MAX_PAYLOAD:
                    raise ConnectionError(f'frame too large ({n})')
                if len(self.buf) >= HDR.size + n:
                    payload = bytes(self.buf[HDR.size:HDR.size + n])
                    del self.buf[:HDR.size + n]
                    return magic, seq, value, payload
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout('reply deadline')
                self.sock.settimeout(left)
            else:
                self.sock.settimeout(None)
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError('connection closed')
            self.buf += data


# ── Gateway side ───────────────────────────────────────────────────────────

_streams = weakref.WeakSet()


def status():
    return [s.stats() for s in list(_streams)]


class RemoteDP8Stream:
    native_rate = 48000
    dry_delay_samples = 2520          # the worker's DP8 stream; dry fallback is undelayed

    # The delay line gives a chunk DN_LAG ticks (100 ms) from enqueue; the
    # measured LAN round trip is ~3 ms on Wi-Fi plus ~15 ms of inference.
    REPLY_TIMEOUT_S = 0.08
    CONNECT_TIMEOUT_S = 1.0
    HELLO_TIMEOUT_S = 10.0            # worker builds + warms an ONNX session
    RETRY_S = 3.0                     # between connect attempts while down
    MAX_CONSEC_TIMEOUTS = 40          # 2 s of no replies: assume wedged, reconnect

    def __init__(self, registry=None, bus='unknown'):
        if registry is None:
            import remote_workers
            registry = remote_workers.REGISTRY
        self._registry = registry
        self.bus = bus
        self._lock = threading.Lock()   # guards _sock/_reader swaps vs the connect thread
        self._sock = None
        self._reader = None
        self._worker = ''
        self._connecting = False
        self._next_try = 0.0
        self._closed = False
        self._seq = 0
        self._consec_timeouts = 0
        self.counts = {'ok': 0, 'timeout': 0, 'down': 0, 'error': 0,
                       'stale': 0, 'connects': 0}
        self.last_error = ''
        self.rtt_ms_avg = None
        self.rtt_ms_max = 0.0
        self.proc_ms_avg = None
        _streams.add(self)

    # -- connection management ------------------------------------------------

    def _start_connect(self):
        now = time.monotonic()
        if self._connecting or now < self._next_try or self._closed:
            return
        self._next_try = now + self.RETRY_S
        w = self._registry.pick('denoise')
        if w is None:
            self.last_error = 'no denoise worker registered'
            return
        self._connecting = True
        threading.Thread(target=self._connect, args=(w,), daemon=True,
                         name=f'DenoiseConnect-{self.bus}').start()

    def _connect(self, w):
        sock = None
        try:
            hostport = w['url'].split('://', 1)[1]
            host, port = hostport.rsplit(':', 1)
            host = host.strip('[]')
            sock = socket.create_connection((host, int(port)), timeout=self.CONNECT_TIMEOUT_S)
            tune_socket(sock)
            hello = json.dumps({'bus': self.bus, 'rate': self.native_rate,
                                'engine': 'dpdfnet'}).encode()
            sock.settimeout(self.CONNECT_TIMEOUT_S)
            sock.sendall(pack(M_HELLO, PROTO, 0.0, hello))
            reader = FrameReader(sock)
            magic, _seq, init_ms, payload = reader.read(time.monotonic() + self.HELLO_TIMEOUT_S)
            if magic != M_OK:
                err = payload.decode('utf-8', 'replace')
                raise ConnectionError(f'worker refused: {err}')
            with self._lock:
                if self._closed:
                    raise ConnectionError('stream closed')
                self._drop_locked()
                self._sock, self._reader = sock, reader
                self._worker = w['name']
                self._consec_timeouts = 0
                self.counts['connects'] += 1
            sock = None
            print(f"  [DFN] {self.bus}: remote denoise connected to {w['name']} "
                  f"({w['url']}, stream ready in {init_ms:.0f} ms)")
            self.last_error = ''
        except Exception as e:
            self.last_error = f"{w.get('name', '?')}: {e}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            self._connecting = False

    def _drop_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._reader = None

    def _fail(self, why):
        with self._lock:
            was = self._sock is not None
            self._drop_locked()
        self.last_error = why
        self._next_try = time.monotonic() + self.RETRY_S
        if was:
            print(f"  [DFN] {self.bus}: remote denoise link to {self._worker} lost ({why}) "
                  f"— bus plays dry until it reconnects")

    # -- stream interface -----------------------------------------------------

    def _count(self, result):
        self.counts[result] += 1
        try:
            import metrics as _m
            _m.denoise_remote_total.labels(bus=self.bus, result=result).inc()
        except Exception:
            pass

    def process(self, samples_i16):
        return self.process_mix(samples_i16, 1.0)

    def process_mix(self, samples_i16, mix):
        samples_i16 = np.asarray(samples_i16, dtype=np.int16)
        with self._lock:
            sock, reader = self._sock, self._reader
        if sock is None:
            self._start_connect()
            self._count('down')
            return samples_i16
        seq = self._seq = (self._seq + 1) & 0xFFFFFFFF
        t0 = time.monotonic()
        try:
            sock.settimeout(self.REPLY_TIMEOUT_S)
            sock.sendall(pack(M_REQ, seq, max(0.0, min(1.0, float(mix))),
                              samples_i16.astype('<i2').tobytes()))
            deadline = t0 + self.REPLY_TIMEOUT_S
            while True:
                magic, rseq, proc_ms, payload = reader.read(deadline)
                if magic != M_RSP:
                    raise ConnectionError(f'unexpected frame {magic!r}')
                if rseq == seq:
                    break
                self._count('stale')       # reply to a chunk that already gave up
        except socket.timeout:
            self._count('timeout')
            self._consec_timeouts += 1
            if self._consec_timeouts >= self.MAX_CONSEC_TIMEOUTS:
                self._fail(f'{self._consec_timeouts} replies in a row missed the deadline')
            return samples_i16
        except Exception as e:
            self._count('error')
            self._fail(str(e) or type(e).__name__)
            return samples_i16
        self._consec_timeouts = 0
        rtt = (time.monotonic() - t0) * 1000.0
        self.rtt_ms_avg = rtt if self.rtt_ms_avg is None else 0.95 * self.rtt_ms_avg + 0.05 * rtt
        self.rtt_ms_max = max(self.rtt_ms_max, rtt)
        self.proc_ms_avg = (proc_ms if self.proc_ms_avg is None
                            else 0.95 * self.proc_ms_avg + 0.05 * proc_ms)
        try:
            import metrics as _m
            _m.denoise_remote_rtt_ms.labels(bus=self.bus).observe(rtt)
        except Exception:
            pass
        out = np.frombuffer(payload, dtype='<i2').astype(np.int16)
        if out.size != samples_i16.size:
            self._count('error')
            return samples_i16
        self._count('ok')
        return out

    def close(self):
        self._closed = True
        with self._lock:
            self._drop_locked()

    def stats(self):
        with self._lock:
            connected = self._sock is not None
        r = lambda v: None if v is None else round(v, 1)  # noqa: E731
        return {'bus': self.bus, 'worker': self._worker, 'connected': connected,
                'counts': dict(self.counts), 'rtt_ms_avg': r(self.rtt_ms_avg),
                'rtt_ms_max': r(self.rtt_ms_max), 'proc_ms_avg': r(self.proc_ms_avg),
                'last_error': self.last_error}
