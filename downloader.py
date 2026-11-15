# -*- coding: utf-8 -*-
"""下载器：一个作品一个文件夹，按「净影/作者名_抖音号/作品id/」保存。

- 视频 / 图集图片 / 实况动图 / 背景音乐 / 文案.txt 全部放在同一个作品文件夹里；
- 主界面「下载后打包成 tar」开关打开时，下载完把作品文件夹打成
  `<作者目录>/<作品id>.tar` 并删除散文件；WebDAV 上传的也是这个 tar；
- 不再单独建 Music 目录；
- 旧版 `downloads/Videos/<作者>/<作品id>.tar` 与更早的散文件目录仍能识别、能播放。
"""
import base64
import binascii
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.parse
from typing import Callable, Optional

import requests

import archive
import config
import net_policy

ProgressCallback = Callable[[int, int, int], None]  # (item百分数, index, total)


class DownloadCancelled(Exception):
    """用户取消下载。"""


# 会话与重试/UA/代理策略统一放在 net_policy；这里保留 _get_session 这个名字，
# 因为离线测试会 patch 它注入假 Session（见 tests/test_opt_20261113.py）。

# 「原画质」回捞回调，由主窗口在开关打开时注入（downloader 不反向依赖
# original_stream / Playwright）。签名：
#   resolver(video_id, best_gear_size, force=False) -> OriginalStream | None
# 拿不到原片时返回 None，这里自动回退 info.play_url（最高转码档）。
_ORIGINAL_RESOLVER = None


def set_original_resolver(resolver):
    global _ORIGINAL_RESOLVER
    _ORIGINAL_RESOLVER = resolver


def _video_stream_url(info, force=False):
    """选本次真正要下载的视频直链，返回 (url, 是否原片)。

    批量同步是「先解析全部作者、再统一下载」，解析阶段的 CDN 直链到下载时
    早就过期了。所以这里按 video_id 即时回捞一次（解析阶段的结果有 120 秒
    短缓存，单条下载不会重复发请求）。
    """
    vid = getattr(info, "video_id", "")
    if _ORIGINAL_RESOLVER is not None and vid:
        try:
            stream = _ORIGINAL_RESOLVER(
                vid, getattr(info, "best_gear_size", 0), force=force)
        except Exception as exc:  # noqa: BLE001 - 回捞失败绝不能让作品下载失败
            logging.warning("原画质回捞失败，回退最高转码档：%s", exc)
            stream = None
        if stream is not None and getattr(stream, "is_original", False) and stream.url:
            info.ladder_quality = info.ladder_quality or info.quality
            info.original_url = stream.url
            info.original_size = getattr(stream, "size", 0)
            info.original_verdict = "original"
            info.quality = stream.label()
            return stream.url, True
        # 通道未开放 / 未登录 / 软失效：清掉旧直链，用 ladder 里的最高档
        info.original_url = ""
        info.original_verdict = "transcoded"
        if getattr(info, "ladder_quality", ""):
            info.quality = info.ladder_quality
    url = getattr(info, "original_url", "") or info.play_url
    return url, bool(getattr(info, "original_url", ""))


def _get_session() -> requests.Session:
    """每个线程复用一个连接池：同一作品的多张图片/视频不再重复 TLS 握手。

    构造细节（Retry / UA / 代理）已收敛到 net_policy.build_session，这里只负责
    按「cdn」通道取线程本地会话。行为与旧版一致，额外获得 408/425 重试、
    Retry-After 支持与可选代理。
    """
    return net_policy.session_for("cdn")


class DownloadResult:
    def __init__(self):
        # 2026-11-08 起每个作品只留一个 tar：archive_path 是最终产物，
        # video_path / album_paths 也指向这个 tar（历史记录字段不变，兼容旧数据）。
        self.archive_path = ""
        self.archive_members: list[str] = []
        self.video_path = ""
        self.music_path = ""
        self.album_paths: list[str] = []
        self.live_paths: list[str] = []
        self.caption_path = ""
        self.size = 0
        self.count = 0
        self.music_saved = False


# Windows 设备名：目录叫这些名字时 CreateDirectory 必失败（CON.txt 也不行）
_RESERVED_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                   | {f"COM{i}" for i in range(1, 10)}
                   | {f"LPT{i}" for i in range(1, 10)})


def sanitize_segment(value: str, limit: int = 60) -> str:
    if not value:
        return ""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", value)
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) > limit:
        safe = safe[:limit].strip()
    # Windows 上目录名不能以点或空格结尾，也不能是 CON/NUL 这类设备名。
    # 昵称正好是「CON」、或者结尾带点的作者以前会让 makedirs 直接失败，
    # 而“已下载”判断又恒为 False，于是每轮同步都把这位作者的作品重下一遍。
    safe = safe.rstrip(" .")
    if not safe:
        return ""
    if safe.split(".")[0].upper() in _RESERVED_NAMES:
        safe = "_" + safe
    return safe


def _safe_subdir(value: str) -> bool:
    """目录名是否安全（不能为空、不能是绝对路径、不能跳出上层目录）。"""
    if not value:
        return False
    if os.path.isabs(value) or os.path.splitdrive(value)[0]:
        return False
    parts = value.replace("\\", "/").split("/")
    return all(part not in ("..", ".", "") for part in parts)


def build_author_folder(nickname: str, unique_id: str) -> str:
    name = sanitize_segment(nickname, 24) or "作者"
    if unique_id:
        name = f"{name}_{sanitize_segment(unique_id, 24)}"
    return name


def archive_filename(info, work_folder: str) -> str:
    """旧版（2026-11-08）的作品打包名：`<作品id>.tar`（只为读老数据保留）。"""
    stem = sanitize_segment(str(getattr(info, "aweme_id", "") or ""), 40)
    return (stem or work_folder) + archive.ARCHIVE_EXT


def work_folder_name(info, fallback: str = "") -> str:
    """作品目录名：作品 id；拿不到 id 时退回「作者_文案_时间」文件夹名。"""
    stem = sanitize_segment(str(getattr(info, "aweme_id", "") or ""), 40)
    return stem or fallback


def work_dir(author_folder: str, work_folder: str, info=None) -> str:
    """作品目录：`<下载根>\\净影\\<作者名_抖音号>\\<作品id>\\`。

    2026-11-09 起的保存结构；`info` 为 None 时直接用 work_folder 作为目录名
    （测试和读旧数据时用）。
    """
    name = work_folder_name(info, work_folder) if info is not None else work_folder
    return os.path.join(config.SAVE_ROOT, author_folder, name)


def build_work_folder(info, author_name: str) -> str:
    t = int(getattr(info, "create_time", 0) or 0)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t)) if t > 0 else "00000000_000000"
    author = sanitize_segment(author_name, 24)
    # 文件夹名里的文案只取前 15 个字，避免路径过长
    title = sanitize_segment(info.display_title(), 15)
    return f"{author}_{title}_{stamp}"


# GSLB 前置节点会把抖音真实来源主机名写在路径里，或放在 query 参数里。
_TRUSTED_ORIGIN_MARKERS = (
    ".douyinvod.com/", ".douyincdn.com/", ".douyinstatic.com/",
)
_TRUSTED_ORIGIN_SUFFIXES = (
    ".douyinvod.com", ".douyincdn.com", ".douyinstatic.com",
)
# 实测轮换 CDN 会把真实来源放在这些参数里；值可能是明文，也可能是省略
# padding 的 base64（例如 real_host=djgyLW5jZG4tY29sZC5kb3V5aW52b2QuY29t
# 解码为 v82-ncdn-cold.douyinvod.com）。adi 解码后是一串 &sp=...&ym=<host>。
_ORIGIN_QUERY_KEYS = ("http_x-tos-host", "real_host", "adi")


def _maybe_b64(raw: str) -> str:
    """把 GSLB 参数值按 base64 还原成文本；不像可读文本就返回空串。"""
    text = (raw or "").strip()
    if len(text) < 8 or not re.match(r"^[A-Za-z0-9_=-]+$", text):
        return ""
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded, validate=False).decode("utf-8", "ignore")
    except (ValueError, UnicodeError, binascii.Error):
        return ""
    if not decoded or sum(c.isprintable() for c in decoded) < len(decoded) * 0.9:
        return ""
    return decoded.lower()


def _origin_hints(parsed) -> list[str]:
    """取出 URL 里所有「抖音真实来源主机」线索（明文 + base64 解码结果）。"""
    # 不能像旧代码那样把 query 整体 lower()：base64 值含大写字母，转小写会解码失败。
    query = urllib.parse.parse_qs(parsed.query)
    hints = []
    for key in _ORIGIN_QUERY_KEYS:
        raw = (query.get(key) or [""])[0]
        if not raw:
            continue
        hints.append(raw.lower())
        decoded = _maybe_b64(raw)
        if decoded:
            hints.append(decoded)
    return hints


def _is_video_like(parsed) -> bool:
    """抖音视频直链的路径特征：用来区分 CDN 节点和任意第三方站点。"""
    path = (parsed.path or "").lower()
    query = urllib.parse.parse_qs(parsed.query.lower())
    return "/video/tos/" in path or "u_reqid" in query


def _is_allowed_host(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return False
    for suffix in config.ALLOWED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    # 原片 play 直链还会再 302 到按区域/时间轮换的 GSLB 节点（实测出现过
    # pkoplink.com、jspcdn.cn、*.ydycdn.cn、*.sjxydc.com、*.hbttl.cn）。
    # 逐个补白名单永远追不上，改认 URL 自带的抖音来源标记：路径里带真实
    # 主机名，或 query 里的 http_x-tos-host / real_host / adi 指回抖音，
    # 且必须具备视频直链特征，避免任意站点靠伪造路径绕过白名单。
    if not _is_video_like(parsed):
        return False
    path = parsed.path.lower()
    if any(marker in path for marker in _TRUSTED_ORIGIN_MARKERS):
        return True
    return any(any(suffix in hint for suffix in _TRUSTED_ORIGIN_SUFFIXES)
               for hint in _origin_hints(parsed))


def _is_allowed_redirect(url: str) -> bool:
    """重定向链上的 CDN 节点：没进白名单，但仍是抖音视频直链形态时放行。

    只用于「初始 URL 已通过 _is_allowed_host」的 302 目标（有些 GSLB 节点不
    带任何来源参数，例如 down-updatekh18.hbttl.cn）。仍要求 https/http、
    非 IP、非本地地址、且路径是 /video/tos/，防止 302 被劫持到任意站点。
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return False
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    path = (parsed.path or "").lower()
    if "/video/tos/" not in path:
        return False
    # 抖音 CDN 的标准形态是 /video/tos/<region>/tos-cn-XXXX/<object>。
    # 只凭一个 /video/tos/ 前缀就放行，等于任何伪造路径的站点都能过。
    tail = path.split("/video/tos/", 1)[1]
    parts = [part for part in tail.split("/") if part]
    if len(parts) < 3 or not parts[1].startswith("tos-cn-"):
        return False
    # 再要求带一个抖音 GSLB 的分发参数，避免任意站点靠拼一段相似路径蒙混过关。
    query = urllib.parse.parse_qs(parsed.query.lower())
    return any(key in query for key in (
        "x_r_id", "ydyv", "cts", "nqck", "dy_q", "dy_va_biz_cert",
        "real_host", "adi", "http_x-tos-host", "u_reqid"))


def _guess_ext(url: str, kind: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if kind == "image":
        if ".webp" in path:
            return ".webp"
        if ".png" in path:
            return ".png"
        return ".jpg"
    if kind == "audio":
        if ".m4a" in path:
            return ".m4a"
        if ".aac" in path:
            return ".aac"
        if ".wav" in path:
            return ".wav"
        if ".flac" in path:
            return ".flac"
        return ".mp3"
    if ".mov" in path:
        return ".mov"
    if ".webm" in path:
        return ".webm"
    return ".mp4"


def _set_file_times(path: str, timestamp: int):
    """把文件创建时间、修改时间、访问时间都设为作品发布时间。"""
    if timestamp <= 0 or not os.path.exists(path):
        return
    try:
        os.utime(path, (timestamp, timestamp))
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        # 不声明 restype 时 ctypes 默认把返回值当 c_int（32 位）：64 位下真实
        # HANDLE 的高位会被截断，而且失败时返回的 -1 永远不等于
        # c_void_p(-1).value(0xFFFFFFFFFFFFFFFF)，失败判断形同虚设——接着会
        # 拿 -1 去 SetFileTime/CloseHandle，异常又被下面的 except 悄悄吞掉。
        create = kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                           wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        filetime_p = ctypes.POINTER(wintypes.FILETIME)
        set_time = kernel32.SetFileTime
        set_time.argtypes = [wintypes.HANDLE, filetime_p, filetime_p, filetime_p]
        set_time.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create(path, GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                        OPEN_EXISTING, 0, None)
        invalid = wintypes.HANDLE(-1).value
        if not handle or handle == invalid:
            logging.debug("CreateFileW 失败，跳过设置创建时间: %s", path)
            return
        try:
            # FILETIME: 100ns since 1601-01-01
            ft_value = int((timestamp + 11644473600) * 10_000_000)
            filetime = wintypes.FILETIME(ft_value & 0xFFFFFFFF, ft_value >> 32)
            if not set_time(handle, ctypes.byref(filetime),
                            ctypes.byref(filetime), ctypes.byref(filetime)):
                logging.debug("SetFileTime 失败: %s", path)
        finally:
            close_handle(handle)
    except Exception as exc:  # noqa: BLE001 - 时间设置失败不影响下载结果
        logging.debug("设置文件创建时间失败 %s: %s", path, exc)


def _download_to_file(url: str, target_path: str, progress: Optional[ProgressCallback],
                      index: int, total: int, timeout: int = 60,
                      timestamp: int = 0,
                      cancel_event: Optional[threading.Event] = None,
                      byte_callback=None, chunk_size: int = 256 * 1024,
                      resume: bool = True) -> int:
    """下载到 target_path（.part → rename），并设置文件时间为发布时间。

    支持断点续传：上次中断留下的 `.part` 会用 Range 继续；服务器不支持 Range 时自动重下。
    `resume=False` 用于「换了直链后的重试」：旧 `.part` 属于上一条 URL（可能是过期
    的原片 CDN 或更高/更低档位），带 Range 续传会把两个不同文件拼成一个坏 mp4，
    必须从头覆盖写。
    """
    if not _is_allowed_host(url):
        raise ValueError("不允许的下载域名: " + url)
    # 按 cdn 通道取令牌：被限流时 AutoThrottle 自动加延迟；等待过程随时响应取消。
    # 放在每个媒体文件的请求前（而不是整个作品前），并发下载才能被真正限住。
    if not net_policy.policy().wait("cdn", cancel_event):
        raise DownloadCancelled("下载已取消")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    part = target_path + ".part"
    headers = {"Accept-Encoding": "identity"}
    part_size = 0
    if not resume:
        # 删除失败也没关系：下面会以 "wb" 截断重写，不带 Range。
        try:
            os.remove(part)
        except OSError:
            pass
    elif os.path.exists(part):
        try:
            part_size = os.path.getsize(part)
        except OSError:
            part_size = 0
    if part_size > 0:
        headers["Range"] = f"bytes={part_size}-"
    size = 0
    last_percent = -1
    with _get_session().get(url, headers=headers, stream=True, timeout=(10, timeout)) as response:
        for hop in response.history:
            if not (_is_allowed_host(hop.url) or _is_allowed_redirect(hop.url)):
                raise ValueError("跳转到不允许的域名: " + hop.url)
        if response.status_code == 416 and part_size:
            try:
                os.remove(part)
            except OSError:
                pass
            raise IOError("续传位置无效，已重置缓存，请重试")
        if not (_is_allowed_host(response.url) or _is_allowed_redirect(response.url)):
            raise ValueError("跳转到不允许的域名: " + response.url)
        response.raise_for_status()
        body_len = int(response.headers.get("Content-Length", 0) or 0)
        if part_size and response.status_code == 206:
            mode = "ab"
            size = part_size
            total_len = part_size + body_len
        else:
            mode = "wb"
            size = 0
            total_len = body_len
        with open(part, mode) as out:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("下载已取消")
                if not chunk:
                    continue
                out.write(chunk)
                size += len(chunk)
                if byte_callback is not None:
                    try:
                        byte_callback(len(chunk))
                    except Exception:  # noqa: BLE001 - 统计出错不影响下载
                        pass
                if total_len > 0 and progress:
                    percent = int(size * 100 / total_len)
                    if percent != last_percent:
                        last_percent = percent
                        progress(min(100, percent), index, total)
    if size == 0:
        try:
            os.remove(part)
        except OSError:
            pass
        raise IOError("下载内容为空，直链可能已过期")
    os.replace(part, target_path)
    _set_file_times(target_path, timestamp)
    return size


def write_caption_file(directory: str, author: str, unique_id: str, title: str,
                       aweme_id: str, share_url: str, timestamp: int) -> str:
    """把完整文案单独保存为 文案.txt，避免文件夹名过长丢失内容。"""
    os.makedirs(directory, exist_ok=True)
    t = int(timestamp or 0)
    time_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "未知"
    link = share_url or ("https://www.douyin.com/video/" + aweme_id)
    text = (
        f"作者：{author}\n"
        f"抖音号：{unique_id or '未知'}\n"
        f"发布时间：{time_text}\n"
        f"作品链接：{link}\n\n"
        f"文案：\n{title or ''}\n"
    )
    path = os.path.join(directory, "文案.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _set_file_times(path, t)
    return path


def _write_caption(info, directory: str, timestamp: int) -> str:
    title = (getattr(info, "title", "") or "").strip()
    if not title:
        title = info.display_title()
    return write_caption_file(
        directory,
        info.safe_author(),
        getattr(info, "unique_id", ""),
        title,
        getattr(info, "aweme_id", ""),
        getattr(info, "share_url", ""),
        timestamp,
    )


def has_music_file(directory: str) -> bool:
    """作品目录里是否已经有背景音乐文件（`音乐.mp3` / `.m4a` / …）。"""
    try:
        with os.scandir(directory) as entries:
            return any(
                entry.is_file() and entry.name.startswith("音乐.")
                and not entry.name.endswith(".part")
                for entry in entries)
    except OSError:
        return False


def download_music_only(info, author_folder: str, work_folder: str,
                        cancel_event: Optional[threading.Event] = None) -> str:
    """给「已下载但没有音乐」的作品补下背景音乐，返回文件路径；无需补时返回 ""。

    只处理 2026-11-09 起的散文件目录 `<SAVE_ROOT>/<作者>/<作品id>/`：tar 包、
    旧结构目录、媒体已被「上传后清理」删掉的作品一律不动，避免破坏归档或违背
    用户的清理意图。`_download_to_file` 抛异常时不落盘，下一轮同步会自然重试。
    """
    aweme_id = str(getattr(info, "aweme_id", "") or "")
    url = str(getattr(info, "music_url", "") or "")
    if not url:
        logging.info("作品 %s 未下载背景音乐：本次数据里没有音乐播放地址", aweme_id)
        return ""
    directory = work_dir(author_folder, work_folder, info)
    if not os.path.isdir(directory):
        logging.info("作品 %s 音乐补齐跳过：作品目录不存在（tar 包 / 旧结构 / 已清理）",
                     aweme_id)
        return ""
    if has_music_file(directory):
        return ""
    ext = _guess_ext(url, "audio")
    target = os.path.join(directory, "音乐" + ext)
    # index/total 传 0/1：补齐是单文件操作，不参与整条作品进度轴。
    _download_to_file(
        url, target, None, 0, 1,
        timestamp=int(getattr(info, "create_time", 0) or 0),
        cancel_event=cancel_event)
    logging.info("背景音乐补齐完成 aweme=%s size=%d path=%s",
                 aweme_id, os.path.getsize(target), target)
    return target


def download_work(info, author_folder: str, work_folder: str,
                  progress: Optional[ProgressCallback] = None,
                  cancel_event: Optional[threading.Event] = None,
                  byte_callback=None, pack_tar: bool = False) -> DownloadResult:
    r"""下载一个作品（视频/图集 + 背景音乐），存进一个按作品 id 命名的文件夹。

    2026-11-09 起的保存结构：

        <下载根>\净影\<作者名_抖音号>\<作品id>\
                视频.mp4 · 图片_01.jpg · 动图_01.mp4 · 音乐.mp3 · 文案.txt

    默认保留作品文件夹；`pack_tar=True` 时（主界面开关打开）在下载线程里
    把文件夹打成 `<作者目录>/<作品id>.tar`，删除散文件，历史记录/播放器指向
    这个 tar。打包失败会保留文件夹并记日志，不影响已下载内容。
    """
    result = DownloadResult()
    timestamp = int(getattr(info, "create_time", 0) or 0)
    directory = work_dir(author_folder, work_folder, info)
    # 目录名来自昵称/文案，最后一道防线：不允许跳出 SAVE_ROOT。
    # 校验「实际用到的那两段名字」，而不是拼接后的 basename——否则
    # `x/../作品` 这种会被 normpath 抹成 `作品`，反而检查不出来。
    for part in (author_folder, work_folder_name(info, work_folder)):
        if not _safe_subdir(part):
            raise IOError(f"保存目录名不合法：{part!r}")
    os.makedirs(directory, exist_ok=True)
    video_dir = directory
    music_dir = directory

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("下载已取消")

    # 进度按「整个作品」统计：把这个作品要下载的所有文件（视频 / 图集图片 /
    # 实况动图 / 背景音乐）排成一条连续的进度轴，unit 依次递增、total 是全部
    # 文件数。这样进度条只会 0→100% 单调前进，不会每换一个文件就重走一遍。
    if info.is_album() and info.image_urls:
        n = len(info.image_urls)
        image_videos = getattr(info, "image_videos", None) or []
        live_flags = [bool(image_videos[i]) if i < len(image_videos) else False
                      for i in range(n)]
        total = (n + sum(1 for flag in live_flags if flag)
                 + (1 if info.music_url else 0))
        unit = 0
        for i, url in enumerate(info.image_urls):
            check_cancel()
            ext = _guess_ext(url, "image")
            target = os.path.join(video_dir, f"图片_{i + 1:02d}{ext}")
            result.size += _download_to_file(
                url, target, progress, unit, total,
                timestamp=timestamp, cancel_event=cancel_event,
                byte_callback=byte_callback)
            unit += 1
            result.album_paths.append(target)
            # 实况图：同时保存对应的动图视频（也占一格进度，避免跳回上一格）
            live_url = image_videos[i] if live_flags[i] else ""
            live_path = ""
            if live_url:
                live_ext = _guess_ext(live_url, "video")
                live_target = os.path.join(video_dir, f"动图_{i + 1:02d}{live_ext}")
                try:
                    result.size += _download_to_file(
                        live_url, live_target, progress, unit, total,
                        timestamp=timestamp, cancel_event=cancel_event,
                        byte_callback=byte_callback)
                    live_path = live_target
                except DownloadCancelled:
                    raise
                except Exception:
                    live_path = ""
                unit += 1
            result.live_paths.append(live_path)
        result.count = n
    elif info.has_video():
        total = 2 if info.music_url else 1
        video_url, is_original = _video_stream_url(info)
        ext = _guess_ext(video_url, "video")
        target = os.path.join(video_dir, "视频" + ext)
        # 规范 Step 7：原片通常几十 MB，用 1 MB 分块；普通档保持 256 KB。
        chunk = 1024 * 1024 if is_original else 256 * 1024
        for attempt in (0, 1):
            try:
                result.size = _download_to_file(
                    video_url, target, progress, 0, total,
                    timestamp=timestamp, cancel_event=cancel_event,
                    byte_callback=byte_callback, chunk_size=chunk,
                    resume=(attempt == 0))
                break
            except DownloadCancelled:
                raise
            except Exception:
                if attempt == 0 and is_original:
                    # CDN 直链有时效：清缓存重新回捞一次；仍失败就回退最高转码档。
                    # 重新回捞会换 URL（甚至从原片降到转码档），第一次留下的
                    # .part 属于旧 URL，重试必须从头写，不能按 Range 接着拼。
                    logging.warning("原画质直链下载失败，重新回捞一次：%s",
                                    info.aweme_id, exc_info=True)
                    video_url, is_original = _video_stream_url(info, force=True)
                    chunk = 1024 * 1024 if is_original else 256 * 1024
                    continue
                raise
        result.video_path = target
        result.count = 1
        unit = 1
    else:
        raise IOError("作品没有可下载的视频或图片")

    if info.music_url:
        check_cancel()
        ext = _guess_ext(info.music_url, "audio")
        target = os.path.join(music_dir, "音乐" + ext)
        result.size += _download_to_file(
            info.music_url, target, progress, unit, total,
            timestamp=timestamp, cancel_event=cancel_event,
            byte_callback=byte_callback)
        result.music_path = target
        result.music_saved = True
    else:
        # 以前这里完全静默：用户只能靠翻文件夹才发现少了音乐，也分不清是
        # 「作品本来就没配乐」还是「解析失效」。明确记一条带原因的日志。
        why = ("music 节点缺少播放地址（play_url / play_uri / uri 均为空）"
               if getattr(info, "music_title", "")
               else "详情数据里没有 music 节点（作品可能用原声 / 无配乐）")
        logging.info("作品 %s 未下载背景音乐：%s", info.aweme_id, why)

    # 完整文案单独存 txt（文件夹名只保留前 15 字），和作品放在同一个文件夹里
    result.caption_path = _write_caption(info, video_dir, timestamp)
    result.archive_path = ""
    result.archive_members = []
    if pack_tar:
        _pack_work_result(result, directory, info)
    return result


def _pack_work_result(result: DownloadResult, directory: str, info) -> None:
    """把作品文件夹打成 tar，并把结果路径改写成 tar（播放器按需解包）。

    只有 tar 写成功、且成员非空才删除散文件；打包失败保留原目录，
    用户至少还能拿到已下载的视频/图片。
    """
    parent = os.path.dirname(directory)
    tar_path = os.path.join(parent, archive_filename(info, os.path.basename(directory)))
    try:
        archive.pack(directory, tar_path)
        members = archive.member_names(tar_path)
        if not members:
            raise OSError("归档包为空")
    except (OSError, tarfile.TarError) as exc:
        logging.warning("pack work failed, keep folder %s: %s", directory, exc)
        try:
            os.remove(tar_path)
        except OSError:
            pass
        return
    result.archive_path = tar_path
    result.archive_members = members
    # 历史记录与上传队列只认 video_path / album_paths：这里统一指向 tar。
    # tar 不能直接播放，用系统播放器打开前需要先解包（archive.extract）。
    result.video_path = tar_path
    if result.album_paths:
        result.album_paths = [tar_path]
    result.live_paths = []
    result.music_path = ""
    result.caption_path = ""
    # tar 已完整落盘，散文件目录可以删了；删除失败也不影响播放。
    shutil.rmtree(directory, ignore_errors=True)