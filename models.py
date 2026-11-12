# -*- coding: utf-8 -*-
"""作品模型与 aweme JSON 解析，对齐 Android 版 DouyinJson / MediaInfo。"""
from typing import Optional


class MediaInfo:
    def __init__(self):
        self.aweme_id = ""
        self.title = ""
        self.author = ""
        self.author_id = ""
        self.unique_id = ""
        self.avatar_url = ""
        self.cover_url = ""
        self.play_url = ""
        self.music_url = ""
        self.music_title = ""
        self.share_url = ""
        self.quality = ""
        self.duration_ms = 0
        self.width = 0
        self.height = 0
        self.digg_count = 0
        self.comment_count = 0
        self.collect_count = 0
        self.share_count = 0
        self.create_time = 0
        self.location = ""
        self.ip_location = ""
        self.image_urls: list[str] = []
        self.image_videos: list[str] = []

    def is_album(self):
        return bool(self.image_urls)

    def has_video(self):
        return bool(self.play_url)

    def display_title(self):
        return self.title.strip() if self.title and self.title.strip() else ("抖音作品 " + self.aweme_id)

    def safe_author(self):
        return self.author.strip() if self.author and self.author.strip() else "抖音创作者"


def _first_url(candidates) -> str:
    """从 url_list / dict / string 中取第一个可用 https 链接。"""
    def grab(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("url_list", "url"):
                urls = value.get(key)
                if urls:
                    if isinstance(urls, str):
                        return urls
                    if isinstance(urls, list) and urls:
                        first = urls[0]
                        if isinstance(first, str):
                            return first
                        if isinstance(first, dict):
                            for subkey in ("url", "uri"):
                                u = first.get(subkey)
                                if isinstance(u, str) and u:
                                    return u
            uri = value.get("uri")
            if isinstance(uri, str) and uri:
                return uri
        return ""
    if isinstance(candidates, list):
        for item in candidates:
            url = grab(item)
            if url:
                return url
        return ""
    return grab(candidates)


def _first_nonempty(a, b):
    return a if a else b


def _clean_url(url: str) -> str:
    value = url.strip() if url else ""
    if value.startswith("http://"):
        value = "https://" + value[7:]
    value = value.replace("/playwm/", "/play/").replace("playwm?", "play?")
    value = value.replace("watermark=1", "watermark=0")
    return value


def _music_url_from(music: dict) -> str:
    url = _first_url(music.get("play_url"))
    if not url:
        play = music.get("play_url")
        if isinstance(play, dict):
            uri = play.get("uri")
            if isinstance(uri, str):
                url = uri
    if not url:
        url = _first_nonempty(str(music.get("play_uri", "")), str(music.get("uri", "")))
    if not url:
        return ""
    url = _clean_url(url)
    if not url.startswith(("http://", "https://")):
        part = url if url.startswith("/") else "/" + url
        url = "https://sf-cdn-tos.douyinstatic.com" + part
    return url


def parse_aweme(detail: dict) -> MediaInfo:
    info = MediaInfo()
    info.aweme_id = str(_first_nonempty(
        detail.get("aweme_id", ""), detail.get("awemeId", "")))
    info.title = str(_first_nonempty(detail.get("desc", ""), detail.get("title", "")))
    info.create_time = int(detail.get("create_time", 0) or 0)
    info.share_url = "https://www.douyin.com/video/" + info.aweme_id

    # 地点：POI 名称优先，其次地址/城市；部分作品没有地点字段
    poi = detail.get("poi_info")
    if isinstance(poi, dict):
        info.location = str(_first_nonempty(
            poi.get("name", ""),
            _first_nonempty(poi.get("address", ""), poi.get("city", ""))))
    if not info.location:
        info.location = str(detail.get("location", "") or detail.get("poi_name", "") or "")

    # 发布 IP 属地：不同版本字段不同，能取到就显示
    for key in ("ip_label", "ip_location", "ip_attribution"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            info.ip_location = value.strip()
            break
    if not info.ip_location:
        author_obj = detail.get("author")
        if isinstance(author_obj, dict):
            for key in ("ip_label", "ip_location"):
                value = author_obj.get(key)
                if isinstance(value, str) and value.strip():
                    info.ip_location = value.strip()
                    break

    author = detail.get("author")
    if isinstance(author, dict):
        info.author = str(author.get("nickname", ""))
        info.author_id = _first_nonempty(author.get("sec_uid", ""), author.get("uid", ""))
        info.unique_id = str(author.get("unique_id", ""))
        info.avatar_url = _clean_url(_first_url(author.get("avatar_thumb"))) or \
            _clean_url(_first_url(author.get("avatar_medium")))

    stats = detail.get("statistics")
    if isinstance(stats, dict):
        info.digg_count = int(stats.get("digg_count", 0) or 0)
        info.comment_count = int(stats.get("comment_count", 0) or 0)
        info.collect_count = int(stats.get("collect_count", 0) or 0)
        info.share_count = int(stats.get("share_count", 0) or 0)

    video = detail.get("video")
    if isinstance(video, dict):
        info.duration_ms = int(video.get("duration", 0) or 0)
        info.cover_url = _clean_url(_first_url(video.get("cover"))) or \
            _clean_url(_first_url(video.get("origin_cover"))) or \
            _clean_url(_first_url(video.get("dynamic_cover")))
        _select_video(video, info)

    images = detail.get("images")
    if isinstance(images, list) and images:
        for image in images:
            url = _clean_url(_first_url(image))
            if not url and isinstance(image, dict):
                url = _clean_url(_first_url(image.get("download_url")))
            if url:
                info.image_urls.append(url)
                # 实况图：每张图可能自带一段短视频
                live = ""
                if isinstance(image, dict):
                    video = image.get("video")
                    if isinstance(video, dict):
                        live = _video_url(video)
                info.image_videos.append(live)

    music = detail.get("music")
    if isinstance(music, dict):
        info.music_url = _music_url_from(music)
        info.music_title = str(music.get("title", ""))

    return info


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stream_candidates(video: dict) -> list[dict]:
    """列出 video 里所有可下载的流（带分辨率、体积、码率、编码）。

    抖音网页的 `bit_rate` 是清晰度阶梯：每条带自己的 `play_addr`
    （含 width/height/data_size）。以外还有 `play_addr` / `play_addr_h264` /
    `play_addr_265` 三个兜底地址。以前只在这几个兜底地址里挑，等于放弃阶梯，
    最高只拿到 1080p；原画（2048/2160 那条）几乎都是 `bit_rate` 里的 H.265。
    """
    candidates = []

    def add(address, entry=None, label="", h265=False, fmt="mp4", bit_rate=0):
        """加一条候选流；分辨率/体积优先取地址里的，缺了就用外面那层的。"""
        if not isinstance(address, dict):
            return
        url = _clean_url(_first_url(address))
        if not url:
            return
        entry = entry if isinstance(entry, dict) else {}
        candidates.append({
            "url": url,
            "width": _int(address.get("width")) or _int(entry.get("width")),
            "height": _int(address.get("height")) or _int(entry.get("height")),
            "size": _int(address.get("data_size")) or _int(entry.get("data_size")),
            "bit_rate": _int(bit_rate) or _int(address.get("bit_rate")),
            "h265": bool(h265),
            "format": str(fmt or ""),
            "label": str(label or ""),
        })

    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        gear = str(item.get("gear_name", ""))
        # 有的响应不填 is_h265，只在 gear_name 里写 HEVC/265
        h265 = bool(item.get("is_h265") or item.get("is_bytevc1")
                    or "HEVC" in gear.upper() or "265" in gear)
        add(item.get("play_addr"), item, gear, h265,
            item.get("format", "mp4"), item.get("bit_rate"))
    # 这三个兜底地址不给外层条目：video 上的 width/height 是「源片」尺寸，
    # 拿它当这条流的分辨率会把 720p 的流标成 2K/4K。
    add(video.get("play_addr_265"), None, "play_addr_265", True)
    add(video.get("play_addr_h264"), None, "play_addr_h264", False)
    add(video.get("play_addr"), None, "play_addr", video.get("is_h265"))
    return candidates


def _rank_stream(stream):
    """越高越好的排序键：分辨率 → 完整 mp4 → 码率 → 体积 → H.265。

    「原画」就是同一分辨率里码率最高的那一档，所以码率必须排在体积前面：
    抖音的 `bit_rate` 条目偶尔不带 `data_size`，旧排序会把它当成 0，让同
    分辨率的低码率档反而胜出（画质变糊却不报错）。码率缺失时才用体积兜底。

    最后一项优先 H.265：抖音的原画基本都是 HEVC 编码，同码率同体积时
    H.265 那条才是原始编码流，而不是网页转码出来的 H.264。
    """
    area = stream["width"] * stream["height"]
    complete = 0 if stream["format"].lower() == "dash" else 1
    rate = stream["bit_rate"] or stream["size"]
    return (area, complete, rate, stream["size"], 1 if stream["h265"] else 0)


def _quality_label(stream) -> str:
    """把“原画”翻译成人能看懂的清晰度标签，例如「原画 2160p · H.265」。"""
    short = min(stream["width"], stream["height"]) if (
        stream["width"] and stream["height"]) else 0
    label = f"原画 {short}p" if short else "原画"
    if stream["h265"]:
        label += " · H.265"
    return label


def _video_url(video: dict) -> str:
    """从 video 结构里取可下载的播放地址（实况图用，同样挑最高画质）。"""
    if not isinstance(video, dict):
        return ""
    candidates = _stream_candidates(video)
    if candidates:
        return max(candidates, key=_rank_stream)["url"]
    return _clean_url(_first_url(video.get("play_addr"))) or \
        _clean_url(_first_url(video.get("play_addr_h264")))


def _select_video(video: dict, info: MediaInfo):
    """选最高可用的画质（原画）：分辨率最大，其次体积最大的完整 mp4。"""
    candidates = _stream_candidates(video)
    if candidates:
        best = max(candidates, key=_rank_stream)
        info.play_url = best["url"]
        info.quality = _quality_label(best)
        info.width = best["width"]
        info.height = best["height"]
        return
    # 一个可选流都没有（字段缺失）：退回老办法，至少拿到能播的地址
    info.play_url = _clean_url(_first_url(video.get("play_addr"))) or \
        _clean_url(_first_url(video.get("play_addr_h264"))) or \
        _clean_url(_first_url(video.get("play_addr_265")))
    info.quality = str(video.get("ratio", "原始画质"))


def find_detail(root: dict) -> Optional[dict]:
    """从详情接口响应里找 aweme_detail。"""
    if not isinstance(root, dict):
        return None
    for key in ("aweme_detail", "detail", "aweme", "item"):
        value = root.get(key)
        if isinstance(value, dict):
            return value
    return None