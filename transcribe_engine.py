"""
Transcription inference engines — shared by transcriber.py (gateway) and
transcribe_worker.py (remote worker).

LocalInferenceEngine  — loads Parakeet or Moonshine v2 in-process.
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

# Moonshine v1 (moonshine/*) and Whisper (whisper/*) were removed 2026-11-12:
# on real radio clips Parakeet recovered 1054 words to Moonshine base's 736 at
# the same speed, and v1 returned 13 words for a 50 s transmission the others
# got ~180 from. A saved key for a removed model falls back to DEFAULT_MODEL.
_VALID_MODELS = frozenset({
    # NVIDIA Parakeet TDT 0.6B v2, English, int8 ONNX via `onnx-asr`.
    'parakeet/tdt-0.6b-v2',
    # Moonshine v2 (Feb 2026, `moonshine-voice` package, own C++/ORT runtime).
    'moonshine2/small', 'moonshine2/medium',
})
DEFAULT_MODEL = 'parakeet/tdt-0.6b-v2'


def _cpu_threads() -> int:
    """Inference thread count: env override, else half the cores.

    Saturating every core starves the gateway's audio threads on the host and
    thermally throttles a passively-cooled worker (the 2013 Mac Mini), which
    ends up slower than running on fewer threads. TRANSCRIBE_CPU_THREADS wins;
    WHISPER_CPU_THREADS is still read because existing worker units set it.
    """
    import os as _os
    _default = max(1, (_os.cpu_count() or 4) // 2)
    _raw = _os.environ.get('TRANSCRIBE_CPU_THREADS') or _os.environ.get('WHISPER_CPU_THREADS')
    try:
        return max(1, int(_raw)) if _raw else _default
    except ValueError:
        return _default


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

def _pick_worker(pool: list):
    """Return the least-busy ready engine for the next utterance.

    Skips engines that aren't ready (e.g. unreachable remote workers) so we
    degrade gracefully. Both remaining model families handle short and long
    clips alike, so there is no routing by clip length any more (that split
    existed only to keep short clips off Whisper's fixed 30 s window).
    """
    if not pool:
        return None
    ready = [e for e in pool if e.is_ready()]
    if not ready:
        # Nothing ready — return least-busy from the full pool. The transcribe
        # call will fail, but at least we tried and the error is visible.
        return min(pool, key=lambda e: e._inflight)
    return min(ready, key=lambda e: e._inflight)


class LocalInferenceEngine:
    """Loads and runs Parakeet or Moonshine v2 in-process."""

    def __init__(self, model_key: str):
        self.model_key = model_key if model_key in _VALID_MODELS else DEFAULT_MODEL
        _parts = self.model_key.split('/', 1)
        self.engine = _parts[0]      # 'parakeet' | 'moonshine2'
        self.model_size = _parts[1]  # e.g. 'tdt-0.6b-v2', 'small'
        self._model = None
        # The moonshine-voice transcriber is one native handle; the dispatcher
        # can run several utterances on this engine at once, and the C API
        # does not promise that is safe. Parakeet's ORT sessions are
        # thread-safe and stay unlocked.
        self._call_lock = threading.Lock()
        self._inflight = 0           # in-flight requests (for pool dispatch)
        self._dispatched = 0         # gateway-side completed call count
        self._proc_sum = 0.0         # sum of proc_time across dispatched calls
        self._audio_sum = 0.0        # sum of audio durations across dispatched calls

    def load(self):
        """Load the model. Raises on failure."""
        if self.engine == 'moonshine2':
            # Weights (~100-300 MB) download to ~/.cache/moonshine_voice on
            # first load and are reused after. English models are MIT.
            from moonshine_voice import Transcriber, get_model_for_language, ModelArch
            _arch = {'small': ModelArch.SMALL_STREAMING,
                     'medium': ModelArch.MEDIUM_STREAMING}[self.model_size]
            _path, _arch = get_model_for_language('en', _arch)
            self._model = Transcriber(_path, _arch)
        elif self.engine == 'parakeet':
            # ~630 MB int8 ONNX from Hugging Face (istupakov export), cached
            # under ~/.cache/huggingface after the first load.
            import onnx_asr
            import onnxruntime as ort
            _opts = ort.SessionOptions()
            _opts.intra_op_num_threads = _cpu_threads()
            _opts.inter_op_num_threads = 1
            self._model = onnx_asr.load_model(
                f'nemo-parakeet-{self.model_size}', quantization='int8',
                sess_options=_opts, providers=['CPUExecutionProvider'])
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
        if self.engine == 'moonshine2':
            with self._call_lock:
                transcript = self._model.transcribe_without_streaming(
                    audio_16k.astype(np.float32).tolist(), 16000)
            return ' '.join(l.text.strip() for l in transcript.lines if l.text and l.text.strip())
        elif self.engine == 'parakeet':
            return self._model.recognize(audio_16k.astype(np.float32), sample_rate=16000) or ''
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
        if self.engine == 'moonshine2' and self._model is not None:
            try:
                self._model.close()
            except Exception:
                log_exc()


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
