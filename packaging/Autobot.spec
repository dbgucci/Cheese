# PyInstaller spec: the standalone opening-range autobot.
#
#   pyinstaller packaging/Autobot.spec --noconfirm
#
# Produces dist/ORB-Autobot.exe -- no Python needed on the target machine.
#
# Two things about this build are not obvious:
#
# 1. **It must be built on Windows.** Not because of PyInstaller's usual
#    inability to cross-compile, though that applies too, but because the
#    MetaTrader5 package exists only for Windows. A Linux build would produce a
#    binary whose entire purpose was missing.
#
# 2. **MetaTrader5 must be installed before building.** A frozen exe cannot see
#    site-packages on the user's machine, so a package that is missing at build
#    time is missing forever -- and the failure arrives at runtime as an
#    ImportError the user cannot fix by installing anything.
#
# This is a console app on purpose. It is a trading bot whose output is a
# running log of what it did and refused to do; hiding that behind a windowless
# process would leave the user with no way to see either.
#
# The entry point is the top-level run_autobot.py rather than the launcher
# module itself. PyInstaller executes its entry script as __main__ with no
# package context, so aiming it at markets/launcher.py builds cleanly and then
# dies on the first relative import -- a failure that exists only in the frozen
# binary and never when running from source.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller; fall back to cwd when linted standalone.
ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

block_cipher = None

# MetaTrader5 is a compiled extension. PyInstaller's static analysis handles the
# simple case, but collecting it explicitly is what makes the difference on the
# machines where it does not.
try:
    mt5_datas, mt5_binaries, mt5_hiddenimports = collect_all("MetaTrader5")
except Exception:
    mt5_datas, mt5_binaries, mt5_hiddenimports = [], [], []
    print("=" * 70)
    print("WARNING: MetaTrader5 is not installed in this build environment.")
    print("The exe will build and will start, but it will not be able to reach")
    print("a broker -- it fails at the connection step with an ImportError that")
    print("the user cannot fix by installing anything, because a frozen exe")
    print("cannot load packages from their machine.")
    print("Run `pip install MetaTrader5` on this Windows machine, then rebuild.")
    print("=" * 70)

a = Analysis(
    [str(ROOT / "run_autobot.py")],
    pathex=[str(ROOT / "src")],
    binaries=mt5_binaries,
    datas=mt5_datas,
    hiddenimports=[
        "cheese_signals",
        "cheese_signals.paths",
        "cheese_signals.markets",
        "cheese_signals.markets.autobot",
        "cheese_signals.markets.clock",
        "cheese_signals.markets.costs",
        "cheese_signals.markets.execution",
        "cheese_signals.markets.guards",
        "cheese_signals.markets.mt5_bridge",
        "cheese_signals.markets.orb",
        "cheese_signals.markets.orb_backtest",
        "cheese_signals.markets.survey",
        "cheese_signals.markets.launcher",
        # Imported inside functions so the module loads on non-Windows for the
        # test suite, which means static analysis never sees them.
        "MetaTrader5",
        "zoneinfo",
    ] + mt5_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # None of the GUI or plotting stack is reachable from the autobot, and
    # excluding it takes the exe from hundreds of megabytes to tens.
    excludes=[
        "tkinter",
        "matplotlib",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "BinaryOptionsToolsV2",
        "cheese_signals.gui",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ORB-Autobot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,           # the log *is* the interface; never hide it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
