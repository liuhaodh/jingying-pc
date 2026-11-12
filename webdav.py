# -*- coding: utf-8 -*-
"""WebDAV 上传：保持本地 downloads 下的原目录结构，支持并行上传与增量跳过。"""
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

# 目录创建失败后短时间内不再重复尝试，避免几千个文件各卡一次超时
_DIR_FAIL_TTL = 20.0


def _upload_priority(roots):
    """按待扫描目录的顺序给顶层目录排优先级，返回 {顶层目录名: 序号}。

    「上传全部」要先传作品（视频/图集/文案），再传音乐和作者资料，用户才
    不会长时间只看到音乐。以前硬编码只把 `Videos/` 排到最前；2026-11-09 起
    新作品改存 `净影/`，中文名按字符串排序会落到最后。这里直接由 roots
    （即 config.UPLOAD_DIRS）推导，目录结构再变也不会脱节。
    """
    priority = {}
    for index, root in enumerate(roots or []):
        name = os.path.basename(os.path.normpath(root)) if root else ""
        if name:
            priority.setdefault(name, index)
    # roots 没给（例如直接从代码调用 upload_tree）时按老结构兜底
    for name, index in (("Videos", 0), ("Music", 1), ("作者资料", 2)):
        priority.setdefault(name, index + 100)
    return priority


class WebDAVError(Exception):
    pass


class _ProgressFile:
    """可 seek 的文件包装器：供 requests 在认证/重定向时回退重读，同时统计字节。

    不能用生成器作为 PUT body：部分 WebDAV 服务器（如在用 123pan）会返回
    HTTP 400，且 requests 无法对生成器执行 rewind。
    """

    def __init__(self, fileobj, on_bytes=None):
        self._file = fileobj
        self._on_bytes = on_bytes
        self._reported = 0

    def read(self, size=-1):
        data = self._file.read(size)
        if data:
            position = self._file.tell()
            # 用高水位统计，认证重试时重新读取不会把进度统计两遍
            if position > self._reported:
                delta = position - self._reported
                self._reported = position
                if self._on_bytes is not None:
                    try:
                        self._on_bytes(delta)
                    except Exception:  # noqa: BLE001
                        pass
        return data

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()

    def seekable(self):
        return True

    def __len__(self):
        try:
            return os.fstat(self._file.fileno()).st_size
        except (OSError, AttributeError):
            return 0

    def __getattr__(self, name):
        return getattr(self._file, name)


def _iter_file(path, on_bytes=None, chunk_size=256 * 1024):
    """按块读取文件并上报字节数，供上传进度/速度统计。"""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            if on_bytes is not None:
                try:
                    on_bytes(len(chunk))
                except Exception:  # noqa: BLE001 - 统计失败不影响上传
                    pass
            yield chunk


class WebDAVClient:
    def __init__(self, base_url, username="", password="", remote_root="", verify=True):
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise WebDAVError("WebDAV 地址为空")
        self.username = username or ""
        self.password = password or ""
        self.remote_root = (remote_root or "").strip().strip("/")
        self.verify = bool(verify)
        self._local = threading.local()
        # 所有本实例创建的 Session 都登记一份，任务结束后统一关掉连接池；
        # thread-local 属性本身没法从创建者线程枚举。
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._dir_lock = threading.Lock()
        self._created_dirs = set()
        # {目录: (失败时间, 错误信息)}：目录建不出来时后续文件快速失败，
        # 而不是每个文件都等一遍重试；过一会儿再试，服务端恢复后能自动继续。
        self._dir_failures = {}

    def _session(self, digest=False):
        attr = "digest_session" if digest else "session"
        session = getattr(self._local, attr, None)
        if session is None:
            session = requests.Session()
            if self.username:
                session.auth = (requests.auth.HTTPDigestAuth(self.username, self.password)
                                if digest else (self.username, self.password))
            session.headers.update({"User-Agent": "JingYing-PC/1.0"})
            session.verify = self.verify
            setattr(self._local, attr, session)
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self):
        """关掉本实例创建的 requests.Session 连接池。

        每次「测试连接 / 上传全部 / 开始自动上传」都会 new 一个 client，
        thread-local 的 Session 以前没有任何回收路径，socket 只能等 GC。
        必须在所有上传线程退出后调用；之后若再用会自动建新连接池。
        """
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭连接池失败不能影响退出
                pass
        for attr in ("session", "digest_session"):
            if hasattr(self._local, attr):
                try:
                    delattr(self._local, attr)
                except AttributeError:
                    pass

    def _url(self, rel_path=""):
        parts = []
        root_parts = [p for p in self.remote_root.split("/") if p] if self.remote_root else []
        rel_parts = ([p for p in rel_path.replace("\\", "/").split("/") if p]
                     if rel_path else [])
        # 本地相对路径本身就以远程根目录开头时（例如根目录叫「净影」，而作品
        # 保存在 净影/作者/作品），不要再拼一次，否则会出现 净影/净影/作者。
        if root_parts and rel_parts[:len(root_parts)] == root_parts:
            root_parts = []
        parts.extend(root_parts)
        parts.extend(rel_parts)
        encoded = "/".join(urllib.parse.quote(p, safe="") for p in parts if p)
        return self.base_url + ("/" + encoded if encoded else "")

    def _request(self, method, rel_path="", **kwargs):
        url = self._url(rel_path)
        try:
            response = self._session().request(method, url, timeout=(10, 120), **kwargs)
            if response.status_code == 401 and self.username:
                # 有些服务器使用 Digest 认证；body 是文件时需要回到开头
                data = kwargs.get("data")
                if hasattr(data, "seek"):
                    try:
                        data.seek(0)
                    except (OSError, ValueError):
                        pass
                response = self._session(digest=True).request(
                    method, url, timeout=(10, 120), **kwargs)
        except requests.RequestException as e:
            raise WebDAVError(str(e)) from e
        return response

    def _mkcol(self, rel_path):
        """创建一级目录，返回 (是否成功, 状态码, 响应片段)。

        200/201/204 是常规成功，405 表示目录已存在。其余状态一律交给调用方
        用 PROPFIND 核实：123 云盘在并发/刚建目录时会回 409，但目录其实是好的；
        而父目录真的不存在时也是 409，不能想当然当成成功。
        """
        response = self._request("MKCOL", rel_path)
        status = response.status_code
        if status in (301, 302):
            # 少数服务器要求集合路径带结尾斜杠
            response = self._request("MKCOL", rel_path + "/")
            status = response.status_code
        if status in (200, 201, 204, 405):
            return True, status, ""
        if status == 401:
            # 遇到 401 时 _request 已经用 Digest 认证重试过；仍失败说明凭据不对
            raise WebDAVError("创建目录失败：用户名或密码不正确（HTTP 401）")
        if status == 403:
            raise WebDAVError(f"创建目录失败 {rel_path}：没有写入权限（HTTP 403）")
        return False, status, (response.text or "")[:160].replace("\n", " ")

    def _dir_exists(self, rel_path):
        """PROPFIND 核实目录是否真的存在。网络异常按不存在处理，让上层重试。"""
        try:
            response = self._request(
                "PROPFIND", rel_path, headers={"Depth": "0"})
        except WebDAVError as exc:
            logging.warning("PROPFIND %s failed: %s", rel_path, exc)
            return False
        return response.status_code in (200, 207)

    def _recent_failure(self, rel_dir):
        """rel_dir 的任意一级最近创建失败时，返回那条错误（快速失败）。"""
        now = time.monotonic()
        parts = rel_dir.split("/")
        for index in range(1, len(parts) + 1):
            failed = self._dir_failures.get("/".join(parts[:index]))
            if failed and now - failed[0] < _DIR_FAIL_TTL:
                return failed[1]
        return ""

    def ensure_dir(self, rel_dir):
        """逐级创建远程目录，已存在视为成功；线程安全。

        只有「确认存在」才写进缓存。以前把 409/301 也当成创建成功，一旦服务端
        临时回 409，父目录实际没建出来，后面所有子目录和上传都会失败，而且
        本次运行再也不会重试——表现就是「上传全部失败」。
        """
        rel_dir = rel_dir.replace("\\", "/").strip("/")
        if not rel_dir:
            return
        with self._dir_lock:
            if rel_dir in self._created_dirs:
                return
            message = self._recent_failure(rel_dir)
            if message:
                raise WebDAVError(message)
            parts = [p for p in rel_dir.split("/") if p]
            current = []
            for part in parts:
                current.append(part)
                key = "/".join(current)
                if key in self._created_dirs:
                    continue
                ok, status, detail = False, 0, ""
                for attempt in range(3):
                    ok, status, detail = self._mkcol(key)
                    if ok:
                        break
                    # 409/423/5xx 往往是服务端暂时状态，稍等再试
                    if status not in (409, 423, 500, 502, 503):
                        break
                    time.sleep(0.2 * (attempt + 1))
                if not ok and self._dir_exists(key):
                    ok = True
                if not ok:
                    message = (
                        f"创建远程目录失败 {key}: HTTP {status} {detail}").strip()
                    self._dir_failures[key] = (time.monotonic(), message)
                    logging.warning(message)
                    raise WebDAVError(message)
                self._created_dirs.add(key)
                self._dir_failures.pop(key, None)

    def ensure_root(self):
        """确认远程根目录可用；手动「上传全部」开始前调用，先给出明确错误。"""
        if self.remote_root:
            self.ensure_dir(self.remote_root)

    def upload_file(self, local_path, rel_path, on_bytes=None):
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        parent = "/".join(rel_path.split("/")[:-1])
        try:
            size = os.path.getsize(local_path)
        except OSError:
            size = 0
        last = None
        for attempt in range(2):
            if parent:
                self.ensure_dir(parent)
            with open(local_path, "rb") as handle:
                body = _ProgressFile(handle, on_bytes)
                response = self._request(
                    "PUT", rel_path, data=body,
                    headers={"Content-Length": str(size), "Overwrite": "T"})
            if response.status_code in (200, 201, 204):
                return
            detail = (response.text or "")[:160].replace("\n", " ")
            last = WebDAVError(
                f"上传失败 {rel_path}: HTTP {response.status_code} {detail}")
            # 409 多半是父目录刚被删/缓存过期：清掉缓存，重建后再传一次
            if response.status_code != 409 or not parent or attempt:
                break
            with self._dir_lock:
                self._created_dirs = {
                    key for key in self._created_dirs
                    if not (key == parent or parent.startswith(key + "/"))}
                for key in list(self._dir_failures):
                    if key == parent or parent.startswith(key + "/"):
                        self._dir_failures.pop(key, None)
        raise last

    # ---------- 读取（用于核对云端已有文件的画质） ----------
    def stat(self, rel_path):
        """返回 (是否存在, 字节数)。优先 HEAD，服务端不支持时退回 PROPFIND。"""
        rel_path = rel_path.replace("\\", "/").strip("/")
        # HEAD 只回头部，比 PROPFIND 的 XML 便宜；个别 WebDAV 网关不支持 HEAD，
        # 会回 405/501/403，这时再走 PROPFIND。
        response = self._request("HEAD", rel_path)
        head_ok = response.status_code in (200, 204)
        if head_ok:
            length = response.headers.get("Content-Length")
            try:
                size = int(length) if length is not None else 0
            except (TypeError, ValueError):
                size = 0
            if size > 0:
                return (True, size)
            # HEAD 没带 Content-Length（部分网关会这样）：继续用 PROPFIND
            # 取 getcontentlength，别把「读不到大小」当成「读不到画质」。
        if response.status_code in (404, 410):
            return (False, 0)
        response = self._request("PROPFIND", rel_path, headers={"Depth": "0"})
        if response.status_code == 404:
            return (head_ok, 0)
        if response.status_code not in (200, 207):
            detail = (response.text or "")[:120].replace("\n", " ")
            raise WebDAVError(
                f"读取远端文件信息失败 {rel_path}: HTTP {response.status_code} {detail}")
        text = response.text or ""
        # 命名空间前缀不固定（d: / D: / lp1:），统一按「标签名结尾」匹配
        match = re.search(
            r"<(?:[\w.-]+:)?getcontentlength[^>]*>\s*(\d+)\s*<", text, re.I)
        return (True, int(match.group(1)) if match else 0)

    def fetch_range(self, rel_path, start, end):
        """按字节区间读取（闭区间），用于只取 MP4 的 moov/tkhd。"""
        start, end = max(0, int(start)), max(0, int(end))
        headers = {"Range": f"bytes={start}-{end}"}
        # stream=True：服务器不支持 Range 而回整个文件时，只读到 end 就断开，
        # 不会先把几百 MB 的视频整个吃进内存再切一小块。
        response = self._request("GET", rel_path, headers=headers, stream=True)
        try:
            if response.status_code == 206:
                return response.content or b""
            if response.status_code in (200, 203):
                limit = 32 * 1024 * 1024
                if end + 1 > limit:
                    raise WebDAVError(
                        f"服务器不支持 Range，且所需区间超过 {limit} 字节：{rel_path}")
                chunks = []
                remaining = end + 1
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    take = chunk[:remaining]
                    chunks.append(take)
                    remaining -= len(take)
                    if remaining <= 0:
                        break
                return b"".join(chunks)[start:end + 1]
            if response.status_code == 404:
                raise WebDAVError(f"远端文件不存在：{rel_path}")
            raise WebDAVError(
                f"读取远端文件失败 {rel_path}: HTTP {response.status_code}")
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def download_to(self, rel_path, target_path):
        """把远端文件流式下载到本地路径（tar 包探测画质时用）。"""
        response = self._request("GET", rel_path, stream=True)
        try:
            if response.status_code not in (200, 206):
                raise WebDAVError(
                    f"下载远端文件失败 {rel_path}: HTTP {response.status_code}")
            with open(target_path, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if chunk:
                        handle.write(chunk)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def test(self):
        """测试连接，并确认远程根目录真的可写。"""
        response = self._request("PROPFIND", "", headers={"Depth": "0"})
        if response.status_code in (200, 207, 301, 302):
            return True
        if response.status_code == 404 and self.remote_root:
            # 远程根目录还没建：尝试建一次，成功即视为可用
            self.ensure_dir(self.remote_root)
            return True
        response = self._request("OPTIONS", "")
        if response.status_code in (200, 204, 207):
            if self.remote_root:
                self.ensure_dir(self.remote_root)
            return True
        raise WebDAVError(f"连接失败：HTTP {response.status_code}")

    def upload_tree(self, local_root, progress=None, state=None, save_state=None,
                    max_workers=3, on_start=None, on_done=None, on_total=None,
                    on_bytes=None, on_progress=None, on_file_progress=None,
                    state_lock=None, rel_map=None, roots=None):
        """并行上传 local_root 下全部文件。

        返回 (成功数, 跳过数, 失败列表)。
        state: {相对路径: "大小:修改时间"}，已上传且未变化的文件跳过。
        roots: 只扫描这些目录（相对路径仍按 local_root 计算）。开发版 local_root
            是用户主目录，不限定范围会把桌面、文档、AppData 全部上传。
        on_start(rel, size) / on_done(rel, status) 在线程里调用。
        on_bytes(n)：每上传 n 字节回调（用于速度统计）。
        on_progress(done_bytes, total_bytes)：总进度回调。
        on_file_progress(rel, done_bytes, total_bytes)：单个文件的进度。
        status: done / skipped / failed。
        """
        files = []
        seen = set()
        scan_roots = [local_root]
        if roots is not None:
            base_abs = os.path.abspath(local_root)
            scan_roots = []
            for root in roots:
                if not root or not os.path.isdir(root):
                    continue
                try:
                    inside = os.path.commonpath(
                        [base_abs, os.path.abspath(root)]) == base_abs
                except ValueError:
                    inside = False
                if inside:
                    scan_roots.append(root)
        for scan_root in scan_roots:
            for base, _dirs, names in os.walk(scan_root):
                for name in names:
                    path = os.path.join(base, name)
                    rel = os.path.relpath(path, local_root).replace("\\", "/")
                    if rel.startswith("../") or rel == "..":
                        continue
                    if rel_map is not None:
                        rel = rel_map(rel)
                    if rel in seen:
                        continue
                    seen.add(rel)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    files.append((path, rel, size))
        priority = _upload_priority(roots)
        files.sort(key=lambda item: (
            priority.get(item[1].split("/", 1)[0], 999), item[1]))
        total = len(files)
        total_bytes = sum(item[2] for item in files)
        if on_total:
            on_total(total)
        if on_progress:
            on_progress(0, total_bytes)
        lock = threading.Lock()
        # state 可能被多个上传线程同时读写；保存状态时必须和修改使用同一把锁。
        state_lock = state_lock or lock
        counters = {"ok": 0, "skipped": 0, "done": 0}
        failed = []
        done_bytes = 0

        def advance(count, real=True):
            nonlocal done_bytes
            try:
                count = max(0, int(count or 0))
            except (TypeError, ValueError):
                return
            if not count:
                return
            with lock:
                done_bytes = min(total_bytes, done_bytes + count)
                current = done_bytes
            if on_progress:
                on_progress(current, total_bytes)
            if real and on_bytes is not None:
                try:
                    on_bytes(count)
                except Exception:  # noqa: BLE001
                    pass

        def handle(path, rel, size):
            with lock:
                counters["done"] += 1
                index = counters["done"]
            if progress:
                progress(f"正在上传 {index}/{total}：{rel}")
            try:
                fingerprint = f"{os.path.getsize(path)}:{int(os.path.getmtime(path))}"
            except OSError:
                fingerprint = ""
            with state_lock:
                already = bool(state is not None and fingerprint
                               and state.get(rel) == fingerprint)
            if already:
                with lock:
                    counters["skipped"] += 1
                advance(size, real=False)
                if on_done:
                    on_done(rel, "skipped")
                return
            if on_start:
                on_start(rel, size)
            uploaded = [0]

            def file_bytes(count):
                uploaded[0] += count
                advance(count, real=True)
                if on_file_progress is not None:
                    try:
                        on_file_progress(rel, uploaded[0], size)
                    except Exception:  # noqa: BLE001
                        pass

            try:
                last_error = None
                for attempt in range(2):
                    try:
                        self.upload_file(path, rel, on_bytes=file_bytes)
                        last_error = None
                        break
                    except Exception as e:  # noqa: BLE001
                        last_error = e
                        if attempt == 0:
                            time.sleep(1.0)
                if last_error is not None:
                    raise last_error
                advance(max(0, size - uploaded[0]), real=False)
                if on_file_progress is not None:
                    try:
                        on_file_progress(rel, size, size)
                    except Exception:  # noqa: BLE001
                        pass
                with lock:
                    counters["ok"] += 1
                with state_lock:
                    if state is not None and fingerprint:
                        state[rel] = fingerprint
                if state is not None and fingerprint and save_state:
                    try:
                        save_state()
                    except Exception as exc:  # noqa: BLE001
                        # 文件已经上传成功；状态落盘失败不能算上传失败。
                        import logging
                        logging.warning("WebDAV state save failed: %s", exc)
                if on_done:
                    on_done(rel, "done")
            except Exception as e:  # noqa: BLE001
                advance(max(0, size - uploaded[0]), real=False)
                if on_file_progress is not None:
                    try:
                        on_file_progress(rel, uploaded[0], size)
                    except Exception:  # noqa: BLE001
                        pass
                import logging
                logging.warning("WebDAV upload failed %s: %s", rel, e)
                with lock:
                    failed.append((rel, str(e)))
                if on_done:
                    on_done(rel, "failed")

        workers = max(1, min(8, int(max_workers or 1)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for path, rel, size in files:
                executor.submit(handle, path, rel, size)
        return counters["ok"], counters["skipped"], failed

