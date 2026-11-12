# -*- coding: utf-8 -*-
"""下载器：目录命名、断点续传、取消、域名白名单（本地 HTTP 服务，不联网）。"""
import http.server
import os
import threading
import time
import unittest
from unittest import mock

from tests.support import isolated_config

import config
import downloader
import models


PAYLOAD = b"0123456789abcdef" * 4096  # 64 KiB


class _Handler(http.server.BaseHTTPRequestHandler):
    support_range = True
    payload = PAYLOAD

    def log_message(self, *_args):
        pass

    def do_GET(self):
        data = self.payload
        header = self.headers.get("Range")
        start = 0
        if header and self.support_range:
            try:
                start = int(header.split("=")[1].split("-")[0])
            except (IndexError, ValueError):
                start = 0
        if start >= len(data):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = data[start:]
        if start and self.support_range:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloaderTests(unittest.TestCase):
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

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self._real_is_allowed = downloader._is_allowed_host
        self.allowed = lambda url: (url.startswith(f"http://127.0.0.1:{self.port}/")
                                    or self._real_is_allowed(url))
        self.patch = mock.patch.object(downloader, "_is_allowed_host", side_effect=self.allowed)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._ctx.__exit__(None, None, None)

    def _url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"

    def test_naming_helpers(self):
        self.assertEqual(downloader.sanitize_segment('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(downloader.sanitize_segment("  多   空格  "), "多 空格")
        self.assertEqual(downloader.build_author_folder("作者名", "uid1"), "作者名_uid1")
        info = models.MediaInfo()
        info.title = "很长的标题" * 10
        info.create_time = 1752069813
        folder = downloader.build_work_folder(info, "作者名")
        self.assertTrue(folder.startswith("作者名_"))
        self.assertTrue(folder.endswith(time.strftime("%Y%m%d_%H%M%S", time.localtime(1752069813))))

    def test_reserved_and_trailing_dot_names(self):
        """Windows 设备名、结尾点/空格、`.`/`..` 都要处理掉。

        昵称正好叫 CON 的作者以前会让 makedirs 直接失败，而“已下载”判断恒为
        False，于是每轮同步都重下一遍；`.`/`..` 则会把文件写到下载根之外。
        """
        self.assertEqual(downloader.sanitize_segment("CON"), "_CON")
        self.assertEqual(downloader.sanitize_segment("nul.txt"), "_nul.txt")
        self.assertEqual(downloader.sanitize_segment("COM1"), "_COM1")
        self.assertEqual(downloader.sanitize_segment("普通名字"), "普通名字")
        self.assertEqual(downloader.sanitize_segment("结尾点. "), "结尾点")
        self.assertEqual(downloader.sanitize_segment("."), "")
        self.assertEqual(downloader.sanitize_segment(".."), "")
        self.assertEqual(downloader.sanitize_segment("   "), "")
        # 作者目录永远不会拼出空名或 `..`
        self.assertEqual(downloader.build_author_folder("..", ""), "作者")
        self.assertEqual(downloader.build_author_folder("", ""), "作者")

    def test_download_work_rejects_unsafe_folder(self):
        info = models.MediaInfo()
        info.play_url = self._url("a.mp4")
        info.create_time = 0
        with self.assertRaises(IOError):
            downloader.download_work(info, "..", "作品")
        with self.assertRaises(IOError):
            downloader.download_work(info, "作者", os.path.join("x", "..", "作品"))

    def test_host_whitelist(self):
        self.assertTrue(self._real_is_allowed("https://v11.douyinvod.com/a.mp4"))
        self.assertTrue(self._real_is_allowed("https://p3.douyinpic.com/a.jpg"))
        # 音乐直链会跳到字节 GSLB 前置，不放行会导致背景音乐全部下载失败
        self.assertTrue(self._real_is_allowed(
            "https://v9-default.tt.x.bsgslb.cn/lf9-music-east.douyinstatic.com/obj/a.mp3"))
        self.assertFalse(self._real_is_allowed("https://douyin.com.evil.com/a.mp4"))
        self.assertFalse(self._real_is_allowed("https://127.0.0.1/a.mp4"))
        self.assertFalse(self._real_is_allowed("ftp://v11.douyinvod.com/a.mp4"))

    def test_guess_ext(self):
        self.assertEqual(downloader._guess_ext("https://x/a.webp", "image"), ".webp")
        self.assertEqual(downloader._guess_ext("https://x/a.png?x=1", "image"), ".png")
        self.assertEqual(downloader._guess_ext("https://x/a.m4a", "audio"), ".m4a")
        self.assertEqual(downloader._guess_ext("https://x/a.webm", "video"), ".webm")
        self.assertEqual(downloader._guess_ext("https://x/a", "video"), ".mp4")

    def test_download_fresh(self):
        target = os.path.join(self.tmp, "a.mp4")
        size = downloader._download_to_file(self._url("a.mp4"), target, None, 0, 1, timestamp=0)
        self.assertEqual(size, len(PAYLOAD))
        self.assertEqual(open(target, "rb").read(), PAYLOAD)
        self.assertFalse(os.path.exists(target + ".part"))

    def test_resume_with_range(self):
        target = os.path.join(self.tmp, "b.mp4")
        part = target + ".part"
        with open(part, "wb") as f:
            f.write(PAYLOAD[:1000])
        size = downloader._download_to_file(self._url("b.mp4"), target, None, 0, 1)
        self.assertEqual(size, len(PAYLOAD))
        self.assertEqual(open(target, "rb").read(), PAYLOAD)
        self.assertFalse(os.path.exists(part))

    def test_resume_falls_back_when_server_ignores_range(self):
        _Handler.support_range = False
        try:
            target = os.path.join(self.tmp, "c.mp4")
            with open(target + ".part", "wb") as f:
                f.write(PAYLOAD[:1000])
            downloader._download_to_file(self._url("c.mp4"), target, None, 0, 1)
            self.assertEqual(open(target, "rb").read(), PAYLOAD)
        finally:
            _Handler.support_range = True

    def test_stale_part_reset_on_416(self):
        target = os.path.join(self.tmp, "d.mp4")
        with open(target + ".part", "wb") as f:
            f.write(b"x" * (len(PAYLOAD) + 10))
        with self.assertRaises(IOError):
            downloader._download_to_file(self._url("d.mp4"), target, None, 0, 1)
        self.assertFalse(os.path.exists(target + ".part"))

    def test_cancel(self):
        target = os.path.join(self.tmp, "e.mp4")
        event = threading.Event()
        event.set()
        with self.assertRaises(downloader.DownloadCancelled):
            downloader._download_to_file(self._url("e.mp4"), target, None, 0, 1,
                                         cancel_event=event)
        self.assertFalse(os.path.exists(target))

    def test_rejects_unknown_host(self):
        target = os.path.join(self.tmp, "f.mp4")
        with self.assertRaises(ValueError):
            downloader._download_to_file("https://evil.example.com/a.mp4", target, None, 0, 1)

    def test_download_work_video_folder(self):
        """一个作品 = 净影/作者_id/<作品id>/，里面是 视频 + 文案，不再打 tar。"""
        info = models.MediaInfo()
        info.aweme_id = "123"
        info.title = "作品标题"
        info.author = "作者"
        info.unique_id = "uid"
        info.play_url = self._url("video.mp4")
        info.create_time = 1752069813
        result = downloader.download_work(info, "作者_uid", "作者_作品标题_20250709_204333")
        expected_dir = os.path.join(config.SAVE_ROOT, "作者_uid", "123")
        self.assertEqual(os.path.normcase(os.path.dirname(result.video_path)),
                         os.path.normcase(expected_dir))
        self.assertEqual(os.path.basename(result.video_path), "视频.mp4")
        self.assertTrue(os.path.isfile(result.video_path))
        # 作品文件夹里同时有完整文案
        self.assertEqual(os.path.normcase(os.path.dirname(result.caption_path)),
                         os.path.normcase(expected_dir))
        self.assertEqual(os.path.basename(result.caption_path), "文案.txt")
        # 不再产出 tar
        self.assertEqual(result.archive_path, "")
        self.assertEqual(result.archive_members, [])
        caption = open(result.caption_path, encoding="utf-8").read()
        self.assertIn("作品链接：https://www.douyin.com/video/123", caption)

    def test_download_work_album_with_music(self):
        """图集 + 音乐都进同一个作品文件夹，Music 目录不再生成。"""
        info = models.MediaInfo()
        info.aweme_id = "456"
        info.title = "图集"
        info.author = "作者"
        info.unique_id = "uid"
        info.image_urls = [self._url("1.jpg"), self._url("2.jpg")]
        info.image_videos = ["", ""]
        info.music_url = self._url("music.mp3")
        result = downloader.download_work(info, "作者_uid", "作者_图集_20250709_204333")
        expected_dir = os.path.join(config.SAVE_ROOT, "作者_uid", "456")
        self.assertEqual([os.path.basename(p) for p in result.album_paths],
                         ["图片_01.jpg", "图片_02.jpg"])
        self.assertEqual(result.video_path, "")
        self.assertTrue(result.music_saved)
        self.assertEqual(os.path.basename(result.music_path), "音乐.mp3")
        for path in list(result.album_paths) + [result.music_path, result.caption_path]:
            self.assertEqual(os.path.normcase(os.path.dirname(path)),
                             os.path.normcase(expected_dir))
        self.assertFalse(os.path.isdir(os.path.join(config.MUSIC_BASE, "作者_uid")))

    def test_download_work_cleans_staging(self):
        """下载完成后临时目录里不能留散文件（取消/异常也一样）。"""
        info = models.MediaInfo()
        info.aweme_id = "999"
        info.title = "清理"
        info.author = "作者"
        info.play_url = self._url("video.mp4")
        info.create_time = 0
        before = set(os.listdir(config.TEMP_DIR))
        downloader.download_work(info, "作者_uid", "作者_清理_20250709_204333")
        after = set(os.listdir(config.TEMP_DIR))
        self.assertEqual(after - before, set())

    def test_download_work_progress_is_per_work_album(self):
        """图集进度按整个作品排一条轴：图片/动图/音乐依次各占一格，不重复、不倒退。"""
        calls = []

        def fake_download(url, target, progress, index, total, **kwargs):
            calls.append((index, total))
            if progress:
                for pct in (0, 50, 100):
                    progress(pct, index, total)
            return 10

        info = models.MediaInfo()
        info.aweme_id = "789"
        info.title = "图集进度"
        info.author = "作者"
        info.unique_id = "uid"
        info.image_urls = [self._url("1.jpg"), self._url("2.jpg")]
        info.image_videos = [self._url("1.mp4"), ""]  # 只有第一张有实况动图
        info.music_url = self._url("music.mp3")

        with mock.patch.object(downloader, "_download_to_file",
                               side_effect=fake_download):
            downloader.download_work(info, "作者_uid", "作者_进度_20250709_204333")

        # 顺序：图片0、动图0、图片1、音乐 → 序号 0..3，文件总数 4
        self.assertEqual([idx for idx, _ in calls], [0, 1, 2, 3])
        self.assertTrue(all(total == 4 for _, total in calls))
        values = [int((idx + pct / 100.0) * 100 / total)
                  for idx, total in calls for pct in (0, 50, 100)]
        self.assertEqual(values, sorted(values))  # 单调不倒退
        self.assertEqual((values[0], values[-1]), (0, 100))

    def test_download_work_progress_is_per_work_video(self):
        """视频 + 音乐：两个文件排在同一条进度轴上（0-50%、50-100%），不是各走各的。"""
        calls = []

        def fake_download(url, target, progress, index, total, **kwargs):
            calls.append((index, total))
            if progress:
                for pct in (0, 100):
                    progress(pct, index, total)
            return 10

        info = models.MediaInfo()
        info.aweme_id = "790"
        info.title = "视频进度"
        info.author = "作者"
        info.unique_id = "uid"
        info.play_url = self._url("video.mp4")
        info.music_url = self._url("music.mp3")

        with mock.patch.object(downloader, "_download_to_file",
                               side_effect=fake_download):
            downloader.download_work(info, "作者_uid", "作者_视频_20250709_204333")

        self.assertEqual([idx for idx, _ in calls], [0, 1])
        self.assertTrue(all(total == 2 for _, total in calls))


if __name__ == "__main__":
    unittest.main()
