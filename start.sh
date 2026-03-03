#!/bin/bash
# Start Broadcastify streaming - ROBUST VERSION

echo "=========================================="
echo "Starting Broadcastify Stream"
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
    rm -f /tmp/darkice_audio 2>/dev/null
    sudo modprobe -r snd-aloop 2>/dev/null
    echo "Done"
    exit
}

trap cleanup INT TERM

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "⚠ Warning: Running as root may cause permission issues"
    echo ""
fi

# Cache sudo credentials once up front (avoids repeat password prompts)
sudo -v

# 1. Kill any existing processes
echo "[1/10] Checking for existing processes..."
pkill -9 darkice 2>/dev/null && echo "  Killed existing Darkice"
pkill -9 ffmpeg 2>/dev/null && echo "  Killed existing FFmpeg"
sleep 1

# Also kill any Python gateway processes (just in case)
pkill -9 -f "mumble_radio_gateway" 2>/dev/null && echo "  Killed existing gateway"
sleep 1

# 2. Start TH-9800 CAT control if not already running
echo "[2/10] Checking TH-9800 CAT control..."
if pgrep -f "TH9800_CAT.py" > /dev/null 2>&1; then
    TH9800_PID=$(pgrep -f TH9800_CAT.py | head -1)
    echo "  ✓ TH-9800 CAT already running (PID: $TH9800_PID)"
else
    TH9800_SCRIPT="$HOME/Downloads/th9800/TH9800_CAT.py"
    if [ -f "$TH9800_SCRIPT" ]; then
        python3 "$TH9800_SCRIPT" &
        TH9800_PID=$!
        sleep 2
        if ps -p $TH9800_PID > /dev/null 2>&1; then
            echo "  ✓ TH-9800 CAT started (PID: $TH9800_PID)"
        else
            echo "  ⚠ TH-9800 CAT failed to start (continuing anyway)"
            TH9800_PID=""
        fi
    else
        echo "  ⚠ TH-9800 CAT script not found at $TH9800_SCRIPT (skipping)"
    fi
fi

# 3. Start Claude Code in the gateway folder if not already running
echo "[3/10] Checking Claude Code..."
GATEWAY_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CLAUDE_RUNNING=false
for pid in $(pgrep -f "claude" 2>/dev/null); do
    if [ -d "/proc/$pid" ] && [ "$(readlink -f /proc/$pid/cwd 2>/dev/null)" = "$GATEWAY_DIR" ]; then
        CLAUDE_RUNNING=true
        echo "  ✓ Claude Code already running in gateway folder (PID: $pid)"
        break
    fi
done
if [ "$CLAUDE_RUNNING" = false ]; then
    if command -v claude > /dev/null 2>&1; then
        if command -v lxterminal > /dev/null 2>&1; then
            lxterminal --working-directory="$GATEWAY_DIR" -e claude &
            disown
        elif command -v x-terminal-emulator > /dev/null 2>&1; then
            cd "$GATEWAY_DIR" && x-terminal-emulator -e claude &
            disown
            cd - > /dev/null
        elif command -v gnome-terminal > /dev/null 2>&1; then
            gnome-terminal --working-directory="$GATEWAY_DIR" -- claude &
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

# 4. Set CPU governor to performance for consistent scheduling latency
echo "[4/10] Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$cpu" > /dev/null 2>&1 || true
done
echo "  ✓ CPU governor set (or unsupported on this platform)"

# 5. Unload and reload ALSA loopback (fresh start)
echo "[5/10] Resetting ALSA loopback..."
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

# 6. Reset AIOC USB device (clears stale audio output state)
echo "[6/10] Resetting AIOC USB device..."
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

# 7. Create named pipe
echo "[7/10] Creating named pipe..."
# Force remove old pipe (even if busy)
rm -f /tmp/darkice_audio 2>/dev/null
# Kill any processes using it
fuser -k /tmp/darkice_audio 2>/dev/null
sleep 1
# Create fresh pipe
mkfifo /tmp/darkice_audio
chmod 666 /tmp/darkice_audio
echo "  ✓ Pipe created: /tmp/darkice_audio"

# 8. Start Darkice with visible output
echo "[8/10] Starting Darkice..."
echo "  (Darkice output will be shown below)"
echo "  ----------------------------------------"

# Start Darkice in background but capture output
darkice -c /etc/darkice.cfg > /tmp/darkice.log 2>&1 &
DARKICE_PID=$!

# Wait and check if it started successfully
sleep 4

if ! ps -p $DARKICE_PID > /dev/null 2>&1; then
    echo "  ----------------------------------------"
    if grep -qi "forbidden\|mountpoint occupied\|maximum sources" /tmp/darkice.log 2>/dev/null; then
        echo "  ⚠ Darkice: feed already live on another server — continuing without streaming"
        echo "  (Broadcastify mountpoint is occupied; local audio bridge will still run)"
        DARKICE_PID=""
        export GATEWAY_FEED_OCCUPIED=1
    else
        echo "  ✗ Darkice FAILED to start!"
        echo ""
        echo "Error output:"
        cat /tmp/darkice.log
        echo ""
        echo "Common fixes:"
        echo "  1. Check /etc/darkice.cfg has: device = hw:Loopback,1,0"
        echo "  2. Check bitrate matches Broadcastify (usually 16)"
        echo "  3. Check Broadcastify password is correct"
        echo "  4. Run: sudo modprobe -r snd-aloop && sudo modprobe snd-aloop"
        cleanup
    fi
else
    # Show first few lines of Darkice output
    head -n 10 /tmp/darkice.log
    echo "  ----------------------------------------"
    echo "  ✓ Darkice running (PID: $DARKICE_PID)"
    echo "  Full log: /tmp/darkice.log"
fi

# 9. Start FFmpeg bridge with auto-restart
echo "[9/10] Starting FFmpeg bridge..."
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

# 10. Start Gateway
echo "[10/10] Starting gateway..."
echo ""

# Find the gateway file - ONLY in same directory as this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
GATEWAY_FILE="$SCRIPT_DIR/mumble_radio_gateway.py"

if [ ! -f "$GATEWAY_FILE" ]; then
    echo "✗ Gateway file not found!"
    echo "  Expected: $GATEWAY_FILE"
    echo "  Make sure mumble_radio_gateway.py is in the SAME directory as start.sh"
    cleanup
    exit 1
fi

echo "Using: $GATEWAY_FILE"
echo ""
echo "=========================================="
echo "All components started successfully!"
echo "=========================================="
echo "  Darkice:  ${DARKICE_PID:+"PID $DARKICE_PID (log: /tmp/darkice.log)"}${DARKICE_PID:-"disabled (mountpoint occupied)"}"
echo "  FFmpeg:   PID $FFMPEG_PID (log: /tmp/ffmpeg.log)"
echo "  Gateway:  Starting now (nice -n -10)..."
echo ""
echo "Press Ctrl+C to stop everything"
echo ""
sleep 2

# Start gateway with elevated scheduling priority so it competes well against
# CPU-heavy apps (e.g. SDRconnect).  nice -n -10 raises priority without RT
# scheduling — safe, no risk of starving the kernel.
nice -n -10 python3 "$GATEWAY_FILE"

# If gateway exits, cleanup
cleanup
