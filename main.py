"""
Entry point.

Wires together: ColorManager, ProfileStorage, ProcessWatcher, MainWindow, TrayIcon.
The QApplication is set to NOT quit on last window closed - main window hides to tray.
"""

import os
import sys

from PyQt6.QtCore import QAbstractNativeEventFilter
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from core import paths
from core.color_manager import (
    ColorManager,
    capture_gamma_ramps,
    release_persistent_hdc,
)
from core.icon_extractor import get_app_icon, get_game_icon
from core.process_watcher import ProcessWatcher, _is_foreground_exclusive_fullscreen
from core.profile_storage import ProfileStorage
from gui.main_window import MainWindow
from gui.tray import TrayIcon


# Win32 messages we care about.
_WM_DISPLAYCHANGE = 0x007E
_WM_POWERBROADCAST = 0x0218
_PBT_APMRESUMESUSPEND = 0x0007
_PBT_APMRESUMEAUTOMATIC = 0x0012


class _DisplayPowerFilter(QAbstractNativeEventFilter):
    """
    Listens for WM_DISPLAYCHANGE (monitor hot-plug, resolution change) and
    WM_POWERBROADCAST resume notifications. Both can leave persistent display
    DCs stale or undo our applied gamma ramp, so we hand off to a callback
    that rebuilds DCs and re-applies the active profile.
    """

    def __init__(self, on_event):
        super().__init__()
        self._on_event = on_event

    def nativeEventFilter(self, eventType, message):
        if eventType not in (b"windows_generic_MSG", "windows_generic_MSG"):
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        except Exception:
            return False, 0
        if msg.message == _WM_DISPLAYCHANGE:
            self._on_event("display_change")
        elif msg.message == _WM_POWERBROADCAST:
            if int(msg.wParam) in (_PBT_APMRESUMESUSPEND, _PBT_APMRESUMEAUTOMATIC):
                self._on_event("resume")
        return False, 0


_APP_USER_MODEL_ID = "bomsh.MonitorColorSwitcher.1"
_SINGLE_INSTANCE_MUTEX = "Local\\bomsh.MonitorColorSwitcher.SingleInstance"


def _acquire_single_instance_lock() -> bool:
    """
    Take a Windows named mutex so a second copy of the app (autostart + manual
    launch, double-click while already in tray) can detect the existing one
    and exit. Returns True if WE are the first instance, False if another is
    already running.

    The mutex handle is intentionally leaked into the process — Windows
    releases it on process termination, including crashes, so this is
    self-healing.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        h = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        if not h:
            # Couldn't create the mutex at all — fall through, don't gate startup.
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        return True




def _set_app_user_model_id() -> None:
    """
    Tell Windows this process is its own app, not a Python interpreter instance.
    Without this, taskbar groups the window under python.exe / pythonw.exe and
    shows the Python icon instead of our QApplication.windowIcon.
    Must run BEFORE the first window is shown.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass  # non-fatal; we just keep the default Python AppID


def main() -> int:
    _set_app_user_model_id()

    if not _acquire_single_instance_lock():
        # Another instance is already running — exit silently so autostart +
        # manual launch don't end up fighting over profiles.json and DCs.
        # We could ping the existing instance to bring its window forward,
        # but that needs IPC; for now silent-exit is the safe default.
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Monitor Color Switcher")
    app.setWindowIcon(get_app_icon(paths.assets_dir()))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "Ошибка", "Системный трей недоступен.")
        return 1

    color_manager = ColorManager()
    storage = ProfileStorage(paths.profiles_path())
    watcher = ProcessWatcher(interval_ms=2500)
    window = MainWindow(
        storage=storage,
        color_manager=color_manager,
        active_process_provider=watcher.active_process,
    )
    tray = TrayIcon(app)

    def _desktop_profile():
        for p in storage.load():
            if p.is_desktop:
                return p
        return None

    def _apply_desktop():
        """Apply the desktop profile if it exists; otherwise restore captured ramp."""
        dp = _desktop_profile()
        if dp is None:
            color_manager.reset()
            return None
        color_manager.apply(
            dp.vibrance, dp.brightness, dp.contrast, dp.gamma, dp.black_lift
        )
        return dp

    # Wire process watcher to color manager
    def on_process_started(_proc_name: str, profile):
        ok = color_manager.apply(
            profile.vibrance, profile.brightness, profile.contrast, profile.gamma,
            profile.black_lift,
        )
        if ok:
            # Heuristic: if the just-started game took exclusive fullscreen,
            # warn that the gamma ramp probably isn't visible inside the game.
            in_fullscreen = _is_foreground_exclusive_fullscreen()
            if in_fullscreen:
                window.notify_status(
                    f"Активен профиль «{profile.name}» (но игра в полноэкранном "
                    "режиме — фильтр виден только в windowed/borderless)"
                )
                tray.show_message_with_icon(
                    "Monitor Color Switcher",
                    f"Применён профиль: {profile.name}\n"
                    "В fullscreen-играх эффект может быть невиден — попробуй borderless.",
                    get_game_icon(profile.exe_path),
                )
            else:
                window.notify_status(f"Активен профиль «{profile.name}»")
                tray.show_message_with_icon(
                    "Monitor Color Switcher",
                    f"Применён профиль: {profile.name}",
                    get_game_icon(profile.exe_path),
                )
        else:
            window.notify_status(f"Не удалось применить профиль «{profile.name}»")
            tray.show_message(
                "Monitor Color Switcher",
                f"Не удалось применить профиль: {profile.name}",
            )

    def on_process_stopped(_proc_name: str):
        dp = _apply_desktop()
        if dp is not None:
            window.notify_status(f"Активен профиль «{dp.name}»")
            tray.show_message("Monitor Color Switcher", f"Применён профиль: {dp.name}")
        else:
            window.notify_status("Профиль сброшен (процесс завершён)")
            tray.show_message("Monitor Color Switcher", "Цвет сброшен к дефолту")

    watcher.process_started.connect(on_process_started)
    watcher.process_stopped.connect(on_process_stopped)

    # Initial profile set + react to edits in the UI
    watcher.set_profiles(storage.load())
    window.profiles_changed.connect(watcher.set_profiles)

    # Apply the desktop profile right away — that's the baseline color when no
    # game is running. The watcher will swap to a game profile if it sees one.
    _apply_desktop()

    # Tray actions
    def show_window():
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def quit_app():
        watcher.stop()
        color_manager.shutdown()
        app.quit()

    def graceful_shutdown():
        """Runs on any QApplication exit path (Logout, Ctrl+C, last-window, etc.)
        to make sure the gamma ramp gets restored. Idempotent."""
        try:
            watcher.stop()
        except Exception:
            pass
        try:
            color_manager.shutdown()
        except Exception:
            pass

    app.aboutToQuit.connect(graceful_shutdown)

    # Re-apply currently active profile after monitor hot-plug or system resume.
    def on_native_event(kind: str):
        # Rebuild DC list and recapture ramps before re-applying. Order matters:
        # rebuild first so the new active-monitor set is known.
        try:
            color_manager.rebuild_for_display_change()
        except Exception:
            return
        # Re-apply: if a game profile is currently active, prefer it; else desktop.
        active_proc = watcher.active_process()
        if active_proc:
            for p in storage.load():
                if p.process and p.process.lower() == active_proc:
                    color_manager.apply(
                        p.vibrance, p.brightness, p.contrast, p.gamma, p.black_lift
                    )
                    return
        _apply_desktop()

    _native_filter = _DisplayPowerFilter(on_native_event)
    app.installNativeEventFilter(_native_filter)
    # Keep a strong reference on QApplication so it survives until quit.
    app._native_filter = _native_filter  # type: ignore[attr-defined]

    tray.show_requested.connect(show_window)
    tray.quit_requested.connect(quit_app)

    # If launched via autostart, start hidden in tray
    started_minimized = "--minimized" in sys.argv
    if not started_minimized:
        window.show()

    watcher.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
