"""Repeater database manager — ARD (Amateur Repeater Directory) data.

Downloads per-state JSON from the ARD GitHub repo, caches locally,
and provides GPS-aware proximity queries for the gateway.

Data source: https://github.com/Amateur-Repeater-Directory/ARD-RepeaterList
"""

import json
import math
import os
import threading
import time
import urllib.request

# State bounding boxes: {name: (min_lat, max_lat, min_lon, max_lon)}
# Covers the 50 US states + DC.  Used to decide which state files to fetch
# based on GPS position + search radius.
_STATE_BOUNDS = {
    'Alabama': (30.22, 35.01, -88.47, -84.89),
    'Alaska': (51.21, 71.39, -179.15, -129.98),
    'Arizona': (31.33, 37.00, -114.81, -109.04),
    'Arkansas': (33.00, 36.50, -94.62, -89.64),
    'California': (32.53, 42.01, -124.41, -114.13),
    'Colorado': (36.99, 41.00, -109.06, -102.04),
    'Connecticut': (40.95, 42.05, -73.73, -71.79),
    'Delaware': (38.45, 39.84, -75.79, -75.05),
    'Florida': (24.40, 31.00, -87.63, -80.03),
    'Georgia': (30.36, 35.00, -85.61, -80.84),
    'Hawaii': (18.91, 22.24, -160.25, -154.81),
    'Idaho': (41.99, 49.00, -117.24, -111.04),
    'Illinois': (36.97, 42.51, -91.51, -87.02),
    'Indiana': (37.77, 41.76, -88.10, -84.78),
    'Iowa': (40.38, 43.50, -96.64, -90.14),
    'Kansas': (36.99, 40.00, -102.05, -94.59),
    'Kentucky': (36.50, 39.15, -89.57, -81.96),
    'Louisiana': (28.93, 33.02, -94.04, -88.82),
    'Maine': (43.06, 47.46, -71.08, -66.95),
    'Maryland': (37.91, 39.72, -79.49, -75.05),
    'Massachusetts': (41.24, 42.89, -73.51, -69.93),
    'Michigan': (41.70, 48.26, -90.42, -82.41),
    'Minnesota': (43.50, 49.38, -97.24, -89.49),
    'Mississippi': (30.17, 35.00, -91.66, -88.10),
    'Missouri': (36.00, 40.61, -95.77, -89.10),
    'Montana': (44.36, 49.00, -116.05, -104.04),
    'Nebraska': (39.99, 43.00, -104.05, -95.31),
    'Nevada': (35.00, 42.00, -120.01, -114.04),
    'New Hampshire': (42.70, 45.31, -72.56, -70.70),
    'New Jersey': (38.93, 41.36, -75.56, -73.89),
    'New Mexico': (31.33, 37.00, -109.05, -103.00),
    'New York': (40.50, 45.02, -79.76, -71.86),
    'North Carolina': (33.84, 36.59, -84.32, -75.46),
    'North Dakota': (45.94, 49.00, -104.05, -96.55),
    'Ohio': (38.40, 42.33, -84.82, -80.52),
    'Oklahoma': (33.62, 37.00, -103.00, -94.43),
    'Oregon': (41.99, 46.29, -124.57, -116.46),
    'Pennsylvania': (39.72, 42.27, -80.52, -74.69),
    'Rhode Island': (41.15, 42.02, -71.86, -71.12),
    'South Carolina': (32.05, 35.21, -83.35, -78.54),
    'South Dakota': (42.48, 45.94, -104.06, -96.44),
    'Tennessee': (34.98, 36.68, -90.31, -81.65),
    'Texas': (25.84, 36.50, -106.65, -93.51),
    'Utah': (36.99, 42.00, -114.05, -109.04),
    'Vermont': (42.73, 45.02, -73.44, -71.47),
    'Virginia': (36.54, 39.47, -83.68, -75.24),
    'Washington': (45.54, 49.00, -124.85, -116.92),
    'West Virginia': (37.20, 40.64, -82.64, -77.72),
    'Wisconsin': (42.49, 47.08, -92.89, -86.25),
    'Wyoming': (40.99, 45.01, -111.06, -104.05),
    'District of Columbia': (38.79, 38.99, -77.12, -76.91),
}

# ARD GitHub raw base URL
_ARD_BASE = 'https://raw.githubusercontent.com/Amateur-Repeater-Directory/ARD-RepeaterList/main/States'

# Cache directory
_CACHE_DIR = os.path.expanduser('~/.config/radio-gateway/repeaters')

# ARD states that actually have files (not all 50 are published yet)
_ARD_AVAILABLE = None  # populated on first fetch attempt


class RepeaterManager:
    """GPS-aware repeater database from the Amateur Repeater Directory."""

    CACHE_MAX_AGE = 86400  # re-download after 24h
    DEFAULT_RADIUS = 50    # km

    def __init__(self, config, gps_manager=None):
        self.config = config
        self._gps = gps_manager
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._repeaters = []       # all loaded repeaters
        self._nearby = []          # filtered by distance, cached
        self._loaded_states = []   # which states are loaded
        self._last_lat = 0.0
        self._last_lon = 0.0
        self._last_radius = 0.0
        self._last_refresh = 0
        self.last_error = ''

    def start(self):
        if not getattr(self.config, 'ENABLE_REPEATER_DB', False):
            return
        os.makedirs(_CACHE_DIR, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='RepeaterManager', daemon=True)
        self._thread.start()
        print('[Repeaters] Manager started')

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_status(self):
        with self._lock:
            return {
                'enabled': True,
                'loaded': len(self._repeaters),
                'states': list(self._loaded_states),
                'nearby_count': len(self._nearby),
                'radius_km': float(getattr(self.config, 'REPEATER_RADIUS_KM', self.DEFAULT_RADIUS)),
                'last_refresh': self._last_refresh,
                'last_error': self.last_error,
            }

    def get_nearby(self, lat=None, lon=None, radius_km=None, band=None, operational_only=True):
        """Return repeaters sorted by distance from position.

        Falls back to GPS position if lat/lon not provided.
        """
        if lat is None or lon is None:
            lat, lon = self._get_gps_position()
        if lat == 0.0 and lon == 0.0:
            return []
        if radius_km is None:
            radius_km = float(getattr(self.config, 'REPEATER_RADIUS_KM', self.DEFAULT_RADIUS))

        with self._lock:
            reps = self._repeaters

        results = []
        for r in reps:
            if operational_only and not r.get('isOperational', True):
                continue
            if band and r.get('band', '').lower() != band.lower():
                continue
            d = _haversine(lat, lon, r['latitude'], r['longitude'])
            if d <= radius_km:
                entry = dict(r)
                entry['distance_km'] = round(d, 1)
                results.append(entry)

        results.sort(key=lambda x: x['distance_km'])
        return results

    def get_tune_params(self, callsign, frequency=None):
        """Get radio tune parameters for a specific repeater.

        Returns dict with frequency, input_frequency, offset, offset_sign,
        ctcss_tone — suitable for passing to radio plugins.
        """
        with self._lock:
            for r in self._repeaters:
                if r['callsign'] == callsign:
                    if frequency and abs(r['outputFrequency'] - frequency) > 0.001:
                        continue
                    return {
                        'frequency': r['outputFrequency'],
                        'input_frequency': r['inputFrequency'],
                        'offset': r.get('offset', 0),
                        'offset_sign': r.get('offsetSign', ''),
                        'ctcss_tone': r.get('ctcssTx', 0),
                        'band': r.get('band', ''),
                        'callsign': r['callsign'],
                        'city': r.get('nearestCity', ''),
                    }
        return None

    def refresh(self):
        """Force re-download and reload."""
        self._last_refresh = 0
        self._load_for_position(*self._get_gps_position())

    # ------------------------------------------------------------------ internals

    def _get_gps_position(self):
        """Get current position from GPS manager or config fallback."""
        if self._gps:
            s = self._gps.get_status()
            if s.get('fix', 0) > 0:
                return s['lat'], s['lon']
        # Fallback to config
        lat = float(getattr(self.config, 'AUTOMATION_REPEATER_LAT', 0))
        lon = float(getattr(self.config, 'AUTOMATION_REPEATER_LON', 0))
        return lat, lon

    def _run(self):
        """Background thread: load data on start, refresh periodically."""
        # Initial load
        lat, lon = self._get_gps_position()
        # Wait up to 30s for GPS fix if no position yet
        if lat == 0.0 and lon == 0.0:
            for _ in range(30):
                if self._stop.is_set():
                    return
                lat, lon = self._get_gps_position()
                if lat != 0.0 or lon != 0.0:
                    break
                time.sleep(1)

        if lat != 0.0 or lon != 0.0:
            self._load_for_position(lat, lon)
        else:
            print('  [Repeaters] No GPS position available — waiting')

        # Periodic refresh (every hour, or when position changes significantly)
        while not self._stop.is_set():
            self._stop.wait(300)  # check every 5 min
            if self._stop.is_set():
                break
            lat, lon = self._get_gps_position()
            if lat == 0.0 and lon == 0.0:
                continue
            # Reload if position moved >10km or data is >24h old
            if self._last_lat != 0.0:
                moved = _haversine(lat, lon, self._last_lat, self._last_lon)
                if moved < 10 and (time.time() - self._last_refresh) < self.CACHE_MAX_AGE:
                    continue
            self._load_for_position(lat, lon)

    def _load_for_position(self, lat, lon):
        """Determine needed states, download if needed, load into memory."""
        radius = float(getattr(self.config, 'REPEATER_RADIUS_KM', self.DEFAULT_RADIUS))
        states = self._states_for_position(lat, lon, radius)
        if not states:
            with self._lock:
                self.last_error = 'No US state found for position'
            return

        all_reps = []
        loaded = []
        for state in states:
            reps = self._ensure_state(state)
            if reps is not None:
                all_reps.extend(reps)
                loaded.append(state)

        with self._lock:
            self._repeaters = all_reps
            self._loaded_states = loaded
            self._last_lat = lat
            self._last_lon = lon
            self._last_refresh = time.time()
            self._nearby = []  # invalidate cache
            self.last_error = ''

        count = len(all_reps)
        nearby = sum(1 for r in all_reps
                     if _haversine(lat, lon, r['latitude'], r['longitude']) <= radius)
        print(f'  [Repeaters] Loaded {count} from {", ".join(loaded)} — {nearby} within {radius}km')

    def _states_for_position(self, lat, lon, radius_km):
        """Return list of state names whose bounding box overlaps the search circle."""
        # Convert radius to rough degree buffer (~111km per degree lat)
        buf = radius_km / 111.0
        matches = []
        for state, (min_lat, max_lat, min_lon, max_lon) in _STATE_BOUNDS.items():
            if (lat + buf >= min_lat and lat - buf <= max_lat and
                    lon + buf >= min_lon and lon - buf <= max_lon):
                matches.append(state)
        return matches

    def _ensure_state(self, state):
        """Return repeater list for a state, downloading if cache is stale."""
        cache_file = os.path.join(_CACHE_DIR, f'{state}.json')

        # Use cache if fresh
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < self.CACHE_MAX_AGE:
                return self._load_file(cache_file)

        # Download
        reps = self._download_state(state, cache_file)
        if reps is not None:
            return reps

        # Fall back to stale cache
        if os.path.exists(cache_file):
            return self._load_file(cache_file)
        return None

    def _download_state(self, state, cache_file):
        """Download a state file from ARD GitHub."""
        url = f'{_ARD_BASE}/{state}.json'
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'RadioGateway/2.0 (repeater-manager)'
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(cache_file, 'wb') as f:
                f.write(data)
            reps = json.loads(data)
            if isinstance(reps, list):
                return reps
            return reps.get('repeaters', [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # State file doesn't exist in ARD yet — not an error
                pass
            else:
                with self._lock:
                    self.last_error = f'Download {state}: HTTP {e.code}'
                print(f'  [Repeaters] Download {state} failed: HTTP {e.code}')
        except Exception as e:
            with self._lock:
                self.last_error = f'Download {state}: {e}'
            print(f'  [Repeaters] Download {state} failed: {e}')
        return None

    @staticmethod
    def _load_file(path):
        """Load a cached state JSON file."""
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            return data.get('repeaters', [])
        except Exception:
            return None


def _haversine(lat1, lon1, lat2, lon2):
    """Haversine distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
