"""Dynamic DNS updater (No-IP compatible)."""

import threading
import time

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


