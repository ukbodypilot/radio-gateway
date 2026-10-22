"""Prometheus metrics for radio-gateway.

Single registry shared across modules. Import the metric objects you
need; set/observe at the boundary you already have (bus tick, PTT
transition, transcription completion). Keep instrumentation off the
audio hot path — sample at coarse boundaries, not inside chunk
callbacks. Labels stay low-cardinality (bus/engine/endpoint), never
per-utterance.
"""

from log_util import log_exc
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

REGISTRY = CollectorRegistry()

bus_audio_level = Gauge(
    'rg_bus_audio_level',
    'Per-bus audio level (0.0-1.0, smoothed via audio_util.update_level)',
    ['bus'],
    registry=REGISTRY,
)

bus_ptt_active = Gauge(
    'rg_bus_ptt_active',
    'Per-bus PTT state (1=keyed, 0=idle)',
    ['bus'],
    registry=REGISTRY,
)

transcription_inflight = Gauge(
    'rg_transcription_inflight',
    'Utterances currently being transcribed, per engine',
    ['engine'],
    registry=REGISTRY,
)

transcription_seconds = Histogram(
    'rg_transcription_seconds',
    'Transcription wall time per utterance',
    ['engine'],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

transcription_dispatched_total = Counter(
    'rg_transcription_dispatched_total',
    'Utterances dispatched per engine',
    ['engine'],
    registry=REGISTRY,
)

stream_bytes_sent_total = Counter(
    'rg_stream_bytes_sent_total',
    'Bytes sent to each outbound stream (Broadcastify/Icecast)',
    ['stream'],
    registry=REGISTRY,
)

stream_reconnects_total = Counter(
    'rg_stream_reconnects_total',
    'Outbound stream reconnect events',
    ['stream'],
    registry=REGISTRY,
)

link_endpoint_up = Gauge(
    'rg_link_endpoint_up',
    'Gateway-link endpoint heartbeat state (1=up, 0=down)',
    ['endpoint'],
    registry=REGISTRY,
)

link_audio_underruns_total = Counter(
    'rg_link_audio_underruns_total',
    'Link audio jitter-buffer underrun events',
    ['endpoint'],
    registry=REGISTRY,
)

cpu_temp_c = Gauge(
    'rg_cpu_temp_c',
    'CPU temperature in Celsius',
    registry=REGISTRY,
)

fan_rpm = Gauge(
    'rg_fan_rpm',
    'Fan RPM (label identifies which fan)',
    ['fan'],
    registry=REGISTRY,
)

vad_speech_events_total = Counter(
    'rg_vad_speech_events_total',
    'Silero VAD speech-segment closures per bus',
    ['bus'],
    registry=REGISTRY,
)

denoise_apply_ms = Histogram(
    'rg_denoise_apply_ms',
    'Per-chunk neural-denoise apply time in milliseconds',
    ['bus', 'engine'],
    buckets=(0.5, 1, 2.5, 5, 10, 25, 50, 100),
    registry=REGISTRY,
)

gc_pause_ms = Histogram(
    'rg_gc_pause_ms',
    'Garbage-collection stop-the-world pause per generation, milliseconds',
    ['generation'],
    # CPython holds the GIL for the whole collection, so this is the pause
    # EVERY thread sees — including the audio tick. The bus tick runs its
    # collects at the end of the tick body so pauses are absorbed by the
    # ~45 ms sleep window; anything past 50 ms actually delays a tick.
    buckets=(1, 2.5, 5, 10, 25, 50, 100, 250, 500),
    registry=REGISTRY,
)

gc_pause_overrun_total = Counter(
    'rg_gc_pause_overrun_total',
    'GC pauses that exceeded the bus tick interval (i.e. delayed a tick)',
    registry=REGISTRY,
)


def _refresh_host_telemetry():
    """Sample CPU temp + fan RPM lazily on scrape. Cheap (~ms): reads
    /sys files only. Failure is non-fatal — gauges retain prior value.
    """
    try:
        from transcribe_engine import _host_cpu_temp_c, _host_fan_rpm
        t = _host_cpu_temp_c()
        if t is not None:
            cpu_temp_c.set(t)
        rpm = _host_fan_rpm()
        if rpm is not None:
            fan_rpm.labels(fan='primary').set(rpm)
    except Exception:
        log_exc()


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    _refresh_host_telemetry()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
