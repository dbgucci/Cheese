@echo off
REM Analyse every candle the bots have ever recorded, and report honestly
REM whether any of it supports a tradeable edge.
REM
REM Double-click this from the repo folder. Nothing needs to be running --
REM this reads the journal the bots already wrote to your Desktop.

cd /d "%~dp0"

echo ============================================
echo   OTC research: is there an edge in here?
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

echo [1/3] Creating virtual environment...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat

echo [2/3] Installing the package...
python -m pip install --upgrade pip >nul
pip install -e .
if errorlevel 1 (
    echo ERROR: could not install the package. The output above says why.
    pause
    exit /b 1
)

echo.
echo [3/3] Running the analysis...
echo.
echo Searching your Desktop folders for every bot's candle data, then
echo analysing all of it together. Expect this to take several minutes.
echo.

python run_research.py --auto --payout 0.92 --out "%USERPROFILE%\Desktop\otc-research-report.txt"
if errorlevel 1 (
    echo.
    echo The analysis did not complete. The output above says why.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Saved to your Desktop as otc-research-report.txt
echo ============================================
echo.
echo Read it in this order:
echo.
echo   1. DATA INVENTORY - check the "Sources NOT used" list. Anything in
echo      there that you believe IS candle data means we are analysing less
echo      than you think.
echo.
echo   2. STEP 1 - if a feed shows no departure from a random walk, the
echo      pattern tables below it are noise and should be read as such.
echo.
echo   3. STEP 2 - only rules marked CONFIRMED OUT OF SAMPLE mean anything.
echo.
echo Set your real payout if it is not 92 percent:
echo   python run_research.py --auto --payout 0.80
echo.
pause
exit /b 0
