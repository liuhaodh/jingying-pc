# -*- coding: utf-8 -*-
"""并行下载队列：支持同步过程中持续追加作者作品。"""
import time
import types
import unittest
from unittest import mock

from tests.support import isolated_config  # noqa: F401  （确保 Qt/路径环境已初始化）

from PySide6.QtWidgets import QApplication

import parallel


class DownloadManagerQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def test_enqueue_after_begin_processes_all_jobs(self):
        done = []

        def fake_download(info, author_folder, work_folder, **_kwargs):
            time.sleep(0.02)
            return types.SimpleNamespace(video_path="", music_path="",
                                         album_paths=[], live_paths=[])

        dm = parallel.DownloadManager(2)
        dm.item_done.connect(lambda index, _result: done.append(index))
        jobs = [(object(), "author", "work") for _ in range(3)]
        with mock.patch.object(parallel.downloader, "download_work", fake_download):
            dm.begin(2)
            dm.enqueue(jobs[:2], start_index=0)
            dm.enqueue(jobs[2:], start_index=2)
            deadline = time.time() + 5
            while len(done) < 3 and time.time() < deadline:
                self.qa.processEvents()
                time.sleep(0.01)
            dm.stop()
        self.assertEqual(sorted(done), [0, 1, 2])

    def test_stale_round_results_are_dropped(self):
        """停止后立刻重开一轮：上一轮迟到的结果不能写进新一轮。

        「停止同步」不会杀线程，单次下载请求最长还能再跑 60 秒；这些任务带着
        旧索引回来时，以前会把作品 A 的结果写成新一轮作品 B 的历史，并让
        _dl_done 虚增、同步提前收尾。
        """
        done = []
        errors = []

        def slow_download(info, author_folder, work_folder, **_kwargs):
            time.sleep(0.25)
            return types.SimpleNamespace(video_path="", music_path="",
                                         album_paths=[], live_paths=[])

        dm = parallel.DownloadManager(2)
        dm.item_done.connect(lambda index, _result: done.append(index))
        dm.item_error.connect(lambda index, _message: errors.append(index))
        with mock.patch.object(parallel.downloader, "download_work", slow_download):
            dm.begin(2)
            dm.enqueue([(object(), "author", "work")], start_index=0)
            time.sleep(0.05)
            # 等价于「停止同步 → 立刻重新同步」：同一批索引重新开始
            dm.begin(2)
            dm.enqueue([(object(), "author", "work")], start_index=0)
            deadline = time.time() + 3
            while len(done) < 1 and time.time() < deadline:
                self.qa.processEvents()
                time.sleep(0.01)
            dm.stop()
        self.assertEqual(done, [0])
        self.assertEqual(errors, [])

    def test_emit_progress_is_per_work_and_monotonic(self):
        """作品进度：文件序号换算到整条轴，且同一作品只前进、不倒退。"""
        dm = parallel.DownloadManager(1)
        got = []
        dm.item_progress.connect(lambda _index, value: got.append(value))
        # 一个作品 2 个文件：第一个 0→100% 占 0-50%，第二个占 50-100%
        dm._emit_progress(0, 0, 0, 2)
        # 测试里两次调用几乎同时，节流会拦掉中间值；这里手动放行
        dm._last_progress_time[0] = 0.0
        dm._emit_progress(0, 100, 0, 2)
        dm._emit_progress(0, 100, 1, 2)  # 100% 必发
        # 倒退的进度（例如重试从 0 开始）应被忽略
        dm._emit_progress(0, 10, 0, 2)
        self.assertEqual(got, [0, 50, 100])
        # 越界也钳到 100
        dm._emit_progress(1, 100, 5, 2)
        self.assertEqual(got[-1], 100)


if __name__ == "__main__":
    unittest.main()
