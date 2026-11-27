"""Extra web-UI logins beside WEB_CONFIG_USERNAME / WEB_CONFIG_PASSWORD.

WEB_EXTRA_USERS = name:pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>, name2:...
Only salted PBKDF2 hashes are stored, never a password: add or remove entries with
    python3 tools/web_user.py add <name>       (asks for the password)
    python3 tools/web_user.py remove <name>
and restart the gateway. Every extra user has the same access as the main login.

The browser re-sends Basic credentials on every request, and one PBKDF2 check costs
~0.1 s of CPU, so a credential that verified once is remembered (as a SHA-256 of the
header, in memory only) and later requests skip the hash.
"""

import hashlib
import hmac
import secrets
import threading

ITERATIONS = 240_000
_CACHE_MAX = 64

_ok_cache = {}             # sha256("user:pw") -> user name
_cache_lock = threading.Lock()


def make_hash(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def check_hash(password, stored):
    try:
        algo, it, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def parse(text):
    """'a:hash, b:hash' -> {'a': hash, 'b': hash}. Malformed entries are dropped."""
    users = {}
    for part in str(text or '').split(','):
        name, sep, h = part.strip().partition(':')
        name, h = name.strip(), h.strip()
        if sep and name and h.startswith('pbkdf2_sha256$'):
            users[name] = h
    return users


def format_users(users):
    return ', '.join(f"{n}:{h}" for n, h in users.items())


def verify(users, user, password):
    """The user name when user/password matches an extra user, else None."""
    stored = users.get(user)
    if not stored:
        return None
    key = hashlib.sha256(f"{user}:{password}".encode('utf-8')).hexdigest()
    with _cache_lock:
        if _ok_cache.get(key) == user:
            return user
    if not check_hash(password, stored):
        return None
    with _cache_lock:
        if len(_ok_cache) >= _CACHE_MAX:
            _ok_cache.clear()
        _ok_cache[key] = user
    return user
