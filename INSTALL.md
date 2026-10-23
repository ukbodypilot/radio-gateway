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
- `darkice` — only for the legacy external Broadcastify encoder. The default
  stream path is an internal ffmpeg pipe and does not need it.
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

### Companion repos

A few features live in sibling repos with their own installers:

- **TH9800_CAT** (https://github.com/ukbodypilot/TH9800_CAT) —
  standalone CAT control daemon for the TYT TH-9800. Required if you
  set `ENABLE_TH9800 = true` in `gateway_config.txt`. Clone and run
  its own install script; it installs its own systemd unit
  (`th9800-cat.service`), which the gateway (re)starts in its startup
  pre-flight and then reaches on TCP `CAT_PORT` (9800).
- **kv4p-ht-python** (https://github.com/ukbodypilot/kv4p-ht-python) —
  KV4P HT driver. Cloned automatically by this repo's installer into
  `~/kv4p-ht-python`.
- **D75-CAT-Control** (https://github.com/ukbodypilot/D75-CAT-Control)
  — Kenwood D75 Bluetooth CAT. Standalone; the gateway's D75 plugin
  talks to the radio directly over RFCOMM so you only need this repo
  if you want the GUI tool.

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

The script is broken into 16 numbered phases (it prints `[ n/16 ]` as it goes):

1. System packages (`pacman`/`apt`): Python, PortAudio, HIDAPI, libsndfile, FFmpeg, Opus, alsa-utils, git, Avahi; plus `cloudflared` if available.
2. ALSA loopback module (`snd-aloop`): persistent load + validation.
3. Python packages (see `requirements.txt`): numpy, scipy, pyaudio, pymumble, useful-moonshine-onnx + faster-whisper (transcription engines), silero-vad, pyrnnoise (denoise), `mcp<2`, etc. Then the KV4P-HT Python driver, cloned and installed editable at `~/kv4p-ht-python`, and the Kokoro TTS model files. The transcription pool can route between local and remote workers — see [docs/transcription-pool.md](docs/transcription-pool.md) for multi-machine deployment.
4. UDEV rules for AIOC and KV4P, and optionally CH340 USB relay modules.
5. User added to `audio` + serial groups; realtime limits; an old `modprobe`/`nice` sudoers rule from earlier versions is removed. On a Raspberry Pi, a `/run/user` permissions fix.
6. Optional, legacy: DarkIce. Not used by the default stream path.
7. Optional: SDR stack (SDRplay API, SoapySDR, `rtl_airband`).
8. Optional: ADS-B stack (dump1090-fa + FlightRadar24 feeder).
9. Mumble GUI client.
10. Mumble server (murmurd), if you want a local one.
11. OpenSSL TLS compatibility for older Mumble servers.
12. `gateway_config.txt` created from `examples/gateway_config.txt` if missing.
13. Scripts made executable.
14. Systemd unit installed: `/etc/systemd/system/radio-gateway.service` with substituted paths.
15. Desktop shortcuts (start / stop / restart).
16. **Observability stack**: native Prometheus + Grafana via `pacman` (Arch) or apt (Debian/Ubuntu, Grafana repo). Prometheus scrapes `localhost:8080/metrics` every 15s; Grafana auto-provisions the **Radio Gateway** dashboard (bus levels, PTT, link endpoints, stream throughput, transcription stats). Defaults: Prometheus at `:9090`, Grafana at `:3000` with `admin / radio`. Change the password from the Grafana UI before exposing beyond LAN.

It finishes with a post-install health check.

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

### Email alerts (recommended)

Stream outages, Fleet Manager reports and auto-fix notices, and transcription
keyword hits are sent by email. Nothing else notifies you, so set this up.

1. Use a Gmail account and turn on 2-Step Verification.
2. Create an **App password** (Google Account → Security → App passwords). It is
   16 characters and is not your normal password.
3. Edit `gateway_config.txt`:
   ```ini
   ENABLE_EMAIL        = true
   EMAIL_ADDRESS       = you@gmail.com
   EMAIL_APP_PASSWORD  = <16-char app password>
   EMAIL_RECIPIENT     =            # blank = send to yourself
   ```

Without these the gateway logs each alert as `NOT SENT (email not configured)`,
and the installer's health check warns about it.

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

First a four-step pre-flight: `[1/4]` stale gateway processes (only real
`radio_gateway.py` processes are killed, not editors or greps that mention
the name), `[2/4]` TH-9800 CAT control, `[3/4]` CPU governor set to
`schedutil`, `[4/4]` AIOC USB reset. Then the hardware-library check: if
hidapi, pyaudio or pymumble is missing the gateway stops here and prints the
command that installs it, for example:

```
ERROR: hidapi library not found!
Install it with: pip3 install hidapi --break-system-packages
```

After that the console prints the main sequence. A healthy start looks roughly:

```
✓ Configuration loaded from 'gateway_config.txt'
✓ Audio setup complete
✓ Connected to Mumble
✓ Broadcastify: direct Icecast stream to audio9.broadcastify.com
✓ Connecting TH-9800 serial
✓ CAT: Setup complete
✓ Loop Recorder initialized
```

If one of these fails, the corresponding feature is disabled but the
gateway continues running. Check the log for the specific error.

A config value the loader can't read (a word where a number belongs, say)
does not stop the load. It prints a warning with the line number and keeps
that key's default:

```
WARNING: gateway_config.txt:212: GPS_BAUD = 'fast' is not a valid int; keeping 9600
```

Errors the gateway catches and carries on from are logged as
`[swallowed] file.py:LINE in function: ...`, once per place per 5 minutes.
One or two after a USB unplug are normal; a steady stream from one place is a
bug worth reporting.

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
| `.transcribe_settings.json`         | Engine + VAD + pool routing settings (edited via `/transcribe` page)       |
| `recordings/`                       | Loop recorder segments + on-demand exports                                 |
| `logs/`                             | Rolling console logs                                                       |

All of the above are gitignored and will be recreated at runtime if missing.

---

## 6a. Updating an existing install

```bash
cd /path/to/radio-gateway
git pull
sudo systemctl restart radio-gateway
sudo journalctl -u radio-gateway -n 100 --no-pager
```

Read the release's **Upgrading** notes in [CHANGELOG.md](CHANGELOG.md) before
restarting. For 5.0.0 they are:

- Look for `WARNING: gateway_config.txt:<line>:` lines in the startup log
  (see above). Before 5.0.0 one bad value silently stopped every key after it
  from loading, so a config that "worked" may have been running on defaults.
- Keys placed inside a `[plugin.instance]` section such as `[kv4p.vhf]` now
  belong to that instance only. If you had put a global key there, move it.
- New keys, both on the config form: `PTT_TIMEOUT_SECONDS` (default 180,
  `0` = off) and `LOOP_RECORD_KBPS` (default 48).
- The Telegram bot is removed: `sudo systemctl disable --now telegram-bot
  claude-gateway`, delete both unit files from `/etc/systemd/system/`,
  `sudo systemctl daemon-reload`, delete the `[telegram]` block from
  `gateway_config.txt` and revoke the bot with `@BotFather`. Make sure email
  alerts are set up (section 3).
- `scripts/remote_bt_proxy.py` is gone; `tools/remote_bt_proxy.py` is the one copy.

Re-run `scripts/install.sh` only when a release says so; it is idempotent.

## 6b. Pinned packages in a virtualenv (optional)

By default the service runs `/usr/bin/python3` with packages installed
system-wide and unpinned, so an OS update or another `pip install` can change
them. `scripts/venv.sh` moves the service onto its own virtualenv built from
exact versions:

```bash
scripts/venv.sh freeze   # once, on a working gateway: writes requirements.lock
scripts/venv.sh build    # ~/.radio-gateway-venv from the lock (--no-deps)
scripts/venv.sh check    # libraries, versions and the test suite, in the venv
scripts/venv.sh use      # systemd drop-in; takes effect on the next restart
sudo systemctl restart radio-gateway
```

`scripts/venv.sh unuse` removes the drop-in and the next restart is back on
the system Python. The venv sees system site-packages (SoapySDR and other
apt modules), but its locked copies win. The MCP server started from
`.mcp.json` still runs the system `python3`.

`scripts/venv.sh freeze` calls `tools/freeze_lock.py`, which pins the
versions installed on *this* machine. Run it on the gateway itself, not on a
development box, or the lock pins the wrong versions.

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

### `ERROR: ... library not found!` and the service stops

The startup check (`hw_deps.py`) found hidapi, pyaudio or pymumble missing.
Run the install command it printed, as the user the service runs as, then
restart. If you use the pinned virtualenv (6b), install into the venv or
rebuild it with `scripts/venv.sh build`.

### HTTPS mode doesn't take effect

`WEB_CONFIG_HTTPS` takes the words `false`, `self-signed` or `letsencrypt`
(`letsencrypt` uses `DDNS_HOSTNAME`). Before 5.0.0 the two HTTPS modes were
silently read as off; if you set one long ago and forgot, it now loads, and
the web UI moves to `https://`.

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
sudo systemctl disable --now radio-gateway
sudo rm /etc/systemd/system/radio-gateway.service

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
