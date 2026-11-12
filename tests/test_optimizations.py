# -*- coding: utf-8 -*-
"""2026-11-12 优化回归：日志不冻界面、探针不堆积、历史定位、上传顺序与节流等。"""
import os
import threading
import time
import unittest
from types import SimpleNamespace

from tests.support import isolated_config

import app
import archive
import config
import line_log
import links
import webdav
import webdav_panel

SEC_UID = "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE"


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        # 卡住退避是模块级状态，用例之间不能互相影响
        line_log._STALL_UNTIL = 0.0

    def tearDown(self):
        line_log._STALL_UNTIL = 0.0
        self._ctx.__exit__(None, None, None)


class LineLogTimeoutTests(IsolatedTests):
    def test_flush_times_out_instead_of_freezing_gui(self):
        """写盘被外部卡住时，日志窗口的 load() 不能无限等 Queue.join()。"""
        original = line_log._write_lines
        release = threading.Event()

        def blocked(path, lines):
            release.wait(1.0)
            original(path, lines)

        line_log._write_lines = blocked
        try:
            path = os.path.join(self.tmp, "stalled.log")
            line_log.append(path, "hello")
            started = time.monotonic()
            self.assertFalse(line_log.flush(timeout=0.05))
            self.assertLess(time.monotonic() - started, 0.5,
                            "限时等待不能退化成阻塞等待")
        finally:
            release.set()
            line_log._write_lines = original
            line_log.flush()   # 让全局写线程恢复，避免污染后续用例

    def test_load_skips_when_write_lock_is_held(self):
        """写盘线程卡住时会一直持有 _LOCK；等锁也必须限时。"""
        line_log.flush()   # 先把队列清空，排除 flush 本身的等待
        path = os.path.join(self.tmp, "locked.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old\n")
        self.assertTrue(line_log._LOCK.acquire(timeout=1.0))
        try:
            started = time.monotonic()
            self.assertIsNone(line_log.load(path, 100))
            self.assertFalse(line_log.clear(path))
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(os.path.exists(path))
        finally:
            line_log._LOCK.release()

    def test_load_backs_off_while_writer_is_stalled(self):
        """卡住后的冷却期内直接跳过刷新，主线程不能被反复拖住。"""
        line_log._STALL_UNTIL = time.monotonic() + 5.0
        try:
            path = os.path.join(self.tmp, "cooldown.log")
            started = time.monotonic()
            self.assertIsNone(line_log.load(path, 100))
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            line_log._STALL_UNTIL = 0.0

    def test_flush_without_timeout_still_waits_for_all_lines(self):
        path = os.path.join(self.tmp, "normal.log")
        for i in range(5):
            line_log.append(path, f"line-{i}")
        self.assertTrue(line_log.flush())
        self.assertEqual(line_log.pending(), 0)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 5)


class ProbeCleanupTests(IsolatedTests):
    def test_probe_name_is_stable_across_calls(self):
        folder = os.path.join(self.tmp, "probe_dir")
        self.assertEqual(config._pick_dir([folder]), folder)
        config._pick_dir([folder])
        self.assertEqual(os.listdir(folder), [".write_probe"])

    def test_old_probes_removed_but_fresh_and_user_files_kept(self):
        stale = time.time() - 3 * 3600
        old = os.path.join(config.DATA_DIR, ".write_probe_1234_1700000000")
        temp_old = os.path.join(config.TEMP_DIR, ".jingying_probe_4321")
        fresh = os.path.join(config.DATA_DIR, ".write_probe_9999")
        user_file = os.path.join(config.DATA_DIR, "notes.tmp")
        for path in (old, temp_old, fresh, user_file):
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
        for path in (old, temp_old, user_file):
            os.utime(path, (stale, stale))
        removed = config.cleanup_stale_temp_files(max_age_s=3600)
        self.assertGreaterEqual(removed, 2)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(temp_old))
        self.assertTrue(os.path.exists(fresh), "刚写的探针不能被删")
        self.assertTrue(os.path.exists(user_file), "用户文件不能被误删")


class HistoryLookupTests(unittest.TestCase):
    @staticmethod
    def _entry(aweme_id, path):
        return SimpleNamespace(aweme_id=aweme_id, video_path=path,
                               album_paths=[])

    def test_row_shift_does_not_open_someone_elses_work(self):
        items = [self._entry("A", "/a"), self._entry("B", "/b"),
                 self._entry("C", "/c")]
        # 列表渲染完后后台又插入了两条新作品，C 从第 3 行挪到第 5 行
        shifted = [self._entry("X", "/x"), self._entry("Y", "/y")] + items
        found = app.MainWindow._find_history_entry(shifted, "C", row=2)
        self.assertEqual(found.video_path, "/c")

    def test_missing_id_returns_none(self):
        items = [self._entry("A", "/a")]
        self.assertIsNone(app.MainWindow._find_history_entry(items, "ZZZ", row=0))

    def test_legacy_entry_without_id_falls_back_to_row(self):
        items = [self._entry("", "/legacy")]
        found = app.MainWindow._find_history_entry(items, None, row=0)
        self.assertEqual(found.video_path, "/legacy")


class UploadOrderTests(IsolatedTests):
    def test_priority_derives_from_upload_dirs(self):
        roots = [os.path.join(self.tmp, name)
                 for name in ("净影", "Videos", "Music", "作者资料")]
        priority = webdav._upload_priority(roots)
        self.assertLess(priority["净影"], priority["Music"])
        self.assertLess(priority["净影"], priority["作者资料"])
        self.assertLess(priority["Videos"], priority["Music"])

    def test_jingying_videos_uploaded_before_music(self):
        order = []
        client = webdav.WebDAVClient("http://fake")
        client.upload_file = lambda path, rel, on_bytes=None: order.append(rel)
        for rel, data in (("净影/作者/作品1/视频.mp4", b"video"),
                          ("Music/作者/音乐.mp3", b"music"),
                          ("作者资料/作者/头像.jpg", b"avatar")):
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        roots = [os.path.join(self.tmp, name)
                 for name in ("净影", "Music", "作者资料")]
        ok, _skipped, failed = client.upload_tree(
            self.tmp, roots=roots, max_workers=1)
        self.assertEqual(ok, 3)
        self.assertEqual(failed, [])
        self.assertEqual(order[0], "净影/作者/作品1/视频.mp4")
        self.assertLess(order.index("净影/作者/作品1/视频.mp4"),
                        order.index("Music/作者/音乐.mp3"))
        client.close()

    def test_close_closes_created_sessions(self):
        client = webdav.WebDAVClient("http://fake")
        session = client._session()
        closed = []
        session.close = lambda: closed.append(1)
        client.close()
        self.assertEqual(closed, [1])
        self.assertEqual(client._sessions, [])


class AutoUploadStateTests(unittest.TestCase):
    def test_throttles_writes_and_force_flushes(self):
        writes = []
        fake = SimpleNamespace(
            _auto_uploader=SimpleNamespace(
                state_snapshot=lambda: {"server": "s", "root": "r", "files": {}}),
            _webdav_state_lock=threading.Lock(),
            _save_webdav_state=lambda state: writes.append(state),
        )
        webdav_panel.WebdavMixin._save_auto_state(fake)
        webdav_panel.WebdavMixin._save_auto_state(fake)
        self.assertEqual(len(writes), 1, "20 秒内的重复落盘应该被节流")
        webdav_panel.WebdavMixin._save_auto_state(fake, force=True)
        self.assertEqual(len(writes), 2, "任务收尾必须强制落盘一次")


class ArchiveCompleteTests(IsolatedTests):
    def _make_tar(self):
        work = os.path.join(self.tmp, "作者_文案_20261108_101010")
        os.makedirs(work)
        for name, data in (("视频.mp4", b"v" * 100), ("文案.txt", b"hello")):
            with open(os.path.join(work, name), "wb") as f:
                f.write(data)
        tar_path = os.path.join(self.tmp, "Videos", "作者_uid", "123.tar")
        archive.pack(work, tar_path)
        return tar_path

    def test_complete_marker_is_written(self):
        tar_path = self._make_tar()
        cache = os.path.join(self.tmp, "cache")
        result = archive.extract(tar_path, cache)
        self.assertTrue(os.path.isfile(result["video"]))
        self.assertTrue(os.path.isfile(
            os.path.join(result["dir"], archive._COMPLETE_MARKER)))

    def test_half_extracted_cache_is_repaired(self):
        tar_path = self._make_tar()
        cache = os.path.join(self.tmp, "cache")
        folder = archive.extract(tar_path, cache)["dir"]
        # 模拟逐个 move 中途被强杀：删掉标记和一个文件
        os.remove(os.path.join(folder, archive._COMPLETE_MARKER))
        os.remove(os.path.join(folder, "文案.txt"))
        again = archive.extract(tar_path, cache)
        self.assertTrue(os.path.isfile(again["caption"]),
                        "半个缓存不能被当成完整缓存")
        self.assertTrue(os.path.isfile(
            os.path.join(folder, archive._COMPLETE_MARKER)))

    def test_legacy_cache_without_marker_is_accepted(self):
        tar_path = self._make_tar()
        cache = os.path.join(self.tmp, "cache")
        folder = archive.extract(tar_path, cache)["dir"]
        os.remove(os.path.join(folder, archive._COMPLETE_MARKER))
        self.assertTrue(os.path.isfile(archive.extract(tar_path, cache)["caption"]))
        self.assertTrue(os.path.isfile(
            os.path.join(folder, archive._COMPLETE_MARKER)), "应补写标记")


class LinksDedupeTests(unittest.TestCase):
    def test_dedupe_keeps_order(self):
        text = (f"https://www.douyin.com/user/{SEC_UID}\n"
                f"{SEC_UID}\n{SEC_UID}\n")
        self.assertEqual(links.extract_all_sec_uids(text), [SEC_UID])
        self.assertEqual(links.extract_all_sec_uids(""), [])


class CrashLogCleanupTests(IsolatedTests):
    def test_old_empty_crash_logs_are_removed(self):
        stale = time.time() - 10 * 86400
        for i in range(8):
            path = os.path.join(self.tmp, f"crash-{1000 + i}.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            os.utime(path, (stale, stale))
        current = os.path.join(self.tmp, f"crash-{os.getpid()}.log")
        with open(current, "w", encoding="utf-8") as f:
            f.write("current")
        app._cleanup_old_crash_logs(self.tmp, keep=2)
        self.assertTrue(os.path.exists(current), "当前进程的崩溃日志必须保留")
        left = [n for n in os.listdir(self.tmp) if n.startswith("crash-")]
        self.assertLessEqual(len(left), 3)


if __name__ == "__main__":
    unittest.main()
