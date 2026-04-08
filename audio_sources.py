#!/usr/bin/env python3
"""Audio source and mixer classes for radio-gateway."""

import sys
import os
import time
import signal
import threading
import threading as _thr
import subprocess
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


class FilePlaybackSource(AudioSource):
    """Audio file playback source"""
    def __init__(self, config, gateway):
        super().__init__("FilePlayback", config)
        self.gateway = gateway
        self.priority = 0  # HIGHEST priority - announcements interrupt radio
        self.ptt_control = True  # File playback triggers PTT
        self.volume = getattr(config, 'PLAYBACK_VOLUME', 4.0)
        
        self.audio_level = 0      # Output level for routing display

        # Playback state
        self.current_file = None
        self.file_data = None
        self.file_position = 0
        self.playlist = []  # Queue of files to play
        self._play_seq = 0  # Sequence counter — each button press gets a unique ID
        import threading as _th
        self._play_lock = _th.Lock()  # Serializes stop+decode+queue
        self._loop_active = False  # Test loop mode
        
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
    
    def toggle_test_loop(self):
        """Toggle test loop — plays loop.mp3/loop.wav from audio dir on repeat."""
        import os
        if self._loop_active:
            # Stop loop
            self._loop_active = False
            self.stop_playback()
            print("[Playback] Test loop stopped")
            return {'ok': True, 'looping': False}
        # Find loop file
        audio_dir = self.announcement_directory
        for name in ('loop.mp3', 'loop.wav', 'loop.ogg'):
            path = os.path.join(audio_dir, name)
            if os.path.exists(path):
                self._loop_active = True
                self.queue_file(path)
                print(f"[Playback] Test loop started: {name}")
                return {'ok': True, 'looping': True, 'file': name}
        return {'ok': False, 'error': 'No loop.mp3/loop.wav found in audio/'}

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
        self._loop_active = False
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
        """Check if it's time for a periodic announcement.

        IMPORTANT: This runs inside get_audio() on the BusManager tick thread.
        We must NOT call queue_file() here because _decode_file() does disk I/O
        and audio decoding that blocks for 150-450ms, stalling all buses.
        Instead we use the pre-decoded cache (_station_id_pcm).
        """
        # Use auto-detected station_id file (key 0)
        if self.announcement_interval <= 0 or not self.file_status['0']['exists']:
            return

        current_time = time.time()
        if self.last_announcement_time == 0:
            self.last_announcement_time = current_time
            # Pre-decode station ID so we never decode in the audio thread
            self._ensure_station_id_cached()
            return

        # Check if enough time has passed
        elapsed = current_time - self.last_announcement_time
        if elapsed >= self.announcement_interval:
            # Check if radio is idle
            if not self.gateway.vad_active:
                station_id_path = self.file_status['0']['path']
                if station_id_path:
                    # Use cached PCM — never decode in audio thread
                    pcm = self._get_station_id_cached(station_id_path)
                    if pcm is not None:
                        self.playlist.append((station_id_path, pcm))
                    self.last_announcement_time = current_time
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"\n[Playback] Periodic station ID triggered (every {self.announcement_interval}s)")

    def _ensure_station_id_cached(self):
        """Pre-decode station ID file so periodic announcements never block."""
        path = self.file_status.get('0', {}).get('path')
        if path and not hasattr(self, '_station_id_pcm'):
            pcm = self._decode_file(path)
            if pcm:
                self._station_id_pcm = pcm
                self._station_id_path = path

    def _get_station_id_cached(self, path):
        """Return cached station ID PCM, re-decoding only if path changed."""
        if hasattr(self, '_station_id_pcm') and getattr(self, '_station_id_path', '') == path:
            return self._station_id_pcm
        # Path changed — decode in background (return None this tick, decode for next)
        import threading
        def _bg_decode():
            pcm = self._decode_file(path)
            if pcm:
                self._station_id_pcm = pcm
                self._station_id_path = path
        threading.Thread(target=_bg_decode, daemon=True).start()
        return None
    
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
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Calculate chunk size in bytes (16-bit = 2 bytes per sample)
        chunk_bytes = chunk_size * self.config.AUDIO_CHANNELS * 2

        # During the PTT announcement delay the radio is keying up.  Return silence
        # without advancing the file position so no audio is lost.
        if getattr(self.gateway, 'announcement_delay_active', False):
            return b'\x00' * chunk_bytes, True

        # Check if we have enough data left
        if self.file_position >= len(self.file_data):
            # Loop mode: rewind and continue
            if self._loop_active:
                self.file_position = 0
                return self.file_data[:chunk_bytes].ljust(chunk_bytes, b'\x00'), False
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

        # Level metering for routing display
        try:
            _arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            _rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
            if _rms > 0:
                _level = max(0, min(100, (20 * _math_mod.log10(_rms / 32767.0) + 60) * (100 / 60)))
            else:
                _level = 0
            if _level > self.audio_level:
                self.audio_level = int(_level)
            else:
                self.audio_level = int(self.audio_level * 0.7 + _level * 0.3)
        except Exception:
            pass

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

    def setup_audio(self, port_override=None):
        """Bind listen socket and start the reader/accept thread."""
        import socket
        bind_host = '0.0.0.0'
        port = int(port_override) if port_override else int(self.config.REMOTE_AUDIO_PORT)
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

                    # Track level in reader thread (works even when not on a bus)
                    try:
                        arr = np.frombuffer(payload, dtype=np.int16).astype(np.float32)
                        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
                        if rms > 0:
                            _lv = max(0, min(100, (20.0 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))
                        else:
                            _lv = 0
                        if _lv > self.audio_level:
                            self.audio_level = int(_lv)
                        else:
                            self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
                    except Exception:
                        pass

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




class LinkAudioSource(AudioSource):
    """Audio source for Gateway Link — receives duplex audio from a remote endpoint.

    Similar to RemoteAudioSource but fed by GatewayLinkServer's frame dispatch.
    The server calls push_audio() when AUDIO frames arrive from the endpoint.
    """

    def __init__(self, config, gateway, endpoint_name="default"):
        super().__init__(f"LINK:{endpoint_name}", config)
        self.gateway = gateway
        self.endpoint_name = endpoint_name
        self.priority = int(getattr(config, 'LINK_AUDIO_PRIORITY', 3))
        self.sdr_priority = self.priority
        self.ptt_control = False
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = getattr(config, 'LINK_AUDIO_DUCK', False)
        self.tx_audio_boost = 1.0     # separate TX gain for put_audio path
        self.tx_audio_level = 0       # TX level 0-100 (updated in put_audio)
        self.audio_boost = float(getattr(config, 'LINK_AUDIO_BOOST', 1.0))
        self.display_gain = float(getattr(config, 'LINK_AUDIO_DISPLAY_GAIN', 1.0))
        self.server_connected = False
        self.muted = False
        self.audio_level = 0
        self._chunk_bytes = int(getattr(config, 'AUDIO_RATE', 48000)) * 2 * int(getattr(config, 'AUDIO_CHANNELS', 1)) // 20  # 50ms
        self._chunk_queue = _queue_mod.deque(maxlen=16)
        self._sub_buffer = b''
        self._link_server = None  # Set by gateway_core after init

    def setup_audio(self):
        return True

    def push_audio(self, pcm):
        """Called by GatewayLinkServer reader thread when AUDIO frame arrives."""
        self._chunk_queue.append(pcm)
        # Track level from incoming audio — shows on routing page even when unrouted
        try:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                _lv = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                _lv = 0
            if _lv > self.audio_level:
                self.audio_level = int(_lv)
            else:
                self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
        except Exception:
            pass

    def get_audio(self, chunk_size):
        if not self.enabled or self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False
        if not self.server_connected:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        cb = self._chunk_bytes
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.popleft()
                self._sub_buffer += blob
            except IndexError:
                self.audio_level = max(0, int(self.audio_level * 0.7))
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Level metering — no VAD gate here, bus handles that
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        if rms > 0:
            raw_level = int(max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60))))
            display_level = min(100, int(raw_level * self.display_gain))
            if display_level > self.audio_level:
                self.audio_level = display_level
            else:
                self.audio_level = int(self.audio_level * 0.7 + display_level * 0.3)
        else:
            self.audio_level = max(0, int(self.audio_level * 0.7))

        # Audio boost
        if self.audio_boost != 1.0:
            arr = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16)
            raw = arr.tobytes()

        return raw, False

    def write_tx_audio(self, pcm):
        """Send gateway audio to the remote endpoint."""
        if self._link_server and self._link_server.connected:
            try:
                self._link_server.send_audio(pcm)
            except Exception:
                pass

    def put_audio(self, pcm):
        """Send TX audio to the remote endpoint (SoloBus radio interface)."""
        if self.gateway and self.gateway.link_server:
            try:
                if self.tx_audio_boost != 1.0:
                    _arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    pcm = np.clip(_arr * self.tx_audio_boost, -32768, 32767).astype(np.int16).tobytes()
                self.gateway.link_server.send_audio_to(self.endpoint_name, pcm)
                # TX level metering
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
                if rms > 0:
                    level = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))
                else:
                    level = 0
                if level > self.tx_audio_level:
                    self.tx_audio_level = int(level)
                else:
                    self.tx_audio_level = int(self.tx_audio_level * 0.7 + level * 0.3)
            except Exception:
                pass

    def execute(self, cmd):
        """Route commands to the remote endpoint via link server (SoloBus radio interface)."""
        _srv = self.gateway.link_server if self.gateway else None
        if _srv:
            try:
                print(f"  [LinkSrc:{self.endpoint_name}] execute: {cmd}")
                _srv.send_command_to(self.endpoint_name, cmd)
                return {"ok": True}
            except Exception as e:
                print(f"  [LinkSrc:{self.endpoint_name}] execute error: {e}")
                return {"ok": False, "error": str(e)}
        print(f"  [LinkSrc:{self.endpoint_name}] execute: no link server (gw={self.gateway is not None})")
        return {"ok": False, "error": "link server not available"}

    def ptt_on(self):
        self.execute({'cmd': 'ptt', 'state': True})

    def ptt_off(self):
        self.execute({'cmd': 'ptt', 'state': False})

    def is_active(self):
        return self.enabled and not self.muted and self.server_connected

    def get_status(self):
        if not self.enabled:
            return "LINK: disabled"
        return f"LINK: {'connected' if self.server_connected else 'disconnected'}"


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


class MumbleSource(AudioSource):
    """Receives Mumble RX audio and feeds it into the bus system.

    The Mumble sound_received_handler pushes PCM into a queue.
    get_audio() pulls from the queue with ptt_control=True so the
    bus knows to key the radio when Mumble audio is active.
    """
    def __init__(self, config, gateway=None):
        super().__init__("MUMBLE_RX", config)
        self.gateway = gateway
        self.priority = 0  # Highest — Mumble audio takes priority
        self.ptt_control = True
        self.volume = 1.0
        self.enabled = True
        self.muted = False
        self.audio_level = 0
        self.audio_boost = float(getattr(config, 'OUTPUT_VOLUME', 1.0))
        self.vad_threshold_db = float(getattr(config, 'MUMBLE_VAD_THRESHOLD', -40.0))

        self._chunk_queue = _queue_mod.Queue(maxsize=64)
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(config, 'AUDIO_CHANNELS', 1) * 2

    def push_audio(self, pcm_bytes):
        """Called by sound_received_handler to push Mumble RX audio."""
        # Track level here so it works even when not on a bus
        try:
            arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                _lv = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                _lv = 0
            if _lv > self.audio_level:
                self.audio_level = int(_lv)
            else:
                self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
        except Exception:
            pass
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
        if not self.enabled or self.muted:
            return None, False

        cb = self._chunk_bytes  # target chunk size in bytes

        # Accumulate Mumble frames into sub-buffer
        if not hasattr(self, '_sub_buffer'):
            self._sub_buffer = b''

        _drained = 0
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.get_nowait()
                self._sub_buffer += blob
                _drained += 1
            except _queue_mod.Empty:
                break

        if len(self._sub_buffer) < cb:
            return None, False

        # Full chunk available — no padding, no clicks
        data = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        # Apply volume
        if self.audio_boost != 1.0:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            data = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Level metering (in get_audio for bus-connected path)
        try:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            if rms > 0:
                level = max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))
            else:
                level = 0
            if level > self.audio_level:
                self.audio_level = int(level)
            else:
                self.audio_level = int(self.audio_level * 0.7 + level * 0.3)
        except Exception:
            pass

        # VAD-gated PTT: only key radio when voice detected, not on silence/noise
        vad_pass = False
        try:
            _arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            _rms = float(np.sqrt(np.mean(_arr * _arr))) if len(_arr) > 0 else 0.0
            if _rms > 0:
                vad_pass = (20.0 * _math_mod.log10(_rms / 32767.0)) > self.vad_threshold_db
        except Exception:
            pass
        return data, vad_pass

    def is_active(self):
        return self.enabled and not self.muted and not self._chunk_queue.empty()

    def get_status(self):
        if not self.enabled:
            return "MUMBLE_RX: Disabled"
        return f"MUMBLE_RX: {self.audio_level}%"

    def cleanup(self):
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except _queue_mod.Empty:
                break


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
        # Track level in push (works without bus)
        try:
            arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            _lv = int(max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))) if rms > 0 else 0
            if _lv > self.audio_level:
                self.audio_level = int(_lv)
            else:
                self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
        except Exception:
            pass
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
                break

        if len(self._sub_buffer) < cb:
            # Not enough data yet — return silence to keep PTT keyed while connected
            return b'\x00' * cb, True if self._sub_buffer else False

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
        # Track level in push (works without bus)
        try:
            arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
            _lv = int(max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))) if rms > 0 else 0
            if _lv > self.audio_level:
                self.audio_level = int(_lv)
            else:
                self.audio_level = int(self.audio_level * 0.7 + _lv * 0.3)
        except Exception:
            pass
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
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        raw_level = int(max(0, min(100, (20 * _math_mod.log10(rms / 32767.0) + 60) * (100 / 60)))) if rms > 0 else 0
        self.audio_level = raw_level if raw_level > self.audio_level else int(self.audio_level * 0.7 + raw_level * 0.3)

        # VAD-gated PTT: only trigger PTT when audio exceeds threshold
        vad_pass = False
        if rms > 0:
            db = 20.0 * _math_mod.log10(rms / 32767.0)
            vad_pass = db > self.vad_threshold_db

        # Apply volume multiplier
        if self.volume != 1.0:
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


class StreamOutputSource:
    """Direct Icecast streaming — PCM → ffmpeg MP3 → Icecast HTTP SOURCE.

    Replaces the old DarkIce/FFmpeg/ALSA loopback chain with a single
    in-process pipeline. No external processes needed.
    """
    SILENCE_INTERVAL = 0.05  # seconds between silence frames (50ms = 20 ticks/sec)

    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        self.connected = False
        self._encoder = None      # ffmpeg subprocess
        self._icecast_sock = None  # TCP socket to Icecast
        self._lock = threading.Lock()
        self._reader_thread = None
        self._keepalive_thread = None
        self._last_audio_time = 0  # monotonic time of last real audio push
        self._bytes_sent = 0
        self._connect_time = 0
        self._reconnect_backoff = 5

        if config.ENABLE_STREAM_OUTPUT:
            self._connect()

    def _connect(self):
        """Connect to Icecast and start the MP3 encoder pipeline."""
        import socket, base64

        server = getattr(self.config, 'STREAM_SERVER', '')
        port = int(getattr(self.config, 'STREAM_PORT', 8000))
        mount = getattr(self.config, 'STREAM_MOUNT', '/stream')
        password = getattr(self.config, 'STREAM_PASSWORD', '')
        bitrate = int(getattr(self.config, 'STREAM_BITRATE', 16))
        name = getattr(self.config, 'STREAM_NAME', 'Radio Gateway')

        if not server or not password:
            print("  ⚠ Broadcastify: missing server or password")
            return

        # Connect TCP to Icecast
        try:
            sock = socket.create_connection((server, port), timeout=10)
            # Send SOURCE request (Icecast SOURCE protocol)
            auth = base64.b64encode(f"source:{password}".encode()).decode()
            headers = (
                f"SOURCE {mount} HTTP/1.0\r\n"
                f"Authorization: Basic {auth}\r\n"
                f"Content-Type: audio/mpeg\r\n"
                f"ice-name: {name}\r\n"
                f"ice-public: 1\r\n"
                f"ice-bitrate: {bitrate}\r\n"
                f"\r\n"
            )
            sock.sendall(headers.encode())

            # Read response
            resp = b''
            sock.settimeout(5)
            try:
                while b'\r\n\r\n' not in resp and len(resp) < 1024:
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    resp += chunk
            except socket.timeout:
                pass

            resp_str = resp.decode(errors='replace')
            if '200' not in resp_str.split('\n')[0]:
                print(f"  ⚠ Broadcastify: Icecast rejected connection: {resp_str.strip()}")
                sock.close()
                return

            sock.settimeout(None)
            self._icecast_sock = sock

        except Exception as e:
            print(f"  ⚠ Broadcastify: connection failed: {e}")
            return

        # Start ffmpeg MP3 encoder: PCM stdin → MP3 stdout
        import subprocess as sp
        try:
            self._encoder = sp.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-f', 's16le', '-ar', '48000', '-ac', '1', '-i', 'pipe:0',
                '-c:a', 'libmp3lame', '-b:a', f'{bitrate}k',
                '-flush_packets', '1',
                '-fflags', '+nobuffer',
                '-f', 'mp3', 'pipe:1'
            ], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL)
        except Exception as e:
            print(f"  ⚠ Broadcastify: ffmpeg encoder failed: {e}")
            self._icecast_sock.close()
            self._icecast_sock = None
            return

        # Reader thread: reads MP3 from ffmpeg, sends to Icecast
        def _reader():
            while self._encoder and self._encoder.poll() is None:
                try:
                    data = self._encoder.stdout.read(4096)
                    if not data:
                        break
                    with self._lock:
                        if self._icecast_sock:
                            self._icecast_sock.sendall(data)
                            self._bytes_sent += len(data)
                except (BrokenPipeError, OSError, ConnectionError):
                    print("  [Broadcastify] Connection lost")
                    self.connected = False
                    break
                except Exception:
                    break
            # Clean up on exit
            self.connected = False

        self._reader_thread = threading.Thread(target=_reader, daemon=True,
                                                name="Broadcastify-sender")
        self._reader_thread.start()
        self.connected = True
        self._connect_time = time.time()
        self._last_audio_time = time.monotonic()
        self._bytes_sent = 0

        # Keepalive: feed silence to encoder when no real audio arrives
        if not self._keepalive_thread or not self._keepalive_thread.is_alive():
            self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True,
                                                       name="Broadcastify-keepalive")
            self._keepalive_thread.start()
        print(f"  ✓ Broadcastify: direct Icecast stream to {server}:{port}{mount} ({bitrate}kbps)")

    def send_audio(self, audio_data):
        """Send raw PCM audio to the MP3 encoder. Auto-reconnects on failure."""
        if not self.connected or not self._encoder:
            # Auto-reconnect if we were previously connected
            if self._connect_time > 0 and not getattr(self, '_reconnecting', False):
                self._reconnecting = True
                def _auto_reconnect():
                    time.sleep(5)
                    print("  [Broadcastify] Auto-reconnecting...")
                    self.close()
                    self._connect()
                    self._reconnecting = False
                threading.Thread(target=_auto_reconnect, daemon=True).start()
            return
        try:
            self._encoder.stdin.write(audio_data)
            self._last_audio_time = time.monotonic()
        except (BrokenPipeError, OSError):
            self.connected = False
        except Exception:
            pass

    def _keepalive_loop(self):
        """Feed silence to the encoder when no real audio is arriving.

        Icecast servers drop SOURCE connections that go idle.  By sending
        silence frames the MP3 encoder keeps producing a constant bitrate
        stream even when the radio is quiet.
        """
        # 50ms of silence at 48kHz mono 16-bit = 4800 bytes
        _silence = b'\x00' * 4800
        while True:
            time.sleep(self.SILENCE_INTERVAL)
            if not self.connected or not self._encoder:
                continue
            # Only send silence if no real audio in the last 100ms
            if time.monotonic() - self._last_audio_time < 0.1:
                continue
            try:
                self._encoder.stdin.write(_silence)
            except (BrokenPipeError, OSError):
                self.connected = False
            except Exception:
                pass

    def reconnect(self):
        """Tear down and reconnect."""
        self.close()
        time.sleep(1)
        self._connect()

    def close(self):
        """Clean shutdown."""
        self.connected = False
        if self._encoder:
            try:
                self._encoder.stdin.close()
            except Exception:
                pass
            try:
                self._encoder.kill()
                self._encoder.wait(timeout=3)
            except Exception:
                pass
            self._encoder = None
        if self._icecast_sock:
            try:
                self._icecast_sock.close()
            except Exception:
                pass
            self._icecast_sock = None

    @property
    def uptime(self):
        """Seconds since connection."""
        return time.time() - self._connect_time if self._connect_time else 0

    @property
    def bytes_sent_mb(self):
        """MB sent."""
        return self._bytes_sent / (1024 * 1024)
    
    def cleanup(self):
        """Close pipe"""
        if self.pipe:
            try:
                self.pipe.close()
            except:
                pass




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

