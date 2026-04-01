#!/usr/bin/env python3
"""Windows Audio Client for Radio Gateway — Full Duplex.

Runs both directions simultaneously:
  TX: Captures audio from a local input device and sends it to the gateway
      via TCP (connects out to gateway's REMOTE_AUDIO_RX_PORT, default 9602).
  RX: Listens on a local port for the gateway to connect in and push audio,
      then plays it on a local output device (gateway connects from port 9600).

Protocol: length-prefixed PCM — [4-byte big-endian uint32 length][PCM payload]
Audio: 48000 Hz, mono, 16-bit signed little-endian PCM, 2400 frames per chunk.

Keyboard controls:
  l = Toggle TX ON/MUTE (mic capture)
  p = Toggle RX PLAY/MUTE (speaker output)
  , (or <) = TX volume down 5%
  . (or >) = TX volume up 5%
  [ = RX volume down 5%
  ] = RX volume up 5%

Usage:
    pip install sounddevice numpy
    python windows_audio_client.py [gateway_host]

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

DEFAULT_TX_PORT = 9602  # Gateway's RX listen port (we connect out to this)
DEFAULT_RX_PORT = 9600  # Local listen port (gateway connects in on this)

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

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
def list_input_devices():
    devices = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_input_channels"]))
    return devices


def list_output_devices():
    devices = []
    for d in sd.query_devices():
        if d["max_output_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_output_channels"]))
    return devices


def find_device_by_name(name, output=False):
    for d in sd.query_devices():
        if output:
            if d["max_output_channels"] > 0 and d["name"] == name:
                return d["index"]
        else:
            if d["max_input_channels"] > 0 and d["name"] == name:
                return d["index"]
    return None


def choose_input_device(cfg):
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

    print("\nAvailable input devices (TX mic):")
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

    print("\nAvailable output devices (RX speaker):")
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
    n_samples = len(pcm_bytes) // 2
    if n_samples == 0:
        return -100.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(samples * samples))
    if rms < 1:
        return -100.0
    return 20.0 * math.log10(rms / 32768.0)


def level_bar(db, width=20, vol_pct=100):
    vol_frac = max(0.0, min(1.0, vol_pct / 100.0))
    if vol_frac > 0:
        scaled_db = db + 20.0 * math.log10(vol_frac)
    else:
        scaled_db = -100.0
    clamped = max(-60.0, min(0.0, scaled_db))
    filled = int((clamped + 60.0) / 60.0 * width)
    marker_pos = int(vol_frac * width)
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

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

# ---------------------------------------------------------------------------
# TX thread — capture mic and send to gateway
# ---------------------------------------------------------------------------
def _tx_thread_func(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name):
    """Capture mic audio and send to gateway's RX port."""
    import queue
    tx_q = queue.Queue(maxsize=32)

    def _tx_callback(indata, frames, time_info, status):
        """Called by sounddevice from audio thread — push PCM to queue."""
        try:
            tx_q.put_nowait(bytes(indata))
        except queue.Full:
            pass  # drop oldest implicitly by not queuing

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAMES_PER_BUFFER,
        device=in_dev_index,
        channels=CHANNELS,
        dtype="int16",
        callback=_tx_callback,
    )
    stream.start()

    sock = None

    def connect():
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

    try:
        while state["running"]:
            # Connect / reconnect
            if sock is None:
                state["tx_connected"] = False
                sock = connect()
                if sock is None:
                    # Drain queue while waiting to reconnect
                    deadline = time.monotonic() + RECONNECT_INTERVAL
                    while time.monotonic() < deadline and state["running"]:
                        try:
                            tx_q.get(timeout=0.1)
                        except queue.Empty:
                            pass
                    continue
                state["tx_connected"] = True

            # Get audio from callback queue
            try:
                pcm = tx_q.get(timeout=0.1)
            except queue.Empty:
                continue

            # Apply volume
            vol = state["tx_vol"]
            if vol < 100:
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                samples *= vol / 100.0
                pcm = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

            # Compute level for display
            state["tx_db"] = rms_db(pcm)

            # Send or send silence based on LIVE state
            send_pcm = pcm if state["tx_live"] else SILENCE
            try:
                header = struct.pack(">I", len(send_pcm))
                sock.sendall(header + send_pcm)
            except Exception:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
                continue
    finally:
        stream.stop()
        stream.close()
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        state["tx_connected"] = False

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

    try:
        while state["running"]:
            # Accept a connection
            try:
                conn, addr = listen_sock.accept()
            except socket.timeout:
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

            out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAMES_PER_BUFFER,
                device=out_dev_index,
                channels=CHANNELS,
                dtype="int16",
                callback=_rx_callback,
            )
            out_stream.start()

            try:
                while state["running"]:
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
    finally:
        listen_sock.close()

# ---------------------------------------------------------------------------
# Display thread — update status line
# ---------------------------------------------------------------------------
def _display_thread_func(state, gateway_host, tx_port, rx_port,
                         in_dev_name, out_dev_name):
    """Periodically redraw the status display."""
    def clear():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    last_header = ""
    while state["running"]:
        # Build header
        header = (
            f"{BOLD}Radio Gateway — Full Duplex Audio Client{RESET}\n"
            f"\n"
            f"  TX mic    : {in_dev_name}\n"
            f"  RX speaker: {out_dev_name}\n"
            f"  Gateway   : {gateway_host}  (TX→{tx_port}  RX←{rx_port})\n"
            f"\n"
            f"  Keys: {CYAN}l{RESET}=TX on/mute  {CYAN}p{RESET}=RX play/mute  "
            f"{CYAN}</>={RESET}TX vol  {CYAN}[/]={RESET}RX vol  Ctrl+C=quit\n"
        )

        if header != last_header:
            clear()
            sys.stdout.write(header)
            last_header = header

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

        # Move cursor to status area (line 9)
        sys.stdout.write(f"\033[9;1H")
        sys.stdout.write(
            f"  TX {tx_tag:>20s}  [{tx_bar}] {tx_sdb:+6.1f} dBFS  Vol:{tx_vol:3d}%   \n"
            f"  RX {rx_tag:>20s}  [{rx_bar}] {rx_sdb:+6.1f} dBFS  Vol:{rx_vol:3d}%   \n"
        )
        if rx_from:
            sys.stdout.write(f"     Gateway connected from {rx_from}   \n")
        else:
            sys.stdout.write(f"     Listening on port {rx_port} ...              \n")
        sys.stdout.flush()

        time.sleep(0.1)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()

    # --- Gateway host -------------------------------------------------------
    gateway_host = None
    if len(sys.argv) >= 2:
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

    # --- Audio devices ------------------------------------------------------
    try:
        in_dev_index, in_dev_name = choose_input_device(cfg)
        out_dev_index, out_dev_name = choose_output_device(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

    # --- Save config --------------------------------------------------------
    cfg["gateway_host"] = gateway_host
    cfg["tx_port"] = tx_port
    cfg["rx_port"] = rx_port
    cfg["tx_device_name"] = in_dev_name
    cfg["rx_device_name"] = out_dev_name
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
    }

    # --- Start threads ------------------------------------------------------
    threads = [
        threading.Thread(target=_keyboard_listener, args=(state,), daemon=True),
        threading.Thread(target=_tx_thread_func, args=(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name), daemon=True, name="TX"),
        threading.Thread(target=_rx_thread_func, args=(state, cfg, rx_port, out_dev_index, out_dev_name), daemon=True, name="RX"),
        threading.Thread(target=_display_thread_func, args=(state, gateway_host, tx_port, rx_port, in_dev_name, out_dev_name), daemon=True, name="Display"),
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


if __name__ == "__main__":
    main()
