#!/usr/bin/env python3
"""Windows Audio Client for Mumble Radio Gateway.

Captures audio from a local Windows input device (e.g. VB-Audio Virtual Cable)
and sends it over TCP to the gateway.

Two modes:
  SDR input source     — connects to gateway port 9600 (Remote Audio link).
                         Audio enters the mixer as an SDR-style source with
                         ducking and priority support.
  Announcement source  — connects to gateway port 9601 (Announcement Input).
                         Audio triggers PTT and is transmitted over the radio.
                         Silence is ignored so PTT is only active during speech.

Protocol: length-prefixed PCM — [4-byte big-endian uint32 length][PCM payload]
Audio: 48000 Hz, mono, 16-bit signed little-endian PCM, 2400 frames per chunk.

Keyboard controls:
  l = Toggle LIVE/IDLE — when LIVE, real audio is sent; when IDLE, silence is sent

Usage:
    pip install sounddevice
    python windows_audio_client.py [gateway_host] [gateway_port]

On first run the script will prompt for mode, audio device, and gateway host,
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

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice is required.  Install it with:  python -m pip install sounddevice")
    sys.exit(1)

import numpy as np

# ---------------------------------------------------------------------------
# Constants — must match gateway defaults
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48000
CHANNELS = 1
FRAMES_PER_BUFFER = 2400  # 2400 frames x 2 bytes = 4800 bytes per chunk
RECONNECT_INTERVAL = 5  # seconds between connection attempts
SILENCE = b'\x00' * (FRAMES_PER_BUFFER * 2)  # 4800 bytes of silence

MODE_SDR = "sdr"
MODE_ANNOUNCE = "announce"
DEFAULT_PORTS = {MODE_SDR: 9600, MODE_ANNOUNCE: 9601}
MODE_LABELS = {MODE_SDR: "SDR input source", MODE_ANNOUNCE: "Announcement source"}

# ANSI colors
RED = "\033[91m"
GREEN = "\033[92m"
WHITE = "\033[97m"
GRAY = "\033[90m"
RESET = "\033[0m"

CONFIG_FILENAME = "windows_audio_client.json"

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    path = _config_path()
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Keyboard input (cross-platform)
# ---------------------------------------------------------------------------
def _keyboard_listener(state):
    """Background thread: read single keypresses and update shared state."""
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
                if ch == "l":
                    state["live"] = not state["live"]
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
                    if ch == "l":
                        state["live"] = not state["live"]
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
def list_input_devices():
    """Return list of (index, name, max_input_channels) for input devices."""
    devices = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_input_channels"]))
    return devices


def find_device_by_name(name):
    """Return device index matching *name*, or None."""
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and d["name"] == name:
            return d["index"]
    return None


def choose_mode(cfg):
    """Resolve or prompt for operating mode.  Returns MODE_SDR or MODE_ANNOUNCE."""
    saved = cfg.get("mode")
    if saved in (MODE_SDR, MODE_ANNOUNCE):
        return saved

    print("\nOperating mode:")
    print(f"  1) SDR input source      (port {DEFAULT_PORTS[MODE_SDR]})  — audio mixed into Mumble stream")
    print(f"  2) Announcement source   (port {DEFAULT_PORTS[MODE_ANNOUNCE]})  — audio transmitted over radio via PTT")

    while True:
        try:
            choice = input("\nSelect mode [1]: ").strip()
            if choice in ("", "1"):
                return MODE_SDR
            if choice == "2":
                return MODE_ANNOUNCE
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


def choose_device(cfg):
    """Resolve or prompt for an input device.  Returns (index, name)."""
    saved_name = cfg.get("device_name")
    if saved_name:
        idx = find_device_by_name(saved_name)
        if idx is not None:
            return idx, saved_name
        print(f"Saved device not found: {saved_name}")

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        sys.exit(1)

    print("\nAvailable input devices:")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nSelect device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")

# ---------------------------------------------------------------------------
# Level meter
# ---------------------------------------------------------------------------
def rms_db(pcm_bytes):
    """Compute RMS level in dBFS from 16-bit LE PCM."""
    n_samples = len(pcm_bytes) // 2
    if n_samples == 0:
        return -100.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(samples * samples))
    if rms < 1:
        return -100.0
    return 20.0 * math.log10(rms / 32768.0)


def level_bar(db, width=20):
    """Return a simple ASCII level bar."""
    # Map -60..0 dBFS to 0..width
    clamped = max(-60.0, min(0.0, db))
    filled = int((clamped + 60.0) / 60.0 * width)
    return "#" * filled + "-" * (width - filled)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()

    # --- Operating mode -----------------------------------------------------
    try:
        mode = choose_mode(cfg)
    except KeyboardInterrupt:
        sys.exit(0)
    cfg["mode"] = mode
    default_port = DEFAULT_PORTS[mode]

    # --- Resolve gateway host/port from args, config, or prompt -----------
    host = None
    port = None
    if len(sys.argv) >= 2:
        host = sys.argv[1]
    if len(sys.argv) >= 3:
        try:
            port = int(sys.argv[2])
        except ValueError:
            print(f"Invalid port: {sys.argv[2]}")
            sys.exit(1)

    if not host:
        host = cfg.get("gateway_host")
    if not port:
        port = cfg.get("gateway_port")

    if not host:
        host = input("Gateway host (IP or hostname): ").strip()
        if not host:
            print("No host provided.")
            sys.exit(1)
    if not port:
        port_str = input(f"Gateway port [{default_port}]: ").strip()
        port = int(port_str) if port_str else default_port

    port = int(port)

    # --- Audio device -------------------------------------------------------
    try:
        dev_index, dev_name = choose_device(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

    # Save config
    cfg["device_name"] = dev_name
    cfg["gateway_host"] = host
    cfg["gateway_port"] = port
    save_config(cfg)

    print(f"\nMode   : {MODE_LABELS[mode]}")
    print(f"Device : {dev_name} (index {dev_index})")
    print(f"Gateway: {host}:{port}")
    print(f"Format : {SAMPLE_RATE} Hz, mono, 16-bit, {FRAMES_PER_BUFFER} frames/chunk")
    print(f"\nPress 'l' to toggle LIVE/IDLE — audio is NOT sent until you go LIVE")
    print("Press Ctrl+C to stop.\n")

    # --- Shared state for keyboard thread -----------------------------------
    state = {"live": False, "running": True}

    # Start keyboard listener
    kb_thread = threading.Thread(target=_keyboard_listener, args=(state,), daemon=True)
    kb_thread.start()

    # --- Open audio stream --------------------------------------------------
    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAMES_PER_BUFFER,
        device=dev_index,
        channels=CHANNELS,
        dtype="int16",
    )
    stream.start()

    sock = None

    def connect():
        """Attempt TCP connection to gateway.  Returns socket or None."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((host, port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            return s
        except Exception as e:
            print(f"\rConnect failed: {e}" + " " * 20)
            try:
                s.close()
            except Exception:
                pass
            return None

    try:
        while True:
            # Connect / reconnect
            if sock is None:
                print(f"Connecting to {host}:{port} ...")
                sock = connect()
                if sock is None:
                    # Keep reading (and discarding) audio so the stream doesn't stall
                    deadline = time.monotonic() + RECONNECT_INTERVAL
                    while time.monotonic() < deadline:
                        try:
                            stream.read(FRAMES_PER_BUFFER)
                        except Exception:
                            pass
                    continue
                print(f"Connected to {host}:{port}")

            # Read audio
            try:
                data, overflowed = stream.read(FRAMES_PER_BUFFER)
                pcm = bytes(data)
            except Exception as e:
                print(f"\nAudio read error: {e}")
                break

            # Choose what to send based on LIVE state
            is_live = state["live"]
            send_pcm = pcm if is_live else SILENCE

            # Send
            try:
                header = struct.pack(">I", len(send_pcm))
                sock.sendall(header + send_pcm)
            except Exception:
                print(f"\nDisconnected from {host}:{port}")
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
                continue

            # Status line: state + level meter
            if is_live:
                status = f"{RED}LIVE{RESET}"
            else:
                status = f"{GREEN}IDLE{RESET}"
            db = rms_db(pcm)
            bar = level_bar(db)
            sys.stdout.write(f"\r  {status}  [{bar}] {db:+6.1f} dBFS ")
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\nShutting down.")
    finally:
        state["running"] = False
        stream.stop()
        stream.close()
        if sock:
            try:
                sock.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
