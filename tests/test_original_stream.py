# -*- coding: utf-8 -*-
"""《抖音「原画质」获取实现规范》落地回归（全部离线，不访问抖音）。

覆盖：第 4 节字段/选档、第 5 节一级判定、第 6 节三个实测样本、第 7 节四条
软失效断言、第 8 节 MP4 结构检查，以及本项目解析/下载链路的接入点。
"""
import asyncio
import os
import struct
import tempfile
import unittest
from unittest import mock

from tests.support import isolated_config  # noqa: F401 - 初始化 Qt/路径环境

import downloader
import models
import original_stream as oq

CDN = "https://v5-ali-northeast.douyinvod.com/abc/video.mp4?x=1"
MB = 1048576


def _gear(name, size, width=1080, height=1920):
    return {"gear_name": name,
            "play_addr": {"data_size": size, "width": width, "height": height,
                          "url_list": ["https://x.douyinvod.com/" + name]}}


def _detail(vid="v0200fg10000abcdef", gears=None, desc="自己的作品", author="本人"):
    return {
        "aweme_id": "7525082444551310602",
        "desc": desc,
        "author": {"nickname": author},
        "video": {
            "play_addr": {"uri": vid, "url_list": ["https://x.douyinvod.com/main"],
                          "data_size": 4 * MB},
            "bit_rate": gears if gears is not None else [
                _gear("normal_1080_0", int(5.74 * MB)),
                _gear("normal_540_0", int(4.99 * MB), 540, 960),
                _gear("normal_720_0", int(4.18 * MB), 720, 1280),
            ],
        },
    }


class FakeResp:
    def __init__(self, status=302, headers=None):
        self.status = status
        self.headers = headers or {}


RANGE_HEADERS = {"content-type": "video/mp4", "content-length": "1024",
                 "content-range": "bytes 0-1023/40203845"}
XML_HEADERS = {"content-type": "application/xml", "content-length": "0"}


class FakeAsyncRequest:
    def __init__(self, location=CDN, head_headers=None, status=302,
                 head_status=200, range_headers=None):
        self.location = location
        self.head_headers = head_headers or {
            "content-length": str(int(78.46 * MB)), "content-type": "video/mp4"}
        self.head_status = head_status
        self.range_headers = range_headers or RANGE_HEADERS
        self.status = status
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if "Range" in (kwargs.get("headers") or {}):
            return FakeResp(206, self.range_headers)
        return FakeResp(self.status, {"location": self.location} if self.location else {})

    async def head(self, url, **kwargs):
        self.calls.append(("head", url, kwargs))
        return FakeResp(self.head_status, self.head_headers)


class FakeSyncRequest(FakeAsyncRequest):
    """同步版请求桩：下载线程的 OriginalSession 用。"""

    def get(self, url, **kwargs):
        return asyncio.run(super().get(url, **kwargs))

    def head(self, url, **kwargs):
        return asyncio.run(super().head(url, **kwargs))


class FakeSyncContext:
    def __init__(self, request):
        self.request = request


class FakePage:
    def __init__(self, final_url=""):
        self.final_url = final_url
        self.evaluated = []

    async def evaluate(self, script, url):
        self.evaluated.append((script, url))
        return self.final_url


class FakeContext:
    def __init__(self, request):
        self.request = request


async def _no_sleep(seconds):
    return None


class PureRulesTests(unittest.TestCase):
    """第 4~7 节纯判定：不碰网络。"""

    def test_video_id_is_play_addr_uri_not_aweme_id(self):
        detail = _detail("v0200fg10000xyz")
        self.assertEqual(oq.video_id_of(detail), "v0200fg10000xyz")
        self.assertNotEqual(oq.video_id_of(detail), detail["aweme_id"])

    def test_play_url_uses_default_ratio_and_required_params(self):
        url = oq.build_play_url("v0200fg10000xyz")
        self.assertTrue(url.startswith(oq.PLAY_ENDPOINT))
        for frag in ("video_id=v0200fg10000xyz", "ratio=default", "line=0",
                     "improve_bitrate=1", "watermark=0"):
            self.assertIn(frag, url)

    def test_best_gear_sorts_by_data_size_not_resolution(self):
        # 规范实测反例：540p 档体积比 720p 档大
        best = oq.best_gear(_detail()["video"])
        self.assertEqual(best["gear_name"], "normal_1080_0")
        self.assertEqual(best["size"], int(5.74 * MB))
        video = {"bit_rate": [_gear("normal_540_0", int(4.99 * MB), 540, 960),
                              _gear("normal_720_0", int(4.18 * MB), 720, 1280)]}
        self.assertEqual(oq.best_gear(video)["gear_name"], "normal_540_0")

    def test_best_gear_falls_back_to_play_addr_when_ladder_empty(self):
        video = {"play_addr": {"data_size": 123456, "url_list": ["https://x/a"]}}
        self.assertEqual(oq.best_gear(video)["size"], 123456)
        with self.assertRaises(oq.OriginalStreamError):
            oq.best_gear({"play_addr": {"uri": "v0200fg"}})

    def test_classify_matches_three_real_samples(self):
        cases = [(78.46, 3.25, "original", 24.14),
                 (2.68, 6.21, "transcoded", 0.43),
                 (21.36, 5.74, "original", 3.72)]
        for original, best, verdict, factor in cases:
            got, ratio = oq.classify(int(original * MB), int(best * MB))
            self.assertEqual(got, verdict)
            self.assertAlmostEqual(ratio, factor, places=2)

    def test_classify_threshold_and_missing_sizes(self):
        self.assertEqual(oq.classify(120, 100)[0], "original")
        self.assertEqual(oq.classify(119, 100)[0], "transcoded")
        self.assertEqual(oq.classify(0, 100), ("transcoded", 0.0))
        self.assertEqual(oq.classify(100, 0), ("transcoded", 0.0))

    def test_cdn_info_assertion_4(self):
        self.assertEqual(oq.cdn_info({"content-length": "200000",
                                      "content-type": "video/mp4"}),
                         (200000, "video/mp4"))
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({"content-length": "1024", "content-type": "video/mp4"})
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({"content-length": "200000", "content-type": "image/jpeg"})
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({})

    def test_cdn_info_reads_total_from_content_range(self):
        # 真实 CDN 的 HEAD 405/404，Range GET 回 206 + 1 KB，总长度在斜杠后面
        self.assertEqual(oq.cdn_info(RANGE_HEADERS), (40203845, "video/mp4"))
        with self.assertRaises(oq.OriginalStreamError):
            oq.cdn_info({"content-type": "video/mp4",
                         "content-range": "bytes 0-1023/2048"})

    def test_need_range_fallback_only_for_broken_head(self):
        self.assertTrue(oq.need_range_fallback(XML_HEADERS, 404))
        self.assertTrue(oq.need_range_fallback({"content-type": "video/mp4"}, 200))
        # HEAD 200 且给了长度、类型却不对：是真的降级流，不要再发请求
        self.assertFalse(oq.need_range_fallback(
            {"content-length": "200000", "content-type": "text/html"}, 200))

    def test_header_get_is_case_insensitive(self):
        self.assertEqual(oq.header_get({"LoCaTiOn": CDN}, "location"), CDN)
        self.assertEqual(oq.header_get({}, "location"), "")

    def test_assert_detail_four_soft_failures(self):
        oq.assert_detail(_detail())
        for bad in (
            {},
            {**_detail(), "aweme_id": ""},
            {**_detail(), "desc": "", "author": {"nickname": ""}},
            {**_detail(), "video": {"play_addr": {"uri": ""}}},
            {**_detail(), "video": {"play_addr": {"uri": "v1"}, "bit_rate": []}},
        ):
            with self.assertRaises(oq.OriginalStreamError):
                oq.assert_detail(bad)

    def test_detect_challenge(self):
        for url, title in (("https://www.douyin.com/verify", ""),
                           ("", "请完成滑动验证"), ("", "secsdk challenge"),
                           ("https://x/captcha", "")):
            self.assertTrue(oq.detect_challenge(url, title))
        self.assertFalse(oq.detect_challenge("https://www.douyin.com/video/123", "抖音"))

    def test_find_nested_detail_recurses_router_data(self):
        router = {"loaderData": {"video": {"videoInfoRes": {
            "item_list": [_detail()]}}}}
        found = oq.find_nested_detail(router)
        self.assertEqual(found["aweme_id"], "7525082444551310602")
        self.assertIsNone(oq.find_nested_detail({"a": {"b": [1, 2]}}))
        loop = {"aweme_detail": _detail()}
        loop["self"] = loop
        self.assertEqual(oq.find_nested_detail(loop)["aweme_id"],
                         "7525082444551310602")

    def test_cdn_host_whitelist(self):
        self.assertTrue(oq._allowed_cdn(CDN))
        self.assertTrue(oq._allowed_cdn("https://v11-colds.douyinvod.com/a"))
        self.assertFalse(oq._allowed_cdn("https://evil.example.com/a"))
        self.assertFalse(oq._allowed_cdn("file:///etc/passwd"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        oq.clear_cache()
        oq.set_enabled(True)

    def tearDown(self):
        oq.clear_cache()
        oq.set_enabled(False)
        downloader.set_original_resolver(None)

    def test_only_original_streams_are_cached(self):
        good = oq.OriginalStream(video_id="v1", url=CDN, size=10 * MB,
                                 best_size=MB, factor=10, verdict="original")
        bad = oq.OriginalStream(video_id="v2", url=CDN, size=MB,
                                best_size=2 * MB, factor=0.5, verdict="transcoded")
        oq.remember(good)
        oq.remember(bad)
        self.assertIsNotNone(oq.cached("v1"))
        self.assertIsNone(oq.cached("v2"))

    def test_cache_expires_and_force_bypasses(self):
        stream = oq.OriginalStream(video_id="v1", url=CDN, verdict="original")
        oq.remember(stream)
        self.assertIsNotNone(oq.cached("v1"))
        with mock.patch.object(oq.time, "monotonic", return_value=1e6):
            self.assertIsNone(oq.cached("v1"))
        self.assertIsNone(oq.cached("v1"))  # 过期时顺手清掉
        oq.remember(stream)
        self.assertIsNone(oq.cached("v1", force=True))
        self.assertIsNone(oq.cached("v1"))

    def test_resolver_disabled_when_switch_off(self):
        oq.set_enabled(False)
        self.assertIsNone(oq.resolve_for_download("v1", MB))

    def test_session_failure_cools_down_batch_sync(self):
        calls = []

        class Broken:
            def resolve(self, *args, **kwargs):
                calls.append(args)
                raise oq.OriginalStreamError("未登录")

        with mock.patch.object(oq, "get_session", return_value=Broken()):
            with self.assertRaises(oq.OriginalStreamError):
                oq.resolve_for_download("v1", MB)
            self.assertIsNone(oq.resolve_for_download("v2", MB))
        self.assertEqual(len(calls), 1, "冷却期间不能每个作品都重启浏览器")
        oq.set_enabled(True)   # 用户重新打开开关 → 允许再试
        with mock.patch.object(oq, "get_session", return_value=Broken()):
            with self.assertRaises(oq.OriginalStreamError):
                oq.resolve_for_download("v3", MB)
        self.assertEqual(len(calls), 2)


class AsyncResolveTests(unittest.TestCase):
    """第 6 节页面内回捞：fake 浏览器上下文，不发真实请求。"""

    def setUp(self):
        oq.clear_cache()
        oq.set_enabled(True)

    def tearDown(self):
        oq.clear_cache()
        oq.set_enabled(False)

    def _run(self, request, page=None, detail=None, **kwargs):
        ctx = FakeContext(request)
        return asyncio.run(oq.resolve_original(
            ctx, page, detail or _detail(), sleeper=_no_sleep, **kwargs))

    def test_location_head_and_original_verdict(self):
        request = FakeAsyncRequest()
        stream = self._run(request)
        self.assertTrue(stream.is_original)
        self.assertEqual(stream.url, CDN)
        self.assertEqual(stream.best_gear, "normal_1080_0")
        self.assertAlmostEqual(stream.factor, 78.46 / 5.74, places=2)
        self.assertEqual(stream.verdict, "original")
        self.assertIn("原始码流", stream.label())
        get_call = [c for c in request.calls if c[0] == "get"][0]
        self.assertEqual(get_call[2]["max_redirects"], 0)
        self.assertEqual(get_call[2]["headers"]["Referer"], oq.REFERER)
        self.assertEqual([c[0] for c in request.calls], ["get", "head"])
        self.assertIsNotNone(oq.cached("v0200fg10000abcdef"))

    def test_transcoded_channel_does_not_replace_best_gear(self):
        request = FakeAsyncRequest(head_headers={
            "content-length": str(int(2.68 * MB)), "content-type": "video/mp4"})
        stream = self._run(request)
        self.assertEqual(stream.verdict, "transcoded")
        self.assertFalse(stream.is_original)
        self.assertIsNone(oq.cached("v0200fg10000abcdef"))

    def test_location_missing_falls_back_to_page_fetch(self):
        request = FakeAsyncRequest(location="")
        page = FakePage(final_url=CDN)
        stream = self._run(request, page=page)
        self.assertEqual(stream.via, "fetch-fallback")
        self.assertEqual(stream.url, CDN)
        self.assertEqual(len(page.evaluated), 1)
        self.assertIn("body.cancel", page.evaluated[0][0])

    def test_no_location_raises(self):
        with self.assertRaises(oq.OriginalStreamError):
            self._run(FakeAsyncRequest(location=""), page=FakePage())

    def test_cdn_outside_whitelist_raises(self):
        with self.assertRaises(oq.OriginalStreamError):
            self._run(FakeAsyncRequest(location="https://evil.example.com/a"))

    def test_cdn_assertion_propagates(self):
        request = FakeAsyncRequest(head_headers={
            "content-length": "100", "content-type": "video/mp4"})
        with self.assertRaisesRegex(oq.OriginalStreamError, "断言 4"):
            self._run(request)

    def test_head_404_falls_back_to_range_get(self):
        """真实样本：腾讯 COS 的 HEAD 回 404 XML，Range GET 给出真实体积与类型。"""
        request = FakeAsyncRequest(head_status=404, head_headers=XML_HEADERS,
                                   range_headers=RANGE_HEADERS)
        detail = _detail(gears=[_gear("adapt_lowest_4_1", 59895193, 2340, 2160)])
        stream = self._run(request, detail=detail)
        self.assertEqual(stream.size, 40203845)
        self.assertEqual(stream.verdict, "transcoded")   # 0.67x，通道未开放
        self.assertAlmostEqual(stream.factor, 0.67, places=2)
        self.assertEqual([c[0] for c in request.calls], ["get", "head", "get"])
        self.assertEqual(request.calls[-1][2]["headers"]["Range"], "bytes=0-1023")

    def test_head_200_with_bad_type_does_not_retry(self):
        request = FakeAsyncRequest(head_headers={
            "content-length": "200000", "content-type": "text/plain"})
        with self.assertRaises(oq.OriginalStreamError):
            self._run(request)
        self.assertEqual([c[0] for c in request.calls], ["get", "head"])

    def test_detail_assertion_runs_before_any_request(self):
        request = FakeAsyncRequest()
        with self.assertRaises(oq.OriginalStreamError):
            self._run(request, detail={**_detail(), "video": {}})
        self.assertEqual(request.calls, [])

    def test_throttle_is_applied(self):
        delays = []

        async def sleeper(seconds):
            delays.append(seconds)

        request = FakeAsyncRequest()
        asyncio.run(oq.resolve_original(FakeContext(request), None, _detail(),
                                        sleeper=sleeper))
        self.assertEqual(len(delays), 1)
        self.assertGreaterEqual(delays[0], 1.2)
        self.assertLessEqual(delays[0], 3.0)


class SyncSessionTests(unittest.TestCase):
    """下载线程的即时回捞：不启动浏览器，用同步请求桩覆盖缓存与兜底。"""

    def setUp(self):
        oq.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="jy-osession-")

    def tearDown(self):
        oq.clear_cache()

    def _session(self, request):
        session = oq.OriginalSession(
            session_dir=os.path.join(self.tmp, "profile"),
            cookie_file=os.path.join(self.tmp, "missing.json"))
        session._ensure = lambda: None       # 跳过真实浏览器与预热
        session._ctx = FakeSyncContext(request)
        session._page = object()
        session._sleeper = lambda seconds: None
        return session

    def test_head_404_falls_back_to_range_get(self):
        request = FakeSyncRequest(head_status=404, head_headers=XML_HEADERS)
        stream = self._session(request).resolve("v1", 59895193)
        self.assertEqual(stream.size, 40203845)
        self.assertEqual(stream.verdict, "transcoded")
        self.assertAlmostEqual(stream.factor, 0.67, places=2)
        self.assertIsNone(oq.cached("v1"))

    def test_original_stream_cached_for_immediate_download(self):
        request = FakeSyncRequest()
        session = self._session(request)
        stream = session.resolve("v1", int(5.74 * MB))
        self.assertTrue(stream.is_original)
        self.assertIsNotNone(oq.cached("v1"))
        before = len(request.calls)
        self._session(request).resolve("v1", int(5.74 * MB))
        self.assertEqual(len(request.calls), before, "命中缓存不应重复发请求")

    def test_force_refresh_bypasses_cache(self):
        request = FakeSyncRequest()
        session = self._session(request)
        session.resolve("v1", int(5.74 * MB))
        before = len(request.calls)
        session.resolve("v1", int(5.74 * MB), force=True)
        self.assertEqual(len(request.calls) - before, 2)  # play + HEAD


class ModelIntegrationTests(unittest.TestCase):
    def test_parse_aweme_keeps_video_id_and_data_size_best_gear(self):
        info = models.parse_aweme(_detail("v0200fgZZZ"))
        self.assertEqual(info.video_id, "v0200fgZZZ")
        self.assertEqual(info.best_gear_size, int(5.74 * MB))
        self.assertEqual(info.best_gear_name, "normal_1080_0")
        self.assertEqual(info.play_url, "https://x.douyinvod.com/normal_1080_0")

    def test_uri_that_is_not_a_url_is_not_used_as_play_url(self):
        # 只有 video_id、没有 url_list 时，不能把 v0200fg… 当直链
        url = models._first_url({"uri": "v0200fg10000abcdef"})
        self.assertEqual(url, "")
        info = models.parse_aweme({
            "aweme_id": "1", "desc": "x", "author": {"nickname": "a"},
            "video": {"play_addr": {"uri": "v0200fg10000abcdef"}},
        })
        self.assertEqual(info.play_url, "")
        self.assertEqual(info.video_id, "v0200fg10000abcdef")


class DownloaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        oq.clear_cache()
        self.info = models.parse_aweme(_detail("v0200fgDL"))
        self.info.title = "测试"
        self._tmp = tempfile.mkdtemp(prefix="jy-original-")

    def tearDown(self):
        downloader.set_original_resolver(None)
        oq.clear_cache()

    def test_original_resolver_replaces_url_and_quality(self):
        stream = oq.OriginalStream(video_id="v0200fgDL", url=CDN, size=78 * MB,
                                   best_size=int(5.74 * MB), factor=13.6,
                                   verdict="original")
        downloader.set_original_resolver(
            lambda vid, best, force=False: stream)
        url, is_original = downloader._video_stream_url(self.info)
        self.assertEqual(url, CDN)
        self.assertTrue(is_original)
        self.assertEqual(self.info.original_verdict, "original")
        self.assertTrue(self.info.quality.startswith("原始码流"))
        self.assertEqual(self.info.ladder_quality, "原画 1080p")

    def test_transcoded_resolver_falls_back_to_best_gear(self):
        self.info.original_url = CDN
        self.info.quality = "原始码流 78.0MB"
        self.info.ladder_quality = "原画 1080p"
        stream = oq.OriginalStream(video_id="v0200fgDL", url=CDN,
                                   verdict="transcoded")
        downloader.set_original_resolver(
            lambda vid, best, force=False: stream)
        url, is_original = downloader._video_stream_url(self.info)
        self.assertEqual(url, self.info.play_url)
        self.assertFalse(is_original)
        self.assertEqual(self.info.quality, "原画 1080p")

    def test_resolver_exception_never_breaks_download(self):
        def broken(vid, best, force=False):
            raise oq.OriginalStreamError("未登录")

        downloader.set_original_resolver(broken)
        url, is_original = downloader._video_stream_url(self.info)
        self.assertEqual(url, self.info.play_url)
        self.assertFalse(is_original)

    def test_failed_original_link_is_refetched_then_falls_back(self):
        calls = []

        def resolver(vid, best, force=False):
            calls.append(force)
            if not force:
                return oq.OriginalStream(video_id=vid, url=CDN, size=78 * MB,
                                         best_size=best, factor=10,
                                         verdict="original")
            return oq.OriginalStream(video_id=vid, url=CDN,
                                     verdict="transcoded")

        downloader.set_original_resolver(resolver)
        sizes = []
        chunks = []

        def fake_download(url, target, *args, **kwargs):
            sizes.append(url)
            chunks.append(kwargs.get("chunk_size"))
            if len(sizes) == 1:
                raise IOError("直链已过期")
            return 1234

        with mock.patch.object(downloader, "_download_to_file", fake_download), \
                mock.patch.object(downloader, "work_dir", return_value=self._tmp), \
                mock.patch.object(downloader, "work_folder_name", return_value="7525"), \
                mock.patch.object(downloader, "_safe_subdir", return_value=True):
            result = downloader.download_work(self.info, "作者", "7525")
        self.assertEqual(result.size, 1234)
        self.assertEqual(calls, [False, True])
        self.assertEqual(sizes, [CDN, self.info.play_url])
        # 原片 1 MB 分块；回退转码档后恢复 256 KB
        self.assertEqual(chunks, [1024 * 1024, 256 * 1024])


# ---------------------------------------------------------------- MP4 结构
def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _sample_entry(codec: bytes, width: int, height: int, *, legacy=False) -> bytes:
    # ISO BMFF：sample entry box 头 8 字节，宽高在 box 起始 +32，
    # 即 _box 的 payload 内 +24。legacy=True 复现旧实现误读的 +40。
    at = 32 if legacy else 24
    payload = bytearray(80)
    payload[at:at + 2] = struct.pack(">H", width)
    payload[at + 2:at + 4] = struct.pack(">H", height)
    return _box(codec, bytes(payload))


def _tkhd(width: int, height: int, *, version=0) -> bytes:
    """构造 tkhd，末尾是 16.16 定点的 width/height（音轨为 0）。"""
    if version == 1:
        body = (bytes([1]) + b"\x00" * 3 + b"\x00" * 16 + b"\x00" * 8
                + b"\x00" * 8 + b"\x00" * 16 + b"\x00" * 36)
    else:
        body = b"\x00" * 24 + b"\x00" * 16 + b"\x00" * 36
    return _box(b"tkhd", body + struct.pack(">II", width << 16, height << 16))


def _stsd(entries: bytes, count: int = 1) -> bytes:
    return _box(b"stsd", b"\x00" * 4 + struct.pack(">I", count) + entries)


def _trak(entry: bytes, tkhd: bytes = b"") -> bytes:
    stbl = _box(b"stbl", _stsd(entry))
    return _box(b"trak", tkhd + _box(b"mdia", _box(b"minf", stbl)))


def _mvhd(timescale=1000, duration=1200, version=0) -> bytes:
    if version == 1:
        payload = (bytes([1]) + b"\x00" * 3 + b"\x00" * 16
                   + struct.pack(">I", timescale) + struct.pack(">Q", duration)
                   + b"\x00" * 80)
    else:
        payload = (b"\x00" * 4 + b"\x00" * 8
                   + struct.pack(">I", timescale) + struct.pack(">I", duration)
                   + b"\x00" * 80)
    return _box(b"mvhd", payload)


def _make_mp4(path: str, *, brand=b"qt  ", codec=b"hvc1", audio_first=False,
              meta=b"com.apple.quicktime.software iPhone 2025-06-06T16:52:08Z",
              mdat_size=1_600_000, mdat_noise=b"", dims=(3840, 2160),
              legacy_layout=False, tkhd_dims=None):
    traks = _trak(_sample_entry(codec, *dims, legacy=legacy_layout),
                  _tkhd(*tkhd_dims) if tkhd_dims else b"")
    if audio_first:
        traks = _trak(_sample_entry(b"mp4a", 0, 0)) + traks
    moov = _box(b"moov", _mvhd() + traks + _box(b"udta", meta))
    ftyp = _box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"isom")
    with open(path, "wb") as f:
        f.write(ftyp + moov + _box(b"mdat", mdat_noise + b"\x00" * mdat_size))


class Mp4InspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jy-mp4-")

    def test_original_features_are_read_without_ffprobe(self):
        path = os.path.join(self.tmp, "original.mp4")
        _make_mp4(path)
        info = oq.inspect_mp4(path)
        self.assertEqual(info["brand"], "qt")
        self.assertEqual(info["codecs"], ["hvc1"])
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertAlmostEqual(info["duration_s"], 1.2, places=2)
        self.assertGreater(info["avg_bitrate_kbps"], 10000)
        self.assertTrue(info["metadata"]["apple_quicktime"])
        self.assertTrue(info["metadata"]["apple_software"])
        self.assertEqual(info["metadata"]["capture_time"], "2025-06-06T16:52:08Z")
        self.assertIn("原始上传码流特征充分", oq.file_verdict(info))

    def test_spec_offset_matches_real_hevc_original(self):
        # 真实原片：hvc1 + qt，宽高在 sample entry box+32，tkhd 也有 1080x1920
        path = os.path.join(self.tmp, "real-layout.mp4")
        _make_mp4(path, dims=(1080, 1920), tkhd_dims=(1080, 1920))
        info = oq.inspect_mp4(path)
        self.assertEqual(info["codecs"], ["hvc1"])
        self.assertEqual((info["width"], info["height"]), (1080, 1920))

    def test_legacy_plus40_layout_still_read(self):
        # 旧实现按 box+40 读；保留兼容，避免历史/异种封装器退化为 0
        path = os.path.join(self.tmp, "legacy-layout.mp4")
        _make_mp4(path, dims=(720, 1280), legacy_layout=True)
        self.assertEqual(
            (oq.inspect_mp4(path)["width"], oq.inspect_mp4(path)["height"]),
            (720, 1280))

    def test_tkhd_fallback_when_stsd_has_no_dims(self):
        path = os.path.join(self.tmp, "tkhd-only.mp4")
        _make_mp4(path, dims=(0, 0), tkhd_dims=(1080, 1920))
        info = oq.inspect_mp4(path)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))

    def test_tkhd_v1_and_audio_track_zero_dims(self):
        path = os.path.join(self.tmp, "tkhd-v1.mp4")
        video = _trak(_sample_entry(b"hvc1", 0, 0), _tkhd(1080, 1920, version=1))
        audio = _trak(_sample_entry(b"mp4a", 0, 0), _tkhd(0, 0))
        moov = _box(b"moov", _mvhd() + audio + video)
        with open(path, "wb") as f:
            f.write(_box(b"ftyp", b"qt  ") + moov)
        info = oq.inspect_mp4(path)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))

    def test_audio_track_first_does_not_hide_video_codec(self):
        path = os.path.join(self.tmp, "audio-first.mp4")
        _make_mp4(path, audio_first=True)
        self.assertEqual(oq.inspect_mp4(path)["codecs"], ["hvc1"])

    def test_metadata_scan_is_limited_to_moov(self):
        path = os.path.join(self.tmp, "mdat-noise.mp4")
        _make_mp4(path, meta=b"platform tag bytedance douyin",
                  mdat_noise=b"com.apple.quicktime.software")
        info = oq.inspect_mp4(path)
        # mdat 里的假标记不能被算成原片证据
        self.assertFalse(info["metadata"]["apple_software"])
        self.assertTrue(info["metadata"]["bytedance"])

    def test_codec_changed_is_hard_evidence(self):
        original = os.path.join(self.tmp, "a.mp4")
        transcoded = os.path.join(self.tmp, "b.mp4")
        _make_mp4(original, codec=b"hvc1")
        _make_mp4(transcoded, brand=b"isom", codec=b"avc1",
                  meta=b"Lavf58.12.100 bytedance", mdat_size=200000)
        left, right = oq.inspect_mp4(original), oq.inspect_mp4(transcoded)
        self.assertTrue(oq.codec_changed(left, right))
        self.assertEqual(right["brand"], "isom")
        self.assertEqual(right["codecs"], ["avc1"])
        # Lavf 两边都可能出现，单独不构成判据
        self.assertNotIn("原始上传码流特征充分", oq.file_verdict(right))

    def test_mvhd_version1(self):
        path = os.path.join(self.tmp, "v1.mp4")
        moov = _box(b"moov", _mvhd(timescale=1000, duration=2000, version=1)
                    + _trak(_sample_entry(b"avc1", 1080, 1920)))
        with open(path, "wb") as f:
            f.write(_box(b"ftyp", b"isom" + b"\x00" * 12) + moov)
        self.assertAlmostEqual(oq.inspect_mp4(path)["duration_s"], 2.0, places=2)


class CliDownloadTests(unittest.TestCase):
    """真实跑一遍「流式下载 → 落盘 → 结构校验」，用本地 HTTP 服务代替 CDN。"""

    def test_download_cdn_streams_and_file_is_verifiable(self):
        import http.server
        import importlib.util
        import threading

        cli_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "original_cli.py")
        spec = importlib.util.spec_from_file_location("jy_original_cli", cli_path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        tmp = tempfile.mkdtemp(prefix="jy-clidl-")
        src = os.path.join(tmp, "src.mp4")
        _make_mp4(src, mdat_size=200000)
        with open(src, "rb") as handle:
            data = handle.read()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定名
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/a.mp4" % server.server_address[1]
            target = os.path.join(tmp, "out", "视频.mp4")
            with mock.patch.object(oq, "_allowed_cdn", return_value=True):
                size = cli.download_cdn(url, target)
            self.assertEqual(size, len(data))
            self.assertFalse(os.path.exists(target + ".part"))
            info = oq.inspect_mp4(target)
            self.assertEqual(info["brand"], "qt")
            self.assertEqual(info["codecs"], ["hvc1"])
            self.assertIn("码流疑似未经重编码", oq.file_verdict(info))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
