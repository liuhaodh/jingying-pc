# -*- coding: utf-8 -*-
"""播放器：文案浮层显示/隐藏、自动连播开关、tar 作品按需解包。"""
import os
import unittest

from tests.support import ROOT  # noqa: F401

from player import PlayerWindow


class PlayerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        feed = [
            {"type": "video", "path": "missing-1.mp4", "title": "第一条", "author": "作者"},
            {"type": "video", "path": "missing-2.mp4", "title": "第二条", "author": "作者"},
        ]
        self.player = PlayerWindow(feed)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.player.close()
        self.player.deleteLater()
        self.qa.processEvents()
        self.player = None

    def test_overlay_toggle(self):
        self.assertIsNotNone(self.player.info_overlay)
        self.assertTrue(self.player.info_visible)
        self.player._toggle_info(False)
        self.assertFalse(self.player.info_visible)
        self.assertTrue(self.player.info_overlay.isHidden())
        self.assertEqual(self.player.info_btn.text(), "显示文案")
        self.player._toggle_info(True)
        self.assertTrue(self.player.info_visible)
        self.assertFalse(self.player.info_overlay.isHidden())
        self.assertEqual(self.player.info_btn.text(), "隐藏文案")

    def test_click_overlay_hides(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.player.show()
        for _ in range(10):
            self.qa.processEvents()
        self.player._toggle_info(True)
        self.assertFalse(self.player.info_overlay.isHidden())
        QTest.mouseClick(self.player.info_overlay, Qt.MouseButton.LeftButton)
        self.qa.processEvents()
        self.assertFalse(self.player.info_visible)
        self.assertTrue(self.player.info_overlay.isHidden())

    def test_new_video_shows_caption_again(self):
        self.player._toggle_info(False)
        self.assertTrue(self.player.info_overlay.isHidden())
        self.player._load(1)
        self.assertTrue(self.player.info_visible)
        self.assertFalse(self.player.info_overlay.isHidden())

    def test_album_slideshow_three_seconds_and_loops(self):
        import os
        import tempfile

        from PySide6.QtGui import QColor, QImage

        from player import SLIDESHOW_INTERVAL_MS, PlayerWindow

        tmp = tempfile.mkdtemp(prefix="jy-album-")
        paths = []
        for index, color in enumerate(("#3366cc", "#cc6633", "#33aa66")):
            image = QImage(160, 240, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            path = os.path.join(tmp, f"{index}.jpg")
            self.assertTrue(image.save(path, "JPG"))
            paths.append(path)
        album = PlayerWindow([{
            "type": "album", "images": paths, "videos": ["", "", ""],
            "title": "三张图", "author": "作者", "music": "", "time": 0,
        }])
        try:
            self.assertEqual(album.image_timer.interval(), SLIDESHOW_INTERVAL_MS)
            self.assertEqual(SLIDESHOW_INTERVAL_MS, 3000)
            self.assertTrue(album.image_timer.isActive())
            self.assertEqual(album.image_index, 0)
            album._auto_next_image()
            self.assertEqual(album.image_index, 1)
            album._auto_next_image()
            self.assertEqual(album.image_index, 2)
            album._auto_next_image()
            self.assertEqual(album.image_index, 0)  # 循环回第一张
        finally:
            album.close()
            album.deleteLater()
            self.qa.processEvents()

    def test_live_photo_advances_after_playback(self):
        import os
        import tempfile
        from unittest import mock

        from PySide6.QtGui import QColor, QImage
        from PySide6.QtMultimedia import QMediaPlayer

        from player import PlayerWindow

        tmp = tempfile.mkdtemp(prefix="jy-live-")
        paths = []
        for index, color in enumerate(("#3366cc", "#cc6633", "#33aa66")):
            image = QImage(160, 240, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            path = os.path.join(tmp, f"{index}.jpg")
            self.assertTrue(image.save(path, "JPG"))
            paths.append(path)
        live = os.path.join(tmp, "live.mp4")
        with open(live, "wb") as f:
            f.write(b"not-a-real-video-but-exists")
        album = PlayerWindow([{
            "type": "album", "images": paths, "videos": ["", live, ""],
            "title": "实况图", "author": "作者", "music": "", "time": 0,
        }])
        try:
            self.assertEqual(album.image_index, 0)
            self.assertTrue(album.image_timer.isActive())
            with mock.patch.object(album.live_player, "setSource"), \
                    mock.patch.object(album.live_player, "play"), \
                    mock.patch.object(album.live_player, "stop"):
                album.image_index = 1
                album._show_image()
                # 实况图不能用固定 3 秒定时器，必须等播放结束
                self.assertFalse(album.image_timer.isActive())
                self.assertEqual(album._live_current, live)
                album._on_live_status(QMediaPlayer.MediaStatus.EndOfMedia)
                self.qa.processEvents()
                self.assertEqual(album.image_index, 2)
                # 下一张是静态图，恢复 3 秒轮播
                self.assertTrue(album.image_timer.isActive())
        finally:
            album.close()
            album.deleteLater()
            self.qa.processEvents()

    def test_autoplay_switch(self):
        from PySide6.QtMultimedia import QMediaPlayer
        calls = []
        self.player.next = lambda: calls.append(1)
        self.assertTrue(self.player.autoplay_cb.isChecked())
        self.player._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertEqual(len(calls), 1)

        self.player.autoplay_cb.setChecked(False)
        self.player._on_status(QMediaPlayer.MediaStatus.EndOfMedia)
        self.assertEqual(len(calls), 1)


class ArchiveFeedTests(unittest.TestCase):
    """信息流里给的是 tar 包时，播放器要按需解包再播。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile

        import archive

        self.tmp = tempfile.mkdtemp(prefix="jy-archive-")
        self.addCleanup(self._cleanup)
        work = os.path.join(self.tmp, "作者_文案_20261108_101010")
        os.makedirs(work)
        for name, data in (("图片_01.jpg", b"i1"), ("音乐.mp3", b"m1"),
                           ("文案.txt", "文案".encode("utf-8"))):
            with open(os.path.join(work, name), "wb") as f:
                f.write(data)
        self.tar_path = os.path.join(self.tmp, "Videos", "作者_uid", "123.tar")
        archive.pack(work, self.tar_path)
        feed = [{
            "type": "album",
            "images": [],
            "videos": [],
            "music": "",
            "archive": self.tar_path,
            "title": "打包的图集",
            "author": "作者",
        }]
        self.player = PlayerWindow(feed)

    def _cleanup(self):
        import shutil
        self.player.close()
        self.player.deleteLater()
        self.qa.processEvents()
        self.player = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_feed_item_is_materialized_from_archive(self):
        # PlayerWindow 构造时就会加载第 0 条，此时应该已经解包完毕
        item = self.player.feed[0]
        self.assertNotIn("archive", item)
        self.assertEqual([os.path.basename(p) for p in item["images"]],
                         ["图片_01.jpg"])
        self.assertTrue(item["music"].endswith("音乐.mp3"))
        self.assertTrue(item["images"][0].startswith(self.player._play_cache))

    def test_close_removes_extract_cache(self):
        self.player._load(0)
        cache = self.player._play_cache
        self.assertTrue(cache)
        self.player.close()
        self.assertFalse(os.path.exists(cache))


if __name__ == "__main__":
    unittest.main()
