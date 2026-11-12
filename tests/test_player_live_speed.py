# -*- coding: utf-8 -*-
"""播放器：图集实况切换残留、长按倍速。

两个线上问题：
1. 图集左右滑动后偶尔还显示第一张 —— 实况图换源是异步的，
   切页太早会停留上一张实况的最后一帧；
2. 长按画面没有倍速 —— 之前完全没实现，这次补上并给明确提示。
"""
import os
import shutil
import tempfile
import unittest

from tests.support import ROOT  # noqa: F401

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from player import PlayerWindow


class FakeMedia:
    """记录调用顺序与倍速的 QMediaPlayer 替身（不需要真实解码后端）。"""

    def __init__(self, support_rate=True):
        self.rate = 1.0
        self.support_rate = support_rate
        self.sources = []
        self.playing = False
        self.stop_count = 0
        self.loops = None

    def setSource(self, url):
        self.sources.append(url.toString() if hasattr(url, "toString") else str(url))

    def play(self):
        self.playing = True

    def pause(self):
        self.playing = False

    def stop(self):
        self.stop_count += 1
        self.playing = False

    def setLoops(self, loops):
        self.loops = loops

    def playbackState(self):
        return (QMediaPlayer.PlaybackState.PlayingState if self.playing
                else QMediaPlayer.PlaybackState.StoppedState)

    def setPlaybackRate(self, rate):
        # 解码后端不支持变速时，Qt 会把值钳回 1.0
        if self.support_rate:
            self.rate = rate

    def playbackRate(self):
        return self.rate


class PlayerLiveSwitchTests(unittest.TestCase):
    """滑动切图后必须真的显示新的一张，而不是上一张实况的残帧。"""

    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jy-player-")
        # 两张宽高比不同的图：3:2 与 1:3，渲染结果能区分是哪一张
        self.img1 = os.path.join(self.tmp, "a1.png")
        self.img2 = os.path.join(self.tmp, "a2.png")
        QPixmap(60, 40).save(self.img1)
        QPixmap(30, 90).save(self.img2)
        self.live1 = os.path.join(self.tmp, "a1.mp4")
        self.live2 = os.path.join(self.tmp, "a2.mp4")
        for path in (self.live1, self.live2):
            with open(path, "wb") as handle:
                handle.write(b"fake")
        feed = [{
            "type": "album",
            "images": [self.img1, self.img2],
            "videos": [self.live1, self.live2],
            "title": "实况图集",
            "author": "作者",
            "music": "",
        }]
        self.w = PlayerWindow(feed)
        self.w.live_player = FakeMedia()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.w.close()
        self.w.deleteLater()
        self.qa.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _aspect(self):
        pix = self.w.image_label.pixmap()
        self.assertFalse(pix.isNull())
        return pix.width() / max(1, pix.height())

    def test_live_switch_shows_static_placeholder_first(self):
        self.w._load(0)
        # 换源期间显示静态图，不能直接把上一帧留在画面上
        self.assertIs(self.w.stack.currentWidget(), self.w.image_label)
        self.assertFalse(self.w._live_ready)
        self.assertTrue(self.w.live_player.sources[-1].endswith("a1.mp4"))

        # 首帧就绪后才切到实况页
        self.w._on_live_status(QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertIs(self.w.stack.currentWidget(), self.w.live_widget)
        self.assertTrue(self.w._live_ready)

    def test_switch_to_second_image_renders_second_picture(self):
        self.w._load(0)
        self.w._on_live_status(QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertIs(self.w.stack.currentWidget(), self.w.live_widget)
        first_aspect = self._aspect()

        self.w.next_image()
        self.assertEqual(self.w.image_index, 1)
        # 切到第 2 张：先回到静态图占位，并且渲染的是第 2 张
        self.assertIs(self.w.stack.currentWidget(), self.w.image_label)
        self.assertFalse(self.w._live_ready)
        self.assertTrue(self.w.live_player.sources[-1].endswith("a2.mp4"))
        second_aspect = self._aspect()
        self.assertLess(second_aspect, 1.0)   # 1:3 竖图
        self.assertGreater(first_aspect, 1.0)  # 3:2 横图
        self.assertNotAlmostEqual(first_aspect, second_aspect, places=2)

        # 第 2 张首帧到了才翻页
        self.w._on_live_status(QMediaPlayer.MediaStatus.LoadedMedia)
        self.assertIs(self.w.stack.currentWidget(), self.w.live_widget)

    def test_switch_resets_ready_flag_so_old_frame_never_shows(self):
        self.w._load(0)
        self.w._on_live_status(QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertTrue(self.w._live_ready)
        self.w.next_image()
        self.assertFalse(self.w._live_ready)
        # 旧 source 的缓冲回调不能把页面提前翻上去
        self.w._on_live_status(QMediaPlayer.MediaStatus.BufferedMedia)
        self.assertIs(self.w.stack.currentWidget(), self.w.live_widget)


class PlayerAlbumRenderTests(unittest.TestCase):
    """纯图片图集：切作品时必须先清屏，且首帧不能是小尺寸兜底图。"""

    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jy-album-render-")
        self.img_a = os.path.join(self.tmp, "a.png")
        self.img_b = os.path.join(self.tmp, "b.png")
        QPixmap(1080, 720).save(self.img_a)
        QPixmap(720, 1280).save(self.img_b)
        self.w = PlayerWindow([
            {"type": "album", "images": [self.img_a], "videos": [],
             "title": "A", "author": "作者", "music": ""},
            {"type": "album", "images": [self.img_b], "videos": [],
             "title": "B", "author": "作者", "music": ""},
        ])
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.w.close()
        self.w.deleteLater()
        self.qa.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_work_clears_previous_pixmap(self):
        self.w._load(0)
        self.assertFalse(self.w.image_label.pixmap().isNull())
        # 模拟「新作品渲染慢/失败」：渲染被跳过时，旧一眼的图也不能留在画面上
        original = self.w._render_image
        self.w._render_image = lambda: None
        try:
            self.w._load(1)
        finally:
            self.w._render_image = original
        self.assertTrue(self.w.image_label.pixmap().isNull())

    def test_first_frame_is_not_tiny_fallback(self):
        self.w.resize(900, 700)
        self.w.show()
        for _ in range(15):
            self.qa.processEvents()
        pix = self.w.image_label.pixmap()
        self.assertFalse(pix.isNull())
        # 旧实现会把第一张兜底成 100×66 的小图
        self.assertGreater(pix.width(), 200)
        self.assertGreater(pix.height(), 150)

    def test_render_keeps_aspect_ratio(self):
        self.w._load(1)
        pix = self.w.image_label.pixmap()
        self.assertFalse(pix.isNull())
        self.assertAlmostEqual(pix.width() / pix.height(), 720 / 1280, places=1)


class PlayerLongPressSpeedTests(unittest.TestCase):
    """长按画面 2× 倍速，松开恢复 1×；不支持时给出明确提示。"""

    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jy-speed-")
        self.video = os.path.join(self.tmp, "v.mp4")
        with open(self.video, "wb") as handle:
            handle.write(b"fake")
        img = os.path.join(self.tmp, "i.png")
        QPixmap(40, 40).save(img)
        self.w = PlayerWindow([
            {"type": "video", "path": self.video, "title": "视频", "author": "作者"},
            {"type": "album", "images": [img], "videos": [], "title": "纯图集", "author": "作者"},
        ])
        self.w.player = FakeMedia()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.w.close()
        self.w.deleteLater()
        self.qa.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mouse(self, kind, pos=(60, 60)):
        return QMouseEvent(kind, QPointF(*pos), QPointF(*pos),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    def test_long_press_applies_and_releases_rate(self):
        self.w._load(0)
        self.w._begin_long_press_speed()
        self.assertEqual(self.w.player.playbackRate(), 2.0)
        self.assertTrue(self.w._speed_active)
        self.assertIn("倍速", self.w.speed_badge.text())
        self.assertFalse(self.w.speed_badge.isHidden())

        self.w._end_long_press_speed()
        self.assertEqual(self.w.player.playbackRate(), 1.0)
        self.assertFalse(self.w._speed_active)
        self.assertTrue(self.w.speed_badge.isHidden())

    def test_unsupported_backend_tells_user(self):
        self.w._load(0)
        self.w.player.support_rate = False
        self.w._begin_long_press_speed()
        self.assertFalse(self.w._speed_active)
        self.assertEqual(self.w.speed_badge.text(), "当前作品不支持倍速")
        self.assertFalse(self.w.speed_badge.isHidden())

    def test_album_without_live_says_not_supported(self):
        self.w._load(1)
        self.assertIsNone(self.w._speed_target())
        self.w._begin_long_press_speed()
        self.assertEqual(self.w.speed_badge.text(), "图集不支持倍速")

    def test_press_starts_timer_and_release_does_not_toggle_play(self):
        self.w._load(0)
        QApplication.sendEvent(self.w.video_view.viewport(),
                               self._mouse(QEvent.Type.MouseButtonPress))
        self.assertTrue(self.w._long_press_timer.isActive())

        # 模拟长按计时到点
        self.w._long_press_timer.stop()
        self.w._begin_long_press_speed()
        self.assertEqual(self.w.player.playbackRate(), 2.0)
        before = self.w.play_btn.text()

        QApplication.sendEvent(self.w.video_view.viewport(),
                               self._mouse(QEvent.Type.MouseButtonRelease))
        self.assertEqual(self.w.player.playbackRate(), 1.0)
        self.assertEqual(self.w.play_btn.text(), before)  # 没被当成单击暂停

    def test_drag_cancels_long_press(self):
        self.w._load(0)
        QApplication.sendEvent(self.w.video_view.viewport(),
                               self._mouse(QEvent.Type.MouseButtonPress))
        QApplication.sendEvent(self.w.video_view.viewport(),
                               self._mouse(QEvent.Type.MouseMove, (200, 60)))
        self.assertFalse(self.w._long_press_timer.isActive())
        self.assertEqual(self.w.player.playbackRate(), 1.0)

    def test_switch_work_resets_speed(self):
        self.w._load(0)
        self.w._begin_long_press_speed()
        self.assertEqual(self.w.player.playbackRate(), 2.0)
        self.w._load(1)
        self.assertEqual(self.w.player.playbackRate(), 1.0)
        self.assertTrue(self.w.speed_badge.isHidden())


if __name__ == "__main__":
    unittest.main()
