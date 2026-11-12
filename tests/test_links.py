# -*- coding: utf-8 -*-
"""链接与分享文案解析。"""
import unittest

from tests.support import ROOT  # noqa: F401  （设置路径与临时目录）

import links


SEC_UID = "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE"
VIDEO_URL = "https://www.douyin.com/video/7525082444551310602"


class LinkTests(unittest.TestCase):
    def test_video_id_from_url(self):
        self.assertEqual(links.extract_aweme_id(VIDEO_URL), "7525082444551310602")
        self.assertEqual(links.extract_aweme_id(VIDEO_URL + "?previous_page=app_code_link"),
                         "7525082444551310602")
        self.assertEqual(links.extract_aweme_id("/note/7597000314998344994"), "7597000314998344994")

    def test_video_id_from_share_text(self):
        text = "7.68 复制打开抖音，看看【作者的作品】 https://www.douyin.com/video/7525082444551310602 复制此链接"
        self.assertEqual(links.extract_aweme_id(text), "7525082444551310602")

    def test_video_id_from_modal_id(self):
        self.assertEqual(
            links.extract_aweme_id("https://www.douyin.com/discover?modal_id=7525082444551310602"),
            "7525082444551310602")

    def test_pure_id(self):
        self.assertEqual(links.extract_aweme_id("7525082444551310602"), "7525082444551310602")
        self.assertEqual(links.extract_aweme_id("123"), "")

    def test_sec_uid(self):
        self.assertEqual(links.extract_sec_uid(SEC_UID), SEC_UID)
        self.assertEqual(links.extract_sec_uid("https://www.douyin.com/user/" + SEC_UID), SEC_UID)
        self.assertEqual(links.extract_sec_uid(f"看看这个 https://www.douyin.com/user/{SEC_UID}?from=x"),
                         SEC_UID)
        self.assertEqual(links.extract_sec_uid(VIDEO_URL), "")

    def test_short_link(self):
        self.assertTrue(links.is_short_link("https://v.douyin.com/iRNBho6u/"))
        self.assertFalse(links.is_short_link(VIDEO_URL))

    def test_extract_url_strips_punctuation(self):
        self.assertEqual(
            links.extract_url("链接：https://v.douyin.com/iRNBho6u/，复制打开"),
            "https://v.douyin.com/iRNBho6u/")
        self.assertEqual(links.extract_url("没有链接"), "")

    def test_profile_input(self):
        self.assertTrue(links.is_profile_input("https://www.douyin.com/user/" + SEC_UID))
        self.assertFalse(links.is_profile_input(VIDEO_URL))

    def test_extract_all_sec_uids(self):
        text = "\n".join([
            "https://www.douyin.com/user/" + SEC_UID,
            "https://www.douyin.com/user/" + SEC_UID,
            "另一个 " + SEC_UID.replace("TJPY", "AAAA"),
        ])
        found = links.extract_all_sec_uids(text)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], SEC_UID)

    def test_builders(self):
        self.assertEqual(links.video_page_url("123"), "https://www.douyin.com/video/123")
        self.assertEqual(links.profile_page_url(SEC_UID), "https://www.douyin.com/user/" + SEC_UID)


if __name__ == "__main__":
    unittest.main()
