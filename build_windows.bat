@echo off
REM Build KPS.exe on a Windows machine.
REM Requires Python 3.10+ installed and on PATH.

REM Always run from the folder this script lives in, so double-clicking it
REM from Explorer (or running it from another directory) still works.
cd /d "%~dp0"

echo ============================================
echo   Building KPS for Windows
echo ============================================
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
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo [2/4] Installing dependencies (a few minutes on first run)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :installfail
pip install -e .
if errorlevel 1 goto :installfail
pip install PySide6 pyinstaller
if errorlevel 1 goto :installfail

REM The live Pocket Option feed. This must be installed BEFORE building:
REM a frozen exe cannot load packages installed later, so if this is
REM missing at build time the exe will only support the synthetic feed.
pip install binaryoptionstoolsv2
if errorlevel 1 (
    echo.
    echo WARNING: binaryoptionstoolsv2 could not be installed.
    echo The app will still build, but the live Pocket Option feed will not
    echo be available in it - only the "synthetic" practice feed.
    echo.
)

echo.
echo [3/4] Building executable (this takes a few minutes)...
pyinstaller packaging\KPS.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. The output above says why.
    pause
    exit /b 1
)

echo.
echo [4/4] Done.
echo.
if exist dist\KPS.exe (
    echo SUCCESS: dist\KPS.exe
    echo.
    echo Copy that file wherever you like and double-click it.
    echo On first run it creates a "KPS" folder on your Desktop
    echo for its database, settings and exports.
) else (
    echo BUILD FAILED: dist\KPS.exe was not produced.
    echo Scroll up to find the first line containing "ERROR".
)
echo.
pause
exit /b 0

:installfail
echo.
echo ERROR: dependency installation failed. The output above says why.
echo Common causes: no internet connection, or a proxy/firewall blocking pip.
pause
exit /b 1
