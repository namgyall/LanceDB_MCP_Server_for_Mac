@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0setup.ps1" (
    echo.
    echo Portable setup script is missing.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
    echo.
    echo Runtime setup failed. Review the error above and try Run.bat again.
    pause
    exit /b 1
)

if not exist "%~dp0runtime\pythonw.exe" (
    echo.
    echo Portable Python runtime is missing after setup.
    pause
    exit /b 1
)

start "" "%~dp0runtime\pythonw.exe" "%~dp0gui.py"
if errorlevel 1 (
    echo.
    echo Could not start the PySide6 desktop app.
    pause
    exit /b 1
)
endlocal
