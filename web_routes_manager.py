"""POST handlers for the Fleet Manager UI: toggle, run, save docs, ack."""

"""POST route handlers extracted from web_server.py."""

import json as json_mod



def handle_manager_toggle(handler, parent):
    """POST /manager/toggle  — {enabled: true/false}"""
    gw = parent.gateway if parent else None
    eng = getattr(gw, 'manager_engine', None) if gw else None
    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = json_mod.loads(handler.rfile.read(length))
        enabled = bool(body.get('enabled', False))
        if eng:
            eng.set_enabled(enabled)
        resp = json_mod.dumps({'ok': True, 'enabled': enabled}).encode()
    except Exception as e:
        resp = json_mod.dumps({'ok': False, 'error': str(e)}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)

def handle_manager_config(handler, parent):
    """POST /manager/config  — {daily_time: "HH:MM"}"""
    gw = parent.gateway if parent else None
    eng = getattr(gw, 'manager_engine', None) if gw else None
    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = json_mod.loads(handler.rfile.read(length))
        if eng and 'daily_time' in body:
            eng.set_daily_time(body['daily_time'])
        if eng and 'check_interval_hours' in body:
            eng.set_check_interval(int(body['check_interval_hours']))
        resp = json_mod.dumps({'ok': True}).encode()
    except Exception as e:
        resp = json_mod.dumps({'ok': False, 'error': str(e)}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)

def handle_manager_save(handler, parent):
    """POST /manager/save  — {doc: "hourly", content: "..."}"""
    gw = parent.gateway if parent else None
    eng = getattr(gw, 'manager_engine', None) if gw else None
    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = json_mod.loads(handler.rfile.read(length))
        doc     = body.get('doc', '')
        content = body.get('content', '')
        if eng and eng.save_doc(doc, content):
            resp = json_mod.dumps({'ok': True}).encode()
        else:
            resp = json_mod.dumps({'ok': False, 'error': 'unknown doc or engine unavailable'}).encode()
    except Exception as e:
        resp = json_mod.dumps({'ok': False, 'error': str(e)}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)

def handle_manager_run(handler, parent):
    """POST /manager/run  — {task: "hourly"|"daily"}"""
    gw = parent.gateway if parent else None
    eng = getattr(gw, 'manager_engine', None) if gw else None
    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = json_mod.loads(handler.rfile.read(length))
        task = body.get('task', 'hourly')
        if eng:
            eng.run_now(task)
            resp = json_mod.dumps({'ok': True, 'task': task}).encode()
        else:
            resp = json_mod.dumps({'ok': False, 'error': 'manager engine not available'}).encode()
    except Exception as e:
        resp = json_mod.dumps({'ok': False, 'error': str(e)}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)

def handle_manager_ack(handler, parent):
    """POST /manager/ack  — clear unread alerts"""
    gw = parent.gateway if parent else None
    eng = getattr(gw, 'manager_engine', None) if gw else None
    if eng:
        eng.acknowledge()
    resp = json_mod.dumps({'ok': True}).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)
