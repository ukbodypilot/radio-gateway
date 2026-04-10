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

    def __init__(self, bus_id, segment_start, bus_dir, sample_rate=48000):
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
            ['lame', '-r', '-s', str(sample_rate), '--bitwidth', '16',
             '-m', 'm', '-b', '128', '--signed', '--little-endian',
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

        # Stop writer thread, drain remaining queue into pipe
        self._writer_running = False
        try:
            self._write_queue.put_nowait(None)  # sentinel
        except Exception:
            pass
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
                 sample_rate=48000):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'recordings', 'loop')
        self._base_dir = base_dir
        self._segment_seconds = segment_seconds
        self._default_retention_hours = retention_hours
        self._retention = {}     # bus_id → hours (per-bus override)
        self._sample_rate = sample_rate
        self._active = {}        # bus_id → LoopSegment
        self._lock = threading.Lock()
        self._cleanup_counter = 0
        os.makedirs(self._base_dir, exist_ok=True)

    def set_retention(self, bus_id, hours):
        """Set per-bus retention window in hours."""
        self._retention[bus_id] = hours

    def get_retention(self, bus_id):
        """Get retention hours for a bus."""
        return self._retention.get(bus_id, self._default_retention_hours)

    # -- Recording ----------------------------------------------------------

    def feed(self, bus_id, pcm_data):
        """Feed PCM audio for a bus.  Called from BusManager thread."""
        seg_start = self._current_segment_start()
        with self._lock:
            current = self._active.get(bus_id)

            # Rotate segment if boundary crossed or first feed
            if current is None or current.segment_start != seg_start:
                if current is not None:
                    current.close()
                bus_dir = os.path.join(self._base_dir, bus_id)
                os.makedirs(bus_dir, exist_ok=True)
                current = LoopSegment(bus_id, seg_start, bus_dir, self._sample_rate)
                self._active[bus_id] = current

            current.feed(pcm_data)

        # Periodic cleanup (every ~3 seconds at 50ms ticks)
        self._cleanup_counter += 1
        if self._cleanup_counter >= 60:
            self._cleanup_counter = 0
            self._cleanup(bus_id)

    def stop(self, bus_id=None):
        """Stop recording for one bus or all."""
        with self._lock:
            if bus_id:
                seg = self._active.pop(bus_id, None)
                if seg:
                    seg.close()
            else:
                for seg in self._active.values():
                    seg.close()
                self._active.clear()

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
            cmd = ['ffmpeg', '-y', '-i', seg['path'],
                   '-ss', str(offset), '-t', str(duration)]
            if fmt == 'wav':
                cmd += ['-acodec', 'pcm_s16le', '-ar', str(self._sample_rate)]
            else:
                cmd += ['-c', 'copy']
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
