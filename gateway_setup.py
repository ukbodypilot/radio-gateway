"""
gateway_setup.py — phased initialization helpers for RadioGateway.setup_audio().

Each function performs one logical setup phase (one plugin / source / service)
and takes the RadioGateway instance as its first argument. Lifted out of
gateway_core.py to keep RadioGateway.setup_audio() short and the phases
individually navigable.

Conventions:
  - Functions don't raise; they print a warning and leave the relevant
    attribute as None on failure. The orchestrator carries on.
  - All required attributes on `gw` (self.config, self.bus_manager, etc.)
    must already exist when these are called.
  - Order matters: SDR must come before TH9800 (rtl_airband fork before
    PortAudio init); CAT serial connect must come after TH9800Plugin
    has produced the cat_client.

Add a new phase by writing a new top-level function here and adding a call
in RadioGateway.setup_audio().
"""

import re
import signal
import threading
import time
import traceback


# ── Phase 1: SDR (must come first — fork before PortAudio init) ────────────

def setup_sdr(gw):
    """Initialize SDR plugin (RSPduo dual tuner).

    Must run before TH-9800/PyAudio init: rtl_airband subprocess forks must
    happen before PortAudio initializes (Pa_Initialize is not fork-safe and
    will SIGSEGV if a fork happens after init).
    """
    gw.sdr_plugin = None
    if not (gw.config.ENABLE_SDR or getattr(gw.config, 'ENABLE_SDR2', False)):
        return
    try:
        from plugins.sdr import SDRPlugin
        print("Initializing SDR plugin (RSPduo dual tuner)...")
        gw.sdr_plugin = SDRPlugin()
        if gw.sdr_plugin.setup(gw.config):
            for _t in [gw.sdr_plugin.get_tuner(1), gw.sdr_plugin.get_tuner(2)]:
                if _t:
                    _t._stream_trace = gw._stream_trace
            print("✓ SDR plugin initialized (routing managed by BusManager)")
        else:
            gw.sdr_plugin = None
    except Exception as e:
        print(f"⚠ Warning: SDR plugin: {e}")
        gw.sdr_plugin = None
    if gw.sdr_plugin:
        gw.sdr_processor = gw.sdr_plugin._processor1
        gw.sdr2_processor = gw.sdr_plugin._processor2


# ── Phase 2: TH-9800 (AIOC + CAT + relays + audio streams) ─────────────────

def setup_th9800(gw):
    """Initialize TH-9800 plugin and propagate backward-compat aliases.

    The plugin owns pyaudio_instance, output_stream, AIOC device, CAT client,
    and relays — but older code paths reference them as gw.<attr> directly,
    so we publish aliases.
    """
    gw.th9800_plugin = None
    try:
        from plugins.th9800 import TH9800Plugin
        print("Initializing TH-9800 plugin...")
        gw.th9800_plugin = TH9800Plugin()
        if gw.th9800_plugin.setup(gw.config, gateway=gw):
            gw.th9800_plugin._stream_trace = gw._stream_trace
            print("✓ TH-9800 plugin initialized (routing managed by BusManager)")
        else:
            print("⚠ TH-9800 plugin setup failed")
            gw.th9800_plugin = None
    except Exception as e:
        print(f"⚠ TH-9800 plugin error: {e}")
        traceback.print_exc()
        gw.th9800_plugin = None

    if gw.th9800_plugin:
        gw.radio_source = gw.th9800_plugin
        gw.pyaudio_instance = gw.th9800_plugin._pyaudio
        gw.input_stream = None  # reader thread owns lifecycle — never cache
        gw.output_stream = gw.th9800_plugin._output_stream
        gw.aioc_device = gw.th9800_plugin._aioc_device
        gw.aioc_available = gw.th9800_plugin._aioc_available
        gw.cat_client = gw.th9800_plugin._cat_client
        gw.radio_processor = gw.th9800_plugin._processor
        gw.relay_radio = gw.th9800_plugin._relay_radio
        gw.relay_ptt = gw.th9800_plugin._relay_ptt
        gw.relay_charger = gw.th9800_plugin._relay_charger
    else:
        gw.radio_source = None

    gw.stream_age = time.time()


# ── Phase 3: Playback sources (speaker, file, loop) ────────────────────────

def setup_playback(gw):
    """Open speaker output + file playback + loop playback sources."""
    from audio_sources import FilePlaybackSource, LoopPlaybackSource
    gw.open_speaker_output()

    if gw.config.ENABLE_PLAYBACK:
        try:
            gw.playback_source = FilePlaybackSource(gw.config, gw)
            print("✓ File playback source initialized (routed via bus manager)")
        except Exception as e:
            print(f"⚠ Warning: Could not initialize playback source: {e}")
            gw.playback_source = None
    else:
        gw.playback_source = None

    gw.loop_playback_source = LoopPlaybackSource(gw)
    # BGM + Announcer are their own sources (own routing nodes) so the bed
    # and the repeating message can play at once and duck against each other.
    from audio_sources import BGMSource, AnnouncerSource
    gw.bgm_source = BGMSource(gw)
    gw.announcer_source = AnnouncerSource(gw)
    try:
        import announcer
        announcer.apply(gw)          # restore the persisted message
    except Exception as _e:
        print(f'  [Announcer] restore failed: {_e}')
    gw.loop_playback_source._stream_trace = gw._stream_trace


# ── Phase 4: TTS engine ────────────────────────────────────────────────────

TTS_ENGINES = ('kokoro', 'edge', 'gtts')

TTS_ENGINE_LABELS = {
    'kokoro': 'Kokoro — offline, 55 voices',
    'edge':   'Edge — Microsoft Neural, 47 voices (needs internet)',
    'gtts':   'gTTS — Google, 22 accents (needs internet)',
}


def apply_tts_engine(gw, backend):
    """(Re)initialise the TTS backend in place. Returns (ok, message).

    Split out of setup_tts so the engine can be swapped at runtime without a
    gateway restart. The swap is atomic from a caller's point of view: the new
    engine is built first, and gw.tts_engine / gw._tts_backend are only
    published together, under _tts_lock, once it succeeded. A failed switch
    therefore leaves the previous working engine in place rather than dropping
    TTS entirely.
    """
    import threading
    if not hasattr(gw, '_tts_lock'):
        gw._tts_lock = threading.Lock()
    backend = str(backend or '').lower().strip()
    if backend not in TTS_ENGINES:
        return False, f"unknown engine '{backend}' (expected one of {', '.join(TTS_ENGINES)})"

    engine = None
    try:
        if backend == 'kokoro':
            import os as _os
            from kokoro_onnx import Kokoro
            _base = _os.path.join(_os.path.dirname(__file__), 'tools', 'models', 'kokoro')
            _model = _os.path.join(_base, 'kokoro-v1.0.onnx')
            _voices = _os.path.join(_base, 'voices-v1.0.bin')
            if not _os.path.exists(_model) or not _os.path.exists(_voices):
                return False, f"Kokoro model files missing in {_base}"
            # Reuse an already-loaded Kokoro rather than paying the model load
            # again on every switch back to it.
            engine = getattr(gw, '_kokoro_instance', None) or Kokoro(_model, _voices)
            gw._kokoro_instance = engine
        elif backend == 'edge':
            import edge_tts
            engine = edge_tts
        else:
            from gtts import gTTS
            engine = gTTS
    except ImportError as e:
        pkg = {'kokoro': 'kokoro-onnx', 'edge': 'edge-tts', 'gtts': 'gtts'}[backend]
        return False, f"{pkg} not installed ({e}) — pip3 install {pkg} --break-system-packages"
    except Exception as e:
        return False, f"could not initialise {backend}: {e}"

    with gw._tts_lock:
        gw.tts_engine = engine
        gw._tts_backend = backend
    return True, TTS_ENGINE_LABELS.get(backend, backend)


def switch_tts_engine(gw, backend, persist=True):
    """Swap the live TTS engine and optionally write it to the config file."""
    ok, msg = apply_tts_engine(gw, backend)
    if ok and persist:
        try:
            gw.config.TTS_ENGINE = gw._tts_backend
        except Exception:
            pass
    return ok, msg


def setup_tts(gw):
    """Initialize text-to-speech (Kokoro ONNX, Edge TTS, or gTTS)."""
    import threading
    gw.tts_engine = None
    gw._tts_lock = threading.Lock()
    gw._tts_backend = str(getattr(gw.config, 'TTS_ENGINE', 'kokoro')).lower().strip()
    gw._kokoro_instance = None  # lazy-initialised on first use
    if not gw.config.ENABLE_TTS:
        print("  Text-to-speech: DISABLED (set ENABLE_TTS = true to enable)")
        return
    try:
        print("Initializing text-to-speech...")
        if gw._tts_backend == 'kokoro':
            import os as _os
            from kokoro_onnx import Kokoro
            _base = _os.path.join(_os.path.dirname(__file__), 'tools', 'models', 'kokoro')
            _model = _os.path.join(_base, 'kokoro-v1.0.onnx')
            _voices = _os.path.join(_base, 'voices-v1.0.bin')
            if not _os.path.exists(_model) or not _os.path.exists(_voices):
                print(f"⚠ Kokoro model files missing in {_base}")
                print(f"  Download with:")
                print(f"  wget -P {_base} https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx")
                print(f"  wget -P {_base} https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin")
                gw.tts_engine = None
                return
            gw.tts_engine = Kokoro(_model, _voices)
            gw._kokoro_instance = gw.tts_engine
            print("✓ Text-to-speech (Kokoro ONNX / offline) initialized")
        elif gw._tts_backend == 'edge':
            import edge_tts
            gw.tts_engine = edge_tts
            print("✓ Text-to-speech (Edge TTS / Microsoft Neural) initialized")
        else:
            from gtts import gTTS
            gw.tts_engine = gTTS
            print("✓ Text-to-speech (gTTS / Google) initialized")
        print("  Use !speak <text> in Mumble to generate TTS")
    except ImportError as e:
        if gw._tts_backend == 'kokoro':
            pkg = 'kokoro-onnx'
        elif gw._tts_backend == 'edge':
            pkg = 'edge-tts'
        else:
            pkg = 'gtts'
        print(f"⚠ {pkg} not installed: {e}")
        print(f"  Install with: pip3 install {pkg} --break-system-packages")
        gw.tts_engine = None
    except Exception as e:
        print(f"⚠ Warning: Could not initialize TTS: {e}")
        gw.tts_engine = None


# ── Phase 5: Remote audio TX + RX ──────────────────────────────────────────

def setup_remote_audio(gw):
    """Initialize remote-audio TX server and RX source if enabled."""
    from audio_sources import RemoteAudioServer, RemoteAudioSource
    role = getattr(gw.config, 'REMOTE_AUDIO_ROLE', 'disabled').lower().strip("'\"")
    if role == 'disabled':
        return

    # TX server: connects out to remote client
    try:
        if not gw.config.REMOTE_AUDIO_HOST:
            print("⚠ Warning: REMOTE_AUDIO_HOST not set — TX server needs a destination IP")
        else:
            gw.remote_audio_server = RemoteAudioServer(gw.config)
            gw.remote_audio_server.start()
    except Exception as e:
        print(f"⚠ Warning: Could not start remote audio TX server: {e}")
        gw.remote_audio_server = None

    # RX source: listens for inbound audio
    try:
        rx_port = int(getattr(gw.config, 'REMOTE_AUDIO_RX_PORT', 9602))
        print(f"Initializing remote audio RX source (listening on 0.0.0.0:{rx_port})...")
        gw.remote_audio_source = RemoteAudioSource(gw.config, gw)
        if gw.remote_audio_source.setup_audio(port_override=rx_port):
            gw.remote_audio_source.enabled = True
            gw.remote_audio_source.duck = gw.config.REMOTE_AUDIO_DUCK
            gw.remote_audio_source.sdr_priority = int(gw.config.REMOTE_AUDIO_PRIORITY)
            print(f"✓ Remote audio RX source initialized (routing managed by BusManager)")
        else:
            print("⚠ Warning: Could not initialize remote audio RX source")
            gw.remote_audio_source = None
    except Exception as e:
        print(f"⚠ Warning: Could not initialize remote audio RX source: {e}")
        gw.remote_audio_source = None


# ── Phase 6: Announcement input (network → PTT) ────────────────────────────

def setup_announce_input(gw):
    from audio_sources import NetworkAnnouncementSource
    if not getattr(gw.config, 'ENABLE_ANNOUNCE_INPUT', False):
        return
    try:
        bind_host = gw.config.ANNOUNCE_INPUT_HOST or '0.0.0.0'
        port = gw.config.ANNOUNCE_INPUT_PORT
        print(f"Initializing announcement input (listening on {bind_host}:{port})...")
        gw.announce_input_source = NetworkAnnouncementSource(gw.config, gw)
        if gw.announce_input_source.setup_audio():
            print(f"✓ Announcement input (ANNIN) initialized (routing managed by BusManager)")
            if not gw.aioc_available:
                print("  ⚠ No AIOC — PTT will not activate (audio discarded)")
        else:
            print("⚠ Warning: Could not initialize announcement input")
            gw.announce_input_source = None
    except Exception as e:
        print(f"⚠ Warning: Could not initialize announcement input: {e}")
        gw.announce_input_source = None


# ── Phase 7: Web mic + monitor sources ─────────────────────────────────────

def setup_web_audio(gw):
    """Browser mic → radio TX, and browser mic → mixer (no PTT)."""
    from audio_sources import WebMicSource, WebMonitorSource
    if getattr(gw.config, 'ENABLE_WEB_MIC', True):
        try:
            gw.web_mic_source = WebMicSource(gw.config, gw)
            if gw.web_mic_source.setup_audio():
                print("✓ Web microphone source (WEBMIC) initialized (routing managed by BusManager)")
        except Exception as e:
            print(f"⚠ Warning: Could not initialize web mic source: {e}")
            gw.web_mic_source = None

    if getattr(gw.config, 'ENABLE_WEB_MONITOR', True):
        try:
            gw.web_monitor_source = WebMonitorSource(gw.config, gw)
            if gw.web_monitor_source.setup_audio():
                print("✓ Web monitor source (MONITOR) initialized (routing managed by BusManager)")
        except Exception as e:
            print(f"⚠ Warning: Could not initialize web monitor source: {e}")
            gw.web_monitor_source = None


# ── Phase 8: KV4P loopback endpoints ───────────────────────────────────────

import os


def setup_kv4p_loopback_endpoints(gw):
    """Spawn one supervised link_endpoint.py child per [kv4p.*] section.

    Each child connects to the gateway's own link server at 127.0.0.1:LINK_PORT
    and appears in BusManager / web routing identically to a remote endpoint —
    so multiple kv4ps (VHF + UHF + ...) are independently routable.

    Legacy [kv4p] config blocks are migrated in place to [kv4p.vhf] on first
    run; a one-time backup is written to gateway_config.txt.legacy_kv4p.bak.
    """
    import os
    import sys
    from endpoints_state import migrate_legacy_kv4p_block, read_sections

    # Convert any legacy single-instance config block. Idempotent.
    try:
        if migrate_legacy_kv4p_block():
            print("  [KV4P] Migrated legacy [kv4p] block → [kv4p.vhf] "
                  "(backup: gateway_config.txt.legacy_kv4p.bak)")
    except Exception as e:
        print(f"  [KV4P] Legacy migration error: {e}")

    sections = read_sections('kv4p')
    if not sections:
        return

    link_port = int(getattr(gw.config, 'LINK_PORT', 9700))
    repo_root = os.path.dirname(os.path.abspath(__file__))
    endpoint_script = os.path.join(repo_root, 'tools', 'link_endpoint.py')

    spawned = 0
    for instance, cfg in sections.items():
        if not cfg.get('enable', True):
            continue
        # Display name strips trailing 'hf' from the band suffix so
        # endpoints register as 'kv4p-v' / 'kv4p-u' instead of the noisier
        # 'kv4p-vhf' / 'kv4p-uhf'. Config section name + internal source/
        # sink IDs (kv4p_vhf, kv4p_vhf_tx, JS instance: 'vhf') are
        # unchanged — this is purely the on-the-wire endpoint name used
        # by the link server / status UI.
        suffix = instance[:-2] if instance.lower().endswith('hf') else instance
        name = f'kv4p-{suffix or instance}'
        port_path = cfg.get('port') or cfg.get('device') or '/dev/ttyUSB0'

        # Pass the whole section as JSON so the plugin gets per-instance
        # knobs (default_freq, ctcss, processing flags, etc.) without
        # bloating the link_endpoint CLI.
        import json as _json
        cfg_json = _json.dumps({k: v for k, v in cfg.items()
                                if k not in ('enable',)})
        argv = [
            sys.executable, endpoint_script,
            '--server', f'127.0.0.1:{link_port}',
            '--name', name,
            '--plugin', 'kv4p',
            '--device', str(port_path),
            '--plugin-config-json', cfg_json,
            '--no-update',
        ]
        try:
            gw.process_supervisor.add(
                name, argv,
                cwd=repo_root,
                restart=True, backoff=(2, 30),
            )
            spawned += 1
            print(f"  [KV4P] Loopback endpoint '{name}' supervised "
                  f"(port={port_path})")
        except Exception as e:
            print(f"  [KV4P] Failed to spawn '{name}': {e}")

    if spawned:
        print(f"✓ KV4P: {spawned} loopback endpoint(s) spawned")


# ── Phase 9: Packet radio (Direwolf TNC) ───────────────────────────────────

def setup_packet(gw):
    gw.packet_plugin = None
    if not getattr(gw.config, 'ENABLE_PACKET', False):
        return
    try:
        from plugins.packet import PacketRadioPlugin
        print("Initializing Packet Radio plugin...")
        gw.packet_plugin = PacketRadioPlugin()
        if gw.packet_plugin.setup(gw.config, gateway=gw):
            print("✓ Packet Radio plugin initialized (routed via bus manager)")
        else:
            print("⚠ Warning: Packet Radio plugin setup failed")
            gw.packet_plugin = None
    except Exception as e:
        print(f"⚠ Packet Radio plugin error: {e}")
        traceback.print_exc()
        gw.packet_plugin = None


# ── Phase 10: Gateway Link server + endpoint callbacks ─────────────────────

def _sanitize_ep_name(n):
    return re.sub(r'[^a-z0-9_]', '_', n.lower()).strip('_')


def _make_link_callbacks(gw):
    """Build the four endpoint callbacks for GatewayLinkServer.

    Kept as a closure factory so the callbacks can close over gw without
    polluting the module namespace.
    """
    from audio_sources import LinkAudioSource
    from endpoints_state import (build_restore_commands, extract_state_from_status,
                                 get_endpoint as _get_saved,
                                 update_endpoint as _save_state)

    def _is_kv4p(name):
        return isinstance(name, str) and name.startswith('kv4p-')

    def _push_restore(name):
        """Send last-known freq/CTCSS/power to a kv4p endpoint after it connects."""
        saved = _get_saved(name)
        cmds = build_restore_commands(saved)
        if not cmds:
            return
        # Brief delay so the endpoint-side plugin has finished setup()
        # before commands start arriving.
        def _send():
            time.sleep(1.5)
            srv = getattr(gw, 'link_server', None)
            if not srv:
                return
            for c in cmds:
                try:
                    srv.send_command_to(name, c)
                    time.sleep(0.2)
                except Exception as e:
                    print(f"  [Link] restore '{c.get('cmd')}' "
                          f"to {name} failed: {e}")
            print(f"  [Link] Restored {len(cmds)} settings on {name}")
        threading.Thread(target=_send, daemon=True,
                         name=f'restore-{name}').start()

    def on_register(info):
        name = info.get('name', '')
        if not name:
            return None
        src = LinkAudioSource(gw.config, gw, endpoint_name=name)
        src.setup_audio()
        src.enabled = True

        # Plugin type is the stable routing ID — survives endpoint renames.
        # Fall back to sanitized name for generic 'audio' plugins or
        # when the plugin type would collide with a builtin.
        _plugin = info.get('plugin', 'audio')
        src.plugin_type = _plugin
        # 'kv4p' stays in the set despite there being no in-core kv4p source:
        # forces every kv4p endpoint to use a sanitized name as routing id
        # ('kv4p_vhf', 'kv4p_uhf') so they don't fight over the bare 'kv4p'
        # id. Bus manager + web routing assume this naming.
        _builtin_ids = {
            'aioc', 'kv4p', 'sdr', 'sdr1', 'sdr2',
            'playback', 'loop_playback', 'webmic',
            'announce', 'monitor', 'mumble_rx',
            'remote_audio', 'echolink',
        }
        _existing_ids = {getattr(s, 'source_id', None)
                         for s in gw.link_endpoints.values()}
        if (_plugin and _plugin != 'audio'
                and _plugin not in _builtin_ids
                and _plugin not in _existing_ids):
            _raw_id = _plugin
        else:
            _raw_id = _sanitize_ep_name(name)

        src.source_id = _raw_id
        src.sink_id = _raw_id + '_tx'
        print(f"  [Link] {name}: source_id={src.source_id} "
              f"sink_id={src.sink_id} plugin={_plugin}")

        saved = gw.link_endpoint_settings.get(name, {})
        src.muted = saved.get('rx_muted', False)
        if 'rx_boost' in saved:
            src.audio_boost = saved['rx_boost'] / 100.0
        if 'tx_boost' in saved:
            src.tx_audio_boost = saved['tx_boost'] / 100.0
        # Per-endpoint jitter cushion. A wired LAN endpoint can run tighter
        # than one over WiFi or a tunnel, and they reconnect independently,
        # so this belongs per endpoint rather than in the global config.
        if 'jitter_prefill' in saved:
            src.set_jitter_prefill(saved['jitter_prefill'])
        src.server_connected = True
        src._stream_trace = gw._stream_trace
        src._endpoint_caps = info.get('capabilities', {})
        gw.link_endpoints[name] = src
        print(f"  [Link] {name} registered (routing managed by BusManager)")
        gw._link_ptt_active[name] = False
        gw._link_last_status[name] = {}
        gw._link_tx_levels[name] = 0
        if _is_kv4p(name):
            _push_restore(name)
        if hasattr(gw, 'bus_manager') and gw.bus_manager:
            try:
                gw.bus_manager.reload()
                print(f"  [Link] Bus manager reloaded for {name}")
            except Exception as _bme:
                print(f"  [Link] Bus reload error: {_bme}")
        # Re-send data mode if packet plugin was active on this endpoint
        if gw.packet_plugin and gw.packet_plugin._mode in ('winlink', 'bbs', 'aprs'):
            _pp = gw.packet_plugin
            if _pp._find_endpoint() == name:
                threading.Thread(
                    target=lambda: _pp._send_endpoint_mode('data'),
                    daemon=True, name='pkt-mode-restore',
                ).start()
                print(f"  [Link] Restoring data mode on reconnected endpoint {name}")
        return src

    def on_disconnect(name):
        src = gw.link_endpoints.pop(name, None)
        if src:
            src.server_connected = False
            if gw.bus_manager and gw.bus_manager.listen_bus:
                gw.bus_manager.listen_bus.remove_source(src.name)
        gw._link_ptt_active.pop(name, None)
        gw._link_last_status.pop(name, None)
        gw._link_tx_levels.pop(name, None)

    def on_ack(name, ack):
        cmd = ack.get('cmd', '')
        result = ack.get('result', {})
        if not isinstance(result, dict):
            return
        if cmd == 'ptt':
            gw._link_ptt_active[name] = result.get('ptt', False)
            return
        if name not in gw._link_last_status:
            gw._link_last_status[name] = {}
        if cmd == 'status':
            gw._link_last_status[name].update(result.get('status', result))
            if _is_kv4p(name):
                _persist_kv4p(name, result.get('status', result))
            return
        # Any other command — merge its result fields (squelch, rf_power,
        # rit_hz, agc, rx_gain_db, ...) straight into the cached endpoint
        # status. Without this the cache only refreshes on the endpoint's
        # periodic status push (up to _POLL_INTERVAL seconds away), so a
        # web knob/slider snaps back to the stale value in the meantime.
        gw._link_last_status[name].update(
            {k: v for k, v in result.items()
             if k not in ('ok', 'error', 'response')})

    def on_endpoint_status(name, status):
        if not isinstance(status, dict) or status.get('type') == 'heartbeat':
            return
        # Forward Direwolf log lines to packet plugin
        # direwolf_log status frames are no longer sent — direwolf is a
        # gateway-side process now (packet_tnc.py captures its stdout).
        if name not in gw._link_last_status:
            gw._link_last_status[name] = {}
        gw._link_last_status[name].update(status)
        if 'ptt_active' in status:
            gw._link_ptt_active[name] = status['ptt_active']
        if _is_kv4p(name):
            _persist_kv4p(name, status)

    def _persist_kv4p(name, status):
        """Extract persistable fields from a status dict and update state JSON."""
        try:
            fields = extract_state_from_status(status)
            if fields:
                _save_state(name, fields)
        except Exception as e:
            print(f"  [Link] persist {name} state error: {e}")

    return on_register, on_disconnect, on_ack, on_endpoint_status


def setup_gateway_link(gw):
    """Start the Gateway Link server (duplex audio + command protocol)."""
    if not getattr(gw.config, 'ENABLE_GATEWAY_LINK', False):
        return
    try:
        from gateway_link import GatewayLinkServer
        link_port = int(getattr(gw.config, 'LINK_PORT', 9700))
        print(f"Initializing Gateway Link server (port {link_port})...")
        gw._load_link_settings()

        on_register, on_disconnect, on_ack, on_endpoint_status = _make_link_callbacks(gw)

        # Per-endpoint log store — endpoints ship stdout/stderr over the
        # link as P.LOG frames; the store appends to rotating files under
        # logs/endpoints/. See docs/endpoint_logs_design.md.
        from core.endpoint_logs import EndpointLogStore
        gw.endpoint_log_store = EndpointLogStore()

        gw.link_server = GatewayLinkServer(
            port=link_port,
            on_register=on_register,
            on_disconnect=on_disconnect,
            on_ack=on_ack,
            on_endpoint_status=on_endpoint_status,
            on_log_lines=gw.endpoint_log_store.append,
            supervisor=gw.process_supervisor,
        )
        gw.link_server.start()
        print(f"  Gateway Link listening on port {link_port}")
    except Exception as e:
        print(f"  Gateway Link error: {e}")
        traceback.print_exc()
        gw.link_server = None
        gw.link_audio_source = None


# ── Phase 11: Mumble local servers ─────────────────────────────────────────

def setup_mumble_servers(gw):
    from gateway_utils import MumbleServerManager
    for i in (1, 2):
        cfg_key = f'ENABLE_MUMBLE_SERVER_{i}'
        attr_key = f'mumble_server_{i}'
        if not getattr(gw.config, cfg_key, False):
            continue
        try:
            print(f"Initializing Mumble Server {i}...")
            mgr = MumbleServerManager(i, gw.config)
            setattr(gw, attr_key, mgr)
            mgr.start()
            state, port = mgr.get_status()
            if state == MumbleServerManager.STATE_RUNNING:
                print(f"  Mumble Server {i}: running on port {port}")
            elif state == MumbleServerManager.STATE_CONFIGURED:
                print(f"  Mumble Server {i}: configured on port {port} (autostart=false)")
            elif state == MumbleServerManager.STATE_ERROR:
                print(f"  Mumble Server {i}: ERROR — {mgr.error_msg}")
        except Exception as e:
            print(f"  Warning: Mumble Server {i} init failed: {e}")
            mgr = getattr(gw, attr_key, None)
            if mgr:
                mgr.state = MumbleServerManager.STATE_ERROR
                mgr.error_msg = str(e)


# ── Phase 12-21: small services with the same pattern ─────────────────────

def setup_smart_announce(gw):
    if not getattr(gw.config, 'ENABLE_SMART_ANNOUNCE', False):
        return
    try:
        from smart_announce import SmartAnnouncementManager
        gw.smart_announce = SmartAnnouncementManager(gw)
        gw.smart_announce.start()
    except Exception as e:
        print(f"  [SmartAnnounce] Init error: {e}")


def setup_web_config(gw):
    if not getattr(gw.config, 'ENABLE_WEB_CONFIG', False):
        return
    try:
        from web_server import WebConfigServer
        gw.web_config_server = WebConfigServer(gw.config, gateway=gw)
        gw.web_config_server.start()
    except Exception as e:
        print(f"  [WebConfig] Init error: {e}")


def setup_manager_engine(gw):
    """Fleet Manager Engine — always on; manages its own enabled state."""
    try:
        from manager_engine import ManagerEngine
        gw.manager_engine = ManagerEngine(gw.config, gateway=gw)
        gw.manager_engine.start()
        print(f"  [Manager] Fleet Manager Engine started")
    except Exception as e:
        print(f"  [Manager] Init error: {e}")


def setup_alert_engine(gw):
    """In-process alert engine — polls local Prometheus, sends email.

    Skipped silently if local Prometheus isn't reachable; the gateway works
    fine without it, the only loss is alerting (and the Manager docs already
    cover threshold queries from their side).
    """
    if not getattr(gw.config, 'ENABLE_ALERT_ENGINE', False):
        print(f"  [Alerts] disabled via config")
        return
    try:
        from alerts import AlertEngine
        prom_url = str(getattr(gw.config, 'PROMETHEUS_URL',
                               'http://127.0.0.1:9090/prometheus')).strip()
        poll = int(getattr(gw.config, 'ALERT_POLL_INTERVAL', 30))
        gw.alert_engine = AlertEngine(gw, prom_url=prom_url, poll_interval=poll)
        gw.alert_engine.start()
    except Exception as e:
        print(f"  [Alerts] Init error: {e}")


def setup_ddns(gw):
    if not getattr(gw.config, 'ENABLE_DDNS', False):
        return
    try:
        from gateway_utils import DDNSUpdater
        gw.ddns_updater = DDNSUpdater(gw.config, gw)
        gw.ddns_updater.start()
    except Exception as e:
        print(f"  [DDNS] Init error: {e}")


def setup_supervised_streamers(gw):
    """Register mumble-server with ProcessSupervisor when opted in.

    Default is off — the service keeps its existing systemd-managed
    behaviour. When SUPERVISE_MUMBLE=true, the gateway becomes the
    supervisor and restarts the service on death.
    """
    sup = getattr(gw, 'process_supervisor', None)
    if not sup:
        return

    if bool(getattr(gw.config, 'SUPERVISE_MUMBLE', False)):
        # mumble-server-gw1.service / -gw2 are still defined; flipping this
        # flag tells the supervisor to spawn murmurd directly so the gateway
        # owns the process. Requires running as root or with sudoers entries.
        import shutil
        murmurd = (shutil.which('murmurd') or shutil.which('mumble-server')
                   or '/usr/bin/mumble-server')
        for n in (1, 2):
            cfg = f'/etc/mumble-server-gw{n}.ini'
            if not os.path.exists(cfg):
                continue
            try:
                sup.add(
                    f'mumble-gw{n}',
                    [murmurd, '-fg', '-ini', cfg],
                    restart=True, backoff=(5, 60),
                    run_as_user='_mumble-server',  # falls back to 'mumble-server'
                )
                print(f"  [Stream] mumble-gw{n} supervised")
            except ValueError as e:
                if 'unknown user' in str(e):
                    try:
                        sup.add(
                            f'mumble-gw{n}',
                            [murmurd, '-fg', '-ini', cfg],
                            restart=True, backoff=(5, 60),
                            run_as_user='mumble-server',
                        )
                        print(f"  [Stream] mumble-gw{n} supervised "
                              f"(as 'mumble-server')")
                    except Exception as e2:
                        print(f"  [Stream] mumble-gw{n} supervisor add failed: {e2}")
            except Exception as e:
                print(f"  [Stream] mumble-gw{n} supervisor add failed: {e}")


def setup_cloudflare_tunnel(gw):
    if not getattr(gw.config, 'ENABLE_CLOUDFLARE_TUNNEL', False):
        return
    try:
        from gateway_utils import CloudflareTunnel
        gw.cloudflare_tunnel = CloudflareTunnel(
            gw.config,
            on_url_changed=gw._on_tunnel_url_changed,
            supervisor=gw.process_supervisor)
        gw.cloudflare_tunnel.start()
    except Exception as e:
        print(f"  [Tunnel] Init error: {e}")


def setup_email(gw):
    if not getattr(gw.config, 'ENABLE_EMAIL', False):
        return
    try:
        from gateway_utils import EmailNotifier
        gw.email_notifier = EmailNotifier(gw.config, gw)
        if gw.email_notifier.is_configured():
            print(f"  [Email] Notifier ready ({gw.email_notifier._recipient})")
            if getattr(gw.config, 'EMAIL_ON_STARTUP', True):
                gw.email_notifier.send_startup_delayed()
            periodic_hours = int(getattr(gw.config, 'EMAIL_PERIODIC_HOURS', 24) or 0)
            if periodic_hours > 0:
                gw.email_notifier.start_periodic_status(periodic_hours * 3600)
        else:
            print(f"  [Email] Missing credentials — skipping")
            gw.email_notifier = None
    except Exception as e:
        print(f"  [Email] Init error: {e}")


def setup_gdrive(gw):
    gw.gdrive = None
    if not getattr(gw.config, 'ENABLE_GDRIVE', False):
        return
    try:
        from gdrive import GDriveClient
        _remote = str(getattr(gw.config, 'GDRIVE_REMOTE', 'gdrive'))
        _folder = str(getattr(gw.config, 'GDRIVE_FOLDER', 'radio-gateway'))
        _rclone_conf = str(getattr(gw.config, 'GDRIVE_RCLONE_CONFIG', '') or '')
        gw.gdrive = GDriveClient(
            remote=_remote,
            folder_path=_folder,
            rclone_config=_rclone_conf or None)
        if gw.cloudflare_tunnel:
            threading.Thread(target=gw._publish_tunnel_url,
                             daemon=True, name="gdrive-publish").start()
    except Exception as e:
        print(f"  [GDrive] Init error: {e}")


def setup_gps(gw):
    if not getattr(gw.config, 'ENABLE_GPS', False):
        return
    try:
        from gateway_utils import GPSManager
        gw.gps_manager = GPSManager(gw.config)
        gw.gps_manager.start()
    except Exception as e:
        print(f"  [GPS] Init error: {e}")


def setup_repeaters(gw):
    if not getattr(gw.config, 'ENABLE_REPEATER_DB', False):
        return
    try:
        from repeater_manager import RepeaterManager
        gw.repeater_manager = RepeaterManager(gw.config, gw.gps_manager)
        gw.repeater_manager.start()
    except Exception as e:
        print(f"  [Repeaters] Init error: {e}")


def setup_echolink(gw):
    gw.echolink_source = None
    if not gw.config.ENABLE_ECHOLINK:
        return
    try:
        from audio_sources import EchoLinkSource
        print("Initializing EchoLink integration...")
        gw.echolink_source = EchoLinkSource(gw.config, gw)
        if gw.echolink_source.connected:
            print("✓ EchoLink source initialized (routing managed by BusManager)")
            print("  Audio routing:")
            if gw.config.ECHOLINK_TO_MUMBLE: print("    EchoLink → Mumble: ON")
            if gw.config.ECHOLINK_TO_RADIO:  print("    EchoLink → Radio TX: ON")
            if gw.config.RADIO_TO_ECHOLINK:  print("    Radio RX → EchoLink: ON")
            if gw.config.MUMBLE_TO_ECHOLINK: print("    Mumble → EchoLink: ON")
        else:
            print("  ✗ EchoLink IPC not available")
            print("    Make sure TheLinkBox is running")
            gw.echolink_source = None
    except Exception as e:
        print(f"⚠ Warning: Could not initialize EchoLink: {e}")
        gw.echolink_source = None


def setup_streaming(gw):
    gw.stream_output = None
    if not gw.config.ENABLE_STREAM_OUTPUT:
        return
    try:
        from audio_sources import StreamOutputSource
        print("Connecting to Icecast server...")
        gw.stream_output = StreamOutputSource(gw.config, gw)
        if gw.stream_output.connected:
            print("✓ Icecast streaming active")
            print(f"  Listen at: http://{gw.config.STREAM_SERVER}:{gw.config.STREAM_PORT}{gw.config.STREAM_MOUNT}")
        else:
            print("  ✗ Icecast connection failed")
            gw.stream_output = None
    except Exception as e:
        print(f"⚠ Warning: Could not initialize streaming: {e}")
        gw.stream_output = None


# ── Phase 22-23: CAT serial connect + startup commands ─────────────────────

def setup_cat_connect(gw):
    """Reconnect TH-9800 serial and send CAT startup commands.

    Must run after TH9800Plugin has set up gw.cat_client. Sends a sequence
    via the cat_client's socket to reach the radio over the network bridge.
    """
    if not gw.cat_client:
        return
    print("Connecting TH-9800 serial...")
    try:
        with gw.cat_client._sock_lock:
            gw.cat_client._sock.sendall(b"!serial disconnect\n")
            gw.cat_client._recv_line(timeout=3.0)
        time.sleep(2)
        with gw.cat_client._sock_lock:
            gw.cat_client._sock.sendall(b"!serial connect\n")
            gw.cat_client._last_activity = time.monotonic()
            connect_resp = gw.cat_client._recv_line(timeout=10.0)
        if connect_resp and 'serial connected' in connect_resp:
            gw.cat_client._serial_connected = True
            print(f"  Serial connected: {connect_resp}")
            try:
                gw.cat_client.set_rts(True)
            except Exception:
                pass
        else:
            print(f"  Serial connect failed: {connect_resp}")
    except Exception as e:
        print(f"  Serial connect error: {e}")

    if not gw.config.CAT_STARTUP_COMMANDS:
        print("  CAT startup commands disabled (CAT_STARTUP_COMMANDS = false)")
        return

    print("Sending CAT startup commands...")
    _cat_ref = gw.cat_client
    _prev_handler = signal.getsignal(signal.SIGINT)

    def _cat_sigint(sig, frame):
        _cat_ref._stop = True

    signal.signal(signal.SIGINT, _cat_sigint)
    try:
        gw.cat_client.setup_radio(gw.config)
    except KeyboardInterrupt:
        gw.cat_client._stop = True
    finally:
        signal.signal(signal.SIGINT, _prev_handler)
    if gw.cat_client._stop:
        print("\n  CAT setup interrupted")
