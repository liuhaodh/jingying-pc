# -*- coding: utf-8 -*-
"""长跑两个问题的回归：CDN 轮换域名误拒、同步时主线程反复卡顿。"""
import inspect
import os
import types
import unittest
from unittest import mock

from tests.support import isolated_config

import downloader
import transfer

# 全部来自 2026-11-15 全量同步的真实日志：都曾被旧逻辑判为「跳转到不允许的域名」
REAL_CDN_URLS = [
    "https://7a9e2f50.ydycdn.cn:58001/316535d2d7335161cbadfd86317fd4ad/6afc0254/"
    "video/tos/cn/tos-cn-v-0015c800/oIAfKtbe7VIuCGBGCjGb06L1A1"
    "?adi=JnNwPVVOSSZzZj1TRCZ5bT12ODItbmNkbi1jb2xkLmRvdXlpbnZvZC5jb20"
    "&cts=0&nqck=0&real_host=djgyLW5jZG4tY29sZC5kb3V5aW52b2QuY29t"
    "&x_r_id=01bca914d14e5f3a91b124997ad3b7d9&ydyv=v1.1.6",
    "https://4b19a02d-9.sjxydc.com/e274c38d4b533a579623363015fd6a82/6afc027a/"
    "video/tos/cn/tos-cn-v-0015c000-ce/oAEgA1KBSitiAqiezXEAPvA"
    "?cts=0&nqck=0&real_host=djktbmNkbi1jb2xkLmRvdXlpbnZvZC5jb20="
    "&x_r_id=e06cee7c091c5ad237f0eec005eafdea",
    "https://3c0e5888.ydycdn.cn:58001/5ace7d973a2202262290d871e376166e/6afc0278/"
    "video/tos/cn/tos-cn-v-0015c000-ce/ooEhGepBBIYQIQ8CjAVgLD2"
    "?adi=JnNwPVVOSSZzZj1TRCZ5bT12ODItbmNkbi1jb2xkLmRvdXlpbnZvZC5jb20"
    "&real_host=djgyLW5jZG4tY29sZC5kb3V5aW52b2QuY29t&x_r_id=e308f9e6",
]

# 重定向链上的节点：没有任何来源参数，靠 302 兜底放行
REDIRECT_ONLY_URL = (
    "https://down-updatekh18.hbttl.cn/video/tos/cn/tos-cn-v-0015c004/"
    "o4AEgEqA9GoEqA8f2A39DvaPeR2DABFnN9gABD/?dy_q=1794729314&dy_va_biz_cert=")

FORGED_URLS = [
    "https://evil.example.com/video/tos/steal",
    "https://evil.example.com/video/tos/cn/tos-cn-v-0015c800/x",
    "https://evil.example.com/normal/movie.mp4",
    "https://1.2.3.4/video/tos/cn/tos-cn-v-0015c800/x?dy_q=1",
    "http://localhost/video/tos/cn/tos-cn-v-0015c800/x?dy_q=1",
]


class CdnAllowTests(unittest.TestCase):
    def test_real_rotating_cdns_are_allowed(self):
        for url in REAL_CDN_URLS:
            with self.subTest(url=url[:60]):
                self.assertTrue(downloader._is_allowed_host(url))

    def test_b64_real_host_decodes_to_douyin_origin(self):
        decoded = downloader._maybe_b64(
            "djgyLW5jZG4tY29sZC5kb3V5aW52b2QuY29t")
        self.assertIn(".douyinvod.com", decoded)
        self.assertIn(
            ".douyinvod.com",
            downloader._maybe_b64("JnNwPVVOSSZzZj1TRCZ5bT12ODItbmNkbi1jb2xkLmRvdXlpbnZvZC5jb20"))

    def test_legacy_hints_still_work(self):
        # 旧逻辑认的 http_x-tos-host 与路径内主机名不能被改坏
        self.assertTrue(downloader._is_allowed_host(
            "https://cdn.example.net/video/tos/x?http_x-tos-host=v.douyinvod.com"))
        self.assertTrue(downloader._is_allowed_host(
            "https://cdn.example.net/obj/v.douyinvod.com/video/tos/x"))

    def test_plain_whitelisted_host_still_allowed(self):
        self.assertTrue(downloader._is_allowed_host(
            "https://api.douyin.com/aweme/v1/play/whatever"))

    def test_redirect_fallback_for_gsbl_without_hints(self):
        self.assertFalse(downloader._is_allowed_host(REDIRECT_ONLY_URL))
        self.assertTrue(downloader._is_allowed_redirect(REDIRECT_ONLY_URL))

    def test_forged_urls_are_rejected_everywhere(self):
        for url in FORGED_URLS:
            with self.subTest(url=url[:60]):
                self.assertFalse(downloader._is_allowed_host(url))
                self.assertFalse(downloader._is_allowed_redirect(url))


class _FakeResponse:
    def __init__(self, final_url, history_urls, payload=b"JYDATA" * 100):
        self.url = final_url
        self.history = [types.SimpleNamespace(url=u) for u in history_urls]
        self.status_code = 200
        self.headers = {"Content-Length": str(len(payload))}
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=256 * 1024):
        yield self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, *_args, **_kwargs):
        return self._response


class RedirectChainTests(unittest.TestCase):
    """端到端：可信初始 URL 302 到轮换 CDN 时不再抛「不允许的域名」。"""

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def _run(self, final_url, history_urls):
        target = os.path.join(self.tmp, "clip.mp4")
        resp = _FakeResponse(final_url, history_urls)
        with mock.patch.object(downloader, "_get_session",
                               return_value=_FakeSession(resp)):
            return downloader._download_to_file(
                "https://www.douyin.com/aweme/v1/play/vid", target, None, 0, 1), target

    def test_gsbl_redirect_is_downloadable(self):
        size, target = self._run(REDIRECT_ONLY_URL, [REAL_CDN_URLS[0]])
        self.assertGreater(size, 0)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"JYDATA" * 100)

    def test_forged_redirect_still_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._run("https://evil.example.com/normal/movie.mp4", [])
        self.assertIn("不允许的域名", str(ctx.exception))


class TransferBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qa = QApplication.instance() or QApplication([])

    def test_single_add_is_still_synchronous(self):
        panel = transfer.TransferPanel("下载")
        panel.add_item("a", "作品A")
        self.assertEqual(panel.list.count(), 1)
        self.assertTrue(panel.list.updatesEnabled())

    def test_batch_adds_all_rows_in_one_pass(self):
        panel = transfer.TransferPanel("下载")
        panel.add_items_batch([(f"k{i}", f"作品{i}") for i in range(10)])
        self.assertEqual(panel.list.count(), 10)
        self.assertEqual(panel.item_keys(),
                         [f"k{i}" for i in range(9, -1, -1)])
        self.assertTrue(panel.list.updatesEnabled())

    def test_batch_over_limit_reuses_rows_without_building_new_ones(self):
        panel = transfer.TransferPanel("下载")
        for i in range(transfer.MAX_VISIBLE_ROWS):
            panel.add_item(f"k{i}", f"作品{i}")
        with mock.patch.object(panel, "_build_row",
                               wraps=panel._build_row) as build:
            panel.add_items_batch([(f"n{i}", f"新{i}") for i in range(25)])
        self.assertEqual(panel.list.count(), transfer.MAX_VISIBLE_ROWS)
        self.assertEqual(len(panel._rows), transfer.MAX_VISIBLE_ROWS)
        self.assertEqual(build.call_count, 0)
        self.assertTrue(panel.list.updatesEnabled())

    def test_batch_with_empty_entries_is_noop(self):
        panel = transfer.TransferPanel("下载")
        panel.add_items_batch([])
        self.assertEqual(panel.list.count(), 0)
        self.assertTrue(panel.list.updatesEnabled())


class WiringTests(unittest.TestCase):
    def test_item_started_queues_instead_of_touching_widgets(self):
        import app
        src = inspect.getsource(app.MainWindow._dl_item_started)
        self.assertIn("_queue_transfer_add", src)
        self.assertNotIn("self._transfer_add(", src)

    def test_flush_is_called_before_any_row_lookup(self):
        import app
        for name in ("_dl_item_progress", "_dl_item_done", "_dl_item_error",
                     "_dl_item_skipped", "_maybe_finish_downloads",
                     "_begin_download_stats"):
            src = inspect.getsource(getattr(app.MainWindow, name))
            self.assertIn("_flush_transfer_adds", src, name)

    def test_progress_signal_throttle_is_relaxed(self):
        import parallel
        src = inspect.getsource(parallel.DownloadManager._emit_progress)
        self.assertIn("< 0.25", src)


if __name__ == "__main__":
    unittest.main()
