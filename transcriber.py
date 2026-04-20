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


def _bus_sdr_sources(gateway, bus_id):
    """Return the set of SDR source ids ('sdr1'/'sdr2') wired to this bus."""
    try:
        bm = getattr(gateway, 'bus_manager', None)
        if not bm:
            return set()
        bus = bm._busses.get(bus_id) if hasattr(bm, '_busses') else None
        if not bus:
            return set()
        out = set()
        for slot in getattr(bus, 'source_slots', []):
            rid = getattr(slot, 'routing_id', None) or getattr(slot.source, 'name', '')
            if rid in ('sdr1', 'sdr2'):
                out.add(rid)
        return out
    except Exception:
        return set()


def _resolve_freq_tag(gateway, source_id):
    """Look up the current frequency for a bus/source ID. Returns e.g. '446.760' or ''."""
    if not source_id or not gateway:
        return ''
    try:
        sdr = getattr(gateway, 'sdr_plugin', None)
        # Per-tuner attribution (preferred — comes from dominant-source tracking).
        if sdr and source_id == 'sdr1':
            f1 = getattr(sdr, 'frequency', 0)
            return f'{f1:.3f}' if f1 else ''
        if sdr and source_id == 'sdr2':
            f2 = getattr(sdr, 'frequency2', 0)
            return f'{f2:.3f}' if f2 else ''
        # Bus-id fallback: only emit freqs for SDR tuners actually wired to
        # this bus, not whatever the plugin has tuned. Avoids reporting two
        # frequencies when only one tuner is routed.
        if source_id in ('main', 'sdr', 'sdr_rspduo'):
            wired = _bus_sdr_sources(gateway, source_id)
            if not wired and sdr and source_id != 'main':
                # Legacy 'sdr'/'sdr_rspduo' ids with no routing info — keep
                # old behaviour as a last resort.
                f1 = getattr(sdr, 'frequency', 0)
                f2 = getattr(sdr, 'frequency2', 0)
                if f1 and f2:
                    return f'{f1:.3f}/{f2:.3f}'
                return f'{f1:.3f}' if f1 else ''
            freqs = []
            if 'sdr1' in wired:
                f1 = getattr(sdr, 'frequency', 0)
                if f1:
                    freqs.append(f'{f1:.3f}')
            if 'sdr2' in wired:
                f2 = getattr(sdr, 'frequency2', 0)
                if f2:
                    freqs.append(f'{f2:.3f}')
            return '/'.join(freqs)
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

    The ONNX session is shared class-wide — first instance loads it, subsequent
    instances reuse. Only the per-instance state tensor (_state / _context)
    is kept per Silero stream, so one instance per bus is cheap.
    """

    _sess = None  # shared InferenceSession across instances
    _sr = np.array(_SILERO_SR, dtype=np.int64)

    @classmethod
    def _ensure_session(cls):
        if cls._sess is not None:
            return
        import onnxruntime as ort
        try:
            from importlib.resources import files
            model_path = str(files('silero_vad.data').joinpath('silero_vad.onnx'))
        except Exception:
            import silero_vad.data as _d
            model_path = os.path.join(os.path.dirname(_d.__file__), 'silero_vad.onnx')
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        cls._sess = ort.InferenceSession(
            model_path, providers=['CPUExecutionProvider'], sess_options=opts)

    def __init__(self):
        self._ensure_session()
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


class _StreamState:
    """Per-bus transcription pipeline state.

    One instance per bus wired to the transcription sink. Each holds its own
    Silero VAD context, frame accumulator, utterance buffer, and RNNoise
    state — so two buses feeding the sink in the same tick don't scramble
    each other's audio or VAD timing.
    """
    __slots__ = (
        'vad', 'denoise_stream',
        'frame_acc', 'audio_buf_16k', 'audio_buf_samples', 'buf_start_time',
        'vad_open', 'vad_close_time',
        'vad_last_prob', 'vad_prob_env', 'vad_prob_peak', 'vad_envelope',
        'upstream_counts', 'last_upstream_source',
    )

    def __init__(self):
        self.vad = _SileroVAD()
        # None = never attempted; False = attempted and failed (don't retry);
        # _RNNoiseStream instance = active.
        self.denoise_stream = None
        self.frame_acc = np.zeros(0, dtype=np.float32)
        self.audio_buf_16k = []
        self.audio_buf_samples = 0
        self.buf_start_time = 0
        self.vad_open = False
        self.vad_close_time = 0
        self.vad_last_prob = 0.0
        self.vad_prob_env = 0.0
        self.vad_prob_peak = 0.0
        self.vad_envelope = -100.0
        self.upstream_counts = {}
        self.last_upstream_source = None


class RadioTranscriber:
    """Silero-gated, Moonshine-powered audio transcription engine.

    Holds a dict of per-bus _StreamState instances so multiple buses wired to
    the transcription sink get independent VAD + attribution. Shared config
    (model, VAD threshold, denoise enable, boost) applies across all streams.
    """

    def __init__(self, config, gateway=None):
        self._config = config
        self._gateway = gateway
        self._model = None
        self._tokenizer = None
        self._vad_ready = False
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

        # Per-bus pipeline state. {source_id (=bus id): _StreamState}. Created
        # lazily on first feed() from that bus. Lock guards dict mutation
        # (creation from bus tick thread vs iteration from HTTP status thread).
        self._streams = {}
        self._streams_lock = threading.Lock()

        self._results = collections.deque(maxlen=100)
        self._results_lock = threading.Lock()

        self._stats = collections.deque(maxlen=50)
        self._stats_lock = threading.Lock()

        self._pending = collections.deque(maxlen=5)
        self._pending_evt = threading.Event()

        # Feed queue: audio enqueued by bus tick, drained by _feed_worker
        # thread. Keeps per-frame VAD / RNNoise / resample off the bus tick
        # path so a slow ONNX inference or GC pause can't cause SDR choppiness.
        # maxlen caps memory if the worker falls behind (old samples are
        # dropped — better than blowing RAM and better than blocking tick).
        self._feed_queue = collections.deque(maxlen=200)
        self._feed_queue_lock = threading.Lock()
        self._feed_evt = threading.Event()
        self._feed_thread = None

        # Feed-path health counters. Always on (zero cost) — read via
        # get_status() so we have actual numbers when audio misbehaves
        # instead of having to guess. Reset on start().
        self._feed_stats = {
            'enqueued': 0,            # total items successfully enqueued
            'dropped_full': 0,        # items dropped because queue hit maxlen
            'enqueue_blocks': 0,      # times feed() blocked > 5ms on the lock
            'peak_qd': 0,             # highest observed queue depth
            'processed': 0,           # items processed by worker
            'worker_errors': 0,       # exceptions in _process_feed
            'proc_total_ms': 0.0,     # total time in _process_feed
            'proc_max_ms': 0.0,       # worst single-call duration
            'proc_last_ms': 0.0,      # most recent call duration
            'per_stream_ms': {},      # {source_id: cumulative ms in _process_feed}
        }

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
        # Guards denoise enable/mix across HTTP/MCP and tick threads.
        self._denoise_lock = threading.Lock()

        self._max_samples_16k = int(_MAX_UTTERANCE_SECS * _SILERO_SR)
        self._soft_cap_samples_16k = int(_SOFT_CAP_SECS * _SILERO_SR)

    def start(self):
        """Start transcriber — loads model + spawns feed worker."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="Transcriber")
        self._thread.start()
        self._feed_thread = threading.Thread(target=self._feed_worker, daemon=True,
                                             name="TranscriberFeed")
        self._feed_thread.start()
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
        """Toggle neural denoise on the ASR path. Tears down all per-bus
        RNNoise streams when disabled so idle state doesn't linger."""
        enabled = bool(enabled)
        with self._denoise_lock:
            if enabled == self._denoise_enabled:
                return
            self._denoise_enabled = enabled
        if not enabled:
            with self._streams_lock:
                streams = list(self._streams.values())
            for s in streams:
                if s.denoise_stream and s.denoise_stream is not False:
                    try:
                        s.denoise_stream.close()
                    except Exception:
                        pass
                s.denoise_stream = None
        self._save()

    def set_denoise_mix(self, mix):
        """Set denoise wet/dry mix (0.0–1.0)."""
        with self._denoise_lock:
            self._denoise_mix = max(0.0, min(1.0, float(mix)))
        self._save()

    def stop(self):
        self._running = False
        self._pending_evt.set()
        self._feed_evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._feed_thread:
            self._feed_thread.join(timeout=5)
        # Release per-stream resources (RNNoise + Silero state).
        with self._streams_lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for s in streams:
            if s.denoise_stream and s.denoise_stream is not False:
                try:
                    s.denoise_stream.close()
                except Exception:
                    pass
        # Drop the shared Silero ONNX session so a subsequent start() reloads
        # it cleanly (avoids lingering runtime state across restarts).
        _SileroVAD._sess = None
        self._vad_ready = False

    def feed(self, pcm_48k, source_id=None, upstream_source=None):
        """Enqueue 48kHz 16-bit PCM for background processing. Called from
        the bus tick; MUST return in microseconds so the tick meets its 50 ms
        budget. All per-frame work (VAD, RNNoise, resample) runs on the
        _feed_worker thread.

        Instrumentation: records enqueue event to stream_trace when active,
        bumps feed_stats counters, and logs if the lock acquisition blocks
        >5ms (a real-world warning sign of worker contention).
        """
        if not self._enabled or not self._vad_ready:
            return
        if source_id is None:
            source_id = '_default'
        _t0 = time.monotonic()
        # Check + evict under lock so queue depth and drop count are consistent.
        with self._feed_queue_lock:
            _lock_ms = (time.monotonic() - _t0) * 1000
            _dropped = False
            # maxlen behaviour: appending to a full deque silently drops the
            # oldest. We detect that up-front so it shows in the stats.
            if len(self._feed_queue) >= self._feed_queue.maxlen:
                self._feed_stats['dropped_full'] += 1
                _dropped = True
            self._feed_queue.append((pcm_48k, source_id, upstream_source))
            _qd = len(self._feed_queue)
            if _qd > self._feed_stats['peak_qd']:
                self._feed_stats['peak_qd'] = _qd
            self._feed_stats['enqueued'] += 1
        if _lock_ms > 5.0:
            self._feed_stats['enqueue_blocks'] += 1
        _st = getattr(self._gateway, '_stream_trace', None) if self._gateway else None
        if _st and _st.active:
            _extra = f'drop_full qd={_qd}' if _dropped else (f'lock={_lock_ms:.1f}ms' if _lock_ms > 1 else '')
            _st.record(f'trans_feed_{source_id}', 'enqueue', pcm_48k, _qd, _extra)
        self._feed_evt.set()

    def _feed_worker(self):
        """Background thread: drain _feed_queue, run the per-frame pipeline."""
        _stats = self._feed_stats
        while self._running:
            self._feed_evt.wait(timeout=0.5)
            self._feed_evt.clear()
            while self._running:
                with self._feed_queue_lock:
                    if not self._feed_queue:
                        break
                    item = self._feed_queue.popleft()
                pcm_48k, source_id, upstream_source = item
                _t0 = time.monotonic()
                try:
                    self._process_feed(pcm_48k, source_id, upstream_source)
                except Exception as e:
                    _stats['worker_errors'] += 1
                    if not getattr(self, '_feed_err_logged', False):
                        self._feed_err_logged = True
                        print(f"  [Transcribe] feed worker error: {e}")
                _dur_ms = (time.monotonic() - _t0) * 1000
                _stats['processed'] += 1
                _stats['proc_total_ms'] += _dur_ms
                _stats['proc_last_ms'] = _dur_ms
                if _dur_ms > _stats['proc_max_ms']:
                    _stats['proc_max_ms'] = _dur_ms
                _stats['per_stream_ms'][source_id] = (
                    _stats['per_stream_ms'].get(source_id, 0.0) + _dur_ms)
                # Stream trace: record the process event so we can see per-bus
                # timing and catch slow outliers in the dump.
                _st = getattr(self._gateway, '_stream_trace', None) if self._gateway else None
                if _st and _st.active and _dur_ms > 5.0:
                    _st.record(f'trans_proc_{source_id}', 'process', pcm_48k, -1, f'{_dur_ms:.1f}ms')

    def _process_feed(self, pcm_48k, source_id, upstream_source):
        """Run the full per-frame pipeline for one audio chunk from one bus.

        Called from _feed_worker, never from the bus tick. Does the heavy
        work: dBFS metering, RNNoise denoise, anti-aliased resample, Silero
        VAD, utterance buffering.
        """
        stream = self._get_or_create_stream(source_id)
        if stream is None:
            return
        stream.last_upstream_source = upstream_source

        # dBFS envelope — display only, not used for gating.
        db = pcm_db(pcm_48k)
        if db > stream.vad_envelope:
            stream.vad_envelope += (db - stream.vad_envelope) * 0.3
        else:
            stream.vad_envelope += (db - stream.vad_envelope) * 0.05

        # Optional neural denoise at 48 kHz (RNNoise's native rate) before
        # resampling. Per-bus RNNoise state — instance initialised lazily on
        # first enable. False sentinel = construction failed, don't retry.
        arr_48k_i16 = np.frombuffer(pcm_48k, dtype=np.int16)
        with self._denoise_lock:
            _den_enabled = self._denoise_enabled
            _den_mix = self._denoise_mix
        if _den_enabled:
            if stream.denoise_stream is None:
                try:
                    stream.denoise_stream = _RNNoiseStream()
                except Exception as e:
                    print(f"  [Transcribe] Denoise unavailable ({source_id}): {e}")
                    stream.denoise_stream = False
            if stream.denoise_stream and stream.denoise_stream is not False:
                try:
                    denoised = stream.denoise_stream.process(arr_48k_i16)
                    # Align lengths (startup residue → dry-fill the gap),
                    # then blend wet/dry per _denoise_mix so we don't wipe
                    # out the signal when RNNoise mis-classifies the band.
                    if denoised.size > 0:
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
                    print(f"  [Transcribe] Denoise error ({source_id}): {e}")
        elif stream.denoise_stream and stream.denoise_stream is not False:
            # Denoise toggled off — release this stream's RNNoise state.
            try:
                stream.denoise_stream.close()
            except Exception:
                pass
            stream.denoise_stream = None

        # Convert to float32 [-1, 1] and resample once.
        arr_48k = arr_48k_i16.astype(np.float32) / 32768.0
        arr_16k = _resample_48k_to_16k(arr_48k)
        if self._audio_boost != 1.0:
            # Soft-clip via tanh: preserves headroom and avoids the square-wave
            # harmonics that hard-clipping introduces, which Silero's v5 model
            # (trained on natural speech) mishandles.
            arr_16k = np.tanh(arr_16k * self._audio_boost).astype(np.float32)

        # Per-bus frame accumulator.
        stream.frame_acc = np.concatenate([stream.frame_acc, arr_16k])
        while len(stream.frame_acc) >= _SILERO_FRAME:
            frame = stream.frame_acc[:_SILERO_FRAME]
            stream.frame_acc = stream.frame_acc[_SILERO_FRAME:]
            self._process_frame(frame, stream, source_id)

    def _get_or_create_stream(self, source_id):
        """Return the _StreamState for this bus, creating it lazily.

        Silero instance construction reuses the shared ONNX session, so the
        only per-bus cost is a pair of small numpy state arrays.
        """
        stream = self._streams.get(source_id)
        if stream is not None:
            return stream
        try:
            stream = _StreamState()
        except Exception as e:
            print(f"  [Transcribe] Stream init failed for {source_id}: {e}")
            return None
        with self._streams_lock:
            # Another caller may have raced us.
            existing = self._streams.get(source_id)
            if existing is not None:
                return existing
            self._streams[source_id] = stream
        return stream

    def _process_frame(self, frame_16k, stream, source_id):
        """Run Silero on one 32 ms frame and drive VAD state for this stream."""
        prob = stream.vad.probability(frame_16k)
        stream.vad_last_prob = prob
        if prob > stream.vad_prob_peak:
            stream.vad_prob_peak = prob
        # Smoothed envelope for the UI bar: fast attack, slow decay so the
        # status poll catches peaks rather than silence gaps.
        if prob > stream.vad_prob_env:
            stream.vad_prob_env += (prob - stream.vad_prob_env) * 0.5
        else:
            stream.vad_prob_env += (prob - stream.vad_prob_env) * 0.05
        now = time.time()
        exit_thresh = max(0.0, self._vad_threshold - _VAD_HYSTERESIS)

        if stream.vad_open:
            # Always buffer while VAD is open.
            stream.audio_buf_16k.append(frame_16k.copy())
            stream.audio_buf_samples += _SILERO_FRAME
            # Tally upstream source for attribution at utterance close.
            _us = stream.last_upstream_source
            if _us:
                stream.upstream_counts[_us] = stream.upstream_counts.get(_us, 0) + 1

            # Hard 60s cap — force close to stay under Moonshine's 64s limit.
            if stream.audio_buf_samples >= self._max_samples_16k:
                self._submit_utterance(stream, source_id)
                return

            # Inside the soft-cap zone, take any probability dip as a cut
            # point so long utterances split on natural pauses rather than
            # mid-word at the hard cap. If speech continues, VAD re-opens on
            # the next above-threshold frame with no audio lost.
            if (stream.audio_buf_samples >= self._soft_cap_samples_16k
                    and prob < exit_thresh):
                self._submit_utterance(stream, source_id)
                return

            if prob < exit_thresh:
                if stream.vad_close_time == 0:
                    stream.vad_close_time = now
                elif now - stream.vad_close_time > self._vad_hold_time:
                    self._submit_utterance(stream, source_id)
            else:
                # Speech resumed during hold window — reset close timer.
                stream.vad_close_time = 0
        else:
            if prob >= self._vad_threshold:
                stream.vad_open = True
                stream.vad_close_time = 0
                stream.audio_buf_16k = [frame_16k.copy()]
                stream.audio_buf_samples = _SILERO_FRAME
                stream.buf_start_time = now
                # Reset upstream tally at utterance open, seed with this frame.
                stream.upstream_counts = {}
                _us = stream.last_upstream_source
                if _us:
                    stream.upstream_counts[_us] = 1

    def _submit_utterance(self, stream, source_id):
        """Finalize this stream's current utterance and queue it for transcription."""
        stream.vad_open = False
        stream.vad_close_time = 0
        duration = stream.audio_buf_samples / _SILERO_SR
        if duration >= self._min_duration and stream.audio_buf_16k:
            audio_16k = np.concatenate(stream.audio_buf_16k)
            # Pick the upstream source that had the most frames during this
            # utterance. If nothing was tallied, fall back to the bus id.
            _dominant = None
            if stream.upstream_counts:
                _dominant = max(stream.upstream_counts.items(), key=lambda kv: kv[1])[0]
            self._pending.append({
                'audio_16k': audio_16k,
                'start_time': stream.buf_start_time,
                'duration': duration,
                'source_id': source_id,
                'upstream_source': _dominant,
            })
            self._pending_evt.set()
        stream.audio_buf_16k = []
        stream.audio_buf_samples = 0
        stream.upstream_counts = {}

    def get_results(self, since=0, limit=50):
        with self._results_lock:
            results = [r for r in self._results if r['timestamp'] > since]
            return results[-limit:]

    def get_status(self):
        # Aggregate VAD indicators across all active per-bus streams. Peaks
        # are reset per-stream so short speech bursts between polls are still
        # visible on the bar. Per-stream detail is also exposed for future UI.
        with self._streams_lock:
            items = list(self._streams.items())
        any_open = False
        max_prob = 0.0
        max_peak = 0.0
        max_env = 0.0
        max_db = -100.0
        streams_payload = []
        for sid, s in items:
            if s.vad_open:
                any_open = True
            if s.vad_last_prob > max_prob:
                max_prob = s.vad_last_prob
            if s.vad_prob_env > max_env:
                max_env = s.vad_prob_env
            if s.vad_prob_peak > max_peak:
                max_peak = s.vad_prob_peak
            if s.vad_envelope > max_db:
                max_db = s.vad_envelope
            streams_payload.append({
                'id': sid,
                'vad_open': s.vad_open,
                'vad_prob': round(max(s.vad_last_prob, s.vad_prob_env, s.vad_prob_peak), 3),
                'vad_db': round(s.vad_envelope, 1),
                'upstream': s.last_upstream_source,
            })
            s.vad_prob_peak = 0.0
        return {
            'running': self._running,
            'enabled': self._enabled,
            'engine': 'moonshine',
            'vad_engine': 'silero',
            'model': self._model_size,
            'model_loaded': self._model is not None,
            'vad_open': any_open,
            'vad_prob': round(max(max_prob, max_env, max_peak), 3),
            'vad_db': round(max_db, 1),
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
            'streams': streams_payload,
            'stats': self.get_stats(),
            'feed': self._get_feed_health(),
        }

    def _get_feed_health(self):
        """Snapshot of feed-path counters — queue health, worker load, drops.

        Read via get_status() / transcription_status MCP tool. All counters
        are monotonic since start; compare two readings to get rates.
        """
        _s = self._feed_stats
        _processed = _s['processed']
        return {
            'queue_depth': len(self._feed_queue),
            'queue_max': self._feed_queue.maxlen,
            'peak_qd': _s['peak_qd'],
            'enqueued': _s['enqueued'],
            'processed': _processed,
            'dropped_full': _s['dropped_full'],
            'enqueue_blocks_gt_5ms': _s['enqueue_blocks'],
            'worker_errors': _s['worker_errors'],
            'proc_last_ms': round(_s['proc_last_ms'], 2),
            'proc_max_ms': round(_s['proc_max_ms'], 2),
            'proc_mean_ms': round(_s['proc_total_ms'] / _processed, 2) if _processed else 0.0,
            'per_stream_mean_ms': {
                sid: round(ms / max(_processed, 1), 2)
                for sid, ms in _s['per_stream_ms'].items()
            },
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
            # Prime the shared Silero ONNX session so per-bus streams don't
            # each pay the load cost on first feed().
            _SileroVAD._ensure_session()
            self._vad_ready = True
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
                        # Prefer the upstream source (sdr1/sdr2/aioc) for
                        # tagging — gives per-tuner freqs. Fall back to the
                        # bus id if attribution didn't land.
                        _tag_id = item.get('upstream_source') or item.get('source_id')
                        freq_tag = _resolve_freq_tag(self._gateway, _tag_id)
                        result = {
                            'timestamp': item['start_time'],
                            'duration': round(item['duration'], 1),
                            'proc_time': round(_proc_time, 1),
                            'ratio': round(_ratio, 2),
                            'text': text.strip(),
                            'freq': freq_tag,
                            'source': _tag_id or item.get('source_id', ''),
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
