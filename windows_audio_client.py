#!/usr/bin/env python3
"""Windows Audio Client for Mumble Radio Gateway.

Two roles (consistent with gateway REMOTE_AUDIO_ROLE):

  server — SENDS audio TO the gateway.
           Audio flow: Windows input device → TCP → Gateway
           Client connects out to gateway port.

  client — RECEIVES audio FROM the gateway.
           Audio flow: Gateway → TCP → Windows output device
           Client listens on a port; gateway connects in and pushes audio.

Server-role modes:
  SDR input source     — connects to gateway port 9600 (Remote Audio link).
                         Audio enters the mixer as an SDR-style source with
                         ducking and priority support.
  Announcement source  — connects to gateway port 9601 (Announcement Input).
                         Audio triggers PTT and is transmitted over the radio.
                         Silence is ignored so PTT is only active during speech.

Protocol: length-prefixed PCM — [4-byte big-endian uint32 length][PCM payload]
Audio: 48000 Hz, mono, 16-bit signed little-endian PCM, 2400 frames per chunk.

Keyboard controls:
  l = Toggle LIVE/IDLE (server) or LIVE/MUTE (client)
  m = Switch between server and client roles

Usage:
    pip install sounddevice
    python windows_audio_client.py [host] [port]

On first run the script will prompt for role, mode, audio device, and host,
then save the selection to windows_audio_client.json alongside this script.
Each role has its own saved settings (devices, host/port) in the config file.
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

ROLE_SERVER = "server"
ROLE_CLIENT = "client"

MODE_SDR = "sdr"
MODE_ANNOUNCE = "announce"
DEFAULT_PORTS = {MODE_SDR: 9600, MODE_ANNOUNCE: 9601}
MODE_LABELS = {MODE_SDR: "SDR input source", MODE_ANNOUNCE: "Announcement source"}

ROLE_LABELS = {ROLE_SERVER: "Server (send audio)", ROLE_CLIENT: "Client (receive audio)"}

# ANSI colors
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
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
                elif ch == "m":
                    state["switch_role"] = True
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
                    elif ch == "m":
                        state["switch_role"] = True
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


def list_output_devices():
    """Return list of (index, name, max_output_channels) for output devices."""
    devices = []
    for d in sd.query_devices():
        if d["max_output_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_output_channels"]))
    return devices


def find_device_by_name(name, output=False):
    """Return device index matching *name*, or None."""
    for d in sd.query_devices():
        if output:
            if d["max_output_channels"] > 0 and d["name"] == name:
                return d["index"]
        else:
            if d["max_input_channels"] > 0 and d["name"] == name:
                return d["index"]
    return None


def choose_role(cfg):
    """Resolve or prompt for role.  Returns ROLE_SERVER or ROLE_CLIENT."""
    saved = cfg.get("role")
    if saved in (ROLE_SERVER, ROLE_CLIENT):
        return saved

    print("\nRole:")
    print("  1) Server — send audio TO the gateway     (input device → TCP → gateway)")
    print("  2) Client — receive audio FROM the gateway (gateway → TCP → output device)")

    while True:
        try:
            choice = input("\nSelect role [1]: ").strip()
            if choice in ("", "1"):
                return ROLE_SERVER
            if choice == "2":
                return ROLE_CLIENT
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


def choose_mode(cfg):
    """Resolve or prompt for operating mode.  Returns MODE_SDR or MODE_ANNOUNCE."""
    saved = cfg.get("server_mode")
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


def choose_input_device(cfg):
    """Resolve or prompt for an input device.  Returns (index, name)."""
    saved_name = cfg.get("server_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=False)
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


def choose_output_device(cfg):
    """Resolve or prompt for an output device.  Returns (index, name)."""
    saved_name = cfg.get("client_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=True)
        if idx is not None:
            return idx, saved_name
        print(f"Saved output device not found: {saved_name}")

    devices = list_output_devices()
    if not devices:
        print("No output devices found.")
        sys.exit(1)

    print("\nAvailable output devices:")
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
# Server role — send audio to gateway
# ---------------------------------------------------------------------------
def run_server(cfg, state):
    """Send audio from local input device to the gateway over TCP.

    Returns True if user pressed 'm' to switch roles, False otherwise.
    """
    # --- Operating mode -----------------------------------------------------
    try:
        mode = choose_mode(cfg)
    except KeyboardInterrupt:
        return False
    cfg["server_mode"] = mode
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
            return False

    if not host:
        host = cfg.get("server_host")
    if not port:
        port = cfg.get("server_port")

    if not host:
        host = input("Gateway host (IP or hostname): ").strip()
        if not host:
            print("No host provided.")
            return False
    if not port:
        port_str = input(f"Gateway port [{default_port}]: ").strip()
        port = int(port_str) if port_str else default_port

    port = int(port)

    # --- Audio device -------------------------------------------------------
    try:
        dev_index, dev_name = choose_input_device(cfg)
    except KeyboardInterrupt:
        return False

    # Save config
    cfg["server_device_name"] = dev_name
    cfg["server_host"] = host
    cfg["server_port"] = port
    save_config(cfg)

    print(f"\nRole   : {CYAN}Server (send audio){RESET}")
    print(f"Mode   : {MODE_LABELS[mode]}")
    print(f"Device : {dev_name} (index {dev_index})")
    print(f"Gateway: {host}:{port}")
    print(f"Format : {SAMPLE_RATE} Hz, mono, 16-bit, {FRAMES_PER_BUFFER} frames/chunk")
    print(f"\nPress 'l' to toggle LIVE/IDLE — audio is NOT sent until you go LIVE")
    print(f"Press 'm' to switch to client role")
    print("Press Ctrl+C to stop.\n")

    # --- Reset state --------------------------------------------------------
    state["live"] = False
    state["switch_role"] = False

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

    switched = False
    try:
        while not state["switch_role"]:
            # Connect / reconnect
            if sock is None:
                print(f"Connecting to {host}:{port} ...")
                sock = connect()
                if sock is None:
                    # Keep reading (and discarding) audio so the stream doesn't stall
                    deadline = time.monotonic() + RECONNECT_INTERVAL
                    while time.monotonic() < deadline and not state["switch_role"]:
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
            role_tag = f"{CYAN}SV{RESET}"
            sys.stdout.write(f"\r  {role_tag} {status}  [{bar}] {db:+6.1f} dBFS ")
            sys.stdout.flush()

        switched = state["switch_role"]
    except KeyboardInterrupt:
        print("\n\nShutting down.")
    finally:
        stream.stop()
        stream.close()
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    return switched

# ---------------------------------------------------------------------------
# Client role — receive audio from gateway
# ---------------------------------------------------------------------------
def _recv_exact(sock, n):
    """Read exactly n bytes from socket.  Returns bytes or None on disconnect."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def run_client(cfg, state):
    """Receive audio from the gateway and play on local output device.

    Returns True if user pressed 'm' to switch roles, False otherwise.
    """
    # --- Resolve listen port from args, config, or prompt -------------------
    port = None
    if len(sys.argv) >= 3:
        try:
            port = int(sys.argv[2])
        except ValueError:
            print(f"Invalid port: {sys.argv[2]}")
            return False
    if not port:
        port = cfg.get("client_port")
    if not port:
        port_str = input(f"Listen port [{DEFAULT_PORTS[MODE_SDR]}]: ").strip()
        port = int(port_str) if port_str else DEFAULT_PORTS[MODE_SDR]
    port = int(port)

    # --- Output device ------------------------------------------------------
    try:
        dev_index, dev_name = choose_output_device(cfg)
    except KeyboardInterrupt:
        return False

    # Save config
    cfg["client_device_name"] = dev_name
    cfg["client_port"] = port
    save_config(cfg)

    print(f"\nRole   : {CYAN}Client (receive audio){RESET}")
    print(f"Device : {dev_name} (index {dev_index})")
    print(f"Listen : port {port}")
    print(f"Format : {SAMPLE_RATE} Hz, mono, 16-bit, {FRAMES_PER_BUFFER} frames/chunk")
    print(f"\nPress 'l' to toggle PLAY/MUTE — when MUTE, received audio is discarded")
    print(f"Press 'm' to switch to server role")
    print("Press Ctrl+C to stop.\n")

    # --- Reset state --------------------------------------------------------
    state["live"] = False
    state["switch_role"] = False

    # --- Listen socket ------------------------------------------------------
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind(("0.0.0.0", port))
    listen_sock.listen(1)
    listen_sock.settimeout(1.0)

    print(f"Listening on port {port} — waiting for gateway connection ...")

    switched = False
    try:
        while not state["switch_role"]:
            # Accept a connection
            try:
                conn, addr = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(0.2)  # Allow keypress checks during silence
            print(f"\nGateway connected from {addr[0]}:{addr[1]}")

            # Open output stream for this connection
            out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAMES_PER_BUFFER,
                device=dev_index,
                channels=CHANNELS,
                dtype="int16",
            )
            out_stream.start()

            try:
                while not state["switch_role"]:
                    # Read length prefix (timeout lets us check keypress state)
                    try:
                        hdr = _recv_exact(conn, 4)
                    except socket.timeout:
                        # No data — update status line and re-check state
                        if state["live"]:
                            status = f"{GREEN}PLAY{RESET}"
                        else:
                            status = f"{YELLOW}MUTE{RESET}"
                        role_tag = f"{CYAN}CL{RESET}"
                        sys.stdout.write(f"\r  {role_tag} {status}  [{level_bar(-100)}] {-100:+6.1f} dBFS ")
                        sys.stdout.flush()
                        continue
                    if hdr is None:
                        break
                    length = struct.unpack(">I", hdr)[0]
                    if length == 0 or length > 960000:
                        break

                    # Read PCM payload (blocking — header already confirmed data is coming)
                    conn.settimeout(None)
                    pcm = _recv_exact(conn, length)
                    conn.settimeout(0.2)
                    if pcm is None:
                        break

                    is_live = state["live"]

                    # Play or discard
                    if is_live:
                        out_stream.write(pcm)

                    # Status line
                    if is_live:
                        status = f"{GREEN}PLAY{RESET}"
                    else:
                        status = f"{YELLOW}MUTE{RESET}"
                    db = rms_db(pcm)
                    bar = level_bar(db)
                    role_tag = f"{CYAN}CL{RESET}"
                    sys.stdout.write(f"\r  {role_tag} {status}  [{bar}] {db:+6.1f} dBFS ")
                    sys.stdout.flush()

            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                out_stream.stop()
                out_stream.close()
                try:
                    conn.close()
                except Exception:
                    pass
                if not state["switch_role"]:
                    print(f"\nGateway disconnected — waiting for reconnect ...")

        switched = state["switch_role"]
    except KeyboardInterrupt:
        print("\n\nShutting down.")
    finally:
        listen_sock.close()

    return switched

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()

    # --- Role selection -----------------------------------------------------
    try:
        role = choose_role(cfg)
    except KeyboardInterrupt:
        sys.exit(0)
    cfg["role"] = role
    save_config(cfg)

    # Shared state lives across role switches — keyboard thread stays running
    state = {"live": False, "running": True, "switch_role": False}
    kb_thread = threading.Thread(target=_keyboard_listener, args=(state,), daemon=True)
    kb_thread.start()

    while True:
        if role == ROLE_SERVER:
            switched = run_server(cfg, state)
        else:
            switched = run_client(cfg, state)

        if not switched:
            break

        # Flip role
        role = ROLE_CLIENT if role == ROLE_SERVER else ROLE_SERVER
        cfg["role"] = role
        save_config(cfg)
        print(f"\n\n{'='*60}")
        print(f"  Switching to {CYAN}{ROLE_LABELS[role]}{RESET}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
