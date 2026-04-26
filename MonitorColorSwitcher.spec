# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Monitor Color Switcher.
#
# Build:
#   pyinstaller --clean MonitorColorSwitcher.spec
#
# Output: dist/MonitorColorSwitcher.exe (single-file, windowed, with embedded
# icon). User data (profiles.json, cache/) is written next to the .exe at
# runtime — see core/paths.py.

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/app.svg", "assets"),
        ("assets/app.ico", "assets"),
    ],
    hiddenimports=[
        "PyQt6.QtSvg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim things PyInstaller bundles by default but we don't use.
        "tkinter",
        "unittest",
        "test",
        "pydoc",
        "PyQt6.QtNetwork",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MonitorColorSwitcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app.ico",
)
