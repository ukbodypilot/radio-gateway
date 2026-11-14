#!/usr/bin/env python3
"""rtl_airband stats: the generated configs ask for them, metrics exports them.

Added 2026-11-14 chasing 43 ms SDR drops: rtl_airband counts pulse
underflows, output overruns and input buffer overflows, but nothing ever
read them. Checks that every generated config sets stats_filepath and
buffer_ms, that the parser survives partial files, and that the collector
turns *_count(er) lines into rg_airband_*_total counters with an instance
label and skips stale files.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _hwstubs  # noqa: E402,F401
import metrics  # noqa: E402
import plugins.sdr as sdr  # noqa: E402
from plugins.sdr import SDRPlugin  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


# --- generated configs ---
p = SDRPlugin.__new__(SDRPlugin)
for a, v in (('_single_centerfreq', 446.7), ('_single_antenna', ''), ('_single_gain_mode', 'agc'),
             ('_single_correction', 0.0), ('_single_tau', 200), ('_single_rfgr', 4),
             ('_single_ifgr', 40), ('_single_agc_setpoint', -30), ('_single_bias_t', False),
             ('_single_rf_notch', False), ('_single_dab_notch', False),
             ('_single_iq_correction', True), ('_single_external_ref', False),
             ('_single_sample_rate', 2.0)):
    setattr(p, a, v)
p._single_channels = [{'freq': 446.76, 'modulation': 'nfm', 'label': 'PAPA'},
                      {'freq': 446.64, 'modulation': 'nfm', 'label': 'SPARC'}]
cap = {}
real_run = sdr.subprocess.run


def fake_run(args, input=None, **k):
    cap[args[-1]] = input.decode()
    return subprocess.CompletedProcess(args, 0, b'', b'')


sdr.subprocess.run = fake_run
try:
    p._write_config_single()
finally:
    sdr.subprocess.run = real_run
conf = cap.get(SDRPlugin.CONFIG_PATH_SINGLE, '')
check('single config sets stats_filepath', f'stats_filepath = "{SDRPlugin.AIRBAND_STATS_PATH}";' in conf)
check('single config sets stats_update_interval', 'stats_update_interval = 2.0;' in conf)
check('every pulse output gets buffer_ms + rate_match', conf.count('buffer_ms = ') == conf.count('rate_match = true;') == conf.count('type = "pulse"') == 2,
      f"{conf.count('buffer_ms = ')} vs {conf.count('type = \"pulse\"')}")
check('globals come before devices:', conf.index('stats_filepath') < conf.index('devices:'))
check('stats paths match the collector glob',
      all(os.path.basename(x).startswith('rtl_airband_stats') and x.endswith('.txt')
          and os.path.dirname(x) == os.path.dirname(metrics.AIRBAND_STATS_GLOB)
          for x in (SDRPlugin.AIRBAND_STATS_PATH, SDRPlugin.AIRBAND_STATS_PATH_SDR1,
                    SDRPlugin.AIRBAND_STATS_PATH_SDR2)))

# --- parser ---
SAMPLE = '''# HELP pulse_underflow_count Number of times a PulseAudio output stream has run dry.
# TYPE pulse_underflow_count counter
pulse_underflow_count{stream="SDR PAPA 446.760 MHz"}\t3
pulse_underflow_count{stream="SDR SPARC 446.640 MHz"}\t1

output_overrun_count{device="0"}\t0
buffer_overflow_count{device="0"}\t2
channel_dbfs_signal_level{freq="446.760",label="PAPA"}\t-31.5
channel_squelch_counter{freq="446.760",label="PAPA"}\t42
channel_flappy_counter{freq="446.760",label="PAPA"}\t7
channel_noise_level{freq="446.6'''          # truncated mid-line, as a reader might see it
parsed = metrics.parse_airband_stats(SAMPLE)
check('parser reads labelled values', parsed['pulse_underflow_count'][0] ==
      ({'stream': 'SDR PAPA 446.760 MHz'}, 3.0), str(parsed.get('pulse_underflow_count')))
check('parser skips a truncated last line', 'channel_noise_level' not in parsed)

# --- collector ---
d = tempfile.mkdtemp()
metrics.AIRBAND_STATS_GLOB = os.path.join(d, 'rtl_airband_stats*.txt')
with open(os.path.join(d, 'rtl_airband_stats.txt'), 'w') as f:
    f.write(SAMPLE)
stale = os.path.join(d, 'rtl_airband_stats_sdr1.txt')
with open(stale, 'w') as f:
    f.write('pulse_underflow_count{stream="old"}\t99\n')
old = time.time() - 3600
os.utime(stale, (old, old))

fams = {fam.name: fam for fam in metrics._AirbandCollector().collect()}
uf = fams.get('rg_airband_pulse_underflow')
check('underflow exported as a counter', uf is not None and uf.type == 'counter', str(list(fams)))
samples = {(s.labels.get('instance'), s.labels.get('stream')): s.value for s in (uf.samples if uf else [])
           if s.name.endswith('_total')}
check('values carried with instance=single',
      samples.get(('single', 'SDR PAPA 446.760 MHz')) == 3.0, str(samples))
check('stale file skipped', not any(k[1] == 'old' for k in samples))
check('signal level exported as a gauge',
      fams.get('rg_airband_channel_dbfs_signal_level') is not None
      and fams['rg_airband_channel_dbfs_signal_level'].type == 'gauge')
check('stats age reported', any(s.labels['instance'] == 'single' and s.value < 60
                                for s in fams['rg_airband_stats_age_seconds'].samples))

print('PASS' if not _fails else f'FAIL ({_fails})')
sys.exit(1 if _fails else 0)
