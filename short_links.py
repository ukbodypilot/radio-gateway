"""Short links for the Cloudflare quick-tunnel URL, for typing on a phone or car browser.

The trycloudflare hostname is four random words, long to type. With TUNNEL_SHORT_LINKS on,
the gateway asks spoo.me for a short link with a random 7-character lowercase code (easy to
type on a car keyboard). TinyURL and is.gd are fallbacks only: both BLACKLIST every
*.trycloudflare.com URL (phishing abuse; checked 2026-11-27), as do da.gd, v.gd and clck.ru.
Links go to the tunnel root and to each APP_LINKS path (/tesla/, /family/), and are made per
tunnel URL: when cloudflared comes back with a new hostname, the next lookup makes new ones.

The result is cached in SHORT_FILE (beside the tunnel URL file) so a gateway restart on the
same tunnel reuses the links, and so the Tesla and Family Locator apps on this machine can put
the same short link in their own emails. Format:
    {"tunnel": "https://x.trycloudflare.com", "links": {"/": "https://tinyurl.com/abc", ...},
     "updated": 1700000000}
A file whose "tunnel" is not the current URL is stale and must be ignored.

A short code can be guessed by scanning, the trycloudflare name cannot. So no links are made
while WEB_CONFIG_PASSWORD is blank or the default "user": the short link would hand the whole
gateway to anyone who scans for it.

Network calls are bounded (TIMEOUT per service) and a failure waits RETRY_AFTER before the
next try, so a dead shortener costs an email a few seconds once, not every time.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

SHORT_FILE = '/tmp/cloudflare_tunnel_short.json'
TIMEOUT = 6
RETRY_AFTER = 300

ALIAS_LEN = 7               # 36^7 codes: not worth scanning, and the gateway asks for a login anyway
_SERVICES = (
    ('tinyurl', 'https://tinyurl.com/api-create.php?url='),
    ('is.gd', 'https://is.gd/create.php?format=simple&url='),
)

_lock = threading.Lock()
_state = {'tunnel': None, 'links': {}}
_last_fail = {}            # long url -> time of the last failed attempt
stats = {'made': 0, 'failed': 0, 'cached': 0, 'last_error': '', 'blocked': ''}


def enabled(config):
    """TUNNEL_SHORT_LINKS as a bool."""
    return str(config.TUNNEL_SHORT_LINKS).strip().lower() in ('true', '1', 'yes', 'on')


def blocked_reason(config):
    """Why short links are off for this config, or '' when they may be made."""
    if not enabled(config):
        return 'TUNNEL_SHORT_LINKS is off'
    pw = str(config.WEB_CONFIG_PASSWORD or '')
    if pw in ('', 'user'):
        return 'web password is blank or the default "user"; set WEB_CONFIG_PASSWORD first'
    return ''


def app_paths(config):
    """The tunnel root plus every APP_LINKS entry that is a gateway path."""
    from web.sysinfo import parse_app_links
    paths = ['/']
    for a in parse_app_links(config.APP_LINKS):
        if a['url'].startswith('/') and a['url'] not in paths:
            paths.append(a['url'])
    return paths


def _spoo(long_url):
    """spoo.me with our own lowercase code; a taken code gets one more try. Returns (url, error)."""
    import json as _json
    import secrets
    import string
    err = ''
    for _ in range(2):
        alias = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(ALIAS_LEN))
        data = urllib.parse.urlencode({'url': long_url, 'alias': alias}).encode()
        req = urllib.request.Request('https://spoo.me/', data=data, method='POST',
                                     headers={'Accept': 'application/json', 'User-Agent': 'radio-gateway'})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = _json.loads(r.read(2000) or b'{}').get('short_url', '')
            if out.startswith(('http://spoo.me/', 'https://spoo.me/')):
                return 'https://' + out.split('://', 1)[1], ''     # spoo.me serves https too
            err = f'spoo.me: unexpected reply {str(out)[:40]!r}'
        except Exception as e:
            err = f'spoo.me: {e}'
    return None, err


def _shorten(long_url):
    """Ask each service in turn. Returns the short URL or None."""
    out, err = _spoo(long_url)
    if out:
        return out
    errs = [err]
    for name, api in _SERVICES:
        try:
            req = urllib.request.Request(api + urllib.parse.quote(long_url, safe=''),
                                         headers={'User-Agent': 'radio-gateway'})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = r.read(200).decode('ascii', 'replace').strip()
            if out.startswith('https://') and len(out) < 60 and ' ' not in out:
                return out
            errs.append(f'{name}: unexpected reply {out[:40]!r}')
        except Exception as e:
            errs.append(f'{name}: {e}')
    err = '; '.join(errs)
    stats['last_error'] = err
    print(f"  [ShortLink] could not shorten {long_url}: {err}")
    return None


def _load_file(tunnel):
    try:
        with open(SHORT_FILE) as f:
            d = json.load(f)
        if d.get('tunnel') == tunnel and isinstance(d.get('links'), dict):
            return dict(d['links'])
    except (OSError, ValueError):
        pass
    return {}


def _save_file():
    tmp = SHORT_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump({'tunnel': _state['tunnel'], 'links': _state['links'], 'updated': int(time.time())}, f)
        os.replace(tmp, SHORT_FILE)
    except OSError as e:
        print(f"  [ShortLink] could not write {SHORT_FILE}: {e}")


def get(config, tunnel_url, path='/'):
    """The short link for tunnel_url + path, making it if needed. None when off or failing."""
    if not tunnel_url:
        return None
    reason = blocked_reason(config)
    stats['blocked'] = reason
    if reason:
        return None
    tunnel_url = tunnel_url.rstrip('/')
    with _lock:
        if _state['tunnel'] != tunnel_url:          # new tunnel: old links point at a dead hostname
            _state['tunnel'] = tunnel_url
            _state['links'] = _load_file(tunnel_url)
            if _state['links']:
                stats['cached'] += len(_state['links'])
        if path in _state['links']:
            return _state['links'][path]
        long_url = tunnel_url + path
        if time.time() - _last_fail.get(long_url, 0) < RETRY_AFTER:
            return None
        short = _shorten(long_url)
        if not short:
            stats['failed'] += 1
            _last_fail[long_url] = time.time()
            return None
        _state['links'][path] = short
        stats['made'] += 1
        _save_file()
        print(f"  [ShortLink] {long_url} -> {short}")
        return short


def current(config, tunnel_url):
    """What /status shows: the links already made for this tunnel (no network), or why none."""
    out = {'blocked': blocked_reason(config), 'last_error': stats['last_error'], 'links': {}}
    if tunnel_url and not out['blocked']:
        tunnel_url = tunnel_url.rstrip('/')
        with _lock:
            if _state['tunnel'] == tunnel_url:
                out['links'] = dict(_state['links'])
        if not out['links']:
            out['links'] = _load_file(tunnel_url)
    return out


def refresh_async(config, tunnel_url):
    """Make the links for a (new) tunnel URL in the background, so emails find them ready."""
    if not tunnel_url or blocked_reason(config):
        return

    def _run():
        for p in app_paths(config):
            get(config, tunnel_url, p)

    threading.Thread(target=_run, daemon=True, name='short-links').start()


def display(short):
    """'https://tinyurl.com/abc' -> 'tinyurl.com/abc' (what you type)."""
    return short.split('://', 1)[-1] if short else short
