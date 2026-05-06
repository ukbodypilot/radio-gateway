#!/usr/bin/env python3
"""
D75 Link Plugin — TH-D75 Bluetooth radio as a Gateway Link endpoint.

Wraps the existing remote_bt_proxy.py BT classes (SerialManager + AudioManager)
in the RadioPlugin interface so the D75 appears as a standard link endpoint.

Audio: D75 SCO is 8kHz 16-bit mono. The link protocol uses 48kHz.
This plugin resamples between the two rates.

Usage:
    python3 tools/link_endpoint.py --name d75-bt --plugin d75

Requires: the D75 to be paired and trusted via bluetoothctl.
"""

import os
import sys
import struct
import threading
import time
import queue as _queue_mod

# Add scripts/ to path for remote_bt_proxy imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_dir = os.path.dirname(_script_dir)
sys.path.insert(0, os.path.join(_project_dir, 'scripts'))
sys.path.insert(0, _project_dir)

from gateway_link import RadioPlugin

# Import BT classes from remote_bt_proxy
from remote_bt_proxy import (
    SerialManager, AudioManager, ensure_paired,
    AUDIO_FRAME_SIZE, D75_MAC,
)

# SCO runs at 8kHz, link protocol at 48kHz
SCO_RATE = 8000
LINK_RATE = 48000
RESAMPLE_RATIO = LINK_RATE // SCO_RATE  # 6


def _upsample_np(data_8k, prev_last=0.0):
    """Upsample 8kHz 16-bit PCM to 48kHz using numpy interpolation.

    Prepends prev_last from the previous chunk to smooth the boundary.
    Returns (pcm_48k_bytes, new_prev_last).
    """
    import numpy as np
    arr_8k = np.frombuffer(data_8k, dtype=np.int16).astype(np.float32)
    if len(arr_8k) == 0:
        return data_8k, prev_last
    extended = np.concatenate(([prev_last], arr_8k))
    out_len = len(arr_8k) * RESAMPLE_RATIO
    idx = np.linspace(0, len(extended) - 1, out_len).astype(np.float32)
    arr_48k = np.interp(idx, np.arange(len(extended), dtype=np.float32), extended)
    new_prev = float(arr_8k[-1])
    return np.clip(arr_48k, -32768, 32767).astype(np.int16).tobytes(), new_prev


def _downsample(data_48k):
    """Downsample 48kHz 16-bit PCM to 8kHz by decimation."""
    samples = struct.unpack(f'<{len(data_48k) // 2}h', data_48k)
    out = [samples[i] for i in range(0, len(samples), RESAMPLE_RATIO)]
    return struct.pack(f'<{len(out)}h', *out)


class D75Plugin(RadioPlugin):
    """TH-D75 Bluetooth radio plugin for Gateway Link.

    Connects to the D75 via Bluetooth (RFCOMM + SCO) and presents
    duplex audio + CAT control through the link endpoint protocol.
    """

    name = "d75"
    capabilities = {
        "audio_rx": True,
        "audio_tx": True,
        "ptt": True,
        "frequency": True,
        "ctcss": True,
        "power": False,
        "rx_gain": True,
        "tx_gain": True,
        "smeter": False,
        "status": True,
    }

    def __init__(self):
        self._serial = None
        self._audio = None
        self._mac = D75_MAC
        self._rx_queue = _queue_mod.Queue(maxsize=32)
        self._rx_buf = b''
        self._running = False
        self._chunk_size = 4800  # 50ms at 48kHz 16-bit mono
        self._status_dirty = False  # set by execute() to trigger immediate status report
        self.status_interval = 2.0  # D75 has live telemetry (S-meter, freq push)
        self._rx_gain_db = 0.0
        self._tx_gain_db = 0.0
        self._settings_file = os.path.expanduser('~/.config/link-endpoint/d75-settings.json')

    def setup(self, config):
        """Connect to D75 via Bluetooth."""
        mac = config.get('device', '') or self._mac
        if mac and ':' in mac:
            self._mac = mac

        saved = self._load_settings()
        if saved:
            self._rx_gain_db = max(-20, min(20, float(saved.get('rx_gain_db', 0))))
            self._tx_gain_db = max(-20, min(20, float(saved.get('tx_gain_db', 0))))
            print(f"[D75] Restored gains RX={self._rx_gain_db:+.1f} dB TX={self._tx_gain_db:+.1f} dB")

        print(f"[D75] Connecting to {self._mac}...")

        if not ensure_paired(self._mac):
            print(f"[D75] WARNING: {self._mac} may not be paired")

        # Connect CAT serial (RFCOMM ch2)
        self._serial = SerialManager(self._mac)
        if not self._serial.connect():
            raise RuntimeError(f"D75 serial connect failed ({self._mac})")

        # Connect audio (RFCOMM ch1 + SCO)
        self._audio = AudioManager(self._mac)
        # Don't send CKPD — serial is already open (cross-channel issue)
        if not self._audio.connect(send_ckpd=False):
            self._serial.disconnect()
            raise RuntimeError(f"D75 audio connect failed ({self._mac})")

        # Start RX reader that collects SCO frames and resamples to 48kHz
        self._running = True
        self._rx_thread = threading.Thread(
            target=self._rx_reader, daemon=True, name="D75-RX")
        self._rx_thread.start()

        print(f"[D75] Connected — CAT + Audio ready")

    def teardown(self):
        """Disconnect Bluetooth."""
        self._running = False
        if self._audio:
            self._audio.disconnect()
        if self._serial:
            self._serial.disconnect()
        print("[D75] Disconnected")

    def get_audio(self, chunk_size=4800):
        """Get one chunk of 48kHz PCM audio from D75 RX."""
        try:
            data = self._rx_queue.get_nowait()
            if self._rx_gain_db != 0.0:
                data = self._apply_volume(data, self._db_to_linear(self._rx_gain_db))
            return data, False
        except _queue_mod.Empty:
            return None, False

    def put_audio(self, pcm):
        """Send 48kHz PCM audio to D75 TX (downsampled to 8kHz for SCO)."""
        if not self._audio or not self._audio.connected:
            return
        try:
            # Cap TX buffer at 200ms (1600 bytes @ 8kHz) to prevent unbounded growth
            if hasattr(self._audio, '_tx_buf_lock'):
                with self._audio._tx_buf_lock:
                    if len(self._audio._tx_buf) > 1600:
                        return  # drop — buffer full
            if self._tx_gain_db != 0.0:
                pcm = self._apply_volume(pcm, self._db_to_linear(self._tx_gain_db))
            data_8k = _downsample(pcm)
            self._audio.write_sco(data_8k)
        except Exception as e:
            print(f"[D75] TX error: {e}")

    def execute(self, cmd):
        """Handle commands from the gateway."""
        if not isinstance(cmd, dict):
            return {"ok": False, "error": "invalid command"}

        action = cmd.get('cmd', '')

        if action == 'ptt':
            state = cmd.get('state', False)
            if self._serial and self._serial.connected:
                resp = self._serial.send_raw("TX" if state else "RX")
                return {"ok": True, "ptt": state, "response": resp}
            return {"ok": False, "error": "serial not connected"}

        elif action == 'frequency':
            freq = cmd.get('freq')
            band = cmd.get('band', 0)
            if freq and self._serial and self._serial.connected:
                # FQ command: band,freq_in_hz (11 digits, zero-padded)
                freq_hz = int(float(freq) * 1e6)
                resp = self._serial.send_raw(f"FQ {band:01d},{freq_hz:011d}")
                return {"ok": True, "response": resp}
            return {"ok": False, "error": "missing freq or serial not connected"}

        elif action == 'cat':
            # Raw CAT command passthrough
            raw = cmd.get('raw', '')
            if raw and self._serial and self._serial.connected:
                resp = self._serial.send_raw(raw)
                # Update cached state directly from SET commands.
                _parts = raw.split()
                _cmd_code = _parts[0].upper() if _parts else ''
                try:
                    if len(_parts) > 1 and ',' in _parts[1]:
                        # Band,value commands: PC 0,2  MD 0,1  SQ 0,3
                        _args = _parts[1].split(',')
                        _band = int(_args[0])
                        _val = int(_args[1])
                        if 0 <= _band <= 1:
                            if _cmd_code == 'PC':
                                self._serial.band[_band]['power'] = _val
                            elif _cmd_code == 'MD':
                                self._serial.band[_band]['mode'] = _val
                            elif _cmd_code == 'SQ':
                                self._serial.band[_band]['squelch'] = _val
                    elif len(_parts) > 1:
                        # Single-value commands: DL 1  BC 0
                        _val = int(_parts[1])
                        if _cmd_code == 'DL':
                            self._serial.dual_band = _val
                        elif _cmd_code == 'BC':
                            self._serial.active_band = _val
                except (ValueError, IndexError, KeyError):
                    pass
                # Flag that status should be sent immediately
                self._status_dirty = True
                return {"ok": True, "response": resp}
            return {"ok": False, "error": "missing raw command"}

        elif action == 'tone':
            # tone {band} off|tone|ctcss|dcs [{hz_or_code}]
            return self._fo_modify_tone(cmd.get('raw', ''))

        elif action == 'shift':
            # shift {band} {0|1|2}
            return self._fo_modify_shift(cmd.get('raw', ''))

        elif action == 'offset':
            # offset {band} {mhz}
            return self._fo_modify_offset(cmd.get('raw', ''))

        elif action == 'memscan':
            # Scan memory channels and return parsed list
            return self._memscan()

        elif action == 'rx_gain':
            self._rx_gain_db = max(-20, min(20, float(cmd.get('db', 0))))
            self._save_settings()
            print(f"[D75] RX gain set to {self._rx_gain_db:+.1f} dB")
            return {"ok": True, "rx_gain_db": self._rx_gain_db}

        elif action == 'tx_gain':
            self._tx_gain_db = max(-20, min(20, float(cmd.get('db', 0))))
            self._save_settings()
            print(f"[D75] TX gain set to {self._tx_gain_db:+.1f} dB")
            return {"ok": True, "tx_gain_db": self._tx_gain_db}

        elif action == 'status':
            return {"ok": True, "status": self.get_status()}

        return {"ok": False, "error": f"unknown command: {action}"}

    def get_status(self):
        """Return D75 radio state using SerialManager's full to_dict format."""
        status = {"plugin": self.name, "mac": self._mac}
        if self._serial:
            # Use to_dict for complete band data — forward all fields with
            # names matching what the D75 web page expects
            full = self._serial.to_dict()
            status["serial_connected"] = full.get('serial_connected', self._serial.connected)
            status["transmitting"] = full.get('transmitting', False)
            status["model"] = full.get('model_id', '')
            status["serial_number"] = full.get('serial_number', '')
            status["firmware"] = full.get('fw_version', '')
            status["battery_level"] = full.get('battery_level', -1)
            status["bluetooth"] = full.get('bluetooth', False)
            status["active_band"] = full.get('active_band', 0)
            status["dual_band"] = full.get('dual_band', 0)
            # Band dicts with all fields (frequency, power, mode, squelch, etc.)
            status["band"] = [full.get('band_0', {}), full.get('band_1', {})]
        if self._audio:
            status["audio_connected"] = self._audio.connected
        # RX/TX active state
        status["input_active"] = bool(self._audio and self._audio.connected)
        status["output_active"] = bool(self._audio and self._audio.connected)
        # Gain settings
        status["rx_gain_db"] = self._rx_gain_db
        status["tx_gain_db"] = self._tx_gain_db
        # System stats (CPU, RAM, disk, temp)
        status.update(self._get_system_stats())
        return status

    @staticmethod
    def _apply_volume(pcm, gain):
        """Apply a gain multiplier to 16-bit signed LE PCM audio."""
        import struct as _struct
        n = len(pcm) // 2
        samples = _struct.unpack(f'<{n}h', pcm)
        out = []
        for s in samples:
            v = int(s * gain)
            if v > 32767: v = 32767
            elif v < -32768: v = -32768
            out.append(v)
        return _struct.pack(f'<{n}h', *out)

    @staticmethod
    def _db_to_linear(db):
        return 10 ** (db / 20.0)

    def _save_settings(self):
        try:
            import json
            d = os.path.dirname(self._settings_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self._settings_file, 'w') as f:
                json.dump({"rx_gain_db": self._rx_gain_db, "tx_gain_db": self._tx_gain_db}, f)
        except Exception as e:
            print(f"[D75] Failed to save settings: {e}")

    def _load_settings(self):
        try:
            import json
            with open(self._settings_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return None

    @classmethod
    def _get_system_stats(cls):
        """Get CPU, RAM, disk, temp for the endpoint machine."""
        import os, time
        stats = {}
        try:
            with open('/proc/stat') as f:
                parts = f.readline().split()
            vals = [int(v) for v in parts[1:]]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            if not hasattr(cls, '_prev_cpu'):
                cls._prev_cpu = None
            if cls._prev_cpu:
                dt = total - cls._prev_cpu[0]
                di = idle - cls._prev_cpu[1]
                stats['cpu_pct'] = round((1.0 - di / dt) * 100, 1) if dt > 0 else 0.0
            else:
                stats['cpu_pct'] = 0.0
            cls._prev_cpu = (total, idle)
        except Exception:
            pass
        try:
            mem = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    p = line.split()
                    k = p[0].rstrip(':')
                    if k in ('MemTotal', 'MemAvailable'):
                        mem[k] = int(p[1])
            total = mem.get('MemTotal', 0)
            avail = mem.get('MemAvailable', 0)
            stats['ram_pct'] = round(100.0 * (total - avail) / total) if total else 0
            stats['ram_mb'] = (total - avail) // 1024
            stats['ram_total_mb'] = total // 1024
        except Exception:
            pass
        try:
            import shutil
            st = shutil.disk_usage('/')
            stats['disk_pct'] = round(100.0 * st.used / st.total) if st.total else 0
            stats['disk_free_gb'] = round(st.free / (1024**3), 1)
        except Exception:
            pass
        try:
            for zp in sorted(__import__('glob').glob('/sys/class/thermal/thermal_zone*/temp')):
                with open(zp) as f:
                    t = int(f.read().strip()) / 1000
                if t > 0:
                    stats['cpu_temp_c'] = round(t, 1)
                    break
        except Exception:
            pass
        try:
            with open('/proc/net/route') as f:
                for line in f:
                    fields = line.strip().split()
                    if fields[1] == '00000000':
                        stats['net_iface'] = fields[0]
                        break
            if 'net_iface' in stats:
                import subprocess
                out = subprocess.check_output(
                    ['ip', '-4', 'addr', 'show', stats['net_iface']],
                    stderr=subprocess.DEVNULL, timeout=2).decode()
                for line in out.split('\n'):
                    line = line.strip()
                    if line.startswith('inet '):
                        stats['net_ip'] = line.split()[1].split('/')[0]
                        break
        except Exception:
            pass
        return stats

    # -- Internal: memory scan --

    def _memscan(self):
        """Scan D75 memory channels via ME CAT commands. Returns channel list."""
        if not self._serial or not self._serial.connected:
            return {"ok": False, "error": "serial not connected"}
        _modes = {0: 'FM', 1: 'AM', 2: 'LSB', 3: 'USB', 4: 'CW', 5: 'DV'}
        _shifts = {0: 'S', 1: '+', 2: '-'}
        channels = []
        _empty_streak = 0
        for ch_num in range(1000):
            ch_str = str(ch_num).zfill(3)
            resp = self._serial.send_raw(f"ME {ch_str}")
            if not resp or ',' not in str(resp):
                _empty_streak += 1
                if _empty_streak >= 5:
                    break
                continue
            me_line = ''
            for line in str(resp).split('\n'):
                line = line.strip()
                if line.startswith('ME') and ',' in line:
                    me_line = line[3:] if line.startswith('ME ') else line[2:]
                    break
            if not me_line:
                _empty_streak += 1
                if _empty_streak >= 5:
                    break
                continue
            fields = me_line.split(',')
            if len(fields) < 14:
                continue
            try:
                freq_hz = int(fields[1])
                if freq_hz < 1000000:
                    continue
                freq = freq_hz / 1_000_000
                field2 = int(fields[2])
                mode = int(fields[5])
                tone_on = fields[8] == '1'
                ctcss_on = fields[9] == '1'
                dcs_on = fields[10] == '1'
                shift = int(fields[13])
                if field2 >= 100_000_000:
                    tx_freq = field2 / 1_000_000
                    diff = tx_freq - freq
                    if abs(diff) < 0.001:
                        shift_str = 'S'; offset_str = ''
                    elif abs(diff) > 50:
                        shift_str = 'X'; offset_str = f'{tx_freq:.4f}'
                    elif diff > 0:
                        shift_str = '+'; offset_str = f'{diff:.4f}'
                    else:
                        shift_str = '-'; offset_str = f'{abs(diff):.4f}'
                elif field2 > 0 and shift != 0:
                    offset_mhz = field2 / 1_000_000
                    shift_str = '+' if shift == 1 else '-'
                    offset_str = f'{offset_mhz:.4f}'
                else:
                    shift_str = 'S'; offset_str = ''
                tone_str = ''
                tone_idx = int(fields[15])
                ctcss_idx = int(fields[16])
                if ctcss_on:
                    if ctcss_idx < len(self._CTCSS): tone_str = self._CTCSS[ctcss_idx]
                elif tone_on:
                    idx = ctcss_idx if tone_idx == 0 and ctcss_idx > 0 else tone_idx
                    if idx < len(self._CTCSS): tone_str = self._CTCSS[idx]
                elif dcs_on:
                    idx = int(fields[17])
                    if idx < len(self._DCS): tone_str = 'D' + self._DCS[idx]
                name = fields[20].strip() if len(fields) > 20 else ''
                power = int(fields[21]) if len(fields) > 21 and fields[21].strip().isdigit() else -1
                channels.append({
                    'ch': ch_str, 'freq': round(freq, 4),
                    'offset': offset_str, 'mode': _modes.get(mode, '?'),
                    'shift': shift_str, 'tone': tone_str, 'name': name,
                    'me_fields': ','.join(fields[1:14] + fields[15:22]) if len(fields) >= 22 else '',
                    'power': power,
                })
                _empty_streak = 0
            except (ValueError, IndexError):
                continue
        return {"ok": True, "channels": channels}

    # -- Internal: FO modify helpers --

    _CTCSS = [
        "67.0","69.3","71.9","74.4","77.0","79.7","82.5","85.4","88.5",
        "91.5","94.8","97.4","100.0","103.5","107.2","110.9","114.8","118.8","123.0",
        "127.3","131.8","136.5","141.3","146.2","151.4","156.7","162.2","167.9",
        "173.8","179.9","186.2","192.8","203.5","206.5","210.7","218.1","225.7",
        "229.1","233.6","241.8","250.3","254.1"]

    _DCS = ["023","025","026","031","032","036","043","047","051","053","054",
        "065","071","072","073","074","114","115","116","122","125","131",
        "132","134","143","145","152","155","156","162","165","172","174",
        "205","212","223","225","226","243","244","245","246","251","252",
        "255","261","263","265","266","271","274","306","311","315","325",
        "331","332","343","346","351","356","364","365","371","411","412",
        "413","423","431","432","445","446","452","454","455","462","464",
        "465","466","503","506","516","523","526","532","546","565","606",
        "612","624","627","631","632","654","662","664","703","712","723",
        "731","732","734","743","754"]

    def _fo_read(self, band):
        """Read FO for a band, return field list or None."""
        resp = self._serial.send_raw(f"FO {band}")
        if resp and resp.startswith('FO') and resp.count(',') >= 10:
            return resp.split(',')
        return None

    def _fo_write(self, fp, band):
        """Write FO fields back and trigger readback + status update."""
        r = self._serial.send_raw(','.join(fp))
        # Immediate readback — updates cached state synchronously
        time.sleep(0.1)
        self._serial.send_raw(f"FO {band}")
        self._status_dirty = True
        return {"ok": True, "response": r or 'ok'}

    def _fo_modify_tone(self, raw):
        """Handle: tone {band} off|tone|ctcss|dcs [{hz_or_code}]"""
        parts = (raw or '').split()
        if len(parts) < 2:
            return {"ok": False, "error": "usage: tone {band} off|tone|ctcss|dcs [{hz}]"}
        band = int(parts[0])
        ttype = parts[1].lower()
        fp = self._fo_read(band)
        if not fp:
            return {"ok": False, "error": "FO read failed"}
        # Clear all tone flags
        fp[8] = '0'; fp[9] = '0'; fp[10] = '0'
        hz = ''
        code = ''
        if ttype == 'off':
            pass
        elif ttype in ('tone', 'ctcss'):
            hz = parts[2] if len(parts) > 2 else ''
            if hz not in self._CTCSS:
                return {"ok": False, "error": f"unknown CTCSS: {hz}"}
            idx = self._CTCSS.index(hz)
            if ttype == 'tone':
                fp[8] = '1'; fp[14] = f'{idx:02d}'
            else:
                fp[9] = '1'; fp[15] = f'{idx:02d}'; fp[14] = f'{idx:02d}'
        elif ttype == 'dcs':
            code = parts[2] if len(parts) > 2 else ''
            if code not in self._DCS:
                return {"ok": False, "error": f"unknown DCS: {code}"}
            fp[10] = '1'; fp[16] = f'{self._DCS.index(code):03d}'
        else:
            return {"ok": False, "error": f"unknown tone type: {ttype}"}
        result = self._fo_write(fp, band)
        # Directly update cached freq_info (don't rely on FO readback parsing)
        if result.get('ok') and 0 <= band <= 1:
            fi = self._serial.band[band].get('freq_info', {})
            fi['tone_status'] = (ttype == 'tone')
            fi['ctcss_status'] = (ttype == 'ctcss')
            fi['dcs_status'] = (ttype == 'dcs')
            if ttype == 'tone':
                fi['tone_hz'] = hz
            elif ttype == 'ctcss':
                fi['ctcss_hz'] = hz
            elif ttype == 'dcs':
                fi['dcs_code'] = code
            self._serial.band[band]['freq_info'] = fi
        return result

    def _fo_modify_shift(self, raw):
        """Handle: shift {band} {0=simplex|1=plus|2=minus}"""
        parts = (raw or '').split()
        if len(parts) < 2:
            return {"ok": False, "error": "usage: shift {band} 0|1|2"}
        band = int(parts[0])
        direction = parts[1].strip()
        fp = self._fo_read(band)
        if not fp:
            return {"ok": False, "error": "FO read failed"}
        fp[13] = direction
        result = self._fo_write(fp, band)
        if result.get('ok') and 0 <= band <= 1:
            fi = self._serial.band[band].get('freq_info', {})
            fi['shift_direction'] = direction
            self._serial.band[band]['freq_info'] = fi
        return result

    def _fo_modify_offset(self, raw):
        """Handle: offset {band} {mhz}"""
        parts = (raw or '').split()
        if len(parts) < 2:
            return {"ok": False, "error": "usage: offset {band} {mhz}"}
        band = int(parts[0])
        offset_mhz = float(parts[1])
        fp = self._fo_read(band)
        if not fp:
            return {"ok": False, "error": "FO read failed"}
        fp[2] = f'{int(offset_mhz * 1_000_000):010d}'
        result = self._fo_write(fp, band)
        if result.get('ok') and 0 <= band <= 1:
            fi = self._serial.band[band].get('freq_info', {})
            fi['offset'] = f'{offset_mhz:.4f}'
            self._serial.band[band]['freq_info'] = fi
        return result

    # -- Internal: audio --

    def _rx_reader(self):
        """Collect SCO frames from AudioManager, resample to 48kHz, queue chunks.

        SCO delivers 48-byte frames (24 samples @ 8kHz = 3ms each).
        We accumulate into a buffer, resample when we have enough for one
        48kHz chunk (50ms = 800 samples @ 8kHz = 1600 bytes).
        """
        # 50ms of 8kHz audio = 400 samples = 800 bytes
        chunk_8k_size = (self._chunk_size // 2) // RESAMPLE_RATIO * 2  # 800 bytes

        # Tap into the SCO read by registering ourselves as a "client"
        # AudioManager broadcasts to TCP clients — we use a socketpair as a pipe
        import socket
        r_sock, w_sock = socket.socketpair()
        self._audio.add_stream_client(w_sock)

        _rx_bytes = 0
        _rx_chunks = 0
        _rx_diag = time.time()
        _prev_last = 0.0  # carry-over sample for smooth chunk boundaries
        try:
            while self._running:
                try:
                    data = r_sock.recv(4096)
                    if not data:
                        break
                    _rx_bytes += len(data)
                    self._rx_buf += data

                    # Process complete chunks
                    while len(self._rx_buf) >= chunk_8k_size:
                        chunk_8k = self._rx_buf[:chunk_8k_size]
                        self._rx_buf = self._rx_buf[chunk_8k_size:]

                        # Upsample 8kHz → 48kHz (with cross-chunk interpolation)
                        chunk_48k, _prev_last = _upsample_np(chunk_8k, _prev_last)

                        _rx_chunks += 1
                        # DIAG with level
                        if time.time() - _rx_diag > 10.0:
                            import struct as _st
                            _samples = _st.unpack(f'<{len(chunk_48k)//2}h', chunk_48k)
                            _rms = (sum(s*s for s in _samples) / len(_samples)) ** 0.5 if _samples else 0
                            _db = 20 * __import__('math').log10(_rms / 32767.0) if _rms > 0 else -100
                            print(f"[D75-RX] bytes={_rx_bytes} chunks={_rx_chunks} q={self._rx_queue.qsize()} rms={_rms:.0f} db={_db:.1f}")
                            _rx_diag = time.time()
                        # Queue for get_audio()
                        try:
                            self._rx_queue.put_nowait(chunk_48k)
                        except _queue_mod.Full:
                            try:
                                self._rx_queue.get_nowait()
                            except _queue_mod.Empty:
                                pass
                            try:
                                self._rx_queue.put_nowait(chunk_48k)
                            except _queue_mod.Full:
                                pass
                except Exception as e:
                    if self._running:
                        print(f"[D75] RX reader error: {e}")
                    break
        finally:
            r_sock.close()
