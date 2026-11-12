# -*- coding: utf-8 -*-
"""WebDAV 上传：配置、增量上传、传输进度、上传后清理本地媒体。

以 mixin 的形式挂在 MainWindow 上：这些方法需要主窗口的控件与状态
（status / upload_panel / _up_meter / _upload_active …），由 MainWindow.__init__
统一创建，本模块只负责业务逻辑。
"""
import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox,
)

import auto_upload
import config
import webdav
from workers import Worker

# 「上传后清理本地」认识的媒体后缀。作品归档包（.tar）也算：作品的全部文件
# （含文案）都在包里，上传成功后本地就不需要再留一份。
MEDIA_EXTS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
    ".webp", ".jpg", ".jpeg", ".png", ".gif", ".heic",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg",
    ".tar",
}

# 上传记录里的相对路径都以这几个顶层目录开头（对应 UPLOAD_DIRS）。
UPLOAD_BASES = ("净影", "Videos", "Music")


def _close_client(dav):
    """关掉 WebDAVClient 的连接池；测试里的假 client 没有 close 也不能抛。"""
    try:
        dav.close()
    except Exception:  # noqa: BLE001
        pass


def upload_unit_of(rel):
    """把上传记录里的相对路径归到「作品单元」：作品目录，或作品 tar 文件本身。

    只有「主文件」（视频 / 图集首图 / 归档 tar）才算数：音乐、文案、头像单独
    上传成功不能代表这个作品已经备份好，否则视频传失败时会被误判成已下载。
    """
    rel = str(rel or "").replace("\\", "/").strip("/")
    if not rel:
        return ""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] not in UPLOAD_BASES:
        return ""
    name = parts[-1]
    if name.lower().endswith(".tar"):
        return rel
    if not (name.startswith("视频.") or name.startswith("图片_01.")):
        return ""
    if len(parts) < 3:
        return ""
    return "/".join(parts[:-1])


class UploadBridge(QObject):
    # 字节用 qint64：Qt 的 int 只有 32 位，累计上传超过 2GB 后 emit 会抛
    # OverflowError，上传进度就再也不刷新了。
    started = Signal(str, "qint64")          # 相对路径, 文件字节数
    done = Signal(str, str)                  # 相对路径, done/skipped/failed
    bytes_sent = Signal("qint64")            # 已发送字节（速度统计）
    progress = Signal("qint64", "qint64")    # 已上传字节, 总字节
    file_progress = Signal(str, "qint64", "qint64")  # 相对路径, 当前文件已传字节, 文件总字节


class WebdavMixin:
    """WebDAV 上传、进度、上传记录与上传后清理。

    宿主（MainWindow）需要提供：
    - `_webdav`（服务器配置）、`_webdav_state_cache`、`_webdav_state_lock`
    - 控件 `upload_panel` / `webdav_status` / `webdav_auto_cb` / `webdav_delete_cb`
    - `_up_meter` / `_up_total_bytes` / `_up_done_bytes` / `_upload_active`
    - `_settings` + `_save_settings`、`_transfer_add` / `_transfer_update`
    - `_track_worker`（起后台线程）、`_handle_failed`（统一报错）
    """

    def _upload_task_started(self, rel, total_bytes=0):
        key = f"up-{rel}"
        self._transfer_add(key, rel)
        self._transfer_update(key, "上传中…")
        self.upload_panel.set_item_progress(key, 0)

    def _upload_file_progress(self, rel, done, total):
        try:
            percent = int(done) * 100 / max(1, int(total))
        except (TypeError, ValueError):
            return
        self.upload_panel.set_item_progress(f"up-{rel}", percent)

    def _upload_task_done(self, rel, status):
        label = {"done": "完成", "skipped": "跳过（已上传）", "failed": "失败"}.get(status, status)
        self._transfer_update(f"up-{rel}", label)
        if status in ("done", "skipped"):
            self.upload_panel.set_item_progress(f"up-{rel}", 100)
            # 云端确认成功就立刻删本地：以前只在整个上传任务结束时扫一遍，
            # 任务被中断、关窗或中途失败就一个文件都不删，表现就是「上传完
            # 了本地还在」。随文件删还有个好处：删完马上腾出磁盘空间。
            self._delete_after_upload(rel)
        counts = self._upload_counts
        key = {"done": "ok", "skipped": "skipped", "failed": "failed"}.get(status)
        if key:
            counts[key] = counts.get(key, 0) + 1
        self.webdav_status.setText(
            f"上传中：完成 {counts.get('ok', 0)}，跳过 {counts.get('skipped', 0)}，"
            f"失败 {counts.get('failed', 0)}")

    # ---------- 上传后清理本地媒体 ----------
    def _delete_uploaded_media(self, remote_rel):
        """远程确认成功后，删除对应本地媒体；文案和作者资料永不删除。"""
        import logging
        rel = self._webdav_local_rel(remote_rel)
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in ("净影", "Videos", "Music"):
            return
        if os.path.splitext(parts[-1])[1].lower() not in MEDIA_EXTS:
            return
        root = os.path.abspath(config.UPLOAD_ROOT)
        path = os.path.abspath(os.path.join(root, *parts))
        try:
            if os.path.commonpath([root, path]) != root:
                return
        except ValueError:
            return
        if not os.path.isfile(path):
            return
        try:
            os.remove(path)
        except OSError as exc:
            logging.warning("delete local media failed %s: %s", path, exc)
            return
        # 删完后顺手清掉空的 作品/作者/Music 目录；有 文案.txt 的目录会保留。
        folder = os.path.dirname(path)
        stop = os.path.join(root, parts[0])
        while folder and os.path.abspath(folder) != os.path.abspath(stop):
            try:
                os.rmdir(folder)
            except OSError:
                break
            folder = os.path.dirname(folder)
        logging.info("deleted uploaded local media %s", path)

    def _delete_after_upload(self, rel):
        """单个文件确认在云端后立刻删本地媒体（受「上传后删除」开关控制）。"""
        self._note_uploaded_unit(rel)
        cb = getattr(self, "webdav_delete_cb", None)
        if cb is None or not cb.isChecked():
            return
        self._delete_uploaded_media(rel)

    # ---------- 已上传单元索引（供「是否已下载」判断用） ----------
    def _uploaded_units(self):
        """云端已有备份的「作品单元」集合：作品目录（去掉文件名）或作品 tar 本身。

        「上传后删除本地」删掉媒体以后，本地目录里只剩 文案.txt，而同步时
        「是否已下载」只看磁盘上有没有 视频./图片_01.，于是每轮同步都会把
        整个库重新下载一遍——删了又回来，用户看到的就是「删不掉」。
        这里把上传记录当成「已备份」凭证：单元在集合里就算已下载，不再重下。
        """
        cfg = getattr(self, "_webdav", {}) or {}
        key = (cfg.get("url", ""), cfg.get("remote_root", ""))
        cache = getattr(self, "_uploaded_units_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        units = set()
        files = self._load_webdav_state().get("files") or {}
        for rel in files:
            unit = upload_unit_of(rel)
            if unit:
                units.add(unit)
        self._uploaded_units_cache = (key, units)
        return units

    def _note_uploaded_unit(self, rel):
        """上传完成时增量更新单元索引，避免每次重算十几万条记录。"""
        cfg = getattr(self, "_webdav", {}) or {}
        key = (cfg.get("url", ""), cfg.get("remote_root", ""))
        cache = getattr(self, "_uploaded_units_cache", None)
        if cache is None or cache[0] != key:
            return
        unit = upload_unit_of(rel)
        if unit:
            cache[1].add(unit)

    def _work_backed_up(self, author_folder, names):
        """作品是否已经上传到云端（本地媒体被「上传后删除」清掉时用）。"""
        if not author_folder or not names:
            return False
        try:
            units = self._uploaded_units()
        except Exception:  # noqa: BLE001
            return False
        if not units:
            return False
        for base in ("净影", "Videos", "Music"):
            for name in names:
                if not name:
                    continue
                if f"{base}/{author_folder}/{name}" in units:
                    return True
        return False

    def _cleanup_uploaded_media_async(self):
        """上传结束后按已上传记录批量清理本地媒体；保留文案/作者资料。"""
        if (getattr(self, "_cleanup_active", False)
                or getattr(self, "webdav_delete_cb", None) is None
                or not self.webdav_delete_cb.isChecked()):
            return
        files = self._load_webdav_state().get("files") or {}
        if not files:
            return
        self._cleanup_active = True
        root = os.path.abspath(config.UPLOAD_ROOT)

        def task(stage=None):
            if stage:
                stage("正在清理已上传的本地媒体…")
            deleted = 0
            failed = 0
            # 新结构在 净影/ 下，旧数据在 Videos/Music 下，三处都要扫
            for base in (config.SAVE_ROOT, config.DOWNLOAD_BASE, config.MUSIC_BASE):
                targets = []
                for dirpath, _dirs, names in os.walk(base):
                    for name in names:
                        if os.path.splitext(name)[1].lower() not in MEDIA_EXTS:
                            continue
                        path = os.path.join(dirpath, name)
                        rel = os.path.relpath(path, root).replace("\\", "/")
                        if self._webdav_remote_rel(rel) in files:
                            targets.append(path)
                for path in targets:
                    try:
                        os.remove(path)
                        deleted += 1
                    except OSError as exc:
                        failed += 1
                        logging.warning("cleanup local media failed %s: %s", path, exc)
                for dirpath, _dirs, _names in os.walk(base, topdown=False):
                    if os.path.abspath(dirpath) == os.path.abspath(base):
                        continue
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass
            # 没有任何日志时，用户报「删不掉」根本没法排查：留一条汇总。
            logging.info("cleanup uploaded media: deleted=%d failed=%d", deleted, failed)
            return deleted

        worker = Worker(task)
        worker.stage.connect(
            lambda text: self.status.setText(text),
            Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._cleanup_media_done,
                            Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._cleanup_media_failed,
                              Qt.ConnectionType.QueuedConnection)
        self._cleanup_worker = self._track_worker(worker)
        worker.start()

    def _cleanup_media_done(self, count):
        self._cleanup_active = False
        self.status.setText(f"已清理本地媒体 {count} 个文件（文案和作者资料已保留）")
        self.webdav_status.setText(
            f"已清理本地媒体 {count} 个文件；文案和作者资料已保留")

    def _cleanup_media_failed(self, message):
        self._cleanup_active = False
        self.status.setText(f"清理本地媒体失败：{message}")

    # ---------- 配置与上传记录 ----------
    def _webdav_file(self):
        return os.path.join(config.DATA_DIR, "webdav.json")

    def _load_webdav_config(self):
        import json
        try:
            with open(self._webdav_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_webdav_config(self, data):
        import json
        self._webdav = data
        try:
            with open(self._webdav_file(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError as e:
            QMessageBox.warning(self, "WebDAV", f"保存配置失败：{e}")
        self._refresh_webdav_status()

    def _refresh_webdav_status(self, lazy=False):
        cfg = getattr(self, "_webdav", {}) or {}
        url = cfg.get("url", "")
        if hasattr(self, "webdav_auto_cb"):
            self.webdav_auto_cb.setEnabled(bool(url))
            if url:
                self.webdav_auto_cb.setToolTip(
                    "同步下载通讯录新作品的同时，把新文件传到 WebDAV")
            else:
                self.webdav_auto_cb.setToolTip("请先配置 WebDAV 服务器")
        if url:
            root = cfg.get("remote_root", "") or "/"
            if lazy and getattr(self, "_webdav_state_cache", None) is None:
                # 上传记录可达 10 MB：启动时先不解析，等后台 maintenance 读完再补上数量。
                self.webdav_status.setText(f"已配置：{url}    远程目录：{root}")
                return
            count = len((self._load_webdav_state().get("files") or {}))
            self.webdav_status.setText(
                f"已配置：{url}    远程目录：{root}    已上传记录：{count} 个文件")
        else:
            self.webdav_status.setText("未配置。配置后可将 净影/作者资料 等目录按原结构上传。")

    def _set_auto_upload_enabled(self, checked):
        self._settings["webdav_auto_upload"] = bool(checked)
        self._save_settings()
        if not checked and self._auto_uploader is not None:
            old = self._auto_uploader
            try:
                self._save_auto_state(force=True)
            except Exception:  # noqa: BLE001
                pass
            self._auto_uploader = None
            self._upload_active = False
            old.close()
        self.webdav_status.setText(
            "通讯录同步时自动上传：已开启" if checked else "通讯录同步时自动上传：已关闭")

    def _set_delete_after_upload(self, checked):
        if checked:
            answer = QMessageBox.question(
                self, "上传后删除本地媒体",
                "开启后，上传成功以及已记录为已上传的视频、图片、音乐会从本地删除。\n\n"
                "文案、历史记录、作者头像/简介和作者资料会保留。\n"
                "请确认 WebDAV 上的备份可以正常访问。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.webdav_delete_cb.blockSignals(True)
                self.webdav_delete_cb.setChecked(False)
                self.webdav_delete_cb.blockSignals(False)
                return
        self._settings["webdav_delete_after_upload"] = bool(checked)
        self._save_settings()
        if checked:
            self.webdav_status.setText(
                "上传成功后删除本地媒体：已开启（只删视频/图片/音乐，保留文案和作者资料）")
        else:
            self.webdav_status.setText("上传成功后删除本地媒体：已关闭")

    def _webdav_state_file(self):
        return os.path.join(config.DATA_DIR, "webdav_uploaded.json")

    def _load_webdav_state(self):
        """读取上传状态。

        状态文件会随作品数量线性增长（实测 7.8 万条记录 ≈ 10 MB），每次刷新状态栏
        都重新解析要 0.4 s 以上。这里按（服务器, 远程根目录）缓存解析结果，程序内部
        只通过本对象读写状态，缓存即最新值。
        """
        import json
        cfg = getattr(self, "_webdav", {}) or {}
        key = (cfg.get("url", ""), cfg.get("remote_root", ""))
        cache = getattr(self, "_webdav_state_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        state = {"server": cfg.get("url", ""), "root": cfg.get("remote_root", ""), "files": {}}
        try:
            with open(self._webdav_state_file(), encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict) and data.get("server") == state["server"]
                    and data.get("root") == state["root"]):
                files = data.get("files")
                if not isinstance(files, dict):
                    files = {}
                if not files:
                    # 兼容旧版把 相对路径->指纹 直接写在顶层的状态文件
                    files = {key: value for key, value in data.items()
                             if key not in ("server", "root", "files")
                             and isinstance(value, str)}
                state["files"] = files
        except (OSError, ValueError):
            pass
        self._webdav_state_cache = (key, state)
        return state

    def _save_webdav_state(self, state):
        import json
        path = self._webdav_state_file()
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _webdav_remote_rel(self, rel):
        """远程目录与本地拍平后的目录保持一致：Videos/作者/作品。"""
        return str(rel or "").replace("\\", "/")

    def _webdav_local_rel(self, rel):
        """远程 rel 与本地拍平后的 rel 一致。"""
        return str(rel or "").replace("\\", "/")

    def _configure_webdav(self):
        cfg = getattr(self, "_webdav", {}) or {}
        dialog = QDialog(self)
        dialog.setWindowTitle("WebDAV 配置")
        dialog.resize(520, 320)
        form = QFormLayout(dialog)
        url_edit = QLineEdit(cfg.get("url", ""))
        url_edit.setPlaceholderText("https://dav.example.com/remote.php/dav/files/user")
        user_edit = QLineEdit(cfg.get("username", ""))
        pass_edit = QLineEdit(cfg.get("password", ""))
        pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        root_edit = QLineEdit(cfg.get("remote_root", "净影"))
        root_edit.setPlaceholderText("远程根目录，可留空")
        verify_cb = QCheckBox("忽略 HTTPS 证书错误（自签名证书时勾选）")
        verify_cb.setChecked(cfg.get("verify", True) is False)
        form.addRow("服务器地址：", url_edit)
        form.addRow("用户名：", user_edit)
        form.addRow("密码：", pass_edit)
        form.addRow("远程根目录：", root_edit)
        form.addRow(verify_cb)
        note = QLabel("上传时保留 净影/Videos/Music/作者资料 的原目录结构；"
                      "只扫描这些目录，不会动其他文件。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6b7280;font-size:11px;")
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url = url_edit.text().strip().rstrip("/")
        if not url:
            QMessageBox.warning(self, "WebDAV", "服务器地址不能为空。")
            return
        self._save_webdav_config({
            "url": url,
            "username": user_edit.text().strip(),
            "password": pass_edit.text(),
            "remote_root": root_edit.text().strip().strip("/"),
            "verify": not verify_cb.isChecked(),
        })

    def _webdav_client(self):
        cfg = getattr(self, "_webdav", {}) or {}
        if not cfg.get("url"):
            QMessageBox.information(self, "WebDAV", "请先配置 WebDAV 服务器。")
            return None
        try:
            return webdav.WebDAVClient(
                cfg.get("url", ""), cfg.get("username", ""),
                cfg.get("password", ""), cfg.get("remote_root", ""),
                verify=cfg.get("verify", True))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "WebDAV", str(e))
            return None

    def _test_webdav(self):
        client = self._webdav_client()
        if client is None:
            return
        if self.worker and self.worker.isRunning():
            _close_client(client)
            QMessageBox.information(self, "WebDAV", "已有任务在进行，请稍后再试。")
            return

        def done(_ok):
            _close_client(client)
            QMessageBox.information(self, "WebDAV", "连接成功，远程目录可写。")

        def failed(message):
            _close_client(client)
            self._handle_failed(message)

        self._run_worker(client.test, on_result=done, on_failed=failed,
                         busy_text="正在测试 WebDAV 连接…", force=True)

    # ---------- 通讯录同步时自动增量上传 ----------
    def _start_auto_upload(self):
        """通讯录同步开始时，启动增量上传队列（与下载并行，不占用解析线程）。"""
        if self._auto_uploader is not None:
            return
        if not self.webdav_auto_cb.isChecked():
            return
        cfg = getattr(self, "_webdav", {}) or {}
        url = cfg.get("url", "")
        if not url or self._upload_active:
            return
        try:
            client = webdav.WebDAVClient(
                url, cfg.get("username", ""), cfg.get("password", ""),
                cfg.get("remote_root", ""), verify=cfg.get("verify", True))
        except Exception:  # noqa: BLE001
            return
        root = config.UPLOAD_ROOT
        uploader = auto_upload.AutoUploader(
            client, root, self._load_webdav_state(), self._save_auto_state,
            self._up_threads, parent=self, rel_map=self._webdav_remote_rel)
        uploader.started.connect(self._upload_task_started)
        uploader.file_progress.connect(self._upload_file_progress)
        uploader.file_done.connect(self._auto_file_done)
        uploader.file_failed.connect(self._auto_file_failed)
        uploader.bytes_sent.connect(self._on_upload_bytes)
        uploader.progress.connect(self._on_upload_progress)
        uploader.finished.connect(self._auto_upload_finished)
        self._auto_uploader = uploader
        self._auto_upload_client = client
        self._upload_counts = {"ok": 0, "skipped": 0, "failed": 0}
        self._upload_last_error = ""
        self._upload_active = True
        self._up_total_bytes = 0
        self._up_done_bytes = 0
        self._up_meter.reset()
        self.upload_panel.set_stats(0, 0, "自动上传准备中…")
        uploader.start()

    def _save_auto_state(self, force=False):
        """自动上传的记录落盘：平时 20 秒节流，任务收尾强制写一次。

        以前每成功上传一个文件就把上传记录整个序列化一遍（本机 7.8 万条、
        约 10 MB），同步上千个作品时是上千次全量磁盘写，O(n²) 地拖慢上传；
        手动「上传全部」那条链路本来就有节流，两条实现不一致。
        """
        uploader = self._auto_uploader
        if uploader is None:
            return
        lock = getattr(self, "_auto_state_save_lock", None)
        if lock is None:
            lock = self._auto_state_save_lock = threading.Lock()
            self._auto_state_last_save = 0.0
        # 平时拿不到锁就让别的线程继续传文件；收尾时阻塞等，确保不丢记录。
        if not lock.acquire(force):
            return
        try:
            now = time.monotonic()
            if not force and now - self._auto_state_last_save < 20.0:
                return
            self._auto_state_last_save = now
            with self._webdav_state_lock:
                self._save_webdav_state(uploader.state_snapshot())
        finally:
            lock.release()

    def _queue_auto_upload(self, result):
        uploader = self._auto_uploader
        if uploader is None:
            return
        paths = [result.video_path, result.music_path, result.caption_path]
        paths += list(result.album_paths)
        paths += list(getattr(result, "live_paths", []))
        # 2026-11-09 起一个作品一个文件夹，archive_path 恒为空（老 tar 结构已废弃），
        # 不用再把不存在的归档路径塞进队列。
        self._queue_auto_upload_paths(paths)

    def _queue_auto_upload_paths(self, paths):
        uploader = self._auto_uploader
        if uploader is None:
            return
        uploader.enqueue([path for path in paths if path])

    def _auto_file_done(self, rel, status, _size=0):
        # 必须是 MainWindow 的绑定方法：AutoUploader 从上传线程 emit，
        # 不能用 lambda 直接改 UI，否则会在非主线程操作 QWidget。
        self._upload_task_done(rel, status)

    def _auto_file_failed(self, rel, message):
        """记下第一条真实错误：界面只显示「失败 N 个」时根本没法排查。"""
        if not getattr(self, "_upload_last_error", ""):
            self._upload_last_error = message
        self.webdav_status.setText(f"上传失败：{str(message)[:160]}")
        self.webdav_status.setToolTip(f"{rel}\n{message}")

    def _auto_upload_finished(self, ok, skipped, failed):
        # 收尾时强制落盘一次：节流窗口内成功的文件不能因为任务结束而丢记录。
        try:
            self._save_auto_state(force=True)
        except Exception:  # noqa: BLE001
            pass
        self._upload_active = False
        self._auto_uploader = None
        # 所有上传线程都已退出，这时才能安全关掉 client 的连接池。
        client = getattr(self, "_auto_upload_client", None)
        self._auto_upload_client = None
        if client is not None:
            try:
                _close_client(client)
            except Exception:  # noqa: BLE001
                pass
        # 不立即 deleteLater：上传线程的 file_done 信号可能还在事件队列里，
        # 删除 QObject 后再投递会造成 Qt 崩溃；留给主窗口父对象统一回收。
        self._refresh_webdav_status()
        detail = f"自动上传完成：成功 {ok} · 跳过 {skipped} · 失败 {failed}"
        if failed:
            message = getattr(self, "_upload_last_error", "")
            if message:
                detail += f"（{str(message)[:160]}）"
            self.webdav_status.setToolTip(message or detail)
        self.webdav_status.setText(detail)
        self.upload_panel.set_stats(
            100 if (ok or skipped or self._up_total_bytes) else 0,
            0.0, detail)
        self._cleanup_uploaded_media_async()

    # ---------- 手动「上传全部」 ----------
    def _upload_webdav(self):
        client = self._webdav_client()
        if client is None:
            return
        if self._upload_active:
            _close_client(client)
            QMessageBox.information(self, "WebDAV", "已有上传任务在进行，请稍后再试。")
            return
        answer = QMessageBox.question(
            self, "上传全部",
            "将 downloads 下全部文件按原目录结构上传到 WebDAV？\n"
            "文件较多时会持续一段时间。")
        if answer != QMessageBox.StandardButton.Yes:
            _close_client(client)
            return
        root = config.UPLOAD_ROOT
        if not os.path.isdir(root):
            _close_client(client)
            QMessageBox.information(self, "WebDAV", "本地下载目录不存在。")
            return

        state_obj = self._load_webdav_state() if self.webdav_skip_cb.isChecked() else None
        state = state_obj.get("files") if state_obj is not None else None
        state_lock = threading.RLock()
        state_save_lock = threading.Lock()
        last_state_save = [0.0]
        bridge = UploadBridge()
        bridge.started.connect(self._upload_task_started)
        bridge.done.connect(self._upload_task_done)
        bridge.bytes_sent.connect(self._on_upload_bytes)
        bridge.progress.connect(self._on_upload_progress)
        bridge.file_progress.connect(self._upload_file_progress)
        self._upload_bridge = bridge
        self._upload_counts = {"ok": 0, "skipped": 0, "failed": 0}
        self._upload_last_error = ""
        self._upload_active = True
        self._up_total_bytes = 0
        self._up_done_bytes = 0
        self._up_meter.reset()
        self.upload_panel.set_stats(0, 0, "准备上传…")
        upload_workers = self._up_threads

        def save_state():
            if state_obj is None:
                return
            # 同一时间只允许一个线程做快照+落盘；其余上传线程继续传文件。
            if not state_save_lock.acquire(False):
                return
            try:
                now = time.monotonic()
                if now - last_state_save[0] < 20.0:
                    return
                last_state_save[0] = now
                with state_lock:
                    snapshot = dict(state)
                self._save_webdav_state({
                    "server": state_obj.get("server", ""),
                    "root": state_obj.get("root", ""),
                    "files": snapshot,
                })
            finally:
                state_save_lock.release()

        def task(with_stage=None):
            stage = with_stage or (lambda text: None)
            # 先确认远程根目录可写：建不出来时立刻报错，而不是让每个文件
            # 都失败一遍、最后只显示「失败 N 个」。
            client.ensure_root()
            return client.upload_tree(
                root, roots=config.UPLOAD_DIRS,
                progress=stage, state=state, save_state=save_state,
                state_lock=state_lock, rel_map=self._webdav_remote_rel,
                max_workers=upload_workers,
                on_start=lambda rel, size: bridge.started.emit(rel, int(size)),
                on_done=lambda rel, status: bridge.done.emit(rel, status),
                on_bytes=lambda count: bridge.bytes_sent.emit(int(count)),
                on_progress=lambda done, total: bridge.progress.emit(int(done), int(total)),
                on_file_progress=lambda rel, done, total: bridge.file_progress.emit(
                    rel, int(done), int(total)))

        def done(result):
            ok, skipped, failed = result
            # 上传线程已全部退出，关掉这个 client 的连接池（每次点「上传全部」
            # 都会新建一个 client，以前从不回收）。
            _close_client(client)
            self._upload_active = False
            if state_obj is not None:
                with state_lock:
                    snapshot = dict(state)
                self._save_webdav_state({
                    "server": state_obj.get("server", ""),
                    "root": state_obj.get("root", ""),
                    "files": snapshot,
                })
            self._refresh_webdav_status()
            text = f"上传完成：成功 {ok} 个文件"
            if state_obj is not None:
                text += f"，跳过已上传 {skipped} 个"
            if failed:
                text += f"，失败 {len(failed)} 个"
                detail = "\n".join(f"{name}: {err}" for name, err in failed[:5])
                text += "\n\n前几个失败：\n" + detail
            self.upload_panel.set_stats(100, 0, text.split("\n")[0])
            self._cleanup_uploaded_media_async()
            QMessageBox.information(self, "WebDAV", text)

        def failed(message):
            _close_client(client)
            self._upload_active = False
            self.upload_panel.set_idle("上传失败")
            self._handle_failed(message)

        # 手动“上传全部”不能占用 self.worker：解析/同步/单作品任务都用
        # self.worker，上传一开就会被误判成“已有解析任务”，两边互相挡。
        upload_worker = Worker(task)
        upload_worker.stage.connect(
            lambda text: self.status.setText(text),
            Qt.ConnectionType.QueuedConnection)
        upload_worker.done.connect(done, Qt.ConnectionType.QueuedConnection)
        upload_worker.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        self._upload_worker = self._track_worker(upload_worker)
        self.status.setText("正在上传到 WebDAV…")
        upload_worker.start()

    def _on_upload_bytes(self, count):
        self._up_meter.add(count)

    def _on_upload_progress(self, done, total):
        self._up_done_bytes = max(self._up_done_bytes, max(0, int(done)))
        self._up_total_bytes = max(0, int(total))

