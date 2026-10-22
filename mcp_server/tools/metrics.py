"""Prometheus metrics MCP tools — list what's exported, run PromQL queries
against the local Prometheus instance.

The gateway exports ~13 metrics at /metrics (handled by metrics.py); a
native Prometheus instance scrapes them and is reverse-proxied at
http://127.0.0.1:9090/prometheus/. These tools let MCP callers ask
"what's the CPU temp?" or "is broadcastify still streaming?" without
shelling to curl + jq.
"""

import json
import urllib.parse
import urllib.request

from mcp_server.server import mcp, GW_BASE_URL, _auth_headers


PROM_BASE = 'http://127.0.0.1:9090/prometheus'


@mcp.tool()
def metrics_list() -> str:
    """
    List every Prometheus metric the gateway exports, with its HELP text
    and type. Returns one line per metric, e.g.
        rg_cpu_temp_c (gauge): CPU temperature in degrees Celsius.

    Use this to discover what metrics_query can ask about.
    """
    try:
        req = urllib.request.Request(GW_BASE_URL + '/metrics', headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return f"Error fetching /metrics: {e}"

    # Parse # HELP + # TYPE pairs. Skip the _created counter variants —
    # they duplicate the parent name and add noise.
    helps = {}
    types = {}
    for line in body.splitlines():
        if line.startswith('# HELP '):
            parts = line[7:].split(' ', 1)
            if len(parts) == 2:
                helps[parts[0]] = parts[1]
        elif line.startswith('# TYPE '):
            parts = line[7:].split(' ', 1)
            if len(parts) == 2:
                types[parts[0]] = parts[1]

    names = sorted(n for n in helps if not n.endswith('_created'))
    if not names:
        return 'No metrics exported (gateway may not be serving /metrics).'
    lines = []
    for n in names:
        t = types.get(n, '?')
        h = helps.get(n, '')
        lines.append(f"{n} ({t}): {h}")
    return '\n'.join(lines)


@mcp.tool()
def metrics_query(query: str) -> str:
    """
    Run an instant PromQL query against the local Prometheus instance.
    Returns the matching series with their current values.

    Examples:
        metrics_query("rg_cpu_temp_c")
            → current CPU temperature
        metrics_query("rate(rg_stream_bytes_sent_total[1m])")
            → broadcastify throughput, bytes/sec averaged over last minute
        metrics_query("rg_link_endpoint_up == 0")
            → which link endpoints are currently down

    Use metrics_list to discover available metric names.

    Args:
        query: A PromQL expression. Instant queries only — for ranges,
               use the Grafana dashboard at /grafana/.
    """
    url = f"{PROM_BASE}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        return f"Error querying Prometheus: {e}"

    if data.get('status') != 'success':
        return f"Prometheus error: {data.get('error', 'unknown')}"

    result = data.get('data', {}).get('result', [])
    if not result:
        return f"No series match '{query}'"

    # Render each series as one line: "name{label=val, ...} = value"
    lines = []
    for series in result:
        metric = series.get('metric', {})
        name = metric.pop('__name__', '?')
        labels = ', '.join(f"{k}={v}" for k, v in sorted(metric.items())
                           if k not in ('instance', 'job'))
        label_str = f"{{{labels}}}" if labels else ''
        value = series.get('value', [None, '?'])[1]
        lines.append(f"{name}{label_str} = {value}")
    return '\n'.join(lines)
