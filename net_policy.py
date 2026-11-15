# -*- coding: utf-8 -*-
"""网络策略层：统一重试、限流、自适应退避（AutoThrottle）与风控识别。

借鉴 Scrapling 的 Fetcher / Spider 分层思路，把原来散落在 downloader / parallel /
parser / app 里的「重试几次、等多久、什么时候该让一让」收敛到一处，但**不引入任何
新依赖**（只用标准库 + 项目已有的 requests），也不会改变 PyInstaller 打包形态。

设计边界，改代码前请先读完：

1. **只管策略，不管业务**：本模块不认识 MediaInfo / 作品 / WebDAV，只认识一个
   逻辑通道 ``key``（``page`` / ``cdn`` / ``asset``）和一次「尝试」的成败。
2. **默认不改变成功路径的速度**：令牌桶的速率取得比现有实际节奏宽松，
   AutoThrottle 的初始延迟是 0，只有真的出现 429/403/5xx、风控特征或明显变慢
   时才逐步加延迟，恢复后再缓慢回落。正常跑起来和以前一样快。
3. **可一键回退**：设 ``JINGYING_NET_POLICY=0`` 即完全停用新策略（限流直接放行、
   重试仍执行但退避归零、断言与诊断仍可用），便于现场比对。
4. **200 不等于成功**：抖音的失效大多是静默的——HTTP 200、JSON 结构完好，但
   ``aweme_detail`` 为空。``assert_payload`` / ``BadPayload`` 让这种坏数据也能
   触发重试，而不是悄悄降级成 720p 兜底。
5. **取消优先**：所有等待都是可中断的短睡（0.1s 粒度），停止同步 / 关窗时
   不会卡在退避或令牌桶里。

环境变量（全部可选，不设即使用默认值）：

===========================  ================================================
变量                          含义
===========================  ================================================
JINGYING_NET_POLICY           ``0`` 关闭全部新策略
JINGYING_NET_AUTOTHROTTLE     ``0`` 关闭自适应退避（保留固定令牌桶）
JINGYING_NET_QPS_PAGE         页面/接口通道令牌速率，默认 2/s
JINGYING_NET_QPS_CDN          媒体下载通道令牌速率，默认 20/s
JINGYING_NET_QPS_ASSET        封面/头像通道令牌速率，默认 6/s
JINGYING_NET_RETRY            单次操作最大尝试次数，默认 3
JINGYING_NET_BASE_DELAY       首次重试基准延迟（秒），默认 0.6
JINGYING_NET_MAX_DELAY        单次退避上限（秒），默认 8
JINGYING_PROXY                显式代理（如 http://127.0.0.1:7890），默认不设
JINGYING_UA_POOL              逗号分隔的 UA 池；不设则沿用 config.DESKTOP_UA
===========================  ================================================
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - requests 总会带 urllib3
    Retry = None

import config

log = logging.getLogger(__name__)

# 这些异常是「用户主动取消 / 需要人工介入」，重试没有意义，且会拖慢退出。
# 用类名而不是 import：避免 net_policy 反向依赖 parser / downloader 造成循环导入。
_NON_RETRYABLE_NAMES = frozenset({
    "DownloadCancelled", "ParseCancelled", "NeedLogin", "TooOldWebView",
    "SystemExit", "KeyboardInterrupt", "GeneratorExit", "CancelledError",
})

# 与 Scrapling 的 blocked-request detection 对应：这些状态说明对方在限我们。
_BLOCKED_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)

_CHALLENGE_MARKERS = (
    "captcha", "verify", "challenge", "secsdk", "risk/verify",
    "人机校验", "滑动验证", "请完成验证", "访问频繁", "操作过于频繁",
    "请输入验证码", "安全验证", "账号异常",
)


class BadPayload(RuntimeError):
    """HTTP 200 / 结构完好，但内容是风控兜底数据或已失效的空壳，应触发重试。"""


class Blocked(RuntimeError):
    """命中风控（429/403/人机校验页），应退避后重试，并让 AutoThrottle 降速。"""


# ---------------------------------------------------------------- 环境配置
def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, low: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(low, value)


def _env_int(name: str, default: int, low: int = 1) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(low, value)


def _sleep_interruptible(seconds: float, cancel_event=None, *,
                         sleep: Callable[[float], Any] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> bool:
    """可中断的等待；返回 False 表示期间被取消。

    桌面应用最怕「点了停止还在睡 8 秒」，所以退避一律拆成 0.1s 的短睡。
    ``sleep`` / ``clock`` 可注入，让离线测试不必真的等。
    """
    if seconds <= 0:
        return not (cancel_event is not None and cancel_event.is_set())
    deadline = clock() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return True
        sleep(min(0.1, remaining))


async def _asleep_interruptible(seconds: float, cancel_event=None, *,
                                asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                                clock: Callable[[], float] = time.monotonic) -> bool:
    if seconds <= 0:
        return not (cancel_event is not None and cancel_event.is_set())
    deadline = clock() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return True
        await asleep(min(0.1, remaining))


# ---------------------------------------------------------------- 重试策略
@dataclass
class RetryPolicy:
    """指数退避 + 抖动。``attempt`` 从 1 开始，表示「第 N 次失败后要等多久」。"""

    max_attempts: int = 3
    base_delay: float = 0.6
    max_delay: float = 8.0
    backoff: float = 2.0
    jitter: float = 0.25
    retry_statuses: tuple[int, ...] = _RETRY_STATUSES
    sleep: Callable[[float], Any] = time.sleep
    asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    rng: Callable[[], float] = random.random

    def delay_for(self, attempt: int) -> float:
        raw = self.base_delay * (self.backoff ** max(0, attempt - 1))
        raw = min(raw, self.max_delay)
        if self.jitter > 0:
            raw *= 1.0 + (self.rng() * 2.0 - 1.0) * self.jitter
        return max(0.0, min(raw, self.max_delay))

    def should_retry(self, exc: BaseException) -> bool:
        """默认：除「用户取消 / 需要登录」外都重试，保持项目原有的宽容语义。"""
        if isinstance(exc, (BadPayload, Blocked)):
            return True
        return type(exc).__name__ not in _NON_RETRYABLE_NAMES

    def wait(self, attempt: int, cancel_event=None) -> bool:
        return _sleep_interruptible(self.delay_for(attempt), cancel_event,
                                   sleep=self.sleep, clock=self.clock)

    async def wait_async(self, attempt: int, cancel_event=None) -> bool:
        return await _asleep_interruptible(self.delay_for(attempt), cancel_event,
                                           asleep=self.asleep, clock=self.clock)


@dataclass
class AttemptResult:
    """一次「带策略的调用」的完整结果，调用方据此决定成功/失败分支。"""

    ok: bool
    value: Any = None
    error: Optional[BaseException] = None
    attempts: int = 0
    elapsed_ms: float = 0.0
    delays: list[float] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "delays": [round(d, 3) for d in self.delays],
            "reason": self.reason,
            "error": str(self.error or "")[:200],
        }


def classify_failure(exc: BaseException) -> str:
    """把异常归成可统计的短标签，供诊断面板/日志使用。"""
    if isinstance(exc, BadPayload):
        return "bad_payload"
    if isinstance(exc, Blocked):
        return "blocked"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status in (403, 429):
            return f"http_{status}_blocked"
        if status:
            return f"http_{status}"
        return "http_error"
    if isinstance(exc, requests.exceptions.RequestException):
        return "request"
    return type(exc).__name__.lower()


def status_of(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    return int(getattr(response, "status_code", 0) or 0)


def retry_after_of(exc: BaseException) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 限流
class TokenBucket:
    """按逻辑通道的令牌桶。无后台线程，懒补充，线程安全。"""

    def __init__(self, rate: float, capacity: Optional[float] = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = time.sleep,
                 asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.rate = max(0.01, float(rate))
        self.capacity = float(capacity) if capacity else self.rate * 2.0
        self._tokens = self.capacity
        self._updated = clock()
        self._lock = threading.Lock()
        self.clock = clock
        self.sleep = sleep
        self.asleep = asleep

    def _refill(self, now: float) -> None:
        delta = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + delta * self.rate)

    def _needed_wait(self, tokens: float, now: float) -> float:
        self._refill(now)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        return (tokens - self._tokens) / self.rate

    def acquire(self, tokens: float = 1.0, *, cancel_event=None,
                timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            with self._lock:
                wait_s = self._needed_wait(tokens, self.clock())
            if wait_s <= 0:
                return True
            if deadline is not None and self.clock() + wait_s > deadline:
                return False
            if not _sleep_interruptible(wait_s, cancel_event,
                                        sleep=self.sleep, clock=self.clock):
                return False

    async def acquire_async(self, tokens: float = 1.0, *, cancel_event=None,
                            timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            with self._lock:
                wait_s = self._needed_wait(tokens, self.clock())
            if wait_s <= 0:
                return True
            if deadline is not None and self.clock() + wait_s > deadline:
                return False
            if not await _asleep_interruptible(wait_s, cancel_event,
                                               asleep=self.asleep, clock=self.clock):
                return False


class AutoThrottle:
    """被限流时自动降速、恢复后缓慢提速（对应 Scrapling 的 AutoThrottle）。

    平时 delay=0，不改变速度；只有 429/403/5xx、风控特征或响应明显变慢才加延迟。
    ``Retry-After`` 会被尊重，但封顶 5 秒——桌面应用不能为了一个响应头卡住 UI。
    """

    def __init__(self, *, max_delay: float = 3.0, min_delay: float = 0.0,
                 slow_threshold: float = 2.0, backoff: float = 2.0,
                 decay: float = 0.8, min_blocked_delay: float = 0.5,
                 retry_after_cap: float = 5.0, enabled: bool = True):
        self.max_delay = max_delay
        self.min_delay = min_delay
        self.slow_threshold = slow_threshold
        self.backoff = backoff
        self.decay = decay
        self.min_blocked_delay = min_blocked_delay
        self.retry_after_cap = retry_after_cap
        self.enabled = enabled
        self._delays: dict[str, float] = {}
        self._lock = threading.Lock()

    def delay_for(self, key: str) -> float:
        if not self.enabled:
            return 0.0
        with self._lock:
            return self._delays.get(key, self.min_delay)

    def record(self, key: str, *, latency_s: float = 0.0, ok: bool = True,
               status: int = 0, retry_after: Optional[float] = None,
               blocked: bool = False) -> float:
        if not self.enabled:
            return 0.0
        with self._lock:
            cur = self._delays.get(key, self.min_delay)
            if blocked or status in _BLOCKED_STATUSES:
                nxt = min(self.max_delay, max(cur * self.backoff,
                                              self.min_blocked_delay))
                if retry_after is not None:
                    nxt = max(nxt, min(self.retry_after_cap, retry_after))
            elif not ok:
                nxt = min(self.max_delay, max(cur, 0.1))
            elif latency_s > self.slow_threshold:
                nxt = min(self.max_delay,
                          max(cur, min(latency_s * 0.25, 0.5)))
            else:
                nxt = max(self.min_delay, cur * self.decay)
                if nxt < 0.01:
                    nxt = 0.0
            self._delays[key] = nxt
            return nxt

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {k: round(v, 3) for k, v in self._delays.items()}

    def reset(self) -> None:
        with self._lock:
            self._delays.clear()


class DomainThrottle:
    """令牌桶（固定 QPS 上限）+ AutoThrottle（被限流时自适应降速）的组合。"""

    def __init__(self, rates: dict[str, float], *, enabled: bool = True,
                 auto: Optional[AutoThrottle] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = time.sleep,
                 asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.enabled = enabled
        self.rates = dict(rates)
        self.auto = auto or AutoThrottle()
        self.clock = clock
        self.sleep = sleep
        self.asleep = asleep
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.rates.get(key, 8.0),
                                     clock=self.clock, sleep=self.sleep,
                                     asleep=self.asleep)
                self._buckets[key] = bucket
            return bucket

    def wait(self, key: str, cancel_event=None) -> bool:
        if not self.enabled:
            return True
        if not self._bucket(key).acquire(cancel_event=cancel_event):
            return False
        extra = self.auto.delay_for(key)
        return _sleep_interruptible(extra, cancel_event,
                                    sleep=self.sleep, clock=self.clock) if extra > 0 else True

    async def wait_async(self, key: str, cancel_event=None) -> bool:
        if not self.enabled:
            return True
        if not await self._bucket(key).acquire_async(cancel_event=cancel_event):
            return False
        extra = self.auto.delay_for(key)
        if extra > 0:
            return await _asleep_interruptible(extra, cancel_event,
                                               asleep=self.asleep, clock=self.clock)
        return True

    def record(self, key: str, *, latency_s: float = 0.0, ok: bool = True,
               status: int = 0, retry_after: Optional[float] = None,
               blocked: bool = False) -> float:
        return self.auto.record(key, latency_s=latency_s, ok=ok, status=status,
                                retry_after=retry_after, blocked=blocked)

    def snapshot(self) -> dict:
        return {"rates": dict(self.rates), "auto_delays": self.auto.snapshot(),
                "enabled": self.enabled}


# ---------------------------------------------------------------- 诊断
class FetchDiagnostics:
    """轻量级结构化事件环形缓冲，供日志与排障读取（不落盘、不进 UI 线程）。"""

    def __init__(self, maxlen: int = 200):
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._counters: Counter = Counter()
        self._lock = threading.Lock()

    def record(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)
            key = event.get("key", "")
            self._counters["total"] += 1
            self._counters["ok" if event.get("ok") else "fail"] += 1
            self._counters[f"key:{key}"] += 1
            # 每个「重试尝试」事件 +1；不能用 attempts-1 累加，否则
            # 第 2、3 次尝试会把同一次重试重复计数。
            if event.get("retry"):
                self._counters["retried"] += 1
            reason = event.get("reason")
            if reason:
                self._counters[f"reason:{reason}"] += 1

    def snapshot(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._events)[-limit:]

    def summary(self) -> dict:
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._counters.clear()


# ---------------------------------------------------------------- 内容断言 / 风控识别
def check_payload(obj: Any, kind: str) -> str:
    """返回空串表示可用，否则返回失败原因（不抛异常，方便只记日志的场景）。

    抖音的失效大多是「HTTP 200 但字段空了」，只看状态码会长期把兜底数据当成功。
    ``kind``: ``detail``（作品详情）/ ``post``（主页作品列表）/ ``user``（用户信息）。
    """
    if not isinstance(obj, dict) or not obj:
        return "响应不是非空对象"
    if kind == "detail":
        if not str(obj.get("aweme_id") or obj.get("awemeId") or ""):
            return "缺 aweme_id"
        video = obj.get("video")
        images = obj.get("images")
        # 故意只做「骨架」断言：图集作品常常没有 video，直播/特殊作品常常
        # 没有 bit_rate。判得太细会把正常作品误判成风控数据，反而制造降级。
        if not isinstance(video, dict) and not isinstance(images, list):
            return "video / images 均缺失，疑似风控兜底数据"
        return ""
    if kind == "post":
        items = obj.get("aweme_list")
        if items is None and isinstance(obj.get("data"), dict):
            items = obj["data"].get("aweme_list")
        if not isinstance(items, list):
            return "缺 aweme_list"
        return ""
    if kind == "user":
        return "" if obj else "用户信息为空"
    return ""


def assert_payload(obj: Any, kind: str) -> None:
    """``check_payload`` 的严格版：坏数据抛 BadPayload 以触发重试。"""
    reason = check_payload(obj, kind)
    if reason:
        raise BadPayload(f"{kind} 断言失败：{reason}")


def is_challenge(url: str = "", title: str = "", body: str = "") -> bool:
    """基于 URL / 标题 / 正文关键字识别人机校验与频控页。

    只在「没拿到数据 / 解析失败」的路径上调用，避免正常页面里的登录面板误伤。
    """
    haystack = f"{url} {title} {body[:4000]}".lower()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


# ---------------------------------------------------------------- 会话
def _proxy_config() -> Optional[dict]:
    proxy = os.environ.get("JINGYING_PROXY", "").strip()
    if not proxy:
        # 不设时沿用 requests 的 trust_env（系统 HTTP(S)_PROXY 仍然生效）
        return None
    return {"http": proxy, "https": proxy}


def user_agent(key: str = "") -> str:
    """默认沿用项目唯一的 DESKTOP_UA；设置 JINGYING_UA_POOL 才启用轮换。

    注意：轮换只作用于纯 HTTP 通道（封面/头像/CDN 下载），浏览器通道由 Playwright
    单独设置 UA，两者不一致反而更容易被识别，因此默认绝不轮换。
    """
    pool = [u.strip() for u in os.environ.get("JINGYING_UA_POOL", "").split(",")
            if u.strip()]
    if pool:
        return random.choice(pool)
    return getattr(config, "DESKTOP_UA", "")


def build_session(*, key: str = "cdn", headers: Optional[dict] = None,
                  verify: Optional[bool] = None) -> requests.Session:
    """统一构造 requests.Session：连接池 + Retry + UA + 可选代理。

    与旧版 downloader._get_session 的差异只有：408/425 也纳入重试状态、
    显式尊重 Retry-After、可选代理与 UA 池。默认行为完全一致。
    """
    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=2, connect=2, read=2, status=2,
            backoff_factor=0.6,
            status_forcelist=_RETRY_STATUSES,
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    else:  # pragma: no cover
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    merged = {"User-Agent": user_agent(key)}
    if key != "webdav":
        merged["Referer"] = "https://www.douyin.com/"
    if headers:
        merged.update(headers)
    session.headers.update(merged)
    proxies = _proxy_config()
    if proxies:
        session.proxies.update(proxies)
    if verify is not None:
        session.verify = verify
    return session


_SESSION_LOCAL = threading.local()


def session_for(key: str = "cdn", **kwargs) -> requests.Session:
    """每线程、每通道复用一个连接池（TLS 握手只做一次）。"""
    store = getattr(_SESSION_LOCAL, "sessions", None)
    if store is None:
        store = {}
        _SESSION_LOCAL.sessions = store
    session = store.get(key)
    if session is None:
        session = build_session(key=key, **kwargs)
        store[key] = session
    return session


def close_sessions() -> None:
    store = getattr(_SESSION_LOCAL, "sessions", None)
    if not store:
        return
    for session in store.values():
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    store.clear()


# ---------------------------------------------------------------- 运行时单例
class NetworkPolicy:
    """把上面各块组装成一个可替换的运行时；测试用 ``reset_runtime()`` 隔离。"""

    def __init__(self):
        self.enabled = _env_bool("JINGYING_NET_POLICY", True)
        auto_enabled = _env_bool("JINGYING_NET_AUTOTHROTTLE", True) and self.enabled
        self.auto = AutoThrottle(enabled=auto_enabled)
        rates = {
            "page": _env_float("JINGYING_NET_QPS_PAGE", 2.0, 0.1),
            "cdn": _env_float("JINGYING_NET_QPS_CDN", 20.0, 0.1),
            "asset": _env_float("JINGYING_NET_QPS_ASSET", 6.0, 0.1),
            "webdav": _env_float("JINGYING_NET_QPS_WEBDAV", 6.0, 0.1),
        }
        self.throttle = DomainThrottle(rates, enabled=self.enabled, auto=self.auto)
        self.retry = RetryPolicy(
            max_attempts=_env_int("JINGYING_NET_RETRY", 3),
            base_delay=_env_float("JINGYING_NET_BASE_DELAY", 0.6),
            max_delay=_env_float("JINGYING_NET_MAX_DELAY", 8.0),
        )
        self.diagnostics = FetchDiagnostics()

    # -- 便捷方法 ---------------------------------------------------------
    def wait(self, key: str, cancel_event=None) -> bool:
        return self.throttle.wait(key, cancel_event)

    async def wait_async(self, key: str, cancel_event=None) -> bool:
        return await self.throttle.wait_async(key, cancel_event)

    def record(self, key: str, *, latency_s: float = 0.0, ok: bool = True,
               status: int = 0, retry_after: Optional[float] = None,
               blocked: bool = False, attempt: int = 1, reason: str = "") -> None:
        self.throttle.record(key, latency_s=latency_s, ok=ok, status=status,
                             retry_after=retry_after, blocked=blocked)
        self.diagnostics.record({
            "ts": round(time.time(), 3), "key": key, "ok": ok,
            "status": status, "latency_ms": round(latency_s * 1000, 1),
            "blocked": blocked, "auto_delay": round(self.auto.delay_for(key), 3),
            "attempt": attempt, "retry": attempt > 1, "reason": reason,
        })

    def snapshot(self) -> dict:
        return {"enabled": self.enabled,
                "throttle": self.throttle.snapshot(),
                "retry": {"max_attempts": self.retry.max_attempts,
                          "base_delay": self.retry.base_delay,
                          "max_delay": self.retry.max_delay},
                "summary": self.diagnostics.summary()}


_GLOBAL: Optional[NetworkPolicy] = None
_GLOBAL_LOCK = threading.Lock()


def policy() -> NetworkPolicy:
    global _GLOBAL
    if _GLOBAL is None:
        with _GLOBAL_LOCK:
            if _GLOBAL is None:
                _GLOBAL = NetworkPolicy()
    return _GLOBAL


def reset_runtime() -> NetworkPolicy:
    """清空限流/退避/诊断状态并重建单例（仅供测试与手动重置）。"""
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = NetworkPolicy()
    return _GLOBAL


def runtime_snapshot() -> dict:
    return policy().snapshot()


# ---------------------------------------------------------------- 带策略的调用
def retry_sync(fn: Callable[[], Any], *, key: str = "", tag: str = "",
               validate: Optional[Callable[[Any], None]] = None,
               should_retry: Optional[Callable[[BaseException], bool]] = None,
               on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
               cancel_event=None, runtime: Optional[NetworkPolicy] = None,
               throttle: bool = True) -> AttemptResult:
    """同步版：限流 → 尝试 → 断言 → 失败退避重试 → 记录诊断。

    ``validate(result)`` 里可以调 ``assert_payload``，让「200 但坏数据」也重试。
    不抛异常：结果一律通过 ``AttemptResult`` 返回，调用方自己决定如何降级。
    """
    rt = runtime or policy()
    rp = rt.retry
    check = should_retry or rp.should_retry
    started = time.monotonic()
    delays: list[float] = []
    last_exc: Optional[BaseException] = None
    for attempt in range(1, rp.max_attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            return AttemptResult(False, attempts=attempt, reason="cancelled")
        if throttle:
            rt.wait(key, cancel_event)
        t0 = time.monotonic()
        try:
            value = fn()
            if validate is not None:
                validate(value)
            latency = time.monotonic() - t0
            rt.record(key, latency_s=latency, ok=True, attempt=attempt)
            return AttemptResult(True, value=value, attempts=attempt,
                                 elapsed_ms=(time.monotonic() - started) * 1000,
                                 delays=delays)
        except Exception as exc:  # noqa: BLE001 - 策略层必须吞下并分类所有业务异常
            last_exc = exc
            latency = time.monotonic() - t0
            status = status_of(exc)
            blocked = isinstance(exc, Blocked) or status in _BLOCKED_STATUSES
            reason = classify_failure(exc)
            rt.record(key, latency_s=latency, ok=False, status=status,
                      retry_after=retry_after_of(exc), blocked=blocked,
                      attempt=attempt, reason=reason)
            if attempt >= rp.max_attempts or not check(exc):
                return AttemptResult(False, error=exc, attempts=attempt,
                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                     delays=delays, reason=reason)
            delay = 0.0 if not rt.enabled else rp.delay_for(attempt)
            delays.append(delay)
            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:  # noqa: BLE001 - 回调出错不能盖掉原异常
                    log.debug("retry callback failed", exc_info=True)
            log.info("net_policy retry key=%s tag=%s attempt=%d/%d delay=%.2fs reason=%s",
                     key, tag, attempt, rp.max_attempts, delay, reason)
            if not rp.wait(attempt, cancel_event):
                return AttemptResult(False, error=exc, attempts=attempt,
                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                     delays=delays, reason="cancelled")
    return AttemptResult(False, error=last_exc, attempts=rp.max_attempts,
                         reason=classify_failure(last_exc) if last_exc else "unknown")


async def retry_async(fn: Callable[[], Awaitable[Any]], *, key: str = "", tag: str = "",
                      validate: Optional[Callable[[Any], None]] = None,
                      should_retry: Optional[Callable[[BaseException], bool]] = None,
                      on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
                      cancel_event=None, runtime: Optional[NetworkPolicy] = None,
                      throttle: bool = True) -> AttemptResult:
    """异步版：用于 Playwright 页面操作（goto / evaluate / 整段抓取）。"""
    rt = runtime or policy()
    rp = rt.retry
    check = should_retry or rp.should_retry
    started = time.monotonic()
    delays: list[float] = []
    last_exc: Optional[BaseException] = None
    for attempt in range(1, rp.max_attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            return AttemptResult(False, attempts=attempt, reason="cancelled")
        if throttle:
            await rt.wait_async(key, cancel_event)
        t0 = time.monotonic()
        try:
            value = await fn()
            if validate is not None:
                validate(value)
            latency = time.monotonic() - t0
            rt.record(key, latency_s=latency, ok=True, attempt=attempt)
            return AttemptResult(True, value=value, attempts=attempt,
                                 elapsed_ms=(time.monotonic() - started) * 1000,
                                 delays=delays)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            latency = time.monotonic() - t0
            status = status_of(exc)
            blocked = isinstance(exc, Blocked) or status in _BLOCKED_STATUSES
            reason = classify_failure(exc)
            rt.record(key, latency_s=latency, ok=False, status=status,
                      retry_after=retry_after_of(exc), blocked=blocked,
                      attempt=attempt, reason=reason)
            if attempt >= rp.max_attempts or not check(exc):
                return AttemptResult(False, error=exc, attempts=attempt,
                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                     delays=delays, reason=reason)
            delay = 0.0 if not rt.enabled else rp.delay_for(attempt)
            delays.append(delay)
            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:  # noqa: BLE001
                    log.debug("retry callback failed", exc_info=True)
            log.info("net_policy retry(async) key=%s tag=%s attempt=%d/%d "
                     "delay=%.2fs reason=%s",
                     key, tag, attempt, rp.max_attempts, delay, reason)
            if not await rp.wait_async(attempt, cancel_event):
                return AttemptResult(False, error=exc, attempts=attempt,
                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                     delays=delays, reason="cancelled")
    return AttemptResult(False, error=last_exc, attempts=rp.max_attempts,
                         reason=classify_failure(last_exc) if last_exc else "unknown")
