"""GET route handlers extracted from web_server.py."""

import json as json_mod
import os
import time


def handle_status(handler, parent):
    """GET /status"""
    # JSON status endpoint for live dashboard
    data = parent.gateway.get_status_dict() if parent.gateway else {}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_theme(handler, parent):
    """GET /theme"""
    # Theme config JSON — used by static HTML pages
    t = parent._get_theme()
    gw_name = str(getattr(parent.config, 'GATEWAY_NAME', '') or '').strip()
    data = {**t, 'gateway_name': gw_name}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'max-age=60')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_pages(handler, parent):
    """GET /pages/*"""
    # Serve static HTML files from web_pages/ directory (and subdirs like fonts/)
    import os as _os
    _page_name = handler.path[7:]  # strip '/pages/'
    if '..' in _page_name:
        handler.send_response(403)
        handler.end_headers()
        return
    _page_dir = _os.path.realpath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'web_pages'))
    _page_path = _os.path.realpath(_os.path.join(_page_dir, _page_name))
    if not _page_path.startswith(_page_dir + _os.sep):
        handler.send_response(403)
        handler.end_headers()
        return
    if _os.path.isfile(_page_path):
        _ct = 'text/html; charset=utf-8'
        if _page_name.endswith('.css'):
            _ct = 'text/css'
        elif _page_name.endswith('.js'):
            _ct = 'application/javascript'
        elif _page_name.endswith('.woff2'):
            _ct = 'font/woff2'
        try:
            with open(_page_path, 'rb') as _f:
                _body = _f.read()
            handler.send_response(200)
            handler.send_header('Content-Type', _ct)
            handler.send_header('Content-Length', str(len(_body)))
            handler.end_headers()
            handler.wfile.write(_body)
        except BrokenPipeError:
            pass
    else:
        handler.send_response(404)
        handler.end_headers()


def handle_sysinfo(handler, parent):
    """GET /sysinfo"""
    # System status JSON endpoint
    data = parent._get_sysinfo()
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_catstatus(handler, parent):
    """GET /catstatus"""
    # JSON radio CAT state endpoint
    data = {'connected': False, 'cat_enabled': False}
    if parent.gateway:
        data['cat_enabled'] = getattr(parent.gateway.config, 'ENABLE_CAT_CONTROL', False)
        if parent.gateway.cat_client:
            data = parent.gateway.cat_client.get_radio_state()
            data['cat_enabled'] = True
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_monitor_apk(handler, parent):
    """GET /monitor-apk"""
    import os
    apk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'room-monitor.apk')
    if os.path.exists(apk_path):
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/vnd.android.package-archive')
        handler.send_header('Content-Disposition', 'attachment; filename="room-monitor.apk"')
        handler.send_header('Content-Length', str(os.path.getsize(apk_path)))
        handler.end_headers()
        with open(apk_path, 'rb') as f:
            handler.wfile.write(f.read())
    else:
        handler.send_response(404)
        handler.end_headers()
        handler.wfile.write(b'APK not built yet')


def handle_transcription_log(handler, parent):
    """GET /transcription/log?limit=100&offset=0"""
    limit, offset = 100, 0
    try:
        qs = handler.path.split('?', 1)[1] if '?' in handler.path else ''
        for part in qs.split('&'):
            if part.startswith('limit='):
                limit = max(1, min(500, int(part[6:])))
            elif part.startswith('offset='):
                offset = max(0, int(part[7:]))
    except Exception:
        pass
    rows = []
    tl = getattr(parent.gateway, 'transcription_log', None) if parent.gateway else None
    if tl:
        rows = tl.get_recent(limit=limit, offset=offset)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'rows': rows}).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_transcriptions(handler, parent):
    """GET /transcriptions"""
    # Return recent transcriptions as JSON
    since = 0
    try:
        qs = handler.path.split('?', 1)[1] if '?' in handler.path else ''
        for part in qs.split('&'):
            if part.startswith('since='):
                since = float(part[6:])
    except Exception:
        pass
    data = {'results': [], 'status': {}}
    if parent.gateway and parent.gateway.transcriber:
        data['results'] = parent.gateway.transcriber.get_results(since=since)
        data['status'] = parent.gateway.transcriber.get_status()
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_d75status(handler, parent):
    """GET /d75status"""
    # D75 CAT state endpoint — reads from link endpoint or legacy plugin
    data = {'connected': False, 'd75_enabled': False, 'tcp_connected': False,
            'serial_connected': False, 'btstart_in_progress': False,
            'service_running': False, 'status_detail': ''}
    if parent.gateway:
        gw = parent.gateway
        data['d75_enabled'] = getattr(gw.config, 'ENABLE_D75', False)
        data['d75_mode'] = 'disabled'

        # Check for D75 link endpoint first
        _link = getattr(gw, 'link_server', None)
        _d75_ep = None
        if _link:
            for ep_name, ep_src in gw.link_endpoints.items():
                if getattr(ep_src, 'plugin_type', None) == 'd75':
                    _d75_ep = ep_name
                    break

        if _d75_ep:
            # D75 is a link endpoint
            data['d75_enabled'] = True
            data['connected'] = True
            data['tcp_connected'] = True
            data['serial_connected'] = True
            data['d75_mode'] = 'link_endpoint'
            data['service_running'] = True
            data['status_detail'] = ''
            data['link_endpoint'] = _d75_ep
            # Forward all endpoint status fields into data
            _ep_status = getattr(gw, '_link_last_status', {}).get(_d75_ep, {})
            for _k, _v in _ep_status.items():
                if _k not in ('band', 'plugin', 'mac', 'uptime'):
                    data[_k] = _v
            # Band info: convert from array to band_0/band_1 with fixups
            _mm_names = {0: 'VFO', 1: 'Memory', 2: 'Call', 3: 'DV'}
            _bands = _ep_status.get('band', [{}, {}])
            for _bi, _bkey in enumerate(('band_0', 'band_1')):
                if _bi < len(_bands) and isinstance(_bands[_bi], dict):
                    _bd = dict(_bands[_bi])
                    if 'memory_mode' in _bd and isinstance(_bd['memory_mode'], int):
                        _bd['memory_mode'] = _mm_names.get(_bd['memory_mode'], '?')
                    # Map s_meter → signal (D75 page expects 'signal')
                    if 's_meter' in _bd and 'signal' not in _bd:
                        _bd['signal'] = _bd['s_meter']
                    data[_bkey] = _bd
            # Audio level from the link audio source
            for _src_name, _src in getattr(gw, 'link_endpoints', {}).items():
                if getattr(_src, 'plugin_type', None) == 'd75':
                    data['audio_connected'] = True
                    data['audio_level'] = getattr(_src, 'audio_level', 0)
                    data['audio_boost'] = int(getattr(_src, 'audio_boost', 1.0) * 100)
                    break

        # Build status detail
        if not _d75_ep:
            if not data['d75_enabled']:
                data['status_detail'] = 'D75 disabled in config'
            else:
                data['status_detail'] = 'D75 not connected (no link endpoint or plugin)'
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_kv4pstatus(handler, parent):
    """GET /kv4pstatus"""
    # KV4P status JSON endpoint — served by KV4PPlugin
    data = {'connected': False, 'kv4p_enabled': False}
    if parent.gateway:
        data['kv4p_enabled'] = getattr(parent.gateway.config, 'ENABLE_KV4P', False)
        _kv4p_p = getattr(parent.gateway, 'kv4p_plugin', None)
        if _kv4p_p:
            data.update(_kv4p_p.get_status())
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_d75memlist(handler, parent):
    """GET /d75memlist"""
    # D75 memory channel list — scans channels via endpoint or legacy plugin
    import json as json_mod
    import threading as _thr
    channels = []
    gw = parent.gateway
    # Check for D75 link endpoint
    _link = getattr(gw, 'link_server', None) if gw else None
    _d75_ep = None
    if _link:
        for ep_name, ep_src in gw.link_endpoints.items():
            if getattr(ep_src, 'plugin_type', None) == 'd75':
                _d75_ep = ep_name
                break
    if _d75_ep and _link:
        # Send memscan command and wait for ACK with results
        _scan_result = [None]
        _scan_evt = _thr.Event()
        _orig_ack = getattr(gw, '_link_scan_ack', None)
        def _scan_ack(name, ack):
            if name == _d75_ep and ack.get('cmd') == 'memscan':
                _scan_result[0] = ack.get('result', {})
                _scan_evt.set()
        # Temporarily hook the ACK callback
        gw._link_scan_ack = _scan_ack
        # Store original on_ack and wrap it
        _orig_on_ack = _link._on_ack
        def _wrapped_ack(name, ack):
            if _orig_on_ack:
                _orig_on_ack(name, ack)
            if gw._link_scan_ack:
                gw._link_scan_ack(name, ack)
        _link._on_ack = _wrapped_ack
        try:
            print(f"  [D75 Scan] Sending memscan to {_d75_ep}...")
            _link.send_command_to(_d75_ep, {'cmd': 'memscan'})
            _scan_evt.wait(timeout=60)  # scan can take a while
            print(f"  [D75 Scan] Got result: {len(_scan_result[0].get('channels', [])) if _scan_result[0] else 'timeout'}")
            if _scan_result[0] and _scan_result[0].get('ok'):
                channels = _scan_result[0].get('channels', [])
        finally:
            _link._on_ack = _orig_on_ack
            gw._link_scan_ack = None
        data = channels
        try:
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Cache-Control', 'no-cache')
            handler.end_headers()
            handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
        except BrokenPipeError:
            pass
        return
    # No link endpoint — return empty channel list
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(channels).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_sdrstatus(handler, parent):
    """GET /sdrstatus"""
    # SDR status JSON endpoint — served by SDRPlugin
    data = {}
    _sdr_p = getattr(parent.gateway, 'sdr_plugin', None) if parent.gateway else None
    if _sdr_p:
        data = _sdr_p.get_status()
    else:
        data = {'error': 'SDR plugin not available', 'process_alive': False}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_automationstatus(handler, parent):
    """GET /automationstatus"""
    # Automation engine status JSON
    data = {}
    if parent.gateway and parent.gateway.automation_engine:
        data = parent.gateway.automation_engine.get_status()
    else:
        data = {'enabled': False}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_adsbstatus(handler, parent):
    """GET /adsbstatus"""
    # ADS-B component status and live aircraft data
    import json as json_mod
    import subprocess
    data = {'enabled': False, 'dump1090': False, 'web': False, 'fr24feed': False,
            'aircraft': 0, 'messages': 0, 'messages_rate': 0.0}
    data['enabled'] = bool(getattr(parent.config, 'ENABLE_ADSB', False)) if parent.config else False
    if data['enabled']:
        # Service liveness checks
        for _svc, _key in [('dump1090-fa', 'dump1090'), ('dump1090-fa-web', 'web'), ('fr24feed', 'fr24feed')]:
            try:
                _r = subprocess.run(['systemctl', 'is-active', _svc],
                                    capture_output=True, text=True, timeout=2)
                data[_key] = (_r.stdout.strip() == 'active')
            except Exception:
                data[_key] = False
        # Live aircraft data from dump1090 JSON output
        try:
            import json as _jm
            with open('/run/dump1090-fa/aircraft.json') as _af:
                _ac = _jm.load(_af)
            _now = _ac.get('now', 0)
            data['aircraft'] = sum(1 for a in _ac.get('aircraft', []) if a.get('seen', 999) < 60)
            _msgs = _ac.get('messages', 0)
            # Compute message rate using previous sample
            _prev = getattr(parent, '_adsb_prev_msgs', None)
            _prev_t = getattr(parent, '_adsb_prev_t', 0)
            import time as _time_mod
            _now_t = _time_mod.monotonic()
            if _prev is not None and _now_t > _prev_t:
                _dt = _now_t - _prev_t
                data['messages_rate'] = round((_msgs - _prev) / _dt, 1)
            data['messages'] = _msgs
            parent._adsb_prev_msgs = _msgs
            parent._adsb_prev_t = _now_t
        except Exception:
            pass
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_telegramstatus(handler, parent):
    """GET /telegramstatus"""
    import json as json_mod, os as _os
    data = {'enabled': False, 'bot_running': False, 'bot_username': '',
            'tmux_session': '', 'tmux_active': False,
            'messages_today': 0, 'last_message_time': None,
            'last_message_text': '', 'last_reply_time': None}
    data['enabled'] = bool(getattr(parent.config, 'ENABLE_TELEGRAM', False)) if parent.config else False
    # Always check bot process and token — even if disabled in config
    try:
        import subprocess as _sp
        _r = _sp.run(['systemctl', 'is-active', 'telegram-bot'],
                     capture_output=True, text=True, timeout=2)
        data['bot_running'] = _r.stdout.strip() == 'active'
    except Exception:
        data['bot_running'] = False
    _token = str(getattr(parent.config, 'TELEGRAM_BOT_TOKEN', '')) if parent.config else ''
    data['token_set'] = bool(_token and len(_token) > 10)
    if data['enabled']:
        status_file = getattr(parent.config, 'TELEGRAM_STATUS_FILE', '/tmp/tg_status.json')
        try:
            with open(status_file) as _sf:
                _sd = json_mod.load(_sf)
            data.update({k: _sd[k] for k in data if k in _sd and k != 'bot_running'})
        except Exception:
            pass
    # Always check tmux session
    session = getattr(parent.config, 'TELEGRAM_TMUX_SESSION', 'claude-gateway') if parent.config else 'claude-gateway'
    data['tmux_session'] = session
    try:
        _r = _sp.run(['tmux', 'has-session', '-t', session],
                     capture_output=True, timeout=2)
        data['tmux_active'] = (_r.returncode == 0)
    except Exception:
        data['tmux_active'] = False
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_usbipstatus(handler, parent):
    """GET /usbipstatus"""
    import json as json_mod
    if parent.usbip_manager:
        data = parent.usbip_manager.get_status()
    else:
        data = {'enabled': bool(getattr(parent.config, 'ENABLE_USBIP', False)),
                'server': str(getattr(parent.config, 'USBIP_SERVER', '')),
                'server_reachable': False, 'devices': [],
                'last_error': '', 'last_check': None}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_gpsstatus(handler, parent):
    """GET /gpsstatus"""
    import json as json_mod
    gw = parent.gateway
    if gw and gw.gps_manager:
        data = gw.gps_manager.get_status()
    else:
        data = {'enabled': bool(getattr(parent.config, 'ENABLE_GPS', False)),
                'connected': False, 'fix': 0, 'satellites': []}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_repeaterstatus(handler, parent):
    """GET /repeaterstatus"""
    import json as json_mod
    from urllib.parse import urlparse, parse_qs
    gw = parent.gateway
    if gw and gw.repeater_manager:
        qs = parse_qs(urlparse(handler.path).query)
        band = qs.get('band', [''])[0]
        radius = float(qs.get('radius', [0])[0] or 0) or None
        operational = qs.get('operational', ['true'])[0].lower() != 'false'
        reps = gw.repeater_manager.get_nearby(
            band=band or None, radius_km=radius, operational_only=operational)
        status = gw.repeater_manager.get_status()
        data = {'ok': True, 'status': status, 'repeaters': reps}
    else:
        data = {'ok': True, 'status': {'enabled': False}, 'repeaters': []}
    try:
        resp = json_mod.dumps(data).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(resp)
    except BrokenPipeError:
        pass


def handle_automationhistory(handler, parent):
    """GET /automationhistory"""
    # Automation task history JSON
    data = []
    if parent.gateway and parent.gateway.automation_engine:
        data = parent.gateway.automation_engine.get_history()
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_tracestatus(handler, parent):
    """GET /tracestatus"""
    _gw = parent.gateway
    _ts = {'audio_trace': False, 'watchdog_trace': False}
    if _gw:
        _ts['audio_trace'] = getattr(_gw, '_trace_recording', False)
        _ts['watchdog_trace'] = getattr(_gw, '_watchdog_active', False)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(_ts).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_logdata(handler, parent):
    """GET /logdata"""
    # Log data API — returns JSON lines after given sequence number
    import urllib.parse as _up
    qs = _up.parse_qs(_up.urlparse(handler.path).query)
    after = int(qs.get('after', ['0'])[0])
    writer = parent.gateway._status_writer if parent.gateway else None
    lines = []
    last_seq = after
    if writer:
        for seq, text in writer.get_log_lines(after_seq=after, limit=500):
            lines.append(text)
            last_seq = seq
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'seq': last_seq, 'lines': lines}).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_recordingslist(handler, parent):
    """GET /recordingslist"""
    # JSON list of recording files
    import json as json_mod
    rec_dir = ''
    if parent.gateway and parent.gateway.automation_engine:
        rec_dir = parent.gateway.automation_engine.recorder._dir
    files = []
    if rec_dir and os.path.isdir(rec_dir):
        for fname in sorted(os.listdir(rec_dir), reverse=True):
            fpath = os.path.join(rec_dir, fname)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            # Parse metadata from filename: RADIO_FREQ_DATE_TIME_LABEL.ext
            parts = fname.rsplit('.', 1)
            ext = parts[1] if len(parts) > 1 else ''
            name_parts = parts[0].split('_')
            radio = name_parts[0] if name_parts else ''
            freq = name_parts[1].replace('MHz', '') if len(name_parts) > 1 else ''
            # Date is YYYY-MM-DD format
            date_str = name_parts[2] if len(name_parts) > 2 else ''
            time_str = name_parts[3] if len(name_parts) > 3 else ''
            label = '_'.join(name_parts[4:]) if len(name_parts) > 4 else ''
            files.append({
                'name': fname,
                'size': stat.st_size,
                'radio': radio,
                'freq': freq,
                'date': date_str,
                'time': time_str,
                'label': label,
                'ext': ext,
            })
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(files).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_recordingsdownload(handler, parent):
    """GET /recordingsdownload"""
    # Download a recording file
    import urllib.parse
    qs = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(qs)
    fname = params.get('file', [''])[0]
    rec_dir = ''
    if parent.gateway and parent.gateway.automation_engine:
        rec_dir = parent.gateway.automation_engine.recorder._dir
    if not fname or not rec_dir:
        handler.send_response(400)
        handler.end_headers()
        return
    # Sanitize filename — no path traversal
    fname = os.path.basename(fname)
    fpath = os.path.join(rec_dir, fname)
    if not os.path.isfile(fpath):
        handler.send_response(404)
        handler.end_headers()
        return
    try:
        ext = fname.rsplit('.', 1)[-1].lower()
        ctype = {'mp3': 'audio/mpeg', 'wav': 'audio/wav'}.get(ext, 'application/octet-stream')
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        handler.send_header('Content-Length', str(os.path.getsize(fpath)))
        handler.end_headers()
        with open(fpath, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except BrokenPipeError:
        pass


def handle_pat_proxy(handler, parent):
    """GET /pat or /pat/* — reverse proxy to Pat Winlink web UI."""
    import urllib.request as _ureq
    import urllib.error as _uerr
    _pat_port = 8082
    if parent.gateway and parent.gateway.packet_plugin:
        _pat_port = getattr(parent.gateway.packet_plugin, '_pat_port', 8082)
    # Strip /pat prefix — /pat → /ui, /pat/ui → /ui, /pat/dist/x → /dist/x
    _proxy_path = handler.path[4:] or '/ui'
    if _proxy_path == '/' or _proxy_path == '':
        _proxy_path = '/ui'
    # Pass query string through
    if '?' in handler.path:
        _qs = handler.path.split('?', 1)[1]
        _proxy_path = _proxy_path.split('?')[0] + '?' + _qs
    _target = f'http://127.0.0.1:{_pat_port}{_proxy_path}'
    try:
        # Read POST body if present
        _content_len = int(handler.headers.get('Content-Length', 0))
        _body = handler.rfile.read(_content_len) if _content_len > 0 else None
        _method = handler.command or 'GET'
        _req = _ureq.Request(_target, data=_body, method=_method)
        for _h in ('Accept', 'Accept-Language', 'Accept-Encoding', 'Content-Type'):
            _v = handler.headers.get(_h)
            if _v:
                _req.add_header(_h, _v)
        with _ureq.urlopen(_req, timeout=10) as _resp:
            _body = _resp.read()
            _ctype = _resp.headers.get('Content-Type', 'application/octet-stream')
            # Inject <base href="/pat/"> into HTML so relative asset paths resolve
            # through the proxy (dist/js/app.js → /pat/dist/js/app.js)
            if 'text/html' in _ctype and b'<head>' in _body:
                _body = _body.replace(b'<head>', b'<head><base href="/pat/">', 1)
            handler.send_response(200)
            handler.send_header('Content-Type', _ctype)
            handler.send_header('Content-Length', str(len(_body)))
            handler.end_headers()
            handler.wfile.write(_body)
    except _uerr.HTTPError as _e:
        try:
            handler.send_response(_e.code)
            handler.end_headers()
        except BrokenPipeError:
            pass
    except Exception:
        _err = b'<html><body style="background:#1a1a1a;color:#888;text-align:center;padding-top:80px"><h3>Pat not running</h3><p>Switch to Winlink mode to start Pat.</p></body></html>'
        try:
            handler.send_response(503)
            handler.send_header('Content-Type', 'text/html')
            handler.send_header('Content-Length', str(len(_err)))
            handler.end_headers()
            handler.wfile.write(_err)
        except BrokenPipeError:
            pass


def handle_adsb_proxy(handler, parent):
    """GET /adsb or /adsb/*"""
    # Reverse proxy to dump1090-fa web interface
    import urllib.request as _ureq
    import urllib.error as _uerr
    _adsb_port = int(getattr(parent.config, 'ADSB_PORT', 30080)) if parent.config else 30080
    # Strip /adsb prefix — /adsb → /, /adsb/foo → /foo
    _proxy_path = handler.path[5:] or '/'
    _target = f'http://127.0.0.1:{_adsb_port}{_proxy_path}'
    try:
        _req = _ureq.Request(_target)
        # Forward useful request headers
        for _h in ('Accept', 'Accept-Language', 'If-Modified-Since', 'If-None-Match', 'Accept-Encoding'):
            _v = handler.headers.get(_h)
            if _v:
                _req.add_header(_h, _v)
        with _ureq.urlopen(_req, timeout=10) as _resp:
            _body = _resp.read()
            _ctype = _resp.headers.get('Content-Type', 'application/octet-stream')
            _etag = _resp.headers.get('ETag', '')
            _lmod = _resp.headers.get('Last-Modified', '')
            handler.send_response(200)
            handler.send_header('Content-Type', _ctype)
            handler.send_header('Content-Length', str(len(_body)))
            if _etag:
                handler.send_header('ETag', _etag)
            if _lmod:
                handler.send_header('Last-Modified', _lmod)
            handler.end_headers()
            handler.wfile.write(_body)
    except _uerr.HTTPError as _e:
        try:
            handler.send_response(_e.code)
            handler.end_headers()
        except BrokenPipeError:
            pass
    except Exception:
        _adsb_err = (
            f'<html><head><meta charset="utf-8"></head><body style="background:#1a1a1a;color:#e0e0e0;'
            f'font-family:-apple-system,sans-serif;text-align:center;padding-top:80px">'
            f'<h2 style="color:#e74c3c">ADS-B Unavailable</h2>'
            f'<p style="margin-top:12px">dump1090-fa is not running on port {_adsb_port}</p>'
            f'<p style="margin-top:8px;color:#888">Start it with:</p>'
            f'<code style="display:block;margin-top:8px;color:#2ecc71">sudo systemctl start dump1090-fa</code>'
            f'</body></html>'
        ).encode('utf-8')
        try:
            handler.send_response(503)
            handler.send_header('Content-Type', 'text/html; charset=utf-8')
            handler.send_header('Content-Length', str(len(_adsb_err)))
            handler.end_headers()
            handler.wfile.write(_adsb_err)
        except BrokenPipeError:
            pass


def handle_config(handler, parent):
    """GET /config"""
    # Config editor
    html = parent._generate_html()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.end_headers()
    handler.wfile.write(html.encode('utf-8'))


def handle_routing_status(handler, parent):
    """GET /routing/status"""
    # Return current routing state for the UI
    data = parent._get_routing_status()
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_routing_levels(handler, parent):
    """GET /routing/levels"""
    # Return RX and TX audio levels separately
    data = {}
    gw = parent.gateway
    if gw:
        # RX levels (sources)
        if gw.sdr_plugin:
            _sdr = gw.sdr_plugin
            data['sdr'] = _sdr.audio_level
            if getattr(_sdr, '_tuner1', None):
                data['sdr1'] = _sdr._tuner1.audio_level
            if getattr(_sdr, '_tuner2', None):
                data['sdr2'] = _sdr._tuner2.audio_level
        if gw.kv4p_plugin:
            data['kv4p'] = gw.kv4p_plugin.audio_level
        if getattr(gw, 'th9800_plugin', None):
            data['aioc'] = gw.th9800_plugin.audio_level
        if getattr(gw, 'playback_source', None):
            data['playback'] = getattr(gw.playback_source, 'audio_level', 0)
        if getattr(gw, 'loop_playback_source', None):
            data['loop_playback'] = getattr(gw.loop_playback_source, 'audio_level', 0)
        if getattr(gw, 'announce_input_source', None):
            data['announce'] = getattr(gw.announce_input_source, 'audio_level', 0)
        if getattr(gw, 'web_mic_source', None):
            data['webmic'] = gw.web_mic_source.audio_level if gw.web_mic_source.client_connected else 0
        if getattr(gw, 'web_monitor_source', None):
            data['monitor'] = gw.web_monitor_source.audio_level
        if getattr(gw, 'mumble_source', None):
            data['mumble_rx'] = gw.mumble_source.audio_level
        else:
            data['mumble_rx'] = getattr(gw, 'rx_audio_level', 0)
        if getattr(gw, 'remote_audio_source', None):
            data['remote_audio'] = gw.remote_audio_source.audio_level
        # Link endpoint RX + TX levels — all dynamic via source_id/sink_id
        for _ln, _ls in gw.link_endpoints.items():
            _ep_id = getattr(_ls, 'source_id', None)
            _sink_id = getattr(_ls, 'sink_id', None)
            if _ep_id:
                _ls.audio_level = max(0, int(getattr(_ls, 'audio_level', 0) * 0.8))
                data[_ep_id] = _ls.audio_level
            if _sink_id:
                data[_sink_id] = gw._link_tx_levels.get(_ln, 0)
        # TX levels (built-in radios)
        if gw.kv4p_plugin:
            data['kv4p_tx'] = getattr(gw.kv4p_plugin, 'tx_audio_level', 0)
        if getattr(gw, 'th9800_plugin', None):
            data['aioc_tx'] = getattr(gw.th9800_plugin, 'tx_audio_level', 0)
        # Passive sinks — only show level if connected to a bus
        _all_sinks = getattr(gw, '_bus_sinks', {})
        _all_connected = set()
        for _sinks in _all_sinks.values():
            _all_connected.update(_sinks)
        # Decay all sink/source levels on each poll (200ms interval)
        gw.speaker_audio_level = max(0, int(getattr(gw, 'speaker_audio_level', 0) * 0.8))
        gw.stream_audio_level = max(0, int(getattr(gw, 'stream_audio_level', 0) * 0.8))
        gw.mumble_tx_level = max(0, int(getattr(gw, 'mumble_tx_level', 0) * 0.8))
        if getattr(gw, 'mumble_source', None):
            gw.mumble_source.audio_level = max(0, int(gw.mumble_source.audio_level * 0.8))
        if gw.kv4p_plugin:
            gw.kv4p_plugin.tx_audio_level = max(0, int(getattr(gw.kv4p_plugin, 'tx_audio_level', 0) * 0.8))
        # Decay link endpoint TX levels
        for _ln, _ls in gw.link_endpoints.items():
            _ls.tx_audio_level = max(0, int(getattr(_ls, 'tx_audio_level', 0) * 0.8))
            if _ln in gw._link_tx_levels:
                gw._link_tx_levels[_ln] = max(0, int(gw._link_tx_levels[_ln] * 0.8))
        if getattr(gw, 'th9800_plugin', None):
            gw.th9800_plugin.tx_audio_level = max(0, int(getattr(gw.th9800_plugin, 'tx_audio_level', 0) * 0.8))
        # Report sink levels — 0 when disconnected so bars clear
        data['speaker'] = gw.speaker_audio_level if 'speaker' in _all_connected else 0
        data['broadcastify'] = gw.stream_audio_level if 'broadcastify' in _all_connected else 0
        data['mumble'] = gw.mumble_tx_level if 'mumble' in _all_connected else 0
        data['transcription'] = getattr(gw, 'transcription_audio_level', 0) if 'transcription' in _all_connected else 0
        gw.transcription_audio_level = max(0, int(getattr(gw, 'transcription_audio_level', 0) * 0.8))
        gw.remote_audio_tx_level = max(0, int(getattr(gw, 'remote_audio_tx_level', 0) * 0.8))
        data['remote_audio_tx'] = getattr(gw, 'remote_audio_tx_level', 0) if 'remote_audio_tx' in _all_connected else 0
        gw.nul_audio_level = max(0, int(getattr(gw, 'nul_audio_level', 0) * 0.8))
        data['nul'] = getattr(gw, 'nul_audio_level', 0) if 'nul' in _all_connected else 0
        # Bus output levels
        _bm = getattr(gw, 'bus_manager', None)
        if _bm:
            for _bid, _blv in _bm._bus_levels.items():
                data['bus_' + _bid] = _blv
        # Primary listen bus level (managed by BusManager)
        if _bm and _bm.listen_bus:
            _listen_id = getattr(_bm, '_listen_bus_id', 'listen')
            data['bus_' + _listen_id] = _bm._bus_levels.get(_listen_id, 0)
        # PTT state for TX sinks — use pre-computed sink_id
        _ptt = {}
        for _pn, _pa in gw._link_ptt_active.items():
            _ep_src = gw.link_endpoints.get(_pn)
            _sink_id = getattr(_ep_src, 'sink_id', None) if _ep_src else None
            if _sink_id:
                _ptt[_sink_id] = _pa
        if gw.kv4p_plugin and hasattr(gw.kv4p_plugin, 'ptt_active'):
            _ptt['kv4p_tx'] = gw.kv4p_plugin.ptt_active
        if getattr(gw, 'th9800_plugin', None) and hasattr(gw.th9800_plugin, 'ptt_active'):
            _ptt['aioc_tx'] = gw.th9800_plugin.ptt_active
        data['_ptt'] = _ptt
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(data).encode('utf-8'))
    except BrokenPipeError:
        pass


def handle_voice_status(handler, parent):
    """GET /voice/status"""
    import subprocess
    _vr_target = os.environ.get('TMUX_TARGET', 'claude-voice')
    result = subprocess.run(
        ['tmux', 'has-session', '-t', _vr_target],
        capture_output=True,
    )
    alive = result.returncode == 0
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps({'tmux_target': _vr_target, 'session_alive': alive}).encode())


def handle_voice_view(handler, parent):
    """GET /voice/view"""
    import subprocess
    tmux_target = os.environ.get('TMUX_TARGET', 'claude-voice')
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', tmux_target, '-p'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        handler.send_response(503)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'error': f"tmux session '{tmux_target}' not found"}).encode())
    else:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'content': result.stdout}).encode())


# ── Packet Radio GET handlers ──

# Packet/Winlink handlers moved to web_routes_packet.py
# Loop recorder handlers moved to web_routes_loop.py
from web_routes_packet import _pkt_json, handle_winlink_api, handle_packet_status
from web_routes_packet import handle_packet_packets, handle_packet_aprs_stations
from web_routes_packet import handle_packet_bbs_buffer, handle_packet_log
from web_routes_loop import handle_loop_api, handle_loop_post


# ── Endpoint self-update API ──

_ENDPOINT_FILES = [
    'gateway_link.py',
    'link_endpoint.py',
    'd75_link_plugin.py',
    'remote_bt_proxy.py',
]


def handle_endpoint_version(handler, parent):
    """GET /api/endpoint/version — hash of endpoint files for update check."""
    import hashlib
    _dir = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    files = {}
    for fname in _ENDPOINT_FILES:
        for path in [os.path.join(_dir, fname),
                     os.path.join(_dir, 'tools', fname),
                     os.path.join(_dir, 'scripts', fname)]:
            if os.path.isfile(path):
                with open(path, 'rb') as f:
                    content = f.read()
                h.update(content)
                files[fname] = len(content)
                break
    data = {'version': h.hexdigest()[:16], 'files': files}
    body = json_mod.dumps(data).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_endpoint_files(handler, parent):
    """GET /api/endpoint/files — download all endpoint files as JSON bundle."""
    import base64
    _dir = os.path.dirname(os.path.abspath(__file__))
    bundle = {}
    for fname in _ENDPOINT_FILES:
        for path in [os.path.join(_dir, fname),
                     os.path.join(_dir, 'tools', fname),
                     os.path.join(_dir, 'scripts', fname)]:
            if os.path.isfile(path):
                with open(path, 'rb') as f:
                    bundle[fname] = base64.b64encode(f.read()).decode()
                break
    body = json_mod.dumps(bundle).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ── Google Drive + Tunnel URL API ──

def handle_tunnel_link_url(handler, parent):
    """GET /api/tunnel/link-url — return current WS link URL via tunnel."""
    gw = parent.gateway if parent else None
    tunnel = getattr(gw, 'cloudflare_tunnel', None) if gw else None
    url = tunnel.get_url() if tunnel else None
    data = {}
    if url:
        data['url'] = url
        data['ws_link'] = url.replace('https://', 'wss://').replace('http://', 'ws://').rstrip('/') + '/ws/link'
    else:
        data['url'] = None
        data['ws_link'] = None
    body = json_mod.dumps(data).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_gdrive_status(handler, parent):
    """GET /api/gdrive/status — Google Drive integration status."""
    gw = parent.gateway if parent else None
    gdrive = getattr(gw, 'gdrive', None) if gw else None
    if gdrive:
        data = gdrive.get_status()
    else:
        data = {'configured': False}
    body = json_mod.dumps(data).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_gdrive_files(handler, parent):
    """GET /api/gdrive/files — list files in the gateway Drive folder."""
    gw = parent.gateway if parent else None
    gdrive = getattr(gw, 'gdrive', None) if gw else None
    if not gdrive:
        body = json_mod.dumps({'files': [], 'error': 'not configured'}).encode()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    try:
        files = gdrive.list_files()
    except Exception as e:
        files = []
    body = json_mod.dumps({'files': files}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

