# PyInstaller spec: builds a single-file Windows executable.
#
#   pyinstaller packaging/KPS.spec --noconfirm
#
# Produces dist/KPS.exe -- no Python install required on the
# target machine.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller; fall back to cwd when linted standalone.
ROOT = Path(globals().get("SPECPATH", ".")).resolve().parent

block_cipher = None

# The Pocket Option client is a compiled Rust/PyO3 extension with subpackages
# and a native .pyd. PyInstaller's static analysis does not follow it, so it
# must be collected explicitly -- otherwise the frozen exe raises
# "binaryoptionstoolsv2 is not installed" no matter what the user pip installs
# on their machine, because a frozen app cannot see site-packages at all.
try:
    po_datas, po_binaries, po_hiddenimports = collect_all("BinaryOptionsToolsV2")
except Exception:
    # Building without the optional live-feed dependency installed: the app
    # still builds and runs, with the Pocket Option feed unavailable.
    po_datas, po_binaries, po_hiddenimports = [], [], []
    print("WARNING: BinaryOptionsToolsV2 not found; the built exe will not "
          "support the live Pocket Option feed. Run "
          "`pip install binaryoptionstoolsv2` before building to include it.")

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=po_binaries,
    datas=po_datas,
    hiddenimports=[
        "cheese_signals",
        "cheese_signals.gui",
        "cheese_signals.gui.app",
        "cheese_signals.gui.theme",
        "cheese_signals.gui.widgets",
        "cheese_signals.gui.branding",
        "cheese_signals.gui.settings_page",
        "cheese_signals.setups",
        "cheese_signals.triggers",
        "cheese_signals.execution",
        "cheese_signals.diagnostics",
        "cheese_signals.trend",
        "cheese_signals.strategy_lab",
        "cheese_signals.data",
        "cheese_signals.data.synthetic",
        "cheese_signals.data.csv_feed",
        # Imported lazily at runtime, so static analysis never sees it.
        "cheese_signals.data.pocket_option",
        "cheese_signals.notifiers",
        "cheese_signals.notifiers.telegram",
    ] + po_hiddenimports,
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

# Generate the KPS icon at build time so no binary asset is committed.
icon_path = ROOT / "packaging" / "kps.ico"
try:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "src"))
    from cheese_signals.gui.branding import write_ico
    write_ico(str(icon_path))
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
    name="KPS",
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
