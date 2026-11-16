"""Split an utterance that holds two speakers (fast turnarounds on nets).

The transcriber ends an utterance after VAD_HOLD (1 s) of silence. On a busy
net the next station keys up sooner than that, so one "over" can be net
control followed by a check-in: one fingerprint blends two voices and the
check-in's callsign lands on net control's voice.

FM leaves a short gap between transmissions (squelch tail, courtesy tone,
key-up): a pause shorter than the hold. Each such pause inside a long
utterance is a candidate boundary; the fingerprints of the audio either side
(up to WIN seconds) decide. Split where they are less alike than SPLIT_AT,
best (least alike) boundary first, then the same test again on each side.

Measured 2026-11-15 on 24 h of SDR1 (150 joins of two different labelled
people with a 0.3 s gap, 76 same-person joins, 300 real long overs): the
same person either side of a pause is only ~0.37 alike at the median, so
side fingerprints alone separate poorly. At 0.30: 73% of two-speaker joins
caught, 22% of same-person joins and 22% of real long overs wrongly split;
at 0.20: 38% / 12% / 11%. Hence SPEAKER_ID_SPLIT defaults to off. The next
step is a veto from the known voices: split only if the halves match
DIFFERENT known voices.
"""
import importlib.util
import os
import threading

import numpy as np

FRAME = 512                  # Silero frame, 32 ms at 16 kHz
SR = 16000
WIN = 4.0                    # seconds of audio either side of a pause to compare
MIN_SIDE = 1.5               # each side needs this much speech to fingerprint
MIN_PAUSE = 0.16             # seconds of low VAD probability that count as a gap
PAUSE_PROB = 0.15
SPLIT_AT = 0.30              # measured: see module docstring / CHANGELOG
MIN_SECS = 5.0               # shorter utterances are never split
MAX_CANDIDATES = 4           # longest pauses only: bounded cost per utterance
MAX_PARTS = 3

_sess = None
_sess_lock = threading.Lock()


def _silero():
    """Silero VAD session, located without importing silero_vad (which pulls torch)."""
    global _sess
    with _sess_lock:
        if _sess is None:
            import onnxruntime as ort
            path = os.environ.get('SILERO_ONNX_PATH', '')
            if not path:
                spec = importlib.util.find_spec('silero_vad')
                for base in (spec.submodule_search_locations or []) if spec else []:
                    cand = os.path.join(base, 'data', 'silero_vad.onnx')
                    if os.path.exists(cand):
                        path = cand
            if not path:
                raise RuntimeError('Silero VAD model not found')
            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            _sess = ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])
        return _sess


def vad_probs(audio, boost=2.0):
    """Per-32 ms speech probability, on the same tanh-boosted signal the transcriber gates on."""
    sess = _silero()
    b = np.tanh(np.asarray(audio, np.float32) * boost).astype(np.float32)
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, 64), np.float32)
    sr = np.array(SR, np.int64)
    out = np.empty(len(b) // FRAME, np.float32)
    for i in range(len(out)):
        x = np.concatenate([ctx, b[i * FRAME:(i + 1) * FRAME][None, :]], axis=1)
        p, state = sess.run(None, {'input': x, 'state': state, 'sr': sr})
        ctx = x[:, -64:]
        out[i] = p[0, 0]
    return out


def pauses(probs, lo=PAUSE_PROB, min_secs=MIN_PAUSE, edge_secs=MIN_SIDE):
    """[(start_s, end_s)] of gaps inside the utterance, away from its ends."""
    min_f = max(1, int(round(min_secs * SR / FRAME)))
    edge_f = int(edge_secs * SR / FRAME)
    out, i, n = [], 0, len(probs)
    while i < n:
        if probs[i] < lo:
            j = i
            while j < n and probs[j] < lo:
                j += 1
            if j - i >= min_f and i > edge_f and j < n - edge_f:
                out.append((i * FRAME / SR, j * FRAME / SR))
            i = j
        else:
            i += 1
    return out


def _side_cos(audio, embed, gap, lo, hi):
    """Cosine between WIN s before and after a gap, within [lo, hi) seconds; None if too short."""
    a0 = max(lo, gap[0] - WIN)
    b1 = min(hi, gap[1] + WIN)
    left = audio[int(a0 * SR):int(gap[0] * SR)]
    right = audio[int(gap[1] * SR):int(b1 * SR)]
    if len(left) < MIN_SIDE * SR or len(right) < MIN_SIDE * SR:
        return None
    el, er = embed(left), embed(right)
    if el is None or er is None:
        return None
    return float(el @ er)


def find_boundaries(audio, embed, probs=None, split_at=SPLIT_AT):
    """Times (s) where the speaker changes, in order; [] for a single-speaker utterance.

    embed(audio) -> unit vector or None. Bounded: at most MAX_CANDIDATES
    pauses are tested per segment, MAX_PARTS parts in all.
    """
    dur = len(audio) / SR
    if dur < MIN_SECS:
        return []
    if probs is None:
        probs = vad_probs(audio)
    gaps = pauses(probs)
    cuts = []

    def search(lo, hi, depth):
        if len(cuts) >= MAX_PARTS - 1 or hi - lo < 2 * MIN_SIDE:
            return
        inside = [g for g in gaps if g[0] - lo >= MIN_SIDE and hi - g[1] >= MIN_SIDE]
        inside = sorted(inside, key=lambda g: g[0] - g[1])[:MAX_CANDIDATES]   # longest gaps first
        best = None
        for g in inside:
            c = _side_cos(audio, embed, g, lo, hi)
            if c is not None and (best is None or c < best[0]):
                best = (c, g)
        if best is None or best[0] >= split_at:
            return
        t = round((best[1][0] + best[1][1]) / 2, 3)
        cuts.append(t)
        if depth < MAX_PARTS - 1:
            search(lo, t, depth + 1)
            search(t, hi, depth + 1)

    search(0.0, dur, 1)
    return sorted(cuts)[:MAX_PARTS - 1]


def split_text(text, cut_times, word_times, duration):
    """Split a transcript at speaker-change times. Returns ([part text], [start offset]).

    With word timings (Parakeet) each word goes to the part it was spoken in;
    without, the split is proportional to time (the fallback estimate).
    """
    if not cut_times:
        return [text], [0]
    text = text or ''
    if word_times:
        offs = []
        for t in cut_times:
            off = next((o for o, wt in word_times if wt >= t), len(text))
            offs.append(off)
    else:
        offs = [int(len(text) * t / duration) if duration else len(text) for t in cut_times]
        # snap to a word boundary
        offs = [text.rfind(' ', 0, o) + 1 if ' ' in text[:o] else o for o in offs]
    parts, starts, prev = [], [], 0
    for o in offs + [len(text)]:
        seg = text[prev:o]
        lead = len(seg) - len(seg.lstrip())
        parts.append(seg.strip())
        starts.append(prev + lead)
        prev = o
    return parts, starts


def part_word_times(word_times, start_off, end_off, t0):
    """Word timings re-based onto one part's text and audio."""
    if not word_times:
        return None
    return [[o - start_off, round(t - t0, 2)] for o, t in word_times if start_off <= o < end_off]
