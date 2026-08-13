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


def test_no_table_header_is_elided(window, app):
    """"Range window" rendered as "!ange windov". QTableWidget centres header
    text, so a column needs more width than the label alone."""
    window.nav_group.button(0).click()
    _settle(app)
    narrow = []
    for table in window.findChildren(QTableWidget):
        if not table.isVisible():
            continue
        header = table.horizontalHeader()
        metrics = header.fontMetrics()
        for col in range(table.columnCount()):
            item = table.horizontalHeaderItem(col)
            if item is None:
                continue
            needed = metrics.horizontalAdvance(item.text()) + 18
            if header.sectionSize(col) < needed:
                narrow.append((item.text(), header.sectionSize(col), needed))
    assert not narrow, f"elided headers: {narrow}"


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
