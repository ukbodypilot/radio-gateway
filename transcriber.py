"""
Radio Transcriber — VAD-gated voice-to-text using faster-whisper.

Taps audio from the gateway's mixer output. When VAD detects a transmission,
buffers the audio. When VAD closes, transcribes the buffered segment and
stores the result with timestamp and source info.

Results are served via HTTP for the web UI and optionally forwarded to
Mumble/Telegram.
"""

import collections
import json
import math
import os
import numpy as np
import threading
import time

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '.transcribe_settings.json')


def _resolve_freq_tag(gateway, source_id):
    """Look up the current frequency for a bus/source ID. Returns e.g. '446.760' or ''."""
    if not source_id or not gateway:
        return ''
    try:
        sdr = getattr(gateway, 'sdr_plugin', None)
        if sdr and source_id in ('main', 'sdr', 'sdr_rspduo'):
            f1 = getattr(sdr, 'frequency', 0)
            f2 = getattr(sdr, 'frequency2', 0)
            if f1 and f2:
                return f'{f1:.3f}/{f2:.3f}'
            return f'{f1:.3f}' if f1 else ''
        if source_id in ('th9800', 'aioc'):
            cat = getattr(gateway, 'cat_client', None)
            if cat:
                freq = getattr(cat, '_frequency', 0) or getattr(cat, 'frequency', 0)
                if freq:
                    return f'{freq:.3f}' if isinstance(freq, float) else str(freq)
        if source_id == 'kv4p':
            kv = getattr(gateway, 'kv4p_plugin', None)
            if kv:
                return f'{kv._frequency:.3f}'
        if source_id == 'd75':
            for name in getattr(gateway, 'link_endpoints', {}):
                if 'd75' in name.lower():
                    status = getattr(gateway, '_link_last_status', {}).get(name, {})
                    bands = status.get('band', [])
                    if bands:
                        freq = bands[0].get('frequency', '')
                        if freq:
                            return str(freq)
    except Exception:
        pass
    return ''


def _load_saved_settings():
    """Load persisted transcriber settings."""
    try:
        with open(_SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(settings):
    """Persist transcriber settings."""
    try:
        with open(_SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


class RadioTranscriber:
    """VAD-gated audio transcription engine."""

    def __init__(self, config, gateway=None):
        self._config = config
        self._gateway = gateway
        self._model = None
        self._running = False
        self._thread = None

        # Load saved settings (override config defaults)
        _saved = _load_saved_settings()
        self._enabled = _saved.get('enabled', True)

        # VAD state
        self._vad_open = False
        self._vad_envelope = -100.0
        self._vad_threshold = float(_saved.get('vad_threshold', getattr(config, 'TRANSCRIBE_VAD_THRESHOLD', -35)))
        self._vad_hold_time = float(_saved.get('vad_hold', getattr(config, 'TRANSCRIBE_VAD_HOLD', 1.0)))
        self._vad_close_time = 0
        self._min_duration = float(_saved.get('min_duration', getattr(config, 'TRANSCRIBE_MIN_DURATION', 0.5)))

        # Audio buffer (accumulated during open VAD)
        self._audio_buf = []
        self._audio_buf_samples = 0
        self._buf_start_time = 0

        # Results ring buffer
        self._results = collections.deque(maxlen=100)
        self._results_lock = threading.Lock()

        # Performance stats
        self._stats = collections.deque(maxlen=50)
        self._stats_lock = threading.Lock()

        # Pending transcription queue
        self._pending = collections.deque(maxlen=5)
        self._pending_evt = threading.Event()

        # Config (saved settings override config file)
        self._model_size = str(_saved.get('model', getattr(config, 'TRANSCRIBE_MODEL', 'base')))
        self._language = str(_saved.get('language', getattr(config, 'TRANSCRIBE_LANGUAGE', 'en')))
        self._sample_rate = int(getattr(config, 'AUDIO_RATE', 48000))
        self._forward_mumble = _saved.get('forward_mumble', bool(getattr(config, 'TRANSCRIBE_FORWARD_MUMBLE', True)))
        self._forward_telegram = _saved.get('forward_telegram', bool(getattr(config, 'TRANSCRIBE_FORWARD_TELEGRAM', False)))
        self._audio_boost = float(_saved.get('audio_boost', 100)) / 100.0

    def start(self):
        """Start transcriber — loads model in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="Transcriber")
        self._thread.start()
        self._save()
        print(f"  [Transcribe] Started (model={self._model_size}, lang={self._language}, boost={int(self._audio_boost*100)}%)")

    def _save(self):
        """Persist current settings."""
        _save_settings({
            'enabled': self._enabled,
            'mode': 'chunked',
            'model': self._model_size,
            'language': self._language,
            'vad_threshold': self._vad_threshold,
            'vad_hold': self._vad_hold_time,
            'min_duration': self._min_duration,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
        })

    def stop(self):
        self._running = False
        self._pending_evt.set()
        if self._thread:
            self._thread.join(timeout=5)

    def feed(self, pcm_48k, source_id=None):
        """Feed 48kHz 16-bit mono PCM from the mixer. Called every tick (~50ms)."""
        if not self._enabled:
            return
        self._current_source = source_id
        arr = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        db = 20 * math.log10(rms / 32767.0) if rms > 0 else -100.0

        # Envelope follower
        if db > self._vad_envelope:
            self._vad_envelope += (db - self._vad_envelope) * 0.3
        else:
            self._vad_envelope += (db - self._vad_envelope) * 0.05

        now = time.time()

        if self._vad_envelope > self._vad_threshold:
            if not self._vad_open:
                # VAD opens — start buffering
                self._vad_open = True
                self._audio_buf = []
                self._audio_buf_samples = 0
                self._buf_start_time = now
            self._vad_close_time = 0
            self._audio_buf.append(arr)
            self._audio_buf_samples += len(arr)
        else:
            if self._vad_open:
                # Still buffering during hold period
                self._audio_buf.append(arr)
                self._audio_buf_samples += len(arr)
                if self._vad_close_time == 0:
                    self._vad_close_time = now
                elif now - self._vad_close_time > self._vad_hold_time:
                    # VAD closes — submit for transcription
                    self._vad_open = False
                    duration = self._audio_buf_samples / self._sample_rate
                    if duration >= self._min_duration:
                        audio = np.concatenate(self._audio_buf)
                        self._pending.append({
                            'audio': audio,
                            'start_time': self._buf_start_time,
                            'duration': duration,
                            'source_id': getattr(self, '_current_source', None),
                        })
                        self._pending_evt.set()
                    self._audio_buf = []
                    self._audio_buf_samples = 0

    def get_results(self, since=0, limit=50):
        """Return transcription results since timestamp."""
        with self._results_lock:
            results = [r for r in self._results if r['timestamp'] > since]
            return results[-limit:]

    def get_status(self):
        """Return transcriber state."""
        return {
            'running': self._running,
            'enabled': self._enabled,
            'mode': 'chunked',
            'model': self._model_size,
            'language': self._language,
            'model_loaded': self._model is not None,
            'vad_open': self._vad_open,
            'vad_db': round(self._vad_envelope, 1),
            'vad_threshold': self._vad_threshold,
            'vad_hold': self._vad_hold_time,
            'min_duration': self._min_duration,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
            'pending': len(self._pending),
            'total_transcriptions': len(self._results),
            'stats': self.get_stats(),
        }

    def get_stats(self):
        """Return processing performance stats."""
        with self._stats_lock:
            if not self._stats:
                return {'count': 0}
            stats = list(self._stats)
        ratios = [s['ratio'] for s in stats]
        durations = [s['duration'] for s in stats]
        proc_times = [s['proc_time'] for s in stats]
        return {
            'count': len(stats),
            'avg_ratio': round(sum(ratios) / len(ratios), 3),
            'min_ratio': round(min(ratios), 3),
            'max_ratio': round(max(ratios), 3),
            'avg_duration': round(sum(durations) / len(durations), 1),
            'avg_proc_time': round(sum(proc_times) / len(proc_times), 1),
            'realtime_pct': round(sum(1 for r in ratios if r < 1.0) / len(ratios) * 100),
            'recent': stats[-5:],
        }

    # -- Internal --

    def _run(self):
        """Background thread: load model, process pending transcriptions."""
        # Load model
        try:
            from faster_whisper import WhisperModel
            print(f"  [Transcribe] Loading {self._model_size} model...")
            self._model = WhisperModel(self._model_size, device='cpu',
                                        compute_type='int8')
            print(f"  [Transcribe] Model loaded")
        except Exception as e:
            print(f"  [Transcribe] Failed to load model: {e}")
            self._running = False
            return

        while self._running:
            self._pending_evt.wait(timeout=1.0)
            self._pending_evt.clear()

            while self._pending and self._running:
                item = self._pending.popleft()
                try:
                    _t0 = time.monotonic()
                    text = self._transcribe(item['audio'])
                    _proc_time = time.monotonic() - _t0
                    _duration = item['duration']
                    _ratio = _proc_time / _duration if _duration > 0 else 0
                    _stat = {
                        'timestamp': item['start_time'],
                        'duration': round(_duration, 2),
                        'proc_time': round(_proc_time, 2),
                        'ratio': round(_ratio, 3),
                        'realtime': _ratio < 1.0,
                        'samples': len(item['audio']),
                        'text_len': len(text.strip()) if text else 0,
                    }
                    with self._stats_lock:
                        self._stats.append(_stat)
                    print(f"  [Transcribe] {_duration:.1f}s audio → {_proc_time:.1f}s process ({_ratio:.2f}x realtime)")
                    if text and text.strip():
                        freq_tag = _resolve_freq_tag(self._gateway, item.get('source_id'))
                        result = {
                            'timestamp': item['start_time'],
                            'duration': round(item['duration'], 1),
                            'proc_time': round(_proc_time, 1),
                            'ratio': round(_ratio, 2),
                            'text': text.strip(),
                            'freq': freq_tag,
                            'source': item.get('source_id', ''),
                            'time_str': time.strftime('%H:%M:%S',
                                                      time.localtime(item['start_time'])),
                        }
                        with self._results_lock:
                            self._results.append(result)
                        _freq_prefix = f'[{freq_tag}] ' if freq_tag else ''
                        print(f"  [Transcribe] [{result['time_str']}] "
                              f"{_freq_prefix}({result['duration']}s) {result['text']}")

                        # Forward to Mumble chat
                        if self._forward_mumble and self._gateway and self._gateway.mumble:
                            try:
                                self._gateway.send_text_message(
                                    f"[{result['time_str']}] {_freq_prefix}{result['text']}")
                            except Exception:
                                pass
                        # Forward to Telegram
                        if self._forward_telegram and self._gateway:
                            try:
                                import urllib.request
                                _tg_url = f"http://127.0.0.1:8080/telegram_send"
                                _tg_data = json.dumps({
                                    'text': f"[{result['time_str']}] {result['text']}"
                                }).encode()
                                urllib.request.urlopen(
                                    urllib.request.Request(_tg_url, data=_tg_data,
                                        headers={'Content-Type': 'application/json'}),
                                    timeout=5)
                            except Exception:
                                pass
                except Exception as e:
                    print(f"  [Transcribe] Error: {e}")

    def _transcribe(self, audio_48k):
        """Transcribe a numpy float32 audio array (48kHz) → text string."""
        if self._model is None:
            return None

        # Downsample 48kHz → 16kHz (Whisper expects 16kHz)
        # Simple decimation by 3
        audio_16k = audio_48k[::3].copy()

        # Apply audio boost
        if self._audio_boost != 1.0:
            audio_16k = np.clip(audio_16k * self._audio_boost, -32768, 32767)

        # Normalize to [-1, 1] float32
        audio_16k = audio_16k / 32768.0

        segments, info = self._model.transcribe(
            audio_16k,
            language=self._language,
            beam_size=3,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=300,
                speech_pad_ms=200,
            ),
        )

        text_parts = []
        for segment in segments:
            text_parts.append(segment.text)

        return ' '.join(text_parts)


class StreamingTranscriber:
    """Rolling-buffer streaming transcription using faster-whisper.

    Instead of waiting for VAD close, continuously accumulates audio and
    re-transcribes the buffer every few seconds. Produces partial results
    that update in real-time, plus final results when silence is detected.

    Uses the same interface as RadioTranscriber (feed/get_results/get_status)
    so it's a drop-in replacement.
    """

    def __init__(self, config, gateway=None):
        self._config = config
        self._gateway = gateway
        self._model = None
        self._running = False
        self._thread = None

        # Load saved settings
        _saved = _load_saved_settings()
        self._enabled = _saved.get('enabled', True)

        # Config (saved settings override config file)
        self._model_size = str(_saved.get('model', getattr(config, 'TRANSCRIBE_MODEL', 'base')))
        self._language = str(_saved.get('language', getattr(config, 'TRANSCRIBE_LANGUAGE', 'en')))
        self._sample_rate = int(getattr(config, 'AUDIO_RATE', 48000))
        self._forward_mumble = _saved.get('forward_mumble', bool(getattr(config, 'TRANSCRIBE_FORWARD_MUMBLE', True)))
        self._forward_telegram = _saved.get('forward_telegram', bool(getattr(config, 'TRANSCRIBE_FORWARD_TELEGRAM', False)))
        self._audio_boost = float(_saved.get('audio_boost', 100)) / 100.0

        # Streaming config
        self._transcribe_interval = float(getattr(config, 'TRANSCRIBE_STREAM_INTERVAL', 3.0))
        self._max_buffer_secs = float(getattr(config, 'TRANSCRIBE_MAX_BUFFER', 15.0))
        self._silence_threshold = float(_saved.get('vad_threshold', getattr(config, 'TRANSCRIBE_VAD_THRESHOLD', -35)))
        self._silence_duration = float(_saved.get('vad_hold', getattr(config, 'TRANSCRIBE_VAD_HOLD', 1.5)))

        # Audio buffer (rolling)
        self._audio_lock = threading.Lock()
        self._audio_buf = np.array([], dtype=np.float32)
        self._buf_start_time = 0
        self._last_speech_time = 0
        self._vad_envelope = -100.0
        self._vad_open = False
        self._finalize_pending = False

        # Results
        self._results = collections.deque(maxlen=100)
        self._results_lock = threading.Lock()
        self._partial_text = ''  # current partial (not yet finalized)
        self._partial_time = 0

        # Stats
        self._stats = collections.deque(maxlen=50)
        self._stats_lock = threading.Lock()

        # Transcription event
        self._new_audio = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="StreamTranscribe")
        self._thread.start()
        self._save()
        print(f"  [StreamTranscribe] Started (model={self._model_size}, "
              f"interval={self._transcribe_interval}s)")

    def _save(self):
        _save_settings({
            'enabled': self._enabled,
            'mode': 'streaming',
            'model': self._model_size,
            'language': self._language,
            'vad_threshold': self._silence_threshold,
            'vad_hold': self._silence_duration,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
        })

    def stop(self):
        self._running = False
        self._new_audio.set()
        if self._thread:
            self._thread.join(timeout=5)

    def feed(self, pcm_48k, source_id=None):
        """Feed 48kHz 16-bit mono PCM from the bus sink."""
        if not self._enabled:
            return
        self._current_source = source_id
        arr = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        db = 20 * math.log10(rms / 32767.0) if rms > 0 else -100.0

        # Envelope follower
        if db > self._vad_envelope:
            self._vad_envelope += (db - self._vad_envelope) * 0.3
        else:
            self._vad_envelope += (db - self._vad_envelope) * 0.05

        now = time.time()
        has_speech = self._vad_envelope > self._silence_threshold

        if has_speech:
            self._last_speech_time = now
            if not self._vad_open:
                self._vad_open = True
                self._buf_start_time = now

        with self._audio_lock:
            self._audio_buf = np.concatenate([self._audio_buf, arr])
            _buf_duration = len(self._audio_buf) / self._sample_rate

        if has_speech:
            self._new_audio.set()

        # Force finalization when buffer gets too long (long continuous TX)
        if _buf_duration >= self._max_buffer_secs and self._partial_text.strip():
            self._finalize_pending = True
            self._new_audio.set()
            return

        # Detect end of speech — signal finalization to the transcribe thread
        if self._vad_open and not has_speech:
            if now - self._last_speech_time > self._silence_duration:
                self._vad_open = False
                self._finalize_pending = True
                self._new_audio.set()

    def get_results(self, since=0, limit=50):
        with self._results_lock:
            results = [r for r in self._results if r['timestamp'] > since]
            # Include current partial as a special entry
            if self._partial_text.strip() and self._vad_open:
                partial = {
                    'timestamp': self._partial_time or time.time(),
                    'duration': 0,
                    'proc_time': 0,
                    'ratio': 0,
                    'text': self._partial_text.strip(),
                    'time_str': time.strftime('%H:%M:%S',
                                              time.localtime(self._partial_time or time.time())),
                    'partial': True,
                }
                results.append(partial)
            return results[-limit:]

    def get_status(self):
        return {
            'running': self._running,
            'enabled': self._enabled,
            'mode': 'streaming',
            'model': self._model_size,
            'language': self._language,
            'model_loaded': self._model is not None,
            'vad_open': self._vad_open,
            'vad_db': round(self._vad_envelope, 1),
            'vad_threshold': self._silence_threshold,
            'vad_hold': self._silence_duration,
            'min_duration': 0,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
            'stream_interval': self._transcribe_interval,
            'buffer_secs': round(len(self._audio_buf) / self._sample_rate, 1),
            'pending': 0,
            'total_transcriptions': len(self._results),
            'partial': self._partial_text[:80] if self._partial_text else '',
            'stats': self.get_stats(),
        }

    def get_stats(self):
        with self._stats_lock:
            if not self._stats:
                return {'count': 0}
            stats = list(self._stats)
        ratios = [s['ratio'] for s in stats]
        durations = [s['duration'] for s in stats]
        proc_times = [s['proc_time'] for s in stats]
        return {
            'count': len(stats),
            'avg_ratio': round(sum(ratios) / len(ratios), 3),
            'min_ratio': round(min(ratios), 3),
            'max_ratio': round(max(ratios), 3),
            'avg_duration': round(sum(durations) / len(durations), 1),
            'avg_proc_time': round(sum(proc_times) / len(proc_times), 1),
            'realtime_pct': round(sum(1 for r in ratios if r < 1.0) / len(ratios) * 100),
            'recent': stats[-5:],
        }

    # -- Internal --

    def _run(self):
        try:
            from faster_whisper import WhisperModel
            print(f"  [StreamTranscribe] Loading {self._model_size} model...")
            self._model = WhisperModel(self._model_size, device='cpu',
                                        compute_type='int8')
            print(f"  [StreamTranscribe] Model loaded")
        except Exception as e:
            print(f"  [StreamTranscribe] Failed to load model: {e}")
            self._running = False
            return

        while self._running:
            self._new_audio.wait(timeout=self._transcribe_interval)
            self._new_audio.clear()

            # Check for finalization request
            if self._finalize_pending:
                self._finalize_pending = False
                # Do one final transcribe of the complete buffer
                with self._audio_lock:
                    audio = self._audio_buf.copy()
                    self._audio_buf = np.array([], dtype=np.float32)
                if len(audio) >= self._sample_rate * 0.5:
                    t0 = time.monotonic()
                    text = self._transcribe(audio)
                    proc_time = time.monotonic() - t0
                    duration = len(audio) / self._sample_rate
                    ratio = proc_time / duration if duration > 0 else 0
                    with self._stats_lock:
                        self._stats.append({
                            'timestamp': time.time(),
                            'duration': round(duration, 2),
                            'proc_time': round(proc_time, 2),
                            'ratio': round(ratio, 3),
                            'realtime': ratio < 1.0,
                            'samples': len(audio),
                            'text_len': len(text) if text else 0,
                        })
                    if text and text.strip():
                        self._partial_text = text.strip()
                        self._partial_time = self._buf_start_time or time.time()
                self._finalize_result()
                self._buf_start_time = time.time()  # fresh timestamp for next segment
                continue

            if not self._vad_open and len(self._audio_buf) == 0:
                continue

            with self._audio_lock:
                if len(self._audio_buf) < self._sample_rate * 0.5:
                    continue
                audio = self._audio_buf.copy()

            duration = len(audio) / self._sample_rate
            t0 = time.monotonic()
            text = self._transcribe(audio)
            proc_time = time.monotonic() - t0
            ratio = proc_time / duration if duration > 0 else 0

            with self._stats_lock:
                self._stats.append({
                    'timestamp': time.time(),
                    'duration': round(duration, 2),
                    'proc_time': round(proc_time, 2),
                    'ratio': round(ratio, 3),
                    'realtime': ratio < 1.0,
                    'samples': len(audio),
                    'text_len': len(text) if text else 0,
                })

            if text and text.strip():
                self._partial_text = text.strip()
                self._partial_time = self._buf_start_time or time.time()
                print(f"  [StreamTranscribe] partial ({duration:.1f}s→{proc_time:.1f}s "
                      f"{ratio:.2f}x): {text.strip()[:60]}")

    def _transcribe(self, audio_48k):
        if self._model is None:
            return None
        audio_16k = audio_48k[::3].copy()
        if self._audio_boost != 1.0:
            audio_16k = np.clip(audio_16k * self._audio_boost, -32768, 32767)
        audio_16k = audio_16k / 32768.0
        segments, info = self._model.transcribe(
            audio_16k, language=self._language,
            beam_size=1,  # faster for streaming
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=200),
        )
        return ' '.join(s.text for s in segments)

    def _finalize_result(self):
        """Convert partial to completed result."""
        freq_tag = _resolve_freq_tag(self._gateway, getattr(self, '_current_source', None))
        result = {
            'timestamp': self._partial_time or time.time(),
            'duration': 0,
            'proc_time': 0,
            'ratio': 0,
            'text': self._partial_text.strip(),
            'freq': freq_tag,
            'source': getattr(self, '_current_source', ''),
            'time_str': time.strftime('%H:%M:%S',
                                      time.localtime(self._partial_time or time.time())),
        }
        with self._results_lock:
            self._results.append(result)
        _freq_prefix = f'[{freq_tag}] ' if freq_tag else ''
        print(f"  [StreamTranscribe] FINAL: [{result['time_str']}] {_freq_prefix}{result['text'][:60]}")

        if self._forward_mumble and self._gateway and self._gateway.mumble:
            try:
                self._gateway.send_text_message(
                    f"[{result['time_str']}] {_freq_prefix}{result['text']}")
            except Exception:
                pass
        if self._forward_telegram and self._gateway:
            try:
                import urllib.request
                urllib.request.urlopen(
                    urllib.request.Request(
                        'http://127.0.0.1:8080/telegram_send',
                        data=json.dumps({'text': f"[{result['time_str']}] {_freq_prefix}{result['text']}"}).encode(),
                        headers={'Content-Type': 'application/json'}),
                    timeout=5)
            except Exception:
                pass

        self._partial_text = ''
        self._partial_time = 0
