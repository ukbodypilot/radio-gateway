"""Cloudflare tunnel + Google Drive MCP tools.

Split out of mcp_server/tools/routing.py on 2026-05-30; tools registered
against the shared ``mcp`` instance via @mcp.tool() decorator side
effects on import.
"""

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def cloudflare_status() -> str:
    """
    Get the Cloudflare tunnel URL and its short links (TUNNEL_SHORT_LINKS: one
    spoo.me link for the gateway and one per APP_LINKS path, remade whenever the
    tunnel URL changes; the same links go out in emails).
    """
    result = _get('/status')
    url = result.get('tunnel_url', '')
    if not url:
        return "No Cloudflare tunnel active"
    lines = [f"Tunnel URL: {url}"]
    short = result.get('tunnel_short_links') or {}
    if short.get('links'):
        for path, link in short['links'].items():
            lines.append(f"Short {path:<9} {link}")
    elif short.get('blocked'):
        lines.append(f"Short links: off ({short['blocked']})")
    elif short.get('last_error'):
        lines.append(f"Short links: failing ({short['last_error']})")
    return "\n".join(lines)


@mcp.tool()
def tunnel_link_url() -> str:
    """
    Get the Cloudflare tunnel URL plus the derived WebSocket link URL used by
    remote endpoints (ws(s)://host/ws/link). Useful for provisioning a new
    endpoint with the correct link target.
    """
    data = _get('/api/tunnel/link-url')
    if data.get('ok') is False:
        return f"Error: {data.get('error', 'unknown')}"
    url = data.get('url')
    ws = data.get('ws_link')
    if not url:
        return "No Cloudflare tunnel active"
    return f"Tunnel: {url}\nWS link: {ws}"


@mcp.tool()
def gdrive_status() -> str:
    """
    Get Google Drive integration status: rclone remote and folder, authentication,
    folder access and storage use. The tunnel URL is published there for endpoints.
    """
    data = _get('/api/gdrive/status', timeout=30)   # runs `rclone about`: seconds when its cache is cold
    if 'error' in data and not data.get('configured'):
        return f"Error: {data['error']}"
    if not data.get('configured'):
        return "Google Drive not configured (ENABLE_GDRIVE=false)"
    gb = 1024 ** 3
    lines = ["Google Drive Status:",
             f"  Remote: {data.get('remote', '?')}  Folder: {data.get('folder_path', '?')}",
             f"  Authenticated: {data.get('authenticated', False)}",
             f"  Accessible: {data.get('folder_accessible', False)}"]
    if data.get('total_bytes'):
        lines.append(f"  Storage: {data.get('used_bytes', 0) / gb:.1f} GB used of "
                     f"{data['total_bytes'] / gb:.1f} GB ({data.get('free_bytes', 0) / gb:.1f} GB free)")
    for k in ('error', 'folder_error'):
        if data.get(k):
            lines.append(f"  Error: {data[k]}")
    return "\n".join(lines)


@mcp.tool()
def gdrive_list_files() -> str:
    """
    List files in the gateway's Google Drive folder.
    Shows file names, sizes, and modification times.
    """
    data = _get('/api/gdrive/files')
    files = data.get('files', [])
    if not files:
        return "No files in Drive folder"
    lines = ["Google Drive Files:", ""]
    for f in files:
        size = f.get('size', '?')
        if size != '?':
            size = int(size)
            if size >= 1048576:
                size = f"{size/1048576:.1f} MB"
            elif size >= 1024:
                size = f"{size/1024:.0f} KB"
            else:
                size = f"{size} B"
        mod = (f.get('modifiedTime', '')[:19].replace('T', ' ')) or '?'
        lines.append(f"  {f['name']}  ({size})  {mod}")
    return "\n".join(lines)


@mcp.tool()
def gdrive_publish_tunnel() -> str:
    """
    Publish the current Cloudflare tunnel URL to Google Drive.
    This writes tunnel_url.json to the shared Drive folder so
    remote endpoints can discover the gateway address.
    """
    result = _post('/api/gdrive/publish-tunnel', {})
    if result.get('ok'):
        return "Tunnel URL published to Google Drive"
    return f"Error: {result.get('error', 'unknown')}"
