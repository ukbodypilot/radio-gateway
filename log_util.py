"""Rate-limited logging for exceptions the gateway catches and carries on from.

Hundreds of `except Exception: pass` blocks used to hide failures entirely;
a PTT unkey that failed, or a metric that never updated, left no trace.
Those blocks now call log_exc(), which prints one line per call site and
then stays quiet for `interval` seconds, so a fault inside the 50 ms tick
loop costs a few lines an hour, not thousands.

    try:
        ...
    except Exception:
        log_exc()

Lines go to stdout, so LogWriter puts them in the web log and the log file.
"""

import os
import sys
import threading
import time

_INTERVAL = 300.0

_lock = threading.Lock()
_last = {}        # site -> monotonic time of last print
_suppressed = {}  # site -> count swallowed since last print


def log_exc(key=None, interval=_INTERVAL):
    """Log the exception being handled, at most once per `interval` per site.

    The site is the caller's file:line unless `key` is given. Safe to call
    from any thread; never raises.
    """
    try:
        exc = sys.exc_info()[1]
        if key is None:
            f = sys._getframe(1)
            key = f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno} in {f.f_code.co_name}"
        now = time.monotonic()
        with _lock:
            last = _last.get(key)
            if last is not None and now - last < interval:
                _suppressed[key] = _suppressed.get(key, 0) + 1
                return
            _last[key] = now
            skipped = _suppressed.pop(key, 0)
        more = f" (+{skipped} more since last report)" if skipped else ""
        what = f"{type(exc).__name__}: {exc}" if exc is not None else "no active exception"
        print(f"[swallowed] {key}: {what[:300]}{more}", flush=True)
    except Exception:
        pass
