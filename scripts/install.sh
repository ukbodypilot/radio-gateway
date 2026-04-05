#!/bin/bash
# ============================================================
# Radio Gateway — Installation Script
# Supports: Raspberry Pi, Debian/Ubuntu, Arch Linux
# ============================================================

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
GATEWAY_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "============================================================"
echo "Radio Gateway - Installation"
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
echo "[ 1/15 ] Installing system packages..."
if [ "$DISTRO" = "arch" ]; then
    sudo pacman -Sy --noconfirm --needed \
        python \
        python-pip \
        python-pyaudio \
        portaudio \
        hidapi \
        libsndfile \
        ffmpeg \
        opus \
        alsa-utils \
        git \
        tmux \
        avahi \
        nss-mdns
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
        libopus0 \
        libopus-dev \
        git \
        tmux \
        alsa-utils \
        avahi-daemon \
        avahi-utils
fi

echo "  ✓ System packages installed"

# cloudflared — Cloudflare tunnel for free public HTTPS access (optional)
echo "  Installing cloudflared..."
if command -v cloudflared &>/dev/null; then
    echo "  ✓ cloudflared already installed"
elif [ "$DISTRO" = "arch" ]; then
    # Try AUR helper first, fall back to manual binary
    if command -v yay &>/dev/null; then
        yay -S --noconfirm cloudflared-bin 2>/dev/null \
            && echo "  ✓ cloudflared installed (AUR)" \
            || echo "  ⚠ cloudflared install failed — Cloudflare tunnel will not work"
    elif command -v paru &>/dev/null; then
        paru -S --noconfirm cloudflared-bin 2>/dev/null \
            && echo "  ✓ cloudflared installed (AUR)" \
            || echo "  ⚠ cloudflared install failed — Cloudflare tunnel will not work"
    else
        echo "  ⚠ No AUR helper found — install cloudflared manually:"
        echo "    yay -S cloudflared-bin"
    fi
else
    # Debian/Ubuntu/RPi — official Cloudflare repo
    set +e
    if curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null 2>&1 \
        && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null 2>&1 \
        && sudo apt-get update -qq 2>/dev/null \
        && sudo apt-get install -y cloudflared 2>/dev/null; then
        echo "  ✓ cloudflared installed (Cloudflare repo)"
    else
        echo "  ⚠ cloudflared install failed — Cloudflare tunnel will not work"
        echo "    See: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    fi
    set -e
fi
echo

# ── 2. ALSA loopback module ──────────────────────────────────
echo "[ 2/15 ] Setting up ALSA loopback (for SDR input)..."

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
# Cards may take a moment to appear after modprobe — retry up to 3s
LOOPBACK_COUNT=0
for _wait in 1 2 3 4 5 6; do
    LOOPBACK_LINES=$(aplay -l 2>/dev/null | grep "Loopback" | grep "device 0" || true)
    LOOPBACK_COUNT=$(echo "$LOOPBACK_LINES" | grep -c "Loopback" || true)
    [ -z "$LOOPBACK_LINES" ] && LOOPBACK_COUNT=0
    [ "$LOOPBACK_COUNT" -ge 3 ] && break
    sleep 0.5
done
echo "  Loopback cards visible: $LOOPBACK_COUNT (expected 3 at hw:4 hw:5 hw:6)"
echo "$LOOPBACK_LINES" | grep "Loopback" | sed 's/^/    /' || true
echo

# ── 3. Python packages ───────────────────────────────────────
echo "[ 3/15 ] Installing Python packages..."

# Helper: try --break-system-packages (Debian 12+), then plain pip
_pip() {
    pip3 install "$@" --break-system-packages 2>/dev/null \
        || pip3 install "$@" 2>/dev/null
}

# Ensure setuptools is present — required by legacy setup.py packages (e.g. pymumble)
# Only install if missing (skip upgrade check — slow on Pi)
set +e
if ! python3 -c "import setuptools" 2>/dev/null; then
    _pip setuptools 2>/dev/null
fi
set -e

# Core packages (excluding pymumble — handled separately due to PyPI name variants)
# Only install packages that are missing — avoids slow pip index checks on re-run
CORE_PKGS="hid numpy scipy pyaudio soundfile resampy psutil gtts edge-tts pyserial opuslib"
MISSING_PKGS=""
for pkg in $CORE_PKGS; do
    # Map pip names to Python import names where they differ
    case "$pkg" in
        pyaudio)     imp="pyaudio" ;;
        soundfile)   imp="soundfile" ;;
        gtts)        imp="gtts" ;;
        edge-tts)    imp="edge_tts" ;;
        pyserial)    imp="serial" ;;
        opuslib)     imp="opuslib" ;;
        *)           imp="$pkg" ;;
    esac
    if ! python3 -c "import $imp" 2>/dev/null; then
        MISSING_PKGS="$MISSING_PKGS $pkg"
    fi
done

set +e
if [ -n "$MISSING_PKGS" ]; then
    echo "  Installing missing packages:$MISSING_PKGS"
    _pip $MISSING_PKGS
    CORE_STATUS=$?
    if [ $CORE_STATUS -eq 0 ]; then
        echo "  ✓ Core Python packages installed"
    else
        echo "  ⚠ Some core packages may have failed — check output above"
    fi
else
    echo "  ✓ All core Python packages already installed"
fi

# pymumble: try pymumble-py3 first (Python-3 fork), fall back to pymumble
MUMBLE_OK=false
if python3 -c "import pymumble_py3" 2>/dev/null || python3 -c "import pymumble" 2>/dev/null; then
    echo "  ✓ pymumble already installed"
    MUMBLE_OK=true
elif _pip "pymumble-py3>=1.0.0" 2>/dev/null; then
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

# ── 3b. KV4P HT Python driver ────────────────────────────────
echo "       Installing KV4P HT Python driver..."
KV4P_DIR="$HOME/kv4p-ht-python"
if [ -d "$KV4P_DIR" ]; then
    echo "  ✓ kv4p-ht-python already exists at $KV4P_DIR"
    # Pull latest
    (cd "$KV4P_DIR" && git pull --ff-only 2>/dev/null) \
        && echo "  ✓ Updated to latest" \
        || echo "  ⚠ Could not update — using existing version"
else
    if git clone https://github.com/ukbodypilot/kv4p-ht-python.git "$KV4P_DIR" 2>/dev/null; then
        echo "  ✓ Cloned kv4p-ht-python to $KV4P_DIR"
    else
        echo "  ⚠ Could not clone kv4p-ht-python — KV4P radio support will not work"
        echo "    Clone manually: git clone https://github.com/ukbodypilot/kv4p-ht-python.git $KV4P_DIR"
    fi
fi

# Install in editable mode so gateway can import it
if [ -d "$KV4P_DIR" ]; then
    set +e
    _pip -e "$KV4P_DIR" 2>/dev/null \
        && echo "  ✓ kv4p-ht-python installed (editable)" \
        || echo "  ⚠ Could not pip install kv4p-ht-python — gateway will use sys.path fallback"
    set -e
fi

# MCP Python package — required for gateway_mcp.py (AI control interface)
set +e
_pip mcp 2>/dev/null \
    && echo "  ✓ mcp installed (MCP server for AI control)" \
    || echo "  ⚠ Could not pip install mcp — gateway_mcp.py will not work (run: pip install mcp)"
set -e

# sounddevice — CFFI-based audio I/O (used as fallback for direct ALSA access)
set +e
_pip sounddevice 2>/dev/null \
    && echo "  ✓ sounddevice installed" \
    || echo "  ⚠ Could not pip install sounddevice — not critical, arecord used as primary"
set -e

# Pat Winlink client — packet radio email (optional)
if command -v pat &>/dev/null; then
    echo "  ✓ Pat Winlink client already installed ($(pat version 2>/dev/null | head -1))"
else
    echo "  Installing Pat Winlink client..."
    _PAT_VER="0.19.2"
    _PAT_ARCH="$(uname -m)"
    case "$_PAT_ARCH" in
        x86_64)  _PAT_ARCH="amd64" ;;
        aarch64) _PAT_ARCH="arm64" ;;
        armv7l)  _PAT_ARCH="armhf" ;;
        armv6l)  _PAT_ARCH="armhf" ;;
    esac
    _PAT_URL="https://github.com/la5nta/pat/releases/download/v${_PAT_VER}/pat_${_PAT_VER}_linux_${_PAT_ARCH}.tar.gz"
    if curl -sL "$_PAT_URL" -o /tmp/pat.tar.gz && tar xzf /tmp/pat.tar.gz -C /tmp/; then
        _PAT_BIN="$(find /tmp -name pat -type f -executable 2>/dev/null | head -1)"
        if [ -z "$_PAT_BIN" ]; then
            _PAT_BIN="$(find /tmp/pat_* -name pat -type f 2>/dev/null | head -1)"
        fi
        if [ -n "$_PAT_BIN" ]; then
            sudo cp "$_PAT_BIN" /usr/local/bin/pat
            sudo chmod +x /usr/local/bin/pat
            echo "  ✓ Pat v${_PAT_VER} installed to /usr/local/bin/pat"
        else
            echo "  ⚠ Pat binary not found in archive"
        fi
        rm -f /tmp/pat.tar.gz
    else
        echo "  ⚠ Could not download Pat — Winlink email will not work"
        echo "    Download manually: https://github.com/la5nta/pat/releases"
    fi
fi

# faster-whisper — local voice-to-text transcription engine (optional, ~500MB model download on first use)
set +e
if python3 -c "import faster_whisper" 2>/dev/null; then
    echo "  ✓ faster-whisper already installed"
else
    _pip faster-whisper 2>/dev/null \
        && echo "  ✓ faster-whisper installed (transcription engine)" \
        || echo "  ⚠ Could not pip install faster-whisper — transcription will not work"
fi
set -e

# UDEV rule for CP2102 (KV4P HT USB-serial chip) — stable /dev/kv4p symlink
KV4P_UDEV='SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="kv4p", MODE="0666"'
if [ ! -f /etc/udev/rules.d/99-kv4p.rules ]; then
    echo "$KV4P_UDEV" | sudo tee /etc/udev/rules.d/99-kv4p.rules > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=tty
    echo "  ✓ UDEV rule installed — KV4P HT will appear as /dev/kv4p"
else
    echo "  ✓ KV4P UDEV rule already exists"
fi
echo

# ── 4. UDEV rules for AIOC ──────────────────────────────────
echo "[ 4/15 ] Setting up UDEV rules for AIOC USB device..."
UDEV_RULE='SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="audio"
SUBSYSTEM=="hidraw", SUBSYSTEMS=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="audio"
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0666", GROUP="uucp"'

if [ ! -f /etc/udev/rules.d/99-aioc.rules ]; then
    echo "$UDEV_RULE" | sudo tee /etc/udev/rules.d/99-aioc.rules > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "  ✓ UDEV rules installed — AIOC accessible without sudo"
else
    echo "  ✓ UDEV rules already exist"
fi
echo

# ── 4b. UDEV rules for CH340 USB relay ───────────────────────
echo "       Setting up UDEV rules for USB relay (optional)..."

# Always re-detect relays — hardware or purpose may have changed between runs
# Find CH340-style USB serial devices (product="USB Serial", vendor 1a86)
RELAY_PORTS=()
RELAY_DEVS=()
for tty in /dev/ttyUSB*; do
    [ -e "$tty" ] || continue
    prod="$(udevadm info -a -n "$tty" 2>/dev/null | grep -m1 'ATTRS{product}==' | sed 's/.*"\(.*\)"/\1/')"
    mfr="$(udevadm info -a -n "$tty" 2>/dev/null | grep -m1 'ATTRS{manufacturer}==' | sed 's/.*"\(.*\)"/\1/')"
    # CH340 typically shows product="USB Serial" with no manufacturer (or vendor "1a86")
    if [ "$prod" = "USB Serial" ] || echo "$mfr" | grep -qi "1a86"; then
        kpath="$(udevadm info -a -n "$tty" 2>/dev/null | grep 'KERNELS==' | sed -n '2p' | sed 's/.*"\(.*\)"/\1/')"
        if [ -n "$kpath" ]; then
            RELAY_PORTS+=("$kpath")
            RELAY_DEVS+=("$tty")
        fi
    fi
done

if [ ${#RELAY_PORTS[@]} -eq 0 ]; then
    echo "  ⚠ No CH340 USB relay detected (skipping — plug in relay and re-run installer)"
    # Remove stale rules if hardware is gone
    if [ -f /etc/udev/rules.d/99-relay-udev.rules ]; then
        sudo rm -f /etc/udev/rules.d/99-relay-udev.rules
        sudo udevadm control --reload-rules
        echo "  ✓ Removed stale relay udev rules"
    fi
else
    # Helper: click a relay on then off (visible/audible identification)
    # Uses python3 for reliable binary serial I/O (bash can't handle null bytes)
    relay_identify() {
        local dev="$1"
        python3 -c "
import serial, time
try:
    s = serial.Serial('$dev', 9600, timeout=1)
    for _ in range(3):
        s.write(b'\xA0\x01\x01\xA2')  # ON
        time.sleep(0.3)
        s.write(b'\xA0\x01\x00\xA1')  # OFF
        time.sleep(0.3)
    s.close()
except Exception as e:
    print(f'    ⚠ Could not click relay: {e}')
" 2>/dev/null
    }

    echo "  Found ${#RELAY_PORTS[@]} CH340 USB serial device(s):"
    for i in "${!RELAY_PORTS[@]}"; do
        echo "    $((i+1)). ${RELAY_DEVS[$i]} (USB port ${RELAY_PORTS[$i]})"
    done
    echo ""

    if [ ${#RELAY_PORTS[@]} -eq 1 ]; then
        # Single relay — just ask what it's for
        echo "  Assign this relay a purpose:"
        echo "    r = Radio power button (/dev/relay_radio)"
        echo "    c = Charger control    (/dev/relay_charger)"
        echo "    p = PTT control        (/dev/relay_ptt)"
        echo "    t = Test (click relay to confirm it works)"
        echo "    s = Skip (don't assign)"
        echo ""
        while true; do
            echo -n "  ${RELAY_DEVS[0]} — purpose? [r/c/p/t/s]: "
            read -r purpose
            case "$purpose" in
                t|T)
                    echo "    Clicking relay 3 times..."
                    relay_identify "${RELAY_DEVS[0]}"
                    echo "    Did you see/hear it? Choose purpose now."
                    continue
                    ;;
                r|R)
                    printf 'SUBSYSTEM=="tty", KERNELS=="%s", SYMLINK+="relay_radio", MODE="0666"\n' \
                        "${RELAY_PORTS[0]}" | sudo tee /etc/udev/rules.d/99-relay-udev.rules > /dev/null
                    echo "    → /dev/relay_radio"
                    break
                    ;;
                c|C)
                    printf 'SUBSYSTEM=="tty", KERNELS=="%s", SYMLINK+="relay_charger", MODE="0666"\n' \
                        "${RELAY_PORTS[0]}" | sudo tee /etc/udev/rules.d/99-relay-udev.rules > /dev/null
                    echo "    → /dev/relay_charger"
                    break
                    ;;
                p|P)
                    printf 'SUBSYSTEM=="tty", KERNELS=="%s", SYMLINK+="relay_ptt", MODE="0666"\n' \
                        "${RELAY_PORTS[0]}" | sudo tee /etc/udev/rules.d/99-relay-udev.rules > /dev/null
                    echo "    → /dev/relay_ptt"
                    break
                    ;;
                s|S|"")
                    echo "    → skipped"
                    sudo rm -f /etc/udev/rules.d/99-relay-udev.rules
                    break
                    ;;
                *)  echo "    Invalid — enter r, c, p, t, or s" ;;
            esac
        done
    else
        # Multiple relays — identify before assigning
        echo "  Multiple relays detected — you need to identify which is which."
        echo "  Press a number to click that relay (3 on/off pulses), then assign purposes."
        echo ""

        # Identification loop — let user click relays until they know which is which
        while true; do
            echo "  Identify relays (click to find which is which):"
            for i in "${!RELAY_PORTS[@]}"; do
                echo "    $((i+1)) = Click relay at ${RELAY_DEVS[$i]}"
            done
            echo "    d = Done identifying, assign purposes now"
            echo -n "  Choice: "
            read -r id_choice
            if [ "$id_choice" = "d" ] || [ "$id_choice" = "D" ]; then
                break
            elif [ "$id_choice" -ge 1 ] 2>/dev/null && [ "$id_choice" -le ${#RELAY_PORTS[@]} ]; then
                idx=$((id_choice - 1))
                echo "    Clicking relay $id_choice (${RELAY_DEVS[$idx]})..."
                relay_identify "${RELAY_DEVS[$idx]}"
                echo "    Done."
            else
                echo "    ⚠ Invalid — enter a relay number or d"
            fi
            echo ""
        done

        # Assignment loop
        echo ""
        echo "  Now assign each relay a purpose:"
        echo "    r = Radio power button (/dev/relay_radio)"
        echo "    c = Charger control    (/dev/relay_charger)"
        echo "    p = PTT control        (/dev/relay_ptt)"
        echo "    s = Skip (don't assign)"
        echo ""

        RULES=""
        ASSIGNED_RADIO=false
        ASSIGNED_CHARGER=false
        ASSIGNED_PTT=false
        for i in "${!RELAY_PORTS[@]}"; do
            while true; do
                echo -n "  Relay $((i+1)) (${RELAY_DEVS[$i]}) — purpose? [r/c/p/s]: "
                read -r purpose
                case "$purpose" in
                    r|R)
                        if $ASSIGNED_RADIO; then
                            echo "    Radio relay already assigned — pick another"
                            continue
                        fi
                        RULES="${RULES}SUBSYSTEM==\"tty\", KERNELS==\"${RELAY_PORTS[$i]}\", SYMLINK+=\"relay_radio\", MODE=\"0666\"\n"
                        ASSIGNED_RADIO=true
                        echo "    → /dev/relay_radio"
                        break
                        ;;
                    c|C)
                        if $ASSIGNED_CHARGER; then
                            echo "    Charger relay already assigned — pick another"
                            continue
                        fi
                        RULES="${RULES}SUBSYSTEM==\"tty\", KERNELS==\"${RELAY_PORTS[$i]}\", SYMLINK+=\"relay_charger\", MODE=\"0666\"\n"
                        ASSIGNED_CHARGER=true
                        echo "    → /dev/relay_charger"
                        break
                        ;;
                    p|P)
                        if $ASSIGNED_PTT; then
                            echo "    PTT relay already assigned — pick another"
                            continue
                        fi
                        RULES="${RULES}SUBSYSTEM==\"tty\", KERNELS==\"${RELAY_PORTS[$i]}\", SYMLINK+=\"relay_ptt\", MODE=\"0666\"\n"
                        ASSIGNED_PTT=true
                        echo "    → /dev/relay_ptt"
                        break
                        ;;
                    s|S|"")
                        echo "    → skipped"
                        break
                        ;;
                    *)  echo "    Invalid — enter r, c, p, or s" ;;
                esac
            done
        done

        if [ -n "$RULES" ]; then
            printf "$RULES" | sudo tee /etc/udev/rules.d/99-relay-udev.rules > /dev/null
        else
            echo "  ⚠ No relays assigned — removing udev rules"
            sudo rm -f /etc/udev/rules.d/99-relay-udev.rules
        fi
    fi

    # Reload and verify
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=tty
    sleep 1
    if [ -f /etc/udev/rules.d/99-relay-udev.rules ]; then
        echo "  ✓ Relay UDEV rules installed"
        [ -L /dev/relay_radio ] && echo "    /dev/relay_radio   → $(readlink -f /dev/relay_radio)"
        [ -L /dev/relay_charger ] && echo "    /dev/relay_charger → $(readlink -f /dev/relay_charger)"
        [ -L /dev/relay_ptt ] && echo "    /dev/relay_ptt     → $(readlink -f /dev/relay_ptt)"
    fi
fi
echo

# ── 5. Audio group, realtime limits, and sudoers ─────────────────
echo "[ 5/15 ] Setting up audio permissions..."
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

# Add user to serial group (uucp on Arch, dialout on Debian — for AIOC ttyACM access)
SERIAL_GROUP=""
if getent group uucp > /dev/null 2>&1; then
    SERIAL_GROUP="uucp"
elif getent group dialout > /dev/null 2>&1; then
    SERIAL_GROUP="dialout"
fi
if [ -n "$SERIAL_GROUP" ]; then
    if id -nG "$ACTUAL_USER" 2>/dev/null | grep -qw "$SERIAL_GROUP"; then
        echo "  ✓ $ACTUAL_USER already in $SERIAL_GROUP group"
    else
        if sudo usermod -aG "$SERIAL_GROUP" "$ACTUAL_USER" 2>/dev/null; then
            echo "  ✓ Added $ACTUAL_USER to $SERIAL_GROUP group (takes effect on next login)"
        else
            echo "  ⚠ Could not add $ACTUAL_USER to $SERIAL_GROUP group — run manually: sudo usermod -aG $SERIAL_GROUP $ACTUAL_USER"
        fi
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
    NICE_BIN=$(which nice 2>/dev/null || echo /usr/bin/nice)
    printf '%s ALL=(ALL) NOPASSWD: %s snd-aloop, %s -r snd-aloop, %s\n' \
        "$ACTUAL_USER" "$MODPROBE_BIN" "$MODPROBE_BIN" "$NICE_BIN" \
        | sudo tee "$SUDOERS_FILE" > /dev/null \
        && sudo chmod 440 "$SUDOERS_FILE" \
        && echo "  ✓ Passwordless sudo configured for modprobe snd-aloop" \
        || echo "  ⚠ Could not write sudoers rule — start.sh will prompt for sudo password"
fi

set -e
echo

# ── 5b. Fix /run/user runtime directory permissions (RPi only) ───
if $IS_PI; then
    echo "       Fixing /run/user runtime directory permissions (RPi)..."
    # systemd-logind creates /run/user/1000 with 0770 on some RPi configs,
    # but XDG_RUNTIME_DIR requires 0700 — Qt/Mumble warns on every launch
    TMPFILES_RULE="/etc/tmpfiles.d/run-user-perms.conf"
    if [ ! -f "$TMPFILES_RULE" ]; then
        echo 'd /run/user/%U 0700 - - -' | sudo tee "$TMPFILES_RULE" > /dev/null
        echo "  ✓ Created $TMPFILES_RULE (fixes 0770 → 0700 on boot)"
    else
        echo "  ✓ $TMPFILES_RULE already exists"
    fi
    # Fix it right now too
    chmod 0700 /run/user/$(id -u) 2>/dev/null || true
fi
echo

# ── 6. Darkice (optional — for Broadcastify/Icecast streaming) ───
echo "[ 6/15 ] Darkice streaming (optional)..."
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

# ── 7. SDR stack (optional — for SDR receiver input via rtl_airband) ──
echo "[ 7/15 ] Installing SDR receiver stack (optional — for web-controlled SDR input)..."
set +e

SDR_INSTALLED=false

if [ "$DISTRO" = "arch" ]; then
    # SDR packages are in AUR — need an AUR helper
    AUR_USER=${SUDO_USER:-$USER}
    AUR_HELPER=""
    for helper in yay paru; do
        if sudo -u "$AUR_USER" bash -c "command -v $helper" &>/dev/null; then
            AUR_HELPER="$helper"
            break
        fi
    done

    if [ -n "$AUR_HELPER" ]; then
        # Install SDRplay API library (provides sdrplay_apiService daemon)
        if command -v sdrplay_apiService &>/dev/null || pacman -Q libsdrplay 2>/dev/null | grep -q libsdrplay; then
            echo "  ✓ SDRplay API (libsdrplay) already installed"
        else
            echo "  Installing SDRplay API (libsdrplay) from AUR..."
            if sudo -u "$AUR_USER" $AUR_HELPER -S --noconfirm libsdrplay 2>/dev/null; then
                echo "  ✓ libsdrplay installed"
            else
                echo "  ⚠ Could not install libsdrplay — install manually: $AUR_HELPER -S libsdrplay"
            fi
        fi

        # Install SoapySDR + SDRplay plugin (SoapySDR bridge for rtl_airband)
        if python3 -c "import SoapySDR" 2>/dev/null || pacman -Q soapysdr 2>/dev/null | grep -q soapysdr; then
            echo "  ✓ SoapySDR already installed"
        else
            echo "  Installing SoapySDR from AUR..."
            if sudo -u "$AUR_USER" $AUR_HELPER -S --noconfirm soapysdr 2>/dev/null; then
                echo "  ✓ SoapySDR installed"
            else
                echo "  ⚠ Could not install SoapySDR — install manually: $AUR_HELPER -S soapysdr"
            fi
        fi

        if pacman -Q soapysdrplay3-git 2>/dev/null | grep -q soapysdrplay3; then
            echo "  ✓ SoapySDRPlay3 plugin already installed"
        else
            echo "  Installing SoapySDRPlay3 plugin from AUR..."
            if sudo -u "$AUR_USER" $AUR_HELPER -S --noconfirm soapysdrplay3-git 2>/dev/null; then
                echo "  ✓ SoapySDRPlay3 installed"
            else
                echo "  ⚠ Could not install SoapySDRPlay3 — install manually: $AUR_HELPER -S soapysdrplay3-git"
            fi
        fi

        # Install rtl_airband (SDR demodulator — outputs audio to PulseAudio)
        if command -v rtl_airband &>/dev/null; then
            echo "  ✓ rtl_airband already installed"
            SDR_INSTALLED=true
        else
            echo "  Installing rtl_airband from AUR..."
            if sudo -u "$AUR_USER" $AUR_HELPER -S --noconfirm rtlsdr-airband-git 2>/dev/null; then
                echo "  ✓ rtl_airband installed"
                SDR_INSTALLED=true
            else
                echo "  ⚠ Could not install rtl_airband — install manually: $AUR_HELPER -S rtlsdr-airband-git"
            fi
        fi
    else
        echo "  ⚠ No AUR helper found (yay/paru) — cannot install SDR packages"
        echo "    Install an AUR helper, then run:"
        echo "      yay -S libsdrplay soapysdr soapysdrplay3-git rtlsdr-airband-git"
    fi
else
    # Debian/Ubuntu — SoapySDR is in apt, others built/downloaded automatically
    echo "  Installing SoapySDR from apt..."
    sudo apt-get install -y soapysdr-tools libsoapysdr-dev 2>/dev/null \
        && echo "  ✓ SoapySDR installed" \
        || echo "  ⚠ Could not install SoapySDR — install manually"

    # ── rtl_airband: build from source if not already installed ──
    if command -v rtl_airband &>/dev/null; then
        echo "  ✓ rtl_airband already installed"
        SDR_INSTALLED=true
    else
        echo "  Building rtl_airband from source..."

        # Install build dependencies
        sudo apt-get install -y build-essential cmake pkg-config \
            libmp3lame-dev libshout3-dev 'libconfig++-dev' \
            libfftw3-dev librtlsdr-dev libpulse-dev libsoapysdr-dev 2>/dev/null

        # Raspberry Pi GPU support (optional, non-fatal if missing)
        if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "armv7l" ]; then
            sudo apt-get install -y libraspberrypi-dev 2>/dev/null || true
        fi

        RTLAIRBAND_BUILD_DIR=$(mktemp -d)
        if git clone --depth 1 https://github.com/charlie-foxtrot/RTLSDR-Airband.git "$RTLAIRBAND_BUILD_DIR/RTLSDR-Airband" 2>/dev/null; then
            cd "$RTLAIRBAND_BUILD_DIR/RTLSDR-Airband"
            mkdir -p build && cd build

            # Determine cmake platform flag for Pi
            RTLAIRBAND_CMAKE_FLAGS="-DSOAPYSDR=ON -DPULSEAUDIO=ON -DNFM=ON"
            if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "armv7l" ]; then
                RTLAIRBAND_CMAKE_FLAGS="$RTLAIRBAND_CMAKE_FLAGS -DPLATFORM=rpiv2"
            fi

            if cmake $RTLAIRBAND_CMAKE_FLAGS ../ 2>/dev/null && make -j"$(nproc)" 2>/dev/null; then
                sudo make install 2>/dev/null
                if command -v rtl_airband &>/dev/null; then
                    echo "  ✓ rtl_airband built and installed"
                    SDR_INSTALLED=true
                else
                    echo "  ⚠ rtl_airband build succeeded but binary not found in PATH"
                fi
            else
                echo "  ⚠ rtl_airband build failed — install manually from:"
                echo "    https://github.com/charlie-foxtrot/RTLSDR-Airband"
            fi
            cd "$GATEWAY_DIR"
        else
            echo "  ⚠ Could not clone RTLSDR-Airband repo — check network connection"
        fi
        rm -rf "$RTLAIRBAND_BUILD_DIR"
    fi

    # ── SDRplay API: download and install if not present ──
    if command -v sdrplay_apiService &>/dev/null; then
        echo "  ✓ SDRplay API already installed"
    else
        echo "  Installing SDRplay API..."
        SDRPLAY_RUN="SDRplay_RSP_API-Linux-3.15.2.run"
        SDRPLAY_URL="https://www.sdrplay.com/software/$SDRPLAY_RUN"
        SDRPLAY_TMP=$(mktemp -d)

        if curl -fSL -o "$SDRPLAY_TMP/$SDRPLAY_RUN" "$SDRPLAY_URL" 2>/dev/null \
           || wget -q -O "$SDRPLAY_TMP/$SDRPLAY_RUN" "$SDRPLAY_URL" 2>/dev/null; then
            chmod +x "$SDRPLAY_TMP/$SDRPLAY_RUN"
            # The .run installer is interactive — feed it 'yes' + enter to accept license
            if echo -e "y\ny" | sudo "$SDRPLAY_TMP/$SDRPLAY_RUN" --noexec --target "$SDRPLAY_TMP/extracted" 2>/dev/null; then
                # Run the inner install script non-interactively
                if [ -f "$SDRPLAY_TMP/extracted/install.sh" ]; then
                    cd "$SDRPLAY_TMP/extracted"
                    echo -e "y\ny" | sudo bash install.sh 2>/dev/null
                    cd "$GATEWAY_DIR"
                fi
            else
                # Fallback: try running directly (some versions don't support --noexec)
                echo -e "y\ny" | sudo "$SDRPLAY_TMP/$SDRPLAY_RUN" 2>/dev/null || true
            fi

            if command -v sdrplay_apiService &>/dev/null; then
                echo "  ✓ SDRplay API installed"
                sudo systemctl enable sdrplay.service 2>/dev/null || true
            else
                echo "  ⚠ SDRplay API install may have failed — verify with: command -v sdrplay_apiService"
                echo "    Manual download: https://www.sdrplay.com/downloads/"
            fi
        else
            echo "  ⚠ Could not download SDRplay API — install manually from:"
            echo "    https://www.sdrplay.com/downloads/"
        fi
        rm -rf "$SDRPLAY_TMP"
    fi
fi

# Create rtl_airband config directory
sudo mkdir -p /etc/rtl_airband
echo "  ✓ /etc/rtl_airband/ directory ready"

# Enable sdrplay.service (the API daemon that rtl_airband talks to)
if systemctl list-unit-files sdrplay.service &>/dev/null 2>&1; then
    sudo systemctl enable sdrplay.service 2>/dev/null || true
    echo "  ✓ sdrplay.service enabled (starts on boot)"
else
    echo "  ⚠ sdrplay.service not found — SDRplay API not installed yet"
fi

# Deploy WirePlumber null-audio-sink config for SDR capture
# Creates sdr_capture + sdr_capture2 PipeWire sinks that the gateway reads via FFmpeg
WIREPLUMBER_SDR_SRC="$GATEWAY_DIR/scripts/90-sdr-capture-sink.conf"
WIREPLUMBER_SDR_DEST="$WIREPLUMBER_CONF_DIR/90-sdr-capture-sink.conf"
if [ -d "$WIREPLUMBER_CONF_DIR" ] || mkdir -p "$WIREPLUMBER_CONF_DIR" 2>/dev/null; then
    if [ ! -f "$WIREPLUMBER_SDR_DEST" ]; then
        if [ -f "$WIREPLUMBER_SDR_SRC" ]; then
            cp "$WIREPLUMBER_SDR_SRC" "$WIREPLUMBER_SDR_DEST" \
                && echo "  ✓ WirePlumber SDR capture sinks configured (sdr_capture + sdr_capture2)" \
                || echo "  ⚠ Could not install WirePlumber SDR sink config"
        else
            echo "  ⚠ $WIREPLUMBER_SDR_SRC not found — skipping SDR sink config"
        fi
    else
        echo "  ✓ WirePlumber SDR capture sink config already exists"
    fi
    systemctl --user restart wireplumber 2>/dev/null || true
else
    echo "  ⚠ Could not create WirePlumber config directory — SDR audio routing needs manual setup"
fi

# Sudoers: allow passwordless sudo for SDR operations
# rtl_airband config writes, process management, sdrplay service control
SUDOERS_SDR="/etc/sudoers.d/radio-gateway-sdr"
KILLALL_BIN=$(which killall 2>/dev/null || echo /usr/bin/killall)
SYSTEMCTL_SDR=$(which systemctl 2>/dev/null || echo /usr/bin/systemctl)
TEE_BIN=$(which tee 2>/dev/null || echo /usr/bin/tee)
if [ -n "$ACTUAL_USER" ]; then
    cat > /tmp/_sudoers_sdr <<SUDOERS_EOF
# Radio Gateway — SDR operations (passwordless)
$ACTUAL_USER ALL=(ALL) NOPASSWD: $TEE_BIN /etc/rtl_airband/*
$ACTUAL_USER ALL=(ALL) NOPASSWD: $KILLALL_BIN rtl_airband
$ACTUAL_USER ALL=(ALL) NOPASSWD: $KILLALL_BIN -9 rtl_airband
$ACTUAL_USER ALL=(ALL) NOPASSWD: $KILLALL_BIN -9 sdrplay_apiService
$ACTUAL_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL_SDR start sdrplay.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL_SDR stop sdrplay.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL_SDR restart sdrplay.service
SUDOERS_EOF
    if visudo -cf /tmp/_sudoers_sdr > /dev/null 2>&1; then
        sudo cp /tmp/_sudoers_sdr "$SUDOERS_SDR"
        sudo chmod 440 "$SUDOERS_SDR"
        echo "  ✓ Passwordless sudo configured for SDR operations"
    else
        echo "  ⚠ Could not validate SDR sudoers rule"
    fi
    rm -f /tmp/_sudoers_sdr
fi

set -e
echo

# ── 7b. ADS-B stack (optional — dump1090-fa + FlightRadar24 feeder) ─
echo "[ 7b ] Installing ADS-B stack (optional — dump1090-fa + fr24feed)..."
set +e

DUMP1090_PORT=30080   # Avoids conflict with gateway default port 8080

if [ "$DISTRO" = "arch" ]; then
    # ── RTL-SDR SoapySDR plugin (from official extra repo) ──
    if pacman -Q soapyrtlsdr &>/dev/null; then
        echo "  ✓ soapyrtlsdr already installed"
    else
        sudo pacman -S --noconfirm soapyrtlsdr \
            && echo "  ✓ soapyrtlsdr installed" \
            || echo "  ⚠ Could not install soapyrtlsdr"
    fi

    # ── lighttpd (serves dump1090-fa web UI) ──
    if command -v lighttpd &>/dev/null; then
        echo "  ✓ lighttpd already installed"
    else
        sudo pacman -S --noconfirm lighttpd \
            && echo "  ✓ lighttpd installed" \
            || echo "  ⚠ Could not install lighttpd"
    fi

    # ── dump1090-fa: build from source (AUR package fails on modern GCC) ──
    if command -v dump1090-fa &>/dev/null; then
        echo "  ✓ dump1090-fa already installed"
    else
        echo "  Building dump1090-fa from source (FlightAware GitHub)..."
        _D1090_BUILD=$(mktemp -d)
        if git clone --depth 1 https://github.com/flightaware/dump1090.git "$_D1090_BUILD" 2>/dev/null; then
            # Remove -Werror so it builds on newer GCC without warnings-as-errors
            sed -i 's/-Werror //' "$_D1090_BUILD/Makefile"
            sed -i 's/-Werror$//' "$_D1090_BUILD/Makefile"
            if make -C "$_D1090_BUILD" -j"$(nproc)" RTLSDR=yes 2>/dev/null; then
                sudo install -Dm755 "$_D1090_BUILD/dump1090" /usr/bin/dump1090-fa
                sudo install -Dm755 "$_D1090_BUILD/view1090" /usr/bin/view1090
                sudo mkdir -p /usr/share/dump1090-fa
                sudo cp -r "$_D1090_BUILD/public_html" /usr/share/dump1090-fa/html
                echo "  ✓ dump1090-fa built and installed"
            else
                echo "  ⚠ dump1090-fa build failed — check build deps: sudo pacman -S base-devel librtlsdr"
            fi
        else
            echo "  ⚠ Could not clone dump1090-fa from GitHub"
        fi
        rm -rf "$_D1090_BUILD"
    fi

    # ── fr24feed: AUR package 'flightradar24' (pre-built binary from FR24) ──
    # Requires --nodeps because 'dump1090' generic dep has no pacman provider
    AUR_USER=${SUDO_USER:-$USER}
    AUR_HELPER=""
    for helper in yay paru; do
        if sudo -u "$AUR_USER" bash -c "command -v $helper" &>/dev/null; then
            AUR_HELPER="$helper"
            break
        fi
    done

    if command -v fr24feed &>/dev/null || pacman -Q flightradar24 &>/dev/null; then
        echo "  ✓ fr24feed (flightradar24) already installed"
    elif [ -n "$AUR_HELPER" ]; then
        echo "  Installing fr24feed from AUR (flightradar24)..."
        _FR24_PKG=$(mktemp -d)
        if sudo -u "$AUR_USER" bash -c "cd '$_FR24_PKG' && $AUR_HELPER -G flightradar24 --getpkgbuild" 2>/dev/null \
            && sudo -u "$AUR_USER" bash -c "cd '$_FR24_PKG/flightradar24' && makepkg --noconfirm --skipchecksums" 2>/dev/null; then
            _PKG_FILE=$(ls "$_FR24_PKG"/flightradar24/flightradar24-*.pkg.tar.zst 2>/dev/null | head -1)
            if [ -n "$_PKG_FILE" ]; then
                sudo pacman -U "$_PKG_FILE" --nodeps --nodeps --noconfirm \
                    && echo "  ✓ fr24feed installed" \
                    || echo "  ⚠ Could not install fr24feed package"
            else
                echo "  ⚠ fr24feed package file not found after build"
            fi
        else
            echo "  ⚠ Could not build fr24feed from AUR"
            echo "    Manual: $AUR_HELPER -G flightradar24 && cd flightradar24 && makepkg -si --nodeps"
        fi
        rm -rf "$_FR24_PKG"
    else
        echo "  ⚠ No AUR helper found — cannot install fr24feed"
        echo "    Install yay/paru, then: yay -G flightradar24 && cd flightradar24 && makepkg -si --nodeps"
    fi

    # ── dump1090 user and rtlsdr group membership ──
    id dump1090 &>/dev/null || sudo useradd -r -s /sbin/nologin -d /var/lib/dump1090-fa dump1090
    sudo usermod -aG rtlsdr dump1090 2>/dev/null || true
    sudo mkdir -p /var/lib/dump1090-fa
    sudo chown dump1090:dump1090 /var/lib/dump1090-fa 2>/dev/null || true

    # ── lighttpd config for dump1090-fa on port 30080 ──
    sudo mkdir -p /etc/dump1090-fa
    if [ ! -f /etc/dump1090-fa/lighttpd.conf ]; then
        sudo tee /etc/dump1090-fa/lighttpd.conf > /dev/null << LIGHTTPD_EOF
server.port = $DUMP1090_PORT
server.bind = "0.0.0.0"
server.document-root = "/usr/share/dump1090-fa/html"
server.pid-file = "/run/dump1090-fa/lighttpd.pid"
server.errorlog = "/var/log/dump1090-fa-lighttpd.log"
server.modules = ( "mod_alias", "mod_setenv", "mod_staticfile", "mod_dirlisting" )
index-file.names = ( "index.html" )
alias.url = ( "/data/" => "/run/dump1090-fa/" )
\$HTTP["url"] =~ "^/data/.*\\.json\$" {
    setenv.set-response-header = ( "Access-Control-Allow-Origin" => "*" )
}
mimetype.assign = (
    ".html" => "text/html", ".js" => "application/javascript",
    ".css"  => "text/css",  ".json" => "application/json",
    ".png"  => "image/png", ".gif" => "image/gif", ".ico" => "image/x-icon"
)
LIGHTTPD_EOF
        echo "  ✓ lighttpd config written for port $DUMP1090_PORT"
    else
        echo "  ✓ lighttpd config already exists"
    fi

    # ── systemd service: dump1090-fa decoder ──
    if [ ! -f /etc/systemd/system/dump1090-fa.service ]; then
        sudo tee /etc/systemd/system/dump1090-fa.service > /dev/null << SVCEOF
[Unit]
Description=dump1090-fa ADS-B receiver
After=network.target

[Service]
User=dump1090
Group=rtlsdr
RuntimeDirectory=dump1090-fa
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/dump1090-fa --device-type rtlsdr --net --net-ro-port 30002 --net-sbs-port 30003 --net-bi-port 30004 --write-json /run/dump1090-fa --quiet
Type=simple
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
        echo "  ✓ dump1090-fa.service created"
    fi

    # ── systemd service: dump1090-fa web (lighttpd) ──
    if [ ! -f /etc/systemd/system/dump1090-fa-web.service ]; then
        sudo tee /etc/systemd/system/dump1090-fa-web.service > /dev/null << WEBSVCEOF
[Unit]
Description=dump1090-fa web interface (lighttpd on port $DUMP1090_PORT)
After=dump1090-fa.service
Requires=dump1090-fa.service

[Service]
Type=forking
PIDFile=/run/dump1090-fa/lighttpd.pid
ExecStart=/usr/bin/lighttpd -f /etc/dump1090-fa/lighttpd.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
WEBSVCEOF
        echo "  ✓ dump1090-fa-web.service created"
    fi

else
    # Debian/Ubuntu/Raspberry Pi — use FlightAware apt repo for dump1090-fa
    # and FlightRadar24 apt repo for fr24feed

    # dump1090-fa via FlightAware piaware apt repository
    if command -v dump1090-fa &>/dev/null; then
        echo "  ✓ dump1090-fa already installed"
    else
        echo "  Adding FlightAware apt repository and installing dump1090-fa..."
        FA_DEB_URL="https://flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware-support"
        FA_DEB="piaware-support_10.0_all.deb"
        if curl -fsSL "${FA_DEB_URL}/${FA_DEB}" -o "/tmp/${FA_DEB}" 2>/dev/null; then
            if sudo dpkg -i "/tmp/${FA_DEB}" 2>/dev/null && sudo apt-get install -y dump1090-fa 2>/dev/null; then
                echo "  ✓ dump1090-fa installed"
            else
                echo "  ⚠ Could not install dump1090-fa via FlightAware repo"
                echo "    Manual install: https://flightaware.com/adsb/piaware/install"
            fi
            rm -f "/tmp/${FA_DEB}"
        else
            echo "  ⚠ Could not download FlightAware apt package"
            echo "    Manual install: https://flightaware.com/adsb/piaware/install"
        fi
    fi

    # Configure dump1090-fa HTTP port to avoid conflict with gateway on 8080
    DUMP1090_DEFAULT="/etc/default/dump1090-fa"
    if [ -f "$DUMP1090_DEFAULT" ]; then
        if grep -q "net-http-port" "$DUMP1090_DEFAULT"; then
            sudo sed -i "s/--net-http-port [0-9]*/--net-http-port $DUMP1090_PORT/" "$DUMP1090_DEFAULT" \
                && echo "  ✓ dump1090-fa HTTP port updated to $DUMP1090_PORT" \
                || echo "  ⚠ Could not update HTTP port in $DUMP1090_DEFAULT"
        else
            # Append http port to NET_OPTIONS line, or add it
            if grep -q "NET_OPTIONS" "$DUMP1090_DEFAULT"; then
                sudo sed -i "s/NET_OPTIONS=\"\(.*\)\"/NET_OPTIONS=\"\1 --net-http-port $DUMP1090_PORT\"/" "$DUMP1090_DEFAULT" \
                    && echo "  ✓ dump1090-fa HTTP port set to $DUMP1090_PORT" \
                    || echo "  ⚠ Could not set HTTP port in $DUMP1090_DEFAULT — add --net-http-port $DUMP1090_PORT to NET_OPTIONS manually"
            else
                echo "NET_OPTIONS=\"--net --net-http-port $DUMP1090_PORT --net-ro-port 30002 --net-sbs-port 30003 --net-bi-port 30004,30104\"" \
                    | sudo tee -a "$DUMP1090_DEFAULT" > /dev/null \
                    && echo "  ✓ dump1090-fa HTTP port set to $DUMP1090_PORT" \
                    || echo "  ⚠ Could not write to $DUMP1090_DEFAULT"
            fi
        fi
    else
        echo "  ⚠ $DUMP1090_DEFAULT not found — configure dump1090-fa HTTP port to $DUMP1090_PORT manually"
    fi

    # fr24feed via FlightRadar24 apt repository
    if command -v fr24feed &>/dev/null; then
        echo "  ✓ fr24feed already installed"
    else
        echo "  Adding FlightRadar24 apt repository and installing fr24feed..."
        curl -s https://repo-feed.flightradar24.com/flightradar24.key \
            | sudo gpg --dearmor -o /usr/share/keyrings/flightradar24.gpg 2>/dev/null
        echo "deb [signed-by=/usr/share/keyrings/flightradar24.gpg] https://repo-feed.flightradar24.com rpi-stable main" \
            | sudo tee /etc/apt/sources.list.d/fr24feed.list > /dev/null
        if sudo apt-get update -qq 2>/dev/null && sudo apt-get install -y fr24feed 2>/dev/null; then
            echo "  ✓ fr24feed installed"
        else
            echo "  ⚠ Could not install fr24feed from apt"
            echo "    Manual install: https://www.flightradar24.com/share-your-data"
        fi
    fi
fi

# Enable and start dump1090-fa service
if systemctl list-unit-files dump1090-fa.service &>/dev/null 2>&1; then
    sudo systemctl enable dump1090-fa.service 2>/dev/null \
        && echo "  ✓ dump1090-fa.service enabled (starts on boot)" \
        || echo "  ⚠ Could not enable dump1090-fa.service"
    sudo systemctl restart dump1090-fa.service 2>/dev/null \
        && echo "  ✓ dump1090-fa started" \
        || echo "  ⚠ Could not start dump1090-fa.service — check: journalctl -u dump1090-fa -n 20"
else
    echo "  ⚠ dump1090-fa.service not found — install dump1090-fa first"
fi

# fr24feed requires interactive account signup — print instructions
echo ""
echo "  ── FlightRadar24 signup (run once to activate feeding) ──────────────"
echo "  fr24feed is installed but needs your FR24 account credentials."
echo "  Run this command and follow the prompts:"
echo "    sudo fr24feed --signup"
echo "  Then enable the service:"
echo "    sudo systemctl enable --now fr24feed"
echo "  ─────────────────────────────────────────────────────────────────────"
echo ""
echo "  To enable ADS-B in the gateway set in gateway_config.txt:"
echo "    ENABLE_ADSB = true"
echo "    ADSB_PORT   = $DUMP1090_PORT"

set -e
echo

# ── 8. Mumble GUI client ─────────────────────────────────────
echo "[ 9/15 ] Installing Mumble client..."
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
echo "[ 10/15 ] Installing Mumble server (optional — for local server instances)..."
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
echo "[ 11/15 ] Configuring OpenSSL for TLS 1.0 compatibility..."
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
echo "[ 12/15 ] Setting up configuration..."

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
echo "[ 13/15 ] Setting permissions..."
chmod +x "$GATEWAY_DIR/radio_gateway.py" 2>/dev/null || true
chmod +x "$GATEWAY_DIR/scripts/"*.sh 2>/dev/null || true
chmod +x "$GATEWAY_DIR/scripts/install.sh" 2>/dev/null || true
echo "  ✓ Scripts are executable"
echo

# ── 12. Systemd service ─────────────────────────────────────
echo "[ 14/15 ] Installing systemd service..."
ACTUAL_USER=${SUDO_USER:-$USER}
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")
ACTUAL_UID=$(id -u "$ACTUAL_USER")
SYSTEMCTL_BIN=$(command -v systemctl 2>/dev/null || echo /usr/bin/systemctl)

SERVICE_DEST="/etc/systemd/system/radio-gateway.service"
SERVICE_TEMPLATE="$SCRIPT_DIR/radio-gateway.service.template"
if [ -f "$SERVICE_TEMPLATE" ]; then
    sed -e "s|__USER__|$ACTUAL_USER|g" \
        -e "s|__GATEWAY_DIR__|$GATEWAY_DIR|g" \
        -e "s|__HOME__|$ACTUAL_HOME|g" \
        -e "s|__UID__|$ACTUAL_UID|g" \
        "$SERVICE_TEMPLATE" | sudo tee "$SERVICE_DEST" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable radio-gateway.service 2>/dev/null || true
    echo "  ✓ radio-gateway.service installed and enabled"

# Install Telegram bot service (not enabled — requires config first)
TG_SERVICE_SRC="$GATEWAY_DIR/tools/telegram-bot.service"
TG_SERVICE_DEST="/etc/systemd/system/telegram-bot.service"
if [ -f "$TG_SERVICE_SRC" ]; then
    sudo cp "$TG_SERVICE_SRC" "$TG_SERVICE_DEST"
    sudo systemctl daemon-reload
    echo "  ✓ telegram-bot.service installed (not enabled — configure first)"
fi
    echo "    Start:   sudo systemctl start radio-gateway"
    echo "    Stop:    sudo systemctl stop radio-gateway"
    echo "    Restart: sudo systemctl restart radio-gateway"
    echo "    Logs:    journalctl -u radio-gateway -f"
else
    echo "  ⚠ Service template not found ($SERVICE_TEMPLATE) — skipping"
fi

# Passwordless sudo for systemctl start/stop/restart (needed by desktop shortcuts)
SUDOERS_GW="/etc/sudoers.d/radio-gateway"
printf '%s ALL=(ALL) NOPASSWD: %s start radio-gateway.service\n' \
    "$ACTUAL_USER" "$SYSTEMCTL_BIN" > /tmp/_sudoers_gw
printf '%s ALL=(ALL) NOPASSWD: %s stop radio-gateway.service\n' \
    "$ACTUAL_USER" "$SYSTEMCTL_BIN" >> /tmp/_sudoers_gw
printf '%s ALL=(ALL) NOPASSWD: %s restart radio-gateway.service\n' \
    "$ACTUAL_USER" "$SYSTEMCTL_BIN" >> /tmp/_sudoers_gw
REBOOT_BIN="$(command -v reboot 2>/dev/null || echo /sbin/reboot)"
printf '%s ALL=(ALL) NOPASSWD: %s\n' \
    "$ACTUAL_USER" "$REBOOT_BIN" >> /tmp/_sudoers_gw
if visudo -cf /tmp/_sudoers_gw > /dev/null 2>&1; then
    sudo cp /tmp/_sudoers_gw "$SUDOERS_GW"
    sudo chmod 440 "$SUDOERS_GW"
    echo "  ✓ Passwordless sudo configured for gateway service commands"
else
    echo "  ⚠ Could not validate sudoers rule — desktop shortcuts will prompt for password"
fi
rm -f /tmp/_sudoers_gw
echo

# ── 13. Desktop shortcuts ──────────────────────────────────
echo "[ 15/15 ] Creating desktop shortcuts..."
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ] || mkdir -p "$DESKTOP_DIR" 2>/dev/null; then
    # Remove old manual-launch shortcut (replaced by systemd service shortcuts)
    rm -f "$DESKTOP_DIR/radio-gateway.desktop" 2>/dev/null

    SHORTCUTS_OK=0
    for tmpl in gateway-start gateway-stop gateway-restart; do
        SRC="$SCRIPT_DIR/${tmpl}.desktop.template"
        DEST="$DESKTOP_DIR/${tmpl}.desktop"
        if [ -f "$SRC" ]; then
            cp "$SRC" "$DEST"
            chmod +x "$DEST"
            SHORTCUTS_OK=$((SHORTCUTS_OK + 1))
        fi
    done

    if [ $SHORTCUTS_OK -eq 3 ]; then
        echo "  ✓ Desktop shortcuts created:"
        echo "    Gateway Start   — start the service"
        echo "    Gateway Stop    — stop the service"
        echo "    Gateway Restart — restart the service"
    else
        echo "  ⚠ Some shortcut templates missing ($SHORTCUTS_OK/3 created)"
    fi
else
    echo "  ⚠ Desktop directory not found (skipping shortcuts)"
fi
echo

# ── Start the gateway service ─────────────────────────────────
echo "Starting radio-gateway service..."
if sudo systemctl start radio-gateway.service 2>/dev/null; then
    sleep 2
    if systemctl is-active --quiet radio-gateway.service; then
        echo "  ✓ radio-gateway is running"
    else
        echo "  ⚠ radio-gateway started but may have exited — check: journalctl -u radio-gateway -n 50"
    fi
else
    echo "  ⚠ Could not start radio-gateway — check: journalctl -u radio-gateway -n 50"
fi
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
echo "  3. Connect your AIOC USB device and/or KV4P HT"
echo "     (unplug and replug after install so udev rules take effect)"
echo "     KV4P HT will appear as /dev/kv4p (set KV4P_PORT = /dev/kv4p)"
echo
echo "  4. Log out and back in so audio group membership takes effect"
echo "     (needed for darkice realtime scheduling without sudo)"
echo
echo "  5. Start the gateway (use desktop shortcuts or systemctl):"
echo "       sudo systemctl start radio-gateway"
echo "     Or use the Gateway Start shortcut on your desktop"
echo
echo "SDR RECEIVER (optional — RSPduo / RTL-SDR via rtl_airband):"
echo "  The installer configured rtl_airband, SoapySDR, and PipeWire SDR sinks."
echo "  Set ENABLE_SDR = true and SDR_DEVICE_TYPE = sdrplay in gateway_config.txt"
echo "  Use the SDR Control page (http://<gateway-ip>:8080/sdr) to tune and manage"
echo "  Audio chain: RSPduo → SoapySDR → rtl_airband → PulseAudio → sdr_capture sink → gateway"
echo "  Verify SDR sinks: pw-cli list-objects | grep sdr_capture"
echo
echo "ADS-B AIRCRAFT TRACKING (optional — RTL-SDR + dump1090-fa + FlightRadar24):"
echo "  dump1090-fa is installed and configured on port $DUMP1090_PORT"
echo "  Enable in gateway_config.txt:"
echo "    ENABLE_ADSB = true"
echo "    ADSB_PORT   = $DUMP1090_PORT"
echo "  ADS-B map will appear as an 'ADS-B' tab in the gateway web UI"
echo "  To feed FlightRadar24, run: sudo fr24feed --signup"
echo "  then: sudo systemctl enable --now fr24feed"
echo "  Status: sudo systemctl status dump1090-fa fr24feed"
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
echo "WEB CONFIGURATION UI & LIVE DASHBOARD (optional):"
echo "  Set ENABLE_WEB_CONFIG = true in gateway_config.txt"
echo "  Config editor: http://<gateway-ip>:8080/"
echo "  Live dashboard: http://<gateway-ip>:8080/dashboard"
echo "  Radio control:  http://<gateway-ip>:8080/radio"
echo "  SDR control:    http://<gateway-ip>:8080/sdr"
echo "  Log viewer:     http://<gateway-ip>:8080/logs"
echo "  Set WEB_CONFIG_PASSWORD for basic auth (user: admin)"
echo "  Firewall: sudo ufw allow 8080/tcp"
echo
echo "TELEGRAM BOT — PHONE CONTROL (optional):"
echo "  Control the gateway from your phone in plain English via a Claude Code session."
echo "  Voice notes sent to the bot are transmitted over the radio automatically."
echo "  1. Create a bot via @BotFather on Telegram — copy the token"
echo "  2. Send a message to your bot to get your chat_id:"
echo "       curl 'https://api.telegram.org/bot<TOKEN>/getUpdates'"
echo "  3. Edit gateway_config.txt:"
echo "       ENABLE_TELEGRAM      = true"
echo "       TELEGRAM_BOT_TOKEN   = <token>"
echo "       TELEGRAM_CHAT_ID     = <chat_id>"
echo "  4. Start Claude Code in a named tmux session:"
echo "       tmux new-session -s claude-gateway"
echo "       cd $GATEWAY_DIR && claude --dangerously-skip-permissions"
echo "       (detach: Ctrl+B then D)"
echo "  5. Enable and start the bot service:"
echo "       sudo systemctl enable --now telegram-bot"
echo "  See README.md — Telegram Bot section for full details."
echo
echo "DYNAMIC DNS (optional):"
echo "  Set ENABLE_DDNS = true in gateway_config.txt"
echo "  Configure DDNS_USERNAME, DDNS_PASSWORD, DDNS_HOSTNAME"
echo "  Updates on startup and then every DDNS_UPDATE_INTERVAL seconds"
echo
echo "DOCS:"
echo "  README.md                       — full documentation"
echo "  docs/TTS_TEXT_COMMANDS_GUIDE.md — Mumble text commands"
echo
