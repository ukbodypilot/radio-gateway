---
name: Refactor Plan — PTT + File Split
description: Full plan for splitting radio_gateway.py into files and redesigning PTT
type: project
---

## Stable checkpoint
Git tag `v-stable-pre-refactor` on main branch (2026-03-20).
To restore a single file: `git checkout v-stable-pre-refactor -- radio_gateway.py`

## Why: PTT Architecture Problem
Current `TX_RADIO` is a single global — all audio goes to one radio.
No per-source routing. PTT methods scattered across 5 functions called from
set_ptt_state() routing on a single config value. User wants:
- Different functions routable to different radios (playback→TH9800, AI→KV4P, etc.)
- Each radio has its own PTT method — can't share a global PTT_METHOD
- AIOC complication: it's both audio interface AND PTT GPIO for TH-9800

## Step 1 — Split into files (mechanical, no behaviour change)
Target structure:
- `radio_gateway.py`  — main() + config loading + startup only
- `gateway_core.py`   — RadioGateway class, audio loops
- `audio_sources.py`  — all AudioSource subclasses
- `ptt.py`            — PTT controller classes (new, clean)
- `web_server.py`     — WebConfigServer + all HTTP handlers
- `web_dashboard.py`  — dashboard/radio/kv4p/d75 HTML+JS strings
- `smart_announce.py` — SmartAnnouncementManager + AI backends
- `cat_client.py`     — RadioCATClient, D75CATClient

**Why:** 21k lines in one file hides bugs and makes PTT refactor very hard.
Split first, then refactor into clean structure.

## Step 2 — Lockfile fix
start.sh: write PID to /tmp/gateway.lock, check on startup.
gateway Python: write PID on start, delete on exit.
**Why:** Multiple instances cause port conflicts and duplicate audio.

## Step 3 — Minor fixes
- Strip ANSI codes from email log dump (regex: `re.sub(r'\x1b\[[0-9;]*m', '', line)`)
- CW: log warning + skip unknown chars in generate_cw_pcm()
- Smart Announce: filter id=0 from activity display in web UI
- TCP_NODELAY: remove dead config key from docs/config examples

## Step 4 — PTT Refactor (the big one)
### New PTT controller classes (ptt.py)
```python
class RadioPTT:
    def key(self): ...
    def unkey(self): ...
    def is_keyed(self): ...

class TH9800PTT(RadioPTT):
    # Wraps AIOC GPIO / relay / software CAT !ptt based on PTT_METHOD
    # Owns RTS state — switches USB/Radio Controlled internally
    # No other code touches RTS

class D75PTT(RadioPTT):
    # CAT !ptt on / !ptt off (explicit, not toggle)

class KV4PPTT(RadioPTT):
    # kv4p-ht-python library serial PTT
```

### New per-source TX radio config
Replace single `TX_RADIO` with per-source keys:
```
MUMBLE_TX_RADIO = th9800
PLAYBACK_TX_RADIO = th9800
TTS_TX_RADIO = th9800
CW_TX_RADIO = th9800
SMART_ANNOUNCE_TX_RADIO = th9800
WEBMIC_TX_RADIO = th9800
```
Each source looks up its own radio and calls the right PTT object.

### PTT state tracking
Each RadioPTT tracks its own keyed state.
Remove shared `ptt_active` flag — replace with `any(r.is_keyed() for r in radios)`.
Status display shows per-radio PTT state.

### AIOC note
AIOC is connected to TH-9800. TH9800PTT handles the full sequence:
- AIOC GPIO: pause drain → set RTS Radio Controlled → GPIO → unkey → RTS USB Controlled → resume drain
- Software: CAT !ptt toggle with _software_ptt_on dedup
- Relay: relay set_state()
TH9800PTT constructor takes PTT_METHOD and wires to the right internal method.

## How to apply
Work on main branch (user preference — no separate branch).
Commit after each step completes and tests pass.
Tag each major step: `v-split-complete`, `v-lockfile`, `v-ptt-refactor`.
