"""Where the app stores its data.

Requirement: everything lives in a folder on the user's Desktop, next to the
app, so the data is easy to find, back up, and inspect without digging
through AppData.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_FOLDER_NAME = "CheeseSignals"


def _desktop_dir() -> Path:
    home = Path.home()

    # Windows: OneDrive-redirected Desktops are extremely common, and writing
    # to the non-redirected path would silently create a folder the user
    # never sees. Prefer whichever actually exists.
    candidates = [
        home / "Desktop",
        home / "OneDrive" / "Desktop",
        home / "OneDrive - Personal" / "Desktop",
    ]
    if sys.platform == "win32":
        onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if onedrive:
            candidates.insert(0, Path(onedrive) / "Desktop")

    for path in candidates:
        if path.is_dir():
            return path
    return home  # last resort: no Desktop (headless/server), fall back to home


def data_dir() -> Path:
    """Root data folder: ``<Desktop>/CheeseSignals``. Created on first use.

    Override with the ``CHEESE_SIGNALS_HOME`` environment variable (used by
    the test suite so tests never touch a real Desktop).
    """
    override = os.environ.get("CHEESE_SIGNALS_HOME")
    root = Path(override) if override else _desktop_dir() / APP_FOLDER_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "signals.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d
