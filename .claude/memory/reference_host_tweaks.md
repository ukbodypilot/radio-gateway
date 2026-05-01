---
name: Host-side service tweaks (not in repo)
description: Systemd unit overrides, masked units, and desktop settings applied to the optiplex3020 gateway host — none are tracked in the radio-gateway git repo, so grep won't find them
type: reference
originSessionId: 7ea91610-ff2b-43d6-9bc6-0b176f2d8891
---
# Host-side tweaks applied 2026-04-20

These live in `/etc/systemd/system/` and `xfconf`, not in the radio-gateway repo. If a future session notices the effect (clean journal, low idle load, fast th9800-cat stops) and goes looking for the cause in the repo, they won't find it here. Check `systemctl cat <unit>` and `xfconf-query` instead.

## Power management (2026-04-29)

- **`/etc/systemd/system/radio-gateway-powersave.service`** — sets CPU governor to `schedutil` on all 4 cores at boot + USB autosuspend for GPS dongle (1-10) and unused RTL2838 dongle (1-9). Enabled in multi-user.target. `power-profiles-daemon` is set to `balanced` but does NOT apply the governor in intel_pstate passive/intel_cpufreq mode — this service fills the gap. AIOC, KV4P, SDRplay, relay FTDI, and CAT serial stay forced `on` (active radio devices — autosuspend would risk audio glitches). PCIe ASPM is firmware-locked (Optiplex 3020 BIOS takes control) — cannot override at runtime or via kernel param without risking instability.

## Systemd overrides

- **`/etc/systemd/system/mumble-server-gw1.service.d/override.conf`** — `SuccessExitStatus=15 SIGTERM`. Silences the "Failed with result 'exit-code'" journal line that appeared on every gateway restart (Murmur exits 15 on SIGTERM, which is clean — systemd just didn't know).
- **`/etc/systemd/system/chrome-remote-desktop@user.service.d/override.conf`** — `Nice=10 CPUWeight=30 IOWeight=50`. Deprioritises CRD's VP8 encoder so radio_gateway/sdrplay win contention.

## Masked units

- `archlinux-keyring-wkd-sync.timer` and `.service` — symlinked to `/dev/null`. Saves ~1.5 min on first boot after an Arch mirror cycle; we don't need WKD sync for routine use.

## Desktop / XFCE

- `xfwm4 /general/use_compositing = false` — huge win under CRD. Compositing forces an extra redraw path per window change; with CRD re-encoding VP8 every framebuffer damage, compositor was doubling Xorg CPU.

## User shell

- `~/.bashrc` aliases `claude` → `nice -n 10 claude` so future Claude Code sessions can't starve the audio pipeline during heavy agent work.

## What NOT to change

- `th9800-cat.service` unit is stock. The 10s→23ms stop-time fix was in the **program** (`TH9800_CAT.py` single-loop refactor, commit 216aa57 in that repo), not the unit file. Don't add `TimeoutStopSec=3` overrides "to be safe" — the code already exits cleanly.
- `sdrplay.service` is stock. The SEGV mitigation is in `sdr_plugin.py` (2s libusb grace, commit 068fd6a in radio-gateway).
