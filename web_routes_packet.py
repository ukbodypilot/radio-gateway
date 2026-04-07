"""GET route handlers for Packet Radio and Winlink API."""

import json as json_mod
import os


def _pkt_json(handler, data):
    """Helper to send JSON response for packet endpoints."""
    body = json_mod.dumps(data).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


_winlink_gw_cache = {'data': None, 'time': 0}

def _winlink_gateways():
    """Return nearby Winlink RMS packet gateways from Pat's cached rmslist.json.

    Pat downloads the full gateway list from api.winlink.org and caches it
    locally.  We read the JSON directly — no CLI parsing, no API key needed.
    """
    import json as _json, time as _time, math as _math
    now = _time.time()
    if _winlink_gw_cache['data'] and now - _winlink_gw_cache['time'] < 3600:
        return _winlink_gw_cache['data']

    rmslist = os.path.expanduser('~/.local/share/pat/rmslist.json')
    if not os.path.exists(rmslist):
        return {"ok": False, "error": "rmslist.json not found — run pat rmslist first"}

    try:
        with open(rmslist) as f:
            data = _json.load(f)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Our position (from GPS config or default)
    my_lat, my_lon = 33.75, -117.87  # DM13do default
    max_dist_km = 100

    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = _math.radians(lat2 - lat1)
        dlon = _math.radians(lon2 - lon1)
        a = _math.sin(dlat/2)**2 + _math.cos(_math.radians(lat1)) * _math.cos(_math.radians(lat2)) * _math.sin(dlon/2)**2
        return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1-a))

    gateways = []
    for gw in data.get('Gateways', []):
        lat = gw.get('Latitude', 0)
        lon = gw.get('Longitude', 0)
        if not lat or not lon:
            continue
        dist = _haversine(my_lat, my_lon, lat, lon)
        if dist > max_dist_km:
            continue
        for ch in gw.get('GatewayChannels', []):
            modes = str(ch.get('SupportedModes', ''))
            if 'Packet' not in modes:
                continue
            freq_hz = ch.get('Frequency', 0)
            freq_mhz = f'{freq_hz / 1e6:.3f}' if freq_hz else '?'
            gateways.append({
                'callsign': gw.get('Callsign', '?'),
                'grid': ch.get('Gridsquare', ''),
                'lat': round(lat, 5),
                'lon': round(lon, 5),
                'dist_km': round(dist, 1),
                'freq': freq_mhz,
                'modem': int(ch.get('Baud', 1200)),
                'last_active': gw.get('LastStatus', ''),
                'hours_since': gw.get('HoursSinceStatus', -1),
                'range_mi': ch.get('RadioRange', ''),
                'antenna': ch.get('Antenna', ''),
                'hours': ch.get('OperatingHours', ''),
            })

    gateways.sort(key=lambda g: g['dist_km'])
    result = {"ok": True, "gateways": gateways}
    _winlink_gw_cache['data'] = result
    _winlink_gw_cache['time'] = now
    return result


def handle_winlink_api(handler, parent):
    """GET /packet/winlink/* — Winlink mailbox API."""
    import json as _json, email, glob as _glob
    path = handler.path.split('?')[0]
    params = {}
    if '?' in handler.path:
        for p in handler.path.split('?')[1].split('&'):
            k, _, v = p.partition('=')
            params[k] = v

    callsign = 'WA6NKR'
    if parent.gateway and parent.gateway.packet_plugin:
        callsign = parent.gateway.packet_plugin._callsign
    mailbox = os.path.expanduser(f'~/.local/share/pat/mailbox/{callsign}')

    if path == '/packet/winlink/messages':
        folder = params.get('folder', 'in')
        folder_path = os.path.join(mailbox, folder)
        messages = []
        if os.path.isdir(folder_path):
            for f in sorted(_glob.glob(os.path.join(folder_path, '*.b2f')), reverse=True):
                try:
                    with open(f, 'rb') as fh:
                        raw = fh.read()
                    msg = email.message_from_bytes(raw)
                    mid = os.path.basename(f).replace('.b2f', '')
                    messages.append({
                        'mid': mid,
                        'from': msg.get('From', ''),
                        'to': msg.get('To', ''),
                        'subject': msg.get('Subject', ''),
                        'date': msg.get('Date', ''),
                    })
                except Exception:
                    pass
        _pkt_json(handler, {"ok": True, "messages": messages})

    elif path == '/packet/winlink/read':
        mid = params.get('mid', '')
        folder = params.get('folder', 'in')
        msg_path = os.path.join(mailbox, folder, f'{mid}.b2f')
        if not os.path.exists(msg_path):
            _pkt_json(handler, {"ok": False, "error": "not found"})
            return
        try:
            with open(msg_path, 'rb') as fh:
                raw = fh.read()
            msg = email.message_from_bytes(raw)
            body = ''
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain':
                        body = part.get_payload(decode=True).decode('utf-8', errors='replace')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='replace')
            _pkt_json(handler, {
                "ok": True,
                "from": msg.get('From', ''),
                "to": msg.get('To', ''),
                "subject": msg.get('Subject', ''),
                "date": msg.get('Date', ''),
                "body": body,
            })
        except Exception as e:
            _pkt_json(handler, {"ok": False, "error": str(e)})
    elif path == '/packet/winlink/log':
        from web_routes_post import _winlink_log
        _pkt_json(handler, {"ok": True, "log": _winlink_log})

    elif path == '/packet/winlink/gateways':
        _pkt_json(handler, _winlink_gateways())

    else:
        _pkt_json(handler, {"ok": False, "error": "unknown endpoint"})


def handle_packet_status(handler, parent):
    """GET /packet/status"""
    gw = parent.gateway if parent else None
    if gw and gw.packet_plugin:
        _pkt_json(handler, gw.packet_plugin.get_status())
    else:
        _pkt_json(handler, {"mode": "disabled"})

def handle_packet_packets(handler, parent):
    """GET /packet/packets"""
    gw = parent.gateway if parent else None
    if gw and gw.packet_plugin:
        _pkt_json(handler, {"packets": list(gw.packet_plugin._decoded_packets)})
    else:
        _pkt_json(handler, {"packets": []})

def handle_packet_aprs_stations(handler, parent):
    """GET /packet/aprs_stations"""
    gw = parent.gateway if parent else None
    if gw and gw.packet_plugin:
        _pkt_json(handler, {"stations": gw.packet_plugin._aprs_stations})
    else:
        _pkt_json(handler, {"stations": {}})

def handle_packet_bbs_buffer(handler, parent):
    """GET /packet/bbs_buffer"""
    gw = parent.gateway if parent else None
    if gw and gw.packet_plugin:
        _pkt_json(handler, {"lines": list(gw.packet_plugin._bbs_buffer)})
    else:
        _pkt_json(handler, {"lines": []})

def handle_packet_log(handler, parent):
    """GET /packet/log"""
    gw = parent.gateway if parent else None
    if gw and gw.packet_plugin:
        _pkt_json(handler, {"lines": list(gw.packet_plugin._direwolf_log)})
    else:
        _pkt_json(handler, {"lines": []})
