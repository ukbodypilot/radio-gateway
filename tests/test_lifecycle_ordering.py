#!/usr/bin/env python3
"""Characterization tests for core/lifecycle.py startup and teardown ORDER.

Why this file exists: `core/lifecycle.py` is 1018 lines, is the next refactor
target, and has no test coverage at all. What makes it dangerous to split is
not its size -- it is that the order of operations inside `run()` and
`cleanup()` is load-bearing, and every constraint is recorded only as a prose
comment. Reorder any of them and the process still starts, the suite still
passes, and you find out weeks later.

The constraints pinned here, and what breaking each one costs:

  * Long-lived subsystems (loop recorder, automation, transcription log,
    transcriber) are constructed BEFORE BusManager.start(), so their object
    graphs are swept into the permanent generation by the startup GC freeze.
    Measured 2026-07-27: gen-1 average 3.080 ms -> 1.016 ms. Move them after
    start() and you silently give back a 3x GC win with no visible symptom.

  * Plugin discovery runs AFTER the buses exist, and bus_manager.reload() runs
    after discovery. Buses built before discovery resolve an external-plugin TX
    radio to None and never call put_audio -- the radio simply never keys,
    while every meter still moves.

  * Teardown is reverse-dependency ordered: the supervisor is reaped first,
    Mumble is stopped BEFORE the audio streams close (it stops the callbacks
    that would otherwise fire into closed streams), and the Broadcastify
    stream is closed before ALSA (prevents mmap errors).

These are assertions about ORDER, not about behaviour. They deliberately do not
check that any subsystem works -- only that `run()` and `cleanup()` still touch
things in the sequence the comments claim. That is exactly the property a
refactor threatens and no other test in this repo covers.

SAFETY: `run()` is not inert. Its log-file preamble deletes
`logs/gateway-*.log` older than LOG_FILE_DAYS, taking its blast radius from a
config value the caller supplies -- running it with a test config against the
repo destroys real logs. `repo_root` is derived from the module's `__file__`,
so pointing `core.lifecycle.__file__` at a temp directory redirects both the
log file and the retention sweep. `_assert_sandboxed()` verifies that
redirection actually took effect and refuses to run if it did not, so a future
change to how `repo_root` is computed fails the test instead of eating logs.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _tmpdirs import mkdtemp   # noqa: E402  (registers cleanup on import)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Recording stand-ins
# ---------------------------------------------------------------------------
class Rec:
    """Records every method call against it. Truthy, so `if self.x:` guards pass."""

    def __init__(self, name, log):
        object.__setattr__(self, '_n', name)
        object.__setattr__(self, '_log', log)

    def __getattr__(self, item):
        if item.startswith('__'):
            raise AttributeError(item)

        def call(*a, **k):
            self._log.append(f'{self._n}.{item}')
            return True        # is_active() and friends
        return call

    def __bool__(self):
        return True


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _recording_class(name, log, extra=None):
    """A class whose construction and every method call are recorded."""
    ns = {
        '__init__': lambda self, *a, **k: log.append(f'{name}.__init__'),
        '__getattr__': lambda self, item: (
            (_ for _ in ()).throw(AttributeError(item)) if item.startswith('__')
            else (lambda *a, **k: log.append(f'{name}.{item}'))
        ),
    }
    ns.update(extra or {})
    return type(name, (), ns)


class Cfg:
    LOG_BUFFER_LINES = 10
    LOG_FILE_DAYS = 1              # deliberately destructive value; sandbox must contain it
    VERBOSE_LOGGING = False
    ENABLE_AUTOMATION = True
    ENABLE_TRANSCRIPTION_LOG = True
    ENABLE_TRANSCRIPTION = True

    def __getattr__(self, k):
        return False


def _build_host(log, sandbox):
    """Compose _LifecycleMixin onto a stub host and stub every seam run() uses."""
    # Deferred imports inside run() are the seam: replace the modules it will
    # import with recorders. gateway_core must be faked too -- importing the
    # real one pulls in the whole gateway.
    class _W:
        def __init__(self, *a, **k):
            self._orig = sys.__stdout__

        def write(self, *a, **k):
            pass

        def flush(self):
            pass

        def _append_log(self, *a):
            pass

    _fake_module('gateway_core', LogWriter=_W, __version__='test')
    _fake_module('loop_recorder', LoopRecorder=_recording_class('LoopRecorder', log))
    _fake_module('radio_automation', AutomationEngine=_recording_class('AutomationEngine', log))
    _fake_module('transcription_log', TranscriptionLog=_recording_class('TranscriptionLog', log))
    _fake_module('transcriber', RadioTranscriber=_recording_class('RadioTranscriber', log))

    bus_extra = {
        'start': lambda self: log.append('BusManager.start'),
        'reload': lambda self: log.append('BusManager.reload'),
        'get_bus_sinks': lambda self: {},
        'get_listen_bus_id': lambda self: 'lb',
        'is_bus_muted': lambda self, i: False,
        'listen_bus': None,
    }
    _fake_module('bus_manager', BusManager=_recording_class('BusManager', log, bus_extra))

    def _discover(cfg, gw):
        log.append('discover_plugins')
        return {'usrp': Rec('plugin:usrp', log)}      # non-empty -> reload() path
    _fake_module('plugin_loader', discover_plugins=_discover)

    import core.lifecycle as L
    from core.monitor import _MonitorMixin
    from core.audio_restart import _AudioRestartMixin
    L.__file__ = os.path.join(sandbox, 'core', 'lifecycle.py')   # redirect repo_root

    # Compose the same mixins RadioGateway does, in the same order. Using
    # _LifecycleMixin alone would still pass, and would therefore not notice a
    # split that forgot to wire the others into gateway_core.
    class Host(L._LifecycleMixin, _MonitorMixin, _AudioRestartMixin):
        def __init__(self):
            self.config = Cfg()
            self.running = True
            self._notifications = []
            self._notif_seq = 0
            self._status_writer = None
            self._watchdog_active = False
            self.ptt_active = False
            # Subsystems whose teardown order we assert on
            self.process_supervisor = Rec('process_supervisor', log)
            self.stream_output = Rec('stream_output', log)
            self.mumble = Rec('mumble', log)
            self.input_stream = Rec('input_stream', log)
            self.output_stream = Rec('output_stream', log)
            self.speaker_stream = Rec('speaker_stream', log)
            self.pyaudio_instance = Rec('pyaudio_instance', log)
            # Everything else stays falsy so its teardown branch is skipped
            self.loop_recorder = None
            self.automation_engine = None
            self.transcriber = None
            self.transcription_log = None
            self.bus_manager = None
            self.mixer = None
            self._external_plugins = {}

        # Seams run() calls directly
        def setup_audio(self):
            log.append('setup_audio')
            return True

        def setup_mumble(self):
            log.append('setup_mumble')
            return True

        def audio_transmit_loop(self):
            pass

        def status_monitor_loop(self):
            # Deliberately NOT recorded: it runs on its own thread, so anything
            # it appended would land at a nondeterministic index and make every
            # ordering assertion flaky.
            pass

        def _load_source_gains(self):
            log.append('_load_source_gains')

        def _apply_source_gains(self):
            log.append('_apply_source_gains')

        def _dump_audio_trace(self, *a, **k):
            pass

        def __getattr__(self, item):
            if item.startswith('__'):
                raise AttributeError(item)
            return lambda *a, **k: None      # unasserted collaborators stay silent

    return Host()


def _assert_sandboxed(sandbox):
    """Refuse to run unless run()'s log dir really points inside the sandbox.

    run() deletes logs/gateway-*.log older than LOG_FILE_DAYS. If the __file__
    redirection ever stops working, this test would delete the real ones. Fail
    loudly instead.
    """
    import core.lifecycle as L
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(L.__file__)))
    log_dir = os.path.join(repo_root, 'logs')
    if os.path.abspath(log_dir).startswith(os.path.abspath(REPO) + os.sep):
        raise SystemExit(
            f"REFUSING TO RUN: computed log_dir is inside the repo ({log_dir}).\n"
            "run() deletes logs/gateway-*.log beyond LOG_FILE_DAYS. The "
            "core.lifecycle.__file__ redirection is no longer effective -- fix "
            "the harness before running this test."
        )
    return log_dir


def _run_lifecycle():
    """Drive run() through startup, straight past the main loop, into cleanup()."""
    log = []
    sandbox = mkdtemp(prefix='lifecycle-')
    os.makedirs(os.path.join(sandbox, 'core'), exist_ok=True)
    host = _build_host(log, sandbox)
    _assert_sandboxed(sandbox)

    host.running = False           # main loop exits at once -> finally: cleanup()
    saved_fd2 = os.dup(2)
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        host.run()
    finally:
        # run() dup2()s over fd 2 and swaps sys.stdout/stderr. cleanup() restores
        # them, but a failure mid-run must not leave the test process mute.
        os.dup2(saved_fd2, 2)
        os.close(saved_fd2)
        sys.stdout, sys.stderr = saved_out, saved_err
    return log, sandbox


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
def _idx(log, event):
    if event not in log:
        raise AssertionError(f'event never happened: {event}')
    return log.index(event)


def _before(log, a, b):
    return _idx(log, a) < _idx(log, b)


CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check('audio is set up before Mumble')
def _(log):
    assert _before(log, 'setup_audio', 'setup_mumble')


@check('long-lived subsystems are built BEFORE BusManager.start (GC freeze)')
def _(log):
    for sub in ('LoopRecorder.__init__', 'AutomationEngine.__init__',
                'TranscriptionLog.__init__', 'RadioTranscriber.__init__'):
        assert _before(log, sub, 'BusManager.start'), (
            f'{sub} moved after BusManager.start() — this silently gives back '
            'the gen-1 3.080ms -> 1.016ms win measured 2026-07-27'
        )


@check('plugin discovery runs after the buses exist')
def _(log):
    assert _before(log, 'BusManager.start', 'discover_plugins')


@check('bus_manager.reload() runs after plugin discovery')
def _(log):
    assert _before(log, 'discover_plugins', 'BusManager.reload'), (
        'without this an external-plugin TX sink stays bound to None and the '
        'radio never keys, while every meter still moves'
    )


@check('persisted gains are restored after BusManager.start')
def _(log):
    assert _before(log, 'BusManager.start', '_load_source_gains')
    assert _before(log, '_load_source_gains', '_apply_source_gains')


@check('teardown reaps the process supervisor first')
def _(log):
    first_teardown = _idx(log, 'process_supervisor.shutdown_all')
    for later in ('stream_output.cleanup', 'mumble.stop', 'input_stream.close'):
        assert first_teardown < _idx(log, later)


@check('Mumble is stopped BEFORE the audio streams close')
def _(log):
    for stream in ('input_stream.close', 'output_stream.close', 'speaker_stream.close'):
        assert _before(log, 'mumble.stop', stream), (
            'closing audio streams first leaves Mumble callbacks firing into '
            'closed streams'
        )


@check('the Broadcastify stream is closed before ALSA')
def _(log):
    assert _before(log, 'stream_output.cleanup', 'input_stream.close'), (
        'stopping ALSA first produces mmap errors'
    )


@check('PyAudio is terminated after every stream is closed')
def _(log):
    for stream in ('input_stream.close', 'output_stream.close', 'speaker_stream.close'):
        assert _before(log, stream, 'pyaudio_instance.terminate')


@check('startup precedes teardown for every subsystem touched')
def _(log):
    assert _before(log, 'setup_audio', 'process_supervisor.shutdown_all')


@check('run() writes its log inside the sandbox, not the repo')
def _(log):
    # Pinned because repo_root is derived from __file__'s DEPTH: moving this
    # method one directory level in a refactor relocates the logs silently.
    # The same dirname(__file__) mistake already cost this repo two months of
    # daily logs landing in core/logs/.
    assert True   # enforced by _assert_sandboxed() before run(); see below


def _check_composition():
    """The real RadioGateway must still expose everything lifecycle.py used to.

    core/lifecycle.py was split (monitor + audio_restart extracted). A split
    that moves a method out but forgets to add its mixin to the RadioGateway
    bases fails only at runtime, on the first call -- for status_monitor_loop
    that is the supervisor thread dying silently one second after startup.
    """
    import gateway_core
    cls = gateway_core.RadioGateway
    out = []
    for meth, expect in [('run', '_LifecycleMixin'),
                         ('cleanup', '_LifecycleMixin'),
                         ('notify', '_LifecycleMixin'),
                         ('status_monitor_loop', '_MonitorMixin'),
                         ('_charger_should_be_on', '_MonitorMixin'),
                         ('restart_audio_input', '_AudioRestartMixin'),
                         ('restart_pyaudio', '_AudioRestartMixin')]:
        owner = next((c.__name__ for c in cls.__mro__ if meth in c.__dict__), None)
        ok = owner == expect
        out.append((ok, f'{meth} resolves from {expect}'
                        + ('' if ok else f' (got {owner})')))
    return out


def main():
    # Composition is checked FIRST: _run_lifecycle() installs a fake
    # `gateway_core` in sys.modules (run() imports LogWriter from it), which
    # would shadow the real module this check needs.
    failed = 0
    for ok, label in _check_composition():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failed += 1

    log, sandbox = _run_lifecycle()

    for name, fn in CHECKS:
        try:
            fn(log)
            print(f'  PASS  {name}')
        except AssertionError as e:
            failed += 1
            print(f'  FAIL  {name}\n          {e}')

    # The sandbox assertion is structural: prove the log file really landed there.
    logs_dir = os.path.join(sandbox, 'logs')
    if os.path.isdir(logs_dir) and any(f.startswith('gateway-') for f in os.listdir(logs_dir)):
        print('  PASS  log file was created inside the sandbox')
    else:
        failed += 1
        print(f'  FAIL  no gateway-*.log in {logs_dir} — sandbox seam may be broken')

    print(f'\n  recorded sequence ({len(log)} events):')
    for i, e in enumerate(log):
        print(f'    {i:>3}  {e}')

    print('\nALL PASS' if not failed else f'\n{failed} FAILED')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
