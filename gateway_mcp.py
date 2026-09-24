#!/usr/bin/env python3
"""
Radio Gateway MCP Server (entry point)

Exposes the radio gateway as a set of AI-callable tools via the Model Context
Protocol (MCP) stdio transport.  Claude Code (or any MCP client) can load this
server and control the gateway without API keys.

Usage (stdio, local):
    python3 gateway_mcp.py

Claude Code configuration (.mcp.json at the repo root):
    {
      "mcpServers": {
        "radio-gateway": {
          "command": "python3",
          "args": ["./gateway_mcp.py"],
          "cwd": "."
        }
      }
    }

The implementation lives in the ``mcp_server`` package:
  - ``mcp_server/server.py`` — FastMCP instance + HTTP helpers + config loader + GW_ROOT
  - ``mcp_server/tools/control.py``       — gateway/SDR/radio TX/recordings/logs/automation/audio-trace/telegram/ADS-B/USB-IP
  - ``mcp_server/tools/radios.py``        — TH-9800 / D75 / KV4P / IC-7100 / processes / mixer / config / process control
  - ``mcp_server/tools/routing.py``       — audio routing: bus/sink wiring, mute, gain, processing filters, denoise tuning, bus delay
  - ``mcp_server/tools/fleet.py``         — endpoint SSH / packet + BBS / Winlink / stream-trace / sink-stats / scheme mgmt / endpoint battery
  - ``mcp_server/tools/transcription.py`` — transcription status, config, log query
  - ``mcp_server/tools/link.py``          — link endpoint status + commands
  - ``mcp_server/tools/loop_recorder.py`` — loop recorder status, retention, export, archive
  - ``mcp_server/tools/cloud.py``         — Cloudflare tunnel / Google Drive
  - ``mcp_server/tools/repeaters.py``     — repeater lookup, tune, refresh
  - ``mcp_server/tools/metrics.py``       — Prometheus metric discovery + PromQL queries
  - ``mcp_server/tools/usrp.py``          — AllStar USRP node connect/disconnect/stats (usrp + usrp2)
  - ``mcp_server/tools/manager.py``       — Fleet Manager: scheduled check status, reports, docs, run control

Every module is imported by ``mcp_server/__init__._register_all_tools`` — the
@mcp.tool() decorator registers on import, so a new module is invisible until
it is added to that import list.

Tool modules must resolve repo files against ``server.GW_ROOT``, not their own
``__file__``: they sit two directories below the gateway root.

This file is intentionally a thin shim so the ``.mcp.json`` entry, tmux session
launchers, and any external scripts that spawn ``python3 gateway_mcp.py``
continue working without modification.
"""

from mcp_server import run


if __name__ == '__main__':
    run()
