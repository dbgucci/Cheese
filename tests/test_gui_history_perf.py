"""The History tab must open instantly no matter how large the journal is.

This is a regression guard for a real, shipped bug: History used a
QTableWidget whose columns were set to ResizeToContents. Populating one that
is already laid out inside a visible window re-measures every cell on every
``setItem`` call, which cost 23 ms *per cell*. At 500 rows x 8 columns that
was 91 seconds to open the tab -- experienced as "the app freezes more the
more history I collect".

Two tests, deliberately different in kind:

- a structural one, which fails fast and explains itself if someone
  reintroduces content-based sizing or a widget-item table;
- a timing one, which is the property actually being promised.
"""

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QAbstractTableModel  # noqa: E402
from PySide6.QtWidgets import QApplication, QHeaderView  # noqa: E402

from cheese_signals.gui import models  # noqa: E402


@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _rows(n: int) -> list[dict]:
    return [
        {
            "asset": "EURUSD_otc", "direction": 1 if i % 2 else -1, "score": 0.72,
            "strategy": "trend_continuation + bos", "entry_at": f"2026-08-01T{i % 24:02d}:00:00+00:00",
            "won": i % 2, "pnl": 8.5 if i % 2 else -10.0, "session": "london_ny_overlap",
            "outcome_reason": "expiry closed beyond the level in the signal direction",
        }
        for i in range(n)
    ]


@pytest.fixture
def page(app):
    """A HistoryPage wired to a stub window, shown, as it is in the real app.

    Shown matters: the bug only appeared once the widget was visible and
    laid out, which is why an offscreen-but-hidden test would have missed it.
    """
    from PySide6.QtWidgets import QMainWindow

    from cheese_signals.gui.app import HistoryPage

    class _Journal:
        def __init__(self):
            self.rows = _rows(5_000)

        def joined_results(self, limit=None):
            return self.rows[:limit] if limit else self.rows

    class _Window:
        journal = _Journal()

    host = QMainWindow()
    p = HistoryPage(_Window())
    host.setCentralWidget(p)
    host.resize(1400, 900)
    host.show()
    app.processEvents()
    yield p
    host.close()


def test_history_uses_a_model_not_per_cell_widgets(page):
    assert isinstance(page.model, QAbstractTableModel)
    assert page.table.model() is page.model


def test_no_column_measures_its_contents(page):
    """ResizeToContents is the specific setting that caused the freeze."""
    header = page.table.horizontalHeader()
    modes = [header.sectionResizeMode(i) for i in range(page.model.columnCount())]
    assert QHeaderView.ResizeMode.ResizeToContents not in modes, (
        "a content-sized column re-measures every row on every update"
    )


def test_rows_have_a_fixed_height(page):
    """Per-row height calculation is the other way this becomes O(rows)."""
    assert page.table.verticalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed


def test_opening_a_large_history_is_fast(page, app):
    page.refresh()
    app.processEvents()

    t = time.perf_counter()
    for _ in range(3):
        page.refresh()
        app.processEvents()
    per_refresh_ms = (time.perf_counter() - t) / 3 * 1000

    assert page.model.rowCount() == 5_000
    # The old implementation took ~91_000 ms for 500 rows. A budget of one
    # second for ten times the rows is loose enough not to be flaky on a
    # slow CI box, and still fails by three orders of magnitude if the
    # per-cell behaviour ever returns.
    assert per_refresh_ms < 1000, f"History refresh took {per_refresh_ms:.0f} ms"


def test_search_filters_without_touching_the_database(page, app):
    page.refresh()
    app.processEvents()
    full = page.model.rowCount()

    page.search.setText("gbpusd")
    app.processEvents()
    assert page.model.rowCount() == 0

    page.search.setText("eurusd")
    app.processEvents()
    assert page.model.rowCount() == full


def test_search_matches_derived_words(page, app):
    """"win"/"buy" are rendered, not stored, and must still be searchable."""
    page.refresh()
    app.processEvents()

    page.search.setText("loss")
    app.processEvents()
    losses = page.model.rowCount()
    assert 0 < losses < 5_000

    for row in range(losses):
        index = page.model.index(row, models.RESULT)
        assert page.model.data(index) == "LOSS"


def test_headers_and_values_share_an_alignment(app):
    """A right-aligned number under a centred header reads as broken."""
    from PySide6.QtCore import Qt

    model = models.TradeTableModel(_rows(5))
    for col in range(model.columnCount()):
        header = model.headerData(
            col, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole
        )
        cell = model.data(model.index(0, col), Qt.ItemDataRole.TextAlignmentRole)
        expected_right = col in (models.CONF, models.PNL)
        assert bool(header & int(Qt.AlignmentFlag.AlignRight)) == expected_right
        if expected_right:
            assert cell is not None and cell & int(Qt.AlignmentFlag.AlignRight)


def test_every_column_has_a_starting_width(app):
    """One width per column except the last, which stretches."""
    assert len(models.COLUMN_WIDTHS) == len(models.COLUMNS) - 1
    assert all(w > 0 for w in models.COLUMN_WIDTHS)
