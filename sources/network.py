"""Network-fed sources: EchoLink, the remote audio client/server pair,
gateway-link endpoints, network announcements, and Mumble."""

from log_util import log_exc
import time
import threading
import queue as _queue_mod
import socket
import numpy as np

# Shared level-metering and gain helpers
from audio_util import (
    pcm_level, pcm_db,
)

from sources.base import AudioSource


class EchoLinkSource(AudioSource):
    """EchoLink audio input via TheLinkBox IPC"""
    def __init__(self, config, gateway):
        super().__init__("EchoLink", config)
        self.gateway = gateway
        self.priority = 2  # After Radio (1), before Files (0)
        self.ptt_control = True  # EchoLink doesn't trigger radio PTT
        self.volume = 1.0
        
        # IPC state
        self.rx_pipe = None
        self.tx_pipe = None
        self.connected = False
        self.last_audio_time = 0
        # Carries the remainder of a short pipe read to the next get_audio.
        self._sub_buffer = b''
        
        # Try to setup IPC
        if config.ENABLE_ECHOLINK:
            self.setup_ipc()
    
    def setup_ipc(self):
        """Setup named pipes for TheLinkBox IPC"""
        import os
        import errno
        
        try:
            rx_path = self.config.ECHOLINK_RX_PIPE
            tx_path = self.config.ECHOLINK_TX_PIPE
            
            # Create named pipes if they don't exist
            for pipe_path in [rx_path, tx_path]:
                if not os.path.exists(pipe_path):
                    try:
                        os.mkfifo(pipe_path)
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  Created FIFO: {pipe_path}")
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
            
            # Open pipes (non-blocking mode)
            import fcntl
            
            # RX pipe (read from TheLinkBox)
            self.rx_pipe = open(rx_path, 'rb', buffering=0)
            flags = fcntl.fcntl(self.rx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.rx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            # TX pipe (write to TheLinkBox)
            self.tx_pipe = open(tx_path, 'wb', buffering=0)
            flags = fcntl.fcntl(self.tx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.tx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            self.connected = True
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"  ✓ EchoLink IPC connected via named pipes")
                print(f"    RX: {rx_path}")
                print(f"    TX: {tx_path}")
            
        except Exception as e:
            print(f"  ⚠ EchoLink IPC setup failed: {e}")
            print(f"    Make sure TheLinkBox is running and configured")
            self.connected = False
    
    def get_audio(self, chunk_size):
        """Get audio from EchoLink via named pipe"""
        if not self.connected or not self.rx_pipe:
            return None, False
        
        try:
            chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2  # 16-bit

            # A non-blocking pipe read returns whatever is buffered, which is
            # very often less than a full chunk. The old code compared
            # `len(data) == chunk_bytes` and threw everything else away, so
            # any partial read silently dropped that audio and the stream
            # arrived chopped. Accumulate instead and only serve whole chunks.
            data = self.rx_pipe.read(chunk_bytes)
            if data:
                self._sub_buffer += data
            if len(self._sub_buffer) < chunk_bytes:
                return None, False
            out = self._sub_buffer[:chunk_bytes]
            self._sub_buffer = self._sub_buffer[chunk_bytes:]

            self.last_audio_time = time.time()

            # Apply volume
            if self.volume != 1.0:
                arr = np.frombuffer(out, dtype=np.int16).astype(np.float32)
                out = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

            return out, False  # No PTT control

        except BlockingIOError:
            # No data available (non-blocking read)
            return None, False
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Read error: {e}")
            return None, False
    
    def send_audio(self, audio_data):
        """Send audio to EchoLink via named pipe"""
        if not self.connected or not self.tx_pipe:
            return
        
        try:
            self.tx_pipe.write(audio_data)
            self.tx_pipe.flush()
        except BlockingIOError:
            # Pipe full, skip this chunk
            pass
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Write error: {e}")
    
    def is_active(self):
        """EchoLink is active if we've received audio recently"""
        if not self.connected:
            return False
        return (time.time() - self.last_audio_time) < 2.0
    
    def cleanup(self):
        """Close IPC connections"""
        if self.rx_pipe:
            try:
                self.rx_pipe.close()
            except Exception:
                pass
        if self.tx_pipe:
            try:
                self.tx_pipe.close()
            except Exception:
                pass




class RemoteAudioServer:
    """Connects out to a remote client and sends mixed audio over TCP.

    REMOTE_AUDIO_HOST = destination IP of the client machine.
    The server initiates the TCP connection and pushes length-prefixed PCM.
    Reconnects automatically if the link drops.
    """
    def __init__(self, config):
        self.config = config
        self.host = config.REMOTE_AUDIO_HOST
        self.port = int(config.REMOTE_AUDIO_PORT)
        self.connected = False
        self.client_address = None  # "host:port" when connected
        self._socket = None
        self._connect_thread = None
        self._running = False
        self._reconnect_interval = float(getattr(config, 'REMOTE_AUDIO_RECONNECT_INTERVAL', 5.0))
        # Unsent remainder of a partially-sent frame. The protocol is
        # length-prefixed, so a frame must either finish or the connection
        # must reset — dropping a half-sent frame desyncs the receiver's
        # framing permanently (it reads PCM bytes as length headers).
        # Bounded: always < one frame.
        self._tx_backlog = b''

    def start(self):
        """Spawn connection thread that connects out to the client."""
        if not self.host:
            print("⚠ REMOTE_AUDIO_HOST not set — server has no destination to connect to")
            return
        self._running = True
        self._connect_thread = threading.Thread(
            target=self._connect_loop, name="RemoteAudio-connect", daemon=True
        )
        self._connect_thread.start()
        print(f"✓ Remote audio server will connect to {self.host}:{self.port}")

    def _connect_loop(self):
        """Connect to the client, reconnect on failure."""
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((self.host, self.port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setblocking(False)  # non-blocking so send_audio never stalls audio loop
                self._tx_backlog = b''   # stale partial frame belongs to the old connection
                self._socket = sock
                self.client_address = f"{self.host}:{self.port}"
                self.connected = True
                print(f"\n[RemoteAudio] Connected to client {self.client_address}")
                # Stay in this loop until disconnect is detected.
                # Probe the socket every 0.5s with select() — if the remote
                # end closes, the socket becomes readable (recv returns b'').
                # This catches disconnects even when send_audio() isn't called
                # (e.g. VAD gating all audio as silence).
                while self._running and self.connected:
                    try:
                        import select as _sel
                        readable, _, _ = _sel.select([sock], [], [], 0.5)
                        if readable:
                            # Socket readable on a send-only link = remote closed
                            probe = sock.recv(1)
                            if not probe:
                                break  # clean close
                    except Exception:
                        break  # error = dead
            except Exception:
                log_exc()
            finally:
                self.connected = False
                self.client_address = None
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
            if self._running:
                time.sleep(self._reconnect_interval)

    def send_audio(self, pcm_data):
        """Send length-prefixed PCM to connected client.

        Non-blocking, framing-safe: a partially-sent frame is parked in
        _tx_backlog and resumed on the next call before anything new goes
        out. (The old code dropped the REST of a half-sent frame on
        backpressure, which desynced the receiver's length-prefix framing
        — every later frame decoded as garbage until reconnect.) When a
        backlog is standing, NEW frames are dropped whole — the receiver
        misses 50ms instead of losing the stream.
        """
        sock = self._socket
        if not sock:
            self._tx_backlog = b''
            return
        import struct
        try:
            # Finish any partially-sent frame first.
            if self._tx_backlog:
                self._tx_backlog = self._send_some(sock, self._tx_backlog)
                if self._tx_backlog:
                    return  # still backed up — drop the new frame whole
            frame = struct.pack('>I', len(pcm_data)) + pcm_data
            self._tx_backlog = self._send_some(sock, frame)
        except Exception:
            # Link broken — trigger reconnect
            self.connected = False
            self._socket = None
            self._tx_backlog = b''
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _send_some(sock, buf):
        """Non-blocking send of *buf*; returns the unsent remainder (b'' if
        fully sent). Raises on a broken connection."""
        while buf:
            try:
                n = sock.send(buf)
            except BlockingIOError:
                break  # TCP buffer full — caller keeps the remainder
            if n == 0:
                raise ConnectionError("send returned 0")
            buf = buf[n:]
        return buf

    def reset(self):
        """Force-close the current connection so _connect_loop reconnects."""
        sock = self._socket
        self._socket = None
        self.connected = False
        self.client_address = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def cleanup(self):
        """Close socket."""
        self._running = False
        self.connected = False
        sock = self._socket
        self._socket = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


class RemoteAudioSource(AudioSource):
    """Listens for a TCP connection from a RemoteAudioServer and receives audio.

    REMOTE_AUDIO_HOST = bind address ('' or unset → 0.0.0.0, all interfaces).
    The server connects in; this end accepts and reads length-prefixed PCM.

    Name starts with 'SDR' so the mixer's duck system automatically handles it
    the same way it handles SDR1/SDR2 sources.
    """
    def __init__(self, config, gateway):
        super().__init__("SDRSV", config)
        self.gateway = gateway
        self.priority = 2  # Same as SDR sources in the mixer
        self.sdr_priority = int(config.REMOTE_AUDIO_PRIORITY)
        self.ptt_control = True
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = config.REMOTE_AUDIO_DUCK
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.server_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=32)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None
        self._conn = None  # current accepted connection (for reset)
        # Jitter buffer: prime with N chunks (~50 ms each) before draining so
        # WASAPI callback-gap spikes don't underrun the bus. Modeled on
        # LinkAudioSource. Un-primes only on disconnect; transient underruns
        # emit silence but stay primed. 8 chunks = ~400 ms initial cushion,
        # maxsize=32 = ~1.6 s absorption capacity for bus-side stalls.
        # 8 chunks = ~400 ms. Higher than the link default because this feed
        # comes from a Windows client whose WASAPI callback gaps are much
        # burstier than a link endpoint's. maxsize=32 caps absorption at
        # ~1.6 s. Clamped to [1, 31]: 0 defeats the buffer, >= maxsize can
        # never prime.
        try:
            _pf = int(getattr(config, 'REMOTE_AUDIO_JITTER_PREFILL', 8))
        except (TypeError, ValueError):
            _pf = 8
        self._jitter_prefill = max(1, min(_pf, 31))
        self._jitter_primed = False

    def setup_audio(self, port_override=None):
        """Bind listen socket and start the reader/accept thread."""
        bind_host = '0.0.0.0'
        port = int(port_override) if port_override else int(self.config.REMOTE_AUDIO_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="SDRSV-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Remote audio client listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept connections from the server and read length-prefixed PCM."""
        import struct

        while self._reader_running:
            # Wait for the server to connect in
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self._conn = conn
                self.server_connected = True
                print(f"\n[SDRSV] Server connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    # Read 4-byte length header
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break  # sanity check
                    # Read PCM payload
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break

                    # Track level in reader thread (works even when not on a bus)
                    try:
                        self.audio_level = pcm_level(payload, self.audio_level)
                    except Exception:
                        log_exc()

                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        # Drop oldest to keep queue fresh
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
                    if (not self._jitter_primed and
                            self._chunk_queue.qsize() >= self._jitter_prefill):
                        self._jitter_primed = True
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[SDRSV] Connection error: {e}")
            finally:
                self.server_connected = False
                self._conn = None
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def reset(self):
        """Force-close the current connection so the reader thread re-accepts."""
        conn = self._conn
        self._conn = None
        self.server_connected = False
        self._sub_buffer = b''
        self._jitter_primed = False
        # Drain the queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes from socket, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Drain queue, slice sub-buffer, level metering, audio boost."""
        if not self.enabled:
            return None, False

        # Skip queue lock entirely when not connected — nothing to drain.
        # Un-prime so the next connection starts with a fresh prefill cushion.
        if not self.server_connected and not self._sub_buffer:
            self._jitter_primed = False
            return None, False

        # Wait until the jitter buffer is primed before delivering audio.
        # Single transient underruns (below) emit silence for that tick but
        # leave _jitter_primed alone — only disconnect re-arms it.
        if not self._jitter_primed:
            return None, False

        cb = self._chunk_bytes

        # Fill sub-buffer from queue
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Muted: keep draining but discard
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering and audio boost
        arr = np.frombuffer(raw, dtype=np.int16)
        if len(arr) > 0:
            display_gain = float(self.config.REMOTE_AUDIO_DISPLAY_GAIN)
            self.audio_level = pcm_level(raw, self.audio_level, gain=display_gain)

            audio_boost = float(self.config.REMOTE_AUDIO_AUDIO_BOOST)
            if audio_boost != 1.0:
                farr = arr.astype(np.float32)
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        return raw, False  # Never triggers PTT

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "SDRSV: Disabled"
        elif self.muted:
            return "SDRSV: Muted"
        elif self.server_connected:
            return f"SDRSV: Connected ({self.audio_level}%)"
        else:
            return "SDRSV: Disconnected"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
            if self._reader_thread.is_alive():
                print(f"[RemoteAudioSource] Warning: reader thread did not stop within 2s")
        self._sub_buffer = b''




class LinkAudioSource(AudioSource):
    """Audio source for Gateway Link — receives duplex audio from a remote endpoint.

    Similar to RemoteAudioSource but fed by GatewayLinkServer's frame dispatch.
    The server calls push_audio() when AUDIO frames arrive from the endpoint.
    """

    def __init__(self, config, gateway, endpoint_name="default"):
        super().__init__(f"LINK:{endpoint_name}", config)
        self.gateway = gateway
        self.endpoint_name = endpoint_name
        self.priority = int(getattr(config, 'LINK_AUDIO_PRIORITY', 3))
        self.sdr_priority = self.priority
        self.ptt_control = True
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = getattr(config, 'LINK_AUDIO_DUCK', False)
        self.tx_audio_boost = 1.0     # separate TX gain for put_audio path
        self.tx_audio_level = 0       # TX level 0-100 (updated in put_audio)
        self.audio_boost = float(getattr(config, 'LINK_AUDIO_BOOST', 1.0))
        self.display_gain = float(getattr(config, 'LINK_AUDIO_DISPLAY_GAIN', 1.0))
        self.server_connected = False
        self.muted = False
        self.audio_level = 0
        self._audio_level_last_mono = 0.0
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(config, 'AUDIO_CHANNELS', 1) * 2  # 50 ms, 16-bit
        # Jitter buffer. Single producer (link reader thread → push_audio)
        # and single consumer (bus tick → get_audio). deque's append/popleft
        # are individually atomic under the GIL; compound ops (e.g. len()
        # followed by popleft) are NOT safe. Do not add a second producer or
        # consumer without switching to queue.Queue.
        self._chunk_queue = _queue_mod.deque(maxlen=16)
        self._sub_buffer = b''
        self._link_server = None  # Set by gateway_core after init
        # Chunks (50 ms each) to accumulate before draining. This is the
        # dominant tunable in end-to-end latency: 4 = 200 ms of cushion.
        # A wired LAN endpoint can usually run 2; keep 4+ for WiFi or a
        # Cloudflare-tunnelled endpoint. Tune with evidence, not taste —
        # rg_link_audio_underruns_total counts what a too-low value costs.
        # Per-endpoint override lives in link_endpoint_settings and is
        # applied by set_jitter_prefill() once the endpoint registers.
        self._jitter_prefill = self._clamp_prefill(
            getattr(config, 'LINK_JITTER_PREFILL', 4))
        self._last_push_mono = time.monotonic()  # track last audio arrival for underrun timeout
        # Device identity — set by _link_on_register, used by all consumers
        self.source_id = None      # routing source ID, e.g. "d75", "ftm_150"
        self.sink_id = None        # routing TX sink ID, e.g. "d75_tx", "ftm_150_tx"
        self.plugin_type = None    # device class from REGISTER, e.g. "d75", "aioc", "audio"
        self._jitter_primed = False

    def _clamp_prefill(self, value):
        """Keep prefill in [1, queue_maxlen-1].

        0 would defeat the buffer entirely — every scheduling hiccup becomes
        a dropout. Above maxlen the queue can never reach the threshold, so
        the source would never prime and would stay silent forever. Exactly
        maxlen does prime, but only with the queue completely full, i.e. at
        the point where the next push starts dropping the oldest chunk — so
        the ceiling is maxlen-1 to keep one chunk of headroom.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = 4
        return max(1, min(v, (self._chunk_queue.maxlen or 16) - 1))

    def set_jitter_prefill(self, value):
        """Change the prefill depth at runtime (per-endpoint tuning).

        Takes effect on the next prime — i.e. after the current transmission
        ends — so it never chops audio that is already flowing.
        """
        old = self._jitter_prefill
        self._jitter_prefill = self._clamp_prefill(value)
        if self._jitter_prefill != old:
            print(f"  [Link:{self.endpoint_name}] jitter prefill "
                  f"{old} -> {self._jitter_prefill} chunks "
                  f"({self._jitter_prefill * 50} ms cushion)")
        return self._jitter_prefill

    def setup_audio(self):
        return True

    def flush_buffers(self):
        """Flush jitter buffer and sub-buffer. Called on playback stop or bus change."""
        self._chunk_queue.clear()
        self._sub_buffer = b''
        self._jitter_primed = False
        self._last_push_mono = time.monotonic()

    def push_audio(self, pcm):
        """Called by GatewayLinkServer reader thread when AUDIO frame arrives."""
        _st = getattr(self, '_stream_trace', None)
        _qd = len(self._chunk_queue)
        self._chunk_queue.append(pcm)
        if not self._jitter_primed and len(self._chunk_queue) >= self._jitter_prefill:
            self._jitter_primed = True
        if _st and _st.active:
            _extra = f'overflow' if _qd >= self._chunk_queue.maxlen else ''
            _st.record(f'{self.endpoint_name}_rx', 'push_audio', pcm, _qd, _extra)
        try:
            self.audio_level = pcm_level(pcm, self.audio_level)
            self._audio_level_last_mono = time.monotonic()
            self._last_push_mono = time.monotonic()
        except Exception:
            log_exc()

    def meter_level(self):
        # When this source is a sink-only endpoint (not a member of any bus),
        # get_audio() is never called, so audio_level never decays. Return 0
        # if no push_audio in the last 250 ms so the dashboard meter falls
        # back to silence instead of freezing at the last pre-squelch value.
        if time.monotonic() - getattr(self, '_audio_level_last_mono', 0) > 0.25:
            return 0
        return self.audio_level

    def get_audio(self, chunk_size):
        _st = getattr(self, '_stream_trace', None)
        if not self.enabled or self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False
        if not self.server_connected:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            self._jitter_primed = False
            return None, False

        # Jitter buffer: wait for prefill before starting to drain
        if not self._jitter_primed:
            return None, False

        cb = self._chunk_bytes
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.popleft()
                self._sub_buffer += blob
            except IndexError:
                self.audio_level = max(0, int(self.audio_level * 0.7))
                # If no audio has arrived for >2s, the source went legitimately
                # quiet (e.g. squelch gating). Drop the primed flag so we stop
                # counting underruns until audio resumes and re-primes.
                if time.monotonic() - self._last_push_mono > 2.0:
                    self._jitter_primed = False
                    self._sub_buffer = b''
                    return None, False
                if _st and _st.active:
                    _st.record(f'{self.endpoint_name}_rx', 'get_audio', None,
                               0, 'UNDERRUN')
                try:
                    import metrics as _m
                    _m.link_audio_underruns_total.labels(endpoint=self.endpoint_name).inc()
                except Exception:
                    log_exc()
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        if _st and _st.active:
            _st.record(f'{self.endpoint_name}_rx', 'get_audio', raw,
                       len(self._chunk_queue))

        # Level metering — no VAD gate here, bus handles that
        self.audio_level = pcm_level(raw, self.audio_level, gain=self.display_gain)

        # Audio boost
        if self.audio_boost != 1.0:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            arr = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16)
            raw = arr.tobytes()

        return raw, False

    def write_tx_audio(self, pcm):
        """Send gateway audio to the remote endpoint."""
        if self._link_server and self._link_server.connected:
            try:
                self._link_server.send_audio(pcm)
            except Exception:
                log_exc()

    def put_audio(self, pcm):
        """Send TX audio to the remote endpoint (SoloBus radio interface)."""
        if self.gateway and self.gateway.link_server:
            try:
                if self.tx_audio_boost != 1.0:
                    _arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    pcm = np.clip(_arr * self.tx_audio_boost, -32768, 32767).astype(np.int16).tobytes()
                self.gateway.link_server.send_audio_to(self.endpoint_name, pcm)
                self.tx_audio_level = pcm_level(pcm, self.tx_audio_level)
                _st = getattr(self, '_stream_trace', None)
                if _st and _st.active:
                    _st.record(f'{self.endpoint_name}_tx', 'put_audio', pcm)
            except Exception:
                log_exc()

    def execute(self, cmd):
        """Route commands to the remote endpoint via link server (SoloBus radio interface)."""
        _srv = self.gateway.link_server if self.gateway else None
        if _srv:
            try:
                print(f"  [LinkSrc:{self.endpoint_name}] execute: {cmd}")
                _srv.send_command_to(self.endpoint_name, cmd)
                return {"ok": True}
            except Exception as e:
                print(f"  [LinkSrc:{self.endpoint_name}] execute error: {e}")
                return {"ok": False, "error": str(e)}
        print(f"  [LinkSrc:{self.endpoint_name}] execute: no link server (gw={self.gateway is not None})")
        return {"ok": False, "error": "link server not available"}

    def ptt_on(self):
        self.execute({'cmd': 'ptt', 'state': True})

    def ptt_off(self):
        self.execute({'cmd': 'ptt', 'state': False})

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "LINK: disabled"
        return f"LINK: {'connected' if self.server_connected else 'disconnected'}"


class NetworkAnnouncementSource(AudioSource):
    """Listens for an inbound TCP connection on port 9601 and receives PCM
    audio to transmit over the radio.

    Same wire format as RemoteAudioSource (length-prefixed 16-bit mono PCM at
    the configured sample rate).  Unlike RemoteAudioSource, ptt_control=True so
    the mixer routes the audio to radio TX and activates PTT.  PTT is released
    automatically by the gateway's PTT_RELEASE_DELAY timeout once the queue
    drains after the sender disconnects.
    """
    def __init__(self, config, gateway):
        super().__init__("ANNIN", config)
        self.gateway = gateway
        self.priority = 0           # Same highest priority as FilePlayback
        self.ptt_control = True     # Routes to radio TX and activates PTT
        self.volume = float(getattr(config, 'ANNOUNCE_INPUT_VOLUME', 4.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2   # 16-bit mono
        self._ptt_hold_time = 2.0   # seconds of silence before releasing PTT
        self._last_above_threshold = 0.0  # monotonic time of last above-threshold chunk
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None

    def setup_audio(self):
        """Bind listen socket and start accept/reader thread."""
        bind_host = self.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
        port = int(self.config.ANNOUNCE_INPUT_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="ANNIN-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Announcement input listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept one client at a time and read length-prefixed PCM."""
        import struct

        while self._reader_running:
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self.client_connected = True
                print(f"\n[ANNIN] Client connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break
                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Connection error: {e}")
            finally:
                self.client_connected = False
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Client disconnected")

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Return (pcm, True) when above-threshold audio is available.

        Silence frames are consumed from the queue but discarded (return
        (None, False)) so PTT is not triggered by idle stream packets.
        A 2-second hold keeps PTT active through brief pauses in speech
        so the radio doesn't drop and re-key between sentences.
        """
        if not self.enabled or self.muted:
            return None, False

        cb = self._chunk_bytes
        now = time.monotonic()

        # Fill sub-buffer from queue — always drain so idle silence doesn't
        # back up the queue while the connection is held open.
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                # No data in queue — check if PTT hold is still active
                if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                    return b'\x00' * cb, True  # silence but keep PTT keyed
                self.audio_level = 0
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering + threshold gate
        self.audio_level = pcm_level(raw, self.audio_level)
        db = pcm_db(raw)

        threshold_db = float(getattr(self.config, 'ANNOUNCE_INPUT_THRESHOLD', -45.0))
        if db < threshold_db:
            # Below threshold — hold PTT with silence for up to 2s
            if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                return b'\x00' * cb, True  # silence but keep PTT keyed
            self.audio_level = 0
            return None, False  # Hold expired: let PTT release

        # Above threshold — update hold timer
        self._last_above_threshold = now

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, True   # Above threshold: route to radio TX and activate PTT

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "ANNIN: Disabled"
        elif self.client_connected:
            return f"ANNIN: Connected ({self.audio_level}%)"
        else:
            return "ANNIN: Waiting"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
            if self._reader_thread.is_alive():
                print(f"[ANNIN] Warning: reader thread did not stop within 2s")
        self._sub_buffer = b''


class MumbleSource(AudioSource):
    """Receives Mumble RX audio and feeds it into the bus system.

    The Mumble sound_received_handler pushes PCM into a queue.
    get_audio() pulls from the queue with ptt_control=True so the
    bus knows to key the radio when Mumble audio is active.
    """
    def __init__(self, config, gateway=None):
        super().__init__("MUMBLE_RX", config)
        self.gateway = gateway
        self.priority = 0  # Highest — Mumble audio takes priority
        self.ptt_control = True
        self.volume = 1.0
        self.enabled = True
        self.muted = False
        self.audio_level = 0
        self.audio_boost = float(getattr(config, 'OUTPUT_VOLUME', 1.0))
        self.vad_threshold_db = float(getattr(config, 'MUMBLE_VAD_THRESHOLD', -40.0))

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(config, 'AUDIO_CHANNELS', 1) * 2

    def push_audio(self, pcm_bytes):
        """Called by sound_received_handler to push Mumble RX audio."""
        try:
            self.audio_level = pcm_level(pcm_bytes, self.audio_level)
        except Exception:
            log_exc()
        try:
            self._chunk_queue.put_nowait(pcm_bytes)
        except _queue_mod.Full:
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(pcm_bytes)
            except _queue_mod.Full:
                pass

    def get_audio(self, chunk_size):
        if not self.enabled or self.muted:
            return None, False

        cb = self._chunk_bytes  # target chunk size in bytes

        # Accumulate Mumble frames into sub-buffer
        if not hasattr(self, '_sub_buffer'):
            self._sub_buffer = b''

        _drained = 0
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
                _drained += 1
            except _queue_mod.Empty:
                break

        if len(self._sub_buffer) < cb:
            return None, False

        # Full chunk available — no padding, no clicks
        data = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Apply volume
        if self.audio_boost != 1.0:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            data = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Level metering + VAD
        try:
            self.audio_level = pcm_level(data, self.audio_level)
            vad_pass = pcm_db(data) > self.vad_threshold_db
        except Exception:
            vad_pass = False
        return data, vad_pass

    def is_active(self):
        return self.enabled and not self.muted and not self._chunk_queue.empty()

    def get_status(self):
        if not self.enabled:
            return "MUMBLE_RX: Disabled"
        return f"MUMBLE_RX: {self.audio_level}%"

    def cleanup(self):
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
