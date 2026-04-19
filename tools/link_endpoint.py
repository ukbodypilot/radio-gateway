#!/usr/bin/env python3
"""
link_endpoint.py -- Standalone Gateway Link endpoint.

Connects to a Radio Gateway master and streams duplex audio with
command support. Hardware is abstracted via plugins.

Usage:
    python3 link_endpoint.py --server HOST:PORT --name NAME [--plugin audio] [--device DEVICE]

Examples:
    python3 tools/link_endpoint.py --server 192.168.2.140:9700 --name garage-radio
    python3 tools/link_endpoint.py --server 192.168.2.140:9700 --name garage-radio --plugin audio --device hw:1,0
    python3 tools/link_endpoint.py --server 192.168.2.140:9700 --name garage-aioc --plugin aioc
    python3 tools/link_endpoint.py --server 192.168.2.140:9700 --name mobile-kv4p --plugin kv4p --device /dev/ttyUSB0
"""

import argparse
import json
import os
import signal
import struct
import sys
import threading
import time

# Add parent directory to path so we can import gateway_link
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from gateway_link import GatewayLinkClient, AudioPlugin, AIOCPlugin, RadioPlugin, discover_gateway
except ImportError as e:
    print(f"[Endpoint] Failed to import gateway_link: {e}")
    print("[Endpoint] Make sure gateway_link.py is in the parent directory.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------

_PLUGINS = {
    'audio': AudioPlugin,
    'aioc': AIOCPlugin,
}

# Lazy-load optional plugins that have extra dependencies
def _load_d75():
    from d75_link_plugin import D75Plugin
    return D75Plugin

_LAZY_PLUGINS = {
    'd75': _load_d75,
}


def load_plugin(name):
    """Load a plugin by name. Built-in: 'audio', 'aioc', 'd75'."""
    cls = _PLUGINS.get(name)
    if not cls:
        loader = _LAZY_PLUGINS.get(name)
        if loader:
            try:
                cls = loader()
            except ImportError as e:
                print(f"[Endpoint] Plugin '{name}' import failed: {e}")
                sys.exit(1)
    if not cls:
        available = ', '.join(sorted(list(_PLUGINS.keys()) + list(_LAZY_PLUGINS.keys())))
        print(f"[Endpoint] Unknown plugin: {name}. Available: {available}")
        sys.exit(1)
    return cls()


def list_audio_devices():
    """List available audio devices (requires pyaudio)."""
    try:
        import pyaudio
    except ImportError:
        print("[Endpoint] pyaudio not installed -- cannot list devices.")
        print("[Endpoint] Install with: pip install pyaudio")
        return
    pa = pyaudio.PyAudio()
    print(f"[Endpoint] Audio devices ({pa.get_device_count()}):")
    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
            direction = []
            if info.get('maxInputChannels', 0) > 0:
                direction.append('IN')
            if info.get('maxOutputChannels', 0) > 0:
                direction.append('OUT')
            tag = '/'.join(direction) if direction else '---'
            print(f"  [{i}] {info['name']}  ({tag})")
        except Exception:
            continue
    pa.terminate()


def parse_server(server_str):
    """Parse 'host:port' string. Returns (host, port) or exits on error."""
    if ':' not in server_str:
        print(f"[Endpoint] Invalid server address: {server_str}")
        print("[Endpoint] Expected format: HOST:PORT (e.g. 192.168.2.140:9700)")
        sys.exit(1)
    parts = server_str.rsplit(':', 1)
    host = parts[0]
    try:
        port = int(parts[1])
        if not (1 <= port <= 65535):
            raise ValueError("port out of range")
    except ValueError:
        print(f"[Endpoint] Invalid port in '{server_str}' -- must be 1-65535")
        sys.exit(1)
    return host, port


# ---------------------------------------------------------------------------
# Status reporter -- periodic status to master
# ---------------------------------------------------------------------------

class StatusReporter:
    """Sends periodic status frames to the master gateway."""

    def __init__(self, client, plugin, interval=10.0):
        self._client = client
        self._plugin = plugin
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._start_time = time.monotonic()

    def start(self):
        self._thread = threading.Thread(target=self._run,
                                        name="EndpointStatus", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        # Poll at 250ms intervals, send status when dirty or interval elapsed
        _last_send = 0
        while not self._stop.is_set():
            self._stop.wait(0.25)
            if self._stop.is_set():
                break
            _dirty = getattr(self._plugin, '_status_dirty', False)
            _elapsed = time.monotonic() - _last_send
            if _dirty or _elapsed >= self._interval:
                if _dirty:
                    self._plugin._status_dirty = False
                if self._client.connected:
                    try:
                        status = self._plugin.get_status()
                        status['uptime'] = round(time.monotonic() - self._start_time, 1)
                        status['code_version'] = _compute_local_version()
                        self._client.send_status(status)
                        _last_send = time.monotonic()
                    except Exception as e:
                        print(f"[Endpoint] Status send error: {e}")


# ---------------------------------------------------------------------------
# Gain utility
# ---------------------------------------------------------------------------

def apply_gain(pcm, gain):
    """Apply a gain multiplier to 16-bit signed LE PCM audio in-place."""
    if gain == 1.0:
        return pcm
    samples = struct.unpack(f'<{len(pcm) // 2}h', pcm)
    gained = []
    for s in samples:
        v = int(s * gain)
        # Clamp to int16 range
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        gained.append(v)
    return struct.pack(f'<{len(gained)}h', *gained)


# ---------------------------------------------------------------------------
# Self-update — check gateway for newer endpoint code
# ---------------------------------------------------------------------------

def _compute_local_version():
    """Compute hash of local endpoint files."""
    import hashlib
    h = hashlib.sha256()
    _dir = os.path.dirname(os.path.abspath(__file__))
    for fname in ['gateway_link.py', 'link_endpoint.py',
                  'd75_link_plugin.py', 'remote_bt_proxy.py']:
        path = os.path.join(_dir, fname)
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()[:16]


# Serializes concurrent update attempts so startup, on_connect and the
# hourly checker don't race on the file write. Non-blocking acquire: if
# an update is already in flight, the next caller is a no-op.
_update_lock = threading.Lock()


def _check_for_update(gateway_url):
    """Check gateway for newer endpoint code. Returns True if updated + restart needed."""
    if not _update_lock.acquire(blocking=False):
        return False
    try:
        return _check_for_update_locked(gateway_url)
    finally:
        _update_lock.release()


def _check_for_update_locked(gateway_url):
    import urllib.request, base64
    try:
        # Check version
        local_ver = _compute_local_version()
        req = urllib.request.Request(f'{gateway_url}/api/endpoint/version')
        resp = urllib.request.urlopen(req, timeout=10)
        remote = json.loads(resp.read())
        remote_ver = remote.get('version', '')

        if remote_ver == local_ver:
            print(f"[Update] Code is up to date (v={local_ver})")
            return False

        print(f"[Update] New version available: local={local_ver} remote={remote_ver}")

        # Download files
        req = urllib.request.Request(f'{gateway_url}/api/endpoint/files')
        resp = urllib.request.urlopen(req, timeout=30)
        bundle = json.loads(resp.read())

        _dir = os.path.dirname(os.path.abspath(__file__))
        updated = 0
        for fname, b64data in bundle.items():
            content = base64.b64decode(b64data)
            path = os.path.join(_dir, fname)
            # Check if content actually changed
            try:
                with open(path, 'rb') as f:
                    if f.read() == content:
                        continue
            except FileNotFoundError:
                pass
            with open(path, 'wb') as f:
                f.write(content)
            updated += 1
            print(f"[Update] Updated: {fname} ({len(content)} bytes)")

        if updated > 0:
            print(f"[Update] {updated} file(s) updated — restarting...")
            # Try to persist updates to base dir (survives reboot on ro root)
            _base = os.path.join(_dir, '..')
            if os.path.isdir(os.path.join(_base, 'run')):
                # We're in a tmpfs /run subdir — persist to parent
                try:
                    subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,rw', '/'],
                                   capture_output=True, timeout=5)
                    for fname, b64data in bundle.items():
                        _ppath = os.path.join(_base, fname)
                        with open(_ppath, 'wb') as pf:
                            pf.write(base64.b64decode(b64data))
                    subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,ro', '/'],
                                   capture_output=True, timeout=5)
                    print(f"[Update] Persisted to base dir")
                except Exception as pe:
                    print(f"[Update] Persist failed (ok, tmpfs has new code): {pe}")
            return True
        else:
            print(f"[Update] Files unchanged despite version mismatch")
            return False

    except Exception as e:
        print(f"[Update] Check failed: {e} (continuing with current code)")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Gateway Link Endpoint -- connect to a Radio Gateway master '
                    'and stream duplex audio.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  %(prog)s --server 192.168.2.140:9700 --name garage-radio\n'
               '  %(prog)s --server 192.168.2.140:9700 --name garage --device hw:1,0\n'
               '  %(prog)s --list-devices\n')

    parser.add_argument('--server', help='Gateway address HOST:PORT')
    parser.add_argument('--name', help='Endpoint name (shown on master dashboard)')
    parser.add_argument('--plugin', default='audio',
                        help='Hardware plugin (default: audio)')
    parser.add_argument('--device', default='',
                        help='Audio/serial device name, index, or path')
    parser.add_argument('--rate', type=int, default=48000,
                        help='Sample rate in Hz (default: 48000)')
    parser.add_argument('--gain', type=float, default=1.0,
                        help='Input gain multiplier (default: 1.0)')
    parser.add_argument('--status-interval', type=float, default=10.0,
                        help='Status report interval in seconds (default: 10)')
    parser.add_argument('--tunnel-url',
                        help='WebSocket tunnel URL (wss://xxx.trycloudflare.com/ws/link)')
    parser.add_argument('--gdrive-remote', default='gdrive',
                        help='rclone remote name for Google Drive (default: gdrive)')
    parser.add_argument('--gdrive-folder', default='radio-gateway',
                        help='Google Drive folder path (default: radio-gateway)')
    parser.add_argument('--rclone-config', default='',
                        help='Path to rclone.conf (default: system default)')
    parser.add_argument('--no-update', action='store_true',
                        help='Skip self-update check on startup')
    parser.add_argument('--list-devices', action='store_true',
                        help='List available audio devices and exit')

    args = parser.parse_args()

    # --list-devices mode
    if args.list_devices:
        list_audio_devices()
        sys.exit(0)

    # Self-update check — try LAN gateway first, then tunnel URL
    if not args.no_update:
        _update_url = None
        if args.server:
            _host = args.server.rsplit(':', 1)[0]
            _update_url = f'http://{_host}:8080'
        if _update_url and _check_for_update(_update_url):
            os.execv(sys.executable, [sys.executable] + sys.argv + ['--no-update'])
        elif args.tunnel_url:
            # Try via tunnel (convert wss:// ws link to https:// base)
            _tun = args.tunnel_url.replace('wss://', 'https://').replace('ws://', 'http://')
            _tun = _tun.replace('/ws/link', '')
            if _check_for_update(_tun):
                os.execv(sys.executable, [sys.executable] + sys.argv + ['--no-update'])

    # Load cached settings
    _settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')
    _settings = {}
    try:
        with open(_settings_path) as f:
            _settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Resolve server address
    host, port = None, None
    if args.server:
        host, port = parse_server(args.server)
    else:
        print("[Endpoint] No --server specified — discovering gateway via mDNS...")
        result = discover_gateway(timeout=10)
        if result:
            host, port = result
        else:
            print("[Endpoint] mDNS discovery failed — will use tunnel fallback")

    # Resolve tunnel URL: CLI → settings cache → Google Drive
    ws_url = args.tunnel_url or _settings.get('tunnel_url')

    def _resolve_tunnel_url():
        """Fetch tunnel URL from Google Drive via rclone."""
        try:
            cmd = ['rclone']
            if args.rclone_config:
                cmd += ['--config', args.rclone_config]
            cmd += ['cat', f'{args.gdrive_remote}:{args.gdrive_folder}/tunnel_url.json']
            r = __import__('subprocess').run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                url = data.get('ws_link')
                if url:
                    print(f"  [Link] Tunnel URL from Drive: {url}")
                    # Cache it
                    _settings['tunnel_url'] = url
                    try:
                        with open(_settings_path, 'w') as f:
                            json.dump(_settings, f, indent=2)
                    except Exception:
                        pass
                    return url
        except Exception as e:
            print(f"  [Link] Drive URL fetch failed: {e}")
        return None

    # If no URL at all, try Drive now
    if not ws_url and not host:
        ws_url = _resolve_tunnel_url()

    if not host and not ws_url:
        parser.error('No server, no tunnel URL, and Drive lookup failed. '
                     'Use --server HOST:PORT or --tunnel-url URL')

    if not args.name:
        parser.error('--name is required (e.g. --name garage-radio)')

    # Graceful shutdown — defined early so plugin retry loop can use it
    shutdown_requested = threading.Event()

    def handle_signal(signum, frame):
        if not shutdown_requested.is_set():
            print(f"\n[Endpoint] Signal {signum} received, shutting down...")
            shutdown_requested.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Load and set up plugin
    print(f"[Endpoint] Loading plugin: {args.plugin}")
    plugin = load_plugin(args.plugin)

    plugin_config = {
        'device': args.device,
        'rate': args.rate,
        'gain': args.gain,
    }

    # Try plugin setup — if it fails, retry in background thread
    _plugin_ready = threading.Event()
    try:
        plugin.setup(plugin_config)
        _plugin_ready.set()
        print(f"[Endpoint] Plugin '{plugin.name}' ready "
              f"(device={args.device or 'default'}, rate={args.rate}, gain={args.gain})")
    except Exception as e:
        print(f"[Endpoint] Plugin setup failed: {e} — will retry in background")

        def _plugin_retry():
            while not shutdown_requested.is_set():
                shutdown_requested.wait(10)
                if shutdown_requested.is_set():
                    break
                try:
                    plugin.setup(plugin_config)
                    _plugin_ready.set()
                    print(f"[Endpoint] Plugin '{plugin.name}' ready (delayed)")
                    return
                except Exception as _e:
                    print(f"[Endpoint] Plugin retry failed: {_e}")

        threading.Thread(target=_plugin_retry, daemon=True, name="plugin-retry").start()

    # Audio callbacks
    def on_audio_from_master(pcm):
        """Called from reader thread when master sends audio."""
        plugin.put_audio(pcm)

    def on_command_from_master(cmd):
        """Called from reader thread when master sends a command."""
        cmd_str = cmd.get('cmd', str(cmd)) if isinstance(cmd, dict) else str(cmd)

        # Restart command — ACK then os.execv
        if cmd_str == 'restart':
            print(f"[Endpoint] Restart requested by gateway")
            try:
                client.send_ack('restart', {"ok": True})
            except Exception:
                pass
            time.sleep(0.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        result = plugin.execute(cmd)
        ok = result.get('ok', False) if isinstance(result, dict) else False
        print(f"[Endpoint] Command: {cmd_str} -> {'OK' if ok else result}")
        # Send ACK back to master with the result
        try:
            client.send_ack(cmd_str, result if isinstance(result, dict) else {"ok": False})
        except Exception as e:
            print(f"[Endpoint] ACK send error: {e}")

    def on_status_from_master(status):
        """Called from reader thread when master sends status/heartbeat."""
        stype = status.get('type', '') if isinstance(status, dict) else ''
        if stype == 'heartbeat':
            pass  # Silent -- heartbeats are normal
        else:
            print(f"[Endpoint] Status from master: {status}")

    # Update check on reconnect — runs in background so it doesn't block
    _last_update_check = [0.0]
    _UPDATE_CHECK_INTERVAL = 300  # check at most every 5 minutes

    def on_connect_from_master():
        """Called on each (re)connect. Check for code updates."""
        now = time.time()
        if now - _last_update_check[0] < _UPDATE_CHECK_INTERVAL:
            return
        _last_update_check[0] = now
        _url = f'http://{host}:8080' if host else None
        if _url:
            try:
                if _check_for_update(_url):
                    print("[Update] New code installed, restarting...")
                    # Remove --no-update from argv if present for clean restart
                    _argv = [a for a in sys.argv if a != '--no-update']
                    os.execv(sys.executable, [sys.executable] + _argv)
            except Exception as e:
                print(f"[Update] Reconnect update check failed: {e}")

    # Create client
    client = GatewayLinkClient(
        host, port, args.name,
        capabilities=plugin.capabilities,
        plugin_name=plugin.name,
        on_audio=on_audio_from_master,
        on_command=on_command_from_master,
        on_status=on_status_from_master,
        on_connect=on_connect_from_master,
        ws_url=ws_url,
        url_resolver=_resolve_tunnel_url,
    )

    # Periodic update checker — runs every hour regardless of connection state
    def _periodic_update_checker():
        time.sleep(3600)  # first check after 1 hour
        while True:
            _url = f'http://{host}:8080' if host else None
            if _url:
                try:
                    if _check_for_update(_url):
                        print("[Update] Periodic check: new code installed, restarting...")
                        _argv = [a for a in sys.argv if a != '--no-update']
                        os.execv(sys.executable, [sys.executable] + _argv)
                except Exception as e:
                    print(f"[Update] Periodic check failed: {e}")
            time.sleep(3600)

    threading.Thread(target=_periodic_update_checker, daemon=True,
                     name="update-check").start()

    # Start client (connects in background, auto-reconnect)
    client.start()

    # Start status reporter
    # Use plugin's preferred interval if it declares one, otherwise CLI arg
    _interval = getattr(plugin, 'status_interval', None) or args.status_interval
    reporter = StatusReporter(client, plugin, interval=_interval)
    reporter.start()

    # Audio capture loop (main thread)
    _target = f"{host}:{port}" if host else "(tunnel)"
    if ws_url:
        _target += f" fallback: WS tunnel"
    print(f"[Endpoint] Streaming to {_target} as '{args.name}' -- Ctrl+C to stop")
    use_gain = args.gain != 1.0

    # Diagnostic counters for audio loop
    _diag_time = time.monotonic()
    _diag_reads = 0
    _diag_nulls = 0
    _diag_sends = 0
    _diag_slow_reads = 0
    _diag_slow_sends = 0
    _diag_max_read = 0.0
    _diag_max_send = 0.0
    _DIAG_INTERVAL = 10.0

    try:
        while not shutdown_requested.is_set():
            if not _plugin_ready.is_set():
                time.sleep(0.5)
                continue
            _t0 = time.monotonic()
            pcm, _ptt = plugin.get_audio()
            _read_ms = (time.monotonic() - _t0) * 1000

            if pcm and client.connected:
                if use_gain:
                    pcm = apply_gain(pcm, args.gain)
                _t1 = time.monotonic()
                client.send_audio(pcm)
                _send_ms = (time.monotonic() - _t1) * 1000
                _diag_sends += 1
                if _send_ms > 10:
                    _diag_slow_sends += 1
                if _send_ms > _diag_max_send:
                    _diag_max_send = _send_ms
            elif not pcm:
                _diag_nulls += 1
                time.sleep(0.01)

            _diag_reads += 1
            if _read_ms > 80:
                _diag_slow_reads += 1
            if _read_ms > _diag_max_read:
                _diag_max_read = _read_ms

            _now = time.monotonic()
            if _now - _diag_time >= _DIAG_INTERVAL:
                print(f"[Endpoint-DIAG] {_DIAG_INTERVAL:.0f}s: "
                      f"reads={_diag_reads} nulls={_diag_nulls} sends={_diag_sends} "
                      f"slow_read={_diag_slow_reads} (max={_diag_max_read:.0f}ms) "
                      f"slow_send={_diag_slow_sends} (max={_diag_max_send:.0f}ms)")
                _diag_time = _now
                _diag_reads = _diag_nulls = _diag_sends = 0
                _diag_slow_reads = _diag_slow_sends = 0
                _diag_max_read = _diag_max_send = 0.0

    except Exception as e:
        print(f"[Endpoint] Audio loop error: {e}")
    finally:
        print("[Endpoint] Stopping...")
        reporter.stop()
        client.stop()
        plugin.teardown()
        print("[Endpoint] Stopped")


if __name__ == '__main__':
    main()
