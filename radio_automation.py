"""Automation Engine for Radio Gateway.

Provides scheduled, autonomous radio operations driven by a text scheme file.
Architecture: Text scheme → AutomationEngine → Actions (tune, record, announce)

Future: English mission → AI Planner → scheme → AutomationEngine → Actions
"""

import csv
import math
import os
import random
import re
import struct
import subprocess
import threading
import time
from datetime import datetime, timedelta


# ============================================================================
#  RepeaterDatabase — loads RepeaterBook CSV, provides query methods
# ============================================================================

class RepeaterDatabase:
    """Query interface for RepeaterBook CSV exports.

    Each repeater is a dict with: frequency, input_freq, callsign, city, state,
    pl_tone, offset, use, status, lat, lon, distance_mi.
    """

    def __init__(self, csv_path, home_lat=None, home_lon=None):
        self._repeaters = []
        self._home_lat = home_lat
        self._home_lon = home_lon
        if csv_path and os.path.exists(csv_path):
            self._load(csv_path)

    def _load(self, csv_path):
        """Load repeaters from RepeaterBook CSV export.

        Supports multiple RepeaterBook export formats:
          - Full export: Frequency, Input Freq, Callsign, Nearest City, State, PL, Lat, Long, ...
          - Proximity export: Output Freq, Input Freq, Call, Location, County, State, Uplink Tone, ...
        """
        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        # Frequency: "Frequency" (full) or "Output Freq" (proximity)
                        freq = float(row.get('Frequency') or row.get('Output Freq') or 0)
                        if freq < 1:
                            continue
                        lat = float(row.get('Lat') or row.get('Latitude') or 0)
                        lon = float(row.get('Long') or row.get('Longitude') or 0)
                        inp = float(row.get('Input Freq', 0) or 0)
                        # PL tone: "PL" or "Tone" (full), "Uplink Tone" (proximity)
                        pl = row.get('PL', '') or row.get('Tone', '') or row.get('Uplink Tone', '') or ''
                        if pl.upper() == 'CSQ':
                            pl = ''  # carrier squelch = no tone
                        offset = row.get('Offset', '') or ''
                        # Callsign: "Callsign" (full) or "Call" (proximity)
                        callsign = (row.get('Callsign') or row.get('Call') or '').strip()
                        # City: "Nearest City" (full) or "Location" (proximity)
                        city = (row.get('Nearest City') or row.get('Location') or '').strip()
                        state = (row.get('State') or '').strip()
                        modes = (row.get('Modes') or row.get('Use') or '').strip()
                        status = (row.get('Operational Status') or row.get('Status') or '').strip()
                        dist = self._distance(lat, lon) if self._home_lat and lat != 0 else None
                        self._repeaters.append({
                            'frequency': freq,
                            'input_freq': inp if inp > 0 else None,
                            'callsign': callsign,
                            'city': city,
                            'state': state,
                            'pl_tone': pl.strip(),
                            'offset': offset.strip(),
                            'use': modes,
                            'status': status,
                            'lat': lat,
                            'lon': lon,
                            'distance_mi': dist,
                        })
                    except (ValueError, TypeError):
                        continue
            print(f"[Automation] Loaded {len(self._repeaters)} repeaters from {os.path.basename(csv_path)}")
        except Exception as e:
            print(f"[Automation] Failed to load repeater CSV: {e}")

    def _distance(self, lat, lon):
        """Haversine distance in miles from home coordinates."""
        if not self._home_lat or not self._home_lon:
            return None
        R = 3958.8  # Earth radius in miles
        lat1, lon1 = math.radians(self._home_lat), math.radians(self._home_lon)
        lat2, lon2 = math.radians(lat), math.radians(lon)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return R * 2 * math.asin(math.sqrt(a))

    def query(self, band=None, max_distance=None, count=10):
        """Return repeaters matching filters, sorted by distance."""
        results = self._filter(band, max_distance)
        results.sort(key=lambda r: r.get('distance_mi') or 99999)
        return results[:count]

    def by_callsign(self, callsign):
        """Find a repeater by callsign (case-insensitive)."""
        cs = callsign.upper()
        for r in self._repeaters:
            if r['callsign'].upper() == cs:
                return r
        return None

    def by_frequency(self, freq, tolerance=0.005):
        """Find repeaters near a frequency (MHz)."""
        return [r for r in self._repeaters if abs(r['frequency'] - freq) <= tolerance]

    def random_selection(self, count=5, band=None, max_distance=None):
        """Return random repeaters matching filters."""
        pool = self._filter(band, max_distance)
        if len(pool) <= count:
            return pool
        return random.sample(pool, count)

    def to_summary(self, repeaters=None):
        """Compact text summary for AI consumption."""
        reps = repeaters or self._repeaters[:20]
        lines = []
        for r in reps:
            dist = f" ({r['distance_mi']:.1f}mi)" if r.get('distance_mi') else ""
            tone = f" PL {r['pl_tone']}" if r.get('pl_tone') else ""
            lines.append(f"{r['frequency']:.4f} {r['callsign']} {r['city']}, {r['state']}{tone}{dist}")
        return '\n'.join(lines)

    def _filter(self, band=None, max_distance=None):
        """Filter repeaters by band and distance."""
        results = list(self._repeaters)
        if band:
            lo, hi = self._band_range(band)
            results = [r for r in results if lo <= r['frequency'] <= hi]
        if max_distance and self._home_lat:
            results = [r for r in results if r.get('distance_mi') is not None and r['distance_mi'] <= max_distance]
        return results

    @staticmethod
    def _band_range(band):
        """Convert band name to frequency range (MHz)."""
        band = str(band).lower().strip()
        ranges = {
            '2m': (144.0, 148.0),
            '70cm': (420.0, 450.0),
            '1.25m': (222.0, 225.0),
            '6m': (50.0, 54.0),
            '10m': (28.0, 29.7),
        }
        return ranges.get(band, (0, 9999))

    @property
    def count(self):
        return len(self._repeaters)


# ============================================================================
#  RadioController — abstraction over all radios
# ============================================================================

class RadioController:
    """Unified interface to SDR, TH-9800, and D75 radios.

    The automation engine calls this; never touches radio clients directly.
    """

    def __init__(self, gateway):
        self._gw = gateway

    def tune(self, radio, frequency, mode='FM', pl_tone=None):
        """Tune a radio to a frequency. Returns status dict."""
        radio = radio.lower()
        try:
            if radio == 'sdr':
                return self._tune_sdr(frequency, mode)
            elif radio == 'th9800':
                return self._tune_th9800(frequency, pl_tone)
            else:
                return {'ok': False, 'error': f'Unknown radio: {radio}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_state(self, radio):
        """Get current state of a radio."""
        radio = radio.lower()
        try:
            if radio == 'sdr':
                mgr = getattr(self._gw, 'web_config_server', None)
                mgr = getattr(mgr, 'sdr_manager', None) if mgr else None
                if mgr:
                    return {'ok': True, 'frequency': mgr.frequency, 'modulation': mgr.modulation}
                return {'ok': False, 'error': 'SDR not available'}
            elif radio == 'th9800':
                cat = getattr(self._gw, 'cat_client', None)
                if cat:
                    state = cat.get_radio_state()
                    return {'ok': True, **state}
                return {'ok': False, 'error': 'TH-9800 not connected'}
            else:
                return {'ok': False, 'error': f'Unknown radio: {radio}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def check_signal(self, radio):
        """Check audio level / signal presence on a radio."""
        radio = radio.lower()
        try:
            if radio == 'sdr':
                # Read SDR audio level from gateway
                level = getattr(self._gw, 'sdr_audio_level', 0)
                return {'ok': True, 'level': level, 'has_signal': level > 50}
            elif radio == 'th9800':
                level = getattr(self._gw, 'rx_audio_level', 0)
                return {'ok': True, 'level': level, 'has_signal': level > 50}
            else:
                return {'ok': False, 'error': f'Unknown radio: {radio}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def available_radios(self):
        """Return list of available radio names."""
        radios = []
        mgr = getattr(self._gw, 'web_config_server', None)
        if mgr and getattr(mgr, 'sdr_manager', None):
            radios.append('sdr')
        if getattr(self._gw, 'cat_client', None):
            radios.append('th9800')
        return radios

    def _tune_sdr(self, frequency, mode):
        """Tune SDR via RTLAirbandManager."""
        mgr = getattr(self._gw, 'web_config_server', None)
        mgr = getattr(mgr, 'sdr_manager', None) if mgr else None
        if not mgr:
            return {'ok': False, 'error': 'SDR not available'}
        mod_map = {'FM': 'nfm', 'NFM': 'nfm', 'AM': 'am'}
        mod = mod_map.get(mode.upper(), 'nfm') if mode else 'nfm'
        result = mgr.apply_settings(frequency=frequency, modulation=mod)
        return result

    def _tune_th9800(self, frequency, pl_tone):
        """Tune TH-9800 — channel-based radio, find closest channel or log warning."""
        cat = getattr(self._gw, 'cat_client', None)
        if not cat:
            return {'ok': False, 'error': 'TH-9800 not connected'}
        # TH-9800 is channel-based; direct frequency tuning is not supported via CAT.
        # Log the request for manual channel setup.
        print(f"[Automation] TH-9800: frequency {frequency} MHz requested (channel-based radio)")
        return {'ok': True, 'note': 'TH-9800 is channel-based; set up channels manually'}

    # D75 tuning is now handled by the link endpoint


# ============================================================================
#  AudioRecorder — records RX audio to MP3 via lame (streaming)
# ============================================================================

class AudioRecorder:
    """Records PCM audio to MP3 files by piping to lame in real time."""

    def __init__(self, gateway, recordings_dir='recordings'):
        self._gw = gateway
        self._dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), recordings_dir)
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.Lock()
        self._recording = False
        self._start_time = 0
        self._duration = 0
        self._label = ''
        self._file_path = None
        self._encoder = None  # lame subprocess

    def start(self, duration_seconds, label='', radio='', frequency=None):
        """Start recording. Launches lame encoder subprocess."""
        with self._lock:
            if self._recording:
                return self._file_path
            ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
            parts = []
            if radio:
                parts.append(radio.upper())
            if frequency is not None:
                parts.append(f"{float(frequency):.4f}MHz")
            parts.append(ts)
            if label:
                parts.append(re.sub(r'[^\w\-.]', '_', label))
            fname = '_'.join(parts) + '.mp3'
            self._file_path = os.path.join(self._dir, fname)
            # Launch lame: read raw PCM from stdin, write MP3 to file
            # Input: 48kHz, 16-bit signed LE, mono
            try:
                self._encoder = subprocess.Popen(
                    ['lame', '-r', '-s', '48', '--bitwidth', '16', '-m', 'm',
                     '-b', '128', '--signed', '--little-endian',
                     '-', self._file_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                print(f"[Automation] Failed to start lame encoder: {e}")
                self._encoder = None
                return None
            self._duration = duration_seconds
            self._start_time = time.monotonic()
            self._recording = True
            self._label = label
            print(f"[Automation] Recording started: {fname} ({duration_seconds}s)")
            return self._file_path

    def feed(self, pcm_data):
        """Called from main audio loop with PCM data (48kHz 16-bit mono)."""
        if not self._recording:
            return
        with self._lock:
            if not self._recording:
                return
            # Pipe PCM to lame
            if self._encoder and self._encoder.stdin:
                try:
                    self._encoder.stdin.write(pcm_data)
                except (BrokenPipeError, OSError):
                    pass
            # Check duration
            if self._duration > 0 and (time.monotonic() - self._start_time) >= self._duration:
                self._finish()

    def stop(self):
        """Stop recording and finalize MP3. Returns file path or None."""
        with self._lock:
            if not self._recording:
                return None
            return self._finish()

    def is_recording(self):
        return self._recording

    def _finish(self):
        """Close lame encoder and finalize MP3. Must be called with lock held."""
        self._recording = False
        path = self._file_path
        if self._encoder:
            try:
                self._encoder.stdin.close()
                self._encoder.wait(timeout=10)
            except Exception:
                try:
                    self._encoder.kill()
                except Exception:
                    pass
            self._encoder = None
        if path and os.path.exists(path):
            size = os.path.getsize(path)
            duration = time.monotonic() - self._start_time
            print(f"[Automation] Recording saved: {os.path.basename(path)} ({duration:.1f}s, {size // 1024}KB)")
            return path
        else:
            print("[Automation] Recording stopped: no file produced")
            return None


# ============================================================================
#  SchemeParser — parses the scheme text file
# ============================================================================

class TaskDef:
    """Parsed task definition from a scheme file line."""
    __slots__ = ('name', 'schedule', 'radio', 'action', 'options')

    def __init__(self, name, schedule, radio, action, options):
        self.name = name
        self.schedule = schedule
        self.radio = radio
        self.action = action
        self.options = options

    def __repr__(self):
        return f"TaskDef({self.name}, {self.schedule}, {self.radio}, {self.action})"


class ScheduleConfig:
    """Parsed schedule specification."""

    def __init__(self):
        self.type = 'interval'  # 'interval' or 'daily'
        self.interval_min = 0   # seconds (for 'interval')
        self.interval_max = 0   # seconds (for random range)
        self.at_hour = 0        # (for 'daily')
        self.at_minute = 0      # (for 'daily')
        self.jitter = 0         # seconds of random jitter

    def next_interval(self):
        """Return seconds until next run for interval schedules."""
        if self.interval_min == self.interval_max:
            return self.interval_min
        return random.uniform(self.interval_min, self.interval_max)


class SchemeParser:
    """Parses automation scheme text files."""

    def parse(self, file_path):
        """Parse a scheme file into a list of TaskDefs."""
        tasks = []
        try:
            with open(file_path, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) < 4:
                        print(f"[Automation] Scheme line {line_num}: need at least 4 fields, got {len(parts)}")
                        continue
                    name = parts[0]
                    schedule = self.parse_schedule(parts[1])
                    radio = parts[2].lower()
                    action = parts[3].lower()
                    options = self.parse_options(parts[4]) if len(parts) > 4 else {}
                    tasks.append(TaskDef(name, schedule, radio, action, options))
        except Exception as e:
            print(f"[Automation] Failed to parse scheme file: {e}")
        return tasks

    def parse_schedule(self, schedule_str):
        """Parse schedule string into ScheduleConfig."""
        sc = ScheduleConfig()
        s = schedule_str.strip().lower()

        # "at HH:MM" with optional "jitter=Xm"
        at_match = re.match(r'at\s+(\d{1,2}):(\d{2})', s)
        if at_match:
            sc.type = 'daily'
            sc.at_hour = int(at_match.group(1))
            sc.at_minute = int(at_match.group(2))
            jitter_match = re.search(r'jitter\s*=\s*(\d+)\s*m', s)
            if jitter_match:
                sc.jitter = int(jitter_match.group(1)) * 60
            return sc

        # "every X-Yh" or "every X-Ym" (random range)
        range_match = re.match(r'every\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(h|m|s)', s)
        if range_match:
            sc.type = 'interval'
            lo = float(range_match.group(1))
            hi = float(range_match.group(2))
            unit = range_match.group(3)
            mult = {'h': 3600, 'm': 60, 's': 1}[unit]
            sc.interval_min = lo * mult
            sc.interval_max = hi * mult
            return sc

        # "every Xh" or "every Xm" or "every Xs" (fixed interval)
        fixed_match = re.match(r'every\s+(\d+(?:\.\d+)?)\s*(h|m|s)', s)
        if fixed_match:
            sc.type = 'interval'
            val = float(fixed_match.group(1))
            unit = fixed_match.group(2)
            mult = {'h': 3600, 'm': 60, 's': 1}[unit]
            sc.interval_min = val * mult
            sc.interval_max = val * mult
            return sc

        print(f"[Automation] Unknown schedule format: '{schedule_str}'")
        # Default: every 1 hour
        sc.interval_min = 3600
        sc.interval_max = 3600
        return sc

    def parse_options(self, options_str):
        """Parse key=value pairs into a dict."""
        opts = {}
        if not options_str:
            return opts
        # Match key=value where value may be quoted
        for match in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|(\S+))', options_str):
            key = match.group(1)
            val = match.group(2) if match.group(2) is not None else match.group(3)
            # Type coercion
            if val.lower() in ('true', 'yes', 'on'):
                opts[key] = True
            elif val.lower() in ('false', 'no', 'off'):
                opts[key] = False
            else:
                try:
                    opts[key] = int(val)
                except ValueError:
                    try:
                        opts[key] = float(val)
                    except ValueError:
                        opts[key] = val
        return opts


# ============================================================================
#  AutomationEngine — scheduler + executor
# ============================================================================

class AutomationEngine:
    """Scheduler and executor for automation tasks.

    Reads a scheme file, schedules tasks, executes them one at a time.
    """

    def __init__(self, gateway):
        self._gw = gateway
        self._config = gateway.config
        self._parser = SchemeParser()
        self._tasks = []          # list of TaskDef
        self._task_state = {}     # task_name -> {next_run, last_run, ...}
        self._history = []        # completed task log
        self._history_max = 100
        self._lock = threading.Lock()
        self._radio_lock = threading.Lock()  # one task at a time
        self._running = False
        self._thread = None
        self._current_task = None  # currently executing task name

        # Components
        self.radio = RadioController(gateway)
        self.recorder = AudioRecorder(
            gateway,
            getattr(self._config, 'AUTOMATION_RECORDINGS_DIR', 'recordings')
        )
        self.repeater_db = None

        # Load repeater database if configured
        csv_path = getattr(self._config, 'AUTOMATION_REPEATER_FILE', '')
        if csv_path and os.path.exists(csv_path):
            self.repeater_db = RepeaterDatabase(
                csv_path,
                home_lat=getattr(self._config, 'AUTOMATION_REPEATER_LAT', 0) or None,
                home_lon=getattr(self._config, 'AUTOMATION_REPEATER_LON', 0) or None,
            )

        # Load scheme
        self._scheme_file = getattr(self._config, 'AUTOMATION_SCHEME_FILE', 'automation_scheme.txt')
        if not os.path.isabs(self._scheme_file):
            self._scheme_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), self._scheme_file)
        self.reload_scheme()

    def start(self):
        """Launch scheduler thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, name="Automation", daemon=True)
        self._thread.start()
        task_count = len(self._tasks)
        radios = self.radio.available_radios()
        reps = self.repeater_db.count if self.repeater_db else 0
        print(f"[Automation] Engine started: {task_count} tasks, radios={radios}, repeaters={reps}")

    def stop(self):
        """Stop scheduler thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        # Stop any active recording
        if self.recorder.is_recording():
            self.recorder.stop()
        print("[Automation] Engine stopped")

    def reload_scheme(self):
        """Re-read scheme file and reset task schedules."""
        with self._lock:
            self._tasks = self._parser.parse(self._scheme_file) if os.path.exists(self._scheme_file) else []
            # Initialize/reset schedule state
            now = time.monotonic()
            new_state = {}
            for t in self._tasks:
                old = self._task_state.get(t.name, {})
                if t.schedule.type == 'interval':
                    # If task existed before, keep its next_run; otherwise schedule first run
                    # start_now option: fire immediately on first load
                    if t.options.get('start_now', False) and not old:
                        next_run = now
                    else:
                        next_run = old.get('next_run', now + t.schedule.next_interval())
                    new_state[t.name] = {
                        'next_run': next_run,
                        'last_run': old.get('last_run', 0),
                        'run_count': old.get('run_count', 0),
                    }
                else:  # daily
                    new_state[t.name] = {
                        'last_run_date': old.get('last_run_date', ''),
                        'last_run': old.get('last_run', 0),
                        'run_count': old.get('run_count', 0),
                    }
            self._task_state = new_state
        if self._tasks:
            print(f"[Automation] Scheme loaded: {len(self._tasks)} tasks from {os.path.basename(self._scheme_file)}")
            for t in self._tasks:
                print(f"  {t.name}: {t.action} on {t.radio} ({t.schedule.type})")

    def get_status(self):
        """Return status dict for web UI."""
        with self._lock:
            tasks = []
            now = time.monotonic()
            for t in self._tasks:
                state = self._task_state.get(t.name, {})
                info = {
                    'name': t.name,
                    'action': t.action,
                    'radio': t.radio,
                    'schedule_type': t.schedule.type,
                }
                if t.schedule.type == 'interval':
                    nr = state.get('next_run', 0)
                    info['next_run_secs'] = max(0, int(nr - now))
                else:
                    info['at'] = f"{t.schedule.at_hour:02d}:{t.schedule.at_minute:02d}"
                    info['last_run_date'] = state.get('last_run_date', '')
                lr = state.get('last_run', 0)
                info['last_run_ago'] = int(now - lr) if lr > 0 else None
                tasks.append(info)
            return {
                'enabled': True,
                'running': self._running,
                'current_task': self._current_task,
                'recording': self.recorder.is_recording(),
                'tasks': tasks,
                'history_count': len(self._history),
                'repeater_count': self.repeater_db.count if self.repeater_db else 0,
                'radios': self.radio.available_radios(),
            }

    def get_history(self):
        """Return completed task history (most recent first)."""
        with self._lock:
            return list(reversed(self._history))

    def trigger(self, task_name):
        """Manually trigger a task by name. Returns True if found and queued."""
        for t in self._tasks:
            if t.name == task_name:
                threading.Thread(
                    target=self._execute_task, args=(t,),
                    name=f"Auto-{t.name}", daemon=True
                ).start()
                return True
        return False

    # ── Scheduler ──

    def _scheduler_loop(self):
        """Check every 30s for tasks that are due."""
        while self._running:
            try:
                self._check_tasks()
            except Exception as e:
                print(f"[Automation] Scheduler error: {e}")
            # Sleep in small increments for responsive shutdown
            for _ in range(60):
                if not self._running:
                    return
                time.sleep(0.5)

    def _check_tasks(self):
        """Check all tasks and run any that are due."""
        if not self._in_time_window():
            return

        now_mono = time.monotonic()
        now_dt = datetime.now()

        for t in self._tasks:
            state = self._task_state.get(t.name)
            if not state:
                continue

            # Skip if max_runs reached
            max_runs = t.options.get('max_runs')
            if max_runs is not None and state.get('run_count', 0) >= int(max_runs):
                continue

            due = False
            if t.schedule.type == 'interval':
                if now_mono >= state.get('next_run', 0):
                    due = True
            elif t.schedule.type == 'daily':
                today = now_dt.strftime('%Y-%m-%d')
                if state.get('last_run_date') != today:
                    # Check if we're past the target time (with jitter)
                    target = now_dt.replace(hour=t.schedule.at_hour, minute=t.schedule.at_minute, second=0)
                    jitter = random.uniform(-t.schedule.jitter, t.schedule.jitter) if t.schedule.jitter else 0
                    target += timedelta(seconds=jitter)
                    if now_dt >= target:
                        due = True

            if due:
                # Update schedule state before executing (prevent re-trigger)
                with self._lock:
                    if t.schedule.type == 'interval':
                        state['next_run'] = now_mono + t.schedule.next_interval()
                    elif t.schedule.type == 'daily':
                        state['last_run_date'] = now_dt.strftime('%Y-%m-%d')

                # Execute in a thread (but acquire radio lock for serialization)
                threading.Thread(
                    target=self._execute_task, args=(t,),
                    name=f"Auto-{t.name}", daemon=True
                ).start()

    def _in_time_window(self):
        """Check if current time is within the automation time window."""
        start_str = getattr(self._config, 'AUTOMATION_START_TIME', '06:00')
        end_str = getattr(self._config, 'AUTOMATION_END_TIME', '23:00')
        try:
            now = datetime.now()
            start_h, start_m = map(int, start_str.split(':'))
            end_h, end_m = map(int, end_str.split(':'))
            start_mins = start_h * 60 + start_m
            end_mins = end_h * 60 + end_m
            now_mins = now.hour * 60 + now.minute
            if start_mins <= end_mins:
                return start_mins <= now_mins <= end_mins
            else:
                # Overnight window (e.g., 22:00 - 06:00)
                return now_mins >= start_mins or now_mins <= end_mins
        except Exception:
            return True

    # ── Task Execution ──

    def _execute_task(self, task):
        """Execute a single task with radio lock."""
        max_duration = getattr(self._config, 'AUTOMATION_MAX_TASK_DURATION', 600)

        # Acquire radio lock (timeout = max task duration)
        if not self._radio_lock.acquire(timeout=max_duration):
            print(f"[Automation] {task.name}: could not acquire radio lock, skipping")
            return

        try:
            # Wait for radio to be free (same pattern as SmartAnnounce)
            if not self._wait_for_radio_idle(task.name, timeout=120):
                return

            self._current_task = task.name
            start_time = time.monotonic()
            print(f"[Automation] ── {task.name}: starting ({task.action} on {task.radio}) ──")

            result = {}
            try:
                if task.action == 'tune_and_listen':
                    result = self._action_tune_and_listen(task)
                elif task.action == 'scan_repeaters':
                    result = self._action_scan_repeaters(task)
                elif task.action == 'announce':
                    result = self._action_announce(task)
                elif task.action == 'tune_only':
                    result = self._action_tune_only(task)
                else:
                    result = {'error': f'Unknown action: {task.action}'}
                    print(f"[Automation] {task.name}: unknown action '{task.action}'")
            except Exception as e:
                result = {'error': str(e)}
                print(f"[Automation] {task.name}: execution error: {e}")

            elapsed = time.monotonic() - start_time
            print(f"[Automation] ── {task.name}: completed in {elapsed:.1f}s ──")

            # Log to history
            with self._lock:
                self._task_state[task.name]['last_run'] = time.monotonic()
                self._task_state[task.name]['run_count'] = self._task_state[task.name].get('run_count', 0) + 1
                self._history.append({
                    'task': task.name,
                    'action': task.action,
                    'radio': task.radio,
                    'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'elapsed': round(elapsed, 1),
                    'result': result,
                })
                if len(self._history) > self._history_max:
                    self._history = self._history[-self._history_max:]

        finally:
            self._current_task = None
            self._radio_lock.release()

    def _wait_for_radio_idle(self, task_name, timeout=120):
        """Wait for radio to be free (no VAD, no playback)."""
        for attempt in range(int(timeout / 5)):
            vad_busy = getattr(self._gw, 'vad_active', False)
            pb_busy = (self._gw.playback_source and self._gw.playback_source.current_file) if hasattr(self._gw, 'playback_source') else False
            if not vad_busy and not pb_busy:
                return True
            if attempt == 0:
                print(f"[Automation] {task_name}: waiting for radio to be free...")
            time.sleep(5)
        print(f"[Automation] {task_name}: radio busy too long, skipping")
        return False

    def _parse_duration(self, val):
        """Parse duration string like '30s', '5m', '2h' to seconds."""
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip().lower()
        m = re.match(r'^(\d+(?:\.\d+)?)\s*(s|m|h)?$', s)
        if m:
            num = float(m.group(1))
            unit = m.group(2) or 's'
            return num * {'s': 1, 'm': 60, 'h': 3600}[unit]
        return float(s)  # fallback: assume seconds

    # ── Actions ──

    def _action_tune_and_listen(self, task):
        """Tune to a frequency, listen for a duration, optionally record."""
        opts = task.options
        freq = opts.get('freq')
        if not freq:
            print(f"[Automation] {task.name}: no frequency specified")
            return {'error': 'no frequency'}

        freq = float(freq)
        mode = opts.get('mode', 'FM')
        pl = opts.get('pl')
        listen = self._parse_duration(opts.get('listen', 60))
        record = opts.get('record', False)

        # Tune
        result = self.radio.tune(task.radio, freq, mode=mode, pl_tone=pl)
        print(f"[Automation] {task.name}: tuned to {freq} MHz — {result}")
        time.sleep(2)  # settle time

        # Record if requested
        rec_path = None
        if record:
            rec_path = self.recorder.start(listen, label=task.name, radio=task.radio, frequency=freq)

        # Listen
        time.sleep(listen)

        # Stop recording if we started one
        if record and self.recorder.is_recording():
            rec_path = self.recorder.stop()

        # Check signal
        sig = self.radio.check_signal(task.radio)
        return {
            'frequency': freq,
            'signal': sig,
            'recording': rec_path,
            'listened': listen,
        }

    def _action_scan_repeaters(self, task):
        """Scan random repeaters, check for signal, optionally record active ones."""
        if not self.repeater_db:
            print(f"[Automation] {task.name}: no repeater database loaded")
            return {'error': 'no repeater database'}

        opts = task.options
        band = opts.get('band')
        count = int(opts.get('count', 5))
        listen = self._parse_duration(opts.get('listen', 30))
        record = opts.get('record', False)
        max_dist = opts.get('max_distance')
        if max_dist:
            max_dist = float(max_dist)
        announce_summary = opts.get('announce_summary', False)

        repeaters = self.repeater_db.random_selection(count=count, band=band, max_distance=max_dist)
        if not repeaters:
            print(f"[Automation] {task.name}: no repeaters found for band={band}")
            return {'error': 'no repeaters found'}

        print(f"[Automation] {task.name}: scanning {len(repeaters)} repeaters (band={band}, listen={listen}s)")
        results = []

        for rpt in repeaters:
            freq = rpt['frequency']
            callsign = rpt.get('callsign', '?')
            city = rpt.get('city', '')
            pl = rpt.get('pl_tone')

            # Tune
            tune_result = self.radio.tune(task.radio, freq, mode='FM', pl_tone=pl)
            time.sleep(2)  # settle

            # Listen and check signal
            has_signal = False
            rec_path = None

            # Sample signal over listen period
            check_interval = min(listen, 5)
            checks = max(1, int(listen / check_interval))
            for i in range(checks):
                time.sleep(check_interval)
                sig = self.radio.check_signal(task.radio)
                if sig.get('has_signal'):
                    has_signal = True
                    break

            status_str = 'ACTIVE' if has_signal else 'quiet'

            # Record active repeaters if requested
            if has_signal and record:
                remaining = max(5, listen - (check_interval * (i + 1)))
                rec_path = self.recorder.start(remaining, label=f"scan_{callsign}", radio=task.radio, frequency=freq)
                time.sleep(remaining)
                if self.recorder.is_recording():
                    rec_path = self.recorder.stop()

            dist = f" ({rpt['distance_mi']:.1f}mi)" if rpt.get('distance_mi') else ""
            print(f"[Automation]   {freq:.4f} {callsign} {city}{dist} — {status_str}")

            results.append({
                'frequency': freq,
                'callsign': callsign,
                'city': city,
                'has_signal': has_signal,
                'recording': rec_path,
            })

        # Announce summary if requested
        if announce_summary:
            active = [r for r in results if r['has_signal']]
            if active:
                summary = f"Repeater scan complete. Found {len(active)} active out of {len(results)} scanned. "
                for r in active:
                    summary += f"{r['callsign']} on {r['frequency']:.3f}. "
            else:
                summary = f"Repeater scan complete. Scanned {len(results)} repeaters, no activity detected."
            try:
                self._gw.speak_text(summary, voice=opts.get('voice'))
                # Wait for TTS playback to finish
                for _ in range(300):
                    if not (self._gw.playback_source and self._gw.playback_source.current_file):
                        break
                    time.sleep(0.1)
            except Exception as e:
                print(f"[Automation] {task.name}: announce failed: {e}")

        return {
            'scanned': len(results),
            'active': sum(1 for r in results if r['has_signal']),
            'results': results,
        }

    def _action_announce(self, task):
        """Play a TTS announcement on the current frequency."""
        opts = task.options
        text = opts.get('text', '')
        if not text:
            print(f"[Automation] {task.name}: no text specified")
            return {'error': 'no text'}

        # Variable substitution
        callsign = getattr(self._config, 'MUMBLE_USERNAME', 'RadioGateway')
        text = text.replace('{callsign}', callsign)
        text = text.replace('{time}', datetime.now().strftime('%H:%M'))
        text = text.replace('{date}', datetime.now().strftime('%B %d'))

        voice = opts.get('voice')
        print(f"[Automation] {task.name}: announcing ({len(text.split())} words)")

        try:
            self._gw.speak_text(text, voice=voice)
            # Wait for playback to finish
            for _ in range(300):
                if not (self._gw.playback_source and self._gw.playback_source.current_file):
                    break
                time.sleep(0.1)
            return {'text': text, 'voice': voice}
        except Exception as e:
            print(f"[Automation] {task.name}: TTS failed: {e}")
            return {'error': str(e)}

    def _action_tune_only(self, task):
        """Just tune to a frequency, no listening or recording."""
        opts = task.options
        freq = opts.get('freq')
        if not freq:
            print(f"[Automation] {task.name}: no frequency specified")
            return {'error': 'no frequency'}

        freq = float(freq)
        mode = opts.get('mode', 'FM')
        pl = opts.get('pl')

        result = self.radio.tune(task.radio, freq, mode=mode, pl_tone=pl)
        print(f"[Automation] {task.name}: tuned to {freq} MHz — {result}")
        return {'frequency': freq, 'result': result}
