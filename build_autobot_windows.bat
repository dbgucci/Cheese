@echo off
REM Build ORB-Autobot.exe on a Windows machine.
REM Requires Python 3.10+ installed and on PATH. Double-click this file.

cd /d "%~dp0"

echo ==================================================
echo   Building ORB-Autobot.exe
echo ==================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" on the first install screen.
    echo Then close this window, open a new one, and run this script again.
    echo.
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
if not exist .venv-autobot (
    python -m venv .venv-autobot
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo [2/4] Installing dependencies...
call .venv-autobot\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :installfail
pip install -e .
if errorlevel 1 goto :installfail
pip install pyinstaller
if errorlevel 1 goto :installfail

REM MetaTrader5 must be present at BUILD time. A frozen exe cannot load
REM packages installed on the user's machine afterwards, so if this is missing
REM now, the finished exe can never reach a broker.
pip install MetaTrader5
if errorlevel 1 (
    echo.
    echo ==========================================================
    echo ERROR: MetaTrader5 could not be installed.
    echo.
    echo Without it the exe cannot reach a broker at all, so the
    echo build is stopping rather than producing something broken.
    echo.
    echo MetaTrader5 is Windows-only and needs a 64-bit Python.
    echo Check: python -c "import platform; print(platform.architecture())"
    echo ==========================================================
    pause
    exit /b 1
)

echo.
echo [3/4] Building (a few minutes)...
pyinstaller packaging\Autobot.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. The output above says why.
    pause
    exit /b 1
)

echo.
echo [4/4] Done.
echo.
if exist dist\ORB-Autobot.exe (
    echo SUCCESS: dist\ORB-Autobot.exe
    echo.
    echo Copy it anywhere - your Desktop is fine - and double-click it.
    echo.
    echo BEFORE YOU RUN IT: start MetaTrader 5 and log in. This app
    echo attaches to a running terminal, it does not launch one.
    echo.
    echo It starts in dry-run mode. Nothing is sent to your broker
    echo until you pick the live option and type a confirmation.
) else (
    echo BUILD FAILED: dist\ORB-Autobot.exe was not produced.
    echo Scroll up to the first line containing "ERROR".
)
echo.
pause
exit /b 0

:installfail
echo.
echo ERROR: dependency installation failed. The output above says why.
echo Common causes: no internet connection, or a proxy blocking pip.
pause
exit /b 1
