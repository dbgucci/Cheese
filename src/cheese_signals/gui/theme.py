"""Design system: colours, type scale, and the Qt stylesheet.

Deliberately not the default Qt look. The palette is a deep neutral slate
with a single cyan accent, generous spacing, 10-12px radii, and one
consistent type scale -- the visual language of a modern desktop trading
tool rather than a 2005 winforms app.
"""

from __future__ import annotations

# ------------------------------- palette --------------------------------
BG = "#0B0F17"          # window background
SURFACE = "#121826"     # cards / panels
SURFACE_ALT = "#171F2F"  # nested surfaces, table headers
BORDER = "#222C40"
BORDER_SOFT = "#1A2333"

TEXT = "#E8EDF7"
TEXT_MUTED = "#8A97AE"
TEXT_FAINT = "#5C6880"

ACCENT = "#22D3EE"
ACCENT_DIM = "#0E7490"
ACCENT_GLOW = "rgba(34, 211, 238, 0.14)"

BUY = "#22C55E"
BUY_SOFT = "rgba(34, 197, 94, 0.14)"
SELL = "#F43F5E"
SELL_SOFT = "rgba(244, 63, 94, 0.14)"
WARN = "#F59E0B"
WARN_SOFT = "rgba(245, 158, 11, 0.14)"
NEUTRAL = "#64748B"

FONT_STACK = '"Segoe UI Variable", "Segoe UI", "Inter", -apple-system, system-ui, sans-serif'
MONO_STACK = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'


def stylesheet() -> str:
    return f"""
* {{
    font-family: {FONT_STACK};
    color: {TEXT};
}}

QWidget#Root, QMainWindow {{
    background: {BG};
}}

/* ------------------------------ sidebar ------------------------------ */
QWidget#Sidebar {{
    background: {SURFACE};
    border-right: 1px solid {BORDER_SOFT};
}}

QLabel#BrandMark {{
    font-size: 19px;
    font-weight: 700;
    letter-spacing: 0.3px;
    padding: 2px 0;
}}

QLabel#BrandSub {{
    font-size: 11px;
    color: {TEXT_FAINT};
    letter-spacing: 1.4px;
    text-transform: uppercase;
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 11px 14px;
    font-size: 13.5px;
    font-weight: 500;
    color: {TEXT_MUTED};
    text-align: left;
}}
QPushButton#NavButton:hover {{
    background: {SURFACE_ALT};
    color: {TEXT};
}}
QPushButton#NavButton:checked {{
    background: {ACCENT_GLOW};
    color: {ACCENT};
    font-weight: 600;
}}

/* ------------------------------- cards ------------------------------- */
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}

QFrame#StatCard {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}

QLabel#StatValue {{
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.5px;
}}
QLabel#StatLabel {{
    font-size: 11px;
    color: {TEXT_FAINT};
    letter-spacing: 1.1px;
    text-transform: uppercase;
}}
QLabel#StatDelta {{
    font-size: 11.5px;
    color: {TEXT_MUTED};
}}

/* --------------------------- signal cards ---------------------------- */
QFrame#SignalCardBuy {{
    background: {SURFACE};
    border: 1px solid rgba(34, 197, 94, 0.35);
    border-left: 3px solid {BUY};
    border-radius: 12px;
}}
QFrame#SignalCardSell {{
    background: {SURFACE};
    border: 1px solid rgba(244, 63, 94, 0.35);
    border-left: 3px solid {SELL};
    border-radius: 12px;
}}

QLabel#SignalPair {{
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 0.2px;
}}
QLabel#SignalMeta {{
    font-size: 12px;
    color: {TEXT_MUTED};
}}
QLabel#SignalReason {{
    font-size: 11.5px;
    color: {TEXT_FAINT};
}}
QLabel#Countdown {{
    font-family: {MONO_STACK};
    font-size: 23px;
    font-weight: 700;
    color: {ACCENT};
}}
QLabel#CountdownLabel {{
    font-size: 10px;
    color: {TEXT_FAINT};
    letter-spacing: 1.2px;
    text-transform: uppercase;
}}

QLabel#PillBuy {{
    background: {BUY_SOFT};
    color: {BUY};
    border: 1px solid rgba(34, 197, 94, 0.4);
    border-radius: 11px;
    padding: 4px 12px;
    font-size: 11.5px;
    font-weight: 700;
    letter-spacing: 0.6px;
}}
QLabel#PillSell {{
    background: {SELL_SOFT};
    color: {SELL};
    border: 1px solid rgba(244, 63, 94, 0.4);
    border-radius: 11px;
    padding: 4px 12px;
    font-size: 11.5px;
    font-weight: 700;
    letter-spacing: 0.6px;
}}
QLabel#PillNeutral {{
    background: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 4px 12px;
    font-size: 11.5px;
    font-weight: 600;
}}

/* ------------------------------ headings ----------------------------- */
QLabel#PageTitle {{
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
}}
QLabel#PageSubtitle {{
    font-size: 12.5px;
    color: {TEXT_MUTED};
}}
QLabel#SectionTitle {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#Hint {{
    font-size: 11.5px;
    color: {TEXT_FAINT};
}}

/* ------------------------------ controls ----------------------------- */
QPushButton#Primary {{
    background: {ACCENT};
    color: #04252B;
    border: none;
    border-radius: 9px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#Primary:hover {{ background: #38DDF2; }}
QPushButton#Primary:pressed {{ background: {ACCENT_DIM}; }}
QPushButton#Primary:disabled {{ background: {BORDER}; color: {TEXT_FAINT}; }}

QPushButton#Danger {{
    background: {SELL_SOFT};
    color: {SELL};
    border: 1px solid rgba(244, 63, 94, 0.45);
    border-radius: 9px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton#Danger:hover {{ background: rgba(244, 63, 94, 0.22); }}

QPushButton#Ghost {{
    background: transparent;
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 9px 16px;
    font-size: 12.5px;
    font-weight: 500;
}}
QPushButton#Ghost:hover {{ background: {SURFACE_ALT}; color: {TEXT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 9px 11px;
    font-size: 13px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_GLOW};
    selection-color: {ACCENT};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; border: none; }}

QCheckBox {{ font-size: 13px; spacing: 9px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border-radius: 5px;
    border: 1px solid {BORDER};
    background: {SURFACE_ALT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
}}

QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 15px; height: 15px; margin: -6px 0;
    background: {ACCENT}; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT_DIM}; border-radius: 2px; }}

/* ------------------------------- tables ------------------------------ */
QTableWidget, QTableView {{
    background: {SURFACE};
    alternate-background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
    gridline-color: {BORDER_SOFT};
    font-size: 12.5px;
}}
QHeaderView::section {{
    background: {SURFACE_ALT};
    color: {TEXT_FAINT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 9px 10px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-transform: uppercase;
}}
QTableWidget::item {{ padding: 8px 10px; border: none; }}
QTableWidget::item:selected {{ background: {ACCENT_GLOW}; color: {TEXT}; }}

/* ----------------------------- scrollbars ---------------------------- */
QScrollBar:vertical {{
    background: transparent; width: 9px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {NEUTRAL}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* ------------------------------ statusbar ---------------------------- */
QFrame#StatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER_SOFT};
}}
QLabel#StatusText {{ font-size: 12px; color: {TEXT_MUTED}; }}

QTextEdit {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 9px;
    font-family: {MONO_STACK};
    font-size: 11.5px;
    color: {TEXT_MUTED};
}}

QToolTip {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 9px;
}}
"""
