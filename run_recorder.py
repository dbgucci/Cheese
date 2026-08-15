#!/usr/bin/env python3
"""Record Pocket Option OTC candles continuously, so the research has data.

The first real research run found the archive held 3.8 days of candles.
Testing an hour-of-day rule needs 5.6 days before one hypothesis becomes
measurable, and the conditional patterns worth looking for need about four
weeks. That gap is the whole problem, and only running time closes it.

    python run_recorder.py                      # default watchlist
    python run_recorder.py --assets EURUSD_otc GBPUSD_otc
    python run_recorder.py --status             # how much history exists

Leave it running. It places no orders, needs no strategy, and reconnects
by itself. Check back in a fortnight and run the research again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import os  # noqa: E402

from cheese_signals import paths, settings as settings_mod, storage  # noqa: E402
from cheese_signals.research import recorder as rec  # noqa: E402

# The six pairs already in the journal, so a default run deepens the history
# that exists rather than starting six shallow new ones.
DEFAULT_ASSETS = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "AUDUSD_otc",
    "USDJPY_otc",
    "USDCAD_otc",
    "EURJPY_otc",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record OTC candles to the journal, continuously.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--assets",
        nargs="+",
        default=DEFAULT_ASSETS,
        help="Assets to record (default: the six already in the journal).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30.0,
        help="Seconds between polls (default 30; candles close every 60).",
    )
    parser.add_argument(
        "--timeframe-seconds",
        type=int,
        default=60,
        help="Candle size in seconds (default 60).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print how much history exists and what it can test, then exit.",
    )
    parser.add_argument(
        "--set-ssid",
        action="store_true",
        help=(
            "Paste your Pocket Option session id and save it to settings.json. "
            "Read from stdin, not the command line, so the shell cannot mangle "
            "its quotes and it never lands in shell history."
        ),
    )
    return parser.parse_args(argv)


def resolve_ssid() -> str | None:
    """Find the session id: settings.json first, then the environment.

    settings.json is preferred because the SSID is a JSON blob full of quotes,
    braces and brackets. Passing that through ``setx`` or PowerShell mangles
    it -- the quotes terminate the argument early -- and every workaround puts
    a live credential into shell history. A file holds it verbatim.
    """
    try:
        saved = settings_mod.Settings.load().pocket_option_ssid
        if saved:
            return saved
    except Exception:  # noqa: BLE001 - a broken settings file must not block --status
        pass
    return os.environ.get("POCKET_OPTION_SSID") or None


def set_ssid_interactively() -> int:
    print("Paste your Pocket Option session id, then press Enter.")
    print("It looks like: 42[\"auth\",{\"session\":\"...\",\"isDemo\":1,...}]")
    print()
    try:
        pasted = input("SSID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    if not pasted:
        print("Nothing pasted; leaving the current value alone.")
        return 1

    settings = settings_mod.Settings.load()
    settings.pocket_option_ssid = pasted
    settings.save()

    print()
    print(f"Saved to {paths.settings_path()}")
    print(f"({len(pasted)} characters). Now run: python run_recorder.py")
    print()
    print(
        "This is a live session token. Treat that file the way you would a\n"
        "password, and note it will expire in days, not weeks."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.status:
        print(rec.progress_report())
        return 0

    if args.set_ssid:
        return set_ssid_interactively()

    ssid = resolve_ssid()
    if not ssid:
        print(
            "No Pocket Option session id found.\n\n"
            "Save one without fighting your shell's quoting:\n"
            "    python run_recorder.py --set-ssid\n\n"
            "It is stored in settings.json next to the journal, which is where\n"
            "the desktop app reads it from too.",
            file=sys.stderr,
        )
        return 1

    from cheese_signals.data import get_pocket_option_feed

    def factory(asset: str):
        return get_pocket_option_feed(
            asset, timeframe_seconds=args.timeframe_seconds, ssid=ssid
        )

    journal = storage.Journal()
    recorder = rec.Recorder(
        assets=args.assets,
        feed_factory=factory,
        journal=journal,
        poll_seconds=args.poll_seconds,
    )
    rec.install_signal_handlers(recorder)

    print(f"Recording {len(args.assets)} asset(s) to {journal.path}")
    print("Ctrl+C to stop. Leave this running -- more days is the whole point.\n")

    try:
        stats = recorder.run()
    except KeyboardInterrupt:
        stats = recorder.stats

    print()
    print(stats.summary())
    print()
    print(rec.progress_report(journal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
