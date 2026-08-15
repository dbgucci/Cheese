# PyInstaller spec: ORB-Signals.exe -- the break/retest alerts window.
#
#   pyinstaller packaging/Signals.spec --noconfirm
#
# A windowed app, unlike the autobot build: this one has a real interface, and a
# console flashing up behind it is noise. Errors surface in the Activity page and
# the status bar instead.
#
# Must be built on Windows. PyInstaller cannot cross-compile, and MetaTrader5 is
# published for Windows only -- and it has to be installed *at build time*,
# because a frozen exe cannot load packages from the user's machine afterwards.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

block_cipher = None

try:
    mt5_datas, mt5_binaries, mt5_hiddenimports = collect_all("MetaTrader5")
except Exception:
    mt5_datas, mt5_binaries, mt5_hiddenimports = [], [], []
    print("=" * 70)
    print("WARNING: MetaTrader5 is not installed in this build environment.")
    print("The window will build and open, and will not be able to read prices.")
    print("Run `pip install MetaTrader5` on this Windows machine and rebuild.")
    print("=" * 70)

a = Analysis(
    [str(ROOT / "run_signals_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=mt5_binaries,
    datas=mt5_datas,
    hiddenimports=[
        "cheese_signals",
        "cheese_signals.paths",
        "cheese_signals.gui",
        "cheese_signals.gui.signals_app",
        "cheese_signals.gui.common",
        "cheese_signals.gui.theme",
        "cheese_signals.gui.widgets",
        "cheese_signals.gui.icons",
        "cheese_signals.gui.branding",
        "cheese_signals.markets",
        "cheese_signals.markets.clock",
        "cheese_signals.markets.orb",
        "cheese_signals.markets.signals",
        # Imported inside a function so a missing PySide6 degrades to "no
        # chart" rather than "no app", which also means static analysis never
        # sees it and the frozen build would ship without it.
        "cheese_signals.markets.chart",
        "cheese_signals.markets.signal_settings",
        "cheese_signals.markets.mt5_bridge",
        "cheese_signals.markets.survey",
        "cheese_signals.notifiers",
        "cheese_signals.notifiers.telegram",
        # Imported inside functions, so static analysis never sees them.
        "MetaTrader5",
        "zoneinfo",
    ] + mt5_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Qt ships a great deal this window never touches, and the autobot's trading
    # code is deliberately excluded so the shipped binary contains no order path
    # at all -- the safety claim is then a property of the build, not a promise.
    excludes=[
        "tkinter",
        "matplotlib",
        "BinaryOptionsToolsV2",
        "cheese_signals.markets.autobot",
        "cheese_signals.markets.mt5_trader",
        "cheese_signals.markets.launcher",
        "cheese_signals.markets.orb_backtest",
        "cheese_signals.gui.autobot_app",
        "cheese_signals.gui.autobot_worker",
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

# Generated at build time so no binary asset is committed. "ORB" rather than the
# KPS monogram: two apps from one repository with identical taskbar icons is how
# a user opens the wrong one.
icon_path = ROOT / "packaging" / "orb.ico"
try:
    import os
    import sys as _sys

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    _sys.path.insert(0, str(ROOT / "src"))
    from cheese_signals.gui.branding import write_ico

    write_ico(str(icon_path), text="ORB")
    print(f"generated icon: {icon_path}")
except Exception as exc:
    print(f"WARNING: could not generate the icon ({exc}); building without one")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ORB-Signals",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed: the interface is the interface
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)
