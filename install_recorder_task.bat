@echo off
REM Install the candle recorder as a Windows scheduled task, so it survives
REM reboots, Windows updates, and closed windows.
REM
REM A console window left open for four weeks does not survive four weeks.
REM It survives until the first reboot, sleep, or accidental close -- and the
REM history it was building stops silently at that point. A scheduled task
REM restarts itself instead.
REM
REM Run this from an ADMINISTRATOR command prompt (right-click, Run as
REM administrator), because registering a task needs it.

setlocal
cd /d "%~dp0"

set TASKNAME=KPS-CandleRecorder

echo ============================================
echo   Install recorder as a scheduled task
echo ============================================
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: this must be run as administrator.
    echo Right-click this file and choose "Run as administrator".
    pause
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip >nul
    pip install -e . >nul
    pip install binaryoptionstoolsv2
    if errorlevel 1 (
        echo ERROR: binaryoptionstoolsv2 would not install.
        pause
        exit /b 1
    )
)

REM The session id lives in settings.json, not an environment variable. It is
REM a JSON blob full of quotes, which setx and PowerShell both mangle -- and a
REM scheduled task running as SYSTEM would not inherit a user variable anyway.
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from cheese_signals import settings; sys.exit(0 if settings.Settings.load().pocket_option_ssid else 1)"
if errorlevel 1 (
    echo ERROR: no Pocket Option session id has been saved yet.
    echo.
    echo Save one first ^(this reads it from a prompt, so the shell cannot
    echo mangle its quotes^):
    echo.
    echo    .venv\Scripts\python.exe run_recorder.py --set-ssid
    echo.
    echo Then run this script again.
    echo.
    pause
    exit /b 1
)

echo Registering scheduled task "%TASKNAME%"...
echo.

REM /sc onstart survives reboots; /ru SYSTEM means it runs without a logged-in
REM user. The task is recreated each time this script runs, so re-running it
REM is how you update the configuration.
schtasks /query /tn "%TASKNAME%" >nul 2>&1
if not errorlevel 1 (
    echo   an existing task was found - replacing it
    schtasks /end /tn "%TASKNAME%" >nul 2>&1
    schtasks /delete /tn "%TASKNAME%" /f >nul 2>&1
)

schtasks /create ^
    /tn "%TASKNAME%" ^
    /tr "'%CD%\.venv\Scripts\python.exe' '%CD%\run_recorder.py'" ^
    /sc onstart ^
    /ru SYSTEM ^
    /rl HIGHEST ^
    /f
if errorlevel 1 (
    echo ERROR: could not register the task. The output above says why.
    pause
    exit /b 1
)

REM Restart the task if the process ever exits, and never stop it for
REM running "too long" -- running for weeks is the entire point.
schtasks /change /tn "%TASKNAME%" /ri 5 /du 9999:59 >nul 2>&1

echo.
echo Starting it now...
schtasks /run /tn "%TASKNAME%"

echo.
echo ============================================
echo   Installed and running
echo ============================================
echo.
echo It will restart automatically after a reboot.
echo.
echo Check how much history exists:
echo    python run_recorder.py --status
echo.
echo Stop it:
echo    schtasks /end /tn "%TASKNAME%"
echo.
echo Remove it entirely:
echo    schtasks /delete /tn "%TASKNAME%" /f
echo.
echo IMPORTANT: your Pocket Option session id will expire before four weeks
echo are up. When it does the recorder keeps running but records nothing, so
echo check --status every few days. If the bar count has stopped rising, set
echo a fresh POCKET_OPTION_SSID and re-run this script.
echo.
pause
exit /b 0
