"""ALSA recovery: rebuilding the input stream, and PyAudio itself.

Extracted from ``core/lifecycle.py`` (step 2 of its split). These two are a
self-contained pair -- ``restart_audio_input`` falls back to
``restart_pyaudio`` when reopening the stream is not enough -- and nothing
outside them calls either.

Both suppress stderr while they work: ALSA is extremely noisy on reopen, and
the useful signal is the return value, not the chatter.

Kept class-bound for the same reason as the rest of ``core/``: the methods read
and write ``self.*`` attributes owned by ``RadioGateway``.
"""

import sys
import time

import pyaudio


class _AudioRestartMixin:
    def restart_audio_input(self):
        """Attempt to restart the audio input stream"""
        # Suppress ALL stderr during restart (ALSA is very noisy)
        import os as restart_os

        # Use the original stderr fd (may have been redirected by LogWriter)
        _orig_stderr = getattr(self, '_orig_stderr', sys.stderr)
        stderr_fd = _orig_stderr.fileno() if hasattr(_orig_stderr, 'fileno') else 2
        saved_stderr_fd = restart_os.dup(stderr_fd)
        devnull_fd = restart_os.open(restart_os.devnull, restart_os.O_WRONLY)
        
        try:
            # Redirect stderr to suppress ALSA messages
            restart_os.dup2(devnull_fd, stderr_fd)
            
            # Signal audio loop to stop reading
            self.restarting_stream = True
            
            # Give current read operation time to complete
            time.sleep(0.15)
            
            if self.config.VERBOSE_LOGGING:
                # Temporarily restore stderr for our diagnostic messages
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print("  [Diagnostic] Closing input stream...")
                restart_os.dup2(devnull_fd, stderr_fd)
            
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                    self.input_stream.close()
                except:
                    pass  # Ignore all errors during close
            
            # Small delay to let ALSA settle
            time.sleep(0.2)
            
            if self.config.VERBOSE_LOGGING:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print("  [Diagnostic] Re-finding AIOC device...")
                # Keep stderr suppressed for device enumeration
            
            # Re-find AIOC device (with stderr still suppressed)
            input_idx, _ = self.find_aioc_audio_device()
            
            if input_idx is None:
                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print("  ✗ Could not find AIOC input device")
                return
            
            # Determine format
            if self.config.AUDIO_BITS == 16:
                audio_format = pyaudio.paInt16
            elif self.config.AUDIO_BITS == 24:
                audio_format = pyaudio.paInt24
            elif self.config.AUDIO_BITS == 32:
                audio_format = pyaudio.paInt32
            else:
                audio_format = pyaudio.paInt16
            
            if self.config.VERBOSE_LOGGING:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                print(f"  [Diagnostic] Opening new input stream (device {input_idx})...")
                restart_os.dup2(devnull_fd, stderr_fd)
            
            try:
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                )

                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print("  ✓ Audio input stream restarted")
                    restart_os.dup2(devnull_fd, stderr_fd)
                
                # Give USB/ALSA time to stabilize after restart
                time.sleep(0.1)
                
                # Update stream age
                self.stream_age = time.time()
                
                # Re-enable audio loop
                self.restarting_stream = False
                
            except Exception as stream_error:
                if self.config.VERBOSE_LOGGING:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                    print(f"  ✗ Failed to open stream: {type(stream_error).__name__}: {stream_error}")
                    print("  [Diagnostic] Attempting full PyAudio restart...")
                
                # If stream creation fails, try restarting entire PyAudio instance
                self.restart_pyaudio()
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                try:
                    restart_os.dup2(saved_stderr_fd, stderr_fd)
                except:
                    pass
                print(f"  ✗ Failed to restart audio input: {type(e).__name__}: {e}")
        finally:
            # Always restore stderr and cleanup
            try:
                restart_os.dup2(saved_stderr_fd, stderr_fd)
                restart_os.close(saved_stderr_fd)
                restart_os.close(devnull_fd)
            except:
                pass
            # Always re-enable audio loop
            self.restarting_stream = False

    def restart_pyaudio(self):
        """Restart the entire PyAudio instance (for serious ALSA errors)"""
        try:
            if self.config.VERBOSE_LOGGING:
                print("  [Diagnostic] Terminating PyAudio instance...")
            
            # Close all streams
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                    self.input_stream.close()
                except:
                    pass
            
            if self.output_stream:
                try:
                    self.output_stream.stop_stream()
                    self.output_stream.close()
                except:
                    pass
            
            # Terminate PyAudio
            if self.pyaudio_instance:
                try:
                    self.pyaudio_instance.terminate()
                except:
                    pass
            
            time.sleep(0.5)  # Give ALSA time to clean up
            
            if self.config.VERBOSE_LOGGING:
                print("  [Diagnostic] Reinitializing PyAudio...")
            
            # Reinitialize PyAudio
            self.pyaudio_instance = pyaudio.PyAudio()
            
            # Find devices
            input_idx, output_idx = self.find_aioc_audio_device()
            
            # Determine format
            if self.config.AUDIO_BITS == 16:
                audio_format = pyaudio.paInt16
            else:
                audio_format = pyaudio.paInt16
            
            # Recreate streams
            if output_idx is not None:
                self.output_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    output=True,
                    output_device_index=output_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4  # Larger buffer for smoother output
                )
            
            if input_idx is not None:
                self.input_stream = self.pyaudio_instance.open(
                    format=audio_format,
                    channels=self.config.AUDIO_CHANNELS,
                    rate=self.config.AUDIO_RATE,
                    input=True,
                    input_device_index=input_idx,
                    frames_per_buffer=self.config.AUDIO_CHUNK_SIZE * 4,
                )

            if self.config.VERBOSE_LOGGING:
                print("  ✓ PyAudio fully restarted")
            
        except Exception as e:
            if self.config.VERBOSE_LOGGING:
                print(f"  ✗ Failed to restart PyAudio: {type(e).__name__}: {e}")
