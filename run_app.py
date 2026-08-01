"""Entry point for the desktop app (and the PyInstaller build target)."""

from __future__ import annotations

import os
import sys

# When frozen by PyInstaller the package lives inside the bundle; in a source
# checkout it lives under src/.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cheese_signals.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
