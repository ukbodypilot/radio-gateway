"""GPS receiver manager — USB serial NMEA or simulated."""

import json
import math
import os
import threading
import time

class GPSManager:
    """USB serial GPS receiver (VK-162 or similar NMEA device).

    Reads NMEA sentences from a serial port, parses position/altitude/
    satellite data, and exposes a status dict for the web UI and MCP.

    Config keys:
        ENABLE_GPS  (bool) — enable this manager
        GPS_PORT    (str)  — serial device, e.g. /dev/ttyACM0
        GPS_BAUD    (int)  — baud rate, default 9600
    """

    POLL_INTERVAL = 0.1  # seconds between serial reads

    def __init__(self, config):
        self.config = config
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Mode tracking
        self._mode = 'simulate' if str(getattr(config, 'GPS_PORT', '')).strip() == 'simulate' else 'serial'

        # Status fields
        self.connected = False
        self.fix = 0           # 0=none, 1=GPS, 2=DGPS
        self.lat = 0.0
        self.lon = 0.0
        self.alt = 0.0         # meters
        self.speed = 0.0       # km/h
        self.heading = 0.0     # degrees true
        self.hdop = 99.9
        self.satellites_used = 0
        self.satellites = []   # [{prn, elevation, azimuth, snr}, ...]
        self.last_fix_time = ''
        self.last_error = ''
        self._gsv_buf = {}     # accumulate multi-sentence GSV

    def start(self):
        if not getattr(self.config, 'ENABLE_GPS', False):
            return
        port = str(getattr(self.config, 'GPS_PORT', '/dev/ttyACM0')).strip()
        if not port:
            print('[GPS] ENABLE_GPS=true but GPS_PORT is empty — not starting')
            return
        self._stop.clear()
        target = self._run_simulate if self._mode == 'simulate' else self._run
        self._thread = threading.Thread(target=target, name='GPSManager', daemon=True)
        self._thread.start()
        label = 'simulated (DM13do)' if self._mode == 'simulate' else port
        print(f'[GPS] Manager started → {label}')

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_status(self):
        port = str(getattr(self.config, 'GPS_PORT', '/dev/ttyACM0'))
        with self._lock:
            return {
                'enabled': True,
                'port': port,
                'simulated': self._mode == 'simulate',
                'connected': self.connected,
                'fix': self.fix,
                'lat': round(self.lat, 6),
                'lon': round(self.lon, 6),
                'alt': round(self.alt, 1),
                'speed': round(self.speed, 1),
                'heading': round(self.heading, 1),
                'hdop': round(self.hdop, 1),
                'satellites_used': self.satellites_used,
                'satellites': list(self.satellites),
                'last_fix_time': self.last_fix_time,
                'last_error': self.last_error,
            }

    def switch_mode(self, mode):
        """Switch between 'simulate' and 'serial' mode without gateway restart.

        Returns (ok, message) tuple.
        """
        if mode not in ('simulate', 'serial'):
            return False, f"Unknown mode: {mode}"
        if mode == self._mode:
            return True, f"Already in {mode} mode"

        # Stop current thread
        self.stop()

        # Reset state
        with self._lock:
            self.connected = False
            self.fix = 0
            self.satellites = []
            self.last_fix_time = ''
            self.last_error = ''

        self._mode = mode
        self._stop.clear()
        target = self._run_simulate if mode == 'simulate' else self._run
        self._thread = threading.Thread(target=target, name='GPSManager', daemon=True)
        self._thread.start()

        label = 'simulated' if mode == 'simulate' else str(getattr(self.config, 'GPS_PORT', '/dev/ttyACM0'))
        print(f'  [GPS] Switched to {label}')
        return True, f"Switched to {mode} mode"

    def set_simulated_position(self, lat=None, lon=None, alt=None, speed=None, heading=None):
        """Update simulated position (only works in simulate mode)."""
        if self._mode != 'simulate':
            return False
        with self._lock:
            if lat is not None:
                self.lat = float(lat)
            if lon is not None:
                self.lon = float(lon)
            if alt is not None:
                self.alt = float(alt)
            if speed is not None:
                self.speed = float(speed)
            if heading is not None:
                self.heading = float(heading)
        return True

    # ------------------------------------------------------------------ internals

    def _run(self):
        import serial
        port = str(getattr(self.config, 'GPS_PORT', '/dev/ttyACM0')).strip()
        baud = int(getattr(self.config, 'GPS_BAUD', 9600))

        while not self._stop.is_set():
            ser = None
            try:
                ser = serial.Serial(port, baud, timeout=1)
                with self._lock:
                    self.connected = True
                    self.last_error = ''
                print(f'  [GPS] Connected to {port} @ {baud}')

                while not self._stop.is_set():
                    try:
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode('ascii', errors='ignore').strip()
                        if not line.startswith('$'):
                            continue
                        if not self._verify_checksum(line):
                            continue
                        # Strip checksum for parsing
                        body = line.split('*')[0][1:]  # remove $ and *XX
                        fields = body.split(',')
                        sid = fields[0]
                        if sid.endswith('GGA'):
                            self._parse_gga(fields)
                        elif sid.endswith('RMC'):
                            self._parse_rmc(fields)
                        elif sid.endswith('GSV'):
                            self._parse_gsv(fields)
                    except Exception:
                        pass

            except Exception as e:
                with self._lock:
                    self.connected = False
                    self.last_error = str(e)
                # Retry every 5s
                self._stop.wait(5)
            finally:
                if ser:
                    try:
                        ser.close()
                    except Exception:
                        pass

    @staticmethod
    def _verify_checksum(line):
        """Verify NMEA checksum: XOR of bytes between $ and *."""
        if '*' not in line:
            return False
        body, chk = line[1:].split('*', 1)
        try:
            expected = int(chk[:2], 16)
        except ValueError:
            return False
        calc = 0
        for c in body:
            calc ^= ord(c)
        return calc == expected

    @staticmethod
    def _nmea_to_decimal(value, direction):
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        if not value:
            return 0.0
        # Find the degree/minute split: degrees are everything before last 2 digits before decimal
        dot = value.index('.')
        deg = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
        result = deg + minutes / 60.0
        if direction in ('S', 'W'):
            result = -result
        return result

    def _parse_gga(self, fields):
        """Parse GGA — fix, position, altitude, HDOP, satellites used."""
        if len(fields) < 15:
            return
        with self._lock:
            try:
                self.fix = int(fields[6]) if fields[6] else 0
                if self.fix > 0:
                    self.lat = self._nmea_to_decimal(fields[2], fields[3])
                    self.lon = self._nmea_to_decimal(fields[4], fields[5])
                    self.satellites_used = int(fields[7]) if fields[7] else 0
                    self.hdop = float(fields[8]) if fields[8] else 99.9
                    self.alt = float(fields[9]) if fields[9] else 0.0
                    t = fields[1]
                    if len(t) >= 6:
                        self.last_fix_time = f"{t[0:2]}:{t[2:4]}:{t[4:6]} UTC"
            except (ValueError, IndexError):
                pass

    def _parse_rmc(self, fields):
        """Parse RMC — speed, heading, date."""
        if len(fields) < 10:
            return
        with self._lock:
            try:
                if fields[2] == 'A':  # valid fix
                    self.lat = self._nmea_to_decimal(fields[3], fields[4])
                    self.lon = self._nmea_to_decimal(fields[5], fields[6])
                    self.speed = float(fields[7]) * 1.852 if fields[7] else 0.0  # knots to km/h
                    self.heading = float(fields[8]) if fields[8] else 0.0
            except (ValueError, IndexError):
                pass

    def _parse_gsv(self, fields):
        """Parse GSV — satellites in view with signal strength.

        GSV comes in multi-sentence groups per constellation: $GPGSV, $GLGSV, $GAGSV...
        Buffer each constellation separately. Merge all constellations whenever the
        last constellation group (by arrival order) completes a full cycle.
        """
        if len(fields) < 4:
            return
        try:
            total_msgs = int(fields[1])
            msg_num = int(fields[2])
            sats = []
            i = 4
            while i + 3 < len(fields):
                prn = int(fields[i]) if fields[i] else 0
                elev = int(fields[i + 1]) if fields[i + 1] else 0
                azim = int(fields[i + 2]) if fields[i + 2] else 0
                snr = int(fields[i + 3]) if fields[i + 3] else 0
                sats.append({'prn': prn, 'elevation': elev, 'azimuth': azim, 'snr': snr})
                i += 4

            key = fields[0]  # e.g. GPGSV, GLGSV, GAGSV
            if msg_num == 1:
                self._gsv_buf[key] = sats
            else:
                self._gsv_buf.setdefault(key, []).extend(sats)

            if msg_num == total_msgs:
                # This constellation is complete — merge all buffered constellations
                all_sats = []
                for k, v in self._gsv_buf.items():
                    all_sats.extend(v)
                all_sats.sort(key=lambda s: s['snr'], reverse=True)
                with self._lock:
                    self.satellites = all_sats
        except (ValueError, IndexError):
            pass

    def _run_simulate(self):
        """Generate fake GPS data for testing (DM13do — Santa Ana, CA)."""
        import random, datetime
        with self._lock:
            self.connected = True
            self.last_error = ''
        print('  [GPS] Simulated receiver active')

        # Fake satellite constellation
        fake_sats = [
            {'prn': 2, 'elevation': 65, 'azimuth': 120, 'snr': 42},
            {'prn': 5, 'elevation': 45, 'azimuth': 210, 'snr': 38},
            {'prn': 7, 'elevation': 30, 'azimuth': 55, 'snr': 35},
            {'prn': 13, 'elevation': 72, 'azimuth': 315, 'snr': 44},
            {'prn': 15, 'elevation': 20, 'azimuth': 170, 'snr': 28},
            {'prn': 20, 'elevation': 55, 'azimuth': 260, 'snr': 40},
            {'prn': 24, 'elevation': 10, 'azimuth': 90, 'snr': 18},
            {'prn': 28, 'elevation': 38, 'azimuth': 340, 'snr': 32},
            {'prn': 30, 'elevation': 5, 'azimuth': 45, 'snr': 12},
            {'prn': 66, 'elevation': 50, 'azimuth': 130, 'snr': 36},  # GLONASS
            {'prn': 72, 'elevation': 25, 'azimuth': 200, 'snr': 22},  # GLONASS
        ]

        # Set initial position
        with self._lock:
            self.fix = 1
            self.lat = 33.7455
            self.lon = -117.8677
            self.alt = 35.0
            self.speed = 0.0
            self.heading = 0.0
            self.hdop = 0.9
            self.satellites_used = 9

        while not self._stop.is_set():
            now = datetime.datetime.utcnow()
            with self._lock:
                self.last_fix_time = now.strftime('%H:%M:%S') + ' UTC'
                self.hdop = 0.9 + random.uniform(0, 0.3)
                # Vary SNR slightly each tick
                self.satellites = []
                for s in fake_sats:
                    self.satellites.append({
                        'prn': s['prn'],
                        'elevation': s['elevation'],
                        'azimuth': s['azimuth'],
                        'snr': max(0, s['snr'] + random.randint(-3, 3)),
                    })
                self.satellites.sort(key=lambda x: x['snr'], reverse=True)
            self._stop.wait(2)

        with self._lock:
            self.connected = False


