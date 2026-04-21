# TB-8506F Root & GSI Installation Plan
*Lenovo Tab M8 3rd Gen — MT8768T (Helio P22T)*
*Authored: 2026-04-21*

---

## Device Summary

| Property | Value |
|---|---|
| Model | Lenovo TB-8506F (Tab M8 Gen 3) |
| SoC | MediaTek MT8768T (Helio P22T) |
| MTK HW code | `0x766` (same silicon family as MT6765) |
| Stock OS | Android 10 (maxes out, no OTA beyond this) |
| Treble | Yes (Project Treble mandatory on Android 10+) |
| Partition scheme | A-only (system-as-system) |
| Bootloader unlock | No official method — requires BROM exploit |
| Flash method | SP Flash Tool (backup/restore) + fastboot (GSI flash) |

---

## Tools Required

Install all of these on your Linux machine before starting.

```bash
# mtkclient — BROM exploit and bootloader unlock
pip install mtkclient --break-system-packages
# or from source (preferred, stays current):
git clone https://github.com/bkerler/mtkclient
cd mtkclient
pip install -r requirements.txt --break-system-packages

# udev rules (required for Linux USB access to MTK BROM mode)
sudo cp mtkclient/Setup/Linux/*.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# ADB + fastboot (platform-tools)
# Already present on Arch — verify:
adb version && fastboot version

# SP Flash Tool (for stock ROM backup/restore and recovery)
# Download Linux version from: https://spflashtool.com/
# Extract, mark as executable, run as root if needed
```

---

## Phase 0 — Pre-Flight (Do This First, No Exceptions)

### 0.1 Enable USB Debugging on the tablet

Settings → About tablet → tap Build Number 7 times → back to Settings → Developer Options → enable USB Debugging and OEM Unlocking (if visible).

### 0.2 Verify ADB connection

```bash
adb devices
# Should show: <serial>  device
```

### 0.3 Extract and save the preloader binary

This is critical. The mtkclient BROM exploit on MT8768T **requires the device's own preloader** to set up DRAM correctly. Without it, the DA handshake will fail at the EMI data send step.

```bash
# Pull preloader while the device is still stock and booting normally
adb shell su -c "dd if=/dev/block/by-name/preloader of=/sdcard/preloader_ot8.bin"
adb pull /sdcard/preloader_ot8.bin ./preloader_ot8.bin
# Verify it's non-zero
ls -lh preloader_ot8.bin
```

If you don't have root yet (stock device), get the preloader from the stock ROM package instead (see 0.4).

### 0.4 Download stock ROM — keep it forever

The stock ROM is your recovery lifeline. Download the latest ROW build:

- Source: https://www.needrom.com/download/lenovo-smart-tab-m8-tb-8506f/
- Build: `TB-8506F_S000042_240309_ROW` (latest as of plan date)
- Extract it — inside you'll find `preloader_ot8.bin` (or similar), `boot.img`, `system.img`, scatter file, etc.
- **Back this up to a safe location. Do not lose it.**

The scatter file (`MT6768_Android_scatter.txt` or similar) is required for SP Flash Tool operations.

### 0.5 Full partition dump via mtkclient (optional but strongly recommended)

Once mtkclient is set up, you can take a full NAND dump before touching anything:

```bash
# Boot into BROM mode first (see Phase 1), then:
python mtk rf full_backup.bin --preloader preloader_ot8.bin
# This dumps the entire eMMC — slow but complete insurance
```

---

## Phase 1 — Bootloader Unlock via BROM Exploit

### 1.1 How BROM mode works on this device

BROM (Boot ROM) is MediaTek's lowest-level boot mode, below the preloader. It's baked into the SoC and cannot be patched by OTA updates. mtkclient exploits a vulnerability in the BROM USB protocol (kamakiri exploit for MT6765/MT8768 family) to gain arbitrary read/write access to the device, including modifying the `seccfg` partition which controls bootloader lock state.

### 1.2 Enter BROM mode

1. Power off the tablet completely
2. Start the mtkclient command on your Linux machine (it waits for USB connection)
3. Hold **Volume Down** (or try both Volume Up + Volume Down simultaneously if Vol Down alone fails)
4. While holding the button(s), connect the USB cable to the tablet
5. Hold until mtkclient detects the device (you'll see output in the terminal)
6. Release the buttons

The device will not show anything on screen in BROM mode — that's normal.

### 1.3 Run the unlock

```bash
cd mtkclient  # or wherever you cloned/installed it

# Start the unlock command FIRST, then enter BROM mode per 1.2
python mtk da seccfg unlock --preloader ../preloader_ot8.bin
```

Expected successful output flow:
```
Preloader - CPU: MT6765/MT8768t(Helio P35/G35)
Preloader - HW code: 0x766
Preloader - BROM mode detected.
PLTools - Loading payload from mt6765_payload.bin
Exploitation - Kamakiri Run
Exploitation - Done sending payload...
DAXFlash - Successfully uploaded stage 1
DAXFlash - DRAM setup passed.
DAXFlash - Uploading stage 2...
[seccfg unlock operations...]
Done.
```

### 1.4 Reboot and verify

```bash
python mtk reset
# Tablet reboots
# On first boot after unlock, tablet may show a warning screen — this is normal
# Boot may be slower than usual the first time
```

To verify unlock worked:
```bash
adb reboot bootloader
fastboot getvar unlocked
# Should return: unlocked: yes
```

### 1.5 Troubleshooting BROM

| Symptom | Fix |
|---|---|
| "Waiting for PreLoader VCOM" never resolves | Try different USB cable, USB 2.0 port specifically, different button combo |
| "DA handshake failed" | You need `--preloader preloader_ot8.bin` — don't skip it |
| "Device is protected" + auth error | Kamakiri payload should bypass this — if it doesn't, try `--ptype=carbonara` |
| "EMI data send failed" | Preloader mismatch — get preloader from the actual device (adb method in 0.3) |
| Connect/disconnect loop on Linux | Known issue — try `echo 0 > /sys/bus/usb/devices/usb1/authorized` then re-authorize, or use a powered hub |

---

## Phase 2 — Prepare Boot Image (Magisk Root)

Root is required for storage bug patching (Phase 4) and general usefulness.

### 2.1 Extract boot.img from stock ROM

The stock ROM package from Phase 0 contains `boot.img`. Extract it:

```bash
# From the downloaded stock ROM zip/tar
unzip TB-8506F_S000042_240309_ROW.zip -d stock_rom/
ls stock_rom/  # find boot.img
```

### 2.2 Patch boot.img with Magisk

1. Copy `boot.img` to the tablet's internal storage: `adb push stock_rom/boot.img /sdcard/boot.img`
2. Install Magisk APK on the tablet: download from https://github.com/topjohnwu/Magisk/releases
3. Open Magisk → Install → Select and Patch a File → choose `/sdcard/boot.img`
4. Magisk creates `magisk_patched_XXXXX.img` in `/sdcard/Download/`
5. Pull it back: `adb pull /sdcard/Download/magisk_patched_*.img ./magisk_patched_boot.img`

### 2.3 Prepare disabled vbmeta

AVB (Android Verified Boot) must be disabled or every GSI will bootloop. Use the vbmeta from the stock ROM package, or generate a blank one:

```bash
# Option A: use vbmeta.img from stock ROM (preferred)
ls stock_rom/vbmeta.img  # should exist

# Option B: generate empty vbmeta
python3 -c "
import struct
# Minimal valid vbmeta with verification disabled
# Just use the stock one if available
"
# Actually — just download a pre-made vbmeta_disabled.img from a trusted source
# or extract from stock and use --disable-verity --disable-verification flags
```

---

## Phase 3 — Flash GSI

### 3.1 GSI selection

The TB-8506F is **A-only** (not A/B), ARM64. You need the `arm64_bvN` or `arm64_bvS` variant of any GSI.

Recommended GSIs in order of reported stability on this device:

1. **Evolution X** (Android 13/14) — best reported results on TB-8506F Gen 3
2. **Project Elixir** — boots cleanly, good setup experience
3. **phh AOSP / Andy Yan LineageOS GSI** — lightweight, good baseline for patching

Download sources:
- Evolution X GSI: https://sourceforge.net/projects/evolution-x/files/GSI/
- Andy Yan LineageOS GSI: https://sourceforge.net/projects/andyyan-gsi/files/
- phh AOSP: https://github.com/phhusson/treble_experimentations/releases

Pick the `arm64_bvN` (no GApps, A-only) or `arm64_bvS` (with PHH su) variant.

### 3.2 Flash sequence

```bash
# Reboot to fastboot/bootloader
adb reboot bootloader
# Verify device shows
fastboot devices

# Step 1: Flash Magisk-patched boot
fastboot flash boot magisk_patched_boot.img

# Step 2: Disable AVB verification
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img

# Step 3: Wipe and flash system partition with GSI
fastboot erase system
fastboot flash system EvolutionX_GSI_arm64_bvN.img
# Note: this may take 5-15 minutes for a ~1.5GB image

# Step 4: Wipe userdata (required after GSI flash)
fastboot -w

# Step 5: Reboot
fastboot reboot
```

**First boot takes 3-8 minutes.** Do not power off.

### 3.3 Skip setup screen if stuck

Some GSIs stall at the setup wizard. Fix via ADB once the device is partially booted:

```bash
adb shell settings put secure user_setup_complete 1
adb shell settings put global device_provisioned 1
adb shell am start -n com.android.launcher3/.Launcher  # or equivalent
```

---

## Phase 4 — Storage Bug Diagnosis and Patching

The Gen 3 TB-8506F has a known issue on GSIs where apps cannot install/download due to storage appearing full or unavailable. This phase is the patching workflow.

### 4.1 Diagnose first

```bash
# Check what's actually mounted where
adb shell df -h
adb shell mount | grep -E "data|sdcard|fuse|storage"
adb shell cat /proc/mounts

# Check logcat for the actual error
adb logcat | grep -iE "storage|sdcard|fuse|f2fs|ext4|vold|mount" 2>/dev/null | head -50
```

### 4.2 Fix A — FUSE vs sdcardfs mismatch (most common)

Newer AOSP GSIs default to FUSE for emulated storage. MT8768T vendor blobs expect sdcardfs. Fix:

```bash
# Test fix without committing (resetprop is live, doesn't survive reboot)
adb shell su -c "resetprop ro.sys.sdcardfs true"
adb shell su -c "resetprop persist.sys.sdcardfs always"
# Then restart vold and see if storage comes back
adb shell su -c "stop vold && start vold"

# If that fixes it, make permanent via Magisk module:
# Create module that adds to /system/build.prop:
# ro.sys.sdcardfs=true
# persist.sys.sdcardfs=always
```

### 4.3 Fix B — fstab wrong mount options

```bash
# Pull current fstab from vendor
adb shell su -c "cat /vendor/etc/fstab.mt6768" > fstab.mt6768
# or
adb shell su -c "find /vendor/etc -name 'fstab*' -exec cat {} \;"

# Look for /data entry — check for f2fs vs ext4, check flags
# Common fix: change f2fs to ext4 if kernel doesn't have f2fs support in GSI
# Or add: ,context=u:object_r:system_data_file:s0 to userdata mount flags

# Edit locally, push back (requires root + remount)
adb push fstab.mt6768.patched /sdcard/
adb shell su -c "mount -o remount,rw /vendor && cp /sdcard/fstab.mt6768.patched /vendor/etc/fstab.mt6768"
```

### 4.4 Fix C — Reformat userdata with correct filesystem

If the GSI kernel doesn't support the filesystem /data was originally formatted as:

```bash
# From fastboot (no root needed)
fastboot format:ext4 userdata
# or if the GSI kernel has f2fs:
fastboot format:f2fs userdata
fastboot reboot
```

### 4.5 Fix D — SELinux context on /data is wrong

```bash
adb shell su -c "restorecon -RF /data"
adb shell su -c "restorecon -RF /data/media"
```

### 4.6 Fix E — Magisk module approach (cleanest, fully reversible)

If direct patching is messy, build a Magisk module that overlays the corrected files at boot:

```bash
mkdir -p magisk-storage-fix/system/build.prop.d/
mkdir -p magisk-storage-fix/META-INF/com/google/android/

# module.prop
cat > magisk-storage-fix/module.prop << 'EOF'
id=storage-fix-tb8506f
name=Storage Fix TB-8506F
version=v1
versionCode=1
author=claude-code
description=Fixes emulated storage on MT8768T GSI
EOF

# system.prop overlay
cat > magisk-storage-fix/system.prop << 'EOF'
ro.sys.sdcardfs=true
persist.sys.sdcardfs=always
EOF

# Package as zip
cd magisk-storage-fix && zip -r ../storage-fix.zip . && cd ..
adb push storage-fix.zip /sdcard/
# Install via Magisk app → Modules → Install from storage
```

### 4.7 If the bug is kernel-level

If none of the above work and logcat shows the kernel itself missing the storage driver, the path forward is:

1. Try a different GSI built with an MTK-aware kernel (check phhusson's variants with MTK patches)
2. Or build a custom kernel — grab Lenovo's GPL kernel source from their developer portal, apply the GSI's kernel config, compile with the MTK storage/FUSE patches

---

## Phase 5 — Post-Install Tuning

### WiFi / Bluetooth
Usually works via vendor blobs. If not:
```bash
adb shell su -c "setprop persist.vendor.bt.bdaddr_path /data/misc/bluetooth/bdaddr"
```

### MTK VoLTE (if you need it — probably irrelevant for tablet)
PHH GSIs have a settings panel: Settings → PHH Treble Settings → IMS features → Force 4G calling

### Performance
```bash
# Disable SELinux if causing issues (not recommended long-term)
adb shell su -c "setenforce 0"

# Check governor
adb shell su -c "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
```

### GApps (if using vanilla GSI)
Flash MindTheGapps or NikGapps ARM64 Android 13 via Magisk → Modules, or via TWRP if installed.

---

## Recovery Plan — If Things Go Wrong

### Scenario: Soft brick (bootloop, black screen)

SP Flash Tool can restore the stock ROM completely:

```bash
# Linux — run as root
./SPFlashTool  # open the app
# Load the scatter file from the stock ROM package
# Select "Download" mode
# Select all partitions
# Power off tablet, connect via USB
# Tool auto-detects and flashes
```

### Scenario: mtkclient bricked something

```bash
# Re-enter BROM mode and reflash preloader first
python mtk w preloader preloader_ot8.bin
python mtk reset
```

### Scenario: Bootloader re-lock needed

```bash
python mtk da seccfg lock --preloader preloader_ot8.bin
```

---

## Claude Code Integration

This entire process can be driven from a Claude Code tmux session with the tablet connected via USB. Claude Code handles:

- All `adb`, `fastboot`, `python mtk` commands autonomously
- Logcat parsing and diagnosis
- File patching (fstab, build.prop, Magisk modules)
- Build environment setup if kernel compilation becomes needed

The physical steps requiring human hands:
1. Holding the button combo to enter BROM mode (Phase 1.2)
2. Confirming any on-screen prompts on the tablet during first boot

Everything else is commandline-automatable.

---

## Checklist

- [ ] USB debugging enabled on tablet
- [ ] `adb devices` shows device
- [ ] `preloader_ot8.bin` extracted and saved
- [ ] Stock ROM downloaded and backed up
- [ ] mtkclient installed with udev rules
- [ ] Full NAND backup taken (optional but recommended)
- [ ] Bootloader unlocked (`fastboot getvar unlocked` → yes)
- [ ] Magisk-patched `boot.img` created
- [ ] GSI downloaded (Evolution X ARM64 bvN recommended)
- [ ] `vbmeta.img` ready
- [ ] GSI flashed successfully
- [ ] First boot completes
- [ ] Storage bug assessed and patched if needed
- [ ] Root verified in Magisk app
- [ ] Stock ROM SP Flash Tool restore tested (on a spare if possible)

---

## Key References

- mtkclient repo: https://github.com/bkerler/mtkclient
- TB-8506F stock firmware: https://www.needrom.com/download/lenovo-smart-tab-m8-tb-8506f/
- XDA Tab M8 thread: https://xdaforums.com/t/lenovo-tab-m8.4049539/
- Andy Yan GSI builds: https://sourceforge.net/projects/andyyan-gsi/files/
- phh treble GSIs: https://github.com/phhusson/treble_experimentations/releases
- Magisk: https://github.com/topjohnwu/Magisk/releases

---
*Plan authored via research session 2026-04-21. MT8768T BROM exploit confirmed working on same silicon (kamakiri / mt6765_payload.bin). GSI compatibility confirmed by XDA Gen 3 users. Storage bug documented and patch paths identified.*
