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


def load_plugin(name):
    """Load a plugin by name. Built-in: 'audio'. Future: 'kv4p', 'd75', etc."""
    cls = _PLUGINS.get(name)
    if not cls:
        available = ', '.join(sorted(_PLUGINS.keys()))
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
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            if self._client.connected:
                try:
                    status = self._plugin.get_status()
                    status['uptime'] = round(time.monotonic() - self._start_time, 1)
                    self._client.send_status(status)
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
    parser.add_argument('--list-devices', action='store_true',
                        help='List available audio devices and exit')

    args = parser.parse_args()

    # --list-devices mode
    if args.list_devices:
        list_audio_devices()
        sys.exit(0)

    # Resolve server address — auto-discover via mDNS if --server not given
    if args.server:
        host, port = parse_server(args.server)
    else:
        print("[Endpoint] No --server specified — discovering gateway via mDNS...")
        result = discover_gateway(timeout=10)
        if result:
            host, port = result
        else:
            parser.error('Gateway not found via mDNS. Use --server HOST:PORT')
    if not args.name:
        parser.error('--name is required (e.g. --name garage-radio)')

    # Load and set up plugin
    print(f"[Endpoint] Loading plugin: {args.plugin}")
    plugin = load_plugin(args.plugin)

    plugin_config = {
        'device': args.device,
        'rate': args.rate,
        'gain': args.gain,
    }

    try:
        plugin.setup(plugin_config)
    except Exception as e:
        print(f"[Endpoint] Plugin setup failed: {e}")
        print(f"[Endpoint] Check that the device '{args.device or 'default'}' "
              "is available and not in use.")
        sys.exit(1)

    print(f"[Endpoint] Plugin '{plugin.name}' ready "
          f"(device={args.device or 'default'}, rate={args.rate}, gain={args.gain})")

    # Audio callbacks
    def on_audio_from_master(pcm):
        """Called from reader thread when master sends audio."""
        plugin.put_audio(pcm)

    def on_command_from_master(cmd):
        """Called from reader thread when master sends a command."""
        result = plugin.execute(cmd)
        cmd_str = cmd.get('cmd', str(cmd)) if isinstance(cmd, dict) else str(cmd)
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

    # Create client
    client = GatewayLinkClient(
        host, port, args.name,
        capabilities=plugin.capabilities,
        plugin_name=plugin.name,
        on_audio=on_audio_from_master,
        on_command=on_command_from_master,
        on_status=on_status_from_master,
    )

    # Graceful shutdown
    shutdown_requested = threading.Event()

    def handle_signal(signum, frame):
        if not shutdown_requested.is_set():
            print(f"\n[Endpoint] Signal {signum} received, shutting down...")
            shutdown_requested.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start client (connects in background, auto-reconnect)
    client.start()

    # Start status reporter
    reporter = StatusReporter(client, plugin, interval=args.status_interval)
    reporter.start()

    # Audio capture loop (main thread)
    print(f"[Endpoint] Streaming to {host}:{port} as '{args.name}' -- Ctrl+C to stop")
    use_gain = args.gain != 1.0

    try:
        while not shutdown_requested.is_set():
            pcm, _ptt = plugin.get_audio()
            if pcm and client.connected:
                if use_gain:
                    pcm = apply_gain(pcm, args.gain)
                client.send_audio(pcm)
            elif not pcm:
                # No audio available -- brief sleep to avoid busy-wait
                time.sleep(0.01)
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
