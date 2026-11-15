# -*- coding: utf-8 -*-
"""传输统计与面板：下载、上传各自显示进度条、速度和任务列表。"""
import threading
import time
from collections import deque

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QSizePolicy, QVBoxLayout, QWidget,
)

try:  # PySide6 一定带 shiboken6；留个兜底，避免裁剪/打包环境差异导致 import 失败
    from shiboken6 import isValid as _qt_valid
except ImportError:  # pragma: no cover
    def _qt_valid(obj):
        return obj is not None

MB = 1024.0 * 1024.0
# 传输列表最多保留的行数：行数越多，insertItem(0) 重排所有 itemWidget 越慢。
MAX_VISIBLE_ROWS = 60


def _dispose_row_widget(widget):
    """显式回收 setItemWidget 挂上去的自定义行。

    QListWidget.clear()/takeItem() 只删 QListWidgetItem，挂在 viewport 上的
    QWidget 不会被自动回收。一轮全量同步有上万条记录时，会累积上千个孤儿
    QWidget（每个含 3 个 QLabel + 进度条 + 布局），Qt 在后续布局/重绘里可能
    直接 0xc0000005 崩在 QtWidgets.pyd。removeItemWidget 可能已经由 Qt 删掉
    C++ 对象，所以这里先判有效性再操作。
    """
    if widget is None or not _qt_valid(widget):
        return
    try:
        widget.setParent(None)
        widget.deleteLater()
    except RuntimeError:  # C++ 对象已被 Qt 删除
        pass


def dispose_list_widgets(list_widget):
    """清空 QListWidget 前调用：解绑并回收所有自定义行 widget。"""
    if list_widget is None:
        return
    for index in range(list_widget.count()):
        item = list_widget.item(index)
        widget = list_widget.itemWidget(item)
        if widget is not None:
            list_widget.removeItemWidget(item)
            _dispose_row_widget(widget)


def format_rate(bytes_per_sec) -> str:
    rate = max(0.0, float(bytes_per_sec or 0))
    return f"{rate / MB:.2f} MB/s"


def format_bytes(count) -> str:
    value = max(0.0, float(count or 0))
    if value >= MB:
        return f"{value / MB:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value:.0f} B"


class TransferMeter:
    """线程安全的字节计数器；速度按最近约 2.5 秒滑动窗口计算。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._samples = deque(maxlen=32)
        self._started = time.monotonic()

    def reset(self):
        with self._lock:
            self._total = 0
            self._samples.clear()
            self._started = time.monotonic()

    def add(self, count):
        try:
            count = int(count)
        except (TypeError, ValueError):
            return
        if count <= 0:
            return
        with self._lock:
            self._total += count
            now = time.monotonic()
            if not self._samples or now - self._samples[-1][0] >= 0.15:
                self._samples.append((now, self._total))

    def total(self) -> int:
        with self._lock:
            return self._total

    def rate(self) -> float:
        with self._lock:
            now = time.monotonic()
            # 一段时间没有新数据，说明已经传完，速度归零
            if not self._samples or now - self._samples[-1][0] > 2.0:
                return 0.0
            cutoff = now - 2.5
            while len(self._samples) > 1 and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if len(self._samples) < 2:
                elapsed = max(0.001, now - self._started)
                return self._total / elapsed if elapsed >= 0.3 else 0.0
            t0, b0 = self._samples[0]
            t1, b1 = self._samples[-1]
            if t1 <= t0:
                return 0.0
            return max(0.0, (b1 - b0) / (t1 - t0))


class TransferPanel(QFrame):
    """一个方向（下载或上传）的进度、速度和任务列表。"""

    def __init__(self, title, accent="#2563eb", thread_combo=None,
                 thread_label="线程", compact=True, parent=None):
        super().__init__(parent)
        self.setObjectName("transferPanel")
        self.accent = accent
        self.compact = compact
        self._rows = {}
        self.last_rate = 0.0
        self.last_detail = ""
        self.setStyleSheet(f"""
            QFrame#transferPanel {{
                background: #f8fafc;
                border: 1px solid #e3e9f2;
                border-radius: 10px;
            }}
            QFrame#transferPanel QLabel {{
                background: transparent;
                color: #374151;
            }}
            QListWidget {{
                background: #ffffff;
                border: 1px solid #eef2f7;
                border-radius: 8px;
                padding: 2px;
                outline: none;
            }}
            QListWidget::item {{
                color: #4b5563;
                padding: 4px 6px;
                border-radius: 5px;
                font-size: 11px;
            }}
            QListWidget::item:hover {{
                background: #f0f5ff;
            }}
            QProgressBar {{
                background: #e5eaf3;
                border: none;
                border-radius: 4px;
                height: 8px;
                text-align: center;
                color: transparent;
            }}
            QProgressBar::chunk {{
                background: {accent};
                border-radius: 4px;
            }}
            QComboBox {{
                background: #ffffff;
                color: #374151;
                border: 1px solid #d8e0ec;
                border-radius: 6px;
                padding: 2px 4px;
                font-size: 11px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size:13px;font-weight:700;color:#1f2937;")
        head.addWidget(self.title_label)
        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("font-size:11px;color:#6b7280;")
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        head.addWidget(self.detail_label, 1)
        if thread_combo is not None:
            label = QLabel(thread_label)
            label.setStyleSheet("font-size:11px;color:#6b7280;")
            head.addWidget(label)
            thread_combo.setFixedWidth(56)
            thread_combo.setToolTip(f"{title}线程数（对下一次任务生效）")
            head.addWidget(thread_combo)
        self.speed_label = QLabel("0.00 MB/s")
        self.speed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.speed_label.setMinimumWidth(96)
        self.speed_label.setStyleSheet(
            f"font-size:11px;font-weight:600;color:{accent};background:transparent;")
        head.addWidget(self.speed_label)
        layout.addLayout(head)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setWordWrap(False)
        # 每行高度固定 36px：告诉 Qt 不必为每个 itemWidget 重算尺寸，长队列
        # 同步时 insertItem 触发的全列表重排会便宜很多。
        self.list.setUniformItemSizes(True)
        if compact:
            self.list.setMinimumHeight(114)
            self.list.setMaximumHeight(156)
        else:
            self.list.setMinimumHeight(160)
        layout.addWidget(self.list)

    # ---------- 任务条目（每个任务一条独立进度条） ----------
    def _build_row(self, title):
        """造一套行控件。只有列表还没满 60 行时才会调用；满了走 add_item
        的行复用，避免长同步里每个作品都新建/销毁 QWidget。"""
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(4, 3, 4, 3)
        box.setSpacing(2)
        line = QHBoxLayout()
        line.setSpacing(6)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size:11px;color:#374151;")
        title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_label.setToolTip(title)
        line.addWidget(title_label, 1)
        status_label = QLabel("等待中…")
        status_label.setStyleSheet("font-size:10px;color:#6b7280;")
        line.addWidget(status_label)
        percent_label = QLabel("0%")
        percent_label.setStyleSheet("font-size:10px;color:#6b7280;")
        percent_label.setMinimumWidth(30)
        percent_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                   Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(percent_label)
        box.addLayout(line)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(5)
        box.addWidget(bar)
        widget.setMinimumHeight(36)
        return widget, {
            "widget": widget,
            "title": title_label,
            "status": status_label,
            "percent": percent_label,
            "bar": bar,
            "title_text": title,
        }

    def add_item(self, key, title):
        row = self._rows.get(key)
        if row is not None:
            # 迟到的信号可能拿到已经被回收的行：别再碰失效的 C++ 对象。
            if not _qt_valid(row.get("widget")):
                self._rows.pop(key, None)
            else:
                row["title"].setText(title)
                row["title"].setToolTip(title)
                row["title_text"] = title
                return

        # 以前满 60 行直接 return，一轮同步后面的任务全不显示；改成「每来一个
        # 新任务就新建一套控件 + deleteLater 回收旧行」后，真实长同步实测主线程
        # UI stall 57s 并在 Qt C 层段错误退出。现在的做法是**复用最老一行的
        # 控件**：只改文本/进度，把它的 item 挪到顶部，全程零创建、零 deleteLater。
        reuse_item = None
        reuse_row = None
        if self.list.count() >= MAX_VISIBLE_ROWS:
            reuse_item = self.list.item(self.list.count() - 1)
            if reuse_item is None:
                return
            old_key = reuse_item.data(Qt.ItemDataRole.UserRole)
            reuse_row = self._rows.pop(old_key, None)

        if reuse_row is not None and _qt_valid(reuse_row.get("widget")):
            widget = reuse_row["widget"]
            row = reuse_row
            row["status"].setText("等待中…")
            row["percent"].setText("0%")
            row["bar"].setValue(0)
        else:
            widget, row = self._build_row(title)

        item = reuse_item if reuse_item is not None else QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setSizeHint(QSize(0, 36))
        row["item"] = item
        row["title"].setText(title)
        row["title"].setToolTip(title)
        row["title_text"] = title
        self._rows[key] = row

        # 解绑 → 摘行 → 插到最前 → 重新绑定同一个 widget，全程关闭重绘。
        self.list.setUpdatesEnabled(False)
        try:
            if reuse_item is not None:
                old_widget = self.list.itemWidget(reuse_item)
                self.list.removeItemWidget(reuse_item)
                if reuse_row is None:
                    _dispose_row_widget(old_widget)
                elif _qt_valid(widget):
                    # removeItemWidget 后控件仍挂在 viewport 上，先脱离 parent
                    # 才能被 setItemWidget 重新挂回（否则会留下孤儿控件）。
                    widget.setParent(None)
                self.list.takeItem(self.list.count() - 1)
            self.list.insertItem(0, item)
            self.list.setItemWidget(item, widget)
        finally:
            self.list.setUpdatesEnabled(True)

    def update_item(self, key, status):
        row = self._rows.get(key)
        if row is None:
            return
        if not _qt_valid(row.get("widget")):
            self._rows.pop(key, None)
            return
        row["status"].setText(status)
        if status in ("完成", "跳过（已上传）"):
            self.set_item_progress(key, 100)

    def set_item_progress(self, key, percent):
        row = self._rows.get(key)
        if row is None:
            return
        if not _qt_valid(row.get("widget")):
            self._rows.pop(key, None)
            return
        try:
            percent = int(max(0, min(100, float(percent))))
        except (TypeError, ValueError):
            percent = 0
        row["bar"].setValue(percent)
        row["percent"].setText(f"{percent}%")

    def item_keys(self):
        keys = []
        for i in range(self.list.count()):
            keys.append(self.list.item(i).data(Qt.ItemDataRole.UserRole))
        return keys

    def snapshot(self):
        result = []
        for key in self.item_keys():
            row = self._rows.get(key)
            if row is None:
                continue
            if not _qt_valid(row.get("widget")):
                self._rows.pop(key, None)
                continue
            result.append({
                "key": key,
                "title": row["title_text"],
                "status": row["status"].text(),
                "percent": row["bar"].value(),
            })
        return result

    def sync_from(self, other, force=False):
        """把另一个面板的任务状态同步过来（用于「传输列表」窗口）。

        「传输列表」每 400 ms 刷新一次，任务行最多 120 条；内容没变时直接返回，
        避免空转时每帧都重设几十个控件的文字/进度。
        """
        entries = other.snapshot()
        signature = tuple((entry["key"], entry["title"], entry["status"], entry["percent"])
                          for entry in entries)
        if not force and signature == getattr(self, "_sync_signature", None):
            return
        self._sync_signature = signature
        wanted = {entry["key"]: entry for entry in entries}
        # 删除对方已经清掉的任务
        for i in range(self.list.count() - 1, -1, -1):
            item = self.list.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key not in wanted:
                old_widget = self.list.itemWidget(item)
                self.list.removeItemWidget(item)
                old_row = self._rows.pop(key, None)
                _dispose_row_widget(old_widget or (old_row or {}).get("widget"))
                self.list.takeItem(i)
        for entry in reversed(entries):
            key = entry["key"]
            row = self._rows.get(key)
            if row is None or not _qt_valid(row.get("widget")):
                self._rows.pop(key, None)
                self.add_item(key, entry["title"])
            else:
                if row["title_text"] != entry["title"]:
                    row["title_text"] = entry["title"]
                    row["title"].setText(entry["title"])
                    row["title"].setToolTip(entry["title"])
            self.update_item(key, entry["status"])
            self.set_item_progress(key, entry["percent"])

    def texts(self):
        return [row["title_text"] for row in self._rows.values()]

    def clear_items(self):
        dispose_list_widgets(self.list)
        self.list.clear()
        self._rows.clear()
        self._sync_signature = None

    # ---------- 进度 ----------
    def set_stats(self, percent, rate, detail=""):
        try:
            percent = int(max(0, min(100, float(percent))))
        except (TypeError, ValueError):
            percent = 0
        self.progress.setValue(percent)
        self.last_rate = float(rate or 0)
        self.speed_label.setText(format_rate(self.last_rate))
        self.last_detail = detail or ""
        self.detail_label.setText(self.last_detail)

    def set_idle(self, detail=""):
        self.progress.setValue(0)
        self.last_rate = 0.0
        self.speed_label.setText("0.00 MB/s")
        self.last_detail = detail or ""
        self.detail_label.setText(self.last_detail)

    def copy_stats_from(self, other):
        self.progress.setValue(other.progress.value())
        self.speed_label.setText(other.speed_label.text())
        self.detail_label.setText(other.detail_label.text())
        self.last_rate = other.last_rate
        self.last_detail = other.last_detail
