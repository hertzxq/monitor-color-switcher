"""
Main application window.

Left pane:  list of profiles + add / delete buttons.
Right pane: editor (name, process .exe, four sliders).
Bottom:     "Apply now" / "Reset" buttons + status label.

Closing the window hides it to tray (handled in main.py via the close event).
"""

import os
from typing import List, Optional

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core import paths
from core.color_manager import ColorManager
from core.icon_extractor import get_app_icon, get_game_icon
from core.profile_storage import ProfileStorage
from models.profile import GameProfile


class _IntSliderRow(QWidget):
    """Slider + SpinBox bound together, integer values."""

    valueChanged = pyqtSignal(int)

    def __init__(self, label: str, minimum: int, maximum: int, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(label)
        self._label.setMinimumWidth(110)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(minimum, maximum)
        self._spin = QSpinBox()
        self._spin.setRange(minimum, maximum)
        self._spin.setMinimumWidth(70)

        self._slider.valueChanged.connect(self._spin.setValue)
        self._spin.valueChanged.connect(self._slider.setValue)
        self._slider.valueChanged.connect(self.valueChanged.emit)

        layout.addWidget(self._label)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._spin)

    def value(self) -> int:
        return self._slider.value()

    def setValue(self, v: int) -> None:
        self._slider.setValue(v)


class _FloatSliderRow(QWidget):
    """Slider with integer steps that maps to a float via a divisor."""

    valueChanged = pyqtSignal(float)

    def __init__(self, label: str, fmin: float, fmax: float, divisor: int = 100, parent=None):
        super().__init__(parent)
        self._divisor = divisor
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(label)
        self._label.setMinimumWidth(110)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(int(fmin * divisor), int(fmax * divisor))
        self._spin = QDoubleSpinBox()
        self._spin.setRange(fmin, fmax)
        self._spin.setDecimals(2)
        self._spin.setSingleStep(0.05)
        self._spin.setMinimumWidth(80)

        self._slider.valueChanged.connect(self._on_slider)
        self._spin.valueChanged.connect(self._on_spin)

        layout.addWidget(self._label)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._spin)

    def _on_slider(self, v: int):
        f = v / self._divisor
        self._spin.blockSignals(True)
        self._spin.setValue(f)
        self._spin.blockSignals(False)
        self.valueChanged.emit(f)

    def _on_spin(self, f: float):
        self._slider.blockSignals(True)
        self._slider.setValue(int(round(f * self._divisor)))
        self._slider.blockSignals(False)
        self.valueChanged.emit(f)

    def value(self) -> float:
        return self._spin.value()

    def setValue(self, f: float) -> None:
        self._spin.setValue(f)


class MainWindow(QMainWindow):
    profiles_changed = pyqtSignal(list)  # emits List[GameProfile]
    quit_requested = pyqtSignal()

    def __init__(
        self,
        storage: ProfileStorage,
        color_manager: ColorManager,
        parent: Optional[QWidget] = None,
        active_process_provider=None,
    ):
        """
        active_process_provider: optional callable returning the lower-cased
        name of the currently watched/active game process, or None if no game
        is active. Used to decide whether slider tweaks should live-apply.
        """
        super().__init__(parent)
        self.setWindowTitle("Monitor Color Switcher")
        self.setWindowIcon(get_app_icon(paths.assets_dir()))
        self.resize(820, 480)

        self._storage = storage
        self._color = color_manager
        self._active_process_provider = active_process_provider
        self._profiles: List[GameProfile] = self._storage.load()
        self._loading = False  # guard while populating UI from a profile

        # Live preview: instead of applying on every slider tick (which would
        # hammer GDI 50+ times per drag), debounce a single apply().
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(80)
        self._live_timer.timeout.connect(self._do_live_apply)

        # Ensure there is exactly one desktop profile, and it lives at the top
        # of the list. This is the profile applied when no game is running.
        self._ensure_desktop_profile()

        self._build_ui()
        self._refresh_list(select_index=0 if self._profiles else None)

        warnings = []
        if not self._color.nvapi_available:
            warnings.append(
                f"NvAPI: {self._color.nvapi_error or 'unknown'}.\n"
                "→ Управление насыщенностью отключено."
            )
        elif not self._color.vibrance_writable:
            warnings.append(
                "NvAPI: драйвер NVIDIA отклоняет non-zero DVC через обе функции "
                "(legacy SetDVCLevel и SetDVCLevelEx).\n"
                "→ Сброс насыщенности к нейтральной работает, повышение — нет.\n"
                "Известный регресс на драйверах 5xx+. Для повышения насыщенности "
                "используй NVIDIA Control Panel вручную."
            )
        if not self._color.gamma_supported:
            warnings.append(
                "SetDeviceGammaRamp игнорируется системой — яркость / контраст / гамма "
                "работать НЕ будут.\n\n"
                "Что проверить (в таком порядке):\n"
                "  1. Параметры → Дисплей → HDR — отключить.\n"
                "  2. Параметры → Дисплей → Управление цветом (Auto Color Management) — отключить.\n"
                "  3. Параметры → Дисплей → Ночной свет — отключить.\n"
                "  4. Параметры → Дисплей → Расширенный → Дополнительные параметры → "
                "Управление цветом → снять активный ICC-профиль.\n"
                "  5. NVIDIA Control Panel → Display → Adjust desktop color settings → "
                "выбрать «Use NVIDIA settings» (а не «Other application»).\n\n"
                "Только насыщенность через NvAPI продолжит работать."
            )
        if warnings:
            QMessageBox.warning(self, "Доступность функций", "\n\n".join(warnings))

    # ----- UI construction -----

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 560])
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self._btn_apply = QPushButton("Применить сейчас")
        self._btn_apply.clicked.connect(self._on_apply_clicked)
        self._btn_reset = QPushButton("Сбросить")
        self._btn_reset.clicked.connect(self._on_reset_clicked)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #888;")
        bottom.addWidget(self._btn_apply)
        bottom.addWidget(self._btn_reset)
        bottom.addStretch(1)
        bottom.addWidget(self._status)
        root.addLayout(bottom)

    def _build_left_pane(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 6, 0)

        layout.addWidget(QLabel("Профили"))
        self._list = QListWidget()
        self._list.setIconSize(QSize(32, 32))
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        btn_add = QPushButton("Добавить")
        btn_add.clicked.connect(self._on_add)
        self._btn_delete = QPushButton("Удалить")
        self._btn_delete.clicked.connect(self._on_delete)
        row.addWidget(btn_add)
        row.addWidget(self._btn_delete)
        layout.addLayout(row)
        return w

    def _build_right_pane(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 0, 0, 0)

        # name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Имя"))
        self._ed_name = QLineEdit()
        self._ed_name.editingFinished.connect(self._on_name_changed)
        name_row.addWidget(self._ed_name, 1)
        layout.addLayout(name_row)

        # process .exe
        proc_row = QHBoxLayout()
        proc_row.addWidget(QLabel("Процесс"))
        self._ed_process = QLineEdit()
        self._ed_process.setPlaceholderText("cs2.exe")
        self._ed_process.editingFinished.connect(self._on_process_changed)
        self._btn_browse = QPushButton("Обзор…")
        self._btn_browse.clicked.connect(self._on_browse_exe)
        proc_row.addWidget(self._ed_process, 1)
        proc_row.addWidget(self._btn_browse)
        layout.addLayout(proc_row)

        # sliders
        self._sl_vibrance = _IntSliderRow("Насыщенность", 0, 100)
        self._sl_brightness = _FloatSliderRow("Яркость", 0.3, 2.0)
        self._sl_contrast = _FloatSliderRow("Контраст", 0.3, 2.0)
        self._sl_gamma = _FloatSliderRow("Гамма", 0.3, 3.0)
        # Black lift = "shadow boost". Range 0..0.5 with finer step (1/200).
        self._sl_black_lift = _FloatSliderRow("Тени", 0.0, 0.5, divisor=200)
        self._sl_black_lift.setToolTip(
            "Поднимает только тёмные пиксели, не трогая средние и светлые.\n"
            "Полезно чтобы видеть игроков в тёмных углах. 0 = выключено."
        )

        for row in (
            self._sl_vibrance, self._sl_brightness, self._sl_contrast,
            self._sl_gamma, self._sl_black_lift,
        ):
            layout.addWidget(row)

        self._sl_vibrance.valueChanged.connect(lambda v: self._on_field_changed("vibrance", v))
        self._sl_brightness.valueChanged.connect(lambda v: self._on_field_changed("brightness", v))
        self._sl_contrast.valueChanged.connect(lambda v: self._on_field_changed("contrast", v))
        self._sl_gamma.valueChanged.connect(lambda v: self._on_field_changed("gamma", v))
        self._sl_black_lift.valueChanged.connect(lambda v: self._on_field_changed("black_lift", v))

        layout.addStretch(1)
        return w

    # ----- list / profile state -----

    def _ensure_desktop_profile(self) -> None:
        """
        Make sure there is exactly one desktop profile, sitting at index 0.

        - On first run (no profiles or no desktop) — create one.
        - If multiple profiles have is_desktop=True (corrupted state from
          hand-edited JSON), keep the first one as the singleton and CLEAR
          is_desktop=False on the others instead of deleting them. The
          original code dropped extras silently, which lost user data.
        """
        desktops = [p for p in self._profiles if p.is_desktop]
        if not desktops:
            self._profiles.insert(0, GameProfile(
                name="Рабочий стол",
                process="",
                exe_path="",
                vibrance=50,
                brightness=1.0,
                contrast=1.0,
                gamma=1.0,
                is_desktop=True,
            ))
            self._storage.save(self._profiles)
            return

        # The first one we found becomes the canonical desktop.
        desktop = desktops[0]
        # Demote any extras to regular game-style profiles. They may not have
        # a process bound — that's fine, the watcher just ignores them.
        for extra in desktops[1:]:
            extra.is_desktop = False

        # Move desktop to index 0 without dropping anything.
        self._profiles = [desktop] + [p for p in self._profiles if p is not desktop]

        # The desktop singleton must NOT have a process binding, so the watcher
        # never matches it. If the user somehow set one, clear it but keep the
        # rest of its settings.
        if desktop.process or desktop.exe_path:
            desktop.process = ""
            desktop.exe_path = ""

        self._storage.save(self._profiles)

    def _refresh_list(self, select_index: Optional[int] = None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._profiles:
            if p.is_desktop:
                label = p.name
            else:
                label = f"{p.name}  ({p.process})" if p.process else p.name
            item = QListWidgetItem(label)
            icon = get_game_icon(p.exe_path) if not p.is_desktop else QIcon()
            if not icon.isNull():
                item.setIcon(icon)
            self._list.addItem(item)
        self._list.blockSignals(False)

        if select_index is None or not self._profiles:
            self._populate_editor(None)
            return
        select_index = max(0, min(len(self._profiles) - 1, select_index))
        self._list.setCurrentRow(select_index)

    def _current_profile(self) -> Optional[GameProfile]:
        idx = self._list.currentRow()
        if 0 <= idx < len(self._profiles):
            return self._profiles[idx]
        return None

    def _populate_editor(self, profile: Optional[GameProfile]) -> None:
        self._loading = True
        try:
            enabled = profile is not None
            is_desktop = bool(profile and profile.is_desktop)

            # Sliders + name are editable for both desktop and game profiles.
            for w in (
                self._ed_name,
                self._sl_brightness, self._sl_contrast, self._sl_gamma,
                self._sl_black_lift,
            ):
                w.setEnabled(enabled)

            # Vibrance is special: if the driver rejects non-zero DVC writes
            # (legacy + Ex both blocked on NVIDIA 5xx+), the slider just lies.
            # Disable it with an explanatory tooltip so the user isn't fighting
            # a control that does nothing.
            vibrance_works = self._color.nvapi_available and self._color.vibrance_writable
            self._sl_vibrance.setEnabled(enabled and vibrance_works)
            if not vibrance_works:
                self._sl_vibrance.setToolTip(
                    "Недоступно: драйвер NVIDIA отклоняет программную запись DVC.\n"
                    "Для повышения насыщенности используй NVIDIA Control Panel\n"
                    "или Game Filter в NVIDIA App (Alt+Z в игре)."
                )
            else:
                self._sl_vibrance.setToolTip("")

            # Process binding only applies to game profiles. For desktop the
            # field is locked (and the Browse button next to it).
            self._ed_process.setEnabled(enabled and not is_desktop)
            if hasattr(self, "_btn_browse"):
                self._btn_browse.setEnabled(enabled and not is_desktop)
            # Same for Delete: desktop is a singleton, not deletable.
            if hasattr(self, "_btn_delete"):
                self._btn_delete.setEnabled(enabled and not is_desktop)

            if profile is None:
                self._ed_name.setText("")
                self._ed_process.setText("")
                self._sl_vibrance.setValue(0)
                self._sl_brightness.setValue(1.0)
                self._sl_contrast.setValue(1.0)
                self._sl_gamma.setValue(1.0)
                self._sl_black_lift.setValue(0.0)
                return
            self._ed_name.setText(profile.name)
            self._ed_process.setText(profile.process)
            self._sl_vibrance.setValue(profile.vibrance)
            self._sl_brightness.setValue(profile.brightness)
            self._sl_contrast.setValue(profile.contrast)
            self._sl_gamma.setValue(profile.gamma)
            self._sl_black_lift.setValue(profile.black_lift)
        finally:
            self._loading = False

    # ----- slots -----

    def _on_row_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._profiles):
            self._populate_editor(self._profiles[idx])
        else:
            self._populate_editor(None)

    def _on_add(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите .exe игры", "", "Executables (*.exe);;All files (*)"
        )
        if not path:
            return
        exe_name = os.path.basename(path)
        name = os.path.splitext(exe_name)[0]

        # H3: warn if another profile already targets this exe — ProcessWatcher
        # dedups by process name and only keeps one of them, so a duplicate
        # profile would silently never trigger.
        existing = next(
            (p for p in self._profiles
             if p.process and p.process.lower() == exe_name.lower()),
            None,
        )
        if existing is not None:
            choice = QMessageBox.question(
                self,
                "Профиль уже существует",
                f"Профиль для «{exe_name}» уже есть: «{existing.name}».\n\n"
                "Если добавить ещё один с тем же процессом — сработает только "
                "один из них (последний по списку).\n\nВсё равно добавить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return

        profile = GameProfile(name=name, process=exe_name, exe_path=path)
        self._profiles.append(profile)
        self._save_and_emit()
        self._refresh_list(select_index=len(self._profiles) - 1)

    def _on_delete(self) -> None:
        idx = self._list.currentRow()
        if not (0 <= idx < len(self._profiles)):
            return
        # Desktop profile is a singleton — cannot be deleted from the UI.
        if self._profiles[idx].is_desktop:
            return
        del self._profiles[idx]
        self._save_and_emit()
        self._refresh_list(select_index=min(idx, len(self._profiles) - 1) if self._profiles else None)

    def _on_browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите .exe игры", "", "Executables (*.exe);;All files (*)"
        )
        if not path:
            return
        exe_name = os.path.basename(path)
        self._ed_process.setText(exe_name)
        # Update both process name and exe_path on the current profile, then
        # refresh so the icon in the list picks up the new path.
        prof = self._current_profile()
        if prof is not None:
            prof.process = exe_name
            prof.exe_path = path
            self._save_and_emit()
            idx = self._list.currentRow()
            self._refresh_list(select_index=idx)
        else:
            self._on_process_changed()

    def _on_name_changed(self) -> None:
        if self._loading:
            return
        prof = self._current_profile()
        if prof is None:
            return
        new_name = self._ed_name.text().strip()
        if not new_name or new_name == prof.name:
            return
        prof.name = new_name
        self._save_and_emit()
        # update list label without losing selection
        idx = self._list.currentRow()
        if 0 <= idx < self._list.count():
            label = f"{prof.name}  ({prof.process})" if prof.process else prof.name
            self._list.item(idx).setText(label)

    def _on_process_changed(self) -> None:
        if self._loading:
            return
        prof = self._current_profile()
        if prof is None:
            return
        new_proc = self._ed_process.text().strip()
        if new_proc == prof.process:
            return
        prof.process = new_proc
        self._save_and_emit()
        idx = self._list.currentRow()
        if 0 <= idx < self._list.count():
            label = f"{prof.name}  ({prof.process})" if prof.process else prof.name
            self._list.item(idx).setText(label)

    def _on_field_changed(self, field: str, value) -> None:
        if self._loading:
            return
        prof = self._current_profile()
        if prof is None:
            return
        if field == "vibrance":
            prof.vibrance = int(value)
        elif field == "brightness":
            prof.brightness = float(value)
        elif field == "contrast":
            prof.contrast = float(value)
        elif field == "gamma":
            prof.gamma = float(value)
        elif field == "black_lift":
            prof.black_lift = float(value)
        self._save_and_emit()
        # Live preview when editing the currently active profile.
        if self._is_currently_active(prof):
            self._live_timer.start()

    def _is_currently_active(self, profile: GameProfile) -> bool:
        """
        True if the given profile is the one currently affecting the screen,
        i.e. the active game profile, or — if no game is running — the
        desktop profile.
        """
        active = (
            self._active_process_provider() if self._active_process_provider else None
        )
        if active:
            return bool(profile.process and profile.process.lower() == active.lower())
        return profile.is_desktop

    def _do_live_apply(self) -> None:
        """Debounced apply of the currently selected active profile."""
        prof = self._current_profile()
        if prof is None or not self._is_currently_active(prof):
            return
        self._color.apply(
            prof.vibrance, prof.brightness, prof.contrast, prof.gamma, prof.black_lift
        )

    def _on_apply_clicked(self) -> None:
        prof = self._current_profile()
        if prof is None:
            self._set_status("Сначала выберите профиль")
            return
        self._color.apply(
            prof.vibrance, prof.brightness, prof.contrast, prof.gamma, prof.black_lift
        )
        # If a game is currently running, "Apply now" of a different profile
        # is a temporary test — when the game closes the watcher will revert
        # to the desktop profile, not back to this one. Be explicit so the
        # user doesn't think the change is permanent.
        active = (
            self._active_process_provider() if self._active_process_provider else None
        )
        if active and not self._is_currently_active(prof):
            self._set_status(
                f"Применён профиль «{prof.name}» (тест) — пока запущена игра, "
                "после её закрытия восстановится профиль рабочего стола"
            )
        else:
            self._set_status(f"Применён профиль «{prof.name}» (тест)")

    def _on_reset_clicked(self) -> None:
        self._color.reset()
        self._set_status("Сброшено к дефолту")

    # ----- helpers -----

    def _save_and_emit(self) -> None:
        self._storage.save(self._profiles)
        self.profiles_changed.emit(list(self._profiles))

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    # exposed for ProcessWatcher signal handlers
    def notify_status(self, text: str) -> None:
        self._set_status(text)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Hide to tray instead of quitting; the QApplication is configured with
        # quitOnLastWindowClosed=False in main.py.
        event.ignore()
        self.hide()
