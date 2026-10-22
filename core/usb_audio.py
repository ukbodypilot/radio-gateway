"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import os
import time

import numpy as np
try:
    import pyaudio
except ImportError:   # off the box; hw_deps.require() stops the service
    pyaudio = None
from audio_util import pcm_level


class _USBAudioMixin:
    def find_aioc_audio_device(self):
        """Find AIOC audio device index"""
        # Suppress ALSA warnings if not in verbose mode

        # Only suppress if not verbose
        if not self.config.VERBOSE_LOGGING:
            # Hardcode fd 2 — sys.stderr may be LogWriter (fileno→stdout)
            saved_stderr = os.dup(2)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.close(devnull)
                p = pyaudio.PyAudio()
            finally:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
        else:
            p = pyaudio.PyAudio()
        
        aioc_input_index = None
        aioc_output_index = None
        
        # Check if manually specified
        if self.config.AIOC_INPUT_DEVICE >= 0:
            aioc_input_index = self.config.AIOC_INPUT_DEVICE
        if self.config.AIOC_OUTPUT_DEVICE >= 0:
            aioc_output_index = self.config.AIOC_OUTPUT_DEVICE
        
        # Auto-detect if not specified
        if aioc_input_index is None or aioc_output_index is None:
            if self.config.VERBOSE_LOGGING:
                print("\nSearching for AIOC audio device...")
                print("Available audio devices:")
            
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                name = info['name'].lower()
                
                if self.config.VERBOSE_LOGGING:
                    print(f"  [{i}] {info['name']} (in:{info['maxInputChannels']}, out:{info['maxOutputChannels']})")
                
                # Look for AIOC device by various names
                if any(keyword in name for keyword in ['aioc', 'all-in-one', 'cm108', 'usb audio', 'usb sound']):
                    if self.config.VERBOSE_LOGGING:
                        print(f"    → Potential AIOC device!")
                    if info['maxInputChannels'] > 0 and aioc_input_index is None:
                        aioc_input_index = i
                        if self.config.VERBOSE_LOGGING:
                            print(f"    → Using as INPUT device")
                    if info['maxOutputChannels'] > 0 and aioc_output_index is None:
                        aioc_output_index = i
                        if self.config.VERBOSE_LOGGING:
                            print(f"    → Using as OUTPUT device")
        
        p.terminate()
        return aioc_input_index, aioc_output_index
    
    def find_speaker_device(self, p):
        """Resolve SPEAKER_OUTPUT_DEVICE string to a (index, name) tuple (index may be None for default)."""
        spec = self.config.SPEAKER_OUTPUT_DEVICE.strip()
        if not spec:
            return None, 'system default'
        if spec.isdigit():
            idx = int(spec)
            try:
                name = p.get_device_info_by_index(idx)['name']
            except Exception:
                name = spec
            return idx, name
        # Name search
        if self.config.VERBOSE_LOGGING:
            print("\nSearching for speaker output device...")
            print("Available output devices:")
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if self.config.VERBOSE_LOGGING:
                print(f"  [{i}] {info['name']} (out:{info['maxOutputChannels']})")
            if info['maxOutputChannels'] > 0 and spec.lower() in info['name'].lower():
                return i, info['name']
        print(f"Warning: Speaker output device '{spec}' not found -- using system default")
        return None, 'system default'


    def _speaker_enqueue(self, data):
        """Route audio to speaker — either real PortAudio or virtual (metering only).

        Virtual mode: just track audio level for the routing page, no actual output.
        Real mode: apply volume, queue for PortAudio callback.
        """
        if self.speaker_muted or not data:
            return

        # Level metering (always, regardless of mode)
        try:
            spk = data
            if self.config.SPEAKER_VOLUME != 1.0:
                arr = np.frombuffer(spk, dtype=np.int16).astype(np.float32)
                spk = np.clip(arr * self.config.SPEAKER_VOLUME, -32768, 32767).astype(np.int16).tobytes()
            self.speaker_audio_level = pcm_level(spk, self.speaker_audio_level)
        except Exception:
            return

        # Virtual speaker — metering only, no real audio output
        if not self.speaker_queue:
            return

        try:
            # Absorb hw/sw clock drift: drain excess when queue gets deep.
            _spk_qd = self.speaker_queue.qsize()
            if _spk_qd >= 4:
                _dropped = 0
                while self.speaker_queue.qsize() > 2:
                    try:
                        self.speaker_queue.get_nowait()
                        _dropped += 1
                    except Exception:
                        break
                # Track drops for audio quality trace
                if not hasattr(self, '_spk_drop_count'):
                    self._spk_drop_count = 0
                    self._spk_drop_total = 0
                self._spk_drop_count += _dropped
                self._spk_drop_total += _dropped
            self.speaker_queue.put_nowait(spk)
        except Exception:
            pass

    def _speaker_callback(self, in_data, frame_count, time_info, status):
        """PortAudio callback for speaker output.  Runs on PortAudio's own
        real-time audio thread.  PortAudio maintains an internal buffer
        (typically 2-3 periods ≈ 100-150ms) so brief GIL delays from other
        Python threads (e.g. status bar) are absorbed without underruns."""
        _cb_t0 = time.monotonic()
        _expected_bytes = frame_count * self.config.AUDIO_CHANNELS * 2
        try:
            chunk = self.speaker_queue.get_nowait()
            if self.speaker_muted:
                chunk = b'\x00' * _expected_bytes
            elif len(chunk) < _expected_bytes:
                chunk = chunk + b'\x00' * (_expected_bytes - len(chunk))
        except Exception:
            chunk = b'\x00' * _expected_bytes  # silence on underrun

        if self._trace_recording:
            _qd = self.speaker_queue.qsize()
            self._spk_trace.append((
                _cb_t0 - self._audio_trace_t0,          # 0: time (s)
                0.0,                                      # 1: wait_ms (n/a for callback)
                (time.monotonic() - _cb_t0) * 1000,      # 2: callback_ms
                _qd,                                      # 3: queue depth after get
                len(chunk),                               # 4: data_len
                False,                                    # 5: was_empty
                self.speaker_muted,                       # 6: was_muted
            ))

        return (chunk, pyaudio.paContinue)

    def open_speaker_output(self):
        """Open speaker output — real PortAudio device or virtual (metering only).

        Virtual mode: no PortAudio stream opened, no PipeWire link to mess with.
        Audio level is still tracked for the routing page. Use SPEAKER_MODE=virtual
        in config, or it auto-falls back to virtual if the device isn't found.
        """
        if not self.config.ENABLE_SPEAKER_OUTPUT:
            return

        speaker_mode = str(getattr(self.config, 'SPEAKER_MODE', 'auto')).lower()

        if speaker_mode == 'virtual':
            # Virtual speaker — metering only, no real audio
            print(f"✓ Speaker output: virtual (metering only, no audio device)")
            return

        # Try to open a real PortAudio device
        try:
            device_index, device_name = self.find_speaker_device(self.pyaudio_instance)
            if device_index is None and speaker_mode == 'auto':
                # No matching device — fall back to virtual
                print(f"✓ Speaker output: virtual (no device found, metering only)")
                return
            import queue
            self.speaker_queue = queue.Queue(maxsize=6)
            self.speaker_stream = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=self.config.AUDIO_CHANNELS,
                rate=self.config.AUDIO_RATE,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=self.config.AUDIO_CHUNK_SIZE,
                stream_callback=self._speaker_callback,
            )
            print(f"✓ Speaker output initialized OK")
            print(f"  Device: {device_name}")
        except Exception as e:
            print(f"✓ Speaker output: virtual (device open failed: {e})")
            self.speaker_stream = None

