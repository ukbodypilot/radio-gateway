"""Soundboard file playback and loop-recording playback sources."""

from log_util import log_exc
import os
import time
import threading
import subprocess
import queue as _queue_mod
import numpy as np

# Shared level-metering and gain helpers
from audio_util import (
    pcm_rms, pcm_level, apply_gain,
)

from sources.base import AudioSource, bgm_files


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
            log_exc()
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
            log_exc()

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
                        log_exc()
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
                    log_exc()

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
