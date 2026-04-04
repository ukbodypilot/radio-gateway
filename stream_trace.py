"""Per-stream audio chunk tracer.

Records a lightweight row at every handoff point when tracing is active.
Toggled by the same 'i' key / web button as the main audio trace.
Dumped alongside the main trace to tools/stream_trace.txt.
"""

import collections
import time
import numpy as np


class StreamTrace:
    """Lock-free per-chunk recorder for audio stream handoffs."""

    def __init__(self, maxlen=60000):
        self._buf = collections.deque(maxlen=maxlen)
        self.active = False
        self._t0 = 0.0

    def start(self):
        self._t0 = time.monotonic()
        self._buf.clear()
        self.active = True

    def stop(self):
        self.active = False

    def record(self, stream, point, chunk, qd=-1, extra=''):
        """Record one handoff event.

        Args:
            stream: stream id (aioc_rx, aioc_tx, kv4p_tx, sdr1_rx, sdr2_rx)
            point:  handoff name (read, queue_put, queue_get, bus_tick, deliver, sink)
            chunk:  audio bytes (or None)
            qd:     queue depth at this point (-1 if N/A)
            extra:  freeform string (e.g. 'overflow', 'underrun', 'repeat')
        """
        if not self.active:
            return
        _t = time.monotonic()
        _len = len(chunk) if chunk else 0
        _rms = 0.0
        if chunk and _len >= 2:
            try:
                arr = np.frombuffer(chunk[:min(_len, 9600)], dtype=np.int16)
                _rms = float(np.sqrt(np.dot(arr.astype(np.float64), arr) / len(arr)))
            except Exception:
                pass
        self._buf.append((_t - self._t0, stream, point, _len, _rms, qd, extra))

    def dump(self, path):
        """Write stream trace to file."""
        import os
        import statistics

        rows = list(self._buf)
        if not rows:
            return

        os.makedirs(os.path.dirname(path), exist_ok=True)
        dur = rows[-1][0] - rows[0][0] if len(rows) > 1 else 0

        with open(path, 'w') as f:
            f.write(f"Stream Trace: {len(rows)} events, {dur:.1f}s\n")
            f.write(f"{'='*100}\n\n")

            # Per-stream summary
            streams = {}
            for r in rows:
                sid = r[1]
                if sid not in streams:
                    streams[sid] = {}
                pt = r[2]
                if pt not in streams[sid]:
                    streams[sid][pt] = []
                streams[sid][pt].append(r)

            for sid in sorted(streams.keys()):
                f.write(f"STREAM: {sid}\n")
                for pt in sorted(streams[sid].keys()):
                    evts = streams[sid][pt]
                    rms_vals = [e[4] for e in evts if e[4] > 0]
                    lens = [e[3] for e in evts]
                    qds = [e[5] for e in evts if e[5] >= 0]
                    extras = [e[6] for e in evts if e[6]]

                    f.write(f"  {pt}: {len(evts)} events")
                    if rms_vals:
                        f.write(f"  rms: mean={statistics.mean(rms_vals):.0f} "
                                f"min={min(rms_vals):.0f} max={max(rms_vals):.0f}")
                    zero_rms = sum(1 for e in evts if e[4] == 0 and e[3] > 0)
                    if zero_rms:
                        f.write(f"  SILENT={zero_rms}")
                    if lens:
                        unique_lens = set(lens)
                        if len(unique_lens) > 1:
                            f.write(f"  lens={sorted(unique_lens)}")
                    if qds:
                        f.write(f"  qd: mean={statistics.mean(qds):.1f} max={max(qds)}")
                    if extras:
                        extra_counts = {}
                        for x in extras:
                            extra_counts[x] = extra_counts.get(x, 0) + 1
                        f.write(f"  flags={dict(extra_counts)}")
                    f.write("\n")

                # Timing between consecutive events at each point
                for pt in sorted(streams[sid].keys()):
                    evts = streams[sid][pt]
                    if len(evts) > 1:
                        intervals = [(evts[i+1][0] - evts[i][0]) * 1000 for i in range(len(evts)-1)]
                        if intervals:
                            f.write(f"    {pt} intervals: mean={statistics.mean(intervals):.1f}ms "
                                    f"stdev={statistics.stdev(intervals):.1f}ms "
                                    f"min={min(intervals):.1f}ms max={max(intervals):.1f}ms")
                            over_80 = sum(1 for iv in intervals if iv > 80)
                            over_100 = sum(1 for iv in intervals if iv > 100)
                            if over_80:
                                f.write(f"  >80ms={over_80} >100ms={over_100}")
                            f.write("\n")

                # Repeated chunks (same RMS + same length in sequence)
                for pt in sorted(streams[sid].keys()):
                    evts = streams[sid][pt]
                    repeats = 0
                    for i in range(1, len(evts)):
                        if (evts[i][3] == evts[i-1][3] and evts[i][4] > 10
                                and abs(evts[i][4] - evts[i-1][4]) < 0.5):
                            repeats += 1
                    if repeats:
                        f.write(f"    {pt} *** {repeats} possible repeated chunks ***\n")

                f.write("\n")

            # Per-event detail
            f.write(f"{'='*100}\n")
            f.write("PER-EVENT DETAIL\n")
            f.write(f"{'='*100}\n")
            f.write(f"{'t(s)':>8} {'stream':<12} {'point':<14} {'len':>5} {'rms':>8} {'qd':>3} {'extra'}\n")
            f.write(f"{'-'*80}\n")
            for r in rows:
                f.write(f"{r[0]:8.3f} {r[1]:<12} {r[2]:<14} {r[3]:5} {r[4]:8.0f} {r[5]:3} {r[6]}\n")

        print(f"\n  Stream trace written to: {path}")
