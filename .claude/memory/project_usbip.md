---
name: USBIP USB over TCP feature
description: Planned feature to add USB/IP device sharing to the gateway so remote USB devices (BT dongle, RTL-SDR on a Pi) appear locally
type: project
---

Add `USBIPManager` class to share USB devices from a remote machine (e.g. Pi) to the gateway machine over TCP.

**Why:** User wants to access USB devices (Bluetooth dongle, RTL-SDR) physically connected to a remote Raspberry Pi from the gateway machine, without physical access.

**How to apply:** Model after `RTLAirbandManager`. Server side runs `usbipd` and binds devices; client side runs `usbip attach`. Once attached, device appears as normal local USB — no other gateway code needs to change.

**Plan:**
- `USBIPManager` class: manages `usbipd` (server) or `usbip attach/detach` (client) via subprocess
- Config: `ENABLE_USBIP`, `USBIP_MODE` (server/client), `USBIP_SERVER_IP`, `USBIP_DEVICES` list (bus_id + label)
- Web UI panel: show available remote devices, attach/detach buttons, connection status, polled status endpoint `/usbipstatus`
- Installer: `pacman -S usbip` / `apt install usbip`; load kernel modules `usbip_core`, `usbip_host` (server), `vhci_hcd` (client)
- Key packages researched: PythonUSBIP (Frazew/PythonUSBIP), pyusbip (jwise/pyusbip) — may not be needed, subprocess wrapper around `usbip` CLI is sufficient
- TCP port 3240 (standard USBIP port)
