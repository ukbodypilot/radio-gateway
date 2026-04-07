---
name: FTM-150 control head reverse engineering
description: Attempted RE of faceplate-body serial protocol, shelved — proprietary PWM-modulated bus
type: project
---

## FTM-150 Control Head Bus RE — Shelved (2026-04-05)

Attempted to reverse engineer the Yaesu FTM-150 faceplate-to-body serial protocol for CAT-style control. Used 8ch 24MHz logic analyzer (Saleae clone, FX2LP) + OWON VDS1022 USB scope.

**Outcome:** Protocol is proprietary PWM-modulated on a ~50kHz carrier. Not standard UART/SPI/I2C. Would need analog demodulator (envelope detector/LPF) before digital capture is viable. Project shelved.

**Key findings:**
- Two active data lines, no separate clock
- ~50kHz carrier signal (~5V peak, 2us high / 18us low)
- Data in carrier envelope: ~1ms bursts, ~4ms gaps, 5ms frame period (~200 fps)
- Pulse width varies (0.6-1.0ms) suggesting PWM encoding
- ~4000-8000 bits/sec estimated — control/display bus only, not audio
- UART decode at 500kbaud produced structured-looking frames but these are carrier demod artifacts

**Files in repo:**
- `ftm150-re/` — capture scripts, sigrok .sr captures, decode tools, README
- `docs/ftm150-reverse-engineering.md` — full plan and reference

**Tools installed on gateway machine:**
- sigrok-cli, PulseView, sigrok-firmware-fx2lafw (Arch: `pacman -S`)
- owon-vds-tiny 1.1.5 at `/usr/bin/owon-vds-tiny`
- USB serial driver blacklisted for OWON: `/etc/modprobe.d/owon-blacklist.conf`

**Why:** Goal was CAT control like TH-9800. Protocol complexity makes it impractical without hardware demod stage.
**How to apply:** Don't attempt further without building an analog envelope detector circuit first. For CAT control, use radios with documented serial protocols.
