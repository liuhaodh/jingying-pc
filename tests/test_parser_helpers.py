# -*- coding: utf-8 -*-
"""parser 的纯逻辑部分：目录锁取哪个目录、Cookie 是否过期、临时目录清理。

这些都不需要真的启动浏览器，可以在本机离线跑。
"""
import os
import time
import unittest

from tests.support import isolated_config

import config
import parser


class SessionDirBindingTests(unittest.TestCase):
    def test_positional_session_dir_is_used_for_lock(self):
        """位置传参也要取到 session_dir。

        以前只读 kwargs，而 `resolve_profile_sync` / `check_login_status_sync`
        都是位置传参，结果账号 2~5 的任务全去抢账号 1 的目录锁：既白白串行，
        又可能让一次登录检查被别的账号的浏览器拖到超时。
        """
        key = parser._bound_session_dir(
            parser.resolve_profile, ("sec", None, 1000, "D:/session_2", "c2.json"), {})
        self.assertEqual(key, "D:/session_2")
        key = parser._bound_session_dir(
            parser.resolve_profile, ("sec",), {"session_dir": "D:/session_3"})
        self.assertEqual(key, "D:/session_3")
        # 不传就用默认目录
        self.assertEqual(parser._bound_session_dir(parser.resolve_video, ("url",), {}),
                         parser.SESSION_DIR)
        self.assertIs(parser._lock_for("D:/session_2"),
                      parser._lock_for("D:/session_2"))
        self.assertIsNot(parser._lock_for("D:/session_2"),
                         parser._lock_for("D:/session_3"))


class LoginCookieTests(unittest.TestCase):
    def test_session_cookie_without_expires_is_alive(self):
        self.assertTrue(parser._cookie_alive({"name": "sessionid", "value": "x"}))

    def test_login_cookie_detection(self):
        self.assertTrue(parser._has_login_cookie([{"name": "sessionid", "value": "x"}]))
        self.assertTrue(parser._has_login_cookie([{"name": "sid_tt", "value": "x"}]))
        self.assertFalse(parser._has_login_cookie(
            [{"name": "passport_csrf_token", "value": "x"}]))

    def test_expired_login_cookie_is_not_logged_in(self):
        """过期的登录 Cookie 不能再算「已登录」。

        以前只看名字：服务端会话过期后界面仍显示已登录，同步整轮报「需要登录」，
        重启又变回「已登录」，来回矛盾。
        """
        past = time.time() - 3600
        future = time.time() + 3600
        self.assertFalse(parser._has_login_cookie(
            [{"name": "sessionid", "value": "x", "expires": past}]))
        self.assertTrue(parser._has_login_cookie(
            [{"name": "sessionid", "value": "x", "expires": future}]))
        # value 为空不算登录
        self.assertFalse(parser._has_login_cookie(
            [{"name": "sessionid", "value": "", "expires": future}]))


class TempProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_removes_only_old_browser_profiles(self):
        os.makedirs(config.TEMP_DIR, exist_ok=True)
        old = os.path.join(config.TEMP_DIR, "jy_profile_old")
        fresh = os.path.join(config.TEMP_DIR, "jy_profile_new")
        other = os.path.join(config.TEMP_DIR, "keep_me")
        for path in (old, fresh, other):
            os.makedirs(path)
        stale = time.time() - 5 * 86400
        os.utime(old, (stale, stale))
        removed = parser.cleanup_temp_profiles(max_age_days=2.0)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(other))

    def test_removes_playwright_temp_profiles(self):
        """Playwright 自己建的临时浏览器目录也要清（本机实测堆了 66 个 / 306 MB）。"""
        os.makedirs(config.TEMP_DIR, exist_ok=True)
        stale = time.time() - 9 * 86400
        targets = ["playwright_chromiumdev_profile-abc",
                   "playwright-artifacts-xyz",
                   "jy_cookie_1"]
        others = ["playwright_report.txt", "msedge_BITS_1"]
        for name in targets + others:
            path = os.path.join(config.TEMP_DIR, name)
            os.makedirs(path)
            os.utime(path, (stale, stale))
        removed = parser.cleanup_temp_profiles(max_age_days=2.0, limit=10)
        self.assertEqual(removed, 3)
        for name in targets:
            self.assertFalse(os.path.exists(os.path.join(config.TEMP_DIR, name)))
        for name in others:
            self.assertTrue(os.path.exists(os.path.join(config.TEMP_DIR, name)))

    def test_limit_bounds_one_run(self):
        os.makedirs(config.TEMP_DIR, exist_ok=True)
        stale = time.time() - 9 * 86400
        for i in range(5):
            path = os.path.join(config.TEMP_DIR, f"jy_profile_{i}")
            os.makedirs(path)
            os.utime(path, (stale, stale))
        self.assertEqual(parser.cleanup_temp_profiles(max_age_days=2.0, limit=2), 2)

    def test_default_limit_clears_a_real_backlog(self):
        """默认上限要够清掉积压：本机实测堆过 44 个（约 243 MB）。

        旧默认值是 12，积压要启动四次才清得完，期间磁盘一直占着几百 MB。
        """
        os.makedirs(config.TEMP_DIR, exist_ok=True)
        stale = time.time() - 9 * 86400
        for i in range(20):
            path = os.path.join(config.TEMP_DIR,
                                f"playwright_chromiumdev_profile-{i:02d}")
            os.makedirs(path)
            os.utime(path, (stale, stale))
        self.assertEqual(parser.cleanup_temp_profiles(max_age_days=2.0), 20)
        self.assertEqual(
            [n for n in os.listdir(config.TEMP_DIR) if n.startswith("playwright_")],
            [])

    def test_oldest_dirs_are_removed_first(self):
        """一次删不完时留下最新的，而不是随机剩下更旧的。"""
        os.makedirs(config.TEMP_DIR, exist_ok=True)
        now = time.time()
        old = os.path.join(config.TEMP_DIR, "jy_profile_old")
        newer = os.path.join(config.TEMP_DIR, "jy_profile_newer")
        for path, age in ((old, 9 * 86400), (newer, 5 * 86400)):
            os.makedirs(path)
            os.utime(path, (now - age, now - age))
        self.assertEqual(parser.cleanup_temp_profiles(max_age_days=2.0, limit=1), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(newer))


if __name__ == "__main__":
    unittest.main()
