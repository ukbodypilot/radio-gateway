#!/usr/bin/env python3
"""The /routing gain slider and mute must reach discovered plugins (usrp...).

_get_plugin_by_id only knew built-in sources and link endpoints, so the
slider on an AllStar node returned "unknown source/sink" and changed nothing
live, even though a gain saved in source_gains.json was applied at startup
through bus_manager._get_source.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

from web.routing_cmds import _RoutingCmdsMixin  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


node = types.SimpleNamespace(audio_boost=1.0, tx_audio_boost=1.0, muted=False, tx_muted=False)
saved = []
gw = types.SimpleNamespace(
    sdr_plugin=None, link_endpoints={}, _external_plugins={'usrp': node},
    _source_gains={}, _save_source_gains=lambda: saved.append(1))

r = _RoutingCmdsMixin.__new__(_RoutingCmdsMixin)
r.gateway = gw

check('usrp resolves to the plugin', r._get_plugin_by_id('usrp') is node)
check('usrp_tx resolves to the same plugin', r._get_plugin_by_id('usrp_tx') is node)
check('unknown id still None', r._get_plugin_by_id('nope') is None)
check('unknown _tx id still None', r._get_plugin_by_id('nope_tx') is None)

res = r._routing_cmd_gain({'id': 'usrp', 'value': 316}, [], [])
check('gain on usrp ok', res.get('ok'), res)
check('RX gain applied live', abs(node.audio_boost - 3.16) < 1e-9, node.audio_boost)
check('TX gain untouched', node.tx_audio_boost == 1.0)
check('gain persisted', gw._source_gains.get('usrp') == 316 and saved, gw._source_gains)

res = r._routing_cmd_gain({'id': 'usrp_tx', 'value': 50}, [], [])
check('usrp_tx gain sets TX boost', res.get('ok') and abs(node.tx_audio_boost - 0.5) < 1e-9)

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
