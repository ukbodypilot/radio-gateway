"""Extracted from web_server.py during Phase 1.B.

These methods stay class-bound (the original code reads/writes plenty of
self.* state); composed back into ``WebConfigServer`` via inheritance.
Module-level helpers can land here too as the surface gets carved up
further.
"""

import os
import socket
import socketserver
import subprocess
import threading
import time


class _SysinfoMixin:
    def _get_sysinfo(self):
        """Gather system status: CPU, memory, disk I/O, network, temps, IPs."""
        import os
        info = {}
        try:
            # CPU usage — average across cores from /proc/stat delta
            # Cache result for 1s minimum to prevent near-zero deltas from rapid polls
            # Split: critical = us+sy+hi+si (real-time work, pressures audio),
            #        background = nice (yields automatically, spare capacity),
            #        iowait = disk-starved (invisible in old total).
            if not hasattr(self, '_prev_cpu'):
                self._prev_cpu = None
                self._prev_cpu_time = 0
                self._cached_cpu = {'cpu_pct': 0.0, 'cpu_critical_pct': 0.0,
                                    'cpu_background_pct': 0.0, 'cpu_iowait_pct': 0.0}
            now = time.monotonic()
            if now - self._prev_cpu_time < 1.0:
                info.update(self._cached_cpu)
            else:
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                parts = line.split()
                cur = [int(x) for x in parts[1:8]]  # user nice sys idle iowait irq softirq
                if self._prev_cpu:
                    d = [c - p for c, p in zip(cur, self._prev_cpu)]
                    total = sum(d) or 1
                    us, ni, sy, idle, io, hi, si = d
                    critical = us + sy + hi + si
                    self._cached_cpu = {
                        'cpu_critical_pct': round(100.0 * critical / total, 1),
                        'cpu_background_pct': round(100.0 * ni / total, 1),
                        'cpu_iowait_pct': round(100.0 * io / total, 1),
                        'cpu_pct': round(100.0 * (total - idle) / total, 1),
                    }
                info.update(self._cached_cpu)
                self._prev_cpu = cur
                self._prev_cpu_time = now

            # Per-core CPU count
            info['cpu_cores'] = os.cpu_count() or 1

            # Load average + per-core (load/cores > 1.0 = genuinely queueing)
            load1, load5, load15 = os.getloadavg()
            info['load'] = [round(load1, 2), round(load5, 2), round(load15, 2)]
            info['load_per_core'] = round(load1 / (info['cpu_cores'] or 1), 2)
        except Exception:
            info['cpu_pct'] = 0.0
            info['cpu_critical_pct'] = 0.0
            info['cpu_background_pct'] = 0.0
            info['cpu_iowait_pct'] = 0.0
            info['cpu_cores'] = 1
            info['load'] = [0, 0, 0]
            info['load_per_core'] = 0.0

        try:
            # Memory from /proc/meminfo
            mem = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts[0].rstrip(':') in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                        mem[parts[0].rstrip(':')] = int(parts[1])  # kB
            total = mem.get('MemTotal', 0)
            avail = mem.get('MemAvailable', 0)
            used = total - avail
            info['mem_total_mb'] = round(total / 1024)
            info['mem_used_mb'] = round(used / 1024)
            info['mem_pct'] = round(100.0 * used / total, 1) if total else 0
            swap_total = mem.get('SwapTotal', 0)
            swap_free = mem.get('SwapFree', 0)
            info['swap_total_mb'] = round(swap_total / 1024)
            info['swap_used_mb'] = round((swap_total - swap_free) / 1024)
        except Exception:
            info['mem_total_mb'] = 0
            info['mem_used_mb'] = 0
            info['mem_pct'] = 0
            info['swap_total_mb'] = 0
            info['swap_used_mb'] = 0

        try:
            # Disk I/O from /proc/diskstats delta
            if not hasattr(self, '_prev_disk'):
                self._prev_disk = None
                self._prev_disk_time = 0
            now = time.monotonic()
            disk_r = 0
            disk_w = 0
            cur_disk = {}
            import re as _re
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    parts = line.split()
                    name = parts[2]
                    # Only count whole disks (sda, nvme0n1, mmcblk0) not partitions
                    if name.startswith('loop') or name.startswith('ram'):
                        continue
                    # Skip partitions: sdXN, nvme0n1pN, mmcblk0pN
                    if _re.match(r'^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)$', name):
                        # sectors read (field 5, idx 5), sectors written (field 9, idx 9)
                        rd = int(parts[5])
                        wr = int(parts[9])
                        cur_disk[name] = (rd, wr)
            if self._prev_disk and (now - self._prev_disk_time) > 0:
                dt = now - self._prev_disk_time
                for name in cur_disk:
                    if name in self._prev_disk:
                        dr = cur_disk[name][0] - self._prev_disk[name][0]
                        dw = cur_disk[name][1] - self._prev_disk[name][1]
                        disk_r += dr * 512  # sectors are 512 bytes
                        disk_w += dw * 512
                disk_r = disk_r / dt
                disk_w = disk_w / dt
            self._prev_disk = cur_disk
            self._prev_disk_time = now
            info['disk_read_bps'] = round(disk_r)
            info['disk_write_bps'] = round(disk_w)
        except Exception:
            info['disk_read_bps'] = 0
            info['disk_write_bps'] = 0

        try:
            # Disk usage for root filesystem
            st = os.statvfs('/')
            total_bytes = st.f_frsize * st.f_blocks
            free_bytes = st.f_frsize * st.f_bavail
            used_bytes = total_bytes - free_bytes
            info['disk_total_gb'] = round(total_bytes / (1024**3), 1)
            info['disk_used_gb'] = round(used_bytes / (1024**3), 1)
            info['disk_pct'] = round(100.0 * used_bytes / total_bytes, 1) if total_bytes else 0
        except Exception:
            info['disk_total_gb'] = 0
            info['disk_used_gb'] = 0
            info['disk_pct'] = 0

        try:
            # Network I/O from /proc/net/dev delta
            if not hasattr(self, '_prev_net'):
                self._prev_net = None
                self._prev_net_time = 0
            now = time.monotonic()
            cur_net = {}
            with open('/proc/net/dev', 'r') as f:
                for line in f:
                    if ':' not in line:
                        continue
                    iface, rest = line.split(':', 1)
                    iface = iface.strip()
                    if iface == 'lo':
                        continue
                    parts = rest.split()
                    rx_bytes = int(parts[0])
                    tx_bytes = int(parts[8])
                    cur_net[iface] = (rx_bytes, tx_bytes)
            net_rx = 0
            net_tx = 0
            if self._prev_net and (now - self._prev_net_time) > 0:
                dt = now - self._prev_net_time
                for iface in cur_net:
                    if iface in self._prev_net:
                        net_rx += cur_net[iface][0] - self._prev_net[iface][0]
                        net_tx += cur_net[iface][1] - self._prev_net[iface][1]
                net_rx = net_rx / dt
                net_tx = net_tx / dt
            self._prev_net = cur_net
            self._prev_net_time = now
            info['net_rx_bps'] = round(net_rx)
            info['net_tx_bps'] = round(net_tx)
        except Exception:
            info['net_rx_bps'] = 0
            info['net_tx_bps'] = 0

        try:
            # TCP connection count
            count = 0
            with open('/proc/net/tcp', 'r') as f:
                for line in f:
                    if line.strip().startswith('sl'):
                        continue
                    count += 1
            with open('/proc/net/tcp6', 'r') as f:
                for line in f:
                    if line.strip().startswith('sl'):
                        continue
                    count += 1
            info['tcp_connections'] = count
        except Exception:
            info['tcp_connections'] = 0

        try:
            # Temperatures from /sys/class/thermal or /sys/class/hwmon
            temps = []
            # thermal zones
            import glob as _glob
            for tz in sorted(_glob.glob('/sys/class/thermal/thermal_zone*/temp')):
                try:
                    zone_dir = os.path.dirname(tz)
                    with open(tz, 'r') as f:
                        val = int(f.read().strip()) / 1000.0
                    label = 'CPU'
                    type_file = os.path.join(zone_dir, 'type')
                    if os.path.exists(type_file):
                        with open(type_file, 'r') as f:
                            label = f.read().strip()
                    if val > 0:
                        temps.append({'label': label, 'temp': round(val, 1)})
                except Exception:
                    pass
            # hwmon sensors (for GPU, NVMe, etc.)
            for hwmon in sorted(_glob.glob('/sys/class/hwmon/hwmon*')):
                try:
                    name_file = os.path.join(hwmon, 'name')
                    hw_name = ''
                    if os.path.exists(name_file):
                        with open(name_file, 'r') as f:
                            hw_name = f.read().strip()
                    for tf in sorted(_glob.glob(os.path.join(hwmon, 'temp*_input'))):
                        with open(tf, 'r') as f:
                            val = int(f.read().strip()) / 1000.0
                        # Try to find a label
                        label_file = tf.replace('_input', '_label')
                        lbl = hw_name
                        if os.path.exists(label_file):
                            with open(label_file, 'r') as f:
                                lbl = f.read().strip()
                        if val > 0 and not any(t['label'] == lbl and t['temp'] == round(val, 1) for t in temps):
                            temps.append({'label': lbl, 'temp': round(val, 1)})
                except Exception:
                    pass
            info['temps'] = temps
        except Exception:
            info['temps'] = []

        try:
            # IP addresses
            import socket
            # `ip -4 -o addr show` used to be spawned on every /sysinfo
            # poll; addresses rarely change, so cache them for 30 s.
            _now = time.monotonic()
            _cached = getattr(self, '_sysinfo_ips_cache', None)
            if _cached and _now - _cached[0] < 30.0:
                ips = _cached[1]
            else:
                ips = []
                import subprocess
                result = subprocess.run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True, text=True, timeout=2)
                for line in result.stdout.strip().split('\n'):
                    if not line:
                        continue
                    parts = line.split()
                    # Format: idx iface inet addr/prefix ...
                    iface = parts[1]
                    if iface == 'lo':
                        continue
                    addr = parts[3].split('/')[0]
                    ips.append({'iface': iface, 'addr': addr})
                self._sysinfo_ips_cache = (_now, ips)
            info['ips'] = ips

            # Hostname
            info['hostname'] = socket.gethostname()
            info['gateway_name'] = str(getattr(self.config, 'GATEWAY_NAME', '') or '').strip() if self.gateway else ''
            # Callsign (from PACKET_CALLSIGN) — shown in the shell identity plate.
            cs = str(getattr(self.config, 'PACKET_CALLSIGN', '') or '').strip().upper()
            info['callsign'] = cs if cs and cs != 'N0CALL' else ''

            # Cloudflare tunnel URL for display in system status
            if self.gateway and self.gateway.cloudflare_tunnel:
                info['tunnel_url'] = self.gateway.cloudflare_tunnel.get_url() or ''
            else:
                info['tunnel_url'] = ''
        except Exception:
            info['ips'] = []
            info['hostname'] = ''

        return info





