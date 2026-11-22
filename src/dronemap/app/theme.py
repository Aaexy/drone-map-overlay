"""Look and feel: colour tokens and the app stylesheet (dark and light)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

ACCENT = "#ff5c36"

DARK = dict(
    bg="#0c0f14",
    surface="#141920",
    surface2="#1b212a",
    raised="#222935",
    border="#262e3a",
    border_hi="#343e4d",
    text="#e9edf3",
    muted="#8b95a7",
    faint="#5a6476",
    accent=ACCENT,
    accent_hi="#ff7756",
    accent_soft="rgba(255,92,54,0.14)",
    success="#3ddc97",
    track="#2a323f",
    grid="#1a2029",
    canvas="#07090c",
)
LIGHT = dict(
    bg="#f3f4f6",
    surface="#ffffff",
    surface2="#f0f2f5",
    raised="#e8ebf0",
    border="#dfe3e9",
    border_hi="#c9d0da",
    text="#11151b",
    muted="#5c6676",
    faint="#9aa3b1",
    accent=ACCENT,
    accent_hi="#ff7756",
    accent_soft="rgba(255,92,54,0.12)",
    success="#1fae6f",
    track="#d9dee6",
    grid="#e9ecf0",
    canvas="#dfe3e8",
)

T = dict(DARK)  # current tokens; widgets read from here when painting


def resolve(mode: str) -> str:
    if mode in ("dark", "light"):
        return mode
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"
    except Exception:
        return "dark"


def apply(app: QApplication, mode: str = "dark"):
    m = resolve(mode)
    T.clear()
    T.update(DARK if m == "dark" else LIGHT)
    app.setStyle(QStyleFactory.create("Fusion"))
    try:  # dark/light native title bars (Qt 6.8+)
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if m == "dark" else Qt.ColorScheme.Light)
    except Exception:
        pass
    pal = QPalette()
    c = lambda k: QColor(T[k]) if not T[k].startswith("rgba") else QColor(T["raised"])
    pal.setColor(QPalette.ColorRole.Window, c("bg"))
    pal.setColor(QPalette.ColorRole.WindowText, c("text"))
    pal.setColor(QPalette.ColorRole.Base, c("surface2"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("surface"))
    pal.setColor(QPalette.ColorRole.Text, c("text"))
    pal.setColor(QPalette.ColorRole.Button, c("surface2"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("text"))
    pal.setColor(QPalette.ColorRole.Highlight, c("accent"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c("raised"))
    pal.setColor(QPalette.ColorRole.ToolTipText, c("text"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c("faint"))
    pal.setColor(QPalette.ColorRole.Mid, c("border"))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
    for w in app.allWidgets():
        w.update()
    return m


def stylesheet() -> str:
    return (
        """
* { outline: none; }
QWidget { color: %(text)s; font-size: 13px; }
QMainWindow, QDialog { background: %(bg)s; }
QLabel { background: transparent; }
QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="faint"] { color: %(faint)s; font-size: 12px; }
QLabel[role="h1"] { font-size: 22px; font-weight: 700; }
QLabel[role="wordmark"] { font-size: 15px; font-weight: 700; letter-spacing: 0.2px; }
QLabel[role="chip"] { color: %(muted)s; background: %(surface2)s; border: 1px solid %(border)s;
    border-radius: 8px; padding: 1px 7px; font-size: 11px; }
QLabel[role="section"] { color: %(muted)s; font-size: 11px; font-weight: 700; letter-spacing: 1.4px; }
QLabel[role="pct"] { color: %(text)s; font-weight: 700; font-size: 13px; }

QFrame[role="card"] { background: %(surface)s; border: 1px solid %(border)s; border-radius: 14px; }

QPushButton { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 9px;
    padding: 7px 14px; color: %(text)s; font-weight: 600; }
QPushButton:hover { background: %(raised)s; border-color: %(border_hi)s; }
QPushButton:pressed { background: %(border)s; }
QPushButton:disabled { color: %(faint)s; }
QPushButton[kind="primary"] { background: %(accent)s; border: 1px solid %(accent)s; color: white; padding: 9px 20px; }
QPushButton[kind="primary"]:hover { background: %(accent_hi)s; border-color: %(accent_hi)s; }
QPushButton[kind="ghost"] { background: transparent; border: 1px solid transparent; color: %(muted)s; }
QPushButton[kind="ghost"]:hover { background: %(surface2)s; color: %(text)s; }
QPushButton[kind="seg"] { background: transparent; border: none; border-radius: 7px; padding: 6px 10px;
    color: %(muted)s; font-weight: 600; }
QPushButton[kind="seg"]:hover { color: %(text)s; }
QPushButton[kind="seg"]:checked { background: %(raised)s; color: %(text)s; }
QWidget[role="segbar"] { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 9px; }

QLineEdit { background: %(surface2)s; border: 1px solid %(border)s; border-radius: 8px; padding: 6px 9px;
    selection-background-color: %(accent)s; }
QLineEdit:focus { border-color: %(accent)s; }

QSlider::groove:horizontal { height: 4px; background: %(track)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; border: 1px solid rgba(0,0,0,0.25); }
QSlider::handle:horizontal:hover { background: #fff; border: 2px solid %(accent)s; }

QProgressBar { background: %(track)s; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(border_hi)s; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip { background: %(raised)s; color: %(text)s; border: 1px solid %(border_hi)s; border-radius: 6px; padding: 4px 8px; }
QMessageBox { background: %(surface)s; }
QMenu { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 14px; border-radius: 5px; }
QMenu::item:selected { background: %(accent_soft)s; }
"""
        % T
    )
