@echo off
REM Double-click this to run the ORB break/retest signal bot.
REM
REM It installs what it needs on first run into a local .venv-signals folder,
REM then starts watching. Signals only -- this cannot place an order.
REM
REM BEFORE RUNNING: start MetaTrader 5 and log in. The bot reads prices from a
REM running terminal; it does not launch one, and it never trades.

cd /d "%~dp0"

echo ==================================================
echo   ORB Signals - break and retest alerts
echo ==================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo.
    echo Install Python from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" on the first screen. Then close this window,
    echo open a new one, and double-click this file again.
    echo.
    pause
    exit /b 1
)

if not exist .venv-signals (
    echo [1/3] Creating a local Python environment, one time only...
    python -m venv .venv-signals
    if errorlevel 1 (
        echo ERROR: could not create the environment.
        pause
        exit /b 1
    )
)

call .venv-signals\Scripts\activate.bat

REM Installed every run, but pip is a no-op when everything is already present,
REM so this costs a second and removes "did I remember to install it" as a
REM failure mode.
echo [2/3] Checking dependencies...
python -m pip install --upgrade pip --quiet
pip install -e . --quiet
if errorlevel 1 (
    echo ERROR: could not install the package. The output above says why.
    pause
    exit /b 1
)

pip install MetaTrader5 --quiet
if errorlevel 1 (
    echo.
    echo ==========================================================
    echo ERROR: MetaTrader5 would not install.
    echo.
    echo It is Windows-only and needs 64-bit Python. The usual cause
    echo is a Python version it has no wheel for yet - Python 3.13
    echo is often too new. Installing Python 3.12 alongside and
    echo running this again fixes it.
    echo.
    python -c "import platform,sys; print('  you have Python', platform.python_version(), platform.architecture()[0])"
    echo ==========================================================
    pause
    exit /b 1
)

echo [3/3] Starting.
echo.
echo Reminder: MetaTrader 5 must be open and logged in.
echo Ctrl+C to stop.
echo.

python -m cheese_signals.markets.signals %*

echo.
pause
