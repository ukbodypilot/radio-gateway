#!/usr/bin/env python3
"""SDR sample rates: only offer rates rtl_airband can turn into clean audio.

rtl_airband steps round(sample_rate / 16000) input samples per NFM audio
sample, so 1 MS/s produced 15873 Hz audio (drops) and 500 kS/s 16129 Hz
(chops) -- both were on the menu (2026-11-05). Checks the validator, that
every offered rate passes it, that the page's menus match the plugin's
lists, that a bad rate is refused on apply, and that the config gets a
whole number of Hz.  Zero-IF rates (all but 2.0 in single mode) are refused
too: on the RSPduo they read ~42 dB down and gave no audio (2026-11-05).
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _hwstubs  # noqa: E402,F401
import plugins.sdr as sdr  # noqa: E402
from plugins.sdr import SDRPlugin, sample_rate_problem  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


print("Validator:")
check("single 2.0 MS/s accepted", sample_rate_problem(2.0) is None, str(sample_rate_problem(2.0)))
for r in (0.192, 0.384, 0.768, 2.048, 2.56, 4.0, 10.0):
    p = sample_rate_problem(r)
    check(f"single {r} MS/s rejected (zero-IF)", p is not None and 'zero-IF' in p, str(p))
for r, why in ((0.25, 'divide'), (0.5, 'divide'), (1.0, 'divide'), (10.66, 'divide'),
               (1.536, 'not supported'), (0.096 * 3, 'not supported'), (11.0, 'not supported')):
    p = sample_rate_problem(r)
    check(f"single {r} MS/s rejected ({why})", p is not None and why in p, str(p))
check("1 MS/s reason names the wrong audio rate", '15873 Hz' in (sample_rate_problem(1.0) or ''),
      str(sample_rate_problem(1.0)))
check("dual 2.0 accepted", sample_rate_problem(2.0, single=False) is None)
check("dual 1.0 rejected", sample_rate_problem(1.0, single=False) is not None)
check("dual 0.768 rejected (single-tuner only)", sample_rate_problem(0.768, single=False) is not None)

print("Offered lists:")
check("every SINGLE_SAMPLE_RATES entry is clean",
      all(sample_rate_problem(r) is None for r in SDRPlugin.SINGLE_SAMPLE_RATES))
check("every dual SAMPLE_RATES entry is clean",
      all(sample_rate_problem(r, single=False) is None for r in SDRPlugin.SAMPLE_RATES))

html = open(os.path.join(ROOT, 'web_pages', 'sdr.html')).read()


def options(sel_id):
    m = re.search(r'<select id="' + sel_id + r'"[^>]*>(.*?)</select>', html, re.S)
    return [float(v) for v in re.findall(r'value="([0-9.]+)"', m.group(1))] if m else None


check("single menu == SINGLE_SAMPLE_RATES", options('single-sample-rate') == SDRPlugin.SINGLE_SAMPLE_RATES,
      str(options('single-sample-rate')))
check("dual menu == SAMPLE_RATES", options('sdr-sr') == SDRPlugin.SAMPLE_RATES, str(options('sdr-sr')))
m = re.search(r'var rates = \[([^\]]*)\]', html)
check("Auto Center list == SINGLE_SAMPLE_RATES",
      m and [float(x) for x in m.group(1).split(',')] == SDRPlugin.SINGLE_SAMPLE_RATES)

print("Apply and config:")
p = SDRPlugin.__new__(SDRPlugin)
p._sdr_mode = 'single'
p._single_sample_rate = 2.0
restarts = []
p._restart_rtl_airband_single = lambda: restarts.append(1) or {'ok': True}
r = p._apply_single_settings({'cmd': 'single_tune', 'sample_rate': 1.0})
check("single apply refuses 1.0", not r['ok'] and 'divide' in r['error'], str(r))
check("refused rate not stored, no restart", p._single_sample_rate == 2.0 and not restarts)
r = p._apply_single_settings({'cmd': 'single_tune', 'sample_rate': 0.768})
check("single apply refuses 0.768 (zero-IF)", not r['ok'] and 'zero-IF' in r['error'], str(r))
r = p._apply_single_settings({'cmd': 'single_tune', 'sample_rate': 2.0})
check("single apply takes 2.0", r['ok'] and p._single_sample_rate == 2.0 and restarts, str(r))
check("config rate is whole Hz", p._single_rate_hz() == 2000000)
p._single_sample_rate = 0.384         # saved before zero-IF was refused
check("old saved zero-IF rate falls back to 2 MS/s", p._single_rate_hz() == 2000000)
p._single_sample_rate = 0.5           # e.g. an old saved value
check("old bad saved rate falls back to 2 MS/s", p._single_rate_hz() == 2000000)
p.sample_rate = 1.0
check("dual bad saved rate falls back to 2 MS/s", p._dual_rate_hz() == 2000000)
p.sample_rate = 2.56                  # dual writer clamps to 2.0
check("dual clamps 2.56 to 2 MS/s", p._dual_rate_hz() == 2000000)

# The config text uses the whole-Hz value.
cap = {}
real_run = sdr.subprocess.run


def fake_run(args, input=None, **k):
    cap['conf'] = input.decode()
    return subprocess.CompletedProcess(args, 0, b'', b'')


sdr.subprocess.run = fake_run
try:
    for a, v in (('_single_centerfreq', 446.7), ('_single_antenna', ''), ('_single_gain_mode', 'agc'),
                 ('_single_correction', 0.0), ('_single_tau', 200), ('_single_rfgr', 4),
                 ('_single_ifgr', 40), ('_single_agc_setpoint', -30), ('_single_bias_t', False),
                 ('_single_rf_notch', False), ('_single_dab_notch', False),
                 ('_single_iq_correction', True), ('_single_external_ref', False)):
        setattr(p, a, v)
    p._single_sample_rate = 2.0
    p._single_channels = [{'freq': 446.76, 'modulation': 'nfm'}]
    p._write_config_single()
finally:
    sdr.subprocess.run = real_run
check("single config writes sample_rate = 2000000;", 'sample_rate = 2000000;' in cap.get('conf', ''),
      re.search(r'sample_rate = [^;]*;', cap.get('conf', '')) and
      re.search(r'sample_rate = [^;]*;', cap['conf']).group(0))

print(f"\n{'ALL PASS' if _fails == 0 else f'{_fails} FAILED'}")
sys.exit(1 if _fails else 0)
