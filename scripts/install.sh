#!/bin/bash
# ============================================================
# Mumble Radio Gateway — Installation Script
# Supports: Raspberry Pi, Debian/Ubuntu, Arch Linux
# ============================================================

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
GATEWAY_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "============================================================"
echo "Mumble Radio Gateway - Installation"
echo "============================================================"
echo "Gateway directory: $GATEWAY_DIR"
echo

# ── Detect platform ──────────────────────────────────────────
ARCH=$(uname -m)
IS_PI=false
if [ -f /proc/device-tree/model ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

echo "Platform: $ARCH"
if $IS_PI; then
    echo "Detected: Raspberry Pi"
else
    echo "Detected: Standard Linux PC"
fi

# ── Detect distro / package manager ─────────────────────────
DISTRO="unknown"
if command -v pacman &>/dev/null; then
    DISTRO="arch"
elif command -v apt-get &>/dev/null; then
    DISTRO="debian"
else
    echo "ERROR: No supported package manager found (need apt-get or pacman)"
    exit 1
fi
echo "Package manager: $DISTRO"
echo

# ── 1. System packages ───────────────────────────────────────
echo "[ 1/11 ] Installing system packages..."
if [ "$DISTRO" = "arch" ]; then
    sudo pacman -Sy --noconfirm --needed \
        python \
        python-pip \
        python-pyaudio \
        portaudio \
        hidapi \
        libsndfile \
        ffmpeg \
        git
else
    sudo apt-get update -qq
    sudo apt-get install -y \
        python3 \
        python3-pip \
        python3-pyaudio \
        portaudio19-dev \
        libhidapi-libusb0 \
        libhidapi-dev \
        libsndfile1 \
        ffmpeg \
        git
fi

echo "  ✓ System packages installed"
echo

# ── 2. ALSA loopback module ──────────────────────────────────
echo "[ 2/11 ] Setting up ALSA loopback (for SDR input)..."

# Write modprobe options first:
#   enable=1,1,1 → enable 3 independent loopback cards
#   index=4,5,6  → pin them to hw:4 hw:5 hw:6 on every machine
echo "options snd-aloop enable=1,1,1 index=4,5,6" | sudo tee /etc/modprobe.d/snd-aloop.conf > /dev/null
echo "  ✓ /etc/modprobe.d/snd-aloop.conf → enable=1,1,1 index=4,5,6"

# Show what parameters this kernel's snd-aloop actually supports
echo "  Supported module parameters:"
modinfo snd-aloop 2>/dev/null | grep "^parm:" | sed 's/^/    /' || echo "    (modinfo not available)"

# Stop audio services that may hold the module open
sudo systemctl stop pulseaudio.service pulseaudio.socket \
    pipewire.service pipewire.socket wireplumber.service 2>/dev/null || true

# Unload — try modprobe -r then rmmod -f as fallback
UNLOADED=false
if lsmod | grep -q snd_aloop; then
    if sudo modprobe -r snd-aloop 2>/dev/null; then
        UNLOADED=true
    elif sudo rmmod -f snd_aloop 2>/dev/null; then
        UNLOADED=true
    else
        echo "  ⚠ Could not unload snd-aloop — a reboot will apply the new settings"
    fi
    sleep 1
fi

# Load with explicit parameters
if ! lsmod | grep -q snd_aloop; then
    if sudo modprobe snd-aloop enable=1,1,1 index=4,5,6; then
        echo "  ✓ snd-aloop loaded"
    else
        echo "  ✗ Failed to load snd-aloop"
        exit 1
    fi
fi

# Show what the kernel actually applied
echo "  Active module parameters:"
printf "    enable = "; cat /sys/module/snd_aloop/parameters/enable 2>/dev/null || echo "(unavailable)"
printf "    index  = "; cat /sys/module/snd_aloop/parameters/index  2>/dev/null || echo "(unavailable)"

# Make it load on boot
if [ "$DISTRO" = "arch" ]; then
    if [ ! -f /etc/modules-load.d/snd-aloop.conf ] || ! grep -q "snd-aloop" /etc/modules-load.d/snd-aloop.conf 2>/dev/null; then
        echo "snd-aloop" | sudo tee /etc/modules-load.d/snd-aloop.conf > /dev/null
        echo "  ✓ Added snd-aloop to /etc/modules-load.d/ (auto-load on boot)"
    else
        echo "  ✓ snd-aloop already in /etc/modules-load.d/"
    fi
else
    if ! grep -q "snd-aloop" /etc/modules 2>/dev/null; then
        echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
        echo "  ✓ Added snd-aloop to /etc/modules (auto-load on boot)"
    else
        echo "  ✓ snd-aloop already in /etc/modules"
    fi
fi

# Verify — count cards (each card has 2 devices, count device 0 entries only)
LOOPBACK_LINES=$(aplay -l 2>/dev/null | grep "Loopback" | grep "device 0" || true)
LOOPBACK_COUNT=$(echo "$LOOPBACK_LINES" | grep -c "Loopback" || true)
[ -z "$LOOPBACK_LINES" ] && LOOPBACK_COUNT=0
echo "  Loopback cards visible: $LOOPBACK_COUNT (expected 3 at hw:4 hw:5 hw:6)"
echo "$LOOPBACK_LINES" | grep "Loopback" | sed 's/^/    /' || true
echo

# ── 3. Python packages ───────────────────────────────────────
echo "[ 3/11 ] Installing Python packages..."

# Helper: try --break-system-packages (Debian 12+), then plain pip
_pip() {
    pip3 install "$@" --break-system-packages 2>/dev/null \
        || pip3 install "$@" 2>/dev/null
}

# Ensure setuptools is functional — required by legacy setup.py packages (e.g. pymumble)
# On some Debian systems python3-setuptools is absent or broken, causing metadata-generation-failed
set +e
_pip --upgrade setuptools 2>/dev/null
set -e

# Core packages (excluding pymumble — handled separately due to PyPI name variants)
set +e
_pip hid numpy pyaudio soundfile resampy psutil gtts
CORE_STATUS=$?
set -e
if [ $CORE_STATUS -eq 0 ]; then
    echo "  ✓ Core Python packages installed"
else
    echo "  ⚠ Some core packages may have failed — check output above"
fi

# pymumble: try pymumble-py3 first (Python-3 fork), fall back to pymumble
set +e
MUMBLE_OK=false
if _pip "pymumble-py3>=1.0.0" 2>/dev/null; then
    echo "  ✓ pymumble-py3 installed"
    MUMBLE_OK=true
elif _pip pymumble 2>/dev/null; then
    echo "  ✓ pymumble installed (fallback package name)"
    MUMBLE_OK=true
fi
set -e
if ! $MUMBLE_OK; then
    echo "  ✗ Could not install pymumble automatically"
    echo "    Try manually: pip3 install pymumble --break-system-packages"
    echo "              or: pip3 install pymumble-py3 --break-system-packages"
fi
echo

# ── 4. UDEV rules for AIOC ──────────────────────────────────
echo "[ 4/11 ] Setting up UDEV rules for AIOC USB device..."
UDEV_RULE='SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="audio"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="plugdev"'

if [ ! -f /etc/udev/rules.d/99-aioc.rules ]; then
    echo "$UDEV_RULE" | sudo tee /etc/udev/rules.d/99-aioc.rules > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "  ✓ UDEV rules installed — AIOC accessible without sudo"
else
    echo "  ✓ UDEV rules already exist"
fi
echo

# ── 5. Audio group, realtime limits, and sudoers ─────────────────
echo "[ 5/11 ] Setting up audio permissions..."
set +e   # None of this should abort the install

# Determine the real (non-root) user running this script
ACTUAL_USER=${SUDO_USER:-$USER}

# Add user to audio group (ALSA device access + darkice realtime scheduling)
if id -nG "$ACTUAL_USER" 2>/dev/null | grep -qw audio; then
    echo "  ✓ $ACTUAL_USER already in audio group"
else
    if sudo usermod -aG audio "$ACTUAL_USER" 2>/dev/null; then
        echo "  ✓ Added $ACTUAL_USER to audio group (takes effect on next login)"
    else
        echo "  ⚠ Could not add $ACTUAL_USER to audio group — run manually: sudo usermod -aG audio $ACTUAL_USER"
    fi
fi

# Allow audio group to use realtime scheduling (required by darkice without sudo)
sudo mkdir -p /etc/security/limits.d
if [ ! -f /etc/security/limits.d/audio-realtime.conf ]; then
    printf '@audio\t-\trtprio\t95\n@audio\t-\tmemlock\tunlimited\n' \
        | sudo tee /etc/security/limits.d/audio-realtime.conf > /dev/null \
        && echo "  ✓ Realtime scheduling limits configured for audio group" \
        || echo "  ⚠ Could not write audio-realtime.conf — darkice may need sudo"
else
    echo "  ✓ /etc/security/limits.d/audio-realtime.conf already exists"
fi

# Allow passwordless sudo for modprobe snd-aloop (used by start.sh on each run)
MODPROBE_BIN=$(which modprobe 2>/dev/null || echo /usr/sbin/modprobe)
SUDOERS_FILE=/etc/sudoers.d/mumble-gateway
if [ -n "$ACTUAL_USER" ]; then
    printf '%s ALL=(ALL) NOPASSWD: %s snd-aloop, %s -r snd-aloop\n' \
        "$ACTUAL_USER" "$MODPROBE_BIN" "$MODPROBE_BIN" \
        | sudo tee "$SUDOERS_FILE" > /dev/null \
        && sudo chmod 440 "$SUDOERS_FILE" \
        && echo "  ✓ Passwordless sudo configured for modprobe snd-aloop" \
        || echo "  ⚠ Could not write sudoers rule — start.sh will prompt for sudo password"
fi

set -e
echo

# ── 6. Darkice (optional — for Broadcastify/Icecast streaming) ───
echo "[ 6/11 ] Darkice streaming (optional)..."
set +e
if [ "$DISTRO" = "arch" ]; then
    if sudo pacman -S --noconfirm --needed lame 2>/dev/null; then
        echo "  ✓ lame (MP3 encoder) installed"
    else
        echo "  ⚠ Could not install lame — streaming may not work"
    fi
    if command -v darkice &>/dev/null; then
        echo "  ✓ Darkice already installed"
        DARKICE_STATUS=0
    else
        # AUR helpers refuse to run as root — run as the real user
        AUR_USER=${SUDO_USER:-$USER}
        AUR_HELPER=""
        for helper in yay paru; do
            if sudo -u "$AUR_USER" bash -c "command -v $helper" &>/dev/null; then
                AUR_HELPER="$helper"
                break
            fi
        done
        if [ -n "$AUR_HELPER" ]; then
            echo "  Installing darkice from AUR via $AUR_HELPER..."
            # Remove any pre-existing /etc/darkice.cfg to avoid pacman file conflict
            if [ -f /etc/darkice.cfg ]; then
                sudo rm -f /etc/darkice.cfg
            fi
            if sudo -u "$AUR_USER" $AUR_HELPER -S --noconfirm darkice 2>/dev/null; then
                echo "  ✓ Darkice installed from AUR"
                DARKICE_STATUS=0
            else
                echo "  ⚠ Skipping darkice — AUR install via $AUR_HELPER failed"
                echo "    Try manually: $AUR_HELPER -S darkice"
                echo "    This is optional: only needed for Broadcastify/Icecast streaming"
                DARKICE_STATUS=1
            fi
        else
            echo "  ⚠ Skipping darkice — no AUR helper found (yay/paru)"
            echo "    Install an AUR helper, then run: yay -S darkice"
            echo "    This is optional: only needed for Broadcastify/Icecast streaming"
            DARKICE_STATUS=1
        fi
    fi
else
    sudo apt-get install -y darkice lame 2>/dev/null
    DARKICE_STATUS=$?
    if [ $DARKICE_STATUS -eq 0 ]; then
        echo "  ✓ Darkice + lame installed"
    else
        echo "  ⚠ Skipping darkice — could not install from apt"
        echo "    To install manually: sudo apt-get install darkice lame"
        echo "    This is optional: only needed for Broadcastify/Icecast streaming"
    fi
fi

# Create /etc/darkice.cfg from example if it doesn't exist
DARKICE_CFG=/etc/darkice.cfg
DARKICE_EXAMPLE="$GATEWAY_DIR/scripts/darkice.cfg.example"
if [ $DARKICE_STATUS -eq 0 ]; then
    if [ ! -f "$DARKICE_CFG" ]; then
        if [ -f "$DARKICE_EXAMPLE" ]; then
            sudo cp "$DARKICE_EXAMPLE" "$DARKICE_CFG" \
                && echo "  ✓ Created $DARKICE_CFG — edit with your Broadcastify credentials" \
                || echo "  ⚠ Could not create $DARKICE_CFG — copy manually from $DARKICE_EXAMPLE"
        else
            echo "  ⚠ Example not found — create $DARKICE_CFG manually"
        fi
    else
        echo "  ✓ $DARKICE_CFG already exists (not overwritten)"
    fi
else
    echo "  Skipping darkice.cfg setup — darkice not installed"
fi

# Configure WirePlumber to not manage ALSA loopback devices
# (prevents it locking them to S32_LE and blocking DarkIce)
WIREPLUMBER_CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WIREPLUMBER_CONF="$WIREPLUMBER_CONF_DIR/99-disable-loopback.conf"
WIREPLUMBER_SRC="$GATEWAY_DIR/scripts/99-disable-loopback.conf"
if command -v wireplumber &>/dev/null || systemctl --user is-active wireplumber &>/dev/null; then
    mkdir -p "$WIREPLUMBER_CONF_DIR"
    if [ ! -f "$WIREPLUMBER_CONF" ]; then
        if [ -f "$WIREPLUMBER_SRC" ]; then
            cp "$WIREPLUMBER_SRC" "$WIREPLUMBER_CONF" \
                && echo "  ✓ WirePlumber loopback exclusion installed" \
                || echo "  ⚠ Could not install WirePlumber config — DarkIce may fail to open audio device"
        else
            echo "  ⚠ $WIREPLUMBER_SRC not found — skipping WirePlumber config"
        fi
    else
        echo "  ✓ WirePlumber loopback config already exists (not overwritten)"
    fi
    systemctl --user restart wireplumber 2>/dev/null || true
else
    echo "  Skipping WirePlumber config — wireplumber not running"
fi

set -e
echo

# ── 7. Mumble GUI client ─────────────────────────────────────
echo "[ 7/11 ] Installing Mumble client..."
set +e
if [ "$DISTRO" = "arch" ]; then
    sudo pacman -S --noconfirm --needed mumble 2>/dev/null
else
    sudo apt-get install -y mumble 2>/dev/null
fi
if [ $? -eq 0 ]; then
    echo "  ✓ Mumble client installed"
else
    echo "  ⚠ Could not install mumble — install manually"
fi
set -e
echo

# ── 8. Mumble server (murmurd) ───────────────────────────────
echo "[ 8/11 ] Installing Mumble server (optional — for local server instances)..."
set +e
if [ "$DISTRO" = "arch" ]; then
    if sudo pacman -S --noconfirm --needed mumble-server 2>/dev/null; then
        echo "  ✓ mumble-server installed"
        # Create the _mumble-server system user and directories
        sudo systemd-sysusers /usr/lib/sysusers.d/mumble-server.conf 2>/dev/null || true
        sudo systemd-tmpfiles --create /usr/lib/tmpfiles.d/mumble-server.conf 2>/dev/null || true
        # Disable the default mumble-server service — gateway manages its own instances
        sudo systemctl stop mumble-server.service 2>/dev/null || true
        sudo systemctl disable mumble-server.service 2>/dev/null || true
        echo "  ✓ Default mumble-server service disabled (gateway manages its own instances)"
    else
        echo "  ⚠ Could not install mumble-server — install manually if needed"
        echo "    This is optional: only needed if ENABLE_MUMBLE_SERVER_1/2 = true"
    fi
else
    if sudo apt-get install -y mumble-server 2>/dev/null; then
        echo "  ✓ mumble-server installed"
        # Disable the default mumble-server service — gateway manages its own instances
        sudo systemctl stop mumble-server.service 2>/dev/null || true
        sudo systemctl disable mumble-server.service 2>/dev/null || true
        echo "  ✓ Default mumble-server service disabled (gateway manages its own instances)"
    else
        echo "  ⚠ Could not install mumble-server — install manually if needed"
        echo "    This is optional: only needed if ENABLE_MUMBLE_SERVER_1/2 = true"
    fi
fi

# Ensure required directories exist with correct ownership
for MSDIR in /var/lib/mumble-server /var/log/mumble-server /var/run/mumble-server; do
    sudo mkdir -p "$MSDIR" 2>/dev/null || true
done
# Set ownership — Arch uses '_mumble-server', Debian uses 'mumble-server'
if id _mumble-server &>/dev/null; then
    MS_USER=_mumble-server
elif id mumble-server &>/dev/null; then
    MS_USER=mumble-server
else
    MS_USER=""
fi
if [ -n "$MS_USER" ]; then
    sudo chown "$MS_USER:$MS_USER" /var/lib/mumble-server /var/log/mumble-server /var/run/mumble-server 2>/dev/null || true
    echo "  ✓ Mumble server directories created (owned by $MS_USER)"
fi
set -e
echo

# ── 9. OpenSSL TLS compatibility (for older Mumble servers) ──
echo "[ 9/11 ] Configuring OpenSSL for TLS 1.0 compatibility..."
OPENSSL_CNF="/etc/ssl/openssl.cnf"
if [ -f "$OPENSSL_CNF" ]; then
    # Check if already patched
    if grep -q "MinProtocol = TLSv1" "$OPENSSL_CNF" 2>/dev/null; then
        echo "  ✓ OpenSSL TLS 1.0 compatibility already configured"
    else
        # Back up original
        sudo cp "$OPENSSL_CNF" "${OPENSSL_CNF}.bak"
        echo "  ✓ Backed up $OPENSSL_CNF to ${OPENSSL_CNF}.bak"

        # Add ssl_conf directive under [openssl_init] if not present
        if ! grep -q "^ssl_conf" "$OPENSSL_CNF" 2>/dev/null; then
            sudo sed -i '/^\[openssl_init\]/a ssl_conf = ssl_sect' "$OPENSSL_CNF"
        fi

        # Add [ssl_sect] and [system_default_sect] sections before [provider_sect]
        if ! grep -q "^\[ssl_sect\]" "$OPENSSL_CNF" 2>/dev/null; then
            sudo sed -i '/^\[provider_sect\]/i \[ssl_sect\]\nsystem_default = system_default_sect\n\n[system_default_sect]\nMinProtocol = TLSv1\nCipherString = DEFAULT:@SECLEVEL=0\n' "$OPENSSL_CNF"
        fi

        echo "  ✓ OpenSSL patched: TLS 1.0 + SECLEVEL=0 (needed for older Mumble servers)"
    fi
else
    echo "  ⚠ $OPENSSL_CNF not found — skipping TLS compatibility patch"
fi
echo

# ── 10. Gateway configuration ────────────────────────────────
echo "[ 10/11 ] Setting up configuration..."

CONFIG_DEST="$GATEWAY_DIR/gateway_config.txt"
CONFIG_SRC="$GATEWAY_DIR/examples/gateway_config.txt"

if [ ! -f "$CONFIG_DEST" ]; then
    if [ -f "$CONFIG_SRC" ]; then
        cp "$CONFIG_SRC" "$CONFIG_DEST"
        echo "  ✓ Created gateway_config.txt from example"
    else
        echo "  ⚠ Example config not found — you will need to create gateway_config.txt manually"
    fi
else
    echo "  ✓ gateway_config.txt already exists (not overwritten)"
fi

# Create audio directory for announcements
mkdir -p "$GATEWAY_DIR/audio"
echo "  ✓ audio/ directory ready (place announcement files here)"
echo

# ── 11. Make scripts executable ──────────────────────────────
echo "[ 11/11 ] Setting permissions..."
chmod +x "$GATEWAY_DIR/mumble_radio_gateway.py" 2>/dev/null || true
chmod +x "$GATEWAY_DIR/scripts/"*.sh 2>/dev/null || true
chmod +x "$GATEWAY_DIR/start.sh" 2>/dev/null || true
echo "  ✓ Scripts are executable"
echo

# ── Summary ──────────────────────────────────────────────────
echo "============================================================"
echo "Installation complete!"
echo "============================================================"
echo
echo "NEXT STEPS:"
echo
echo "  1. Edit gateway_config.txt:"
echo "       MUMBLE_SERVER   = your.mumble.server"
echo "       MUMBLE_PORT     = 64738"
echo "       MUMBLE_USERNAME = RadioGateway"
echo
echo "  2. If using Broadcastify streaming, edit /etc/darkice.cfg:"
echo "       password  = YOUR_STREAM_PASSWORD"
echo "       mountPoint = YOUR_STREAM_KEY"
echo "       device    = hw:<card>,1,0  (check: aplay -l | grep Loopback)"
echo
echo "  3. Connect your AIOC USB device"
echo "     (unplug and replug after install so udev rules take effect)"
echo
echo "  4. Log out and back in so audio group membership takes effect"
echo "     (needed for darkice realtime scheduling without sudo)"
echo
echo "  5. Run the gateway:"
echo "       python3 $GATEWAY_DIR/mumble_radio_gateway.py"
echo
echo "SDR INPUT (optional):"
echo "  Route SDR software audio output to ALSA loopback hw:X,0"
echo "  Gateway reads from the capture side: hw:X,1"
echo "  Set SDR_DEVICE_NAME in gateway_config.txt"
echo "  Verify loopback devices: aplay -l | grep Loopback"
echo
echo "STREAMING (optional):"
echo "  Configure /etc/darkice.cfg with your Broadcastify credentials"
echo "  Set ENABLE_STREAM_OUTPUT = true in gateway_config.txt"
echo "  Use start.sh to launch gateway + Darkice together"
echo
echo "LOCAL MUMBLE SERVER (optional):"
echo "  Set ENABLE_MUMBLE_SERVER_1 = true in gateway_config.txt"
echo "  Configure port, password, and max users as needed"
echo "  The gateway will create and manage the server instance via systemd"
echo "  Firewall: sudo ufw allow 64738/tcp && sudo ufw allow 64738/udp"
echo
echo "DOCS:"
echo "  README.md                       — full documentation"
echo "  docs/MANUAL.txt                 — user guide"
echo "  docs/TTS_TEXT_COMMANDS_GUIDE.md — Mumble text commands"
echo
