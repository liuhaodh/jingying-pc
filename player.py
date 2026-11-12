# -*- coding: utf-8 -*-
"""净影播放器：视频 + 图集混合信息流，右侧作品列表（按作者筛选）。

布局：
- 左下：视频/图片
- 右下：作者、文案（右对齐）+ 进度条 + 上一条/切换视频/下一条
- 右侧：作品列表，可按作者筛选，点击直接播放对应作品
"""
import os
import shutil
import tempfile
import time as _time

from PySide6.QtCore import Qt, QTimer, QUrl, QSizeF
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QLabel, QSlider, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QStackedWidget, QSizePolicy, QListWidget, QListWidgetItem,
    QComboBox, QAbstractItemView, QCheckBox, QGraphicsView, QGraphicsScene,
)
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor

import archive
import config
import theme

# 图集多张照片时的自动切换间隔（毫秒）
SLIDESHOW_INTERVAL_MS = 3000


def _remove_tree(path, attempts=4, delay=0.05):
    """删掉一个目录，删不掉就短暂重试几次。

    Windows 上文件句柄的释放有一点延迟：`QMediaPlayer.stop()` 之后 FFmpeg
    后端可能还短暂持有本地文件，这时 `shutil.rmtree(ignore_errors=True)`
    会静默失败，解包缓存就留在临时目录里越堆越多。所以这里失败后重试。
    """
    for _ in range(max(1, attempts)):
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        _time.sleep(delay)
    return not os.path.exists(path)


class PlayerWindow(QWidget):
    """feed: list[dict]
    {"type":"video","path":..., "title":..., "author":...}
    {"type":"album","images":[...], "title":..., "author":..., "music":...}
    """

    def __init__(self, feed, start=0, on_sync=None):
        super().__init__()
        self.on_sync = on_sync
        self._row_labels = {}
        self._row_base_text = {}
        self.feed = []
        for item in feed:
            if isinstance(item, dict):
                self.feed.append(item)
            else:
                path, title, author = (list(item) + ["", "", ""])[:3]
                self.feed.append({"type": "video", "path": path,
                                  "title": title, "author": author})
        self.index = max(0, min(start, len(self.feed) - 1)) if self.feed else 0
        self._loaded_index = None
        self.image_index = 0
        self.tracking = False
        self._drag_start = None
        self._drag_active = False
        self._visible_indices = []
        self.page = 1
        self._pix_cache = {}
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._render_image)

        self.setWindowTitle("净影 · 观看已下载")
        import theme; self.setStyleSheet(theme.APP_QSS)
        self.resize(1000, 760)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_ui()

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_item)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_playback_state)

        self.music_player = QMediaPlayer(self)
        self.music_audio = QAudioOutput(self)
        self.music_player.setAudioOutput(self.music_audio)

        # 实况图播放器（图集里的小视频）
        self.live_player = QMediaPlayer(self)
        self.live_audio = QAudioOutput(self)
        self.live_audio.setVolume(0.6)
        self.live_player.setAudioOutput(self.live_audio)
        self.live_player.setVideoOutput(self.live_item)
        self.live_player.setLoops(QMediaPlayer.Loops.Infinite)
        self.live_player.mediaStatusChanged.connect(self._on_live_status)
        self.live_player.errorOccurred.connect(self._on_live_error)
        self._live_current = ""
        self._live_token = 0

        self.image_timer = QTimer(self)
        self.image_timer.timeout.connect(self._auto_next_image)

        self._rebuild_list()
        self._load(self.index)

    # ---------- UI ----------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧播放区
        left = QWidget(self)
        left.setStyleSheet("background:#ffffff;")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.stack = QStackedWidget(left)

        def make_video_view(parent):
            view = QGraphicsView(parent)
            view.setStyleSheet("background:#000;border:none;")
            view.setBackgroundBrush(QColor("#000000"))
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setMouseTracking(True)
            view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            # QGraphicsVideoItem 每帧都会重绘；关闭抗锯齿/平滑缩放，减少 CPU 缩放。
            view.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            view.setOptimizationFlag(
                QGraphicsView.OptimizationFlag.DontSavePainterState, True)
            view.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
            view.setCacheMode(QGraphicsView.CacheModeFlag.CacheNone)
            # Windows 下优先用 OpenGL 视口，让视频帧走 GPU 缩放；没有 OpenGL
            # 驱动时自动退回普通 QWidget 视口，不影响播放。
            if (os.name == "nt"
                    and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
                    and os.environ.get("JINGYING_VIDEO_OPENGL", "1") != "0"):
                try:
                    from PySide6.QtOpenGLWidgets import QOpenGLWidget
                    from PySide6.QtGui import QOpenGLContext
                    probe = QOpenGLContext()
                    if probe.create():
                        gl_viewport = QOpenGLWidget()
                        gl_viewport.setAttribute(
                            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
                        view.setViewport(gl_viewport)
                        view.setViewportUpdateMode(
                            QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
                except Exception:  # noqa: BLE001
                    pass
            scene = QGraphicsScene(view)
            view.setScene(scene)
            item = QGraphicsVideoItem()
            item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            scene.addItem(item)
            return view, scene, item

        self.video_view, self.video_scene, self.video_item = make_video_view(left)
        self.video_widget = self.video_view
        self.stack.addWidget(self.video_view)
        self.image_label = QLabel("", left)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background:#f5f7fb;color:#6b7280;")
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.stack.addWidget(self.image_label)
        self.live_view, self.live_scene, self.live_item = make_video_view(left)
        self.live_widget = self.live_view
        self.stack.addWidget(self.live_view)
        left_layout.addWidget(self.stack, 1)

        # 文案浮层：覆盖在视频/图片上方，点击浮层可隐藏。
        # 注意要作为 stack 的兄弟节点（而不是 stack 的子节点），否则切换视频时
        # QStackedWidget 会把当前视频页 raise 到浮层上面，部分视频只剩黑底、看不到文案。
        self.info_visible = True
        self.info_overlay = QWidget(left)
        self.info_overlay.setObjectName("captionOverlay")
        self.info_overlay.setStyleSheet(
            "QWidget#captionOverlay{background:rgba(15,23,42,150);border-radius:12px;}"
            "QLabel{background:transparent;color:#ffffff;}")
        self.info_overlay.setCursor(Qt.CursorShape.PointingHandCursor)
        self.info_overlay.setToolTip("点击隐藏文案")
        overlay_layout = QHBoxLayout(self.info_overlay)
        overlay_layout.setContentsMargins(14, 10, 14, 10)
        overlay_layout.setSpacing(10)
        self.avatar_label = QLabel(self.info_overlay)
        self.avatar_label.setFixedSize(44, 44)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_label.setStyleSheet(
            "background:#1e293b;color:#ffffff;border-radius:22px;"
            "font-size:16px;font-weight:700;")
        overlay_layout.addWidget(self.avatar_label, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.author_label = QLabel("", self.info_overlay)
        self.author_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.author_label.setStyleSheet("color:#93c5fd;font-size:15px;font-weight:700;")
        info.addWidget(self.author_label)
        self.meta_label = QLabel("", self.info_overlay)
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.meta_label.setStyleSheet("color:#cbd5e1;font-size:11px;")
        info.addWidget(self.meta_label)
        self.title_label = QLabel("", self.info_overlay)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.title_label.setWordWrap(True)
        self.title_label.setMaximumHeight(54)
        self.title_label.setStyleSheet("color:#f8fafc;font-size:13px;")
        info.addWidget(self.title_label)
        overlay_layout.addLayout(info, 1)
        for child in (self.avatar_label, self.author_label,
                      self.meta_label, self.title_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.info_overlay.installEventFilter(self)
        self.info_overlay.hide()

        # 底部进度与按钮
        panel = QWidget(left)
        panel.setStyleSheet("background:#ffffff;")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 6, 16, 10)
        panel_layout.setSpacing(6)
        self.seek = QSlider(Qt.Orientation.Horizontal, panel)
        self.seek.setRange(0, 1000)
        self.seek.setStyleSheet(
            "QSlider::groove:horizontal{height:4px;background:#d8e0ec;border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#2563eb;border-radius:2px;}"
            "QSlider::handle:horizontal{width:12px;margin:-4px 0;border-radius:6px;background:#2563eb;}")
        self.seek.sliderPressed.connect(lambda: setattr(self, "tracking", True))
        self.seek.sliderReleased.connect(self._on_seek_released)
        self.seek.sliderMoved.connect(self._on_seek_moved)
        panel_layout.addWidget(self.seek)
        row = QHBoxLayout()
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color:#6b7280;font-size:11px;")
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.autoplay_cb = QCheckBox("自动连播")
        self.autoplay_cb.setChecked(True)
        self.autoplay_cb.setStyleSheet("QCheckBox{color:#374151;font-size:12px;}")
        self.autoplay_cb.setToolTip("当前视频播放结束后自动播放下一条")
        row.addWidget(self.autoplay_cb)
        self.info_btn = QPushButton("隐藏文案")
        self.info_btn.setToolTip("显示/隐藏覆盖在画面上的文案")
        row.addWidget(self.info_btn)
        self.list_btn = QPushButton("列表")
        self.play_btn = QPushButton("暂停")
        self.prev_btn = QPushButton("上一条")
        self.switch_btn = QPushButton("切换视频")
        self.next_btn = QPushButton("下一条")
        for btn in (self.info_btn, self.list_btn, self.play_btn, self.prev_btn,
                    self.switch_btn, self.next_btn):
            btn.setStyleSheet(
                "QPushButton{background:#ffffff;color:#1f2937;border:1px solid #d8e0ec;border-radius:8px;"
                "padding:7px 12px;font-size:12px;}"
                "QPushButton:hover{background:#dbe4f2;}")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(btn)
        self.info_btn.clicked.connect(self._toggle_info)
        self.list_btn.clicked.connect(self._toggle_sidebar)
        self.play_btn.clicked.connect(self.toggle_play)
        self.prev_btn.clicked.connect(self.prev)
        self.switch_btn.clicked.connect(self.next)
        self.next_btn.clicked.connect(self.next)
        panel_layout.addLayout(row)
        left_layout.addWidget(panel)
        root.addWidget(left, 1)

        # 右侧作品列表
        self.sidebar = QWidget(self)
        self.sidebar.setFixedWidth(300)
        self.sidebar.setStyleSheet("background:#ffffff;")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(10, 12, 10, 10)
        side.setSpacing(8)

        head = QHBoxLayout()
        self.list_title = QLabel("作品列表")
        self.list_title.setStyleSheet("color:#1f2937;font-size:14px;font-weight:700;")
        head.addWidget(self.list_title)
        head.addStretch(1)
        self.hide_btn = QPushButton("收起")
        self.hide_btn.setStyleSheet(
            "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #d8e0ec;"
            "border-radius:6px;padding:4px 10px;font-size:11px;}")
        self.hide_btn.clicked.connect(self._toggle_sidebar)
        head.addWidget(self.hide_btn)
        side.addLayout(head)

        self.author_filter = QComboBox(self.sidebar)
        self.author_filter.setStyleSheet(
            "QComboBox{background:#ffffff;color:#1f2937;border:1px solid #d8e0ec;"
            "border-radius:6px;padding:6px;}")
        self.author_filter.currentIndexChanged.connect(self._on_filter_changed)
        side.addWidget(self.author_filter)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#6b7280;font-size:11px;")
        side.addWidget(self.count_label)

        self.work_list = QListWidget(self.sidebar)
        self.work_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.work_list.setStyleSheet(
            "QListWidget{background:#ffffff;border:none;}"
            "QListWidget::item{color:#4b5563;padding:8px 8px;border-radius:6px;}"
            "QListWidget::item:selected{background:#dbe7ff;color:#1f2937;}")
        self.work_list.itemClicked.connect(self._on_work_clicked)
        side.addWidget(self.work_list, 1)
        self.more_btn = QPushButton("加载更多")
        self.more_btn.setVisible(False)
        self.more_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_btn.setStyleSheet(
            "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #d8e0ec;"
            "border-radius:6px;padding:6px;font-size:11px;}")
        self.more_btn.clicked.connect(self._load_more_works)
        side.addWidget(self.more_btn)
        root.addWidget(self.sidebar)

        self.image_label.installEventFilter(self)
        for view in (self.video_view, self.live_view):
            view.installEventFilter(self)
            view.viewport().installEventFilter(self)
        self.stack.installEventFilter(self)
        self.installEventFilter(self)

    def _toggle_sidebar(self):
        visible = not self.sidebar.isVisible()
        self.sidebar.setVisible(visible)
        self.hide_btn.setText("收起")
        self.list_btn.setText("隐藏列表" if visible else "列表")

    def _rebuild_list(self):
        counts = {}
        for item in self.feed:
            a = item.get("author") or "抖音创作者"
            counts[a] = counts.get(a, 0) + 1
        current = self.author_filter.currentText() if self.author_filter.count() else "全部作者"
        self.author_filter.blockSignals(True)
        self.author_filter.clear()
        self.author_filter.addItem(f"全部作者（{len(self.feed)}）")
        for a, n in counts.items():
            self.author_filter.addItem(f"{a}（{n}）")
        # 恢复之前选择（去掉末尾数量括号）
        base = current.split("（")[0]
        target = 0
        for i in range(self.author_filter.count()):
            if self.author_filter.itemText(i).split("（")[0] == base:
                target = i
                break
        self.author_filter.setCurrentIndex(target)
        self.author_filter.blockSignals(False)
        self.list_title.setText(f"作品列表（{len(self.feed)}）")
        self._fill_work_list()

    def _fill_work_list(self, reset=True):
        if reset:
            self.page = 1
        self.work_list.clear()
        self._visible_indices = []
        self._row_labels = {}
        self._row_base_text = {}
        selected_author = self.author_filter.currentText().split("（")[0]
        candidates = []
        for i, item in enumerate(self.feed):
            author = item.get("author") or "抖音创作者"
            if selected_author != "全部作者" and author != selected_author:
                continue
            candidates.append(i)
        shown = candidates[: self.page * 60]
        for i in shown:
            item = self.feed[i]
            author = item.get("author") or "抖音创作者"
            kind = "图集" if item.get("type") == "album" else "视频"
            title = (item.get("title") or "抖音作品").strip()
            if len(title) > 48:
                title = title[:48] + "…"
            ts = int(item.get("time") or 0)
            time_text = _time.strftime("%m-%d %H:%M", _time.localtime(ts)) if ts else "时间未知"
            base = f"【{kind}】{author} · {time_text}\n{title}"
            entry = QListWidgetItem("")
            entry.setData(Qt.ItemDataRole.UserRole, i)
            self.work_list.addItem(entry)
            self._visible_indices.append(i)
            if self.on_sync is not None:
                row = QWidget(self.work_list)
                row.setStyleSheet("background:transparent;")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(4, 4, 4, 4)
                rl.setSpacing(4)
                lab = QLabel(base, row)
                lab.setWordWrap(True)
                lab.setMaximumWidth(200)
                lab.setStyleSheet("color:#4b5563;font-size:11px;background:transparent;")
                rl.addWidget(lab, 1)
                play_btn = QPushButton("播放", row)
                sync_btn = QPushButton("同步", row)
                for b in (play_btn, sync_btn):
                    b.setFixedWidth(42)
                    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    b.setStyleSheet(
                        "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #d8e0ec;"
                        "border-radius:5px;padding:3px 0;font-size:10px;}")
                play_btn.clicked.connect(lambda _=False, idx=i: self._load(idx))
                sync_btn.clicked.connect(lambda _=False, idx=i: self._on_sync_clicked(idx))
                rl.addWidget(play_btn)
                rl.addWidget(sync_btn)
                row.setMinimumHeight(48)
                entry.setSizeHint(row.sizeHint())
                self.work_list.setItemWidget(entry, row)
                self._row_labels[i] = lab
                self._row_base_text[i] = base
            else:
                entry.setText(base)
        videos = sum(1 for i in self._visible_indices if self.feed[i].get("type") != "album")
        albums = len(self._visible_indices) - videos
        remaining = len(candidates) - len(shown)
        self.more_btn.setVisible(remaining > 0)
        if remaining > 0:
            self.more_btn.setText(f"加载更多（还有 {remaining} 个）")
        self.count_label.setText(
            f"当前显示 {len(shown)}/{len(candidates)} 个（视频 {videos} · 图集 {albums}）")
        self._sync_list_selection()

    def _load_more_works(self):
        self.page += 1
        self._fill_work_list(reset=False)

    def _on_sync_clicked(self, index):
        if self.on_sync is not None and 0 <= index < len(self.feed):
            self.on_sync(self.feed[index])

    def _on_filter_changed(self, _index):
        self._fill_work_list()
        # 如果当前作品不在筛选结果里，跳到该筛选下的第一个
        if self._visible_indices and self.index not in self._visible_indices:
            self._load(self._visible_indices[0])
        else:
            self._sync_list_selection()

    def _on_work_clicked(self, item):
        index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(self.feed):
            self._load(index)

    def _sync_list_selection(self):
        for row in range(self.work_list.count()):
            item = self.work_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == self.index:
                self.work_list.blockSignals(True)
                self.work_list.setCurrentRow(row)
                self.work_list.blockSignals(False)
                break
        # 在自定义行上标记当前播放项
        for idx, label in self._row_labels.items():
            base = self._row_base_text.get(idx, "")
            label.setText(("▶ " if idx == self.index else "   ") + base)

    # ---------- 播放 ----------
    def _load(self, index):
        if not self.feed:
            self.close()
            return
        if index < 0:
            index = 0
        if index >= len(self.feed):
            index = 0
        if index != self._loaded_index:
            # 每个作品默认显示文案；用户隐藏后只对当前作品生效
            self._loaded_index = index
            self.info_visible = True
            if hasattr(self, "info_btn"):
                self.info_btn.setText("隐藏文案")
        self.index = index
        self.image_index = 0
        self._layout_video_items()
        item = self._materialize(index)
        author = item.get("author") or "抖音创作者"
        # 当前作者头像
        avatar_path = item.get("avatar") or ""
        pix = self._circular_pixmap(avatar_path, 44) if avatar_path else None
        if pix is not None:
            self.avatar_label.setPixmap(pix)
            self.avatar_label.setText("")
        else:
            self.avatar_label.clear()
            self.avatar_label.setText((author or "净")[0])
        self.author_label.setText("@" + author)
        self.title_label.setText(item.get("title") or "抖音作品")
        import time as _time
        ts = int(item.get("time") or 0)
        time_text = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts)) if ts else "时间未知"
        self.meta_label.setText(f"发布时间：{time_text}")
        self.player.stop()
        self.music_player.stop()
        self.live_player.stop()
        self.image_timer.stop()

        if item.get("type") == "album":
            self.stack.setCurrentWidget(self.image_label)
            self.seek.setRange(0, max(1, len(item.get("images") or [])) - 1)
            self.seek.setValue(0)
            self._show_image()
            self._play_album_music(item)
            self.play_btn.setText(
                "暂停" if (len(item.get("images") or []) > 1
                          or (item.get("music") and os.path.exists(item.get("music"))))
                else "播放")
        else:
            self.stack.setCurrentWidget(self.video_widget)
            self.seek.setRange(0, 1000)
            self.seek.setValue(0)
            path = item.get("path", "")
            if path and os.path.exists(path):
                self.player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
                self.player.play()
                self.play_btn.setText("暂停")
            else:
                self._skip_broken()
        self._layout_info_overlay()
        self.info_overlay.setVisible(self.info_visible)
        self._raise_overlay()
        self._sync_list_selection()

    def _play_cache_root(self):
        """解包缓存目录：整个播放器窗口共用一个，关闭时删除。"""
        if getattr(self, "_play_cache", None) is None:
            try:
                self._play_cache = tempfile.mkdtemp(prefix="jy_play_", dir=config.TEMP_DIR)
            except OSError:
                self._play_cache = tempfile.mkdtemp(prefix="jy_play_")
        return self._play_cache

    def _materialize(self, index):
        """取这一条作品的播放信息；如果是 tar 包，先解到缓存目录。

        2026-11-08 起磁盘上每个作品只有一个 `<作品id>.tar`。信息流里带
        `archive` 键的条目在第一次切到时解包，解完把 feed[index] 换成真实路径，
        来回切同一个作品不会重复解压。
        """
        item = self.feed[index]
        tar_path = item.get("archive")
        if not tar_path:
            return item
        resolved = archive.extract(tar_path, self._play_cache_root())
        new_item = dict(item)
        new_item.pop("archive", None)
        if item.get("type") == "album":
            new_item["images"] = resolved["images"]
            new_item["videos"] = list(resolved["lives"])
            new_item["music"] = resolved["music"]
        else:
            new_item["path"] = resolved["video"] or item.get("path", "")
        self.feed[index] = new_item
        return new_item

    def _layout_video_items(self):
        """把 QGraphicsVideoItem 铺满 QGraphicsView 视口；视频由 Qt 绘制，不是原生窗口。"""
        if not (hasattr(self, "video_view") and hasattr(self, "live_view")):
            return
        for view, scene, item in ((self.video_view, self.video_scene, self.video_item),
                                  (self.live_view, self.live_scene, self.live_item)):
            size = view.viewport().size()
            width = max(1, size.width())
            height = max(1, size.height())
            scene.setSceneRect(0, 0, width, height)
            item.setSize(QSizeF(width, height))
            item.setPos(0, 0)

    def _layout_info_overlay(self):
        if getattr(self, "info_overlay", None) is None:
            return
        rect = self.stack.geometry()
        width = max(240, rect.width() - 48)
        height = min(140, max(96, int(rect.height() * 0.24)))
        x = rect.x() + 24
        y = rect.y() + max(8, rect.height() - height - 18)
        self.info_overlay.setGeometry(x, y, width, height)

    def _raise_overlay(self):
        if getattr(self, "info_overlay", None) is not None and self.info_visible:
            self._layout_info_overlay()
            self.info_overlay.raise_()

    def _toggle_info(self, visible=None):
        """显示/隐藏画面上的文案浮层。"""
        self.info_visible = (not self.info_visible) if visible is None else bool(visible)
        self.info_overlay.setVisible(self.info_visible)
        self.info_btn.setText("隐藏文案" if self.info_visible else "显示文案")
        if self.info_visible:
            self._raise_overlay()

    def _show_image(self):
        item = self.feed[self.index]
        images = item.get("images") or []
        if not images:
            self.image_label.setText("图集为空")
            return
        self._live_token += 1
        self.image_index = max(0, min(self.image_index, len(images) - 1))
        videos = item.get("videos") or []
        live = videos[self.image_index] if self.image_index < len(videos) else ""
        if live and os.path.exists(live):
            # 实况图：多图时只播一遍，播放结束后再切下一张；单图时循环播放
            self.image_timer.stop()
            self._live_current = live
            self.live_player.setLoops(
                QMediaPlayer.Loops.Once if len(images) > 1 else QMediaPlayer.Loops.Infinite)
            self.stack.setCurrentWidget(self.live_widget)
            self._layout_video_items()
            self.live_player.setSource(QUrl.fromLocalFile(os.path.abspath(live)))
            self.live_player.play()
            self.seek.setValue(self.image_index)
            self.time_label.setText(f"实况 {self.image_index + 1}/{len(images)}")
            self._raise_overlay()
            return
        self._live_current = ""
        self.live_player.stop()
        self.stack.setCurrentWidget(self.image_label)
        self._render_image()
        self._raise_overlay()
        self.seek.setValue(self.image_index)
        self.time_label.setText(f"图集 {self.image_index + 1}/{len(images)}")
        if len(images) > 1:
            self.image_timer.start(SLIDESHOW_INTERVAL_MS)

    def _on_live_status(self, status):
        if status != QMediaPlayer.MediaStatus.EndOfMedia or not self._live_current:
            return
        item = self.feed[self.index] if self.feed else {}
        images = item.get("images") or []
        if item.get("type") != "album" or len(images) <= 1:
            return
        token = self._live_token
        QTimer.singleShot(0, lambda: self._advance_after_live(token))

    def _on_live_error(self, _error, _message):
        if not self._live_current:
            return
        item = self.feed[self.index] if self.feed else {}
        images = item.get("images") or []
        if item.get("type") != "album" or len(images) <= 1:
            return
        token = self._live_token
        QTimer.singleShot(1200, lambda: self._advance_after_live(token))

    def _advance_after_live(self, token):
        if token != self._live_token or not self._live_current:
            return
        item = self.feed[self.index] if self.feed else {}
        images = item.get("images") or []
        if item.get("type") != "album" or len(images) <= 1:
            return
        self._auto_next_image()

    def _image_pixmap(self, path):
        """缓存已解码的原图，窗口缩放时只做缩放，不重复读盘解码。"""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return QPixmap()
        cached = self._pix_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        pix = QPixmap(path)
        if not pix.isNull():
            if len(self._pix_cache) >= 8:
                self._pix_cache.clear()
            self._pix_cache[path] = (mtime, pix)
        return pix

    def _render_image(self):
        item = self.feed[self.index]
        images = item.get("images") or []
        if not images:
            self.image_label.setText("图集为空")
            return
        self.image_index = max(0, min(self.image_index, len(images) - 1))
        pix = self._image_pixmap(images[self.image_index])
        if pix.isNull():
            self.image_label.setText("图片无法加载")
            return
        avail = self.stack.size()
        scaled = pix.scaled(
            max(100, avail.width() - 12), max(100, avail.height() - 12),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.image_label.setText("")
        self.image_label.setPixmap(scaled)

    def _auto_next_image(self):
        images = self.feed[self.index].get("images") or []
        if len(images) <= 1:
            return
        self.image_index = (self.image_index + 1) % len(images)
        self._show_image()

    def _play_album_music(self, item):
        music = item.get("music")
        if music and os.path.exists(music):
            self.music_player.setSource(QUrl.fromLocalFile(os.path.abspath(music)))
            self.music_player.play()

    def _skip_broken(self):
        for i in range(self.index + 1, len(self.feed)):
            item = self.feed[i]
            if item.get("type") == "album" and item.get("images"):
                self._load(i)
                return
            if item.get("path") and os.path.exists(item.get("path")):
                self._load(i)
                return
        self.title_label.setText("文件不存在或无法播放")

    def _ordered_indices(self):
        """当前作者筛选下的全部作品下标（不受分页影响）。"""
        selected = self.author_filter.currentText().split("（")[0] \
            if self.author_filter.count() else "全部作者"
        if selected == "全部作者":
            return list(range(len(self.feed)))
        return [i for i, item in enumerate(self.feed)
                if (item.get("author") or "抖音创作者") == selected]

    def _step(self, delta):
        order = self._ordered_indices()
        if not order:
            return
        if self.index not in order:
            self._load(order[0])
            return
        if len(order) <= 1:
            return
        position = order.index(self.index)
        self._load(order[(position + delta) % len(order)])

    def next(self):
        self._step(1)

    def toggle_play(self):
        """暂停 / 继续播放：视频控制播放器，图集控制轮播与音乐。"""
        item = self.feed[self.index]
        if item.get("type") == "album":
            images = item.get("images") or []
            videos = item.get("videos") or []
            has_live = (self.image_index < len(videos)
                        and videos[self.image_index] and os.path.exists(videos[self.image_index]))
            playing = (self.image_timer.isActive()
                       or self.music_player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState
                       or self.live_player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState)
            if playing:
                self.image_timer.stop()
                self.music_player.pause()
                self.live_player.pause()
                self.play_btn.setText("播放")
            else:
                if has_live:
                    self.live_player.play()
                elif len(images) > 1:
                    self.image_timer.start(SLIDESHOW_INTERVAL_MS)
                self.music_player.play()
                self.play_btn.setText("暂停")
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_playback_state(self, state):
        if self.feed and self.feed[self.index].get("type") != "album":
            self.play_btn.setText(
                "暂停" if state == QMediaPlayer.PlaybackState.PlayingState else "播放")
            if state == QMediaPlayer.PlaybackState.PlayingState:
                # 视频首帧到达后可能重新提升视频窗，确保文案浮层仍在最上面
                self._raise_overlay()

    def prev(self):
        self._step(-1)

    def next_image(self):
        images = self.feed[self.index].get("images") or []
        if not images:
            return
        self.image_index = (self.image_index + 1) % len(images)
        self._show_image()

    def prev_image(self):
        images = self.feed[self.index].get("images") or []
        if not images:
            return
        self.image_index = (self.image_index - 1) % len(images)
        self._show_image()

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.autoplay_cb.isChecked():
                self.next()

    def _on_error(self, error, message):
        self._skip_broken()

    def _on_position(self, pos_ms):
        if self.feed[self.index].get("type") == "album":
            return
        duration = self.player.duration()
        if duration > 0 and not self.tracking:
            self.seek.setValue(int(pos_ms * 1000 / duration))
        self.time_label.setText(f"{self._fmt(pos_ms)} / {self._fmt(duration)}")

    def _on_seek_released(self):
        self.tracking = False
        item = self.feed[self.index]
        if item.get("type") == "album":
            images = item.get("images") or []
            if images:
                self.image_index = max(0, min(self.seek.value(), len(images) - 1))
                self._show_image()
            return
        duration = self.player.duration()
        if duration > 0:
            self.player.setPosition(int(self.seek.value() * duration / 1000))

    def _on_seek_moved(self, value):
        item = self.feed[self.index]
        if item.get("type") == "album":
            images = item.get("images") or []
            if images:
                self.time_label.setText(f"图集 {min(value, len(images) - 1) + 1}/{len(images)}")
            return
        duration = self.player.duration()
        if duration > 0:
            self.time_label.setText(f"{self._fmt(value * duration / 1000)} / {self._fmt(duration)}")

    # ---------- 手势 ----------
    def eventFilter(self, obj, event):
        from PySide6.QtGui import QMouseEvent, QWheelEvent
        from PySide6.QtCore import QEvent
        if obj is self.info_overlay:
            if event.type() == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
                self._switch_by_wheel(event.angleDelta().y())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._toggle_info(False)
                return True
        if obj in (self.video_view, self.video_view.viewport(),
                   self.live_view, self.live_view.viewport(),
                   self.image_label, self.stack):
            if event.type() == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
                self._switch_by_wheel(event.angleDelta().y())
                return True
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                self._drag_start = event.position()
                self._drag_active = False
            elif event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                if self._drag_start is not None:
                    dx = abs(event.position().x() - self._drag_start.x())
                    dy = abs(event.position().y() - self._drag_start.y())
                    if dx > 12 or dy > 12:
                        self._drag_active = True
            elif event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
                if self._drag_start is not None:
                    dx = event.position().x() - self._drag_start.x()
                    dy = event.position().y() - self._drag_start.y()
                    if self._drag_active and max(abs(dx), abs(dy)) > 70:
                        if abs(dx) > abs(dy):
                            if self.feed[self.index].get("type") == "album":
                                self.next_image() if dx < 0 else self.prev_image()
                            else:
                                self.next() if dx < 0 else self.prev()
                        else:
                            self.next() if dy < 0 else self.prev()
                    elif not self._drag_active:
                        if not self.info_visible:
                            # 文案被隐藏时，单击画面先恢复文案
                            self._toggle_info(True)
                        else:
                            self.toggle_play()
                    self._drag_start = None
                    self._drag_active = False
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                self._toggle_fullscreen()
        return super().eventFilter(obj, event)

    def _switch_by_wheel(self, delta):
        if delta == 0:
            return
        # 滚轮始终切换作品（图集切图用左右方向键 / 横向拖动）
        self.next() if delta < 0 else self.prev()

    def wheelEvent(self, event):
        self._switch_by_wheel(event.angleDelta().y())
        event.accept()

    def keyPressEvent(self, event):
        is_album = self.feed and self.feed[self.index].get("type") == "album"
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.next()
        elif event.key() in (Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.prev()
        elif event.key() == Qt.Key.Key_Left:
            self.prev_image() if is_album else self.prev()
        elif event.key() == Qt.Key.Key_Right:
            self.next_image() if is_album else self.next()
        elif event.key() == Qt.Key.Key_Space:
            if is_album:
                self.toggle_play()
            elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
        elif event.key() == Qt.Key.Key_F11:
            self._toggle_fullscreen()
        elif event.key() == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showMaximized()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    def _toggle_fullscreen(self):
        self.showMaximized() if self.isFullScreen() else self.showFullScreen()

    def showEvent(self, event):
        super().showEvent(event)
        theme.apply_light_titlebar(self)
        if not getattr(self, "_shown_once", False):
            self._shown_once = True
            self.showMaximized()
        self.setFocus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_video_items()
        self._layout_info_overlay()
        if self.feed and self.feed[self.index].get("type") == "album":
            # 拖动窗口时合并连续 resize，避免每一帧都缩放整张原图
            self._resize_timer.start(80)

    @staticmethod
    def _fmt(ms):
        total = max(0, int(ms // 1000))
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _circular_pixmap(path, size):
        """把头像裁成圆形，避免方形图片破坏圆形边框。"""
        source = QPixmap(path)
        if source.isNull():
            return None
        source = source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(0, 0, size, size)
        painter.setClipPath(clip)
        x = (size - source.width()) // 2
        y = (size - source.height()) // 2
        painter.drawPixmap(x, y, source)
        painter.end()
        return canvas

    def closeEvent(self, event):
        # 先把三个播放器的媒体源清空，再删解包缓存。只 stop() 不够：
        # Windows 下播放器只要还引用着本地文件，目录里的 mp4 就删不掉。
        for media in (self.player, self.music_player, self.live_player):
            try:
                media.stop()
                media.setSource(QUrl())
            except RuntimeError:  # 底层对象已被销毁
                continue
        self.image_timer.stop()
        # 关窗后解包缓存就没用了：不留垃圾在临时目录里
        cache = getattr(self, "_play_cache", None)
        if cache:
            _remove_tree(cache)
            self._play_cache = None
        super().closeEvent(event)
