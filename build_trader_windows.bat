@echo off
REM Build KPSTrader.exe -- the real-market autotrader.
REM Requires Python 3.12 (MetaQuotes does not publish wheels for the newest
REM Python release), installed with "Add python.exe to PATH" ticked.

cd /d "%~dp0"

echo ============================================
echo   Building KPSTrader for Windows
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3.12 from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip >nul
pip install -e .
if errorlevel 1 goto :installfail
pip install pyinstaller
if errorlevel 1 goto :installfail

REM This must be installed BEFORE building. A frozen exe cannot load packages
REM installed afterwards, so a build without it produces an exe that starts
REM and then cannot reach the terminal.
pip install MetaTrader5
if errorlevel 1 (
    echo.
    echo ERROR: MetaTrader5 would not install, and the exe is useless without it.
    echo.
    python -c "import sys; print('You are running Python %%d.%%d' %% sys.version_info[:2])"
    echo.
    echo MetaQuotes publishes wheels for a limited set of Python versions.
    echo Install Python 3.12, delete the .venv folder here, and run this again.
    echo.
    pause
    exit /b 1
)

echo.
echo [3/4] Building (a few minutes)...
pyinstaller packaging\KPSTrader.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. The output above says why.
    pause
    exit /b 1
)

echo.
echo [4/4] Done.
echo.
if exist dist\KPSTrader.exe (
    echo SUCCESS: dist\KPSTrader.exe
    echo.
    echo Copy it anywhere and double-click it. On first run it creates a
    echo "KPS Markets" folder on your Desktop holding trader.json and a log.
    echo.
    echo It starts in DRY RUN. To trade for real, edit trader.json and set
    echo   "live": true
    echo Do that only after watching it in dry run and running the cost survey.
) else (
    echo BUILD FAILED: dist\KPSTrader.exe was not produced.
    echo Scroll up for the first line containing "ERROR".
)
echo.
pause
exit /b 0

:installfail
echo.
echo ERROR: dependency installation failed. The output above says why.
pause
exit /b 1
