#!/usr/bin/env python3
"""Speaker ID: the transcriber hook, live assignment, and that it can never block transcription.

A fake embedder stands in for the ONNX model: each test "voice" is a fixed
unit vector, so scores are exact and the assignment rules can be checked
directly. Callsigns and names here are invented.
"""

import os
import sys
import tempfile
import threading
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaker_id.service import SpeakerID   # noqa: E402
from speaker_id.store import SpeakerStore  # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


A = unit([1, 0, 0, 0])
B = unit([0, 1, 0, 0])
A_SIMILAR = unit([1, 0.3, 0, 0])          # cos to A = 0.958
A_MIDDLE = unit([0.55, 0, 0.835, 0])     # cos to A = 0.55, to B = 0 -> between new (0.45) and accept (0.60)


class FakeEmbedder:
    """audio[0] carries which voice this clip is."""
    VOICES = {1.0: A, 2.0: B, 3.0: A_SIMILAR, 4.0: A_MIDDLE}

    def __init__(self):
        self.calls = 0
        self.block = None

    def embed(self, audio):
        self.calls += 1
        if self.block:
            self.block.wait(5)
        return self.VOICES[float(audio[1])]


def cfg(**kw):
    c = dict(SPEAKER_ID_MODEL='fake', SPEAKER_ID_ACCEPT=0.60, SPEAKER_ID_NEW=0.45,
             SPEAKER_ID_MIN_SECS=1.5, SPEAKER_ID_LEARN_SECS=3.0, SPEAKER_ID_KEEP_CLIPS=False,
             SPEAKER_ID_BUSES='', SPEAKER_ID_DB_PATH='')
    c.update(kw)
    return types.SimpleNamespace(**c)


_uid = iter(range(1, 10_000))


def utt(voice, dur=5.0, bus='sdr1', ts=None):
    a = np.zeros(int(dur * 16000), np.float32)
    a[1] = voice          # the fake embedder's voice tag (tiny, survives arctanh)
    return {'uid': next(_uid), 'audio_16k': a, 'audio_boost': 1.0, 'duration': dur,
            'source_id': bus, 'start_time': ts or time.time(), 'upstream_source': None}


def make(**kw):
    tmp = tempfile.mkdtemp()
    store = SpeakerStore(os.path.join(tmp, 'speakers.db'))
    s = SpeakerID(cfg(**kw), None, store=store, embedder=FakeEmbedder())
    s.start()
    return s, store


def drain(s, timeout=5.0):
    """Wait until the worker has finished every job handed to it."""
    end = time.time() + timeout
    while time.time() < end and s.counters['processed'] < s.counters['queued']:
        time.sleep(0.01)


print("\n1. live assignment")
s, st = make()
s.on_utterance(utt(1.0)); drain(s)
check("first long over starts a profile", s.counters['new_profile'] == 1, str(dict(s.counters)))
s.on_utterance(utt(3.0)); drain(s)
check("a similar voice matches and teaches the profile", s.counters['matched_learn'] == 1, str(dict(s.counters)))
check("the profile's over count went up", s._profiles[0][2] == 2)
s.on_utterance(utt(2.0)); drain(s)
check("a different voice starts a second profile", s.counters['new_profile'] == 2 and len(s._profiles) == 2)
s.on_utterance(utt(4.0)); drain(s)
check("a score between NEW and ACCEPT is 'unsure' and creates nothing",
      s.counters['unsure'] == 1 and len(s._profiles) == 2, str(dict(s.counters)))
n_before = s._profiles[0][2]
s.on_utterance(utt(1.0, dur=2.0)); drain(s)
check("a short over (>= MIN, < LEARN) matches but does not teach",
      s.counters['matched'] == 1 and s._profiles[0][2] == n_before, str(dict(s.counters)))
s.on_utterance(utt(2.0, dur=1.0)); drain(s)
check("an over under MIN_SECS is not fingerprinted", s.counters['too_short'] == 1)
check("every fingerprinted over is stored", st.counts()['utterances'] == 5, str(st.counts()))
s.stop()

print("\n2. profiles survive a restart")
s2 = SpeakerID(cfg(), None, store=SpeakerStore(st.path), embedder=FakeEmbedder())
s2.start()
check("profiles reload from the database", len(s2._profiles) == 2)
s2.on_utterance(utt(2.0)); drain(s2)
check("...and a known voice matches after the restart", s2.counters['matched_learn'] == 1)
s2.stop()

print("\n3. transcript joins its over by uid, in order")
s, st = make()
u = utt(1.0)
s.on_utterance(u)
s.on_result(u['uid'], {'text': 'this is a test over', 'tx_id': 42, 'freq': '146.520'})
drain(s)
row = st.utterance(1)
check("text and transcription row id are stored on the over",
      row and row['text'] == 'this is a test over' and row['tx_id'] == 42, str(row))
check("freq comes from the transcript", row and row['freq'] == '146.520')
s.on_result(999999, {'text': 'orphan'}); drain(s)
check("a transcript with no fingerprinted over is counted, not an error",
      s.counters['text_orphan'] == 1 and s.counters['errors'] == 0)
chips = s.chips_for([{'tx_id': 42, 'text': 'x'}, {'tx_id': 7, 'text': 'y'}])
check("the UI chip is attached to the matching transcription row",
      chips[0].get('speaker', {}).get('profile_id') == row['profile_id'] and 'speaker' not in chips[1])
s.stop()

print("\n4. bus filter")
s, st = make(SPEAKER_ID_BUSES='sdr1, sdr2')
s.on_utterance(utt(1.0, bus='th9800')); s.on_utterance(utt(1.0, bus='sdr2')); drain(s)
check("only listed buses are fingerprinted",
      s.counters['skipped_bus'] == 1 and s.counters['embedded'] == 1, str(dict(s.counters)))
s.stop()

print("\n5. it can never block the transcriber")
s, st = make()
s._embedder.block = threading.Event()
t0 = time.perf_counter()
for _ in range(200):
    s.on_utterance(utt(1.0))
dt = time.perf_counter() - t0
check("200 handovers to a stuck worker return at once", dt < 0.5, f'{dt:.3f}s')
check("the overflow is dropped and counted", s.counters['dropped'] >= 200 - s._q_max - 1,
      str(dict(s.counters)))
s._embedder.block.set()
s.stop()

print("\n6. the transcriber's audio boost is undone")
x = (np.sin(np.linspace(0, 50, 16000)) * 0.4).astype(np.float32)
item = {'audio_16k': np.tanh(x * 2.0).astype(np.float32), 'audio_boost': 2.0}
check("arctanh recovers the bus audio", np.abs(SpeakerID.raw_audio(item) - x).max() < 1e-4)
check("no boost passes through", SpeakerID.raw_audio({'audio_16k': x, 'audio_boost': 1.0}) is not None)

print("\n7. the transcriber hook")
import transcriber   # noqa: E402
src = open(transcriber.__file__).read()
check("the transcriber attaches gateway.speaker_id in its constructor (survives a model-switch rebuild)",
      "getattr(gateway, 'speaker_id', None)" in src and 'self.add_listener(_sid)' in src)
T = transcriber.RadioTranscriber
t = object.__new__(T)
t._listeners = []
t._utt_seq = iter(range(1, 100))
t._feed_stats_lock = threading.Lock()
t._close_reasons = {}
t._min_duration = 0.5
t._audio_boost = 2.0
t._pending = __import__('collections').deque()
t._pending_evt = threading.Event()
got = []


class Bad:
    def on_utterance(self, item):
        raise RuntimeError('listener exploded')


class Good:
    def on_utterance(self, item):
        got.append(item)


t.add_listener(Bad()); t.add_listener(Good())
stream = types.SimpleNamespace(vad_open=True, vad_close_time=0, low_run=0,
                               audio_buf_samples=32000, audio_buf_16k=[np.zeros(32000, np.float32)],
                               buf_start_time=123.0, upstream_counts={'sdr1': 5})
t._submit_utterance(stream, 'sdr1')
check("a failing listener does not stop the utterance reaching ASR", len(t._pending) == 1)
check("...nor the next listener", len(got) == 1)
check("the handover carries uid and the boost to undo",
      got and got[0].get('uid') == 1 and got[0].get('audio_boost') == 2.0 and got[0] is t._pending[0])

print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
