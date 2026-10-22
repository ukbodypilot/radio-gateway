"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""

import socket
import threading
import time


class _AGWPEProxyMixin:
    # ── Tunables ───────────────────────────────────────────────────
    # Cap on concurrent Pat ↔ Direwolf sessions. Pat normally only runs one,
    # but a stale or crashed browser session can leave a socket open while a
    # new one starts — the cap prevents runaway thread growth.
    _AGWPE_MAX_SESSIONS = 10
    _AGWPE_LOCAL_PORT = 8010
    _AGWPE_REMOTE_PORT = 8010
    _AGWPE_DIREWOLF_WAIT_SECS = 20.0
    _AGWPE_FORWARD_BUF = 4096

    def _start_agwpe_proxy(self):
        """Start a local TCP proxy on 127.0.0.1:8010 → endpoint AGWPE port.
        Pat connects here; we forward to whichever endpoint is the packet radio."""
        # Lock guarding _proxy_sessions_active. _agwpe_proxy_handle's inc/dec
        # was unsynchronised — multiple parallel sessions could read a stale
        # value in the cap check or in the session-end "restart Direwolf?"
        # decision.
        self._proxy_sessions_lock = threading.Lock()
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', self._AGWPE_LOCAL_PORT))
            srv.listen(2)
            srv.settimeout(1.0)
            self._agwpe_proxy_sock = srv
            threading.Thread(target=self._agwpe_proxy_loop, daemon=True,
                             name="agwpe-proxy").start()
            print(f"  [Packet] AGWPE proxy listening on 127.0.0.1:{self._AGWPE_LOCAL_PORT}")
        except OSError as e:
            print(f"  [Packet] AGWPE proxy failed to start: {e}")

    def _agwpe_proxy_loop(self):
        """Accept connections on local AGWPE proxy and forward to endpoint.

        If the endpoint isn't in data mode when Pat connects, automatically
        switches to data mode and waits for Direwolf's AGW port to open
        (up to 20 seconds).
        """
        srv = self._agwpe_proxy_sock
        while self._running and srv:
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._agwpe_proxy_handle,
                             args=(client, addr), daemon=True,
                             name="agwpe-proxy-conn").start()

    def _agwpe_proxy_handle(self, client, addr):
        """Handle one Pat → endpoint AGWPE forwarding session."""
        with self._proxy_sessions_lock:
            if self._proxy_sessions_active >= self._AGWPE_MAX_SESSIONS:
                print(f"  [Packet] AGWPE proxy: session cap reached "
                      f"({self._proxy_sessions_active}), rejecting {addr}")
                try:
                    client.close()
                except Exception:
                    pass
                return
            self._proxy_sessions_active += 1
        try:
            self._agwpe_proxy_session(client, addr)
        finally:
            with self._proxy_sessions_lock:
                self._proxy_sessions_active -= 1

    def _agwpe_proxy_session(self, client, addr):
        """Inner session handler — called from _agwpe_proxy_handle."""
        ep_ip = self._get_endpoint_ip()
        if not ep_ip:
            print(f"  [Packet] AGWPE proxy: no endpoint available, rejecting {addr}")
            client.close()
            return

        # Auto-switch to data mode if needed so Direwolf starts
        if self._mode == 'idle':
            print(f"  [Packet] AGWPE proxy: Pat connected, auto-switching to winlink mode")
            self._set_mode('winlink')

        # Retry connecting to endpoint AGW port while Direwolf starts.
        remote = None
        deadline = time.monotonic() + self._AGWPE_DIREWOLF_WAIT_SECS
        attempt = 0
        while time.monotonic() < deadline and self._running:
            attempt += 1
            try:
                ep_ip = self._get_endpoint_ip()  # re-resolve in case endpoint reconnected
                r = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                r.settimeout(2.0)
                r.connect((ep_ip, self._AGWPE_REMOTE_PORT))
                r.settimeout(None)  # clear connect timeout for data transfer
                remote = r
                break
            except Exception:
                if attempt == 1:
                    print(f"  [Packet] AGWPE proxy: waiting for Direwolf on "
                          f"{ep_ip}:{self._AGWPE_REMOTE_PORT}...")
                time.sleep(1.0)

        if remote is None:
            print(f"  [Packet] AGWPE proxy: Direwolf not ready on "
                  f"{ep_ip}:{self._AGWPE_REMOTE_PORT} after "
                  f"{self._AGWPE_DIREWOLF_WAIT_SECS:.0f}s, rejecting")
            client.close()
            return

        print(f"  [Packet] AGWPE proxy: connected {addr} → "
              f"{ep_ip}:{self._AGWPE_REMOTE_PORT} (attempt {attempt})")
        done = threading.Event()
        _t0 = time.monotonic()
        # Per-frame trace is off by default — Pat sessions send hundreds of
        # small frames and the trace floods the log. Set
        # PACKET_AGWPE_TRACE = True in gateway_config.txt to enable.
        _trace = bool(getattr(self._config, 'PACKET_AGWPE_TRACE', False)) if self._config else False

        def _fwd(src, dst, label):
            _bytes = 0
            _frames = 0
            _last_t = _t0
            try:
                while self._running:
                    data = src.recv(self._AGWPE_FORWARD_BUF)
                    if not data:
                        elapsed = time.monotonic() - _t0
                        print(f"  [Packet] AGWPE [{label}]: EOF after {_frames}f "
                              f"{_bytes}B {elapsed:.1f}s", flush=True)
                        break
                    _bytes += len(data)
                    _frames += 1
                    if _trace:
                        now = time.monotonic()
                        gap = now - _last_t
                        _last_t = now
                        elapsed = now - _t0
                        print(f"  [Packet] AGWPE [{label}]: #{_frames} {len(data)}B "
                              f"t={elapsed:.1f}s gap={gap:.1f}s", flush=True)
                    dst.sendall(data)
            except Exception as _e:
                elapsed = time.monotonic() - _t0
                print(f"  [Packet] AGWPE [{label}]: ERR after {_frames}f "
                      f"{_bytes}B {elapsed:.1f}s: {_e}", flush=True)
            finally:
                # shutdown() before close() so Direwolf sees an immediate
                # FIN on the AGWPE socket and reaps its client slot.
                # Without this, sockets pile up in ESTABLISHED state on
                # the endpoint, eventually exhausting Direwolf's AGWPE
                # client pool — the 4th-or-so Pat connect then times out
                # with "context deadline exceeded".
                for _s in (src, dst):
                    try:
                        _s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        _s.close()
                    except Exception:
                        pass
                done.set()

        threading.Thread(target=_fwd, args=(client, remote, 'pat→dw'),
                         daemon=True).start()
        threading.Thread(target=_fwd, args=(remote, client, 'dw→pat'),
                         daemon=True).start()

        # Block until session ends — keeps _proxy_sessions_active > 0 while live
        done.wait()
        elapsed = time.monotonic() - _t0
        with self._proxy_sessions_lock:
            _active_now = self._proxy_sessions_active
        print(f"  [Packet] AGWPE proxy: session ended after {elapsed:.1f}s "
              f"(active_sessions={_active_now})", flush=True)

        # Forced Direwolf restart after a session ends. Workaround for
        # observed fragility where Pat's next KISS connection was racing
        # Direwolf's socket state. Set PACKET_DISABLE_FORCED_RESTART=True
        # in gateway_config.txt to skip — the state machine in
        # packet/state.py now surfaces transition failures explicitly,
        # so disabling this workaround no longer means silent breakage.
        disable_forced = bool(getattr(self._config, 'PACKET_DISABLE_FORCED_RESTART', False)) if self._config else False
        if self._mode in ('winlink', 'bbs') and not disable_forced:
            if _active_now > 1:
                print("  [Packet] AGWPE proxy: new session active — skipping restart")
            else:
                print("  [Packet] AGWPE proxy: restarting Direwolf for clean reconnect")
                self._send_endpoint_mode('data')
        elif self._mode in ('winlink', 'bbs') and disable_forced:
            print("  [Packet] AGWPE proxy: session ended — forced restart disabled")

