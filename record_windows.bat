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

if "%POCKET_OPTION_SSID%"=="" (
    echo.
    echo ERROR: POCKET_OPTION_SSID is not set.
    echo.
    echo The recorder needs your Pocket Option session id. Set it for this
    echo window and re-run:
    echo    set POCKET_OPTION_SSID=your-session-id-here
    echo.
    echo To make it permanent, use the System environment variable settings.
    echo.
    pause
    exit /b 1
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
