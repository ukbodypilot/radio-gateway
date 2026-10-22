"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import time

from audio_util import pcm_level


class _MumbleIOMixin:
    def sound_received_handler(self, user, soundchunk):
        """Called when audio is received from Mumble server"""
        _t0 = time.monotonic()

        # Feed MumbleSource for routing system
        if hasattr(self, 'mumble_source') and self.mumble_source:
            self.mumble_source.push_audio(soundchunk.pcm)

        _t1 = time.monotonic()

        # Track when we last received audio
        self.last_rx_audio_time = time.time()

        # Calculate audio level (with smoothing)
        self.rx_audio_level = pcm_level(soundchunk.pcm, self.rx_audio_level)

        _t2 = time.monotonic()

        # Update last sound time
        self.last_sound_time = time.time()

        _t3 = time.monotonic()
        # Timing diagnostic
        if not hasattr(self, '_srh_count'):
            self._srh_count = 0
            self._srh_max_ms = 0.0
            self._srh_total_ms = 0.0
        self._srh_count += 1
        _elapsed = (_t3 - _t0) * 1000
        _push_ms = (_t1 - _t0) * 1000
        _level_ms = (_t2 - _t1) * 1000
        self._srh_total_ms += _elapsed
        if _elapsed > self._srh_max_ms:
            self._srh_max_ms = _elapsed
        if self._srh_count <= 3 or self._srh_count % 50 == 0:
            _avg = self._srh_total_ms / self._srh_count
            print(f"  [SRH] #{self._srh_count}: {_elapsed:.2f}ms (push={_push_ms:.2f} level={_level_ms:.2f}) avg={_avg:.2f}ms max={self._srh_max_ms:.2f}ms")
        # MumbleSource.push_audio() feeds the queue, SoloBus drains it
        # and calls put_audio() + PTT on the radio plugin.
        # Legacy direct path disabled to avoid double-writing to output stream.
    
    def speak_text(self, text, voice=None):
        from text_commands import speak_text as _speak_text
        return _speak_text(self, text, voice=voice)
    def send_text_message(self, message):
        """
        Send text message to current Mumble channel
        
        Args:
            message: Text message to send
        """
        try:
            if self.config.VERBOSE_LOGGING:
                print(f"\n[Mumble Text] Attempting to send: {message[:100]}...")
            if self.mumble and hasattr(self.mumble, 'users') and hasattr(self.mumble.users, 'myself'):
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] Mumble object exists, calling send_message...")
                # Try the send_message method (might be the correct one)
                self.mumble.users.myself.send_message(message)
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✓ Message sent successfully")
            else:
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✗ Mumble not ready")
        except AttributeError as ae:
            # Try alternate method
            try:
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] Trying alternate method...")
                self.mumble.my_channel().send_text_message(message)
                if self.config.VERBOSE_LOGGING:
                    print(f"[Mumble Text] ✓ Message sent via channel method")
            except Exception as e2:
                print(f"\n[Mumble Text] ✗ Both methods failed: {ae}, {e2}")
        except Exception as e:
            print(f"\n[Mumble Text] ✗ Error sending: {e}")
            import traceback
            traceback.print_exc()
    
    def on_text_message(self, text_message):
        from text_commands import on_text_message as _on_text_message
        _on_text_message(self, text_message)
