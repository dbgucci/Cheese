@echo off
REM Run the autotrader against MetaTrader 5.
REM
REM MetaTrader 5 must be OPEN, logged in, and "Algo Trading" enabled in the
REM toolbar. This starts in DRY RUN: it decides everything and places nothing.

cd /d "%~dp0"

echo ============================================
echo   Autotrader  (dry run)
echo ============================================
echo.
echo Nothing is sent to the broker in this mode. It reports what it would
echo have done so you can watch it for a few sessions first.
echo.
echo To trade for real, run this instead once you trust it:
echo   .venv\Scripts\activate ^&^& python -m cheese_signals.markets.run --live
echo.

if not exist .venv (
    echo Run survey_windows.bat first - it sets up the environment.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

python -m cheese_signals.markets.run %*
if errorlevel 1 (
    echo.
    echo The trader stopped. The output above says why.
    pause
    exit /b 1
)
pause
exit /b 0
