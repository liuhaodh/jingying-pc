# -*- coding: utf-8 -*-
"""通讯录同步用的增量 WebDAV 上传队列。"""
import os
import time
import unittest

from tests.support import isolated_config

from auto_upload import AutoUploader


class FakeClient:
    def __init__(self):
        self.files = []

    def upload_file(self, local_path, rel_path, on_bytes=None):
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(128 * 1024)
                if not chunk:
                    break
                if on_bytes:
                    on_bytes(len(chunk))
        self.files.append(rel_path)


class AutoUploaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def _wait(self, predicate, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.qa.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.qa.processEvents()
        return predicate()

    def test_uploads_queued_files(self):
        files = {}
        for name, data in (("a.mp4", b"a" * 4096), ("b.mp4", b"b" * 8192)):
            path = os.path.join(self.tmp, name)
            with open(path, "wb") as f:
                f.write(data)
            files[name] = path
        client = FakeClient()
        uploader = AutoUploader(client, self.tmp, state={}, max_workers=2)
        finished = []
        progress = []
        uploader.finished.connect(lambda ok, skipped, failed: finished.append(
            (ok, skipped, failed)))
        uploader.progress.connect(lambda done, total: progress.append((done, total)))
        uploader.start()
        self.assertEqual(uploader.enqueue(list(files.values())), 2)
        uploader.close()
        self.assertTrue(self._wait(lambda: bool(finished)))
        self.assertEqual(finished[0], (2, 0, 0))
        self.assertEqual(sorted(client.files), ["a.mp4", "b.mp4"])
        # 两个上传线程在锁外各自 emit，到达顺序不保证：后完成的那个可能先 emit。
        # 界面侧 `_on_upload_progress` 用 max() 保证只前进，所以这里断言「报过终值」
        # 就够了；断言「最后一条是终值」会和线程调度赛跑（全量跑时偶发失败）。
        self.assertTrue(self._wait(
            lambda: (4096 + 8192, 4096 + 8192) in progress))
        self.assertEqual(max(done for done, _total in progress), 4096 + 8192)

    def test_skips_uploaded_unchanged_file(self):
        path = os.path.join(self.tmp, "a.mp4")
        with open(path, "wb") as f:
            f.write(b"x" * 1000)
        fingerprint = f"{os.path.getsize(path)}:{int(os.path.getmtime(path))}"
        client = FakeClient()
        uploader = AutoUploader(client, self.tmp,
                                state={"a.mp4": fingerprint}, max_workers=1)
        finished = []
        uploader.finished.connect(lambda ok, skipped, failed: finished.append(
            (ok, skipped, failed)))
        uploader.start()
        uploader.enqueue([path])
        uploader.close()
        self.assertTrue(self._wait(lambda: bool(finished)))
        self.assertEqual(finished[0], (0, 1, 0))
        self.assertEqual(client.files, [])


    def test_byte_signals_survive_more_than_2gb(self):
        """累计上传超过 2GB 时，Qt 的 32 位 int 信号会抛 OverflowError。

        真实症状：一次长同步里 emit 9.4GB 后，每次都报
        ``OverflowError: Value 9496286729 exceeds limits of type "int"``，
        上传进度条从此不再更新。字节类信号必须用 qint64。
        """
        uploader = AutoUploader(FakeClient(), self.tmp, state={}, max_workers=1)
        got = {}
        uploader.progress.connect(
            lambda done, total: got.setdefault("progress", (done, total)))
        uploader.bytes_sent.connect(lambda n: got.setdefault("bytes", n))
        uploader.file_progress.connect(
            lambda rel, done, total: got.setdefault("file", (rel, done, total)))
        uploader.started.connect(
            lambda rel, size: got.setdefault("started", (rel, size)))
        uploader.file_done.connect(
            lambda rel, status, size: got.setdefault("done", (rel, status, size)))
        seen = 9496286729          # 现场溢出的那个累计字节数
        total = 12 * 1024 ** 3
        uploader.progress.emit(seen, total)
        uploader.bytes_sent.emit(seen)
        uploader.file_progress.emit("a.mp4", seen, total)
        uploader.started.emit("a.mp4", total)
        uploader.file_done.emit("a.mp4", "done", total)
        self.assertEqual(got["progress"], (seen, total))
        self.assertEqual(got["bytes"], seen)
        self.assertEqual(got["file"], ("a.mp4", seen, total))
        self.assertEqual(got["started"], ("a.mp4", total))
        self.assertEqual(got["done"], ("a.mp4", "done", total))


    def test_failure_reports_reason(self):
        """上传失败必须带上真实原因，否则界面只有「失败 N 个」没法排查。"""
        class BrokenClient:
            def upload_file(self, local_path, rel_path, on_bytes=None):
                raise RuntimeError("HTTP 409 创建远程目录失败 净影")

        path = os.path.join(self.tmp, "a.mp4")
        with open(path, "wb") as f:
            f.write(b"x" * 100)
        uploader = AutoUploader(BrokenClient(), self.tmp, state={}, max_workers=1)
        details = []
        uploader.file_failed.connect(lambda rel, msg: details.append((rel, msg)))
        uploader.start()
        uploader.enqueue([path])
        uploader.close()
        self.assertTrue(self._wait(lambda: bool(details)))
        self.assertEqual(details[0][0], "a.mp4")
        self.assertIn("HTTP 409", details[0][1])


if __name__ == "__main__":
    unittest.main()
