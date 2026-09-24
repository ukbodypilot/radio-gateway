"""Fleet Manager Engine — scheduled Claude-driven fleet health checks."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, date


_BASE = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE   = os.path.join(_BASE, 'manager_state.json')
_REPORTS_FILE = os.path.join(_BASE, 'manager_reports.jsonl')
_HOURLY_FILE  = os.path.join(_BASE, 'hourly.md')
_DAILY_FILE   = os.path.join(_BASE, 'daily.md')
_CONST_FILE   = os.path.join(_BASE, 'SYSTEM_MANIFEST.md')

_DEFAULT_STATE = {
    'enabled':              False,
    'daily_time':           '06:00',
    'check_interval_hours': 1,      # 1, 2, 4, 8, or 12
    'last_check':           None,   # "YYYY-MM-DD-HH" (slot-aligned)
    'last_daily':           None,   # "YYYY-MM-DD"
    'unread_alerts':        False,
    'running':              False,
    'last_run_type':        None,
    'last_run_ts':          None,
}

_MAX_WAIT_SECS   = 600   # 10 min timeout waiting for Claude's report
_LOOP_INTERVAL   = 30    # seconds between schedule checks


class ManagerEngine:
    def __init__(self, config, gateway=None):
        self.config  = config
        self.gateway = gateway
        self._state  = dict(_DEFAULT_STATE)
        self._lock   = threading.Lock()
        self._stop   = threading.Event()
        self._thread = None
        self._load_state()

    # ── State persistence ─────────────────────────────────────────────────

    def _load_state(self):
        try:
            with open(_STATE_FILE) as f:
                loaded = json.load(f)
            for k, v in _DEFAULT_STATE.items():
                self._state[k] = loaded.get(k, v)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  [Manager] State load error: {e}")

    def _save_state(self):
        try:
            from atomic_json import save_json
            save_json(_STATE_FILE, self._state)
        except Exception as e:
            print(f"  [Manager] State save error: {e}")

    # ── Public API ────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name='manager-engine')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def get_status(self):
        with self._lock:
            st = dict(self._state)
        return st

    def get_reports(self, limit=50):
        reports = []
        try:
            with open(_REPORTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            reports.append(json.loads(line))
                        except Exception:
                            pass
        except FileNotFoundError:
            pass
        return reports[-limit:]

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._state['enabled'] = bool(enabled)
            if enabled:
                now = datetime.now()
                self._state['last_check'] = self._check_slot_key(now)
                self._state['last_daily'] = now.strftime('%Y-%m-%d')
            self._save_state()

    def set_daily_time(self, t: str):
        with self._lock:
            self._state['daily_time'] = t
            self._save_state()

    def set_check_interval(self, hours: int):
        valid = (1, 2, 4, 6, 8, 12)
        hours = hours if hours in valid else 1
        with self._lock:
            self._state['check_interval_hours'] = hours
            # Recompute last_check under the new interval so the loop doesn't
            # immediately fire due to a slot-key mismatch after the change.
            self._state['last_check'] = self._check_slot_key(datetime.now())
            self._save_state()

    def acknowledge(self):
        with self._lock:
            self._state['unread_alerts'] = False
            self._save_state()

    def run_now(self, task_type: str):
        """Trigger an immediate run (non-blocking — runs in background thread)."""
        def _run_and_mark():
            self._run_task(task_type)
            now = datetime.now()
            with self._lock:
                if task_type == 'daily':
                    self._state['last_daily'] = now.strftime('%Y-%m-%d')
                    self._state['last_check'] = self._check_slot_key(now)
                else:
                    self._state['last_check'] = self._check_slot_key(now)
                self._save_state()
        threading.Thread(target=_run_and_mark, daemon=True).start()

    def read_doc(self, name: str):
        path = self._doc_path(name)
        if not path or not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def save_doc(self, name: str, content: str):
        path = self._doc_path(name)
        if not path:
            return False
        with open(path, 'w') as f:
            f.write(content)
        return True

    # ── Scheduler loop ────────────────────────────────────────────────────

    def _check_slot_key(self, now):
        """Return a slot key aligned to the check interval (e.g. '2026-05-09-04' for a 4h window)."""
        interval = self._state.get('check_interval_hours', 1)
        slot_hour = (now.hour // interval) * interval
        return now.strftime('%Y-%m-%d-') + f'{slot_hour:02d}'

    def _loop(self):
        while not self._stop.wait(_LOOP_INTERVAL):
            with self._lock:
                enabled = self._state.get('enabled')
                running = self._state.get('running')
            if not enabled or running:
                continue
            now        = datetime.now()
            day_key    = now.strftime('%Y-%m-%d')
            check_slot = self._check_slot_key(now)
            daily_time = self._state.get('daily_time', '06:00')
            with self._lock:
                last_check = self._state.get('last_check')
                last_daily = self._state.get('last_daily')

            # Daily fires first; if it fires, also mark the check slot so the
            # smaller check is skipped when both would coincide.
            if now.strftime('%H:%M') >= daily_time and last_daily != day_key:
                self._run_task('daily')
                with self._lock:
                    self._state['last_daily'] = day_key
                    self._state['last_check'] = check_slot  # suppress coinciding check
                    self._save_state()
            elif last_check != check_slot:
                self._run_task('hourly')
                with self._lock:
                    self._state['last_check'] = check_slot
                    self._save_state()

    # ── Task execution ────────────────────────────────────────────────────

    def _run_task(self, task_type: str):
        run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
        print(f"  [Manager] Starting {task_type} run {run_id}")
        with self._lock:
            self._state['running']       = True
            self._state['last_run_type'] = task_type
            self._state['last_run_ts']   = datetime.now().isoformat(timespec='seconds')
            self._save_state()
        try:
            task_file = _DAILY_FILE if task_type == 'daily' else _HOURLY_FILE
            try:
                with open(task_file) as f:
                    task_content = f.read()
            except Exception as e:
                print(f"  [Manager] Cannot read {task_file}: {e}")
                self._write_error_report(run_id, task_type, f"task file unreadable: {e}")
                return

            prompt = self._build_prompt(task_type, run_id, task_content)

            # _run_oneshot blocks until the process exits, and writes its
            # own error report on every failure path.
            if not self._run_oneshot(prompt, run_id, task_type):
                print(f"  [Manager] Run {run_id} failed")
                return
            entry = self._find_report(run_id)
            if not entry:
                return

            print(f"  [Manager] Run {run_id} complete — severity: {entry.get('severity','?')}")
            severity = entry.get('severity', 'ok')
            if severity in ('elevated', 'warning'):
                with self._lock:
                    self._state['unread_alerts'] = True
                    self._save_state()
            if severity in ('elevated', 'warning'):
                self._send_alert(task_type, entry)
            fix = entry.get('fix', '').strip()
            if fix:
                self._apply_fix(fix, entry)

        finally:
            with self._lock:
                self._state['running'] = False
                self._save_state()

    def _collect_snapshot(self) -> str:
        """Pre-collect hourly system metrics so Claude receives data, not commands to run."""

        def _sh(cmd, timeout=5):
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=timeout)
                return r.stdout.strip() or r.stderr.strip() or '(empty)'
            except Exception as e:
                return f'error: {e}'

        def _curl(url, timeout=5):
            try:
                r = subprocess.run(
                    ['curl', '-s', '--max-time', str(timeout), url],
                    capture_output=True, text=True, timeout=timeout + 2)
                return r.stdout.strip()
            except Exception as e:
                return f'error: {e}'

        def _prom(query, timeout=5):
            try:
                r = subprocess.run(
                    ['curl', '-s', '--max-time', str(timeout),
                     '--data-urlencode', f'query={query}',
                     'http://localhost:8080/prometheus/api/v1/query'],
                    capture_output=True, text=True, timeout=timeout + 2)
                d = json.loads(r.stdout)
                return [(x['metric'], x['value'][1]) for x in d['data']['result']]
            except Exception as e:
                return f'error: {e}'

        out = []

        # Service states
        for svc in ['radio-gateway', 'mumble-server-gw1']:
            out.append(f'{svc}: {_sh(f"systemctl is-active {svc}")}')

        # SDR watchdog
        out.append(f'rtl_airband_procs: {_sh("pgrep -c rtl_airband")}')

        # Disk / load / memory
        out.append(f'disk: {_sh("df -h / | tail -1")}')
        out.append(f'uptime: {_sh("uptime")}')
        out.append(f'memory:\n{_sh("free -m")}')

        # Gateway HTTP status (stream connected, sdr_running, etc.)
        raw = _curl('http://localhost:8080/status')
        try:
            d = json.loads(raw)
            out.append(f'stream_connected: {d.get("stream_connected", "?")}')
            out.append(f'sdr_running: {d.get("sdr_running", "?")}')
        except Exception:
            out.append(f'gateway_status_raw: {raw[:300]}')

        # Transcription pool
        raw = _curl('http://localhost:8080/transcriptions?since=0')
        try:
            d = json.loads(raw)
            s = d.get('status', {})
            summary = {k: s.get(k) for k in
                       ('enabled', 'mode', 'model_loaded', 'pending',
                        'inflight', 'pending_audio_secs')}
            out.append(f'transcription: {json.dumps(summary)}')
            for w in (s.get('workers') or []):
                wsum = {k: w.get(k) for k in
                        ('type', 'engine', 'reachable', 'model_loaded', 'inflight',
                         'avg_ratio', 'cpu_temp_c', 'last_switch_error')}
                out.append(f'  worker: {json.dumps(wsum)}')
        except Exception:
            out.append(f'transcription_raw: {raw[:300]}')

        # Supervised child processes. Pre-collected because hourly.md requires
        # cloudflared/mdns state in every report, and the prompt tells the run
        # not to collect data itself — without this the only way to satisfy
        # both was an extra tool call, i.e. two more turns of context re-read.
        raw = _curl('http://localhost:8080/api/processes')
        try:
            d = json.loads(raw)
            out.append(f'supervisor: {d.get("supervisor")}')
            for name, pr in (d.get('processes') or {}).items():
                psum = {k: pr.get(k) for k in
                        ('state', 'pid', 'uptime', 'restart_count', 'last_exit')}
                out.append(f'  process {name}: {json.dumps(psum)}')
        except Exception:
            out.append(f'processes_raw: {raw[:300]}')

        # Prometheus signals
        out.append('--- prometheus ---')
        prom_queries = [
            ('bus_levels_stuck_1h',       'max_over_time(rg_bus_audio_level[1h]) == 0'),
            ('ptt_activity_1h',           'sum(increase(rg_bus_ptt_active[1h]))'),
            ('transcription_inflight_10m','max_over_time(rg_transcription_inflight[10m])'),
            ('stream_throughput_kbps',    'rate(rg_stream_bytes_sent_total[5m]) * 8 / 1000'),
            ('link_flapping_1h',          'changes(rg_link_endpoint_up[1h])'),
            ('link_underruns_per_min',    'rate(rg_link_audio_underruns_total[10m]) * 60'),
            ('cpu_temp_c',                'rg_cpu_temp_c'),
            ('denoise_p99_ms',            'histogram_quantile(0.99, sum by (le, bus, engine)'
                                          ' (rate(rg_denoise_apply_ms_bucket[10m])))'),
        ]
        for label, q in prom_queries:
            out.append(f'{label}: {_prom(q)}')

        return '\n'.join(out)

    def _build_prompt(self, task_type: str, run_id: str, task_content: str) -> str:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if task_type == 'hourly':
            snapshot = self._collect_snapshot()
            return (
                f"[MANAGER TASK — HOURLY] Run ID: {run_id} | Time: {now}\n\n"
                f"{task_content}\n\n"
                f"<pre_collected_snapshot>\n{snapshot}\n</pre_collected_snapshot>\n\n"
                f"The snapshot above was collected by the manager engine immediately before "
                f"this prompt. Do NOT run any data-collection commands — all the values you "
                f"need are already in the snapshot. Read it, apply the thresholds from the "
                f"task description, and write the JSON report now.\n\n"
                f"Your run_id for the report is: {run_id}"
            )
        return (
            f"[MANAGER TASK — {task_type.upper()}] Run ID: {run_id} | Time: {now}\n\n"
            f"{task_content}\n\n"
            f"Your run_id for the report is: {run_id}"
        )

    def _claude_bin(self) -> str:
        return str(getattr(self.config, 'MANAGER_CLAUDE_BIN', '') or
                   os.environ.get('CLAUDE_BIN', '') or
                   '/home/user/.local/bin/claude')

    # Bounded-output rules live here as well as in CLAUDE.md: a one-shot run
    # has no user to stop it pasting a 40k-line journal into its own context.
    _ONESHOT_SYSTEM = (
        "You are a non-interactive one-shot fleet check. Never read an unbounded "
        "log: use `journalctl -n 50 --no-pager` and `tail -n 100`, never bare "
        "`journalctl` or `cat` on a log file. Do not ask questions \u2014 complete the "
        "checks and append the single JSON report line."
    )

    def _run_oneshot(self, prompt: str, run_id: str, task_type: str) -> bool:
        """Run one manager check in a fresh `claude -p` process.

        The manager contract is already stateless \u2014 _build_prompt inlines the
        entire snapshot, and the result comes back through manager_reports.jsonl
        keyed by run_id \u2014 so reusing a conversation buys nothing. It costs a
        great deal: the old tmux session lived for days, and because runs are an
        hour apart (past the prompt cache TTL) each one re-sent AND re-cached the
        whole accumulated history. One 9-day session burned ~68M tokens to move
        ~174k tokens of actual content, with the final hourly checks paying
        ~370k cache-write tokens apiece. A fresh process per run makes that
        growth structurally impossible.
        """
        model = str(getattr(self.config, 'MANAGER_CLAUDE_MODEL', 'sonnet') or 'sonnet')
        try:
            max_turns = int(getattr(self.config, 'MANAGER_MAX_TURNS', 40) or 40)
        except (TypeError, ValueError):
            max_turns = 40
        cmd = [
            self._claude_bin(), '-p', prompt,
            '--dangerously-skip-permissions',
            '--model', model,
            '--max-turns', str(max_turns),
            '--append-system-prompt', self._ONESHOT_SYSTEM,
        ]
        env = dict(os.environ)
        env.setdefault('HOME', '/home/user')
        env['PATH'] = '/home/user/.local/bin:' + env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
        try:
            r = subprocess.run(cmd, cwd=_BASE, capture_output=True, text=True,
                               timeout=_MAX_WAIT_SECS, env=env)
        except subprocess.TimeoutExpired:
            self._write_error_report(run_id, task_type,
                                     f'claude -p timed out after {_MAX_WAIT_SECS}s')
            return False
        except FileNotFoundError:
            self._write_error_report(run_id, task_type,
                                     f'claude binary not found at {self._claude_bin()}')
            return False
        except Exception as e:
            self._write_error_report(run_id, task_type,
                                     f'claude -p failed: {type(e).__name__}: {e}')
            return False

        if self._find_report(run_id):
            return True

        # The run finished but never appended its line. If it printed the JSON
        # instead, salvage it rather than discarding the whole check.
        if self._salvage_report(r.stdout or '', run_id):
            print(f"  [Manager] Run {run_id}: report salvaged from stdout")
            return True

        detail = (r.stderr or r.stdout or '').strip().replace('\n', ' ')[:300]
        self._write_error_report(
            run_id, task_type,
            f'claude -p exit {r.returncode} but no report written: {detail or "no output"}')
        return False

    def _salvage_report(self, stdout: str, run_id: str) -> bool:
        """Append a report the run printed to stdout but failed to write."""
        for line in stdout.splitlines():
            line = line.strip().strip('`').strip()
            if not line.startswith('{') or run_id not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get('run_id') != run_id:
                continue
            try:
                with open(_REPORTS_FILE, 'a') as f:
                    f.write(json.dumps(e) + '\n')
                return True
            except Exception as exc:
                print(f"  [Manager] Salvage write failed: {exc}")
                return False
        return False

    def _report_count(self) -> int:
        try:
            with open(_REPORTS_FILE) as f:
                return sum(1 for l in f if l.strip())
        except FileNotFoundError:
            return 0

    def _find_report(self, run_id: str):
        try:
            with open(_REPORTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if e.get('run_id') == run_id:
                            return e
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
        return None

    def _write_error_report(self, run_id: str, task_type: str, reason: str):
        entry = {
            'ts':       datetime.now().isoformat(timespec='seconds'),
            'task':     task_type,
            'run_id':   run_id,
            'severity': 'elevated',
            'summary':  f"Manager run failed: {reason}",
            'findings': [reason],
        }
        try:
            with open(_REPORTS_FILE, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            print(f"  [Manager] Failed to write error report: {e}")
        with self._lock:
            self._state['unread_alerts'] = True
            self._save_state()

    # Systemd-unit fixes. These MUST go through sudo: the gateway runs as
    # User=user (see radio-gateway.service), and polkit's default policy for
    # org.freedesktop.systemd1.manage-units is auth_admin_keep — so a bare
    # `systemctl restart <unit>` from here fails with
    #   "Access denied ... requires interactive authentication"  (exit 1)
    # *before* systemd even resolves the unit name. Every one of these actions
    # had silently failed that way since they were written; the 2026-07-30
    # stream outage ran 6 extra hours on a fix that never had a chance to run.
    # `sudo -n` (non-interactive) so a missing sudoers rule fails loudly and
    # immediately rather than hanging on a password prompt.
    _UNIT_FIX_ACTIONS = {
        'restart-mumble':  'mumble-server-gw1.service',
        'restart-sdrplay': 'sdrplay.service',
        'restart-gateway': 'radio-gateway.service',
    }

    def _fix_restart_stream(self) -> tuple:
        """Restart the Broadcastify feed.

        This used to be `systemctl restart darkice`, which was wrong twice
        over: there is no darkice.service on this host, and — more
        importantly — the alarm this fix answers is `stream_connected`, which
        reads `stream_output.connected` (gateway_core.py), the gateway's
        in-process Python Icecast client. DarkIce is a separate legacy
        process; restarting it could never clear the alarm. Confirmed
        2026-07-31: darkice restarted cleanly and stream_connected stayed
        false.
        """
        gw = self.gateway
        so = getattr(gw, 'stream_output', None) if gw else None
        if so is None:
            return False, 'no stream_output on the gateway (streaming disabled?)'
        try:
            so.reconnect()
        except Exception as e:
            return False, f'reconnect() raised: {e}'
        # reconnect() is synchronous, so `connected` is meaningful here.
        if getattr(so, 'connected', False):
            return True, 'stream reconnected'
        return False, f"reconnect ran but stream still down: {getattr(so, '_last_error', '') or 'no error recorded'}"

    def _fix_restart_unit(self, unit: str) -> tuple:
        try:
            r = subprocess.run(['sudo', '-n', 'systemctl', 'restart', unit],
                               capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return False, 'timed out after 30s'
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'
        if r.returncode == 0:
            return True, f'{unit} restarted'
        # Surface stderr. The old code captured it and logged only the exit
        # code, which is precisely why an auth failure looked like a mystery
        # "exit 1" for months.
        detail = (r.stderr or r.stdout or '').strip().replace('\n', ' ')[:300]
        return False, f'exit {r.returncode}: {detail or "no output"}'

    # The prompts (hourly.md/daily.md) now ask for 'restart-stream', but a
    # report written against an older prompt — or an LLM going from memory —
    # can still say 'restart-darkice'. Map it rather than dropping the fix.
    _FIX_ALIASES = {'restart-darkice': 'restart-stream'}

    def _apply_fix(self, fix: str, entry: dict):
        alias = self._FIX_ALIASES.get(fix)
        if alias:
            print(f"  [Manager] Fix '{fix}' is deprecated — treating as '{alias}'")
            fix = alias
        print(f"  [Manager] Applying fix: {fix}")
        # The notice goes out BEFORE the fix runs, because restart-gateway kills
        # this process — but it says "attempting", and a second email reports
        # the actual outcome for every fix that survives to send one.
        self._send_fix_notice(fix, entry)
        if fix == 'restart-stream':
            ok, detail = self._fix_restart_stream()
        elif fix in self._UNIT_FIX_ACTIONS:
            ok, detail = self._fix_restart_unit(self._UNIT_FIX_ACTIONS[fix])
        else:
            print(f"  [Manager] Unknown fix '{fix}' — ignored")
            self._send_fix_result(fix, False, 'unknown fix action')
            return
        print(f"  [Manager] Fix '{fix}': {'ok' if ok else 'FAILED'} — {detail}")
        self._send_fix_result(fix, ok, detail)
        # A fix that failed is not a resolved incident. Record it on the entry
        # so the report carries the reason, and keep the alert unread.
        if not ok:
            entry.setdefault('findings', []).append(
                f'AUTO-FIX FAILED: {fix} — {detail}')
            try:
                with self._lock:
                    self._state['unread_alerts'] = True
                    self._save_state()
            except Exception:
                pass

    def _notify_email(self, subject: str, body: str, log_note: str) -> bool:
        """Email an alert via the gateway's EmailNotifier; True only if it went.

        Bounded (smtplib timeout is 15 s), so it is safe to call before a fix
        that may kill this process. A missing or unconfigured notifier is
        logged loudly rather than swallowed: an alert path that fails silently
        looks identical to a healthy fleet.
        """
        notifier = getattr(self.gateway, 'email_notifier', None)
        if notifier is None or not notifier.is_configured():
            print(f"  [Manager] ALERT NOT SENT (email not configured): {subject}")
            return False
        try:
            ok = bool(notifier.send(subject, body))
        except Exception as e:
            print(f"  [Manager] Email send raised: {e}")
            return False
        print(f"  [Manager] {log_note}" if ok else f"  [Manager] Email send FAILED: {subject}")
        return ok

    def _send_alert(self, task_type: str, entry: dict):
        text = f"{entry.get('summary', '')}"
        findings = entry.get('findings', [])
        if findings:
            text += "\n\n" + "\n".join(f"- {f}" for f in findings[:10])
        self._notify_email(
            f"[Gateway Manager] {task_type} run: {entry.get('severity', 'alert')}",
            f"{entry.get('ts', '')}\n\n{text}",
            f"Alert email sent for {task_type} run")

    def _send_fix_notice(self, fix: str, entry: dict):
        # "Attempting", not "applied". This goes out BEFORE the fix runs
        # (restart-gateway kills this process, so there may be no "after" for
        # that one); _send_fix_result reports what actually happened.
        self._notify_email(
            f"[Gateway Manager] auto-fix attempting: {fix}",
            f"{entry.get('ts', '')}\nAttempting: {fix}\n\n{entry.get('summary', '')}",
            f"Fix notice sent: {fix}")

    def _send_fix_result(self, fix: str, ok: bool, detail: str):
        """Report the ACTUAL outcome of a fix."""
        self._notify_email(
            f"[Gateway Manager] auto-fix {'OK' if ok else 'FAILED'}: {fix}",
            f"Action: {fix}\n{detail}",
            f"Fix result sent: {fix}")

    def _doc_path(self, name: str):
        return {
            'constitution': _CONST_FILE,
            'hourly':       _HOURLY_FILE,
            'daily':        _DAILY_FILE,
        }.get(name)
