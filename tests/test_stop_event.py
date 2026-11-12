# -*- coding: utf-8 -*-
"""协作式退出（stop_event）的回归用例。

覆盖三层：Worker 的信号注入与静默取消、parser 的检查点与可中断等锁、
MainWindow.closeEvent 的「通知 → 等待 → 窄兜底」。
"""
import asyncio
import inspect
import threading
import time
import unittest
from unittest import mock

import parser
import workers
from tests.support import isolated_config


class WorkerStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def test_cancel_event_and_stage_are_injected(self):
        seen = {}

        def task(cancel_event=None, stage=None):
            seen["event"] = cancel_event
            seen["stage"] = stage
            return "ok"

        w = workers.Worker(task)
        results, failures = [], []
        w.done.connect(results.append)
        w.failed.connect(failures.append)
        w.start()
        self.assertTrue(w.wait(3000))
        self.qa.processEvents()
        self.assertEqual(results, ["ok"])
        self.assertEqual(failures, [])
        self.assertIs(seen["event"], w.stop_event)
        self.assertTrue(callable(seen["stage"]))
        w.deleteLater()

    def test_stop_event_alias_is_injected(self):
        seen = {}

        def task(stop_event):
            seen["e"] = stop_event

        w = workers.Worker(task)
        w.start()
        self.assertTrue(w.wait(3000))
        self.assertIs(seen["e"], w.stop_event)
        w.deleteLater()

    def test_task_without_cancel_param_keeps_old_behavior(self):
        def task():
            return 42

        w = workers.Worker(task)
        results = []
        w.done.connect(results.append)
        w.start()
        self.assertTrue(w.wait(3000))
        self.qa.processEvents()
        self.assertEqual(results, [42])
        w.deleteLater()

    def test_request_stop_runs_finally(self):
        cleaned = threading.Event()

        def task(cancel_event):
            try:
                while not cancel_event.is_set():
                    time.sleep(0.01)
            finally:
                cleaned.set()

        w = workers.Worker(task)
        w.start()
        time.sleep(0.05)
        w.request_stop()
        self.assertTrue(w.wait(3000))
        self.assertTrue(cleaned.is_set(), "协作退出必须让 finally 跑完")
        w.deleteLater()

    def test_cancelled_exception_does_not_emit_failed(self):
        def task(cancel_event):
            while not cancel_event.is_set():
                time.sleep(0.01)
            raise parser.ParseCancelled("已取消")

        w = workers.Worker(task)
        failures = []
        w.failed.connect(failures.append)
        w.start()
        time.sleep(0.05)
        w.request_stop()
        self.assertTrue(w.wait(3000))
        self.qa.processEvents()
        self.assertEqual(failures, [], "关窗时不应再弹「失败」提示")
        w.deleteLater()

    def test_real_failure_still_emits_failed(self):
        def task(cancel_event):
            raise ValueError("真实错误")

        w = workers.Worker(task)
        failures = []
        w.failed.connect(failures.append)
        w.start()
        self.assertTrue(w.wait(3000))
        self.qa.processEvents()
        self.assertEqual(failures, ["真实错误"])
        w.deleteLater()


class ParserCancelTests(unittest.TestCase):
    def test_check_cancel(self):
        event = threading.Event()
        parser._check_cancel(None)
        parser._check_cancel(event)
        event.set()
        with self.assertRaises(parser.ParseCancelled):
            parser._check_cancel(event)

    def test_resolvers_expose_cancel_event(self):
        for fn in (parser.resolve_video, parser.resolve_profile,
                   parser.resolve_video_sync, parser.resolve_profile_sync):
            self.assertIn("cancel_event", inspect.signature(fn).parameters,
                          f"{fn.__name__} 缺少 cancel_event 参数")

    def test_browser_lock_wait_is_interruptible(self):
        """占住配置目录锁后，已置位的 stop_event 必须让等锁协程立刻放弃。"""
        session_dir = "unit-test-locked-session"
        lock = parser._lock_for(session_dir)
        self.assertTrue(lock.acquire(timeout=1))
        event = threading.Event()
        event.set()

        @parser._serialized_browser
        async def fake(cancel_event=None, session_dir=None):
            return "should-not-run"

        try:
            start = time.time()
            with self.assertRaises(parser.ParseCancelled):
                asyncio.run(fake(cancel_event=event, session_dir=session_dir))
            self.assertLess(time.time() - start, 1.0)
        finally:
            lock.release()

    def test_browser_lock_normal_path_unaffected(self):
        @parser._serialized_browser
        async def fake(cancel_event=None, session_dir=None):
            return "ran"

        result = asyncio.run(fake(cancel_event=threading.Event(),
                                  session_dir="unit-test-free-session"))
        self.assertEqual(result, "ran")


class _FakeInfo:
    aweme_id = "unit-test-aweme"
    author_id = "uid"
    unique_id = ""
    cover_url = ""
    share_url = ""
    create_time = 0
    location = ""
    ip_location = ""
    music_url = ""

    def display_title(self):
        return "单元测试标题"

    def safe_author(self):
        return "单元测试作者"

    def is_album(self):
        return False


class CloseEventStopTests(unittest.TestCase):
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
        try:
            self.window.close()
        except Exception:  # noqa: BLE001
            pass
        self.window.deleteLater()
        self.qa.processEvents()
        self.app.MainWindow._check_login = self._old_check
        self.app.MainWindow._start_maintenance = self._old_maint
        self._ctx.__exit__(None, None, None)

    def test_close_notifies_worker_without_terminating(self):
        started = threading.Event()
        cleaned = threading.Event()

        def task(cancel_event):
            started.set()
            try:
                while not cancel_event.is_set():
                    time.sleep(0.01)
            finally:
                cleaned.set()

        w = workers.Worker(task)
        self.window._track_worker(w)
        w.start()
        self.assertTrue(started.wait(3))

        with mock.patch("logging.warning") as warn:
            self.window.close()

        self.assertTrue(cleaned.is_set(), "关窗时线程必须跑完 finally")
        self.assertFalse(w.isRunning())
        self.assertFalse(any("未响应停止请求" in str(call)
                             for call in warn.call_args_list))
        w.deleteLater()

    def test_download_task_receives_cancel_event(self):
        import downloader
        started = threading.Event()
        captured = {}

        def fake_download(info, author_folder, work_folder, **kwargs):
            event = kwargs.get("cancel_event")
            captured["event"] = event
            started.set()
            while event is not None and not event.is_set():
                time.sleep(0.01)
            raise downloader.DownloadCancelled("下载已取消")

        with mock.patch.object(downloader, "download_work",
                               side_effect=fake_download):
            self.window._download_work(_FakeInfo(), "作者_uid", "作品目录")
            self.assertTrue(started.wait(3))
            self.assertIsInstance(captured["event"], threading.Event)
            self.window.close()

        self.assertTrue(captured["event"].is_set(),
                        "停止信号必须贯通到 download_work")

    def test_unresponsive_worker_still_falls_back_to_terminate(self):
        class FakeWorker:
            def __init__(self):
                self.running = True
                self.stopped = False
                self.terminated = False

            def isRunning(self):
                return self.running

            def request_stop(self):
                self.stopped = True

            def wait(self, _ms):
                return True

            def terminate(self):
                self.terminated = True
                self.running = False

        fake = FakeWorker()
        self.window._workers.append(fake)
        with self.assertLogs(level="WARNING") as logs:
            self.window.close()
        self.assertTrue(any("未响应停止请求" in m for m in logs.output),
                        "兜底强杀必须留下日志")
        self.assertTrue(fake.stopped, "必须先发协作停止通知")
        self.assertTrue(fake.terminated, "无响应线程仍要有兜底")
