# -*- coding: utf-8 -*-
"""v1.6.4 回归：批量同步（多线程下载）时原画质回捞必须可用。

历史缺陷：Playwright sync 会话绑定创建它的线程，8 个下载线程复用同一个
OriginalSession 会抛 "Cannot switch to a different thread"，触发 2 分钟冷却后
整轮同步静默回退最高转码档。修复方式是把会话收敛到一条专属 daemon 线程。
"""
import threading
import types
import unittest

from tests.support import isolated_config  # noqa: F401 - 初始化 Qt/路径环境

import config  # noqa: E402
import downloader  # noqa: E402
import original_stream as oq  # noqa: E402

CDN = "https://v3-default.douyinvod.com/original_upload.mp4"


class ProbeSession:
    """替代真实 Chromium 会话：只记录自己被调用的线程。"""

    def __init__(self, exc=None):
        self.exc = exc
        self.tids = []
        self.close_tid = None
        self.calls = []

    def resolve(self, video_id, best_size=0, force=False):
        self.tids.append(threading.get_ident())
        self.calls.append((video_id, best_size, force))
        if self.exc is not None:
            raise self.exc
        return oq.OriginalStream(
            video_id=video_id, url=CDN, size=22_402_202,
            best_size=best_size, factor=3.724, verdict="original",
            content_type="video/mp4", via="play")

    def close(self):
        self.close_tid = threading.get_ident()


def make_info(i):
    return types.SimpleNamespace(
        video_id=f"v{i}", aweme_id=str(7703135432608072339 + i),
        play_url=f"https://v26-default.douyinvod.com/transcoded_{i}.mp4",
        best_gear_size=6_015_200, original_url="", original_size=0,
        original_verdict="", quality="1080P 最高档",
        ladder_quality="1080P 最高档")


class OriginalSessionThreadTests(unittest.TestCase):
    def setUp(self):
        oq.set_enabled(True)
        oq._FAILURE_UNTIL = 0.0
        oq._cache.clear()
        self.probe = ProbeSession()
        oq._session = self.probe
        downloader.set_original_resolver(None)

    def tearDown(self):
        oq.set_enabled(False)
        oq._FAILURE_UNTIL = 0.0
        oq._session = None
        oq._cache.clear()
        downloader.set_original_resolver(None)

    def test_all_download_threads_run_in_one_session_thread(self):
        """8 个下载线程并发回捞：会话始终在同一线程被使用，且都拿到原片。"""
        results = {}
        lock = threading.Lock()

        def run(i):
            stream = oq.resolve_for_download(f"v{i}", 6_015_200)
            with lock:
                results[i] = stream

        threads = [threading.Thread(target=run, args=(i,), name=f"dl-{i}")
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(len(results), 8)
        self.assertTrue(all(s is not None and s.is_original for s in results.values()))
        self.assertEqual(len(self.probe.tids), 8)
        self.assertEqual(len(set(self.probe.tids)), 1,
                         "Playwright sync 会话必须在唯一线程里复用")
        self.assertNotIn(self.probe.tids[0],
                         [threading.get_ident()] + [t.ident for t in threads])

    def test_close_runs_in_session_thread(self):
        oq.resolve_for_download("v1", 6_015_200)
        oq.close_session()
        self.assertEqual(self.probe.close_tid, self.probe.tids[0])
        self.assertIsNone(oq._session)

    def test_session_failure_is_reraised_and_cools_down(self):
        oq._session = ProbeSession(exc=oq.SessionFailure("未登录"))
        with self.assertRaises(oq.SessionFailure):
            oq.resolve_for_download("v9", 6_015_200)
        self.assertGreater(oq._FAILURE_UNTIL, 0)
        # 冷却期内不再打扰会话，静默回退最高转码档
        self.assertIsNone(oq.resolve_for_download("v10", 6_015_200))
        self.assertEqual(len(oq._session.calls), 1)

    def test_single_work_soft_failure_does_not_cool_down(self):
        """CDN 头异常只影响当前作品，不能让之后两分钟全部回退转码档。"""
        soft = ProbeSession(exc=oq.OriginalStreamError("断言 4 失败：CDN 头异常"))
        oq._session = soft
        self.assertIsNone(oq.resolve_for_download("v11", 6_015_200))
        self.assertEqual(oq._FAILURE_UNTIL, 0.0)
        self.assertIsNone(oq.resolve_for_download("v12", 6_015_200))
        self.assertEqual(len(soft.calls), 2, "下一个作品仍应尝试回捞")

    def test_batch_download_uses_original_url(self):
        """端到端：批量同步的多线程下载不再全部回退最高转码档。"""
        downloader.set_original_resolver(oq.resolve_for_download)
        infos = [make_info(i) for i in range(6)]
        flags = {}

        def run(i):
            url, is_original = downloader._video_stream_url(infos[i])
            flags[i] = (url, is_original)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(flags), 6)
        for i, (url, is_original) in flags.items():
            self.assertTrue(is_original, f"作品 {i} 不应回退")
            self.assertEqual(url, CDN)
            self.assertEqual(infos[i].original_verdict, "original")
            self.assertTrue(infos[i].quality.startswith("原始码流"))


class CdnHeaderTests(unittest.TestCase):
    def test_missing_content_type_accepted_when_size_is_plausible(self):
        """部分 GSLB 节点不返回 Content-Type，但体积已由 Content-Range 确认。"""
        self.assertEqual(oq.cdn_info({"content-length": "33169590"}),
                         (33169590, "video/mp4"))
        self.assertEqual(
            oq.cdn_info({"content-range": "bytes 0-1023/40203845",
                         "content-length": "1024"}),
            (40203845, "video/mp4"))

    def test_placeholder_and_non_video_still_rejected(self):
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({"content-length": "1024"})
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({"content-length": "200000",
                         "content-type": "image/jpeg"})


class OriginalCdnWhitelistTests(unittest.TestCase):
    def test_original_stream_cdn_redirects_allowed(self):
        hosts = [
            "https://d6j3wcpdqlpqs.v1d.pkoplink.com/fsb9446b3f.a.bdydns.com/a.mp4",
            "https://cdn.example.bdydns.com/a.mp4",
            "https://wx-redirect-opencdn-g.a.bdycdn.cn/a.mp4",
            "https://v26-default.douyinvod.com/a.mp4",
        ]
        for url in hosts:
            self.assertTrue(downloader._is_allowed_host(url), url)
            self.assertTrue(oq._allowed_cdn(url), url)

    def test_unrelated_host_still_blocked(self):
        for url in ("https://evil.example.com/a.mp4", "http://127.0.0.1/a.mp4",
                    "ftp://v.douyinvod.com/a.mp4"):
            self.assertFalse(downloader._is_allowed_host(url), url)

    def test_whitelist_is_in_config(self):
        for suffix in ("pkoplink.com", "bdydns.com", "bdycdn.cn", "jspcdn.cn"):
            self.assertIn(suffix, config.ALLOWED_HOST_SUFFIXES)

    def test_rotating_gslb_host_allowed_by_origin_marker(self):
        """GSLB 域名会轮换，靠 URL 里的抖音来源标记放行，而不是逐个补白名单。"""
        urls = [
            # 2026-11-13 全量同步实测被拒的跳转
            ("https://iqhvbghbvehek.jspcdn.cn/v5-se-jltc-cold.douyinvod.com/"
             "921bf1af/video/tos/cn/tos-cn-v-0015c800/o8GS.mp4?u_reqid=6af5fc8f"),
            # 未来出现的新域名：query 里指回抖音真实主机
            ("https://cdn-node-7.example-isp.net/video/tos/abc.mp4"
             "?http_x-tos-host=v5-se.douyinvod.com&u_reqid=1"),
        ]
        for url in urls:
            self.assertTrue(downloader._is_allowed_host(url), url)

    def test_forged_origin_without_video_marker_still_blocked(self):
        for url in ("https://evil.example.com/a.mp4",
                    "https://evil.example.com/x.douyinvod.com/a.mp4",
                    "https://evil.example.com/a.mp4?http_x-tos-host=v5.douyinvod.com"):
            self.assertFalse(downloader._is_allowed_host(url), url)


if __name__ == "__main__":
    unittest.main()
