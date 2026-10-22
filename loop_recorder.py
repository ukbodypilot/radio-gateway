"""Loop Recorder — per-bus continuous recording with waveform envelope.

Records audio as segmented MP3 files (5-minute chunks) with pre-computed
waveform data (peak + RMS per second).  Manages retention (auto-delete
old segments) and provides query/export APIs for the web UI.

Storage layout:
    recordings/loop/<bus_id>/YYYYMMDD_HHMM.mp3
    recordings/loop/<bus_id>/YYYYMMDD_HHMM.wfm   (binary: [peak,rms] per second)
"""

import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
import threading
import time
from datetime import datetime, timedelta

import numpy as np

from audio_util import pcm_rms


# ---------------------------------------------------------------------------
# LoopSegment — one active MP3 file + waveform accumulator
# ---------------------------------------------------------------------------

class LoopSegment:
    """Manages a single recording segment: lame encoder + waveform computation.

    Encoder writes are done in a background thread to avoid blocking the
    bus tick loop.  feed() only appends to an in-memory queue and computes
    waveform data — never touches the pipe.
    """

    def __init__(self, bus_id, segment_start, bus_dir, sample_rate=48000, kbps=48):
        self.bus_id = bus_id
        self.segment_start = segment_start  # datetime, wall-clock aligned
        self._sample_rate = sample_rate
        self._bytes_per_second = sample_rate * 2  # 16-bit mono

        # File paths
        ts = segment_start.strftime('%Y%m%d_%H%M')
        self.mp3_path = os.path.join(bus_dir, f'{ts}.mp3')
        self.wfm_path = os.path.join(bus_dir, f'{ts}.wfm')

        # Waveform accumulators
        self.wfm_peaks = []   # peak per second (0-255)
        self.wfm_rms = []     # RMS per second (0-255)
        self._sample_buf = bytearray()
        self._first_feed = True  # pad from segment start on first feed

        # Async encoder: queue + writer thread
        import queue as _q
        self._write_queue = _q.Queue(maxsize=500)
        self._encoder = subprocess.Popen(
            # --resample keeps every segment at the input rate: at low
            # bitrates lame would otherwise drop to 24 kHz, and the export
            # concat demuxer needs all segments to share one sample rate.
            ['lame', '-r', '-s', str(sample_rate), '--bitwidth', '16',
             '-m', 'm', '-b', str(int(kbps)), '--cbr',
             '--resample', f'{sample_rate / 1000:g}',
             '--signed', '--little-endian',
             '-', self.mp3_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._writer_running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True,
            name=f"loop-enc-{bus_id}")
        self._writer_thread.start()

    def _writer_loop(self):
        """Drain write queue into lame stdin pipe (background thread)."""
        import queue as _q
        while self._writer_running:
            try:
                data = self._write_queue.get(timeout=1.0)
            except _q.Empty:
                continue
            if data is None:  # sentinel: close
                break
            try:
                self._encoder.stdin.write(data)
            except (BrokenPipeError, OSError):
                break

    def feed(self, pcm_data):
        """Feed PCM data to encoder and accumulate waveform.

        The actual pipe write happens in _writer_loop; this method only
        enqueues data and computes waveform — guaranteed fast.
        """
        if not self._encoder or self._encoder.stdin.closed:
            return

        # On first feed: pad with silence from segment start to now
        # so waveform and MP3 are aligned to the wall-clock boundary.
        if self._first_feed:
            self._first_feed = False
            elapsed = time.time() - self.segment_start.timestamp()
            pad_seconds = max(0, int(elapsed))
            if pad_seconds > 0:
                silence = b'\x00' * self._bytes_per_second
                for _ in range(pad_seconds):
                    try:
                        self._write_queue.put_nowait(silence)
                    except Exception:
                        return
                    self.wfm_peaks.append(0)
                    self.wfm_rms.append(0)

        try:
            self._write_queue.put_nowait(pcm_data)
        except Exception:
            return  # queue full — drop frame rather than block

        # Accumulate for waveform computation
        self._sample_buf.extend(pcm_data)
        while len(self._sample_buf) >= self._bytes_per_second:
            chunk = bytes(self._sample_buf[:self._bytes_per_second])
            self._sample_buf = self._sample_buf[self._bytes_per_second:]
            self._compute_waveform_sample(chunk)

    def _compute_waveform_sample(self, pcm_one_second):
        """Compute peak and RMS for one second of PCM, append to arrays."""
        arr = np.frombuffer(pcm_one_second, dtype=np.int16)
        if len(arr) == 0:
            self.wfm_peaks.append(0)
            self.wfm_rms.append(0)
            return
        peak = float(np.max(np.abs(arr)))
        rms = pcm_rms(pcm_one_second)
        # Map to 0-255 (linear scale relative to int16 max)
        self.wfm_peaks.append(min(255, int(peak / 32768.0 * 255)))
        self.wfm_rms.append(min(255, int(rms / 32768.0 * 255)))

    def close(self):
        """Finalize MP3 and write waveform sidecar."""
        # Flush remaining samples as partial-second entry
        if self._sample_buf:
            self._compute_waveform_sample(bytes(self._sample_buf))
            self._sample_buf.clear()

        # Stop writer thread, drain remaining queue into pipe. Only the
        # sentinel stops it: clearing _writer_running first made the writer
        # exit on its next loop check and drop whatever was still queued,
        # truncating the segment's tail whenever lame lagged.
        try:
            self._write_queue.put(None, timeout=2.0)  # sentinel, after the backlog
        except Exception:
            self._writer_running = False  # queue wedged full: give up on the tail
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=5)

        # Close encoder
        if self._encoder and self._encoder.stdin and not self._encoder.stdin.closed:
            try:
                self._encoder.stdin.close()
            except Exception:
                pass
            try:
                self._encoder.wait(timeout=5)
            except Exception:
                self._encoder.kill()

        # Write waveform sidecar (binary: [peak0, rms0, peak1, rms1, ...])
        if self.wfm_peaks:
            try:
                with open(self.wfm_path, 'wb') as f:
                    for p, r in zip(self.wfm_peaks, self.wfm_rms):
                        f.write(struct.pack('BB', p, r))
            except Exception as e:
                print(f"  [LoopRec] Failed to write wfm {self.wfm_path}: {e}")

    @property
    def duration_seconds(self):
        """Number of complete seconds recorded so far."""
        return len(self.wfm_peaks)


# ---------------------------------------------------------------------------
# LoopRecorder — manages per-bus recording lifecycle
# ---------------------------------------------------------------------------

class LoopRecorder:
    """Continuous loop recorder with per-bus segmented MP3 + waveform."""

    def __init__(self, base_dir=None, segment_seconds=300, retention_hours=24,
                 sample_rate=48000, kbps=48):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'recordings', 'loop')
        self._base_dir = base_dir
        self._segment_seconds = segment_seconds
        self._default_retention_hours = retention_hours
        self._retention = {}     # bus_id → hours (per-bus override)
        self._sample_rate = sample_rate
        self._kbps = kbps
        self._active = {}        # bus_id → LoopSegment (published, feed-owned)
        self._lock = threading.Lock()
        os.makedirs(self._base_dir, exist_ok=True)

        # ── Housekeeping thread ────────────────────────────────────────────
        # feed() runs on the BusManager tick thread (SCHED_RR) and must never
        # touch the filesystem. Two things used to happen there:
        #   * segment rotation constructed a LoopSegment inline — os.makedirs
        #     plus subprocess.Popen('lame') — while holding the lock EVERY
        #     loop-recording bus needs, so one rotation stalled them all.
        #     (The matching close() was moved off-tick after an 800 ms stall;
        #     the open never was.)
        #   * retention cleanup ran os.listdir + os.unlink every ~3 s.
        # Now feed() only appends to an in-memory buffer and this thread does
        # the spawning and the deleting.
        self._pending = {}       # bus_id → list of pcm chunks awaiting a segment
        self._wanted = {}        # bus_id → segment_start datetime to create
        self._hk_evt = threading.Event()
        self._hk_running = True
        self._last_cleanup = 0.0
        # ~2 s of 50 ms chunks. Only ever holds audio for the few ms a lame
        # spawn takes; the bound stops a wedged spawn eating memory.
        self._pending_max = 40
        self._hk_thread = threading.Thread(
            target=self._housekeeping_loop, daemon=True, name='LoopRec-hk')
        self._hk_thread.start()

    def set_retention(self, bus_id, hours):
        """Set per-bus retention window in hours."""
        self._retention[bus_id] = hours

    def get_retention(self, bus_id):
        """Get retention hours for a bus."""
        return self._retention.get(bus_id, self._default_retention_hours)

    # -- Recording ----------------------------------------------------------

    def feed(self, bus_id, pcm_data):
        """Feed PCM audio for a bus.  Called from the BusManager tick thread.

        Never does I/O: on the steady-state path this is a dict lookup plus
        a queue append. When a segment boundary is crossed (or the bus is
        recording for the first time) the audio is parked in _pending and
        the housekeeping thread spawns the encoder, then drains the backlog
        into it — see _housekeeping_loop.
        """
        seg_start = self._current_segment_start()
        _need_hk = False
        with self._lock:
            current = self._active.get(bus_id)
            if current is not None and current.segment_start == seg_start:
                current.feed(pcm_data)
            else:
                # Boundary crossed / cold start — hand off to housekeeping.
                buf = self._pending.setdefault(bus_id, [])
                buf.append(pcm_data)
                if len(buf) > self._pending_max:
                    del buf[:len(buf) - self._pending_max]  # drop oldest
                self._wanted[bus_id] = seg_start
                _need_hk = True
        if _need_hk:
            # Self-heal: if the housekeeping thread ever died (or stop() was
            # called and recording resumed), nothing would ever create a
            # segment again and every bus would silently stop recording.
            _t = getattr(self, '_hk_thread', None)
            if _t is None or not _t.is_alive():
                self._hk_running = True
                self._hk_thread = threading.Thread(
                    target=self._housekeeping_loop, daemon=True,
                    name='LoopRec-hk')
                self._hk_thread.start()
            self._hk_evt.set()

    def _housekeeping_loop(self):
        """Off-tick worker: create segments, close old ones, prune retention."""
        while self._hk_running:
            self._hk_evt.wait(timeout=1.0)
            self._hk_evt.clear()
            try:
                self._service_pending()
            except Exception as e:
                print(f"  [LoopRec] housekeeping error: {e}")
            # Retention sweep every 60 s across every bus with data on disk.
            # (It used to piggyback on feed() every ~3 s and only ever ran for
            # whichever bus happened to trigger it, so a bus that stopped
            # feeding was never pruned.)
            _now = time.monotonic()
            if _now - self._last_cleanup >= 60.0:
                self._last_cleanup = _now
                try:
                    self._cleanup_all()
                except Exception as e:
                    print(f"  [LoopRec] cleanup error: {e}")

    def _service_pending(self):
        """Create any requested segments and drain buffered audio into them."""
        with self._lock:
            wanted = list(self._wanted.items())
        for bus_id, seg_start in wanted:
            bus_dir = os.path.join(self._base_dir, bus_id)
            try:
                # The expensive part — makedirs + Popen('lame') — runs here,
                # with no lock held, off the audio thread.
                os.makedirs(bus_dir, exist_ok=True)
                new_seg = LoopSegment(bus_id, seg_start, bus_dir,
                                      self._sample_rate, self._kbps)
            except Exception as e:
                print(f"  [LoopRec] failed to open segment for {bus_id}: {e}")
                with self._lock:
                    # Drop the request and its backlog; retry on the next feed
                    # rather than spinning on a broken encoder every second.
                    if self._wanted.get(bus_id) == seg_start:
                        del self._wanted[bus_id]
                    self._pending.pop(bus_id, None)
                continue
            with self._lock:
                # Skip if a newer boundary was requested while we were
                # spawning — the next pass creates that one instead.
                if self._wanted.get(bus_id) != seg_start:
                    _stale = new_seg
                else:
                    _stale = None
                    old_seg = self._active.get(bus_id)
                    for chunk in self._pending.pop(bus_id, []):
                        new_seg.feed(chunk)
                    # Publish before closing the old one so an in-flight feed()
                    # lands on the new segment, not a closing bytearray.
                    self._active[bus_id] = new_seg
                    del self._wanted[bus_id]
            if _stale is not None:
                _stale.close()
                continue
            if old_seg is not None:
                old_seg.close()   # outside the lock — joins its writer thread

    def _cleanup_all(self):
        """Run retention cleanup for every bus directory on disk."""
        if not os.path.isdir(self._base_dir):
            return
        for bus_id in os.listdir(self._base_dir):
            if os.path.isdir(os.path.join(self._base_dir, bus_id)):
                self._cleanup(bus_id)

    def shutdown(self):
        """Stop the housekeeping thread (called from stop())."""
        self._hk_running = False
        self._hk_evt.set()
        t = getattr(self, '_hk_thread', None)
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def stop(self, bus_id=None):
        """Stop recording for one bus, or all (also stops housekeeping)."""
        if bus_id is None:
            # Full stop: park housekeeping first so it can't publish a new
            # segment behind us, then flush whatever it hadn't got to yet.
            self.shutdown()
            try:
                self._service_pending()
            except Exception:
                pass
        with self._lock:
            if bus_id:
                self._wanted.pop(bus_id, None)
                self._pending.pop(bus_id, None)
                segs = [self._active.pop(bus_id, None)]
            else:
                self._wanted.clear()
                self._pending.clear()
                segs = list(self._active.values())
                self._active.clear()
        # close() joins the encoder writer thread — never under the lock.
        for seg in segs:
            if seg is not None:
                seg.close()

    # -- Queries ------------------------------------------------------------

    def get_buses(self, enabled_bus_ids=None):
        """List buses that have loop recording data or are enabled.

        Args:
            enabled_bus_ids: set of bus IDs with loop=True in routing config.
                Buses with no data yet but enabled will show as active/empty.

        Returns: [{'id': str, 'earliest': float, 'latest': float,
                   'segments': int, 'active': bool}]
        """
        result = {}
        now = time.time()
        # Buses with data on disk
        if os.path.isdir(self._base_dir):
            for bus_id in os.listdir(self._base_dir):
                bus_dir = os.path.join(self._base_dir, bus_id)
                if not os.path.isdir(bus_dir):
                    continue
                mp3s = sorted(f for f in os.listdir(bus_dir) if f.endswith('.mp3'))
                if mp3s:
                    earliest = self._filename_to_epoch(mp3s[0])
                    latest = self._filename_to_epoch(mp3s[-1]) + self._segment_seconds
                    result[bus_id] = {
                        'id': bus_id,
                        'earliest': earliest,
                        'latest': latest,
                        'segments': len(mp3s),
                        'active': bus_id in self._active,
                        'retention_hours': self.get_retention(bus_id),
                    }
        # Buses that are enabled but have no data yet
        if enabled_bus_ids:
            for bus_id in enabled_bus_ids:
                if bus_id not in result:
                    result[bus_id] = {
                        'id': bus_id,
                        'earliest': now,
                        'latest': now,
                        'segments': 0,
                        'active': True,
                        'retention_hours': self.get_retention(bus_id),
                    }
        # Add disk usage stats per bus
        for bus_id, entry in result.items():
            bus_dir = os.path.join(self._base_dir, bus_id)
            if os.path.isdir(bus_dir):
                total_bytes = sum(
                    os.path.getsize(os.path.join(bus_dir, f))
                    for f in os.listdir(bus_dir) if f.endswith('.mp3')
                )
                entry['disk_mb'] = round(total_bytes / (1024 * 1024), 1)
            else:
                entry['disk_mb'] = 0.0
        return sorted(result.values(), key=lambda b: b['id'])

    def get_waveform(self, bus_id, start_ts, end_ts):
        """Return waveform envelope data for a time range.

        Returns: {'start': float, 'end': float, 'resolution': 1.0,
                  'peaks': [int], 'rms': [int]}
        """
        bus_dir = os.path.join(self._base_dir, bus_id)
        if not os.path.isdir(bus_dir):
            return {'start': start_ts, 'end': end_ts, 'resolution': 1.0,
                    'peaks': [], 'rms': []}

        peaks = []
        rms = []

        # Collect waveform sources: disk files + active segment
        wfm_sources = list(self._find_wfm_files(bus_id, start_ts, end_ts))
        # Add active segment if it has live data and overlaps the range
        with self._lock:
            active = self._active.get(bus_id)
            if active and active.wfm_peaks:
                a_start = active.segment_start.timestamp()
                a_end = a_start + self._segment_seconds
                if a_end > start_ts and a_start < end_ts:
                    # Check if already covered by a disk wfm file
                    disk_starts = {s for s, _ in wfm_sources}
                    if a_start not in disk_starts:
                        wfm_sources.append((a_start, None))  # None = read from memory
                wfm_sources.sort(key=lambda x: x[0])

        for seg_start_epoch, wfm_path in wfm_sources:
            seg_data = self._read_wfm(bus_id, seg_start_epoch, wfm_path)
            if seg_data is None:
                continue

            seg_peaks, seg_rms = seg_data
            # Compute overlap: which seconds of this segment fall in [start_ts, end_ts]
            seg_end_epoch = seg_start_epoch + len(seg_peaks)
            overlap_start = max(start_ts, seg_start_epoch)
            overlap_end = min(end_ts, seg_end_epoch)
            if overlap_start >= overlap_end:
                continue

            # Pad with zeros if there's a gap before this segment
            expected_pos = start_ts + len(peaks)
            if seg_start_epoch > expected_pos:
                gap = int(seg_start_epoch - expected_pos)
                peaks.extend([0] * gap)
                rms.extend([0] * gap)

            # Slice the segment data
            idx_start = int(overlap_start - seg_start_epoch)
            idx_end = int(overlap_end - seg_start_epoch)
            peaks.extend(seg_peaks[idx_start:idx_end])
            rms.extend(seg_rms[idx_start:idx_end])

        # Pad trailing zeros
        expected_len = int(end_ts - start_ts)
        if len(peaks) < expected_len:
            pad = expected_len - len(peaks)
            peaks.extend([0] * pad)
            rms.extend([0] * pad)

        return {
            'start': start_ts,
            'end': end_ts,
            'resolution': 1.0,
            'peaks': peaks[:expected_len],
            'rms': rms[:expected_len],
        }

    def get_segments(self, bus_id, start_ts, end_ts):
        """Return segment file info for a time range.

        Returns: [{'path': str, 'start': float, 'end': float, 'size': int}]
        """
        bus_dir = os.path.join(self._base_dir, bus_id)
        if not os.path.isdir(bus_dir):
            return []
        result = []
        mp3s = sorted(f for f in os.listdir(bus_dir) if f.endswith('.mp3'))
        for fname in mp3s:
            seg_start = self._filename_to_epoch(fname)
            seg_end = seg_start + self._segment_seconds
            if seg_end <= start_ts or seg_start >= end_ts:
                continue
            fpath = os.path.join(bus_dir, fname)
            result.append({
                'path': fpath,
                'file': fname,
                'start': seg_start,
                'end': seg_end,
                'size': os.path.getsize(fpath),
            })
        return result

    # -- Export -------------------------------------------------------------

    def export_range(self, bus_id, start_ts, end_ts, fmt='mp3'):
        """Export a time range to a temporary file.  Returns file path.

        Uses ffmpeg to concatenate and trim segments.
        """
        segments = self.get_segments(bus_id, start_ts, end_ts)
        if not segments:
            return None

        suffix = '.mp3' if fmt == 'mp3' else '.wav'
        outfile = tempfile.NamedTemporaryFile(
            suffix=suffix, prefix=f'loop_{bus_id}_', delete=False)
        outfile.close()

        if len(segments) == 1:
            # Single segment: trim with ffmpeg
            seg = segments[0]
            offset = max(0, start_ts - seg['start'])
            duration = min(end_ts, seg['end']) - max(start_ts, seg['start'])
            cmd = ['ffmpeg', '-y', '-ss', str(offset), '-i', seg['path'],
                   '-t', str(duration)]
            if fmt == 'wav':
                cmd += ['-acodec', 'pcm_s16le', '-ar', str(self._sample_rate)]
            else:
                cmd += ['-acodec', 'libmp3lame', '-b:a', '128k']
            cmd.append(outfile.name)
        else:
            # Multi-segment: concat demuxer
            concat_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix='concat_', delete=False)
            for seg in segments:
                concat_file.write(f"file '{seg['path']}'\n")
            concat_file.close()

            offset = max(0, start_ts - segments[0]['start'])
            duration = end_ts - start_ts
            cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                   '-i', concat_file.name,
                   '-ss', str(offset), '-t', str(duration)]
            if fmt == 'wav':
                cmd += ['-acodec', 'pcm_s16le', '-ar', str(self._sample_rate)]
            else:
                cmd += ['-c', 'copy']
            cmd.append(outfile.name)

        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        except Exception as e:
            print(f"  [LoopRec] Export failed: {e}")
            return None
        finally:
            if len(segments) > 1:
                try:
                    os.unlink(concat_file.name)
                except Exception:
                    pass

        if os.path.getsize(outfile.name) == 0:
            os.unlink(outfile.name)
            return None
        return outfile.name

    # -- Retention cleanup --------------------------------------------------

    def _cleanup(self, bus_id):
        """Delete segments older than retention window."""
        bus_dir = os.path.join(self._base_dir, bus_id)
        if not os.path.isdir(bus_dir):
            return
        hours = self._retention.get(bus_id, self._default_retention_hours)
        cutoff = time.time() - (hours * 3600)
        for fname in os.listdir(bus_dir):
            if not fname.endswith(('.mp3', '.wfm')):
                continue
            base = fname.rsplit('.', 1)[0]  # YYYYMMDD_HHMM
            try:
                seg_epoch = self._filename_to_epoch(base + '.mp3')
            except Exception:
                continue
            if seg_epoch < cutoff:
                try:
                    os.unlink(os.path.join(bus_dir, fname))
                except Exception:
                    pass

    # -- Bulk operations ----------------------------------------------------

    def delete_all(self):
        """Delete all loop recordings for all buses. Returns count of files deleted."""
        count = 0
        with self._lock:
            # Close all active segments first
            for seg in self._active.values():
                seg.close()
            self._active.clear()
        if os.path.isdir(self._base_dir):
            for bus_id in os.listdir(self._base_dir):
                bus_dir = os.path.join(self._base_dir, bus_id)
                if not os.path.isdir(bus_dir):
                    continue
                for fname in os.listdir(bus_dir):
                    if fname.endswith(('.mp3', '.wfm')):
                        try:
                            os.unlink(os.path.join(bus_dir, fname))
                            count += 1
                        except Exception:
                            pass
                # Remove empty bus dir
                try:
                    os.rmdir(bus_dir)
                except OSError:
                    pass
        print(f"  [LoopRec] Deleted all recordings ({count} files)")
        return count

    def zip_all(self):
        """Create a zip of all loop recordings. Returns temp file path or None."""
        if not os.path.isdir(self._base_dir):
            return None
        outfile = tempfile.NamedTemporaryFile(
            suffix='.zip', prefix='loop_all_', delete=False)
        outfile.close()
        count = 0
        try:
            with zipfile.ZipFile(outfile.name, 'w', zipfile.ZIP_STORED) as zf:
                for bus_id in sorted(os.listdir(self._base_dir)):
                    bus_dir = os.path.join(self._base_dir, bus_id)
                    if not os.path.isdir(bus_dir):
                        continue
                    for fname in sorted(os.listdir(bus_dir)):
                        if fname.endswith('.mp3'):
                            fpath = os.path.join(bus_dir, fname)
                            zf.write(fpath, os.path.join(bus_id, fname))
                            count += 1
        except Exception as e:
            print(f"  [LoopRec] Zip failed: {e}")
            try:
                os.unlink(outfile.name)
            except Exception:
                pass
            return None
        if count == 0:
            os.unlink(outfile.name)
            return None
        print(f"  [LoopRec] Zipped {count} files → {outfile.name}")
        return outfile.name

    def archive_all(self):
        """Archive all recordings to recordings/loop_archive/<timestamp>/. Returns path or None."""
        if not os.path.isdir(self._base_dir):
            return None
        archive_base = os.path.join(os.path.dirname(self._base_dir), 'loop_archive')
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_dir = os.path.join(archive_base, ts)
        os.makedirs(archive_dir, exist_ok=True)
        count = 0
        # Close active segments so files are complete
        with self._lock:
            for seg in self._active.values():
                seg.close()
            self._active.clear()
        for bus_id in sorted(os.listdir(self._base_dir)):
            bus_dir = os.path.join(self._base_dir, bus_id)
            if not os.path.isdir(bus_dir):
                continue
            mp3s = sorted(f for f in os.listdir(bus_dir) if f.endswith('.mp3'))
            if not mp3s:
                continue
            dest_dir = os.path.join(archive_dir, bus_id)
            os.makedirs(dest_dir, exist_ok=True)
            for fname in mp3s:
                src = os.path.join(bus_dir, fname)
                shutil.move(src, os.path.join(dest_dir, fname))
                count += 1
                # Also move wfm sidecar if present
                wfm = fname.replace('.mp3', '.wfm')
                wfm_path = os.path.join(bus_dir, wfm)
                if os.path.exists(wfm_path):
                    shutil.move(wfm_path, os.path.join(dest_dir, wfm))
        if count == 0:
            try:
                os.rmdir(archive_dir)
            except OSError:
                pass
            return None
        print(f"  [LoopRec] Archived {count} files → {archive_dir}")
        return archive_dir

    # -- Helpers ------------------------------------------------------------

    def _current_segment_start(self):
        """Return the current wall-clock-aligned segment start as datetime."""
        now = datetime.now()
        minute = (now.minute // (self._segment_seconds // 60)) * (self._segment_seconds // 60)
        return now.replace(minute=minute, second=0, microsecond=0)

    def find_segment(self, bus_id, ts):
        """Resolve (bus_id, epoch_ts) → segment file metadata, or None.

        Returns dict {'path', 'wfm_path', 'start_epoch', 'duration', 'offset_s'}
        where `offset_s` is the seek position into the file that corresponds to
        `ts`. Falls back to a directory scan when the boundary-floored filename
        doesn't exist (e.g. recording was rotated out, or boundary math drifted
        past a clock change), so the caller doesn't need to know the layout.
        """
        bus_dir = os.path.join(self._base_dir, bus_id)
        if not os.path.isdir(bus_dir):
            return None
        # Floor to the segment boundary; this is the canonical filename.
        boundary = int(ts // self._segment_seconds) * self._segment_seconds
        candidate = datetime.fromtimestamp(boundary).strftime('%Y%m%d_%H%M')
        mp3 = os.path.join(bus_dir, f'{candidate}.mp3')
        if os.path.isfile(mp3):
            wfm = os.path.join(bus_dir, f'{candidate}.wfm')
            return {
                'path': mp3,
                'wfm_path': wfm if os.path.isfile(wfm) else None,
                'start_epoch': float(boundary),
                'duration': float(self._segment_seconds),
                'offset_s': max(0.0, ts - boundary),
            }
        # Fallback: scan the dir for a segment whose [start, start+dur) brackets ts.
        for fname in sorted(os.listdir(bus_dir)):
            if not fname.endswith('.mp3'):
                continue
            try:
                seg_start = self._filename_to_epoch(fname)
            except ValueError:
                continue
            if seg_start <= ts < seg_start + self._segment_seconds:
                wfm = os.path.join(bus_dir, fname[:-4] + '.wfm')
                return {
                    'path': os.path.join(bus_dir, fname),
                    'wfm_path': wfm if os.path.isfile(wfm) else None,
                    'start_epoch': seg_start,
                    'duration': float(self._segment_seconds),
                    'offset_s': max(0.0, ts - seg_start),
                }
        return None

    def _filename_to_epoch(self, fname):
        """Parse YYYYMMDD_HHMM.mp3 → epoch float."""
        base = fname.replace('.mp3', '').replace('.wfm', '')
        dt = datetime.strptime(base, '%Y%m%d_%H%M')
        return dt.timestamp()

    def _find_wfm_files(self, bus_id, start_ts, end_ts):
        """Yield (seg_start_epoch, wfm_path) for segments overlapping range."""
        bus_dir = os.path.join(self._base_dir, bus_id)
        if not os.path.isdir(bus_dir):
            return
        for fname in sorted(os.listdir(bus_dir)):
            if not fname.endswith('.wfm'):
                continue
            seg_start = self._filename_to_epoch(fname)
            seg_end = seg_start + self._segment_seconds
            if seg_end <= start_ts or seg_start >= end_ts:
                continue
            yield seg_start, os.path.join(bus_dir, fname)

    def _read_wfm(self, bus_id, seg_start_epoch, wfm_path):
        """Read waveform data.  Returns (peaks_list, rms_list) or None.

        wfm_path=None means read from active segment in memory.
        """
        # Check active segment for live data
        with self._lock:
            active = self._active.get(bus_id)
            if active and active.segment_start.timestamp() == seg_start_epoch:
                return list(active.wfm_peaks), list(active.wfm_rms)

        # Read from disk
        if wfm_path is None or not os.path.isfile(wfm_path):
            return None
        try:
            with open(wfm_path, 'rb') as f:
                data = f.read()
            peaks = []
            rms = []
            for i in range(0, len(data), 2):
                if i + 1 < len(data):
                    peaks.append(data[i])
                    rms.append(data[i + 1])
            return peaks, rms
        except Exception:
            return None
