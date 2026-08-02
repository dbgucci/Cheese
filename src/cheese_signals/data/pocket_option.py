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
"""

from __future__ import annotations

import os
import sys

import pandas as pd

from .base import CandleFeed, validate_candles


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
    def __init__(self, asset: str, timeframe_seconds: int = 60, ssid: str | None = None):
        try:
            from BinaryOptionsToolsV2.pocketoption import PocketOption
        except ImportError as exc:
            raise ImportError(_missing_dependency_message(exc)) from exc

        ssid = ssid or os.environ.get("POCKET_OPTION_SSID")
        if not ssid:
            raise ValueError(
                "No Pocket Option SSID provided. Pass ssid=... or set the "
                "POCKET_OPTION_SSID environment variable."
            )

        self.asset = asset
        self.timeframe_seconds = timeframe_seconds
        self._client = PocketOption(ssid=ssid)

    def get_candles(self, count: int) -> pd.DataFrame:
        raw = self._client.get_candles(self.asset, self.timeframe_seconds, 0)
        return _normalize(raw, count)


def _normalize(raw, count: int) -> pd.DataFrame:
    """Convert whatever shape the client returns (list[dict] typically) into our schema."""
    df = pd.DataFrame(raw)
    if df.empty:
        raise RuntimeError("Pocket Option returned no candle data; check asset name and SSID validity")

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
        df.index = pd.to_datetime(df["timestamp"], unit="s")
        df = df.drop(columns=["timestamp"])
    df = df.sort_index()
    df = validate_candles(df)
    return df.tail(count)
