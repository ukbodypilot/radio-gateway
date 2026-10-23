"""StreamOutputSource: the Broadcastify / Icecast MP3 stream sink."""

from log_util import log_exc
import os
import time
import threading
import socket
import select

# Shared level-metering and gain helpers



class StreamOutputSource:
    """Direct Icecast streaming — PCM → ffmpeg MP3 → Icecast HTTP SOURCE.

    Replaces the old DarkIce/FFmpeg/ALSA loopback chain with a single
    in-process pipeline. No external processes needed.
    """
    SILENCE_INTERVAL = 0.05  # seconds between silence frames (50ms = 20 ticks/sec)
    SUPERVISOR_INTERVAL = 10.0  # seconds between independent stream-health checks

    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        self.connected = False
        self._encoder = None      # ffmpeg subprocess
        self._icecast_sock = None  # TCP socket to Icecast
        # Dual-channel (Broadcastify two-scanner) feed. When on, the bus manager
        # combines the broadcastify_l / broadcastify_r sinks into interleaved
        # stereo before enqueueing, and the encoder is told to expect 2 channels.
        # Broadcastify specifies 16 kbps per scanner, so a dual feed is 32 kbps
        # total — the same per-channel rate as the mono feed, not a step up in
        # quality per channel.
        self._dual = bool(getattr(config, 'STREAM_DUAL_CHANNEL', True))
        self._channels = 2 if self._dual else 1
        # Keepalive silence must match whatever the encoder expects, so it
        # doubles in dual mode. Getting this wrong desynchronises the channels
        # for the rest of the connection: a half-length write leaves the
        # encoder mid-frame and every later sample lands in the wrong channel.
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2 * self._channels
        self._lock = threading.Lock()
        # Serialise writes to the ffmpeg encoder's stdin. Two threads write
        # PCM into it: send_audio() (called from BusManager / sink-drain
        # thread) and _keepalive_loop (silence when idle). Concurrent writes
        # interleave bytes mid-sample and the MP3 output decodes as static
        # / "silence" — see v3.5-A regression notes (2026-05-02).
        self._encoder_lock = threading.Lock()
        # Guards the _reconnecting flag only. Deliberately separate from
        # _encoder_lock so the recovery path can never be blocked by a writer
        # that is stuck in the encoder — that coupling is what turned a
        # routine socket drop into a 15h outage on 2026-07-30.
        self._reconnect_lock = threading.Lock()
        # Serialises the DESTRUCTIVE half of a reconnect (close() + _connect()).
        # _connect() writes shared instance state — _encoder, _icecast_sock,
        # connected — so two workers running it at once corrupt each other:
        # one connects, the other's close() drops that fresh connection, and
        # its own connect is then refused "403 Mountpoint in use" because the
        # server still holds the mount it just dropped. That is precisely the
        # 2026-08-19 flap: 1850 attempts over 3h42, attempt numbers printing
        # out of order (#79, #83, #72, #85) because ~8 workers were live at
        # once. _reconnect_lock guards a bool and is held for microseconds;
        # this one is held across a network round trip, so they must stay
        # separate or the trigger path would block on the connect path.
        self._connect_lock = threading.Lock()
        # How long a worker waits for _connect_lock before retiring. Longer
        # than the wedge timeout so a worker normally outlasts a slow
        # predecessor, but bounded so threads cannot pile up without limit.
        self._connect_lock_wait = 45.0
        # Bumped whenever the watchdog force-releases _reconnecting out from
        # under a still-live worker. A worker whose captured epoch no longer
        # matches has been superseded and must retire without touching the
        # connection. See _trigger_reconnect's watchdog for why.
        self._reconnect_epoch = 0
        # Instrumentation for the paths added 2026-08-20 — without these the
        # only evidence that the fix is working is an absence of log spam,
        # which is indistinguishable from the feature never running.
        self._reconnect_superseded = 0
        self._reconnect_wedged = 0
        # Watchdog patience before a reconnect worker is declared wedged.
        # This is the BASE budget: _connect_confirm is added to it
        # in _trigger_reconnect: the confirmation wait happens inside the
        # worker, and a watchdog that fires because of it would declare every
        # slow-but-fine reconnect "wedged".
        self._reconnect_wedge_timeout = 30.0
        self._reader_thread = None
        self._keepalive_thread = None
        self._last_audio_time = 0  # monotonic time of last real audio push
        self._bytes_sent = 0
        # Monotonic time of the last byte written to the Icecast socket.
        # 0 until the first byte of the first connection goes out.
        self._last_bytes_time = 0.0
        self._connect_time = 0
        self._reconnect_backoff = 5
        # Longer wait used only after a "403 Mountpoint in use" rejection, i.e.
        # when the server is still holding our own previous source connection.
        # Measured clear time was under 10s; 15 gives headroom without making
        # ordinary reconnects sluggish. Configurable because a different Icecast
        # host may reap sources on a different schedule.
        self._mount_wait = float(getattr(config, 'STREAM_MOUNT_WAIT', 15.0))
        # True when the server may still be holding our previous source
        # connection: set on a detected drop AND on a 403 mount-in-use refusal,
        # cleared by a successful connect.
        self._mount_in_use = False
        # Last failure, surfaced on the dashboard. Kept even after a
        # successful reconnect: 'it broke at 03:12 and recovered' is the
        # thing you want to see hours later, and clearing it on recovery
        # would hide every transient drop that happened overnight.
        self._last_error = ''
        self._last_error_time = 0.0   # unix epoch, 0 = never
        self._reconnect_count = 0
        self._last_drop_time = 0   # monotonic time of last connection drop
        self._was_connected = False  # True once first successful connection
        # Reconnect-in-flight guard. Previously this only ever existed via
        # getattr(self, '_reconnecting', False), so it was invisible in
        # __init__ and easy to miss when reasoning about the recovery path.
        self._reconnecting = False
        # Supervisor thread — the reconnect trigger of last resort. See
        # _supervisor_loop for why keepalive alone was not enough.
        self._supervisor_thread = None
        # Set once the gateway is shutting this source down for good. Stops
        # the reader/supervisor from helpfully resurrecting the stream while
        # the process is on its way out.
        self._shutdown = False
        # True while a teardown we asked for is in progress (close(), and
        # therefore reconnect() too). The reader thread dies as a *result* of
        # such a teardown, so without this it would treat our own deliberate
        # close as a drop and queue a second, redundant reconnect.
        self._teardown_intentional = False
        # Hard ceiling on how long a single write into the encoder may take
        # before we declare the encoder wedged and tear it down. See
        # _encoder_write. Must stay well under STREAM_MOUNT_WAIT so a wedged
        # encoder is reaped long before the reconnect delay elapses.
        self._encoder_write_timeout = float(
            getattr(config, 'STREAM_ENCODER_WRITE_TIMEOUT', 1.0))
        # How long a fresh connection gets to push its first byte before we
        # stop calling it a success. The keepalive feeds the encoder every
        # SILENCE_INTERVAL (50 ms) whether or not the radio is busy, so a
        # healthy 32 kbps mount hands the reader a 4096-byte chunk about once
        # a second -- 5 s is ~5x that, and a quiet channel is NOT a reason for
        # this to come up empty.
        self._connect_confirm = float(
            getattr(config, 'STREAM_CONNECT_CONFIRM', 5.0))
        # Longest gap between outgoing bytes that still counts as flowing.
        # Compared against the same ~1 s cadence, so this is 15x margin.
        self._flow_stale_after = float(
            getattr(config, 'STREAM_FLOW_STALE_AFTER', 15.0))

        if config.ENABLE_STREAM_OUTPUT:
            self._connect()

    def _connect(self):
        """Connect to Icecast and start the MP3 encoder pipeline."""
        import base64

        server = getattr(self.config, 'STREAM_SERVER', '')
        port = int(getattr(self.config, 'STREAM_PORT', 8000))
        mount = getattr(self.config, 'STREAM_MOUNT', '/stream')
        password = getattr(self.config, 'STREAM_PASSWORD', '')
        bitrate = int(getattr(self.config, 'STREAM_BITRATE', 16))
        name = getattr(self.config, 'STREAM_NAME', 'Radio Gateway')
        # ice-description is listed in the Icecast SOURCE spec alongside
        # ice-name/ice-genre/ice-url; we had the config value all along
        # and simply never sent it.
        description = getattr(self.config, 'STREAM_DESCRIPTION', '') or name
        # Encoder output sample rate. MUST stay below 32 kHz: at 32/44.1/48 kHz
        # the encoder is MPEG-1 Layer III, whose LOWEST Layer III bitrate is
        # 32 kbps, so lame silently clamps anything smaller and -b:a is ignored.
        # Broadcastify specifies 16 kbps for a single scanner (32 kbps for a
        # dual-channel feed, i.e. the same 16k per scanner), and 16 kbps only
        # exists in MPEG-2 Layer III — 16/22.05/24 kHz. Measured 2026-07-28:
        #   48 kHz  asked 16k -> 32.3 kbps   (and asked 32k -> 32.3 kbps too)
        #   22.05kHz asked 16k -> 16.3 kbps  <- on spec
        # 22.05 kHz carries 11 kHz of audio; narrowband FM voice is ~3 kHz, so
        # nothing audible is lost — the old 48 kHz feed was spending half its
        # bitrate on empty spectrum the radio never produced.
        out_rate = int(getattr(self.config, 'STREAM_SAMPLE_RATE', 22050))

        if not server or not password:
            print("  ⚠ Broadcastify: missing server or password")
            self._note_error("missing server or password")
            return

        # Connect TCP to Icecast
        try:
            sock = socket.create_connection((server, port), timeout=10)
            # Send SOURCE request (Icecast SOURCE protocol)
            auth = base64.b64encode(f"source:{password}".encode()).decode()
            headers = (
                f"SOURCE {mount} HTTP/1.0\r\n"
                f"Authorization: Basic {auth}\r\n"
                f"Content-Type: audio/mpeg\r\n"
                f"ice-name: {name}\r\n"
                f"ice-public: 1\r\n"
                f"ice-bitrate: {bitrate}\r\n"
                f"ice-description: {description}\r\n"
                # ice-audio-info is how a source DECLARES its format. Without it
                # the server has only the MP3 frames to go on: Broadcastify's
                # feed page showed Sample Rate 0, Bitrate 0, Channels 1 — those
                # zeros are "unknown", not a misread, since decoding the frames
                # would have yielded 22050 and 32. The audio was always right
                # (verified 2026-07-28 against the live mount: channels=2,
                # audio on L while R stayed at exactly zero).
                #
                # BOTH key dialects are sent. Icecast's own docs give two
                # real-world examples with different names —
                #   LadioCast: samplerate=44100;quality=10%2e0;channels=2
                #   Butt:      ice-bitrate=128;ice-channels=2;ice-samplerate=44100
                # — and Icecast passes the pairs through rather than
                # normalising them.
                #
                # CONFIRMED 2026-07-28: Broadcastify reads the ice- prefixed
                # (Butt) dialect. A build sending ONLY those keys took their
                # feed page from Sample Rate 0 / Bitrate 0 / Channels 1 to
                # 22050 / 32 / 2. The bare keys are therefore not required for
                # Broadcastify and are kept only so this works against Icecast
                # servers that read the other dialect — this is a public
                # project and not everyone streams to Broadcastify. Unknown
                # keys are ignored by any parser, so the cost is a few bytes
                # once per connect.
                f"ice-audio-info: samplerate={out_rate};channels={self._channels};"
                f"bitrate={bitrate};ice-samplerate={out_rate};"
                f"ice-channels={self._channels};ice-bitrate={bitrate}\r\n"
                f"\r\n"
            )
            sock.sendall(headers.encode())

            # Read response
            resp = b''
            sock.settimeout(5)
            try:
                while b'\r\n\r\n' not in resp and len(resp) < 1024:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    resp += chunk
            except socket.timeout:
                pass

            resp_str = resp.decode(errors='replace')
            if '200' not in resp_str.split('\n')[0]:
                print(f"  ⚠ Broadcastify: Icecast rejected connection: {resp_str.strip()}")
                self._note_error(f"Icecast rejected: {resp_str.splitlines()[0].strip()}")
                # "403 Mountpoint in use" is not a real failure — it means the
                # server has not yet reaped OUR previous source connection, so
                # retrying sooner cannot possibly work. Flag it so the reconnect
                # worker waits for the mount to clear instead of burning an
                # attempt. Any other rejection (bad password, wrong mount) is a
                # genuine error and keeps the short retry.
                self._mount_in_use = ('mountpoint in use' in resp_str.lower())
                sock.close()
                return
            self._mount_in_use = False

            sock.settimeout(None)
            self._icecast_sock = sock

        except Exception as e:
            print(f"  ⚠ Broadcastify: connection failed: {e}")
            self._note_error(f"connection failed: {e}")
            return

        # Start ffmpeg MP3 encoder: PCM stdin → MP3 stdout
        import subprocess as sp
        try:
            self._encoder = sp.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                # Input: raw PCM as the gateway produces it — 48 kHz, mono, or
                # interleaved stereo when the dual-channel feed is enabled.
                '-f', 's16le', '-ar', '48000', '-ac', str(self._channels),
                '-i', 'pipe:0',
                # Output: resampled — this -ar is AFTER -i so it sets the
                # ENCODER rate, not the input rate. Without it the encoder runs
                # at 48 kHz, forcing MPEG-1 and a 32 kbps floor. See out_rate.
                '-c:a', 'libmp3lame', '-ar', str(out_rate), '-b:a', f'{bitrate}k',
            ] + ([
                # Two independent receivers are UNCORRELATED, so joint stereo
                # (which codes mid/side to exploit L/R similarity) wastes bits
                # and can smear one channel into the other. Plain stereo keeps
                # them separate, which is the whole point of a dual feed.
                '-joint_stereo', '0',
            ] if self._dual else []) + [
                '-flush_packets', '1',
                '-fflags', '+nobuffer',
                '-f', 'mp3', 'pipe:1'
            ], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE)
            # Make the encoder's stdin NON-BLOCKING. A blocking write into a
            # full pipe is what deadlocked the whole stream on 2026-07-30:
            # ffmpeg stopped draining stdin (because nobody was draining its
            # stdout after the reader thread died), the sink thread parked
            # forever inside stdin.write() while holding _encoder_lock, and
            # the keepalive thread — the only thing that can trigger a
            # reconnect — parked behind that same lock. 15h of dead air with
            # _reconnect_count stuck at 0. With O_NONBLOCK the write fails
            # fast instead, and _encoder_write turns that into a teardown.
            try:
                import fcntl
                _fd = self._encoder.stdin.fileno()
                fcntl.fcntl(_fd, fcntl.F_SETFL,
                            fcntl.fcntl(_fd, fcntl.F_GETFL) | os.O_NONBLOCK)
            except Exception as e:
                # Not fatal — we still have the deadline in _encoder_write,
                # it just degrades to a blocking write we cannot interrupt.
                print(f"  ⚠ Broadcastify: could not set encoder stdin non-blocking: {e}")
            self._start_encoder_stderr_reader()
        except Exception as e:
            print(f"  ⚠ Broadcastify: ffmpeg encoder failed: {e}")
            self._note_error(f"ffmpeg encoder failed: {e}")
            self._icecast_sock.close()
            self._icecast_sock = None
            return

        # Prometheus label for this stream. Resolved once per connect rather
        # than per chunk — it cannot change without reconnecting.
        _metric_stream = mount or 'icecast'

        # Reader thread: reads MP3 from ffmpeg, sends to Icecast
        # Bind this reader to the encoder and socket of THIS connection.
        # It used to reach through self._encoder / self._icecast_sock on every
        # iteration, so after a reconnect swapped those fields a surviving
        # reader from the previous connection would read the NEW encoder's
        # stdout and push it into the NEW socket — two readers draining one
        # pipe, interleaving MP3 frames. Defaults capture the values now.
        def _reader(enc=self._encoder, sock=self._icecast_sock):
            while enc.poll() is None:
                try:
                    data = enc.stdout.read(4096)
                    if not data:
                        break
                    with self._lock:
                        if self._icecast_sock is sock:
                            sock.sendall(data)
                            self._bytes_sent += len(data)
                            # Timestamp of the last byte that actually reached
                            # the server. `connected` only ever meant "the
                            # SOURCE handshake was accepted", which is not the
                            # same claim — see _confirm_bytes_moving.
                            self._last_bytes_time = time.monotonic()
                            # Count bytes HERE, at the point they actually go
                            # out. This used to be done in
                            # stream_stats.get_stream_stats(), which has no
                            # background caller — it only runs when a web
                            # request asks for gateway status. The counter
                            # therefore sat flat between dashboard loads and
                            # then jumped by the whole backlog at once (seen
                            # 2026-07-28: flat 20 min, then +86,691,840 in one
                            # step), so rate() read 0 for long stretches. That
                            # made the 'broadcastify_stream_down' alert in
                            # alerts.py fire 4-11 times a day on a perfectly
                            # healthy stream, and made
                            # manager_engine's stream_throughput_kbps equally
                            # wrong. Never derive a rate metric from a
                            # pull-driven status call.
                            try:
                                import metrics as _m
                                _m.stream_bytes_sent_total.labels(
                                    stream=_metric_stream).inc(len(data))
                            except Exception:
                                log_exc()  # metrics must never break the stream
                except (BrokenPipeError, OSError, ConnectionError) as e:
                    uptime_s = int(time.time() - self._connect_time) if self._connect_time else 0
                    print(f"  [Broadcastify] Connection lost after {uptime_s}s: {e}")
                    self._note_error(f"connection lost after {uptime_s}s: {e}")
                    self.connected = False
                    self._last_drop_time = time.monotonic()
                    # Our source connection just died, so the server may still
                    # be holding the mount. Assume so until a connect proves
                    # otherwise — retrying before it is reaped only earns a
                    # "403 Mountpoint in use" and burns an attempt.
                    self._mount_in_use = True
                    break
                except Exception as e:
                    print(f"  [Broadcastify] Reader error: {e}")
                    self._note_error(f"reader error: {e}")
                    break
            # A reader belongs to exactly ONE connection. By the time this one
            # unwinds, a reconnect may already have published a newer encoder —
            # and then everything below would be acting on a connection this
            # thread has nothing to do with: flipping `connected` false, killing
            # the live encoder and queueing yet another reconnect. Whose reader
            # would in turn do the same to ITS successor.
            #
            # That is the self-sustaining half of the 2026-08-19 flap: 1850
            # reconnects over 3h42, of which only 26 were watchdog-driven. The
            # watchdog race started it; this is what kept it going, one
            # "Reconnected successfully" immediately murdered by the previous
            # connection's reader, ~900 times over. A stale reader must exit
            # silently and let the current generation live.
            if self._encoder is not enc:
                return
            # Clean up on exit
            if self.connected:
                uptime_s = int(time.time() - self._connect_time) if self._connect_time else 0
                print(f"  [Broadcastify] Reader thread exited (was connected {uptime_s}s)")
                self._last_drop_time = time.monotonic()
                self._mount_in_use = True   # same reasoning as above
            self.connected = False
            # Reap the encoder. THIS thread is ffmpeg's only stdout consumer,
            # so once it exits ffmpeg's 64 KiB stdout pipe fills, ffmpeg
            # blocks on write, and it therefore stops reading stdin — which
            # in turn wedges every PCM writer feeding it. Leaving the encoder
            # alive here is what deadlocked the stream on 2026-07-30 (ffmpeg
            # was still running 40h later, holding both pipes). Kill it as
            # part of the same drop that killed this thread.
            self._teardown_encoder()
            # And make sure something is actually driving recovery: the
            # supervisor would catch this within SUPERVISOR_INTERVAL anyway,
            # but asking here makes reconnection immediate on the common path.
            # Skipped when WE tore the encoder down — this thread dying is the
            # expected consequence of close()/reconnect(), not a fault.
            if not self._teardown_intentional:
                self._trigger_reconnect()

        self._reader_thread = threading.Thread(target=_reader, daemon=True,
                                                name="Broadcastify-sender")
        self._reader_thread.start()
        # A live encoder again — any future reader death is a genuine fault.
        self._teardown_intentional = False
        self.connected = True
        self._was_connected = True
        self._connect_time = time.time()
        self._last_audio_time = time.monotonic()
        self._bytes_sent = 0
        # Start the flow clock now: a connection one tick old has not gone
        # stale, it simply has not had time to push anything yet.
        self._last_bytes_time = time.monotonic()

        # Keepalive: feed silence to encoder when no real audio arrives
        if not self._keepalive_thread or not self._keepalive_thread.is_alive():
            self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True,
                                                       name="Broadcastify-keepalive")
            self._keepalive_thread.start()
        # Independent recovery supervisor — see _supervisor_loop. Started here
        # rather than in __init__ so it only exists once streaming is actually
        # live, and guarded the same way as the keepalive thread so repeated
        # reconnects do not stack up copies of it.
        if not self._supervisor_thread or not self._supervisor_thread.is_alive():
            self._supervisor_thread = threading.Thread(target=self._supervisor_loop,
                                                       daemon=True,
                                                       name="Broadcastify-supervisor")
            self._supervisor_thread.start()
        print(f"  ✓ Broadcastify: direct Icecast stream to {server}:{port}{mount} ({bitrate}kbps)")

    def _encoder_write(self, data):
        """Write PCM into the encoder under a hard deadline.

        Returns True if every byte went in, False if the encoder is wedged.

        The encoder's stdin is O_NONBLOCK (set in _connect), so a full pipe
        surfaces as BlockingIOError instead of parking the calling thread
        forever. That is the fix for the 2026-07-30 deadlock: ffmpeg stopped
        draining stdin, and the plain `stdin.write()` this replaces blocked
        indefinitely *while holding _encoder_lock*, taking the recovery path
        down with it.

        A partial write here desynchronises the channels for the rest of the
        connection (see _chunk_bytes), so the caller MUST treat False as
        "reap the encoder and reconnect", never as "retry this frame".
        """
        enc = self._encoder
        if enc is None or enc.stdin is None:
            return False
        try:
            fd = enc.stdin.fileno()
        except Exception:
            return False
        view = memoryview(data)
        deadline = time.monotonic() + self._encoder_write_timeout
        while view:
            try:
                view = view[os.write(fd, view):]
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    select.select([], [fd], [], min(remaining, 0.05))
                except Exception:
                    return False
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True

    def _teardown_encoder(self):
        """Kill the ffmpeg encoder. Deliberately does NOT take _encoder_lock.

        The entire point is to break threads that are already stuck inside
        that lock, so taking it here would deadlock against the very
        condition we are clearing. SIGKILL makes any in-flight write fail
        with EPIPE, which releases the lock for us.
        """
        enc = self._encoder
        if enc is None:
            return
        if self._encoder is enc:
            self._encoder = None
        for step in (enc.kill, enc.stdin.close, enc.stdout.close):
            try:
                step()
            except Exception:
                log_exc()
        try:
            enc.wait(timeout=3)
        except Exception:
            pass

    def _supervisor_loop(self):
        """Reconnect trigger of last resort.

        Recovery used to hang solely off _keepalive_loop — but that thread is
        itself a writer into the encoder, so any condition that wedged the
        encoder also wedged the only thing able to recover from it. On
        2026-07-30 that cost 15h of dead air with _reconnect_count stuck at 0:
        the keepalive thread was parked on _encoder_lock and never looped back
        to re-read `self.connected`.

        This thread touches neither the encoder nor _encoder_lock, so it stays
        responsive no matter what the writer threads are doing.
        """
        while True:
            time.sleep(self.SUPERVISOR_INTERVAL)
            try:
                if self._shutdown:
                    return
                if not self._was_connected or self.connected or self._reconnecting:
                    continue
                # A reconnect worker holding the connect lock may be part-way
                # through building a fresh encoder, which it has not yet
                # published by setting `connected`. Reaping it here would
                # destroy the very recovery we are trying to drive, so leave
                # an in-flight connect alone and re-check next interval.
                if self._connect_lock.locked():
                    continue
                if self._encoder is not None:
                    # Stream is down but the encoder is still alive — writers
                    # may be parked inside it. Reap it before retrying.
                    print("  [Broadcastify] Supervisor: stream down with encoder "
                          "still alive — reaping it")
                    self._teardown_encoder()
                print("  [Broadcastify] Supervisor: stream down — triggering reconnect")
                self._trigger_reconnect()
            except Exception as e:
                print(f"  [Broadcastify] Supervisor error: {e}")

    def _trigger_reconnect(self):
        """Start a reconnect attempt unless one is already in flight.

        Extracted from send_audio so that every detector — the sink thread,
        the keepalive tick and the supervisor — shares one guarded path
        rather than each open-coding the flag handling.
        """
        if not self._was_connected or self._shutdown:
            return
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
            self._reconnect_count += 1
            count = self._reconnect_count
            epoch = self._reconnect_epoch
        try:
            import metrics as _m
            _m.stream_reconnects_total.labels(stream='broadcastify').inc()
        except Exception:
            log_exc()

        def _auto_reconnect():
            try:
                # Wait for the mount to clear when our own source connection
                # may still be held server-side — set both when a drop is
                # detected (the reader thread) and when a connect is refused
                # with "403 Mountpoint in use".
                # Observed 2026-07-27 (11:12 and 11:27): drop -> attempt #1 at
                # +5s refused 403 -> attempt #2 at +10s succeeded. The mount
                # clears somewhere under 10s, so the old flat 5s retry was
                # racing it and spending an attempt to learn nothing. Waiting
                # up front costs ~5s more dead air than the old two-attempt
                # recovery but reconnects on the FIRST try with no error
                # logged. The fast 5s path remains for connects that fail for
                # any other reason, where retrying sooner does help.
                delay = self._mount_wait if self._mount_in_use else self._reconnect_backoff
                if delay != self._reconnect_backoff:
                    print(f"  [Broadcastify] Mount still held by our previous "
                          f"connection — waiting {delay}s for it to clear")
                time.sleep(delay)
                # Everything below mutates shared connection state, so only one
                # worker may be inside it at a time. Waiting here (rather than
                # barging in) is what stops a late worker from closing a
                # connection a peer just established.
                if not self._connect_lock.acquire(timeout=self._connect_lock_wait):
                    self._reconnect_superseded += 1
                    print(f"  [Broadcastify] Attempt #{count} gave up waiting for "
                          f"the connect lock — another attempt is still in flight")
                    return
                try:
                    # Three ways this attempt can already be pointless. Checking
                    # them under the lock is what makes the check meaningful:
                    # the state cannot change while we hold it.
                    if self._shutdown:
                        return
                    if epoch != self._reconnect_epoch:
                        self._reconnect_superseded += 1
                        print(f"  [Broadcastify] Attempt #{count} superseded "
                              f"(epoch {epoch} < {self._reconnect_epoch}) — retiring "
                              f"without touching the connection")
                        return
                    if self.connected:
                        self._reconnect_superseded += 1
                        print(f"  [Broadcastify] Attempt #{count} unnecessary — "
                              f"stream is already back up")
                        return
                    print(f"  [Broadcastify] Auto-reconnecting (attempt #{count})...")
                    try:
                        self.close()
                    except Exception as e:
                        print(f"  [Broadcastify] close() raised during reconnect: {e}")
                    try:
                        self._connect()
                    except Exception as e:
                        print(f"  [Broadcastify] _connect() raised: {e}")
                    if not self.connected:
                        print(f"  [Broadcastify] Reconnect failed (attempt #{count})")
                        self._note_error(f"reconnect failed (attempt #{count})")
                    elif self._confirm_bytes_moving(count):
                        pass   # _confirm_bytes_moving logs the success itself
                    else:
                        # Handshake accepted, nothing moving. Deliberately NOT
                        # torn down here: a blocked write trips the 1 s
                        # _encoder_write deadline and _on_encoder_wedged
                        # already reaps and retries, with _supervisor_loop as
                        # the backstop. Both fired correctly throughout the
                        # 2026-08-21 stall -- the only thing broken was this
                        # message calling it a success. A teardown here would
                        # be a third recovery path racing the two that work.
                        print(f"  [Broadcastify] Attempt #{count} connected but "
                              f"no data moved in {self._connect_confirm:g}s — "
                              f"NOT counting this as recovered")
                        self._note_error(
                            f"attempt #{count} connected but pushed 0 bytes in "
                            f"{self._connect_confirm:g}s")
                finally:
                    self._connect_lock.release()
            finally:
                # Only clear the in-flight flag if we are still the current
                # generation. A superseded worker finishing late must not clear
                # the flag its successor is holding, or a third worker spawns
                # alongside the second and the serialisation is lost.
                with self._reconnect_lock:
                    if epoch == self._reconnect_epoch:
                        self._reconnecting = False

        worker = threading.Thread(target=_auto_reconnect, daemon=True,
                                  name="Broadcastify-reconnect")
        worker.start()

        # Watchdog: if the reconnect worker hangs (e.g. close() wedged on a
        # half-dead pipe), force-release the _reconnecting flag after 30s so a
        # later tick can spawn a fresh attempt. The wedged worker may leak its
        # encoder/socket, but the stream as a whole recovers instead of
        # staying dark.
        def _watchdog():
            budget = self._reconnect_wedge_timeout + self._connect_confirm
            worker.join(timeout=budget)
            if worker.is_alive():
                # Release the flag so recovery is not held hostage by this
                # worker, AND bump the epoch so the worker retires on wake.
                # The old code did only the first half, on the assumption that
                # a wedged worker is effectively dead. It is not always: a
                # worker parked in getaddrinfo during a DNS outage
                # (socket.create_connection's timeout covers the TCP connect,
                # NOT name resolution) comes back minutes later and resumes
                # tearing down whatever connection succeeded meanwhile. That
                # assumption is what turned a 4-minute DNS blip into a
                # 3h42 reconnect flap on 2026-08-19.
                with self._reconnect_lock:
                    self._reconnect_epoch += 1
                    self._reconnecting = False
                    ep = self._reconnect_epoch
                self._reconnect_wedged += 1
                print(f"  [Broadcastify] Reconnect attempt #{count} wedged >"
                      f"{budget:g}s — releasing flag "
                      f"(epoch now {ep}; attempt #{count} will retire on wake)")
        threading.Thread(target=_watchdog, daemon=True,
                         name="Broadcastify-reconnect-wd").start()

    def send_audio(self, audio_data):
        """Send raw PCM audio to the MP3 encoder. Auto-reconnects on failure."""
        if not self.connected or self._encoder is None:
            self._trigger_reconnect()
            return
        # The lock still serialises whole frames against _keepalive_loop, but
        # the write underneath it is now deadline-bounded, so holding it can
        # no longer park this thread (and everything queued behind it)
        # indefinitely.
        with self._encoder_lock:
            ok = self._encoder_write(audio_data)
            if ok:
                self._last_audio_time = time.monotonic()
        if not ok:
            self._on_encoder_wedged()

    def _confirm_bytes_moving(self, count):
        """Wait for a just-established connection to actually push bytes.

        `self.connected` only ever meant "the Icecast SOURCE handshake was
        accepted". That is a much weaker claim than "the stream is back", and
        on 2026-08-21 the difference cost ten minutes of dead air: the uplink
        stalled, ten reconnect attempts each completed their handshake, and
        every one of them logged "Reconnected successfully" while
        rg_stream_bytes_sent_total sat at a flat +0 for 2.5 of those minutes.
        TCP connected, Icecast accepted, not one payload byte moved. The log
        said recovered, the byte counter said dark, and only the counter was
        right -- the incident was invisible in the log and obvious in
        Prometheus.

        _connect() resets _bytes_sent to 0, so any advance seen here belongs
        to THIS connection and cannot be inherited from the last one.

        Returns True (and logs the success) once bytes move, False if the
        window expires, the connection drops again, or we are shutting down.
        """
        started = time.monotonic()
        deadline = started + self._connect_confirm
        while time.monotonic() < deadline:
            if self._shutdown:
                return False
            if not self.connected:
                return False       # dropped again mid-confirmation
            if self._bytes_sent > 0:
                took = time.monotonic() - started
                print(f"  [Broadcastify] Reconnected successfully (attempt "
                      f"#{count}) — {self._bytes_sent} bytes on the wire "
                      f"in {took:.1f}s")
                return True
            time.sleep(0.1)
        return False

    @property
    def data_flowing(self):
        """True when bytes reached the server recently enough to count as up.

        The companion to _confirm_bytes_moving for callers that poll rather
        than watch a single reconnect -- see the health check in
        core/lifecycle.py, which made the same connected-means-healthy
        assumption and so reported "Stream recovered" five times during the
        same stall.
        """
        if not self.connected:
            return False
        if not self._last_bytes_time:
            return False
        return (time.monotonic() - self._last_bytes_time) < self._flow_stale_after

    def _on_encoder_wedged(self):
        """Encoder stopped accepting PCM — reap it and start recovery.

        Called with _encoder_lock RELEASED: _teardown_encoder must not take
        it, and the reconnect worker needs a clean field.
        """
        if not self.connected and self._encoder is None:
            return  # someone else already handled this drop
        print("  [Broadcastify] Encoder stopped accepting audio — reaping and reconnecting")
        self._note_error("encoder wedged (write deadline exceeded)")
        self.connected = False
        self._last_drop_time = time.monotonic()
        # Our source connection is going away, so the server may still hold
        # the mount — same reasoning as the reader thread's drop path.
        self._mount_in_use = True
        self._teardown_encoder()
        self._trigger_reconnect()

    def _start_encoder_stderr_reader(self):
        """Drain and log the encoder's stderr.

        This used to be DEVNULL. When ffmpeg exited the reader loop simply saw
        EOF and logged "Reader thread exited" with no reason — the one piece of
        evidence that would explain an encoder death was being thrown away.

        It MUST be drained, not merely redirected: a PIPE nobody reads fills its
        64 KB kernel buffer and then blocks ffmpeg's next write forever, which
        would wedge the encoder. That is a worse failure than the missing logs,
        so the thread is started immediately after Popen and exits on EOF.

        ffmpeg runs at -loglevel error, so in normal operation this produces
        nothing at all.
        """
        enc = self._encoder
        if enc is None or enc.stderr is None:
            return

        def _drain(pipe):
            try:
                for raw in iter(pipe.readline, b''):
                    line = raw.decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    print(f"  [Broadcastify] ffmpeg: {line}")
                    self._note_error(f"ffmpeg: {line}")
            except Exception:
                log_exc()          # pipe closed under us during shutdown
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threading.Thread(target=_drain, args=(enc.stderr,), daemon=True,
                         name="Broadcastify-ffmpeg-err").start()

    def _note_error(self, msg):
        """Record the most recent stream failure for the dashboard."""
        try:
            self._last_error = str(msg)[:200]
            self._last_error_time = time.time()
        except Exception:
            log_exc()   # diagnostics must never break the stream

    def _keepalive_loop(self):
        """Feed silence to the encoder when no real audio is arriving.

        Icecast servers drop SOURCE connections that go idle.  By sending
        silence frames the MP3 encoder keeps producing a constant bitrate
        stream even when the radio is quiet.
        """
        # One bus tick of silence — matches the chunk size every other
        # source produces, so the encoder sees uniform input regardless of
        # whether real audio or keepalive is flowing.
        _silence = b'\x00' * self._chunk_bytes
        while True:
            time.sleep(self.SILENCE_INTERVAL)
            if not self.connected or self._encoder is None:
                # Keepalive also drives reconnection — send_audio() only fires
                # when the listen bus has audio, so quiet radios never retry.
                # _supervisor_loop is the backstop if this thread is ever
                # unable to reach here.
                self._trigger_reconnect()
                continue
            ok = True
            try:
                # Re-check the idle gate while holding the lock so we never
                # race with send_audio mid-frame; both writers always produce
                # whole 4800-byte frames into the encoder.
                #
                # BOUNDED acquire: an unbounded `with self._encoder_lock` is
                # exactly how this thread was lost on 2026-07-30 — it parked
                # here behind a sink thread stuck in a blocking write and
                # never returned to the `not self.connected` check above, so
                # the reconnect on the previous branch became unreachable.
                if not self._encoder_lock.acquire(timeout=1.0):
                    continue
                try:
                    if time.monotonic() - self._last_audio_time < 0.1:
                        continue
                    ok = self._encoder_write(_silence)
                finally:
                    self._encoder_lock.release()
            except Exception:
                log_exc()
            if not ok:
                self._on_encoder_wedged()

    def reconnect(self):
        """Tear down and reconnect."""
        self.close()
        time.sleep(1)
        self._connect()

    def close(self):
        """Clean shutdown."""
        self.connected = False
        # Tell the reader thread that its imminent death is our doing.
        # Cleared again by a successful _connect(); if the reconnect fails,
        # _supervisor_loop is the backstop and does not consult this flag.
        self._teardown_intentional = True
        # Kills the process FIRST, then closes the pipes. stdin.close() can
        # block indefinitely if the read end is in a half-dead state with
        # buffered bytes — observed wedging the reconnect path for 11+ hours
        # after a Broadcastify socket reset. SIGKILL guarantees the pipe is
        # torn down so the subsequent close() returns immediately.
        self._teardown_encoder()
        if self._icecast_sock:
            try:
                self._icecast_sock.close()
            except Exception:
                pass
            self._icecast_sock = None

    @property
    def uptime(self):
        """Seconds since connection."""
        return time.time() - self._connect_time if self._connect_time else 0

    @property
    def bytes_sent_mb(self):
        """MB sent."""
        return self._bytes_sent / (1024 * 1024)
    
    def cleanup(self):
        """Shut the stream down. Alias for close() — the gateway's shutdown
        path calls cleanup() on every source uniformly.

        This used to reference `self.pipe`, an attribute that has never
        existed on this class (it predates the DarkIce->direct-Icecast
        rewrite), so shutdown raised AttributeError here every single time
        and the encoder/socket were left to the OS instead of being closed.
        """
        # Permanent, unlike close() — suppresses the reader/supervisor
        # recovery paths so shutdown cannot race a reconnect back up.
        self._shutdown = True
        self.close()




# CW generation moved to audio_util.py — re-exported above for backward compat
