"""Background music (with ducking) and the periodic announcer."""

import os
import time
import threading
import math as _math_mod
import numpy as np

# Shared level-metering and gain helpers
from audio_util import (
    pcm_level,
)

from sources.base import AudioSource, bgm_files, bgm_path


class BGMSource(AudioSource):
    """Looping background-music bed, as its own routing node.

    Separate from FilePlaybackSource on purpose: that source decodes one file
    at a time, so BGM sharing it meant the music stopped the moment anything
    else played. A repeating message over a music bed needs both running at
    once, which means BGM needs its own buffer and its own node in /routing.

    Ducking: every other duckable source is DROPPED from the mix when a ducker
    is active. This one instead reports `duck_level`, a linear gain the bus
    applies in place of muting, so the bed stays audible underneath — the
    broadcast behaviour rather than a hard cut.
    """

    def __init__(self, gateway):
        super().__init__("BGM", gateway.config)
        self.gateway = gateway
        self.priority = 12
        self.ptt_control = True
        self.volume = 1.0
        self.audio_level = 0
        self._lock = threading.Lock()
        self._pcm = None        # decoded bed, played on repeat
        self._pos = 0
        self._slot = None
        # Duck envelope — 1.0 = full level, duck_level = fully ducked.
        # Timed in AUDIO time (samples produced), not wall clock: the ramp
        # advances per chunk, so a wall-clock hold would drift out of step
        # whenever the bus stalls or runs ahead.
        self._duck_gain = 1.0
        self._audio_secs = 0.0
        self._duck_hold_until = 0.0
        self._auto_stopped = False

    @property
    def _max_secs(self):
        """Seconds a bed may run before stopping itself. 0 disables the cap."""
        try:
            return max(0.0, float(getattr(self.config, 'BGM_MAX_SECONDS', 120.0)))
        except (TypeError, ValueError):
            return 120.0

    @property
    def duck_level(self):
        """Linear gain used instead of muting when ducked (0.0-1.0)."""
        try:
            db = float(getattr(self.config, 'BGM_DUCK_DB', -12.0))
        except (TypeError, ValueError):
            db = -12.0
        return max(0.0, min(1.0, 10.0 ** (db / 20.0)))

    def _cfg_secs(self, key, default):
        try:
            return max(0.01, float(getattr(self.config, key, default)))
        except (TypeError, ValueError):
            return default

    def _duck_target(self):
        """Full gain, or the ducked gain while the announcer is speaking.

        This is a deliberate side-channel: BGM asks the announcer directly
        rather than the bus doing it. The bus-level duck only exists on a
        ListenBus, and a ListenBus gates its Mumble sink on a bus-wide VAD
        flag that steady music does not hold open — so routing the bed through
        one made the music disappear between announcements. Ducking here works
        on ANY bus type. The coupling is narrow (one is_active() read) and
        stated plainly so it is not a mystery later.
        """
        ann = getattr(self.gateway, 'announcer_source', None)
        speaking = bool(ann is not None and ann.is_active())
        if speaking:
            # Hold keeps the bed down across short gaps so it cannot pump
            # between words or over the tail of a phrase.
            self._duck_hold_until = self._audio_secs + self._cfg_secs('BGM_DUCK_HOLD', 0.4)
        held = self._audio_secs < self._duck_hold_until
        return self.duck_level if (speaking or held) else 1.0

    def _duck_ramp(self, frames):
        """Advance the duck envelope one chunk. Returns (start_gain, end_gain).

        Travels at a constant rate across the full duck range, so the times
        below are the real transition durations rather than a vague
        coefficient. Ramped per sample by the caller — stepping the gain once
        per chunk is audible as zipper noise on sustained music.
        """
        chunk_secs = frames / float(getattr(self.config, 'AUDIO_RATE', 48000) or 48000)
        self._audio_secs += chunk_secs
        target = self._duck_target()
        start = self._duck_gain
        # Down fast (get out of the way), back up slowly — the broadcast feel.
        t_const = (self._cfg_secs('BGM_DUCK_ATTACK', 0.25) if target < start
                   else self._cfg_secs('BGM_DUCK_RELEASE', 1.2))
        span = max(1e-6, 1.0 - self.duck_level)
        max_step = span * (chunk_secs / t_const)
        delta = target - start
        end = target if abs(delta) <= max_step else start + _math_mod.copysign(max_step, delta)
        self._duck_gain = end
        return start, end

    @property
    def _audio_dir(self):
        ps = getattr(self.gateway, 'playback_source', None)
        return (getattr(ps, 'announcement_directory', None)
                or getattr(self.config, 'PLAYBACK_DIRECTORY', './audio/'))

    def bgm_state(self):
        """Per-bed availability and which is playing, for the UI."""
        out = []
        for n, name in bgm_files(self.config):
            out.append({
                'slot': n,
                'file': os.path.basename(name),
                'available': os.path.exists(bgm_path(self.config, name, self._audio_dir)),
                'playing': self._slot == n,
                'remaining': (round(max(0.0, self._max_secs - self._audio_secs), 1)
                              if (self._slot == n and self._max_secs) else None),
            })
        return out

    def play_slot(self, slot, action='toggle'):
        """Start/stop a bed by slot number. Returns a dict for the endpoint.

        Only one bed at a time — this source has a single decode buffer, so
        starting bed 2 while 1 runs swaps rather than stacking. Unlike the old
        implementation this no longer touches FilePlaybackSource, so BGM keeps
        playing while soundboard hits, TTS and the announcer come and go.
        """
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return {'ok': False, 'error': f'bad BGM slot {slot!r}', 'bgm': self.bgm_state()}

        mapping = dict(bgm_files(self.config))
        if slot not in mapping:
            return {'ok': False, 'error': f'BGM slot {slot} not configured',
                    'bgm': self.bgm_state()}

        action = str(action or 'toggle').lower()
        if action == 'stop' or (action == 'toggle' and self._slot == slot):
            self.stop()
            print(f"[BGM] Bed {slot} stopped")
            return {'ok': True, 'playing': None, 'bgm': self.bgm_state()}

        path = bgm_path(self.config, mapping[slot], self._audio_dir)
        if not os.path.exists(path):
            return {'ok': False,
                    'error': f'{os.path.basename(path)} not found in the audio directory',
                    'bgm': self.bgm_state()}
        if not self.play(slot, path):
            self.stop()
            return {'ok': False, 'error': f'could not decode {os.path.basename(path)}',
                    'bgm': self.bgm_state()}
        print(f"[BGM] Bed {slot} started: {os.path.basename(path)}")
        return {'ok': True, 'playing': slot, 'bgm': self.bgm_state()}

    def play(self, slot, path):
        """Decode `path` and start looping it. Returns True on success."""
        # Reuse FilePlaybackSource's decoder rather than reimplementing the
        # resample/channel handling — identical format guarantees, one place
        # to fix.
        ps = getattr(self.gateway, 'playback_source', None)
        if ps is None:
            return False
        # Beds are levelled offline; see _decode_file's normalize note.
        pcm = ps._decode_file(path, normalize=False)
        if not pcm:
            return False
        with self._lock:
            self._pcm = pcm
            self._pos = 0
            self._slot = slot
            # Reset elapsed play time so the runtime cap measures this bed,
            # not the total since the gateway started.
            self._audio_secs = 0.0
            self._duck_gain = 1.0
            self._duck_hold_until = 0.0
        return True

    def stop(self):
        with self._lock:
            self._pcm = None
            self._pos = 0
            self._slot = None
            self.audio_level = 0
            self._duck_gain = 1.0
            self._audio_secs = 0.0
            self._duck_hold_until = 0.0

    @property
    def playing_slot(self):
        return self._slot

    def is_active(self):
        return self._pcm is not None

    def get_audio(self, chunk_size):
        want = chunk_size * getattr(self.config, 'AUDIO_CHANNELS', 1) * 2
        with self._lock:
            pcm = self._pcm
            if not pcm:
                return None, False
            # Wrap around the end of the bed, splicing across the seam so a
            # short bed cannot emit a part-frame (which would desynchronise
            # stereo for the rest of the loop).
            out = pcm[self._pos:self._pos + want]
            self._pos += len(out)
            while len(out) < want:
                self._pos = 0
                take = min(want - len(out), len(pcm))
                out += pcm[:take]
                self._pos = take
            frames = len(out) // 2
            g0, g1 = self._duck_ramp(frames)
            # Runtime cap. _duck_ramp has just advanced _audio_secs, which is
            # elapsed play time for THIS bed. Cleared inline rather than via
            # stop(): stop() takes the same non-reentrant lock we are holding.
            if self._max_secs and self._audio_secs >= self._max_secs:
                self._pcm = None
                self._pos = 0
                self._slot = None
                self.audio_level = 0
                self._duck_gain = 1.0
                self._audio_secs = 0.0
                self._duck_hold_until = 0.0
                self._auto_stopped = True
            if self.volume != 1.0 or g0 != 1.0 or g1 != 1.0:
                arr = np.frombuffer(out, dtype=np.int16).astype(np.float32)
                if g0 != 1.0 or g1 != 1.0:
                    # Per-sample ramp across the chunk — a single step per
                    # chunk is audible as zipper noise on sustained music.
                    arr = arr * np.linspace(g0, g1, num=frames, dtype=np.float32)
                out = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()
            self.audio_level = pcm_level(out, self.audio_level)
        if self._auto_stopped:
            self._auto_stopped = False
            print(f"[BGM] Bed stopped — reached BGM_MAX_SECONDS ({self._max_secs:.0f}s)")
            # Cheap, no I/O: the announcer follows the bed, so it must go quiet
            # too. Done outside the lock and without touching the JSON store,
            # because this runs on the audio thread.
            ann = getattr(self.gateway, 'announcer_source', None)
            if ann is not None:
                ann.set_enabled(False)
        return out, True

    @property
    def max_secs(self):
        return self._max_secs

    @property
    def ducking(self):
        """True when the bed is below full level (for the UI)."""
        return self._duck_gain < 0.999

    def cleanup(self):
        self.stop()


class AnnouncerSource(AudioSource):
    """Repeats a short TTS message at a fixed interval.

    Its own node so it can be routed independently of the music, and so it can
    act as the DUCKER against BGM — higher priority and not duckable, which is
    what the bus uses to decide who ducks whom.

    Between announcements it returns None (silent, inactive), so it only
    occupies the bus while actually speaking.
    """

    def __init__(self, gateway):
        super().__init__("Announcer", gateway.config)
        self.gateway = gateway
        self.priority = 1
        self.ptt_control = True
        self.volume = 1.0
        self.audio_level = 0
        self._lock = threading.Lock()
        self._pcm = None            # decoded message, replayed each cycle
        self._pos = None            # None = idle between announcements
        self._next_at = 0.0
        self._enabled = False
        self._last_error = ''

    def set_message_pcm(self, pcm):
        with self._lock:
            self._pcm = pcm or None
            self._pos = None

    def set_enabled(self, on, interval=None):
        with self._lock:
            self._enabled = bool(on)
            self._pos = None
            # Speak once promptly on enable rather than waiting a full cycle.
            self._next_at = time.monotonic() + (1.0 if on else 0.0)

    @property
    def interval(self):
        try:
            v = float(getattr(self.config, 'ANNOUNCER_INTERVAL', 10.0))
        except (TypeError, ValueError):
            v = 10.0
        return max(2.0, v)

    def is_active(self):
        return self._pos is not None

    def get_audio(self, chunk_size):
        want = chunk_size * getattr(self.config, 'AUDIO_CHANNELS', 1) * 2
        now = time.monotonic()
        with self._lock:
            if not self._enabled or not self._pcm:
                self._pos = None
                return None, False
            if self._pos is None:
                if now < self._next_at:
                    return None, False
                self._pos = 0           # start an announcement
            out = self._pcm[self._pos:self._pos + want]
            self._pos += len(out)
            if self._pos >= len(self._pcm):
                # Finished — interval is measured from the END of speech, so a
                # long message cannot overlap its own next cycle.
                self._pos = None
                self._next_at = now + self.interval
            if not out:
                return None, False
            if len(out) < want:
                out = out.ljust(want, b'\x00')
            if self.volume != 1.0:
                arr = np.frombuffer(out, dtype=np.int16).astype(np.float32)
                out = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()
            self.audio_level = pcm_level(out, self.audio_level)
        return out, True

    def cleanup(self):
        with self._lock:
            self._enabled = False
            self._pos = None
