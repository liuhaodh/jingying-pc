# -*- coding: utf-8 -*-
"""2026-11-13 优化回归。

覆盖：换源重试不拼接旧 .part、迟到 UI 回调有 context、头像缓存 LRU、
上传队列异常补账、画质缓存并发安全、cookies 句柄与发版快照连接回收。
全部离线，不访问真实网络/抖音页面。
"""
import ast
import json
import os
import sqlite3
import threading
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import isolated_config

from PySide6.QtCore import Qt

import app
import auto_upload
import build_release
import downloader
import models
import parser
import remote_quality
import transfer


class _IsolatedTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)


# ---------- 1. 换源重试不能按 Range 续传旧 .part ----------

class _FakeResponse:
    def __init__(self, body, url, status=200):
        self.history = []
        self.status_code = status
        self.url = url
        self.headers = {"Content-Length": str(len(body))}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self._body


class _FakeSession:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.headers_seen = []

    def get(self, url, headers=None, **_kwargs):
        self.headers_seen.append(dict(headers or {}))
        return _FakeResponse(self.body, url, self.status)


class ResumeOnRetryTests(_IsolatedTests):
    def _info(self):
        info = models.MediaInfo()
        info.aweme_id = "7525082444551310602"
        info.title = "测试作品"
        info.author = "作者"
        info.unique_id = "uid1"
        info.play_url = "http://127.0.0.1/a.mp4"
        info.create_time = 1752069813
        return info

    def test_retry_after_new_url_starts_from_zero(self):
        """第一次原片直链失败、重新回捞换了 URL 后，不能再续传旧 .part。"""
        calls = []

        def fake_download(url, target, *args, **kwargs):
            calls.append((url, kwargs.get("resume")))
            if len(calls) == 1:
                # 模拟 CDN 中途断掉，留下半截 .part
                with open(target + ".part", "wb") as handle:
                    handle.write(b"HALF-ORIGINAL")
                raise IOError("直链失效")
            return 4321

        urls = [("http://127.0.0.1/original.mp4", True),
                ("http://127.0.0.1/transcoded.mp4", False)]
        with mock.patch.object(downloader, "_video_stream_url", side_effect=urls), \
             mock.patch.object(downloader, "_download_to_file", side_effect=fake_download), \
             mock.patch.object(downloader.logging, "warning"):
            result = downloader.download_work(self._info(), "作者_uid1", "作品id")

        self.assertEqual(result.size, 4321)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][1], True, "第一次仍要保留断点续传")
        self.assertIs(calls[1][1], False, "换了 URL 的重试必须从头覆盖写")
        self.assertEqual(calls[1][0], "http://127.0.0.1/transcoded.mp4")

    def test_resume_false_overwrites_stale_part_without_range(self):
        target = os.path.join(self.tmp, "视频.mp4")
        with open(target + ".part", "wb") as handle:
            handle.write(b"STALE-BYTES" * 100)
        session = _FakeSession(b"FRESH-CONTENT")
        with mock.patch.object(downloader, "_get_session", return_value=session), \
             mock.patch.object(downloader, "_is_allowed_host", return_value=True), \
             mock.patch.object(downloader, "_set_file_times"):
            size = downloader._download_to_file(
                "http://127.0.0.1/a.mp4", target, None, 0, 1, resume=False)
        self.assertEqual(size, len(b"FRESH-CONTENT"))
        self.assertNotIn("Range", session.headers_seen[0])
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"FRESH-CONTENT")

    def test_resume_true_still_asks_for_range(self):
        """回归保护：正常重试/续传语义不能被改坏。"""
        target = os.path.join(self.tmp, "b.mp4")
        stale = b"OLD" * 100
        with open(target + ".part", "wb") as handle:
            handle.write(stale)
        session = _FakeSession(b"TAIL", status=206)
        with mock.patch.object(downloader, "_get_session", return_value=session), \
             mock.patch.object(downloader, "_is_allowed_host", return_value=True), \
             mock.patch.object(downloader, "_set_file_times"):
            downloader._download_to_file(
                "http://127.0.0.1/b.mp4", target, None, 0, 1)
        self.assertEqual(session.headers_seen[0].get("Range"),
                         f"bytes={len(stale)}-")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), stale + b"TAIL")


# ---------- 2. 迟到的 UI 回调必须挂在窗口生命周期上 ----------

class DeferredCallbackTests(unittest.TestCase):
    def test_all_single_shot_calls_have_context(self):
        """app.py 里 QTimer.singleShot 必须传 self，窗口销毁后回调才会被取消。"""
        source = Path(app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "singleShot"
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "QTimer"]
        self.assertTrue(calls, "app.py 里应该有 singleShot 调用")
        for node in calls:
            self.assertGreaterEqual(
                len(node.args), 3,
                f"app.py:{node.lineno} 的 QTimer.singleShot 缺少 context，"
                f"窗口销毁后迟到回调会访问已删除的控件")

    def test_history_scroll_restore_timer_parented_to_window(self):
        """历史列表「加载更多」的滚动恢复 timer 要以窗口为 parent。"""
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])

        entry = SimpleNamespace(is_album=False, title="作品", author="作者",
                                video_path="x.mp4", album_paths=[], aweme_id="id1")
        fake = mock.MagicMock()
        fake.history_page = 1
        fake.history_list.verticalScrollBar.return_value.value.return_value = 17
        fake.store.load_history.return_value = [entry]
        timer = mock.MagicMock()
        with mock.patch.object(app, "QTimer", return_value=timer) as timer_cls:
            app.MainWindow._render_history(fake, reset=False)
        timer_cls.assert_called_once_with(fake)
        timer.setSingleShot.assert_called_once_with(True)
        timer.start.assert_called_once_with(0)
        timer.timeout.connect.assert_called_once()


# ---------- 3. 头像缓存：只淘汰最久没用的，不再整表清空 ----------

class AvatarCacheTests(unittest.TestCase):
    def test_cache_uses_lru_instead_of_full_clear(self):
        host = SimpleNamespace(_avatar_cache=OrderedDict())
        for i in range(200):
            app.MainWindow._cache_avatar(host, ("p", str(i), 44), f"pix{i}")
        self.assertEqual(len(host._avatar_cache), 200)

        app.MainWindow._cache_avatar(host, ("p", "200", 44), "pix200")
        self.assertEqual(len(host._avatar_cache), 200, "上限仍是 200")
        self.assertNotIn(("p", "0", 44), host._avatar_cache)
        # 旧实现到 201 个会 clear()，这里 199 个旧头像必须还在
        self.assertIn(("p", "1", 44), host._avatar_cache)
        self.assertIn(("p", "199", 44), host._avatar_cache)

        # 命中过的 key 要挪到最近使用端，不会被下一轮淘汰
        app.MainWindow._cache_avatar(host, ("p", "1", 44), "pix1")
        app.MainWindow._cache_avatar(host, ("p", "201", 44), "pix201")
        self.assertIn(("p", "1", 44), host._avatar_cache)
        self.assertNotIn(("p", "2", 44), host._avatar_cache)


# ---------- 4. 上传 worker 意外崩溃要补账 ----------

class AutoUploadCrashTests(_IsolatedTests):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.qa.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_handle_crash_is_logged_and_accounted(self):
        path = os.path.join(self.tmp, "a.mp4")
        with open(path, "wb") as handle:
            handle.write(b"a" * 4096)
        uploader = auto_upload.AutoUploader(
            mock.MagicMock(), self.tmp, state={}, max_workers=1)
        finished = []
        uploader.finished.connect(lambda *values: finished.append(values))
        with mock.patch.object(uploader, "_handle", side_effect=RuntimeError("boom")), \
             mock.patch.object(auto_upload.logging, "exception"):
            uploader.start()
            self.assertEqual(uploader.enqueue([path]), 1)
            uploader.close()
            self.assertTrue(self._wait(lambda: bool(finished)), "队列必须能收尾")
        self.assertEqual(uploader._failed, 1)
        self.assertEqual(uploader._done, 4096, "进度条不能永远差这一格")
        self.assertEqual(finished[0], (0, 0, 1))


# ---------- 5. 画质缓存的并发加载/落盘 ----------

class RemoteQualityCacheTests(_IsolatedTests):
    def test_concurrent_remember_keeps_every_entry(self):
        cache_path = os.path.join(self.tmp, "remote_quality.json")
        probe = remote_quality.RemoteQualityProbe(
            None, cache_path=cache_path, namespace="server-a")

        def work(i):
            probe._remember(f"a/{i}.mp4", 100 + i, 1920, 1080)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(120)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(probe._load_cache()), 120,
                         "并发首次加载不能互相覆盖、丢掉刚记住的分辨率")
        probe.save(force=True)
        with open(cache_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data["files"]), 120)

    def test_concurrent_save_leaves_no_tmp_file(self):
        cache_path = os.path.join(self.tmp, "remote_quality.json")
        probe = remote_quality.RemoteQualityProbe(
            None, cache_path=cache_path, namespace="server-a")

        def work(i):
            probe._remember(f"b/{i}.mp4", 100 + i, 1280, 720)
            probe.save(force=True)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        leftovers = [name for name in os.listdir(os.path.dirname(cache_path))
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "临时文件按线程区分，不能互相踩或残留")
        with open(cache_path, encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle).get("files"), dict)


# ---------- 6. cookies 句柄 / 发版 SQLite 连接 ----------

class ResourceHandleTests(_IsolatedTests):
    def test_load_saved_cookies_closes_file(self):
        path = os.path.join(self.tmp, "cookies.json")
        opener = mock.mock_open(read_data='[{"name": "sid", "value": "1"}, {"bad": 1}]')
        with mock.patch("builtins.open", opener):
            cookies = parser.load_saved_cookies(path)
        self.assertEqual(cookies, [{"name": "sid", "value": "1"}])
        handle = opener.return_value.__enter__.return_value
        self.assertTrue(handle.__exit__.called,
                        "必须用 with 打开，Windows 上不能一直占着 cookies.json")

    def test_copy_db_commits_and_closes_both_connections(self):
        src = Path(self.tmp) / "history.db"
        dst = Path(self.tmp) / "snapshot.db"
        source = sqlite3.connect(src)
        source.execute("CREATE TABLE t (v TEXT)")
        source.execute("INSERT INTO t VALUES ('hello')")
        source.commit()
        source.close()

        real_connect = sqlite3.connect
        created = []

        def tracked(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            created.append(conn)
            return conn

        with mock.patch.object(build_release.sqlite3, "connect", side_effect=tracked):
            build_release._copy_db(src, dst)

        check = real_connect(dst)
        try:
            self.assertEqual(check.execute("SELECT v FROM t").fetchall(), [("hello",)],
                             "换成 closing 后仍要提交事务，快照不能是空库")
        finally:
            check.close()
        self.assertEqual(len(created), 2)
        for conn in created:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


# ---------- 7. 传输面板满 60 行后仍要滚动显示新任务 ----------

class TransferPanelEvictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def test_new_tasks_replace_oldest_instead_of_being_dropped(self):
        panel = transfer.TransferPanel("下载")
        total = transfer.MAX_VISIBLE_ROWS + 20
        try:
            for i in range(total):
                panel.add_item(f"job-{i}", f"作品 {i}")
            self.qa.processEvents()
            self.assertEqual(panel.list.count(), transfer.MAX_VISIBLE_ROWS)
            self.assertLessEqual(len(panel._rows), transfer.MAX_VISIBLE_ROWS)
            keys = [panel.list.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(panel.list.count())]
            newest = f"job-{total - 1}"
            self.assertEqual(keys[0], newest, "最新任务要插在最上面")
            self.assertIn(newest, panel._rows)
            self.assertNotIn("job-0", keys, "最老的任务应被裁剪掉")
            self.assertNotIn("job-0", panel._rows)
        finally:
            panel.clear_items()
            panel.deleteLater()
            self.qa.processEvents()

    def test_full_list_reuses_row_widgets(self):
        """真实长同步回归：满 60 行后不能再每个任务都新建/销毁 QWidget。

        曾有一版「插入新行 + deleteLater 回收旧行」跑真实通讯录同步时，
        主线程 UI stall 57s，两小时后 Qt C 层段错误退出（退出码 139）。
        """
        from PySide6.QtWidgets import QWidget
        panel = transfer.TransferPanel("下载")
        try:
            for i in range(transfer.MAX_VISIBLE_ROWS):
                panel.add_item(f"k{i}", f"作品 {i}")
            baseline = len(panel.list.viewport().findChildren(QWidget))
            with mock.patch.object(panel, "_build_row",
                                   wraps=panel._build_row) as build:
                for i in range(2000):
                    panel.add_item(f"n{i}", f"作品 {i}")
                self.qa.processEvents()
            self.assertEqual(build.call_count, 0,
                             "列表满后必须复用行控件，不能每个任务都造一套")
            self.assertEqual(panel.list.count(), transfer.MAX_VISIBLE_ROWS)
            self.assertEqual(len(panel._rows), transfer.MAX_VISIBLE_ROWS)
            self.assertEqual(len(panel.list.viewport().findChildren(QWidget)),
                             baseline, "不能积累孤儿/待删除控件")
            keys = [panel.list.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(panel.list.count())]
            self.assertEqual(keys[0], "n1999")
            self.assertNotIn("k0", panel._rows)
        finally:
            panel.clear_items()
            panel.deleteLater()
            self.qa.processEvents()


# ---------- 8. 登录等待：不自旋、取消不干等、迟到结果不串轮 ----------

class LoginWaitTests(_IsolatedTests):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def _fake_window(self):
        fake = SimpleNamespace()
        fake.syncing = False
        fake._account_count = 1
        fake._login_running = False
        fake._login_result = None
        fake._login_generation = 0
        fake.account_combo = mock.MagicMock()
        fake.account_combo.currentIndex.return_value = 0
        fake.login_btn = mock.MagicMock()
        fake.status = mock.MagicMock()
        fake.login_signal = mock.MagicMock()
        fake._set_login_state = mock.MagicMock()
        return fake

    def test_stale_login_signal_does_not_leak_into_next_round(self):
        fake = self._fake_window()
        fake._login_generation = 3
        app.MainWindow._login_done(fake, 2, "")
        self.assertIsNone(fake._login_result, "上一轮的迟到信号必须被丢弃")
        app.MainWindow._login_done(fake, 3, "")
        self.assertEqual(fake._login_result, "")

    def test_cancel_returns_without_waiting_for_the_10s_spin(self):
        fake = self._fake_window()
        cancel_btn = object()
        box = mock.MagicMock()
        box.addButton.side_effect = [mock.MagicMock(), cancel_btn]
        box.clickedButton.return_value = cancel_btn
        release = threading.Event()

        def blocking_login(*_args, **_kwargs):
            release.wait(2.0)

        with mock.patch.object(app.config, "CHROME_PATH", "fake.exe"), \
             mock.patch.object(app, "QMessageBox", return_value=box), \
             mock.patch.object(parser, "login_sync", side_effect=blocking_login):
            started = time.monotonic()
            app.MainWindow._do_login(fake)
            elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 1.0, "点「取消」不能干等满 10 秒")
        self.assertFalse(fake._login_running)
        self.assertTrue(fake.login_btn.setEnabled.called)
        self.assertEqual(fake.status.setText.call_args_list[-1].args[0],
                         "已取消登录")

    def test_no_busy_wait_spin_left_in_app(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        spins = [node.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.While)
                 and "processEvents" in ast.dump(node)
                 and "sleep" in ast.dump(node)]
        self.assertEqual(spins, [],
                         "登录确认要用 QEventLoop + 定时器，不能主线程自旋")


# ---------- 9. 自动上传状态锁的懒初始化 ----------

class AutoStateSaveLockTests(unittest.TestCase):
    def test_first_concurrent_save_shares_one_lock(self):
        import webdav_panel

        fake = SimpleNamespace(
            _auto_uploader=mock.MagicMock(),
            _webdav_state_lock=threading.Lock(),
            _save_webdav_state=mock.MagicMock(),
        )
        errors = []
        barrier = threading.Barrier(8)

        def work():
            try:
                barrier.wait(timeout=3)
                webdav_panel.WebdavMixin._save_auto_state(fake, force=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertIsNotNone(getattr(fake, "_auto_state_save_lock", None))
        self.assertIsInstance(fake._auto_state_last_save, float)
        self.assertEqual(fake._save_webdav_state.call_count, 1,
                         "首次并发不能各自建锁、把 10MB 记录写好几遍")
