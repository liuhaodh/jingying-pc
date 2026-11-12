# -*- coding: utf-8 -*-
"""播放器入口：把已下载作品整理成信息流，打开抖音式竖屏播放器。

以 mixin 的形式挂在 MainWindow 上：读取历史/通讯录、组装 feed、创建
PlayerWindow，并处理播放器里「同步」按钮回传的单条作品。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

import archive
import downloader
import parser
from player import PlayerWindow
from workers import Worker


class PlayerMixin:
    """播放器入口：组装已下载信息流并管理播放器窗口。

    宿主（MainWindow）需要提供：
    - `store`（历史/通讯录）、`status` 状态栏、`player` 播放器引用
    - `_run_worker` / `_track_worker`（起后台线程）、`_sync_note`
    """

    def _open_player(self):
        if getattr(self, "player", None) is not None:
            self.player.show()
            self.player.raise_()
            self.player.activateWindow()
            return
        if getattr(self, "_player_loading", False):
            return
        self._player_loading = True
        self.status.setText("正在整理已下载列表…")

        def task(stage=None):
            authors = self.store.load_authors()
            avatar_by_sec = {
                a.sec_uid: a.avatar_local
                for a in authors if a.sec_uid and a.avatar_local
            }
            avatar_by_uid = {
                a.unique_id: a.avatar_local
                for a in authors if a.unique_id and a.avatar_local
            }
            name_avatars = {}
            for a in authors:
                if a.nickname and a.avatar_local:
                    name_avatars.setdefault(a.nickname, []).append(a.avatar_local)
            # 同名的不同作者不能按昵称硬匹配，否则头像会串；只保留唯一昵称。
            avatar_by_name = {
                name: paths[0] for name, paths in name_avatars.items()
                if name and len(paths) == 1
            }
            feed = []
            for item in self.store.load_history():
                # 旧历史记录可能没有 sec_uid/unique_id；空字符串绝不能作为
                # 字典 key 去查头像，否则会命中第一个“空 ID 作者”的头像。
                avatar = ""
                if item.author_sec_uid:
                    avatar = avatar_by_sec.get(item.author_sec_uid, "")
                elif item.unique_id:
                    avatar = avatar_by_uid.get(item.unique_id, "")
                else:
                    avatar = avatar_by_name.get(item.author, "")
                if item.is_album:
                    images = [p for p in item.album_paths if p and os.path.exists(p)]
                    if not images:
                        continue
                    entry = {
                        "type": "album",
                        "images": images,
                        "videos": item.album_videos,
                        "title": item.title,
                        "author": item.author,
                        "music": item.music_path if item.music_path and os.path.exists(item.music_path) else "",
                        "time": item.create_time,
                        "location": item.location,
                        "ip": item.ip_location,
                        "aweme_id": item.aweme_id,
                        "avatar": avatar,
                    }
                elif item.video_path and os.path.exists(item.video_path):
                    entry = {
                        "type": "video",
                        "path": item.video_path,
                        "title": item.title,
                        "author": item.author,
                        "time": item.create_time,
                        "location": item.location,
                        "ip": item.ip_location,
                        "aweme_id": item.aweme_id,
                        "avatar": avatar,
                    }
                else:
                    continue
                # 2026-11-08 起作品是一个 tar：这里只把包的路径交给播放器，
                # 真正解包在切到这一条时才做（见 PlayerWindow._materialize），
                # 否则打开播放器就要把整个信息流解压一遍。
                primary = item.video_path or (item.album_paths[0] if item.album_paths else "")
                if archive.is_archive(primary):
                    entry["archive"] = primary
                    if item.is_album:
                        entry["images"] = []
                        entry["videos"] = []
                        entry["music"] = ""
                    else:
                        entry["path"] = primary
                feed.append(entry)
            feed.sort(key=lambda entry: int(entry.get("time") or 0), reverse=True)
            return feed

        worker = Worker(task)
        worker.done.connect(self._player_feed_ready)
        worker.failed.connect(self._player_feed_failed,
                              Qt.ConnectionType.QueuedConnection)
        self._player_worker = self._track_worker(worker)
        worker.start()

    def _player_feed_failed(self, message):
        self._player_loading = False
        self.status.setText("")
        QMessageBox.warning(self, "观看已下载", message or "读取已下载列表失败")

    def _player_feed_ready(self, feed):
        self._player_loading = False
        self.status.setText("")
        if not feed:
            QMessageBox.information(
                self, "提示",
                "还没有已下载的作品。先保存视频或图集后再来观看吧。")
            return
        self.player = PlayerWindow(feed, on_sync=self._sync_work_by_id)
        self.player.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.player.destroyed.connect(self._player_destroyed)
        self.player.show()
        self.player.raise_()
        self.player.activateWindow()

    def _player_destroyed(self):
        self.player = None

    def _sync_work_by_id(self, feed_item):
        """播放器列表里单个作品的“同步”按钮：重新解析并下载该作品。"""
        aweme_id = feed_item.get("aweme_id")
        if not aweme_id:
            QMessageBox.information(self, "同步", "这条记录缺少作品 ID，无法单独同步。")
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "同步", "已有任务在进行，请稍后再试。")
            return

        def task(with_stage=None):
            stage = with_stage or (lambda t: None)
            stage(f"正在同步作品 {aweme_id}…")
            info = parser.resolve_video_sync("https://www.douyin.com/video/" + aweme_id)
            author_name = info.safe_author()
            author_folder = downloader.build_author_folder(author_name, info.unique_id)
            work_folder = downloader.build_work_folder(info, author_name)
            result = downloader.download_work(info, author_folder, work_folder)
            # 更新历史记录（只更新这一条，别为一条记录全量重写整个历史）
            items = self.store.load_history()
            changed = None
            for entry in items:
                if entry.aweme_id == aweme_id:
                    entry.video_path = result.video_path
                    entry.music_path = result.music_path
                    entry.album_paths = result.album_paths
                    entry.album_videos = list(getattr(result, "live_paths", []))
                    entry.is_album = info.is_album()
                    entry.create_time = info.create_time
                    entry.location = info.location
                    entry.ip_location = info.ip_location
                    entry.title = info.display_title()
                    entry.author = author_name
                    entry.author_sec_uid = info.author_id
                    entry.unique_id = info.unique_id
                    changed = entry
                    break
            if changed is not None:
                self.store.update_history_items([changed])
            return result

        def done(result):
            self.status.setText("单个作品同步完成")
            self._render_history()
            QMessageBox.information(
                self, "同步完成",
                "作品已重新同步。\n作品包：{}".format(
                    getattr(result, "archive_path", "") or "无"))

        worker = Worker(task)
        worker.done.connect(lambda r: done(r),
                            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(
            lambda msg: QMessageBox.warning(self, "同步失败", msg or "同步失败"),
            Qt.ConnectionType.QueuedConnection)
        self.worker = self._track_worker(worker)
        worker.start()

