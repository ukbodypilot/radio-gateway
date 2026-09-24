# Text Commands & TTS Guide

## TTS Engines

Three engines are available, selected via `TTS_ENGINE` in `gateway_config.txt`:

| Engine | Quality | Requires | Voices selectable |
|--------|---------|----------|-------------------|
| `kokoro` | High — offline neural (default) | ONNX models in `tools/models/kokoro/` (~340 MB, downloaded by install.sh) | 55 across 9 languages |
| `edge` | High — Microsoft Neural (online) | Internet + `edge-tts` pip package | 47 English voices, 14 locales |
| `gtts` | Moderate — Google Translate (online) | Internet + `gtts` pip package | 22 accents / languages |

**Switching engines is live.** The engine dropdown sits next to the voice
dropdown on `/controls` and `/dashboard/operate`; picking one rebuilds the
backend in place and persists the choice — no gateway restart. Engines whose
pip package is missing appear greyed out rather than hidden. A switch that
fails leaves the previous working engine running, and only writes config on
success, so you cannot end up booting into a broken engine.

The voice list follows the active engine automatically. Edge labels carry
Microsoft's own personality tags where useful — `Ana (US F) — Cartoon`,
`Christopher (US M) — Authority`, `Roger (US M) — Lively`.

**From an MCP client:** `tts_engine_status` lists engines, which is active and
whether each is importable; `tts_engine_set` performs the same live swap as the
dropdown and persists `TTS_ENGINE`.

### Installing / upgrading

```bash
# Kokoro (default engine) — pip package only, model files handled by install.sh
pip install kokoro-onnx

# Download Kokoro model files manually if install.sh was not used:
mkdir -p tools/models/kokoro
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
wget -O tools/models/kokoro/kokoro-v1.0.onnx "$BASE/kokoro-v1.0.onnx"
wget -O tools/models/kokoro/voices-v1.0.bin  "$BASE/voices-v1.0.bin"

# Edge / gTTS (online fallback engines)
pip install edge-tts gtts
```

---

## Configuration (`gateway_config.txt`)

```ini
[tts]
ENABLE_TTS = true
TTS_ENGINE = kokoro             # kokoro | edge | gtts

# Kokoro: string voice ID from the list below
KOKORO_DEFAULT_VOICE = af_heart

# gTTS/Edge: numeric accent (1=US 2=British 3=Australian 4=Indian …)
TTS_DEFAULT_VOICE = 1

TTS_VOLUME = 1.0                # Volume multiplier (1.0 = normal)
TTS_SPEED  = 1.0                # Speed multiplier (1.0 = normal; >1.0 faster; requires ffmpeg)
PTT_TTS_DELAY = 0.5             # PTT pre-key settle time before audio starts
```

---

## Kokoro Voice List

The voice dropdown in `/controls` and the dashboard's Operate page (`/dashboard/operate`) is populated automatically from the active engine. For reference, all 54 Kokoro voices:

### American English (prefix `a`)
| Voice ID | Description |
|----------|-------------|
| `af_heart` | Heart (US F) ★★★★ — warm, natural |
| `af_bella` | Bella (US F) ★★★½ |
| `af_nicole` | Nicole (US F) ★★★ — whisper-y |
| `af_aoede` | Aoede (US F) ★★★ |
| `af_kore` | Kore (US F) ★★★ |
| `af_sarah` | Sarah (US F) ★★★ |
| `af_nova` | Nova (US F) ★★★ |
| `af_sky` | Sky (US F) ★★½ |
| `af_alloy` | Alloy (US F) ★★½ |
| `af_jessica` | Jessica (US F) ★★½ |
| `af_river` | River (US F) ★★½ |
| `am_adam` | Adam (US M) ★★★★ |
| `am_echo` | Echo (US M) ★★★ |
| `am_eric` | Eric (US M) ★★★ |
| `am_fenrir` | Fenrir (US M) ★★★ |
| `am_liam` | Liam (US M) ★★★ |
| `am_michael` | Michael (US M) ★★★ |
| `am_onyx` | Onyx (US M) ★★★ |
| `am_puck` | Puck (US M) ★★★ |
| `am_santa` | Santa (US M) ★ |

### British English (prefix `b`)
| Voice ID | Description |
|----------|-------------|
| `bf_emma` | Emma (GB F) ★★★ |
| `bf_isabella` | Isabella (GB F) ★★★ |
| `bm_george` | George (GB M) ★★★ |
| `bm_lewis` | Lewis (GB M) ★★★ |

### Japanese (prefix `j`)
| Voice ID | Description |
|----------|-------------|
| `jf_alpha` | Alpha (JP F) ★★★ |
| `jf_gongitsune` | Gongitsune (JP F) ★★★ |
| `jf_nezuko` | Nezuko (JP F) ★★★ |
| `jf_tebukuro` | Tebukuro (JP F) ★★★ |
| `jm_kumo` | Kumo (JP M) ★★★ |

### Mandarin Chinese (prefix `z`)
| Voice ID | Description |
|----------|-------------|
| `zf_xiaobei` | Xiaobei (ZH F) ★★★ |
| `zm_yunjian` | Yunjian (ZH M) ★★★ |
| `zm_yunxi` | Yunxi (ZH M) ★★★ |
| `zm_yunxia` | Yunxia (ZH M) ★★★ |
| `zm_yunyang` | Yunyang (ZH M) ★★★ |

### Spanish (prefix `e`)
| Voice ID | Description |
|----------|-------------|
| `ef_dora` | Dora (ES F) ★★★ |
| `em_alex` | Alex (ES M) ★★★ |
| `em_santa` | Santa (ES M) ★★★ |

### French (prefix `f`)
| Voice ID | Description |
|----------|-------------|
| `ff_siwis` | Siwis (FR F) ★★★ |
| `fm_geraint` | Geraint (FR M) ★★★ |

### Hindi (prefix `h`)
| Voice ID | Description |
|----------|-------------|
| `hf_alpha` | Alpha (HI F) ★★★ |
| `hm_omega` | Omega (HI M) ★★★ |

### Italian (prefix `i`)
| Voice ID | Description |
|----------|-------------|
| `if_sara` | Sara (IT F) ★★★ |
| `im_nicola` | Nicola (IT M) ★★★ |

### Portuguese / Brazilian (prefix `p`)
| Voice ID | Description |
|----------|-------------|
| `pf_dora` | Dora (PT F) ★★★ |
| `pm_alex` | Alex (PT M) ★★★ |
| `pm_santa` | Santa (PT M) ★★★ |

---

## Soundboard

Playback slots with no local file are auto-filled with random royalty-free
sound effects (Mixkit). **Refresh** (`↻ New`) re-rolls them; **Cats** opens a
tick-list of the 31 categories with sound counts.

| Key | Meaning |
|-----|---------|
| `PLAYBACK_SLOTS` | number of slots, default 20 (slot 0 is the station ID) |
| `SOUNDBOARD_CATEGORIES` | comma-separated; `-name` excludes; blank = all |
| `SOUNDBOARD_MAX_SECONDS` | reject clips longer than this, default 15 (`0` = no cap) |

Both apply on the next Refresh with no restart. Ticking every category stores
*blank* rather than a 31-name list, so a pool that grows later is picked up
automatically. A filter that matches nothing falls back to the full pool — a
silent soundboard is worse than an ignored filter.

The length cap exists because the pool contains full-length music tracks (id
2474 is 72 s) that are useless as effects. Measured lengths are remembered in
`<playback-dir>/.soundboard_meta.json`, deliberately *outside* the `.cache`
directory that Refresh wipes, so a rejected clip is never re-fetched.

Files named `station_id*`, `loop.*` and the configured BGM beds are reserved and
never occupy a numbered slot.

**From an MCP client:** `soundboard_categories` shows counts and the current
selection, `soundboard_set_categories` persists a new filter, and
`soundboard_refresh` clears the cache and re-fills the slots.

## Background music and the repeating message

Three looping music beds, each with its own spoken message, mixed with
broadcast-style ducking. Both are **their own routing nodes** — `BGM` and
`Announcer` — so you wire them wherever you want in `/routing`.

Drop `bgm1.mp3`, `bgm2.mp3`, `bgm3.mp3` in the playback directory (or set
`BGM_FILES`), then press a BGM pad. **Msg** opens a dialog with one message and
one voice picker per bed.

| Key | Default | Meaning |
|-----|---------|---------|
| `BGM_FILES` | `bgm1.mp3, bgm2.mp3, bgm3.mp3` | the beds |
| `BGM_DUCK_DB` | `-12.0` | how far the bed drops under the voice — never to zero |
| `BGM_DUCK_ATTACK` | `0.25` | seconds to duck down |
| `BGM_DUCK_HOLD` | `0.4` | seconds held down across gaps, so it cannot pump |
| `BGM_DUCK_RELEASE` | `1.2` | seconds to come back up |
| `BGM_MAX_SECONDS` | `120` | stop the bed automatically (`0` = never) |
| `ANNOUNCER_INTERVAL` | `10` | seconds between repeats, measured from the *end* of speech |

Behaviour worth knowing:

- The announcer speaks whichever bed is playing, and goes quiet when BGM stops.
  A bed with no message plays music only — that is not an error.
- Only one bed loops at a time; starting another swaps rather than stacking.
- Messages persist in `~/.config/radio-gateway/announcer.json`. Text is saved
  even if TTS fails, and a message that fails to synthesise is left *disabled*
  rather than enabled-but-mute.
- A per-bed voice belonging to a different engine is dropped in favour of the
  engine default, so hot-swapping the engine degrades rather than breaks.

### Controlling beds and the announcer from an MCP client

`bgm_status` / `bgm_control` (slot 1-3, `start`/`stop`/`toggle`) drive the pads,
and `announcer_status` / `announcer_configure` edit the per-bed messages,
voices, interval, max length and master enable. Starting a bed plays through the
**System Sounds** routing, so if that node feeds a radio sink it transmits.

### Why the bed ducks itself

Ducking lives in `BGMSource`, not the bus. Only `ListenBus` implements ducking
at all, and a listen bus gates its Mumble sink on a **bus-wide VAD flag** that
steady music does not hold open — routing a bed through one makes the music
vanish between announcements while the voice punches through. Ducking in the
source works on any bus type. See `_duck_target` in `audio_sources.py`.

Beds are decoded with `normalize=False`, so a set levelled offline keeps its
loudness match. The peak-normalise that quiet soundboard clips rely on would
otherwise re-level each bed by its own crest factor.

## Mumble Chat Commands

### !speak \<text\>
Broadcast TTS on radio using the default voice.

```
!speak Net will start in 5 minutes
!speak Emergency traffic — all stations stand by
```

### !speak \<voice\> \<text\>
Override voice for this message only.

**Kokoro engine** — use a voice ID string:
```
!speak af_bella Good morning all stations
!speak bm_george This is the weekly net
!speak am_adam Attention all stations
```

**gTTS/Edge engine** — use a numeric accent:
```
!speak 2 Hello from British voice
!speak 3 G'day from Australian voice
```

Voice IDs: `af_heart af_bella am_adam bm_george bf_emma` — see full list above.

### !cw \<text\>
Send Morse code (CW) on radio.

```
!cw de w1aw
!cw qst qst de n1tpv
!cw 73
```

Config: `CW_WPM` (default 20 wpm), `CW_FREQUENCY` (default 600 Hz), `CW_VOLUME` (default 1.0).

### !play \<slot\>
Play an announcement file by slot number. `PLAYBACK_SLOTS` (default 20) sets how
many there are; slot 0 is always the station ID.

```
!play 0     # Station ID
!play 1     # Announcement slot 1
!play 12    # Slot 12 — multi-digit slots work here
```

Only slots 0–9 can be triggered from the **physical keyboard**, because a
keypress is a single character. Higher slots work from the web UI, `!play` and
MCP.

### !files
List loaded announcement files and their slot numbers.

### !stop
Stop playback and clear the queue.

### !mute / !unmute
Mute/unmute Mumble → Radio TX without stopping the gateway.

### !id
Shortcut for `!play 0` (station ID).

### !status
Print current gateway status to Mumble chat.

### !help
Print the command list.

---

## Audio Flow

```
Mumble User → !speak → Gateway → Kokoro ONNX synthesis → WAV (24kHz)
                                                          ↓
                                              Resample to 48kHz (resampy)
                                                          ↓
                                                  PTT pre-key delay
                                                          ↓
                                              Radio broadcast via bus routing
```

---

## Troubleshooting

**TTS keys radio but plays silence**
- Check the routing page (`/routing`) — the playback source must be wired to an active radio bus. If the solo bus is connected to a disabled plugin (e.g. D75 off), audio won't reach the radio.

**"Kokoro model files missing"**
- Run `scripts/install.sh` to download them, or manually:
  ```bash
  BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
  mkdir -p tools/models/kokoro
  wget -O tools/models/kokoro/kokoro-v1.0.onnx "$BASE/kokoro-v1.0.onnx"
  wget -O tools/models/kokoro/voices-v1.0.bin  "$BASE/voices-v1.0.bin"
  ```

**Fallback to edge/gtts**
- Set `TTS_ENGINE = edge` or `TTS_ENGINE = gtts` in `gateway_config.txt`.
- Requires internet for both.

**"TTS not available"**
- `ENABLE_TTS = true` must be set.
- Kokoro: check `tools/models/kokoro/` contains both model files.
- edge/gtts: check internet connectivity and pip packages.

**Voice dropdown shows wrong voices after engine change**
- The dropdown is populated from the live status API — it updates within ~2 seconds of the status poll cycle.

**TTS sounds slow / words cut off**
- Try reducing `TTS_SPEED` (e.g. `1.0`) or increasing `PTT_TTS_DELAY` (e.g. `0.75`).

---

## Security Notes

Text commands have no authentication by default — any Mumble user can trigger TTS.  
For public servers, add a user whitelist in `on_text_message()` in `text_commands.py`:

```python
AUTHORIZED = ['W1XYZ', 'K2ABC']
if not any(call in sender_name for call in AUTHORIZED):
    return
```
