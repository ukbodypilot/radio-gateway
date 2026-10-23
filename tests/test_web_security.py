#!/usr/bin/env python3
"""Web UI and link security (review findings H1, H3, H4, H5, M3).

Before:
  * the web UI took POSTs and WebSocket upgrades from any web page, so a
    page you visited could drive /manager/run (claude with every permission),
    /reboothost or /ws_mic, password or not;
  * with no password, the Cloudflare tunnel put all of that on the internet;
  * any host could REGISTER on :9700 as any endpoint and evict the real one;
  * endpoint updates were unsigned and wrote whatever file names the bundle
    named, "../" included;
  * a web password locked endpoints out of /ws/link;
  * /config showed four passwords in clear.

Real HTTP and link sockets on localhost; nothing outside the repo is touched.
"""

import base64
import http.client
import importlib.util
import json
import os
import shutil
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import _hwstubs  # noqa: E402,F401
import _tmpdirs  # noqa: E402
import config_schema  # noqa: E402
from gateway_link import (  # noqa: E402
    GatewayLinkClient, GatewayLinkServer, bundle_signature, verify_bundle,
)
from web.handler import GatewayRequestHandler, ThreadedServer  # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── /config masking ──────────────────────────────────────────────

def secrets_masked():
    print("\n1. every password-like key is masked on /config")
    keys = config_schema.secret_keys()
    for k in ('MUMBLE_PASSWORD', 'MUMBLE_SERVER_1_PASSWORD', 'MUMBLE_SERVER_2_PASSWORD',
              'CAT_PASSWORD', 'PACKET_APRS_IS_PASSCODE', 'LINK_SECRET',
              'WEB_CONFIG_PASSWORD', 'STREAM_PASSWORD'):
        check(f"{k} is secret", k in keys)
    check("bool WEB_TUNNEL_ALLOW_NO_PASSWORD is not", 'WEB_TUNNEL_ALLOW_NO_PASSWORD' not in keys)

    import web_server
    srv = web_server.WebConfigServer.__new__(web_server.WebConfigServer)
    html = srv._render_field('MUMBLE_PASSWORD', 'hunter2', '')
    check("Mumble password not in the rendered field", 'hunter2' not in html, html)


# ── Web access control ───────────────────────────────────────────

class FakeTunnel:
    def __init__(self, url):
        self.url = url

    def get_url(self):
        return self.url


def start_web(password='', link_secret='', tunnel_allow_open=False, tunnel_url=None,
              link_port=9700):
    gw = types.SimpleNamespace(cloudflare_tunnel=FakeTunnel(tunnel_url) if tunnel_url else None,
                               _plugin_web_routes={},
                               config=types.SimpleNamespace(LINK_PORT=link_port))
    parent = types.SimpleNamespace(gateway=gw, config=types.SimpleNamespace(LINK_SECRET=link_secret))
    Handler = type('H', (GatewayRequestHandler,), {
        'parent': parent, 'password': password, 'link_secret': link_secret,
        'tunnel_allow_open': tunnel_allow_open,
    })
    srv = ThreadedServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def req(srv, method, path, headers=None, body=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def basic(pw):
    return {'Authorization': 'Basic ' + base64.b64encode(f'user:{pw}'.encode()).decode()}


def web_access():
    print("\n2. cross-site requests (CSRF)")
    srv = start_web()
    host = f"127.0.0.1:{srv.server_address[1]}"
    # 404 = got past access control to "no such route".
    st, _, _ = req(srv, 'POST', '/nope', body=b'{}')
    check("POST with no Origin (curl, MCP) passes", st == 404, st)
    st, _, _ = req(srv, 'POST', '/nope', {'Origin': f'http://{host}'}, b'{}')
    check("same-origin POST passes", st == 404, st)
    st, _, _ = req(srv, 'POST', '/manager/run', {'Origin': 'https://evil.example'}, b'{}')
    check("cross-site POST to /manager/run refused", st == 403, st)
    st, _, _ = req(srv, 'POST', '/nope', {'Origin': 'null'}, b'{}')
    check("Origin: null refused", st == 403, st)
    st, _, _ = req(srv, 'GET', '/ws_mic', {'Origin': 'https://evil.example',
                                          'Upgrade': 'websocket', 'Connection': 'Upgrade'})
    check("cross-site WebSocket upgrade refused", st == 403, st)
    st, _, _ = req(srv, 'GET', '/nope', {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors'})
    check("cross-site sub-resource GET refused", st == 403, st)
    st, _, _ = req(srv, 'GET', '/dashboard', {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
    check("link from another site to a page still opens", st == 200, st)
    st, _, _ = req(srv, 'GET', '/nope', {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
    check("...but not to an API route", st == 403, st)
    st, _, _ = req(srv, 'GET', '/dashboard', {'Sec-Fetch-Site': 'same-origin'})
    check("same-origin GET passes", st == 200, st)
    srv.shutdown()

    # tunnel_allow_open so the no-password tunnel rule (section 3) is out of
    # the way and this checks only the Origin logic.
    srv = start_web(tunnel_allow_open=True, tunnel_url='https://abc-def.trycloudflare.com')
    st, _, _ = req(srv, 'POST', '/nope', {'Origin': 'https://abc-def.trycloudflare.com',
                                         'Cf-Ray': 'x'}, b'{}')
    check("tunnel hostname counts as same origin even if Host was rewritten", st == 404, st)
    srv.shutdown()

    print("\n3. no password: LAN open, tunnel closed except endpoints")
    srv = start_web()
    st, _, _ = req(srv, 'GET', '/dashboard')
    check("LAN GET /dashboard works with no password", st == 200, st)
    tun = {'Cf-Connecting-Ip': '203.0.113.9', 'Cf-Ray': 'abc'}
    st, body, _ = req(srv, 'GET', '/dashboard', tun)
    check("tunnel GET /dashboard is 403", st == 403, st)
    check("403 says what to do", b'WEB_CONFIG_PASSWORD' in body, body)
    st, _, _ = req(srv, 'GET', '/api/endpoint/version', tun)
    check("tunnel endpoint update check still works", st == 200, st)
    st, _, _ = req(srv, 'GET', '/ws/link', tun)
    check("tunnel /ws/link not blocked (endpoints keep working)", st not in (401, 403), st)
    srv.shutdown()
    srv = start_web(tunnel_allow_open=True)
    st, _, _ = req(srv, 'GET', '/dashboard', tun)
    check("WEB_TUNNEL_ALLOW_NO_PASSWORD = true restores the old behaviour", st == 200, st)
    srv.shutdown()

    print("\n4. password set")
    srv = start_web(password='pw')
    st, _, _ = req(srv, 'GET', '/dashboard')
    check("no credentials -> 401", st == 401, st)
    st, _, _ = req(srv, 'GET', '/dashboard', basic('pw'))
    check("right credentials -> 200", st == 200, st)
    st, _, _ = req(srv, 'GET', '/dashboard', basic('nope'))
    check("wrong credentials -> 401", st == 401, st)
    st, _, _ = req(srv, 'POST', '/reboothost', {**basic('pw'), 'Origin': 'https://evil.example'}, b'{}')
    check("credentials don't help a cross-site POST", st == 403, st)
    st, _, _ = req(srv, 'GET', '/api/endpoint/files')
    check("endpoint updates need no web password", st == 200, st)
    st, _, _ = req(srv, 'GET', '/metrics')
    check("local Prometheus scrape of /metrics needs no login", st not in (401, 403), st)
    st, _, _ = req(srv, 'GET', '/metrics', {'Cf-Ray': 'x'})
    check("...but /metrics through the tunnel does", st == 401, st)
    st, _, _ = req(srv, 'GET', '/ws/link')
    check("/ws/link without LINK_SECRET still needs the password", st == 401, st)
    srv.shutdown()
    srv = start_web(password='pw', link_secret='s')
    st, _, _ = req(srv, 'GET', '/ws/link')
    check("/ws/link with LINK_SECRET skips Basic auth (H5)", st != 401, st)
    srv.shutdown()


# ── Link REGISTER auth ───────────────────────────────────────────

def link_auth():
    print("\n5. link REGISTER needs LINK_SECRET when the gateway has one (H3)")
    port = free_port()
    registered = []
    srv = GatewayLinkServer(port=port, secret='s3cret', bind='127.0.0.1',
                            on_register=lambda info: registered.append(info['name']))
    srv.start()
    good = GatewayLinkClient('127.0.0.1', port, 'kv4p-v', {}, secret='s3cret')
    good.start()
    check("endpoint with the secret registers",
          wait_for(lambda: 'kv4p-v' in srv.get_endpoint_names()), srv.get_endpoint_names())
    real_sock = srv._endpoints['kv4p-v'].sock

    for label, secret in (("no secret (old endpoint)", ''), ("wrong secret", 'guess')):
        bad = GatewayLinkClient('127.0.0.1', port, 'kv4p-v', {}, secret=secret)
        bad.start()
        time.sleep(1.5 if not secret else 1.0)
        bad.stop()
        check(f"impostor with {label} did not evict the real endpoint",
              srv._endpoints.get('kv4p-v') is not None
              and srv._endpoints['kv4p-v'].sock is real_sock)
    check("on_register ran once", registered == ['kv4p-v'], registered)
    good.stop()
    srv.stop()

    print("\n5b. the same through /ws/link (tunnel path), with a web password set")
    port = free_port()
    srv = GatewayLinkServer(port=port, secret='s3cret', bind='127.0.0.1')
    srv.start()
    web = start_web(password='pw', link_secret='s3cret', link_port=port)
    ws_url = f"ws://127.0.0.1:{web.server_address[1]}/ws/link"
    c = GatewayLinkClient(None, None, 'ic7100', {}, ws_url=ws_url, secret='s3cret')
    c.start()
    check("endpoint registers over the WebSocket bridge, no web password needed",
          wait_for(lambda: 'ic7100' in srv.get_endpoint_names()), srv.get_endpoint_names())
    c.stop()
    bad = GatewayLinkClient(None, None, 'ic7100-x', {}, ws_url=ws_url, secret='guess')
    bad.start()
    time.sleep(1.5)
    check("wrong secret over the bridge is refused",
          'ic7100-x' not in srv.get_endpoint_names())
    bad.stop()
    web.shutdown()
    srv.stop()

    print("\n6. gateway without LINK_SECRET still takes everyone")
    port = free_port()
    srv = GatewayLinkServer(port=port)
    srv.start()
    old = GatewayLinkClient('127.0.0.1', port, 'old', {})
    new = GatewayLinkClient('127.0.0.1', port, 'new', {}, secret='s3cret')
    old.start()
    new.start()
    check("endpoint without a secret registers",
          wait_for(lambda: 'old' in srv.get_endpoint_names()))
    check("endpoint with a secret registers (after a short challenge wait)",
          wait_for(lambda: 'new' in srv.get_endpoint_names()))
    old.stop()
    new.stop()
    srv.stop()


# ── Signed updates ───────────────────────────────────────────────

class BundleHandler(BaseHTTPRequestHandler):
    files = {}
    sig = ''
    extra = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/api/endpoint/version':
            body = json.dumps({'version': 'remote-differs'}).encode()
        else:
            d = {k: base64.b64encode(v).decode() for k, v in self.files.items()}
            d.update(self.extra)
            body = json.dumps(d).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        if self.path == '/api/endpoint/files' and self.sig:
            self.send_header('X-Bundle-Signature', self.sig)
        self.end_headers()
        self.wfile.write(body)


def signed_updates():
    print("\n7. update bundle signature (H4)")
    files = {'gateway_link.py': b'a', 'link_endpoint.py': b'b'}
    sig = bundle_signature('k', files)
    check("good signature verifies", verify_bundle('k', files, sig))
    check("changed file fails", not verify_bundle('k', {**files, 'gateway_link.py': b'x'}, sig))
    check("wrong key fails", not verify_bundle('j', files, sig))
    check("missing signature fails", not verify_bundle('k', files, ''))

    print("\n8. the endpoint updater")
    tmp = _tmpdirs.mkdtemp('rgw_sec_')
    # Not named "run": the updater treats <parent>/run as a read-only-root
    # Pi and remounts / to persist, which a test must never do.
    run = os.path.join(tmp, 'ep')
    os.makedirs(run)
    shutil.copy(os.path.join(REPO, 'tools', 'link_endpoint.py'), run)
    spec = importlib.util.spec_from_file_location('le_under_test', os.path.join(run, 'link_endpoint.py'))
    le = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(le)

    srv = ThreadingHTTPServer(('127.0.0.1', 0), BundleHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    new = {'log_shipper.py': b'# new\n'}

    os.environ.pop('RADIO_GATEWAY_LINK_SECRET', None)
    BundleHandler.files, BundleHandler.sig = new, ''
    BundleHandler.extra = {'../escaped.py': base64.b64encode(b'pwned').decode()}
    check("no secret: unsigned bundle installs", le._check_for_update_locked(url) is True)
    check("...but a ../ entry is not written",
          not os.path.exists(os.path.join(tmp, 'escaped.py')))

    with open(os.path.join(tmp, 'link_secret'), 'w') as f:
        f.write('k\n')   # parent dir, as on a read-only-root Pi
    BundleHandler.files = {'log_shipper.py': b'# newer\n'}
    BundleHandler.extra = {}
    BundleHandler.sig = ''
    check("with a secret: unsigned bundle refused", le._check_for_update_locked(url) is False)
    BundleHandler.sig = bundle_signature('wrong', BundleHandler.files)
    check("with a secret: wrongly signed bundle refused", le._check_for_update_locked(url) is False)
    with open(os.path.join(run, 'log_shipper.py'), 'rb') as f:
        check("refused bundle left the file alone", f.read() == b'# new\n')
    BundleHandler.sig = bundle_signature('k', BundleHandler.files)
    check("with a secret: correctly signed bundle installs", le._check_for_update_locked(url) is True)
    srv.shutdown()

    print("\n9. the gateway signs what it serves")
    web = start_web(link_secret='k')
    st, body, r = req(web, 'GET', '/api/endpoint/files')
    got = {k: base64.b64decode(v) for k, v in json.loads(body).items()}
    check("served bundle carries a valid signature",
          st == 200 and verify_bundle('k', got, r.getheader('X-Bundle-Signature', '')))
    web.shutdown()
    web = start_web()
    st, body, r = req(web, 'GET', '/api/endpoint/files')
    check("no LINK_SECRET: no signature header", r.getheader('X-Bundle-Signature') is None)
    web.shutdown()


def main():
    secrets_masked()
    web_access()
    link_auth()
    signed_updates()
    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
