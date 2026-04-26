# Monitor Color Switcher

A small Windows tray utility that automatically swaps your monitor colour
profile (brightness, contrast, gamma, shadow lift, NVIDIA Digital Vibrance)
when a chosen game launches, and reverts to your desktop profile when the
game closes.

Built with PyQt6 + ctypes. NVIDIA-aware but works on non-NVIDIA systems too
(without vibrance — gamma ramp still applies).

## Features

- **Per-game profiles** — pick `.exe`, set brightness/contrast/gamma/shadow
  lift/vibrance, save. The watcher polls running processes every 2.5 s and
  applies the matching profile automatically.
- **Desktop profile** — singleton profile applied when no game is running.
- **Multi-monitor** — gamma ramp is applied to every active display.
- **Live preview** — slider tweaks on the currently active profile apply
  immediately, with debounce.
- **Shadow lift** — selectively brightens dark pixels without burning
  highlights ("black equalizer"-style).
- **Hot-plug & sleep aware** — re-applies the active profile after monitor
  changes or system resume.
- **Safe shutdown** — atexit hooks ensure the gamma ramp is restored even on
  abnormal termination (logoff, SIGTERM).
- **Single-instance** — second launch (autostart + manual) silently exits.
- **Tray-only when minimised**, with optional Windows autostart via
  `HKCU\…\Run` (uses `pythonw.exe`, no console flash).

## Limitations

- **NVIDIA driver 5xx+ regression**: legacy NvAPI Digital Vibrance is rejected
  with `NVAPI_INVALID_ARGUMENT` regardless of how it is called. The vibrance
  slider is auto-disabled in that case. Workaround: NVIDIA Control Panel or
  NVIDIA App Game Filter.
- **Exclusive fullscreen games** ignore the desktop GDI gamma ramp because
  Direct3D applications own their own swap-chain gamma path. Switch the game
  to *Borderless* / *Windowed* to see the effect, or use NVIDIA App Game
  Filter for fullscreen.
- **Windows DWM** can silently drop gamma writes on systems with HDR / Auto
  Color Management / Night Light / a calibrated ICC profile active. If the
  app warns about this at startup, disable those features.

## Installation

### From a release zip

1. Grab the latest zip from [Releases](../../releases).
2. Extract `MonitorColorSwitcher.exe` somewhere writable (Documents, USB
   stick, anywhere). The first launch creates `profiles.json` and `cache/`
   right next to the executable — keep them together if you move the .exe.
3. Double-click. The icon will appear in the system tray.

### From source

Requires Python 3.12+ on Windows.

```cmd
git clone https://github.com/hertzxq/monitor-color-switcher.git
cd monitor-color-switcher
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Pass `--minimized` to start hidden in tray (used by the autostart entry).

## Building the .exe

```cmd
.venv\Scripts\pip install pyinstaller
build.bat
```

Output: `dist\MonitorColorSwitcher.exe` (single-file, ~35 MB, GUI-only,
embedded multi-resolution icon).

## profiles.json schema

Auto-created next to the executable. Example:

```json
[
  {
    "name": "Рабочий стол",
    "process": "",
    "exe_path": "",
    "vibrance": 50,
    "brightness": 1.0,
    "contrast": 1.0,
    "gamma": 1.0,
    "black_lift": 0.0,
    "is_desktop": true
  },
  {
    "name": "CS2",
    "process": "cs2.exe",
    "exe_path": "C:\\Games\\Steam\\steamapps\\common\\CS2\\game\\bin\\win64\\cs2.exe",
    "vibrance": 80,
    "brightness": 1.1,
    "contrast": 1.05,
    "gamma": 1.0,
    "black_lift": 0.15
  }
]
```

| Field | Range | Notes |
| --- | --- | --- |
| `vibrance` | 0..100 | 50 = neutral. Mapped to NvAPI internal range. |
| `brightness` | 0.3..2.0 | Additive offset. 1.0 = no change. |
| `contrast` | 0.3..2.0 | Around midpoint. 1.0 = no change. |
| `gamma` | 0.3..3.0 | Power curve. 1.0 = no change. |
| `black_lift` | 0.0..0.5 | Selectively raises dark pixels. 0 = off. |
| `is_desktop` | bool | Singleton, applied when no game is running. |

If `profiles.json` becomes unreadable, the app moves it aside as
`profiles.json.broken-<timestamp>` rather than overwriting it.

## Project layout

```
main.py                  # entry point: wires everything, native event filter
models/profile.py        # GameProfile dataclass
core/
  paths.py               # frozen-aware resource / user-data paths
  color_manager.py       # NvAPI vibrance + GDI gamma ramp + persistent DCs
  process_watcher.py     # psutil polling, fullscreen heuristic
  profile_storage.py     # atomic JSON storage with broken-file backup
  icon_extractor.py      # game icons via QFileIconProvider + SVG→ICO
gui/
  main_window.py         # profile editor with live preview
  tray.py                # system tray + autostart helper
assets/
  app.svg                # source app icon
  app.ico                # generated multi-resolution icon (used by EXE)
MonitorColorSwitcher.spec  # PyInstaller config
build.bat                # one-shot local build
```

## License

MIT.
