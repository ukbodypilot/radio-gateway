# Offload workers: remote DP8 denoise and Kokoro TTS

Two small services that run on another machine (here, the macmini) so the
gateway box doesn't do the heavy lifting:

| Worker | File | Port | Kind | Nice | What it does |
|---|---|---|---|---|---|
| Denoise | `tools/denoise_worker.py` | 9802 (+9803 status) | `denoise` | 0 | Real-time DP8 for any bus set to **remote** |
| TTS | `tools/tts_worker.py` | 9801 | `tts` | 10 | Kokoro synthesis when `TTS_ENGINE=kokoro` |

The transcription worker (`tools/transcribe_worker.py`, nice 15) is separate.
Its niceness is the lowest priority, so a long transcription never starves a
live bus.

## No addresses in config

Each worker POSTs `{kind, name, port, ...}` to the gateway's
`/worker/register` every 30 s. The gateway takes the address from the socket
peer and expires a worker after 90 s without a heartbeat. A worker that comes
back under the same name at a new address replaces the old entry, so DHCP
changes and Wi-Fi to wired moves need no action. `GET /workers` shows the
pool, every remote denoise stream and the TTS counters.

## Denoise: one TCP stream per bus

In `/routing`, each bus's denoise row has a **local / remote** button. It sets
the bus's `dfn_engine` to `dpdfnet` or `dpdfnet_remote`, and the setting is
saved in `routing_config.json`. A remote bus opens one persistent TCP connection
to the worker, which runs that bus's own `_DPDFNet8kStream`. Remote output is
bit-identical to local (tested).

Rules (`remote_denoise.py`):

- **A miss plays DRY, never local.** A late reply, no worker, or a broken
  link passes the audio through unchanged. Falling back to local would put
  the load back on the gateway the moment the worker blinks.
- The reply deadline is 80 ms, inside the 100 ms delay line. Connecting never
  blocks the audio thread.
- Replies carry a sequence number, and a late reply is discarded. After 40
  misses in a row (2 s) the stream reconnects, retrying every 3 s.
- TCP keepalive and `TCP_USER_TIMEOUT` reap dead peers on both ends. Before
  that was added, a vanished link left the worker's stream slot taken forever.

Counters:

- Prometheus: `rg_denoise_remote_total{bus,result}` (ok, timeout, down, error, stale)
  and `rg_denoise_remote_rtt_ms{bus}`.
- The worker itself: `http://<worker>:9803/status`, which reports per-stream
  processing time, CPU temperature and load.

Measured on the macmini with 4 buses for 30 s over gigabit: 2400/2400
replies on time, round trip ~22 ms (of which ~21 ms is inference), worst
66 ms. The CPU package reached 81 °C (high = 87 °C), so 4 to 5 buses is about
the comfortable limit on that box (`--max-streams 6`).

### Gotcha: a worker with Wi-Fi and wired both on the same subnet

Linux answers ARP for any of its addresses on any interface. With both
interfaces up, the gateway's ARP entry for the wired IP flipped to the
Wi-Fi MAC mid-stream, and all four streams stalled together. Run the worker
on **one** interface. (The fix used was Wi-Fi off: `nmcli con mod <wifi>
connection.autoconnect no`.) If you must keep both, set
`net.ipv4.conf.all.arp_ignore=1` and `arp_announce=2`.

## TTS

With `TTS_REMOTE_WORKER = true` (the default), Kokoro synthesis goes to the
freshest registered `tts` worker that reports its model as loaded. The
remote timeout is 5 s plus the length of the text / 15. If the worker fails or
none is registered, the gateway uses its local Kokoro, which it loads on first
use. The local model files must still be present: a switch to Kokoro without
them is refused up front. Counters: `rg_tts_remote_total{result}` (remote,
fallback, local). Measured: 3.2 s to synthesise 4 s of speech on the macmini.

## Installing a worker

Copy `audio_util.py`, `log_util.py` and `remote_denoise.py`, plus
`tools/{worker_common,denoise_worker,tts_worker}.py`, the
`tools/models/dpdfnet*` model and `tools/models/kokoro/` into a directory such
as `~/offload`. Then run `pip install kokoro-onnx onnxruntime numpy` and add
user units like:

```ini
# ~/.config/systemd/user/denoise-worker.service
[Unit]
Description=Radio Gateway Denoise Worker (remote DP8, real time)
After=network-online.target

[Service]
WorkingDirectory=%h/offload
ExecStart=/usr/bin/python3 -u tools/denoise_worker.py --port 9802 --gateway http://GATEWAY:8080 --name macmini
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

The TTS unit is the same, with
`tools/tts_worker.py --port 9801 --threads 4`. Enable both with
`systemctl --user enable --now denoise-worker tts-worker` and
`loginctl enable-linger $USER`. If the web login isn't the default, pass
`--gateway-username/--gateway-password` (or set `GATEWAY_USERNAME` and
`GATEWAY_PASSWORD`).
