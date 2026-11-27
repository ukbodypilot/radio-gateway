"""Tunnel short links: made per tunnel URL, refused on a default password, never blocking an email.

Also the tunnel's change detection: after the health check kills an expired tunnel it
blanks _url, and the respawned URL used to arrive with no "old" URL, so no change email.
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import short_links

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


tmpdir = tempfile.mkdtemp()
short_links.SHORT_FILE = os.path.join(tmpdir, 'short.json')

calls = []
answer = {'ok': True}
made = [0]


def fake_shorten(long_url):
    calls.append(long_url)
    if not answer['ok']:
        return None
    made[0] += 1
    return f"https://tinyurl.com/x{made[0]}"


short_links._shorten = fake_shorten


def cfg(on=True, pw='s3cret-pass', apps='Tesla=/tesla/, Family=/family/, LAN=http://10.0.0.1:8480/'):
    return types.SimpleNamespace(TUNNEL_SHORT_LINKS=on, WEB_CONFIG_PASSWORD=pw, APP_LINKS=apps)


def reset():
    calls.clear()
    made[0] = 0
    short_links._state.update(tunnel=None, links={})
    short_links._last_fail.clear()
    try:
        os.unlink(short_links.SHORT_FILE)
    except OSError:
        pass


T1 = 'https://alpha-beta-gamma-delta.trycloudflare.com'
T2 = 'https://other-words-here-now.trycloudflare.com'

# --- gating
reset()
check('off by default config -> None', short_links.get(cfg(on=False), T1) is None and not calls)
check('default password refused', short_links.get(cfg(pw='user'), T1) is None and not calls)
check('blank password refused', short_links.get(cfg(pw=''), T1) is None and not calls)
check('blocked reason names the password', 'password' in short_links.blocked_reason(cfg(pw='user')))
check('string "true" counts as on', short_links.enabled(cfg(on='true')))

# --- paths
check('app paths = root + gateway paths only',
      short_links.app_paths(cfg()) == ['/', '/tesla/', '/family/'], str(short_links.app_paths(cfg())))

# --- making + caching
reset()
a = short_links.get(cfg(), T1, '/')
b = short_links.get(cfg(), T1 + '/', '/')
check('made once, then cached', a == b == 'https://tinyurl.com/x1' and calls == [T1 + '/'], str(calls))
t = short_links.get(cfg(), T1, '/tesla/')
check('per-path link', t == 'https://tinyurl.com/x2' and calls[-1] == T1 + '/tesla/')
d = json.load(open(short_links.SHORT_FILE))
check('file keyed by tunnel', d['tunnel'] == T1 and d['links'] == {'/': a, '/tesla/': t}, str(d))

# --- restart on the same tunnel reuses the file
short_links._state.update(tunnel=None, links={})
calls.clear()
check('restart reuses file', short_links.get(cfg(), T1, '/') == a and not calls)

# --- new tunnel -> new links, file rewritten
c = short_links.get(cfg(), T2, '/')
check('new tunnel makes a new link', c != a and calls == [T2 + '/'])
d = json.load(open(short_links.SHORT_FILE))
check('file now names the new tunnel only', d['tunnel'] == T2 and list(d['links']) == ['/'], str(d))

# --- failure backs off
reset()
answer['ok'] = False
check('failure -> None', short_links.get(cfg(), T1, '/') is None)
check('second try within RETRY_AFTER does not call out', short_links.get(cfg(), T1, '/') is None and len(calls) == 1)
short_links._last_fail[T1 + '/'] -= short_links.RETRY_AFTER + 1
answer['ok'] = True
check('retries after the wait', short_links.get(cfg(), T1, '/') == 'https://tinyurl.com/x1')

# --- display
check('display drops the scheme', short_links.display('https://tinyurl.com/abc') == 'tinyurl.com/abc')

# --- emails
import email_notifier  # noqa: E402

reset()
gw = types.SimpleNamespace(cloudflare_tunnel=types.SimpleNamespace(get_url=lambda: T1))
en = email_notifier.EmailNotifier(cfg(), gw)
lines = en._short_lines(T1)
check('email lines: root + apps', len(lines) == 3 and lines[0].startswith('Short:') and '/tesla/' in lines[1], str(lines))
body = en._with_short_footer('alert text')
check('footer added to a plain email', body.endswith('Gateway (short link): https://tinyurl.com/x1'), body)
check('no footer when the body already has it', en._with_short_footer(body) == body)
en_off = email_notifier.EmailNotifier(cfg(pw='user'), gw)
check('switched on + default password: email says why',
      'password' in ' '.join(en_off._short_lines(T1)), str(en_off._short_lines(T1)))
check('switched off: no line at all', email_notifier.EmailNotifier(cfg(on=False), gw)._short_lines(T1) == [])
check('footer skipped when blocked', en_off._with_short_footer('x') == 'x')

# --- tunnel change detection survives the health check blanking _url
import cloudflare_tunnel  # noqa: E402

cloudflare_tunnel.CloudflareTunnel.URL_FILE = os.path.join(tmpdir, 'url')
changed = []
tun = cloudflare_tunnel.CloudflareTunnel(cfg(on=False), on_url_changed=changed.append)
tun._on_log_line(f'INF |  {T1}  |')
check('first URL is not a change', changed == [])
tun._url = None                       # what the health check does after killing cloudflared
tun._on_log_line(f'INF |  {T2}  |')
check('respawn after health-check kill emails the change', changed == [T2], str(changed))
tun._on_log_line(f'INF |  {T2}  |')
check('same URL again is not a change', changed == [T2])

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
sys.exit(1 if FAIL else 0)
