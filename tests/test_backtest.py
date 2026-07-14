from cheese_signals.backtest import run_backtest
from cheese_signals.data.synthetic import generate_synthetic_candles


def test_synthetic_candles_have_no_nans():
    # Regression test: a pandas index-alignment bug once made every high/low
    # value NaN, which silently produced zero signals in every backtest.
    df = generate_synthetic_candles(500, seed=1)
    assert not df[["open", "high", "low", "close"]].isna().any().any()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()


def test_backtest_produces_at_least_one_trade_on_enough_data():
    df = generate_synthetic_candles(8000, seed=42)
    report = run_backtest(df, payout=0.85, threshold=0.55)
    assert report.n_trades > 0


def test_backtest_is_deterministic_given_a_seed():
    df1 = generate_synthetic_candles(1500, seed=123)
    df2 = generate_synthetic_candles(1500, seed=123)
    r1 = run_backtest(df1)
    r2 = run_backtest(df2)
    assert r1.n_trades == r2.n_trades
    assert r1.net_pnl == r2.net_pnl


def test_backtest_produces_sane_report_fields():
    df = generate_synthetic_candles(3000, seed=42)
    report = run_backtest(df, payout=0.85)
    assert report.n_trades >= 0
    assert 0.0 <= report.win_rate <= 1.0
    assert abs(report.breakeven_win_rate - (1 / 1.85)) < 1e-9
    if report.n_trades:
        assert report.max_drawdown >= 0


def test_backtest_respects_cooldown_between_signals():
    df = generate_synthetic_candles(2000, seed=5)
    report = run_backtest(df, cooldown_candles=10)
    timestamps = [t.timestamp for t in report.trades]
    for a, b in zip(timestamps, timestamps[1:]):
        assert (b - a).total_seconds() >= 10 * 60 - 1
