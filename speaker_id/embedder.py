"""Speaker-embedding model wrapper (3D-Speaker / WeSpeaker ONNX exports).

One InferenceSession per process, single-threaded: an embedding costs ~7% of
real time on .140 for ERes2Net, and speaker ID must never compete with the
bus tick or ASR for cores.
"""
import os
import threading

import numpy as np

from speaker_id.features import fbank

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'tools', 'models', 'speaker')

# Model keys -> file in MODEL_DIR. Chosen by the 2026-11-15 bake-off on 24 h of
# SDR audio (narrowband FM): ERes2Net 6.3% EER, CAM++ zh+en 11.1%; the
# VoxCeleb-only CAM++/ResNet exports were 30-38% and are not offered.
MODELS = {
    'eres2net': '3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx',
    'campplus': '3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx',
}
DEFAULT_MODEL = 'eres2net'
MIN_SECONDS = 0.5


class Embedder:
    def __init__(self, model_key=DEFAULT_MODEL, model_dir=MODEL_DIR):
        if model_key not in MODELS:
            raise ValueError(f'unknown speaker model {model_key!r}; have {sorted(MODELS)}')
        import onnxruntime as ort
        path = os.path.join(model_dir, MODELS[model_key])
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.enable_cpu_mem_arena = False      # clip lengths vary; the arena only grows
        self._sess = ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])
        meta = self._sess.get_modelmeta().custom_metadata_map
        self.model_key = model_key
        self.dim = int(meta.get('output_dim', 0)) or self._sess.get_outputs()[0].shape[-1]
        self._mean_norm = meta.get('feature_normalize_type', 'global-mean') == 'global-mean'
        self._input = self._sess.get_inputs()[0].name
        self._lock = threading.Lock()

    def embed(self, audio_16k):
        """float32 16 kHz mono in [-1, 1] -> L2-normalised embedding, or None if too short."""
        if len(audio_16k) < MIN_SECONDS * 16000:
            return None
        f = fbank(audio_16k)
        if self._mean_norm:
            f = f - f.mean(axis=0, keepdims=True)
        with self._lock:
            e = self._sess.run(None, {self._input: f[None, :, :]})[0][0]
        e = np.asarray(e, np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n > 0 else None
