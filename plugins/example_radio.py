"""Example Radio Plugin — template for writing your own.

This file shows the complete plugin interface with detailed comments.
Copy this file, rename it, and fill in the hardware-specific parts.

To use:
  1. Copy this file: cp example_radio.py myradio.py
  2. Edit PLUGIN_ID, PLUGIN_NAME, and implement the hardware methods
  3. Add to gateway_config.txt: ENABLE_MYRADIO = True
  4. Restart the gateway
  5. The plugin appears as a source in the routing UI — connect it to a bus

Audio format everywhere: 48000 Hz, 16-bit signed little-endian, mono.
One chunk = 2400 samples = 4800 bytes = 50 ms.
"""

import queue
import threading
import time

import numpy as np


class ExampleRadioPlugin:
    """A radio plugin template.

    Required class attributes:
        PLUGIN_ID   — lowercase identifier used in routing config (e.g., 'myradio')
        PLUGIN_NAME — human-readable name shown in the UI
    """

    # ── Required class attributes ──────────────────────────────────
    PLUGIN_ID = 'example'           # Used as source ID in routing config
    PLUGIN_NAME = 'Example Radio'   # Shown in routing UI nodes

    # ── Standard properties (read by BusManager and routing UI) ────
    name = PLUGIN_NAME              # Must match PLUGIN_NAME
    ptt_control = False             # True if this plugin can key a transmitter
    #   When True: audio from this source triggers PTT on solo/listen buses
    #   When False: audio is passive (receive-only, like an SDR)

    def __init__(self):
        # Audio state
        self.enabled = True         # False = get_audio returns None
        self.muted = False          # False = get_audio returns None
        self.audio_level = 0        # RX level 0-100 (updated in get_audio)
        self.tx_audio_level = 0     # TX level 0-100 (updated in put_audio)
        self.audio_boost = 1.0      # RX gain multiplier (set from routing UI)
        self.tx_audio_boost = 1.0   # TX gain multiplier (set from routing UI)

        # Internal state
        self._config = None
        self._gateway = None
        self._rx_queue = None
        self._running = False
        self._thread = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def setup(self, config, gateway=None):
        """Initialize hardware. Called once during gateway startup.

        Args:
            config: gateway config object. Your config keys are available as
                    attributes, e.g., config.EXAMPLE_DEVICE if gateway_config.txt
                    has EXAMPLE_DEVICE = /dev/ttyUSB0
            gateway: RadioGateway instance. Use for shared state if needed.
                     Avoid holding strong references to large objects.

        Returns:
            True if hardware initialized successfully, False to skip this plugin.
        """
        self._config = config
        self._gateway = gateway
        self._rx_queue = queue.Queue(maxsize=16)

        # ── Open your hardware here ──
        # Example: self._serial = serial.Serial(config.EXAMPLE_DEVICE, 9600)
        # Example: self._stream = pyaudio.open(...)

        # Start RX reader thread
        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, daemon=True,
                                         name=f'{self.PLUGIN_ID}-rx')
        self._thread.start()
        return True

    def cleanup(self):
        """Shutdown hardware. Called when gateway stops."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        # Close your hardware: self._serial.close(), etc.

    # ── Audio Interface ────────────────────────────────────────────

    def get_audio(self, chunk_size=None):
        """Get one chunk of RX audio from this radio.

        Called every 50ms by the bus tick loop (BusManager thread).
        Must be non-blocking — return immediately.

        Args:
            chunk_size: expected bytes (usually 4800). Can be None.

        Returns:
            (pcm_bytes, ptt_required): tuple
            - pcm_bytes: 4800 bytes of 48kHz 16-bit mono PCM, or None if no audio
            - ptt_required: True if this audio should trigger PTT (deterministic
              sources like announcements). Usually False for radio RX.
        """
        if not self.enabled or self.muted:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        try:
            chunk = self._rx_queue.get_nowait()
        except queue.Empty:
            self.audio_level = max(0, int(self.audio_level * 0.7))
            return None, False

        # Apply RX gain boost (set from routing UI per-source gain slider)
        if self.audio_boost != 1.0:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            chunk = np.clip(arr * self.audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Update level meter
        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        level = min(100, max(0, int((20 * np.log10(max(rms, 1) / 32768) + 60) * 100 / 60)))
        self.audio_level = level if level > self.audio_level else int(self.audio_level * 0.7 + level * 0.3)

        return chunk, False

    def put_audio(self, pcm):
        """Send audio to this radio for transmission.

        Called by SoloBus when this radio is the TX target on a bus.
        Only called when PTT is active (bus manages PTT timing).

        Args:
            pcm: 4800 bytes of 48kHz 16-bit mono PCM
        """
        if not self.enabled:
            return

        # Apply TX gain boost
        if self.tx_audio_boost != 1.0:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            pcm = np.clip(arr * self.tx_audio_boost, -32768, 32767).astype(np.int16).tobytes()

        # Update TX level meter
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        self.tx_audio_level = min(100, int(rms / 327.68))

        # ── Write to your hardware here ──
        # Example: self._tx_stream.write(pcm)
        pass

    # ── Control Interface ──────────────────────────────────────────

    def execute(self, cmd):
        """Handle a control command from the web UI or automation.

        Standard commands (implement what your hardware supports):
            {"cmd": "ptt", "state": true/false}      — key/unkey transmitter
            {"cmd": "frequency", "freq_mhz": 146.52}  — tune to frequency
            {"cmd": "ctcss", "tone": 103.5}            — set CTCSS tone (0=off)
            {"cmd": "power", "level": "low"}           — TX power (low/mid/high)
            {"cmd": "status"}                          — request status dict

        Args:
            cmd: dict with at least 'cmd' key

        Returns:
            dict with at least 'ok' key (bool). Include relevant state in response.
        """
        c = cmd.get('cmd', '')

        if c == 'ptt':
            state = cmd.get('state', False)
            # ── Key/unkey your transmitter here ──
            return {'ok': True, 'ptt': state}

        elif c == 'frequency':
            freq = cmd.get('freq_mhz', 0)
            # ── Tune your radio here ──
            return {'ok': True, 'frequency': freq}

        elif c == 'status':
            return {'ok': True, **self.get_status()}

        return {'ok': False, 'error': f'unknown command: {c}'}

    def get_status(self):
        """Return current state for web UI display.

        Called periodically by the status monitor. Keep it fast.
        """
        return {
            'plugin': self.PLUGIN_ID,
            'name': self.name,
            'enabled': self.enabled,
            'muted': self.muted,
            'audio_level': self.audio_level,
            'tx_audio_level': self.tx_audio_level,
        }

    # ── Internal: RX Reader Thread ─────────────────────────────────

    def _rx_loop(self):
        """Background thread: read audio from hardware, queue for get_audio().

        This runs continuously. Read 50ms chunks (4800 bytes at 48kHz 16-bit mono)
        from your hardware and put them on the queue. get_audio() drains the queue
        from the BusManager thread.

        If your hardware uses a different sample rate, resample here.
        If your hardware uses a different format, convert here.
        """
        CHUNK_BYTES = 4800  # 50ms at 48kHz 16-bit mono

        while self._running:
            # ── Replace this with your hardware read ──
            # Example with PyAudio:
            #   pcm = self._input_stream.read(2400, exception_on_overflow=False)
            # Example with subprocess (arecord):
            #   pcm = self._process.stdout.read(CHUNK_BYTES)
            # Example with serial (encoded audio):
            #   raw = self._serial.read(encoded_size)
            #   pcm = self._decode(raw)

            # Placeholder: sleep to simulate 50ms timing
            time.sleep(0.05)
            continue  # Remove this when you add real hardware reads

            # Queue the chunk (drop oldest if full to prevent stalling)
            try:
                self._rx_queue.put_nowait(pcm)
            except queue.Full:
                try:
                    self._rx_queue.get_nowait()
                except queue.Empty:
                    pass
                self._rx_queue.put_nowait(pcm)
