#!/usr/bin/env python3
"""Run commands by tapping the laptop: side (left/right) + tap count."""
import array
import json
import math
import queue
import subprocess
import sys
import threading
from pathlib import Path

CONFIG = Path(__file__).with_name("config.json")
RATE = 48000
BLOCK = RATE // 100  # 10 ms of frames
REFRACTORY = 8       # blocks to ignore after a tap (80 ms)
GROUP_END = 60       # blocks of quiet that end a tap group (600 ms)
WARMUP = 50          # blocks to learn the noise floor before detecting (mic starts silent)
CAL_TAPS = 5

DEFAULT = {
    "device": "plughw:0,6",
    "sensitivity": 6.0,
    "calibration": {"left": 0.4, "right": -0.4},
    "bindings": [
        {"side": "right", "taps": 2, "command": "firefox"},
        {"side": "left", "taps": 2, "command": "gnome-terminal"},
    ],
}


def load_config():
    if CONFIG.exists():
        return {**DEFAULT, **json.loads(CONFIG.read_text())}
    save_config(DEFAULT)
    return json.loads(json.dumps(DEFAULT))


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def side_of(value, cal):
    mid = (cal["left"] + cal["right"]) / 2
    return "left" if (value - mid) * (cal["left"] - cal["right"]) > 0 else "right"


def dispatch(cfg, side, count):
    for b in cfg["bindings"]:
        if b["side"] == side and int(b["taps"]) == count:
            subprocess.Popen(b["command"], shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return b["command"]


class Listener(threading.Thread):
    """Reads stereo audio from arecord and posts events to `events`:
    ("tap", lr_value), ("pattern", side, count), ("error", message)."""

    def __init__(self, cfg, events):
        super().__init__(daemon=True)
        self.cfg, self.events = cfg, events
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        cmd = ["arecord", "-q", "-D", self.cfg["device"], "-c", "2", "-r", str(RATE),
               "-f", "S16_LE", "-t", "raw"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.events.put(("error", "arecord not found (install alsa-utils)"))
            return
        floor, block_no, last_tap, taps = None, 0, -10**9, []
        try:
            while self.running:
                data = proc.stdout.read(BLOCK * 4)
                if len(data) < BLOCK * 4:
                    self.events.put(("error", proc.stderr.read().decode() or "audio stream ended"))
                    return
                samples = array.array("h", data)
                left = math.sqrt(sum(x * x for x in samples[0::2]) / BLOCK) + 1
                right = math.sqrt(sum(x * x for x in samples[1::2]) / BLOCK) + 1
                level = left + right
                block_no += 1
                sens = self.cfg["sensitivity"]
                if floor is None:
                    floor = level
                if block_no > WARMUP and level > floor * sens and block_no - last_tap > REFRACTORY:
                    last_tap = block_no
                    value = math.log(left / right)
                    taps.append(value)
                    self.events.put(("tap", value))
                w = 0.1 if block_no <= WARMUP else 0.01
                floor = (1 - w) * floor + w * min(level, floor * sens)
                if taps and block_no - last_tap > GROUP_END:
                    cal = self.cfg["calibration"]
                    lefts = sum(side_of(v, cal) == "left" for v in taps)
                    side = "left" if lefts * 2 > len(taps) else "right"
                    self.events.put(("pattern", side, len(taps)))
                    taps = []
        finally:
            proc.terminate()


def headless():
    cfg, events = load_config(), queue.Queue()
    Listener(cfg, events).start()
    print("Listening... Ctrl+C to quit")
    while True:
        ev = events.get()
        if ev[0] == "tap":
            print(f"  tap  L/R={ev[1]:+.3f}")
        elif ev[0] == "pattern":
            ran = dispatch(cfg, ev[1], ev[2])
            print(f"{ev[1]} x{ev[2]}" + (f" -> {ran}" if ran else ""))
        else:
            sys.exit(ev[1])


def gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg, events = load_config(), queue.Queue()
    state = {"listener": None, "cal": None, "cal_values": {"left": [], "right": []}}

    root = tk.Tk()
    root.title("Tapper")

    tree = ttk.Treeview(root, columns=("side", "taps", "command"), show="headings", height=8)
    for col, width in (("side", 70), ("taps", 50), ("command", 320)):
        tree.heading(col, text=col.capitalize())
        tree.column(col, width=width)
    tree.grid(row=0, column=0, columnspan=6, sticky="nsew", padx=6, pady=6)

    def refresh():
        tree.delete(*tree.get_children())
        for i, b in enumerate(cfg["bindings"]):
            tree.insert("", "end", iid=str(i), values=(b["side"], b["taps"], b["command"]))

    def edit_dialog(binding=None):
        win = tk.Toplevel(root)
        win.title("Binding")
        side = tk.StringVar(value=binding["side"] if binding else "right")
        taps = tk.IntVar(value=binding["taps"] if binding else 2)
        command = tk.StringVar(value=binding["command"] if binding else "")
        ttk.Label(win, text="Side").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(win, textvariable=side, values=("left", "right"), state="readonly",
                     width=8).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(win, text="Taps").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Spinbox(win, from_=1, to=9, textvariable=taps, width=5).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(win, text="Command").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(win, textvariable=command, width=40).grid(row=2, column=1, padx=6)

        def ok():
            new = {"side": side.get(), "taps": taps.get(), "command": command.get().strip()}
            if not new["command"]:
                return
            if binding:
                binding.update(new)
            else:
                cfg["bindings"].append(new)
            save_config(cfg)
            refresh()
            win.destroy()

        ttk.Button(win, text="OK", command=ok).grid(row=3, column=1, sticky="e", padx=6, pady=6)
        win.transient(root)
        win.grab_set()

    def selected():
        sel = tree.selection()
        return cfg["bindings"][int(sel[0])] if sel else None

    def edit():
        if selected():
            edit_dialog(selected())

    def delete():
        if selected():
            cfg["bindings"].remove(selected())
            save_config(cfg)
            refresh()

    def start():
        if not state["listener"]:
            state["listener"] = Listener(cfg, events)
            state["listener"].start()
            toggle.config(text="Stop")
            status.set("Listening...")

    def stop():
        if state["listener"]:
            state["listener"].stop()
            state["listener"] = None
            toggle.config(text="Start")
            status.set("Stopped")

    def calibrate():
        start()
        state["cal"] = "left"
        state["cal_values"] = {"left": [], "right": []}
        status.set(f"Calibrating: tap the LEFT side {CAL_TAPS} times (slowly)")

    def on_tap(value):
        mode = state["cal"]
        if not mode:
            status.set(f"tap  L/R={value:+.3f}  ({side_of(value, cfg['calibration'])})")
            return
        values = state["cal_values"][mode]
        values.append(value)
        if len(values) < CAL_TAPS:
            status.set(f"Calibrating {mode.upper()}: {len(values)}/{CAL_TAPS}")
        elif mode == "left":
            state["cal"] = "right"
            status.set(f"Calibrating: now tap the RIGHT side {CAL_TAPS} times (slowly)")
        else:
            state["cal"] = None
            cal = {s: sum(v) / len(v) for s, v in state["cal_values"].items()}
            cfg["calibration"] = cal
            save_config(cfg)
            status.set(f"Calibrated: left={cal['left']:+.3f} right={cal['right']:+.3f}")

    def poll():
        while not events.empty():
            ev = events.get()
            if ev[0] == "tap":
                on_tap(ev[1])
            elif ev[0] == "pattern" and not state["cal"]:
                ran = dispatch(cfg, ev[1], ev[2])
                status.set(f"{ev[1]} x{ev[2]}" + (f"  ->  {ran}" if ran else "  (no binding)"))
            elif ev[0] == "error":
                stop()
                messagebox.showerror("Tapper", ev[1])
        root.after(50, poll)

    def set_sensitivity(value):
        cfg["sensitivity"] = round(float(value), 1)
        save_config(cfg)

    ttk.Button(root, text="Add", command=edit_dialog).grid(row=1, column=0, padx=3)
    ttk.Button(root, text="Edit", command=edit).grid(row=1, column=1, padx=3)
    ttk.Button(root, text="Delete", command=delete).grid(row=1, column=2, padx=3)
    toggle = ttk.Button(root, text="Start", command=lambda: stop() if state["listener"] else start())
    toggle.grid(row=1, column=3, padx=3)
    ttk.Button(root, text="Calibrate", command=calibrate).grid(row=1, column=4, padx=3)

    ttk.Label(root, text="Sensitivity (higher = needs louder tap)").grid(row=2, column=0, columnspan=3, sticky="w", padx=6)
    scale = tk.Scale(root, from_=2, to=30, resolution=0.5, orient="horizontal",
                     command=set_sensitivity, length=200)
    scale.set(cfg["sensitivity"])
    scale.grid(row=2, column=3, columnspan=3, sticky="w")

    status = tk.StringVar(value="Stopped")
    ttk.Label(root, textvariable=status).grid(row=3, column=0, columnspan=6, sticky="w", padx=6, pady=6)

    root.columnconfigure(5, weight=1)
    root.rowconfigure(0, weight=1)
    refresh()
    start()
    poll()
    root.mainloop()


if __name__ == "__main__":
    headless() if "--headless" in sys.argv else gui()
