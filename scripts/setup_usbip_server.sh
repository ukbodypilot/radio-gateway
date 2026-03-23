#!/bin/bash
# =============================================================================
# USBIP Server Setup — radio-gateway project
# Turns this machine into a USB/IP server so USB devices (BT dongle, RTL-SDR,
# AIOC, etc.) can be accessed remotely by the gateway machine over TCP 3240.
#
# Usage:  sudo bash setup_usbip_server.sh
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo $0"

# Detect OS
if command -v apt-get &>/dev/null; then
    DISTRO="debian"
elif command -v pacman &>/dev/null; then
    DISTRO="arch"
else
    error "Unsupported distro — only Debian/Ubuntu/RPi and Arch Linux supported"
fi

info "Detected: $DISTRO"

# ─── Step 1: Install packages ─────────────────────────────────────────────────
step "1. Installing usbip"

if [[ $DISTRO == "debian" ]]; then
    apt-get update -qq
    # Try kernel-version-specific tools first, fall back to generic
    KVER=$(uname -r)
    # Install usbip userspace tools — package name varies by distro:
    #   Debian/MX/RPiOS: 'usbip' contains usbipd in /usr/sbin
    #   Ubuntu: usbipd lives in linux-tools-$(uname -r) or linux-tools-generic
    apt-get install -y -qq usbip 2>/dev/null || true
    # Try kernel-version-specific tools (Ubuntu)
    apt-get install -y -qq "linux-tools-${KVER}" 2>/dev/null || true
    # Try generic tools (Ubuntu fallback)
    apt-get install -y -qq linux-tools-generic 2>/dev/null || true

    # Locate usbipd — check common locations
    USBIPD_BIN=$(find /usr/lib/linux-tools -name usbipd 2>/dev/null | sort | tail -1)
    [[ -z "$USBIPD_BIN" ]] && USBIPD_BIN=$(command -v usbipd 2>/dev/null || true)
    [[ -z "$USBIPD_BIN" ]] && USBIPD_BIN=$(find /usr/sbin /usr/bin /sbin -name usbipd 2>/dev/null | head -1)
    [[ -z "$USBIPD_BIN" ]] && error "Could not find usbipd — install usbip package manually"
    info "usbipd at: $USBIPD_BIN"
else
    pacman -Sy --noconfirm --needed usbip
    USBIPD_BIN=$(command -v usbipd)
fi

# ─── Step 2: Kernel modules ───────────────────────────────────────────────────
step "2. Loading kernel modules"

modprobe usbip_core
modprobe usbip_host
info "Modules loaded"

# Persist across reboots
if [[ $DISTRO == "debian" ]]; then
    MODULES_FILE="/etc/modules"
else
    MODULES_FILE="/etc/modules-load.d/usbip.conf"
fi

for mod in usbip_core usbip_host; do
    if ! grep -qxF "$mod" "$MODULES_FILE" 2>/dev/null; then
        echo "$mod" >> "$MODULES_FILE"
        info "Added $mod to $MODULES_FILE"
    fi
done

# Detect init system
USE_SYSTEMD=false
if [[ "$(ps -p 1 -o comm=)" == "systemd" ]]; then
    USE_SYSTEMD=true
fi
info "Init system: $( $USE_SYSTEMD && echo systemd || echo SysV/other )"

# ─── Step 3: Service setup ────────────────────────────────────────────────────
step "3. Creating usbipd service"

if $USE_SYSTEMD; then
    cat > /etc/systemd/system/usbipd.service << EOF
[Unit]
Description=USB/IP Daemon
After=network.target
Documentation=https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html

[Service]
Type=forking
ExecStart=${USBIPD_BIN} -D
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    info "Created /etc/systemd/system/usbipd.service"
else
    # SysV init (MX Linux, older Debian, etc.)
    cat > /etc/init.d/usbipd << SYSV
#!/bin/sh
### BEGIN INIT INFO
# Provides:          usbipd
# Required-Start:    \$network
# Required-Stop:     \$network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: USB/IP Daemon
### END INIT INFO
DAEMON=${USBIPD_BIN}
PIDFILE=/var/run/usbipd.pid
case "\$1" in
  start)
    echo "Starting usbipd..."
    start-stop-daemon --start --background --make-pidfile --pidfile \$PIDFILE --exec \$DAEMON
    sleep 2
    nohup /usr/local/bin/usbip-bind-devices >> /var/log/usbip-bind.log 2>&1 &
    ;;
  stop)
    echo "Stopping usbipd..."
    start-stop-daemon --stop --pidfile \$PIDFILE 2>/dev/null || true
    rm -f \$PIDFILE
    ;;
  restart) \$0 stop; sleep 1; \$0 start ;;
  status)
    start-stop-daemon --status --pidfile \$PIDFILE && echo "usbipd running" || echo "usbipd stopped"
    ;;
  *) echo "Usage: \$0 {start|stop|restart|status}"; exit 1 ;;
esac
exit 0
SYSV
    chmod +x /etc/init.d/usbipd
    info "Created /etc/init.d/usbipd (SysV)"
fi

# ─── Step 4: Device bind script ───────────────────────────────────────────────
step "4. Creating device bind script"

cat > /usr/local/bin/usbip-bind-devices << 'BINDEOF'
#!/bin/bash
# =============================================================================
# USBIP Device Binder — auto-bind USB devices for remote sharing
#
# BIND_IDS accepts two formats — use whichever is easier:
#   Bus ID:          "1-1"  or  "1-1.4"   (from: sudo usbip list -l)
#   Vendor:Product:  "0bda:a728"           (from: lsusb)
#
# Run manually:  sudo usbip-bind-devices
# Check log:     cat /var/log/usbip-bind.log
# =============================================================================

BIND_IDS=(
    # Examples — uncomment and edit:
    # "1-1"          # bind by bus ID (simplest)
    # "0bda:a728"    # Realtek Bluetooth 5.4 Radio
    # "0bda:2838"    # RTL2838 DVB-T (ADS-B / RTL-SDR)
    # "1209:7388"    # AIOC (All-In-One Cable)
    # "0403:6001"    # FT232R (CAT cable)
    # "10c4:ea60"    # CP2102 USB-Serial (KV4P)
)

LOG=/var/log/usbip-bind.log
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== usbip-bind-devices starting ==="

if [[ ${#BIND_IDS[@]} -eq 0 ]]; then
    log "No devices configured — edit BIND_IDS in /usr/local/bin/usbip-bind-devices"
    log "Available devices:"
    usbip list -l 2>&1 | tee -a "$LOG"
    exit 0
fi

# Wait briefly for usbipd to be ready
sleep 2

bind_one() {
    local bus_id="$1" label="$2"
    log "Binding $label at bus $bus_id..."
    local out
    out=$(usbip bind -b "$bus_id" 2>&1)
    if echo "$out" | grep -q "complete\|already"; then
        log "  OK: $bus_id bound"
        return 0
    else
        log "  ERROR: $bus_id — $out"
        return 1
    fi
}

bound=0
for id in "${BIND_IDS[@]}"; do
    # Detect format: bus ID looks like digits/dashes/dots only (e.g. 1-1, 1-1.4)
    if [[ "$id" =~ ^[0-9]+(-[0-9]+(\.[0-9]+)*)+$ ]]; then
        # Direct bus ID
        bind_one "$id" "$id" && ((bound++)) || true
    else
        # vendor:product — find matching bus IDs via sysfs
        vendor="${id%%:*}"
        product="${id##*:}"
        found=0
        while IFS= read -r vid_path; do
            v=$(cat "$vid_path" 2>/dev/null)
            p=$(cat "$(dirname "$vid_path")/idProduct" 2>/dev/null)
            if [[ "${v,,}" == "${vendor,,}" && "${p,,}" == "${product,,}" ]]; then
                bus_id=$(basename "$(dirname "$vid_path")")
                bind_one "$bus_id" "$id" && ((bound++)) || true
                found=1
            fi
        done < <(find /sys/bus/usb/devices -maxdepth 2 -name idVendor 2>/dev/null)
        [[ $found -eq 0 ]] && log "WARNING: No device found for $id (not plugged in?)"
    fi
done

log "Done — $bound device(s) bound"
log "Currently exported:"
usbip list -l 2>&1 | tee -a "$LOG"
BINDEOF

chmod +x /usr/local/bin/usbip-bind-devices
info "Created /usr/local/bin/usbip-bind-devices"

# ─── Step 5: usbip-bind service ───────────────────────────────────────────────
step "5. Creating usbip-bind service"

if $USE_SYSTEMD; then
    cat > /etc/systemd/system/usbip-bind.service << 'EOF'
[Unit]
Description=USB/IP Device Binding
After=usbipd.service
Requires=usbipd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/usbip-bind-devices
ExecStop=/bin/bash -c 'usbip list -l 2>/dev/null | grep -oP "^\s+\K[0-9]+-[0-9.]+(?=:)" | xargs -r -I{} usbip unbind -b {} 2>/dev/null; true'

[Install]
WantedBy=multi-user.target
EOF
    info "Created /etc/systemd/system/usbip-bind.service"
else
    info "SysV: bind script called directly from /etc/init.d/usbipd start"
fi

# ─── Step 6: Enable and start ─────────────────────────────────────────────────
step "6. Enabling services"

if $USE_SYSTEMD; then
    systemctl daemon-reload
    systemctl enable usbipd.service
    systemctl enable usbip-bind.service
    systemctl start usbipd.service
    sleep 1
    systemctl is-active usbipd.service && info "usbipd is running" || warn "usbipd failed to start — check: journalctl -u usbipd"
else
    update-rc.d usbipd defaults
    service usbipd start
    sleep 1
    service usbipd status && info "usbipd is running" || warn "usbipd failed to start — check: /var/log/usbip-bind.log"
fi

# ─── Step 7: Firewall ─────────────────────────────────────────────────────────
step "7. Firewall"

if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 3240/tcp comment "USB/IP"
    info "ufw: opened port 3240/tcp"
else
    warn "ufw not active — ensure port 3240/TCP is open to the gateway machine"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  USBIP server setup complete${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. List connected USB devices:"
echo "     lsusb"
echo ""
echo "  2. Edit the bind config and add device IDs to share:"
echo "     sudo nano /usr/local/bin/usbip-bind-devices"
echo ""
echo "  3. Start binding (after editing bind config):"
if $USE_SYSTEMD; then
    echo "     sudo systemctl start usbip-bind"
else
    echo "     sudo service usbipd restart"
fi
echo ""
echo "  4. Verify devices are exported:"
echo "     sudo usbip list -l"
echo ""
echo "  5. From the gateway machine (port 3240 must be reachable):"
echo "     usbip list -r <this-machine-ip>"
echo "     sudo usbip attach -r <this-machine-ip> -b <bus_id>"
echo ""
echo "  Log: /var/log/usbip-bind.log"
echo ""
