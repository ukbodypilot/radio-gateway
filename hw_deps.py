"""Hardware and Mumble libraries the gateway needs at run time.

Importing a gateway module used to sys.exit(1) when hidapi, pyaudio or
pymumble was missing, so nothing could be imported (or tested) off the box.
Modules now import these optionally, and main() calls require() before it
starts anything, which stops the service with the same install hints.

patch_ssl() holds the SSL shims pymumble needs on Python 3.12+ and against
old Murmur servers. It used to be copied into both radio_gateway.py and
gateway_core.py; it is idempotent, so both can still call it.
"""

import importlib
import ssl as _ssl
import sys

# (import name, what to call it, how to install it)
_REQUIRED = (
    ('hid', 'hidapi library', ['pip3 install hidapi --break-system-packages']),
    ('pyaudio', 'pyaudio library', ['sudo apt-get install python3-pyaudio']),
)
_PYMUMBLE_HINT = ['pip3 install pymumble --break-system-packages',
                  'pip3 install pymumble-py3 --break-system-packages']

_patched = False


def _import_pymumble_mod():
    """pymumble's .mumble module under either PyPI name, or None."""
    for name in ('pymumble_py3', 'pymumble'):
        try:
            return importlib.import_module(name + '.mumble')
        except ImportError:
            continue
    return None


def missing():
    """[(what, [install hints]), ...] for each required library not importable."""
    out = []
    for mod, what, hints in _REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append((what, hints))
    if _import_pymumble_mod() is None:
        out.append(('pymumble library', _PYMUMBLE_HINT))
    return out


def require():
    """Exit with install hints if a required library is missing."""
    gone = missing()
    for what, hints in gone:
        print(f"ERROR: {what} not found!")
        print(f"Install it with: {hints[0]}")
        for h in hints[1:]:
            print(f"          or: {h}")
    if gone:
        sys.exit(1)


def _ssl_wrap_compat(sock, keyfile=None, certfile=None, server_side=False,
                     cert_reqs=None, ssl_version=None, ca_certs=None,
                     do_handshake_on_connect=True, suppress_ragged_eofs=True,
                     ciphers=None, **_):
    ctx = _ssl.SSLContext(
        _ssl.PROTOCOL_TLS_SERVER if server_side else _ssl.PROTOCOL_TLS_CLIENT
    )
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    ctx.minimum_version = _ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    if certfile:
        ctx.load_cert_chain(certfile, keyfile)
    if ca_certs:
        ctx.load_verify_locations(ca_certs)
    if ciphers:
        ctx.set_ciphers(ciphers)
    return ctx.wrap_socket(sock, server_side=server_side,
                           do_handshake_on_connect=do_handshake_on_connect,
                           suppress_ragged_eofs=suppress_ragged_eofs)


def _wrap_socket_compat(sock, keyfile=None, certfile=None,
                        verify_mode=_ssl.CERT_NONE, server_hostname=None):
    # ssl.create_default_context() enforces SECLEVEL=1+, which rejects the
    # SHA-1 certs old Murmur 1.2.x servers generate. Lower to SECLEVEL=0.
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    ctx.minimum_version = _ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    if certfile:
        ctx.load_cert_chain(certfile, keyfile)
    return ctx.wrap_socket(sock, server_hostname=server_hostname)


def patch_ssl():
    """Apply the pymumble SSL shims once. Safe to call when pymumble is missing."""
    global _patched
    if _patched:
        return
    # pymumble uses ssl.wrap_socket() (removed in 3.12) and
    # ssl.PROTOCOL_TLSv1_2 (deprecated). Patch before importing pymumble.
    if not hasattr(_ssl, 'wrap_socket'):
        _ssl.wrap_socket = _ssl_wrap_compat
    if not hasattr(_ssl, 'PROTOCOL_TLSv1_2'):
        _ssl.PROTOCOL_TLSv1_2 = _ssl.PROTOCOL_TLS_CLIENT
    mod = _import_pymumble_mod()
    if mod is not None:
        mod._wrap_socket = _wrap_socket_compat
    _patched = True
