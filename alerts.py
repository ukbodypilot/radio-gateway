"""In-process alert engine — evaluates PromQL against the local Prometheus
and dispatches email notifications when rules trip.

Why in-process and not alertmanager? alertmanager is the standard answer but
adds a new daemon, config file, and mail bridge. The gateway already has
a working email path and a single-host scope, so a small polling loop
delivers the same outcome with a fraction of the moving parts. Swap to
alertmanager later if the rule set grows past a few dozen.

Design notes:
- Rules are plain dicts (name, query, condition, for, severity, message).
- "for" is a debounce: how long the rule must be true before firing.
- An "OK" notification is sent when a firing rule recovers.
- Each rule tracks state in-memory; a process restart re-arms but never
  duplicates an existing-firing alert because the recovery only fires on
  state transitions.
"""

import json
import threading
import time
import urllib.request


DEFAULT_RULES = [
    {
        'name': 'stream_down',
        'query': 'rate(rg_stream_bytes_sent_total[2m])',
        'condition': 'eq_zero',
        'for_seconds': 120,
        'severity': 'critical',
        'message': 'Broadcastify stream is not transmitting (0 bytes/s for 2 min)',
    },
    {
        'name': 'link_endpoint_down',
        'query': 'rg_link_endpoint_up',
        'condition': 'eq_zero',
        'for_seconds': 60,
        'severity': 'warning',
        'message': 'Link endpoint reports DOWN',
    },
    {
        'name': 'cpu_temp_hot',
        'query': 'rg_cpu_temp_c',
        'condition': 'gt_85',
        'for_seconds': 180,
        'severity': 'warning',
        'message': 'CPU temperature sustained above 85 °C',
    },
    {
        'name': 'denoise_p99_slow',
        'query': 'histogram_quantile(0.99, sum by (le, bus, engine) (rate(rg_denoise_apply_ms_bucket[5m])))',
        'condition': 'gt_50',
        'for_seconds': 300,
        'severity': 'warning',
        'message': 'Neural denoise p99 above 50 ms — bus tick at risk',
    },
    {
        'name': 'transcription_backlog',
        'query': 'rg_transcription_inflight',
        'condition': 'gt_5',
        'for_seconds': 300,
        'severity': 'warning',
        'message': 'Transcription backlog sustained (inflight > 5 for 5 min)',
    },
]


def _condition_matches(value: float, cond: str) -> bool:
    """Cheap, named conditions. Avoids parsing expressions from the rules dict."""
    if cond == 'eq_zero':
        return value == 0.0
    if cond == 'gt_50':
        return value > 50.0
    if cond == 'gt_85':
        return value > 85.0
    if cond == 'gt_5':
        return value > 5.0
    return False


def _query_prometheus(prom_url: str, query: str, timeout: float = 5.0):
    """Return the raw result list from a Prometheus instant query, or []."""
    body = json.dumps({'query': query}).encode()
    # Prom expects form-encoded, not JSON; build a GET with urlencoded.
    import urllib.parse
    url = f"{prom_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get('status') != 'success':
            return []
        return data['data'].get('result', [])
    except Exception:
        return []


class AlertEngine:
    """Polls Prom on an interval, fires/recovers rules, dispatches email."""

    def __init__(self, gateway, prom_url='http://127.0.0.1:9090/prometheus',
                 poll_interval=30, rules=None):
        self.gateway = gateway
        self.prom_url = prom_url
        self.poll_interval = poll_interval
        self.rules = list(rules) if rules is not None else list(DEFAULT_RULES)
        # state[rule_name][series_label_key] = {'firing': bool, 'first_seen': ts}
        self._state = {r['name']: {} for r in self.rules}
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='alert-engine')
        self._thread.start()
        print(f"  [Alerts] engine started ({len(self.rules)} rules, poll {self.poll_interval}s)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    # ── core loop ───────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._evaluate_once()
            except Exception as e:
                print(f"  [Alerts] eval error: {e}")
            self._stop.wait(self.poll_interval)

    def _evaluate_once(self):
        now = time.time()
        for rule in self.rules:
            results = _query_prometheus(self.prom_url, rule['query'])
            seen_keys = set()
            for series in results:
                try:
                    value = float(series['value'][1])
                except (KeyError, ValueError, TypeError):
                    continue
                key = _series_key(series.get('metric', {}))
                seen_keys.add(key)
                self._update_state(rule, key, series.get('metric', {}), value, now)
            # Recover series that have stopped appearing (e.g. label disappeared).
            stale = set(self._state[rule['name']]) - seen_keys
            for key in stale:
                self._recover(rule, key)

    def _update_state(self, rule, key, labels, value, now):
        st = self._state[rule['name']]
        cur = st.get(key, {'firing': False, 'first_seen': 0.0})
        if _condition_matches(value, rule['condition']):
            if not cur['firing']:
                if cur['first_seen'] == 0.0:
                    cur['first_seen'] = now
                if now - cur['first_seen'] >= rule['for_seconds']:
                    cur['firing'] = True
                    self._dispatch(rule, labels, value, fired=True)
        else:
            if cur['firing']:
                cur['firing'] = False
                cur['first_seen'] = 0.0
                self._dispatch(rule, labels, value, fired=False)
            else:
                cur['first_seen'] = 0.0
        st[key] = cur

    def _recover(self, rule, key):
        st = self._state[rule['name']]
        cur = st.get(key)
        if cur and cur.get('firing'):
            self._dispatch(rule, {'series': key}, None, fired=False)
        st.pop(key, None)

    # ── dispatch ────────────────────────────────────────────────────────
    def _dispatch(self, rule, labels, value, fired):
        label_str = ' '.join(f'{k}={v}' for k, v in labels.items()
                              if k != '__name__') or '(no labels)'
        if fired:
            text = f"[{rule['severity'].upper()}] {rule['name']} — {label_str}\n{rule['message']} (value={value})"
        else:
            text = f"[RECOVERED] {rule['name']} — {label_str}"
        print(f"  [Alerts] {text}")
        self._send_email(rule, text, fired)

    def _send_email(self, rule, text, fired):
        gw = self.gateway
        notifier = getattr(gw, 'email_notifier', None) if gw is not None else None
        if notifier is None or not notifier.is_configured():
            print(f"  [Alerts] NOT SENT (email not configured): {rule['name']}")
            return
        state = rule['severity'].upper() if fired else 'RECOVERED'
        try:
            notifier.send(f"[Gateway alert] {state}: {rule['name']}", text)
        except Exception as e:
            print(f"  [Alerts] Email send failed: {e}")


def _series_key(metric_labels):
    """Stable key for a series across polls — label set sorted by name."""
    return '|'.join(f'{k}={v}' for k, v in sorted(metric_labels.items())
                    if k != '__name__')
