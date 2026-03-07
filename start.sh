#!/bin/bash
# Radio Gateway — startup script

# Resolve script directory immediately (handles symlinks and relative invocation)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Ensure ~/.local/bin is in PATH (not present in non-login shells like desktop shortcuts)
[[ -d "$HOME/.local/bin" && ":$PATH:" != *":$HOME/.local/bin:"* ]] && export PATH="$HOME/.local/bin:$PATH"

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
START_CLAUDE_CODE="$(read_config START_CLAUDE_CODE false)"

echo "=========================================="
echo "Starting Radio Gateway"
echo "=========================================="
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo "Cleaning up..."
    if [ ! -z "$TH9800_PID" ]; then
        kill $TH9800_PID 2>/dev/null
        echo "  Stopped TH-9800 CAT"
    fi
    if [ ! -z "$DARKICE_PID" ]; then
        kill $DARKICE_PID 2>/dev/null
        echo "  Stopped Darkice"
    fi
    if [ ! -z "$FFMPEG_PID" ]; then
        kill $FFMPEG_PID 2>/dev/null
        echo "  Stopped FFmpeg"
    fi
    if [ ! -z "$SUDO_KEEPALIVE_PID" ]; then
        kill $SUDO_KEEPALIVE_PID 2>/dev/null
    fi
    rm -f /tmp/darkice_audio 2>/dev/null
    sudo modprobe -r snd-aloop 2>/dev/null
    # Restore terminal from raw mode (gateway sets cbreak for keyboard controls)
    stty sane 2>/dev/null
    echo "Done"
    exit
}

trap cleanup INT TERM

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "⚠ Warning: Running as root may cause permission issues"
    echo ""
fi

# Cache sudo credentials and keep them alive for the entire session
# (avoids repeat password prompts when gateway uses sudo internally)
sudo -v
( while true; do sudo -n -v 2>/dev/null; sleep 50; done ) &
SUDO_KEEPALIVE_PID=$!

# 1. Kill any existing processes
echo "[1/11] Checking for existing processes..."
pkill -9 darkice 2>/dev/null && echo "  Killed existing Darkice"
pkill -9 ffmpeg 2>/dev/null && echo "  Killed existing FFmpeg"
sleep 1

# Also kill any Python gateway processes (just in case)
pkill -9 -f "radio_gateway" 2>/dev/null && echo "  Killed existing gateway"

# Stop leftover mumble-server instances from prior gateway runs so they don't
# linger on stale ports (the gateway will start fresh ones with current config)
for svc in mumble-server-gw1 mumble-server-gw2; do
    if systemctl is-active --quiet "$svc.service" 2>/dev/null; then
        sudo systemctl stop "$svc.service" 2>/dev/null && echo "  Stopped $svc"
    fi
done
sleep 1

# 2. Start Mumble GUI client if not already running
echo "[2/11] Checking Mumble client..."
if pgrep -x "mumble" > /dev/null 2>&1; then
    echo "  ✓ Mumble already running (PID: $(pgrep -x mumble | head -1))"
else
    if command -v mumble > /dev/null 2>&1; then
        mumble > /dev/null 2>&1 &
        disown
        sleep 2
        if pgrep -x "mumble" > /dev/null 2>&1; then
            echo "  ✓ Mumble started (PID: $(pgrep -x mumble | head -1))"
        else
            echo "  ⚠ Mumble failed to start (continuing anyway)"
        fi
    else
        echo "  ⚠ Mumble not installed (skipping)"
    fi
fi

# 3. Start TH-9800 CAT control if not already running
echo "[3/11] Checking TH-9800 CAT control..."
if [ "$START_TH9800_CAT" != "true" ]; then
    echo "  ⚠ Disabled in config (START_TH9800_CAT = false)"
elif pgrep -f "TH9800_CAT.py" > /dev/null 2>&1; then
    TH9800_PID=$(pgrep -f TH9800_CAT.py | head -1)
    echo "  ✓ TH-9800 CAT already running (PID: $TH9800_PID)"
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

    if [ -n "$TH9800_DIR" ]; then
        # Prefer run.sh (uses venv), fall back to running TH9800_CAT.py directly
        if [ -f "$TH9800_DIR/run.sh" ]; then
            TH9800_CMD="$TH9800_DIR/run.sh"
        elif [ -f "$TH9800_DIR/TH9800_CAT.py" ]; then
            TH9800_CMD="python3 $TH9800_DIR/TH9800_CAT.py"
        else
            TH9800_CMD=""
        fi

        if [ -n "$TH9800_CMD" ]; then
            echo "  Found TH-9800 at: $TH9800_DIR"
            $TH9800_CMD > /tmp/th9800_cat.log 2>&1 &
            TH9800_PID=$!
            sleep 2
            if ps -p $TH9800_PID > /dev/null 2>&1; then
                echo "  ✓ TH-9800 CAT started (PID: $TH9800_PID)"
            else
                echo "  ⚠ TH-9800 CAT failed to start (continuing anyway)"
                TH9800_PID=""
            fi
        else
            echo "  ⚠ TH-9800 folder found ($TH9800_DIR) but no run.sh or TH9800_CAT.py inside"
        fi
    else
        echo "  ⚠ No TH-9800 folder found in ~/Downloads (skipping)"
    fi
fi

# 4. Start Claude Code in the gateway folder if not already running
echo "[4/11] Checking Claude Code..."
GATEWAY_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CLAUDE_RUNNING=false
if [ "$START_CLAUDE_CODE" != "true" ]; then
    echo "  ⚠ Disabled in config (START_CLAUDE_CODE = false)"
    CLAUDE_RUNNING=true
fi
CLAUDE_PID="$(pgrep -x claude 2>/dev/null | head -1)"
if [ -n "$CLAUDE_PID" ] && [ "$CLAUDE_RUNNING" = false ]; then
    CLAUDE_RUNNING=true
    echo "  ✓ Claude Code already running (PID: $CLAUDE_PID)"
fi
if [ "$CLAUDE_RUNNING" = false ]; then
    CLAUDE_BIN="$(command -v claude 2>/dev/null)"
    if [ -n "$CLAUDE_BIN" ]; then
        if command -v xfce4-terminal > /dev/null 2>&1; then
            xfce4-terminal --geometry=150x25 --working-directory="$GATEWAY_DIR" -e "$CLAUDE_BIN" &
            disown
        elif command -v lxterminal > /dev/null 2>&1; then
            lxterminal --geometry=150x25 --working-directory="$GATEWAY_DIR" -e "$CLAUDE_BIN" &
            disown
        elif command -v x-terminal-emulator > /dev/null 2>&1; then
            cd "$GATEWAY_DIR" && x-terminal-emulator --geometry=150x25 -e "$CLAUDE_BIN" &
            disown
            cd - > /dev/null
        elif command -v gnome-terminal > /dev/null 2>&1; then
            gnome-terminal --geometry=150x25 --working-directory="$GATEWAY_DIR" -- "$CLAUDE_BIN" &
            disown
        else
            echo "  ⚠ No supported terminal emulator found (skipping)"
            CLAUDE_RUNNING=true  # skip the success check
        fi
        if [ "$CLAUDE_RUNNING" = false ]; then
            sleep 2
            echo "  ✓ Claude Code launched in new terminal"
        fi
    else
        echo "  ⚠ Claude Code not installed (skipping)"
    fi
fi

# 5. Set CPU governor to performance for consistent scheduling latency
echo "[5/11] Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$cpu" > /dev/null 2>&1 || true
done
echo "  ✓ CPU governor set (or unsupported on this platform)"

# 6. Unload and reload ALSA loopback (fresh start)
echo "[6/11] Resetting ALSA loopback..."
sudo modprobe -r snd-aloop 2>/dev/null
sleep 1
sudo modprobe snd-aloop
if [ $? -ne 0 ]; then
    echo "  ✗ Failed to load ALSA loopback"
    exit 1
fi
sleep 2  # Wait for device to be ready
echo "  ✓ ALSA loopback loaded"

# Verify device exists
if ! aplay -l 2>/dev/null | grep -q "Loopback"; then
    echo "  ⚠ Warning: Loopback device not visible in aplay -l"
fi

# 7. Reset AIOC USB device (clears stale audio output state)
echo "[7/11] Resetting AIOC USB device..."
AIOC_USB=""
for d in /sys/bus/usb/devices/*/product; do
    if grep -qi "all-in-one" "$d" 2>/dev/null; then
        AIOC_USB="$(dirname "$d")"
        break
    fi
done
if [ -n "$AIOC_USB" ] && [ -f "$AIOC_USB/authorized" ]; then
    echo "  Found AIOC at $AIOC_USB"
    if [ -w "$AIOC_USB/authorized" ]; then
        echo 0 > "$AIOC_USB/authorized"
        sleep 1
        echo 1 > "$AIOC_USB/authorized"
    else
        # Needs root — reuse the sudo credential already cached from modprobe above
        sudo sh -c "echo 0 > $AIOC_USB/authorized && sleep 1 && echo 1 > $AIOC_USB/authorized"
    fi
    sleep 2  # Wait for USB re-enumeration
    echo "  ✓ AIOC USB reset complete"
else
    echo "  ⚠ AIOC USB device not found (skipping reset)"
fi

# 8-10. Streaming pipeline (pipe + DarkIce + FFmpeg) — only if ENABLE_STREAM_OUTPUT = true
if [ "$ENABLE_STREAM_OUTPUT" = "true" ]; then
    # 8. Create named pipe
    echo "[8/11] Creating named pipe..."
    rm -f /tmp/darkice_audio 2>/dev/null
    fuser -k /tmp/darkice_audio 2>/dev/null
    sleep 1
    mkfifo /tmp/darkice_audio
    chmod 666 /tmp/darkice_audio
    echo "  ✓ Pipe created: /tmp/darkice_audio"

    # 9. Start Darkice with visible output
    echo "[9/11] Starting Darkice..."
    echo "  (Darkice output will be shown below)"
    echo "  ----------------------------------------"

    darkice -c /etc/darkice.cfg > /tmp/darkice.log 2>&1 &
    DARKICE_PID=$!

    sleep 4

    if ! ps -p $DARKICE_PID > /dev/null 2>&1; then
        echo "  ----------------------------------------"
        if grep -qi "forbidden\|mountpoint occupied\|maximum sources" /tmp/darkice.log 2>/dev/null; then
            echo "  ⚠ Darkice: feed already live on another server — continuing without streaming"
            echo "  (Broadcastify mountpoint is occupied; local audio bridge will still run)"
            DARKICE_PID=""
            export GATEWAY_FEED_OCCUPIED=1
        else
            echo "  ✗ Darkice FAILED to start — continuing without streaming"
            echo ""
            echo "Error output:"
            cat /tmp/darkice.log
            echo ""
            echo "Common fixes:"
            echo "  1. Check /etc/darkice.cfg has: device = hw:Loopback,1,0"
            echo "  2. Check bitrate matches Broadcastify (usually 16)"
            echo "  3. Check Broadcastify password is correct"
            echo "  4. Run: sudo modprobe -r snd-aloop && sudo modprobe snd-aloop"
            DARKICE_PID=""
        fi
    else
        head -n 10 /tmp/darkice.log
        echo "  ----------------------------------------"
        echo "  ✓ Darkice running (PID: $DARKICE_PID)"
        echo "  Full log: /tmp/darkice.log"
    fi

    # 10. Start FFmpeg bridge with auto-restart
    echo "[10/11] Starting FFmpeg bridge..."
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
        echo "  ✗ FFmpeg failed to start!"
        cat /tmp/ffmpeg.log
        cleanup
    fi

    echo "  ✓ FFmpeg bridge running (PID: $FFMPEG_PID)"
else
    echo "[8-10/11] Streaming disabled (ENABLE_STREAM_OUTPUT = false) — skipping DarkIce/FFmpeg"
fi

# 11. Start Gateway
echo "[11/11] Starting gateway..."
echo ""

# Find the gateway file - ONLY in same directory as this script
GATEWAY_FILE="$SCRIPT_DIR/radio_gateway.py"

if [ ! -f "$GATEWAY_FILE" ]; then
    echo "✗ Gateway file not found!"
    echo "  Expected: $GATEWAY_FILE"
    echo "  Make sure radio_gateway.py is in the SAME directory as start.sh"
    cleanup
    exit 1
fi

echo "Using: $GATEWAY_FILE"
echo ""
echo "=========================================="
echo "All components started successfully!"
echo "=========================================="
if [ "$ENABLE_STREAM_OUTPUT" = "true" ]; then
    echo "  Darkice:  ${DARKICE_PID:+"PID $DARKICE_PID (log: /tmp/darkice.log)"}${DARKICE_PID:-"disabled (see error above)"}"
    echo "  FFmpeg:   PID $FFMPEG_PID (log: /tmp/ffmpeg.log)"
else
    echo "  Streaming: disabled"
fi
echo "  Gateway:  Starting now (nice -n -10)..."
echo ""
echo "Press Ctrl+C to stop everything"
echo ""
sleep 2

# Start gateway with elevated scheduling priority.
# Renice the current shell (children inherit nice value), then run gateway
# in the foreground so it keeps stdin for keyboard controls.
sudo renice -n -10 -p $$ > /dev/null 2>&1
python3 "$GATEWAY_FILE"

# If gateway exits, cleanup
cleanup
