"""Research tools: find out whether an edge exists before building on one.

The rest of this project builds and runs strategies. This package asks the
prior question -- does the data support *any* strategy? -- and is deliberately
built so that the answer is allowed to be no.

Entry point: ``python run_research.py`` (or ``research_windows.bat``).
"""

from . import dataset, features, mine, randomwalk, report, stats  # noqa: F401
from .stats import Payout  # noqa: F401

__all__ = [
    "dataset",
    "features",
    "mine",
    "randomwalk",
    "report",
    "stats",
    "Payout",
]
