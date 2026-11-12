# -*- coding: utf-8 -*-
"""通讯录同步：批量抓取作者主页、排队下载、失败重试与状态汇总。

以 mixin 的形式挂在 MainWindow 上。依赖主窗口的下载管理器（self.dm）、
通讯录控件（contact_status / contact_sync_btn）和同步状态字段。
"""
import os
import random
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

import author_assets
import config
import download_log
import downloader
import parser
import store as store_mod
import sync_log
from workers import Worker


class ContactsSyncMixin:
    """通讯录同步状态机。

    宿主（MainWindow）需要提供：
    - `store` / `dm`（通讯录存储、并行下载管理器）
    - `syncing`、`_sync_queue` / `_sync_index` / `_sync_failed_objs` 等状态字段
    - 控件 `contact_status` / `contact_sync_btn` / `contact_list` / `sync_log`
    - `_run_worker` / `_track_worker`（起后台线程）、`_render_contacts`
    """

    def _maybe_finish_sync(self):
        if (not self.syncing or self._dl_mode != "sync"
                or not self._sync_profiles_done
                or self._dl_done < self._dl_total):
            return
        self._dl_mode = None
        try:
            self.dm.stop()
        except Exception:  # noqa: BLE001
            pass
        self._finish_sync()

    def _sync_note(self, text):
        """保留最近几条“已发现作者作品数”，同步过程中持续可见。"""
        notes = getattr(self, "_sync_notes", [])
        notes.append(str(text))
        self._sync_notes = notes[-2:]
        if hasattr(self, "sync_log"):
            self.sync_log.setText("  ·  ".join(self._sync_notes))
            self.sync_log.setVisible(bool(self._sync_notes))

    def _auto_sync(self):
        if not self.isVisible():
            return
        if self.store.load_authors() and not self.syncing:
            self._sync_all(auto=True)

    def _toggle_sync(self):
        if self.syncing:
            self._stop_sync()
        else:
            self._sync_all()

    # ---------- 失败作者重试 ----------
    def _failed_authors_file(self):
        return os.path.join(config.DATA_DIR, "sync_failed.json")

    def _load_failed_authors(self):
        import json
        try:
            with open(self._failed_authors_file(), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return
            objs = []
            for item in data:
                if not isinstance(item, dict) or not item.get("sec_uid"):
                    continue
                objs.append(store_mod.Author(
                    str(item["sec_uid"]), str(item.get("nickname") or ""),
                    str(item.get("unique_id") or ""), ""))
            self._sync_failed_objs = objs
        except (OSError, ValueError):
            pass

    def _save_failed_authors(self):
        import json
        try:
            path = self._failed_authors_file()
            if not self._sync_failed_objs:
                if os.path.exists(path):
                    os.remove(path)
                return
            payload = [{
                "sec_uid": a.sec_uid,
                "nickname": a.nickname,
                "unique_id": a.unique_id,
            } for a in self._sync_failed_objs]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _update_retry_button(self):
        if not hasattr(self, "retry_failed_btn"):
            return
        count = len(getattr(self, "_sync_failed_objs", []))
        self.retry_failed_btn.setText(f"重试失败({count})")
        self.retry_failed_btn.setVisible(count > 0 and not self.syncing)

    def _clear_sync_failure(self, author):
        before = len(self._sync_failed_objs)
        self._sync_failed_objs = [
            a for a in self._sync_failed_objs if a.sec_uid != author.sec_uid]
        if len(self._sync_failed_objs) != before:
            self._save_failed_authors()
            self._update_retry_button()

    def _record_sync_failure(self, author, short):
        self._sync_failed_authors.append((author.label(), short))
        if all(a.sec_uid != author.sec_uid for a in self._sync_failed_objs):
            self._sync_failed_objs.append(author)
        self._save_failed_authors()
        self._update_retry_button()

    def _retry_failed_authors(self):
        if self.syncing:
            return
        if self._check_worker and self._check_worker.isRunning():
            self.status.setText("正在检查登录状态，请稍后再重试失败作者。")
            return
        failed = list(getattr(self, "_sync_failed_objs", []))
        if not failed:
            self._update_retry_button()
            return
        self._sync_all(authors=failed, retrying=True)

    def _stop_sync(self):
        if not self.syncing and self._dl_mode != "sync":
            return
        self.syncing = False
        self._sync_profiles_done = False
        self._dl_mode = None
        # 停止即作废本轮：还在跑的 worker 迟到返回时不能再走
        # `_sync_author_works` —— 那条路会把作品塞回已经 stop 的下载队列
        # （队列的 executor 已经是 None，enqueue 会顺手 begin 出一批新下载）。
        self._sync_generation = getattr(self, "_sync_generation", 0) + 1
        self.contact_sync_btn.setText("全部同步")
        try:
            self.dm.stop()
        except Exception:  # noqa: BLE001
            pass
        self.contact_status.setText("已停止同步（已下载的作品会保留）")
        self.status.setText("同步已停止")
        self.download_panel.set_idle("同步已停止")
        sync_log.append_event("同步已停止（已下载的作品会保留）")
        try:
            self.store.flush()
        except Exception:  # noqa: BLE001
            pass
        if self._auto_uploader is not None:
            self._auto_uploader.close()
        if self._contacts_dirty:
            self._contacts_dirty = False
            self._render_contacts()
        self._update_retry_button()

    def _shuffle_sync_authors(self, authors):
        """每次同步随机打乱作者顺序，并尽量避开上次的第一位作者。"""
        result = list(authors)
        if len(result) > 1:
            rng = random.SystemRandom()
            rng.shuffle(result)
            last_first = getattr(self, "_last_sync_first", "")
            if last_first and result[0].sec_uid == last_first:
                swap_index = rng.randrange(1, len(result))
                result[0], result[swap_index] = result[swap_index], result[0]
            self._last_sync_first = result[0].sec_uid
        elif result:
            self._last_sync_first = result[0].sec_uid
        return result

    # ---------- 同步主流程 ----------
    def _sync_all(self, auto=False, authors=None, retrying=False):
        if self._login_running:
            self.status.setText("请先在浏览器完成登录，再点「全部同步」")
            return
        if not (config.CHROME_PATH or parser.CHROME_PATH):
            # 没装浏览器时以前会给每位作者各起一次注定失败的浏览器，
            # 全部记成「读取失败」，提示和真实原因完全对不上。
            self.status.setText(
                "没有找到可用的 Chrome / Edge：请安装浏览器，"
                "或设置环境变量 JINGYING_CHROME 指向 chrome.exe。")
            return
        if self._check_worker and self._check_worker.isRunning():
            if authors is not None:
                self.status.setText("正在检查登录状态，请稍后再重试失败作者。")
                return
            self._sync_after_check = True
            self.status.setText("正在检查登录状态，完成后自动开始同步…")
            return
        if self.syncing:
            self.contact_status.setText("正在同步中；如需停止请点「停止同步」")
            return
        if self.worker and self.worker.isRunning():
            self.status.setText("已有解析任务在进行，请稍后再试。")
            return
        if self._dl_mode is not None:
            self.status.setText("已有下载任务在进行，请稍后再试。")
            return
        if getattr(self, "_single_dl", False):
            self.status.setText("正在保存当前作品，请稍后再同步。")
            return
        if authors is None:
            authors = self.store.load_authors()
        else:
            authors = list(authors)
        if not authors:
            self.status.setText("通讯录还没有作者；解析作者主页后点「加入通讯录」")
            return
        self.syncing = True
        self._sync_auto = auto
        # 同步轮次号：停止后立刻重新同步时，上一轮还在跑的 worker（单次主页
        # 抓取最长 10 分钟）会迟到 emit finished/done。带上轮次号的回调只认
        # 自己那一轮，否则会 pop 掉新一轮同一槽位正在跑的作者，让
        # `_sync_profiles_done` 提前为真、整轮同步静默丢作者。
        self._sync_generation = getattr(self, "_sync_generation", 0) + 1
        self._sync_retry = set()
        self._sync_failed_authors = []
        if not retrying:
            self._sync_failed_objs = []
            self._save_failed_authors()
        self._sync_current = None
        self._update_retry_button()
        self.contact_sync_btn.setText("停止同步")
        self._sync_retried = set()
        self._sync_queue = self._shuffle_sync_authors(authors)
        sync_log.append_event(
            f"开始重试 {len(self._sync_queue)} 位失败作者" if retrying
            else f"开始同步，共 {len(self._sync_queue)} 位作者")
        self._sync_index = 0
        self._sync_total_new = 0
        self._sync_total_fail = 0
        self._sync_profiles_done = False
        self._sync_notes = []
        self._contacts_dirty = False
        self._sync_account_index = 0
        self._sync_account_index_current = 0
        self._sync_account_label = ""
        self._sync_author_account = {}
        # 每位作者这一轮还要下载多少作品（供「下载日志」算「还剩多少没下完」）
        self._sync_author_remaining = {}
        # 历史/通讯录按时间限频落盘，避免大历史反复全量重写卡住主线程
        self._last_store_flush = time.monotonic()
        # 多账号并发：每个可用账号占一个槽位，同一账号同一时间只跑一个浏览器
        # （各自 session_dir 的锁会兜底），不同账号的浏览器则真正并行。
        # 没有检测到任何已登录账号时仍尝试账号1（与旧行为一致），由「登录失效」
        # 分支负责收尾，避免同步直接空转。
        active = [i for i, value in enumerate(self._account_logged)
                  if value is True]
        if not active:
            active = [0]
        self._sync_slots = list(active)
        self._sync_multi = len(self._sync_slots) > 1
        self._sync_active = {}
        self._sync_workers = {}
        if hasattr(self, "sync_log"):
            self.sync_log.clear()
            self.sync_log.setVisible(False)
        # 同步使用一个可追加任务的下载队列：抓完一位作者的主页后，
        # 立刻把作品加入下载队列并继续抓下一位，下载不再阻塞同步。
        self._dl_mode = "sync"
        self._dl_jobs = []
        self._dl_authors = []
        # 新一轮的传输列表 key（避免和上一轮残留的任务行撞在一起）
        self._dl_run = getattr(self, "_dl_run", 0) + 1
        self._dl_done = 0
        self._dl_total = 0
        self._dl_success = 0
        self._dl_fail = 0
        self._dl_author = None
        self._dl_progress.clear()
        self._dl_meter.reset()
        self.download_panel.set_stats(0, 0, "等待抓取作者资料…")
        self.dm.begin(self._concurrency(), byte_callback=self._dl_meter.add)
        self._start_auto_upload()
        self._sync_dispatch()

    def _sync_prefix(self, fallback=None):
        """当前正在下载的作者；多账号并发时状态里不再强调某一个账号。"""
        if fallback is None:
            return "同步中"
        try:
            label = fallback.label()
        except AttributeError:
            label = str(fallback)
        return f"正在同步 {label} 的作品"

    def _sync_dispatch(self):
        """把队列里的作者填满空闲的账号槽位；槽位 i 固定绑定账号 i。

        一个槽位同一时间只跑一位作者，但不同槽位（不同账号）并行——这正是
        「多个账号同时打开不同作者」带来的提速。槽位释放由 worker 的 finished
        信号触发（_on_sync_worker_finished），这里只负责补位和收尾判定。
        """
        if not self.syncing:
            return
        for account_index in list(self._sync_slots):
            if account_index in self._sync_active:
                continue
            if self._sync_index >= len(self._sync_queue):
                break
            author = self._sync_queue[self._sync_index]
            self._sync_index += 1
            self._sync_active[account_index] = author
            self._run_sync_worker(author, account_index)
        # 账号全部失效、却还有作者没同步：这些作者无法继续，统一记为失败，
        # 否则队列永远非空会把同步卡住。
        if self._sync_index < len(self._sync_queue) and not self._sync_slots:
            for author in self._sync_queue[self._sync_index:]:
                self._record_sync_failure(author, "没有可用登录账号")
            self._sync_index = len(self._sync_queue)
        # 队列空且所有槽位空闲 → 作者资料抓取阶段结束，等剩余下载收尾
        if self._sync_index >= len(self._sync_queue) and not self._sync_active:
            self._sync_profiles_done = True
            self.contact_status.setText(
                f"作者资料已抓取完，正在等待剩余下载"
                f"（{self._dl_done}/{self._dl_total}）")
            self._maybe_finish_sync()

    def _sync_stale(self, generation):
        """回调是否来自已经结束的那一轮同步。"""
        return generation is not None and generation != getattr(
            self, "_sync_generation", generation)

    def _on_sync_worker_finished(self, account_index):
        """某个账号槽位的 worker 结束：释放槽位并立刻补下一个作者。"""
        self._sync_active.pop(account_index, None)
        self._sync_workers.pop(account_index, None)
        self._sync_dispatch()

    def _run_sync_worker(self, author, account_index):
        """在账号 account_index 自己的浏览器配置里抓取 author 的主页。

        不使用 self.worker（那是单条解析/检查的专用槽位），而由 self._sync_workers
        单独持有，这样多个账号的 worker 可以真正并发运行。
        """
        if not self.syncing:
            return
        generation = getattr(self, "_sync_generation", 0)
        label = f"账号{account_index + 1}"
        # 记下这位作者用的是哪个账号，收尾时写进「查看日志」
        self._sync_author_account[author.sec_uid] = label
        sync_log.append_start(label, author.label())
        account_kwargs = {}
        if account_index != 0 or self._sync_multi:
            account_kwargs = {
                "session_dir": config.SESSION_DIRS[account_index],
                "cookie_file": config.COOKIE_FILES[account_index],
            }
        self.contact_status.setText(
            f"正在同步 {min(self._sync_index, len(self._sync_queue))}/"
            f"{len(self._sync_queue)}（{label}）：{author.label()}")
        worker = Worker(parser.resolve_profile, author.sec_uid, **account_kwargs)
        worker.stage.connect(
            lambda text: self.status.setText(text),
            Qt.ConnectionType.QueuedConnection)
        # 带上账号号：以前这里漏了 account_index，解析成功后的异常分支会按默认值 0
        # 去改「账号1」的登录状态，把真正失效的账号 N 留在可用槽位里。
        worker.done.connect(
            lambda result: (None if self._sync_stale(generation)
                            else self._sync_profile_done(author, result, account_index)),
            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(
            lambda msg: (None if self._sync_stale(generation)
                         else self._sync_failed(author, msg, account_index)),
            Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(
            lambda: (None if self._sync_stale(generation)
                     else self._on_sync_worker_finished(account_index)),
            Qt.ConnectionType.QueuedConnection)
        # 复用全局 _workers 持有线程引用，避免运行中被析构导致崩溃
        self._sync_workers[account_index] = self._track_worker(worker)
        worker.start()

    def _queue_avatar_download(self, author, url):
        """头像放后台线程下载：同步收尾在主线程，网络请求会把界面卡住。"""
        if not url:
            return
        inflight = getattr(self, "_avatar_inflight", None)
        if inflight is None:
            inflight = self._avatar_inflight = set()
        if author.sec_uid in inflight:
            return
        inflight.add(author.sec_uid)

        def task():
            return author_assets.fetch_avatar(author.sec_uid, url)

        def done(result):
            inflight.discard(author.sec_uid)
            if not result:
                return
            path, digest = result
            if not author_assets.apply_avatar(author, path, digest):
                return
            self.store.add_author(author, flush=False)
            self._queue_auto_upload_paths([config.AUTHORS_FILE, path])
            if self.syncing:
                self._contacts_dirty = True
            else:
                self._render_contacts()

        def failed(_message):
            inflight.discard(author.sec_uid)

        worker = Worker(task)
        worker.done.connect(done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        self._track_worker(worker)
        worker.start()

    def _sync_profile_done(self, author, profile, account_index=0):
        """主页解析成功的收尾；任何异常都要回到同步状态机，避免卡住。"""
        try:
            self._sync_author_works(author, profile)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.exception("sync works failed for %s", author.label())
            self._sync_failed(author, str(e) or "同步作品失败", account_index)

    def _sync_failed(self, author, message, account_index=0):
        import logging
        logging.warning("sync author failed %s: %s", author.label(), message)
        if not self.syncing:
            return
        short = (message or "未知错误").replace("\n", " ")[:80]
        login_needed = any(key in (message or "") for key in
                           ("需要登录", "请先登录", "登录已失效"))
        if login_needed:
            if account_index in self._sync_slots:
                # 只有「这一轮确实在用」的账号才改登录状态并占掉槽位：迟到的
                # 失败回调以前会把账号1 标成未登录并写进 login_state.json。
                self._set_login_state(False, [], account_index)
                self._sync_slots.remove(account_index)
            if self._sync_slots:
                # 还有其他登录账号：把作者退回队列交给它接手；槽位会在
                # worker 结束后释放并自动补位，这里不主动推进。
                self._sync_queue.append(author)
                self.contact_status.setText(
                    f"账号{account_index + 1} 登录失效，已把 {author.label()} "
                    f"退回队列等待其他账号")
                return
            # 没有其他账号了：记录失败、结束同步并提示重新登录
            self._record_sync_failure(author, short)
            self._finish_sync(stopped=True)
            self._handle_failed(message)
            return
        if author.sec_uid not in self._sync_retry:
            self._sync_retry.add(author.sec_uid)
            self.contact_status.setText(
                f"{author.label()} 读取失败，正在重试：{short}")
            # 重新排队，空闲槽位会再抓一次（最多一次，由 _sync_retry 兜底）。
            self._sync_queue.append(author)
            return
        self._record_sync_failure(author, short)
        self.contact_status.setText(f"{author.label()} 同步失败，已跳过：{short}")
        # 不再排程：worker 结束后由 finished 释放槽位并补下一个。

    def _finish_sync(self, stopped=False):
        if not self.syncing and not stopped:
            return
        auto = getattr(self, "_sync_auto", False)
        # 同步期间 add_author 只标脏，这里统一落盘
        try:
            self.store.flush()
        except Exception:  # noqa: BLE001
            pass
        self.syncing = False
        self._sync_profiles_done = False
        self._sync_current = None
        self.contact_sync_btn.setText("全部同步")
        if stopped:
            self._dl_mode = None
            try:
                self.dm.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._auto_uploader is not None:
            self._auto_uploader.close()
        if stopped:
            self.contact_status.setText("已停止同步（已下载的作品会保留）")
            self.status.setText("同步已停止")
            self.download_panel.set_idle("同步已停止")
            sync_log.append_event("同步已停止（登录失效或无法继续）")
            if self._contacts_dirty:
                self._contacts_dirty = False
                self._render_contacts()
            self._update_retry_button()
            return
        self._dl_mode = None
        try:
            self.dm.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._contacts_dirty:
            self._contacts_dirty = False
            self._render_contacts()
        fails = list(getattr(self, "_sync_failed_authors", []))
        text = (f"全部同步完成：成功 {self._sync_total_new} 个作品，"
                f"失败 {self._sync_total_fail} 个")
        if fails:
            text += f"，{len(fails)} 位作者读取失败"
        sync_log.append_event(text)
        self.contact_status.setText(text)
        self.contact_status.setToolTip(
            "\n".join(f"{name}: {err}" for name, err in fails) if fails else "")
        self.status.setText("同步完成")
        self._update_retry_button()
        self._render_history()
        # 一次大同步会把上万个 MediaInfo 挂在队列里；结束后释放，避免长期占内存。
        # 此时 _dl_mode 已是 None，迟到的下载回调会直接返回，不会越界访问。
        self._dl_jobs = []
        self._dl_authors = []
        if fails and not auto:
            detail = "\n".join(f"{name}: {err}" for name, err in fails[:5])
            QMessageBox.warning(
                self, "同步完成（部分失败）",
                text + "\n\n" + detail + "\n\n可再次点「全部同步」重试。")

    # ---------- 作者目录与已下载判断 ----------
    def _author_dir_names(self, author_folder):
        """一次拿到作者目录下的所有条目名（新结构的作品 id 目录 / 旧 tar / 旧文件夹）。

        大作者有几百个作品，逐个作品 scandir 会在主线程累计出明显卡顿；
        先扫一次作者目录，名字对不上就直接判定为「没下过」。
        """
        names = set()
        for base in (config.SAVE_ROOT, config.DOWNLOAD_BASE):
            try:
                with os.scandir(os.path.join(base, author_folder)) as entries:
                    names.update(entry.name for entry in entries)
            except OSError:
                continue
        return names

    def _work_files_exist(self, work, author_folder, author_name,
                          work_folder=None):
        """已下载判断：新结构作品目录里有视频/首图，或旧版 tar / 旧作品文件夹。

        2026-11-09 起作品保存在 `<下载根>\\净影\\作者\\<作品id>\\`，
        2026-11-08 那批是 `<作品id>.tar`，更早的是「作者_文案_时间/」目录，
        三种都要认，否则老作品会被重新下载一遍。
        """
        folder_name = work_folder or downloader.build_work_folder(work, author_name)
        # 1) 新结构：净影/作者/<作品id>/
        new_dir = downloader.work_dir(author_folder, folder_name, work)
        prefix = "图片_01." if work.is_album() else "视频."
        try:
            with os.scandir(new_dir) as entries:
                for entry in entries:
                    if entry.name.startswith(prefix):
                        return True
        except OSError:
            pass
        # 2) tar：主界面「下载后打包成 tar」放在 SAVE_ROOT/作者/ 下，
        #    2026-11-08 那批旧包在 DOWNLOAD_BASE/作者/ 下，两处都要认。
        for base in (config.SAVE_ROOT, config.DOWNLOAD_BASE):
            tar_path = os.path.join(
                base, author_folder,
                downloader.archive_filename(work, folder_name))
            try:
                if os.path.isfile(tar_path) and os.path.getsize(tar_path) > 0:
                    return True
            except OSError:
                pass
        # 3) 更早的散文件目录
        folder = os.path.join(config.DOWNLOAD_BASE, author_folder, folder_name)
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith(prefix):
                        return True
        except OSError:
            return False
        return False

    def _work_in_cloud(self, author_folder, new_name, tar_name, folder_name):
        """作品是否已备份到 WebDAV。

        开启「上传成功后删除本地」后，作品目录里只剩 文案.txt，磁盘上看不出
        下过没下过；不看上传记录的话，每轮同步都会把整个库重新下载一遍。
        """
        checker = getattr(self, "_work_backed_up", None)
        if checker is None:
            return False
        try:
            return bool(checker(author_folder, (new_name, tar_name, folder_name)))
        except Exception:  # noqa: BLE001
            return False

    def _rename_author_folder(self, author, old_name, old_uid, old_folder, new_folder):
        """昵称/抖音号变化时，把旧作者文件夹改名，并更新历史记录里的路径。"""
        moved = 0
        bases = [
            os.path.join(config.SAVE_ROOT),
            os.path.join(config.DOWNLOAD_BASE),
            os.path.join(config.MUSIC_BASE),
        ]
        for base in bases:
            old_dir = os.path.join(base, old_folder)
            new_dir = os.path.join(base, new_folder)
            if not os.path.isdir(old_dir):
                continue
            try:
                if not os.path.exists(new_dir):
                    os.replace(old_dir, new_dir)
                    moved += 1
                else:
                    for entry in os.listdir(old_dir):
                        src = os.path.join(old_dir, entry)
                        dst = os.path.join(new_dir, entry)
                        if not os.path.exists(dst):
                            os.replace(src, dst)
                            moved += 1
                    try:
                        os.rmdir(old_dir)
                    except OSError:
                        pass
            except OSError:
                continue

        pairs = [
            (os.path.join(config.SAVE_ROOT, old_folder),
             os.path.join(config.SAVE_ROOT, new_folder)),
            (os.path.join(config.DOWNLOAD_BASE, old_folder),
             os.path.join(config.DOWNLOAD_BASE, new_folder)),
            (os.path.join(config.MUSIC_BASE, old_folder),
             os.path.join(config.MUSIC_BASE, new_folder)),
        ]
        items = self.store.load_history()
        changed_items = []
        for item in items:
            match = (item.author_sec_uid == author.sec_uid
                     or (old_uid and item.unique_id == old_uid)
                     or (old_name and item.author == old_name))
            if not match:
                continue
            changed = False
            for attr in ("video_path", "music_path"):
                value = getattr(item, attr)
                if not value:
                    continue
                for old_prefix, new_prefix in pairs:
                    if value.startswith(old_prefix):
                        candidate = new_prefix + value[len(old_prefix):]
                        if os.path.exists(candidate) or not os.path.exists(value):
                            setattr(item, attr, candidate)
                            changed = True
                        break
            new_albums = []
            for path in item.album_paths:
                candidate = path
                for old_prefix, new_prefix in pairs:
                    if path.startswith(old_prefix):
                        possibly = new_prefix + path[len(old_prefix):]
                        if os.path.exists(possibly) or not os.path.exists(path):
                            candidate = possibly
                        break
                new_albums.append(candidate)
            if new_albums != item.album_paths:
                item.album_paths = new_albums
                changed = True
            if changed:
                changed_items.append(item)
        # 只 UPDATE 受影响的这些行；历史上万条也不再全量重写
        self.store.update_history_items(changed_items)
        return moved > 0, moved + len(changed_items)

    def _sync_author_works(self, author, profile):
        if not self.syncing:
            return
        self._clear_sync_failure(author)
        import logging
        logging.info("sync author=%s videos=%d albums=%d sig=%d avatar=%s total=%s",
                     author.unique_id or author.sec_uid[:12],
                     len(profile.videos), len(profile.albums),
                     len(profile.signature or ""), bool(profile.avatar_url),
                     getattr(profile, "total_works", 0))
        total_works = len(profile.videos) + len(profile.albums)
        declared = int(getattr(profile, "total_works", 0) or 0)
        if declared > total_works:
            self._sync_note(
                f"已发现 {author.label()} 的 {total_works} 个作品（主页显示 {declared} 个）")
        else:
            self._sync_note(f"已发现 {author.label()} 的 {total_works} 个作品")
        previous = len(author.saved_ids)
        # 主页作品列表偶尔只加载出首屏，明显少于已下载数量时自动重试一次
        if (previous >= 20 and total_works < max(10, previous // 2)
                and author.sec_uid not in getattr(self, "_sync_retried", set())):
            self._sync_retried.add(author.sec_uid)
            self.contact_status.setText(
                f"{author.label()}：作品列表加载不完整（{total_works} 个），"
                f"重新排队补齐…")
            # 重新排队，空闲槽位会再次抓取该作者（最多一次，由 _sync_retried 兜底）
            self._sync_queue.append(author)
            return
        # 「查看日志」只记这一类信息：账号 / 作者 / 作品数。
        # 放在重试判断之后，保证记的是最终确认的数量，不会重复记一条。
        account_label = self._sync_author_account.get(
            author.sec_uid, self._sync_account_label or "账号1")
        sync_log.append(account_label, author.label(), total_works)
        # 「下载日志」也记一条：用哪个账号打开了哪位作者、找到了多少作品
        download_log.append_open(account_label, author.label(), total_works)
        old_name = author.nickname
        old_uid = author.unique_id
        author_name = profile.nickname or old_name
        author_folder = downloader.build_author_folder(
            author_name, profile.unique_id or old_uid)
        rename_note = ""
        if (old_name or old_uid) and profile.nickname:
            old_folder = downloader.build_author_folder(old_name, old_uid)
            if old_folder and old_folder != author_folder:
                ok, count = self._rename_author_folder(
                    author, old_name, old_uid, old_folder, author_folder)
                rename_note = ("（作者文件夹已改名）" if ok
                               else "（作者文件夹改名失败，文件可能被占用）")
                logging.info("author folder rename %s -> %s ok=%s count=%s",
                             old_folder, author_folder, ok, count)
        # 无论有没有新作品，都先检查并保存头像/简介。
        # 注意：这里在主线程（worker 的 done 回调）里，头像不在这里下载，
        # 只标记「需要下载」，交给后台线程，避免网络请求把界面卡住。
        changes = author_assets.update_author_assets(
            author, profile, download_avatar=False)
        profile_note = ""
        changed_profile = False
        if profile.nickname and profile.nickname != author.nickname:
            author.nickname = profile.nickname
            changed_profile = True
        if profile.unique_id and profile.unique_id != author.unique_id:
            author.unique_id = profile.unique_id
            changed_profile = True
        if changes["avatar_changed"] or changes["signature_changed"]:
            profile_note = "（资料有修改，已另存一份）"
        if declared > total_works:
            profile_note += (
                f"（主页显示 {declared} 个，本次读到 {total_works} 个；"
                "可能含私密/受限作品，或列表未加载完）")
        elif getattr(profile, "truncated", False):
            profile_note += "（作品列表未加载完，可再同步一次补齐）"
        profile_note += rename_note
        if (changed_profile or changes["signature_changed"]
                or changes["avatar_needs_download"]):
            # 同步期间只标脏，等按时间限频的 store.flush() / 同步结束统一落盘，
            # 避免每抓到一个作者就重写一次 authors.json 卡主线程。
            self.store.add_author(author, flush=not self.syncing)
            # 作者资料已经在 downloads/作者资料 下：同步时把 authors.json 也放进
            # 自动上传队列，和作品一起备份到 WebDAV。
            self._queue_auto_upload_paths([config.AUTHORS_FILE])
            # 同步过程中不要每抓到一个作者就重建整个通讯录列表：
            # 频繁 clear()/setItemWidget() 会删除大量自定义行，长时间同步容易触发 Qt 栈溢出。
            if self.syncing:
                self._contacts_dirty = True
            else:
                self._render_contacts()
        if changes["avatar_needs_download"]:
            self._queue_avatar_download(author, profile.avatar_url)

        works = list(profile.videos) + list(profile.albums)
        if not works:
            self.contact_status.setText(
                f"已发现 {author.label()} 的 0 个作品{profile_note}")
            # 槽位由 worker 的 finished 释放并自动补位，这里不用再推进
            return
        pending = []
        skipped = 0
        work_folders = {}
        # 先扫一次作者目录，拿到已存在的条目名（作品 tar 或旧版作品文件夹）。
        # 名字对不上的作品直接判定为未下载，省掉每个作品一次 os.scandir。
        existing_names = self._author_dir_names(author_folder)
        for work in works:
            # 主页作品对象经常没有作者抖音号；用本次作者资料补齐，避免文案.txt
            # 写成“抖音号：未知”。
            if not work.unique_id:
                work.unique_id = author.unique_id or profile.unique_id
            if not work.author_id:
                work.author_id = author.sec_uid
            if not work.author:
                work.author = author.nickname or profile.nickname
            folder_name = downloader.build_work_folder(work, author_name)
            work_folders[work.aweme_id] = folder_name
            # 新结构目录名是作品 id；旧结构是 <作品id>.tar 或「作者_文案_时间」目录。
            # 三个名字都要比，否则新下载的作品每轮同步都会被当成「没下过」重下一遍。
            new_name = downloader.work_folder_name(work, folder_name)
            tar_name = downloader.archive_filename(work, folder_name)
            on_disk = (new_name in existing_names
                       or tar_name in existing_names
                       or folder_name in existing_names)
            if not on_disk:
                # 本地可能被「上传后删除」清空了：云端已有备份就别再下一遍。
                if self._work_in_cloud(author_folder, new_name, tar_name, folder_name):
                    skipped += 1
                else:
                    pending.append(work)
                continue
            # 只按磁盘文件判断，避免 saved_ids 记录了但文件已丢失时被误跳过
            if self._work_files_exist(work, author_folder, author_name,
                                      folder_name):
                skipped += 1
            elif self._work_in_cloud(author_folder, new_name, tar_name, folder_name):
                # 目录还在（只剩文案.txt），媒体已经上传后删掉了
                skipped += 1
            else:
                pending.append(work)
        if not pending:
            self.contact_status.setText(
                f"已发现 {author.label()} 的 {total_works} 个作品，全部已下载"
                f"（跳过 {skipped} 个）{profile_note}")
            # 槽位由 worker 的 finished 释放并自动补位，这里不用再推进
            return
        # 记下该作者这一轮还要下多少个，供「下载日志」算「还剩多少没下完」
        self._sync_author_remaining[author.sec_uid] = len(pending)
        jobs = [(work, author_folder, work_folders[work.aweme_id])
                for work in pending]
        start_index = len(self._dl_jobs)
        self._dl_jobs.extend(jobs)
        self._dl_authors.extend([author] * len(jobs))
        self._dl_total = len(self._dl_jobs)
        self._dl_mode = "sync"
        self._dl_author = None
        self.contact_status.setText(
            f"已发现 {author.label()} 的 {total_works} 个作品："
            f"新增 {len(pending)} 个，跳过已下载 {skipped} 个"
            f"（已加入并发下载队列）{profile_note}")
        self.dm.enqueue(jobs, start_index=start_index)
        # 不等待这批下载完成：worker 结束后槽位自动释放，由 _sync_dispatch
        # 补入下一位作者，继续并发抓取。

