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
echo [4/4] Checking the connection first...
echo.
python -m cheese_signals.markets.survey --check
if errorlevel 1 goto :nomt5
echo.
echo Press a key to run the full 90-day survey, or close this window if the
echo account above is not the one you meant to measure.
pause >nul

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

:nomt5
echo.
echo Could not talk to MetaTrader 5.
echo.
echo Liquid Brokers does support MT5 - full licence, no MT4 - but it is
echo often not switched on until you ask for it. Check, in order:
echo.
echo   1. MetaTrader 5 is open and logged in to your Liquid Brokers account.
echo   2. You have MT5 credentials at all. In the client portal look under
echo      account details for an MT5 login, password and server name. If
echo      there is no MT5 section, email support@liquidbrokers.com and ask
echo      them to enable MT5 access on your account.
echo   3. You installed MT5 from Liquid Brokers' own site if possible -
echo      their build has the correct server preset.
echo   4. If you run more than one MT5, pass the exact one:
echo        python -m cheese_signals.markets.survey --check --terminal "C:\Path\terminal64.exe"
echo.
pause
exit /b 1
