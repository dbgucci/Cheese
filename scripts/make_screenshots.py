"""Regenerate docs/screenshots/*.png from the real app.

Seeds a throwaway journal with plausible demo trades, renders each page
offscreen, and writes the PNGs the README links to. Run it after any visual
change so the README never advertises a theme the app no longer has.

    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHEESE_SIGNALS_HOME"] = tempfile.mkdtemp(prefix="kps-shots-")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cheese_signals import storage  # noqa: E402
from cheese_signals.gui import theme  # noqa: E402
from cheese_signals.gui.app import MainWindow  # noqa: E402
from cheese_signals.scheduler import schedule_signal  # noqa: E402
from cheese_signals.strategies import DOWN, UP  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
PAIRS = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDCAD_otc", "EURJPY_otc"]
REASONS_WIN = [
    "trend held: price never traded back through the broken swing high",
    "entered on the break, expiry closed 0.8 ATR beyond the level",
    "pullback resolved in the trend direction as the setup expected",
]
REASONS_LOSS = [
    "broke structure then reversed inside the same candle -- false break",
    "ADX 12: the 'trend' was noise, and the break did not follow through",
    "expiry closed 1 pip against entry -- a coin-flip outcome, not a read",
]


def seed(journal: storage.Journal, n: int = 140) -> None:
    rng = random.Random(11)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    for i in range(n):
        detected = now - timedelta(minutes=(n - i) * 7)
        asset = rng.choice(PAIRS)
        direction = rng.choice([UP, DOWN])
        score = round(rng.uniform(0.58, 0.86), 2)
        sig = schedule_signal(
            asset, direction, score,
            "trend_continuation + bos",
            "broke structure: close 1.10412 > swing high 1.10380",
            detected, "london_ny_overlap", lead_minutes=1, expiry_minutes=1,
        )
        signal_id = journal.record_signal(
            asset, direction, score, sig.strategy, sig.reason,
            detected, sig.entry_at, sig.expiry_at, sig.session,
            sig.entry_at.hour, {"adx": round(rng.uniform(8, 34), 1)},
        )
        won = rng.random() < 0.53
        entry = round(rng.uniform(0.9, 1.4), 5)
        move = rng.uniform(0.00004, 0.0004) * (1 if won == (direction == UP) else -1)
        journal.record_outcome(
            signal_id, asset, direction, entry, round(entry + move, 5), won,
            0.85, 10.0, sig.expiry_at,
            rng.choice(REASONS_WIN if won else REASONS_LOSS),
        )


def main() -> None:
    j = storage.Journal()
    seed(j)
    j.close()

    app = QApplication([])
    app.setStyleSheet(theme.stylesheet())
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    app.processEvents()

    OUT.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(["live", "history", "analytics", "diagnostics", "settings"]):
        button = w.nav_group.button(index)
        if button is None:
            break
        button.click()
        app.processEvents()
        app.processEvents()
        path = OUT / f"{name}.png"
        w.grab().save(str(path))
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
