"""MCP tools read today's payload shapes (2026-11-27 audit).

Each case is a tool that had drifted from the gateway: it ran, but crashed or
printed '?' because a field was renamed. The fake _get returns payloads copied
from the live gateway, so a future rename fails here instead of quietly in chat.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import mcp_server  # noqa: E402

mcp_server._register_all_tools()
from mcp_server.tools import cloud, control, fleet, transcription, workers  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + str(detail)[:300] if detail and not cond else ''}")
    if not cond:
        FAIL.append(name)


def fake(mod, payloads):
    mod._get = lambda path, timeout=5: payloads[path.split('?', 1)[0]]


# smart_announce: countdowns became dicts, and 'remaining' can be a word
fake(control, {'/status': {'smart_announce_enabled': True, 'smart_activity': {},
                           'smart_countdowns': [{'id': 1, 'remaining': 'Manual', 'mode': 'manual'},
                                                {'id': 2, 'remaining': 125, 'mode': 'auto'}]}})
out = control.smart_announce_status()
check('smart_announce: word countdown', 'slot 1 [manual]: Manual' in out, out)
check('smart_announce: seconds countdown', 'slot 2 [auto]: next in 2m05s' in out, out)

# gdrive: remote/folder_path/storage, not account_email/folder_name
fake(cloud, {'/api/gdrive/status': {'configured': True, 'remote': 'gdrive', 'folder_path': 'radio-gateway',
                                    'authenticated': True, 'folder_accessible': True,
                                    'total_bytes': 18 * 1024 ** 3, 'used_bytes': 3 * 1024 ** 3,
                                    'free_bytes': 15 * 1024 ** 3}})
out = cloud.gdrive_status()
check('gdrive: no ? fields', '?' not in out and 'radio-gateway' in out and '3.0 GB used of 18.0 GB' in out, out)

# cloudflare: short links from /status
fake(cloud, {'/status': {'tunnel_url': 'https://a-b-c-d.trycloudflare.com',
                         'tunnel_short_links': {'links': {'/': 'https://spoo.me/abc1234'}, 'blocked': ''}}})
out = cloud.cloudflare_status()
check('cloudflare: short link shown', 'https://spoo.me/abc1234' in out, out)
fake(cloud, {'/status': {'tunnel_url': 'https://a-b-c-d.trycloudflare.com',
                         'tunnel_short_links': {'links': {}, 'blocked': 'TUNNEL_SHORT_LINKS is off'}}})
check('cloudflare: says why links are off', 'off (TUNNEL_SHORT_LINKS is off)' in cloud.cloudflare_status())

# winlink: freq/dist_km, not frequency/distance
fake(fleet, {'/packet/winlink/gateways': {'gateways': [
    {'callsign': 'KM6RTE-10', 'grid': 'DM13CS', 'dist_km': 8.5, 'freq': '145.090', 'modem': 1200, 'hours_since': 0},
    {'callsign': 'KE6VZZ-10', 'grid': 'DM13AR', 'dist_km': 8.0, 'freq': '144.970', 'modem': 1200, 'hours_since': 1}]}})
out = fleet.winlink_gateways()
check('winlink: real freq and distance', '144.970 MHz' in out and ' 8.0 km' in out and '?' not in out, out)
check('winlink: nearest first', out.index('KE6VZZ') < out.index('KM6RTE'), out)

# transcription: the saved on/off toggle is spelled out
fake(transcription, {'/transcriptions': {'status': {'mode': 'remote', 'model': 'parakeet/tdt-0.6b-v2',
                                                    'enabled': False, 'model_loaded': True}, 'results': []}})
check('transcription: paused is explicit', 'PAUSED' in transcription.transcription_status())

# new: offload workers + denoise
fake(workers, {
    '/workers': {'pool': {'denoise': {'workers': [{'name': 'macmini', 'url': 'tcp://10.0.0.9:9802', 'age_s': 3,
                                                  'meta': {'engine': 'dpdfnet', 'streams': 2, 'max_streams': 6,
                                                           'model_loaded': True, 'loadavg': 1.0, 'cpu_temp_c': 54}}],
                                      'stats': {'accepted': 1}},
                          'tts': {'workers': [], 'stats': {}}},
                 'denoise': [{'bus': 'bus_sdr1', 'worker': 'macmini', 'connected': True,
                              'counts': {'ok': 10, 'timeout': 0, 'down': 1}, 'rtt_ms_avg': 31.5, 'rtt_ms_max': 73.1}]},
    '/denoise/status': {'busses': [
        {'id': 'sdr1', 'enabled': True, 'engine': 'dpdfnet_remote', 'mix': 1.0, 'bypass_db': -60, 'sent': 5,
         'bypassed': 2, 'ms_avg': 30.4, 'local_cores': 0.0, 'remote': {'worker': 'macmini', 'connected': True}},
        {'id': 'aux', 'enabled': False}]}})
out = workers.offload_workers_status()
check('workers: worker line', 'macmini tcp://10.0.0.9:9802' in out and 'streams=2/6' in out, out)
check('workers: empty pool says where to look', 'none registered' in out, out)
check('workers: per-bus link', 'bus_sdr1: macmini connected  ok=10' in out, out)
out = workers.denoise_status()
check('denoise: remote bus', 'sdr1: dpdfnet_remote' in out and 'worker=macmini connected' in out, out)
check('denoise: off bus', 'aux: denoise off' in out, out)

print(f"\n{'FAIL' if FAIL else 'PASS'}: {len(FAIL)} failure(s)")
sys.exit(1 if FAIL else 0)
