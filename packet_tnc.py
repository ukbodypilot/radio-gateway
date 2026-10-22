"""Gateway-side Direwolf TNC.

Previously lived inside `AIOCPlugin` on the endpoint, which made it impossible
to have direwolf and an AIOC on different machines and tangled audio routing
with packet decoding. This module owns Direwolf as a top-level concern,
supervised by the gateway's ProcessSupervisor.

Audio source:
  - Local AIOC (default): direwolf opens the AIOC ALSA device directly via
    `ADEVICE hw:N,0`. AIOCPlugin on the loopback endpoint releases its own
    arecord/aplay handles when packet_radio enters data mode, so direwolf
    has exclusive access. This is the only path validated in this commit.
  - Remote AIOC (flagged, not validated): a future AudioPipe helper will
    pipe the endpoint's already-decoded audio bus into direwolf's stdin
    (`direwolf -r 24000 -B 1200 -`). PTT goes back via the endpoint
    command channel. Not wired in this commit.

KISS / AGW ports stay on the gateway, where packet_radio.py and `pat`
already expect them, so the rest of the packet stack is untouched.
"""

try:
    from log_util import log_exc
except ImportError:   # file used on its own (worker copy, self-test run)
    def log_exc(*_a, **_k):
        pass
import os
import re
import threading
from collections import deque


_DEFAULT_DIREWOLF_PATH = '/usr/bin/direwolf'
_CONF_PATH = '/tmp/direwolf_endpoint.conf'
_AUDIO_LEVEL_RE = re.compile(r'audio level\s*=\s*(\d+)')


class PacketTNC:
    """Direwolf wrapper. One instance per gateway."""

    SUPERVISOR_NAME = 'direwolf'

    def __init__(self, supervisor, direwolf_path=_DEFAULT_DIREWOLF_PATH):
        self._supervisor = supervisor
        self._direwolf_path = direwolf_path
        self._lock = threading.Lock()
        self._log_tail = deque(maxlen=200)
        self._audio_level = 0
        self._audio_peak = 0
        self._kiss_port = 0
        self._callsign = ''
        self._modem = 1200
        self._aioc_hw = None

    # ── Public API ─────────────────────────────────────────────────

    def start(self, callsign, ssid=0, modem=1200, kiss_port=8001,
              ptt_channel=3, aioc_hw=None):
        """Start direwolf with the given TNC parameters.

        aioc_hw: explicit ALSA device (e.g. 'hw:1,0'). None = auto-discover.
        Returns True on launch (supervisor accepted the entry).
        """
        if not os.path.exists(self._direwolf_path):
            print(f"  [TNC] direwolf not found at {self._direwolf_path}")
            return False

        device = aioc_hw or self._discover_aioc_hw()
        if not device:
            print("  [TNC] No AIOC ALSA device found — direwolf cannot start")
            return False
        if device.startswith('plughw:'):
            device = device.replace('plughw:', 'hw:', 1)

        full_call = str(callsign).strip().upper()
        if ssid:
            full_call = full_call.split('-')[0] + f'-{ssid}'

        ptt_line = self._ptt_line(ptt_channel)
        conf = (
            f"ADEVICE {device} {device}\n"
            f"ARATE 48000\n"
            f"ACHANNELS 1\n\n"
            f"CHANNEL 0\n"
            f"MYCALL {full_call}\n"
            f"MODEM {int(modem)}\n\n"
            f"{ptt_line}"
            f"FIX_BITS 1\n\n"
            f"KISSPORT {int(kiss_port)}\n"
            f"AGWPORT 8010\n\n"
            f"DIGIPEAT 0 0 ^WIDE[3-7]-[1-7]$|^TEST$ ^WIDE[12]-[12]$\n"
        )
        try:
            with open(_CONF_PATH, 'w') as f:
                f.write(conf)
        except Exception as e:
            print(f"  [TNC] config write error: {e}")
            return False

        with self._lock:
            self._kiss_port = int(kiss_port)
            self._callsign = full_call
            self._modem = int(modem)
            self._aioc_hw = device

        argv = [self._direwolf_path, '-c', _CONF_PATH, '-t', '0']
        try:
            self._supervisor.add(
                self.SUPERVISOR_NAME, argv,
                restart=True, backoff=(2, 30),
                stdout_handler=self._on_log_line,
            )
            print(f"  [TNC] direwolf supervised (device={device}, "
                  f"KISS={kiss_port}, MYCALL={full_call}, MODEM={modem})")
            return True
        except ValueError:
            # Already registered — restart with the new config
            self._supervisor.restart(self.SUPERVISOR_NAME)
            print(f"  [TNC] direwolf restarted with updated config")
            return True
        except Exception as e:
            print(f"  [TNC] direwolf start failed: {e}")
            return False

    def stop(self):
        """Stop direwolf if running."""
        try:
            self._supervisor.stop(self.SUPERVISOR_NAME)
            print("  [TNC] direwolf stopped")
        except KeyError:
            pass
        except Exception as e:
            print(f"  [TNC] direwolf stop error: {e}")

    def status(self):
        with self._lock:
            running = False
            pid = None
            try:
                s = self._supervisor.status(self.SUPERVISOR_NAME)
                pid = s.get('pid')
                running = bool(pid)
            except KeyError:
                pass
            return {
                'running': running,
                'pid': pid,
                'kiss_port': self._kiss_port,
                'callsign': self._callsign,
                'modem': self._modem,
                'aioc_device': self._aioc_hw,
                'audio_level': self._audio_level,
                'audio_peak': self._audio_peak,
                'log_tail': list(self._log_tail)[-15:],
            }

    @property
    def audio_level(self):
        return self._audio_level

    @property
    def log_tail(self):
        return list(self._log_tail)

    # ── Internals ──────────────────────────────────────────────────

    def _on_log_line(self, line):
        print(f"  [Direwolf] {line}", flush=True)
        self._log_tail.append(line)
        m = _AUDIO_LEVEL_RE.search(line)
        if m:
            try:
                lvl = int(m.group(1))
                self._audio_level = lvl
                if lvl > self._audio_peak:
                    self._audio_peak = lvl
            except ValueError:
                pass

    def _discover_aioc_hw(self):
        try:
            with open('/proc/asound/cards') as f:
                for line in f:
                    if 'AllInOneCable' in line or 'All-In-One' in line:
                        return f'hw:{line.strip().split()[0]},0'
        except Exception:
            log_exc()
        return None

    def _ptt_line(self, ptt_channel):
        """Resolve the AIOC HID for CM108 PTT and return a Direwolf PTT line."""
        try:
            for hid in sorted(os.listdir('/sys/class/hidraw')):
                uevent = f'/sys/class/hidraw/{hid}/device/uevent'
                if not os.path.exists(uevent):
                    continue
                with open(uevent) as f:
                    if '00001209' in f.read():  # AIOC VID
                        path = f'/dev/{hid}'
                        return f'PTT CM108 {path} {int(ptt_channel)}\n'
        except Exception:
            log_exc()
        return ''
