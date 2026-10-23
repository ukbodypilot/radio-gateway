# Claude Instructions -- Radio Gateway

## Memory

Claude Code's auto-memory path is derived from the working directory:
`~/.claude/projects/<slugified-cwd>/memory/`. For this repo at the
default clone location that resolves to
`~/.claude/projects/-home-user-Downloads-radio-gateway/memory/`, but if
the repo is cloned anywhere else the path updates automatically.

At the end of every session, and whenever a significant bug or pattern
is found, update the memory files:
- `<auto-memory-path>/MEMORY.md` — concise project overview (keep under 200 lines)
- `<auto-memory-path>/bugs.md` — bug history

Also mirror the updated files into `.claude/memory/` inside this project
directory. IMPORTANT: this repo is PUBLIC, so `.claude/memory/` is gitignored
and is NOT committed here. The memory is version-controlled in the PRIVATE repo
`ukbodypilot/claude-notes` (folder `radio-gateway/`). Never put secrets
(passwords, keys, tokens) in memory files regardless.

### Moving to a new machine
Clone wherever you like (this repo is path-agnostic; the installer
substitutes paths at install time). Memory lives in the PRIVATE repo
`ukbodypilot/claude-notes` (folder `radio-gateway/`), NOT in this public repo.
Seed both the gitignored mirror and the auto-memory path from there:
```bash
git clone git@github.com:ukbodypilot/claude-notes.git ~/claude-notes
MEM_DIR="$HOME/.claude/projects/$(pwd | sed 's|/|-|g')/memory"
mkdir -p .claude/memory "$MEM_DIR"
cp ~/claude-notes/radio-gateway/* .claude/memory/
cp ~/claude-notes/radio-gateway/* "$MEM_DIR/"
```
To push memory changes back: copy `.claude/memory/*` into
`~/claude-notes/radio-gateway/` and commit/push that private repo.

#### Do NOT mix project and machine memory (`MEMORY.md` collision)

`claude-notes` holds two different indexes, both named `MEMORY.md`:

| Scope | claude-notes path | Live source |
|-------|-------------------|-------------|
| **Radio Gateway project** (`# Radio Gateway — Project Memory`) | `radio-gateway/MEMORY.md` | `.claude/memory/` + `<auto-memory-path>/` |
| **Machine-wide** (`# Machine: user-optiplex3020`) | `memory/MEMORY.md` | `~/.claude/projects/-home-user/memory/` |

The copy commands above are only safe while `radio-gateway/MEMORY.md` is the
*project* index. On 2026-07-31 the machine-wide index had been copied over it,
so `cp .claude/memory/* ~/claude-notes/radio-gateway/` showed up as an 84-line
deletion — it would have wiped the machine notes, and the seeding command in
the other direction would have pushed the wrong index into every new clone.

**Before running either copy, check `head -1` on both `MEMORY.md` files and
confirm each says what the table above expects.** Individual non-index memory
files may legitimately exist in both places (9 of the 10 currently shared
filenames are byte-identical); `MEMORY.md` is the only genuine collision.

### Syncing gateway_config.txt between machines
`gateway_config.txt` is NOT in the repo. If missing, ask the user for the source machine's
IP/hostname and username, then fetch it:
```bash
scp user@source-ip:$(pwd)/gateway_config.txt .
```

## Project Rules
- `gateway_config.txt` is in `.gitignore` -- NEVER commit it (repo is public; it contains stream keys and passwords)
- NEVER commit Broadcastify credentials (STREAM_PASSWORD, STREAM_MOUNT) or any other secrets
- To sync config between machines: copy the file manually (scp/rsync) -- do NOT commit it
- Never commit the `bak/` directory
- Only commit when the user explicitly asks
- Never auto-push

## Bounded output when diagnosing
Every command you run here is re-sent to the model on **every subsequent turn** of the
session, so one unbounded dump is not paid for once -- it is paid for again and again.
A single 9-day manager session burned ~68M tokens moving ~174k tokens of real content.

- `journalctl -u <unit> -n 50 --no-pager` -- never bare `journalctl`, never `-f` here
- `tail -n 100 logs/endpoints/<name>.log` -- never `cat` a log file
- Pipe wide output through `head`/`grep` before it reaches you; count with `wc -l`
  rather than printing the thing you are counting
- `git log --oneline -20`, not `git log`
- If a check genuinely needs a big file, grep it for the answer instead of reading it

When a session has been going a long time and the current problem is unrelated to the
earlier one, `/clear` rather than carrying the whole history forward.

## Tests and CI
- `python3 tests/run_all.py` runs every `tests/test_*.py` in its own process;
  `python3 tests/run_all.py ptt config` runs only names containing those words.
  Each test is a plain script with a PASS/FAIL printer, no pytest.
- Missing hardware libraries (hidapi, pyaudio, pymumble, opuslib) are stubbed by
  `tests/_hwstubs.py`, so the suite runs off the box. Off the box it needs
  `pip install numpy "mcp<2" edge-tts gtts` plus ffmpeg and lame.
- `.github/workflows/ci.yml` runs `ruff check .` (config in `ruff.toml`) and
  `tests/run_all.py` on every push. Keep both green before committing.

## Code conventions (since 5.0.0)
- **Config keys**: every key has its default and type in `config_schema.py`
  `DEFAULTS`, and is either on the web form (`LAYOUT`) or listed in
  `NOT_IN_FORM` with a reason. Also add it to `examples/gateway_config.txt`.
  `tests/test_config_schema.py` fails if any of these disagree. `Config` sets
  every `DEFAULTS` key as an attribute, so a `getattr(cfg, KEY, fallback)`
  fallback is never used and only drifts from the schema; read `cfg.KEY`.
- **The config form edits the file in place** (`WebConfigServer._save_config`):
  comments, unknown keys and `[plugin.instance]` sections survive a save.
- **Caught exceptions**: never a bare `except: pass`. Use
  `except Exception: log_exc()` (`log_util.py`), which logs once per call site
  per 5 minutes.
- **Hardware imports** are optional at import time; `hw_deps.require()` in
  `main()` stops the service with install hints. Don't `sys.exit` on import.
- **WebSocket framing** exists once, in `gateway_link.py` (`ws_encode`,
  `ws_read_frame`, `ws_read_message`), shared by the browser handlers, the
  `/ws/link` bridge and the link client. `tests/test_ws_frames.py` covers it.
- **PTT**: every manual key path goes through `PttTimeOutTimer`
  (`gateway_link.py`, `PTT_TIMEOUT_SECONDS`). A new PTT path must too.
- **MCP tools**: the tool count in README.md, docs/mcp.md and docs/index.md is
  checked by `tests/test_mcp_tool_registry.py`; update all three.

## Mixer v2.0 Architecture (COMPLETE)
**Reference docs:**
- `docs/mixer-v2-design.md` -- architecture reference (bus types, plugin model, routing, API)
- `docs/history/mixer-v2-progress.md` -- development history, decisions log, test results

The v2.0 architecture uses bus-based audio routing with all radios as plugins:
- **4 bus types:** Listen, Solo, Duplex Repeater, Simplex Repeater
- **Local plugins** in `plugins/` (auto-discovered): TH-9800, SDR, packet, USRP/USRP2.
  D75, KV4P and IC-7100 are **link endpoints** (`tools/*_link_plugin.py`)
- **Sources own their processing** (gate/HPF/LPF/notch/gain) -- busses route clean PCM
- **Ducking is per-bus, priority-based** -- no hardcoded source name rules
- **All sinks gated by routing connections** -- visual Drawflow node editor
- **BusManager** tick thread (50 ms, SCHED_RR) does all mixing and sink delivery and
  owns `gc` (disabled, collected on the tick). The old `audio_transmit_loop` runs at
  normal priority and only applies queued PTT, feeds the MP3 encoder and the trace.
- **One bus per sink** -- `routing_rules.py` rejects a second bus into the same sink

## Gateway Link (duplex audio + command protocol)
- See `docs/gateway_link.md` for architecture, protocol, plugin system, and roadmap
- See `CHANGELOG.md` for project-wide release history
- Many endpoints at once, over TCP :9700 or WebSocket `/ws/link` (Cloudflare tunnel)
- Endpoint plugins: audio, aioc (built in), d75, kv4p, ic7100 (`tools/*_link_plugin.py`)
- Config: `ENABLE_GATEWAY_LINK`, `LINK_PORT` (default 9700), `PTT_TIMEOUT_SECONDS`

## AllStarLink (USRP bridge) — v4.0
- See `docs/allstar_bridge.md` for the bridge-node (ASL3 container) setup + gateway-side usage
- `plugins/usrp.py` — in-gateway USRP/DVSwitch plugin; `/usrp` panel for runtime node connect/disconnect via the bridge node's AMI
- Routing UI, `/routing/levels`, `_get_radio_plugin`, and `web_routes` dispatch are now generic over `_external_plugins` — discovered plugins become routable sources/sinks automatically
- Config: `ENABLE_USRP`, `USRP_REMOTE_HOST/PORT`, `USRP_LISTEN_PORT`, `USRP_NODE`, `USRP_AMI_*`
