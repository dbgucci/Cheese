# PyInstaller spec: builds a single-file Windows executable.
#
#   pyinstaller packaging/CheeseSignals.spec --noconfirm
#
# Produces dist/CheeseSignals.exe -- no Python install required on the
# target machine.

import sys
from pathlib import Path

# SPECPATH is injected by PyInstaller; fall back to cwd when linted standalone.
ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

block_cipher = None

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "cheese_signals",
        "cheese_signals.gui",
        "cheese_signals.gui.app",
        "cheese_signals.gui.theme",
        "cheese_signals.gui.widgets",
        "cheese_signals.data",
        "cheese_signals.data.synthetic",
        "cheese_signals.data.csv_feed",
        "cheese_signals.notifiers",
        "cheese_signals.notifiers.telegram",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt ships a lot we never touch; excluding it keeps the exe substantially
    # smaller and speeds up start-up.
    excludes=[
        "tkinter",
        "matplotlib",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtDesigner",
        "PySide6.QtTest",
        "PySide6.QtOpenGL",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = ROOT / "packaging" / "icon.ico"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CheeseSignals",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed app: no console window behind the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)
