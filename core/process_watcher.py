"""
Watches running processes via psutil and emits signals when a profile-tracked
process starts or stops. Polls on a QTimer in the GUI thread (cheap iteration,
no separate thread needed for 2-3 sec interval).
"""

import ctypes
import sys
from ctypes import wintypes
from typing import Dict, Iterable, Optional, Set

import psutil
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from models.profile import GameProfile


def _is_foreground_exclusive_fullscreen() -> bool:
    """
    Heuristic: True if the foreground window covers its entire monitor without
    any borders/titlebar (likely Direct3D exclusive fullscreen or borderless).

    Used to warn the user that GDI gamma ramp will not be visible inside the
    game even though SetDeviceGammaRamp returned success — Direct3D apps in
    exclusive fullscreen own their own gamma path.

    Caveat: borderless windowed games look identical to exclusive fullscreen
    by these checks, so we can't perfectly distinguish them. We err on the
    side of warning when in doubt — borderless usually means DWM still
    composites and ramp DOES work, but the user can confirm by toggling.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False

        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return False

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False

        # Window covers entire monitor?
        if (rect.left, rect.top, rect.right, rect.bottom) != (
            mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom,
        ):
            return False
        return True
    except OSError:
        return False


class ProcessWatcher(QObject):
    # Emitted when the first matching process appears (process_name, profile)
    process_started = pyqtSignal(str, object)
    # Emitted when the last currently-tracked process disappears
    process_stopped = pyqtSignal(str)

    def __init__(self, interval_ms: int = 2500, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)
        self._profiles_by_proc: Dict[str, GameProfile] = {}
        self._active_proc: Optional[str] = None  # currently applied profile's process name

    def set_profiles(self, profiles: Iterable[GameProfile]) -> None:
        """
        Replace the watched-process map. Lower-cased process names as keys.

        If the currently active profile is no longer in the new map (deleted
        or had its process renamed), emit process_stopped immediately so the
        ColorManager has a chance to revert / re-apply the desktop profile,
        instead of leaving the screen tinted until the user closes the game.
        """
        new_map: Dict[str, GameProfile] = {
            p.process.lower(): p for p in profiles if p.process
        }
        # Detect "active profile vanished" before swapping the map.
        if self._active_proc is not None and self._active_proc not in new_map:
            stopped = self._active_proc
            self._active_proc = None
            self._profiles_by_proc = new_map
            self.process_stopped.emit(stopped)
            return
        self._profiles_by_proc = new_map

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
            # immediate first check so we don't wait `interval_ms`
            self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def active_process(self) -> Optional[str]:
        return self._active_proc

    def _tick(self) -> None:
        if not self._profiles_by_proc:
            if self._active_proc is not None:
                stopped = self._active_proc
                self._active_proc = None
                self.process_stopped.emit(stopped)
            return

        running: Set[str] = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not name:
                continue
            running.add(name.lower())

        if self._active_proc is not None:
            if self._active_proc in running:
                return  # still active, nothing to do
            stopped = self._active_proc
            self._active_proc = None
            self.process_stopped.emit(stopped)

        # Try to find a new active process (first match wins, by dict insertion order)
        for proc_name, profile in self._profiles_by_proc.items():
            if proc_name in running:
                self._active_proc = proc_name
                self.process_started.emit(proc_name, profile)
                return
