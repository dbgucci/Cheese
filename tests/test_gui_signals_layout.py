"""Layout invariants for the signals window.

Every bug this file guards against actually shipped. The window opened, said
"Watching", and was unusable: the Settings page had no scroll area, so Qt
compressed rather than scrolled it -- four lines of Telegram instructions became
one clipped line and every spin box became a sliver. The controls sat at the far
right edge of a 1920px window because each row was its own QHBoxLayout with a
stretched label. The brand read "ORB Signal". A table header read "!ange windov".

None of that is catchable by testing the logic underneath, and all of it is
obvious in a rendered window -- so these tests render one. They assert the
properties a screenshot would have revealed, which is the part a person forgets
to re-check after the next change.
"""

import pytest

# PySide6 can be installed and still unimportable: on a headless machine its Qt
# libraries are often absent, which raises a plain ImportError rather than
# ModuleNotFoundError -- and pytest.importorskip only skips on the latter.
try:
    from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QLabel,
                                   QPushButton, QScrollArea, QTableWidget)
except ImportError as exc:  # pragma: no cover - environment dependent
    pytest.skip(f"PySide6 is unusable here: {exc}", allow_module_level=True)

from cheese_signals.gui import theme  # noqa: E402
from cheese_signals.gui.signals_app import SignalsWindow  # noqa: E402

WIDTH, HEIGHT = 1920, 1040


@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.stylesheet())
    return instance


@pytest.fixture(scope="module")
def window(app, tmp_path_factory, request):
    import os

    os.environ["CHEESE_SIGNALS_HOME"] = str(tmp_path_factory.mktemp("orb-gui"))
    win = SignalsWindow()
    win.resize(WIDTH, HEIGHT)
    win.show()
    app.processEvents()
    request.addfinalizer(win.close)
    return win


def _settle(app, times: int = 25) -> None:
    for _ in range(times):
        app.processEvents()


def _visible_labels(root):
    return [w for w in root.findChildren(QLabel) if w.isVisible() and w.text()]


def _fill_tables(window):
    """Put realistic rows in both tables.

    The empty tables prove only that the headers fit. Every cell-level clipping
    bug -- and there was one, "14:46" rendered as "14:..." -- needs data in the
    rows to show up, and this app's tables are empty until a market opens.
    """
    from datetime import datetime, timedelta, timezone

    from cheese_signals.markets import signals as sig
    from cheese_signals.markets.execution import BUY, SELL

    window.nav_group.button(0).click()
    window.feed.signal_table.setRowCount(0)
    window.feed._result_cells.clear()
    at = datetime(2026, 3, 2, 14, 46, tzinfo=timezone.utc)

    def signal(kind, symbol, direction, entry, stop, target, digits, when):
        return sig.Signal(
            kind=kind, symbol=symbol, direction=direction, at=when, entry=entry,
            stop=stop, target=target, range_low=entry - 20, range_high=entry,
            range_points=200.0, risk_points=200.0, reward_points=400.0,
            cost_points=12.0, session_label="US cash equities",
            session_open=at, flat_by=at + timedelta(hours=6), reason="t",
            digits=digits)

    retest = signal(sig.RETEST, "US30", BUY, 44010.0, 43990.0, 44050.0, 1, at)
    window.feed.add_signal(retest)
    window.feed.add_signal(signal(sig.BREAK, "XAUUSD", SELL, 2412.40, 2418.90,
                                  2399.40, 2, at + timedelta(minutes=9)))
    window.feed.set_result(sig.Outcome(
        symbol="US30", direction=BUY, result=sig.WIN, entry=44010.0,
        stop=43990.0, target=44050.0, exit_price=44050.0, opened_at=at,
        closed_at=at + timedelta(minutes=23), risk_points=200.0, points=400.0,
        cost_points=12.0, r_gross=2.0, r_net=1.94,
        session_label="US cash equities", reason="the target was reached",
        digits=1))
    window.feed.set_states([
        {"symbol": "XAUUSD247", "window": "08:00-08:15", "range": "640 pts",
         "state": "retested", "detail": "came back to 2412.40 and held it"},
        {"symbol": "US30", "window": "14:30-14:45", "range": "200 pts",
         "state": "range forming", "detail": ""},
    ])
    return retest


# ------------------------------ the window opens ------------------------------
def test_the_window_builds_and_has_its_three_pages(window):
    assert window.stack.count() == 3
    assert window.windowTitle() == "ORB Signals"


def test_it_says_it_cannot_trade(window):
    """The claim is on the face of the window, not only in a docstring."""
    text = " ".join(lab.text() for lab in _visible_labels(window))
    assert "cannot place a trade" in text


# ------------------------------ the scroll area ------------------------------
def test_the_settings_page_scrolls_rather_than_compressing(window, app):
    """Without this the page is not scrollable, it is squashed: Qt shrinks every
    widget to fit and multi-line labels lose all but a clipped first line."""
    window.nav_group.button(1).click()
    _settle(app)
    assert window.settings_page.findChildren(QScrollArea), \
        "the settings page has no scroll area"


def test_the_settings_content_is_taller_than_the_window(window, app):
    """Which is why the scroll area is needed rather than optional."""
    window.nav_group.button(1).click()
    _settle(app)
    area = window.settings_page.findChildren(QScrollArea)[0]
    assert area.widget().sizeHint().height() > HEIGHT - 200


# ------------------------------ nothing is clipped ------------------------------
def test_no_label_on_the_settings_page_is_clipped(window, app):
    """The failure mode that made the first build unreadable: a label narrower
    than the text it holds renders elided or cut off."""
    window.nav_group.button(1).click()
    _settle(app)
    clipped = []
    for label in _visible_labels(window.settings_page):
        if label.wordWrap():
            continue                       # a wrapped label is allowed to be narrow
        if label.width() + 1 < label.sizeHint().width():
            clipped.append((label.text()[:48], label.width(),
                            label.sizeHint().width()))
    assert not clipped, f"clipped labels: {clipped}"


def test_the_brand_mark_fits_the_sidebar(window):
    """232px rendered "ORB Signals" as "ORB Signal": the mark is letter-spaced,
    so it needs more room than its character count suggests."""
    brands = [lab for lab in _visible_labels(window)
              if lab.objectName() == "BrandMark"]
    assert brands, "no brand mark found"
    for brand in brands:
        assert brand.width() + 1 >= brand.sizeHint().width(), \
            f"brand '{brand.text()}' is clipped"


def test_the_sidebar_widens_for_a_brand_mark_that_does_not_fit(window, app):
    """The mechanism, not the current numbers.

    The plain "is the brand clipped" test above passed locally on 252px and the
    Windows build clipped anyway, where the same letter-spaced text measures
    286px -- a fixed width cannot be right on both. So this enlarges the mark
    until it cannot possibly fit and asserts the sidebar follows it, which is
    what makes the platform's own metrics irrelevant.
    """
    brand = window._brand_labels[0]
    before = window._sidebar.width()
    original = brand.styleSheet()
    try:
        brand.setStyleSheet("font-size: 44px; letter-spacing: 9px;")
        brand.updateGeometry()
        window._fit_sidebar()
        _settle(app)
        assert window._sidebar.width() > before
        assert brand.width() + 1 >= brand.sizeHint().width()
    finally:
        brand.setStyleSheet(original)
        brand.updateGeometry()
        window._fit_sidebar()
        _settle(app)


def test_a_column_widens_for_a_header_that_does_not_fit(window, app):
    """Same argument for the tables: 160px held "Range window" here and elided
    it on Windows, which needs 162. The requested width is a floor."""
    from PySide6.QtGui import QFont

    window.nav_group.button(0).click()
    _settle(app)
    table = window.feed.state_table
    header = table.horizontalHeader()
    before = [table.columnWidth(c) for c in range(table.columnCount())]
    original = header.font()
    try:
        big = QFont(original)
        big.setPointSize(original.pointSize() + 14 if original.pointSize() > 0
                         else 30)
        header.setFont(big)
        table.fit_columns()
        _settle(app)
        widened = [c for c in range(table.columnCount())
                   if table.columnWidth(c) > before[c]]
        assert widened, "no column grew for a header that no longer fits"
    finally:
        header.setFont(original)
        table.fit_columns()
        _settle(app)


def test_no_table_header_is_elided(window, app):
    """"Range window" rendered as "!ange windov".

    Measured with Qt's own sectionSizeHint rather than fontMetrics plus a guess
    at the padding, because that guess is what let the first version pass here
    and clip on Windows: the stylesheet's 14px of section padding and its
    letter-spacing are invisible to fontMetrics and included in the hint.
    """
    window.nav_group.button(0).click()
    _settle(app)
    narrow = []
    for table in window.findChildren(QTableWidget):
        if not table.isVisible():
            continue
        header = table.horizontalHeader()
        for col in range(table.columnCount()):
            item = table.horizontalHeaderItem(col)
            if item is None:
                continue
            needed = header.sectionSizeHint(col)
            if header.sectionSize(col) < needed:
                narrow.append((item.text(), header.sectionSize(col), needed))
    assert not narrow, f"elided headers: {narrow}"


def test_no_cell_is_elided_once_the_tables_have_rows(window, app):
    """The headers fitting proves nothing about the values under them: 70px held
    "Time" and elided "14:46" to "14:...", because a cell carries 14px of
    stylesheet padding on each side that fontMetrics cannot see."""
    _fill_tables(window)
    _settle(app)
    narrow = []
    for table in (window.feed.signal_table, window.feed.state_table):
        stretch = table._stretch
        for col in range(table.columnCount()):
            if col == stretch:
                continue           # takes the slack; its text is allowed to wrap out
            needed = table.sizeHintForColumn(col)
            if table.columnWidth(col) < needed:
                texts = [table.item(r, col).text() for r in range(table.rowCount())
                         if table.item(r, col) is not None]
                narrow.append((texts, table.columnWidth(col), needed))
    assert not narrow, f"elided cells: {narrow}"


def test_a_result_lands_on_the_row_of_the_entry_it_belongs_to(window, app):
    """Rows are inserted at the top, so a row index recorded when the retest
    fired points at a later signal by the time the result arrives."""
    _fill_tables(window)
    _settle(app)
    table = window.feed.signal_table
    rows = {}
    for r in range(table.rowCount()):
        rows[(table.item(r, 1).text(), table.item(r, 2).text())] = r
    retest_row = rows[("RETEST", "US30")]
    assert "WIN" in table.item(retest_row, 8).text()
    for (stage, _symbol), r in rows.items():
        if stage != "RETEST":
            assert table.item(r, 8).text() in ("", None) or not table.item(r, 8).text()


# ------------------------------ controls are usable ------------------------------
def test_every_control_has_a_usable_size(window, app):
    """Compressed spin boxes and buttons were a few pixels tall and unreadable."""
    window.nav_group.button(1).click()
    _settle(app)
    tiny = []
    for widget in (window.settings_page.findChildren(QAbstractSpinBox)
                   + window.settings_page.findChildren(QPushButton)):
        if not widget.isVisible():
            continue
        if widget.height() < 22 or widget.width() < 40:
            tiny.append((type(widget).__name__, widget.width(), widget.height()))
    assert not tiny, f"unusably small controls: {tiny}"


def test_the_spin_boxes_show_their_values(window, app):
    """They rendered as empty bars, which looked like broken styling and was a
    squashed layout."""
    window.nav_group.button(1).click()
    _settle(app)
    page = window.settings_page
    assert page.range_minutes.text().startswith("15")
    assert page.target_r.text().startswith("2")
    assert page.window_minutes.text().startswith("120")


def test_controls_sit_beside_their_labels_not_at_the_screen_edge(window, app):
    """Each row was its own QHBoxLayout with a stretched label, which pushed
    every control to the far right of a wide window."""
    window.nav_group.button(1).click()
    _settle(app)
    page = window.settings_page
    for control in (page.range_minutes, page.target_r, page.window_minutes):
        left = control.mapTo(window, control.rect().topLeft()).x()
        assert left < WIDTH * 0.55, \
            f"{type(control).__name__} starts at x={left} on a {WIDTH}px window"


# ------------------------------ it renders ------------------------------
def test_the_window_paints_without_raising(window, app, tmp_path):
    """The cheapest possible check that the whole tree can actually be drawn."""
    for index in range(window.stack.count()):
        window.nav_group.button(index).click()
        _settle(app)
        pixmap = window.grab()
        assert not pixmap.isNull()
        assert pixmap.width() == WIDTH
