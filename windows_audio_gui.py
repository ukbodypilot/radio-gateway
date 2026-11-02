#!/usr/bin/env python3
"""Windows Audio Client for Radio Gateway — desktop GUI.

A graphical front end for windows_audio_client.py. All audio work (program-feed
capture, operator mic, voice effects, gateway TCP links) is done by the engine's
own TX / MIC / RX threads; this module only builds the shared `state` dict they
read, and draws it. The console client keeps working unchanged.

Controls:
  - Gateway host + Connect / Disconnect.
  - Device dropdowns for the program feed, operator mic and speaker. Changing
    one while connected hot-swaps it (the engine picks up *_dev_pending).
  - Hold-to-talk button (press and hold), an optional latch mode, SPACE while the
    window is focused, and an optional global hotkey (default F9) that works
    from any window.
  - Mic gain (0-1000%), program volume, speaker volume, duck depth, voice effect.
  - Live level meters, connection state, update status.
  - Native folder dialog for the TX WAV recording, tray icon with Show / Quit.

Run from source:   python windows_audio_gui.py [gateway_host] [--no-update]
Normally started by the packaged launcher (windows_client_launcher.py).
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog

import customtkinter as ctk

import windows_audio_client as wac

try:
    import keyboard as _kb          # global hold-to-talk hotkey (optional)
except Exception:
    _kb = None

try:
    import psutil                   # CPU / memory stats (optional)
except Exception:
    psutil = None

try:
    import pystray                  # tray icon (optional)
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

FROZEN = bool(getattr(sys, "frozen", False))
APP_NAME = "RadioGatewayClient"
WIDE_PX = 1000          # window width at which Controls and Stats go side by side
MAX_RECENT_HOSTS = 6
_ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def data_dir():
    """Per-user folder for config, logs and (when packaged) the engine .py files."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_streams():
    """A windowed (no console) exe has no stdout/stderr, but the engine prints
    and writes to stderr. Point both at a log file so nothing raises."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = open(os.path.join(data_dir(), "client.log"), "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def strip_ansi(s):
    """Remove the console colour codes the engine puts in its status messages."""
    return _ANSI.sub("", s or "")


def dedupe(devices):
    """One entry per device name (PortAudio lists each device once per host API).
    wac.find_device_by_name already resolves to the first match, so keep that one."""
    seen, out = set(), []
    for idx, name, ch in devices:
        if name not in seen:
            seen.add(name)
            out.append((idx, name, ch))
    return out


def level_frac(db):
    """Map a dBFS level to a 0..1 meter fill over a 60 dB range."""
    return max(0.0, min(1.0, (db + 60.0) / 60.0))


def fmt_bytes(n):
    """Human-readable byte count."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


class Graph(ctk.CTkFrame):
    """Scrolling line graph on a plain canvas, newest sample at the right.

    `hi=None` autoscales (never below `floor`); a fixed `lo`/`hi` pins the range.
    A None sample leaves a gap in the line. Drawing is skipped while the graph
    isn't on screen, so the hidden Stats tab costs nothing.
    """

    def __init__(self, parent, title, unit="", lo=0.0, hi=None, floor=1.0,
                 fmt="{:.0f}", color="#4aa3ff", n=90, height=52):
        super().__init__(parent, fg_color="transparent")
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.val = ctk.CTkLabel(head, text="-", anchor="e",
                                font=ctk.CTkFont(size=12, weight="bold"))
        self.val.pack(side="right")
        self.cv = tk.Canvas(self, height=height, bg="#1b1b1b", highlightthickness=0)
        self.cv.pack(fill="x")
        self.cv.bind("<Configure>", lambda e: self._render())
        self.data = deque(maxlen=n)
        self._text = None
        self.unit, self.lo, self.hi, self.floor = unit, lo, hi, floor
        self.fmt, self.color = fmt, color

    def push(self, v):
        """Add a sample (or None for 'no data') and redraw."""
        self.data.append(v)
        text = "-" if v is None else self.fmt.format(v) + self.unit
        if text != self._text:
            self._text = text
            self.val.configure(text=text)
        self._render()

    def _render(self):
        cv = self.cv
        if not cv.winfo_viewable():
            return
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        vals = [v for v in self.data if v is not None]
        hi = self.hi if self.hi is not None else max(self.floor, max(vals, default=0.0) * 1.2)
        lo = self.lo
        span = (hi - lo) or 1.0
        cv.create_line(0, h / 2, w, h / 2, fill="#2a2a2a")
        step = w / max(1, self.data.maxlen - 1)
        x0 = w - (len(self.data) - 1) * step
        seg = []

        def flush():
            if len(seg) >= 4:
                cv.create_line(*seg, fill=self.color, width=1.5)
            elif len(seg) == 2:
                cv.create_oval(seg[0] - 1.5, seg[1] - 1.5, seg[0] + 1.5, seg[1] + 1.5,
                               fill=self.color, outline="")

        for i, v in enumerate(self.data):
            if v is None:
                flush()
                seg.clear()
                continue
            y = h - 2 - (max(lo, min(hi, v)) - lo) / span * (h - 4)
            seg += [x0 + i * step, y]
        flush()
        cv.create_text(w - 3, 2, anchor="ne", fill="#707070", font=("Segoe UI", 8),
                       text=f"{hi:.0f}")


class App(ctk.CTk):
    """Main window. `self.eng` is the engine's shared state dict while
    connected, and None otherwise; `self.vals` holds the user's settings so they
    survive a disconnect and seed the next connection."""

    def __init__(self, argv):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Radio Gateway Client")
        h = min(860, max(520, self.winfo_screenheight() - 120))
        self.geometry(f"600x{h}+40+20")
        self.minsize(520, 480)

        self.argv = argv
        self.cfg = wac.load_config()
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.cfg.get("window_geometry") or "")
        if m:
            # Clamp to the current screen so an unplugged monitor can't strand the window.
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, hh, x, y = (int(v) for v in m.groups())
            w, hh = max(520, min(w, sw)), max(480, min(hh, sh))
            x, y = max(0, min(x, sw - 120)), max(0, min(y, sh - 120))
            self.geometry(f"{w}x{hh}+{x}+{y}")
        self.eng = None
        self.threads = []
        self.tray = None
        self.hotkey_hooks = []
        self._ptt_release_job = None
        self._closing = False
        self._todo = []            # callables queued by other threads, run by _poll
        self._cfg_cache = {}       # widget -> last configure() options, see _cfg
        self._picked = set()       # device kinds the user chose by hand this session
        self._bar_last = {}        # meter key -> last drawn value
        self._host0 = ''

        c = self.cfg
        self.vals = {
            "tx_vol": 100, "rx_vol": 100,
            "mic_vol": int(c.get("mic_vol", 100)),
            "duck_pct": int(c.get("duck_pct", 25)),
            "effect_index": int(c.get("effect_index", 0)),
            "tx_live": bool(c.get("tx_live", False)),
            "rx_play": True,
        }
        self.vals["effected"] = self.vals["effect_index"] != 0

        # stats sampling state
        self._net_prev = None            # (monotonic, tx_bytes, rx_bytes)
        self._rtt = None                 # latest gateway round-trip, ms
        self._rtt_hist = deque(maxlen=30)
        self._probe_target = None        # (host, port) the RTT probe connects to
        self._probe_stop = threading.Event()
        self._proc = None
        self._cpu_prev = None            # (monotonic, {tid: cpu seconds})
        if psutil is not None:
            try:
                self._proc = psutil.Process()
                self._proc.cpu_percent(None)       # prime; first call returns 0
                psutil.cpu_percent(None)
            except Exception:
                self._proc = None

        self._build()
        self._load_devices()
        self._apply_hotkey()
        self._start_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close_button)
        self._relayout_job = None
        self.bind("<Configure>", self._on_configure)
        self.bind_all("<KeyPress-space>", self._space_down)
        self.bind_all("<KeyRelease-space>", self._space_up)
        self._host0 = self.host.get().strip()
        self._set_probe_target(self._host0)
        threading.Thread(target=self._probe_loop, daemon=True, name="Probe").start()
        self.after(80, self._poll)
        self.after(250, self._tick_fast)
        self.after(1000, self._tick_slow)
        threading.Thread(target=self._startup_update_then_connect, daemon=True).start()

    # ------------------------------------------------------------------ UI --
    def _row(self, parent, text):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(f, text=text, width=120, anchor="w").pack(side="left")
        return f

    def _section(self, title):
        box = ctk.CTkFrame(self.body)
        box.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(box, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 2))
        return box

    def _on_configure(self, event):
        """Window resized: re-pick the layout once the drag settles."""
        if event.widget is not self:
            return
        if self._relayout_job is not None:
            self.after_cancel(self._relayout_job)
        self._relayout_job = self.after(80, self._relayout)

    def _relayout(self, force=False):
        self._relayout_job = None
        wide = self.winfo_width() >= WIDE_PX
        if wide == self._layout_wide and not force:
            return
        self._layout_wide = wide
        if wide:
            self.seg.grid_remove()
            self.body.grid(row=1, column=0, columnspan=1, sticky="nsew", padx=(0, 4))
            self.stats_body.grid(row=1, column=1, columnspan=1, sticky="nsew", padx=(4, 0))
            return
        self.seg.grid(row=0, column=0, columnspan=2, pady=(2, 6))
        show, hide = ((self.body, self.stats_body) if self.seg.get() == "Controls"
                      else (self.stats_body, self.body))
        hide.grid_remove()
        show.grid(row=1, column=0, columnspan=2, sticky="nsew")

    def _build(self):
        # Footer is packed first so it stays visible; the controls scroll above it.
        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.pack(side="bottom", fill="x")
        # Both panes always exist; _relayout() shows them side by side when the
        # window is wide, otherwise one at a time behind a Controls / Stats switch.
        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.pack(side="top", fill="both", expand=True, padx=8)
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_columnconfigure((0, 1), weight=1, uniform="pane")
        self.seg = ctk.CTkSegmentedButton(self.main, values=["Controls", "Stats"],
                                          command=lambda _v: self._relayout(force=True))
        self.seg.set("Controls")
        self.body = ctk.CTkScrollableFrame(self.main, fg_color="transparent")
        self.stats_body = ctk.CTkScrollableFrame(self.main, fg_color="transparent")
        self._layout_wide = None

        # --- connection ------------------------------------------------------
        box = self._section("Gateway")
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 4))
        host = (self.argv[0] if self.argv and not self.argv[0].startswith("--")
                else self.cfg.get("gateway_host", ""))
        recent = list(self.cfg.get("recent_hosts") or [])
        if host and host not in recent:
            recent.insert(0, host)
        # Editable dropdown: type a new address or pick a previous gateway.
        self.host = ctk.CTkComboBox(r, values=recent or [""])
        self.host.set(host)
        self.host.pack(side="left", fill="x", expand=True)
        self.host.bind("<Return>", lambda e: self._host_committed())
        self.btn_connect = ctk.CTkButton(r, text="Connect", width=110, command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=(8, 0))
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 8))
        self.lbl_tx = ctk.CTkLabel(r, text="TX: offline", anchor="w")
        self.lbl_tx.pack(side="left", padx=(0, 18))
        self.lbl_rx = ctk.CTkLabel(r, text="RX: offline", anchor="w")
        self.lbl_rx.pack(side="left")
        self.sw_auto = ctk.CTkSwitch(r, text="Auto-connect on start")
        if self.cfg.get("auto_connect", True):
            self.sw_auto.select()
        self.sw_auto.pack(side="right")

        # --- devices ---------------------------------------------------------
        box = self._section("Audio devices")
        self.dev_menus = {}
        for kind, label in (("tx", "Program feed (in)"), ("mic", "Operator mic (in)"),
                            ("out", "Speaker (out)")):
            f = self._row(box, label)
            m = ctk.CTkOptionMenu(f, values=["(none)"], dynamic_resizing=False,
                                  command=lambda name, k=kind: self._device_chosen(k, name))
            m.pack(side="left", fill="x", expand=True)
            self.dev_menus[kind] = m
        f = ctk.CTkFrame(box, fg_color="transparent")
        f.pack(fill="x", padx=12, pady=(2, 8))
        ctk.CTkButton(f, text="Rescan devices", width=130, command=self._rescan).pack(side="left")

        # --- levels ----------------------------------------------------------
        box = self._section("Levels")
        self.bars, self.sliders = {}, {}
        self._meter_row(box, "tx", "Program feed", "tx_vol", 100, "Live", "tx_live")
        self._meter_row(box, "rx", "Speaker", "rx_vol", 100, "Play", "rx_play")
        self._meter_row(box, "mic", "Mic gain %", "mic_vol", wac.MIC_GAIN_MAX, None, None)
        f = self._row(box, "Duck depth %")
        s = ctk.CTkSlider(f, from_=0, to=100, number_of_steps=20,
                          command=lambda v: self._set("duck_pct", int(v)))
        s.set(self.vals["duck_pct"])
        s.pack(side="left", fill="x", expand=True)
        self.lbl_duck = ctk.CTkLabel(f, text="", width=48)
        self.lbl_duck.pack(side="left")
        self.sliders["duck_pct"] = (s, self.lbl_duck)
        ctk.CTkLabel(box, text="How far the program feed dips while you talk.",
                     text_color="gray60").pack(anchor="w", padx=12, pady=(0, 8))

        # --- voice effect ----------------------------------------------------
        box = self._section("Voice effect (mic)")
        f = ctk.CTkFrame(box, fg_color="transparent")
        f.pack(fill="x", padx=12, pady=(0, 8))
        self.fx_menu = ctk.CTkOptionMenu(f, values=[n for n, _ in wac.EFFECTS],
                                         command=self._effect_chosen)
        self.fx_menu.set(wac.effect_name(self.vals["effect_index"]))
        self.fx_menu.pack(side="left", fill="x", expand=True)
        self.fx_switch = ctk.CTkSwitch(f, text="On", command=self._effect_toggled)
        if self.vals["effected"]:
            self.fx_switch.select()
        self.fx_switch.pack(side="left", padx=(12, 0))

        # --- talk ------------------------------------------------------------
        box = self._section("Talk")
        self.btn_ptt = ctk.CTkButton(box, text="HOLD TO TALK  (Space)", height=64,
                                     font=ctk.CTkFont(size=18, weight="bold"),
                                     fg_color="#3b3b3b", hover_color="#4a4a4a")
        self.btn_ptt.pack(fill="x", padx=12, pady=(2, 6))
        self.btn_ptt.bind("<ButtonPress-1>", lambda e: self._ptt_press(), add="+")
        self.btn_ptt.bind("<ButtonRelease-1>", lambda e: self._ptt_release(), add="+")
        f = ctk.CTkFrame(box, fg_color="transparent")
        f.pack(fill="x", padx=12, pady=(0, 8))
        self.sw_latch = ctk.CTkSwitch(f, text="Latch (click to toggle)")
        if self.cfg.get("ptt_latch"):
            self.sw_latch.select()
        self.sw_latch.pack(side="left")
        self.sw_hotkey = ctk.CTkSwitch(f, text="Global hotkey", command=self._apply_hotkey)
        if self.cfg.get("ptt_hotkey_enabled"):
            self.sw_hotkey.select()
        if _kb is None:
            self.sw_hotkey.configure(state="disabled", text="Global hotkey (keyboard lib missing)")
        self.sw_hotkey.pack(side="left", padx=(18, 6))
        self.ent_hotkey = ctk.CTkEntry(f, width=64)
        self.ent_hotkey.insert(0, self.cfg.get("ptt_hotkey", "f9"))
        self.ent_hotkey.pack(side="left")
        self.ent_hotkey.bind("<Return>", lambda e: self._apply_hotkey())
        self.ent_hotkey.bind("<FocusOut>", lambda e: self._apply_hotkey())

        # --- footer ----------------------------------------------------------
        f = ctk.CTkFrame(self.footer, fg_color="transparent")
        f.pack(fill="x", padx=14, pady=(10, 0))
        self.btn_rec = ctk.CTkButton(f, text="Record TX to WAV…", width=150,
                                     command=self._toggle_record)
        self.btn_rec.pack(side="left")
        ctk.CTkButton(f, text="Quit", width=80, fg_color="#7a2b2b", hover_color="#933",
                      command=self.quit_app).pack(side="right")
        self.lbl_msg = ctk.CTkLabel(self.footer, text="", anchor="w", justify="left",
                                    wraplength=520, text_color="gray70")
        self.lbl_msg.pack(fill="x", padx=16, pady=(8, 0))
        self.lbl_ver = ctk.CTkLabel(self.footer, text="", anchor="w", text_color="gray55")
        self.lbl_ver.pack(fill="x", padx=16, pady=(2, 8))
        self._refresh_sliders()
        self._build_stats()
        self._relayout(force=True)

    def _stat_section(self, title):
        box = ctk.CTkFrame(self.stats_body)
        box.pack(fill="x", padx=6, pady=(8, 0))
        ctk.CTkLabel(box, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 2))
        return box

    def _stat_graph(self, box, *a, **kw):
        g = Graph(box, *a, **kw)
        g.pack(fill="x", padx=12, pady=3)
        return g

    def _stat_text(self, box):
        lbl = ctk.CTkLabel(box, text="", anchor="w", justify="left",
                           font=ctk.CTkFont(family="Consolas", size=12), text_color="gray75")
        lbl.pack(fill="x", padx=12, pady=(2, 10))
        return lbl

    def _build_stats(self):
        box = self._stat_section("Network")
        self.g_txk = self._stat_graph(box, "TX to gateway", " kbps", floor=64, color="#4aa3ff")
        self.g_rxk = self._stat_graph(box, "RX from gateway", " kbps", floor=64, color="#66bb6a")
        self.g_rtt = self._stat_graph(box, "Gateway round-trip (TCP connect)", " ms",
                                      floor=10, fmt="{:.1f}", color="#ffb74d")
        self.t_net = self._stat_text(box)

        box = self._stat_section("Audio engine")
        self.g_lvl_tx = self._stat_graph(box, "Program feed level", " dB", lo=-60, hi=0,
                                         fmt="{:.0f}", color="#4aa3ff")
        self.g_lvl_mic = self._stat_graph(box, "Mic level (after gain)", " dB", lo=-60, hi=0,
                                          fmt="{:.0f}", color="#ba68c8")
        self.g_lvl_rx = self._stat_graph(box, "Speaker level", " dB", lo=-60, hi=0,
                                         fmt="{:.0f}", color="#66bb6a")
        self.g_loop = self._stat_graph(box, "TX loop time per chunk", " ms", floor=25,
                                       fmt="{:.1f}", color="#e57373")
        self.t_audio = self._stat_text(box)

        box = self._stat_section("CPU / GPU")
        self.g_cpu = self._stat_graph(box, "This app (CPU, all cores)", "%", floor=10,
                                      fmt="{:.1f}", color="#4aa3ff")
        self.g_sys = self._stat_graph(box, "Whole system (CPU)", "%", hi=100, color="#9e9e9e")
        self.t_load = self._stat_text(box)
        self.g_gpu = self._stat_graph(box, "GPU", "%", hi=100, color="#76ff03")
        self.t_gpu = self._stat_text(box)

    def _meter_row(self, parent, key, label, vol_key, vmax, sw_text, sw_key):
        f = self._row(parent, label)
        bar = ctk.CTkProgressBar(f, height=14)
        bar.set(0)
        bar.pack(side="left", fill="x", expand=True)
        self.bars[key] = bar
        if sw_text:
            sw = ctk.CTkSwitch(f, text=sw_text, width=70,
                               command=lambda k=sw_key: self._switch_changed(k))
            if self.vals[sw_key]:
                sw.select()
            sw.pack(side="left", padx=(10, 0))
            setattr(self, "sw_" + sw_key, sw)
        f2 = ctk.CTkFrame(parent, fg_color="transparent")
        f2.pack(fill="x", padx=12, pady=(0, 2))
        ctk.CTkLabel(f2, text="", width=120).pack(side="left")
        s = ctk.CTkSlider(f2, from_=0, to=vmax, number_of_steps=vmax // (10 if vmax > 100 else 5),
                          command=lambda v, k=vol_key: self._set(k, int(v)))
        s.set(self.vals[vol_key])
        s.pack(side="left", fill="x", expand=True)
        lbl = ctk.CTkLabel(f2, text="", width=48)
        lbl.pack(side="left")
        self.sliders[vol_key] = (s, lbl)

    # ------------------------------------------------------------- settings --
    def _set(self, key, value):
        """Update a user setting and push it to the running engine."""
        self.vals[key] = value
        if self.eng is not None:
            self.eng[key] = value
        self._refresh_sliders()

    def _refresh_sliders(self):
        for key, (_s, lbl) in self.sliders.items():
            lbl.configure(text=f"{self.vals[key]}%")

    def _switch_changed(self, key):
        self._set(key, bool(getattr(self, "sw_" + key).get()))

    def _effect_chosen(self, name):
        names = [n for n, _ in wac.EFFECTS]
        i = names.index(name)
        self._push_effect(i, i != 0)

    def _effect_toggled(self):
        on = bool(self.fx_switch.get())
        i = self.vals["effect_index"]
        if on and i == 0:
            i = 1                      # same as the 'v' key: leave the Raw slot
        self._push_effect(i, on and i != 0)

    def _push_effect(self, index, effected):
        self.vals["effect_index"] = index
        self.vals["effected"] = effected
        self.fx_menu.set(wac.effect_name(index))
        if effected:
            self.fx_switch.select()
        else:
            self.fx_switch.deselect()
        if self.eng is not None:
            self.eng["effect_index"] = index
            self.eng["effect_name"] = wac.effect_name(index)
            self.eng["effected"] = effected

    # -------------------------------------------------------------- devices --
    def _device_lists(self):
        try:
            return (dedupe(wac.list_input_devices()),
                    dedupe(wac.list_output_devices()))
        except Exception as e:
            self._msg(f"Device scan failed: {e}")
            return [], []

    def _load_devices(self):
        ins, outs = self._device_lists()
        self.dev_items = {"tx": ins, "mic": ins, "out": outs}
        saved = {"tx": self.cfg.get("tx_device_name"),
                 "mic": self.cfg.get("mic_device_name"),
                 "out": self.cfg.get("rx_device_name")}
        for kind, menu in self.dev_menus.items():
            names = [n for _i, n, _c in self.dev_items[kind]] or ["(none)"]
            menu.configure(values=names)
            if kind in self._picked and menu.get() in names:
                continue                  # the user's own choice stands
            # Saved device if it is present, else whatever is showing, else the first.
            menu.set(saved[kind] if saved[kind] in names
                     else (menu.get() if menu.get() in names else names[0]))

    def _rescan(self, quiet=False):
        if self.eng is not None:
            self._msg("Disconnect first to rescan — PortAudio can't re-enumerate "
                      "while streams are open.")
            return
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
        except Exception as e:
            self._msg(f"Rescan failed: {e}")
            return
        self._load_devices()
        if not quiet:
            self._msg("Device list refreshed.")

    def _missing_saved(self):
        """Saved device names that aren't in the current device list (and that the
        user hasn't replaced by hand)."""
        keys = {"tx": "tx_device_name", "mic": "mic_device_name", "out": "rx_device_name"}
        missing = []
        for kind, key in keys.items():
            want = self.cfg.get(key)
            have = [n for _i, n, _c in self.dev_items[kind]]
            if want and kind not in self._picked and want not in have:
                missing.append(want)
        return missing

    def _auto_connect(self, tries=0):
        """Connect on start, but only with the devices you saved. Exclusive
        devices (WDM-KS) can be invisible for a moment while a previous copy is
        still letting go of them, so rescan a few times before giving up. If one
        is still missing, don't connect with a stand-in: say so instead."""
        missing = self._missing_saved()
        if missing and tries < 8:
            self._msg(f"Waiting for audio device: {missing[0]} …")
            self._rescan(quiet=True)
            self.after(750, lambda: self._auto_connect(tries + 1))
            return
        if missing:
            self._msg(f"Saved device not found: {', '.join(missing)}. "
                      "Pick one above and press Connect.")
            return
        self.connect()

    def _device_chosen(self, kind, name):
        self._picked.add(kind)
        if self.eng is None:
            return
        for idx, n, _c in self.dev_items[kind]:
            if n == name:
                self.eng[f"{kind}_dev_pending"] = (idx, n)
                self._msg(f"Switching {kind} device -> {name} …")
                return

    def _selected(self, kind):
        name = self.dev_menus[kind].get()
        for idx, n, _c in self.dev_items[kind]:
            if n == name:
                return idx, n
        return None

    # ----------------------------------------------------------- connection --
    def _remember_host(self, host):
        """Put `host` at the head of the recent-gateways list (and the dropdown)."""
        recent = [h for h in (self.cfg.get("recent_hosts") or []) if h != host]
        recent.insert(0, host)
        recent = recent[:MAX_RECENT_HOSTS]
        self.cfg["recent_hosts"] = recent
        self.cfg["gateway_host"] = host
        self.host.configure(values=recent)

    def _host_committed(self):
        """Enter in the host box: remember it and connect if we aren't already."""
        host = self.host.get().strip()
        if not host:
            return
        self._remember_host(host)
        self._save_cfg()
        self._set_probe_target(host)
        if self.eng is None:
            self.connect()

    def _toggle_connect(self):
        if self.eng is None:
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        host = self.host.get().strip()
        if not host:
            self._msg("Enter the gateway host first.")
            return
        picks = {k: self._selected(k) for k in ("tx", "mic", "out")}
        missing = [k for k, v in picks.items() if v is None]
        if missing:
            self._msg("Pick a device for: " + ", ".join(missing))
            return
        (tx_i, tx_n), (mic_i, mic_n), (out_i, out_n) = picks["tx"], picks["mic"], picks["out"]

        self._remember_host(host)
        cfg = self.cfg
        wac.AUDIO_LATENCY = cfg.get("audio_latency", wac.AUDIO_LATENCY)
        try:
            wac.TX_BLOCK_MS = max(5, int(cfg.get("tx_block_ms", wac.TX_BLOCK_MS)))
        except (TypeError, ValueError):
            pass
        tx_port = cfg.get("tx_port", wac.DEFAULT_TX_PORT)
        rx_port = cfg.get("rx_port", wac.DEFAULT_RX_PORT)
        cfg.update(gateway_host=host, tx_port=tx_port, rx_port=rx_port)
        # Remember a device only if you picked it (or none was saved yet); a
        # stand-in chosen because the saved one was missing must not replace it.
        for kind, key, name in (("tx", "tx_device_name", tx_n),
                                ("mic", "mic_device_name", mic_n),
                                ("out", "rx_device_name", out_n)):
            if kind in self._picked or not cfg.get(key):
                cfg[key] = name
        self._save_cfg()

        v = self.vals
        v["ptt_held"] = False
        state = {
            "running": True, "tx_live": v["tx_live"], "rx_play": v["rx_play"],
            "tx_vol": v["tx_vol"], "rx_vol": v["rx_vol"],
            "tx_connected": False, "rx_connected": False,
            "tx_db": -100.0, "rx_db": -100.0, "rx_from": None,
            "tx_resample_enabled": True, "tx_xruns": 0, "tx_last_frames": 0,
            "picker": None, "picker_msg": "",
            "tx_dev_name": tx_n, "rx_dev_name": out_n, "mic_dev_name": mic_n,
            "ptt_key": "space", "ptt_held": False, "mic_tx_active": False,
            "mic_db": -100.0, "mic_vol": v["mic_vol"], "duck_pct": v["duck_pct"],
            "effect_index": v["effect_index"], "effected": v["effected"],
            "effect_name": wac.effect_name(v["effect_index"]),
            "tx_wav_dir": cfg.get("wav_dir") or data_dir(),
        }
        mic_fifo = wac.SampleFIFO(maxlen=wac.FRAMES_PER_BUFFER * 4)
        specs = (
            ("TX", wac._tx_thread_func, (state, cfg, host, tx_port, tx_i, tx_n, mic_fifo)),
            ("MIC", wac._mic_thread_func, (state, cfg, mic_i, mic_n, mic_fifo)),
            ("RX", wac._rx_thread_func, (state, cfg, rx_port, out_i, out_n)),
        )
        self.threads = [threading.Thread(target=fn, args=a, daemon=True, name=n)
                        for n, fn, a in specs]
        for t in self.threads:
            t.start()
        # Same nvidia-smi poller the console client uses; it quietly exits if
        # there is no NVIDIA GPU.
        threading.Thread(target=wac._gpu_monitor_thread, args=(state,),
                         daemon=True, name="GPU").start()
        self._net_prev = (time.monotonic(), 0, 0)
        self._set_probe_target(host)
        self.eng = state
        self.btn_connect.configure(text="Disconnect")
        self._msg(f"Connecting to {host} …")

    def disconnect(self):
        st = self.eng
        if st is None:
            return
        self._save_cfg()
        st["running"] = False
        st["ptt_held"] = False
        self.eng = None
        self._net_prev = None
        self.btn_connect.configure(text="Connect", state="disabled")
        self.lbl_tx.configure(text="TX: offline", text_color="gray70")
        self.lbl_rx.configure(text="RX: offline", text_color="gray70")
        self._reset_meters()
        # Give the engine threads a moment to close their streams before the
        # user can reconnect and re-open the same devices.
        self.after(1200, lambda: self.btn_connect.configure(state="normal"))
        self._msg("Disconnected.")

    def _reset_meters(self):
        for bar in self.bars.values():
            bar.set(0)

    # ------------------------------------------------------------------ PTT --
    def _set_ptt(self, held):
        self.vals["ptt_held"] = held
        if self.eng is not None:
            self.eng["ptt_held"] = held

    def _ptt_held(self):
        return bool(self.vals.get("ptt_held"))

    def _ptt_press(self):
        if self.sw_latch.get():
            self._set_ptt(not self._ptt_held())
        else:
            self._set_ptt(True)

    def _ptt_release(self):
        if not self.sw_latch.get():
            self._set_ptt(False)

    def _space_down(self, _e):
        if isinstance(self.focus_get(), tk.Entry):
            return
        # Windows auto-repeat sends Release+Press pairs while the key is held;
        # cancel the pending release so the talk state doesn't flicker.
        if self._ptt_release_job is not None:
            self.after_cancel(self._ptt_release_job)
            self._ptt_release_job = None
        else:
            self._ptt_press()

    def _space_up(self, _e):
        if self.sw_latch.get():
            self._ptt_release_job = None
            return
        if self._ptt_release_job is None:
            self._ptt_release_job = self.after(60, self._space_released)

    def _space_released(self):
        self._ptt_release_job = None
        self._ptt_release()

    def _apply_hotkey(self):
        """(Re)install the global hold-to-talk hotkey from the switch + entry."""
        if _kb is None:
            return
        for h in self.hotkey_hooks:
            try:
                _kb.unhook(h)
            except Exception:
                pass
        self.hotkey_hooks = []
        if not self.sw_hotkey.get():
            return
        key = self.ent_hotkey.get().strip().lower() or "f9"
        try:
            self.hotkey_hooks = [
                _kb.on_press_key(key, lambda e: self._hotkey(True)),
                _kb.on_release_key(key, lambda e: self._hotkey(False)),
            ]
        except Exception as e:
            self._msg(f"Hotkey '{key}' failed: {e}")

    def _hotkey(self, down):
        # Called from the keyboard library's thread; Tk is not thread-safe, so
        # only touch plain state here (the poll loop repaints the button).
        if self.sw_latch.get():
            if down and not getattr(self, "_hk_down", False):
                self._set_ptt(not self._ptt_held())
        else:
            self._set_ptt(down)
        self._hk_down = down

    # ------------------------------------------------------------ recording --
    def _toggle_record(self):
        st = self.eng
        if st is None:
            self._msg("Connect first — recording captures the outgoing TX audio.")
            return
        if st.get("tx_wav_request"):
            st["tx_wav_request"] = False
            self.btn_rec.configure(text="Record TX to WAV…")
            self._msg(f"Saved {st.get('tx_wav_path') or 'recording'}")
            return
        folder = filedialog.askdirectory(title="Folder for TX recordings",
                                         initialdir=self.cfg.get("wav_dir") or data_dir())
        if not folder:
            return
        self.cfg["wav_dir"] = folder
        st["tx_wav_dir"] = folder
        st["tx_wav_request"] = True
        self.btn_rec.configure(text="Stop recording")

    # ----------------------------------------------------------------- poll --
    def _msg(self, text):
        self._cfg(self.lbl_msg, text=text)

    def _cfg(self, widget, **kw):
        """configure() only the options that changed. customtkinter redraws a
        widget on every configure(), so skipping no-ops keeps the UI thread cheap."""
        cache = self._cfg_cache.setdefault(widget, {})
        changed = {k: v for k, v in kw.items() if k not in cache or cache[k] != v}
        if changed:
            cache.update(changed)
            widget.configure(**changed)

    def _setbar(self, key, value):
        """Move a meter only when it changed enough to be visible."""
        if abs(value - self._bar_last.get(key, -1.0)) >= 0.012:
            self._bar_last[key] = value
            self.bars[key].set(value)

    def _poll(self):
        while self._todo:
            self._todo.pop(0)()
        st = self.eng
        if st is not None:
            self._setbar("tx", level_frac(st.get("tx_db", -100.0)))
            self._setbar("rx", level_frac(st.get("rx_db", -100.0)))
            _bar, mic_scaled = wac.level_bar(st.get("mic_db", -100.0),
                                             vol_pct=st.get("mic_vol", 100))
            self._setbar("mic", level_frac(mic_scaled))
            ok, bad = "#4caf50", "#e57373"
            self._cfg(self.lbl_tx,
                      text="TX: " + ("connected" if st["tx_connected"] else "connecting…"),
                      text_color=ok if st["tx_connected"] else bad)
            frm = st.get("rx_from")
            self._cfg(self.lbl_rx,
                      text="RX: " + (f"connected ({frm.split(':')[0]})" if st["rx_connected"] and frm else
                                     "connected" if st["rx_connected"] else "waiting for gateway…"),
                      text_color=ok if st["rx_connected"] else bad)
            msg = strip_ansi(st.get("picker_msg") or "")
            err = st.get("tx_error") or st.get("mic_error")
            if err and not msg:
                msg = f"Error: {err}"
            if not msg and st["tx_connected"] and self.lbl_msg.cget("text").startswith("Connecting"):
                msg = "Connected."
            if msg:
                self._msg(msg)
            if not st.get("tx_wav_request") and self.btn_rec.cget("text") == "Stop recording":
                self.btn_rec.configure(text="Record TX to WAV…")
        held = self._ptt_held()
        self._cfg(self.btn_ptt,
                  fg_color="#c62828" if held else "#3b3b3b",
                  hover_color="#d32f2f" if held else "#4a4a4a",
                  text="ON AIR" if held else "HOLD TO TALK  (Space)")
        self._cfg(self.lbl_ver, text=self._version_text())
        if not self._closing:
            self.after(80, self._poll)

    # ---------------------------------------------------------------- stats --
    def _set_probe_target(self, host):
        port = int(self.cfg.get("web_port", wac.DEFAULT_WEB_PORT))
        self._probe_target = (host, port) if host else None

    def _probe_loop(self):
        """Time a TCP connect to the gateway's web port every 2 s. A handshake
        is a fair round-trip measure and needs nothing from the gateway beyond
        the web server it already runs."""
        while not self._probe_stop.is_set():
            target = self._probe_target
            rtt = None
            if target:
                t0 = time.perf_counter()
                try:
                    with socket.create_connection(target, timeout=2):
                        rtt = (time.perf_counter() - t0) * 1000.0
                except OSError:
                    rtt = None
            self._rtt = rtt
            self._probe_stop.wait(2.0)

    def _tick_fast(self):
        """4 Hz: level and loop-time graphs."""
        st = self.eng
        if st is not None:
            self.g_lvl_tx.push(max(-60.0, st.get("tx_db", -100.0)))
            _bar, mic_scaled = wac.level_bar(st.get("mic_db", -100.0),
                                             vol_pct=st.get("mic_vol", 100))
            self.g_lvl_mic.push(max(-60.0, mic_scaled))
            self.g_lvl_rx.push(max(-60.0, st.get("rx_db", -100.0)))
            self.g_loop.push(st.get("tx_iter_ms", 0.0))
        if not self._closing:
            self.after(250, self._tick_fast)

    def _tick_slow(self):
        """1 Hz: throughput, round-trip, CPU/GPU and the text readouts."""
        try:
            self._sample_slow()
        except Exception as e:
            sys.stderr.write(f"[stats] {type(e).__name__}: {e}\n")
        if not self._closing:
            self.after(1000, self._tick_slow)

    def _sample_slow(self):
        st = self.eng
        now = time.monotonic()

        # -- network ------------------------------------------------------------
        if st is not None and self._net_prev is not None:
            t0, tx0, rx0 = self._net_prev
            tx, rx = st.get("tx_bytes", 0), st.get("rx_bytes", 0)
            dt = max(1e-3, now - t0)
            self.g_txk.push((tx - tx0) * 8.0 / dt / 1000.0)
            self.g_rxk.push((rx - rx0) * 8.0 / dt / 1000.0)
            self._net_prev = (now, tx, rx)
        self._rtt_hist.append(self._rtt)
        self.g_rtt.push(self._rtt)
        ok = [r for r in self._rtt_hist if r is not None]
        lost = len(self._rtt_hist) - len(ok)
        jitter = (sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)
                  if len(ok) > 1 else 0.0)
        net = [f"RTT   avg {sum(ok) / len(ok):.1f} ms   jitter {jitter:.1f} ms   "
               f"failed {lost}/{len(self._rtt_hist)}" if ok
               else "RTT   no reply from the gateway web port"]
        if st is not None:
            net.insert(0, f"Sent {fmt_bytes(st.get('tx_bytes', 0))}   "
                          f"received {fmt_bytes(st.get('rx_bytes', 0))}   "
                          f"reconnects TX {max(0, st.get('tx_connects', 0) - 1)}  "
                          f"RX {max(0, st.get('rx_connects', 0) - 1)}")
        self.t_net.configure(text="\n".join(net))

        # -- audio engine -------------------------------------------------------
        if st is not None:
            cb = st.get("tx_cb_stats") or {}

            def rate(key):
                v = st.get(key)
                return f"{v / 1000:.1f} kHz" if v else "-"

            self.t_audio.configure(text="\n".join([
                f"TX feed   capture {rate('tx_capture_rate')} x{st.get('tx_capture_channels', '-')}ch"
                f"  resample {st.get('tx_resample_backend', '-')}"
                f"  block {wac.TX_BLOCK_MS} ms  latency {wac.AUDIO_LATENCY}",
                f"          callbacks {cb.get('callbacks', 0)}  xruns {st.get('tx_xruns', 0)}"
                f"  queue {st.get('tx_q_depth', 0)}  dropped {st.get('tx_q_drops', 0)}"
                f"  send {st.get('tx_send_ms', 0.0):.2f} ms",
                f"Mic       capture {rate('mic_capture_rate')}"
                f"  resample {st.get('mic_resample_backend', '-')}"
                f"  xruns {st.get('mic_xruns', 0)}  dropped {st.get('mic_q_drops', 0)}",
                f"Speaker   stream {rate('rx_stream_rate')}  device {rate('rx_device_rate')}"
                f"  queue {st.get('rx_q_depth', 0)}  from {st.get('rx_from') or '-'}",
            ]))
        else:
            self.t_audio.configure(text="Not connected.")

        # -- CPU ----------------------------------------------------------------
        if self._proc is not None:
            ncpu = psutil.cpu_count() or 1
            self.g_cpu.push(self._proc.cpu_percent(None) / ncpu)
            self.g_sys.push(psutil.cpu_percent(None))
            names = {t.native_id: t.name for t in threading.enumerate()}
            names[threading.main_thread().native_id] = "UI"
            cur = {th.id: th.user_time + th.system_time for th in self._proc.threads()}
            lines = []
            if self._cpu_prev is not None:
                pt, prev = self._cpu_prev
                dt = max(1e-3, now - pt)
                share = {}
                for tid, secs in cur.items():
                    if tid in prev:
                        key = names.get(tid, "audio / other")
                        share[key] = share.get(key, 0.0) + (secs - prev[tid]) / dt * 100.0
                top = sorted(share.items(), key=lambda kv: -kv[1])
                lines = [f"{k} {v:.0f}%" for k, v in top if v >= 0.5][:6]
            self._cpu_prev = (now, cur)
            rss = self._proc.memory_info().rss
            self.t_load.configure(text=(
                f"Memory {fmt_bytes(rss)}   threads {len(cur)}\n"
                f"Busiest threads (% of one core): " + (", ".join(lines) or "idle")))
        else:
            self.t_load.configure(text="CPU stats need the psutil package (pip install psutil).")

        # -- GPU ----------------------------------------------------------------
        if st is not None and st.get("gpu_available"):
            self.g_gpu.push(st.get("gpu_util", 0.0))
            self.t_gpu.configure(text=(
                f"{st.get('gpu_name', 'GPU')}   memory "
                f"{st.get('gpu_mem_used', 0):.0f} / {st.get('gpu_mem_total', 0):.0f} MiB\n"
                "Voice effects run on the CPU, so this normally stays near zero."))
        else:
            self.t_gpu.configure(text="No NVIDIA GPU data (nvidia-smi unavailable). "
                                      "Voice effects run on the CPU anyway.")

    def _version_text(self):
        s = wac.UPDATE_STATE
        status = s.get("status", "unknown")
        label = {"current": "up to date", "updated": "just updated from the gateway",
                 "behind": "update available — restart to apply",
                 "failed": "update check failed: " + s.get("detail", ""),
                 "disabled": "update check disabled", "skipped": "update check skipped",
                 "checking": "checking for updates…"}.get(status, "")
        return f"Version {s.get('version') or '?'} — {label}"

    # --------------------------------------------------------------- update --
    def _startup_update_then_connect(self):
        """Update check first (before any device is opened), then auto-connect."""
        host = self._host0
        web_port = int(self.cfg.get("web_port", wac.DEFAULT_WEB_PORT))
        try:
            if not self.cfg.get("update_enabled", True):
                wac.UPDATE_STATE.update(status="disabled")
            elif "--no-update" in self.argv:
                if os.environ.get(wac.UPDATE_ENV):
                    wac.UPDATE_STATE.update(status="updated")
                else:
                    wac.UPDATE_STATE.update(status="skipped")
                    if host:
                        wac.verify_version_only(host, web_port)
            elif host:
                wac.UPDATE_STATE.update(status="checking")
                if wac.check_for_update(host, web_port):
                    self._todo.append(self._relaunch)
                    return
            wac.UPDATE_STATE["version"] = wac._local_version()
        except Exception as e:
            wac.UPDATE_STATE.update(status="failed", detail=f"{type(e).__name__}: {e}")
        if host and self.cfg.get("auto_connect", True):
            self._todo.append(self._auto_connect)

    def _relaunch(self):
        """Start a fresh copy of ourselves (--no-update so it can't loop) and exit."""
        self._msg("Update installed — restarting …")
        args = [a for a in self.argv if a != "--no-update"] + ["--no-update"]
        env = dict(os.environ)
        env[wac.UPDATE_ENV] = wac.UPDATE_STATE.get("version", "") or wac._local_version()
        if FROZEN:
            cmd = [sys.executable] + args
            # PyInstaller one-file: the child must unpack its own temp dir
            # instead of reusing ours (which is deleted when we exit).
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        else:
            cmd = [sys.executable, os.path.abspath(__file__)] + args
        try:
            subprocess.Popen(cmd, env=env, close_fds=True)
        except Exception as e:
            self._msg(f"Restart failed ({e}) — please start the client again.")
            return
        self.quit_app()

    # ----------------------------------------------------------------- tray --
    def _start_tray(self):
        if pystray is None:
            return
        img = Image.new("RGB", (64, 64), "#1f1f1f")
        d = ImageDraw.Draw(img)
        d.ellipse((12, 12, 52, 52), fill="#2e7d32")
        d.rectangle((28, 22, 36, 38), fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self._todo.append(self._show), default=True),
            pystray.MenuItem("Quit", lambda: self._todo.append(self.quit_app)),
        )
        try:
            self.tray = pystray.Icon(APP_NAME, img, "Radio Gateway Client", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def _show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close_button(self):
        if self.tray is not None:
            self.withdraw()        # keep running in the tray
        else:
            self.quit_app()

    # ----------------------------------------------------------------- exit --
    def _save_cfg(self):
        c = self.cfg
        host = self.host.get().strip()
        if host:                       # remember it even if we never connected
            c["gateway_host"] = host
        c["auto_connect"] = bool(self.sw_auto.get())
        try:
            geo = self.geometry()
            m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
            if m and int(m.group(1)) >= 520 and int(m.group(2)) >= 480:
                c["window_geometry"] = geo     # skip pre-map sizes like 200x200
        except Exception:
            pass
        c["mic_vol"] = self.vals["mic_vol"]
        c["duck_pct"] = self.vals["duck_pct"]
        c["effect_index"] = self.vals["effect_index"]
        c["tx_live"] = self.vals["tx_live"]
        c["ptt_latch"] = bool(self.sw_latch.get())
        c["ptt_hotkey_enabled"] = bool(self.sw_hotkey.get())
        c["ptt_hotkey"] = self.ent_hotkey.get().strip().lower() or "f9"
        try:
            wac.save_config(c)
        except Exception:
            pass

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        self._probe_stop.set()
        try:
            self._save_cfg()
        except Exception:
            pass
        if self.eng is not None:
            self.eng["running"] = False
        for h in self.hotkey_hooks:
            try:
                _kb.unhook(h)
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.after(300, self.destroy)


_MUTEX = None           # held for the life of the process (single-instance lock)
_QUIT_EVENT = None      # named event: signalled by a newer launch to ask us to quit


def take_over():
    """Make this launch the only running copy: quit the others.

    Closing the window only hides it to the tray, so without this every launch
    stacks another copy that fights for the audio devices and the RX port.
    Order: (1) signal the named quit event, which every running copy of this
    version watches, and wait for their lock to free; (2) force-close any
    RadioGatewayClient.exe still alive, which catches older builds that have no
    watcher. Windows only; a no-op elsewhere.
    """
    global _MUTEX, _QUIT_EVENT
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateEventW.restype = ctypes.c_void_p
    for fn in (k32.CloseHandle, k32.SetEvent, k32.ResetEvent):
        fn.argtypes = [ctypes.c_void_p]
    event = k32.CreateEventW(None, True, False, "Local\\RadioGatewayClientGUI.Quit")

    # 1. Ask running copies to quit cleanly, then wait for the lock to free up.
    k32.SetEvent(event)
    deadline = time.monotonic() + 5.0
    while True:
        handle = k32.CreateMutexW(None, False, "Local\\RadioGatewayClientGUI")
        if ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
            _MUTEX = handle
            break
        k32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            break                                   # a stuck copy: step 2 handles it
        time.sleep(0.2)

    # 2. Force-close whatever is left of the packaged exe (never ourselves, nor
    #    the one-file bootstrap process that launched us).
    if FROZEN:
        keep = [os.getpid(), os.getppid()]
        cmd = ["taskkill", "/F", "/IM", os.path.basename(sys.executable)]
        for pid in keep:
            cmd += ["/FI", f"PID ne {pid}"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=15,
                           creationflags=0x08000000)    # CREATE_NO_WINDOW
        except Exception:
            pass
        time.sleep(0.5)                              # let ports and devices free up
    if _MUTEX is None:
        _MUTEX = k32.CreateMutexW(None, False, "Local\\RadioGatewayClientGUI")
    # The event stays signalled while old copies exit; clear it so it doesn't
    # tell *us* to quit, then watch it for the next launch.
    k32.ResetEvent(event)
    _QUIT_EVENT = event


def start_quit_watcher(app):
    """Quit this copy cleanly when a newer launch signals the quit event."""
    if _QUIT_EVENT is None:
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    def watch():
        k32.WaitForSingleObject(_QUIT_EVENT, 0xFFFFFFFF)
        app._todo.append(app.quit_app)               # run on the Tk thread

    threading.Thread(target=watch, daemon=True, name="QuitWatch").start()


def main():
    take_over()
    ensure_streams()
    if FROZEN:
        # Config lives in the per-user data folder, not next to the exe.
        cfg_path = os.path.join(data_dir(), wac.CONFIG_FILENAME)
        wac._config_path = lambda: cfg_path
        if not os.path.exists(cfg_path):
            # First run of the exe: adopt the console client's saved settings
            # (gateway, devices, ...) if its JSON sits beside the exe or up to
            # two folders above it (dist\new\ -> dist\ -> repo root), so the
            # gateway address isn't asked for again.
            folder = os.path.dirname(os.path.abspath(sys.executable))
            for _ in range(3):
                legacy = os.path.join(folder, wac.CONFIG_FILENAME)
                if os.path.isfile(legacy):
                    try:
                        shutil.copyfile(legacy, cfg_path)
                    except OSError:
                        pass
                    break
                folder = os.path.dirname(folder)
    app = App(sys.argv[1:])
    start_quit_watcher(app)
    app.mainloop()


if __name__ == "__main__":
    main()
