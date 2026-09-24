"""Shared MCP server scaffolding — instance, config, HTTP helpers.

Lifted verbatim from the legacy gateway_mcp.py preamble. Tool modules
import ``mcp`` (the FastMCP instance) plus the HTTP helpers from here,
so all the decorator-based registration paths land on the same server.
"""

import base64
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
    cfg_path = os.path.join(GW_ROOT, 'gateway_config.txt')
    port = 8080
    password = ''
    https = False
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            for line in f:
                line = line.strip()
                if '=' not in line or line.startswith('#'):
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = v.strip()
                if k == 'WEB_CONFIG_PORT':
                    try:
                        port = int(v)
                    except ValueError:
                        pass
                elif k == 'WEB_CONFIG_PASSWORD':
                    password = v
                elif k == 'WEB_CONFIG_HTTPS':
                    https = v.lower() in ('true', '1', 'yes')
    scheme = 'https' if https else 'http'
    return f'{scheme}://127.0.0.1:{port}', password


GW_BASE_URL, GW_PASSWORD = _load_config()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers():
    if GW_PASSWORD:
        creds = base64.b64encode(f'admin:{GW_PASSWORD}'.encode()).decode()
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
