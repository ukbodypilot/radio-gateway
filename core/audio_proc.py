"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import collections
import json as json_mod
import math as _math_mod
import os
import queue as _queue_mod
import re
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
from audio_util import pcm_level, pcm_rms, rms_to_level, pcm_db


class _AudioProcMixin:
    def calculate_audio_level(self, pcm_data):
        """Calculate RMS audio level from PCM data (0-100 scale)"""
        try:
            if not pcm_data:
                return 0
            return rms_to_level(pcm_rms(pcm_data))
        except Exception:
            return 0

    def _update_sv_level(self, pcm_data):
        """Update sv_audio_level from PCM data sent to remote client."""
        self.sv_audio_level = pcm_level(pcm_data, self.sv_audio_level)

    def apply_highpass_filter(self, pcm_data):
        """Apply high-pass filter to remove low-frequency rumble"""
        try:
            import math
            from scipy.signal import lfilter, lfilter_zi

            samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
            if len(samples) == 0:
                return pcm_data

            # First-order IIR high-pass: H(z) = alpha*(1 - z^-1) / (1 - alpha*z^-1)
            cutoff = self.config.HIGHPASS_CUTOFF_FREQ
            sample_rate = self.config.AUDIO_RATE
            rc = 1.0 / (2.0 * math.pi * cutoff)
            dt = 1.0 / sample_rate
            alpha = rc / (rc + dt)

            b = np.array([alpha, -alpha], dtype=np.float64)
            a = np.array([1.0, -alpha], dtype=np.float64)

            # Initialize state on first call (zi shape: (1,))
            if self.highpass_state is None:
                self.highpass_state = lfilter_zi(b, a) * 0.0

            filtered, self.highpass_state = lfilter(b, a, samples, zi=self.highpass_state)
            return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()

        except Exception:
            return pcm_data
    
    def apply_noise_gate(self, pcm_data):
        """Apply noise gate with attack/release to reduce background hiss"""
        try:
            import array
            import math
            
            samples = array.array('h', pcm_data)
            if len(samples) == 0:
                return pcm_data
            
            # Convert threshold from dB to linear
            threshold_db = self.config.NOISE_GATE_THRESHOLD
            threshold = 32767.0 * pow(10.0, threshold_db / 20.0)
            
            # Attack and release times in samples
            attack_samples = (self.config.NOISE_GATE_ATTACK / 1000.0) * self.config.AUDIO_RATE
            release_samples = (self.config.NOISE_GATE_RELEASE / 1000.0) * self.config.AUDIO_RATE
            
            # Attack and release coefficients
            attack_coef = 1.0 / attack_samples if attack_samples > 0 else 1.0
            release_coef = 1.0 / release_samples if release_samples > 0 else 0.1
            
            # Apply gate with envelope follower
            gated = []
            for sample in samples:
                # Calculate signal level (absolute value)
                level = abs(sample)
                
                # Update envelope with attack/release
                if level > self.gate_envelope:
                    self.gate_envelope += (level - self.gate_envelope) * attack_coef
                else:
                    self.gate_envelope += (level - self.gate_envelope) * release_coef
                
                # Calculate gain based on envelope vs threshold
                if self.gate_envelope > threshold:
                    gain = 1.0
                else:
                    # Smooth transition below threshold
                    ratio = self.gate_envelope / threshold if threshold > 0 else 0
                    gain = ratio * ratio  # Quadratic for smooth fade
                
                gated.append(int(sample * gain))
            
            return array.array('h', gated).tobytes()
            
        except Exception:
            return pcm_data
    
    def _sync_radio_processor(self):
        """Sync global config flags into the radio AudioProcessor instance."""
        p = self.radio_processor
        p.enable_hpf = self.config.ENABLE_HIGHPASS_FILTER
        p.hpf_cutoff = self.config.HIGHPASS_CUTOFF_FREQ
        p.enable_lpf = self.config.ENABLE_LOWPASS_FILTER
        p.lpf_cutoff = self.config.LOWPASS_CUTOFF_FREQ
        p.enable_notch = self.config.ENABLE_NOTCH_FILTER
        p.notch_freq = self.config.NOTCH_FREQ
        p.notch_q = self.config.NOTCH_Q
        p.enable_noise_gate = self.config.ENABLE_NOISE_GATE
        p.gate_threshold = self.config.NOISE_GATE_THRESHOLD
        p.gate_attack = self.config.NOISE_GATE_ATTACK
        p.gate_release = self.config.NOISE_GATE_RELEASE

    def _sync_sdr_plugin_processors(self):
        """Sync SDR processing config into the SDRPlugin's processor instances."""
        if self.sdr_plugin:
            from plugins.sdr import SDRPlugin
            SDRPlugin._sync_processor(self.sdr_plugin._processor1, self.config)
            SDRPlugin._sync_processor(self.sdr_plugin._processor2, self.config)

    # D75 processing is handled by the link endpoint — no local sync needed

    # KV4P processor sync: gone. The endpoint plugin reads its own
    # processing flags from the [kv4p.<instance>] section at startup.

    def process_audio_for_mumble(self, pcm_data):
        """Apply all enabled audio processing to clean up radio audio before sending to Mumble.
        Now delegates to the radio AudioProcessor instance.
        """
        # Keep legacy state in sync (old code may read self.gate_envelope etc.)
        self._sync_radio_processor()
        result = self.radio_processor.process(pcm_data)
        self.gate_envelope = self.radio_processor.gate_envelope
        self.highpass_state = self.radio_processor.highpass_state
        return result

    def _load_link_settings(self):
        """Load saved per-endpoint settings (rx_muted, tx_muted) from JSON."""
        try:
            with open(self._link_settings_path) as f:
                import json as _json
                self.link_endpoint_settings = _json.load(f)
        except (FileNotFoundError, ValueError):
            self.link_endpoint_settings = {}

    def _save_link_settings(self):
        """Persist per-endpoint settings to JSON."""
        import json as _json
        try:
            os.makedirs(os.path.dirname(self._link_settings_path), exist_ok=True)
            from atomic_json import save_json
            save_json(self._link_settings_path, self.link_endpoint_settings)
        except Exception as e:
            print(f"  [Link] Failed to save settings: {e}")

    def _load_source_gains(self):
        """Load saved source/sink gain overrides from JSON."""
        try:
            with open(self._source_gains_path) as f:
                import json as _json
                data = _json.load(f)
                self._source_gains = data.get('sources', {})
                # Restore sink gains too
                for sid, val in data.get('sinks', {}).items():
                    self._sink_gains[sid] = val / 100.0
        except (FileNotFoundError, ValueError):
            self._source_gains = {}

    def _save_source_gains(self):
        """Persist source/sink gain overrides to JSON."""
        import json as _json
        try:
            os.makedirs(os.path.dirname(self._source_gains_path), exist_ok=True)
            data = {'sources': self._source_gains,
                    'sinks': {k: int(v * 100) for k, v in self._sink_gains.items()}}
            with open(self._source_gains_path, 'w') as f:
                _json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  [Gains] Failed to save: {e}")

    def _apply_source_gains(self):
        """Apply saved gain overrides to source objects. Called after all sources init."""
        _bm = getattr(self, 'bus_manager', None)
        for source_id, gain_pct in self._source_gains.items():
            plugin = _bm._get_source(source_id) if _bm else None
            if plugin:
                _is_tx = source_id.endswith('_tx')
                if _is_tx and hasattr(plugin, 'tx_audio_boost'):
                    plugin.tx_audio_boost = gain_pct / 100.0
                else:
                    plugin.audio_boost = gain_pct / 100.0
                if getattr(self.config, 'VERBOSE_LOGGING', False):
                    print(f"  [Gains] Restored {source_id} = {gain_pct}%")

    # process_audio_for_sdr removed — SDRPlugin handles processing internally

    def check_vad(self, pcm_data):
        """Voice Activity Detection - determines if audio should be sent to Mumble"""
        if not self.config.ENABLE_VAD:
            return True  # VAD disabled, always send

        try:
            if not pcm_data:
                return False

            db_level = pcm_db(pcm_data)

            # Attack and release coefficients (samples per second)
            chunks_per_second = self.config.AUDIO_RATE / self.config.AUDIO_CHUNK_SIZE
            attack_coef = 1.0 / (self.config.VAD_ATTACK * chunks_per_second)
            release_coef = 1.0 / (self.config.VAD_RELEASE * chunks_per_second)
            
            # Update envelope follower
            if db_level > self.vad_envelope:
                # Attack: fast rise
                self.vad_envelope += (db_level - self.vad_envelope) * min(1.0, attack_coef)
            else:
                # Release: slow decay
                self.vad_envelope += (db_level - self.vad_envelope) * min(1.0, release_coef)
            
            current_time = time.time()
            
            # Check if signal exceeds threshold
            if self.vad_envelope > self.config.VAD_THRESHOLD:
                if not self.vad_active:
                    # VAD opening
                    self.vad_active = True
                    self.vad_open_time = current_time
                    self.vad_transmissions += 1
                return True
            else:
                # Below threshold
                if self.vad_active:
                    # Check minimum duration
                    open_duration = current_time - self.vad_open_time  # seconds
                    if open_duration < self.config.VAD_MIN_DURATION:
                        # Haven't met minimum duration yet, stay open
                        return True
                    
                    # Check release time
                    if self.vad_close_time == 0:
                        self.vad_close_time = current_time
                    
                    release_duration = current_time - self.vad_close_time  # seconds
                    if release_duration < self.config.VAD_RELEASE:
                        # Still in release tail
                        return True
                    else:
                        # Release complete, close VAD
                        self.vad_active = False
                        self.vad_close_time = 0
                        return False
                else:
                    # VAD is closed and staying closed
                    self.vad_close_time = 0
                    return False
                    
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"\n[VAD] Error: {e}")
            return True  # On error, allow transmission
    
    def check_vox(self, pcm_data):
        """Check if audio level exceeds VOX threshold (indicates radio is receiving)"""
        if not self.config.ENABLE_VOX:
            return True  # VOX disabled, always transmit
        
        try:
            if not pcm_data:
                return False

            db = pcm_db(pcm_data)

            # Attack and release timing
            attack_time = self.config.VOX_ATTACK_TIME / 1000.0  # ms to seconds
            release_time = self.config.VOX_RELEASE_TIME / 1000.0
            
            # Update VOX level with attack/release envelope
            if db > self.vox_level:
                # Attack: fast rise
                self.vox_level = db
            else:
                # Release: slow decay
                # Calculate decay rate to reach threshold in release_time
                decay_rate = abs(self.config.VOX_THRESHOLD - db) / (release_time * (self.config.AUDIO_RATE / self.config.AUDIO_CHUNK_SIZE))
                self.vox_level = max(db, self.vox_level - decay_rate)
            
            # Check if above threshold
            if self.vox_level > self.config.VOX_THRESHOLD:
                if not self.vox_active:
                    if self.config.VERBOSE_LOGGING:
                        print(f"\n[VOX] Radio receiving (level: {self.vox_level:.1f} dB)")
                self.vox_active = True
                self.last_vox_active_time = time.time()
                return True
            else:
                # Check if we're still in release period
                time_since_active = time.time() - self.last_vox_active_time
                if time_since_active < release_time:
                    return True  # Still in tail
                else:
                    if self.vox_active:
                        if self.config.VERBOSE_LOGGING:
                            print(f"\n[VOX] Radio silent (level: {self.vox_level:.1f} dB)")
                    self.vox_active = False
                    return False
                    
        except Exception:
            return True  # On error, allow transmission
        
