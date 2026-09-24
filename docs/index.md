# Documentation Index

Pick the path that matches what you're trying to do.

## 📥 I want to install or set up the hardware

| Doc | What it covers |
|-----|---------------|
| [../INSTALL.md](../INSTALL.md) | Full install: gateway service, dependencies, systemd units, audio loopback. |
| [HARDWARE_SETUP.md](HARDWARE_SETUP.md) | Physical wiring: AIOC, relays, antennas, the rack |
| [pi-endpoint-build.md](pi-endpoint-build.md) | Building a Pi-based link endpoint (D75 BT bridge or CM5 AIOC HT) |
| [config-reference.md](config-reference.md) | Every `gateway_config.txt` key, grouped by feature |

## 🎙️ I want to operate the radio gateway

| Doc | What it covers |
|-----|---------------|
| [web-ui.md](web-ui.md) | The shell + dashboard sub-pages (Overview / Endpoints / Services / Operate) |
| [fleet-manager.md](fleet-manager.md) | Document-driven autonomous monitoring + repairs |
| [transcription-pool.md](transcription-pool.md) | Multi-machine ASR (Moonshine + Whisper) |
| [audio-routing.md](audio-routing.md) | Bus mixer, processing chain, streaming, speaker modes |
| [radio-plugins.md](radio-plugins.md) | TH-9800 / TH-D75 / KV4P / SDR / link endpoints (FTM-150, IC-7100) |
| [windows-client.md](windows-client.md) | Windows operator position — program feed + mic, live device switching, self-update |
| [allstar_bridge.md](allstar_bridge.md) | AllStarLink bridge node + in-gateway USRP plugin + `/usrp` panel |
| [packet-radio.md](packet-radio.md) | Packet TNC + APRS + Winlink email |
| [loop-recorder.md](loop-recorder.md) | Per-bus rolling buffer + scrubback |
| [mcp.md](mcp.md) | 167 MCP tools for AI control + the tools the bots use |
| [TTS_TEXT_COMMANDS_GUIDE.md](TTS_TEXT_COMMANDS_GUIDE.md) | TTS engines + voices, soundboard, background music + repeating message, smart announcements, AI text |

## 🔌 I want to extend it — write a plugin or endpoint

| Doc | What it covers |
|-----|---------------|
| [plugin-development.md](plugin-development.md) | Building a new radio plugin (bus-based interface) |
| [gateway_link.md](gateway_link.md) | Link endpoint protocol — connect remote radios over TCP |
| [endpoint_logs_design.md](endpoint_logs_design.md) | How every link endpoint's stdout/stderr is captured under `logs/endpoints/` |
| [mixer-v2-design.md](mixer-v2-design.md) | Audio routing architecture: buses, sinks, processors |

## 📜 I want to understand a past decision

| Doc | What it covers |
|-----|---------------|
| [history/audio-quality-research.md](history/audio-quality-research.md) | RNNoise vs DeepFilterNet evaluation notes |
| [history/mixer-v2-progress.md](history/mixer-v2-progress.md) | v2 mixer build log + decision history |
| [history/ftm150-reverse-engineering.md](history/ftm150-reverse-engineering.md) | FTM-150 control head RE notes (project shelved) |
| [windows-client-audio-investigation.md](windows-client-audio-investigation.md) | Why the Windows client produced silent chunks, and the 44.1 kHz + soxr fix |
| [../CHANGELOG.md](../CHANGELOG.md) | Versioned release notes |

## 🎨 Conventions for these docs

Each docs page follows the same shape so they're predictable:

1. **One-line summary** at the top — answer "what is this".
2. **Overview / why this exists** — the problem that motivated it.
3. **Architecture / how it works** — the model, diagrams welcome.
4. **Usage / API** — how to do the thing.
5. **Configuration** — keys, defaults, where they live.
6. **Notes from running it** — gotchas, edge cases, lessons.
7. **Source pointers** — file paths so a reader can jump straight to code.

Pages older than this format will be migrated incrementally — newer pages
(`transcription-pool.md`, `fleet-manager.md`) are the reference templates.
