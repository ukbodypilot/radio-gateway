"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import time

try:
    from pymumble_py3 import Mumble
    from pymumble_py3.callbacks import (
        PYMUMBLE_CLBK_SOUNDRECEIVED,
        PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
    )
except ImportError:
    try:
        from pymumble import Mumble
        from pymumble.callbacks import (
            PYMUMBLE_CLBK_SOUNDRECEIVED,
            PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
        )
    except ImportError:   # off the box; hw_deps.require() stops the service
        Mumble = None
        PYMUMBLE_CLBK_SOUNDRECEIVED = PYMUMBLE_CLBK_TEXTMESSAGERECEIVED = None


class _SetupAudioMumbleMixin:
    def setup_audio(self):
        """Initialize plugins, audio sources, and external services.

        Each phase is a function in gateway_setup.py. They run in order
        because some have dependencies (SDR before TH-9800 for the fork
        safety constraint; TH-9800 before CAT connect because the plugin
        produces the cat_client; tunnel before GDrive because GDrive
        publishes the tunnel URL on startup).

        Phases catch their own exceptions and leave the relevant attribute
        as None on failure — they don't raise. The single outer try/except
        is a last-resort safety net for the orchestrator itself.
        """
        if self.config.VERBOSE_LOGGING:
            print("Initializing audio...")
        try:
            import gateway_setup as gs
            gs.setup_sdr(self)
            gs.setup_th9800(self)
            gs.setup_playback(self)
            gs.setup_tts(self)
            gs.setup_remote_audio(self)
            gs.setup_announce_input(self)
            gs.setup_web_audio(self)
            # setup_gateway_link must run BEFORE kv4p loopback endpoints —
            # the endpoints connect to the link server at 127.0.0.1:9700.
            gs.setup_gateway_link(self)
            gs.setup_kv4p_loopback_endpoints(self)
            gs.setup_packet(self)
            gs.setup_mumble_servers(self)
            gs.setup_smart_announce(self)
            gs.setup_web_config(self)
            gs.setup_manager_engine(self)
            gs.setup_alert_engine(self)
            gs.setup_ddns(self)
            gs.setup_cloudflare_tunnel(self)
            gs.setup_supervised_streamers(self)
            gs.setup_email(self)
            gs.setup_gdrive(self)
            gs.setup_gps(self)
            gs.setup_repeaters(self)
            gs.setup_echolink(self)
            gs.setup_streaming(self)
            gs.setup_cat_connect(self)
            return True
        except Exception as e:
            print(f"✗ Could not initialize audio: {e}")
            import traceback; traceback.print_exc()
            return False

    def setup_mumble(self):
        """Initialize Mumble connection"""

        if self.secondary_mode:
            print()
            print("=" * 60)
            print("  SECONDARY MODE — this machine is not the active gateway")
            print("  Reason: Broadcastify feed already live on another server")
            print("  Mumble: DISABLED (username would conflict)")
            print("  Stream encoder: DISABLED (mountpoint already occupied)")
            print("  Audio bridge (FFmpeg/loopback) still running.")
            print("=" * 60)
            return True

        # Create MumbleSource for routing system
        from audio_sources import MumbleSource
        self.mumble_source = MumbleSource(self.config, gateway=self)
        print(f"\nConnecting to Mumble: {self.config.MUMBLE_SERVER}:{self.config.MUMBLE_PORT}...")

        try:
            # Create Mumble client
            print(f"  Creating Mumble client...")
            self.mumble = Mumble(
                self.config.MUMBLE_SERVER, 
                self.config.MUMBLE_USERNAME,
                port=self.config.MUMBLE_PORT,
                password=self.config.MUMBLE_PASSWORD if self.config.MUMBLE_PASSWORD else '',
                reconnect=False,  # pymumble reconnect causes ghost cycling on local servers
                stereo=self.config.MUMBLE_STEREO,
                debug=self.config.MUMBLE_DEBUG
            )
            
            # Set loop rate for low latency
            self.mumble.set_loop_rate(self.config.MUMBLE_LOOP_RATE)
            
            # Set up callback for received audio
            self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self.sound_received_handler)
            
            # Set up callback for text messages
            if self.config.ENABLE_TEXT_COMMANDS:
                try:
                    self.mumble.callbacks.set_callback(PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self.on_text_message)
                    print("✓ Text message callback registered")
                    print("  Send text commands in Mumble chat (e.g., !status, !help)")
                except Exception as callback_err:
                    print(f"⚠ Text callback registration failed: {callback_err}")
            else:
                print("  Text commands: DISABLED (set ENABLE_TEXT_COMMANDS = true to enable)")
            
            # Enable receiving sound
            self.mumble.set_receive_sound(True)
            
            # Connect
            print(f"  Starting Mumble connection...")
            self.mumble.start()
            
            print(f"  Waiting for Mumble to be ready...")
            self.mumble.is_ready()
            
            print(f"✓ Connected as '{self.config.MUMBLE_USERNAME}'")
            
            # Wait for codec to initialize
            print("  Waiting for audio codec to initialize...")
            max_wait = 5  # seconds
            wait_start = time.time()
            while time.time() - wait_start < max_wait:
                if hasattr(self.mumble.sound_output, 'encoder_framesize') and self.mumble.sound_output.encoder_framesize is not None:
                    print(f"  ✓ Audio codec ready (framesize: {self.mumble.sound_output.encoder_framesize})")

                    break
                time.sleep(0.1)
            else:
                print("  ⚠ Audio codec not initialized after 5s")
                print("    Audio may not work until codec is ready")
                print("    This usually resolves itself within 10-30 seconds")

            # Increase audio_per_packet to bundle more frames per Mumble packet.
            # Default 0.02 (20ms = 1 frame/packet) causes stutter when pymumble's
            # loop is GIL-starved (only fires ~20x/sec instead of 50x/sec).
            # At 0.06 (60ms = 3 frames/packet), 20 sends/sec × 60ms = 1200ms/sec.
            try:
                self.mumble.sound_output.set_audio_per_packet(0.06)
                print(f"  Mumble audio_per_packet set to 0.06 (60ms, 3 frames/packet)")
            except Exception as e:
                print(f"  ⚠ Could not set audio_per_packet: {e}")

            # Apply audio quality settings now that the codec is ready.
            # set_bandwidth() was never called before — the library default is 50kbps.
            # complexity=10: max Opus quality (marginal CPU cost on Pi)
            # signal=3001: OPUS_SIGNAL_VOICE — tunes psychoacoustic model for speech
            try:
                self.mumble.set_bandwidth(self.config.MUMBLE_BITRATE)
                enc = getattr(self.mumble.sound_output, 'encoder', None)
                if enc is not None:
                    enc.vbr = 1 if self.config.MUMBLE_VBR else 0
                    enc.complexity = 10
                    enc.signal = 3001  # OPUS_SIGNAL_VOICE
                    print(f"  ✓ Opus encoder: {self.config.MUMBLE_BITRATE//1000}kbps, "
                          f"VBR={'on' if self.config.MUMBLE_VBR else 'off'}, "
                          f"complexity=10, signal=voice")
                else:
                    print(f"  ✓ Mumble bandwidth set to {self.config.MUMBLE_BITRATE//1000}kbps "
                          f"(VBR will apply when codec negotiates)")
            except Exception as qe:
                print(f"  ⚠ Could not apply audio quality settings: {qe}")

            # Join channel if specified
            if self.config.MUMBLE_CHANNEL:
                try:
                    print(f"  Joining channel: {self.config.MUMBLE_CHANNEL}")
                    channel = self.mumble.channels.find_by_name(self.config.MUMBLE_CHANNEL)
                    if channel:
                        channel.move_in()
                        print(f"  ✓ Joined channel: {self.config.MUMBLE_CHANNEL}")
                    else:
                        print(f"  ⚠ Channel '{self.config.MUMBLE_CHANNEL}' not found")
                        print(f"    Staying in root channel")
                except Exception as ch_err:
                    print(f"  ✗ Could not join channel: {ch_err}")
            
            if self.config.VERBOSE_LOGGING:
                print(f"  Loop rate: {self.config.MUMBLE_LOOP_RATE}s ({1/self.config.MUMBLE_LOOP_RATE:.0f} Hz)")
            
            return True
            
        except Exception as e:
            if 'already in use' in str(e).lower() or 'username already' in str(e).lower():
                self.secondary_mode = True
                print()
                print("=" * 60)
                print("  SECONDARY MODE — this machine is not the active gateway")
                print(f"  Reason: Mumble username '{self.config.MUMBLE_USERNAME}' already connected")
                print("  Mumble: DISABLED (username conflict)")
                print("  Hint: the stream encoder may also fail if the Broadcastify feed is already live.")
                print("=" * 60)
                return True
            print(f"\n✗ MUMBLE CONNECTION FAILED: {e}")
            print(f"\n  Configuration:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            print(f"    Username: {self.config.MUMBLE_USERNAME}")
            print(f"\n  Please check:")
            print(f"  1. Is the Mumble server running?")
            print(f"  2. Is the IP address correct in gateway_config.txt?")
            print(f"  3. Is the port correct? (default: 64738)")
            print(f"  4. Can you connect with the official Mumble client?")
            print(f"\n  Test with Mumble client first:")
            print(f"    Server: {self.config.MUMBLE_SERVER}")
            print(f"    Port: {self.config.MUMBLE_PORT}")
            return False
    
    # gTTS voice map: number → (lang, tld, description)
    # gTTS voices (Google Translate, robotic but reliable)
    # gTTS voices — (lang, tld, label). The tld picks the ACCENT for a given
    # language, which is why 'en' appears many times.
    # 1-9 keep their indices: TTS_DEFAULT_VOICE is a number, so renumbering
    # would silently change a user's configured voice. Append only.
    TTS_VOICES = {
        1: ('en', 'com',                  'US English'),
        2: ('en', 'co.uk',                'British English'),
        3: ('en', 'com.au',               'Australian English'),
        4: ('en', 'co.in',                'Indian English'),
        5: ('en', 'co.za',                'South African English'),
        6: ('en', 'ca',                   'Canadian English'),
        7: ('en', 'ie',                   'Irish English'),
        8: ('fr', 'fr',                   'French'),
        9: ('de', 'de',                   'German'),
        10: ('en', 'com.ng',              'Nigerian English'),
        11: ('en', 'com.ph',              'Philippine English'),
        12: ('es', 'es',                  'Spanish (Spain)'),
        13: ('es', 'com.mx',              'Spanish (Mexico)'),
        14: ('it', 'it',                  'Italian'),
        15: ('pt', 'com.br',              'Portuguese (Brazil)'),
        16: ('nl', 'nl',                  'Dutch'),
        17: ('pl', 'pl',                  'Polish'),
        18: ('sv', 'se',                  'Swedish'),
        19: ('ja', 'co.jp',               'Japanese'),
        20: ('ko', 'co.kr',               'Korean'),
        21: ('hi', 'co.in',               'Hindi'),
        22: ('ru', 'ru',                  'Russian'),
    }

    # Edge TTS voices (Microsoft Neural, natural sounding).
    # 1-9 are the original set and MUST keep their indices — TTS_DEFAULT_VOICE
    # in gateway_config.txt is a number, so renumbering silently changes a
    # user's configured voice. Append new voices only.
    # Labels carry Microsoft's own VoicePersonalities tag where it is useful
    # (Ana is genuinely tagged Cartoon/Cute; Christopher is tagged Authority).
    EDGE_TTS_VOICES = {
        1: ('en-US-AndrewNeural',                         'Andrew (US M) — Confident'),
        2: ('en-GB-RyanNeural',                           'Ryan (GB M)'),
        3: ('en-AU-WilliamMultilingualNeural',            'William ML (AU M)'),
        4: ('en-IN-PrabhatNeural',                        'Prabhat (IN M)'),
        5: ('en-US-GuyNeural',                            'Guy (US M) — Passion'),
        6: ('en-CA-LiamNeural',                           'Liam (CA M)'),
        7: ('en-IE-ConnorNeural',                         'Connor (IE M)'),
        8: ('en-US-AvaNeural',                            'Ava (US F) — Expressive'),
        9: ('en-US-EmmaNeural',                           'Emma (US F) — Cheerful'),
        10: ('en-US-AnaNeural',                           'Ana (US F) — Cartoon'),
        11: ('en-US-AriaNeural',                          'Aria (US F) — Confident'),
        12: ('en-US-AvaMultilingualNeural',               'Ava ML (US F) — Expressive'),
        13: ('en-US-EmmaMultilingualNeural',              'Emma ML (US F) — Cheerful'),
        14: ('en-US-JennyNeural',                         'Jenny (US F) — Comfort'),
        15: ('en-US-MichelleNeural',                      'Michelle (US F) — Pleasant'),
        16: ('en-US-AndrewMultilingualNeural',            'Andrew ML (US M) — Confident'),
        17: ('en-US-BrianMultilingualNeural',             'Brian ML (US M) — Casual'),
        18: ('en-US-BrianNeural',                         'Brian (US M) — Casual'),
        19: ('en-US-ChristopherNeural',                   'Christopher (US M) — Authority'),
        20: ('en-US-EricNeural',                          'Eric (US M) — Rational'),
        21: ('en-US-RogerNeural',                         'Roger (US M) — Lively'),
        22: ('en-US-SteffanNeural',                       'Steffan (US M) — Rational'),
        23: ('en-AU-NatashaNeural',                       'Natasha (AU F)'),
        24: ('en-CA-ClaraNeural',                         'Clara (CA F)'),
        25: ('en-GB-LibbyNeural',                         'Libby (GB F)'),
        26: ('en-GB-MaisieNeural',                        'Maisie (GB F)'),
        27: ('en-GB-SoniaNeural',                         'Sonia (GB F)'),
        28: ('en-GB-ThomasNeural',                        'Thomas (GB M)'),
        29: ('en-HK-YanNeural',                           'Yan (HK F)'),
        30: ('en-HK-SamNeural',                           'Sam (HK M)'),
        31: ('en-IE-EmilyNeural',                         'Emily (IE F)'),
        32: ('en-IN-NeerjaExpressiveNeural',              'NeerjaExpressive (IN F)'),
        33: ('en-IN-NeerjaNeural',                        'Neerja (IN F)'),
        34: ('en-KE-AsiliaNeural',                        'Asilia (KE F)'),
        35: ('en-KE-ChilembaNeural',                      'Chilemba (KE M)'),
        36: ('en-NG-EzinneNeural',                        'Ezinne (NG F)'),
        37: ('en-NG-AbeoNeural',                          'Abeo (NG M)'),
        38: ('en-NZ-MollyNeural',                         'Molly (NZ F)'),
        39: ('en-NZ-MitchellNeural',                      'Mitchell (NZ M)'),
        40: ('en-PH-RosaNeural',                          'Rosa (PH F)'),
        41: ('en-PH-JamesNeural',                         'James (PH M)'),
        42: ('en-SG-LunaNeural',                          'Luna (SG F)'),
        43: ('en-SG-WayneNeural',                         'Wayne (SG M)'),
        44: ('en-TZ-ImaniNeural',                         'Imani (TZ F)'),
        45: ('en-TZ-ElimuNeural',                         'Elimu (TZ M)'),
        46: ('en-ZA-LeahNeural',                          'Leah (ZA F)'),
        47: ('en-ZA-LukeNeural',                          'Luke (ZA M)'),
    }

    # Kokoro voices — voice_id → human label
    # lang_code is the first char of the voice_id (a=US, b=GB, j=JA, z=ZH, e=ES, f=FR, h=HI, i=IT, p=PT)
    KOKORO_VOICES = {
        # American English
        'af_heart':    'Heart (US F) ★★★★',
        'af_bella':    'Bella (US F) ★★★½',
        'af_nicole':   'Nicole (US F) ★★★',
        'af_aoede':    'Aoede (US F) ★★½',
        'af_kore':     'Kore (US F) ★★½',
        'af_sarah':    'Sarah (US F) ★★½',
        'af_nova':     'Nova (US F) ★★',
        'af_alloy':    'Alloy (US F) ★★',
        'af_isabella': 'Isabella (US F) ★★',
        'af_jessica':  'Jessica (US F) ★',
        'af_river':    'River (US F) ★',
        'af_sky':      'Sky (US F) ★',
        'am_michael':  'Michael (US M) ★★½',
        'am_fenrir':   'Fenrir (US M) ★★½',
        'am_puck':     'Puck (US M) ★★½',
        'am_echo':     'Echo (US M) ★',
        'am_eric':     'Eric (US M) ★',
        'am_liam':     'Liam (US M) ★',
        'am_onyx':     'Onyx (US M) ★',
        'am_santa':    'Santa (US M) ★',
        'am_adam':     'Adam (US M) ★',
        # British English
        'bf_emma':     'Emma (GB F) ★★★',
        'bf_isabella': 'Isabella (GB F) ★★',
        'bf_alice':    'Alice (GB F) ★',
        'bf_lily':     'Lily (GB F) ★',
        'bm_lewis':    'Lewis (GB M) ★½',
        'bm_fable':    'Fable (GB M) ★★',
        'bm_george':   'George (GB M) ★★',
        'bm_daniel':   'Daniel (GB M) ★',
        # Japanese
        'jf_alpha':    'Alpha (JA F) ★★½',
        'jf_gongitsune': 'Gongitsune (JA F) ★★',
        'jf_tebukuro': 'Tebukuro (JA F) ★★',
        'jf_nezumi':   'Nezumi (JA F) ★½',
        'jm_kumo':     'Kumo (JA M) ★½',
        # Mandarin
        'zf_xiaobei':  'Xiaobei (ZH F) ★',
        'zf_xiaoni':   'Xiaoni (ZH F) ★',
        'zf_xiaoxiao': 'Xiaoxiao (ZH F) ★',
        'zf_xiaoyi':   'Xiaoyi (ZH F) ★',
        'zm_yunjian':  'Yunjian (ZH M) ★',
        'zm_yunxi':    'Yunxi (ZH M) ★',
        'zm_yunxia':   'Yunxia (ZH M) ★',
        'zm_yunyang':  'Yunyang (ZH M) ★',
        # Spanish
        'ef_dora':     'Dora (ES F)',
        'em_alex':     'Alex (ES M)',
        'em_santa':    'Santa (ES M)',
        # French
        'ff_siwis':    'Siwis (FR F) ★★★',
        # Hindi
        'hf_alpha':    'Alpha (HI F) ★★',
        'hf_beta':     'Beta (HI F) ★★',
        'hm_omega':    'Omega (HI M) ★★',
        'hm_psi':      'Psi (HI M) ★★',
        # Italian
        'if_sara':     'Sara (IT F) ★★',
        'im_nicola':   'Nicola (IT M) ★★',
        # Portuguese
        'pf_dora':     'Dora (PT F)',
        'pm_alex':     'Alex (PT M)',
        'pm_santa':    'Santa (PT M)',
    }

