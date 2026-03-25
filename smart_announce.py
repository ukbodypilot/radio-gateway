#!/usr/bin/env python3
"""AI-powered smart announcement manager for radio-gateway."""

import os
import time
import threading
import subprocess
import shutil


class SmartAnnouncementManager:
    """AI-powered announcements via claude CLI.

    Reads SMART_ANNOUNCE_N entries from config. Each entry has:
        interval (seconds), voice (1-9), target_seconds (max speech length), {prompt}

    Claude CLI composes a spoken message based on the prompt (web search included).
    The result is fed to the existing gTTS pipeline for broadcast.
    """

    # ~2.5 words/second for gTTS speech
    WORDS_PER_SECOND = 2.5

    def __init__(self, gateway):
        self.gateway = gateway
        self.config = gateway.config
        self._entries = []  # list of dicts: {id, interval, voice, target_secs, prompt, last_run}
        self._thread = None
        self._stop = False
        self._lock = threading.Lock()  # protects _entries mutations
        self._claude_bin = None
        self._activity = {}  # {entry_id: {'step': str, 'time': float}} — live status for web UI
        self._parse_entries()

    def _parse_entries(self):
        """Find all SMART_ANNOUNCE_N entries in config.
        Supports two formats:
        - New: individual keys (SMART_ANNOUNCE_N_PROMPT, _INTERVAL, _VOICE, _TARGET_SECS)
        - Old: packed string (SMART_ANNOUNCE_N = interval, voice, target_secs, {prompt})
        """
        for i in range(1, 20):
            try:
                # Try new individual-key format first
                prompt_key = f'SMART_ANNOUNCE_{i}_PROMPT'
                prompt = str(getattr(self.config, prompt_key, '') or '').strip()
                if prompt:
                    interval = int(getattr(self.config, f'SMART_ANNOUNCE_{i}_INTERVAL', 3600))
                    voice = int(getattr(self.config, f'SMART_ANNOUNCE_{i}_VOICE', 1))
                    target_secs = min(int(getattr(self.config, f'SMART_ANNOUNCE_{i}_TARGET_SECS', 15)), 60)
                    mode = str(getattr(self.config, f'SMART_ANNOUNCE_{i}_MODE', 'auto') or 'auto').strip().lower()
                    if mode not in ('auto', 'manual'):
                        mode = 'auto'
                    self._entries.append({
                        'id': i,
                        'interval': interval,
                        'voice': voice,
                        'target_secs': target_secs,
                        'mode': mode,
                        'prompt': prompt,
                        'last_run': 0,
                    })
                    continue
                # Fallback: old packed format
                key = f'SMART_ANNOUNCE_{i}'
                raw = getattr(self.config, key, None)
                if raw is None:
                    continue
                entry = self._parse_entry(i, str(raw))
                if entry:
                    self._entries.append(entry)
            except Exception as e:
                print(f"  [SmartAnnounce] Error parsing entry {i}: {e}")

    def _parse_entry(self, entry_id, raw):
        """Parse: interval, voice, target_seconds, {prompt text here}"""
        brace_start = raw.find('{')
        brace_end = raw.rfind('}')
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            print(f"  [SmartAnnounce] Entry {entry_id}: missing {{prompt}} in braces")
            return None
        prompt = raw[brace_start + 1:brace_end].strip()
        prefix = raw[:brace_start]
        parts = [p.strip() for p in prefix.split(',') if p.strip()]
        if len(parts) < 3:
            print(f"  [SmartAnnounce] Entry {entry_id}: need interval, voice, target_seconds before {{prompt}}")
            return None
        return {
            'id': entry_id,
            'interval': int(parts[0]),
            'voice': int(parts[1]),
            'target_secs': min(int(parts[2]), 60),
            'mode': 'auto',
            'prompt': prompt,
            'last_run': 0,
        }

    def _init_claude_cli(self):
        """Find the claude CLI binary."""
        candidate = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
        if os.path.isfile(candidate):
            self._claude_bin = candidate
            print(f"  [SmartAnnounce] claude CLI: {self._claude_bin}")
            return True
        print("  [SmartAnnounce] claude CLI not found — install Claude Code or check PATH")
        return False

    def start(self):
        """Start the background timer thread."""
        if not self._entries:
            return
        try:
            if not self._init_claude_cli():
                return
            _sa_start = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
            _sa_end = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
            if _sa_start and _sa_end:
                print(f"  [SmartAnnounce] Time window: {_sa_start}-{_sa_end}")
            else:
                print(f"  [SmartAnnounce] Time window: unrestricted")
            print(f"  [SmartAnnounce] Initialized with {len(self._entries)} scheduled announcement(s)")
            for e in self._entries:
                mode_str = 'manual' if e['mode'] == 'manual' else f"every {e['interval']}s"
                print(f"    #{e['id']}: {mode_str}, voice {e['voice']}, "
                      f"~{e['target_secs']}s, prompt: {e['prompt'][:60]}...")
        except Exception as e:
            print(f"  [SmartAnnounce] Init error: {e}")
            return

        self._stop = False
        self._thread = threading.Thread(target=self._timer_loop, daemon=True,
                                        name="SmartAnnounce")
        self._thread.start()

    def get_countdowns(self):
        """Return list of (id, seconds_remaining, mode) for each entry."""
        now = time.time()
        result = []
        with self._lock:
            for e in self._entries:
                remaining = max(0, e['interval'] - (now - e['last_run']))
                result.append((e['id'], int(remaining), e['mode']))
        return result

    def stop(self):
        """Stop the timer thread."""
        self._stop = True

    def _in_time_window(self):
        """Check if current time is within the configured start/end window.
        Returns True if no window configured or current time is inside it."""
        import datetime
        start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
        end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
        if not start_str or not end_str:
            return True
        try:
            now = datetime.datetime.now().time()
            sh, sm = map(int, start_str.split(':'))
            eh, em = map(int, end_str.split(':'))
            start = datetime.time(sh, sm)
            end = datetime.time(eh, em)
            if start <= end:
                return start <= now <= end
            else:
                # Overnight wrap (e.g. 22:00 - 06:00)
                return now >= start or now <= end
        except (ValueError, AttributeError):
            return True  # invalid format → don't restrict

    def _timer_loop(self):
        """Check intervals and trigger announcements."""
        # Stagger first runs: don't all fire at t=0
        now = time.time()
        with self._lock:
            for e in self._entries:
                e['last_run'] = now
        _was_in_window = self._in_time_window()
        if _was_in_window:
            start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
            end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
            if start_str and end_str:
                print(f"[SmartAnnounce] Active — time window {start_str}-{end_str}")
        while not self._stop:
            time.sleep(5)
            _in_window = self._in_time_window()
            if _in_window != _was_in_window:
                start_str = str(getattr(self.config, 'SMART_ANNOUNCE_START_TIME', '') or '')
                end_str = str(getattr(self.config, 'SMART_ANNOUNCE_END_TIME', '') or '')
                if _in_window:
                    print(f"\n[SmartAnnounce] Active — time window {start_str}-{end_str} started")
                else:
                    print(f"\n[SmartAnnounce] Paused — time window {start_str}-{end_str} ended")
                _was_in_window = _in_window
            if not _in_window:
                continue
            now = time.time()
            with self._lock:
                due = [(e, now) for e in self._entries if e['mode'] == 'auto' and now - e['last_run'] >= e['interval']]
                for e, t in due:
                    e['last_run'] = t
            for e, _ in due:
                try:
                    self._run_announcement(e)
                except Exception as ex:
                    print(f"\n[SmartAnnounce] Error on #{e['id']}: {ex}")

    def _set_activity(self, entry_id, step):
        """Update live activity status for web UI."""
        self._activity[entry_id] = {'step': step, 'time': time.time()}

    def _clear_activity(self, entry_id):
        """Clear activity status after completion."""
        self._activity.pop(entry_id, None)

    def get_activity(self):
        """Return current activity dict for all entries. Auto-expires after 120s."""
        now = time.time()
        expired = [k for k, v in self._activity.items() if now - v['time'] > 120]
        for k in expired:
            del self._activity[k]
        return {k: v['step'] for k, v in self._activity.items()}

    def _call_claude_cli(self, entry):
        """Run claude -p with the entry prompt, return announcement text or None."""
        eid = entry['id']
        max_words = int(entry['target_secs'] * self.WORDS_PER_SECOND)
        prompt = (
            f"You are composing a spoken radio announcement. "
            f"Respond with ONLY the spoken text — no preamble, no sign-off, no markdown, no bullet points. "
            f"Use {max_words} words or fewer. Write numbers as words. "
            f"Start directly with facts. No greetings, no intros, no station names.\n\n"
            f"{entry['prompt']}"
        )
        print(f"\n[SmartAnnounce] #{eid}: Running claude -p ({max_words} word limit)...")
        self._set_activity(eid, 'Asking Claude')
        try:
            result = subprocess.run(
                [self._claude_bin, '-p', prompt],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                err = result.stderr.strip()[:200] if result.stderr else '(no stderr)'
                print(f"[SmartAnnounce] #{eid}: claude exited {result.returncode}: {err}")
                return None
            text = result.stdout.strip()
            if not text:
                print(f"[SmartAnnounce] #{eid}: empty response from claude")
                return None
            return text
        except subprocess.TimeoutExpired:
            print(f"[SmartAnnounce] #{eid}: claude timed out after 120s")
            return None
        except Exception as e:
            print(f"[SmartAnnounce] #{eid}: claude error: {e}")
            return None

    def _run_announcement(self, entry, manual=False):
        """Call Claude CLI, get text, speak it. manual=True skips time window check."""
        eid = entry['id']
        try:
            if not self._claude_bin:
                self._set_activity(eid, 'No claude CLI')
                print(f"\n[SmartAnnounce] #{eid}: claude CLI not available")
                return
            if not manual and not self._in_time_window():
                print(f"\n[SmartAnnounce] #{eid}: Skipped — outside time window")
                return
        except Exception as e:
            self._set_activity(eid, f'Error: {e}')
            print(f"\n[SmartAnnounce] #{eid}: Pre-check error: {e}")
            return

        try:
            text = self._call_claude_cli(entry)
            if not text:
                self._set_activity(eid, 'No results')
                return

            # Add optional top/tail text with pauses
            top_text = str(entry.get('top_text', '') or '').strip()
            if not top_text:
                top_text = str(getattr(self.config, f'SMART_ANNOUNCE_{eid}_TOP_TEXT', '') or '').strip()
            if not top_text:
                top_text = str(getattr(self.config, 'SMART_ANNOUNCE_TOP_TEXT', '') or '').strip()
            tail_text = str(entry.get('tail_text', '') or '').strip()
            if not tail_text:
                tail_text = str(getattr(self.config, f'SMART_ANNOUNCE_{eid}_TAIL_TEXT', '') or '').strip()
            if not tail_text:
                tail_text = str(getattr(self.config, 'SMART_ANNOUNCE_TAIL_TEXT', '') or '').strip()
            if top_text:
                text = f"{top_text} ... {text}"
            if tail_text:
                text = f"{text} ... {tail_text}"

            self._set_activity(eid, f'Generating TTS ({len(text.split())}w)')
            print(f"[SmartAnnounce] #{entry['id']}: ── SENDING TO TTS ({len(text.split())} words, voice {entry['voice']}) ──")
            print(f"  {text}")

            # Wait for radio to be free before transmitting
            self._set_activity(eid, 'Waiting for radio')
            for attempt in range(100):
                vad_busy = getattr(self.gateway, 'vad_active', False)
                pb_busy = (self.gateway.playback_source and
                           self.gateway.playback_source.current_file)
                if not vad_busy and not pb_busy:
                    break
                if attempt == 0:
                    print(f"[SmartAnnounce] #{entry['id']}: Waiting for radio to be free...")
                time.sleep(5)
            else:
                self._set_activity(eid, 'Dropped (radio busy)')
                print(f"[SmartAnnounce] #{entry['id']}: Radio busy too long, dropping announcement")
                return

            # RTS switching only needed for AIOC PTT — the relay must route mic
            # wiring through front panel. Software PTT uses !ptt via serial which
            # requires USB Controlled (serial connected), so RTS must NOT be changed.
            _ptt_method = str(getattr(self.gateway.config, 'PTT_METHOD', 'aioc')).lower()
            _tx_radio = str(getattr(self.gateway.config, 'TX_RADIO', 'th9800')).lower()
            _rts_saved = None
            _cat = getattr(self.gateway, 'cat_client', None)
            if _ptt_method != 'software' and _tx_radio != 'd75' and _cat:
                _rts_saved = _cat.get_rts()
                if _rts_saved is None or _rts_saved is True:
                    try:
                        self._set_activity(eid, 'Switching RTS')
                        _cat._pause_drain()
                        try:
                            _cat.set_rts(False)  # Radio Controlled
                            time.sleep(0.3)
                            _cat._drain(0.5)
                        finally:
                            _cat._drain_paused = False
                        print(f"[SmartAnnounce] #{eid}: RTS changed to Radio Controlled (was {'USB' if _rts_saved else 'unknown'})")
                    except Exception as _re:
                        print(f"[SmartAnnounce] #{eid}: RTS set failed: {_re}")

            self._set_activity(eid, f'Speaking ({len(text.split())}w)')
            # Reset radio activity timestamp before PTT — claude -p takes 15-60s
            # during which no drain reads happen, making _last_radio_rx stale.
            # Without this, software PTT refuses to key.
            if _tx_radio != 'd75' and self.gateway.cat_client and self.gateway.cat_client._last_radio_rx > 0:
                self.gateway.cat_client._last_radio_rx = time.monotonic()
            self.gateway.speak_text(text, voice=entry['voice'])

            # Wait for playback to finish before restoring RTS
            for _w in range(600):
                if not (self.gateway.playback_source and self.gateway.playback_source.current_file):
                    break
                time.sleep(0.1)

            if _ptt_method != 'software' and _tx_radio != 'd75' and _cat and _rts_saved is not None and _rts_saved is not False:
                try:
                    self._set_activity(eid, 'Restoring RTS')
                    _cat.set_rts(_rts_saved)
                    print(f"[SmartAnnounce] #{eid}: RTS restored to {'USB' if _rts_saved else 'Radio'} Controlled")
                    # Refresh display after RTS change
                    time.sleep(0.3)
                    _cat._pause_drain()
                    try:
                        _cat._send_button([0x00, 0x25], 3, 5)
                        time.sleep(0.15)
                        _cat._send_button_release()
                        time.sleep(0.3)
                        _cat._drain(0.5)
                        _cat._send_button([0x00, 0xA5], 3, 5)
                        time.sleep(0.15)
                        _cat._send_button_release()
                        time.sleep(0.3)
                        _cat._drain(0.5)
                    finally:
                        _cat._drain_paused = False
                except Exception as _re:
                    print(f"[SmartAnnounce] #{eid}: RTS restore failed: {_re}")

            self._set_activity(eid, 'Done')

        except Exception as e:
            self._set_activity(eid, f'Error: {e}')
            print(f"\n[SmartAnnounce] #{entry['id']}: Error: {e}")

    def trigger(self, entry_id):
        """Manually trigger a specific announcement. Returns True if found."""
        with self._lock:
            entry = next((e for e in self._entries if e['id'] == entry_id), None)
        if entry:
            threading.Thread(target=self._run_announcement, args=(entry, True),
                             daemon=True, name=f"SmartAnnounce-{entry_id}").start()
            return True
        return False

    def get_entries(self):
        """Return list of configured entries for status display."""
        return self._entries
