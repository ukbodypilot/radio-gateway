# Loop Recorder

Continuous per-bus audio recording with visual waveform review, playback, and export.

![Loop Recorder Screenshot](loop-recorder-screenshot.png)

## Overview

The Loop Recorder continuously records audio from any bus as segmented MP3 files with real-time waveform visualization. Each bus can be recorded independently with its own retention window. Recordings are stored as 5-minute MP3 segments and automatically cleaned up when they exceed the retention period.

## Enabling Recording

1. Open the **Routing** page
2. Find the bus you want to record (e.g., Main, TH9800)
3. Click the **R** button (it turns red when active)
4. Recording starts immediately — audio is captured as it flows through the bus

Multiple buses can record simultaneously. Each gets its own storage directory and waveform track.

## Using the Recorder Page

Navigate to **Audio → Loop Recorder** in the nav menu.

### Waveform Display

Each recording bus is shown as a stacked waveform track:
- **Light blue fill** — peak envelope (loudest samples)
- **Solid blue fill** — RMS envelope (average energy)
- **Flat line** — silence (noise gate closed or no signal)
- **Red vertical line** — playback cursor position

### Navigation

| Action | How |
|--------|-----|
| **Zoom in** | Mouse scroll wheel, or `+` button |
| **Zoom out** | Mouse scroll wheel, or `-` button |
| **Fit all** | `Fit All` button (show entire recording) |
| **Pan** | Click and drag left/right |
| **Zoom to point** | Click a spot, then use `+`/`-` (zooms centered on click) |

### Playback

| Action | How |
|--------|-----|
| **Play from position** | Click on the waveform |
| **Play from start** | Click the play button (no prior click) |
| **Pause/resume** | Click the play button again |
| **Stop** | Click the stop button |
| **Seek** | Drag the seek slider |
| **Volume** | Drag the volume slider |

Each bus has independent playback — you can play multiple buses simultaneously.

### Selecting and Exporting

| Action | How |
|--------|-----|
| **Select range** | Right-click and drag on the waveform |
| **Export selection** | Selection auto-fills the start/end times, click Download |
| **Manual export** | Type start and end times (HH:MM:SS), choose MP3 or WAV, click Download |

## Retention

Each bus has a configurable retention window (how long recordings are kept). The default is 24 hours.

To change retention:
1. Open the Loop Recorder page
2. Use the **Retention** dropdown in the toolbar (1h, 2h, 4h, 8h, 12h, 24h, 48h, 3d, 7d)
3. The setting is saved per-bus and persists across restarts

Segments older than the retention window are automatically deleted.

## Storage

Recordings are stored in `recordings/loop/<bus_id>/`:

```
recordings/loop/
  main/
    20260407_0800.mp3    # 5-minute MP3 segment
    20260407_0800.wfm    # waveform data (peak + RMS per second)
    20260407_0805.mp3
    20260407_0805.wfm
    ...
  th9800/
    ...
```

### Disk usage

- MP3 at 128 kbps: ~4.7 MB per 5-minute segment
- 24 hours: ~1.35 GB per bus
- Waveform data: ~170 KB per 24 hours (negligible)

The dashboard shows per-bus disk usage and write rate in the Loop Recorder panel.

## Dashboard Panel

The dashboard shows a Loop Recorder status panel with:
- Per-bus recording indicator (red dot = active)
- Segment count and recording duration
- Disk usage (MB/GB) and write rate (MB/h)
- Retention setting
- Link to open the full recorder page

## Technical Details

- Audio is tapped after bus processing (gate/HPF/LPF/notch) in BusManager
- Waveform data is computed in real-time from raw PCM (not decoded from MP3)
- Active segment waveform is served from memory (no delay waiting for segment close)
- Playback supports HTTP Range requests for native browser seeking
- Export uses ffmpeg for cross-segment stitching and format conversion
- The `loop` flag is stored in `routing_config.json` per-bus processing config

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/loop/buses` | List buses with recording data |
| GET | `/loop/waveform?bus=&start=&end=` | Waveform envelope data (JSON) |
| GET | `/loop/play?bus=&start=&end=` | Stream MP3 for playback |
| POST | `/loop/export` | Export range as MP3/WAV download |
