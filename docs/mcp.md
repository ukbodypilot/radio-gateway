# MCP Server

`gateway_mcp.py` is a stdio-based [MCP](https://modelcontextprotocol.io) server. It gives Claude (or any MCP-compatible AI client) full control of the gateway via its HTTP API. **173 tools** across status, radios, routing, transcription, packet, fleet management, and more.

The Telegram bot, the Fleet Manager's hourly/daily Claude runs, and the voice control page all use these tools internally — the gateway itself reads its own state through this surface.

## How it's used

In Claude Code or any MCP client, the server is registered in `.mcp.json` at the project root. Enable in Claude Code settings:

```json
{ "enableAllProjectMcpServers": true }
```

The MCP server is launched as a child process of the MCP client (Claude Code, voice page, Telegram bot). Restarting the radio-gateway service does **not** restart the MCP server — they're separate processes connected by HTTP.

## Tool categories

| Category | Tools |
|----------|-------|
| **Status** | `gateway_status`, `sdr_status`, `cat_status`, `system_info`, `d75_status`, `ic7100_status`, `kv4p_status`, `telegram_status`, `gps_status`, `cloudflare_status` |
| **Radio TX** | `radio_ptt`, `radio_tts`, `radio_cw`, `radio_ai_announce`, `radio_set_tx`, `radio_get_tx` |
| **TH-9800** | `radio_frequency` |
| **TH-D75** | `d75_command`, `d75_frequency`, `d75_memscan` |
| **IC-7100** | `ic7100_command`, `ic7100_frequency`, `ic7100_vfo`, `ic7100_memory_recall`, `ic7100_memory_mode`, `ic7100_call_channel`, `ic7100_memory_store`, `ic7100_memory_clear`, `ic7100_memory_read`, `ic7100_memory_to_vfo` |
| **KV4P HT** | `kv4p_status`, `kv4p_command` |
| **AllStar / USRP** | `usrp_nodes`, `usrp_status`, `usrp_connect`, `usrp_disconnect`, `usrp_disconnect_all`, `usrp_links`, `usrp_node_stats` |
| **SDR** | `sdr_tune`, `sdr_single_tune`, `sdr_restart`, `sdr_stop`, `sdr_set_mode`, `sdr_add_channel`, `sdr_remove_channel` |
| **Routing** | `routing_status`, `routing_levels`, `routing_connect`, `routing_disconnect`, `bus_create`, `bus_delete`, `bus_mute`, `bus_rename`, `sink_mute`, `bus_toggle_processing`, `set_gain`, `bus_set_denoise_atten`, `bus_set_denoise_engine`, `bus_set_denoise_mix`, `bus_set_denoise_bypass`, `bus_set_delay`, `speaker_mode`, `mixer_control` (legacy per-source mixer) |
| **Loop recorder** | `loop_recorder_status`, `loop_recorder_toggle`, `loop_recorder_retention`, `loop_recorder_summary`, `loop_recorder_activity`, `loop_recorder_export`, `loop_recorder_delete_all`, `loop_recorder_archive_all`, `loop_recorder_download_all`, `loop_playback_control`, `test_loop_toggle` |
| **Transcription** | `transcription_status`, `transcription_workers`, `transcription_config`, `transcription_log_query`, `transcription_log_recent`, `transcription_search` |
| **Repeaters / GPS** | `nearby_repeaters`, `repeater_info`, `repeater_tune`, `repeater_refresh`, `gps_status`, `gps_set_position`, `gps_switch_mode` |
| **Packet / Winlink** | `packet_status`, `packet_mode`, `packet_decoded`, `packet_aprs_stations`, `packet_send_aprs`, `packet_aprs_beacon`, `packet_log`, `packet_bbs_connect`, `packet_bbs_disconnect`, `packet_bbs_send`, `packet_bbs_buffer`, `packet_force_audio`, `packet_set_endpoint`, `winlink_compose`, `winlink_connect`, `winlink_gateways`, `winlink_messages`, `winlink_read`, `winlink_log` |
| **Gateway link / endpoints** | `link_endpoint_status`, `link_endpoint_command`, `endpoint_ping`, `endpoint_reboot`, `endpoint_battery`, `endpoint_version`, `endpoint_ssh`, `endpoint_logs` |
| **Automation** | `automation_status`, `automation_history`, `automation_reload`, `automation_trigger`, `automation_scheme_read`, `automation_scheme_edit` |
| **Fleet Manager** | `manager_status`, `manager_reports`, `manager_doc_read`, `manager_doc_write`, `manager_toggle`, `manager_config`, `manager_ack`, `manager_run` |
| **Smart Announce** | `smart_announce_status`, `smart_announce_trigger` |
| **Broadcastify / streaming** | `broadcastify_status` |
| **BGM / announcer** | `bgm_status`, `bgm_control`, `announcer_status`, `announcer_configure` |
| **TTS / soundboard** | `tts_engine_status`, `tts_engine_set`, `soundboard_categories`, `soundboard_set_categories`, `soundboard_refresh` |
| **Relay / GPIO** | `relay_status`, `relay_charger_toggle` |
| **ADS-B / Pi-hole** | `adsb_status`, `pihole_status` |
| **Metrics** | `metrics_list`, `metrics_query` |
| **Recordings** | `recordings_list`, `recordings_delete` |
| **Cloud / GDrive** | `gdrive_status`, `gdrive_list_files`, `gdrive_publish_tunnel`, `cloudflare_status`, `tunnel_link_url`, `voice_view`, `voice_status`, `voice_send` |
| **System / Diag** | `gateway_logs`, `gateway_restart`, `gateway_key`, `audio_trace_toggle`, `stream_trace_toggle`, `stream_trace_read`, `trace_status`, `bus_sink_stats`, `bus_source_stats`, `config_read`, `process_control`, `processes_status`, `usbip_status` |
| **Telegram** | `telegram_reply`, `telegram_status`, `telegram_logs` |
| **TH-9800 CAT recovery** | `cat_serial_status`, `cat_reconnect`, `cat_serial_connect`, `cat_setup_radio` |

## Architecture

The gateway exposes its HTTP API on `:8080` (web UI port). The MCP server is a thin shim that:

1. Receives tool calls from the MCP client over stdio
2. Translates each to an HTTP call against `localhost:8080`
3. Returns the parsed JSON response back over stdio

This split lets multiple MCP clients (Claude Code, Telegram bot, voice page, Fleet Manager) all talk to the same gateway concurrently — they each spawn their own `gateway_mcp.py` instance but all converge on the single HTTP API.

## Adding a tool

Tools live in `mcp_server/tools/` — one module per domain. Each module imports the shared `mcp` instance and uses `@mcp.tool()` decorator registration:

```python
# mcp_server/tools/mymodule.py
from mcp_server.server import mcp, _get, _post

@mcp.tool()
def my_new_tool(arg1: str) -> str:
    """
    What this does. Be specific about side effects
    (e.g. "keys the transmitter — radio will transmit").
    """
    result = _post('/some/endpoint', {'arg1': arg1})
    if result.get('ok'):
        return f"Done: {result.get('msg', 'OK')}"
    return f"Failed: {result.get('error', 'unknown')}"
```

Then register the module in `mcp_server/__init__.py`:

```python
from mcp_server.tools import (  # noqa: F401
    ...,
    mymodule,
)
```

The tool's docstring becomes what the LLM reads when deciding whether to call it.

## Source pointers

- [`gateway_mcp.py`](../gateway_mcp.py) — entry point (`mcp_server.run()`)
- [`mcp_server/server.py`](../mcp_server/server.py) — shared `mcp` instance, `_get`/`_post` HTTP helpers, config loader
- [`mcp_server/tools/`](../mcp_server/tools/) — one module per tool category:
  - `control.py` — status, radio TX, broadcastify, smart announce, relay, ADS-B, automation, system
  - `radios.py` — TH-9800, D75, IC-7100, KV4P, processes, Telegram
  - `usrp.py` — AllStar/USRP node control
  - `routing.py` — bus mixer + routing
  - `fleet.py` — endpoint management, packet, Winlink, stream trace, automation schemes
  - `link.py` — gateway link endpoints
  - `loop_recorder.py` — loop recorder + playback
  - `transcription.py` — transcription engine + log search
  - `cloud.py` — Cloudflare, GDrive, voice relay
  - `repeaters.py` — repeater directory + GPS
  - `metrics.py` — Prometheus metrics
- [`.mcp.json`](../.mcp.json) — Claude Code registration
- HTTP endpoints called by tools live in `web_routes_get.py` and `web_routes_*.py` (per-domain handlers)
