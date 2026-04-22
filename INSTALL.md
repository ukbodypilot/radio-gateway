# Installing radio-gateway

Step-by-step walkthrough for a fresh install. The top-level `README.md`
has a terse Quick Start; this document covers everything the installer
can't automate (credential collection, troubleshooting, post-install
verification).

> **TL;DR** — Clone anywhere, run `scripts/install.sh`, edit
> `gateway_config.txt` with your Mumble details, start the service,
> check `http://<host>:8080/dashboard`. The rest of this file
> explains the parts that commonly trip people up.

---

## 1. Prerequisites

### Operating system

Tested on Arch Linux and Debian 12 (Raspberry Pi OS, Ubuntu 22.04+).
Anything with `pacman` or `apt-get` will work; the installer auto-detects
which distro you're on.

### User account

- A regular user account with `sudo` access. The installer needs sudo for
  package installation, UDEV rules, and systemd unit installation, but
  the gateway itself runs as your normal user.
- Don't clone as root. Run `scripts/install.sh` as your user; it will
  escalate via `sudo` where needed.

### AUR helper (Arch only)

Several optional components live in the AUR:

- `cloudflared-bin` — for exposing the web UI over HTTPS via Cloudflare tunnel
- `darkice` — for Broadcastify/Icecast streaming
- `libsdrplay` / `soapysdrplay3-git` / `rtlsdr-airband-git` — only if you're using an SDRplay RSPduo

Install `yay` (or `paru`) **before** running `scripts/install.sh`:

```bash
sudo pacman -S --needed base-devel git
git clone https://aur.archlinux.org/yay.git /tmp/yay
(cd /tmp/yay && makepkg -si)
```

The installer will prompt if no AUR helper is found and let you continue
without it (skipping those optional features).

### Hardware

None strictly required at install time, but plug in whatever you plan to
use before running the installer so UDEV rules can detect it:

| Device                    | Need it if you want to…                              |
|---------------------------|------------------------------------------------------|
| AIOC USB interface        | Key / monitor any analog radio (TH-9800, FT-2900, …) |
| KV4P HT                   | Use a KV4P HT radio                                  |
| SDRplay RSPduo / RTL-SDR  | Passive SDR monitoring, scanner channels             |
| CH340-based USB relay     | Automated radio power on/off                         |
| u-blox GNSS / USB GPS     | Location stamping, repeater proximity, APRS         |

---

## 2. Clone and run the installer

```bash
git clone https://github.com/ukbodypilot/radio-gateway.git
cd radio-gateway
bash scripts/install.sh
```

The installer is idempotent — you can re-run it any time. Re-running is
also how you apply updates to systemd units, UDEV rules, or the
post-install health checks.

### What the installer does

The script is broken into 15 numbered phases. The main ones:

1. System packages (`pacman`/`apt`): Python, PortAudio, HIDAPI, FFmpeg, Opus, alsa-utils, tmux, avahi.
2. ALSA loopback module (`snd-aloop`): persistent load + validation.
3. Python packages (see `requirements.txt`): numpy, scipy, pyaudio, pymumble, moonshine-onnx (transcription), silero-vad, pyrnnoise (denoise), mcp, etc.
4. KV4P-HT Python driver cloned + installed editable at `~/kv4p-ht-python`.
5. UDEV rules for KV4P, AIOC, optionally CH340 USB relay modules.
6. User added to `audio` + serial groups; realtime limits + passwordless sudo for `modprobe`.
7. Optional: Darkice, WirePlumber loopback disable, SDRplay stack, Mumble server.
8. `gateway_config.txt` created from `examples/gateway_config.txt` if missing.
9. Systemd unit installed: `/etc/systemd/system/radio-gateway.service` with substituted paths.
10. Post-install health check.

### After the installer finishes

- Read the **Post-install health check** output carefully — it flags
  missing modules, missing binaries, and config placeholders still in
  place.
- **Log out and log back in** (or run `newgrp audio`) so the new group
  memberships take effect. Services started before this will fail with
  permission errors when opening audio devices.

---

## 3. Obtain credentials

The default `gateway_config.txt` ships with placeholders. You'll need
real values for anything you want to actually use.

### Mumble server (required)

You need a Mumble server to connect to. Options:

- **Self-host** — `sudo pacman -S mumble-server` (Arch) or
  `sudo apt install mumble-server` (Debian). Default port 64738. Edit
  `/etc/mumble-server.ini`, set a superuser password, start with
  `sudo systemctl enable --now mumble-server`.
- **Use a public server** — list at https://www.mumble.info/servers/ or
  join any existing Mumble community.
- **Set up via gateway** — `ENABLE_MUMBLE_SERVER_1 = true` in config;
  the gateway will create and supervise a local instance via systemd.

Fill in `[mumble]` section with server, port, username (display name),
and password.

### Broadcastify streaming (optional)

If you want the gateway to re-broadcast audio to https://broadcastify.com:

1. Sign up for an account, apply for a feed, get approved.
2. Broadcastify gives you a stream key (looks like `/3pc56gsd8v4n`) and password.
3. In `gateway_config.txt`:
   ```ini
   STREAM_MOUNT = /your-stream-key
   STREAM_PASSWORD = your-password
   STREAM_SERVER = audio9.broadcastify.com   # or whichever server they assigned
   ```

### Telegram bot (optional — phone control)

Lets you control the gateway from your phone via a Claude Code
session.

1. On Telegram, message `@BotFather`, run `/newbot`, follow prompts.
2. BotFather gives you a token like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`.
3. Message your new bot from your personal account. Then run:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Find your chat_id in the JSON response.
4. Edit `gateway_config.txt`:
   ```ini
   ENABLE_TELEGRAM     = true
   TELEGRAM_BOT_TOKEN  = <token>
   TELEGRAM_CHAT_ID    = <chat_id>
   ```

See `README.md → Telegram Bot` for the full Claude-Code-in-tmux setup.

### Google Drive (optional — cloud backup + tunnel URL publishing)

Handled entirely in the web UI at `/gdrive`. Click through the OAuth
flow; credentials are stored in `~/.config/radio-gateway/`.

### Dynamic DNS (optional)

If you're hosting the web UI from a home connection, configure DDNS in
`[ddns]`. The gateway updates on startup and at the interval you
specify.

---

## 4. Pre-flight checklist

Before first start, verify:

- [ ] `gateway_config.txt` exists and Mumble fields are filled in.
- [ ] `snd-aloop` is loaded: `lsmod | grep snd_aloop`.
- [ ] Your user is in the `audio` group: `groups`.
- [ ] USB devices you'll use are plugged in and visible:
    - `ls -la /dev/kv4p` (KV4P)
    - `ls -la /dev/ttyACM*` (AIOC, GPS)
    - `ls -la /dev/relay_*` (CH340 relay modules)
- [ ] You can reach your Mumble server: `nc -zv <MUMBLE_SERVER> 64738`.

---

## 5. First start

Two ways:

```bash
# Foreground (see console output, Ctrl-C to stop)
python3 radio_gateway.py

# Or via systemd (preferred — auto-restarts, logs to journald)
sudo systemctl enable --now radio-gateway
sudo journalctl -u radio-gateway -f
```

Once running, open the web UI: `http://<your-host>:8080/dashboard`

### Expected startup sequence

The console prints a numbered sequence. A healthy start looks roughly:

```
✓ Config loaded
✓ Audio setup complete
✓ Connected to Mumble
✓ Broadcastify: direct Icecast stream to audio9.broadcastify.com
✓ Connecting TH-9800 serial
✓ CAT: Setup complete
✓ Loop Recorder initialized
```

If one of these fails, the corresponding feature is disabled but the
gateway continues running. Check the log for the specific error.

---

## 6. Runtime state

On first start the gateway creates `~/.config/radio-gateway/` and
populates it as you use features. Summary:

| File                                           | Purpose                                                                | Safe to delete?                                       |
|------------------------------------------------|------------------------------------------------------------------------|-------------------------------------------------------|
| `~/.config/radio-gateway/link_endpoints.json`  | Remote Link endpoint registry                                          | Yes — endpoints must re-register (auto on next connect) |
| `~/.config/radio-gateway/source_gains.json`    | Per-source gain slider positions                                       | Yes — resets to 1.0                                   |
| `~/.config/radio-gateway/gdrive_credentials.json` | Google Drive OAuth token                                            | Yes — user re-auths in `/gdrive`                      |
| `~/.config/radio-gateway/repeaters/*.json`     | Cached RepeaterBook listings keyed by grid square                      | Yes — re-downloaded on next lookup                    |

Project-local runtime files (all inside the repo directory):

| File                                | Purpose                                                                    |
|-------------------------------------|----------------------------------------------------------------------------|
| `gateway_config.txt`                | Your edited config (NEVER committed — in `.gitignore`)                     |
| `routing_config.json`               | Bus/connection topology (edited via `/routing` page)                       |
| `sdr_channels.json`                 | SDR channel definitions (edited via `/sdr` page)                           |
| `.transcribe_settings.json`         | Moonshine + VAD thresholds (edited via `/transcribe` page)                 |
| `recordings/`                       | Loop recorder segments + on-demand exports                                 |
| `logs/`                             | Rolling console logs                                                       |

All of the above are gitignored and will be recreated at runtime if missing.

---

## 7. Troubleshooting

### `modprobe snd-aloop` fails

Fresh kernel without the ALSA loopback module. Reboot after installing
kernel-headers, then re-run the installer. On Raspberry Pi the module
should be available by default via `raspberrypi-kernel-headers`.

### Gateway starts but audio is silent

- Check `pactl list short sources` / `sinks` — the loopback devices
  must be visible.
- Group membership must have taken effect: log out, log in, `groups`
  should list `audio`.
- Confirm `speaker_enabled = false` isn't the cause if you expected
  local monitoring.

### "No AUR helper found" mid-install

You got past the initial prompt without installing `yay`/`paru`.
Re-run the installer after installing one; the affected features will
install on the second pass.

### USB device not detected

- Unplug, replug, wait 2 seconds.
- `dmesg | tail -20` — should show the USB enumeration event.
- `lsusb` — device should appear.
- If a stable `/dev/kv4p` or `/dev/relay_*` symlink is missing, the
  UDEV rule didn't match. Re-run the installer with the device plugged
  in.

### Mumble keeps disconnecting

- Server certificate expired (common on old Mumble servers): the
  installer patches `/etc/ssl/openssl.cnf` to allow SHA-1 certificates.
  If it didn't, re-run the installer and look for the "OpenSSL TLS 1.0
  patch" step.
- Python 3.12+: `pymumble` needs an SSL compatibility fix which the
  installer applies automatically.

### Systemd service fails on boot but works manually

Audio group hadn't taken effect when systemd started the service.
Either:
- `sudo systemctl restart radio-gateway` after logging out/in once, or
- Reboot.

### I want a path other than `~/Downloads/radio-gateway`

Fine. The installer derives its target paths from the clone location;
systemd templates are substituted at install time. Clone wherever you
like and run `scripts/install.sh`.

Older versions of `CLAUDE.md` and comments may still reference
`/home/user/Downloads/radio-gateway` — those are historical; the code
itself is path-agnostic.

---

## 8. Uninstalling

No automated uninstall yet. Manual:

```bash
# Stop + disable services
sudo systemctl disable --now radio-gateway telegram-bot claude-gateway
sudo rm /etc/systemd/system/radio-gateway.service \
         /etc/systemd/system/telegram-bot.service \
         /etc/systemd/system/claude-gateway.service

# Remove UDEV rules
sudo rm -f /etc/udev/rules.d/99-{kv4p,aioc,ch340-relay}.rules
sudo udevadm control --reload-rules

# Remove the gateway directory
rm -rf ~/Downloads/radio-gateway   # or wherever you cloned

# Keep ~/.config/radio-gateway/ if you want to preserve state
```

The `snd-aloop` module auto-load in `/etc/modules-load.d/snd-aloop.conf`
can be removed if nothing else on the system needs it.

---

Questions, bugs, or a fresh-install step that confused you — open an
issue at https://github.com/ukbodypilot/radio-gateway/issues.
