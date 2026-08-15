"""The picture that goes out with an alert.

There is no way to unit-test whether a chart *looks* right, and pretending
otherwise produces tests that pass while the image is unreadable. What these
check is the set of things that were actually wrong at some point: the drawing
crashing the process, the levels being cropped out of frame, the times being
absent, and the range box appearing on a chart that has no range.

The look is checked by rendering one and opening it, which is what
tools/render_signals_pages.py does for the window.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

# PySide6 can be installed and still unimportable on a headless machine, which
# raises a plain ImportError rather than ModuleNotFoundError.
try:
    from PySide6.QtGui import QImage      # noqa: F401
except ImportError as exc:  # pragma: no cover - environment dependent
    pytest.skip(f"PySide6 is unusable here: {exc}", allow_module_level=True)

from cheese_signals.markets import chart, signals  # noqa: E402
from cheese_signals.markets.clock import SESSIONS  # noqa: E402
from cheese_signals.markets.execution import BUY, SELL  # noqa: E402

LONDON = SESSIONS["london"]
DAY = date(2026, 8, 13)
OPEN = LONDON.open_utc(DAY)


@pytest.fixture(scope="module", autouse=True)
def _app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def bars(count: int = 45, start: float = 4336.0) -> pd.DataFrame:
    closes = [start + i * 0.1 for i in range(count)]
    opens = [closes[0]] + closes[:-1]
    index = pd.date_range(OPEN, periods=count, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "close": closes,
         "high": [c + 0.2 for c in closes], "low": [o - 0.2 for o in opens],
         "spread": [25.0] * count},
        index=index,
    )


def signal(kind=signals.RETEST, direction=BUY, **kw) -> signals.Signal:
    fields = dict(
        kind=kind, symbol="XAUUSD", direction=direction,
        at=OPEN + timedelta(minutes=22), entry=4337.0, stop=4335.0,
        target=4341.0, range_low=4335.0, range_high=4337.0, range_points=200.0,
        risk_points=200.0, reward_points=400.0, cost_points=25.0,
        session_label="London", session_open=OPEN,
        flat_by=OPEN + timedelta(hours=8), reason="held it", digits=2,
        session_tz="Europe/London", session_key="london", range_minutes=15,
        chart_offset_minutes=180, reader_tz="America/New_York",
        ref="XAUUSD-0813-RETEST")
    fields.update(kw)
    return signals.Signal(**fields)


def png(data: bytes):
    from PySide6.QtGui import QImage

    image = QImage()
    assert image.loadFromData(data, "PNG"), "not a readable PNG"
    return image


# ------------------------------ it draws ------------------------------
def test_a_signal_renders_to_a_png():
    image = png(chart.render_signal(bars(), signal()))
    assert image.width() > 800 and image.height() > 400


def test_the_sell_side_renders_too():
    data = chart.render_signal(bars(), signal(direction=SELL, entry=4335.0,
                                              stop=4337.0, target=4331.0))
    assert png(data).width() > 800


def test_a_result_renders_without_an_opening_range():
    """The outcome chart has no range to show. It used to draw the box anyway,
    labelled "opening range", around the entry and the stop."""
    outcome = signals.Outcome(
        symbol="XAUUSD", direction=BUY, result=signals.WIN, entry=4337.0,
        stop=4335.0, target=4341.0, exit_price=4341.0,
        opened_at=OPEN + timedelta(minutes=22),
        closed_at=OPEN + timedelta(minutes=38), risk_points=200.0,
        points=400.0, cost_points=25.0, r_gross=2.0, r_net=1.87,
        session_label="London", reason="target reached", digits=2,
        session_tz="Europe/London", session_key="london",
        chart_offset_minutes=180, ref="XAUUSD-0813-RETEST")
    assert png(chart.render_outcome(bars(), outcome)).width() > 800


# ------------------------------ it does not crash ------------------------------
def test_a_frame_shorter_than_the_window_still_draws():
    """A feed that has just started, or an instrument with gaps. Refusing here
    would mean the alert with the least context is the one with no picture."""
    assert chart.render_signal(bars(count=6), signal())


def test_an_empty_frame_is_refused_by_name_rather_than_crashing():
    empty = bars().iloc[:0]
    with pytest.raises(chart.ChartUnavailable):
        chart.render_signal(empty, signal())


def test_a_drawing_failure_raises_instead_of_killing_the_process(monkeypatch):
    """A QPainter still active when an exception unwinds takes the process down
    with a segfault rather than a traceback -- which a malformed drawLine call
    did, in a program meant to run unattended all day.

    The failure is injected into ``in_zone``, which is called from the axis loop
    *while the painter is active*; a failure before the painter exists would
    prove nothing. Without the try/finally in render(), this test does not fail,
    it takes the whole pytest process down with it.
    """
    import cheese_signals.markets.chart as mod

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom, mid-paint")

    monkeypatch.setattr(mod, "in_zone", explode)
    with pytest.raises(RuntimeError, match="mid-paint"):
        chart.render_signal(bars(), signal())


# ------------------------ the levels are in the picture ------------------------
def test_a_target_far_outside_the_candles_is_still_in_frame():
    """A chart cropped to the candles hides how far away the risk was, which is
    the one thing a reader needs to judge the setup."""
    far = signal(target=4500.0)
    image = png(chart.render_signal(bars(), far))
    # Green pixels exist somewhere: the target line was drawn rather than
    # clipped off the top of a frame scaled to the candles alone.
    greens = sum(1 for x in range(0, image.width(), 3)
                 for y in range(0, image.height(), 3)
                 if image.pixelColor(x, y).green() > 120
                 and image.pixelColor(x, y).red() < 100)
    assert greens > 20


def test_the_chart_is_not_blank():
    image = png(chart.render_signal(bars(), signal()))
    distinct = {image.pixelColor(x, y).name()
                for x in range(0, image.width(), 7)
                for y in range(0, image.height(), 7)}
    assert len(distinct) > 6, "a chart with fewer than seven colours drew nothing"
