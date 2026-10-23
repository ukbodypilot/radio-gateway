# Endpoint Log Collection — Design

**Status:** v1 + v2 shipped 2026-05-30.
**Motivation:** The DietPi running the D75 endpoint had no persistent journal. When the D75-side BT serial dropped, the `[D75-WD]` watchdog ticks were lost on every reboot, so there was no record of *why* the link plugin couldn't reach the radio across an extended outage. The link channel already exists between gateway and every endpoint — endpoints just don't use it for log shipping.

## Goals

- Capture endpoint stdout/stderr at the gateway, persistently.
- Survive endpoint reboot/crash — the exact case that bit us today.
- Zero new ports / no new auth surface (ride the link the gateway already trusts).
- Bounded memory on the endpoint and bounded disk on the gateway.
- No log loss in steady state; graceful drop under stress.

## Non-goals (for now)

- No structured logging / log-level filtering — lines are opaque text.
- No Loki / Elastic / external sink. Per-endpoint rotating files are fine for a fleet of <10 endpoints.
- No subprocess stdout capture. If the endpoint Popens something, that subprocess's stdout still goes to systemd's volatile journal as today. Out of scope for v1.

## Wire protocol

One new frame opcode on the existing `gateway_link` channel:

```
GatewayLinkProtocol.LOG = 0x06
```

Payload is a JSON object:

```json
{
  "type": "log",
  "lines": [
    {"ts": 1748630412.4, "stream": "stdout", "text": "[D75-WD] tick #42 serial=true rx=true"},
    {"ts": 1748630412.4, "stream": "stderr", "text": "[D75] Serial read error: ..."}
  ]
}
```

Batched: up to 1 second of lines per frame so frame rate stays around 1 Hz per endpoint regardless of log volume. Server dispatch lives alongside the existing `AUDIO` / `COMMAND` / `STATUS` / `REGISTER` / `ACK` branches in the same reader loops (raw TCP main reader + the two WebSocket readers).

## Endpoint side

Module `tools/log_shipper.py`:

- `TeeStream(orig, capture)` — file-like wrapper that delegates writes to `orig` (so local visibility is preserved — answers Q1: **tee, not replace**) and also splits lines through `capture(stream, text)`.
- `LogShipper(link_client, buf_max=1000)`:
  - Replaces `sys.stdout` and `sys.stderr` with `TeeStream`s at startup.
  - `collections.deque(maxlen=1000)` holds `(ts, stream, text)` tuples — overflow silently drops oldest (~200 KB RAM worst case).
  - Daemon flush thread wakes every 1.0s; if the link is up, ships everything in the deque as a single `P.LOG` frame.
  - On reconnect: ships the whole backlog (answers Q3: **yes backlog**). The value is "what happened just before the crash that killed the link." Cost is one bursty frame on each reconnect; acceptable.
  - Best-effort send: failed `_link._send(...)` re-queues the batch at the front of the deque and tries again next tick.
  - A batch is split into several LOG frames so each payload stays under the link protocol's 65535-byte limit. Before 5.0.0 an oversize batch (a reconnect backlog, typically) raised `struct.error` in the flush thread and stopped that endpoint's log shipping until it restarted.

Installed by `link_endpoint.py`'s `__main__` immediately after the `GatewayLinkClient` is constructed, before any plugin loads. That way the shipper captures every line including plugin setup logs.

## Gateway side

Module `core/endpoint_logs.py`:

- `EndpointLogStore(base_dir='logs/endpoints')`
  - Lazy per-endpoint file open: `logs/endpoints/<sanitized>.log`.
  - Sanitized = `[a-zA-Z0-9_-]+`; anything else → `_`. Prevents path traversal even though endpoint names come over the trusted link.
  - Rotation: `logging.handlers.RotatingFileHandler(maxBytes=5_000_000, backupCount=3)`. With 5 endpoints that's ~75 MB cap.
  - Line format: `2026-05-30T19:15:42 [stdout] <text>\n` — plain greppable text, timezone-naive local.
  - `append(endpoint_name, lines)` is the only write path; internal `threading.Lock` serializes appends per file handle.
- `tail(endpoint_name, lines=50)` — returns last N lines of the file as a string. Used by both the MCP tool and the web UI.

Wired into `GatewayLinkServer` via a new constructor kwarg `on_log_lines=callback`. `setup_gateway_link()` in `gateway_setup.py` instantiates the store and passes `store.append` as the callback.

## MCP tool

Added to `mcp_server/tools/fleet.py` (fleet is where all other endpoint-related tools live):

```
endpoint_logs(endpoint: str, lines: int = 50, stream: str = 'all') -> str
```

Calls a new gateway HTTP route `/api/endpoint_logs?name=X&lines=N&stream=S` which returns the tail as plain text. Single tool (answers Q2: **one tool, not split**).

## Web UI (v2)

New page at `/endpoints/logs`:

- Sticky header with tabs — one tab per registered endpoint, plus an "all" tab.
- Live tail pane: monospace, last 500 lines, scrolled to bottom.
- 2 s polling against `/api/endpoint_logs?name=X&lines=500`. No SSE / WS needed — pattern matches existing UI pages like `/packet` and `/routing`.
- Reuses the existing auth + nav.

## Failure modes

| Scenario | Behavior |
|---|---|
| Gateway link down | Endpoint deque accumulates up to 1000 lines, then oldest drops. Lines carry their original `ts`, so on reconnect the gateway sees them in true order. |
| Endpoint floods stdout | Bounded deque protects RAM. Dropped lines are silent in v1 (could add `[N dropped]` marker if it becomes a real problem). |
| Gateway disk fills | Rotation cap (~75 MB for typical fleet) is the backstop. |
| Non-UTF-8 bytes | All decoded with `errors='replace'` (U+FFFD). |
| Endpoint reboots | New process starts with empty deque; the pre-restart log on disk at gateway is preserved (the whole point). |
| Gateway restarts | Existing rotating files survive untouched. Endpoint shipper's deque continues to fill; on gateway-link reconnect, it ships the backlog. |
| Two endpoints same name | The link server already rejects duplicate registrations, so no risk. |

## Security / privacy

No new trust boundary — the link is already authoritative for radio control. Log content may include BT MACs, internal IPs, and occasional CAT command bytes. Nothing more sensitive than what's already on that channel. The sanitized filename prevents directory traversal even if the endpoint name were ever attacker-controlled.

## Scope phases

- **v1** (~155 LOC): protocol opcode + endpoint shipper + gateway store + MCP tool + HTTP route. Validate over a week of normal operation.
- **v2** (~80 LOC): web UI page.
- **v3** (deferred — probably never needed): Loki / OpenSearch sink for cross-endpoint correlation queries.

## Decisions captured

1. **Tee, not replace** — local stdout still goes to the endpoint's own stdout (and from there to its systemd journal, if persistent). Shipped lines are a copy. Safer; cost is negligible.
2. **One MCP tool** — `endpoint_logs(name, lines=50, stream='all')`. Splitting into tail/search adds surface area we don't need yet.
3. **Backlog on reconnect** — endpoint dumps its buffered backlog as a single frame on first send after reconnect. The whole point of this feature is "what happened just before the link died"; not shipping the backlog would defeat that.
