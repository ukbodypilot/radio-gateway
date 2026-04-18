"""POST route handlers extracted from web_server.py."""

import json as json_mod
import os
import time
import subprocess
import threading as _thr

from audio_sources import generate_cw_pcm
from cat_client import RadioCATClient


def _resolve_source(gw, source_id):
    """Resolve a source ID to the source object, checking plugins then link endpoints."""
    _plugin_map = {
        'sdr': 'sdr_plugin', 'kv4p': 'kv4p_plugin',
        'remote': 'remote_audio_source', 'announce': 'announce_input_source',
    }
    if source_id in _plugin_map:
        return getattr(gw, _plugin_map[source_id], None)
    # Link endpoint lookup by source_id
    for name, src in getattr(gw, 'link_endpoints', {}).items():
        if getattr(src, 'source_id', None) == source_id:
            return src
    return None


def handle_key(handler, parent):
    """POST /key"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
        key_char = data.get('key', '')
        if key_char and parent.gateway:
            parent.gateway.handle_key(key_char)
    except Exception:
        pass
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(b'{"ok":true}')
    return


def handle_transcribe_config(handler, parent):
    """POST /transcribe_config"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        key = data.get('key', '')
        value = data.get('value', '')
        tx = parent.gateway.transcriber if parent.gateway else None
        if not tx:
            result = {'ok': False, 'error': 'transcriber not running'}
        elif key == 'enabled':
            tx._enabled = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'vad_threshold':
            tx._vad_threshold = float(value)
            if hasattr(tx, '_silence_threshold'):
                tx._silence_threshold = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'vad_hold':
            tx._vad_hold_time = float(value)
            if hasattr(tx, '_silence_duration'):
                tx._silence_duration = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'min_duration':
            tx._min_duration = float(value)
            tx._save(); result = {'ok': True}
        elif key == 'language':
            tx._language = str(value)
            tx._save(); result = {'ok': True}
        elif key == 'forward_mumble':
            tx._forward_mumble = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'forward_telegram':
            tx._forward_telegram = bool(value)
            tx._save(); result = {'ok': True}
        elif key == 'audio_boost':
            tx._audio_boost = float(value) / 100.0
            tx._save(); result = {'ok': True}
        elif key == 'clear':
            with tx._results_lock:
                tx._results.clear()
            result = {'ok': True}
        elif key == 'model':
            tx._model_size = str(value)
            tx._save()
            result = {'ok': True, 'note': 'model change takes effect on restart'}
        elif key == 'mode':
            if parent.gateway:
                parent.gateway.config.TRANSCRIBE_MODE = str(value)
                # Save mode to settings file so restart picks it up
                from transcriber import _load_saved_settings, _save_settings
                _s = _load_saved_settings()
                _s['mode'] = str(value)
                _save_settings(_s)
            result = {'ok': True, 'note': 'mode change takes effect on restart'}
        elif key == 'restart':
            # Restart transcriber with current settings
            gw = parent.gateway
            if gw:
                if gw.transcriber:
                    gw.transcriber.stop()
                from transcriber import _load_saved_settings
                _saved = _load_saved_settings()
                _mode = _saved.get('mode', str(getattr(gw.config, 'TRANSCRIBE_MODE', 'chunked'))).lower()
                try:
                    if _mode == 'streaming':
                        from transcriber import StreamingTranscriber
                        gw.transcriber = StreamingTranscriber(gw.config, gw)
                    else:
                        from transcriber import RadioTranscriber
                        gw.transcriber = RadioTranscriber(gw.config, gw)
                    gw.transcriber.start()
                    result = {'ok': True, 'mode': _mode}
                except Exception as _re:
                    result = {'ok': False, 'error': str(_re)}
            else:
                result = {'ok': False, 'error': 'gateway not ready'}
        else:
            result = {'ok': False, 'error': f'unknown key: {key}'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    return


def handle_testloop(handler, parent):
    """POST /testloop"""
    result = {'ok': False, 'error': 'playback not available'}
    if parent.gateway and parent.gateway.playback_source:
        result = parent.gateway.playback_source.toggle_test_loop()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    return


def handle_mixer(handler, parent):
    """POST /mixer"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'no gateway'}
    try:
        data = json_mod.loads(body)
        action = data.get('action', '')
        source = data.get('source', '')
        gw = parent.gateway
        if not gw:
            pass
        elif action == 'status':
            # Return full mixer state
            s = gw.get_status_dict()
            result = {'ok': True, 'mutes': {
                'tx': s.get('tx_muted', False),
                'rx': s.get('rx_muted', False),
                'sdr1': s.get('sdr1_muted', False),
                'sdr2': s.get('sdr2_muted', False),
                'd75': s.get('d75_muted', False),
                'kv4p': s.get('kv4p_muted', False),
                'remote': s.get('remote_muted', False),
                'announce': s.get('announce_muted', False),
                'speaker': s.get('speaker_muted', False),
            }, 'levels': {
                'radio_rx': s.get('radio_rx', 0),
                'radio_tx': s.get('radio_tx', 0),
                'sdr1': s.get('sdr1_level', 0),
                'sdr2': s.get('sdr2_level', 0),
                'd75': s.get('d75_level', 0),
                'kv4p': s.get('kv4p_level', 0),
                'remote': s.get('remote_level', 0),
                'announce': s.get('an_level', 0),
                'speaker': s.get('speaker_level', 0),
            }, 'volume': s.get('volume', 1.0),
            'duck': {
                'sdr1': s.get('sdr1_duck', False),
                'sdr2': getattr(_resolve_source(gw, 'sdr'), 'duck', False),
                'd75': getattr(_resolve_source(gw, 'd75'), 'duck', False),
                'kv4p': getattr(_resolve_source(gw, 'kv4p'), 'duck', False),
                'remote': getattr(_resolve_source(gw, 'remote'), 'duck', False),
            }, 'ducked': {
                'sdr1': s.get('sdr1_ducked', False),
                'sdr2': s.get('sdr2_ducked', False),
                'remote': s.get('cl_ducked', False),
            }, 'flags': {
                'vad': s.get('vad_enabled', False),
                'agc': getattr(gw.config, 'ENABLE_AGC', False),
                'echo_cancel': getattr(gw.config, 'ENABLE_ECHO_CANCELLATION', False),
                'rebroadcast': s.get('sdr_rebroadcast', False),
                'talkback': getattr(gw, 'tx_talkback', False),
                'manual_ptt': s.get('manual_ptt', False),
            }, 'boost': {
                'd75': int(getattr(_resolve_source(gw, 'd75'), 'audio_boost', 1.0) * 100),
                'kv4p': int(getattr(_resolve_source(gw, 'kv4p'), 'audio_boost', 1.0) * 100),
                'remote': int(getattr(_resolve_source(gw, 'remote'), 'audio_boost', 1.0) * 100),
            }, 'processing': {
                'radio': gw.radio_processor.get_active_list() if hasattr(gw, 'radio_processor') else [],
                'sdr': gw.sdr_processor.get_active_list() if hasattr(gw, 'sdr_processor') else [],
                'd75': gw.d75_processor.get_active_list() if hasattr(gw, 'd75_processor') else [],
                'kv4p': gw.kv4p_processor.get_active_list() if hasattr(gw, 'kv4p_processor') else [],
            }}

        elif action in ('mute', 'unmute', 'toggle'):
            # Mute control for a specific source
            _mute_map = {
                'tx':       ('tx_muted', None),
                'rx':       ('rx_muted', None),
                'sdr1':     ('sdr_muted', 'sdr_plugin'),
                'sdr2':     ('sdr2_muted', 'sdr_plugin'),
                'kv4p':     ('kv4p_muted', 'kv4p_plugin'),
                'remote':   ('remote_audio_muted', 'remote_audio_source'),
                'announce': ('announce_input_muted', 'announce_input_source'),
                'speaker':  ('speaker_muted', None),
            }
            if source == 'global':
                current = gw.tx_muted and gw.rx_muted
                if action == 'toggle':
                    want = not current
                elif action == 'mute':
                    want = True
                else:
                    want = False
                gw.tx_muted = want
                gw.rx_muted = want
                result = {'ok': True, 'muted': want}
            elif source in _mute_map:
                attr, src_attr = _mute_map[source]
                current = getattr(gw, attr, False)
                if action == 'toggle':
                    want = not current
                elif action == 'mute':
                    want = True
                else:
                    want = False
                setattr(gw, attr, want)
                # Sync to source object if it has .muted
                if src_attr:
                    src_obj = getattr(gw, src_attr, None)
                    if src_obj:
                        src_obj.muted = want
                result = {'ok': True, 'source': source, 'muted': want}
            elif source.startswith('link_rx:') or source.startswith('link_tx:'):
                parts = source.split(':', 1)
                direction = parts[0]  # 'link_rx' or 'link_tx'
                ep_name = parts[1] if len(parts) > 1 else ''
                if not ep_name:
                    result = {'ok': False, 'error': 'missing endpoint name'}
                else:
                    settings = gw.link_endpoint_settings.setdefault(ep_name, {})
                    mute_key = 'rx_muted' if direction == 'link_rx' else 'tx_muted'
                    current = settings.get(mute_key, False)
                    want = not current if action == 'toggle' else (action == 'mute')
                    settings[mute_key] = want
                    if direction == 'link_rx':
                        src = gw.link_endpoints.get(ep_name)
                        if src:
                            src.muted = want
                    gw._save_link_settings()
                    result = {'ok': True, 'muted': want}
            else:
                # Try generic link endpoint by sanitised name
                _ep_src = None
                _ep_name = None
                for _n, _s in gw.link_endpoints.items():
                    if getattr(_s, 'source_id', None) == source:
                        _ep_src = _s
                        _ep_name = _n
                        break
                if _ep_src:
                    current = getattr(_ep_src, 'muted', False)
                    want = not current if action == 'toggle' else (action == 'mute')
                    _ep_src.muted = want
                    settings = gw.link_endpoint_settings.setdefault(_ep_name, {})
                    settings['rx_muted'] = want
                    gw._save_link_settings()
                    result = {'ok': True, 'source': source, 'muted': want}
                else:
                    result = {'ok': False, 'error': f'unknown source: {source}'}

        elif action == 'volume':
            # Set absolute INPUT_VOLUME
            val = data.get('value')
            if val is not None:
                gw.config.INPUT_VOLUME = max(0.1, min(3.0, float(val)))
                result = {'ok': True, 'volume': round(gw.config.INPUT_VOLUME, 2)}
            else:
                result = {'ok': True, 'volume': round(gw.config.INPUT_VOLUME, 2)}

        elif action == 'duck':
            # Enable/disable duck on a source
            state = data.get('state')  # true/false or omit for toggle
            src_obj = _resolve_source(gw, source)
            if src_obj and hasattr(src_obj, 'duck'):
                if state is None:
                    src_obj.duck = not src_obj.duck
                else:
                    src_obj.duck = bool(state)
                result = {'ok': True, 'source': source, 'duck': src_obj.duck}
            else:
                result = {'ok': False, 'error': f'duck not supported for: {source}'}

        elif action == 'boost':
            # Set per-source audio boost (percentage 0-500)
            pct = data.get('value', 100)
            src_obj = _resolve_source(gw, source)
            if src_obj and hasattr(src_obj, 'audio_boost'):
                src_obj.audio_boost = max(0, min(5.0, float(pct) / 100.0))
                result = {'ok': True, 'source': source, 'boost_pct': int(src_obj.audio_boost * 100)}
            else:
                result = {'ok': False, 'error': f'boost not supported for: {source}'}

        elif action == 'flag':
            # Toggle or set a mixer flag (vad, agc, echo_cancel, rebroadcast)
            flag = data.get('flag', '')
            state = data.get('state')  # true/false or omit for toggle
            if flag == 'vad':
                if state is None:
                    gw.config.ENABLE_VAD = not gw.config.ENABLE_VAD
                else:
                    gw.config.ENABLE_VAD = bool(state)
                result = {'ok': True, 'flag': 'vad', 'enabled': gw.config.ENABLE_VAD}
            elif flag == 'agc':
                if state is None:
                    gw.config.ENABLE_AGC = not gw.config.ENABLE_AGC
                else:
                    gw.config.ENABLE_AGC = bool(state)
                result = {'ok': True, 'flag': 'agc', 'enabled': gw.config.ENABLE_AGC}
            elif flag == 'echo_cancel':
                if state is None:
                    gw.config.ENABLE_ECHO_CANCELLATION = not gw.config.ENABLE_ECHO_CANCELLATION
                else:
                    gw.config.ENABLE_ECHO_CANCELLATION = bool(state)
                result = {'ok': True, 'flag': 'echo_cancel', 'enabled': gw.config.ENABLE_ECHO_CANCELLATION}
            elif flag == 'rebroadcast':
                if state is None:
                    new_state = not gw.sdr_rebroadcast
                else:
                    new_state = bool(state)
                gw.sdr_rebroadcast = new_state
                if not new_state:
                    # Clean up PTT if disabling rebroadcast
                    if getattr(gw, '_rebroadcast_ptt_active', False):
                        gw._rebroadcast_ptt_active = False
                    if gw.radio_source:
                        gw.radio_source.enabled = True
                result = {'ok': True, 'flag': 'rebroadcast', 'enabled': gw.sdr_rebroadcast}
            elif flag == 'talkback':
                if state is None:
                    gw.tx_talkback = not gw.tx_talkback
                else:
                    gw.tx_talkback = bool(state)
                result = {'ok': True, 'flag': 'talkback', 'enabled': gw.tx_talkback}
            else:
                result = {'ok': False, 'error': f'unknown flag: {flag}'}

        elif action == 'processing':
            # Toggle or set audio processing filter
            # source: radio, sdr, d75, kv4p
            # filter: gate, hpf, lpf, notch
            filt = data.get('filter', '')
            proc_state = data.get('state')  # true/false or omit for toggle
            valid_sources = ('radio', 'sdr', 'd75', 'kv4p')
            valid_filters = ('gate', 'hpf', 'lpf', 'notch')
            if source not in valid_sources:
                result = {'ok': False, 'error': f'source must be one of: {", ".join(valid_sources)}'}
            elif filt not in valid_filters:
                result = {'ok': False, 'error': f'filter must be one of: {", ".join(valid_filters)}'}
            else:
                gw.handle_proc_toggle(source, filt, state=proc_state)
                # Read back the current state
                _proc_map = {
                    'radio': gw.radio_processor,
                    'sdr': gw.sdr_processor,
                    'd75': gw.d75_processor,
                    'kv4p': gw.kv4p_processor,
                }
                proc_obj = _proc_map.get(source)
                active = proc_obj.get_active_list() if proc_obj else []
                result = {'ok': True, 'source': source, 'active': active}

        else:
            result = {'ok': False, 'error': f'unknown action: {action}'}

    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_aitext(handler, parent):
    """POST /aitext"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    ok = False
    error = None
    try:
        data = json_mod.loads(body)
        prompt = data.get('text', '').strip()
        target_secs = int(data.get('target_secs', 30))
        voice = int(data.get('voice', 1))
        top_text = data.get('top_text', 'QST').strip()
        tail_text = data.get('tail_text', 'Callsign').strip()
        if not prompt:
            error = 'no text provided'
        elif not parent.gateway:
            error = 'gateway not ready'
        elif not parent.gateway.smart_announce:
            error = 'smart announce not available'
        else:
            sa = parent.gateway.smart_announce
            # Build a synthetic entry for ad-hoc prompt
            entry = {
                'id': 0,
                'prompt': prompt,
                'voice': voice,
                'target_secs': min(max(target_secs, 5), 120),
                'interval': 0,
                'mode': 'manual',
                'top_text': top_text,
                'tail_text': tail_text,
            }
            _thr.Thread(target=sa._run_announcement, args=(entry, True),
                        daemon=True, name="WebAIText").start()
            ok = True
    except Exception as e:
        error = str(e)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
    handler.wfile.write(resp.encode())
    return


def handle_cw(handler, parent):
    """POST /cw"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    ok = False
    error = None
    try:
        data = json_mod.loads(body)
        text = data.get('text', '').strip()
        if not text:
            error = 'no text provided'
        elif not parent.gateway:
            error = 'gateway not ready'
        elif not parent.gateway.playback_source:
            error = 'playback not available'
        else:
            gw = parent.gateway
            _wpm  = int(data.get('wpm',  gw.config.CW_WPM))
            _freq = int(data.get('freq', gw.config.CW_FREQUENCY))
            _vol  = float(data.get('vol', gw.config.CW_VOLUME))
            def _do_cw():
                pcm = generate_cw_pcm(text, _wpm, _freq, 48000)
                if _vol != 1.0:
                    import numpy as _np
                    pcm = _np.clip(pcm.astype(_np.float32) * _vol,
                                   -32768, 32767).astype(_np.int16)
                import wave as _wave, tempfile as _tmp
                tf = _tmp.NamedTemporaryFile(suffix='.wav', delete=False, prefix='cw_')
                tf.close()
                with _wave.open(tf.name, 'wb') as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                    wf.writeframes(pcm.tobytes())
                if not gw.playback_source.queue_file(tf.name):
                    import os as _os
                    _os.unlink(tf.name)
            _thr.Thread(target=_do_cw, daemon=True, name="WebCW").start()
            ok = True
    except Exception as e:
        error = str(e)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
    handler.wfile.write(resp.encode())
    return


def handle_tts(handler, parent):
    """POST /tts"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    ok = False
    error = None
    try:
        data = json_mod.loads(body)
        text = data.get('text', '').strip()
        voice = data.get('voice', None)
        if not text:
            error = 'no text provided'
        elif not parent.gateway:
            error = 'gateway not ready'
        elif not parent.gateway.tts_engine:
            error = 'TTS not available'
        else:
            def _do_tts():
                parent.gateway.speak_text(text, voice=voice)
            _thr.Thread(target=_do_tts, daemon=True, name="WebTTS").start()
            ok = True
    except Exception as e:
        error = str(e)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    resp = '{"ok":true}' if ok else '{"ok":false,"error":' + json_mod.dumps(error) + '}'
    handler.wfile.write(resp.encode())
    return


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


def handle_proc_toggle(handler, parent):
    """POST /proc_toggle"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
        source = data.get('source', '')  # "radio" or "sdr"
        filt = data.get('filter', '')    # "gate", "hpf", "lpf", "notch"
        if source and filt and parent.gateway:
            parent.gateway.handle_proc_toggle(source, filt)
    except Exception:
        pass
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(b'{"ok":true}')
    return


def handle_d75cmd(handler, parent):
    """POST /d75cmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        cmd = data.get('cmd', '')
        args = data.get('args', '')
        gw = parent.gateway

        # Check for D75 link endpoint first (new path)
        _link = getattr(gw, 'link_server', None) if gw else None
        _d75_ep = None
        if _link:
            for ep_name, ep_src in gw.link_endpoints.items():
                if getattr(ep_src, 'plugin_type', None) == 'd75':
                    _d75_ep = ep_name
                    break

        if _d75_ep and _link:
            # Route through Gateway Link endpoint
            if cmd == 'cat':
                _link.send_command_to(_d75_ep, {'cmd': 'cat', 'raw': args})
                result = {'ok': True, 'response': f'sent via link endpoint'}
            elif cmd == 'ptt':
                # Toggle: check current state from cached endpoint status
                _ptt_now = getattr(gw, '_link_ptt_active', {}).get(_d75_ep, False)
                _link.send_command_to(_d75_ep, {'cmd': 'ptt', 'state': not _ptt_now})
                result = {'ok': True, 'response': f'PTT {"off" if _ptt_now else "on"} via link'}
            elif cmd == 'freq':
                _link.send_command_to(_d75_ep, {'cmd': 'frequency', 'freq': args})
                result = {'ok': True, 'response': f'freq set via link'}
            elif cmd == 'status':
                _link.send_command_to(_d75_ep, {'cmd': 'status'})
                result = {'ok': True, 'response': 'status requested'}
            elif cmd in ('btstart', 'btstop', 'reconnect', 'start_service'):
                # BT lifecycle managed by the remote endpoint — not applicable
                result = {'ok': True, 'response': f'{cmd}: managed by link endpoint'}
            elif cmd == 'mute':
                # Mute the link audio source on the gateway side
                _link_src = None
                for _src_name, _src in getattr(gw, 'link_endpoints', {}).items():
                    if getattr(_src, 'plugin_type', None) == 'd75':
                        _link_src = _src
                        break
                if _link_src:
                    _link_src.muted = not _link_src.muted
                    result = {'ok': True, 'muted': _link_src.muted}
                else:
                    result = {'ok': True, 'response': 'mute toggled'}
            elif cmd == 'vol':
                try:
                    pct = int(args)
                    pct = max(0, min(500, pct))
                    _link.send_command_to(_d75_ep, {'cmd': 'rx_gain', 'gain': pct / 100.0})
                    result = {'ok': True, 'response': f'boost={pct}%'}
                except (ValueError, TypeError):
                    result = {'ok': False, 'error': 'usage: vol 0-500'}
            elif cmd in ('tone', 'shift', 'offset'):
                # High-level FO-modify commands
                _link.send_command_to(_d75_ep, {'cmd': cmd, 'raw': args})
                result = {'ok': True, 'response': f'{cmd} sent via link'}
            else:
                # Pass through as raw CAT
                _link.send_command_to(_d75_ep, {'cmd': 'cat', 'raw': f'{cmd} {args}'.strip()})
                result = {'ok': True, 'response': f'sent via link'}

        else:
            result = {'ok': False, 'error': 'D75 not connected (no link endpoint or plugin)'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_gpscmd(handler, parent):
    """POST /gpscmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'GPS manager not available'}
    try:
        data = json_mod.loads(body)
        gps = getattr(parent.gateway, 'gps_manager', None) if parent.gateway else None
        if gps:
            cmd = data.get('cmd', '')
            if cmd == 'set_position':
                ok = gps.set_simulated_position(
                    lat=data.get('lat'), lon=data.get('lon'),
                    alt=data.get('alt'), speed=data.get('speed'),
                    heading=data.get('heading'))
                result = {'ok': ok, 'error': '' if ok else 'Not in simulate mode'}
            elif cmd == 'switch_mode':
                mode = data.get('mode', '')
                ok, msg = gps.switch_mode(mode)
                result = {'ok': ok, 'message': msg}
            elif cmd == 'status':
                result = {'ok': True, 'status': gps.get_status()}
            else:
                result = {'ok': False, 'error': f'Unknown command: {cmd}'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        resp = json_mod.dumps(result).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(resp)))
        handler.end_headers()
        handler.wfile.write(resp)
    except BrokenPipeError:
        pass


def handle_kv4pcmd(handler, parent):
    """POST /kv4pcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'KV4P plugin not available'}
    try:
        data = json_mod.loads(body)
        _kv4p_p = getattr(parent.gateway, 'kv4p_plugin', None) if parent.gateway else None
        if _kv4p_p:
            cmd = data.get('cmd', '')
            args = data.get('args', '')
            # Map web UI command format to plugin execute format
            if cmd == 'freq':
                result = _kv4p_p.execute({'cmd': 'freq', 'frequency': float(args)})
            elif cmd == 'txfreq':
                result = _kv4p_p.execute({'cmd': 'freq', 'frequency': _kv4p_p._frequency, 'tx_frequency': float(args)})
            elif cmd == 'squelch':
                result = _kv4p_p.execute({'cmd': 'squelch', 'level': int(args)})
            elif cmd == 'ctcss':
                _ctcss_hz = ["67.0","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
                    "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
                    "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
                    "173.8","179.9","186.2","192.8","203.5","210.7","218.1","225.7","233.6","241.8","250.3"]
                def _hz_to_code(s):
                    s = str(s).strip()
                    if s == '0' or s.lower() in ('none', ''):
                        return 0
                    try:
                        return _ctcss_hz.index(s) + 1
                    except ValueError:
                        return int(float(s))
                parts = str(args).split()
                tx = _hz_to_code(parts[0]) if len(parts) > 0 else 0
                rx = _hz_to_code(parts[1]) if len(parts) > 1 else tx
                result = _kv4p_p.execute({'cmd': 'ctcss', 'tx': tx, 'rx': rx})
            elif cmd == 'bandwidth':
                result = _kv4p_p.execute({'cmd': 'bandwidth', 'wide': str(args).lower() in ('1', 'wide', 'true')})
            elif cmd == 'power':
                result = _kv4p_p.execute({'cmd': 'power', 'high': str(args).lower() in ('1', 'high', 'true', 'h')})
            elif cmd == 'ptt':
                result = _kv4p_p.execute({'cmd': 'ptt', 'state': not _kv4p_p._transmitting})
            elif cmd == 'smeter':
                if _kv4p_p._radio:
                    _kv4p_p._radio.enable_smeter(str(args).lower() in ('1', 'true', 'on', ''))
                result = {'ok': True}
            elif cmd == 'vol':
                result = _kv4p_p.execute({'cmd': 'boost', 'value': int(args) / 100.0})
            elif cmd == 'testtone':
                result = _kv4p_p.execute({'cmd': 'testtone', 'frequency': float(args) if args else 440})
            elif cmd == 'record':
                result = _kv4p_p.execute({'cmd': 'capture'})
            elif cmd == 'reconnect':
                result = _kv4p_p.execute({'cmd': 'reconnect'})
            else:
                result = _kv4p_p.execute(data)
        elif data.get('cmd') == 'reconnect' and parent.gateway:
            # Reconnect even when plugin is None — recreate it
            try:
                from kv4p_plugin import KV4PPlugin
                parent.gateway.kv4p_plugin = KV4PPlugin()
                if parent.gateway.kv4p_plugin.setup(parent.gateway.config):
                    result = {'ok': True, 'response': 'Reconnected'}
                else:
                    parent.gateway.kv4p_plugin = None
                    result = {'ok': False, 'error': 'Reconnect failed'}
            except Exception as e:
                result = {'ok': False, 'error': str(e)}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_linkcmd(handler, parent):
    """POST /linkcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        gw = parent.gateway
        endpoint_name = data.get('endpoint', '')
        if not endpoint_name:
            result = {'ok': False, 'error': 'missing endpoint name'}
        elif not gw or not gw.link_server:
            result = {'ok': False, 'error': 'link server not running'}
        elif endpoint_name not in gw.link_endpoints:
            result = {'ok': False, 'error': f'endpoint not connected: {endpoint_name}'}
        else:
            cmd_name = data.get('cmd', '')
            # Mark that we're waiting for a fresh status (don't clear existing)
            _prev_status = gw._link_last_status.get(endpoint_name, {})
            _prev_uptime = _prev_status.get('uptime', -1)
            gw.link_server.send_command_to(endpoint_name, data)
            if cmd_name == 'status':
                import time as _time
                for _ in range(10):  # 1 second max
                    _time.sleep(0.1)
                    _cur = gw._link_last_status.get(endpoint_name, {})
                    if _cur and _cur.get('uptime', -1) != _prev_uptime:
                        break  # new status arrived
                result = {'ok': True, 'status': gw._link_last_status.get(endpoint_name, {})}
            else:
                result = {'ok': True, 'sent': data}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_catcmd(handler, parent):
    """POST /catcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        cmd = data.get('cmd', '')
        gw = parent.gateway
        if cmd == 'SET_TX_RADIO' and gw:
            radio = data.get('radio', '').lower()
            if radio in ('th9800', 'd75', 'kv4p'):
                gw.config.TX_RADIO = radio
                result = {'ok': True, 'radio': radio}
            else:
                result = {'ok': False, 'error': 'unknown radio'}
        elif cmd == 'GET_TX_RADIO' and gw:
            result = {'ok': True, 'radio': str(getattr(gw.config, 'TX_RADIO', 'th9800')).lower()}
        elif cmd == 'CAT_DISCONNECT' and gw and gw.cat_client:
            gw.cat_client._stop = True
            gw.cat_client.close()
            gw.cat_client = None
            print("\n  [CAT] Disconnected via web")
            result = {'ok': True}
        elif cmd == 'SERIAL_DISCONNECT' and gw and gw.cat_client:
            gw.cat_client._pause_drain()
            try:
                resp = gw.cat_client._send_cmd("!serial disconnect")
            finally:
                gw.cat_client._drain_paused = False
            if resp and 'disconnected' in resp:
                gw.cat_client._serial_connected = False
            print(f"\n  [CAT] Serial disconnect: {resp}")
            result = {'ok': resp and 'disconnected' in resp, 'status': resp or ''}
        elif cmd == 'SERIAL_CONNECT' and gw and gw.cat_client:
            # serial connect takes ~4s (startup sequence with sleeps)
            cat = gw.cat_client
            cat._pause_drain()
            try:
                with cat._sock_lock:
                    cat._sock.sendall(b"!serial connect\n")
                    cat._last_activity = time.monotonic()
                    resp = cat._recv_line(timeout=10.0)
            finally:
                cat._drain_paused = False
            ok = resp and 'connected' in resp
            already = ok and 'already' in resp
            if ok:
                cat._serial_connected = True
                try:
                    cat.set_rts(True)
                except Exception:
                    pass

            print(f"\n  [CAT] Serial connect: {resp}")
            result = {'ok': ok, 'status': resp or ''}
        elif cmd == 'SETUP_RADIO' and gw and gw.cat_client:
            # Run setup_radio (channels, volume, power) from config
            cat = gw.cat_client
            try:
                cat.setup_radio(gw.config)
                result = {'ok': True, 'status': 'setup complete'}
            except Exception as e:
                print(f"\n  [CAT] Setup error: {e}")
                result = {'ok': False, 'status': str(e)}
        elif cmd == 'SERIAL_STATUS' and gw and gw.cat_client:
            resp = gw.cat_client._send_cmd("!serial status")
            result = {'ok': True, 'status': resp or 'unknown'}
        elif cmd == 'MIC_PTT' and gw:
            # Key/unkey TH-9800 via configured PTT_METHOD, regardless of TX_RADIO
            gw._web_th9800_ptt = not getattr(gw, '_web_th9800_ptt', False)
            state = gw._web_th9800_ptt
            method = str(getattr(gw.config, 'PTT_METHOD', 'aioc')).lower()
            if method == 'relay':
                gw._ptt_relay(state)
            elif method == 'software':
                gw._ptt_software(state)
            else:
                gw._ptt_aioc(state)
            result = {'ok': True}
        elif cmd == 'CAT_RECONNECT' and gw:
            if gw.cat_client:
                ok = gw.cat_client.reconnect()
                print(f"\n  [CAT] Reconnected via web: {'ok' if ok else 'failed'}")
                result = {'ok': ok}
            else:
                # Create fresh client
                host = str(getattr(gw.config, 'CAT_HOST', '127.0.0.1'))
                port = int(getattr(gw.config, 'CAT_PORT', 9800))
                pw = str(getattr(gw.config, 'CAT_PASSWORD', '') or '')
                cat = RadioCATClient(host, port, pw)
                if cat.connect():
                    cat.start_background_drain()
                    gw.cat_client = cat
                    print("\n  [CAT] Connected via web")
                    result = {'ok': True}
                else:
                    print("\n  [CAT] Connect failed via web")
                    result = {'ok': False, 'error': 'Connection failed'}
        elif cmd and gw and gw.cat_client:
            cat = gw.cat_client
            if cmd == 'VOL_LEFT':
                ret = cat.send_web_volume(cat.LEFT, data.get('value', 50))
                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
            elif cmd == 'VOL_RIGHT':
                ret = cat.send_web_volume(cat.RIGHT, data.get('value', 50))
                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
            elif cmd == 'SQ_LEFT':
                ret = cat.send_web_squelch(cat.LEFT, data.get('value', 25))
                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
            elif cmd == 'SQ_RIGHT':
                ret = cat.send_web_squelch(cat.RIGHT, data.get('value', 25))
                result = {'ok': False, 'error': ret} if ret == 'serial not connected' else {'ok': True}
            else:
                ret = cat.send_web_command(cmd)
                if isinstance(ret, str):
                    if 'serial not connected' in ret:
                        cat._serial_connected = False
                    result = {'ok': False, 'error': ret}
                else:
                    result = {'ok': bool(ret)}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_sdrcmd(handler, parent):
    """POST /sdrcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'SDR plugin not available'}
    try:
        data = json_mod.loads(body)
        _sdr_p = getattr(parent.gateway, 'sdr_plugin', None) if parent.gateway else None
        if _sdr_p:
            result = _sdr_p.execute(data)
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_tracecmd(handler, parent):
    """POST /tracecmd"""
    content_length = int(handler.headers.get('Content-Length', 0))
    post_data = handler.rfile.read(content_length).decode('utf-8')
    import urllib.parse as _up
    params = _up.parse_qs(post_data)
    trace_type = params.get('type', [''])[0]
    _gw = parent.gateway
    result = {'ok': False}
    if _gw and trace_type == 'audio':
        _gw._trace_recording = not _gw._trace_recording
        if _gw._trace_recording:
            _gw._audio_trace.clear()
            _gw._spk_trace.clear()
            _gw._trace_events.clear()
            import time as _trace_time
            _gw._audio_trace_t0 = _trace_time.monotonic()
            # Start stream-level trace
            if hasattr(_gw, '_stream_trace'):
                _gw._stream_trace.start()
            print(f"\n[Trace] Recording STARTED (via web UI)")
        else:
            # Stop stream-level trace and dump
            if hasattr(_gw, '_stream_trace'):
                _gw._stream_trace.stop()
            print(f"\n[Trace] Recording STOPPED ({len(_gw._audio_trace)} ticks captured)")
            _gw._dump_audio_trace()
            # Dump stream trace
            if hasattr(_gw, '_stream_trace'):
                import os as _os
                _st_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'tools', 'stream_trace.txt')
                _gw._stream_trace.dump(_st_path)
        import time as _trace_time2
        _gw._trace_events.append((_trace_time2.monotonic(), 'trace', 'on' if _gw._trace_recording else 'off'))
        result = {'ok': True, 'active': _gw._trace_recording}
    elif _gw and trace_type == 'watchdog':
        _gw._watchdog_active = not _gw._watchdog_active
        if _gw._watchdog_active:
            _gw._watchdog_t0 = time.monotonic()
            _gw._watchdog_thread = _thr.Thread(
                target=_gw._watchdog_trace_loop, daemon=True)
            _gw._watchdog_thread.start()
            print(f"\n[Watchdog] Trace STARTED (via web UI)")
        else:
            print(f"\n[Watchdog] Trace STOPPED (via web UI)")
        result = {'ok': True, 'active': _gw._watchdog_active}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_reboothost(handler, parent):
    """POST /reboothost"""
    import subprocess as _sp
    result = {'ok': False}
    try:
        _sp.Popen(['sudo', 'reboot'])
        result = {'ok': True}
    except Exception as _e:
        result = {'ok': False, 'error': str(_e)}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    except BrokenPipeError:
        pass
    return


def handle_restartgateway(handler, parent):
    """POST /restartgateway — restart the radio-gateway systemd service."""
    import subprocess as _sp
    result = {'ok': False}
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'ok': True}).encode('utf-8'))
        try:
            handler.wfile.flush()
        except Exception:
            pass
    except BrokenPipeError:
        pass
    try:
        _sp.Popen(['sudo', 'systemctl', 'restart', 'radio-gateway.service'])
    except Exception as _e:
        print(f"  [restart] failed: {_e}")
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
            _count = sum(1 for k in '123456789' if gw.playback_source.file_status[k]['exists']
                         and gw.playback_source.file_status[k].get('path', '').find('.cache') >= 0)
            result = {'ok': True, 'count': _count}
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


def handle_darkicecmd(handler, parent):
    """POST /darkicecmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False}
    try:
        data = json_mod.loads(body)
        cmd = data.get('cmd', '')
        gw = parent.gateway
        if gw:
            if cmd == 'start':
                if not gw._find_darkice_pid():
                    gw._restart_darkice()
                    result = {'ok': True, 'msg': 'DarkIce started'}
                else:
                    result = {'ok': True, 'msg': 'DarkIce already running'}
            elif cmd == 'stop':
                pid = gw._find_darkice_pid()
                if pid:
                    import signal as sig_mod
                    try:
                        os.kill(pid, sig_mod.SIGTERM)
                        time.sleep(1)
                        # Check if still alive
                        if gw._find_darkice_pid():
                            os.kill(pid, sig_mod.SIGKILL)
                    except ProcessLookupError:
                        pass
                    gw._darkice_pid = None
                    gw._darkice_was_running = False  # Prevent auto-restart
                    result = {'ok': True, 'msg': 'DarkIce stopped'}
                else:
                    result = {'ok': True, 'msg': 'DarkIce not running'}
            elif cmd == 'restart':
                pid = gw._find_darkice_pid()
                if pid:
                    import signal as sig_mod
                    try:
                        os.kill(pid, sig_mod.SIGTERM)
                        time.sleep(1)
                        if gw._find_darkice_pid():
                            os.kill(pid, sig_mod.SIGKILL)
                    except ProcessLookupError:
                        pass
                    gw._darkice_pid = None
                    time.sleep(1)
                gw._restart_darkice()
                gw._darkice_was_running = True  # Re-enable auto-restart
                result = {'ok': True, 'msg': 'DarkIce restarted'}
    except Exception as e:
        result = {'ok': False, 'msg': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    return


def handle_recordingsdelete(handler, parent):
    """POST /recordingsdelete"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    data = json_mod.loads(body)
    filenames = data.get('files', [])
    delete_all = data.get('delete_all', False)
    rec_dir = ''
    if parent.gateway and parent.gateway.automation_engine:
        rec_dir = parent.gateway.automation_engine.recorder._dir
    deleted = 0
    if rec_dir and os.path.isdir(rec_dir):
        if delete_all:
            for fname in os.listdir(rec_dir):
                fpath = os.path.join(rec_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                        deleted += 1
                    except OSError:
                        pass
        else:
            for fname in filenames:
                fname = os.path.basename(fname)  # no path traversal
                fpath = os.path.join(rec_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                        deleted += 1
                    except OSError:
                        pass
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps({'deleted': deleted}).encode('utf-8'))
    return


def handle_telegramcmd(handler, parent):
    """POST /telegramcmd"""
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'unknown command'}
    try:
        data = json_mod.loads(body)
        cmd = data.get('cmd', '')
        if cmd in ('start', 'stop', 'restart'):
            _r = subprocess.run(['sudo', 'systemctl', cmd, 'telegram-bot'],
                                capture_output=True, text=True, timeout=10)
            result = {'ok': _r.returncode == 0,
                      'output': (_r.stdout + _r.stderr).strip()}
        elif cmd == 'enable':
            _r = subprocess.run(['sudo', 'systemctl', 'enable', 'telegram-bot'],
                                capture_output=True, text=True, timeout=10)
            result = {'ok': _r.returncode == 0}
        elif cmd == 'disable':
            _r = subprocess.run(['sudo', 'systemctl', 'disable', 'telegram-bot'],
                                capture_output=True, text=True, timeout=10)
            result = {'ok': _r.returncode == 0}
        elif cmd == 'logs':
            _r = subprocess.run(['journalctl', '-u', 'telegram-bot', '--no-pager', '-n', '50'],
                                capture_output=True, text=True, timeout=5)
            result = {'ok': True, 'logs': _r.stdout}
        else:
            result = {'ok': False, 'error': f'unknown command: {cmd}'}
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode('utf-8'))
    return


def handle_open_tmux(handler, parent):
    """POST /open_tmux"""
    session = getattr(parent.config, 'TELEGRAM_TMUX_SESSION', 'claude-gateway') if parent.config else 'claude-gateway'
    try:
        subprocess.Popen(
            ['xfce4-terminal', '-e', f'tmux attach-session -t {session}'],
            env={**os.environ, 'DISPLAY': ':0'},
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ok = True
    except Exception:
        ok = False
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps({'ok': ok}).encode('utf-8'))
    return


def handle_exit(handler, parent):
    """POST /exit"""
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(b'{"ok":true}')
    if parent.gateway:
        parent.gateway.restart_requested = False
        parent.gateway.running = False
    return


def handle_routing_cmd(handler, parent):
    """POST /routing/cmd"""
    import json as json_mod
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    result = {'ok': False, 'error': 'invalid'}
    try:
        data = json_mod.loads(body)
        result = parent._handle_routing_cmd(data)
    except Exception as e:
        result = {'ok': False, 'error': str(e)}
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode())
    return


def handle_voice_send(handler, parent):
    """POST /voice/send"""
    import json as json_mod
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
    except Exception:
        data = {}
    text = data.get('text', '').strip()
    tmux_target = os.environ.get('TMUX_TARGET', 'claude-voice')
    if not text:
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"error":"empty text"}')
        return
    chk = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
    if chk.returncode != 0:
        handler.send_response(503)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'error': f"tmux session '{tmux_target}' not found"}).encode())
        return
    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', text])
    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps({'ok': True, 'sent': text}).encode())
    return


def handle_voice_session(handler, parent):
    """POST /voice/session"""
    import json as json_mod
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
    except Exception:
        data = {}
    action = data.get('action', '')
    tmux_target = 'claude-voice'
    result = {'ok': False}

    if action == 'start':
        # Create session if it doesn't exist, then launch claude
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode == 0:
            result = {'ok': True, 'msg': 'session already exists'}
        else:
            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            # Auto-confirm workspace trust prompt after startup
            import time; time.sleep(3)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            result = {'ok': True, 'msg': 'session created, claude started'}

    elif action == 'restart':
        # Send Ctrl+C to stop current process, wait, then start claude again
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode != 0:
            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
        else:
            # Send Ctrl+C twice to kill any running process
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(1)
            # Clear the screen before starting fresh
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            import time; time.sleep(0.3)
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions'])
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
        # Auto-confirm workspace trust prompt after startup
        import time; time.sleep(3)
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
        result = {'ok': True, 'msg': 'claude restarted'}

    elif action == 'stop':
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode == 0:
            # Send Ctrl+C to stop Claude, clear screen, leave the shell running
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            result = {'ok': True, 'msg': 'claude stopped'}
        else:
            result = {'ok': True, 'msg': 'session not running'}
    else:
        result = {'ok': False, 'error': 'unknown action'}

    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode())
    return


def handle_config_form(handler, parent):
    """POST fallback -- config form save"""
    import urllib.parse
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    form = urllib.parse.parse_qs(body, keep_blank_values=True)
    # Flatten: parse_qs returns lists; for checkboxes with hidden fallback,
    # take the LAST value (checkbox 'true' comes after hidden 'false')
    values = {k: v[-1] for k, v in form.items() if k != '_action'}
    action = form.get('_action', ['save'])[0]

    # Checkboxes: the hidden fallback field ensures unchecked boxes
    # submit 'false'. If a boolean key is completely absent from the
    # form (page not fully loaded, truncated POST), do NOT force it
    # to false — use the current running value instead.
    # Only force false if we received a reasonable number of keys
    # (full form submission has 200+ keys).
    if len(values) > 100:
        for key, default_val in parent._defaults.items():
            if isinstance(default_val, bool) and key not in values:
                values[key] = 'false'
    else:
        print(f"  [Config] WARNING: partial form ({len(values)} keys) — merging with current config")

    parent._save_config(values)
    # Reload config from file so the config page reflects saved values
    parent.config.load_config()
    handler.send_response(303)
    handler.send_header('Location', '/config?saved=1')
    handler.end_headers()


# ── Packet Radio POST handlers ──

def handle_packet_cmd(handler, parent):
    """POST /packet/* — dispatch packet radio commands."""
    import json as _json
    path = handler.path.split('?')[0]
    action = path.replace('/packet/', '')

    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length) if length > 0 else b'{}'
        data = _json.loads(body) if body else {}
    except Exception:
        data = {}

    gw = parent.gateway if parent else None
    result = {"ok": False, "error": "packet plugin not available"}

    if gw and gw.packet_plugin:
        pp = gw.packet_plugin
        if action == 'mode':
            result = pp.execute({'cmd': 'set_mode', 'mode': data.get('mode', 'idle')})
        elif action == 'aprs_beacon':
            result = pp.execute({'cmd': 'aprs_beacon'})
        elif action == 'aprs_send':
            result = pp.execute({'cmd': 'aprs_send', 'to': data.get('to', ''), 'message': data.get('message', '')})
        elif action == 'bbs_connect':
            result = pp.execute({'cmd': 'bbs_connect', 'callsign': data.get('callsign', '')})
        elif action == 'bbs_disconnect':
            result = pp.execute({'cmd': 'bbs_disconnect'})
        elif action == 'bbs_send':
            result = pp.execute({'cmd': 'bbs_send', 'text': data.get('text', '')})
        elif action == 'force_audio':
            result = pp.execute({'cmd': 'force_audio'})
        elif action == 'winlink/compose':
            result = _winlink_compose(data)
        elif action == 'winlink/connect':
            result = _winlink_connect(data)
        else:
            result = {"ok": False, "error": f"unknown action: {action}"}

    resp = _json.dumps(result).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)


def _winlink_compose(data):
    """Queue a Winlink message via Pat CLI."""
    import subprocess, shutil
    pat = shutil.which('pat')
    if not pat:
        return {"ok": False, "error": "pat not installed"}
    to = data.get('to', '').strip()
    cc = data.get('cc', '').strip()
    subject = data.get('subject', '').strip()
    body = data.get('body', '')
    if not to or not subject:
        return {"ok": False, "error": "to and subject required"}
    cmd = [pat, 'compose', '-s', subject]
    if cc:
        cmd += ['-c', cc]
    cmd.append(to)
    try:
        proc = subprocess.run(cmd, input=body.encode(), capture_output=True, timeout=10)
        if proc.returncode == 0:
            return {"ok": True}
        return {"ok": False, "error": proc.stderr.decode().strip() or f"exit code {proc.returncode}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_winlink_log = ''  # shared log buffer for polling

def _winlink_connect(data):
    """Connect to a Winlink gateway via Pat CLI + AGW."""
    global _winlink_log
    import subprocess, shutil, threading
    pat = shutil.which('pat')
    if not pat:
        return {"ok": False, "error": "pat not installed"}
    gateway = data.get('gateway', '').strip()
    if not gateway:
        return {"ok": False, "error": "gateway callsign required"}
    _winlink_log = f"Connecting to {gateway}...\n"
    try:
        proc = subprocess.Popen(
            [pat, 'connect', f'ax25+agwpe:///{gateway}'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        # Read output line-by-line into shared buffer
        def _reader():
            global _winlink_log
            for line in proc.stdout:
                text = line.decode('utf-8', errors='replace').rstrip()
                if text:
                    _winlink_log += text + '\n'
            proc.wait()
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=180)
        if proc.poll() is None:
            proc.kill()
            _winlink_log += '\n[TIMEOUT — killed after 180s]\n'
            return {"ok": False, "error": "timeout (180s)"}
        if proc.returncode == 0:
            _winlink_log += '\nSync complete.\n'
            return {"ok": True, "result": "sync complete"}
        _winlink_log += f'\nExit code: {proc.returncode}\n'
        return {"ok": False, "error": f"exit code {proc.returncode}"}
    except Exception as e:
        _winlink_log += f'\nError: {e}\n'
        return {"ok": False, "error": str(e)}


# ── Loop Recorder POST handlers ──

def handle_loop_export(handler, parent):
    """POST /loop/export — export a time range as downloadable audio file."""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length) if length > 0 else b'{}'
        data = json_mod.loads(body) if body else {}
    except Exception:
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"ok":false,"error":"invalid JSON body"}')
        return

    gw = parent.gateway if parent else None
    lr = getattr(gw, 'loop_recorder', None) if gw else None
    if not lr:
        handler.send_response(503)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"ok":false,"error":"loop recorder not available"}')
        return

    bus = data.get('bus', '')
    start = data.get('start')
    end = data.get('end')
    fmt = data.get('format', 'mp3')
    if not bus or start is None or end is None:
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"ok":false,"error":"missing bus, start, or end"}')
        return
    if fmt not in ('mp3', 'wav'):
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"ok":false,"error":"format must be mp3 or wav"}')
        return

    temp_path = None
    try:
        temp_path = lr.export_range(bus, float(start), float(end), fmt=fmt)
        if not temp_path or not os.path.isfile(temp_path):
            handler.send_response(404)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(b'{"ok":false,"error":"no audio found for range"}')
            return

        ctype = {'mp3': 'audio/mpeg', 'wav': 'audio/wav'}.get(fmt, 'application/octet-stream')
        fname = f"loop_{bus}_{int(float(start))}_{int(float(end))}.{fmt}"
        fsize = os.path.getsize(temp_path)
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        handler.send_header('Content-Length', str(fsize))
        handler.end_headers()
        with open(temp_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except BrokenPipeError:
        pass
    except Exception as e:
        try:
            handler.send_response(500)
            handler.send_header('Content-Type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json_mod.dumps({"ok": False, "error": str(e)}).encode('utf-8'))
        except Exception:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
