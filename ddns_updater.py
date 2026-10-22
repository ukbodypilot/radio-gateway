"""Dynamic DNS updater (No-IP compatible)."""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import threading
import time

class DDNSUpdater:
    """Dynamic DNS updater (No-IP compatible protocol).

    Runs a background thread that periodically updates a DDNS hostname
    with the machine's current public IP via the No-IP update API.
    """

    def __init__(self, config, gateway=None):
        self.config = config
        # Only used to reach gw.email_notifier, which is created *after* the
        # DDNS updater in gateway_setup — so it is looked up lazily, never cached.
        self.gateway = gateway
        self._stop = False
        self._thread = None
        self._last_ip = None
        self._last_status = None   # 'good', 'nochg', or error string
        self._last_update = 0      # time.time() of last update attempt
        # Counters — a stuck DDNS is diagnosed by which of these is not moving
        self.stats = {
            'checks': 0,           # public-IP polls
            'checkip_failures': 0, # public-IP lookup errors
            'updates_sent': 0,     # actual calls to the DDNS provider
            'updates_skipped': 0,  # suppressed because the IP was unchanged
            'update_failures': 0,  # provider returned an error code
            'backoff_skips': 0,    # suppressed while backing off after a failure
            'resolve_failures': 0, # hostname did not resolve at all (expired?)
            'mismatches': 0,       # resolved to an IP that is not ours
            'alerts_sent': 0,      # mismatch alert emails sent
        }
        self._resolved_ip = None   # what DNS currently says the hostname is
        self._mismatch_since = 0   # consecutive bad verification cycles
        self._last_alert = 0       # time.time() of last alert email

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

    def get_stats(self):
        """Return counters plus last-known state (for /status and MCP)."""
        return dict(self.stats, last_ip=self._last_ip,
                    last_status=self._last_status,
                    last_update=self._last_update,
                    resolved_ip=self._resolved_ip,
                    mismatch_since=self._mismatch_since)

    def _get_public_ip(self, url):
        """Return current public IP as a string, or None if the lookup failed."""
        import urllib.request
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'RadioGateway/1.0 radio_gateway.py')
            with urllib.request.urlopen(req, timeout=10) as resp:
                ip = resp.read().decode().strip()
            # Cheap sanity check — reject HTML error pages etc.
            parts = ip.split('.')
            if len(parts) == 4 and all(p.isdigit() and int(p) < 256 for p in parts):
                return ip
            if ':' in ip and len(ip) <= 45:   # IPv6
                return ip
        except Exception:
            log_exc()
        return None

    def _resolve_hostname(self, hostname):
        """Resolve hostname via DNS. Returns an IP string, or None if it does
        not resolve at all (which is what an expired hostname looks like).

        Note: getaddrinfo takes no timeout argument, so this is bounded only by
        the resolver's own settings. It runs on the dedicated DDNS thread, so a
        slow resolver delays DDNS checks and nothing else.
        """
        import socket
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
            for fam, _, _, _, sockaddr in infos:
                if fam == socket.AF_INET:
                    return sockaddr[0]
            return infos[0][4][0] if infos else None
        except Exception:
            return None

    def _verify_dns(self, hostname, cur_ip, grace, alert_interval):
        """Compare what DNS says against our real public IP, and alert on a
        sustained disagreement.

        This is the check that catches hostname EXPIRY. A No-IP DDNS Key updates
        whatever hostname it is bound to and ignores the hostname parameter, so
        a dead hostname does not necessarily report 'nohost' — the update can
        look successful while the name resolves to nothing.
        """
        resolved = self._resolve_hostname(hostname)
        self._resolved_ip = resolved

        if resolved is None:
            self.stats['resolve_failures'] += 1
            problem = f"{hostname} does not resolve (hostname expired or deleted?)"
        elif cur_ip and resolved != cur_ip:
            self.stats['mismatches'] += 1
            problem = (f"{hostname} resolves to {resolved} but our public IP "
                       f"is {cur_ip}")
        else:
            if self._mismatch_since:
                print(f"\n[DDNS] {hostname} verified OK again ({resolved})")
                self._notify(f"DDNS recovered: {hostname}",
                             f"{hostname} now resolves correctly to {resolved}.")
            self._mismatch_since = 0
            return

        self._mismatch_since += 1
        # Tolerate a few cycles so normal DNS propagation is not alarming.
        if self._mismatch_since < grace:
            return

        now = time.time()
        if now - self._last_alert < alert_interval:
            return
        self._last_alert = now
        self.stats['alerts_sent'] += 1
        print(f"\n[DDNS] *** VERIFY FAILED: {problem}")
        self._notify(
            f"DDNS problem: {hostname}",
            f"{problem}\n\n"
            f"Sustained for {self._mismatch_since} consecutive checks.\n\n"
            f"Most likely cause: free No-IP hostnames expire unless confirmed\n"
            f"via their emailed link every 30 days. DDNS updates do NOT reset\n"
            f"that clock. Check your No-IP account.\n\n"
            f"Last provider response: {self._last_status}\n"
            f"Counters: {self.stats}")

    def _notify(self, subject, body):
        """Send via the gateway's EmailNotifier if one is configured."""
        try:
            notifier = getattr(self.gateway, 'email_notifier', None)
            if notifier and notifier.is_configured():
                notifier.send(subject, body)
        except Exception as e:
            print(f"  [DDNS] Alert send failed: {e}")

    def _update_loop(self):
        import urllib.request
        import base64

        username = str(self.config.DDNS_USERNAME)
        password = str(self.config.DDNS_PASSWORD)
        hostname = str(self.config.DDNS_HOSTNAME)
        url_base = str(getattr(self.config, 'DDNS_UPDATE_URL',
                                'https://dynupdate.no-ip.com/nic/update') or
                       'https://dynupdate.no-ip.com/nic/update')
        checkip_url = str(getattr(self.config, 'DDNS_CHECKIP_URL',
                                  'https://api.ipify.org') or
                          'https://api.ipify.org')
        interval = max(60, int(getattr(self.config, 'DDNS_UPDATE_INTERVAL', 300)))
        force_interval = max(interval,
                             int(getattr(self.config, 'DDNS_FORCE_INTERVAL', 86400)))
        verify = bool(getattr(self.config, 'DDNS_VERIFY_DNS', True))
        grace = max(1, int(getattr(self.config, 'DDNS_MISMATCH_GRACE', 3)))
        alert_interval = int(getattr(self.config, 'DDNS_ALERT_INTERVAL', 86400))
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()

        # Codes that will never fix themselves — worth shouting about rather
        # than burying in the log at one line per cycle.
        fatal = ('nohost', 'badauth', 'badagent', 'abuse', '!donator', 'notfqdn')

        sent_ip = None        # IP the provider last confirmed for us
        last_sent = 0.0       # time.time() of the last actual provider call
        retry_after = 0.0     # honour backoff after a failure
        backoff = 0           # current failure backoff in seconds

        while not self._stop:
            now = time.time()
            self.stats['checks'] += 1
            cur_ip = self._get_public_ip(checkip_url)
            if cur_ip is None:
                self.stats['checkip_failures'] += 1

            # Skip the provider call when nothing changed. A periodic forced
            # update still runs so a record we lost track of gets re-asserted.
            force_due = (now - last_sent) >= force_interval
            if now < retry_after:
                # A previous update failed; retrying every cycle would both be
                # useless (fatal codes never self-heal) and look like abuse.
                self.stats['backoff_skips'] += 1
            elif cur_ip is not None and cur_ip == sent_ip and not force_due:
                self.stats['updates_skipped'] += 1
            else:
                try:
                    url = f"{url_base}?hostname={hostname}"
                    req = urllib.request.Request(url)
                    req.add_header('Authorization', f'Basic {creds}')
                    req.add_header('User-Agent', 'RadioGateway/1.0 radio_gateway.py')
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        result = resp.read().decode().strip()
                except Exception as e:
                    result = f"error: {e}"

                self.stats['updates_sent'] += 1
                last_sent = now

                # Parse response: "good IP", "nochg IP", or error codes
                parts = result.split()
                code = parts[0] if parts else result
                ip = parts[1] if len(parts) > 1 else ''

                self._last_update = now
                self._last_status = code
                if code in ('good', 'nochg'):
                    if code == 'good' or self._last_ip is None:
                        print(f"\n[DDNS] {hostname} → {ip}")
                    self._last_ip = ip
                    sent_ip = ip or cur_ip
                    backoff = 0
                    retry_after = 0.0
                else:
                    self.stats['update_failures'] += 1
                    sent_ip = None
                    # Exponential backoff, capped at 6h. Fatal codes start at
                    # the cap — no amount of retrying will fix them.
                    if code in fatal:
                        backoff = 21600
                        print(f"\n[DDNS] *** {hostname}: '{code}' — this will NOT "
                              f"recover on its own. Free No-IP hostnames expire "
                              f"unless confirmed by email every 30 days. "
                              f"Retrying every {backoff // 3600}h until fixed.")
                    else:
                        backoff = min(max(interval * 2, backoff * 2), 21600)
                        print(f"\n[DDNS] Update failed: {result} "
                              f"(retry in {backoff}s)")
                    retry_after = now + backoff

            # Independent verification: does the hostname actually resolve to
            # us? This does not depend on the provider's own response, which is
            # why it catches expiry that the update call reports as success.
            if verify:
                try:
                    self._verify_dns(hostname, cur_ip, grace, alert_interval)
                except Exception as e:
                    print(f"  [DDNS] Verify error: {e}")

            # Sleep in small increments so stop is responsive
            for _ in range(int(interval)):
                if self._stop:
                    return
                time.sleep(1)


