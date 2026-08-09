# PyInstaller spec for the real-market autotrader.
#
#   pyinstaller packaging/KPSTrader.spec --noconfirm
#
# Produces dist/KPSTrader.exe -- a console application, because the whole
# point of watching it is reading what it decided and why.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

block_cipher = None

# MetaTrader5 is a compiled extension. A frozen app cannot see site-packages,
# so it has to be collected at BUILD time -- installing it on the target
# machine afterwards does nothing. Without this the exe starts fine and then
# reports that the bridge is unavailable, which looks like a bug in the app
# rather than a missing build dependency.
try:
    mt5_datas, mt5_binaries, mt5_hidden = collect_all("MetaTrader5")
except Exception:
    mt5_datas, mt5_binaries, mt5_hidden = [], [], []
    print("WARNING: MetaTrader5 is not installed, so the built exe will not "
          "be able to connect to a terminal. Run `pip install MetaTrader5` "
          "before building.")

a = Analysis(
    [str(ROOT / "run_trader.py")],
    pathex=[str(ROOT / "src")],
    binaries=mt5_binaries,
    datas=mt5_datas,
    hiddenimports=[
        "cheese_signals",
        "cheese_signals.paths",
        "cheese_signals.markets",
        "cheese_signals.markets.config",
        "cheese_signals.markets.costs",
        "cheese_signals.markets.execution",
        "cheese_signals.markets.guards",
        "cheese_signals.markets.mt5_bridge",
        "cheese_signals.markets.news",
        "cheese_signals.markets.run",
        "cheese_signals.markets.strategy",
        "cheese_signals.markets.survey",
        "cheese_signals.markets.trader",
        "cheese_signals.notifiers",
        "cheese_signals.notifiers.telegram",
    ] + mt5_hidden,
    hookspath=[],
    runtime_hooks=[],
    # No GUI in this one: Qt would triple the size for nothing.
    excludes=[
        "tkinter",
        "matplotlib",
        "PySide6",
        "scipy",
        "sklearn",
        "IPython",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = ROOT / "packaging" / "kps.ico"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="KPSTrader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,           # the log IS the interface
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)
