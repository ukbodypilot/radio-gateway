"""Shared plumbing for the offload workers (tools/tts_worker.py,
tools/denoise_worker.py): gateway self-registration heartbeat, auth,
CLI args, host health readings.

Same pattern as tools/transcribe_worker.py (which keeps its own copy so it
stays a single-file deploy): the worker POSTs {"kind", "name", "port", ...}
to the gateway's /worker/register every ~30 s, forever. The gateway takes
the address from the socket peer and expires a worker after 90 s of
silence, so no worker address is configured anywhere.
"""

import argparse
import base64
import glob
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

REGISTER_PATH = '/worker/register'


def add_gateway_args(parser: argparse.ArgumentParser, port: int):
    parser.add_argument('--port', type=int, default=port,
                        help=f'Port to listen on (default {port})')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address (default 0.0.0.0)')
    parser.add_argument('--gateway', default=os.environ.get('GATEWAY_URL', ''),
                        help='Gateway base URL, e.g. http://192.168.2.140:8080. Env: GATEWAY_URL')
    parser.add_argument('--name', default='', help="Name shown on the gateway (default: hostname)")
    parser.add_argument('--register-interval', type=float, default=30.0,
                        help='Seconds between heartbeats (default 30; clamped to gateway TTL/3)')
    parser.add_argument('--gateway-username', default=os.environ.get('GATEWAY_USERNAME', 'user'),
                        help="The gateway's WEB_CONFIG_USERNAME. Env: GATEWAY_USERNAME")
    parser.add_argument('--gateway-password', default=os.environ.get('GATEWAY_PASSWORD', 'user'),
                        help="The gateway's WEB_CONFIG_PASSWORD. Env: GATEWAY_PASSWORD")
    parser.add_argument('--nice', type=int, default=None,
                        help='Raise niceness by this much at start (default per worker)')


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def cpu_temp_c():
    temps = [v / 1000.0 for v in (_read_int(f) for f in
             glob.glob('/sys/class/thermal/thermal_zone*/temp')) if v and v > 0]
    return round(max(temps), 1) if temps else None


def fan_rpm():
    for f in sorted(glob.glob('/sys/devices/platform/applesmc.*/fan*_input')):
        v = _read_int(f)
        if v is not None:
            return v
    return None


def register_once(gateway_url, payload, auth_header=None, timeout=5):
    headers = {'Content-Type': 'application/json'}
    if auth_header:
        headers['Authorization'] = auth_header
    req = urllib.request.Request(gateway_url.rstrip('/') + REGISTER_PATH,
                                 data=json.dumps(payload).encode(),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Reachable, but said no: 404 = gateway too old for /worker/register,
        # 401 = wrong --gateway-username/--gateway-password.
        return {'ok': False, 'error': f'HTTP {e.code} {e.reason}'}
    except Exception as e:
        return {'ok': False, 'error': f'gateway unreachable ({e})'}


def register_loop(gateway_url, payload_fn, interval, auth_header=None, tag='worker'):
    """Heartbeat forever; log only state changes (plus a reminder every
    10 min while failing, so a broken registration never looks healthy)."""
    last_ok = None
    last_err = 0.0
    while True:
        try:
            payload = payload_fn()
        except Exception as e:
            payload = {}
            print(f'[{tag}] payload error: {e}', flush=True)
        resp = register_once(gateway_url, payload, auth_header)
        ok = bool(resp and resp.get('ok'))
        why = (resp or {}).get('error', 'gateway unreachable')
        if ok != last_ok:
            if ok:
                print(f'[{tag}] Registered with {gateway_url} as {payload.get("name")} '
                      f'(url={resp.get("url", "?")}, ttl={resp.get("ttl", "?")}s)', flush=True)
            else:
                print(f'[{tag}] Registration with {gateway_url} failed: {why}', flush=True)
                last_err = time.time()
            last_ok = ok
        elif not ok and time.time() - last_err > 600:
            print(f'[{tag}] Still not registered with {gateway_url}: {why}', flush=True)
            last_err = time.time()
        ttl = (resp or {}).get('ttl')
        sleep = interval
        if isinstance(ttl, (int, float)) and ttl > 0:
            sleep = max(5.0, min(interval, ttl / 3.0))
        time.sleep(sleep)


def start_registration(args, kind, extra_fn=None, tag='worker'):
    """Start the heartbeat thread if --gateway is set. extra_fn() -> dict
    merged into each heartbeat (live counters, model state)."""
    if not args.gateway:
        print(f'[{tag}] No --gateway / GATEWAY_URL: not registering', flush=True)
        return
    name = args.name or socket.gethostname()
    auth = None
    if args.gateway_password:
        auth = 'Basic ' + base64.b64encode(
            f'{args.gateway_username}:{args.gateway_password}'.encode()).decode()

    def payload():
        p = {'kind': kind, 'name': name, 'port': args.port,
             'cpus': os.cpu_count(), 'cpu_temp_c': cpu_temp_c()}
        if extra_fn:
            p.update(extra_fn())
        return p

    threading.Thread(target=register_loop,
                     args=(args.gateway, payload, args.register_interval, auth, tag),
                     daemon=True, name='GatewayRegister').start()
    print(f'[{tag}] Registering with gateway {args.gateway} as {name}', flush=True)


def nice_by(n):
    if not n:
        return
    try:
        os.nice(n)
    except Exception as e:
        print(f'nice({n}) failed: {e}', flush=True)
