#!/usr/bin/env python3
"""AI-powered smart announcement manager for radio-gateway."""

import sys
import os
import time
import signal
import threading
import threading as _thr
import subprocess
import shutil
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

class SmartAnnouncementManager:
    """AI-powered announcements with pluggable backend (Claude or Gemini).

    Reads SMART_ANNOUNCE_N entries from config. Each entry has:
        interval (seconds), voice (1-9), target_seconds (max speech length), {prompt}

    The selected AI backend composes a spoken message based on the prompt.
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
        self._client = None  # AI client instance (anthropic.Anthropic or genai model)
        self._backend = str(getattr(self.config, 'SMART_ANNOUNCE_AI_BACKEND', 'duckduckgo')).strip().lower()
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
        # Find the prompt in braces
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

    def _init_ollama(self, verbose=True):
        """Detect Ollama and select a model. Sets _ollama_available and _ollama_model."""
        self._ollama_available = False
        configured_model = str(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_MODEL', '') or '').strip()
        try:
            import urllib.request, json
            req = urllib.request.Request('http://127.0.0.1:11434/api/tags', method='GET')
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                data = json.loads(resp.read())
                models = [m.get('name', '') for m in data.get('models', [])]
                if configured_model:
                    if configured_model in models or any(m.startswith(configured_model) for m in models):
                        self._ollama_model = configured_model
                        self._ollama_available = True
                    elif verbose:
                        print(f"  [SmartAnnounce] Ollama model '{configured_model}' not found (available: {', '.join(models)})")
                        print(f"    Pull it with: ollama pull {configured_model}")
                elif models:
                    self._ollama_model = models[0]
                    self._ollama_available = True
                elif verbose:
                    print("  [SmartAnnounce] Ollama running but no models pulled")
                    print("    Pull a model with: ollama pull llama3.1:8b")
        except Exception:
            pass
        if self._ollama_available and verbose:
            print(f"  [SmartAnnounce] Ollama — using model '{self._ollama_model}'")

    def _init_claude(self):
        """Initialize Claude (Anthropic) backend."""
        api_key = getattr(self.config, 'SMART_ANNOUNCE_API_KEY', '')
        if not api_key:
            print("  [SmartAnnounce] No API key configured (SMART_ANNOUNCE_API_KEY)")
            return False
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            return True
        except ImportError:
            print("  [SmartAnnounce] anthropic package not installed")
            print("    Install with: pip3 install anthropic --break-system-packages")
            return False

    def _init_gemini(self):
        """Initialize Google Gemini backend."""
        api_key = getattr(self.config, 'SMART_ANNOUNCE_GEMINI_API_KEY', '')
        if not api_key:
            print("  [SmartAnnounce] No Gemini API key configured (SMART_ANNOUNCE_GEMINI_API_KEY)")
            return False
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            return True
        except ImportError:
            print("  [SmartAnnounce] google-genai package not installed")
            print("    Install with: pip3 install google-genai --break-system-packages")
            return False

    def _init_duckduckgo(self):
        """Initialize DuckDuckGo search + Ollama backend (free, no API key needed).
        Uses ddgs for web search and Ollama (if running) for natural speech composition.
        Falls back to reading search snippets directly if Ollama is unavailable."""
        try:
            from ddgs import DDGS
            self._client = DDGS()
        except ImportError:
            try:
                from duckduckgo_search import DDGS
                self._client = DDGS()
            except ImportError:
                print("  [SmartAnnounce] ddgs package not installed")
                print("    Install with: pip3 install ddgs --break-system-packages")
                return False
        self._init_ollama()
        if not self._ollama_available:
            print("  [SmartAnnounce] Ollama not available — using search snippets directly")
            print("    For better results, install Ollama: curl -fsSL https://ollama.com/install.sh | sh")
        return True

    def _init_google_scrape(self):
        """Initialize Google AI Overview scrape backend.
        Uses xdotool to drive the user's real Firefox browser on the desktop,
        performs a Google search, clicks 'Show more' to expand the AI Overview,
        then copies the page text and extracts the AI Overview section.
        Requires: xdotool, xclip, Firefox running on DISPLAY=:0."""
        import shutil, subprocess
        missing = []
        for tool in ('xdotool', 'xclip'):
            if not shutil.which(tool):
                missing.append(tool)
        if missing:
            print(f"  [SmartAnnounce] google-scrape requires: {', '.join(missing)}")
            print(f"    Install with: sudo pacman -S {' '.join(missing)}")
            return False
        # Ensure DISPLAY is set (needed for xdotool even if started from a non-GUI shell)
        if not os.environ.get('DISPLAY'):
            os.environ['DISPLAY'] = ':0'
            print("  [SmartAnnounce] Set DISPLAY=:0")
        # Check Firefox at init (non-fatal — it may start later)
        try:
            result = subprocess.run(['xdotool', 'search', '--name', 'Mozilla Firefox'],
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':0')})
            windows = [w.strip() for w in result.stdout.strip().split('\n') if w.strip()]
            if windows:
                print(f"  [SmartAnnounce] Firefox detected ({len(windows)} windows)")
            else:
                print("  [SmartAnnounce] Firefox not detected yet — will check again at announcement time")
        except Exception as e:
            print(f"  [SmartAnnounce] Cannot check Firefox: {e} — will retry at announcement time")
        self._init_ollama()
        if not self._ollama_available:
            print("  [SmartAnnounce] Ollama not available — AI Overview text sent directly to TTS")
        self._client = True  # marker that backend is ready
        return True

    def _init_claude_scrape(self):
        """Initialize Claude AI scrape backend.
        Runs Firefox on a virtual display (Xvfb :99) so scraping doesn't
        interfere with the user's desktop. The user's VNC (:0, port 5900)
        and xrdp (port 3389) are not affected.
        First-time setup: log into claude.ai on the virtual display by running:
          DISPLAY=:99 x11vnc -display :99 -nopw -rfbport 5999 &
        then VNC to port 5999, log into claude.ai in the Firefox there, then close.
        Requires: xdotool, xclip, Xvfb, Firefox."""
        import shutil, subprocess
        missing = []
        for tool in ('xdotool', 'xclip', 'Xvfb', 'firefox'):
            if not shutil.which(tool):
                missing.append(tool)
        if missing:
            print(f"  [SmartAnnounce] claude-scrape requires: {', '.join(missing)}")
            return False

        # Start Xvfb on :99 if not already running
        self._scrape_display = ':99'
        try:
            # Check if :99 is already in use
            result = subprocess.run(['xdpyinfo', '-display', self._scrape_display],
                                    capture_output=True, timeout=3)
            if result.returncode != 0:
                # Start Xvfb on :99 with a reasonable resolution
                print(f"  [SmartAnnounce] Starting Xvfb on {self._scrape_display}...")
                subprocess.Popen(['Xvfb', self._scrape_display, '-screen', '0', '1920x1080x24'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
                # Verify it started
                result = subprocess.run(['xdpyinfo', '-display', self._scrape_display],
                                        capture_output=True, timeout=3)
                if result.returncode != 0:
                    print(f"  [SmartAnnounce] Failed to start Xvfb on {self._scrape_display}")
                    return False
                print(f"  [SmartAnnounce] Xvfb running on {self._scrape_display}")
            else:
                print(f"  [SmartAnnounce] Xvfb already running on {self._scrape_display}")

            # Start x11vnc on port 5999 for the virtual display (troubleshooting/login)
            vnc_port = '5999'
            vnc_check = subprocess.run(['ss', '-tlnp'], capture_output=True, text=True, timeout=3)
            if f':{vnc_port}' not in vnc_check.stdout:
                subprocess.Popen(['x11vnc', '-display', self._scrape_display,
                                  '-nopw', '-shared', '-forever', '-rfbport', vnc_port],
                                 env={**os.environ, 'DISPLAY': self._scrape_display},
                                 stdout=open('/tmp/x11vnc_99.log', 'w'),
                                 stderr=subprocess.STDOUT)
                time.sleep(1)
                print(f"  [SmartAnnounce] VNC for virtual display on port {vnc_port}")
            else:
                print(f"  [SmartAnnounce] VNC already listening on port {vnc_port}")
            print(f"  [SmartAnnounce] To log into claude.ai: VNC to port {vnc_port}")
        except Exception as e:
            print(f"  [SmartAnnounce] Xvfb error: {e}")
            return False

        # Launch Firefox on the virtual display with a separate profile
        scrape_env = {**os.environ, 'DISPLAY': self._scrape_display}
        try:
            result = subprocess.run(['xdotool', 'search', '--name', 'Mozilla Firefox'],
                                    capture_output=True, text=True, timeout=5, env=scrape_env)
            windows = [w.strip() for w in result.stdout.strip().split('\n') if w.strip()]
            if windows:
                print(f"  [SmartAnnounce] Firefox already running on {self._scrape_display}")
            else:
                print(f"  [SmartAnnounce] Launching Firefox on {self._scrape_display}...")
                subprocess.Popen(['firefox', '--no-remote', '-P', 'claude-scrape',
                                  'https://claude.ai'],
                                 env=scrape_env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(5)
                print(f"  [SmartAnnounce] Firefox launched on {self._scrape_display}")
                print(f"  [SmartAnnounce] NOTE: If first run, log into claude.ai via:")
                print(f"    DISPLAY={self._scrape_display} x11vnc -display {self._scrape_display} -nopw -rfbport 5999 &")
                print(f"    Then VNC to port 5999, log in, and close the VNC viewer.")
        except Exception as e:
            print(f"  [SmartAnnounce] Firefox launch error: {e}")

        self._init_ollama()
        if not self._ollama_available:
            print("  [SmartAnnounce] Ollama not available — Claude response will be truncated to target length")
        self._client = True
        return True

    def start(self):
        """Start the background timer thread."""
        if not self._entries:
            return
        try:
            if self._backend == 'duckduckgo':
                ok = self._init_duckduckgo()
            elif self._backend == 'google-scrape':
                ok = self._init_google_scrape()
            elif self._backend == 'claude-scrape':
                ok = self._init_claude_scrape()
            elif self._backend == 'gemini':
                ok = self._init_gemini()
            else:
                ok = self._init_claude()
            if not ok:
                return
            print(f"  [SmartAnnounce] Backend: {self._backend}")
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

    def _run_announcement(self, entry, manual=False):
        """Call AI API, get text, speak it. manual=True skips time window check."""
        eid = entry['id']
        try:
            if not self._client:
                self._set_activity(eid, 'No API client')
                print(f"\n[SmartAnnounce] #{eid}: No API client (missing key?)")
                return
            if not manual and not self._in_time_window():
                print(f"\n[SmartAnnounce] #{eid}: Skipped — outside time window")
                return
        except Exception as e:
            self._set_activity(eid, f'Error: {e}')
            print(f"\n[SmartAnnounce] #{eid}: Pre-check error: {e}")
            return

        max_words = int(entry['target_secs'] * self.WORDS_PER_SECOND)
        system_prompt = (
            f"Summarize the search results as spoken text in {max_words} words or fewer. "
            "Start directly with facts. No greetings, no sign-offs, no intros, no station names, "
            "no website names. Write numbers as words. Only include facts from the provided text."
        )

        try:
            self._set_activity(eid, f'Searching ({self._backend})')
            if self._backend == 'duckduckgo':
                text = self._call_duckduckgo(entry, system_prompt, max_words)
            elif self._backend == 'google-scrape':
                text = self._call_google_scrape(entry, system_prompt, max_words)
            elif self._backend == 'claude-scrape':
                text = self._call_claude_scrape(entry, system_prompt, max_words)
            elif self._backend == 'gemini':
                text = self._call_gemini(entry, system_prompt, max_words)
            else:
                text = self._call_claude(entry, system_prompt, max_words)
            if not text:
                self._set_activity(eid, 'No results')
                return

            # No truncation — let the LLM/backend control length naturally

            # Add optional top/tail text with pauses
            # Priority: entry dict override → per-slot config → global config
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
            # Reset radio activity timestamp before PTT — the scrape + Ollama
            # process takes 60-120s during which no drain reads happen, making
            # _last_radio_rx stale. Without this, software PTT refuses to key.
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
            print(f"\n[SmartAnnounce] #{entry['id']}: API error: {e}")

    def _call_claude(self, entry, system_prompt, max_words):
        """Call Claude API with web search, return announcement text or None."""
        print(f"\n[SmartAnnounce] #{entry['id']}: Calling Claude API...")
        response = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": entry['prompt']}],
        )
        text_parts = []
        for block in response.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)
        text = ' '.join(text_parts).strip()
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Claude")
            return None
        return text

    def _call_gemini(self, entry, system_prompt, max_words):
        """Call Gemini API with Google Search grounding, return announcement text or None."""
        from google.genai import types
        print(f"\n[SmartAnnounce] #{entry['id']}: Calling Gemini API (Google Search)...")
        google_search_tool = types.Tool(google_search=types.GoogleSearch())
        response = self._client.models.generate_content(
            model="gemini-2.0-flash",
            contents=entry['prompt'],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[google_search_tool],
                max_output_tokens=1024,
            ),
        )
        text = response.text.strip() if response.text else ''
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Gemini")
            return None
        return text

    def _call_duckduckgo(self, entry, system_prompt, max_words):
        """Free web search via DuckDuckGo + Ollama for speech composition.
        Falls back to formatted search snippets if Ollama is unavailable."""
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        import re

        search_query = entry['prompt']
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        print(f"\n[SmartAnnounce] #{entry['id']}: Searching: {search_query}")
        ddgs = DDGS()

        # Use both web search and news search for richer results
        web_results = []
        news_results = []
        try:
            web_results = ddgs.text(search_query, max_results=5) or []
        except Exception as e:
            print(f"[SmartAnnounce] #{entry['id']}: web search error: {e}")
        try:
            news_results = ddgs.news(search_query, max_results=5) or []
        except Exception as e:
            print(f"[SmartAnnounce] #{entry['id']}: news search error: {e}")

        if not web_results and not news_results:
            print(f"[SmartAnnounce] #{entry['id']}: no search results")
            return None

        # Build context — news results first (more relevant for current events)
        context_parts = []
        if news_results:
            context_parts.append("NEWS HEADLINES:")
            for r in news_results:
                context_parts.append(f"- {r.get('title', '')}: {r.get('body', '')}")
        if web_results:
            context_parts.append("WEB RESULTS:")
            for r in web_results:
                context_parts.append(f"- {r.get('title', '')}: {r.get('body', '')}")
        search_context = "\n".join(context_parts)

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── SEARCH RESULTS ({len(news_results)} news, {len(web_results)} web) ──")
            if news_results:
                print(f"  NEWS:")
                for r in news_results:
                    print(f"    {r.get('title', '')}: {r.get('body', '')[:120]}")
            if web_results:
                print(f"  WEB:")
                for r in web_results:
                    print(f"    {r.get('title', '')}: {r.get('body', '')[:120]}")

        # If Ollama is available, use it to compose natural speech
        if getattr(self, '_ollama_available', False):
            return self._ollama_compose(entry, system_prompt, max_words, search_context)

        # Fallback: format search snippets directly for TTS
        print(f"[SmartAnnounce] #{entry['id']}: Composing from search snippets (no Ollama)...")
        snippets = []
        for r in (news_results + web_results)[:3]:
            body = r.get('body', '').strip()
            if body:
                # Clean up for speech: remove URLs, extra whitespace
                body = re.sub(r'https?://\S+', '', body)
                body = re.sub(r'\s+', ' ', body).strip()
                snippets.append(body)
        text = '. '.join(snippets)
        # Trim to word limit
        words = text.split()
        if len(words) > max_words:
            text = ' '.join(words[:max_words])
        return text if text else None

    def _scrape_google_ai_overview(self, search_query):
        """Drive the real Firefox browser via xdotool to Google search and extract AI Overview.
        Returns the AI Overview text or None."""
        import subprocess, urllib.parse
        display_env = {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':0')}

        def xdo(*args, timeout=5):
            return subprocess.run(['xdotool'] + list(args),
                                  capture_output=True, text=True, timeout=timeout, env=display_env)

        def xclip_get():
            r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                               capture_output=True, text=True, timeout=5, env=display_env)
            return r.stdout if r.returncode == 0 else ''

        # Find the main Firefox window (largest one with "Mozilla Firefox" in title)
        def _find_firefox_window():
            """Return (wid, area) of the largest Firefox window, or (None, 0)."""
            r = xdo('search', '--name', 'Mozilla Firefox')
            if r.returncode != 0 or not r.stdout.strip():
                return None, 0
            wids = [w.strip() for w in r.stdout.strip().split('\n') if w.strip()]
            best_wid, best_area = None, 0
            for wid in wids:
                try:
                    geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                                         capture_output=True, text=True, timeout=3, env=display_env)
                    w = h = 0
                    for line in geo.stdout.strip().split('\n'):
                        if line.startswith('WIDTH='): w = int(line.split('=')[1])
                        if line.startswith('HEIGHT='): h = int(line.split('=')[1])
                    area = w * h
                    if area > best_area:
                        best_area = area
                        best_wid = wid
                except Exception:
                    continue
            return best_wid, best_area

        best_wid, best_area = _find_firefox_window()

        if not best_wid or best_area < 10000:
            # No usable Firefox window — try to launch it
            launched = False
            if best_wid is None:
                print(f"[SmartAnnounce] google-scrape: Firefox not running, launching...")
                try:
                    subprocess.Popen(['firefox'], env=display_env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launched = True
                except Exception as e:
                    print(f"[SmartAnnounce] google-scrape: failed to launch Firefox: {e}")
                    return None
            else:
                print(f"[SmartAnnounce] google-scrape: Firefox window too small, waiting for it to load...")
                launched = True

            if launched:
                # Wait up to 30s for a fully-rendered window (area >= 10000)
                for _wait in range(30):
                    time.sleep(1)
                    best_wid, best_area = _find_firefox_window()
                    if best_wid and best_area >= 10000:
                        print(f"[SmartAnnounce] google-scrape: Firefox ready after {_wait + 1}s")
                        # Extra settle time for freshly launched Firefox to finish
                        # loading its home page so it doesn't interfere with navigation
                        time.sleep(5)
                        # Re-find window in case IDs changed during load
                        best_wid, best_area = _find_firefox_window()
                        break
                else:
                    print(f"[SmartAnnounce] google-scrape: Firefox not ready within 30s")
                    return None

        # Save currently active window to restore later
        active_result = xdo('getactivewindow')
        prev_wid = active_result.stdout.strip() if active_result.returncode == 0 else None

        try:
            # Activate Firefox
            xdo('windowactivate', '--sync', best_wid)
            time.sleep(0.2)

            # Navigate via URL bar (Ctrl+L) — dev console doesn't work reliably
            # when Firefox is showing a page with keyboard event handlers.
            # Use udm=50 to go directly to Google AI Mode (no JS click needed).
            encoded_q = urllib.parse.quote_plus(search_query)
            url = f'https://www.google.com/search?q={encoded_q}&hl=en&udm=50'
            print(f"[SmartAnnounce] google-scrape: navigating Firefox to AI Mode...")
            xdo('key', 'ctrl+l')
            time.sleep(0.3)
            xdo('key', 'ctrl+a')
            time.sleep(0.1)
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=url.encode(), env=display_env, timeout=3)
            xdo('key', 'ctrl+v')
            time.sleep(0.1)
            xdo('key', 'Return')
            print(f"[SmartAnnounce] google-scrape: waiting for AI Mode response...")
            time.sleep(10)

            # Re-find and re-activate Firefox (in case an ad/popup stole focus)
            best_wid2, _ = _find_firefox_window()
            if not best_wid2:
                best_wid2 = best_wid
            xdo('windowactivate', '--sync', best_wid2)
            time.sleep(0.3)

            # Clear clipboard so stale data from prior scrape can't leak through
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=b'', env=display_env, timeout=3)

            # Click near the top-left of the page content (avoids ads which are
            # typically in the center/right) to focus the page, then select all + copy
            xdo('mousemove', '--window', best_wid2, '150', '300')
            time.sleep(0.1)
            xdo('click', '1')
            time.sleep(0.2)
            xdo('key', 'ctrl+a')
            time.sleep(0.2)
            xdo('key', 'ctrl+c')
            time.sleep(0.3)

            # Get clipboard
            page_text = xclip_get()
            if not page_text:
                print(f"[SmartAnnounce] google-scrape: clipboard empty")
                return None

            # Extract AI content — two formats:
            # 1. "AI Overview" section in regular search results
            # 2. AI Mode page (content starts after search query, ends at sources/footer)
            import re
            ai_start = page_text.find('AI Overview')
            if ai_start != -1:
                # Format 1: regular AI Overview
                ai_text = page_text[ai_start:]
                for end_marker in ['Dive deeper in AI Mode', 'AI can make mistakes', 'Dive deeper']:
                    end_pos = ai_text.find(end_marker)
                    if end_pos > 0:
                        ai_text = ai_text[:end_pos]
                        break
                ai_text = ai_text.replace('AI Overview', '', 1).strip()
                ai_text = re.sub(r'^\+\d+\s*', '', ai_text).strip()
            else:
                # Format 2: AI Mode — content is between the search query and footer/sources
                # Page starts with: "Skip to main content...AI Mode\nAll\nNews...\n<search query>\n<AI content>"
                lines = page_text.split('\n')
                # Find the search query line, content starts after it
                query_lower = search_query.lower().strip()
                content_start = -1
                for i, line in enumerate(lines):
                    if line.strip().lower() == query_lower:
                        content_start = i + 1
                        break
                if content_start == -1:
                    # Try partial match
                    for i, line in enumerate(lines):
                        if query_lower[:30] in line.strip().lower():
                            content_start = i + 1
                            break
                if content_start == -1:
                    # Check for CAPTCHA
                    if 'unusual traffic' in page_text.lower() or 'captcha' in page_text.lower():
                        print(f"[SmartAnnounce] google-scrape: Google CAPTCHA detected")
                    else:
                        print(f"[SmartAnnounce] google-scrape: could not find AI content in {len(page_text)} chars")
                    return None
                # Content runs until sources/footer markers
                ai_lines = []
                for line in lines[content_start:]:
                    lt = line.strip()
                    # Stop at footer/source markers
                    if lt in ('Sources', 'Related searches', 'People also search for',
                              'HelpSend feedbackPrivacyTerms') or lt.startswith('Results are personalized'):
                        break
                    ai_lines.append(lt)
                ai_text = '\n'.join(ai_lines).strip()

            return ai_text if ai_text else None

        finally:
            # Restore previous window focus
            if prev_wid:
                try:
                    xdo('windowactivate', prev_wid)
                except Exception:
                    pass

    def _call_google_scrape(self, entry, system_prompt, max_words):
        """Scrape Google AI Overview via Firefox, pre-clean, then summarize with Ollama."""
        import re
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        search_query = entry['prompt']
        print(f"\n[SmartAnnounce] #{entry['id']}: Searching: {search_query}")

        ai_text = self._scrape_google_ai_overview(search_query)
        if not ai_text:
            print(f"[SmartAnnounce] #{entry['id']}: no AI Overview found")
            return None

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── AI OVERVIEW ({len(ai_text)} chars) ──")
            for line in ai_text.split('\n'):
                print(f"  {line}")

        # Pre-clean: strip junk so Ollama processes less text
        lines = ai_text.split('\n')
        cleaned = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip short header-only lines
            if line.endswith(':') and len(line.split()) <= 6:
                continue
            # Remove bullet markers
            line = re.sub(r'^[\s•·\-\*]+', '', line).strip()
            # Remove URLs
            line = re.sub(r'https?://\S+', '', line).strip()
            # Remove citation markers like [1], [2], +1, +2
            line = re.sub(r'\[\d+\]', '', line)
            line = re.sub(r'^\+\d+\s*', '', line).strip()
            # Remove source attributions
            line = re.sub(r'^Source:.*$', '', line, flags=re.IGNORECASE).strip()
            if line and len(line.split()) >= 3:
                cleaned.append(line)

        pre_cleaned = ' '.join(cleaned)
        # Trim input to keep Ollama fast
        words = pre_cleaned.split()
        if len(words) > 200:
            pre_cleaned = ' '.join(words[:200])

        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── PRE-CLEANED ({len(pre_cleaned.split())} words) ──")
            print(f"  {pre_cleaned[:300]}...")

        # Send pre-cleaned text through Ollama for natural spoken summary
        return self._ollama_compose(entry, system_prompt, max_words, pre_cleaned)

    def _scrape_claude_ai(self, prompt_text, eid=None):
        """Drive Firefox via xdotool to claude.ai, enter prompt, and copy response.
        The prompt itself contains length/format instructions so the response
        is used directly by gTTS with no post-processing.
        Returns the response text or None."""
        import subprocess
        _disp = getattr(self, '_scrape_display', ':0')
        display_env = {**os.environ, 'DISPLAY': _disp}

        def xdo(*args, timeout=5):
            return subprocess.run(['xdotool'] + list(args),
                                  capture_output=True, text=True, timeout=timeout, env=display_env)

        def xclip_get():
            r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                               capture_output=True, text=True, timeout=5, env=display_env)
            return r.stdout if r.returncode == 0 else ''

        def xclip_set(text):
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=text.encode(), env=display_env, timeout=3)

        def _find_firefox_window():
            """Return (wid, area) of the largest Firefox window, or (None, 0)."""
            r = xdo('search', '--name', 'Mozilla Firefox')
            if r.returncode != 0 or not r.stdout.strip():
                return None, 0
            wids = [w.strip() for w in r.stdout.strip().split('\n') if w.strip()]
            best_wid, best_area = None, 0
            for wid in wids:
                try:
                    geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                                         capture_output=True, text=True, timeout=3, env=display_env)
                    w = h = 0
                    for line in geo.stdout.strip().split('\n'):
                        if line.startswith('WIDTH='): w = int(line.split('=')[1])
                        if line.startswith('HEIGHT='): h = int(line.split('=')[1])
                    area = w * h
                    if area > best_area:
                        best_area = area
                        best_wid = wid
                except Exception:
                    continue
            return best_wid, best_area

        best_wid, best_area = _find_firefox_window()

        if not best_wid or best_area < 10000:
            launched = False
            if best_wid is None:
                print(f"[SmartAnnounce] claude-scrape: Firefox not running, launching on {_disp}...")
                try:
                    subprocess.Popen(['firefox', '--no-remote', '-P', 'claude-scrape'],
                                     env=display_env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launched = True
                except Exception as e:
                    print(f"[SmartAnnounce] claude-scrape: failed to launch Firefox: {e}")
                    return None
            else:
                print(f"[SmartAnnounce] claude-scrape: Firefox window too small, waiting...")
                launched = True
            if launched:
                for _wait in range(30):
                    time.sleep(1)
                    best_wid, best_area = _find_firefox_window()
                    if best_wid and best_area >= 10000:
                        print(f"[SmartAnnounce] claude-scrape: Firefox ready after {_wait + 1}s")
                        time.sleep(5)
                        best_wid, best_area = _find_firefox_window()
                        break
                else:
                    print(f"[SmartAnnounce] claude-scrape: Firefox not ready within 30s")
                    return None

        # Save currently active window to restore later
        active_result = xdo('getactivewindow')
        prev_wid = active_result.stdout.strip() if active_result.returncode == 0 else None

        try:
            # Activate Firefox
            xdo('windowactivate', '--sync', best_wid)
            time.sleep(0.2)

            # Navigate to claude.ai via URL bar
            print(f"[SmartAnnounce] claude-scrape: navigating to claude.ai...")
            xdo('key', 'ctrl+l')
            time.sleep(0.3)
            xdo('key', 'ctrl+a')
            time.sleep(0.1)
            xclip_set('https://claude.ai/new')
            xdo('key', 'ctrl+v')
            time.sleep(0.1)
            xdo('key', 'Return')

            # Wait for claude.ai to load — verify by checking window title
            print(f"[SmartAnnounce] claude-scrape: waiting for page load...")
            for _load_wait in range(15):
                time.sleep(1)
                r = xdo('getactivewindow')
                if r.returncode == 0:
                    wid = r.stdout.strip()
                    name_r = subprocess.run(['xdotool', 'getwindowname', wid],
                                            capture_output=True, text=True, timeout=3, env=display_env)
                    title = name_r.stdout.strip() if name_r.returncode == 0 else ''
                    if 'Claude' in title or 'claude' in title:
                        print(f"[SmartAnnounce] claude-scrape: page loaded after {_load_wait + 1}s")
                        break
            else:
                print(f"[SmartAnnounce] claude-scrape: page may not have loaded (proceeding anyway)")

            # Re-find and re-activate Firefox
            best_wid2, _ = _find_firefox_window()
            if not best_wid2:
                best_wid2 = best_wid
            xdo('windowactivate', '--sync', best_wid2)
            time.sleep(0.5)

            # The chat input on claude.ai/new is auto-focused on page load.
            # Don't click anything — clicks risk hitting UI elements and stealing focus.
            # Just paste directly and submit.
            if eid: self._set_activity(eid, 'Entering prompt')
            print(f"[SmartAnnounce] claude-scrape: entering prompt ({len(prompt_text)} chars)...")
            xclip_set(prompt_text)
            time.sleep(0.2)
            xdo('key', 'ctrl+v')
            time.sleep(1.0)

            # Submit with Enter
            xdo('key', 'Return')
            if eid: self._set_activity(eid, 'Waiting for Claude')
            print(f"[SmartAnnounce] claude-scrape: submitted, waiting for Claude response...")

            # Wait for Claude to respond, then copy page text via Ctrl+A/Ctrl+C.
            # Click the response area (60% right, 50% down) to focus the page body
            # so Ctrl+A selects page content, not URL bar or chat input.
            time.sleep(25)  # initial wait for Claude to generate response

            prev_text = ''
            stable_count = 0
            page_text = ''

            for attempt in range(6):  # 6 polls × ~5s = ~30s max
                best_wid3, _ = _find_firefox_window()
                _wid = best_wid3 or best_wid2
                if _wid:
                    xdo('windowactivate', '--sync', _wid)
                time.sleep(0.3)

                # Focus the page body by middle-clicking (button 2) in the
                # response area. Middle-click doesn't follow links in Firefox,
                # avoiding the problem of accidentally opening new tabs.
                try:
                    _geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', _wid],
                                         capture_output=True, text=True, timeout=3, env=display_env)
                    _ww = _wh = 0
                    for _gl in _geo.stdout.strip().split('\n'):
                        if _gl.startswith('WIDTH='): _ww = int(_gl.split('=')[1])
                        if _gl.startswith('HEIGHT='): _wh = int(_gl.split('=')[1])
                    if _ww and _wh:
                        _cx, _cy = str(int(_ww * 0.6)), str(int(_wh * 0.5))
                        xdo('mousemove', '--window', _wid, _cx, _cy)
                        time.sleep(0.05)
                        xdo('click', '2')  # middle-click — focuses without following links
                        time.sleep(0.2)
                except Exception:
                    pass
                xclip_set('')
                xdo('key', 'ctrl+a')
                time.sleep(0.3)
                xdo('key', 'ctrl+c')
                time.sleep(0.5)

                page_text = xclip_get()
                if eid: self._set_activity(eid, f'Reading response ({attempt+1}/6)')
                print(f"[SmartAnnounce] claude-scrape: poll {attempt+1}/6, clipboard: {len(page_text)} chars")

                if page_text and len(page_text) > 100:
                    if page_text == prev_text:
                        stable_count += 1
                        if stable_count >= 1:
                            print(f"[SmartAnnounce] claude-scrape: page text stable")
                            break
                    else:
                        stable_count = 0
                    prev_text = page_text

                time.sleep(5)

            if not page_text and prev_text:
                page_text = prev_text

            if not page_text or len(page_text) < 50:
                print(f"[SmartAnnounce] claude-scrape: no page text captured ({len(page_text) if page_text else 0} chars)")
                return None

            # Extract Claude's response from the page text.
            # The page contains nav, prompt, Claude's response, and footer.
            # Find the prompt, then take everything after it until footer markers.
            lines = page_text.split('\n')
            print(f"[SmartAnnounce] claude-scrape: captured {len(page_text)} chars, {len(lines)} lines")

            # Find the user's prompt
            prompt_snippet = prompt_text[:40].strip()
            content_start = -1
            for i, line in enumerate(lines):
                if prompt_snippet in line:
                    content_start = i + 1
                    break
            if content_start == -1:
                short = ' '.join(prompt_text.split()[:4])
                for i, line in enumerate(lines):
                    if short in line:
                        content_start = i + 1
                        break

            if content_start == -1:
                print(f"[SmartAnnounce] claude-scrape: could not find prompt in page")
                print(f"[SmartAnnounce] claude-scrape: FULL: {page_text[:500]}")
                return None

            # Collect response lines until footer
            response_lines = []
            for line in lines[content_start:]:
                lt = line.strip()
                if not lt:
                    continue
                if 'Claude can make mistakes' in lt or \
                   'Please double-check' in lt or \
                   lt in ('Copy', 'Retry', 'Edit', 'Start a new chat'):
                    break
                if 'Anthropic' in lt and len(lt) < 50:
                    break
                if len(lt) <= 2:
                    continue
                response_lines.append(lt)

            # Find the start of Claude's actual prose response.
            # Tool results (weather widgets, search cards) appear as short
            # structured lines (temperatures, day names, percentages) before
            # Claude's natural language response. The prose response typically
            # starts with a sentence — look for the first line that starts
            # with a capital letter and contains a verb-like pattern (has
            # spaces and is longer than a label).
            prose_start = 0
            for j, rl in enumerate(response_lines):
                # A prose sentence: starts with letter, has multiple words,
                # and is reasonably long (not just "78°" or "Monday")
                words = rl.split()
                if len(words) >= 5 and len(rl) > 30:
                    prose_start = j
                    break

            if prose_start > 0:
                print(f"[SmartAnnounce] claude-scrape: skipping {prose_start} widget/tool lines")
                response_lines = response_lines[prose_start:]

            response_text = ' '.join(response_lines).strip()
            print(f"[SmartAnnounce] claude-scrape: extracted {len(response_text)} chars from {len(response_lines)} lines")
            if response_text:
                print(f"[SmartAnnounce] claude-scrape: preview: {response_text[:200]}")
            return response_text if response_text else None

        finally:
            if prev_wid:
                try:
                    xdo('windowactivate', prev_wid)
                except Exception:
                    pass

    def _call_claude_scrape(self, entry, system_prompt, max_words):
        """Scrape Claude AI via Firefox, then rewrite via Ollama for length control."""
        prompt_text = entry['prompt']
        eid = entry['id']
        print(f"\n[SmartAnnounce] #{eid}: claude-scrape prompt: {prompt_text[:80]}...")

        self._set_activity(eid, 'Opening Claude.ai')
        response = self._scrape_claude_ai(prompt_text, eid)
        if not response:
            print(f"[SmartAnnounce] #{eid}: no response from Claude")
            return None

        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        if verbose:
            print(f"[SmartAnnounce] #{eid}: ── CLAUDE RESPONSE ({len(response)} chars) ──")
            print(f"  {response[:500]}")

        # Rewrite via Ollama for length control, or return raw if Ollama unavailable
        if self._ollama_available:
            self._set_activity(eid, f'Condensing ({self._ollama_model})')
            return self._ollama_compose(entry, system_prompt, max_words, response)
        # Truncate to max_words as a basic fallback
        words = response.split()
        if len(words) > max_words:
            response = ' '.join(words[:max_words])
        return response

    def _ollama_compose(self, entry, system_prompt, max_words, search_context):
        """Use local Ollama to compose natural speech from search results."""
        import urllib.request, json
        prompt = (
            f"{system_prompt}\n\n"
            f"Web search results:\n{search_context}\n\n"
            f"Based on the above, compose a complete summary in exactly {max_words} words or fewer. "
            f"You MUST finish your final sentence — never stop mid-sentence. "
            f"No intro, no date or time, just the content."
        )
        verbose = getattr(self.config, 'VERBOSE_LOGGING', False)
        print(f"[SmartAnnounce] #{entry['id']}: Sending to LLM ({self._ollama_model})...")
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── LLM PROMPT ──")
            for line in prompt.split('\n'):
                print(f"  {line}")
        temperature = float(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_TEMPERATURE', 0.7))
        top_p = float(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_TOP_P', 0.9))
        num_ctx = int(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_NUM_CTX', 1024))
        num_thread = int(getattr(self.config, 'SMART_ANNOUNCE_OLLAMA_NUM_THREAD', 0))
        options = {
            "num_predict": max_words * 3,
            "temperature": temperature,
            "top_p": top_p,
            "num_ctx": num_ctx,
        }
        if num_thread > 0:
            options["num_thread"] = num_thread
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: Ollama options: temp={temperature}, top_p={top_p}, ctx={num_ctx}, threads={num_thread or 'all'}, max_tokens={max_words * 3}")
        payload = json.dumps({
            "model": self._ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": options,
            "context": [],  # fresh context — don't carry over from previous calls
        }).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:11434/api/generate',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        text = data.get('response', '').strip()
        if not text:
            print(f"[SmartAnnounce] #{entry['id']}: empty response from Ollama")
            return None
        if verbose:
            print(f"[SmartAnnounce] #{entry['id']}: ── LLM RESPONSE ──")
            print(f"  {text}")
        return text

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


