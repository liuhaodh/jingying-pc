# -*- coding: utf-8 -*-
"""读取视频文件的真实分辨率，不依赖 ffmpeg。

用途：判断 WebDAV 上已有的作品是不是低画质。云端文件只按 Range 读取
`moov/tkhd` 这两个盒子（通常几 KB），不把整个视频拉下来。

支持的输入：
- 本地文件路径（`probe_path`）
- 任意可 seek 的文件对象（`probe_mp4`），例如 tar 包里的成员
- 只能按字节区间取的远端文件（`RangeReader` + `probe_mp4`）

MP4 的 `moov` 可能在文件开头（faststart），也可能在结尾，所以先扫头部，
没有再回退扫尾部。抖音的原片两种都有。
"""
import io
import os
import tarfile

# 头部扫不到 moov 时，从末尾回退多少字节再找一次
_TAIL_WINDOW = 16 * 1024 * 1024
# RangeReader 每次取块的字节数：太小请求多，太大浪费流量
_BLOCK = 256 * 1024


class RangeReader(io.RawIOBase):
    """把一个「按区间取字节」的回调包装成可 seek 的只读文件对象。

    `fetch(start, end)` 的 end 是闭区间，返回该区间的 bytes。块按需加载并
    缓存，解析 MP4 盒子时只会取到极少量数据。
    """

    def __init__(self, fetch, size, block=_BLOCK):
        super().__init__()
        self._fetch = fetch
        self.size = max(0, int(size or 0))
        self._block = max(1024, int(block))
        self._cache = {}
        self._pos = 0

    # ---- io 协议 ----
    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._pos + offset
        elif whence == os.SEEK_END:
            pos = self.size + offset
        else:
            raise ValueError("不支持的 whence")
        self._pos = max(0, min(self.size, int(pos)))
        return self._pos

    def _block_at(self, index):
        data = self._cache.get(index)
        if data is not None:
            return data
        start = index * self._block
        end = min(self.size, start + self._block) - 1
        if end < start:
            data = b""
        else:
            data = self._fetch(start, end) or b""
        self._cache[index] = data
        return data

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        n = min(int(n), max(0, self.size - self._pos))
        chunks = []
        while n > 0:
            index, offset = divmod(self._pos, self._block)
            data = self._block_at(index)
            if offset >= len(data):
                break
            piece = data[offset:offset + n]
            if not piece:
                break
            chunks.append(piece)
            self._pos += len(piece)
            n -= len(piece)
        return b"".join(chunks)


def _iter_boxes(handle, start, end):
    """遍历 [start, end) 内的 MP4 盒子，产出 (类型, 内容起点, 内容终点)。"""
    pos = start
    guard = 0
    while pos + 8 <= end:
        guard += 1
        if guard > 4096:  # 结构异常时别无限转圈
            return
        handle.seek(pos)
        head = handle.read(8)
        if len(head) < 8:
            return
        size = int.from_bytes(head[0:4], "big")
        kind = head[4:8]
        header = 8
        if size == 1:
            extra = handle.read(8)
            if len(extra) < 8:
                return
            size = int.from_bytes(extra, "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, pos + size
        pos += size


def _find_moov(handle, size):
    """返回 (moov 内容起点, 内容终点)；找不到返回 None。"""
    for kind, start, end in _iter_boxes(handle, 0, size):
        if kind == b"moov":
            return start, end
    if size <= 0:
        return None
    window = min(size, _TAIL_WINDOW)
    handle.seek(size - window)
    tail = handle.read(window)
    index = tail.rfind(b"moov")
    while index >= 0:
        if index >= 4:
            box_size = int.from_bytes(tail[index - 4:index], "big")
            payload = size - window + index + 4
            if box_size >= 8 and payload + (box_size - 8) <= size:
                return payload, payload + box_size - 8
        index = tail.rfind(b"moov", 0, index)
    return None


def _tkhd_size(handle, start, end):
    """在 moov 内容里递归找 trak/tkhd，取宽高最大的那条（音频轨宽高为 0）。"""
    best = (0, 0)
    for kind, payload, box_end in _iter_boxes(handle, start, end):
        if kind == b"trak":
            width, height = _tkhd_size(handle, payload, box_end)
            if width * height > best[0] * best[1]:
                best = (width, height)
        elif kind == b"tkhd":
            # tkhd 固定以 width/height（16.16 定点）结尾，v0/v1 都一样
            if box_end - payload >= 8:
                handle.seek(box_end - 8)
                raw = handle.read(8)
                if len(raw) == 8:
                    return (int.from_bytes(raw[0:4], "big") >> 16,
                            int.from_bytes(raw[4:8], "big") >> 16)
    return best


def probe_mp4(handle, size):
    """从可 seek 的文件对象里读 (宽, 高)；读不出来返回 (0, 0)。"""
    try:
        moov = _find_moov(handle, int(size or 0))
        if moov is None:
            return (0, 0)
        width, height = _tkhd_size(handle, moov[0], moov[1])
    except (OSError, ValueError, IndexError):
        return (0, 0)
    if width <= 0 or height <= 0 or width > 32768 or height > 32768:
        return (0, 0)
    return (width, height)


def probe_path(path):
    """读本地视频文件的 (宽, 高)。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            return probe_mp4(handle, size)
    except OSError:
        return (0, 0)


def probe_tar(path):
    """读 tar 作品包里的视频分辨率。

    `_pack_work_result` 打出来的包里有 `视频.mp4`；只读该成员，不用解包。
    """
    try:
        with tarfile.open(path) as bundle:
            members = [m for m in bundle.getmembers() if m.isfile()]
            videos = [m for m in members
                      if os.path.basename(m.name).startswith("视频.")]
            if not videos:
                videos = [m for m in members
                          if os.path.splitext(m.name)[1].lower()
                          in (".mp4", ".mov", ".m4v")]
            videos.sort(key=lambda m: m.size, reverse=True)
            for member in videos:
                try:
                    handle = bundle.extractfile(member)
                except (OSError, tarfile.TarError):
                    continue
                if handle is None:
                    continue
                with handle:
                    width, height = probe_mp4(handle, member.size)
                if width and height:
                    return (width, height)
    except (OSError, tarfile.TarError):
        return (0, 0)
    return (0, 0)


def shortest(width, height):
    """竖屏/横屏统一按短边比较清晰度，1080×1920 与 1920×1080 同档。"""
    try:
        width, height = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return 0
    if width <= 0 or height <= 0:
        return 0
    return min(width, height)


def should_upgrade(target_w, target_h, remote_w, remote_h, tolerance=1.05):
    """云端分辨率是否明显低于本次解析结果。

    只用 5% 的容差挡掉编码尺寸抖动（例如 1078 与 1080），跨档差异（720/1080/
    2160）远大于这个比例，不会被误判。任一侧读不出分辨率时一律返回 False：
    宁可不重下，也不能因为解析失败把整个库重下一遍。
    """
    target = shortest(target_w, target_h)
    remote = shortest(remote_w, remote_h)
    if not target or not remote:
        return False
    return target >= remote * float(tolerance)
