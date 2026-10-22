"""Helpers for code that used to talk to a single in-core ``gw.kv4p_plugin``.

After the refactor each kv4p is a separately-registered endpoint connected to
the gateway link server (``kv4p-vhf``, ``kv4p-uhf``, ...). Most legacy callers
just want "the kv4p" — a single object whose ``audio_level`` /
``tx_audio_level`` / ``execute(cmd)`` they can poke. These helpers give them
that without each site having to know about endpoint plumbing.

For new code that genuinely needs per-instance data, call ``list_endpoints``
or read ``gw._link_last_status[name]`` directly.
"""

from typing import Optional


_KV4P_PREFIX = 'kv4p-'


def list_endpoints(gw) -> list:
    """Return [(name, LinkAudioSource)] for every connected kv4p endpoint."""
    eps = getattr(gw, 'link_endpoints', {}) or {}
    return [(n, s) for n, s in eps.items() if isinstance(n, str)
            and n.startswith(_KV4P_PREFIX)]


def first_endpoint(gw):
    """Return the first connected kv4p endpoint (LinkAudioSource) or None."""
    pairs = list_endpoints(gw)
    if not pairs:
        return None
    # Sort by name so the "first" is stable (kv4p-uhf would come before
    # kv4p-vhf alphabetically — fine, just deterministic).
    pairs.sort(key=lambda p: p[0])
    return pairs[0][1]


def first_endpoint_name(gw) -> Optional[str]:
    pairs = list_endpoints(gw)
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    return pairs[0][0]


def find_endpoint(gw, instance_or_name):
    """Look up a kv4p endpoint by instance ('vhf'), short name ('kv4p-vhf'),
    or fall back to the first connected one.

    Endpoints register with their trailing 'hf' stripped ('kv4p-v' / 'kv4p-u'),
    so a caller passing 'vhf' or 'kv4p-vhf' still resolves correctly by
    trying the stripped form after the literal one.
    """
    if not instance_or_name:
        return first_endpoint(gw)
    eps = getattr(gw, 'link_endpoints', {}) or {}
    if instance_or_name in eps:
        return eps[instance_or_name]
    full = f'{_KV4P_PREFIX}{instance_or_name}'
    if full in eps:
        return eps[full]
    # Suffix-stripped fallback: 'vhf' → 'v', 'kv4p-vhf' → 'kv4p-v'.
    if instance_or_name.lower().endswith('hf'):
        short_inst = instance_or_name[:-2]
        if short_inst in eps:
            return eps[short_inst]
        short_full = f'{_KV4P_PREFIX}{short_inst}'
        if short_full in eps:
            return eps[short_full]
    return first_endpoint(gw)


def aggregate_status(gw) -> dict:
    """Roll up status across all kv4p endpoints for legacy single-instance
    status keys (kv4p_enabled / kv4p_level / kv4p_tx_level / kv4p_muted).

    First-endpoint wins; the dict also contains ``kv4p_endpoints`` mapping
    name → {audio_level, tx_audio_level, muted, status} for new consumers
    that want per-instance data.
    """
    first = first_endpoint(gw)
    enabled = first is not None and getattr(first, 'server_connected', False)
    per = {}
    status_cache = getattr(gw, '_link_last_status', {}) or {}
    for name, src in list_endpoints(gw):
        per[name] = {
            'audio_level': getattr(src, 'audio_level', 0),
            'tx_audio_level': getattr(src, 'tx_audio_level', 0),
            'muted': bool(getattr(src, 'muted', False)),
            'connected': bool(getattr(src, 'server_connected', False)),
            'status': status_cache.get(name, {}),
        }
    return {
        'kv4p_enabled': enabled,
        'kv4p_level': getattr(first, 'audio_level', 0) if first else 0,
        'kv4p_tx_level': getattr(first, 'tx_audio_level', 0) if first else 0,
        'kv4p_muted': bool(getattr(first, 'muted', False)) if first else False,
        'kv4p_endpoints': per,
    }


def execute(gw, cmd, instance=None):
    """Send a command to a kv4p endpoint. Returns the link server's submit
    result (dict with at least ``ok``: bool, or ``error``).
    """
    name = None
    if instance:
        full = f'{_KV4P_PREFIX}{instance}' if not instance.startswith(_KV4P_PREFIX) else instance
        if full in (getattr(gw, 'link_endpoints', {}) or {}):
            name = full
    if name is None:
        name = first_endpoint_name(gw)
    if not name:
        return {"ok": False, "error": "no kv4p endpoint connected"}
    srv = getattr(gw, 'link_server', None)
    if not srv:
        return {"ok": False, "error": "link server not running"}
    try:
        srv.send_command_to(name, cmd)
        return {"ok": True, "endpoint": name}
    except Exception as e:
        return {"ok": False, "error": str(e), "endpoint": name}


def set_muted(gw, muted, instance=None):
    """Toggle mute on a specific kv4p endpoint (or the first one)."""
    src = find_endpoint(gw, instance) if instance else first_endpoint(gw)
    if src is None:
        return False
    src.muted = bool(muted)
    return True


# ── Property installed on RadioGateway as `kv4p_plugin` shim ────────

class _KV4PProxy:
    """Quacks like the old in-core KV4PPlugin for legacy callers.

    Returns sane zero/False defaults when no kv4p endpoint is connected so
    callers that do ``if gw.kv4p_plugin: ...`` see a falsy value, matching
    the pre-refactor behaviour.
    """

    __slots__ = ('_gw',)

    def __init__(self, gw):
        self._gw = gw

    def __bool__(self):
        return first_endpoint(self._gw) is not None

    def _src(self):
        return first_endpoint(self._gw)

    @property
    def audio_level(self):
        s = self._src(); return getattr(s, 'audio_level', 0) if s else 0

    @audio_level.setter
    def audio_level(self, v):
        s = self._src()
        if s is not None: s.audio_level = v

    @property
    def tx_audio_level(self):
        s = self._src(); return getattr(s, 'tx_audio_level', 0) if s else 0

    @tx_audio_level.setter
    def tx_audio_level(self, v):
        s = self._src()
        if s is not None: s.tx_audio_level = v

    @property
    def muted(self):
        s = self._src(); return bool(getattr(s, 'muted', False)) if s else False

    @muted.setter
    def muted(self, v):
        s = self._src()
        if s is not None: s.muted = bool(v)

    @property
    def tx_muted(self):
        s = self._src(); return bool(getattr(s, 'tx_muted', False)) if s else False

    @property
    def server_connected(self):
        s = self._src(); return bool(getattr(s, 'server_connected', False)) if s else False

    @property
    def ptt_active(self):
        # Surface PTT state from the cached endpoint status. The link
        # server pushes plugin status fields into _link_last_status on
        # each update, and the kv4p plugin emits 'transmitting'.
        name = first_endpoint_name(self._gw)
        if not name:
            return False
        st = (getattr(self._gw, '_link_last_status', {}) or {}).get(name, {})
        return bool(st.get('transmitting', False))

    @property
    def _processor(self):
        # No gateway-side processor any more; let callers that probe this
        # field see None rather than blowing up.
        return None

    @property
    def _tx_buf(self):
        return b''

    @_tx_buf.setter
    def _tx_buf(self, v):
        # No-op: TX buffering lives endpoint-side now. Pre-PTT clears used
        # to reset gw.kv4p_plugin._tx_buf to drop stale audio — that's
        # handled by KV4PPlugin._set_ptt inside the endpoint already.
        pass

    def execute(self, cmd):
        return execute(self._gw, cmd)

    def get_status(self):
        s = self._src()
        if s is None:
            return {'plugin': 'kv4p', 'connected': False}
        name = first_endpoint_name(self._gw)
        cached = (getattr(self._gw, '_link_last_status', {}) or {}).get(name, {})
        return cached or {'plugin': 'kv4p', 'connected': True}

    def get_trace_snapshot(self):
        # Trace counters used to be per-tick instrumentation owned by the
        # in-core plugin. Endpoint plugins no longer push these to the
        # gateway, so return zeros — keeps the watchdog CSV columns stable.
        return {}


def install_proxy(gw):
    """Replace gw.kv4p_plugin with a live proxy. Idempotent."""
    if not isinstance(getattr(gw, 'kv4p_plugin', None), _KV4PProxy):
        gw.kv4p_plugin = _KV4PProxy(gw)
