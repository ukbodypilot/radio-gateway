"""USB/IP remote device attachment manager."""

import threading
import time

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


