"""Gateway-side persistence + config for link endpoints.

Two responsibilities:

1. Parse [kv4p.*] (and similar future [<plugin>.*]) sections from
   gateway_config.txt into per-instance dicts. The flat loader in
   radio_gateway.py:Config skips section headers entirely, so this is a
   second pass that picks up only sectioned keys.

2. Read/write endpoints_state.json — gateway-owned runtime state keyed
   by endpoint name (frequency, ctcss, power, etc.). On endpoint
   connect, the gateway pushes saved state via commands; on status
   updates, the gateway records changes back.
"""

import json
import os
import re
import threading
import time

from config_format import parse_config_value
from config_schema import infer


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_STATE_PATH = os.path.join(_REPO_ROOT, 'endpoints_state.json')
_CONFIG_PATH = os.path.join(_REPO_ROOT, 'gateway_config.txt')


# ── Sectioned config reader ─────────────────────────────────────────

_SECTION_RE = re.compile(r'^\[([^\]]+)\]\s*$')


def read_sections(prefix, config_path=_CONFIG_PATH):
    """Return {instance_name: {key: value}} for all [<prefix>.<instance>] sections.

    Values are read with the flat loader's rules (config_format: quoting and
    inline comments) and typed by config_schema.infer (bool/int/float/str).
    Keys are lowercased so plugins read them with familiar names (e.g. 'port',
    'default_freq'). The bare [<prefix>] section (legacy single-instance) is
    *not* returned here — call migrate_legacy_block() first if you want it
    converted on disk.
    """
    out = {}
    if not os.path.exists(config_path):
        return out
    current = None
    target = f'{prefix}.'
    with open(config_path, 'r') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1).strip().lower()
                if section.startswith(target):
                    instance = section[len(target):]
                    current = instance
                    out.setdefault(current, {})
                else:
                    current = None
                continue
            if current is None or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip().lower()
            value = parse_config_value(value)
            if not value:
                continue
            out[current][key] = infer(value, hex_ok=True)
    return out


# ── Legacy [kv4p] → [kv4p.vhf] one-shot migration ───────────────────

_LEGACY_KV4P_MAP = {
    'KV4P_PORT': 'port',
    'KV4P_FREQ': 'default_freq',
    'KV4P_TX_FREQ': 'tx_freq',
    'KV4P_SQUELCH': 'squelch',
    'KV4P_CTCSS_TX': 'ctcss_tx',
    'KV4P_CTCSS_RX': 'ctcss_rx',
    'KV4P_BANDWIDTH': 'bandwidth',
    'KV4P_HIGH_POWER': 'high_power',
    'KV4P_SMETER': 'smeter',
    'KV4P_AUDIO_DUCK': 'audio_duck',
    'KV4P_AUDIO_PRIORITY': 'audio_priority',
    'KV4P_AUDIO_DISPLAY_GAIN': 'audio_display_gain',
    'KV4P_AUDIO_BOOST': 'audio_boost',
    'KV4P_RECONNECT_INTERVAL': 'reconnect_interval',
    'ENABLE_KV4P': 'enable',
    'KV4P_PROC_ENABLE_NOISE_GATE': 'proc_enable_noise_gate',
    'KV4P_PROC_NOISE_GATE_THRESHOLD': 'proc_noise_gate_threshold',
    'KV4P_PROC_NOISE_GATE_ATTACK': 'proc_noise_gate_attack',
    'KV4P_PROC_NOISE_GATE_RELEASE': 'proc_noise_gate_release',
    'KV4P_PROC_ENABLE_HPF': 'proc_enable_hpf',
    'KV4P_PROC_HPF_CUTOFF': 'proc_hpf_cutoff',
    'KV4P_PROC_ENABLE_LPF': 'proc_enable_lpf',
    'KV4P_PROC_LPF_CUTOFF': 'proc_lpf_cutoff',
    'KV4P_PROC_ENABLE_NOTCH': 'proc_enable_notch',
    'KV4P_PROC_NOTCH_FREQ': 'proc_notch_freq',
    'KV4P_PROC_NOTCH_Q': 'proc_notch_q',
}


def migrate_legacy_kv4p_block(config_path=_CONFIG_PATH, instance='vhf'):
    """Rewrite a legacy [kv4p] block as [kv4p.<instance>] in place.

    No-op if the file is missing, if a [kv4p.<instance>] section already
    exists, or if no [kv4p] block is found. Returns True if the file was
    modified.
    """
    if not os.path.exists(config_path):
        return False
    with open(config_path, 'r') as f:
        lines = f.readlines()

    sections_present = set()
    for raw in lines:
        line = raw.strip()
        m = _SECTION_RE.match(line)
        if m:
            sections_present.add(m.group(1).strip().lower())

    target_section = f'kv4p.{instance}'
    if target_section in sections_present:
        # User has already moved to sectioned config — leave the legacy
        # block alone so they can delete it themselves when comfortable.
        return False
    if 'kv4p' not in sections_present:
        return False

    out = []
    in_block = False
    converted = False
    for raw in lines:
        stripped = raw.strip()
        m = _SECTION_RE.match(stripped)
        if m:
            if m.group(1).strip().lower() == 'kv4p':
                out.append(f'[{target_section}]\n')
                in_block = True
                converted = True
                continue
            in_block = False
            out.append(raw)
            continue
        if in_block and '=' in stripped and not stripped.startswith('#'):
            key, value = raw.split('=', 1)
            new_key = _LEGACY_KV4P_MAP.get(key.strip())
            if new_key:
                # Preserve indentation; rest-of-line (value + any inline comment
                # + trailing newline) is reattached verbatim.
                indent = raw[:len(raw) - len(raw.lstrip())]
                out.append(f'{indent}{new_key} ={value}')
                continue
        out.append(raw)

    if converted:
        backup = config_path + '.legacy_kv4p.bak'
        try:
            if not os.path.exists(backup):
                with open(backup, 'w') as f:
                    f.writelines(lines)
        except OSError:
            pass
        # Atomic replace — this is gateway_config.txt (stream keys,
        # passwords); a crash mid-write must not truncate it.
        tmp = config_path + '.tmp'
        with open(tmp, 'w') as f:
            f.writelines(out)
        os.replace(tmp, config_path)
    return converted


# ── Runtime state (endpoints_state.json) ────────────────────────────

_state_lock = threading.Lock()


def load_state(path=_STATE_PATH):
    """Return dict {endpoint_name: {key: value}}. Empty dict if missing."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"  [EndpointState] load error: {e}")
    return {}


def save_state(state, path=_STATE_PATH):
    """Atomic write of state dict."""
    with _state_lock:
        tmp = f'{path}.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [EndpointState] save error: {e}")


def update_endpoint(name, fields, path=_STATE_PATH):
    """Merge fields into state[name] and persist."""
    state = load_state(path)
    if name not in state:
        state[name] = {}
    if not isinstance(state[name], dict):
        state[name] = {}
    state[name].update(fields)
    state[name]['_updated_at'] = int(time.time())
    save_state(state, path)


def get_endpoint(name, path=_STATE_PATH):
    return load_state(path).get(name, {})


# ── Helpers for kv4p specifically ──────────────────────────────────

_KV4P_RESTORE_KEYS = (
    'frequency', 'tx_frequency', 'squelch', 'ctcss_tx', 'ctcss_rx',
    'bandwidth', 'high_power', 'audio_boost',
)


def build_restore_commands(saved):
    """Translate a saved-state dict into a list of execute() commands.

    Used on endpoint connect to push the gateway-side last-known state back
    to the plugin so freq/CTCSS/power survive restarts.
    """
    cmds = []
    if not saved:
        return cmds
    try:
        freq = float(saved.get('frequency', 0)) if saved.get('frequency') else 0
        tx_freq = float(saved.get('tx_frequency', 0)) if saved.get('tx_frequency') else 0
    except (TypeError, ValueError):
        freq = tx_freq = 0
    if freq > 0:
        cmds.append({'cmd': 'freq', 'frequency': freq, 'tx_frequency': tx_freq})
    if 'squelch' in saved:
        cmds.append({'cmd': 'squelch', 'level': saved['squelch']})
    if 'ctcss_tx' in saved or 'ctcss_rx' in saved:
        c = {'cmd': 'ctcss'}
        if 'ctcss_tx' in saved: c['tx'] = saved['ctcss_tx']
        if 'ctcss_rx' in saved: c['rx'] = saved['ctcss_rx']
        cmds.append(c)
    if 'bandwidth' in saved:
        cmds.append({'cmd': 'bandwidth', 'wide': bool(saved['bandwidth'])})
    if 'high_power' in saved:
        cmds.append({'cmd': 'power', 'high': bool(saved['high_power'])})
    if 'audio_boost' in saved:
        try:
            cmds.append({'cmd': 'boost', 'value': float(saved['audio_boost'])})
        except (TypeError, ValueError):
            pass
    return cmds


def extract_state_from_status(status):
    """Pick out the persistable fields from a plugin status dict.

    Field names here mirror what KV4PPlugin.get_status() emits, but the
    function is plugin-agnostic for any plugin that reports the same keys.
    """
    if not isinstance(status, dict):
        return {}
    out = {}
    for k in _KV4P_RESTORE_KEYS:
        if k in status:
            v = status[k]
            # Strings like '147.435000' get normalised to floats so the JSON
            # is round-trippable without surprises.
            if k in ('frequency', 'tx_frequency'):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
            out[k] = v
    return out
