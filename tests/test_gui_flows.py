# -*- coding: utf-8 -*-
"""GUI 流程测试：批量下载、通讯录同步、批量导入（不联网，替换下载与解析）。"""
import os
import re
import threading
import time
import unittest
from unittest import mock

from tests.support import isolated_config

import config
import download_log
import downloader
import models
import parser
import store as store_mod
import sync_log
import webdav


SEC_UID = "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE"


def _work(index):
    info = models.MediaInfo()
    info.aweme_id = str(7525082444551310600 + index)
    info.title = f"测试作品{index}"
    info.author = "测试作者"
    info.unique_id = "uid1"
    info.author_id = SEC_UID
    info.play_url = "https://v11.douyinvod.com/test.mp4"
    info.create_time = 1752069813 + index
    return info


def _fake_download(info, author_folder, work_folder, progress=None, cancel_event=None,
                   byte_callback=None, pack_tar=False):
    result = downloader.DownloadResult()
    # 必须和 downloader.work_dir 的真实实现一致：2026-11-09 起作品目录用
    # 作品 id 命名，而不是 build_work_folder() 传进来的「作者_文案_时间」。
    # 以前这里直接拼 work_folder，导致「上传后删除」用例检查的是一个根本
    # 不存在的目录，断言恒真，也让 _work_backed_up 的凭证永远对不上。
    base = downloader.work_dir(author_folder, work_folder, info)
    os.makedirs(base, exist_ok=True)
    if info.is_album():
        for i in range(len(info.image_urls)):
            path = os.path.join(base, f"图片_{i + 1:02d}.jpg")
            with open(path, "wb") as f:
                f.write(b"jpeg")
            if byte_callback:
                byte_callback(4)
            result.album_paths.append(path)
    else:
        path = os.path.join(base, "视频.mp4")
        with open(path, "wb") as f:
            f.write(b"mp4")
        if byte_callback:
            byte_callback(3)
        result.video_path = path
    if progress:
        progress(100, 0, 1)
    result.count = 1
    return result


class GuiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        import app
        self.app = app
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self._old_check = app.MainWindow._check_login
        self._old_maint = app.MainWindow._start_maintenance
        app.MainWindow._check_login = lambda self: None
        app.MainWindow._start_maintenance = lambda self: None
        self.window = app.MainWindow()
        # patcher 是 _patch 对象，没有 call_count；真正记录调用次数的是
        # start() 返回的 MagicMock。以前第二轮的「不该重新下载」断言会直接
        # 抛 AttributeError（被更早的失败挡住，一直没被发现）。
        self._dl_patcher = mock.patch.object(downloader, "download_work",
                                             side_effect=_fake_download)
        self.patch_download = self._dl_patcher.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._dl_patcher.stop()
        self.window.close()
        self.window.deleteLater()
        self.qa.processEvents()
        self.window = None
        self.app.MainWindow._check_login = self._old_check
        self.app.MainWindow._start_maintenance = self._old_maint
        self._ctx.__exit__(None, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.qa.processEvents()

    def _wait(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.qa.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.qa.processEvents()
        return predicate()

    def _profile(self, count=3):
        profile = parser.ProfileInfo()
        profile.sec_uid = SEC_UID
        profile.nickname = "测试作者"
        profile.unique_id = "uid1"
        profile.videos = [_work(i) for i in range(count)]
        return profile

    def test_batch_download_flow(self):
        self.window._show_batch(self._profile(3))
        self.window._batch_download()
        self.assertEqual(self.window._dl_mode, "batch")
        self.assertTrue(self._wait(lambda: self.window._dl_mode is None))
        self.assertEqual(self.window._dl_done, 3)
        self.assertEqual(self.window._dl_success, 3)
        self.assertTrue(self.window.parse_btn.isEnabled())
        self.assertEqual(self.window.batch_progress.value(), 100)
        self.assertEqual(len(self.window.store.load_history()), 3)
        self.assertTrue(os.path.exists(config.HISTORY_DB))
        self.assertEqual(self.window.download_panel.list.count(), 3)
        self.assertEqual(self.window.history_list.count(), 3)
        self.window._update_transfer_stats()
        self.assertEqual(self.window.download_panel.progress.value(), 100)
        self.assertIn("完成", self.window.download_panel.detail_label.text())
        # 「下载日志」批量下载：打开作者 + 3 个作品各自的「开始 / 完成」
        dl_lines = download_log.load()
        self.assertEqual(len(dl_lines), 7)
        self.assertTrue(any(
            line.endswith("用 账号1 打开了 测试作者，找到了 3 个作品")
            for line in dl_lines))
        self.assertEqual(sum("开始下载" in line for line in dl_lines), 3)
        self.assertEqual(sum("下载完成" in line for line in dl_lines), 3)
        self.assertTrue(all("测试作者" in line for line in dl_lines))
        self.assertEqual(sum("已全部下完" in line for line in dl_lines), 1)
        self.assertTrue(any("还剩 2 个没下完" in line for line in dl_lines))

    def test_sync_all_flow(self):
        author = store_mod.Author(SEC_UID, "测试作者", "uid1", "")
        self.window.store.add_author(author)
        profile = self._profile(2)
        profile.total_works = 5

        def fake_resolve(sec_uid, stage=None, **kwargs):
            return profile

        with mock.patch.object(parser, "resolve_profile", side_effect=fake_resolve):
            self.window._sync_all()
            self.assertTrue(self.window.syncing)
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))

        self.assertEqual(len(self.window.store.load_history()), 2)
        self.assertIn("全部同步完成", self.window.contact_status.text())
        self.assertEqual(self.window._sync_total_new, 2)
        self.assertEqual(self.window._sync_total_fail, 0)
        self.assertIn("已发现 测试作者", self.window.sync_log.text())
        self.assertIn("2 个作品", self.window.sync_log.text())
        self.assertIn("主页显示 5 个", self.window.sync_log.text())
        # 「查看日志」：整体开始 → 作者开始 → 作者完成 → 整体结束
        lines = sync_log.load()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(
            re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line)
            for line in lines))
        self.assertTrue(any("开始同步，共 1 位作者" in line for line in lines))
        self.assertTrue(any("用 账号1 开始同步 测试作者·uid1" in line for line in lines))
        self.assertTrue(any(
            "使用 账号1 同步了 测试作者·uid1 的 2 个作品" in line
            for line in lines))
        self.assertTrue(any("全部同步完成：成功 2 个作品" in line for line in lines))
        authors = self.window.store.load_authors()
        self.assertEqual(len(authors[0].saved_ids), 2)
        # 「下载日志」：先记用哪个账号打开了作者、找到多少作品，再逐个记下载
        dl_lines = download_log.load()
        self.assertEqual(len(dl_lines), 5)
        self.assertTrue(any(
            line.endswith("用 账号1 打开了 测试作者·uid1，找到了 2 个作品")
            for line in dl_lines))
        self.assertEqual(sum("开始下载" in line for line in dl_lines), 2)
        self.assertEqual(sum("下载完成" in line for line in dl_lines), 2)
        self.assertTrue(all("测试作者·uid1" in line for line in dl_lines))
        # 每条日志都要带时间前缀
        self.assertTrue(all(
            re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", line)
            for line in dl_lines))
        self.assertTrue(any("还剩 1 个没下完" in line for line in dl_lines))
        self.assertTrue(any("已全部下完" in line for line in dl_lines))
        self.assertNotIn("下载失败", "\n".join(dl_lines))

    def test_sync_continues_while_download_blocks(self):
        sec2 = "MS4wLjABAAAAsecond-author-for-parallel-sync-test"
        self.window.store.add_author(store_mod.Author(SEC_UID, "作者一", "uid1", ""))
        self.window.store.add_author(store_mod.Author(sec2, "作者二", "uid2", ""))
        resolve_calls = []
        download_started = threading.Event()
        release_download = threading.Event()

        def resolve(sec_uid, stage=None, **kwargs):
            resolve_calls.append(sec_uid)
            profile = parser.ProfileInfo()
            profile.sec_uid = sec_uid
            profile.nickname = "作者一" if sec_uid == SEC_UID else "作者二"
            profile.unique_id = "uid1" if sec_uid == SEC_UID else "uid2"
            profile.videos = [_work(0 if sec_uid == SEC_UID else 100)]
            return profile

        def slow_download(info, author_folder, work_folder, **kwargs):
            download_started.set()
            release_download.wait(8)
            return _fake_download(info, author_folder, work_folder, **kwargs)

        with mock.patch.object(parser, "resolve_profile", side_effect=resolve), \
                mock.patch.object(downloader, "download_work", side_effect=slow_download):
            self.window._sync_all()
            self.assertTrue(self._wait(lambda: download_started.is_set(), timeout=8))
            # 第一个作品还在下载时，第二位作者的主页应该已经被抓取
            self.assertTrue(self._wait(lambda: len(resolve_calls) >= 2, timeout=8))
            self.assertTrue(self.window.syncing)
            release_download.set()
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))

        self.assertEqual(len(resolve_calls), 2)
        self.assertEqual(len(self.window.store.load_history()), 2)

    def test_sync_uploads_to_webdav_in_parallel(self):
        uploaded = []

        class FakeDavClient:
            def __init__(self, *args, **kwargs):
                pass

            def upload_file(self, local_path, rel_path, on_bytes=None):
                with open(local_path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        if on_bytes:
                            on_bytes(len(chunk))
                uploaded.append(rel_path)

        self.window.store.add_author(store_mod.Author(SEC_UID, "测试作者", "uid1", ""))
        self.window._webdav = {"url": "http://fake", "remote_root": "净影"}
        self.window.webdav_auto_cb.setChecked(True)
        profile = self._profile(2)

        with mock.patch.object(parser, "resolve_profile",
                               side_effect=lambda sec_uid, stage=None, **kwargs: profile), \
                mock.patch.object(webdav, "WebDAVClient", FakeDavClient):
            self.window._sync_all()
            self.assertTrue(self.window.syncing)
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))
            self.assertTrue(self._wait(lambda: not self.window._upload_active, timeout=10))

        self.assertEqual(len(uploaded), 2)
        # 2026-11-09 起作品保存在 <下载根>/净影/<作者>/<作品id>/，上传相对路径随之变化
        self.assertTrue(all(path.startswith("净影/") for path in uploaded))
        self.assertEqual(self.window.upload_panel.list.count(), 2)

    def test_open_player_builds_feed_from_history(self):
        """播放器入口（PlayerMixin）：从历史组装视频 + 图集信息流并打开播放器。"""
        video = os.path.join(self.tmp, "v.mp4")
        with open(video, "wb") as f:
            f.write(b"x")
        images = []
        for i in range(2):
            path = os.path.join(self.tmp, f"i{i}.jpg")
            with open(path, "wb") as f:
                f.write(b"x")
            images.append(path)

        single = store_mod.HistoryItem()
        single.aweme_id = "1"
        single.title = "视频作品"
        single.author = "测试作者"
        single.video_path = video
        single.create_time = 1700000000
        album = store_mod.HistoryItem()
        album.aweme_id = "2"
        album.title = "图集作品"
        album.author = "测试作者"
        album.album_paths = images
        album.is_album = True
        album.create_time = 1700000100
        self.window.store.add_history(single)
        self.window.store.add_history(album)

        self.window._open_player()
        self.assertTrue(self._wait(lambda: self.window.player is not None, timeout=10))
        feed = self.window.player.feed
        self.assertEqual(len(feed), 2)
        self.assertEqual({entry["type"] for entry in feed}, {"video", "album"})
        # 信息流按发布时间倒序，图集在前
        self.assertEqual(feed[0]["aweme_id"], "2")
        self.assertEqual(len(feed[0]["images"]), 2)
        self.window.player.close()

    def test_download_progress_keeps_author_name(self):
        author = store_mod.Author(SEC_UID, "测试作者", "uid1", "")
        work = _work(0)
        result = downloader.DownloadResult()
        result.video_path = os.path.join(self.tmp, "fake.mp4")
        self.window._sync_current = author
        self.window._sync_queue = [author]
        self.window._sync_index = 1
        self.window._dl_mode = "sync"
        self.window._dl_jobs = [(work, "author", "work")]
        self.window._dl_authors = [author]
        self.window._dl_total = 1
        self.window._dl_done = 0
        self.window._dl_item_done(0, result)
        self.assertIn("正在同步 测试作者", self.window.contact_status.text())

    def test_failed_authors_persist_across_restart(self):
        author = store_mod.Author(SEC_UID, "失败作者", "uid1", "")
        self.window._record_sync_failure(author, "临时失败")
        self.window._sync_failed_objs = []
        self.window._load_failed_authors()
        self.assertEqual([a.sec_uid for a in self.window._sync_failed_objs], [SEC_UID])

    def test_retry_failed_authors_only_retries_those(self):
        author = store_mod.Author(SEC_UID, "失败作者", "uid1", "")
        self.window._record_sync_failure(author, "临时失败")
        self.assertFalse(self.window.retry_failed_btn.isHidden())
        self.assertIn("重试失败(1)", self.window.retry_failed_btn.text())
        calls = []

        def resolve(sec_uid, stage=None, **kwargs):
            calls.append(sec_uid)
            p = parser.ProfileInfo()
            p.sec_uid = sec_uid
            p.nickname = "失败作者"
            p.unique_id = "uid1"
            p.videos = [_work(0)]
            return p

        with mock.patch.object(parser, "resolve_profile", side_effect=resolve):
            self.window._retry_failed_authors()
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))

        self.assertEqual(calls, [SEC_UID])
        self.assertTrue(self.window.retry_failed_btn.isHidden())

    def test_sync_shuffle_avoids_same_first_author(self):
        authors = [
            store_mod.Author(f"MS4wLjABAAAAorder{i}", f"作者{i}", f"uid{i}", "")
            for i in range(3)
        ]
        self.window._last_sync_first = authors[0].sec_uid
        order = self.window._shuffle_sync_authors(authors)
        self.assertEqual({a.sec_uid for a in order}, {a.sec_uid for a in authors})
        self.assertNotEqual(order[0].sec_uid, authors[0].sec_uid)

    def test_two_logged_accounts_alternate_for_sync(self):
        secs = [f"MS4wLjABAAAAaccount{i}-alternate-test" for i in range(5)]
        self.window._account_logged = [True] * 5
        self.window._logged_in = True
        for i, sec in enumerate(secs):
            self.window.store.add_author(
                store_mod.Author(sec, f"作者{i}", f"uid{i}", ""))
        calls = []

        def resolve(sec_uid, stage=None, **kwargs):
            calls.append(dict(kwargs))
            p = parser.ProfileInfo()
            p.sec_uid = sec_uid
            p.nickname = f"作者{secs.index(sec_uid)}"
            p.unique_id = f"uid{secs.index(sec_uid)}"
            p.videos = [_work(secs.index(sec_uid) * 100)]
            return p

        with mock.patch.object(parser, "resolve_profile", side_effect=resolve):
            self.window._sync_all()
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))

        self.assertEqual(len(calls), 5)
        used = [call.get("session_dir") for call in calls]
        self.assertEqual(set(used), set(config.SESSION_DIRS[:5]))
        for i in range(5):
            self.assertEqual(used.count(config.SESSION_DIRS[i]), 1)
            self.assertIn(config.COOKIE_FILES[i],
                          [call.get("cookie_file") for call in calls])

    def test_sync_runs_authors_concurrently_across_accounts(self):
        """两个账号都登录时，同步应并行抓取不同作者，而不是串行等一个完成。"""
        self.window._account_logged = [True, True] + [False] * 3
        self.window._logged_in = True
        secs = ["MS4wLjABAAAAconcurrent-one", "MS4wLjABAAAAconcurrent-two"]
        for i, sec in enumerate(secs):
            self.window.store.add_author(
                store_mod.Author(sec, f"并发作者{i}", f"cuid{i}", ""))

        started = []
        release = threading.Event()
        lock = threading.Lock()

        def resolve(sec_uid, stage=None, **kwargs):
            with lock:
                started.append(sec_uid)
            # 卡住第一位作者的浏览器：若同步仍是串行，第二位不可能开始；
            # 两位都能进入这里，说明确实是并发抓取。
            release.wait(8)
            idx = secs.index(sec_uid)
            p = parser.ProfileInfo()
            p.sec_uid = sec_uid
            p.nickname = f"并发作者{idx}"
            p.unique_id = f"cuid{idx}"
            p.videos = [_work(idx * 10)]
            return p

        with mock.patch.object(parser, "resolve_profile", side_effect=resolve):
            self.window._sync_all()
            self.assertTrue(self._wait(lambda: len(started) >= 2, timeout=8))
            release.set()
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))

        self.assertEqual(len(started), 2)
        self.assertEqual(len(self.window.store.load_history()), 2)

    def test_import_authors_text(self):
        text = "https://www.douyin.com/user/" + SEC_UID
        self.window._import_authors_text(text, "测试导入")
        authors = self.window.store.load_authors()
        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0].sec_uid, SEC_UID)
        self.assertIn("新增 1 个", self.window.contact_status.text())

    def test_delete_author_only_removes_contact(self):
        author = store_mod.Author(SEC_UID, "待删除作者", "uid1", "")
        self.window.store.add_author(author)
        self.assertEqual(len(self.window.store.load_authors()), 1)
        with mock.patch.object(
                self.app.QMessageBox, "question",
                return_value=self.app.QMessageBox.StandardButton.Yes):
            self.window._delete_author(SEC_UID, "待删除作者")
        self.assertEqual(self.window.store.load_authors(), [])

    def test_repair_caption_metadata(self):
        self.window.store.add_author(store_mod.Author(SEC_UID, "作者", "uid1", ""))
        work = os.path.join(config.DOWNLOAD_BASE, "作者_uid1", "作品_20250101_010101")
        os.makedirs(work, exist_ok=True)
        caption = os.path.join(work, "文案.txt")
        with open(caption, "w", encoding="utf-8") as f:
            f.write("作者：作者\n抖音号：未知\n\n文案：\n\n")
        item = store_mod.HistoryItem()
        item.aweme_id = "123"
        item.author = "作者"
        item.title = "真正的文案"
        item.video_path = os.path.join(work, "视频.mp4")
        self.window.store.add_history(item)
        self.window._repair_caption_metadata()
        text = open(caption, encoding="utf-8").read()
        self.assertIn("抖音号：uid1", text)
        self.assertIn("真正的文案", text)

    def test_delete_author_keeps_contacts_scroll_and_page(self):
        # 作者列表在独立窗口里，只有窗口显示后列表才有真实高度和滚动范围
        self.window._open_contacts_window()
        self.qa.processEvents()
        for i in range(60):
            self.window.store.add_author(
                store_mod.Author(f"SEC{i:03d}", f"作者{i}", "", ""))
        self.window._load_more_contacts()
        self.qa.processEvents()
        self.assertEqual(self.window.contact_page, 2)
        bar = self.window.contact_list.verticalScrollBar()
        bar.setValue(min(20, bar.maximum()))
        with mock.patch.object(
                self.app.QMessageBox, "question",
                return_value=self.app.QMessageBox.StandardButton.Yes):
            self.window._delete_author("SEC000", "作者0")
        self.assertEqual(self.window.contact_page, 2)
        self.assertEqual(len(self.window.store.load_authors()), 59)
        self.assertTrue(self._wait(lambda: bar.value() > 0, timeout=3))

    def test_load_more_contacts_keeps_scroll_position(self):
        self.window._open_contacts_window()
        self.qa.processEvents()
        for i in range(60):
            self.window.store.add_author(
                store_mod.Author(f"SEC{i:03d}", f"作者{i}", "", ""))
        self.window._render_contacts()
        self.qa.processEvents()
        bar = self.window.contact_list.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(min(20, bar.maximum()))
        before = bar.value()
        self.window._load_more_contacts()
        self.assertTrue(self._wait(lambda: bar.value() > 0, timeout=3))
        self.assertGreaterEqual(bar.value(), min(before, bar.maximum()))

    def test_upload_delete_media_keeps_metadata(self):
        base = os.path.join(config.DOWNLOAD_BASE, "作者", "作品")
        os.makedirs(base, exist_ok=True)
        media = os.path.join(base, "视频.mp4")
        caption = os.path.join(base, "文案.txt")
        for path, data in ((media, b"mp4"), (caption, "作者：测试".encode())):
            with open(path, "wb") as f:
                f.write(data)
        music_dir = os.path.join(config.MUSIC_BASE, "作者", "作品")
        os.makedirs(music_dir, exist_ok=True)
        music = os.path.join(music_dir, "音乐.mp3")
        with open(music, "wb") as f:
            f.write(b"mp3")
        avatar_dir = os.path.join(os.path.dirname(config.DOWNLOAD_BASE),
                                  "作者资料", "authors", "SEC")
        os.makedirs(avatar_dir, exist_ok=True)
        avatar = os.path.join(avatar_dir, "avatar.jpg")
        with open(avatar, "wb") as f:
            f.write(b"jpg")

        with mock.patch.object(
                self.app.QMessageBox, "question",
                return_value=self.app.QMessageBox.StandardButton.Yes):
            self.window.webdav_delete_cb.setChecked(True)
        self.window._delete_uploaded_media("Videos/作者/作品/视频.mp4")
        self.window._delete_uploaded_media("Music/作者/作品/音乐.mp3")
        self.window._delete_uploaded_media("Videos/作者/作品/文案.txt")
        self.window._delete_uploaded_media("作者资料/authors/SEC/avatar.jpg")

        self.assertFalse(os.path.exists(media))
        self.assertTrue(os.path.exists(caption))
        self.assertFalse(os.path.exists(music))
        self.assertTrue(os.path.exists(avatar))
        self.assertEqual(
            self.window._webdav_remote_rel("Videos/作者/作品/视频.mp4"),
            "Videos/作者/作品/视频.mp4")
        self.assertEqual(
            self.window._webdav_local_rel("Videos/作者/作品/视频.mp4"),
            "Videos/作者/作品/视频.mp4")

    def test_upload_then_delete_skips_redownload(self):
        """上传后删本地媒体：文件真的删掉，且下一轮同步不会重新下载。"""
        uploaded = []

        class FakeDavClient:
            def __init__(self, *args, **kwargs):
                pass

            def upload_file(self, local_path, rel_path, on_bytes=None):
                with open(local_path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        if on_bytes:
                            on_bytes(len(chunk))
                uploaded.append(rel_path)

        self.window.store.add_author(store_mod.Author(SEC_UID, "测试作者", "uid1", ""))
        self.window._webdav = {"url": "http://fake", "remote_root": "净影"}
        self.window.webdav_auto_cb.setChecked(True)
        with mock.patch.object(
                self.app.QMessageBox, "question",
                return_value=self.app.QMessageBox.StandardButton.Yes):
            self.window.webdav_delete_cb.setChecked(True)
        profile = self._profile(2)
        author_folder = downloader.build_author_folder("测试作者", "uid1")

        def sync_once():
            with mock.patch.object(
                    parser, "resolve_profile",
                    side_effect=lambda sec_uid, stage=None, **kwargs: profile), \
                    mock.patch.object(webdav, "WebDAVClient", FakeDavClient):
                self.window._sync_all()
                self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=20))
                self.assertTrue(
                    self._wait(lambda: not self.window._upload_active, timeout=10))

        sync_once()
        self.assertEqual(len(uploaded), 2)
        # 视频已从本地删除（目录里没有别的文件，作品目录也一并清掉）
        for index in range(2):
            work_dir = os.path.join(config.SAVE_ROOT, author_folder,
                                    str(_work(index).aweme_id))
            self.assertFalse(
                os.path.exists(os.path.join(work_dir, "视频.mp4")),
                f"{work_dir} 里的视频没有被删除")
        # 上传记录里留下了「已备份」凭证
        self.assertTrue(self.window._work_backed_up(
            author_folder, (str(_work(0).aweme_id), "", "")))

        # 第二轮同步：本地没有媒体文件，但云端已有备份，不能再下一遍
        downloads = self.patch_download.call_count
        sync_once()
        self.assertEqual(self.patch_download.call_count, downloads,
                         "已上传的作品被重新下载了")

    def test_sync_failure_recovers_and_can_retry(self):
        self.window.store.add_author(store_mod.Author(SEC_UID, "测试作者", "uid1", ""))
        calls = []

        def fail(sec_uid, stage=None, **kwargs):
            calls.append(sec_uid)
            raise parser.ParseError("网络超时，读取失败")

        with mock.patch.object(parser, "resolve_profile", side_effect=fail), \
                mock.patch.object(self.app.QMessageBox, "warning", return_value=None):
            self.window._sync_all()
            self.assertTrue(self.window.syncing)
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=8))

        # 同一个作者会先自动重试一次，再记为失败并允许重新点「全部同步」
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.window.contact_sync_btn.text(), "全部同步")
        self.assertIn("失败", self.window.contact_status.text())
        self.assertTrue(self._wait(lambda: self.window.worker is None, timeout=2))

        with mock.patch.object(
                parser, "resolve_profile",
                side_effect=lambda sec_uid, stage=None, **kwargs: self._profile(1)), \
                mock.patch.object(self.app.QMessageBox, "warning", return_value=None):
            self.window._sync_all()
            self.assertTrue(self.window.syncing)
            self.assertTrue(self._wait(lambda: not self.window.syncing, timeout=8))
        self.assertEqual(len(self.window.store.load_history()), 1)

    def test_album_batch_flow(self):
        album = models.MediaInfo()
        album.aweme_id = "7525082444551319999"
        album.title = "图集作品"
        album.author = "测试作者"
        album.unique_id = "uid1"
        album.author_id = SEC_UID
        album.image_urls = ["https://p3.douyinpic.com/1.jpg", "https://p3.douyinpic.com/2.jpg"]
        album.image_videos = ["", ""]
        album.create_time = 1752069813
        profile = parser.ProfileInfo()
        profile.sec_uid = SEC_UID
        profile.nickname = "测试作者"
        profile.unique_id = "uid1"
        profile.albums = [album]
        self.window._show_batch(profile)
        self.window._batch_download()
        self.assertTrue(self._wait(lambda: self.window._dl_mode is None))
        items = self.window.store.load_history()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].is_album)
        self.assertEqual(len(items[0].album_paths), 2)


if __name__ == "__main__":
    unittest.main()
