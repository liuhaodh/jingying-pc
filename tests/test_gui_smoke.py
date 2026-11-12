# -*- coding: utf-8 -*-
"""离屏 GUI 冒烟测试：主窗口能构造、关键交互不抛异常。

这些用例在 2026-10-30 曾能直接抓到 `_xfer_new` / `parseBtn` 这类启动即崩的问题。
"""
import os
import time
import unittest

from tests.support import isolated_config

import config
import downloader
import store as store_mod


class GuiSmokeTests(unittest.TestCase):
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
        self.addCleanup(self._cleanup)

    def _cleanup(self):
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

    def test_window_builds(self):
        self.assertTrue(self.window.parse_btn.isEnabled())
        self.assertIsNotNone(self.window.download_panel.list)
        self.assertIsNotNone(self.window.upload_panel.list)
        self.assertIsNotNone(self.window.history_list)
        self.assertIsNotNone(self.window.contact_list)
        self.assertEqual(self.window.windowTitle(), f"{config.APP_NAME} · 电脑版 v{config.APP_VERSION}")
        self.assertEqual(self.window.download_panel.progress.value(), 0)
        self.assertEqual(self.window.upload_panel.progress.value(), 0)

    def test_sync_log_window(self):
        """「查看日志」只显示账号 / 作者 / 作品数。"""
        import sync_log
        sync_log.clear()
        sync_log.append_event("开始同步，共 2 位作者", when="2026-11-06 21:00:00")
        sync_log.append_start("账号2", "作者甲·uid1", when="2026-11-06 21:00:01")
        sync_log.append("账号2", "作者甲·uid1", 103, when="2026-11-06 21:00:10")
        sync_log.append_event("同步结束：成功 103 个作品，失败 0 个",
                              when="2026-11-06 21:00:11")
        self.window._open_sync_log_window()
        window = self.window.sync_log_window
        text = window.text.toPlainText()
        self.assertEqual(text.splitlines(), [
            "[2026-11-06 21:00:00] 开始同步，共 2 位作者",
            "[2026-11-06 21:00:01] 用 账号2 开始同步 作者甲·uid1",
            "[2026-11-06 21:00:10] 使用 账号2 同步了 作者甲·uid1 的 103 个作品",
            "[2026-11-06 21:00:11] 同步结束：成功 103 个作品，失败 0 个",
        ])
        self.assertNotIn("下载失败", text)
        self.assertIn("（4 条）", window.title.text())
        self.assertTrue(window.isVisible())
        # 自动刷新：不点「刷新」，新写入的日志也能出现
        sync_log.append_event("同步已停止", when="2026-11-06 21:00:12")
        window._auto_refresh()
        self.assertIn("同步已停止", window.text.toPlainText())
        window.clear_log()
        self.assertEqual(window.text.toPlainText(), "")
        window.close()

    def test_download_log_window(self):
        """「下载日志」显示下载了谁的哪个作品、该作者还剩多少没下完。"""
        import download_log
        download_log.clear()
        download_log.append_open("账号2", "作者甲·uid1", 103,
                                 when="2026-11-06 21:00:00")
        download_log.append_start("作者甲·uid1", "第一个作品",
                                  when="2026-11-06 21:00:01")
        download_log.append("作者甲·uid1", "第一个作品", 3,
                            when="2026-11-06 21:00:05")
        download_log.append_start("作者甲·uid1", "最后一个作品",
                                  when="2026-11-06 21:00:06")
        download_log.append_fail("作者甲·uid1", "最后一个作品",
                                 when="2026-11-06 21:00:09")
        self.window._open_download_log_window()
        window = self.window.download_log_window
        text = window.text.toPlainText()
        self.assertEqual(text.splitlines(), [
            "[2026-11-06 21:00:00] 用 账号2 打开了 作者甲·uid1，找到了 103 个作品",
            "[2026-11-06 21:00:01] 开始下载 作者甲·uid1 的《第一个作品》",
            "[2026-11-06 21:00:05] 下载完成 作者甲·uid1 的《第一个作品》，该作者还剩 3 个没下完",
            "[2026-11-06 21:00:06] 开始下载 作者甲·uid1 的《最后一个作品》",
            "[2026-11-06 21:00:09] 下载失败 作者甲·uid1 的《最后一个作品》",
        ])
        self.assertIn("（5 条）", window.title.text())
        self.assertTrue(window.isVisible())
        # 自动刷新：队列里新加的行不点「刷新」也能出现
        download_log.append("作者甲·uid1", "第三个作品", 2,
                            when="2026-11-06 21:00:12")
        window._auto_refresh()
        self.assertIn("第三个作品", window.text.toPlainText())
        window.clear_log()
        self.assertEqual(window.text.toPlainText(), "")
        window.close()

    def test_three_standalone_windows(self):
        """通讯录 / 最近保存 / 传输各自独立成窗，主窗口只保留同名引用。"""
        self.assertFalse(self.window.contacts_window.isVisible())
        self.assertFalse(self.window.history_window.isVisible())
        self.assertFalse(self.window.transfer_window.isVisible())
        self.window._open_contacts_window()
        self.assertTrue(self.window.contacts_window.isVisible())
        self.window._open_history_window()
        self.assertTrue(self.window.history_window.isVisible())
        self.window._open_transfer_window()
        self.assertTrue(self.window.transfer_window.isVisible())
        # 控件由窗口持有，主窗口只是引用，业务代码不用改
        self.assertIs(self.window.contact_list, self.window.contacts_window.list)
        self.assertIs(self.window.history_list, self.window.history_window.list)
        self.assertIs(self.window.download_panel,
                      self.window.transfer_window.download_panel)
        self.assertIs(self.window.upload_panel,
                      self.window.transfer_window.upload_panel)
        for win in (self.window.contacts_window, self.window.history_window,
                    self.window.transfer_window):
            win.close()
            self.assertFalse(win.isVisible())

    def test_transfer_list(self):
        self.window._transfer_add("dl-k1", "下载任务")
        self.window._transfer_update("dl-k1", "下载中…")
        self.assertEqual(self.window.download_panel.list.count(), 1)
        self.assertIn("下载中", self.window.download_panel.snapshot()[0]["status"])
        self.window.download_panel.set_item_progress("dl-k1", 42)
        self.assertEqual(self.window.download_panel.snapshot()[0]["percent"], 42)
        self.window._transfer_update("dl-k1", "完成")
        self.assertEqual(self.window.download_panel.snapshot()[0]["percent"], 100)
        self.window._transfer_add("up-a.mp4", "a.mp4")
        self.assertEqual(self.window.upload_panel.list.count(), 1)
        self.assertEqual(self.window.download_panel.list.count(), 1)

        # 传输面板现在住在独立窗口里，主窗口持有的就是同一批控件
        self.window._open_transfer_window()
        self.assertTrue(self.window.transfer_window.isVisible())
        self.assertIs(self.window.transfer_window.download_panel,
                      self.window.download_panel)
        self.assertEqual(self.window.transfer_window.upload_panel.snapshot()[0]["title"],
                         "a.mp4")
        self.window.transfer_window.close()

        self.window._clear_transfers()
        self.assertEqual(self.window.download_panel.list.count(), 0)
        self.assertEqual(self.window.upload_panel.list.count(), 0)

    def test_transfer_stats(self):
        self.window.download_panel.set_stats(55, 2 * 1024 * 1024, "1/2")
        self.assertEqual(self.window.download_panel.progress.value(), 55)
        self.assertEqual(self.window.download_panel.speed_label.text(), "2.00 MB/s")
        self.window.upload_panel.set_stats(120, 0, "x")
        self.assertEqual(self.window.upload_panel.progress.value(), 100)
        self.assertEqual(self.window.upload_panel.speed_label.text(), "0.00 MB/s")

    def test_five_account_login_state(self):
        for i in range(5):
            self.window._set_login_state(True, [], i)
        self.assertEqual(self.window._account_logged, [True] * 5)
        for i in range(5):
            self.assertIn("✓", self.window.account_combo.itemText(i))
        self.assertEqual(self.window.account_combo.count(), 5)

    def test_sync_button_has_no_hover_popup(self):
        from PySide6.QtCore import Qt
        self.assertEqual(self.window.contact_sync_btn.toolTip(), "")
        self.assertEqual(self.window.contact_sync_btn.focusPolicy(),
                         Qt.FocusPolicy.NoFocus)

    def test_tooltip_theme_is_light(self):
        from PySide6.QtGui import QPalette
        palette = self.qa.palette()
        self.assertEqual(palette.color(QPalette.ColorRole.ToolTipBase).name(), "#ffffff")
        self.assertEqual(palette.color(QPalette.ColorRole.ToolTipText).name(), "#111111")

    def test_legacy_webdav_state_migrates_top_level_paths(self):
        import json
        self.window._webdav = {"url": "http://example/dav", "remote_root": "净影"}
        path = self.window._webdav_state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "server": "http://example/dav", "root": "净影", "files": {},
                "Music/净影/a.mp3": "10:1",
            }, f, ensure_ascii=False)
        state = self.window._load_webdav_state()
        self.assertEqual(state["files"], {"Music/净影/a.mp3": "10:1"})

    def test_thread_setting_persists_and_window_syncs(self):
        self.window._set_threads("download", "6")
        self.assertEqual(self.window._dl_threads, 6)
        self.assertEqual(self.window.transfer_dl_combo.currentText(), "6")

        # 传输窗口里的下拉框就是主窗口那一份，改它等于改设置
        self.window.transfer_window.up_combo.setCurrentText("8")
        self.assertEqual(self.window._up_threads, 8)
        self.assertEqual(self.window.transfer_up_combo.currentText(), "8")

        # 主窗口改动后，窗口里的下拉框跟着变
        self.window._set_threads("download", "4")
        self.window._open_transfer_window()
        self.assertEqual(self.window.transfer_window.dl_combo.currentText(), "4")
        self.window.transfer_window.close()

        # 重新构造主窗口，设置应持久化
        second = self.app.MainWindow()
        try:
            self.assertEqual(second._dl_threads, 4)
            self.assertEqual(second._up_threads, 8)
            self.assertEqual(second.transfer_dl_combo.currentText(), "4")
        finally:
            second.close()
            second.deleteLater()
            self.qa.processEvents()

    def test_batch_finish_reenables_parse_button(self):
        self.window.parse_btn.setEnabled(False)
        self.window._dl_mode = "batch"
        self.window._dl_total = 1
        self.window._dl_done = 1
        self.window._dl_success = 1
        self.window._dl_fail = 0
        self.window._maybe_finish_downloads()
        self.assertIsNone(self.window._dl_mode)
        self.assertTrue(self.window.parse_btn.isEnabled())
        self.assertEqual(self.window.batch_progress.value(), 100)

    def test_history_renders_after_maintenance(self):
        """启动时先显示“载入中”占位，后台整理结束后才渲染真实记录。"""
        self.assertIn("载入中", self.window.history_title.text())
        item = store_mod.HistoryItem()
        item.aweme_id = "7525082444551310603"
        item.title = "维护后渲染的作品"
        item.author = "测试作者"
        self.window.store.add_history(item)
        self.window._maintenance_finished((0, 0))
        self.assertEqual(self.window.history_list.count(), 1)
        self.assertIn("维护后渲染的作品", self.window.history_list.item(0).text())
        self.assertIn("最近保存（1）", self.window.history_title.text())

    def test_history_ready_stage_renders_before_scan_finishes(self):
        """后台线程读完历史就通知界面渲染，不用等扫盘/修时间跑完。"""
        item = store_mod.HistoryItem()
        item.aweme_id = "7525082444551310604"
        item.title = "读完就渲染"
        item.author = "测试作者"
        self.window.store.add_history(item)
        self.window._maintenance_stage(self.window.HISTORY_READY_STAGE)
        self.assertIn("读完就渲染", self.window.history_list.item(0).text())
        # 再收到一次就绪通知不能重复渲染/跳页
        self.window._maintenance_stage(self.window.HISTORY_READY_STAGE)
        self.assertEqual(self.window.history_list.count(), 1)

    def test_maintenance_failure_still_renders_history(self):
        """后台整理抛异常时不能一直停在“载入中…”。"""
        self.window._maintenance_failed("boom")
        self.assertNotIn("载入中", self.window.history_title.text())

    def test_render_history_and_contacts(self):
        item = store_mod.HistoryItem()
        item.aweme_id = "7525082444551310602"
        item.title = "一个测试作品"
        item.author = "测试作者"
        item.video_path = os.path.join(self.tmp, "test.mp4")
        self.window.store.add_history(item)
        author = store_mod.Author("MS4wLjABAAAA_test", "测试作者", "uid1", "")
        self.window.store.add_author(author)

        self.window._render_history()
        self.assertEqual(self.window.history_list.count(), 1)
        self.assertIn("一个测试作品", self.window.history_list.item(0).text())

        self.window._render_contacts()
        self.assertEqual(self.window.contact_list.count(), 1)

    def test_show_result_without_cover(self):
        from models import MediaInfo
        info = MediaInfo()
        info.aweme_id = "1"
        info.title = "标题"
        info.author = "作者"
        info.play_url = "https://v11.douyinvod.com/x.mp4"
        self.window._show_result(info)
        self.assertTrue(self.window.result_card.isVisibleTo(self.window))
        self.assertEqual(self.window.title_label.text(), "标题")
        self.assertFalse(self.window.music_btn.isVisibleTo(self.window))

    def test_apply_cover(self):
        from PySide6.QtGui import QImage, QColor
        path = os.path.join(self.tmp, "cover.jpg")
        image = QImage(120, 60, QImage.Format.Format_RGB32)
        image.fill(QColor("#3366cc"))
        self.assertTrue(image.save(path, "JPG"))
        self.window._apply_cover(path, "")
        self.assertFalse(self.window.cover.pixmap().isNull())

    def test_cover_path(self):
        path = self.window._cover_path("a/b")
        self.assertTrue(path.endswith("a_b.jpg"))

    def test_rebuild_history_from_disk(self):
        folder = os.path.join(config.DOWNLOAD_BASE, "作者_uid",
                              "作者_标题_20250709_204333")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "视频.mp4"), "wb") as f:
            f.write(b"x")
        downloader.write_caption_file(folder, "作者", "uid", "标题",
                                      "7525082444551310602", "", 1752069813)
        added = self.window._rebuild_history_from_disk()
        self.assertEqual(added, 1)
        items = self.window.store.load_history()
        self.assertEqual(items[0].aweme_id, "7525082444551310602")
        self.assertEqual(items[0].author, "作者")

    def test_stale_download_callbacks_do_not_crash(self):
        """上一轮遗留的下载回调（索引越界）不能再抛 IndexError。

        PySide6 新版对槽函数里的未捕获异常会直接终止进程；停止同步后立刻重开时
        这种情况是真实会出现的。
        """
        self.window._dl_mode = "sync"
        self.window._dl_jobs = []
        self.window._dl_total = 2
        self.window._dl_item_done(5, object())
        self.window._dl_item_error(9, "boom")
        self.window._dl_item_progress(7, 50)
        self.window._dl_item_started(3)
        self.assertEqual(self.window._dl_done, 0)
        self.assertEqual(self.window._dl_fail, 0)

    def test_transfer_keys_change_between_rounds(self):
        """传输列表的行 key 不能靠 id(list)：每轮必须不一样。"""
        from models import MediaInfo
        info = MediaInfo()
        info.aweme_id = "1"
        info.author = "作者"
        self.window._dl_mode = "sync"
        self.window._dl_jobs = [(info, "作者目录", "作品目录")]
        first = self.window._dl_key(0)
        self.window._dl_run += 1
        second = self.window._dl_key(0)
        self.assertNotEqual(first, second)

    def test_clear_history_hides_load_more_button(self):
        """清空历史后「加载更多」按钮必须隐藏（以前会残留还能点）。"""
        for i in range(3):
            item = store_mod.HistoryItem()
            item.aweme_id = str(i)
            item.title = f"作品 {i}"
            item.author = "作者"
            self.window.store.add_history(item)
        self.window.history_page = 20
        self.window._render_history(reset=False)
        self.window.store.clear_history()
        self.window._render_history()
        self.assertFalse(self.window.history_more_btn.isVisibleTo(self.window))

    def test_browser_busy_reason(self):
        """没浏览器 / 登录窗口开着 / 正在查登录时都要给出可读原因。"""
        import config as cfg
        saved = cfg.CHROME_PATH, self.app.parser.CHROME_PATH
        try:
            cfg.CHROME_PATH = ""
            self.app.parser.CHROME_PATH = ""
            self.assertIn("Chrome", self.window._browser_busy_reason())
            cfg.CHROME_PATH = "C:/chrome.exe"
            self.app.parser.CHROME_PATH = "C:/chrome.exe"
            self.assertEqual(self.window._browser_busy_reason(), "")
            self.window._login_running = True
            self.assertIn("登录", self.window._browser_busy_reason())
        finally:
            cfg.CHROME_PATH, self.app.parser.CHROME_PATH = saved
            self.window._login_running = False

    def test_normalize_file_times_skips_correct_files(self):
        ts = 1752069813
        target = os.path.join(self.tmp, "already.mp4")
        with open(target, "wb") as f:
            f.write(b"x")
        downloader._set_file_times(target, ts)
        item = store_mod.HistoryItem()
        item.aweme_id = "1"
        item.video_path = target
        item.create_time = ts
        self.window.store.add_history(item)
        self.assertEqual(self.window._normalize_file_times(), 0)

        wrong = os.path.join(self.tmp, "wrong.mp4")
        with open(wrong, "wb") as f:
            f.write(b"x")
        os.utime(wrong, (ts + 86400, ts + 86400))
        item2 = store_mod.HistoryItem()
        item2.aweme_id = "2"
        item2.video_path = wrong
        item2.create_time = ts
        self.window.store.add_history(item2)
        self.assertGreaterEqual(self.window._normalize_file_times(), 1)
        self.assertLessEqual(abs(os.path.getmtime(wrong) - ts), 2)


class ParserUnitTests(unittest.TestCase):
    def test_login_cookie_detection(self):
        import parser
        self.assertTrue(parser._has_login_cookie([{"name": "sessionid", "value": "x"}]))
        self.assertTrue(parser._has_login_cookie([{"name": "sid_tt", "value": "x"}]))
        self.assertFalse(parser._has_login_cookie([{"name": "passport_csrf_token", "value": "x"}]))

    def test_resolver_cached(self):
        import parser
        first = parser._resolver_js()
        self.assertIn("__JY_STATE__", first)
        self.assertIs(parser._resolver_js(), first)


if __name__ == "__main__":
    unittest.main()
