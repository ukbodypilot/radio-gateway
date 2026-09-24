# Changelog

All notable changes to Radio Gateway.

## [Unreleased]

### Removed — the Telegram bot; alerts are emailed instead

The bot and everything behind it are gone: `tools/telegram_bot.py`, the
`telegram-bot` and `claude-gateway` services and `claude-gateway.sh`, the
`/telegram` page, `/telegramstatus`, `/telegramcmd`, `/open_tmux`, the Telegram
dashboard chip and panel, the `telegram_reply` / `telegram_status` /
`telegram_logs` MCP tools (170 -> 167), and every `TELEGRAM_*` config key.
Voice-note-to-radio-TX went with it. Claude Code's own remote control replaces
the interactive side.

Why: the bot typed messages into a `--dangerously-skip-permissions` Claude
session, so the only lock was the Telegram account itself — a hijacked account
was a root shell (`sudo` is NOPASSWD here), with no second factor, allowlist or
confirmation.

Alerts moved to email (`EmailNotifier`, already used for startup, periodic
status, tunnel and DDNS notices):
- **Fleet Manager** reports (`elevated`/`warning`), the pre-fix "attempting"
  notice and the post-fix outcome. The notice is sent synchronously before the
  fix runs because `restart-gateway` kills the sender; SMTP is bounded at 15 s.
- **In-process alert engine** (`alerts.py`). It now defaults to **off** (`ENABLE_ALERT_ENGINE = false`): the key had dropped out of the live config while the code defaulted to on, so it was running and would have started emailing raw-threshold noise.
- **Broadcastify stream alerts** already emailed; the duplicate Telegram send
  was dropped.
- A missing or unconfigured notifier is logged as `ALERT NOT SENT (email not
  configured)` rather than swallowed, and `install.sh`'s health check now warns
  when `EMAIL_APP_PASSWORD` is unset.

Also removed with it: the Fleet Manager's tmux fallback (`MANAGER_RUN_MODE`,
`_send_to_tmux`, `_run_via_tmux`, `tests/test_manager_tmux_submit.py`) — with no
`claude-gateway` session it could only fail — and `tmux` from the installer's
package list.

### Fixed — transcription keyword alerts had been dead

`TRANSCRIPTION_ALERT_KEYWORDS` (and the keyword box on `/transcribe`) POSTed to
`/telegram_send`, a route that no longer exists, inside a bare `except: pass`,
so it silently did nothing. `check_keywords()` now returns the hit and the
transcriber emails it, at most once per keyword per 5 minutes, from a background
thread. `TRANSCRIBE_FORWARD_TELEGRAM` / `forward_telegram` (same dead route) is
removed.

### Upgrading

On an existing install, after pulling: `sudo systemctl disable --now
telegram-bot claude-gateway`, remove `/etc/systemd/system/telegram-bot.service`
and `claude-gateway.service`, then `sudo systemctl daemon-reload`. Delete the
`[telegram]` block from `gateway_config.txt` (unknown keys are harmless, but the
bot token should not sit there), revoke the bot with `@BotFather`, and remove
the `telegram-bot` and `claude-gateway` entries from your `SYSTEM_MANIFEST.md`.
Set `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` if you have not, or nothing will
tell you when something breaks.

### Removed — the /voice page and its MCP tools

The talk-to-Claude tmux relay is gone: `web_routes_voice.py`, `voice.html`, the
`/voice`, `/voice/status`, `/voice/view`, `/voice/send` and `/voice/session`
routes, the Voice nav link, the "Voice Tmux"/"LAN Voice" lines in the status
emails, and the `voice_view` / `voice_status` / `voice_send` MCP tools
(173 -> 170). Claude Code's own remote control replaces it.

It ran in its own `claude-voice` tmux session and shared nothing with the
Telegram bot, which used `claude-gateway` (both since removed — see above). `/voice/send` typed into a
`--dangerously-skip-permissions` session, so on an install with a blank
`WEB_CONFIG_PASSWORD` it was an unauthenticated command path. That is closed.

### Added — 18 MCP tools (155 -> 173), audit of the tool layer

The BGM beds, per-bed announcer, hot-swappable TTS engine and soundboard
category picker (v4.3/v4.4) had web routes but no MCP tools: `bgm_status`,
`bgm_control`, `announcer_status`, `announcer_configure`, `tts_engine_status`,
`tts_engine_set`, `soundboard_categories`, `soundboard_set_categories`,
`soundboard_refresh` (new `mcp_server/tools/audio_beds.py`).

A dashboard-vs-tools sweep then added the panel actions that had no tool:
`gps_set_position`, `gps_switch_mode`, `transcription_search` (FTS5),
`trace_status` (the trace toggles were blind), and TH-9800 CAT
link recovery — `cat_serial_status`, `cat_reconnect`, `cat_serial_connect`,
`cat_setup_radio`. `cat_setup_radio` overwrites the radio's settings from
config; none of these key the transmitter. Still gated, not built: TH-9800
`MIC_PTT`, KV4P test tone, and IC-7100 power/mic gain (TX-side).

Deliberately NOT exposed: restart/reboot/exit routes, `/config` (carries
secrets), websocket and file-serving routes, worker self-registration.

### Removed
- `broadcastify_control` (start/stop/restart) went away with the dead DarkIce
  control surface in `e18435b`; docs and tool counts still listed it.

### Fixed — a config value could not contain a `#`

`PACKET_APRS_SYMBOL` could not be set from `gateway_config.txt` at all. Its
default `/#` — the standard APRS digipeater symbol — read back as `/`, because
the parser treated every `#` as the start of an inline comment. Quoting did not
help either: surrounding quotes were stripped *before* the comment split, so
`"/#"` truncated exactly the same way. Setting the key did something other than
what it said, with no error anywhere.

`parse_config_value()` now applies three rules in order:

1. **A quoted value is literal.** Everything between the opening quote and the
   matching closing quote is the value; anything after it is discarded. An
   unterminated quote is left as-is rather than swallowing the rest of the line.
2. **`#` starts a comment only at the start of a value or after whitespace.**
   `foo # note` comments; `/#` does not.
3. **Text inside `{braces}` is exempt**, nesting included — smart-announce
   prompts use `#` inside brace expressions.

Rule 2 is what makes this safe to land on live configs. Verified rather than
assumed: running the old and new parsers over the gateway's own
`gateway_config.txt` produces **identical values for all 384 keys**, and over
the example template the only difference is `PACKET_APRS_SYMBOL: '/' -> '/#'`
— the bug itself. `PACKET_APRS_SYMBOL = /#` is restored to the template,
uncommented.

The **write** side is the other half of the same bug. `_save_config` rewrites
the entire config file whenever the web form is saved, emitting bare
`KEY = value`, so a reader fix alone would still lose data the next time you
pressed Save. Both halves now live in a new `config_format.py`:
`format_config_value()` quotes a value exactly when `parse_config_value()`
would not give it back unchanged — using the reader itself as the oracle, so
there is no second copy of the rules to forget to update, and the other ~400
keys stay unquoted and readable.

Tests: `tests/test_config_value_parsing.py` — 18 parser cases, twelve
round-trip cases, and three end-to-end through `Config.load_config()`.
`unspaced hash kept` and `spaced comment` pin the two directions of rule 2
against each other, because a rule that fixes one by breaking the other is not
a fix.

## [4.6.0] -- 2026-08-27

Claims that used to be taken on trust now require evidence: a reconnect is not
a recovery until bytes move, a PTT is not a key until the radio hears it, and a
sink fed by two buses is refused rather than silently mangled. Plus the config
form stops handing secrets to the browser, and the manager stops burning 68M
tokens to ask a 3k-token question.

### Security — the config form was shipping secrets to the browser

`type="password"` masked the six sensitive keys **visually** while the real
value still sat in `value="..."` — readable via View Source by anyone who could
load `/config`, which has no auth. They now render as empty text inputs.

Not `type="password"` with a blank value either: that made Chrome treat
`/config` as a login page and fire password-reuse warnings on the rotating
`trycloudflare` hostname. There is no login here, so there is no password field.

Blank now means **keep the stored value** — `handle_config_form` drops empty
sensitive keys before `_save_config`, which falls back to the current config.
Without that half, every config save would have wiped all six secrets.

`DDNS_PASSWORD` was being rendered in clear and is now in `_SENSITIVE_KEYS`.

### Fixed — reconnect workers were murdering each other's connections

The 2026-08-19 flap: **1850 reconnect attempts in 3 h 42 m**, with attempt
numbers printing out of order (#79, #83, #72, #85) because ~8 workers were live
at once. `_connect()` writes shared instance state (`_encoder`,
`_icecast_sock`, `connected`), so concurrent workers corrupted each other — one
connects, the next one's `close()` drops that fresh connection, and its own
connect is then refused `403 Mountpoint in use` by the server still holding the
mount.

- `_connect_lock` serialises the destructive half (close + `_connect`). It is
  held across a network round trip, so it stays separate from `_reconnect_lock`,
  which guards a bool for microseconds — merging them would block the trigger
  path behind the connect path.
- `_reconnect_epoch` retires workers the watchdog has superseded rather than
  letting them touch a connection that is no longer theirs.
- `_reader()` binds the encoder and socket of **its** connection via default
  args. It used to re-read `self._encoder` every iteration, so a surviving
  reader from the old connection would drain the NEW encoder's stdout into the
  NEW socket — two readers, one pipe, interleaved MP3 frames.
- `superseded`/`wedged` counters are exported through `get_status()`, because
  the only other evidence the fix works is an absence of log spam, which is
  indistinguishable from the feature never running.

### Changed — manager checks run as one-shot `claude -p`

Manager runs were pasted into a long-lived Claude TUI that never exited. One
session ran Jul 29 – Aug 6 (9 days, 297 turns) with context growing
monotonically 41k → 403k tokens. It cost 42.9M cache-read + 24.9M cache-write +
266k output ≈ **68M tokens to move ~174k tokens of actual content** — roughly
390× amplification.

The cache-write half is the real cost, and it is not about verbose logs. Runs
are an hour apart, past the prompt-cache TTL, so each hourly check re-wrote the
entire accumulated history at the 1.25× premium just to ask a ~3k-token
question: single late-life runs paid 290k, 348k and 371k cache-write tokens.
Trimming log output attacks the 174k and does nothing about the 390×.

Nothing needed that continuity — `_build_prompt` already inlines the whole
snapshot and the answer returns via `manager_reports.jsonl` keyed by `run_id`.
`_run_oneshot` spawns a fresh process per run, so the growth is structurally
impossible. `MANAGER_RUN_MODE=tmux` keeps the old path as `_run_via_tmux`.

Failure handling is stricter than the polling loop it replaces: timeout,
missing binary, and clean-exit-without-report each write an elevated report
carrying the real stderr rather than hanging 600 s in silence.

New keys: `MANAGER_RUN_MODE`, `MANAGER_CLAUDE_BIN`, `MANAGER_CLAUDE_MODEL`,
`MANAGER_MAX_TURNS`. Documented in [`docs/fleet-manager.md`](docs/fleet-manager.md).

### Changed — the supervised-process table is pre-collected in the hourly snapshot

`hourly.md` requires cloudflared/mDNS state in every report — they have no
other check anywhere — but the hourly prompt tells the run not to collect data
itself. The only way to satisfy both was a `processes_status` tool call, and in
a one-shot run every tool call is another full re-read of the ~43k context.

`/api/processes` is now part of the snapshot (+520 tokens), so a healthy hourly
check needs no probes at all: read, threshold, append the report. Measured on
live runs: **4 turns / 172.7k tokens → 2 turns / 85.7k**.

### Added — Windows client: live device switching and self-update

Device switching (`i` = TX feed, `k` = mic, `o` = RX speaker) opens a modal
list over the dashboard; audio keeps running until a choice is committed.

The picker never touches a PortAudio stream. Selecting only sets
`state["<kind>_dev_pending"]`; the thread that owns that stream consumes it on
its next loop iteration and does the close/reopen itself, so a stream is only
ever manipulated from the thread that created it. Each switch rebuilds the new
device's native rate, channel count, block size and resampler, and drains the
capture queue first — bytes captured at the old rate would be garbled by the
new device's parameters. A device that won't open falls back to the one that
was working. The mic thread no longer exits when a device fails to open, which
previously made it impossible to pick a replacement without restarting.

Self-update mirrors what the Linux link endpoints already do: hash the local
files, compare with the gateway, pull the bundle and relaunch. The gateway
serves it from `/api/winclient/{version,files}`, kept as a **separate manifest**
from `_ENDPOINT_FILES` — that list is hashed as a unit and its copy in
`tools/link_endpoint.py` must stay byte-identical, so adding the Windows client
there would change the shared hash and push a useless bundle to every endpoint.

New page: [`docs/windows-client.md`](docs/windows-client.md).

### Added — the web mic ducks local playback while keyed

On a speakerphone the dashboard's own playback is acoustically coupled back
into the browser mic, so the operator hears themselves returned. Holding MIC
(or Space) now silences local output for the duration of the hold.

The PCM player's gain node covers PCM, AS1 and AS2 together — the AS taps feed
their RX audio into the PCM stream rather than playing separately — and the MP3
`<audio>` element is muted alongside it.

- `_wsApplyGain()` is the single writer of the PCM gain, so a slider drag
  mid-over can't un-duck the stream.
- A 15 ms ramp rather than a step, which would click on a live stream.
- `.muted` rather than `.volume`, leaving the user's slider value intact.
- Duck on press, not on session open, so nothing leaks while `getUserMedia` is
  still prompting.
- `micTeardown` and the `getUserMedia` rejection clear `_micKeyed` directly,
  bypassing `micRelease` — both un-duck explicitly, otherwise a dropped socket
  or a denied prompt would strand the dashboard permanently silent.

### Fixed — DDNS updated unconditionally, and could not see hostname expiry

The updater POSTed to No-IP every cycle regardless of whether the public IP had
changed, so the interval had been pushed to 30000 s (8 h 20 m) to stay under
their abuse threshold — at the cost of up to 8 h of stale DNS after a change.

It now polls the public IP via `DDNS_CHECKIP_URL` and only calls the provider
when it actually differs, plus a forced update every `DDNS_FORCE_INTERVAL` as a
safety net. The interval drops to 300 s: **change detection goes from ~8 h to
≤5 min while provider traffic falls from ~3/day to roughly one per month.**

Failures back off exponentially (6 h cap). Fatal codes (`nohost`, `badauth`,
`abuse`, …) start at the cap since they never self-heal — without this a dead
hostname would send 288 `nohost` calls/day, worse than before.

`DDNS_VERIFY_DNS` adds independent verification: resolve the hostname and
compare against our real public IP, emailing after `DDNS_MISMATCH_GRACE`
consecutive bad checks and again on recovery. This deliberately does **not**
trust the provider's response — a No-IP DDNS Key updates whatever hostname it
is bound to and ignores the hostname parameter, so an expired name can return
`nochg` while resolving to nothing. That is how the previous failure went
unnoticed.

New keys: `DDNS_CHECKIP_URL`, `DDNS_FORCE_INTERVAL`, `DDNS_VERIFY_DNS`,
`DDNS_MISMATCH_GRACE`, `DDNS_ALERT_INTERVAL`, `DDNS_UPDATE_INTERVAL`.

### Fixed — a Telegram answer composed but never sent looked identical to silence

`telegram_reply()` is the only path from the tmux session back to the phone, so
a missed call is invisible: "answered but never sent" and "never ran" both look
like silence. That cost a real answer — the session composed a full reply in
the pane and never sent it, and nothing anywhere recorded a problem.

After injecting a prompt, a watchdog thread polls the `last_reply_time` that
`telegram_reply()` stamps into the status file. If it hasn't moved within
`TELEGRAM_REPLY_TIMEOUT` (new, default 300 s, 0 disables), the bot sends the
last assistant block from the pane so the answer still reaches the user. When
nothing on screen looks like an answer it reports the silence and echoes the
question instead.

`_inject` also sends `C-u` before pasting, so a half-typed line in the prompt
box cannot get the injected prompt appended to it and submitted as one garbled
request.

### Fixed — tests exhausted /tmp's inode table, and two stubs silently disabled a suite

`test_soundboard_categories.py` called `tempfile.mkdtemp()` without cleanup, and
each case pre-creates one stub file per pool entry (~780) to keep
`_fill_soundboard_slots` off the network. One run leaked 144 directories and
~64,000 inodes.

Repeated runs filled the inode table — 2,035,767 inodes, 100% used, ~3000 stale
dirs — while `df -h` still showed **gigabytes free**, because the stub files are
empty. Every process writing to `/tmp` then failed with `No space left on
device`, and the test itself started failing because it could not create files,
which reads as a code regression rather than a full disk.

All suites now share `tests/_tmpdirs.py`, which closes a gap the earlier
per-file helpers shared: `atexit` does **not** run when a runner's timeout kills
the process, and these suites are slow enough to be killed in practice. A run
killed mid-way is exactly when the most directories are outstanding. Three
mechanisms, because one is not enough:

| Mechanism | Covers |
|-----------|--------|
| `atexit` | Normal exit, including a non-zero exit after a failure |
| `SIGTERM`/`SIGINT` | Runner timeouts; re-raised with the default handler so the exit status still reflects the signal |
| Stale sweep | `SIGKILL` cannot be trapped, so each run removes same-prefix dirs older than an hour |

Every suite now measures **+0 inodes and 0 leftover directories**, verified
including a SIGTERM mid-run.

Separately, `tests/test_stream_dead_uplink.py` pins the 2026-08-21 stall, and
adding it exposed that the two hand-rolled stubs were missing the new
attributes. Omitting them did **not** fail the suite:
`test_stream_reconnect_flap` raised `AttributeError` inside the reconnect worker
*and* inside the watchdog thread, both daemon threads, so both tracebacks were
swallowed and the suite exited 0 while the code under test was dead. The
visible symptom was the FIXED row quietly dropping to `attempts=1/wedged=0` —
which reads as an improvement. It was the watchdog never running at all.

### Fixed — "Reconnected successfully" now means bytes actually moved

`self.connected` on the Broadcastify source only ever meant "the Icecast
SOURCE handshake was accepted", but three places read it as "the stream is
up". On 2026-08-21 the uplink stalled for ten minutes and the difference
became the whole story: ten reconnect attempts each completed their handshake
and each logged `Reconnected successfully`, while `rg_stream_bytes_sent_total`
sat at a flat **+0 for 2.5 of those minutes**. TCP connected, Icecast
accepted, not one payload byte moved. The 30 s health check agreed, printing
`Stream recovered` and emailing it five times. The incident was invisible in
the log and obvious in Prometheus — and only Prometheus was right.

Two claims now require evidence:

- `_confirm_bytes_moving()` holds a fresh connection for up to
  `STREAM_CONNECT_CONFIRM` (5 s) waiting for `_bytes_sent` — which `_connect()`
  zeroes, so the advance cannot be inherited from the previous connection — to
  leave 0. It logs the success with the byte count and how long it took, or
  reports `connected but no data moved in Ns — NOT counting this as
  recovered`.
- A new `data_flowing` property requires a byte to have reached the server
  within `STREAM_FLOW_STALE_AFTER` (15 s), and the health check in
  `core/lifecycle.py` uses it in place of bare `connected`. A mount pushing
  nothing is down, and now alerts as down.

Neither path tears anything down. A blocked write already trips the 1 s
`_encoder_write` deadline, `_on_encoder_wedged` reaps and retries, and
`_supervisor_loop` backstops it; both fired correctly throughout the stall.
The recovery machinery was never the bug — the success message was. A teardown
here would only add a third path racing the two that work.

Regression test: `tests/test_stream_dead_uplink.py`. Case 4 is the load-bearing
one — it proves that a `_bytes_sent` left over from the previous connection
*would* confirm a dead link, so the zeroing in `_connect()` is what makes the
check mean anything.

The keepalive feeds the encoder every 50 ms whether or not the radio is busy,
so a healthy 32 kbps mount hands the reader a 4096-byte chunk about once a
second. 5 s is ~5x that margin and 15 s is ~15x, so a quiet channel never
trips either check. The reconnect watchdog budget gains `STREAM_CONNECT_CONFIRM`
to cover the confirmation wait, which happens inside the worker it watches.

### Fixed — recovery emails no longer arrive titled "Stream Down"

`_send_stream_alert` hardcoded the subject `Broadcastify Stream Down` for
every alert it sent, recovery included. The body was right and the subject was
wrong, and the subject is the half you see in a phone notification — so the
2026-08-21 stall sent ten "Down" mails in ten minutes, five of which were
actually announcing successful recoveries. It takes a `subject` now, and both
callers in `core/lifecycle.py` pass one.

The default is a neutral `Broadcastify Stream Alert`, not `Down`: a caller
that forgets should get a vague subject rather than a confidently false one,
which is precisely how the original read.

### Added — a sink can only be fed by one bus

The graph editor would happily draw a second bus into a sink, and nothing
checked. Two buses never mix into a sink, and what happens instead depends on
the sink:

- Queue sinks (mumble, broadcastify, transcription, remote_audio_tx, speaker)
  share ONE bounded deque keyed by sink_id in `_enqueue_sink`. Both buses
  append and the drain thread sends each in turn, so the far end gets 50 ms
  fragments of the two sources **alternating**, at twice real time, until the
  queue backs up and drops.
- `broadcastify_l` / `broadcastify_r` are a per-tick slot, not a queue: the
  second bus overwrites the first within the tick and its audio is silently
  discarded. (The two *sides* are separate sink ids, so the normal
  dual-channel feed is unaffected — that is two buses into two sinks.)
- Radio `*_tx` sinks are the worst. Every SoloBus builds its own `_PttWorker`
  while `_get_radio_plugin` returns the SHARED plugin object, so two buses put
  two PTT threads on one radio with private `_desired`/`_applied` state. When
  the first bus unkeys, the radio drops carrier — and the second worker, which
  only acts on a state CHANGE, never re-keys. The second bus then transmits
  into an unkeyed radio for the rest of its transmission, logging nothing,
  with every meter still moving.

The rule now lives in `routing_rules.py` and is enforced three times: the
editor refuses the drag and names the bus already holding the sink; the
`connect` and `save_all` API paths refuse before writing; and BusManager warns
on load but still builds the graph, because a routing mistake must not stop the
gateway booting. `nul` is exempt — it discards audio and exists to park a bus.

Mixing several sources is a within-bus operation (`mix_audio_streams`, with the
ducking and priorities that go with it). Put the sources on one bus instead.

Tests: `tests/test_routing_sink_conflicts.py`.

### Fixed — routing_connect / routing_disconnect had never worked

Both MCP tools posted `{'from', 'to'}` while `_routing_cmd_connect` reads
`source` / `bus` / `sink`, so every call fell through to the handler's
`specify source+bus or bus+sink` error. They are the only callers of those two
commands — the routing UI saves via `save_all` — so neither the tools nor those
handler branches had ever run.

Their auto-detect was separately wrong: a hardcoded sink-id list that had
already drifted, naming `kv4p_tx` (no longer a sink) and omitting
`transcription`, which was therefore classified `source-bus`. It now reads the
live graph from `/routing/status`. That also handles ids like `sdr2` and
`webmic`, which name **both** a source and a bus — the target is tested first,
since a sink target unambiguously means `bus-sink`. Unknown or illegal edges
are now named rather than guessed at, and post nothing.

Tests: `tests/test_mcp_routing_connect.py`.

### Fixed — which SINK KIND you wired decided whether PTT worked

A source that never asserts the PTT flag could key a link endpoint but never a
plugin radio. `RemoteAudioSource` ends every path with `return raw, False`, and
that was enough for the CM5, because `bus_manager`'s deliver loop keys link
endpoints from **audio level alone** (`LINK_AUTO_PTT_THRESHOLD`), consulting no
flag. A plugin radio like the TH-9800 owns no such path — it is keyed only by
`SoloBus`, from the source's flag — so the identical source on the identical
bus type produced audio, moved every meter, and never once keyed the radio.
Nothing was logged, because no key was ever *requested*.

`SoloBus.tick` Phase 2 now falls back to keying on TX audio level, using the
same 0-100 scale and the same default threshold as link endpoints, so both sink
kinds behave alike. `AUTO_PTT_THRESHOLD` overrides it; `0` restores flag-only
keying.

It belongs in the bus, not the deliver loop: the bus already owns `_PttWorker`
for its radios, and a second keyer on the same radio is the two-owners bug the
one-bus-per-sink rule exists to prevent.

Three guards, each pinned by a test:

- **`_tx_only` only.** A radio that came from a *source* is RX+TX, and keying it
  on the level of audio it just received keys it from its own receiver.
- **`tx_muted` honoured**, so this cannot newly key a muted radio.
- **Flag beats level.** A flag-asserting source still keys when quiet — the
  level trigger is a fallback, never a gate on the existing path.

The `PTT ON via <radio>` log line now names which trigger fired — `(flag)` or
`(level)` — so this is diagnosable from the first commit rather than as a
follow-up.

Link endpoints keyed by a bus switch from deliver-loop VOX to an explicit PTT
command, and their hold becomes `PTT_RELEASE_DELAY` (1.0 s) rather than
`LINK_AUTO_PTT_HOLD` (0.5 s) — a slightly longer tail, consistent with every
other bus-keyed transmission.

Tests: `tests/test_solobus_auto_ptt.py`.

### Fixed — a PTT that never reached the radio no longer reports success

`_ptt_via_software` threw the CAT server's reply away, so a key that never
happened was indistinguishable from one that did: `_set_ptt` marked the radio
keyed, `execute()` returned `{"ok": True}`, and the dashboard, `/status` and MCP
all reported a transmission that did not exist. The server had plenty to say —
`serial not connected` when the FTDI link is down, which RF ingress on 2m does
to this radio — and none of it was read.

The reply is now parsed and the verdict honoured. Replies are echoed
(`CMD{ptt[off]} False`), so the state is the **last** whitespace token; the echo
itself contains `on`/`off` and must not be matched on. A missing, empty,
unparsable or `serial not connected` reply is a failure, as is the radio
reporting the opposite of what was asked (the server's `mic_ptt` mirror and the
radio having diverged).

Two safety properties, both pinned by tests:

- **A failed attempt is retried, not swallowed.** The old `if state_on ==
  self._ptt_active: return` guard made a repeat request a no-op — the same
  stale-mirror trap the CAT server itself has. It now skips only when the
  current state actually *reached* the radio.
- **`_ptt_active` still tracks what was ASKED for**, even on failure, so the
  unkey that follows is always attempted. There is no path where the gateway
  believes it is unkeyed while the radio might still be transmitting.

`get_status()` gains `ptt_confirmed`, `ptt_failures` and `ptt_last_error`, and
failures print `PTT ON FAILED — <reason>`. Relay and AIOC paths report nothing
and are unchanged: silence still counts as applied.

Tests: `tests/test_th9800_ptt_confirm.py`.

### Added — TX-path counters on the TH-9800 (audio dropped before the radio)

`put_audio()` feeds a `deque(maxlen=16)` that **silently discards its oldest
chunk** when the writer thread falls behind, and it bypasses
`BusManager._enqueue_sink`, so `aioc_tx` never appears in `/sinkstats`. A stall
or an overflow on the TX path was completely invisible. On 2026-08-22 a 50/50
mark-space stutter while transmitting could not be attributed to queue
starvation vs USB contention, because there was no number for either.

Queue: `tx_enqueued`, `tx_drops`, `tx_depth_max`, `tx_depth_now`. The drop is
detected **before** the append — a full deque displaces silently, so checking
afterwards always reads "not full". The first overflow logs once and names the
cause; later drops go to the counter instead of spamming the log.

Hardware write: `tx_written`, `tx_write_ms_max`, `tx_write_ms_avg`,
`tx_write_errors`. Timed **even when the write raises** — a write that blocks
and then fails is exactly the USB-contention case worth seeing, and timing only
successes would hide it.

All eight are in the plugin's `get_status()` **and mirrored into `/status` as
`th9800_tx`** — `get_status()` is reachable only via
`execute({'cmd': 'status'})`, which no web route or MCP tool calls, so counters
living there alone would have been unreadable. Instrumentation nothing can read
is decoration.

Reading them:

- `tx_drops` climbing → the bus outruns the writer; audio is discarded before
  it reaches the radio.
- `tx_drops` flat but `tx_write_ms_max` in the tens of ms → the AIOC write is
  stalling. One bus tick is 50 ms, so anything near that is the `arecord` RX
  reader and the TX writer fighting over the same `hw:` device. The RX reader
  is **not** gated during PTT.
- Both quiet during a stutter → the cause is downstream of the gateway.

Instrumentation only: the RX reader is not gated, the queue depth is unchanged,
and `arecord` still uses raw `hw:`. Those are plausible fixes for something not
yet measured, and applying one now would make the next stutter harder to
attribute, not easier.

Tests: `tests/test_th9800_tx_counters.py`.

### Documentation

Every `docs/*.md` page was reviewed against the code for this release.

- **New** [`docs/windows-client.md`](docs/windows-client.md) — the Windows
  operator position had 826 lines of client code and no user-facing page.
- [`docs/fleet-manager.md`](docs/fleet-manager.md) — the architecture diagram
  still showed `tmux send-keys` as *the* execution mechanism; it now describes
  one-shot `claude -p` with the tmux path documented as a fallback.
- [`docs/audio-routing.md`](docs/audio-routing.md) — new sections for the
  one-bus-per-sink rule, the PTT sink-kind asymmetry, and Broadcastify uplink
  health (`connected` is not `flowing`).
- [`docs/plugin-development.md`](docs/plugin-development.md) — PTT keying is now
  part of the documented plugin contract.
- [`docs/web-ui.md`](docs/web-ui.md) — mic ducking.
- [`README.md`](README.md) — the "What's new" list stopped at v4.2; v4.3–v4.5
  were never added.
- [`examples/gateway_config.txt`](examples/gateway_config.txt) — **131 keys were
  missing**, including whole subsystems (KV4P, PACKET, USRP2, D75, SDR_PROC,
  AUTOMATION, GPS, USBIP, ADSB). Every key the code reads — via the defaults
  dict *or* `getattr` — is now present, in 39 sections. Three keys nothing reads
  (`START_TH9800_CAT`, `TH9800_CAT_HEADLESS` — both orphaned when `start.sh` was
  removed — and `SDR2_PRIORITY`, superseded by `SDR_PRIORITY_ORDER`) are
  commented out and marked deprecated.
- [`docs/index.md`](docs/index.md) — `endpoint_logs_design.md` and
  `windows-client-audio-investigation.md` were never listed.

## [4.5.0] -- 2026-08-03

Links you ask for stay up, the browser mic is push-to-talk, and node numbers
have names.

### Added — persistent AllStar links

A link you connect is now restored after a gateway restart, an ASL restart, a
reboot or a three-month absence, and stays gone once you Disconnect. The wanted
set lives in `usrp_desired.json`; `_reconcile_links` compares it against what
`rpt lstats` actually reports, once per AMI poll.

Links used to be one-shot `ilink 3`, which app_rpt never re-establishes — the
reason a link dropped overnight was still down in the morning with
`RECONNECTS 0`. We deliberately do **not** use app_rpt's "permanent" mode
(`12`/`13`) either: despite the name it is weaker, living only in Asterisk's
memory so it does not survive a restart, while re-dialling every ~10 s for ever
with no backoff and no way to tune it. Two retry loops fought, and only one
could be told to slow down. The reconciler now owns all retry timing.

Retries back off 30 s doubling to 30 min, then go **dormant** — once an hour,
indefinitely, still wanted. Dormant is reported loudly but is not abandonment:
a node that returns next month relinks itself. An attempt is judged on a LATER
poll, never on the AMI command's return, and a link sitting in `CONNECTING` is
counted as missing — treating presence in `lstats` as success made a node that
silently refuses us report "ok, 0 fails" for ever. The reconciler also refuses
to run against a link list it did not just read, so an AMI outage does not read
as "every link is down".

### Added — shared node address book

`usrp_nodes.json` maps node numbers to your own names for them, shared by both
AllStar panels: name 45412 once and AS2 shows it too. Sorted most-recently-used
first, so it replaces both per-instance recents dropdowns, whose contents are
migrated in on first load. Every node number on the panel — saved, kept
connected, your links, conference members, reconnect stats and the header — is
a link to that node's AllStarLink stats page.

### Changed — browser MIC is hold-to-talk

The shell's MIC button was a latch: click it, walk away, and the transmitter
stayed keyed. It is now press-and-hold (or hold Space), with the mic stream and
socket lingering 30 s after release so a second over does not re-pay the
getUserMedia + handshake latency and clip its first syllable. An open socket
therefore no longer implies a keyed transmitter.

Release is sent explicitly but is never the only thing that stops TX. Two
watchdogs run on the BUS thread, so neither depends on the browser still being
alive: a 2 s dead-man on the key refresh (covering a lost pointerup, a hidden
tab, a closed lid, dead wifi) and a 120 s time-out timer measured from the
first press, so refreshes cannot extend an over past it.

### Fixed

- The AllStar panel's entire `<script>` block failed to parse. `_PANEL_HTML` is
  a non-raw Python triple-quoted string, so `\'` inside an inline `onclick` was
  consumed by Python and reached the browser as `''`. Every button on both
  pages did nothing, with no server-side trace. Row buttons now use
  `data-act`/`data-node` with one delegated listener, and a test renders both
  panels through the real path and parses the result.
- The panel referenced `var(--t-err)` / `var(--t-warn)` but is standalone and
  does not load `common.css`, so those colours never applied — the
  link-reconnect warning had never once shown one. Theme vars are now declared
  in the page, and a test asserts every var used is declared.
- Web mic gain (`WEB_MIC_VOLUME`, 4×) hard-clipped with `np.clip` into a
  transmitter; now `apply_gain`'s tanh soft-clip.
- Connect reported success whenever Asterisk accepted the command, so a node
  that silently refuses looked identical to one that connected. It now re-reads
  `lstats` after a short settle and distinguishes connected, dialling-no-answer
  and no-link-created.

## [4.4.0] -- 2026-08-02

Monitor either AllStar node directly in the browser, without going through the
routing graph.

### Added — AS1 / AS2 monitor taps in the shell bar

Two toggles beside MP3 / PCM / MIC that play an AllStar node's RX audio
straight into the browser PCM stream.

The tap is filled in the plugin's RX path, not by a second `get_audio()` call —
that pops `_rx_queue` and would have stolen audio from the bus, causing
dropouts on the AllStar bus while monitoring. Filling it in the RX path also
means the tap works whether or not the node is wired to a bus, and it never
touches `/routing`: a listen button should not rewrite the graph.

Selection is exclusive — **PCM main, AS1 or AS2**. A tap replaces that tick's
bus contributions rather than mixing with them, so pressing AS1 gives you AS1
alone instead of AS1 plus every bus with `P` set. Enabling one tap clears the
other, and stopping the PCM player clears any tap (it exists only to feed that
stream). Button state is always re-read from the server after a toggle, so the
display cannot drift from what is actually playing.

The tap queue is bounded at 4 chunks (~200 ms), so a stalled browser cannot
grow it, and turning a tap off clears it so re-enabling never replays stale
audio.

## [4.3.1] -- 2026-08-02

AllStar panel usability: the two nodes are now distinguishable, and the panel
paints from a warm cache instead of waiting on AMI.

### Fixed — the two AllStar instances were indistinguishable

The nav labelled entries by NODE number (`AllStar 683970` / `AllStar 683971`),
which cannot be matched against the routing page's ALLSTAR 1 / ALLSTAR 2. The
`1`/`2` fallback in that expression only fired when the node was `0`, which
never happens on a working node. Instance 1's `PLUGIN_NAME` was also unnumbered
(`AllStar (USRP)`) while instance 2 already said `AllStar 2 (USRP)`.

Nav, panel title and services card now all read `AllStar 1` / `AllStar 2`,
matching `/routing`. The node number moved to the nav tooltip and stays in each
panel title.

`usrp2` carried a full copy of `_http_panel` just to swap two URLs, so a
placeholder added upstream rendered literally on that instance
(`__LABEL__ — node 683971`). Substitution moved to `_render_panel()` on the
parent; the subclass now overrides only `_panel_rewrites()`.

### Fixed — AllStar panel took ~9s to populate

The page made three serial AMI calls on load (`rpt lstats`, `rpt nodes`,
`rpt stats`), each bounded at 3s. A background poller now keeps links and node
stats warm from gateway startup, and the panel paints from `/usrp/status` — a
cheap local read — before the AMI-backed refresh returns.

Cached data is never presented as live: `/usrp/status` reports `links_age` /
`stats_age` and `links_stale` / `stats_stale`, the panel labels stale data
rather than showing it as current, and a cold cache counts as stale so a fresh
gateway never shows an empty list as "nothing connected". Caches update only on
a successful AMI call, so a failed poll ages out rather than blanking.

Three bugs found while building it:

- The poller called `self.get_links()`, which does not exist — the method is
  `link_status()`. Node stats refreshed correctly while links never did, so
  `links_age` climbed forever and the data still waited on a page visit.
- The cache paint ran once on load, so opening the page during the first ~10s
  after a restart showed "not current" and never re-checked. It is now driven
  by the existing 1.5s status poll.
- Only the direct links were cached; the indirect/conference list was computed
  and discarded, so that group still waited on AMI and the panel filled in two
  visible stages. Both are cached now, and one `renderLinks()` serves the
  cached and live paths so they cannot drift apart.

Poll failures are rate-limited: first occurrence, then at most once a minute
with a suppressed count, plus a recovery line. Unbounded, this wrote ~12k lines
a day per node during an outage.

## [4.3.0] -- 2026-08-02

Background music beds with a repeating spoken message and broadcast ducking, a
hot-swappable TTS engine with 85 more voices, a soundboard category picker, 20
playback slots — plus the audio-path fixes found while building them: a stream
deadlock that had cost 15 hours of dead air, fleet-manager auto-fixes that had
never once worked, periodic clicks on the AllStar send path, and resampling
that was ~100x slower than it needed to be.

### Fixed — routing page: `.title-box` never existed

Two `querySelector('.title-box')` call sites on `/routing` were selecting an
element nothing creates. Drawflow builds `.drawflow-node >
.drawflow_content_node`, and the page's node HTML is bare title text followed by
`<div class="box">` — there is no `.title-box` anywhere. Both call sites were
guarded by `if (el)`, so they failed silently rather than erroring.

Consequences, all long-standing:

- The **speaker-mode V/A/R buttons** have never rendered.
- The two `.title-box` CSS rules have never matched anything, so attempts to
  adjust padding above the node label had no effect.

Both injections now target `.drawflow_content_node` and insert before `.box` so
they land on the title line. The dead CSS rules are left in place — porting
their styling across would change the node appearance.

### Changed — routing node heights line up

`min-height` sat only on `.source`/`.sink`, leaving bus height purely
content-driven, so the two kinds never matched after auto-arrange. It now lives
on the shared `.drawflow-node` rule as `--node-h` (64px) with `box-sizing:
border-box`, giving one lever for all three node types.

Bus nodes were trimmed ~5px (proc buttons 14→12px, `.proc-buttons` margin-top
2→0, bus `.level-bar` margin-top 3→2px) to meet the raised floor.

The solo bus delay slider moved from a full-width body row into the title line.
As a body row it added ~14px that no other bus type had, so solo could never
align; and it used `flex: 1`, stretching to the node's 290px width. It is now a
fixed 54px control with a bare numeric readout.

### Fixed — periodic clicks on the AllStar/USRP send path

`UsrpPlugin.put_audio` downsampled 48 kHz → 8 kHz by calling `resample_poly` on
each 50 ms bus chunk independently. `resample_poly` is stateless, so the
anti-alias FIR's edge transients landed at every chunk boundary — 20 clicks a
second, heard as "sparkles" at the far end while the same audio was clean at a
PCM sink (which never resamples).

The RX path already solved this and documents it: `_feed_rx` carries
`RX_RESAMPLE_PAD` samples of context on each side with a held-back look-ahead.
TX now mirrors it via `TX_RESAMPLE_PAD`. Measured against a continuously
resampled reference: peak error **416.00 → 1.72**, rms **20.93 → 0.96**, and the
errors no longer cluster at the chunk seam.

Unkey now also clears the resampler context, not just the audio backlog —
otherwise the next key-up began by emitting the tail of the previous
transmission.

### Fixed — the gateway re-levelled deliberately levelled audio

`_decode_file` peak-normalises anything below −1 dBFS, which is right for quiet
soundboard clips but destroys an offline loudness match: each file is boosted by
its own crest factor. Three beds matched to 0.0 LU came out 2.2 dB apart.

`_decode_file` takes `normalize=True`; only `BGMSource.play()` opts out, so the
caller states the intent rather than the function guessing from filenames.
Measured spread across the beds: **2.16 dB → 0.30 dB**. The soundboard path is
untouched — a quiet clip is still lifted from −2.47 to −1.00 dBFS.

### Added — BGM runtime cap

`BGM_MAX_SECONDS` (default 120, `0` = never) stops a bed automatically so an
unattended loop cannot run for ever. Editable from the Msg dialog and persisted
with the messages; the playing pad's tooltip shows the countdown.

Reaching the cap also silences the announcer — a voice talking over music that
has ended is worse than either on its own. Timed in audio time, matching the
duck envelope, and `play()` resets the clock so switching beds gives a fresh
allowance rather than inheriting the previous bed's.

`0` is treated as a real value throughout rather than a falsy default, or "no
cap" would be impossible to set.

### Added — background music beds with a repeating spoken message

Three looping music beds and a per-bed TTS announcement, mixed together with
broadcast-style ducking.

- **`BGMSource`** and **`AnnouncerSource`** are their own routing nodes (`bgm`,
  `announcer`), each with its own decode buffer. BGM originally shared
  `FilePlaybackSource`, which plays one file at a time — so the music stopped
  the moment anything else played. Separate sources are what let a message run
  over a bed at all.
- **Per-bed messages and voices.** Each bed carries its own text and voice; the
  announcer speaks whichever bed is playing and goes quiet when BGM stops.
  State persists to `~/.config/radio-gateway/announcer.json`. Synthesis is
  cached per bed on `(text, voice, backend)`, so the repeat cycle is free and
  editing one bed does not re-render another. A voice belonging to a different
  engine is dropped rather than allowed to raise (`valid_voice`).
- **Duck envelope in `BGMSource`** — attack `BGM_DUCK_ATTACK` (0.25s), hold
  `BGM_DUCK_HOLD` (0.4s), release `BGM_DUCK_RELEASE` (1.2s), depth
  `BGM_DUCK_DB` (−12 dB, never to zero). Ramped per sample; stepping the gain
  once per chunk is audible as zipper noise on sustained music. Timed in AUDIO
  time rather than wall clock so the hold cannot drift out of step with the
  ramp when the bus stalls.

  The bed ducks itself rather than relying on bus ducking, because only
  `ListenBus` implements ducking at all, and a listen bus gates its Mumble sink
  on a bus-wide VAD flag that steady music does not hold open — routing a bed
  through one made the music vanish between announcements. Ducking in the
  source works on any bus type. The coupling is documented in `_duck_target`.
- Opt-in partial ducking in `audio_bus`: a source exposing `duck_level` is
  attenuated instead of dropped. No existing source has the attribute, so every
  live audio path keeps its exact hard-mute behaviour.
- BGM beds are excluded from the numbered soundboard slots, the same
  reservation that keeps `loop.*` out.

### Fixed — resampling was ~100x slower than it needed to be

`_decode_file` resampled with `resampy`. On a 5-minute 44.1 kHz bed that took
**25.9s**, which is why a BGM button could take half a minute to make a sound —
48 kHz files skipped the block entirely, so only some files were slow.

Now prefers `soxr` (already a dependency), falling back to resampy then linear.
Same file decodes in **1.30s**. Verified equivalent: correlation 1.000000
against resampy on a known tone, max sample difference 0.00088.

The int16 cast is now clipped. Both resamplers can ring past full scale on
transients, and the old unclipped cast wrapped that round to the opposite
polarity — an audible tick rather than a soft clip.

`_decode_file` is shared, so soundboard clips, announcements and TTS decode all
benefit whenever the source is not already 48 kHz.

### Fixed — BGM and Announcer had no meters on the routing page

`/routing/levels` lists sources explicitly and did not include the new nodes, so
they rendered with a permanently dead activity bar while producing audio.

### Fixed — the test loop's Stop button could not stop it

The Loop button was a blind toggle whose lit state lived only in the browser.
Loop state was never published in `/status`, so anything that stopped playback
server-side (the Stop button, a queued announcement, a restart) left the button
lit *and inverted* — the next click sent "toggle" to a server that was not
looping and therefore **started** a loop instead of stopping one. The same gap
left the button illuminated indefinitely after the sample ended.

- `/testloop` takes an explicit `{"action": "start"|"stop"|"toggle"}`; the
  button sends what it means instead of toggling blind.
- `loop_active` is published in `/status` and the button re-syncs from it on
  every poll, so any server-side change is reflected within one tick.
- A start that finds no `loop.*` file no longer leaves the flag asserted.
- `toggleTestLoop` moved to `common.js`; `dash_operate.html` had a second copy
  that shadowed it (the inline script loads after `common.js`).

### Fixed — loop.mp3 occupied a playback slot

The slot scanner globbed the audio directory and excluded only `station_id`, so
the test-loop bed was assigned to slot 1. Pressing Loop and pressing 1 played
the same file, which is why the loop looked like it was playing "the sample in
button 1". `loop.*` is now reserved alongside `station_id*`.

### Fixed — `handle_key` used a substring test for slot ids

`char in '0123456789'` is a substring match, so a two-digit slot id passed only
when its digits happened to be adjacent — `'12'` worked, `'13'` did not. Now an
explicit `isdigit()` plus a membership check against the real slot set.

### Added — configurable playback slots (default 20)

`PLAYBACK_SLOTS` (1-99, default 20) replaces the hardcoded nine. The web grid is
built from `/status` rather than hardcoded HTML, so the count is a config change
rather than an edit, and filename prefixes now accept multiple digits
(`12_siren.mp3` claims slot 12).

Only slots 1-9 are reachable from the **physical keyboard** — a keypress is one
character. Higher slots work from the web UI, `!play <n>` and MCP.

UI: the playback grid is sounds only, with transport (Stop / Loop / New / Cats)
on its own row beneath, and both grids use `auto-fit` columns instead of a fixed
three — twenty pads in three columns is a very tall panel.

### Fixed — every Edge/gTTS voice came out as voice 1

Voice selection has never worked on the Edge or gTTS backends. The web UI
publishes the voice as a string (`_get_tts_voices` returns `'value': str(k)`,
and JSON keeps it a string into `handle_tts`), but both branches resolved it
with `voice if isinstance(voice, int) else <config default>` — so every web
request failed the isinstance test and silently fell back to
`TTS_DEFAULT_VOICE`. Kokoro was unaffected because its branch tests for `str`,
which is why the bug stayed hidden while Kokoro was the default.

`_resolve_voice_index()` now accepts an int or a digit-string, rejects `bool`
(an int subclass), falls back cleanly for an out-of-range or stale value, and
survives a non-numeric `TTS_DEFAULT_VOICE`.

Two related fixes: `!speak <n>` validated against `TTS_VOICES` (gTTS) even when
Edge was active, so indices above the gTTS table were rejected and spoken in
the default voice; and bare `!speak` listed gTTS voices while on Edge.

### Added — hot-swappable TTS engine with a GUI dropdown

`gw._tts_backend` was resolved once in `setup_tts()` at startup, so changing
`TTS_ENGINE` needed a gateway restart.

- `apply_tts_engine()` rebuilds the backend in place. The new engine is
  constructed first and published with `_tts_backend` together under
  `_tts_lock`, so a *failed* switch leaves the previous working engine live
  rather than dropping TTS, and config is only written on success.
- `speak_text()` snapshots the `(backend, engine)` pair under the lock and then
  releases it — reading them separately mid-swap could pair `'kokoro'` with the
  edge module, and holding the lock would make a switch wait for a long
  synthesis to finish.
- `GET/POST /tts/engine` — GET reports each engine, which is active, and
  whether its package actually imports; POST swaps and persists via
  `_save_config`. Verified to survive a restart for all three engines.
- Engine dropdown on `/controls` and `/dashboard/operate`. Unavailable engines
  are shown disabled rather than hidden. The voice list repopulates itself: the
  switch changes what `/status` reports in `tts_voices` and the existing poller
  rebuilds when the value set changes.

### Added — many more TTS voices

Edge exposed 9 of the 47 English voices Microsoft actually serves, and gTTS 9
of its accents. Now 47 and 22 respectively (Kokoro was already complete at 55).
Edge labels carry Microsoft's own `VoicePersonalities` tag where it is useful —
`Ana` is tagged Cartoon/Cute, `Christopher` Authority, `Roger` Lively.

Indices 1-9 keep their positions in both tables: `TTS_DEFAULT_VOICE` is a
number, so renumbering would silently change a user's configured voice.

### Added — soundboard category picker and a clip-length cap

The Refresh button drew uniformly from all 784 sounds, so the eight biggest
categories were 41% of every draw, 78% of refreshes repeated a category, and an
average of 2.2 categories carried over from the previous refresh. The rare, more
interesting ones (`boing`, `fart`, `scream`, `squeak`, `wrong`) turned up in only
13% of refreshes.

- **`SOUNDBOARD_CATEGORIES`** — comma-separated list restricting the draw;
  prefix a name with `-` to exclude it, blank means all. Read at refresh time,
  so saving applies on the next Refresh with no gateway restart. Unknown names
  are ignored with a warning listing the valid ones, and a filter that matches
  nothing falls back to the full pool rather than leaving the soundboard silent.
- **Category picker in the GUI** — a *Cats* button beside Refresh on `/controls`
  and `/dashboard/operate` opens a tick-list of all 31 categories with sound
  counts, a live tally and All/None shortcuts. Built as a native `<dialog>` (Esc
  and focus trapping for free) and shared via `common.js`/`common.css` so
  neither page carries picker markup. Backed by `GET`/`POST
  /soundboard/categories`. Ticking everything stores blank rather than a
  31-name list, so a pool that grows later is picked up automatically.
- **`SOUNDBOARD_MAX_SECONDS`** (default 15, 0 disables) — the pool contains
  full-length music tracks that are useless as effects; id 2474 measured 72.9s
  and id 489 50s, between them a quarter of the slots. `Content-Length` over the
  worst-case MP3 bitrate gives a lower bound on duration, so oversized files are
  rejected without downloading the body; the rest are measured with `ffprobe`
  (5s timeout) and deleted if over. Lengths are memoised in
  `<playback-dir>/.soundboard_meta.json`, deliberately outside `.cache` so the
  Refresh cache wipe doesn't force a re-download just to re-learn a clip was too
  long. Rejected picks are replaced, not skipped.

### Fixed — the same soundboard clip could occupy two slots

19 sound ids are filed under more than one category (id 2891 is under `boing`,
`fart` *and* `funny`) and the download URL is built from the id alone, so the
old fixed slice could hand one clip to two slots under two different filenames.
Picking now de-duplicates by id.

### Fixed — the soundboard Refresh button under-reported

Downloads run on the `Soundboard-prefetch` thread started inside
`check_file_availability()`, so most had not landed when the handler counted
them — a good refresh could report "Refreshed 0 sounds". The response now also
carries `pending`, and both pages say how many are still loading (and which
categories are active). `/dashboard/operate` uses a toast instead of `alert()`.

### Fixed — the Broadcastify stream could deadlock and never reconnect

On 2026-07-30 the feed dropped at 22:08 and stayed dark for 15 hours having
made **zero** reconnect attempts (`_reconnect_count` was still 0 the next
afternoon). The trigger was an ordinary socket drop; the outage was ours.

The chain, confirmed live with py-spy:

1. `_reader` caught the `BrokenPipeError`, set `connected = False` and exited
   — **without killing the ffmpeg encoder**. That process was still alive 40h
   later.
2. `_reader` is ffmpeg's only stdout consumer, so its 64 KiB stdout pipe
   filled, ffmpeg blocked on write, and it therefore stopped reading stdin.
3. The bus sink thread parked forever in `self._encoder.stdin.write()` —
   while holding `_encoder_lock`.
4. `_keepalive_loop` had already passed its `if not self.connected` gate and
   was parked on that same lock, so it could never loop back to re-read the
   flag. Its `send_audio(b'')` call is the **only** reconnect trigger in the
   codebase, and it had become unreachable.

Fixes:

- The encoder's stdin is now `O_NONBLOCK`, and all writes go through
  `_encoder_write()`, which is deadline-bounded (`STREAM_ENCODER_WRITE_TIMEOUT`,
  default 1.0s) and reports a wedged encoder instead of blocking.
- `_reader` reaps the encoder (`_teardown_encoder()`) as part of the same drop
  that kills it, so ffmpeg can never outlive the connection.
- `_keepalive_loop` uses a **bounded** `acquire(timeout=1.0)`, so it can no
  longer be parked indefinitely behind a stuck writer.
- New `Broadcastify-supervisor` thread re-checks stream health every 10s and
  drives recovery on its own. It touches neither the encoder nor
  `_encoder_lock`, so it stays live no matter what the writers are doing —
  recovery no longer depends on a thread that shares the failure mode.
- Reconnect triggering is consolidated in `_trigger_reconnect()` behind a
  dedicated `_reconnect_lock`, with `_shutdown` / `_teardown_intentional`
  flags so shutdown and deliberate `reconnect()` calls don't race it.

Regression tests: `tests/test_stream_encoder_deadlock.py`.

### Fixed — every fleet-manager auto-fix had silently never worked

All four `fix` actions ran a bare `systemctl restart <unit>`. The gateway runs
as `User=user`, and polkit's default for
`org.freedesktop.systemd1.manage-units` is `auth_admin_keep`, so each one
failed `exit 1` with *"Access denied ... requires interactive
authentication"* — a check that happens **before** systemd resolves the unit
name.

- Unit restarts now use `sudo -n systemctl restart <unit>`, with matching
  rules in `/etc/sudoers.d/radio-gateway`.
- `restart-darkice` → **`restart-stream`**, which calls
  `stream_output.reconnect()`. The old action was doubly wrong: there is no
  `darkice.service` on this host, and the `stream_connected` alarm it answered
  reports the gateway's *in-process* Icecast client, which DarkIce cannot
  affect. (`restart-darkice` is kept as a deprecated alias.)
- `_apply_fix` now logs subprocess **stderr** instead of only the exit code,
  sends a second Telegram message with the real outcome, appends an
  `AUTO-FIX FAILED: ...` finding, and leaves the alert unread on failure.
  The pre-fix Telegram now says "Attempting", not "Action".

Regression tests: `tests/test_manager_fix_actions.py`.

## [4.2.0] -- 2026-07-29

Transcribe workers now dial into the gateway like every other fleet machine,
and the dashboard — which had grown status panels stacked behind tabs behind
more tabs — is split into four sub-pages where nothing is hidden. Plus a round
of web-layer cleanup: one `/status` poll instead of two, a nav that reflects
what's actually plugged in, dispatch tables instead of a 100-branch elif
chain, and escaping for the remote-supplied strings that reach `innerHTML`.

### Added — dashboard split into four sub-pages

The single `/dashboard` page had reached the point where most of its state was
invisible: seven services shared one tab strip, System and Status hid behind
another, and the Gateway Link panel split status and controls across two more.
The Dashboard nav item is now a group of four pages:

- **Overview** (`/dashboard`) — system bars and gateway status flags side by
  side, plus a subsystem **annunciator**: every subsystem (Link, Workers,
  Stream, Loop, AllStar, GPS, ADS-B, USB/IP, Telegram, Automation) is a
  status lamp that is always present — lit when running, dark when disabled,
  never hidden — and links to its sub-page.
- **Endpoints** (`/dashboard/endpoints`) — each Gateway Link endpoint is one
  card with its status readouts *and* its PTT/mute/gain controls together.
  Below it, **transcribe workers get the same endpoint-style cards**: engine
  and model tags, ready/loading/unreachable state, done/active counts,
  processing ratio, RAM/temp/fan, and for remote workers the URL, poll age
  and registration heartbeat. Card skeletons (which hold sliders) rebuild
  only on identity changes so a poll can't fight a drag.
- **Services** (`/dashboard/services`) — Broadcastify (with bitrate
  sparkline), Loop Recorder, AllStar, GPS, ADS-B, USB/IP and Telegram as
  visible panels. Disabled services ghost out instead of disappearing.
- **Operate** (`/dashboard/operate`) — Playback grid, Transmit (TTS / CW /
  AI / Fart tabs — input modes, not status), Automation engine.

Shared `web_pages/dash.css` + `dash.js` keep the four pages in lockstep;
`common.js` gained `stItem`/`stVal`/`stRow`/`stYesNo`/`stOnOff` builders so
pages stop hand-assembling the same status-row spans.

### Changed — web layer

- **One `/status` poll, not two.** The shell already polls at 1 s for the
  meter strip; it now broadcasts each payload into the content iframe via
  `postMessage`, and dashboard pages consume the broadcast instead of running
  their own 2 s poll. A page opened outside the shell falls back to fetching
  after 5 s of silence, and offline-detect/auto-reload behaviour carries over.
- **The Radios menu reflects reality.** Nav entries are driven by the same
  `/status` payload (sticky per session, so an endpoint reboot doesn't make
  menu items flicker), and AllStar entries are built from `/usrp/nodes` —
  discovered plugin instances get menu items automatically. Retired hardware
  (KV4P's old slot) no longer occupies a greyed-out entry forever.
- **`web_server.py` route dispatch is now table-driven.** The ~60-branch
  `do_GET` and ~45-branch `do_POST` elif chains became exact-match /
  query-stripped / ordered-prefix tables with one shared dispatcher. Route
  parity was replay-verified against the old chains (105 routes). The inline
  gdrive-publish block moved to `web_routes_system.handle_gdrive_publish_tunnel`.
- **Accessibility**: transmit tabs carry `role=tablist/tab/tabpanel` with
  `aria-selected` maintained; icon-only buttons and gain/volume sliders have
  `aria-label`s; the shell nav dropdowns are keyboard-operable (Tab, Enter /
  Space, Escape, `aria-expanded`).

### Fixed

- A worker that self-registered **before** the transcriber's `_run()` took its
  pool snapshot ended up in the pool twice — once via the snapshot (which also
  started a second status-poller thread for it) and once via the late-
  registrant rescue, which only filtered on `registered=True`. The orphan twin
  was unexpirable because TTL expiry matches engines by identity through the
  registration map, which only held one of the two. The rescue now identity-
  filters against the loaded set, and already-running engines are not started
  twice.
- The TH-D75 memory-channel table interpolated radio-supplied channel names
  into `innerHTML` unescaped — remote content from a fleet device reaching the
  gateway UI. Now escaped, along with `/controls` smart-announce activity
  strings (which can embed upstream error text). The `ic7100` and `kv4p`
  pages were audited and only render locally-computed values.

### Added — transcription workers register themselves

Remote transcribe workers were the only machines in the fleet the gateway had
to be *told* about: link endpoints dial into :9700 and REGISTER, but a worker
sat passive behind a URL pinned in `.transcribe_settings.json`. macmini's DHCP
lease moved three times (`.109` → `.132` → `.143`) and each move silently
emptied the remote half of the pool until someone edited a file — the daily
fleet check spent weeks reporting "macmini SSH refused" while actually probing
an address macmini had left.

Workers started with `--gateway http://<gateway>:8080` now POST
`/transcribe_worker/register` on a heartbeat, and the gateway takes the
worker's address **off the socket** — so the worker never needs to know its own
IP, and the gateway needs no worker address in config.

- `RemoteEngine` gained `name` / `registered`; `start(blocking_first_poll=False)`
  keeps the first `/status` poll off the caller's thread, so registering an
  unreachable worker can't park a web thread (or the pool lock) for the 5 s
  socket timeout.
- The pool is now mutable at runtime under `_pool_lock`, and the executor is
  sized with headroom because it can't be resized after construction.
- TTL expiry (`TRANSCRIBE_WORKER_TTL_SECS`, default 90) drops silent workers.
  Config-pinned URLs are never expired — an operator-pinned worker that is down
  stays visible as `unreachable`.
- Discovered addresses are deliberately **not** persisted to
  `.transcribe_settings.json`; writing them back would recreate the same stale-
  address failure.
- With an empty pool the transcriber now stays up waiting for registrations
  instead of stopping, and drops utterances with a counter rather than crashing
  on `_pick_worker` returning `None`.
- New: `transcription_workers` MCP tool, `registration` block in
  `get_status()` (accept/refresh/reject/expire/drop counters), `self-reg` badge
  on the `/transcribe` Workers row, `TRANSCRIBE_ALLOW_WORKER_REGISTRATION`.

### Fixed — three MCP tools dead since the 2026-05-28 split

`config_read`, `automation_scheme_read` and `automation_scheme_edit` resolved
repo files against their own `__file__`. The mega-refactor moved them from
`gateway_mcp.py` at the root down into `mcp_server/tools/`, so they had been
looking for `gateway_config.txt` and `automation_scheme.txt` two directories
too deep for two months. Each failed with a *"not found"* string rather than an
exception, which is exactly why nobody noticed.

`mcp_server/server.py` now exports `GW_ROOT`, and every tool module resolves
repo files against it. Secret redaction in `config_read` was verified against
the real file once it could actually read it.

### Added — 21 MCP tools (134 -> 155)

Gaps found by diffing the routing command dispatch table, the packet plugin's
`execute()` actions, and the served HTTP routes against the registered tools.

- **Routing (3)** — `bus_set_denoise_mix`, `bus_set_denoise_bypass`,
  `bus_set_delay`. All three were reachable from the routing UI only;
  `set_dfn_bypass` is the CPU knob added in the 2026-07-27 review-fix pass.
- **Packet (7)** — `packet_bbs_connect`, `packet_bbs_disconnect`,
  `packet_bbs_send`, `packet_bbs_buffer`, `packet_aprs_beacon`,
  `packet_force_audio`, `packet_set_endpoint`. `packet_mode('bbs')` could enter
  BBS mode but nothing could then hold a session.
- **Fleet Manager (8)** — new `mcp_server/tools/manager.py`: `manager_status`,
  `manager_reports`, `manager_doc_read`, `manager_doc_write`, `manager_toggle`,
  `manager_config`, `manager_ack`, `manager_run`. The subsystem has had a web
  page since 2026-05-18 and zero MCP coverage. `manager_run` is documented as a
  recursion hazard — it starts a Claude session, so it must not be looped on.
- **Voice relay + USB/IP (3)** — `voice_status`, `voice_send`, `usbip_status`.

### Changed

- `broadcastify_status` was still written in DarkIce terms. There is no DarkIce
  process any more — the audio is encoded in-process — so the tool now reports
  the encoder format, the L/R dual-channel state, throughput and `last_error`
  as named fields instead of dumping a raw JSON blob. The `darkice_*` keys in
  `/status` keep their historical names; the tool relabels them.
- Docs: the "142 MCP tools" figure in `README.md`, `docs/mcp.md` and the v4.0
  changelog entry was never correct — the real count at v4.0.0 was 134. The
  v4.0 entry is corrected to 134 and the current figures read 155. The category
  table in `docs/mcp.md` now covers every registered tool (`mixer_control` and
  `pihole_status` had never been listed).
- `gateway_mcp.py`'s module map listed 4 of the 12 tool modules and described a
  `routing.py` that still owned transcription, link endpoints and the loop
  recorder — all split out on 2026-05-30. Rewritten, with a note that a new
  module is invisible until it is added to `_register_all_tools`.

### Fixed — the installer, and four things a fresh install got wrong

`scripts/install.sh` had not been touched since v4.0.0 while two releases
shipped, so its guidance had drifted and two features were unreachable on a
clean box. Fixing that meant actually running it on a fresh Arch VM for the
first time in a while, which turned up four bugs that were invisible on the
live gateway — every one of them because **.140 works on historical packages
a fresh install never gets**. The installer's output was describing a system
nobody had built from scratch in a month.

- **A first install could not reach Mumble.** `pymumble` pins
  `protobuf==3.12.2`; `onnxruntime` and `faster-whisper` both pull a modern
  protobuf later in the same run, and protobuf 4+ refuses to load pymumble's
  pre-generated `mumble_pb2.py`. Run 1 ended on 7.35.1 with pymumble
  unimportable; a *second* installer run silently fixed it by reinstalling
  pymumble last. There is now a pin-repair step as the final pip action.
  3.12.2 satisfies pymumble, onnxruntime, moonshine and faster-whisper
  together. .140 never saw this — it has been on 3.12.2 for years.
- **Silero VAD did not work at all on a fresh install.** `_SileroVAD` runs
  the bundled ONNX through onnxruntime and needs no torch, but both of its
  model-path lookups went *through* the package, whose `__init__` imports
  torch — which the installer deliberately skips (`--no-deps`, to avoid
  ~2 GB). Now resolved with `find_spec` on the top-level name, which locates
  the package without executing it. Worked on .140 only because of torch
  left over from the Whisper era.
- **Every `whisper/*` model failed to load.** `016defa` swapped the
  installer's pip line from `faster-whisper` to `useful-moonshine-onnx`
  instead of adding to it, but `_VALID_MODELS` kept the whisper keys and
  `/transcribe` still offers them in both dropdowns — where Whisper is the
  starred recommendation. Moonshine is the default engine, not the only one;
  both are installed again.
- **Observability was wired up with no data in it.** `metrics.py` imports
  `prometheus-client`, which `install.sh` never installed, while that same
  installer set up Prometheus, set up Grafana and provisioned the dashboard.
  Invisible because every `import metrics` call site sits inside
  `try/except: pass` — instrumentation just no-opped and `/metrics` 500'd.

Also fixed in passing: `tools/build_rnnoise.sh` could never have run
anywhere. Its `cp .libs/librnnoise.so.*` globs to two paths (libtool leaves a
symlink beside the real file), so `cp` demanded a directory target, and it
needs `wget`, which the installer treats as optional elsewhere. It is now
wired into the installer as an optional step — a `-march=native` librnnoise
runs the denoise hot path at ~0.22 ms/frame against the wheel's ~0.9 ms,
which no fresh install had ever had.

New in the installer: `tools/check_deps_drift.py` diffs `requirements.txt`
against what `install.sh` actually installs and exits non-zero on drift —
`install.sh` does not read `requirements.txt` (several packages need handling
a flat `pip install -r` gets wrong), and those two hand-maintained lists are
how faster-whisper and prometheus-client both went missing. The health check
runs it, claims `import metrics` explicitly, and now also checks stream
sample-rate/bitrate coherence, transcription-engine importability, optimized
librnnoise presence, and the v4.2.0 dashboard sub-page files so a partial
deploy fails loudly instead of 404ing. `examples/gateway_config.txt` gains
`STREAM_MOUNT_WAIT`, a `[transcription]` section (there was none) covering
worker self-registration, and `SUPERVISE_DARKICE`/`SUPERVISE_MUMBLE`.

Verified end to end: a pristine Arch cloud image, one installer run, exit 0 —
then Kokoro TTS → Silero VAD → both ASR engines returning the exact reference
sentence, and `/metrics` rendering 16 series.

## [4.1.0] -- 2026-07-28

Broadcastify goes dual-channel, and a round of measurement-led cleanup across the
audio path. The theme of this release is that several things were quietly wrong
in the *reporting* rather than the audio — a metric fed from a web handler, a
bitrate that was never applied, stream metadata that was never sent — and fixing
the instrumentation is what exposed the rest.

### Added — dual-channel Broadcastify feed

Broadcastify accepts a stereo feed carrying a different receiver on each channel.
`STREAM_DUAL_CHANNEL` swaps the single `Broadcastify` routing node for a
`Broadcastify [L]` / `Broadcastify [R]` pair; route a different bus to each.

- The mono node and the L/R pair are **mutually exclusive by construction**, so an
  invalid routing graph cannot be expressed and there is nothing to validate. A
  third "both" node was considered and rejected — routing one bus to L and R is
  already equivalent, and it was the sole source of the invalid state.
- L and R arrive from different buses, so they are parked in per-tick slots and
  interleaved at the **end of the tick** into a single enqueue. Both channels
  therefore share one bounded queue: with a queue each, a drop on one side would
  offset the channels permanently and the drift would stay invisible until the
  receivers had separated by seconds. A short buffer is zero-padded, never
  truncated, for the same reason.
- Encoder uses `-joint_stereo 0`. Joint stereo codes mid/side to exploit L/R
  similarity that two independent receivers do not have.
- Per-channel meters (`stream_audio_l` / `_r`) on the routing page; a shared meter
  would show identical bars and hide the asymmetry a dual feed exists to show.

### Added — stream diagnostics

- **Dashboard**: last stream error with local timestamp and relative age, retained
  after recovery so an overnight drop is still visible; plus an inline sparkline
  of the last hour of bitrate, read through the gateway's own `/prometheus/` proxy.
- **ffmpeg stderr is captured** rather than sent to `/dev/null`. An encoder exit
  previously logged "Reader thread exited" with no reason. Drained on a dedicated
  thread — an unread pipe fills its ~64 KB buffer and blocks the encoder.

### Fixed — Broadcastify

- **`STREAM_BITRATE` had never taken effect.** At 48 kHz the encoder is MPEG-1
  Layer III, whose lowest Layer III bitrate is 32 kbps, so lame silently clamped
  16 up to 32. The feed ran at double Broadcastify's single-scanner spec. Output is
  now resampled to `STREAM_SAMPLE_RATE` (22050, MPEG-2 Layer III) where 16 kbps
  exists — verified 16.3 kbps, `sample_rate=22050 bit_rate=16000`. 22.05 kHz
  carries 11 kHz of audio against ~3 kHz of narrowband FM voice, so nothing
  audible is lost and upstream bandwidth halves.
- **False "feed has failed" alerts, 4–11 a day, on a healthy stream.**
  `rg_stream_bytes_sent_total` was incremented inside a web status handler with no
  background caller, so it only advanced while somebody had the dashboard open —
  flat, then one 86 MB step. `rate()` read zero in every gap. Now incremented
  where the bytes are written to the socket. Also corrects
  `manager_engine` `stream_throughput_kbps`, which derives from the same counter.
- **Every reconnect wasted its first attempt on `403 Mountpoint in use`** — the
  server had not yet reaped our own previous connection. A drop or a 403 now makes
  the worker wait `STREAM_MOUNT_WAIT` (15 s) instead of 5 s. Recovery went from
  10 s across two attempts to 15 s on one clean attempt.
- **No format metadata was sent.** The feed page showed Sample Rate 0, Bitrate 0,
  Channels 1 — unknown, not misread. `ice-audio-info` (both the `ice-` prefixed and
  bare key dialects) plus `ice-description` are now sent; the page reads
  22050 / 32 / 2.
- Routing UI meters on the new L/R nodes were permanently dead — `/routing/levels`
  keyed only on the old `broadcastify` sink id.

### Fixed — elsewhere

- **Google Drive showed "Not yet connected" indefinitely.** The startup probe ran
  once with no retry, so a single transient timeout stuck for the life of the
  process. Uploads were unaffected throughout — nothing but the status display read
  that flag. Now retries with backoff, 60 s timeout, and re-probes lazily from
  `get_status()` without blocking the request.
- Audio/PTT/CPU review findings — 12 items plus 3 found while fixing. Highlights:
  LoopRecorder filesystem work moved off the tick (rotation worst case 8.35 ms →
  0.047 ms), endpoint DSP vectorised (RX path 0.429 → 0.015 ms), TH-9800 PTT
  serialised with RTS restore on every exit path, per-endpoint jitter prefill made
  configurable.

### Performance — GC pauses

The gateway's SCHED_RR tick had a full collection overrunning its 50 ms budget
every 10 minutes, 100% of the time.

- `gc.freeze()` ran as the tick thread's first act, which only captured what
  existed at that instant. The transcriber loads Silero VAD + Moonshine ONNX on its
  own thread ~2 s later, so the expensive graphs escaped it. The freeze is now
  **deferred to a one-shot 60 s in**, after async startup work has settled.
- **gen-2 ~63 ms → ~4 ms**, measured over 14 hours and 85 collects with zero
  overruns and zero link-audio underruns. gen-1 3.080 → 0.566 ms, gen-0 0.336 →
  0.113 ms.
- The GC callback was appended on every tick-loop entry and never removed, so each
  routing reload left another live copy inflating `rg_gc_pause_ms_count` and
  `rg_gc_pause_overrun_total`. The counter read 8 for 4 real overruns.
- Moving `gc.collect()` off the tick thread was considered and rejected: CPython
  collects with the GIL held, so a collect from any thread stops every thread.

### Removed

- **Legacy SDR rebroadcast** — the `sdr_rebroadcast` toggle (`b` key, `/mixer`
  `flag=rebroadcast`, `SDR_REBROADCAST_PTT_HOLD`, and the BusManager
  `drain_sdr_rebroadcast` queue) is gone. It keyed the transmitter from the main
  loop and wrote its audio to `gw.output_stream`, which has been permanently
  `None` since AIOC TX moved into `TH9800Plugin` (2026-03-30) — so enabling it
  put a **dead carrier** on the air. The supported equivalent is routing: wire
  the receiver to a solo bus carrying the radio's `*_tx` sink, which does PTT and
  TX audio properly through `SoloBus`. See
  [docs/audio-routing.md](docs/audio-routing.md#rebroadcasting-a-receiver-onto-a-radio).
  The `/mixer` endpoint still accepts `flag=rebroadcast` but returns an error
  pointing at the routing page rather than silently doing nothing.

## [4.0.0] -- 2026-06-26

AllStar integration, Kokoro offline TTS, USRP2 dual-node support, IC-7100 squelch fix, and a complete MCP tool overhaul. The plugin platform is now fully generic over discovered plugins — routing UI, bus radio resolution, meters, and `web_routes` dispatch all enumerate `_external_plugins` rather than hardcoded radio names. Any future plugin gets full UI and MCP coverage for free.

### Added — AllStarLink (USRP) bridge

The gateway can now bridge into the AllStar network as a first-class audio source/sink and connect to any node on demand — without running a radio node on the gateway box. A headless ASL3 "bridge node" (no RF hardware) speaks the USRP (DVSwitch) protocol to a new in-gateway plugin; everything else (Mumble, web player, other radios) routes to/from AllStar like any other bus endpoint.

- **`plugins/usrp.py`** — in-gateway USRP/DVSwitch plugin. Full-duplex 8 kHz↔48 kHz bridge (scipy `resample_poly` ×6), paced 20 ms sender with keyup/unkey framing, bounded RX/TX queues, RX + TX level meters. Wire format: 32 B header (`USRP` + 7 big-endian int32) + 160×int16 LE @ 8 kHz.
- **`plugins/usrp2.py`** — second USRP plugin instance for a second ASL3 bridge node. Shares the same codebase with independent config keys (`ENABLE_USRP2`, `USRP2_*`). Both appear as separate bus sources/sinks with independent `/usrp` and `/usrp2` panels.
- **Runtime node control via AMI** — short-lived authenticated sessions to the bridge node's Asterisk Manager Interface issue `rpt cmd <node> ilink …`: connect (transceive/monitor), disconnect one, disconnect all, and `rpt lstats`/`rpt nodes` for status. The target node is chosen at runtime, not baked into config.
- **`/usrp` control panel** — connect to any node, **per-node Disconnect** on your *direct* links, a read-only "in conference (via a hub)" list for nodes reached *through* a hub, most-recently-connected link sorted to the top, and live RX/TX/COS status. Plus a **nav link** (Radios ▸ AllStar) and a dual-lane **"ASL"** RX/TX meter in the top frame.
- **`/usrp` recent-nodes dropdown** — the panel remembers the last 10 connected nodes (persisted to `usrp_recent.json`).
- **AllStar dashboard section** — `/usrp` status (node, bridge AMI, RX/TX, packets, connected nodes) in the dashboard service panel next to USB/IP and Telegram.
- **Dynamic multi-node AllStar dashboard panels** — enumerate both USRP instances dynamically; no hardcoded ASL1/ASL2 labels.
- **Link quality stats** — pps (packets per second) and packet loss % visible per node in the panel. Local and remote stats compared.
- **Solo bus output delay slider** — `/routing` UI gains a latency-compensation slider per solo bus output; manager snapshot pre-collects before delivery.
- **Bridge-node recipe** — [`docs/allstar_bridge.md`](docs/allstar_bridge.md): a headless ASL3 container (`asl3-asterisk`, no DAHDI), `rxchannel = USRP/…`, registered node, AMI user, reboot-persistent. Node-to-node linking is permissionless, so it reaches the whole ASL3 network.
- **Config** — `ENABLE_USRP`, `USRP_REMOTE_HOST/PORT`, `USRP_LISTEN_PORT`, `USRP_NODE`, `USRP_AMI_HOST/PORT/USER/SECRET`; and `ENABLE_USRP2` / `USRP2_*` equivalents.

### Added — Kokoro ONNX offline TTS (new default engine)

- **Kokoro ONNX TTS engine** — offline, high-quality neural TTS is now the default engine (`TTS_ENGINE = kokoro`). 54 voices across 9 languages (US/GB English, Japanese, Mandarin, Spanish, French, Hindi, Italian, Brazilian Portuguese). No internet required at runtime. Voice dropdown in `/controls` and `/dashboard` auto-populates from the active engine's voice list.
- Per-message voice override: `!speak af_bella Hello` (Kokoro string IDs) or `!speak 2 Hello` (gTTS/Edge numeric accents).
- New config key: `KOKORO_DEFAULT_VOICE` (default `af_heart`). Smart Announce voice fields accept Kokoro IDs.
- Model files (~340 MB) gitignored, downloaded by `scripts/install.sh` into `tools/models/kokoro/`.

### Added — MCP tool overhaul (134 tools, up from 95+)

- **AllStar/USRP tools** (`mcp_server/tools/usrp.py`) — `usrp_nodes`, `usrp_status`, `usrp_connect`, `usrp_disconnect`, `usrp_disconnect_all`, `usrp_links`, `usrp_node_stats`. Cover both USRP instances via `node_id` param.
- **Broadcastify tools** — `broadcastify_status`, `broadcastify_control` (start/stop/restart).
- **Smart Announce tools** — `smart_announce_status` (slots, countdowns, activity), `smart_announce_trigger` (fire any slot immediately).
- **Relay/GPIO tools** — `relay_status`, `relay_charger_toggle`.
- **ADS-B tool** — `adsb_status` (dump1090-fa health, aircraft count, message rate, FR24 feed).
- **Bug fixes** — `audio_trace_toggle` and `stream_trace_toggle` called `_auth_headers()` which wasn't imported (NameError on every call); fixed. `stream_trace_read` resolved to `mcp_server/tools/tools/stream_trace.txt` (wrong); now globs `{gateway_root}/tools/stream_trace_*.txt` for the most recent timestamped dump. `loop_recorder_export` and `loop_recorder_download_all` hardcoded `http://127.0.0.1:8080`; replaced with `GW_BASE_URL` + auth headers.

### Changed — plugin platform is now generic over discovered plugins

- **`web_routes()` dispatch is finally wired.** Registration existed since the Phase-2 refactor but `web_server` never consulted `_plugin_web_routes`; `do_GET` **and** `do_POST` now do, so any plugin can serve its own pages/endpoints.
- **Routing UI, level meters, and bus radio resolution enumerate `_external_plugins`** (capability-driven) instead of hardcoded `sdr_plugin`/`th9800_plugin` refs — a discovered plugin with `audio_rx`/`audio_tx` now appears as an RX source and `<id>_tx` sink, gets node meters (`/routing/levels`, `/status`), and is reachable as a bus TX radio. Future in-tree plugins get all of this for free.

### Fixed

- **AllStar TX was dead (`pkts_tx=0`)** — buses are built *before* plugin discovery; a solo/duplex bus whose TX radio is an external plugin resolved to `None` at creation and never attached its radio. `core/lifecycle.py` now calls `bus_manager.reload()` after discovery so external-plugin radios attach.
- **USRP RX audio crackle eliminated** — the upsampler ran `resample_poly` per 20 ms frame, leaving ~7% edge-taper glitches at every boundary (~50 Hz crackle). Now a continuous resampler carries filter context across frames (~8 ms added latency, crackle gone).
- **IC-7100 false squelch underruns** — CIV squelch-closed state gated audio and caused ~1200 false underruns/minute after the CIV connection came up (~12h runtime). Fixed with a 2 s silence timeout before downgrading the primed flag.
- Plugin **discovery aborted** ("`'X' object has no attribute 'name'`") when an external plugin lacked a `.name` attr. Plugins now require it.
- `/usrp` **link list parsed empty** — app_rpt prefixes nodes with a status letter (`T55553`), defeating a `\b(\d+)\b` regex. Now matches digit runs not bordered by digits.
- **No Disconnect buttons** — AMI wraps CLI output as `Output: <line>`, so the `rpt lstats` line-parser saw `Output:` as token 0 and found zero direct links. The `Output:` envelope is now stripped in `_ami_command`.
- **External-plugin TX meter froze** — the bus only decays built-in radios' `tx_audio_level`; the USRP plugin now self-decays its own.

### Added — soundboard

- Real-sample soundboard on `/controls`; `tools/fetch_freesound_farts.py` fetches CC0 clips via the Freesound API (key from env / `gateway_config.txt`, `audio/farts/` gitignored).

### Notes

- One interop limitation: nodes running older/HamVOIP-style app_rpt can drop the link (~10 s) when the app_rpt `newkey` handshake doesn't complete — a remote-node-side issue, not the bridge (verified: the bridge connects and holds to ASL3 nodes/hubs across both monitor and transceive).
- Kokoro model files (~340 MB) are not in git — run `scripts/install.sh` or download manually into `tools/models/kokoro/`.

## [3.8.5] -- 2026-05-25

Follow-up to 3.8.0 — the IC-7100 panel hit the bench and the gaps showed up immediately. This release closes them: TX audio actually leaves the radio, PTT refusals are visible in the GUI, the dashboard meter no longer lies when the squelch closes.

### Fixed — IC-7100 TX path

- **USB audio codec detection by VID:PID** — the PCM2901 in the IC-7100 doesn't put "IC-7100" in its ALSA name, so the endpoint's name-matching `_find_alsa_card()` was returning `None` and TX audio went to the void. Now reads `/proc/asound/cardN/usbid` and matches `08bb:2901`.
- **Auto DATA-mode toggle around gateway-initiated PTT** — the radio's DATA-OFF MOD source is the front-panel mic; DATA-ON MOD is the USB codec. Gateway PTT now flips DATA on before keying and off after release, so the operator's manual mic still works between transmissions.
- **Split-mode aware DATA toggle** — DATA mode is per-VFO/mode, so in split the gateway has to set it on both VFOs before keying. Plugin swaps VFOs, sets DATA on the inactive side, swaps back. Fixed silent carrier on split-mode TX.

### Fixed — Visibility & feedback

- **PTT route surfaces real ACKs** — `gateway_link.send_command_to_and_wait()` with `_cmd_id` correlation; refused PTT (TX interlock blocks HF) now returns `ok:false` to the GUI instead of optimistic `ok:true`. Previously the dashboard claimed PTT was engaged when the endpoint had rejected it.
- **TX interlock state persists across endpoint restart** — `tx_allow_hf` and `vu` saved alongside other settings; surviving the link-endpoint restart cycle.
- **Dashboard outer-frame meter stops freezing at last pre-squelch level** — `LinkAudioSource.audio_level` is only decayed inside `get_audio()` (called by the bus tick). The IC-7100 is wired as a sink-only endpoint, so the bus tick never touches it and the level froze at whatever `push_audio` last computed — which scales with signal strength, hence the "stuck S-meter" impression. New `meter_level()` returns 0 if no `push_audio` in the last 250 ms; `/status` uses it.
- **RX audio gated on squelch state** — the IC-7100's USB codec streams unconditionally (no internal squelch like the kv4p/D75). Plugin's `get_audio()` now gates on `civ.squelch_open` so the bus meters track operator-relevant audio instead of the radio's open-codec noise floor.
- **Faster S-meter on `/ic7100`** — piggybacked `get_smeter()` onto the meter loop's RX cadence (~3 Hz). Was unusably slow before.

### Changed

- **Interlock dashboard dots** — orange when TX is blocked, blue when enabled. Matches the rest of the panel's status-pill vocabulary.

### Notes

- `PTT_ACTIVATION_DELAY`, `PTT_TTS_DELAY`, `PTT_ANNOUNCEMENT_DELAY` bumped to 0.75 s in the gateway config to give the IC-7100 a touch more carrier-stabilize time before the first sample plays. Config tweak only; not a code change.
- Restart the gateway after upgrading (Python modules load at start). The IC-7100 plugin needs redeployment to the endpoint host that runs it.

## [3.7.0] -- 2026-05-18

### Added — Distributed transcription pool

The transcription system is now multi-machine. VAD continues to run locally, but ASR inference can be local, remote, or routed between both per utterance.

- **`transcribe_engine.py`** — shared module: `LocalInferenceEngine` (Moonshine or Whisper via faster-whisper in-process), `RemoteEngine` (HTTP POST to a worker with background `/status` polling), `_pick_worker()` dispatcher with length-based tier routing and least-busy fallback.
- **`tools/transcribe_worker.py`** — standalone HTTP server wrapping `LocalInferenceEngine`. Endpoints: `POST /transcribe` (raw float32 audio in, JSON `{text, proc_time}` out), `POST /model` (runtime model swap with old-model release + `malloc_trim`), `GET /status` (model state, CPU temp, fan RPM, RAM, last switch error). Reads `WHISPER_CPU_THREADS` from env to cap CPU thread count on thermally constrained boxes.
- **Pool modes**: `off`, `local`, `remote`, `pool`. Pool mode adds length-based routing — clips under `TRANSCRIBE_SPLIT_THRESHOLD_SECS` prefer Moonshine engines (no fixed-window cost); longer clips prefer Whisper engines. Soft fallback to the other tier on outage.
- **New config keys**: `TRANSCRIBE_MODE`, `TRANSCRIBE_REMOTE_URLS` (comma-separated), `TRANSCRIBE_SPLIT_THRESHOLD_SECS` (default 10). `TRANSCRIBE_REMOTE_URL` accepted as alias.
- **Out-of-order result handling** — utterances inserted into the log sorted by `start_time` (VAD close time) not arrival order, so pool completions stay chronologically correct.
- **Per-engine telemetry** — `dispatched`, `inflight`, `avg_ratio`, `ram_mb`, `cpu_temp_c`, `fan_rpm`, `last_switch_error` on every worker card.
- **Web UI** at **/transcribe** rebuilt: numbered sections (`01 / Signal`, `02 / Throughput`, `03 / Resources`, `04 / Workers`, …), fixed-width tabular numerics so values never push neighbouring elements, worker cards with stable slots, mode selector + split threshold slider.

### Added — Audio meter unification (RG.vu)

System-wide upgrade of every audio level / VAD meter to hardware-VU physics.

- **`web_pages/common.js`** — `RG.vu` engine: rAF interpolator with asymmetric attack (~50ms) / decay (~500ms) envelope, peak-hold sliver, clip-path based gradient reveal so green→amber→red zones only become visible when the bar actually reaches their tick locations (no more whole-bar colour flipping).
- **Adoption** in dashboard link bars, sdr level + per-channel bars, monitor, d75, packet, kv4p, transcribe, and the outer-frame shell.html strip (AIOC, KV4P, SDR1/2, REMOTE, AN, SP, MON, link endpoints). All polls at 1Hz but render at 60fps via the rAF loop.
- **System bars** (sysBar — CPU/RAM/Swap/Disk) fixed: was flipping the whole bar to yellow/red when crossing 60%/80%; now uses a fixed-width gradient revealed by width, so amber/red zones only appear when the bar fills past their tick positions.

### Added — Gateway docs persistence

- **`scripts/radio-gateway-backup.service`** + `.timer` — 6-hourly rclone push of operational docs (`hourly.md`, `daily.md`, `SYSTEM_MANIFEST.md`) and state files (`manager_state.json`, `manager_reports.jsonl`, `.transcribe_settings.json`) to `gdrive:radio-gateway/manager/`. Secrets (`gateway_config.txt`) deliberately excluded.
- **Manager reports JSONL** retention is open-ended on disk (was un-bounded; the 7-day prune got reverted at user request — reports persist across reboots and the manager UI reads up to the last 100).

### Fixed

- **`install.sh` package-manager detection** ([#3](https://github.com/ukbodypilot/radio-gateway/issues/3)) — was matching the Debian-shipped `pacman` arcade-game package and assuming Arch on a Raspberry Pi OS install. Now reads `/etc/os-release` first; command-existence fallback checks for `/etc/pacman.d` before accepting pacman.
- **`radio-gateway-powersave.service`** — was failing since May 10 because USB devices (GPS dongle, RTL2838) had migrated to a different bus. Rewritten to match devices by `idVendor:idProduct` not hardcoded path; checked into repo + installed by the installer.
- **D75 watchdog silent death** — the BT serial reconnect loop's outer thread had no `try/except`, so a stray exception killed the watchdog and left the plugin in a half-broken state (audio still flowing, serial dead, no recovery attempts, multiple reconnects per day). Wrapped the loop body, added a 60-second heartbeat log, switched the spawn to `python3 -u` so any future failure prints reach the journal immediately.
- **dell-smm-hwmon module** loaded + persisted on the gateway Optiplex so the local LocalInferenceEngine's fan RPM telemetry populates (CPU fan + chassis fan are now visible alongside core temp).
- **Manager reports rendering** — newer reports use a dict for `findings` while older ones used a list; the renderer assumed list and silently crashed on dicts, leaving the page blank. Handles both shapes now.
- **Pool-aware fallback** — `_pick_worker()` filters to ready engines before tier selection. If the long-tier remote is offline, long clips fall back to the local Moonshine engine instead of vanishing into a failed HTTP POST.

## [3.6.0] -- 2026-05-08

### Added — Fleet Manager

A **document-driven autonomous monitoring and maintenance system**. Instead of hard-coded checks and alert rules, the Fleet Manager works by sending plain-English task documents to the `claude-gateway` tmux session on a schedule, then reading back a structured JSON report. The entire monitoring behaviour is plain text editable in a browser — no code changes, no restarts.

- **`manager_engine.py`** — background scheduler thread. Fires hourly tasks at the top of each hour and daily tasks at a configurable time (default 06:00). Embeds a `run_id` in each prompt and polls `manager_reports.jsonl` for a matching response (10-minute timeout). Thread-safe state in `manager_state.json`.
- **`SYSTEM_MANIFEST.md`** — authoritative fleet reference document. Hardware, roles, LAN/Tailscale IPs, service contracts, known failure modes for every node. Read by the agent before each daily run. Written by the agent when a node relocates to a new IP. Gitignored — never committed.
- **`hourly.md`** / **`daily.md`** — plain-English task lists. Hourly covers services, SDR, stream, disk, memory. Daily adds fleet-wide ping sweep, subnet node-discovery when a known IP is unreachable (SSH fingerprint identification + automatic manifest update), Docker stack check on Mac Mini, log error counts, housekeeping. Both fully editable in the browser.
- **Web UI** at **System → Manager**: ON/OFF toggle, daily run time, Run Now buttons, View (rendered markdown) and Edit (in-browser textarea, Ctrl+S saves) for all three documents, persistent scrollable report list with expandable findings.
- **Red alert dot** on the System menu label. Lights on unacknowledged elevated reports; clears when Manager page is opened.
- **Telegram escalation** on `severity: elevated` reports.
- All manager runtime files added to `.gitignore`.

### Changed

- Shell nav System dropdown: Manager added as first item.

## [3.5.0] -- 2026-05-03

Two big things this release: a **persistent transcription log with natural-language search**, and a **bus tick refactor** that pushes every sink off the audio hot path. Plus an installer overhaul, several UI redesigns, and a handful of fixes for bugs that surfaced along the way.

### Added — Transcription log + AI search

- **Persistent transcription log** (`transcription_log.py`). SQLite database with FTS5 full-text search. Every transcript Moonshine produces is stored with its timestamp, source bus, frequency tag, duration, and text. Survives restarts.
- **Natural-language query via Claude CLI.** `POST /transcription/query` takes plain English ("What was said on 446.76 today?", "Any emergency traffic this week?") and returns a plain-English answer. The gateway translates the question to SQL via the local Claude CLI, runs it against the FTS5 index, summarises the matching rows.
- **MCP tools** `transcription_log_query(question)` and `transcription_log_recent(limit=20)` — same surface area, callable from any MCP client (e.g. the Telegram bot).
- **Web UI** for the log: search box, answer box, recent-transcripts list. Lives on the Transcribe page.
- **Alert keywords** (`TRANSCRIPTION_ALERT_KEYWORDS` config + runtime-persisted edit). Comma-separated list of words to watch for. When a transcript matches, the keyword check fires (current path: pluggable callback into the gateway; in this release wired up for log highlighting).
- **Per-transcript forwarding toggles**: `forward_mumble` and `forward_telegram` (runtime-persisted via the Transcribe page). Lets you mirror live transcripts into the Mumble channel as text, or into a Telegram chat, without rewiring routing.

### Added — Bus tick refactor (v3.5-A through E.1)

The audio path off-tick rework. Five planned refactors; the fifth was instrumented and the data showed it wasn't worth doing. Headline numbers: noise gate 12-24 ms/call → 0.011 ms/call. No sink call blocks the bus tick anymore.

- **Per-sink off-tick drain queues.** `BusManager._enqueue_sink` stages a sink call into a bounded deque (`maxlen=8`, drop-oldest); a per-sink daemon thread named `SinkDrain-<id>` drains and dispatches via `_do_sink_send`. Sinks converted: **broadcastify, mumble, automation_recorder, echolink_legacy**. Audit found that transcriber, speaker, link TX, loop_recorder, and remote_audio_tx were already off-tick by design.
- **Per-sink drain stats** — `enqueued / drops / drained / errors / drain_total_ms / drain_max_ms / depth_max / depth_now / drain_avg_ms / idle_s / thread_alive` per sink, surfaced via `BusManager.get_sink_stats()`, `GET /sinkstats`, and the `bus_sink_stats` MCP tool. Closes the diagnostic blind spot the off-tick model created.
- **Per-source `get_audio()` timing** — `audio_bus._timed_get_audio` wraps every call site (ListenBus / SoloBus / DuplexRepeater / SimplexRepeater). Counters surfaced via `BusManager.get_source_stats()`, `GET /sourcestats`, and the `bus_source_stats` MCP tool.
- **`TickContext` (frozen dataclass)** — read-side gateway state snapshotted once per tick. `_deliver_audio` and `_handle_listen_tick` consume it instead of probing `self.gateway` live, so a routing reload or config flip mid-tick can no longer half-apply.
- **Tick-owned level meters** — `BusManager._meters` and `_link_tx_meters` own the canonical values; one post-tick mirror copies them to gw so existing UI consumers keep working without learning new attribute paths.
- **Numba-jit noise gate.** `_apply_noise_gate` inner loop moved into `@numba.njit(cache=True)` (`audio_util._gate_loop`). Bit-identical output verified against synthetic transient signal. JIT warmup runs in a daemon thread at module import (`GateJITWarmup`) so the first audio tick that needs the gate never sees the compile cost. Pure-Python fallback present.
- **Mumble sink-side VAD** — `BusManager._mumble_sink_vad(audio)` envelope-follower for non-listen-bus mumble routings. Squelch hiss off the AIOC mic no longer shows as continuous TX when mumble is wired off a solo bus.

### Added — UI

- **Dashboard tabbed redesign.** Controls and dashboard merged into a unified tabbed layout. Smart button status rows, declutter pass on status blocks, panel reordering. Routing page gain via mouse-wheel.
- **Shell redesign.** Site-wide shell template overhauled. Source name normalisation, TTS pre-key PTT, routing-port wiring polish.
- **Recorder page** — upstream RX frequency now shown next to bus name on each segment.

### Changed

- **Bus tick service** uses `python3 radio_gateway.py` directly. The `start.sh` shim was already removed from the repo; the systemd service template still pointed at it. Cleaned up alongside the v3.5 work.
- **Loop playback** streams `/loop/play` directly from ffmpeg now instead of writing a temp file first — lower disk churn, faster preview start.
- **Per-decode transcriber timing log** (`X.Xs audio → Y.Ys process (Z.ZZx realtime)`) gated under `config.VERBOSE_LOGGING`. Was firing on every decode in production logs.
- **Transcriber inference thread** runs at `nice +10` so it can't preempt BusManager.

### Fixed

- **Loop recorder 820 ms BusManager stall.** Old segment close ran synchronously at the rotation boundary and could block the bus tick for hundreds of ms. Now closed asynchronously in a background thread so the tick doesn't see it.
- **Stream auto-reconnect when radio is quiet.** Reconnect was only tried from `send_audio()`, but when the radio is silent that path doesn't fire — so a broken Icecast connection during quiet periods stayed broken. Reconnect now also fires from the keepalive loop.
- **TX level bars decay in the tick.** Previously only HTTP-poll-driven, so the bars stayed lit when the routing page was closed. Decay now happens per-tick.
- **Broadcastify level meter** updates regardless of bus type. Was gated on `_is_listen` from the v1 era — caused the meter to stay at 0 when broadcastify lived on a non-listen bus, even though audio was flowing.
- **`StreamOutputSource` ffmpeg encoder stdin race.** Two threads write PCM into the encoder's stdin: `send_audio()` and `_keepalive_loop()`. After v3.5-A moved broadcastify off-tick, occasional concurrent writes interleaved bytes mid-sample and the MP3 decoded as static. Fixed by serialising both write sites on `_encoder_lock`.
- **`BusManager.reload()` orphaned drain threads.** `stop()` joined the SinkDrain threads but `start()` didn't recreate them, so the next enqueue appended to a deque nothing was draining. Fixed by clearing `_sink_queues / _sink_events / _sink_threads / _sink_stats` after the join.
- **Bus-id tag on source → solo → sink routings** for the transcribe path — was reporting the bus id where the upstream source-id was the right tag.

### Investigated and explicitly skipped

- **E.2 — convert decode-on-demand sources to push-from-reader-thread.** After E.1 instrumentation ran in production for a few minutes, combined source overhead measured ~20 ms/sec (2 % of one core). The supposedly-slow sources (SDR1/SDR2) cost what they cost because of inline IIR filter processing in their per-source `AudioProcessor`, not because they decode on demand inside `get_audio()`. KV4P, the theoretical worst case, isn't routed in production. Pushing `get_audio()` off the tick would have changed nothing measurable. Decision recorded in `docs/v3.5-refactor.md`.

### Installer

- `scripts/install.sh` overhauled. ALSA loopback reduced to a single card (only the packet plugin uses it now). Step counter cleaned up — sections renumbered to a contiguous 1-15. SoapySDRPlay3 AUR build patched to inject `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` (CMake 4.x rejects pre-3.5 minimums). FlightRadar24 makepkg now passes `--nodeps` (the `dump1090` dep has no pacman provider). Stale sudoers rule for `modprobe snd-aloop` removed (snd-aloop is loaded at boot via `/etc/modules-load.d/`; gateway uses `os.nice` directly). New `scripts/darkice.cfg.example` ships in the repo so install.sh's auto-copy step actually has a template to copy. Telegram-bot service install moved out from inside the gateway-service install block.
- Smoke-tested end-to-end on a clean Arch QEMU VM (`Arch-Linux-x86_64-cloudimg`). Documented in install.sh comments.

## [3.4.0] -- 2026-04-22

Deployability release. First version where a fresh clone on a clean Arch / Debian box can be driven to a working install without hand-holding.

### Added
- **`INSTALL.md`** — full fresh-install walkthrough: prereqs, AUR helper setup, credential acquisition (Mumble / Broadcastify / Telegram / GDrive), pre-flight checklist, expected first-start output, runtime-state map, troubleshooting, manual uninstall, companion-repo pointers (TH9800_CAT, kv4p-ht-python, D75-CAT-Control).
- **`requirements.txt`** covering the full Python dep set so `pip install -r requirements.txt` works alongside (or instead of) `scripts/install.sh`.
- **`scripts/install.sh` post-install health check** — surfaces the usual "why won't it start?" problems immediately: snd-aloop loaded, audio group membership, USB device detection (`/dev/kv4p`, `/dev/relay_*`, `/dev/gps`), config placeholders still in place, runtime-binary availability, Python import smoke test.
- **`scripts/install.sh` AUR-helper fail-fast** — detects `yay`/`paru` up front on Arch; prompts before continuing so users know which optional features will be skipped.
- **Annotated `examples/gateway_config.txt`** — "Fresh install minimum" block at the top lists REQUIRED / RECOMMENDED / HARDWARE fields and points at INSTALL.md for per-credential instructions.
- **Loop playback: source-owned clock + meter**, independent of routing. `LoopPlaybackSource` advances position and updates the meter in its reader thread now, so clicking play without wiring `loop_playback` to a bus still ticks the clock and shows activity. Wiring mid-play taps the ongoing stream. Reader paces itself at real-time via `time.sleep`; queue drops oldest on full instead of stalling ffmpeg.
- **Per-bus Export mode on the recorder page** — new Export button replaces the old "Export:" label. Click-drag selects a time range (populates start/end fields) and the cursor becomes a crosshair. Right-click drag still selects anywhere. Bare click in export mode clears rather than plays.
- **Explicit Play / Stop buttons** for loop playback, separate from the Playback mode toggle. Stop no longer exits mode; Play resumes from the last server position.
- **`tunnel_link_url` + `voice_view` MCP tools** — expose the cloudflared tunnel URL (with derived wss:// link target) and the live `claude-voice` tmux pane.
- **Routing page mouse-wheel zoom** on the drawflow canvas; respects Drawflow's `zoom_min`/`zoom_max`.
- **NUL sink** — drop-only bus destination; lets a bus exist (recording, routing anchor) without forwarding audio anywhere.

### Changed
- **CLAUDE.md no longer mandates `/home/user/Downloads/radio-gateway` as the clone path.** Memory sync snippet now derives the auto-memory path from `$(pwd)` so any clone location works.
- **`.mcp.json` uses repo-relative paths** (`cwd: "."`, `args: ["./gateway_mcp.py"]`). No hand-edit required on a new machine.
- **Audio level bars redesigned** across shell / dashboard / routing pages: 18 px track with inset shadow + 70/95 % zone ticks, 8 px centered fill with glow, asymmetric CSS transition (80 ms rise / 250 ms fall) driven by JS exponential smoothing (instant attack / 0.15 decay) for a VU-meter feel.
- **Dashboard layout** — PWRB and the Net/TCP/temps blocks no longer force row breaks; short items flow into the same auto-fill grid.
- **Routing page level-meter bars** ported the shell/dashboard VU-meter aesthetic in miniature (8 px track / 4 px fill).
- **Routing page: selected flowing connections** recolor to the accent stroke like selected inactive ones while keeping the dashed animation, so selection is visible on active lines.
- **TX / RX mute independence** — `kv4p_plugin` / `th9800_plugin` gain a separate `tx_muted` flag. Muting a radio's TX sink no longer silences its RX source.
- **Lazy MP3 encoder** — WS streaming encoder starts on first subscriber and stops when the last leaves, rather than running flat-out from gateway startup.
- **Richer CPU metrics** in `/sysinfo` — split into `cpu_critical_pct` (us+sy+hi+si, real-time pressure), `cpu_background_pct` (nice), `cpu_iowait_pct`, and `load_per_core`.
- **Denoise inference moved off the bus tick** — per-bus neural inference (RNNoise / DFN3) now runs on its own thread so a slow tick doesn't stall audio.
- **SDR: 2 s libusb grace period** between `killall -9 sdrplay_apiService` and `systemctl start sdrplay.service` so the next start doesn't re-claim still-pending USB handles and SEGV.

### Fixed
- **Broadcastify auto-reconnect latched off** after `_connect()` raised — `_reconnecting` stayed True forever. Now wrapped in `try/finally`; a DNS blip or Icecast refusal no longer kills reconnection indefinitely.
- **Routing page TX sink level leak** — client mirrored source RX level onto `<source>_tx` sink bars, poisoning the smoothed history with RX audio. TH9800 TX on a different bus than TH9800 RX now shows only what's actually flowing to the TX, not what RX is hearing.
- **Transcribe bus-id tag** on source → solo → sink routings now correctly identifies the upstream source.
- **`.gitignore`** covers `tools/*_trace.txt` so runtime diagnostic outputs don't keep reappearing as untracked.
- **`tools/kv4p_raw_capture.py`** output paths are now `__file__`-derived instead of hardcoded to `/home/user/Downloads/...`.

### Removed
- **`recording_playback` MCP tool** — was a "not yet implemented" stub with no route behind it.
- **Client-side RX → TX bar mirror** on the routing page (replaced by the server's explicit TX sink levels).

## [3.3.0] -- 2026-04-19

### Added
- **DeepFilterNet 3 denoise engine** — second neural denoiser alongside RNNoise, selectable per bus.
  - `_DFN3Stream` in `audio_util.py`: stateful streaming ONNX (16 MB) via existing onnxruntime. No new Python deps, no numpy conflict. ~40 dB cut on white noise. Model vendored in-repo at `tools/models/dfn3/denoiser_model.onnx` — no runtime download.
  - Engine abstraction: `DenoiseStream` duck-type, `make_denoise_stream(engine)` factory, shared `_mix_with_dry_delay()` helper. `_RNNoiseStream` conforms to the same interface.
  - Per-bus engine selector: routing-page pill (`RNN` / `DFN`) next to the mix slider, click to swap live. Hidden when denoise is off. Per-bus `dfn_atten_db` input (default 18 dB) to cap neural-gate pumping.
  - **Phase-aligned wet/dry mix** with engine-specific dry-path delay (RNN 960 samples / DFN3 1440 samples). Killed the chorus/reverb smear the naive add produced at any mix < 1.0.
  - HTTP: `set_dfn_engine`, `set_dfn_atten`, `set_dfn_mix` handlers. MCP: `bus_set_denoise_engine`, `bus_set_denoise_atten` tools.
  - ONNX session warmup (80 frames, ~350 ms) runs synchronously at startup — eliminates the cold-start bus-tick spikes that caused "first-few-minutes-of-stutters".
  - ORT `intra_op_num_threads = 2` (optimum per benchmark; 3+ regresses on DFN's sequential GRU graph).
- **Per-stream transcription feed workers** — each bus wired to the transcription sink gets its own worker thread (`TranscribeFeed-<bus_id>`). Two buses' audio no longer serialises through one worker. Combined with the D7 refactor, transcription feed-worker load dropped from 25.6 ms mean / 1185 ms max → **1.9 ms / 9 ms**.
- **Moonshine repetition-suppressed decoder** — custom greedy decoder wraps `MoonshineOnnxModel.generate()` with no-repeat-3-gram logit masking + low-diversity early exit. Eliminates the "Anno, Anno, Anno, Anno, …" loops that upstream's pure argmax produced on ambiguous audio.
- **Multi-radio TX on a single solo bus** — `SoloBus.add_extra_tx_radio()` + fan-out in `_fire_ptt` and Phase 3 audio push. Announce → grunge → ftm_tx + aioc_tx now keys both radios simultaneously. Caveat: slight lead/lag possible if the two radios have very different TX settle times.
- **Dominant-source attribution for transcripts** — when multiple sources feed one bus (e.g. SDR1 + SDR2 on the same listen bus), each utterance is now tagged with the actual upstream tuner's frequency rather than the bus id. Tracks per-frame RMS at mix time, picks the mode across the VAD window.
- **Shared `apply_gain()` with tanh soft-clip** — hoisted to `audio_util.py`. All five routing-path gain sites (listen ducker/duckee, solo TX mix, solo RX boost, per-sink gain) now route through it. Gain > 100% saturates smoothly instead of flat-topping into square-wave harmonics.
- **File playback peak-normalisation on decode** — `FilePlaybackSource` now brings quiet files up to −1 dBFS before the gain slider path. Solves "announcements too quiet" complaint without boosting noise.
- **Telemetry** — `transcription_status` exposes per-stream VAD state, per-queue depth, `proc_mean_ms / max_ms`, `worker_count`. Feed-health readout on the transcribe page surfaces it live.
- **Design pass** — phosphor/instrument-panel aesthetic across all pages:
  - Radial vignette + 3% fractal-noise grain overlay
  - 44 px tall level-meter strip in shell bar with inset channels + 70/95% zone ticks + per-channel glow
  - Identity plate: beacon LED (green/warn/dead), callsign, display-font clock
  - Dashboard 2-column layout ≥1400px
  - `.empty-live` scanning sweep + breathing glyph
  - Routing: widened bus nodes (230 → 290 px) + tightened padding; colour-coded sockets (green=source, cyan=bus, red=sink); `.flowing` animated signal flow on active connections
  - SkyAware (ADS-B) iframe styled to match (grey nav + buttons instead of PiAware blue)
  - Logs: Danger dropdown removed; Restart Gateway + Reboot Host buttons inline
  - Transcribe: fixed-width Audio / Speech meters; status-line 110 px pin to stop jitter

### Fixed
- **Chorus / volume pumping on denoise** — see "Phase-aligned wet/dry mix" above. Measured delays: RNN 960, DFN3 1440.
- **NameError in bus_manager transcription dispatch** — referenced `bus` where only `bus_id` was in scope. Silently caught by try/except, so feed() was never called. Transcription appeared dead.
- **Dual-tuner SDR2 not capturing when wired to a solo bus** — `sync_listen_bus` only counted listen-bus connections, so tuner2 got stopped as "not routed". Now splits into `tuner_needed` (any bus → keeps tuner alive) vs `should_be_on` (listen bus only → add to listen-bus mix).
- **Removed Recording sink stub** — was a v1 leftover that never got a v2.0 implementation (`pass` in the dispatcher, level hardcoded to 0). Loop Recorder's per-bus R button is the actual mechanism. One-time migration strips dangling `bus → recording` connections on load.
- **WCAG AA contrast** — `--t-text-mute` raised from `#4d5a68` (2.7:1) to `#6b7a8a` (4.5:1).
- **Concurrency** — feed-stats lock, non-blocking `_update_lock` in link_endpoint, GIL-safe deque docs.
- **Resource leaks** — ONNX session + per-stream denoise state released in `transcriber.stop()`.

### Removed
- **ASR-path denoise duplicate** — D7 refactor collapsed the two denoise paths into one. Transcription sink inherits whatever the bus already processed. One knob (per-bus D + engine + mix + cap). No more double-denoise footgun; ~200 lines of duplicate code gone.
- **Recording sink node + handlers** — see "Removed Recording sink stub" above.

## [3.2.0] -- 2026-04-19

### Added
- **Moonshine ASR** — replaced Whisper with Moonshine ONNX (`useful-moonshine-onnx`). English-only, CPU-efficient. Real-time on Haswell i5 at base model. `StreamingTranscriber` removed; single utterance-close path.
- **Silero VAD** — replaced dBFS envelope follower with Silero v5 ML speech classifier. Probability threshold (0.0–1.0, default 0.5) with hysteresis (exit = threshold − 0.15). Ignores squelch tails, DTMF, pilot tones, carrier noise. Smoothed probability bar for UI polling (fast-attack 0.5, slow-decay 0.05).
- **RNNoise neural denoise** — per-bus "D" toggle button in routing page with wet/dry mix slider. Shared singleton via pyrnnoise ctypes binding; per-bus stream state. Also available on ASR path via transcribe controls. Soft-clip (tanh) on audio boost path to prevent Silero detection regression.
- **Anti-aliased ASR resampling** — `scipy.signal.resample_poly(audio_48k, 1, 3)` replaces bare `audio_48k[::3]` decimation.
- **Hallucination blocklist** — post-transcription filter drops common no-speech outputs.
- **30-second utterance cap** — hard buffer limit independent of `TRANSCRIBE_MAX_BUFFER`; prevents OOM on stuck-open VAD.
- **Transcript source + frequency** — each entry shows radio name and tuned frequency (e.g. `SDR1 · 446.760 MHz`). TH-9800 reads left VFO from `cat_client._vfo_text['LEFT']`.
- **SDR single-tuner multi-channel mode** — RSPduo one tuner at configurable sample rate (up to 10.66 MHz BW) with up to 2 demodulated channels. Band overview visualisation. Auto-center. 57% CPU reduction vs dual-tuner at equivalent channel count.
- **SDR1/SDR2 as independent routing nodes** — each tuner channel independently routable to any bus.
- **Google Drive integration** — Cloudflare tunnel URL published to Drive as `tunnel_url.json`. Drive file list, storage stats, and publish button on `/gdrive`.
- **Packet auto-discovery** — Gateway Link AIOC endpoint discovered via mDNS. Internal AGWPE proxy eliminates per-endpoint Pat configuration.
- **Gateway Link** — endpoint self-update; internet WebSocket transport with auto-upgrade to LAN TCP; Pi Zero 2W support; jitter buffer; async TX sends.
- **Broadcastify health monitoring** — byte-rate and RTT tracking with alerts.
- **Bus rename** — double-click bus name on routing page for inline editing.
- **Gain slider reset** — double-click any gain slider to reset to 100%.
- **UI redesign** — phosphor/instrument-panel theme across all 20 pages. JetBrains Mono throughout, cyan reserved for live signals, green/amber/red signal vocabulary. See commit history `ui-redesign` series.

### Fixed
- **Routing: selected node background** — overrides Drawflow's bundled `background:red`.
- **Packet AGWPE session cap** — `_AGWPE_MAX_SESSIONS = 10` prevents unbounded sessions.
- **Loop recorder toggle-off** — `stop(bus_id)` called immediately; disabled buses filtered from API.
- **Link endpoint noise gate** — default threshold raised −48 → −40 dB; settings persist.
- **PCM WebSocket stutter** — audio pushed from bus tick thread; duplicate main-loop push removed.
- **Stuck PTT** — level threshold, bus reload cleanup, 60s safety timeout.

### Removed
- **Whisper / faster-whisper / ctranslate2** — fully replaced by Moonshine.
- **Streaming transcription mode** — `StreamingTranscriber` and `mode` config field removed.
- **Legacy D75 plugin** — `d75_plugin.py` deleted; D75 is link-endpoint-only.

## [3.1.0] -- 2026-04-09

### Added
- **SDR single-tuner mode** — RSPduo runs one tuner with multi-channel demodulation
  - Mode selector on `/sdr` page and `sdr_set_mode` MCP tool
  - Configurable sample rate (0.25–10.66 MHz) and center frequency
  - Bandwidth visualization showing channel positions within tunable band
  - Per-channel audio level bars in channel editor
  - Auto-center button calculates optimal center freq and sample rate
  - Max 2 channels with independent PipeWire sinks for per-channel routing
  - SDR CPU reduced 57% (31% → 13%) at 1 MHz sample rate
  - Mode, channels, and device settings persist across restarts
  - Closed-loop controls: every action verifies and reports outcome
  - Full stream trace instrumentation (overflow, underrun, slow drain, timing)
- **SDR1/SDR2 as separate routing nodes** — each tuner channel independently routable
  to any bus (no more internal-only ducking)
- **Bus rename** — double-click bus name on routing page for inline editing
- **Gain slider reset** — double-click any gain slider to reset to 100%
- **Alphabetical bus sort** in routing auto-arrange
- **`audio_util.py`** — shared level metering module (`pcm_level`, `pcm_db`, `pcm_rms`,
  `rms_to_level`, `update_level`), AudioProcessor, and CW generation extracted from
  audio_sources.py; used by all plugins
- **`_resolve_source()`** in web_routes_post.py — unified plugin + link endpoint
  attribute lookup for duck/boost/mute
- **Web UI shared code** — `common.js` expanded with `postJson`, `getJson`,
  `createPoller`, `sendKey`, `openTmux`, formatting helpers; `common.css` expanded
  with status colors, layout grid, level bars
- **Bus display names** shown in loop recorder dashboard and recorder page

### Fixed
- **Loop recorder toggle-off** — `stop(bus_id)` called immediately when loop flag
  toggled off; disabled buses filtered from API
- **Link endpoint noise gate** — default threshold raised -48 → -40 dB (AIOC noise
  floor was passing through); gate settings now persist in endpoint `settings.json`
- **LinkAudioSource TX metering** — `put_audio()` now updates `tx_audio_level` so
  TX nodes show activity on routing page (affects all link endpoints)
- **common.js load order** — fixed controls.html and recordings.html where common.js
  loaded after inline scripts that depend on it
- **Duplicate kv4p_plugin init** and no-op self-assignment in reconnect handler
- **Controls page responsive layout** — fixed-width tiles replaced with flex:1 tiles;
  inline container styles moved to CSS classes

### Removed
- **Legacy D75 plugin** — `d75_plugin.py` (730 lines) deleted; all d75_plugin
  references removed from 11 files (~1,136 lines total). D75 is now link-endpoint-only.
- **Duplicate level metering** — ~55 inline RMS→dB→level→decay patterns replaced
  with `audio_util` calls across 10 files (-282 lines)

## [3.0.0] -- 2026-04-07

### Architecture
- **Listen bus unified into BusManager** — single code path for all bus types
  - Primary listen bus moved from gateway_core main loop into BusManager
  - All buses (listen, solo, duplex, simplex) share one tick loop and delivery path
  - Main loop simplified to SDR rebroadcast TX and WebSocket push
  - Net reduction of ~500 lines from gateway_core.py

### Added
- **Loop Recorder** — per-bus continuous recording with visual waveform review
  - Enable with "R" button per bus in routing UI
  - Segmented MP3 storage (5-min chunks) with configurable retention (1h to 7d)
  - Canvas-based waveform viewer with zoom, pan, click-to-play
  - Right-click drag to select time range for export (MP3 or WAV)
  - Stacked multi-bus view with independent playback per bus
  - Dashboard panel with per-bus stats (segments, disk usage, write rate)
  - Real-time waveform from active segments (no delay for segment close)
  - HTTP Range support for native browser seeking
  - See [docs/loop-recorder.md](docs/loop-recorder.md) for full guide
- **Plugin auto-discovery** — drop a .py file in `plugins/`, add config flag, restart
  - No gateway code changes needed to add a new radio
  - Template at `plugins/example_radio.py` with detailed comments
  - Developer guide at [docs/plugin-development.md](docs/plugin-development.md)

### Fixed
- Status API: darkice_pid, darkice_restarts, stream_restarts were hardcoded
- TH9800: audio_level computed after processing (noise gate now squelches level bar)
- Shell nav bar: fixed-width buttons (no layout shift when streaming)

### Changed
- Shell nav bar: stream timer shown inside button, indicator dots removed
- Default volume sliders at 50% (was 100%)

## [2.0.0] -- 2026-03-31

### Architecture
- Bus-based audio routing replacing monolithic AudioMixer
  - 4 bus types: Listen, Solo, Duplex Repeater, Simplex Repeater
  - Per-bus audio processing, ducking, and stream controls
  - Bus mute, sink mute, source mute with visual feedback
- All radios refactored as plugins: SDRPlugin, TH9800Plugin, D75Plugin, KV4PPlugin
  - Standard `get_audio()`/`put_audio()` interface for bus routing
  - Hardware-specific methods for UI controls
  - Per-plugin processing chains (gate/HPF/LPF/notch/gain)
- All sinks gated by routing connections (no implicit audio flow)
- Visual routing UI with Drawflow node editor (sources | busses | sinks)
  - Live level bars in source/sink nodes
  - Mute buttons and gain sliders in nodes
  - Save/load routing configurations

### Added
- Full duplex Remote Audio (Windows client on ports 9600/9602)
- Direct Icecast streaming (replaced DarkIce/FFmpeg/ALSA loopback pipeline)
- Mumble as routable source and sink (MumbleSource with PTT control)
- Room Monitor as routable source with VAD
- Web Mic in nav bar (accessible from all pages)
- Speaker virtual mode (prevents PipeWire feedback loops)
- 14 new MCP tools for routing and automation
- BusManager: runs routing-configured busses alongside main loop

### Removed
- Console/terminal UI: StatusBar, keyboard handler, ANSI display (~650 lines)
- Old AudioMixer and AIOCRadioSource (~900 lines)
- 13 `_generate_*` web methods (~5400 lines from web_server.py)
- Dead PTT code and old AIOC audio paths
- Diagnostic trace prints
- Backward compatibility aliases (d75_cat, d75_audio_source, kv4p_cat, kv4p_audio_source)

### Changed
- Web pages extracted to static HTML (13 pages in web_pages/)
- Controls page streamlined
- 13 static page routes consolidated to single `_STATIC_PAGES` lookup
- Utility classes extracted to gateway_utils.py (DDNSUpdater, EmailNotifier, CloudflareTunnel)
- TH-9800 AIOC init replaced with TH9800Plugin
- SDR init simplified (~80 lines to ~15 lines via SDRPlugin)
- Main loop 8-tuple replaced with BusOutput consumption
- Blocking audio reader replaces PortAudio callback

## [1.7.0] -- 2026-03-27

### Added
- Gateway Link: duplex audio + command protocol with plugin architecture (`gateway_link.py`)
  - Framed TCP protocol: `[type 1B][length 2B][payload]` -- 5 frame types (AUDIO/COMMAND/STATUS/REGISTER/ACK)
  - `RadioPlugin` base class for hardware abstraction (setup/teardown/get_audio/put_audio/execute/get_status)
  - `AudioPlugin`: generic sound card via PyAudio (any ALSA/PipeWire device)
  - `AIOCPlugin`: finds AIOC device via `/proc/asound/cards` (not PyAudio)
  - `tools/link_endpoint.py`: standalone endpoint script with plugin registry, gain control, status reporter
  - `LinkAudioSource`: mixer integration with level metering, audio boost, duck support
  - Config: `ENABLE_GATEWAY_LINK`, `LINK_PORT`, `LINK_AUDIO_PRIORITY`, `LINK_AUDIO_DUCK`, `LINK_AUDIO_BOOST`, `LINK_AUDIO_DISPLAY_GAIN`
- Multi-endpoint support: N simultaneous connections, dict keyed by endpoint name
  - Dynamic `LinkAudioSource` creation/destruction per endpoint
  - Per-endpoint controls on `/controls` page (PTT button, RX/TX level bars, gain sliders, mute buttons)
  - Per-endpoint settings persisted to `~/.config/radio-gateway/link_endpoints.json`
  - RX/TX gain controls in dB (-10 to +10), persisted per endpoint
  - RX/TX mute (gateway-side, per-endpoint)
  - VAD-gated level bars
- Command language: `ptt`, `rx_gain`, `tx_gain`, `status` + ACK responses
- PTT safety timeout (60s auto-unkey)
- Bidirectional heartbeat (5s interval) with dead peer detection (15s)
- 10s socket timeout on both sides for cable-pull detection
- mDNS auto-discovery: gateway publishes `_radiogateway._tcp`, endpoint discovers via `avahi-browse`
- Zero-config endpoint usage: `python3 link_endpoint.py --name pi-aioc --plugin aioc`
- `docs/gateway_link.md`: comprehensive architecture and protocol documentation
- LINK audio bar (orange) on dashboard

### Fixed
- Client deadlock: `_send` calling `_close` while holding lock
- Reader cleanup: only calls `on_disconnect` if it owns the entry
- `/linkcmd` missing `return` caused config wipes on POST
- Config page `_CONFIG_LAYOUT` must include all sections or Save wipes unlisted ones

## [1.6.0] -- 2026-03-26

### Added
- D75 BT TX audio via SCO (48-byte frame splitting + paced TX thread at 3ms interval)
- D75 memory channel load via FO (ME-to-FO field mapping with lockout field skip)
- Room Monitor: browser page (`/monitor`) + Android APK (`tools/room-monitor-app/`)
  - `WebMonitorSource`: no PTT, priority 5, `/ws_monitor` WebSocket endpoint
  - Browser: getUserMedia with processing disabled, gain 1x-50x, client-side VAD
  - Wake Lock API + silent audio loop to prevent tab suspension
  - Android Kotlin app with foreground service, UNPROCESSED mic, partial wake lock
  - `/monitor-apk` route serves APK download
- SDR click suppressor (>800 sample jump interpolation, per-source + output)
- Broadcast-style additive audio mixing with soft tanh limiter (knee 24000, max 32767)
- Cloudflare tunnel URL displayed in System Status and startup email
- Broadcastify status panel on dashboard (uptime/sent/rate/RTT/health/PID)
- `/controls` page (control groups moved from dashboard)
- Audio level bars in shell frame (always visible across all pages)
- ADS-B dark mode with NEXRAD weather overlay, centered Santa Ana CA, US mil layers
- Telegram status checks bot process regardless of `ENABLE_TELEGRAM` flag
- D75 proxy: battery level, TNC status, beacon type status reporting
- 15s btstart retry loop for D75 BT reconnection
- TX Talkback config (`TX_TALKBACK` in `[ptt]` section, default off)

### Changed
- Web UI restructure: compact 0.8em nav, no page titles, no footer, inline MP3/PCM controls
- Audio bars reordered: RX, TX, KV4P, D75, SDR1, SDR2, SV, AN, SP, MON, LINK
- D75 PTT: fire-and-forget `sendall()` (no audio thread blocking via `_send_cmd`)
- D75 proxy SM poll: 0.5s to 3s with exponential backoff (up to 30s after failures)
- D75 proxy init: deferred FO/SM/PC queries (skip on fresh BT connect)
- KV4P logging gated behind `VERBOSE_LOGGING`
- Email linkifier supports `wss://` and `ws://` URLs
- README rewritten: 2822 to 1073 lines with collapsible detail sections
- Code defaults updated to match production config (ENABLE_ADSB, ENABLE_DDNS, etc.)

### Fixed
- D75 `connected` status showed TCP as radio-connected (now requires `serial_connected`)
- D75 `_recv_line` EOF did not set `_connected=False` (poll thread never reconnected)
- D75 `close()` killed poll thread reconnect loop (added `_disconnect_for_reconnect`)
- D75 reconnect handler: missing `D75CATClient` import in `web_server.py`
- D75 ME-to-FO lockout field shift (ME[14] not present in FO 21-field format)
- D75 ME field[2] dual meaning (offset vs TX freq for cross-band repeater)
- D75 TX audio silent (SCO SEQPACKET requires 48-byte frames, not arbitrary sizes)
- D75 TX stutter (burst delivery replaced by paced TX thread)
- D75 playback JS newline syntax error
- MON bar: float percentage values and stuck level after WebSocket disconnect
- ADS-B map broken by stray `false` in `layers.js` europe.push calls
- Config file damage from `Edit` tool `replace_all` on multi-line values
- `btstart` non-blocking: proxy returns immediately, BT connects in background
- `btstart` button shown during auto-connect (added `_btstart_in_progress` flag)

## [1.5.0] -- 2026-03-25

### Added
- MCP server (`gateway_mcp.py`): 31 stdio tools for AI control of the gateway
  - 20 core tools: gateway_status, sdr_status/tune/restart/stop, cat_status, radio_ptt/tts/cw/ai_announce/set_tx/get_tx, recordings_list/delete, gateway_logs/key, automation_trigger, audio_trace_toggle, telegram_reply, system_info
  - 11 additional tools: radio_frequency, d75_status/command/frequency, kv4p_status/command, mixer_control, recording_playback (stub), config_read, telegram_status, process_control
- `/mixer` HTTP endpoint: dedicated mixer control (7 actions: status/mute/unmute/toggle/volume/duck/boost/flag/processing)
- D75/KV4P per-source audio processing buttons (Gate/HPF/LPF/Notch) with live highlighting
- HEADLESS_MODE in start.sh (skips Mumble GUI launch)

### Changed
- Mixer sources list expanded: global, tx, rx, sdr1, sdr2, d75, kv4p, remote, announce, speaker

## [1.4.0] -- 2026-03-24

### Added
- TH-D75 Bluetooth radio integration (`D75CATClient` in `cat_client.py`)
  - Remote BT proxy (`scripts/remote_bt_proxy.py`) on ports 9750 (CAT) / 9751 (audio)
  - FO command support (21-field format, LA3QMA/Hamlib spec)
  - Channel load via `d75GoChannel` with band switching
- Telegram bot (`tools/telegram_bot.py`): phone control via text and voice
  - Voice notes: ffmpeg-to-PCM at real-time rate via ANNIN port 9601
  - Text messages injected into Claude tmux session for MCP processing
- Smart Announcements rewritten: single `claude -p` backend (replaced 5 old backends)
  - `smart_announce.py`: 300 lines (down from 1300), no external dependencies
- D75 starts muted by default (prevents SDR ducking from idle noise)

### Changed
- SDR_SIGNAL_THRESHOLD raised from -70 to -45 dBFS (D75 noise at -65 was permanently ducking SDRs)

### Fixed
- ANNIN level bar stuck at last value after voice note (reset to 0 when queue drains)
- D75 btstart blocking caused protocol desync (made non-blocking with background thread)
- D75 `_do_btstart` skipped `serial.connect()` step
- D75 poll thread self-join crash in `close()` (thread identity check added)
- D75 tone/shift/offset wrong FO indices (4 layered bugs: 11-field vs 21-field, wrong positions, gateway timeout crash)

## [1.3.0] -- 2026-03-23

### Added
- SDR post-duck audio handling improvements
- Re-duck inhibit timer (REDUCK_INHIBIT_TIME = 2.0s)

### Fixed
- SDR post-duck stutter: removed aioc_ducks_sdrs gate, added re-duck inhibit + fade-in reset
- MCP sdr_tune wrong payload keys

## [1.2.0] -- 2026-03-21

### Added
- ADS-B aircraft tracking (dump1090-fa + lighttpd + FlightRadar24 feed)
- Gateway reverse proxy for ADS-B (`/adsb/*`)
- TH-9800 auto serial connect on startup

### Fixed
- TH-9800 PTT blind toggle state inversion (switched to explicit `!ptt on`/`!ptt off`)

## [1.1.0] -- 2026-03-19

### Added
- KV4P HT radio support (`KV4PAudioSource`, CP2102 USB-serial, Opus codec)

### Fixed
- KV4P TX 20% audio dropout
- KV4P CTCSS off-by-one (DRA818 uses 38 tones, not TH-9800's 39-tone list)

## [1.0.0] -- 2026-03-13

### Fixed
- DISPLAY_TEXT VFO misattribution (vfo_byte from packet, not stale `_channel_vfo`)
- RTS change corrupts display text
