#!/bin/bash
# fix_config.sh — Restore all user-approved boolean settings in gateway_config.txt
# Run this after any config damage to restore production defaults.
# These values were approved by the user on 2026-03-26.

CONFIG="${1:-gateway_config.txt}"

if [ ! -f "$CONFIG" ]; then
    echo "Config file not found: $CONFIG"
    exit 1
fi

echo "Restoring production defaults in $CONFIG..."

# Core features
sed -i \
  -e 's/^HEADLESS_MODE = false/HEADLESS_MODE = true/' \
  -e 's/^ENABLE_WEB_CONFIG = false/ENABLE_WEB_CONFIG = true/' \
  -e 's/^ENABLE_WEB_MIC = false/ENABLE_WEB_MIC = true/' \
  -e 's/^ENABLE_WEB_MONITOR = false/ENABLE_WEB_MONITOR = true/' \
  -e 's/^ENABLE_CLOUDFLARE_TUNNEL = false/ENABLE_CLOUDFLARE_TUNNEL = true/' \
  -e 's/^ENABLE_D75 = false/ENABLE_D75 = true/' \
  -e 's/^ENABLE_KV4P = false/ENABLE_KV4P = true/' \
  -e 's/^ENABLE_TH9800 = false/ENABLE_TH9800 = true/' \
  -e 's/^ENABLE_SDR = false/ENABLE_SDR = true/' \
  -e 's/^ENABLE_SDR2 = false/ENABLE_SDR2 = true/' \
  -e 's/^ENABLE_VAD = false/ENABLE_VAD = true/' \
  -e 's/^ENABLE_TTS = false/ENABLE_TTS = true/' \
  -e 's/^ENABLE_PLAYBACK = false/ENABLE_PLAYBACK = true/' \
  -e 's/^ENABLE_SOUNDBOARD = false/ENABLE_SOUNDBOARD = true/' \
  -e 's/^ENABLE_SMART_ANNOUNCE = false/ENABLE_SMART_ANNOUNCE = true/' \
  -e 's/^ENABLE_STREAM_OUTPUT = false/ENABLE_STREAM_OUTPUT = true/' \
  -e 's/^ENABLE_RELAY_RADIO = false/ENABLE_RELAY_RADIO = true/' \
  -e 's/^ENABLE_ADSB = false/ENABLE_ADSB = true/' \
  -e 's/^ENABLE_ANNOUNCE_INPUT = false/ENABLE_ANNOUNCE_INPUT = true/' \
  -e 's/^ENABLE_DDNS = false/ENABLE_DDNS = true/' \
  -e 's/^ENABLE_USBIP = false/ENABLE_USBIP = true/' \
  -e 's/^ENABLE_SPEAKER_OUTPUT = false/ENABLE_SPEAKER_OUTPUT = true/' \
  -e 's/^ENABLE_TEXT_COMMANDS = false/ENABLE_TEXT_COMMANDS = true/' \
  -e 's/^ENABLE_EMAIL = false/ENABLE_EMAIL = true/' \
  -e 's/^EMAIL_ON_STARTUP = false/EMAIL_ON_STARTUP = true/' \
  -e 's/^ENABLE_GATEWAY_LINK = false/ENABLE_GATEWAY_LINK = true/' \
  -e 's/^ENABLE_TELEGRAM = false/ENABLE_TELEGRAM = true/' \
  "$CONFIG"

# Mumble
sed -i \
  -e 's/^MUMBLE_RECONNECT = false/MUMBLE_RECONNECT = true/' \
  -e 's/^MUMBLE_VBR = false/MUMBLE_VBR = true/' \
  -e 's/^ENABLE_MUMBLE_SERVER_1 = false/ENABLE_MUMBLE_SERVER_1 = true/' \
  -e 's/^MUMBLE_SERVER_1_AUTOSTART = false/MUMBLE_SERVER_1_AUTOSTART = true/' \
  "$CONFIG"

# Audio processing (HPF on for all sources)
sed -i \
  -e 's/^ENABLE_HIGHPASS_FILTER = false/ENABLE_HIGHPASS_FILTER = true/' \
  -e 's/^SDR_PROC_ENABLE_HPF = false/SDR_PROC_ENABLE_HPF = true/' \
  -e 's/^D75_PROC_ENABLE_HPF = false/D75_PROC_ENABLE_HPF = true/' \
  -e 's/^KV4P_PROC_ENABLE_HPF = false/KV4P_PROC_ENABLE_HPF = true/' \
  "$CONFIG"

# KV4P
sed -i \
  -e 's/^KV4P_HIGH_POWER = false/KV4P_HIGH_POWER = true/' \
  -e 's/^KV4P_SMETER = false/KV4P_SMETER = true/' \
  "$CONFIG"

# SDR
sed -i \
  -e 's/^SDR_INTERNAL_AUTOSTART = false/SDR_INTERNAL_AUTOSTART = true/' \
  -e 's/^SDR_DUCK = false/SDR_DUCK = true/' \
  -e 's/^SDR2_DUCK = false/SDR2_DUCK = true/' \
  "$CONFIG"

# Other
sed -i \
  -e 's/^SPEAKER_START_MUTED = false/SPEAKER_START_MUTED = true/' \
  -e 's/^CAT_STARTUP_COMMANDS = false/CAT_STARTUP_COMMANDS = true/' \
  "$CONFIG"

echo "Done. Verify:"
echo "  true:  $(grep -c '= true' "$CONFIG")"
echo "  false: $(grep -c '= false' "$CONFIG")"
