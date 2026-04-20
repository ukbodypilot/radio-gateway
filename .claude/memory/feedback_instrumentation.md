---
name: Always instrument new code before debugging it
description: User strongly prefers explicit counters, timing logs, and telemetry over speculative back-and-forth debugging. When adding a new subsystem, add always-on health counters surfaced via MCP/UI as part of the initial implementation.
type: feedback
originSessionId: 0f1aa1d1-941b-4fe3-bef5-83a416b4760d
---
When building new subsystems in the radio-gateway project (workers, queues, ONNX pipelines, anything with timing or load), **add always-on telemetry as part of the first commit**, not as a follow-up. The user explicitly said (session of 2026-04-19):

> "is the new code instrumented? I don't think the overall audio is clean enough and I think we should make sure we have diagnosis in place to prevent an hours long claude lead guessing session lol"

**Why:** Past sessions have burned hours on speculation when a single counter would have revealed the bottleneck. The `transcription_status` MCP tool + transcribe-page feed-health row exist specifically because of this. They cost ~zero CPU and removed the entire "guess what's happening" phase from follow-up debugging.

**How to apply:**
- Counters on a locked stats dict: `enqueued`, `processed`, `dropped`, `peak_qd`, `proc_last_ms`, `proc_mean_ms`, `proc_max_ms`, `per_stream_ms`, `worker_errors`.
- Surface via the relevant MCP status tool + a status readout in the matching web page.
- Colour-code values (amber/red) at meaningful thresholds so a glance tells the story.
- Keep it monotonic since-start — compare two samples to get rates; don't try to compute windowed averages on the fly.
- **Check live state with the MCP tool before writing diagnostic code.** `gateway_logs` + `transcription_status` + `routing_levels` usually show the fault inside two tool calls.
