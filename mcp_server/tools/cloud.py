"""Cloudflare tunnel + Google Drive MCP tools.

Split out of mcp_server/tools/routing.py on 2026-05-30; tools registered
against the shared ``mcp`` instance via @mcp.tool() decorator side
effects on import.
"""

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def cloudflare_status() -> str:
    """
    Get the Cloudflare tunnel URL and connection status.
    """
    result = _get('/status')
    url = result.get('tunnel_url', '')
    return f"Tunnel URL: {url}" if url else "No Cloudflare tunnel active"


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
    Get Google Drive integration status: authentication, folder access,
    service account email, and tunnel URL publication state.
    """
    data = _get('/api/gdrive/status')
    if not data.get('configured'):
        return "Google Drive not configured (ENABLE_GDRIVE=false)"
    lines = ["Google Drive Status:"]
    lines.append(f"  Account: {data.get('account_email', '?')}")
    lines.append(f"  Authenticated: {data.get('authenticated', False)}")
    folder = data.get('folder_name', data.get('folder_id', '?'))
    lines.append(f"  Folder: {folder}")
    lines.append(f"  Accessible: {data.get('folder_accessible', False)}")
    if data.get('folder_error'):
        lines.append(f"  Error: {data['folder_error']}")
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
