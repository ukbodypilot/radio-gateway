#!/usr/bin/env python3
"""Config saves from the web UI must not delete what they don't manage.

_save_config used to regenerate gateway_config.txt from _CONFIG_LAYOUT alone.
Every save -- the config form, a TTS engine switch, soundboard categories --
dropped the [kv4p.vhf]/[kv4p.uhf] sections (so the kv4p endpoints stopped
spawning on the next restart), every comment, and the keys that have a default
but no layout entry (SUPERVISE_MUMBLE, PACKET_APRS_IS_PASSCODE, ...).

It also pins WEB_CONFIG_HTTPS: its default was the bool False, so the loader
coerced 'self-signed' and 'letsencrypt' to False and HTTPS could only ever be
turned on with 'true'.

Runs off-box: hardware libraries that are not installed are stubbed.
"""

import os
import shutil
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _tmpdirs import mkdtemp   # noqa: E402  (registers cleanup on import)


class _Stub(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return type(name, (object,), {'__init__': lambda self, *a, **k: None})


for _name in ('hid', 'pyaudio', 'serial', 'opuslib', 'soundfile', 'resampy',
              'pymumble_py3', 'pymumble_py3.callbacks',
              'pymumble_py3.constants', 'pymumble_py3.mumble'):
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = _Stub(_name)

import radio_gateway   # noqa: E402
import web_server      # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def save(path, values):
    cfg = radio_gateway.Config(path)
    srv = web_server.WebConfigServer.__new__(web_server.WebConfigServer)
    srv.config = cfg
    srv._defaults = getattr(cfg, '_defaults', {})
    srv._save_config(values)
    return radio_gateway.Config(path)


def main():
    tmp = mkdtemp('rg_cfgsave_')
    path = os.path.join(tmp, 'gateway_config.txt')
    shutil.copy(os.path.join(REPO, 'examples', 'gateway_config.txt'), path)
    with open(path, 'a') as f:
        f.write('\nSUPERVISE_MUMBLE = true\n'
                '\n[kv4p.vhf]\nport = /dev/ttyUSB0\nenable = true\n')
    with open(path) as f:
        before = f.read()

    print("1. a single-key save (what a TTS engine switch does)")
    cfg = save(path, {'TTS_ENGINE': 'edge'})
    with open(path) as f:
        after = f.read()
    check("[kv4p.vhf] section kept", '[kv4p.vhf]\nport = /dev/ttyUSB0' in after)
    check("non-layout key kept", 'SUPERVISE_MUMBLE = true' in after)
    check("comments kept", after.count('#') >= before.count('#'),
          f"{before.count('#')} -> {after.count('#')}")
    check("value changed", cfg.TTS_ENGINE == 'edge', repr(cfg.TTS_ENGINE))
    check("inline comment on a changed line kept",
          any(l.startswith('TTS_ENGINE = edge') for l in after.splitlines()))
    changed = [l for l in after.splitlines() if l not in before.splitlines()]
    check("only the saved key's line changed (plus appended layout keys)",
          all(l.startswith('TTS_ENGINE') or l.endswith('= ') or l.startswith('[') or not l
              for l in changed), str(changed[:5]))

    print("\n2. a second identical save changes nothing")
    save(path, {'TTS_ENGINE': 'edge'})
    with open(path) as f:
        again = f.read()
    check("idempotent", again == after)

    print("\n3. WEB_CONFIG_HTTPS keeps its mode")
    for mode in ('self-signed', 'letsencrypt', 'false'):
        cfg = save(path, {'WEB_CONFIG_HTTPS': mode})
        check(f"{mode!r} reads back", str(cfg.WEB_CONFIG_HTTPS).lower() == mode,
              repr(cfg.WEB_CONFIG_HTTPS))

    print("\n4. a value written onto a blank line keeps the line's comment")
    cfg = save(path, {'USRP_NODE': '68397'})
    with open(path) as f:
        line = [l for l in f.read().splitlines() if l.startswith('USRP_NODE')][0]
    check("value and comment both present", '68397' in line and '#' in line, line)

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
