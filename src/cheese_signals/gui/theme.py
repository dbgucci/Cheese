"""KPS design system: deep charcoal, brushed gold, candle red/green.

Gold carries brand and emphasis; red and green are reserved *exclusively* for
market direction and outcomes, so a colour never means two things at once. A
gold button is an action; a green one is a buy or a win; a red one is a sell,
a loss, or a stop.
"""

from __future__ import annotations

# ------------------------------- palette --------------------------------
BG = "#0C0B09"           # near-black with a warm cast
BG_DEEP = "#080706"
SURFACE = "#15130F"      # cards
SURFACE_ALT = "#1D1A14"  # nested surfaces, table headers
SURFACE_HI = "#252017"
BORDER = "#332C1E"
BORDER_SOFT = "#241F16"

TEXT = "#F2EADA"
TEXT_MUTED = "#A2957C"
TEXT_FAINT = "#6E634F"

GOLD = "#D4AF37"         # primary accent
GOLD_BRIGHT = "#F0CC5A"
GOLD_DIM = "#8A7222"
GOLD_GLOW = "rgba(212, 175, 55, 0.15)"
GOLD_SOFT = "rgba(212, 175, 55, 0.10)"

BUY = "#26A65B"          # candle green
BUY_BRIGHT = "#2FD072"
BUY_SOFT = "rgba(38, 166, 91, 0.16)"
SELL = "#D2493A"         # candle red
SELL_BRIGHT = "#F05545"
SELL_SOFT = "rgba(210, 73, 58, 0.16)"
WARN = "#E0A030"
NEUTRAL = "#6E634F"

FONT_STACK = '"Segoe UI Variable", "Segoe UI", "Inter", -apple-system, system-ui, sans-serif'
MONO_STACK = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'


def stylesheet() -> str:
    return f"""
* {{
    font-family: {FONT_STACK};
    color: {TEXT};
}}

QWidget#Root, QMainWindow {{ background: {BG}; }}

/* ------------------------------ sidebar ------------------------------ */
QWidget#Sidebar {{
    background: {BG_DEEP};
    border-right: 1px solid {BORDER_SOFT};
}}

QLabel#BrandMark {{
    font-size: 26px;
    font-weight: 800;
    letter-spacing: 3px;
    color: {GOLD};
}}
QLabel#BrandSub {{
    font-size: 10px;
    color: {TEXT_FAINT};
    letter-spacing: 2.2px;
    text-transform: uppercase;
}}
QFrame#BrandRule {{
    background: {GOLD_DIM};
    max-height: 1px;
    border: none;
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0px;
    padding: 12px 14px;
    font-size: 13.5px;
    font-weight: 500;
    color: {TEXT_MUTED};
    text-align: left;
}}
QPushButton#NavButton:hover {{ background: {SURFACE}; color: {TEXT}; }}
QPushButton#NavButton:checked {{
    background: {GOLD_SOFT};
    border-left: 2px solid {GOLD};
    color: {GOLD};
    font-weight: 600;
}}

/* ------------------------------- cards ------------------------------- */
QFrame#Card, QFrame#StatCard {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}

QLabel#StatValue {{ font-size: 27px; font-weight: 700; letter-spacing: -0.5px; }}
QLabel#StatLabel {{
    font-size: 10px; color: {TEXT_FAINT};
    letter-spacing: 1.4px; text-transform: uppercase;
}}
QLabel#StatDelta {{ font-size: 11.5px; color: {TEXT_MUTED}; }}

/* --------------------------- signal cards ---------------------------- */
QFrame#SignalCardBuy {{
    background: {SURFACE};
    border: 1px solid rgba(38, 166, 91, 0.30);
    border-left: 3px solid {BUY};
    border-radius: 10px;
}}
QFrame#SignalCardSell {{
    background: {SURFACE};
    border: 1px solid rgba(210, 73, 58, 0.30);
    border-left: 3px solid {SELL};
    border-radius: 10px;
}}

QLabel#SignalPair {{ font-size: 17px; font-weight: 700; letter-spacing: 0.4px; }}
QLabel#SignalMeta {{ font-size: 12px; color: {TEXT_MUTED}; }}
QLabel#SignalReason {{ font-size: 11.5px; color: {TEXT_FAINT}; }}
QLabel#Countdown {{
    font-family: {MONO_STACK};
    font-size: 23px; font-weight: 700; color: {GOLD};
}}
QLabel#CountdownLabel {{
    font-size: 9.5px; color: {TEXT_FAINT};
    letter-spacing: 1.4px; text-transform: uppercase;
}}

QLabel#PillBuy {{
    background: {BUY_SOFT}; color: {BUY_BRIGHT};
    border: 1px solid rgba(38, 166, 91, 0.45);
    border-radius: 11px; padding: 4px 12px;
    font-size: 11.5px; font-weight: 700; letter-spacing: 0.7px;
}}
QLabel#PillSell {{
    background: {SELL_SOFT}; color: {SELL_BRIGHT};
    border: 1px solid rgba(210, 73, 58, 0.45);
    border-radius: 11px; padding: 4px 12px;
    font-size: 11.5px; font-weight: 700; letter-spacing: 0.7px;
}}
QLabel#PillNeutral {{
    background: {SURFACE_ALT}; color: {TEXT_MUTED};
    border: 1px solid {BORDER}; border-radius: 11px;
    padding: 4px 12px; font-size: 11.5px; font-weight: 600;
}}
QLabel#PillGold {{
    background: {GOLD_SOFT}; color: {GOLD};
    border: 1px solid {GOLD_DIM}; border-radius: 11px;
    padding: 4px 12px; font-size: 11.5px; font-weight: 700;
}}

/* ------------------------------ headings ----------------------------- */
QLabel#PageTitle {{ font-size: 23px; font-weight: 700; letter-spacing: -0.2px; }}
QLabel#PageSubtitle {{ font-size: 12.5px; color: {TEXT_MUTED}; }}
QLabel#SectionTitle {{
    font-size: 11px; font-weight: 700; color: {GOLD};
    letter-spacing: 1.5px; text-transform: uppercase;
}}
QLabel#Hint {{ font-size: 11.5px; color: {TEXT_FAINT}; }}
QLabel#ConflictWarning {{
    font-size: 11.5px; color: {WARN};
    background: rgba(224, 160, 48, 0.10);
    border: 1px solid rgba(224, 160, 48, 0.35);
    border-radius: 7px; padding: 8px 10px;
}}

/* ------------------------------ controls ----------------------------- */
QPushButton#Primary {{
    background: {GOLD}; color: #1A1405;
    border: none; border-radius: 8px; padding: 10px 22px;
    font-size: 13px; font-weight: 700; letter-spacing: 0.3px;
}}
QPushButton#Primary:hover {{ background: {GOLD_BRIGHT}; }}
QPushButton#Primary:pressed {{ background: {GOLD_DIM}; }}
QPushButton#Primary:disabled {{ background: {BORDER}; color: {TEXT_FAINT}; }}

QPushButton#Buy {{
    background: {BUY}; color: #04150A;
    border: none; border-radius: 8px; padding: 10px 22px;
    font-size: 13px; font-weight: 700;
}}
QPushButton#Buy:hover {{ background: {BUY_BRIGHT}; }}

QPushButton#Danger {{
    background: {SELL}; color: #1B0503;
    border: none; border-radius: 8px; padding: 10px 22px;
    font-size: 13px; font-weight: 700;
}}
QPushButton#Danger:hover {{ background: {SELL_BRIGHT}; }}

QPushButton#Ghost {{
    background: transparent; color: {TEXT_MUTED};
    border: 1px solid {BORDER}; border-radius: 8px;
    padding: 9px 16px; font-size: 12.5px; font-weight: 500;
}}
QPushButton#Ghost:hover {{ background: {SURFACE_ALT}; color: {GOLD}; border-color: {GOLD_DIM}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 8px 11px;
    font-size: 13px;
    selection-background-color: {GOLD_DIM};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {GOLD};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {BG_DEEP}; color: {TEXT_FAINT}; border-color: {BORDER_SOFT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT}; border: 1px solid {BORDER};
    selection-background-color: {GOLD_SOFT}; selection-color: {GOLD};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; border: none; }}

QCheckBox {{ font-size: 13px; spacing: 9px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px; border-radius: 4px;
    border: 1px solid {BORDER}; background: {SURFACE_ALT};
}}
QCheckBox::indicator:checked {{ background: {GOLD}; border: 1px solid {GOLD}; }}
QCheckBox::indicator:disabled {{ background: {BG_DEEP}; border-color: {BORDER_SOFT}; }}
QCheckBox:disabled {{ color: {TEXT_FAINT}; }}

QGroupBox {{
    border: 1px solid {BORDER_SOFT}; border-radius: 8px;
    margin-top: 10px; padding-top: 10px; font-size: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {GOLD};
}}

/* ------------------------------- tables ------------------------------ */
QTableWidget, QTableView {{
    background: {SURFACE};
    alternate-background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    gridline-color: {BORDER_SOFT};
    font-size: 12.5px;
}}
QHeaderView::section {{
    background: {SURFACE_ALT}; color: {GOLD};
    border: none; border-bottom: 1px solid {BORDER};
    padding: 9px 10px; font-size: 10.5px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
}}
QTableWidget::item {{ padding: 8px 10px; border: none; }}
QTableWidget::item:selected {{ background: {GOLD_SOFT}; color: {TEXT}; }}

/* ----------------------------- scrollbars ---------------------------- */
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {GOLD_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QTabWidget::pane {{ border: 1px solid {BORDER_SOFT}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_MUTED};
    padding: 9px 16px; margin-right: 2px;
    border-bottom: 2px solid transparent; font-size: 12.5px;
}}
QTabBar::tab:selected {{ color: {GOLD}; border-bottom: 2px solid {GOLD}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* ------------------------------ statusbar ---------------------------- */
QFrame#StatusBar {{ background: {BG_DEEP}; border-top: 1px solid {BORDER_SOFT}; }}
QLabel#StatusText {{ font-size: 12px; color: {TEXT_MUTED}; }}

QTextEdit {{
    background: {BG_DEEP}; border: 1px solid {BORDER};
    border-radius: 8px; font-family: {MONO_STACK};
    font-size: 11.5px; color: {TEXT_MUTED};
}}

QToolTip {{
    background: {SURFACE_HI}; color: {TEXT};
    border: 1px solid {GOLD_DIM}; border-radius: 6px; padding: 6px 9px;
}}
"""
