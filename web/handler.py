"""HTTP request handler and threaded server for the web UI.

Moved out of WebConfigServer.start(), where both classes used to be
defined inline. See GatewayRequestHandler for how routing works.
"""

import http.server
import os
import socketserver
import sys

from log_util import log_exc

# web_pages/ lives at the repo root, one level up from this package.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GatewayRequestHandler(http.server.BaseHTTPRequestHandler):
    """Request handler for the web UI, API and WebSocket endpoints.

    Routing is table-driven: _STATIC_PAGES serves web_pages/ files, and the
    _GET_* / _POST_* tables map a path to 'module:function' in one of the
    web_routes_* modules. Every route function is called as
    fn(request_handler, parent), where parent is the WebConfigServer.

    WebConfigServer.start() subclasses this to bind ``parent`` and
    ``password``; the class attributes here are only placeholders.
    """

    parent = None     # the WebConfigServer this handler serves
    password = ''     # WEB_CONFIG_PASSWORD; empty means no auth

    protocol_version = "HTTP/1.1"  # Required for WebSocket upgrade

    def end_headers(self):
        # For non-WebSocket responses, close connection to avoid
        # HTTP/1.1 keep-alive issues (no Content-Length on dynamic responses)
        if not self._upgrading_ws:
            self.send_header('Connection', 'close')
            self.close_connection = True
        super().end_headers()

    def setup(self):
        super().setup()
        self._upgrading_ws = False

    def log_message(self, format, *args):
        pass  # Suppress request logging

    def _check_auth(self):
        if not self.password:
            return True
        import base64
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            self._send_auth_required()
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            user, pw = decoded.split(':', 1)
            # compare_digest: constant-time, no timing side-channel
            import hmac
            if (hmac.compare_digest(user, 'admin')
                    and hmac.compare_digest(pw, self.password)):
                return True
        except Exception:
            log_exc()
        self._send_auth_required()
        return False

    def _send_auth_required(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Gateway Config"')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h1>401 Unauthorized</h1>')

    # Static page routes — path → filename in web_pages/
    _STATIC_PAGES = {
        '/': 'shell.html',
        '/dashboard': 'dashboard.html',
        '/dashboard/endpoints': 'dash_endpoints.html',
        '/dashboard/services': 'dash_services.html',
        '/dashboard/operate': 'dash_operate.html',
        '/controls': 'controls.html',
        '/sdr': 'sdr.html',
        '/d75': 'd75.html',
        '/ic7100': 'ic7100.html',
        '/kv4p': 'kv4p.html',
        '/radio': 'radio.html',
        '/monitor': 'monitor.html',
        '/recordings': 'recordings.html',
        '/recorder': 'recorder.html',
        '/transcribe': 'transcribe.html',
        '/txlog': 'txlog.html',
        '/logs': 'logs.html',
        '/gps': 'gps.html',
        '/repeaters': 'repeaters.html',
        '/aircraft': 'aircraft.html',
        '/routing': 'routing.html',
        '/packet': 'packet.html',
        '/processes': 'processes.html',
        '/gdrive': 'gdrive.html',
        '/manager': 'manager.html',
        '/grafana': 'grafana.html',
        '/endpoints/logs': 'endpoint_logs.html',
    }

    # ── Route tables ─────────────────────────────────────────────
    # Values are 'module:function' — g=web_routes_get,
    # s=web_routes_stream, p=web_routes_post, l=web_routes_loop.
    # Modules resolve at request time (imports are cached), same as
    # the old elif chain. Lookup order: EXACT (full path) →
    # QEXACT (query string stripped) → PREFIX (ordered startswith;
    # first hit wins, so keep more-specific prefixes first).

    _GET_EXACT = {
        '/status':                'g:handle_status',
        '/soundboard/categories': 'p:handle_soundboard_categories',
        '/announcer':             'p:handle_announcer',
        '/tts/engine':            'p:handle_tts_engine',
        '/metrics':               'g:handle_metrics',
        '/sinkstats':             'g:handle_sinkstats',
        '/sourcestats':           'g:handle_sourcestats',
        '/theme':                 'g:handle_theme',
        '/sysinfo':               'g:handle_sysinfo',
        '/catstatus':             'g:handle_catstatus',
        '/monitor-apk':           'g:handle_monitor_apk',
        '/d75status':             'g:handle_d75status',
        '/ic7100status':          'g:handle_ic7100status',
        '/api/processes':         'g:handle_api_processes',
        '/d75memlist':            'g:handle_d75memlist',
        '/sdrstatus':             'g:handle_sdrstatus',
        '/automationstatus':      'g:handle_automationstatus',
        '/adsbstatus':            'g:handle_adsbstatus',
        '/usbipstatus':           'g:handle_usbipstatus',
        '/gpsstatus':             'g:handle_gpsstatus',
        '/automationhistory':     'g:handle_automationhistory',
        '/ws_audio':              's:handle_ws_audio',
        '/ws_mic':                's:handle_ws_mic',
        '/ws_monitor':            's:handle_ws_monitor',
        '/ws/link':               's:handle_ws_link',
        '/stream':                's:handle_stream',
        '/tracestatus':           'g:handle_tracestatus',
        '/recordingslist':        'g:handle_recordingslist',
        '/adsb':                  'g:handle_adsb_proxy',
        '/pat':                   'g:handle_pat_proxy',
        '/routing/status':        'g:handle_routing_status',
        '/routing/levels':        'g:handle_routing_levels',
        '/packet/status':         'g:handle_packet_status',
        '/packet/packets':        'g:handle_packet_packets',
        '/packet/aprs_stations':  'g:handle_packet_aprs_stations',
        '/packet/bbs_buffer':     'g:handle_packet_bbs_buffer',
        '/packet/log':            'g:handle_packet_log',
        '/api/endpoint/version':  'g:handle_endpoint_version',
        '/api/endpoint/files':    'g:handle_endpoint_files',
        '/api/winclient/version': 'g:handle_winclient_version',
        '/api/winclient/files':   'g:handle_winclient_files',
        '/api/tunnel/link-url':   'g:handle_tunnel_link_url',
        '/api/gdrive/status':     'g:handle_gdrive_status',
        '/api/gdrive/files':      'g:handle_gdrive_files',
        '/manager/status':        'g:handle_manager_status',
        '/manager/reports':       'g:handle_manager_reports',
    }
    _GET_QEXACT = {
        '/config':                'g:handle_config',
        # Accepts ?instance=vhf|uhf for multi-radio routing.
        '/kv4pstatus':            'g:handle_kv4pstatus',
    }
    _GET_PREFIX = (
        ('/pages/',               'g:handle_pages'),
        ('/transcription/log',    'g:handle_transcription_log'),
        ('/transcript_search',    'g:handle_transcript_search'),
        ('/loopaudio',            'g:handle_loopaudio'),
        ('/transcriptions',       'g:handle_transcriptions'),
        ('/repeaterstatus',       'g:handle_repeaterstatus'),
        ('/logdata',              'g:handle_logdata'),
        ('/recordingsdownload',   'g:handle_recordingsdownload'),
        ('/adsb/',                'g:handle_adsb_proxy'),
        ('/pat/',                 'g:handle_pat_proxy'),
        ('/grafana/',             'g:handle_grafana_proxy'),
        ('/prometheus/',          'g:handle_prometheus_proxy'),
        ('/loop/',                'g:handle_loop_api'),
        ('/api/endpoint_logs',    'g:handle_endpoint_logs'),
        ('/packet/winlink/',      'g:handle_winlink_api'),
        ('/manager/doc',          'g:handle_manager_doc'),
        ('/manager/view',         'g:handle_manager_view'),
        ('/manager/edit',         'g:handle_manager_edit'),
    )

    def _dispatch(self, exact, qexact, prefixes):
        """Look up self.path in the route tables and run the handler.
        Returns True when a route matched, False to let the caller
        try plugin routes / the method's fallback."""
        import web_routes_get as _rg
        import web_routes_stream as _rs
        import web_routes_post as _rp
        import web_routes_loop as _rl
        _mods = {'g': _rg, 's': _rs, 'p': _rp, 'l': _rl}
        route = exact.get(self.path) or qexact.get(self.path.split('?', 1)[0])
        if route is None:
            for _pfx, _r in prefixes:
                if self.path.startswith(_pfx):
                    route = _r
                    break
        if route is None:
            return False
        _m, _fn = route.split(':', 1)
        getattr(_mods[_m], _fn)(self, self.parent)
        return True

    def _dispatch_plugin_route(self):
        """Plugin-contributed routes (web_routes() hook). Handler
        signature: handler(request_handler, parent). Same path map
        serves GET + POST; the handler inspects self.command."""
        _routes = getattr(self.parent.gateway, '_plugin_web_routes', None)
        _key = self.path.split('?', 1)[0]
        if _routes and _key in _routes:
            _routes[_key](self, self.parent)
            return True
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        import os

        # Serve static HTML pages
        if self.path in self._STATIC_PAGES:
            _fname = self._STATIC_PAGES[self.path]
            _p = os.path.join(_REPO_DIR, 'web_pages', _fname)
            try:
                with open(_p, 'rb') as _f:
                    _body = _f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(_body)
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        if self._dispatch(self._GET_EXACT, self._GET_QEXACT, self._GET_PREFIX):
            return
        if self._dispatch_plugin_route():
            return
        # Unmatched GET — send 404 so the browser doesn't hang
        # waiting for a response that never comes. Exact routes
        # don't match with a stray query string appended, and
        # that used to silently never respond.
        try:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')
        except Exception:
            log_exc()

    _POST_EXACT = {
        '/key':                        'p:handle_key',
        '/fart/preview':               'p:handle_fart_preview',
        '/fart/send':                  'p:handle_fart_send',
        '/transcription/query':        'p:handle_transcription_query',
        '/transcribe_config':          'p:handle_transcribe_config',
        '/transcribe_worker/register': 'p:handle_transcribe_worker_register',
        '/testloop':                   'p:handle_testloop',
        '/bgm':                        'p:handle_bgm',
        '/announcer':                  'p:handle_announcer',
        '/mixer':                      'p:handle_mixer',
        '/aitext':                     'p:handle_aitext',
        '/cw':                         'p:handle_cw',
        '/tts':                        'p:handle_tts',
        '/automationcmd':              'p:handle_automationcmd',
        '/proc_toggle':                'p:handle_proc_toggle',
        '/d75cmd':                     'p:handle_d75cmd',
        '/ic7100cmd':                  'p:handle_ic7100cmd',
        '/gpscmd':                     'p:handle_gpscmd',
        '/kv4pcmd':                    'p:handle_kv4pcmd',
        '/linkcmd':                    'p:handle_linkcmd',
        '/catcmd':                     'p:handle_catcmd',
        '/sdrcmd':                     'p:handle_sdrcmd',
        '/tracecmd':                   'p:handle_tracecmd',
        '/reboothost':                 'p:handle_reboothost',
        '/restartgateway':             'p:handle_restartgateway',
        '/refreshsounds':              'p:handle_refreshsounds',
        '/soundboard/categories':      'p:handle_soundboard_categories',
        '/tts/engine':                 'p:handle_tts_engine',
        '/recordingsdelete':           'p:handle_recordingsdelete',
        '/exit':                       'p:handle_exit',
        '/routing/cmd':                'p:handle_routing_cmd',
        # Exact match wins before the '/loop/' prefix below.
        '/loop/export':                'p:handle_loop_export',
        '/api/gdrive/publish-tunnel':  'p:handle_gdrive_publish_tunnel',
        '/pat':                        'g:handle_pat_proxy',
        '/manager/toggle':             'p:handle_manager_toggle',
        '/manager/config':             'p:handle_manager_config',
        '/manager/save':               'p:handle_manager_save',
        '/manager/run':                'p:handle_manager_run',
        '/manager/ack':                'p:handle_manager_ack',
    }
    _POST_QEXACT = {}
    _POST_PREFIX = (
        ('/loop/',       'l:handle_loop_post'),
        ('/pat/',        'g:handle_pat_proxy'),
        ('/grafana/',    'g:handle_grafana_proxy'),
        ('/prometheus/', 'g:handle_prometheus_proxy'),
        ('/packet/',     'p:handle_packet_cmd'),
    )

    def do_POST(self):
        if not self._check_auth():
            return
        if self._dispatch(self._POST_EXACT, self._POST_QEXACT, self._POST_PREFIX):
            return
        if self._dispatch_plugin_route():
            return
        # Config form submission. Only /config: this used to be the
        # fallback for EVERY unmatched POST, so a typo'd fetch or a
        # page for a disabled plugin rewrote gateway_config.txt.
        if self.path.split('?', 1)[0] == '/config':
            import web_routes_post as _rp
            _rp.handle_config_form(self, self.parent)
            return
        try:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')
        except Exception:
            log_exc()


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server with one daemon thread per request."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32  # default 5 is too low for concurrent dashboard clients

    def handle_error(self, request, client_address):
        # Default socketserver.handle_error dumps a 20-line stack
        # trace to stderr for any uncaught exception in a handler.
        # The single most common one is BrokenPipeError when a
        # browser closes the tab mid-response — benign, but
        # unreadable noise. Swallow that one; defer everything
        # else to the base implementation.
        _et, _ev, _ = sys.exc_info()
        if isinstance(_ev, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)
