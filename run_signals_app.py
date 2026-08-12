"""Entry point for ORB-Signals.exe (and for running the window from source).

A top-level shim rather than pointing PyInstaller at the GUI module directly:
PyInstaller executes its entry script as ``__main__`` with no package context, so
aiming it at ``gui/signals_app.py`` builds cleanly and then dies on the first
relative import -- a failure that appears only in the frozen binary.
"""

from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cheese_signals.gui.signals_app import main

if __name__ == "__main__":
    raise SystemExit(main())
