"""Email notification sender (Gmail SMTP)."""

import threading
import time

try:
    from gateway_core import __version__
except ImportError:
    __version__ = "2.0.0"

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

    def send_tunnel_changed(self, new_url):
        """Send notification that the Cloudflare tunnel URL has changed."""
        import datetime
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        lines = [
            f"Cloudflare tunnel URL changed at {now}",
            "",
            f"Gateway:   {new_url}",
            f"Config:    {new_url}/config",
            f"Monitor:   {new_url}/monitor",
            f"Monitor App: {new_url}/ws_monitor",
            f"Voice Tmux: {new_url.rstrip('/')}/voice",
            "",
            "The previous tunnel link has expired. Update your bookmarks.",
            "",
            "-- Radio Gateway",
        ]

        hostname = ''
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            pass

        subject = f"Tunnel URL Changed{' — ' + hostname if hostname else ''}"
        self.send(subject, '\n'.join(lines))

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


