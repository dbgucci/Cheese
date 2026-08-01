@echo off
REM Build CheeseSignals.exe on a Windows machine.
REM Requires Python 3.10+ installed and on PATH.

echo ============================================
echo   Building Cheese Signals for Windows
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.10 or newer from https://python.org
    echo Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
if not exist .venv python -m venv .venv

echo [2/4] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -e . >nul
pip install PySide6 pyinstaller >nul

echo [3/4] Building executable (this takes a few minutes)...
pyinstaller packaging\CheeseSignals.spec --noconfirm

echo [4/4] Done.
echo.
if exist dist\CheeseSignals.exe (
    echo SUCCESS: dist\CheeseSignals.exe
    echo.
    echo Copy that file to your Desktop and double-click it.
    echo It will create a "CheeseSignals" folder on your Desktop for its data.
) else (
    echo BUILD FAILED - see the output above.
)
echo.
pause
