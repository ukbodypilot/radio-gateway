"""Shared MCP server scaffolding — instance, config, HTTP helpers.

Lifted verbatim from the legacy gateway_mcp.py preamble. Tool modules
import ``mcp`` (the FastMCP instance) plus the HTTP helpers from here,
so all the decorator-based registration paths land on the same server.
"""

import base64
import http.client
import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Config — read from gateway_config.txt if present
# ---------------------------------------------------------------------------

# Gateway root — two levels above mcp_server/tools/, one above mcp_server/.
# Tool modules must resolve repo files (gateway_config.txt, automation
# scheme files) against this, NOT against their own dirname: the
# 2026-05-28 split moved them down two directories, which silently broke
# config_read and the automation_scheme_* tools for two months.
GW_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config():
    """Read the web UI port, password and HTTPS mode from gateway_config.txt.

    Values go through the gateway's own parse_config_value, so inline
    comments and quoting mean the same thing here as in the gateway. The
    old hand parser kept inline comments and quotes as part of the password
    (auth failed) and treated only true/1/yes as HTTPS, so the documented
    'self-signed' and 'letsencrypt' modes made every tool talk plain http to
    a TLS socket.
    """
    import sys
    if GW_ROOT not in sys.path:
        sys.path.insert(0, GW_ROOT)
    from config_format import parse_config_value
    from config_schema import DEFAULTS
    cfg_path = os.path.join(GW_ROOT, 'gateway_config.txt')
    port = 8080
    # Same rule as the gateway's loader: a blank value keeps the default.
    username = DEFAULTS['WEB_CONFIG_USERNAME']
    password = DEFAULTS['WEB_CONFIG_PASSWORD']
    https = False
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                line = line.strip()
                if '=' not in line or line.startswith('#') or line.startswith('['):
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = parse_config_value(v)
                if k == 'WEB_CONFIG_PORT':
                    try:
                        port = int(v)
                    except ValueError:
                        pass
                elif k == 'WEB_CONFIG_PASSWORD' and v:
                    password = v
                elif k == 'WEB_CONFIG_USERNAME' and v:
                    username = v
                elif k == 'WEB_CONFIG_HTTPS':
                    https = v.lower() in ('true', '1', 'yes', 'self-signed',
                                          'letsencrypt', 'lets-encrypt', 'le')
    scheme = 'https' if https else 'http'
    return f'{scheme}://127.0.0.1:{port}', username, password


class _LoopbackHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that skips certificate checks for 127.0.0.1 only.

    The gateway's cert is self-signed, or a Let's Encrypt cert for the DDNS
    name -- neither verifies against 127.0.0.1, so every tool failed when
    HTTPS was on. Traffic to any other host keeps full verification.
    """

    def __init__(self):
        import ssl
        super().__init__()
        self._loopback_ctx = ssl.create_default_context()
        self._loopback_ctx.check_hostname = False
        self._loopback_ctx.verify_mode = ssl.CERT_NONE

    def https_open(self, req):
        if req.host.split(':')[0] == '127.0.0.1':
            return self.do_open(http.client.HTTPSConnection, req,
                                context=self._loopback_ctx)
        return super().https_open(req)


GW_BASE_URL, GW_USERNAME, GW_PASSWORD = _load_config()
urllib.request.install_opener(urllib.request.build_opener(_LoopbackHTTPSHandler()))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers():
    if GW_PASSWORD:
        creds = base64.b64encode(f'{GW_USERNAME}:{GW_PASSWORD}'.encode()).decode()
        return {'Authorization': f'Basic {creds}'}
    return {}


def _get(path: str) -> dict:
    url = GW_BASE_URL + path
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}', 'ok': False}
    except Exception as e:
        return {'error': str(e), 'ok': False}


def _post(path: str, data: dict, timeout: int = 10) -> dict:
    url = GW_BASE_URL + path
    body = json.dumps(data).encode()
    headers = {**_auth_headers(), 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}', 'ok': False}
    except Exception as e:
        return {'error': str(e), 'ok': False}


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name='radio-gateway',
    instructions=(
        'Control a software-defined radio (SDR) + radio repeater gateway. '
        'Use gateway_status first to understand what is connected and running. '
        'Frequencies are in MHz unless noted. '
        'PTT commands key/unkey the transmitter — always unkey after transmitting. '
        'TTS and CW transmit audio over the air; confirm the radio is on the right '
        'frequency before transmitting.'
    ),
)
