---
name: Access methods available to Claude on this machine
description: Clarifies that Claude has multiple ways to interact with the gateway, not just MCP
type: feedback
---

Claude runs on the same machine as the gateway and has multiple access methods available:

1. **MCP tools** (`gateway_mcp.py`) — preferred for gateway control (status, PTT, TTS, tuning, logs, etc.). 19 purpose-built tools that talk to the gateway's HTTP API on port 8080.
2. **HTTP/TCP directly** — Claude can call the gateway's HTTP API on port 8080 directly via bash/curl without going through MCP.
3. **Filesystem** — Claude can read and edit source files directly (`radio_gateway.py`, config files, logs on disk, etc.).
4. **Shell** — Claude can run any bash command on the machine.

**Why:** The user clarified this after Claude incorrectly stated it "only uses MCP." MCP is the default tool for gateway control, but it is not the only option.

**How to apply:** When choosing how to interact with the gateway or investigate an issue, pick the most appropriate method. MCP for control operations, filesystem/shell for deeper investigation, source code analysis, or tasks not covered by MCP tools.
