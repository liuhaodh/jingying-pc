# -*- coding: utf-8 -*-
"""回捞抖音「原始上传码流」，对齐《抖音「原画质」获取实现规范》(2026-11)。

和规范一致的三条边界，改代码前请先读完：

1. 这里拿到的**不是上传时那个文件的字节副本**。目标是「视频码流未经重编码」，
   容器与元数据仍会被平台用 FFmpeg 重封装。需要 SHA-256 对齐时请回头找原文件。
2. **不实现、不破解任何签名**（a_bogus / msToken）。需要签名的 detail 请求一律
   由真实作品页的 JS 代发，本模块只监听响应；play 端点在已登录的浏览器上下文
   内发出，cookie 天然正确。
3. 仅用于**本人作品或已获授权内容**的归档与画质分析，不用于批量抓取他人作品。
   原片通道并非对所有作品开放，拿不到是正常结果，此时回退最高转码档。

四个层次：

- 纯判定：`assert_detail` / `best_gear` / `build_play_url` / `cdn_info` /
  `classify` / `detect_challenge` —— 不碰网络，离线可测；
- 页面内解析：`resolve_original`（async，接在 parser.resolve_video 的浏览器上下文里）；
- 下载时即时回捞：`OriginalSession`（sync，独立 profile，跨作品复用 + 短 TTL 缓存），
  避免「先解析一千个作品、几十分钟后再下载」时 CDN 直链已过期；
- 落盘后校验：`inspect_mp4`（不需要 ffprobe）。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import config
import media_probe

PLAY_ENDPOINT = "https://www.douyin.com/aweme/v1/play/"
DETAIL_MARK = "/aweme/v1/web/aweme/detail/"
REFERER = "https://www.douyin.com/"
MIN_CDN_BYTES = 100 * 1024          # 断言 4：小于 100 KB 视为占位/降级流
ORIGINAL_FACTOR = 1.2                # 一级判定阈值
CACHE_TTL = 120.0                    # 直链新鲜度窗口（秒）
THROTTLE = (1.2, 3.0)               # 规范建议的请求间随机延迟
VIDEO_TAGS = (b"avc1", b"hvc1", b"hev1", b"av01", b"vp09", b"avc3")
_LOGIN_COOKIES = ("sessionid", "sid_tt", "sid_guard", "passport_auth", "uid_tt")
_CAPTURE_TIME = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_LAVF = re.compile(rb"Lavf\d+\.\d+")

# fetch 回退：只取最终 URL，立刻取消响应体，别把 80 MB 视频拉一遍。
_FETCH_FINAL_URL_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include', redirect: 'follow'});
  const final = r.url;
  try { if (r.body) await r.body.cancel(); } catch (e) {}
  return final;
}"""

_ENABLED = False
_lock = threading.RLock()
_cache: dict[str, tuple[float, "OriginalStream"]] = {}
_session: "OriginalSession | None" = None
_FAILURE_UNTIL = 0.0            # 会话级失败（未登录/无浏览器）后的冷却截止时刻
FAILURE_COOLDOWN = 120.0


class OriginalStreamError(RuntimeError):
    """回捞链路的软失效（结构变化、风控兜底、未登录、CDN 头不对）。"""


@dataclass(frozen=True)
class OriginalStream:
    video_id: str = ""
    url: str = ""
    size: int = 0
    best_size: int = 0
    best_gear: str = ""
    factor: float = 0.0
    verdict: str = "transcoded"      # original | transcoded
    content_type: str = ""
    via: str = "play"                # play | cache | fetch-fallback

    @property
    def is_original(self) -> bool:
        return self.verdict == "original" and bool(self.url)

    def label(self) -> str:
        """给结果卡片/日志用的人话标签。"""
        if not self.is_original:
            return "最高转码档"
        mb = self.size / 1048576
        return f"原始码流 {mb:.1f}MB · {self.factor:.1f}x"

    def as_dict(self) -> dict:
        return {
            "video_id": self.video_id, "url": self.url, "size": self.size,
            "best_size": self.best_size, "best_gear": self.best_gear,
            "factor": round(self.factor, 3), "verdict": self.verdict,
            "content_type": self.content_type, "via": self.via,
        }


# ---------------------------------------------------------------- 开关与缓存
def set_enabled(flag: bool) -> None:
    global _ENABLED, _FAILURE_UNTIL
    with _lock:
        _ENABLED = bool(flag)
        # 用户重新打开开关时给一次全新的尝试机会
        _FAILURE_UNTIL = 0.0
        if not _ENABLED:
            _cache.clear()


def enabled() -> bool:
    return _ENABLED


def remember(stream: OriginalStream) -> None:
    """解析阶段拿到的直链放进短 TTL 缓存，紧接着的下载直接复用。"""
    if not stream.is_original:
        return
    with _lock:
        _cache[stream.video_id] = (time.monotonic(), stream)


def cached(video_id: str, force: bool = False) -> OriginalStream | None:
    with _lock:
        hit = _cache.get(video_id)
        if not hit:
            return None
        stamp, stream = hit
        if force or time.monotonic() - stamp > CACHE_TTL:
            _cache.pop(video_id, None)
            return None
        return stream


def clear_cache(video_id: str = "") -> None:
    with _lock:
        if video_id:
            _cache.pop(video_id, None)
        else:
            _cache.clear()


# ---------------------------------------------------------------- 纯判定逻辑
def header_get(headers, name: str) -> str:
    """Playwright 的 headers 是小写字典型的 dict，桩对象可能用任意大小写。"""
    if not headers:
        return ""
    target = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == target:
                return str(value or "")
    except AttributeError:
        pass
    getter = getattr(headers, "get", None)
    return str(getter(name) or "") if callable(getter) else ""


def _allowed_cdn(url: str) -> bool:
    """CDN 直链必须落在项目既有的主机白名单内，防止 302 被劫持到任意地址。"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host:
            return False
        return any(host == suffix or host.endswith("." + suffix)
                   for suffix in config.ALLOWED_HOST_SUFFIXES)
    except ValueError:
        return False


def assert_detail(detail: dict) -> None:
    """规范第 7 节前三条软失效断言。

    抖音的失效大多是静默的：HTTP 200、JSON 结构完好，但 aweme_detail 为空、
    play_addr 缺失、data_size 变得很小。只看状态码会长期返回坏数据。
    """
    if not isinstance(detail, dict) or not detail:
        raise OriginalStreamError("aweme_detail 为空（命中风控兜底数据）")
    if not str(detail.get("aweme_id") or detail.get("awemeId") or ""):
        raise OriginalStreamError("断言 1 失败：aweme_id 为空，解析层可能已失效")
    author = detail.get("author")
    author_name = author.get("nickname", "") if isinstance(author, dict) else author
    if not str(detail.get("desc") or "").strip() and not str(author_name or "").strip():
        raise OriginalStreamError("断言 2 失败：desc 与 author 同时为空，疑似风控兜底数据")
    video = detail.get("video")
    if not isinstance(video, dict):
        raise OriginalStreamError("断言 3 失败：video 节点缺失")
    play_addr = video.get("play_addr")
    vid = play_addr.get("uri") if isinstance(play_addr, dict) else ""
    if not vid or not (video.get("bit_rate") or []):
        raise OriginalStreamError(
            "断言 3 失败：video.play_addr.uri 或 bit_rate 缺失，作品不可见或结构已变更")
    return None


def video_id_of(detail: dict) -> str:
    """★ 原片标识是 video.play_addr.uri，不是 aweme_id，也不是一个 URL。"""
    video = detail.get("video") if isinstance(detail, dict) else None
    play_addr = video.get("play_addr") if isinstance(video, dict) else None
    return str(play_addr.get("uri") or "") if isinstance(play_addr, dict) else ""


def best_gear(video: dict) -> dict:
    """规范 Step 5：按 play_addr.data_size 降序取最高转码档。

    不能按分辨率或 gear_name 排序。实测反例：normal_540_0 (4.99 MB) 比
    normal_720_0 (4.18 MB) 还大，按分辨率排会取错档。
    这里只用来算 factor 的分母，**不改**项目原有的「分辨率优先」下载选档。
    """
    gears = []
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        addr = item.get("play_addr")
        if not isinstance(addr, dict):
            continue
        urls = addr.get("url_list") or []
        if not urls:
            continue
        try:
            size = int(addr.get("data_size") or item.get("data_size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            width, height = int(addr.get("width") or 0), int(addr.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        gears.append({
            "gear_name": str(item.get("gear_name") or ""),
            "size": size,
            "url": str(urls[0] or ""),
            "width": width,
            "height": height,
            "is_h265": bool(item.get("is_h265") or item.get("is_bytevc1")),
        })
    if not gears:
        # 没有阶梯时用主 play_addr 自己的体积兜底，绝不把分母当 0（会假阳性）
        addr = video.get("play_addr") if isinstance(video, dict) else None
        if isinstance(addr, dict):
            try:
                size = int(addr.get("data_size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size:
                urls = addr.get("url_list") or []
                gears.append({"gear_name": "play_addr", "size": size,
                              "url": str(urls[0]) if urls else "",
                              "width": 0, "height": 0, "is_h265": False})
    if not gears:
        raise OriginalStreamError("bit_rate 为空且 play_addr 无 data_size，无法做体积倍率判定")
    gears.sort(key=lambda g: g["size"], reverse=True)
    return gears[0]


def build_play_url(video_id: str, ratio: str = "default", line: int = 0,
                   improve_bitrate: int = 1, watermark: int = 0) -> str:
    """★ ratio=default 走「原始/默认」通道，不要传 1080p/720p/4k。"""
    query = urlencode({
        "video_id": video_id, "ratio": ratio, "line": line,
        "improve_bitrate": improve_bitrate, "watermark": watermark,
    })
    return PLAY_ENDPOINT + "?" + query


def cdn_info(headers, size_override: int = 0) -> tuple[int, str]:
    """规范断言 4：体积 > 100 KB 且 Content-Type 以 video/ 开头。

    部分 CDN 对 `HEAD` 返回 404 `application/xml`，但支持 `Range` GET；这时
    响应里是 `Content-Range: bytes 0-1023/40203845`，总长度要从斜杠后面取，
    不能拿 `Content-Length: 1024` 当文件体积。
    """
    try:
        size = int(header_get(headers, "content-length") or 0)
    except (TypeError, ValueError):
        size = 0
    content_range = header_get(headers, "content-range")
    match = re.search(r"/\s*(\d+)\s*$", content_range or "")
    if match:
        size = int(match.group(1))
    if not size:
        size = int(size_override or 0)
    content_type = header_get(headers, "content-type")
    if size <= MIN_CDN_BYTES or not content_type.lower().startswith("video/"):
        raise OriginalStreamError(
            f"断言 4 失败：CDN 头异常（{size} 字节 / {content_type or '无 Content-Type'}），"
            "可能是降级流或占位流")
    return size, content_type


def need_range_fallback(headers, status: int) -> bool:
    """HEAD 失败时是否值得再试 Range GET（而不是直接判定为降级流）。"""
    if status >= 400:
        return True
    return not header_get(headers, "content-length")


RANGE_HEADERS = {"Range": "bytes=0-1023"}


def classify(original_size: int, best_size: int,
             threshold: float = ORIGINAL_FACTOR) -> tuple[str, float]:
    """一级判定：factor >= 1.2 → original，否则 transcoded。

    实测：真原片 24.2x / 3.72x，通道未开放 0.43x，中间没有模糊地带。
    """
    try:
        best = int(best_size or 0)
        original = int(original_size or 0)
    except (TypeError, ValueError):
        return "transcoded", 0.0
    if best <= 0 or original <= 0:
        return "transcoded", 0.0
    factor = original / best
    return ("original" if factor >= float(threshold) else "transcoded", factor)


def detect_challenge(url: str = "", title: str = "") -> bool:
    """人机校验检测（规范 Step 2）。命中时应当明确报错，不要返回坏数据。"""
    text = (str(url or "") + " " + str(title or "")).lower()
    return any(mark in text for mark in
               ("verify", "captcha", "验证", "滑动", "secsdk"))


def find_nested_detail(root) -> dict | None:
    """规范 Step 3：从 window._ROUTER_DATA 里递归找首个 aweme_detail / item 节点。

    _ROUTER_DATA 结构会变，写死完整路径一定失效。
    """
    seen = set()

    def walk(node, depth=0):
        if depth > 12 or id(node) in seen:
            return None
        if isinstance(node, dict):
            seen.add(id(node))
            # 路径二：item_list[0] 本身就是 detail，没有外层 key 包着
            if node.get("aweme_id") and any(
                    key in node for key in ("video", "images", "desc")):
                return node
            for key in ("aweme_detail", "aweme", "item"):
                value = node.get(key)
                if isinstance(value, dict) and value.get("aweme_id"):
                    return value
            for value in node.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(node, list):
            seen.add(id(node))
            for value in node[:50]:
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(root)


# ---------------------------------------------------------------- 页面内回捞
async def resolve_original(context, page, detail: dict, *, request=None,
                           sleeper=None, throttle: bool = True) -> OriginalStream:
    """在**已登录的浏览器上下文内**回捞原片直链（规范 Step 6）。

    `context` / `page` 由 parser.resolve_video 提供：页面自己的 JS 已经算好
    签名并发出 detail 请求，这里不需要（也不应该）再实现签名。
    失败一律抛 OriginalStreamError，由调用方记录后回退最高转码档。
    """
    assert_detail(detail)
    vid = video_id_of(detail)
    gear = best_gear(detail["video"])
    req = request or context.request
    sleep = sleeper or asyncio.sleep
    if throttle:
        await sleep(random.uniform(*THROTTLE))

    headers = {"Referer": REFERER, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    url = build_play_url(vid)
    # max_redirects=0：只读 Location，绝不把几十 MB 视频拉一遍
    response = await req.get(url, headers=headers, max_redirects=0, timeout=30000)
    status = int(getattr(response, "status", 0) or 0)
    cdn = header_get(getattr(response, "headers", {}), "location")
    via = "play"
    if not cdn and page is not None:
        # 降级：页面上下文内 fetch 跟随后取最终 URL；拿到就取消响应体
        cdn = await page.evaluate(_FETCH_FINAL_URL_JS, url)
        via = "fetch-fallback"
    if not cdn:
        raise OriginalStreamError(
            f"play 端点没有返回 Location（HTTP {status}），原片通道可能未开放")
    if not _allowed_cdn(cdn):
        raise OriginalStreamError("play 端点返回的 CDN 域名不在白名单内")
    head = await req.head(cdn, headers={"Referer": REFERER}, timeout=20000)
    head_headers = getattr(head, "headers", {})
    try:
        size, content_type = cdn_info(head_headers)
    except OriginalStreamError:
        if not need_range_fallback(head_headers,
                                   int(getattr(head, "status", 0) or 0)):
            raise
        # 腾讯 COS 一类 CDN 的 HEAD 会回 404 application/xml，但 Range GET 正常。
        # 只读 1 KB（不取视频正文），从 Content-Range 拿总长度。
        ranged = await req.get(cdn, headers={"Referer": REFERER, **RANGE_HEADERS},
                               max_redirects=0, timeout=20000)
        size, content_type = cdn_info(getattr(ranged, "headers", {}))
        logging.info("CDN HEAD 不可用，改用 Range GET 取体积：%s 字节", size)
    verdict, factor = classify(size, gear["size"])
    stream = OriginalStream(
        video_id=vid, url=cdn, size=size, best_size=gear["size"],
        best_gear=gear["gear_name"], factor=factor, verdict=verdict,
        content_type=content_type, via=via)
    logging.info("original stream probe: aweme=%s vid=%s size=%s best=%s factor=%.2f verdict=%s",
                 detail.get("aweme_id"), vid[:24], size, gear["size"], factor, verdict)
    remember(stream)
    return stream


# ---------------------------------------------------------------- 下载时回捞
class OriginalSession:
    """sync Playwright 会话：下载线程里按 video_id 即时取新鲜直链。

    用独立 profile 目录，避免与主页抓取争用同一个 Chromium 用户目录；Cookie
    从项目已保存的 cookies.json 注入，登录态由用户此前「登录抖音」建立。
    单个实例跨作品复用，请求用锁串行 + 规范要求的小节流，避免触发风控。
    """

    def __init__(self, session_dir: str = "", cookie_file: str = "",
                 headless: bool = True, sleeper=None):
        self.session_dir = session_dir or os.path.join(config.DATA_DIR, "browser_session_original")
        self.cookie_file = cookie_file or os.path.join(config.DATA_DIR, "cookies.json")
        self.headless = headless
        self._sleeper = sleeper or time.sleep
        self._pw = None
        self._ctx = None

    def _ensure(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright  # 延迟导入：离线测试不需要
        os.makedirs(self.session_dir, exist_ok=True)
        pw = sync_playwright().start()
        try:
            ctx = pw.chromium.launch_persistent_context(
                self.session_dir,
                headless=self.headless,
                executable_path=config.CHROME_PATH or None,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent=config.DESKTOP_UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN", timezone_id="Asia/Shanghai",
            )
        except Exception:
            pw.stop()
            raise
        try:
            saved = self._load_cookies()
            if saved:
                try:
                    ctx.add_cookies(saved)
                except Exception:  # noqa: BLE001 - cookie 过期不影响后续预热
                    logging.warning("original session: 注入已保存 Cookie 失败", exc_info=True)
            # 预热：让平台下发 ttwid / odin_tt / s_v_web_id 等基础 cookie
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                          timeout=30000)
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001 - 预热失败不致命，后面请求会给明确错误
                logging.warning("original session: 预热访问首页失败", exc_info=True)
            if detect_challenge(page.url, page.title()):
                raise OriginalStreamError("命中人机校验，请在「登录抖音」里人工处理后再试")
            names = {c.get("name", "") for c in ctx.cookies()}
            if not names.intersection(_LOGIN_COOKIES):
                raise OriginalStreamError(
                    "没有检测到登录状态。请先在程序里点「登录抖音」完成扫码；"
                    "未登录时 detail 常返回裁剪数据或只给低档流。")
        except Exception:
            # 失败时必须把浏览器关干净，否则下次 resolve 会拿到半初始化状态
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            pw.stop()
            raise
        self._page = page
        self._pw, self._ctx = pw, ctx

    def _load_cookies(self):
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict) and c.get("name")]
        except (OSError, ValueError):
            pass
        return []

    def ensure(self):
        """公开入口：返回 (browser_context, page)，CLI 的 detail 捕获也用它。"""
        with _lock:
            self._ensure()
            return self._ctx, self._page

    def resolve(self, video_id: str, best_size: int = 0,
                *, force: bool = False) -> OriginalStream | None:
        """返回探测结果（含 transcoded，供 CLI 打印 factor）；调用方自行决定是否采用。"""
        if not video_id:
            return None
        with _lock:
            hit = cached(video_id, force=force)
            if hit is not None:
                return hit
            self._sleeper(random.uniform(*THROTTLE))
            self._ensure()
            url = build_play_url(video_id)
            headers = {"Referer": REFERER, "Accept": "*/*",
                       "Accept-Language": "zh-CN,zh;q=0.9"}
            resp = self._ctx.request.get(url, headers=headers,
                                         max_redirects=0, timeout=30000)
            cdn = header_get(resp.headers, "location")
            via = "play"
            if not cdn:
                cdn = self._page.evaluate(_FETCH_FINAL_URL_JS, url)
                via = "fetch-fallback"
            if not cdn:
                raise OriginalStreamError("play 端点没有返回 Location")
            if not _allowed_cdn(cdn):
                raise OriginalStreamError("play 端点返回的 CDN 域名不在白名单内")
            head = self._ctx.request.head(cdn, headers={"Referer": REFERER},
                                          timeout=20000)
            try:
                size, content_type = cdn_info(head.headers)
            except OriginalStreamError:
                if not need_range_fallback(head.headers, int(head.status or 0)):
                    raise
                ranged = self._ctx.request.get(
                    cdn, headers={"Referer": REFERER, **RANGE_HEADERS},
                    max_redirects=0, timeout=20000)
                size, content_type = cdn_info(ranged.headers)
                logging.info("CDN HEAD 不可用，改用 Range GET 取体积：%s 字节", size)
            verdict, factor = classify(size, best_size)
            stream = OriginalStream(
                video_id=video_id, url=cdn, size=size, best_size=best_size,
                factor=factor, verdict=verdict, content_type=content_type, via=via)
            if stream.is_original:
                remember(stream)
            else:
                logging.info("original stream not available: vid=%s factor=%.2f -> 回退最高转码档",
                             video_id[:24], factor)
            return stream

    def close(self):
        with _lock:
            ctx, pw = self._ctx, self._pw
            self._ctx = self._pw = None
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def get_session() -> OriginalSession:
    global _session
    with _lock:
        if _session is None:
            _session = OriginalSession()
        return _session


def close_session() -> None:
    global _session
    with _lock:
        session, _session = _session, None
        _cache.clear()
    if session is not None:
        session.close()


def resolve_for_download(video_id: str, best_size: int = 0,
                         *, force: bool = False) -> OriginalStream | None:
    """download_work 注入的回调：未开启开关时直接返回 None。

    会话级失败（未登录、浏览器起不来）会冷却 2 分钟：批量同步有上千个作品，
    不能让每个作品都去重启一次 Chromium。冷却期间静默回退最高转码档。
    """
    global _FAILURE_UNTIL
    if not enabled():
        return None
    if time.monotonic() < _FAILURE_UNTIL:
        return None
    try:
        return get_session().resolve(video_id, best_size, force=force)
    except Exception as exc:  # noqa: BLE001 - 交给调用方记录并回退
        _FAILURE_UNTIL = time.monotonic() + FAILURE_COOLDOWN
        logging.warning("原画质回捞会话失败，冷却 %.0f 秒：%s",
                        FAILURE_COOLDOWN, exc)
        raise


atexit.register(close_session)


# ---------------------------------------------------------------- 落盘后校验
def _ftyp_brand(handle) -> str:
    for kind, start, end in media_probe._iter_boxes(handle, 0, min(64, os.fstat(handle.fileno()).st_size)):
        if kind == b"ftyp" and end - start >= 4:
            handle.seek(start)
            # 4CC 可能是 "qt  "（带补空格），规范表格里写作 qt
            return handle.read(4).decode("ascii", "replace").strip()
        break
    return ""


def _find_box(handle, start, end, target: bytes):
    for kind, payload, box_end in media_probe._iter_boxes(handle, start, end):
        if kind == target:
            return payload, box_end
    return None


def _scan_stsd(handle, moov_start, moov_end):
    """遍历**所有** trak 找视频 sample entry（音轨可能排在前面）。"""
    codecs, dims = [], (0, 0)
    for kind, payload, box_end in media_probe._iter_boxes(handle, moov_start, moov_end):
        if kind != b"trak":
            continue
        node = (payload, box_end)
        for box in (b"mdia", b"minf", b"stbl", b"stsd"):
            node = _find_box(handle, node[0], node[1], box)
            if node is None:
                break
        if node is None:
            continue
        stsd_start, stsd_end = node
        # stsd: version/flags(4) + entry_count(4) + N × sample entry
        handle.seek(stsd_start + 8)
        pos = stsd_start + 8
        while pos + 8 <= stsd_end:
            handle.seek(pos)
            head = handle.read(8)
            if len(head) < 8:
                break
            esize = int.from_bytes(head[:4], "big")
            etype = head[4:8]
            if etype in VIDEO_TAGS:
                codecs.append(etype.decode("ascii", "replace"))
                # 规范 8.1：sample entry 偏移 +32 / +34 是宽高
                handle.seek(pos + 8 + 32)
                raw = handle.read(4)
                if len(raw) == 4:
                    width = int.from_bytes(raw[:2], "big")
                    height = int.from_bytes(raw[2:], "big")
                    if width * height > dims[0] * dims[1]:
                        dims = (width, height)
            if esize <= 8:
                break
            pos += esize
    return codecs, dims


def _mvhd_duration(handle, moov_start, moov_end):
    node = _find_box(handle, moov_start, moov_end, b"mvhd")
    if node is None:
        return 0.0
    start, end = node
    if end - start < 20:
        return 0.0
    handle.seek(start)
    head = handle.read(4)
    version = head[0] if head else 0
    if version == 1 and end - start >= 32:
        handle.seek(start + 20)
        timescale = int.from_bytes(handle.read(4), "big")
        duration = int.from_bytes(handle.read(8), "big")
    else:
        handle.seek(start + 12)
        timescale = int.from_bytes(handle.read(4), "big")
        duration = int.from_bytes(handle.read(4), "big")
    return duration / timescale if timescale else 0.0


def _metadata_flags(blob: bytes) -> dict:
    """元数据扫描**只在 moov 范围内**，扫全文件会被 mdat 随机字节污染。"""
    flags = {
        "apple_quicktime": b"com.apple.quicktime" in blob,
        "apple_software": b"com.apple.quicktime.software" in blob,
        "lavf": bool(_LAVF.search(blob)),
        "ilst": b"ilst" in blob,
        "bytedance": b"bytedance" in blob or b"douyin" in blob,
    }
    hit = _CAPTURE_TIME.search(blob)
    flags["capture_time"] = hit.group(0).decode("ascii") if hit else ""
    return flags


def inspect_mp4(path: str) -> dict:
    """规范第 8 节：不装 ffprobe，用 MP4/QuickTime 结构判定文件特征。"""
    size = os.path.getsize(path)
    result = {
        "path": path, "bytes": size, "brand": "", "codecs": [],
        "width": 0, "height": 0, "duration_s": 0.0,
        "avg_bitrate_kbps": 0.0, "metadata": {}, "evidence": [],
    }
    with open(path, "rb") as handle:
        result["brand"] = _ftyp_brand(handle)
        moov = media_probe._find_moov(handle, size)
        if moov is not None:
            codecs, dims = _scan_stsd(handle, moov[0], moov[1])
            result["codecs"], result["width"], result["height"] = codecs, dims[0], dims[1]
            result["duration_s"] = round(_mvhd_duration(handle, moov[0], moov[1]), 3)
            handle.seek(moov[0])
            result["metadata"] = _metadata_flags(handle.read(
                min(moov[1] - moov[0], 8 * 1024 * 1024)))
    if result["duration_s"]:
        result["avg_bitrate_kbps"] = round(size * 8 / result["duration_s"] / 1000, 1)
    meta = result["metadata"]
    if result["brand"] == "qt":
        result["evidence"].append("ftyp 品牌 qt（拍摄原片常见）")
    if meta.get("apple_software"):
        result["evidence"].append("保留 Apple 设备拍摄元数据")
    if meta.get("capture_time"):
        result["evidence"].append("保留原始拍摄时间戳 " + meta["capture_time"])
    if result["avg_bitrate_kbps"] > 10000:
        result["evidence"].append("平均码率数万 kbps")
    if meta.get("bytedance"):
        result["evidence"].append("含平台注入标识")
    # Lavf 两个文件都有（平台对原片也重封装过），单独不能作为判据
    return result


def codec_changed(original: dict, transcoded: dict) -> bool:
    """规范 5.2：编码被更换（如 HEVC → H.264）即为「必然经过重编码」的硬证据。"""
    left = set(original.get("codecs") or [])
    right = set(transcoded.get("codecs") or [])
    return bool(left and right and not left.intersection(right))


def file_verdict(info: dict, transcoded: dict | None = None) -> str:
    """给一句**不夸大**的结论：码流层判定，不等于字节副本。"""
    score = 0
    if info.get("brand") == "qt":
        score += 1
    if (info.get("metadata") or {}).get("apple_software"):
        score += 2
    if (info.get("metadata") or {}).get("capture_time"):
        score += 1
    if (info.get("avg_bitrate_kbps") or 0) > 10000:
        score += 1
    if transcoded and codec_changed(info, transcoded):
        score += 2
    if score >= 4:
        return ("原始上传码流特征充分：视频码流疑似未经重编码；但容器与元数据已被平台"
                "用 FFmpeg 重封装，不等于上传时那个文件的字节副本。")
    return "未发现足够的原始码流证据，按转码成品对待。"
