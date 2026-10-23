# Windows Audio Client

A full-duplex console client that carries program audio and your voice between a
Windows machine and the gateway, with live device switching, broadcast-style
ducking, real-time voice effects, and self-update from the gateway.

## Why this exists

The gateway's link-endpoint protocol assumes a Linux box with a radio attached.
The Windows client solves a different problem: an **operator position**. It
takes whatever a virtual cable is carrying (program audio, a desktop
application, another radio's output), sends it to the gateway continuously, and
lets the operator talk over it — with the program automatically ducking under
their voice, the way a broadcast desk does.

It also plays back whatever the gateway pushes to it, so one window is both the
studio feed and the monitor.

## Architecture

Two independent TCP connections, opposite directions:

```
   Windows client                          Gateway
   ──────────────                          ───────
   TX  program feed ─┐
       operator mic ─┴─ mix + duck ──────▶ connects OUT to REMOTE_AUDIO_RX_PORT
                                            (default 9602)

   RX  speaker      ◀──────────────────── gateway connects IN
                                            (from port 9600)
```

**Protocol:** length-prefixed PCM — `[4-byte big-endian uint32 length][PCM payload]`.
**Audio:** 48 000 Hz, mono, 16-bit signed little-endian, 2400 frames per chunk.

Note the asymmetry: for TX the client dials **out** to the gateway, but for RX
it **listens** and the gateway dials in. A firewall that only allows outbound
connections will give you a working TX path and a silent speaker.

## Three devices, not two

The client selects three devices at startup and saves them to its JSON config:

| Role | What it is |
|------|-----------|
| **TX program feed** | Input continuously sent to the gateway — typically a virtual cable carrying radio or program audio |
| **RX speaker** | Where audio received from the gateway is played |
| **Operator mic** | Your voice, mixed on top of the program feed **only while you hold the talk key** |

The program ducks under the operator mic while talking, broadcast style. Duck
depth is adjustable live.

## Usage

```
pip install sounddevice numpy keyboard
python windows_audio_client.py [gateway_host] [--no-update]
```

On first run it prompts for the audio devices and gateway host, then saves the
selection to `windows_audio_client.json` alongside the script.

### Keyboard controls

Keys act only while the console window is **focused**.

**Mic (your voice)**

| Key | Action |
|-----|--------|
| `SPACE` | Talk on/off — **latching**: tap to start, tap again to stop |
| `-` / `=` | Mic gain down / up 10% (0–1000%; mics need real preamp gain) |
| `v` | Voice effect on / off |
| `n` / `m` | Previous / next voice effect |

**TX (program feed)**

| Key | Action |
|-----|--------|
| `l` | On / mute |
| `<` / `>` | Volume down / up 5% |
| `;` / `'` | Duck depth down / up 5% — how far the program dips while you talk |
| `r` | Resample on/off (diagnostic) |
| `w` | Record the outgoing mix to a WAV file |

**RX (speaker)**

| Key | Action |
|-----|--------|
| `p` | Play / mute |
| `[` / `]` | Volume down / up 5% |

**Devices — swap on the fly, no restart, audio keeps running**

| Key | Action |
|-----|--------|
| `i` | Pick the TX program feed input |
| `k` | Pick the operator mic input |
| `o` | Pick the RX speaker output |

**Other:** `d` toggles diagnostics, `Ctrl+C` quits.

### The device picker

Pressing `i`, `k` or `o` opens an inline picker over the status display. Inside
it:

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up the list |
| digits | Type an item number directly |
| `Backspace` | Correct a typed number |
| `Enter` | Commit the selection |
| `Esc` or `q` | Cancel — changes nothing |

Committing reopens just that one stream (`_switch_capture`, `_switch_mic`,
`_open_out`); the other two directions keep running untouched. The new choice
is written to the JSON config, so it survives a restart.

## Voice effects

The operator mic can run through real-time DSP applied in-process — no external
application. Chipmunk, deep/monster, robot, alien, echo, cathedral, telephone,
megaphone, bitcrush and others. Cycle with `n`/`m`, toggle with `v`.

All effects are **stateful and click-free across chunk boundaries** — they carry
filter state between 2400-frame blocks rather than restarting each chunk, which
is what would otherwise produce a click at 20 ms intervals.

## Self-update

On startup the client asks the gateway whether it is running current code and,
if not, downloads the new version and relaunches itself.

```
GET /api/winclient/version   →  {"version": "<16 hex>", "files": [...]}
GET /api/winclient/files     →  base64 JSON bundle of the client files
```

The version is a sha256 over the files in `UPDATE_FILES`, truncated to 16 hex
characters, computed **identically on both ends** — the client hashes its local
copies, the gateway hashes the ones it would serve.

Pass `--no-update` to skip the download. The relaunch adds that flag
automatically, so an update can never loop.

On the `--no-update` path the client still runs `verify_version_only()`, so the
dashboard can report whether the client is current even when it is not allowed
to act on it.

### Why the client bundle is separate from the endpoint bundle

`_WINCLIENT_FILES` in `web_routes_get.py` is deliberately **not** part of
`_ENDPOINT_FILES`. Adding the Windows client to the shared endpoint bundle would
change the shared version hash, and every Linux link endpoint would then
download a bundle containing a file it has no use for.

### Failure behaviour

Every failure mode starts the client anyway rather than blocking on the network:

- Gateway unreachable or check throws → logs `check failed: … — starting anyway`
- Gateway returns no version → logs `gateway returned no version — skipping`
- Version mismatch but the files turn out identical → logs `files unchanged
  despite version mismatch` and does **not** relaunch. The file content gates
  the write, so a stale hash cannot cause a pointless relaunch loop
- Relaunch itself fails → tells you to restart manually

## Configuration

Client side — `windows_audio_client.json`, written next to the script:

| Key | Purpose |
|-----|---------|
| `web_port` | Gateway web port for the update check (default `8080`) |
| `update_enabled` | Whether to self-update |

Gateway side — `gateway_config.txt`:

| Key | Default | Purpose |
|-----|---------|---------|
| `REMOTE_AUDIO_ROLE` | `disabled` | `server`, `client`, or `disabled` |
| `REMOTE_AUDIO_HOST` | — | Server: bind address. Client: server IP |
| `REMOTE_AUDIO_PORT` | `9600` | RX port the gateway connects in from |
| `REMOTE_AUDIO_DUCK` | `true` | Duck other sources when this one is active |
| `REMOTE_AUDIO_PRIORITY` | `0` | Ducking priority (`0` = ducks all local SDRs) |
| `REMOTE_AUDIO_AUDIO_BOOST` | `1.0` | RX volume multiplier |
| `REMOTE_AUDIO_DISPLAY_GAIN` | `1.0` | Meter sensitivity only |
| `REMOTE_AUDIO_RECONNECT_INTERVAL` | `5.0` | Seconds between reconnect attempts |
| `REMOTE_AUDIO_JITTER_PREFILL` | `8` | 50 ms chunks buffered before playback starts. Higher than a link endpoint's `4` because this path is burstier |

## Notes from running it

- **The client is a source that never asserts the PTT flag.**
  `RemoteAudioSource` ends every path with `return raw, False`. Historically
  that meant it could key a link endpoint (keyed from audio level) but never a
  plugin radio like the TH-9800 (keyed from the flag) — identical wiring,
  silently different result. See
  [audio-routing.md](audio-routing.md#ptt-the-sink-kind-decides-the-keying-mechanism).
- **Silent chunks were a client-side resample artifact, not a gateway bug.**
  Resolved by capturing at the device's native 44.1 kHz and resampling with
  soxr on the client, with a jitter buffer on the gateway — rather than
  resampling naively in the callback. The `r` key toggles resampling for
  diagnosis. The investigation is written up in
  [windows-client-audio-investigation.md](windows-client-audio-investigation.md).
- **`SPACE` latches**, unlike the web UI's MIC button, which is hold-to-talk.
  A console window that loses focus mid-over leaves the client talking; the
  gateway's dead-man and TOT on the bus thread are what actually stop it.
- Mic gain goes to 1000% on purpose. Consumer mics through a virtual cable
  routinely arrive 20 dB below the program feed.

## Source pointers

- [`windows_audio_client.py`](../windows_audio_client.py) — the whole client
- [`web_routes_get.py`](../web_routes_get.py) — `handle_winclient_version`,
  `handle_winclient_files`, `_winclient_bundle`
- [`sources/network.py`](../sources/network.py) — `RemoteAudioSource`, the gateway
  end of both connections
