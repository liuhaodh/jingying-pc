# -*- coding: utf-8 -*-
"""上传后删除本地媒体：主线程只入队，真正的 os.remove 在后台线程。

回归背景：同步时每上传成功一个文件就在主线程删一个文件，某些环境的安全
删除校验会让单次 os.remove 耗时数秒，看门狗记录到连续 UI stall 13~36s。
"""
import threading
import time
import unittest
from unittest import mock

import webdav_panel


class _FakeCheckBox:
    def __init__(self, checked=True):
        self.checked = checked

    def isChecked(self):
        return self.checked


class _Host(webdav_panel.WebdavMixin):
    """不创建 QWidget 的最小宿主，只提供被测方法需要的状态。"""

    def __init__(self, delete_enabled=True):
        self._webdav = {}
        self._uploaded_units_cache = None
        self.webdav_delete_cb = _FakeCheckBox(delete_enabled)
        self._upgrade_delete_paths = set()


class LocalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.host = _Host()
        self.calls = []

    def tearDown(self):
        self.host.shutdown_local_cleanup()

    def _record_delete(self, delay=0.0):
        def fake(rel):
            if delay:
                time.sleep(delay)
            self.calls.append((rel, threading.current_thread().name))
        return fake

    def test_upload_delete_runs_off_main_thread(self):
        """上传完成的回调（主线程）不能自己删文件，全部交给后台线程。"""
        with mock.patch.object(
                self.host, "_delete_uploaded_media",
                side_effect=self._record_delete()):
            for i in range(20):
                self.host._delete_after_upload(f"净影/作者/作品{i}/视频.mp4")
            self.host.shutdown_local_cleanup()
        self.assertEqual(len(self.calls), 20)
        self.assertTrue(
            all(thread == "jy-local-cleanup" for _, thread in self.calls),
            "删除文件的调用必须全部发生在后台线程")

    def test_delete_switch_off_queues_nothing(self):
        host = _Host(delete_enabled=False)
        try:
            with mock.patch.object(
                    host, "_delete_uploaded_media",
                    side_effect=self._record_delete()):
                host._delete_after_upload("净影/作者/作品/视频.mp4")
                host.shutdown_local_cleanup()
            self.assertEqual(self.calls, [])
        finally:
            host.shutdown_local_cleanup()

    def test_duplicate_rel_is_deduped(self):
        """同一个文件重复收到上传完成信号时不重复删。"""
        with mock.patch.object(
                self.host, "_delete_uploaded_media",
                side_effect=self._record_delete(delay=0.05)):
            for _ in range(6):
                self.host._delete_after_upload("净影/作者/作品/视频.mp4")
            self.host.shutdown_local_cleanup()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "净影/作者/作品/视频.mp4")

    def test_upgrade_delete_only_removes_key_in_main_thread(self):
        """低画质重下的副本：集合摘除在主线程，删文件在后台。"""
        rel = "净影/作者/作品/视频.mp4"
        self.host._upgrade_delete_paths = {rel, "净影/作者/其他/视频.mp4"}
        with mock.patch.object(
                self.host, "_delete_uploaded_media",
                side_effect=self._record_delete()):
            self.host._delete_upgraded_local(rel)
            self.host.shutdown_local_cleanup()
        self.assertEqual(self.host._upgrade_delete_paths,
                         {"净影/作者/其他/视频.mp4"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "jy-local-cleanup")

    def test_one_failure_does_not_stop_the_queue(self):
        state = {"n": 0}

        def flaky(rel):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("safe-delete hook denied")
            self.calls.append((rel, threading.current_thread().name))

        with mock.patch.object(
                self.host, "_delete_uploaded_media", side_effect=flaky), \
                mock.patch("webdav_panel.logging.exception"):
            for i in range(3):
                self.host._delete_after_upload(f"净影/作者/作品{i}/视频.mp4")
            self.host.shutdown_local_cleanup()
        self.assertEqual(len(self.calls), 2)

    def test_slow_deletes_do_not_block_main_thread(self):
        """删除本身很慢（安全钩子）时，上传完成回调仍应立即返回。"""
        with mock.patch.object(
                self.host, "_delete_uploaded_media",
                side_effect=self._record_delete(delay=0.05)):
            started = time.monotonic()
            for i in range(8):
                self.host._delete_after_upload(f"净影/作者/作品{i}/视频.mp4")
            elapsed = time.monotonic() - started
            self.host.shutdown_local_cleanup()
        self.assertLess(elapsed, 0.2, "上传完成回调不能等后台删除")
        self.assertEqual(len(self.calls), 8)

    def test_shutdown_is_idempotent(self):
        with mock.patch.object(self.host, "_delete_uploaded_media",
                               side_effect=self._record_delete()):
            self.host._delete_after_upload("净影/作者/作品/视频.mp4")
            self.host.shutdown_local_cleanup()
            self.host.shutdown_local_cleanup()


if __name__ == "__main__":
    unittest.main()
