@echo off
REM Local build script. Produces dist\MonitorColorSwitcher.exe.
REM Requires the project's .venv to exist with PyQt6/psutil/pywin32 installed.

setlocal

if not exist .venv\Scripts\python.exe (
    echo .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo === Installing PyInstaller ===
.venv\Scripts\python.exe -m pip install --upgrade --quiet pyinstaller

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === Building executable ===
.venv\Scripts\python.exe -m PyInstaller --clean MonitorColorSwitcher.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo === Done ===
echo Output: dist\MonitorColorSwitcher.exe
dir /b dist\MonitorColorSwitcher.exe
