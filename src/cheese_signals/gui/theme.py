"""KPS design system.

Structurally this is a flat, hairline-separated dark interface: a true-black
canvas, surfaces raised by a single lighter step rather than by shadow, 1px
dividers instead of boxes, and generous whitespace doing the work that borders
used to. Nothing casts a shadow, and no panel is outlined on all four sides
unless it is genuinely a container.

Colour is used sparingly and means one thing at a time:

- **gold** is the brand and the primary action;
- **green and red are reserved exclusively for market direction and
  outcomes**, so a green button is always a buy or a win, never "confirm";
- everything else is greyscale, and hierarchy comes from type weight and
  spacing rather than from tinting.
"""

from __future__ import annotations

# -------------------------------- palette --------------------------------
# A true-black canvas with surfaces stepped in small, even increments. The
# steps are deliberately close together: on an OLED-ish dark UI, large jumps
# in surface lightness read as clutter.
BG = "#000000"
BG_ELEVATED = "#0A0A0A"
SURFACE = "#0F0F0F"
SURFACE_ALT = "#161616"
SURFACE_HI = "#1F1F1F"

# Hairlines. BORDER is the visible divider; BORDER_SOFT is barely there and
# is for separating rows inside an already-bounded surface.
BORDER = "#262626"
BORDER_SOFT = "#1A1A1A"

TEXT = "#FAFAFA"
TEXT_MUTED = "#A8A8A8"
TEXT_FAINT = "#6B6B6B"

GOLD = "#D4AF37"
GOLD_BRIGHT = "#EFC94C"
GOLD_DIM = "#7A6420"
GOLD_GLOW = "rgba(212, 175, 55, 0.14)"
GOLD_SOFT = "rgba(212, 175, 55, 0.08)"

BUY = "#26A65B"
BUY_BRIGHT = "#35D67C"
BUY_SOFT = "rgba(38, 166, 91, 0.14)"
SELL = "#E0483A"
SELL_BRIGHT = "#FF6152"
SELL_SOFT = "rgba(224, 72, 58, 0.14)"
WARN = "#E0A030"
NEUTRAL = "#5A5A5A"

FONT_STACK = '"Segoe UI Variable Display", "Segoe UI", "Inter", -apple-system, system-ui, sans-serif'
MONO_STACK = '"JetBrains Mono", "Cascadia Mono", "SF Mono", "Consolas", monospace'

# Spacing grid. Layout code imports these instead of hardcoding pixels, so the
# rhythm stays consistent when a page is edited.
PAGE_MARGIN_H = 40
PAGE_MARGIN_TOP = 34
PAGE_MARGIN_BOTTOM = 28
GAP = 12
GAP_LG = 22
GAP_XL = 32

RADIUS = 14        # cards and containers
RADIUS_SM = 10     # inputs, buttons
ROW_HEIGHT = 46    # table rows -- roomy, like a feed


def stylesheet() -> str:
    return f"""
* {{
    font-family: {FONT_STACK};
    color: {TEXT};
    outline: none;
}}

QWidget#Root, QMainWindow {{ background: {BG}; }}

/* ------------------------------- sidebar -------------------------------
   A rail, not a panel: no fill of its own, separated from the content by a
   single hairline. Nav items are full-width rounded rows that fill on hover,
   the way a modern web nav behaves. */
QWidget#Sidebar {{
    background: {BG};
    border-right: 1px solid {BORDER_SOFT};
}}

QLabel#BrandMark {{
    font-size: 21px;
    font-weight: 700;
    letter-spacing: 5px;
    color: {TEXT};
}}
QLabel#BrandSub {{
    font-size: 9.5px;
    color: {TEXT_FAINT};
    letter-spacing: 2.4px;
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 12px 14px;
    font-size: 14px;
    font-weight: 400;
    color: {TEXT_MUTED};
    text-align: left;
}}
QPushButton#NavButton:hover {{ background: {SURFACE}; color: {TEXT}; }}
QPushButton#NavButton:checked {{
    background: {SURFACE_ALT};
    color: {TEXT};
    font-weight: 700;
}}

/* -------------------------------- cards --------------------------------
   One flat surface, one hairline, a generous radius. No shadows anywhere --
   depth comes from the surface step alone. */
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
}}
QFrame#Plain {{ background: transparent; border: none; }}

/* Stat strip: tiles share one surface, divided by hairlines, like a profile
   header's counts rather than four separate boxes. */
QFrame#StatStrip {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
}}
QFrame#StatTile {{ background: transparent; border: none; }}
QFrame#StatDivider {{ background: {BORDER_SOFT}; border: none; max-width: 1px; }}

QLabel#StatValue {{ font-size: 30px; font-weight: 700; letter-spacing: -1px; }}
QLabel#StatLabel {{
    font-size: 10.5px; color: {TEXT_FAINT};
    letter-spacing: 1.2px;
}}
QLabel#StatDelta {{ font-size: 11.5px; color: {TEXT_MUTED}; }}

/* ----------------------------- signal cards ---------------------------- */
QFrame#SignalCardBuy, QFrame#SignalCardSell {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
}}
QFrame#SignalCardBuy {{ border-left: 3px solid {BUY}; }}
QFrame#SignalCardSell {{ border-left: 3px solid {SELL}; }}

QLabel#SignalPair {{ font-size: 19px; font-weight: 700; letter-spacing: -0.3px; }}
QLabel#SignalMeta {{ font-size: 12.5px; color: {TEXT_MUTED}; }}
QLabel#SignalReason {{ font-size: 12px; color: {TEXT_FAINT}; }}
QLabel#Countdown {{
    font-family: {MONO_STACK};
    font-size: 28px; font-weight: 600; letter-spacing: -0.5px; color: {GOLD};
}}
QLabel#CountdownLabel {{
    font-size: 9.5px; color: {TEXT_FAINT}; letter-spacing: 1.6px;
}}

/* Pills are fully rounded and unbordered -- a tinted chip, not a bordered
   badge. */
QLabel#PillBuy {{
    background: {BUY_SOFT}; color: {BUY_BRIGHT};
    border: none; border-radius: 13px; padding: 5px 14px;
    font-size: 11.5px; font-weight: 700; letter-spacing: 0.6px;
}}
QLabel#PillSell {{
    background: {SELL_SOFT}; color: {SELL_BRIGHT};
    border: none; border-radius: 13px; padding: 5px 14px;
    font-size: 11.5px; font-weight: 700; letter-spacing: 0.6px;
}}
QLabel#PillNeutral {{
    background: {SURFACE_ALT}; color: {TEXT_MUTED};
    border: none; border-radius: 13px; padding: 5px 14px;
    font-size: 11.5px; font-weight: 600;
}}
QLabel#PillGold {{
    background: {GOLD_SOFT}; color: {GOLD};
    border: none; border-radius: 13px; padding: 5px 14px;
    font-size: 11.5px; font-weight: 700;
}}

/* ------------------------------- headings ------------------------------ */
QLabel#PageTitle {{ font-size: 28px; font-weight: 700; letter-spacing: -0.7px; }}
QLabel#PageSubtitle {{ font-size: 13px; color: {TEXT_MUTED}; }}
QLabel#SectionTitle {{
    font-size: 13px; font-weight: 700; color: {TEXT}; letter-spacing: -0.1px;
}}
QLabel#Hint {{ font-size: 12px; color: {TEXT_FAINT}; }}
QLabel#ConflictWarning {{
    font-size: 12px; color: {WARN};
    background: rgba(224, 160, 48, 0.08);
    border: none; border-left: 2px solid {WARN};
    border-radius: 4px; padding: 10px 14px;
}}

/* ------------------------------- controls ------------------------------ */
QPushButton#Primary {{
    background: {GOLD}; color: #16110A;
    border: none; border-radius: {RADIUS_SM}px; padding: 11px 24px;
    font-size: 13.5px; font-weight: 700;
}}
QPushButton#Primary:hover {{ background: {GOLD_BRIGHT}; }}
QPushButton#Primary:pressed {{ background: {GOLD_DIM}; }}
QPushButton#Primary:disabled {{ background: {SURFACE_HI}; color: {TEXT_FAINT}; }}

QPushButton#Buy {{
    background: {BUY}; color: #03130A;
    border: none; border-radius: {RADIUS_SM}px; padding: 11px 24px;
    font-size: 13.5px; font-weight: 700;
}}
QPushButton#Buy:hover {{ background: {BUY_BRIGHT}; }}

QPushButton#Danger {{
    background: {SELL}; color: #1A0402;
    border: none; border-radius: {RADIUS_SM}px; padding: 11px 24px;
    font-size: 13.5px; font-weight: 700;
}}
QPushButton#Danger:hover {{ background: {SELL_BRIGHT}; }}

/* Secondary actions are filled, not outlined -- an outline competes with the
   hairlines and makes a toolbar look like a form. */
QPushButton#Ghost {{
    background: {SURFACE_ALT}; color: {TEXT};
    border: none; border-radius: {RADIUS_SM}px;
    padding: 10px 16px; font-size: 12.5px; font-weight: 600;
}}
QPushButton#Ghost:hover {{ background: {SURFACE_HI}; }}
QPushButton#Ghost:disabled {{ background: {SURFACE}; color: {TEXT_FAINT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_ALT};
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
    padding: 9px 12px;
    font-size: 13px;
    selection-background-color: {GOLD_DIM};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    background: {SURFACE_HI};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {GOLD};
    background: {SURFACE_ALT};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {BG_ELEVATED}; color: {TEXT_FAINT};
}}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT}; border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px; padding: 4px;
    selection-background-color: {SURFACE_HI}; selection-color: {GOLD};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; border: none; }}

QCheckBox {{ font-size: 13px; spacing: 10px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid {BORDER}; background: {SURFACE_ALT};
}}
QCheckBox::indicator:hover {{ border-color: {TEXT_FAINT}; }}
QCheckBox::indicator:checked {{ background: {GOLD}; border: 1px solid {GOLD}; }}
QCheckBox::indicator:disabled {{ background: {BG_ELEVATED}; border-color: {BORDER_SOFT}; }}
QCheckBox:disabled {{ color: {TEXT_FAINT}; }}

/* Settings groups: a labelled section with a single hairline above it, not a
   box drawn around the fields. */
QGroupBox {{
    border: none;
    border-top: 1px solid {BORDER_SOFT};
    margin-top: 32px;
    padding-top: 22px;
    font-size: 13px;
    font-weight: 700;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 0px; top: 4px; padding: 0;
    color: {TEXT};
}}

/* -------------------------------- tables -------------------------------
   Borderless, no grid, no zebra striping: rows are separated by the header
   hairline and by row height alone. */
QTableView {{
    background: {SURFACE};
    alternate-background-color: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
    gridline-color: transparent;
    font-size: 13px;
    padding: 4px;
}}
QTableView::item {{ padding: 0 14px; border: none; }}
QTableView::item:selected {{ background: {SURFACE_ALT}; color: {TEXT}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {SURFACE}; color: {TEXT_FAINT};
    border: none; border-bottom: 1px solid {BORDER_SOFT};
    padding: 10px 14px; font-size: 10.5px; font-weight: 700;
    letter-spacing: 1.1px;
}}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

/* ------------------------------ scrollbars ----------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {SURFACE_HI}; border-radius: 5px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {NEUTRAL}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {SURFACE_HI}; border-radius: 5px; min-width: 36px; }}
QScrollBar::handle:horizontal:hover {{ background: {NEUTRAL}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* Tabs as a segmented control: a pill track with a filled active segment. */
QTabWidget::pane {{ border: none; top: 8px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_MUTED};
    padding: 9px 18px; margin-right: 4px;
    border: none; border-radius: 999px;
    font-size: 13px; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {TEXT}; background: {SURFACE}; }}
QTabBar::tab:selected {{ color: {TEXT}; background: {SURFACE_ALT}; }}

/* ------------------------------- statusbar ----------------------------- */
QFrame#StatusBar {{ background: {BG}; border-top: 1px solid {BORDER_SOFT}; }}
QLabel#StatusText {{ font-size: 12px; color: {TEXT_FAINT}; }}

QTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px; padding: 12px;
    font-family: {MONO_STACK};
    font-size: 12px; color: {TEXT_MUTED};
}}

QToolTip {{
    background: {SURFACE_HI}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px;
}}
"""
