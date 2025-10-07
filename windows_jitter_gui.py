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

# Pre-allocate a reusable INPUT structure to reduce per-event allocations
_GLOBAL_INPUT = INPUT(type=INPUT_MOUSE)

def send_mouse_rel(dx: int, dy: int) -> None:
    mi = _GLOBAL_INPUT.mi
    mi.dx = dx
    mi.dy = dy
    mi.mouseData = 0
    mi.dwFlags = MOUSEEVENTF_MOVE
    mi.time = 0
    mi.dwExtraInfo = None
    sent = user32.SendInput(1, ctypes.byref(_GLOBAL_INPUT), ctypes.sizeof(INPUT))
    if sent != 1:
        err = ctypes.get_last_error()
        # Avoid spamming UI; errors can occur if desktop/session focus changes
        # print(f"SendInput failed: {err}")
        _ = err

# Priority helpers
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.GetCurrentThread.restype = ctypes.c_void_p
kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel32.SetPriorityClass.restype = ctypes.c_int
kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
kernel32.SetThreadPriority.restype = ctypes.c_int

HIGH_PRIORITY_CLASS = 0x00000080
THREAD_PRIORITY_HIGHEST = 2

def _boost_priorities() -> None:
    h_proc = kernel32.GetCurrentProcess()
    if h_proc:
        kernel32.SetPriorityClass(h_proc, HIGH_PRIORITY_CLASS)
    h_thread = kernel32.GetCurrentThread()
    if h_thread:
        kernel32.SetThreadPriority(h_thread, THREAD_PRIORITY_HIGHEST)

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
    def __init__(self, radius_px: float, events_per_second: float, rotations_per_second: float, performance_mode: bool = False) -> None:
        if events_per_second <= 0:
            raise ValueError("events_per_second must be > 0")
        if rotations_per_second < 0:
            raise ValueError("rotations_per_second must be >= 0")
        self._radius = float(radius_px)
        self._hz = float(events_per_second)
        self._rps = float(rotations_per_second)
        self._performance_mode = bool(performance_mode)
        # Pull (recoil) in pixels per second along X (right +) and Y (down +)
        self._pull_dx_per_sec = 0.0
        self._pull_dy_per_sec = 0.0

        self._active = False
        self._active_lock = threading.Lock()
        self._param_lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_active(self, active: bool) -> None:
        with self._active_lock:
            self._active = active

    def set_performance_mode(self, enabled: bool) -> None:
        self._performance_mode = bool(enabled)

    def update_pull(self, magnitude_px_per_sec: float, angle_deg: float) -> None:
        # 0° = right, 90° = down, 180° = left, 270° = up
        rad = math.radians(angle_deg)
        self._pull_dx_per_sec = float(magnitude_px_per_sec) * math.cos(rad)
        self._pull_dy_per_sec = float(magnitude_px_per_sec) * math.sin(rad)

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
        # Optional: boost process and thread priority for lower latency
        if self._performance_mode:
            try:
                _boost_priorities()
            except Exception:
                pass
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

                target = last_tick + step_seconds
                now = time.perf_counter()
                remaining = target - now
                if remaining > 0:
                    if self._performance_mode and remaining > 0.0006:
                        # Sleep most of the time, then busy-wait for the final few hundred microseconds
                        time.sleep(remaining - 0.0004)
                        while True:
                            now = time.perf_counter()
                            if now >= target:
                                break
                    else:
                        # Regular precise sleep
                        time.sleep(remaining)
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

                # Circular component
                dx_f = radius * (cur_cos - last_cos)
                dy_f = radius * (cur_sin - last_sin)
                last_cos = cur_cos
                last_sin = cur_sin

                # Recoil pull component (constant drift per second)
                drift_x = self._pull_dx_per_sec * step_seconds
                drift_y = self._pull_dy_per_sec * step_seconds

                residual_x += dx_f + drift_x
                residual_y += dy_f + drift_y
                dx = int(round(residual_x))
                dy = int(round(residual_y))
                residual_x -= dx
                residual_y -= dy

                if dx != 0 or dy != 0:
                    send_mouse_rel(dx, dy)
        finally:
            _reset_timer_resolution(1)


class R2Monitor:
    def __init__(self, controller_index: int, threshold: int, toggle_mode: bool, jitter: JitterMouse, ui_callback=None, backend: str = "auto", sdl_axis_index: int = -1, poll_interval_s: float = 0.002) -> None:
        self.index = int(controller_index)
        self.threshold = max(0, min(255, int(threshold)))
        self.toggle_mode = bool(toggle_mode)
        self.jitter = jitter
        self.ui_callback = ui_callback  # function(active: bool)
        self.backend = backend.lower() if backend else "auto"
        self.sdl_axis_index = int(sdl_axis_index)
        self.poll_interval_s = float(max(0.001, poll_interval_s))

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

    def _signal_ui(self, active: bool, backend_label: Optional[str] = None, analog_01: Optional[float] = None) -> None:
        if not self.ui_callback:
            return
        label = backend_label or ""
        # Notify UI with active state, backend and analog value if supported
        try:
            self.ui_callback(active, label, analog_01)
        except TypeError:
            # Backward compat: older UI callback with single arg
            try:
                self.ui_callback(active, label)
            except TypeError:
                self.ui_callback(active)

    def _run(self) -> None:
        mode = self.backend
        if mode == "xinput":
            self._run_xinput()
            return
        if mode == "sdl":
            self._run_sdl()
            return

        # auto: try XInput first, then fallback to SDL if not available
        trial_deadline = time.perf_counter() + 2.0  # 2 seconds trial
        had_value = False
        while not self._stop.is_set() and time.perf_counter() < trial_deadline:
            val = xinput_get_r2_value(self.index)
            if val is not None:
                had_value = True
                break
            time.sleep(0.02)
        if had_value:
            self._run_xinput()
        else:
            self._run_sdl()

    def _run_xinput(self) -> None:
        pressed_prev = False
        toggled = False
        # Inform UI that backend is XInput
        self._signal_ui(False, backend_label="XInput", analog_01=0.0)
        while not self._stop.is_set():
            val = xinput_get_r2_value(self.index)
            if val is None:
                # Not connected or error; back off a bit
                time.sleep(0.05)
                self.jitter.set_active(False)
                self._signal_ui(False, backend_label="XInput", analog_01=0.0)
                continue

            v01 = max(0.0, min(1.0, val / 255.0))
            pressed = val >= self.threshold
            if self.toggle_mode:
                if pressed and not pressed_prev:
                    toggled = not toggled
                    self.jitter.set_active(toggled)
                    self._signal_ui(toggled, backend_label="XInput", analog_01=v01)
            else:
                self.jitter.set_active(pressed)
                self._signal_ui(pressed, backend_label="XInput", analog_01=v01)

            pressed_prev = pressed
            time.sleep(self.poll_interval_s)

    def _run_sdl(self) -> None:
        # Lazy import to avoid dependency unless needed
        try:
            import pygame  # type: ignore
        except Exception:
            # Cannot fallback; keep inactive
            while not self._stop.is_set():
                self.jitter.set_active(False)
                time.sleep(0.25)
            return

        pygame.init()
        pygame.joystick.init()
        last_ok = False
        pressed_prev = False
        toggled = False

        def ensure_joystick():
            if pygame.joystick.get_count() <= self.index:
                return None
            js = pygame.joystick.Joystick(self.index)
            js.init()
            return js

        js = ensure_joystick()
        self._signal_ui(False, backend_label="SDL", analog_01=0.0)
        while not self._stop.is_set():
            if js is None:
                pygame.joystick.quit()
                pygame.joystick.init()
                js = ensure_joystick()
                last_ok = False
                time.sleep(0.25)
                continue

            try:
                pygame.event.pump()
                # Determine axis if auto (-1)
                axis = self.sdl_axis_index
                if axis < 0:
                    # Calibrate for ~1.5s; ask user to press R2 during this time
                    axes_count = js.get_numaxes()
                    baseline = [0.0] * axes_count
                    scores = [0.0] * axes_count
                    for i in range(axes_count):
                        try:
                            baseline[i] = float(js.get_axis(i))
                        except Exception:
                            baseline[i] = 0.0
                    start = time.perf_counter()
                    while (time.perf_counter() - start) < 1.5 and not self._stop.is_set():
                        pygame.event.pump()
                        for i in range(axes_count):
                            try:
                                v = float(js.get_axis(i))
                            except Exception:
                                v = baseline[i]
                            d = abs(v - baseline[i])
                            if d > scores[i]:
                                scores[i] = d
                        time.sleep(0.01)
                    try:
                        best_axis = max(range(axes_count), key=lambda i: scores[i])
                        if scores[best_axis] > 0.2:
                            axis = best_axis
                            self.sdl_axis_index = best_axis
                        else:
                            # fallback if movement too small
                            axis = 5 if axes_count > 5 else max(0, axes_count - 1)
                            self.sdl_axis_index = axis
                    except ValueError:
                        axis = 5 if axes_count > 5 else 0
                        self.sdl_axis_index = axis

                # Axis value typically in [-1, 1] where -1 is unpressed; some drivers give 0..1
                val = float(js.get_axis(axis))
                # Normalize to 0..1
                if -1.001 <= val <= 1.001:
                    v01 = (val + 1.0) * 0.5
                else:
                    # already 0..1 or unusual; clamp
                    v01 = max(0.0, min(1.0, val))
                pressed = v01 >= (self.threshold / 255.0)
                if self.toggle_mode:
                    if pressed and not pressed_prev:
                        toggled = not toggled
                        self.jitter.set_active(toggled)
                        self._signal_ui(toggled, backend_label=f"SDL axis {self.sdl_axis_index}", analog_01=v01)
                else:
                    self.jitter.set_active(pressed)
                    self._signal_ui(pressed, backend_label=f"SDL axis {self.sdl_axis_index}", analog_01=v01)
                pressed_prev = pressed
                last_ok = True
                time.sleep(self.poll_interval_s)
            except Exception:
                # device may have been disconnected or axis invalid; reset
                last_ok = False
                self.jitter.set_active(False)
                self._signal_ui(False, backend_label="SDL", analog_01=0.0)
                time.sleep(0.25)


class JitterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("R2 Mouse Jitter (Windows)")
        self.resizable(False, False)

        # State
        self.jitter = JitterMouse(radius_px=4.0, events_per_second=500.0, rotations_per_second=80.0)
        self.monitor: Optional[R2Monitor] = None
        self.running = False
        self.accent = "#6C63FF"  # Accent color
        self.bg_dark = "#1E1E2E"
        self.bg_mid = "#2A2A3C"
        self.fg_text = "#E6E6F0"

        # Global style
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        self.configure(bg=self.bg_dark)
        style.configure('TFrame', background=self.bg_mid)
        style.configure('TLabel', background=self.bg_mid, foreground=self.fg_text, font=("Segoe UI", 10))
        style.configure('Header.TLabel', background=self.bg_dark, foreground='white', font=("Segoe UI Semibold", 14))
        style.configure('Accent.TButton', font=("Segoe UI Semibold", 10))
        style.map('Accent.TButton', background=[('active', self.accent)], foreground=[('active', 'white')])
        style.configure('TCheckbutton', background=self.bg_mid, foreground=self.fg_text)
        style.configure('Horizontal.TScale', background=self.bg_mid)

        # UI elements
        # Header with gradient
        header = tk.Canvas(self, height=64, highlightthickness=0, bg=self.bg_dark)
        header.grid(row=0, column=0, sticky="ew")
        self._draw_gradient(header, self.bg_dark, self.accent)
        header.bind("<Configure>", lambda e: self._draw_gradient(header, self.bg_dark, self.accent))
        header_lbl = ttk.Label(self, text="R2 Mouse Jitter", style='Header.TLabel')
        header_lbl.place(x=16, y=16)

        main = ttk.Frame(self, padding=12, style='TFrame')
        main.grid(row=1, column=0, sticky="nsew")

        row = 0
        ttk.Label(main, text="Controller Index (0-3)").grid(row=row, column=0, sticky="w")
        self.index_var = tk.IntVar(value=0)
        self.index_spin = ttk.Spinbox(main, from_=0, to=3, textvariable=self.index_var, width=5)
        self.index_spin.grid(row=row, column=1, sticky="e")
        row += 1

        ttk.Label(main, text="Backend").grid(row=row, column=0, sticky="w")
        self.backend_var = tk.StringVar(value="Auto")
        self.backend_combo = ttk.Combobox(main, textvariable=self.backend_var, values=["Auto", "XInput", "SDL"], state="readonly", width=10)
        self.backend_combo.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="SDL Axis (-1=Auto)").grid(row=row, column=0, sticky="w")
        self.sdl_axis_var = tk.IntVar(value=-1)
        self.sdl_axis_spin = ttk.Spinbox(main, from_=-1, to=15, textvariable=self.sdl_axis_var, width=5)
        self.sdl_axis_spin.grid(row=row, column=1, sticky="e")
        row += 1

        ttk.Label(main, text="Strength (radius pixels)").grid(row=row, column=0, sticky="w")
        self.radius_var = tk.DoubleVar(value=4.0)
        self.radius_scale = ttk.Scale(main, from_=1.0, to=10.0, variable=self.radius_var, orient=tk.HORIZONTAL)
        self.radius_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Smoothness (events/sec)").grid(row=row, column=0, sticky="w")
        self.hz_var = tk.DoubleVar(value=500.0)
        self.hz_scale = ttk.Scale(main, from_=300.0, to=2000.0, variable=self.hz_var, orient=tk.HORIZONTAL)
        self.hz_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Rotation (rotations/sec)").grid(row=row, column=0, sticky="w")
        self.rps_var = tk.DoubleVar(value=100.0)
        self.rps_scale = ttk.Scale(main, from_=20.0, to=240.0, variable=self.rps_var, orient=tk.HORIZONTAL)
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

        # Recoil pull controls (down-right)
        ttk.Label(main, text="Recoil Pull Speed (px/sec)").grid(row=row, column=0, sticky="w")
        self.pull_speed_var = tk.DoubleVar(value=60.0)
        self.pull_speed_scale = ttk.Scale(main, from_=0.0, to=200.0, variable=self.pull_speed_var, orient=tk.HORIZONTAL)
        self.pull_speed_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Pull Angle (deg, 0=right, 90=down)").grid(row=row, column=0, sticky="w")
        self.pull_angle_var = tk.DoubleVar(value=35.0)
        self.pull_angle_scale = ttk.Scale(main, from_=0.0, to=180.0, variable=self.pull_angle_var, orient=tk.HORIZONTAL)
        self.pull_angle_scale.grid(row=row, column=1, sticky="ew")
        row += 1

        self.perf_var = tk.BooleanVar(value=False)
        self.perf_chk = ttk.Checkbutton(main, text="Performance Mode (higher priority, faster polling)", variable=self.perf_var)
        self.perf_chk.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Label(main, text="R2 Level").grid(row=row, column=0, sticky="w")
        self.r2_level = ttk.Progressbar(main, orient=tk.HORIZONTAL, length=180, mode='determinate', maximum=100)
        self.r2_level.grid(row=row, column=1, sticky="ew")
        row += 1

        self.active_label = ttk.Label(main, text="Status: Idle", foreground="gray")
        self.active_label.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.start_btn = ttk.Button(btn_frame, text="Start", command=self.on_start, style='Accent.TButton')
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
            self.jitter.set_performance_mode(self.perf_var.get())
            self.jitter.update_pull(magnitude_px_per_sec=self.pull_speed_var.get(), angle_deg=self.pull_angle_var.get())
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self.active_label.configure(text="Settings applied", foreground="gray")

    def on_start(self) -> None:
        if self.running:
            return
        self.on_apply()
        self.jitter.start()
        backend_choice = self.backend_var.get().lower()
        if backend_choice not in ("auto", "xinput", "sdl"):
            backend_choice = "auto"

        poll = 0.001 if self.perf_var.get() else 0.002

        self.monitor = R2Monitor(
            controller_index=int(self.index_var.get()),
            threshold=int(self.thresh_var.get()),
            toggle_mode=bool(self.toggle_var.get()),
            jitter=self.jitter,
            ui_callback=self._update_active_ui,
            backend=backend_choice,  # try XInput first, then fallback SDL
            sdl_axis_index=int(self.sdl_axis_var.get()),
            poll_interval_s=poll,
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

    def _update_active_ui(self, active: bool, backend_label: str = "", analog: Optional[float] = None) -> None:
        # This is called from a background thread; schedule to main thread
        def _apply():
            if not self.running:
                return
            suffix = f" [{backend_label}]" if backend_label else ""
            self.active_label.configure(text=f"Status: {'ACTIVE' if active else 'Running'}{suffix}", foreground=("red" if active else "green"))
            if analog is not None:
                self.r2_level['value'] = int(round(analog * 100))
        self.after(0, _apply)

    def on_close(self) -> None:
        try:
            self.on_stop()
        finally:
            self.destroy()

    def _draw_gradient(self, canvas: tk.Canvas, color1: str, color2: str) -> None:
        canvas.delete("grad")
        width = canvas.winfo_width() or 600
        height = canvas.winfo_height() or 64
        # Simple horizontal gradient
        steps = max(1, width)
        r1, g1, b1 = self.winfo_rgb(color1)
        r2, g2, b2 = self.winfo_rgb(color2)
        for i in range(steps):
            r = int(r1 + (r2 - r1) * i / steps)
            g = int(g1 + (g2 - g1) * i / steps)
            b = int(b1 + (b2 - b1) * i / steps)
            hex_color = f"#{r>>8:02x}{g>>8:02x}{b>>8:02x}"
            canvas.create_line(i, 0, i, height, tags=("grad",), fill=hex_color)


def main() -> int:
    app = JitterApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
