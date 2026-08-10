"""Entry point for the opening-range autobot (and the PyInstaller build target).

This file exists because a PyInstaller entry script is executed as ``__main__``
with no package context, so pointing the build at ``markets/launcher.py``
directly produces an exe that dies immediately on its first relative import --
a failure that appears only in the frozen binary and never when running from
source. A thin top-level shim is the standard fix, and it is the same shape as
``run_app.py`` next to it.
"""

from __future__ import annotations

import os
import sys

# When frozen by PyInstaller the package lives inside the bundle; in a source
# checkout it lives under src/.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cheese_signals.markets.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
