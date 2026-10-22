"""POST handlers for automation tasks and sound mapping refresh."""

"""POST route handlers extracted from web_server.py."""

import json as json_mod
import os



def handle_automationcmd(handler, parent):
    """POST /automationcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        cmd = data.get('cmd', '')
        engine = parent.gateway.automation_engine if parent.gateway else None
        if not engine:
            result = {'ok': False, 'error': 'Automation not enabled'}
        elif cmd == 'trigger':
            task_name = data.get('task', '')
            if engine.trigger(task_name):
                result = {'ok': True, 'triggered': task_name}
            else:
                result = {'ok': False, 'error': f'Task not found: {task_name}'}
        elif cmd == 'reload':
            engine.reload_scheme()
            result = {'ok': True, 'tasks': len(engine._tasks)}
        elif cmd == 'stop_recording':
            path = engine.recorder.stop()
            result = {'ok': True, 'path': path}
        else:
            result = {'ok': False, 'error': f'Unknown command: {cmd}'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode())
    return

def handle_tts_engine(handler, parent):
    """GET/POST /tts/engine

    GET  -> available engines, which is active, and whether each is importable.
    POST -> {"engine": "edge"} swaps the live engine and persists it.

    Swapping is hot: apply_tts_engine() rebuilds the backend in place, so the
    next /status poll serves that engine's voice list and the dropdowns
    repopulate themselves. No gateway restart.
    """
    import gateway_setup
    gw = parent.gateway

    def state(extra=None):
        active = getattr(gw, '_tts_backend', '') if gw else ''
        engines = []
        for name in gateway_setup.TTS_ENGINES:
            # Report importability so the UI can grey out an engine whose
            # package is missing rather than letting it be selected and fail.
            try:
                if name == 'kokoro':
                    import kokoro_onnx  # noqa: F401
                elif name == 'edge':
                    import edge_tts     # noqa: F401
                else:
                    import gtts         # noqa: F401
                ok = True
            except Exception:
                ok = False
            engines.append({'value': name,
                            'label': gateway_setup.TTS_ENGINE_LABELS.get(name, name),
                            'available': ok,
                            'active': name == active})
        d = {'ok': True, 'engines': engines, 'active': active,
             'enabled': bool(getattr(gw.config, 'ENABLE_TTS', False)) if gw else False}
        if extra:
            d.update(extra)
        return d

    if handler.command == 'POST':
        try:
            length = int(handler.headers.get('Content-Length', 0))
            body = handler.rfile.read(length).decode('utf-8') if length else '{}'
            want = str(json_mod.loads(body or '{}').get('engine', '')).lower().strip()
            if gw is None:
                raise RuntimeError('gateway not available')
            ok, msg = gateway_setup.switch_tts_engine(gw, want, persist=False)
            if ok:
                # Persist through the same path the config page uses so the
                # choice survives a restart.
                parent._save_config({'TTS_ENGINE': gw._tts_backend})
                parent.config.load_config()
                print(f"  [TTS] Engine switched to {gw._tts_backend}")
            result = state({'ok': ok, 'message': msg})
        except Exception as e:
            result = state({'ok': False, 'message': str(e)})
    else:
        try:
            result = state()
        except Exception as e:
            result = {'ok': False, 'message': str(e)}

    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(result).encode())
    except BrokenPipeError:
        pass
    return


def _soundboard_state(parent):
    """Everything the category picker needs to render itself."""
    gw = parent.gateway
    ps = gw.playback_source if gw else None
    if ps is None:
        return {'ok': False, 'error': 'playback source not available'}
    counts = ps.soundboard_categories()
    pool, note = ps._select_soundboard_pool()
    selected = sorted({c for c, _ in pool})
    raw = str(getattr(gw.config, 'SOUNDBOARD_CATEGORIES', '') or '').strip()
    return {
        'ok': True,
        # Ordered biggest-first so the picker reads consistently.
        'categories': [{'name': n, 'count': c}
                       for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        'selected': selected,
        'filter': raw,
        'pool_size': len(pool),
        'max_seconds': float(getattr(gw.config, 'SOUNDBOARD_MAX_SECONDS', 15) or 0),
        # True when no filter is set, so the UI can show "all ticked" without
        # having to persist a filter naming all 31 categories.
        'all': not raw,
        'note': note,
    }


def handle_soundboard_categories(handler, parent):
    """GET/POST /soundboard/categories

    GET  -> available categories, counts, and what is currently selected.
    POST -> {"categories": [...]} persists SOUNDBOARD_CATEGORIES.
    """
    if handler.command == 'POST':
        try:
            length = int(handler.headers.get('Content-Length', 0))
            body = handler.rfile.read(length).decode('utf-8') if length else '{}'
            payload = json_mod.loads(body or '{}')
            gw = parent.gateway
            ps = gw.playback_source if gw else None
            if ps is None:
                raise RuntimeError('playback source not available')
            valid = set(ps.soundboard_categories())
            wanted = [str(c).strip().lower() for c in payload.get('categories', [])]
            wanted = [c for c in wanted if c in valid]
            # Everything ticked is stored as blank rather than a 31-name list:
            # blank keeps meaning "all", so a pool that grows later is picked
            # up automatically instead of being frozen to today's categories.
            value = '' if len(wanted) == len(valid) else ', '.join(sorted(wanted))
            parent._save_config({'SOUNDBOARD_CATEGORIES': value})
            parent.config.load_config()
            result = _soundboard_state(parent)
            result['saved'] = value
        except Exception as e:
            result = {'ok': False, 'error': str(e)}
    else:
        try:
            result = _soundboard_state(parent)
        except Exception as e:
            result = {'ok': False, 'error': str(e)}

    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(result).encode())
    except BrokenPipeError:
        pass
    return


def handle_refreshsounds(handler, parent):
    """POST /refreshsounds"""
    result = {'ok': False, 'count': 0}
    gw = parent.gateway
    if gw and gw.playback_source:
        try:
            # Clear cached soundboard files
            _cache_dir = os.path.join(gw.playback_source.announcement_directory, '.cache')
            if os.path.isdir(_cache_dir):
                import shutil
                shutil.rmtree(_cache_dir)
            # Re-scan files (local files stay, new random fills)
            gw.playback_source.check_file_availability()
            _slots = gw.playback_source.slot_keys()
            _count = sum(1 for k in _slots if gw.playback_source.file_status[k]['exists']
                         and gw.playback_source.file_status[k].get('path', '').find('.cache') >= 0)
            # Downloads run on the Soundboard-prefetch thread started inside
            # check_file_availability(), so most of them have NOT landed by the
            # time we get here — _count alone reports ~0 and the UI used to say
            # "Refreshed 0 sounds" on a perfectly good refresh. Report the slots
            # still being filled too so the message can be truthful.
            _pending = sum(1 for k in _slots
                           if not gw.playback_source.file_status[k]['exists'])
            result = {'ok': True, 'count': _count, 'pending': _pending}
            # Report which categories are in play and what else is on offer, so
            # SOUNDBOARD_CATEGORIES is discoverable without reading the source.
            try:
                _ps = gw.playback_source
                _pool, _note = _ps._select_soundboard_pool()
                result['categories'] = sorted(_ps.soundboard_categories())
                result['selected'] = sorted({c for c, _ in _pool})
                result['filter'] = str(getattr(gw.config, 'SOUNDBOARD_CATEGORIES', '') or '')
                result['pool_size'] = len(_pool)
                if _note:
                    result['note'] = _note
            except Exception:
                pass  # discovery info is a nicety; never fail the refresh over it
        except Exception as _e:
            result = {'ok': False, 'error': str(_e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    try:
        handler.wfile.write(json_mod.dumps(result).encode())
    except BrokenPipeError:
        pass
    return
