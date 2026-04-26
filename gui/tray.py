"""
System tray icon + autostart helper (HKCU\\...\\Run via winreg).
"""

import os
import sys
from typing import Optional

from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from core import paths
from core.icon_extractor import get_app_icon

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "MonitorColorSwitcher"


def _autostart_command() -> str:
    """Build absolute command line for HKCU\\...\\Run."""
    py = os.path.abspath(sys.executable)
    # Prefer pythonw.exe so autostart doesn't pop a console window.
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        py = pyw
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if script and script.lower().endswith(".py"):
        return f'"{py}" "{script}" --minimized'
    # frozen / bundled exe case
    return f'"{py}" --minimized'


def is_autostart_enabled() -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


class TrayIcon(QObject):
    show_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, app: QApplication, parent: Optional[QObject] = None):
        super().__init__(parent)
        icon: QIcon = get_app_icon(paths.assets_dir())

        self._tray = QSystemTrayIcon(icon, parent=self)
        self._tray.setToolTip("Monitor Color Switcher")

        menu = QMenu()
        action_open = QAction("Открыть", menu)
        action_open.triggered.connect(self.show_requested.emit)
        menu.addAction(action_open)

        self._action_autostart = QAction("Автозапуск", menu)
        self._action_autostart.setCheckable(True)
        self._action_autostart.setChecked(is_autostart_enabled())
        self._action_autostart.toggled.connect(self._on_autostart_toggled)
        menu.addAction(self._action_autostart)

        menu.addSeparator()
        action_quit = QAction("Выйти", menu)
        action_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(action_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_requested.emit()

    def _on_autostart_toggled(self, checked: bool):
        ok = set_autostart(checked)
        if not ok:
            # roll back UI state if registry write failed
            self._action_autostart.blockSignals(True)
            self._action_autostart.setChecked(not checked)
            self._action_autostart.blockSignals(False)

    def show_message(self, title: str, body: str) -> None:
        self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 3000)

    def show_message_with_icon(self, title: str, body: str, icon: QIcon) -> None:
        """Like show_message, but uses a custom QIcon (e.g. game icon)."""
        if icon is None or icon.isNull():
            self.show_message(title, body)
            return
        # The QIcon overload of showMessage shows the icon as the balloon image.
        self._tray.showMessage(title, body, icon, 4000)
