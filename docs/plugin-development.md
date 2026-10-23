# Radio Plugin Development Guide

This guide covers how to add a new radio to the gateway. There are two approaches:

| Approach | Best for | Effort | Gateway changes needed |
|----------|----------|--------|----------------------|
| **Link Endpoint** | Radio on a separate device (Pi, laptop) | Low | None |
| **Local Plugin** | Radio attached to the gateway machine | Low | None (auto-discovery) |

Both approaches give you full integration: the radio appears in the routing UI, can be placed on any bus (listen, solo, duplex, simplex), and supports PTT/frequency/CTCSS if your hardware can.

## Audio Format (everywhere)

All audio in the gateway uses one format:

| Property | Value |
|----------|-------|
| Sample rate | 48000 Hz |
| Bit depth | 16-bit signed little-endian |
| Channels | Mono |
| Chunk size | 2400 samples = 4800 bytes = 50 ms |

If your hardware uses a different format, convert in your plugin.

---

## Approach 1: Local Plugin (auto-discovered)

**Zero gateway code changes.** Drop a `.py` file in `plugins/`, add a config flag, restart.

### Step 1: Copy the template

```bash
cd /path/to/radio-gateway
cp examples/example_plugin/plugin.py plugins/myradio.py
```

The formal contract — required methods, optional hooks, capability flags —
lives in [`plugins/_base.py`](../plugins/_base.py). Read that file for the
authoritative reference; the example plugin under `examples/example_plugin/`
satisfies it.

### Step 2: Edit your plugin

Open `myradio.py` and change:

```python
class MyRadioPlugin:
    PLUGIN_ID = 'myradio'           # lowercase, used in routing config
    PLUGIN_NAME = 'My Radio'        # shown in routing UI
    name = PLUGIN_NAME
    ptt_control = True              # True if your radio can TX
```

Then implement the hardware-specific parts:
- `setup()` — open serial port, USB device, sound card
- `_rx_loop()` — read audio from hardware, queue it
- `put_audio()` — write audio to hardware for TX
- `execute()` — handle PTT, frequency, CTCSS commands
- `cleanup()` — close hardware

The template has detailed comments for each method.

### Step 3: Enable in config

Add to `gateway_config.txt`:

```ini
[myradio]
ENABLE_MYRADIO = True
MYRADIO_DEVICE = /dev/ttyUSB0
MYRADIO_BAUD = 9600
```

The enable key is always `ENABLE_` + `PLUGIN_ID` in uppercase.

Your plugin's own keys don't need an entry in `config_schema.py`: a key with
no schema default is typed by `config_schema.infer()` (number, then
`true`/`false`, else string). If your plugin ships with the repo and you add
its keys to `examples/gateway_config.txt`, add them to `config_schema.py` too,
or `tests/test_config_schema.py` fails; see
[config-reference.md](config-reference.md#adding-a-new-config-key).

### Step 4: Restart gateway

```bash
sudo systemctl restart radio-gateway.service
```

You should see in the logs:

```
  [Plugins] My Radio loaded from myradio.py
```

### Step 5: Connect in routing UI

1. Open the gateway web UI → Routing page
2. Your plugin appears as a source node named `myradio`
3. Drag a connection from it to a bus (e.g., the listen bus)
4. Save

### How it works

```
                        ┌──────────────┐
  plugins/myradio.py →  │ plugin_loader │ → auto-discovers at startup
                        └──────┬───────┘
                               │
                    ┌──────────▼──────────┐
                    │ BusManager          │
                    │  _get_source()      │ → finds by PLUGIN_ID
                    │  sync_listen_bus()  │ → adds to source map
                    │  _tick_loop()       │ → calls get_audio() every 50ms
                    │  _deliver_audio()   │ → routes to sinks
                    └─────────────────────┘
```

### Testing standalone

Before integrating, test your plugin in isolation. Run it from the repo root
so `plugins/` imports:

```python
# test_myplugin.py
from plugins.myradio import MyRadioPlugin

class FakeConfig:
    MYRADIO_DEVICE = '/dev/ttyUSB0'

p = MyRadioPlugin()
assert p.setup(FakeConfig())

# Read 10 chunks
for i in range(10):
    pcm, ptt = p.get_audio(4800)
    if pcm:
        print(f"Chunk {i}: {len(pcm)} bytes, level={p.audio_level}")
    else:
        print(f"Chunk {i}: silence")
    import time; time.sleep(0.05)

p.cleanup()
```

To keep a test, put it in `tests/test_myradio.py` as a plain script that
prints PASS/FAIL lines and exits non-zero on failure (see any existing test).
`python3 tests/run_all.py myradio` runs it with `tests/_hwstubs.py` loaded, so
`hid`, `pyaudio`, `pymumble` and `opuslib` import even on a machine without
them; the stubs refuse to open a device, so test the logic around your
hardware, not the hardware. CI runs every test on each push.

### Errors you catch and carry on from

Don't write `except Exception: pass`. Use `log_util.log_exc()`, which logs
`[swallowed] myradio.py:LINE in function: Error: ...` once per call site per
5 minutes, so a fault inside a 50 ms loop is visible without flooding the log:

```python
from log_util import log_exc

try:
    self._serial.write(cmd)
except Exception:
    log_exc()
```

### Common pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| Plugin not found at startup | File starts with `_` | Rename (files starting with `_` are skipped) |
| "set ENABLE_X to enable" | Config flag missing | Add `ENABLE_MYRADIO = True` to gateway_config.txt |
| get_audio blocks the bus loop | Hardware read is blocking | Move reads to background thread, use queue |
| Audio sounds wrong | Wrong sample rate or format | Resample to 48kHz 16-bit mono in _rx_loop |
| Level meter stuck at 0 | Not updating self.audio_level | Compute RMS in get_audio (see template) |
| Plugin not in routing UI | Not connected in routing config | Add source node and connection in routing UI |
| Radio never keys, but meters move | Source never asserts the PTT flag | The bus keys plugin radios from the flag; see PTT keying below. Check `AUTO_PTT_THRESHOLD` is not `0` |

---

### PTT keying: know which mechanism drives your plugin

A plugin radio and a link endpoint are keyed by **different code paths**, and
the difference is easy to trip over when writing a plugin or a source.

- A **link endpoint** is keyed by `bus_manager`'s deliver loop from audio
  **level** — once the bus exceeds `LINK_AUTO_PTT_THRESHOLD`, it keys.
- A **plugin radio** is keyed from `SoloBus.tick()` Phase 2, driven by the
  source's **PTT flag** (the second element of the `(audio, ptt)` tuple your
  source returns).

So a source that returns `ptt=False` on every path still keys a link endpoint,
but historically never keyed a plugin radio at all — identical wiring, silently
different result. Since v4.6.0 the bus applies a **level trigger as a fallback**
for plugin radios (`AUTO_PTT_THRESHOLD`, defaulting to
`LINK_AUTO_PTT_THRESHOLD`), so both sink kinds now behave alike. It is gated on
`_tx_only` — an RX+TX radio is never keyed from the level of audio it just
received — and honours `tx_muted`.

**If you are writing a source**, assert the PTT flag when you mean to transmit.
The level fallback is a safety net, not the contract.

**If you are writing a plugin**, expect to be keyed through `set_ptt()` from a
`_PttWorker` thread owned by the bus. Never spawn a second keyer for the same
radio: two owners with private `_desired`/`_applied` state is a known failure
mode — see [audio-routing.md](audio-routing.md#one-bus-per-sink).

**If your plugin can transmit, give it the transmit time-out.** Every built-in
PTT path honours `PTT_TIMEOUT_SECONDS` (default 180 s, `0` = off) through
`PttTimeOutTimer` from `gateway_link.py`. The TH-9800 plugin is the pattern:

```python
from gateway_link import PttTimeOutTimer

def setup(self, config, gateway=None):
    self._tot = PttTimeOutTimer(config.PTT_TIMEOUT_SECONDS, self._tot_expired)
    ...

def _tot_expired(self, _key):
    print(f"  [MyRadio] PTT time-out ({self._tot.limit:g}s) -- forcing unkey")
    self._key(False)

def set_ptt(self, on):
    if on:
        if not self._tot.allow_key('myradio'):
            return            # timed out and still held: stay unkeyed
    else:
        self._tot.released('myradio')
    self._key(on)
```

`allow_key()` arms the timer on the first key; after it fires, key-on requests
are refused until they pause for 5 s. Link endpoints get this from the gateway
for free.

**Diagnosing:** no `PTT ON` line at all means the key was never *requested*. A
`PTT ON` with no carrier means the request reached your plugin and the hardware
path is at fault.

---

## Approach 2: Link Endpoint (remote radio)

Your radio runs on a separate device and connects to the gateway over TCP.

### Step 1: Set up the remote device

```bash
# Copy these files to your Pi/device (one directory):
scp gateway:/path/to/radio-gateway/gateway_link.py .
scp gateway:/path/to/radio-gateway/tools/link_endpoint.py .
scp gateway:/path/to/radio-gateway/tools/log_shipper.py .
pip install pyaudio        # for the audio/aioc plugins
```

### Step 2: Test with a sound card

```bash
# List available audio devices
python3 link_endpoint.py --list-devices

# Connect with default sound card
python3 link_endpoint.py --server 192.168.2.140:9700 --name my-radio
```

Your radio should appear in the gateway's routing UI immediately, as source
`my_radio` and TX sink `my_radio_tx`. Off the LAN, use
`--tunnel-url wss://<gateway-host>/ws/link` instead of `--server` (see
[gateway_link.md](gateway_link.md#websocket-cloudflare-tunnel)).

### Step 3: Write a custom plugin (optional)

If you need more than a generic sound card (PTT control, frequency tuning, etc.), write a plugin class:

```python
# my_endpoint_plugin.py
from gateway_link import RadioPlugin

class MyEndpointPlugin(RadioPlugin):
    name = "myradio"
    capabilities = {
        "audio_rx": True,   "audio_tx": True,
        "ptt": True,        "frequency": True,
        "ctcss": False,     "power": False,
        "rx_gain": False,   "tx_gain": False,
        "smeter": False,    "status": True,
    }

    def setup(self, config):
        # Open your hardware
        return True

    def teardown(self):
        # Close hardware
        pass

    def get_audio(self, chunk_size=4800):
        # Return (pcm_bytes, False)
        return self._read_audio(), False

    def put_audio(self, pcm):
        # Play/transmit received audio
        self._write_audio(pcm)

    def execute(self, cmd):
        if cmd.get('cmd') == 'ptt':
            self._set_ptt(cmd['state'])
            return {"ok": True}
        elif cmd.get('cmd') == 'frequency':
            self._tune(cmd['freq_mhz'])
            return {"ok": True}
        return {"ok": False, "error": "unknown"}

    def get_status(self):
        return {"plugin": self.name, "frequency": self._freq}
```

Save it as `tools/myradio_link_plugin.py` (the shipped ones are
`d75_link_plugin.py`, `kv4p_link_plugin.py` and `ic7100_link_plugin.py`), then
register it lazily in `tools/link_endpoint.py` so hosts without its
dependencies still start:

```python
def _load_myradio():
    from myradio_link_plugin import MyEndpointPlugin
    return MyEndpointPlugin

_LAZY_PLUGINS = {
    ...,
    'myradio': _load_myradio,
}
```

```bash
python3 link_endpoint.py --server 192.168.2.140:9700 --name my-radio --plugin myradio
```

Pass per-instance settings with `--plugin-config-json '{"default_freq": 146.52}'`;
they arrive merged into the `config` dict given to `setup()`.

You don't need a time-out timer on the endpoint: the gateway applies
`PTT_TIMEOUT_SECONDS` to every `ptt` command it sends. A local safety unkey
(as `AIOCPlugin` does after 60 s) still helps if the gateway disappears
mid-over.

### Wire Protocol

TCP (or one frame per WebSocket binary message through `/ws/link`), framed
packets: `[type:1][length:2 BE][payload:N]`, payload at most 65535 bytes.

| Type | Byte | Direction | Payload |
|------|------|-----------|---------|
| AUDIO | 0x01 | Both | Raw PCM (4800 bytes) |
| COMMAND | 0x02 | Server → Endpoint | JSON `{"cmd_id":"x","cmd":"ptt","state":true}` |
| STATUS | 0x03 | Both | JSON status dict |
| REGISTER | 0x04 | Endpoint → Server | JSON `{"name":"x","plugin":"x","capabilities":{...}}` |
| ACK | 0x05 | Endpoint → Server | JSON `{"cmd_id":"x","ok":true,...}` |
| LOG | 0x06 | Endpoint → Server | JSON `{"type":"log","lines":[...]}` (stdout/stderr, see [endpoint_logs_design.md](endpoint_logs_design.md)) |

### Connection lifecycle

```
1. Endpoint connects to gateway:9700 (or wss://.../ws/link)
2. Endpoint sends REGISTER within 10 s → gateway shows it in routing UI
   (a name that is already connected is rejected)
3. Both sides exchange AUDIO frames (50ms chunks, continuous)
4. Gateway sends COMMAND frames → endpoint replies with ACK
   (ptt commands pass the gateway's PTT time-out first)
5. Gateway sends a heartbeat every 5 s; endpoint sends STATUS every 10 s
6. TCP close, or 90 s with no frame → gateway drops the endpoint
7. Endpoint reconnects with backoff (2 s, growing to 60 s)
```

### Auto-start on boot (systemd)

```ini
# /etc/systemd/system/radio-endpoint.service
[Unit]
Description=Radio Gateway Endpoint
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 link_endpoint.py --server 192.168.2.140:9700 --name my-radio
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable radio-endpoint
sudo systemctl start radio-endpoint
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Gateway Machine                           │
│                                                              │
│  plugins/myradio.py ──┐                                      │
│  plugins/th9800.py ───┤                                      │
│  plugins/sdr.py ──────┤     ┌──────────────┐                │
│  plugins/usrp.py ─────┼────>│  BusManager   │──> Mumble     │
│                       │     │              │──> Broadcastify │
│  Link Endpoints ──────┤     │  Listen Bus  │──> Speaker     │
│   (TCP :9700 or       │     │  Solo Bus    │──> Recording   │
│    wss /ws/link)      │     │  Duplex Bus  │──> Transcribe  │
│    ├─ d75 ────────────┤     │  Simplex Bus │──> WebSocket   │
│    ├─ kv4p-v ─────────┤     │              │                │
│    ├─ ftm-150 ────────┤     │              │                │
│    └─ my-radio ───────┘     └──────────────┘                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                          ▲
           TCP :9700      │
┌─────────────────────────┴───────────────────────┐
│              Remote Device (Pi)                  │
│  link_endpoint.py + my_endpoint_plugin.py        │
│  ├─ Reads audio from hardware                    │
│  ├─ Sends AUDIO frames to gateway                │
│  ├─ Receives COMMAND frames (PTT, freq, etc.)    │
│  └─ Sends STATUS heartbeats                      │
└──────────────────────────────────────────────────┘
```

## Existing plugins for reference

| File | Radio | Type | Connection |
|------|-------|------|------------|
| `plugins/th9800.py` | TYT TH-9800 | Local | AIOC USB + CAT TCP; uses `PttTimeOutTimer` |
| `plugins/sdr.py` | RSPduo SDR | Local | PipeWire + rtl_airband |
| `plugins/packet.py` | Packet TNC | Local | Direwolf on a link endpoint |
| `plugins/usrp.py`, `plugins/usrp2.py` | AllStarLink bridge | Local | USRP over UDP + AMI |
| `gateway_link.py` (AudioPlugin) | Generic sound card | Endpoint | PyAudio/ALSA |
| `gateway_link.py` (AIOCPlugin) | AIOC + HID GPIO PTT | Endpoint | PyAudio + HID |
| `tools/kv4p_link_plugin.py` | KV4P HT | Endpoint | USB serial + Opus |
| `tools/d75_link_plugin.py` | Kenwood TH-D75 | Endpoint | Bluetooth RFCOMM + SCO |
| `tools/ic7100_link_plugin.py` | Icom IC-7100 | Endpoint | CI-V + USB audio |
| `examples/example_plugin/plugin.py` | Template | Local | (your hardware) |
| `plugins/_base.py` | Formal contract reference (Protocol + capability flags + optional hooks) | — | — |
