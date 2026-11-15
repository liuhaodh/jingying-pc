# -*- coding: utf-8 -*-
"""本地持久化：通讯录（作者列表，JSON）与最近保存历史（SQLite）。

性能说明：
- 历史记录用 SQLite（`history.db`）增量写入：新增作品只 INSERT，作者改名只 UPDATE
  受影响的少量行，不再把几万条历史整份重写成 `history.json`（这样一次要 1 秒多）。
- 历史在内存里保留一份缓存，`load_history()` 直接返回缓存副本；SQLite 负责落盘。
- 第一次启动会自动把旧的 `history.json` 导入 SQLite，并把旧文件改名为
  `history.json.migrated`，不会重复导入。
- 作者列表仍然用 JSON（数据量小），所有读写同一把可重入锁，GUI 线程与下载线程
  可以同时访问。
"""
import json
import os
import sqlite3
import threading
import time

import config


class Author:
    def __init__(self, sec_uid="", nickname="", unique_id="", avatar_url=""):
        self.sec_uid = sec_uid
        self.nickname = nickname
        self.unique_id = unique_id
        self.avatar_url = avatar_url
        self.signature = ""
        self.avatar_local = ""
        self.avatar_hash = ""
        self.avatar_history: list[str] = []
        self.signature_history: list[dict] = []
        self.added_at = 0
        self.last_sync_at = 0
        self.saved_ids: list[str] = []

    def to_dict(self):
        return {
            "sec_uid": self.sec_uid,
            "nickname": self.nickname,
            "unique_id": self.unique_id,
            "avatar_url": self.avatar_url,
            "signature": self.signature,
            "avatar_local": self.avatar_local,
            "avatar_hash": self.avatar_hash,
            "avatar_history": list(self.avatar_history),
            "signature_history": list(self.signature_history),
            "added_at": self.added_at,
            "last_sync_at": self.last_sync_at,
            "saved_ids": list(self.saved_ids),
        }

    @staticmethod
    def from_dict(data: dict):
        author = Author(
            data.get("sec_uid", ""),
            data.get("nickname", ""),
            data.get("unique_id", ""),
            data.get("avatar_url", ""),
        )
        author.signature = str(data.get("signature", ""))
        author.avatar_local = str(data.get("avatar_local", ""))
        author.avatar_hash = str(data.get("avatar_hash", ""))
        author.avatar_history = [str(x) for x in (data.get("avatar_history") or [])]
        author.signature_history = [x for x in (data.get("signature_history") or [])
                                    if isinstance(x, dict)]
        author.added_at = int(data.get("added_at", 0) or 0)
        author.last_sync_at = int(data.get("last_sync_at", 0) or 0)
        author.saved_ids = [str(x) for x in (data.get("saved_ids") or [])]
        return author

    def is_saved(self, aweme_id):
        return aweme_id in self.saved_ids

    def label(self):
        name = self.nickname or "作者"
        if self.unique_id:
            return f"{name}·{self.unique_id}"
        return name


class HistoryItem:
    def __init__(self):
        self.aweme_id = ""
        self.title = ""
        self.author = ""
        self.author_sec_uid = ""
        self.unique_id = ""
        self.cover = ""
        self.video_path = ""
        self.music_path = ""
        self.album_paths: list[str] = []
        self.album_videos: list[str] = []
        self.share_url = ""
        self.is_album = False
        self.create_time = 0
        self.location = ""
        self.ip_location = ""
        self.saved_at = 0
        # SQLite 里的行号（seq），只用于按行更新，不参与序列化
        self._db_seq = None

    def to_dict(self):
        return {
            "aweme_id": self.aweme_id,
            "title": self.title,
            "author": self.author,
            "author_sec_uid": self.author_sec_uid,
            "unique_id": self.unique_id,
            "cover": self.cover,
            "video_path": self.video_path,
            "music_path": self.music_path,
            "album_paths": list(self.album_paths),
            "album_videos": list(self.album_videos),
            "share_url": self.share_url,
            "is_album": self.is_album,
            "create_time": self.create_time,
            "location": self.location,
            "ip_location": self.ip_location,
            "saved_at": self.saved_at,
        }

    @staticmethod
    def from_dict(data: dict):
        item = HistoryItem()
        item.aweme_id = str(data.get("aweme_id", ""))
        item.title = str(data.get("title", ""))
        item.author = str(data.get("author", ""))
        item.author_sec_uid = str(data.get("author_sec_uid", ""))
        item.unique_id = str(data.get("unique_id", ""))
        item.cover = str(data.get("cover", ""))
        item.video_path = str(data.get("video_path", ""))
        item.music_path = str(data.get("music_path", ""))
        item.album_paths = [str(x) for x in (data.get("album_paths") or [])]
        item.album_videos = [str(x) for x in (data.get("album_videos") or [])]
        item.share_url = str(data.get("share_url", ""))
        item.is_album = bool(data.get("is_album", False))
        item.create_time = int(data.get("create_time", 0) or 0)
        item.location = str(data.get("location", ""))
        item.ip_location = str(data.get("ip_location", ""))
        item.saved_at = int(data.get("saved_at", 0) or 0)
        return item


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_text(path, text):
    """原子写入（内容已序列化）：先写临时文件再替换，避免中断留下半个文件。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _dump_json(data):
    """紧凑 JSON：程序只读不靠人工编辑，连空格都不要。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class _BackgroundWriter:
    """把 authors.json 的落盘交给后台线程。

    同步/下载把磁盘占满时，主线程同步写约 700KB 的 authors.json 实测会冻结
    3～4 秒（看门狗记录到 ``UI stall 3.8s``，而且是每 60 秒一次）。序列化留在
    调用线程（约 14ms），真正的 open/写入/替换放到后台；同一文件连续提交时
    只保留最新一份，不会排队堆积。
    """

    def __init__(self, name="jy-json-writer"):
        self._cond = threading.Condition()
        self._pending = {}      # path -> text（每个文件只留最新）
        self._inflight = 0
        self._closed = False
        self._thread = None
        self._name = name

    def submit(self, path, text):
        if self._closed:
            return
        with self._cond:
            self._pending[path] = text
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop,
                                                name=self._name, daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _loop(self):
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if not self._pending:
                    return
                path, text = self._pending.popitem()
                self._inflight += 1
            try:
                _write_text(path, text)
            except Exception:  # noqa: BLE001 - 写盘失败不能让后台线程死掉
                pass
            finally:
                with self._cond:
                    self._inflight -= 1
                    self._cond.notify_all()

    def wait(self, timeout=10.0):
        """等待已提交的写入全部完成，返回是否等到了。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._pending or self._inflight:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def close(self, timeout=10.0):
        self.wait(timeout)
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, timeout))


# ---------- 历史（SQLite） ----------
# 列顺序即 _row_values 的顺序；seq 是自增主键，越大越新。
HISTORY_FIELDS = (
    "aweme_id", "title", "author", "author_sec_uid", "unique_id", "cover",
    "video_path", "music_path", "album_paths", "album_videos", "share_url",
    "is_album", "create_time", "location", "ip_location", "saved_at",
)
_INSERT_SQL = (
    f"INSERT OR REPLACE INTO history ({','.join(HISTORY_FIELDS)}) "
    f"VALUES ({','.join('?' * len(HISTORY_FIELDS))})"
)
_UPDATE_SQL = (
    f"UPDATE history SET {','.join(f'{name}=?' for name in HISTORY_FIELDS)} "
    "WHERE seq=?"
)
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    author_sec_uid TEXT NOT NULL DEFAULT '',
    unique_id TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    video_path TEXT NOT NULL DEFAULT '',
    music_path TEXT NOT NULL DEFAULT '',
    album_paths TEXT NOT NULL DEFAULT '[]',
    album_videos TEXT NOT NULL DEFAULT '[]',
    share_url TEXT NOT NULL DEFAULT '',
    is_album INTEGER NOT NULL DEFAULT 0,
    create_time INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL DEFAULT '',
    ip_location TEXT NOT NULL DEFAULT '',
    saved_at INTEGER NOT NULL DEFAULT 0
)
"""
# 非空 aweme_id 才去重；空 id 的记录允许多条
_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_aweme "
    "ON history(aweme_id) WHERE aweme_id <> ''"
)


def _row_values(item):
    return (
        item.aweme_id or "",
        item.title or "",
        item.author or "",
        item.author_sec_uid or "",
        item.unique_id or "",
        item.cover or "",
        item.video_path or "",
        item.music_path or "",
        json.dumps(list(item.album_paths or []), ensure_ascii=False),
        json.dumps(list(item.album_videos or []), ensure_ascii=False),
        item.share_url or "",
        1 if item.is_album else 0,
        int(item.create_time or 0),
        item.location or "",
        item.ip_location or "",
        int(item.saved_at or 0),
    )


def _item_from_row(row):
    item = HistoryItem()
    item.aweme_id = row["aweme_id"] or ""
    item.title = row["title"] or ""
    item.author = row["author"] or ""
    item.author_sec_uid = row["author_sec_uid"] or ""
    item.unique_id = row["unique_id"] or ""
    item.cover = row["cover"] or ""
    item.video_path = row["video_path"] or ""
    item.music_path = row["music_path"] or ""
    try:
        item.album_paths = [str(x) for x in json.loads(row["album_paths"] or "[]")]
    except (ValueError, TypeError):
        item.album_paths = []
    try:
        item.album_videos = [str(x) for x in json.loads(row["album_videos"] or "[]")]
    except (ValueError, TypeError):
        item.album_videos = []
    item.share_url = row["share_url"] or ""
    item.is_album = bool(row["is_album"])
    item.create_time = int(row["create_time"] or 0)
    item.location = row["location"] or ""
    item.ip_location = row["ip_location"] or ""
    item.saved_at = int(row["saved_at"] or 0)
    item._db_seq = row["seq"]
    return item


def _quarantine_db(path):
    """数据库损坏时挪到 .corrupt，让程序能从空历史继续跑。"""
    for suffix in ("", "-wal", "-shm"):
        src = path + suffix
        if os.path.exists(src):
            try:
                os.replace(src, src + ".corrupt")
            except OSError:
                try:
                    os.remove(src)
                except OSError:
                    pass


class Store:
    # None = 不限制历史条数；旧版 10000 上限会把更早的已下载作品从
    # “观看已下载”里挤掉，即使文件仍在磁盘上。
    MAX_HISTORY = None

    def _limit_history(self, items):
        items = list(items)
        if self.MAX_HISTORY is None:
            return items
        return items[:max(0, int(self.MAX_HISTORY))]

    def __init__(self):
        config.ensure_dirs()
        self._lock = threading.RLock()
        self._history = None
        self._authors = None
        self._history_index = {}
        self._authors_dirty = False
        self._conn = None
        self._writer = _BackgroundWriter()

    def close(self):
        """关闭 SQLite 连接并等 authors.json 写完（测试/退出时用）。"""
        writer = getattr(self, "_writer", None)
        if writer is not None:
            try:
                if self._authors_dirty:
                    # 退出时后台线程可能刚被 terminate() 停在锁里。以前这里用
                    # `with self._lock` 无上限地等，主线程就永久卡住、进程残留；
                    # 现在拿不到锁就放弃这次落盘（authors.json 上一份快照仍在）。
                    if self._lock.acquire(timeout=2.0):
                        try:
                            self._flush_authors()
                        finally:
                            self._lock.release()
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        conn = getattr(self, "_conn", None)
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 内部 ----------
    def _reindex_history(self):
        """建立 aweme_id -> HistoryItem 索引，供 add_history 做 O(1) 去重。"""
        self._history_index = {}
        for item in self._history or []:
            if item.aweme_id:
                self._history_index.setdefault(item.aweme_id, item)

    def _ensure_db(self):
        if self._conn is not None:
            return self._conn
        path = config.HISTORY_DB
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = self._open_db(path)
        self._migrate_history_json(self._conn)
        return self._conn

    @staticmethod
    def _open_db(path):
        def connect():
            conn = sqlite3.connect(path, check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                # WAL + NORMAL：每次 INSERT 都落盘，但不必每个作品都 fsync
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute(_CREATE_SQL)
                conn.execute(_INDEX_SQL)
                return conn
            except Exception:
                # Windows 上不关连接就无法改名/删除损坏的库
                conn.close()
                raise

        try:
            return connect()
        except sqlite3.DatabaseError:
            # 数据库损坏：挪走重建，历史从空开始（与旧 JSON 损坏时行为一致）
            _quarantine_db(path)
            return connect()

    def _migrate_history_json(self, conn):
        """第一次启动把旧 history.json 导入 SQLite，然后改名避免重复导入。"""
        path = config.HISTORY_FILE
        if not os.path.exists(path):
            return
        try:
            if conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]:
                return
        except sqlite3.DatabaseError:
            return
        data = _read_json(path, [])
        if not isinstance(data, list) or not data:
            return
        rows = []
        # history.json 是「新→旧」，倒序插入让最新记录拿到最大 seq
        for entry in reversed(data):
            if not isinstance(entry, dict):
                continue
            if not (entry.get("aweme_id") or entry.get("video_path")
                    or entry.get("album_paths")):
                continue
            rows.append(_row_values(HistoryItem.from_dict(entry)))
        if rows:
            conn.executemany(_INSERT_SQL, rows)
            conn.commit()
            # 大事务会把 WAL 撑到和库差不多大；迁移后立刻 checkpoint 回主库
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
        try:
            os.replace(path, path + ".migrated")
        except OSError:
            pass

    def _history_items(self):
        if self._history is None:
            conn = self._ensure_db()
            try:
                rows = conn.execute(
                    "SELECT * FROM history ORDER BY seq DESC").fetchall()
            except sqlite3.DatabaseError:
                rows = []
            self._history = [_item_from_row(row) for row in rows]
            self._reindex_history()
        return self._history

    def _author_items(self):
        if self._authors is None:
            data = _read_json(config.AUTHORS_FILE, [])
            authors = []
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and entry.get("sec_uid"):
                        authors.append(Author.from_dict(entry))
            self._authors = authors
        return self._authors

    def _replace_all_db(self):
        conn = self._ensure_db()
        # 缓存是「新→旧」，倒序插入让最新记录拿到最大 seq
        rows = [_row_values(x) for x in reversed(self._history or [])]
        try:
            conn.execute("DELETE FROM history")
            if rows:
                conn.executemany(_INSERT_SQL, rows)
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            return
        self._backfill_seqs(conn)

    def _backfill_seqs(self, conn):
        """全量重写后回填每条记录的 seq，供后续按行更新。"""
        try:
            pairs = conn.execute(
                "SELECT seq, aweme_id FROM history ORDER BY seq ASC").fetchall()
        except sqlite3.DatabaseError:
            return
        seq_map = {}
        blank_seqs = []
        for seq, aweme_id in pairs:
            if aweme_id:
                seq_map[aweme_id] = seq
            else:
                blank_seqs.append(seq)
        # 空 aweme_id 的记录是允许存在多条的（唯一索引带 WHERE aweme_id<>''）；
        # 以前它们全被映射到同一个 seq，后面的 update_history_items 会把
        # 多条记录反复写到同一行：最后一条覆盖前面全部，其余条目库里的值永远
        # 停留在旧值（作者改名后路径没落库，下次启动就提示“文件已被移动”）。
        # 这类记录按库里的先后顺序一一对应（历史缓存是「新→旧」）。
        blank_iter = iter(reversed(blank_seqs))
        for item in self._history or []:
            if item.aweme_id:
                item._db_seq = seq_map.get(item.aweme_id)
            else:
                item._db_seq = next(blank_iter, None)

    def _trim_db(self, conn):
        """MAX_HISTORY 有上限时，删掉内存和数据库里最旧的记录。"""
        if self.MAX_HISTORY is None:
            return
        keep = max(0, int(self.MAX_HISTORY))
        dropped = (self._history or [])[keep:]
        if dropped:
            for item in dropped:
                if item.aweme_id:
                    self._history_index.pop(item.aweme_id, None)
            del self._history[keep:]
        try:
            conn.execute(
                "DELETE FROM history WHERE seq NOT IN "
                "(SELECT seq FROM history ORDER BY seq DESC LIMIT ?)", (keep,))
        except sqlite3.DatabaseError:
            pass

    def _flush_authors(self, block=False):
        """写作者资料。

        block=False（默认）交给后台线程：同步期间磁盘被下载/上传占满时，
        主线程同步写 700KB 会冻结 3～4 秒，而这是每 60 秒触发一次的。
        block=True 立即同步写好（用户手动增删作者、程序退出时用）。
        """
        text = _dump_json([a.to_dict() for a in (self._authors or [])])
        self._authors_dirty = False
        if block:
            # 先等后台把旧内容写完，避免旧快照后落盘把新内容盖掉
            self._writer.wait(10.0)
            _write_text(config.AUTHORS_FILE, text)
        else:
            self._writer.submit(config.AUTHORS_FILE, text)

    def flush(self, block=False):
        """历史已增量写入 SQLite；这里只把作者资料落盘。

        返回 True 表示已确认写入磁盘（block=True 时），否则只是排进后台队列。
        """
        with self._lock:
            if self._authors_dirty:
                self._flush_authors(block=block)
                return block
            if block:
                return self._writer.wait(10.0)
            return False

    # ---------- 通讯录 ----------
    def load_authors(self):
        with self._lock:
            return list(self._author_items())

    def save_authors(self, authors):
        with self._lock:
            self._authors = list(authors)
            self._flush_authors(block=True)

    def add_author(self, author: Author, flush: bool = True):
        """写入一位作者。flush=False 时只标脏，等 store.flush() 统一落盘。

        同步期间每抓到一个作者就重写一次 authors.json 会在主线程卡顿，
        所以同步流程传 flush=False。
        """
        with self._lock:
            authors = self._author_items()
            for i, existing in enumerate(authors):
                if existing.sec_uid == author.sec_uid:
                    if existing is not author:
                        saved = list(existing.saved_ids)
                        author.saved_ids = [x for x in author.saved_ids if x not in saved] + saved
                        author.added_at = existing.added_at or int(time.time() * 1000)
                    authors[i] = author
                    if flush:
                        self._flush_authors(block=True)
                    else:
                        self._authors_dirty = True
                    return
            if not author.added_at:
                author.added_at = int(time.time() * 1000)
            authors.insert(0, author)
            if flush:
                self._flush_authors(block=True)
            else:
                self._authors_dirty = True

    def remove_author(self, sec_uid):
        with self._lock:
            self._authors = [a for a in self._author_items() if a.sec_uid != sec_uid]
            self._flush_authors(block=True)

    def authors_not_in(self, sec_uids):
        """返回不在给定 sec_uid 集合里的作者（供「按 txt 对齐通讯录」预览确认）。

        保持通讯录原顺序，只读、不修改任何数据。
        """
        keep = set(sec_uids)
        with self._lock:
            return [a for a in self._author_items() if a.sec_uid not in keep]

    def remove_authors(self, sec_uids):
        """批量移除作者：一次过滤、一次落盘，返回实际移除数量。

        逐个调 remove_author 会为每位作者都重写一遍 authors.json，几百位作者
        时主线程会明显卡住。这里只落盘一次。
        """
        targets = set(sec_uids)
        if not targets:
            return 0
        with self._lock:
            authors = self._author_items()
            before = len(authors)
            self._authors = [a for a in authors if a.sec_uid not in targets]
            removed = before - len(self._authors)
            if removed:
                self._flush_authors(block=True)
            return removed

    def mark_saved(self, author: Author, aweme_id):
        with self._lock:
            for existing in self._author_items():
                if existing.sec_uid == author.sec_uid:
                    if aweme_id not in existing.saved_ids:
                        existing.saved_ids.append(aweme_id)
                    existing.last_sync_at = int(time.time() * 1000)
                    # 下载是并发的：每个作品都重写 authors.json 会让主线程频繁卡几秒。
                    # 只标脏，由每 500 个作品的批量 flush / 同步结束统一落盘。
                    self._authors_dirty = True
                    return

    # ---------- 历史 ----------
    def load_history(self):
        with self._lock:
            return list(self._history_items())

    def add_history(self, item: HistoryItem, flush=True):
        """追加一条历史，直接写入 SQLite（flush 参数保留兼容，历史不再延迟落盘）。

        去重走 aweme_id 索引：同一个作品再次保存会移动到最前面（seq 变最大）。
        """
        with self._lock:
            items = self._history_items()
            if item.aweme_id:
                existing = self._history_index.pop(item.aweme_id, None)
                if existing is not None and existing is not item:
                    try:
                        items.remove(existing)
                    except ValueError:  # 索引与列表不同步时忽略
                        pass
                self._history_index[item.aweme_id] = item
            items.insert(0, item)
            conn = self._ensure_db()
            try:
                cursor = conn.execute(_INSERT_SQL, _row_values(item))
                item._db_seq = cursor.lastrowid
                self._trim_db(conn)
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()

    def save_history(self, items):
        with self._lock:
            self._history = self._limit_history(items)
            self._reindex_history()
            self._replace_all_db()

    def rewrite_history(self, flush: bool = True):
        """条目已在内存中原地修改时，按当前缓存全量写回。

        常规的作者改名走 update_history_items() 只更新少量行；这个方法留给
        “批量修复元数据”等少见的全量场景。flush 参数保留兼容（SQLite 即时写入）。
        """
        with self._lock:
            if self._history is None:
                return
            self._replace_all_db()

    def update_history_items(self, items):
        """只更新指定的历史行（作者改名等），避免几万条全量重写。"""
        items = [x for x in items if x is not None]
        if not items:
            return 0
        with self._lock:
            conn = self._ensure_db()
            updated = 0
            try:
                for item in items:
                    seq = getattr(item, "_db_seq", None)
                    if seq:
                        cursor = conn.execute(
                            _UPDATE_SQL, _row_values(item) + (seq,))
                        if cursor.rowcount:
                            updated += 1
                            continue
                    cursor = conn.execute(_INSERT_SQL, _row_values(item))
                    item._db_seq = cursor.lastrowid
                    updated += 1
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()
                return 0
            return updated

    def merge_history(self, new_items):
        """把新发现的记录合并进历史（按 aweme_id 去重），不覆盖并发写入的其他记录。"""
        with self._lock:
            items = self._history_items()
            known = {x.aweme_id for x in items if x.aweme_id}
            additions = [x for x in new_items if not (x.aweme_id and x.aweme_id in known)]
            if not additions:
                return 0
            self._history = self._limit_history(additions + items)
            self._reindex_history()
            conn = self._ensure_db()
            try:
                conn.executemany(
                    _INSERT_SQL, [_row_values(x) for x in reversed(additions)])
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()
            self._backfill_seqs(conn)
            return len(additions)

    def clear_history(self):
        with self._lock:
            self._history = []
            self._reindex_history()
            conn = self._ensure_db()
            try:
                conn.execute("DELETE FROM history")
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()
