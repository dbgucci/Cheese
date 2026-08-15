@echo off
REM Collect OTC candles for every pair, continuously.
REM
REM Leave this running. It places no trades and runs no strategy -- it only
REM records raw candles so the analysis has enough data to say something.

cd /d "%~dp0"

echo ============================================
echo   KPS OTC data collector
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

echo Installing / updating...
python -m pip install --upgrade pip >nul
pip install -e . >nul
if errorlevel 1 (
    echo ERROR: could not install the package.
    pause
    exit /b 1
)
pip install binaryoptionstoolsv2 >nul
if errorlevel 1 (
    echo.
    echo WARNING: the live Pocket Option client would not install.
    echo Only the synthetic practice feed will work.
    echo.
)

echo.
echo Pairs and settings live in:
echo   %USERPROFILE%\Desktop\KPS Data\pairs.txt
echo   %USERPROFILE%\Desktop\KPS Data\collector.json
echo.
echo Put your Pocket Option SSID into collector.json before the first live run.
echo Edit pairs.txt to change which pairs are collected - one per line.
echo.
echo Press Ctrl+C at any time to stop. Progress is saved continuously.
echo.

python -m cheese_signals.research.collect %*
if errorlevel 1 (
    echo.
    echo The collector stopped. The output above says why.
    pause
    exit /b 1
)
pause
exit /b 0
