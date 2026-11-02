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
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

import windows_audio_client as wac

try:
    import keyboard as _kb          # global hold-to-talk hotkey (optional)
except Exception:
    _kb = None

try:
    import pystray                  # tray icon (optional)
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

FROZEN = bool(getattr(sys, "frozen", False))
APP_NAME = "RadioGatewayClient"
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
        self.geometry(f"560x{h}+40+20")
        self.minsize(520, 480)

        self.argv = argv
        self.cfg = wac.load_config()
        self.eng = None
        self.threads = []
        self.tray = None
        self.hotkey_hooks = []
        self._ptt_release_job = None
        self._closing = False
        self._todo = []            # callables queued by other threads, run by _poll
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

        self._build()
        self._load_devices()
        self._apply_hotkey()
        self._start_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close_button)
        self.bind_all("<KeyPress-space>", self._space_down)
        self.bind_all("<KeyRelease-space>", self._space_up)
        self._host0 = self.host.get().strip()
        self.after(80, self._poll)
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

    def _build(self):
        # Footer is packed first so it stays visible; the controls scroll above it.
        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.pack(side="bottom", fill="x")
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(side="top", fill="both", expand=True)

        # --- connection ------------------------------------------------------
        box = self._section("Gateway")
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 4))
        self.host = ctk.CTkEntry(r, placeholder_text="gateway IP or hostname")
        host = (self.argv[0] if self.argv and not self.argv[0].startswith("--")
                else self.cfg.get("gateway_host", ""))
        self.host.insert(0, host)
        self.host.pack(side="left", fill="x", expand=True)
        self.btn_connect = ctk.CTkButton(r, text="Connect", width=110, command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=(8, 0))
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 8))
        self.lbl_tx = ctk.CTkLabel(r, text="TX: offline", anchor="w")
        self.lbl_tx.pack(side="left", padx=(0, 18))
        self.lbl_rx = ctk.CTkLabel(r, text="RX: offline", anchor="w")
        self.lbl_rx.pack(side="left")

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
            cur = menu.get() if menu.get() in names else None
            menu.set(cur or (saved[kind] if saved[kind] in names else names[0]))

    def _rescan(self):
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
        self._msg("Device list refreshed.")

    def _device_chosen(self, kind, name):
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

        cfg = self.cfg
        wac.AUDIO_LATENCY = cfg.get("audio_latency", wac.AUDIO_LATENCY)
        try:
            wac.TX_BLOCK_MS = max(5, int(cfg.get("tx_block_ms", wac.TX_BLOCK_MS)))
        except (TypeError, ValueError):
            pass
        tx_port = cfg.get("tx_port", wac.DEFAULT_TX_PORT)
        rx_port = cfg.get("rx_port", wac.DEFAULT_RX_PORT)
        cfg.update(gateway_host=host, tx_port=tx_port, rx_port=rx_port,
                   tx_device_name=tx_n, rx_device_name=out_n, mic_device_name=mic_n)
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
        self.lbl_msg.configure(text=text)

    def _poll(self):
        while self._todo:
            self._todo.pop(0)()
        st = self.eng
        if st is not None:
            self.bars["tx"].set(level_frac(st.get("tx_db", -100.0)))
            self.bars["rx"].set(level_frac(st.get("rx_db", -100.0)))
            _bar, mic_scaled = wac.level_bar(st.get("mic_db", -100.0),
                                             vol_pct=st.get("mic_vol", 100))
            self.bars["mic"].set(level_frac(mic_scaled))
            ok, bad = "#4caf50", "#e57373"
            self.lbl_tx.configure(text="TX: " + ("connected" if st["tx_connected"] else "connecting…"),
                                  text_color=ok if st["tx_connected"] else bad)
            frm = st.get("rx_from")
            self.lbl_rx.configure(
                text="RX: " + (f"connected ({frm[0]})" if st["rx_connected"] and frm else
                               "connected" if st["rx_connected"] else "waiting for gateway…"),
                text_color=ok if st["rx_connected"] else bad)
            msg = strip_ansi(st.get("picker_msg") or "")
            err = st.get("tx_error") or st.get("mic_error")
            if err and not msg:
                msg = f"Error: {err}"
            if msg:
                self._msg(msg)
            if not st.get("tx_wav_request") and self.btn_rec.cget("text") == "Stop recording":
                self.btn_rec.configure(text="Record TX to WAV…")
        held = self._ptt_held()
        self.btn_ptt.configure(
            fg_color="#c62828" if held else "#3b3b3b",
            hover_color="#d32f2f" if held else "#4a4a4a",
            text="ON AIR" if held else "HOLD TO TALK  (Space)")
        self.lbl_ver.configure(text=self._version_text())
        if not self._closing:
            self.after(80, self._poll)

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
            self._todo.append(self.connect)

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


def main():
    ensure_streams()
    if FROZEN:
        # Config lives beside the engine files, in the per-user data folder.
        wac._config_path = lambda: os.path.join(data_dir(), wac.CONFIG_FILENAME)
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()
