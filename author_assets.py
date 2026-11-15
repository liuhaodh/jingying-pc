# -*- coding: utf-8 -*-
"""作者资料存档：加入通讯录时保存头像与主页简介；发现修改时另存一份。"""
import hashlib
import os
import time

import config
import net_policy

MAX_SNAPSHOTS = 30


def _author_dir(sec_uid: str) -> str:
    safe = "".join(c for c in sec_uid if c.isalnum() or c in "-_")[:80] or "author"
    path = os.path.join(config.AUTHOR_ASSETS_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _extension(url: str) -> str:
    path = url.split("?")[0].lower()
    for ext in (".webp", ".png", ".gif", ".jpeg", ".jpg"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def _download_avatar(url: str, sec_uid: str) -> tuple[str, str]:
    """下载头像，返回 (本地路径, sha256)。文件名带时间戳，不覆盖旧头像。"""
    def do_fetch():
        with net_policy.session_for("asset").get(url, timeout=30) as response:
            response.raise_for_status()
            return response.content

    outcome = net_policy.retry_sync(do_fetch, key="asset", tag="avatar")
    if not outcome.ok:
        raise outcome.error or IOError(outcome.reason or "头像下载失败")
    data = outcome.value
    digest = hashlib.sha256(data).hexdigest()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"avatar_{stamp}_{digest[:8]}{_extension(url)}"
    target = os.path.join(_author_dir(sec_uid), name)
    if not os.path.exists(target):
        with open(target, "wb") as f:
            f.write(data)
    return target, digest


def update_author_assets(author, profile, force: bool = False,
                         download_avatar: bool = True) -> dict:
    """对比并保存作者头像/简介。

    返回 {"avatar_changed", "signature_changed", "avatar_path",
          "avatar_needs_download"}。
    头像或简介有变化时，会另外保存一份历史副本（不覆盖旧的）。

    download_avatar=False 时不在当前线程下载头像（同步收尾在主线程，网络请求
    会把界面卡住）：只把 avatar_url 记下来并置 avatar_needs_download，交给
    调用方用后台线程下载，完成后调 apply_avatar()。
    """
    result = {"avatar_changed": False, "signature_changed": False,
              "avatar_path": "", "avatar_needs_download": False}
    now_ms = int(time.time() * 1000)

    signature = (getattr(profile, "signature", "") or "").strip()
    if signature and (force or signature != author.signature):
        author.signature = signature
        author.signature_history.append({"text": signature, "time": now_ms})
        author.signature_history = author.signature_history[-MAX_SNAPSHOTS:]
        result["signature_changed"] = True

    avatar_url = (getattr(profile, "avatar_url", "") or "").strip()
    if avatar_url and (force or not author.avatar_local or avatar_url != author.avatar_url):
        author.avatar_url = avatar_url
        if not download_avatar:
            # 交给后台线程下载，避免阻塞主线程
            result["avatar_needs_download"] = True
            return result
        try:
            path, digest = _download_avatar(avatar_url, author.sec_uid)
            # URL 变化但内容相同：不重复存副本
            if force or digest != author.avatar_hash:
                author.avatar_history.append(path)
                author.avatar_history = author.avatar_history[-MAX_SNAPSHOTS:]
                author.avatar_local = path
                author.avatar_hash = digest
                result["avatar_changed"] = True
                result["avatar_path"] = path
        except Exception:
            # 头像下载失败不影响同步
            pass
    return result


def fetch_avatar(sec_uid: str, url: str):
    """后台线程用：下载头像，返回 (本地路径, sha256)。"""
    return _download_avatar(url, sec_uid)


def apply_avatar(author, path: str, digest: str) -> bool:
    """把后台下载好的头像写回作者；内容没变就不另存副本。"""
    if not path:
        return False
    if digest and digest == author.avatar_hash:
        return False
    author.avatar_history.append(path)
    author.avatar_history = author.avatar_history[-MAX_SNAPSHOTS:]
    author.avatar_local = path
    author.avatar_hash = digest
    return True