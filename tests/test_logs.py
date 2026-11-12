# -*- coding: utf-8 -*-
"""两个日志文件：后台写盘、读取前落盘、超过上限裁剪、残留临时文件清理。"""
import os
import time
import unittest

from tests.support import isolated_config

import config
import download_log
import sync_log


class LogWriterTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        sync_log.clear()
        download_log.clear()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_sync_log_lines_land_before_load(self):
        sync_log.append_event("开始同步，共 1 位作者", when="2026-11-07 10:00:00")
        sync_log.append_start("账号1", "作者甲", when="2026-11-07 10:00:01")
        sync_log.append("账号1", "作者甲", 3, when="2026-11-07 10:00:05")
        # load() 会先等后台把队列落盘，读到的内容必须完整
        lines = sync_log.load()
        self.assertEqual(lines, [
            "[2026-11-07 10:00:00] 开始同步，共 1 位作者",
            "[2026-11-07 10:00:01] 用 账号1 开始同步 作者甲",
            "[2026-11-07 10:00:05] 使用 账号1 同步了 作者甲 的 3 个作品",
        ])

    def test_sync_log_is_trimmed_to_limit(self):
        limit = config.MAX_SYNC_LOG_LINES
        for i in range(limit + 5):
            sync_log.append_event(f"事件 {i}")
        lines = sync_log.load()
        self.assertEqual(len(lines), limit)
        self.assertTrue(lines[-1].endswith(f"事件 {limit + 4}"))

    def test_download_log_roundtrip_and_signature(self):
        before = download_log.signature()
        download_log.append("作者甲", "作品一", 2, when="2026-11-07 10:00:00")
        lines = download_log.load()
        self.assertEqual(len(lines), 1)
        self.assertIn("该作者还剩 2 个没下完", lines[0])
        # load() 前先落盘，所以窗口刷新后队列一定是空的
        self.assertEqual(download_log.pending(), 0)
        self.assertNotEqual(download_log.signature(), before)

    def test_clear_removes_file(self):
        sync_log.append_event("随便一条")
        sync_log.load()
        self.assertTrue(os.path.exists(config.SYNC_LOG_FILE))
        sync_log.clear()
        self.assertFalse(os.path.exists(config.SYNC_LOG_FILE))


class StaleTempCleanupTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        config.ensure_dirs()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_only_old_atomic_write_leftovers_are_removed(self):
        """原子写入被强杀留下的 `名字.<pid>.<tid>.tmp` 要清掉，别人的 .tmp 不动。"""
        old = os.path.join(config.DATA_DIR, "authors.json.123.456.tmp")
        fresh = os.path.join(config.DATA_DIR, "authors.json.789.101.tmp")
        other = os.path.join(config.DATA_DIR, "notes.tmp")
        for path in (old, fresh, other):
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
        stale = time.time() - 3 * 3600
        os.utime(old, (stale, stale))
        os.utime(other, (stale, stale))
        removed = config.cleanup_stale_temp_files(max_age_s=3600)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(other))


class AppLogRotationTests(unittest.TestCase):
    """app.log 必须轮转：以前只增不减，本机实测长到 12 MB。"""

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        config.ensure_dirs()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_oversized_log_rolls_over_instead_of_growing(self):
        import glob
        import logging

        import app

        path = os.path.join(config.DATA_DIR, "app.log")
        handler = app._log_file_handler(path)
        logger = logging.getLogger("tests.rotation")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            for _ in range(60):
                logger.info("x" * 50000)  # 约 3 MB，超过单文件上限
        finally:
            logger.removeHandler(handler)
            handler.close()
        files = glob.glob(path + "*")
        sizes = [os.path.getsize(p) for p in files]
        self.assertGreater(len(files), 1, "日志应该已经轮转出备份文件")
        self.assertTrue(all(s <= app.APP_LOG_MAX_BYTES for s in sizes),
                        f"单文件不得超过上限：{sizes}")
        self.assertLessEqual(len(files), app.APP_LOG_BACKUPS + 1)


if __name__ == "__main__":
    unittest.main()
