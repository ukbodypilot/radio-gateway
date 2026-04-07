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
cd ~/Downloads/radio-gateway/plugins
cp example_radio.py myradio.py
```

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

Before integrating, test your plugin in isolation:

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

### Common pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| Plugin not found at startup | File starts with `_` | Rename (files starting with `_` are skipped) |
| "set ENABLE_X to enable" | Config flag missing | Add `ENABLE_MYRADIO = True` to gateway_config.txt |
| get_audio blocks the bus loop | Hardware read is blocking | Move reads to background thread, use queue |
| Audio sounds wrong | Wrong sample rate or format | Resample to 48kHz 16-bit mono in _rx_loop |
| Level meter stuck at 0 | Not updating self.audio_level | Compute RMS in get_audio (see template) |
| Plugin not in routing UI | Not connected in routing config | Add source node and connection in routing UI |

---

## Approach 2: Link Endpoint (remote radio)

Your radio runs on a separate device and connects to the gateway over TCP.

### Step 1: Set up the remote device

```bash
# Copy these two files to your Pi/device:
scp gateway:~/Downloads/radio-gateway/tools/link_endpoint.py .
scp gateway:~/Downloads/radio-gateway/gateway_link.py .
```

### Step 2: Test with a sound card

```bash
# List available audio devices
python3 link_endpoint.py --list-devices

# Connect with default sound card
python3 link_endpoint.py --server 192.168.2.140:9700 --name my-radio
```

Your radio should appear in the gateway's routing UI immediately.

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

Register and run:

```bash
# In link_endpoint.py, add to _PLUGINS dict:
# from my_endpoint_plugin import MyEndpointPlugin
# _PLUGINS['myradio'] = MyEndpointPlugin

python3 link_endpoint.py --server 192.168.2.140:9700 --name my-radio --plugin myradio
```

### Wire Protocol

TCP, framed packets: `[type:1][length:2 BE][payload:N]`

| Type | Byte | Direction | Payload |
|------|------|-----------|---------|
| AUDIO | 0x01 | Both | Raw PCM (4800 bytes) |
| COMMAND | 0x02 | Server → Endpoint | JSON `{"cmd_id":"x","cmd":"ptt","state":true}` |
| STATUS | 0x03 | Both | JSON status dict |
| REGISTER | 0x04 | Endpoint → Server | JSON `{"name":"x","plugin":"x","capabilities":{...}}` |
| ACK | 0x05 | Endpoint → Server | JSON `{"cmd_id":"x","ok":true,...}` |

### Connection lifecycle

```
1. Endpoint connects to gateway:9700
2. Endpoint sends REGISTER frame → gateway shows it in routing UI
3. Both sides exchange AUDIO frames (50ms chunks, continuous)
4. Gateway sends COMMAND frames → endpoint replies with ACK
5. Both send STATUS heartbeats (every 5-10s)
6. TCP close or 15s timeout → cleanup
7. Endpoint auto-reconnects every 5s
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
│  th9800_plugin.py ────┤                                      │
│  sdr_plugin.py ───────┤     ┌──────────────┐                │
│  kv4p_plugin.py ──────┼────>│  BusManager   │──> Mumble     │
│                       │     │              │──> Broadcastify │
│  Link Endpoints ──────┤     │  Listen Bus  │──> Speaker     │
│   (TCP :9700)         │     │  Solo Bus    │──> Recording   │
│    ├─ d75-bt ─────────┤     │  Duplex Bus  │──> Transcribe  │
│    ├─ ftm-150 ────────┤     │  Simplex Bus │──> WebSocket   │
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
| `th9800_plugin.py` | Yaesu TH-9800 | Local | AIOC USB + CAT TCP |
| `kv4p_plugin.py` | KV4P HT | Local | USB serial + Opus |
| `sdr_plugin.py` | RSPduo SDR | Local | PipeWire + rtl_airband |
| `gateway_link.py` (AudioPlugin) | Generic sound card | Endpoint | PyAudio/ALSA |
| `gateway_link.py` (AIOCPlugin) | AIOC + CM108 PTT | Endpoint | PyAudio + HID |
| `plugins/example_radio.py` | Template | Local | (your hardware) |
