# -*- coding: utf-8 -*-
"""净影 PC 版 · 全局配置"""
import hashlib
import json
import os
import shutil
import sys
import time

APP_NAME = "净影"
# 唯一版本真源：窗口标题 / exe 版本资源 / CHANGELOG / zip 名 / git tag 全部
# 从这里读。发版时只改这一行，再执行 `python build_release.py`；不要在其他
# 地方手写版本号，否则又会出现「改了代码但版本号不变」。
APP_VERSION = "1.7.1"

# 项目根目录（随脚本位置）
ROOT = os.path.dirname(os.path.abspath(__file__))

_BUILD_SHA_CACHE = None


def build_sha() -> str:
    """发行包的构建短 sha；源码运行（含测试）返回空串。

    构建脚本会把 git 短 sha 写进 exe 同目录的 build_sha.txt，只有打包产物
    带这个文件。这样同一版本号的不同构建也能从标题上区分；开发态标题保持
    稳定，不会被构建产物影响（GUI 冒烟测试也依赖这一点）。
    """
    global _BUILD_SHA_CACHE
    if _BUILD_SHA_CACHE is not None:
        return _BUILD_SHA_CACHE
    _BUILD_SHA_CACHE = ""
    if not getattr(sys, "frozen", False):
        return _BUILD_SHA_CACHE
    bases = (os.path.dirname(os.path.abspath(sys.executable)),
             getattr(sys, "_MEIPASS", "") or "")
    for base in bases:
        if not base:
            continue
        try:
            with open(os.path.join(base, "build_sha.txt"), "r", encoding="utf-8") as f:
                sha = f.read().strip().split()[0]
        except (OSError, IndexError, UnicodeError):
            continue
        if sha and sha.lower() != "nogit":
            _BUILD_SHA_CACHE = sha[:12]
            break
    return _BUILD_SHA_CACHE


def window_title() -> str:
    """窗口标题的唯一生成入口（app.py 与测试共用）。"""
    title = f"{APP_NAME} · 电脑版 v{APP_VERSION}"
    sha = build_sha()
    return f"{title} ({sha})" if sha else title


def _app_dir() -> str:
    """程序所在目录（打包后为 exe 所在目录，而不是 _internal）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return ROOT


def _pick_dir(candidates, fallback_name="data") -> str:
    """按顺序挑一个可写目录；全部失败时退到程序目录下的 fallback。"""
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            # 探测文件不删（某些云同步/杀毒/沙箱环境删除刚创建的文件会卡住
            # 几十秒，启动阶段不能冒这个险），但用固定名字覆盖写：以前文件名
            # 带 pid+时间戳，每次启动都多留一个，本机实测数据目录堆了 40 多个。
            # 旧格式的残留由 cleanup_stale_temp_files() 回收。
            probe = os.path.join(path, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            return path
        except OSError:
            continue
    # 兜底目录在只读位置（例如装在 Program Files）、或数据盘被拔掉时也会失败；
    # 以前这里没有任何保护，import config 直接抛 OSError，双击 exe 无窗口、
    # 无日志地退出。最后退到系统临时目录，保证程序至少能起来。
    for candidate in (os.path.join(_app_dir(), fallback_name),
                      os.path.join(_local_appdata, "JingYing", fallback_name)):
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            continue
    import tempfile
    writable = os.path.join(tempfile.gettempdir(), "JingYing", fallback_name)
    os.makedirs(writable, exist_ok=True)
    return writable


_local_appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
_stable_data = os.path.join(_local_appdata, "JingYing")
# 分享用的便携包会在 exe 同目录放 portable.flag：
# - 数据只放在便携目录内，不读取本机安装版/开发版数据；
# - 跳过 %LOCALAPPDATA% 的旧数据迁移，避免把作者、登录状态带进别人的包。
PORTABLE = bool(
    getattr(sys, "frozen", False)
    and os.path.isfile(os.path.join(_app_dir(), "portable.flag"))
)


def _portable_data_dir() -> str:
    """数据放在程序目录之外，替换 exe/发行包时不会跟着被删除。

    开发构建：jingying-pc/JingYingData
    解压发行包：JingYingPC 同级的 JingYingData
    便携分享包：JingYingPC/JingYingData（整个文件夹可整体拷贝）
    """
    if PORTABLE:
        return os.path.join(_app_dir(), "JingYingData")
    if not getattr(sys, "frozen", False):
        return os.path.join(ROOT, "JingYingData")
    exe_dir = _app_dir()
    parent = os.path.dirname(exe_dir)
    grandparent = os.path.dirname(parent)
    if os.path.isfile(os.path.join(grandparent, "JingYingPC.spec")):
        return os.path.join(grandparent, "JingYingData")
    return os.path.join(parent, "JingYingData")


if getattr(sys, "frozen", False):
    _default_data = _portable_data_dir()
    _default_videos = os.path.join(_app_dir(), "downloads", "Videos")
    _default_music = os.path.join(_app_dir(), "downloads", "Music")
else:
    _default_data = _portable_data_dir()
    _default_videos = os.path.expanduser(os.path.join("~", "Videos"))
    _default_music = os.path.expanduser(os.path.join("~", "Music"))


def _migrate_legacy_data(target: str):
    """把旧版 exe/data、%LOCALAPPDATA%\\JingYing 下的数据迁移到新目录。"""
    try:
        target_abs = os.path.normcase(os.path.abspath(target))
    except (TypeError, ValueError):
        return
    legacy_dirs = [
        os.path.join(_app_dir(), "data"),
        os.path.join(_local_appdata, "JingYing"),
    ]
    for legacy in legacy_dirs:
        try:
            if os.path.normcase(os.path.abspath(legacy)) == target_abs:
                continue
        except (TypeError, ValueError):
            continue
        if not os.path.isdir(legacy):
            continue
        for name in ("authors.json", "history.json", "webdav.json",
                     "webdav_uploaded.json", "settings.json", "cookies.json",
                     "login_state.json"):
            src = os.path.join(legacy, name)
            dst = os.path.join(target, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass
        for name in ("authors", "covers"):
            src = os.path.join(legacy, name)
            dst = os.path.join(target, name)
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    shutil.copytree(src, dst)
                except OSError:
                    pass


# 数据目录：环境变量 > 便携目录 > 用户目录；重建/解压覆盖不会清空。
_data_candidates = [
    os.environ.get("JINGYING_DATA_DIR", ""),
    _default_data,
    os.path.join(_app_dir(), "data"),
]
if not PORTABLE:
    _data_candidates.insert(2, _stable_data)
DATA_DIR = _pick_dir(_data_candidates, "data")
if not os.environ.get("JINGYING_DATA_DIR") and not PORTABLE:
    _migrate_legacy_data(DATA_DIR)
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
# 历史记录改用 SQLite 增量写入；首次启动会自动把旧的 history.json 导入这个库
HISTORY_DB = os.path.join(DATA_DIR, "history.db")
# 「查看日志」专用：只记录「用哪个账号同步了哪位作者的多少个作品」，不写别的。
SYNC_LOG_FILE = os.path.join(DATA_DIR, "sync_log.txt")
MAX_SYNC_LOG_LINES = 2000
# 「下载日志」专用：每下完一个作品记一行，带作者和该作者剩余未下载数。
DOWNLOAD_LOG_FILE = os.path.join(DATA_DIR, "download_log.txt")
MAX_DOWNLOAD_LOG_LINES = 5000

# 旧版保存位置（只读兼容）：downloads\Videos\<作品id>.tar / 更早的散文件目录，
# 以及单独的背景音乐根。新下载一律写到 SAVE_ROOT，这里只用于识别与播放老数据。
DOWNLOAD_BASE = _pick_dir([
    os.environ.get("JINGYING_DOWNLOAD_BASE", ""),
    _default_videos,
    os.path.join(_app_dir(), "downloads", "Videos"),
], os.path.join("downloads", "Videos"))
MUSIC_BASE = _pick_dir([
    os.environ.get("JINGYING_MUSIC_BASE", ""),
    _default_music,
    os.path.join(_app_dir(), "downloads", "Music"),
], os.path.join("downloads", "Music"))
FOLDER_NAME = "净影"

# 作品保存目录（2026-11-09 起）：
#   <下载根>\净影\<作者名_抖音号>\<作品id>\视频.mp4|图片_01.jpg|动图_01.mp4|音乐.mp3|文案.txt
# 放在 downloads 下、和 Videos / Music / 作者资料 平级：
#   - 目录名就是「净影」，符合用户要的「先是净影」；
#   - WebDAV 上传根仍然是 downloads，新作品和旧数据在同一条上传链路里，
#     远程相对路径就是 净影/作者/作品id/文件；
#   - 旧的 downloads\Videos\<作者>\<作品id>.tar 与更早的散文件目录仍能识别、能播放。
SAVE_ROOT = (os.environ.get("JINGYING_SAVE_ROOT")
             or os.path.join(os.path.dirname(DOWNLOAD_BASE), FOLDER_NAME))

# 作者头像、简介和简介历史跟作品放在一起，WebDAV 上传 downloads 时会一并
# 上传到 作者资料/ 下；不再放在 JingYingData 里。
AUTHOR_PROFILE_DIR = os.path.join(os.path.dirname(DOWNLOAD_BASE), "作者资料")
AUTHOR_ASSETS_DIR = os.path.join(AUTHOR_PROFILE_DIR, "authors")
AUTHORS_FILE = os.path.join(AUTHOR_PROFILE_DIR, "authors.json")

# WebDAV 上传根目录与扫描范围。
# 相对路径按 UPLOAD_ROOT 计算，和远程目录结构一一对应；开发版的 UPLOAD_ROOT
# 是用户主目录，如果不限定 UPLOAD_DIRS，「上传全部」会把桌面、文档、AppData
# 也扫描进去（以前就是这样），既慢又可能把私人文件传上云。
UPLOAD_ROOT = os.path.dirname(DOWNLOAD_BASE)
UPLOAD_DIRS = [SAVE_ROOT, DOWNLOAD_BASE, MUSIC_BASE, AUTHOR_PROFILE_DIR]


def _flatten_download_root(base):
    """把旧版 Videos/净影/作者 和 Music/净影/作者 拍平成 作者 目录。"""
    legacy = os.path.join(base, FOLDER_NAME)
    if not os.path.isdir(legacy):
        return
    try:
        names = os.listdir(legacy)
    except OSError:
        # 网络盘/权限不足时不要因为一次列举失败就让整个程序起不来
        return
    for name in names:
        src = os.path.join(legacy, name)
        dst = os.path.join(base, name)
        try:
            if not os.path.exists(dst):
                shutil.move(src, dst)
            elif os.path.isdir(src) and os.path.isdir(dst):
                for child in os.listdir(src):
                    child_src = os.path.join(src, child)
                    child_dst = os.path.join(dst, child)
                    if not os.path.exists(child_dst):
                        shutil.move(child_src, child_dst)
                os.rmdir(src)
        except OSError:
            pass
    try:
        os.rmdir(legacy)
    except OSError:
        pass


def _flatten_history_paths():
    """同步把历史记录里的旧路径改成拍平后的路径。

    历史文件可达几十 MB，每次启动都解析一遍要 0.6~1 秒。迁移成功后写一个标记文件，
    之后只要旧目录不存在就直接跳过；标记缺失（上次迁移中断）时才重新扫描。
    """
    old_video = os.path.join(DOWNLOAD_BASE, FOLDER_NAME)
    old_music = os.path.join(MUSIC_BASE, FOLDER_NAME)
    legacy = os.path.isdir(old_video) or os.path.isdir(old_music)
    marker = os.path.join(DATA_DIR, "history_paths_flattened_v1")
    if not legacy and os.path.exists(marker):
        return
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, list):
        return
    changed = False

    def fix(path):
        nonlocal changed
        if not path:
            return path
        for old, new in ((old_video, DOWNLOAD_BASE), (old_music, MUSIC_BASE)):
            if path.startswith(old + os.sep) or path.startswith(old + "/"):
                changed = True
                return new + path[len(old):]
        return path

    for entry in data:
        if not isinstance(entry, dict):
            continue
        for key in ("video_path", "music_path"):
            value = fix(entry.get(key))
            if value != entry.get(key):
                entry[key] = value
        for key in ("album_paths", "album_videos"):
            values = entry.get(key)
            if isinstance(values, list):
                entry[key] = [fix(x) for x in values]
    if changed:
        tmp = f"{HISTORY_FILE}.flatten.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, HISTORY_FILE)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not legacy:
        # 旧目录已经不在了：记录一次，下次启动不再解析历史文件。
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write("1")
        except OSError:
            pass


def _migrate_flat_downloads():
    for step in (lambda: _flatten_download_root(DOWNLOAD_BASE),
                 lambda: _flatten_download_root(MUSIC_BASE),
                 _flatten_history_paths):
        try:
            step()
        except Exception:  # noqa: BLE001 - 迁移失败只影响旧目录，不能拦住启动
            pass


_migrate_flat_downloads()


def cleanup_stale_temp_files(max_age_s: float = 3600.0) -> int:
    """清理异常退出留下的临时文件，以及旧版本遗留的写权限探针。

    原子写入的临时文件名是 `<目标>.<pid>.<tid>.tmp`，正常结束时会自己删掉；
    进程被强杀 / 关窗时线程被终止，`finally` 跑不到，文件就永久留在数据目录里
    （实测能攒到 50 MB）。老版本每次启动还会写带 pid 的 `.write_probe_*` /
    `.jingying_probe_*` 且从不删除（本机实测 40 多个）；现在探针改成固定名字
    覆盖写，这里把旧格式的残留一并回收。

    只删一小时以前、且名字严格匹配的文件，避免误删用户文件和另一个实例
    正在写的文件。
    """
    import re
    pattern = re.compile(r"\.\d+\.\d+\.tmp$")
    probe_pattern = re.compile(r"^\.(?:write_probe|jingying_probe)_\d+(?:_\d+)?$")
    now = time.time()
    removed = 0
    folders = (DATA_DIR, AUTHOR_PROFILE_DIR,
               globals().get("TEMP_DIR"), globals().get("DOWNLOAD_BASE"),
               globals().get("MUSIC_BASE"))
    for folder in folders:
        if not folder:
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            is_atomic_leftover = name.endswith(".tmp") and bool(pattern.search(name))
            is_old_probe = bool(probe_pattern.match(name))
            if not (is_atomic_leftover or is_old_probe):
                continue
            path = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(path) < max_age_s:
                    continue
                os.remove(path)
                removed += 1
            except OSError:
                pass
            except Exception:  # noqa: BLE001
                pass
    return removed


def _author_assets_marker():
    """作者资料迁移标记（按目标目录区分）。

    数据目录和下载目录都可以被环境变量/便携包改到别处，同一个标记名会让
    「在 A 目录迁移成功」挡住「B 目录还没迁移」的情况，所以把目标路径也算进标记名。
    """
    digest = hashlib.sha1(
        os.path.normcase(os.path.abspath(AUTHOR_PROFILE_DIR)).encode("utf-8", "ignore")
    ).hexdigest()[:8]
    return os.path.join(DATA_DIR, f"author_assets_migrated_v1_{digest}")


def _migrate_author_assets():
    """把旧版 JingYingData/authors.json 和头像目录复制到 downloads/作者资料。

    旧文件保留作为备份，不删除；JSON 里的绝对头像路径同步改到新目录。
    迁移成功一次后写标记文件，之后启动直接跳过：以前每次启动都要读一遍
    700KB 的通讯录、再对每位作者的头像做 isfile 检查（347 位作者 ≈ 上千次
    系统调用），而这些工作 99% 的时候已经做完了。
    """
    marker = _author_assets_marker()
    # 标记在、但目标通讯录没了（换了下载目录/被清理过）就重新迁移一次
    if os.path.exists(marker) and os.path.exists(AUTHORS_FILE):
        return
    old_json = os.path.join(DATA_DIR, "authors.json")
    old_dir = os.path.join(DATA_DIR, "authors")
    try:
        os.makedirs(AUTHOR_PROFILE_DIR, exist_ok=True)
        if not os.path.exists(AUTHORS_FILE) and os.path.exists(old_json):
            shutil.copy2(old_json, AUTHORS_FILE)
        if os.path.isdir(old_dir):
            os.makedirs(AUTHOR_ASSETS_DIR, exist_ok=True)
            for name in os.listdir(old_dir):
                src = os.path.join(old_dir, name)
                dst = os.path.join(AUTHOR_ASSETS_DIR, name)
                if os.path.isdir(src):
                    if not os.path.exists(dst):
                        shutil.copytree(src, dst)
                    else:
                        # 只补缺失文件，不覆盖新目录里已经更新过的头像。
                        for filename in os.listdir(src):
                            source_file = os.path.join(src, filename)
                            target_file = os.path.join(dst, filename)
                            if (os.path.isfile(source_file)
                                    and not os.path.exists(target_file)):
                                shutil.copy2(source_file, target_file)
                elif not os.path.exists(dst):
                    shutil.copy2(src, dst)
        if os.path.exists(AUTHORS_FILE):
            with open(AUTHORS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            def _localize(entry, path):
                """把通讯录里指向桌面/旧数据目录的头像收进作者资料目录。"""
                if not path or not os.path.isfile(path):
                    return path
                try:
                    in_assets = os.path.normcase(path).startswith(
                        os.path.normcase(AUTHOR_ASSETS_DIR) + os.sep)
                except (TypeError, ValueError):
                    in_assets = False
                if in_assets:
                    return path
                sec = "".join(c for c in str(entry.get("sec_uid", ""))
                              if c.isalnum() or c in "-_")[:80] or "imported"
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                digest = hashlib.sha1(path.encode("utf-8", "ignore")).hexdigest()[:8]
                folder = os.path.join(AUTHOR_ASSETS_DIR, sec)
                os.makedirs(folder, exist_ok=True)
                target = os.path.join(folder, f"avatar_imported_{digest}{ext}")
                if not os.path.exists(target):
                    shutil.copy2(path, target)
                return target

            changed = False
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                path = _localize(entry, entry.get("avatar_local"))
                if path != entry.get("avatar_local"):
                    entry["avatar_local"] = path
                    changed = True
                history = entry.get("avatar_history")
                if isinstance(history, list):
                    for i, path in enumerate(history):
                        new_path = _localize(entry, path)
                        if new_path != path:
                            history[i] = new_path
                            changed = True
            if changed:
                tmp = f"{AUTHORS_FILE}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, AUTHORS_FILE)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("1")
    except (OSError, ValueError):
        # 迁移失败时继续用旧路径，不影响程序启动；下次启动再试
        pass


_migrate_author_assets()
try:
    cleanup_stale_temp_files()
except Exception:  # noqa: BLE001 - 清理只是顺手做的事
    pass

# 浏览器会话目录（登录 Cookie）：放在数据目录下，升级不会丢失。
SESSION_DIR = os.environ.get(
    "JINGYING_SESSION_DIR", os.path.join(DATA_DIR, "browser_session"))
# 最多五个抖音号，各自独立的 Chromium 配置与 Cookie 文件；同步时已登录的账号轮流使用。
SESSION_DIRS = [
    SESSION_DIR,
    os.environ.get("JINGYING_SESSION_DIR_2",
                   os.path.join(DATA_DIR, "browser_session_2")),
    os.environ.get("JINGYING_SESSION_DIR_3",
                   os.path.join(DATA_DIR, "browser_session_3")),
    os.environ.get("JINGYING_SESSION_DIR_4",
                   os.path.join(DATA_DIR, "browser_session_4")),
    os.environ.get("JINGYING_SESSION_DIR_5",
                   os.path.join(DATA_DIR, "browser_session_5")),
]
COOKIE_FILES = [
    os.path.join(DATA_DIR, "cookies.json"),
    os.path.join(DATA_DIR, "cookies_2.json"),
    os.path.join(DATA_DIR, "cookies_3.json"),
    os.path.join(DATA_DIR, "cookies_4.json"),
    os.path.join(DATA_DIR, "cookies_5.json"),
]


def _ensure_temp_dir():
    """Playwright/Chromium 需要可写的临时目录（放数据目录下，重建不丢）。"""
    import tempfile
    preferred = os.path.join(DATA_DIR, "tmp")
    try:
        os.makedirs(preferred, exist_ok=True)
        # 固定名字覆盖写、不删：启动阶段删除文件在部分环境会卡住，而固定
        # 名字不会像带 pid 的文件那样每次启动都多留一个。
        probe = os.path.join(preferred, ".jingying_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.environ["TEMP"] = preferred
        os.environ["TMP"] = preferred
        return preferred
    except OSError:
        return tempfile.gettempdir()


TEMP_DIR = _ensure_temp_dir()

# 本地 Chromium（按顺序查找：环境变量 → 项目自带 → 开发环境 → 系统浏览器）
def _browser_usable(path: str) -> bool:
    """判断浏览器目录是不是完整的 Chromium 发行包。

    便携版偶尔会被清理掉部分文件；缺少 `chrome_crashpad_handler.exe` 时，
    Chromium 启动会报 “crash server failed to launch, self-terminating” 直接退出，
    表现为所有解析都失败。这里只做一次文件存在性检查，成本可以忽略。
    """
    folder = os.path.dirname(path)
    if not os.path.exists(os.path.join(folder, "chrome.dll")):
        # 不是完整的安装目录（例如只放了一个启动器），不额外校验
        return True
    return os.path.exists(os.path.join(folder, "chrome_crashpad_handler.exe"))


def resolve_chrome() -> str:
    candidates = []
    env = os.environ.get("JINGYING_CHROME")
    if env:
        candidates.append(env)
    candidates += [
        os.path.join(ROOT, "tools", "chromium", "chrome-win", "chrome.exe"),
        os.path.join(os.path.dirname(ROOT), "tools", "chromium", "chrome-win", "chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path) and _browser_usable(path):
            return path
    return ""


CHROME_PATH = resolve_chrome()

# 抖音 UA
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 抓取上限
MAX_WORKS = 5000
MAX_EMPTY_SCROLLS = 12
# 接口明确表示还有下一页时，允许多等几次滚动，避免大账号只抓到几百个就停
MAX_EMPTY_SCROLLS_WITH_MORE = 40
# 接口返回 has_more=0 时不要立刻相信：抖音偶尔会先回一个假的 0，
# 再继续返回后续页；至少再滚几次确认没有新作品。
MAX_EMPTY_SCROLLS_DONE = 5
# has_more=0 但主页总数明显更多、且最后一页是满页时，可能是假结束；多等一会
MAX_EMPTY_SCROLLS_SUSPICIOUS = 20
PROFILE_TIMEOUT_MS = 600000

# 主机白名单（视频/图集/音乐下载）
ALLOWED_HOST_SUFFIXES = [
    "douyin.com", "iesdouyin.com", "douyinvod.com", "douyincdn.com",
    "snssdk.com", "amemv.com", "ixigua.com", "pstatp.com",
    "byteimg.com", "douyinpic.com", "bytecdn.cn", "bytedance.com",
    "volccdn.com", "bytednsdoc.com", "bytegoofy.com", "zjcdn.com",
    "feelgood.cn", "ibytedapm.com", "douyinstatic.com", "muscdn.com",
    "byteoc.com", "toutiaocdn.com", "toutiaoimg.com", "smtcdns.com",
    # 音乐直链经常跳转到字节的 GSLB 前置：
    # https://v9-default.tt.x.bsgslb.cn/lf9-music-east.douyinstatic.com/obj/...?real_host=<base64>
    # 路径里带的是真实 douyinstatic 主机名，不放行会导致几乎所有背景音乐下载失败。
    "bsgslb.cn",
    # 原始上传码流的 play 直链下载时还会再 302 到第三方 GSLB/CDN 节点：
    # https://d6j3wcpdqlpqs.v1d.pkoplink.com/fsb9446b3f.a.bdydns.com/...
    # 参数 http_x-tos-host=*.douyinvod.com 指明真实来源；不放行会让原片在
    # 落盘前被 downloader._download_to_file 判为非法跳转并重取、最终回退转码档。
    # 2026-11-13 全量同步又实测到 iqhvbghbvehek.jspcdn.cn（路径里带
    # v5-se-jltc-cold.douyinvod.com）。白名单只是兜底，轮换域名主要靠
    # downloader._is_allowed_host 里的「抖音来源标记」判定放行。
    "pkoplink.com", "bdydns.com", "bdycdn.cn", "jspcdn.cn",
]


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SAVE_ROOT, exist_ok=True)
    os.makedirs(DOWNLOAD_BASE, exist_ok=True)
    os.makedirs(MUSIC_BASE, exist_ok=True)
    os.makedirs(AUTHOR_PROFILE_DIR, exist_ok=True)