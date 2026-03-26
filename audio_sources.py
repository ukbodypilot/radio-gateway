#!/usr/bin/env python3
"""Audio source and mixer classes for radio-gateway."""

import sys
import os
import time
import signal
import threading
import threading as _thr
import subprocess
import shutil
import json as json_mod
import collections
import queue as _queue_mod
from struct import Struct
import socket
import select
import array as _array_mod
import math as _math_mod
import re
import numpy as np

try:
    import hid
except ImportError:
    print("ERROR: hidapi library not found!")
    print("Install it with: pip3 install hidapi --break-system-packages")
    sys.exit(1)

try:
    import pyaudio
except ImportError:
    print("ERROR: pyaudio library not found!")
    print("Install it with: sudo apt-get install python3-pyaudio")
    sys.exit(1)

class AudioSource:
    """Base class for all audio sources"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.enabled = True
        self.priority = 0  # Lower = higher priority
        self.volume = 1.0
        self.ptt_control = False  # Can this source trigger PTT?
        
    def initialize(self):
        """Initialize the audio source. Return True on success."""
        return True
    
    def cleanup(self):
        """Clean up resources"""
        pass
    
    def get_audio(self, chunk_size):
        """
        Get audio chunk from this source.
        Returns: (audio_bytes, should_trigger_ptt)
        audio_bytes: PCM audio data or None
        should_trigger_ptt: True if this audio should key PTT
        """
        return None, False
    
    def is_active(self):
        """Return True if source currently has audio to transmit"""
        return False
    
    def get_status(self):
        """Return status string for display"""
        return f"{self.name}: {'ON' if self.enabled else 'OFF'}"


class AudioProcessor:
    """Per-source audio processing chain with independent filter state.

    Each audio source (Radio, SDR1, SDR2, etc.) gets its own AudioProcessor
    instance so filters run independently with their own state (envelope,
    filter memory, etc.) and can be toggled per-source.
    """

    def __init__(self, name, config):
        self.name = name          # e.g. "radio", "sdr"
        self.config = config      # gateway Config object (for AUDIO_RATE, etc.)

        # Per-source enable flags (set from config or toggled at runtime)
        self.enable_hpf = False
        self.hpf_cutoff = 300         # Hz
        self.enable_lpf = False
        self.lpf_cutoff = 3000        # Hz
        self.enable_notch = False
        self.notch_freq = 1000        # Hz — target frequency
        self.notch_q = 30.0           # Q factor (higher = narrower notch)
        self.enable_noise_gate = False
        self.gate_threshold = -40     # dB
        self.gate_attack = 0.01       # seconds
        self.gate_release = 0.1       # seconds

        # Filter state (persists across audio chunks for continuity)
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0

    def reset_state(self):
        """Reset all filter states (e.g. when source restarts)."""
        self.highpass_state = None
        self.lowpass_state = None
        self.notch_state = None
        self.gate_envelope = 0.0

    def process(self, pcm_data):
        """Run the full processing chain on PCM data. Order:
        HPF → LPF → Notch → Noise Gate
        """
        if not pcm_data:
            return pcm_data

        processed = pcm_data

        if self.enable_hpf:
            processed = self._apply_hpf(processed)

        if self.enable_lpf:
            processed = self._apply_lpf(processed)

        if self.enable_notch:
            processed = self._apply_notch(processed)

        if self.enable_noise_gate:
            processed = self._apply_noise_gate(processed)

        return processed

    def get_active_list(self):
        """Return list of active filter names for status display."""
        active = []
        if self.enable_noise_gate: active.append('Gate')
        if self.enable_hpf: active.append('HPF')
        if self.enable_lpf: active.append('LPF')
        if self.enable_notch: active.append(f'Notch')
        return active

    # --- Filter implementations ---

    def _apply_hpf(self, pcm_data):
        """First-order IIR high-pass filter."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.hpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b = np.array([alpha, -alpha], dtype=np.float64)
            a = np.array([1.0, -alpha], dtype=np.float64)

            if self.highpass_state is None:
                self.highpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.highpass_state = lfilter(b, a, samples, zi=self.highpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_lpf(self, pcm_data):
        """First-order IIR low-pass filter — cuts high-frequency hiss above cutoff."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            cutoff = self.lpf_cutoff
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = dt / (rc + dt)

            b = np.array([alpha], dtype=np.float64)
            a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)

            if self.lowpass_state is None:
                self.lowpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.lowpass_state = lfilter(b, a, samples, zi=self.lowpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_notch(self, pcm_data):
        """Second-order IIR notch (band-stop) filter — removes a specific frequency."""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            sample_rate = self.config.AUDIO_RATE
            w0 = 2.0 * math.pi * self.notch_freq / sample_rate
            bw = w0 / self.notch_q
            r = 1.0 - (bw / 2.0)
            r = max(0.0, min(r, 0.9999))  # clamp for stability

            # Transfer function: H(z) = (1 - 2cos(w0)z^-1 + z^-2) / (1 - 2r*cos(w0)z^-1 + r^2*z^-2)
            cos_w0 = math.cos(w0)
            b = np.array([1.0, -2.0 * cos_w0, 1.0], dtype=np.float64)
            a = np.array([1.0, -2.0 * r * cos_w0, r * r], dtype=np.float64)
            # Normalize so passband gain = 1
            b = b / (1.0 + abs(1.0 - r))

            if self.notch_state is None:
                self.notch_state = lfilter_zi(b, a) * 0.0

            filtered, self.notch_state = lfilter(b, a, samples, zi=self.notch_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return pcm_data

    def _apply_noise_gate(self, pcm_data):
        """Noise gate with attack/release envelope."""
        try:
            import array as _arr
            import math

            samples = _arr.array('h', pcm_data)
            if len(samples) == 0:
                return pcm_data

            threshold_db = self.gate_threshold
            threshold = 32767.0 * pow(10.0, threshold_db / 20.0)

            attack_samples = self.gate_attack * self.config.AUDIO_RATE
            release_samples = self.gate_release * self.config.AUDIO_RATE

            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1

            gated = []
            for sample in samples:
                level = abs(sample)

                if level > self.gate_envelope:
                    self.gate_envelope += (level - self.gate_envelope) * attack_coef
                else:
                    self.gate_envelope += (level - self.gate_envelope) * release_coef

                if self.gate_envelope > threshold:
                    gain = 1.0
                else:
                    ratio = self.gate_envelope / threshold if threshold > 0 else 0
                    gain = ratio * ratio

                gated.append(int(sample * gain))

            return _arr.array('h', gated).tobytes()
        except Exception:
            return pcm_data


class AIOCRadioSource(AudioSource):
    """Radio audio source via AIOC device"""
    def __init__(self, config, gateway):
        super().__init__("Radio", config)
        self.gateway = gateway  # Reference to main gateway for shared resources
        self.priority = 1  # Lower priority than file playback
        self.ptt_control = False  # Radio RX doesn't control PTT
        self.volume = config.INPUT_VOLUME

        # Queue for audio blobs delivered by PortAudio's callback thread.
        # The ALSA period is opened at 4×AUDIO_CHUNK_SIZE so each callback
        # delivers one 200ms blob.  get_audio() pre-buffers 3 blobs (600ms)
        # before first serve, then slices into 50ms sub-chunks (non-blocking).
        # WARNING: Do NOT reduce blob_mult below 4 or pre-buffer below 3.
        # AIOC USB audio has significant jitter; smaller values cause clicks,
        # robot sounds, and volume discontinuities. Tested 2× and 1× — both
        # produced artifacts. These values are the proven minimum for clean audio.
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._blob_mult = 4  # ALSA period = 4×AUDIO_CHUNK_SIZE — DO NOT REDUCE
        self._blob_bytes = config.AUDIO_CHUNK_SIZE * self._blob_mult * config.AUDIO_CHANNELS * 2
        # Pre-compute sizes for the hot callback path and get_audio() slicer.
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * config.AUDIO_CHANNELS * 2  # 16-bit
        self._chunk_secs = config.AUDIO_CHUNK_SIZE / config.AUDIO_RATE           # ~0.05 s
        # Sub-chunk slicing state (accessed only from the get_audio() call site).
        self._sub_buffer = b''
        self._prebuffering = True   # Wait for 3 blobs before first serve
        self._last_blocked_ms = 0.0  # instrumentation: how long get_audio blocked on blob fetch

        # Enhanced trace instrumentation
        self._cb_overflow_count = 0
        self._cb_underflow_count = 0
        self._cb_drop_count = 0
        self._last_cb_status = 0
        self._last_serve_sample = 0
        self._serve_discontinuity = 0.0
        self._sub_buffer_after = 0

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """PortAudio input callback — invoked at each ALSA period (4×AUDIO_CHUNK_SIZE frames).

        Each callback delivers a 200ms blob.  get_audio() pre-buffers 3 blobs
        (600ms cushion) before starting to serve, then slices into 50ms sub-chunks.

        Keep this method minimal — it runs in PortAudio's audio thread."""
        if status:
            self._last_cb_status = status
            if status & 0x2:  # paInputOverflow
                self._cb_overflow_count += 1
            if status & 0x1:  # paInputUnderflow
                self._cb_underflow_count += 1
        if in_data:
            try:
                self._chunk_queue.put_nowait(in_data)
            except _queue_mod.Full:
                self._cb_drop_count += 1
        return (None, pyaudio.paContinue)

    def cleanup(self):
        pass  # No resources to release; PortAudio stream is owned by the gateway

    def get_audio(self, chunk_size):
        """Get audio from radio via AIOC input stream"""
        # Reset the full-duplex cache every call so stale data is never forwarded
        self._rx_cache = None

        if not self.gateway.input_stream or self.gateway.restarting_stream:
            return None, False

        # Mute check BEFORE blob fetch — avoids blocking the main loop for
        # 60ms to get data that would be thrown away.  Flush stale data so
        # the sub-buffer is fresh when unmuted.
        if self.gateway.rx_muted:
            self._sub_buffer = b''
            self._prebuffering = True
            while not self._chunk_queue.empty():
                try:
                    self._chunk_queue.get_nowait()
                except _queue_mod.Empty:
                    break
            self._last_blocked_ms = 0.0
            return None, False

        try:
            # Eagerly drain all available blobs from queue into the sub-buffer.
            # This is critical for the pre-buffer gate: the old loop only fetched
            # when sub_buffer < chunk_bytes, which starved the pre-buffer check.
            cb = self._chunk_bytes
            _t0 = time.monotonic()
            _fetched = False
            while True:
                try:
                    blob = self._chunk_queue.get_nowait()
                    self._sub_buffer += blob
                    _fetched = True
                except _queue_mod.Empty:
                    break
            self._last_blocked_ms = (time.monotonic() - _t0) * 1000 if _fetched else 0.0

            # Cap sub-buffer to prevent stale audio buildup under CPU load.
            if self._blob_bytes > 0 and len(self._sub_buffer) > self._blob_bytes * 5:
                self._sub_buffer = self._sub_buffer[-(self._blob_bytes * 5):]

            # Pre-buffer gate: after the sub-buffer empties, accumulate 3 blobs
            # (600ms cushion) before serving.  Absorbs USB delivery jitter.
            if self._prebuffering:
                if len(self._sub_buffer) < self._blob_bytes * 3:
                    return None, False  # still accumulating
                self._prebuffering = False

            if len(self._sub_buffer) < cb:
                self._prebuffering = True  # depleted — re-enter prebuffer
                return None, False

            data = self._sub_buffer[:cb]
            self._sub_buffer = self._sub_buffer[cb:]
            self._sub_buffer_after = len(self._sub_buffer)

            # Sample discontinuity detection
            if len(data) >= 2:
                first_sample = int.from_bytes(data[0:2], byteorder='little', signed=True)
                self._serve_discontinuity = float(abs(first_sample - self._last_serve_sample))
                self._last_serve_sample = int.from_bytes(data[-2:], byteorder='little', signed=True)

            # Update capture time so stream-health checks stay happy
            self.gateway.last_audio_capture_time = time.time()
            self.gateway.last_successful_read = time.time()
            self.gateway.audio_capture_active = True

            # Apply volume if needed
            if self.volume != 1.0 and data:
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                data = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

            # Apply audio processing
            data = self.gateway.process_audio_for_mumble(data)

            # Apply click-suppression envelope for 150ms after any PTT state change.
            # We use a time-based window (not a one-shot flag) because the sub-chunk
            # slicing in get_audio() means the AIOC transient from the HID write can
            # appear 1-3 sub-chunks after the flag would otherwise be cleared.
            # Gain: 0 for first 30 ms, linearly ramps 0→1 from 30 ms to 130 ms.
            t_since_ptt = time.monotonic() - self.gateway._ptt_change_time
            if t_since_ptt < 0.130 and data:
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                n = len(arr)
                t_samples = t_since_ptt + np.arange(n, dtype=np.float32) / self.config.AUDIO_RATE
                gain = np.clip((t_samples - 0.030) / 0.100, 0.0, 1.0)
                data = (arr * gain).astype(np.int16).tobytes()

            # Cache the processed audio for full-duplex forwarding during PTT.
            # The transmit loop reads this directly so RX → Mumble works even if
            # VAD is blocking and regardless of ptt_active timing in the mixer.
            self._rx_cache = data

            # Check VAD - always call to keep the envelope/state current.
            should_transmit = self.gateway.check_vad(data)

            # Calculate audio level for the RX (AIOC) status bar — gate on AIOC's OWN
            # VAD result, not gateway.vad_active (shared global state).
            # Problem: check_vad() in the main loop is also called on the MIXED audio
            # (AIOC + SDR) every tick AFTER this method returns.  That call can set
            # vad_active=True due to SDR signal even when AIOC is silent.  The next
            # tick's get_audio() would then read vad_active=True and calculate the AIOC
            # noise floor level (~20%), making the RX bar show false activity.
            # Using should_transmit (this call's local result) means the bar only lights
            # up when AIOC itself has signal above threshold.
            current_level = self.gateway.calculate_audio_level(data) if should_transmit else 0
            if current_level > self.gateway.tx_audio_level:
                self.gateway.tx_audio_level = current_level
            else:
                self.gateway.tx_audio_level = int(self.gateway.tx_audio_level * 0.7 + current_level * 0.3)

            # Full-duplex: when the gateway is transmitting (PTT active), bypass
            # the VAD gate so radio RX still flows to Mumble via the normal path.
            if self.gateway.ptt_active:
                should_transmit = True

            # During the PTT click-suppression window, force the muted/faded audio
            # through to Mumble even if VAD says no.  Without this, Mumble skips the
            # ~130ms of silence while the speaker plays it, causing a permanent
            # speaker/Mumble sync offset after every PTT event.
            if time.monotonic() - self.gateway._ptt_change_time < 0.130:
                should_transmit = True

            if should_transmit:
                return data, False  # Don't trigger PTT (radio RX)
            else:
                return None, False
                
        except Exception as e:
            # Log the error so we can see what's wrong
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[RadioSource] Error reading audio: {type(e).__name__}: {e}")
            return None, False
    
    def is_active(self):
        """Radio is active if VAD is detecting signal"""
        return self.gateway.vad_active


class FilePlaybackSource(AudioSource):
    """Audio file playback source"""
    def __init__(self, config, gateway):
        super().__init__("FilePlayback", config)
        self.gateway = gateway
        self.priority = 0  # HIGHEST priority - announcements interrupt radio
        self.ptt_control = True  # File playback triggers PTT
        self.volume = getattr(config, 'PLAYBACK_VOLUME', 4.0)
        
        # Playback state
        self.current_file = None
        self.file_data = None
        self.file_position = 0
        self.playlist = []  # Queue of files to play
        self._play_seq = 0  # Sequence counter — each button press gets a unique ID
        import threading as _th
        self._play_lock = _th.Lock()  # Serializes stop+decode+queue
        
        # Periodic announcement - auto-detect station_id file
        self.last_announcement_time = 0
        self.announcement_interval = config.PLAYBACK_ANNOUNCEMENT_INTERVAL if hasattr(config, 'PLAYBACK_ANNOUNCEMENT_INTERVAL') else 0
        self.announcement_directory = config.PLAYBACK_DIRECTORY if hasattr(config, 'PLAYBACK_DIRECTORY') else './audio/'
        
        # File status tracking for status line indicators (0-9 = 10 files)
        self.file_status = {
            '0': {'exists': False, 'playing': False, 'path': None},  # station_id
            '1': {'exists': False, 'playing': False, 'path': None},
            '2': {'exists': False, 'playing': False, 'path': None},
            '3': {'exists': False, 'playing': False, 'path': None},
            '4': {'exists': False, 'playing': False, 'path': None},
            '5': {'exists': False, 'playing': False, 'path': None},
            '6': {'exists': False, 'playing': False, 'path': None},
            '7': {'exists': False, 'playing': False, 'path': None},
            '8': {'exists': False, 'playing': False, 'path': None},
            '9': {'exists': False, 'playing': False, 'path': None}
        }
        self.check_file_availability()
    
    def check_file_availability(self):
        """Scan audio directory and intelligently load files"""
        import os
        import glob
        
        if not os.path.exists(self.announcement_directory):
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Audio directory not found: {self.announcement_directory}")
            return
        
        # Storage for found files
        file_map = {}  # key -> (filepath, filename)
        
        # Step 1: Look for station_id (key 0)
        # Priority: station_id.mp3 > station_id.wav > station_id.*
        station_id_found = False
        for ext in ['.mp3', '.wav', '.ogg', '.flac', '.m4a']:
            path = os.path.join(self.announcement_directory, f'station_id{ext}')
            if os.path.exists(path):
                file_map['0'] = (path, os.path.basename(path))
                station_id_found = True
                break
        
        # Step 2: Look for numbered files (1_ through 9_)
        # Example: 1_welcome.mp3, 2_emergency.wav, etc.
        all_files = []
        for ext in ['*.mp3', '*.wav', '*.ogg', '*.flac', '*.m4a']:
            all_files.extend(glob.glob(os.path.join(self.announcement_directory, ext)))
        
        # Sort files alphabetically for consistent loading
        all_files.sort()
        
        # First pass: Look for files with number prefixes (1_ through 9_)
        for filepath in all_files:
            filename = os.path.basename(filepath)
            
            # Skip station_id files
            if filename.startswith('station_id'):
                continue
            
            # Check for number prefix (1_ through 9_)
            if len(filename) >= 2 and filename[0].isdigit() and filename[1] == '_':
                key = filename[0]
                if key in '123456789' and key not in file_map:
                    file_map[key] = (filepath, filename)
        
        # Second pass: If slots still empty, fill with any remaining files
        unassigned_files = [f for f in all_files 
                           if os.path.basename(f) not in [v[1] for v in file_map.values()]
                           and not os.path.basename(f).startswith('station_id')]
        
        # Fill empty slots in order (1-9)
        for filepath in unassigned_files:
            # Find next empty slot
            assigned = False
            for slot in range(1, 10):
                key = str(slot)
                if key not in file_map:
                    file_map[key] = (filepath, os.path.basename(filepath))
                    assigned = True
                    break
            
            if not assigned:
                # All slots 1-9 are full
                break
        
        # Step 3: Fill empty slots with random online sound effects
        if getattr(self.config, 'ENABLE_SOUNDBOARD', True):
            self._fill_soundboard_slots(file_map)

        # Step 4: Update file_status with found files
        for key in '0123456789':
            if key in file_map:
                filepath, filename = file_map[key]
                self.file_status[key]['exists'] = True
                self.file_status[key]['path'] = filepath
                self.file_status[key]['filename'] = filename

        # Step 5: Print file mapping (will be displayed before status bar)
        self.file_mapping_display = self._generate_file_mapping_display(file_map, station_id_found)

    # Curated pool of 429 free sound effects from Mixkit (royalty-free, no attribution)
    # URL pattern: https://assets.mixkit.co/active_storage/sfx/{id}/{id}-preview.mp3
    # Categories: animals, applause, arcade, bells, boing, buzzer, cartoon, crowd,
    #             drums, explosion, funny, game, horns, impact, sirens, transition,
    #             whistles, whoosh
    SOUNDBOARD_POOL = [
        # Animals (50)
        ('animals', 1), ('animals', 6), ('animals', 7), ('animals', 13), ('animals', 17),
        ('animals', 20), ('animals', 23), ('animals', 45), ('animals', 51), ('animals', 54),
        ('animals', 59), ('animals', 60), ('animals', 61), ('animals', 76), ('animals', 83),
        ('animals', 85), ('animals', 87), ('animals', 91), ('animals', 92), ('animals', 93),
        ('animals', 96), ('animals', 105), ('animals', 108), ('animals', 309), ('animals', 1212),
        ('animals', 1744), ('animals', 1751), ('animals', 1770), ('animals', 1775), ('animals', 1776),
        ('animals', 1780), ('animals', 2458), ('animals', 2462), ('animals', 2466), ('animals', 2467),
        ('animals', 2485), ('animals', 2469), ('animals', 2471), ('animals', 2474), ('animals', 2476),
        ('animals', 2479), ('animals', 2481), ('animals', 2483), ('animals', 2486), ('animals', 2488),
        ('animals', 2490), ('animals', 2492), ('animals', 2494), ('animals', 2496), ('animals', 2498),
        # Applause (35)
        ('applause', 103), ('applause', 362), ('applause', 439), ('applause', 442), ('applause', 475),
        ('applause', 476), ('applause', 477), ('applause', 478), ('applause', 482), ('applause', 484),
        ('applause', 485), ('applause', 500), ('applause', 501), ('applause', 502), ('applause', 504),
        ('applause', 505), ('applause', 507), ('applause', 508), ('applause', 509), ('applause', 510),
        ('applause', 512), ('applause', 513), ('applause', 515), ('applause', 516), ('applause', 517),
        ('applause', 518), ('applause', 519), ('applause', 521), ('applause', 522), ('applause', 523),
        ('applause', 3035), ('applause', 3036), ('applause', 3039), ('applause', 480), ('applause', 486),
        # Arcade (45)
        ('arcade', 210), ('arcade', 211), ('arcade', 212), ('arcade', 213), ('arcade', 216),
        ('arcade', 217), ('arcade', 220), ('arcade', 221), ('arcade', 223), ('arcade', 234),
        ('arcade', 235), ('arcade', 236), ('arcade', 237), ('arcade', 240), ('arcade', 253),
        ('arcade', 254), ('arcade', 257), ('arcade', 272), ('arcade', 277), ('arcade', 278),
        ('arcade', 470), ('arcade', 767), ('arcade', 866), ('arcade', 1084), ('arcade', 1698),
        ('arcade', 1699), ('arcade', 1933), ('arcade', 1953), ('arcade', 2027), ('arcade', 2803),
        ('arcade', 2810), ('arcade', 2811), ('arcade', 2852), ('arcade', 2854), ('arcade', 2859),
        ('arcade', 2973), ('arcade', 214), ('arcade', 218), ('arcade', 219), ('arcade', 222),
        ('arcade', 224), ('arcade', 238), ('arcade', 239), ('arcade', 241), ('arcade', 271),
        # Bells (30)
        ('bells', 109), ('bells', 110), ('bells', 111), ('bells', 113), ('bells', 587),
        ('bells', 591), ('bells', 592), ('bells', 595), ('bells', 600), ('bells', 601),
        ('bells', 603), ('bells', 621), ('bells', 765), ('bells', 931), ('bells', 933),
        ('bells', 937), ('bells', 938), ('bells', 939), ('bells', 1046), ('bells', 1569),
        ('bells', 1743), ('bells', 1791), ('bells', 2256), ('bells', 3109), ('bells', 112),
        ('bells', 588), ('bells', 593), ('bells', 596), ('bells', 598), ('bells', 602),
        # Boing (10)
        ('boing', 2895), ('boing', 2896), ('boing', 2897), ('boing', 2898), ('boing', 2899),
        ('boing', 2893), ('boing', 2894), ('boing', 2892), ('boing', 2891), ('boing', 2890),
        # Buzzer (25)
        ('buzzer', 31), ('buzzer', 932), ('buzzer', 941), ('buzzer', 948), ('buzzer', 950),
        ('buzzer', 954), ('buzzer', 955), ('buzzer', 992), ('buzzer', 1647), ('buzzer', 2131),
        ('buzzer', 2132), ('buzzer', 2133), ('buzzer', 2591), ('buzzer', 2961), ('buzzer', 2962),
        ('buzzer', 2963), ('buzzer', 2964), ('buzzer', 2966), ('buzzer', 2967), ('buzzer', 2968),
        ('buzzer', 2969), ('buzzer', 3090), ('buzzer', 940), ('buzzer', 949), ('buzzer', 951),
        # Cartoon (20)
        ('cartoon', 107), ('cartoon', 741), ('cartoon', 2151), ('cartoon', 2195), ('cartoon', 2257),
        ('cartoon', 2363), ('cartoon', 742), ('cartoon', 743), ('cartoon', 745), ('cartoon', 747),
        ('cartoon', 2153), ('cartoon', 2193), ('cartoon', 2196), ('cartoon', 2258), ('cartoon', 2259),
        ('cartoon', 2360), ('cartoon', 2361), ('cartoon', 2362), ('cartoon', 2364), ('cartoon', 2365),
        # Cinematic (20)
        ('cinematic', 2838), ('cinematic', 2839), ('cinematic', 2840), ('cinematic', 2841),
        ('cinematic', 2842), ('cinematic', 2843), ('cinematic', 2844), ('cinematic', 2845),
        ('cinematic', 2846), ('cinematic', 2847), ('cinematic', 2848), ('cinematic', 2849),
        ('cinematic', 2850), ('cinematic', 2851), ('cinematic', 2853), ('cinematic', 2855),
        ('cinematic', 2856), ('cinematic', 2857), ('cinematic', 2858), ('cinematic', 2860),
        # Click (15)
        ('click', 546), ('click', 547), ('click', 548), ('click', 549), ('click', 550),
        ('click', 551), ('click', 552), ('click', 553), ('click', 554), ('click', 555),
        ('click', 556), ('click', 557), ('click', 2568), ('click', 2570), ('click', 2571),
        # Crowd (30)
        ('crowd', 360), ('crowd', 363), ('crowd', 368), ('crowd', 376), ('crowd', 377),
        ('crowd', 423), ('crowd', 424), ('crowd', 429), ('crowd', 432), ('crowd', 444),
        ('crowd', 448), ('crowd', 458), ('crowd', 459), ('crowd', 460), ('crowd', 461),
        ('crowd', 462), ('crowd', 469), ('crowd', 520), ('crowd', 531), ('crowd', 974),
        ('crowd', 1573), ('crowd', 1958), ('crowd', 2111), ('crowd', 3022), ('crowd', 364),
        ('crowd', 370), ('crowd', 378), ('crowd', 425), ('crowd', 433), ('crowd', 449),
        # Drums (30)
        ('drums', 487), ('drums', 488), ('drums', 492), ('drums', 546), ('drums', 558),
        ('drums', 559), ('drums', 560), ('drums', 562), ('drums', 563), ('drums', 565),
        ('drums', 566), ('drums', 567), ('drums', 570), ('drums', 573), ('drums', 576),
        ('drums', 577), ('drums', 2295), ('drums', 2299), ('drums', 2300), ('drums', 2426),
        ('drums', 2569), ('drums', 2909), ('drums', 489), ('drums', 490), ('drums', 491),
        ('drums', 564), ('drums', 568), ('drums', 571), ('drums', 574), ('drums', 575),
        # Explosion (40)
        ('explosion', 351), ('explosion', 782), ('explosion', 1278), ('explosion', 1300),
        ('explosion', 1338), ('explosion', 1343), ('explosion', 1562), ('explosion', 1616),
        ('explosion', 1687), ('explosion', 1689), ('explosion', 1690), ('explosion', 1693),
        ('explosion', 1694), ('explosion', 1696), ('explosion', 1700), ('explosion', 1702),
        ('explosion', 1703), ('explosion', 1704), ('explosion', 1705), ('explosion', 1722),
        ('explosion', 2599), ('explosion', 2758), ('explosion', 2759), ('explosion', 2772),
        ('explosion', 2773), ('explosion', 2777), ('explosion', 2780), ('explosion', 2782),
        ('explosion', 2800), ('explosion', 2801), ('explosion', 2804), ('explosion', 2806),
        ('explosion', 2809), ('explosion', 2994), ('explosion', 1688), ('explosion', 1691),
        ('explosion', 1695), ('explosion', 1697), ('explosion', 1701), ('explosion', 2760),
        # Funny (45)
        ('funny', 343), ('funny', 391), ('funny', 395), ('funny', 414), ('funny', 422),
        ('funny', 424), ('funny', 429), ('funny', 471), ('funny', 473), ('funny', 527),
        ('funny', 528), ('funny', 578), ('funny', 579), ('funny', 616), ('funny', 715),
        ('funny', 744), ('funny', 746), ('funny', 923), ('funny', 959), ('funny', 2194),
        ('funny', 2209), ('funny', 2358), ('funny', 2364), ('funny', 2813), ('funny', 2873),
        ('funny', 2880), ('funny', 2881), ('funny', 2882), ('funny', 2885), ('funny', 2886),
        ('funny', 2889), ('funny', 2890), ('funny', 2891), ('funny', 2894), ('funny', 2955),
        ('funny', 3050), ('funny', 392), ('funny', 393), ('funny', 396), ('funny', 415),
        ('funny', 472), ('funny', 474), ('funny', 577), ('funny', 580), ('funny', 581),
        # Game (35)
        ('game', 226), ('game', 231), ('game', 265), ('game', 266), ('game', 276),
        ('game', 689), ('game', 2042), ('game', 2043), ('game', 2045), ('game', 2047),
        ('game', 2058), ('game', 2059), ('game', 2061), ('game', 2062), ('game', 2063),
        ('game', 2065), ('game', 2066), ('game', 2067), ('game', 2069), ('game', 2073),
        ('game', 2324), ('game', 2361), ('game', 2821), ('game', 2837), ('game', 3154),
        ('game', 227), ('game', 228), ('game', 232), ('game', 233), ('game', 264),
        ('game', 267), ('game', 275), ('game', 2044), ('game', 2046), ('game', 2060),
        # Horns (30)
        ('horns', 529), ('horns', 530), ('horns', 713), ('horns', 714), ('horns', 716),
        ('horns', 717), ('horns', 718), ('horns', 719), ('horns', 720), ('horns', 722),
        ('horns', 724), ('horns', 727), ('horns', 973), ('horns', 1565), ('horns', 1632),
        ('horns', 1654), ('horns', 2289), ('horns', 2291), ('horns', 2785), ('horns', 3111),
        ('horns', 715), ('horns', 721), ('horns', 723), ('horns', 725), ('horns', 726),
        ('horns', 728), ('horns', 972), ('horns', 1566), ('horns', 1633), ('horns', 2290),
        # Impact (35)
        ('impact', 263), ('impact', 772), ('impact', 773), ('impact', 774), ('impact', 781),
        ('impact', 784), ('impact', 788), ('impact', 833), ('impact', 1143), ('impact', 2150),
        ('impact', 2152), ('impact', 2182), ('impact', 2198), ('impact', 2199), ('impact', 2589),
        ('impact', 2600), ('impact', 2655), ('impact', 2778), ('impact', 2779), ('impact', 2784),
        ('impact', 2900), ('impact', 2901), ('impact', 2902), ('impact', 2905), ('impact', 2937),
        ('impact', 3046), ('impact', 775), ('impact', 776), ('impact', 783), ('impact', 785),
        ('impact', 786), ('impact', 834), ('impact', 835), ('impact', 2153), ('impact', 2183),
        # Laser (15)
        ('laser', 1554), ('laser', 1555), ('laser', 1556), ('laser', 1557), ('laser', 1558),
        ('laser', 1559), ('laser', 1560), ('laser', 1561), ('laser', 2810), ('laser', 2811),
        ('laser', 2812), ('laser', 2814), ('laser', 2815), ('laser', 2816), ('laser', 2817),
        # Notifications (20)
        ('notifications', 2309), ('notifications', 2310), ('notifications', 2311),
        ('notifications', 2312), ('notifications', 2313), ('notifications', 2314),
        ('notifications', 2315), ('notifications', 2316), ('notifications', 2317),
        ('notifications', 2318), ('notifications', 2319), ('notifications', 2320),
        ('notifications', 2321), ('notifications', 2322), ('notifications', 2323),
        ('notifications', 2325), ('notifications', 2326), ('notifications', 2327),
        ('notifications', 2328), ('notifications', 2329),
        # Sirens (25)
        ('sirens', 445), ('sirens', 1008), ('sirens', 1640), ('sirens', 1641), ('sirens', 1642),
        ('sirens', 1643), ('sirens', 1644), ('sirens', 1645), ('sirens', 1646), ('sirens', 1649),
        ('sirens', 1650), ('sirens', 1651), ('sirens', 1652), ('sirens', 1653), ('sirens', 1655),
        ('sirens', 1656), ('sirens', 1657), ('sirens', 1929), ('sirens', 1648), ('sirens', 1654),
        ('sirens', 1658), ('sirens', 1659), ('sirens', 1930), ('sirens', 1931), ('sirens', 1932),
        # Swoosh (20)
        ('swoosh', 1461), ('swoosh', 1462), ('swoosh', 1463), ('swoosh', 1464), ('swoosh', 1466),
        ('swoosh', 1467), ('swoosh', 1468), ('swoosh', 1469), ('swoosh', 1470), ('swoosh', 1471),
        ('swoosh', 1472), ('swoosh', 1473), ('swoosh', 1475), ('swoosh', 1476), ('swoosh', 1477),
        ('swoosh', 1478), ('swoosh', 1479), ('swoosh', 1480), ('swoosh', 1481), ('swoosh', 1482),
        # Transition (35)
        ('transition', 166), ('transition', 175), ('transition', 1146), ('transition', 1287),
        ('transition', 1465), ('transition', 1474), ('transition', 2282), ('transition', 2290),
        ('transition', 2412), ('transition', 2608), ('transition', 2615), ('transition', 2630),
        ('transition', 2638), ('transition', 2639), ('transition', 2719), ('transition', 2907),
        ('transition', 2908), ('transition', 2919), ('transition', 3057), ('transition', 3089),
        ('transition', 3114), ('transition', 3115), ('transition', 3120), ('transition', 3121),
        ('transition', 3146), ('transition', 3161), ('transition', 167), ('transition', 168),
        ('transition', 176), ('transition', 177), ('transition', 2283), ('transition', 2609),
        ('transition', 2616), ('transition', 2631), ('transition', 2640),
        # Water (15)
        ('water', 523), ('water', 524), ('water', 525), ('water', 526), ('water', 2401),
        ('water', 2402), ('water', 2403), ('water', 2404), ('water', 2405), ('water', 2406),
        ('water', 2407), ('water', 2409), ('water', 2410), ('water', 2411), ('water', 2413),
        # Whistles (30)
        ('whistles', 406), ('whistles', 506), ('whistles', 605), ('whistles', 606), ('whistles', 607),
        ('whistles', 608), ('whistles', 609), ('whistles', 610), ('whistles', 611), ('whistles', 612),
        ('whistles', 613), ('whistles', 614), ('whistles', 615), ('whistles', 738), ('whistles', 1631),
        ('whistles', 2049), ('whistles', 2050), ('whistles', 2587), ('whistles', 2647), ('whistles', 2657),
        ('whistles', 3103), ('whistles', 3105), ('whistles', 604), ('whistles', 616), ('whistles', 617),
        ('whistles', 739), ('whistles', 740), ('whistles', 2051), ('whistles', 2588), ('whistles', 2648),
        # Whoosh (30)
        ('whoosh', 787), ('whoosh', 1485), ('whoosh', 1486), ('whoosh', 1489), ('whoosh', 1490),
        ('whoosh', 1491), ('whoosh', 1492), ('whoosh', 1493), ('whoosh', 1714), ('whoosh', 1721),
        ('whoosh', 2350), ('whoosh', 2408), ('whoosh', 2596), ('whoosh', 2623), ('whoosh', 2650),
        ('whoosh', 2651), ('whoosh', 2903), ('whoosh', 2918), ('whoosh', 3005), ('whoosh', 3024),
        ('whoosh', 1487), ('whoosh', 1488), ('whoosh', 1494), ('whoosh', 1715), ('whoosh', 1716),
        ('whoosh', 1717), ('whoosh', 1718), ('whoosh', 1719), ('whoosh', 1720), ('whoosh', 2351),
        # Fart (8)
        ('fart', 3041), ('fart', 3043), ('fart', 3051), ('fart', 3052),
        ('fart', 3053), ('fart', 3054), ('fart', 3055), ('fart', 3056),
        # Laugh (19)
        ('laugh', 409), ('laugh', 410), ('laugh', 411), ('laugh', 416), ('laugh', 417),
        ('laugh', 418), ('laugh', 420), ('laugh', 421), ('laugh', 426), ('laugh', 427),
        ('laugh', 428), ('laugh', 431), ('laugh', 2254), ('laugh', 2261), ('laugh', 2262),
        ('laugh', 2263), ('laugh', 2264), ('laugh', 2265), ('laugh', 2993),
        # Scream (7)
        ('scream', 349), ('scream', 440), ('scream', 1010), ('scream', 1963),
        ('scream', 1966), ('scream', 1972), ('scream', 2097),
        # Monster (27)
        ('monster', 8), ('monster', 12), ('monster', 16), ('monster', 90), ('monster', 306),
        ('monster', 1737), ('monster', 1777), ('monster', 1956), ('monster', 1957),
        ('monster', 1960), ('monster', 1970), ('monster', 1973), ('monster', 1974),
        ('monster', 1975), ('monster', 1976), ('monster', 1977), ('monster', 1978),
        ('monster', 2207), ('monster', 2208), ('monster', 2231), ('monster', 2233),
        ('monster', 2234), ('monster', 2240), ('monster', 2241), ('monster', 3092),
        ('monster', 3127), ('monster', 3168),
        # Horror (14)
        ('horror', 561), ('horror', 634), ('horror', 894), ('horror', 963),
        ('horror', 1157), ('horror', 1162), ('horror', 1495), ('horror', 1583),
        ('horror', 1729), ('horror', 2482), ('horror', 2484), ('horror', 2563),
        ('horror', 2566), ('horror', 3058),
        # Squeak (11)
        ('squeak', 10), ('squeak', 1009), ('squeak', 1011), ('squeak', 1012),
        ('squeak', 1013), ('squeak', 1014), ('squeak', 1016), ('squeak', 1017),
        ('squeak', 1018), ('squeak', 1019), ('squeak', 1020),
        # Wrong (9)
        ('wrong', 946), ('wrong', 1540), ('wrong', 2876), ('wrong', 2939),
        ('wrong', 2941), ('wrong', 2947), ('wrong', 2960), ('wrong', 3159), ('wrong', 3219),
    ]

    def _fill_soundboard_slots(self, file_map):
        """Download random sound effects from Mixkit to fill empty playback slots."""
        import os, random, urllib.request

        empty_slots = [str(k) for k in range(1, 10) if str(k) not in file_map]
        if not empty_slots:
            return

        cache_dir = os.path.join(self.announcement_directory, '.cache')
        os.makedirs(cache_dir, exist_ok=True)

        # Pick random sounds from pool (without replacement)
        pool = list(self.SOUNDBOARD_POOL)
        random.shuffle(pool)
        picks = pool[:len(empty_slots)]

        for slot, (category, sfx_id) in zip(empty_slots, picks):
            filename = f"{category}_{sfx_id}.mp3"
            filepath = os.path.join(cache_dir, filename)

            # Download if not already cached
            if not os.path.exists(filepath):
                url = f"https://assets.mixkit.co/active_storage/sfx/{sfx_id}/{sfx_id}-preview.mp3"
                try:
                    urllib.request.urlretrieve(url, filepath)
                    print(f"  [Soundboard] Downloaded: {filename}")
                except Exception as e:
                    print(f"  [Soundboard] Failed to download {filename}: {e}")
                    continue

            if os.path.exists(filepath):
                file_map[slot] = (filepath, filename)
    
    def _generate_file_mapping_display(self, file_map, station_id_found):
        """Generate the file mapping display string"""
        lines = []
        lines.append("=" * 60)
        lines.append("FILE PLAYBACK MAPPING")
        lines.append("=" * 60)
        
        if not file_map:
            lines.append("No audio files found in: " + self.announcement_directory)
            lines.append("Supported formats: .mp3, .wav, .ogg, .flac, .m4a")
            lines.append("")
            lines.append("Naming conventions:")
            lines.append("  station_id.mp3 or station_id.wav  → Key [0]")
            lines.append("  1_filename.mp3                    → Key [1]")
            lines.append("  2_filename.wav                    → Key [2]")
            lines.append("  Or place any audio files and they'll auto-assign to keys 1-9")
            lines.append("=" * 60)
            return "\n".join(lines)
        
        # Show all keys 1-9 then 0 (matching status bar order)
        # Format: "Key [N]: filename.mp3" or "Key [N]: <none>"
        
        # Keys 1-9 - Announcements
        for key in '123456789':
            if key in file_map:
                lines.append(f"Key [{key}]: {file_map[key][1]}")
            else:
                lines.append(f"Key [{key}]: <none>")
        
        # Key 0 - Station ID (at end, matching status bar)
        if '0' in file_map:
            lines.append(f"Key [0]: {file_map['0'][1]}")
        else:
            lines.append(f"Key [0]: <none>")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def print_file_mapping(self):
        """Print the file mapping (call this just before status bar starts)"""
        if hasattr(self, 'file_mapping_display'):
            print(self.file_mapping_display)
    
    def get_file_status_string(self):
        """Get status indicator string for display"""
        # ANSI color codes
        WHITE = '\033[97m'
        GREEN = '\033[92m'
        RED = '\033[91m'
        RESET = '\033[0m'
        
        status_str = ""
        # Show all 10 slots: 1-9 then 0 (station_id at end) - no brackets to save space
        for key in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']:
            if self.file_status[key]['playing']:
                # Red when playing
                status_str += f"{RED}{key}{RESET}"
            elif self.file_status[key]['exists']:
                # Green when file exists
                status_str += f"{GREEN}{key}{RESET}"
            else:
                # White when no file
                status_str += f"{WHITE}{key}{RESET}"
        
        return status_str
        
    def queue_file(self, filepath):
        """Pre-decode an audio file and add it to the playback queue.
        Decoding happens here (caller's thread) so the audio transmit loop
        never blocks on file I/O."""
        self._pb_log_n = 0  # Reset playback log counter
        import os

        # Check if file exists
        full_path = filepath
        if not os.path.exists(filepath):
            # Try with announcement directory prefix
            alt_path = os.path.join(self.announcement_directory, filepath)
            if os.path.exists(alt_path):
                full_path = alt_path
            else:
                # File not found
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] File not found: {filepath}")
                    print(f"  Looked in: {os.path.abspath(filepath)}")
                    print(f"  Looked in: {os.path.abspath(alt_path)}")
                return False

        # Pre-decode the file now (runs in keyboard/callback thread, not audio thread)
        pcm_bytes = self._decode_file(full_path)
        if pcm_bytes is None:
            return False

        self.playlist.append((full_path, pcm_bytes))
        if self.gateway.config.VERBOSE_LOGGING:
            print(f"\n[Playback] ✓ Queued: {os.path.basename(full_path)} ({len(self.playlist)} in queue)")
        return True

    def load_next_file(self):
        """Activate the next pre-decoded file from the queue (no I/O)."""
        if not self.playlist:
            return False

        filepath, pcm_bytes = self.playlist.pop(0)
        self.file_data = pcm_bytes
        self.file_position = 0
        self.current_file = filepath

        # Mark file as playing in status display
        for key, info in self.file_status.items():
            if info['path'] == filepath:
                self.file_status[key]['playing'] = True
                break

        return True
    
    def stop_playback(self):
        """Stop current playback and clear queue"""
        # Mark current file as not playing
        if self.current_file:
            # Find which key this file belongs to
            for key, info in self.file_status.items():
                if info['path'] == self.current_file:
                    self.file_status[key]['playing'] = False
                    break

        # Clear current playback
        self.current_file = None
        self.file_data = None
        self.file_position = 0

        # Clear queue
        self.playlist.clear()

        # Release PTT immediately (don't wait for timeout)
        gw = self.gateway
        if gw.ptt_active and not gw.manual_ptt_mode and not gw._rebroadcast_ptt_active:
            gw.ptt_active = False
            gw._pending_ptt_state = False

        # Restore RTS state
        self._restore_playback_rts()

        if self.gateway.config.VERBOSE_LOGGING:
            print("\n[Playback] ✓ Stopped playback and cleared queue")
    
    def _decode_file(self, filepath):
        """Decode an audio file to PCM bytes.  Returns bytes on success, None on failure.
        Called from queue_file() in the caller's thread so the audio loop never blocks."""
        try:
            import os

            # Get file extension
            file_ext = os.path.splitext(filepath)[1].lower()

            # Try soundfile first (best option for Python 3.13)
            try:
                import soundfile as sf
                import numpy as np

                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] Decoding {os.path.basename(filepath)} (using soundfile)...")

                # Read audio file - soundfile handles MP3 via libsndfile + ffmpeg
                audio_data, sample_rate = sf.read(filepath, dtype='int16')

                # Get file info
                channels = 1 if len(audio_data.shape) == 1 else audio_data.shape[1]
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  Format: {sample_rate}Hz, {channels}ch, 16-bit")

                # Convert stereo to mono if needed
                if channels == 2:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Converting stereo to mono...")
                    audio_data = audio_data.mean(axis=1).astype('int16')
                elif channels > 2:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Converting {channels} channels to mono...")
                    audio_data = audio_data.mean(axis=1).astype('int16')

                # Resample if needed
                if sample_rate != self.config.AUDIO_RATE:
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Resampling: {sample_rate}Hz → {self.config.AUDIO_RATE}Hz")
                    try:
                        import resampy
                        # resampy works with float data
                        audio_float = audio_data.astype('float32') / 32768.0
                        audio_resampled = resampy.resample(audio_float, sample_rate, self.config.AUDIO_RATE)
                        audio_data = (audio_resampled * 32768.0).astype('int16')
                    except ImportError:
                        # Fallback: simple linear interpolation
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"    (using basic resampling - install resampy for better quality)")
                        ratio = self.config.AUDIO_RATE / sample_rate
                        new_length = int(len(audio_data) * ratio)
                        indices = (np.arange(new_length) / ratio).astype(int)
                        audio_data = audio_data[indices]

                duration_sec = len(audio_data) / self.config.AUDIO_RATE
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  ✓ Decoded {duration_sec:.1f}s of audio")

                return audio_data.tobytes()

            except ImportError:
                # soundfile not available, try wave module (WAV only)
                if file_ext != '.wav':
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"\n[Playback] Error: {file_ext.upper()} not supported without soundfile")
                        print(f"  Install soundfile for multi-format support:")
                        print(f"    pip install soundfile resampy --break-system-packages")
                        print(f"  Also install system library:")
                        print(f"    sudo apt-get install libsndfile1")
                        print(f"\n  Or convert to WAV:")
                        print(f"    ffmpeg -i {os.path.basename(filepath)} -ar 48000 -ac 1 output.wav")
                    return None

                # Fall back to wave module for WAV files
                import wave

                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"\n[Playback] Decoding {os.path.basename(filepath)} (WAV only)...")

                with wave.open(filepath, 'rb') as wf:
                    # Get file info
                    channels = wf.getnchannels()
                    rate = wf.getframerate()
                    width = wf.getsampwidth()
                    frames = wf.getnframes()

                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Format: {rate}Hz, {channels}ch, {width*8}-bit")

                    # Check format compatibility
                    needs_conversion = False

                    if channels != self.config.AUDIO_CHANNELS:
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {channels} channel(s), expected {self.config.AUDIO_CHANNELS}")
                            print(f"    File may not play correctly")
                        needs_conversion = True

                    if rate != self.config.AUDIO_RATE:
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {rate}Hz, expected {self.config.AUDIO_RATE}Hz")
                            print(f"    Audio will play at wrong speed!")
                        needs_conversion = True

                    if width != 2:  # 16-bit = 2 bytes
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  ⚠ Warning: {width*8}-bit, expected 16-bit")
                        needs_conversion = True

                    if needs_conversion and self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Convert with: ffmpeg -i {os.path.basename(filepath)} -ar 48000 -ac 1 -sample_fmt s16 output.wav")
                        print(f"  Or install soundfile for automatic conversion")

                    pcm_bytes = wf.readframes(frames)
                    duration_sec = frames / rate
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  ✓ Decoded {duration_sec:.1f}s of audio")

                    return pcm_bytes

        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Error decoding {filepath}: {e}")
            return None
    
    def check_periodic_announcement(self):
        """Check if it's time for a periodic announcement"""
        # Use auto-detected station_id file (key 0)
        if self.announcement_interval <= 0 or not self.file_status['0']['exists']:
            return
        
        current_time = time.time()
        if self.last_announcement_time == 0:
            self.last_announcement_time = current_time
            return
        
        # Check if enough time has passed
        elapsed = current_time - self.last_announcement_time
        if elapsed >= self.announcement_interval:
            # Check if radio is idle
            if not self.gateway.vad_active:
                # Queue the station_id file
                station_id_path = self.file_status['0']['path']
                if station_id_path:
                    self.queue_file(station_id_path)
                    self.last_announcement_time = current_time
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"\n[Playback] Periodic station ID triggered (every {self.announcement_interval}s)")
    
    def get_audio(self, chunk_size):
        """Get audio chunk from file playback"""
        import os
        
        # Check for periodic announcements
        self.check_periodic_announcement()
        
        # If no file is playing, try to load next from queue
        if not self.current_file and self.playlist:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[FilePlayback] Loading file from queue (queue length: {len(self.playlist)})")
            if not self.load_next_file():
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"[FilePlayback] Failed to load file from queue")
                return None, False
            else:
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"[FilePlayback] Successfully loaded: {os.path.basename(self.current_file)}")
        
        # No file playing
        if not self.file_data:
            return None, False

        # Calculate chunk size in bytes (16-bit = 2 bytes per sample)
        chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2

        # During the PTT announcement delay the radio is keying up.  Return silence
        # without advancing the file position so no audio is lost.
        # Skip delay for D75/KV4P — no relay settling needed.
        _tx_radio = str(getattr(self.gateway.config, 'TX_RADIO', 'th9800')).lower()
        _skip_delay = _tx_radio in ('d75', 'kv4p')
        if getattr(self.gateway, 'announcement_delay_active', False) and not _skip_delay:
            return b'\x00' * chunk_bytes, True

        # Instrument: log playback state on first few calls per file
        if not hasattr(self, '_pb_log_n'):
            self._pb_log_n = 0
        self._pb_log_n += 1
        if self._pb_log_n <= 5 or self._pb_log_n % 50 == 0:
            _peak = 0
            _remaining = len(self.file_data) - self.file_position
            if _remaining > 0:
                _arr = np.frombuffer(self.file_data[self.file_position:min(self.file_position + chunk_bytes, len(self.file_data))], dtype=np.int16)
                _peak = int(np.max(np.abs(_arr))) if len(_arr) > 0 else 0
            print(f"  [Playback] get_audio #{self._pb_log_n}: pos={self.file_position}/{len(self.file_data)} remaining={_remaining}B peak={_peak} delay={getattr(self.gateway, 'announcement_delay_active', False)}")

        # Check if we have enough data left
        if self.file_position >= len(self.file_data):
            # File finished
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Playback] Finished: {os.path.basename(self.current_file) if self.current_file else 'unknown'}")
            
            # Reset volume to configured level (in case TTS boosted it)
            self.volume = getattr(self.gateway.config, 'PLAYBACK_VOLUME', 4.0)
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"[Playback] Volume reset to {self.volume}x")
            
            # Mark file as not playing by matching path
            if self.current_file:
                for key, info in self.file_status.items():
                    if info['path'] == self.current_file:
                        self.file_status[key]['playing'] = False
                        break
            
            self.current_file = None
            self.file_data = None
            self.file_position = 0
            
            # Try to load next file
            if self.playlist:
                if not self.load_next_file():
                    self._restore_playback_rts()
                    return None, False
                # Continue with the new file
            else:
                self._restore_playback_rts()
                return None, False
        
        # Get chunk from file
        end_pos = min(self.file_position + chunk_bytes, len(self.file_data))
        chunk = self.file_data[self.file_position:end_pos]
        self.file_position = end_pos
        
        # Pad with silence if chunk is too short
        if len(chunk) < chunk_bytes:
            chunk += b'\x00' * (chunk_bytes - len(chunk))
        
        # Apply volume
        if self.volume != 1.0:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            chunk = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

        # Small yield to prevent file playback from overwhelming other threads
        # (especially important now that we removed priority scheduling)
        import time
        time.sleep(0.001)  # 1ms - negligible latency but helps system balance
        
        # File playback triggers PTT - ALWAYS
        return chunk, True
    
    def _restore_playback_rts(self):
        """Restore RTS to saved state after playback finishes (runs in background thread).
        Only applies to AIOC PTT mode — software PTT uses !ptt directly.
        RTS must be Radio Controlled during AIOC PTT (relay routes mic wiring
        through front panel). Restored to USB Controlled after so CAT resumes."""
        _ptt_method = str(getattr(self.gateway.config, 'PTT_METHOD', 'aioc')).lower()
        _tx_radio = str(getattr(self.gateway.config, 'TX_RADIO', 'th9800')).lower()
        if _ptt_method == 'software' or _tx_radio in ('d75', 'kv4p'):
            return
        _saved = getattr(self.gateway, '_playback_rts_saved', None)
        if _saved is not None:
            self.gateway._playback_rts_saved = None
            _cat = getattr(self.gateway, 'cat_client', None)
            if _cat and (_saved is True or _saved is None):
                def _do_restore():
                    try:
                        _cat.set_rts(True if _saved else False)
                        print(f"\n[Playback] RTS restored to {'USB' if _saved else 'Radio'} Controlled")
                        # Refresh display after RTS change to prevent VFO display corruption
                        time.sleep(0.3)
                        _cat._pause_drain()
                        try:
                            _cat._send_button([0x00, 0x25], 3, 5)  # Left dial press
                            time.sleep(0.15)
                            _cat._send_button_release()
                            time.sleep(0.3)
                            _cat._drain(0.5)
                            _cat._send_button([0x00, 0xA5], 3, 5)  # Right dial press
                            time.sleep(0.15)
                            _cat._send_button_release()
                            time.sleep(0.3)
                            _cat._drain(0.5)
                        finally:
                            _cat._drain_paused = False
                    except Exception:
                        pass
                import threading
                threading.Thread(target=_do_restore, daemon=True, name="RTS-Restore").start()

    def is_active(self):
        """Playback is active if file is currently playing"""
        return self.current_file is not None
    
    def get_status(self):
        """Return status string for display"""
        if self.current_file:
            import os
            filename = os.path.basename(self.current_file)
            progress = (self.file_position / len(self.file_data)) * 100 if self.file_data else 0
            return f"{self.name}: Playing {filename} ({progress:.0f}%)"
        elif self.playlist:
            return f"{self.name}: {len(self.playlist)} queued"
        else:
            return f"{self.name}: Idle"


class EchoLinkSource(AudioSource):
    """EchoLink audio input via TheLinkBox IPC"""
    def __init__(self, config, gateway):
        super().__init__("EchoLink", config)
        self.gateway = gateway
        self.priority = 2  # After Radio (1), before Files (0)
        self.ptt_control = False  # EchoLink doesn't trigger radio PTT
        self.volume = 1.0
        
        # IPC state
        self.rx_pipe = None
        self.tx_pipe = None
        self.connected = False
        self.last_audio_time = 0
        
        # Try to setup IPC
        if config.ENABLE_ECHOLINK:
            self.setup_ipc()
    
    def setup_ipc(self):
        """Setup named pipes for TheLinkBox IPC"""
        import os
        import errno
        
        try:
            rx_path = self.config.ECHOLINK_RX_PIPE
            tx_path = self.config.ECHOLINK_TX_PIPE
            
            # Create named pipes if they don't exist
            for pipe_path in [rx_path, tx_path]:
                if not os.path.exists(pipe_path):
                    try:
                        os.mkfifo(pipe_path)
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"  Created FIFO: {pipe_path}")
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
            
            # Open pipes (non-blocking mode)
            import fcntl
            
            # RX pipe (read from TheLinkBox)
            self.rx_pipe = open(rx_path, 'rb', buffering=0)
            flags = fcntl.fcntl(self.rx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.rx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            # TX pipe (write to TheLinkBox)
            self.tx_pipe = open(tx_path, 'wb', buffering=0)
            flags = fcntl.fcntl(self.tx_pipe, fcntl.F_GETFL)
            fcntl.fcntl(self.tx_pipe, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            self.connected = True
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"  ✓ EchoLink IPC connected via named pipes")
                print(f"    RX: {rx_path}")
                print(f"    TX: {tx_path}")
            
        except Exception as e:
            print(f"  ⚠ EchoLink IPC setup failed: {e}")
            print(f"    Make sure TheLinkBox is running and configured")
            self.connected = False
    
    def get_audio(self, chunk_size):
        """Get audio from EchoLink via named pipe"""
        if not self.connected or not self.rx_pipe:
            return None, False
        
        try:
            chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2  # 16-bit
            data = self.rx_pipe.read(chunk_bytes)
            
            if data and len(data) == chunk_bytes:
                self.last_audio_time = time.time()
                
                # Apply volume
                if self.volume != 1.0:
                    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    data = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

                return data, False  # No PTT control
            else:
                return None, False
                
        except BlockingIOError:
            # No data available (non-blocking read)
            return None, False
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Read error: {e}")
            return None, False
    
    def send_audio(self, audio_data):
        """Send audio to EchoLink via named pipe"""
        if not self.connected or not self.tx_pipe:
            return
        
        try:
            self.tx_pipe.write(audio_data)
            self.tx_pipe.flush()
        except BlockingIOError:
            # Pipe full, skip this chunk
            pass
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[EchoLink] Write error: {e}")
    
    def is_active(self):
        """EchoLink is active if we've received audio recently"""
        if not self.connected:
            return False
        return (time.time() - self.last_audio_time) < 2.0
    
    def cleanup(self):
        """Close IPC connections"""
        if self.rx_pipe:
            try:
                self.rx_pipe.close()
            except Exception:
                pass
        if self.tx_pipe:
            try:
                self.tx_pipe.close()
            except Exception:
                pass


class SDRSource(AudioSource):
    """SDR receiver audio input via ALSA loopback"""
    def __init__(self, config, gateway, name="SDR1", sdr_priority=1):
        super().__init__(name, config)
        self.gateway = gateway
        self.priority = 2  # Audio mixer priority (lower than radio/files)
        self.sdr_priority = sdr_priority  # Priority for SDR-to-SDR ducking (1=higher, 2=lower)
        self.ptt_control = False  # SDR doesn't trigger PTT
        self.volume = 1.0
        self.mix_ratio = 1.0  # Volume applied when ducking is disabled
        self.duck = True      # When True: silence SDR if higher priority source is active
        self.enabled = True   # Start enabled by default
        self.muted = False    # Can be muted independently
        
        # Audio stream
        self.input_stream = None
        self.pyaudio = None
        self.audio_level = 0
        self.last_read_time = 0
        
        # Dropout tracking
        self.dropout_count = 0
        self.overflow_count = 0
        self.total_reads = 0
        self.last_stats_time = time.time()

        # Loopback watchdog — detects stalled ALSA reads and attempts recovery
        self._last_successful_read = time.monotonic()
        self._watchdog_restarts = 0
        self._watchdog_stage = 0      # 0=healthy, 1=reopen, 2=reinit pyaudio, 3=reload module
        self._recovering = False
        self._watchdog_gave_up = False

        # PortAudio callback mode — same proven pattern as AIOCRadioSource.
        # The callback fires at each ALSA period (~200ms), queues the blob.
        # get_audio() drains blobs into a sub-buffer and slices into 50ms
        # consumer chunks.  3-blob prebuffer (600ms) absorbs delivery jitter.
        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(self, 'sdr_channels', 1) * 2
        self._blob_bytes = 0       # set in setup_audio() once channels/multiplier known
        self._prebuffering = True   # gate: wait for 3 blobs before first serve
        self._last_blocked_ms = 0.0  # instrumentation: how long get_audio blocked on blob fetch
        self._blob_times = collections.deque(maxlen=64)  # instrumentation: reader blob timestamps
        self._plc_total = 0        # instrumentation: kept for trace compatibility

        # Enhanced trace instrumentation
        self._cb_overflow_count = 0   # PortAudio callback reported input overflow
        self._cb_underflow_count = 0  # PortAudio callback reported input underflow
        self._cb_drop_count = 0       # blobs dropped because queue was full
        self._last_cb_status = 0      # last callback status flags
        self._last_serve_sample = 0   # last sample value served (for discontinuity detection)
        self._serve_discontinuity = 0.0  # abs delta between last sample of prev chunk and first of current
        self._sub_buffer_after = 0    # sub-buffer bytes after serving chunk

        if self.config.VERBOSE_LOGGING:
            print(f"[{self.name}] Initializing SDR audio source...")
    
    def setup_audio(self):
        """Initialize SDR audio input from ALSA loopback"""
        try:
            import pyaudio
            import os as _os
            if not self.config.VERBOSE_LOGGING:
                _saved = _os.dup(2)
                try:
                    _dn = _os.open(_os.devnull, _os.O_WRONLY)
                    _os.dup2(_dn, 2); _os.close(_dn)
                    self.pyaudio = pyaudio.PyAudio()
                finally:
                    _os.dup2(_saved, 2); _os.close(_saved)
            else:
                self.pyaudio = pyaudio.PyAudio()

            if self.config.VERBOSE_LOGGING:
                config_device_attr = 'SDR2_DEVICE_NAME' if self.name == "SDR2" else 'SDR_DEVICE_NAME'
                target_name = getattr(self.config, config_device_attr, '')
                print(f"[{self.name}] Searching for device matching: {target_name}")
                print(f"[{self.name}] Available input devices:")
                for i in range(self.pyaudio.get_device_count()):
                    info = self.pyaudio.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        print(f"[{self.name}]   [{i}] {info['name']} (in:{info['maxInputChannels']})")

            device_index, device_name = self._find_device()

            if device_index is None:
                config_device_attr = 'SDR2_DEVICE_NAME' if self.name == "SDR2" else 'SDR_DEVICE_NAME'
                print(f"[{self.name}] ✗ SDR device not found")
                target = getattr(self.config, config_device_attr, '')
                if target:
                    print(f"[{self.name}]   Looked for: {target}")
                    print(f"[{self.name}]   Try one of these formats:")
                    print(f"[{self.name}]     {config_device_attr} = Loopback")
                    print(f"[{self.name}]     {config_device_attr} = hw:2,0")
                    print(f"[{self.name}]   Or enable VERBOSE_LOGGING to see all devices")
                return False

            if not self._open_stream(device_index):
                raise Exception(f"Failed to open audio stream on device {device_index}")

            # Start the stream explicitly (callback mode)
            if not self.input_stream.is_active():
                self.input_stream.start_stream()

            if self.config.VERBOSE_LOGGING:
                config_buffer_attr = 'SDR2_BUFFER_MULTIPLIER' if self.name == "SDR2" else 'SDR_BUFFER_MULTIPLIER'
                buffer_multiplier = getattr(self.config, config_buffer_attr, 4)
                buffer_size = self.config.AUDIO_CHUNK_SIZE * buffer_multiplier
                print(f"[{self.name}] ✓ Audio input configured: {device_name}")
                print(f"[{self.name}]   Channels: {self.sdr_channels} ({'stereo' if self.sdr_channels == 2 else 'mono'})")
                period_ms = buffer_size / self.config.AUDIO_RATE * 1000
                print(f"[{self.name}]   Callback mode: {buffer_size} frames ({period_ms:.0f}ms per period)")

            # Flush any stale data
            while not self._chunk_queue.empty():
                try:
                    self._chunk_queue.get_nowait()
                except _queue_mod.Empty:
                    break

            # Wait for initial blobs to arrive via callback (3 blobs = 600ms)
            prefill_deadline = time.monotonic() + 2.0
            while self._chunk_queue.qsize() < 3 and time.monotonic() < prefill_deadline:
                time.sleep(0.01)

            if self.config.VERBOSE_LOGGING:
                print(f"[{self.name}] ✓ Callback stream active (queue: {self._chunk_queue.qsize()} blobs)")

            return True
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"[{self.name}] ✗ Failed to setup audio: {e}")
            return False
    
    def _sdr_callback(self, in_data, frame_count, time_info, status):
        """PortAudio input callback — fires at each ALSA period.

        Identical pattern to AIOCRadioSource._audio_callback.
        Keep minimal — runs in PortAudio's audio thread."""
        if status:
            self._last_cb_status = status
            if status & 0x2:  # paInputOverflow
                self._cb_overflow_count += 1
            if status & 0x1:  # paInputUnderflow
                self._cb_underflow_count += 1
        if in_data:
            _now = time.monotonic()
            self._last_successful_read = _now
            self._blob_times.append(_now)
            try:
                self._chunk_queue.put_nowait(in_data)
            except _queue_mod.Full:
                self._cb_drop_count += 1
        return (None, pyaudio.paContinue)

    def get_audio(self, chunk_size):
        """Get processed audio from SDR receiver.

        Same proven pattern as AIOCRadioSource:
        1. Eagerly drain all blobs from reader queue into sub-buffer
        2. Cap sub-buffer to prevent latency buildup
        3. Pre-buffer gate: wait for 3 blobs (600ms) before first serve
        4. Serve one 50ms chunk; if depleted, re-enter prebuffer
        """
        if not self.enabled:
            return None, False

        if not self.input_stream:
            return None, False

        cb = self._chunk_bytes

        # Eagerly drain all blobs from queue into sub-buffer, smoothing
        # the junction to eliminate sample discontinuity clicks.
        # IMPORTANT: No data is removed — both sub-buffer tail and blob
        # head are modified in-place so the buffer doesn't shrink over time.
        _t0 = time.monotonic()
        _fetched = False
        _SMOOTH = 16  # samples to taper on each side of junction (~0.33ms)
        _SMOOTH_BYTES = _SMOOTH * 2  # int16 = 2 bytes per sample
        while True:
            try:
                blob = self._chunk_queue.get_nowait()
                # Smooth junction: taper last N samples of sub-buffer and
                # first N samples of new blob toward their shared midpoint.
                # This eliminates clicks without removing any data.
                if self._sub_buffer and len(blob) >= _SMOOTH_BYTES and len(self._sub_buffer) >= _SMOOTH_BYTES:
                    # Get the boundary samples
                    last_sample = int.from_bytes(self._sub_buffer[-2:], 'little', signed=True)
                    first_sample = int.from_bytes(blob[0:2], 'little', signed=True)
                    jump = abs(last_sample - first_sample)
                    # Only smooth if there's a significant discontinuity
                    if jump > 500:
                        mid = (last_sample + first_sample) / 2.0
                        # Taper tail of sub-buffer toward midpoint
                        tail_arr = np.frombuffer(self._sub_buffer[-_SMOOTH_BYTES:], dtype=np.int16).astype(np.float32)
                        w = np.linspace(0.0, 1.0, len(tail_arr), dtype=np.float32)
                        tail_arr = tail_arr * (1.0 - w) + mid * w
                        self._sub_buffer = self._sub_buffer[:-_SMOOTH_BYTES] + np.clip(tail_arr, -32768, 32767).astype(np.int16).tobytes()
                        # Taper head of blob from midpoint
                        head_arr = np.frombuffer(blob[:_SMOOTH_BYTES], dtype=np.int16).astype(np.float32)
                        w = np.linspace(0.0, 1.0, len(head_arr), dtype=np.float32)
                        head_arr = mid * (1.0 - w) + head_arr * w
                        blob = np.clip(head_arr, -32768, 32767).astype(np.int16).tobytes() + blob[_SMOOTH_BYTES:]
                self._sub_buffer += blob
                _fetched = True
            except _queue_mod.Empty:
                break
        self._last_blocked_ms = (time.monotonic() - _t0) * 1000 if _fetched else 0.0

        # Cap sub-buffer to prevent stale audio buildup under CPU load.
        if self._blob_bytes > 0 and len(self._sub_buffer) > self._blob_bytes * 5:
            self._sub_buffer = self._sub_buffer[-(self._blob_bytes * 5):]

        # Pre-buffer gate: after depletion, accumulate 1 full blob worth
        # of data before serving.  This provides ~200ms cushion (4 consumer
        # chunks) which absorbs normal ALSA delivery jitter.  The crossfade
        # can leave a partial-blob residue, so using blob_bytes (not 2×) as
        # the threshold avoids over-waiting.
        if self._prebuffering:
            if self._blob_bytes > 0 and len(self._sub_buffer) < self._blob_bytes:
                return None, False  # still accumulating
            self._prebuffering = False

        if len(self._sub_buffer) < cb:
            self._prebuffering = True  # depleted — re-enter prebuffer
            return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]
        self._sub_buffer_after = len(self._sub_buffer)

        # Sample discontinuity detection: compare last sample of previous
        # chunk to first sample of this chunk.  Large jumps cause clicks.
        if len(raw) >= 2:
            first_sample = int.from_bytes(raw[0:2], byteorder='little', signed=True)
            delta = abs(first_sample - self._last_serve_sample)
            self._serve_discontinuity = float(delta)
            # Update last sample (last 2 bytes of raw, which is stereo or mono)
            self._last_serve_sample = int.from_bytes(raw[-2:], byteorder='little', signed=True)

        # Muted: chunk was sliced (keeps sub-buffer fresh), discard it.
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        self.total_reads += 1
        self.last_read_time = time.time()

        # Stereo→mono (all numpy processing happens here, not in reader thread)
        arr = np.frombuffer(raw, dtype=np.int16)
        if hasattr(self, 'sdr_channels') and self.sdr_channels == 2 and len(arr) >= 2:
            stereo = arr.reshape(-1, 2).astype(np.int32)
            arr = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(np.int16)
            raw = arr.tobytes()

        # Level metering and audio boost
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            _dg_key = 'SDR2_DISPLAY_GAIN' if self.name == 'SDR2' else 'SDR_DISPLAY_GAIN'
            display_gain = getattr(self.gateway.config, _dg_key, 1.0)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            _ab_key = 'SDR2_AUDIO_BOOST' if self.name == 'SDR2' else 'SDR_AUDIO_BOOST'
            audio_boost = getattr(self.gateway.config, _ab_key, 1.0)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        # Apply per-source audio processing (HPF, LPF, notch, gate, etc.)
        raw = self.gateway.process_audio_for_sdr(raw, self.name)

        return raw, False  # SDR never triggers PTT

    def is_active(self):
        """SDR is active if enabled and receiving audio"""
        return self.enabled and not self.muted and self.input_stream is not None
    
    def get_status(self):
        """Return status string"""
        if not self.enabled:
            return "SDR: Disabled"
        elif self.muted:
            return "SDR: Muted"
        else:
            return f"SDR: Active ({self.audio_level}%)"
    
    def cleanup(self):
        """Close SDR audio stream"""
        self._sub_buffer = b''

        if self.input_stream:
            try:
                # Stop stream first to prevent ALSA errors
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                time.sleep(0.05)  # Give ALSA time to clean up buffers
                self.input_stream.close()
            except Exception:
                pass  # Suppress ALSA errors during shutdown
        if self.pyaudio:
            try:
                self.pyaudio.terminate()
            except Exception:
                pass  # Suppress errors

    def _stop_reader(self):
        """Stop the callback stream and clear buffers."""
        # Callback stops automatically when stream is stopped/closed
        self._sub_buffer = b''
        self._prebuffering = True  # rebuild cushion on next start
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break

    def _close_stream(self):
        """Close the ALSA input stream safely."""
        if self.input_stream:
            try:
                if self.input_stream.is_active():
                    self.input_stream.stop_stream()
                time.sleep(0.05)
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None

    def _start_reader(self):
        """Start the callback stream and wait for initial blobs."""
        # Callback fires automatically once stream is active
        if self.input_stream and not self.input_stream.is_active():
            self.input_stream.start_stream()

        # Wait for 3 blobs (600ms) — matches get_audio() prebuffer gate
        prefill_deadline = time.monotonic() + 2.0
        while self._chunk_queue.qsize() < 3 and time.monotonic() < prefill_deadline:
            time.sleep(0.01)

    def _find_device(self):
        """Find the SDR ALSA device. Returns (device_index, device_name) or (None, None)."""
        if self.name == "SDR2":
            config_device_attr = 'SDR2_DEVICE_NAME'
        else:
            config_device_attr = 'SDR_DEVICE_NAME'

        target_name = getattr(self.config, config_device_attr, '')
        if not target_name:
            return None, None

        for i in range(self.pyaudio.get_device_count()):
            info = self.pyaudio.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                name_lower = info['name'].lower()
                # Extract hw device from target if format is hw:Name,X,Y
                if target_name.startswith('hw:') and ',' in target_name:
                    parts = target_name.split(',')
                    if len(parts) >= 2:
                        hw_device = f"hw:{parts[-2]},{parts[-1]}"
                        if hw_device in name_lower:
                            return i, info['name']
                if target_name.lower() in name_lower:
                    return i, info['name']

        return None, None

    def _open_stream(self, device_index):
        """Open ALSA input stream on given device. Returns True on success."""
        import pyaudio
        if self.name == "SDR2":
            config_buffer_attr = 'SDR2_BUFFER_MULTIPLIER'
        else:
            config_buffer_attr = 'SDR_BUFFER_MULTIPLIER'
        buffer_multiplier = getattr(self.config, config_buffer_attr, 4)
        buffer_size = self.config.AUDIO_CHUNK_SIZE * buffer_multiplier

        device_info = self.pyaudio.get_device_info_by_index(device_index)
        max_channels = device_info['maxInputChannels']
        sdr_channels = min(2, max_channels)

        try:
            self.input_stream = self.pyaudio.open(
                format=pyaudio.paInt16,
                channels=sdr_channels,
                rate=self.config.AUDIO_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=buffer_size,
                stream_callback=self._sdr_callback
            )
            self.sdr_channels = sdr_channels
            self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
            self._blob_bytes = self._chunk_bytes * buffer_multiplier
            return True
        except Exception:
            if sdr_channels == 2:
                try:
                    sdr_channels = 1
                    self.input_stream = self.pyaudio.open(
                        format=pyaudio.paInt16,
                        channels=sdr_channels,
                        rate=self.config.AUDIO_RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=buffer_size,
                        stream_callback=self._sdr_callback
                    )
                    self.sdr_channels = sdr_channels
                    self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * sdr_channels * 2
                    self._blob_bytes = self._chunk_bytes * buffer_multiplier
                    return True
                except Exception:
                    return False
            return False

    def _restart_stream(self, stage):
        """Attempt staged recovery of the ALSA loopback.

        Stage 1: Reopen stream (close + reopen ALSA device)
        Stage 2: Reinitialize PyAudio entirely
        Stage 3: Reload snd-aloop kernel module (requires SDR_WATCHDOG_MODPROBE=true)

        Returns True on success, False on failure.
        """
        import pyaudio as _pyaudio_mod

        if stage == 1:
            print(f"\n[{self.name}] Watchdog: stage 1 recovery — reopening ALSA stream")
            try:
                self._stop_reader()
                self._close_stream()
                time.sleep(0.2)  # ALSA settle
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 1 failed — device not found")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 1 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 1 success — stream reopened ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 1 failed — {e}")
                return False

        elif stage == 2:
            print(f"\n[{self.name}] Watchdog: stage 2 recovery — reinitializing PyAudio")
            try:
                self._stop_reader()
                self._close_stream()
                if self.pyaudio:
                    try:
                        self.pyaudio.terminate()
                    except Exception:
                        pass
                time.sleep(0.5)
                self.pyaudio = _pyaudio_mod.PyAudio()
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 2 failed — device not found")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 2 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 2 success — PyAudio reinitialized ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 2 failed — {e}")
                return False

        elif stage == 3:
            if self.name == "SDR2":
                modprobe_enabled = getattr(self.config, 'SDR2_WATCHDOG_MODPROBE', False)
            else:
                modprobe_enabled = getattr(self.config, 'SDR_WATCHDOG_MODPROBE', False)
            if not modprobe_enabled:
                return False

            print(f"\n[{self.name}] Watchdog: stage 3 recovery — reloading snd-aloop kernel module")
            try:
                import subprocess
                self._stop_reader()
                self._close_stream()
                if self.pyaudio:
                    try:
                        self.pyaudio.terminate()
                    except Exception:
                        pass
                    self.pyaudio = None

                result = subprocess.run(['sudo', 'modprobe', '-r', 'snd-aloop'],
                                        timeout=10, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"[{self.name}] Watchdog: modprobe -r failed: {result.stderr.strip()}")
                    # Continue anyway — module may not have been loaded
                time.sleep(1.0)

                result = subprocess.run(['sudo', 'modprobe', 'snd-aloop'],
                                        timeout=10, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"[{self.name}] Watchdog: modprobe load failed: {result.stderr.strip()}")
                    return False
                time.sleep(1.0)  # Wait for devices to re-appear

                self.pyaudio = _pyaudio_mod.PyAudio()
                dev_idx, dev_name = self._find_device()
                if dev_idx is None:
                    print(f"[{self.name}] Watchdog: stage 3 failed — device not found after reload")
                    return False
                if not self._open_stream(dev_idx):
                    print(f"[{self.name}] Watchdog: stage 3 failed — could not open stream")
                    return False
                self._start_reader()
                print(f"[{self.name}] Watchdog: stage 3 success — module reloaded ({dev_name})")
                return True
            except Exception as e:
                print(f"[{self.name}] Watchdog: stage 3 failed — {e}")
                return False

        return False

    def check_watchdog(self):
        """Check for stalled ALSA reads and attempt staged recovery.

        Called from status_monitor_loop (~once per second).
        """
        if self._recovering or self._watchdog_gave_up:
            return

        if self.name == "SDR2":
            timeout = getattr(self.config, 'SDR2_WATCHDOG_TIMEOUT', 10)
            max_restarts = getattr(self.config, 'SDR2_WATCHDOG_MAX_RESTARTS', 5)
        else:
            timeout = getattr(self.config, 'SDR_WATCHDOG_TIMEOUT', 10)
            max_restarts = getattr(self.config, 'SDR_WATCHDOG_MAX_RESTARTS', 5)

        elapsed = time.monotonic() - self._last_successful_read
        if elapsed < timeout:
            self._watchdog_stage = 0  # healthy
            return

        if self._watchdog_restarts >= max_restarts:
            if not self._watchdog_gave_up:
                print(f"\n[{self.name}] Watchdog: gave up after {max_restarts} recovery attempts")
                self._watchdog_gave_up = True
            return

        self._recovering = True
        try:
            # Try stages in order until one succeeds
            for stage in (1, 2, 3):
                if self._restart_stream(stage):
                    self._watchdog_restarts += 1
                    self._last_successful_read = time.monotonic()
                    self._watchdog_stage = 0
                    return
            # All stages failed
            self._watchdog_restarts += 1
            print(f"[{self.name}] Watchdog: all recovery stages failed (attempt {self._watchdog_restarts}/{max_restarts})")
        finally:
            self._recovering = False


class PipeWireSDRSource(SDRSource):
    """SDR audio input via PipeWire virtual sink monitor.

    Instead of reading from an ALSA loopback device (which delivers audio in
    high-jitter 200ms blobs), this source reads from a PipeWire virtual sink's
    monitor via parec subprocess.  PipeWire delivers a continuous, low-jitter
    stream — no blob boundaries, no prebuffering gaps, no crossfade needed.

    Config: set SDR_DEVICE_NAME = pw:<sink_name> (e.g. pw:sdr_capture)
    The sink must exist (created via pw-cli or startup script) and the SDR
    app's output must be routed to it.
    """

    def __init__(self, config, gateway, name="SDR1", sdr_priority=1):
        super().__init__(config, gateway, name=name, sdr_priority=sdr_priority)
        self._parec_proc = None
        self._reader_thread = None
        self._reader_running = False
        self._pw_sink_name = None  # set in setup_audio

    def setup_audio(self):
        """Start parec subprocess reading from PipeWire monitor."""
        import subprocess as _sp

        # Determine sink name from config
        if self.name == "SDR2":
            device_cfg = getattr(self.config, 'SDR2_DEVICE_NAME', '')
        else:
            device_cfg = getattr(self.config, 'SDR_DEVICE_NAME', '')

        # Strip pw: or pipewire: prefix
        if device_cfg.lower().startswith('pw:'):
            self._pw_sink_name = device_cfg[3:]
        elif device_cfg.lower().startswith('pipewire:'):
            self._pw_sink_name = device_cfg[9:]
        else:
            self._pw_sink_name = device_cfg

        monitor_name = f"{self._pw_sink_name}.monitor"

        # Verify the monitor source exists, auto-create sink if missing
        try:
            result = _sp.run(['pactl', 'list', 'short', 'sources'],
                             capture_output=True, text=True, timeout=5)
            if monitor_name not in result.stdout:
                print(f"[{self.name}] PipeWire monitor '{monitor_name}' not found, creating sink '{self._pw_sink_name}'...")
                create_result = _sp.run([
                    'pw-cli', 'create-node', 'adapter',
                    '{ factory.name=support.null-audio-sink'
                    f' node.name={self._pw_sink_name}'
                    ' media.class=Audio/Sink'
                    ' object.linger=true'
                    ' audio.position=[FL,FR] }'
                ], capture_output=True, text=True, timeout=5)
                if create_result.returncode != 0:
                    print(f"[{self.name}] Failed to create sink: {create_result.stderr.strip()}")
                    return False
                # Wait for PipeWire to register the new monitor
                time.sleep(1)
                result = _sp.run(['pactl', 'list', 'short', 'sources'],
                                 capture_output=True, text=True, timeout=5)
                if monitor_name not in result.stdout:
                    print(f"[{self.name}] Sink created but monitor '{monitor_name}' still not found")
                    print(f"[{self.name}]   Available sources:")
                    for line in result.stdout.strip().split('\n'):
                        if 'monitor' in line.lower():
                            print(f"[{self.name}]     {line}")
                    return False
                print(f"[{self.name}] Sink '{self._pw_sink_name}' created successfully")
        except Exception as e:
            print(f"[{self.name}] Failed to check PipeWire sources: {e}")
            return False

        # Set channel info — PipeWire sink is stereo
        self.sdr_channels = 2
        self._chunk_bytes = self.config.AUDIO_CHUNK_SIZE * self.sdr_channels * 2
        self._blob_bytes = self._chunk_bytes  # no blob concept, but keep for trace compat

        # Start parec reading from PipeWire monitor (native PulseAudio, no FFmpeg overhead)
        try:
            self._parec_proc = _sp.Popen([
                'parec',
                '--device=' + monitor_name,
                '--format=s16le',
                '--rate=' + str(self.config.AUDIO_RATE),
                '--channels=2',
                '--latency-msec=50',
            ], stdout=_sp.PIPE, stderr=_sp.PIPE)
        except FileNotFoundError:
            print(f"[{self.name}] parec not found — required for PipeWire SDR source")
            return False
        except Exception as e:
            print(f"[{self.name}] Failed to start parec: {e}")
            return False

        # Reader thread: reads fixed-size chunks from parec stdout and queues them
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._pw_reader_loop, daemon=True, name=f"{self.name}-pw-reader")
        self._reader_thread.start()

        # Wait briefly for first data
        _deadline = time.monotonic() + 2.0
        while self._chunk_queue.qsize() < 2 and time.monotonic() < _deadline:
            time.sleep(0.01)

        # Set input_stream to a truthy sentinel so the rest of the gateway
        # knows this source is active (many checks do `if source.input_stream:`)
        self.input_stream = True  # sentinel, not a real stream object

        if self._chunk_queue.qsize() > 0:
            self._prebuffering = False  # no prebuffering needed for PipeWire
            print(f"[{self.name}] PipeWire source active (monitor: {monitor_name})")
            return True
        else:
            print(f"[{self.name}] No audio received from PipeWire after 2s")
            self._reader_running = False
            if self._parec_proc:
                self._parec_proc.kill()
            return False

    def _pw_reader_loop(self):
        """Read fixed-size chunks from parec stdout and queue them."""
        chunk_bytes = self._chunk_bytes  # 50ms stereo = 9600 bytes
        proc = self._parec_proc
        while self._reader_running and proc and proc.poll() is None:
            try:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                if len(data) < chunk_bytes:
                    # Short read — pad with silence (shouldn't happen normally)
                    data += b'\x00' * (chunk_bytes - len(data))
                self._last_successful_read = time.monotonic()
                self._blob_times.append(time.monotonic())
                try:
                    self._chunk_queue.put_nowait(data)
                except _queue_mod.Full:
                    # Drop oldest to keep buffer fresh
                    try:
                        self._chunk_queue.get_nowait()
                    except _queue_mod.Empty:
                        pass
                    try:
                        self._chunk_queue.put_nowait(data)
                    except _queue_mod.Full:
                        pass
            except Exception:
                if self._reader_running:
                    time.sleep(0.01)

    def get_audio(self, chunk_size):
        """Get one chunk from PipeWire stream.

        Takes exactly ONE chunk per call. The reader thread delivers ~1 chunk
        per 50ms and the transmit loop consumes ~1 per 50ms. Taking only one
        keeps a small queue cushion so the next tick always has data.
        Only drain extras if queue grows beyond 4 (latency cap).
        """
        if not self.enabled or not self._reader_running:
            return None, False

        # Take one chunk (leave the rest as cushion for next tick)
        data = None
        try:
            data = self._chunk_queue.get_nowait()
        except _queue_mod.Empty:
            pass

        # If queue is building up (>6), drain extras to cap latency at ~300ms
        if data is not None:
            qsz = self._chunk_queue.qsize()
            while qsz > 6:
                try:
                    data = self._chunk_queue.get_nowait()
                    qsz -= 1
                except _queue_mod.Empty:
                    break

        self._last_blocked_ms = 0.0  # no blocking in PipeWire mode

        if data is None:
            return None, False

        cb = self._chunk_bytes

        # Muted: consume but discard
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        self.total_reads += 1
        self.last_read_time = time.time()

        raw = data

        # Stereo→mono FIRST (before discontinuity tracking)
        arr = np.frombuffer(raw, dtype=np.int16)
        if self.sdr_channels == 2 and len(arr) >= 2:
            stereo = arr.reshape(-1, 2).astype(np.int32)
            arr = ((stereo[:, 0] + stereo[:, 1]) >> 1).astype(np.int16)
            raw = arr.tobytes()

        # Sample discontinuity tracking (on mono data)
        if len(raw) >= 2:
            first_sample = int.from_bytes(raw[0:2], byteorder='little', signed=True)
            delta = abs(first_sample - self._last_serve_sample)
            self._serve_discontinuity = float(delta)
            self._last_serve_sample = int.from_bytes(raw[-2:], byteorder='little', signed=True)

        self._sub_buffer_after = self._chunk_queue.qsize() * cb  # approx remaining

        # Level metering and audio boost
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            _dg_key = 'SDR2_DISPLAY_GAIN' if self.name == 'SDR2' else 'SDR_DISPLAY_GAIN'
            display_gain = getattr(self.gateway.config, _dg_key, 1.0)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            _ab_key = 'SDR2_AUDIO_BOOST' if self.name == 'SDR2' else 'SDR_AUDIO_BOOST'
            audio_boost = getattr(self.gateway.config, _ab_key, 1.0)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        # Apply SDR audio processing (HPF, LPF, notch, gate)
        raw = self.gateway.process_audio_for_sdr(raw, self.name)

        return raw, False

    def cleanup(self):
        """Stop parec and reader thread."""
        self._reader_running = False
        self.input_stream = None
        if self._parec_proc:
            try:
                self._parec_proc.kill()
                self._parec_proc.wait(timeout=2)
            except Exception:
                pass
            self._parec_proc = None

    def _stop_reader(self):
        """Stop the reader and clear queue."""
        self._reader_running = False
        self._prebuffering = True
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break

    def _start_reader(self):
        """Restart reader after stop."""
        if self._parec_proc and self._parec_proc.poll() is None:
            self._reader_running = True
            if not self._reader_thread or not self._reader_thread.is_alive():
                self._reader_thread = threading.Thread(
                    target=self._pw_reader_loop, daemon=True, name=f"{self.name}-pw-reader")
                self._reader_thread.start()

    def _close_stream(self):
        """Close the parec stream."""
        self.cleanup()

    def _find_device(self):
        """Not used for PipeWire source."""
        return None, None

    def _watchdog_recover(self, max_restarts):
        """Restart parec if it died."""
        if self._parec_proc and self._parec_proc.poll() is not None:
            print(f"[{self.name}] PipeWire: parec process died, restarting...")
            self.cleanup()
            if self.setup_audio():
                print(f"[{self.name}] PipeWire: recovered")
            else:
                print(f"[{self.name}] PipeWire: recovery failed")


class RemoteAudioServer:
    """Connects out to a remote client and sends mixed audio over TCP.

    REMOTE_AUDIO_HOST = destination IP of the client machine.
    The server initiates the TCP connection and pushes length-prefixed PCM.
    Reconnects automatically if the link drops.
    """
    def __init__(self, config):
        self.config = config
        self.host = config.REMOTE_AUDIO_HOST
        self.port = int(config.REMOTE_AUDIO_PORT)
        self.connected = False
        self.client_address = None  # "host:port" when connected
        self._socket = None
        self._connect_thread = None
        self._running = False
        self._reconnect_interval = float(getattr(config, 'REMOTE_AUDIO_RECONNECT_INTERVAL', 5.0))

    def start(self):
        """Spawn connection thread that connects out to the client."""
        if not self.host:
            print("⚠ REMOTE_AUDIO_HOST not set — server has no destination to connect to")
            return
        self._running = True
        self._connect_thread = threading.Thread(
            target=self._connect_loop, name="RemoteAudio-connect", daemon=True
        )
        self._connect_thread.start()
        print(f"✓ Remote audio server will connect to {self.host}:{self.port}")

    def _connect_loop(self):
        """Connect to the client, reconnect on failure."""
        import socket
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((self.host, self.port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setblocking(False)  # non-blocking so send_audio never stalls audio loop
                self._socket = sock
                self.client_address = f"{self.host}:{self.port}"
                self.connected = True
                print(f"\n[RemoteAudio] Connected to client {self.client_address}")
                # Stay in this loop until disconnect is detected.
                # Probe the socket every 0.5s with select() — if the remote
                # end closes, the socket becomes readable (recv returns b'').
                # This catches disconnects even when send_audio() isn't called
                # (e.g. VAD gating all audio as silence).
                while self._running and self.connected:
                    try:
                        import select as _sel
                        readable, _, _ = _sel.select([sock], [], [], 0.5)
                        if readable:
                            # Socket readable on a send-only link = remote closed
                            probe = sock.recv(1)
                            if not probe:
                                break  # clean close
                    except Exception:
                        break  # error = dead
            except Exception:
                pass
            finally:
                self.connected = False
                self.client_address = None
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
            if self._running:
                time.sleep(self._reconnect_interval)

    def send_audio(self, pcm_data):
        """Send length-prefixed PCM to connected client.
        Uses non-blocking send to avoid stalling the audio transmit loop
        if the TCP buffer is full (e.g. slow client or network hiccup)."""
        sock = self._socket
        if not sock:
            return
        import struct
        try:
            frame = struct.pack('>I', len(pcm_data)) + pcm_data
            total = len(frame)
            sent = 0
            while sent < total:
                try:
                    n = sock.send(frame[sent:])
                    if n == 0:
                        raise ConnectionError("send returned 0")
                    sent += n
                except BlockingIOError:
                    # Socket buffer full — drop the rest of this frame
                    # rather than blocking the audio loop
                    break
        except Exception:
            # Link broken — trigger reconnect
            self.connected = False
            self._socket = None
            try:
                sock.close()
            except Exception:
                pass

    def reset(self):
        """Force-close the current connection so _connect_loop reconnects."""
        sock = self._socket
        self._socket = None
        self.connected = False
        self.client_address = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def cleanup(self):
        """Close socket."""
        self._running = False
        self.connected = False
        sock = self._socket
        self._socket = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


class RemoteAudioSource(AudioSource):
    """Listens for a TCP connection from a RemoteAudioServer and receives audio.

    REMOTE_AUDIO_HOST = bind address ('' or unset → 0.0.0.0, all interfaces).
    The server connects in; this end accepts and reads length-prefixed PCM.

    Name starts with 'SDR' so the mixer's duck system automatically handles it
    the same way it handles SDR1/SDR2 sources.
    """
    def __init__(self, config, gateway):
        super().__init__("SDRSV", config)
        self.gateway = gateway
        self.priority = 2  # Same as SDR sources in the mixer
        self.sdr_priority = int(config.REMOTE_AUDIO_PRIORITY)
        self.ptt_control = False
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = config.REMOTE_AUDIO_DUCK
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.server_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None
        self._conn = None  # current accepted connection (for reset)

    def setup_audio(self):
        """Bind listen socket and start the reader/accept thread."""
        import socket
        bind_host = self.config.REMOTE_AUDIO_HOST or '0.0.0.0'
        port = int(self.config.REMOTE_AUDIO_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="SDRSV-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Remote audio client listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept connections from the server and read length-prefixed PCM."""
        import socket, struct

        while self._reader_running:
            # Wait for the server to connect in
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self._conn = conn
                self.server_connected = True
                print(f"\n[SDRSV] Server connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    # Read 4-byte length header
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break  # sanity check
                    # Read PCM payload
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break
                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        # Drop oldest to keep queue fresh
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[SDRSV] Connection error: {e}")
            finally:
                self.server_connected = False
                self._conn = None
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def reset(self):
        """Force-close the current connection so the reader thread re-accepts."""
        conn = self._conn
        self._conn = None
        self.server_connected = False
        self._sub_buffer = b''
        # Drain the queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes from socket, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Drain queue, slice sub-buffer, level metering, audio boost."""
        if not self.enabled:
            return None, False

        # Skip queue lock entirely when not connected — nothing to drain
        if not self.server_connected and not self._sub_buffer:
            return None, False

        cb = self._chunk_bytes

        # Fill sub-buffer from queue
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Muted: keep draining but discard
        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Level metering and audio boost
        arr = np.frombuffer(raw, dtype=np.int16)
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = float(self.config.REMOTE_AUDIO_DISPLAY_GAIN)
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            audio_boost = float(self.config.REMOTE_AUDIO_AUDIO_BOOST)
            if audio_boost != 1.0:
                arr = np.clip(farr * audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        return raw, False  # Never triggers PTT

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "SDRSV: Disabled"
        elif self.muted:
            return "SDRSV: Muted"
        elif self.server_connected:
            return f"SDRSV: Connected ({self.audio_level}%)"
        else:
            return "SDRSV: Disconnected"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
            if self._reader_thread.is_alive():
                print(f"[RemoteAudioSource] Warning: reader thread did not stop within 2s")
        self._sub_buffer = b''


class D75AudioSource(AudioSource):
    """TCP client that connects to D75_CAT.py's audio streaming port (9751)
    and receives raw 8kHz 16-bit mono PCM, resamples to 48kHz for the mixer.

    The D75 audio stream has no length prefix — just continuous raw PCM bytes.
    Reconnects automatically if the connection drops.
    """
    def __init__(self, config, gateway):
        super().__init__("D75", config)
        self.gateway = gateway
        self.priority = 2
        self.sdr_priority = int(getattr(config, 'D75_AUDIO_PRIORITY', 2))
        self.ptt_control = False
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = getattr(config, 'D75_AUDIO_DUCK', True)
        self.enabled = True
        self.muted = False
        self.audio_boost = float(getattr(config, 'D75_AUDIO_BOOST', 1.0))

        self.audio_level = 0
        self.server_connected = False

        self._host = config.D75_HOST
        self._port = int(config.D75_AUDIO_PORT)
        self._reconnect_interval = float(getattr(config, 'D75_RECONNECT_INTERVAL', 5.0))

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono at 48kHz
        self._reader_running = False
        self._reader_thread = None
        self._sock = None

    def setup_audio(self):
        """Start the reader thread that connects out to D75 audio port."""
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="D75-audio-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ D75 audio source connecting to {self._host}:{self._port}")
        return True

    def _reader_thread_func(self):
        """Connect to D75 audio port, read raw 8kHz PCM, upsample to 48kHz.

        Uses linear interpolation (6x upsample) which is clean for the exact
        integer ratio 8kHz→48kHz and avoids boundary artifacts that batch
        resamplers (resampy) cause on small streaming chunks.
        """
        import socket as _sock_mod

        # Accumulate 400 samples at 8kHz = 50ms = produces 2400 samples at 48kHz
        samples_8k = self.config.AUDIO_CHUNK_SIZE // 6  # 2400 // 6 = 400
        bytes_8k = samples_8k * 2  # 800 bytes
        # Pre-compute interpolation indices (constant for all chunks)
        _ratio = 6
        _out_len = samples_8k * _ratio
        _interp_idx = np.linspace(0, samples_8k - 1, _out_len).astype(np.float32)
        _interp_src = np.arange(samples_8k, dtype=np.float32)
        # Keep last sample from previous chunk for seamless interpolation
        _prev_last = np.float32(0)

        while self._reader_running:
            sock = None
            try:
                sock = _sock_mod.socket(_sock_mod.AF_INET, _sock_mod.SOCK_STREAM)
                sock.settimeout(self._reconnect_interval)
                sock.connect((self._host, self._port))
                sock.setsockopt(_sock_mod.IPPROTO_TCP, _sock_mod.TCP_NODELAY, 1)
                sock.settimeout(2.0)
                self._sock = sock
                self.server_connected = True
                _prev_last = np.float32(0)
                print(f"\n[D75] Audio connected to {self._host}:{self._port}")

                raw_buf = b''
                while self._reader_running:
                    try:
                        data = sock.recv(4096)
                    except _sock_mod.timeout:
                        continue
                    if not data:
                        break
                    raw_buf += data

                    # Process complete chunks
                    while len(raw_buf) >= bytes_8k:
                        chunk_8k = raw_buf[:bytes_8k]
                        raw_buf = raw_buf[bytes_8k:]

                        # 6x linear interpolation (streaming-safe)
                        arr_8k = np.frombuffer(chunk_8k, dtype=np.int16).astype(np.float32)
                        # Prepend previous chunk's last sample for seamless boundary
                        extended = np.concatenate(([_prev_last], arr_8k))
                        _prev_last = arr_8k[-1]
                        # Interpolate: map 0..samples_8k in output to 0..samples_8k in extended
                        idx_ext = np.linspace(0, len(extended) - 1, _out_len).astype(np.float32)
                        arr_48k = np.interp(idx_ext, np.arange(len(extended), dtype=np.float32), extended)
                        pcm_48k = np.clip(arr_48k, -32768, 32767).astype(np.int16).tobytes()

                        try:
                            self._chunk_queue.put_nowait(pcm_48k)
                        except _queue_mod.Full:
                            try:
                                self._chunk_queue.get_nowait()
                            except _queue_mod.Empty:
                                pass
                            try:
                                self._chunk_queue.put_nowait(pcm_48k)
                            except _queue_mod.Full:
                                pass

            except _sock_mod.timeout:
                pass
            except Exception as e:
                if self._reader_running and getattr(self.config, 'VERBOSE_LOGGING', False):
                    print(f"\n[D75] Audio connection error: {e}")
            finally:
                self.server_connected = False
                self._sock = None
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if self._reader_running:
                    time.sleep(self._reconnect_interval)

    _tx_write_count = 0
    def write_tx_audio(self, pcm_48k):
        """Downsample 48kHz PCM to 8kHz and send to D75 via audio TCP for BT TX.

        The D75_CAT.py AudioTCPServer reads incoming data from connected clients
        and writes it to SCO for radio transmission.
        """
        if not self._sock or not self.server_connected:
            if self._tx_write_count == 0:
                print(f"  [D75 TX] write_tx_audio: no sock/server (sock={self._sock is not None}, conn={self.server_connected})")
            return False
        try:
            # Downsample 48kHz → 8kHz (take every 6th sample)
            arr = np.frombuffer(pcm_48k, dtype=np.int16)
            arr_8k = arr[::6]
            data_8k = arr_8k.tobytes()
            self._sock.sendall(data_8k)
            self._tx_write_count += 1
            if self._tx_write_count <= 3 or self._tx_write_count % 100 == 0:
                peak = int(np.max(np.abs(arr_8k))) if len(arr_8k) > 0 else 0
                print(f"  [D75 TX] sent {len(data_8k)}B (#{self._tx_write_count}, peak={peak})")
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            print(f"  [D75 TX] write error: {e}")
            return False

    def get_audio(self, chunk_size):
        """Drain queue, slice sub-buffer, level metering, audio boost."""
        if not self.enabled:
            return None, False
        if not self.server_connected and not self._sub_buffer:
            return None, False

        cb = self._chunk_bytes
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        should_discard = self.muted or (self.gateway.tx_muted and self.gateway.rx_muted)
        if should_discard:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        arr = np.frombuffer(raw, dtype=np.int16)
        if len(arr) > 0:
            farr = arr.astype(np.float32)
            rms = float(np.sqrt(np.mean(farr * farr)))
            if rms > 0:
                db = 20 * _math_mod.log10(rms / 32767.0)
                raw_level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                raw_level = 0
            display_gain = float(getattr(self.config, 'D75_AUDIO_DISPLAY_GAIN', 1.0))
            display_level = min(100, int(raw_level * display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)

            if self.audio_boost != 1.0:
                arr = np.clip(farr * self.audio_boost, -32768, 32767).astype(np.int16)
                raw = arr.tobytes()

        # Apply D75 audio processing chain (HPF → LPF → Notch → Noise Gate)
        if hasattr(self.gateway, 'd75_processor'):
            raw = self.gateway.process_audio_for_d75(raw)

        return raw, False

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "D75: Disabled"
        elif self.muted:
            return "D75: Muted"
        elif self.server_connected:
            return f"D75: Connected ({self.audio_level}%)"
        else:
            return "D75: Disconnected"

    def reset(self):
        """Force-close connection to trigger reconnect."""
        sock = self._sock
        self._sock = None
        self.server_connected = False
        self._sub_buffer = b''
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def cleanup(self):
        """Stop reader thread."""
        self._reader_running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._sub_buffer = b''


# ── KV4P HT Radio (USB Serial + Opus Audio) ──────────────────────────────

class KV4PCATClient:
    """Control interface for KV4P HT radio over USB serial.

    Wraps the kv4p Python driver to provide a CAT-like interface matching the
    gateway's D75CATClient pattern: connect, poll state, send commands.
    """

    def __init__(self, port, config, verbose=False):
        self._port = port
        self._config = config
        self._verbose = verbose
        self._radio = None          # KV4PRadio instance
        self._connected = False
        self._serial_connected = False
        self._lock = threading.Lock()
        self._stop = False
        self._poll_thread = None

        # Radio state
        self._frequency = 146.520
        self._tx_frequency = 146.520
        self._squelch = 4
        self._bandwidth = 1
        self._ctcss_tx = 0
        self._ctcss_rx = 0
        self._high_power = True
        self._signal = 0          # S-meter raw value (0-255)
        self._transmitting = False
        self._firmware_version = 0
        self._rf_module = 'VHF'
        self._smeter_enabled = False

        # Callbacks (set by KV4PAudioSource)
        self.on_rx_audio = None   # Opus frames
        self.on_smeter = None

    def connect(self):
        """Open serial connection to KV4P HT."""
        try:
            sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
            from kv4p.radio import KV4PRadio
            from kv4p.protocol import GroupConfig, VersionInfo
            self._radio = KV4PRadio(self._port)

            # Wire up callbacks before open
            self._radio.on_rx_audio = self._on_rx_audio
            self._radio.on_smeter = self._on_smeter
            self._radio.on_phys_ptt = self._on_phys_ptt

            ver = self._radio.open(handshake_timeout=10)
            self._connected = True
            self._serial_connected = True

            if ver:
                self._firmware_version = ver.firmware_version
                self._rf_module = ver.rf_module_type.name if hasattr(ver.rf_module_type, 'name') else 'VHF'

            # Apply initial config
            freq = float(getattr(self._config, 'KV4P_FREQ', 146.520))
            tx_freq = float(getattr(self._config, 'KV4P_TX_FREQ', 0))
            if tx_freq <= 0:
                tx_freq = freq
            self._frequency = freq
            self._tx_frequency = tx_freq
            self._squelch = int(getattr(self._config, 'KV4P_SQUELCH', 4))
            self._bandwidth = int(getattr(self._config, 'KV4P_BANDWIDTH', 1))
            self._ctcss_tx = int(getattr(self._config, 'KV4P_CTCSS_TX', 0))
            self._ctcss_rx = int(getattr(self._config, 'KV4P_CTCSS_RX', 0))
            self._high_power = bool(getattr(self._config, 'KV4P_HIGH_POWER', True))

            self._apply_group()
            time.sleep(0.3)
            # Enable DRA818 hardware filters after SA818 is initialized by GROUP
            from kv4p.protocol import FiltersConfig
            self._radio.set_filters(FiltersConfig(pre_emphasis=True, highpass=True, lowpass=True))
            self._radio.set_power(self._high_power)

            if getattr(self._config, 'KV4P_SMETER', True):
                self._radio.enable_smeter(True)
                self._smeter_enabled = True

            return True
        except Exception as e:
            print(f"  KV4P connect error: {e}")
            self._connected = False
            self._serial_connected = False
            return False

    def close(self):
        """Stop and disconnect."""
        self._stop = True
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)
        if self._radio:
            try:
                self._radio.close()
            except Exception:
                pass
        self._connected = False
        self._serial_connected = False

    def _apply_group(self):
        """Send current frequency/tone/squelch config to radio.

        Sends the GROUP command twice with a short delay — the SA818 module
        occasionally fails to apply the config on the first attempt,
        especially after firmware reset or power cycle.
        """
        if not self._radio:
            return
        sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
        from kv4p.protocol import GroupConfig
        group = GroupConfig(
            tx_freq=self._tx_frequency,
            rx_freq=self._frequency,
            bandwidth=self._bandwidth,
            ctcss_tx=self._ctcss_tx,
            squelch=self._squelch,
            ctcss_rx=self._ctcss_rx,
        )
        self._radio.tune(group)
        time.sleep(0.2)
        self._radio.tune(group)  # Retry to ensure SA818 applies config

    def set_frequency(self, freq_mhz, tx_freq_mhz=None):
        """Set RX (and optionally TX) frequency."""
        self._frequency = freq_mhz
        self._tx_frequency = tx_freq_mhz if tx_freq_mhz else freq_mhz
        self._apply_group()

    def set_squelch(self, level):
        """Set squelch level (0-8)."""
        self._squelch = max(0, min(8, int(level)))
        self._apply_group()

    def set_ctcss(self, tx=None, rx=None):
        """Set CTCSS tone codes."""
        if tx is not None:
            self._ctcss_tx = int(tx)
        if rx is not None:
            self._ctcss_rx = int(rx)
        self._apply_group()

    def set_bandwidth(self, wide=True):
        """Set bandwidth: True = wide (25kHz), False = narrow (12.5kHz)."""
        self._bandwidth = 1 if wide else 0
        self._apply_group()

    def set_power(self, high=True):
        """Set TX power level."""
        self._high_power = high
        if self._radio:
            self._radio.set_power(high)

    def ptt_on(self):
        """Key the transmitter."""
        if self._radio:
            self._radio.ptt_on()
            self._transmitting = True

    def ptt_off(self):
        """Unkey the transmitter."""
        if self._radio:
            self._radio.ptt_off()
            self._transmitting = False

    def send_tx_audio(self, opus_data):
        """Send an Opus-encoded TX audio frame."""
        if self._radio:
            self._radio.send_audio(opus_data)

    def enable_smeter(self, enabled=True):
        """Enable/disable S-meter reporting."""
        self._smeter_enabled = enabled
        if self._radio:
            self._radio.enable_smeter(enabled)

    _rx_audio_count = 0

    def _on_rx_audio(self, opus_data):
        """Called by KV4PRadio reader thread with Opus RX audio."""
        self._rx_audio_count += 1
        if self._rx_audio_count <= 3 or self._rx_audio_count % 500 == 0:
            print(f"\n[KV4P] _on_rx_audio #{self._rx_audio_count}: {len(opus_data)}B, callback={'set' if self.on_rx_audio else 'NONE'}", flush=True)
        if self.on_rx_audio:
            self.on_rx_audio(opus_data)

    def _on_smeter(self, rssi):
        """Called by KV4PRadio reader thread with RSSI value."""
        self._signal = rssi
        if self.on_smeter:
            self.on_smeter(rssi)

    def _on_phys_ptt(self, pressed):
        """Called when physical PTT button on KV4P is pressed/released."""
        if self._verbose:
            print(f"\n[KV4P] Physical PTT {'pressed' if pressed else 'released'}")

    def start_polling(self):
        """Start background health-check thread."""
        self._stop = False
        self._poll_thread = threading.Thread(target=self._poll_func, daemon=True, name="KV4P-poll")
        self._poll_thread.start()

    def _poll_func(self):
        """Background thread: monitor connection health, auto-reconnect."""
        while not self._stop:
            for _ in range(20):  # 2 second sleep in 0.1s increments
                if self._stop:
                    return
                time.sleep(0.1)
            # Check if radio is still responding
            if self._radio and not self._radio._running:
                print("\n[KV4P] Radio connection lost, attempting reconnect...")
                self._connected = False
                self._serial_connected = False
                try:
                    self._radio.close()
                except Exception:
                    pass
                time.sleep(float(getattr(self._config, 'KV4P_RECONNECT_INTERVAL', 5.0)))
                self.connect()

    def get_radio_state(self):
        """Return state dict for web UI."""
        return {
            'connected': self._connected,
            'serial_connected': self._serial_connected,
            'frequency': f'{self._frequency:.6f}',
            'tx_frequency': f'{self._tx_frequency:.6f}',
            'squelch': self._squelch,
            'bandwidth': self._bandwidth,
            'ctcss_tx': self._ctcss_tx,
            'ctcss_rx': self._ctcss_rx,
            'high_power': self._high_power,
            'signal': self._signal,
            'transmitting': self._transmitting,
            'firmware_version': self._firmware_version,
            'rf_module': self._rf_module,
            'smeter_enabled': self._smeter_enabled,
        }


class KV4PAudioSource(AudioSource):
    """Audio source for KV4P HT radio — receives Opus-encoded RX audio,
    decodes to 48kHz 16-bit mono PCM for the gateway mixer.

    Also handles TX audio: encodes 48kHz PCM to Opus and sends via serial.
    """

    def __init__(self, config, gateway):
        super().__init__("KV4P", config)
        self.gateway = gateway
        self.priority = 2
        self.sdr_priority = int(getattr(config, 'KV4P_AUDIO_PRIORITY', 2))
        self.ptt_control = False
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = getattr(config, 'KV4P_AUDIO_DUCK', True)
        self.enabled = True
        self.muted = False
        self.audio_boost = float(getattr(config, 'KV4P_AUDIO_BOOST', 1.0))

        self._chunk_queue = collections.deque(maxlen=16)  # Buffer for timing jitter, drops oldest on overflow
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * config.AUDIO_CHANNELS * 2  # samples × channels × 16-bit
        # Streaming resampler: maintains a fractional sample position across
        # the sub_buffer to produce exactly _chunk_bytes per tick at the mixer's
        # sample rate. The ratio compensates for the ESP32's ~2% clock offset.
        # Phase continuity across chunks prevents clicks/swimming artifacts.
        self._resample_ratio = 1.132  # Measured: ESP32 sends at 28.3fps vs nominal 25fps
        self._ratio_adjust_interval = 20   # Fine-tune every 20 ticks (1 second)
        self._ratio_tick_count = 0
        self._resample_pos = 0.0     # Fractional position in sub_buffer (in samples)
        self._buf_max = self._chunk_bytes * 6  # Cap latency
        self._decoder = None
        self._encoder = None
        self._dc_remover = None    # DCOffsetRemover instance
        self._vol_ramp = None      # VolumeRamp instance
        self._was_active = False   # Track audio activity for ramp
        self.server_connected = False
        self.audio_level = 0

    def setup_audio(self):
        """Initialize Opus codec, DSP chain, and wire up to KV4P CAT client."""
        try:
            sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
            import opuslib
            from kv4p.audio import DCOffsetRemover, VolumeRamp
            self._decoder = opuslib.Decoder(48000, 1)
            self._encoder = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)
            # Fast DC remover applied per-Opus-frame to remove inter-frame DC jumps
            self._dc_remover_frame = DCOffsetRemover(decay_time=0.02, sample_rate=48000)
            # Slower DC remover on mixer output for residual offset
            self._dc_remover = DCOffsetRemover(decay_time=0.25, sample_rate=48000)
            self._vol_ramp = VolumeRamp(alpha=0.05, threshold=0.7)
            self.server_connected = True
            print("  KV4P Opus codec + DSP initialized (DC removal, volume ramp)")
            return True
        except ImportError:
            print("  ⚠ opuslib not installed — KV4P audio disabled")
            print("    Install with: pip install opuslib --break-system-packages")
            return False
        except Exception as e:
            print(f"  ⚠ KV4P audio init error: {e}")
            return False

    def on_opus_rx(self, opus_data):
        """Called by KV4PCATClient when Opus RX audio arrives from radio.

        Decodes Opus, then resamples from the ESP32's 2%-overclocked rate
        down to exact 48kHz. Resampling here (per-frame) avoids phase
        discontinuities at frame boundaries.
        """
        if not self._decoder or not self.enabled:
            return
        self._trace_rx_frames += 1
        self._trace_rx_bytes += len(opus_data)
        try:
            pcm = self._decoder.decode(opus_data, 1920)  # 40ms frame → 3840 bytes
            if len(self._chunk_queue) >= self._chunk_queue.maxlen:
                self._chunk_queue.popleft()
                self._trace_queue_drops += 1
            self._chunk_queue.append(pcm)
        except Exception:
            pass  # Corrupt frame — skip

    def write_tx_audio(self, pcm_48k):
        """Encode 48kHz PCM and send to radio for transmission.

        Args:
            pcm_48k: bytes of signed 16-bit LE mono PCM at 48kHz
        Returns:
            True if sent, False on error
        """
        if not self._encoder:
            return False
        cat = getattr(self.gateway, 'kv4p_cat', None)
        if not cat:
            return False
        try:
            # Opus needs exactly 1920 samples (40ms at 48kHz) = 3840 bytes.
            # Accumulate across calls: the 960-byte remainder from each 4800-byte
            # mixer chunk is carried into the next call rather than discarded,
            # otherwise 20% of TX audio is silently dropped every tick.
            frame_bytes = 1920 * 2
            self._tx_buf = getattr(self, '_tx_buf', b'') + pcm_48k
            buf = self._tx_buf
            # Trace: RMS of full input including carry-over
            try:
                import numpy as np
                _arr = np.frombuffer(pcm_48k, dtype=np.int16).astype(np.float32)
                self._trace_tx_input_rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
            except Exception:
                pass
            frames_sent = 0
            while len(buf) >= frame_bytes:
                try:
                    opus_frame = self._encoder.encode(buf[:frame_bytes], 1920)
                    cat.send_tx_audio(opus_frame)
                    frames_sent += 1
                except Exception:
                    self._trace_tx_errors += 1
                buf = buf[frame_bytes:]
            self._tx_buf = buf  # carry remainder into next call
            self._trace_tx_frames += frames_sent
            self._trace_tx_dropped += len(buf)  # bytes not yet sent this tick
            return True
        except Exception:
            return False

    # Instrumentation state
    _inst_count = 0
    _inst_returns = 0
    _inst_nones = 0
    _inst_trims = 0
    _inst_t0 = 0
    _inst_intervals = []
    _inst_sub_sizes = []

    # Per-tick trace state (read and reset each mixer tick when trace is active)
    _trace_rx_frames = 0       # Opus frames received since last tick
    _trace_rx_bytes = 0        # Opus bytes received since last tick
    _trace_decode_errors = 0   # Opus decode failures since last tick
    _trace_queue_drops = 0     # Frames dropped due to queue overflow
    _trace_sub_buf_before = 0  # sub_buffer size at start of get_audio
    _trace_sub_buf_after = 0   # sub_buffer size after get_audio read
    _trace_returned_data = False  # Did get_audio return data this tick?
    _trace_pcm_rms = 0.0      # RMS of returned audio chunk
    # TX-side trace
    _trace_tx_frames = 0      # Opus frames encoded and sent to radio this tick
    _trace_tx_dropped = 0     # PCM bytes in carry buffer at end of write_tx_audio (not dropped, carried to next tick)
    _trace_tx_input_rms = 0.0 # RMS of PCM fed into encoder this tick
    _trace_tx_errors = 0      # Opus encoder exceptions this tick

    def get_audio(self, chunk_size):
        """Pull decoded PCM audio from the queue for the mixer."""
        import time as _time
        now = _time.monotonic()
        self._inst_count += 1

        # Track call interval
        if self._inst_t0 > 0:
            self._inst_intervals.append(now - self._inst_t0)
        self._inst_t0 = now

        if not self.enabled or not self.server_connected:
            self._trace_returned_data = False
            return None, False

        # Drain queue into sub-buffer
        q_drained = 0
        while self._chunk_queue:
            self._sub_buffer += self._chunk_queue.popleft()
            q_drained += 1

        self._trace_sub_buf_before = len(self._sub_buffer)
        trimmed = 0
        self._inst_sub_sizes.append(len(self._sub_buffer))

        # Adaptive PLL: adjust resample ratio every tick to keep buffer near target.
        # Proportional control: ratio nudge proportional to buffer error.
        buf_target = self._chunk_bytes * 3  # 14400 bytes — higher target for underrun margin
        buf_now = len(self._sub_buffer)
        buf_error = (buf_now - buf_target) / buf_target  # normalized: >0 = too full, <0 = too empty
        # Proportional gain: 0.002 per 100% error, capped
        adjustment = buf_error * 0.002
        self._resample_ratio = max(0.95, min(1.25, self._resample_ratio + adjustment))

        self._inst_returns += 1
        if self._inst_count % 200 == 0:
            self._print_inst("ok")

        # Streaming resampler: consume input at ratio 1.02 (ESP32 2% overclock)
        # using vectorized linear interpolation with phase continuity.
        import numpy as np
        n_input_samples = len(self._sub_buffer) // 2
        out_samples_needed = self._chunk_bytes // 2  # 2400

        # Need enough input samples to produce one output chunk
        input_needed = int(self._resample_pos + out_samples_needed * self._resample_ratio) + 2
        if n_input_samples < input_needed:
            self._inst_nones += 1
            self._trace_returned_data = False
            self._trace_sub_buf_after = len(self._sub_buffer)
            self._trace_pcm_rms = 0.0
            self.audio_level = int(self.audio_level * 0.9)
            return None, False

        in_samples = np.frombuffer(self._sub_buffer, dtype=np.int16).astype(np.float32)

        # Vectorized linear interpolation with fractional positions
        positions = self._resample_pos + np.arange(out_samples_needed) * self._resample_ratio
        indices = positions.astype(np.intp)
        fracs = positions - indices
        # Clamp to valid range
        np.clip(indices, 0, n_input_samples - 2, out=indices)
        out = in_samples[indices] * (1.0 - fracs) + in_samples[indices + 1] * fracs

        # Consume the input samples we've read past
        consumed_samples = int(positions[-1]) + 1
        self._resample_pos = positions[-1] + self._resample_ratio - consumed_samples
        self._sub_buffer = self._sub_buffer[consumed_samples * 2:]

        # Cap buffer to bound latency
        if len(self._sub_buffer) > self._buf_max:
            excess = len(self._sub_buffer) - self._buf_max
            excess = (excess + 1) & ~1
            self._sub_buffer = self._sub_buffer[excess:]
            self._resample_pos = 0.0
            self._inst_trims += 1

        self._trace_sub_buf_after = len(self._sub_buffer)
        self._trace_returned_data = True
        pcm_data = np.clip(out, -32768, 32767).astype(np.int16).tobytes()

        # Mute check
        if self.muted or (getattr(self.gateway, 'tx_muted', False) and getattr(self.gateway, 'rx_muted', False)):
            self.audio_level = int(self.audio_level * 0.7)
            return None, False

        # DC offset removal (matches ESP32 firmware pipeline)
        if self._dc_remover:
            pcm_data = self._dc_remover.process(pcm_data)

        # Level metering
        try:
            import numpy as np
            arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            rms = np.sqrt(np.mean(arr ** 2)) if len(arr) > 0 else 0.0
            if rms > 0:
                import math
                db = 20 * math.log10(rms / 32767.0)
                level = max(0, min(100, (db + 60) * (100 / 60)))
            else:
                level = 0
            display_gain = float(getattr(self.config, 'KV4P_AUDIO_DISPLAY_GAIN', 1.0))
            level = min(100, level * display_gain)
            if level > self.audio_level:
                self.audio_level = int(level)
            else:
                self.audio_level = int(self.audio_level * 0.7 + level * 0.3)
        except Exception:
            pass

        # Audio boost
        if self.audio_boost != 1.0:
            try:
                import numpy as np
                arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
                pcm_data = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()
            except Exception:
                pass

        # Audio processing chain
        if hasattr(self.gateway, 'process_audio_for_kv4p'):
            pcm_data = self.gateway.process_audio_for_kv4p(pcm_data)

        # Trace: compute RMS of final output
        try:
            import numpy as np
            _arr = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            self._trace_pcm_rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
        except Exception:
            self._trace_pcm_rms = 0.0

        # Recording hook: append PCM to file when active
        if getattr(self, '_recording_file', None):
            try:
                self._recording_file.write(pcm_data)
            except Exception:
                pass

        return pcm_data, False

    def get_trace_snapshot(self):
        """Return trace state dict and reset per-tick counters."""
        snap = {
            'rx_frames': self._trace_rx_frames,
            'rx_bytes': self._trace_rx_bytes,
            'decode_errors': self._trace_decode_errors,
            'queue_drops': self._trace_queue_drops,
            'sub_buf_before': self._trace_sub_buf_before,
            'sub_buf_after': self._trace_sub_buf_after,
            'returned_data': self._trace_returned_data,
            'pcm_rms': self._trace_pcm_rms,
            'queue_len': len(self._chunk_queue),
            'tx_frames': self._trace_tx_frames,
            'tx_dropped': self._trace_tx_dropped,
            'tx_input_rms': self._trace_tx_input_rms,
            'tx_errors': self._trace_tx_errors,
        }
        # Reset per-tick counters
        self._trace_rx_frames = 0
        self._trace_rx_bytes = 0
        self._trace_decode_errors = 0
        self._trace_queue_drops = 0
        self._trace_tx_frames = 0
        self._trace_tx_dropped = 0
        self._trace_tx_input_rms = 0.0
        self._trace_tx_errors = 0
        return snap

    def _print_inst(self, tag):
        """Print audio pipeline instrumentation."""
        total = self._inst_count
        ret = self._inst_returns
        none = self._inst_nones
        trims = self._inst_trims
        intervals = self._inst_intervals[-200:] if self._inst_intervals else []

        avg_int = sum(intervals) / len(intervals) * 1000 if intervals else 0
        min_int = min(intervals) * 1000 if intervals else 0
        max_int = max(intervals) * 1000 if intervals else 0

        sub_sizes = self._inst_sub_sizes[-200:] if self._inst_sub_sizes else []
        avg_sub = sum(sub_sizes) / len(sub_sizes) if sub_sizes else 0

        pct_data = (ret / total * 100) if total > 0 else 0
        pct_none = (none / total * 100) if total > 0 else 0

        print(f"\n[KV4P Audio] {tag} | calls={total} data={ret}({pct_data:.0f}%) none={none}({pct_none:.0f}%) trims={trims}"
              f" | interval: avg={avg_int:.1f}ms min={min_int:.1f}ms max={max_int:.1f}ms"
              f" | sub_buf: avg={avg_sub:.0f}B chunk={self._chunk_bytes}B"
              f" | queue_max={self._chunk_queue.maxlen}", flush=True)

        # Reset counters
        self._inst_intervals = []
        self._inst_sub_sizes = []

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "KV4P: Disabled"
        if self.muted:
            return "KV4P: Muted"
        if self.server_connected:
            return f"KV4P: Connected ({int(self.audio_level)}%)"
        return "KV4P: Disconnected"

    def reset(self):
        """Reset audio state."""
        self._sub_buffer = b''
        self._chunk_queue.clear()
        self.audio_level = 0
        self._was_active = False
        if self._dc_remover:
            self._dc_remover.reset()
        if self._vol_ramp:
            self._vol_ramp.stop()

    def cleanup(self):
        self._sub_buffer = b''
        self._chunk_queue.clear()
        self.server_connected = False
        self._was_active = False


class NetworkAnnouncementSource(AudioSource):
    """Listens for an inbound TCP connection on port 9601 and receives PCM
    audio to transmit over the radio.

    Same wire format as RemoteAudioSource (length-prefixed 16-bit mono PCM at
    the configured sample rate).  Unlike RemoteAudioSource, ptt_control=True so
    the mixer routes the audio to radio TX and activates PTT.  PTT is released
    automatically by the gateway's PTT_RELEASE_DELAY timeout once the queue
    drains after the sender disconnects.
    """
    def __init__(self, config, gateway):
        super().__init__("ANNIN", config)
        self.gateway = gateway
        self.priority = 0           # Same highest priority as FilePlayback
        self.ptt_control = True     # Routes to radio TX and activates PTT
        self.volume = float(getattr(config, 'ANNOUNCE_INPUT_VOLUME', 4.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=16)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2   # 16-bit mono
        self._ptt_hold_time = 2.0   # seconds of silence before releasing PTT
        self._last_above_threshold = 0.0  # monotonic time of last above-threshold chunk
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None

    def setup_audio(self):
        """Bind listen socket and start accept/reader thread."""
        import socket
        bind_host = self.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
        port = int(self.config.ANNOUNCE_INPUT_PORT)
        self._listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_socket.settimeout(1.0)
        self._listen_socket.bind((bind_host, port))
        self._listen_socket.listen(1)
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_thread_func,
            name="ANNIN-reader",
            daemon=True
        )
        self._reader_thread.start()
        print(f"✓ Announcement input listening on {bind_host}:{port}")
        return True

    def _reader_thread_func(self):
        """Accept one client at a time and read length-prefixed PCM."""
        import socket, struct

        while self._reader_running:
            conn = None
            try:
                conn, addr = self._listen_socket.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(2.0)
                self.client_connected = True
                print(f"\n[ANNIN] Client connected from {addr[0]}:{addr[1]}")

                while self._reader_running:
                    header = self._recv_exact(conn, 4)
                    if header is None:
                        break
                    msg_len = struct.unpack('>I', header)[0]
                    if msg_len == 0 or msg_len > 96000:
                        break
                    payload = self._recv_exact(conn, msg_len)
                    if payload is None:
                        break
                    try:
                        self._chunk_queue.put_nowait(payload)
                    except _queue_mod.Full:
                        try:
                            self._chunk_queue.get_nowait()
                        except _queue_mod.Empty:
                            pass
                        try:
                            self._chunk_queue.put_nowait(payload)
                        except _queue_mod.Full:
                            pass
            except socket.timeout:
                continue
            except Exception as e:
                if self._reader_running and self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Connection error: {e}")
            finally:
                self.client_connected = False
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if self.config.VERBOSE_LOGGING:
                    print(f"\n[ANNIN] Client disconnected")

    @staticmethod
    def _recv_exact(sock, n):
        """Receive exactly n bytes, or return None on disconnect."""
        data = b''
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except Exception:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def get_audio(self, chunk_size):
        """Return (pcm, True) when above-threshold audio is available.

        Silence frames are consumed from the queue but discarded (return
        (None, False)) so PTT is not triggered by idle stream packets.
        A 2-second hold keeps PTT active through brief pauses in speech
        so the radio doesn't drop and re-key between sentences.
        """
        if not self.enabled or self.muted:
            return None, False

        cb = self._chunk_bytes
        now = time.monotonic()

        # Fill sub-buffer from queue — always drain so idle silence doesn't
        # back up the queue while the connection is held open.
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                # No data in queue — check if PTT hold is still active
                if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                    return b'\x00' * cb, True  # silence but keep PTT keyed
                self.audio_level = 0
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering + threshold gate
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        if rms > 0:
            db = 20 * _math_mod.log10(rms / 32767.0)
            raw_level = max(0, min(100, (db + 60) * (100 / 60)))
        else:
            db = -100.0
            raw_level = 0

        if raw_level > self.audio_level:
            self.audio_level = raw_level
        else:
            self.audio_level = int(self.audio_level * 0.7 + raw_level * 0.3)

        threshold_db = float(getattr(self.config, 'ANNOUNCE_INPUT_THRESHOLD', -45.0))
        if db < threshold_db:
            # Below threshold — hold PTT with silence for up to 2s
            if now - self._last_above_threshold < self._ptt_hold_time and self._last_above_threshold > 0:
                return b'\x00' * cb, True  # silence but keep PTT keyed
            self.audio_level = 0
            return None, False  # Hold expired: let PTT release

        # Above threshold — update hold timer
        self._last_above_threshold = now

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, True   # Above threshold: route to radio TX and activate PTT

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "ANNIN: Disabled"
        elif self.client_connected:
            return f"ANNIN: Connected ({self.audio_level}%)"
        else:
            return "ANNIN: Waiting"

    def cleanup(self):
        """Stop reader thread and close listen socket."""
        self._reader_running = False
        if self._listen_socket:
            try:
                self._listen_socket.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
            if self._reader_thread.is_alive():
                print(f"[ANNIN] Warning: reader thread did not stop within 2s")
        self._sub_buffer = b''


class WebMicSource(AudioSource):
    """Receives browser microphone audio via WebSocket and routes to radio TX.

    PTT is explicitly controlled by the user's button toggle — active for the
    entire duration of the WebSocket connection, not gated by audio level.
    """
    def __init__(self, config, gateway):
        super().__init__("WEBMIC", config)
        self.gateway = gateway
        self.priority = 0
        self.ptt_control = True
        self.volume = float(getattr(config, 'WEB_MIC_VOLUME', 25.0))
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.client_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono

    def setup_audio(self):
        return True  # WebSocket handler manages connections

    def push_audio(self, pcm_bytes):
        """Called by WebSocket handler to push raw PCM into the queue."""
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

        cb = self._chunk_bytes

        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
            except _queue_mod.Empty:
                # No audio in queue but client is connected — send silence, keep PTT keyed
                return b'\x00' * cb, True

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering (for UI display only)
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        raw_level = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60))) if rms > 0 else 0
        self.audio_level = raw_level if raw_level > self.audio_level else int(self.audio_level * 0.7 + raw_level * 0.3)

        # Apply volume multiplier
        if self.volume != 1.0:
            arr = arr * self.volume
            raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

        return raw, True

    def is_active(self):
        return self.enabled and not self.muted and self.client_connected

    def get_status(self):
        if not self.enabled:
            return "WEBMIC: Disabled"
        elif self.client_connected:
            return f"WEBMIC: TX ({self.audio_level}%)"
        else:
            return "WEBMIC: Idle"

    def cleanup(self):
        self._sub_buffer = b''


class StreamOutputSource:
    """Stream audio output to named pipe for Darkice"""
    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        self.connected = False
        self.pipe = None
        
        # Try to open pipe if enabled
        if config.ENABLE_STREAM_OUTPUT:
            self.setup_stream()
    
    def setup_stream(self):
        """Open named pipe for Darkice"""
        import os
        
        try:
            pipe_path = '/tmp/darkice_audio'
            
            # Create pipe if it doesn't exist
            if not os.path.exists(pipe_path):
                os.mkfifo(pipe_path)
                os.chmod(pipe_path, 0o666)
                if self.gateway.config.VERBOSE_LOGGING:
                    print(f"  Created pipe: {pipe_path}")
            
            # Open pipe for writing (non-blocking)
            import fcntl
            self.pipe = open(pipe_path, 'wb', buffering=0)
            
            # Make non-blocking
            fd = self.pipe.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            self.connected = True
            
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"  ✓ Streaming via Darkice pipe")
                print(f"    Pipe: {pipe_path}")
                print(f"    Format: PCM 48kHz mono 16-bit")
                print(f"    Make sure Darkice is running:")
                print(f"      darkice -c /etc/darkice.cfg")
                
        except Exception as e:
            print(f"  ⚠ Darkice pipe setup failed: {e}")
            print(f"    Install: sudo apt-get install darkice")
            print(f"    Configure: /etc/darkice.cfg")
            print(f"    Start: darkice -c /etc/darkice.cfg")
            self.connected = False
    
    def send_audio(self, audio_data):
        """Send raw PCM audio to Darkice via pipe"""
        if not self.connected or not self.pipe:
            return
        
        try:
            self.pipe.write(audio_data)
                
        except BlockingIOError:
            # Pipe full - skip this chunk
            pass
        except BrokenPipeError:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Stream] Darkice pipe broken - Darkice may have stopped")
            self.connected = False
        except Exception as e:
            if self.gateway.config.VERBOSE_LOGGING:
                print(f"\n[Stream] Pipe error: {e}")
            self.connected = False
    
    def cleanup(self):
        """Close pipe"""
        if self.pipe:
            try:
                self.pipe.close()
            except:
                pass


class AudioMixer:
    """Mix audio from multiple sources with priority handling"""
    def __init__(self, config):
        self.config = config
        self.sources = []
        self.mixing_mode = 'simultaneous'  # Mix all sources together
        self.call_count = 0  # Debug counter
        
        # Per-source signal state for attack/release hysteresis
        self.signal_state = {}

        # Hysteresis + transition timing
        self.SIGNAL_ATTACK_TIME  = config.SIGNAL_ATTACK_TIME
        self.SIGNAL_RELEASE_TIME = config.SIGNAL_RELEASE_TIME
        self.SWITCH_PADDING_TIME = getattr(config, 'SWITCH_PADDING_TIME', 1.0)
        # After duck-in, block new duck-out for this many seconds.
        # Prevents AIOC tail blobs (VoIP trailing audio, echo, tones) from
        # immediately re-ducking the SDR and causing stutter.
        self.REDUCK_INHIBIT_TIME = getattr(config, 'REDUCK_INHIBIT_TIME', 2.0)

        # Duck state machines — one entry per duck-group (e.g. 'aioc_vs_sdrs')
        # Tracks current duck state and active padding windows
        self.duck_state = {}

        # Per-SDR hold timers: instant attack, held release for smooth audio
        self.sdr_hold_until = {}      # {sdr_name: float timestamp}
        self.sdr_prev_included = {}   # {sdr_name: bool} - for fade-in detection

        # SDR-to-SDR duck cooldown: once a lower-priority SDR unducks (starts
        # playing because the higher-priority SDR's signal hold expired), it
        # gets SDR_DUCK_COOLDOWN seconds of immunity before a higher-priority
        # SDR can re-duck it.  This prevents rapid toggling when a higher-
        # priority SDR has intermittent signal or noise near the threshold.
        # SIGNAL_RELEASE_TIME already provides the same hold in the other
        # direction (higher-priority keeps playing 3s after signal stops), so
        # this makes the behaviour symmetric.
        self.SDR_DUCK_COOLDOWN = getattr(config, 'SDR_DUCK_COOLDOWN', 3.0)
        self._sdr_duck_cooldown_until = {}   # {sdr_name: float} earliest time re-duck allowed
        self._sdr_prev_ducked_by_sdr = {}    # {sdr_name: bool} Rule 2 ducked last tick
        
    def add_source(self, source):
        """Add an audio source to the mixer"""
        self.sources.append(source)
        # Sort by priority (lower number = higher priority)
        self.sources.sort(key=lambda s: s.priority)
        
    def remove_source(self, name):
        """Remove a source by name"""
        self.sources = [s for s in self.sources if s.name != name]
    
    def get_source(self, name):
        """Get a source by name"""
        for source in self.sources:
            if source.name == name:
                return source
        return None
    
    def get_mixed_audio(self, chunk_size):
        """
        Get mixed audio from all enabled sources.
        Returns: (mixed_audio, ptt_required, active_sources, sdr1_was_ducked, sdr2_was_ducked, rx_audio, sdrsv_was_ducked, sdr_only_audio)
        """
        self.call_count += 1

        # Debug output every 100 calls
        if self.call_count % 100 == 0 and self.config.VERBOSE_LOGGING:
            print(f"\n[Mixer Debug] Called {self.call_count} times, {len(self.sources)} sources")
            for src in self.sources:
                print(f"  Source: {src.name}, enabled={src.enabled}, priority={src.priority}")

        if not self.sources:
            return None, False, [], False, False, None, False, None

        # Priority mode: only use highest priority active source
        if self.mixing_mode == 'priority':
            for source in self.sources:
                if not source.enabled:
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] Skipping {source.name} (disabled)")
                    continue

                # Try to get audio from this source
                audio, ptt = source.get_audio(chunk_size)

                # Debug what each source returns
                if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                    if audio is not None:
                        print(f"  [Mixer] {source.name} returned audio ({len(audio)} bytes), PTT={ptt}")
                    else:
                        print(f"  [Mixer] {source.name} returned None (no audio)")

                if audio is not None:
                    return audio, ptt and source.ptt_control, [source.name], False, False, None, False, None

            # No sources had audio
            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] No sources returned audio")
            return None, False, [], False, False, None, False, None

        # Simultaneous mode: mix all active sources
        elif self.mixing_mode == 'simultaneous':
            return self._mix_simultaneous(chunk_size)

        # Duck mode: reduce volume of lower priority when higher priority active
        elif self.mixing_mode == 'duck':
            return self._mix_with_ducking(chunk_size)

        return None, False, [], False, False, None, False, None
    
    def _mix_simultaneous(self, chunk_size):
        """Mix all active sources together with SDR priority-based ducking"""
        mixed_audio = None
        ptt_required = False
        active_sources = []
        ptt_audio = None      # Separate PTT audio
        non_ptt_audio = None  # Non-PTT, non-SDR audio (Radio RX etc)
        sdr_sources = {}      # Dictionary of SDR sources: name -> (audio, source_obj)

        # Phase 1: Non-SDR sources (Radio, FilePlayback, etc.)
        # Get their audio first so we can compute the duck state before
        # touching SDR ring buffers.
        for source in self.sources:
            if source.name.startswith("SDR"):
                continue
            if not source.enabled:
                if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                    print(f"  [Mixer-Simultaneous] Skipping {source.name} (disabled)")
                continue

            audio, ptt = source.get_audio(chunk_size)

            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                if audio is not None:
                    print(f"  [Mixer-Simultaneous] {source.name} returned audio ({len(audio)} bytes), PTT={ptt}")
                else:
                    print(f"  [Mixer-Simultaneous] {source.name} returned None")

            if audio is None:
                continue

            active_sources.append(source.name)

            # Separate PTT and non-PTT sources
            if ptt and source.ptt_control:
                ptt_required = True
                if ptt_audio is None:
                    ptt_audio = audio
                else:
                    ptt_audio = self._mix_audio_streams(ptt_audio, audio, 0.5)
            else:
                if non_ptt_audio is None:
                    non_ptt_audio = audio
                else:
                    non_ptt_audio = self._mix_audio_streams(non_ptt_audio, audio, 0.5)

        # Collect SDR source names for duck state machine (audio fetched in Phase 2)
        _sdr_source_names = [s.name for s in self.sources if s.name.startswith("SDR") and s.enabled]

        # --- SDR priority-based ducking decision ---
        # AIOC audio (Radio RX) and PTT audio always take priority over all SDRs
        # Between SDRs: lower sdr_priority number = higher priority (ducks others)
        # BUT: Only duck if there's actual audio signal (not just silence/zeros)
        # Uses hysteresis to prevent rapid on/off switching (stuttering)
        
        import time
        current_time = time.time()

        # Capture configurable threshold for the nested function
        _sdr_signal_threshold = getattr(self.config, 'SDR_SIGNAL_THRESHOLD', -60.0)

        # Helper function to check if audio has actual signal (instantaneous)
        def check_signal_instant(audio_data):
            """Check if audio contains actual signal above noise floor (instant check, no hysteresis)"""
            if not audio_data:
                return False
            try:
                arr = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                if len(arr) == 0:
                    return False
                rms = float(np.sqrt(np.mean(arr * arr)))
                if rms > 0:
                    db = 20 * _math_mod.log10(rms / 32767.0)
                    return db > _sdr_signal_threshold
                return False
            except Exception:
                return False

        # Helper function with hysteresis for stable signal detection
        def has_actual_audio(audio_data, source_name):
            """
            Check if audio has actual signal with attack/release hysteresis.

            Attack: signal must be CONTINUOUSLY present for SIGNAL_ATTACK_TIME before
                    a switch is allowed.  Any chunk of silence resets the attack timer,
                    so brief transients never trigger a source switch.

            Release: once active, the source must be continuously silent for
                     SIGNAL_RELEASE_TIME before it is declared inactive again.
            """
            if source_name not in self.signal_state:
                self.signal_state[source_name] = {
                    'has_signal': False,
                    'signal_continuous_start': 0.0,  # start of current unbroken signal run
                    'last_signal_time': 0.0,
                    'last_silence_time': current_time,
                }

            state = self.signal_state[source_name]
            signal_present_now = check_signal_instant(audio_data)

            if signal_present_now:
                state['last_signal_time'] = current_time
                if state['signal_continuous_start'] == 0.0:
                    # First chunk of a new continuous signal run — start the attack timer
                    state['signal_continuous_start'] = current_time
            else:
                state['last_silence_time'] = current_time
                # Any silence breaks continuity — reset the attack timer
                state['signal_continuous_start'] = 0.0

            if not state['has_signal']:
                # Inactive — fire attack only when signal has been unbroken for ATTACK_TIME
                if state['signal_continuous_start'] > 0.0:
                    continuous_duration = current_time - state['signal_continuous_start']
                    if continuous_duration >= self.SIGNAL_ATTACK_TIME:
                        state['has_signal'] = True
                        if self.config.VERBOSE_LOGGING:
                            print(f"  [Mixer] {source_name} ACTIVATED "
                                  f"(continuous signal for {continuous_duration:.2f}s)")
            else:
                # Active — release only after RELEASE_TIME of continuous silence
                time_since_signal = current_time - state['last_signal_time']
                if time_since_signal >= self.SIGNAL_RELEASE_TIME:
                    state['has_signal'] = False
                    if self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {source_name} RELEASED "
                              f"(silent for {time_since_signal:.2f}s)")

            return state['has_signal']
        
        other_audio_active = (ptt_audio is not None) or (non_ptt_audio is not None)

        # Trace state tracking
        _hold_fired = False
        _radio_has_signal = False
        _sdr_trace = {}

        # Check if other_audio actually has signal (not just zeros) with hysteresis.
        # PTT audio (file playback) is deterministic — when FilePlaybackSource returns data
        # it IS playing.  Applying attack hysteresis to it would delay SDR ducking AND would
        # trigger a duck-out transition that inserts SWITCH_PADDING_TIME of silence, cutting
        # the start of every announcement and dropping PTT.
        # Only apply hysteresis to non-PTT radio RX to suppress noise/squelch-tail transients.
        if other_audio_active:
            ptt_is_active = ptt_audio is not None  # Deterministic: treat as active immediately
            non_ptt_has_signal = has_actual_audio(non_ptt_audio, "Radio") if non_ptt_audio else False
            other_audio_active = ptt_is_active or non_ptt_has_signal
            _radio_has_signal = non_ptt_has_signal

            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                if non_ptt_audio and not non_ptt_has_signal:
                    print(f"  [Mixer] Non-PTT audio present but only silence - not ducking SDRs")

        # --- Duck state machine with transition padding ---
        # Manages the AIOC/Radio/PTT vs SDR duck relationship.
        # When a transition occurs (ducking starts or stops), SWITCH_PADDING_TIME
        # seconds of silence are inserted so the changeover is never abrupt:
        #   duck-out: both SDR and radio are silenced → then radio takes over
        #   duck-in:  SDR resumes immediately (fade-in handles onset click)
        ds = self.duck_state.setdefault('aioc_vs_sdrs', {
            'is_ducked': False,
            'prev_signal': False,
            'padding_end_time': 0.0,
            'transition_type': None,   # 'out' = duck starting, 'in' = duck ending
            '_radio_last_audio_time': 0.0,
            '_aioc_last_blob_time': 0.0,  # monotonic time of last tick with actual AIOC audio
            '_duck_in_time': 0.0,         # monotonic time of last duck-in (is_ducked→False)
        })

        # Track when Radio/PTT last had audio.  AIOC delivers 200ms blobs with
        # brief gaps between them — without a hold, each gap triggers a spurious
        # duck-in/duck-out transition cycle (2 × SWITCH_PADDING_TIME of silence).
        if other_audio_active:
            ds['_radio_last_audio_time'] = current_time
        elif ds.get('is_ducked', False):
            # Radio was ducking SDRs — hold it stable through AIOC blob gaps.
            # 1000ms covers two full blob periods (2 × 400ms) with margin.
            # AIOC blob gaps can reach 800-850ms; 500ms was too short and
            # caused spurious duck-in/duck-out transitions (SDR breakthrough).
            if current_time - ds.get('_radio_last_audio_time', 0.0) < 1.0:
                other_audio_active = True
                _hold_fired = True

        prev_signal = ds['prev_signal']
        ds['prev_signal'] = other_audio_active

        _reduck_inhibit = (current_time - ds.get('_duck_in_time', 0.0)) < self.REDUCK_INHIBIT_TIME
        if not ds['is_ducked'] and other_audio_active and not prev_signal and not _reduck_inhibit:
            # Transition: other audio just became active → start ducking SDRs.
            # _reduck_inhibit blocks re-ducking for REDUCK_INHIBIT_TIME after the
            # last duck-in: AIOC tail blobs (VoIP trailing audio, echo, system
            # tones) during that window are ignored, preventing them from starting
            # a new 1s duck cycle and causing post-duck stutter.
            # Record whether SDR had actual signal now so we know whether the
            # transition-silence is needed (SDR→radio handoff) or not (radio-only).
            ds['is_ducked'] = True
            ds['padding_end_time'] = current_time + self.SWITCH_PADDING_TIME
            ds['transition_type'] = 'out'
            # Only count SDR as "active" if it had genuine signal recently
            # (hold timer still running).  SDR included only via sdr_is_sole_source
            # with no real signal doesn't warrant transition silence — there's
            # nothing audible to "clean break" from.
            ds['sdr_active_at_transition'] = any(
                self.sdr_prev_included.get(name, False)
                and current_time < self.sdr_hold_until.get(name, 0.0)
                for name in _sdr_source_names
            )
            if self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] SDR duck-OUT: {self.SWITCH_PADDING_TIME:.2f}s transition silence "
                      f"(SDR active: {ds['sdr_active_at_transition']})")
        elif ds['is_ducked'] and not other_audio_active and prev_signal:
            # Transition: other audio just went inactive → stop ducking SDRs.
            # No padding on duck-in: SDR resumes immediately with fade-in
            # (onset fade at line 1882 prevents click).  Duck-in padding would
            # add a needless 1s gap between Radio stopping and SDR resuming.
            ds['is_ducked'] = False
            ds['padding_end_time'] = 0.0
            ds['transition_type'] = None
            ds['_duck_in_time'] = current_time  # arm re-duck inhibit timer
            # Reset sdr_prev_included so the onset fade-in always fires when
            # SDR resumes after a duck.  Without this, sdr_prev_included stays
            # True from before the duck and the first SDR chunk after duck-in
            # jumps to full volume with no fade-in → audible click.
            for _n in _sdr_source_names:
                self.sdr_prev_included[_n] = False
            if self.config.VERBOSE_LOGGING:
                print(f"  [Mixer] SDR duck-IN: immediate (no padding)")

        in_padding = current_time < ds['padding_end_time']
        # SDR suppression: suppress whenever is_ducked or in_padding.
        # Tying this to is_ducked (not to whether non_ptt_audio is None) is
        # critical to prevent post-duck flapping stutter:
        #
        #   Old logic: aioc_ducks_sdrs = (is_ducked or padding) and non_ptt_audio is not None
        #   Problem:   AIOC stops → non_ptt_audio=None → aioc_ducks_sdrs=False immediately.
        #              SDR plays.  AIOC sends a tail blob 150ms later → aioc_ducks_sdrs=True.
        #              SDR abruptly cut off.  Repeat for each tail → choppy stutter.
        #
        #   New logic: aioc_ducks_sdrs = is_ducked or in_padding
        #   Effect:    SDR stays suppressed through the full 1s hold window after AIOC
        #              stops.  AIOC tail blobs reset _radio_last_audio_time, extending the
        #              hold — SDR never starts until AIOC is truly done and 1s has passed.
        #              Clean single release, no flapping.
        #
        # Keep _aioc_last_blob_time/_aioc_blob_recent for the trace nptt_none flag only.
        if non_ptt_audio is not None:
            ds['_aioc_last_blob_time'] = current_time
        _aioc_blob_recent = (current_time - ds['_aioc_last_blob_time']) < 0.15
        _aioc_audio_none = non_ptt_audio is None  # captured for trace: True = AIOC gap this tick

        aioc_ducks_sdrs = ds['is_ducked'] or in_padding
        # During duck-out padding: silence ALL output so the switch is a clean break
        in_transition_out = in_padding and ds['transition_type'] == 'out'

        # Phase 2: Fetch SDR audio.  Always call get_audio() to drain the
        # ring buffer — ducked audio is stale and must be discarded so SDR
        # starts with fresh/current audio when the duck releases.
        for source in self.sources:
            if not source.name.startswith("SDR"):
                continue
            if not source.enabled:
                continue
            audio, _ptt = source.get_audio(chunk_size)
            sdr_duck = source.duck if hasattr(source, 'duck') else True
            if aioc_ducks_sdrs and sdr_duck:
                # Ducked — discard audio, pass None so ducking logic tracks state
                sdr_sources[source.name] = (None, source)
            else:
                if audio is not None:
                    active_sources.append(source.name)
                sdr_sources[source.name] = (audio, source)

        sdr1_was_ducked = False
        sdr2_was_ducked = False
        sdrsv_was_ducked = False

        # First pass: determine which SDRs should be ducked
        sdrs_to_include = {}  # SDRs that will actually be mixed

        # Sort SDR sources by priority (lower number = higher priority)
        sorted_sdrs = sorted(
            sdr_sources.items(),
            key=lambda x: getattr(x[1][1], 'sdr_priority', 99)
        )

        # Pre-scan: check which SDRs have instant signal this tick.
        # Used to refine sole_source: an SDR with no signal should not be
        # force-included when another SDR already has real audio, because
        # the no-signal SDR would just add loopback noise to the mix.
        _sdrs_with_signal = set()
        for _pre_name, (_pre_audio, _pre_src) in sorted_sdrs:
            if _pre_audio is not None and check_signal_instant(_pre_audio):
                _sdrs_with_signal.add(_pre_name)

        for sdr_name, (sdr_audio, sdr_source) in sorted_sdrs:
            sdr_duck = sdr_source.duck if hasattr(sdr_source, 'duck') else True
            sdr_priority = getattr(sdr_source, 'sdr_priority', 99)

            should_duck = False

            ducked_by_sdr = False  # Rule 2 specifically (not Rule 1)

            if sdr_duck:
                # Rule 1: AIOC/PTT/Radio audio ducks ALL SDRs (with padding on transitions)
                if aioc_ducks_sdrs:
                    should_duck = True
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} ducked by AIOC/Radio/PTT audio")
                else:
                    # Rule 2: Higher priority SDR (lower number) ducks lower priority SDRs
                    # Only duck if the higher-priority SDR has actual signal —
                    # not when it's included merely because it's the sole source type.
                    # Uses 'sig' which is hysteresis-based (requires SIGNAL_ATTACK_TIME
                    # seconds of continuous signal) so brief noise spikes from a higher-
                    # priority SDR don't immediately mute a lower-priority one.
                    # 'hold' is intentionally excluded here: it is for audio inclusion
                    # (fade-out) only, not for ducking decisions.
                    for other_name, (_, other_source_obj) in sorted_sdrs:
                        if other_name == sdr_name:
                            break  # only check sources processed before this one
                        other_priority = getattr(other_source_obj, 'sdr_priority', 99)
                        other_trace = _sdr_trace.get(other_name, {})
                        other_has_signal = other_trace.get('sig')  # hysteresis-based only
                        if other_priority < sdr_priority and other_has_signal:
                            ducked_by_sdr = True
                            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                                print(f"  [Mixer] {sdr_name} (priority {sdr_priority}) ducked by {other_name} (priority {other_priority})")
                            break

                    # Cooldown: after this SDR unducks from a Rule 2 duck, it gets
                    # SDR_DUCK_COOLDOWN seconds of immunity before it can be re-ducked.
                    # This prevents rapid toggling when the higher-priority SDR has
                    # intermittent signal near the threshold.
                    if ducked_by_sdr:
                        cooldown_until = self._sdr_duck_cooldown_until.get(sdr_name, 0.0)
                        if current_time < cooldown_until:
                            ducked_by_sdr = False  # cooldown active — keep playing

                    should_duck = ducked_by_sdr

            # Track Rule 2 transitions for cooldown timer
            prev_ducked_by_sdr = self._sdr_prev_ducked_by_sdr.get(sdr_name, False)
            if prev_ducked_by_sdr and not ducked_by_sdr and not aioc_ducks_sdrs:
                # Transition: was ducked by higher-priority SDR, now unducked.
                # Start cooldown — this SDR gets guaranteed play time.
                self._sdr_duck_cooldown_until[sdr_name] = current_time + self.SDR_DUCK_COOLDOWN
                if self.config.VERBOSE_LOGGING:
                    print(f"  [Mixer] {sdr_name} unduck cooldown started ({self.SDR_DUCK_COOLDOWN:.1f}s)")
            self._sdr_prev_ducked_by_sdr[sdr_name] = ducked_by_sdr

            # Track ducking state for status bar
            if should_duck:
                _sdr_trace[sdr_name] = {'ducked': True, 'inc': False, 'sig': False, 'hold': False, 'sole': False, 'fi': False, 'fo': False}
                if sdr_name == "SDR1":
                    sdr1_was_ducked = True
                elif sdr_name == "SDR2":
                    sdr2_was_ducked = True
                elif sdr_name == "SDRSV":
                    sdrsv_was_ducked = True
            else:
                # Instant attack + held release for SDR inclusion.
                #
                # The old has_actual_audio() approach used a 0.1s attack timer which
                # dropped the first 200ms chunk (one full AUDIO_CHUNK_SIZE period) and
                # then switched to full volume abruptly → missing audio + pop/click.
                #
                # New approach:
                #   - Include immediately on any detectable signal (no attack delay)
                #   - Hold inclusion for SIGNAL_RELEASE_TIME after signal stops so brief
                #     pauses don't cause dropouts and the tail fades away naturally
                #   - Apply a short linear fade-in at the moment of first inclusion to
                #     prevent the onset click when SDR activates after silence
                has_instant = check_signal_instant(sdr_audio)
                if has_instant:
                    self.sdr_hold_until[sdr_name] = current_time + self.SIGNAL_RELEASE_TIME
                hold_active = current_time < self.sdr_hold_until.get(sdr_name, 0.0)
                # has_sig_hyst: attack-hysteresis version used for SDR-to-SDR ducking
                # decisions (Rule 2).  Requires SIGNAL_ATTACK_TIME seconds of continuous
                # signal before firing so brief noise spikes don't immediately duck
                # lower-priority SDRs.  Release mirrors SIGNAL_RELEASE_TIME via the
                # has_actual_audio() state machine, so ducking lasts 3s after signal stops
                # (same as hold_active, but with the attack guard on the front end).
                has_sig_hyst = has_actual_audio(sdr_audio, sdr_name)
                # When SDR is the only source type (no radio RX or PTT audio),
                # force-include so we don't gate out the only audio available.
                # BUT: if this SDR has no signal and another SDR does, don't
                # force-include — it would just add loopback noise to the mix.
                no_aioc = non_ptt_audio is None and ptt_audio is None
                other_sdrs_have_signal = bool(_sdrs_with_signal - {sdr_name})
                sdr_is_sole_source = no_aioc and (has_instant or hold_active)
                include_sdr = has_instant or hold_active or sdr_is_sole_source
                _sdr_trace[sdr_name] = {'ducked': False, 'inc': include_sdr, 'sig': has_sig_hyst, 'inst': has_instant, 'hold': hold_active, 'sole': sdr_is_sole_source, 'fi': False, 'fo': False}

                if sdr_audio is None:
                    # No data this cycle (reader thread momentarily behind).
                    # Preserve sdr_prev_included so that when audio returns we
                    # resume cleanly: if it was True, the next chunk continues
                    # without a spurious fade-in click; if it was False, it stays
                    # False and the normal onset fade-in fires as expected.
                    # Clear sig so this SDR's stale hysteresis hold does not
                    # duck lower-priority SDRs while it has no audio to offer.
                    _sdr_trace[sdr_name]['sig'] = False
                    continue

                prev_included = self.sdr_prev_included.get(sdr_name, False)
                # Track fade-in/fade-out events for trace instrumentation.
                # fi=True → 10ms onset fade applied (first inclusion after silence/duck)
                # fo=True → fade-out applied (last frame before going silent)
                _sdr_trace[sdr_name]['fi'] = include_sdr and not prev_included
                _sdr_trace[sdr_name]['fo'] = not include_sdr and prev_included

                if include_sdr:
                    audio_to_include = sdr_audio
                    if not prev_included:
                        # Onset: fade-in from 0→1 over first 10ms (480 samples)
                        arr = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.float32)
                        fade_len = min(480, len(arr))
                        arr[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
                        audio_to_include = arr.astype(np.int16).tobytes()
                    self.sdr_prev_included[sdr_name] = True
                    sdrs_to_include[sdr_name] = (audio_to_include, sdr_source)
                    if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} included (instant={'yes' if has_instant else 'hold'})")
                elif prev_included:
                    # Transition frame: was included last chunk, not now.
                    # Apply fade-out so the cutoff is always smooth regardless of
                    # how much time elapsed since the last iteration (avoids the
                    # timing-window bug where a slow AIOC read skips the fade).
                    arr = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.float32)
                    arr *= np.linspace(1.0, 0.0, len(arr))
                    audio_to_include = arr.astype(np.int16).tobytes()
                    self.sdr_prev_included[sdr_name] = False
                    sdrs_to_include[sdr_name] = (audio_to_include, sdr_source)
                    if self.config.VERBOSE_LOGGING:
                        print(f"  [Mixer] {sdr_name} fade-out (hold expired)")
                else:
                    self.sdr_prev_included[sdr_name] = False
        
        # Second pass: actually mix the non-ducked SDRs.
        # Use sum-and-clip instead of crossfade: each SDR contributes at full
        # gain regardless of how many are active.  Crossfade (ratio=0.5) caused
        # a 6 dB step on SDR1 every time SDR2 entered or exited the mix.
        sdr_only_audio = None
        for sdr_name, (sdr_audio, sdr_source) in sdrs_to_include.items():
            # Build SDR-only mix for rebroadcast (before merging into non_ptt_audio)
            if sdr_only_audio is None:
                sdr_only_audio = sdr_audio
            else:
                s1 = np.frombuffer(sdr_only_audio, dtype=np.int16).astype(np.int32)
                s2 = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.int32)
                smin = min(len(s1), len(s2))
                sdr_only_audio = np.clip(
                    s1[:smin] + s2[:smin], -32768, 32767
                ).astype(np.int16).tobytes()

            if non_ptt_audio is None:
                non_ptt_audio = sdr_audio
            else:
                arr1 = np.frombuffer(non_ptt_audio, dtype=np.int16).astype(np.int32)
                arr2 = np.frombuffer(sdr_audio, dtype=np.int16).astype(np.int32)
                min_len = min(len(arr1), len(arr2))
                non_ptt_audio = np.clip(
                    arr1[:min_len] + arr2[:min_len], -32768, 32767
                ).astype(np.int16).tobytes()
        
        # Priority: PTT audio always wins (full volume, no mixing with radio)
        if ptt_audio is not None:
            mixed_audio = ptt_audio
            if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
                print(f"  [Mixer-Simultaneous] Using PTT audio at FULL VOLUME (not mixing with radio)")
        elif non_ptt_audio is not None:
            mixed_audio = non_ptt_audio

        # Duck-out transition: SDRs are already silenced by aioc_ducks_sdrs.
        # Do NOT silence mixed_audio here — that would throw away Radio audio
        # for the entire SWITCH_PADDING_TIME (1s), causing a silence gap every
        # time Radio returns after SDR was playing.

        # When PTT (file playback) wins the mix, non_ptt_audio (radio RX) is not
        # included in mixed_audio.  Carry it out separately so the transmit loop
        # can still forward it to Mumble — listeners hear the radio channel even
        # while an announcement is being transmitted.
        rx_audio = non_ptt_audio if ptt_required else None

        # Store trace state for audio_trace instrumentation
        self._last_trace_state = {
            'dk': ds['is_ducked'],
            'hold': _hold_fired,
            'pad': in_padding,
            'tOut': in_transition_out,
            'sdrAT': ds.get('sdr_active_at_transition', False),
            'oaa': other_audio_active,
            'radioSig': _radio_has_signal,
            'ducks': aioc_ducks_sdrs,
            'nptt_none': _aioc_audio_none,  # True = AIOC blob gap this tick
            'ri': _reduck_inhibit,          # True = re-duck blocked by inhibit timer
            'ptt': ptt_required,
            'sdrs': _sdr_trace,
        }

        if self.call_count % 100 == 1 and self.config.VERBOSE_LOGGING:
            print(f"  [Mixer-Simultaneous] Result: {len(active_sources)} active sources, PTT={ptt_required}")

        return mixed_audio, ptt_required, active_sources, sdr1_was_ducked, sdr2_was_ducked, rx_audio, sdrsv_was_ducked, sdr_only_audio
    
    def _mix_with_ducking(self, chunk_size):
        """Mix with ducking: reduce lower priority sources"""
        # Find highest priority active source
        high_priority_active = False
        for source in self.sources:
            if source.enabled:
                audio, _ = source.get_audio(chunk_size)
                if audio is not None:
                    high_priority_active = True
                    break
        
        # If high priority is active, duck the others
        mixed_audio = None
        ptt_required = False
        active_sources = []
        
        for i, source in enumerate(self.sources):
            if not source.enabled:
                continue
            
            audio, ptt = source.get_audio(chunk_size)
            if audio is None:
                continue
            
            active_sources.append(source.name)
            
            # Duck lower priority sources
            if i > 0 and high_priority_active:
                audio = self._apply_volume(audio, 0.3)  # 30% volume
            
            if ptt and source.ptt_control:
                ptt_required = True
            
            if mixed_audio is None:
                mixed_audio = audio
            else:
                mixed_audio = self._mix_audio_streams(mixed_audio, audio, 0.5)
        
        return mixed_audio, ptt_required, active_sources, False, False, None, False, None

    def _mix_audio_streams(self, audio1, audio2, ratio=0.5):
        """Mix two audio streams together"""
        arr1 = np.frombuffer(audio1, dtype=np.int16).astype(np.float32)
        arr2 = np.frombuffer(audio2, dtype=np.int16).astype(np.float32)

        # Ensure same length
        min_len = min(len(arr1), len(arr2))
        arr1 = arr1[:min_len]
        arr2 = arr2[:min_len]

        mixed = np.clip(arr1 * ratio + arr2 * (1.0 - ratio), -32768, 32767).astype(np.int16)
        return mixed.tobytes()
    
    def _apply_volume(self, audio, volume):
        """Apply volume multiplier to audio"""
        arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
        return np.clip(arr * volume, -32768, 32767).astype(np.int16).tobytes()
    
    def get_status(self):
        """Get status of all sources"""
        status = []
        for source in self.sources:
            status.append(source.get_status())
        return status


_MORSE_TABLE = {
    'A': '.-',   'B': '-...', 'C': '-.-.', 'D': '-..',  'E': '.',
    'F': '..-.', 'G': '--.',  'H': '....', 'I': '..',   'J': '.---',
    'K': '-.-',  'L': '.-..', 'M': '--',   'N': '-.',   'O': '---',
    'P': '.--.', 'Q': '--.-', 'R': '.-.',  'S': '...',  'T': '-',
    'U': '..-',  'V': '...-', 'W': '.--',  'X': '-..-', 'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', '/': '-..-.', '-': '-....-',
}


def generate_cw_pcm(text, wpm=15, freq=700, sample_rate=48000):
    """Return int16 numpy array of CW audio for text. Standard PARIS timing."""
    dit_n = int(sample_rate * 1.2 / wpm)
    t = np.arange(dit_n) / sample_rate
    dit_tone = (np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    dah_tone = np.tile(dit_tone, 3)
    dit_sil  = np.zeros(dit_n,     dtype=np.int16)
    char_sil = np.zeros(3 * dit_n, dtype=np.int16)
    word_sil = np.zeros(7 * dit_n, dtype=np.int16)

    chunks = []
    for wi, word in enumerate(text.upper().split()):
        if wi:
            chunks.append(word_sil)
        for ci, ch in enumerate(word):
            if ci:
                chunks.append(char_sil)
            pattern = _MORSE_TABLE.get(ch, '')
            if not pattern:
                print(f"[CW] Warning: skipping unknown character {ch!r}")
                continue
            for ei, el in enumerate(pattern):
                if ei:
                    chunks.append(dit_sil)
                chunks.append(dit_tone if el == '.' else dah_tone)

    return np.concatenate(chunks) if chunks else np.zeros(dit_n, dtype=np.int16)

