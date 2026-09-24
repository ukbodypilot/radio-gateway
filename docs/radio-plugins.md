# Radio Plugins

Every radio the gateway controls is a plugin — same interface, same lifecycle, same place in the routing graph as any other source/sink. The set today is TH-9800, TH-D75, KV4P, RSPduo SDR (built-in plugins) plus FTM-150 and IC-7100 (link endpoints — same plugin model, different transport).

For the plugin contract and building a new one, see [plugin-development.md](plugin-development.md).

## TH-9800

Yaesu FTM-9800 dual-band quad-receiver. Connected via **AIOC** (All-In-One-Cable) over USB for audio + PTT, and **CAT** over serial (currently bridged via TCP from a separate Celeron host running USB/IP).

**Capabilities**
- Full CAT control: dual VFO, all memories, every front-panel button replicated in the web UI
- HID PTT via AIOC
- Browser-mic PTT (push the on-screen mic button or hold spacebar on `/radio`)
- Soft-clip on TX path (no hard clipping under audio boost)
- USB reset recovery — if AIOC vanishes from `/dev/snd`, plugin attempts USB rebind before giving up

**Web UI**: `/radio` — full front-panel emulation plus signal meters and per-VFO frequency display.

**CAT link recovery** (also MCP tools): `cat_serial_status` reports the bridge's serial state; `cat_reconnect` re-opens the TCP link to the CAT bridge; `cat_serial_connect` opens its serial side (~4 s startup, raises RTS); `cat_setup_radio` re-pushes channels, volume and power from config and so overwrites front-panel changes. None of them key the transmitter.

**Source**: [`plugins/th9800.py`](../plugins/th9800.py) · **Config**: `[radio]`, `[ptt]`, `[cat]` in `gateway_config.txt`.

## TH-D75

Kenwood TH-D75 handheld with built-in BT serial + audio. Connects over Bluetooth using RFCOMM channel 2 for CAT, RFCOMM channel 1 + SCO for audio.

**Capabilities**
- Band A / B status (frequency, mode, S-meter, tone/CTCSS/DCS, shift, offset)
- D-STAR mode awareness
- Memory load with tone/mode/shift/offset
- Live battery percentage
- TNC mode (APRS / DV-DATA / OFF)

**Bridge architecture** — the D75 plugin doesn't run on the gateway directly. It runs on a small headless Pi (typically a Pi Zero 2 W, called `d75-pi` in our fleet) co-located with the radio, and connects to the gateway over the [Link endpoint protocol](gateway_link.md). The Pi handles BT pairing + SCO audio + RFCOMM CAT and tunnels everything over a single TCP connection.

**Watchdog** — RFCOMM ch2 (CAT) can die while SCO (audio) keeps going. The plugin's watchdog tries a serial-only reconnect first (up to 3 attempts) before escalating to a full BT teardown + reconnect. Wrapped in try/except so an unexpected exception doesn't kill the watchdog thread silently. 60s heartbeat log so you can verify the watchdog is alive.

**Web UI**: `/d75` — band A/B panels, memory loader, S-meter, battery, TNC controls.

**Source**: [`tools/d75_link_plugin.py`](../tools/d75_link_plugin.py) on the Pi · [`audio_sources.py`](../audio_sources.py) `LinkAudioSource` on the gateway.

## KV4P

KV4P-HT — open-source SA818/DRA818-based 2 m / 70 cm handheld interface board. CP2102 USB serial bridge to an Opus-capable audio path.

**Capabilities**
- Squelch (0-8), CTCSS tone (38 tones) on RX and TX
- High / low power (5 W / 1 W)
- Narrow / wide deviation
- S-meter (raw 0-255 normalised to S0-S9)
- Audio level meter
- Configurable volume boost (0-500 %)
- Channel scan + memory store

**Web UI**: `/kv4p` — frequency display, all controls in a single panel, signal + audio meters.

**Source**: [`kv4p_endpoints.py`](../kv4p_endpoints.py) + [`tools/kv4p_link_plugin.py`](../tools/kv4p_link_plugin.py) · **Config**: `[kv4p]` in `gateway_config.txt`.

## RSPduo SDR

SDRplay RSPduo via the SDRplay API + `rtl_airband` for AM/NBFM/WBFM demod and audio output. Two runtime modes selectable at the `/sdr` page:

| Mode | Behaviour | CPU |
|------|-----------|-----|
| **Single tuner, multi-channel** | One tuner up to 10.66 MHz wide bandwidth; up to 2 demod channels within that range. CTCSS gating per channel. | -57 % vs dual |
| **Dual tuner, master/slave** | Two independent tuners (e.g. one VHF, one UHF). Independent center freq, gain, AGC. | higher |

**Channel processing** — each demodulated channel has its own AudioProcessor (gate / HPF / LPF / notch / boost) inside the SDR plugin before it hits the routing graph. Add or remove channels from the UI; the rtl_airband config is rewritten and the process restarted automatically.

**Web UI**: `/sdr` — mode selector, per-tuner controls, per-channel signal meters and config. Frequency entry accepts plain decimal MHz (`146.520`).

**Source**: [`plugins/sdr.py`](../plugins/sdr.py) · **Config**: `[sdr1]` / `[sdr2]` in `gateway_config.txt`.

## AllStar (USRP)

Not a radio — a bridge into the AllStarLink network. `plugins/usrp.py` is an in-gateway plugin that speaks the USRP (DVSwitch) protocol over UDP to a **headless ASL3 bridge node** (no RF hardware) running elsewhere (a container on the gateway box by default, or any reachable host). It appears in the routing UI as an RX source (`usrp`) and a TX sink (`usrp_tx`) like a full-duplex radio.

- **Audio** — full-duplex 8 kHz↔48 kHz resample; RX = audio from the linked node(s), TX = audio the gateway sends out.
- **Control** — the `/usrp` panel connects/disconnects nodes at runtime via the bridge node's AMI (`rpt ilink`), with per-node disconnect on your direct links and a read-only view of conference members reached through a hub.
- **Setup** — the bridge node (ASL3 container, node registration, USRP `rxchannel`, AMI) is documented in [allstar_bridge.md](allstar_bridge.md). Config keys: `ENABLE_USRP`, `USRP_REMOTE_HOST/PORT`, `USRP_LISTEN_PORT`, `USRP_NODE`, `USRP_AMI_*`.
- **Note** — half-duplex link behavior (`duplex = 0` on the node) is intentional and correct for an AllStar link. Reaches the whole ASL3 network; nodes on older/HamVOIP-style app_rpt may not hold the link (a remote-side `newkey` interop issue).

## Link endpoints (FTM-150, IC-7100, future)

Two radios in our fleet talk to the gateway over the [Gateway Link protocol](gateway_link.md):

- **FTM-150** — runs on the `mx` machine (Intel Celeron). AIOC over USB locally; the link endpoint tunnels audio + PTT + status over TCP.
- **IC-7100** — runs on a CM5 (when deployed). CI-V CAT + USB Audio Class 2 audio via a single USB connection; link endpoint plugin (`tools/ic7100_link_plugin.py`) speaks the gateway link protocol.

A new radio of any kind can become a link endpoint by writing a plugin against the link protocol's interface — no gateway code changes. See [gateway_link.md](gateway_link.md) for the spec and [plugin-development.md](plugin-development.md) for the plugin contract.

## Common plugin contract

Every plugin (built-in or link endpoint) exposes:

- `setup(config, gateway=None) → bool` — wire up hardware, start I/O threads, return success
- `get_status() → dict` — JSON-serialisable status snapshot (frequency, mode, S-meter, etc.)
- `get_audio() → (bytes, bool)` — pull one chunk of RX audio (PCM 48 kHz mono, plus an `is_squelch_open` flag)
- `put_audio(chunk)` — push one chunk of TX audio
- `execute(cmd) → dict` — handle a command (`ptt`, `frequency`, `ctcss`, `mode`, ...)
- `teardown()` — clean shutdown

The gateway's routing engine treats every plugin as both a source and a sink (some are RX-only; they just don't expose a sink). See [plugin-development.md](plugin-development.md) for the full interface.

## Source pointers

- Built-in plugins live in `plugins/`: `th9800.py`, `sdr.py`, `packet.py`, `usrp.py`, `usrp2.py`.
  KV4P is a **link-endpoint** plugin, not a local one — `kv4p_endpoints.py` on the
  gateway side and `tools/kv4p_link_plugin.py` on the endpoint.
- Link endpoint plugins live in `tools/` and are deployed to the endpoint host
- Plugin auto-discovery from `plugins/` directory — see [plugin-development.md](plugin-development.md)
