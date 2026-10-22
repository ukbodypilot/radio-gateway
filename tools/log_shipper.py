"""Endpoint log shipper — tees stdout/stderr through a bounded ring
buffer and ships line batches to the gateway over the existing
``gateway_link`` channel as P.LOG frames.

Local writes still go to the original stdout/stderr (tee, not replace),
so the endpoint's own systemd journal — if persistent — keeps its
copy too. The gateway gets a persistent copy across endpoint reboots.

See ``docs/endpoint_logs_design.md``.
"""

import collections
import sys
import threading
import time

from gateway_link import GatewayLinkProtocol


# Max lines held in the per-endpoint deque when the link is down. ~200 lines
# of typical 200-byte log → ~40 KB; 1000 lines → ~200 KB worst case. The
# deque silently drops oldest on overflow.
_DEFAULT_BUF_MAX = 1000

# Flush cadence — one LOG frame per second per endpoint regardless of log
# volume. Aligned with the gateway's status_interval cadence.
_FLUSH_INTERVAL = 1.0


class _TeeStream:
    """File-like wrapper that delegates to *orig* and also calls
    *capture(stream_name, text_chunk)*. Thread-safe."""

    def __init__(self, orig, capture, stream_name):
        self._orig = orig
        self._capture = capture
        self._stream_name = stream_name
        self._lock = threading.Lock()

    def write(self, s):
        # Always pass through to the original FD first — failure to capture
        # must never block local stdout. Errors in capture are swallowed.
        n = self._orig.write(s)
        try:
            with self._lock:
                self._capture(self._stream_name, s)
        except Exception:
            pass
        return n

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._orig.isatty()
        except Exception:
            return False

    # Forward anything else (e.g. encoding, buffer) to the original.
    def __getattr__(self, name):
        return getattr(self._orig, name)


class LogShipper:
    """Buffered shipper that captures stdout/stderr and forwards to the
    gateway as P.LOG frames.

    Lifecycle:
        shipper = LogShipper()        # installs tees immediately
        ... main() runs, plugin sets up, logs start collecting ...
        client = GatewayLinkClient(...)
        shipper.attach(client)        # starts the flush thread
        client.start()
    """

    def __init__(self, buf_max=_DEFAULT_BUF_MAX, flush_interval=_FLUSH_INTERVAL):
        self._buf = collections.deque(maxlen=buf_max)
        self._buf_lock = threading.Lock()
        self._flush_interval = flush_interval
        # Per-stream partial-line buffers — stdout/stderr writes don't
        # necessarily land on newline boundaries.
        self._partial = {'stdout': '', 'stderr': ''}
        self._client = None
        self._flush_thread = None
        self._stop = threading.Event()
        # Install tees right now so we capture from the moment the shipper
        # is constructed, not the moment the client attaches.
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _TeeStream(sys.stdout, self._capture, 'stdout')
        sys.stderr = _TeeStream(sys.stderr, self._capture, 'stderr')

    # -- internal ----------------------------------------------------------

    def _capture(self, stream_name, chunk):
        """Split *chunk* on newlines, append complete lines to the deque."""
        if not chunk:
            return
        buf = self._partial[stream_name] + chunk
        # rsplit on \n: everything except the last element is a complete line.
        parts = buf.split('\n')
        self._partial[stream_name] = parts[-1]
        if len(parts) == 1:
            return  # no newline yet
        now = time.time()
        with self._buf_lock:
            for line in parts[:-1]:
                if line.endswith('\r'):
                    line = line[:-1]
                self._buf.append({'ts': now, 'stream': stream_name, 'text': line})

    def _flush_loop(self):
        P = GatewayLinkProtocol
        while not self._stop.is_set():
            time.sleep(self._flush_interval)
            client = self._client
            if client is None or not client.connected:
                continue
            with self._buf_lock:
                if not self._buf:
                    continue
                batch = list(self._buf)
                self._buf.clear()
            # Link frames carry at most 64 KB. One oversize batch (a backlog
            # built up while disconnected) used to fail on every flush and be
            # re-queued forever, so the endpoint's logs never shipped again.
            # Send it as several frames, and cap any single huge line.
            import json as _json
            sent = 0
            try:
                for chunk in self._chunks(batch, P.MAX_PAYLOAD - 1024, _json):
                    client._send(P.LOG, _json.dumps({'type': 'log', 'lines': chunk}).encode('utf-8'))
                    sent += len(chunk)
            except Exception:
                # Re-queue what didn't go, at the front, oldest first.
                with self._buf_lock:
                    for line in reversed(batch[sent:]):
                        self._buf.appendleft(line)

    @staticmethod
    def _chunks(batch, limit, json_mod):
        """Yield sub-lists of *batch* whose JSON stays under *limit* bytes."""
        chunk, size = [], 40
        for line in batch:
            text = line.get('text', '')
            if len(text) > 8192:
                line = dict(line, text=text[:8192] + ' [truncated]')
            n = len(json_mod.dumps(line).encode('utf-8')) + 2
            if chunk and size + n > limit:
                yield chunk
                chunk, size = [], 40
            chunk.append(line)
            size += n
        if chunk:
            yield chunk

    # -- public API --------------------------------------------------------

    def attach(self, client):
        """Bind to a ``GatewayLinkClient`` and start the flush thread."""
        self._client = client
        if self._flush_thread is None or not self._flush_thread.is_alive():
            self._flush_thread = threading.Thread(
                target=self._flush_loop, daemon=True, name="LogShipper-flush",
            )
            self._flush_thread.start()

    def stop(self):
        """Stop the flush thread and restore original stdout/stderr."""
        self._stop.set()
        if sys.stdout is not self._orig_stdout:
            sys.stdout = self._orig_stdout
        if sys.stderr is not self._orig_stderr:
            sys.stderr = self._orig_stderr
