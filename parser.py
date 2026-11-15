# -*- coding: utf-8 -*-
"""抖音解析：复用 Android 版 resolver.js，用 Playwright 驱动 Chromium 抓取。

- 单条作品：打开 /video/{id}，读取页面真实请求的 aweme_detail / 播放地址。
- 作者主页：打开 /user/{sec_uid}，滚动捕获 aweme_list（视频 + 图集）。
- 登录：有头浏览器打开抖音官网，用户扫码后自动保存 Cookie，后续解析复用。
"""
import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Callable, Optional

import config
import links
import net_policy
import original_stream
from models import MediaInfo, find_detail, parse_aweme

RESOLVER_PATH = os.path.join(config.ROOT, "resolver.js")
CHROME_PATH = config.CHROME_PATH
SESSION_DIR = config.SESSION_DIR

# 同一个持久化配置目录（session_dir）同一时间只允许一个浏览器任务，否则会触发
# Chromium 的 ProcessSingleton 锁，表现为“读不到作品”或直接失败。但不同账号用的是
# 不同的 session_dir，各自一把锁即可——这样多账号同步可以真正并发：每个账号在
# 自己的配置目录里跑一个浏览器，互不干扰。登录检查 / 单条解析 / 主页同步只要不在
# 同一个 session_dir 上，就可以同时进行。
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()
_BROWSER_LOCK_TIMEOUT = 900

# 每次调用（登录 / 单条解析 / 主页同步 / 登录检查）登记的浏览器 context。
# 以前只有部分分支会显式 `_close_context`，任何一条异常路径（goto 超时、
# 页面跳转销毁执行上下文、字段解析失败）都会留下没关掉的持久化 Chromium：
# Cookie 不落盘、profile 锁残留，下次启动就命中 ProcessSingleton 并被悄悄
# 换成临时配置目录。这里改成「谁创建谁登记，调用结束统一关」：
# 无论正常返回还是抛异常，退出装饰器时都会把本次调用创建的 context 全部关闭。
_OPEN_CONTEXTS = contextvars.ContextVar("jy_open_contexts", default=None)


# 兜底解析（只拿到播放地址、没有画质阶梯）时的画质标签：UI 与下载日志都用它，
# 让用户能一眼看出这条不是按阶梯择优拿到的原画。
FALLBACK_QUALITY = "兜底地址（未取到画质阶梯）"


class ParseCancelled(Exception):
    """收到退出/停止请求，解析在安全点主动中断。"""


def _check_cancel(cancel_event):
    """协作式退出的统一检查点：置位后抛 ParseCancelled，让 finally 正常收尾。"""
    if cancel_event is not None and cancel_event.is_set():
        raise ParseCancelled("解析已取消")


def _register_context(context):
    registry = _OPEN_CONTEXTS.get()
    if registry is not None:
        registry.append(context)
    return context


async def _close_tracked_contexts():
    registry = _OPEN_CONTEXTS.get()
    if not registry:
        return
    contexts = list(registry)
    registry.clear()
    for context in contexts:
        await _close_context(context)


@contextlib.asynccontextmanager
async def _playwright():
    """替代 `async with async_playwright() as p`：退出前先关掉本次的 context。

    浏览器要在 Playwright 驱动还活着的时候关，才能把 Cookie 写回配置目录、
    释放 profile 锁。直接写 `async with async_playwright()` 的话，函数体一结束
    驱动就先停了，之后的 `context.close()` 只能算尽力而为。
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        try:
            yield p
        finally:
            try:
                await _close_tracked_contexts()
            except Exception:  # noqa: BLE001
                logging.warning("关闭浏览器 context 失败", exc_info=True)


def _lock_for(session_dir):
    """返回某个 session_dir 专属的锁；不同账号（不同目录）的锁互相独立。"""
    key = session_dir or SESSION_DIR
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SESSION_LOCKS[key] = lock
        return lock


def _bound_session_dir(func, args, kwargs):
    """从实际传参里取 session_dir（位置参数也不要漏）。

    以前只看 `kwargs.get("session_dir")`，而 `resolve_profile_sync` /
    `check_login_status_sync` 都是位置传参，结果账号 2~5 的任务全都去抢
    账号 1 的目录锁：既白白串行，又可能让登录检查被别的账号浏览器拖到超时。
    """
    if "session_dir" in kwargs:
        return kwargs["session_dir"] or SESSION_DIR
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return SESSION_DIR
    return bound.arguments.get("session_dir") or SESSION_DIR


def _serialized_browser(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # 按本次调用要用的配置目录取锁：同目录串行，不同目录可并发。
        lock = _lock_for(_bound_session_dir(func, args, kwargs))
        try:
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            cancel_event = (bound.arguments.get("cancel_event")
                            or bound.arguments.get("stop_event"))
        except (TypeError, ValueError):
            cancel_event = kwargs.get("cancel_event") or kwargs.get("stop_event")
        # 不能一头扎进 900 秒的阻塞 acquire：关窗时这个线程必须能在
        # stop_event 置位后立刻放弃等锁，否则只能被 terminate() 强杀。
        # 短超时间歇获取，仍在同一线程 acquire，RLock 的可重入语义不变。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _BROWSER_LOCK_TIMEOUT
        while True:
            _check_cancel(cancel_event)
            if lock.acquire(timeout=0.2):
                break
            if loop.time() >= deadline:
                raise ParseError("浏览器正被其他任务占用（登录/解析/同步），请稍后重试。")
            await asyncio.sleep(0.05)
        token = _OPEN_CONTEXTS.set([])
        try:
            return await func(*args, **kwargs)
        finally:
            try:
                await _close_tracked_contexts()
            except Exception:  # noqa: BLE001 - 收尾失败不能盖掉真正的异常
                logging.warning("关闭浏览器 context 失败", exc_info=True)
            finally:
                _OPEN_CONTEXTS.reset(token)
                lock.release()
    return wrapper


class NeedLogin(Exception):
    """主页作品列表需要登录。"""


class ParseError(Exception):
    pass


class TooOldWebView(Exception):
    pass


class ProfileInfo:
    def __init__(self):
        self.sec_uid = ""
        self.nickname = ""
        self.unique_id = ""
        self.avatar_url = ""
        self.signature = ""
        self.videos: list[MediaInfo] = []
        self.albums: list[MediaInfo] = []
        self.truncated = False
        self.total_works = 0


_RESOLVER_CACHE = ""


def _resolver_js() -> str:
    global _RESOLVER_CACHE
    if not _RESOLVER_CACHE:
        with open(RESOLVER_PATH, "r", encoding="utf-8") as f:
            _RESOLVER_CACHE = f.read()
    return _RESOLVER_CACHE


def _browser_args(headless: bool) -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if headless:
        args.append("--headless=new")
    else:
        args += ["--start-maximized", "--window-position=80,60"]
    return args


# ---------------------------------------------------------------- 网络策略接线
def _check_goto_response(response) -> None:
    """page.goto 拿到 403/429/5xx 时 Playwright 不抛异常，属于典型静默失败。"""
    status = getattr(response, "status", 0)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return
    if status in (403, 429) or status >= 500:
        raise net_policy.Blocked(f"页面返回 HTTP {status}")


async def _goto_with_retry(page, url: str, cancel_event=None,
                           tag: str = "goto") -> bool:
    """页面导航：先按 page 通道取令牌，再带指数退避重试。

    返回是否成功。失败时**不抛异常**，让调用方沿用原有的降级逻辑（例如单条
    解析在 goto 失败后仍会继续轮询 __JY_STATE__，页面可能自己跳转成功了）。
    但「用户取消」必须原样抛 ParseCancelled，不能被策略层吞掉。
    """
    async def attempt():
        if not await net_policy.policy().wait_async("page", cancel_event):
            raise ParseCancelled("解析已取消")
        response = await page.goto(url, wait_until="domcontentloaded")
        _check_goto_response(response)
        return response

    outcome = await net_policy.retry_async(
        attempt, key="page", tag=tag, cancel_event=cancel_event,
        throttle=False)  # attempt 内部已自行取令牌
    if not outcome.ok:
        if isinstance(outcome.error, ParseCancelled) or outcome.reason == "cancelled":
            raise ParseCancelled("解析已取消")
        logging.warning("页面打开失败 url=%s attempts=%d reason=%s: %s",
                        str(url)[:120], outcome.attempts, outcome.reason,
                        str(outcome.error or "")[:160])
    return outcome.ok


async def _poll_video_state(page, seconds: float, cancel_event=None):
    """在给定预算内轮询 __JY_STATE__，返回 (detail_text, play_url, last_state)。

    人机校验检测从「只有打开原画质开关才做」改成始终做：以前没开开关时命中
    校验只会一路轮询到超时，最后给用户一句笼统的「解析失败」。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, seconds)
    state = {}
    play_url = ""
    while loop.time() < deadline:
        _check_cancel(cancel_event)
        try:
            state = await page.evaluate(
                "() => { const s = window.__JY_STATE__ || {};"
                " return { detail: s.detail || '', play: s.play || '',"
                " meta: s.meta || {}, title: document.title,"
                " url: location.href }; }"
            )
        except Exception:
            # 页面正在跳转/刷新时执行上下文会被销毁，等待后重试。
            await asyncio.sleep(1.0)
            continue
        url = str(state.get("url", "") or "")
        title = str(state.get("title", "") or "")
        if (net_policy.is_challenge(url, title)
                or original_stream.detect_challenge(url, title)):
            net_policy.policy().record("page", ok=False, blocked=True, status=429)
            raise ParseError(
                "抖音要求人机校验。请先在「登录抖音」打开的有头浏览器里完成验证，再解析。")
        detail_text = state.get("detail", "")
        play_url = state.get("play", "") or play_url
        if detail_text:
            return detail_text, play_url, state
        await asyncio.sleep(0.7)
    return "", play_url, state


def _focus_browser_window():
    """把抖音登录窗口置前（Windows），避免用户找不到浏览器窗口。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            try:
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if "抖音" in title or "Douyin" in title or "Chromium" in title or "Google Chrome" in title:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.BringWindowToTop(hwnd)
                        user32.SetForegroundWindow(hwnd)
                        # 某些 Windows 前台锁会让 SetForegroundWindow 失效，再用
                        # TOPMOST 短暂提一下，确保登录页不会被主窗口挡在后面。
                        try:
                            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                                0x0001 | 0x0002 | 0x0040)  # NOSIZE|NOMOVE|SHOWWINDOW
                            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                                                0x0001 | 0x0002 | 0x0040)
                        except Exception:
                            pass
                        found.append(title)
                        return False
            except Exception:
                pass
            return True

        user32.EnumWindows(callback, 0)
    except Exception:
        pass


COOKIE_FILE = os.path.join(config.DATA_DIR, "cookies.json")


def save_cookies(cookies, cookie_file=None):
    path = cookie_file or COOKIE_FILE
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
    except OSError:
        pass


def load_saved_cookies(cookie_file=None):
    path = cookie_file or COOKIE_FILE
    try:
        # 以前是 json.load(open(...))：句柄要等 GC 才关，Windows 上会一直占着
        # cookies.json，登录流程重写同名文件时可能失败。
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict) and c.get("name")]
    except (OSError, ValueError):
        pass
    return []


async def _launch(playwright_instance, headless: bool,
                  session_dir: str = None, cookie_file: str = None,
                  fresh: bool = False):
    session_dir = session_dir or SESSION_DIR
    os.makedirs(session_dir, exist_ok=True)
    if fresh:
        # 主页分页使用全新的临时上下文 + Cookie：持久化 Chromium 配置里可能残留
        # 上一次访问的标签页/缓存状态，抖音会在第 50~100 个作品处提前返回
        # has_more=0；全新上下文能正常继续分页。
        browser = await playwright_instance.chromium.launch(
            headless=headless,
            executable_path=CHROME_PATH,
            args=_browser_args(headless),
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=config.DESKTOP_UA,
        )
        context._jy_browser = browser
    else:
        try:
            context = await playwright_instance.chromium.launch_persistent_context(
                session_dir,
                headless=headless,
                executable_path=CHROME_PATH,
                args=_browser_args(headless),
                viewport={"width": 1280, "height": 900},
                user_agent=config.DESKTOP_UA,
            )
        except Exception as e:  # noqa: BLE001
            message = str(e)
            if "ProcessSingleton" not in message and "profile is already in use" not in message:
                raise
            # 浏览器配置损坏：改用临时配置，并注入已保存的 Cookie
            temp_profile = tempfile.mkdtemp(prefix="jy_profile_", dir=config.TEMP_DIR)
            context = await playwright_instance.chromium.launch_persistent_context(
                temp_profile,
                headless=headless,
                executable_path=CHROME_PATH,
                args=_browser_args(headless),
                viewport={"width": 1280, "height": 900},
                user_agent=config.DESKTOP_UA,
            )
            # 临时配置目录必须跟着 context 一起删，否则每次兜底都会在磁盘上
            # 留下几十 MB 的 `jy_profile_*`（以前从来没有清理过）。
            context._jy_temp_profile = temp_profile
    saved = load_saved_cookies(cookie_file)
    if saved:
        try:
            await context.add_cookies(saved)
        except Exception:
            pass
    return _register_context(context)


async def _close_context(context):
    browser = getattr(context, "_jy_browser", None)
    try:
        await context.close()
    except Exception:
        pass
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    temp_profile = getattr(context, "_jy_temp_profile", None)
    if temp_profile:
        try:
            shutil.rmtree(temp_profile, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def cleanup_temp_profiles(max_age_days: float = 2.0, limit: int = 60) -> int:
    """清掉上次运行遗留的临时浏览器目录。

    三类都会被堆下来：`_launch` 的 ProcessSingleton 兜底用 `jy_profile_*`、
    Cookie 迁移用 `jy_cookie_*`、Playwright 自己给临时浏览器建
    `playwright_chromiumdev_profile-*`（每个几 MB，本机实测堆了 66 个、306 MB）。
    进程被强杀（或关窗时线程被终止）时它们都来不及删。

    只清理两天以前的、每次最多 `limit` 个：正在用的浏览器目录不会被误删，
    清理放在后台维护线程里跑，不拖慢启动。按修改时间从旧到新删，这样即使
    一次没删完，留下的也是较新的目录；`limit` 取 60 是为了让一次启动就能
    清掉积压（旧值 12 时，44 个残留要启动四次才清得完）。
    """
    root = config.TEMP_DIR
    prefixes = ("jy_profile_", "jy_cookie_",
                "playwright_chromiumdev_profile-", "playwright-artifacts-")
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    cutoff = time.time() - max_age_days * 86400
    stale = []
    for name in names:
        if not name.startswith(prefixes):
            continue
        path = os.path.join(root, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime <= cutoff:
            stale.append((mtime, path))
    if not stale:
        return 0
    stale.sort()  # 最旧的先删
    removed = 0
    freed = 0
    for _mtime, path in stale[: max(1, int(limit))]:
        try:
            freed += _tree_size(path)
        except OSError:
            pass
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                continue
        if os.path.exists(path):
            continue  # 删失败（被占用）就不算数
        removed += 1
    if removed:
        logging.info("cleaned %d stale browser temp dirs, freed %.1f MB",
                     removed, freed / (1024 * 1024))
    return removed


def _tree_size(path):
    """统计目录大小（只用于日志里的「释放了多少 MB」）。"""
    if not os.path.isdir(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


LOGIN_COOKIE_NAMES = {
    "sessionid", "sessionid_ss", "sid_guard", "sid_tt",
    "passport_auth", "uid_tt", "uid_tt_ss",
}


def _cookie_alive(cookie) -> bool:
    """Cookie 是否还在有效期内。

    抖音的登录 Cookie 会过期，但本地 `cookies.json` 里一直留着同名记录；
    以前只比对名字，服务端会话过期后界面仍然显示「已登录」，同步则整轮
    报「需要登录」，重启又变回「已登录」。这里按 `expires` 过滤一次
    （`expires <= 0` 表示会话 Cookie，无法判断，按有效处理）。
    """
    if not isinstance(cookie, dict):
        return False
    try:
        expires = float(cookie.get("expires", -1) or -1)
    except (TypeError, ValueError):
        return True
    if expires <= 0:
        return True
    return expires > time.time()


def _has_login_cookie(cookies) -> bool:
    """只有真正的登录态 Cookie 才算登录成功。

    注意：匿名访客也会有 passport_csrf_token 等 cookie，不能用 "passport" 前缀
    做兜底，否则会在用户还没扫码时误判为已登录并立刻关闭登录窗口。
    """
    names = {c.get("name", "") for c in cookies
             if _cookie_alive(c) and c.get("value")}
    hit = names & LOGIN_COOKIE_NAMES
    if hit:
        import logging
        logging.info("login cookies detected: %s", sorted(hit))
    return bool(hit)


@_serialized_browser
async def login_interactive(stop_event=None, timeout_s: int = 900,
                            stage: Optional[Callable[[str], None]] = None,
                            session_dir: str = None, cookie_file: str = None):
    """有头打开抖音官网，用户扫码登录。

    stop_event：用户在界面上点「我已完成登录」后置位，提前结束等待。
    返回时 Cookie 已保存在持久化浏览器目录中。
    """
    def say(text):
        if stage:
            stage(text)

    async with _playwright() as p:
        say("正在打开浏览器…")
        context = await _launch(p, headless=False, session_dir=session_dir,
                                cookie_file=cookie_file)
        context.set_default_timeout(120000)
        page = context.pages[0] if context.pages else await context.new_page()
        # 降低自动化特征，避免登录页直接拒绝
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        say("请在浏览器中扫码登录…")
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                            timeout=120000)
            # 登录窗口可能比主窗口晚几秒才出现，反复置前几次，避免用户以为没打开。
            for _ in range(6):
                await page.bring_to_front()
                _focus_browser_window()
                await asyncio.sleep(1.0)
        except Exception:
            # 登录页加载慢/跳转都不影响用户继续操作
            for _ in range(3):
                _focus_browser_window()
                await asyncio.sleep(0.7)

        deadline = asyncio.get_event_loop().time() + timeout_s
        detected = False
        notified = False
        # 只检测、不自动关闭：必须等用户点「我已完成登录」才关浏览器，
        # 避免用户还在扫码时窗口自己消失。
        while asyncio.get_event_loop().time() < deadline:
            try:
                cookies = await context.cookies()
                if _has_login_cookie(cookies):
                    detected = True
                    if not notified:
                        say("已检测到登录状态，请回到程序点「我已完成登录」")
                        notified = True
            except Exception:
                pass
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(1)

        # 用户点「完成」后再给页面 2 秒写入 Cookie
        if stop_event is not None and stop_event.is_set():
            await asyncio.sleep(2)
            try:
                cookies = await context.cookies()
                detected = detected or _has_login_cookie(cookies)
            except Exception:
                pass

        try:
            save_cookies(await context.cookies(), cookie_file)
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass

        if not detected:
            raise ParseError(
                "没有检测到登录状态。请确认已在打开的浏览器里完成扫码/手机号登录；"
                "如果已经登录，请重新点击「登录抖音」再试。")
        return True


def _fallback_info(aweme_id: str, play_url: str) -> MediaInfo:
    """只拿到播放地址时的兜底结果。

    兜底地址来自页面播放器实际请求，能下但通常不是最高档（多为 720p）。
    这里把降级写明并落 app.log，避免用户以为下到的是原画。
    """
    info = MediaInfo()
    info.aweme_id = aweme_id
    info.play_url = play_url
    info.title = ""
    info.quality = FALLBACK_QUALITY
    logging.warning(
        "detail missing, fallback to player url: aweme=%s url=%s",
        aweme_id, (play_url or "")[:120])
    return info


@_serialized_browser
async def resolve_video(raw_input: str, stage: Optional[Callable[[str], None]] = None,
                        timeout_ms: int = 60_000,
                        cancel_event: Optional[threading.Event] = None) -> MediaInfo:
    """解析单条作品。cancel_event 置位时在安全点抛 ParseCancelled。"""
    async def say(text):
        if stage:
            stage(text)

    _check_cancel(cancel_event)
    async with _playwright() as p:
        context = await _launch(p, headless=True)
        context.set_default_timeout(20000)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script(_resolver_js())

        await say("正在解析链接…")
        aweme_id = links.extract_aweme_id(raw_input)
        if aweme_id:
            if not await _goto_with_retry(page, links.video_page_url(aweme_id),
                                          cancel_event, tag="video-page"):
                # 抖音可能在加载中跳转/刷新，继续轮询页面状态即可。
                await page.wait_for_timeout(2000)
            _check_cancel(cancel_event)
        else:
            await say("正在打开短链…")
            await _goto_with_retry(page, links.extract_url(raw_input),
                                   cancel_event, tag="short-link")
            await page.wait_for_timeout(2500)
            _check_cancel(cancel_event)
            final = page.url
            aweme_id = links.extract_aweme_id(final)
            if not aweme_id:
                raise ParseError("没有找到有效的抖音链接")
            # 短链会跳到 H5 分享页（iesdouyin.com/share/video/...）。那个页面
            # 不请求 /aweme/detail/，探针抓不到元数据，只能退回播放地址兜底，
            # 而兜底地址通常只有 720p。拿到 id 后回到桌面作品页，才能走到带
            # 完整 bit_rate 清晰度阶梯的详情接口，拿到原画档。
            _check_cancel(cancel_event)
            await say("正在打开作品页…")
            if not await _goto_with_retry(page, links.video_page_url(aweme_id),
                                          cancel_event, tag="video-page-from-short"):
                # 页面跳转/刷新失败时保持当前页，后面还有兜底逻辑
                await page.wait_for_timeout(1500)
            _check_cancel(cancel_event)
        await say("正在等待视频数据…")

        # 总预算切成 80% + 20%：首轮没拿到数据时，按 page 通道退避一次、
        # 重载页面再试。总耗时不变，但能救回「首次加载被风控/网络抖动打断」。
        budget_s = timeout_ms / 1000
        detail_text = ""
        play_url = ""
        for round_index, fraction in enumerate((0.8, 0.2)):
            if round_index:
                if not await net_policy.policy().wait_async("page", cancel_event):
                    raise ParseCancelled("解析已取消")
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception:
                    await page.wait_for_timeout(1000)
                await say("首次未取到作品数据，正在重试…")
            detail_text, play_url, _state = await _poll_video_state(
                page, budget_s * fraction, cancel_event)
            if detail_text:
                break

        detail = None
        if detail_text:
            try:
                root = json.loads(detail_text)
                detail = find_detail(root)
            except (ValueError, KeyError) as exc:
                logging.warning("作品详情 JSON 解析失败 aweme=%s: %s", aweme_id, exc)
        # 「HTTP 200 但 aweme_detail 是空壳」以前会被 except: pass 静默吞掉，
        # 直接降级成 720p 兜底。现在明确断言并记一条带原因的结构化事件。
        reason = net_policy.check_payload(detail, "detail") if detail else ""
        if detail and not reason:
            info = parse_aweme(detail)
            if not info.ip_location:
                try:
                    body = await page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 8000) : ''")
                    m = re.search(r"IP\s*属地[：:]\s*([^\s\n]+)", body or "")
                    if m:
                        info.ip_location = m.group(1)
                except Exception:
                    pass
            await say("解析完成")
            # 原画质回捞（默认关闭）：就在同一个已登录浏览器上下文里，
            # 用页面自己算好签名的 detail 数据打 play 端点。任何软失效
            # 都只记日志，绝不阻塞原本已经成功的最高转码档解析结果。
            if original_stream.enabled() and detail.get("video"):
                await say("正在核对原始码流…")
                try:
                    stream = await original_stream.resolve_original(
                        context, page, detail)
                    info.original_verdict = stream.verdict
                    if stream.is_original:
                        info.ladder_quality = info.quality
                        info.original_url = stream.url
                        info.original_size = stream.size
                        info.quality = stream.label()
                except Exception as exc:  # noqa: BLE001 - 原片通道未开放是正常结果
                    info.original_verdict = "unavailable"
                    logging.warning(
                        "original stream unavailable, keep best gear: %s", exc)
            return info
        if detail_text:
            why = reason or "响应里没有 aweme_detail 节点，页面结构可能已变更"
            logging.warning("作品详情断言失败 aweme=%s reason=%s（将走播放地址兜底）",
                            aweme_id, why)
            net_policy.policy().record("page", ok=False,
                                       blocked=("风控" in why or "结构" in why))

        # 兜底：页面捕获到的播放地址
        if play_url:
            await say("解析完成（播放地址兜底，画质可能不是最高档）")
            return _fallback_info(aweme_id, play_url)
        raise ParseError("解析失败。抖音页面可能要求验证，请重新解析或先完成登录。")


@_serialized_browser
async def resolve_profile(sec_uid: str, stage: Optional[Callable[[str], None]] = None,
                          timeout_ms: int = config.PROFILE_TIMEOUT_MS,
                          session_dir: str = None, cookie_file: str = None,
                          cancel_event: Optional[threading.Event] = None) -> ProfileInfo:
    """抓取作者主页全部作品（视频 + 图集）。需要已登录的 Cookie；否则抛 NeedLogin。"""
    async def say(text):
        if stage:
            stage(text)

    _check_cancel(cancel_event)
    info = ProfileInfo()
    info.sec_uid = sec_uid
    seen: dict[str, MediaInfo] = {}
    last_text = ""

    async with _playwright() as p:
        context = await _launch(p, headless=True, session_dir=session_dir,
                                cookie_file=cookie_file,
                                fresh=bool(load_saved_cookies(cookie_file)))
        context.set_default_timeout(20000)
        page = context.pages[0] if context.pages else await context.new_page()
        page_posts = []
        consumed_posts = 0

        def _on_post(_source, text):
            if text:
                page_posts.append(str(text))

        # 抖音主页可能在一次滚动中连续返回多页；只轮询 lastProfileText 会丢掉
        # 中间页，导致前 50 个之后再也读不到。通过绑定把每一页都送到 Python。
        await page.expose_binding("jyOnPost", _on_post)
        await page.add_init_script(
            "window.JY = window.JY || {};"
            "window.JY.onPostPage = function(t){ try { window.jyOnPost(String(t)); } catch(e) {} };")
        await page.add_init_script(_resolver_js())
        # 主页作品列表必须登录；先看 Cookie，避免白等 3 分钟才报错。
        try:
            cookies = await context.cookies()
        except Exception:  # noqa: BLE001
            cookies = []
        if cookies and not _has_login_cookie(cookies):
            await _close_context(context)
            raise NeedLogin("主页作品列表需要登录，请先点「登录抖音」完成登录，再点「全部同步」。")
        await say("正在打开创作者主页…")
        if not await _goto_with_retry(page, links.profile_page_url(sec_uid),
                                      cancel_event, tag="profile-page"):
            # 导航失败时保留页面继续探测：登录/风控页也能从 innerText 判出来，
            # 后面的 NeedLogin / 登录检测分支会给出准确提示。
            await page.wait_for_timeout(1500)
        await page.wait_for_timeout(3000)
        _check_cancel(cancel_event)
        # 先拿主页顶部的“作品 N”或用户接口的 aweme_count，作为分页是否读完的依据
        try:
            head_text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 1200) : ''")
            match = re.search(r"作品\s*(\d+)", head_text or "")
            if match:
                declared_now = int(match.group(1))
                info.total_works = declared_now
        except Exception:
            pass
        try:
            user_text = await page.evaluate(
                "() => (window.__JY_STATE__ || {}).lastProfileUserText || ''")
            if user_text:
                user = (json.loads(user_text).get("user")) or {}
                count = user.get("aweme_count")
                if count not in (None, ""):
                    declared_now = int(count)
                    info.total_works = declared_now
        except (ValueError, TypeError):
            pass
        if "login" in (page.url or "").lower():
            await _close_context(context)
            raise NeedLogin("抖音登录已失效，请点「登录抖音」重新登录后同步。")

        # 登录检测（在拿到任何作品前）
        try:
            body = await page.evaluate("() => (document.body ? document.body.innerText.slice(0,2500) : '')")
        except Exception:
            body = ""
        if not seen and any(k in body for k in ("登录后免费", "扫码登录", "验证码登录", "请先登录")):
            await _close_context(context)
            raise NeedLogin("主页作品列表需要登录，请先在应用内登录抖音")

        empty_scrolls = 0
        no_work_scrolls = 0
        last_height = 0
        last_has_more = None
        last_page_len = 0
        declared_now = 0
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000

        async def process_post(post_text):
            nonlocal last_has_more, last_page_len, last_text
            if not post_text or post_text == last_text:
                return False
            last_text = post_text
            try:
                root = json.loads(post_text)
            except ValueError:
                return False
            # /aweme/post 也可能「HTTP 200 但没有 aweme_list」（风控兜底）。
            # 以前这里直接得到空列表，滚动循环只会以为是「还没加载出来」。
            post_reason = net_policy.check_payload(root, "post")
            if post_reason:
                logging.warning("主页作品响应断言失败 sec_uid=%s reason=%s",
                                sec_uid, post_reason)
                net_policy.policy().record("page", ok=False)
            aweme_list = root.get("aweme_list") or (root.get("data") or {}).get("aweme_list") or []
            last_page_len = len(aweme_list)
            for item in aweme_list:
                work = parse_aweme(item)
                if work.aweme_id and work.aweme_id not in seen:
                    seen[work.aweme_id] = work
                    if not info.nickname and work.author:
                        info.nickname = work.author
                    if not info.unique_id and work.unique_id:
                        info.unique_id = work.unique_id
                    if not info.avatar_url and work.avatar_url:
                        info.avatar_url = work.avatar_url
            await say(f"已发现 {len(seen)} 个作品，继续加载…")
            root_has_more = root.get("has_more")
            if root_has_more is not None:
                last_has_more = root_has_more
            return True

        while asyncio.get_event_loop().time() < deadline:
            _check_cancel(cancel_event)
            # 每滚一次都会触发一次 /aweme/post 请求；按 page 通道取令牌，
            # 被限流时 AutoThrottle 自动放慢，避免多账号并发把账号打进频控。
            if not await net_policy.policy().wait_async("page", cancel_event):
                raise ParseCancelled("解析已取消")
            try:
                await page.evaluate("() => (window.__JY_SCROLL__ && window.__JY_SCROLL__()) || 0")
            except Exception:
                await asyncio.sleep(1.0)
                continue
            await page.wait_for_timeout(1200)
            _check_cancel(cancel_event)
            try:
                state = await page.evaluate(
                    "() => { const s = window.__JY_STATE__ || {};"
                    " return { text: s.lastProfileText || '', height: document.documentElement.scrollHeight,"
                    " pages: s.profilePages || 0, user: s.lastProfileUserText || '' }; }"
                )
            except Exception:
                # 页面跳转/刷新时上下文销毁，跳过本轮继续。
                await asyncio.sleep(1.0)
                continue
            text = state.get("text", "")
            new_posts = page_posts[consumed_posts:]
            consumed_posts = len(page_posts)
            processed = False
            for post_text in new_posts:
                if await process_post(post_text):
                    processed = True
            if text and text != last_text:
                if await process_post(text):
                    processed = True
            if processed:
                empty_scrolls = 0
            logging.info("profile scroll seen=%d pages=%s empty=%d has_more=%s textlen=%d",
                         len(seen), state.get("pages"), empty_scrolls,
                         last_has_more, len(text or ""))
            user_text = state.get("user", "")
            if user_text:
                try:
                    user = (json.loads(user_text).get("user")) or {}
                    count = user.get("aweme_count")
                    if count not in (None, ""):
                        info.total_works = int(count)
                        declared_now = info.total_works
                except (ValueError, TypeError):
                    pass
            # 小作者可能只有 1~10 个作品；只要接口已结束且已达到主页总数，
            # 立即收尾，不需要额外等“50 个”或多次空滚动。
            if (str(last_has_more) == "0" and declared_now
                    and len(seen) >= declared_now):
                info.truncated = False
                break
            height = state.get("height", 0)
            if height > last_height + 40:
                last_height = height
                empty_scrolls = 0
            else:
                empty_scrolls += 1
            base_empty_limit = (config.MAX_EMPTY_SCROLLS_DONE
                                if str(last_has_more) == "0"
                                else config.MAX_EMPTY_SCROLLS)
            if empty_scrolls >= base_empty_limit and seen:
                # 服务端明确说还有下一页时，虚拟列表高度不变也不能立刻放弃
                if (str(last_has_more) == "1"
                        and empty_scrolls < config.MAX_EMPTY_SCROLLS_WITH_MORE):
                    pass
                elif (str(last_has_more) == "0"
                        and empty_scrolls < config.MAX_EMPTY_SCROLLS_DONE):
                    # has_more=0 可能来得过早，再给几轮滚动机会
                    pass
                elif (str(last_has_more) == "0"
                        and declared_now > len(seen)
                        and empty_scrolls < config.MAX_EMPTY_SCROLLS_SUSPICIOUS):
                    # 主页总数还没读齐：即使 has_more=0 也继续滚，避免只读前几十个
                    pass
                else:
                    info.truncated = str(last_has_more) != "0"
                    logging.info(
                        "profile scroll stop has_more=%s empty=%d declared=%d "
                        "seen=%d page_len=%d",
                        last_has_more, empty_scrolls, declared_now,
                        len(seen), last_page_len)
                    break
            if not seen:
                no_work_scrolls += 1
                # 登录/风控/网络异常时不要干等满 3 分钟
                if no_work_scrolls >= 8:
                    break
            if len(seen) >= config.MAX_WORKS:
                info.truncated = True
                break
        else:
            # 达到本轮抓取时限，保留已发现的作品并标记不完整
            info.truncated = True

        # 用户信息：优先来自 /user/profile/other 的捕获（state.lastProfileUserText）
        user_text = await page.evaluate(
            "() => (window.__JY_STATE__ || {}).lastProfileUserText || ''")
        page_text = ""
        try:
            page_text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''")
        except Exception:
            pass
        if user_text:
            try:
                user = (json.loads(user_text).get("user")) or {}
                if user.get("nickname"):
                    info.nickname = user["nickname"]
                if user.get("unique_id"):
                    info.unique_id = user["unique_id"]
                if user.get("signature"):
                    info.signature = user["signature"]
                count = user.get("aweme_count")
                if count not in (None, ""):
                    try:
                        info.total_works = int(count)
                    except (TypeError, ValueError):
                        pass
                avatar = user.get("avatar_thumb") or {}
                urls = avatar.get("url_list") or []
                if urls:
                    info.avatar_url = urls[0]
            except ValueError:
                pass

        await _close_context(context)

        logging.info("profile seen=%d declared=%d truncated=%s",
                     len(seen), info.total_works, info.truncated)
        for work in seen.values():
            if work.is_album():
                info.albums.append(work)
            else:
                info.videos.append(work)

        if not seen:
            # 有些正常账号确实没有公开作品（0 作品/私密账号），主页用户接口
            # 仍能拿到昵称和抖音号。这种情况要更新通讯录资料，不能每轮都报
            # “没有读到作品”，否则联系人会一直显示“待同步”。
            zero_markers = ("暂无内容", "还未发布过作品", "私密账号", "作品\n0")
            has_profile = bool(info.nickname or info.unique_id)
            if has_profile and (info.total_works == 0
                                or any(marker in page_text for marker in zero_markers)):
                info.truncated = False
                info.total_works = 0
                await say("主页暂无可下载作品，已更新作者资料")
                return info
            raise ParseError(
                "没有从主页读到作品。可能原因：未登录或登录已过期、主页需要安全验证、"
                "网络较慢。请点「登录抖音」确认登录后，再点「全部同步」重试。")
        await say(f"加载完成，共 {len(seen)} 个作品")
        return info


def resolve_video_sync(raw_input: str, stage=None, timeout_ms: int = 60_000,
                       cancel_event: Optional[threading.Event] = None) -> MediaInfo:
    return asyncio.run(resolve_video(raw_input, stage, timeout_ms,
                                     cancel_event=cancel_event))


def resolve_profile_sync(sec_uid: str, stage=None,
                         timeout_ms: int = config.PROFILE_TIMEOUT_MS,
                         session_dir: str = None, cookie_file: str = None,
                         cancel_event: Optional[threading.Event] = None) -> ProfileInfo:
    return asyncio.run(resolve_profile(sec_uid, stage, timeout_ms,
                                       session_dir, cookie_file,
                                       cancel_event=cancel_event))


def login_sync(stop_event=None, timeout_s: int = 900,
               session_dir: str = None, cookie_file: str = None) -> bool:
    return asyncio.run(login_interactive(stop_event, timeout_s,
                                         session_dir=session_dir,
                                         cookie_file=cookie_file))


@_serialized_browser
async def check_login_status(session_dir: str = None, cookie_file: str = None):
    """判断是否已登录：优先读本地 Cookie 文件，不打开登录窗口。"""
    saved = [c for c in load_saved_cookies(cookie_file) if _cookie_alive(c)]
    saved_names = sorted({c.get("name", "") for c in saved
                          if c.get("name") in LOGIN_COOKIE_NAMES and c.get("value")})
    if saved_names:
        return True, saved_names
    # 从未登录过的第二账号不必为一个空目录启动浏览器。
    if session_dir and not os.path.isdir(session_dir):
        return False, []
    async with _playwright() as p:
        context = await _launch(p, headless=True, session_dir=session_dir,
                                cookie_file=cookie_file)
        try:
            cookies = await context.cookies()
            if _has_login_cookie(cookies):
                save_cookies(cookies, cookie_file)
                names = sorted({c.get("name", "") for c in cookies
                                if c.get("name") in LOGIN_COOKIE_NAMES and c.get("value")})
                return True, names
        finally:
            try:
                await context.close()
            except Exception:
                pass
    return False, []


def migrate_cookies_from_profile() -> bool:
    """从旧持久化配置里提取 Cookie 存到 cookies.json（配置损坏时用）。"""
    from playwright.sync_api import sync_playwright
    session = config.SESSION_DIR
    if not os.path.isdir(session):
        return False
    cookie_src = os.path.join(session, "Default", "Network", "Cookies")
    if not os.path.exists(cookie_src):
        return False
    # 没登录过的用户每次启动都会走到这里：迁移失败时以前会一直重试，
    # 每次白白启动两只浏览器。迁移成功过就写标记，之后直接跳过。
    marker = os.path.join(config.DATA_DIR, "cookie_migrate_v1")
    if os.path.exists(marker):
        return False
    temp = tempfile.mkdtemp(prefix="jy_cookie_", dir=config.TEMP_DIR)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                temp, headless=True, executable_path=config.CHROME_PATH,
                args=_browser_args(True), viewport={"width": 1280, "height": 900},
                user_agent=config.DESKTOP_UA)
            ctx.close()
        for rel in (("Local State",), ("Default", "Network", "Cookies"),
                    ("Default", "Network", "Cookies-journal")):
            src = os.path.join(session, *rel)
            dst = os.path.join(temp, *rel)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                temp, headless=True, executable_path=config.CHROME_PATH,
                args=_browser_args(True), viewport={"width": 1280, "height": 900},
                user_agent=config.DESKTOP_UA)
            cookies = ctx.cookies()
            if _has_login_cookie(cookies):
                save_cookies(cookies)
            ctx.close()
        if load_saved_cookies():
            _write_marker(marker)
            return True
        return False
    except Exception:
        return False
    finally:
        # 临时配置目录以前从来没删过，每次迁移都留一份在 DATA_DIR/tmp 里。
        shutil.rmtree(temp, ignore_errors=True)


def _write_marker(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError:
        pass


def check_login_status_sync(session_dir: str = None, cookie_file: str = None):
    with _lock_for(session_dir):
        if not load_saved_cookies(cookie_file) and cookie_file in (None, COOKIE_FILE):
            migrate_cookies_from_profile()
        try:
            return asyncio.run(check_login_status(session_dir, cookie_file))
        except Exception as e:  # noqa: BLE001
            message = str(e)
            if "ProcessSingleton" in message or "profile is already in use" in message:
                if repair_session_profile(session_dir):
                    time.sleep(1.5)
                    return asyncio.run(check_login_status(session_dir, cookie_file))
            raise


def repair_session_profile(session_dir: str = None) -> bool:
    """浏览器配置损坏导致无法启动时：重建配置并保留登录 Cookie。"""
    from playwright.sync_api import sync_playwright
    session = session_dir or config.SESSION_DIR
    backup = session + ".bak"
    try:
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        if os.path.isdir(session):
            os.replace(session, backup)
        os.makedirs(session, exist_ok=True)
        # 先启动一次生成全新配置
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                session, headless=True, executable_path=config.CHROME_PATH,
                args=_browser_args(True), viewport={"width": 1280, "height": 900},
                user_agent=config.DESKTOP_UA)
            context.close()
        # 清理首次启动留下的锁文件
        for name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(session, name))
            except OSError:
                pass
        # 从旧配置导入登录 Cookie 与加密密钥
        for rel in (("Local State",),
                    ("Default", "Network", "Cookies"),
                    ("Default", "Network", "Cookies-journal")):
            src = os.path.join(backup, *rel)
            dst = os.path.join(session, *rel)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        import logging
        logging.warning("browser session profile rebuilt, backup=%s", backup)
        return True
    except Exception as e:  # noqa: BLE001
        import logging
        logging.warning("repair session profile failed: %s", e)
        return False