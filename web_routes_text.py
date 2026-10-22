"""POST handlers for text generation: !speak TTS, CW, AI text."""

"""POST route handlers extracted from web_server.py."""

import json as json_mod
import threading as _thr

from audio_util import generate_cw_pcm


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
        _v = data.get('voice', 1)
        voice = _v if isinstance(_v, str) and not _v.isdigit() else int(_v or 1)
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
