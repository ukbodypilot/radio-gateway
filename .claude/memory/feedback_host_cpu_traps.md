---
name: Host CPU traps under chrome-remote-desktop
description: Non-obvious things that drive 4x load on the gateway host when the user is connected via CRD — close these if found running
type: feedback
originSessionId: 7ea91610-ff2b-43d6-9bc6-0b176f2d8891
---
The user runs the gateway host desktop via chrome-remote-desktop (CRD). CRD re-encodes the framebuffer (VP8/VP9) whenever pixels change, so any **continuously-animating window** on the remote desktop multiplies CPU cost across: Xorg → compositor → CRD encoder → wire.

## The xfce4-taskmanager trap (measured 2026-04-20)

Leaving `xfce4-taskmanager` open on the remote desktop drove load from **0.94 → 4.74** on 4 cores. Its live CPU-usage graphs repaint at ~1 Hz, which forces CRD to encode a new frame every second, which pulls radio_gateway + sdrplay off-CPU during scheduling. Closing the one window dropped load back to 1.20 — the biggest single optimisation of the session.

**Why:** It's not the taskmanager's own CPU that hurts (1.8%). It's the cascade it induces in the display pipeline and the scheduler contention with realtime audio work.

**How to apply:** If future session notices sustained load >2 with no obvious gateway-side cause, check `DISPLAY=:0 wmctrl -l` for animated apps on the host desktop. Common culprits: system monitors, media players with visualisers, clocks with seconds, Discord/Slack with animated avatars, any webpage with auto-refresh or live charts. Close them rather than trying to tune them.

Don't run live web dashboards (including the gateway's own `/routing` or `/monitor` pages) *inside* the remote Chrome — port-forward + view from the local machine instead. Every pixel change is CRD encoder work.

## Compositor off is the baseline

`xfwm4 /general/use_compositing = false` is set (see reference_host_tweaks.md). Don't turn it back on for "eye candy" — it roughly doubles Xorg CPU during CRD sessions.
