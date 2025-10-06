#!/usr/bin/env python3
import argparse
import math
import os
import re
import signal
import sys
import threading
import time
from typing import Optional

try:
    from evdev import InputDevice, UInput, ecodes, list_devices
except Exception as exc:  # pragma: no cover
    print("This tool requires the 'evdev' package. Install with: pip install evdev", file=sys.stderr)
    raise


def is_writable_uinput() -> bool:
    return os.access("/dev/uinput", os.W_OK) or os.access("/dev/input/uinput", os.W_OK)


def find_controller_device(name_regex: Optional[str]) -> Optional[str]:
    devices = [InputDevice(path) for path in list_devices()]

    candidates = []
    for dev in devices:
        try:
            caps = dev.capabilities(verbose=False)
        except Exception:
            continue
        has_r2_axis = ecodes.EV_ABS in caps and ecodes.ABS_RZ in [code for code, _ in caps.get(ecodes.EV_ABS, [])]
        has_r2_button = ecodes.EV_KEY in caps and ecodes.BTN_TR2 in [code for code, _ in caps.get(ecodes.EV_KEY, [])]
        name_ok = True
        if name_regex:
            try:
                name_ok = re.search(name_regex, dev.name or "", flags=re.IGNORECASE) is not None
            except re.error:
                name_ok = name_regex.lower() in (dev.name or "").lower()
        if (has_r2_axis or has_r2_button) and name_ok:
            candidates.append((dev.path, dev.name or ""))

    # Prefer devices with ABS_RZ first
    for path, name in candidates:
        try:
            caps = InputDevice(path).capabilities(verbose=False)
        except Exception:
            continue
        if ecodes.EV_ABS in caps and ecodes.ABS_RZ in [code for code, _ in caps.get(ecodes.EV_ABS, [])]:
            return path

    return candidates[0][0] if candidates else None


class JitterMouse:
    def __init__(self, radius_px: float, events_per_second: float, rotations_per_second: float, debug: bool = False) -> None:
        if events_per_second <= 0:
            raise ValueError("events_per_second must be > 0")
        if rotations_per_second < 0:
            raise ValueError("rotations_per_second must be >= 0")
        self.radius = float(radius_px)
        self.hz = float(events_per_second)
        self.rps = float(rotations_per_second)
        self.debug = debug

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active_flag = False
        self._active_lock = threading.Lock()

        # Create a virtual relative mouse
        self.uinput = UInput({
            ecodes.EV_REL: (ecodes.REL_X, ecodes.REL_Y),
            ecodes.EV_KEY: (ecodes.BTN_LEFT, ecodes.BTN_RIGHT),
        }, name="r2-mouse-jitter", bustype=ecodes.BUS_USB)

    def set_active(self, active: bool) -> None:
        with self._active_lock:
            self._active_flag = active

    def _is_active(self) -> bool:
        with self._active_lock:
            return self._active_flag

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="MouseJitter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        try:
            self.uinput.close()
        except Exception:
            pass

    def _run(self) -> None:
        step_seconds = 1.0 / self.hz
        # Angle step per tick for target rotations per second
        angle_step = 2.0 * math.pi * (self.rps / self.hz)
        angle = 0.0
        last_cos = math.cos(angle)
        last_sin = math.sin(angle)
        res_x = 0.0
        res_y = 0.0
        last_tick = time.perf_counter()

        while not self._stop_event.is_set():
            # Sleep precisely to maintain rate
            now = time.perf_counter()
            sleep_time = (last_tick + step_seconds) - now
            if sleep_time > 0:
                time.sleep(sleep_time)
                now = time.perf_counter()
            last_tick = now

            if not self._is_active():
                # When inactive, keep advancing angle to avoid jumps when re-enabled
                angle = (angle + angle_step) % (2.0 * math.pi)
                last_cos = math.cos(angle)
                last_sin = math.sin(angle)
                continue

            # Compute ideal delta along circle
            angle = (angle + angle_step) % (2.0 * math.pi)
            cur_cos = math.cos(angle)
            cur_sin = math.sin(angle)

            dx_float = self.radius * (cur_cos - last_cos)
            dy_float = self.radius * (cur_sin - last_sin)

            last_cos = cur_cos
            last_sin = cur_sin

            # Accumulate fractional motion to maintain smoothness with integer rel events
            res_x += dx_float
            res_y += dy_float
            dx = int(round(res_x))
            dy = int(round(res_y))
            res_x -= dx
            res_y -= dy

            if dx != 0 or dy != 0:
                self.uinput.write(ecodes.EV_REL, ecodes.REL_X, dx)
                self.uinput.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                self.uinput.syn()

                if self.debug:
                    print(f"rel: dx={dx} dy={dy}")


class R2Monitor:
    def __init__(self, device_path: str, threshold: float, toggle_mode: bool, jitter: JitterMouse, debug: bool = False) -> None:
        self.device_path = device_path
        self.threshold = max(0.0, min(1.0, threshold))
        self.toggle_mode = toggle_mode
        self.jitter = jitter
        self.debug = debug

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="R2Monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        pressed_prev = False
        toggled = False
        while not self._stop_event.is_set():
            try:
                dev = InputDevice(self.device_path)
            except Exception as exc:
                if self.debug:
                    print(f"Waiting for device {self.device_path}: {exc}")
                time.sleep(1.0)
                continue

            absinfo = None
            try:
                absinfo = dev.absinfo(ecodes.ABS_RZ)
            except Exception:
                absinfo = None

            if self.debug:
                print(f"Monitoring R2 on {dev.path} ({dev.name}), threshold={self.threshold}")

            try:
                for event in dev.read_loop():
                    if self._stop_event.is_set():
                        break

                    if event.type == ecodes.EV_ABS and event.code == ecodes.ABS_RZ:
                        # Normalize
                        if absinfo is None:
                            try:
                                absinfo = dev.absinfo(ecodes.ABS_RZ)
                            except Exception:
                                absinfo = None
                        if absinfo is not None and absinfo.max > absinfo.min:
                            val_norm = (event.value - absinfo.min) / float(absinfo.max - absinfo.min)
                        else:
                            # Fallback range 0..255
                            val_norm = min(1.0, max(0.0, event.value / 255.0))

                        pressed = val_norm >= self.threshold

                    elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TR2:
                        pressed = event.value != 0
                    else:
                        continue

                    if self.toggle_mode:
                        if pressed and not pressed_prev:
                            toggled = not toggled
                            if self.debug:
                                print(f"Toggle -> {'ON' if toggled else 'OFF'}")
                            self.jitter.set_active(toggled)
                    else:
                        self.jitter.set_active(pressed)

                    pressed_prev = pressed
            except OSError:
                # Device disconnected; retry loop
                if self.debug:
                    print("Device disconnected, retrying in 1s...")
                time.sleep(1.0)
            except Exception as exc:
                if self.debug:
                    print(f"Monitor error: {exc}")
                time.sleep(0.5)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jitter mouse in a tight circle while PS5 R2 is pressed.")
    parser.add_argument("--device", help="/dev/input/eventX path for controller (auto-detect if omitted)")
    parser.add_argument("--name-regex", default="DualSense|Wireless Controller|Sony|PlayStation|PS5", help="Regex to pick controller by name during auto-detect")
    parser.add_argument("--radius", type=float, default=4.0, help="Circle radius in pixels (2-6 recommended)")
    parser.add_argument("--hz", type=float, default=500.0, help="Event rate (movements per second)")
    parser.add_argument("--rps", type=float, default=80.0, help="Rotations per second of the circle")
    parser.add_argument("--threshold", type=float, default=0.15, help="R2 analog press threshold (0..1)")
    parser.add_argument("--toggle", action="store_true", help="Toggle mode (press R2 to toggle jitter ON/OFF)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not is_writable_uinput():
        print("/dev/uinput is not writable. Run with sudo or add user to 'input' group.", file=sys.stderr)
        return 1

    device_path = args.device or find_controller_device(args.name_regex)
    if not device_path:
        print("Could not find a suitable controller device. Use --device /dev/input/eventX.", file=sys.stderr)
        return 2

    if args.debug:
        print(f"Using controller device: {device_path}")

    jitter = JitterMouse(radius_px=args.radius, events_per_second=args.hz, rotations_per_second=args.rps, debug=args.debug)
    r2 = R2Monitor(device_path=device_path, threshold=args.threshold, toggle_mode=args.toggle, jitter=jitter, debug=args.debug)

    jitter.start()
    r2.start()

    # Graceful shutdown on SIGINT/SIGTERM
    stop_event = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
    finally:
        r2.stop()
        jitter.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
