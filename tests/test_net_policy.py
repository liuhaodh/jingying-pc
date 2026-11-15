# -*- coding: utf-8 -*-
"""网络策略层离线测试：退避、限流、自适应降速、坏数据断言与主链路接线。

全部用可推进的假时钟，不联网、不启浏览器、不真的 sleep。
"""
import asyncio
import threading
import types
import unittest
from unittest import mock

from tests.support import isolated_config  # noqa: F401  （初始化 Qt/路径环境）

import config
import downloader
import net_policy
import parallel
import parser


class FakeClock:
    """单调时钟；sleep 同时推进它，让退避/令牌桶测试瞬间跑完。"""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def fast_runtime(max_attempts=3):
    """返回一个注入了假时钟的运行时，并把它设为全局单例。"""
    rt = net_policy.reset_runtime()
    clock = FakeClock()
    rt.retry.clock = clock
    rt.retry.sleep = clock.sleep
    rt.retry.asleep = clock.asleep
    rt.retry.rng = lambda: 0.5       # 抖动因子恒为 1.0
    rt.retry.max_attempts = max_attempts
    rt.throttle.clock = clock
    rt.throttle.sleep = clock.sleep
    rt.throttle.asleep = clock.asleep
    return rt, clock


class StrategyTestBase(unittest.TestCase):
    def setUp(self):
        self.rt, self.clock = fast_runtime()

    def tearDown(self):
        net_policy.reset_runtime()


class RetryPolicyTests(StrategyTestBase):
    def test_exponential_backoff(self):
        rp = net_policy.RetryPolicy(jitter=0.0, base_delay=0.6, backoff=2.0)
        self.assertEqual([rp.delay_for(i) for i in range(1, 4)],
                         [0.6, 1.2, 2.4])

    def test_max_delay_is_capped(self):
        rp = net_policy.RetryPolicy(jitter=0.0, base_delay=3.0, max_delay=5.0)
        self.assertEqual(rp.delay_for(6), 5.0)

    def test_jitter_stays_within_bounds(self):
        rp = net_policy.RetryPolicy(jitter=0.5, base_delay=1.0,
                                    rng=lambda: 1.0)
        self.assertAlmostEqual(rp.delay_for(1), 1.5)
        rp2 = net_policy.RetryPolicy(jitter=0.5, base_delay=1.0,
                                     rng=lambda: 0.0)
        self.assertAlmostEqual(rp2.delay_for(1), 0.5)

    def test_cancellation_and_login_errors_are_not_retried(self):
        rp = self.rt.retry
        download_cancelled = type("DownloadCancelled", (Exception,), {})
        parse_cancelled = type("ParseCancelled", (Exception,), {})
        self.assertFalse(rp.should_retry(download_cancelled("x")))
        self.assertFalse(rp.should_retry(parse_cancelled("x")))
        self.assertTrue(rp.should_retry(ConnectionError("x")))
        self.assertTrue(rp.should_retry(net_policy.BadPayload("bad")))

    def test_wait_is_interruptible(self):
        rp = self.rt.retry
        event = threading.Event()
        event.set()
        self.assertFalse(rp.wait(1, event))
        self.assertEqual(self.clock.sleeps, [])


class RetrySyncTests(StrategyTestBase):
    def test_success_on_first_attempt(self):
        out = net_policy.retry_sync(lambda: 42, key="page", throttle=False)
        self.assertTrue(out.ok)
        self.assertEqual(out.value, 42)
        self.assertEqual(out.attempts, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_retries_with_backoff_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("boom")
            return "ok"

        out = net_policy.retry_sync(flaky, key="page", tag="t", throttle=False)
        self.assertTrue(out.ok)
        self.assertEqual(out.value, "ok")
        self.assertEqual(out.attempts, 3)
        self.assertEqual(calls["n"], 3)
        self.assertEqual([round(d, 2) for d in out.delays], [0.6, 1.2])
        self.assertGreater(len(self.clock.sleeps), 0)

    def test_exhausts_attempts_and_returns_last_error(self):
        err = RuntimeError("always")

        def broken():
            raise err

        out = net_policy.retry_sync(broken, key="cdn", throttle=False)
        self.assertFalse(out.ok)
        self.assertIs(out.error, err)
        self.assertEqual(out.attempts, 3)

    def test_non_retryable_stops_immediately(self):
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            raise type("DownloadCancelled", (Exception,), {})("stop")

        out = net_policy.retry_sync(cancelled, key="cdn", throttle=False)
        self.assertFalse(out.ok)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out.attempts, 1)

    def test_validation_makes_bad_payload_retryable(self):
        """HTTP 200 但内容为空壳：validate 抛 BadPayload 也能触发重试。"""
        calls = {"n": 0}
        good = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["u"]}}}

        def fetch():
            calls["n"] += 1
            return {} if calls["n"] < 3 else good

        def validate(value):
            net_policy.assert_payload(value, "detail")

        out = net_policy.retry_sync(fetch, key="page", validate=validate,
                                    throttle=False)
        self.assertTrue(out.ok)
        self.assertEqual(out.attempts, 3)

    def test_cancel_event_short_circuits(self):
        event = threading.Event()
        event.set()
        out = net_policy.retry_sync(lambda: 1, key="page", cancel_event=event,
                                    throttle=False)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "cancelled")

    def test_cancel_during_retry_wait(self):
        event = threading.Event()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise ConnectionError("boom")

        def on_retry(_attempt, _exc, _delay):
            event.set()

        out = net_policy.retry_sync(flaky, key="page", cancel_event=event,
                                    on_retry=on_retry, throttle=False)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "cancelled")
        self.assertEqual(calls["n"], 1)

    def test_diagnostics_counts_retries_and_reasons(self):
        def broken():
            raise net_policy.Blocked("429")

        net_policy.retry_sync(broken, key="page", throttle=False)
        summary = self.rt.diagnostics.summary()
        self.assertEqual(summary.get("fail"), 3)
        self.assertEqual(summary.get("retried"), 2)
        self.assertEqual(summary.get("reason:blocked"), 3)
        self.assertEqual(len(self.rt.diagnostics.snapshot()), 3)


class TokenBucketTests(unittest.TestCase):
    def test_capacity_then_rate_limited_wait(self):
        clock = FakeClock()
        bucket = net_policy.TokenBucket(rate=1.0, capacity=2.0,
                                        clock=clock, sleep=clock.sleep)
        self.assertTrue(bucket.acquire())
        self.assertTrue(bucket.acquire())
        self.assertEqual(clock.sleeps, [])
        # 第三个令牌要等 1 秒；假时钟推进后补出一个令牌
        self.assertTrue(bucket.acquire())
        self.assertAlmostEqual(clock.now, 1.0, places=3)

    def test_cancel_event_aborts_wait(self):
        clock = FakeClock()
        bucket = net_policy.TokenBucket(rate=0.1, capacity=1.0,
                                        clock=clock, sleep=clock.sleep)
        self.assertTrue(bucket.acquire())
        event = threading.Event()
        event.set()
        self.assertFalse(bucket.acquire(cancel_event=event))

    def test_async_acquire_uses_injected_clock(self):
        clock = FakeClock()
        bucket = net_policy.TokenBucket(rate=1.0, capacity=1.0,
                                        clock=clock, asleep=clock.asleep)
        self.assertTrue(asyncio.run(bucket.acquire_async()))
        self.assertTrue(asyncio.run(bucket.acquire_async()))
        self.assertAlmostEqual(clock.now, 1.0, places=3)


class AutoThrottleTests(unittest.TestCase):
    def test_starts_at_zero_and_speeds_up_on_block(self):
        auto = net_policy.AutoThrottle()
        self.assertEqual(auto.delay_for("page"), 0.0)
        first = auto.record("page", ok=False, status=429)
        self.assertGreaterEqual(first, 0.5)
        second = auto.record("page", ok=False, status=429)
        self.assertGreater(second, first)

    def test_retry_after_respected_but_capped(self):
        auto = net_policy.AutoThrottle()
        delay = auto.record("page", ok=False, status=429, retry_after=120.0)
        self.assertEqual(delay, 5.0)

    def test_recovers_after_successes(self):
        auto = net_policy.AutoThrottle()
        blocked = auto.record("page", ok=False, status=429)
        for _ in range(20):
            current = auto.record("page", ok=True, latency_s=0.05)
        self.assertLess(current, blocked)
        self.assertEqual(current, 0.0)

    def test_slow_response_raises_delay_mildly(self):
        auto = net_policy.AutoThrottle(slow_threshold=1.0)
        delay = auto.record("page", ok=True, latency_s=4.0)
        self.assertGreater(delay, 0.0)
        self.assertLessEqual(delay, 0.5)

    def test_disabled_never_slows_down(self):
        auto = net_policy.AutoThrottle(enabled=False)
        self.assertEqual(auto.record("page", ok=False, status=429), 0.0)
        self.assertEqual(auto.delay_for("page"), 0.0)


class DomainThrottleTests(unittest.TestCase):
    def test_disabled_policy_passes_through(self):
        clock = FakeClock()
        throttle = net_policy.DomainThrottle({"page": 0.1}, enabled=False,
                                             clock=clock, sleep=clock.sleep)
        self.assertTrue(throttle.wait("page"))
        self.assertEqual(clock.sleeps, [])

    def test_blocked_response_adds_delay_to_next_wait(self):
        clock = FakeClock()
        throttle = net_policy.DomainThrottle({"page": 100.0},
                                             clock=clock, sleep=clock.sleep)
        self.assertTrue(throttle.wait("page"))
        throttle.record("page", ok=False, status=429)
        self.assertTrue(throttle.wait("page"))
        self.assertGreater(clock.now, 0.0)

    def test_snapshot_shape(self):
        snap = net_policy.DomainThrottle({"page": 2.0}).snapshot()
        self.assertIn("rates", snap)
        self.assertIn("auto_delays", snap)
        self.assertIn("enabled", snap)


class PayloadTests(unittest.TestCase):
    def test_detail_assertions(self):
        self.assertIn("不是非空对象", net_policy.check_payload(None, "detail"))
        self.assertIn("aweme_id", net_policy.check_payload({"video": {}}, "detail"))
        self.assertIn("video / images", net_policy.check_payload(
            {"aweme_id": "1"}, "detail"))
        good = {"aweme_id": "1", "video": {"play_addr": {"url_list": ["u"]}}}
        self.assertEqual(net_policy.check_payload(good, "detail"), "")
        # 图集作品只有 images，不能被误判成风控兜底数据
        album = {"aweme_id": "1", "images": [{"url_list": ["u"]}]}
        self.assertEqual(net_policy.check_payload(album, "detail"), "")

    def test_post_assertion_supports_nested_data(self):
        self.assertIn("不是非空对象", net_policy.check_payload({}, "post"))
        self.assertIn("aweme_list", net_policy.check_payload(
            {"status_code": 0}, "post"))
        self.assertEqual(net_policy.check_payload({"aweme_list": []}, "post"), "")
        self.assertEqual(net_policy.check_payload(
            {"data": {"aweme_list": []}}, "post"), "")

    def test_assert_payload_raises_bad_payload(self):
        with self.assertRaises(net_policy.BadPayload):
            net_policy.assert_payload({}, "detail")

    def test_challenge_detection(self):
        self.assertTrue(net_policy.is_challenge(
            url="https://www.douyin.com/verify/xyz"))
        self.assertTrue(net_policy.is_challenge(body="请完成人机校验后继续"))
        self.assertTrue(net_policy.is_challenge(title="访问频繁，请稍后再试"))
        self.assertFalse(net_policy.is_challenge(
            url="https://www.douyin.com/video/123", title="抖音"))

    def test_classify_failure_labels(self):
        self.assertEqual(net_policy.classify_failure(net_policy.BadPayload("x")),
                         "bad_payload")
        self.assertEqual(net_policy.classify_failure(net_policy.Blocked("x")),
                         "blocked")


class SessionTests(unittest.TestCase):
    def tearDown(self):
        net_policy.close_sessions()

    def test_default_ua_matches_project_constant(self):
        net_policy.close_sessions()
        session = net_policy.build_session(key="cdn")
        self.assertEqual(session.headers.get("User-Agent"), config.DESKTOP_UA)
        self.assertEqual(session.headers.get("Referer"), "https://www.douyin.com/")
        session.close()

    def test_explicit_proxy_and_ua_pool(self):
        env = {"JINGYING_PROXY": "http://127.0.0.1:7890",
               "JINGYING_UA_POOL": "UA-A, UA-B"}
        with mock.patch.dict("os.environ", env, clear=False):
            session = net_policy.build_session(key="asset")
            self.assertEqual(session.proxies.get("https"), "http://127.0.0.1:7890")
            self.assertIn(session.headers.get("User-Agent"), ("UA-A", "UA-B"))
            session.close()

    def test_session_for_reuses_per_thread_pool(self):
        net_policy.close_sessions()
        self.assertIs(net_policy.session_for("cdn"),
                      net_policy.session_for("cdn"))
        self.assertIsNot(net_policy.session_for("cdn"),
                         net_policy.session_for("asset"))


class WiringTests(StrategyTestBase):
    """确认策略确实接进了主链路，而不是只存在于新模块里。"""

    def test_downloader_session_comes_from_policy(self):
        self.assertIs(downloader._get_session(),
                      net_policy.session_for("cdn"))

    def test_parallel_retries_failed_download(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        dm = parallel.DownloadManager(1)
        dm.begin(1)
        done = []
        errors = []
        dm.item_done.connect(lambda idx, _res: done.append(idx))
        dm.item_error.connect(lambda idx, _msg: errors.append(idx))
        calls = {"n": 0}

        def flaky_download(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("cdn reset")
            return "RESULT"

        with mock.patch.object(parallel.downloader, "download_work", flaky_download):
            dm._run_one(0, (types.SimpleNamespace(aweme_id="a1"), "author", "work"),
                        dm._generation)
        dm.stop()
        self.assertEqual(calls["n"], 3)
        self.assertEqual(done, [0])
        self.assertEqual(errors, [])

    def test_parallel_does_not_retry_user_cancel(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        dm = parallel.DownloadManager(1)
        dm.begin(1)
        errors = []
        dm.item_error.connect(lambda idx, msg: errors.append((idx, msg)))
        calls = {"n": 0}

        def cancelled_download(*_args, **_kwargs):
            calls["n"] += 1
            raise downloader.DownloadCancelled("取消")

        with mock.patch.object(parallel.downloader, "download_work",
                               cancelled_download):
            dm._run_one(0, (types.SimpleNamespace(aweme_id="a2"), "a", "w"),
                        dm._generation)
        dm.stop()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(errors[0][1], "已取消")


class FakeResponse:
    def __init__(self, status=200):
        self.status = status


class FakeGotoPage:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def goto(self, _url, wait_until=None):
        self.calls += 1
        item = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeStatePage:
    def __init__(self, state):
        self.state = state
        self.calls = 0

    async def evaluate(self, _js):
        self.calls += 1
        return self.state


class ParserWiringTests(StrategyTestBase):
    def test_goto_retries_transient_navigation_error(self):
        page = FakeGotoPage([ConnectionError("net"), FakeResponse(200)])
        ok = asyncio.run(parser._goto_with_retry(page, "https://x", None))
        self.assertTrue(ok)
        self.assertEqual(page.calls, 2)

    def test_goto_treats_403_as_blocked_and_retries(self):
        page = FakeGotoPage([FakeResponse(403)] * 3)
        ok = asyncio.run(parser._goto_with_retry(page, "https://x", None))
        self.assertFalse(ok)
        self.assertEqual(page.calls, 3)

    def test_goto_cancel_raises_parse_cancelled(self):
        event = threading.Event()
        event.set()
        page = FakeGotoPage([FakeResponse(200)])
        with self.assertRaises(parser.ParseCancelled):
            asyncio.run(parser._goto_with_retry(page, "https://x", event))

    def test_poll_state_detects_challenge_without_original_switch(self):
        """以前只有打开「原画质」开关才检测人机校验。"""
        page = FakeStatePage({
            "detail": "", "play": "",
            "url": "https://www.douyin.com/captcha/verify", "title": "",
        })
        with self.assertRaises(parser.ParseError):
            asyncio.run(parser._poll_video_state(page, 1.0, None))
        self.assertEqual(page.calls, 1)

    def test_poll_state_returns_detail_immediately(self):
        page = FakeStatePage({
            "detail": "{\"aweme_detail\": {}}", "play": "p",
            "url": "https://www.douyin.com/video/1", "title": "抖音",
        })
        detail, play, _state = asyncio.run(
            parser._poll_video_state(page, 1.0, None))
        self.assertEqual(detail, "{\"aweme_detail\": {}}")
        self.assertEqual(play, "p")


if __name__ == "__main__":
    unittest.main()
