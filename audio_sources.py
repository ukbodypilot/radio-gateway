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

# Shared audio utilities — level metering, AudioProcessor, CW generation
from audio_util import (
    pcm_rms, rms_to_level, update_level, pcm_level, pcm_db, apply_gain,
    AudioProcessor, generate_cw_pcm,
)

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

def bgm_files(config):
    """[(slot, name_or_path), ...] from BGM_FILES. 1-based.

    Module-level because two classes need it: BGMSource plays these, and
    FilePlaybackSource must keep them out of the numbered soundboard slots.
    """
    raw = str(getattr(config, 'BGM_FILES', '') or '').strip()
    names = ([n.strip() for n in raw.replace('\n', ',').split(',') if n.strip()]
             if raw else ['bgm1.mp3', 'bgm2.mp3', 'bgm3.mp3'])
    return list(enumerate(names, 1))


def bgm_path(config, name, audio_dir):
    return name if os.path.isabs(name) else os.path.join(audio_dir, name)


class AudioSource:
    """Base class for all audio sources"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.enabled = True
        self.priority = 0  # Lower = higher priority
        self.volume = 1.0
        self.ptt_control = True  # Can this source trigger PTT?
        
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


# AudioProcessor moved to audio_util.py — re-exported above for backward compat


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
        self._loop_active = False  # Test loop / BGM mode
        self._loop_label = None    # 'test' or 'bgm<N>' — which bed is looping
        
        # Periodic announcement - auto-detect station_id file
        self.last_announcement_time = 0
        self.announcement_interval = config.PLAYBACK_ANNOUNCEMENT_INTERVAL if hasattr(config, 'PLAYBACK_ANNOUNCEMENT_INTERVAL') else 0
        self.announcement_directory = config.PLAYBACK_DIRECTORY if hasattr(config, 'PLAYBACK_DIRECTORY') else './audio/'
        
        # Playback slots. Slot '0' is always the station ID; '1'..PLAYBACK_SLOTS
        # are the soundboard. Keys are STRINGS throughout because slot 0 is
        # special-cased by name in several places and the web UI passes them as
        # JSON strings.
        #
        # Only 1-9 can ever be reached from the physical keyboard — a keypress
        # is one character, so there is no way to type slot 12. Higher slots are
        # addressable from the web UI, `!play 12` in Mumble, and MCP.
        self.slot_count = max(1, min(99, int(getattr(config, 'PLAYBACK_SLOTS', 20))))
        self.file_status = {
            k: {'exists': False, 'playing': False, 'path': None}
            for k in self.slot_keys(include_station_id=True)
        }
        self.check_file_availability()

    def slot_keys(self, include_station_id=False):
        """Soundboard slot keys as strings, in display order.

        Station ID ('0') is listed LAST when included, matching the status bar
        and the on-screen grid where it sits after the numbered slots.
        """
        keys = [str(i) for i in range(1, self.slot_count + 1)]
        if include_station_id:
            keys.append('0')
        return keys

    # Files that live in the audio directory but must never occupy a slot.
    # station_id is the periodic ID; loop.* is the test-loop bed, which used to
    # get scooped up as slot 1 — pressing Loop then looked like it was playing
    # "the sample in button 1", because it was the same file.
    _RESERVED_PREFIXES = ('station_id', 'loop.')

    @classmethod
    def _is_reserved(cls, filename):
        name = os.path.basename(filename).lower()
        return any(name.startswith(p) for p in cls._RESERVED_PREFIXES)

    def _is_reserved_for_slots(self, filename):
        """Class-level reservations PLUS the configured BGM beds.

        BGM files sit in the same directory as the soundboard cache, so without
        this they would be handed a numbered slot — the same trap loop.mp3 fell
        into.
        """
        if self._is_reserved(filename):
            return True
        base = os.path.basename(filename).lower()
        return base in {os.path.basename(n).lower()
                        for _, n in bgm_files(self.config)}

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
        
        slot_keys = self.slot_keys()

        # First pass: files named "<n>_something" claim slot <n>. Multi-digit
        # prefixes are supported now that there can be more than nine slots,
        # so 12_siren.mp3 lands in slot 12.
        for filepath in all_files:
            filename = os.path.basename(filepath)
            if self._is_reserved_for_slots(filename):
                continue
            prefix = filename.split('_', 1)[0]
            if prefix.isdigit() and prefix in slot_keys and prefix not in file_map:
                file_map[prefix] = (filepath, filename)

        # Second pass: anything left fills the remaining slots in order.
        claimed = {v[1] for v in file_map.values()}
        unassigned_files = [f for f in all_files
                            if os.path.basename(f) not in claimed
                            and not self._is_reserved_for_slots(f)]

        empty = [k for k in slot_keys if k not in file_map]
        for filepath, key in zip(unassigned_files, empty):
            file_map[key] = (filepath, os.path.basename(filepath))
        
        # Step 4: Update file_status with found files (local files only here —
        # soundboard downloads complete asynchronously and update file_status
        # themselves as each file lands).
        for key in self.slot_keys(include_station_id=True):
            if key in file_map:
                filepath, filename = file_map[key]
                self.file_status[key]['exists'] = True
                self.file_status[key]['path'] = filepath
                self.file_status[key]['filename'] = filename

        # Step 5: Print file mapping (will be displayed before status bar)
        self.file_mapping_display = self._generate_file_mapping_display(file_map, station_id_found)

        # Step 6: Kick off soundboard prefetch in the background. Must NOT
        # block startup — the original synchronous call wedged the entire
        # gateway init pipeline for 18+ minutes on a stuck mixkit.co socket
        # (no timeout on urlretrieve), so broadcastify and everything after
        # setup_playback never came up. Background thread + per-request
        # timeout keeps soundboard slot population best-effort.
        if getattr(self.config, 'ENABLE_SOUNDBOARD', True):
            import threading
            threading.Thread(target=self._fill_soundboard_slots, args=(file_map,),
                             daemon=True, name="Soundboard-prefetch").start()

    # Curated pool of 433 free sound effects from Mixkit (royalty-free, no attribution)
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
        # Fart (12) — full free-tier Mixkit fart category
        ('fart', 3041), ('fart', 3043), ('fart', 3051), ('fart', 3052),
        ('fart', 3053), ('fart', 3054), ('fart', 3055), ('fart', 3056),
        ('fart', 2889), ('fart', 2890), ('fart', 2891), ('fart', 3050),
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

    @classmethod
    def soundboard_categories(cls):
        """Category label -> number of sounds, derived from SOUNDBOARD_POOL.

        The label is ours, not Mixkit's: the download URL is built from the
        numeric id alone, so these strings exist purely to group the pool and
        to name the cached file. That means a category can be renamed or
        re-grouped freely without breaking any download.
        """
        counts = {}
        for cat, _ in cls.SOUNDBOARD_POOL:
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    def _select_soundboard_pool(self):
        """Apply the SOUNDBOARD_CATEGORIES filter. Returns (pool, note).

        Config is a comma-separated list of category names. A name prefixed
        with '-' excludes instead of includes, so both of these work:

            SOUNDBOARD_CATEGORIES = boing, fart, scream, wrong
            SOUNDBOARD_CATEGORIES = -animals, -applause

        Blank or absent means every category. Read at call time rather than
        cached, so saving the config page (which calls config.load_config())
        takes effect on the very next refresh with no gateway restart.
        """
        raw = str(getattr(self.config, 'SOUNDBOARD_CATEGORIES', '') or '').strip()
        available = self.soundboard_categories()
        full = list(self.SOUNDBOARD_POOL)
        if not raw:
            return full, ''

        include, exclude, unknown = set(), set(), []
        for tok in raw.replace('\n', ',').split(','):
            tok = tok.strip().lower()
            if not tok:
                continue
            negated = tok.startswith('-')
            name = tok[1:].strip() if negated else tok
            if name not in available:
                unknown.append(name)
            elif negated:
                exclude.add(name)
            else:
                include.add(name)

        if unknown:
            print(f"  [Soundboard] Ignoring unknown categor"
                  f"{'y' if len(unknown) == 1 else 'ies'}: {', '.join(sorted(unknown))}")
            print(f"  [Soundboard] Valid categories: {', '.join(sorted(available))}")

        # Never leave the slots empty over a config typo — a soundboard that
        # silently goes quiet is worse than one that ignores you. Both the
        # all-unknown case and the exclude-everything case fall back loudly.
        if not include and not exclude:
            print("  [Soundboard] SOUNDBOARD_CATEGORIES named no valid categories — "
                  "falling back to the full pool")
            return full, 'filter matched nothing, using all categories'

        chosen = (include or set(available)) - exclude
        pool = [p for p in full if p[0] in chosen]
        if not pool:
            print("  [Soundboard] SOUNDBOARD_CATEGORIES matched no sounds — "
                  "falling back to the full pool")
            return full, 'filter matched nothing, using all categories'
        return pool, (f"{len(pool)} sounds from {len(chosen)} "
                      f"categor{'y' if len(chosen) == 1 else 'ies'}: "
                      f"{', '.join(sorted(chosen))}")

    # Worst-case MP3 bitrate (320 kbps = 40 kB/s). Content-Length divided by
    # this is a LOWER BOUND on a clip's duration, so a file that exceeds the
    # cap even at this bitrate is definitely too long and can be rejected
    # without downloading it.
    _MAX_MP3_BYTES_PER_SEC = 40000

    def _soundboard_meta_path(self):
        """Learned clip durations. Deliberately OUTSIDE .cache, which the
        Refresh button deletes wholesale — otherwise every refresh would
        re-download the same known-too-long clips to re-learn what it already
        knew."""
        import os
        return os.path.join(self.announcement_directory, '.soundboard_meta.json')

    def _load_soundboard_meta(self):
        import json, os
        path = self._soundboard_meta_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}   # corrupt memo is not worth failing a refresh over

    def _save_soundboard_meta(self, meta):
        import json, os
        path = self._soundboard_meta_path()
        try:
            tmp = path + '.partial'
            with open(tmp, 'w') as f:
                json.dump(meta, f)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [Soundboard] Could not save duration memo: {e}")

    @staticmethod
    def _sound_duration(path):
        """Clip length in seconds, or None if it can't be determined.

        Bounded timeout per the project rule on subprocess calls — this runs on
        the prefetch thread, but a wedged ffprobe would still pin the slot.
        """
        import subprocess
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', path],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except Exception:
            pass
        return None

    def _fill_soundboard_slots(self, file_map):
        """Download random sound effects from Mixkit to fill empty playback slots."""
        import os, random, urllib.request

        empty_slots = [k for k in self.slot_keys() if k not in file_map]
        if not empty_slots:
            return

        cache_dir = os.path.join(self.announcement_directory, '.cache')
        os.makedirs(cache_dir, exist_ok=True)

        pool, note = self._select_soundboard_pool()
        if note:
            print(f"  [Soundboard] {note}")
        random.shuffle(pool)

        max_secs = float(getattr(self.config, 'SOUNDBOARD_MAX_SECONDS', 15) or 0)
        meta = self._load_soundboard_meta()
        meta_dirty = False
        byte_ceiling = max_secs * self._MAX_MP3_BYTES_PER_SEC if max_secs > 0 else 0

        # Walk candidates until the slots are full. This is a loop rather than
        # a fixed slice because a candidate can now be REJECTED (too long), and
        # a rejected pick must be replaced rather than leaving a slot empty.
        #
        # De-duplicate by sound id, not by (category, id): 19 ids sit under
        # more than one category — id 2891 is under boing, fart AND funny —
        # and the URL is built from the id alone, so a plain slice could hand
        # the SAME clip to two slots under two different filenames.
        slots = list(empty_slots)
        seen_ids = set()
        skipped_long = 0

        for category, sfx_id in pool:
            if not slots:
                break
            if sfx_id in seen_ids:
                continue
            seen_ids.add(sfx_id)

            # Already known to be over the cap — skip without a round trip.
            known = meta.get(str(sfx_id))
            if max_secs > 0 and isinstance(known, (int, float)) and known > max_secs:
                skipped_long += 1
                continue

            filename = f"{category}_{sfx_id}.mp3"
            filepath = os.path.join(cache_dir, filename)

            if not os.path.exists(filepath):
                url = f"https://assets.mixkit.co/active_storage/sfx/{sfx_id}/{sfx_id}-preview.mp3"
                try:
                    # Bounded timeout is essential: this runs in a background
                    # thread, but a leaked stuck socket would still pin file
                    # descriptors and leave a permanently-empty slot.
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        clen = resp.headers.get('Content-Length')
                        if byte_ceiling and clen and int(clen) > byte_ceiling:
                            # Too big to be under the cap even at 320 kbps, so
                            # skip the body entirely. Record the lower-bound
                            # duration so future refreshes skip it for free.
                            meta[str(sfx_id)] = int(clen) / self._MAX_MP3_BYTES_PER_SEC
                            meta_dirty = True
                            skipped_long += 1
                            continue
                        data = resp.read()
                    tmp_path = filepath + '.partial'
                    with open(tmp_path, 'wb') as f:
                        f.write(data)
                    os.replace(tmp_path, filepath)
                except Exception as e:
                    print(f"  [Soundboard] Failed to download {filename}: {e}")
                    continue

            # Enforce the cap authoritatively. Content-Length only bounds it;
            # a 128 kbps 60s track sails under the byte ceiling.
            if max_secs > 0:
                dur = self._sound_duration(filepath)
                if dur is not None:
                    if meta.get(str(sfx_id)) != dur:
                        meta[str(sfx_id)] = dur
                        meta_dirty = True
                    if dur > max_secs:
                        print(f"  [Soundboard] Skipping {filename}: {dur:.0f}s "
                              f"exceeds SOUNDBOARD_MAX_SECONDS={max_secs:.0f}")
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                        skipped_long += 1
                        continue

            slot = slots.pop(0)
            print(f"  [Soundboard] Slot {slot}: {filename}")
            file_map[slot] = (filepath, filename)
            # Background-mode: update file_status so the slot becomes playable
            # as soon as the download lands. Step 4 in check_file_availability
            # only sees local files because this runs asynchronously.
            self.file_status[slot]['exists'] = True
            self.file_status[slot]['path'] = filepath
            self.file_status[slot]['filename'] = filename

        if meta_dirty:
            self._save_soundboard_meta(meta)
        if skipped_long:
            print(f"  [Soundboard] Skipped {skipped_long} clip(s) longer than "
                  f"{max_secs:.0f}s")
        if slots:
            # Say so rather than leaving slots mysteriously blank.
            print(f"  [Soundboard] {len(slots)} slot(s) left unfilled — widen "
                  f"SOUNDBOARD_CATEGORIES or raise SOUNDBOARD_MAX_SECONDS")
    
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
        
        # Numbered slots - Announcements
        for key in self.slot_keys():
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
        
    def queue_file(self, filepath, target_rms_db=None):
        """Pre-decode an audio file and add it to the playback queue.
        Decoding happens here (caller's thread) so the audio transmit loop
        never blocks on file I/O. `target_rms_db` forwards to _decode_file
        for loudness-normalising synthesised TTS — see its docstring."""
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
        pcm_bytes = self._decode_file(full_path, target_rms_db=target_rms_db)
        if pcm_bytes is None:
            return False

        self.playlist.append((full_path, pcm_bytes))
        if self.gateway.config.VERBOSE_LOGGING:
            print(f"\n[Playback] ✓ Queued: {os.path.basename(full_path)} ({len(self.playlist)} in queue)")
        return True

    def queue_pcm(self, pcm_bytes, name="synth"):
        """Queue already-decoded int16 PCM (AUDIO_RATE, mono) for playback.

        Used by the fart synthesizer to push generated audio through the
        same priority-0, PTT-keying path as soundboard files — no file on
        disk, no decode. `name` is a display label only (matches no slot,
        so no 0-9 button lights up)."""
        if not pcm_bytes:
            return False
        self.playlist.append((name, pcm_bytes))
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
    
    def toggle_test_loop(self, action='toggle'):
        """Start/stop the test loop (loop.mp3/wav/ogg from the audio dir).

        `action` is 'start', 'stop' or 'toggle'. Explicit actions exist because
        a blind toggle desynchronises: the loop also stops via stop_playback()
        (the Stop button, a queued announcement, PTT release), and the button
        never learned. The UI would then show "Stop Loop" while the server was
        idle, so the next click STARTED a loop instead of stopping one — which
        is exactly the "stop loop won't stop it" report. The button now sends
        what it means and re-syncs from /status.
        """
        import os
        action = str(action or 'toggle').lower()
        want_stop = action == 'stop' or (action == 'toggle' and self._loop_active)

        if want_stop:
            was = self._loop_active
            self._loop_active = False
            self._loop_label = None
            if was:
                self.stop_playback()
                print("[Playback] Test loop stopped")
            return {'ok': True, 'looping': False, 'was_looping': was}

        for name in ('loop.mp3', 'loop.wav', 'loop.ogg'):
            path = os.path.join(self.announcement_directory, name)
            if os.path.exists(path):
                self._loop_active = True
                self._loop_label = 'test'
                self.queue_file(path)
                print(f"[Playback] Test loop started: {name}")
                return {'ok': True, 'looping': True, 'file': name}
        # Nothing to play — make sure we do not leave the flag (and therefore
        # the button) asserted for a loop that never started.
        self._loop_active = False
        return {'ok': False, 'looping': False,
                'error': 'No loop.mp3/loop.wav/loop.ogg found in the audio directory'}

    @property
    def loop_active(self):
        return bool(self._loop_active)

    def stop_playback(self):
        """Stop current playback and clear queue"""
        # Clear ALL playing flags — only one file plays at a time
        for key in self.file_status:
            self.file_status[key]['playing'] = False

        # Clear current playback
        self._loop_active = False
        self._loop_label = None
        self.current_file = None
        # Inject 200ms silence to flush downstream buffers (endpoint aplay,
        # WebSocket send queue, browser audio buffer) before clearing file_data
        _silence = b'\x00' * (self.config.AUDIO_RATE * self.config.AUDIO_CHANNELS * 2 // 5)  # 200ms
        self.file_data = _silence
        self.file_position = 0

        # Clear queue
        self.playlist.clear()

        # Release PTT immediately (don't wait for timeout)
        gw = self.gateway
        if gw.ptt_active and not gw.manual_ptt_mode:
            gw.ptt_active = False
            gw._pending_ptt_state = False

        # Restore RTS state
        self._restore_playback_rts()

        if self.gateway.config.VERBOSE_LOGGING:
            print("\n[Playback] ✓ Stopped playback and cleared queue")
    
    def _decode_file(self, filepath, normalize=True, target_rms_db=None):
        """Decode an audio file to PCM bytes.  Returns bytes on success, None on failure.
        Called from queue_file() in the caller's thread so the audio loop never blocks.

        `normalize=False` skips the peak-normalise step. Pass it for material the
        operator has already levelled deliberately — BGM beds are loudness-matched
        offline, and peak-normalising them re-levels each one by its own crest
        factor, which silently undoes that match (measured: a 0.0 LU set came out
        2.2 dB apart). The caller states the intent rather than this function
        guessing from the filename.

        `target_rms_db`, when set, adds a loudness (RMS) pass on top of the peak
        pass: measured across kokoro/edge/gtts, all three sit well under −1 dBFS
        peak with a 16-18 dB crest factor, so peak-normalising alone still leaves
        RMS around −17 to −19 dBFS — quiet enough that operators reach for a gain
        slider downstream instead. This pass boosts to the target through the same
        tanh soft-clip as the mixer's sink gain, so unlike a hard-clamped gain
        stage it saturates gracefully rather than flat-topping. One-way like the
        peak pass — never attenuates."""
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
                    # soxr first: same quality tier as resampy but roughly two
                    # orders of magnitude faster on this hardware. Measured on
                    # a 5-minute 44.1 kHz bed — resampy ~57s, soxr 0.48s. That
                    # 25s decode was the whole reason a BGM button took ~26s to
                    # make a sound; 48 kHz files skip this block entirely, which
                    # is why only some files were slow.
                    audio_float = audio_data.astype('float32') / 32768.0
                    resampled = None
                    try:
                        import soxr
                        resampled = soxr.resample(audio_float, sample_rate,
                                                  self.config.AUDIO_RATE)
                    except ImportError:
                        try:
                            import resampy
                            resampled = resampy.resample(audio_float, sample_rate,
                                                         self.config.AUDIO_RATE)
                        except ImportError:
                            resampled = None

                    if resampled is not None:
                        # Clip before the int16 cast. Both resamplers can ring
                        # slightly past full scale on transients, and the old
                        # unclipped cast wrapped that round to the opposite
                        # polarity — an audible tick rather than a soft clip.
                        audio_data = np.clip(resampled * 32768.0,
                                             -32768, 32767).astype('int16')
                    else:
                        # Fallback: simple linear interpolation
                        if self.gateway.config.VERBOSE_LOGGING:
                            print(f"    (basic resampling — install soxr for better quality)")
                        ratio = self.config.AUDIO_RATE / sample_rate
                        new_length = int(len(audio_data) * ratio)
                        indices = (np.arange(new_length) / ratio).astype(int)
                        audio_data = audio_data[indices]

                # Peak-normalise quiet files so the gain slider isn't the only
                # way to get reasonable loudness. Targets −1 dBFS; leaves files
                # already at or above −1 dBFS untouched (one-way ratchet, never
                # attenuates). Caller's volume slider still applies afterwards,
                # but on already-normalised audio so ≤100% stays clean.
                _peak = int(np.max(np.abs(audio_data))) if len(audio_data) else 0
                if normalize and 0 < _peak < 29204:  # 29204 ≈ −1 dBFS on int16
                    _ratio = 29204.0 / _peak
                    _f32 = audio_data.astype(np.float32) * _ratio
                    audio_data = np.clip(_f32, -32768, 32767).astype(np.int16)
                    if self.gateway.config.VERBOSE_LOGGING:
                        print(f"  Normalised peak {_peak} → 29204 (+{20*np.log10(_ratio):.1f} dB)")

                if target_rms_db is not None:
                    _rms = pcm_rms(audio_data.tobytes())
                    if _rms > 0:
                        _cur_db = 20 * np.log10(_rms / 32768.0)
                        _need_db = target_rms_db - _cur_db
                        if _need_db > 0.5:
                            _gain = 10 ** (_need_db / 20.0)
                            audio_data = np.frombuffer(
                                apply_gain(audio_data.tobytes(), _gain), dtype=np.int16)
                            if self.gateway.config.VERBOSE_LOGGING:
                                print(f"  Loudness-normalised RMS {_cur_db:.1f} → "
                                      f"{target_rms_db:.1f} dBFS (+{_need_db:.1f} dB, tanh)")

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
            
            # Clear all playing flags (only one file plays at a time)
            for key in self.file_status:
                self.file_status[key]['playing'] = False

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
        
        # Apply volume. Soft-clip via tanh for volume > 1 so pushing the
        # slider past 100% rolls off cleanly instead of flat-topping into
        # square-wave harmonics. At volume ≤ 1 this is a pure gain (tanh is
        # near-linear in the small-signal region).
        if self.volume != 1.0:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            if self.volume > 1.0:
                # Normalise → scale → tanh → back to int16.
                boosted = np.tanh(arr / 32768.0 * self.volume) * 32768.0
                chunk = boosted.astype(np.int16).tobytes()
            else:
                chunk = (arr * self.volume).astype(np.int16).tobytes()

        # Level metering for routing display
        try:
            self.audio_level = pcm_level(chunk, self.audio_level)
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


class LoopPlaybackSource(AudioSource):
    """Plays loop recorder audio as a routable source node.

    Uses ffmpeg to stream-decode MP3 segments into raw PCM.  A reader
    thread fills a bounded queue; get_audio() drains it at bus-tick rate.
    Playback persists even when the web page is closed.
    """

    SAMPLE_RATE = 48000
    _DIAG_INTERVAL = 10.0  # seconds between console diagnostics

    def __init__(self, gateway):
        super().__init__("LoopPlayback", gateway.config)
        self.gateway = gateway
        self.priority = 10
        self.ptt_control = True
        self.volume = 1.0
        self.audio_level = 0
        self._stream_trace = None  # set by gateway_core after init

        self._lock = threading.Lock()
        self._playing = False
        self._bus_id = None
        self._play_start = 0.0
        self._play_position = 0.0
        self._decoder = None
        self._concat_path = None
        self._pcm_queue = _queue_mod.Queue(maxsize=200)
        self._reader_thread = None

        # Diagnostics — rolling counters reset every _DIAG_INTERVAL
        self._diag_time = 0.0
        self._diag_reads = 0          # reader: successful pipe reads
        self._diag_read_bytes = 0     # reader: total bytes read
        self._diag_short_reads = 0    # reader: reads shorter than chunk_bytes
        self._diag_put_full = 0       # reader: queue full (put timeout)
        self._diag_get_ok = 0         # get_audio: successful pulls
        self._diag_get_empty = 0      # get_audio: queue was empty (underrun)
        self._diag_get_pad = 0        # get_audio: chunk needed zero-padding
        self._diag_get_trim = 0       # get_audio: chunk was oversized
        self._diag_last_qd = 0        # most recent queue depth at get_audio
        self._diag_max_qd = 0         # max queue depth seen in window
        self._diag_min_qd = 200       # min queue depth seen in window
        self._diag_get_intervals = [] # monotonic intervals between get_audio calls

    def _diag_reset(self):
        """Reset rolling diagnostic counters."""
        self._diag_time = time.monotonic()
        self._diag_reads = 0
        self._diag_read_bytes = 0
        self._diag_short_reads = 0
        self._diag_put_full = 0
        self._diag_get_ok = 0
        self._diag_get_empty = 0
        self._diag_get_pad = 0
        self._diag_get_trim = 0
        self._diag_max_qd = 0
        self._diag_min_qd = 200
        self._diag_get_intervals = []

    def _diag_print(self):
        """Print diagnostic summary to console (gated by VERBOSE_LOGGING)."""
        if not getattr(self.config, 'VERBOSE_LOGGING', False):
            return
        dt = time.monotonic() - self._diag_time
        if dt < 0.1:
            return
        ivs = self._diag_get_intervals
        iv_mean = sum(ivs) / len(ivs) if ivs else 0
        iv_max = max(ivs) if ivs else 0
        iv_min = min(ivs) if ivs else 0
        jitter = iv_max - iv_min if ivs else 0
        over_60 = sum(1 for v in ivs if v > 60)
        over_80 = sum(1 for v in ivs if v > 80)
        print(f"  [LoopPlay-DIAG] {dt:.0f}s: "
              f"read={self._diag_reads} ({self._diag_read_bytes//1024}kB) "
              f"short={self._diag_short_reads} full={self._diag_put_full} | "
              f"get={self._diag_get_ok} empty={self._diag_get_empty} "
              f"pad={self._diag_get_pad} trim={self._diag_get_trim} | "
              f"qd={self._diag_min_qd}-{self._diag_max_qd} | "
              f"iv={iv_mean:.1f}/{iv_max:.1f}ms "
              f"jitter={jitter:.1f}ms >60={over_60} >80={over_80}")

    # -- control API --------------------------------------------------------

    def play(self, bus_id, start_epoch):
        """Start (or seek) playback of a loop recorder bus from *start_epoch*."""
        self.stop()
        lr = getattr(self.gateway, 'loop_recorder', None)
        if not lr:
            return False

        # Determine end: latest available data for this bus
        buses = lr.get_buses()
        bus_info = next((b for b in buses if b['id'] == bus_id), None)
        if not bus_info:
            return False
        end_epoch = bus_info['latest']
        if start_epoch >= end_epoch:
            return False

        segments = lr.get_segments(bus_id, start_epoch, end_epoch)
        if not segments:
            return False

        print(f"  [LoopPlay] {len(segments)} segments, "
              f"{end_epoch - start_epoch:.0f}s duration")

        # Build ffmpeg concat file
        import tempfile
        cf = tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', prefix='lp_concat_', delete=False)
        for seg in segments:
            cf.write(f"file '{seg['path']}'\n")
        cf.close()
        self._concat_path = cf.name

        offset = max(0, start_epoch - segments[0]['start'])
        duration = end_epoch - start_epoch
        channels = getattr(self.config, 'AUDIO_CHANNELS', 1)

        # ffmpeg decodes flat-out; the reader thread paces its pipe reads at
        # real-time so the clock advances regardless of bus consumption.
        # (-re on the input would interact badly with -ss, forcing the seek
        # to happen at real time — 172 s of concat input = 172 s of wall-
        # clock stall before the first byte emerges.)
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
               '-f', 'concat', '-safe', '0', '-i', cf.name,
               '-ss', str(offset), '-t', str(duration),
               '-f', 's16le', '-ar', str(self.SAMPLE_RATE),
               '-ac', str(channels), 'pipe:1']
        try:
            self._decoder = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  [LoopPlay] Decode failed: {e}")
            self._cleanup_files()
            return False

        with self._lock:
            self._bus_id = bus_id
            self._play_start = start_epoch
            self._play_position = start_epoch
            self._playing = True
            while not self._pcm_queue.empty():
                try:
                    self._pcm_queue.get_nowait()
                except _queue_mod.Empty:
                    break

        self._diag_reset()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="loop-play-read")
        self._reader_thread.start()
        print(f"  [LoopPlay] Playing {bus_id} from "
              f"{time.strftime('%H:%M:%S', time.localtime(start_epoch))}")
        return True

    def stop(self):
        """Stop playback and clean up."""
        with self._lock:
            was = self._playing
            self._playing = False
        if self._decoder:
            try:
                self._decoder.kill()
                self._decoder.wait(timeout=5)
            except Exception:
                pass
            self._decoder = None
        # Drain queue
        drained = 0
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
                drained += 1
            except _queue_mod.Empty:
                break
        self._cleanup_files()
        self.audio_level = 0
        if was:
            self._diag_print()
            print(f"  [LoopPlay] Stopped (drained {drained} queued chunks)")

    def _cleanup_files(self):
        if self._concat_path:
            try:
                os.unlink(self._concat_path)
            except Exception:
                pass
            self._concat_path = None

    # -- reader thread ------------------------------------------------------

    def _reader_loop(self):
        """Fill PCM queue from ffmpeg stdout, and drive clock + meter.

        The reader owns position and audio_level: ffmpeg is run with -re so
        it paces at real-time, and each successful pipe read advances the
        play position by its sample count and updates audio_level. This lets
        the source run standalone — clock ticks and meter animates — whether
        or not a bus is consuming via get_audio(). If a bus IS consuming, it
        pulls chunks from the queue and plays them. If no consumer exists,
        the queue fills and we drop the oldest chunk on each new put so the
        reader never stalls.
        """
        channels = getattr(self.config, 'AUDIO_CHANNELS', 1)
        chunk_bytes = int(self.SAMPLE_RATE * 0.05) * channels * 2  # 50ms
        tick_s = chunk_bytes / (self.SAMPLE_RATE * channels * 2)  # 0.05s
        next_tick = time.monotonic()
        _st = self._stream_trace
        try:
            while self._playing and self._decoder:
                _t0 = time.monotonic()
                data = self._decoder.stdout.read(chunk_bytes)
                _read_ms = (time.monotonic() - _t0) * 1000
                if not data:
                    if _st and _st.active:
                        _st.record('lp_read', 'eof', None, self._pcm_queue.qsize())
                    break

                _qd = self._pcm_queue.qsize()
                self._diag_reads += 1
                self._diag_read_bytes += len(data)
                if len(data) < chunk_bytes:
                    self._diag_short_reads += 1

                # Advance clock + meter from the reader, independent of
                # whether get_audio() is being called.
                samples = len(data) // (channels * 2)
                if samples:
                    self._play_position += samples / self.SAMPLE_RATE
                try:
                    self.audio_level = pcm_level(data, self.audio_level)
                except Exception:
                    pass

                if _st and _st.active:
                    _extra = ''
                    if len(data) < chunk_bytes:
                        _extra = f'short:{len(data)}/{chunk_bytes}'
                    if _read_ms > 50:
                        _extra += f' slow_read:{_read_ms:.0f}ms'
                    _st.record('lp_read', 'pipe_read', data, _qd, _extra)

                # Non-blocking put with drop-oldest on full — keeps the
                # reader running even when no bus is draining the queue.
                try:
                    self._pcm_queue.put_nowait(data)
                    if _st and _st.active:
                        _st.record('lp_read', 'queue_put', data,
                                   self._pcm_queue.qsize())
                except _queue_mod.Full:
                    self._diag_put_full += 1
                    try:
                        self._pcm_queue.get_nowait()
                    except _queue_mod.Empty:
                        pass
                    try:
                        self._pcm_queue.put_nowait(data)
                    except _queue_mod.Full:
                        pass
                    if _st and _st.active:
                        _st.record('lp_read', 'queue_put', data, _qd,
                                   'FULL_DROP_OLDEST')

                # Pace this loop to real-time audio rate. ffmpeg decodes
                # flat-out into the pipe; we throttle here so the clock +
                # meter tick at the correct speed and ffmpeg blocks on
                # pipe-full once the queue has caught up.
                next_tick += tick_s
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    # Fell behind (e.g. after a disk stall) — resync so we
                    # don't spin reading back-to-back trying to catch up.
                    next_tick = time.monotonic()
        except Exception as e:
            print(f"  [LoopPlay] Reader error: {e}")
        finally:
            with self._lock:
                self._playing = False
            self.audio_level = 0
            if _st and _st.active:
                _st.record('lp_read', 'exit', None,
                           self._pcm_queue.qsize(), 'reader_done')

    # -- AudioSource interface ----------------------------------------------

    def get_audio(self, chunk_size):
        """Pure drain for bus consumers — position + meter are owned by the
        reader thread so playback runs regardless of routing."""
        _now = time.monotonic()

        if not self._playing:
            return None, False

        _qd = self._pcm_queue.qsize()
        self._diag_last_qd = _qd
        if _qd > self._diag_max_qd:
            self._diag_max_qd = _qd
        if _qd < self._diag_min_qd:
            self._diag_min_qd = _qd

        # Track get_audio call intervals (ms)
        if hasattr(self, '_last_get_time') and self._last_get_time > 0:
            _iv = (_now - self._last_get_time) * 1000
            self._diag_get_intervals.append(_iv)
        self._last_get_time = _now

        _st = self._stream_trace

        try:
            data = self._pcm_queue.get_nowait()
        except _queue_mod.Empty:
            self._diag_get_empty += 1
            if _st and _st.active:
                _st.record('lp_out', 'get_audio', None, 0, 'UNDERRUN')
            return None, False

        self._diag_get_ok += 1

        channels = getattr(self.config, 'AUDIO_CHANNELS', 1)
        expected = chunk_size * channels * 2
        _extra = ''
        if len(data) < expected:
            _extra = f'pad:{expected - len(data)}'
            self._diag_get_pad += 1
            data += b'\x00' * (expected - len(data))
        elif len(data) > expected:
            _extra = f'trim:{len(data) - expected}'
            self._diag_get_trim += 1
            data = data[:expected]

        if _st and _st.active:
            _st.record('lp_out', 'get_audio', data, _qd, _extra)

        # Volume
        if self.volume != 1.0:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            data = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

        # Periodic diagnostic dump
        if _now - self._diag_time >= self._DIAG_INTERVAL:
            self._diag_print()
            self._diag_reset()

        return data, True

    def is_active(self):
        return self._playing

    def get_status(self):
        if self._playing:
            pos = time.strftime('%H:%M:%S', time.localtime(self._play_position))
            qd = self._pcm_queue.qsize()
            return f"{self.name}: Playing {self._bus_id} @ {pos} (qd={qd})"
        return f"{self.name}: Idle"

    def get_status_dict(self):
        return {
            'playing': self._playing,
            'bus': self._bus_id,
            'position': self._play_position,
            'start': self._play_start,
            'queue_depth': self._pcm_queue.qsize(),
            'underruns': self._diag_get_empty,
        }


class EchoLinkSource(AudioSource):
    """EchoLink audio input via TheLinkBox IPC"""
    def __init__(self, config, gateway):
        super().__init__("EchoLink", config)
        self.gateway = gateway
        self.priority = 2  # After Radio (1), before Files (0)
        self.ptt_control = True  # EchoLink doesn't trigger radio PTT
        self.volume = 1.0
        
        # IPC state
        self.rx_pipe = None
        self.tx_pipe = None
        self.connected = False
        self.last_audio_time = 0
        # Carries the remainder of a short pipe read to the next get_audio.
        self._sub_buffer = b''
        
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

            # A non-blocking pipe read returns whatever is buffered, which is
            # very often less than a full chunk. The old code compared
            # `len(data) == chunk_bytes` and threw everything else away, so
            # any partial read silently dropped that audio and the stream
            # arrived chopped. Accumulate instead and only serve whole chunks.
            data = self.rx_pipe.read(chunk_bytes)
            if data:
                self._sub_buffer += data
            if len(self._sub_buffer) < chunk_bytes:
                return None, False
            out = self._sub_buffer[:chunk_bytes]
            self._sub_buffer = self._sub_buffer[chunk_bytes:]

            self.last_audio_time = time.time()

            # Apply volume
            if self.volume != 1.0:
                arr = np.frombuffer(out, dtype=np.int16).astype(np.float32)
                out = np.clip(arr * self.volume, -32768, 32767).astype(np.int16).tobytes()

            return out, False  # No PTT control

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
        # Unsent remainder of a partially-sent frame. The protocol is
        # length-prefixed, so a frame must either finish or the connection
        # must reset — dropping a half-sent frame desyncs the receiver's
        # framing permanently (it reads PCM bytes as length headers).
        # Bounded: always < one frame.
        self._tx_backlog = b''

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
                self._tx_backlog = b''   # stale partial frame belongs to the old connection
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

        Non-blocking, framing-safe: a partially-sent frame is parked in
        _tx_backlog and resumed on the next call before anything new goes
        out. (The old code dropped the REST of a half-sent frame on
        backpressure, which desynced the receiver's length-prefix framing
        — every later frame decoded as garbage until reconnect.) When a
        backlog is standing, NEW frames are dropped whole — the receiver
        misses 50ms instead of losing the stream.
        """
        sock = self._socket
        if not sock:
            self._tx_backlog = b''
            return
        import struct
        try:
            # Finish any partially-sent frame first.
            if self._tx_backlog:
                self._tx_backlog = self._send_some(sock, self._tx_backlog)
                if self._tx_backlog:
                    return  # still backed up — drop the new frame whole
            frame = struct.pack('>I', len(pcm_data)) + pcm_data
            self._tx_backlog = self._send_some(sock, frame)
        except Exception:
            # Link broken — trigger reconnect
            self.connected = False
            self._socket = None
            self._tx_backlog = b''
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _send_some(sock, buf):
        """Non-blocking send of *buf*; returns the unsent remainder (b'' if
        fully sent). Raises on a broken connection."""
        while buf:
            try:
                n = sock.send(buf)
            except BlockingIOError:
                break  # TCP buffer full — caller keeps the remainder
            if n == 0:
                raise ConnectionError("send returned 0")
            buf = buf[n:]
        return buf

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
        self.ptt_control = True
        self.volume = 1.0
        self.mix_ratio = 1.0
        self.duck = config.REMOTE_AUDIO_DUCK
        self.enabled = True
        self.muted = False

        self.audio_level = 0
        self.server_connected = False

        self._chunk_queue = _queue_mod.Queue(maxsize=32)
        self._sub_buffer = b''
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2  # 16-bit mono
        self._reader_running = False
        self._reader_thread = None
        self._listen_socket = None
        self._conn = None  # current accepted connection (for reset)
        # Jitter buffer: prime with N chunks (~50 ms each) before draining so
        # WASAPI callback-gap spikes don't underrun the bus. Modeled on
        # LinkAudioSource. Un-primes only on disconnect; transient underruns
        # emit silence but stay primed. 8 chunks = ~400 ms initial cushion,
        # maxsize=32 = ~1.6 s absorption capacity for bus-side stalls.
        # 8 chunks = ~400 ms. Higher than the link default because this feed
        # comes from a Windows client whose WASAPI callback gaps are much
        # burstier than a link endpoint's. maxsize=32 caps absorption at
        # ~1.6 s. Clamped to [1, 31]: 0 defeats the buffer, >= maxsize can
        # never prime.
        try:
            _pf = int(getattr(config, 'REMOTE_AUDIO_JITTER_PREFILL', 8))
        except (TypeError, ValueError):
            _pf = 8
        self._jitter_prefill = max(1, min(_pf, 31))
        self._jitter_primed = False

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
                        self.audio_level = pcm_level(payload, self.audio_level)
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
                    if (not self._jitter_primed and
                            self._chunk_queue.qsize() >= self._jitter_prefill):
                        self._jitter_primed = True
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
        self._jitter_primed = False
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

        # Skip queue lock entirely when not connected — nothing to drain.
        # Un-prime so the next connection starts with a fresh prefill cushion.
        if not self.server_connected and not self._sub_buffer:
            self._jitter_primed = False
            return None, False

        # Wait until the jitter buffer is primed before delivering audio.
        # Single transient underruns (below) emit silence for that tick but
        # leave _jitter_primed alone — only disconnect re-arms it.
        if not self._jitter_primed:
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
            display_gain = float(self.config.REMOTE_AUDIO_DISPLAY_GAIN)
            self.audio_level = pcm_level(raw, self.audio_level, gain=display_gain)

            audio_boost = float(self.config.REMOTE_AUDIO_AUDIO_BOOST)
            if audio_boost != 1.0:
                farr = arr.astype(np.float32)
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
        self.ptt_control = True
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
        self._audio_level_last_mono = 0.0
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * getattr(config, 'AUDIO_CHANNELS', 1) * 2  # 50 ms, 16-bit
        # Jitter buffer. Single producer (link reader thread → push_audio)
        # and single consumer (bus tick → get_audio). deque's append/popleft
        # are individually atomic under the GIL; compound ops (e.g. len()
        # followed by popleft) are NOT safe. Do not add a second producer or
        # consumer without switching to queue.Queue.
        self._chunk_queue = _queue_mod.deque(maxlen=16)
        self._sub_buffer = b''
        self._link_server = None  # Set by gateway_core after init
        # Chunks (50 ms each) to accumulate before draining. This is the
        # dominant tunable in end-to-end latency: 4 = 200 ms of cushion.
        # A wired LAN endpoint can usually run 2; keep 4+ for WiFi or a
        # Cloudflare-tunnelled endpoint. Tune with evidence, not taste —
        # rg_link_audio_underruns_total counts what a too-low value costs.
        # Per-endpoint override lives in link_endpoint_settings and is
        # applied by set_jitter_prefill() once the endpoint registers.
        self._jitter_prefill = self._clamp_prefill(
            getattr(config, 'LINK_JITTER_PREFILL', 4))
        self._last_push_mono = time.monotonic()  # track last audio arrival for underrun timeout
        # Device identity — set by _link_on_register, used by all consumers
        self.source_id = None      # routing source ID, e.g. "d75", "ftm_150"
        self.sink_id = None        # routing TX sink ID, e.g. "d75_tx", "ftm_150_tx"
        self.plugin_type = None    # device class from REGISTER, e.g. "d75", "aioc", "audio"
        self._jitter_primed = False

    def _clamp_prefill(self, value):
        """Keep prefill in [1, queue_maxlen-1].

        0 would defeat the buffer entirely — every scheduling hiccup becomes
        a dropout. Above maxlen the queue can never reach the threshold, so
        the source would never prime and would stay silent forever. Exactly
        maxlen does prime, but only with the queue completely full, i.e. at
        the point where the next push starts dropping the oldest chunk — so
        the ceiling is maxlen-1 to keep one chunk of headroom.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = 4
        return max(1, min(v, (self._chunk_queue.maxlen or 16) - 1))

    def set_jitter_prefill(self, value):
        """Change the prefill depth at runtime (per-endpoint tuning).

        Takes effect on the next prime — i.e. after the current transmission
        ends — so it never chops audio that is already flowing.
        """
        old = self._jitter_prefill
        self._jitter_prefill = self._clamp_prefill(value)
        if self._jitter_prefill != old:
            print(f"  [Link:{self.endpoint_name}] jitter prefill "
                  f"{old} -> {self._jitter_prefill} chunks "
                  f"({self._jitter_prefill * 50} ms cushion)")
        return self._jitter_prefill

    def setup_audio(self):
        return True

    def flush_buffers(self):
        """Flush jitter buffer and sub-buffer. Called on playback stop or bus change."""
        self._chunk_queue.clear()
        self._sub_buffer = b''
        self._jitter_primed = False
        self._last_push_mono = time.monotonic()

    def push_audio(self, pcm):
        """Called by GatewayLinkServer reader thread when AUDIO frame arrives."""
        _st = getattr(self, '_stream_trace', None)
        _qd = len(self._chunk_queue)
        self._chunk_queue.append(pcm)
        if not self._jitter_primed and len(self._chunk_queue) >= self._jitter_prefill:
            self._jitter_primed = True
        if _st and _st.active:
            _extra = f'overflow' if _qd >= self._chunk_queue.maxlen else ''
            _st.record(f'{self.endpoint_name}_rx', 'push_audio', pcm, _qd, _extra)
        try:
            self.audio_level = pcm_level(pcm, self.audio_level)
            self._audio_level_last_mono = time.monotonic()
            self._last_push_mono = time.monotonic()
        except Exception:
            pass

    def meter_level(self):
        # When this source is a sink-only endpoint (not a member of any bus),
        # get_audio() is never called, so audio_level never decays. Return 0
        # if no push_audio in the last 250 ms so the dashboard meter falls
        # back to silence instead of freezing at the last pre-squelch value.
        if time.monotonic() - getattr(self, '_audio_level_last_mono', 0) > 0.25:
            return 0
        return self.audio_level

    def get_audio(self, chunk_size):
        _st = getattr(self, '_stream_trace', None)
        if not self.enabled or self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False
        if not self.server_connected:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            self._jitter_primed = False
            return None, False

        # Jitter buffer: wait for prefill before starting to drain
        if not self._jitter_primed:
            return None, False

        cb = self._chunk_bytes
        while len(self._sub_buffer) < cb:
            try:
                blob = self._chunk_queue.popleft()
                self._sub_buffer += blob
            except IndexError:
                self.audio_level = max(0, int(self.audio_level * 0.7))
                # If no audio has arrived for >2s, the source went legitimately
                # quiet (e.g. squelch gating). Drop the primed flag so we stop
                # counting underruns until audio resumes and re-primes.
                if time.monotonic() - self._last_push_mono > 2.0:
                    self._jitter_primed = False
                    self._sub_buffer = b''
                    return None, False
                if _st and _st.active:
                    _st.record(f'{self.endpoint_name}_rx', 'get_audio', None,
                               0, 'UNDERRUN')
                try:
                    import metrics as _m
                    _m.link_audio_underruns_total.labels(endpoint=self.endpoint_name).inc()
                except Exception:
                    pass
                return None, False

        raw = self._sub_buffer[:cb]
        self._sub_buffer = self._sub_buffer[cb:]

        if _st and _st.active:
            _st.record(f'{self.endpoint_name}_rx', 'get_audio', raw,
                       len(self._chunk_queue))

        # Level metering — no VAD gate here, bus handles that
        self.audio_level = pcm_level(raw, self.audio_level, gain=self.display_gain)

        # Audio boost
        if self.audio_boost != 1.0:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
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
                self.tx_audio_level = pcm_level(pcm, self.tx_audio_level)
                _st = getattr(self, '_stream_trace', None)
                if _st and _st.active:
                    _st.record(f'{self.endpoint_name}_tx', 'put_audio', pcm)
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
        self.audio_level = pcm_level(raw, self.audio_level)
        db = pcm_db(raw)

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
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
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
        try:
            self.audio_level = pcm_level(pcm_bytes, self.audio_level)
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

        # Level metering + VAD
        try:
            self.audio_level = pcm_level(data, self.audio_level)
            vad_pass = pcm_db(data) > self.vad_threshold_db
        except Exception:
            vad_pass = False
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


class StreamOutputSource:
    """Direct Icecast streaming — PCM → ffmpeg MP3 → Icecast HTTP SOURCE.

    Replaces the old DarkIce/FFmpeg/ALSA loopback chain with a single
    in-process pipeline. No external processes needed.
    """
    SILENCE_INTERVAL = 0.05  # seconds between silence frames (50ms = 20 ticks/sec)
    SUPERVISOR_INTERVAL = 10.0  # seconds between independent stream-health checks

    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        self.connected = False
        self._encoder = None      # ffmpeg subprocess
        self._icecast_sock = None  # TCP socket to Icecast
        # Dual-channel (Broadcastify two-scanner) feed. When on, the bus manager
        # combines the broadcastify_l / broadcastify_r sinks into interleaved
        # stereo before enqueueing, and the encoder is told to expect 2 channels.
        # Broadcastify specifies 16 kbps per scanner, so a dual feed is 32 kbps
        # total — the same per-channel rate as the mono feed, not a step up in
        # quality per channel.
        self._dual = bool(getattr(config, 'STREAM_DUAL_CHANNEL', True))
        self._channels = 2 if self._dual else 1
        # Keepalive silence must match whatever the encoder expects, so it
        # doubles in dual mode. Getting this wrong desynchronises the channels
        # for the rest of the connection: a half-length write leaves the
        # encoder mid-frame and every later sample lands in the wrong channel.
        self._chunk_bytes = config.AUDIO_CHUNK_SIZE * 2 * self._channels
        self._lock = threading.Lock()
        # Serialise writes to the ffmpeg encoder's stdin. Two threads write
        # PCM into it: send_audio() (called from BusManager / sink-drain
        # thread) and _keepalive_loop (silence when idle). Concurrent writes
        # interleave bytes mid-sample and the MP3 output decodes as static
        # / "silence" — see v3.5-A regression notes (2026-05-02).
        self._encoder_lock = threading.Lock()
        # Guards the _reconnecting flag only. Deliberately separate from
        # _encoder_lock so the recovery path can never be blocked by a writer
        # that is stuck in the encoder — that coupling is what turned a
        # routine socket drop into a 15h outage on 2026-07-30.
        self._reconnect_lock = threading.Lock()
        # Serialises the DESTRUCTIVE half of a reconnect (close() + _connect()).
        # _connect() writes shared instance state — _encoder, _icecast_sock,
        # connected — so two workers running it at once corrupt each other:
        # one connects, the other's close() drops that fresh connection, and
        # its own connect is then refused "403 Mountpoint in use" because the
        # server still holds the mount it just dropped. That is precisely the
        # 2026-08-19 flap: 1850 attempts over 3h42, attempt numbers printing
        # out of order (#79, #83, #72, #85) because ~8 workers were live at
        # once. _reconnect_lock guards a bool and is held for microseconds;
        # this one is held across a network round trip, so they must stay
        # separate or the trigger path would block on the connect path.
        self._connect_lock = threading.Lock()
        # How long a worker waits for _connect_lock before retiring. Longer
        # than the wedge timeout so a worker normally outlasts a slow
        # predecessor, but bounded so threads cannot pile up without limit.
        self._connect_lock_wait = 45.0
        # Bumped whenever the watchdog force-releases _reconnecting out from
        # under a still-live worker. A worker whose captured epoch no longer
        # matches has been superseded and must retire without touching the
        # connection. See _trigger_reconnect's watchdog for why.
        self._reconnect_epoch = 0
        # Instrumentation for the paths added 2026-08-20 — without these the
        # only evidence that the fix is working is an absence of log spam,
        # which is indistinguishable from the feature never running.
        self._reconnect_superseded = 0
        self._reconnect_wedged = 0
        # Watchdog patience before a reconnect worker is declared wedged.
        # This is the BASE budget: _connect_confirm is added to it
        # in _trigger_reconnect: the confirmation wait happens inside the
        # worker, and a watchdog that fires because of it would declare every
        # slow-but-fine reconnect "wedged".
        self._reconnect_wedge_timeout = 30.0
        self._reader_thread = None
        self._keepalive_thread = None
        self._last_audio_time = 0  # monotonic time of last real audio push
        self._bytes_sent = 0
        # Monotonic time of the last byte written to the Icecast socket.
        # 0 until the first byte of the first connection goes out.
        self._last_bytes_time = 0.0
        self._connect_time = 0
        self._reconnect_backoff = 5
        # Longer wait used only after a "403 Mountpoint in use" rejection, i.e.
        # when the server is still holding our own previous source connection.
        # Measured clear time was under 10s; 15 gives headroom without making
        # ordinary reconnects sluggish. Configurable because a different Icecast
        # host may reap sources on a different schedule.
        self._mount_wait = float(getattr(config, 'STREAM_MOUNT_WAIT', 15.0))
        # True when the server may still be holding our previous source
        # connection: set on a detected drop AND on a 403 mount-in-use refusal,
        # cleared by a successful connect.
        self._mount_in_use = False
        # Last failure, surfaced on the dashboard. Kept even after a
        # successful reconnect: 'it broke at 03:12 and recovered' is the
        # thing you want to see hours later, and clearing it on recovery
        # would hide every transient drop that happened overnight.
        self._last_error = ''
        self._last_error_time = 0.0   # unix epoch, 0 = never
        self._reconnect_count = 0
        self._last_drop_time = 0   # monotonic time of last connection drop
        self._was_connected = False  # True once first successful connection
        # Reconnect-in-flight guard. Previously this only ever existed via
        # getattr(self, '_reconnecting', False), so it was invisible in
        # __init__ and easy to miss when reasoning about the recovery path.
        self._reconnecting = False
        # Supervisor thread — the reconnect trigger of last resort. See
        # _supervisor_loop for why keepalive alone was not enough.
        self._supervisor_thread = None
        # Set once the gateway is shutting this source down for good. Stops
        # the reader/supervisor from helpfully resurrecting the stream while
        # the process is on its way out.
        self._shutdown = False
        # True while a teardown we asked for is in progress (close(), and
        # therefore reconnect() too). The reader thread dies as a *result* of
        # such a teardown, so without this it would treat our own deliberate
        # close as a drop and queue a second, redundant reconnect.
        self._teardown_intentional = False
        # Hard ceiling on how long a single write into the encoder may take
        # before we declare the encoder wedged and tear it down. See
        # _encoder_write. Must stay well under STREAM_MOUNT_WAIT so a wedged
        # encoder is reaped long before the reconnect delay elapses.
        self._encoder_write_timeout = float(
            getattr(config, 'STREAM_ENCODER_WRITE_TIMEOUT', 1.0))
        # How long a fresh connection gets to push its first byte before we
        # stop calling it a success. The keepalive feeds the encoder every
        # SILENCE_INTERVAL (50 ms) whether or not the radio is busy, so a
        # healthy 32 kbps mount hands the reader a 4096-byte chunk about once
        # a second -- 5 s is ~5x that, and a quiet channel is NOT a reason for
        # this to come up empty.
        self._connect_confirm = float(
            getattr(config, 'STREAM_CONNECT_CONFIRM', 5.0))
        # Longest gap between outgoing bytes that still counts as flowing.
        # Compared against the same ~1 s cadence, so this is 15x margin.
        self._flow_stale_after = float(
            getattr(config, 'STREAM_FLOW_STALE_AFTER', 15.0))

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
        # ice-description is listed in the Icecast SOURCE spec alongside
        # ice-name/ice-genre/ice-url; we had the config value all along
        # and simply never sent it.
        description = getattr(self.config, 'STREAM_DESCRIPTION', '') or name
        # Encoder output sample rate. MUST stay below 32 kHz: at 32/44.1/48 kHz
        # the encoder is MPEG-1 Layer III, whose LOWEST Layer III bitrate is
        # 32 kbps, so lame silently clamps anything smaller and -b:a is ignored.
        # Broadcastify specifies 16 kbps for a single scanner (32 kbps for a
        # dual-channel feed, i.e. the same 16k per scanner), and 16 kbps only
        # exists in MPEG-2 Layer III — 16/22.05/24 kHz. Measured 2026-07-28:
        #   48 kHz  asked 16k -> 32.3 kbps   (and asked 32k -> 32.3 kbps too)
        #   22.05kHz asked 16k -> 16.3 kbps  <- on spec
        # 22.05 kHz carries 11 kHz of audio; narrowband FM voice is ~3 kHz, so
        # nothing audible is lost — the old 48 kHz feed was spending half its
        # bitrate on empty spectrum the radio never produced.
        out_rate = int(getattr(self.config, 'STREAM_SAMPLE_RATE', 22050))

        if not server or not password:
            print("  ⚠ Broadcastify: missing server or password")
            self._note_error("missing server or password")
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
                f"ice-description: {description}\r\n"
                # ice-audio-info is how a source DECLARES its format. Without it
                # the server has only the MP3 frames to go on: Broadcastify's
                # feed page showed Sample Rate 0, Bitrate 0, Channels 1 — those
                # zeros are "unknown", not a misread, since decoding the frames
                # would have yielded 22050 and 32. The audio was always right
                # (verified 2026-07-28 against the live mount: channels=2,
                # audio on L while R stayed at exactly zero).
                #
                # BOTH key dialects are sent. Icecast's own docs give two
                # real-world examples with different names —
                #   LadioCast: samplerate=44100;quality=10%2e0;channels=2
                #   Butt:      ice-bitrate=128;ice-channels=2;ice-samplerate=44100
                # — and Icecast passes the pairs through rather than
                # normalising them.
                #
                # CONFIRMED 2026-07-28: Broadcastify reads the ice- prefixed
                # (Butt) dialect. A build sending ONLY those keys took their
                # feed page from Sample Rate 0 / Bitrate 0 / Channels 1 to
                # 22050 / 32 / 2. The bare keys are therefore not required for
                # Broadcastify and are kept only so this works against Icecast
                # servers that read the other dialect — this is a public
                # project and not everyone streams to Broadcastify. Unknown
                # keys are ignored by any parser, so the cost is a few bytes
                # once per connect.
                f"ice-audio-info: samplerate={out_rate};channels={self._channels};"
                f"bitrate={bitrate};ice-samplerate={out_rate};"
                f"ice-channels={self._channels};ice-bitrate={bitrate}\r\n"
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
                self._note_error(f"Icecast rejected: {resp_str.splitlines()[0].strip()}")
                # "403 Mountpoint in use" is not a real failure — it means the
                # server has not yet reaped OUR previous source connection, so
                # retrying sooner cannot possibly work. Flag it so the reconnect
                # worker waits for the mount to clear instead of burning an
                # attempt. Any other rejection (bad password, wrong mount) is a
                # genuine error and keeps the short retry.
                self._mount_in_use = ('mountpoint in use' in resp_str.lower())
                sock.close()
                return
            self._mount_in_use = False

            sock.settimeout(None)
            self._icecast_sock = sock

        except Exception as e:
            print(f"  ⚠ Broadcastify: connection failed: {e}")
            self._note_error(f"connection failed: {e}")
            return

        # Start ffmpeg MP3 encoder: PCM stdin → MP3 stdout
        import subprocess as sp
        try:
            self._encoder = sp.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                # Input: raw PCM as the gateway produces it — 48 kHz, mono, or
                # interleaved stereo when the dual-channel feed is enabled.
                '-f', 's16le', '-ar', '48000', '-ac', str(self._channels),
                '-i', 'pipe:0',
                # Output: resampled — this -ar is AFTER -i so it sets the
                # ENCODER rate, not the input rate. Without it the encoder runs
                # at 48 kHz, forcing MPEG-1 and a 32 kbps floor. See out_rate.
                '-c:a', 'libmp3lame', '-ar', str(out_rate), '-b:a', f'{bitrate}k',
            ] + ([
                # Two independent receivers are UNCORRELATED, so joint stereo
                # (which codes mid/side to exploit L/R similarity) wastes bits
                # and can smear one channel into the other. Plain stereo keeps
                # them separate, which is the whole point of a dual feed.
                '-joint_stereo', '0',
            ] if self._dual else []) + [
                '-flush_packets', '1',
                '-fflags', '+nobuffer',
                '-f', 'mp3', 'pipe:1'
            ], stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE)
            # Make the encoder's stdin NON-BLOCKING. A blocking write into a
            # full pipe is what deadlocked the whole stream on 2026-07-30:
            # ffmpeg stopped draining stdin (because nobody was draining its
            # stdout after the reader thread died), the sink thread parked
            # forever inside stdin.write() while holding _encoder_lock, and
            # the keepalive thread — the only thing that can trigger a
            # reconnect — parked behind that same lock. 15h of dead air with
            # _reconnect_count stuck at 0. With O_NONBLOCK the write fails
            # fast instead, and _encoder_write turns that into a teardown.
            try:
                import fcntl
                _fd = self._encoder.stdin.fileno()
                fcntl.fcntl(_fd, fcntl.F_SETFL,
                            fcntl.fcntl(_fd, fcntl.F_GETFL) | os.O_NONBLOCK)
            except Exception as e:
                # Not fatal — we still have the deadline in _encoder_write,
                # it just degrades to a blocking write we cannot interrupt.
                print(f"  ⚠ Broadcastify: could not set encoder stdin non-blocking: {e}")
            self._start_encoder_stderr_reader()
        except Exception as e:
            print(f"  ⚠ Broadcastify: ffmpeg encoder failed: {e}")
            self._note_error(f"ffmpeg encoder failed: {e}")
            self._icecast_sock.close()
            self._icecast_sock = None
            return

        # Prometheus label for this stream. Resolved once per connect rather
        # than per chunk — it cannot change without reconnecting.
        _metric_stream = mount or 'icecast'

        # Reader thread: reads MP3 from ffmpeg, sends to Icecast
        # Bind this reader to the encoder and socket of THIS connection.
        # It used to reach through self._encoder / self._icecast_sock on every
        # iteration, so after a reconnect swapped those fields a surviving
        # reader from the previous connection would read the NEW encoder's
        # stdout and push it into the NEW socket — two readers draining one
        # pipe, interleaving MP3 frames. Defaults capture the values now.
        def _reader(enc=self._encoder, sock=self._icecast_sock):
            while enc.poll() is None:
                try:
                    data = enc.stdout.read(4096)
                    if not data:
                        break
                    with self._lock:
                        if self._icecast_sock is sock:
                            sock.sendall(data)
                            self._bytes_sent += len(data)
                            # Timestamp of the last byte that actually reached
                            # the server. `connected` only ever meant "the
                            # SOURCE handshake was accepted", which is not the
                            # same claim — see _confirm_bytes_moving.
                            self._last_bytes_time = time.monotonic()
                            # Count bytes HERE, at the point they actually go
                            # out. This used to be done in
                            # stream_stats.get_stream_stats(), which has no
                            # background caller — it only runs when a web
                            # request asks for gateway status. The counter
                            # therefore sat flat between dashboard loads and
                            # then jumped by the whole backlog at once (seen
                            # 2026-07-28: flat 20 min, then +86,691,840 in one
                            # step), so rate() read 0 for long stretches. That
                            # made the 'broadcastify_stream_down' alert in
                            # alerts.py fire 4-11 times a day on a perfectly
                            # healthy stream, and made
                            # manager_engine's stream_throughput_kbps equally
                            # wrong. Never derive a rate metric from a
                            # pull-driven status call.
                            try:
                                import metrics as _m
                                _m.stream_bytes_sent_total.labels(
                                    stream=_metric_stream).inc(len(data))
                            except Exception:
                                pass  # metrics must never break the stream
                except (BrokenPipeError, OSError, ConnectionError) as e:
                    uptime_s = int(time.time() - self._connect_time) if self._connect_time else 0
                    print(f"  [Broadcastify] Connection lost after {uptime_s}s: {e}")
                    self._note_error(f"connection lost after {uptime_s}s: {e}")
                    self.connected = False
                    self._last_drop_time = time.monotonic()
                    # Our source connection just died, so the server may still
                    # be holding the mount. Assume so until a connect proves
                    # otherwise — retrying before it is reaped only earns a
                    # "403 Mountpoint in use" and burns an attempt.
                    self._mount_in_use = True
                    break
                except Exception as e:
                    print(f"  [Broadcastify] Reader error: {e}")
                    self._note_error(f"reader error: {e}")
                    break
            # A reader belongs to exactly ONE connection. By the time this one
            # unwinds, a reconnect may already have published a newer encoder —
            # and then everything below would be acting on a connection this
            # thread has nothing to do with: flipping `connected` false, killing
            # the live encoder and queueing yet another reconnect. Whose reader
            # would in turn do the same to ITS successor.
            #
            # That is the self-sustaining half of the 2026-08-19 flap: 1850
            # reconnects over 3h42, of which only 26 were watchdog-driven. The
            # watchdog race started it; this is what kept it going, one
            # "Reconnected successfully" immediately murdered by the previous
            # connection's reader, ~900 times over. A stale reader must exit
            # silently and let the current generation live.
            if self._encoder is not enc:
                return
            # Clean up on exit
            if self.connected:
                uptime_s = int(time.time() - self._connect_time) if self._connect_time else 0
                print(f"  [Broadcastify] Reader thread exited (was connected {uptime_s}s)")
                self._last_drop_time = time.monotonic()
                self._mount_in_use = True   # same reasoning as above
            self.connected = False
            # Reap the encoder. THIS thread is ffmpeg's only stdout consumer,
            # so once it exits ffmpeg's 64 KiB stdout pipe fills, ffmpeg
            # blocks on write, and it therefore stops reading stdin — which
            # in turn wedges every PCM writer feeding it. Leaving the encoder
            # alive here is what deadlocked the stream on 2026-07-30 (ffmpeg
            # was still running 40h later, holding both pipes). Kill it as
            # part of the same drop that killed this thread.
            self._teardown_encoder()
            # And make sure something is actually driving recovery: the
            # supervisor would catch this within SUPERVISOR_INTERVAL anyway,
            # but asking here makes reconnection immediate on the common path.
            # Skipped when WE tore the encoder down — this thread dying is the
            # expected consequence of close()/reconnect(), not a fault.
            if not self._teardown_intentional:
                self._trigger_reconnect()

        self._reader_thread = threading.Thread(target=_reader, daemon=True,
                                                name="Broadcastify-sender")
        self._reader_thread.start()
        # A live encoder again — any future reader death is a genuine fault.
        self._teardown_intentional = False
        self.connected = True
        self._was_connected = True
        self._connect_time = time.time()
        self._last_audio_time = time.monotonic()
        self._bytes_sent = 0
        # Start the flow clock now: a connection one tick old has not gone
        # stale, it simply has not had time to push anything yet.
        self._last_bytes_time = time.monotonic()

        # Keepalive: feed silence to encoder when no real audio arrives
        if not self._keepalive_thread or not self._keepalive_thread.is_alive():
            self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True,
                                                       name="Broadcastify-keepalive")
            self._keepalive_thread.start()
        # Independent recovery supervisor — see _supervisor_loop. Started here
        # rather than in __init__ so it only exists once streaming is actually
        # live, and guarded the same way as the keepalive thread so repeated
        # reconnects do not stack up copies of it.
        if not self._supervisor_thread or not self._supervisor_thread.is_alive():
            self._supervisor_thread = threading.Thread(target=self._supervisor_loop,
                                                       daemon=True,
                                                       name="Broadcastify-supervisor")
            self._supervisor_thread.start()
        print(f"  ✓ Broadcastify: direct Icecast stream to {server}:{port}{mount} ({bitrate}kbps)")

    def _encoder_write(self, data):
        """Write PCM into the encoder under a hard deadline.

        Returns True if every byte went in, False if the encoder is wedged.

        The encoder's stdin is O_NONBLOCK (set in _connect), so a full pipe
        surfaces as BlockingIOError instead of parking the calling thread
        forever. That is the fix for the 2026-07-30 deadlock: ffmpeg stopped
        draining stdin, and the plain `stdin.write()` this replaces blocked
        indefinitely *while holding _encoder_lock*, taking the recovery path
        down with it.

        A partial write here desynchronises the channels for the rest of the
        connection (see _chunk_bytes), so the caller MUST treat False as
        "reap the encoder and reconnect", never as "retry this frame".
        """
        enc = self._encoder
        if enc is None or enc.stdin is None:
            return False
        try:
            fd = enc.stdin.fileno()
        except Exception:
            return False
        view = memoryview(data)
        deadline = time.monotonic() + self._encoder_write_timeout
        while view:
            try:
                view = view[os.write(fd, view):]
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    select.select([], [fd], [], min(remaining, 0.05))
                except Exception:
                    return False
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True

    def _teardown_encoder(self):
        """Kill the ffmpeg encoder. Deliberately does NOT take _encoder_lock.

        The entire point is to break threads that are already stuck inside
        that lock, so taking it here would deadlock against the very
        condition we are clearing. SIGKILL makes any in-flight write fail
        with EPIPE, which releases the lock for us.
        """
        enc = self._encoder
        if enc is None:
            return
        if self._encoder is enc:
            self._encoder = None
        for step in (enc.kill, enc.stdin.close, enc.stdout.close):
            try:
                step()
            except Exception:
                pass
        try:
            enc.wait(timeout=3)
        except Exception:
            pass

    def _supervisor_loop(self):
        """Reconnect trigger of last resort.

        Recovery used to hang solely off _keepalive_loop — but that thread is
        itself a writer into the encoder, so any condition that wedged the
        encoder also wedged the only thing able to recover from it. On
        2026-07-30 that cost 15h of dead air with _reconnect_count stuck at 0:
        the keepalive thread was parked on _encoder_lock and never looped back
        to re-read `self.connected`.

        This thread touches neither the encoder nor _encoder_lock, so it stays
        responsive no matter what the writer threads are doing.
        """
        while True:
            time.sleep(self.SUPERVISOR_INTERVAL)
            try:
                if self._shutdown:
                    return
                if not self._was_connected or self.connected or self._reconnecting:
                    continue
                # A reconnect worker holding the connect lock may be part-way
                # through building a fresh encoder, which it has not yet
                # published by setting `connected`. Reaping it here would
                # destroy the very recovery we are trying to drive, so leave
                # an in-flight connect alone and re-check next interval.
                if self._connect_lock.locked():
                    continue
                if self._encoder is not None:
                    # Stream is down but the encoder is still alive — writers
                    # may be parked inside it. Reap it before retrying.
                    print("  [Broadcastify] Supervisor: stream down with encoder "
                          "still alive — reaping it")
                    self._teardown_encoder()
                print("  [Broadcastify] Supervisor: stream down — triggering reconnect")
                self._trigger_reconnect()
            except Exception as e:
                print(f"  [Broadcastify] Supervisor error: {e}")

    def _trigger_reconnect(self):
        """Start a reconnect attempt unless one is already in flight.

        Extracted from send_audio so that every detector — the sink thread,
        the keepalive tick and the supervisor — shares one guarded path
        rather than each open-coding the flag handling.
        """
        if not self._was_connected or self._shutdown:
            return
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
            self._reconnect_count += 1
            count = self._reconnect_count
            epoch = self._reconnect_epoch
        try:
            import metrics as _m
            _m.stream_reconnects_total.labels(stream='broadcastify').inc()
        except Exception:
            pass

        def _auto_reconnect():
            try:
                # Wait for the mount to clear when our own source connection
                # may still be held server-side — set both when a drop is
                # detected (the reader thread) and when a connect is refused
                # with "403 Mountpoint in use".
                # Observed 2026-07-27 (11:12 and 11:27): drop -> attempt #1 at
                # +5s refused 403 -> attempt #2 at +10s succeeded. The mount
                # clears somewhere under 10s, so the old flat 5s retry was
                # racing it and spending an attempt to learn nothing. Waiting
                # up front costs ~5s more dead air than the old two-attempt
                # recovery but reconnects on the FIRST try with no error
                # logged. The fast 5s path remains for connects that fail for
                # any other reason, where retrying sooner does help.
                delay = self._mount_wait if self._mount_in_use else self._reconnect_backoff
                if delay != self._reconnect_backoff:
                    print(f"  [Broadcastify] Mount still held by our previous "
                          f"connection — waiting {delay}s for it to clear")
                time.sleep(delay)
                # Everything below mutates shared connection state, so only one
                # worker may be inside it at a time. Waiting here (rather than
                # barging in) is what stops a late worker from closing a
                # connection a peer just established.
                if not self._connect_lock.acquire(timeout=self._connect_lock_wait):
                    self._reconnect_superseded += 1
                    print(f"  [Broadcastify] Attempt #{count} gave up waiting for "
                          f"the connect lock — another attempt is still in flight")
                    return
                try:
                    # Three ways this attempt can already be pointless. Checking
                    # them under the lock is what makes the check meaningful:
                    # the state cannot change while we hold it.
                    if self._shutdown:
                        return
                    if epoch != self._reconnect_epoch:
                        self._reconnect_superseded += 1
                        print(f"  [Broadcastify] Attempt #{count} superseded "
                              f"(epoch {epoch} < {self._reconnect_epoch}) — retiring "
                              f"without touching the connection")
                        return
                    if self.connected:
                        self._reconnect_superseded += 1
                        print(f"  [Broadcastify] Attempt #{count} unnecessary — "
                              f"stream is already back up")
                        return
                    print(f"  [Broadcastify] Auto-reconnecting (attempt #{count})...")
                    try:
                        self.close()
                    except Exception as e:
                        print(f"  [Broadcastify] close() raised during reconnect: {e}")
                    try:
                        self._connect()
                    except Exception as e:
                        print(f"  [Broadcastify] _connect() raised: {e}")
                    if not self.connected:
                        print(f"  [Broadcastify] Reconnect failed (attempt #{count})")
                        self._note_error(f"reconnect failed (attempt #{count})")
                    elif self._confirm_bytes_moving(count):
                        pass   # _confirm_bytes_moving logs the success itself
                    else:
                        # Handshake accepted, nothing moving. Deliberately NOT
                        # torn down here: a blocked write trips the 1 s
                        # _encoder_write deadline and _on_encoder_wedged
                        # already reaps and retries, with _supervisor_loop as
                        # the backstop. Both fired correctly throughout the
                        # 2026-08-21 stall -- the only thing broken was this
                        # message calling it a success. A teardown here would
                        # be a third recovery path racing the two that work.
                        print(f"  [Broadcastify] Attempt #{count} connected but "
                              f"no data moved in {self._connect_confirm:g}s — "
                              f"NOT counting this as recovered")
                        self._note_error(
                            f"attempt #{count} connected but pushed 0 bytes in "
                            f"{self._connect_confirm:g}s")
                finally:
                    self._connect_lock.release()
            finally:
                # Only clear the in-flight flag if we are still the current
                # generation. A superseded worker finishing late must not clear
                # the flag its successor is holding, or a third worker spawns
                # alongside the second and the serialisation is lost.
                with self._reconnect_lock:
                    if epoch == self._reconnect_epoch:
                        self._reconnecting = False

        worker = threading.Thread(target=_auto_reconnect, daemon=True,
                                  name="Broadcastify-reconnect")
        worker.start()

        # Watchdog: if the reconnect worker hangs (e.g. close() wedged on a
        # half-dead pipe), force-release the _reconnecting flag after 30s so a
        # later tick can spawn a fresh attempt. The wedged worker may leak its
        # encoder/socket, but the stream as a whole recovers instead of
        # staying dark.
        def _watchdog():
            budget = self._reconnect_wedge_timeout + self._connect_confirm
            worker.join(timeout=budget)
            if worker.is_alive():
                # Release the flag so recovery is not held hostage by this
                # worker, AND bump the epoch so the worker retires on wake.
                # The old code did only the first half, on the assumption that
                # a wedged worker is effectively dead. It is not always: a
                # worker parked in getaddrinfo during a DNS outage
                # (socket.create_connection's timeout covers the TCP connect,
                # NOT name resolution) comes back minutes later and resumes
                # tearing down whatever connection succeeded meanwhile. That
                # assumption is what turned a 4-minute DNS blip into a
                # 3h42 reconnect flap on 2026-08-19.
                with self._reconnect_lock:
                    self._reconnect_epoch += 1
                    self._reconnecting = False
                    ep = self._reconnect_epoch
                self._reconnect_wedged += 1
                print(f"  [Broadcastify] Reconnect attempt #{count} wedged >"
                      f"{budget:g}s — releasing flag "
                      f"(epoch now {ep}; attempt #{count} will retire on wake)")
        threading.Thread(target=_watchdog, daemon=True,
                         name="Broadcastify-reconnect-wd").start()

    def send_audio(self, audio_data):
        """Send raw PCM audio to the MP3 encoder. Auto-reconnects on failure."""
        if not self.connected or self._encoder is None:
            self._trigger_reconnect()
            return
        # The lock still serialises whole frames against _keepalive_loop, but
        # the write underneath it is now deadline-bounded, so holding it can
        # no longer park this thread (and everything queued behind it)
        # indefinitely.
        with self._encoder_lock:
            ok = self._encoder_write(audio_data)
            if ok:
                self._last_audio_time = time.monotonic()
        if not ok:
            self._on_encoder_wedged()

    def _confirm_bytes_moving(self, count):
        """Wait for a just-established connection to actually push bytes.

        `self.connected` only ever meant "the Icecast SOURCE handshake was
        accepted". That is a much weaker claim than "the stream is back", and
        on 2026-08-21 the difference cost ten minutes of dead air: the uplink
        stalled, ten reconnect attempts each completed their handshake, and
        every one of them logged "Reconnected successfully" while
        rg_stream_bytes_sent_total sat at a flat +0 for 2.5 of those minutes.
        TCP connected, Icecast accepted, not one payload byte moved. The log
        said recovered, the byte counter said dark, and only the counter was
        right -- the incident was invisible in the log and obvious in
        Prometheus.

        _connect() resets _bytes_sent to 0, so any advance seen here belongs
        to THIS connection and cannot be inherited from the last one.

        Returns True (and logs the success) once bytes move, False if the
        window expires, the connection drops again, or we are shutting down.
        """
        started = time.monotonic()
        deadline = started + self._connect_confirm
        while time.monotonic() < deadline:
            if self._shutdown:
                return False
            if not self.connected:
                return False       # dropped again mid-confirmation
            if self._bytes_sent > 0:
                took = time.monotonic() - started
                print(f"  [Broadcastify] Reconnected successfully (attempt "
                      f"#{count}) — {self._bytes_sent} bytes on the wire "
                      f"in {took:.1f}s")
                return True
            time.sleep(0.1)
        return False

    @property
    def data_flowing(self):
        """True when bytes reached the server recently enough to count as up.

        The companion to _confirm_bytes_moving for callers that poll rather
        than watch a single reconnect -- see the health check in
        core/lifecycle.py, which made the same connected-means-healthy
        assumption and so reported "Stream recovered" five times during the
        same stall.
        """
        if not self.connected:
            return False
        if not self._last_bytes_time:
            return False
        return (time.monotonic() - self._last_bytes_time) < self._flow_stale_after

    def _on_encoder_wedged(self):
        """Encoder stopped accepting PCM — reap it and start recovery.

        Called with _encoder_lock RELEASED: _teardown_encoder must not take
        it, and the reconnect worker needs a clean field.
        """
        if not self.connected and self._encoder is None:
            return  # someone else already handled this drop
        print("  [Broadcastify] Encoder stopped accepting audio — reaping and reconnecting")
        self._note_error("encoder wedged (write deadline exceeded)")
        self.connected = False
        self._last_drop_time = time.monotonic()
        # Our source connection is going away, so the server may still hold
        # the mount — same reasoning as the reader thread's drop path.
        self._mount_in_use = True
        self._teardown_encoder()
        self._trigger_reconnect()

    def _start_encoder_stderr_reader(self):
        """Drain and log the encoder's stderr.

        This used to be DEVNULL. When ffmpeg exited the reader loop simply saw
        EOF and logged "Reader thread exited" with no reason — the one piece of
        evidence that would explain an encoder death was being thrown away.

        It MUST be drained, not merely redirected: a PIPE nobody reads fills its
        64 KB kernel buffer and then blocks ffmpeg's next write forever, which
        would wedge the encoder. That is a worse failure than the missing logs,
        so the thread is started immediately after Popen and exits on EOF.

        ffmpeg runs at -loglevel error, so in normal operation this produces
        nothing at all.
        """
        enc = self._encoder
        if enc is None or enc.stderr is None:
            return

        def _drain(pipe):
            try:
                for raw in iter(pipe.readline, b''):
                    line = raw.decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    print(f"  [Broadcastify] ffmpeg: {line}")
                    self._note_error(f"ffmpeg: {line}")
            except Exception:
                pass          # pipe closed under us during shutdown
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threading.Thread(target=_drain, args=(enc.stderr,), daemon=True,
                         name="Broadcastify-ffmpeg-err").start()

    def _note_error(self, msg):
        """Record the most recent stream failure for the dashboard."""
        try:
            self._last_error = str(msg)[:200]
            self._last_error_time = time.time()
        except Exception:
            pass   # diagnostics must never break the stream

    def _keepalive_loop(self):
        """Feed silence to the encoder when no real audio is arriving.

        Icecast servers drop SOURCE connections that go idle.  By sending
        silence frames the MP3 encoder keeps producing a constant bitrate
        stream even when the radio is quiet.
        """
        # One bus tick of silence — matches the chunk size every other
        # source produces, so the encoder sees uniform input regardless of
        # whether real audio or keepalive is flowing.
        _silence = b'\x00' * self._chunk_bytes
        while True:
            time.sleep(self.SILENCE_INTERVAL)
            if not self.connected or self._encoder is None:
                # Keepalive also drives reconnection — send_audio() only fires
                # when the listen bus has audio, so quiet radios never retry.
                # _supervisor_loop is the backstop if this thread is ever
                # unable to reach here.
                self._trigger_reconnect()
                continue
            ok = True
            try:
                # Re-check the idle gate while holding the lock so we never
                # race with send_audio mid-frame; both writers always produce
                # whole 4800-byte frames into the encoder.
                #
                # BOUNDED acquire: an unbounded `with self._encoder_lock` is
                # exactly how this thread was lost on 2026-07-30 — it parked
                # here behind a sink thread stuck in a blocking write and
                # never returned to the `not self.connected` check above, so
                # the reconnect on the previous branch became unreachable.
                if not self._encoder_lock.acquire(timeout=1.0):
                    continue
                try:
                    if time.monotonic() - self._last_audio_time < 0.1:
                        continue
                    ok = self._encoder_write(_silence)
                finally:
                    self._encoder_lock.release()
            except Exception:
                pass
            if not ok:
                self._on_encoder_wedged()

    def reconnect(self):
        """Tear down and reconnect."""
        self.close()
        time.sleep(1)
        self._connect()

    def close(self):
        """Clean shutdown."""
        self.connected = False
        # Tell the reader thread that its imminent death is our doing.
        # Cleared again by a successful _connect(); if the reconnect fails,
        # _supervisor_loop is the backstop and does not consult this flag.
        self._teardown_intentional = True
        # Kills the process FIRST, then closes the pipes. stdin.close() can
        # block indefinitely if the read end is in a half-dead state with
        # buffered bytes — observed wedging the reconnect path for 11+ hours
        # after a Broadcastify socket reset. SIGKILL guarantees the pipe is
        # torn down so the subsequent close() returns immediately.
        self._teardown_encoder()
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
        """Shut the stream down. Alias for close() — the gateway's shutdown
        path calls cleanup() on every source uniformly.

        This used to reference `self.pipe`, an attribute that has never
        existed on this class (it predates the DarkIce->direct-Icecast
        rewrite), so shutdown raised AttributeError here every single time
        and the encoder/socket were left to the OS instead of being closed.
        """
        # Permanent, unlike close() — suppresses the reader/supervisor
        # recovery paths so shutdown cannot race a reconnect back up.
        self._shutdown = True
        self.close()




# CW generation moved to audio_util.py — re-exported above for backward compat



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
