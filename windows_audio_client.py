#!/usr/bin/env python3
"""Windows Audio Client for Radio Gateway — Full Duplex.

Runs both directions simultaneously:
  TX: Captures audio from a local input device and sends it to the gateway
      via TCP (connects out to gateway's REMOTE_AUDIO_RX_PORT, default 9602).
  RX: Listens on a local port for the gateway to connect in and push audio,
      then plays it on a local output device (gateway connects from port 9600).

Protocol: length-prefixed PCM — [4-byte big-endian uint32 length][PCM payload]
Audio: 48000 Hz, mono, 16-bit signed little-endian PCM, 2400 frames per chunk.

Three audio devices are selected at startup (saved to the JSON config):
  1) TX PROGRAM FEED — input continuously sent to the gateway (e.g. a virtual
     cable carrying radio/program audio).
  2) RX SPEAKER      — where audio received from the gateway is played.
  3) OPERATOR MIC    — your voice, mixed on top of the program feed only while
     you hold the talk key. The program ducks under your voice (broadcast style).

Voice effects: the operator mic can be run through real-time DSP effects
(chipmunk, deep/monster, robot, alien, echo, cathedral, telephone, megaphone,
bitcrush, …) applied in-process — no external app. Cycle effects with n/m and
toggle on/off with v. All effects are stateful/click-free across chunks.

Keyboard controls (only act while this window is focused):
  MIC (your voice):
    SPACE  = talk on/off (tap to start, tap again to stop — latching)
    - / =  = mic gain down / up 10% (0–1000%; boosts the quiet mic)
    v      = voice effect on / off
    n / m  = previous / next voice effect
  TX (program feed):
    l      = on / mute
    < / >  = volume down / up 5%
    ; / '  = duck depth down / up 5% (how far the program dips while you talk)
    r      = resample on/off (diagnostic)
    w      = record the outgoing mix to a WAV file
  RX (speaker):
    p      = play / mute
    [ / ]  = volume down / up 5%
  DEVICES (swap a device on the fly — no restart, audio keeps running):
    i      = pick the TX program feed input
    k      = pick the operator mic input
    o      = pick the RX speaker output
  d        = toggle diagnostics    Ctrl+C = quit

Self-update: on startup the client asks the gateway whether it is running the
current code and, if not, downloads and relaunches itself. Pass --no-update to
skip the check (the relaunch adds it automatically so an update can't loop).

Usage:
    pip install sounddevice numpy keyboard
    python windows_audio_client.py [gateway_host] [--no-update]

On first run the script will prompt for audio devices and gateway host,
then save the selection to windows_audio_client.json alongside this script.
"""

import json
import math
import os
import socket
import struct
import sys
import threading
import time
import wave
from datetime import datetime

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice is required.  Install it with:  python -m pip install sounddevice")
    sys.exit(1)

import numpy as np

# Optional resampler backends (used only when capture device's native rate != 48k)
_RESAMPLER = None
try:
    import soxr as _soxr  # best: stateful streaming resampler
    _RESAMPLER = "soxr"
except ImportError:
    try:
        from scipy import signal as _scipy_signal  # fallback: per-chunk polyphase
        _RESAMPLER = "scipy"
    except ImportError:
        _RESAMPLER = None

# ---------------------------------------------------------------------------
# Constants — must match gateway defaults
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48000
CHANNELS = 1
FRAMES_PER_BUFFER = 2400  # 2400 frames x 2 bytes = 4800 bytes per chunk
RECONNECT_INTERVAL = 5  # seconds between connection attempts
SILENCE = b'\x00' * (FRAMES_PER_BUFFER * 2)  # 4800 bytes of silence

DEFAULT_TX_PORT = 9602  # Gateway's RX listen port (we connect out to this)
DEFAULT_RX_PORT = 9600  # Local listen port (gateway connects in on this)

MIC_GAIN_MAX = 1000  # max mic gain in percent (10x boost) — mics need preamp gain

# Latency tuning (overridable in the JSON config). AUDIO_LATENCY is PortAudio's
# suggested latency: "low" | "high" | a float in seconds. TX_BLOCK_MS is the
# mic/program capture+mix block — smaller = less latency, but too small risks
# xruns/glitches on slow machines or high-latency host APIs (MME). The RX
# playback block stays at FRAMES_PER_BUFFER for gateway chunk compatibility.
AUDIO_LATENCY = "low"
TX_BLOCK_MS = 20

# ANSI colors
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

CONFIG_FILENAME = "windows_audio_client.json"

# Self-update: the gateway's web server (WEB_CONFIG_PORT, default 8080) serves
# /api/winclient/version and /api/winclient/files. Overridable in the JSON
# config as "web_port" / "update_enabled".
DEFAULT_WEB_PORT = 8080
UPDATE_FILES = ["windows_audio_client.py"]

# Env var the parent uses to tell the relaunched child what it just did. The
# child runs with --no-update so it never repeats the check, which would
# otherwise leave it with nothing to show on the dashboard.
UPDATE_ENV = "WAC_UPDATED_TO"

# What the startup update check concluded, surfaced on the dashboard.
# status: current | updated | failed | skipped | disabled | unknown
UPDATE_STATE = {"version": "", "status": "unknown", "detail": ""}

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
def _config_path():
    """Path of the JSON config file, next to this script."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config():
    """Load the saved config. Returns {} if the file is missing or unreadable."""
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    """Write *cfg* to the config file."""
    path = _config_path()
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Keyboard input (cross-platform)
# ---------------------------------------------------------------------------
def _keyboard_listener(state):
    """Read single keypresses from the console and update shared state.

    Input comes from the CONSOLE INPUT buffer (msvcrt on Windows), which only
    delivers keystrokes while THIS terminal window/tab is focused — so the
    hotkeys never fire system-wide or leak in from another terminal window.
    No global hook, no fragile focus detection. Trade-off: the console cannot
    report key release, so SPACE is a tap-on / tap-off talk toggle.
    """
    _keyboard_listener_console(state)



def _keyboard_listener_console(state):
    """Read single keypresses from the console input buffer (focus-local).
    No key-release detection, so the transmit key (SPACE) is a talk toggle."""
    try:
        # Windows
        import msvcrt
        while state["running"]:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                try:
                    ch = ch.decode("utf-8", errors="ignore").lower()
                except Exception:
                    ch = ""
                _handle_key(ch, state)
            time.sleep(0.05)
    except ImportError:
        # Unix / Linux / macOS
        import tty
        import termios
        import select
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while state["running"]:
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    ch = sys.stdin.read(1).lower()
                    _handle_key(ch, state)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _handle_key(ch, state):
    """Act on one keypress. See the module docstring for the key map.

    While a device picker is open every key goes to the picker instead.
    """
    # The picker is modal: while it is open it swallows every key, so a stray
    # keystroke can't mute TX or step the voice effect behind the menu.
    if state.get("picker") is not None:
        _picker_key(ch, state)
        return
    if ch == "l":
        state["tx_live"] = not state["tx_live"]
    elif ch == "p":
        state["rx_play"] = not state["rx_play"]
    elif ch in (",", "<"):
        state["tx_vol"] = max(0, state["tx_vol"] - 5)
    elif ch in (".", ">"):
        state["tx_vol"] = min(100, state["tx_vol"] + 5)
    elif ch == "[":
        state["rx_vol"] = max(0, state["rx_vol"] - 5)
    elif ch == "]":
        state["rx_vol"] = min(100, state["rx_vol"] + 5)
    elif ch == "r":
        state["tx_resample_enabled"] = not state.get("tx_resample_enabled", True)
    elif ch == "w":
        # toggle WAV dump of the post-processed (48 kHz mono) TX stream
        state["tx_wav_request"] = not state.get("tx_wav_request", False)
    elif ch == "d":
        # toggle expanded diagnostics
        state["show_diag"] = not state.get("show_diag", False)
    # --- on-the-fly device switching ---------------------------------------
    elif ch == "i":
        open_picker(state, "tx")
    elif ch == "k":
        open_picker(state, "mic")
    elif ch == "o":
        open_picker(state, "out")
    # --- mic / broadcast-mix controls --------------------------------------
    # Mic level is a GAIN that can boost above 100% (mics are quiet; the program
    # feed is near full-scale). Range 0–1000% in 10% steps; OS key-repeat ramps
    # it quickly when the key is held.
    elif ch == "-":
        state["mic_vol"] = max(0, state.get("mic_vol", 100) - 10)
    elif ch in ("=", "+"):
        state["mic_vol"] = min(MIC_GAIN_MAX, state.get("mic_vol", 100) + 10)
    elif ch == ";":
        state["duck_pct"] = max(0, state.get("duck_pct", 25) - 5)
    elif ch in ("'", '"'):
        state["duck_pct"] = min(100, state.get("duck_pct", 25) + 5)
    elif ch == "v":
        # toggle the voice effect on/off. If it's currently off and no effect is
        # selected (still on the Raw slot), jump to the first real effect so 'v'
        # actually turns a voice on rather than enabling an empty slot.
        on = state.get("effected", False) and state.get("effect_index", 0) != 0
        if on:
            state["effected"] = False
        else:
            if state.get("effect_index", 0) == 0:
                state["effect_index"] = 1
                state["effect_name"] = effect_name(1)
            state["effected"] = True
    elif ch == "n":
        i = (state.get("effect_index", 0) - 1) % len(EFFECTS)
        state["effect_index"] = i
        state["effect_name"] = effect_name(i)
        state["effected"] = (i != 0)
    elif ch == "m":
        i = (state.get("effect_index", 0) + 1) % len(EFFECTS)
        state["effect_index"] = i
        state["effect_name"] = effect_name(i)
        state["effected"] = (i != 0)
    elif ch == " ":
        # SPACE is a latching talk toggle (console input can't report key-up).
        state["ptt_held"] = not state.get("ptt_held", False)

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
def list_input_devices():
    """Return (index, name, channels) for every device that can capture."""
    devices = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_input_channels"]))
    return devices


def list_output_devices():
    """Return (index, name, channels) for every device that can play."""
    devices = []
    for d in sd.query_devices():
        if d["max_output_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_output_channels"]))
    return devices


def find_device_by_name(name, output=False):
    """Return the index of the input (or output) device named *name*, or None."""
    for d in sd.query_devices():
        if output:
            if d["max_output_channels"] > 0 and d["name"] == name:
                return d["index"]
        else:
            if d["max_input_channels"] > 0 and d["name"] == name:
                return d["index"]
    return None


def choose_input_device(cfg):
    """Pick the TX program-feed input: the saved one if it still exists,
    otherwise ask on the console. Returns (index, name).
    """
    saved_name = cfg.get("tx_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=False)
        if idx is not None:
            return idx, saved_name
        print(f"Saved input device not found: {saved_name}")

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        sys.exit(1)

    print("\n[1/3] TX PROGRAM FEED — the input sent to the gateway continuously")
    print("      (e.g. a virtual audio cable carrying your radio / program audio).")
    print("      Available input devices:")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nProgram feed device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


def choose_mic_device(cfg):
    """Pick the operator microphone (a second input device, separate from the
    program feed). Persisted under "mic_device_name"."""
    saved_name = cfg.get("mic_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=False)
        if idx is not None:
            return idx, saved_name
        print(f"Saved mic device not found: {saved_name}")

    devices = list_input_devices()
    if not devices:
        print("No input devices found for the mic.")
        sys.exit(1)

    print("\n[3/3] OPERATOR MICROPHONE — your voice, mixed on top of the program")
    print("      feed only while you hold the talk key (SPACE). The program ducks")
    print("      under your voice. Available input devices:")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nMic device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


def choose_output_device(cfg):
    """Pick the RX speaker output: the saved one if it still exists,
    otherwise ask on the console. Returns (index, name).
    """
    saved_name = cfg.get("rx_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=True)
        if idx is not None:
            return idx, saved_name
        print(f"Saved output device not found: {saved_name}")

    devices = list_output_devices()
    if not devices:
        print("No output devices found.")
        sys.exit(1)

    print("\n[2/3] RX SPEAKER — where audio received from the gateway is played.")
    print("      Available output devices:")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nSpeaker device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")

# ---------------------------------------------------------------------------
# Self-update — pull newer client code from the gateway on startup
# ---------------------------------------------------------------------------
# Mirrors what the Linux link endpoints already do (tools/link_endpoint.py):
# hash the local files, ask the gateway for its hash, and if they differ pull
# the bundle and relaunch. The hash must be computed over UPDATE_FILES in the
# same order the gateway uses in _WINCLIENT_FILES, or the two can never match
# and every startup re-downloads.

def _local_version():
    """sha256 over the local copies of UPDATE_FILES, first 16 hex chars."""
    import hashlib
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in UPDATE_FILES:
        path = os.path.join(here, fname)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:16]


def check_for_update(gateway_host, web_port):
    """Ask the gateway for newer client code; write it if there is any.

    Returns True when something changed and the caller should relaunch. Any
    failure (gateway down, no web server, bad JSON) returns False — a failed
    update check must never stop the client from starting.
    """
    import base64
    import urllib.request

    base = f"http://{gateway_host}:{web_port}"
    try:
        local = _local_version()
        with urllib.request.urlopen(f"{base}/api/winclient/version", timeout=10) as r:
            remote = json.loads(r.read())
        remote_ver = remote.get("version", "")
        if not remote_ver:
            print("  [update] gateway returned no version — skipping")
            UPDATE_STATE.update(status="failed", detail="gateway sent no version")
            return False
        if remote_ver == local:
            print(f"  [update] up to date (v={local})")
            UPDATE_STATE.update(status="current", detail="")
            return False

        print(f"  [update] new version available: local={local} remote={remote_ver}")
        with urllib.request.urlopen(f"{base}/api/winclient/files", timeout=60) as r:
            bundle = json.loads(r.read())

        here = os.path.dirname(os.path.abspath(__file__))
        updated = 0
        for fname in UPDATE_FILES:
            b64 = bundle.get(fname)
            if not b64:
                continue
            content = base64.b64decode(b64)
            path = os.path.join(here, fname)
            # Compare content, not just the version hash: this is what actually
            # gates the write, so a stale hash can't cause a pointless relaunch.
            try:
                with open(path, "rb") as f:
                    if f.read() == content:
                        continue
            except FileNotFoundError:
                pass
            with open(path, "wb") as f:
                f.write(content)
            updated += 1
            print(f"  [update] updated {fname} ({len(content)} bytes)")

        if not updated:
            print("  [update] files unchanged despite version mismatch")
            UPDATE_STATE.update(status="current",
                                detail="version mismatch, files identical")
            return False
        UPDATE_STATE.update(version=remote_ver, status="updated", detail="")
        return True
    except Exception as e:
        print(f"  [update] check failed: {type(e).__name__}: {e} — starting anyway")
        UPDATE_STATE.update(status="failed", detail=f"{type(e).__name__}: {e}")
        return False


def verify_version_only(gateway_host, web_port):
    """Ask the gateway for its version and compare — but never download or
    relaunch. Used on the --no-update path so the dashboard can still tell the
    truth about whether this code is current.

    This is what stops the status line depending on the parent process having
    cooperated: a client relaunched by an older build (which knew nothing about
    UPDATE_ENV) can still confirm for itself that it is up to date.
    """
    import urllib.request
    try:
        local = _local_version()
        with urllib.request.urlopen(
                f"http://{gateway_host}:{web_port}/api/winclient/version",
                timeout=10) as r:
            remote = json.loads(r.read()).get("version", "")
        if not remote:
            UPDATE_STATE.update(status="failed", detail="gateway sent no version")
        elif remote == local:
            UPDATE_STATE.update(status="current", detail="")
        else:
            UPDATE_STATE.update(status="behind", detail=f"gateway has {remote}")
    except Exception as e:
        UPDATE_STATE.update(status="failed", detail=f"{type(e).__name__}: {e}")


def relaunch():
    """Restart this script with the same arguments plus --no-update.

    os.execv on Windows is not a real exec — it spawns and lets the parent die,
    which hands control back to the shell early and leaves the console in a bad
    state. This client's whole UI is ANSI + msvcrt console input, so spawn a
    child that inherits the console and exit cleanly instead.
    """
    argv = [a for a in sys.argv[1:] if a != "--no-update"] + ["--no-update"]
    cmd = [sys.executable, os.path.abspath(__file__)] + argv
    print(f"  [update] relaunching: {' '.join(cmd)}\n")
    sys.stdout.flush()
    # Passed by env rather than argv so it can't collide with the gateway-host
    # positional or end up saved into the config.
    env = dict(os.environ)
    env[UPDATE_ENV] = UPDATE_STATE.get("version", "") or _local_version()
    try:
        import subprocess
        subprocess.Popen(cmd, close_fds=False, env=env)
    except Exception as e:
        print(f"  [update] relaunch failed: {e}")
        print("  Restart the client manually to pick up the new version.")
        try:
            input("\nPress Enter to exit ")
        except Exception:
            pass
    sys.exit(0)

def update_status_line():
    """One coloured line describing the code version and the update check.

    Always shows the version actually running (hashed from disk), so it stays
    truthful even when the check never ran or failed.
    """
    ver = UPDATE_STATE.get("version") or "?"
    status = UPDATE_STATE.get("status", "unknown")
    detail = UPDATE_STATE.get("detail", "")
    if status == "current":
        tag = f"{GREEN}latest{RESET}"
    elif status == "updated":
        tag = f"{GREEN}latest{RESET} {CYAN}(just updated from the gateway){RESET}"
    elif status == "behind":
        tag = (f"{YELLOW}UPDATE AVAILABLE{RESET}{GRAY} — {detail}; "
               f"restart without --no-update{RESET}")
    elif status == "failed":
        tag = f"{RED}UPDATE CHECK FAILED{RESET}{GRAY} — {detail}{RESET}"
    elif status == "disabled":
        tag = f"{GRAY}update check disabled in config{RESET}"
    elif status == "skipped":
        tag = f"{YELLOW}not checked{RESET}{GRAY} (--no-update){RESET}"
    else:
        tag = f"{GRAY}unknown{RESET}"
    return f"  Version   : {WHITE}{ver}{RESET}  {tag}"

# ---------------------------------------------------------------------------
# Runtime device picker — swap an audio device without restarting
# ---------------------------------------------------------------------------
# i / k / o open a modal list over the dashboard. While state["picker"] is set
# the keyboard thread routes EVERY key into the picker (so the normal hotkeys
# can't fire behind the menu) and the display thread draws the list instead of
# the meters. Choosing an entry only sets state["<kind>_dev_pending"]; the
# thread that owns that stream picks it up on its next loop iteration and does
# the close/reopen itself, so a PortAudio stream is only ever touched from the
# thread that created it.

PICKERS = {
    "tx":  ("i", "TX PROGRAM FEED", "tx_dev",  False),
    "mic": ("k", "OPERATOR MIC",    "mic_dev", False),
    "out": ("o", "RX SPEAKER",      "rx_dev",  True),
}


def open_picker(state, kind):
    """Build the device list for `kind` and put the UI into picker mode."""
    _key, title, prefix, is_output = PICKERS[kind]
    try:
        items = list_output_devices() if is_output else list_input_devices()
    except Exception as e:
        state["picker_msg"] = f"{RED}device scan failed: {e}{RESET}"
        return
    if not items:
        state["picker_msg"] = f"{RED}no {'output' if is_output else 'input'} devices found{RESET}"
        return
    # Start the highlight on whatever is in use right now.
    cur_name = state.get(f"{prefix}_name")
    sel = 0
    for n, (_idx, name, _ch) in enumerate(items):
        if name == cur_name:
            sel = n
            break
    state["picker_msg"] = ""
    state["picker"] = {
        "kind": kind, "title": title, "prefix": prefix,
        "items": items, "sel": sel, "typed": "", "error": "",
    }


def close_picker(state):
    """Close the device picker without choosing."""
    state["picker"] = None


def _picker_commit(state, p):
    """Hand the highlighted device to the owning thread and leave picker mode."""
    items = p["items"]
    if p["typed"]:
        try:
            n = int(p["typed"])
        except ValueError:
            n = 0
        if not (1 <= n <= len(items)):
            p["typed"] = ""
            p["error"] = f"pick 1-{len(items)}"
            return
        p["sel"] = n - 1
    idx, name, _ch = items[p["sel"]]
    state[f"{p['kind']}_dev_pending"] = (idx, name)
    state["picker_msg"] = f"{YELLOW}switching {p['title'].lower()} -> {name} ...{RESET}"
    close_picker(state)


def _picker_key(ch, state):
    """Modal key handling — every keystroke lands here while the picker is open."""
    p = state.get("picker")
    if not p:
        return
    items = p["items"]
    if ch in ("\x1b", "q"):                      # Esc / q — cancel, change nothing
        state["picker_msg"] = ""
        close_picker(state)
    elif ch in ("\r", "\n"):
        _picker_commit(state, p)
    elif ch.isdigit():
        # Typing digits moves the highlight; Enter commits. Keeping the digits
        # instead of acting on the first one is what makes 1 and 12 both work.
        p["typed"] = (p["typed"] + ch)[-3:]
        p["error"] = ""
        try:
            n = int(p["typed"])
        except ValueError:
            n = 0
        if 1 <= n <= len(items):
            p["sel"] = n - 1
    elif ch in ("\x08", "\x7f"):
        p["typed"] = p["typed"][:-1]
    elif ch == "j":
        p["typed"] = ""
        p["sel"] = (p["sel"] + 1) % len(items)
    elif ch == "k":
        p["typed"] = ""
        p["sel"] = (p["sel"] - 1) % len(items)


def render_picker(state):
    """Full-screen frame for the picker (replaces the dashboard while open)."""
    p = state.get("picker")
    items = p["items"]
    cur_name = state.get(f"{p['prefix']}_name")
    out = ["\033[2J\033[H", f"{BOLD}Select {p['title']}{RESET}\n\n"]
    for n, (idx, name, ch_count) in enumerate(items):
        live = f"  {GREEN}(in use){RESET}" if name == cur_name else ""
        row = f"{n + 1:3d}) {name}  (index {idx}, {ch_count}ch)"
        if n == p["sel"]:
            out.append(f"  {CYAN}{BOLD}> {row}{RESET}{live}\n")
        else:
            out.append(f"    {GRAY}{row}{RESET}{live}\n")
    typed = f"   typed:{WHITE}{p['typed']}{RESET}" if p["typed"] else ""
    err = f"   {RED}{p['error']}{RESET}" if p.get("error") else ""
    out.append(
        f"\n  {CYAN}j{RESET}/{CYAN}k{RESET} move   {CYAN}0-9{RESET} pick by number   "
        f"{CYAN}Enter{RESET} use it   {CYAN}Esc{RESET} cancel{typed}{err}\n"
        f"  {GRAY}Audio keeps running until you press Enter. Devices plugged in "
        f"after this client started may not be listed.{RESET}\n"
    )
    return "".join(out)

# ---------------------------------------------------------------------------
# Level meter
# ---------------------------------------------------------------------------
def rms_db(pcm_bytes):
    """RMS level of int16 PCM in dBFS; -100 for silence or an empty buffer."""
    n_samples = len(pcm_bytes) // 2
    if n_samples == 0:
        return -100.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(samples * samples))
    if rms < 1:
        return -100.0
    return 20.0 * math.log10(rms / 32768.0)


def level_bar(db, width=20, vol_pct=100):
    """Render a 60 dB level bar with a marker at the volume setting.

    *vol_pct* scales the level first, so a boosted mic reads louder.
    Returns (bar, scaled_db).
    """
    # vol_pct may exceed 100 for the mic gain control (boost); the dB scaling
    # reflects the boost (clamped at 0 dBFS), while the marker pins to the bar.
    vol_frac = max(0.0, vol_pct / 100.0)
    if vol_frac > 0:
        scaled_db = db + 20.0 * math.log10(vol_frac)
    else:
        scaled_db = -100.0
    clamped = max(-60.0, min(0.0, scaled_db))
    filled = int((clamped + 60.0) / 60.0 * width)
    marker_pos = int(min(1.0, vol_frac) * width)
    marker_pos = max(0, min(width, marker_pos))
    bar_chars = []
    for i in range(width):
        if i == marker_pos and marker_pos < width:
            bar_chars.append("|")
        elif i < filled:
            bar_chars.append("#")
        else:
            bar_chars.append("-")
    return "".join(bar_chars), scaled_db


_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def sparkline(values, width=20, lo=0.0, hi=100.0):
    """Render a list of recent values as a fixed-width block sparkline (a small
    'graph'). Newest sample on the right; left-padded with blanks until full."""
    span = (hi - lo) or 1.0
    vals = list(values)[-width:]
    out = []
    for v in vals:
        frac = max(0.0, min(1.0, (v - lo) / span))
        out.append(_SPARK_CHARS[int(frac * (len(_SPARK_CHARS) - 1))])
    return "".join(out).rjust(width)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _recv_exact(sock, n):
    """Read exactly *n* bytes from *sock*. Returns None if the peer closes first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

# ---------------------------------------------------------------------------
# Resampling helpers (shared by the program-feed TX path and the mic path)
# ---------------------------------------------------------------------------
def _make_resampler(capture_rate):
    """Build a mono int16 resampler from capture_rate -> SAMPLE_RATE.

    Returns (resampler, backend). backend is one of:
      "none"    — no resampling needed (capture_rate == SAMPLE_RATE)
      "soxr"    — resampler is a stateful soxr.ResampleStream (one per stream)
      "scipy"   — resampler is a ("scipy", up, down) tuple for resample_poly
      "missing" — no resampler backend installed; caller passes audio through
    """
    if capture_rate == SAMPLE_RATE:
        return None, "none"
    resampler = None
    backend = "none"
    if _RESAMPLER == "soxr":
        try:
            resampler = _soxr.ResampleStream(
                capture_rate, SAMPLE_RATE, 1, dtype="int16", quality="HQ",
            )
            backend = "soxr"
        except Exception as e:
            sys.stderr.write(f"  soxr init failed: {e}\n")
            resampler = None
    if resampler is None and _RESAMPLER in ("soxr", "scipy"):
        try:
            from math import gcd
            g = gcd(capture_rate, SAMPLE_RATE)
            resampler = ("scipy", SAMPLE_RATE // g, capture_rate // g)
            backend = "scipy"
        except Exception:
            resampler = None
    if resampler is None:
        backend = "missing"
    return resampler, backend


def _apply_resampler(resampler, backend, samples):
    """Resample a mono int16 numpy array to 48 kHz.

    Returns a mono int16 array, or None when a stateful backend is still
    warming up (no output yet). When backend is "none"/"missing" the input is
    returned unchanged.
    """
    if backend == "soxr":
        out = resampler.resample_chunk(samples)
        if out is None or out.size == 0:
            return None
        return np.ascontiguousarray(out, dtype=np.int16)
    if backend == "scipy":
        _, up, down = resampler
        out = _scipy_signal.resample_poly(samples.astype(np.float32), up, down)
        return np.clip(out, -32768, 32767).astype(np.int16)
    return samples


class SampleFIFO:
    """Thread-safe mono int16 sample buffer for handing audio between a capture
    thread (producer) and the mixer (consumer).

    push() appends samples; pull(n) returns exactly n samples, zero-padded on
    underrun. The buffer is capped (oldest samples dropped) so a stalled
    consumer can't grow it without bound.
    """

    def __init__(self, maxlen=SAMPLE_RATE):  # ~1 s cap
        """Create an empty FIFO holding at most *maxlen* samples."""
        self._buf = np.zeros(0, dtype=np.int16)
        self._lock = threading.Lock()
        self._maxlen = int(maxlen)

    def push(self, samples):
        """Append int16 samples, dropping the oldest past the cap."""
        with self._lock:
            self._buf = np.concatenate([self._buf, samples])
            if len(self._buf) > self._maxlen:
                self._buf = self._buf[-self._maxlen:]

    def pull(self, n):
        """Remove and return exactly *n* samples, zero-padded if fewer are buffered."""
        with self._lock:
            have = len(self._buf)
            if have >= n:
                out = self._buf[:n]
                self._buf = self._buf[n:]
                return out
            out = np.zeros(n, dtype=np.int16)
            if have:
                out[:have] = self._buf
                self._buf = self._buf[:0]
            return out

    def available(self):
        """Number of samples buffered."""
        with self._lock:
            return len(self._buf)

    def clear(self):
        """Drop everything buffered."""
        with self._lock:
            self._buf = self._buf[:0]


# ---------------------------------------------------------------------------
# Voice effects — real-time DSP applied to the operator mic
#
# Each effect is a small stateful processor: process(x) takes a float32 mono
# chunk (int16 amplitude scale) and returns a float32 chunk of the SAME length,
# keeping whatever state it needs (oscillator phase, delay buffers, filter
# state) across chunks so there are no clicks at chunk boundaries. Everything is
# vectorised with numpy (no per-sample Python loops) so it runs comfortably in
# real time. Cycle with n/m, toggle on/off with v.
# ---------------------------------------------------------------------------

def _fb_comb(buf, idx0, x, fb):
    """Feedback comb/delay, robust to any chunk length: returns the delayed
    signal and writes (x + fb*delayed) back. Processes in slices no larger than
    the buffer so a chunk longer than the delay still works. Returns new idx."""
    bl = len(buf)
    n = len(x)
    out = np.empty(n, dtype=np.float32)
    off = 0
    i = idx0
    while off < n:
        m = min(bl, n - off)
        idx = (i + np.arange(m)) % bl
        d = buf[idx]
        out[off:off + m] = d
        buf[idx] = x[off:off + m] + fb * d
        i = (i + m) % bl
        off += m
    return out, i


class _FX:
    """Base effect = passthrough (the 'Raw' slot)."""
    name = "Raw"
    def __init__(self, rate):
        """*rate* is the sample rate in Hz."""
        self.rate = rate
    def process(self, x):
        """Process one float32 mono chunk (int16 amplitude scale).

        Returns a float32 chunk of the same length. Subclasses keep their state
        (phase, delay lines, filter state) across calls so chunk boundaries
        don't click.
        """
        return x


class _Pitch(_FX):
    """Real-time pitch shift via a vectorised dual-delay-line (two taps offset
    half a window, crossfaded with complementary triangular windows). Keeps
    duration constant, so it works on a live voice. semitones >0 = up."""
    def __init__(self, rate, semitones, name):
        super().__init__(rate)
        self.name = name
        self.ratio = 2.0 ** (semitones / 12.0)
        self.win = max(384, int(0.032 * rate))
        self.hist = np.zeros(self.win, dtype=np.float32)
        self.dphase = 0.0
    def process(self, x):
        n = len(x); win = self.win
        sig = np.concatenate([self.hist, x])
        base = win + np.arange(n)
        inc = 1.0 - self.ratio
        d = self.dphase + inc * np.arange(n)
        d1 = np.mod(d, win)
        d2 = np.mod(d + win * 0.5, win)
        def gather(dd):
            p = base - dd
            i0 = np.floor(p).astype(np.int64)
            fr = (p - i0).astype(np.float32)
            i0 = np.clip(i0, 0, len(sig) - 2); i1 = i0 + 1
            return sig[i0] * (1.0 - fr) + sig[i1] * fr
        a = gather(d1); b = gather(d2)
        w1 = 1.0 - np.abs(2.0 * d1 / win - 1.0)
        w2 = 1.0 - np.abs(2.0 * d2 / win - 1.0)
        s = w1 + w2; s = np.where(s < 1e-6, 1.0, s)
        out = ((a * w1 + b * w2) / s).astype(np.float32)
        self.dphase = float(np.mod(self.dphase + inc * n, win))
        self.hist = sig[-win:].copy()
        return out


class _Ring(_FX):
    """Ring modulation — multiply by a sine carrier. Low freq = robot, higher =
    metallic/alien. Carrier phase is continuous across chunks."""
    def __init__(self, rate, freq, name):
        super().__init__(rate)
        self.name = name; self.freq = freq; self.ph = 0.0
    def process(self, x):
        n = len(x)
        t = (2.0 * np.pi * self.freq / self.rate) * (self.ph + np.arange(n))
        self.ph += n
        return (x * np.sin(t)).astype(np.float32)


class _Tremolo(_FX):
    """Amplitude modulation (volume wobble)."""
    def __init__(self, rate, freq=7.0, depth=0.85, name="Tremolo"):
        super().__init__(rate)
        self.name = name; self.freq = freq; self.depth = depth; self.ph = 0.0
    def process(self, x):
        n = len(x)
        t = (2.0 * np.pi * self.freq / self.rate) * (self.ph + np.arange(n))
        self.ph += n
        lfo = (1.0 - self.depth) + self.depth * 0.5 * (1.0 + np.sin(t))
        return (x * lfo).astype(np.float32)


class _Vibrato(_FX):
    """Pitch warble via an LFO-modulated fractional delay."""
    def __init__(self, rate, freq=5.5, depth_ms=3.0, name="Wobble"):
        super().__init__(rate)
        self.name = name; self.freq = freq
        self.depth = depth_ms * rate / 1000.0
        self.md = int(self.depth) + 4
        self.hist = np.zeros(self.md, dtype=np.float32); self.ph = 0.0
    def process(self, x):
        n = len(x); md = self.md
        sig = np.concatenate([self.hist, x]); base = md + np.arange(n)
        t = (2.0 * np.pi * self.freq / self.rate) * (self.ph + np.arange(n))
        self.ph += n
        dd = self.depth * 0.5 * (1.0 + np.sin(t))
        p = base - dd
        i0 = np.floor(p).astype(np.int64); fr = (p - i0).astype(np.float32)
        i0 = np.clip(i0, 0, len(sig) - 2); i1 = i0 + 1
        out = (sig[i0] * (1.0 - fr) + sig[i1] * fr).astype(np.float32)
        self.hist = sig[-md:].copy()
        return out


class _Echo(_FX):
    """Single feedback delay mixed with the dry voice."""
    def __init__(self, rate, delay=0.28, fb=0.45, mix=0.5, name="Echo"):
        super().__init__(rate)
        self.name = name
        self.buf = np.zeros(max(1, int(rate * delay)), dtype=np.float32)
        self.i = 0; self.fb = fb; self.mix = mix
    def process(self, x):
        d, self.i = _fb_comb(self.buf, self.i, x, self.fb)
        return (x + self.mix * d).astype(np.float32)


class _Reverb(_FX):
    """Schroeder-ish reverb from parallel feedback combs (delays chosen > a
    typical chunk so they stay click-free)."""
    def __init__(self, rate, name="Cathedral", mix=0.6):
        super().__init__(rate)
        self.name = name; self.mix = mix
        self.combs = [{"buf": np.zeros(max(1, int(rate * d)), dtype=np.float32),
                       "i": 0, "fb": fb}
                      for d, fb in ((0.0297, 0.80), (0.0371, 0.79),
                                    (0.0411, 0.78), (0.0437, 0.77))]
    def process(self, x):
        acc = np.zeros(len(x), dtype=np.float32)
        for c in self.combs:
            d, c["i"] = _fb_comb(c["buf"], c["i"], x, c["fb"])
            acc += d
        acc /= len(self.combs)
        return (x * (1.0 - self.mix) + acc * self.mix).astype(np.float32)


class _Bandpass(_FX):
    """Telephone-style band-limited voice."""
    def __init__(self, rate, lo=400.0, hi=3000.0, gain=1.3, name="Telephone"):
        super().__init__(rate)
        self.name = name; self.gain = gain
        from scipy.signal import butter
        self.sos = butter(4, [lo, hi], btype="band", fs=rate, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))
    def process(self, x):
        from scipy.signal import sosfilt
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        return (y * self.gain).astype(np.float32)


class _Distort(_FX):
    """Overdrive + band-limit = megaphone / bullhorn."""
    def __init__(self, rate, drive=9.0, name="Megaphone"):
        super().__init__(rate)
        self.name = name; self.drive = drive
        from scipy.signal import butter
        self.sos = butter(2, [350.0, 3200.0], btype="band", fs=rate, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))
    def process(self, x):
        from scipy.signal import sosfilt
        y = np.tanh(self.drive * (x / 32768.0)) * (32768.0 * 0.7)
        y, self.zi = sosfilt(self.sos, y, zi=self.zi)
        return y.astype(np.float32)


class _Bitcrush(_FX):
    """Lo-fi: bit-depth reduction + sample-and-hold downsampling."""
    def __init__(self, rate, bits=5, downsample=6, name="Bitcrush"):
        super().__init__(rate)
        self.name = name; self.bits = bits; self.ds = max(1, downsample)
        self.cnt = 0; self.last = 0.0
    def process(self, x):
        n = len(x); ds = self.ds
        step = float(2 ** (16 - self.bits))
        pos = np.arange(n) + self.cnt
        grp = (pos // ds) * ds - self.cnt
        out = np.empty(n, dtype=np.float32)
        valid = grp >= 0
        gi = np.clip(grp, 0, n - 1)
        out[:] = np.where(valid, x[gi], self.last)
        self.last = float(out[-1])
        self.cnt = (self.cnt + n) % ds
        return (np.round(out / step) * step).astype(np.float32)


class _Underwater(_FX):
    """Muffled low-pass + slow wobble."""
    def __init__(self, rate, name="Underwater"):
        super().__init__(rate)
        self.name = name
        from scipy.signal import butter
        self.sos = butter(4, 700.0, btype="low", fs=rate, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))
        self.vib = _Vibrato(rate, freq=3.0, depth_ms=4.0, name="uw")
    def process(self, x):
        from scipy.signal import sosfilt
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        return self.vib.process(y.astype(np.float32))


class _Drive(_FX):
    """Tanh overdrive (no band-limit) — distortion/growl for use in chains."""
    def __init__(self, rate, drive=5.0, mix=1.0, name="Drive"):
        super().__init__(rate); self.name = name; self.drive = drive; self.mix = mix
    def process(self, x):
        y = np.tanh(self.drive * (x / 32768.0)) * (32768.0 * 0.8)
        return (x * (1.0 - self.mix) + y * self.mix).astype(np.float32)


class _NoiseAdd(_FX):
    """Add hiss (radio static)."""
    def __init__(self, rate, level=350.0, name="Noise"):
        super().__init__(rate); self.name = name; self.level = level
    def process(self, x):
        return (x + self.level * np.random.randn(len(x)).astype(np.float32)).astype(np.float32)


class _RingSweep(_FX):
    """Ring modulation with an LFO-swept carrier — sci-fi laser warble."""
    def __init__(self, rate, lo=60.0, hi=900.0, lfo=0.4, name="Sci-Fi"):
        super().__init__(rate); self.name = name
        self.lo = lo; self.hi = hi; self.lfo = lfo; self.cph = 0.0; self.lph = 0.0
    def process(self, x):
        n = len(x)
        lt = (2.0 * np.pi * self.lfo / self.rate) * (self.lph + np.arange(n)); self.lph += n
        f = self.lo + (self.hi - self.lo) * 0.5 * (1.0 + np.sin(lt))
        ph = self.cph + np.cumsum(2.0 * np.pi * f / self.rate)
        self.cph = float(ph[-1] % (2.0 * np.pi))
        return (x * np.sin(ph)).astype(np.float32)


class _AutoWah(_FX):
    """LFO-swept band-pass — funky wah / talkbox."""
    def __init__(self, rate, lo=350.0, hi=2400.0, lfo=1.6, name="Auto-Wah"):
        super().__init__(rate); self.name = name
        self.lo = lo; self.hi = hi; self.lfo = lfo; self.lph = 0.0; self.zi = None
    def process(self, x):
        from scipy.signal import butter, sosfilt
        n = len(x)
        t = (2.0 * np.pi * self.lfo / self.rate) * (self.lph + n * 0.5); self.lph += n
        c = self.lo + (self.hi - self.lo) * 0.5 * (1.0 + np.sin(t))
        bw = c * 0.5
        low = max(40.0, c - bw * 0.5); high = min(self.rate * 0.49, c + bw * 0.5)
        sos = butter(2, [low, high], btype="band", fs=self.rate, output="sos")
        if self.zi is None or self.zi.shape[0] != sos.shape[0]:
            self.zi = np.zeros((sos.shape[0], 2))
        y, self.zi = sosfilt(sos, x, zi=self.zi)
        return (y * 2.2).astype(np.float32)


class _Flanger(_FX):
    """Dry + short swept delay — jet-plane whoosh."""
    def __init__(self, rate, base_ms=1.0, depth_ms=3.5, lfo=0.25, mix=0.5, name="Flanger"):
        super().__init__(rate); self.name = name
        self.base = base_ms * rate / 1000.0; self.depth = depth_ms * rate / 1000.0
        self.lfo = lfo; self.mix = mix
        self.md = int(self.base + self.depth) + 4
        self.hist = np.zeros(self.md, dtype=np.float32); self.ph = 0.0
    def process(self, x):
        n = len(x); md = self.md
        sig = np.concatenate([self.hist, x]); base = md + np.arange(n)
        t = (2.0 * np.pi * self.lfo / self.rate) * (self.ph + np.arange(n)); self.ph += n
        d = self.base + self.depth * 0.5 * (1.0 + np.sin(t))
        p = base - d
        i0 = np.floor(p).astype(np.int64); fr = (p - i0).astype(np.float32)
        i0 = np.clip(i0, 0, len(sig) - 2); i1 = i0 + 1
        delayed = sig[i0] * (1.0 - fr) + sig[i1] * fr
        self.hist = sig[-md:].copy()
        return (x * (1.0 - self.mix) + delayed * self.mix).astype(np.float32)


class _Gate(_FX):
    """Rhythmic on/off chopping (trance/stutter gate); edges smoothed to tame clicks."""
    def __init__(self, rate, freq=11.0, duty=0.5, name="Stutter"):
        super().__init__(rate); self.name = name; self.freq = freq; self.duty = duty
        self.ph = 0.0; self.zf = np.zeros(1, dtype=np.float64)
    def process(self, x):
        from scipy.signal import lfilter
        n = len(x)
        t = ((self.ph + np.arange(n)) * self.freq / self.rate) % 1.0; self.ph += n
        hard = (t < self.duty).astype(np.float64)
        a = 0.01
        env, self.zf = lfilter([a], [1.0, -(1.0 - a)], hard, zi=self.zf)
        return (x * env.astype(np.float32)).astype(np.float32)


class _Chain(_FX):
    """Run several effects in series (output of one feeds the next)."""
    def __init__(self, rate, name, stages):
        super().__init__(rate); self.name = name; self.stages = stages
    def process(self, x):
        for s in self.stages:
            x = s.process(x)
        return x


class _Parallel(_FX):
    """Sum several effects fed the same input — harmonies / chorus / layering."""
    def __init__(self, rate, name, branches, gains=None):
        super().__init__(rate); self.name = name; self.branches = branches
        self.gains = gains or [1.0] * len(branches)
        self.norm = 1.0 / max(1e-6, sum(self.gains))
    def process(self, x):
        acc = np.zeros(len(x), dtype=np.float32)
        for b, g in zip(self.branches, self.gains):
            acc = acc + g * b.process(x)
        return (acc * self.norm).astype(np.float32)


# (name, factory) — index 0 is Raw. n/m cycle this list; v toggles on/off.
EFFECTS = [
    ("Raw",        lambda r: _FX(r)),
    ("Chipmunk",   lambda r: _Pitch(r, +9, "Chipmunk")),
    ("Helium",     lambda r: _Pitch(r, +5, "Helium")),
    ("Squeaky",    lambda r: _Pitch(r, +15, "Squeaky")),
    ("Deep",       lambda r: _Pitch(r, -5, "Deep")),
    ("Monster",    lambda r: _Pitch(r, -10, "Monster")),
    ("Titan",      lambda r: _Pitch(r, -15, "Titan")),
    ("Demon",      lambda r: _Chain(r, "Demon", [_Pitch(r, -13, "d"), _Drive(r, 5.0)])),
    ("Robot",      lambda r: _Ring(r, 60.0, "Robot")),
    ("Dalek",      lambda r: _Chain(r, "Dalek", [_Ring(r, 30.0, "d"), _Drive(r, 3.5)])),
    ("Alien",      lambda r: _Ring(r, 220.0, "Alien")),
    ("Cylon",      lambda r: _Chain(r, "Cylon", [_Ring(r, 50.0, "c"), _Tremolo(r, 5.0, 0.8, "t")])),
    ("Sci-Fi",     lambda r: _RingSweep(r)),
    ("Chord",      lambda r: _Parallel(r, "Chord", [_FX(r), _Pitch(r, +7, "5"), _Pitch(r, +12, "8")], [1.0, 0.9, 0.8])),
    ("Chorus",     lambda r: _Parallel(r, "Chorus", [_FX(r), _Vibrato(r, 0.6, 6.0, "a"), _Vibrato(r, 0.9, 8.0, "b")], [1.0, 0.9, 0.9])),
    ("Ghost",      lambda r: _Chain(r, "Ghost", [_Pitch(r, +4, "g"), _Reverb(r, "gr", 0.7), _Tremolo(r, 4.0, 0.6, "t")])),
    ("Wobble",     lambda r: _Vibrato(r, 5.5, 3.0, "Wobble")),
    ("Drunk",      lambda r: _Vibrato(r, 1.1, 9.0, "Drunk")),
    ("Tremolo",    lambda r: _Tremolo(r, 7.0, 0.85, "Tremolo")),
    ("Flanger",    lambda r: _Flanger(r)),
    ("Auto-Wah",   lambda r: _AutoWah(r)),
    ("Echo",       lambda r: _Echo(r, 0.28, 0.45, 0.5, "Echo")),
    ("Cathedral",  lambda r: _Reverb(r, "Cathedral", 0.6)),
    ("Telephone",  lambda r: _Bandpass(r, 400.0, 3000.0, 1.3, "Telephone")),
    ("CB-Radio",   lambda r: _Chain(r, "CB-Radio", [_Bandpass(r, 500.0, 2600.0, 1.2, "b"), _Drive(r, 4.0), _NoiseAdd(r, 300.0)])),
    ("Megaphone",  lambda r: _Distort(r, 9.0, "Megaphone")),
    ("8-Bit",      lambda r: _Chain(r, "8-Bit", [_Bitcrush(r, 4, 8, "b"), _Ring(r, 40.0, "r")])),
    ("Bitcrush",   lambda r: _Bitcrush(r, 5, 6, "Bitcrush")),
    ("Stutter",    lambda r: _Gate(r, 11.0, 0.5, "Stutter")),
    ("Underwater", lambda r: _Underwater(r)),
]

def effect_name(index):
    """Name of effect *index*, wrapping around the EFFECTS list."""
    return EFFECTS[index % len(EFFECTS)][0]

def make_effect(index, rate):
    """Create a fresh instance of effect *index* (wrapping) at *rate*."""
    return EFFECTS[index % len(EFFECTS)][1](rate)
# ---------------------------------------------------------------------------
# TX thread — capture mic and send to gateway
# ---------------------------------------------------------------------------
def _tx_thread_func(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name,
                    mic_fifo=None):
    """Capture the program feed, mix in the operator mic, and send to the gateway.

    Captures the program device at its native rate / channel count, downmixes to
    mono and resamples to 48 kHz. While the hold-to-talk key is held
    (state["ptt_held"]) the program is ducked to state["duck_pct"] and the mic
    (already voice-effected upstream in the mic thread) is summed on top —
    broadcast/DJ style. Resampling of the program feed can be toggled off at
    runtime with the 'r' key (for A/B diagnosis).
    """
    import queue
    tx_q = queue.Queue(maxsize=32)

    state.setdefault("tx_resample_enabled", True)
    state.setdefault("tx_xruns", 0)
    state.setdefault("tx_last_frames", 0)

    # Everything that depends on WHICH device is open lives here, because the
    # device can change at runtime ('i' key). The processing loop below reads
    # these on every chunk rather than closing over them.
    cap = {"stream": None, "rate": SAMPLE_RATE, "channels": 1,
           "needs_resample": False, "resampler": None, "backend": "none",
           "index": in_dev_index, "name": in_dev_name}

    # Callback statistics (always visible in GUI)
    cb_stats = {
        "xruns": 0,
        "callbacks": 0,
        "total_frames": 0,
        "last_frames": 0,
        "last_status": "",
        "last_ts": None,
        "gap_min": None,
        "gap_max": None,
        "gap_sum": 0.0,
        "gap_count": 0,
        "frames_hist": {},   # frames -> count
        "started": time.monotonic(),
    }

    def _tx_callback(indata, frames, time_info, status):
        """Called by sounddevice from audio thread — push PCM to queue."""
        now = time.monotonic()
        if status:
            cb_stats["xruns"] += 1
            cb_stats["last_status"] = str(status)
        if cb_stats["last_ts"] is not None:
            gap = now - cb_stats["last_ts"]
            if cb_stats["gap_min"] is None or gap < cb_stats["gap_min"]:
                cb_stats["gap_min"] = gap
            if cb_stats["gap_max"] is None or gap > cb_stats["gap_max"]:
                cb_stats["gap_max"] = gap
            cb_stats["gap_sum"] += gap
            cb_stats["gap_count"] += 1
        cb_stats["last_ts"] = now
        cb_stats["callbacks"] += 1
        cb_stats["total_frames"] += frames
        cb_stats["last_frames"] = frames
        h = cb_stats["frames_hist"]
        h[frames] = h.get(frames, 0) + 1
        # Mirror into shared state for display thread
        state["tx_xruns"] = cb_stats["xruns"]
        state["tx_last_frames"] = frames
        state["tx_cb_stats"] = cb_stats
        try:
            tx_q.put_nowait(bytes(indata))
        except queue.Full:
            cb_stats["q_drops"] = cb_stats.get("q_drops", 0) + 1
            state["tx_q_drops"] = cb_stats["q_drops"]

    def _reset_cb_stats():
        """Zero the capture stats so the effective-rate / gap readouts describe
        the device that is open NOW, not an average across a device change."""
        cb_stats.update(xruns=0, callbacks=0, total_frames=0, last_frames=0,
                        last_status="", last_ts=None, gap_min=None, gap_max=None,
                        gap_sum=0.0, gap_count=0, frames_hist={}, q_drops=0,
                        started=time.monotonic())
        state["tx_xruns"] = 0
        state["tx_q_drops"] = 0

    def _open_capture(dev_index, dev_name):
        """Open `dev_index` at its native rate and build a matching resampler.

        Raises if the device won't open — the caller decides what to fall back
        to. On success `cap` describes the live stream.
        """
        try:
            dev_info = sd.query_devices(dev_index)
            rate = int(round(float(dev_info["default_samplerate"]))) or SAMPLE_RATE
            max_in_ch = int(dev_info["max_input_channels"])
        except Exception:
            rate, max_in_ch = SAMPLE_RATE, 1
        channels = 2 if max_in_ch >= 2 else 1
        needs_resample = (rate != SAMPLE_RATE)
        resampler, backend = (None, "none")
        if needs_resample:
            resampler, backend = _make_resampler(rate)
            if backend == "missing":
                sys.stderr.write(
                    f"[tx] WARNING: {dev_name!r} captures at {rate} Hz, the gateway "
                    f"needs 48000 Hz, and no resampler is installed (pip install soxr). "
                    f"Audio will sound choppy.\n")

        # TX_BLOCK_MS blocks at the capture rate (decoupled from the network/RX
        # chunk size so we can run a low-latency capture without breaking RX).
        block = max(1, int(round(rate * TX_BLOCK_MS / 1000.0)))
        stream = sd.RawInputStream(
            samplerate=rate,
            blocksize=block,
            device=dev_index,
            channels=channels,
            dtype="int16",
            latency=AUDIO_LATENCY,
            callback=_tx_callback,
        )
        stream.start()
        cap.update(stream=stream, rate=rate, channels=channels,
                   needs_resample=needs_resample, resampler=resampler,
                   backend=backend, index=dev_index, name=dev_name)

        state["tx_capture_rate"] = float(rate)
        state["tx_capture_channels"] = channels
        state["tx_needs_resample"] = needs_resample
        state["tx_resample_backend"] = backend
        state["tx_dev_name"] = dev_name
        try:
            state["tx_stream_rate"] = float(stream.samplerate)
        except Exception:
            state["tx_stream_rate"] = float(rate)
        state["tx_device_rate"] = float(rate)

        # Diagnostic dump (stderr, so it survives the display thread's
        # screen-clears in the scrollback)
        try:
            sys.stderr.write(
                f"[tx] device={dev_name!r} (index {dev_index})\n"
                f"[tx] requested: rate={rate} channels={channels} blocksize={block}\n"
                f"[tx] actual stream rate={stream.samplerate} latency={stream.latency}\n"
                f"[tx] resample: needed={needs_resample} backend={backend} "
                f"enabled={state.get('tx_resample_enabled', True)}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
        return stream

    def _switch_capture(dev_index, dev_name):
        """Swap the program feed to another device without dropping the TCP
        connection to the gateway. If the new device won't open we go back to
        the one that was working rather than leaving TX dead."""
        prev = (cap["index"], cap["name"])
        old = cap["stream"]
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception:
                pass
        # Drop whatever the old device left queued: those bytes are at the old
        # rate and channel count, and processing them with the new device's
        # parameters would garble a chunk.
        while True:
            try:
                tx_q.get_nowait()
            except queue.Empty:
                break
        _reset_cb_stats()
        try:
            _open_capture(dev_index, dev_name)
        except Exception as e:
            sys.stderr.write(f"[tx] switch to {dev_name!r} failed: {e}\n")
            state["picker_msg"] = f"{RED}TX feed: {dev_name} — {e}{RESET}"
            try:
                _open_capture(*prev)
                state["picker_msg"] += f"  {GRAY}(kept {prev[1]}){RESET}"
            except Exception as e2:
                sys.stderr.write(f"[tx] fallback to {prev[1]!r} FAILED: {e2}\n")
                cap["stream"] = None
                state["tx_error"] = str(e2)
                state["picker_msg"] += f"  {RED}(old device gone too — press i){RESET}"
            return
        state["picker_msg"] = f"{GREEN}TX feed -> {dev_name}{RESET}"
        cfg["tx_device_name"] = dev_name
        try:
            save_config(cfg)
        except Exception:
            pass

    _open_capture(in_dev_index, in_dev_name)

    sock = None
    wav_writer = None

    def connect():
        """Open the TX socket to the gateway. Returns it, or None on failure."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((gateway_host, tx_port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            return s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            return None

    last_connect_attempt = 0.0
    try:
        while state["running"]:
            # Device swap requested by the picker ('i'). Done here, on the
            # thread that owns the stream, never from the keyboard thread.
            pending = state.pop("tx_dev_pending", None)
            if pending:
                _switch_capture(*pending)

            # Non-blocking connect / reconnect: try every RECONNECT_INTERVAL
            # without blocking the audio-processing path, so the level meter
            # keeps updating even when the gateway is down.
            if sock is None:
                state["tx_connected"] = False
                if time.monotonic() - last_connect_attempt >= RECONNECT_INTERVAL:
                    last_connect_attempt = time.monotonic()
                    sock = connect()
                    if sock is not None:
                        state["tx_connected"] = True

            # Get audio from callback queue (ALWAYS — regardless of socket)
            try:
                pcm = tx_q.get(timeout=0.1)
            except queue.Empty:
                continue

            # Snapshot queue depth right after the get, so the GUI shows how
            # much the loop is behind.
            state["tx_q_depth"] = tx_q.qsize()
            iter_start = time.monotonic()

            try:
                # Decode → numpy
                samples = np.frombuffer(pcm, dtype=np.int16)

                # Downmix stereo → mono (avoid int16 overflow via int32)
                if cap["channels"] == 2:
                    samples = (samples.reshape(-1, 2).astype(np.int32)
                               .mean(axis=1).astype(np.int16))

                # Resample to 48 kHz if needed and currently enabled
                if (cap["needs_resample"] and state.get("tx_resample_enabled", True)
                        and cap["resampler"] is not None):
                    out = _apply_resampler(cap["resampler"], cap["backend"], samples)
                    if out is None:
                        continue  # stateful backend still warming up
                    samples = out

                # --- Broadcast mix: duck program + overlay mic ----------------
                # tx_live ('l') gates the program feed; ptt_held (hold-to-talk)
                # gates the mic. While transmitting, the program ducks under the
                # voice. We always emit a chunk (silence when nothing is live)
                # so the stream to the gateway stays continuous.
                ptt = state.get("ptt_held", False)
                state["mic_tx_active"] = ptt
                n = len(samples)
                if not state["tx_live"]:
                    prog_gain = 0.0
                elif ptt:
                    prog_gain = state.get("duck_pct", 25) / 100.0
                else:
                    prog_gain = 1.0
                mixed = samples.astype(np.float32) * prog_gain
                if ptt:
                    # The mic feed comes from mic_fifo, which captures the VAC
                    # return cable. VCClient owns the physical mic (Focusrite) and
                    # writes that cable either raw (pass_through=True) or
                    # voice-changed (pass_through=False) — toggled by 'v' over
                    # REST. So the mixer always reads mic_fifo; the raw/effected
                    # choice happens upstream in VCClient.
                    if mic_fifo is not None:
                        mic = mic_fifo.pull(n).astype(np.float32)
                        mic *= state.get("mic_vol", 100) / 100.0
                        mixed = mixed + mic
                else:
                    # Not transmitting: drop any buffered mic so the next over
                    # starts fresh. The mic thread keeps capturing while PTT is
                    # released, so without this the FIFO fills and you'd hear ~1s
                    # of stale audio on key-up (the "massive latency").
                    if mic_fifo is not None:
                        mic_fifo.clear()

                samples = np.clip(mixed, -32768, 32767).astype(np.int16)

                # Apply master TX volume
                vol = state["tx_vol"]
                if vol < 100:
                    fs = samples.astype(np.float32) * (vol / 100.0)
                    samples = np.clip(fs, -32768, 32767).astype(np.int16)

                pcm = samples.tobytes()
            except Exception as e:
                print(f"\n  TX processing error: {type(e).__name__}: {e}")
                continue

            # WAV dump of post-processed audio (toggled with 'w'). The header
            # rate must match the data rate: 48 kHz when resampling is on,
            # native capture_rate when it's off (so the file plays at the
            # correct speed in either mode).
            want_wav = state.get("tx_wav_request", False)
            if want_wav and wav_writer is None:
                resampling_now = (cap["needs_resample"]
                                  and state.get("tx_resample_enabled", True)
                                  and cap["resampler"] is not None)
                wav_rate = SAMPLE_RATE if resampling_now else cap["rate"]
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                wav_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"tx_dump_{wav_rate}_{ts}.wav",
                )
                try:
                    wav_writer = wave.open(wav_path, "wb")
                    wav_writer.setnchannels(1)
                    wav_writer.setsampwidth(2)
                    wav_writer.setframerate(wav_rate)
                    state["tx_wav_path"] = wav_path
                    state["tx_wav_bytes"] = 0
                    state["tx_wav_rate"] = wav_rate
                except Exception as e:
                    print(f"\n  Failed to open WAV file: {e}")
                    wav_writer = None
                    state["tx_wav_request"] = False
            elif not want_wav and wav_writer is not None:
                try:
                    wav_writer.close()
                except Exception:
                    pass
                wav_writer = None
                state["tx_wav_path"] = None
            if wav_writer is not None:
                try:
                    wav_writer.writeframesraw(pcm)
                    state["tx_wav_bytes"] = state.get("tx_wav_bytes", 0) + len(pcm)
                except Exception:
                    pass

            # Compute level for display (always — even when offline)
            state["tx_db"] = rms_db(pcm)

            t_before_send = time.monotonic()

            # Send if connected; otherwise quietly drop and keep going so
            # the meter / WAV dump still work while waiting on the gateway.
            # Program mute and mic gating are already folded into the mix above,
            # so we always send the mixed chunk to keep the stream continuous.
            if sock is not None:
                try:
                    header = struct.pack(">I", len(pcm))
                    sock.sendall(header + pcm)
                except Exception:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None

            now = time.monotonic()
            state["tx_iter_ms"] = (now - iter_start) * 1000.0
            state["tx_send_ms"] = (now - t_before_send) * 1000.0
    except Exception as e:
        import traceback
        print(f"\n  TX thread FATAL: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if cap["stream"] is not None:
            try:
                cap["stream"].stop()
                cap["stream"].close()
            except Exception:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if wav_writer is not None:
            try:
                wav_writer.close()
            except Exception:
                pass
        state["tx_connected"] = False

# ---------------------------------------------------------------------------
# Mic thread — capture operator mic, resample to 48 kHz, feed the mixer
# ---------------------------------------------------------------------------
def _mic_thread_func(state, cfg, mic_dev_index, mic_dev_name, mic_fifo):
    """Capture the operator mic, apply the selected voice effect, and push
    48 kHz mono PCM into mic_fifo for the mixer.

    The effect is chosen with n/m (state["effect_index"]) and toggled with v
    (state["effected"]); index 0 / effect off = clean mic. The effect processor
    is (re)built whenever the selection changes so its internal state starts
    fresh, then runs per chunk with no clicks at chunk boundaries.
    """
    import queue
    mic_q = queue.Queue(maxsize=32)

    state.setdefault("mic_db", -100.0)

    # Per-device state, rebuilt whenever the mic is swapped at runtime ('k').
    cap = {"stream": None, "rate": SAMPLE_RATE, "channels": 1,
           "needs_resample": False, "resampler": None, "backend": "none",
           "index": mic_dev_index, "name": mic_dev_name}

    def _cb(indata, frames, time_info, status):
        """Sound device callback: queue the mic block, counting xruns and drops."""
        if status:
            state["mic_xruns"] = state.get("mic_xruns", 0) + 1
        try:
            mic_q.put_nowait(bytes(indata))
        except queue.Full:
            state["mic_q_drops"] = state.get("mic_q_drops", 0) + 1

    def _open_mic(dev_index, dev_name):
        """Open `dev_index` at its native rate. Raises if it won't open."""
        try:
            dev_info = sd.query_devices(dev_index)
            rate = int(round(float(dev_info["default_samplerate"]))) or SAMPLE_RATE
            max_in_ch = int(dev_info["max_input_channels"])
        except Exception:
            rate, max_in_ch = SAMPLE_RATE, 1
        channels = 2 if max_in_ch >= 2 else 1
        needs_resample = (rate != SAMPLE_RATE)
        resampler, backend = (None, "none")
        if needs_resample:
            resampler, backend = _make_resampler(rate)
        block = max(1, int(round(rate * TX_BLOCK_MS / 1000.0)))
        stream = sd.RawInputStream(
            samplerate=rate,
            blocksize=block,
            device=dev_index,
            channels=channels,
            dtype="int16",
            latency=AUDIO_LATENCY,
            callback=_cb,
        )
        stream.start()
        cap.update(stream=stream, rate=rate, channels=channels,
                   needs_resample=needs_resample, resampler=resampler,
                   backend=backend, index=dev_index, name=dev_name)
        state["mic_capture_rate"] = float(rate)
        state["mic_capture_channels"] = channels
        state["mic_resample_backend"] = backend
        state["mic_dev_name"] = dev_name
        state["mic_error"] = ""
        sys.stderr.write(
            f"[mic] device={dev_name!r} rate={rate} "
            f"ch={channels} block={block} resample={backend}\n"
        )
        sys.stderr.flush()
        return stream

    def _switch_mic(dev_index, dev_name):
        """Swap the operator mic. Falls back to the previous device if the new
        one won't open; if that fails too the thread stays alive with no stream
        so you can pick a working device with 'k' instead of restarting."""
        prev = (cap["index"], cap["name"])
        old = cap["stream"]
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception:
                pass
        cap["stream"] = None
        # Old-rate bytes must not be processed with the new device's parameters.
        while True:
            try:
                mic_q.get_nowait()
            except queue.Empty:
                break
        mic_fifo.clear()
        try:
            _open_mic(dev_index, dev_name)
        except Exception as e:
            sys.stderr.write(f"[mic] switch to {dev_name!r} failed: {e}\n")
            state["picker_msg"] = f"{RED}Mic: {dev_name} — {e}{RESET}"
            state["mic_error"] = str(e)
            try:
                _open_mic(*prev)
                state["picker_msg"] += f"  {GRAY}(kept {prev[1]}){RESET}"
            except Exception as e2:
                sys.stderr.write(f"[mic] fallback to {prev[1]!r} FAILED: {e2}\n")
                state["mic_error"] = str(e2)
                state["picker_msg"] += f"  {RED}(old mic gone too — press k){RESET}"
            return
        state["picker_msg"] = f"{GREEN}Mic -> {dev_name}{RESET}"
        cfg["mic_device_name"] = dev_name
        try:
            save_config(cfg)
        except Exception:
            pass

    try:
        _open_mic(mic_dev_index, mic_dev_name)
    except Exception as e:
        # Don't kill the thread — without it 'k' could never rescue a bad mic.
        sys.stderr.write(f"[mic] failed to open {mic_dev_name!r}: {e}\n")
        state["mic_error"] = str(e)

    cur_idx = -1          # which effect is currently instantiated
    cur_fx = None         # the live effect processor (stateful)

    try:
        while state["running"]:
            # Device swap requested by the picker ('k'), handled on the thread
            # that owns the stream.
            pending = state.pop("mic_dev_pending", None)
            if pending:
                _switch_mic(*pending)

            try:
                pcm = mic_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                samples = np.frombuffer(pcm, dtype=np.int16)
                if cap["channels"] == 2:
                    samples = (samples.reshape(-1, 2).astype(np.int32)
                               .mean(axis=1).astype(np.int16))
                if cap["needs_resample"] and cap["resampler"] is not None:
                    out = _apply_resampler(cap["resampler"], cap["backend"], samples)
                    if out is None:
                        continue
                    samples = out

                # --- voice effect -------------------------------------------
                idx = int(state.get("effect_index", 0))
                if idx != cur_idx:
                    cur_idx = idx
                    try:
                        cur_fx = make_effect(idx, SAMPLE_RATE)
                    except Exception as e:
                        sys.stderr.write(f"[fx] build {idx} failed: {e}\n")
                        cur_fx = None
                if state.get("effected") and idx != 0 and cur_fx is not None:
                    try:
                        y = cur_fx.process(samples.astype(np.float32))
                        samples = np.clip(y, -32768, 32767).astype(np.int16)
                    except Exception as e:
                        sys.stderr.write(f"[fx] {effect_name(idx)} error: {e}\n")

                state["mic_db"] = rms_db(samples.tobytes())
                mic_fifo.push(samples)
            except Exception as e:
                sys.stderr.write(f"[mic] processing error: {type(e).__name__}: {e}\n")
                continue
    finally:
        if cap["stream"] is not None:
            try:
                cap["stream"].stop()
                cap["stream"].close()
            except Exception:
                pass
        state["mic_db"] = -100.0


# ---------------------------------------------------------------------------
# RX thread — receive audio from gateway and play
# ---------------------------------------------------------------------------
def _rx_thread_func(state, cfg, rx_port, out_dev_index, out_dev_name):
    """Listen for gateway connection and play received audio."""
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listen_sock.bind(("0.0.0.0", rx_port))
    except OSError as e:
        print(f"\n  Port {rx_port} already in use — is another instance running? ({e})")
        return
    listen_sock.listen(1)
    listen_sock.settimeout(1.0)

    # Which speaker we're playing to. Changed at runtime by the picker ('o').
    out_dev = {"index": out_dev_index, "name": out_dev_name}

    def _take_out_pending():
        """Consume a picker request. True means the caller must (re)open the
        output stream; when no stream is open there's nothing else to do."""
        pending = state.pop("out_dev_pending", None)
        if not pending:
            return False
        out_dev["index"], out_dev["name"] = pending
        state["rx_dev_name"] = pending[1]
        state["picker_msg"] = f"{GREEN}RX speaker -> {pending[1]}{RESET}"
        cfg["rx_device_name"] = pending[1]
        try:
            save_config(cfg)
        except Exception:
            pass
        return True

    try:
        while state["running"]:
            # Accept a connection
            try:
                conn, addr = listen_sock.accept()
            except socket.timeout:
                # No stream open while we wait, so just record the choice.
                _take_out_pending()
                continue
            except OSError:
                break

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(0.2)
            state["rx_connected"] = True
            state["rx_from"] = f"{addr[0]}:{addr[1]}"

            import queue as _queue
            rx_q = _queue.Queue(maxsize=32)

            def _rx_callback(outdata, frames, time_info, status):
                """Called by sounddevice from audio thread — pull PCM from queue."""
                try:
                    pcm = rx_q.get_nowait()
                    expected = frames * CHANNELS * 2  # 16-bit
                    if len(pcm) >= expected:
                        outdata[:] = pcm[:expected]
                    else:
                        outdata[:len(pcm)] = pcm
                        outdata[len(pcm):] = b'\x00' * (expected - len(pcm))
                except _queue.Empty:
                    outdata[:] = b'\x00' * len(outdata)

            def _open_out():
                """Open the playback stream on whatever out_dev names now."""
                st = sd.RawOutputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=FRAMES_PER_BUFFER,
                    device=out_dev["index"],
                    channels=CHANNELS,
                    dtype="int16",
                    latency=AUDIO_LATENCY,
                    callback=_rx_callback,
                )
                st.start()
                try:
                    state["rx_stream_rate"] = float(st.samplerate)
                except Exception:
                    state["rx_stream_rate"] = float(SAMPLE_RATE)
                try:
                    state["rx_device_rate"] = float(
                        sd.query_devices(out_dev["index"])["default_samplerate"])
                except Exception:
                    state["rx_device_rate"] = 0.0
                state["rx_dev_name"] = out_dev["name"]
                return st

            out_stream = _open_out()
            prev_dev = (out_dev["index"], out_dev["name"])

            try:
                while state["running"]:
                    # Speaker swap requested by the picker ('o'). Reopening
                    # here keeps the gateway's TCP connection up — only the
                    # audio device is torn down.
                    if _take_out_pending():
                        try:
                            out_stream.stop()
                            out_stream.close()
                        except Exception:
                            pass
                        try:
                            out_stream = _open_out()
                        except Exception as e:
                            sys.stderr.write(
                                f"[rx] switch to {out_dev['name']!r} failed: {e}\n")
                            state["picker_msg"] = (
                                f"{RED}RX speaker: {out_dev['name']} — {e}{RESET}")
                            # Fall back to the device that was working.
                            out_dev["index"], out_dev["name"] = prev_dev
                            state["rx_dev_name"] = prev_dev[1]
                            try:
                                out_stream = _open_out()
                                state["picker_msg"] += f"  {GRAY}(kept {prev_dev[1]}){RESET}"
                            except Exception as e2:
                                sys.stderr.write(
                                    f"[rx] fallback to {prev_dev[1]!r} FAILED: {e2}\n")
                                state["picker_msg"] += (
                                    f"  {RED}(old speaker gone too — press o){RESET}")
                                break
                        prev_dev = (out_dev["index"], out_dev["name"])

                    try:
                        hdr = _recv_exact(conn, 4)
                    except socket.timeout:
                        continue
                    if hdr is None:
                        break
                    length = struct.unpack(">I", hdr)[0]
                    if length == 0 or length > 960000:
                        break

                    conn.settimeout(None)
                    pcm = _recv_exact(conn, length)
                    conn.settimeout(0.2)
                    if pcm is None:
                        break

                    state["rx_db"] = rms_db(pcm)

                    if state["rx_play"]:
                        vol = state["rx_vol"]
                        if vol < 100:
                            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                            samples *= vol / 100.0
                            pcm_out = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
                        else:
                            pcm_out = pcm
                        try:
                            rx_q.put_nowait(pcm_out)
                        except _queue.Full:
                            try:
                                rx_q.get_nowait()
                            except _queue.Empty:
                                pass
                            try:
                                rx_q.put_nowait(pcm_out)
                            except _queue.Full:
                                pass

            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                out_stream.stop()
                out_stream.close()
                try:
                    conn.close()
                except Exception:
                    pass
                state["rx_connected"] = False
                state["rx_from"] = None
                state["rx_stream_rate"] = 0.0
                state["rx_device_rate"] = 0.0
    finally:
        listen_sock.close()

# ---------------------------------------------------------------------------
# GPU monitor thread — poll nvidia-smi for utilisation + memory (the graph)
# ---------------------------------------------------------------------------
def _gpu_monitor_thread(state):
    """Poll nvidia-smi ~1/s for GPU utilisation + memory and keep a short
    history for the display sparkline. Silently disables itself (no GPU line
    shown) if nvidia-smi is unavailable — e.g. no Nvidia card."""
    import subprocess
    HIST = 60
    query = ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,name",
             "--format=csv,noheader,nounits"]
    # CREATE_NO_WINDOW on Windows so polling doesn't flash a console window.
    flags = 0x08000000 if sys.platform == "win32" else 0
    def _sample():
        """Run nvidia-smi once. Returns (util %, used MiB, total MiB, name)."""
        out = subprocess.run(query, capture_output=True, text=True,
                             timeout=5, creationflags=flags).stdout.strip()
        line = out.splitlines()[0]
        p = [x.strip() for x in line.split(",")]
        return float(p[0]), float(p[1]), float(p[2]), (p[3] if len(p) > 3 else "GPU")
    try:
        _sample()
    except Exception:
        state["gpu_available"] = False
        return
    state["gpu_available"] = True
    state.setdefault("gpu_hist", [])
    while state["running"]:
        try:
            util, mu, mt, name = _sample()
            state["gpu_util"] = util
            state["gpu_mem_used"] = mu
            state["gpu_mem_total"] = mt
            state["gpu_name"] = name
            h = state["gpu_hist"]
            h.append(util)
            if len(h) > HIST:
                del h[:len(h) - HIST]
        except Exception:
            pass
        time.sleep(1.0)

# ---------------------------------------------------------------------------
# Display thread — update status line
# ---------------------------------------------------------------------------
def _display_thread_func(state, gateway_host, tx_port, rx_port,
                         in_dev_name, out_dev_name, mic_dev_name=""):
    """Periodically redraw the status display."""
    while state["running"]:
        # The picker is modal — draw it instead of the meters, and refresh
        # faster so moving the highlight feels immediate.
        if state.get("picker") is not None:
            try:
                sys.stdout.write(render_picker(state))
                sys.stdout.flush()
            except Exception:
                pass
            time.sleep(0.08)
            continue

        # Device names can change under us ('i' / 'k' / 'o'), so read the live
        # ones rather than the values captured at startup.
        in_dev_name = state.get("tx_dev_name") or in_dev_name
        out_dev_name = state.get("rx_dev_name") or out_dev_name
        mic_dev_name = state.get("mic_dev_name") or mic_dev_name

        # --- TX rate / resample status -------------------------------------
        cap_rate = state.get("tx_capture_rate", 0.0)
        cap_ch = state.get("tx_capture_channels", 0)
        needs_r = state.get("tx_needs_resample", False)
        backend = state.get("tx_resample_backend", "none")
        rs_on = state.get("tx_resample_enabled", True)

        if cap_rate <= 0:
            tx_rate_str = f"{GRAY}—{RESET}"
        elif not needs_r:
            tx_rate_str = f"{GREEN}{cap_rate/1000:g} kHz {cap_ch}ch (passthrough){RESET}"
        elif backend == "missing":
            tx_rate_str = (f"{RED}{cap_rate/1000:g} kHz {cap_ch}ch → 48 kHz "
                           f"(NO RESAMPLER — install soxr!){RESET}")
        elif rs_on:
            tx_rate_str = (f"{GREEN}{cap_rate/1000:g} kHz {cap_ch}ch → "
                           f"48 kHz mono ({backend}){RESET}")
        else:
            tx_rate_str = (f"{YELLOW}{cap_rate/1000:g} kHz {cap_ch}ch → 48 kHz "
                           f"RESAMPLE OFF ({backend}){RESET}")

        # --- RX rate status -------------------------------------------------
        rx_stream = state.get("rx_stream_rate", 0.0)
        rx_dev = state.get("rx_device_rate", 0.0)
        if not rx_stream:
            rx_rate_str = f"{GRAY}—{RESET}"
        elif rx_dev and abs(rx_dev - rx_stream) > 1.0:
            rx_rate_str = (f"{RED}{rx_stream/1000:g} kHz "
                           f"(device {rx_dev/1000:g} kHz!){RESET}")
        else:
            rx_rate_str = f"{GREEN}{rx_stream/1000:g} kHz{RESET}"

        # TX status
        tx_conn = state["tx_connected"]
        tx_live = state["tx_live"]
        tx_db = state.get("tx_db", -100.0)
        tx_vol = state["tx_vol"]
        if not tx_conn:
            tx_tag = f"{YELLOW}DISCONNECTED{RESET}"
        elif tx_live:
            tx_tag = f"{GREEN}  ON{RESET}"
        else:
            tx_tag = f"{YELLOW}MUTE{RESET}"
        tx_bar, tx_sdb = level_bar(tx_db, vol_pct=tx_vol)

        # RX status
        rx_conn = state["rx_connected"]
        rx_play = state["rx_play"]
        rx_db = state.get("rx_db", -100.0)
        rx_vol = state["rx_vol"]
        rx_from = state.get("rx_from")
        if not rx_conn:
            rx_tag = f"{YELLOW}WAITING{RESET}"
        elif rx_play:
            rx_tag = f"{GREEN}PLAY{RESET}"
        else:
            rx_tag = f"{YELLOW}MUTE{RESET}"
        rx_bar, rx_sdb = level_bar(rx_db, vol_pct=rx_vol)

        # MIC / broadcast-mix status
        mic_db = state.get("mic_db", -100.0)
        mic_vol = state.get("mic_vol", 100)
        duck_pct = state.get("duck_pct", 25)
        ptt = state.get("ptt_held", False)
        effected = state.get("effected", False)
        mic_bar, mic_sdb = level_bar(mic_db, vol_pct=mic_vol)
        if state.get("mic_error"):
            mic_tag = f"{RED}ERR{RESET}"
        elif ptt:
            mic_tag = f"{RED}● FX{RESET}" if effected else f"{RED}● TX{RESET}"
        else:
            mic_tag = f"{GRAY}idle{RESET}"
        talk_help = "tap = talk on/off"
        # Voice-effect field: show the selected effect and whether it's engaged.
        fx_idx = state.get("effect_index", 0)
        fx_name = state.get("effect_name") or effect_name(fx_idx)
        if effected and fx_idx != 0:
            fx_field = f"  FX:{GREEN}{fx_name}{RESET}"
        else:
            fx_field = f"  FX:{GRAY}off{RESET}"

        # --- Diagnostic block (always visible) -----------------------------
        xruns = state.get("tx_xruns", 0)
        xrun_color = RED if xruns else GRAY
        cb = state.get("tx_cb_stats") or {}
        cbs = cb.get("callbacks", 0)
        total_frames = cb.get("total_frames", 0)
        last_frames = cb.get("last_frames", 0)
        last_status = cb.get("last_status", "")
        gap_min = cb.get("gap_min")
        gap_max = cb.get("gap_max")
        gap_sum = cb.get("gap_sum", 0.0)
        gap_count = cb.get("gap_count", 0)
        gap_avg = (gap_sum / gap_count) if gap_count else None
        started = cb.get("started")
        elapsed = (time.monotonic() - started) if started else 0.0
        # Effective rate from total frames over elapsed wall time
        eff_rate = (total_frames / elapsed) if elapsed > 0 else 0.0
        # Top 3 most-common frames-per-callback values
        hist = cb.get("frames_hist") or {}
        top = sorted(hist.items(), key=lambda kv: -kv[1])[:3]
        hist_str = ", ".join(f"{k}×{v}" for k, v in top) if top else "—"

        def _ms(x):
            return f"{x*1000:.1f}ms" if x is not None else "—"

        # Effective-rate sanity color: red if it diverges from the requested
        # capture rate by more than 2%.
        cap_rate = state.get("tx_capture_rate", 0.0) or 1.0
        if eff_rate > 0:
            err = abs(eff_rate - cap_rate) / cap_rate
            eff_color = RED if err > 0.02 else GREEN
        else:
            eff_color = GRAY

        # WAV recording status
        wav_path = state.get("tx_wav_path")
        wav_bytes = state.get("tx_wav_bytes", 0)
        if wav_path:
            wav_secs = wav_bytes / (SAMPLE_RATE * 2)
            wav_line = (f"  {RED}● REC{RESET} {os.path.basename(wav_path)}  "
                        f"{wav_secs:6.1f} s  ({wav_bytes/1024:.1f} KB)\n")
        else:
            wav_line = ""

        # GPU utilisation graph (only when an Nvidia GPU / nvidia-smi is present).
        # Mirror the TX/RX/MIC layout exactly so the four graphs line up: a
        # COLORED status tag in the same column (its ANSI codes contribute the
        # same hidden width as the other tags, so `:>20s` yields the same visible
        # width and the bracket lands in the same place), then the 20-wide graph,
        # then the numbers — matching "[bar] <value> ...".
        if state.get("gpu_available"):
            g_util = state.get("gpu_util", 0.0)
            g_mu = state.get("gpu_mem_used", 0.0)
            g_mt = state.get("gpu_mem_total", 0.0)
            g_name = state.get("gpu_name", "GPU")
            g_spark = sparkline(state.get("gpu_hist", []), width=20)
            g_color = GREEN if g_util >= 5 else GRAY
            gpu_tag = (f"{GREEN}● active{RESET}" if g_util >= 5
                       else f"{GRAY}idle{RESET}")
            gpu_line = (f"  GPU {gpu_tag:>20s}  [{g_color}{g_spark}{RESET}] "
                        f"{g_util:5.1f}% util  {g_mu:.0f}/{g_mt:.0f} MiB  {g_name[:14]}\n")
        else:
            gpu_line = ""

        if rx_from:
            conn_line = f"     Gateway connected from {rx_from}\n"
        else:
            conn_line = f"     Listening on port {rx_port} ...\n"

        # Result of the last device swap (or the error that stopped it).
        pmsg = state.get("picker_msg")
        if pmsg:
            conn_line += f"     {pmsg}\n"

        # Render full frame each refresh — no absolute cursor positioning
        # (avoids the meter rendering over a wrapped header line).
        frame = (
            "\033[2J\033[H"
            f"{BOLD}Radio Gateway — Full Duplex Audio Client{RESET}\n"
            f"\n"
            f"  TX feed   : {in_dev_name}\n"
            f"              {tx_rate_str}\n"
            f"  RX speaker: {out_dev_name}\n"
            f"              {rx_rate_str}\n"
            f"  Mic       : {mic_dev_name}\n"
            f"  Gateway   : {gateway_host}  (TX→{tx_port}  RX←{rx_port})\n"
            f"{update_status_line()}\n"
            f"\n"
            f"  {BOLD}Controls{RESET}\n"
            f"   {WHITE}MIC{RESET}  your voice — mixed in only while you talk:\n"
            f"        {CYAN}SPACE{RESET} {talk_help:<14s} {CYAN}-{RESET}/{CYAN}={RESET} mic gain      "
            f"{CYAN}v{RESET} voice effect on/off   {CYAN}n{RESET}/{CYAN}m{RESET} prev/next effect\n"
            f"   {WHITE}TX {RESET}  program feed — sent to the gateway continuously:\n"
            f"        {CYAN}l{RESET} on/mute        {CYAN}<{RESET}/{CYAN}>{RESET} volume      "
            f"{CYAN};{RESET}/{CYAN}'{RESET} duck (dip while you talk)   {CYAN}r{RESET} resample   {CYAN}w{RESET} rec WAV\n"
            f"   {WHITE}RX {RESET}  speaker — audio received from the gateway:\n"
            f"        {CYAN}p{RESET} play/mute      {CYAN}[{RESET}/{CYAN}]{RESET} volume      "
            f"{CYAN}d{RESET} diagnostics            {CYAN}Ctrl+C{RESET} quit\n"
            f"   {WHITE}DEV{RESET}  swap a device on the fly (audio keeps running):\n"
            f"        {CYAN}i{RESET} TX feed        {CYAN}k{RESET} mic         "
            f"{CYAN}o{RESET} RX speaker\n"
            f"\n"
            f"  TX  {tx_tag:>20s}  [{tx_bar}] {tx_sdb:+6.1f} dBFS  Vol:{tx_vol:3d}%\n"
            f"  RX  {rx_tag:>20s}  [{rx_bar}] {rx_sdb:+6.1f} dBFS  Vol:{rx_vol:3d}%\n"
            f"  MIC {mic_tag:>20s}  [{mic_bar}] {mic_sdb:+6.1f} dBFS  "
            f"Gain:{mic_vol:4d}%  Duck:{duck_pct:3d}%{fx_field}\n"
            f"{gpu_line}"
            f"{conn_line}"
        )

        # Compact one-line diag (always visible). Anything unhealthy lights
        # up in red so problems are obvious at a glance.
        q_drops = state.get("tx_q_drops", 0)
        cps = cbs / elapsed if elapsed > 0 else 0.0
        rate_field = f"{eff_color}{eff_rate:.0f}Hz{RESET}"
        drops_field = (f"{RED}drops={q_drops}{RESET}" if q_drops
                       else f"{GRAY}drops=0{RESET}")
        xrun_field = (f"{RED}xrun={xruns}{RESET}" if xruns
                      else f"{GRAY}xrun=0{RESET}")
        frame += (
            f"     {GRAY}TX: {cps:5.1f}cb/s  "
            f"q={state.get('tx_q_depth',0)}  "
            f"{drops_field}{GRAY}  {xrun_field}{GRAY}  "
            f"rate={rate_field}{GRAY}  "
            f"iter={state.get('tx_iter_ms',0):.1f}ms"
            f"{RESET}  {CYAN}d{RESET}=detail\n"
        )

        # Expanded diag (toggled with 'd')
        if state.get("show_diag", False):
            frame += (
                f"\n"
                f"  {BOLD}TX capture diagnostics{RESET}\n"
                f"     callbacks  : {cbs}   elapsed: {elapsed:6.2f}s\n"
                f"     frames/cb  : last={last_frames}   hist: {hist_str}\n"
                f"     total frms : {total_frames}\n"
                f"     eff. rate  : {eff_color}{eff_rate:8.1f} Hz{RESET}   "
                f"(requested {cap_rate:.0f} Hz)\n"
                f"     cb gaps    : min={_ms(gap_min)}  "
                f"avg={_ms(gap_avg)}  max={_ms(gap_max)}\n"
                f"     xruns      : {xrun_color}{xruns}{RESET}   "
                f"last status: {last_status or '—'}\n"
                f"     q drops    : {(RED if q_drops else GRAY)}{q_drops}{RESET}   "
                f"q depth: {state.get('tx_q_depth',0)}\n"
                f"     loop time  : iter={state.get('tx_iter_ms',0):.1f}ms  "
                f"send={state.get('tx_send_ms',0):.1f}ms\n"
            )

        frame += f"{wav_line}"
        sys.stdout.write(frame)
        sys.stdout.flush()

        time.sleep(0.25)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def diag_capture(seconds=5):
    """Capture raw audio at the input device's native rate. No resampling,
    no network, no GUI. Writes a WAV at the device's native rate and prints
    per-callback statistics. Use this to isolate capture problems from
    everything downstream.
    """
    import queue, collections
    here = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(here, "diag_output.txt")
    log_fh = open(log_path, "w", encoding="utf-8")
    def log(msg=""):
        """Print a line and append it to diag_output.txt."""
        print(msg, flush=True)
        log_fh.write(msg + "\n")
        log_fh.flush()
    cfg = load_config()
    in_dev_index, in_dev_name = choose_input_device(cfg)
    dev_info = sd.query_devices(in_dev_index)
    capture_rate = int(round(float(dev_info["default_samplerate"]))) or 48000
    max_in_ch = int(dev_info["max_input_channels"])
    capture_channels = 2 if max_in_ch >= 2 else 1
    capture_block = max(1, int(round(capture_rate * FRAMES_PER_BUFFER / SAMPLE_RATE)))

    log(f"\n[diag] device: {in_dev_name}")
    log(f"[diag] device info: {dev_info}")
    log(f"[diag] capture: rate={capture_rate} ch={capture_channels} "
        f"block={capture_block}  (recording {seconds}s)\n")

    q = queue.Queue()
    frame_sizes = collections.Counter()
    status_msgs = []
    t_start = [None]
    t_last = [None]
    gap_log = []
    total_frames = [0]

    def cb(indata, frames, time_info, status):
        """Sound device callback: record timing, block size and status, queue the data."""
        now = time.monotonic()
        if t_start[0] is None:
            t_start[0] = now
        if t_last[0] is not None:
            gap_log.append(now - t_last[0])
        t_last[0] = now
        if status:
            status_msgs.append(str(status))
        frame_sizes[frames] += 1
        total_frames[0] += frames
        q.put(bytes(indata))

    stream = sd.RawInputStream(
        samplerate=capture_rate,
        blocksize=capture_block,
        device=in_dev_index,
        channels=capture_channels,
        dtype="int16",
        callback=cb,
    )
    stream.start()
    log(f"[diag] stream actual: rate={stream.samplerate} latency={stream.latency}")
    log(f"[diag] recording...")

    deadline = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < deadline:
        try:
            chunks.append(q.get(timeout=0.5))
        except queue.Empty:
            pass

    stream.stop()
    stream.close()

    elapsed = (t_last[0] - t_start[0]) if t_start[0] else 0.0
    raw = b"".join(chunks)
    expected_frames = int(elapsed * capture_rate)
    log(f"\n[diag] callbacks received   : {sum(frame_sizes.values())}")
    log(f"[diag] frames-per-callback   : {dict(frame_sizes)}")
    log(f"[diag] total frames captured : {total_frames[0]}")
    log(f"[diag] elapsed wall time     : {elapsed:.3f} s")
    log(f"[diag] expected frames @rate : {expected_frames}  "
        f"(ratio: {total_frames[0]/max(1,expected_frames):.3f})")
    log(f"[diag] status flags seen     : "
        f"{collections.Counter(status_msgs) if status_msgs else 'none'}")
    if gap_log:
        gmin = min(gap_log); gmax = max(gap_log)
        gavg = sum(gap_log) / len(gap_log)
        log(f"[diag] inter-callback gaps   : min={gmin*1000:.1f}ms "
            f"avg={gavg*1000:.1f}ms max={gmax*1000:.1f}ms  (n={len(gap_log)})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"diag_raw_{capture_rate}_{capture_channels}ch_{ts}.wav",
    )
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(capture_channels)
        w.setsampwidth(2)
        w.setframerate(capture_rate)
        w.writeframes(raw)
    log(f"\n[diag] wrote raw capture → {wav_path}")
    log(f"[diag] open this WAV at {capture_rate} Hz / {capture_channels}ch — "
        f"if it sounds choppy, the problem is capture itself, not resampling.")
    log(f"\n[diag] full report saved to: {log_path}")
    try:
        log_fh.close()
    except Exception:
        pass


def main():
    """Entry point: choose devices, start the TX / mic / RX / display threads.

    Usage: windows_audio_client.py [gateway_host] [--no-update] [diag].
    The host falls back to the saved config, then a prompt. Any argument
    containing 'diag' runs diag_capture() instead. A self-update check runs
    before any audio device is opened unless --no-update is given or
    updates are disabled in the config.
    """
    # Persist startup info to a file so it survives any later screen clears.
    here = os.path.dirname(os.path.abspath(__file__))
    startup_log = os.path.join(here, "startup.log")
    try:
        with open(startup_log, "w", encoding="utf-8") as f:
            f.write(f"argv = {sys.argv!r}\n")
            f.write(f"cwd  = {os.getcwd()!r}\n")
            f.write(f"file = {__file__!r}\n")
    except Exception:
        pass

    sys.stderr.write(f"[startup] argv = {sys.argv!r}\n")
    sys.stderr.flush()

    # Diagnostic mode: pure capture, no network, no GUI. Permissive arg match.
    want_diag = any("diag" in a.lower() for a in sys.argv[1:])
    if want_diag:
        sys.stderr.write("[startup] entering diag_capture()\n")
        sys.stderr.flush()
        try:
            diag_capture()
        except Exception as e:
            import traceback
            sys.stderr.write(f"[diag] CRASHED: {type(e).__name__}: {e}\n")
            traceback.print_exc(file=sys.stderr)
        try:
            input("\nPress Enter to exit ")
        except Exception:
            pass
        return

    cfg = load_config()

    # --- Latency tuning (config-overridable) --------------------------------
    global AUDIO_LATENCY, TX_BLOCK_MS
    AUDIO_LATENCY = cfg.get("audio_latency", AUDIO_LATENCY)
    try:
        TX_BLOCK_MS = max(5, int(cfg.get("tx_block_ms", TX_BLOCK_MS)))
    except (TypeError, ValueError):
        pass

    # --- Gateway host -------------------------------------------------------
    gateway_host = None
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        gateway_host = sys.argv[1]
    if not gateway_host:
        gateway_host = cfg.get("gateway_host")
    if not gateway_host:
        gateway_host = input("Gateway host (IP or hostname): ").strip()
        if not gateway_host:
            print("No host provided.")
            sys.exit(1)

    # --- Ports --------------------------------------------------------------
    tx_port = cfg.get("tx_port", DEFAULT_TX_PORT)
    rx_port = cfg.get("rx_port", DEFAULT_RX_PORT)
    web_port = int(cfg.get("web_port", DEFAULT_WEB_PORT))

    # --- Self-update --------------------------------------------------------
    # Before any audio device is opened, so a relaunch never has to hand a
    # device back to the OS. --no-update is set on the relaunch itself, which
    # is what stops an update from looping.
    if not cfg.get("update_enabled", True):
        UPDATE_STATE.update(status="disabled")
    elif "--no-update" in sys.argv[1:]:
        if os.environ.get(UPDATE_ENV):
            # The relaunch after a successful update.
            UPDATE_STATE.update(status="updated")
        else:
            # A manual --no-update, or a relaunch by a build too old to set the
            # marker. Confirm against the gateway rather than reporting
            # "not checked" for code that may well be current.
            UPDATE_STATE.update(status="skipped")
            verify_version_only(gateway_host, web_port)
    else:
        cfg["gateway_host"] = gateway_host
        cfg["web_port"] = web_port
        try:
            save_config(cfg)
        except Exception:
            pass
        if check_for_update(gateway_host, web_port):
            relaunch()

    # Hash from disk, so the line reports the code that is actually running
    # rather than whatever the check happened to report.
    try:
        UPDATE_STATE["version"] = _local_version()
    except Exception:
        pass

    # --- Audio devices ------------------------------------------------------
    # in_dev = program feed; mic = operator microphone (voice effects applied
    # to it in-client). out_dev = speaker.
    try:
        in_dev_index, in_dev_name = choose_input_device(cfg)
        out_dev_index, out_dev_name = choose_output_device(cfg)
        mic_dev_index, mic_dev_name = choose_mic_device(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

    # --- Save config --------------------------------------------------------
    cfg["gateway_host"] = gateway_host
    cfg["tx_port"] = tx_port
    cfg["rx_port"] = rx_port
    cfg["tx_device_name"] = in_dev_name
    cfg["rx_device_name"] = out_dev_name
    cfg["mic_device_name"] = mic_dev_name
    save_config(cfg)

    # --- Shared state -------------------------------------------------------
    state = {
        "running": True,
        "tx_live": False,
        "rx_play": True,
        "tx_vol": 100,
        "rx_vol": 100,
        "tx_connected": False,
        "rx_connected": False,
        "tx_db": -100.0,
        "rx_db": -100.0,
        "rx_from": None,
        "tx_resample_enabled": True,
        "tx_xruns": 0,
        "tx_last_frames": 0,
        # device picker ('i' / 'k' / 'o') — see PICKERS
        "picker": None,
        "picker_msg": "",
        "tx_dev_name": in_dev_name,
        "rx_dev_name": out_dev_name,
        "mic_dev_name": mic_dev_name,
        # mic / broadcast-mix state
        "ptt_key": cfg.get("ptt_key", "space"),
        "ptt_held": False,
        "mic_tx_active": False,
        "mic_db": -100.0,
        "mic_vol": int(cfg.get("mic_vol", 100)),
        "duck_pct": int(cfg.get("duck_pct", 25)),
        "effect_index": int(cfg.get("effect_index", 0)),
        "effected": int(cfg.get("effect_index", 0)) != 0,
        "effect_name": effect_name(int(cfg.get("effect_index", 0))),
    }

    # FIFO carrying 48 kHz mono mic PCM from the mic thread to the mixer.
    # Small cap (~200 ms) as a backstop against latency build-up; the mixer also
    # clears it whenever PTT is released so each over starts fresh.
    mic_fifo = SampleFIFO(maxlen=FRAMES_PER_BUFFER * 4)

    # --- Start threads ------------------------------------------------------
    threads = [
        threading.Thread(target=_keyboard_listener, args=(state,), daemon=True),
        threading.Thread(target=_tx_thread_func, args=(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name, mic_fifo), daemon=True, name="TX"),
        threading.Thread(target=_mic_thread_func, args=(state, cfg, mic_dev_index, mic_dev_name, mic_fifo), daemon=True, name="MIC"),
        threading.Thread(target=_rx_thread_func, args=(state, cfg, rx_port, out_dev_index, out_dev_name), daemon=True, name="RX"),
        threading.Thread(target=_display_thread_func, args=(state, gateway_host, tx_port, rx_port, in_dev_name, out_dev_name, mic_dev_name), daemon=True, name="Display"),
        threading.Thread(target=_gpu_monitor_thread, args=(state,), daemon=True, name="GPU"),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nShutting down.")
        state["running"] = False
        time.sleep(0.3)
        # Persist the last mic level / duck / effect so they survive a restart.
        cfg["mic_vol"] = state.get("mic_vol", 100)
        cfg["duck_pct"] = state.get("duck_pct", 25)
        cfg["effect_index"] = state.get("effect_index", 0)
        try:
            save_config(cfg)
        except Exception:
            pass


if __name__ == "__main__":
    main()
