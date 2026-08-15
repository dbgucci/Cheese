@echo off
REM Record OTC candles continuously, so the research has enough data to work
REM with. Places no orders. Leave it running for weeks.
REM
REM The first research run found 3.8 days of history. Testing an hour-of-day
REM rule needs 5.6 days before a single hypothesis becomes measurable, and
REM the patterns worth finding need about four weeks. Only running time
REM closes that gap.

cd /d "%~dp0"

echo ============================================
echo   OTC candle recorder
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" on the first screen.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat

echo Installing the package...
python -m pip install --upgrade pip >nul
pip install -e . >nul
if errorlevel 1 (
    echo ERROR: could not install the package.
    pause
    exit /b 1
)

echo Installing the Pocket Option client...
pip install binaryoptionstoolsv2
if errorlevel 1 (
    echo.
    echo ERROR: binaryoptionstoolsv2 would not install. Without it there is
    echo no live feed to record from.
    pause
    exit /b 1
)

REM The session id lives in settings.json. It is a JSON blob full of quotes,
REM which setx and PowerShell both mangle, so it is pasted at a prompt instead.
python -c "import sys; sys.path.insert(0,'src'); from cheese_signals import settings; sys.exit(0 if settings.Settings.load().pocket_option_ssid else 1)"
if errorlevel 1 (
    echo.
    echo No Pocket Option session id saved yet. Pasting one now.
    echo.
    python run_recorder.py --set-ssid
    if errorlevel 1 (
        echo.
        echo No session id saved - cannot record without one.
        pause
        exit /b 1
    )
)

echo.
echo ============================================
echo   Recording. Leave this window open.
echo ============================================
echo.
echo Ctrl+C stops it cleanly. Check progress any time with:
echo    python run_recorder.py --status
echo.

python run_recorder.py

echo.
echo Stopped. Current history:
echo.
python run_recorder.py --status
echo.
pause
exit /b 0
