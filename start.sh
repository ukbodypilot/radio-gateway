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
ENABLE_CAT_CONTROL="$(read_config ENABLE_TH9800 false)"
START_CLAUDE_CODE="$(read_config START_CLAUDE_CODE false)"
CLAUDE_TMUX_SESSION="$(read_config TELEGRAM_TMUX_SESSION claude-gateway)"
HEADLESS_MODE="$(read_config HEADLESS_MODE false)"

echo "=========================================="
ts "Starting Radio Gateway"
echo "=========================================="
echo ""

# Cleanup function
cleanup() {
    echo ""
    ts "Cleaning up..."
    if systemctl is-active --quiet th9800-cat.service 2>/dev/null; then
        try_sudo systemctl stop th9800-cat.service 2>/dev/null
        ts "  Stopped TH-9800 CAT service"
    fi
    if [ ! -z "$SUDO_KEEPALIVE_PID" ]; then
        kill $SUDO_KEEPALIVE_PID 2>/dev/null
    fi
    rm -f /tmp/gateway.lock 2>/dev/null
    pkill -9 darkice 2>/dev/null
    pkill -f "ffmpeg.*darkice_audio" 2>/dev/null
    rm -f /tmp/darkice_audio 2>/dev/null
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

# Kill gateway process only (not D75_CAT or other Python services)
pkill -9 -f "radio_gateway.py" 2>/dev/null && ts "  Killed existing gateway"
rm -f /tmp/gateway.lock 2>/dev/null

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

# 3. Start TH-9800 CAT control service if CAT is enabled
ts "[3/11] Checking TH-9800 CAT control..."
if [ "$ENABLE_CAT_CONTROL" != "true" ]; then
    ts "  Disabled in config (ENABLE_CAT_CONTROL = false)"
    # Stop the service if it's running but CAT is disabled
    if systemctl is-active --quiet th9800-cat.service 2>/dev/null; then
        try_sudo systemctl stop th9800-cat.service 2>/dev/null
        ts "  Stopped th9800-cat service (CAT disabled)"
    fi
elif systemctl list-unit-files th9800-cat.service &>/dev/null; then
    if systemctl is-active --quiet th9800-cat.service 2>/dev/null; then
        try_sudo systemctl restart th9800-cat.service 2>/dev/null
    else
        try_sudo systemctl start th9800-cat.service 2>/dev/null
    fi
    # Wait for TCP port to be ready (up to 10s)
    CAT_PORT="$(read_config CAT_PORT 9800)"
    for i in $(seq 1 20); do
        if ss -tlnp 2>/dev/null | grep -q ":${CAT_PORT} "; then
            break
        fi
        sleep 0.5
    done
    if systemctl is-active --quiet th9800-cat.service 2>/dev/null && ss -tlnp 2>/dev/null | grep -q ":${CAT_PORT} "; then
        ts "  TH-9800 CAT service ready (port $CAT_PORT)"
    else
        ts "  TH-9800 CAT service failed to start"
        journalctl -u th9800-cat.service --no-pager -n 5 2>/dev/null
    fi
else
    ts "  th9800-cat.service not installed — run TH9800_CAT/install.sh"
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
        if command -v tmux > /dev/null 2>&1; then
            # Prefer tmux — keeps context alive for Telegram bot integration
            if tmux has-session -t "$CLAUDE_TMUX_SESSION" 2>/dev/null; then
                ts "  Claude Code tmux session '$CLAUDE_TMUX_SESSION' already running"
            else
                tmux new-session -d -s "$CLAUDE_TMUX_SESSION" -c "$GATEWAY_DIR" \
                    "$CLAUDE_BIN --dangerously-skip-permissions"
                ts "  Claude Code started in tmux session '$CLAUDE_TMUX_SESSION'"
                ts "    Attach: tmux attach -t $CLAUDE_TMUX_SESSION"
            fi
            # Open a visible terminal window showing the claude session (if on a display)
            if [ -n "$DISPLAY" ] && command -v xfce4-terminal > /dev/null 2>&1; then
                # Only open if no terminal is already attached to this session
                if ! tmux list-clients -t "$CLAUDE_TMUX_SESSION" 2>/dev/null | grep -q pts; then
                    xfce4-terminal --title="Claude Gateway" --geometry=220x50 \
                        -e "tmux attach-session -t $CLAUDE_TMUX_SESSION" &
                    disown
                    ts "  Claude Code terminal window opened"
                fi
            fi
        elif command -v xfce4-terminal > /dev/null 2>&1; then
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

# 6. ALSA loopback (no longer needed — streaming is handled internally)
ts "[6/11] ALSA loopback: skipped (streaming handled by gateway)"

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

# 8-10. Streaming now handled internally by the gateway (direct Icecast)
ts "[8-10/11] Streaming: handled by gateway (direct Icecast, no DarkIce)"
# Kill any leftover DarkIce/FFmpeg from previous versions
pkill -9 darkice 2>/dev/null
pkill -f "ffmpeg.*darkice_audio" 2>/dev/null
rm -f /tmp/darkice_audio 2>/dev/null

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
    ts "  Streaming: direct Icecast (handled by gateway)"
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
