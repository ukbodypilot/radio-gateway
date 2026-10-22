#!/usr/bin/env python3
"""
Mumble to Radio Gateway via AIOC
Reads configuration from gateway_config.txt
Optimized for low latency and high quality audio
"""

from log_util import log_exc
import sys
import os

def _get_version():
    """Build version from git: tag-based (e.g. 1.0.0, 1.0.0-3-g87ba23a) or commit hash."""
    try:
        import subprocess
        v = subprocess.check_output(
            ['git', 'describe', '--tags', '--always'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True).strip()
        return v.lstrip('v')
    except Exception:
        return "unknown"

__version__ = _get_version()
import time
import signal
import subprocess
import socket

# Hardware/Mumble libraries are imported optionally; main() refuses to
# start without them (hw_deps.require). The SSL shims pymumble needs are
# applied here, before anything opens a Mumble connection.
import hw_deps
hw_deps.patch_ssl()

from config_format import parse_config_value
import config_schema


class Config:
    """Configuration loaded from gateway_config.txt"""
    def __init__(self, config_file="gateway_config.txt"):
        self.config_file = config_file
        self.load_config()
    
    def load_config(self):
        """Load configuration from file.

        Defaults and types come from config_schema. Keys inside a dotted
        [plugin.instance] section (e.g. [kv4p.vhf]) belong to that plugin and
        are read by endpoints_state.read_sections; they used to leak in here
        as lowercase attributes (config.port, config.enable).
        """
        # Store defaults for type inference (used by WebConfigServer)
        self._defaults = dict(config_schema.DEFAULTS)

        # Set defaults
        for key, value in self._defaults.items():
            setattr(self, key, value)

        # Try to load from file
        if not os.path.exists(self.config_file):
            print(f"WARNING: Config file '{self.config_file}' not found, using defaults")
            return

        try:
            in_plugin_section = False
            with open(self.config_file, 'r') as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('['):
                        in_plugin_section = '.' in line.split(']', 1)[0]
                        continue
                    if in_plugin_section or '=' not in line:
                        continue

                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = parse_config_value(value)
                    # Skip if value is empty after stripping comments
                    if not value:
                        continue
                    try:
                        setattr(self, key, config_schema.coerce(key, value))
                    except ValueError:
                        # One bad value used to abort the whole load, leaving
                        # every later key at its default without saying so.
                        print(f"WARNING: {self.config_file}:{lineno}: {key} = {value!r} "
                              f"is not a valid {type(self._defaults[key]).__name__}; "
                              f"keeping {self._defaults[key]!r}")

            print(f"✓ Configuration loaded from '{self.config_file}'")

        except Exception as e:
            print(f"WARNING: Error loading config file: {e}")
            print("Using default values")

        # Sync ENABLE_TH9800 / ENABLE_CAT_CONTROL aliases (either one enables TH-9800)
        if getattr(self, 'ENABLE_TH9800', False) and not getattr(self, 'ENABLE_CAT_CONTROL', False):
            self.ENABLE_CAT_CONTROL = True
        elif getattr(self, 'ENABLE_CAT_CONTROL', False) and not getattr(self, 'ENABLE_TH9800', False):
            self.ENABLE_TH9800 = True

from gateway_core import RadioGateway

_GATEWAY_LOCK = '/tmp/gateway.lock'
_STARTUP_LOG = '/tmp/gateway_startup.log'


def _stale_gateway_pids(script_path):
    """PIDs (other than ours) of python processes running *script_path*."""
    me = os.getpid()
    script_path = os.path.realpath(script_path)
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f'/proc/{entry}/cmdline', 'rb') as f:
                argv = [a.decode('utf-8', 'replace') for a in f.read().split(b'\0') if a]
            cwd = os.readlink(f'/proc/{entry}/cwd')
        except OSError:
            continue
        if len(argv) < 2 or not os.path.basename(argv[0]).startswith('python'):
            continue
        # First non-option argument is the script python was asked to run.
        script = next((a for a in argv[1:] if not a.startswith('-')), None)
        if script and os.path.realpath(os.path.join(cwd, script)) == script_path:
            pids.append(int(entry))
    return pids


def _pre_flight(config):
    """Pre-flight checks absorbed from start.sh.

    Runs before RadioGateway init:
    1. Kill stale processes from prior runs
    2. Start TH-9800 CAT systemd service (if enabled)
    3. Set CPU governor to schedutil
    4. Reset AIOC USB device
    """
    import glob

    script_dir = os.path.dirname(os.path.abspath(__file__))

    def _run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, **kw)

    def _sudo(cmd):
        try:
            return _run(['sudo', '-n'] + cmd)
        except Exception:
            return None

    print("=" * 42)
    print(f"[{time.strftime('%H:%M:%S')}] Starting Radio Gateway")
    print("=" * 42)

    # 1. Kill stale processes
    print(f"[{time.strftime('%H:%M:%S')}] [1/4] Checking for stale processes...")
    # Kill stale gateway (but not us)
    # Match only python processes running THIS script. `pgrep -f
    # radio_gateway.py` also matched editors, tail/less, a grep, a test
    # run from another checkout and a sudo wrapper around this process,
    # and SIGKILLed all of them.
    try:
        for pid in _stale_gateway_pids(os.path.abspath(__file__)):
            try:
                os.kill(pid, 9)
                print(f"  Killed stale gateway PID {pid}")
            except OSError:
                pass
    except Exception:
        log_exc()
    # Stop leftover mumble-server instances
    for svc in ['mumble-server-gw1', 'mumble-server-gw2']:
        try:
            result = _run(['systemctl', 'is-active', '--quiet', f'{svc}.service'])
            if result.returncode == 0:
                _sudo(['systemctl', 'stop', f'{svc}.service'])
                print(f"  Stopped {svc}")
        except Exception:
            log_exc()

    # 2. Start TH-9800 CAT service
    print(f"[{time.strftime('%H:%M:%S')}] [2/4] Checking TH-9800 CAT control...")
    if getattr(config, 'ENABLE_TH9800', False) or getattr(config, 'ENABLE_CAT_CONTROL', False):
        try:
            result = _run(['systemctl', 'is-active', '--quiet', 'th9800-cat.service'])
            if result.returncode == 0:
                _sudo(['systemctl', 'restart', 'th9800-cat.service'])
            else:
                _sudo(['systemctl', 'start', 'th9800-cat.service'])
            # Wait for TCP port
            cat_port = int(getattr(config, 'CAT_PORT', 9800))
            for _ in range(20):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect(('127.0.0.1', cat_port))
                    s.close()
                    print(f"  TH-9800 CAT service ready (port {cat_port})")
                    break
                except (ConnectionRefusedError, OSError):
                    time.sleep(0.5)
            else:
                print(f"  TH-9800 CAT service failed to start")
        except Exception as e:
            print(f"  TH-9800 CAT error: {e}")
    else:
        print("  Disabled in config")

    # 3. CPU governor
    print(f"[{time.strftime('%H:%M:%S')}] [3/4] Setting CPU governor to schedutil...")
    for gov_path in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'):
        try:
            subprocess.run(['sudo', '-n', 'tee', gov_path], input='schedutil',
                           text=True, capture_output=True, timeout=5)
        except Exception:
            log_exc()
    print("  CPU governor set (or unsupported)")

    # 4. AIOC USB reset
    print(f"[{time.strftime('%H:%M:%S')}] [4/4] Resetting AIOC USB device...")
    aioc_path = None
    for product_file in glob.glob('/sys/bus/usb/devices/*/product'):
        try:
            with open(product_file) as f:
                if 'all-in-one' in f.read().lower():
                    aioc_path = os.path.dirname(product_file)
                    break
        except Exception:
            log_exc()
    if aioc_path:
        auth_file = os.path.join(aioc_path, 'authorized')
        if os.path.exists(auth_file):
            print(f"  Found AIOC at {aioc_path}")
            try:
                subprocess.run(
                    f'echo 0 | sudo -n tee {auth_file} > /dev/null && sleep 1 && echo 1 | sudo -n tee {auth_file} > /dev/null',
                    shell=True, timeout=10)
                time.sleep(2)  # USB re-enumeration
                print("  AIOC USB reset complete")
            except Exception as e:
                print(f"  AIOC USB reset failed: {e}")
        else:
            print("  AIOC authorized file not found")
    else:
        print("  AIOC USB device not found (skipping reset)")

    print(f"[{time.strftime('%H:%M:%S')}] Pre-flight complete")


def _cleanup_services(config):
    """Post-shutdown cleanup — stop services started by pre-flight."""
    try:
        if getattr(config, 'ENABLE_TH9800', False) or getattr(config, 'ENABLE_CAT_CONTROL', False):
            subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'th9800-cat.service'],
                           capture_output=True, timeout=10)
    except Exception:
        log_exc()
    # Kill any rtl_airband processes (SDR plugin children)
    try:
        subprocess.run(['sudo', '-n', 'killall', '-9', 'rtl_airband'],
                       capture_output=True, timeout=5)
    except Exception:
        log_exc()

def _acquire_lock():
    """Write PID lockfile. Exit if another instance is already running."""
    if os.path.exists(_GATEWAY_LOCK):
        try:
            old_pid = int(open(_GATEWAY_LOCK).read().strip())
            os.kill(old_pid, 0)  # signal 0: just checks process exists
            print(f"ERROR: Gateway already running (PID {old_pid}).")
            print("       Stop the existing instance first.")
            sys.exit(1)
        except (ValueError, OSError):
            pass  # stale lock — overwrite
    try:
        with open(_GATEWAY_LOCK, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Warning: Could not write lockfile {_GATEWAY_LOCK}: {e}")

def _release_lock():
    """Remove lockfile if it belongs to this process."""
    try:
        if open(_GATEWAY_LOCK).read().strip() == str(os.getpid()):
            os.unlink(_GATEWAY_LOCK)
    except Exception:
        log_exc()

def main():
    hw_deps.require()
    _acquire_lock()

    # Find config file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(script_dir, "gateway_config.txt")

    # Load configuration
    config = Config(config_file)

    # Pre-flight: kill stale procs, start CAT, set governor, reset AIOC
    _pre_flight(config)

    # Create and run gateway
    gateway = RadioGateway(config)

    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        gateway.running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    gateway.run()

    # Post-shutdown cleanup
    _cleanup_services(config)
    _release_lock()

    if gateway.restart_requested:
        print("\nRestarting gateway...")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

if __name__ == "__main__":
    main()

