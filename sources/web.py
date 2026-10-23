"""Browser audio: the web microphone source and the web monitor sink."""

from log_util import log_exc
import time
import threading as _thr
import queue as _queue_mod
import numpy as np

# Shared level-metering and gain helpers
from audio_util import (
    pcm_level, pcm_db, apply_gain,
)

from sources.base import AudioSource


class WebMicSource(AudioSource):
    """Receives browser microphone audio via WebSocket and routes to radio TX.

    PTT is an explicit operator hold, NOT a latch and NOT VOX: the browser
    keys on button-press and refreshes that key roughly twice a second for
    as long as the button is held.  Deliberate speech into a transmitter is
    an operator action — see WebMonitorSource for the VAD-gated passive
    counterpart, which is a different job.

    'Connected' and 'keyed' are separate states.  The browser keeps the
    socket and the mic stream open for a linger period between overs so a
    second over doesn't re-pay the getUserMedia + WebSocket handshake
    latency (which would clip the first syllable every time), so a live
    socket must never by itself imply a keyed transmitter.

    Two watchdogs bound the transmission.  Both are evaluated on the BUS
    thread in get_audio(), so neither depends on the browser, its socket,
    or its host still being alive — the release path must not live inside
    the thing whose failure it is there to correct:

      * dead-man (WEB_MIC_KEY_TIMEOUT) — a lapsed refresh unkeys.  Covers a
        lost pointerup, a hidden tab, a slammed laptop lid, dead wifi.
      * time-out timer (WEB_MIC_MAX_TX) — no single over may exceed it.
        Latches: the operator must release and press again to transmit.
    """
    def __init__(self, config, gateway):
        super().__init__("WEBMIC", config)
        self.gateway = gateway
        self.priority = 0
        self.ptt_control = True
        self.volume = float(getattr(config, 'WEB_MIC_VOLUME', 4.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        # ── PTT hold state (see class docstring) ──
        self.key_timeout = float(getattr(config, 'WEB_MIC_KEY_TIMEOUT', 2.0))
        self.max_tx = float(getattr(config, 'WEB_MIC_MAX_TX', 120.0))
        self._key_lock = _thr.Lock()
        self.tx_keyed = False
        self._key_deadline = 0.0
        self._key_started = 0.0
        self._tot_tripped = False
        # Instrumentation from the first commit, not as a follow-up: a
        # deadman/TOT counter that never moves means the watchdog is not
        # actually wired to anything, and a stuck transmitter is exactly
        # the failure you do not get to discover months later.
        self.key_count = 0
        self.deadman_trips = 0
        self.tot_trips = 0
        self.last_unkey_reason = ''

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono

    def setup_audio(self):
        return True  # WebSocket handler manages connections

    # ── PTT hold ────────────────────────────────────────────────────────
    def key(self):
        """Press or refresh from the browser. False when the TOT refuses.

        Called on the WebSocket thread.  Every refresh pushes the dead-man
        deadline out; the TOT deliberately measures from the FIRST press so
        refreshes cannot extend an over past the limit.
        """
        now = time.monotonic()
        with self._key_lock:
            if self._tot_tripped:
                return False
            if not self.tx_keyed:
                self.tx_keyed = True
                self._key_started = now
                self.key_count += 1
                print(f"[WEBMIC] PTT key #{self.key_count}")
            self._key_deadline = now + self.key_timeout
            return True

    def unkey(self, reason='release'):
        """Drop the key. Idempotent; safe from any thread.

        Also clears a tripped TOT — releasing the button is the reset.
        """
        with self._key_lock:
            was_keyed = self.tx_keyed
            held = time.monotonic() - self._key_started
            self.tx_keyed = False
            self._key_deadline = 0.0
            self._tot_tripped = False
            if was_keyed:
                self.last_unkey_reason = reason
        if was_keyed:
            print(f"[WEBMIC] PTT unkey after {held:.1f}s ({reason})")
        # Drop audio captured for the over that just ended — without this
        # the next press transmits the tail of the previous one.
        self._sub_buffer = b''
        while True:
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break

    def _key_active(self):
        """Evaluate both watchdogs. Bus thread. True while TX may proceed."""
        now = time.monotonic()
        with self._key_lock:
            if not self.tx_keyed:
                return False
            if now > self._key_deadline:
                self.tx_keyed = False
                self.deadman_trips += 1
                self.last_unkey_reason = 'deadman'
                print(f"[WEBMIC] ⚠ dead-man: no key refresh for "
                      f"{self.key_timeout:.1f}s — unkeyed")
                return False
            if now - self._key_started > self.max_tx:
                self.tx_keyed = False
                self._tot_tripped = True
                self.tot_trips += 1
                self.last_unkey_reason = 'tot'
                print(f"[WEBMIC] ⚠ time-out timer: {self.max_tx:.0f}s max "
                      f"transmission reached — unkeyed (release to reset)")
                return False
            return True

    def push_audio(self, pcm_bytes):
        """Called by WebSocket handler to push raw PCM into the queue."""
        try:
            self.audio_level = pcm_level(pcm_bytes, self.audio_level)
        except Exception:
            log_exc()
        try:
            self._chunk_queue.put_nowait(pcm_bytes)
        except _queue_mod.Full:
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(pcm_bytes)
            except _queue_mod.Full:
                pass

    def get_audio(self, chunk_size):
        if not self.enabled or self.muted or not self.client_connected:
            return None, False

        # Connected but not keyed — the socket lingering between overs, or a
        # watchdog having just dropped the key. Contribute nothing and ask
        # for no PTT; the bus unkeys after its own release delay.
        if not self._key_active():
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        cb = self._chunk_bytes

        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                break

        if len(self._sub_buffer) < cb:
            # Underrun mid-over: hold the key with silence rather than
            # letting a network hiccup chop the transmission into pieces.
            # Bounded by the dead-man above, so this cannot strand the key.
            return b'\x00' * cb, True

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering (for UI display only)
        self.audio_level = pcm_level(raw, self.audio_level)

        # Volume with tanh soft-clipping above unity — this feeds a
        # transmitter, and flat-topping a browser mic (already AGC'd) at
        # 4x would splatter square-wave harmonics across the channel.
        raw = apply_gain(raw, self.volume)

        return raw, True

    def is_active(self):
        return (self.enabled and not self.muted
                and self.client_connected and self.tx_keyed)

    def get_status(self):
        if not self.enabled:
            return "WEBMIC: Disabled"
        elif not self.client_connected:
            return "WEBMIC: Idle"
        elif self.tx_keyed:
            return f"WEBMIC: TX ({self.audio_level}%)"
        else:
            return "WEBMIC: Armed"

    def cleanup(self):
        self.unkey('cleanup')


class WebMonitorSource(AudioSource):
    """Receives browser monitor audio via WebSocket — VAD-gated PTT.

    Audio feeds into the bus system. On a listen bus, it mixes passively.
    On a solo bus with a TX radio, it keys PTT only when audio exceeds
    the VAD threshold (prevents keying on room silence/noise).
    """
    def __init__(self, config, gateway):
        super().__init__("MONITOR", config)
        self.gateway = gateway
        self.priority = 5
        self.ptt_control = True  # PTT capable, but gated by VAD in get_audio
        self.volume = float(getattr(config, 'WEB_MONITOR_VOLUME', 1.0))
        self.enabled = True
        self.muted = False
        self.vad_threshold_db = float(getattr(config, 'MONITOR_VAD_THRESHOLD', -40.0))

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono

    def setup_audio(self):
        return True

    def push_audio(self, pcm_bytes):
        """Called by WebSocket handler to push raw PCM into the queue."""
        try:
            self.audio_level = pcm_level(pcm_bytes, self.audio_level)
        except Exception:
            log_exc()
        try:
            self._chunk_queue.put_nowait(pcm_bytes)
        except _queue_mod.Full:
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(pcm_bytes)
            except _queue_mod.Full:
                pass

    def get_audio(self, chunk_size):
        if not self.enabled or self.muted or not self.client_connected:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        cb = self._chunk_bytes

        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                break

        if len(self._sub_buffer) < cb:
            return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering + VAD
        self.audio_level = pcm_level(raw, self.audio_level)
        vad_pass = pcm_db(raw) > self.vad_threshold_db

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, vad_pass  # PTT only when voice detected

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "MONITOR: Disabled"
        elif self.client_connected:
            return f"MONITOR: Live ({self.audio_level}%)"
        else:
            return "MONITOR: Idle"

    def cleanup(self):
        self._sub_buffer = b''
