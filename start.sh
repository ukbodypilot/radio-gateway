#!/bin/bash
# Radio Gateway — startup script

# Resolve script directory immediately (handles symlinks and relative invocation)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Ensure ~/.local/bin is in PATH (not present in non-login shells like desktop shortcuts)
[[ -d "$HOME/.local/bin" && ":$PATH:" != *":$HOME/.local/bin:"* ]] && export PATH="$HOME/.local/bin:$PATH"

# Capture all startup output so the gateway can load it into the web /logs viewer
STARTUP_LOG="/tmp/gateway_startup.log"
> "$STARTUP_LOG"  # truncate
# Log to file only (avoids process-substitution fork that causes duplicate gateway
# launches under nohup). Console output goes to /tmp/gateway_start_output.log via
# the nohup redirect in the caller.
exec >> "$STARTUP_LOG" 2>&1

# Timestamped echo — prepends [HH:MM:SS] to every message
ts() { echo "[$(date +%H:%M:%S)] $*"; }

# Read a value from gateway_config.txt (strips comments, whitespace, quotes)
read_config() {
    local key="$1" default="$2"
    local val
    val="$(grep -m1 "^[[:space:]]*${key}[[:space:]]*=" "$SCRIPT_DIR/gateway_config.txt" 2>/dev/null \
         | sed 's/^[^=]*=[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | sed 's/^"//;s/"$//' | tr -d "'")"
    if [ -z "$val" ]; then echo "$default"; else echo "$val"; fi
}

# Read startup options from config
ENABLE_STREAM_OUTPUT="$(read_config ENABLE_STREAM_OUTPUT false)"
START_TH9800_CAT="$(read_config START_TH9800_CAT false)"
TH9800_CAT_HEADLESS="$(read_config TH9800_CAT_HEADLESS false)"
START_CLAUDE_CODE="$(read_config START_CLAUDE_CODE false)"
HEADLESS_MODE="$(read_config HEADLESS_MODE false)"

echo "=========================================="
ts "Starting Radio Gateway"
echo "=========================================="
echo ""

# Cleanup function
cleanup() {
    echo ""
    ts "Cleaning up..."
    if [ ! -z "$TH9800_PID" ]; then
        kill $TH9800_PID 2>/dev/null
        ts "  Stopped TH-9800 CAT"
    fi
    if [ ! -z "$DARKICE_PID" ]; then
        kill $DARKICE_PID 2>/dev/null
        ts "  Stopped Darkice"
    fi
    if [ ! -z "$FFMPEG_PID" ]; then
        kill $FFMPEG_PID 2>/dev/null
        ts "  Stopped FFmpeg"
    fi
    if [ ! -z "$SUDO_KEEPALIVE_PID" ]; then
        kill $SUDO_KEEPALIVE_PID 2>/dev/null
    fi
    rm -f /tmp/darkice_audio 2>/dev/null
    try_sudo modprobe -r snd-aloop 2>/dev/null
    # Restore terminal from raw mode (gateway sets cbreak for keyboard controls)
    stty sane 2>/dev/null
    ts "Done"
    exit
}

trap cleanup INT TERM

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    ts "Warning: Running as root may cause permission issues"
    echo ""
fi

# Cache sudo credentials (skip if no TTY — running as systemd service)
if sudo -n true 2>/dev/null; then
    HAVE_SUDO=true
elif [ -t 0 ]; then
    sudo -v && HAVE_SUDO=true || HAVE_SUDO=false
    if [ "$HAVE_SUDO" = "true" ]; then
        ( while true; do sudo -n -v 2>/dev/null; sleep 50; done ) &
        SUDO_KEEPALIVE_PID=$!
    fi
else
    HAVE_SUDO=false
    ts "  No TTY — sudo commands will be skipped (running as service)"
fi

# Wrapper: run command with sudo if available, skip otherwise
try_sudo() { if [ "$HAVE_SUDO" = "true" ]; then sudo "$@"; else return 0; fi; }

# 1. Kill any existing processes
ts "[1/11] Checking for existing processes..."
pkill -9 darkice 2>/dev/null && ts "  Killed existing Darkice"
pkill -9 ffmpeg 2>/dev/null && ts "  Killed existing FFmpeg"
sleep 1

# Also kill any Python gateway processes (just in case)
pkill -9 -f "radio_gateway" 2>/dev/null && ts "  Killed existing gateway"

# Stop leftover mumble-server instances from prior gateway runs so they don't
# linger on stale ports (the gateway will start fresh ones with current config)
for svc in mumble-server-gw1 mumble-server-gw2; do
    if systemctl is-active --quiet "$svc.service" 2>/dev/null; then
        try_sudo systemctl stop "$svc.service" 2>/dev/null && ts "  Stopped $svc"
    fi
done
sleep 1

# 2. Start Mumble GUI client if not already running (skip in headless mode)
ts "[2/11] Checking Mumble client..."
if [ "$HEADLESS_MODE" = "true" ]; then
    ts "  Skipped — headless mode"
elif pgrep -x "mumble" > /dev/null 2>&1; then
    ts "  Mumble already running (PID: $(pgrep -x mumble | head -1))"
else
    if command -v mumble > /dev/null 2>&1; then
        mumble > /dev/null 2>&1 &
        disown
        sleep 2
        if pgrep -x "mumble" > /dev/null 2>&1; then
            ts "  Mumble started (PID: $(pgrep -x mumble | head -1))"
        else
            ts "  Mumble failed to start (continuing anyway)"
        fi
    else
        ts "  Mumble not installed (skipping)"
    fi
fi

# 3. Start TH-9800 CAT control if not already running
ts "[3/11] Checking TH-9800 CAT control..."
if [ "$START_TH9800_CAT" != "true" ]; then
    ts "  Disabled in config (START_TH9800_CAT = false)"
elif pgrep -f "TH9800_CAT.py" > /dev/null 2>&1; then
    TH9800_PID=$(pgrep -f TH9800_CAT.py | head -1)
    ts "  TH-9800 CAT already running (PID: $TH9800_PID)"
else
    # Search for any folder with "th9800" in its name (case-insensitive)
    TH9800_DIR=""
    for d in "$HOME/Downloads"/*/; do
        base="$(basename "$d")"
        if echo "$base" | grep -qi "th9800"; then
            TH9800_DIR="$d"
            break
        fi
    done

    if [ -n "$TH9800_DIR" ] && [ -f "$TH9800_DIR/TH9800_CAT.py" ]; then
        ts "  Found TH-9800 at: $TH9800_DIR"

        if [ "$TH9800_CAT_HEADLESS" = "true" ]; then
            # Headless mode: no GUI, no dearpygui dependency
            # Read serial device and baud rate from TH9800_CAT config.txt
            TH9800_CFG="$TH9800_DIR/config.txt"
            TH9800_BAUD="19200"
            TH9800_DEVICE=""
            if [ -f "$TH9800_CFG" ]; then
                TH9800_BAUD="$(grep -m1 '^baud_rate=' "$TH9800_CFG" | cut -d= -f2 | tr -d ' ')"
                TH9800_DEVICE_NAME="$(grep -m1 '^device=' "$TH9800_CFG" | cut -d= -f2 | sed 's/^[[:space:]]*//')"
                [ -z "$TH9800_BAUD" ] && TH9800_BAUD="19200"
            fi
            # Find COM port matching device name from config
            if [ -n "$TH9800_DEVICE_NAME" ]; then
                TH9800_DEVICE="$(python3 -c "
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    if '$TH9800_DEVICE_NAME' in p.description:
        print(p.device)
        break
" 2>/dev/null)"
            fi
            if [ -z "$TH9800_DEVICE" ]; then
                ts "  No serial device matching '$TH9800_DEVICE_NAME' found"
                ts "  Available ports:"
                python3 -c "import serial.tools.list_ports; [print(f'    {p.device}: {p.description}') for p in serial.tools.list_ports.comports() if 'ttyUSB' in p.device or 'ttyACM' in p.device]" 2>/dev/null
            else
                CAT_PORT="$(read_config CAT_PORT 9800)"
                CAT_PASSWORD="$(read_config CAT_PASSWORD "")"
                ts "  Starting headless: $TH9800_DEVICE @ $TH9800_BAUD, port $CAT_PORT"
                python3 -u "$TH9800_DIR/TH9800_CAT.py" \
                    -s -c "$TH9800_DEVICE" -b "$TH9800_BAUD" \
                    -p "$CAT_PASSWORD" -sH 0.0.0.0 -sP "$CAT_PORT" \
                    > /tmp/th9800_cat.log 2>&1 &
                TH9800_PID=$!
                # Wait for TCP port to be ready (up to 10s)
                for i in $(seq 1 20); do
                    if ss -tlnp 2>/dev/null | grep -q ":${CAT_PORT} " ; then
                        break
                    fi
                    sleep 0.5
                done
                if ps -p $TH9800_PID > /dev/null 2>&1 && ss -tlnp 2>/dev/null | grep -q ":${CAT_PORT} "; then
                    ts "  TH-9800 CAT headless started (PID: $TH9800_PID, port $CAT_PORT)"
                else
                    ts "  TH-9800 CAT headless failed to start"
                    [ -f /tmp/th9800_cat.log ] && cat /tmp/th9800_cat.log
                    TH9800_PID=""
                fi
            fi
        else
            # GUI mode: prefer run.sh (uses venv), fall back to direct
            if [ -f "$TH9800_DIR/run.sh" ]; then
                TH9800_CMD="$TH9800_DIR/run.sh"
            else
                TH9800_CMD="python3 $TH9800_DIR/TH9800_CAT.py"
            fi
            ts "  Starting GUI mode..."
            $TH9800_CMD > /tmp/th9800_cat.log 2>&1 &
            TH9800_PID=$!
            sleep 2
            if ps -p $TH9800_PID > /dev/null 2>&1; then
                ts "  TH-9800 CAT started (PID: $TH9800_PID)"
            else
                ts "  TH-9800 CAT failed to start (continuing anyway)"
                TH9800_PID=""
            fi
        fi
    elif [ -n "$TH9800_DIR" ]; then
        ts "  TH-9800 folder found ($TH9800_DIR) but no TH9800_CAT.py inside"
    else
        ts "  No TH-9800 folder found in ~/Downloads (skipping)"
    fi
fi

# 4. Start Claude Code in the gateway folder if not already running
ts "[4/11] Checking Claude Code..."
GATEWAY_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CLAUDE_RUNNING=false
if [ "$START_CLAUDE_CODE" != "true" ]; then
    ts "  Disabled in config (START_CLAUDE_CODE = false)"
    CLAUDE_RUNNING=true
fi
CLAUDE_PID="$(pgrep -x claude 2>/dev/null | head -1)"
if [ -n "$CLAUDE_PID" ] && [ "$CLAUDE_RUNNING" = false ]; then
    CLAUDE_RUNNING=true
    ts "  Claude Code already running (PID: $CLAUDE_PID)"
fi
if [ "$CLAUDE_RUNNING" = false ]; then
    CLAUDE_BIN="$(command -v claude 2>/dev/null)"
    if [ -n "$CLAUDE_BIN" ]; then
        if command -v xfce4-terminal > /dev/null 2>&1; then
            xfce4-terminal --geometry=130x25 --working-directory="$GATEWAY_DIR" -e "$CLAUDE_BIN" &
            disown
        elif command -v lxterminal > /dev/null 2>&1; then
            lxterminal --geometry=130x25 --working-directory="$GATEWAY_DIR" -e "$CLAUDE_BIN" &
            disown
        elif command -v x-terminal-emulator > /dev/null 2>&1; then
            cd "$GATEWAY_DIR" && x-terminal-emulator --geometry=130x25 -e "$CLAUDE_BIN" &
            disown
            cd - > /dev/null
        elif command -v gnome-terminal > /dev/null 2>&1; then
            gnome-terminal --geometry=130x25 --working-directory="$GATEWAY_DIR" -- "$CLAUDE_BIN" &
            disown
        else
            ts "  No supported terminal emulator found (skipping)"
            CLAUDE_RUNNING=true  # skip the success check
        fi
        if [ "$CLAUDE_RUNNING" = false ]; then
            sleep 2
            ts "  Claude Code launched in new terminal"
        fi
    else
        ts "  Claude Code not installed (skipping)"
    fi
fi

# 5. Set CPU governor to performance for consistent scheduling latency
ts "[5/11] Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | try_sudo tee "$cpu" > /dev/null 2>&1 || true
done
ts "  CPU governor set (or unsupported on this platform)"

# 6. Unload and reload ALSA loopback (fresh start)
ts "[6/11] Resetting ALSA loopback..."
try_sudo modprobe -r snd-aloop 2>/dev/null
sleep 1
try_sudo modprobe snd-aloop
if [ $? -ne 0 ]; then
    ts "  Failed to load ALSA loopback"
    exit 1
fi
sleep 2  # Wait for device to be ready
ts "  ALSA loopback loaded"

# Verify device exists
if ! aplay -l 2>/dev/null | grep -q "Loopback"; then
    ts "  Warning: Loopback device not visible in aplay -l"
fi

# 7. Reset AIOC USB device (clears stale audio output state)
ts "[7/11] Resetting AIOC USB device..."
AIOC_USB=""
for d in /sys/bus/usb/devices/*/product; do
    if grep -qi "all-in-one" "$d" 2>/dev/null; then
        AIOC_USB="$(dirname "$d")"
        break
    fi
done
if [ -n "$AIOC_USB" ] && [ -f "$AIOC_USB/authorized" ]; then
    ts "  Found AIOC at $AIOC_USB"
    if [ -w "$AIOC_USB/authorized" ]; then
        echo 0 > "$AIOC_USB/authorized"
        sleep 1
        echo 1 > "$AIOC_USB/authorized"
    else
        # Needs root — reuse the sudo credential already cached from modprobe above
        try_sudo sh -c "echo 0 > $AIOC_USB/authorized && sleep 1 && echo 1 > $AIOC_USB/authorized"
    fi
    sleep 2  # Wait for USB re-enumeration
    ts "  AIOC USB reset complete"
else
    ts "  AIOC USB device not found (skipping reset)"
fi

# 8-10. Streaming pipeline (pipe + DarkIce + FFmpeg) — only if ENABLE_STREAM_OUTPUT = true
if [ "$ENABLE_STREAM_OUTPUT" = "true" ]; then
    # 8. Create named pipe
    ts "[8/11] Creating named pipe..."
    rm -f /tmp/darkice_audio 2>/dev/null
    fuser -k /tmp/darkice_audio 2>/dev/null
    sleep 1
    mkfifo /tmp/darkice_audio
    chmod 666 /tmp/darkice_audio
    ts "  Pipe created: /tmp/darkice_audio"

    # 9. Start Darkice with visible output
    ts "[9/11] Starting Darkice..."
    ts "  (Darkice output will be shown below)"
    echo "  ----------------------------------------"

    darkice -c /etc/darkice.cfg > /tmp/darkice.log 2>&1 &
    DARKICE_PID=$!

    sleep 4

    if ! ps -p $DARKICE_PID > /dev/null 2>&1; then
        echo "  ----------------------------------------"
        if grep -qi "forbidden\|mountpoint occupied\|maximum sources" /tmp/darkice.log 2>/dev/null; then
            ts "  Darkice: feed already live on another server — continuing without streaming"
            ts "  (Broadcastify mountpoint is occupied; local audio bridge will still run)"
            DARKICE_PID=""
            export GATEWAY_FEED_OCCUPIED=1
        else
            ts "  Darkice FAILED to start — continuing without streaming"
            echo ""
            ts "Error output:"
            cat /tmp/darkice.log
            echo ""
            ts "Common fixes:"
            echo "  1. Check /etc/darkice.cfg has: device = hw:Loopback,1,0"
            echo "  2. Check bitrate matches Broadcastify (usually 16)"
            echo "  3. Check Broadcastify password is correct"
            echo "  4. Run: sudo modprobe -r snd-aloop && sudo modprobe snd-aloop"
            DARKICE_PID=""
        fi
    else
        head -n 10 /tmp/darkice.log
        echo "  ----------------------------------------"
        ts "  Darkice running (PID: $DARKICE_PID)"
        ts "  Full log: /tmp/darkice.log"
    fi

    # 10. Start FFmpeg bridge with auto-restart
    ts "[10/11] Starting FFmpeg bridge..."
    (
        while true; do
            ffmpeg -loglevel error -f s16le -ar 48000 -ac 1 -i /tmp/darkice_audio \
                   -f alsa hw:Loopback,0,0 2>&1
            sleep 1
        done
    ) > /tmp/ffmpeg.log 2>&1 &
    FFMPEG_PID=$!
    sleep 2

    if ! ps -p $FFMPEG_PID > /dev/null; then
        ts "  FFmpeg failed to start!"
        cat /tmp/ffmpeg.log
        cleanup
    fi

    ts "  FFmpeg bridge running (PID: $FFMPEG_PID)"
else
    ts "[8-10/11] Streaming disabled (ENABLE_STREAM_OUTPUT = false) — skipping DarkIce/FFmpeg"
fi

# 11. Start Gateway
ts "[11/11] Starting gateway..."
echo ""

# Find the gateway file - ONLY in same directory as this script
GATEWAY_FILE="$SCRIPT_DIR/radio_gateway.py"

if [ ! -f "$GATEWAY_FILE" ]; then
    ts "Gateway file not found!"
    ts "  Expected: $GATEWAY_FILE"
    ts "  Make sure radio_gateway.py is in the SAME directory as start.sh"
    cleanup
    exit 1
fi

ts "Using: $GATEWAY_FILE"
echo ""
echo "=========================================="
ts "All components started successfully!"
echo "=========================================="
if [ "$ENABLE_STREAM_OUTPUT" = "true" ]; then
    ts "  Darkice:  ${DARKICE_PID:+"PID $DARKICE_PID (log: /tmp/darkice.log)"}${DARKICE_PID:-"disabled (see error above)"}"
    ts "  FFmpeg:   PID $FFMPEG_PID (log: /tmp/ffmpeg.log)"
else
    ts "  Streaming: disabled"
fi
ts "  Gateway:  Starting now (nice -n -10)..."
echo ""
ts "Press Ctrl+C to stop everything"
echo ""
sleep 2

# Start gateway with elevated scheduling priority.
# Renice the current shell (children inherit nice value), then run gateway
# in the foreground so it keeps stdin for keyboard controls.
try_sudo renice -n -10 -p $$ > /dev/null 2>&1
python3 "$GATEWAY_FILE"

# If gateway exits, cleanup
cleanup
