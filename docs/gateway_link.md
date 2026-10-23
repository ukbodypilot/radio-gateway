# Gateway Link -- Duplex Audio + Command Protocol

Remote radios connect to the gateway as **link endpoints**: a small script on
the radio's host streams duplex audio and carries commands, status and logs over
one connection, and the radio shows up in the routing UI like a local one.

## Why this exists

Each radio used to have bespoke driver code inside the gateway, and a radio
could only be used if it was plugged into the gateway machine. The link protocol
turns any radio on any reachable machine (a Pi next to a handheld, a Celeron
under a mobile rig, a CM5 on an IC-7100) into a routing source and sink with
no gateway code changes: write one plugin class, run `tools/link_endpoint.py`.

```
Gateway (mixer + web UI + link server :9700)
+-- local plugins in plugins/ (TH-9800, SDR, packet, USRP)    no network
+-- GatewayLinkServer
    +-- endpoint "ftm-150"   plugin aioc     TCP, LAN
    +-- endpoint "d75"       plugin d75      TCP, LAN (Pi Zero 2 W, Bluetooth)
    +-- endpoint "kv4p-v"    plugin kv4p     TCP, spawned by the gateway itself
    +-- endpoint "ic7100"    plugin ic7100   WebSocket via Cloudflare tunnel
```

---

## Files

| File | Purpose |
|------|---------|
| `gateway_link.py` | Protocol, server, client, `RadioPlugin` base, `AudioPlugin`, `AIOCPlugin`, `PttTimeOutTimer`, and the RFC 6455 WebSocket codec. Self-contained: no imports from other gateway modules, numpy optional, so it ships to endpoints on its own. |
| `tools/link_endpoint.py` | The endpoint script: plugin registry, gain, status reporter, tunnel fallback, self-update. |
| `tools/d75_link_plugin.py`, `tools/kv4p_link_plugin.py`, `tools/ic7100_link_plugin.py` | Hardware plugins loaded by the endpoint (`--plugin d75` etc.). |
| `tools/log_shipper.py` | Ships the endpoint's stdout/stderr to the gateway (see [endpoint_logs_design.md](endpoint_logs_design.md)). |
| `tools/remote_bt_proxy.py` | Bluetooth serial + SCO audio for the D75 endpoint. |
| `gateway_setup.py` | `setup_gateway_link` starts the server, creates a `LinkAudioSource` per registered endpoint, and wires the PTT time-out and the log store; `setup_kv4p_loopback_endpoints` spawns one local KV4P endpoint per `[kv4p.<name>]` config section. |
| `sources/network.py` (`LinkAudioSource`) | The endpoint as a mixer source/sink: level metering, gain, mute, jitter prefill. |
| `web_routes_stream.py` (`handle_ws_link`) | `/ws/link`: bridges a WebSocket endpoint to the TCP server. |
| `web_routes_radio.py` (`handle_linkcmd`) | `POST /linkcmd`: the web UI's commands to an endpoint. |

---

## Protocol

### Frame format

Every message is one framed packet:

```
+--------+--------+--------+-----...-----+
| Type   | Length (big-endian)| Payload    |
| 1 byte | 2 bytes           | 0-65535 B  |
+--------+--------+--------+-----...-----+
```

The payload is raw PCM for AUDIO and UTF-8 JSON for everything else. The 16-bit
length caps a payload at 65535 bytes; `send_frame` raises `ValueError` for a
larger one rather than sending a corrupt header, and the log shipper splits its
batches to fit (an oversize LOG batch used to stop an endpoint's log shipping
for good).

### Frame types

| Type | Value | Payload | Direction | Description |
|------|-------|---------|-----------|-------------|
| AUDIO | `0x01` | Raw PCM | Both | 48 kHz, 16-bit signed LE, mono. 4800 bytes = 50 ms. |
| COMMAND | `0x02` | JSON | Gateway → endpoint (mostly) | `{"cmd": "ptt", "state": true, "cmd_id": "..."}` |
| STATUS | `0x03` | JSON | Both | Heartbeats and the endpoint's status report. |
| REGISTER | `0x04` | JSON | Endpoint → gateway | Sent once on connect; identifies the endpoint. |
| ACK | `0x05` | JSON | Endpoint → gateway | Result of a COMMAND, keyed by `cmd_id`. |
| LOG | `0x06` | JSON | Endpoint → gateway | `{"type": "log", "lines": [{"ts", "stream", "text"}, ...]}` |
| CHALLENGE | `0x07` | JSON | Gateway → endpoint | `{"nonce": "<32 hex>", "v": 1}`, first frame on every connection (5.1+). |

### Registration

On connect the endpoint sends:

```json
{
  "name": "ftm-150",
  "plugin": "aioc",
  "capabilities": {"audio_rx": true, "audio_tx": true, "ptt": true, "rx_gain": true, "tx_gain": true, "status": true, ...},
  "version": "1.0"
}
```

An endpoint that has its key adds
`"auth": HMAC-SHA256(key, "radio-gateway-link-v1|<nonce>|<name>")` in hex,
answering the CHALLENGE (see [Authentication](#authentication)).

`capabilities` is a dict of flags (`audio_rx`, `audio_tx`, `ptt`, `frequency`,
`ctcss`, `power`, `rx_gain`, `tx_gain`, `smeter`, `packet`, `status`). The
server waits up to 10 s for REGISTER, rejects a second endpoint with a name
that is already connected, and keeps the rest in `link_endpoints.json` under
`~/.config/radio-gateway/` (mutes and gains survive a reconnect).

In routing, an endpoint becomes a source `<id>` and a TX sink `<id>_tx`. The id
is the plugin name for a one-off hardware plugin (`d75`, `ic7100`) and the
sanitised endpoint name otherwise (`kv4p_vhf`, `ftm_150`), so two KV4Ps or two
AIOCs don't collide.

### Heartbeats and dead peers

- The gateway sends `{"type": "heartbeat", "uptime": ...}` every 5 s.
- The endpoint sends its plugin's status every `--status-interval` (10 s
  default) and a heartbeat in between.
- The gateway declares an endpoint dead after **90 s** with no frame at all.
  Socket read timeouts on both sides are 120 s, longer on purpose so the
  gateway's dead-peer check fires first, and TCP keepalive is on.
- The endpoint reconnects with backoff from 2 s up to 60 s.

### Commands

The gateway sends COMMAND, the plugin's `execute()` runs it, and the endpoint
replies with an ACK carrying the same `cmd_id`. Common commands:

| Command | Fields | Who implements it |
|---------|--------|-------------------|
| `status`, `ping` | — | every plugin (base class) |
| `ptt` | `state: bool` | AIOC, D75, KV4P, IC-7100 |
| `rx_gain`, `tx_gain` | `db` (clamped to -20..+20, persisted on the endpoint) | `AudioPlugin` and subclasses |
| `gate` | `enabled`, `threshold` | `AudioPlugin` |
| `mode` | `audio` / `data` (Direwolf TNC on the endpoint) | AIOC |
| `frequency`, `ctcss`, `power`, memories, VFO, ... | plugin-specific | D75, KV4P, IC-7100 |

Unknown commands return `{"ok": false, "error": ...}`. The web UI sends
commands through `POST /linkcmd` with the command plus the endpoint's name,
e.g. `{"endpoint": "ftm-150", "cmd": "ptt", "state": true}`; for `status` it
waits up to 1 s for the fresh report. The MCP radio tools go the same way.

### PTT time-out

Every `ptt` command passes through the server's `PttTimeOutTimer`
(`PTT_TIMEOUT_SECONDS`, default 180, `0` = off). If an endpoint stays keyed
that long the gateway sends the unkey itself, logs
`[Link] <name>: PTT time-out (180s) -- forcing unkey`, and refuses key-on
requests for that endpoint until they have paused for 5 s, so a bus still
pushing audio can't re-key it at once. The TH-9800 plugin uses the same timer
class. Independently, `AIOCPlugin` unkeys itself on the endpoint after 60 s
(`ptt_timeout` plugin config key) in case the gateway goes away mid-over.

### Audio format

48000 Hz, 16-bit signed little-endian, mono, 4800-byte (50 ms) chunks, both
directions. The gateway buffers `LINK_JITTER_PREFILL` chunks (default 4)
before it starts playing an endpoint's audio.

---

## Transports

### TCP (LAN)

The default: the endpoint connects to the gateway's `LINK_PORT` (9700). The
gateway also advertises `_radiogateway._tcp` over mDNS (Avahi), so on the same
LAN the endpoint can find it without `--server`.

### WebSocket (Cloudflare tunnel)

For an endpoint off the LAN, `/ws/link` on the web UI port accepts a WebSocket
and bridges it to the TCP server: **each WebSocket binary message is one link
frame**. The endpoint connects with `--tunnel-url wss://<host>/ws/link`. When
the URL is a `trycloudflare.com` quick tunnel it changes on every gateway
restart, so the endpoint can re-read it from `tunnel_url.json` in Google
Drive (`--gdrive-remote`, `--gdrive-folder`, via rclone).

The connect order is: direct TCP, then the WebSocket URL, then a fresh URL from
Drive, then back off and retry.

All WebSocket framing in the gateway (`ws_encode`, `ws_read_frame`,
`ws_read_message`, `WsClosed`) lives in `gateway_link.py`, shared by
`WebSocketTransport` here, the `/ws/link` bridge and the browser audio, mic and
monitor handlers. It handles headers and masks split across reads, fragments,
pings/pongs of any length, and masks client frames as RFC 6455 requires.
A message over 1 MB (`WS_MAX_PAYLOAD`) drops the connection instead of being
buffered. Masking is vectorised when numpy is present (about 40x faster per
audio frame). `tests/test_ws_frames.py` covers it.

### Authentication

Before 5.1 any host that could reach the link port, or `/ws/link` through
the tunnel, could REGISTER under any name. Because a duplicate name replaces
the existing connection, it could take over a real radio: receive its TX
audio and commands and inject audio as its RX.

Since 5.1 every endpoint has its own key, and nothing is set up by hand:

1. **Master secret.** The gateway uses `LINK_SECRET` if set, otherwise it
   generates one on first start in `~/.config/radio-gateway/link_secret`
   (mode 0600). Each endpoint's key is
   `HMAC-SHA256(master, "radio-gateway-endpoint-key-v1|<name>")`, so one
   endpoint's key is no use for posing as another.
2. **Challenge.** The server's first frame on every connection is a
   CHALLENGE with a fresh random nonce. An endpoint with a key answers in
   REGISTER (`auth`, above).
3. **First connect (trust on first use).** A name the gateway hasn't
   enrolled may register without `auth`. The gateway then sends it
   `{"cmd": "link_key_set", "key": ...}`. The endpoint writes the key to
   `link_secret` (next to `link_endpoint.py`, and on a read-only-root Pi also
   to the persistent parent directory) and ACKs `ok: true` only if the key
   will survive a reboot. Only then is the name enrolled, in
   `~/.config/radio-gateway/link_enrolled.json`.
4. **After that** the name must authenticate. A missing or wrong `auth` gets a
   COMMAND `{"error": "link auth failed: ..."}` and the connection closes,
   **before** the duplicate-name check, so it can't evict the real endpoint.
5. Code updates are signed with the endpoint's key (see
   [Self-update](#self-update)).
6. `/ws/link` needs no web password, because the link server does this.

The nonce is per connection, so a captured REGISTER can't be replayed, and
nothing depends on the clock (Pis without an RTC boot with the wrong time).
This authenticates the endpoint; it doesn't encrypt the audio.

**The trade-off is the first connect.** The key travels once, in the clear on
the LAN TCP path (inside TLS on the tunnel path). Someone sniffing the LAN at
that moment could copy it, and anyone who reaches the gateway first with a
name that isn't enrolled yet can claim that name. Once every endpoint has
connected, set `LINK_ENROLL_NEW = false` and new names are refused; enrolled
ones keep working. To enroll a new endpoint later, turn it back on briefly.

**Nothing gets stranded:**

- An endpoint that can't store its key durably ACKs `ok: false` and stays
  unenrolled, so it can still connect without a key after a reboot.
- Endpoint code from before 5.1 ignores the CHALLENGE and answers
  `link_key_set` as an unknown command (`ok: false`), so it stays
  unenrolled, connects, self-updates, and enrolls on its next connect.
- If the master secret changes, the enrolled list (which stores a
  fingerprint of it) starts empty and every endpoint re-enrolls.
- Rolling the gateway back to a version without link auth: the endpoint
  waits up to 3 s for a CHALLENGE, registers without auth when none comes,
  and accepts that gateway's unsigned updates (see below).
- The KV4P endpoints the gateway starts itself get their key through
  `$RADIO_GATEWAY_LINK_SECRET`.
- To re-enroll one endpoint by hand, remove its name from
  `link_enrolled.json` and restart the gateway.

`LINK_BIND` limits the link port to one interface address (blank = all).

---

## Plugin architecture

### RadioPlugin base class

Endpoint plugins subclass `RadioPlugin` in `gateway_link.py`:

```python
class RadioPlugin:
    name = "base"
    capabilities = {"audio_rx": False, "audio_tx": False, "ptt": False,
                    "frequency": False, "ctcss": False, "power": False,
                    "rx_gain": False, "tx_gain": False, "smeter": False,
                    "packet": False, "status": True}

    def setup(self, config): ...        # config: dict from CLI args + --plugin-config-json
    def teardown(self): ...
    def get_audio(self, chunk_size=4800): ...   # -> (pcm_bytes | None, ptt_flag)
    def put_audio(self, pcm): ...       # audio from the gateway to play / transmit
    def execute(self, cmd): ...         # -> result dict, sent back as the ACK
    def get_status(self): ...           # -> dict, sent as STATUS
```

### Built-in and shipped plugins

| `--plugin` | Class | Where | Hardware |
|------------|-------|-------|----------|
| `audio` | `AudioPlugin` | `gateway_link.py` | Any PyAudio sound card; RX/TX gain, gate |
| `aioc` | `AIOCPlugin` | `gateway_link.py` | AIOC: sound card + HID GPIO PTT; `mode data` runs Direwolf on the endpoint (KISS on 8001) |
| `d75` | `D75Plugin` | `tools/d75_link_plugin.py` | Kenwood TH-D75 over Bluetooth (RFCOMM CAT + SCO audio) |
| `kv4p` | `KV4PPlugin` | `tools/kv4p_link_plugin.py` | KV4P HT over USB serial (Opus audio) |
| `ic7100` | `IC7100Plugin` | `tools/ic7100_link_plugin.py` | Icom IC-7100: CI-V CAT + USB audio; one method per CI-V command |

`d75`, `kv4p` and `ic7100` are loaded lazily, so their dependencies are only
needed on hosts that use them. A new plugin goes in `tools/`, gets an entry in
`_LAZY_PLUGINS` in `tools/link_endpoint.py`, and runs with
`--plugin <name>`. See [plugin-development.md](plugin-development.md) for a
worked example.

### Design decisions

1. **Self-contained module.** `gateway_link.py` has no imports from the rest of
   the gateway and treats numpy as optional, so an endpoint needs only that
   file, `link_endpoint.py` and its plugin.
2. **Lazy hardware imports.** `pyaudio`, `hid` and serial libraries are
   imported in `setup()`, not at module load.
3. **50 ms chunks** everywhere, matching the gateway's bus tick.
4. **Threads.** The server has an accept thread, one reader and one sender
   (with a bounded send queue) per endpoint, and a heartbeat thread. A full send
   queue drops audio rather than blocking the bus tick. Callbacks fire on the
   reader thread and must not block.

---

## Gateway side

### LinkAudioSource

One per registered endpoint, created by `setup_gateway_link`:

- **Routing ids:** source `<id>`, TX sink `<id>_tx` (see Registration).
- **RX:** the reader thread pushes AUDIO frames into a queue; the bus tick
  pulls them after the jitter prefill. Level metering, `LINK_AUDIO_BOOST`,
  per-endpoint RX gain and mute.
- **TX:** a bus wired to `<id>_tx` sends its mix to the endpoint. The deliver
  loop keys the endpoint's PTT from audio level (`LINK_AUTO_PTT_THRESHOLD`,
  held `LINK_AUTO_PTT_HOLD` seconds after the last loud chunk), with
  `PTT_PREKEY_BUFFER_MS` of audio held while the key is applied. See
  [audio-routing.md](audio-routing.md#ptt-the-sink-kind-decides-the-keying-mechanism).
- **UI:** a card on `/dashboard/endpoints` and a bar in the shell's meter strip;
  radio-specific pages (`/d75`, `/kv4p`, `/ic7100`) for the hardware plugins.

### Logs

Endpoints ship their stdout/stderr as LOG frames; the gateway writes
`logs/endpoints/<name>.log` and shows them at `/endpoints/logs`. See
[endpoint_logs_design.md](endpoint_logs_design.md).

### Self-update

Endpoints compare a hash of their code with `GET /api/endpoint/version` on
start, on each reconnect (at most every 5 minutes) and hourly, and fetch
`/api/endpoint/files` when it differs. The bundle is `gateway_link.py`,
`link_endpoint.py`, `log_shipper.py`, `d75_link_plugin.py` and
`remote_bt_proxy.py`; the list in `tools/link_endpoint.py` and
`_ENDPOINT_FILES` in `web_routes_get.py` must match. `--no-update` turns it
off.

The endpoint writes only the files on that list (a bundle entry such as
`../../.bashrc` is ignored). The endpoint names itself in an
`X-Endpoint-Name` header and the gateway signs the bundle with that
endpoint's key (`X-Bundle-Signature`, an HMAC over each file's name and
SHA-256). An endpoint with a key refuses a bundle that isn't signed with it,
unless the gateway didn't challenge its last connection (a gateway from before
5.1, e.g. after a rollback, which can't sign). An endpoint without a key yet
installs unsigned bundles and says so in its log. `windows_audio_client.py`
updates are not signed yet; it too writes only the files on its list.
The update routes need no web password: they serve only code from this
repository. See [pi-endpoint-build.md](pi-endpoint-build.md) for how a read-only-root
Pi persists the update.

---

## Configuration

Gateway side, in `gateway_config.txt` (`[link]` on the config page):

| Key | Default | Description |
|-----|---------|-------------|
| `ENABLE_GATEWAY_LINK` | `false` | Run the link server |
| `LINK_PORT` | `9700` | TCP port for endpoints (the `/ws/link` bridge connects here too) |
| `LINK_BIND` | blank | Interface address to listen on; blank = all |
| `LINK_SECRET` | blank | Master for per-endpoint keys; blank = generated in `~/.config/radio-gateway/link_secret`. See [Authentication](#authentication) |
| `LINK_ENROLL_NEW` | `true` | Let names that aren't enrolled yet connect and receive their key. Turn off once every endpoint has connected |
| `LINK_AUDIO_PRIORITY` | `3` | Ducking priority for link audio (lower = higher priority) |
| `LINK_AUDIO_DUCK` | `false` | Whether link audio ducks SDR sources |
| `LINK_AUDIO_BOOST` | `1.0` | Level multiplier for incoming link audio |
| `LINK_AUDIO_DISPLAY_GAIN` | `1.0` | Display gain for the level bars |
| `LINK_RX_MUTED`, `LINK_TX_MUTED` | `false` | Start-up mute state |
| `LINK_AUTO_PTT_THRESHOLD` | `10` | Bus level (0-100) that keys an endpoint's TX |
| `LINK_AUTO_PTT_HOLD` | `0.5` | Seconds to hold the key after the last loud chunk |
| `LINK_JITTER_PREFILL` | `4` | 50 ms chunks buffered before an endpoint's audio plays |
| `PTT_PREKEY_BUFFER_MS` | `500` | TX audio held while the key is applied |
| `PTT_TIMEOUT_SECONDS` | `180` | Transmit time-out for every endpoint (`0` = off) |

KV4P endpoints are configured per instance in `[kv4p.<name>]` sections
(`port`, `default_freq`, `squelch`, ...). The gateway starts one
`link_endpoint.py --plugin kv4p` per section and passes the section as
`--plugin-config-json`.

---

## Usage

### Gateway

Set `ENABLE_GATEWAY_LINK = true` and restart the gateway. It listens on
`LINK_PORT` and publishes itself over mDNS.

### Endpoint

```bash
# list audio devices
python3 tools/link_endpoint.py --list-devices

# LAN, gateway found over mDNS (needs avahi-utils / avahi)
python3 tools/link_endpoint.py --name living-room --plugin aioc

# explicit address, a device and input gain
python3 tools/link_endpoint.py --server 192.168.2.140:9700 \
    --name garage-radio --device "AIOC" --gain 2.0

# through the Cloudflare tunnel
python3 tools/link_endpoint.py --name ic7100 --plugin ic7100 \
    --tunnel-url wss://example.trycloudflare.com/ws/link
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--server HOST:PORT` | mDNS lookup | Gateway address |
| `--name NAME` | — | Endpoint name (shown on the dashboard; must be unique) |
| `--plugin PLUGIN` | `audio` | `audio`, `aioc`, `d75`, `kv4p`, `ic7100` |
| `--device DEVICE` | system default | Audio device name/index, or serial path |
| `--rate HZ` | `48000` | Sample rate |
| `--gain FLOAT` | `1.0` | Input gain multiplier |
| `--plugin-config-json JSON` | — | Extra plugin settings, e.g. `'{"default_freq":147.435,"squelch":2}'` |
| `--status-interval SEC` | `10.0` | Status report interval |
| `--tunnel-url URL` | — | `wss://.../ws/link` fallback when TCP fails |
| `--gdrive-remote`, `--gdrive-folder`, `--rclone-config` | `gdrive`, `radio-gateway` | Where to re-read a changed tunnel URL |
| `--no-update` | off | Skip the self-update check |
| `--list-devices` | — | List audio devices and exit |

### Deploying to a remote machine

Copy `gateway_link.py`, `tools/link_endpoint.py`, `tools/log_shipper.py` and the
plugin you need into one directory on the endpoint (it gets its key from the
gateway on first connect), install that plugin's
dependencies (`pyaudio` for `audio`/`aioc`, plus `hidapi` for `aioc`), and run
it as above. For a systemd unit see
[plugin-development.md](plugin-development.md#auto-start-on-boot-systemd); for a
complete Pi build, [pi-endpoint-build.md](pi-endpoint-build.md).

---

## Not built yet

- Per-endpoint mix selection beyond what routing already does (an endpoint
  hears whatever bus you wire to its `_tx` sink).
- TLS on the raw TCP port. Off the LAN, use the tunnel (`wss://`).
- Authentication of endpoints. Anything that can reach port 9700 or `/ws/link`
  can register; keep 9700 on the LAN.
- Gateway-to-gateway linking.

---

## Design notes

### Why a custom framing on TCP, and WebSocket only as a carrier?

- **Low latency, minimal overhead.** A 3-byte header and raw PCM is the least
  that can carry duplex 50 ms audio plus control.
- **Simplicity.** The framing is a few dozen lines of stdlib Python with no
  schema files, so the module runs on a minimal Pi image.
- **Reachability.** Raw TCP doesn't pass through Cloudflare's tunnel, so the
  same frames ride inside WebSocket binary messages there. Nothing about the
  link protocol changes; the bridge just unwraps them.

### Why plugins, not a generic audio bridge?

A radio needs control as well as audio: PTT, frequency, CTCSS, power, status.
With the hardware behind a plugin, one connection carries both, and the gateway
tunes a remote radio the same way it tunes a local one.
