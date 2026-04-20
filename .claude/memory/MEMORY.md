# Machine: user-optiplex3020 (Arch Linux)

## User preferences
- sudo password: Platoon69!

## RDP Setup (shared local desktop via x11vnc)
- xrdp + xorgxrdp + x11vnc installed
- xrdp and xrdp-sesman enabled at boot
- x11vnc systemd service at /etc/systemd/system/x11vnc.service - enabled at boot
  - Runs: x11vnc -display :0 -forever -loop -noxdamage -repeat -rfbport 5900 -shared -nopw -localhost
  - After=display-manager.service, WantedBy=graphical.target
- /etc/xrdp/xrdp.ini has [local-desktop] section: "Local Desktop (shared)" using libvnc.so on 127.0.0.1:5900
- autorun is empty (session picker shown) - user selects "Local Desktop (shared)"
- Desktop environment: XFCE4 (started via ~/.xinitrc with dbus-launch)
- Display manager: lightdm with autologin
- Physical display disconnected — headless setup
- /etc/X11/xorg.conf.d/10-headless.conf forces HDMI-1 active at 1920x1080 (prevents laggy RDP/VNC when no monitor connected)

## Git config
- user.name: ukbodypilot
- user.email: robin.pengelly@gmail.com

## SSH hardening (2026-03-17)
- SSH was compromised via password brute-force — cryptominer deployed
- PasswordAuthentication set to `no` in /etc/ssh/sshd_config
- PermitRootLogin set to `no`
- authorized_keys cleared (attacker had added a key)
- No SSH keys currently set up for user — key-based auth needed if remote SSH access is wanted

## Chrome Remote Desktop
- Package: chrome-remote-desktop-existing-session (AUR) — patched to use existing X session via ~/.config/chrome-remote-desktop/Xsession (contains "0" for display :0)
- SYSTEM service chrome-remote-desktop@user.service enabled at boot (the actual daemon)
- USER-level service chrome-remote-desktop.service is DISABLED (it was racing with the system service at boot — both would see each other's process and exit with "already running")
- Override at /etc/systemd/system/chrome-remote-desktop@user.service.d/override.conf
  - Adds: After=graphical.target lightdm.service (prevents race with display manager on boot)
  - Sets: XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS for user 1000
  - Adds: Restart=on-failure RestartSec=5
- Start manually if needed: sudo systemctl start chrome-remote-desktop@user
- Root cause of repeated boot failures: both system and user-level services were enabled, racing at boot and detecting each other as "already running"

## Claude Plan
- User upgraded to Max plan (2026-03-23)

## Voice Relay (added 2026-03-29)
- Voice-to-tmux page hosted on gateway web server at /voice (port 8080, accessible via Cloudflare)
- Routes: /voice, /voice/status, /voice/view, /voice/send, /voice/session
- tmux session: claude-voice (independent of gateway's claude-gateway session for Telegram)
- Session buttons: Start tmux, New Claude, Stop (stop = Ctrl+C + clear, keeps shell alive)
- Claude launches with `--dangerously-skip-permissions` in /home/user, auto-confirms trust prompt
- Standalone Flask server (~/voice-relay/) disabled via `systemctl --user disable voice-relay`
- XFCE autostart entry at ~/.config/autostart/claude-voice-terminal.desktop opens terminal on login
- Email links in gateway_core.py updated to point to /voice on gateway port (not old :5123)

## Radio Gateway v2.0 (shipped 2026-03-31)
- Branch v2.0-mixer merged to main, tagged v2.0.0
- Bus-based audio routing with visual Drawflow UI
- 4 radio plugins: SDR, TH9800, D75, KV4P
- 119 commits, 28 bugs fixed, 7200+ lines dead code removed
- Full duplex remote audio, direct Icecast, Mumble routing, 44+ MCP tools
- `json` imported as `json_mod` in gateway_core.py — critical gotcha

## Radio Gateway v3.3 (shipped 2026-04-19) — [project_denoise.md](project_denoise.md)
- Two selectable neural denoise engines (RNNoise / DeepFilterNet 3), per-bus pill.
- Off-tick per-bus denoise worker (D13) — bus tick `_apply_dfn` cost 24 ms → 0.01 ms.
- Phase-aligned wet/dry mix with measured delays (RNN 960, DFN3 1440 samples).
- Moonshine repetition-suppressed decoder kills "Anno, Anno…" loops.
- DFN3 model vendored at `tools/models/dfn3/denoiser_model.onnx` (16 MB, SHA-pinned).

## Roadmap
1. Clean up start.sh (absorb into Python)
2. Installer / deployment
3. More plugins

## Gotchas
- pam_faillock is active (default: 3 failures = 10min lockout). Reset with: faillock --user user --reset
- When piping sudo password, avoid heredocs that can feed extra lines as password attempts
- x11vnc override at /etc/systemd/system/x11vnc.service.d/override.conf must include `-repeat` flag or key repeat breaks over RDP
- Use `echo 'password' | sudo -S python3 -c "..."` to write files as root — avoids heredoc stdin leakage into tee
- NEVER restart radio-gateway service — user does it themselves
- web_server.py do_GET has `import os` at line 720 inside an elif — any new code using `os` in do_GET must ensure `import os` runs first (it's now at the top of do_GET)
- [Silero VAD + audio boost must soft-clip](feedback_silero_boost.md) — hard-clip on boosted audio breaks Silero v5 detection; always use tanh pre-VAD
- [Always instrument new code before debugging it](feedback_instrumentation.md) — add counters/telemetry in the first commit, not as a follow-up; avoids hours-long guessing sessions
- [Vendor model binaries in-repo, don't runtime-download](feedback_model_vendoring.md) — 16 MB blobs in git are fine; runtime downloads block workers and confuse users
