# -*- coding: utf-8 -*-
"""背景音乐补齐：缺音乐日志、补下载条件与接线回归（本地 HTTP 桩，不联网）。"""
import http.server
import inspect
import os
import threading
import unittest
from unittest import mock

from tests.support import isolated_config

import config
import contacts_sync
import downloader
import models
import net_policy

AUDIO = b"JY-AUDIO-PAYLOAD" * 512


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(AUDIO)))
        self.end_headers()
        self.wfile.write(AUDIO)


class _ServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"


class _IsolatedMixin:
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        net_policy.reset_runtime()
        self.allow = mock.patch.object(downloader, "_is_allowed_host",
                                       return_value=True)
        self.allow.start()
        self.info = models.MediaInfo()
        self.info.aweme_id = "7625256716829025253"
        self.info.title = "花开岁岁"
        self.info.author = "邵哈哈~"
        self.info.play_url = self.url("video.mp4") if hasattr(self, "url") else ""
        self.author_folder = "author"
        self.work_folder = "fallback"

    def tearDown(self):
        self.allow.stop()
        self._ctx.__exit__(None, None, None)
        net_policy.reset_runtime()

    def work_directory(self):
        return downloader.work_dir(self.author_folder, self.work_folder, self.info)

    def make_work_dir(self):
        directory = self.work_directory()
        os.makedirs(directory, exist_ok=True)
        # 模拟「已下载但缺音乐」：有一张图片，没有 音乐.*
        with open(os.path.join(directory, "图片_01.webp"), "wb") as f:
            f.write(b"image")
        return directory


class HasMusicFileTests(unittest.TestCase):
    def test_empty_directory(self):
        with isolated_config() as tmp:
            self.assertFalse(downloader.has_music_file(tmp))

    def test_detects_music_with_any_extension(self):
        with isolated_config() as tmp:
            open(os.path.join(tmp, "音乐.m4a"), "wb").close()
            self.assertTrue(downloader.has_music_file(tmp))

    def test_ignores_unfinished_part(self):
        with isolated_config() as tmp:
            open(os.path.join(tmp, "音乐.mp3.part"), "wb").close()
            self.assertFalse(downloader.has_music_file(tmp))

    def test_missing_directory(self):
        self.assertFalse(downloader.has_music_file("C:/__jy_no_such_dir__/x"))


class BackfillNeededTests(_IsolatedMixin, unittest.TestCase):
    """纯判定：只有「本次有地址 + 散文件目录还在 + 本地没音乐」才补。"""

    def test_no_url_is_not_needed(self):
        directory = self.make_work_dir()
        self.assertFalse(contacts_sync.music_backfill_needed(
            self.info, self.author_folder, self.work_folder))

    def test_needed_when_url_appears_and_music_missing(self):
        self.make_work_dir()
        self.info.music_url = self.url("music.mp3") if hasattr(self, "url") else "https://x/m.mp3"
        self.assertTrue(contacts_sync.music_backfill_needed(
            self.info, self.author_folder, self.work_folder))

    def test_not_needed_when_music_already_exists(self):
        directory = self.make_work_dir()
        self.info.music_url = "https://x/m.mp3"
        open(os.path.join(directory, "音乐.mp3"), "wb").close()
        self.assertFalse(contacts_sync.music_backfill_needed(
            self.info, self.author_folder, self.work_folder))

    def test_not_needed_for_tar_or_missing_directory(self):
        self.info.music_url = "https://x/m.mp3"
        self.assertFalse(contacts_sync.music_backfill_needed(
            self.info, self.author_folder, self.work_folder))


class DownloadMusicOnlyTests(_IsolatedMixin, _ServerMixin, unittest.TestCase):
    def test_no_url_returns_empty_with_explicit_log(self):
        directory = self.make_work_dir()
        with self.assertLogs(level="INFO") as captured:
            result = downloader.download_music_only(
                self.info, self.author_folder, self.work_folder)
        self.assertEqual(result, "")
        self.assertIn("未下载背景音乐", "\n".join(captured.output))
        self.assertFalse(downloader.has_music_file(directory))

    def test_existing_music_is_left_alone(self):
        directory = self.make_work_dir()
        self.info.music_url = self.url("music.mp3")
        target = os.path.join(directory, "音乐.mp3")
        with open(target, "wb") as f:
            f.write(b"keep-me")
        self.assertEqual(downloader.download_music_only(
            self.info, self.author_folder, self.work_folder), "")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"keep-me")

    def test_missing_directory_is_skipped(self):
        self.info.music_url = self.url("music.mp3")
        with self.assertLogs(level="INFO") as captured:
            result = downloader.download_music_only(
                self.info, self.author_folder, self.work_folder)
        self.assertEqual(result, "")
        self.assertIn("作品目录不存在", "\n".join(captured.output))

    def test_downloads_music_and_keeps_other_files(self):
        directory = self.make_work_dir()
        self.info.create_time = 1775393430
        self.info.music_url = self.url("music.mp3")
        with self.assertLogs(level="INFO") as captured:
            path = downloader.download_music_only(
                self.info, self.author_folder, self.work_folder)
        self.assertTrue(path)
        self.assertEqual(os.path.basename(path), "音乐.mp3")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), AUDIO)
        self.assertTrue(os.path.exists(os.path.join(directory, "图片_01.webp")))
        self.assertFalse(os.path.exists(path + ".part"))
        self.assertIn("背景音乐补齐完成", "\n".join(captured.output))

    def test_cancel_leaves_no_partial_file(self):
        directory = self.make_work_dir()
        self.info.music_url = self.url("music.mp3")
        event = threading.Event()
        event.set()
        with self.assertRaises(downloader.DownloadCancelled):
            downloader.download_music_only(
                self.info, self.author_folder, self.work_folder,
                cancel_event=event)
        self.assertFalse(downloader.has_music_file(directory))
        self.assertEqual(os.listdir(directory), ["图片_01.webp"])


class DownloadWorkLoggingTests(_IsolatedMixin, _ServerMixin, unittest.TestCase):
    """缺音乐不再静默：下载完成后必须有一条带原因的日志。"""

    def _run_download(self):
        return downloader.download_work(
            self.info, self.author_folder, self.work_folder)

    def test_logs_when_music_node_absent(self):
        with self.assertLogs(level="INFO") as captured:
            self._run_download()
        output = "\n".join(captured.output)
        self.assertIn("未下载背景音乐", output)
        self.assertIn("没有 music 节点", output)
        self.assertFalse(downloader.has_music_file(self.work_directory()))

    def test_logs_when_music_has_no_playable_url(self):
        self.info.music_title = "原创原声"
        with self.assertLogs(level="INFO") as captured:
            self._run_download()
        output = "\n".join(captured.output)
        self.assertIn("未下载背景音乐", output)
        self.assertIn("music 节点缺少播放地址", output)

    def test_still_downloads_music_when_url_present(self):
        self.info.music_url = self.url("music.mp3")
        result = self._run_download()
        self.assertTrue(result.music_saved)
        self.assertTrue(result.music_path)
        self.assertTrue(downloader.has_music_file(self.work_directory()))


class WiringTests(unittest.TestCase):
    """防止接线被误删：同步判重处必须调用补齐判定与后台队列。"""

    def test_sync_author_works_is_wired_to_backfill(self):
        src = inspect.getsource(contacts_sync.ContactsSyncMixin._sync_author_works)
        self.assertIn("music_backfill_needed", src)
        self.assertIn("_queue_music_backfill", src)
        self.assertIn("music_jobs", src)

    def test_backfill_runs_outside_download_manager(self):
        src = inspect.getsource(contacts_sync.ContactsSyncMixin._queue_music_backfill)
        self.assertIn("download_music_only", src)
        self.assertIn("Worker", src)
        self.assertNotIn("self.dm.enqueue", src)


if __name__ == "__main__":
    unittest.main()
