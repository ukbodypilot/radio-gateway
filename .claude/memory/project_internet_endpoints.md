---
name: Internet Endpoint Connectivity
description: WIP — connecting link endpoints over the internet via CF tunnel + Google Drive discovery
type: project
originSessionId: 3e10387f-d7fa-4569-a8af-a5999f7014cd
---
## Status: Gateway side DONE, endpoint side NOT YET IMPLEMENTED

### What's built (gateway side)
- `/ws/link` WebSocket bridge in web_routes_stream.py — proxies WS to TCP:9700
- Google Drive `tunnel_url.json` auto-published on startup + URL change
- `/api/tunnel/link-url` returns current wss:// URL
- CF tunnel health check with HTTP probe + URL refresh + Drive publish

### What's NOT built (endpoint side)
- WebSocket transport in GatewayLinkClient (gateway_link.py)
- Fallback connection flow: LAN TCP → WS via Drive-discovered URL
- `--gdrive-credentials` / `--gdrive-folder-id` CLI args for link_endpoint.py
- settings.json tunnel URL caching on endpoint
- Exponential backoff on WS reconnect

### Design decisions
- Gateway is authoritative — only it requests new CF URLs
- Endpoints only READ tunnel_url.json from Drive, never write
- rclone used for Drive access (service accounts have zero quota on personal Google)
- Pi endpoints need rclone installed + rclone.conf with OAuth token deployed
- Same rclone.conf works on gateway and all endpoints (read-only for endpoints)

### Key files
- `gdrive.py` — rclone-based Drive client
- `web_routes_stream.py` — WS link bridge (handle_ws_link)
- `gateway_core.py` — _publish_tunnel_url
- `gateway_link.py` — needs WebSocket transport (TODO)
- `tools/link_endpoint.py` — needs Drive URL discovery + WS fallback (TODO)
