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
| `[email]` | `EMAIL_ADDRESS` + `EMAIL_APP_PASSWORD` | Alert emails (stream outages, Fleet Manager, keyword hits) |

## Section index

| Section | Covers |
|---------|--------|
| `[startup]` | Startup verbosity |
| `[mumble]` | Mumble client connection + servers |
| `[radio]` | AIOC USB radio (TH-9800 default) |
| `[audio]` | Sample rate, chunk size, channel count |
| `[levels]` | Per-source gain, master volumes |
| `[ptt]` | PTT method, hold times, talkback, link auto-PTT, and the transmit time-out `PTT_TIMEOUT_SECONDS` (default 180 s, `0` = off; see below) |
| `[vad]` | Silero VAD threshold, hold, hysteresis |
| `[vox]` | Legacy dB-envelope VOX (most paths use VAD now) |
| `[processing]` | High-pass / low-pass / notch / noise gate / RNNoise |
| `[sdr1]` / `[sdr2]` | SDR receivers: device, ducking, boost, `SDR_PRIORITY_ORDER` |
| `[switching]` | SDR ducking, priority, signal threshold |
| `[remote]` | Remote audio TX/RX link (full duplex) |
| `[announce]` | Network announcement input (port 9601) |
| `[playback]` | Playback source — file announcements, soundboard (see [Soundboard categories](#soundboard-categories)) |
| `[tts]` | Text-to-speech — `TTS_ENGINE` (`kokoro`/`edge`/`gtts`), voice selection, volume, speed |
| `[speaker]` | Local speaker output mode (virtual/auto/real) |
| `[streaming]` | Broadcastify / Icecast feed (in-process ffmpeg encoder) |
| `[transcription]` | Transcription engine, pool and log — mostly file-only keys, see below |
| `[echolink]` | EchoLink integration (legacy, TheLinkBox) |
| `[allstar]` | AllStarLink bridge via USRP — `ENABLE_USRP`, `USRP_REMOTE_HOST/PORT`, `USRP_LISTEN_PORT`, `USRP_NODE`, `USRP_AMI_*` (see [allstar_bridge.md](allstar_bridge.md)) |
| `[relay]` | USB relay control (radio power, antenna switches) |
| `[smart]` | AI-generated smart announcements |
| `[email]` | Gmail SMTP alerting — `ENABLE_EMAIL`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `EMAIL_RECIPIENT`, `EMAIL_ON_STARTUP`, `ENABLE_ALERT_ENGINE` (raw Prometheus-threshold alerts; off by default) |
| `[manager]` | Fleet Manager runs — `MANAGER_CLAUDE_BIN`, `MANAGER_CLAUDE_MODEL`, `MANAGER_MAX_TURNS` |
| `[web]` | Web UI port, theme, auth, `WEB_CONFIG_HTTPS` (`false` / `self-signed` / `letsencrypt`, the last uses `DDNS_HOSTNAME`), Cloudflare tunnel |
| `[ddns]` | DDNS updater (No-IP, Dynu) |
| `[cat]` | TH-9800 CAT control startup commands |
| `[mumble-server-1]` / `[mumble-server-2]` | Optional local Mumble server instances |
| `[kv4p]` | KV4P HT — frequency, squelch, CTCSS, levels, per-source processing |
| `[d75_processing]` | Per-source processing overrides for the Kenwood TH-D75 |
| `[packet]` | Packet TNC / APRS / Winlink (see [packet-radio.md](packet-radio.md)) |
| `[automation]` | Scheduled automation engine — window, task cap, repeater list |
| `[adsb]` | ADS-B map page — `ADSB_PORT` (dump1090-fa's own HTTP port) |
| `[usbip]` | Attach USB devices from another machine over USB/IP |
| `[gps]` / `[repeater_db]` | GPS receiver; repeater directory |
| `[simplex]` | Simplex repeater bus timing |
| `[advanced]` | Tunable thresholds, watchdogs, debug, `LOOP_RECORD_KBPS` (loop recorder MP3 bitrate, default 48), `LOG_FILE_DAYS` (default 30) |
| `[kv4p.<name>]` | One section per KV4P link endpoint the gateway spawns (`[kv4p.vhf]`, `[kv4p.uhf]`, ...). Keys here belong to that instance only |

### Deprecated keys

These still appear in older `gateway_config.txt` files. Nothing reads them —
setting them has no effect. They are commented out in the template.

| Key | Why | Use instead |
|-----|-----|-------------|
| `START_TH9800_CAT` | Was read by `start.sh`, which no longer exists | — |
| `TH9800_CAT_HEADLESS` | Same | — |
| `SDR2_PRIORITY` | Superseded by a single ordering key | `SDR_PRIORITY_ORDER` in `[sdr1]` |
| `SDR_PRIORITY` | Retired (`config_schema.RETIRED`) | `SDR_PRIORITY_ORDER` |
| `SUPERVISE_DARKICE` | Retired with the external-DarkIce stream path | — (streaming is in-process) |
| `TELEGRAM_*` | The Telegram bot was removed in 5.0.0 | Email alerts, `[email]`. Delete the `[telegram]` block; it holds a bot token |
| `TRANSCRIBE_FORWARD_TELEGRAM` | Posted to a route that no longer existed | Keyword hits are emailed (`TRANSCRIPTION_ALERT_KEYWORDS`) |
| `MANAGER_RUN_MODE` | The Fleet Manager's tmux mode was removed; every run is a one-shot `claude -p` | — |

## Transmit time-out (`PTT_TIMEOUT_SECONDS`)

Every manual key path (the TH-9800 plugin's `_set_ptt`, and the PTT command to
any link endpoint: D75, KV4P, IC-7100, AIOC) is watched by one
`PttTimeOutTimer`. After `PTT_TIMEOUT_SECONDS` keyed (default 180) it forces
the radio unkeyed and logs `PTT time-out (180s) -- forcing unkey`. Key-on
requests are then refused until they have **paused for 5 s**, so a bus that is
still pushing audio can't re-key it at once; a fresh press after the pause
works normally. `0` turns the timer off. The browser mic has its own 120 s
time-out on top (see [web-ui.md](web-ui.md)), and the AIOC endpoint plugin a
60 s safety unkey on the endpoint itself.

## File-only keys and settings outside the .ini

Some keys are in `gateway_config.txt` but deliberately **not on the web form**
(`config_schema.NOT_IN_FORM` lists each with its reason): the Fleet Manager's
`MANAGER_*`, the APRS-IS keys, `SUPERVISE_MUMBLE`, `TTS_TARGET_RMS_DB`, the
stream tuning keys and the transcription keys below. Edit the file to change
them; a form save leaves them as they are. Other settings live outside the file
entirely. See the per-feature docs:

- **Transcription pool** (file-only keys) — `TRANSCRIBE_MODE`, `TRANSCRIBE_REMOTE_URLS`, `TRANSCRIBE_SPLIT_THRESHOLD_SECS`, `TRANSCRIBE_ALLOW_WORKER_REGISTRATION` (default true), `TRANSCRIBE_WORKER_TTL_SECS` (default 90), `TRANSCRIPTION_ALERT_KEYWORDS` (emailed, at most once per keyword per 5 minutes) plus the live UI settings in `.transcribe_settings.json`. Workers started with `--gateway` register themselves, so `TRANSCRIBE_REMOTE_URLS` is only for pinning a worker explicitly. See [transcription-pool.md](transcription-pool.md).
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

`[section]` headers are **cosmetic** for the main config: the loader skips
them, so a key works wherever you put it. The one exception is a **dotted**
section such as `[kv4p.vhf]`. Its keys belong to that one plugin instance (the
KV4P endpoint called `vhf`), are read by `endpoints_state.read_sections()`, and
never set the global key of the same name. Before 5.0.0 they leaked: a
`MUMBLE_PORT` under `[kv4p.vhf]` overrode the real one.

**A bad value warns and keeps its default.** If a value doesn't fit its key's
type, the loader prints

```
WARNING: gateway_config.txt:212: GPS_BAUD = 'fast' is not a valid int; keeping 9600
```

and carries on with the next line. Before 5.0.0 one bad value aborted the load
and every key after it silently stayed at its default.

Both halves of the value syntax live in [`config_format.py`](../config_format.py)
so they cannot drift: `parse_config_value()` reads, and `format_config_value()`
writes, quoting a value only when the reader would not give it back unchanged.
Pinned by
[`tests/test_config_value_parsing.py`](../tests/test_config_value_parsing.py),
which asserts the round trip is lossless.

## Saving from the web config form

`POST /config` (the **Save** button on the config page, also the TTS engine
switch and the soundboard category picker) **edits the file in place**. Only
lines whose values change are rewritten, keeping their inline comments; keys
the form doesn't show are left alone; form keys missing from the file are
appended under their `[section]` header. Comments, unknown keys and
`[plugin.instance]` sections survive. Pinned by
[`tests/test_config_save_preserves.py`](../tests/test_config_save_preserves.py).

Before 5.0.0 the save regenerated the whole file from the form layout, which
deleted the `[kv4p.*]` sections (so the KV4P endpoints stopped spawning), every
comment, and keys with no form field such as `SUPERVISE_MUMBLE`,
`PACKET_APRS_IS_PASSCODE` and `MANAGER_CLAUDE_MODEL`. If a 4.x save ate yours,
restore them from a backup or from `examples/gateway_config.txt`.

Secrets are never sent to the browser: every string key whose name contains
`PASSWORD`, `SECRET`, `PASSCODE` or `TOKEN` (`config_schema.secret_keys()`).
Before 5.1 this was a hand-kept list that missed `MUMBLE_PASSWORD`,
`MUMBLE_SERVER_1_PASSWORD`, `MUMBLE_SERVER_2_PASSWORD` and `CAT_PASSWORD`,
which /config showed in clear. Leaving a secret field blank keeps the stored
value.

## Defaults & types

Every key the gateway reads has one entry in
[`config_schema.py`](../config_schema.py):

| Name | What it holds |
|------|---------------|
| `DEFAULTS` | key → default value. The default's Python type is the key's type: `bool`, `int`, `float` or `str`. |
| `LAYOUT` | `(section, title, [keys])` — the config page's sections and fields, in order. |
| `NOT_IN_FORM` | Keys deliberately left off the form, each with the reason (secret, file only, ...). |
| `RETIRED` | Keys still found in old config files that nothing reads. |

`coerce(key, text)` turns the text from the file into a value of the default's
type. Booleans accept `true/yes/1/on` (anything else is false); int keys
accept `0x` hex and decimals; keys with no default are guessed by `infer()`
(number, then boolean word, else string). Plugin instance sections use the
same `infer()`, with hex allowed.

The example file still carries every key with its default and a comment, and is
the best thing to read. The schema is what the code uses, and
[`tests/test_config_schema.py`](../tests/test_config_schema.py) fails if the
two disagree (an example key the schema doesn't know, a form field with no
default, a default that is neither on the form nor in `NOT_IN_FORM`).

## Adding a new config key

1. Add it to `DEFAULTS` in `config_schema.py` with its default. The default's
   type is the key's type, so write `180`, not `'180'`, for a number.
2. Put it on the form by adding it to the right `LAYOUT` section, **or** add it
   to `NOT_IN_FORM` with the reason it is file-only. Optionally give it a hint
   (units, range) in `_FIELD_HINTS` in `web_server.py`.
3. Add it to `examples/gateway_config.txt` in the matching section, with a
   comment saying what it does and what `0`/blank means.
4. Read it in code as `self.config.MY_KEY`. `Config` sets every default as an
   attribute, so a `getattr(self.config, 'MY_KEY', fallback)` fallback is never
   used and only drifts from the schema.
5. Run `python3 tests/run_all.py config`.

Keys a **plugin** in `plugins/` reads for itself (`ENABLE_MYRADIO`,
`MYRADIO_DEVICE`) work without a schema entry: they are typed by `infer()`. Add
them to the schema only if they also go in the example file or on the form.

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
