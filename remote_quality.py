# -*- coding: utf-8 -*-
"""查询 WebDAV 上已有作品的实际分辨率。

通讯录同步时，云端备份过、本地已经删掉的作品以前一律跳过。老备份可能是
720p/540p，而这次解析拿到了 4K，跳过就等于永远停在低画质。这个模块负责
在下载前先问一句「云端那份到底是多大」，只有确实更低时才重下。

读取方式尽量省流量：
- `.mp4` 只按 Range 取 `moov` 周围的几十 KB；
- `.tar` 作品包只能整包取（tar 不压缩，但成员偏移要顺序读），取完立即删除；
- 结果按（相对路径, 文件大小）缓存，文件没变就不再重复探测。
"""
import json
import logging
import os
import tempfile
import threading
import time

import media_probe

# 探测结果的缓存文件放在 data/ 下，随程序走（便携版会跟着 JingYingData）
_CACHE_NAME = "remote_quality.json"


class RemoteQualityProbe:
    """用 WebDAVClient 读远端文件分辨率，带磁盘缓存。"""

    def __init__(self, client, cache_path="", tmp_dir="", max_tar_bytes=512 * 1024 * 1024,
                 cache_ttl=7 * 24 * 3600, namespace=""):
        self.client = client
        self.cache_path = cache_path or ""
        self.tmp_dir = tmp_dir or tempfile.gettempdir()
        self.max_tar_bytes = int(max_tar_bytes)
        # 一份缓存文件可能被多个 WebDAV 服务器共用：同名相对路径在 A 服务器是
        # 4K、在 B 服务器可能只是 720p。把「服务器 + 远程根目录」编进 key，
        # 换服务器后旧记录自然不命中，不会拿错服务器的分辨率去跳过下载。
        self.namespace = str(namespace or "")
        # 缓存有效期内不再访问服务器：否则每轮同步都要给每个老作品发一次
        # PROPFIND，几千个作品就是几千次请求，同步会明显变慢。
        self.cache_ttl = int(cache_ttl)
        self._lock = threading.Lock()
        # 写盘单独一把锁：tmp 按线程区分还不够，Windows 上多个线程同时
        # os.replace 到同一个目标文件会 [WinError 5] 拒绝访问。
        self._write_lock = threading.Lock()
        self._cache = None           # {rel: {"size": int, "w": int, "h": int}}
        self._dirty = False
        self._last_save = 0.0

    # ---------- 缓存 ----------
    def _key(self, rel):
        return f"{self.namespace}|{rel}" if self.namespace else rel

    def _load_cache(self):
        # 同步时多个下载线程会同时走 probe()：以前这里的「检查 None → 读文件 →
        # 赋值」没有锁，两个线程可能各自加载一份，后写的覆盖先写的，刚记住的
        # 分辨率凭空丢失，下一轮又去发一次 PROPFIND。
        with self._lock:
            if self._cache is not None:
                return self._cache
            files = {}
            if self.cache_path:
                try:
                    with open(self.cache_path, encoding="utf-8") as handle:
                        data = json.load(handle)
                    if isinstance(data, dict) and isinstance(data.get("files"), dict):
                        files = {str(k): v for k, v in data["files"].items()
                                 if isinstance(v, dict)}
                except (OSError, ValueError):
                    files = {}
            self._cache = files
            return files

    def save(self, force=False):
        """落盘；默认 30 秒节流，避免一次同步反复重写整份缓存。"""
        with self._lock:
            if not self._dirty or not self.cache_path:
                return
            now = time.monotonic()
            if not force and now - self._last_save < 30.0:
                return
            self._dirty = False
            self._last_save = now
        # 串行化整段落盘：临时文件按线程区分只能避免互相覆盖 .tmp，
        # 目标文件仍是同一个，并发 os.replace 在 Windows 上会失败。
        # 拿锁后再取一次快照，保证最后写入的是最新全量而不是过期副本。
        with self._write_lock:
            with self._lock:
                files = dict(self._cache or {})
            tmp = f"{self.cache_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump({"files": files}, handle, ensure_ascii=False)
                os.replace(tmp, self.cache_path)
            except OSError as exc:
                logging.warning("save remote quality cache failed: %s", exc)
                with self._lock:
                    self._dirty = True
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def _cached(self, rel, size):
        files = self._load_cache()
        entry = files.get(self._key(rel))
        if not isinstance(entry, dict):
            return None
        if int(entry.get("size") or -1) != int(size):
            return None
        width, height = int(entry.get("w") or 0), int(entry.get("h") or 0)
        if not width or not height:
            return None
        return width, height

    def _cached_fresh(self, rel):
        """缓存有效期内直接返回，连 stat 都省掉（同步时每个作品都会走这里）。"""
        entry = self._load_cache().get(self._key(rel))
        if not isinstance(entry, dict):
            return None
        stamp = int(entry.get("ts") or 0)
        if self.cache_ttl > 0 and stamp and time.time() - stamp > self.cache_ttl:
            return None
        width, height = int(entry.get("w") or 0), int(entry.get("h") or 0)
        if not width or not height:
            return None
        return width, height, int(entry.get("size") or 0)

    def _remember(self, rel, size, width, height):
        # _load_cache 自己会拿锁，不能在 self._lock 里再调（普通 Lock 不可重入）。
        files = self._load_cache()
        with self._lock:
            files[self._key(rel)] = {"size": int(size), "w": int(width),
                                     "h": int(height), "ts": int(time.time())}
            self._dirty = True

    # ---------- 探测 ----------
    def probe(self, rel_path):
        """返回 (宽, 高, 文件字节数)；远端没有或读不出来时返回 None。"""
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        if not rel or self.client is None:
            return None
        fresh = self._cached_fresh(rel)
        if fresh:
            return fresh
        try:
            exists, size = self.client.stat(rel)
        except Exception as exc:  # noqa: BLE001 - 探测失败不能影响下载
            logging.warning("remote stat failed %s: %s", rel, exc)
            return None
        if not exists:
            return None
        cached = self._cached(rel, size)
        if cached:
            # 字节数没变 → 还是同一个文件，分辨率直接复用。但要把「核对时间」刷新：
            # 否则这份记录过了 TTL 就永远显示为过期，之后每轮同步都要给这个作品
            # 发一次 stat。刷新后 TTL 的含义才是「最多每 7 天重问一次」。
            self._remember(rel, size, cached[0], cached[1])
            self.save()
            return cached[0], cached[1], size

        width = height = 0
        if rel.lower().endswith(".tar"):
            width, height = self._probe_tar(rel, size)
        else:
            width, height = self._probe_mp4(rel, size)
        if not width or not height:
            logging.info("remote quality unknown %s (size=%s)", rel, size)
            return None
        self._remember(rel, size, width, height)
        self.save()
        return width, height, size

    def _probe_mp4(self, rel, size):
        if size <= 0:
            return (0, 0)

        def fetch(start, end):
            return self.client.fetch_range(rel, start, end)

        try:
            reader = media_probe.RangeReader(fetch, size)
            return media_probe.probe_mp4(reader, size)
        except Exception as exc:  # noqa: BLE001
            logging.warning("remote range probe failed %s: %s", rel, exc)
            return (0, 0)

    def _probe_tar(self, rel, size):
        if size <= 0 or size > self.max_tar_bytes:
            return (0, 0)
        handle = None
        path = ""
        try:
            os.makedirs(self.tmp_dir, exist_ok=True)
            handle, path = tempfile.mkstemp(
                prefix="jy-probe-", suffix=".tar", dir=self.tmp_dir)
            os.close(handle)
            handle = None
            self.client.download_to(rel, path)
            return media_probe.probe_tar(path)
        except Exception as exc:  # noqa: BLE001
            logging.warning("remote tar probe failed %s: %s", rel, exc)
            return (0, 0)
        finally:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
