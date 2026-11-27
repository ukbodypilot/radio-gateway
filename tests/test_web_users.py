"""Extra web logins (WEB_EXTRA_USERS): hashed only, checked beside the main login, cached.

Covers the hash, the cache, tools/web_user.py editing a config in place, the config loader
keeping a hash intact, and a real Basic-auth round trip through GatewayRequestHandler.
"""
import base64
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import _hwstubs  # noqa: E402,F401
import web_users  # noqa: E402
from web.handler import GatewayRequestHandler, ThreadedServer  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + str(detail) if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


# --- hash + parse
h = web_users.make_hash('correct horse', iterations=1000)
check('hash has no password in it', 'correct horse' not in h and h.startswith('pbkdf2_sha256$1000$'))
check('right password checks', web_users.check_hash('correct horse', h))
check('wrong password fails', not web_users.check_hash('correct horsf', h))
check('garbage hash fails safely', not web_users.check_hash('x', 'pbkdf2_sha256$zz'))
users = web_users.parse(f" alice:{h} , bob:plaintext, :nope, carol:{h}")
check('parse keeps hashed entries only', sorted(users) == ['alice', 'carol'], users)
check('format round-trips', web_users.parse(web_users.format_users(users)) == users)

# --- verify + cache
check('verify ok', web_users.verify(users, 'alice', 'correct horse') == 'alice')
check('verify wrong user', web_users.verify(users, 'dave', 'correct horse') is None)
check('verify wrong password', web_users.verify(users, 'alice', 'nope') is None)
real = web_users.check_hash
web_users.check_hash = lambda *a: (_ for _ in ()).throw(AssertionError('hashed again'))
try:
    check('second verify comes from the cache', web_users.verify(users, 'alice', 'correct horse') == 'alice')
finally:
    web_users.check_hash = real
check('cache never accepts a different password', web_users.verify(users, 'alice', 'Correct horse') is None)

# --- tools/web_user.py on a temp config
d = tempfile.mkdtemp()
cfg_path = os.path.join(d, 'gateway_config.txt')
with open(cfg_path, 'w') as f:
    f.write("# top comment\nWEB_CONFIG_PASSWORD = user\nOTHER = 1   # keep me\n")
os.chmod(cfg_path, 0o600)
env = dict(os.environ, GATEWAY_CONFIG=cfg_path)
tool = os.path.join(REPO, 'tools', 'web_user.py')


def run(*args, stdin=''):
    return subprocess.run([sys.executable, tool, *args], input=stdin, capture_output=True, text=True, env=env)


r = run('add', 'remote', stdin='s3cret-pass-1\n')
check('add works', r.returncode == 0 and 'Added remote' in r.stdout, r.stdout + r.stderr)
text = open(cfg_path).read()
check('password never written', 's3cret-pass-1' not in text)
check('other lines kept', text.startswith('# top comment\nWEB_CONFIG_PASSWORD = user\nOTHER = 1   # keep me\n'), text)
check('file mode kept', oct(os.stat(cfg_path).st_mode & 0o777) == '0o600')
r = run('add', 'second', stdin='another-pass\n')
check('second user added on the same line', text.count('WEB_EXTRA_USERS') == 1
      and open(cfg_path).read().count('WEB_EXTRA_USERS') == 1)
check('list', run('list').stdout.split() == ['remote', 'second'])
check('short password refused', run('add', 'x', stdin='short\n').returncode != 0)
check('bad name refused', run('add', 'a:b', stdin='long-enough\n').returncode != 0)
check('remove', run('remove', 'second').returncode == 0 and run('list').stdout.split() == ['remote'])

# --- the gateway's config loader keeps the hash intact
from radio_gateway import Config  # noqa: E402

c = Config(cfg_path)
loaded = web_users.parse(c.WEB_EXTRA_USERS)
check('Config reads the hash unchanged', list(loaded) == ['remote']
      and web_users.check_hash('s3cret-pass-1', loaded['remote']), c.WEB_EXTRA_USERS[:60])

# --- real Basic auth through the handler
parent = types.SimpleNamespace(gateway=types.SimpleNamespace(
    cloudflare_tunnel=None, _plugin_web_routes={}, link_server=None,
    config=types.SimpleNamespace(LINK_PORT=9700)))
Handler = type('H', (GatewayRequestHandler,), {
    'parent': parent, 'username': 'user', 'password': 'main-pass', 'tunnel_allow_open': False,
    'extra_users': loaded})
srv = ThreadedServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def status(user, pw):
    conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
    conn.request('GET', '/nope', headers={'Authorization': 'Basic ' + base64.b64encode(f'{user}:{pw}'.encode()).decode()})
    st = conn.getresponse().status
    conn.close()
    return st


check('main login still works', status('user', 'main-pass') == 404)
check('extra user logs in', status('remote', 's3cret-pass-1') == 404)
check('extra user, wrong password refused', status('remote', 'main-pass') == 401)
check("main password doesn't open the extra name", status('remote', 'main-pass') == 401)
check("extra password doesn't open the main name", status('user', 's3cret-pass-1') == 401)
srv.shutdown()

# --- WEB_LAN_NO_LOGIN: judged from the socket peer, never from headers
import socket  # noqa: E402

H2 = type('H2', (GatewayRequestHandler,), {
    'parent': parent, 'username': 'user', 'password': 'main-pass', 'tunnel_allow_open': False,
    'lan_no_login': True})
srv2 = ThreadedServer(('0.0.0.0', 0), H2)
threading.Thread(target=srv2.serve_forever, daemon=True).start()


def get(host, headers=None):
    conn = http.client.HTTPConnection(host, srv2.server_address[1], timeout=5)
    conn.request('GET', '/nope', headers=headers or {})
    st = conn.getresponse().status
    conn.close()
    return st


check('loopback still needs a login', get('127.0.0.1') == 401)
check('loopback with tunnel headers needs a login', get('127.0.0.1', {'Cf-Connecting-Ip': '192.168.1.5'}) == 401)
try:
    _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _s.connect(('10.255.255.255', 1))
    lan_ip = _s.getsockname()[0]
    _s.close()
except OSError:
    lan_ip = None
if lan_ip and not lan_ip.startswith('127.'):
    check(f'home-network peer ({lan_ip}) gets in with no login', get(lan_ip) == 404)
else:
    print('  SKIP  no LAN address on this machine')
off = types.SimpleNamespace(client_address=('192.168.2.84', 1))
check('192.168.x counts as home', GatewayRequestHandler._from_home_network(off))
check('100.64/10 (Tailscale) counts as home',
      GatewayRequestHandler._from_home_network(types.SimpleNamespace(client_address=('100.101.1.2', 1))))
check('public address does not',
      not GatewayRequestHandler._from_home_network(types.SimpleNamespace(client_address=('203.0.113.9', 1))))
check('loopback does not',
      not GatewayRequestHandler._from_home_network(types.SimpleNamespace(client_address=('127.0.0.1', 1))))
srv2.shutdown()

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
sys.exit(1 if FAIL else 0)
