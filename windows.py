# -*- coding: utf-8 -*-
"""独立窗口：通讯录、最近保存、传输，以及同步日志 / 下载日志。

设计：窗口负责创建并持有控件，MainWindow 只保留同名引用
（`contact_list` / `history_list` / `download_panel` …），所以原有的业务代码里
`self.contact_list.xxx` 这类调用完全不用改。

窗口关闭时只隐藏、不销毁（没有 `WA_DeleteOnClose`），后台线程仍在往这些控件写状态，
再次打开时内容还在。
"""
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout,
)

import download_log
import sync_log
import theme
import transfer

# 日志窗口「还没有取过指纹」的哨兵（文件不存在时指纹是 None）
_UNSET = object()


def _toolbar_button(text, primary=False):
    button = QPushButton(text)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if primary:
        button.setStyleSheet(
            "QPushButton{background:#2563eb;color:#ffffff;border:none;"
            "border-radius:8px;padding:8px 18px;font-weight:600;}"
            "QPushButton:hover{background:#1d4ed8;}")
    return button


class _BaseWindow(QDialog):
    """统一窗口外观：白底、卡片式内容区、关闭只隐藏。"""

    def __init__(self, main, title, size):
        super().__init__(main)
        self.main = main
        self.setWindowTitle(f"{title} · 净影")
        self.setWindowFlags(Qt.WindowType.Window)
        self.resize(*size)
        self.setStyleSheet(theme.APP_QSS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(10)
        self.card = theme.card_frame()
        outer.addWidget(self.card, 1)
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(8)

    def showEvent(self, event):
        super().showEvent(event)
        theme.apply_light_titlebar(self)


class ContactsWindow(_BaseWindow):
    """通讯录：作者列表与同步状态。

    四个操作按钮（全部同步 / 重试失败 / 批量导入 / 导入 txt）放在主窗口，
    这样不用先打开窗口就能发起同步。
    """

    def __init__(self, main):
        super().__init__(main, "通讯录", (780, 760))

        head = QHBoxLayout()
        self.title = QLabel("通讯录")
        self.title.setStyleSheet("font-size:15px;font-weight:600;")
        head.addWidget(self.title)
        head.addStretch(1)
        self.body.addLayout(head)

        self.status = QLabel("")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#2563eb;font-size:12px;")
        self.body.addWidget(self.status)

        self.sync_log = QLabel("")
        self.sync_log.setWordWrap(True)
        self.sync_log.setMaximumHeight(46)
        self.sync_log.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sync_log.setStyleSheet("color:#6b7280;font-size:11px;")
        self.sync_log.setVisible(False)
        self.body.addWidget(self.sync_log)

        self.list = QListWidget()
        self.list.setWordWrap(False)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setIconSize(QSize(26, 26))
        self.list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # 不使用焦点框；行内是自定义透明控件，避免 hover 时露出默认黑框/焦点虚线
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setStyleSheet(
            "QListWidget{background:#ffffff;border:1px solid #e3e9f2;border-radius:8px;padding:4px;outline:none;}"
            "QListWidget::item{color:#1f2937;padding:0;border:none;background:transparent;}"
            "QListWidget::item:hover{background:transparent;border:none;}"
            "QListWidget::item:selected{background:transparent;color:#1f2937;outline:none;border:none;}"
            "QListWidget::item:focus{outline:none;border:none;}"
            "QToolTip{background-color:#ffffff;color:#111111;border:1px solid #d8e0ec;padding:6px;}")
        self.body.addWidget(self.list, 1)

        self.more_btn = _toolbar_button("")
        self.more_btn.setVisible(False)
        self.body.addWidget(self.more_btn)

        self.hint = QLabel(
            "每次同步：作者顺序随机，已下载作品自动跳过，只下载新增作品。\n"
            "目录：净影/作者名_抖音号/作品id/（视频、图片、动图、音乐、文案都在里面）")
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#6b7280;font-size:11px;")
        self.body.addWidget(self.hint)


class HistoryWindow(_BaseWindow):
    """最近保存：已下载作品列表。"""

    def __init__(self, main):
        super().__init__(main, "最近保存", (720, 720))

        head = QHBoxLayout()
        self.title = QLabel("最近保存")
        self.title.setStyleSheet("font-size:15px;font-weight:600;")
        head.addWidget(self.title)
        head.addStretch(1)
        self.open_dir_btn = _toolbar_button("打开目录")
        head.addWidget(self.open_dir_btn)
        self.clear_btn = _toolbar_button("清空")
        head.addWidget(self.clear_btn)
        self.body.addLayout(head)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setStyleSheet(
            "QListWidget{background:#ffffff;border:1px solid #e3e9f2;border-radius:8px;padding:4px;outline:none;}"
            "QListWidget::item{color:#1f2937;padding:8px 10px;border-radius:6px;}"
            "QListWidget::item:hover{background:#f7faff;}"
            "QListWidget::item:selected{background:#eaf1ff;color:#1f2937;outline:none;}"
            "QScrollBar:vertical{background:#ffffff;width:8px;border-radius:4px;}"
            "QScrollBar::handle:vertical{background:#e5eaf3;border-radius:4px;min-height:24px;}")
        self.body.addWidget(self.list, 1)

        self.more_btn = _toolbar_button("")
        self.more_btn.setVisible(False)
        self.body.addWidget(self.more_btn)


class _LogWindow(_BaseWindow):
    """只读日志窗口基类：可见时自动刷新，另有刷新/清空按钮。"""

    LABEL = "日志"

    def __init__(self, main, title, size):
        super().__init__(main, title, size)

        head = QHBoxLayout()
        self.title = QLabel(self.LABEL)
        self.title.setStyleSheet("font-size:15px;font-weight:600;")
        head.addWidget(self.title)
        head.addStretch(1)
        self.refresh_btn = _toolbar_button("刷新")
        head.addWidget(self.refresh_btn)
        self.clear_btn = _toolbar_button("清空")
        head.addWidget(self.clear_btn)
        self.body.addLayout(head)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setStyleSheet(
            "QPlainTextEdit{background:#ffffff;color:#374151;border:1px solid #e3e9f2;"
            "border-radius:8px;padding:8px;font-size:12px;"
            "font-family:'Microsoft YaHei UI','Consolas',monospace;}")
        self.body.addWidget(self.text, 1)

        self._last_sig = _UNSET
        # 窗口开着时每 1.2 秒看一眼文件有没有变化，有变化就自动刷新
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(1200)
        self._auto_timer.timeout.connect(self._auto_refresh)
        self._auto_timer.start()
        self.reload()

    # 子类实现
    def _signature(self):
        raise NotImplementedError

    def _load_lines(self):
        raise NotImplementedError

    def _clear_store(self):
        raise NotImplementedError

    def reload(self):
        sig = self._signature()
        if sig == self._last_sig:
            return
        lines = self._load_lines()
        if lines is None:
            # 写盘被外部占用、拿不到锁：这次跳过刷新，保留窗口已有内容。
            # 不能把 None 当成空日志，否则窗口会闪一下变空再恢复。
            return
        # load() 可能把后台队列写盘，重新取一次指纹
        self._last_sig = self._signature()
        text = "\n".join(lines)
        if text != self.text.toPlainText():
            bar = self.text.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 4
            pos = bar.value()
            self.text.setPlainText(text)
            if at_bottom:
                # 原来就在底部：跟随最新一条
                cursor = self.text.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                self.text.setTextCursor(cursor)
            else:
                # 用户往上翻了：别把他拽回底部
                bar.setValue(min(pos, bar.maximum()))
        self.title.setText(f"{self.LABEL}（{len(lines)} 条）")

    def _auto_refresh(self):
        if self.isVisible():
            self.reload()

    def clear_log(self):
        self._clear_store()
        self._last_sig = _UNSET
        self.reload()


class SyncLogWindow(_LogWindow):
    """同步日志：开始/完成同步、账号与作者、找到多少作品。"""

    LABEL = "同步日志"

    def __init__(self, main):
        super().__init__(main, "同步日志", (720, 560))

    def _signature(self):
        # 写盘在后台线程排队，队列里还有没落盘的行时也算「有新内容」
        return (sync_log.signature(), sync_log.pending())

    def _load_lines(self):
        return sync_log.load()

    def _clear_store(self):
        sync_log.clear()


class DownloadLogWindow(_LogWindow):
    """下载日志：开始/完成/失败、作者、作品和该作者剩余未下载数。"""

    LABEL = "下载日志"

    def __init__(self, main):
        super().__init__(main, "下载日志", (760, 580))

    def _signature(self):
        # 队列里还有没落盘的行时，也要算作「有新内容」
        return (download_log.signature(), download_log.pending())

    def _load_lines(self):
        return download_log.load()

    def _clear_store(self):
        download_log.clear()


class TransferWindow(_BaseWindow):
    """传输：下载 / 上传分开显示进度、速度和任务列表。"""

    def __init__(self, main):
        super().__init__(main, "传输", (900, 700))

        head = QHBoxLayout()
        title = QLabel("传输")
        title.setStyleSheet("font-size:15px;font-weight:600;")
        head.addWidget(title)
        head.addSpacing(16)
        head.addWidget(QLabel("下载线程"))
        self.dl_combo = QComboBox()
        self.dl_combo.addItems(["2", "3", "4", "6", "8"])
        self.dl_combo.setFixedWidth(64)
        head.addWidget(self.dl_combo)
        head.addWidget(QLabel("上传线程"))
        self.up_combo = QComboBox()
        self.up_combo.addItems(["2", "3", "4", "6", "8"])
        self.up_combo.setFixedWidth(64)
        head.addWidget(self.up_combo)
        head.addStretch(1)
        self.clear_btn = _toolbar_button("清空")
        head.addWidget(self.clear_btn)
        self.body.addLayout(head)

        self.download_panel = transfer.TransferPanel(
            "下载", accent="#2563eb", compact=False, parent=self.card)
        self.upload_panel = transfer.TransferPanel(
            "上传", accent="#0f9d76", compact=False, parent=self.card)
        self.body.addWidget(self.download_panel, 1)
        self.body.addWidget(self.upload_panel, 1)
