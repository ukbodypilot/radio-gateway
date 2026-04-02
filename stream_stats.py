"""Broadcastify/Icecast stream statistics and DarkIce management.

Extracted from gateway_core.py.
"""

import os
import re
import subprocess
import time


def find_darkice_pid(gw):
    """Find a running DarkIce process. Returns PID (int) or None."""
    try:
        result = subprocess.run(['pgrep', '-x', 'darkice'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            # pgrep may return multiple PIDs — take the first
            return int(result.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def get_darkice_stats(gw):
    """Get live DarkIce streaming statistics. Returns dict or None."""
    pid = gw._darkice_pid
    if not pid:
        return None
    stats = {}
    try:
        # Process uptime from /proc/pid/stat (field 22 = start time in ticks)
        with open('/proc/uptime') as f:
            sys_uptime = float(f.read().split()[0])
        with open(f'/proc/{pid}/stat') as f:
            start_ticks = int(f.read().split()[21])
        clk_tck = os.sysconf('SC_CLK_TCK')
        stats['uptime'] = int(sys_uptime - start_ticks / clk_tck)
    except Exception:
        stats['uptime'] = 0
    try:
        # TCP connection stats via ss -ti to Broadcastify server
        server = str(getattr(gw.config, 'STREAM_SERVER', '')).strip()
        if not server or server == 'localhost':
            # Find remote IP from /proc/pid/net/tcp
            with open(f'/proc/{pid}/net/tcp') as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if parts[3] == '01':  # ESTABLISHED
                        remote_hex = parts[2].split(':')[0]
                        rip = '.'.join(str(int(remote_hex[i:i+2], 16)) for i in (6, 4, 2, 0))
                        if rip not in ('127.0.0.1', '0.0.0.0'):
                            server = rip
                            break
        if server:
            result = subprocess.run(['ss', '-ti', 'dst', server],
                                    capture_output=True, text=True, timeout=3)
            out = result.stdout
            # Parse key=value pairs from ss extended info
            for key in ('bytes_sent', 'bytes_acked', 'bytes_received',
                        'segs_out', 'segs_in', 'data_segs_out'):
                m = re.search(rf'{key}:(\d+)', out)
                if m:
                    stats[key] = int(m.group(1))
            m = re.search(r'rtt:([\d.]+)/([\d.]+)', out)
            if m:
                stats['rtt'] = float(m.group(1))
            m = re.search(r'send ([\d.]+)(\w+)', out)
            if m:
                stats['send_rate'] = m.group(1) + m.group(2)
            m = re.search(r'busy:(\d+)ms', out)
            if m:
                stats['busy_ms'] = int(m.group(1))
            # TCP connection established = connected
            stats['connected'] = 'ESTAB' in out
        else:
            stats['connected'] = False
    except Exception:
        stats['connected'] = False
    return stats


def get_stream_stats(gw):
    """Get live streaming statistics from direct Icecast connection."""
    so = getattr(gw, 'stream_output', None)
    if not so or not so.connected:
        return {}
    uptime_s = int(so.uptime)
    return {
        'connected': True,
        'uptime': uptime_s,
        'bytes_sent': int(so._bytes_sent),
        'send_rate': f"{so._bytes_sent * 8 / max(uptime_s, 1) / 1000:.1f} kbps" if uptime_s > 0 else '—',
        'server': getattr(gw.config, 'STREAM_SERVER', ''),
        'mount': getattr(gw.config, 'STREAM_MOUNT', ''),
        'bitrate': int(getattr(gw.config, 'STREAM_BITRATE', 16)),
    }


def get_darkice_stats_cached(gw):
    """Return cached DarkIce stats, refreshing every 5 seconds."""
    now = time.time()
    if now - gw._darkice_stats_time > 5:
        gw._darkice_stats_cache = get_darkice_stats(gw)
        gw._darkice_stats_time = now
    return gw._darkice_stats_cache


def restart_darkice(gw):
    """Restart DarkIce after it has died."""
    try:
        subprocess.Popen(
            ['darkice', '-c', '/etc/darkice.cfg'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        pid = find_darkice_pid(gw)
        gw._darkice_restart_count += 1
        if pid:
            gw._darkice_pid = pid
            print(f"\n  DarkIce restarted (PID {pid}), total restarts: {gw._darkice_restart_count}")
        else:
            print(f"\n  DarkIce restart failed — process not found after launch")
    except Exception as e:
        gw._darkice_restart_count += 1
        print(f"\n  DarkIce restart error: {e}")
