"""
Icon extraction + cache.

- get_game_icon(exe_path): returns QIcon for the game executable.
  Uses QFileIconProvider (Qt's shell32 wrapper on Windows) so we don't depend on
  pywin32 just for this. Resulting icon is rasterized at 64x64 and cached as PNG
  under cache/icons/<sha1(exe_path)>.png so subsequent calls are O(1).

- get_app_icon(assets_dir): returns QIcon for the app itself.
  If assets/app.ico exists, uses it directly.
  Else if assets/app.svg exists, rasterizes it into a multi-size QIcon AND
  generates a real cache/app.ico (multi-resolution PNG-embedded ICO). Windows
  taskbar / tray prefer this format and render much sharper than a SVG-derived
  QIcon at small sizes.
"""

import hashlib
import io
import os
import struct
from typing import Optional

from PyQt6.QtCore import QBuffer, QFileInfo, QIODevice
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QFileIconProvider, QStyle

from core import paths


# Lazy: paths.* aren't safe to call at import time in some bundlers because
# sys.executable is set late. Resolve on each call instead — the cost is one
# os.path.join.
def _cache_dir() -> str:
    return os.path.join(paths.cache_dir(), "icons")


def _app_ico_cache_path() -> str:
    return os.path.join(paths.cache_dir(), "app.ico")


_ICON_SIZE = 64

# Sizes Windows actually requests: 16 (tray, list view), 20/24 (small),
# 32 (taskbar @100%), 40/48 (taskbar HiDPI), 64/96/128/256 (Alt-Tab, jump list).
_APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

_provider: Optional[QFileIconProvider] = None


def _get_provider() -> QFileIconProvider:
    global _provider
    if _provider is None:
        _provider = QFileIconProvider()
    return _provider


def _cache_path(exe_path: str) -> str:
    digest = hashlib.sha1(exe_path.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(_cache_dir(), digest + ".png")


def get_game_icon(exe_path: str) -> QIcon:
    """
    Return QIcon for the given exe path. Empty QIcon when path is empty / missing
    or extraction failed. Caches a 64x64 PNG on first successful extraction.
    """
    if not exe_path or not os.path.isfile(exe_path):
        return QIcon()

    cached = _cache_path(exe_path)
    if os.path.isfile(cached):
        icon = QIcon(cached)
        if not icon.isNull():
            return icon
        # cache file is broken - fall through and re-extract

    icon = _get_provider().icon(QFileInfo(exe_path))
    if icon.isNull():
        return QIcon()

    pixmap = icon.pixmap(_ICON_SIZE, _ICON_SIZE)
    if pixmap.isNull():
        return icon  # use as-is, just don't cache

    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        pixmap.save(cached, "PNG")
    except OSError:
        pass  # cache is best-effort

    return QIcon(pixmap)


def get_app_icon(assets_dir: str) -> QIcon:
    """
    Resolve the app icon, in order:
      1. assets/app.ico (user-supplied, takes priority)
      2. cache/app.ico generated previously from SVG (Windows native multi-res)
      3. assets/app.svg (rasterize, generate cache/app.ico for next time)
      4. assets/app.png
      5. SP_ComputerIcon fallback
    """
    user_ico = os.path.join(assets_dir, "app.ico")
    if os.path.isfile(user_ico):
        icon = QIcon(user_ico)
        if not icon.isNull():
            return icon

    svg_path = os.path.join(assets_dir, "app.svg")
    if os.path.isfile(svg_path):
        # If a fresh ICO was generated previously and the SVG hasn't changed
        # since, reuse it. Otherwise re-generate.
        if _is_cache_fresh(svg_path, _app_ico_cache_path()):
            ico_icon = QIcon(_app_ico_cache_path())
            if not ico_icon.isNull():
                return ico_icon
        icon = _render_svg_icon(svg_path)
        if not icon.isNull():
            _try_write_ico_from_svg(svg_path, _app_ico_cache_path())
            return icon

    png_path = os.path.join(assets_dir, "app.png")
    if os.path.isfile(png_path):
        icon = QIcon(png_path)
        if not icon.isNull():
            return icon

    app = QApplication.instance()
    if app is not None:
        return app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    return QIcon()


def _is_cache_fresh(source: str, cached: str) -> bool:
    try:
        return os.path.isfile(cached) and os.path.getmtime(cached) >= os.path.getmtime(source)
    except OSError:
        return False


def _render_svg_icon(svg_path: str) -> QIcon:
    """Rasterize an SVG into multiple sizes with AA for crisp small renders."""
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPainter
    except ImportError:
        # PyQt6-Qt6 ships QtSvg; if it's somehow missing, QIcon's built-in
        # SVG support still works for the window icon (weaker for tray).
        return QIcon(svg_path)

    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return QIcon()

    icon = QIcon()
    for size in _APP_ICON_SIZES:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        # Crisper at 16/20/24 — without these flags small renders look pixelated.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pm)
    return icon


def _try_write_ico_from_svg(svg_path: str, ico_path: str) -> bool:
    """
    Build a multi-resolution Windows ICO from an SVG. Each frame is a PNG-
    encoded image stored inside the ICO container (Vista+ format). Writing
    a real ICO makes the taskbar / tray render dramatically sharper than a
    SVG-derived QIcon, because Windows picks the exact required size from
    the ICO instead of asking Qt to scale.
    """
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPainter, QImage
    except ImportError:
        return False

    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return False

    frames = []
    for size in _APP_ICON_SIZES:
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter)
        painter.end()

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not img.save(buf, "PNG"):
            return False
        frames.append((size, bytes(buf.data())))

    out = io.BytesIO()
    # ICONDIR: reserved=0, type=1 (icon), count
    out.write(struct.pack("<HHH", 0, 1, len(frames)))
    # ICONDIRENTRY × count
    header_size = 6 + 16 * len(frames)
    offset = header_size
    for size, png in frames:
        out.write(struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # Width  (0 means 256)
            size if size < 256 else 0,  # Height (0 means 256)
            0,                          # ColorCount (0 for >=256 colors)
            0,                          # Reserved
            1,                          # Planes
            32,                         # BitCount
            len(png),                   # SizeInBytes
            offset,                     # Offset
        ))
        offset += len(png)
    # Image data
    for _size, png in frames:
        out.write(png)

    try:
        os.makedirs(os.path.dirname(ico_path), exist_ok=True)
        with open(ico_path, "wb") as f:
            f.write(out.getvalue())
        return True
    except OSError:
        return False


def clear_cache() -> None:
    """Remove all cached icon PNGs. Useful when a game's exe was updated."""
    if not os.path.isdir(_cache_dir()):
        return
    for name in os.listdir(_cache_dir()):
        try:
            os.remove(os.path.join(_cache_dir(), name))
        except OSError:
            pass
