# Web UI — shell and dashboard family

The gateway's web UI: a persistent shell (nav, live audio players, meter
strip) framing 20+ feature pages, with the dashboard split into four
sub-pages so no status hides behind a tab.

## Why this exists

The original single `/dashboard` accreted panels until most of its state was
invisible — seven services shared one tab strip, System and Status hid behind
another, and endpoint status and controls lived on separate tabs. v4.2 split
it into sub-pages organised by what you're doing, and put a subsystem
annunciator on the landing page so everything is visible at one glance.

## Architecture

```
┌ shell.html  (loaded at /) ─────────────────────────────────────────┐
│ nav bar · identity plate · MP3/PCM players · web mic               │
│ level-meter strip  (1 s /status poll drives VU bars)               │
│ ┌ iframe name="content" ───────────────────────────────────────┐  │
│ │ /dashboard              Overview  (default)                  │  │
│ │ /dashboard/endpoints    Endpoints                            │  │
│ │ /dashboard/services     Services                             │  │
│ │ /dashboard/operate      Operate                              │  │
│ │ /routing /transcribe /radio ...   feature pages               │  │
│ └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

- **One `/status` poll.** The shell polls `/status` at 1 s for the meter
  strip and **broadcasts each payload into the iframe** via `postMessage`
  (`{type:'rg-status'}`, same-origin). Dashboard pages consume the broadcast
  through `createStatusPoller()` in `dash.js` and only fetch `/status`
  themselves when no broadcast has arrived for 5 s (page opened standalone).
- **Live nav.** The Radios menu is derived from `/status` enable flags and
  `/usrp/nodes` — entries appear when their subsystem reports enabled and
  stay for the session (sticky, so an endpoint reboot doesn't flicker the
  menu). Discovered AllStar plugin instances get menu items automatically.
- **Shared assets.** `common.css`/`common.js` serve every page (theme tokens,
  `RG.vu` meter physics, `stItem`/`stRow` status-row builders);
  `dash.css`/`dash.js` add the dashboard-family layer (toasts, panel tabs,
  annunciator chips, the status-poller wrapper).

## Access control

Every request goes through `GatewayRequestHandler._check_access`
(`web/handler.py`), in this order:

1. **Cross-site requests are refused (403).** A POST or WebSocket upgrade
   whose `Origin` isn't this server, and a cross-site GET that isn't a
   top-level visit to a page (`Sec-Fetch-Site`/`Sec-Fetch-Mode`). Browsers
   send Basic auth credentials with cross-site requests, so without this any
   web page you opened could POST to `/manager/save` and `/manager/run`
   (Claude with every permission), `/reboothost`, or open `/ws_mic` and
   transmit. Requests with no `Origin` (curl, MCP, endpoints) aren't from a
   web page and pass. The tunnel hostname counts as this server.
2. **Endpoint routes.** `/api/endpoint/*` and `/api/winclient/*` (public
   repo code, signed when `LINK_SECRET` is set) need no password.
   `/ws/link` needs none when `LINK_SECRET` is set, because the link server
   authenticates REGISTER. See [gateway_link.md](gateway_link.md#authentication).
   `/metrics` is open to clients on the gateway itself (Prometheus scrapes
   it without credentials), but not through the tunnel.
3. **HTTP Basic auth** with `WEB_CONFIG_USERNAME` / `WEB_CONFIG_PASSWORD`.
   The default login is `user` / `user`, and a blank value in the file keeps
   the default, so there is always a login. Change it.
4. (Defence in depth: if the password were ever empty, requests through the
   Cloudflare tunnel, which carry `Cf-Ray`/`Cf-Connecting-Ip`, would get a
   403 except the endpoint routes and `/ws/link`, unless
   `WEB_TUNNEL_ALLOW_NO_PASSWORD = true`.)

Secrets (any string key whose name contains `PASSWORD`, `SECRET`,
`PASSCODE` or `TOKEN`, from `config_schema.secret_keys()`) are never sent to
the browser; a blank field on save keeps the stored value.

The fleet manager's own snapshot requests to localhost send the password
(via curl's config on stdin, so it isn't visible in `ps`). MCP reads the same
default. `tools/transcribe_worker.py` defaults `--gateway-password` to
`user`. The room-monitor Android app doesn't send credentials, so it can't
connect to `/ws_monitor` any more.

## The four dashboard pages

| Page | Job |
|------|-----|
| **Overview** (`/dashboard`) | System bars (CPU/RAM/swap/disk/net/temps), gateway status flags (PTT, Mumble, CAT, D75, smart countdowns…), and the subsystem **annunciator** — one lamp per subsystem, always present, dark when disabled, each linking to its sub-page. |
| **Endpoints** (`/dashboard/endpoints`) | One card per Gateway Link endpoint: status readouts plus PTT / RX-TX VU bars / mute / gain sliders together. Below, one card per **transcribe worker** (local + remote): engine/model tags, ready state, done/active counts, ratio, RAM/temp/fan, URL + heartbeat for self-registered workers. |
| **Services** (`/dashboard/services`) | Broadcastify (uptime, throughput, last error, bitrate sparkline from Prometheus), Loop Recorder buses, AllStar nodes, GPS fix + satellites, ADS-B, USB/IP devices. All visible; disabled services ghost out. |
| **Operate** (`/dashboard/operate`) | Playback soundboard grid, Smart Announce slots, Transmit (TTS / CW / AI / Fart tabs), Automation engine tasks + history. |

## Notes from running it

- Endpoint/worker card skeletons (which hold sliders) rebuild only when an
  identity key changes (name / connected / PTT / mutes); the status rows
  inside re-render every poll, string-diffed. Rebuilding sliders per poll
  fights the user's drag.
- Remote-supplied strings (endpoint hostnames, worker names, radio memory
  channel names) are escaped before `innerHTML` — fleet machines are trusted,
  but their status payloads shouldn't be an injection vector into the
  gateway UI.
- The shell's **MIC** button is hold-to-talk, not a latch: press (or hold
  Space while the shell has focus) keys, release unkeys. The mic stream and
  `/ws_mic` socket linger 30 s after release so a follow-up over doesn't
  re-pay the getUserMedia + handshake latency and clip its first syllable —
  so an open socket does NOT mean a keyed transmitter, and the button shows
  a cyan `armed` outline rather than the red `live` one. Release is sent as
  an `UNKEY` text frame but is never the only thing that stops TX: the
  gateway dead-mans a lapsed key refresh and enforces a 120 s TOT on the bus
  thread, because a lost `pointerup` or a slammed lid must not be able to
  strand a transmitter. See `WebMicSource` in `sources/web.py`.
- **Holding MIC ducks every local output** for the duration of the hold. On a
  speakerphone the dashboard's own playback is acoustically coupled back into
  the browser mic, so without this the operator hears themselves returned. Two
  outputs are silenced: the PCM player's gain node — which also covers the
  AS1/AS2 taps, since those feed their RX audio into the PCM stream rather
  than playing separately — and the MP3 `<audio>` element, which is the one
  genuinely separate output. Details that matter if you touch it:
  - `_wsApplyGain()` is the **single writer** of the PCM gain. Slider value
    and TX duck are two independent inputs to one gain; writing
    `.gain.value` directly from either would let a slider drag mid-over
    un-duck the stream and put the speakerphone back in the mic.
  - The duck is a 15 ms ramp, not a step — an abrupt gain change on a live
    stream clicks.
  - The MP3 element uses `.muted`, not `.volume`, so the user's slider value
    survives the hold.
  - Ducking happens on **press**, not on session open, so nothing leaks while
    `getUserMedia` is still prompting.
  - `micTeardown` and the `getUserMedia` rejection path clear `_micKeyed`
    directly and un-duck explicitly, bypassing `micRelease`. Otherwise a
    dropped socket or a denied permission prompt would strand the dashboard
    permanently silent.
- Static pages are read from disk per request: editing a page under
  `web_pages/` goes live on refresh. Adding a **route** means editing
  `_STATIC_PAGES` (pages) or the `_GET_*`/`_POST_*` dispatch tables
  (handlers) in `web/handler.py`, which needs a service restart.
- Route dispatch is table-driven: exact-match dict → query-stripped exact →
  ordered prefix list, then plugin-contributed routes (`web_routes()` hook),
  then 404. Only `POST /config` saves the config file; before 5.0.0 any
  unmatched POST fell through to the config save.
- The **config page** (`/config`) is generated from `config_schema.LAYOUT`;
  the grey text beside each field is its hint (`_FIELD_HINTS` in
  `web_server.py`) and its default. Saving edits `gateway_config.txt` in
  place and keeps everything the form doesn't show; see
  [config-reference.md](config-reference.md#saving-from-the-web-config-form).
- **HTTPS**: `WEB_CONFIG_HTTPS = self-signed` or `letsencrypt` (the latter for
  `DDNS_HOSTNAME`) serves the UI over TLS on the same port. Before 5.0.0 both
  modes were silently read as `false`, and in `letsencrypt` mode a NameError in
  the renewal thread took the web UI down; renewal now starts only after the
  server is up.
- **WebSockets** (`/ws_audio` for the PCM player, `/ws_mic`, `/ws_monitor`, `/ws/link`)
  share one RFC 6455 codec in `gateway_link.py`. Messages over 1 MB drop the
  connection; fragmented and split frames are handled.
- **Manual PTT from the UI** (radio pages, keyboard) is subject to the
  transmit time-out `PTT_TIMEOUT_SECONDS` (default 180 s): the radio is
  force-unkeyed and won't re-key until requests pause for 5 s.

## Source pointers

| File | What |
|------|------|
| `web_pages/shell.html` | Shell: nav, players, meter strip, status broadcast |
| `web_pages/dashboard.html` | Overview + annunciator |
| `web_pages/dash_endpoints.html` | Endpoint + transcribe worker cards |
| `web_pages/dash_services.html` | Service panels |
| `web_pages/dash_operate.html` | Playback / Transmit / Automation |
| `web_pages/dash.css`, `dash.js` | Dashboard-family shared styles + helpers |
| `web_pages/common.css`, `common.js` | Theme tokens, VU engine, st* builders |
| `web_server.py` | Server start-up, HTTPS, MP3 stream encoder, config page + `_save_config` |
| `web/handler.py` | Request handler: `_STATIC_PAGES`, dispatch tables, auth |
| `web/certs.py` | Self-signed / Let's Encrypt certificates and renewal |
| `web_routes_stream.py` | WebSocket handlers (audio, mic, monitor, `/ws/link` bridge) |
| `gateway_link.py` | The shared WebSocket codec (`ws_encode`, `ws_read_frame`, `ws_read_message`) |
