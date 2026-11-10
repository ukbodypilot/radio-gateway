#!/usr/bin/env python3
"""Host memory / network / ADS-B gauges that back the dashboard trend charts.

They are sampled on scrape (metrics.render), not from a web handler, so they
keep moving with no dashboard open. Rates need two scrapes.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401

import metrics  # noqa: E402

_fails = 0


def check(name, ok, detail=''):
    global _fails
    if not ok:
        _fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def val(name):
    return metrics.REGISTRY.get_sample_value(name)


NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999999 1 0 0 0 0 0 0 999999 1 0 0 0 0 0 0
enp2s0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0
"""

with tempfile.TemporaryDirectory() as d:
    mem, net, adsb = (os.path.join(d, n) for n in ('meminfo', 'netdev', 'aircraft.json'))
    metrics._MEMINFO_PATH, metrics._NETDEV_PATH, metrics._ADSB_JSON_PATH = mem, net, adsb

    def write(rx, tx, msgs, seen):
        with open(mem, 'w') as f:
            f.write('MemTotal:       16000 kB\nMemFree:  100 kB\nMemAvailable:   12000 kB\n')
        with open(net, 'w') as f:
            f.write(NETDEV.format(rx=rx, tx=tx))
        with open(adsb, 'w') as f:
            json.dump({'messages': msgs, 'aircraft': [{'seen': s} for s in seen]}, f)

    clock = [100.0]
    metrics.time.monotonic = lambda: clock[0]

    write(1000, 5000, 200, [1, 5, 300])
    metrics._refresh_dashboard_history()
    check('memory percent = (total - available) / total', val('rg_host_mem_pct') == 25.0, val('rg_host_mem_pct'))
    check('aircraft counts only those seen in the last 60 s', val('rg_adsb_aircraft') == 2, val('rg_adsb_aircraft'))
    check('first scrape only primes the rates', val('rg_host_net_rx_bps') == 0 and val('rg_adsb_msg_rate') == 0)

    clock[0] += 10
    write(1000 + 20000, 5000 + 4000, 200 + 450, [1])
    metrics._refresh_dashboard_history()
    check('rx rate averaged over the scrape interval, lo excluded', val('rg_host_net_rx_bps') == 2000, val('rg_host_net_rx_bps'))
    check('tx rate', val('rg_host_net_tx_bps') == 400, val('rg_host_net_tx_bps'))
    check('ADS-B message rate', val('rg_adsb_msg_rate') == 45.0, val('rg_adsb_msg_rate'))
    check('aircraft follows the file', val('rg_adsb_aircraft') == 1)

    clock[0] += 10
    write(50, 60, 10, [])          # counters reset (interface / dump1090 restart)
    metrics._refresh_dashboard_history()
    check('a counter reset reads as zero, never negative',
          val('rg_host_net_rx_bps') == 0 and val('rg_host_net_tx_bps') == 0 and val('rg_adsb_msg_rate') == 0)

    os.remove(adsb)
    os.remove(mem)
    clock[0] += 10
    metrics._refresh_dashboard_history()          # must not raise
    check('missing sources keep the previous value', val('rg_host_mem_pct') == 25.0 and val('rg_adsb_aircraft') == 0)

    metrics.link_endpoint_ping_ms.labels(endpoint='EP1').set(1.5)
    body = metrics.render()[0].decode()
    check('all new series are exported', all(n in body for n in (
        'rg_host_mem_pct', 'rg_host_net_rx_bps', 'rg_host_net_tx_bps', 'rg_adsb_aircraft',
        'rg_adsb_msg_rate', 'rg_link_endpoint_ping_ms{endpoint="EP1"}')))

print('ALL PASS' if not _fails else f'{_fails} FAILED')
sys.exit(1 if _fails else 0)
