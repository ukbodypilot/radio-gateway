# Configuration Reference

Every config key the gateway reads from `gateway_config.txt`. **The authoritative source is [`examples/gateway_config.txt`](../examples/gateway_config.txt)** — that file has the latest keys, default values, and per-key comments. This page is an index pointing at it and grouping related keys by feature.

## Where the file lives

```
<gateway-dir>/gateway_config.txt        # your local config (gitignored — has secrets)
<gateway-dir>/examples/gateway_config.txt   # template / docs (in the repo)
```

The installer copies the example to `gateway_config.txt` if one doesn't exist. The example is heavily commented — read it top-to-bottom once, then come back when you need to change something.

## Fresh-install minimums

**Required** (gateway won't connect):

| Section | Key | Purpose |
|---------|-----|---------|
| `[mumble]` | `MUMBLE_SERVER` | Hostname/IP of your Mumble server |
| `[mumble]` | `MUMBLE_PORT` | Default 64738 |
| `[mumble]` | `MUMBLE_USERNAME` | Display name on Mumble |
| `[mumble]` | `MUMBLE_PASSWORD` | Empty if server has no password |

**Recommended** (key optional features):

| Section | Key | Purpose |
|---------|-----|---------|
| `[streaming]` | `STREAM_*` | Broadcastify feed key |
| `[telegram]` | `TELEGRAM_BOT_TOKEN` + `_CHAT_ID` | Telegram bot control |

## Section index

| Section | Covers |
|---------|--------|
| `[startup]` | Startup verbosity + logging |
| `[mumble]` | Mumble client connection + servers |
| `[radio]` | AIOC USB radio (TH-9800 default) |
| `[audio]` | Sample rate, chunk size, channel count |
| `[levels]` | Per-source gain, master volumes |
| `[ptt]` | PTT method, hold times, talkback |
| `[vad]` | Silero VAD threshold, hold, hysteresis |
| `[vox]` | Legacy dB-envelope VOX (most paths use VAD now) |
| `[processing]` | High-pass / low-pass / notch / noise gate / RNNoise |
| `[switching]` | SDR ducking, priority, signal threshold |
| `[remote]` | Remote audio TX/RX link (full duplex) |
| `[announce]` | Network announcement input (port 9601) |
| `[playback]` | Playback source — file announcements, soundboard (see [Soundboard categories](#soundboard-categories)) |
| `[tts]` | Text-to-speech — `TTS_ENGINE` (`kokoro`/`edge`/`gtts`), voice selection, volume, speed |
| `[speaker]` | Local speaker output mode (virtual/auto/real) |
| `[streaming]` | Broadcastify / Icecast feed |
| `[echolink]` | EchoLink integration (legacy, TheLinkBox) |
| `[allstar]` | AllStarLink bridge via USRP — `ENABLE_USRP`, `USRP_REMOTE_HOST/PORT`, `USRP_LISTEN_PORT`, `USRP_NODE`, `USRP_AMI_*` (see [allstar_bridge.md](allstar_bridge.md)) |
| `[relay]` | USB relay control (radio power, antenna switches) |
| `[smart]` | AI-generated smart announcements |
| `[telegram]` | Telegram bot for remote control |
| `[web]` | Web UI port, theme, auth |
| `[ddns]` | DDNS updater (No-IP, Dynu) |
| `[cat]` | TH-9800 CAT control startup commands |
| `[kv4p]` | KV4P HT — frequency, squelch, CTCSS, levels, per-source processing |
| `[d75_processing]` | Per-source processing overrides for the Kenwood TH-D75 |
| `[packet]` | Packet TNC / APRS / Winlink (see [packet-radio.md](packet-radio.md)) |
| `[automation]` | Scheduled automation engine — window, task cap, repeater list |
| `[adsb]` | ADS-B map page — `ADSB_PORT` (dump1090-fa's own HTTP port) |
| `[usbip]` | Attach USB devices from another machine over USB/IP |
| `[advanced]` | Tunable thresholds, watchdogs, debug |

### Deprecated keys

These still appear in older `gateway_config.txt` files. Nothing reads them —
setting them has no effect. They are commented out in the template.

| Key | Why | Use instead |
|-----|-----|-------------|
| `START_TH9800_CAT` | Was read by `start.sh`, which no longer exists | — |
| `TH9800_CAT_HEADLESS` | Same | — |
| `SDR2_PRIORITY` | Superseded by a single ordering key | `SDR_PRIORITY_ORDER` in `[sdr1]` |

## Feature-specific keys not in the .ini

Some features have keys that live OUTSIDE `gateway_config.txt` because they're host-specific or operationally tuned and shouldn't follow the user across machines. See the per-feature docs:

- **Transcription pool** — `TRANSCRIBE_MODE`, `TRANSCRIBE_REMOTE_URLS`, `TRANSCRIBE_SPLIT_THRESHOLD_SECS`, `TRANSCRIBE_ALLOW_WORKER_REGISTRATION` (default true), `TRANSCRIBE_WORKER_TTL_SECS` (default 90) plus the live UI settings in `.transcribe_settings.json`. Workers started with `--gateway` register themselves, so `TRANSCRIBE_REMOTE_URLS` is only for pinning a worker explicitly. See [transcription-pool.md](transcription-pool.md).
- **Fleet Manager** — task docs are `hourly.md` / `daily.md` / `SYSTEM_MANIFEST.md`; engine state in `manager_state.json`; runtime reports in `manager_reports.jsonl`. See [fleet-manager.md](fleet-manager.md).
- **Routing config** — bus topology lives in `routing_config.json`, edited via the visual editor at `/routing`. Not in the .ini.
- **Loop recorder retention** — per-bus, edited in the routing UI.

## Value gotchas

The loader is a hand-rolled line parser, not `configparser`. What that means
for values:

- **`#` starts a comment only at the start of a value or after whitespace.**
  `CALLSIGN = N0CALL   # my note` comments; `PACKET_APRS_SYMBOL = /#` does
  not — the `#` is part of the symbol. Before v4.6.1 any `#` was treated as a
  comment, which made `/#` (the standard APRS digipeater symbol, and the code
  default) impossible to set: it silently read back as `/`.
- **Quoting makes a value literal.** Everything between the opening quote and
  the matching closing quote is the value, and anything after it is discarded
  — so `PACKET_APRS_COMMENT = "Radio # Gateway"  # a note` yields
  `Radio # Gateway`. Use this when a value contains a `#` after a space. An
  unterminated quote is left as-is rather than swallowing the rest of the line.
- **Text inside `{braces}` is exempt** from comment stripping, nesting
  included — smart-announce prompts use `#` inside brace expressions.
- **An empty value means "use the default"**, not "use empty string" — a key
  whose value is blank after comment-stripping is skipped entirely. This is
  also what makes blanking a secret in the web config form mean *keep the
  stored value*.

`[section]` headers are **cosmetic**. The parser skips them, so a key works
wherever you put it; the sections exist purely to group things for humans.

Both halves live in [`config_format.py`](../config_format.py) so they cannot
drift: `parse_config_value()` reads, and `format_config_value()` writes,
quoting a value only when the reader would not give it back unchanged. That
matters because the web config form **rewrites the whole file** on save — a
reader fix alone would still lose data the next time you pressed Save.
Pinned by
[`tests/test_config_value_parsing.py`](../tests/test_config_value_parsing.py),
which asserts the round trip is lossless.

## Defaults & types

The example file has the canonical default for every key as its initial value, and a comment explaining what the key does. When in doubt: read the example. It's the only place that won't drift out of sync with the parser.

Where a key is intentionally blank (e.g. `MUMBLE_PASSWORD =`), the gateway falls back to a sensible default — see the loader in `Config.load_config()` in [`radio_gateway.py`](../radio_gateway.py) for the resolution order.

## Adding a new config key

The convention used across the codebase:

```python
my_value = getattr(self.config, 'MY_NEW_KEY', 'fallback_default')
```

So a new key works the moment you read it; the example file documents it for users. Steps:

1. Use the key in code with a `getattr(...)` + default.
2. Add the key to `examples/gateway_config.txt` in the appropriate section with a comment.
3. If it's a feature-specific tunable that isn't safe to ship as a default (host-specific path, sensitive setting), document its presence here and in the relevant feature doc.

That's the whole pattern. No schema, no migrations.

## Soundboard categories

Playback keys 1–9 that have no local file are auto-filled with random
royalty-free sound effects, re-rolled by the **Refresh** button on `/controls`
and `/dashboard/operate`.

`SOUNDBOARD_CATEGORIES` restricts which categories the draw comes from.
Comma-separated; prefix a name with `-` to exclude it instead. Blank means
every category.

```ini
# only these
SOUNDBOARD_CATEGORIES = boing, fart, scream, squeak, wrong

# everything except these
SOUNDBOARD_CATEGORIES = -animals, -applause, -arcade
```

The same setting can be changed without editing the file via the `/controls`
Cats picker or the `soundboard_set_categories` MCP tool.

Behaviour:

- **Live** — read at refresh time, so saving the config page applies on the very
  next Refresh with no gateway restart.
- **Forgiving** — unknown names are ignored with a warning that lists the valid
  ones. If the filter ends up matching nothing (all names invalid, or everything
  excluded) the full pool is used rather than leaving the soundboard silent.
- **Case- and space-insensitive**; blank tokens and newlines are ignored.
- Exclusions win over inclusions, so `boing, -boing` yields no `boing`.
- A filter narrower than the number of empty slots fills what it can and logs a
  hint; e.g. `scream` has only 7 sounds for 9 slots.

### Picking categories in the GUI

`/controls` and `/dashboard/operate` have a **Cats** button next to Refresh. It
opens a tick-list of every category with its sound count, a running tally of how
many sounds the selection covers, and All / None shortcuts. Saving writes
`SOUNDBOARD_CATEGORIES` and applies on the next Refresh.

Ticking *everything* stores a blank value rather than a 31-name list, so a pool
that grows later is picked up automatically instead of being frozen to today's
categories. Ticking *nothing* also stores blank (= all) — a silent soundboard is
worse than an ignored filter — and the dialog says so before you save.

Endpoints: `GET /soundboard/categories` returns `categories` (name + count),
`selected`, `filter`, `pool_size`, `max_seconds` and `all`;
`POST` the same path with `{"categories": [...]}` to save.

### Clip length cap

`SOUNDBOARD_MAX_SECONDS` (default `15`, `0` disables) rejects clips longer than
the cap. The pool contains full-length music tracks — id 2474 is 72 s, id 489 is
50 s — which are useless as soundboard effects and were filling roughly a
quarter of the slots.

Enforcement is two-stage: `Content-Length` divided by the worst-case MP3 bitrate
(320 kbps) is a lower bound on duration, so oversized files are skipped without
downloading the body; anything that gets through is then measured with `ffprobe`
(5 s timeout) and deleted if it's over. Measured lengths are remembered in
`<playback-dir>/.soundboard_meta.json`, which lives *outside* `.cache` so the
Refresh button's cache wipe doesn't force the gateway to re-download clips just
to re-learn they were too long. A rejected pick is replaced, not skipped, so the
slots still fill.

| Category | Sounds | | Category | Sounds | | Category | Sounds |
|---|---|---|---|---|---|---|---|
| `animals` | 50 | | `bells` | 30 | | `laugh` | 19 |
| `arcade` | 45 | | `crowd` | 30 | | `click` | 15 |
| `funny` | 45 | | `drums` | 30 | | `laser` | 15 |
| `explosion` | 40 | | `horns` | 30 | | `water` | 15 |
| `applause` | 35 | | `whistles` | 30 | | `horror` | 14 |
| `game` | 35 | | `whoosh` | 30 | | `fart` | 12 |
| `impact` | 35 | | `monster` | 27 | | `squeak` | 11 |
| `transition` | 35 | | `buzzer` | 25 | | `boing` | 10 |
| `cartoon` | 20 | | `sirens` | 25 | | `wrong` | 9 |
| `cinematic` | 20 | | `notifications` | 20 | | `scream` | 7 |
| `swoosh` | 20 | | | | | | |

> The category label is ours, not Mixkit's — the download URL is built from the
> numeric sound id alone. 19 ids are deliberately filed under more than one
> category (id 2891 is under `boing`, `fart` **and** `funny`), so picking
> de-duplicates by id to stop one clip occupying two slots.
