"""Extracted from web_server.py during Phase 1.B.

These methods stay class-bound (the original code reads/writes plenty of
self.* state); composed back into ``WebConfigServer`` via inheritance.
Module-level helpers can land here too as the surface gets carved up
further.
"""

import os
import subprocess
import threading
import time


class _CertsMixin:
    def _get_cert(self, mode):
        """Get SSL cert/key paths. Returns (cert_path, key_path) or (None, None)."""
        cert_dir = os.path.join(os.path.dirname(os.path.abspath(self.config.config_file)), 'certs')
        os.makedirs(cert_dir, exist_ok=True)

        if mode == 'letsencrypt':
            domain = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '').strip()
            if not domain:
                print(f"  [WebConfig] Let's Encrypt requires DDNS_HOSTNAME to be set")
                return None, None
            cert_file = os.path.join(cert_dir, 'fullchain.pem')
            key_file = os.path.join(cert_dir, 'privkey.pem')
            # Check if cert exists and is not expiring within 30 days
            if os.path.exists(cert_file) and os.path.exists(key_file):
                if not self._cert_expiring_soon(cert_file, 30):
                    print(f"  [WebConfig] Using existing Let's Encrypt cert for {domain}")
                    return cert_file, key_file
                print(f"  [WebConfig] Certificate expiring soon, renewing...")
            # Obtain/renew via certbot
            if self._run_certbot(domain, cert_dir):
                return cert_file, key_file
            # Existing cert still valid enough? Use it even if renewal failed
            if os.path.exists(cert_file) and os.path.exists(key_file):
                print(f"  [WebConfig] Certbot failed but existing cert still present, using it")
                return cert_file, key_file
            print(f"  [WebConfig] Let's Encrypt failed, falling back to self-signed")
            mode = 'self-signed'

        # self-signed
        cert_file = os.path.join(cert_dir, 'self_signed.pem')
        key_file = os.path.join(cert_dir, 'self_signed_key.pem')
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
            print(f"  [WebConfig] Generating self-signed certificate...")
            import subprocess
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', key_file, '-out', cert_file,
                '-days', '3650', '-nodes',
                '-subj', '/CN=RadioGateway'
            ], capture_output=True)
            print(f"  [WebConfig] Self-signed certificate saved")
        return cert_file, key_file

    def _run_certbot(self, domain, cert_dir):
        """Run certbot standalone to obtain/renew a certificate."""
        email = str(getattr(self.config, 'DDNS_USERNAME', '') or '').strip()
        email_args = ['--email', email, '--no-eff-email'] if email and '@' in email else ['--register-unsafely-without-email']
        port = int(getattr(self.config, 'WEB_CONFIG_PORT', 8080))
        cmd = [
            'certbot', 'certonly', '--standalone',
            '--preferred-challenges', 'http',
            '--http-01-port', '80',
            '-d', domain,
            '--cert-path', os.path.join(cert_dir, 'fullchain.pem'),
            '--key-path', os.path.join(cert_dir, 'privkey.pem'),
            '--fullchain-path', os.path.join(cert_dir, 'fullchain.pem'),
            '--config-dir', os.path.join(cert_dir, 'certbot_config'),
            '--work-dir', os.path.join(cert_dir, 'certbot_work'),
            '--logs-dir', os.path.join(cert_dir, 'certbot_logs'),
            '--non-interactive', '--agree-tos',
        ] + email_args
        print(f"  [WebConfig] Running certbot for {domain}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                # certbot may put certs in its own live/ dir; copy them to our cert_dir
                live_dir = os.path.join(cert_dir, 'certbot_config', 'live', domain)
                target_cert = os.path.join(cert_dir, 'fullchain.pem')
                target_key = os.path.join(cert_dir, 'privkey.pem')
                if os.path.isdir(live_dir):
                    # certbot uses symlinks into archive/; resolve and copy
                    import shutil
                    live_cert = os.path.join(live_dir, 'fullchain.pem')
                    live_key = os.path.join(live_dir, 'privkey.pem')
                    if os.path.exists(live_cert):
                        shutil.copy2(os.path.realpath(live_cert), target_cert)
                    if os.path.exists(live_key):
                        shutil.copy2(os.path.realpath(live_key), target_key)
                print(f"  [WebConfig] Let's Encrypt certificate obtained for {domain}")
                return True
            else:
                print(f"  [WebConfig] Certbot failed (exit {result.returncode})")
                stderr = result.stderr.strip()
                if stderr:
                    for line in stderr.split('\n')[-3:]:
                        print(f"  [WebConfig]   {line}")
                return False
        except FileNotFoundError:
            print(f"  [WebConfig] certbot not found — install with: sudo apt install certbot")
            return False
        except subprocess.TimeoutExpired:
            print(f"  [WebConfig] Certbot timed out")
            return False
        except Exception as e:
            print(f"  [WebConfig] Certbot error: {e}")
            return False

    def _cert_expiring_soon(self, cert_path, days=30):
        """Check if certificate expires within N days."""
        try:
            import subprocess
            result = subprocess.run(
                ['openssl', 'x509', '-enddate', '-noout', '-in', cert_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return True
            # Parse "notAfter=Mar  8 12:00:00 2026 GMT"
            date_str = result.stdout.strip().split('=', 1)[1]
            # openssl format: "Mar  8 12:00:00 2026 GMT"
            from datetime import datetime as dt, timedelta, timezone
            expiry = dt.strptime(date_str.strip(), '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
            remaining = expiry - dt.now(timezone.utc)
            return remaining < timedelta(days=days)
        except Exception:
            return True  # if we can't check, assume it needs renewal

    def _start_renewal_thread(self, cert_path):
        """Background thread to check/renew Let's Encrypt cert every 12 hours."""
        def _renewal_loop():
            while True:
                time.sleep(12 * 3600)  # 12 hours
                try:
                    if self._cert_expiring_soon(cert_path, 30):
                        domain = str(getattr(self.config, 'DDNS_HOSTNAME', '') or '').strip()
                        cert_dir = os.path.dirname(cert_path)
                        if domain and self._run_certbot(domain, cert_dir):
                            print(f"\n[WebConfig] Certificate renewed — restart gateway to use new cert")
                except Exception as e:
                    print(f"\n[WebConfig] Renewal check error: {e}")

        t = threading.Thread(target=_renewal_loop, name='CertRenewal', daemon=True)
        t.start()

