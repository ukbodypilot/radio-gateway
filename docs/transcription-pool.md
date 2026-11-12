# Transcription Pool

The gateway's transcription system is multi-machine. VAD runs locally; ASR inference can run locally, on remote workers, or across both. This doc covers the architecture, configuration, deployment of a remote worker, telemetry, and tuning.

## Engines

| Model key | Package | Notes |
|-----------|---------|-------|
| `parakeet/tdt-0.6b-v2` (default) | `onnx-asr[cpu,hub]` | NVIDIA Parakeet TDT 0.6B, English, int8 ONNX. ~630 MB weights from Hugging Face on first load |
| `moonshine2/small`, `moonshine2/medium` | `moonshine-voice` | Moonshine v2 streaming models, own bundled runtime. ~100-300 MB on first load |

Both cost linearly with clip length (no fixed window) and handle short and long clips alike, so the dispatcher just balances load. Moonshine v1 (`moonshine/*`) and Whisper (`whisper/*`) were removed 2026-11-12; a saved or configured key for either falls back to Parakeet with a log line, on the gateway and on workers.

Measured 2026-11-12 on 43 VAD-cut clips (402 s) from the loop recordings, i5-4590 host
(RTF = processing time / audio time, lower is faster; 4 threads unless noted):

| Model | RTF | Words recovered | 50 s clip |
|-------|-----|-----------------|-----------|
| moonshine/base (removed) | 0.11 | 736 | 13 words (collapses) |
| moonshine2/small | 0.18 | 1034 | 177 |
| moonshine2/medium | 0.25 | 985 | 170 |
| parakeet/tdt-0.6b-v2 | 0.11 (0.18 at the default 2 threads) | 1054 | 183 |

Local inference uses half the cores by default; override with the env var `TRANSCRIBE_CPU_THREADS` (`WHISPER_CPU_THREADS` is still honoured for existing worker units).

A pool lets you scale horizontally by adding remote workers, and degrades gracefully if a worker disappears.

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
| `transcriber.py` | VAD, dispatcher loop, result ordering, forwarding to Mumble, keyword email alerts |
| `tools/transcribe_worker.py` | Standalone HTTP server — runs on the remote host |

## Pool modes

Set via `TRANSCRIBE_MODE` in `gateway_config.txt`:

| Mode | Local engine | Remote engines | Behaviour |
|------|--------------|----------------|-----------|
| `off` | — | — | Transcription disabled |
| `local` | Yes | — | All utterances run on the gateway process |
| `remote` | — | Yes | All utterances POSTed to remote workers |
| `pool` | Yes | Yes | Both available; dispatcher routes per utterance |

## Routing

1. **Filter to ready workers** — engines whose model is loaded and (for remote) whose last `/status` poll succeeded. An unreachable remote is skipped, so its clips fall back to the local engine instead of POSTing into the void.
2. **Pick least-busy** — by `_inflight` counter.

(Routing by clip length existed to keep short clips off Whisper's fixed 30 s window. It went with Whisper; `TRANSCRIBE_SPLIT_THRESHOLD_SECS` is no longer read.)

## Gateway config

```ini
ENABLE_TRANSCRIPTION = true

# Mode: off | local | remote | pool
TRANSCRIBE_MODE = pool

# Local engine model. When mode is local or pool, this is what runs inside
# the gateway process. A /transcribe page choice overrides it.
TRANSCRIBE_MODEL = parakeet/tdt-0.6b-v2

# Remote worker URLs — comma-separated. Only needed to pin a worker; workers
# started with --gateway register themselves.
TRANSCRIBE_REMOTE_URLS =
```

All other transcription settings (VAD threshold, audio boost, alert keywords, Mumble forwarding) live in `.transcribe_settings.json` and are edited via the `/transcribe` page.

## Remote worker

`tools/transcribe_worker.py` is a self-contained HTTP server that wraps `LocalInferenceEngine`. Drop it on any Linux box with Python 3.10+ and `onnx-asr[cpu,hub]` (and `moonshine-voice` for `moonshine2/*`) installed.

### Deployment

```bash
# On the remote host
mkdir -p ~/transcribe
# (scp transcribe_engine.py + transcribe_worker.py here)
pip install --break-system-packages 'onnx-asr[cpu,hub]' moonshine-voice

# Manual smoke test — --gateway makes the worker announce itself
python3 -u transcribe_worker.py --model parakeet/tdt-0.6b-v2 --port 9800 \
    --gateway http://192.168.2.140:8080 --name macmini
```

You should see:
```
[worker] Loading parakeet/tdt-0.6b-v2...
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
| `--gateway-username NAME` | `user` *(env `GATEWAY_USERNAME`)* | The gateway's `WEB_CONFIG_USERNAME`. |
| `--gateway-password PW` | `user` *(env `GATEWAY_PASSWORD`)* | The gateway's `WEB_CONFIG_PASSWORD`; `user` is the gateway default. |

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
Environment=TRANSCRIBE_CPU_THREADS=4
ExecStart=/usr/bin/python3 -u transcribe_worker.py --model parakeet/tdt-0.6b-v2 --port 9800 \
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
journalctl --user-unit transcribe-worker -f
```

Use `--user-unit`, not `--user -u`. With journald on `Storage=volatile` (RAM
only, as on the macmini and the gateway box to spare their SSDs) user units
are not split into a per-user journal file, so `journalctl --user` finds
nothing, or only a stale file left from before the switch. `--user-unit`
filters the system journal and works with either storage mode.

### HTTP interface

`POST /transcribe`
- Body: raw float32 little-endian PCM bytes at 16 kHz mono
- Response: `{"text": "...", "proc_time": 1.23}`
- 503 if model isn't loaded yet

`POST /model`
- Body: `{"model": "moonshine2/small"}`
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
| `model_key` | ✓ | ✓ | e.g. `parakeet/tdt-0.6b-v2`, `moonshine2/small` |
| `engine` | ✓ | ✓ | `parakeet` or `moonshine2` |
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

**Memory leaks on model swap** — Python `gc.collect()` is necessary but not sufficient on Linux. Native inference runtimes (ONNX Runtime; CTranslate2 when Whisper was here) allocate in native heap which Python doesn't free back to the OS without explicit `malloc_trim(0)` via libc. The worker calls this after every swap. Without it, repeated model switches accumulated ~1.5 GB per swap.

**Thermal throttling on passively cooled CPUs** — inference runtimes default to using every CPU thread. On a fanless or marginally-cooled CPU (e.g. 2013 Mac Mini i7-3720QM) this pushed package temp to 102°C and triggered severe thermal throttling — inference actually ran *slower* with all threads than at half-thread count. The worker reads `TRANSCRIBE_CPU_THREADS` (or legacy `WHISPER_CPU_THREADS`) from env (default = `cpu_count() // 2`); start at 4 on a quad-core box and adjust based on temp readings in the Workers row.

## Out-of-order completion handling

Pool dispatch means a long utterance sent to worker A can finish after a shorter one sent to worker B even if it started first. Results are inserted into the log sorted by `start_time` (the wall-clock when VAD closed the utterance) rather than completion order, so chronological order is preserved everywhere.

## Failure modes

| Scenario | Behaviour |
|----------|-----------|
| Remote down | Goes to the local engine — clip still gets transcribed |
| Local broken | Goes to a remote worker — clip still gets transcribed |
| All engines unhealthy | Dispatcher picks least-busy anyway so the error appears in logs rather than being hidden |
| Worker crashes mid-request | HTTP error surfaces in gateway log; engine flagged unreachable on next poll cycle |
| Model swap fails | `last_switch_error` populated for next `/status` poll; old model keeps serving |

## See also

- [docs/gateway_link.md](gateway_link.md) — protocol for remote *radio* endpoints (the worker uses a similar pattern but over HTTP not a custom protocol)
- [docs/mixer-v2-design.md](mixer-v2-design.md) — bus architecture and how the transcription sink fits in
