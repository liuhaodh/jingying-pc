# -*- coding: utf-8 -*-
"""净影 · 电脑版主窗口（PySide6）。功能对齐 Android v1.3.0：
单条解析下载 / 主页批量 / 通讯录自动同步全部作品（含音乐）。

本文件只保留主窗口骨架与单条解析、批量、历史列表；三块较大的业务逻辑放在
独立模块里，以 mixin 形式组合进 MainWindow：

- `webdav_panel.WebdavMixin`     WebDAV 上传、进度、上传后清理
- `contacts_sync.ContactsSyncMixin` 通讯录同步状态机、失败重试
- `workers.Worker`               后台任务线程（各 mixin 共用）
"""
import glob
import os
import sys
import threading
import time

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QPixmap, QFont, QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFrame, QScrollArea, QMessageBox, QTextEdit, QProgressBar,
    QListWidgetItem, QFileDialog, QComboBox, QInputDialog, QSizePolicy,
    QCheckBox,
)

import config
import theme
import windows
import archive
import author_assets
import download_log
import downloader
import parallel
import links
import original_stream
import parser
import store as store_mod
import transfer
from contacts_sync import ContactsSyncMixin
from models import MediaInfo
from webdav_panel import WebdavMixin
from workers import Worker

# 启动日志的轮转上限：单文件 2 MB，保留 3 份备份（合计最多约 8 MB）
APP_LOG_MAX_BYTES = 2 * 1024 * 1024
APP_LOG_BACKUPS = 3


def _apply_tooltip_theme(app):
    """Windows 深色主题下 QToolTip 默认是黑底，强制成白底黑字。"""
    from PySide6.QtGui import QColor, QPalette
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#111111"))
    app.setPalette(palette)
    app.setStyleSheet(theme.TOOLTIP_QSS)


def _card() -> QFrame:
    return theme.card_frame()


LINK_HINT = "粘贴抖音作品链接 / 创作者主页链接\n例如：https://www.douyin.com/video/… 或 https://www.douyin.com/user/MS4wLjABAAAA…"


class MainWindow(WebdavMixin, ContactsSyncMixin, QWidget):
    login_signal = Signal(str)
    THREAD_CHOICES = (2, 3, 4, 6, 8)
    # 后台 maintenance 读到历史后发给界面的标记：收到即渲染列表，不等后续扫盘。
    HISTORY_READY_STAGE = "__history_ready__"

    def __init__(self):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            _apply_tooltip_theme(app)
        self.setWindowTitle(config.window_title())
        # 三块列表搬到独立窗口后，主界面只剩解析和 WebDAV，高度可以矮一些
        self.resize(900, 820)
        self.setMinimumWidth(860)
        from PySide6.QtGui import QColor, QPalette
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.store = store_mod.Store()
        self.current: MediaInfo | None = None
        self.batch_info: parser.ProfileInfo | None = None
        self.syncing = False
        self.worker: Worker | None = None
        self.contact_page = 1
        self.history_page = 1
        self._login_result = None
        self._login_running = False
        self._logged_in = None
        self._account_count = max(1, min(5, len(config.SESSION_DIRS)))
        self._account_logged = [None] * self._account_count
        self._check_worker = None
        self._webdav = self._load_webdav_config()
        self._upload_bridge = None
        self._upload_counts = {"ok": 0, "skipped": 0, "failed": 0}
        self._webdav_state_lock = threading.Lock()
        # 远端画质探测：连接池与缓存都要跨下载线程串行访问
        self._probe_lock = threading.Lock()
        self._upload_root = ""
        self._workers = []
        self._avatar_cache = {}
        self._maintenance_done = False
        self._history_rendered = False
        self._settings = self._load_settings()
        self._dl_threads = self._setting_int("download_threads", 3)
        self._up_threads = self._setting_int("upload_threads", 3)
        self._dl_meter = transfer.TransferMeter()
        self._up_meter = transfer.TransferMeter()
        self._dl_progress = {}
        self._up_total_bytes = 0
        self._up_done_bytes = 0
        self._upload_active = False
        self._cleanup_active = False
        # 已上传单元索引（作品目录 / tar）：判断「本地删了但云端有」的作品
        self._uploaded_units_cache = None
        self._auto_uploader = None
        self._single_dl = False
        self._single_percent = 0
        self._single_title = ""
        self._sync_failed_authors = []
        self._sync_failed_objs = []
        self._load_failed_authors()
        self._sync_retry = set()
        self._sync_current = None
        self._sync_after_check = False
        self._sync_total_new = 0
        self._sync_total_fail = 0
        self._contacts_dirty = False
        self._sync_account_index = 0
        self._sync_account_index_current = 0
        self._sync_account_label = ""
        self._last_sync_first = ""
        self._load_login_state()
        # 下载线程不能读 QWidget：开关状态统一由主线程写进这两个 bool 快照。
        # 默认值要在这里就位，早于 UI 构建（同步可能比窗口更早跑起来）。
        self._cloud_upgrade_flag = bool(
            self._settings.get("webdav_upgrade_low_quality", False))
        self._auto_upload_flag = bool(
            self._settings.get("webdav_auto_upload", True))
        self._upgrade_delete_paths = set()
        # 「原画质」开关：回捞会额外发 play + HEAD 两个请求并加 1.2~3s 节流，
        # 且原片体积可能是转码档的 20 倍，默认关闭。线程侧只认模块级 bool。
        self._original_flag = bool(self._settings.get("original_quality", False))
        original_stream.set_enabled(self._original_flag)
        downloader.set_original_resolver(
            original_stream.resolve_for_download if self._original_flag else None)
        self.dm = parallel.DownloadManager(
            self._dl_threads, pack_tar=bool(self._settings.get("pack_tar", False)),
            precheck=self._cloud_quality_precheck)
        self.dm.item_done.connect(self._dl_item_done)
        self.dm.item_error.connect(self._dl_item_error)
        self.dm.item_skipped.connect(self._dl_item_skipped)
        self.dm.item_started.connect(self._dl_item_started)
        self.dm.item_progress.connect(self._dl_item_progress)
        self._dl_mode = None
        self._dl_jobs = []
        self._dl_authors = []
        # 每轮下载换一个轮次号：传输列表行的 key 用它，避免 id(list) 地址复用
        self._dl_run = 0
        self._dl_done = 0
        self._dl_total = 0
        self._dl_success = 0
        self._dl_fail = 0
        self._dl_skip = 0
        self._dl_author = None
        self._sync_queue = []
        self._sync_index = 0
        self._sync_profiles_done = False
        self.login_signal.connect(self._login_done)
        # 历史补全、文件时间修正都在 QTimer 触发的后台 maintenance 里做：
        # 实测 1.7 万条历史时，同步扫描要 4.6 秒，窗口会一直白屏。
        self._build_ui()
        self.setStyleSheet(theme.APP_QSS)
        self._render_contacts()
        # 历史记录上万条时 JSON 解析要 1~2 秒：先占位，等后台 maintenance 读完再渲染，
        # 窗口可以立刻出现，不再白屏等待。
        self.history_title.setText("最近保存（载入中…）")
        loading = QListWidgetItem("正在载入历史记录…")
        loading.setForeground(Qt.GlobalColor.gray)
        self.history_list.addItem(loading)
        self.transfer_timer = QTimer(self)
        self.transfer_timer.timeout.connect(self._update_transfer_stats)
        self.transfer_timer.start(500)
        # 历史上可能有很多作品：扫描本地目录、补齐文件时间放到后台，避免启动卡顿。
        # 窗口先显示出来，maintenance 在后台线程跑完再刷新列表。
        QTimer.singleShot(0, self._start_maintenance)
        QTimer.singleShot(600, self._check_login)
        # UI 卡顿看门狗：主线程超过 3 秒没有心跳时，把主线程 Python 栈写进日志，
        # 便于定位“未响应几秒后又恢复”的具体原因。
        self._ui_heartbeat = time.monotonic()
        self._ui_watchdog_last_log = 0.0
        self._ui_watchdog_stop = False
        self._ui_heartbeat_timer = QTimer(self)
        self._ui_heartbeat_timer.timeout.connect(self._ui_heartbeat_tick)
        self._ui_heartbeat_timer.start(500)
        threading.Thread(target=self._ui_watchdog_loop,
                         name="jy-ui-watchdog", daemon=True).start()

    def _ui_heartbeat_tick(self):
        self._ui_heartbeat = time.monotonic()

    def _ui_watchdog_loop(self):
        import logging
        import sys
        import traceback
        while not getattr(self, "_ui_watchdog_stop", True):
            time.sleep(1.0)
            try:
                lag = time.monotonic() - self._ui_heartbeat
                if lag < 3.0:
                    continue
                now = time.monotonic()
                if now - self._ui_watchdog_last_log < 10.0:
                    continue
                self._ui_watchdog_last_log = now
                frame = sys._current_frames().get(threading.main_thread().ident)
                stack = "".join(traceback.format_stack(frame)[-12:]) if frame else ""
                logging.warning("UI stall %.1fs\n%s", lag, stack)
            except Exception:  # noqa: BLE001
                pass

    # ---------- UI ----------
    def showEvent(self, event):
        super().showEvent(event)
        theme.apply_light_titlebar(self)

    def _rebuild_history_from_disk(self):
        """把已下载但不在历史里的作品补进历史，保证列表显示全部作品。返回新增数量。

        2026-11-08 起每个作品是一个 `<作品id>.tar`；旧版的作品文件夹仍然认，
        已经下过的老作品不会被当成新作品再补一遍。
        """
        base = os.path.join(config.DOWNLOAD_BASE)
        if not os.path.isdir(base):
            return 0
        items = self.store.load_history()
        known = set()
        known_ids = set()
        for item in items:
            if item.aweme_id:
                known_ids.add(item.aweme_id)
            for path in [item.video_path] + item.album_paths[:1]:
                if path:
                    known.add(os.path.normcase(path))
                    # 旧版散文件：把所属作品文件夹也算作已知，避免没有作品 ID 的
                    # 老作品每次启动都被当成新作品再补一遍。
                    known.add(os.path.normcase(os.path.dirname(path)))
        new_items = []
        for author_dir in os.listdir(base):
            author_path = os.path.join(base, author_dir)
            if not os.path.isdir(author_path):
                continue
            try:
                names = os.listdir(author_path)
            except OSError:
                continue
            for name in names:
                full = os.path.join(author_path, name)
                if os.path.normcase(full) in known:
                    continue
                if archive.is_archive(name) and os.path.isfile(full):
                    item = self._history_from_archive(full, author_dir)
                elif os.path.isdir(full):
                    item = self._history_from_folder(full, author_dir, name)
                else:
                    item = None
                if item is None:
                    continue
                if item.aweme_id and item.aweme_id in known_ids:
                    continue
                item.saved_at = int(time.time() * 1000)
                new_items.append(item)
                known.add(os.path.normcase(full))
                if item.aweme_id:
                    known_ids.add(item.aweme_id)
        return self.store.merge_history(new_items)

    def _read_caption_meta(self, text):
        """从 文案.txt 里还原作品 ID / 作者 / 抖音号 / 发布时间 / 完整文案。"""
        import re
        from datetime import datetime
        meta = {}
        m = re.search(r"作品链接：https://www\.douyin\.com/video/(\d+)", text or "")
        if m:
            meta["aweme_id"] = m.group(1)
        m = re.search(r"作者：(.+)", text or "")
        if m:
            meta["author"] = m.group(1).strip()
        m = re.search(r"抖音号：(.+)", text or "")
        if m and "未知" not in m.group(1):
            meta["unique_id"] = m.group(1).strip()
        m = re.search(r"发布时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text or "")
        if m:
            try:
                meta["create_time"] = int(
                    datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                pass
        m = re.search(r"文案：\n?(.*)", text or "", re.S)
        if m and m.group(1).strip():
            meta["title"] = m.group(1).strip()
        return meta

    @staticmethod
    def _folder_name_meta(work_folder, author_dir):
        """从“作者_文案_时间”文件夹名里取作者、文案、发布时间（兜底用）。"""
        import re
        from datetime import datetime
        meta = {}
        match = re.search(r"_(\d{8}_\d{6})$", work_folder or "")
        if match:
            try:
                meta["create_time"] = int(
                    datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").timestamp())
            except ValueError:
                pass
        stem = work_folder[:match.start()] if match else (work_folder or "")
        author, _, title = stem.partition("_")
        meta["author"] = author or author_dir
        meta["title"] = title or stem
        return meta

    def _history_from_archive(self, tar_path, author_dir):
        """从一个作品 tar 还原历史记录；包读不出来返回 None。"""
        members = archive.member_names(tar_path)
        if not members:
            return None
        root = members[0].split("/")[0]
        meta = self._folder_name_meta(root, author_dir)
        meta.update(self._read_caption_meta(archive.read_member(tar_path, "文案.txt")))
        item = store_mod.HistoryItem()
        stem = os.path.splitext(os.path.basename(tar_path))[0]
        item.aweme_id = meta.get("aweme_id") or (stem if stem.isdigit() else "")
        item.title = meta.get("title") or stem
        item.author = meta.get("author") or author_dir
        item.unique_id = meta.get("unique_id", "")
        item.create_time = int(meta.get("create_time", 0) or 0)
        is_album = any(os.path.basename(m).startswith(archive.IMAGE_PREFIX)
                       for m in members)
        item.is_album = is_album
        if is_album:
            item.album_paths = [tar_path]
        else:
            item.video_path = tar_path
        item.music_path = ""
        item.album_videos = []
        return item

    def _history_from_folder(self, folder, author_dir, work_dir):
        """旧版作品文件夹（视频/图片散在目录里）还原历史记录。"""
        videos = glob.glob(os.path.join(folder, "视频.*"))
        images = sorted(glob.glob(os.path.join(folder, "图片_*.*")))
        if not videos and not images:
            return None
        meta = self._folder_name_meta(work_dir, author_dir)
        caption = os.path.join(folder, "文案.txt")
        if os.path.exists(caption):
            try:
                with open(caption, encoding="utf-8") as f:
                    meta.update(self._read_caption_meta(f.read()))
            except OSError:
                pass
        item = store_mod.HistoryItem()
        item.aweme_id = meta.get("aweme_id", "")
        item.title = meta.get("title") or work_dir
        item.author = meta.get("author") or author_dir
        item.unique_id = meta.get("unique_id", "")
        item.create_time = int(meta.get("create_time", 0) or 0)
        if videos:
            item.video_path = videos[0]
        else:
            item.album_paths = images
            item.is_album = True
            item.album_videos = []
            for idx in range(len(images)):
                found = glob.glob(os.path.join(folder, f"动图_{idx + 1:02d}.*"))
                item.album_videos.append(found[0] if found else "")
        music_folder = os.path.join(config.MUSIC_BASE, author_dir, work_dir)
        if os.path.isdir(music_folder):
            music = glob.glob(os.path.join(music_folder, "音乐.*"))
            if music:
                item.music_path = music[0]
        return item

    def _normalize_marker_file(self):
        return os.path.join(config.DATA_DIR, "normalize_files_state.json")

    def _normalize_signature(self, items):
        """历史列表的“指纹”：条数 + 最新保存时间。变了才需要重新扫盘。"""
        newest = 0
        for item in items:
            if item.saved_at > newest:
                newest = item.saved_at
        return {"count": len(items), "newest": int(newest)}

    def _normalize_file_times(self):
        """把已下载文件的创建/修改时间改成作品发布时间（旧数据从文件夹名推断）。

        只处理时间还不正确的文件，避免每次启动都对成百上千个文件做系统调用。
        返回实际修改的文件数量。

        性能：
        - 历史没变化（条数 + 最新 saved_at 相同）时直接跳过，不再扫盘；
        - 每条记录先 stat 一次所在目录，目录不存在就跳过整条，避免对已删除的
          作品逐个文件做系统调用（实测 1.7 万条历史里大部分是这种情况）；
        - 用一次 `os.stat` 同时拿到“是否存在”和 mtime，比 exists + getmtime 少一半调用。
        """
        import json
        import re
        from datetime import datetime
        items = self.store.load_history()
        signature = self._normalize_signature(items)
        marker = self._normalize_marker_file()
        try:
            with open(marker, encoding="utf-8") as f:
                if json.load(f) == signature:
                    return 0
        except (OSError, ValueError):
            pass
        changed_items = []
        touched = 0
        for item in items:
            ts = item.create_time
            if not ts:
                for p in ([item.video_path] + item.album_paths):
                    if not p:
                        continue
                    m = re.search(r"_(\d{8}_\d{6})(?:[\\/]|$)", p)
                    if m:
                        try:
                            ts = int(datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp())
                            item.create_time = ts
                            changed_items.append(item)
                        except ValueError:
                            pass
                    break
            if ts <= 0:
                continue
            paths = [p for p in ([item.video_path, item.music_path] + item.album_paths) if p]
            if not paths:
                continue
            folder = ""
            for p in ([item.video_path] + item.album_paths[:1]):
                if p:
                    folder = os.path.dirname(p)
                    break
            # 目录不在时只补音乐（音乐在另一个根目录），其余文件一定也不在。
            if folder:
                try:
                    folder_exists = os.path.isdir(folder)
                except OSError:
                    folder_exists = False
                if not folder_exists:
                    folder = ""
                    paths = [item.music_path] if item.music_path else []
            for p in paths:
                try:
                    mtime = os.stat(p).st_mtime
                except OSError:
                    continue
                if abs(mtime - ts) > 2:
                    downloader._set_file_times(p, ts)
                    touched += 1
            # 旧作品补写完整文案 txt（tar 包里的文案不在这里补）
            primary = item.video_path or (item.album_paths[0] if item.album_paths else "")
            if (folder and not archive.is_archive(primary)
                    and not os.path.exists(os.path.join(folder, "文案.txt"))):
                try:
                    downloader.write_caption_file(
                        folder, item.author, item.unique_id, item.title,
                        item.aweme_id, item.share_url, ts)
                except OSError:
                    pass
        if changed_items:
            # 只更新补过时间的这些行，不再全量重写历史
            self.store.update_history_items(changed_items)
        try:
            with open(marker, "w", encoding="utf-8") as f:
                json.dump(signature, f)
        except OSError:
            pass
        return touched

    def _start_maintenance(self):
        """后台整理本地历史与文件时间，不阻塞界面。"""
        if self._maintenance_done:
            return
        self._maintenance_done = True
        worker = Worker(self._maintenance_task)
        worker.stage.connect(self._maintenance_stage,
                             Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._maintenance_finished)
        worker.failed.connect(self._maintenance_failed,
                              Qt.ConnectionType.QueuedConnection)
        self._maintenance_worker = self._track_worker(worker)
        worker.start()

    def _maintenance_stage(self, text):
        """后台线程的进度通知；HISTORY_READY_STAGE 表示历史已可渲染。"""
        if text == self.HISTORY_READY_STAGE:
            self._render_history_once()
            return
        self.status.setText(text)

    def _render_history_once(self):
        """历史列表只渲染一次（整理过程中可能多次收到就绪通知）。"""
        if self._history_rendered:
            return
        self._history_rendered = True
        # 用户已经点过「加载更多」时保持当前页，不要跳回第一页。
        self._render_history(reset=self.history_page <= 1)

    def _maintenance_task(self, stage=None):
        # 先把历史读进内存（上万条时要 1~2 秒），后面的步骤都基于这份缓存。
        self.store.load_history()
        # 读完就通知界面渲染列表，不用等后面的扫盘/修时间跑完。
        if stage:
            stage(self.HISTORY_READY_STAGE)
        # 上传记录可能有 10 MB，也放到后台解析，启动时状态栏先不显示数量。
        if (getattr(self, "_webdav", {}) or {}).get("url"):
            self._load_webdav_state()
        added = self._rebuild_history_from_disk()
        touched = self._normalize_file_times()
        self._repair_caption_metadata()
        # 上次异常退出留下的临时浏览器配置目录（每个几十 MB）
        try:
            parser.cleanup_temp_profiles()
        except Exception:  # noqa: BLE001
            pass
        return added, touched

    def _repair_caption_metadata(self):
        """补齐旧文案里的“抖音号：未知”和空文案。

        旧版下载用的是主页作品对象里的 unique_id，而主页列表通常不带抖音号；
        标题为空时也直接写入了空字符串。这里用通讯录作者资料和历史标题修复一次。
        """
        import re
        marker = os.path.join(config.DATA_DIR, "caption_metadata_fixed_v1")
        if os.path.exists(marker):
            return 0
        authors = self.store.load_authors()
        by_name = {a.nickname: a for a in authors if a.nickname}
        fixed = 0
        for item in self.store.load_history():
            paths = [p for p in ([item.video_path] + list(item.album_paths))
                     if p and not archive.is_archive(p)]
            caption = ""
            for path in paths:
                if not path:
                    continue
                candidate = os.path.join(os.path.dirname(path), "文案.txt")
                if os.path.isfile(candidate):
                    caption = candidate
                    break
            if not caption:
                continue
            author = by_name.get(item.author)
            uid = item.unique_id or (author.unique_id if author else "")
            work_dir = os.path.dirname(caption)
            if not uid:
                match = re.search(r"_([A-Za-z0-9.-]{4,})$", os.path.basename(
                    os.path.dirname(work_dir)))
                if match:
                    uid = match.group(1)
            title = (item.title or "").strip()
            if not title or title.startswith("抖音作品 "):
                folder = os.path.basename(work_dir)
                match = re.match(r"^(.*)_(\d{8}_\d{6})$", folder)
                stem = match.group(1) if match else folder
                prefix = f"{item.author}_" if item.author else ""
                if prefix and stem.startswith(prefix):
                    stem = stem[len(prefix):]
                elif "_" in stem:
                    stem = stem.split("_", 1)[1]
                title = stem.strip()
            try:
                with open(caption, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            new_text = text
            if uid and "抖音号：未知" in new_text:
                new_text = new_text.replace("抖音号：未知", f"抖音号：{uid}", 1)
            if title:
                prefix, sep, body = new_text.partition("文案：")
                if sep and not body.strip():
                    new_text = f"{prefix}{sep}\n{title}\n"
            if new_text != text:
                try:
                    with open(caption, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    fixed += 1
                except OSError:
                    pass
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(fixed))
        except OSError:
            pass
        return fixed

    def _maintenance_finished(self, result):
        try:
            added, _touched = result
        except (TypeError, ValueError):
            added = 0
        # 历史通常在「读完」那一步就渲染过了；这里兜底，保证一定有内容。
        self._render_history_once()
        self._refresh_webdav_status()
        if added:
            self._render_contacts(reset=self.contact_page <= 1)

    def _maintenance_failed(self, message):
        """后台整理失败时仍要把历史渲染出来，不能一直停在“载入中…”。"""
        import logging
        logging.warning("maintenance failed: %s", message)
        self._render_history_once()
        self._refresh_webdav_status()

    def _settings_file(self):
        return os.path.join(config.DATA_DIR, "settings.json")

    def _load_settings(self):
        import json
        try:
            with open(self._settings_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        import json
        try:
            path = self._settings_file()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass

    def _setting_int(self, key, default):
        try:
            value = int(self._settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(self.THREAD_CHOICES, key=lambda choice: abs(choice - value))

    def _set_threads(self, kind, value):
        """线程数只有一个来源：主窗口设置。对话框/主界面改动都会写回并持久化。"""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        count = min(self.THREAD_CHOICES, key=lambda choice: abs(choice - value))
        if kind == "download":
            self._dl_threads = count
            self._settings["download_threads"] = count
        else:
            self._up_threads = count
            self._settings["upload_threads"] = count
        self._save_settings()
        self._sync_thread_combos()
        self.status.setText(f"{'下载' if kind == 'download' else '上传'}线程已设为 {count}，"
                            "下一次任务生效")

    def _set_pack_tar(self, checked):
        """「下载后打包成 tar」开关：立即持久化并同步给下载线程。"""
        enabled = bool(checked)
        self._settings["pack_tar"] = enabled
        self._save_settings()
        if getattr(self, "dm", None) is not None:
            self.dm.pack_tar = enabled

    def _set_original_quality(self, checked):
        """「优先下载原始上传码流」开关：写设置 + 线程侧快照 + 注册/注销回捞器。"""
        enabled = bool(checked)
        self._original_flag = enabled
        self._settings["original_quality"] = enabled
        self._save_settings()
        original_stream.set_enabled(enabled)
        downloader.set_original_resolver(
            original_stream.resolve_for_download if enabled else None)
        if not enabled:
            # 关掉开关就把浏览器会话和直链缓存一起释放，不要留着后台进程
            original_stream.close_session()
        if getattr(self, "status", None) is not None:
            self.status.setText(
                "已开启原始码流优先：拿不到原片会自动回退最高转码档"
                if enabled else "已关闭原始码流优先，按最高转码档下载")

    def _sync_thread_combos(self):
        for combo, value in ((self.transfer_dl_combo, self._dl_threads),
                             (self.transfer_up_combo, self._up_threads)):
            combo.blockSignals(True)
            combo.setCurrentText(str(value))
            combo.blockSignals(False)

    def _concurrency(self):
        return max(1, min(8, int(self._dl_threads or 3)))

    def _begin_download_stats(self, total):
        self._dl_meter.reset()
        self._dl_progress.clear()
        self.download_panel.set_stats(0, 0, f"0/{max(0, total)}")

    def _update_transfer_stats(self):
        if self._dl_mode is not None and self._dl_total:
            current = self._dl_done + sum(
                max(0, min(100, value)) / 100.0 for value in self._dl_progress.values())
            percent = current * 100 / max(1, self._dl_total)
            detail = f"{self._dl_done}/{self._dl_total}"
            if self._dl_fail:
                detail += f" · 失败 {self._dl_fail}"
            self.download_panel.set_stats(percent, self._dl_meter.rate(), detail)
        elif self._dl_mode == "sync" and not self._dl_total:
            self.download_panel.set_stats(0, 0, "等待作者作品…")
        elif self._single_dl:
            self.download_panel.set_stats(self._single_percent, self._dl_meter.rate(),
                                          self._single_title)
        elif self._dl_total:
            finished = self._dl_done >= self._dl_total
            percent = 100 if finished else self._dl_done * 100 / max(1, self._dl_total)
            detail = (f"完成 {self._dl_success} · 失败 {self._dl_fail}" if finished
                      else f"{self._dl_done}/{self._dl_total}")
            self.download_panel.set_stats(percent, 0.0, detail)
        else:
            self.download_panel.set_idle("暂无下载")

        if self._upload_active:
            total = max(1, self._up_total_bytes)
            percent = self._up_done_bytes * 100 / total if self._up_total_bytes else 0
            detail = (f"{transfer.format_bytes(self._up_done_bytes)} / "
                      f"{transfer.format_bytes(self._up_total_bytes)}")
            self.upload_panel.set_stats(percent, self._up_meter.rate(), detail)
        elif self._up_total_bytes:
            self.upload_panel.set_stats(
                100, 0.0,
                f"完成 {self._upload_counts.get('ok', 0)} · "
                f"跳过 {self._upload_counts.get('skipped', 0)} · "
                f"失败 {self._upload_counts.get('failed', 0)}")
        else:
            self.upload_panel.set_idle("暂无上传")

    def _dl_job(self, index):
        """按索引取下载任务；越界（上一轮遗留的回调）返回 None。

        停止同步后立刻重新开始，上一轮还在跑的任务可能带着旧索引回来；
        以前 `self._dl_jobs[index][0]` 直接按下标取值，越界会抛 IndexError
        （PySide6 新版本会直接终止进程），落在新范围内则把作品 A 的结果
        写成作品 B 的历史。
        """
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        jobs = self._dl_jobs or []
        if 0 <= index < len(jobs):
            return jobs[index]
        return None

    def _make_history_item(self, work, result):
        item = store_mod.HistoryItem()
        item.aweme_id = work.aweme_id
        item.title = work.display_title()
        item.author = work.safe_author()
        item.author_sec_uid = work.author_id
        item.unique_id = work.unique_id
        item.cover = work.cover_url
        item.video_path = result.video_path
        item.music_path = result.music_path
        item.album_paths = result.album_paths
        item.album_videos = list(getattr(result, "live_paths", []))
        item.is_album = work.is_album()
        item.create_time = work.create_time
        item.location = work.location
        item.ip_location = work.ip_location
        item.saved_at = int(time.time() * 1000)
        return item

    def _dl_item_done(self, index, result):
        if self._dl_mode is None:
            return
        job = self._dl_job(index)
        if job is None:
            return
        work = job[0]
        self._dl_progress.pop(index, None)
        key = self._dl_key(index)
        self._transfer_update(key, "完成")
        self.download_panel.set_item_progress(key, 100)
        # 历史改为 SQLite 增量写入：每个作品一条 INSERT，不再重写整份文件。
        self.store.add_history(self._make_history_item(work, result), flush=False)
        self._dl_done += 1
        self._dl_success += 1
        # 作者资料现在放在后台线程写，这里只要偶尔把内存快照交过去即可；
        # 序列化约 10 几毫秒，所以比原来的 60 秒更勤，崩溃时丢的改动更少。
        now = time.monotonic()
        last_flush = getattr(self, "_last_store_flush", None)
        if last_flush is None:
            self._last_store_flush = now
        elif now - last_flush >= 10.0:
            self._last_store_flush = now
            self.store.flush()
        if self._dl_mode == "batch":
            self.batch_progress.setValue(int(self._dl_done * 100 / max(1, self._dl_total)))
            self.batch_status.setText(f"并发下载中：完成 {self._dl_done}/{self._dl_total}")
            # 批量下载都是同一位作者：剩余 = 总作品数 - 已完成数
            remaining = max(0, self._dl_total - self._dl_done)
            download_log.append(
                work.safe_author(), work.display_title()[:50], remaining,
                quality=work.quality)
        else:
            author = (self._dl_authors[index]
                      if index < len(self._dl_authors) else self._dl_author)
            if author is not None:
                self.store.mark_saved(author, work.aweme_id)
                # 「该作者还有多少没下完」：用该作品入队时记下的待下载数递减
                pending = getattr(self, "_sync_author_remaining", {}).get(
                    author.sec_uid)
                if pending is not None:
                    remaining = max(0, pending - 1)
                    self._sync_author_remaining[author.sec_uid] = remaining
                    download_log.append(
                        author.label(), work.display_title()[:50], remaining,
                        quality=work.quality)
            self._sync_total_new += 1
            fetched = min(self._sync_index, len(self._sync_queue))
            self.contact_status.setText(
                f"{self._sync_prefix(author)}；已抓取 {fetched}/"
                f"{len(self._sync_queue)} 位作者，下载完成 "
                f"{self._dl_done}/{self._dl_total}")
        if work.aweme_id in self.dm.upgrade_ids:
            # 这是「云端低画质重下」：上传成功后要把这份本地副本删掉
            self._queue_upgrade_delete(result)
            self.dm.upgrade_ids.discard(work.aweme_id)
        self._queue_auto_upload(result)
        self._maybe_finish_downloads()

    def _dl_item_skipped(self, index, reason=""):
        """下载前预检判定「云端画质已经够好」：不下载、不写历史、不算失败。"""
        if self._dl_mode is None:
            return
        job = self._dl_job(index)
        if job is None:
            return
        work = job[0]
        self._dl_progress.pop(index, None)
        import logging
        logging.info("skip download aweme=%s: %s", work.aweme_id, reason)
        self.dm.upgrade_ids.discard(work.aweme_id)
        note = (reason or "已跳过")[:24]
        key = self._dl_key(index)
        self._transfer_update(key, "跳过：" + (reason or "")[:48])
        self.download_panel.set_item_progress(key, 100)
        self._dl_done += 1
        self._dl_skip += 1
        if self._dl_mode == "batch":
            self.batch_progress.setValue(int(self._dl_done * 100 / max(1, self._dl_total)))
            self.batch_status.setText(
                f"并发下载中：完成 {self._dl_done}/{self._dl_total}（跳过：{note}）")
        else:
            author = (self._dl_authors[index]
                      if index < len(self._dl_authors) else self._dl_author)
            if author is not None:
                pending = getattr(self, "_sync_author_remaining", {}).get(
                    author.sec_uid)
                if pending is not None:
                    self._sync_author_remaining[author.sec_uid] = max(0, pending - 1)
            fetched = min(self._sync_index, len(self._sync_queue))
            self.contact_status.setText(
                f"{self._sync_prefix(author)}；已抓取 {fetched}/"
                f"{len(self._sync_queue)} 位作者，下载完成 "
                f"{self._dl_done}/{self._dl_total}"
                f"（跳过 {self._dl_skip} 个，最近：{note}）")
        self._maybe_finish_downloads()

    def _dl_item_error(self, index, message):
        if self._dl_mode is None:
            return
        job = self._dl_job(index)
        if job is None:
            return
        self._dl_progress.pop(index, None)
        import logging
        work = job[0]
        logging.warning("download failed aweme=%s title=%s: %s",
                        work.aweme_id, work.display_title()[:40], message)
        # 「下载日志」：任务失败也补一条，避免只有「开始」没有结尾
        download_log.append_fail(
            self._dl_author_label(index, work), work.display_title()[:50])
        key = self._dl_key(index)
        self._transfer_update(key, "失败：" + (message or "")[:40])
        self._dl_done += 1
        self._dl_fail += 1
        if self._dl_mode == "batch":
            self.batch_progress.setValue(int(self._dl_done * 100 / max(1, self._dl_total)))
            self.batch_status.setText(
                f"并发下载中：完成 {self._dl_done}/{self._dl_total}（失败 {self._dl_fail}）")
        else:
            self._sync_total_fail += 1
            fetched = min(self._sync_index, len(self._sync_queue))
            try:
                failed_author = self._dl_authors[index]
            except (IndexError, AttributeError):
                failed_author = None
            self.contact_status.setText(
                f"{self._sync_prefix(failed_author)}；已抓取 {fetched}/"
                f"{len(self._sync_queue)} 位作者，下载完成 "
                f"{self._dl_done}/{self._dl_total}（失败 {self._dl_fail}）")
        self._maybe_finish_downloads()

    def _maybe_finish_downloads(self):
        if self._dl_mode is None or self._dl_done < self._dl_total:
            return
        mode = self._dl_mode
        if mode == "sync":
            self.store.flush()
            self._maybe_finish_sync()
            return
        self._dl_mode = None
        self.store.flush()
        if mode == "batch":
            self.batch_progress.setValue(100 if self._dl_done > 0 else 0)
            self.batch_download_btn.setEnabled(True)
            self.batch_download_btn.setText("再次下载")
            self.parse_btn.setEnabled(True)
            self.batch_status.setText(
                f"下载完成：成功 {self._dl_success}，失败 {self._dl_fail}"
                + (f"，跳过 {self._dl_skip}" if self._dl_skip else ""))
            self._render_history()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.setStyleSheet("QWidget#root{background:#ffffff;}")

        root = QWidget(self)
        root.setObjectName("root")
        root.setAutoFillBackground(True)
        root.setPalette(self.palette())
        outer.addWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)

        # 标题
        header = QHBoxLayout()
        title = QLabel("净影 <span style='color:#2563eb'>· 无水印下载</span>电脑版")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setStyleSheet("font-size:20px;font-weight:700;color:#1f2937;")
        header.addWidget(title)
        header.addStretch(1)
        # 「观看已下载」入口已按需求移除：本版本不再内置播放器，
        # 下载产物（作品文件夹或 tar）交给系统里的播放器打开。
        self.account_combo = QComboBox()
        self.account_combo.addItems([f"账号 {i + 1}" for i in range(self._account_count)])
        self.account_combo.setFixedWidth(126)
        self.account_combo.setToolTip(
            f"最多登录 {self._account_count} 个抖音号；同步时所有已登录账号按作者轮流使用")
        self.account_combo.setStyleSheet(
            "QComboBox{background:#ffffff;color:#1f2937;border:1px solid #c7d6ef;"
            "border-radius:8px;padding:6px 8px;}"
            "QComboBox QAbstractItemView{background:#ffffff;color:#1f2937;}")
        self.account_combo.currentIndexChanged.connect(self._refresh_login_button)
        header.addWidget(self.account_combo)
        self.login_btn = QPushButton("登录抖音")
        self.login_btn.setStyleSheet(
            "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #c7d6ef;"
            "border-radius:8px;padding:8px 16px;}"
            "QPushButton:hover{background:#f0f5ff;}")
        self.login_btn.clicked.connect(self._do_login)
        header.addWidget(self.login_btn)
        self._refresh_login_button()
        layout.addLayout(header)

        # 输入
        self.input = QTextEdit()
        self.input.setPlaceholderText(LINK_HINT)
        self.input.setFixedHeight(84)
        layout.addWidget(self.input)

        row = QHBoxLayout()
        self.paste_btn = QPushButton("粘贴")
        self.paste_btn.clicked.connect(self._paste)
        row.addWidget(self.paste_btn)
        self.parse_btn = QPushButton("开始解析")
        self.parse_btn.setStyleSheet(
            "QPushButton{background:#2563eb;color:#f5f7fb;font-weight:600;"
            "border-radius:8px;padding:8px 26px;}")
        self.parse_btn.clicked.connect(self._parse)
        row.addStretch(1)
        row.addWidget(self.parse_btn)
        layout.addLayout(row)

        # 状态
        self.status = QLabel("")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#2563eb;font-size:12px;")
        layout.addWidget(self.status)

        # 保存目录（直接显示，避免找不到下载位置）
        dir_row = QHBoxLayout()
        self.dir_label = QLabel(
            "保存目录：" + os.path.join(config.DOWNLOAD_BASE))
        self.dir_label.setStyleSheet("color:#6b7280;font-size:11px;")
        self.dir_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.dir_label.setWordWrap(True)
        self.dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dir_row.addWidget(self.dir_label, 1)
        self.dir_btn = QPushButton("打开")
        self.dir_btn.clicked.connect(self._open_download_dir)
        dir_row.addWidget(self.dir_btn)
        layout.addLayout(dir_row)

        # 三个独立窗口的入口（通讯录 / 传输 / 最近保存）
        entry_row = QHBoxLayout()
        entry_row.setSpacing(8)
        self.contacts_btn = QPushButton("通讯录")
        self.contacts_btn.setStyleSheet(
            "QPushButton{background:#ffffff;color:#2563eb;border:1px solid #c7d6ef;"
            "border-radius:8px;padding:8px 16px;}"
            "QPushButton:hover{background:#f0f5ff;}")
        self.contacts_btn.clicked.connect(self._open_contacts_window)
        entry_row.addWidget(self.contacts_btn)
        self.transfer_btn = QPushButton("传输")
        self.transfer_btn.setStyleSheet(self.contacts_btn.styleSheet())
        self.transfer_btn.clicked.connect(self._open_transfer_window)
        entry_row.addWidget(self.transfer_btn)
        self.history_btn = QPushButton("最近保存")
        self.history_btn.setStyleSheet(self.contacts_btn.styleSheet())
        self.history_btn.clicked.connect(self._open_history_window)
        entry_row.addWidget(self.history_btn)
        self.log_btn = QPushButton("查看日志")
        self.log_btn.setStyleSheet(self.contacts_btn.styleSheet())
        self.log_btn.clicked.connect(self._open_sync_log_window)
        entry_row.addWidget(self.log_btn)
        self.download_log_btn = QPushButton("下载日志")
        self.download_log_btn.setStyleSheet(self.contacts_btn.styleSheet())
        self.download_log_btn.clicked.connect(self._open_download_log_window)
        entry_row.addWidget(self.download_log_btn)
        entry_row.addStretch(1)
        layout.addLayout(entry_row)

        # 通讯录的四个操作按钮放在主窗口，不用打开通讯录窗口就能同步/导入
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.contact_sync_btn = QPushButton("全部同步")
        self.contact_sync_btn.setToolTip("")
        self.contact_sync_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.contact_sync_btn.setStyleSheet(
            "QPushButton{background:#2563eb;color:#ffffff;border:none;"
            "border-radius:8px;padding:8px 18px;font-weight:600;}"
            "QPushButton:hover{background:#1d4ed8;}")
        self.contact_sync_btn.clicked.connect(self._toggle_sync)
        action_row.addWidget(self.contact_sync_btn)
        self.retry_failed_btn = QPushButton("重试失败")
        self.retry_failed_btn.setToolTip("只重新同步上次读取失败的作者，不扫描整个通讯录")
        self.retry_failed_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.retry_failed_btn.clicked.connect(self._retry_failed_authors)
        self.retry_failed_btn.setVisible(False)
        action_row.addWidget(self.retry_failed_btn)
        self.contact_import_btn = QPushButton("批量导入")
        self.contact_import_btn.clicked.connect(self._batch_import_authors)
        action_row.addWidget(self.contact_import_btn)
        self.contact_txt_btn = QPushButton("导入 txt")
        self.contact_txt_btn.clicked.connect(self._import_authors_txt)
        action_row.addWidget(self.contact_txt_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        layout.addWidget(scroll, 1)
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        content = QVBoxLayout(inner)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(14)
        scroll.setWidget(inner)

        # 结果卡片
        self.result_card = _card()
        rl = QVBoxLayout(self.result_card)
        rl.setContentsMargins(16, 16, 16, 16)
        self.cover = QLabel("解析作品的封面会显示在这里")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedHeight(220)
        self.cover.setStyleSheet("color:#6b7280;background:#ffffff;border:1px solid #e3e9f2;border-radius:8px;")
        rl.addWidget(self.cover)
        self.title_label = QLabel("")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-size:15px;font-weight:600;")
        rl.addWidget(self.title_label)
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color:#6b7280;font-size:12px;")
        rl.addWidget(self.meta_label)
        self.save_btn = QPushButton("保存（视频 + 音乐）")
        self.save_btn.clicked.connect(self._save_current)
        rl.addWidget(self.save_btn)
        self.music_btn = QPushButton("单独下载音乐")
        self.music_btn.clicked.connect(self._save_music_only)
        self.music_btn.setVisible(False)
        rl.addWidget(self.music_btn)
        self.result_card.setVisible(False)
        content.addWidget(self.result_card)

        # 批量卡片
        self.batch_card = _card()
        bl = QVBoxLayout(self.batch_card)
        bl.setContentsMargins(16, 16, 16, 16)
        self.batch_author = QLabel("")
        self.batch_author.setStyleSheet("font-size:15px;font-weight:600;")
        bl.addWidget(self.batch_author)
        self.batch_count = QLabel("")
        self.batch_count.setStyleSheet("color:#6b7280;font-size:12px;")
        bl.addWidget(self.batch_count)
        self.batch_status = QLabel("")
        bl.addWidget(self.batch_status)
        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 100)
        self.batch_progress.setValue(0)
        self.batch_progress.setStyleSheet(
            "QProgressBar{background:#e5eaf3;border-radius:6px;height:8px;}"
            "QProgressBar::chunk{background:#2563eb;border-radius:6px;}")
        bl.addWidget(self.batch_progress)
        brow = QHBoxLayout()
        self.batch_download_btn = QPushButton("下载全部（视频 + 图集 + 音乐）")
        self.batch_download_btn.clicked.connect(self._batch_download)
        brow.addWidget(self.batch_download_btn)
        self.batch_add_btn = QPushButton("加入通讯录")
        self.batch_add_btn.clicked.connect(self._batch_add_contact)
        brow.addWidget(self.batch_add_btn)
        bl.addLayout(brow)
        self.batch_card.setVisible(False)
        content.addWidget(self.batch_card)


        # WebDAV 备份
        self.webdav_card = _card()
        wl = QVBoxLayout(self.webdav_card)
        wl.setContentsMargins(16, 12, 16, 12)
        whead = QHBoxLayout()
        wtitle = QLabel("WebDAV 备份")
        wtitle.setStyleSheet("font-size:15px;font-weight:600;")
        whead.addWidget(wtitle)
        whead.addStretch(1)
        self.webdav_config_btn = QPushButton("配置")
        self.webdav_config_btn.clicked.connect(self._configure_webdav)
        whead.addWidget(self.webdav_config_btn)
        self.webdav_test_btn = QPushButton("测试连接")
        self.webdav_test_btn.clicked.connect(self._test_webdav)
        whead.addWidget(self.webdav_test_btn)
        self.webdav_upload_btn = QPushButton("上传全部")
        self.webdav_upload_btn.clicked.connect(self._upload_webdav)
        whead.addWidget(self.webdav_upload_btn)
        wl.addLayout(whead)
        self.webdav_status = QLabel("")
        self.webdav_status.setWordWrap(True)
        self.webdav_status.setStyleSheet("color:#6b7280;font-size:11px;")
        wl.addWidget(self.webdav_status)
        pack_row = QHBoxLayout()
        pack_row.setSpacing(8)
        self.pack_tar_cb = QCheckBox("下载完成后打包成 tar（WebDAV 也上传 tar）")
        self.pack_tar_cb.setChecked(bool(self._settings.get("pack_tar", False)))
        self.pack_tar_cb.setToolTip(
            "选中后每个作品下载完打成一个 <作品id>.tar 并删除散文件，\n"
            "WebDAV 也只上传这个 tar。tar 不能直接播放，要先解包，\n"
            "解包后拖动进度、预览都会变慢。")
        self.pack_tar_cb.setStyleSheet("font-size:11px;color:#4b5563;")
        self.pack_tar_cb.toggled.connect(self._set_pack_tar)
        pack_row.addWidget(self.pack_tar_cb)
        # 用户要求：开关后面直接跟一句代价提示，避免只看到「省空间」就勾上。
        self.pack_tar_hint = QLabel("可能会影响播放性能")
        self.pack_tar_hint.setStyleSheet("color:#b45309;font-size:11px;")
        self.pack_tar_hint.setToolTip(
            "tar 是压缩归档，不能直接播放：每次观看都要先解包。\n"
            "如果主要在电脑上直接看，建议不勾选，保持作品文件夹。")
        pack_row.addWidget(self.pack_tar_hint)
        pack_row.addStretch(1)
        wl.addLayout(pack_row)
        # 「原始上传码流」优先下载：默认关。仅用于本人作品或已获授权内容的归档。
        # 规范里「原片」= 视频码流未经重编码，不等于上传文件的字节副本。
        self.original_cb = QCheckBox("优先下载原始上传码流（原画质）")
        self.original_cb.setChecked(
            bool(self._settings.get("original_quality", False)))
        self.original_cb.setToolTip(
            "在当前已登录的浏览器会话里读取作品详情，用 play 端点回捞原始码流；\n"
            "按「原片体积 / 最高转码档体积 ≥ 1.2」判定，拿不到就自动回退最高转码档。\n"
            "需要已登录抖音；每个作品会多 2 次请求并节流 1.2~3 秒，原片可能大 20 倍。\n"
            "仅用于本人作品或已获授权内容的归档与画质分析。")
        self.original_cb.setStyleSheet("font-size:11px;color:#4b5563;")
        self.original_cb.toggled.connect(self._set_original_quality)
        wl.addWidget(self.original_cb)
        self.webdav_skip_cb = QCheckBox("跳过已上传（只传新增或变化的文件）")
        self.webdav_skip_cb.setChecked(True)
        self.webdav_skip_cb.setStyleSheet("font-size:11px;color:#4b5563;")
        wl.addWidget(self.webdav_skip_cb)
        self.webdav_auto_cb = QCheckBox("通讯录同步时自动上传新文件")
        self.webdav_auto_cb.setChecked(bool(self._settings.get("webdav_auto_upload", True)))
        self.webdav_auto_cb.setToolTip("同步下载通讯录新作品的同时，把新文件传到 WebDAV")
        self.webdav_auto_cb.setStyleSheet("font-size:11px;color:#4b5563;")
        self.webdav_auto_cb.toggled.connect(self._set_auto_upload_enabled)
        wl.addWidget(self.webdav_auto_cb)
        self.webdav_delete_cb = QCheckBox(
            "上传成功后删除本地作品/图片/音乐（保留文案、作者资料）")
        self.webdav_delete_cb.setChecked(
            bool(self._settings.get("webdav_delete_after_upload", False)))
        self.webdav_delete_cb.setToolTip(
            "只删除已成功上传的视频、图片、音乐；文案、历史记录和作者头像/简介不会删除")
        self.webdav_delete_cb.setStyleSheet("font-size:11px;color:#b45309;")
        self.webdav_delete_cb.toggled.connect(self._set_delete_after_upload)
        wl.addWidget(self.webdav_delete_cb)
        # 「云端低画质自动重下」：默认关闭。勾选后同步时先读 WebDAV 上已有文件的
        # 实际分辨率，确实低于本次解析结果才重下，避免把整个库白下一遍。
        self.webdav_upgrade_cb = QCheckBox(
            "云端已有低画质时自动重下（上传后删除本地副本）")
        self.webdav_upgrade_cb.setChecked(
            bool(self._settings.get("webdav_upgrade_low_quality", False)))
        self.webdav_upgrade_cb.setToolTip(
            "同步时先读取 WebDAV 上已有作品的真实分辨率（只取几 KB 文件头）：\n"
            "低于本次解析结果才重新下载，下载后覆盖上传，上传成功再删掉本地副本。\n"
            "需要同时开启「通讯录同步时自动上传新文件」，否则不会自动上传和清理。")
        self.webdav_upgrade_cb.setStyleSheet("font-size:11px;color:#b45309;")
        self.webdav_upgrade_cb.toggled.connect(self._set_cloud_upgrade)
        wl.addWidget(self.webdav_upgrade_cb)
        self._refresh_webdav_status(lazy=True)
        content.addWidget(self.webdav_card)

        content.addStretch(1)

        self._build_windows()

    # ---------- 独立窗口 ----------
    def _build_windows(self):
        """创建通讯录 / 最近保存 / 传输 / 同步日志 / 下载日志窗口，并挂引用。

        业务代码里一直是 `self.contact_list`、`self.history_list`、
        `self.download_panel` 这种写法，这里保持同名引用，窗口化之后这些调用
        一行都不用改；窗口关闭只是隐藏，控件仍然可以被子线程更新。
        """
        self.contacts_window = windows.ContactsWindow(self)
        self.history_window = windows.HistoryWindow(self)
        self.transfer_window = windows.TransferWindow(self)
        self.sync_log_window = windows.SyncLogWindow(self)
        self.download_log_window = windows.DownloadLogWindow(self)

        contacts = self.contacts_window
        self.contact_title = contacts.title
        self.contact_status = contacts.status
        self.sync_log = contacts.sync_log
        self.contact_list = contacts.list
        self.contact_more_btn = contacts.more_btn
        contacts.more_btn.clicked.connect(self._load_more_contacts)

        history = self.history_window
        self.history_title = history.title
        self.history_list = history.list
        self.history_more_btn = history.more_btn
        history.open_dir_btn.clicked.connect(self._open_download_dir)
        history.clear_btn.clicked.connect(self._clear_history)
        history.more_btn.clicked.connect(self._load_more_history)
        history.list.itemDoubleClicked.connect(self._open_history_item)

        trans = self.transfer_window
        self.transfer_dl_combo = trans.dl_combo
        self.transfer_up_combo = trans.up_combo
        self.download_panel = trans.download_panel
        self.upload_panel = trans.upload_panel
        trans.dl_combo.setCurrentText(str(self._dl_threads))
        trans.up_combo.setCurrentText(str(self._up_threads))
        trans.dl_combo.currentTextChanged.connect(
            lambda value: self._set_threads("download", value))
        trans.up_combo.currentTextChanged.connect(
            lambda value: self._set_threads("upload", value))
        trans.clear_btn.clicked.connect(self._clear_transfers)

        log_window = self.sync_log_window
        log_window.refresh_btn.clicked.connect(log_window.reload)
        log_window.clear_btn.clicked.connect(log_window.clear_log)

        dl_log_window = self.download_log_window
        dl_log_window.refresh_btn.clicked.connect(dl_log_window.reload)
        dl_log_window.clear_btn.clicked.connect(dl_log_window.clear_log)

        self._update_retry_button()

    # ---------- 输入 / 解析 ----------
    def _paste(self):
        clipboard = QApplication.clipboard()
        if clipboard:
            self.input.setPlainText(clipboard.text().strip())

    def _browser_busy_reason(self):
        """解析/下载音乐前检查浏览器是否可用、是否正被别的任务占着。

        登录窗口和单条解析都用账号1的浏览器配置目录；parser 里按目录加锁，
        登录进行中再点解析，解析线程会一直阻塞在锁上（最长 15 分钟），
        状态栏停在「正在解析链接…」，而且 `self.worker` 也被占死。
        没装浏览器时同理：Playwright 会抛 “Executable doesn't exist”，
        提示和真实原因没关系，同步还会给每位作者各起一次注定失败的浏览器。
        """
        if not (config.CHROME_PATH or parser.CHROME_PATH):
            return ("没有找到可用的 Chrome / Edge。请安装其中一个，"
                    "或设置环境变量 JINGYING_CHROME 指向 chrome.exe。")
        if getattr(self, "_login_running", False):
            return "登录窗口还开着，请先完成登录再操作。"
        worker = getattr(self, "_check_worker", None)
        if worker is not None and worker.isRunning():
            return "正在检查登录状态，请稍后再试。"
        return ""

    def _parse(self):
        raw = self.input.toPlainText().strip()
        if not raw:
            QMessageBox.information(self, "提示", "请先粘贴抖音链接")
            return
        # 同步现在用多账号并发浏览器；单条解析默认走账号1的配置目录，
        # 为避免和正在同步的账号1抢同一个浏览器目录，这里直接拦住。
        if self.syncing:
            self.status.setText("同步进行中，请先点「停止同步」再解析单条链接")
            return
        reason = self._browser_busy_reason()
        if reason:
            self.status.setText(reason)
            QMessageBox.information(self, "提示", reason)
            return
        if links.is_profile_input(raw):
            sec_uid = links.extract_sec_uid(raw)
            self._run_worker(parser.resolve_profile, sec_uid, on_profile=self._show_batch,
                             busy_text="正在打开创作者主页…")
        else:
            self._run_worker(parser.resolve_video, raw, on_result=self._show_result,
                             busy_text="正在解析链接…")

    def _track_worker(self, worker):
        """持有后台线程引用，线程结束后自动释放，避免运行中被析构导致崩溃。"""
        self._workers.append(worker)
        worker.finished.connect(self._on_worker_finished)
        return worker

    def _on_worker_finished(self):
        worker = self.sender()
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if getattr(self, "worker", None) is worker:
            self.worker = None
        if getattr(self, "_check_worker", None) is worker:
            self._check_worker = None
            if getattr(self, "_sync_after_check", False):
                self._sync_after_check = False
                QTimer.singleShot(0, self._sync_all)
        if getattr(self, "_maintenance_worker", None) is worker:
            self._maintenance_worker = None
        if getattr(self, "_upload_worker", None) is worker:
            self._upload_worker = None
        worker.deleteLater()

    def _wait_old_worker(self):
        """等上一个线程收尾，避免 QThread 运行中被覆盖析构。"""
        if self.worker and self.worker.isRunning():
            self.worker.wait(3000)

    def _run_worker(self, task, *args, on_result=None, on_profile=None, on_failed=None,
                    busy_text="处理中…", force=False, **kwargs):
        if self.worker and self.worker.isRunning() and not force:
            return
        self.status.setText(busy_text)
        # 不能在这里 wait() 旧线程：同步时上一个 worker 刚 emit done 还没完全退出，
        # 主线程 wait(3000) 会直接卡住界面几秒。旧 worker 由 _workers 持有，
        # 它的 done/finished 信号仍会正常回收，不需要阻塞等待。
        worker = Worker(task, *args, **kwargs)
        worker.stage.connect(
            lambda text: self.status.setText(text),
            Qt.ConnectionType.QueuedConnection)
        worker.done.connect(
            lambda result: self._handle_done(result, on_result, on_profile),
            Qt.ConnectionType.QueuedConnection)
        if on_failed is not None:
            worker.failed.connect(on_failed, Qt.ConnectionType.QueuedConnection)
        else:
            worker.failed.connect(self._handle_failed,
                                  Qt.ConnectionType.QueuedConnection)
        self.worker = self._track_worker(worker)
        worker.start()

    def _handle_done(self, result, on_result, on_profile):
        self.status.setText("完成")
        if on_profile and result is not None:
            on_profile(result)
            return
        if on_result and result is not None:
            on_result(result)

    def _handle_failed(self, message):
        import logging
        logging.warning("worker failed: %s", message)
        self.status.setText("")
        login_needed = any(key in (message or "") for key in
                           ("需要登录", "请先登录", "登录已失效"))
        if login_needed and self._logged_in is not True:
            box = QMessageBox(self)
            box.setWindowTitle("需要登录")
            box.setText(
                "主页作品列表需要登录抖音。\n\n点击「是」打开浏览器扫码登录，"
                "登录完成后回到应用重试。")
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if box.exec() == QMessageBox.StandardButton.Yes:
                self._do_login()
            else:
                QMessageBox.warning(self, "提示", message)
        else:
            QMessageBox.warning(self, "提示", message)

    # ---------- 登录 ----------
    def _login_state_file(self):
        return os.path.join(config.DATA_DIR, "login_state.json")

    def _load_login_state(self):
        import json
        count = self._account_count
        try:
            with open(self._login_state_file(), encoding="utf-8") as f:
                data = json.load(f)
            accounts = data.get("accounts")
            if isinstance(accounts, list):
                values = list(accounts) + [None] * count
                self._account_logged = [
                    True if v is True else (False if v is False else None)
                    for v in values[:count]]
            else:
                self._account_logged = [bool(data.get("logged_in"))] + [False] * (count - 1)
            self._logged_in = any(v is True for v in self._account_logged)
        except (OSError, ValueError):
            self._logged_in = None
            self._account_logged = [None] * count

    def _save_login_state(self):
        import json, time
        try:
            with open(self._login_state_file(), "w", encoding="utf-8") as f:
                json.dump({
                    "logged_in": any(v is True for v in self._account_logged),
                    "accounts": [bool(v) for v in self._account_logged],
                    "checked_at": int(time.time()),
                }, f, ensure_ascii=False)
        except OSError:
            pass

    def _refresh_login_button(self):
        if not hasattr(self, "login_btn") or not hasattr(self, "account_combo"):
            return
        for i, state in enumerate(self._account_logged):
            mark = "✓" if state is True else ("…" if state is None else "未登录")
            if i < self.account_combo.count():
                self.account_combo.setItemText(i, f"账号{i + 1} {mark}")
        idx = max(0, min(self._account_count - 1, self.account_combo.currentIndex()))
        ok = self._account_logged[idx]
        self.login_btn.setText("已登录" if ok is True else "登录抖音")
        tips = []
        for i, state in enumerate(self._account_logged):
            tips.append(f"账号{i + 1}：" + (
                "已登录" if state is True else ("未检测" if state is None else "未登录")))
        self.login_btn.setToolTip("\n".join(tips) + "\n同步时所有已登录账号会轮流使用")

    def _set_login_state(self, ok, names=None, account_index=None):
        idx = self.account_combo.currentIndex() if account_index is None else account_index
        idx = max(0, min(self._account_count - 1, int(idx)))
        self._account_logged[idx] = bool(ok)
        self._logged_in = any(v is True for v in self._account_logged)
        self._save_login_state()
        self._refresh_login_button()
        if ok:
            self.status.setText(f"抖音账号{idx + 1}：已登录")
        elif ok is False:
            self.status.setText(f"抖音账号{idx + 1}：未登录（需要时再登录）")

    def _check_login(self):
        if not config.CHROME_PATH:
            for i in range(self._account_count):
                self._set_login_state(False, [], i)
            self.login_btn.setToolTip(
                "未找到 Chrome/Edge。请安装浏览器，或设置环境变量 JINGYING_CHROME。")
            return
        self.login_btn.setText("检查登录…")

        def check_all():
            results = []
            for session, cookie_file in zip(config.SESSION_DIRS, config.COOKIE_FILES):
                try:
                    results.append(parser.check_login_status_sync(session, cookie_file))
                except Exception:  # noqa: BLE001
                    results.append((False, []))
            return results

        worker = Worker(check_all)
        worker.done.connect(self._on_login_checked)
        worker.failed.connect(
            lambda _msg: self._on_login_checked(
                [(False, []) for _ in range(self._account_count)]),
            Qt.ConnectionType.QueuedConnection)
        self._check_worker = self._track_worker(worker)
        worker.start()

    def _on_login_checked(self, result):
        if isinstance(result, list) and len(result) == self._account_count:
            for i, item in enumerate(result):
                try:
                    ok, names = item
                except (TypeError, ValueError):
                    ok, names = False, []
                self._set_login_state(ok, names, i)
            active = [str(i + 1) for i, v in enumerate(self._account_logged) if v is True]
            if active:
                self.status.setText("已登录抖音账号：" + "、".join(active))
            else:
                self.status.setText("抖音账号：未登录")
            return
        try:
            ok, names = result
        except (TypeError, ValueError):
            ok, names = False, []
        self._set_login_state(ok, names, 0)

    def _do_login(self):
        if self.syncing:
            QMessageBox.information(self, "登录", "同步进行中，请先停止同步再切换账号登录。")
            return
        if not config.CHROME_PATH:
            QMessageBox.warning(
                self, "登录",
                "没有找到 Chrome 或 Edge。\n请先安装浏览器，或设置环境变量 "
                "JINGYING_CHROME 指向 chrome.exe / msedge.exe。")
            return
        if getattr(self, "_login_running", False):
            return
        account_index = max(0, min(self._account_count - 1,
                                   self.account_combo.currentIndex()))
        session_dir = config.SESSION_DIRS[account_index]
        cookie_file = config.COOKIE_FILES[account_index]
        self._login_running = True
        self._login_result = None
        self._login_stop = threading.Event()
        self.login_btn.setEnabled(False)
        self.account_combo.setEnabled(False)
        self.status.setText(f"正在打开浏览器，登录账号{account_index + 1}…")

        def worker():
            try:
                parser.login_sync(self._login_stop, 900, session_dir, cookie_file)
                error = ""
            except parser.ParseCancelled:
                # 关窗时 _login_stop 被置位，登录在等浏览器锁的检查点主动退出，
                # 这不是「登录失败」，也不要再往已关闭的界面发信号。
                return
            except Exception as e:  # noqa: BLE001
                error = str(e) or "登录失败"
            self.login_signal.emit(error)

        threading.Thread(target=worker, daemon=True).start()

        box = QMessageBox(self)
        box.setWindowTitle(f"登录抖音 · 账号{account_index + 1}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "浏览器窗口即将打开（若没看到，请查看任务栏）。\n\n"
            f"请登录要使用的第 {account_index + 1} 个抖音号，完成后回到这里点「我已完成登录」。")
        box.addButton("我已完成登录", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        self._login_stop.set()
        if box.clickedButton() is cancel:
            self.status.setText("已取消登录")
        else:
            self.status.setText("正在确认登录状态…")

        # 等待后台线程确认 Cookie（最多 10 秒），期间处理 UI 事件。
        import time
        deadline = time.time() + 10
        while self._login_result is None and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.1)

        error = self._login_result
        self._login_running = False
        self.login_btn.setEnabled(True)
        self.account_combo.setEnabled(True)
        self.status.setText("")
        if box.clickedButton() is cancel:
            return
        if error is None:
            QMessageBox.warning(self, "登录", "登录状态确认超时，请重试。")
        elif error:
            QMessageBox.warning(self, "登录", error)
        else:
            self._set_login_state(True, [], account_index)
            QMessageBox.information(
                self, "登录", f"账号{account_index + 1} 登录成功，Cookie 已保存在本机。")

    def _login_done(self, error):
        self._login_result = error

    # ---------- 结果 ----------
    def _show_result(self, info: MediaInfo):
        self.current = info
        self.result_card.setVisible(True)
        self.title_label.setText(info.display_title())
        quality = info.quality or ""
        self.meta_label.setText(
            f"作者：{info.safe_author()}  抖音号：{info.unique_id or '—'}   "
            f"赞 {info.digg_count} · 评 {info.comment_count}"
            + ("  图集 " + str(len(info.image_urls)) + " 张" if info.is_album() else "")
            + (f"  ·  {quality}" if quality else ""))
        self.save_btn.setText("保存图集 + 音乐" if info.is_album() else "保存视频 + 音乐")
        self.music_btn.setVisible(bool(info.music_url))
        self.music_btn.setEnabled(True)
        if not info.cover_url:
            self.cover.setPixmap(QPixmap())
            self.cover.setText("解析作品的封面会显示在这里")
            return
        target = self._cover_path(info.aweme_id)
        try:
            fresh = os.path.exists(target) and time.time() - os.path.getmtime(target) < 3 * 86400
        except OSError:
            fresh = False
        if fresh:
            self._apply_cover(target, info.aweme_id)
        else:
            self.cover.setPixmap(QPixmap())
            self.cover.setText("封面加载中…")
            self._fetch_cover(info.cover_url, info.aweme_id)

    @staticmethod
    def _cover_path(aweme_id):
        name = (aweme_id or "cover").replace("/", "_").replace("\\", "_")
        return os.path.join(config.DATA_DIR, "covers", name + ".jpg")

    def _fetch_cover(self, url, aweme_id):
        """封面在后台线程下载，避免解析完成时界面被网络阻塞。"""
        target = self._cover_path(aweme_id)

        def task(cancel_event=None):
            import requests
            os.makedirs(os.path.dirname(target), exist_ok=True)
            headers = {"User-Agent": config.DESKTOP_UA, "Referer": "https://www.douyin.com/"}
            part = target + ".part"
            try:
                with requests.get(url, headers=headers, timeout=(10, 20), stream=True) as resp:
                    resp.raise_for_status()
                    with open(part, "wb") as f:
                        for chunk in resp.iter_content(64 * 1024):
                            if cancel_event is not None and cancel_event.is_set():
                                return None
                            if chunk:
                                f.write(chunk)
                os.replace(part, target)
                return target
            finally:
                # 取消/异常时不留半截 .part（封面每次全量覆盖，没有续传语义）
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass

        worker = Worker(task)
        worker.done.connect(lambda path: self._apply_cover(path, aweme_id),
                            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(lambda _msg: self._cover_failed(aweme_id),
                              Qt.ConnectionType.QueuedConnection)
        self._track_worker(worker)
        worker.start()

    def _cover_failed(self, aweme_id):
        if self.current is not None and aweme_id and aweme_id != self.current.aweme_id:
            return
        self.cover.setText("封面加载失败（不影响下载）")

    def _apply_cover(self, path, aweme_id):
        if self.current is not None and aweme_id and aweme_id != self.current.aweme_id:
            return
        if not path or not os.path.exists(path):
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        width = max(240, self.cover.width())
        height = max(140, self.cover.height())
        self.cover.setText("")
        self.cover.setPixmap(pix.scaled(
            width, height, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # ---------- 保存 ----------
    def _save_current(self):
        if not self.current:
            return
        info = self.current
        author_folder = downloader.build_author_folder(info.safe_author(), info.unique_id)
        work_folder = downloader.build_work_folder(info, info.safe_author())
        self._download_work(info, author_folder, work_folder, single=True)

    def _save_music_only(self):
        """只下载背景音乐，存进该作品自己的文件夹（净影/作者_id/<作品id>/音乐.mp3）。"""
        info = self.current
        if not info or not info.music_url:
            QMessageBox.information(self, "提示", "这个作品没有可下载的背景音乐。")
            return
        if self.worker and self.worker.isRunning():
            return
        reason = self._browser_busy_reason()
        if reason:
            self.status.setText(reason)
            QMessageBox.information(self, "提示", reason)
            return
        author_folder = downloader.build_author_folder(info.safe_author(), info.unique_id)
        title = downloader.sanitize_segment(info.display_title(), 40)
        # 和完整下载保持同一个目录：净影/作者_id/<作品id>/音乐.mp3
        target_dir = downloader.work_dir(author_folder, title, info)
        ext = downloader._guess_ext(info.music_url, "audio")
        target = os.path.join(target_dir, "音乐" + ext)
        self.status.setText("正在单独下载音乐…")
        self.music_btn.setEnabled(False)

        def task(with_stage=None, cancel_event=None):
            return downloader._download_to_file(info.music_url, target, None, 0, 1,
                                                cancel_event=cancel_event)

        def done(size):
            self.status.setText(f"音乐已保存：{target}")
            self.music_btn.setEnabled(True)
            try:
                os.startfile(target_dir)
            except OSError:
                pass
            QMessageBox.information(self, "音乐已保存", target)

        def fail(message):
            self.music_btn.setEnabled(True)
            self.status.setText("")
            QMessageBox.warning(self, "下载音乐失败", message or "下载失败")

        worker = Worker(task)
        worker.done.connect(lambda size: done(size),
                            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(lambda msg: fail(msg),
                              Qt.ConnectionType.QueuedConnection)
        self.worker = self._track_worker(worker)
        worker.start()

    def _download_work(self, info, author_folder, work_folder, single=False,
                       on_each_done=None):
        if self.worker and self.worker.isRunning():
            return
        if self._dl_mode is not None:
            QMessageBox.information(self, "提示", "已有下载任务在进行。")
            return
        self.status.setText(f"正在下载：{info.display_title()}（含音乐）…")
        self._single_dl = True
        self._single_percent = 0
        self._single_title = info.display_title()[:32]
        self._dl_meter.reset()
        self.download_panel.set_stats(0, 0, self._single_title)

        def task(with_stage=None, cancel_event=None):
            result = downloader.download_work(
                info, author_folder, work_folder,
                cancel_event=cancel_event,
                # 与并行下载一致：一个作品内的所有文件排成一条进度轴，
                # 换算成作品进度，并且只前进不后退。
                progress=lambda pct, idx, total: setattr(
                    self, "_single_percent",
                    max(self._single_percent,
                        int((idx + max(0, min(100, pct)) / 100.0)
                            * 100 / max(1, total)))),
                byte_callback=self._dl_meter.add)
            item = store_mod.HistoryItem()
            item.aweme_id = info.aweme_id
            item.title = info.display_title()
            item.author = info.safe_author()
            item.author_sec_uid = info.author_id
            item.unique_id = info.unique_id
            item.cover = info.cover_url
            item.video_path = result.video_path
            item.music_path = result.music_path
            item.album_paths = result.album_paths
            item.album_videos = list(getattr(result, "live_paths", []))
            item.share_url = info.share_url
            item.is_album = info.is_album()
            item.create_time = info.create_time
            item.location = info.location
            item.ip_location = info.ip_location
            item.saved_at = int(time.time() * 1000)
            self.store.add_history(item)
            return result

        def done(_result):
            self._single_dl = False
            self.download_panel.set_stats(100, 0, f"完成：{self._single_title}")
            self.status.setText(
                f"已保存：{info.display_title()}" + ("（含音乐）" if _result.music_saved else ""))
            self._render_history()
            if on_each_done:
                on_each_done()

        def failed(message):
            self._single_dl = False
            self.download_panel.set_idle("下载失败")
            self._handle_failed(message)

        worker = Worker(task)
        worker.done.connect(lambda r: done(r),
                            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        worker.stage.connect(lambda t: self.status.setText(t),
                             Qt.ConnectionType.QueuedConnection)
        self.worker = self._track_worker(worker)
        worker.start()

    # ---------- 批量 ----------
    def _show_batch(self, profile: parser.ProfileInfo):
        self.batch_info = profile
        self.batch_card.setVisible(True)
        self.batch_author.setText(
            f"{profile.nickname}" + (f"（抖音号 {profile.unique_id}）" if profile.unique_id else ""))
        self.batch_count.setText(
            f"视频 {len(profile.videos)} · 图集 {len(profile.albums)}  共 "
            f"{len(profile.videos) + len(profile.albums)} 个作品")
        self.batch_status.setText("")
        self.batch_progress.setValue(0)

    def _all_batch_works(self):
        if not self.batch_info:
            return []
        return list(self.batch_info.videos) + list(self.batch_info.albums)

    def _batch_download(self):
        if self._dl_mode is not None:
            QMessageBox.information(self, "提示", "已有下载任务在进行。")
            return
        works = self._all_batch_works()
        if not works:
            QMessageBox.information(self, "提示", "没有可下载的作品")
            return
        info = self.batch_info
        for work in works:
            if not work.unique_id:
                work.unique_id = info.unique_id
            if not work.author_id:
                work.author_id = info.sec_uid
            if not work.author:
                work.author = info.nickname
        # 「下载日志」记一条：主页批量下载走的是默认账号（账号1）
        download_log.append_open("账号1", works[0].safe_author(), len(works))
        author_folder = downloader.build_author_folder(info.nickname, info.unique_id)
        jobs = [(work, author_folder,
                 downloader.build_work_folder(work, info.nickname)) for work in works]
        self._dl_mode = "batch"
        self._dl_jobs = jobs
        self._dl_run += 1
        self._dl_done = 0
        self._dl_total = len(jobs)
        self._dl_success = 0
        self._dl_fail = 0
        self._dl_skip = 0
        self.batch_progress.setValue(0)
        self.batch_download_btn.setEnabled(False)
        self.batch_status.setText(
            f"并发下载中：{len(jobs)} 个作品，{self._concurrency()} 线程")
        self.status.setText("批量下载中…")
        self._begin_download_stats(len(jobs))
        self.dm.start(jobs, self._concurrency(), byte_callback=self._dl_meter.add)

    def _batch_add_contact(self):
        if not self.batch_info:
            return
        from store import Author
        author = Author(self.batch_info.sec_uid, self.batch_info.nickname,
                        self.batch_info.unique_id, self.batch_info.avatar_url)
        author.signature = self.batch_info.signature
        # 加入通讯录时保存作者头像与主页简介。头像改到后台线程下载：
        # 以前在主线程直接 requests.get（timeout=30），网络慢时点一下按钮
        # 界面就假死半分钟；同步流程早就是这样做的。
        assets = author_assets.update_author_assets(
            author, self.batch_info, force=True, download_avatar=False)
        self.store.add_author(author)
        self._render_contacts()
        if assets.get("avatar_needs_download"):
            self._queue_avatar_download(author, author.avatar_url)
        QMessageBox.information(
            self, "通讯录",
            "已加入通讯录，已保存作者头像与主页简介。\n之后每次打开会自动同步新作品，"
            "资料有修改时会另存一份。")

    def _batch_import_authors(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "批量导入主页链接",
            "每行粘贴一个作者主页链接（也支持整段分享文案）：")
        if not ok or not (text or "").strip():
            return
        self._import_authors_text(text, "批量导入")

    def _import_authors_txt(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择关注列表 txt", "", "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            raw = open(path, "rb").read()
        except OSError as e:
            QMessageBox.warning(self, "导入 txt", f"读取失败：{e}")
            return
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text.strip():
            QMessageBox.warning(self, "导入 txt", "文件为空或编码无法识别。")
            return
        self._import_authors_text(text, "txt 导入")

    def _import_authors_text(self, text, source="批量导入"):
        sec_uids = links.extract_all_sec_uids(text)
        if not sec_uids:
            QMessageBox.warning(self, "导入", "没有识别到作者主页链接。")
            return
        existing = {a.sec_uid for a in self.store.load_authors()}
        added = 0
        skipped = 0
        for sid in sec_uids:
            if sid in existing:
                skipped += 1
                continue
            author = store_mod.Author(sid, "待同步", "", "")
            # flush=False：以前每加一个作者都同步重写整份 authors.json（几十万字节，
            # 还要 os.replace 一次）。导入几百个链接时主线程会冻结几十秒。
            # 这里只标脏，循环结束统一落盘一次。
            self.store.add_author(author, flush=False)
            existing.add(sid)
            added += 1
        if added:
            self.store.flush(block=True)
        self._apply_pagination_reset()
        self._render_contacts()
        self.contact_status.setText(
            f"{source}完成：新增 {added} 个，已存在 {skipped} 个。点「全部同步」补全资料并下载。")

    def _apply_pagination_reset(self):
        self.contact_page = 1

    # ---------- 通讯录 ----------
    def _contact_avatar(self, path, name, size=44):
        key = (path, name, size)
        cached = self._avatar_cache.get(key)
        if cached is not None:
            return cached
        # 头像本身只有 100×100 左右，直接解码再缩放最快；用 QImageReader 先读
        # 尺寸反而要多创建一次读取器（每个文件约 8 ms），比整张解码还慢。
        pix = QPixmap(path) if path and os.path.exists(path) else QPixmap()
        if pix.isNull():
            pix = QPixmap(size, size)
            pix.fill(QColor("#dbe7ff"))
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#2563eb"))
            font = QFont("Microsoft YaHei UI", 12)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, (name or "待")[:1])
            painter.end()
            return self._cache_avatar(key, pix)
        source = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(0, 0, size, size)
        painter.setClipPath(clip)
        painter.drawPixmap((size - source.width()) // 2, (size - source.height()) // 2, source)
        painter.end()
        return self._cache_avatar(key, canvas)

    def _cache_avatar(self, key, pixmap):
        if len(self._avatar_cache) >= 200:
            self._avatar_cache.clear()
        self._avatar_cache[key] = pixmap
        return pixmap

    def _render_contacts(self, reset=True):
        if reset:
            self.contact_page = 1
        old_scroll = (self.contact_list.verticalScrollBar().value()
                      if not reset else 0)
        self.contact_list.clear()
        all_authors = self.store.load_authors()
        authors = all_authors[: self.contact_page * 40]
        if not authors:
            placeholder = QListWidgetItem("还没有作者：解析作者主页后点「加入通讯录」")
            placeholder.setForeground(Qt.GlobalColor.gray)
            self.contact_list.addItem(placeholder)
        for author in authors:
            signature = (author.signature or "").strip()
            name = author.nickname or "待同步"
            if name == "待同步" and author.sec_uid:
                name = "待同步…" + author.sec_uid[-6:]
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, author.sec_uid)
            item.setSizeHint(QSize(0, 70))
            item.setToolTip(author.label() + "\n" + (signature or "无简介"))

            row = QWidget(self.contact_list)
            # 行本身不再整体透明，否则右侧“删除”按钮收不到鼠标事件；
            # 头像/文字仍然透明，不会挡住按钮点击。
            row.setToolTip(author.label() + "\n" + (signature or "无简介"))
            # 必须给不透明底色：透明行在部分 Windows 主题下 hover 会透出黑色底框
            row.setStyleSheet("background:#ffffff;")
            row.setAutoFillBackground(False)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(12)
            avatar = QLabel(row)
            avatar.setFixedSize(44, 44)
            avatar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            avatar.setPixmap(self._contact_avatar(author.avatar_local, name, 44))
            rl.addWidget(avatar)
            # 删除按钮放在左侧固定位置：主界面内容较宽时右侧按钮会被窗口裁掉。
            del_btn = QPushButton("删除", row)
            del_btn.setFixedSize(44, 26)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            del_btn.setToolTip("只从通讯录删除，不会删除已下载作品和作者资料")
            del_btn.setEnabled(not self.syncing)
            del_btn.setStyleSheet(
                "QPushButton{background:#fff1f2;color:#b91c1c;border:1px solid #fecdd3;"
                "border-radius:5px;font-size:11px;padding:0;}"
                "QPushButton:hover{background:#ffe4e6;}"
                "QPushButton:disabled{color:#cbd5e1;background:#f8fafc;border-color:#e2e8f0;}")
            del_btn.clicked.connect(
                lambda _checked=False, sid=author.sec_uid, nm=name:
                self._delete_author(sid, nm))
            rl.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignVCenter)
            text_box = QVBoxLayout()
            text_box.setSpacing(2)
            name_label = QLabel(name, row)
            name_label.setStyleSheet("font-size:14px;font-weight:600;color:#1f2937;background:transparent;")
            name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            uid = author.unique_id or "未同步"
            sub = f"抖音号：{uid}   ·   已下载 {len(author.saved_ids)} 个"
            sub_label = QLabel(sub, row)
            sub_label.setStyleSheet("font-size:11px;color:#6b7280;background:transparent;")
            sub_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            sub_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text_box.addWidget(name_label)
            text_box.addWidget(sub_label)
            if signature:
                sig = (signature[:28] + "…") if len(signature) > 28 else signature
                sig_label = QLabel(sig, row)
                sig_label.setStyleSheet("font-size:10px;color:#9aa4b2;background:transparent;")
                sig_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                sig_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                text_box.addWidget(sig_label)
            rl.addLayout(text_box, 1)
            self.contact_list.addItem(item)
            self.contact_list.setItemWidget(item, row)
        remaining = len(all_authors) - len(authors)
        self.contact_more_btn.setVisible(remaining > 0)
        if remaining > 0:
            self.contact_more_btn.setText(f"加载更多（还有 {remaining} 个作者）")
        self.contact_title.setText(f"通讯录 · 手动同步（{len(all_authors)} 位作者）")
        self.contacts_btn.setText(f"通讯录（{len(all_authors)}）")
        if not reset:
            # clear() 会把滚动条归零；等布局更新后恢复用户原来的位置。
            QTimer.singleShot(
                0, lambda value=old_scroll:
                self._restore_list_scroll(self.contact_list, value))

    def _delete_author(self, sec_uid, nickname):
        """从通讯录移除一位作者；不删除已下载作品和作者资料。"""
        if not sec_uid:
            return
        if self.syncing:
            QMessageBox.information(
                self, "删除作者", "正在同步通讯录，请先点「停止同步」再删除作者。")
            return
        answer = QMessageBox.question(
            self, "删除作者",
            f"确定从通讯录删除「{nickname}」吗？\n\n"
            "只删除通讯录记录，不会删除已下载作品、历史记录和作者资料。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_author(sec_uid)
        # 同步失败重试队列里也要移除，避免重试按钮继续统计这位作者。
        self._sync_failed_objs = [
            a for a in getattr(self, "_sync_failed_objs", [])
            if getattr(a, "sec_uid", None) != sec_uid
        ]
        self._save_failed_authors()
        self._update_retry_button()
        # 保留当前加载页数和滚动位置；reset=True 会清空列表并跳回顶部。
        self._render_contacts(reset=False)
        self.contact_status.setText(
            f"已从通讯录删除：{nickname}（已下载作品保留）")

    # ---------- 独立窗口 ----------
    def _show_window(self, window):
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_contacts_window(self):
        self._show_window(self.contacts_window)

    def _open_history_window(self):
        self._render_history(reset=self.history_page <= 1)
        self._show_window(self.history_window)

    def _open_transfer_window(self):
        self._show_window(self.transfer_window)

    def _open_sync_log_window(self):
        self.sync_log_window.reload()
        self._show_window(self.sync_log_window)

    def _open_download_log_window(self):
        self.download_log_window.reload()
        self._show_window(self.download_log_window)

    # ---------- 传输列表 ----------
    def _panel_for(self, key):
        return self.upload_panel if str(key).startswith("up-") else self.download_panel

    def _transfer_add(self, key, text):
        self._panel_for(key).add_item(key, text)

    def _transfer_update(self, key, status):
        self._panel_for(key).update_item(key, status)

    def _clear_transfers(self):
        self.download_panel.clear_items()
        self.upload_panel.clear_items()

    def _dl_author_label(self, index, work):
        """下载任务对应的作者显示名（同步用通讯录作者，批量用作品自带作者）。"""
        if self._dl_mode != "batch":
            try:
                author = self._dl_authors[index]
            except (IndexError, AttributeError):
                author = None
            if author is not None:
                return author.label()
        return work.safe_author()

    def _dl_key(self, index):
        """传输列表里的任务行 key。

        以前用 `id(self._dl_jobs)` 拼：每轮同步都会换成新 list，CPython 很容易
        把同一个地址复用给下一个 list，于是新一轮的任务会写进上一轮同 key 的行，
        新任务干脆不显示。改成显式轮次号 + 索引。
        """
        return f"dl-{self._dl_run}-{index}"

    def _dl_item_started(self, index):
        job = self._dl_job(index)
        if job is None:
            return
        work = job[0]
        key = self._dl_key(index)
        self._transfer_add(key, f"{work.safe_author()} · {work.display_title()[:42]}")
        self._transfer_update(key, "下载中…")
        self.download_panel.set_item_progress(key, 0)
        # 「下载日志」：任务一开始就记一条
        download_log.append_start(
            self._dl_author_label(index, work), work.display_title()[:50])

    def _dl_item_progress(self, index, percent):
        if self._dl_mode is None or self._dl_job(index) is None:
            return
        try:
            value = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return
        self._dl_progress[index] = value
        self.download_panel.set_item_progress(self._dl_key(index), value)

    # ---------- 历史 ----------
    # WebDAV 上传在 webdav_panel.WebdavMixin，通讯录同步在
    # contacts_sync.ContactsSyncMixin。
    def _render_history(self, reset=True):
        if reset:
            self.history_page = 1
        old_scroll = (self.history_list.verticalScrollBar().value()
                      if not reset else 0)
        self.history_list.clear()
        all_items = self.store.load_history()
        items = all_items[: self.history_page * 80]
        self.history_title.setText(f"最近保存（{len(all_items)}）")
        self.history_btn.setText(f"最近保存（{len(all_items)}）")
        remaining = len(all_items) - len(items)
        # 先把「加载更多」的显隐定下来：以前清空历史走的是下面那个提前 return，
        # 按钮会保留上一轮的「加载更多（还有 N 个）」并且还能点。
        self.history_more_btn.setVisible(remaining > 0)
        if remaining > 0:
            self.history_more_btn.setText(f"加载更多（还有 {remaining} 个作品）")
        if not items:
            empty = QListWidgetItem("还没有保存记录：解析并保存后会显示在这里")
            empty.setForeground(Qt.GlobalColor.gray)
            self.history_list.addItem(empty)
            return
        for item in items:
            if item.is_album:
                label = f"【图集】{item.title[:42]}  ·  {item.author}"
            else:
                label = f"【视频】{item.title[:42]}  ·  {item.author}"
            entry = QListWidgetItem(label)
            entry.setToolTip(item.video_path or (item.album_paths[0] if item.album_paths else item.title))
            entry.setData(Qt.ItemDataRole.UserRole, item.aweme_id)
            self.history_list.addItem(entry)
        if not reset:
            QTimer.singleShot(
                0, lambda value=old_scroll:
                self._restore_list_scroll(self.history_list, value))

    def _open_download_dir(self):
        # 新作品都在 <下载根>/净影/作者_id/作品id/ 下；旧数据仍在 Videos 里，
        # 从这里的上一级就能看到两者。
        path = os.path.join(config.SAVE_ROOT)
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)
        except OSError:
            QMessageBox.information(self, "下载目录", path)

    def _load_more_contacts(self):
        self.contact_page += 1
        self._render_contacts(reset=False)

    def _restore_list_scroll(self, widget, value):
        bar = widget.verticalScrollBar()
        bar.setValue(min(max(0, int(value)), bar.maximum()))

    def _load_more_history(self):
        self.history_page += 1
        self._render_history(reset=False)

    @staticmethod
    def _find_history_entry(items, aweme_id, row=-1):
        """按作品 id 找历史条目；行号只兜底「没有 id 的老记录」。

        列表渲染完到用户双击之间，后台同步可能已插入新作品，`row` 会整体
        错位（以前直接 `items[row]`，双击第 5 行会打开别人的作品目录）。
        `QListWidgetItem` 的 UserRole 里存着 aweme_id，用它查才稳定。
        """
        if aweme_id:
            for entry in items:
                if entry.aweme_id == aweme_id:
                    return entry
            return None
        if 0 <= row < len(items) and not items[row].aweme_id:
            return items[row]
        return None

    def _open_history_item(self, item):
        if item is None:
            return
        items = self.store.load_history()
        row = self.history_list.row(item)
        aweme_id = item.data(Qt.ItemDataRole.UserRole)
        entry = self._find_history_entry(items, aweme_id, row)
        path = ""
        if entry is not None:
            path = entry.video_path or (entry.album_paths[0] if entry.album_paths else "")
        if path and os.path.exists(path):
            try:
                os.startfile(os.path.dirname(path))
                return
            except OSError:
                pass
        QMessageBox.information(self, "提示", "文件可能已被移动或删除。")

    def _clear_history(self):
        self.store.clear_history()
        self._render_history()

    # ---------- 工具 ----------
    @staticmethod
    def _fmt_ms(ms):
        import time as t
        return t.strftime("%m-%d %H:%M", t.localtime(ms / 1000))

    def closeEvent(self, event):
        self._ui_watchdog_stop = True
        if getattr(self, "_ui_heartbeat_timer", None) is not None:
            self._ui_heartbeat_timer.stop()
        for name in ("contacts_window", "history_window", "transfer_window",
                     "sync_log_window", "download_log_window"):
            window = getattr(self, name, None)
            if window is not None:
                try:
                    window.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.dm.stop()
        except Exception:  # noqa: BLE001
            pass
        # 原画质回捞用的独立 Chromium 会话，关窗时释放（atexit 还有一层兜底）
        try:
            original_stream.close_session()
        except Exception:  # noqa: BLE001
            pass
        # 远端画质探测用的连接池：关窗时一并释放，并把缓存落盘
        try:
            self._close_probe_client()
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "_auto_uploader", None) is not None:
            try:
                self._auto_uploader.blockSignals(True)
                self._auto_uploader.shutdown(2.0)
            except Exception:  # noqa: BLE001
                pass
            self._auto_uploader = None
        try:
            # 退出前必须确认 authors.json 落盘（后台写线程要等它写完）
            self.store.flush(block=True)
        except Exception:  # noqa: BLE001
            pass
        # 登录用的是独立 threading.Thread（不在 _workers 里）：置位让它结束
        # 900 秒的扫码轮询，否则关窗后浏览器还会一直开着。
        login_stop = getattr(self, "_login_stop", None)
        if login_stop is not None:
            try:
                login_stop.set()
            except Exception:  # noqa: BLE001
                pass
        # 协作式退出：先通知全部后台线程，让它们在检查点跑完 finally/with、
        # 关掉 Playwright context、把浏览器目录锁还回去，再等它们自然结束。
        # 以前是 3 秒一到无差别 terminate()，线程可能正停在写 .part 或持锁处。
        running = [w for w in list(getattr(self, "_workers", [])) if w.isRunning()]
        for worker in running:
            try:
                worker.request_stop()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + 3.0
        for worker in running:
            if worker.isRunning():
                worker.wait(max(0, int((deadline - time.time()) * 1000)))
        # 仅对「已通知 + 已等待仍无响应」的线程保留强制结束，并写进日志。
        # 正常情况下这个循环一条都不会走到。
        for worker in running:
            if worker.isRunning():
                import logging
                logging.warning("后台线程未响应停止请求，强制结束：%s",
                                getattr(worker, "task", worker))
                worker.terminate()
                worker.wait(500)
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


def _cleanup_old_crash_logs(data_dir, keep=5):
    """回收每个进程一个的 crash-<pid>.log（绝大多数是 0 字节的空壳）。

    带 pid 的文件名是特意用来避开「固定 crash.log 被别的进程/云同步占住」
    的，但没有任何回收机制，长期运行会越堆越多。只删明确命名、且是空文件
    或超过 7 天的旧日志；当前进程和最近 keep 份一律保留。
    """
    import glob
    try:
        entries = []
        for path in glob.glob(os.path.join(data_dir, "crash-*.log")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            entries.append((st.st_mtime, path))
    except OSError:
        return
    entries.sort(reverse=True)
    current = f"crash-{os.getpid()}.log"
    now = time.time()
    for mtime, path in entries[keep:]:
        if os.path.basename(path) == current:
            continue
        try:
            if now - mtime < 7 * 86400 and os.path.getsize(path) > 0:
                continue
            os.remove(path)   # 别的实例正打开着会失败，忽略即可
        except OSError:
            pass


def _log_file_handler(path):
    """app.log 的写入器：按 2 MB 轮转、保留 3 份。"""
    from logging.handlers import RotatingFileHandler
    return RotatingFileHandler(path, maxBytes=APP_LOG_MAX_BYTES,
                               backupCount=APP_LOG_BACKUPS, encoding="utf-8")


def _setup_logging():
    """启动日志：写到数据目录，程序异常时可用于排查。

    app.log 按 2 MB 轮转、保留 3 份：以前是纯 FileHandler，日志只增不减，
    长期使用后长到十几 MB（本机实测 12 MB），排查问题时反而更难翻。
    """
    import logging
    config.ensure_dirs()
    try:
        import faulthandler
        # 崩溃日志用带进程号的独立文件：固定的 crash.log 一旦被别的进程/
        # 云同步占住，open(..., "a") 会一直等，程序看起来就是「双击没反应」。
        crash_path = os.path.join(config.DATA_DIR, f"crash-{os.getpid()}.log")
        crash_stream = open(crash_path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(crash_stream, all_threads=True)
        globals()["_CRASH_STREAM"] = crash_stream
        _cleanup_old_crash_logs(config.DATA_DIR)
    except (OSError, ValueError):
        pass
    log_path = os.path.join(config.DATA_DIR, "app.log")
    handlers = []
    try:
        handlers.append(_log_file_handler(log_path))
    except OSError:
        # app.log 被占用时不要让整个程序起不来，换一个本次运行专用的日志
        try:
            log_path = os.path.join(config.DATA_DIR, f"app-{os.getpid()}.log")
            handlers.append(_log_file_handler(log_path))
        except OSError:
            log_path = ""
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=handlers,
        )
    return log_path


def run_selftest():
    """无界面自检：链接解析 / 真实抓取 / 音乐下载 / 目录结构。"""
    import tempfile
    import traceback
    import logging

    def out(msg):
        logging.info(msg)
        try:
            print(msg)
        except Exception:  # noqa: BLE001
            pass

    results = []

    def check(name, func):
        try:
            detail = func()
            results.append((name, True, detail))
            out(f"[PASS] {name}: {detail}")
        except Exception as e:  # noqa: BLE001
            results.append((name, False, str(e)))
            out(f"[FAIL] {name}: {e}")
            out(traceback.format_exc())

    def test_links():
        assert links.extract_aweme_id("https://www.douyin.com/video/7525082444551310602") \
            == "7525082444551310602"
        sec = "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE"
        assert links.extract_sec_uid(f"https://www.douyin.com/user/{sec}") == sec
        assert links.extract_sec_uid(sec) == sec
        return "ok"

    def test_parse_video():
        info = parser.resolve_video_sync("https://www.douyin.com/video/7525082444551310602")
        assert info.aweme_id, "no aweme id"
        assert info.play_url, "no play url"
        return (f"{info.aweme_id} / {info.author} / {info.quality} / "
                f"{info.width}x{info.height} / music={bool(info.music_url)}")

    def test_download_music():
        from models import MediaInfo
        info = MediaInfo()
        info.aweme_id = "selftest"
        info.title = "自检"
        info.music_url = "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/7525082559013718822.mp3"
        old_download, old_music = config.DOWNLOAD_BASE, config.MUSIC_BASE
        tmp = tempfile.mkdtemp(prefix="jingying-selftest-")
        try:
            config.DOWNLOAD_BASE = os.path.join(tmp, "Videos")
            config.MUSIC_BASE = os.path.join(tmp, "Music")
            # 只测音乐：临时伪造视频路径不可行，直接调内部下载函数
            target = os.path.join(config.MUSIC_BASE, "音乐.mp3")
            size = downloader._download_to_file(info.music_url, target, None, 0, 1)
            assert size > 10000 and os.path.exists(target)
            return f"{size} bytes"
        finally:
            config.DOWNLOAD_BASE, config.MUSIC_BASE = old_download, old_music
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_video():
        info = parser.resolve_video_sync("https://www.douyin.com/video/7525082444551310602")
        old = (config.DOWNLOAD_BASE, config.MUSIC_BASE, config.SAVE_ROOT)
        tmp = tempfile.mkdtemp(prefix="jingying-dl-")
        try:
            config.DOWNLOAD_BASE = os.path.join(tmp, "Videos")
            config.MUSIC_BASE = os.path.join(tmp, "Music")
            # 2026-11-09 起作品落在 <下载根>/净影/...，用的是 SAVE_ROOT。
            # 只改 DOWNLOAD_BASE 会把自检文件真的下到用户的净影目录里，
            # 而且下面那条「散文件没清掉」的断言指向空目录、恒为真。
            config.SAVE_ROOT = os.path.join(tmp, config.FOLDER_NAME)
            author_folder = downloader.build_author_folder(info.safe_author(), info.unique_id)
            work_folder = downloader.build_work_folder(info, info.safe_author())
            res = downloader.download_work(info, author_folder, work_folder)
            # 新规则：一个作品一个文件夹，视频 / 音乐 / 文案 / 图片都在里面，
            # archive_path 恒为空，不能再按老 tar 结构断言。
            saved = [p for p in ([res.video_path, res.music_path, res.caption_path]
                                 + list(res.album_paths)) if p and os.path.isfile(p)]
            assert len(saved) >= 2, f"下载文件太少: {saved}"
            folders = {os.path.normcase(os.path.dirname(os.path.abspath(p)))
                       for p in saved}
            assert len(folders) == 1, f"作品文件没有落在同一个文件夹: {folders}"
            folder = next(iter(folders))
            assert os.path.commonpath([os.path.abspath(tmp), folder]) == os.path.abspath(tmp), \
                "自检写到了临时目录之外"
            assert os.path.isfile(os.path.join(folder, "文案.txt")), "缺少文案.txt"
            total = sum(os.path.getsize(p) for p in saved)
            assert total > 100_000, f"下载内容太小: {total}"
            return f"files={len(saved)} size={total} folder={os.path.basename(folder)}"
        finally:
            config.DOWNLOAD_BASE, config.MUSIC_BASE, config.SAVE_ROOT = old
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    check("链接解析", test_links)
    check("真实作品抓取", test_parse_video)
    check("音乐下载", test_download_music)
    check("完整下载(视频+音乐+文案)", test_download_video)

    failed = [r for r in results if not r[1]]
    out("\n%s" % ("SELFTEST PASS" if not failed else f"SELFTEST FAIL ({len(failed)})"))
    return 0 if not failed else 1


def main():
    config.ensure_dirs()
    _setup_logging()
    import logging
    logging.info("startup: args=%s", sys.argv)
    logging.info("version=%s build=%s", config.APP_VERSION, config.build_sha() or "dev")
    logging.info("chrome=%s data=%s", config.CHROME_PATH, config.DATA_DIR)

    # 多账号并发时后台线程（Playwright + 作品解析）会争用 GIL；缩短线程切换
    # 间隔，让主线程更及时拿到执行权，界面响应更跟手（默认 5ms，这里 1ms）。
    try:
        sys.setswitchinterval(0.001)
    except Exception:  # noqa: BLE001
        pass

    if "--selftest" in sys.argv:
        sys.exit(run_selftest())

    # 高 DPI 屏幕（125%/150% 缩放）下避免界面右侧被裁切
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    _apply_tooltip_theme(app)
    try:
        window = MainWindow()
        window.show()
        logging.info("window shown")
        sys.exit(app.exec())
    except Exception:
        logging.exception("fatal error")
        raise


if __name__ == "__main__":
    main()