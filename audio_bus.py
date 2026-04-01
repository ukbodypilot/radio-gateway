"""Audio Bus System — v2.0 mixer replacement.

Bus-based audio routing with priority-based ducking. Replaces the monolithic
AudioMixer class with a generic system where sources and sinks are assigned
to named busses, and bus type determines behaviour.

See docs/mixer-v2-design.md for architecture.
See docs/mixer-v2-progress.md for status.
"""

import math
import time
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# Module-level audio utilities
# ---------------------------------------------------------------------------

def check_signal_instant(audio_data, threshold_db=-60.0):
    """Check if audio contains signal above threshold (instant, no hysteresis).

    Returns True if RMS level exceeds threshold_db (relative to 16-bit full scale).
    """
    if not audio_data:
        return False
    try:
        arr = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        if len(arr) == 0:
            return False
        rms = float(np.sqrt(np.mean(arr * arr)))
        if rms > 0:
            db = 20.0 * math.log10(rms / 32767.0)
            return db > threshold_db
        return False
    except Exception:
        return False


def mix_audio_streams(audio1, audio2):
    """Additive mixing of two PCM streams with soft tanh limiter.

    Broadcast-style: sum both at full volume, compress peaks above knee.
    When only one source has signal, it plays at full level.
    """
    arr1 = np.frombuffer(audio1, dtype=np.int16).astype(np.float32)
    arr2 = np.frombuffer(audio2, dtype=np.int16).astype(np.float32)

    min_len = min(len(arr1), len(arr2))
    arr1 = arr1[:min_len]
    arr2 = arr2[:min_len]

    mixed = arr1 + arr2

    _KNEE = 24000.0
    _MAX = 32767.0
    abs_mixed = np.abs(mixed)
    over = abs_mixed > _KNEE
    if np.any(over):
        excess = (abs_mixed[over] - _KNEE) / (_MAX - _KNEE)
        compressed = _KNEE + (_MAX - _KNEE) * np.tanh(excess)
        mixed[over] = np.sign(mixed[over]) * compressed

    return mixed.astype(np.int16).tobytes()


def additive_mix(streams):
    """Mix N PCM streams via sum-and-clip (no limiter needed for same-tier sources).

    Returns None if no streams provided.
    """
    if not streams:
        return None
    result = None
    for pcm in streams:
        if pcm is None:
            continue
        if result is None:
            result = pcm
        else:
            result = mix_audio_streams(result, pcm)
    return result


def apply_fade_in(pcm, fade_samples=480):
    """Apply linear fade-in (0→1) over first fade_samples samples (10ms at 48kHz)."""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    fade_len = min(fade_samples, len(arr))
    if fade_len > 0:
        arr[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
    return arr.astype(np.int16).tobytes()


def apply_fade_out(pcm):
    """Apply linear fade-out (1→0) over entire chunk."""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if len(arr) > 0:
        arr *= np.linspace(1.0, 0.0, len(arr))
    return arr.astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BusOutput:
    """Output produced by a bus each tick."""
    audio: dict = field(default_factory=dict)           # {sink_name: pcm_bytes}
    ptt: dict = field(default_factory=dict)             # {radio_name: bool}
    active_sources: list = field(default_factory=list)   # source names with audio
    ducked_sources: list = field(default_factory=list)   # source names that were ducked
    status: dict = field(default_factory=dict)           # trace state, extra outputs

    @property
    def mixed_audio(self):
        """Convenience: return the first sink's audio (all sinks get same mix on ListenBus)."""
        for v in self.audio.values():
            return v
        return None


class SourceSlot:
    """Per-source per-bus state. Tracks signal detection, ducking, and inclusion."""

    def __init__(self, source, bus_priority, duckable=True, deterministic=False):
        self.source = source
        self.bus_priority = bus_priority
        self.duckable = duckable
        self.deterministic = deterministic

        # Signal detection (hysteresis)
        self.has_signal = False
        self.signal_continuous_start = 0.0
        self.last_signal_time = 0.0
        self.last_silence_time = 0.0

        # Inclusion (instant attack + held release)
        self.hold_until = 0.0
        self.prev_included = False

        # Peer ducking
        self.ducked_by_peer = False
        self.duck_cooldown_until = 0.0


class DuckGroup:
    """Cross-tier duck state machine.

    Manages the relationship between "ducker" sources (higher priority, not
    duckable) and "duckee" sources (lower priority, duckable). When duckers
    have signal, duckees are suppressed.

    Handles blob gap hold, transition padding, and reduck inhibit — all the
    proven mechanisms from the v1 mixer, but with no source-name references.
    """

    def __init__(self, switch_padding_time=1.0, reduck_inhibit_time=2.0,
                 blob_gap_hold_time=1.0):
        self.is_ducked = False
        self.prev_signal = False
        self.padding_end_time = 0.0
        self.transition_type = None         # 'out' = duck starting
        self.last_audio_time = 0.0          # for blob gap hold
        self.duck_in_time = 0.0             # for reduck inhibit
        self.duckee_active_at_transition = False

        self.switch_padding_time = switch_padding_time
        self.reduck_inhibit_time = reduck_inhibit_time
        self.blob_gap_hold_time = blob_gap_hold_time

    def update(self, ducker_has_signal, duckee_any_active, current_time):
        """Update duck state. Returns (should_duck, in_padding).

        should_duck: True if duckees should be suppressed this tick.
        in_padding: True if we're in the silence window of a transition.
        """
        # Blob gap hold: if ducker had signal recently, hold the duck state
        if ducker_has_signal:
            self.last_audio_time = current_time
        elif self.is_ducked:
            if current_time - self.last_audio_time < self.blob_gap_hold_time:
                ducker_has_signal = True  # hold through gap

        prev_signal = self.prev_signal
        self.prev_signal = ducker_has_signal

        # Reduck inhibit: block new duck-out for N seconds after duck-in
        reduck_inhibit = (current_time - self.duck_in_time) < self.reduck_inhibit_time

        # Duck-out transition: ducker just became active
        if (not self.is_ducked and ducker_has_signal
                and not prev_signal and not reduck_inhibit):
            self.is_ducked = True
            self.padding_end_time = current_time + self.switch_padding_time
            self.transition_type = 'out'
            self.duckee_active_at_transition = duckee_any_active

        # Duck-in transition: ducker just went inactive
        elif self.is_ducked and not ducker_has_signal and prev_signal:
            self.is_ducked = False
            self.padding_end_time = 0.0
            self.transition_type = None
            self.duck_in_time = current_time  # arm reduck inhibit

        # Compute output
        in_padding = (self.transition_type == 'out'
                      and current_time < self.padding_end_time)

        # During duck-out padding, both ducker and duckee are silenced for
        # a clean transition. After padding expires, only duckees are ducked.
        should_duck = self.is_ducked or in_padding

        return should_duck, in_padding


# ---------------------------------------------------------------------------
# Bus base class
# ---------------------------------------------------------------------------

class AudioBus:
    """Base class for all audio bus types."""

    def __init__(self, name, bus_type, config):
        self.name = name
        self.bus_type = bus_type
        self.config = config
        self.source_slots = []
        self.sink_names = []
        self.enabled = True

    def add_source(self, source, bus_priority, duckable=True, deterministic=False):
        """Add a source to this bus with the given priority."""
        slot = SourceSlot(source, bus_priority, duckable=duckable,
                          deterministic=deterministic)
        self.source_slots.append(slot)
        self.source_slots.sort(key=lambda s: s.bus_priority)

    def remove_source(self, name):
        """Remove a source by name."""
        self.source_slots = [s for s in self.source_slots if s.source.name != name]

    def add_sink(self, sink_name):
        """Register a sink that receives this bus's output."""
        if sink_name not in self.sink_names:
            self.sink_names.append(sink_name)

    def get_source(self, name):
        """Get a source by name (backward compat with AudioMixer)."""
        for slot in self.source_slots:
            if slot.source.name == name:
                return slot.source
        return None

    def get_source_slot(self, name):
        """Get a SourceSlot by source name."""
        for slot in self.source_slots:
            if slot.source.name == name:
                return slot
        return None

    @property
    def sources(self):
        """List of source objects (backward compat with AudioMixer)."""
        return [slot.source for slot in self.source_slots]

    def tick(self, chunk_size):
        """Process one audio cycle. Override in subclasses."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ListenBus — replaces AudioMixer._mix_simultaneous
# ---------------------------------------------------------------------------

class ListenBus(AudioBus):
    """Monitor bus: mix multiple sources with priority-based ducking.

    Sources are partitioned into two tiers:
    - Ducker tier (duckable=False): Radio RX, file playback, announcements, etc.
      When these have signal, duckee tier is suppressed.
    - Duckee tier (duckable=True): SDRs, remote audio, link endpoints, etc.
      Among duckees, lower bus_priority ducks higher (peer ducking).

    Preserves all proven mechanisms: hysteresis attack/release, blob gap hold,
    transition padding, reduck inhibit, peer duck cooldown, fade-in/fade-out.
    """

    def __init__(self, name, config):
        super().__init__(name, 'listen', config)

        # Cross-tier duck state machine
        self.duck_group = DuckGroup(
            switch_padding_time=getattr(config, 'SWITCH_PADDING_TIME', 1.0),
            reduck_inhibit_time=getattr(config, 'REDUCK_INHIBIT_TIME', 2.0),
            blob_gap_hold_time=1.0,
        )

        # Config for signal detection
        self.signal_attack_time = getattr(config, 'SIGNAL_ATTACK_TIME', 2.0)
        self.signal_release_time = getattr(config, 'SIGNAL_RELEASE_TIME', 3.0)
        self.signal_threshold_db = getattr(config, 'SDR_SIGNAL_THRESHOLD', -60.0)
        self.duck_cooldown_time = getattr(config, 'SDR_DUCK_COOLDOWN', 3.0)
        self.verbose = getattr(config, 'VERBOSE_LOGGING', False)

        # Backward compat
        self.call_count = 0
        self.mixing_mode = 'simultaneous'
        self._last_trace_state = {}

    def get_status(self):
        """Get status of all sources (backward compat with AudioMixer)."""
        return [slot.source.get_status() for slot in self.source_slots]

    # -- Signal detection with hysteresis --

    def _update_signal_hysteresis(self, slot, audio_data, current_time):
        """Update slot's hysteresis signal state. Returns slot.has_signal.

        Attack: signal must be continuously present for signal_attack_time.
        Release: silence must persist for signal_release_time.
        Any chunk of silence resets the attack timer.
        """
        signal_now = check_signal_instant(audio_data, self.signal_threshold_db)

        if signal_now:
            slot.last_signal_time = current_time
            if slot.signal_continuous_start == 0.0:
                slot.signal_continuous_start = current_time
        else:
            slot.last_silence_time = current_time
            slot.signal_continuous_start = 0.0

        if not slot.has_signal:
            if slot.signal_continuous_start > 0.0:
                duration = current_time - slot.signal_continuous_start
                if duration >= self.signal_attack_time:
                    slot.has_signal = True
                    if self.verbose:
                        print(f"  [Bus:{self.name}] {slot.source.name} ACTIVATED "
                              f"(continuous signal for {duration:.2f}s)")
        else:
            time_since = current_time - slot.last_signal_time
            if time_since >= self.signal_release_time:
                slot.has_signal = False
                if self.verbose:
                    print(f"  [Bus:{self.name}] {slot.source.name} RELEASED "
                          f"(silent for {time_since:.2f}s)")

        return slot.has_signal

    # -- Main tick --

    def tick(self, chunk_size):
        """Process one audio cycle through the 6-phase mixing pipeline."""
        self.call_count += 1
        current_time = time.monotonic()

        ptt_audio = None
        non_ptt_audio = None
        ptt_required = False
        active_sources = []
        ducked_sources = []

        ducker_slots = [s for s in self.source_slots
                        if not s.duckable and s.source.enabled]
        duckee_slots = [s for s in self.source_slots
                        if s.duckable and s.source.enabled]

        # ── Phase 1: Collect ducker (non-duckable) audio ──
        for slot in ducker_slots:
            audio, ptt = slot.source.get_audio(chunk_size)
            if audio is None:
                continue
            # Apply per-source gain (routing page slider)
            _boost = getattr(slot.source, 'audio_boost', 1.0)
            if _boost != 1.0 and audio:
                _arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
                audio = np.clip(_arr * _boost, -32768, 32767).astype(np.int16).tobytes()
            active_sources.append(slot.source.name)
            if ptt and slot.source.ptt_control:
                ptt_required = True
                ptt_audio = audio if ptt_audio is None else mix_audio_streams(ptt_audio, audio)
            else:
                non_ptt_audio = audio if non_ptt_audio is None else mix_audio_streams(non_ptt_audio, audio)

        # ── Phase 2: Cross-tier duck decision ──
        ducker_active = (ptt_audio is not None) or (non_ptt_audio is not None)
        radio_has_signal = False

        if ducker_active:
            # PTT sources are deterministic — immediately active
            ptt_is_active = ptt_audio is not None
            # Non-PTT ducker audio (Radio RX) uses hysteresis
            non_ptt_has_signal = False
            if non_ptt_audio is not None:
                # Use a virtual slot name for the ducker-tier hysteresis
                if not hasattr(self, '_ducker_rx_slot'):
                    self._ducker_rx_slot = SourceSlot(None, -1, duckable=False)
                non_ptt_has_signal = self._update_signal_hysteresis(
                    self._ducker_rx_slot, non_ptt_audio, current_time)
            ducker_active = ptt_is_active or non_ptt_has_signal
            radio_has_signal = non_ptt_has_signal

        duckee_any_active = any(s.prev_included for s in duckee_slots)
        should_duck, in_padding = self.duck_group.update(
            ducker_active, duckee_any_active, current_time)

        # During duck-out padding, silence the ducker audio too for clean transition
        if in_padding and self.duck_group.duckee_active_at_transition:
            non_ptt_audio_for_mix = None
        else:
            non_ptt_audio_for_mix = non_ptt_audio

        # ── Phase 3: Fetch duckee audio (always drain buffers) ──
        duckee_audio = {}
        for slot in duckee_slots:
            audio, _ptt = slot.source.get_audio(chunk_size)
            # Apply per-source gain (routing page slider)
            _boost = getattr(slot.source, 'audio_boost', 1.0)
            if _boost != 1.0 and audio:
                _arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
                audio = np.clip(_arr * _boost, -32768, 32767).astype(np.int16).tobytes()
            if should_duck:
                duckee_audio[slot] = None  # ducked — discard
                if audio is not None:
                    ducked_sources.append(slot.source.name)
            else:
                duckee_audio[slot] = audio

        # ── Phase 4: Peer ducking + signal + hold + fades ──
        # Reset prev_included on duck-in (forces fade-in when duckees resume)
        if not self.duck_group.is_ducked and self.duck_group.duck_in_time == current_time:
            for slot in duckee_slots:
                slot.prev_included = False

        to_include = {}  # slot -> processed audio

        for slot in sorted(duckee_audio.keys(), key=lambda s: s.bus_priority):
            audio = duckee_audio[slot]

            if audio is None and should_duck:
                continue  # cross-tier ducked, already counted

            # Update hysteresis for this slot
            if audio is not None:
                self._update_signal_hysteresis(slot, audio, current_time)

            # Peer ducking: higher-priority duckee with hysteresis signal ducks this one
            peer_ducked = False
            if not should_duck:
                for other in sorted(duckee_audio.keys(), key=lambda s: s.bus_priority):
                    if other.bus_priority >= slot.bus_priority:
                        break
                    if other is slot:
                        continue
                    if other.has_signal:
                        peer_ducked = True
                        break

                # Cooldown: recently-unducked slot gets immunity
                if peer_ducked and current_time < slot.duck_cooldown_until:
                    peer_ducked = False

            # Track peer duck transitions for cooldown
            if slot.ducked_by_peer and not peer_ducked and not should_duck:
                slot.duck_cooldown_until = current_time + self.duck_cooldown_time
            slot.ducked_by_peer = peer_ducked

            if peer_ducked:
                ducked_sources.append(slot.source.name)
                continue

            if audio is None:
                continue

            # Signal detection for inclusion (instant attack + held release)
            has_instant = check_signal_instant(audio, self.signal_threshold_db)
            if has_instant:
                slot.hold_until = current_time + self.signal_release_time

            hold_active = current_time < slot.hold_until
            include = has_instant or hold_active

            # Sole-source fallback: if this is the only duckee with signal, include it
            if not include and not should_duck:
                any_other_included = any(
                    s is not slot and (check_signal_instant(duckee_audio.get(s), self.signal_threshold_db)
                                       or current_time < s.hold_until)
                    for s in duckee_audio.keys()
                    if duckee_audio.get(s) is not None and not s.ducked_by_peer
                )
                if not any_other_included and has_instant:
                    include = True

            if include:
                if not slot.prev_included:
                    # First inclusion after silence/duck — fade in
                    audio = apply_fade_in(audio)
                slot.prev_included = True
                to_include[slot] = audio
                if slot.source.name not in active_sources:
                    active_sources.append(slot.source.name)
            elif slot.prev_included:
                # Was included, now excluded — fade out
                audio = apply_fade_out(audio)
                slot.prev_included = False
                to_include[slot] = audio
                if slot.source.name not in active_sources:
                    active_sources.append(slot.source.name)
            else:
                slot.prev_included = False

        # ── Phase 5: Mix included duckees ──
        duckee_pcm_list = [a for a in to_include.values() if a is not None]
        duckee_mix = additive_mix(duckee_pcm_list)
        duckee_only_audio = duckee_mix  # for SDR rebroadcast compat

        # ── Phase 6: Final assembly ──
        if ptt_audio is not None:
            mixed_audio = ptt_audio
        elif non_ptt_audio_for_mix is not None and duckee_mix is not None:
            mixed_audio = mix_audio_streams(non_ptt_audio_for_mix, duckee_mix)
        elif non_ptt_audio_for_mix is not None:
            mixed_audio = non_ptt_audio_for_mix
        elif duckee_mix is not None:
            mixed_audio = duckee_mix
        else:
            mixed_audio = None

        # rx_audio: non-PTT audio for concurrent RX during PTT
        rx_audio = non_ptt_audio if ptt_required else None

        # Build trace state (backward compat with gateway_core trace recording)
        self._last_trace_state = {
            'dk': self.duck_group.is_ducked,
            'hold': current_time - self.duck_group.last_audio_time < self.duck_group.blob_gap_hold_time if self.duck_group.is_ducked else False,
            'pad': in_padding,
            'tOut': self.duck_group.transition_type == 'out',
            'ducks': should_duck,
            'radio_sig': radio_has_signal,
            'other_active': ducker_active,
            'aioc_gap': not ducker_active and self.duck_group.is_ducked,
            'reduck_inhibit': (current_time - self.duck_group.duck_in_time) < self.duck_group.reduck_inhibit_time,
            'sdrs': {},
        }
        for slot in duckee_slots:
            self._last_trace_state['sdrs'][slot.source.name] = {
                'ducked': slot.source.name in ducked_sources,
                'signal': slot.has_signal,
                'hold_inc': current_time < slot.hold_until,
                'included': slot in to_include,
                'fi': slot in to_include and not slot.prev_included,  # fade-in fired
                'fo': slot not in to_include and slot.prev_included,  # fade-out fired
            }

        # Build output
        audio_dict = {sink: mixed_audio for sink in self.sink_names}
        if not self.sink_names:
            audio_dict['_default'] = mixed_audio

        return BusOutput(
            audio=audio_dict,
            ptt={'_ptt_required': ptt_required},
            active_sources=active_sources,
            ducked_sources=ducked_sources,
            status={
                'rx_audio': rx_audio,
                'duckee_only_audio': duckee_only_audio,
                'trace': self._last_trace_state,
            },
        )


# ---------------------------------------------------------------------------
# Future bus types (stubs)
# ---------------------------------------------------------------------------

class SoloBus(AudioBus):
    """Standalone control of a single radio with its own source/sink pipeline.

    One radio plugin sits at the center:
    - TX sources (webmic, announcements, file playback) → mixed → radio.put_audio()
    - Radio.get_audio() → routed to all registered sinks

    PTT is controlled by whichever TX source has ptt_control=True and is
    producing audio. When PTT audio stops, PTT releases after a hold time.

    Sources are added with add_source(). The radio is set with set_radio().
    Sinks are added with add_sink().
    """

    def __init__(self, name, config):
        super().__init__(name, 'solo', config)
        self._radio = None          # The radio plugin (has get_audio/put_audio)
        self._tx_only = False       # If True, radio is TX-only (don't call get_audio)
        self._tx_sources = []       # SourceSlots for TX sources (webmic, announce, etc.)
        self._ptt_active = False
        self._ptt_hold_until = 0.0
        self._ptt_release_delay = float(getattr(config, 'PTT_RELEASE_DELAY', 1.0))
        self.call_count = 0

    def set_radio(self, radio_plugin):
        """Set the radio plugin at the center of this bus."""
        self._radio = radio_plugin

    def add_tx_source(self, source, bus_priority=0):
        """Add a TX source (webmic, announcements, etc.) that feeds the radio."""
        slot = SourceSlot(source, bus_priority, duckable=False,
                          deterministic=source.ptt_control)
        self._tx_sources.append(slot)
        self._tx_sources.sort(key=lambda s: s.bus_priority)

    def tick(self, chunk_size):
        """Process one audio cycle.

        1. Collect audio from TX sources → mix → send to radio via put_audio()
        2. Get audio from radio via get_audio() → deliver to sinks
        3. Manage PTT state
        """
        self.call_count += 1
        current_time = time.monotonic()

        active_sources = []
        ptt_needed = False
        tx_audio = None

        # ── Phase 1: Collect TX source audio ──
        for slot in self._tx_sources:
            if not slot.source.enabled:
                continue
            audio, ptt = slot.source.get_audio(chunk_size)
            if audio is None:
                continue
            # Apply per-source gain
            _boost = getattr(slot.source, 'audio_boost', 1.0)
            if _boost != 1.0:
                _arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
                audio = np.clip(_arr * _boost, -32768, 32767).astype(np.int16).tobytes()
            active_sources.append(slot.source.name)
            if ptt and slot.source.ptt_control:
                ptt_needed = True
            if tx_audio is None:
                tx_audio = audio
            else:
                tx_audio = mix_audio_streams(tx_audio, audio)

        # ── Phase 2: PTT management ──
        if ptt_needed:
            self._ptt_hold_until = current_time + self._ptt_release_delay
            if not self._ptt_active and self._radio:
                # Key PTT
                self._ptt_active = True
                print(f"  [SoloBus:{self.name}] PTT ON via {self._radio.name if hasattr(self._radio, 'name') else type(self._radio).__name__}")
                if hasattr(self._radio, 'execute'):
                    self._radio.execute({'cmd': 'ptt', 'state': True})
                elif hasattr(self._radio, 'ptt_on'):
                    self._radio.ptt_on()

        if self._ptt_active and current_time > self._ptt_hold_until:
            # Release PTT
            self._ptt_active = False
            if self._radio and hasattr(self._radio, 'execute'):
                self._radio.execute({'cmd': 'ptt', 'state': False})
            elif self._radio and hasattr(self._radio, 'ptt_off'):
                self._radio.ptt_off()

        # ── Phase 3: Send TX audio to radio ──
        if tx_audio is not None and self._radio and self._ptt_active:
            if hasattr(self._radio, 'put_audio'):
                self._radio.put_audio(tx_audio)
            elif hasattr(self._radio, 'write_tx_audio'):
                self._radio.write_tx_audio(tx_audio)

        # ── Phase 4: Get RX audio from radio (skip if TX-only) ──
        rx_audio = None
        if self._radio and not self._tx_only:
            rx_audio, _rx_ptt = self._radio.get_audio(chunk_size)
            if rx_audio is not None:
                active_sources.append(self._radio.name)

        # ── Phase 5: Build output ──
        # If no radio, route TX audio directly to sinks (e.g. Mumble TX as sink)
        _output_audio = rx_audio if self._radio else tx_audio
        audio_dict = {sink: _output_audio for sink in self.sink_names}
        if not self.sink_names:
            audio_dict['_default'] = _output_audio

        return BusOutput(
            audio=audio_dict,
            ptt={self._radio.name if self._radio else '_radio': self._ptt_active},
            active_sources=active_sources,
            ducked_sources=[],
            status={
                'tx_audio_active': tx_audio is not None,
                'ptt_active': self._ptt_active,
                'rx_audio': rx_audio,
            },
        )


class DuplexRepeaterBus(AudioBus):
    """Full duplex cross-link between two radios.

    Side A and Side B are radio plugins. Audio flows both directions
    simultaneously:
      A.get_audio() → B.put_audio()  (A's RX becomes B's TX)
      B.get_audio() → A.put_audio()  (B's RX becomes A's TX)

    PTT on each side is keyed whenever the OTHER side has RX audio.
    Both directions can be active at the same time (full duplex —
    radios must be on different frequencies).

    Optional sinks can be attached to receive a mix of both sides
    (e.g. for recording the cross-link).
    """

    def __init__(self, name, config):
        super().__init__(name, 'duplex_repeater', config)
        self._side_a = None         # Radio plugin
        self._side_b = None         # Radio plugin
        self._a_ptt_active = False
        self._b_ptt_active = False
        self._a_ptt_hold_until = 0.0
        self._b_ptt_hold_until = 0.0
        self._ptt_hold_time = float(getattr(config, 'REPEATER_PTT_HOLD', 1.0))
        self._signal_threshold = float(getattr(config, 'SDR_SIGNAL_THRESHOLD', -60.0))
        self.call_count = 0

    def set_side_a(self, radio_plugin):
        """Set the Side A radio plugin."""
        self._side_a = radio_plugin

    def set_side_b(self, radio_plugin):
        """Set the Side B radio plugin."""
        self._side_b = radio_plugin

    def tick(self, chunk_size):
        """Process one audio cycle — cross-link both directions.

        1. Get RX audio from both sides
        2. Route A's RX → B's TX (with PTT)
        3. Route B's RX → A's TX (with PTT)
        4. Deliver mixed audio to sinks (for recording/monitoring)
        """
        self.call_count += 1
        current_time = time.monotonic()
        active_sources = []

        # ── Phase 1: Get RX audio from both sides ──
        a_rx = None
        b_rx = None

        if self._side_a:
            a_rx, _a_ptt = self._side_a.get_audio(chunk_size)
            if a_rx is not None:
                active_sources.append(self._side_a.name)

        if self._side_b:
            b_rx, _b_ptt = self._side_b.get_audio(chunk_size)
            if b_rx is not None:
                active_sources.append(self._side_b.name)

        # ── Phase 2: A's RX → B's TX ──
        a_has_signal = check_signal_instant(a_rx, self._signal_threshold) if a_rx else False

        if a_has_signal:
            self._b_ptt_hold_until = current_time + self._ptt_hold_time
            if not self._b_ptt_active and self._side_b:
                # Key B's PTT
                self._b_ptt_active = True
                if hasattr(self._side_b, 'execute'):
                    self._side_b.execute({'cmd': 'ptt', 'state': True})
                elif hasattr(self._side_b, 'ptt_on'):
                    self._side_b.ptt_on()

        if self._b_ptt_active and self._side_b:
            if a_rx is not None:
                if hasattr(self._side_b, 'put_audio'):
                    self._side_b.put_audio(a_rx)
                elif hasattr(self._side_b, 'write_tx_audio'):
                    self._side_b.write_tx_audio(a_rx)

        if self._b_ptt_active and current_time > self._b_ptt_hold_until:
            # Release B's PTT
            self._b_ptt_active = False
            if self._side_b:
                if hasattr(self._side_b, 'execute'):
                    self._side_b.execute({'cmd': 'ptt', 'state': False})
                elif hasattr(self._side_b, 'ptt_off'):
                    self._side_b.ptt_off()

        # ── Phase 3: B's RX → A's TX ──
        b_has_signal = check_signal_instant(b_rx, self._signal_threshold) if b_rx else False

        if b_has_signal:
            self._a_ptt_hold_until = current_time + self._ptt_hold_time
            if not self._a_ptt_active and self._side_a:
                # Key A's PTT
                self._a_ptt_active = True
                if hasattr(self._side_a, 'execute'):
                    self._side_a.execute({'cmd': 'ptt', 'state': True})
                elif hasattr(self._side_a, 'ptt_on'):
                    self._side_a.ptt_on()

        if self._a_ptt_active and self._side_a:
            if b_rx is not None:
                if hasattr(self._side_a, 'put_audio'):
                    self._side_a.put_audio(b_rx)
                elif hasattr(self._side_a, 'write_tx_audio'):
                    self._side_a.write_tx_audio(b_rx)

        if self._a_ptt_active and current_time > self._a_ptt_hold_until:
            # Release A's PTT
            self._a_ptt_active = False
            if self._side_a:
                if hasattr(self._side_a, 'execute'):
                    self._side_a.execute({'cmd': 'ptt', 'state': False})
                elif hasattr(self._side_a, 'ptt_off'):
                    self._side_a.ptt_off()

        # ── Phase 4: Build output ──
        # Mix both RX streams for sink delivery (recording/monitoring)
        if a_rx is not None and b_rx is not None:
            mixed = mix_audio_streams(a_rx, b_rx)
        elif a_rx is not None:
            mixed = a_rx
        elif b_rx is not None:
            mixed = b_rx
        else:
            mixed = None

        audio_dict = {sink: mixed for sink in self.sink_names}
        if not self.sink_names:
            audio_dict['_default'] = mixed

        a_name = self._side_a.name if self._side_a else 'A'
        b_name = self._side_b.name if self._side_b else 'B'

        return BusOutput(
            audio=audio_dict,
            ptt={
                a_name: self._a_ptt_active,
                b_name: self._b_ptt_active,
            },
            active_sources=active_sources,
            ducked_sources=[],
            status={
                'a_rx_signal': a_has_signal,
                'b_rx_signal': b_has_signal,
                'a_ptt': self._a_ptt_active,
                'b_ptt': self._b_ptt_active,
            },
        )


class SimplexRepeaterBus(AudioBus):
    """Half-duplex store-and-forward between two radios.

    Only one direction active at a time:
    1. Side A receives → audio buffers
    2. A's signal drops → tail timer expires → buffer plays out on B's TX
    3. Then reverses: B receives → buffers → plays out on A's TX

    State machine:
      IDLE     → A or B has signal → RECEIVING_A or RECEIVING_B
      RECEIVING_A → A signal drops → TAIL_A (wait for tail timer)
      TAIL_A   → tail expires → PLAYING_B (transmit buffer on B)
      PLAYING_B → buffer empty → IDLE
      (mirror for B→A direction)

    Configurable tail timer (how long to wait after RX before TX).
    Optional courtesy tone between RX and TX (not yet implemented).
    """

    # States
    IDLE = 'idle'
    RECEIVING_A = 'rx_a'
    RECEIVING_B = 'rx_b'
    TAIL_A = 'tail_a'       # A stopped, waiting before TX on B
    TAIL_B = 'tail_b'       # B stopped, waiting before TX on A
    PLAYING_B = 'play_b'    # Playing buffered A audio on B's TX
    PLAYING_A = 'play_a'    # Playing buffered B audio on A's TX

    def __init__(self, name, config):
        super().__init__(name, 'simplex_repeater', config)
        self._side_a = None
        self._side_b = None
        self._state = self.IDLE
        self._signal_threshold = float(getattr(config, 'SDR_SIGNAL_THRESHOLD', -60.0))
        self._tail_time = float(getattr(config, 'SIMPLEX_TAIL_TIME', 1.0))
        self._tail_expire = 0.0
        self._buffer = []           # list of PCM chunks
        self._buffer_pos = 0        # playback position in buffer
        self._max_buffer_secs = float(getattr(config, 'SIMPLEX_MAX_BUFFER', 30.0))
        self._max_buffer_chunks = 0  # set in first tick
        self._ptt_active_a = False
        self._ptt_active_b = False
        self.call_count = 0

    def set_side_a(self, radio_plugin):
        self._side_a = radio_plugin

    def set_side_b(self, radio_plugin):
        self._side_b = radio_plugin

    def tick(self, chunk_size):
        """Process one audio cycle through the simplex state machine."""
        self.call_count += 1
        current_time = time.monotonic()
        active_sources = []

        if self._max_buffer_chunks == 0:
            # ~20 chunks/sec at 50ms each
            self._max_buffer_chunks = int(self._max_buffer_secs * 20)

        # Get RX audio from both sides (always drain to avoid stale buffers)
        a_rx, _a_ptt = self._side_a.get_audio(chunk_size) if self._side_a else (None, False)
        b_rx, _b_ptt = self._side_b.get_audio(chunk_size) if self._side_b else (None, False)

        a_has_signal = check_signal_instant(a_rx, self._signal_threshold) if a_rx else False
        b_has_signal = check_signal_instant(b_rx, self._signal_threshold) if b_rx else False

        if a_rx is not None:
            active_sources.append(self._side_a.name if self._side_a else 'A')
        if b_rx is not None:
            active_sources.append(self._side_b.name if self._side_b else 'B')

        playback_audio = None  # audio being played out this tick

        # ── State machine ──
        if self._state == self.IDLE:
            if a_has_signal:
                self._state = self.RECEIVING_A
                self._buffer = []
                self._buffer.append(a_rx)
            elif b_has_signal:
                self._state = self.RECEIVING_B
                self._buffer = []
                self._buffer.append(b_rx)

        elif self._state == self.RECEIVING_A:
            if a_has_signal:
                if len(self._buffer) < self._max_buffer_chunks:
                    self._buffer.append(a_rx)
            else:
                # Signal dropped — start tail timer
                self._state = self.TAIL_A
                self._tail_expire = current_time + self._tail_time

        elif self._state == self.TAIL_A:
            if a_has_signal:
                # Signal came back — resume receiving
                self._state = self.RECEIVING_A
                if a_rx and len(self._buffer) < self._max_buffer_chunks:
                    self._buffer.append(a_rx)
            elif current_time >= self._tail_expire:
                # Tail expired — start playing on B
                self._state = self.PLAYING_B
                self._buffer_pos = 0
                # Key B's PTT
                self._ptt_active_b = True
                if self._side_b:
                    if hasattr(self._side_b, 'execute'):
                        self._side_b.execute({'cmd': 'ptt', 'state': True})
                    elif hasattr(self._side_b, 'ptt_on'):
                        self._side_b.ptt_on()

        elif self._state == self.PLAYING_B:
            if self._buffer_pos < len(self._buffer):
                pcm = self._buffer[self._buffer_pos]
                self._buffer_pos += 1
                if self._side_b:
                    if hasattr(self._side_b, 'put_audio'):
                        self._side_b.put_audio(pcm)
                    elif hasattr(self._side_b, 'write_tx_audio'):
                        self._side_b.write_tx_audio(pcm)
                playback_audio = pcm
            else:
                # Buffer exhausted — unkey B, return to idle
                self._ptt_active_b = False
                if self._side_b:
                    if hasattr(self._side_b, 'execute'):
                        self._side_b.execute({'cmd': 'ptt', 'state': False})
                    elif hasattr(self._side_b, 'ptt_off'):
                        self._side_b.ptt_off()
                self._buffer = []
                self._state = self.IDLE

        elif self._state == self.RECEIVING_B:
            if b_has_signal:
                if len(self._buffer) < self._max_buffer_chunks:
                    self._buffer.append(b_rx)
            else:
                self._state = self.TAIL_B
                self._tail_expire = current_time + self._tail_time

        elif self._state == self.TAIL_B:
            if b_has_signal:
                self._state = self.RECEIVING_B
                if b_rx and len(self._buffer) < self._max_buffer_chunks:
                    self._buffer.append(b_rx)
            elif current_time >= self._tail_expire:
                self._state = self.PLAYING_A
                self._buffer_pos = 0
                self._ptt_active_a = True
                if self._side_a:
                    if hasattr(self._side_a, 'execute'):
                        self._side_a.execute({'cmd': 'ptt', 'state': True})
                    elif hasattr(self._side_a, 'ptt_on'):
                        self._side_a.ptt_on()

        elif self._state == self.PLAYING_A:
            if self._buffer_pos < len(self._buffer):
                pcm = self._buffer[self._buffer_pos]
                self._buffer_pos += 1
                if self._side_a:
                    if hasattr(self._side_a, 'put_audio'):
                        self._side_a.put_audio(pcm)
                    elif hasattr(self._side_a, 'write_tx_audio'):
                        self._side_a.write_tx_audio(pcm)
                playback_audio = pcm
            else:
                self._ptt_active_a = False
                if self._side_a:
                    if hasattr(self._side_a, 'execute'):
                        self._side_a.execute({'cmd': 'ptt', 'state': False})
                    elif hasattr(self._side_a, 'ptt_off'):
                        self._side_a.ptt_off()
                self._buffer = []
                self._state = self.IDLE

        # ── Build output ──
        # Sinks get whatever is currently active (RX or playback)
        sink_audio = playback_audio or a_rx or b_rx
        audio_dict = {sink: sink_audio for sink in self.sink_names}
        if not self.sink_names:
            audio_dict['_default'] = sink_audio

        a_name = self._side_a.name if self._side_a else 'A'
        b_name = self._side_b.name if self._side_b else 'B'

        return BusOutput(
            audio=audio_dict,
            ptt={
                a_name: self._ptt_active_a,
                b_name: self._ptt_active_b,
            },
            active_sources=active_sources,
            ducked_sources=[],
            status={
                'state': self._state,
                'buffer_chunks': len(self._buffer),
                'buffer_pos': self._buffer_pos,
                'a_signal': a_has_signal,
                'b_signal': b_has_signal,
                'a_ptt': self._ptt_active_a,
                'b_ptt': self._ptt_active_b,
            },
        )
