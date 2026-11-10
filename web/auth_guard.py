"""Login throttling and an access record for the web UI.

Basic auth alone lets a client guess as fast as the server answers, and the
server kept no record of who logged in. AuthGuard counts wrong passwords per
client, blocks a client that keeps guessing, and says when a client it has
not seen recently logs in.

Only a request that SENDS credentials and gets them wrong is a failure. A
browser's first request carries none and is answered 401 as usual; counting
those would block every legitimate user on their first visit.

What is counted is DISTINCT wrong credentials. Someone guessing sends a new
password each time; a dashboard tab left open across a password change sends
the same stale one every second, and must not lock its owner out.

No lines are written from here: the handler prints what it is told to, so
this module stays pure and testable with an injected clock.
"""

import hashlib
import ipaddress
import os
import threading

MAX_FAILS = 5            # distinct wrong passwords ...
FAIL_WINDOW = 600.0      # ... within this many seconds trigger a block
BLOCK_BASE = 60.0        # first block; doubles with each further block
BLOCK_MAX = 3600.0
STRIKE_MEMORY = 86400.0  # a client's block history is forgotten after this
SEEN_MEMORY = 86400.0    # "new client" is announced once per client per this
MAX_CLIENTS = 4096       # hard cap on tracked clients

_SALT = os.urandom(16)   # per process; attempts are kept only as salted digests


def attempt_token(credentials):
    """Opaque token for one attempted credential, so repeats can be told from
    new guesses without keeping what was typed."""
    return hashlib.sha256(_SALT + (credentials or '').encode('utf-8', 'replace')).digest()[:12]


def client_key(addr):
    """Throttle key for an address. An IPv6 client owns a whole /64 and can
    rotate through it freely, so the /64 is the client."""
    try:
        ip = ipaddress.ip_address((addr or '').strip())
    except ValueError:
        return 'unknown'
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f'{ip}/64', strict=False).network_address) + '/64'
    return str(ip)


class _Client:
    __slots__ = ('fails', 'blocked_until', 'strikes', 'last_strike', 'last_ok', 'last_touch')

    def __init__(self):
        self.fails = {}          # attempt token -> time last seen
        self.blocked_until = 0.0
        self.strikes = 0         # blocks served; sets the next block's length
        self.last_strike = 0.0
        self.last_ok = None
        self.last_touch = 0.0


class AuthGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = {}
        # Instrumentation, mirrored to Prometheus by the handler.
        self.failures = 0
        self.blocks = 0
        self.refused_while_blocked = 0

    def _get(self, key, now):
        c = self._clients.get(key)
        if c is None:
            if len(self._clients) >= MAX_CLIENTS:
                self._prune(now)
            c = self._clients[key] = _Client()
        c.last_touch = now
        return c

    def _prune(self, now):
        """Drop idle clients; if still full, drop the least recently seen."""
        idle = [k for k, c in self._clients.items()
                if c.blocked_until <= now and now - c.last_touch > STRIKE_MEMORY]
        for k in idle:
            del self._clients[k]
        if len(self._clients) >= MAX_CLIENTS:
            oldest = sorted(self._clients, key=lambda k: self._clients[k].last_touch)
            for k in oldest[:MAX_CLIENTS // 4]:
                if self._clients[k].blocked_until <= now:
                    del self._clients[k]

    def blocked_for(self, key, now):
        """Seconds this client must still wait, 0 if it may try."""
        with self._lock:
            c = self._clients.get(key)
            if c is None or c.blocked_until <= now:
                return 0.0
            self.refused_while_blocked += 1
            return c.blocked_until - now

    def failure(self, key, now, token=None, exempt=False):
        """Record a wrong password. Returns (distinct_in_window, block_seconds,
        first_time); block_seconds is 0 unless this failure started a block,
        and first_time is False for a repeat of a credential already counted.
        An exempt client (this machine) is counted but never blocked."""
        with self._lock:
            self.failures += 1
            c = self._get(key, now)
            c.fails = {k: t for k, t in c.fails.items() if now - t < FAIL_WINDOW}
            if token is None:
                token = object()      # no token given: every failure is distinct
            first = token not in c.fails
            c.fails[token] = now
            n = len(c.fails)
            if exempt or n < MAX_FAILS:
                return n, 0.0, first
            if now - c.last_strike > STRIKE_MEMORY:
                c.strikes = 0
            c.strikes += 1
            c.last_strike = now
            secs = min(BLOCK_BASE * (2 ** (c.strikes - 1)), BLOCK_MAX)
            c.blocked_until = now + secs
            c.fails = {}
            self.blocks += 1
            return n, secs, first

    def success(self, key, now):
        """Record a good login. Returns True when this client has not logged
        in within SEEN_MEMORY, i.e. it is worth a line in the log."""
        with self._lock:
            c = self._get(key, now)
            c.fails = {}
            new = c.last_ok is None or now - c.last_ok > SEEN_MEMORY
            c.last_ok = now
            return new

    def snapshot(self, now):
        with self._lock:
            return {
                'failures': self.failures,
                'blocks': self.blocks,
                'refused_while_blocked': self.refused_while_blocked,
                'blocked_now': sorted(k for k, c in self._clients.items()
                                      if c.blocked_until > now),
                'tracked': len(self._clients),
            }


GUARD = AuthGuard()
