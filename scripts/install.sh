#!/bin/bash
# ============================================================
# Mumble Radio Gateway — Installation Script
# Supports: Raspberry Pi, Debian/Ubuntu amd64, any Debian-based Linux
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
echo

# ── 1. System packages ───────────────────────────────────────
echo "[ 1/10 ] Installing system packages..."
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

echo "  ✓ System packages installed"
echo

# ── 2. ALSA loopback module ──────────────────────────────────
echo "[ 2/10 ] Setting up ALSA loopback (for SDR input)..."

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
if ! grep -q "snd-aloop" /etc/modules 2>/dev/null; then
    echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
    echo "  ✓ Added snd-aloop to /etc/modules (auto-load on boot)"
else
    echo "  ✓ snd-aloop already in /etc/modules"
fi

# Verify — count cards (each card has 2 devices, count device 0 entries only)
LOOPBACK_COUNT=$(aplay -l 2>/dev/null | grep "Loopback" | grep -c "device 0" || true)
echo "  Loopback cards visible: $LOOPBACK_COUNT (expected 3 at hw:4 hw:5 hw:6)"
aplay -l 2>/dev/null | grep "Loopback" | grep "device 0" | sed 's/^/    /'
echo

# ── 3. Python packages ───────────────────────────────────────
echo "[ 3/10 ] Installing Python packages..."

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
echo "[ 4/10 ] Setting up UDEV rules for AIOC USB device..."
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
echo "[ 5/10 ] Setting up audio permissions..."
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
echo "[ 6/10 ] Darkice streaming (optional)..."
set +e
sudo apt-get install -y darkice lame 2>/dev/null
DARKICE_STATUS=$?
if [ $DARKICE_STATUS -eq 0 ]; then
    echo "  ✓ Darkice installed"
else
    echo "  ⚠ darkice could not be installed from apt — skipping"
    echo "    This is optional: streaming to Broadcastify requires darkice,"
    echo "    but all other gateway features work without it."
    echo "    To install manually: sudo apt-get install darkice lame"
fi

# Create /etc/darkice.cfg from example if it doesn't exist
DARKICE_CFG=/etc/darkice.cfg
DARKICE_EXAMPLE="$GATEWAY_DIR/scripts/darkice.cfg.example"
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

# Configure WirePlumber to not manage ALSA loopback devices
# (prevents it locking them to S32_LE and blocking DarkIce)
WIREPLUMBER_CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WIREPLUMBER_CONF="$WIREPLUMBER_CONF_DIR/99-disable-loopback.conf"
WIREPLUMBER_SRC="$GATEWAY_DIR/scripts/99-disable-loopback.conf"
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

set -e
echo

# ── 7. Mumble GUI client ─────────────────────────────────────
echo "[ 7/10 ] Installing Mumble client..."
set +e
sudo apt-get install -y mumble 2>/dev/null
if [ $? -eq 0 ]; then
    echo "  ✓ Mumble client installed"
else
    echo "  ⚠ Could not install mumble — install manually: sudo apt-get install mumble"
fi
set -e
echo

# ── 8. OpenSSL TLS compatibility (for older Mumble servers) ──
echo "[ 8/10 ] Configuring OpenSSL for TLS 1.0 compatibility..."
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

# ── 9. Gateway configuration ─────────────────────────────────
echo "[ 9/10 ] Setting up configuration..."

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

# ── 10. Make scripts executable ──────────────────────────────
echo "[ 10/10 ] Setting permissions..."
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
echo "DOCS:"
echo "  README.md                       — full documentation"
echo "  docs/MANUAL.txt                 — user guide"
echo "  docs/TTS_TEXT_COMMANDS_GUIDE.md — Mumble text commands"
echo
