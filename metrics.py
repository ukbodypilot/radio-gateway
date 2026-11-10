"""Prometheus metrics for radio-gateway.

Single registry shared across modules. Import the metric objects you
need; set/observe at the boundary you already have (bus tick, PTT
transition, transcription completion). Keep instrumentation off the
audio hot path — sample at coarse boundaries, not inside chunk
callbacks. Labels stay low-cardinality (bus/engine/endpoint), never
per-utterance.
"""

import json
import os
import re
import threading
import time

from log_util import log_exc
from prometheus_client import (
    ProcessCollector,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

REGISTRY = CollectorRegistry()

# rg_process_cpu_seconds_total, _resident_memory_bytes, _open_fds, ... for
# the gateway process itself (includes CPU of threads that have exited).
ProcessCollector(namespace='rg', registry=REGISTRY)

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

link_endpoint_ping_ms = Gauge(
    'rg_link_endpoint_ping_ms',
    'Gateway-link endpoint ping round-trip time in milliseconds',
    ['endpoint'],
    registry=REGISTRY,
)

# Host and ADS-B gauges below exist so the dashboard can chart an hour of
# history; they are sampled on scrape in _refresh_dashboard_history().
host_mem_pct = Gauge(
    'rg_host_mem_pct',
    'Host memory in use, percent ((MemTotal - MemAvailable) / MemTotal)',
    registry=REGISTRY,
)

host_net_rx_bps = Gauge(
    'rg_host_net_rx_bps',
    'Host network receive rate in bytes/second, all interfaces except lo',
    registry=REGISTRY,
)

host_net_tx_bps = Gauge(
    'rg_host_net_tx_bps',
    'Host network transmit rate in bytes/second, all interfaces except lo',
    registry=REGISTRY,
)

adsb_aircraft = Gauge(
    'rg_adsb_aircraft',
    'Aircraft heard by dump1090 in the last 60 seconds',
    registry=REGISTRY,
)

adsb_msg_rate = Gauge(
    'rg_adsb_msg_rate',
    'ADS-B messages per second decoded by dump1090',
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

denoise_late_total = Counter(
    'rg_denoise_late_total',
    'Chunks played DRY because the denoise worker missed its slot',
    ['bus', 'engine'],
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


# Paths are module attributes so tests can point them at fixtures.
_MEMINFO_PATH = '/proc/meminfo'
_NETDEV_PATH = '/proc/net/dev'
_ADSB_JSON_PATH = '/run/dump1090-fa/aircraft.json'
_prev_net = None       # (monotonic, rx_bytes, tx_bytes)
_prev_adsb = None      # (monotonic, messages)


def _refresh_dashboard_history():
    """Sample memory, network and ADS-B on scrape (reads three small files).

    Fed from the scrape, never from a web request handler: a gauge fed by
    /sysinfo or /adsbstatus would only move while a dashboard was open.
    Rates are averaged over the interval since the previous scrape; the
    first scrape only primes the counters. Each source fails independently
    and leaves its gauge at the previous value.
    """
    global _prev_net, _prev_adsb
    now = time.monotonic()
    try:
        mem = {}
        with open(_MEMINFO_PATH) as f:
            for line in f:
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable'):
                    mem[key] = int(rest.split()[0])
        if mem.get('MemTotal'):
            host_mem_pct.set(round(
                100.0 * (mem['MemTotal'] - mem.get('MemAvailable', 0)) / mem['MemTotal'], 1))
    except Exception:
        log_exc()
    try:
        rx = tx = 0
        with open(_NETDEV_PATH) as f:
            for line in f:
                if ':' not in line:
                    continue
                iface, rest = line.split(':', 1)
                if iface.strip() == 'lo':
                    continue
                parts = rest.split()
                rx += int(parts[0])
                tx += int(parts[8])
        if _prev_net and now > _prev_net[0]:
            dt = now - _prev_net[0]
            # A counter that went backwards (interface re-created) is a reset,
            # not negative traffic.
            host_net_rx_bps.set(round(max(0, rx - _prev_net[1]) / dt))
            host_net_tx_bps.set(round(max(0, tx - _prev_net[2]) / dt))
        _prev_net = (now, rx, tx)
    except Exception:
        log_exc()
    if os.path.exists(_ADSB_JSON_PATH):
        try:
            with open(_ADSB_JSON_PATH) as f:
                ac = json.load(f)
            adsb_aircraft.set(sum(1 for a in ac.get('aircraft', []) if a.get('seen', 999) < 60))
            msgs = ac.get('messages', 0)
            if _prev_adsb and now > _prev_adsb[0]:
                adsb_msg_rate.set(round(max(0, msgs - _prev_adsb[1]) / (now - _prev_adsb[0]), 1))
            _prev_adsb = (now, msgs)
        except Exception:
            log_exc()


# ── CPU by thread / helper process / host (read from /proc on scrape) ──

_CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

# External processes the gateway starts or depends on. Matched on
# /proc/<pid>/comm (truncated to 15 chars by the kernel).
HELPER_PROCS = ('rtl_airband', 'sdrplay_apiServ', 'ffmpeg', 'lame', 'parec',
                'direwolf', 'pw-cat', 'pw-record', 'arecord', 'aplay')

_IP_RE = re.compile(r'[-_ ]?\d{1,3}(?:\.\d{1,3}){3}.*$')
_TAIL_NUM_RE = re.compile(r'[-_ ]\d+$')
_ANON_RE = re.compile(r'^Thread-\d+ \((.+)\)$')


def thread_label(name):
    """Collapse a thread name to a low-cardinality label: per-connection
    and numbered threads ('LinkReader-192.168.2.141', 'Thread-53
    (audio_loop)', 'loop-enc-7') share one series."""
    m = _ANON_RE.match(name)
    if m:
        name = m.group(1)
    elif re.fullmatch(r'Thread-\d+', name):
        return 'anonymous'
    name = _TAIL_NUM_RE.sub('', _IP_RE.sub('', name))
    return name or 'anonymous'


def _stat_cpu_s(path):
    """utime+stime in seconds from a /proc stat file, or None."""
    try:
        with open(path) as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / _CLK_TCK
    except (OSError, IndexError, ValueError):
        return None


class _CpuCollector:
    """Thread, helper-process and host CPU, sampled at scrape time.

    rg_thread_cpu_seconds_total{thread} sums the LIVE threads that share a
    label; a thread exiting makes it step down, which rate() reads as a
    counter reset. Exited threads' CPU stays in rg_process_cpu_seconds_total.
    """

    def describe(self):
        return []

    def collect(self):
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
        threads = CounterMetricFamily(
            'rg_thread_cpu_seconds', 'CPU time of live gateway threads, by thread name',
            labels=['thread'])
        helpers = CounterMetricFamily(
            'rg_helper_cpu_seconds', 'CPU time of helper processes, by command',
            labels=['proc'])
        helper_n = GaugeMetricFamily(
            'rg_helper_processes', 'Running helper processes, by command',
            labels=['proc'])
        host = CounterMetricFamily(
            'rg_host_cpu_seconds', 'Host CPU time (all cores), by mode',
            labels=['mode'])
        try:
            names = {t.native_id: t.name for t in threading.enumerate()
                     if getattr(t, 'native_id', None)}
            per = {}
            for tid in os.listdir('/proc/self/task'):
                cpu = _stat_cpu_s(f'/proc/self/task/{tid}/stat')
                if cpu is None:
                    continue
                name = names.get(int(tid))
                if name is None:
                    try:
                        with open(f'/proc/self/task/{tid}/comm') as f:
                            name = f.read().strip()
                    except OSError:
                        continue
                lbl = thread_label(name)
                per[lbl] = per.get(lbl, 0.0) + cpu
            for lbl, cpu in per.items():
                threads.add_metric([lbl], cpu)
        except Exception:
            log_exc()
        try:
            tot = {p: 0.0 for p in HELPER_PROCS}
            cnt = {p: 0 for p in HELPER_PROCS}
            for pid in os.listdir('/proc'):
                if not pid.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid}/comm') as f:
                        comm = f.read().strip()
                except OSError:
                    continue
                if comm in tot:
                    cpu = _stat_cpu_s(f'/proc/{pid}/stat')
                    if cpu is not None:
                        tot[comm] += cpu
                        cnt[comm] += 1
            for p in HELPER_PROCS:
                if cnt[p]:
                    helpers.add_metric([p], tot[p])
                helper_n.add_metric([p], cnt[p])
        except Exception:
            log_exc()
        try:
            with open('/proc/stat') as f:
                vals = f.readline().split()[1:]
            modes = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')
            for mode, v in zip(modes, vals):
                host.add_metric([mode], int(v) / _CLK_TCK)
        except Exception:
            log_exc()
        return [threads, helpers, helper_n, host]


if os.path.isdir('/proc/self/task'):
    REGISTRY.register(_CpuCollector())


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    _refresh_host_telemetry()
    _refresh_dashboard_history()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
