"""A watchlist must survive however it was pasted.

The field split on newlines only, so sixteen pairs pasted as one row became a
single 200-character "symbol". Pocket Option rejected it with
``Invalid asset: AUDCAD_otc AUDCHF_otc ...`` followed by boilerplate about
authentication -- so a settings mistake presented as an expired SSID, and the
user re-issued their SSID twice for a problem that had nothing to do with it.
"""

import pytest

from cheese_signals.settings import Settings, parse_assets

PASTED_ON_ONE_LINE = (
    "AUDCAD_otc AUDCHF_otc AUDJPY_otc AUDUSD_otc CADJPY_otc CHFJPY_otc EURCHF_otc "
    "EURGBP_otc EURJPY_otc EURUSD_otc GBPAUD_otc GBPJPY_otc GBPUSD_otc USDCAD_otc "
    "USDCHF_otc  USDJPY_otc"
)


def test_the_exact_paste_that_broke_it():
    valid, rejected = parse_assets(PASTED_ON_ONE_LINE)
    assert len(valid) == 16
    assert valid[0] == "AUDCAD_otc"
    assert valid[-1] == "USDJPY_otc"
    assert rejected == []
    # The double space between USDCHF_otc and USDJPY_otc must not produce an
    # empty entry -- that is what the original paste actually contained.
    assert "" not in valid


@pytest.mark.parametrize("text", [
    "EURUSD_otc\nGBPUSD_otc",
    "EURUSD_otc, GBPUSD_otc",
    "EURUSD_otc GBPUSD_otc",
    "EURUSD_otc;GBPUSD_otc",
    "  EURUSD_otc ,\n\n  GBPUSD_otc  \n",
    "EURUSD_otc\tGBPUSD_otc",
])
def test_every_plausible_separator_works(text):
    assert parse_assets(text)[0] == ["EURUSD_otc", "GBPUSD_otc"]


def test_casing_is_normalised_to_the_platforms():
    assert parse_assets("eurusd_OTC")[0] == ["EURUSD_otc"]
    assert parse_assets("EurUsd")[0] == ["EURUSD"]


def test_duplicates_are_dropped_but_order_is_kept():
    valid, _ = parse_assets("GBPUSD_otc EURUSD_otc GBPUSD_otc")
    assert valid == ["GBPUSD_otc", "EURUSD_otc"]


def test_comments_are_ignored():
    valid, rejected = parse_assets("EURUSD_otc   # my favourites\nGBPUSD_otc")
    assert valid == ["EURUSD_otc", "GBPUSD_otc"]
    assert rejected == []


def test_things_that_cannot_be_symbols_are_rejected():
    valid, rejected = parse_assets("EURUSD_otc https://example.com/x !! GBPUSD_otc")
    assert valid == ["EURUSD_otc", "GBPUSD_otc"]
    assert rejected == ["https://example.com/x", "!!"]


def test_a_symbol_shaped_word_is_passed_through_not_guessed_at():
    """The parser checks shape, not tradeability.

    'FAVOURITES' is indistinguishable from 'XAUUSD' by shape alone, and a
    hardcoded list of valid instruments here would go stale and reject real
    ones. Names that pass are checked by the broker, and a rejection now
    surfaces per asset with the name in it.
    """
    valid, rejected = parse_assets("FAVOURITES")
    assert valid == ["FAVOURITES"]
    assert rejected == []


def test_empty_input_is_empty_not_an_error():
    assert parse_assets("") == ([], [])
    assert parse_assets("   \n\n  ") == ([], [])


def test_non_currency_instruments_are_accepted():
    """The platform lists gold, indices and crypto -- do not reject them."""
    valid, rejected = parse_assets("XAUUSD_otc BTCUSD SP500")
    assert valid == ["XAUUSD_otc", "BTCUSD", "SP500"]
    assert rejected == []


# ------------------------- repairing a saved file -------------------------
def test_a_settings_file_saved_with_the_bug_repairs_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    import json

    from cheese_signals import paths

    broken = Settings()
    data = {"assets": [PASTED_ON_ONE_LINE]}
    paths.settings_path().write_text(json.dumps({**{"theme": "dark"}, **data}))

    loaded = Settings.load()
    assert len(loaded.assets) == 16, "an existing broken watchlist must be repaired on load"
    assert loaded.assets[0] == "AUDCAD_otc"

    # And the repair is persisted, so it happens once rather than every launch.
    on_disk = json.loads(paths.settings_path().read_text())
    assert len(on_disk["assets"]) == 16
    assert broken is not loaded


def test_a_healthy_settings_file_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    import json

    from cheese_signals import paths

    paths.settings_path().write_text(json.dumps({"assets": ["EURUSD_otc", "GBPUSD_otc"]}))
    assert Settings.load().assets == ["EURUSD_otc", "GBPUSD_otc"]


# --------------------------- the feed's own guard ---------------------------
def test_the_feed_refuses_a_run_together_symbol_before_calling_the_broker():
    """The broker's own error blames authentication; ours must not."""
    from cheese_signals.data.pocket_option import PocketOptionFeed

    with pytest.raises(ValueError) as excinfo:
        PocketOptionFeed(PASTED_ON_ONE_LINE, ssid="dummy")

    message = str(excinfo.value)
    assert "16 symbols run together" in message
    assert "Settings -> Pairs & Data" in message
    assert "SSID is not involved" in message


def test_the_feed_accepts_a_normal_symbol_past_the_guard():
    """The guard must reject only malformed names, not every name."""
    from cheese_signals.data.pocket_option import PocketOptionFeed

    try:
        PocketOptionFeed("EURUSD_otc", ssid="dummy")
    except ValueError as exc:
        pytest.fail(f"a valid symbol was rejected: {exc}")
    except Exception:
        pass  # anything past the guard (missing dependency, no network) is fine


# ------------------------- the settings page round-trip -------------------------
def test_the_pairs_box_survives_a_save_reopen_save_cycle():
    """The actual mechanism: opening Settings and saving again merged the list.

    ``QTextEdit(text)`` treats its argument as rich text, in which a newline
    is only whitespace, so it renders sixteen lines as one. The old parser
    then split on newlines and produced a single symbol made of every pair.
    Nothing about the user's input was wrong -- the round-trip broke it.
    """
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QTextEdit

    QApplication.instance() or QApplication([])
    pairs = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"]

    box = QTextEdit()
    box.setPlainText("\n".join(pairs))
    assert parse_assets(box.toPlainText())[0] == pairs

    # ...and the constructor form, which is what shipped, does not survive it.
    assert QTextEdit("\n".join(pairs)).toPlainText() != "\n".join(pairs), (
        "if Qt ever fixes this, the guard comment in settings_page.py can go"
    )


def test_the_settings_page_writes_pairs_one_per_line():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from cheese_signals.gui.settings_page import SettingsPage

    class _Window:
        settings = Settings()

    _Window.settings.assets = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"]
    page = SettingsPage(_Window())

    assert page.assets_edit.toPlainText().splitlines() == _Window.settings.assets
    assert page._pending_settings().assets == _Window.settings.assets
