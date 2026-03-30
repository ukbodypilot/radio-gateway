"""Gateway utility classes — DDNS, email, Cloudflare tunnel, Mumble server, USB/IP.

Extracted from gateway_core.py for readability. These are self-contained
support services used by RadioGateway but not part of the core audio path.
"""

import json
import os
import socket
import subprocess
import threading
import time

import json as json_mod


class DDNSUpdater:
    """Dynamic DNS updater (No-IP compatible protocol).

    Runs a background thread that periodically updates a DDNS hostname
    with the machine's current public IP via the No-IP update API.
    """

    def __init__(self, config):
        self.config = config
        self._stop = False
        self._thread = None
        self._last_ip = None
        self._last_status = None   # 'good', 'nochg', or error string
        self._last_update = 0      # time.time() of last update attempt

    def start(self):
        username = str(getattr(self.config, 'DDNS_USERNAME', '') or '')
        password = str(getattr(self.config, 'DDNS_PASSWORD', '') or '')
        hostname = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '')
        if not username or not password or not hostname:
            print("  [DDNS] Missing username, password, or hostname — skipping")
            return
        self._stop = False
        self._thread = threading.Thread(target=self._update_loop, daemon=True,
                                        name="ddns-updater")
        self._thread.start()
        print(f"  [DDNS] Updater started for {hostname} "
              f"(every {self.config.DDNS_UPDATE_INTERVAL}s)")

    def stop(self):
        self._stop = True

    def get_status(self):
        """Return compact status string for the status bar."""
        if self._last_ip and self._last_status in ('good', 'nochg'):
            return self._last_ip
        elif self._last_status:
            return 'ERR'
        return '...'

    def _update_loop(self):
        import urllib.request
        import base64

        username = str(self.config.DDNS_USERNAME)
        password = str(self.config.DDNS_PASSWORD)
        hostname = str(self.config.DDNS_HOSTNAME)
        url_base = str(getattr(self.config, 'DDNS_UPDATE_URL',
                                'https://dynupdate.no-ip.com/nic/update') or
                       'https://dynupdate.no-ip.com/nic/update')
        interval = max(60, int(getattr(self.config, 'DDNS_UPDATE_INTERVAL', 300)))
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()

        while not self._stop:
            try:
                url = f"{url_base}?hostname={hostname}"
                req = urllib.request.Request(url)
                req.add_header('Authorization', f'Basic {creds}')
                req.add_header('User-Agent', 'RadioGateway/1.0 radio_gateway.py')
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = resp.read().decode().strip()
            except Exception as e:
                result = f"error: {e}"

            # Parse response: "good IP", "nochg IP", or error codes
            parts = result.split()
            code = parts[0] if parts else result
            ip = parts[1] if len(parts) > 1 else ''

            self._last_update = time.time()
            self._last_status = code
            if code in ('good', 'nochg'):
                if code == 'good' or self._last_ip is None:
                    print(f"\n[DDNS] {hostname} → {ip}")
                self._last_ip = ip
            else:
                print(f"\n[DDNS] Update failed: {result}")

            # Sleep in small increments so stop is responsive
            for _ in range(int(interval)):
                if self._stop:
                    return
                time.sleep(1)


class EmailNotifier:
    """Gmail SMTP email sender for gateway notifications."""

    def __init__(self, config, gateway=None):
        self.config = config
        self.gateway = gateway
        self._address = str(getattr(config, 'EMAIL_ADDRESS', '') or '').strip()
        self._password = str(getattr(config, 'EMAIL_APP_PASSWORD', '') or '').strip()
        self._recipient = str(getattr(config, 'EMAIL_RECIPIENT', '') or '').strip() or self._address

    def is_configured(self):
        return bool(self._address and self._password)

    def send(self, subject, body):
        """Send an email. Returns True on success."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        if not self.is_configured():
            print("  [Email] Not configured (missing EMAIL_ADDRESS or EMAIL_APP_PASSWORD)")
            return False

        msg = MIMEMultipart('alternative')
        msg['From'] = self._address
        msg['To'] = self._recipient
        msg['Subject'] = subject

        # Plain text version
        msg.attach(MIMEText(body, 'plain'))

        # HTML version (makes URLs clickable)
        # Linkify URLs BEFORE inserting <br> tags, otherwise <br> gets captured in the URL
        import re
        html_body = re.sub(r'((?:https?|wss?)://\S+)', r'<a href="\1">\1</a>', body)
        html_body = html_body.replace('\n', '<br>\n')
        msg.attach(MIMEText(f'<html><body style="font-family:monospace;font-size:14px">{html_body}</body></html>', 'html'))

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
                server.login(self._address, self._password)
                server.sendmail(self._address, self._recipient, msg.as_string())
            print(f"  [Email] Sent to {self._recipient}: {subject}")
            return True
        except Exception as e:
            print(f"  [Email] Failed: {e}")
            return False

    def send_startup_status(self):
        """Send a status email with gateway info and tunnel URL."""
        import datetime
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        lines = [f"Radio Gateway started at {now}", ""]

        # Tunnel URL
        if self.gateway and self.gateway.cloudflare_tunnel:
            url = self.gateway.cloudflare_tunnel.get_url()
            if url:
                lines.append(f"Gateway:   {url}")
                lines.append(f"Config:    {url}/config")
                lines.append(f"Monitor:   {url}/monitor")
                lines.append(f"Monitor App: {url}/ws_monitor")
                # Voice-to-tmux (remote via tunnel)
                tunnel_base = url.rstrip('/')
                lines.append(f"Voice Tmux: {tunnel_base}/voice")
                lines.append("")

        # LAN link
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            lan_ip = s.getsockname()[0]
            s.close()
            lines.append(f"LAN:       http://{lan_ip}:{port}")
            lines.append(f"LAN App:   http://{lan_ip}:{port}/ws_monitor")
            lines.append(f"LAN Voice: http://{lan_ip}:{port}/voice")
        except Exception:
            pass
        lines.append(f"Local:     http://localhost:{port}")
        lines.append("")

        # Mumble server
        mumble_srv = str(getattr(self.config, 'MUMBLE_SERVER', '') or '')
        mumble_port = int(getattr(self.config, 'MUMBLE_PORT', 64738))
        if mumble_srv:
            lines.append(f"Mumble:    {mumble_srv}:{mumble_port}")

        # DDNS
        ddns_host = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '')
        if ddns_host:
            lines.append(f"DDNS:      {ddns_host}")

        lines.append("")
        lines.append("-- Radio Gateway")

        # Detailed status dump
        lines.append("")
        lines += self._build_status_dump()

        # Log dump
        lines.append("")
        lines.append("--- Recent Log ---")
        try:
            import sys as _sys
            writer = _sys.stdout
            if hasattr(writer, 'get_log_lines'):
                import re as _re
                _ansi_re = _re.compile(r'\x1b\[[0-9;]*m')
                log_lines = writer.get_log_lines(after_seq=0, limit=200)
                for _seq, text in log_lines:
                    lines.append(_ansi_re.sub('', text))
            else:
                lines.append("(log not available)")
        except Exception as e:
            lines.append(f"(log error: {e})")

        hostname = ''
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            pass

        subject = f"Gateway Online{' — ' + hostname if hostname else ''}"
        self.send(subject, '\n'.join(lines))

    def _build_status_dump(self):
        """Build a list of lines with a detailed gateway and system status dump."""
        lines = []
        lines.append("--- Gateway Status ---")

        if self.gateway:
            try:
                s = self.gateway.get_status_dict()

                gw_name = getattr(self.gateway.config, 'GATEWAY_NAME', '') or ''
                if gw_name:
                    lines.append(f"Name:          {gw_name}")
                lines.append(f"Version:       {__version__}")
                lines.append(f"Uptime:        {s.get('uptime', '?')}")

                # Mumble servers
                ms1 = s.get('ms1_state', None)
                ms2 = s.get('ms2_state', None)
                if ms1 is not None:
                    lines.append(f"Mumble 1:      {ms1}")
                if ms2 is not None:
                    lines.append(f"Mumble 2:      {ms2}")
                mumble_client = 'connected' if s.get('mumble') else 'disconnected'
                lines.append(f"Mumble client: {mumble_client}")

                # CAT serial
                cat = s.get('cat', '')
                if cat:
                    rel = s.get('cat_reliability', {})
                    sent = rel.get('sent', 0)
                    missed = rel.get('missed', 0)
                    cat_line = f"CAT serial:    {cat}"
                    if sent:
                        cat_line += f"  ({sent} cmd, {missed} missed)"
                    lines.append(cat_line)

                # PTT
                ptt_m = s.get('ptt_method', '?')
                ptt_a = ' [ACTIVE]' if s.get('ptt_active') else ''
                lines.append(f"PTT:           {ptt_m}{ptt_a}")

                # VAD
                vad_state = 'ON' if s.get('vad_enabled') else 'off'
                lines.append(f"VAD:           {vad_state}")

                # Mute/audio states
                mutes = []
                if s.get('tx_muted'):
                    mutes.append('TX')
                if s.get('rx_muted'):
                    mutes.append('RX')
                if s.get('d75_muted'):
                    mutes.append('D75')
                if s.get('kv4p_muted'):
                    mutes.append('KV4P')
                if s.get('sdr1_muted'):
                    mutes.append('SDR1')
                if s.get('sdr2_muted'):
                    mutes.append('SDR2')
                if s.get('remote_muted'):
                    mutes.append('Remote')
                if s.get('announce_muted'):
                    mutes.append('Announce')
                if s.get('speaker_muted'):
                    mutes.append('Speaker')
                lines.append(f"Mutes:         {', '.join(mutes) if mutes else 'none'}")

                # KV4P
                if s.get('kv4p_enabled'):
                    kv4p_conn = 'connected' if s.get('kv4p_connected') else 'enabled'
                    kv4p_freq = getattr(self.gateway.config, 'KV4P_FREQ', '') if self.gateway else ''
                    kv4p_ctcss = getattr(self.gateway.config, 'KV4P_CTCSS_TX', 0) if self.gateway else 0
                    kv4p_line = f"KV4P:          {kv4p_conn}"
                    if kv4p_freq:
                        kv4p_line += f"  {kv4p_freq} MHz"
                    if kv4p_ctcss and str(kv4p_ctcss) != '0':
                        kv4p_line += f"  CTCSS:{kv4p_ctcss}"
                    lines.append(kv4p_line)

                # D75
                if s.get('d75_enabled'):
                    d75_conn = 'connected' if s.get('d75_connected') else 'enabled'
                    d75_mode = s.get('d75_mode', '')
                    d75_line = f"D75:           {d75_conn}"
                    if d75_mode:
                        d75_line += f"  ({d75_mode})"
                    lines.append(d75_line)

                # SDR
                if s.get('sdr1_enabled'):
                    sdr_name = getattr(self.gateway.config, 'SDR_DEVICE_NAME', '') if self.gateway else ''
                    sdr_line = f"SDR1:          enabled"
                    if sdr_name:
                        sdr_line += f"  ({sdr_name})"
                    lines.append(sdr_line)

                # Streaming
                if s.get('streaming_enabled'):
                    stream_ok = s.get('stream_pipe_ok', False)
                    lines.append(f"Broadcastify:  {'live' if stream_ok else 'pipe disconnected'}")

                # DDNS
                ddns = s.get('ddns', '')
                if ddns:
                    lines.append(f"DDNS:          {ddns}")

                # Charger
                charger = s.get('charger', '')
                if charger:
                    lines.append(f"Charger:       {charger}")

            except Exception as e:
                lines.append(f"(status error: {e})")
        else:
            lines.append("(gateway not available)")

        # System stats
        lines.append("")
        lines.append("--- System ---")

        try:
            with open('/proc/loadavg', 'r') as f:
                parts = f.read().split()
            lines.append(f"Load (1/5/15): {parts[0]} / {parts[1]} / {parts[2]}")
        except Exception:
            pass

        try:
            mem = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    p = line.split()
                    k = p[0].rstrip(':')
                    if k in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                        mem[k] = int(p[1])
            total = mem.get('MemTotal', 0)
            avail = mem.get('MemAvailable', 0)
            used_mb = (total - avail) // 1024
            total_mb = total // 1024
            pct = round(100.0 * (total - avail) / total) if total else 0
            lines.append(f"RAM:           {used_mb} MB / {total_mb} MB ({pct}%)")
            swap_total = mem.get('SwapTotal', 0)
            swap_free = mem.get('SwapFree', 0)
            if swap_total:
                swap_used = (swap_total - swap_free) // 1024
                lines.append(f"Swap:          {swap_used} MB / {swap_total // 1024} MB")
        except Exception:
            pass

        try:
            import shutil as _shutil
            total, used, free = _shutil.disk_usage('/')
            gb = 1024 ** 3
            pct = round(100.0 * used / total) if total else 0
            lines.append(f"Disk (/):      {used // gb} GB / {total // gb} GB ({pct}%)")
        except Exception:
            pass

        try:
            import glob as _glob
            zones = sorted(_glob.glob('/sys/class/thermal/thermal_zone*/temp'))
            temps = []
            for zp in zones:
                try:
                    with open(zp, 'r') as f:
                        t = int(f.read().strip()) / 1000
                    if t > 0:
                        temps.append(f"{t:.0f}°C")
                except Exception:
                    pass
            if temps:
                lines.append(f"Temps:         {', '.join(temps)}")
        except Exception:
            pass

        try:
            import socket as _socket
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            lan_ip = s.getsockname()[0]
            s.close()
            lines.append(f"LAN IP:        {lan_ip}")
        except Exception:
            pass

        return lines

    def send_startup_delayed(self):
        """Wait for tunnel URL (up to 60s) then send startup email."""
        def _delayed():
            # Wait for tunnel URL if tunnel is enabled
            if self.gateway and self.gateway.cloudflare_tunnel:
                for _ in range(60):
                    if self.gateway.cloudflare_tunnel.get_url():
                        break
                    time.sleep(1)
            self.send_startup_status()

        t = threading.Thread(target=_delayed, daemon=True, name="email-startup")
        t.start()


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

    def __init__(self, config):
        self.config = config
        self._process = None  # only set if WE launched it
        self._url = None
        self._thread = None
        self._adopted = False  # True if we reused an existing process

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
                # Validate the URL is still live — stale quick tunnels return 502/530
                if self._url:
                    try:
                        import urllib.request
                        req = urllib.request.Request(self._url, method='HEAD')
                        resp = urllib.request.urlopen(req, timeout=5)
                        if resp.status < 500:
                            print(f"  [Tunnel] Reusing existing cloudflared (URL: {self._url})")
                            return
                    except Exception:
                        pass
                    # URL is stale — kill old cloudflared and start fresh
                    print(f"  [Tunnel] Stale tunnel URL detected — restarting cloudflared")
                    self._url = None
                    try:
                        subprocess.run(['pkill', '-x', 'cloudflared'], capture_output=True, timeout=5)
                        time.sleep(2)
                    except Exception:
                        pass
                    self._adopted = False
                    # Fall through to launch new one
                else:
                    print(f"  [Tunnel] Reusing existing cloudflared (URL not yet cached)")
                    self._thread = threading.Thread(target=self._tail_log, daemon=True,
                                                    name="cf-tunnel")
                    self._thread.start()
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

    def _do_launch(self, port):
        """Launch cloudflared, redirecting output to LOG_FILE so it survives restarts."""
        import subprocess
        try:
            with open(self.LOG_FILE, 'w') as log_f:
                self._process = subprocess.Popen(
                    ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}'],
                    stdout=log_f, stderr=log_f,
                    start_new_session=True  # detach from gateway session/group
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


class MumbleServerManager:
    """Manages local mumble-server (murmurd) instances.

    Each instance gets its own config file and systemd service override.
    Config files are written to /etc/mumble-server-gw{n}.ini and managed
    via systemd (mumble-server-gw{n}.service).
    """

    # State constants
    STATE_DISABLED = 'disabled'
    STATE_CONFIGURED = 'configured'
    STATE_RUNNING = 'running'
    STATE_ERROR = 'error'

    def __init__(self, instance_num, config):
        self.num = instance_num
        self.prefix = f'MUMBLE_SERVER_{instance_num}'
        self.config = config
        self.state = self.STATE_DISABLED
        self.error_msg = ''
        self._service_name = f'mumble-server-gw{instance_num}'
        self._config_path = f'/etc/mumble-server-gw{instance_num}.ini'
        self._db_path = f'/var/lib/mumble-server/mumble-server-gw{instance_num}.sqlite'
        self._log_path = f'/var/log/mumble-server/mumble-server-gw{instance_num}.log'
        self._pid_path = f'/var/run/mumble-server/mumble-server-gw{instance_num}.pid'

    def _get_cfg(self, key):
        """Get a config value for this instance."""
        return getattr(self.config, f'{self.prefix}_{key}', None)

    def is_enabled(self):
        return getattr(self.config, f'ENABLE_{self.prefix}', False)

    def write_config(self):
        """Write the mumble-server .ini file for this instance."""
        port = int(self._get_cfg('PORT') or 64738)
        password = str(self._get_cfg('PASSWORD') or '')
        max_users = int(self._get_cfg('MAX_USERS') or 10)
        max_bw = int(self._get_cfg('MAX_BANDWIDTH') or 72000)
        welcome = str(self._get_cfg('WELCOME') or '')
        reg_name = str(self._get_cfg('REGISTER_NAME') or '')
        allow_html = self._get_cfg('ALLOW_HTML')
        opus_thresh = int(self._get_cfg('OPUS_THRESHOLD') or 0)

        lines = [
            '# Auto-generated by Radio Gateway',
            f'# Instance: Mumble Server {self.num}',
            f'# Do not edit — regenerated on each gateway start',
            '',
            f'port={port}',
            f'serverpassword={password}',
            f'bandwidth={max_bw}',
            f'users={max_users}',
            f'opusthreshold={opus_thresh}',
            f'allowhtml={"true" if allow_html else "false"}',
            f'welcometext={welcome}',
            f'registerName={reg_name}',
            f'bonjour=false',
            '',
            '# Disable autoban (gateway pymumble reconnects trigger it)',
            'autobanAttempts=0',
            '',
            '# Long client timeout (pymumble protocol 1.2.4 ping may not satisfy newer murmur)',
            'timeout=300',
            '',
            f'database={self._db_path}',
            f'logfile={self._log_path}',
            f'pidfile={self._pid_path}',
            '',
            '# Auto-generated SSL (mumble-server creates self-signed on first run)',
            '',
        ]

        try:
            import subprocess
            content = '\n'.join(lines) + '\n'
            result = subprocess.run(
                ['sudo', 'tee', self._config_path],
                input=content, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = f'Failed to write config: {result.stderr.strip()}'
                return False
            return True
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Config write error: {e}'
            return False

    def _setup_systemd_service(self):
        """Create a systemd service override for this instance."""
        import subprocess

        service_file = f'/etc/systemd/system/{self._service_name}.service'
        murmurd_bin = None
        for candidate in ['/usr/sbin/murmurd', '/usr/bin/murmurd',
                          '/usr/sbin/mumble-server', '/usr/bin/mumble-server']:
            try:
                result = subprocess.run(['test', '-x', candidate],
                                        capture_output=True, timeout=2)
                if result.returncode == 0:
                    murmurd_bin = candidate
                    break
            except Exception:
                pass

        if not murmurd_bin:
            # Try 'which' as fallback
            try:
                result = subprocess.run(['which', 'murmurd'], capture_output=True,
                                        text=True, timeout=2)
                if result.returncode == 0:
                    murmurd_bin = result.stdout.strip()
            except Exception:
                pass
            if not murmurd_bin:
                try:
                    result = subprocess.run(['which', 'mumble-server'],
                                            capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        murmurd_bin = result.stdout.strip()
                except Exception:
                    pass

        if not murmurd_bin:
            self.state = self.STATE_ERROR
            self.error_msg = 'murmurd/mumble-server binary not found'
            return False

        # Detect the service user: Arch uses '_mumble-server', Debian uses 'mumble-server'
        import pwd
        svc_user = None
        for candidate_user in ['_mumble-server', 'mumble-server']:
            try:
                pwd.getpwnam(candidate_user)
                svc_user = candidate_user
                break
            except KeyError:
                pass
        if not svc_user:
            self.state = self.STATE_ERROR
            self.error_msg = 'mumble-server system user not found (need _mumble-server or mumble-server)'
            return False

        unit = '\n'.join([
            '[Unit]',
            f'Description=Mumble Server (Gateway Instance {self.num})',
            'After=network.target',
            '',
            '[Service]',
            'Type=simple',
            f'ExecStart={murmurd_bin} -fg -ini {self._config_path}',
            f'User={svc_user}',
            f'Group={svc_user}',
            'Restart=on-failure',
            'RestartSec=5',
            '',
            '[Install]',
            'WantedBy=multi-user.target',
            '',
        ])

        try:
            result = subprocess.run(
                ['sudo', 'tee', service_file],
                input=unit, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = f'Failed to write service: {result.stderr.strip()}'
                return False
            subprocess.run(['sudo', 'systemctl', 'daemon-reload'],
                           capture_output=True, timeout=5)
            return True
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Service setup error: {e}'
            return False

    def start(self):
        """Write config, set up service, and start the mumble-server instance."""
        import subprocess

        if not self.is_enabled():
            self.state = self.STATE_DISABLED
            return

        self.state = self.STATE_CONFIGURED
        self.error_msg = ''

        # Check if mumble-server package is installed
        try:
            result = subprocess.run(['which', 'murmurd'], capture_output=True,
                                    text=True, timeout=2)
            if result.returncode != 0:
                result = subprocess.run(['which', 'mumble-server'],
                                        capture_output=True, text=True, timeout=2)
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = 'mumble-server not installed (run scripts/install.sh)'
                return
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Cannot check for mumble-server: {e}'
            return

        # Stop any existing instance first so config changes (especially port)
        # take effect.  systemctl start is a no-op if the service is already
        # running, so we must explicitly stop+start (restart) every time.
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'stop', f'{self._service_name}.service'],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

        # Ensure directories exist
        for d in ['/var/lib/mumble-server', '/var/log/mumble-server',
                  '/var/run/mumble-server']:
            try:
                subprocess.run(['sudo', 'mkdir', '-p', d],
                               capture_output=True, timeout=3)
            except Exception:
                pass

        # Write config file
        if not self.write_config():
            return

        # Set up systemd service
        if not self._setup_systemd_service():
            return

        autostart = self._get_cfg('AUTOSTART')
        if autostart is False:
            # Configured but not auto-started
            print(f"  Mumble Server {self.num}: configured (autostart=false)")
            return

        # Start the service
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'start', f'{self._service_name}.service'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.state = self.STATE_ERROR
                self.error_msg = result.stderr.strip() or 'systemctl start failed'
                return
            # Brief pause then verify
            time.sleep(0.5)
            self._check_running()
        except Exception as e:
            self.state = self.STATE_ERROR
            self.error_msg = f'Start error: {e}'

    def stop(self):
        """Stop the mumble-server instance."""
        import subprocess
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'stop', f'{self._service_name}.service'],
                capture_output=True, timeout=10
            )
        except Exception:
            pass
        self.state = self.STATE_CONFIGURED if self.is_enabled() else self.STATE_DISABLED

    def _check_running(self):
        """Check if the service is actively running."""
        import subprocess
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', f'{self._service_name}.service'],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() == 'active':
                self.state = self.STATE_RUNNING
            elif self.state != self.STATE_ERROR:
                self.state = self.STATE_ERROR
                # Try to get reason from journal
                try:
                    jr = subprocess.run(
                        ['journalctl', '-u', f'{self._service_name}.service',
                         '-n', '3', '--no-pager', '-q'],
                        capture_output=True, text=True, timeout=3
                    )
                    last_line = jr.stdout.strip().split('\n')[-1] if jr.stdout.strip() else ''
                    self.error_msg = last_line[:80] if last_line else 'service not active'
                except Exception:
                    self.error_msg = 'service not active'
        except Exception as e:
            if self.state != self.STATE_ERROR:
                self.state = self.STATE_ERROR
                self.error_msg = f'status check failed: {e}'

    def check_health(self):
        """Periodic health check — call from status_monitor_loop."""
        if not self.is_enabled():
            self.state = self.STATE_DISABLED
            return
        if self.state == self.STATE_DISABLED:
            return
        self._check_running()

    def get_status(self):
        """Return (state, port) tuple for status bar."""
        port = int(self._get_cfg('PORT') or 64738)
        return self.state, port


# ============================================================================
# USB/IP MANAGER
# ============================================================================

class USBIPManager:
    """Attach remote USB devices from a USB/IP server (usbipd) over TCP.

    Server side: run scripts/setup_usbip_server.sh on the remote machine,
    configure /usr/local/bin/usbip-bind-devices with the device IDs to share.

    Client side (this class): loads vhci-hcd, attaches configured bus IDs,
    monitors attachment health, re-attaches on disconnect.

    Config keys:
        ENABLE_USBIP   (bool)   — enable this manager
        USBIP_SERVER   (str)    — IP/hostname of the usbipd server
        USBIP_DEVICES  (str)    — comma-separated bus IDs to attach, e.g. "1-1.4,1-1.3"
                                  leave empty to attach all exported devices automatically
    """

    POLL_INTERVAL   = 15    # seconds between health checks
    ATTACH_TIMEOUT  = 10    # seconds for usbip commands

    def __init__(self, config):
        self.config  = config
        self._thread = None
        self._stop   = threading.Event()
        self._lock   = threading.Lock()

        # Status reported to web UI
        self.server_reachable = False
        self.exported_devices = []   # [{bus_id, description, attached}]
        self.last_error       = ''
        self.last_check_time  = 0.0

    # ------------------------------------------------------------------ start/stop

    def start(self):
        if not getattr(self.config, 'ENABLE_USBIP', False):
            return
        server = str(getattr(self.config, 'USBIP_SERVER', '')).strip()
        if not server:
            print('[USBIP] ENABLE_USBIP=true but USBIP_SERVER is empty — not starting')
            return
        self._ensure_vhci()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='USBIPManager', daemon=True)
        self._thread.start()
        print(f'[USBIP] Manager started → server {server}')

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ internals

    def _ensure_vhci(self):
        """Load vhci-hcd kernel module if not already present."""
        import subprocess
        result = subprocess.run(['lsmod'], capture_output=True, text=True)
        if 'vhci_hcd' not in result.stdout:
            r = subprocess.run(['sudo', 'modprobe', 'vhci-hcd'],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print('[USBIP] Loaded vhci-hcd kernel module')
            else:
                print(f'[USBIP] WARNING: could not load vhci-hcd: {r.stderr.strip()}')

    def _run_cmd(self, args, timeout=None):
        """Run a usbip command, return (stdout, stderr, returncode)."""
        import subprocess
        timeout = timeout or self.ATTACH_TIMEOUT
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            return '', 'timeout', -1
        except Exception as e:
            return '', str(e), -1

    def _list_remote(self, server):
        """Return list of {bus_id, description} dicts exported by server."""
        stdout, stderr, rc = self._run_cmd(['usbip', 'list', '-r', server])
        devices = []
        if rc != 0:
            return None, stderr.strip() or 'usbip list failed'
        for line in stdout.splitlines():
            # Format: "        1-1.4: Vendor : Product (vid:pid)"
            m = __import__('re').match(r'^\s+([\d\-\.]+):\s+(.+)$', line)
            if m:
                devices.append({'bus_id': m.group(1).strip(),
                                 'description': m.group(2).strip(),
                                 'attached': False})
        return devices, ''

    def _list_attached(self):
        """Return set of bus IDs currently attached on this client."""
        stdout, _, rc = self._run_cmd(['usbip', 'port'])
        if rc != 0:
            return set()
        attached = set()
        for line in stdout.splitlines():
            # "    2-1: Realtek ...  at Remote(192.168.x.x) Bus(1-1.4)"
            m = __import__('re').search(r'Bus\(([\d\-\.]+)\)', line)
            if m:
                attached.add(m.group(1))
        return attached

    def _attach(self, server, bus_id):
        """Attach a single device. Returns True on success."""
        stdout, stderr, rc = self._run_cmd(
            ['sudo', 'usbip', 'attach', '-r', server, '-b', bus_id])
        if rc == 0:
            print(f'[USBIP] Attached {bus_id} from {server}')
            return True
        err = (stdout + stderr).strip()
        print(f'[USBIP] Failed to attach {bus_id}: {err}')
        return False

    def _run(self):
        import time
        server = str(getattr(self.config, 'USBIP_SERVER', '')).strip()
        wanted_raw = str(getattr(self.config, 'USBIP_DEVICES', '')).strip()
        wanted = {b.strip() for b in wanted_raw.split(',') if b.strip()} if wanted_raw else set()

        while not self._stop.is_set():
            try:
                exported, err = self._list_remote(server)
                with self._lock:
                    self.last_check_time = time.time()
                    if exported is None:
                        self.server_reachable = False
                        self.last_error = err
                        self._stop.wait(self.POLL_INTERVAL)
                        continue

                    self.server_reachable = True
                    self.last_error = ''

                    # Filter to wanted bus IDs (or all if USBIP_DEVICES empty)
                    targets = [d for d in exported
                               if not wanted or d['bus_id'] in wanted]

                    attached = self._list_attached()
                    for dev in targets:
                        dev['attached'] = dev['bus_id'] in attached

                    self.exported_devices = targets

                # Attach anything not yet attached
                for dev in targets:
                    if not dev['attached']:
                        ok = self._attach(server, dev['bus_id'])
                        with self._lock:
                            dev['attached'] = ok

            except Exception as e:
                with self._lock:
                    self.last_error = str(e)
                print(f'[USBIP] Manager error: {e}')

            self._stop.wait(self.POLL_INTERVAL)

    # ------------------------------------------------------------------ status for web UI

    def get_status(self):
        """Return dict for /usbipstatus JSON endpoint."""
        import time
        with self._lock:
            return {
                'enabled':          getattr(self.config, 'ENABLE_USBIP', False),
                'server':           str(getattr(self.config, 'USBIP_SERVER', '')),
                'server_reachable': self.server_reachable,
                'devices':          list(self.exported_devices),
                'last_error':       self.last_error,
                'last_check':       round(time.time() - self.last_check_time, 0)
                                    if self.last_check_time else None,
            }


# ============================================================================
# RTL-AIRBAND / SDR MANAGER
# ============================================================================

# RTLAirbandManager removed — absorbed into SDRPlugin in sdr_plugin.py


