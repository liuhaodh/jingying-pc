# -*- coding: utf-8 -*-
"""链接解析：分享文案 / 短链 / 作品 ID / sec_uid，与 Android 版 DouyinLinkParser 对齐。"""
import re

# 宽松：直接搜 http(s) 链接
_LOOSE_URL = re.compile(r"https?://[^\s，。；：""''（）《》【】\"'<>]+")
_SHORT_LINK = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?")
_AWEME_ID = re.compile(r"(?:/video/|/note/|/share/video/|/share/note/)(\d{15,25})")
_MODAL_ID = re.compile(r"[?&](?:modal_id|aweme_id|item_id)=(\d{15,25})")
_PURE_ID = re.compile(r"^\d{15,25}$")
_USER_PATH = re.compile(r"/user/([^/?#]+)")
_PURE_SEC_UID = re.compile(r"^MS4wLjABAAAA[A-Za-z0-9_\-]+$")
_SEC_UID_ANYWHERE = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_\-]{20,}")


def extract_url(text):
    """从分享文案中提取第一个抖音链接。"""
    if not text:
        return ""
    value = text.replace("\\/", "/").strip()
    match = _LOOSE_URL.search(value)
    if not match:
        return ""
    url = match.group(0)
    # 去掉结尾的中文标点
    url = re.sub(r"[，。；：""''（）《》】>]+$", "", url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def extract_aweme_id(text):
    """作品 ID。纯 ID、链接路径、modal_id 参数均可。"""
    if not text:
        return ""
    value = text.replace("\\/", "/").strip()
    if _PURE_ID.match(value):
        return value
    # 先搜 URL 路径，再搜 modal_id 参数
    url = extract_url(value)
    if url:
        match = _AWEME_ID.search(url)
        if match:
            return match.group(1)
        match = _MODAL_ID.search(url)
        if match:
            return match.group(1)
    # 也接受 /video/123、/note/123 这类相对路径
    match = _AWEME_ID.search(value)
    if match:
        return match.group(1)
    match = _MODAL_ID.search(value)
    if match:
        return match.group(1)
    return ""


def is_short_link(text):
    url = extract_url(text)
    return bool(url) and bool(_SHORT_LINK.match(url))


def extract_sec_uid(text):
    """创作者 sec_uid：纯 sec_uid、/user/ 链接、分享文案里的 sec_uid 均可。"""
    if not text:
        return ""
    value = text.replace("\\/", "/").strip()
    if _PURE_SEC_UID.match(value):
        return value
    if extract_aweme_id(value):
        return ""
    url = extract_url(value)
    if url:
        match = _USER_PATH.search(url)
        if match and match.group(1).startswith("MS4wLjABAAAA") and len(match.group(1)) > 20:
            return match.group(1)
    match = _SEC_UID_ANYWHERE.search(value)
    if match:
        return match.group(0)
    return ""


def looks_like_douyin(text):
    return extract_aweme_id(text) != "" or extract_sec_uid(text) != "" or bool(extract_url(text))


def extract_all_sec_uids(text):
    """批量导入：从多行文本中提取所有作者 sec_uid（去重、保持顺序）。"""
    if not text:
        return []
    found = []
    seen = set()
    for match in _SEC_UID_ANYWHERE.finditer(text.replace("\\/", "/")):
        sid = match.group(0)
        # 以前用 `sid not in found` 线性查找，批量导入几千个作者时是 O(n²)。
        if sid not in seen:
            seen.add(sid)
            found.append(sid)
    return found


def is_profile_input(text):
    return extract_sec_uid(text) != ""


def profile_page_url(sec_uid):
    return "https://www.douyin.com/user/" + sec_uid


def video_page_url(aweme_id):
    return "https://www.douyin.com/video/" + aweme_id