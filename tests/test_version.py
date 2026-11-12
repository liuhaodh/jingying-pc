# -*- coding: utf-8 -*-
"""版本体系回归：唯一真源、exe 版本资源、产物命名、CHANGELOG、构建标题。"""
import re
import unittest
from pathlib import Path

import build_release as release
import config

ROOT = Path(__file__).resolve().parent.parent


class VersionSourceTests(unittest.TestCase):
    def test_app_version_is_semver(self):
        self.assertRegex(config.APP_VERSION, r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
        self.assertEqual(len(release.version_tuple4(config.APP_VERSION)), 4)

    def test_build_release_reads_the_same_version(self):
        """发版脚本不 import config，必须与真源读出同一个版本，防止两套版本号。"""
        self.assertEqual(release.read_app_version(ROOT), config.APP_VERSION)

    def test_window_title_source_mode(self):
        """源码运行没有 build_sha.txt，标题保持干净（打包产物才会带短 sha）。"""
        self.assertEqual(config.build_sha(), "")
        self.assertEqual(config.window_title(),
                         f"{config.APP_NAME} · 电脑版 v{config.APP_VERSION}")

    def test_window_title_with_build_sha(self):
        """模拟发行态：带构建短 sha 时标题能区分不同构建。"""
        original = config._BUILD_SHA_CACHE
        try:
            config._BUILD_SHA_CACHE = "a1b2c3d4e5f6"
            self.assertEqual(config.window_title(),
                             f"{config.APP_NAME} · 电脑版 v{config.APP_VERSION} (a1b2c3d4e5f6)")
        finally:
            config._BUILD_SHA_CACHE = original

    def test_version_info_file_matches_app_version(self):
        text = (ROOT / "file_version_info.txt").read_text(encoding="ascii")
        self.assertEqual(text, release.render_version_info(config.APP_VERSION))
        match = re.search(r"filevers=\((\d+), (\d+), (\d+), (\d+)\)", text)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(int(x) for x in match.groups()),
                         release.version_tuple4(config.APP_VERSION))
        self.assertIn(f"StringStruct('FileVersion', '{config.APP_VERSION}')", text)
        self.assertIn(f"StringStruct('ProductVersion', '{config.APP_VERSION}')", text)

    def test_rendered_version_info_is_ascii(self):
        """PyInstaller 用系统默认编码读版本资源，中文会乱码，必须保持纯 ASCII。"""
        release.render_version_info(config.APP_VERSION).encode("ascii")
        self.assertIn("(2, 3, 4, 0)", release.render_version_info("2.3.4"))

    def test_spec_uses_version_info(self):
        spec = (ROOT / "JingYingPC.spec").read_text(encoding="utf-8")
        self.assertIn("file_version_info.txt", spec)
        self.assertIn("**_version_kwargs", spec)

    def test_release_names_are_derived_from_version(self):
        self.assertEqual(release.release_zip_name(config.APP_VERSION),
                         f"净影-PC-v{config.APP_VERSION}-win64.zip")
        self.assertEqual(release.portable_dir_name(config.APP_VERSION),
                         f"净影-便携版-v{config.APP_VERSION}")
        self.assertEqual(release.tag_name(config.APP_VERSION), f"v{config.APP_VERSION}")
        with self.assertRaises(RuntimeError):
            release.release_zip_name("not-a-version")

    def test_changelog_has_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(release.changelog_has_version(config.APP_VERSION, text))

    def test_app_logs_and_window_title_use_single_source(self):
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("config.window_title()", app)
        self.assertIn('logging.info("version=%s build=%s"', app)


if __name__ == "__main__":
    unittest.main()
