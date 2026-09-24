# Transcription Pool

The gateway's transcription system is multi-machine. VAD runs locally; ASR inference can run locally, on a remote worker, or be routed between both by clip length. This doc covers the architecture, configuration, deployment of a remote worker, telemetry, and tuning.

## Why a pool?

The two ASR engine families have opposite cost profiles:

| Engine | Cost model | Wins on |
|--------|-----------|---------|
| **Moonshine** (tiny / base) | Linear with clip length, no fixed window | Short utterances (callsigns, brief replies) |
| **Whisper** (small.en / medium.en / large-v3-turbo) | ~30-second encoder cost per call + decode | Longer transmissions where accuracy matters |

Whisper was trained on fixed 30-second windows. A 1-second clip gets padded with 29 seconds of silence and the encoder still chews through all 30 seconds — so a single short clip on Whisper takes about the same wall-clock as a 30-second clip. That's wasteful when 70% of your utterances are short callsigns.

The pool dispatcher solves this by routing each utterance to the engine that fits it. Plus you can scale horizontally by adding more remote workers, and the system degrades gracefully if a worker disappears.

## Architecture

```
            ┌──────────────────────────── Gateway ────────────────────────────┐
            │                                                                  │
   Audio    │   ┌─────────┐    ┌───────────────────┐    ┌──────────────────┐  │
   buses ───┼──▶│ Silero  │───▶│ Pool dispatcher   │───▶│ LocalInference   │──┼──┐
            │   │ VAD     │    │ _pick_worker()    │    │ Engine (in-proc) │  │  │
            │   └─────────┘    └─────────┬─────────┘    └──────────────────┘  │  │
            │                            │                                     │  │
            │                            │              ┌──────────────────┐  │  │
            │                            └─────────────▶│ RemoteEngine     │──┼──┼──▶ HTTP POST /transcribe
            │                                           │ (HTTP client)    │  │  │       │
            │                                           └──────────────────┘  │  │       │
            └────────────────────────────────────────────────────────────────┘  │       │
                                                                                 │       │
            Wall-clock VAD timestamp → DB/log (ordered by start_time, ─────────┘       ▼
            not arrival order, so out-of-order pool completions stay sorted)    ┌──────────────────┐
                                                                                 │ transcribe_worker│
                                                                                 │ on remote host   │
                                                                                 │ (Mac Mini, etc.) │
                                                                                 └──────────────────┘
```

Key modules:

| File | Role |
|------|------|
| `transcribe_engine.py` | `LocalInferenceEngine`, `RemoteEngine`, `_pick_worker()` — shared by gateway and worker |
| `transcriber.py` | VAD, dispatcher loop, result ordering, forwarding to Mumble/Telegram |
| `tools/transcribe_worker.py` | Standalone HTTP server — runs on the remote host |

## Pool modes

Set via `TRANSCRIBE_MODE` in `gateway_config.txt`:

| Mode | Local engine | Remote engines | Behaviour |
|------|--------------|----------------|-----------|
| `off` | — | — | Transcription disabled |
| `local` | Yes | — | All utterances run on the gateway process |
| `remote` | — | Yes | All utterances POSTed to remote workers |
| `pool` | Yes | Yes | Both available; dispatcher routes per utterance |

## Routing strategies

The dispatcher's selection algorithm:

1. **Filter to ready workers** — engines whose model is loaded and (for remote) whose last `/status` poll succeeded. An unreachable remote is skipped, so a long clip falls back to the local engine instead of POSTing into the void.
2. **Length-based tiering** (when `TRANSCRIBE_SPLIT_THRESHOLD_SECS > 0`):
   - Clip duration < threshold → prefer engines with `engine='moonshine'`
   - Clip duration ≥ threshold → prefer engines with `engine='whisper'`
   - Soft fallback to the other tier if the preferred is empty
3. **Within the chosen tier, pick least-busy** — by `_inflight` counter

When `TRANSCRIBE_SPLIT_THRESHOLD_SECS = 0` the engine type is ignored and least-busy across the whole pool wins.

## Gateway config

```ini
ENABLE_TRANSCRIPTION = true

# Mode: off | local | remote | pool
TRANSCRIBE_MODE = pool

# Local engine model (engine/size). When mode is local or pool, this is what
# runs inside the gateway process. Whisper requires faster-whisper installed.
TRANSCRIBE_MODEL = moonshine/base

# Remote worker URLs — comma-separated. When mode is remote or pool, each URL
# is a transcribe_worker.py instance reachable over HTTP.
TRANSCRIBE_REMOTE_URLS = http://192.168.2.143:9800

# Length-based routing threshold in seconds (pool mode only).
#   0  = least-busy across whole pool, no length routing
#   >0 = clips under N sec → Moonshine tier, longer → Whisper tier
TRANSCRIBE_SPLIT_THRESHOLD_SECS = 10
```

All other transcription settings (VAD threshold, audio boost, alert keywords, Mumble/Telegram forwarding) live in `.transcribe_settings.json` and are edited via the `/transcribe` page.

## Remote worker

`tools/transcribe_worker.py` is a self-contained HTTP server that wraps `LocalInferenceEngine`. Drop it on any Linux box with Python 3.10+ and either `useful-moonshine-onnx` or `faster-whisper` installed.

### Deployment

```bash
# On the remote host
mkdir -p ~/transcribe
# (scp transcribe_engine.py + transcribe_worker.py here)
pip install --break-system-packages useful-moonshine-onnx faster-whisper

# Manual smoke test — --gateway makes the worker announce itself
python3 -u transcribe_worker.py --model whisper/medium.en --port 9800 \
    --gateway http://192.168.2.140:8080 --name macmini
```

You should see:
```
[worker] Loading whisper/medium.en...
[worker] Registering with gateway http://192.168.2.140:8080 as macmini
[worker] Listening on 0.0.0.0:9800
[worker] Registered with gateway http://192.168.2.140:8080 as macmini (status=registered, url=http://192.168.2.143:9800, ttl=90s)
[worker] Model ready
```

### Self-registration (preferred)

With `--gateway`, the worker dials into the gateway and announces itself —
the same direction of travel as a link endpoint's REGISTER on :9700. The
gateway takes the worker's address **off the socket**, so the worker never
needs to know its own IP and the gateway never needs one in config. A DHCP
move fixes itself on the next heartbeat.

| Flag | Default | Meaning |
|---|---|---|
| `--gateway URL` | *(unset — env `GATEWAY_URL`)* | Gateway base URL, e.g. `http://192.168.2.140:8080`. Registration is off when unset. |
| `--name NAME` | hostname | Label shown on the `/transcribe` page, the worker's card on `/dashboard/endpoints`, and in `transcription_workers`. |
| `--register-interval SECS` | 30 | Heartbeat period, auto-clamped to the gateway's TTL/3. |
| `--gateway-password PW` | *(env `GATEWAY_PASSWORD`)* | Only needed if `WEB_CONFIG_PASSWORD` is set on the gateway. |

How it behaves:

- The first heartbeat goes out **while the model is still loading**, so the
  worker shows up as present-but-loading rather than missing for ~30 s.
- Heartbeats are unconditional and forever — the worker never assumes an
  earlier registration stuck. A gateway restart re-populates the pool within
  one heartbeat with nothing to do by hand.
- Miss `TRANSCRIBE_WORKER_TTL_SECS` (default 90 s) of heartbeats and the
  gateway drops the worker from the pool and logs `Worker expired:`.
- **Config-pinned URLs are never expired.** A worker listed in
  `TRANSCRIBE_REMOTE_URLS` stays in the pool showing `unreachable` when it's
  down, because the operator asked for it explicitly. Registration and pinning
  can coexist; a worker that registers a URL already pinned is a no-op.
- Nothing discovered is written back to `.transcribe_settings.json` — that
  would recreate the stale-address problem this exists to remove.

Gateway-side knobs (`gateway_config.txt`):

```ini
# Accept worker self-registration (default true)
TRANSCRIBE_ALLOW_WORKER_REGISTRATION = true
# Drop a registered worker after this many seconds without a heartbeat
TRANSCRIBE_WORKER_TTL_SECS = 90
```

Check who's in the pool from the gateway:

```bash
curl -s localhost:8080/transcriptions?since=0 | python3 -m json.tool | grep -A20 registration
```

or the `transcription_workers` MCP tool, which prints the pool plus the
accept/refresh/expire/drop counters.

### Systemd user service

```ini
# ~/.config/systemd/user/transcribe-worker.service
[Unit]
Description=Radio Gateway Transcription Worker
After=network.target

[Service]
WorkingDirectory=/home/user/transcribe
Environment=WHISPER_CPU_THREADS=4
ExecStart=/usr/bin/python3 -u transcribe_worker.py --model whisper/medium.en --port 9800 \
    --gateway http://192.168.2.140:8080 --name macmini
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user daemon-reload
systemctl --user enable --now transcribe-worker
journalctl --user -u transcribe-worker -f
```

### HTTP interface

`POST /transcribe`
- Body: raw float32 little-endian PCM bytes at 16 kHz mono
- Response: `{"text": "...", "proc_time": 1.23}`
- 503 if model isn't loaded yet

`POST /model`
- Body: `{"model": "whisper/large-v3-turbo"}`
- Async — returns `202 Accepted` immediately, loads in background, stats reset, old model released via `malloc_trim`
- Failure surfaces in next `/status` call as `last_switch_error`

`GET /status`
- JSON: `model_loaded`, `model_key`, `engine`, `total_transcriptions`, `errors`, `avg_ratio`, `uptime_secs`, `ram_mb`, `cpu_temp_c`, `fan_rpm`, `last_switch_error`

The gateway polls `/status` every 10 seconds on each configured remote worker.

## Searching the log

Every transcription is stored in a SQLite log with an FTS5 index. The `/txlog`
page searches it through `GET /transcript_search?q=<expr>&limit=N`, and the
`transcription_search` MCP tool does the same. FTS5 syntax is passed straight
through: quoted phrases, `AND` / `OR` / `NOT`, `NEAR(a b)`, and prefix `wea*`.
A malformed expression returns an `invalid query` error rather than a 500. Each
hit reports whether the loop recorder still holds its audio.

## Telemetry

Per-engine fields surface in `/transcriptions` → `status.workers[]`:

| Field | Local | Remote | Meaning |
|-------|-------|--------|---------|
| `type` | `local` | `remote` | Source location |
| `model_key` | ✓ | ✓ | e.g. `moonshine/base`, `whisper/medium.en` |
| `engine` | ✓ | ✓ | `moonshine` or `whisper` — used by length-based routing |
| `model_loaded` | ✓ | ✓ | Ready to process |
| `reachable` | — | ✓ | Last `/status` poll succeeded |
| `inflight` | ✓ | ✓ | Currently being processed |
| `dispatched` | ✓ | ✓ | Gateway-side completed count (monotonic) |
| `avg_ratio` | ✓ | ✓ | proc_time / audio_duration across all calls |
| `ram_mb` | ✓ | ✓ | Process RSS on that host |
| `cpu_temp_c` | ✓ | ✓ | Highest `/sys/class/thermal/thermal_zone*/temp` |
| `fan_rpm` | ✓ | ✓ | First fan via `applesmc` or `hwmon` |
| `last_switch_error` | — | ✓ | Surface failures from `/model` swaps |

These render as cards in the Workers row of the `/transcribe` page with the unified VU-meter style, and as endpoint-style status cards on `/dashboard/endpoints` (each active worker gets the same card treatment as a Gateway Link endpoint, plus a Workers lamp on the `/dashboard` annunciator). The Fleet Manager's hourly check pulls the same payload and flags elevated state on backlog, thermals, unreachable workers, etc.

## Memory + thermal notes

Two gotchas surfaced building this:

**Memory leaks on model swap** — Python `gc.collect()` is necessary but not sufficient on Linux. faster-whisper's CTranslate2 backend allocates in native heap which Python doesn't free back to the OS without explicit `malloc_trim(0)` via libc. The worker calls this after every swap. Without it, repeated model switches accumulated ~1.5 GB per swap.

**Thermal throttling on passively cooled CPUs** — `faster-whisper.WhisperModel` defaults to using every CPU thread. On a fanless or marginally-cooled CPU (e.g. 2013 Mac Mini i7-3720QM) this pushed package temp to 102°C and triggered severe thermal throttling — inference actually ran *slower* with all threads than at half-thread count. The worker reads `WHISPER_CPU_THREADS` from env (default = `cpu_count() // 2`); start at 4 on a quad-core box and adjust based on temp readings in the Workers row.

## Out-of-order completion handling

Pool dispatch means a long utterance sent to worker A can finish after a shorter one sent to worker B even if it started first. Results are inserted into the log sorted by `start_time` (the wall-clock when VAD closed the utterance) rather than completion order, so chronological order is preserved everywhere.

## Failure modes

| Scenario | Behaviour |
|----------|-----------|
| Remote down, short clip | Goes to local Moonshine — no change |
| Remote down, long clip | Falls back to local Moonshine (with worse fit) — clip still gets transcribed |
| Local broken, short clip | Falls back to remote — clip still gets transcribed |
| All engines unhealthy | Dispatcher picks least-busy anyway so the error appears in logs rather than being hidden |
| Worker crashes mid-request | HTTP error surfaces in gateway log; engine flagged unreachable on next poll cycle |
| Model swap fails | `last_switch_error` populated for next `/status` poll; old model keeps serving |

## See also

- [docs/gateway_link.md](gateway_link.md) — protocol for remote *radio* endpoints (the worker uses a similar pattern but over HTTP not a custom protocol)
- [docs/mixer-v2-design.md](mixer-v2-design.md) — bus architecture and how the transcription sink fits in
