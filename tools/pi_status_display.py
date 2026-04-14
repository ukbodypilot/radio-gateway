#!/usr/bin/env python3
"""Pi endpoint status display for Waveshare 1.44" ST7735S (128x128 SPI)

Shows system status on multiple pages, with button/joystick controls.

Pages: Status (main), Network, D75 Radio, Audio Levels

Buttons:
  KEY1 (GPIO21): Restart endpoint service
  KEY2 (GPIO20): Toggle BT discoverable
  KEY3 (GPIO16): Toggle backlight
  Joystick UP/DOWN (GPIO6/19): Cycle pages
  Joystick PRESS (GPIO13): Force refresh
"""

import time
import os
import socket
import subprocess
import threading

import st7735 as ST7735
import gpiod
from PIL import Image, ImageDraw, ImageFont

# ── Display setup ────────────────────────────────────────────────────────
DISPLAY = ST7735.ST7735(
    port=0, cs=0,
    dc=25, rst=27, backlight=18,
    width=128, height=128,
    rotation=90,
    offset_left=2, offset_top=3,
    invert=False,
    spi_speed_hz=8000000,
)
DISPLAY.begin()

WIDTH = 128
HEIGHT = 128
FONT = ImageFont.load_default()

# Colors
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (0, 255, 0)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)
CYAN = (0, 255, 255)
GREY = (128, 128, 128)
ORANGE = (255, 165, 0)

# ── Button GPIOs (active low) ───────────────────────────────────────────
KEY1 = 21
KEY2 = 20
KEY3 = 16
JOY_UP = 6
JOY_DOWN = 19
JOY_LEFT = 5
JOY_RIGHT = 26
JOY_PRESS = 13

ALL_BUTTONS = [KEY1, KEY2, KEY3, JOY_UP, JOY_DOWN, JOY_LEFT, JOY_RIGHT, JOY_PRESS]

# ── State ────────────────────────────────────────────────────────────────
current_page = 0
PAGE_NAMES = ['Status', 'Network', 'D75 Radio', 'Audio']
NUM_PAGES = len(PAGE_NAMES)
backlight_on = True
force_refresh = False
status_message = None  # temporary message overlay
status_message_until = 0


def show_message(msg, duration=3):
    """Show a temporary status message overlay."""
    global status_message, status_message_until
    status_message = msg
    status_message_until = time.time() + duration


# ── Data helpers ─────────────────────────────────────────────────────────

def get_ip(iface):
    try:
        out = subprocess.check_output(
            ['ip', '-4', 'addr', 'show', iface],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        for line in out.split('\n'):
            line = line.strip()
            if line.startswith('inet '):
                return line.split()[1].split('/')[0]
    except Exception:
        pass
    return None


def get_hostname():
    return socket.gethostname()


def get_cpu():
    try:
        load = os.getloadavg()[0]
        ncpu = os.cpu_count() or 1
        return min(load / ncpu * 100, 100)
    except Exception:
        return 0


def get_cpu_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return int(f.read().strip()) / 1000
    except Exception:
        return 0


def get_ram():
    try:
        with open('/proc/meminfo') as f:
            lines = f.read()
        total = used = 0
        for line in lines.split('\n'):
            if line.startswith('MemTotal:'):
                total = int(line.split()[1])
            elif line.startswith('MemAvailable:'):
                avail = int(line.split()[1])
                used = total - avail
        pct = (used / total * 100) if total else 0
        return pct, used // 1024, total // 1024
    except Exception:
        return 0, 0, 0


def get_disk():
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = (1 - free / total) * 100 if total else 0
        return used_pct, free / (1024**3)
    except Exception:
        return 0, 0


def get_bt_status():
    try:
        out = subprocess.check_output(
            ['hciconfig', 'hci0'], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        return 'UP RUNNING' in out
    except Exception:
        return False


def get_bt_discoverable():
    try:
        out = subprocess.check_output(
            ['hciconfig', 'hci0'], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        return 'PSCAN' in out and 'ISCAN' in out
    except Exception:
        return False


def get_endpoint_status():
    try:
        out = subprocess.check_output(
            ['systemctl', '--user', 'is-active', 'link-endpoint'],
            stderr=subprocess.DEVNULL, timeout=2,
            env={**os.environ,
                 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
        ).decode().strip()
        return out == 'active'
    except Exception:
        return False


def get_wifi_info():
    """Get WiFi SSID, signal, frequency, bitrate."""
    info = {}
    try:
        out = subprocess.check_output(
            ['iwconfig', 'wlan0'], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        for line in out.split('\n'):
            if 'ESSID:' in line:
                ssid = line.split('ESSID:')[1].strip().strip('"')
                info['ssid'] = ssid
            if 'Signal level=' in line:
                sig = line.split('Signal level=')[1].split()[0]
                info['signal'] = sig
            if 'Bit Rate=' in line:
                rate = line.split('Bit Rate=')[1].split()[0]
                info['bitrate'] = rate
            if 'Frequency:' in line:
                freq = line.split('Frequency:')[1].split()[0]
                info['freq'] = freq
    except Exception:
        pass
    return info


def get_d75_info():
    """Read D75 status from endpoint log or gateway."""
    info = {'serial': False, 'freq_a': '', 'freq_b': '', 'battery': -1, 'model': ''}
    try:
        out = subprocess.check_output(
            ['systemctl', '--user', 'status', 'link-endpoint', '--no-pager', '-n', '20'],
            stderr=subprocess.DEVNULL, timeout=2,
            env={**os.environ,
                 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
        ).decode()
        if 'CAT + Audio ready' in out:
            info['serial'] = True
        for line in out.split('\n'):
            if 'State dump' in line:
                if "model='" in line:
                    info['model'] = line.split("model='")[1].split("'")[0]
                if "'frequency': '" in line:
                    freqs = [s.split("'frequency': '")[1].split("'")[0]
                             for s in line.split("'frequency': '")[1:]]
                    if len(freqs) >= 1:
                        info['freq_a'] = freqs[0]
                    if len(freqs) >= 2:
                        info['freq_b'] = freqs[1]
    except Exception:
        pass
    return info


def get_battery():
    """Read battery voltage/percentage from Waveshare UPS HAT C (INA219).
    Returns (voltage, current_mA, percentage) or None if not available."""
    try:
        from ina219 import INA219, DeviceRangeError
        ina = INA219(0.1, address=0x43, busnum=1)
        ina.configure(ina.RANGE_16V, ina.GAIN_AUTO)
        voltage = ina.voltage()
        try:
            current = ina.current()  # mA (negative = discharging)
        except DeviceRangeError:
            current = 0.0
        # Li-Po percentage estimate: 4.2V=100%, 3.0V=0%
        pct = max(0, min(100, (voltage - 3.0) / 1.2 * 100))
        return (voltage, current, pct)
    except Exception:
        return None


# ── Drawing helpers ──────────────────────────────────────────────────────

def color_for_pct(pct, invert=False):
    if not invert:
        pct = 100 - pct
    if pct > 80:
        return RED
    if pct > 60:
        return YELLOW
    return GREEN


def draw_bar(draw, x, y, w, h, pct, color):
    draw.rectangle([x, y, x + w, y + h], outline=GREY)
    fill_w = int(w * min(pct, 100) / 100)
    if fill_w > 0:
        draw.rectangle([x, y, x + fill_w, y + h], fill=color)


def draw_header(draw, title):
    draw.rectangle([0, 0, WIDTH, 11], fill=(30, 30, 60))
    draw.text((4, 1), title, font=FONT, fill=CYAN)
    ts = time.strftime('%H:%M')
    draw.text((98, 1), ts, font=FONT, fill=GREY)


# ── Pages ────────────────────────────────────────────────────────────────

def page_status(draw):
    """Main status page: hostname, IPs, CPU, RAM, disk, BT, EP."""
    L = 4
    R = 122
    draw_header(draw, get_hostname())
    y = 14

    wlan_ip = get_ip('wlan0')
    eth_ip = get_ip('eth0')
    if wlan_ip:
        draw.text((L, y), f'W:{wlan_ip}', font=FONT, fill=WHITE)
        y += 10
    if eth_ip:
        draw.text((L, y), f'E:{eth_ip}', font=FONT, fill=WHITE)
        y += 10
    if not wlan_ip and not eth_ip:
        draw.text((L, y), 'No network', font=FONT, fill=RED)
        y += 10
    y += 2

    cpu = get_cpu()
    temp = get_cpu_temp()
    draw.text((L, y), f'CPU {cpu:2.0f}%', font=FONT, fill=WHITE)
    draw.text((62, y), f'{temp:.0f}C', font=FONT, fill=color_for_pct(temp / 80 * 100, invert=True))
    draw_bar(draw, 92, y + 1, R - 92, 7, cpu, color_for_pct(cpu, invert=True))
    y += 12

    ram_pct, ram_used, ram_total = get_ram()
    draw.text((L, y), f'RAM {ram_pct:2.0f}%', font=FONT, fill=WHITE)
    draw.text((62, y), f'{ram_used}M', font=FONT, fill=GREY)
    draw_bar(draw, 92, y + 1, R - 92, 7, ram_pct, color_for_pct(ram_pct, invert=True))
    y += 12

    disk_pct, disk_free = get_disk()
    draw.text((L, y), f'DSK {disk_pct:2.0f}%', font=FONT, fill=WHITE)
    draw.text((62, y), f'{disk_free:.1f}G', font=FONT, fill=GREY)
    draw_bar(draw, 92, y + 1, R - 92, 7, disk_pct, color_for_pct(disk_pct, invert=True))
    y += 14

    bt = get_bt_status()
    ep = get_endpoint_status()
    draw.text((L, y), 'BT:', font=FONT, fill=WHITE)
    draw.text((24, y), 'ON' if bt else 'OFF', font=FONT, fill=GREEN if bt else RED)
    draw.text((56, y), 'EP:', font=FONT, fill=WHITE)
    draw.text((76, y), 'ON' if ep else 'OFF', font=FONT, fill=GREEN if ep else RED)
    y += 14

    batt = get_battery()
    if batt:
        volts, current, pct = batt
        charging = current > 0
        draw.text((L, y), f'BAT {pct:2.0f}%', font=FONT, fill=WHITE)
        state = 'CHG' if charging else f'{abs(current):.0f}mA'
        draw.text((56, y), state, font=FONT, fill=GREEN if charging else GREY)
        draw_bar(draw, 92, y + 1, R - 92, 7, pct, GREEN if charging else color_for_pct(100 - pct, invert=True))

    try:
        with open('/proc/uptime') as f:
            up = int(float(f.read().split()[0]))
        h, m = up // 3600, (up % 3600) // 60
        draw.text((L, HEIGHT - 12), f'Up:{h}h{m:02d}m', font=FONT, fill=GREY)
    except Exception:
        pass


def page_network(draw):
    """Network details: SSID, signal, IPs, gateway, DNS."""
    L = 4
    draw_header(draw, 'Network')
    y = 14

    wlan_ip = get_ip('wlan0')
    eth_ip = get_ip('eth0')

    wifi = get_wifi_info()
    if wifi.get('ssid'):
        draw.text((L, y), f'SSID: {wifi["ssid"][:14]}', font=FONT, fill=WHITE)
        y += 11
    if wifi.get('signal'):
        draw.text((L, y), f'Signal: {wifi["signal"]}dBm', font=FONT, fill=WHITE)
        y += 11
    if wifi.get('bitrate'):
        draw.text((L, y), f'Rate: {wifi["bitrate"]}Mb/s', font=FONT, fill=WHITE)
        y += 11
    if wifi.get('freq'):
        draw.text((L, y), f'Freq: {wifi["freq"]}GHz', font=FONT, fill=WHITE)
        y += 13

    if wlan_ip:
        draw.text((L, y), f'WiFi: {wlan_ip}', font=FONT, fill=GREEN)
        y += 11
    else:
        draw.text((L, y), 'WiFi: disconnected', font=FONT, fill=RED)
        y += 11

    if eth_ip:
        draw.text((L, y), f'Eth:  {eth_ip}', font=FONT, fill=GREEN)
        y += 11
    else:
        draw.text((L, y), 'Eth:  none', font=FONT, fill=GREY)
        y += 11

    # Gateway
    try:
        out = subprocess.check_output(
            ['ip', 'route', 'show', 'default'],
            stderr=subprocess.DEVNULL, timeout=2).decode()
        gw = out.split('via ')[1].split()[0] if 'via ' in out else '?'
        draw.text((L, y), f'GW:   {gw}', font=FONT, fill=GREY)
    except Exception:
        pass


def page_d75(draw):
    """D75 radio info: connection, frequency, model, battery."""
    L = 4
    draw_header(draw, 'D75 Radio')
    y = 14

    info = get_d75_info()
    bt = get_bt_status()
    disc = get_bt_discoverable()

    draw.text((L, y), 'Bluetooth:', font=FONT, fill=WHITE)
    draw.text((70, y), 'UP' if bt else 'DOWN', font=FONT, fill=GREEN if bt else RED)
    y += 12

    draw.text((L, y), 'Discoverable:', font=FONT, fill=WHITE)
    draw.text((86, y), 'YES' if disc else 'NO', font=FONT, fill=YELLOW if disc else GREY)
    y += 12

    draw.text((L, y), 'Serial:', font=FONT, fill=WHITE)
    draw.text((52, y), 'OK' if info['serial'] else 'NO', font=FONT,
              fill=GREEN if info['serial'] else RED)
    y += 14

    if info['model']:
        draw.text((L, y), f'Model: {info["model"]}', font=FONT, fill=CYAN)
        y += 12

    if info['freq_a']:
        draw.text((L, y), f'Band A: {info["freq_a"]}', font=FONT, fill=WHITE)
        y += 11
    if info['freq_b']:
        draw.text((L, y), f'Band B: {info["freq_b"]}', font=FONT, fill=WHITE)
        y += 11

    if info['battery'] >= 0:
        draw.text((L, y), f'Battery: {info["battery"]}', font=FONT, fill=WHITE)


def page_audio(draw):
    """Audio/endpoint status from service logs."""
    L = 4
    draw_header(draw, 'Audio')
    y = 14

    ep = get_endpoint_status()
    draw.text((L, y), 'Endpoint:', font=FONT, fill=WHITE)
    draw.text((64, y), 'ACTIVE' if ep else 'DOWN', font=FONT, fill=GREEN if ep else RED)
    y += 14

    # Parse recent endpoint diag line
    reads = sends = slow = '?'
    try:
        out = subprocess.check_output(
            ['systemctl', '--user', 'status', 'link-endpoint', '--no-pager', '-n', '30'],
            stderr=subprocess.DEVNULL, timeout=2,
            env={**os.environ,
                 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
        ).decode()
        for line in reversed(out.split('\n')):
            if 'DIAG' in line:
                parts = line.split('reads=')[1] if 'reads=' in line else ''
                if parts:
                    reads = parts.split()[0]
                    sends = line.split('sends=')[1].split()[0] if 'sends=' in line else '?'
                    slow = line.split('slow_read=')[1].split()[0] if 'slow_read=' in line else '?'
                break
    except Exception:
        pass

    draw.text((L, y), f'Reads:  {reads}', font=FONT, fill=WHITE)
    y += 11
    draw.text((L, y), f'Sends:  {sends}', font=FONT, fill=WHITE)
    y += 11
    draw.text((L, y), f'Slow:   {slow}', font=FONT, fill=YELLOW if slow != '0' and slow != '?' else GREEN)
    y += 14

    # Connection info
    try:
        out2 = subprocess.check_output(
            ['systemctl', '--user', 'status', 'link-endpoint', '--no-pager', '-n', '50'],
            stderr=subprocess.DEVNULL, timeout=2,
            env={**os.environ,
                 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
        ).decode()
        for line in reversed(out2.split('\n')):
            if '[Link] Connected' in line:
                if 'TCP' in line:
                    draw.text((L, y), 'Link: TCP (LAN)', font=FONT, fill=GREEN)
                elif 'WS' in line:
                    draw.text((L, y), 'Link: WS (tunnel)', font=FONT, fill=YELLOW)
                else:
                    draw.text((L, y), 'Link: connected', font=FONT, fill=GREEN)
                break
        else:
            draw.text((L, y), 'Link: not connected', font=FONT, fill=RED)
    except Exception:
        draw.text((L, y), 'Link: unknown', font=FONT, fill=GREY)


PAGE_RENDERERS = [page_status, page_network, page_d75, page_audio]


# ── Button handlers ──────────────────────────────────────────────────────

def action_restart_endpoint():
    show_message('Restarting EP...')
    try:
        subprocess.Popen(
            ['systemctl', '--user', 'restart', 'link-endpoint'],
            env={**os.environ,
                 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        show_message('EP restarted', 2)
    except Exception as e:
        show_message(f'Err: {e}', 3)


def action_toggle_discoverable():
    disc = get_bt_discoverable()
    if disc:
        subprocess.run(['sudo', 'hciconfig', 'hci0', 'noscan'],
                       capture_output=True, timeout=3)
        show_message('BT: noscan', 2)
    else:
        subprocess.run(['sudo', 'hciconfig', 'hci0', 'piscan'],
                       capture_output=True, timeout=3)
        show_message('BT: discoverable', 2)


def action_toggle_backlight():
    global backlight_on
    backlight_on = not backlight_on
    DISPLAY.set_backlight(backlight_on)


def action_page_up():
    global current_page
    current_page = (current_page - 1) % NUM_PAGES


def action_page_down():
    global current_page
    current_page = (current_page + 1) % NUM_PAGES


def action_force_refresh():
    global force_refresh
    force_refresh = True


def action_shutdown():
    """Shutdown the Pi. Shows countdown, can be cancelled by any button."""
    global _shutdown_pending
    _shutdown_pending = True
    show_message('Shutting down...', 3)
    time.sleep(3)
    if _shutdown_pending:
        # Clear display before shutdown
        DISPLAY.display(Image.new('RGB', (WIDTH, HEIGHT), BLACK))
        subprocess.run(['sudo', '-n', 'shutdown', '-h', 'now'],
                       capture_output=True, timeout=5)

_shutdown_pending = False


# ── Button polling thread ────────────────────────────────────────────────

def button_thread():
    """Poll buttons via gpiod. Debounced. Long-press joystick = shutdown."""
    global _shutdown_pending
    chip = gpiod.Chip('/dev/gpiochip0')
    config = {pin: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT,
                                       bias=gpiod.line.Bias.PULL_UP)
              for pin in ALL_BUTTONS}
    request = chip.request_lines(config=config)

    handlers = {
        KEY1: action_restart_endpoint,
        KEY2: action_toggle_discoverable,
        KEY3: action_toggle_backlight,
        JOY_UP: action_page_up,
        JOY_DOWN: action_page_down,
        JOY_PRESS: action_force_refresh,
        JOY_LEFT: action_page_up,
        JOY_RIGHT: action_page_down,
    }

    prev = {pin: True for pin in ALL_BUTTONS}
    joy_press_start = 0.0  # track how long joystick is held
    boot_time = time.monotonic()  # ignore inputs for first 5s

    while True:
        if time.monotonic() - boot_time < 5.0:
            time.sleep(0.1)
            continue

        for pin in ALL_BUTTONS:
            val = request.get_value(pin)
            pressed = (val == gpiod.line.Value.ACTIVE)

            if pin == JOY_PRESS:
                if pressed:
                    if joy_press_start == 0.0:
                        joy_press_start = time.monotonic()
                    elif time.monotonic() - joy_press_start > 3.0:
                        # Long press — shutdown
                        threading.Thread(target=action_shutdown, daemon=True).start()
                        joy_press_start = 0.0
                else:
                    if joy_press_start > 0.0 and time.monotonic() - joy_press_start < 3.0:
                        # Short press — refresh
                        action_force_refresh()
                        # Cancel pending shutdown if any
                        _shutdown_pending = False
                    joy_press_start = 0.0
            else:
                if pressed and prev[pin]:
                    # Any button press cancels pending shutdown
                    if _shutdown_pending:
                        _shutdown_pending = False
                        show_message('Shutdown cancelled', 2)
                    else:
                        handler = handlers.get(pin)
                        if handler:
                            handler()
            prev[pin] = not pressed
        time.sleep(0.1)


# ── Main loop ────────────────────────────────────────────────────────────

def render_frame():
    img = Image.new('RGB', (WIDTH, HEIGHT), BLACK)
    draw = ImageDraw.Draw(img)

    PAGE_RENDERERS[current_page](draw)

    # Page indicator dots at bottom center
    dot_y = HEIGHT - 4
    total_w = NUM_PAGES * 8
    dot_x = (WIDTH - total_w) // 2
    for i in range(NUM_PAGES):
        c = WHITE if i == current_page else GREY
        draw.rectangle([dot_x + i * 8, dot_y, dot_x + i * 8 + 4, dot_y + 2], fill=c)

    # Status message overlay
    if status_message and time.time() < status_message_until:
        tw = len(status_message) * 6 + 12
        tx = (WIDTH - tw) // 2
        draw.rectangle([tx, 50, tx + tw, 68], fill=(40, 40, 40), outline=WHITE)
        draw.text((tx + 6, 54), status_message, font=FONT, fill=YELLOW)

    return img


def main():
    # Start button polling in background
    t = threading.Thread(target=button_thread, daemon=True)
    t.start()

    try:
        while True:
            global force_refresh
            img = render_frame()
            DISPLAY.display(img)
            force_refresh = False
            time.sleep(2)
    except KeyboardInterrupt:
        DISPLAY.display(Image.new('RGB', (WIDTH, HEIGHT), BLACK))


if __name__ == '__main__':
    main()
