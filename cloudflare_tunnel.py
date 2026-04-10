"""Cloudflare quick tunnel manager."""

import os
import threading
import time

class CloudflareTunnel:
    """Cloudflare quick tunnel — free public HTTPS access with no port forwarding.

    Launches `cloudflared tunnel --url http://localhost:PORT` as a subprocess.
    Output is redirected to a log file (not a pipe) so cloudflared survives
    gateway restarts without dying from SIGPIPE when the parent is killed.
    start_new_session=True fully detaches cloudflared from the gateway's process
    group, so it is never killed when the gateway is restarted.

    On start(), if an existing cloudflared is already running we adopt it and
    read the cached URL from URL_FILE or the log file.
    """

    URL_FILE = '/tmp/cloudflare_tunnel_url'
    LOG_FILE = '/tmp/cloudflared_output.log'

    HEALTH_CHECK_INTERVAL = 900  # seconds between liveness checks (15 min)

    def __init__(self, config, on_url_changed=None):
        self.config = config
        self._process = None  # only set if WE launched it
        self._url = None
        self._thread = None
        self._adopted = False  # True if we reused an existing process
        self._on_url_changed = on_url_changed  # callback(new_url) when tunnel is replaced
        self._health_thread = None

    def start(self):
        import subprocess
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))

        # Check if cloudflared is already running
        try:
            result = subprocess.run(
                ['pgrep', '-x', 'cloudflared'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Existing cloudflared found — adopt it
                self._adopted = True
                # Try URL file first, then scan log file
                try:
                    with open(self.URL_FILE, 'r') as f:
                        self._url = f.read().strip() or None
                except FileNotFoundError:
                    pass
                if not self._url:
                    self._url = self._scan_log_for_url()
                    if self._url:
                        try:
                            with open(self.URL_FILE, 'w') as f:
                                f.write(self._url)
                        except Exception:
                            pass
                # Cloudflared process is running and we have a cached URL.
                # Don't HEAD-validate through the tunnel — the gateway's web
                # server (the backend) isn't up yet during startup, so cloudflare
                # returns 502 even though the tunnel itself is perfectly healthy.
                # Quick tunnels last ~24h; if it expires, cloudflared exits and
                # the next restart will launch a fresh one.
                if self._url:
                    print(f"  [Tunnel] Reusing existing cloudflared (URL: {self._url})")
                    self._start_health_check()
                    return
                # No cached URL but process is running — tail the log for it
                else:
                    print(f"  [Tunnel] Reusing existing cloudflared (URL not yet cached)")
                    self._thread = threading.Thread(target=self._tail_log, daemon=True,
                                                    name="cf-tunnel")
                    self._thread.start()
                    self._start_health_check()
                    return
        except Exception:
            pass

        # No existing process — launch a new one.
        # Clear stale URL so send_startup_delayed waits for the fresh URL.
        self._url = None
        try:
            os.unlink(self.URL_FILE)
        except Exception:
            pass

        self._do_launch(port)
        self._start_health_check()

    def _start_health_check(self):
        """Start periodic health check thread if not already running."""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True,
                                                name="cf-health")
        self._health_thread.start()

    def _probe_url(self, url):
        """HTTP HEAD probe of tunnel URL. Returns True if reachable (any HTTP response)."""
        import urllib.request
        try:
            req = urllib.request.Request(url, method='HEAD')
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError:
            return True  # Got an HTTP response (e.g. 502) — tunnel itself is alive
        except Exception:
            return False  # Connection refused, DNS failure, timeout — tunnel dead

    def _kill_cloudflared(self):
        """Kill all cloudflared processes."""
        import subprocess, signal
        try:
            result = subprocess.run(['pgrep', '-x', 'cloudflared'],
                                    capture_output=True, text=True, timeout=5)
            for pid in result.stdout.strip().split('\n'):
                pid = pid.strip()
                if pid:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            time.sleep(2)
        except Exception:
            pass

    def _relaunch_tunnel(self):
        """Relaunch cloudflared and wait for new URL. Returns (old_url, new_url)."""
        old_url = self._url
        self._url = None
        self._adopted = False
        self._process = None
        try:
            os.unlink(self.URL_FILE)
        except Exception:
            pass

        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        self._do_launch(port)

        # Wait up to 60s for new URL
        for _ in range(60):
            if self._url:
                break
            time.sleep(1)

        return old_url, self._url

    def _health_check_loop(self):
        """Periodically verify tunnel is alive; relaunch if dead or expired."""
        import subprocess
        consecutive_probe_failures = 0
        PROBE_FAIL_THRESHOLD = 2  # require 2 consecutive failures before relaunch

        while True:
            time.sleep(self.HEALTH_CHECK_INTERVAL)
            try:
                # Check if cloudflared process is running
                result = subprocess.run(
                    ['pgrep', '-x', 'cloudflared'],
                    capture_output=True, text=True, timeout=5
                )
                process_alive = result.returncode == 0 and result.stdout.strip()

                if process_alive and self._url:
                    # Process is running — probe the URL to detect expired tunnels
                    if self._probe_url(self._url):
                        consecutive_probe_failures = 0
                        continue  # Healthy
                    consecutive_probe_failures += 1
                    if consecutive_probe_failures < PROBE_FAIL_THRESHOLD:
                        print(f"  [Tunnel] URL probe failed ({consecutive_probe_failures}/{PROBE_FAIL_THRESHOLD}), will retry...")
                        continue
                    # Tunnel expired while process still running — kill and relaunch
                    print(f"  [Tunnel] URL expired (probe failed {consecutive_probe_failures}x) — killing and relaunching...")
                    self._kill_cloudflared()
                elif process_alive:
                    continue  # Running but no URL cached yet — let _tail_log handle it
                else:
                    print(f"  [Tunnel] cloudflared not running — relaunching...")

                consecutive_probe_failures = 0
                old_url, new_url = self._relaunch_tunnel()

                if new_url and new_url != old_url:
                    print(f"  [Tunnel] New URL after relaunch: {new_url}")
                    if self._on_url_changed:
                        try:
                            self._on_url_changed(new_url)
                        except Exception as e:
                            print(f"  [Tunnel] on_url_changed callback error: {e}")
                elif not new_url:
                    print(f"  [Tunnel] Relaunch failed — no URL after 60s")
            except Exception as e:
                print(f"  [Tunnel] Health check error: {e}")

    def _do_launch(self, port):
        """Launch cloudflared in its own systemd scope so it survives gateway restarts.

        The gateway service uses KillMode=control-group, which kills all child
        processes on restart.  systemd-run --user --scope puts cloudflared in a
        separate cgroup so it is not affected by gateway stop/restart.
        Falls back to plain Popen if systemd-run is not available.
        """
        import subprocess
        try:
            with open(self.LOG_FILE, 'w') as log_f:
                try:
                    # Launch in a separate systemd scope — survives gateway cgroup kill
                    self._process = subprocess.Popen(
                        ['systemd-run', '--user', '--scope', '--unit=cloudflared-tunnel',
                         'cloudflared', 'tunnel', '--url', f'http://localhost:{port}'],
                        stdout=log_f, stderr=log_f,
                        start_new_session=True
                    )
                except FileNotFoundError:
                    # systemd-run not available — fall back to plain launch
                    self._process = subprocess.Popen(
                        ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}'],
                        stdout=log_f, stderr=log_f,
                        start_new_session=True
                    )
        except FileNotFoundError:
            print("  [Tunnel] cloudflared not found — install with: sudo pacman -S cloudflared")
            return
        except Exception as e:
            print(f"  [Tunnel] Failed to start: {e}")
            return

        print(f"  [Tunnel] Starting Cloudflare tunnel for port {port}...")

        # Background thread: detect immediate failures and retry, then tail log for URL
        self._thread = threading.Thread(target=self._run_thread, args=(port,),
                                        daemon=True, name="cf-tunnel")
        self._thread.start()

    def stop(self):
        # Don't kill cloudflared — leave it running so the URL survives gateway restarts
        pass

    def get_url(self):
        if self._url:
            return self._url
        # Fallback: re-check URL file (updated by _tail_log thread)
        try:
            with open(self.URL_FILE, 'r') as f:
                url = f.read().strip()
            if url:
                self._url = url
        except FileNotFoundError:
            pass
        return self._url

    def _scan_log_for_url(self):
        """Scan the full log file for a tunnel URL (used during adoption)."""
        import re
        try:
            with open(self.LOG_FILE, 'r') as f:
                content = f.read()
            m = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', content)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def _run_thread(self, port):
        """Retry cloudflared if it exits immediately (code 1 port conflict), then tail log."""
        import subprocess
        for attempt in range(1, 4):
            time.sleep(1)
            if self._process.poll() is None:
                break  # Running fine — proceed to log tailing

            exit_code = self._process.returncode
            if exit_code != 0 and attempt < 3:
                print(f"  [Tunnel] cloudflared exited immediately (code {exit_code}), "
                      f"retrying in 5s... (attempt {attempt})")
                time.sleep(5)

                # Check if another cloudflared appeared (previous one finally released port)
                try:
                    result = subprocess.run(['pgrep', '-x', 'cloudflared'],
                                            capture_output=True, text=True, timeout=5)
                    if result.returncode == 0 and result.stdout.strip():
                        self._adopted = True
                        try:
                            with open(self.URL_FILE, 'r') as f:
                                self._url = f.read().strip() or None
                        except FileNotFoundError:
                            pass
                        if not self._url:
                            self._url = self._scan_log_for_url()
                        if self._url:
                            print(f"  [Tunnel] Found existing cloudflared (URL: {self._url})")
                            return
                        print(f"  [Tunnel] Found existing cloudflared (URL not yet cached)")
                        break  # Fall through to tail log
                except Exception:
                    pass

                # Relaunch
                try:
                    with open(self.LOG_FILE, 'w') as log_f:
                        self._process = subprocess.Popen(
                            ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}'],
                            stdout=log_f, stderr=log_f,
                            start_new_session=True
                        )
                    print(f"  [Tunnel] Retry {attempt}: Starting cloudflared...")
                except Exception as e:
                    print(f"  [Tunnel] Retry {attempt} failed: {e}")
                    return
            else:
                if exit_code != 0:
                    print(f"\n[Tunnel] cloudflared failed after {attempt} attempt(s) "
                          f"(code {exit_code})")
                return

        self._tail_log()

    def _tail_log(self):
        """Read cloudflared log file and capture tunnel URL as it appears."""
        import re
        try:
            with open(self.LOG_FILE, 'r') as f:
                while True:
                    line = f.readline()
                    if not line:
                        # Also poll URL_FILE as fallback (handles old-style adopted processes)
                        try:
                            with open(self.URL_FILE, 'r') as uf:
                                url = uf.read().strip()
                            if url and url != self._url:
                                self._url = url
                                print(f"  [Tunnel] Public URL: {self._url}")
                                return
                        except FileNotFoundError:
                            pass
                        if self._process and self._process.poll() is not None:
                            break
                        time.sleep(0.2)
                        continue
                    m = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
                    if m:
                        self._url = m.group(1)
                        try:
                            with open(self.URL_FILE, 'w') as f:
                                f.write(self._url)
                        except Exception:
                            pass
                        print(f"  [Tunnel] Public URL: {self._url}")
        except Exception:
            pass
        if self._process and self._process.poll() is not None and self._process.returncode != 0:
            print(f"\n[Tunnel] cloudflared exited (code {self._process.returncode})")


