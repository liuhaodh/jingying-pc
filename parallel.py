# -*- coding: utf-8 -*-
"""多线程下载管理器：多个作品并行下载。"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

import downloader
import net_policy


class DownloadManager(QObject):
    """并行下载若干作品，通过 Qt 信号把结果发回主线程。"""

    item_done = Signal(int, object)      # job index, DownloadResult
    item_error = Signal(int, str)        # job index, error message
    item_skipped = Signal(int, str)      # job index, 跳过原因（云端已是最优画质等）
    item_started = Signal(int)           # job index
    item_progress = Signal(int, int)     # job index, 当前文件百分比
    all_done = Signal(int, int)          # success, fail

    def __init__(self, max_workers: int = 3, pack_tar: bool = False,
                 precheck=None):
        super().__init__()
        self.max_workers = max(1, min(8, int(max_workers)))
        # 主界面「下载后打包成 tar」开关：每个任务提交时读当前值，
        # 运行中切换只影响之后开始的下载。
        self.pack_tar = bool(pack_tar)
        # 下载前的可选预检：`precheck(info, author_folder, work_folder)`
        # 返回非空字符串表示「不用下载」，字符串是给界面看的原因。
        # 挂在下载线程里调用，网络请求不会卡主线程。
        self.precheck = precheck
        # 只有这些作品需要预检（云端已有备份、可能要按分辨率重下的那些）。
        # 单次 membership 判断，多线程并发读一个 set 是安全的。
        self.upgrade_ids = set()
        self._executor = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._remaining = 0
        self._success = 0
        self._fail = 0
        self._byte_callback = None
        self._last_progress = {}
        self._last_progress_time = {}
        # 每一轮 begin() 换一个轮次号。上一轮还没跑完的任务（下载请求的读超时
        # 是 60 秒，停止同步后最多还能再跑一分钟）带着旧轮次号回来时，必须
        # 整个丢弃：否则它们的 item_done/item_error 会按同一个 index 落进新一轮
        # 的 _dl_jobs，把作品 A 的下载结果写成作品 B 的历史，并让 _dl_done
        # 虚增、同步提前收尾。
        self._generation = 0

    def begin(self, max_workers=None, byte_callback=None):
        """开始一个可持续追加任务的下载队列（通讯录同步用）。"""
        self.stop()
        if max_workers:
            self.max_workers = max(1, min(8, int(max_workers)))
        self._stop = threading.Event()
        self._byte_callback = byte_callback
        self._remaining = 0
        self._success = 0
        self._fail = 0
        self._last_progress = {}
        self._last_progress_time = {}
        # 预检名单只对当前这一轮有效，否则上一轮的作品会被白白探测一遍
        self.upgrade_ids.clear()
        with self._lock:
            self._generation += 1
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="jy-dl")

    def _stale(self, generation):
        """任务是否属于已经被 begin()/stop() 作废的上一轮。"""
        return generation != self._generation

    def enqueue(self, jobs, start_index=0):
        """向已 begin 的队列追加任务；索引用于和界面的任务行对应。"""
        if not jobs:
            return
        if self._executor is None:
            self.begin(self.max_workers, self._byte_callback)
        generation = self._generation
        with self._lock:
            self._remaining += len(jobs)
        for offset, job in enumerate(jobs):
            self._executor.submit(self._run_one, start_index + offset, job, generation)

    def start(self, jobs, max_workers=None, byte_callback=None):
        """jobs: list[(MediaInfo, author_folder, work_folder)]"""
        self.begin(max_workers, byte_callback)
        if not jobs:
            self.all_done.emit(0, 0)
            return
        self.enqueue(jobs, 0)

    def _emit_progress(self, index, pct, idx, total, generation=None):
        """下载器每个 256KB 块都会回调；只在百分比变化时发 Qt 信号，避免刷爆主线程。

        一个作品可能包含多个文件（视频 / 图片 / 动图 / 音乐），downloader 会把
        它们排成一条连续的进度轴（idx 依次递增，total 是文件总数），这里换算成
        「整个作品的进度」。同一作品只前进、不后退。
        """
        if generation is not None and self._stale(generation):
            return
        value = int((idx + max(0, min(100, pct)) / 100.0) * 100 / max(1, total))
        value = max(0, min(100, value))
        now = time.monotonic()
        with self._lock:
            last = self._last_progress.get(index, -1)
            if value <= last:
                return
            # 进度信号太密会拖慢主线程：8 个并发任务下 0.12s 会产生约 66 次/秒
            # Qt 信号，长同步实测会让主线程反复卡顿。放宽到每个作品约 4 次/秒
            # （100% 必发），视觉上仍然连续，主线程负担减半。
            if (value < 100
                    and now - self._last_progress_time.get(index, 0.0) < 0.25):
                return
            self._last_progress[index] = value
            self._last_progress_time[index] = now
        self.item_progress.emit(index, value)

    def _run_one(self, index, job, generation=None):
        info, author_folder, work_folder = job
        # 任务提交时那一刻的停止信号：新一轮 begin() 会换掉 self._stop，
        # 旧任务必须继续认自己那一轮（已被置位）的信号，才能马上停下来。
        stop_event = self._stop
        if generation is not None and self._stale(generation):
            return
        if stop_event.is_set():
            self._finish_one(False, index, None, "已取消", generation)
            return
        self.item_started.emit(index)
        # 「云端已有但可能是低画质」的作品：下载前先核对远端分辨率。
        # 达标就整单跳过，省掉一次几十 MB 的下载；低于解析结果才继续。
        if self.precheck is not None and info.aweme_id in self.upgrade_ids:
            try:
                reason = self.precheck(info, author_folder, work_folder)
            except Exception as exc:  # noqa: BLE001 - 预检失败按正常下载处理
                logging.warning("cloud quality precheck failed aweme=%s: %s",
                                info.aweme_id, exc)
                reason = ""
            if reason:
                self._finish_one(False, index, None, reason, generation,
                                 skipped=True)
                return
        # 任务级重试交给 net_policy：指数退避 + 抖动 + 可中断等待 + 结构化诊断，
        # 取代原来的「固定 sleep(1.0) 再来一次」。限流不在这里做，
        # downloader._download_to_file 已按「每个媒体请求」取令牌。
        aweme_id = getattr(info, "aweme_id", "")

        def do_download():
            return downloader.download_work(
                info, author_folder, work_folder,
                cancel_event=stop_event,
                byte_callback=self._byte_callback,
                pack_tar=self.pack_tar,
                progress=lambda pct, idx, total: self._emit_progress(
                    index, pct, idx, total, generation))

        def should_retry(exc):
            return (not isinstance(exc, downloader.DownloadCancelled)
                    and net_policy.policy().retry.should_retry(exc))

        outcome = net_policy.retry_sync(
            do_download, key="cdn", tag=f"aweme={aweme_id}",
            should_retry=should_retry, cancel_event=stop_event,
            throttle=False,
            on_retry=lambda attempt, exc, delay: logging.info(
                "下载重试 aweme=%s 第%d次 等待%.2fs: %s",
                aweme_id, attempt, delay, exc))
        if outcome.ok:
            self._finish_one(True, index, outcome.value, "", generation)
            return
        err = outcome.error
        if (isinstance(err, downloader.DownloadCancelled)
                or outcome.reason == "cancelled" or stop_event.is_set()):
            self._finish_one(False, index, None, "已取消", generation)
            return
        message = str(err) if err else (outcome.reason or "下载失败")
        logging.warning("下载失败 aweme=%s attempts=%d reason=%s: %s",
                        aweme_id, outcome.attempts, outcome.reason, message)
        self._finish_one(False, index, None, message or "下载失败", generation)

    def _finish_one(self, ok, index, result, error, generation=None,
                    skipped=False):
        if generation is not None and self._stale(generation):
            # 上一轮遗留任务：结果整体作废，不能改计数、也不能发信号。
            return
        with self._lock:
            self._remaining -= 1
            if ok:
                self._success += 1
            elif not skipped:
                self._fail += 1
            last = self._remaining <= 0
        if ok:
            self.item_done.emit(index, result)
        elif skipped:
            # 跳过不是失败：不计入失败数，也不写历史，界面单独显示原因。
            self.item_skipped.emit(index, error or "已跳过")
        else:
            self.item_error.emit(index, error)
        if last:
            self.all_done.emit(self._success, self._fail)

    def stop(self):
        self._stop.set()
        executor = self._executor
        self._executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python < 3.9
                executor.shutdown(wait=False)
