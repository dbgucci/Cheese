"""Live Pocket Option candle feed.

This wraps the unofficial ``binaryoptionstoolsv2`` WebSocket client
(https://pypi.org/project/binaryoptionstoolsv2/). It is NOT an official
Pocket Option SDK -- Pocket Option does not publish one -- so treat this
adapter as best-effort: the reverse-engineered protocol can change without
notice, and you are responsible for complying with Pocket Option's terms of
service in your jurisdiction.

Setup:
    pip install binaryoptionstoolsv2
    export POCKET_OPTION_SSID="<your session id, copied from the browser's
        WebSocket handshake while logged into pocketoption.com>"

The SSID is a session token tied to your own logged-in account; this module
never asks for a password and never talks to anything other than Pocket
Option's own servers via that library.

Why a streaming buffer rather than a plain request/response call
---------------------------------------------------------------
The library's ``get_candles(asset, period, offset)`` is deprecated, and its
own documentation warns it "is NOT designed for real-time/live trading as it
does not include the current forming candle and can introduce gaps if called
sequentially during live trading" -- which is precisely how a polling signal
engine uses it. Its ``offset`` is also *seconds of history*, so the obvious
``offset=0`` yields almost no candles at all.

The supported path is ``get_candles_live()``, which backfills history and
then streams gap-free updates through an iterator. An iterator doesn't fit
the pull-based ``CandleFeed`` interface, so a small background thread drains
it into a buffer and ``get_candles()`` returns a snapshot of that buffer
without ever blocking the trading loop.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

import pandas as pd

from .base import CandleFeed, validate_candles

# Enough 1-minute history for the indicators (ADX/EMA warm-up) plus the
# higher-timeframe bias resample, with headroom.
DEFAULT_HISTORY_HOURS = 8.0
DEFAULT_MAX_ROWS = 500
FIRST_DATA_TIMEOUT = 45.0

# One client (one WebSocket session) is shared by every asset being watched.
# A separate PocketOption instance per pair would open six sessions on the
# same account for a six-pair watchlist, which invites connection limits and
# needless reconnect churn.
_CLIENTS: dict[str, object] = {}
_CLIENTS_LOCK = threading.Lock()


def _shared_client(ssid: str):
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(ssid)
        if client is None:
            from BinaryOptionsToolsV2.pocketoption import PocketOption

            client = PocketOption(ssid=ssid)
            _CLIENTS[ssid] = client
        return client


def reset_clients() -> None:
    """Drop cached sessions, so a changed SSID takes effect without a restart."""
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


def _missing_dependency_message(exc: ImportError) -> str:
    """Explain the failure differently for a frozen exe vs. a source checkout.

    Telling a user of the packaged .exe to "pip install" is actively
    misleading: a frozen application cannot see site-packages, so no amount of
    installing on their machine will ever fix it. That case needs a rebuild.
    """
    if getattr(sys, "frozen", False):
        return (
            "The live Pocket Option feed is not available in this build of the app.\n\n"
            "This cannot be fixed by installing anything on your PC -- a packaged .exe "
            "only contains the libraries that were bundled when it was built, and this "
            "one was built without the Pocket Option client.\n\n"
            "Fixes:\n"
            "  * Download a newer build (Actions tab -> Build Windows EXE), or\n"
            "  * Rebuild with build_windows.bat, which now installs "
            "'binaryoptionstoolsv2' before packaging.\n\n"
            "In the meantime, set Data source to 'synthetic' in Settings to keep "
            f"using the app.\n\n(underlying error: {exc})"
        )
    return (
        "The live Pocket Option feed needs the 'binaryoptionstoolsv2' package.\n\n"
        f"Install it into the SAME Python that runs this app:\n"
        f"    {sys.executable} -m pip install binaryoptionstoolsv2\n\n"
        "Installing with a bare `pip install` often targets a different Python "
        "installation or a different virtualenv, which is the usual reason it still "
        f"appears missing after installing.\n\n(underlying error: {exc})"
    )


class PocketOptionFeed(CandleFeed):
    def __init__(
        self,
        asset: str,
        timeframe_seconds: int = 60,
        ssid: str | None = None,
        history_hours: float = DEFAULT_HISTORY_HOURS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ):
        try:
            from BinaryOptionsToolsV2.pocketoption import PocketOption
        except ImportError as exc:
            raise ImportError(_missing_dependency_message(exc)) from exc

        ssid = ssid or os.environ.get("POCKET_OPTION_SSID")
        if not ssid:
            raise ValueError(
                "No Pocket Option SSID provided. Set it in Settings, or set the "
                "POCKET_OPTION_SSID environment variable."
            )

        self.asset = asset
        self.timeframe_seconds = timeframe_seconds
        self.history_hours = history_hours
        self.max_rows = max_rows

        self._client = _shared_client(ssid)
        self._buffer = pd.DataFrame()
        self._lock = threading.Lock()
        self._first_data = threading.Event()
        self._error: Optional[BaseException] = None
        self._stop = threading.Event()

        self._thread = threading.Thread(
            target=self._stream, name=f"po-feed-{asset}", daemon=True
        )
        self._thread.start()

    # ------------------------------ streaming ------------------------------
    def _stream(self) -> None:
        """Drain the live candle iterator into ``self._buffer`` until stopped."""
        try:
            iterator = self._client.get_candles_live(
                self.asset,
                self.timeframe_seconds,
                hours=self.history_hours,
                max_rows=self.max_rows,
            )
            for closed, _forming in iterator:
                if self._stop.is_set():
                    return
                if not closed:
                    continue
                # Only closed candles are used: a strategy must never see the
                # in-progress candle, or it would be reacting to a bar that can
                # still change before it closes.
                df = _normalize(closed)
                with self._lock:
                    self._buffer = df
                self._first_data.set()
        except Exception as exc:  # surfaced to the caller via get_candles()
            self._error = exc
            self._first_data.set()

    def close(self) -> None:
        self._stop.set()

    # ------------------------------ interface ------------------------------
    def get_candles(self, count: int) -> pd.DataFrame:
        if not self._first_data.wait(timeout=FIRST_DATA_TIMEOUT):
            raise RuntimeError(
                f"No data from Pocket Option for '{self.asset}' after "
                f"{FIRST_DATA_TIMEOUT:.0f}s.\n"
                "Usual causes: the SSID has expired (log in again and copy a fresh "
                "one), or this asset is not currently tradeable on your account."
            )

        if self._error is not None:
            raise RuntimeError(
                f"Pocket Option feed failed for '{self.asset}': {self._error}\n"
                "If this mentions authentication, your SSID has probably expired."
            ) from self._error

        with self._lock:
            df = self._buffer.copy()

        if df.empty:
            raise RuntimeError(
                f"Pocket Option returned no candles for '{self.asset}'.\n"
                "Check the asset name is spelled exactly as the platform lists it "
                "(e.g. 'EURUSD_otc') and that it is open for trading right now."
                + self._asset_hint()
            )
        return df.tail(count)

    def _asset_hint(self) -> str:
        """Name some assets the account can actually trade right now.

        "Check the asset name" is only useful advice if we also say what the
        valid names are, so fetch them when the error is raised.
        """
        assets = self.available_assets()
        if not assets:
            return ""
        otc = [a for a in assets if a.lower().endswith("_otc")]
        shown = (otc or assets)[:12]
        return "\n\nCurrently tradeable" + (" OTC" if otc else "") + " assets include:\n  " + ", ".join(shown)

    # ------------------------------ helpers -------------------------------
    def available_assets(self) -> list[str]:
        """Asset names the account can currently trade, for validating settings."""
        try:
            payouts = self._client.payout()
        except Exception:
            return []
        if isinstance(payouts, dict):
            return sorted(k for k, v in payouts.items() if v)
        return []


def _normalize(raw, count: Optional[int] = None) -> pd.DataFrame:
    """Convert the client's candle dicts into our OHLCV schema."""
    df = pd.DataFrame(raw)
    if df.empty:
        return df

    rename_map = {
        "time": "timestamp",
        "from": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "timestamp" in df.columns:
        ts = df["timestamp"]
        # The API has used both epoch seconds and ISO strings across versions.
        if pd.api.types.is_numeric_dtype(ts):
            df.index = pd.to_datetime(ts, unit="s", utc=True)
        else:
            df.index = pd.to_datetime(ts, utc=True)
        df = df.drop(columns=["timestamp"])

    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = validate_candles(df)
    keep = ["open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]]
    return df.tail(count) if count else df
