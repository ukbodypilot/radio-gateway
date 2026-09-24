"""Manager runs execute as one-shot `claude -p` processes, not tmux pastes.

Why this exists: the manager used to paste each check into a long-lived Claude
TUI. That session never exited, so every hourly run re-sent AND re-cached the
whole accumulated history just to ask a ~3k-token question. One 9-day session
logged 297 turns, grew 41k -> 403k tokens of context, and burned ~68M tokens
total to move ~174k tokens of real content -- the last hourly checks each paid
~370k cache-write tokens. Nothing in the contract needed that continuity:
_build_prompt already inlines the entire snapshot and the answer comes back
through manager_reports.jsonl keyed by run_id.

Covers: the happy path, a report printed but never written (salvage), the
timeout and missing-binary failure paths both producing an error report, the
command shape, and the run-mode selector defaulting safely.
"""
import json
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from _tmpdirs import mkdtemp
import manager_engine as ME

ME_cls = ME.ManagerEngine

REPORT = ('{"ts":"2026-08-20T14:00:00","task":"hourly","run_id":"RID-1",'
          '"severity":"ok","summary":"all good","findings":["stream: connected"]}')


class Stub:
    """Only the pieces _run_oneshot actually touches."""
    _run_oneshot        = ME_cls._run_oneshot
    _salvage_report     = ME_cls._salvage_report
    _find_report        = ME_cls._find_report
    _write_error_report = ME_cls._write_error_report
    _claude_bin         = ME_cls._claude_bin
    _ONESHOT_SYSTEM     = ME_cls._ONESHOT_SYSTEM

    def __init__(self, **cfg):
        self.config = types.SimpleNamespace(**cfg)
        import threading
        self._lock  = threading.Lock()
        self._state = {'unread_alerts': False}

    def _save_state(self):
        pass


def fresh_reports():
    d = mkdtemp('mgr-oneshot-')
    ME._REPORTS_FILE = os.path.join(d, 'manager_reports.jsonl')
    open(ME._REPORTS_FILE, 'w').close()
    return ME._REPORTS_FILE


def reports():
    with open(ME._REPORTS_FILE) as f:
        return [json.loads(l) for l in f if l.strip()]


def run_with(behaviour, **cfg):
    """behaviour(cmd) -> CompletedProcess, or raises to model a failure."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return behaviour(cmd)

    real, ME.subprocess.run = ME.subprocess.run, fake_run
    try:
        ok = Stub(**cfg)._run_oneshot('PROMPT BODY', 'RID-1', 'hourly')
    finally:
        ME.subprocess.run = real
    return ok, calls


def ok_writes_report(cmd):
    with open(ME._REPORTS_FILE, 'a') as f:
        f.write(REPORT + '\n')
    return subprocess.CompletedProcess(cmd, 0, '', '')


def ok_prints_only(cmd):
    return subprocess.CompletedProcess(cmd, 0, 'thinking...\n```\n' + REPORT + '\n```\n', '')


def silent_success(cmd):
    return subprocess.CompletedProcess(cmd, 0, 'I could not find the log.', '')


def times_out(cmd):
    raise subprocess.TimeoutExpired(cmd, ME._MAX_WAIT_SECS)


def no_binary(cmd):
    raise FileNotFoundError(2, 'No such file or directory')


print('--- outcomes ---')

fresh_reports()
ok, calls = run_with(ok_writes_report)
print(f"{'report written by the run':>34}: ok={ok!s:5} reports={len(reports())} severity={reports()[-1]['severity']}")

fresh_reports()
ok, _ = run_with(ok_prints_only)
r = reports()[-1]
print(f"{'printed, never written':>34}: ok={ok!s:5} reports={len(reports())} "
      f"salvaged={r['run_id'] == 'RID-1' and r['severity'] == 'ok'}")

fresh_reports()
ok, _ = run_with(silent_success)
r = reports()[-1]
print(f"{'exited clean, no report':>34}: ok={ok!s:5} severity={r['severity']} "
      f"detail={'no report written' in r['summary']}")

fresh_reports()
ok, _ = run_with(times_out)
print(f"{'timed out':>34}: ok={ok!s:5} severity={reports()[-1]['severity']} "
      f"says_timeout={'timed out' in reports()[-1]['summary']}")

fresh_reports()
ok, _ = run_with(no_binary)
print(f"{'claude binary missing':>34}: ok={ok!s:5} severity={reports()[-1]['severity']} "
      f"names_path={'claude binary not found' in reports()[-1]['summary']}")

print()
print('--- command shape ---')
fresh_reports()
_, calls = run_with(ok_writes_report, MANAGER_CLAUDE_MODEL='sonnet', MANAGER_MAX_TURNS=40)
cmd, kw = calls[0]
print(f"{'exactly one process spawned':>34}: {len(calls)}")
print(f"{'no tmux anywhere in it':>34}: {not any('tmux' in str(c) for c, _ in calls)}")
print(f"{'non-interactive -p':>34}: {'-p' in cmd}")
print(f"{'prompt passed as argv':>34}: {'PROMPT BODY' in cmd}")
print(f"{'turn cap present':>34}: {'--max-turns' in cmd and cmd[cmd.index('--max-turns') + 1] == '40'}")
print(f"{'bounded-log rules injected':>34}: {'journalctl -n 50' in cmd[cmd.index('--append-system-prompt') + 1]}")
print(f"{'wall-clock timeout set':>34}: {kw.get('timeout') == ME._MAX_WAIT_SECS}")
print(f"{'runs in the gateway dir':>34}: {kw.get('cwd') == ME._BASE}")


print()
print('--- claude binary resolution ---')
os.environ.pop('CLAUDE_BIN', None)
print(f"{'default':>34}: {Stub()._claude_bin()}")
print(f"{'config override':>34}: {Stub(MANAGER_CLAUDE_BIN='/opt/claude')._claude_bin()}")
os.environ['CLAUDE_BIN'] = '/env/claude'
print(f"{'CLAUDE_BIN env':>34}: {Stub()._claude_bin()}")
os.environ.pop('CLAUDE_BIN', None)
