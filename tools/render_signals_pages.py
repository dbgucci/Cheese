"""Render every page of the signals window to a PNG.

    python tools/render_signals_pages.py out_dir

Exists because the Settings page shipped unreadable and the tests at the time
all passed. The layout bugs -- a missing scroll area, controls pushed to the far
right edge of the window, a clipped brand mark, an elided table header -- are
invisible to logic tests and obvious in a picture. ``tests/test_gui_signals_layout``
now asserts the specific properties those bugs violated, but a test can only
catch a defect someone has already thought of. A screenshot catches the next one.

Run in CI after the build so each artifact carries images of the app it contains,
and a layout regression is visible without a Windows machine or an install.
Offscreen, so it needs no display.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Both must be set before QApplication exists. CHEESE_SIGNALS_HOME keeps the
# render away from real saved settings -- and lets it run on a machine that has
# none, which every CI runner is.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WIDTH, HEIGHT = 1680, 1040


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "screenshots").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CHEESE_SIGNALS_HOME", str(out / "_settings"))

    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cheese_signals.gui import theme
    from cheese_signals.gui.signals_app import SignalsWindow

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())

    win = SignalsWindow()
    win.resize(WIDTH, HEIGHT)
    win.show()
    for _ in range(40):
        app.processEvents()

    names = ["signals", "settings", "activity"]
    written = []
    for index, name in enumerate(names):
        win.nav_group.button(index).click()
        for _ in range(40):
            app.processEvents()
        path = out / f"{name}.png"
        pixmap = win.grab()
        if pixmap.isNull() or not pixmap.save(str(path)):
            print(f"FAILED to render {name}")
            return 1
        written.append(path)
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")

    # The Settings page is the tall one: grab it scrolled to the bottom too, so
    # the half a screenshot cannot show is in the artifact as well.
    from PySide6.QtWidgets import QScrollArea

    win.nav_group.button(1).click()
    for _ in range(40):
        app.processEvents()
    areas = win.settings_page.findChildren(QScrollArea)
    if areas:
        bar = areas[0].verticalScrollBar()
        bar.setValue(bar.maximum())
        for _ in range(40):
            app.processEvents()
        path = out / "settings-bottom.png"
        win.grab().save(str(path))
        print(f"wrote {path}")
    else:
        print("WARNING: the settings page has no scroll area, so it cannot "
              "scroll -- tall content will be compressed instead")

    _sample(app, win, out)
    win.close()
    return 0


def _sample(app, win, out) -> None:
    """One more shot of the Signals page with rows in the tables.

    An empty table proves the headers fit and nothing else. The alignment of the
    values under them, and whether a result reads clearly next to the entry it
    belongs to, only show up with data in the rows -- and this app's tables are
    empty until a market opens, so a build would otherwise never picture them.
    The numbers below are invented; they exist to be looked at, not believed.
    """
    from datetime import datetime, timedelta, timezone

    from cheese_signals.markets import signals as sig
    from cheese_signals.markets.execution import BUY, SELL

    now = datetime(2026, 3, 2, 14, 46, tzinfo=timezone.utc)

    def signal(kind, symbol, direction, entry, stop, target, digits, at):
        return sig.Signal(
            kind=kind, symbol=symbol, direction=direction, at=at, entry=entry,
            stop=stop, target=target, range_low=entry - 20, range_high=entry,
            range_points=200.0, risk_points=200.0, reward_points=400.0,
            cost_points=12.0, session_label="US cash equities",
            session_open=now - timedelta(minutes=16),
            flat_by=now + timedelta(hours=6), reason="sample", digits=digits)

    rows = [
        signal(sig.BREAK, "US30", BUY, 44015.0, 43990.0, 44065.0, 1, now),
        signal(sig.RETEST, "US30", BUY, 44010.0, 43990.0, 44050.0, 1,
               now + timedelta(minutes=4)),
        signal(sig.BREAK, "XAUUSD", SELL, 2412.40, 2418.90, 2399.40, 2,
               now + timedelta(minutes=9)),
    ]
    for row in rows:
        win.feed.add_signal(row)

    entry = rows[1]
    win.feed.set_result(sig.Outcome(
        symbol="US30", direction=BUY, result=sig.WIN, entry=entry.entry,
        stop=entry.stop, target=entry.target, exit_price=entry.target,
        opened_at=entry.at, closed_at=entry.at + timedelta(minutes=23),
        risk_points=200.0, points=400.0, cost_points=12.0, r_gross=2.0,
        r_net=1.94, session_label="US cash equities",
        reason="the target at 44050.0 was reached", digits=1))
    win.tally.add(sig.Outcome(
        symbol="US30", direction=BUY, result=sig.WIN, entry=1.0, stop=0.9,
        target=1.2, exit_price=1.2, opened_at=now, closed_at=now,
        risk_points=200.0, points=400.0, cost_points=12.0, r_gross=2.0,
        r_net=1.94, session_label="US cash equities", reason=""))
    win.tally.add(sig.Outcome(
        symbol="XAUUSD", direction=SELL, result=sig.LOSS, entry=1.0, stop=1.1,
        target=0.8, exit_price=1.1, opened_at=now, closed_at=now,
        risk_points=65.0, points=-65.0, cost_points=4.0, r_gross=-1.0,
        r_net=-1.06, session_label="London", reason=""))
    win.refresh_record_tile()
    win.feed.set_states([
        {"symbol": "US30", "window": "14:30-14:45", "range": "200 pts",
         "state": "retested", "detail": "came back to 44010.0 and held it"},
        {"symbol": "XAUUSD", "window": "08:00-08:15", "range": "64 pts",
         "state": "skipped", "detail": "the range is 1.8x the round-trip cost, "
                                       "below the 3.0x minimum"},
    ])

    win.nav_group.button(0).click()
    for _ in range(40):
        app.processEvents()
    path = out / "signals-sample.png"
    win.grab().save(str(path))
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
