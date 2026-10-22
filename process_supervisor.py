"""Shared supervisor for long-running child processes.

One supervisor thread per managed entry: optional adoption check -> spawn
-> wait -> on exit, reschedule with exponential backoff (capped) if restart=True.

Features used across the codebase:
  - persist_across_restart: leave child running on shutdown_all() (cloudflared).
  - adopt_existing: pre-spawn hook returning a PID to adopt instead of spawning.
  - stdout_handler: live line callback (direwolf log forwarding, cloudflared URL).
  - log_file: defaults to logs/<name>.log with size-based rotation.

Pure stdlib so the same module can run inside link_endpoint.py if needed.
"""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_LOG_ROTATE_BYTES = 5 * 1024 * 1024
_LOG_KEEP = 3


@dataclass
class _Entry:
    name: str
    argv: list
    env: Optional[dict]
    cwd: Optional[str]
    restart: bool
    backoff: tuple
    persist_across_restart: bool
    adopt_existing: Optional[Callable]
    stdout_handler: Optional[Callable]
    log_file: str
    run_as_user: Optional[str] = None
    run_as_group: Optional[str] = None

    proc: Optional[subprocess.Popen] = None
    adopted_pid: Optional[int] = None
    state: str = 'pending'        # pending, starting, running, stopped, failed
    started_at: float = 0.0
    last_exit: Optional[int] = None
    restart_count: int = 0
    thread: Optional[threading.Thread] = None
    # (previously annotated as `field(default_factory=...)` used as a TYPE,
    # which does nothing — the attributes were None unless add() set them)
    stop_event: Optional[threading.Event] = None
    lock: Optional[threading.Lock] = None
    # Generation counter: bumped by stop()/restart() to invalidate the
    # running supervisor loop. Closes two races: (a) stop() during the
    # spawn window left the fresh child running while state said
    # 'stopped'; (b) restart() cleared stop_event under a still-alive old
    # loop, which then kept supervising alongside the new one.
    gen: int = 0
    loop_gen: int = -1


class ProcessSupervisor:
    def __init__(self, log_dir=_LOG_DIR):
        self._log_dir = log_dir
        os.makedirs(self._log_dir, exist_ok=True)
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    # ── Registration ────────────────────────────────────────────────

    def add(self, name, argv, env=None, cwd=None,
            restart=True, backoff=(1, 30),
            persist_across_restart=False,
            adopt_existing=None,
            stdout_handler=None,
            log_file=None,
            autostart=True,
            run_as_user=None, run_as_group=None):
        """Register and (by default) start a supervised process.

        run_as_user / run_as_group: drop privileges to this account via
        os.setgid/setuid in a preexec_fn. Requires the calling process to
        be root, and is used for services like mumble-server that demand
        a dedicated user. Pass account names as strings ('_mumble-server').
        """
        with self._lock:
            if name in self._entries:
                raise ValueError(f"ProcessSupervisor: '{name}' already registered")
            e = _Entry(
                name=name, argv=list(argv), env=env, cwd=cwd,
                restart=restart, backoff=backoff,
                persist_across_restart=persist_across_restart,
                adopt_existing=adopt_existing,
                stdout_handler=stdout_handler,
                log_file=log_file or os.path.join(self._log_dir, f'{name}.log'),
                run_as_user=run_as_user, run_as_group=run_as_group,
            )
            e.stop_event = threading.Event()
            e.lock = threading.Lock()
            self._entries[name] = e
        if autostart:
            self.start(name)
        return name

    def remove(self, name):
        self.stop(name)
        with self._lock:
            self._entries.pop(name, None)

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self, name):
        e = self._get(name)
        with e.lock:
            # A live loop of the CURRENT generation is already supervising.
            # (An older-generation loop may still be draining — it exits on
            # its own via the gen check and must not block a fresh start.)
            if e.thread and e.thread.is_alive() and e.loop_gen == e.gen:
                return
            # Fresh event per loop generation: the old loop keeps its own
            # (already-set) event; clearing a shared one would revive it.
            e.stop_event = threading.Event()
            e.loop_gen = e.gen
            e.thread = threading.Thread(
                target=self._supervisor_loop, args=(e, e.gen, e.stop_event),
                daemon=True, name=f'sup-{name}')
            e.thread.start()

    def stop(self, name, timeout=5.0):
        e = self._get(name)
        with e.lock:
            e.gen += 1  # invalidate the running loop
        e.stop_event.set()
        with e.lock:
            proc = e.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
        if e.thread:
            e.thread.join(timeout=timeout + 1)
        with e.lock:
            e.state = 'stopped'

    def restart(self, name):
        self.stop(name)
        self.start(name)

    def shutdown_all(self, timeout=5.0):
        # Snapshot to avoid mutation during iteration
        with self._lock:
            names = list(self._entries.keys())
        for n in names:
            e = self._entries[n]
            if e.persist_across_restart:
                # Detach: stop supervising but leave the child running
                e.stop_event.set()
                if e.thread:
                    e.thread.join(timeout=timeout)
                continue
            self.stop(n, timeout=timeout)

    # ── Status ──────────────────────────────────────────────────────

    def status(self, name):
        e = self._get(name)
        with e.lock:
            pid = e.proc.pid if (e.proc and e.proc.poll() is None) else e.adopted_pid
            uptime = (time.time() - e.started_at) if (e.started_at and pid) else 0.0
            return {
                'name': e.name,
                'state': e.state,
                'pid': pid,
                'uptime': round(uptime, 1),
                'restart_count': e.restart_count,
                'last_exit': e.last_exit,
                'adopted': e.adopted_pid is not None,
            }

    def status_all(self):
        with self._lock:
            names = list(self._entries.keys())
        return {n: self.status(n) for n in names}

    # ── Internals ───────────────────────────────────────────────────

    def _get(self, name) -> _Entry:
        with self._lock:
            e = self._entries.get(name)
        if not e:
            raise KeyError(f"ProcessSupervisor: unknown process '{name}'")
        return e

    def _supervisor_loop(self, e: _Entry, my_gen: int, stop_evt: threading.Event):
        """One supervision loop, valid for a single generation.

        *my_gen* / *stop_evt* are captured at spawn — stop()/restart()
        bump e.gen and set the CURRENT event, so an old loop always sees
        its own stop signal even after start() installs a fresh event.
        """
        def _stale():
            return stop_evt.is_set() or e.gen != my_gen

        bmin, bmax = e.backoff
        delay = bmin
        while not _stale():
            # Adoption check first
            adopted_pid = None
            if e.adopt_existing:
                try:
                    adopted_pid = e.adopt_existing()
                except Exception as ex:
                    print(f"  [Sup:{e.name}] adopt_existing error: {ex}", flush=True)
            if adopted_pid:
                with e.lock:
                    e.adopted_pid = adopted_pid
                    e.state = 'running'
                    e.started_at = time.time()
                print(f"  [Sup:{e.name}] adopted existing PID {adopted_pid}", flush=True)
                # Poll until the adopted process disappears or we're asked to stop
                while not _stale():
                    if not _pid_alive(adopted_pid):
                        break
                    time.sleep(2.0)
                with e.lock:
                    e.adopted_pid = None
                if _stale():
                    break
                # Adopted process died; fall through to spawn fresh
            try:
                self._spawn_and_wait(e, my_gen, stop_evt)
            except Exception as ex:
                print(f"  [Sup:{e.name}] spawn error: {ex}", flush=True)
                with e.lock:
                    e.state = 'failed'
            if not e.restart or _stale():
                break
            # Backoff before respawn
            with e.lock:
                e.restart_count += 1
            if stop_evt.wait(delay):
                break
            delay = min(bmax, delay * 2)
            # Reset backoff if the previous run was long-lived
            if (time.time() - e.started_at) > 60:
                delay = bmin

    def _spawn_and_wait(self, e: _Entry, my_gen: int, stop_evt: threading.Event):
        log_f = _open_log(e.log_file)
        try:
            stdout = subprocess.PIPE if e.stdout_handler else log_f
            stderr = subprocess.STDOUT
            preexec_fn = _build_preexec(e.run_as_user, e.run_as_group)
            # Force unbuffered output for Python-based children so prints
            # land in the log promptly instead of sitting in a 4 KB block
            # buffer (default when stdout is a file, not a TTY).
            env = dict(e.env) if e.env else dict(os.environ)
            env.setdefault('PYTHONUNBUFFERED', '1')
            proc = subprocess.Popen(
                e.argv, env=env, cwd=e.cwd,
                stdout=stdout, stderr=stderr,
                bufsize=1, text=bool(e.stdout_handler),
                start_new_session=e.persist_across_restart,
                preexec_fn=preexec_fn,
            )
        except FileNotFoundError as ex:
            print(f"  [Sup:{e.name}] not found: {ex}", flush=True)
            with e.lock:
                e.state = 'failed'
            try:
                log_f.close()
            except Exception:
                pass
            return
        with e.lock:
            e.proc = proc
            e.state = 'running'
            e.started_at = time.time()
        # Close the stop-during-spawn window: stop() looked at e.proc
        # before we registered this child, so it couldn't terminate it.
        # If a stop/restart landed while we were spawning, reap it now.
        if stop_evt.is_set() or e.gen != my_gen:
            try:
                proc.terminate()
            except Exception:
                pass
        print(f"  [Sup:{e.name}] spawned PID {proc.pid}: {' '.join(e.argv)}", flush=True)

        # If stdout_handler is set, drain stdout line-by-line and also tee to log
        if e.stdout_handler:
            try:
                for line in proc.stdout:
                    try:
                        log_f.write(line)
                        log_f.flush()
                    except Exception:
                        log_exc()
                    try:
                        e.stdout_handler(line.rstrip('\n'))
                    except Exception as ex:
                        print(f"  [Sup:{e.name}] stdout_handler error: {ex}",
                              flush=True)
            except Exception:
                log_exc()
        rc = proc.wait()
        with e.lock:
            e.last_exit = rc
            e.proc = None
        try:
            log_f.close()
        except Exception:
            pass
        print(f"  [Sup:{e.name}] exited rc={rc}", flush=True)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _open_log(path):
    # Cheap size-based rotation: roll over once the file exceeds the cap
    try:
        if os.path.exists(path) and os.path.getsize(path) > _LOG_ROTATE_BYTES:
            for i in range(_LOG_KEEP - 1, 0, -1):
                src, dst = f'{path}.{i}', f'{path}.{i+1}'
                if os.path.exists(src):
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
            try:
                os.replace(path, f'{path}.1')
            except OSError:
                pass
    except Exception:
        log_exc()
    return open(path, 'a', buffering=1)


def _build_preexec(user, group):
    """Return a preexec_fn that drops privileges to user:group, or None."""
    if not user and not group:
        return None
    import grp
    import pwd

    uid = gid = None
    if user:
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError as e:
            raise ValueError(f"unknown user {user!r}") from e
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError as e:
            raise ValueError(f"unknown group {group!r}") from e

    def _pre():
        if gid is not None:
            os.setgid(gid)
        if uid is not None:
            os.setuid(uid)
    return _pre


def pgrep_first(name):
    """Helper: return first matching PID for `pgrep -x NAME`, or None."""
    try:
        r = subprocess.run(['pgrep', '-x', name],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            pids = r.stdout.split()
            if pids:
                return int(pids[0])
    except Exception:
        log_exc()
    return None
