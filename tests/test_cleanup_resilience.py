#!/usr/bin/env python3
"""cleanup(): a step that raises must not skip the steps after it.

Before, the loop recorder's stop() and the PTT release ran outside any
try, so an exception from the recorder ended cleanup() on the spot: PTT
stayed keyed, Mumble, the CAT client and every audio stream stayed open.
(test_lifecycle_ordering.py pins the ORDER; this pins that it completes.)
"""

import contextlib
import io
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import core.lifecycle as lifecycle  # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class Rec:
    """Stand-in subsystem: logs every method call; raises for names in `fail`."""

    def __init__(self, name, log, fail):
        self._n, self._log, self._fail = name, log, fail

    def __getattr__(self, m):
        if m.startswith('__'):
            raise AttributeError(m)

        def call(*a, **k):
            self._log.append(f'{self._n}.{m}')
            if f'{self._n}.{m}' in self._fail or '*' in self._fail:
                raise RuntimeError('boom')
            return True if m == 'is_active' else None
        return call


SUBSYSTEMS = [
    'process_supervisor', 'loop_recorder', 'stream_output', 'mumble', 'sdr_plugin',
    'remote_audio_source', 'remote_audio_server', 'announce_input_source', 'relay_radio',
    'relay_charger', 'automation_engine', 'transcriber', 'transcription_log', 'smart_announce',
    'manager_engine', 'alert_engine', 'web_config_server', 'gps_manager', 'repeater_manager',
    'ddns_updater', 'cloudflare_tunnel', 'relay_ptt', 'cat_client', 'mumble_server_1',
    'mumble_server_2', 'input_stream', 'speaker_stream', 'output_stream', 'pyaudio_instance',
    'aioc_device',
]


def run_cleanup(fail):
    log = []
    h = object.__new__(type('Host', (lifecycle._LifecycleMixin,), {}))
    for a in SUBSYSTEMS:
        setattr(h, a, Rec(a, log, fail))
    h._external_plugins = {}
    h.config = types.SimpleNamespace(VERBOSE_LOGGING=False)
    h._status_writer = None
    h._watchdog_active = False
    h.running = True
    h.ptt_active = True
    h._dump_audio_trace = lambda: None
    h.set_ptt_state = lambda s: log.append(f'set_ptt({s})')
    out = io.StringIO()
    real_sleep = lifecycle.time.sleep
    lifecycle.time.sleep = lambda s: None
    try:
        with contextlib.redirect_stdout(out):
            h.cleanup()
    finally:
        lifecycle.time.sleep = real_sleep
    return log, out.getvalue()


def main():
    print("1. the loop recorder raising")
    log, out = run_cleanup({'loop_recorder.stop'})
    check("PTT still released", 'set_ptt(False)' in log, str(log[:5]))
    check("Mumble still stopped", 'mumble.stop' in log)
    check("audio streams still closed", 'output_stream.close' in log and 'input_stream.close' in log)
    check("finishes", 'Shutdown complete' in out)
    check("the failure is reported", 'loop recorder failed' in out, out)

    print("\n2. every subsystem raising")
    log, out = run_cleanup({'*'})
    check("finishes anyway", 'Shutdown complete' in out, out[-200:])
    check("every subsystem was still tried",
          all(any(e.startswith(a + '.') for e in log) for a in SUBSYSTEMS),
          str([a for a in SUBSYSTEMS if not any(e.startswith(a + '.') for e in log)]))

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
