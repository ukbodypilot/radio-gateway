"""Stand-ins for the hardware libraries, for running tests off the box.

Installed into sys.modules only for libraries that are NOT importable, so on
the gateway itself the real hidapi / pyaudio / pymumble / opuslib are used.
tests/run_all.py loads this before each test; a test can also import it.
Every stub refuses to open a device, so nothing pretends to be a radio.
"""

import importlib
import sys
import types


class _Any:
    """Absorbs any attribute access or call (pyaudio constants, opus objects)."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return _Any()

    def __int__(self):
        return 0


def _module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    m.__getattr__ = lambda n: _Any if not n.startswith('__') else (_ for _ in ()).throw(AttributeError(n))
    m.__hwstub__ = True
    return m


def _hid():
    class device:
        def __init__(self, *a, **k):
            raise OSError('hid stub: no hardware off the box')
    return {'hid': _module('hid', device=device, Device=device, enumerate=lambda *a, **k: [])}


def _pyaudio():
    class PyAudio(_Any):
        def get_device_count(self):
            return 0

        def get_default_host_api_info(self):
            return {'deviceCount': 0}

        def terminate(self):
            pass
    return {'pyaudio': _module('pyaudio', PyAudio=PyAudio, paInt16=8,
                               paContinue=0, paComplete=1)}


def _pymumble():
    class Mumble:
        def __init__(self, *a, **k):
            raise OSError('pymumble stub: no Mumble server off the box')
    pkg = _module('pymumble_py3', Mumble=Mumble)
    pkg.__path__ = []
    cb = _module('pymumble_py3.callbacks',
                 PYMUMBLE_CLBK_SOUNDRECEIVED='sound',
                 PYMUMBLE_CLBK_TEXTMESSAGERECEIVED='text',
                 PYMUMBLE_CLBK_USERCREATED='uc', PYMUMBLE_CLBK_USERREMOVED='ur',
                 PYMUMBLE_CLBK_CONNECTED='c', PYMUMBLE_CLBK_DISCONNECTED='d')
    const = _module('pymumble_py3.constants')
    mumble = _module('pymumble_py3.mumble', Mumble=Mumble)
    pkg.callbacks, pkg.constants, pkg.mumble = cb, const, mumble
    return {'pymumble_py3': pkg, 'pymumble_py3.callbacks': cb,
            'pymumble_py3.constants': const, 'pymumble_py3.mumble': mumble}


def _opuslib():
    return {'opuslib': _module('opuslib')}


_LIBS = (('hid', _hid), ('pyaudio', _pyaudio),
         ('pymumble_py3', _pymumble), ('opuslib', _opuslib))

installed = []


def install():
    for name, make in _LIBS:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception:
            sys.modules.update(make())
            installed.append(name)


install()
