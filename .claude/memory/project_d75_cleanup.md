---
name: D75 legacy code cleanup
description: Dead code to remove once D75 link endpoint is proven stable (~1 week after 2026-04-01)
type: project
---

After D75 link endpoint has been stable for ~1 week, remove legacy D75 code:

1. **`d75_plugin.py`** — entire file (old TCP proxy client, D75CATClient, D75AudioSource)
2. **`cat_client.py`** — D75CATClient class (check if anything else uses it first)
3. **`gateway_core.py`** — D75 plugin init block, all `self.d75_plugin` references
4. **`web_server.py`** — legacy `gw.d75_plugin` fallback paths in `/d75cmd`, `/d75status`, `/d75memlist`, `_get_routing_status`, `_get_plugin_by_id`, routing levels
5. **`bus_manager.py`** — `gw.d75_plugin` references in `_get_radio_plugin`, `_get_source`
6. **Config** — can remove `ENABLE_D75`, `D75_HOST`, `D75_PORT`, `D75_AUDIO_PORT` keys from examples

Also consider:
- Make the D75 link endpoint auto-reconnect when gateway restarts (currently exits on disconnect)
- Create a systemd service for the endpoint on 192.168.2.134 so it starts on boot
- Remove `scripts/remote_bt_proxy.py` TCP server code (CATServer, AudioServer classes) — only needed for old protocol

**Why:** Dead code hides bugs and adds confusion. But keep it until the link endpoint is proven reliable.

**How to apply:** Wait until ~2026-04-08, then do one clean removal commit.
