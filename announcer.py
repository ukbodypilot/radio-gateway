"""Repeating TTS announcement over the BGM bed.

Each BGM bed carries its OWN message. Starting bed 2 switches the announcer to
bed 2's text; stopping BGM stops the announcer. That coupling is deliberate —
a message is "the thing said over this music", so tying it to the bed removes a
separate selector and makes the pads self-explanatory.

State persists to ~/.config/radio-gateway/announcer.json so it survives a
restart, mirroring the source-gains store. Messages are synthesised on demand
and cached per bed, so the repeat cycle costs nothing and switching beds does
not re-render text that has not changed.
"""

import json
import os
import tempfile
import threading

_PATH = os.path.expanduser('~/.config/radio-gateway/announcer.json')
_lock = threading.Lock()

# text AND voice are per bed; interval and the master enable are global.
DEFAULTS = {
    'messages': {},     # {"1": "text", "2": "...", "3": "..."}
    'voices': {},       # {"1": "19"} — per bed; blank/absent = engine default
    'interval': 10.0,
    'max_seconds': 120.0,   # bed runtime cap; 0 = run until stopped
    'voice': '',        # legacy global fallback, kept so old files still load
    'enabled': False,
}

# slot -> (text, voice, backend, pcm). Invalidated when any of the first three
# change, so editing bed 2 does not throw away bed 1's rendering.
_cache = {}


def load():
    """Persisted state, merged over defaults. Never raises."""
    state = dict(DEFAULTS)
    state['messages'] = {}
    try:
        with open(_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data:
                    state[k] = data[k]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    msgs = state.get('messages')
    state['messages'] = ({str(k): str(v or '') for k, v in msgs.items()}
                         if isinstance(msgs, dict) else {})
    vs = state.get('voices')
    state['voices'] = ({str(k): str(v or '') for k, v in vs.items()}
                       if isinstance(vs, dict) else {})
    try:
        state['interval'] = max(2.0, float(state.get('interval') or 10.0))
    except (TypeError, ValueError):
        state['interval'] = 10.0
    try:
        # 0 is meaningful (no cap), so clamp at 0 rather than a minimum runtime.
        state['max_seconds'] = max(0.0, float(state.get('max_seconds', 120.0)))
    except (TypeError, ValueError):
        state['max_seconds'] = 120.0
    state['enabled'] = bool(state.get('enabled'))
    state['voice'] = str(state.get('voice') or '')
    return state


def save(state):
    """Atomic write — a torn file would silently drop every message."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(_PATH), exist_ok=True)
            tmp = _PATH + '.partial'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, _PATH)
            return True
        except OSError as e:
            print(f"  [Announcer] could not save state: {e}")
            return False


def synthesize(gw, text, voice=''):
    """Render `text` to PCM bytes with the active TTS backend.

    Returns (pcm_bytes, error). Deliberately not speak_text(): that queues for
    immediate playback, whereas the announcer needs the samples in hand to
    replay on its own schedule.
    """
    text = (text or '').strip()
    if not text:
        return None, 'no message text'
    engine = getattr(gw, 'tts_engine', None)
    backend = getattr(gw, '_tts_backend', '')
    if not engine:
        return None, 'TTS not available'
    ps = getattr(gw, 'playback_source', None)
    if ps is None:
        return None, 'playback source not available'

    suffix = '.wav' if backend == 'kokoro' else '.mp3'
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix='announcer_')
    tmp.close()
    path = tmp.name
    try:
        if backend == 'kokoro':
            import soundfile as sf
            vid = str(voice or getattr(gw.config, 'KOKORO_DEFAULT_VOICE', 'af_heart'))
            lang_map = {'a': 'en-us', 'b': 'en-gb', 'j': 'ja', 'z': 'zh', 'e': 'es',
                        'f': 'fr-fr', 'h': 'hi', 'i': 'it', 'p': 'pt-br'}
            samples, rate = engine.create(
                text, voice=vid, speed=1.0,
                lang=lang_map.get(vid[0], 'en-us') if vid else 'en-us')
            sf.write(path, samples, rate)
        elif backend == 'edge':
            import asyncio
            from text_commands import _resolve_voice_index
            n = _resolve_voice_index(gw, voice or None, gw.EDGE_TTS_VOICES)
            asyncio.run(engine.Communicate(text, gw.EDGE_TTS_VOICES[n][0]).save(path))
        else:
            from text_commands import _resolve_voice_index
            n = _resolve_voice_index(gw, voice or None, gw.TTS_VOICES)
            lang, tld, _ = gw.TTS_VOICES[n]
            engine(text, lang=lang, tld=tld, slow=False).save(path)

        pcm = ps._decode_file(path, target_rms_db=getattr(gw.config, 'TTS_TARGET_RMS_DB', None))
        if not pcm:
            return None, 'could not decode the synthesised audio'
        return pcm, ''
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def valid_voice(gw, voice):
    """Return `voice` if it is valid for the ACTIVE engine, else ''.

    Voices are engine-specific, so a per-bed voice saved under Edge is
    meaningless once you hot-swap to Kokoro. Rather than letting synthesis
    raise, fall back to the engine's configured default.
    """
    voice = str(voice or '').strip()
    if not voice:
        return ''
    try:
        valid = {str(v['value']) for v in gw._get_tts_voices()}
    except Exception:
        return ''
    return voice if voice in valid else ''


def _pcm_for(gw, slot, text, voice):
    """Cached synthesis for one bed. Returns (pcm, error)."""
    backend = getattr(gw, '_tts_backend', '')
    key = (str(text), str(voice), backend)
    hit = _cache.get(str(slot))
    if hit and hit[0] == key:
        return hit[1], ''
    pcm, err = synthesize(gw, text, voice)
    if err:
        return None, err
    _cache[str(slot)] = (key, pcm)
    return pcm, ''


def invalidate(slot=None):
    if slot is None:
        _cache.clear()
    else:
        _cache.pop(str(slot), None)


def on_bgm_changed(gw, state=None):
    """Point the announcer at the message belonging to the playing bed.

    Called whenever a bed starts or stops. No bed playing, no message for it,
    or the master switch off => the announcer goes quiet.
    """
    src = getattr(gw, 'announcer_source', None)
    bgm = getattr(gw, 'bgm_source', None)
    if src is None:
        return False, 'announcer source not available'
    st = state if state is not None else load()

    try:
        gw.config.ANNOUNCER_INTERVAL = float(st['interval'])
        gw.config.BGM_MAX_SECONDS = float(st['max_seconds'])
    except Exception:
        pass

    slot = getattr(bgm, 'playing_slot', None) if bgm else None
    text = st['messages'].get(str(slot), '') if slot is not None else ''

    if not st['enabled'] or slot is None or not text.strip():
        src.set_message_pcm(None)
        src.set_enabled(False)
        return True, ''

    # Per-bed voice, falling back to the legacy global then the engine default.
    voice = valid_voice(gw, st['voices'].get(str(slot)) or st.get('voice', ''))
    pcm, err = _pcm_for(gw, slot, text, voice)
    if err:
        # Disabled rather than enabled-but-mute, so the UI never claims to be
        # announcing silence.
        src.set_message_pcm(None)
        src.set_enabled(False)
        return False, err
    src.set_message_pcm(pcm)
    src.set_enabled(True)
    return True, ''


# Backwards-compatible alias — setup calls this at startup.
def apply(gw, state=None):
    return on_bgm_changed(gw, state)
