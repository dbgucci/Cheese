"""Run the pivot backtest straight from a checkout, with nothing installed.

    python run_pivot.py NAS100_M5.csv
    python run_pivot.py --synthetic 60000

Exists because `python -m cheese_signals...` needs the package on sys.path,
which means either installing it or knowing to set PYTHONPATH -- and neither
is worth a round trip when the code is sitting right there in ./src.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    import scipy  # noqa: F401
except ImportError:
    sys.exit("this needs scipy and pandas:\n"
             "    python -m pip install pandas numpy scipy")

from cheese_signals.research.pivot_retest import main

raise SystemExit(main())
