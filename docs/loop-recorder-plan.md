# Loop Recorder — Per-Bus Continuous Recording with Visual Waveform Review

## Context
The gateway records audio for automation playback but has no continuous recording or visual review capability. Users want to review what was on the air, scrub through hours of audio visually, and export clips — per bus, not just the listen bus.

## GUI Mockups

### Routing Page — New "R" Toggle Button
```
 +-----------------------------------------------+
 | Main (listen)                                  |
 | Sources: SDR, AIOC    Sinks: mumble, speaker   |
 | [G] [H] [L] [N]  |  [P] [M] [V] [R]          |
 |                       pcm mp3 vad REC  <-- NEW |
 +-----------------------------------------------+
```
"R" button toggles loop recording for that bus. Red when active.

### Recorder Page (`/recorder`) — Full View (24h zoomed out)
```
 +================================================================+
 | Loop Recorder                                                   |
 +================================================================+
 | Bus: [Main \/]  [TH9800]  [D75-BT]       Retention: 24h       |
 +----------------------------------------------------------------+
 |                                                                  |
 | 08:00    10:00    12:00    14:00    16:00    18:00    20:00      |
 | |         |         |         |         |         |         |    |
 |     .__   .     .  _|||_  . __||||||__    .   _|.   .           |
 | ---|  |---|-----|--|    |--|          |----|-||  |---|--------   |
 |     ''   '     '  '|  |'  ''||||||''    '   '|'   '           |
 | |         |         |         |         |         |         |    |
 | 08:00    10:00    12:00    14:00    16:00    18:00    20:00      |
 |                                                                  |
 |  Scroll to zoom  |  Drag to pan  |  Click to play               |
 +----------------------------------------------------------------+
 |                                                                  |
 | [|<] [>]  00:00:00 / 24:00:00  [==========|==========] Vol [=] |
 |                                                                  |
 +----------------------------------------------------------------+
 | Export:  Start [14:30:00] End [14:35:00]  [MP3] [WAV] [Download]|
 +----------------------------------------------------------------+
```

### Recorder Page — Zoomed In (5 minutes visible)
```
 +================================================================+
 | Loop Recorder                                                   |
 +================================================================+
 | Bus: [Main \/]  [TH9800]  [D75-BT]       Retention: 24h       |
 +----------------------------------------------------------------+
 | [-] [+] [Fit All]                   14:30:00 — 14:35:00        |
 +----------------------------------------------------------------+
 |                                                                  |
 | 14:30    14:31    14:32    14:33    14:34    14:35               |
 | |         |         |         |         |         |              |
 |           _||||||||||||||_              __|||__                  |
 |     _____|                |______  ___|       |___    ___       |
 |    |                              |                  |   |      |
 | ---|                              |                  |   |---   |
 |    |                              |                  |   |      |
 |     '''''|                |''''''  '''|       |'''    '''       |
 |           '||||||||||||||'              ''|||''                  |
 | |         |         |         |         |         |              |
 | 14:30    14:31    14:32    14:33    14:34    14:35               |
 |                                                                  |
 +----------------------------------------------------------------+
 |                                                                  |
 | [|<] [||]  14:31:23 / 14:35:00  [====|================] Vol [=]|
 |       ^playing                    ^cursor                       |
 +----------------------------------------------------------------+
 | Export:  Start [14:30:00] End [14:35:00]  [MP3] [WAV] [Download]|
 +----------------------------------------------------------------+
```

### Recorder Page — With Selection Range
```
 +----------------------------------------------------------------+
 |                                                                  |
 | 14:30    14:31    14:32    14:33    14:34    14:35               |
 | |         |         |         |         |         |              |
 |           _||||||||||||||_              __|||__                  |
 |     _____|################|______  ___|       |___    ___       |
 |    |      ################        |                  |   |      |
 | ---|      ################        |                  |   |---   |
 |    |      ####SELECTED####        |                  |   |      |
 |     '''''|################|''''''  '''|       |'''    '''       |
 |           '||||||||||||||'              ''|||''                  |
 | |         |         |         |         |         |              |
 |            ^drag start        ^drag end                         |
 +----------------------------------------------------------------+
 | Export:  Start [14:31:05] End [14:32:48]  [MP3] [WAV] [Download]|
 |          ^auto-filled from selection                            |
 +----------------------------------------------------------------+
```

## Architecture

### Storage
```
recordings/loop/
  main/                          # per-bus directories
    20260407_0800.mp3            # 5-min segments, wall-clock aligned
    20260407_0800.wfm            # waveform sidecar (600 bytes)
    20260407_0805.mp3
    20260407_0805.wfm
    ...
  th9800/
    ...
```

- **Segment duration:** 5 minutes (288 files/day/bus)
- **Format:** MP3 128kbps via `lame` subprocess (same as existing AudioRecorder)
- **Size:** ~4.7 MB per segment, ~1.35 GB per 24h per bus
- **Waveform:** 2 bytes/second (peak + RMS, 0-255), stored as `.wfm` binary sidecar
- **Retention:** Auto-delete oldest segments past configured window

### New Module: `loop_recorder.py`

```
LoopSegment          — one active MP3 segment + waveform accumulator
  .feed(pcm)         — pipe to lame, compute peak/RMS per second
  .close()           — finalize MP3, write .wfm sidecar

LoopRecorder         — manages per-bus recording lifecycle
  .feed(bus_id, pcm) — route to correct segment, rotate at boundaries
  .stop()            — close all active segments
  .get_waveform(bus_id, start, end) → {peaks, rms, start, end}
  .get_segments(bus_id, start, end) → [{path, start, end, size}]
  .get_buses()       → [{id, earliest, latest, segments, active}]
  .export_range(bus_id, start, end, fmt) → temp file path
  ._cleanup(bus_id)  — delete segments past retention
```

### BusManager Integration
In `_deliver_audio()`, after existing pcm/mp3 queue deposits:
```python
if mixed is not None and proc_cfg.get('loop', False):
    _lr = getattr(gw, 'loop_recorder', None)
    if _lr:
        _lr.feed(bus_id, mixed)
```

### API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/loop/buses` | List buses with loop data |
| GET | `/loop/waveform?bus=&start=&end=` | Waveform envelope JSON |
| GET | `/loop/play?bus=&start=&end=` | Stream stitched MP3 |
| POST | `/loop/export` | Export range as MP3/WAV download |

### Playback
- Single segment: serve MP3 directly (HTTP range requests)
- Cross-segment: `ffmpeg concat` demuxer streams stitched MP3
- Playback cursor syncs to `audio.currentTime` via `requestAnimationFrame`

### Frontend (`web_pages/recorder.html`)
- Canvas-based waveform: peak envelope (light) + RMS envelope (solid)
- Mouse wheel zoom (10s to 24h range), drag to pan
- Click to play, shift+drag to select range
- Selection auto-fills export time fields
- Bus tabs along top, one waveform per selected bus
- Export: start/end time inputs + MP3/WAV format + download button

## Implementation Phases

### Phase 1: Core recording (`loop_recorder.py`)
- LoopSegment + LoopRecorder classes
- feed(), segment rotation, waveform computation, cleanup

### Phase 2: Integration
- Add `loop` toggle to routing config + UI ("R" button)
- Hook into BusManager._deliver_audio()
- Initialize LoopRecorder in gateway_core.py

### Phase 3: API endpoints
- `/loop/buses`, `/loop/waveform`, `/loop/play`, `/loop/export`
- Route registration in web_server.py

### Phase 4: Frontend
- recorder.html with canvas waveform, zoom/pan, click-to-play
- Selection, export controls, bus tabs

## Files to Create/Modify
- **Create:** `loop_recorder.py`, `web_pages/recorder.html`
- **Modify:** `bus_manager.py` (feed hook), `gateway_core.py` (init), `web_server.py` (routes + toggle), `web_routes_get.py` (API), `web_routes_post.py` (export), `web_pages/routing.html` (R button)

## Verification
1. Enable "R" on a bus in routing UI
2. Wait 5+ minutes, verify segments appear in `recordings/loop/<bus_id>/`
3. Open `/recorder`, verify waveform renders
4. Zoom in/out, verify time axis updates
5. Click waveform, verify playback starts at clicked position
6. Drag to select range, verify export times auto-fill
7. Export MP3 and WAV, verify downloaded files play correctly
8. Wait past retention window, verify old segments are cleaned up
