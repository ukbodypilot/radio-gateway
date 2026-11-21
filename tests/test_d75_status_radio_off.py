#!/usr/bin/env python3
"""/d75status must not claim a powered-off D75 is connected or transmitting.

With the radio off, the D75 link endpoint is still connected but has no
serial session, so its status carries neither `serial_connected` nor
`transmitting`. The route used to hardcode serial_connected=True and leave
`transmitting` out; the page's classList.toggle('tx', undefined) then
flipped both band windows red on every poll.
"""
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import _hwstubs  # noqa: F401,E402  (stubs hardware libs before gateway imports)
import web_routes_get  # noqa: E402

FAILS = 0


def check(name, cond):
    global FAILS
    print(('PASS  ' if cond else 'FAIL  ') + name)
    if not cond:
        FAILS += 1


class _Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.path = '/d75status'

    def send_response(self, *_a):
        pass

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass


def _status(ep_status):
    gw = types.SimpleNamespace(
        config=types.SimpleNamespace(ENABLE_D75=False),
        link_server=object(),
        link_endpoints={'D75CM5': types.SimpleNamespace(plugin_type='d75', audio_level=0, audio_boost=1.0)},
        _link_last_status={'D75CM5': ep_status},
    )
    h = _Handler()
    web_routes_get.handle_d75status(h, types.SimpleNamespace(gateway=gw))
    return json.loads(h.wfile.getvalue().decode())


# Radio off: endpoint reports no serial fields at all.
d = _status({'plugin': 'd75', 'audio_connected': True})
check('radio off: link still reported', d.get('tcp_connected') is True)
check('radio off: serial_connected is False', d.get('serial_connected') is False)
check('radio off: transmitting present and False', d.get('transmitting') is False)

# Radio on and keyed: endpoint values win.
d = _status({'serial_connected': True, 'transmitting': True, 'active_band': 1})
check('radio on: serial_connected forwarded', d.get('serial_connected') is True)
check('radio on: transmitting forwarded', d.get('transmitting') is True)

print('\n%s' % ('ALL PASS' if not FAILS else '%d FAILED' % FAILS))
sys.exit(1 if FAILS else 0)
