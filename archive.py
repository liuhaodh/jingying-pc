# -*- coding: utf-8 -*-
"""作品归档：每个作品的全部文件打成一个 `<作品id>.tar`。

规则（2026-11-08 起）：
- 下载时所有文件先落到临时目录里的一个文件夹 `作者_文案_时间/`；
- 下载完整体打包成 `Videos/作者_抖音号/<作品id>.tar`，**tar 里保留这个文件夹**
  作为顶层目录（用户手动解压也不会散落一地）；
- 打包成功后临时文件夹连同散文件一起删除——磁盘上每个作品只剩一个 tar；
- 内置播放器要看单条作品时用 `extract()` 解到临时目录（按「路径 + 大小 + 修改时间」
  缓存，来回切换不会重复解包），播放器关闭时整目录删除。

文件名约定（tar 内，与旧目录结构完全一致）：
    视频.<ext> / 图片_01.<ext>… / 动图_01.<ext>… / 音乐.<ext> / 文案.txt
"""
import os
import shutil
import tarfile
import tempfile

ARCHIVE_EXT = ".tar"
# 解包完成标记：目录「非空」不代表解完了（文件是逐个 move 进去的，中途被
# 强杀会留下半个目录）。缓存命中时必须同时看到这个标记。
_COMPLETE_MARKER = ".complete"

VIDEO_PREFIX = "视频"
IMAGE_PREFIX = "图片_"
LIVE_PREFIX = "动图_"
MUSIC_PREFIX = "音乐"
CAPTION_NAME = "文案.txt"


def is_archive(path) -> bool:
    """这个路径是不是我们打的 tar 包。"""
    return bool(path) and str(path).lower().endswith(ARCHIVE_EXT)


def _files_in(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(name for name in names
                  if os.path.isfile(os.path.join(folder, name)))


def pack(work_dir, tar_path) -> str:
    """把 work_dir 整个文件夹打成 tar_path，返回 tar 路径。

    先写 `.part` 再改名：中途被取消/断电不会留下半个包被当成「已下载」。
    """
    parent = os.path.dirname(tar_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    root = os.path.basename(os.path.normpath(work_dir))
    part = tar_path + ".part"
    try:
        with tarfile.open(part, "w") as tar:
            for name in _files_in(work_dir):
                tar.add(os.path.join(work_dir, name),
                        arcname=f"{root}/{name}", recursive=False)
        os.replace(part, tar_path)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    return tar_path


def member_names(tar_path):
    """tar 里的文件名列表（不含目录，按名字排序）；坏包返回空列表。"""
    try:
        with tarfile.open(tar_path, "r") as tar:
            return sorted(m.name for m in tar.getmembers() if m.isfile())
    except (OSError, tarfile.TarError):
        return []


def read_member(tar_path, suffix):
    """读取 tar 里文件名以 suffix 结尾的第一个成员（读文案.txt 用），失败返回 ""。"""
    try:
        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith(suffix):
                    handle = tar.extractfile(member)
                    if handle is None:
                        return ""
                    return handle.read().decode("utf-8", "ignore")
    except (OSError, tarfile.TarError):
        pass
    return ""


def _classify(names):
    """把 tar/文件夹里的文件名分成视频、图片、动图、音乐、文案。"""
    found = {"video": "", "images": [], "lives": [], "music": "", "caption": ""}
    images = []
    lives = []
    for name in names:
        base = os.path.basename(name)
        if base.startswith(IMAGE_PREFIX):
            images.append(base)
        elif base.startswith(LIVE_PREFIX):
            lives.append(base)
        elif base.startswith(VIDEO_PREFIX):
            found["video"] = base
        elif base.startswith(MUSIC_PREFIX):
            found["music"] = base
        elif base == CAPTION_NAME:
            found["caption"] = base
    # 图片/动图按文件名里的序号排序（图片_02 在 图片_10 前面）
    found["images"] = sorted(images)
    found["lives"] = sorted(lives)
    return found


def _mark_complete(folder):
    try:
        with open(os.path.join(folder, _COMPLETE_MARKER), "w", encoding="utf-8") as f:
            f.write("ok")
        return True
    except OSError:
        return False


def extract(tar_path, cache_root=None):
    """把 tar 解到临时目录，返回各类文件的绝对路径。

    返回 {"video", "images", "lives", "music", "caption", "dir"}：
    - video / music / caption：单个路径或 ""；
    - images / lives：按序号排好的路径列表（lives 与 images 一一对应）。
    """
    cache_root = cache_root or os.path.join(tempfile.gettempdir(), "jingying-play")
    try:
        stat = os.stat(tar_path)
        stamp = f"{stat.st_size}_{int(stat.st_mtime)}"
    except OSError:
        stamp = "0_0"
    folder = os.path.join(cache_root, f"{os.path.basename(tar_path)}.{stamp}")
    result = {"video": "", "images": [], "lives": [], "music": "",
              "caption": "", "dir": folder}
    names = member_names(tar_path)
    kinds = _classify(names)
    expected = [name for name in
                ([kinds["video"], kinds["music"], kinds["caption"]]
                 + kinds["images"] + kinds["lives"]) if name]
    final = False
    if os.path.isdir(folder):
        if os.path.isfile(os.path.join(folder, _COMPLETE_MARKER)):
            final = True      # 完整解过同一个包（大小+时间一致）
        elif expected and all(os.path.isfile(os.path.join(folder, name))
                              for name in expected):
            # 老版本没有标记但文件齐全：补一个，避免白白重解一次。
            final = _mark_complete(folder)
    if not final:
        pending = folder + ".part"
        shutil.rmtree(pending, ignore_errors=True)
        try:
            os.makedirs(pending, exist_ok=True)
            # 逐个成员自己写文件：member 名先压成 basename，既不受 tar 里
            # 相对路径的影响，也不用依赖不同 Python 版本的 extract 过滤参数。
            with tarfile.open(tar_path, "r") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    name = os.path.basename(member.name)
                    if not name or name in (".", ".."):
                        continue
                    handle = tar.extractfile(member)
                    if handle is None:
                        continue
                    with open(os.path.join(pending, name), "wb") as out:
                        shutil.copyfileobj(handle, out, 256 * 1024)
            os.makedirs(folder, exist_ok=True)
            for name in os.listdir(pending):
                os.replace(os.path.join(pending, name), os.path.join(folder, name))
            # 全部 move 完才写标记：中途被强杀留下的半个目录不会再被当成完整缓存。
            final = _mark_complete(folder)
        except (OSError, tarfile.TarError):
            final = False
        finally:
            shutil.rmtree(pending, ignore_errors=True)
    if not final:
        return result

    def join(name):
        return os.path.join(folder, name) if name else ""

    result["video"] = join(kinds["video"])
    result["music"] = join(kinds["music"])
    result["caption"] = join(kinds["caption"])
    result["images"] = [join(x) for x in kinds["images"]]
    result["lives"] = [join(x) for x in kinds["lives"]]
    return result


def cleanup(cache_root):
    """删掉播放器解包缓存目录。"""
    if cache_root:
        shutil.rmtree(cache_root, ignore_errors=True)
