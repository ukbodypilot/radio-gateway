"""Every gateway setting in one place: its default, its type and its form section.

Before this module the list of settings lived in two places that had
drifted apart: the defaults dict in radio_gateway.Config (370 keys) and
web_server.WebConfigServer._CONFIG_LAYOUT (382 keys). 26 keys had a form
field but no default, so they were typed by guesswork and each caller
carried its own fallback; 14 had a default but no form field. A web-form
save of the file once dropped everything outside the layout.

    DEFAULTS     key -> default value; the default's type is the key's type
    LAYOUT       (section, title, [keys]) in web-form and file order
    NOT_IN_FORM  keys deliberately left off the web form, with the reason

tests/test_config_schema.py fails if a key is in one but not the others.

coerce() and infer() are the only places a config string becomes a value;
the flat loader (radio_gateway.Config) and the [plugin.instance] section
reader (endpoints_state.read_sections) both use them.
"""

import re

DEFAULTS = {
    'MUMBLE_SERVER': '192.168.2.126',
    'MUMBLE_PORT': 64738,
    'MUMBLE_USERNAME': 'RadioGateway',
    'MUMBLE_PASSWORD': '',
    'MUMBLE_CHANNEL': '',
    'AUDIO_RATE': 48000,
    'AUDIO_CHUNK_SIZE': 2400,
    'AUDIO_CHANNELS': 1,
    'AUDIO_BITS': 16,
    'MUMBLE_BITRATE': 72000,
    'MUMBLE_VBR': True,
    'MUMBLE_JITTER_BUFFER': 10,
    'TX_RADIO': 'th9800',              # 'th9800', 'd75', or 'kv4p' — which radio for playback/TTS/announce TX
    'TX_TALKBACK': False,              # When True, TX audio (TTS/CW/announce) is also sent to local outputs (speaker/Mumble/stream/PCM)
    'PTT_METHOD': 'aioc',              # 'aioc', 'relay', or 'software'
    'PTT_RELAY_DEVICE': '/dev/relay_ptt',
    'PTT_RELAY_BAUD': 9600,
    'AIOC_PTT_CHANNEL': 3,
    'PTT_RELEASE_DELAY': 0.5,
    'PTT_TIMEOUT_SECONDS': 180,      # Transmit time-out: force unkey after this long keyed (0 = off)
    'LINK_AUTO_PTT_THRESHOLD': 10,   # Bus level (0-100) that keys a link endpoint's TX
    'LINK_AUTO_PTT_HOLD': 0.5,       # Seconds to hold a link endpoint keyed after the last chunk
    'PTT_PREKEY_BUFFER_MS': 500,     # ms of TX audio held while a bus PTT key is still being applied (0=off)
    'LINK_JITTER_PREFILL': 4,        # 50ms chunks buffered before a link endpoint starts playing (latency vs dropout)
    'REMOTE_AUDIO_JITTER_PREFILL': 8, # same for the Windows remote-audio client (burstier, needs more)
    'PTT_ACTIVATION_DELAY': 0.1,
    'AIOC_VID': 0x1209,
    'AIOC_PID': 0x7388,
    'AIOC_INPUT_DEVICE': -1,
    'AIOC_OUTPUT_DEVICE': -1,
    'ENABLE_AGC': False,
    'ENABLE_NOISE_GATE': False,
    'NOISE_GATE_THRESHOLD': -40,
    'NOISE_GATE_ATTACK': 0.01,  # float (seconds)
    'NOISE_GATE_RELEASE': 0.1,  # float (seconds)
    'ENABLE_HIGHPASS_FILTER': True,
    'HIGHPASS_CUTOFF_FREQ': 300,
    'ENABLE_ECHO_CANCELLATION': False,
    'ENABLE_LOWPASS_FILTER': False,
    'LOWPASS_CUTOFF_FREQ': 3000,
    'ENABLE_NOTCH_FILTER': False,
    'NOTCH_FREQ': 1000,
    'NOTCH_Q': 30.0,
    # Per-source processing overrides (SDR).
    # When set, SDR sources use these instead of the global settings.
    # When not set (empty string / default), SDR processing is disabled
    # to preserve backwards compatibility.
    'SDR_PROC_ENABLE_NOISE_GATE': False,
    'SDR_PROC_NOISE_GATE_THRESHOLD': -40,
    'SDR_PROC_NOISE_GATE_ATTACK': 0.01,
    'SDR_PROC_NOISE_GATE_RELEASE': 0.1,
    'SDR_PROC_ENABLE_HPF': True,
    'SDR_PROC_HPF_CUTOFF': 300,
    'SDR_PROC_ENABLE_LPF': False,
    'SDR_PROC_LPF_CUTOFF': 3000,
    'SDR_PROC_ENABLE_NOTCH': False,
    'SDR_PROC_NOTCH_FREQ': 1000,
    'SDR_PROC_NOTCH_Q': 30.0,
    'INPUT_VOLUME': 1.0,
    'OUTPUT_VOLUME': 1.0,
    'MUMBLE_LOOP_RATE': 0.01,
    'MUMBLE_STEREO': False,
    'MUMBLE_RECONNECT': True,
    'MUMBLE_DEBUG': False,
    'NETWORK_TIMEOUT': 10,
    'HEADLESS_MODE': True,          # No console status bar, log to file + web UI
    'LOG_BUFFER_LINES': 2000,      # Lines kept in memory for web /logs viewer
    'LOG_FILE_DAYS': 30,           # Days to keep rolling log files
    'VERBOSE_LOGGING': False,
    'STATUS_UPDATE_INTERVAL': 1.0,  # seconds
    'LOOP_RECORD_KBPS': 48,  # Loop-recorder MP3 bitrate (CBR mono); 128 was ~1.3 GiB/day per bus
    'MAX_MUMBLE_BUFFER_SECONDS': 1.0,
    'BUFFER_MANAGEMENT_VERBOSE': False,
    'ENABLE_VAD': True,
    'VAD_THRESHOLD': -45,
    'VAD_ATTACK': 0.02,  # float (seconds)
    'VAD_RELEASE': 1.0,  # float (seconds)
    'VAD_MIN_DURATION': 0.1,  # float (seconds)
    'ENABLE_STREAM_HEALTH': False,
    'STREAM_RESTART_INTERVAL': 60,
    'STREAM_RESTART_IDLE_TIME': 3,
    'ENABLE_VOX': False,
    'VOX_THRESHOLD': -30,
    'VOX_ATTACK_TIME': 0.05,  # float (seconds)
    'VOX_RELEASE_TIME': 0.5,  # float (seconds)
    # File Playback
    'ENABLE_PLAYBACK': True,
    'PLAYBACK_DIRECTORY': './audio/',
    'PLAYBACK_ANNOUNCEMENT_FILE': '',
    'PLAYBACK_ANNOUNCEMENT_INTERVAL': 0,  # seconds, 0 = disabled
    'PLAYBACK_VOLUME': 1.0,               # float (multiplier; >1.0 boosts, audio is clipped to int16 range)
    # Morse Code (CW)
    'CW_WPM': 20,          # Morse code words per minute
    'CW_FREQUENCY': 600,   # Tone frequency in Hz
    'CW_VOLUME': 1.0,      # Volume multiplier (applied before WAV write; PLAYBACK_VOLUME also applies)
    # Text-to-Speech and Text Commands (Phase 4)
    'ENABLE_TTS': True,
    'ENABLE_TEXT_COMMANDS': True,
    'TTS_ENGINE': 'kokoro',  # kokoro (offline ONNX, default) | edge (Microsoft Neural) | gtts (Google)
    'KOKORO_DEFAULT_VOICE': 'af_heart',  # Kokoro voice ID — see docs/TTS_TEXT_COMMANDS_GUIDE.md
    'TTS_VOLUME': 1.0,  # Volume multiplier for TTS audio (1.0 = normal, 2.0 = double, 3.0 = triple)
    'TTS_TARGET_RMS_DB': -16.0,  # Loudness-normalise synthesised TTS to this RMS (all 3 engines land quieter); tanh soft-clip, one-way
    'TTS_SPEED': 1.0,   # Speech speed (1.0 = normal, 1.3 = 30% faster, 0.8 = slower, requires ffmpeg)
    'TTS_DEFAULT_VOICE': 1, # gTTS/Edge voice (1=US, 2=British, 3=Australian, 4=Indian, 5=SA, 6=Canadian, 7=Irish, 8=French, 9=German)
    'PTT_TTS_DELAY': 0.5,   # Silence padding before TTS (seconds) to prevent cutoff
    'PTT_ANNOUNCEMENT_DELAY': 0.5,  # Seconds after PTT key-up before announcement audio starts
    # SDR Integration
    'SDR_MODE': 'dual',             # 'dual' (master+slave) or 'single' (one tuner, multi-channel)
    'ENABLE_SDR': True,
    'SDR_DEVICE_NAME': 'pw:sdr_capture',  # PipeWire sink (recommended) or ALSA device (e.g., 'hw:6,1')
    'SDR_DUCK': True,             # Duck SDR: silence SDR when higher priority source is active
    'SDR_MIX_RATIO': 1.0,        # Volume/mix ratio when ducking is disabled (1.0 = full volume)
    'SDR_DISPLAY_GAIN': 1.0,     # Display sensitivity multiplier (1.0 = normal, higher = more sensitive bar)
    'SDR_AUDIO_BOOST': 1.0,      # Actual audio volume boost (1.0 = no change, 2.0 = 2x louder)
    'SDR_BUFFER_MULTIPLIER': 4,  # Buffer size multiplier (4 = 4x normal buffer, ~200ms per ALSA read)
    'SDR_WATCHDOG_TIMEOUT': 10,        # seconds with no successful read before recovery
    'SDR_WATCHDOG_MAX_RESTARTS': 5,    # max recovery attempts before giving up
    'SDR_WATCHDOG_MODPROBE': False,    # enable kernel module reload (requires sudoers entry)
    # SDR2 Integration (second SDR receiver)
    'ENABLE_SDR2': False,
    'SDR2_DEVICE_NAME': 'pw:sdr_capture2',
    'SDR2_DUCK': True,
    'SDR2_MIX_RATIO': 1.0,
    'SDR2_DISPLAY_GAIN': 1.0,
    'SDR2_AUDIO_BOOST': 1.5,
    'SDR2_BUFFER_MULTIPLIER': 4,
    'SDR2_WATCHDOG_TIMEOUT': 10,
    'SDR2_WATCHDOG_MAX_RESTARTS': 5,
    'SDR2_WATCHDOG_MODPROBE': False,
    'SDR_PRIORITY_ORDER': 'sdr1',    # which SDR ducks the other: sdr1 / sdr2 / equal
    # Signal Detection Hysteresis (prevents stuttering from rapid on/off)
    'SIGNAL_ATTACK_TIME': 0.25,  # Seconds of CONTINUOUS signal required before a source switch is allowed
    'SIGNAL_RELEASE_TIME': 3.0,  # Seconds of continuous silence required before switching back
    'SWITCH_PADDING_TIME': 1.0,  # Seconds of silence inserted at each transition (duck-out and duck-in)
    'SDR_DUCK_COOLDOWN': 3.0,   # After lower-priority SDR unducks, seconds before higher-priority SDR can re-duck it
    'SDR_SIGNAL_THRESHOLD': -70.0,  # dBFS threshold for SDR signal detection (inclusion + ducking); lower = more sensitive
    # EchoLink Integration (Phase 3B)
    'ENABLE_ECHOLINK': False,
    'ECHOLINK_RX_PIPE': '/tmp/echolink_rx',
    'ECHOLINK_TX_PIPE': '/tmp/echolink_tx',
    'ECHOLINK_TO_MUMBLE': True,
    'ECHOLINK_TO_RADIO': True,
    'RADIO_TO_ECHOLINK': True,
    'MUMBLE_TO_ECHOLINK': True,
    # AllStarLink bridge via USRP (DVSwitch) channel — see plugins/usrp.py.
    # The gateway listens on USRP_LISTEN_PORT and sends to the bridge
    # node's USRP_REMOTE_HOST:USRP_REMOTE_PORT. On the ASL3 bridge node:
    #   rxchannel = USRP/<gateway_ip>:34001:32001  (and enable chan_usrp)
    'ENABLE_USRP': False,
    'USRP_REMOTE_HOST': '127.0.0.1',  # bridge node (ASL3) address
    'USRP_REMOTE_PORT': 32001,        # port the bridge node listens on
    'USRP_LISTEN_PORT': 34001,        # port the gateway listens on
    # Runtime node control via the bridge node's Asterisk Manager
    # Interface — drives the /usrp control panel (connect/disconnect).
    'USRP_NODE': '0',                 # our local node number, e.g. 68397
    'USRP_AMI_HOST': '',              # blank = same host as USRP_REMOTE_HOST
    'USRP_AMI_PORT': 5038,
    'USRP_AMI_USER': '',              # AMI user (manager.conf on the node)
    'USRP_AMI_SECRET': '',            # AMI secret — gateway_config.txt only
    # Second AllStar USRP instance (usrp2 plugin — CM5 node)
    'ENABLE_USRP2': False,
    'USRP2_REMOTE_HOST': '127.0.0.1',
    'USRP2_REMOTE_PORT': 32002,
    'USRP2_LISTEN_PORT': 34002,
    'USRP2_NODE': '0',
    'USRP2_AMI_HOST': '',             # blank = same host as USRP2_REMOTE_HOST
    'USRP2_AMI_PORT': 5038,
    'USRP2_AMI_USER': '',
    'USRP2_AMI_SECRET': '',
    # Streaming Output (Phase 3A)
    'ENABLE_STREAM_OUTPUT': False,
    'STREAM_SERVER': 'localhost',
    'STREAM_PORT': 8000,
    'STREAM_PASSWORD': 'hackme',
    'STREAM_MOUNT': '/radio',
    'STREAM_NAME': 'Radio Gateway',
    'STREAM_DESCRIPTION': 'Radio to Mumble Gateway',
    'STREAM_BITRATE': 16,
    'STREAM_FORMAT': 'mp3',
    # Speaker Output (local monitoring)
    'ENABLE_SPEAKER_OUTPUT': True,
    'SPEAKER_OUTPUT_DEVICE': '',   # '' = system default; or partial name e.g. 'USB Audio', 'hw:2,0'
    'SPEAKER_MODE': 'virtual',     # 'virtual' = metering only (no audio device), 'auto' = try device then fallback, 'real' = require device
    'SPEAKER_VOLUME': 1.0,         # float multiplier
    'SPEAKER_START_MUTED': False,  # Start with speaker unmuted
    # Remote Audio Link (server sends mixed audio to client over TCP)
    'REMOTE_AUDIO_ROLE': 'disabled',       # 'server', 'client', or 'disabled'
    'REMOTE_AUDIO_HOST': '',               # Server: bind addr; Client: server IP
    'REMOTE_AUDIO_PORT': 9600,
    'REMOTE_AUDIO_DUCK': True,
    'REMOTE_AUDIO_PRIORITY': 0,            # sdr_priority for ducking (0 = ducks all local SDRs)
    'REMOTE_AUDIO_DISPLAY_GAIN': 1.0,
    'REMOTE_AUDIO_AUDIO_BOOST': 1.0,
    'REMOTE_AUDIO_RECONNECT_INTERVAL': 5.0,
    # Announcement Input (port 9601 — inbound PCM stream, PTT to radio)
    'ENABLE_ANNOUNCE_INPUT': True,
    'ANNOUNCE_INPUT_PORT': 9601,
    'ANNOUNCE_INPUT_HOST': '',
    'ANNOUNCE_INPUT_THRESHOLD': -45.0,  # dBFS — below this is treated as silence
    'ANNOUNCE_INPUT_VOLUME': 4.0,       # volume multiplier for announcement audio
    # Web Microphone PTT (browser mic → radio TX via WebSocket)
    'ENABLE_WEB_MIC': True,
    'WEB_MIC_VOLUME': 4.0,              # volume multiplier for browser mic audio
    # Hold-to-talk watchdogs, both enforced on the bus thread so a
    # dead browser cannot strand a keyed transmitter.
    'WEB_MIC_KEY_TIMEOUT': 2.0,         # s without a key refresh → unkey
    'WEB_MIC_MAX_TX': 120.0,            # s max single over (TOT), then release to reset
    # Web Monitor (browser mic → mixer via WebSocket, no PTT)
    'ENABLE_WEB_MONITOR': True,
    # How far the BGM bed drops while the announcer speaks, in dB.
    # Broadcast-style: attenuated, not muted, so the bed stays audible.
    # Only BGM uses this — every other source keeps hard-mute ducking.
    'BGM_DUCK_DB': -12.0,
    # BGM duck envelope. Down fast, hold across gaps, back up slowly —
    # the broadcast/DJ feel. Seconds for the FULL transition.
    'BGM_DUCK_ATTACK': 0.25,
    'BGM_DUCK_HOLD': 0.4,
    'BGM_DUCK_RELEASE': 1.2,
    # Stop a BGM bed automatically after this many seconds so an
    # unattended loop cannot run for ever. 0 = no cap.
    'BGM_MAX_SECONDS': 120.0,
    # Seconds between repeats of the announcer message (min 2).
    'ANNOUNCER_INTERVAL': 10.0,
    # Background music beds, comma-separated. Relative names resolve
    # against PLAYBACK_DIRECTORY; absolute paths are used as-is. These
    # play looped through the System Sounds node, so they follow
    # whatever that node is wired to in the routing graph. They are
    # excluded from the numbered soundboard slots.
    'BGM_FILES': 'bgm1.mp3, bgm2.mp3, bgm3.mp3',
    # Number of soundboard slots (1..N). Slot 0 is always the station ID.
    # NOTE only 1-9 are reachable from the physical keyboard — a keypress
    # is a single character. Higher slots work from the web UI, `!play <n>`
    # and MCP.
    'PLAYBACK_SLOTS': 20,
    # Soundboard — auto-fill empty playback slots with random sound effects
    'ENABLE_SOUNDBOARD': True,
    # Restrict which categories the soundboard draws from. Comma-separated;
    # prefix a name with '-' to exclude it. Blank = all categories.
    #   e.g.  boing, fart, scream, wrong     (only these)
    #   e.g.  -animals, -applause            (everything except these)
    # Applies on the next /refreshsounds — no gateway restart needed.
    'SOUNDBOARD_CATEGORIES': '',
    # Reject soundboard clips longer than this many seconds. The pool
    # contains some full-length music tracks (id 2474 is 72s) that are
    # useless as effects. 0 disables the cap.
    'SOUNDBOARD_MAX_SECONDS': 15,
    # Relay Control — Radio Power
    'ENABLE_RELAY_RADIO': False,
    'RELAY_RADIO_DEVICE': '/dev/relay_radio',
    'RELAY_RADIO_BAUD': 9600,
    # Relay Control — Charger Schedule
    'ENABLE_RELAY_CHARGER': False,
    'RELAY_CHARGER_CONTROL': 'gpio',   # 'gpio' or 'serial'
    'CHARGER_RELAY_GPIO': 23,           # BCM pin when RELAY_CHARGER_CONTROL = gpio
    'RELAY_CHARGER_DEVICE': '/dev/relay_charger',
    'RELAY_CHARGER_BAUD': 9600,
    'RELAY_CHARGER_ON_TIME': '23:00',
    'RELAY_CHARGER_OFF_TIME': '06:00',
    # Smart Announcements (AI-powered via Claude CLI)
    'ENABLE_SMART_ANNOUNCE': True,
    'SMART_ANNOUNCE_TOP_TEXT': '',           # Global top text (used if slot has none)
    'SMART_ANNOUNCE_TAIL_TEXT': '',          # Global tail text (used if slot has none)
    'SMART_ANNOUNCE_START_TIME': '08:00',   # HH:MM — empty = no restriction
    'SMART_ANNOUNCE_END_TIME': '22:00',     # HH:MM — empty = no restriction
    # Smart Announcement Slots (1-3, match dashboard buttons)
    'SMART_ANNOUNCE_1_PROMPT': '',           # prompt/search text
    'SMART_ANNOUNCE_1_INTERVAL': 3600,       # seconds between announcements
    'SMART_ANNOUNCE_1_VOICE': 1,             # TTS voice (1-9)
    'SMART_ANNOUNCE_1_TARGET_SECS': 15,      # max speech length in seconds
    'SMART_ANNOUNCE_1_MODE': 'auto',         # auto = scheduled, manual = web UI only
    'SMART_ANNOUNCE_1_TOP_TEXT': '',          # text spoken before (blank = use global)
    'SMART_ANNOUNCE_1_TAIL_TEXT': '',         # text spoken after (blank = use global)
    'SMART_ANNOUNCE_2_PROMPT': '',
    'SMART_ANNOUNCE_2_INTERVAL': 3600,
    'SMART_ANNOUNCE_2_VOICE': 1,
    'SMART_ANNOUNCE_2_TARGET_SECS': 15,
    'SMART_ANNOUNCE_2_MODE': 'auto',
    'SMART_ANNOUNCE_2_TOP_TEXT': '',
    'SMART_ANNOUNCE_2_TAIL_TEXT': '',
    'SMART_ANNOUNCE_3_PROMPT': '',
    'SMART_ANNOUNCE_3_INTERVAL': 3600,
    'SMART_ANNOUNCE_3_VOICE': 1,
    'SMART_ANNOUNCE_3_TARGET_SECS': 15,
    'SMART_ANNOUNCE_3_MODE': 'auto',
    'SMART_ANNOUNCE_3_TOP_TEXT': '',
    'SMART_ANNOUNCE_3_TAIL_TEXT': '',
    # TH-9800 CAT Control
    'ENABLE_TH9800': False,       # Alias for ENABLE_CAT_CONTROL
    'ENABLE_CAT_CONTROL': False,
    'CAT_STARTUP_COMMANDS': True,
    'CAT_HOST': '127.0.0.1',
    'CAT_PORT': 9800,
    'CAT_PASSWORD': '',
    'CAT_LEFT_CHANNEL': -1,     # -1 = don't change
    'CAT_RIGHT_CHANNEL': -1,    # -1 = don't change
    'CAT_LEFT_VOLUME': -1,      # 0-100, -1 = don't change
    'CAT_RIGHT_VOLUME': -1,     # 0-100, -1 = don't change
    'CAT_LEFT_POWER': '',       # L/M/H or blank = don't change
    'CAT_RIGHT_POWER': '',      # L/M/H or blank = don't change
    # D75 (link endpoint — legacy plugin config removed)
    'ENABLE_D75': False,
    # D75 audio processing
    'D75_PROC_ENABLE_NOISE_GATE': False,
    'D75_PROC_NOISE_GATE_THRESHOLD': -40,
    'D75_PROC_NOISE_GATE_ATTACK': 0.01,
    'D75_PROC_NOISE_GATE_RELEASE': 0.1,
    'D75_PROC_ENABLE_HPF': True,
    'D75_PROC_HPF_CUTOFF': 300,
    'D75_PROC_ENABLE_LPF': False,
    'D75_PROC_LPF_CUTOFF': 3000,
    'D75_PROC_ENABLE_NOTCH': False,
    'D75_PROC_NOTCH_FREQ': 1000,
    'D75_PROC_NOTCH_Q': 30.0,
    # KV4P HT Radio (USB serial, Opus audio)
    'ENABLE_KV4P': False,
    'KV4P_PORT': '/dev/ttyUSB0',       # Serial port for KV4P HT
    'KV4P_FREQ': 146.520,               # Default frequency (MHz)
    'KV4P_TX_FREQ': 0.0,                # TX frequency (0 = same as RX)
    'KV4P_SQUELCH': 4,                   # Squelch level 0-8
    'KV4P_CTCSS_TX': 0,                  # TX CTCSS tone code (0 = none)
    'KV4P_CTCSS_RX': 0,                  # RX CTCSS tone code (0 = none)
    'KV4P_BANDWIDTH': 1,                  # 0 = narrow (12.5 kHz), 1 = wide (25 kHz)
    'KV4P_HIGH_POWER': True,             # True = high power, False = low power
    'KV4P_AUDIO_DUCK': True,             # Duck KV4P when higher priority source active
    'KV4P_AUDIO_PRIORITY': 2,            # Priority for ducking
    'KV4P_AUDIO_DISPLAY_GAIN': 1.0,      # Display level sensitivity
    'KV4P_AUDIO_BOOST': 1.0,             # RX volume multiplier
    'KV4P_RECONNECT_INTERVAL': 5.0,      # Seconds between reconnect attempts
    'KV4P_SMETER': True,                  # Enable S-meter reporting
    # KV4P audio processing
    'KV4P_PROC_ENABLE_NOISE_GATE': False,
    'KV4P_PROC_NOISE_GATE_THRESHOLD': -40,
    'KV4P_PROC_NOISE_GATE_ATTACK': 0.01,
    'KV4P_PROC_NOISE_GATE_RELEASE': 0.1,
    'KV4P_PROC_ENABLE_HPF': True,
    'KV4P_PROC_HPF_CUTOFF': 300,
    'KV4P_PROC_ENABLE_LPF': False,
    'KV4P_PROC_LPF_CUTOFF': 3000,
    'KV4P_PROC_ENABLE_NOTCH': False,
    'KV4P_PROC_NOTCH_FREQ': 1000,
    'KV4P_PROC_NOTCH_Q': 30.0,
    # Packet Radio (Direwolf TNC)
    'ENABLE_PACKET': False,
    'PACKET_CALLSIGN': 'N0CALL',
    'PACKET_SSID': 0,
    'PACKET_MODEM': 1200,
    'PACKET_DIREWOLF_PATH': '/usr/bin/direwolf',
    'PACKET_REMOTE_TNC': '',
    'PACKET_RADIO_ENDPOINT': '',     # Link-endpoint name to use for packet; blank = first capable
    'PACKET_UDP_RX_PORT': 7355,
    'PACKET_KISS_PORT': 8001,
    'PACKET_AGW_PORT': 8000,
    'PACKET_PAT_PORT': 8082,
    'PACKET_LOOPBACK_CARD': 'Loopback_1',
    'PACKET_APRS_COMMENT': 'Radio Gateway',
    'PACKET_APRS_SYMBOL': '/#',
    'PACKET_APRS_BEACON_INTERVAL': 600,
    'PACKET_DIGIPEAT': True,
    'PACKET_APRS_IS': False,
    'PACKET_APRS_IS_SERVER': 'noam.aprs2.net',
    'PACKET_APRS_IS_PASSCODE': '',
    # Dynamic DNS (No-IP compatible)
    # Web Configuration UI
    'ENABLE_WEB_CONFIG': True,
    'WEB_CONFIG_PORT': 8080,
    'WEB_CONFIG_PASSWORD': '',    # Basic auth password (user: admin), blank = no auth
    'WEB_CONFIG_HTTPS': 'false',  # false, self-signed, or letsencrypt (a string: a bool default coerced 'self-signed' to False)
    'GATEWAY_NAME': '',           # Display name shown at top of dashboard (blank = none)
    'WEB_THEME': 'grey',          # Dashboard color theme: grey, blue, red, green, purple, amber, teal, pink
    # Cloudflare Tunnel (free public HTTPS access, no port forwarding needed)
    'SDR_INTERNAL_AUTOSTART': True,    # Auto-start internal SDR (rtl_airband) on gateway startup
    'SDR_INTERNAL_AUTOSTART_CHANNEL': 1,   # Channel slot to recall on autostart (-1 = use last settings)
    'ENABLE_CLOUDFLARE_TUNNEL': False,
    # Requests through the tunnel get 403 while WEB_CONFIG_PASSWORD is blank,
    # except the link and endpoint-update routes. true restores the old
    # open-to-the-internet behaviour.
    'WEB_TUNNEL_ALLOW_NO_PASSWORD': False,
    # ProcessSupervisor opt-ins. Default false → existing systemd /
    # manual handling stays in charge. Setting true makes the gateway
    # supervise the service: respawn on death, status in /processes.
    'SUPERVISE_MUMBLE': False,
    # Email notifications (Gmail SMTP)
    'ENABLE_EMAIL': False,
    'ENABLE_ALERT_ENGINE': False,   # raw-threshold Prometheus alerts, emailed; the Fleet Manager is the calibrated path
    'EMAIL_ADDRESS': '',          # Gmail address (sender)
    'EMAIL_APP_PASSWORD': '',     # Gmail app password (not regular password)
    'EMAIL_RECIPIENT': '',        # Where to send notifications (blank = same as EMAIL_ADDRESS)
    'EMAIL_ON_STARTUP': True,     # Send status email on startup
    # Dynamic DNS (No-IP compatible)
    'ENABLE_DDNS': True,
    'DDNS_USERNAME': '',
    'DDNS_PASSWORD': '',
    'DDNS_HOSTNAME': '',
    'DDNS_UPDATE_INTERVAL': 300,   # seconds between public-IP checks (default 5 min)
    'DDNS_UPDATE_URL': 'https://dynupdate.no-ip.com/nic/update',  # No-IP protocol
    # Public-IP lookup used to skip no-op updates. No-IP treats repeated
    # updates with an unchanged IP as abuse, so we only call them on change.
    'DDNS_CHECKIP_URL': 'https://api.ipify.org',
    'DDNS_FORCE_INTERVAL': 86400,  # send an update anyway this often (safety net)
    # Independent verification: resolve the hostname and compare to our
    # public IP. Catches expiry, which a DDNS Key reports as success.
    'DDNS_VERIFY_DNS': True,
    'DDNS_MISMATCH_GRACE': 3,      # bad cycles before alerting (DNS propagation)
    'DDNS_ALERT_INTERVAL': 86400,  # min seconds between alert emails
    # Mumble Server 1 (local mumble-server instance)
    'ENABLE_MUMBLE_SERVER_1': False,
    'MUMBLE_SERVER_1_PORT': 64738,
    'MUMBLE_SERVER_1_PASSWORD': '',
    'MUMBLE_SERVER_1_MAX_USERS': 10,
    'MUMBLE_SERVER_1_MAX_BANDWIDTH': 72000,
    'MUMBLE_SERVER_1_WELCOME': 'Welcome to Radio Gateway Server 1',
    'MUMBLE_SERVER_1_REGISTER_NAME': '',
    'MUMBLE_SERVER_1_ALLOW_HTML': True,
    'MUMBLE_SERVER_1_OPUS_THRESHOLD': 0,
    'MUMBLE_SERVER_1_AUTOSTART': True,
    # Mumble Server 2 (second local mumble-server instance)
    'ENABLE_MUMBLE_SERVER_2': False,
    'MUMBLE_SERVER_2_PORT': 64739,
    'MUMBLE_SERVER_2_PASSWORD': '',
    'MUMBLE_SERVER_2_MAX_USERS': 10,
    'MUMBLE_SERVER_2_MAX_BANDWIDTH': 72000,
    'MUMBLE_SERVER_2_WELCOME': 'Welcome to Radio Gateway Server 2',
    'MUMBLE_SERVER_2_REGISTER_NAME': '',
    'MUMBLE_SERVER_2_ALLOW_HTML': True,
    'MUMBLE_SERVER_2_OPUS_THRESHOLD': 0,
    'MUMBLE_SERVER_2_AUTOSTART': False,
    # USB/IP — share USB devices from a remote machine over TCP
    'ENABLE_USBIP': True,
    'USBIP_SERVER': '',              # IP or hostname of the USB/IP server
    'USBIP_DEVICES': '',             # Comma-separated bus IDs to attach, e.g. "1-1.4,1-1.3"
    # ADS-B aircraft tracking (RTL-SDR via dump1090-fa + FlightRadar24)
    'ENABLE_ADSB': True,
    'ADSB_PORT': 30080,          # dump1090-fa HTTP port (30080 avoids conflict with gateway on 8080)
    # Automation Engine
    'ENABLE_AUTOMATION': False,
    'AUTOMATION_SCHEME_FILE': 'automation_scheme.txt',
    'AUTOMATION_REPEATER_FILE': '',       # RepeaterBook CSV path
    'AUTOMATION_REPEATER_LAT': 0.0,       # Home latitude for distance calc
    'AUTOMATION_REPEATER_LON': 0.0,       # Home longitude for distance calc
    'AUTOMATION_RECORDINGS_DIR': 'recordings',
    'AUTOMATION_START_TIME': '06:00',      # Daily start time (HH:MM)
    'AUTOMATION_END_TIME': '23:00',        # Daily end time (HH:MM)
    'AUTOMATION_MAX_TASK_DURATION': 600,   # Max seconds per task

    # Gateway Link (duplex audio + command protocol for remote endpoints)
    'ENABLE_GATEWAY_LINK': False,
    'LINK_PORT': 9700,
    'LINK_BIND': '',                  # interface address for :9700, blank = all
    # Shared secret for link endpoints (REGISTER HMAC, signed code updates,
    # /ws/link without Basic auth). Blank = open, as before 5.1.
    'LINK_SECRET': '',
    'LINK_AUDIO_DUCK': False,
    'LINK_AUDIO_PRIORITY': 3,
    'LINK_AUDIO_BOOST': 1.0,
    'LINK_AUDIO_DISPLAY_GAIN': 1.0,

    # Fleet Manager runs spawn a fresh `claude -p` per run so context
    # cannot accumulate across runs.
    'MANAGER_CLAUDE_BIN': '',        # blank -> $CLAUDE_BIN, then ~/.local/bin/claude
    'MANAGER_CLAUDE_MODEL': 'sonnet',
    'MANAGER_MAX_TURNS': 40,

    # Advanced
    'START_CLAUDE_CODE': False,
}

# Keys that had a web-form field but no default. Each default is the
# fallback the code already used (getattr(config, KEY, <this>)), so a file
# that doesn't set them behaves exactly as before; now they are typed too.
DEFAULTS.update({
    'ENABLE_GDRIVE': False,
    'GDRIVE_REMOTE': 'gdrive',
    'GDRIVE_FOLDER': 'radio-gateway',
    'ENABLE_GPS': False,
    'GPS_PORT': '/dev/ttyACM0',
    'GPS_BAUD': 9600,
    'ENABLE_REPEATER_DB': False,
    'REPEATER_RADIUS_KM': 50.0,       # RepeaterManager.DEFAULT_RADIUS
    'ENABLE_TRANSCRIPTION': False,
    'TRANSCRIBE_MODEL': 'moonshine/base',
    'TRANSCRIBE_VAD_THRESHOLD': 0.5,
    'TRANSCRIBE_VAD_HOLD': 1.0,
    'TRANSCRIBE_MIN_DURATION': 0.5,
    'TRANSCRIBE_FORWARD_MUMBLE': True,
    'LINK_RX_MUTED': False,
    'LINK_TX_MUTED': False,
    'MUMBLE_VAD_THRESHOLD': -40.0,
    'MONITOR_VAD_THRESHOLD': -40.0,
    'WEB_MONITOR_VOLUME': 1.0,
    'REDUCK_INHIBIT_TIME': 2.0,
    'REMOTE_AUDIO_RX_PORT': 9602,
    'REPEATER_PTT_HOLD': 1.0,
    'SIMPLEX_TAIL_TIME': 1.0,
    'SIMPLEX_MAX_BUFFER': 30.0,
    'SDR_MUTE_DEFAULT': False,
    'SDR2_MUTE_DEFAULT': False,
})

# Keys documented in examples/gateway_config.txt and read by the code, but
# with neither a default nor a form field. Same rule: the code's fallback.
DEFAULTS.update({
    'MANUAL_PTT_MAX_SECS': 300,
    'STREAM_SAMPLE_RATE': 22050,
    'STREAM_DUAL_CHANNEL': True,
    'STREAM_MOUNT_WAIT': 15.0,
    'STREAM_CONNECT_CONFIRM': 5.0,
    'STREAM_FLOW_STALE_AFTER': 15.0,
    'STREAM_ENCODER_WRITE_TIMEOUT': 1.0,
    'TRANSCRIBE_MODE': 'local',
    'TRANSCRIBE_SPLIT_THRESHOLD_SECS': 10.0,
    'TRANSCRIBE_LOG_RESULTS': False,
    'TRANSCRIBE_REMOTE_URLS': '',
    'TRANSCRIBE_REMOTE_URL': '',
    'TRANSCRIBE_ALLOW_WORKER_REGISTRATION': True,
    'TRANSCRIBE_WORKER_TTL_SECS': 90,
    'ENABLE_TRANSCRIPTION_LOG': True,
    'TRANSCRIPTION_LOG_PATH': '',
    'TRANSCRIPTION_ALERT_KEYWORDS': '',
})
_FILE_ONLY_DOCUMENTED = (
    'MANUAL_PTT_MAX_SECS', 'STREAM_SAMPLE_RATE', 'STREAM_DUAL_CHANNEL',
    'STREAM_MOUNT_WAIT', 'STREAM_CONNECT_CONFIRM', 'STREAM_FLOW_STALE_AFTER',
    'STREAM_ENCODER_WRITE_TIMEOUT', 'TRANSCRIBE_MODE',
    'TRANSCRIBE_SPLIT_THRESHOLD_SECS', 'TRANSCRIBE_LOG_RESULTS',
    'TRANSCRIBE_REMOTE_URLS', 'TRANSCRIBE_REMOTE_URL',
    'TRANSCRIBE_ALLOW_WORKER_REGISTRATION', 'TRANSCRIBE_WORKER_TTL_SECS',
    'ENABLE_TRANSCRIPTION_LOG', 'TRANSCRIPTION_LOG_PATH',
    'TRANSCRIPTION_ALERT_KEYWORDS',
)

# In the example config but read by nothing any more. Kept here so the
# schema test can tell them from typos; safe to delete from a config file.
RETIRED = {
    'SDR_PRIORITY': 'no longer read',
    'SUPERVISE_DARKICE': 'no longer read (scripts/install.sh still mentions it)',
}

LAYOUT = [
    ('adsb', 'ADS-B Aircraft Tracking', [
        'ENABLE_ADSB', 'ADSB_PORT',
    ]),
    ('announce', 'Remote Audio [PTT]', [
        'ENABLE_ANNOUNCE_INPUT', 'ANNOUNCE_INPUT_PORT', 'ANNOUNCE_INPUT_HOST',
        'ANNOUNCE_INPUT_THRESHOLD', 'ANNOUNCE_INPUT_VOLUME',
    ]),
    ('audio', 'Audio Format & Buffering', [
        'AUDIO_RATE', 'AUDIO_CHUNK_SIZE', 'AUDIO_CHANNELS', 'AUDIO_BITS',
        'MAX_MUMBLE_BUFFER_SECONDS',
    ]),
    ('levels', 'Audio Levels', [
        'INPUT_VOLUME', 'OUTPUT_VOLUME',
    ]),
    ('processing', 'Audio Processing', [
        'ENABLE_AGC', 'ENABLE_NOISE_GATE', 'NOISE_GATE_THRESHOLD',
        'NOISE_GATE_ATTACK', 'NOISE_GATE_RELEASE',
        'ENABLE_HIGHPASS_FILTER', 'HIGHPASS_CUTOFF_FREQ',
        'ENABLE_LOWPASS_FILTER', 'LOWPASS_CUTOFF_FREQ',
        'ENABLE_NOTCH_FILTER', 'NOTCH_FREQ', 'NOTCH_Q',
        'ENABLE_ECHO_CANCELLATION',
    ]),
    ('automation', 'Automation Engine', [
        'ENABLE_AUTOMATION', 'AUTOMATION_SCHEME_FILE',
        'AUTOMATION_REPEATER_FILE', 'AUTOMATION_REPEATER_LAT', 'AUTOMATION_REPEATER_LON',
        'AUTOMATION_RECORDINGS_DIR',
        'AUTOMATION_START_TIME', 'AUTOMATION_END_TIME',
        'AUTOMATION_MAX_TASK_DURATION',
    ]),
    ('streaming', 'Broadcastify Streaming', [
        'ENABLE_STREAM_OUTPUT', 'STREAM_SERVER', 'STREAM_PORT',
        'STREAM_PASSWORD', 'STREAM_MOUNT', 'STREAM_NAME',
        'STREAM_DESCRIPTION', 'STREAM_BITRATE', 'STREAM_FORMAT',
        'ENABLE_STREAM_HEALTH', 'STREAM_RESTART_INTERVAL',
        'STREAM_RESTART_IDLE_TIME',
    ]),
    ('ddns', 'Dynamic DNS', [
        'ENABLE_DDNS', 'DDNS_USERNAME', 'DDNS_PASSWORD', 'DDNS_HOSTNAME',
        'DDNS_UPDATE_INTERVAL', 'DDNS_UPDATE_URL', 'DDNS_CHECKIP_URL',
        'DDNS_FORCE_INTERVAL', 'DDNS_VERIFY_DNS', 'DDNS_MISMATCH_GRACE',
        'DDNS_ALERT_INTERVAL',
    ]),
    ('echolink', 'EchoLink', [
        'ENABLE_ECHOLINK', 'ECHOLINK_RX_PIPE', 'ECHOLINK_TX_PIPE',
        'ECHOLINK_TO_MUMBLE', 'ECHOLINK_TO_RADIO',
        'RADIO_TO_ECHOLINK', 'MUMBLE_TO_ECHOLINK',
    ]),
    ('email', 'Email Notifications', [
        'ENABLE_EMAIL', 'EMAIL_ADDRESS', 'EMAIL_APP_PASSWORD',
        'EMAIL_RECIPIENT', 'EMAIL_ON_STARTUP', 'ENABLE_ALERT_ENGINE',
    ]),
    ('playback', 'System Sounds', [
        'ENABLE_PLAYBACK', 'PLAYBACK_DIRECTORY',
        'PLAYBACK_ANNOUNCEMENT_FILE', 'PLAYBACK_ANNOUNCEMENT_INTERVAL',
        'PLAYBACK_VOLUME', 'PLAYBACK_SLOTS', 'BGM_FILES', 'BGM_DUCK_DB', 'BGM_DUCK_ATTACK', 'BGM_DUCK_HOLD',
        'BGM_DUCK_RELEASE', 'BGM_MAX_SECONDS', 'ANNOUNCER_INTERVAL',
        'ENABLE_SOUNDBOARD', 'SOUNDBOARD_CATEGORIES',
        'SOUNDBOARD_MAX_SECONDS',
    ]),
    ('kv4p', 'KV4P HT Radio', [
        'ENABLE_KV4P', 'KV4P_PORT', 'KV4P_FREQ', 'KV4P_TX_FREQ',
        'KV4P_SQUELCH', 'KV4P_CTCSS_TX', 'KV4P_CTCSS_RX', 'KV4P_BANDWIDTH',
        'KV4P_HIGH_POWER', 'KV4P_SMETER',
        'KV4P_AUDIO_DUCK', 'KV4P_AUDIO_PRIORITY',
        'KV4P_AUDIO_DISPLAY_GAIN', 'KV4P_AUDIO_BOOST', 'KV4P_RECONNECT_INTERVAL',
        'KV4P_PROC_ENABLE_HPF', 'KV4P_PROC_HPF_CUTOFF',
        'KV4P_PROC_ENABLE_LPF', 'KV4P_PROC_LPF_CUTOFF',
        'KV4P_PROC_ENABLE_NOTCH', 'KV4P_PROC_NOTCH_FREQ', 'KV4P_PROC_NOTCH_Q',
        'KV4P_PROC_ENABLE_NOISE_GATE', 'KV4P_PROC_NOISE_GATE_THRESHOLD',
        'KV4P_PROC_NOISE_GATE_ATTACK', 'KV4P_PROC_NOISE_GATE_RELEASE',
    ]),
    ('mumble', 'Mumble Server', [
        'MUMBLE_SERVER', 'MUMBLE_PORT', 'MUMBLE_USERNAME', 'MUMBLE_PASSWORD',
        'MUMBLE_CHANNEL', 'MUMBLE_BITRATE', 'MUMBLE_VBR',
        'MUMBLE_JITTER_BUFFER', 'MUMBLE_LOOP_RATE', 'MUMBLE_STEREO',
        'MUMBLE_RECONNECT', 'MUMBLE_DEBUG',
        'MUMBLE_VAD_THRESHOLD',
    ]),
    ('mumble-server-1', 'Mumble Server 1', [
        'ENABLE_MUMBLE_SERVER_1', 'MUMBLE_SERVER_1_PORT',
        'MUMBLE_SERVER_1_PASSWORD', 'MUMBLE_SERVER_1_MAX_USERS',
        'MUMBLE_SERVER_1_MAX_BANDWIDTH', 'MUMBLE_SERVER_1_WELCOME',
        'MUMBLE_SERVER_1_REGISTER_NAME', 'MUMBLE_SERVER_1_ALLOW_HTML',
        'MUMBLE_SERVER_1_OPUS_THRESHOLD', 'MUMBLE_SERVER_1_AUTOSTART',
    ]),
    ('mumble-server-2', 'Mumble Server 2', [
        'ENABLE_MUMBLE_SERVER_2', 'MUMBLE_SERVER_2_PORT',
        'MUMBLE_SERVER_2_PASSWORD', 'MUMBLE_SERVER_2_MAX_USERS',
        'MUMBLE_SERVER_2_MAX_BANDWIDTH', 'MUMBLE_SERVER_2_WELCOME',
        'MUMBLE_SERVER_2_REGISTER_NAME', 'MUMBLE_SERVER_2_ALLOW_HTML',
        'MUMBLE_SERVER_2_OPUS_THRESHOLD', 'MUMBLE_SERVER_2_AUTOSTART',
    ]),
    ('packet', 'Packet Radio (Direwolf TNC)', [
        'ENABLE_PACKET', 'PACKET_CALLSIGN', 'PACKET_SSID', 'PACKET_MODEM',
        'PACKET_REMOTE_TNC', 'PACKET_DIREWOLF_PATH',
        'PACKET_KISS_PORT', 'PACKET_AGW_PORT',
        'PACKET_APRS_COMMENT', 'PACKET_APRS_SYMBOL',
        'PACKET_APRS_BEACON_INTERVAL', 'PACKET_DIGIPEAT',
    ]),
    ('ptt', 'PTT (Push-to-Talk)', [
        'TX_RADIO', 'PTT_METHOD', 'PTT_RELAY_DEVICE', 'PTT_RELAY_BAUD',
        'PTT_RELEASE_DELAY', 'PTT_ACTIVATION_DELAY',
        'PTT_TTS_DELAY', 'PTT_ANNOUNCEMENT_DELAY',
        'LINK_AUTO_PTT_THRESHOLD', 'LINK_AUTO_PTT_HOLD', 'PTT_PREKEY_BUFFER_MS',
        'LINK_JITTER_PREFILL', 'REMOTE_AUDIO_JITTER_PREFILL',
        'TX_TALKBACK', 'PTT_TIMEOUT_SECONDS',
    ]),
    ('radio', 'Radio Interface (AIOC)', [
        'AIOC_VID', 'AIOC_PID', 'AIOC_INPUT_DEVICE', 'AIOC_OUTPUT_DEVICE',
        'AIOC_PTT_CHANNEL',
    ]),
    ('relay', 'Relay Control', [
        'ENABLE_RELAY_RADIO', 'RELAY_RADIO_DEVICE', 'RELAY_RADIO_BAUD',
        'ENABLE_RELAY_CHARGER', 'RELAY_CHARGER_CONTROL', 'CHARGER_RELAY_GPIO',
        'RELAY_CHARGER_DEVICE', 'RELAY_CHARGER_BAUD',
        'RELAY_CHARGER_ON_TIME', 'RELAY_CHARGER_OFF_TIME',
    ]),
    ('remote', 'Remote Audio Link', [
        'REMOTE_AUDIO_ROLE', 'REMOTE_AUDIO_HOST', 'REMOTE_AUDIO_PORT',
        'REMOTE_AUDIO_RX_PORT',
        'REMOTE_AUDIO_DUCK', 'REMOTE_AUDIO_PRIORITY',
        'REMOTE_AUDIO_DISPLAY_GAIN', 'REMOTE_AUDIO_AUDIO_BOOST',
        'REMOTE_AUDIO_RECONNECT_INTERVAL',
    ]),
    ('sdr_processing', 'SDR Audio Processing', [
        'SDR_PROC_ENABLE_NOISE_GATE', 'SDR_PROC_NOISE_GATE_THRESHOLD',
        'SDR_PROC_NOISE_GATE_ATTACK', 'SDR_PROC_NOISE_GATE_RELEASE',
        'SDR_PROC_ENABLE_HPF', 'SDR_PROC_HPF_CUTOFF',
        'SDR_PROC_ENABLE_LPF', 'SDR_PROC_LPF_CUTOFF',
        'SDR_PROC_ENABLE_NOTCH', 'SDR_PROC_NOTCH_FREQ', 'SDR_PROC_NOTCH_Q',
    ]),
    ('sdr', 'SDR — RSPduo Dual Tuner', [
        'SDR_INTERNAL_AUTOSTART', 'SDR_INTERNAL_AUTOSTART_CHANNEL',
        'ENABLE_SDR', 'SDR_DEVICE_NAME', 'SDR_DUCK', 'SDR_MIX_RATIO',
        'SDR_DISPLAY_GAIN', 'SDR_AUDIO_BOOST', 'SDR_BUFFER_MULTIPLIER',
        'SDR_WATCHDOG_TIMEOUT', 'SDR_WATCHDOG_MAX_RESTARTS', 'SDR_WATCHDOG_MODPROBE',
        'SDR_MUTE_DEFAULT',
        'ENABLE_SDR2', 'SDR2_DEVICE_NAME', 'SDR2_DUCK', 'SDR2_MIX_RATIO',
        'SDR2_DISPLAY_GAIN', 'SDR2_AUDIO_BOOST', 'SDR2_BUFFER_MULTIPLIER',
        'SDR2_WATCHDOG_TIMEOUT', 'SDR2_WATCHDOG_MAX_RESTARTS', 'SDR2_WATCHDOG_MODPROBE',
        'SDR2_MUTE_DEFAULT',
        'SDR_PRIORITY_ORDER',
    ]),
    ('switching', 'Signal Detection & Switching', [
        'SIGNAL_ATTACK_TIME', 'SIGNAL_RELEASE_TIME', 'SWITCH_PADDING_TIME',
        'SDR_DUCK_COOLDOWN', 'SDR_SIGNAL_THRESHOLD',
        'REDUCK_INHIBIT_TIME',
        'REPEATER_PTT_HOLD', 'SIMPLEX_TAIL_TIME', 'SIMPLEX_MAX_BUFFER',
    ]),
    ('smart', 'Smart Announcements', [
        'ENABLE_SMART_ANNOUNCE',
        'SMART_ANNOUNCE_TOP_TEXT', 'SMART_ANNOUNCE_TAIL_TEXT',
        'SMART_ANNOUNCE_START_TIME', 'SMART_ANNOUNCE_END_TIME',
        'SMART_ANNOUNCE_1_PROMPT', 'SMART_ANNOUNCE_1_INTERVAL',
        'SMART_ANNOUNCE_1_VOICE', 'SMART_ANNOUNCE_1_TARGET_SECS',
        'SMART_ANNOUNCE_1_MODE', 'SMART_ANNOUNCE_1_TOP_TEXT', 'SMART_ANNOUNCE_1_TAIL_TEXT',
        'SMART_ANNOUNCE_2_PROMPT', 'SMART_ANNOUNCE_2_INTERVAL',
        'SMART_ANNOUNCE_2_VOICE', 'SMART_ANNOUNCE_2_TARGET_SECS',
        'SMART_ANNOUNCE_2_MODE', 'SMART_ANNOUNCE_2_TOP_TEXT', 'SMART_ANNOUNCE_2_TAIL_TEXT',
        'SMART_ANNOUNCE_3_PROMPT', 'SMART_ANNOUNCE_3_INTERVAL',
        'SMART_ANNOUNCE_3_VOICE', 'SMART_ANNOUNCE_3_TARGET_SECS',
        'SMART_ANNOUNCE_3_MODE', 'SMART_ANNOUNCE_3_TOP_TEXT', 'SMART_ANNOUNCE_3_TAIL_TEXT',
    ]),
    ('speaker', 'Speaker Output', [
        'ENABLE_SPEAKER_OUTPUT', 'SPEAKER_MODE', 'SPEAKER_OUTPUT_DEVICE',
        'SPEAKER_VOLUME', 'SPEAKER_START_MUTED',
    ]),
    ('cw', 'Text to CW', [
        'CW_WPM', 'CW_FREQUENCY', 'CW_VOLUME',
    ]),
    ('tts', 'Text-to-Speech', [
        'ENABLE_TTS', 'TTS_ENGINE', 'ENABLE_TEXT_COMMANDS', 'TTS_VOLUME', 'TTS_SPEED',
        'KOKORO_DEFAULT_VOICE', 'TTS_DEFAULT_VOICE',
    ]),
    ('cat', 'TH-9800 CAT Control', [
        'ENABLE_TH9800', 'CAT_STARTUP_COMMANDS',
        'CAT_HOST', 'CAT_PORT', 'CAT_PASSWORD',
        'CAT_LEFT_CHANNEL', 'CAT_RIGHT_CHANNEL',
        'CAT_LEFT_VOLUME', 'CAT_RIGHT_VOLUME',
        'CAT_LEFT_POWER', 'CAT_RIGHT_POWER',
    ]),
    ('usrp', 'AllStar Link (USRP node 1)', [
        'ENABLE_USRP',
        'USRP_REMOTE_HOST', 'USRP_REMOTE_PORT', 'USRP_LISTEN_PORT',
        'USRP_NODE',
        'USRP_AMI_HOST', 'USRP_AMI_PORT', 'USRP_AMI_USER', 'USRP_AMI_SECRET',
    ]),
    ('usrp2', 'AllStar Link (USRP node 2)', [
        'ENABLE_USRP2',
        'USRP2_REMOTE_HOST', 'USRP2_REMOTE_PORT', 'USRP2_LISTEN_PORT',
        'USRP2_NODE',
        'USRP2_AMI_HOST', 'USRP2_AMI_PORT', 'USRP2_AMI_USER', 'USRP2_AMI_SECRET',
    ]),
    ('d75', 'TH-D75 Control', [
        'ENABLE_D75',
        'D75_PROC_ENABLE_HPF', 'D75_PROC_HPF_CUTOFF',
        'D75_PROC_ENABLE_LPF', 'D75_PROC_LPF_CUTOFF',
        'D75_PROC_ENABLE_NOTCH', 'D75_PROC_NOTCH_FREQ', 'D75_PROC_NOTCH_Q',
        'D75_PROC_ENABLE_NOISE_GATE', 'D75_PROC_NOISE_GATE_THRESHOLD',
        'D75_PROC_NOISE_GATE_ATTACK', 'D75_PROC_NOISE_GATE_RELEASE',
    ]),
    ('gps', 'GPS Receiver', [
        'ENABLE_GPS', 'GPS_PORT', 'GPS_BAUD',
    ]),
    ('repeaters', 'Repeater Database', [
        'ENABLE_REPEATER_DB', 'REPEATER_RADIUS_KM',
    ]),
    ('usbip', 'USB/IP Remote Devices', [
        'ENABLE_USBIP', 'USBIP_SERVER', 'USBIP_DEVICES',
    ]),
    ('link', 'Gateway Link', [
        'ENABLE_GATEWAY_LINK', 'LINK_PORT', 'LINK_BIND', 'LINK_SECRET',
        'LINK_AUDIO_DUCK', 'LINK_AUDIO_PRIORITY',
        'LINK_AUDIO_BOOST', 'LINK_AUDIO_DISPLAY_GAIN',
        'LINK_RX_MUTED', 'LINK_TX_MUTED',
    ]),
    ('vad', 'Voice Activity Detection', [
        'ENABLE_VAD', 'VAD_THRESHOLD', 'VAD_ATTACK', 'VAD_RELEASE',
        'VAD_MIN_DURATION',
    ]),
    ('vox', 'VOX', [
        'ENABLE_VOX', 'VOX_THRESHOLD', 'VOX_ATTACK_TIME', 'VOX_RELEASE_TIME',
    ]),
    ('web', 'Web Configuration', [
        'ENABLE_WEB_CONFIG', 'WEB_CONFIG_PORT', 'WEB_CONFIG_PASSWORD',
        'WEB_CONFIG_HTTPS', 'GATEWAY_NAME', 'WEB_THEME',
        'ENABLE_WEB_MIC', 'WEB_MIC_VOLUME',
        'WEB_MIC_KEY_TIMEOUT', 'WEB_MIC_MAX_TX',
        'ENABLE_WEB_MONITOR', 'WEB_MONITOR_VOLUME', 'MONITOR_VAD_THRESHOLD',
        'ENABLE_CLOUDFLARE_TUNNEL', 'WEB_TUNNEL_ALLOW_NO_PASSWORD',
        'ENABLE_GDRIVE', 'GDRIVE_REMOTE', 'GDRIVE_FOLDER',
    ]),
    ('transcription', 'Transcription', [
        'ENABLE_TRANSCRIPTION',
        'TRANSCRIBE_MODEL',
        'TRANSCRIBE_VAD_THRESHOLD', 'TRANSCRIBE_VAD_HOLD',
        'TRANSCRIBE_MIN_DURATION',
        'TRANSCRIBE_FORWARD_MUMBLE',
    ]),
    ('advanced', 'Advanced / Diagnostics', [
        'HEADLESS_MODE', 'START_CLAUDE_CODE', 'LOG_BUFFER_LINES', 'LOG_FILE_DAYS',
        'VERBOSE_LOGGING', 'STATUS_UPDATE_INTERVAL',
        'NETWORK_TIMEOUT', 'BUFFER_MANAGEMENT_VERBOSE', 'LOOP_RECORD_KBPS',
    ]),
]

NOT_IN_FORM = {
    # Set in the file only; none of these has ever had a form field.
    'ENABLE_CAT_CONTROL': 'alias of ENABLE_TH9800, kept in sync by Config',
    'PACKET_APRS_IS_PASSCODE': 'secret',
    'MANAGER_CLAUDE_BIN': 'file only',
    'MANAGER_CLAUDE_MODEL': 'file only',
    'MANAGER_MAX_TURNS': 'file only',
    'PACKET_APRS_IS': 'file only',
    'PACKET_APRS_IS_SERVER': 'file only',
    'PACKET_LOOPBACK_CARD': 'file only',
    'PACKET_PAT_PORT': 'file only',
    'PACKET_RADIO_ENDPOINT': 'file only',
    'PACKET_UDP_RX_PORT': 'file only',
    'SDR_MODE': 'file only; the SDR page persists its own mode',
    'SUPERVISE_MUMBLE': 'file only',
    'TTS_TARGET_RMS_DB': 'file only',
}
NOT_IN_FORM.update({k: 'file only' for k in _FILE_ONLY_DOCUMENTED})

_TRUE = ('true', 'yes', '1', 'on')
_BOOL_WORDS = ('true', 'false', 'yes', 'no', 'on', 'off')


def coerce(key, text):
    """The value of KEY written as `text`, typed like its default.

    Keys with no default go through infer(). Raises ValueError for a value
    that doesn't fit an int or float key, as the loader always has.
    """
    if key not in DEFAULTS:
        return infer(text)
    kind = type(DEFAULTS[key])
    if kind is bool:
        return text.lower() in _TRUE
    if kind is int:
        if text.startswith('0x'):          # VID/PID
            return int(text, 16)
        try:
            return int(text)
        except ValueError:
            return float(text)             # decimals allowed for int keys
    if kind is float:
        return float(text)
    return text


def infer(text, hex_ok=False):
    """Best-guess type for a value with no default: number, then bool, else str.

    hex_ok also reads 0x.. as an int; plugin sections ([kv4p.vhf] ...) have
    always done that, the flat file has not.
    """
    low = text.lower()
    if hex_ok:
        if low in ('true', 'yes', 'on'):
            return True
        if low in ('false', 'no', 'off'):
            return False
        if text.startswith('0x'):
            try:
                return int(text, 16)
            except ValueError:
                pass
    try:
        return float(text) if '.' in text else int(text)
    except ValueError:
        pass
    if low in _BOOL_WORDS:
        return low in ('true', 'yes', 'on')
    return text


def layout_keys():
    """Every key on the web form, in order."""
    return [k for _, _, keys in LAYOUT for k in keys]


_SECRET_RE = re.compile(r'PASSWORD|SECRET|PASSCODE|TOKEN')


def secret_keys():
    """Keys whose values must never be sent to the browser: every string key
    whose name contains PASSWORD, SECRET, PASSCODE or TOKEN. A pattern
    rather than a list, because a hand-kept list missed four Mumble/CAT
    passwords, which /config then showed in clear."""
    return {k for k, v in DEFAULTS.items()
            if isinstance(v, str) and _SECRET_RE.search(k)}
