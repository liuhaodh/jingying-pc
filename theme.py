# -*- coding: utf-8 -*-
"""浅色主题：以白色为主，统一控件样式。"""
import os

from PySide6.QtWidgets import QFrame


def card_frame() -> QFrame:
    """白底圆角卡片，主窗口和各个独立窗口共用。"""
    frame = QFrame()
    frame.setStyleSheet(
        "QFrame{background:#ffffff;border:1px solid #e3e9f2;border-radius:10px;}"
        "QLabel{background:transparent;color:#1f2937;}"
        "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #d8e0ec;border-radius:8px;"
        "padding:8px 14px;}"
        "QPushButton:hover{background:#e5eaf3;}"
        "QPushButton:focus{outline:none;border:1px solid #d8e0ec;}"
        "QPushButton:disabled{color:#6b7280;}"
        "QLineEdit{background:#ffffff;color:#1f2937;border:1px solid #d8e0ec;"
        "border-radius:8px;padding:10px;}"
        "QListWidget{background:transparent;border:none;}"
    )
    return frame


def apply_light_titlebar(widget):
    """Windows 10/11：把原生标题栏改为浅色，避免顶部一条黑色。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(0)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        # DWMWA_CAPTION_COLOR = 35（Windows 11）：白色
        color = ctypes.c_int(0x00FFFFFF)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass


APP_QSS = """
QWidget {
    color: #1f2937;
    background: transparent;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
}
QMainWindow, QDialog {
    background: #ffffff;
}
QWidget#root {
    background: #ffffff;
}
QLineEdit, QTextEdit, QPlainTextEdit {
    background: #ffffff;
    color: #1f2937;
    border: 1px solid #d8e0ec;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #cfe0ff;
    selection-color: #1f2937;
}
QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #2563eb;
}
QPushButton {
    background: #ffffff;
    color: #1f2937;
    border: 1px solid #d8e0ec;
    border-radius: 8px;
    padding: 7px 14px;
}
QPushButton:hover {
    background: #eef4ff;
    border-color: #b9cff5;
}
QPushButton:focus {
    outline: none;
    border-color: #b9cff5;
}
QPushButton:pressed {
    background: #dfe9fb;
}
QPushButton:disabled {
    color: #9aa4b2;
    background: #f3f6fb;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #e3e9f2;
    border-radius: 10px;
    outline: none;
}
QListWidget::item {
    color: #374151;
    padding: 6px 8px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background: #dbe7ff;
    color: #1f2937;
    outline: none;
}
QAbstractItemView {
    outline: none;
}
QListWidget::item:hover {
    background: #f0f5ff;
}
QComboBox {
    background: #ffffff;
    color: #1f2937;
    border: 1px solid #d8e0ec;
    border-radius: 8px;
    padding: 6px 8px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1f2937;
    selection-background-color: #dbe7ff;
    selection-color: #1f2937;
    border: 1px solid #d8e0ec;
}
QProgressBar {
    background: #e5eaf3;
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: #2563eb;
    border-radius: 5px;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #cdd7e5;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #b6c4d8;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #cdd7e5;
    border-radius: 4px;
    min-width: 30px;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #dbe3ef;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #2563eb;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #2563eb;
    width: 12px;
    margin: -4px 0;
    border-radius: 6px;
}
QMessageBox, QInputDialog {
    background: #ffffff;
}
QToolTip {
    background: #ffffff;
    color: #1f2937;
    border: 1px solid #d8e0ec;
    padding: 4px;
}
"""

# QToolTip 是顶层窗口，只设置 MainWindow 的样式表可能覆盖不到它；
# 程序启动时把这段挂到 QApplication 上，避免 Windows 默认黑色边框提示框。
TOOLTIP_QSS = """
QToolTip {
    background-color: #ffffff;
    color: #1f2937;
    border: 1px solid #d8e0ec;
    border-radius: 6px;
    padding: 6px 8px;
}
"""
