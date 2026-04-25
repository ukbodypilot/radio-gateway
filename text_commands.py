"""Text command handlers -- Mumble chat commands, keyboard dispatch, TTS.

Extracted from gateway_core.py. These are command dispatchers that
parse user input and call gateway methods.

Each function takes ``gw`` (the RadioGateway instance) as its first
parameter instead of ``self``.
"""

import os
import re
import tempfile
import threading
import time
from html import unescape

import numpy as np

from audio_sources import generate_cw_pcm


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def speak_text(gw, text, voice=None):
    """
    Generate TTS audio from text and play it on radio

    Args:
        text: Text to convert to speech
        voice: Optional voice number (1-9), defaults to TTS_DEFAULT_VOICE config

    Returns:
        bool: True if successful, False otherwise
    """
    if not gw.tts_engine:
        gw.notify("TTS not available (install edge-tts or gtts)")
        return False

    if not gw.playback_source:
        gw.notify("TTS failed: playback source not available")
        return False

    try:
        if gw.config.VERBOSE_LOGGING:
            print(f"\n[TTS] Generating speech: {text[:50]}...")

        # Create temporary file
        temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        temp_path = temp_file.name
        temp_file.close()

        voice_num = voice or int(getattr(gw.config, 'TTS_DEFAULT_VOICE', 1))

        if gw._tts_backend == 'edge':
            # Edge TTS — Microsoft Neural voices (natural sounding)
            edge_voice, voice_desc = gw.EDGE_TTS_VOICES.get(voice_num, gw.EDGE_TTS_VOICES[1])
            if gw.config.VERBOSE_LOGGING:
                print(f"[TTS] Calling Edge TTS (voice {voice_num}: {voice_desc})...")
            try:
                import asyncio
                communicate = gw.tts_engine.Communicate(text, edge_voice)
                asyncio.run(communicate.save(temp_path))
                if gw.config.VERBOSE_LOGGING:
                    print(f"[TTS] ✓ Audio file saved")
            except Exception as tts_error:
                print(f"[TTS] ✗ Edge TTS generation failed: {tts_error}")
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                return False
        else:
            # gTTS — Google Translate voices (robotic but reliable)
            lang, tld, voice_desc = gw.TTS_VOICES.get(voice_num, gw.TTS_VOICES[1])
            if gw.config.VERBOSE_LOGGING:
                print(f"[TTS] Calling gTTS (voice {voice_num}: {voice_desc})...")
            try:
                tts = gw.tts_engine(text, lang=lang, tld=tld, slow=False)
                if gw.config.VERBOSE_LOGGING:
                    print(f"[TTS] Saving to {temp_path}...")
                tts.save(temp_path)
                if gw.config.VERBOSE_LOGGING:
                    print(f"[TTS] ✓ Audio file saved")
            except Exception as tts_error:
                print(f"[TTS] ✗ gTTS generation failed: {tts_error}")
                print(f"[TTS] Check internet connection (gTTS requires internet)")
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                return False

        # Apply speed adjustment if configured
        tts_speed = float(getattr(gw.config, 'TTS_SPEED', 1.0))
        if tts_speed != 1.0 and 0.5 <= tts_speed <= 3.0:
            try:
                import subprocess as sp
                speed_path = temp_path + '.speed.mp3'
                # ffmpeg atempo range is 0.5-2.0; chain filters for values outside
                filters = []
                remaining = tts_speed
                while remaining > 2.0:
                    filters.append('atempo=2.0')
                    remaining /= 2.0
                filters.append(f'atempo={remaining:.4f}')
                sp.run(['ffmpeg', '-y', '-i', temp_path, '-filter:a',
                        ','.join(filters), speed_path],
                       capture_output=True, timeout=30)
                if os.path.exists(speed_path) and os.path.getsize(speed_path) > 500:
                    os.replace(speed_path, temp_path)
                    if gw.config.VERBOSE_LOGGING:
                        print(f"[TTS] Speed adjusted to {tts_speed}x")
                else:
                    print(f"[TTS] ⚠ Speed adjustment failed, using original")
                    try:
                        os.unlink(speed_path)
                    except Exception:
                        pass
            except Exception as speed_err:
                print(f"[TTS] ⚠ Speed adjustment error: {speed_err}")
                try:
                    os.unlink(speed_path)
                except Exception:
                    pass

        # Verify file exists and has valid content
        if not os.path.exists(temp_path):
            print(f"[TTS] ✗ File not created!")
            return False

        size = os.path.getsize(temp_path)
        if gw.config.VERBOSE_LOGGING:
            print(f"[TTS] File size: {size} bytes")

        # Validate it's actually an MP3 file, not an HTML error page
        # MP3 files start with ID3 tag or MPEG frame sync
        try:
            with open(temp_path, 'rb') as f:
                header = f.read(10)

                # Check for ID3 tag (ID3v2)
                is_mp3 = header.startswith(b'ID3')

                # Check for MPEG frame sync (0xFF 0xFB or 0xFF 0xF3)
                if not is_mp3 and len(header) >= 2:
                    is_mp3 = (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)

                # Check if it's HTML (error page)
                is_html = header.startswith(b'<!DOCTYPE') or header.startswith(b'<html')

                if is_html:
                    print(f"[TTS] ✗ gTTS returned HTML error page, not MP3")
                    print(f"[TTS] This usually means:")
                    print(f"  - Rate limiting from Google")
                    print(f"  - Network/firewall blocking")
                    print(f"  - Invalid characters in text")
                    # Read first 200 chars to show error
                    f.seek(0)
                    error_preview = f.read(200).decode('utf-8', errors='ignore')
                    print(f"[TTS] Error preview: {error_preview[:100]}")
                    os.unlink(temp_path)
                    return False

                if not is_mp3:
                    print(f"[TTS] ✗ File doesn't appear to be valid MP3")
                    print(f"[TTS] Header: {header.hex()}")
                    os.unlink(temp_path)
                    return False

                if gw.config.VERBOSE_LOGGING:
                    print(f"[TTS] ✓ Validated MP3 file format")

        except Exception as val_err:
            print(f"[TTS] ✗ Could not validate file: {val_err}")
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            return False

        # File is valid MP3
        if size < 1000:
            # Suspiciously small - probably an error
            print(f"[TTS] ✗ File too small ({size} bytes) - likely an error")
            os.unlink(temp_path)
            return False

        # Skip padding for now - it was causing corruption
        # The MP3 file is ready to play as-is
        if gw.config.VERBOSE_LOGGING:
            print(f"[TTS] MP3 file ready for playback")

        if gw.config.VERBOSE_LOGGING:
            print(f"[TTS] Queueing for playback...")

        # Auto-switch RTS to Radio Controlled for TX — RTS relay must route
        # mic wiring through front panel for AIOC PTT to work.
        # No CAT commands while Radio Controlled (serial disconnected from USB).
        # Software PTT uses !ptt directly and doesn't need RTS switching.
        _ptt_method = str(getattr(gw.config, 'PTT_METHOD', 'aioc')).lower()
        if _ptt_method != 'software':
            _cat = getattr(gw, 'cat_client', None)
            if _cat and not getattr(gw, '_playback_rts_saved', None):
                gw._playback_rts_saved = _cat.get_rts()
                if gw._playback_rts_saved is None or gw._playback_rts_saved is True:
                    _cat._pause_drain()
                    try:
                        _cat.set_rts(False)  # Radio Controlled
                        import time as _time
                        _time.sleep(0.3)
                        _cat._drain(0.5)
                    finally:
                        _cat._drain_paused = False

        # Queue for playback (will go to radio TX)
        if gw.playback_source:
            if gw.config.VERBOSE_LOGGING:
                print(f"[TTS] Playback source exists, queueing file...")

            # Temporarily boost playback volume for TTS
            # Volume will be reset to 1.0 when file finishes playing
            original_volume = gw.playback_source.volume
            gw.playback_source.volume = gw.config.TTS_VOLUME
            if gw.config.VERBOSE_LOGGING:
                print(f"[TTS] Boosting volume from {original_volume}x to {gw.config.TTS_VOLUME}x for TTS playback")
                print(f"[TTS] Volume will auto-reset to 1.0x when TTS finishes")

            result = gw.playback_source.queue_file(temp_path)

            if gw.config.VERBOSE_LOGGING:
                print(f"[TTS] Queue result: {result}")
            if not result:
                print(f"[TTS] ✗ Failed to queue file")
                gw.playback_source.volume = original_volume  # Restore on failure
                return False

            # Pre-key PTT before audio starts so the relay has time to settle.
            # Only needed when nothing else is playing — if PTT is already held
            # from a previous TTS the relay is already closed and no delay is needed.
            _ps = gw.playback_source
            if not _ps.current_file and len(_ps.playlist) == 1:
                import time as _time
                _settle = float(getattr(gw.config, 'TTS_PTT_SETTLE_MS', 750)) / 1000.0
                gw._announcement_ptt_delay_until = _time.time() + _settle
                gw.announcement_delay_active = True
                if gw.config.VERBOSE_LOGGING:
                    print(f"[TTS] PTT pre-key delay: {int(_settle*1000)}ms")
        else:
            print(f"[TTS] ✗ No playback source available!")
            return False

        return True

    except Exception as e:
        print(f"\n[TTS] Error: {e}")
        return False


# ---------------------------------------------------------------------------
# Mumble text-message command dispatcher
# ---------------------------------------------------------------------------

def on_text_message(gw, text_message):
    """
    Handle incoming text messages from Mumble users

    Supports commands:
        !speak [voice#] <text>  - Generate TTS and broadcast on radio (voices 1-9)
        !play <0-9>    - Play announcement file by slot number
        !files         - List loaded announcement files
        !stop          - Stop playback and clear queue
        !mute          - Mute TX (Mumble → Radio)
        !unmute        - Unmute TX
        !id            - Play station ID (shortcut for !play 0)
        !status        - Show gateway status
        !help          - Show available commands
    """
    try:
        # Debug: Print when text is received (if verbose)
        if gw.config.VERBOSE_LOGGING:
            print(f"\n[Mumble Text] Message received from user {text_message.actor}")

        # Get sender info
        sender = gw.mumble.users[text_message.actor]
        sender_name = sender['name']
        # Mumble sends messages as HTML — strip tags and decode entities
        raw_msg = text_message.message
        message = unescape(re.sub(r'<[^>]+>', '', raw_msg)).strip()

        if gw.config.VERBOSE_LOGGING:
            print(f"[Mumble Text] {sender_name}: {message}")

        # Ignore if not a command
        if not message.startswith('!'):
            if gw.config.VERBOSE_LOGGING:
                print(f"[Mumble Text] Not a command (doesn't start with !), ignoring")
            return

        # Parse command
        parts = message.split(None, 1)  # Split on first space
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Handle commands
        if command == '!speak':
            if args:
                # Parse optional voice number: !speak 3 Hello world
                voice = None
                speak_txt = args
                speak_parts = args.split(None, 1)
                if len(speak_parts) == 2 and speak_parts[0].isdigit():
                    v = int(speak_parts[0])
                    if v in gw.TTS_VOICES:
                        voice = v
                        speak_txt = speak_parts[1]
                if speak_text(gw, speak_txt, voice=voice):
                    v_info = f" (voice {voice})" if voice else ""
                    gw.send_text_message(f"Speaking{v_info}: {speak_txt[:50]}...")
                else:
                    gw.send_text_message("TTS not available")
            else:
                voices = " | ".join(f"{k}={v[2]}" for k, v in gw.TTS_VOICES.items())
                gw.send_text_message(f"Usage: !speak [voice#] <text> — Voices: {voices}")

        elif command == '!play':
            if args and args in '0123456789':
                key = args
                if gw.playback_source:
                    path = gw.playback_source.file_status[key]['path']
                    filename = gw.playback_source.file_status[key].get('filename', '')
                    if path:
                        gw.playback_source.queue_file(path)
                        gw.send_text_message(f"Playing: {filename}")
                    else:
                        gw.send_text_message(f"No file on key {key}")
                else:
                    gw.send_text_message("Playback not available")
            else:
                gw.send_text_message("Usage: !play <0-9>")

        elif command == '!cw':
            if not args:
                gw.send_text_message("Usage: !cw &lt;text&gt;")
            else:
                pcm = generate_cw_pcm(args, gw.config.CW_WPM,
                                      gw.config.CW_FREQUENCY, 48000)
                if gw.config.CW_VOLUME != 1.0:
                    pcm = np.clip(pcm.astype(np.float32) * gw.config.CW_VOLUME,
                                  -32768, 32767).astype(np.int16)
                import wave
                tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False, prefix='cw_')
                tmp.close()
                with wave.open(tmp.name, 'wb') as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                    wf.writeframes(pcm.tobytes())
                if gw.playback_source and gw.playback_source.queue_file(tmp.name):
                    gw.send_text_message(f"CW: {args}")
                else:
                    gw.send_text_message("CW: playback unavailable")
                    os.unlink(tmp.name)

        elif command == '!status':
            import psutil

            s = []
            s.append("━━━ GATEWAY STATUS ━━━")

            # Uptime
            uptime_s = int(time.time() - gw.start_time)
            days, rem = divmod(uptime_s, 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
            s.append(f"Uptime: {uptime_str}")

            # Host — CPU, RAM, Disk
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            load1, load5, load15 = os.getloadavg()
            s.append(f"\n📊 HOST:")
            s.append(f"  CPU: {cpu:.0f}%  Load: {load1:.1f} / {load5:.1f} / {load15:.1f}")
            try:
                temps = psutil.sensors_temperatures()
                cpu_temp = temps.get('cpu_thermal', temps.get('coretemp', [{}]))[0]
                s.append(f"  Temp: {cpu_temp.current:.0f}°C")
            except Exception:
                pass
            s.append(f"  RAM: {mem.used // (1024**2)}M / {mem.total // (1024**2)}M ({mem.percent:.0f}%)")
            s.append(f"  Disk: {disk.used // (1024**3)}G / {disk.total // (1024**3)}G ({disk.percent:.0f}%)")

            # Radio
            mutes = []
            if gw.tx_muted: mutes.append("TX")
            if gw.rx_muted: mutes.append("RX")
            if gw.tx_muted and gw.rx_muted: mutes.append("ALL")
            ptt = "TX" if gw.ptt_active else "Idle"
            if gw.manual_ptt_mode: ptt += " (manual)"
            s.append(f"\n📻 RADIO:")
            s.append(f"  PTT: {ptt}  Muted: {', '.join(mutes) if mutes else 'None'}")
            if gw.sdr_rebroadcast:
                s.append(f"  Rebroadcast: ON")

            # Sources
            sources = []
            if gw.radio_source:
                sources.append(f"AIOC ({'muted' if gw.tx_muted else 'active'})")
            if gw.sdr_plugin:
                sources.append(f"SDR1 ({'muted' if gw.sdr_muted else 'active'})")
            if gw.sdr_plugin:
                sources.append(f"SDR2 ({'muted' if gw.sdr2_muted else 'active'})")
            if gw.remote_audio_source:
                sources.append(f"Remote ({'muted' if gw.remote_audio_muted else 'active'})")
            if hasattr(gw, 'announce_source') and gw.announce_source:
                ann_muted = getattr(gw, 'announce_muted', False)
                sources.append(f"Announce ({'muted' if ann_muted else 'active'})")
            if sources:
                s.append(f"  Sources: {', '.join(sources)}")

            # Mumble
            ch = gw.config.MUMBLE_CHANNEL if gw.config.MUMBLE_CHANNEL else "Root"
            users = len(gw.mumble.users) if gw.mumble else 0
            s.append(f"\n💬 MUMBLE:")
            s.append(f"  Channel: {ch}  Users: {users}")

            # Processing — compact, per-source
            proc = []
            if gw.config.ENABLE_VAD: proc.append("VAD")
            radio_active = gw.radio_processor.get_active_list()
            if radio_active:
                proc.append(f"Radio[{','.join(radio_active)}]")
            sdr_active = gw.sdr_processor.get_active_list()
            if sdr_active:
                proc.append(f"SDR[{','.join(sdr_active)}]")
            if proc:
                s.append(f"\n🎛️ Processing: {' | '.join(proc)}")

            # Network
            s.append(f"\n🌐 NETWORK:")
            for iface_name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family.name == 'AF_INET' and addr.address != '127.0.0.1':
                        s.append(f"  {iface_name}: {addr.address}")

            s.append("━━━━━━━━━━━━━━━━━━━━━━")
            gw.send_text_message("\n".join(s))

        elif command == '!files':
            if gw.playback_source:
                lines = ["=== Announcement Files ==="]
                found = False
                for key in '0123456789':
                    info = gw.playback_source.file_status[key]
                    if info['exists']:
                        label = "Station ID" if key == '0' else f"Slot {key}"
                        playing = " [PLAYING]" if info['playing'] else ""
                        lines.append(f"  {label}: {info['filename']}{playing}")
                        found = True
                if not found:
                    lines.append("  No files loaded")
                gw.send_text_message("\n".join(lines))
            else:
                gw.send_text_message("Playback not available")

        elif command == '!stop':
            if gw.playback_source:
                gw.playback_source.stop_playback()
                gw.send_text_message("Playback stopped")
            else:
                gw.send_text_message("Playback not available")

        elif command == '!restart':
            gw.send_text_message("Gateway restarting...")
            gw.restart_requested = True
            gw.running = False

        elif command == '!mute':
            gw.tx_muted = True
            gw.send_text_message("TX muted (Mumble → Radio)")

        elif command == '!unmute':
            gw.tx_muted = False
            gw.send_text_message("TX unmuted")

        elif command == '!id':
            if gw.playback_source:
                info = gw.playback_source.file_status['0']
                if info['path']:
                    gw.playback_source.queue_file(info['path'])
                    gw.send_text_message(f"Playing station ID: {info['filename']}")
                else:
                    gw.send_text_message("No station ID file on slot 0")
            else:
                gw.send_text_message("Playback not available")

        elif command == '!smart':
            if not gw.smart_announce or not gw.smart_announce._claude_bin:
                gw.send_text_message("Smart announcements not configured")
            elif args and args.isdigit():
                entry_id = int(args)
                if gw.smart_announce.trigger(entry_id):
                    gw.send_text_message(f"Triggering smart announcement #{entry_id}...")
                else:
                    gw.send_text_message(f"No smart announcement #{entry_id}")
            else:
                entries = gw.smart_announce.get_entries()
                if entries:
                    lines = [f"#{e['id']}: every {e['interval']}s, voice {e['voice']}, "
                             f"~{e['target_secs']}s — {e['prompt'][:50]}" for e in entries]
                    gw.send_text_message("Smart announcements:\n" + "\n".join(lines)
                                           + "\n\nUsage: !smart <N> to trigger")
                else:
                    gw.send_text_message("No smart announcements configured")

        elif command == '!endpoints' or command == '!ep':
            eps = getattr(gw, 'link_endpoints', {})
            if not eps:
                gw.send_text_message("No link endpoints connected")
            else:
                lines = ["=== Link Endpoints ==="]
                for name, src in eps.items():
                    sid = getattr(src, 'source_id', '?')
                    plugin = getattr(src, 'plugin_type', '?')
                    status = gw._link_last_status.get(name, {})
                    cpu = status.get('cpu_pct', '?')
                    ram = status.get('ram_pct', '?')
                    temp = status.get('cpu_temp_c', '?')
                    ver = status.get('code_version', '?')[:8]
                    uptime = status.get('uptime', 0)
                    h, m = int(uptime) // 3600, (int(uptime) % 3600) // 60
                    lines.append(f"  {name} [{plugin}] sid={sid} "
                                 f"CPU:{cpu}% RAM:{ram}% {temp}C "
                                 f"Up:{h}h{m:02d}m v={ver}")
                gw.send_text_message("\n".join(lines))

        elif command == '!loop':
            if hasattr(gw, 'loop_recorder') and gw.loop_recorder:
                lr = gw.loop_recorder
                buses = lr.get_active_buses() if hasattr(lr, 'get_active_buses') else []
                total = sum(lr.get_segment_count(b) for b in buses) if hasattr(lr, 'get_segment_count') else 0
                gw.send_text_message(f"Loop recorder: {len(buses)} buses, {total} segments")
            else:
                gw.send_text_message("Loop recorder not active")

        elif command == '!help':
            help_text = [
                "=== Gateway Commands ===",
                "!speak [voice#] <text> - TTS broadcast (voices 1-9)",
                "!smart [N]    - List or trigger smart announcement",
                "!cw <text>    - Send Morse code on radio",
                "!play <0-9>   - Play announcement by slot",
                "!files        - List loaded announcement files",
                "!stop         - Stop playback and clear queue",
                "!mute         - Mute TX (Mumble → Radio)",
                "!unmute       - Unmute TX",
                "!id           - Play station ID (slot 0)",
                "!endpoints    - Show connected link endpoints",
                "!loop         - Loop recorder status",
                "!restart      - Restart the gateway",
                "!status       - Show gateway status",
                "!help         - Show this help"
            ]
            gw.send_text_message("\n".join(help_text))

        else:
            gw.send_text_message(f"Unknown command. Try !help")

    except Exception as e:
        if gw.config.VERBOSE_LOGGING:
            print(f"\n[Text Command] Error: {e}")


# ---------------------------------------------------------------------------
# Keyboard / web-key command dispatcher
# ---------------------------------------------------------------------------

def handle_key(gw, char):
    """Process a key command (called by keyboard loop and web UI)."""
    char = char.lower()

    if char == 't':
        gw.tx_muted = not gw.tx_muted
        gw._trace_events.append((time.monotonic(), 'tx_mute', 'on' if gw.tx_muted else 'off'))
    elif char == 'r':
        gw.rx_muted = not gw.rx_muted
        gw._trace_events.append((time.monotonic(), 'rx_mute', 'on' if gw.rx_muted else 'off'))
    elif char == 'm':
        if gw.tx_muted and gw.rx_muted:
            gw.tx_muted = False
            gw.rx_muted = False
        else:
            gw.tx_muted = True
            gw.rx_muted = True
        gw._trace_events.append((time.monotonic(), 'global_mute', f'tx={gw.tx_muted} rx={gw.rx_muted}'))
    elif char == 's':
        if gw.sdr_plugin:
            gw.sdr_muted = not gw.sdr_muted
            gw.sdr_plugin.tuner1_muted = gw.sdr_muted
            gw._trace_events.append((time.monotonic(), 'sdr_mute', 'on' if gw.sdr_muted else 'off'))
    elif char == 'd':
        if gw.sdr_plugin:
            gw.sdr_plugin.duck = not gw.sdr_plugin.duck
    elif char == 'x':
        if gw.sdr_plugin:
            gw.sdr2_muted = not gw.sdr2_muted
            gw.sdr_plugin.tuner2_muted = gw.sdr2_muted
            gw._trace_events.append((time.monotonic(), 'sdr2_mute', 'on' if gw.sdr2_muted else 'off'))
    elif char == 'c':
        if gw.remote_audio_source:
            gw.remote_audio_muted = not gw.remote_audio_muted
            gw.remote_audio_source.muted = gw.remote_audio_muted
            gw._trace_events.append((time.monotonic(), 'remote_mute', 'on' if gw.remote_audio_muted else 'off'))
    elif char == 'k':
        if gw.remote_audio_server:
            gw.remote_audio_server.reset()
            gw._trace_events.append((time.monotonic(), 'remote_reset', 'server'))
        elif gw.remote_audio_source:
            gw.remote_audio_source.reset()
            gw._trace_events.append((time.monotonic(), 'remote_reset', 'client'))
    elif char == 'v':
        gw.config.ENABLE_VAD = not gw.config.ENABLE_VAD
    elif char == ',':
        gw.config.INPUT_VOLUME = max(0.1, gw.config.INPUT_VOLUME - 0.1)
    elif char == '.':
        gw.config.INPUT_VOLUME = min(3.0, gw.config.INPUT_VOLUME + 0.1)
    elif char == 'n':
        gw.config.ENABLE_NOISE_GATE = not gw.config.ENABLE_NOISE_GATE
        gw._sync_radio_processor()
    elif char == 'f':
        gw.config.ENABLE_HIGHPASS_FILTER = not gw.config.ENABLE_HIGHPASS_FILTER
        gw._sync_radio_processor()
    elif char == 'a':
        if gw.announce_input_source:
            gw.announce_input_muted = not gw.announce_input_muted
            gw.announce_input_source.muted = gw.announce_input_muted
    elif char == 'g':
        gw.config.ENABLE_AGC = not gw.config.ENABLE_AGC
    elif char == 'e':
        gw.config.ENABLE_ECHO_CANCELLATION = not gw.config.ENABLE_ECHO_CANCELLATION
    elif char == 'p':
        if gw.aioc_device or str(getattr(gw.config, 'PTT_METHOD', 'aioc')).lower() != 'aioc':
            gw.manual_ptt_mode = not gw.manual_ptt_mode
            gw._pending_ptt_state = gw.manual_ptt_mode
            gw._trace_events.append((time.monotonic(), 'ptt', 'on' if gw.manual_ptt_mode else 'off'))
    elif char == 'b':
        gw.sdr_rebroadcast = not gw.sdr_rebroadcast
        if not gw.sdr_rebroadcast:
            if gw._rebroadcast_ptt_active and gw.ptt_active:
                gw.set_ptt_state(False)
                gw._ptt_change_time = time.monotonic()
                gw._rebroadcast_ptt_active = False
            if gw.radio_source:
                gw.radio_source.enabled = True
            gw._rebroadcast_sending = False
            gw._rebroadcast_ptt_hold_until = 0
        gw._trace_events.append((time.monotonic(), 'sdr_rebroadcast', 'on' if gw.sdr_rebroadcast else 'off'))
    elif char == 'j':
        if gw.relay_radio and not gw._relay_radio_pressing:
            def _pulse_power():
                gw._relay_radio_pressing = True
                gw.relay_radio.set_state(True)
                gw._trace_events.append((time.monotonic(), 'relay_radio', 'press'))
                time.sleep(1.0)
                gw.relay_radio.set_state(False)
                gw._relay_radio_pressing = False
                gw._trace_events.append((time.monotonic(), 'relay_radio', 'release'))
            threading.Thread(target=_pulse_power, daemon=True).start()
    elif char == 'h':
        if gw.relay_charger:
            new_state = not gw.relay_charger_on
            gw.relay_charger.set_state(new_state)
            gw.relay_charger_on = new_state
            gw._charger_manual = True
            gw._trace_events.append((time.monotonic(), 'relay_charger', f'manual_{"on" if new_state else "off"}'))
    elif char == 'o':
        if gw.speaker_stream:
            gw.speaker_muted = not gw.speaker_muted
            gw._trace_events.append((time.monotonic(), 'spk_mute', 'on' if gw.speaker_muted else 'off'))
    elif char == 'y':
        if gw.kv4p_plugin:
            gw.kv4p_muted = not gw.kv4p_muted
            gw.kv4p_plugin.muted = gw.kv4p_muted
            gw._trace_events.append((time.monotonic(), 'kv4p_mute', 'on' if gw.kv4p_muted else 'off'))
    elif char == 'l':
        if gw.cat_client:
            def _send_cat_config():
                try:
                    gw.cat_client._stop = False
                    gw.cat_client.setup_radio(gw.config)
                except Exception:
                    pass
            threading.Thread(target=_send_cat_config, daemon=True, name="CAT-ManualConfig").start()
    elif char in '0123456789':
        if gw.playback_source:
            stored_path = gw.playback_source.file_status[char]['path']
            if stored_path:
                # Auto-set RTS to Radio Controlled for TX playback — RTS relay
                # must route mic wiring through front panel for AIOC PTT.
                # No CAT commands while Radio Controlled (serial disconnected).
                # Software PTT and D75 TX don't need RTS switching.
                _ptt_method = str(getattr(gw.config, 'PTT_METHOD', 'aioc')).lower()
                _tx_radio = str(getattr(gw.config, 'TX_RADIO', 'th9800')).lower()
                if _ptt_method != 'software' and _tx_radio != 'd75':
                    _cat = gw.cat_client
                    if _cat and not getattr(gw, '_playback_rts_saved', None):
                        gw._playback_rts_saved = _cat.get_rts()
                        if gw._playback_rts_saved is None or gw._playback_rts_saved is True:
                            try:
                                _cat._pause_drain()
                                try:
                                    _cat.set_rts(False)  # Radio Controlled
                                    time.sleep(0.3)
                                    _cat._drain(0.5)
                                finally:
                                    _cat._drain_paused = False
                                print(f"\n[Playback] RTS → Radio Controlled")
                            except Exception:
                                pass
                # Stop current playback immediately, then decode+queue
                # in a background thread so the HTTP handler returns fast.
                # The lock serializes concurrent decodes; the sequence
                # counter lets later presses discard earlier in-flight decodes.
                pb = gw.playback_source
                pb._play_seq += 1
                my_seq = pb._play_seq
                pb.stop_playback()
                def _bg_play(_pb=pb, _path=stored_path, _seq=my_seq, _gw=gw):
                    try:
                        with _pb._play_lock:
                            if _pb._play_seq != _seq:
                                return  # A newer button press superseded this one
                            if not _pb.queue_file(_path):
                                _gw.notify(f"Playback failed: {os.path.basename(_path)}")
                    except Exception as e:
                        print(f"\n[Playback] Error in background decode: {e}")
                        _gw.notify(f"Playback error: {e}")
                threading.Thread(target=_bg_play, daemon=True, name="Playback-Queue").start()
    elif char == '-':
        if gw.playback_source:
            gw.playback_source.stop_playback()
    elif char in ('[', ']', '\\'):
        slot = {'[': 1, ']': 2, '\\': 3}[char]
        if gw.smart_announce and gw.smart_announce._claude_bin:
            gw.smart_announce.trigger(slot)
    elif char == '@':
        if gw.email_notifier:
            print(f"\n[Email] Sending status email...")
            threading.Thread(target=gw.email_notifier.send_startup_status,
                             daemon=True, name="email-manual").start()
        else:
            print(f"\n[Email] Not configured")
    elif char == 'q':
        print(f"\n[WebUI] Restarting gateway...")
        gw.restart_requested = True
        gw.running = False
