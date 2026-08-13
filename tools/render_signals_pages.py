"""Render every page of the signals window to a PNG.

    python tools/render_signals_pages.py out_dir

Exists because the Settings page shipped unreadable and the tests at the time
all passed. The layout bugs -- a missing scroll area, controls pushed to the far
right edge of the window, a clipped brand mark, an elided table header -- are
invisible to logic tests and obvious in a picture. ``tests/test_gui_signals_layout``
now asserts the specific properties those bugs violated, but a test can only
catch a defect someone has already thought of. A screenshot catches the next one.

Run in CI after the build so each artifact carries images of the app it contains,
and a layout regression is visible without a Windows machine or an install.
Offscreen, so it needs no display.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Both must be set before QApplication exists. CHEESE_SIGNALS_HOME keeps the
# render away from real saved settings -- and lets it run on a machine that has
# none, which every CI runner is.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WIDTH, HEIGHT = 1680, 1040


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "screenshots").resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CHEESE_SIGNALS_HOME", str(out / "_settings"))

    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cheese_signals.gui import theme
    from cheese_signals.gui.signals_app import SignalsWindow

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())

    win = SignalsWindow()
    win.resize(WIDTH, HEIGHT)
    win.show()
    for _ in range(40):
        app.processEvents()

    names = ["signals", "settings", "activity"]
    written = []
    for index, name in enumerate(names):
        win.nav_group.button(index).click()
        for _ in range(40):
            app.processEvents()
        path = out / f"{name}.png"
        pixmap = win.grab()
        if pixmap.isNull() or not pixmap.save(str(path)):
            print(f"FAILED to render {name}")
            return 1
        written.append(path)
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")

    # The Settings page is the tall one: grab it scrolled to the bottom too, so
    # the half a screenshot cannot show is in the artifact as well.
    from PySide6.QtWidgets import QScrollArea

    win.nav_group.button(1).click()
    for _ in range(40):
        app.processEvents()
    areas = win.settings_page.findChildren(QScrollArea)
    if areas:
        bar = areas[0].verticalScrollBar()
        bar.setValue(bar.maximum())
        for _ in range(40):
            app.processEvents()
        path = out / "settings-bottom.png"
        win.grab().save(str(path))
        print(f"wrote {path}")
    else:
        print("WARNING: the settings page has no scroll area, so it cannot "
              "scroll -- tall content will be compressed instead")

    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
