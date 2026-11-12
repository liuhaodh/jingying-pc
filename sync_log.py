# -*- coding: utf-8 -*-
"""「查看日志」专用记录。

三类行：
- [时间] 开始同步，共 N 位作者 / 同步结束：...
- [时间] 用 账号N 开始同步 作者
- [时间] 使用 账号N 同步了 作者 的 X 个作品

下载失败、网络重试、浏览器报错等一概不写（那些在 `app.log` 里）。

写盘走 `line_log` 的后台队列：同步时每抓到一位作者都要记一行，几百位作者
就是几百次 open/append/close，放主线程会在同步过程中持续拖慢界面。
"""
import time

import config
import line_log


def _path():
    return config.SYNC_LOG_FILE


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_line(account_label, author_name, count, when=None):
    return (f"[{when or _now()}] 使用 {account_label} 同步了 "
            f"{author_name} 的 {count} 个作品")


def format_start(account_label, author_name, when=None):
    return f"[{when or _now()}] 用 {account_label} 开始同步 {author_name}"


def format_event(text, when=None):
    return f"[{when or _now()}] {text}"


def append(account_label, author_name, count, when=None):
    """追加一条「完成同步某作者」的记录。"""
    return line_log.append(
        _path(), format_line(account_label, author_name, count, when=when))


def append_start(account_label, author_name, when=None):
    """追加一条「某个账号开始同步某作者」的记录。"""
    return line_log.append(
        _path(), format_start(account_label, author_name, when=when))


def append_event(text, when=None):
    """追加一条整体事件（开始全部同步 / 同步结束 / 已停止）。"""
    return line_log.append(_path(), format_event(text, when=when))


def signature():
    """文件指纹（mtime + 大小），日志窗口用它判断要不要刷新。"""
    return line_log.signature(_path())


def pending():
    """还没落盘的条数；日志窗口把它一起算进「有没有新内容」。"""
    return line_log.pending()


def load():
    """读取日志；超过上限时顺手裁掉旧记录，避免文件无限增长。"""
    return line_log.load(_path(), config.MAX_SYNC_LOG_LINES)


def clear():
    """清空日志（只是删记录文件，不影响 app.log）。"""
    line_log.clear(_path())
