"""
Filesystem layout helpers.

Two roots, kept separate so a frozen build doesn't store user data inside
the PyInstaller temp extraction directory (which is wiped on every launch):

  resource_dir() — read-only assets shipped inside the build (assets/app.svg,
                   assets/app.ico). In a frozen build this points to
                   sys._MEIPASS; in dev it's the project root.

  user_data_dir() — writable directory for profiles.json and cache/.
                    In a frozen build this is the directory containing the
                    .exe (so the app is portable: copy the .exe along with
                    profiles.json + cache/ and it travels). In dev it's the
                    project root.

If you ever want a non-portable install, swap user_data_dir() to
%LOCALAPPDATA%/MonitorColorSwitcher and the rest of the code will follow.
"""

import os
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> str:
    if is_frozen():
        # sys._MEIPASS is set by PyInstaller's bootloader.
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    # Dev: project root = parent of this file's parent (core/paths.py -> root)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def assets_dir() -> str:
    return os.path.join(resource_dir(), "assets")


def cache_dir() -> str:
    return os.path.join(user_data_dir(), "cache")


def profiles_path() -> str:
    return os.path.join(user_data_dir(), "profiles.json")
