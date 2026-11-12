# -*- coding: utf-8 -*-
"""后台任务线程：把函数丢进 QThread 执行，结果用信号回主线程。

独立成模块是为了让 webdav_panel / contacts_sync 这些 mixin
也能起线程，而不用反过来 import app（会循环导入）。

退出模型：每个 Worker 自带一个 threading.Event。任务函数只要在签名里声明
`cancel_event`（或 `stop_event`），就会收到这个 Event；关窗时
MainWindow.closeEvent 先对所有线程 `request_stop()`，让任务在安全点自行收尾
（跑完 finally / with / context manager），不再一上来就 `terminate()`。
没声明该参数的任务行为完全不变。
"""
import threading

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    stage = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, task, *args, **kwargs):
        super().__init__()
        self.task = task
        self.args = args
        self.kwargs = kwargs
        self._stop_event = threading.Event()

    @property
    def stop_event(self):
        return self._stop_event

    def request_stop(self):
        """协作式请求退出：置位 Event，同时打上 Qt 原生的中断标记。"""
        self._stop_event.set()
        self.requestInterruption()

    def stop_requested(self):
        return self._stop_event.is_set()

    def _inject_kwargs(self, kwargs):
        import inspect
        try:
            params = inspect.signature(self.task).parameters
        except (TypeError, ValueError):
            return kwargs
        for name in ("cancel_event", "stop_event"):
            if name in params and name not in kwargs:
                kwargs[name] = self._stop_event
        if "stage" in params and "stage" not in kwargs:
            kwargs["stage"] = self.stage.emit
        return kwargs

    def run(self):
        import asyncio
        try:
            kwargs = self._inject_kwargs(dict(self.kwargs))
            result = self.task(*self.args, **kwargs)
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
            self.done.emit(result)
        except Exception as e:  # noqa: BLE001
            # 已经请求停止：多半是任务在检查点主动抛的取消异常。此时窗口正在
            # 关闭，不要 emit failed（会弹「失败」对话框、往界面里塞状态）。
            if self._stop_event.is_set():
                return
            self.failed.emit(str(e) or "操作失败")
