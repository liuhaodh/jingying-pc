# -*- coding: utf-8 -*-
"""净影 PC 版测试入口：`python run_tests.py`

覆盖链接解析、aweme 解析、持久化、下载器（含断点续传）以及离屏 GUI 冒烟。
测试数据全部写到系统临时目录，不会碰真实下载目录和通讯录。
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

_BASE = os.path.join(tempfile.gettempdir(), "jingying-pc-tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
os.environ["JINGYING_DATA_DIR"] = os.path.join(_BASE, "data")
os.environ["JINGYING_DOWNLOAD_BASE"] = os.path.join(_BASE, "downloads", "Videos")
os.environ["JINGYING_MUSIC_BASE"] = os.path.join(_BASE, "downloads", "Music")
os.environ["JINGYING_SESSION_DIR"] = os.path.join(_BASE, "data", "browser_session")


def main():
    suite = unittest.defaultTestLoader.discover(
        os.path.join(ROOT, "tests"), pattern="test_*.py", top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_BASE, ignore_errors=True)
    sys.exit(code)
