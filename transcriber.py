"""
Radio Transcriber — VAD-gated voice-to-text using Moonshine + Silero VAD.

Taps audio from the gateway's mixer output. Silero VAD (speech classifier)
decides when speech starts/stops. Buffered audio is transcribed with Moonshine
(CPU-efficient ONNX ASR) when VAD closes, then stored with timestamp and
source info.

Results are served via HTTP for the web UI and optionally forwarded to
Mumble/Telegram.
"""

import collections
import json
import logging
import os
import numpy as np
import threading
import time

from audio_util import pcm_db, _RNNoiseStream

# Silence huggingface_hub's unauthenticated-download warning — it fires on
# every cache hit and clutters the gateway log. We don't need HF auth for
# public Moonshine weights.
logging.getLogger('huggingface_hub').setLevel(logging.ERROR)

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '.transcribe_settings.json')

# Moonshine's encoder allocates proportional to input length. Hard-cap
# utterance buffers at 60s (package asserts max 64s) to prevent OOM on stuck-
# open VAD (squelched repeater, continuous tone). Between the soft and hard
# cap, cut on the first probability dip to split at a natural pause rather
# than mid-word.
_MAX_UTTERANCE_SECS = 60.0
_SOFT_CAP_SECS = 50.0

# Silero VAD constants. 16 kHz, 512-sample frames (32 ms/frame), 64-sample
# context window carried between calls.
_SILERO_SR = 16000
_SILERO_FRAME = 512
_SILERO_CONTEXT = 64

# Hysteresis: exit threshold = enter − 0.15. Prevents rapid toggling on
# boundary speech.
_VAD_HYSTERESIS = 0.15

# Drop these exact phrases (case-insensitive, stripped) — common no-speech
# hallucinations from ASR models trained on YouTube captions.
_HALLUCINATION_BLOCKLIST = frozenset([
    '',
    'you',
    'thanks for watching',
    'thank you for watching',
    'thank you',
    'please subscribe',
    'subscribe',
    'bye',
    'bye-bye',
    'okay',
    'ok',
])

# Strip these trailing characters before blocklist lookup so punctuation
# variants ("Okay.", "okay!") all normalise to the same key.
_HALLUCINATION_STRIP = ' \t\n\r.,!?'


def _is_hallucination(text):
    """True if text matches a known ASR hallucination phrase."""
    return text.strip(_HALLUCINATION_STRIP).lower() in _HALLUCINATION_BLOCKLIST


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
                # TH-9800 always receives on the LEFT VFO. _vfo_text is the
                # 6-char display, usually kHz (e.g. "147435" → 147.435 MHz)
                # but can also be dotted MHz. Normalise both.
                text = (getattr(cat, '_vfo_text', {}) or {}).get('LEFT', '').strip()
                if text:
                    digits = text.replace('.', '').lstrip('0') or '0'
                    try:
                        khz = int(digits)
                        return f'{khz / 1000:.3f}'
                    except ValueError:
                        return text
        if source_id == 'kv4p':
            kv = getattr(gateway, 'kv4p_plugin', None)
            if kv:
                return f'{kv._frequency:.3f}'
        for name, _ep_src in getattr(gateway, 'link_endpoints', {}).items():
            if getattr(_ep_src, 'source_id', None) == source_id:
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
    """Load persisted transcriber settings. Unknown keys are silently ignored."""
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


def _resample_48k_to_16k(audio_48k_f32):
    """Polyphase resample 48kHz float32 → 16kHz with proper anti-aliasing."""
    from scipy.signal import resample_poly
    return resample_poly(audio_48k_f32, 1, 3).astype(np.float32)


class _SileroVAD:
    """Pure-numpy + onnxruntime wrapper around Silero's bundled ONNX model.

    Skips the silero_vad package's torch-dependent loader. Requires the
    silero-vad pip package installed (for the bundled .onnx file path) but
    does not import it directly.
    """

    def __init__(self):
        import onnxruntime as ort
        # Locate the bundled ONNX file without importing silero_vad (which
        # pulls torch). importlib.resources gives us the file path.
        try:
            from importlib.resources import files
            model_path = str(files('silero_vad.data').joinpath('silero_vad.onnx'))
        except Exception:
            # Fallback to known site-packages layout.
            import silero_vad.data as _d
            model_path = os.path.join(os.path.dirname(_d.__file__), 'silero_vad.onnx')

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._sess = ort.InferenceSession(
            model_path, providers=['CPUExecutionProvider'], sess_options=opts)
        self._sr = np.array(_SILERO_SR, dtype=np.int64)
        self.reset()

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _SILERO_CONTEXT), dtype=np.float32)

    def probability(self, frame_16k):
        """Run Silero on one 512-sample float32 frame. Returns speech probability [0,1]."""
        x = np.concatenate([self._context, frame_16k[None, :]], axis=1)
        out, new_state = self._sess.run(
            None, {'input': x, 'state': self._state, 'sr': self._sr})
        self._state = new_state
        self._context = x[:, -_SILERO_CONTEXT:]
        return float(out[0, 0])


class RadioTranscriber:
    """Silero-gated, Moonshine-powered audio transcription engine."""

    def __init__(self, config, gateway=None):
        self._config = config
        self._gateway = gateway
        self._model = None
        self._tokenizer = None
        self._vad = None
        self._running = False
        self._thread = None

        _saved = _load_saved_settings()
        self._enabled = _saved.get('enabled', True)

        # VAD config — threshold is now a probability [0.0–1.0].
        # Legacy dB values (<0) from pre-A2 saves are silently reset.
        _raw_thresh = float(_saved.get('vad_threshold',
                                       getattr(config, 'TRANSCRIBE_VAD_THRESHOLD', 0.5)))
        if _raw_thresh < 0 or _raw_thresh > 1:
            print(f"  [Transcribe] Ignoring legacy vad_threshold={_raw_thresh}; "
                  f"using default 0.5 (threshold is now a probability 0.0-1.0)")
            _raw_thresh = 0.5
        self._vad_threshold = _raw_thresh
        self._vad_hold_time = float(_saved.get('vad_hold', getattr(config, 'TRANSCRIBE_VAD_HOLD', 1.0)))
        self._min_duration = float(_saved.get('min_duration', getattr(config, 'TRANSCRIBE_MIN_DURATION', 0.5)))

        # VAD state
        self._vad_open = False
        self._vad_close_time = 0
        self._vad_last_prob = 0.0
        self._vad_prob_env = 0.0  # smoothed prob for display (fast attack, slow decay)
        self._vad_envelope = -100.0  # dBFS for display only

        # Accumulator for resampled 16 kHz audio before dispatching to Silero
        # in 512-sample frames.
        self._frame_acc = np.zeros(0, dtype=np.float32)

        # Utterance audio buffer (16 kHz, float32, normalized [-1, 1])
        self._audio_buf_16k = []
        self._audio_buf_samples = 0
        self._buf_start_time = 0

        self._results = collections.deque(maxlen=100)
        self._results_lock = threading.Lock()

        self._stats = collections.deque(maxlen=50)
        self._stats_lock = threading.Lock()

        self._pending = collections.deque(maxlen=5)
        self._pending_evt = threading.Event()

        _raw_model = str(_saved.get('model', getattr(config, 'TRANSCRIBE_MODEL', 'base')))
        self._model_size = _raw_model if _raw_model in ('tiny', 'base') else 'base'
        self._sample_rate = int(getattr(config, 'AUDIO_RATE', 48000))
        self._forward_mumble = _saved.get('forward_mumble', bool(getattr(config, 'TRANSCRIBE_FORWARD_MUMBLE', True)))
        self._forward_telegram = _saved.get('forward_telegram', bool(getattr(config, 'TRANSCRIBE_FORWARD_TELEGRAM', False)))
        self._audio_boost = float(_saved.get('audio_boost', 100)) / 100.0
        self._denoise_enabled = bool(_saved.get('denoise',
                                               getattr(config, 'TRANSCRIBE_DENOISE', False)))
        # Wet/dry mix — 1.0 = fully denoised, 0.0 = pass-through. RNNoise
        # over-cuts on radio audio; 0.5 leaves voice audible while knocking
        # the noise floor down ~6 dB.
        _raw_mix = float(_saved.get('denoise_mix',
                                    getattr(config, 'TRANSCRIBE_DENOISE_MIX', 0.5)))
        self._denoise_mix = max(0.0, min(1.0, _raw_mix))
        self._denoise_stream = None
        # Guards all three denoise fields: writes from HTTP/MCP threads race
        # with reads/writes from the bus tick thread in feed().
        self._denoise_lock = threading.Lock()

        self._max_samples_16k = int(_MAX_UTTERANCE_SECS * _SILERO_SR)
        self._soft_cap_samples_16k = int(_SOFT_CAP_SECS * _SILERO_SR)

    def start(self):
        """Start transcriber — loads model in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="Transcriber")
        self._thread.start()
        self._save()
        print(f"  [Transcribe] Started (model=moonshine/{self._model_size}, "
              f"vad_thresh={self._vad_threshold:.2f}, boost={int(self._audio_boost*100)}%)")

    def _save(self):
        _save_settings({
            'enabled': self._enabled,
            'model': self._model_size,
            'vad_threshold': self._vad_threshold,
            'vad_hold': self._vad_hold_time,
            'min_duration': self._min_duration,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
            'denoise': self._denoise_enabled,
            'denoise_mix': self._denoise_mix,
        })

    def set_denoise(self, enabled):
        """Toggle neural denoise on the ASR path. Tears down the stream when
        disabled so we don't keep an unused RNNoise state in memory."""
        enabled = bool(enabled)
        with self._denoise_lock:
            if enabled == self._denoise_enabled:
                return
            self._denoise_enabled = enabled
            if not enabled and self._denoise_stream is not None:
                try:
                    self._denoise_stream.close()
                except Exception:
                    pass
                self._denoise_stream = None
        self._save()

    def set_denoise_mix(self, mix):
        """Set denoise wet/dry mix (0.0–1.0)."""
        with self._denoise_lock:
            self._denoise_mix = max(0.0, min(1.0, float(mix)))
        self._save()

    def stop(self):
        self._running = False
        self._pending_evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        # Release ONNX + RNNoise resources so repeated start/stop cycles
        # don't accumulate sessions in memory.
        with self._denoise_lock:
            if self._denoise_stream is not None:
                try:
                    self._denoise_stream.close()
                except Exception:
                    pass
                self._denoise_stream = None
        if self._vad is not None:
            try:
                self._vad._sess = None
            except Exception:
                pass
            self._vad = None

    def feed(self, pcm_48k, source_id=None):
        """Feed 48kHz 16-bit mono PCM from the mixer. Called every tick (~50ms)."""
        if not self._enabled or self._vad is None:
            return
        self._current_source = source_id

        # dBFS envelope — display only, not used for gating.
        db = pcm_db(pcm_48k)
        if db > self._vad_envelope:
            self._vad_envelope += (db - self._vad_envelope) * 0.3
        else:
            self._vad_envelope += (db - self._vad_envelope) * 0.05

        # Optional neural denoise at 48 kHz (RNNoise's native rate) before
        # resampling. Cleans both VAD input and the utterance buffer in one
        # pass. Lazy-load the stream on first enable; silently disable on
        # library failure so a missing dep never kills the audio path.
        arr_48k_i16 = np.frombuffer(pcm_48k, dtype=np.int16)
        # Snapshot denoise state under the lock so a concurrent
        # set_denoise(False) can't pull the stream out from under us mid-call.
        # Lazy-init the RNNoise stream on first enable while holding the lock.
        with self._denoise_lock:
            _den_enabled = self._denoise_enabled
            _den_mix = self._denoise_mix
            _den_stream = self._denoise_stream
            if _den_enabled and _den_stream is None:
                try:
                    _den_stream = _RNNoiseStream()
                    self._denoise_stream = _den_stream
                except Exception as e:
                    print(f"  [Transcribe] Denoise unavailable: {e}")
                    self._denoise_enabled = False
                    _den_enabled = False
                    _den_stream = None
        if _den_enabled and _den_stream is not None:
            try:
                denoised = _den_stream.process(arr_48k_i16)
                # Align lengths (startup residue → dry-fill the gap),
                # then blend wet/dry per _denoise_mix so we don't wipe
                # out the signal when RNNoise mis-classifies the band.
                if denoised.size == 0:
                    pass  # nothing processed yet; keep dry input
                else:
                    if denoised.size < arr_48k_i16.size:
                        denoised = np.concatenate([denoised, arr_48k_i16[denoised.size:]])
                    elif denoised.size > arr_48k_i16.size:
                        denoised = denoised[: arr_48k_i16.size]
                    w = _den_mix
                    if w >= 0.999:
                        arr_48k_i16 = denoised
                    elif w > 0.001:
                        mixed = (arr_48k_i16.astype(np.int32) * (1.0 - w)
                                 + denoised.astype(np.int32) * w)
                        arr_48k_i16 = np.clip(mixed, -32768, 32767).astype(np.int16)
                    # else w ≈ 0 → keep dry
            except Exception as e:
                print(f"  [Transcribe] Denoise error: {e}")

        # Convert to float32 [-1, 1] and resample once.
        arr_48k = arr_48k_i16.astype(np.float32) / 32768.0
        arr_16k = _resample_48k_to_16k(arr_48k)
        if self._audio_boost != 1.0:
            # Soft-clip via tanh: preserves headroom and avoids the square-wave
            # harmonics that hard-clipping introduces, which Silero's v5 model
            # (trained on natural speech) mishandles.
            arr_16k = np.tanh(arr_16k * self._audio_boost).astype(np.float32)

        # Append to frame accumulator; process 512-sample frames until drained.
        self._frame_acc = np.concatenate([self._frame_acc, arr_16k])
        while len(self._frame_acc) >= _SILERO_FRAME:
            frame = self._frame_acc[:_SILERO_FRAME]
            self._frame_acc = self._frame_acc[_SILERO_FRAME:]
            self._process_frame(frame)

    def _process_frame(self, frame_16k):
        """Run Silero on one 32 ms frame and drive VAD state."""
        prob = self._vad.probability(frame_16k)
        self._vad_last_prob = prob
        # Smoothed envelope for the UI bar: fast attack, slow decay so the
        # status poll (every ~2s) catches peaks rather than silence gaps.
        if prob > self._vad_prob_env:
            self._vad_prob_env += (prob - self._vad_prob_env) * 0.5
        else:
            self._vad_prob_env += (prob - self._vad_prob_env) * 0.05
        now = time.time()
        exit_thresh = max(0.0, self._vad_threshold - _VAD_HYSTERESIS)

        if self._vad_open:
            # Always buffer while VAD is open.
            self._audio_buf_16k.append(frame_16k.copy())
            self._audio_buf_samples += _SILERO_FRAME

            # Hard 60s cap — force close to stay under Moonshine's 64s limit.
            if self._audio_buf_samples >= self._max_samples_16k:
                self._submit_utterance()
                return

            # Inside the soft-cap zone, take any probability dip as a cut
            # point so long utterances split on natural pauses rather than
            # mid-word at the hard cap. If speech continues, VAD re-opens on
            # the next above-threshold frame with no audio lost.
            if (self._audio_buf_samples >= self._soft_cap_samples_16k
                    and prob < exit_thresh):
                self._submit_utterance()
                return

            if prob < exit_thresh:
                if self._vad_close_time == 0:
                    self._vad_close_time = now
                elif now - self._vad_close_time > self._vad_hold_time:
                    self._submit_utterance()
            else:
                # Speech resumed during hold window — reset close timer.
                self._vad_close_time = 0
        else:
            if prob >= self._vad_threshold:
                self._vad_open = True
                self._vad_close_time = 0
                self._audio_buf_16k = [frame_16k.copy()]
                self._audio_buf_samples = _SILERO_FRAME
                self._buf_start_time = now

    def _submit_utterance(self):
        """Finalize the current utterance and queue it for transcription."""
        self._vad_open = False
        self._vad_close_time = 0
        duration = self._audio_buf_samples / _SILERO_SR
        if duration >= self._min_duration and self._audio_buf_16k:
            audio_16k = np.concatenate(self._audio_buf_16k)
            self._pending.append({
                'audio_16k': audio_16k,
                'start_time': self._buf_start_time,
                'duration': duration,
                'source_id': getattr(self, '_current_source', None),
            })
            self._pending_evt.set()
        self._audio_buf_16k = []
        self._audio_buf_samples = 0

    def get_results(self, since=0, limit=50):
        with self._results_lock:
            results = [r for r in self._results if r['timestamp'] > since]
            return results[-limit:]

    def get_status(self):
        return {
            'running': self._running,
            'enabled': self._enabled,
            'engine': 'moonshine',
            'vad_engine': 'silero',
            'model': self._model_size,
            'model_loaded': self._model is not None,
            'vad_open': self._vad_open,
            'vad_prob': round(max(self._vad_last_prob, self._vad_prob_env), 3),
            'vad_db': round(self._vad_envelope, 1),
            'vad_threshold': self._vad_threshold,
            'vad_hold': self._vad_hold_time,
            'min_duration': self._min_duration,
            'forward_mumble': self._forward_mumble,
            'forward_telegram': self._forward_telegram,
            'audio_boost': int(self._audio_boost * 100),
            'denoise': self._denoise_enabled,
            'denoise_mix': round(self._denoise_mix, 2),
            'pending': len(self._pending),
            'total_transcriptions': len(self._results),
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
        """Background thread: load models, process pending transcriptions."""
        try:
            self._vad = _SileroVAD()
            print(f"  [Transcribe] Silero VAD loaded")
        except Exception as e:
            print(f"  [Transcribe] Failed to load Silero VAD: {e}")
            self._running = False
            return

        try:
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
            print(f"  [Transcribe] Loading moonshine/{self._model_size} model...")
            self._model = MoonshineOnnxModel(model_name=f'moonshine/{self._model_size}')
            self._tokenizer = load_tokenizer()
            print(f"  [Transcribe] Model loaded")
        except Exception as e:
            print(f"  [Transcribe] Failed to load Moonshine: {e}")
            self._running = False
            return

        while self._running:
            self._pending_evt.wait(timeout=1.0)
            self._pending_evt.clear()

            while self._pending and self._running:
                item = self._pending.popleft()
                try:
                    _t0 = time.monotonic()
                    text = self._transcribe(item['audio_16k'])
                    _proc_time = time.monotonic() - _t0
                    _duration = item['duration']
                    _ratio = _proc_time / _duration if _duration > 0 else 0
                    _stat = {
                        'timestamp': item['start_time'],
                        'duration': round(_duration, 2),
                        'proc_time': round(_proc_time, 2),
                        'ratio': round(_ratio, 3),
                        'realtime': _ratio < 1.0,
                        'samples': len(item['audio_16k']),
                        'text_len': len(text.strip()) if text else 0,
                    }
                    with self._stats_lock:
                        self._stats.append(_stat)
                    print(f"  [Transcribe] {_duration:.1f}s audio → {_proc_time:.1f}s process ({_ratio:.2f}x realtime)")
                    if text and text.strip() and not _is_hallucination(text):
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

                        if self._forward_mumble and self._gateway and self._gateway.mumble:
                            try:
                                self._gateway.send_text_message(
                                    f"[{result['time_str']}] {_freq_prefix}{result['text']}")
                            except Exception:
                                pass
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

    def _transcribe(self, audio_16k):
        """Transcribe a numpy float32 audio array (16kHz, [-1, 1]) → text string."""
        if self._model is None or self._tokenizer is None:
            return None
        tokens = self._model.generate(audio_16k.astype(np.float32)[None, :])
        decoded = self._tokenizer.decode_batch(tokens)
        return decoded[0] if decoded else ''
