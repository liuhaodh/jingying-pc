# -*- coding: utf-8 -*-
"""测试公共设置：把数据/下载目录指到临时目录，避免污染真实数据。"""
import contextlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_base = os.path.join(tempfile.gettempdir(), "jingying-pc-tests")
os.environ.setdefault("JINGYING_DATA_DIR", os.path.join(_base, "data"))
os.environ.setdefault("JINGYING_DOWNLOAD_BASE", os.path.join(_base, "downloads", "Videos"))
os.environ.setdefault("JINGYING_MUSIC_BASE", os.path.join(_base, "downloads", "Music"))

import config  # noqa: E402  （必须在环境变量之后导入）


@contextlib.contextmanager
def isolated_config():
    """临时替换 config 里的路径，用完恢复并删除临时目录。"""
    tmp = tempfile.mkdtemp(prefix="jingying-test-")
    saved = {
        "DATA_DIR": config.DATA_DIR,
        "AUTHORS_FILE": config.AUTHORS_FILE,
        "HISTORY_FILE": config.HISTORY_FILE,
        "HISTORY_DB": config.HISTORY_DB,
        "SYNC_LOG_FILE": config.SYNC_LOG_FILE,
        "DOWNLOAD_LOG_FILE": config.DOWNLOAD_LOG_FILE,
        "DOWNLOAD_BASE": config.DOWNLOAD_BASE,
        "MUSIC_BASE": config.MUSIC_BASE,
        "SAVE_ROOT": config.SAVE_ROOT,
        "UPLOAD_ROOT": getattr(config, "UPLOAD_ROOT", None),
        "UPLOAD_DIRS": list(getattr(config, "UPLOAD_DIRS", [])),
        "TEMP_DIR": config.TEMP_DIR,
        "SESSION_DIR": config.SESSION_DIR,
        "SESSION_DIRS": list(getattr(config, "SESSION_DIRS", [])),
        "COOKIE_FILES": list(getattr(config, "COOKIE_FILES", [])),
        "CHROME_PATH": config.CHROME_PATH,
    }
    config.DATA_DIR = os.path.join(tmp, "data")
    config.AUTHORS_FILE = os.path.join(config.DATA_DIR, "authors.json")
    config.HISTORY_FILE = os.path.join(config.DATA_DIR, "history.json")
    config.HISTORY_DB = os.path.join(config.DATA_DIR, "history.db")
    config.SYNC_LOG_FILE = os.path.join(config.DATA_DIR, "sync_log.txt")
    config.DOWNLOAD_LOG_FILE = os.path.join(config.DATA_DIR, "download_log.txt")
    config.DOWNLOAD_BASE = os.path.join(tmp, "Videos")
    config.MUSIC_BASE = os.path.join(tmp, "Music")
    # 新结构：<下载根>/净影/<作者名_抖音号>/<作品id>/文件
    config.SAVE_ROOT = os.path.join(tmp, config.FOLDER_NAME)
    config.UPLOAD_ROOT = tmp
    config.UPLOAD_DIRS = [
        config.SAVE_ROOT, config.DOWNLOAD_BASE,
        config.MUSIC_BASE, os.path.join(tmp, "作者资料"),
    ]
    # 浏览器临时目录也要隔离：清理用例会删里面的旧目录，不能碰真实的 tmp
    config.TEMP_DIR = os.path.join(tmp, "tmp")
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    config.SESSION_DIR = os.path.join(config.DATA_DIR, "browser_session")
    config.SESSION_DIRS = [
        os.path.join(config.DATA_DIR, f"browser_session{'' if i == 0 else '_' + str(i + 1)}")
        for i in range(5)
    ]
    config.COOKIE_FILES = [
        os.path.join(config.DATA_DIR, "cookies.json" if i == 0 else f"cookies_{i + 1}.json")
        for i in range(5)
    ]
    config.CHROME_PATH = ""
    config.ensure_dirs()
    try:
        yield tmp
    finally:
        for key, value in saved.items():
            setattr(config, key, value)
        shutil.rmtree(tmp, ignore_errors=True)
