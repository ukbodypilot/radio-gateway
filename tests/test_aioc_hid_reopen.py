#!/usr/bin/env python3
"""AIOC PTT survives the device re-enumerating (2026-10-25, 2026-10-27).

The AIOC drops off the USB bus and returns as a new hidraw node. The endpoint kept its old HID
handle, every PTT write failed with 'No such device', and only a restart fixed it while audio
recovered by itself. Pins: a failed write closes the dead handle, reopens once and retries; no
handle at all also reopens; and when the reopen fails PTT reports an error instead of raising.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway_link import AIOCPlugin   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


class Hid:
    def __init__(self, dead):
        self.dead, self.writes, self.closed = dead, [], False

    def write(self, data):
        if self.dead:
            raise OSError(19, 'No such device')
        self.writes.append(data)

    def close(self):
        self.closed = True


def plugin(hid, fresh):
    p = object.__new__(AIOCPlugin)
    p._hid, p._ptt_on, p._ptt_channel, p._status_dirty = hid, False, 3, False
    p._ptt_hw_lock = threading.Lock()
    p._take_prekey = lambda: b''
    opened = []

    def open_hid():
        opened.append(1)
        p._hid = fresh
        return fresh is not None
    p._open_hid = open_hid
    return p, opened


def main():
    dead, fresh = Hid(True), Hid(False)
    p, opened = plugin(dead, fresh)
    r = p._set_ptt(True)
    check("stale handle: PTT ok after reopen", r.get('ok') is True, str(r))
    check("dead handle closed", dead.closed)
    check("reopened once, write landed on the new handle", len(opened) == 1 and len(fresh.writes) == 1)

    p, opened = plugin(Hid(False), fresh)
    check("healthy handle: no reopen", p._set_ptt(True)['ok'] is True and not opened)

    fresh2 = Hid(False)
    p, opened = plugin(None, fresh2)
    check("no handle: reopens and keys", p._set_ptt(True)['ok'] is True and len(opened) == 1)

    p, opened = plugin(Hid(True), None)
    r = p._set_ptt(True)
    check("reopen fails: error reported, not raised", r.get('ok') is False and 'error' in r, str(r))
    check("failed key-on leaves PTT off", p._ptt_on is False)

    print(f"\n{'FAILED' if failed else 'ok'}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
