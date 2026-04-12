#!/bin/bash
# setup_endpoint.sh — Automated endpoint setup for Radio Gateway
#
# Installs dependencies, configures audio, HID, PipeWire, systemd service,
# and validates the endpoint is ready to connect.
#
# Supports: Debian 12+, Raspberry Pi OS (Bookworm/Trixie), Ubuntu 22.04+
# Requires: sudo access, internet connection
#
# Usage:
#   bash setup_endpoint.sh [options]
#
# Options:
#   --name NAME        Endpoint name (required, e.g. garage-radio)
#   --plugin PLUGIN    Plugin type: audio, aioc, d75 (default: aioc)
#   --device DEVICE    ALSA device (default: auto-detect AIOC)
#   --server HOST:PORT Gateway LAN address (default: auto-discover via mDNS)
#   --gdrive-folder F  Google Drive folder for tunnel URL (default: radio-gateway)
#   --bt-mac MAC       Bluetooth MAC for D75 plugin
#   --rclone-conf PATH Path to rclone.conf to deploy (optional)
#   --skip-reboot      Don't reboot even if systemd-sysv was installed
#   --dry-run          Show what would be done without doing it

set -e

# ── Defaults ──────────────────────────────────────────────────────────────
EP_NAME=""
PLUGIN="aioc"
DEVICE=""
SERVER=""
GDRIVE_FOLDER="radio-gateway"
BT_MAC=""
RCLONE_CONF=""
SKIP_REBOOT=false
DRY_RUN=false
LINK_DIR="$HOME/link"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[X]${NC} $*"; exit 1; }
step()  { echo -e "\n${GREEN}── $* ──${NC}"; }

# ── Parse args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)       EP_NAME="$2"; shift 2 ;;
        --plugin)     PLUGIN="$2"; shift 2 ;;
        --device)     DEVICE="$2"; shift 2 ;;
        --server)     SERVER="$2"; shift 2 ;;
        --gdrive-folder) GDRIVE_FOLDER="$2"; shift 2 ;;
        --bt-mac)     BT_MAC="$2"; shift 2 ;;
        --rclone-conf) RCLONE_CONF="$2"; shift 2 ;;
        --skip-reboot) SKIP_REBOOT=true; shift ;;
        --dry-run)    DRY_RUN=true; shift ;;
        -h|--help)
            head -20 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done

[[ -z "$EP_NAME" ]] && fail "--name is required (e.g. --name garage-radio)"

echo "Radio Gateway Endpoint Setup"
echo "============================"
echo "Name:    $EP_NAME"
echo "Plugin:  $PLUGIN"
echo "Device:  ${DEVICE:-auto-detect}"
echo "Server:  ${SERVER:-auto-discover}"
echo "Drive:   $GDRIVE_FOLDER"
echo ""

$DRY_RUN && info "DRY RUN — no changes will be made" && echo ""

# ── Step 1: Check OS ─────────────────────────────────────────────────────
step "Checking OS"

if [[ ! -f /etc/os-release ]]; then
    fail "Cannot detect OS — /etc/os-release missing"
fi
. /etc/os-release
info "OS: $PRETTY_NAME"
info "Arch: $(uname -m)"
info "Kernel: $(uname -r)"

IS_PI=false
[[ -f /proc/device-tree/model ]] && grep -qi 'raspberry' /proc/device-tree/model 2>/dev/null && IS_PI=true
$IS_PI && info "Detected: Raspberry Pi ($(cat /proc/device-tree/model 2>/dev/null | tr -d '\0'))"

# ── Step 2: Ensure systemd is init ───────────────────────────────────────
step "Checking init system"

INIT=$(cat /proc/1/comm)
if [[ "$INIT" != "systemd" ]]; then
    warn "Init system is '$INIT', not systemd"
    info "Installing systemd-sysv..."
    $DRY_RUN || sudo apt-get install -y systemd-sysv
    if ! $SKIP_REBOOT && ! $DRY_RUN; then
        warn "Reboot required for systemd. Run this script again after reboot."
        echo ""
        read -p "Reboot now? [y/N] " -n 1 -r
        echo
        [[ $REPLY =~ ^[Yy]$ ]] && sudo reboot
        exit 0
    fi
    warn "Reboot needed — rerun this script after reboot"
else
    info "systemd is init — OK"
fi

# ── Step 3: Install packages ─────────────────────────────────────────────
step "Installing dependencies"

PKGS="python3 python3-pyaudio alsa-utils rclone"

# HID support (for AIOC PTT)
if [[ "$PLUGIN" == "aioc" ]]; then
    PKGS="$PKGS python3-hid libhidapi-hidraw0"
fi

# PipeWire for full-duplex audio
PKGS="$PKGS pipewire pipewire-alsa wireplumber"

# D75 needs Bluetooth
if [[ "$PLUGIN" == "d75" ]]; then
    PKGS="$PKGS bluetooth bluez"
fi

info "Packages: $PKGS"
$DRY_RUN || sudo apt-get update -qq
$DRY_RUN || sudo apt-get install -y $PKGS

# ── Step 4: Enable PipeWire user session ──────────────────────────────────
step "Configuring PipeWire"

$DRY_RUN || loginctl enable-linger "$(whoami)" 2>/dev/null || true

# Start PipeWire if not running
if ! pgrep -x pipewire >/dev/null 2>&1; then
    info "Starting PipeWire..."
    $DRY_RUN || systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
fi

# Enable on boot
$DRY_RUN || systemctl --user enable pipewire pipewire-pulse wireplumber 2>/dev/null || true

if pgrep -x pipewire >/dev/null 2>&1; then
    info "PipeWire running — OK"
else
    warn "PipeWire not running (may need reboot)"
fi

# ── Step 5: AIOC udev rules ──────────────────────────────────────────────
if [[ "$PLUGIN" == "aioc" ]]; then
    step "Configuring AIOC udev rules"

    UDEV_FILE="/etc/udev/rules.d/99-aioc.rules"
    if [[ ! -f "$UDEV_FILE" ]] || ! grep -q '1209' "$UDEV_FILE" 2>/dev/null; then
        info "Creating $UDEV_FILE"
        $DRY_RUN || sudo tee "$UDEV_FILE" > /dev/null << 'UDEV'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="plugdev", TAG+="uaccess"
UDEV
        $DRY_RUN || sudo udevadm control --reload-rules
        $DRY_RUN || sudo udevadm trigger
        info "udev rules installed"
    else
        info "udev rules already exist — OK"
    fi
fi

# ── Step 6: Bluetooth SCO-over-HCI (Pi built-in BT) ──────────────────────
if $IS_PI; then
    step "Configuring Bluetooth SCO routing"

    # Pi's Broadcom chip routes SCO to PCM pins by default — need HCI
    if hciconfig hci0 2>/dev/null | grep -q 'UP RUNNING'; then
        info "Sending SCO-over-HCI vendor command..."
        $DRY_RUN || hcitool cmd 0x3f 0x1c 0x01 0x02 0x00 0x00 0x00 >/dev/null 2>&1
        # Make persistent via udev
        UDEV_BT="/etc/udev/rules.d/99-bt-sco-hci.rules"
        if [[ ! -f "$UDEV_BT" ]]; then
            $DRY_RUN || sudo tee "$UDEV_BT" > /dev/null << 'BTDEV'
ACTION=="add", SUBSYSTEM=="bluetooth", KERNEL=="hci0", RUN+="/usr/bin/hcitool cmd 0x3f 0x1c 0x01 0x02 0x00 0x00 0x00"
BTDEV
            info "BT SCO-over-HCI udev rule created"
        fi
    else
        warn "hci0 not UP — skip SCO routing (enable BT first)"
    fi

    # BT/WiFi coexistence tuning (shared BCM43436 radio on Pi Zero 2W)
    # Apply now AND make persistent via boot service
    # - noscan: disable BT page scanning (reduces radio contention)
    # - TX power 15 dBm: shorter WiFi bursts = more BT airtime, higher modulation
    if hciconfig hci0 2>/dev/null | grep -q 'UP RUNNING'; then
        $DRY_RUN || sudo hciconfig hci0 noscan 2>/dev/null || true
        info "BT page scanning disabled (re-enable for pairing)"
    fi
    if [[ -x /usr/sbin/iw ]]; then
        $DRY_RUN || sudo /usr/sbin/iw dev wlan0 set txpower fixed 1500 2>/dev/null || true
        info "WiFi TX power set to 15 dBm"
    fi

    # Persistent boot service for coex tuning
    COEX_SVC="/etc/systemd/system/bt-wifi-coex.service"
    if [[ ! -f "$COEX_SVC" ]]; then
        info "Creating persistent BT/WiFi coex boot service"
        $DRY_RUN || sudo tee "$COEX_SVC" > /dev/null << 'COEXEOF'
[Unit]
Description=BT/WiFi coexistence tuning (Pi shared radio)
After=bluetooth.target network-online.target
Wants=bluetooth.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/hciconfig hci0 noscan
ExecStart=/usr/sbin/iw dev wlan0 set txpower fixed 1500

[Install]
WantedBy=multi-user.target
COEXEOF
        $DRY_RUN || sudo systemctl daemon-reload
        $DRY_RUN || sudo systemctl enable bt-wifi-coex.service
        info "bt-wifi-coex.service enabled at boot"
    else
        info "bt-wifi-coex.service already exists — OK"
    fi

    # Disable WiFi power save
    if command -v iw >/dev/null 2>&1 || [[ -x /usr/sbin/iw ]]; then
        $DRY_RUN || sudo /usr/sbin/iw dev wlan0 set power_save off 2>/dev/null || true
        NM_CONF="/etc/NetworkManager/conf.d/wifi-powersave.conf"
        if [[ ! -f "$NM_CONF" ]]; then
            $DRY_RUN || sudo mkdir -p /etc/NetworkManager/conf.d
            $DRY_RUN || sudo tee "$NM_CONF" > /dev/null << 'NMEOF'
[connection]
wifi.powersave = 2
NMEOF
            info "WiFi power save disabled"
        fi
    fi
fi

# ── Step 7: Deploy endpoint files ────────────────────────────────────────
step "Deploying endpoint files"

$DRY_RUN || mkdir -p "$LINK_DIR"

# Copy files from the same directory as this script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

for f in gateway_link.py link_endpoint.py; do
    SRC=""
    [[ -f "$SCRIPT_DIR/$f" ]] && SRC="$SCRIPT_DIR/$f"
    [[ -f "$REPO_DIR/$f" ]] && SRC="$REPO_DIR/$f"
    if [[ -n "$SRC" ]]; then
        $DRY_RUN || cp "$SRC" "$LINK_DIR/"
        info "Deployed $f"
    else
        warn "$f not found in $SCRIPT_DIR or $REPO_DIR"
    fi
done

# D75 plugin
if [[ "$PLUGIN" == "d75" ]]; then
    for f in d75_link_plugin.py remote_bt_proxy.py; do
        SRC=""
        [[ -f "$SCRIPT_DIR/$f" ]] && SRC="$SCRIPT_DIR/$f"
        [[ -f "$REPO_DIR/scripts/$f" ]] && SRC="$REPO_DIR/scripts/$f"
        if [[ -n "$SRC" ]]; then
            $DRY_RUN || cp "$SRC" "$LINK_DIR/"
            info "Deployed $f"
        fi
    done
fi

# ── Step 8: Deploy rclone config ─────────────────────────────────────────
if [[ -n "$RCLONE_CONF" ]]; then
    step "Deploying rclone config"
    if [[ -f "$RCLONE_CONF" ]]; then
        $DRY_RUN || mkdir -p "$HOME/.config/rclone"
        $DRY_RUN || cp "$RCLONE_CONF" "$HOME/.config/rclone/rclone.conf"
        info "rclone.conf deployed"
    else
        warn "rclone.conf not found at: $RCLONE_CONF"
    fi
fi

# ── Step 9: Auto-detect ALSA device ──────────────────────────────────────
if [[ -z "$DEVICE" && "$PLUGIN" != "d75" ]]; then
    step "Auto-detecting audio device"
    CARD=$(grep -i 'All-In-One' /proc/asound/cards 2>/dev/null | awk '{print $1}')
    if [[ -n "$CARD" ]]; then
        DEVICE="plughw:${CARD},0"
        info "Found AIOC at ALSA card $CARD → $DEVICE"
    else
        DEVICE="default"
        warn "AIOC not found — using default audio device"
    fi
fi

# ── Step 10: Create systemd service ──────────────────────────────────────
step "Creating systemd service"

SVC_NAME="link-endpoint"
SVC_FILE="$HOME/.config/systemd/user/${SVC_NAME}.service"
$DRY_RUN || mkdir -p "$HOME/.config/systemd/user"

# Build ExecStart command
EXEC="$LINK_DIR/link_endpoint.py --name $EP_NAME --plugin $PLUGIN"
[[ -n "$DEVICE" && "$PLUGIN" != "d75" ]] && EXEC="$EXEC --device $DEVICE"
[[ -n "$SERVER" ]] && EXEC="$EXEC --server $SERVER"
[[ -n "$GDRIVE_FOLDER" ]] && EXEC="$EXEC --gdrive-folder $GDRIVE_FOLDER"
[[ -n "$BT_MAC" ]] && EXEC="$EXEC --device $BT_MAC"

AFTER="network-online.target sound.target"
[[ "$PLUGIN" == "d75" ]] && AFTER="network-online.target bluetooth.target"

cat << SVCEOF
[Unit]
Description=Radio Gateway Link Endpoint ($EP_NAME)
After=$AFTER
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$LINK_DIR
ExecStart=/usr/bin/python3 -u $EXEC
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SVCEOF

if ! $DRY_RUN; then
    cat > "$SVC_FILE" << SVCEOF
[Unit]
Description=Radio Gateway Link Endpoint ($EP_NAME)
After=$AFTER
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$LINK_DIR
ExecStart=/usr/bin/python3 -u $EXEC
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable "$SVC_NAME"
    info "Service created and enabled: $SVC_NAME"
fi

# ── Step 11: Validate ────────────────────────────────────────────────────
step "Validating"

ERRORS=0

# Python
if python3 --version >/dev/null 2>&1; then
    info "Python3: $(python3 --version 2>&1)"
else
    warn "Python3 not found"; ERRORS=$((ERRORS+1))
fi

# PyAudio
if python3 -c "import pyaudio" 2>/dev/null; then
    info "PyAudio: OK"
else
    warn "PyAudio not importable"; ERRORS=$((ERRORS+1))
fi

# HID (AIOC only)
if [[ "$PLUGIN" == "aioc" ]]; then
    if python3 -c "import hid" 2>/dev/null; then
        info "HID library: OK"
    else
        warn "HID library not importable"; ERRORS=$((ERRORS+1))
    fi

    if [[ -e /dev/hidraw0 ]]; then
        info "hidraw0: present"
        if python3 -c "import hid; d=hid.device(); d.open(0x1209,0x7388); d.close()" 2>/dev/null; then
            info "HID open test: OK"
        else
            warn "HID open test: FAILED (check permissions — may need replug)"
        fi
    else
        warn "hidraw0: not found (plug in AIOC or replug)"
    fi
fi

# Audio device
if [[ -n "$DEVICE" && "$DEVICE" != "default" && "$PLUGIN" != "d75" ]]; then
    if timeout 2 arecord -D "$DEVICE" -f S16_LE -r 48000 -c 1 -t raw --duration=1 /dev/null 2>/dev/null; then
        info "Audio capture ($DEVICE): OK"
    else
        warn "Audio capture ($DEVICE): FAILED"
        ERRORS=$((ERRORS+1))
    fi
fi

# rclone
if command -v rclone >/dev/null 2>&1; then
    info "rclone: $(rclone version 2>&1 | head -1)"
    if rclone lsd gdrive: >/dev/null 2>&1; then
        info "Google Drive access: OK"
    else
        warn "Google Drive access: FAILED (run 'rclone config' to set up)"
    fi
else
    warn "rclone not found"
fi

# PipeWire
if pgrep -x pipewire >/dev/null 2>&1; then
    info "PipeWire: running"
else
    warn "PipeWire: not running"; ERRORS=$((ERRORS+1))
fi

# Gateway connectivity
if [[ -n "$SERVER" ]]; then
    HOST="${SERVER%%:*}"
    if ping -c1 -W2 "$HOST" >/dev/null 2>&1; then
        info "Gateway ($HOST): reachable"
    else
        warn "Gateway ($HOST): unreachable"
    fi
fi

# Endpoint files
if [[ -f "$LINK_DIR/gateway_link.py" && -f "$LINK_DIR/link_endpoint.py" ]]; then
    info "Endpoint files: present"
else
    warn "Endpoint files: missing from $LINK_DIR"; ERRORS=$((ERRORS+1))
fi

echo ""
if [[ $ERRORS -eq 0 ]]; then
    info "All checks passed!"
else
    warn "$ERRORS check(s) failed — review warnings above"
fi

# ── Step 12: Start? ──────────────────────────────────────────────────────
echo ""
if ! $DRY_RUN; then
    read -p "Start the endpoint service now? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        systemctl --user start "$SVC_NAME"
        sleep 3
        if systemctl --user is-active "$SVC_NAME" >/dev/null 2>&1; then
            info "Endpoint '$EP_NAME' is running!"
            echo ""
            echo "Useful commands:"
            echo "  systemctl --user status $SVC_NAME    # check status"
            echo "  systemctl --user restart $SVC_NAME   # restart"
            echo "  journalctl --user -u $SVC_NAME -f    # follow logs"
        else
            warn "Service failed to start — check: journalctl --user -u $SVC_NAME"
        fi
    fi
fi

echo ""
info "Setup complete."
