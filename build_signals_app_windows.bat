@echo off
REM Build ORB-Signals.exe on a Windows machine. Double-click this file.
REM Requires Python 3.10-3.12 installed and on PATH.

cd /d "%~dp0"

echo ==================================================
echo   Building ORB-Signals.exe
echo   Break and retest alerts. Places no trades.
echo ==================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo.
    echo Install Python 3.12 from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" on the first screen. Then close this window,
    echo open a new one, and double-click this file again.
    echo.
    pause
    exit /b 1
)

echo [1/4] Creating the build environment...
if not exist .venv-signals-app (
    python -m venv .venv-signals-app
    if errorlevel 1 (
        echo ERROR: could not create the environment.
        pause
        exit /b 1
    )
)

call .venv-signals-app\Scripts\activate.bat

echo [2/4] Installing dependencies (a few minutes on first run)...
python -m pip install --upgrade pip --quiet
pip install -e . --quiet
if errorlevel 1 goto :installfail
pip install PySide6 pyinstaller --quiet
if errorlevel 1 goto :installfail

REM Must be present at BUILD time. A frozen exe cannot load packages installed
REM afterwards, so a MetaTrader5 missing now is missing in the exe forever.
pip install MetaTrader5 --quiet
if errorlevel 1 (
    echo.
    echo ==========================================================
    echo ERROR: MetaTrader5 would not install, so the finished exe
    echo could not read prices. Stopping rather than shipping that.
    echo.
    echo It is Windows-only and needs 64-bit Python. The usual cause
    echo is a Python version with no wheel yet - 3.13 is often too
    echo new. Installing Python 3.12 and running this again fixes it.
    echo.
    python -c "import platform; print('  you have Python', platform.python_version(), platform.architecture()[0])"
    echo ==========================================================
    pause
    exit /b 1
)

echo.
echo [3/4] Building (a few minutes)...
pyinstaller packaging\Signals.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. The output above says why.
    pause
    exit /b 1
)

echo.
echo [4/4] Done.
echo.
if exist dist\ORB-Signals.exe (
    echo SUCCESS: dist\ORB-Signals.exe
    echo.
    echo Copy it to your Desktop and double-click it.
    echo.
    echo FIRST RUN:
    echo   1. Start MetaTrader 5 and log in - the app reads prices from it.
    echo   2. Go to Settings and set up Telegram ^(it walks you through it^).
    echo   3. Back on Signals, press Start watching.
    echo.
    echo It sends alerts. It has no code that can place a trade.
) else (
    echo BUILD FAILED: dist\ORB-Signals.exe was not produced.
    echo Scroll up to the first line containing "ERROR".
)
echo.
pause
exit /b 0

:installfail
echo.
echo ERROR: dependency installation failed. The output above says why.
pause
exit /b 1
