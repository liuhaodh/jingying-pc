# -*- coding: utf-8 -*-
"""按行追加的日志文件：后台线程攒批写盘 + 读取时裁剪超限的旧行。

`sync_log`（同步日志）和 `download_log`（下载日志）都要在同步/下载的高频
路径上写文件。如果直接在主线程 open/append/close：
- 几百位作者、上千个作品时会做几百次主线程磁盘写，界面出现轻微卡顿；
- `load()` 每次都要把整个文件读进来再按上限重写。

这里把写盘交给一个后台线程（同一路径只保留追加顺序，攒批落盘），主线程只剩
一次入队。`load()` 会先限时等队列落盘，保证窗口刷新读到的内容尽量完整，
又不会在写盘被外部占用时把 GUI 永久卡死。
"""
import logging
import os
import queue
import threading
import time

_LOCK = threading.Lock()
_QUEUE = queue.Queue()
_WRITER = None
_WRITER_LOCK = threading.Lock()

_BATCH_MAX = 50
_BATCH_WINDOW_S = 0.5
# 写盘线程卡住后，日志窗口一段时间内直接跳过刷新，避免 1.2 秒的定时器
# 每次都让主线程白等一整个 flush 超时（那和「界面未响应」没什么区别）。
_STALL_UNTIL = 0.0
_STALL_COOLDOWN_S = 5.0
# 正常情况最多等一个攒批窗口（0.5s）加写盘余量；太小会让 load() 读到空文件。
_LOAD_FLUSH_TIMEOUT_S = 1.5


def _write_lines(path, lines):
    if not lines:
        return
    try:
        with _LOCK:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except OSError as exc:
        # 以前完全吞掉：日志文件被杀毒/云同步锁住时，界面只会表现为
        # 「日志不更新」，排查时没有任何线索。
        logging.warning("写入日志失败 %s: %s", path, exc)


def _writer_loop():
    while True:
        batch = []
        try:
            batch.append(_QUEUE.get())
            # 攒一小批再写，减少磁盘打开/关闭次数
            deadline = time.monotonic() + _BATCH_WINDOW_S
            while len(batch) < _BATCH_MAX:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                try:
                    batch.append(_QUEUE.get(timeout=timeout))
                except queue.Empty:
                    break
            # 入队时已带好路径：测试里 config 切临时目录也不会写错文件
            groups = {}
            for path, line in batch:
                groups.setdefault(path, []).append(line)
            for path, lines in groups.items():
                _write_lines(path, lines)
        except Exception:  # noqa: BLE001 - 日志线程不能死
            pass
        finally:
            for _item in batch:
                try:
                    _QUEUE.task_done()
                except ValueError:
                    pass


def _ensure_writer():
    global _WRITER
    if _WRITER is not None:
        return
    with _WRITER_LOCK:
        if _WRITER is None:
            thread = threading.Thread(
                target=_writer_loop, name="jy-log-writer", daemon=True)
            thread.start()
            _WRITER = thread


def append(path, line):
    """把一行排进写盘队列（路径在入队时固定），返回该行文本。"""
    _ensure_writer()
    _QUEUE.put((path, line))
    return line


def flush(timeout=None):
    """等队列里已入队的行落盘；返回是否确实等到了全部落盘。

    `timeout=None` 保持原来的无限等待（退出流程要保证数据写完）。
    日志窗口每 1.2 秒在主线程调一次 `load()`，必须给一个上限：写盘线程
    若卡在被外部占用的文件上，无超时的 `Queue.join()` 会让整个界面
    永久冻结，看门狗线程也救不了。
    """
    _ensure_writer()
    if timeout is None:
        _QUEUE.join()
        return True
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _QUEUE.all_tasks_done:
        while _QUEUE.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _QUEUE.all_tasks_done.wait(remaining)
    return True


def signature(path):
    """文件指纹（mtime + 大小），日志窗口用它判断要不要刷新。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def pending():
    """还没落盘的条数。

    不能用 qsize()：写入线程把行取走后、落盘前，qsize 会短暂变 0，
    日志窗口就会误判“没新内容”。unfinished_tasks 要等 task_done 才归零。
    """
    return _QUEUE.unfinished_tasks


def load(path, limit):
    """读取日志；限时等落盘，再裁掉超过上限的旧记录。

    返回 None 表示这次刷新要跳过：写盘线程卡在被外部占用的文件上时，
    它会一直持有 `_LOCK`，无超时地等锁同样会冻住每 1.2 秒刷新一次的 GUI。
    调用方收到 None 应保留窗口里已有的内容，不要当成「日志被清空」。

    超时要大于 `_BATCH_WINDOW_S`（写盘线程本来就会攒批 0.5 秒），否则
    正常刷新也会读不到刚入队的行；一旦真的超时，就进入退避，恢复后自动清零。
    """
    global _STALL_UNTIL
    if time.monotonic() < _STALL_UNTIL:
        return None
    # 队列没落完说明写盘线程正卡着（它同时持有 _LOCK），直接跳过本次刷新，
    # 不要再排一次等锁：否则每 1.2 秒的定时器会把主线程累计卡住一秒。
    if not flush(timeout=_LOAD_FLUSH_TIMEOUT_S):
        _STALL_UNTIL = time.monotonic() + _STALL_COOLDOWN_S
        return None
    _STALL_UNTIL = 0.0
    limit = max(1, int(limit))
    if not _LOCK.acquire(timeout=0.1):
        return None
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, ValueError):
            return []
        if len(lines) > limit:
            lines = lines[-limit:]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                pass
        return lines
    finally:
        _LOCK.release()


def clear(path):
    """清空日志文件（只删这一个文件），返回是否真的执行了。"""
    if not flush(timeout=_LOAD_FLUSH_TIMEOUT_S):
        return False
    if not _LOCK.acquire(timeout=0.1):
        return False
    try:
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    finally:
        _LOCK.release()
