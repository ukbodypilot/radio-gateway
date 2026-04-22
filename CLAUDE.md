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

Also mirror the updated files into `.claude/memory/` inside this
project directory so they travel with the repo.

Read `MEMORY.md` at the start of each session to restore context.

### Moving to a new machine
Clone wherever you like (this repo is path-agnostic; the installer
substitutes paths at install time). After cloning, seed the auto-memory
from the mirrored copy inside the repo:
```bash
MEM_DIR="$HOME/.claude/projects/$(pwd | sed 's|/|-|g')/memory"
mkdir -p "$MEM_DIR"
cp .claude/memory/* "$MEM_DIR/"
```

### Syncing gateway_config.txt between machines (Claude's responsibility)
`gateway_config.txt` is NOT in the repo. At the start of every session,
check whether it exists:
```bash
ls gateway_config.txt
```
If it is missing, ask the user for the source machine's IP/hostname and
username, then fetch it:
```bash
scp user@source-ip:$(pwd)/gateway_config.txt .
```
Do NOT proceed with gateway work until the config file is present — the
gateway will not run without it.

## Project Rules
- `gateway_config.txt` is in `.gitignore` -- NEVER commit it (repo is public; it contains stream keys and passwords)
- NEVER commit Broadcastify credentials (STREAM_PASSWORD, STREAM_MOUNT) or any other secrets
- To sync config between machines: copy the file manually (scp/rsync) -- do NOT commit it
- Never commit the `bak/` directory
- Only commit when the user explicitly asks
- Never auto-push

## Mixer v2.0 Architecture (COMPLETE)
**Reference docs:**
- `docs/mixer-v2-design.md` -- architecture reference (bus types, plugin model, routing, API)
- `docs/mixer-v2-progress.md` -- development history, decisions log, test results

The v2.0 architecture uses bus-based audio routing with all radios as plugins:
- **4 bus types:** Listen, Solo, Duplex Repeater, Simplex Repeater
- **4 radio plugins:** SDRPlugin, TH9800Plugin, D75Plugin, KV4PPlugin
- **Sources own their processing** (gate/HPF/LPF/notch/gain) -- busses route clean PCM
- **Ducking is per-bus, priority-based** -- no hardcoded source name rules
- **All sinks gated by routing connections** -- visual Drawflow node editor
- **BusManager** runs routing-configured busses alongside main loop

## Gateway Link (duplex audio + command protocol)
- See `docs/gateway_link.md` for architecture, protocol, plugin system, and roadmap
- See `CHANGELOG.md` for project-wide release history
- MVP: single endpoint, duplex audio, generic AudioPlugin
- Vision: all radios as plugins, gateway as mixer + protocol hub
- Config: `ENABLE_GATEWAY_LINK`, `LINK_PORT` (default 9700)
