# -*- coding: utf-8 -*-
"""通讯录同步时的增量 WebDAV 上传器。

下载线程把新文件放进队列，上传线程并行上传；不占用主窗口的解析线程，因此
「同步通讯录」和「上传 WebDAV」可以同时进行。
"""
import logging
import os
import queue
import threading
import time

from PySide6.QtCore import QObject, Signal


class AutoUploader(QObject):
    # 字节一律用 qint64：Qt 的 int 是 32 位，一次同步累计上传超过 2GB 后
    # emit 会抛 OverflowError（实测 9.4GB 时刷屏），上传进度从此不再更新。
    started = Signal(str, "qint64")              # rel, size
    file_progress = Signal(str, "qint64", "qint64")  # rel, done, size
    file_done = Signal(str, str, "qint64")       # rel, status, size
    file_failed = Signal(str, str)               # rel, 具体错误（给界面看）
    bytes_sent = Signal("qint64")                # 本次回调新增字节
    progress = Signal("qint64", "qint64")        # done bytes, total bytes
    finished = Signal(int, int, int)             # ok, skipped, failed

    def __init__(self, client, root, state=None, save_state=None,
                 max_workers=2, parent=None, rel_map=None):
        super().__init__(parent)
        self.client = client
        self.root = root
        self.rel_map = rel_map
        self.state = state
        self.save_state = save_state
        self.max_workers = max(1, min(8, int(max_workers or 1)))
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._seen = set()
        self._total = 0
        self._done = 0
        self._ok = 0
        self._skipped = 0
        self._failed = 0
        self._workers = []
        self._closed = False
        self._started = False
        self._exit_count = 0
        self._enqueueing = False

    # ---------- 生命周期 ----------
    def start(self):
        if self._started:
            return
        self._started = True
        for index in range(self.max_workers):
            thread = threading.Thread(target=self._loop, name=f"jy-up-{index}", daemon=True)
            self._workers.append(thread)
            thread.start()

    def enqueue(self, paths):
        if self._closed or self._enqueueing:
            return 0
        self._enqueueing = True
        try:
            added = 0
            with self._lock:
                for path in paths:
                    if not path or path in self._seen or not os.path.exists(path):
                        continue
                    self._seen.add(path)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    self._total += size
                    self._queue.put(path)
                    added += 1
            if added:
                # 注意：这里不要同步 emit progress。该函数是在 DownloadManager
                # item_done 的 Qt 信号栈里调用的，长时间同步时同步发信号会让
                # Qt/PySide 调用栈异常加深。总量由每个文件开始/完成时上报。
                pass
            return added
        finally:
            self._enqueueing = False

    def close(self):
        """不再接收新文件；已入队的文件继续传完。"""
        if self._closed:
            return
        self._closed = True
        if not self._started:
            self.finished.emit(0, 0, 0)
            return
        for _ in self._workers:
            self._queue.put(None)

    def state_snapshot(self):
        with self._lock:
            state = self.state if isinstance(self.state, dict) else {}
            files = state.get("files")
            if not isinstance(files, dict):
                files = {k: v for k, v in state.items()
                         if k not in ("server", "root", "files")}
            return {
                "server": state.get("server", ""),
                "root": state.get("root", ""),
                "files": dict(files),
            }

    def shutdown(self, timeout=2.0):
        """关闭并等待上传线程退出（程序关闭时用，避免线程操作已销毁的窗口）。"""
        self.close()
        deadline = time.time() + max(0.0, timeout)
        for thread in list(self._workers):
            thread.join(max(0.0, deadline - time.time()))

    def _loop(self):
        while True:
            path = self._queue.get()
            if path is None:
                break
            try:
                self._handle(path)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._exit_count += 1
            last = self._exit_count >= len(self._workers)
            ok, skipped, failed = self._ok, self._skipped, self._failed
        if last:
            self.finished.emit(ok, skipped, failed)

    def _state_files(self):
        if not isinstance(self.state, dict):
            return None
        files = self.state.get("files")
        if isinstance(files, dict):
            return files
        # 兼容直接传 {相对路径: 指纹} 的旧调用
        return self.state

    def _handle(self, path):
        rel = os.path.relpath(path, self.root).replace("\\", "/")
        if self.rel_map is not None:
            rel = self.rel_map(rel)
        try:
            size = os.path.getsize(path)
            fingerprint = f"{size}:{int(os.path.getmtime(path))}"
        except OSError:
            size = 0
            fingerprint = ""
        files = self._state_files()
        with self._lock:
            already = bool(files is not None and fingerprint
                           and files.get(rel) == fingerprint)
        if already:
            self._finish_file(rel, "skipped", size)
            return
        self.started.emit(rel, size)
        uploaded = [0]
        pending_bytes = [0]
        last_percent = [-1]
        last_emit = [0.0]

        def on_bytes(count):
            uploaded[0] += count
            pending_bytes[0] += count
            now = time.monotonic()
            percent = int(uploaded[0] * 100 / max(1, size))
            if percent == last_percent[0] and now - last_emit[0] < 0.2:
                return
            last_percent[0] = percent
            last_emit[0] = now
            try:
                if pending_bytes[0]:
                    self.bytes_sent.emit(int(pending_bytes[0]))
                    pending_bytes[0] = 0
                self.file_progress.emit(rel, uploaded[0], size)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.client.upload_file(path, rel, on_bytes=on_bytes)
            try:
                if pending_bytes[0]:
                    self.bytes_sent.emit(int(pending_bytes[0]))
                    pending_bytes[0] = 0
            except Exception:  # noqa: BLE001
                pass
            if files is not None and fingerprint:
                with self._lock:
                    files[rel] = fingerprint
                if self.save_state is not None:
                    self.save_state()
            self._finish_file(rel, "done", size)
        except Exception as exc:  # noqa: BLE001
            # 以前这里完全吞掉异常，界面只看到「失败 N 个」却查不到原因。
            detail = f"{type(exc).__name__}: {exc}"
            logging.warning("WebDAV upload failed %s: %s", rel, detail)
            try:
                self.file_failed.emit(rel, detail)
            except Exception:  # noqa: BLE001
                pass
            self._finish_file(rel, "failed", size)

    def _finish_file(self, rel, status, size):
        with self._lock:
            self._done = min(self._total, self._done + max(0, size))
            if status == "done":
                self._ok += 1
            elif status == "skipped":
                self._skipped += 1
            else:
                self._failed += 1
            done, total = self._done, self._total
        self.file_done.emit(rel, status, size)
        self.progress.emit(done, total)
