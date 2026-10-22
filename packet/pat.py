"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""

import time


class _PatMixin:
    # Cached pat binary path — set on first lookup, reused on every restart.
    # shutil.which is cheap but not free, and we used to call it on every
    # _start_pat. Set to '' if pat isn't installed; that suppresses repeated
    # log noise on hosts that intentionally don't have Pat.
    _pat_bin_cached = None  # None = not looked up yet, '' = looked up & missing

    def _pat_bin(self):
        if self._pat_bin_cached is None:
            import shutil
            self.__class__._pat_bin_cached = shutil.which('pat') or ''
        return self._pat_bin_cached or None

    def _supervisor(self):
        """Return the gateway's ProcessSupervisor, or None if unavailable.
        Wraps three identical guard branches into one accessor."""
        return getattr(self._gateway, 'process_supervisor', None) if self._gateway else None

    def _start_pat(self):
        """Start Pat Winlink client via the gateway's ProcessSupervisor."""
        pat_bin = self._pat_bin()
        if not pat_bin:
            print("  [Packet] Pat not found — install from https://getpat.io/")
            return False
        sup = self._supervisor()
        if not sup:
            print("  [Packet] No process supervisor — cannot start pat")
            return False
        try:
            sup.add('pat', [pat_bin, 'http'], restart=True, backoff=(2, 30))
            print(f"  [Packet] Pat supervised (web UI on port {self._pat_port})")
            return True
        except ValueError:
            sup.restart('pat')
            return True
        except Exception as e:
            print(f"  [Packet] Pat start failed: {e}")
            return False

    def _stop_pat(self):
        """Stop the supervised pat process (gateway shutdown also reaps it)."""
        sup = self._supervisor()
        if not sup:
            return
        try:
            sup.stop('pat')
            print("  [Packet] Pat stopped")
        except KeyError:
            pass
        except Exception as e:
            print(f"  [Packet] Pat stop error: {e}")

    def _is_pat_running(self):
        """Ask the supervisor whether pat is alive."""
        sup = self._supervisor()
        if not sup:
            return False
        try:
            return bool(sup.status('pat').get('pid'))
        except KeyError:
            return False
        except Exception:
            return False
    def _delayed_pat_start(self):
        """Wait for Direwolf to be ready then start Pat HTTP server.

        Tests the remote KISS port (not the local AGWPE proxy) to avoid
        opening a spurious AGWPE session that would trigger a Direwolf
        restart.

        State machine integration: declares STEADY on successful start,
        ERROR with last_error if pat won't start or Direwolf never came up.
        """
        import socket as _sock
        if self._is_pat_running():
            self._reach_steady()
            return
        ep_ip = self._get_endpoint_ip()
        direwolf_up = False
        if ep_ip:
            for _attempt in range(15):
                time.sleep(1)
                try:
                    s = _sock.socket()
                    s.settimeout(2)
                    s.connect((ep_ip, self._kiss_port))
                    s.close()
                    direwolf_up = True
                    break
                except Exception:
                    continue
        if ep_ip and not direwolf_up:
            self._fail(f"Direwolf KISS port {ep_ip}:{self._kiss_port} unreachable after 15s")
            return
        if not self._start_pat():
            self._fail("Pat failed to start (see prior log lines)")
            return
        self._reach_steady()
