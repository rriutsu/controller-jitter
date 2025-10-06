#!/usr/bin/env python3
import ctypes
import math
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

# ----- Windows APIs -----
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
winmm = ctypes.WinDLL('winmm', use_last_error=True)

# timeBeginPeriod/timeEndPeriod for better sleep precision
winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
winmm.timeBeginPeriod.restype = ctypes.c_uint
winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
winmm.timeEndPeriod.restype = ctypes.c_uint

def _set_timer_resolution(ms: int) -> None:
    winmm.timeBeginPeriod(ms)

def _reset_timer_resolution(ms: int) -> None:
    winmm.timeEndPeriod(ms)

# SendInput structures
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.c_void_p),
    )

class INPUT(ctypes.Structure):
    class _INPUT_UNION(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT),)
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_uint), ("u", _INPUT_UNION))

user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


def send_mouse_rel(dx: int, dy: int) -> None:
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        err = ctypes.get_last_error()
        # Avoid spamming UI; errors can occur if desktop/session focus changes
        # print(f"SendInput failed: {err}")
        _ = err

# XInput
_xinput = None
for dll_name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll", "xinput1_2.dll", "xinput1_1.dll"):
    try:
        _xinput = ctypes.WinDLL(dll_name)
        break
    except OSError:
        _xinput = None
if _xinput is None:
    raise OSError("No XInput DLL found. Install/update Xbox Accessories/XInput runtime.")

WORD = ctypes.c_ushort
BYTE = ctypes.c_ubyte
SHORT = ctypes.c_short
DWORD = ctypes.c_uint

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = (
        ("wButtons", WORD),
        ("bLeftTrigger", BYTE),
        ("bRightTrigger", BYTE),
        ("sThumbLX", SHORT),
        ("sThumbLY", SHORT),
        ("sThumbRX", SHORT),
        ("sThumbRY", SHORT),
    )

class XINPUT_STATE(ctypes.Structure):
    _fields_ = (("dwPacketNumber", DWORD), ("Gamepad", XINPUT_GAMEPAD))

_xinput.XInputGetState.argtypes = [DWORD, ctypes.POINTER(XINPUT_STATE)]
_xinput.XInputGetState.restype = DWORD

ERROR_SUCCESS = 0


def xinput_get_r2_value(index: int) -> Optional[int]:
    state = XINPUT_STATE()
    res = _xinput.XInputGetState(DWORD(index), ctypes.byref(state))
    if res != ERROR_SUCCESS:
        return None
    return int(state.Gamepad.bRightTrigger)


class JitterMouse:
    def __init__(self, radius_px: float, events_per_second: float, rotations_per_second: float) -> None:
        if events_per_second <= 0:
            raise ValueError("events_per_second must be > 0")
        if rotations_per_second < 0:
            raise ValueError("rotations_per_second must be >= 0")
        self._radius = float(radius_px)
        self._hz = float(events_per_second)
        self._rps = float(rotations_per_second)

        self._active = False
        self._active_lock = threading.Lock()
        self._param_lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_active(self, active: bool) -> None:
        with self._active_lock:
            self._active = active

    def update_params(self, radius_px: Optional[float] = None, events_per_second: Optional[float] = None, rotations_per_second: Optional[float] = None) -> None:
        with self._param_lock:
            if radius_px is not None:
                self._radius = float(radius_px)
            if events_per_second is not None and events_per_second > 0:
                self._hz = float(events_per_second)
            if rotations_per_second is not None and rotations_per_second >= 0:
                self._rps = float(rotations_per_second)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MouseJitter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        # Improve sleep precision
        _set_timer_resolution(1)
        try:
            angle = 0.0
            last_cos = math.cos(angle)
            last_sin = math.sin(angle)
            residual_x = 0.0
            residual_y = 0.0
            last_tick = time.perf_counter()

            while not self._stop.is_set():
                # Snapshot params each loop
                with self._param_lock:
                    hz = self._hz
                    rps = self._rps
                    radius = self._radius

                step_seconds = 1.0 / hz
                angle_step = 2.0 * math.pi * (rps / hz)

                now = time.perf_counter()
                sleep_time = (last_tick + step_seconds) - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.perf_counter()
                last_tick = now

                with self._active_lock:
                    active = self._active

                # Always advance angle to avoid jumps when re-activating
                angle = (angle + angle_step) % (2.0 * math.pi)
                cur_cos = math.cos(angle)
                cur_sin = math.sin(angle)

                if not active:
                    last_cos = cur_cos
                    last_sin = cur_sin
                    continue

                dx_f = radius * (cur_cos - last_cos)
                dy_f = radius * (cur_sin - last_sin)
                last_cos = cur_cos
                last_sin = cur_sin

                residual_x += dx_f
                residual_y += dy_f
                dx = int(round(residual_x))
                dy = int(round(residual_y))
                residual_x -= dx
                residual_y -= dy

                if dx != 0 or dy != 0:
                    send_mouse_rel(dx, dy)
        finally:
            _reset_timer_resolution(1)


class R2Monitor:
    def __init__(self, controller_index: int, threshold: int, toggle_mode: bool, jitter: JitterMouse, ui_callback=None) -> None:
        self.index = controller_index
        self.threshold = max(0, min(255, int(threshold)))
        self.toggle_mode = toggle_mode
        self.jitter = jitter
        self.ui_callback = ui_callback  # function(active: bool)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="R2Monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        pressed_prev = False
        toggled = False
        while not self._stop.is_set():
            val = xinput_get_r2_value(self.index)
            if val is None:
                # Not connected or error; back off a bit
                time.sleep(0.1)
                if self.ui_callback:
                    self.ui_callback(False)
                self.jitter.set_active(False)
                continue

            pressed = val >= self.threshold
            if self.toggle_mode:
                if pressed and not pressed_prev:
                    toggled = not toggled
                    self.jitter.set_active(toggled)
                    if self.ui_callback:
                        self.ui_callback(toggled)
            else:
                self.jitter.set_active(pressed)
                if self.ui_callback:
                    self.ui_callback(pressed)

            pressed_prev = pressed
            time.sleep(0.002)  # ~500 Hz polling


class JitterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("R2 Mouse Jitter (Windows)")
        self.resizable(False, False)

        # State
        self.jitter = JitterMouse(radius_px=4.0, events_per_second=500.0, rotations_per_second=80.0)
        self.monitor: Optional[R2Monitor] = None
        self.running = False

        # UI elements
        main = ttk.Frame(self, padding=10)
        main.grid(row=0, column=0, sticky="nsew")

        row = 0
        ttk.Label(main, text="Controller Index (0-3)").grid(row=row, column=0, sticky="w")
        self.index_var = tk.IntVar(value=0)
        self.index_spin = ttk.Spinbox(main, from_=0, to=3, textvariable=self.index_var, width=5)
        self.index_spin.grid(row=row, column=1, sticky="e")
        row += 1

        ttk.Label(main, text="Strength (radius pixels)").grid(row=row, column=0, sticky="w")
        self.radius_var = tk.DoubleVar(value=4.0)
        self.radius_scale = ttk.Scale(main, from_=1.0, to=10.0, variable=self.radius_var, orient=tk.HORIZONTAL)
        self.radius_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Smoothness (events/sec)").grid(row=row, column=0, sticky="w")
        self.hz_var = tk.DoubleVar(value=500.0)
        self.hz_scale = ttk.Scale(main, from_=100.0, to=1000.0, variable=self.hz_var, orient=tk.HORIZONTAL)
        self.hz_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Rotation (rotations/sec)").grid(row=row, column=0, sticky="w")
        self.rps_var = tk.DoubleVar(value=80.0)
        self.rps_scale = ttk.Scale(main, from_=10.0, to=200.0, variable=self.rps_var, orient=tk.HORIZONTAL)
        self.rps_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="R2 Threshold (0-255)").grid(row=row, column=0, sticky="w")
        self.thresh_var = tk.IntVar(value=40)
        self.thresh_scale = ttk.Scale(main, from_=1, to=255, variable=self.thresh_var, orient=tk.HORIZONTAL)
        self.thresh_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        self.toggle_var = tk.BooleanVar(value=False)
        self.toggle_chk = ttk.Checkbutton(main, text="Toggle mode (press to toggle)", variable=self.toggle_var)
        self.toggle_chk.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.active_label = ttk.Label(main, text="Status: Idle", foreground="gray")
        self.active_label.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.start_btn = ttk.Button(btn_frame, text="Start", command=self.on_start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.apply_btn = ttk.Button(btn_frame, text="Apply Settings", command=self.on_apply)
        self.apply_btn.pack(side=tk.RIGHT)

        self.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_apply(self) -> None:
        try:
            self.jitter.update_params(radius_px=self.radius_var.get(), events_per_second=self.hz_var.get(), rotations_per_second=self.rps_var.get())
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self.active_label.configure(text="Settings applied", foreground="gray")

    def on_start(self) -> None:
        if self.running:
            return
        self.on_apply()
        self.jitter.start()
        self.monitor = R2Monitor(
            controller_index=int(self.index_var.get()),
            threshold=int(self.thresh_var.get()),
            toggle_mode=bool(self.toggle_var.get()),
            jitter=self.jitter,
            ui_callback=self._update_active_ui,
        )
        self.monitor.start()
        self.running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.active_label.configure(text="Status: Running (hold R2)", foreground="green")

    def on_stop(self) -> None:
        if not self.running:
            return
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.jitter.set_active(False)
        self.jitter.stop()
        self.running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.active_label.configure(text="Status: Stopped", foreground="gray")

    def _update_active_ui(self, active: bool) -> None:
        # This is called from a background thread; schedule to main thread
        def _apply():
            if not self.running:
                return
            self.active_label.configure(text=f"Status: {'ACTIVE' if active else 'Running'}", foreground=("red" if active else "green"))
        self.after(0, _apply)

    def on_close(self) -> None:
        try:
            self.on_stop()
        finally:
            self.destroy()


def main() -> int:
    app = JitterApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
