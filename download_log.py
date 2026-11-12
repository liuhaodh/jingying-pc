# -*- coding: utf-8 -*-
"""「下载日志」专用记录。

两类行：
- 用 账号N 打开了 作者，找到了 X 个作品
- 下载了 作者 的《作品》，该作者还剩 N 个没下完 / 已全部下完

下载失败、重试、网络报错等一概不写（那些在 `app.log` 里）。

写盘走 `line_log` 的后台队列：同步时每个作品完成都会写一行，如果都在主线程
open/close 文件，作品多时会明显卡顿。这里只把行塞进队列，由后台线程攒批落盘，
主线程几乎零开销。
"""
import time

import config
import line_log


def _path():
    return config.DOWNLOAD_LOG_FILE


def _clean(value, fallback):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text or fallback


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_line(author_name, title, remaining, when=None):
    who = _clean(author_name, "未知作者")
    name = _clean(title, "未命名作品")
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        remaining = 0
    if remaining > 0:
        tail = f"该作者还剩 {remaining} 个没下完"
    else:
        tail = "该作者已全部下完"
    return f"[{when or _now()}] 下载完成 {who} 的《{name}》，{tail}"


def format_start_line(author_name, title, when=None):
    who = _clean(author_name, "未知作者")
    name = _clean(title, "未命名作品")
    return f"[{when or _now()}] 开始下载 {who} 的《{name}》"


def format_fail_line(author_name, title, when=None):
    who = _clean(author_name, "未知作者")
    name = _clean(title, "未命名作品")
    return f"[{when or _now()}] 下载失败 {who} 的《{name}》"


def format_open_line(account_label, author_name, found, when=None):
    account = _clean(account_label, "账号1")
    author = _clean(author_name, "未知作者")
    try:
        found = int(found)
    except (TypeError, ValueError):
        found = 0
    return f"[{when or _now()}] 用 {account} 打开了 {author}，找到了 {found} 个作品"


def append(author_name, title, remaining, when=None):
    """追加一条「下载了某作品」的记录，返回写入的文本。

    when 仅测试时用；默认在入队时取当前时间，保证时间戳是事件发生的时间。
    """
    return line_log.append(
        _path(), format_line(author_name, title, remaining, when=when))


def append_start(author_name, title, when=None):
    """追加一条「开始下载某作品」的记录。"""
    return line_log.append(
        _path(), format_start_line(author_name, title, when=when))


def append_fail(author_name, title, when=None):
    """追加一条「某作品下载失败」的记录。"""
    return line_log.append(
        _path(), format_fail_line(author_name, title, when=when))


def append_open(account_label, author_name, found, when=None):
    """追加一条「用账号N打开了某作者、找到了 X 个作品」的记录。"""
    return line_log.append(
        _path(), format_open_line(account_label, author_name, found, when=when))


def signature():
    """文件指纹（mtime + 大小），日志窗口用它判断要不要刷新。"""
    return line_log.signature(_path())


def pending():
    """还没落盘的条数（含写入线程正在处理的那批）。"""
    return line_log.pending()


def load():
    """读取日志；先把队列里还没落盘的内容写掉，再裁掉超限的旧记录。"""
    return line_log.load(_path(), config.MAX_DOWNLOAD_LOG_LINES)


def clear():
    """清空下载日志（只删记录文件，不影响 app.log）。"""
    line_log.clear(_path())
