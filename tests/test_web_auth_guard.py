#!/usr/bin/env python3
"""Login throttling and the access record for the web UI.

Basic auth answered guesses as fast as they arrived and left no trace of who
logged in. Checks, against the pure guard and a real HTTP server: a client
sending different wrong passwords is blocked and stays blocked even for the
right one; a stale tab repeating ONE wrong password is not; a first visit
without credentials is not a failure; blocks are per client and escalate; a
LAN client can't dodge by forging the tunnel header.
"""

import base64
import contextlib
import http.client
import io
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

import metrics  # noqa: E402
from web import auth_guard as ag  # noqa: E402
from web.handler import GatewayRequestHandler, ThreadedServer  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


print("Guard:")
g = ag.AuthGuard()
t = 1000.0
for i in range(ag.MAX_FAILS - 1):
    n, block, first = g.failure('a', t + i, token=i)
check('below the limit: no block', block == 0 and n == ag.MAX_FAILS - 1 and g.blocked_for('a', t + 5) == 0)
n, block, first = g.failure('a', t + 10, token='last')
check('limit reached: blocked for the base time', block == ag.BLOCK_BASE, block)
check('still blocked just before expiry', g.blocked_for('a', t + 10 + ag.BLOCK_BASE - 1) > 0)
check('free after expiry', g.blocked_for('a', t + 10 + ag.BLOCK_BASE + 1) == 0)
check('another client unaffected', g.blocked_for('b', t + 11) == 0)
t2 = t + 200
for i in range(ag.MAX_FAILS):
    n, block, first = g.failure('a', t2 + i, token=('x', i))
check('second block is twice as long', block == ag.BLOCK_BASE * 2, block)
for r in range(12):
    t2 += ag.BLOCK_MAX + 10
    for i in range(ag.MAX_FAILS):
        n, block, first = g.failure('a', t2 + i, token=(r, i))
check('block length is capped', block == ag.BLOCK_MAX, block)

g = ag.AuthGuard()
for i in range(200):
    n, block, first = g.failure('tab', 50.0 + i, token='same-stale-password')
check('one wrong password repeated 200 times never blocks', block == 0 and n == 1, (n, block))
check('repeat is flagged as not new', first is False)

g = ag.AuthGuard()
for i in range(ag.MAX_FAILS - 1):
    g.failure('w', 10.0 + i * (ag.FAIL_WINDOW), token=i)
check('failures spread wider than the window never add up', g.blocked_for('w', 10.0 + 5 * ag.FAIL_WINDOW) == 0
      and g.failure('w', 10.0 + 5 * ag.FAIL_WINDOW, token='z')[1] == 0)

g = ag.AuthGuard()
for i in range(ag.MAX_FAILS * 3):
    n, block, first = g.failure('me', 1.0 + i, token=i, exempt=True)
check('exempt client is counted but never blocked', block == 0 and g.failures == ag.MAX_FAILS * 3)

g = ag.AuthGuard()
check('first good login is new', g.success('c', 5.0) is True)
check('second is not', g.success('c', 6.0) is False)
check('new again after a day', g.success('c', 6.0 + ag.SEEN_MEMORY + 1) is True)
for i in range(ag.MAX_FAILS - 1):
    g.failure('c', 100.0 + i, token=i)
g.success('c', 110.0)
check('a good login clears earlier failures', g.failure('c', 111.0, token='q')[0] == 1)

check('IPv6 addresses in one /64 are one client',
      ag.client_key('2001:db8:1:2::1') == ag.client_key('2001:db8:1:2:ffff::9'))
check('different /64 is a different client',
      ag.client_key('2001:db8:1:2::1') != ag.client_key('2001:db8:1:3::1'))
check('IPv4 kept whole', ag.client_key(' 203.0.113.7 ') == '203.0.113.7')
check('garbage maps to one bucket', ag.client_key('not an ip') == 'unknown')
check('attempt tokens: same in, same out; no plaintext',
      ag.attempt_token('Basic abc') == ag.attempt_token('Basic abc')
      and ag.attempt_token('Basic abc') != ag.attempt_token('Basic abd')
      and b'abc' not in ag.attempt_token('Basic abc'))

g = ag.AuthGuard()
for i in range(ag.MAX_CLIENTS + 500):
    g.failure(f'k{i}', 1.0 + i * 0.001, token=0)
check('tracked clients stay bounded', len(g._clients) <= ag.MAX_CLIENTS, len(g._clients))

print("HTTP server:")
ag.GUARD = ag.AuthGuard()
parent = types.SimpleNamespace(gateway=types.SimpleNamespace(
    cloudflare_tunnel=None, _plugin_web_routes={}, link_server=None,
    config=types.SimpleNamespace(LINK_PORT=9700)))
Handler = type('H', (GatewayRequestHandler,), {'parent': parent, 'password': 'right-horse'})
srv = ThreadedServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(pw=None, ip=None, path='/no-such-page'):
    h = {}
    if pw is not None:
        h['Authorization'] = 'Basic ' + base64.b64encode(f'user:{pw}'.encode()).decode()
    if ip:
        h['Cf-Connecting-Ip'] = ip
        h['Cf-Ray'] = 'test'
    c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
    c.request('GET', path, headers=h)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status, r.getheader('Retry-After')


def counter(name):
    for line in metrics.render()[0].decode().splitlines():
        if line.startswith(name + ' '):
            return float(line.split()[-1])
    return None


log = io.StringIO()
f0 = counter('rg_web_auth_failures_total')
with contextlib.redirect_stdout(log):
    A, B = '198.51.100.7', '198.51.100.8'
    check('right password is let in (404 from the router, not 401)', req('right-horse', A)[0] == 404)
    r_noauth = [req(None, A)[0] for _ in range(20)]
    check('20 requests with no credentials: all 401, none counted', set(r_noauth) == {401}
          and req('right-horse', A)[0] == 404)
    stale = [req('old-password', A)[0] for _ in range(30)]
    check('30 repeats of one stale password: 401 each, never blocked',
          set(stale) == {401} and req('right-horse', A)[0] == 404, set(stale))
    guesses = [req(f'guess{i}', A)[0] for i in range(ag.MAX_FAILS)]
    check('different wrong passwords: 401 until the limit, then 429',
          guesses == [401] * (ag.MAX_FAILS - 1) + [429], guesses)
    st, retry = req('right-horse', A)
    check('blocked client is refused even with the right password', st == 429 and retry and int(retry) > 0, (st, retry))
    check('a blocked client with no credentials still gets the login prompt', req(None, A)[0] == 401)
    check('another client is not affected', req('right-horse', B)[0] == 404)

    # No Cf-Ray: a direct connection. It is from this machine, so never blocked,
    # and a forged Cf-Connecting-Ip alone must not be trusted as an identity.
    local = [req(f'x{i}')[0] for i in range(ag.MAX_FAILS + 3)]
    check('this machine is never blocked', set(local) == {401} and req('right-horse')[0] == 404, local)

srv.shutdown()
out = log.getvalue()
check('new login logged once per client', out.count(f'login from {A} (tunnel)') == 1
      and out.count(f'login from {B} (tunnel)') == 1, out[:400])
check('block is logged with the address', f'BLOCKED {A} (tunnel)' in out)
check('no password ever reaches the log', 'right-horse' not in out and 'guess1' not in out and 'old-password' not in out)
check('failure counter exported and moving', f0 is not None and counter('rg_web_auth_failures_total') > f0)
check('block counters exported', counter('rg_web_auth_blocks_total') >= 1
      and counter('rg_web_auth_refused_blocked_total') >= 1)

# A LAN client forging the tunnel header is keyed by its socket address.
h = Handler.__new__(Handler)
h.client_address = ('192.168.50.20', 5555)
h.headers = {'Cf-Connecting-Ip': '8.8.8.8', 'Cf-Ray': 'forged'}
check('LAN client with a forged tunnel header keeps its real address',
      h._client_identity() == ('192.168.50.20', 'lan'), h._client_identity())

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
