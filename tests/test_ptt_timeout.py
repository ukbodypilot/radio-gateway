#!/usr/bin/env python3
"""Transmit time-out timer (PTT_TIMEOUT_SECONDS).

Before this, only the endpoint-side AIOC plugin, BusManager auto-PTT and the
web mic had a time-out. A manual PTT toggle for the TH-9800, D75, KV4P or
IC-7100 -- from the web UI, a keyboard command or an MCP tool -- stayed keyed
until something unkeyed it, so a closed browser tab or a dead MCP client left
the transmitter on.

Pins: expiry unkeys exactly once; a key-off before expiry cancels it; a key
held past expiry stays locked out while key-on requests keep coming, and a
fresh press after a pause works; 0 disables; and every PTT command sent
through GatewayLinkServer is covered.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway_link import PttTimeOutTimer, GatewayLinkServer   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def main():
    print("1. PttTimeOutTimer")
    fired = []
    tot = PttTimeOutTimer(0.2, fired.append, relock_gap=0.3)
    check("first key-on allowed", tot.allow_key('r'))
    time.sleep(0.35)
    check("expiry fires once", fired == ['r'], str(fired))
    check("held key-on refused after expiry", not tot.allow_key('r'))
    time.sleep(0.1)
    check("still refused while requests keep coming", not tot.allow_key('r'))
    time.sleep(0.4)
    check("fresh press after a pause allowed", tot.allow_key('r'))
    tot.released('r')
    time.sleep(0.3)
    check("key-off cancels the timer", fired == ['r'], str(fired))
    check("released key is allowed again", tot.allow_key('r'))
    tot.released('r')

    off = PttTimeOutTimer(0, fired.append)
    check("limit 0 never refuses", all(off.allow_key('x') for _ in range(3)))

    print("\n2. GatewayLinkServer covers endpoint PTT commands")
    sent, timed_out = [], []
    srv = GatewayLinkServer(port=0, ptt_timeout=0.2, on_ptt_timeout=timed_out.append)
    srv._send_to = lambda name, ftype, payload: sent.append((name, payload))
    srv.send_command_to('kv4p-vhf', {'cmd': 'ptt', 'state': True})
    time.sleep(0.35)
    check("forced unkey sent",
          any(b'"state": false' in p for n, p in sent if n == 'kv4p-vhf'), str(sent))
    check("on_ptt_timeout called", timed_out == ['kv4p-vhf'], str(timed_out))
    n_before = len(sent)
    srv.send_command_to('kv4p-vhf', {'cmd': 'ptt', 'state': True})
    check("held re-key is dropped", len(sent) == n_before)
    srv.send_command_to('kv4p-vhf', {'cmd': 'ptt', 'state': False})
    srv.send_command_to('kv4p-vhf', {'cmd': 'ptt', 'state': True})
    check("key-off then key-on goes through", sent[-1][1].find(b'true') != -1, str(sent[-1]))
    srv.send_command_to('kv4p-vhf', {'cmd': 'ptt', 'state': False})
    srv.send_command_to('kv4p-vhf', {'cmd': 'freq', 'freq': 146.52})
    check("non-PTT commands untouched", b'freq' in sent[-1][1])

    print("\n3. TH-9800 plugin: every keying path ends in _set_ptt")
    import threading
    from plugins.th9800 import TH9800Plugin
    applied = []
    p = object.__new__(TH9800Plugin)
    p._ptt_lock = threading.RLock()
    p._ptt_active = False
    p._ptt_ok = True
    p._ptt_change_time = 0
    p._ptt_method = 'relay'
    p._ptt_via_relay = lambda on: applied.append(on)
    p._tot = PttTimeOutTimer(0.2, p._tot_expired, relock_gap=0.3)
    p._set_ptt(True)
    time.sleep(0.35)
    check("time-out unkeyed the radio", applied == [True, False] and not p._ptt_active,
          str(applied))
    check("held key refused", p._set_ptt(True) is False and applied == [True, False])
    p._set_ptt(False)
    check("after a key-off, keying works", p._set_ptt(True) is not False and applied[-1] is True,
          str(applied))
    p._set_ptt(False)

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
