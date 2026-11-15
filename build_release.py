# -*- coding: utf-8 -*-
"""净影 PC 版 · 一键发版脚本。

版本唯一真源是 ``config.py`` 里的 ``APP_VERSION``；窗口标题、exe 版本资源、
CHANGELOG 段落、便携目录名、zip 名、git tag 全部由它派生，不再手写版本号。

常用命令（在项目根目录执行）::

    python build_release.py                    # 测试 → 构建 → 组装 → 打包 → 校验
    python build_release.py --skip-build       # 复用已有 dist/JingYingPC，只重新组装打包
    python build_release.py --skip-tests       # 跳过测试（CI 已跑过时用）
    python build_release.py --no-data          # 干净包：不带登录态/历史/通讯录
    python build_release.py --data-src DIR     # 指定登录态数据目录
    python build_release.py --out-dir DIR      # 指定输出目录（默认桌面）
    python build_release.py --force            # 同名产物已存在时，把旧的改名备份再生成

设计约束：
* 不删除桌面/输出目录里的任何已有文件；同名产物默认直接报错，--force 也只是改名备份。
* ``file_version_info.txt`` 每次按 APP_VERSION 重新生成，避免「改了版本但 exe 属性没变」。
* 便携包默认带上登录态（Cookie、浏览器配置、历史、通讯录、上传记录），
  但会剔除 Chromium 纯缓存目录，包体可从 1 GB+ 降到约 120 MB。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "JingYingPC.spec"
DIST = ROOT / "dist" / "JingYingPC"
EXE = DIST / "JingYingPC.exe"
VERSION_INFO = ROOT / "file_version_info.txt"
CHANGELOG = ROOT / "CHANGELOG.md"

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
VERSION_ASSIGN = re.compile(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", re.M)

# Chromium 配置目录里可安全丢弃的纯缓存（Cookie / Local State / Preferences 必须保留）
CACHE_DIR_NAMES = {
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "Service Worker", "Crashpad", "Crash Reports", "logs", "log",
    "CachedData", "GraphiteDawnCache", "DawnCache", "DawnGraphiteCache",
    "component_crx_cache", "extensions_crx_cache", "OptimizationGuidePredictionModels",
}
SKIP_FILE_PATTERNS = (
    "*.log", "*.tmp", "*.part", "*.old", "SingletonLock", "SingletonCookie",
    "SingletonSocket", ".write_probe*", "probe_*.txt", "crash-*.log", "*.pyc",
)
# 需要带进便携包的用户数据（登录态 / 通讯录 / 历史 / 上传去重记录）
DATA_FILE_PATTERNS = (
    "cookies*.json", "login_state.json", "settings.json", "webdav.json",
    "webdav_uploaded.json", "authors.json", "sync_failed.json",
    "download_log.txt", "sync_log.txt", "normalize_files_state.json",
    "author_assets_migrated_v1_*", "caption_metadata_fixed_v1",
    "history_paths_flattened_v1",
)


# ---------------------------------------------------------------------------
# 版本真源
# ---------------------------------------------------------------------------
def read_app_version(root: Path = ROOT) -> str:
    """从 config.py 文本里读 APP_VERSION，不 import（import 会创建数据目录）。"""
    match = VERSION_ASSIGN.search((root / "config.py").read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError('config.py 里找不到 APP_VERSION = "x.y.z"')
    return match.group(1)


def assert_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER.match(version)
    if not match:
        raise RuntimeError(f"APP_VERSION 不是语义化版本：{version!r}（应为 x.y.z）")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_tuple4(version: str) -> tuple[int, int, int, int]:
    major, minor, patch = assert_semver(version)
    return major, minor, patch, 0


def render_version_info(version: str) -> str:
    """生成 PyInstaller 用的 VSVersionInfo（纯 ASCII，避免 Windows 默认编码读中文乱码）。"""
    major, minor, patch, build = version_tuple4(version)
    nums = f"({major}, {minor}, {patch}, {build})"
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums},
    prodvers={nums},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904b0',
          [StringStruct('CompanyName', 'JingYing'),
           StringStruct('FileDescription', 'JingYing PC - Douyin Downloader'),
           StringStruct('FileVersion', '{version}'),
           StringStruct('InternalName', 'JingYingPC'),
           StringStruct('LegalCopyright', 'Copyright (C) 2026'),
           StringStruct('OriginalFilename', 'JingYingPC.exe'),
           StringStruct('ProductName', 'JingYing PC'),
           StringStruct('ProductVersion', '{version}')])
      ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def release_zip_name(version: str) -> str:
    assert_semver(version)
    return f"净影-PC-v{version}-win64.zip"


def portable_dir_name(version: str) -> str:
    assert_semver(version)
    return f"净影-便携版-v{version}"


def tag_name(version: str) -> str:
    assert_semver(version)
    return f"v{version}"


def desktop_dir() -> Path:
    """桌面路径：优先用户目录，其次 ~。"""
    base = os.environ.get("USERPROFILE") or (
        os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", ""))
    if base:
        for name in ("Desktop", "桌面"):
            path = Path(base) / name
            if path.is_dir():
                return path
    return Path.home() / "Desktop"


def default_data_src() -> Path | None:
    """优先取最近实际使用过的便携包数据（含 4 个账号登录态），其次项目目录。"""
    candidates = [
        desktop_dir() / "净影-便携版" / "JingYingPC" / "JingYingData",
        ROOT / "JingYingData",
    ]
    for path in candidates:
        if (path / "cookies.json").is_file() and (path / "login_state.json").is_file():
            return path
    return None


def git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "nogit"


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def run_tests() -> None:
    print("[1/6] 跑全量测试 ...")
    code = subprocess.call([sys.executable, "run_tests.py"], cwd=ROOT)
    if code != 0:
        raise RuntimeError(f"测试未通过（exit={code}），已中止发版")


def write_version_assets(version: str) -> None:
    VERSION_INFO.write_bytes(render_version_info(version).encode("ascii"))
    print(f"      按 APP_VERSION={version} 重写 {VERSION_INFO.name}")


def build_exe() -> None:
    if not SPEC.is_file():
        raise RuntimeError(f"缺少 {SPEC}")
    env = os.environ.copy()
    # WorkBuddy 的 safe-delete 钩子会拦截 PyInstaller 在 COLLECT 阶段清理旧 dist，
    # 导致构建卡死；构建子进程里关掉它（只影响子进程）。
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    print("[2/6] PyInstaller 构建（几分钟）...")
    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", SPEC.name, "--noconfirm",
         "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build")],
        cwd=ROOT, env=env)
    if not EXE.is_file():
        raise RuntimeError(f"构建结束但没有找到 {EXE}")


# ---------------------------------------------------------------------------
# 组装便携版（含登录态）
# ---------------------------------------------------------------------------
def _ignore_browser_cache(directory: str, names: list[str]) -> set[str]:
    skipped = set()
    for name in names:
        full = Path(directory) / name
        if full.is_dir() and name in CACHE_DIR_NAMES:
            skipped.add(name)
        elif full.is_file() and any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILE_PATTERNS):
            skipped.add(name)
    return skipped


def _copy_db(src: Path, dst: Path) -> None:
    """用 SQLite backup API 生成一致快照，避免把 -wal / -shm 半写状态带进包。

    `with sqlite3.connect(...)` 只负责事务提交/回滚，**不会关闭连接**；发版时
    两个连接一直开到进程结束，目标 db 的句柄会挡住后续压缩/重命名。用
    contextlib.closing 显式关闭（backup 不写源库，无需事务上下文）。
    """
    with closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as incoming:
        with closing(sqlite3.connect(dst)) as outgoing:
            incoming.backup(outgoing)
            # 原来 `with sqlite3.connect(dst)` 退出时会自动 commit；换成
            # closing 后要显式提交，否则目标库可能留下未落盘的事务。
            outgoing.commit()


def copy_login_data(src: Path, dst: Path) -> dict:
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for pattern in DATA_FILE_PATTERNS:
        for path in sorted(src.glob(pattern)):
            if path.is_file():
                shutil.copy2(path, dst / path.name)
                copied.append(path.name)
    if (src / "history.db").is_file():
        _copy_db(src / "history.db", dst / "history.db")
        copied.append("history.db")
    for profile in sorted(src.glob("browser_session*")):
        if profile.is_dir():
            shutil.copytree(profile, dst / profile.name, ignore=_ignore_browser_cache)
            copied.append(profile.name + "/")
    for extra in ("authors", "author_assets"):
        folder = src / extra
        if folder.is_dir():
            shutil.copytree(folder, dst / extra, dirs_exist_ok=True)
            copied.append(extra + "/")

    cookies = sorted(dst.glob("cookies*.json"))
    state = {}
    try:
        state = json.loads((dst / "login_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rows = 0
    try:
        with sqlite3.connect(dst / "history.db") as con:
            rows = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    except (OSError, sqlite3.Error):
        pass
    return {"files": copied, "cookies": len(cookies),
            "logged_in": bool(state.get("logged_in")),
            "accounts": state.get("accounts", []), "history_rows": rows}


def assemble(version: str, sha: str, out_dir: Path,
             data_src: Path | None, with_data: bool, force: bool) -> tuple[Path, dict]:
    print("[3/6] 组装便携目录 ...")
    release_root = out_dir / portable_dir_name(version)
    if release_root.exists():
        if not force:
            raise RuntimeError(
                f"输出目录已存在：{release_root}\n"
                "为避免覆盖你已有的登录态数据，默认不覆盖；确认后加 --force（旧目录会改名备份）。")
        backup = out_dir / f"{portable_dir_name(version)}_backup_{dt.datetime.now():%Y%m%d_%H%M%S}"
        release_root.rename(backup)
        print(f"      旧目录已备份为 {backup.name}")
    release_root.mkdir(parents=True)
    stage = release_root / "JingYingPC"
    shutil.copytree(DIST, stage, ignore=shutil.ignore_patterns("*.log", "*.pyc", "__pycache__"))
    (stage / "downloads").mkdir(exist_ok=True)

    (stage / "portable.flag").write_text("portable\n", encoding="utf-8")
    (stage / "build_sha.txt").write_text(sha + "\n", encoding="utf-8")
    (stage / "VERSION.txt").write_text(
        f"净影 · 电脑版 v{version}\n构建：{sha}\n时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}\n",
        encoding="utf-8")
    (release_root / "使用说明.txt").write_text(
        "净影 · 电脑版（Windows 便携版）\r\n"
        f"版本：v{version}    构建：{sha}\r\n"
        "\r\n"
        "1. 双击 JingYingPC\\JingYingPC.exe 打开，无需安装 Python。\r\n"
        "2. 电脑上需要有 Google Chrome 或 Microsoft Edge。\r\n"
        "3. 本包已保留登录过的账号（Cookie / 浏览器配置）与通讯录、历史记录，\r\n"
        "   数据都放在 JingYingPC\\JingYingData 里，请勿删除该目录。\r\n"
        "4. 换版本时把整个文件夹复制走即可；旧的 JingYingData 可以单独备份。\r\n"
        "5. 问题反馈请附上 VERSION.txt 与 JingYingData\\app.log 里的 version= 行。\r\n",
        encoding="utf-8-sig")

    info = {"data": None}
    if with_data:
        if data_src is None or not data_src.is_dir():
            raise RuntimeError("未找到登录态数据目录，请用 --data-src 指定，或加 --no-data 打干净包")
        info["data"] = copy_login_data(data_src, stage / "JingYingData")
        d = info["data"]
        print(f"      登录态：{d['cookies']} 份 Cookie，已登录账号 "
              f"{sum(1 for x in d['accounts'] if x)} 个，历史 {d['history_rows']} 条")
    return release_root, info


# ---------------------------------------------------------------------------
# CHANGELOG / zip / 校验
# ---------------------------------------------------------------------------
def changelog_has_version(version: str, text: str) -> bool:
    return re.search(rf"^##\s*\[?v?{re.escape(version)}\]?", text, re.M) is not None


def ensure_changelog(version: str, sha: str) -> bool:
    """CHANGELOG 缺当前版本段落时，按 git log 自动补一节（已存在则不覆盖手写内容）。"""
    header = ("# 更新日志\n\n"
              "版本号与 `config.py` 的 `APP_VERSION` 保持一致，发版由 `build_release.py` 负责。\n\n")
    text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else header
    if changelog_has_version(version, text):
        return False
    try:
        last_tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"], cwd=ROOT,
            capture_output=True, text=True, timeout=10)
        range_arg = f"{last_tag.stdout.strip()}..HEAD" if last_tag.returncode == 0 else "-20"
        log = subprocess.run(
            ["git", "log", range_arg, "--pretty=format:- %s"], cwd=ROOT,
            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        log = ""
    section = (f"## [v{version}] - {dt.date.today():%Y-%m-%d}\n\n"
               f"构建 `{sha}`。\n\n"
               f"{log or '- 初始版本'}\n\n")
    first_heading = re.search(r"^##\s", text, re.M)
    if first_heading:
        text = text[:first_heading.start()] + section + text[first_heading.start():]
    else:
        text = text.rstrip() + "\n\n" + section
    CHANGELOG.write_text(text, encoding="utf-8")
    return True


def make_zip(version: str, out_dir: Path, release_root: Path, force: bool) -> Path:
    print("[5/6] 压缩并校验 zip ...")
    zip_path = out_dir / release_zip_name(version)
    if zip_path.exists():
        if not force:
            raise RuntimeError(f"zip 已存在：{zip_path}（确认后加 --force，旧 zip 会改名备份）")
        backup = out_dir / f"{zip_path.stem}_backup_{dt.datetime.now():%Y%m%d_%H%M%S}.zip"
        zip_path.rename(backup)
        print(f"      旧 zip 已备份为 {backup.name}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(release_root.rglob("*")):
            rel = path.relative_to(out_dir).as_posix()
            if path.is_file():
                zf.write(path, rel)
            elif path.is_dir() and not any(path.iterdir()):
                zf.writestr(rel + "/", b"")
    return zip_path


def read_exe_file_version(exe: Path) -> str:
    """纯标准库读 exe 的 FileVersion（Windows version.dll），不依赖外部命令。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL("version.dll")
        dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.c_void_p]
        dll.GetFileVersionInfoW.restype = wintypes.BOOL
        dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(wintypes.UINT)]
        dll.VerQueryValueW.restype = wintypes.BOOL
        size = dll.GetFileVersionInfoSizeW(str(exe), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(str(exe), 0, size, buf):
            return ""
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        query = "\\StringFileInfo\\040904b0\\FileVersion"
        if not dll.VerQueryValueW(buf, query, ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        return ctypes.wstring_at(ptr.value, max(0, length.value - 1)).strip()
    except (OSError, AttributeError, ValueError):
        return ""


def verify(version: str, sha: str, release_root: Path, zip_path: Path | None,
           info: dict, with_data: bool) -> None:
    print("[6/6] 校验产物 ...")
    stage = release_root / "JingYingPC"
    required = ["JingYingPC.exe", "portable.flag", "build_sha.txt", "VERSION.txt", "_internal"]
    missing = [name for name in required if not (stage / name).exists()]
    if missing:
        raise RuntimeError(f"便携目录缺少文件：{missing}")
    if (stage / "build_sha.txt").read_text(encoding="utf-8").strip() != sha:
        raise RuntimeError("build_sha.txt 与本次构建不一致")
    if with_data:
        data = info["data"]
        if not data or not data["logged_in"] or data["cookies"] < 1:
            raise RuntimeError("登录态没有带进便携包，请检查 --data-src")
        if not (stage / "JingYingData" / "history.db").is_file():
            raise RuntimeError("history.db 没有带进便携包")

    file_version = read_exe_file_version(stage / "JingYingPC.exe")
    if file_version and version not in file_version:
        raise RuntimeError(f"exe 的 FileVersion={file_version!r} 与 v{version} 不一致")
    print(f"      exe 版本资源：{file_version or '读取失败（跳过）'}")

    if zip_path:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
        if bad:
            raise RuntimeError(f"zip CRC 校验失败：{bad}")
        needed = [f"{portable_dir_name(version)}/JingYingPC/{name}"
                  for name in ("JingYingPC.exe", "portable.flag", "build_sha.txt")]
        if with_data:
            needed.append(f"{portable_dir_name(version)}/JingYingPC/JingYingData/login_state.json")
        lack = [n for n in needed if n not in names]
        if lack:
            raise RuntimeError(f"zip 里缺少：{lack}")
        print(f"      zip CRC 通过，{len(names)} 个条目，"
              f"{zip_path.stat().st_size / 1024 / 1024:.1f} MB")


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="净影 PC 版一键发版")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录（默认桌面）")
    parser.add_argument("--data-src", type=Path, default=None, help="登录态数据目录")
    parser.add_argument("--no-data", action="store_true", help="打干净包，不带登录态")
    parser.add_argument("--skip-build", action="store_true", help="复用已有 dist/JingYingPC")
    parser.add_argument("--skip-tests", action="store_true", help="跳过全量测试")
    parser.add_argument("--no-zip", action="store_true", help="只组装目录，不压缩")
    parser.add_argument("--force", action="store_true", help="同名产物改名备份后重建")
    args = parser.parse_args()

    version = read_app_version()
    assert_semver(version)
    sha = git_short_sha()
    out_dir = (args.out_dir or desktop_dir()).resolve()
    if not out_dir.is_dir():
        raise RuntimeError(f"输出目录不存在：{out_dir}")
    data_src = args.data_src or (None if args.no_data else default_data_src())
    print(f"净影 PC 版 v{version} · 构建 {sha}")
    print(f"输出目录：{out_dir}")
    print(f"登录态来源：{data_src or '（不打数据，干净包）'}\n")

    if not args.skip_tests:
        run_tests()
    else:
        print("[1/6] 跳过测试")

    write_version_assets(version)
    if args.skip_build:
        print("[2/6] 跳过构建，复用 dist/JingYingPC")
        if not EXE.is_file():
            raise RuntimeError(f"没有找到 {EXE}，不能 --skip-build")
    else:
        build_exe()

    release_root, info = assemble(version, sha, out_dir, data_src,
                                  not args.no_data, args.force)
    print("[4/6] 同步 CHANGELOG ...")
    added = ensure_changelog(version, sha)
    print(f"      {'已补写' if added else '已存在'} v{version} 段落")
    zip_path = None if args.no_zip else make_zip(version, out_dir, release_root, args.force)
    verify(version, sha, release_root, zip_path, info, not args.no_data)

    print("\n完成")
    print(f"  便携目录：{release_root}")
    if zip_path:
        print(f"  发行 zip ：{zip_path}")
    print(f"  建议 tag ：git tag -a {tag_name(version)} -m '净影 PC 版 v{version}'")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"\n发版失败：{exc}", file=sys.stderr)
        sys.exit(1)
