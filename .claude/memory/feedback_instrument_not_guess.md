---
name: Instrument audio paths, never guess
description: User demands packet-level measurement before any fix attempts — guessing about audio issues wastes time
type: feedback
---

When diagnosing audio quality issues, NEVER guess about what might be wrong. Capture data first, analyze it, then fix what the data shows.

**Why:** User got very frustrated when Claude guessed about noise gates, CAT commands, and processing chains without proving the actual signal was present at the hardware level. Multiple wrong guesses wasted significant time.

**How to apply:**
- Before touching any code, prove audio is present at the hardware level (raw arecord capture, check RMS)
- Use the stream trace system (`tools/stream_trace.txt`) to see per-chunk data at every handoff
- Check queue depths, interval jitter, overflow flags — these tell the real story
- The AIOC always has a measurable noise floor (RMS ~115 at idle) — if RMS is 0, the capture path is broken, not the processing
- Every fix must be validated with a trace capture showing improvement in specific metrics
