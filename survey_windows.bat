@echo off
REM Measure what Liquid Brokers actually charges, per instrument.
REM
REM Double-click this from the repo folder. MetaTrader 5 must be OPEN and
REM logged in to the account you want measured -- the Python package attaches
REM to a running terminal, it does not log in by itself.

cd /d "%~dp0"

echo ============================================
echo   Cost wall survey
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

echo [2/4] Installing the package...
python -m pip install --upgrade pip >nul
pip install -e .
if errorlevel 1 (
    echo ERROR: could not install the package. The output above says why.
    pause
    exit /b 1
)

echo [3/4] Installing the MetaTrader 5 bridge...
pip install MetaTrader5
if errorlevel 1 (
    echo.
    echo ERROR: MetaTrader5 would not install.
    echo.
    echo The usual cause is the Python version. MetaQuotes publishes wheels
    echo for a limited set of versions, and the newest Python release is
    echo often not among them yet.
    echo.
    python -c "import sys; print('You are running Python %%d.%%d' %% sys.version_info[:2])"
    echo.
    echo Fix: install Python 3.12 from
    echo   https://www.python.org/downloads/release/python-3128/
    echo then delete the .venv folder next to this script and run it again.
    echo Both versions can be installed side by side.
    echo.
    pause
    exit /b 1
)

echo.
echo [4/4] Running the survey. MetaTrader 5 must be open and logged in.
echo       This pulls 90 days of 1-minute history for ten instruments and
echo       takes a few minutes.
echo.
python -m cheese_signals.markets.survey --days 90 --out "%USERPROFILE%\Desktop\cost-wall-survey.txt"
if errorlevel 1 (
    echo.
    echo The survey did not complete. The output above says why.
    echo.
    echo Most common causes:
    echo   - MetaTrader 5 is not running, or is not logged in
    echo   - the terminal is running as administrator and this window is not
    echo     ^(or the other way round^) - they must match
    echo   - "Algo Trading" is disabled in the terminal toolbar
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Saved to your Desktop as cost-wall-survey.txt
echo ============================================
echo.
pause
exit /b 0
