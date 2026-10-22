"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

from log_util import log_exc
import os
import sys
import threading
import time



class _LifecycleMixin:
    def notify(self, message, level='error'):
        """Push a notification to the web UI. level: 'error', 'warning', 'info'."""
        self._notif_seq += 1
        self._notifications.append({
            'seq': self._notif_seq,
            'msg': message,
            'level': level,
            'ts': time.time(),
        })

    def handle_proc_toggle(self, source, filt, state=None):
        """Toggle or set a processing filter for a specific source.
        Called from the /proc_toggle and /mixer API endpoints.
        If state is None, toggles; if True/False, sets explicitly.
        """
        # Map source to config keys and sync method
        _source_map = {
            'radio': ({
                'gate':  'ENABLE_NOISE_GATE',
                'hpf':   'ENABLE_HIGHPASS_FILTER',
                'lpf':   'ENABLE_LOWPASS_FILTER',
                'notch': 'ENABLE_NOTCH_FILTER',
            }, '_sync_radio_processor'),
            'sdr': ({
                'gate':  'SDR_PROC_ENABLE_NOISE_GATE',
                'hpf':   'SDR_PROC_ENABLE_HPF',
                'lpf':   'SDR_PROC_ENABLE_LPF',
                'notch': 'SDR_PROC_ENABLE_NOTCH',
            }, '_sync_sdr_plugin_processors'),
            'd75': ({
                'gate':  'D75_PROC_ENABLE_NOISE_GATE',
                'hpf':   'D75_PROC_ENABLE_HPF',
                'lpf':   'D75_PROC_ENABLE_LPF',
                'notch': 'D75_PROC_ENABLE_NOTCH',
            }, None),  # link endpoint manages its own processing
            'kv4p': ({
                'gate':  'KV4P_PROC_ENABLE_NOISE_GATE',
                'hpf':   'KV4P_PROC_ENABLE_HPF',
                'lpf':   'KV4P_PROC_ENABLE_LPF',
                'notch': 'KV4P_PROC_ENABLE_NOTCH',
            }, None),  # link endpoint manages its own processing (kv4p is endpoint-hosted)
        }
        entry = _source_map.get(source)
        if not entry:
            return
        toggle_map, sync_method = entry
        key = toggle_map.get(filt)
        if key:
            if state is None:
                current = getattr(self.config, key, False)
                setattr(self.config, key, not current)
            else:
                setattr(self.config, key, bool(state))
            if sync_method:
                getattr(self, sync_method)()

    def handle_key(self, char):
        from text_commands import handle_key as _handle_key
        _handle_key(self, char)

    def run(self):
        """Main application"""
        # Set up rolling log file (daily rotation, keeps LOG_FILE_DAYS days)
        log_file = None
        try:
            # core/lifecycle.py lives one level below the repo root — go up
            # one more dir than dirname(__file__) or this writes into core/logs/.
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(repo_root, 'logs')
            os.makedirs(log_dir, exist_ok=True)
            # Open today's log file (append mode)
            import datetime as _dt
            today = _dt.date.today().strftime('%Y-%m-%d')
            log_path = os.path.join(log_dir, f'gateway-{today}.log')
            log_file = open(log_path, 'a', encoding='utf-8')
            # Clean up old log files beyond retention
            keep_days = int(getattr(self.config, 'LOG_FILE_DAYS', 30))
            import glob
            for old_log in sorted(glob.glob(os.path.join(log_dir, 'gateway-*.log'))):
                try:
                    fname = os.path.basename(old_log)
                    date_str = fname.replace('gateway-', '').replace('.log', '')
                    log_date = _dt.datetime.strptime(date_str, '%Y-%m-%d').date()
                    if (_dt.date.today() - log_date).days > keep_days:
                        os.remove(old_log)
                except (ValueError, OSError):
                    pass
            self._log_dir = log_dir
        except Exception as e:
            print(f"  [Warning] Could not set up log file: {e}", file=sys.stderr)

        # Clean up stale /tmp log files from previous runs
        for tmp_log in ['/tmp/th9800_cat.log', '/tmp/ffmpeg.log']:
            try:
                if os.path.exists(tmp_log):
                    sz = os.path.getsize(tmp_log)
                    if sz > 10 * 1024 * 1024:  # >10MB, truncate
                        open(tmp_log, 'w').close()
            except Exception:
                log_exc()

        # Install stdout/stderr wrapper early so ALL messages get timestamps.
        # Deferred import: LogWriter and __version__ live in gateway_core
        # which imports this module — top-level import here would loop.
        from gateway_core import LogWriter, __version__
        buf_lines = int(getattr(self.config, 'LOG_BUFFER_LINES', 2000))
        self._status_writer = LogWriter(
            sys.stdout, buffer_lines=buf_lines, log_file=log_file,
            log_dir=getattr(self, '_log_dir', None),
            keep_days=int(getattr(self.config, 'LOG_FILE_DAYS', 30)),
        )
        sys.stdout = self._status_writer
        self._orig_stderr = sys.stderr
        sys.stderr = self._status_writer

        # Pre-populate log buffer with prior startup output so web /logs shows full boot sequence
        try:
            startup_log = '/tmp/gateway_startup.log'
            if os.path.exists(startup_log):
                with open(startup_log, 'r') as f:
                    for line in f:
                        line = line.rstrip('\n')
                        if line:
                            self._status_writer._append_log(line)
        except Exception:
            log_exc()

        print("=" * 60)
        print("Radio Gateway")
        print(f"Version {__version__}")
        print("=" * 60)
        print()
        
        # AIOC init now handled by TH9800Plugin in setup_audio()

        # Initialize Audio
        if not self.setup_audio():
            self.cleanup()
            return False
        
        # Initialize Mumble
        self._mumble_ok = self.setup_mumble()
        mumble_ok = self._mumble_ok
        if not mumble_ok:
            print("\n  ⚠ Mumble connection failed — continuing without Mumble.")
            print("  Radio audio, SDR, and other features will still work.")

        # Redirect OS-level fd 2 (C stderr) through a pipe that feeds back
        # into the LogWriter.  This catches output from external
        # processes (murmurd, Mumble GUI Qt warnings) that share our terminal.
        try:
            self._stderr_pipe_r, self._stderr_pipe_w = os.pipe()
            os.dup2(self._stderr_pipe_w, 2)
            os.close(self._stderr_pipe_w)
            def _stderr_reader():
                buf = b''
                while self.running:
                    try:
                        data = os.read(self._stderr_pipe_r, 4096)
                        if not data:
                            break
                        buf += data
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            text = line.decode('utf-8', errors='replace').rstrip()
                            if text:
                                self._status_writer.write(text + '\n')
                                self._status_writer.flush()
                    except OSError:
                        break
            self._stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
            self._stderr_thread.start()
        except Exception:
            log_exc()  # Non-fatal — stderr just won't be captured

        # Start audio transmit thread
        self._tx_thread = threading.Thread(target=self.audio_transmit_loop, daemon=True)
        self._tx_thread.start()

        # Start status monitor thread (handles PTT timeout and status reporting)
        self._status_thread = threading.Thread(target=self.status_monitor_loop, daemon=True)
        self._status_thread.start()

        # ── Long-lived subsystems are built BEFORE BusManager.start() ──
        # Building them here puts their object graphs in place early enough to
        # be swept into the permanent generation by BusManager's startup freeze,
        # so the frequent collections stop walking them. Measured 2026-07-27
        # over 2h23m, moving these above start():
        #     gen-0 avg 0.336 ms -> 0.212 ms
        #     gen-1 avg 3.080 ms -> 1.016 ms   (3x)
        # This does NOT fix the gen-2 overrun, and it never could: gen-2 is
        # dominated by the transcriber's Silero VAD + Moonshine ONNX sessions,
        # which RadioTranscriber.start() loads on its OWN thread ~2 s after
        # returning. The allocation is late, not the construction — that is
        # handled by deferring the freeze itself (bus_manager _FREEZE_AT_TICK).
        # Keep this order for the gen-0/gen-1 win; don't expect more from it.
        # A runtime transcriber swap must unfreeze first — see the 'restart'
        # handler in web_routes_transcribe.py.

        # Initialize Loop Recorder (per-bus continuous recording)
        try:
            from loop_recorder import LoopRecorder
            self.loop_recorder = LoopRecorder(
                kbps=int(getattr(self.config, 'LOOP_RECORD_KBPS', 48) or 48))
            print("✓ Loop Recorder initialized")
        except Exception as e:
            print(f"  [LoopRec] Failed to initialize: {e}")
            self.loop_recorder = None

        # Start Automation Engine if enabled
        if getattr(self.config, 'ENABLE_AUTOMATION', False):
            try:
                from radio_automation import AutomationEngine
                self.automation_engine = AutomationEngine(self)
                self.automation_engine.start()
            except Exception as e:
                print(f"[Automation] Failed to start: {e}")
                self.automation_engine = None

        # Transcription log (persistent SQLite store)
        if getattr(self.config, 'ENABLE_TRANSCRIPTION_LOG', True):
            try:
                from transcription_log import TranscriptionLog
                self.transcription_log = TranscriptionLog(self.config)
                print('  [TxLog] Transcription log ready')
            except Exception as e:
                print(f'  [TxLog] Failed to open log: {e}')

        # Start Transcriber if enabled
        if getattr(self.config, 'ENABLE_TRANSCRIPTION', False):
            try:
                from transcriber import RadioTranscriber
                self.transcriber = RadioTranscriber(self.config, self)
                self.transcriber.start()
            except Exception as e:
                print(f"[Transcribe] Failed to start: {e}")
                self.transcriber = None

        # Start Bus Manager (additional busses from routing config)
        try:
            from bus_manager import BusManager
            self.bus_manager = BusManager(self)
            self.bus_manager.start()
            self.mixer = self.bus_manager.listen_bus  # Backward compat for trace access
            # Cache bus metadata for web UI (refreshed after routing saves)
            self._bus_stream_flags = self.bus_manager.get_bus_stream_flags()
            self._bus_sinks = self.bus_manager.get_bus_sinks()
            self._listen_bus_id = self.bus_manager.get_listen_bus_id()
            self._listen_bus_muted = self.bus_manager.is_bus_muted(self._listen_bus_id)
            # Restore persisted source/sink gains
            self._load_source_gains()
            self._apply_source_gains()
        except Exception as e:
            print(f"  [BusManager] Failed to start: {e}")
            self.bus_manager = None

        # Load external plugins from plugins/ directory
        self._external_plugins = {}
        try:
            from plugin_loader import discover_plugins
            self._external_plugins = discover_plugins(self.config, self)
            if self._external_plugins:
                print(f"✓ Loaded {len(self._external_plugins)} external plugin(s)")
                # Buses are built BEFORE plugin discovery, so a solo/duplex bus
                # whose radio is an external plugin (e.g. AllStar usrp_tx sink)
                # resolved to None at creation and never got its TX radio →
                # put_audio was never called. Rebuild now that _external_plugins
                # is populated — same mechanism endpoint registration uses
                # (gateway_setup → bus_manager.reload()). reload() re-syncs the
                # listen bus too, so it supersedes sync_listen_bus() here.
                if self.bus_manager:
                    self.bus_manager.reload()
        except Exception as e:
            print(f"  [Plugins] Discovery failed: {e}")

        # Main loop
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
        finally:
            self.cleanup()
    
    def _watchdog_trace_loop(self):
        from audio_trace import watchdog_trace_loop
        watchdog_trace_loop(self)
    def _dump_audio_trace(self):
        from audio_trace import dump_audio_trace
        return dump_audio_trace(self)
    def _shutdown_step(self, what, fn, done=None, quiet=False):
        """Run one shutdown step. A failure is reported (or, with quiet,
        swallowed: ALSA and PyAudio grumble on every close) and the
        remaining steps still run."""
        try:
            fn()
            if done and self.config.VERBOSE_LOGGING:
                print(f"  {done}")
        except Exception as e:
            if not quiet:
                print(f"  [Warning] shutdown: {what} failed: {type(e).__name__}: {e}")

    def _close_stream(self, stream, settle):
        # Stop before close (prevents ALSA mmap errors), then let ALSA settle.
        if stream.is_active():
            stream.stop_stream()
        if settle:
            time.sleep(0.05)
        stream.close()

    def _restore_stdio(self):
        """Undo run()'s stdout/stderr capture so the shutdown prints land."""
        if hasattr(self, '_orig_stderr') and self._orig_stderr:
            sys.stderr = self._orig_stderr
            # Restore fd 2 if we piped it
            try:
                os.dup2(self._orig_stderr.fileno(), 2)
            except Exception:
                log_exc()
            # Close the pipe read end to unblock the reader thread
            if hasattr(self, '_stderr_pipe_r'):
                try:
                    os.close(self._stderr_pipe_r)
                except Exception:
                    pass
        if self._status_writer:
            sys.stdout = self._status_writer._orig
            self._status_writer = None

    def cleanup(self):
        """Clean up resources.

        The ORDER is load-bearing (tests/test_lifecycle_ordering.py): the
        supervisor is reaped first, the stream closes before ALSA, Mumble
        stops before the audio streams it calls back into. Every step runs
        through _shutdown_step, so one that raises can no longer skip the
        rest: an exception from the loop recorder used to leave PTT keyed.
        """
        # Each step is a lambda so that even the attribute lookup happens
        # inside _shutdown_step's try, as it did inside the old try blocks.
        step = self._shutdown_step

        # Reap supervised child processes first (respects per-entry
        # persist_across_restart so cloudflared stays running)
        if getattr(self, 'process_supervisor', None):
            step('process supervisor', lambda: self.process_supervisor.shutdown_all(timeout=5.0))

        # Restore terminal settings (keyboard thread is daemon and may not
        # reach its own finally block before the process exits)
        if hasattr(self, '_terminal_settings'):
            def _restore_terminal():
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._terminal_settings)
            step('terminal settings', _restore_terminal)

        # Restore original stdout/stderr before cleanup prints
        self._restore_stdio()

        # Stop watchdog trace and flush remaining samples
        if self._watchdog_active:
            self._watchdog_active = False

        # Dump audio trace before anything else
        step('audio trace', lambda: self._dump_audio_trace())

        if self.config.VERBOSE_LOGGING:
            print("\nCleaning up...")

        if getattr(self, 'loop_recorder', None):
            step('loop recorder', lambda: self.loop_recorder.stop())

        for pid, plugin in getattr(self, '_external_plugins', {}).items():
            step(f'plugin {pid}', lambda p=plugin: p.cleanup(), quiet=True)

        # Signal threads to stop, and give them time to finish current operations
        self.running = False
        time.sleep(0.2)

        # Close stream output pipe first (before stopping other things)
        if getattr(self, 'stream_output', None):
            step('stream output', lambda: self.stream_output.cleanup(), done='Stream output closed')

        if self.ptt_active:
            step('PTT release', lambda: self.set_ptt_state(False))

        # Close Mumble connection first (stops audio callbacks), and let it
        # fully stop
        if self.mumble:
            step('Mumble', lambda: self.mumble.stop(), quiet=True)
        time.sleep(0.1)

        # Now the audio sources and servers
        for obj, what, done in (
            (self.sdr_plugin, 'SDR', 'SDR audio closed'),
            (self.remote_audio_source, 'remote audio source', 'Remote audio source closed'),
            (self.remote_audio_server, 'remote audio server', 'Remote audio server closed'),
            (self.announce_input_source, 'announcement input', 'Announcement input closed'),
        ):
            if obj:
                step(what, lambda o=obj: o.cleanup(), done=done)

        # Close relay serial ports (leave relays in current state — don't
        # power-cycle on restart)
        if self.relay_radio:
            step('radio relay', lambda: self.relay_radio.close(), done='Radio relay port closed')
        if self.relay_charger:
            step('charger relay', lambda: self.relay_charger.close(), done='Charger relay port closed')

        # Background services, in this order
        for obj, what, method in (
            (self.automation_engine, 'automation', 'stop'),
            (self.transcriber, 'transcriber', 'stop'),
            (self.transcription_log, 'transcription log', 'close'),
            (self.smart_announce, 'smart announce', 'stop'),
            (self.manager_engine, 'manager', 'stop'),
            (self.alert_engine, 'alerts', 'stop'),
            (self.web_config_server, 'web server', 'stop'),
            (self.gps_manager, 'GPS', 'stop'),
            (self.repeater_manager, 'repeater manager', 'stop'),
            (self.ddns_updater, 'DDNS', 'stop'),
            (self.cloudflare_tunnel, 'Cloudflare tunnel', 'stop'),
        ):
            if obj:
                step(what, lambda o=obj, m=method: getattr(o, m)(), quiet=True)

        if self.relay_ptt:
            def _ptt_relay_off():
                self.relay_ptt.set_state(False)
                self.relay_ptt.close()
            step('PTT relay', _ptt_relay_off)

        if self.cat_client:
            step('CAT client', lambda: self.cat_client.close(), done='CAT client closed')

        # Stop local Mumble Server instances on gateway exit
        if self.mumble_server_1:
            step('Mumble server 1', lambda: self.mumble_server_1.stop(), done='Mumble Server 1 stopped')
        if self.mumble_server_2:
            step('Mumble server 2', lambda: self.mumble_server_2.stop(), done='Mumble Server 2 stopped')

        if self.input_stream:
            step('input stream', lambda: self._close_stream(self.input_stream, True), quiet=True)
        if self.speaker_stream:
            step('speaker stream', lambda: self._close_stream(self.speaker_stream, False), quiet=True)
        if self.output_stream:
            step('output stream', lambda: self._close_stream(self.output_stream, True), quiet=True)
        if self.pyaudio_instance:
            step('PyAudio', lambda: self.pyaudio_instance.terminate(), quiet=True)
        if self.aioc_device:
            step('AIOC device', lambda: self.aioc_device.close(), quiet=True)

        print("Shutdown complete")

    def _on_tunnel_url_changed(self, new_url):
        """Called by CloudflareTunnel when the tunnel is relaunched with a new URL."""
        print(f"  [Gateway] Tunnel URL changed: {new_url}")
        if self.email_notifier:
            try:
                self.email_notifier.send_tunnel_changed(new_url)
            except Exception as e:
                print(f"  [Gateway] Failed to send tunnel change email: {e}")
        # Publish new URL to Google Drive
        self._publish_tunnel_url()

