# Gateway Link -- Duplex Audio + Command Protocol

## Vision

The Gateway Link is the foundation for a **distributed radio gateway** where
multiple radios, SDRs, and gateway instances connect as a mesh over TCP.
Today, each radio (TH-9800, TH-D75, KV4P) has bespoke driver code tightly
coupled to the gateway. The Link protocol and plugin architecture will
eventually replace all hardware-specific code with a uniform interface:

**End State:** Every radio is a plugin. The gateway is a mixer + web UI +
protocol hub. Radios can be local (USB) or remote (TCP). Adding a new radio
type means writing one Python class (~100 lines) that implements `RadioPlugin`.

```
Current Architecture:                    Target Architecture:

Gateway                                  Gateway (mixer + UI + protocol)
+-- AIOC driver (built-in)               +-- LinkServer (accepts endpoints)
+-- TH9800 CAT client (built-in)         |   +-- endpoint: TH9800Plugin
+-- D75 BT proxy + client (built-in)     |   +-- endpoint: D75Plugin
+-- KV4P serial driver (built-in)        |   +-- endpoint: KV4PPlugin
+-- SDR rtl_airband manager (built-in)   |   +-- endpoint: AudioPlugin (generic)
                                         |   +-- endpoint: SDRPlugin
                                         |   +-- endpoint: ... (any future radio)
                                         +-- Local plugins (same machine, no TCP)
```

---

## Files

| File | Purpose |
|------|---------|
| `gateway_link.py` | Protocol, server, client, plugin base, AudioPlugin. Self-contained -- zero imports from other gateway modules. Endpoint scripts can import it standalone. |
| `tools/link_endpoint.py` | Standalone endpoint script. Connects to a gateway master and streams duplex audio. Plugin registry, gain control, status reporter. |
| `audio_sources.py` (class `LinkAudioSource`) | Mixer integration. Receives audio from the server's frame dispatch, provides it to the gateway's audio mixer with level metering and boost. |
| `gateway_core.py` (init block) | Wires `GatewayLinkServer` to `LinkAudioSource`, manages connected state callbacks. |

---

## MVP (v1 -- current implementation)

### What's Built

- **Framed TCP protocol:** 5 frame types (AUDIO, COMMAND, STATUS, REGISTER, ACK)
- **GatewayLinkServer:** single endpoint connection, frame dispatch, 5s heartbeat
- **GatewayLinkClient:** auto-reconnect (5s backoff), registration on connect
- **AudioPlugin:** generic sound card via PyAudio -- any ALSA/PipeWire device
- **RadioPlugin base class:** setup/teardown/get_audio/put_audio/execute/get_status
- **LinkAudioSource:** mixer integration with level metering, audio boost, duck support
- **Standalone endpoint script:** `tools/link_endpoint.py` with device listing, gain, status reporter
- **Dashboard integration:** LINK audio bar (orange), status in gateway status dict

### What's NOT Built (v2+)

- Multiple simultaneous endpoint connections
- KV4P, D75, TH9800, SDR hardware plugins
- Command execution (PTT, frequency, CTCSS, power, volume)
- Endpoint management web UI
- Auto-discovery (mDNS/Bonjour)
- Encryption / TLS
- Cross-internet relay via Cloudflare tunnel
- Local plugin mode (same machine, no TCP overhead)

---

## Protocol

### Frame Format

Every message on the wire is a framed packet:

```
+--------+--------+--------+-----...-----+
| Type   | Length (big-endian)| Payload    |
| 1 byte | 2 bytes           | 0-65535 B  |
+--------+--------+--------+-----...-----+
```

- **Type** (1 byte): identifies the frame kind (see table below)
- **Length** (2 bytes, big-endian unsigned): payload size in bytes
- **Payload** (variable): raw bytes (audio) or UTF-8 JSON (commands, status, registration, ack)

Maximum payload size is 65535 bytes (limited by the 2-byte length field).

### Frame Types

| Type | Value | Payload | Direction | Description |
|------|-------|---------|-----------|-------------|
| AUDIO | `0x01` | Raw PCM bytes | Bidirectional | 48 kHz, 16-bit signed LE, mono. 4800 bytes = 50 ms. |
| COMMAND | `0x02` | JSON dict | Server-to-endpoint (primarily) | Action request with `cmd` key. |
| STATUS | `0x03` | JSON dict | Bidirectional | Heartbeat or state report. |
| REGISTER | `0x04` | JSON dict | Endpoint-to-server | Sent once on connect. Identifies the endpoint. |
| ACK | `0x05` | JSON dict | Endpoint-to-server | Response to a COMMAND, keyed by `cmd_id`. |

### Audio Format

Audio frames carry raw PCM with these parameters:

- Sample rate: **48000 Hz**
- Bit depth: **16-bit signed little-endian**
- Channels: **1 (mono)**
- Chunk size: **4800 bytes** (50 ms)

Both directions use the same format. The endpoint reads from its hardware
(microphone, radio RX) and sends AUDIO frames to the server. The server sends
mixed gateway audio back as AUDIO frames.

### Registration

When a client connects, it immediately sends a REGISTER frame:

```json
{
  "name": "garage-radio",
  "plugin": "audio",
  "capabilities": ["audio_rx", "audio_tx"],
  "version": "1.0"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable endpoint name (shown on dashboard) |
| `plugin` | string | Plugin type identifier (`audio`, `kv4p`, `d75`, etc.) |
| `capabilities` | list[str] | What this endpoint can do (see below) |
| `version` | string | Protocol version |

**Capability strings** (current and planned):

| Capability | Meaning |
|------------|---------|
| `audio_rx` | Endpoint can capture and send audio to the server |
| `audio_tx` | Endpoint can receive audio from the server and play/transmit it |
| `ptt` | Endpoint can key/unkey a transmitter |
| `frequency` | Endpoint can tune to a frequency |
| `ctcss` | Endpoint supports CTCSS tone setting |
| `power` | Endpoint can adjust TX power |
| `status` | Endpoint reports detailed hardware status |

The server stores the registration info in `endpoint_info` and logs it.

### Status Heartbeat

The server sends a STATUS frame every 5 seconds:

```json
{
  "type": "heartbeat",
  "uptime": 3600.5
}
```

The endpoint sends its own STATUS frames at a configurable interval
(default 10 seconds via `--status-interval`):

```json
{
  "plugin": "audio",
  "device": "default",
  "rate": 48000,
  "input_active": true,
  "output_active": true,
  "uptime": 120.3
}
```

These heartbeats serve as keepalive and monitoring. If no frames arrive for
an extended period, the TCP connection will eventually fail and trigger
auto-reconnect on the client side.

### Commands (planned v2)

Commands follow a request/response pattern. The server sends a COMMAND frame,
the endpoint executes it and replies with an ACK frame.

```json
// COMMAND (server -> endpoint)
{
  "cmd_id": "abc123",
  "cmd": "ptt",
  "state": true
}

// ACK (endpoint -> server)
{
  "cmd_id": "abc123",
  "ok": true
}
```

Planned command types:

| Command | Fields | Description |
|---------|--------|-------------|
| `ptt` | `state: bool` | Key or unkey the transmitter |
| `frequency` | `freq_mhz: float` | Tune to a frequency |
| `ctcss` | `tone: float` | Set CTCSS tone (0 to disable) |
| `power` | `level: str` | Set TX power (low/mid/high) |
| `volume` | `level: float` | Set audio volume (0.0 - 1.0) |
| `query` | `fields: list` | Request specific status fields |

The `RadioPlugin.execute()` method handles command dispatch. The base class
returns `{"ok": false, "error": "not implemented"}` for all commands.

---

## Plugin Architecture

### RadioPlugin Base Class

All hardware plugins inherit from `RadioPlugin` in `gateway_link.py`:

```python
class RadioPlugin:
    name = "base"
    capabilities = []

    def setup(self, config):
        """Initialize hardware. config is a dict from command-line args."""
        pass

    def teardown(self):
        """Clean shutdown of hardware."""
        pass

    def get_audio(self):
        """Read one chunk of PCM audio from hardware.
        Returns bytes (48 kHz 16-bit signed LE mono, 4800 bytes = 50 ms)
        or None if no data is available."""
        return None

    def put_audio(self, pcm):
        """Write PCM audio to hardware for playback / transmission."""
        pass

    def execute(self, cmd):
        """Handle a command from the master gateway.
        cmd is a dict like {"cmd": "ptt", "state": true}.
        Returns a result dict."""
        return {"ok": False, "error": "not implemented"}

    def get_status(self):
        """Return current hardware state as a dict."""
        return {"plugin": self.name}
```

**Key design decisions:**

1. **Self-contained module:** `gateway_link.py` has zero imports from other
   gateway modules. This means the endpoint script can run standalone on a
   remote machine with only `gateway_link.py` copied over.

2. **Lazy imports:** `AudioPlugin.setup()` imports `pyaudio` lazily so the
   module has no hard dependency on PyAudio at import time.

3. **50 ms audio chunks:** All audio is chunked at 4800 bytes (50 ms at 48 kHz
   mono 16-bit). This matches the gateway's internal audio tick rate.

4. **Thread safety:** `GatewayLinkServer` and `GatewayLinkClient` use a
   `_send_lock` for thread-safe writes. Reads happen in a dedicated reader
   thread. Callbacks fire from the reader thread.

### Writing a New Plugin

To add support for a new radio, create a class that extends `RadioPlugin`:

**Step 1: Define the plugin class**

```python
# In gateway_link.py or a separate file

class KV4PPlugin(RadioPlugin):
    """KV4P HT radio plugin -- CP2102 USB-serial with DRA818."""

    name = "kv4p"
    capabilities = ["audio_rx", "audio_tx", "ptt", "frequency", "ctcss"]

    def __init__(self):
        super().__init__()
        self._serial = None
        self._running = False

    def setup(self, config):
        """Open serial port and initialize DRA818 module."""
        import serial
        port = config.get('device', '/dev/ttyUSB0')
        self._serial = serial.Serial(port, 9600, timeout=1)
        # ... DRA818 init sequence ...
        self._running = True

    def teardown(self):
        """Close serial port."""
        self._running = False
        if self._serial:
            self._serial.close()
            self._serial = None

    def get_audio(self):
        """Read 50 ms of audio from the KV4P's Opus stream."""
        # ... decode Opus, resample to 48 kHz, return 4800 bytes ...
        return pcm_bytes

    def put_audio(self, pcm):
        """Encode and send audio to the KV4P for transmission."""
        # ... encode to Opus, send over serial ...
        pass

    def execute(self, cmd):
        """Handle PTT, frequency, CTCSS commands."""
        action = cmd.get('cmd')
        if action == 'ptt':
            # ... key/unkey via serial ...
            return {"ok": True}
        elif action == 'frequency':
            # ... tune DRA818 ...
            return {"ok": True, "frequency": cmd['freq_mhz']}
        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        return {
            "plugin": self.name,
            "device": self._serial.port if self._serial else None,
            "connected": self._serial is not None and self._serial.is_open,
        }
```

**Step 2: Register the plugin**

In `tools/link_endpoint.py`, add it to the `_PLUGINS` dict:

```python
from gateway_link import KV4PPlugin

_PLUGINS = {
    'audio': AudioPlugin,
    'kv4p': KV4PPlugin,
}
```

**Step 3: Run the endpoint**

```bash
python3 tools/link_endpoint.py \
    --server 192.168.2.140:9700 \
    --name mobile-kv4p \
    --plugin kv4p \
    --device /dev/ttyUSB0
```

### Built-in Plugins

#### AudioPlugin

Generic sound card plugin using PyAudio (portaudio). Works with any
ALSA or PipeWire audio device.

- **Name:** `audio`
- **Capabilities:** `audio_rx`, `audio_tx`
- **Config keys:** `device` (name substring or index), `rate` (default 48000), `channels` (default 1)
- **Device matching:** tries integer index first, then case-insensitive name substring search
- **Audio format:** 48 kHz, 16-bit signed LE, mono, 50 ms chunks (4800 bytes / 2400 frames)

#### Planned Plugins

| Plugin | Radio | Transport | Key challenge |
|--------|-------|-----------|---------------|
| `KV4PPlugin` | KV4P HT | USB serial (CP2102) | Opus codec, DRA818 38-tone CTCSS |
| `D75Plugin` | TH-D75 | Bluetooth SCO via proxy | 48-byte SCO frames, non-blocking btstart |
| `TH9800Plugin` | TH-9800 | CAT serial (FTDI) | Dual VFO, DISPLAY_TEXT parsing |
| `SDRPlugin` | Any SDR | rtl_airband or direct SoapySDR | Read-only (no TX), multiple tuners |

---

## Gateway-Side Integration

### LinkAudioSource

`LinkAudioSource` (in `audio_sources.py`) is the mixer source that receives
audio from the Link server and feeds it into the gateway's audio pipeline:

- **Source name:** `LINK`
- **Priority:** configurable via `LINK_AUDIO_PRIORITY` (default 3)
- **PTT control:** `False` (link audio does not key the radio by default)
- **Queue:** `deque(maxlen=16)` -- the server's `on_audio` callback pushes frames here
- **Level metering:** RMS-based, with configurable display gain
- **Audio boost:** configurable multiplier via `LINK_AUDIO_BOOST`
- **Duck support:** configurable via `LINK_AUDIO_DUCK`
- **TX path:** `write_tx_audio()` sends gateway audio back to the endpoint via the server

### Connection State

The gateway core wires connection state between the server and the audio source:

1. On REGISTER: `link_audio_source.server_connected = True`
2. On disconnect: `link_audio_source.server_connected = False`

When `server_connected` is False, `get_audio()` returns None and the level
meter decays to zero.

### Dashboard

The LINK source appears as an orange audio bar in the shell frame's level
display, after MON (purple). The bar shows real-time audio levels from the
connected endpoint.

---

## Configuration

All Link configuration lives in `gateway_config.txt` under the relevant section:

| Key | Default | Description |
|-----|---------|-------------|
| `ENABLE_GATEWAY_LINK` | `False` | Enable the Gateway Link server |
| `LINK_PORT` | `9700` | TCP port for endpoint connections |
| `LINK_AUDIO_PRIORITY` | `3` | Mixer priority for link audio (lower = higher priority) |
| `LINK_AUDIO_DUCK` | `False` | Whether link audio ducks SDR sources |
| `LINK_AUDIO_BOOST` | `1.0` | Audio level multiplier for incoming link audio |
| `LINK_AUDIO_DISPLAY_GAIN` | `1.0` | Display gain for the LINK level bar |

---

## Usage

### Starting the Server (gateway side)

Set `ENABLE_GATEWAY_LINK = true` in `gateway_config.txt` and restart the
gateway. The server will listen on the configured port (default 9700).

### Running an Endpoint

The standalone endpoint script requires only `gateway_link.py` and Python 3
(plus `pyaudio` for the audio plugin).

**List available audio devices:**

```bash
python3 tools/link_endpoint.py --list-devices
```

Output:
```
[Endpoint] Audio devices (12):
  [0] HDA Intel PCH: ALC887-VD Analog  (IN/OUT)
  [1] HDA Intel PCH: ALC887-VD Digital  (OUT)
  [4] AIOC: USB Audio  (IN/OUT)
  ...
```

**Auto-discover gateway via mDNS (no IP needed):**

```bash
python3 tools/link_endpoint.py --name living-room --plugin aioc
```

The endpoint uses Avahi to find `_radiogateway._tcp` on the local network.
Requires `avahi-utils` (Debian) or `avahi` (Arch). Falls back to error if
no gateway found — use `--server` in that case.

**Connect with explicit server address:**

```bash
python3 tools/link_endpoint.py \
    --server 192.168.2.140:9700 \
    --name living-room
```

**Connect with a specific device and gain:**

```bash
python3 tools/link_endpoint.py \
    --server 192.168.2.140:9700 \
    --name garage-radio \
    --device "AIOC" \
    --gain 2.0
```

**Connect with an ALSA hardware device:**

```bash
python3 tools/link_endpoint.py \
    --server 192.168.2.140:9700 \
    --name remote-sdr \
    --device hw:1,0
```

### Endpoint Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--server HOST:PORT` | Yes | -- | Gateway master address |
| `--name NAME` | Yes | -- | Endpoint name (shown on dashboard) |
| `--plugin PLUGIN` | No | `audio` | Hardware plugin to use |
| `--device DEVICE` | No | system default | Audio device name, index, or path |
| `--rate HZ` | No | `48000` | Sample rate |
| `--gain FLOAT` | No | `1.0` | Input gain multiplier |
| `--status-interval SEC` | No | `10.0` | How often to send status to master |
| `--list-devices` | No | -- | List audio devices and exit |

### Deploying to a Remote Machine

The endpoint only needs two files:

1. `gateway_link.py` -- protocol + plugins
2. `tools/link_endpoint.py` -- endpoint script

Copy them to the remote machine:

```bash
scp gateway_link.py tools/link_endpoint.py user@remote:~/link/
```

On the remote machine:

```bash
pip install pyaudio  # for AudioPlugin
cd ~/link
python3 link_endpoint.py --server gateway-ip:9700 --name remote-node
```

No other gateway dependencies are needed.

---

## Development Roadmap

### Completed

**v1 — MVP** ✓
- Framed TCP protocol (AUDIO, COMMAND, STATUS, REGISTER, ACK)
- Duplex audio streaming
- AudioPlugin (generic sound card via PyAudio)
- Standalone endpoint script with auto-reconnect
- LinkAudioSource mixer integration with level metering

**v2 — Commands & Hardware** ✓
- PTT command with ACK response and 60s safety timeout
- RX/TX gain commands (-10 to +10 dB, persisted on endpoint)
- Status query with structured response
- AIOCPlugin (audio + HID GPIO PTT via /proc/asound/cards)
- Structured capabilities registration

**v3 — Multi-Endpoint** ✓
- N simultaneous endpoint connections (dict keyed by name)
- Dynamic LinkAudioSource creation/destruction per endpoint
- Per-endpoint controls on controls page (PTT, RX/TX bars, gain, mute)
- Per-endpoint status on dashboard with live endpoint state
- Per-endpoint audio bars in shell frame
- Gateway settings persistence (~/.config/radio-gateway/link_endpoints.json)
- Bidirectional heartbeat (5s) with dead peer detection (15s)
- Cable-pull resilience (10s socket timeout both sides)

**v4 — Discovery** ✓
- mDNS auto-discovery via Avahi (gateway publishes `_radiogateway._tcp`)
- Endpoint discovers gateway on LAN with zero config
- Fallback to manual `--server HOST:PORT`

### Current — Mixer Integration

The gateway mixer currently treats all link endpoints the same — each
gets its own `LinkAudioSource` feeding into the broadcast-style additive
mixer. All endpoints hear the full gateway mix (unless TX muted).

**Planned: Conditional Mixer Matrix**
- Per-endpoint routing: endpoint A hears endpoint B but not C
- Source selection: choose which gateway sources an endpoint receives
  (e.g. "pi-aioc gets SDR1+SDR2 only, not radio RX")
- Cross-link: endpoint A's audio routed to endpoint B's TX (relay)
- Web UI matrix editor: grid of sources × endpoints with checkboxes

### Next — Radio Plugins

**KV4PPlugin** — USB serial radio with frequency/CTCSS/power control
- Wraps existing `kv4p-ht` Python package
- Capabilities: audio_rx, audio_tx, ptt, frequency, ctcss, power, smeter
- First plugin with full radio control commands
- Proves the command language handles real tuning operations

**D75Plugin** — Bluetooth radio (TH-D75)
- Wraps existing `remote_bt_proxy.py` BT serial + SCO audio
- Replaces bespoke D75CATClient + D75AudioSource + remote proxy
- Single TCP connection replaces dual ports (9750 + 9751)
- Complex: BT RFCOMM timing, SCO frame pacing, btstart sequencing

**TH9800Plugin** — CAT serial radio (TH-9800)
- Wraps existing `RadioCATClient` + `TH9800_CAT.py`
- Capabilities: ptt, frequency, volume, smeter

**SDRPlugin** — RTL-SDR / RSPduo receiver
- Wraps existing `RTLAirbandManager`
- RX only (no PTT/TX)
- Capabilities: audio_rx, frequency, modulation

### Future — Network & Mesh

- TLS encryption for internet-facing links
- Cross-internet relay via Cloudflare tunnel
- Local plugin mode (same machine, bypass TCP for lower latency)
- Gateway-to-gateway linking (mesh topology)
- Endpoint deployment tooling (systemd service, auto-update)

---

## Design Notes

### Why a Custom Protocol (not HTTP/WebSocket/gRPC)?

- **Low latency:** Audio needs sub-100ms round-trip. HTTP adds overhead.
  WebSocket is close but adds framing complexity. A simple 3-byte header
  with raw PCM is the minimum overhead.
- **Bidirectional audio:** Both sides send and receive audio simultaneously.
  HTTP is request/response. WebSocket works but adds masking overhead.
- **Simplicity:** The entire protocol is ~100 lines of Python. No dependencies.
  No serialization library. No schema files.
- **Self-contained:** The module can be copied to a remote machine and run
  with zero gateway dependencies.

### Why Plugins (not a Generic Audio Bridge)?

A generic audio bridge would only handle audio. But radios need control:
PTT, frequency, CTCSS, power, status. By abstracting hardware into plugins,
the same protocol carries both audio and control. The gateway can tune a
remote radio the same way it tunes a local one.

### Thread Model

```
Server side:
  AcceptThread  -- waits for incoming connections
  ReaderThread  -- reads frames, dispatches to callbacks
  HeartbeatThread -- sends STATUS every 5s
  (Main thread sends audio via send_audio)

Client side:
  ConnectThread -- connects + auto-reconnects, spawns reader
  ReaderThread  -- reads frames, dispatches to callbacks
  StatusThread  -- sends STATUS at configurable interval
  MainThread    -- audio capture loop (get_audio + send_audio)
```

All sends go through a `_send_lock` mutex. Callbacks fire from the reader
thread -- handlers must be fast and non-blocking to avoid stalling the
frame pipeline.
