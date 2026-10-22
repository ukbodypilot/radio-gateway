"""
Unit tests for IC-7100 CI-V plugin — no hardware required.

A CIVSimulator replaces the serial port with an in-process loopback that
speaks the IC-7100 CI-V protocol.  Tests exercise:
  - BCD encode/decode helpers
  - CIVController framing and command methods
  - IC7100Plugin.execute() dispatch
  - Watchdog reconnect logic

Run:
    python3 -m pytest tests/test_ic7100_civ.py -v
    # or without pytest:
    python3 tests/test_ic7100_civ.py
"""

import sys
import os
import threading
import time
import socket
import queue
import unittest

# Add project root and tools/ to path
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, 'tools'))

from ic7100_link_plugin import (
    CIVController,
    IC7100Plugin,
    _freq_to_bcd, _bcd_to_freq,
    _tone_to_bcd, _bcd_to_tone,
    _rit_offset_to_bcd, _bcd_to_rit_offset,
    _pct_to_bcd3, _bcd3_to_pct,
    _dtcs_to_bytes, _bytes_to_dtcs,
    _CTCSS, _DTCS_CODES,
)


# ---------------------------------------------------------------------------
# CI-V Simulator
# ---------------------------------------------------------------------------

class CIVSimulator:
    """
    In-process simulator of an IC-7100 CI-V port.

    Listens on a socket pair; CIVController connects to the fake serial port
    via a monkey-patched serial.Serial that wraps one end of a socketpair.

    The simulator maintains radio state and responds to recognised commands
    with correct CI-V frames.
    """

    PREAMBLE   = b'\xfe\xfe'
    END        = b'\xfd'
    OK         = b'\xfb'
    NG         = b'\xfa'
    CTRL_ADDR  = 0xe0
    RADIO_ADDR = 0x88

    def __init__(self):
        # Simulated radio state
        self.freq_hz  = 146_520_000
        self.mode     = 0x05  # FM
        self.filter   = 0x01
        self.ptt      = False
        self.smeter   = 120   # mid-scale
        self.ctcss_tx_hz  = 88.5
        self.ctcss_rx_hz  = 88.5
        self.ctcss_tx_on  = False
        self.ctcss_rx_on  = False
        # HF feature state (Phase C)
        self.split    = False
        self.rit_hz   = 0
        self.rit_on   = False
        self.xit_on   = False
        self.agc      = 0x02   # 0x01 fast, 0x02 mid, 0x03 slow
        self.nb_on    = False
        self.nb_level = 0      # 0..255
        self.nr_on    = False
        self.nr_level = 0      # 0..255
        self.preamp   = 0      # 0=off, 1=pre1, 2=pre2
        self.atten    = 0x00   # 0x00 off, 0x20 on
        self.if_shift = 0x80   # 0..255, 0x80=center
        self.squelch  = 0      # 0..255 squelch level
        self.rf_power = 0      # 0..255 TX RF output power
        self.mic_gain = 128    # 0..255 TX mic gain (~50%)
        self.po       = 0      # 0..255 TX power meter
        self.swr      = 0      # 0..255 SWR meter
        self.alc      = 0      # 0..255 ALC meter
        self.squelch_open = True   # squelch condition
        self.dtcs_on  = False
        self.dtcs_code = 23        # octal-style int
        self.dtcs_polarity = 0     # 0..3

        # Set up socket pair; expose one end as _sock_ctrl for the controller
        self._sock_radio, self._sock_ctrl = socket.socketpair()
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name='CIVSim')
        self._running = True
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            self._sock_radio.close()
        except Exception:
            pass
        try:
            self._sock_ctrl.close()
        except Exception:
            pass

    # -- Fake serial.Serial --

    def make_fake_serial(self):
        """Return a duck-typed serial.Serial that talks to the simulator."""
        sock = self._sock_ctrl
        sim = self

        class FakeSerial:
            def __init__(self):
                self._buf = b''

            def reset_input_buffer(self):
                pass  # nothing pending on connect

            def write(self, data: bytes):
                sock.sendall(data)

            def _drain(self):
                """Pull anything the simulator has sent into the local buffer."""
                try:
                    sock.setblocking(False)
                    while True:
                        try:
                            chunk = sock.recv(4096)
                        except BlockingIOError:
                            break
                        except Exception:
                            break
                        if not chunk:
                            break
                        self._buf += chunk
                finally:
                    try:
                        sock.setblocking(True)
                    except Exception:
                        pass

            @property
            def in_waiting(self) -> int:
                """Bytes available. _transact polls this instead of doing a
                blocking read of the full frame size — an optimisation the
                plugin gained after this fake was written, which is why every
                transaction here failed with AttributeError."""
                self._drain()
                return len(self._buf)

            def read(self, n: int) -> bytes:
                if not self._buf:
                    # Nothing buffered — wait briefly, as the old read() did.
                    try:
                        sock.settimeout(0.1)
                        self._buf += sock.recv(max(n, 4096))
                    except Exception:
                        pass
                out, self._buf = self._buf[:n], self._buf[n:]
                return out

            def close(self):
                pass  # sim socket closed by stop()

        return FakeSerial()

    # -- Simulator server --

    def _serve(self):
        buf = b''
        while self._running:
            try:
                self._sock_radio.settimeout(0.2)
                data = self._sock_radio.recv(256)
                if not data:
                    break
                buf += data
                while True:
                    frame, buf = self._extract_frame(buf)
                    if frame is None:
                        break
                    resp = self._handle(frame)
                    if resp:
                        # Echo the frame back (IC-7100 echoes its own transmissions)
                        self._sock_radio.sendall(frame)
                        self._sock_radio.sendall(resp)
            except socket.timeout:
                continue
            except Exception:
                break

    def _extract_frame(self, buf):
        """Pull first complete FE FE … FD frame from buf. Returns (frame, remainder)."""
        idx = buf.find(b'\xfe\xfe')
        if idx == -1:
            return None, buf
        end = buf.find(b'\xfd', idx + 4)
        if end == -1:
            return None, buf
        return buf[idx:end+1], buf[end+1:]

    def _make_resp(self, data: bytes) -> bytes:
        return self.PREAMBLE + bytes([self.CTRL_ADDR, self.RADIO_ADDR]) + data + self.END

    def _ok(self) -> bytes:
        return self._make_resp(self.OK)

    def _handle(self, frame: bytes) -> bytes | None:
        # frame = FE FE [radio] [ctrl] [cmd] [subcmd?] [data...] FD
        if len(frame) < 6:
            return None
        # frame[2]=dest=0x88, frame[3]=src=0xe0, frame[4]=cmd
        cmd = frame[4]
        payload = frame[5:-1]  # everything between cmd and FD (may include subcmd+data)

        if cmd == 0x03:   # read freq
            return self._make_resp(bytes([0x03]) + _freq_to_bcd(self.freq_hz))

        elif cmd == 0x05:  # set freq
            if len(payload) >= 5:
                self.freq_hz = _bcd_to_freq(payload[:5])
            return self._ok()

        elif cmd == 0x04:  # read mode
            return self._make_resp(bytes([0x04, self.mode, self.filter]))

        elif cmd == 0x06:  # set mode
            if len(payload) >= 2:
                self.mode = payload[0]
                self.filter = payload[1]
            return self._ok()

        elif cmd == 0x15:  # read meter / status
            if payload and payload[0] == 0x01:
                # Squelch condition: 0=closed, 1=open
                return self._make_resp(
                    bytes([0x15, 0x01, 0x01 if self.squelch_open else 0x00]))
            if payload and payload[0] in (0x02, 0x11, 0x12, 0x13):
                # S-meter (0x02) and the TX meters Po/SWR/ALC (0x11/12/13)
                # share the same 2-BCD 0000–0255 encoding.
                _meter = {0x02: self.smeter, 0x11: self.po,
                          0x12: self.swr, 0x13: self.alc}[payload[0]]
                v = min(255, max(0, _meter))
                b0 = (v // 100) | ((v % 100 // 10) << 4)
                b1 = (v % 10)
                return self._make_resp(bytes([0x15, payload[0], b0, b1]))

        elif cmd == 0x16:  # set/get func
            if not payload:
                return None
            sub = payload[0]
            if len(payload) == 1:  # read
                if sub == 0x02:
                    return self._make_resp(bytes([0x16, 0x02, self.preamp]))
                elif sub == 0x12:
                    return self._make_resp(bytes([0x16, 0x12, self.agc]))
                elif sub == 0x22:
                    return self._make_resp(bytes([0x16, 0x22,
                                                  0x01 if self.nb_on else 0x00]))
                elif sub == 0x40:
                    return self._make_resp(bytes([0x16, 0x40,
                                                  0x01 if self.nr_on else 0x00]))
                elif sub == 0x4a:
                    return self._make_resp(bytes([0x16, 0x4a,
                                                  0x01 if self.dtcs_on else 0x00]))
                elif sub == 0x42:
                    return self._make_resp(bytes([0x16, 0x42,
                                                  0x01 if self.ctcss_rx_on else 0x00]))
                elif sub == 0x43:
                    return self._make_resp(bytes([0x16, 0x43,
                                                  0x01 if self.ctcss_tx_on else 0x00]))
            elif len(payload) == 2:  # write
                if sub == 0x02:
                    self.preamp = payload[1]
                    return self._ok()
                elif sub == 0x12:
                    self.agc = payload[1]
                    return self._ok()
                elif sub == 0x22:
                    self.nb_on = bool(payload[1])
                    return self._ok()
                elif sub == 0x40:
                    self.nr_on = bool(payload[1])
                    return self._ok()
                elif sub == 0x4a:
                    self.dtcs_on = bool(payload[1])
                    return self._ok()
                elif sub == 0x42:
                    self.ctcss_rx_on = bool(payload[1])
                    return self._ok()
                elif sub == 0x43:
                    self.ctcss_tx_on = bool(payload[1])
                    return self._ok()

        elif cmd == 0x1b:  # CTCSS tone freq / DTCS code
            if not payload:
                return None
            sub = payload[0]
            if sub == 0x07:  # DTCS code + polarity (3-byte payload)
                if len(payload) == 1:  # read
                    return self._make_resp(
                        bytes([0x1b, 0x07])
                        + _dtcs_to_bytes(self.dtcs_code, self.dtcs_polarity))
                elif len(payload) >= 4:  # write
                    self.dtcs_code, self.dtcs_polarity = _bytes_to_dtcs(payload[1:4])
                    return self._ok()
            elif len(payload) == 1:  # tone read
                if sub == 0x00:
                    return self._make_resp(
                        bytes([0x1b, 0x00]) + _tone_to_bcd(self.ctcss_tx_hz))
                elif sub == 0x01:
                    return self._make_resp(
                        bytes([0x1b, 0x01]) + _tone_to_bcd(self.ctcss_rx_hz))
            elif len(payload) >= 3:  # tone write
                hz = _bcd_to_tone(payload[1:3])
                if sub == 0x00:
                    self.ctcss_tx_hz = hz
                elif sub == 0x01:
                    self.ctcss_rx_hz = hz
                return self._ok()

        elif cmd == 0x0f:  # Split
            if not payload:  # read
                return self._make_resp(bytes([0x0f, 0x01 if self.split else 0x00]))
            else:            # write
                self.split = bool(payload[0])
                return self._ok()

        elif cmd == 0x11:  # Attenuator
            if not payload:  # read
                return self._make_resp(bytes([0x11, self.atten]))
            else:            # write
                self.atten = payload[0]
                return self._ok()

        elif cmd == 0x21:  # RIT/XIT
            if not payload:
                return None
            sub = payload[0]
            if sub == 0x00:           # offset
                if len(payload) == 1:  # read
                    return self._make_resp(
                        bytes([0x21, 0x00]) + _rit_offset_to_bcd(self.rit_hz))
                elif len(payload) >= 4:  # write
                    self.rit_hz = _bcd_to_rit_offset(payload[1:4])
                    return self._ok()
            elif sub == 0x01:         # RIT on/off
                if len(payload) == 1:
                    return self._make_resp(bytes([0x21, 0x01, 0x01 if self.rit_on else 0x00]))
                elif len(payload) >= 2:
                    self.rit_on = bool(payload[1])
                    return self._ok()
            elif sub == 0x02:         # XIT on/off
                if len(payload) == 1:
                    return self._make_resp(bytes([0x21, 0x02, 0x01 if self.xit_on else 0x00]))
                elif len(payload) >= 2:
                    self.xit_on = bool(payload[1])
                    return self._ok()

        elif cmd == 0x14:  # Level controls (NB lvl, NR lvl, IF shift)
            if not payload:
                return None
            sub = payload[0]
            if sub == 0x03:           # Squelch level
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x03]) + _pct_to_bcd3(self.squelch))
                elif len(payload) >= 3:
                    self.squelch = _bcd3_to_pct(payload[1:3])
                    return self._ok()
            elif sub == 0x07:         # IF shift
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x07]) + _pct_to_bcd3(self.if_shift))
                elif len(payload) >= 3:
                    self.if_shift = _bcd3_to_pct(payload[1:3])
                    return self._ok()
            elif sub == 0x12:         # NB level
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x12]) + _pct_to_bcd3(self.nb_level))
                elif len(payload) >= 3:
                    self.nb_level = _bcd3_to_pct(payload[1:3])
                    return self._ok()
            elif sub == 0x06:         # NR level
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x06]) + _pct_to_bcd3(self.nr_level))
                elif len(payload) >= 3:
                    self.nr_level = _bcd3_to_pct(payload[1:3])
                    return self._ok()
            elif sub == 0x0a:         # RF output power
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x0a]) + _pct_to_bcd3(self.rf_power))
                elif len(payload) >= 3:
                    self.rf_power = _bcd3_to_pct(payload[1:3])
                    return self._ok()
            elif sub == 0x0b:         # Mic gain
                if len(payload) == 1:
                    return self._make_resp(bytes([0x14, 0x0b]) + _pct_to_bcd3(self.mic_gain))
                elif len(payload) >= 3:
                    self.mic_gain = _bcd3_to_pct(payload[1:3])
                    return self._ok()

        elif cmd == 0x1c:  # PTT
            if not payload:
                return None
            sub = payload[0]
            if sub == 0x00:
                if len(payload) == 1:  # read
                    return self._make_resp(
                        bytes([0x1c, 0x00, 0x01 if self.ptt else 0x00]))
                else:  # write
                    self.ptt = bool(payload[1])
                    return self._ok()

        return None  # unrecognised — no response


# ---------------------------------------------------------------------------
# Helper: patch CIVController to use FakeSerial
# ---------------------------------------------------------------------------

def _make_patched_controller(sim: CIVSimulator) -> CIVController:
    """Return a CIVController wired to the simulator instead of a real port."""
    # Run the REAL __init__ (it only assigns attributes — it does not open the
    # port), then swap in the simulator. The old version used __new__ and
    # hand-listed every attribute, so each field added to CIVController since
    # silently went missing here: _tls and data_mode both did, and _tls raised
    # in setUp so the whole suite errored before running. Calling __init__
    # keeps this fixture in sync with the class for free.
    ctrl = CIVController('/dev/null (simulated)', 19200, 0x88, 1.0)
    ctrl._serial = sim.make_fake_serial()
    ctrl.connected = True
    ctrl.freq_hz = 0
    ctrl.mode = 'FM'
    ctrl.filter_idx = 1
    ctrl.transmitting = False
    ctrl.smeter = 0
    ctrl.ctcss_tx_hz = 88.5
    ctrl.ctcss_rx_hz = 88.5
    ctrl.ctcss_tx_on = False
    ctrl.ctcss_rx_on = False
    # HF feature state (Phase C)
    ctrl.split    = False
    ctrl.rit_hz   = 0
    ctrl.rit_on   = False
    ctrl.xit_on   = False
    ctrl.agc      = 'mid'
    ctrl.nb_on    = False
    ctrl.nb_level = 50
    ctrl.nr_on    = False
    ctrl.nr_level = 50
    ctrl.preamp   = 0
    ctrl.atten    = False
    ctrl.if_shift = 50
    ctrl.squelch  = 0
    ctrl.rf_power = 0
    ctrl.mic_gain = 50
    ctrl.po       = 0
    ctrl.swr      = 0
    ctrl.alc      = 0
    ctrl.squelch_open = True
    ctrl.dtcs_on  = False
    ctrl.dtcs_code = 23
    ctrl.dtcs_polarity = 0
    ctrl._poll_state()
    return ctrl


# ---------------------------------------------------------------------------
# Tests: BCD helpers
# ---------------------------------------------------------------------------

class TestBCDHelpers(unittest.TestCase):

    def test_freq_roundtrip(self):
        for hz in [146_520_000, 14_225_000, 432_100_000, 1_296_000_000, 50_000]:
            self.assertEqual(_bcd_to_freq(_freq_to_bcd(hz)), hz,
                             f"Roundtrip failed for {hz} Hz")

    def test_freq_146mhz(self):
        bcd = _freq_to_bcd(146_520_000)
        self.assertEqual(len(bcd), 5)
        self.assertEqual(_bcd_to_freq(bcd), 146_520_000)

    def test_tone_roundtrip(self):
        for hz in _CTCSS:
            decoded = _bcd_to_tone(_tone_to_bcd(hz))
            self.assertAlmostEqual(decoded, hz, places=1,
                                   msg=f"Tone roundtrip failed for {hz} Hz")

    def test_tone_885(self):
        bcd = _tone_to_bcd(88.5)
        self.assertEqual(len(bcd), 2)
        self.assertAlmostEqual(_bcd_to_tone(bcd), 88.5, places=1)

    def test_tone_1000(self):
        # 100.0 Hz
        bcd = _tone_to_bcd(100.0)
        self.assertAlmostEqual(_bcd_to_tone(bcd), 100.0, places=1)


# ---------------------------------------------------------------------------
# Tests: CIVController ↔ simulator
# ---------------------------------------------------------------------------

class TestCIVController(unittest.TestCase):

    def setUp(self):
        self.sim = CIVSimulator()
        self.ctrl = _make_patched_controller(self.sim)

    def tearDown(self):
        self.ctrl.disconnect()
        self.sim.stop()

    def test_get_freq_initial(self):
        freq = self.ctrl.get_freq()
        self.assertIsNotNone(freq)
        self.assertAlmostEqual(freq, 146.52, places=3)

    def test_set_get_freq(self):
        ok = self.ctrl.set_freq(144.200)
        self.assertTrue(ok)
        freq = self.ctrl.get_freq()
        self.assertAlmostEqual(freq, 144.200, places=3)
        self.assertEqual(self.sim.freq_hz, 144_200_000)

    def test_set_freq_hf(self):
        ok = self.ctrl.set_freq(14.225)
        self.assertTrue(ok)
        self.assertEqual(self.sim.freq_hz, 14_225_000)

    def test_get_mode_initial(self):
        mode = self.ctrl.get_mode()
        self.assertEqual(mode, 'FM')

    def test_set_get_mode(self):
        ok = self.ctrl.set_mode('USB')
        self.assertTrue(ok)
        mode = self.ctrl.get_mode()
        self.assertEqual(mode, 'USB')
        self.assertEqual(self.sim.mode, 0x01)

    def test_set_mode_invalid(self):
        ok = self.ctrl.set_mode('INVALID')
        self.assertFalse(ok)

    def test_get_smeter(self):
        self.sim.smeter = 120
        val = self.ctrl.get_smeter()
        self.assertIsNotNone(val)

    def test_ptt_on_off(self):
        ok = self.ctrl.set_ptt(True)
        self.assertTrue(ok)
        self.assertTrue(self.sim.ptt)
        self.assertTrue(self.ctrl.transmitting)

        ok = self.ctrl.set_ptt(False)
        self.assertTrue(ok)
        self.assertFalse(self.sim.ptt)
        self.assertFalse(self.ctrl.transmitting)

    def test_get_ptt(self):
        self.sim.ptt = True
        ptt = self.ctrl.get_ptt()
        self.assertTrue(ptt)

    def test_ctcss_set_tx_freq(self):
        ok = self.ctrl.set_ctcss(tx_hz=103.5)
        self.assertTrue(ok)
        self.assertAlmostEqual(self.sim.ctcss_tx_hz, 103.5, places=1)

    def test_ctcss_set_rx_freq(self):
        ok = self.ctrl.set_ctcss(rx_hz=141.3)
        self.assertTrue(ok)
        self.assertAlmostEqual(self.sim.ctcss_rx_hz, 141.3, places=1)

    def test_ctcss_enable_tx(self):
        ok = self.ctrl.set_ctcss(tx_on=True)
        self.assertTrue(ok)
        self.assertTrue(self.sim.ctcss_tx_on)
        self.assertTrue(self.ctrl.ctcss_tx_on)

    def test_ctcss_enable_rx(self):
        ok = self.ctrl.set_ctcss(rx_on=True)
        self.assertTrue(ok)
        self.assertTrue(self.sim.ctcss_rx_on)
        self.assertTrue(self.ctrl.ctcss_rx_on)

    def test_ctcss_full_set(self):
        ok = self.ctrl.set_ctcss(
            tx_hz=88.5, rx_hz=88.5, tx_on=True, rx_on=True)
        self.assertTrue(ok)
        self.assertAlmostEqual(self.sim.ctcss_tx_hz, 88.5, places=1)
        self.assertAlmostEqual(self.sim.ctcss_rx_hz, 88.5, places=1)
        self.assertTrue(self.sim.ctcss_tx_on)
        self.assertTrue(self.sim.ctcss_rx_on)

    def test_ctcss_read_back(self):
        self.sim.ctcss_tx_hz = 136.5
        self.sim.ctcss_rx_hz = 136.5
        self.sim.ctcss_tx_on = True
        self.sim.ctcss_rx_on = False
        tx_hz, rx_hz, tx_on, rx_on = self.ctrl.get_ctcss()
        self.assertAlmostEqual(tx_hz, 136.5, places=1)
        self.assertAlmostEqual(rx_hz, 136.5, places=1)
        self.assertTrue(tx_on)
        self.assertFalse(rx_on)

    def test_ctcss_all_tones_roundtrip(self):
        for hz in _CTCSS:
            ok = self.ctrl.set_ctcss(tx_hz=hz)
            self.assertTrue(ok, f"set_ctcss failed for {hz} Hz")
            self.assertAlmostEqual(self.sim.ctcss_tx_hz, hz, places=1,
                                   msg=f"CTCSS tone mismatch for {hz} Hz")

    def test_poll_state_updates_cache(self):
        self.sim.freq_hz = 14_225_000
        self.sim.mode = 0x01  # USB
        self.ctrl._poll_state()
        self.assertEqual(self.ctrl.freq_hz, 14_225_000)
        self.assertEqual(self.ctrl.mode, 'USB')

    # -- HF feature tests (Phase C) ------------------------------------

    def test_split_set_get(self):
        self.assertTrue(self.ctrl.set_split(True))
        self.assertTrue(self.sim.split)
        self.assertTrue(self.ctrl.get_split())
        self.assertTrue(self.ctrl.set_split(False))
        self.assertFalse(self.sim.split)

    def test_rit_offset_positive(self):
        self.assertTrue(self.ctrl.set_rit_offset(2500))
        self.assertEqual(self.sim.rit_hz, 2500)
        self.assertEqual(self.ctrl.get_rit_offset(), 2500)

    def test_rit_offset_negative(self):
        self.assertTrue(self.ctrl.set_rit_offset(-1500))
        self.assertEqual(self.sim.rit_hz, -1500)
        self.assertEqual(self.ctrl.get_rit_offset(), -1500)

    def test_rit_offset_clamp(self):
        self.assertTrue(self.ctrl.set_rit_offset(50_000))
        self.assertEqual(self.sim.rit_hz, 9999)

    def test_rit_enable(self):
        self.assertTrue(self.ctrl.set_rit_on(True))
        self.assertTrue(self.sim.rit_on)
        self.assertTrue(self.ctrl.rit_on)

    def test_xit_enable(self):
        self.assertTrue(self.ctrl.set_xit_on(True))
        self.assertTrue(self.sim.xit_on)
        self.assertTrue(self.ctrl.xit_on)

    def test_agc_fast_mid_slow(self):
        for label, byte in [('fast', 0x01), ('mid', 0x02), ('slow', 0x03)]:
            self.assertTrue(self.ctrl.set_agc(label))
            self.assertEqual(self.sim.agc, byte)
            self.assertEqual(self.ctrl.agc, label)

    def test_agc_invalid(self):
        self.assertFalse(self.ctrl.set_agc('hyper'))

    def test_nb_on_off(self):
        self.assertTrue(self.ctrl.set_nb_on(True))
        self.assertTrue(self.sim.nb_on)
        self.assertTrue(self.ctrl.set_nb_on(False))
        self.assertFalse(self.sim.nb_on)

    def test_nb_level(self):
        self.assertTrue(self.ctrl.set_nb_level(50))
        # 50% -> round(50 * 255 / 100) = 128
        self.assertEqual(self.sim.nb_level, 128)
        self.assertEqual(self.ctrl.nb_level, 50)

    def test_nr_on_off(self):
        self.assertTrue(self.ctrl.set_nr_on(True))
        self.assertTrue(self.sim.nr_on)

    def test_nr_level(self):
        self.assertTrue(self.ctrl.set_nr_level(75))
        # 75% -> round(75 * 255 / 100) = 191
        self.assertEqual(self.sim.nr_level, 191)
        self.assertEqual(self.ctrl.nr_level, 75)

    def test_preamp_stages(self):
        for stage in (0, 1, 2):
            self.assertTrue(self.ctrl.set_preamp(stage))
            self.assertEqual(self.sim.preamp, stage)
            self.assertEqual(self.ctrl.preamp, stage)

    def test_preamp_clamp(self):
        self.assertTrue(self.ctrl.set_preamp(99))
        self.assertEqual(self.ctrl.preamp, 2)

    def test_atten_on_off(self):
        # 0x12 = 12 dB. The IC-7100's pad is a fixed 12 dB, not the 20 dB of the
        # IC-7300/IC-7610, and the value byte is the dB amount in BCD. This
        # asserted 0x20 — a value commit 509222a records the radio NG'ing —
        # and was never noticed because the suite errored in setUp.
        self.assertTrue(self.ctrl.set_atten(True))
        self.assertEqual(self.sim.atten, 0x12)
        self.assertTrue(self.ctrl.set_atten(False))
        self.assertEqual(self.sim.atten, 0x00)

    def test_if_shift_center(self):
        self.assertTrue(self.ctrl.set_if_shift(50))
        # 50% -> 128 (close to 0x80 center)
        self.assertEqual(self.sim.if_shift, 128)

    def test_filter_select(self):
        # Filter requires a known mode; set USB first
        self.ctrl.set_mode('USB')
        self.assertTrue(self.ctrl.set_filter(2))
        self.assertEqual(self.sim.filter, 2)
        self.assertEqual(self.ctrl.filter_idx, 2)

    def test_squelch_level(self):
        self.assertTrue(self.ctrl.set_squelch(40))
        # 40% -> round(40 * 255 / 100) = 102
        self.assertEqual(self.sim.squelch, 102)
        self.assertEqual(self.ctrl.squelch, 40)

    def test_squelch_clamp(self):
        self.assertTrue(self.ctrl.set_squelch(250))
        self.assertEqual(self.ctrl.squelch, 100)

    def test_squelch_status_open(self):
        self.sim.squelch_open = True
        self.assertTrue(self.ctrl.get_squelch_status())

    def test_squelch_status_closed(self):
        self.sim.squelch_open = False
        self.assertFalse(self.ctrl.get_squelch_status())

    def test_dtcs_enable(self):
        self.assertTrue(self.ctrl.set_dtcs_on(True))
        self.assertTrue(self.sim.dtcs_on)
        self.assertTrue(self.ctrl.get_dtcs_on())
        self.assertTrue(self.ctrl.set_dtcs_on(False))
        self.assertFalse(self.sim.dtcs_on)

    def test_dtcs_code_set_get(self):
        self.assertTrue(self.ctrl.set_dtcs_code(754, 0))
        self.assertEqual(self.sim.dtcs_code, 754)
        code, pol = self.ctrl.get_dtcs_code()
        self.assertEqual(code, 754)
        self.assertEqual(pol, 0)

    def test_dtcs_code_polarity(self):
        for pol in (0, 1, 2, 3):
            self.assertTrue(self.ctrl.set_dtcs_code(23, pol))
            code, gotpol = self.ctrl.get_dtcs_code()
            self.assertEqual(code, 23)
            self.assertEqual(gotpol, pol)

    def test_dtcs_all_codes_roundtrip(self):
        for code in _DTCS_CODES:
            c, p = _bytes_to_dtcs(_dtcs_to_bytes(code, 0))
            self.assertEqual(c, code, f"DTCS code roundtrip failed for {code}")


class TestStateReadback(unittest.TestCase):
    """Radio → cache sync: the IC-7100 is the source of truth — it has a
    front panel and remembers its own setup — so the plugin must pull the
    radio's current state into its cache, letting the GUI slave to the
    radio rather than the other way round."""

    def setUp(self):
        self.sim = CIVSimulator()
        self.ctrl = _make_patched_controller(self.sim)

    def tearDown(self):
        self.ctrl.disconnect()
        self.sim.stop()

    # -- Individual readback getters --

    def test_get_rit_on(self):
        self.sim.rit_on = True
        self.assertTrue(self.ctrl.get_rit_on())
        self.assertTrue(self.ctrl.rit_on)

    def test_get_xit_on(self):
        self.sim.xit_on = True
        self.assertTrue(self.ctrl.get_xit_on())
        self.assertTrue(self.ctrl.xit_on)

    def test_get_agc(self):
        for raw, name in ((0x01, 'fast'), (0x02, 'mid'), (0x03, 'slow')):
            self.sim.agc = raw
            self.assertEqual(self.ctrl.get_agc(), name)

    def test_get_nb(self):
        self.sim.nb_on, self.sim.nb_level = True, 255
        on, level = self.ctrl.get_nb()
        self.assertTrue(on)
        self.assertEqual(level, 100)

    def test_get_nr(self):
        self.sim.nr_on, self.sim.nr_level = True, 128
        on, level = self.ctrl.get_nr()
        self.assertTrue(on)
        self.assertEqual(level, 50)

    def test_get_preamp(self):
        self.sim.preamp = 2
        self.assertEqual(self.ctrl.get_preamp(), 2)

    def test_get_atten(self):
        self.sim.atten = 0x20
        self.assertTrue(self.ctrl.get_atten())
        self.sim.atten = 0x00
        self.assertFalse(self.ctrl.get_atten())

    def test_get_if_shift(self):
        self.sim.if_shift = 255
        self.assertEqual(self.ctrl.get_if_shift(), 100)

    def test_get_squelch(self):
        self.sim.squelch = 191
        self.assertEqual(self.ctrl.get_squelch(), 75)

    def test_get_rf_power(self):
        self.sim.rf_power = 255
        self.assertEqual(self.ctrl.get_rf_power(), 100)

    def test_get_mic_gain(self):
        self.sim.mic_gain = 128
        self.assertEqual(self.ctrl.get_mic_gain(), 50)

    def test_level_setter_getter_roundtrip(self):
        # A value set through the plugin must read back unchanged via the
        # getter (%→raw→% round-trip through the radio).
        for pct in (0, 25, 50, 75, 100):
            self.assertTrue(self.ctrl.set_rf_power(pct))
            self.assertEqual(self.ctrl.get_rf_power(), pct)

    def test_rx_ctcss_readback(self):
        # 0x1B 0x01 read — regression: the response echoes subcmd 0x01,
        # not 0x00. A mismatched check silently dropped the RX tone.
        self.sim.ctcss_rx_hz = 136.5
        self.ctrl.get_ctcss()
        self.assertAlmostEqual(self.ctrl.ctcss_rx_hz, 136.5, places=1)

    # -- Whole-radio sync --

    def test_poll_settings_pulls_front_panel_change(self):
        # Simulate the operator changing things on the radio itself, then
        # confirm a poll brings the cache in line with the radio.
        self.sim.agc = 0x03
        self.sim.nb_on = True
        self.sim.preamp = 1
        self.sim.atten = 0x20
        self.sim.split = True
        self.sim.rit_on = True
        self.sim.rf_power = 255
        self.sim.mic_gain = 0
        self.sim.dtcs_on = True
        self.ctrl.poll_settings()
        self.assertEqual(self.ctrl.agc, 'slow')
        self.assertTrue(self.ctrl.nb_on)
        self.assertEqual(self.ctrl.preamp, 1)
        self.assertTrue(self.ctrl.atten)
        self.assertTrue(self.ctrl.split)
        self.assertTrue(self.ctrl.rit_on)
        self.assertEqual(self.ctrl.rf_power, 100)
        self.assertEqual(self.ctrl.mic_gain, 0)
        self.assertTrue(self.ctrl.dtcs_on)

    def test_connect_snapshot_is_full(self):
        # _poll_state (run on every connect) must capture the settings
        # group, not just freq/mode — so the GUI matches the radio from
        # the first status frame.
        self.sim.freq_hz = 7_100_000
        self.sim.agc = 0x01
        self.sim.rf_power = 128
        self.ctrl._poll_state()
        self.assertEqual(self.ctrl.freq_hz, 7_100_000)
        self.assertEqual(self.ctrl.agc, 'fast')
        self.assertEqual(self.ctrl.rf_power, 50)


class TestRITHelpers(unittest.TestCase):

    def test_rit_offset_zero(self):
        self.assertEqual(_bcd_to_rit_offset(_rit_offset_to_bcd(0)), 0)

    def test_rit_offset_pos_neg(self):
        for hz in [1, -1, 500, -500, 9999, -9999, 1234, -8765]:
            self.assertEqual(_bcd_to_rit_offset(_rit_offset_to_bcd(hz)), hz)

    def test_pct_bcd3_roundtrip(self):
        for v in [0, 1, 50, 100, 128, 200, 255]:
            self.assertEqual(_bcd3_to_pct(_pct_to_bcd3(v)), v)


# ---------------------------------------------------------------------------
# Tests: IC7100Plugin.execute() dispatch
# ---------------------------------------------------------------------------

class TestIC7100PluginExecute(unittest.TestCase):
    """Tests plugin command dispatch without hardware or audio threads."""

    def setUp(self):
        self.sim = CIVSimulator()
        # Call the REAL __init__ — it is pure assignment; threads and devices
        # are started in setup(), which we never call. Hand-listing "minimal
        # init matching __init__" meant every field added since went missing:
        # _user_cmd_pending was one, and it raised in execute() so 37 tests
        # errored without exercising anything.
        self.plugin = IC7100Plugin()
        self.plugin._civ = _make_patched_controller(self.sim)
        self.plugin._capture = None
        self.plugin._playback = None
        self.plugin._rx_queue = queue.Queue(maxsize=32)
        self.plugin._running = False
        self.plugin._watchdog_thread = None
        self.plugin._poll_thread = None
        self.plugin._rx_gain_db = 0.0
        self.plugin._tx_gain_db = 0.0
        # Permissive baseline so existing TX tests are unaffected; the
        # interlock tests below set this explicitly.
        self.plugin._tx_allow = {'hf': True, 'vu': True}
        self.plugin._status_dirty = False
        self.plugin.status_interval = 2.0
        self.plugin._settings_file = '/tmp/ic7100-test-settings.json'
        self.plugin._audio_device = None

    def tearDown(self):
        self.plugin._civ.disconnect()
        self.sim.stop()

    def test_ptt_on(self):
        r = self.plugin.execute({'cmd': 'ptt', 'state': True})
        self.assertTrue(r['ok'])
        self.assertTrue(self.sim.ptt)

    def test_ptt_off(self):
        self.sim.ptt = True
        r = self.plugin.execute({'cmd': 'ptt', 'state': False})
        self.assertTrue(r['ok'])
        self.assertFalse(self.sim.ptt)

    # -- TX antenna-port safety interlock --

    def test_tx_interlock_blocks_hf(self):
        self.plugin.execute({'cmd': 'frequency', 'freq': 14.200})
        self.plugin._tx_allow = {'hf': False, 'vu': True}
        r = self.plugin.execute({'cmd': 'ptt', 'state': True})
        self.assertFalse(r['ok'])
        self.assertIn('HF', r['error'])
        self.assertFalse(self.sim.ptt)

    def test_tx_interlock_blocks_vu(self):
        self.plugin.execute({'cmd': 'frequency', 'freq': 144.200})
        self.plugin._tx_allow = {'hf': True, 'vu': False}
        r = self.plugin.execute({'cmd': 'ptt', 'state': True})
        self.assertFalse(r['ok'])
        self.assertIn('VHF/UHF', r['error'])
        self.assertFalse(self.sim.ptt)

    def test_tx_interlock_allows_when_enabled(self):
        self.plugin.execute({'cmd': 'frequency', 'freq': 144.200})
        self.plugin._tx_allow = {'hf': False, 'vu': True}
        r = self.plugin.execute({'cmd': 'ptt', 'state': True})
        self.assertTrue(r['ok'])
        self.assertTrue(self.sim.ptt)

    def test_tx_interlock_command_sets_flags(self):
        self.plugin._tx_allow = {'hf': False, 'vu': False}
        r = self.plugin.execute({'cmd': 'tx_interlock', 'hf': True})
        self.assertTrue(r['ok'])
        self.assertTrue(r['tx_allow_hf'])
        self.assertFalse(r['tx_allow_vu'])
        self.assertTrue(self.plugin._tx_allow['hf'])

    def test_tx_interlock_drops_live_ptt(self):
        self.plugin.execute({'cmd': 'frequency', 'freq': 14.200})
        self.plugin._tx_allow = {'hf': True, 'vu': True}
        self.assertTrue(self.plugin.execute({'cmd': 'ptt', 'state': True})['ok'])
        self.assertTrue(self.sim.ptt)
        # Disabling the port we are keyed on must un-key immediately.
        self.plugin.execute({'cmd': 'tx_interlock', 'hf': False})
        self.assertFalse(self.sim.ptt)

    def test_status_exposes_interlock(self):
        self.plugin.execute({'cmd': 'frequency', 'freq': 144.200})
        self.plugin._tx_allow = {'hf': True, 'vu': False}
        s = self.plugin.execute({'cmd': 'status'})['status']
        self.assertTrue(s['tx_allow_hf'])
        self.assertFalse(s['tx_allow_vu'])
        self.assertEqual(s['tx_port'], 'vu')

    def test_frequency(self):
        r = self.plugin.execute({'cmd': 'frequency', 'freq': 144.200})
        self.assertTrue(r['ok'])
        self.assertEqual(self.sim.freq_hz, 144_200_000)

    def test_frequency_missing(self):
        r = self.plugin.execute({'cmd': 'frequency'})
        self.assertFalse(r['ok'])

    def test_mode(self):
        r = self.plugin.execute({'cmd': 'mode', 'mode': 'USB'})
        self.assertTrue(r['ok'])
        self.assertEqual(self.sim.mode, 0x01)

    def test_ctcss_set(self):
        r = self.plugin.execute({'cmd': 'ctcss', 'tx_hz': 88.5, 'tx_on': True})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['ctcss_tx_hz'], 88.5, places=1)
        self.assertTrue(r['ctcss_tx_on'])
        self.assertTrue(self.sim.ctcss_tx_on)

    def test_ctcss_all_params(self):
        r = self.plugin.execute({'cmd': 'ctcss',
                                 'tx_hz': 103.5, 'rx_hz': 103.5,
                                 'tx_on': True,  'rx_on': True})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['ctcss_tx_hz'], 103.5, places=1)
        self.assertAlmostEqual(r['ctcss_rx_hz'], 103.5, places=1)
        self.assertTrue(r['ctcss_tx_on'])
        self.assertTrue(r['ctcss_rx_on'])

    def test_rx_gain(self):
        r = self.plugin.execute({'cmd': 'rx_gain', 'db': 6.0})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(self.plugin._rx_gain_db, 6.0)

    def test_rx_gain_clamp(self):
        r = self.plugin.execute({'cmd': 'rx_gain', 'db': 99.0})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(self.plugin._rx_gain_db, 20.0)

    def test_tx_gain(self):
        r = self.plugin.execute({'cmd': 'tx_gain', 'db': -3.0})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(self.plugin._tx_gain_db, -3.0)

    def test_status(self):
        r = self.plugin.execute({'cmd': 'status'})
        self.assertTrue(r['ok'])
        s = r['status']
        self.assertEqual(s['plugin'], 'ic7100')
        self.assertIn('freq', s)
        self.assertIn('mode', s)
        self.assertIn('ctcss_tx_hz', s)

    def test_unknown_command(self):
        r = self.plugin.execute({'cmd': 'bananas'})
        self.assertFalse(r['ok'])

    def test_invalid_command(self):
        r = self.plugin.execute("not a dict")
        self.assertFalse(r['ok'])

    def test_cat_passthrough(self):
        # Raw read-freq frame: FE FE 88 E0 03 FD
        frame = bytes([0xfe, 0xfe, 0x88, 0xe0, 0x03, 0xfd])
        r = self.plugin.execute({'cmd': 'cat', 'raw': frame.hex()})
        self.assertTrue(r['ok'])
        self.assertIsNotNone(r['response'])

    # -- HF feature dispatch (Phase C) ---------------------------------

    def test_dispatch_split(self):
        r = self.plugin.execute({'cmd': 'split', 'on': True})
        self.assertTrue(r['ok'])
        self.assertTrue(r['split'])

    def test_dispatch_rit_offset_and_enable(self):
        r = self.plugin.execute({'cmd': 'rit', 'hz': 1500, 'on': True})
        self.assertTrue(r['ok'])
        self.assertEqual(r['rit_hz'], 1500)
        self.assertTrue(r['rit_on'])

    def test_dispatch_xit(self):
        r = self.plugin.execute({'cmd': 'xit', 'on': True})
        self.assertTrue(r['ok'])
        self.assertTrue(r['xit_on'])

    def test_dispatch_agc(self):
        r = self.plugin.execute({'cmd': 'agc', 'mode': 'fast'})
        self.assertTrue(r['ok'])
        self.assertEqual(r['agc'], 'fast')

    def test_dispatch_nb(self):
        r = self.plugin.execute({'cmd': 'nb', 'on': True, 'level': 60})
        self.assertTrue(r['ok'])
        self.assertTrue(r['nb_on'])
        self.assertEqual(r['nb_level'], 60)

    def test_dispatch_nr(self):
        r = self.plugin.execute({'cmd': 'nr', 'on': True, 'level': 40})
        self.assertTrue(r['ok'])
        self.assertTrue(r['nr_on'])
        self.assertEqual(r['nr_level'], 40)

    def test_dispatch_preamp(self):
        r = self.plugin.execute({'cmd': 'preamp', 'stage': 1})
        self.assertTrue(r['ok'])
        self.assertEqual(r['preamp'], 1)

    def test_dispatch_atten(self):
        r = self.plugin.execute({'cmd': 'atten', 'on': True})
        self.assertTrue(r['ok'])
        self.assertTrue(r['atten'])

    def test_dispatch_if_shift(self):
        r = self.plugin.execute({'cmd': 'if_shift', 'pct': 70})
        self.assertTrue(r['ok'])
        self.assertEqual(r['if_shift'], 70)

    def test_dispatch_filter(self):
        self.plugin.execute({'cmd': 'mode', 'mode': 'USB'})
        r = self.plugin.execute({'cmd': 'filter', 'idx': 3})
        self.assertTrue(r['ok'])
        self.assertEqual(r['filter'], 3)

    def test_dispatch_squelch(self):
        r = self.plugin.execute({'cmd': 'squelch', 'pct': 55})
        self.assertTrue(r['ok'])
        self.assertEqual(r['squelch'], 55)

    def test_dispatch_squelch_type_noise(self):
        r = self.plugin.execute({'cmd': 'squelch_type', 'type': 'noise'})
        self.assertTrue(r['ok'])
        self.assertFalse(self.sim.ctcss_rx_on)
        self.assertFalse(self.sim.dtcs_on)

    def test_dispatch_squelch_type_tsql(self):
        r = self.plugin.execute({'cmd': 'squelch_type', 'type': 'tsql'})
        self.assertTrue(r['ok'])
        self.assertTrue(self.sim.ctcss_rx_on)
        self.assertFalse(self.sim.dtcs_on)

    def test_dispatch_squelch_type_dtcs(self):
        r = self.plugin.execute({'cmd': 'squelch_type', 'type': 'dtcs'})
        self.assertTrue(r['ok'])
        self.assertFalse(self.sim.ctcss_rx_on)
        self.assertTrue(self.sim.dtcs_on)

    def test_dispatch_squelch_type_bad(self):
        r = self.plugin.execute({'cmd': 'squelch_type', 'type': 'banana'})
        self.assertFalse(r['ok'])

    def test_dispatch_dtcs_code(self):
        r = self.plugin.execute({'cmd': 'dtcs', 'code': 754, 'polarity': 2})
        self.assertTrue(r['ok'])
        self.assertEqual(r['dtcs_code'], 754)
        self.assertEqual(r['dtcs_polarity'], 2)

    def test_status_squelch_type_derived(self):
        self.plugin.execute({'cmd': 'squelch_type', 'type': 'dtcs'})
        s = self.plugin.execute({'cmd': 'status'})['status']
        self.assertEqual(s['squelch_type'], 'dtcs')
        for key in ['dtcs_on', 'dtcs_code', 'dtcs_polarity', 'squelch_type']:
            self.assertIn(key, s)

    def test_status_includes_hf_state(self):
        self.plugin.execute({'cmd': 'split', 'on': True})
        self.plugin.execute({'cmd': 'agc',   'mode': 'slow'})
        r = self.plugin.execute({'cmd': 'status'})
        self.assertTrue(r['ok'])
        s = r['status']
        self.assertTrue(s['split'])
        self.assertEqual(s['agc'], 'slow')
        for key in ['rit_hz','rit_on','xit_on','agc','nb_on','nb_level',
                    'nr_on','nr_level','preamp','atten','if_shift',
                    'squelch','squelch_open']:
            self.assertIn(key, s)


# ---------------------------------------------------------------------------
# Tests: watchdog reconnect logic (no audio)
# ---------------------------------------------------------------------------

class TestWatchdog(unittest.TestCase):

    def test_watchdog_reconnects_on_disconnect(self):
        """Watchdog should re-call connect() when connected goes False."""
        sim = CIVSimulator()
        plugin = IC7100Plugin.__new__(IC7100Plugin)
        plugin._civ = _make_patched_controller(sim)
        plugin._running = True
        plugin._watchdog_thread = None
        plugin._poll_thread = None
        plugin._RECONNECT_DELAY = 1  # fast for test

        connect_calls = []
        original_connect = plugin._civ.connect

        def fake_connect():
            connect_calls.append(1)
            plugin._civ.connected = True
            return True

        plugin._civ.connect = fake_connect

        # Mark disconnected — watchdog should detect and reconnect
        plugin._civ.connected = False

        wt = threading.Thread(target=plugin._watchdog, daemon=True)
        wt.start()
        # RECONNECT_DELAY=1 + watchdog's internal sleep(2) = 3s before connect();
        # wait 4.5s to be safe
        time.sleep(4.5)
        plugin._running = False
        wt.join(timeout=3)

        self.assertGreaterEqual(len(connect_calls), 1,
                                "Watchdog should have called connect()")
        sim.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    unittest.main(verbosity=2)
