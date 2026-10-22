"""Second AllStar USRP instance — CM5 node (node 683971).

Thin subclass of UsrpPlugin that:
  - reads USRP2_* config keys instead of USRP_*
  - registers web routes at /usrp2 instead of /usrp
  - uses plugin id 'usrp2' so it gets its own routing source/sink pair

Enable with:
    ENABLE_USRP2 = True
    USRP2_REMOTE_HOST = 192.168.2.139   # CM5 IP
    USRP2_REMOTE_PORT = 32002           # ASL on CM5 listens here
    USRP2_LISTEN_PORT = 34002           # gateway listens here
    USRP2_NODE = 683971
    USRP2_AMI_HOST = 192.168.2.139
    USRP2_AMI_PORT = 5038
    USRP2_AMI_USER = gateway
    USRP2_AMI_SECRET = <secret>
"""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import os
import sys

# usrp.py lives in the same plugins/ directory
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from usrp import UsrpPlugin  # noqa: E402

_SUFFIXES = (
    'REMOTE_HOST', 'REMOTE_PORT', 'LISTEN_PORT',
    'NODE', 'AMI_HOST', 'AMI_PORT', 'AMI_USER', 'AMI_SECRET',
)


class Usrp2Plugin(UsrpPlugin):
    PLUGIN_ID = 'usrp2'
    PLUGIN_NAME = 'AllStar 2 (USRP)'

    def setup(self, config, gateway=None):
        # Wrap config so parent setup() sees USRP_* keys sourced from USRP2_*
        class _Cfg:
            pass
        cfg = _Cfg()
        for k in dir(config):
            if not k.startswith('__'):
                try:
                    setattr(cfg, k, getattr(config, k))
                except Exception:
                    log_exc()
        for suffix in _SUFFIXES:
            v = getattr(config, f'USRP2_{suffix}', None)
            if v is not None:
                setattr(cfg, f'USRP_{suffix}', v)

        result = super().setup(cfg, gateway=gateway)
        if result:
            # Use a separate recent-nodes file so the two instances don't share history
            self._recent_path = os.path.join(
                os.path.dirname(_here), 'usrp2_recent.json')
            self._recent = self._load_recent()
            # Desired links ARE per-instance — AS1 and AS2 hold independent
            # link sets. The node address book (names) is deliberately shared;
            # see NODE_BOOK in usrp.py.
            self._desired_path = os.path.join(
                os.path.dirname(_here), 'usrp2_desired.json')
            self._desired = self._load_desired()
        return result

    def web_routes(self):
        return [
            ('/usrp2', self._http_panel),
            ('/usrp2/status', self._http_status),
            ('/usrp2/control', self._http_control),
        ]

    def _panel_rewrites(self):
        # Only the URL swaps — the parent handles __NODE__/__LABEL__ and the
        # response itself. This used to be a full _http_panel copy, which is
        # why a placeholder added upstream rendered literally here.
        return (("'/usrp/control'", "'/usrp2/control'"),
                ("'/usrp/status'",  "'/usrp2/status'"))
