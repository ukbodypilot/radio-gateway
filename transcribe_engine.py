"""
Transcription inference engines — shared by transcriber.py (gateway) and
transcribe_worker.py (remote worker).

LocalInferenceEngine  — loads Moonshine or Whisper in-process.
RemoteEngine          — POSTs VAD-gated utterances to a transcribe_worker HTTP
                        endpoint and returns the text response.  Status is
                        polled in a background thread so the gateway dashboard
                        can show remote worker health without blocking audio.
"""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import json
import numpy as np
import threading
import time
import urllib.request
import urllib.error

_VALID_MODELS = frozenset({
    'moonshine/tiny', 'moonshine/base',
    'whisper/small.en', 'whisper/medium.en', 'whisper/large-v3-turbo',
})

_STATUS_POLL_INTERVAL = 10  # seconds between /status polls on remote worker


# ---------------------------------------------------------------------------
# Host telemetry helpers — same shape as the remote worker's, so local
# LocalInferenceEngine cards can show CPU temp/fan/RAM just like remote ones.
# ---------------------------------------------------------------------------

def _read_int_file(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _host_rss_mb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        log_exc()
    return None


def _host_cpu_temp_c():
    """Highest reading across all thermal zones, in °C. None if unavailable."""
    import glob
    temps = []
    for f in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        v = _read_int_file(f)
        if v is not None and v > 0:
            temps.append(v / 1000.0)
    return round(max(temps), 1) if temps else None


def _host_fan_rpm():
    """First fan's RPM via applesmc, or hwmon if available. None if not found."""
    import glob
    for f in sorted(glob.glob('/sys/devices/platform/applesmc.*/fan*_input')):
        v = _read_int_file(f)
        if v is not None:
            return v
    # Fallback: hwmon (works on many regular PCs)
    for f in sorted(glob.glob('/sys/class/hwmon/hwmon*/fan*_input')):
        v = _read_int_file(f)
        if v is not None and v > 0:
            return v
    return None


# ---------------------------------------------------------------------------
# Local engine
# ---------------------------------------------------------------------------

def _pick_worker(pool: list, duration: float = 0.0, split_threshold: float = 0.0):
    """Return the engine for this utterance.

    Skips engines that aren't ready (e.g. unreachable remote workers) so we
    degrade gracefully — e.g. if the long-tier remote is offline, long clips
    fall back to the local Moonshine rather than silently dropping into a
    failed HTTP POST.

    With split_threshold=0 (disabled): least-busy among ready workers.
    With split_threshold>0: route by clip length.  Short clips (< threshold)
    prefer Moonshine engines (no fixed-window cost); long clips prefer Whisper
    engines (30s encoder cost amortises better).  Within the chosen tier, pick
    least-busy.  Soft fallback to the other tier if the preferred is empty
    (covers tier-down case).
    """
    if not pool:
        return None
    ready = [e for e in pool if e.is_ready()]
    if not ready:
        # Nothing ready — return least-busy from the full pool. The transcribe
        # call will fail, but at least we tried and the error is visible.
        return min(pool, key=lambda e: e._inflight)
    if split_threshold <= 0:
        return min(ready, key=lambda e: e._inflight)
    short_tier = [e for e in ready if e.engine == 'moonshine']
    long_tier = [e for e in ready if e.engine == 'whisper']
    preferred = short_tier if duration < split_threshold else long_tier
    fallback = long_tier if duration < split_threshold else short_tier
    candidates = preferred or fallback or ready
    return min(candidates, key=lambda e: e._inflight)


class LocalInferenceEngine:
    """Loads and runs Moonshine or Whisper inference in-process."""

    def __init__(self, model_key: str):
        if '/' not in model_key:
            model_key = f'moonshine/{model_key}'
        self.model_key = model_key if model_key in _VALID_MODELS else 'moonshine/base'
        _parts = self.model_key.split('/', 1)
        self.engine = _parts[0]      # 'moonshine' | 'whisper'
        self.model_size = _parts[1]  # e.g. 'base', 'medium.en'
        self._model = None
        self._tokenizer = None
        self._inflight = 0           # in-flight requests (for pool dispatch)
        self._dispatched = 0         # gateway-side completed call count
        self._proc_sum = 0.0         # sum of proc_time across dispatched calls
        self._audio_sum = 0.0        # sum of audio durations across dispatched calls

    def load(self):
        """Load the model. Raises on failure."""
        if self.engine == 'moonshine':
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
            self._model = MoonshineOnnxModel(model_name=f'moonshine/{self.model_size}')
            self._tokenizer = load_tokenizer()
        elif self.engine == 'whisper':
            from faster_whisper import WhisperModel
            # cpu_threads via env so it's tunable per-host. Defaults to half
            # the CPU count — saturating every thread on a passively-cooled
            # box (e.g. the 2013 Mac Mini) triggers thermal throttling that
            # actually slows inference compared to running on fewer threads.
            import os as _os
            _default_threads = max(1, (_os.cpu_count() or 4) // 2)
            _threads = int(_os.environ.get('WHISPER_CPU_THREADS', _default_threads))
            self._model = WhisperModel(
                self.model_size, device='cpu', compute_type='int8',
                cpu_threads=_threads)
            self._tokenizer = None
        else:
            raise ValueError(f'Unknown engine: {self.engine}')

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def is_ready(self) -> bool:
        return self.is_loaded

    def transcribe(self, audio_16k: np.ndarray) -> str:
        """Transcribe float32 16 kHz audio → text string."""
        if self._model is None:
            return ''
        if self.engine == 'moonshine':
            tokens = _moonshine_generate_no_repeat(
                self._model, audio_16k.astype(np.float32)[None, :])
            decoded = self._tokenizer.decode_batch([tokens])
            return decoded[0] if decoded else ''
        elif self.engine == 'whisper':
            # beam_size=1 (greedy) + without_timestamps cuts decoder cost
            # ~30% with negligible accuracy loss for short radio clips.
            # We use wall-clock VAD timestamps, not Whisper's per-segment ones,
            # so without_timestamps has no impact on the log/DB.
            segments, _ = self._model.transcribe(
                audio_16k.astype(np.float32), beam_size=1, language='en',
                condition_on_previous_text=False, without_timestamps=True)
            return ' '.join(seg.text.strip() for seg in segments)
        return ''

    def record_stat(self, duration: float, proc_time: float):
        """Track running ratio for this engine. Called from dispatcher after each call."""
        self._proc_sum += float(proc_time)
        self._audio_sum += float(duration)

    def get_status(self) -> dict:
        avg_ratio = round(self._proc_sum / self._audio_sum, 3) if self._audio_sum > 0 else None
        return {
            'type': 'local',
            'model_key': self.model_key,
            'engine': self.engine,
            'model_loaded': self.is_loaded,
            'inflight': self._inflight,
            'dispatched': self._dispatched,
            'avg_ratio': avg_ratio,
            'ram_mb': _host_rss_mb(),
            'cpu_temp_c': _host_cpu_temp_c(),
            'fan_rpm': _host_fan_rpm(),
        }

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# Remote engine
# ---------------------------------------------------------------------------

class RemoteEngine:
    """Sends VAD-gated utterances to a transcribe_worker HTTP endpoint."""

    def __init__(self, url: str, timeout: int = 60,
                 name: str = '', registered: bool = False):
        self._url = url.rstrip('/')
        self._timeout = timeout
        # Identity. `registered` distinguishes workers that dialled in to
        # /transcribe_worker/register (TTL-expired when they stop calling)
        # from ones pinned in config (kept forever, even when unreachable).
        self.name = name or self._url
        self.registered = registered
        # Background polling state
        self._lock = threading.Lock()
        self._cached_status: dict = {}
        self._poll_ok: bool = False
        self._last_poll: float = 0.0
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._inflight = 0           # in-flight requests (for pool dispatch)
        self._dispatched = 0         # gateway-side completed call count
        # Engine/model key come from remote worker's /status after first poll
        self.engine = 'remote'
        self.model_key = 'remote'

    def start(self, blocking_first_poll: bool = True):
        """Start background status-polling thread.

        blocking_first_poll=True polls once on the calling thread so
        is_ready() is accurate immediately — right for gateway startup, where
        blocking is free. Pass False when starting an engine from a request
        handler: /status on an unreachable worker costs the full 5s socket
        timeout, and a registration POST must never park a web thread (or
        anything it holds a lock on) for that long.
        """
        self._running = True
        if blocking_first_poll:
            self._do_poll()
            _target = self._poll_loop
        else:
            def _target():
                self._do_poll()   # still immediate, just off the caller's thread
                self._poll_loop()
        self._poll_thread = threading.Thread(
            target=_target, daemon=True, name='RemoteEnginePoller')
        self._poll_thread.start()

    def stop(self):
        self._running = False

    def is_ready(self) -> bool:
        with self._lock:
            return self._poll_ok and bool(self._cached_status.get('model_loaded'))

    def transcribe(self, audio_16k: np.ndarray) -> str:
        """POST float32 audio bytes to /transcribe, return text."""
        data = audio_16k.astype(np.float32).tobytes()
        req = urllib.request.Request(
            f'{self._url}/transcribe',
            data=data,
            headers={'Content-Type': 'application/octet-stream'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode())
                return result.get('text', '')
        except Exception as e:
            print(f'  [RemoteEngine] transcribe error: {e}', flush=True)
            return ''

    def get_status(self) -> dict:
        """Return cached status from last /status poll plus reachability."""
        with self._lock:
            return dict(
                self._cached_status,
                type='remote',
                url=self._url,
                name=self.name,
                registered=self.registered,
                reachable=self._poll_ok,
                inflight=self._inflight,
                dispatched=self._dispatched,
                last_poll_age=round(time.monotonic() - self._last_poll, 1)
                             if self._last_poll else None,
            )

    def _poll_loop(self):
        while self._running:
            time.sleep(_STATUS_POLL_INTERVAL)
            if self._running:
                self._do_poll()

    def _do_poll(self):
        try:
            req = urllib.request.Request(f'{self._url}/status', method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            with self._lock:
                self._cached_status = data
                self._poll_ok = True
                self._last_poll = time.monotonic()
                # Keep local engine/model_key hints up to date
                self.engine = data.get('engine', 'remote')
                self.model_key = data.get('model_key', 'remote')
        except Exception:
            with self._lock:
                self._poll_ok = False


# ---------------------------------------------------------------------------
# Moonshine greedy decoder (no-repeat n-gram + loop detection)
# ---------------------------------------------------------------------------

def _moonshine_generate_no_repeat(model, audio, max_len=192, no_repeat_ngram_size=3):
    """Greedy decode with logit masking that forbids completing any recent
    n-gram, plus low-diversity / repeating-bigram early-exit.

    Extracted from RadioTranscriber so it can be shared with the remote worker.
    Mirrors MoonshineOnnxModel.generate() — same encoder pass, same KV-cache
    plumbing, same EOS check — but adds repetition guards between decoder.run()
    and argmax.
    """
    m = model
    encoder_inputs = dict(input_values=audio)
    audio_attention_mask = np.ones_like(audio, dtype=np.int64)
    if 'attention_mask' in m.encoder_input_names:
        encoder_inputs = dict(attention_mask=audio_attention_mask, **encoder_inputs)
    last_hidden_state = m.encoder.run(None, encoder_inputs)[0]

    past_key_values = {
        f'past_key_values.{i}.{a}.{b}': np.zeros(
            (0, m.num_key_value_heads, 1, m.head_dim), dtype=np.float32)
        for i in range(m.num_layers)
        for a in ('decoder', 'encoder')
        for b in ('key', 'value')
    }
    tokens = [m.decoder_start_token_id]
    input_ids = [tokens]
    for i in range(max_len):
        use_cache_branch = i > 0
        decoder_inputs = dict(
            input_ids=input_ids,
            encoder_hidden_states=last_hidden_state,
            use_cache_branch=[use_cache_branch],
            **past_key_values,
        )
        if 'encoder_attention_mask' in m.decoder_input_names:
            decoder_inputs = dict(
                encoder_attention_mask=audio_attention_mask, **decoder_inputs)

        logits, *present_key_values = m.decoder.run(None, decoder_inputs)
        step_logits = logits[0, -1]

        # 1) No-repeat n-gram masking.
        if no_repeat_ngram_size > 1 and len(tokens) >= no_repeat_ngram_size - 1:
            prefix = tuple(tokens[-(no_repeat_ngram_size - 1):])
            for j in range(len(tokens) - no_repeat_ngram_size + 1):
                if tuple(tokens[j:j + no_repeat_ngram_size - 1]) == prefix:
                    banned = int(tokens[j + no_repeat_ngram_size - 1])
                    step_logits[banned] = -np.inf

        next_token = int(step_logits.argmax())
        tokens.append(next_token)

        if next_token == m.eos_token_id:
            break

        # 2) Loop-detection early exit.
        if len(tokens) >= 10:
            tail = tokens[-10:]
            if len(set(tail)) <= 2:
                break
            bg = (tokens[-2], tokens[-1])
            if sum(1 for k in range(len(tail) - 1)
                   if (tail[k], tail[k + 1]) == bg) >= 3:
                break

        input_ids = [[next_token]]
        for k, v in zip(past_key_values.keys(), present_key_values):
            if not use_cache_branch or 'decoder' in k:
                past_key_values[k] = v

    return tokens
