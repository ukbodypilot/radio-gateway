"""GET/POST route handlers for Loop Recorder API."""

import json as json_mod
import os


def handle_loop_api(handler, parent):
    """GET /loop/* — Loop recorder API dispatcher."""
    import urllib.parse
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path
    params = urllib.parse.parse_qs(parsed.query)
    gw = parent.gateway if parent else None
    lr = getattr(gw, 'loop_recorder', None) if gw else None

    if path == '/loop/buses':
        if not lr:
            _loop_json(handler, [])
            return
        # Pass enabled bus IDs so buses with no data yet still appear
        _enabled = set()
        _bm = getattr(gw, 'bus_manager', None)
        if _bm:
            for _bid, _bcfg in _bm._bus_config.items():
                if _bcfg.get('loop', False):
                    _enabled.add(_bid)
        buses = lr.get_buses(enabled_bus_ids=_enabled)
        # Only return buses that are enabled or actively recording
        buses = [b for b in buses if b.get('active') or b['id'] in _enabled]
        # Add display names and upstream-source freqs from the routing
        # config. Reuse the transcriber's _resolve_freq_tag helper — it
        # already handles sdr1/sdr2, th9800 via CAT client, kv4p, and
        # link endpoints. The routing config lists sources by id
        # (e.g. ['aioc'] or ['sdr1','sdr2']), which is exactly what the
        # resolver wants.
        _bus_names = {}
        _bus_sources = {}
        try:
            import json as _json
            with open(_bm._config_path) as _f:
                for _b in _json.load(_f).get('busses', []):
                    _bus_names[_b['id']] = _b.get('name', _b['id'])
                    _bus_sources[_b['id']] = _b.get('sources', []) or []
        except Exception:
            pass
        try:
            from transcriber import _resolve_freq_tag
        except Exception:
            _resolve_freq_tag = None
        for b in buses:
            b['name'] = _bus_names.get(b['id'], b['id'])
            b['freq'] = ''
            if not _resolve_freq_tag:
                continue
            _freqs = []
            for _sid in _bus_sources.get(b['id'], []):
                _f = _resolve_freq_tag(gw, _sid)
                if _f and _f not in _freqs:
                    _freqs.append(_f)
            if _freqs:
                b['freq'] = '/'.join(_freqs)
        _loop_json(handler, buses)

    elif path == '/loop/waveform':
        if not lr:
            _loop_json(handler, {"error": "loop recorder not available"}, 503)
            return
        bus = params.get('bus', [''])[0]
        start = params.get('start', [''])[0]
        end = params.get('end', [''])[0]
        if not bus or not start or not end:
            _loop_json(handler, {"ok": False, "error": "missing bus, start, or end param"}, 400)
            return
        try:
            data = lr.get_waveform(bus, float(start), float(end))
        except Exception as e:
            _loop_json(handler, {"ok": False, "error": str(e)}, 500)
            return
        _loop_json(handler, data)

    elif path == '/loop/segments':
        if not lr:
            _loop_json(handler, {"segments": []})
            return
        bus = params.get('bus', [''])[0]
        start = params.get('start', [''])[0]
        end = params.get('end', [''])[0]
        if bus and start and end:
            try:
                segs = lr.get_segments(bus, float(start), float(end))
                _loop_json(handler, {"segments": [
                    {"start": s["start"], "end": s["end"], "size": s["size"]}
                    for s in segs
                ]})
            except Exception as e:
                _loop_json(handler, {"segments": [], "error": str(e)})
        else:
            _loop_json(handler, {"segments": [], "error": "missing params"})
        return

    elif path == '/loop/play':
        if not lr:
            handler.send_error(503, 'Loop recorder not available')
            return
        bus = params.get('bus', [''])[0]
        start = params.get('start', [''])[0]
        end = params.get('end', [''])[0]
        if not bus or not start or not end:
            handler.send_error(400, 'Missing bus, start, or end param')
            return
        try:
            start_f, end_f = float(start), float(end)
        except ValueError:
            handler.send_error(400, 'start and end must be numeric')
            return
        segments = lr.get_segments(bus, start_f, end_f)
        if not segments:
            handler.send_error(404, 'No segments found')
            return
        temp_path = None
        try:
            # Always use export_range to trim audio to exact start point
            serve_path = lr.export_range(bus, start_f, end_f, fmt='mp3')
            if not serve_path:
                handler.send_error(500, 'Export failed')
                return
            temp_path = serve_path
            file_size = os.path.getsize(serve_path)
            # Support Range requests for seeking
            range_hdr = handler.headers.get('Range')
            if range_hdr and range_hdr.startswith('bytes='):
                range_spec = range_hdr[6:]
                start_byte = 0
                end_byte = file_size - 1
                if '-' in range_spec:
                    parts = range_spec.split('-', 1)
                    if parts[0]:
                        start_byte = int(parts[0])
                    if parts[1]:
                        end_byte = int(parts[1])
                end_byte = min(end_byte, file_size - 1)
                content_len = end_byte - start_byte + 1
                handler.send_response(206)
                handler.send_header('Content-Type', 'audio/mpeg')
                handler.send_header('Content-Range', f'bytes {start_byte}-{end_byte}/{file_size}')
                handler.send_header('Content-Length', str(content_len))
                handler.send_header('Accept-Ranges', 'bytes')
                handler.end_headers()
                try:
                    with open(serve_path, 'rb') as f:
                        f.seek(start_byte)
                        remaining = content_len
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            handler.wfile.write(chunk)
                            remaining -= len(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass
            else:
                handler.send_response(200)
                handler.send_header('Content-Type', 'audio/mpeg')
                handler.send_header('Content-Length', str(file_size))
                handler.send_header('Accept-Ranges', 'bytes')
                handler.end_headers()
                try:
                    with open(serve_path, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            handler.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass
        except BrokenPipeError:
            pass
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    elif path == '/loop/playback/status':
        lps = getattr(gw, 'loop_playback_source', None) if gw else None
        if lps:
            _loop_json(handler, lps.get_status_dict())
        else:
            _loop_json(handler, {"playing": False})

    else:
        _loop_json(handler, {"ok": False, "error": "unknown endpoint"}, 404)


def handle_loop_post(handler, parent):
    """POST /loop/* — Loop recorder bulk operations."""
    import urllib.parse
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path
    gw = parent.gateway if parent else None
    lr = getattr(gw, 'loop_recorder', None) if gw else None

    if path == '/loop/playback':
        lps = getattr(gw, 'loop_playback_source', None) if gw else None
        if not lps:
            _loop_json(handler, {"ok": False, "error": "loop playback not available"}, 503)
            return
        content_len = int(handler.headers.get('Content-Length', 0))
        try:
            body = json_mod.loads(handler.rfile.read(content_len)) if content_len else {}
        except (json_mod.JSONDecodeError, ValueError):
            _loop_json(handler, {"ok": False, "error": "invalid JSON"}, 400)
            return
        action = body.get('action', '')
        if action == 'play':
            bus_id = body.get('bus', '')
            try:
                start = float(body.get('start', 0))
            except (TypeError, ValueError):
                _loop_json(handler, {"ok": False, "error": "invalid start time"}, 400)
                return
            if not bus_id or not start:
                _loop_json(handler, {"ok": False, "error": "missing bus or start"}, 400)
                return
            ok = lps.play(bus_id, start)
            _loop_json(handler, {"ok": ok})
        elif action == 'stop':
            lps.stop()
            _loop_json(handler, {"ok": True})
        else:
            _loop_json(handler, {"ok": False, "error": "unknown action"}, 400)

    elif path == '/loop/delete_all':
        if not lr:
            _loop_json(handler, {"ok": False, "error": "loop recorder not available"}, 503)
            return
        count = lr.delete_all()
        _loop_json(handler, {"ok": True, "deleted": count})

    elif path == '/loop/download_all':
        if not lr:
            handler.send_error(503, 'Loop recorder not available')
            return
        zip_path = lr.zip_all()
        if not zip_path:
            handler.send_error(404, 'No recordings to download')
            return
        try:
            file_size = os.path.getsize(zip_path)
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip')
            handler.send_header('Content-Disposition', 'attachment; filename="loop_recordings.zip"')
            handler.send_header('Content-Length', str(file_size))
            handler.end_headers()
            with open(zip_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except BrokenPipeError:
            pass
        finally:
            try:
                os.unlink(zip_path)
            except Exception:
                pass

    elif path == '/loop/archive_all':
        if not lr:
            _loop_json(handler, {"ok": False, "error": "loop recorder not available"}, 503)
            return
        archive_path = lr.archive_all()
        if not archive_path:
            _loop_json(handler, {"ok": False, "error": "no recordings to archive"}, 404)
            return
        _loop_json(handler, {"ok": True, "path": archive_path})

    else:
        _loop_json(handler, {"ok": False, "error": "unknown endpoint"}, 404)


def _loop_json(handler, data, status=200):
    """Helper to send JSON response for loop recorder endpoints."""
    body = json_mod.dumps(data).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
