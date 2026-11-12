# -*- coding: utf-8 -*-
"""aweme JSON → MediaInfo 解析。"""
import unittest

from tests.support import ROOT  # noqa: F401

import models


def _detail():
    return {
        "aweme_id": "7525082444551310602",
        "desc": "测试文案 #话题",
        "create_time": 1752069813,
        "ip_label": "浙江",
        "poi_info": {"name": "西湖", "address": "杭州"},
        "author": {
            "nickname": "程序员阿江",
            "sec_uid": "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE",
            "unique_id": "dymm0aterm8v",
            "avatar_thumb": {"url_list": ["https://p3.douyinpic.com/avatar.jpeg"]},
        },
        "statistics": {"digg_count": 10, "comment_count": 2,
                       "collect_count": 3, "share_count": 4},
        "video": {
            "duration": 15000,
            "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
            "bit_rate": [
                {"gear_name": "1080p_HEVC", "bit_rate": 9000000, "width": 1920, "height": 1080,
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/hevc.mp4"]}},
                {"gear_name": "1080p_H264", "bit_rate": 4000000, "width": 1920, "height": 1080,
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/h264.mp4"]}},
                {"gear_name": "720p_H264", "bit_rate": 2000000, "width": 1280, "height": 720,
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/720.mp4"]}},
            ],
        },
        "music": {
            "title": "背景音乐",
            "play_url": {"uri": "https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/1.mp3",
                         "url_list": ["https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/1.mp3"]},
        },
    }


class ModelTests(unittest.TestCase):
    def test_parse_video(self):
        info = models.parse_aweme(_detail())
        self.assertEqual(info.aweme_id, "7525082444551310602")
        self.assertEqual(info.author, "程序员阿江")
        self.assertEqual(info.unique_id, "dymm0aterm8v")
        self.assertEqual(info.ip_location, "浙江")
        self.assertEqual(info.location, "西湖")
        self.assertEqual(info.digg_count, 10)
        self.assertTrue(info.has_video())
        self.assertFalse(info.is_album())
        # 同分辨率下取体积/码率最大的那条（这里是 9M 的 HEVC，不再强制 H.264）
        self.assertTrue(info.play_url.endswith("/hevc.mp4"))
        self.assertEqual(info.quality, "原画 1080p · H.265")
        self.assertEqual(info.width, 1920)
        self.assertTrue(info.music_url.startswith("https://"))
        self.assertEqual(info.music_title, "背景音乐")
        self.assertEqual(info.share_url, "https://www.douyin.com/video/7525082444551310602")

    def test_picks_highest_resolution_stream(self):
        """原画：分辨率最大的那条，而不是 H.264 里码率最高的那条。

        真实抖音详情里原画（2048/2160）几乎只出现在 bit_rate 阶梯的 H.265 项，
        以前的实现优先 H.264，最高只能拿到 1080p。
        """
        detail = _detail()
        detail["video"] = {
            "play_addr": {"url_list": ["https://v11.douyinvod.com/720.mp4"],
                          "width": 780, "height": 720, "data_size": 40203845},
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 833195, "is_h265": 1,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/2160.mp4"],
                                                "width": 2340, "height": 2160,
                                                "data_size": 59895193}},
                {"gear_name": "adapt_lowest_1440_1", "bit_rate": 562955, "is_h265": 1,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/1440.mp4"],
                                                "width": 1560, "height": 1440,
                                                "data_size": 40468659}},
                {"gear_name": "normal_1080_0", "bit_rate": 778933, "is_h265": 0,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/1080.mp4"],
                                                "width": 1170, "height": 1080,
                                                "data_size": 55994498}},
            ],
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/2160.mp4"))
        self.assertEqual((info.width, info.height), (2340, 2160))
        self.assertIn("2160p", info.quality)
        self.assertIn("H.265", info.quality)

    def test_same_resolution_prefers_complete_mp4(self):
        """同分辨率时优先完整 mp4，而不是体积更大的 dash 分段流。"""
        detail = _detail()
        detail["video"] = {
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 833195, "is_h265": 1,
                 "format": "dash", "play_addr": {"url_list": ["https://v11.douyinvod.com/dash.mp4"],
                                                 "width": 2160, "height": 2160,
                                                 "data_size": 90000000}},
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 833195, "is_h265": 1,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/mp4.mp4"],
                                                "width": 2160, "height": 2160,
                                                "data_size": 80000000}},
            ],
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/mp4.mp4"))

    def test_same_resolution_prefers_bigger_file(self):
        """同分辨率、都是完整 mp4 时，体积大的那份赢（哪怕它是 H.264）。"""
        detail = _detail()
        detail["video"] = {
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 833195, "is_h265": 1,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/small-h265.mp4"],
                                                "width": 2160, "height": 2160,
                                                "data_size": 80000000}},
                {"gear_name": "normal_2160_0", "bit_rate": 4000000, "is_h265": 0,
                 "format": "mp4", "play_addr": {"url_list": ["https://v11.douyinvod.com/big-h264.mp4"],
                                                "width": 2160, "height": 2160,
                                                "data_size": 95000000}},
            ],
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/big-h264.mp4"))

    def test_missing_size_does_not_lose_to_lower_bitrate(self):
        """码率高的档位缺 `data_size` 时不能被当成 0，让低码率档反而胜出。

        真实接口里 `bit_rate` 条目偶尔不带 `data_size`；旧排序按体积比较，
        同分辨率下会把这条当成 0 体积，于是选中码率更低的那档——画质变糊
        但界面仍显示「原画」。
        """
        detail = _detail()
        detail["video"] = {
            "bit_rate": [
                {"gear_name": "normal_1080_0", "bit_rate": 1652890, "format": "mp4",
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/high.mp4"],
                               "width": 1080, "height": 1920}},  # 没有 data_size
                {"gear_name": "low_1080_0", "bit_rate": 900000, "format": "mp4",
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/low.mp4"],
                               "width": 1080, "height": 1920,
                               "data_size": 1500000}},
            ],
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/high.mp4"))

    def test_ties_prefer_hevc_master(self):
        """分辨率/码率/体积都相同时取 H.265：那才是原始编码流。"""
        detail = _detail()
        detail["video"] = {
            "bit_rate": [
                {"gear_name": "normal_1080_0", "bit_rate": 1652890, "format": "mp4",
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/h264.mp4"],
                               "width": 1080, "height": 1920,
                               "data_size": 2264254}},
                {"gear_name": "1080_1_1", "bit_rate": 1652890, "is_h265": 1,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v11.douyinvod.com/h265.mp4"],
                               "width": 1080, "height": 1920,
                               "data_size": 2264254}},
            ],
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/h265.mp4"))

    def test_falls_back_to_play_addr_when_no_ladder(self):
        detail = _detail()
        detail["video"] = {
            "ratio": "1080p",
            "play_addr": {"url_list": ["https://v11.douyinvod.com/only.mp4"],
                          "width": 1080, "height": 1920},
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/only.mp4"))
        self.assertIn("1080p", info.quality)

    def test_live_photo_uses_best_stream(self):
        detail = _detail()
        detail.pop("video")
        detail["images"] = [{
            "url_list": ["https://p3.douyinpic.com/a.webp"],
            "video": {"bit_rate": [
                {"gear_name": "low", "bit_rate": 100, "format": "mp4",
                 "play_addr": {"url_list": ["https://v3.douyinvod.com/small.mp4"],
                               "width": 720, "height": 1280, "data_size": 1000}},
                {"gear_name": "high", "bit_rate": 900, "format": "mp4",
                 "play_addr": {"url_list": ["https://v3.douyinvod.com/big.mp4"],
                               "width": 1080, "height": 1920, "data_size": 9000}},
            ]},
        }]
        info = models.parse_aweme(detail)
        self.assertTrue(info.image_videos[0].endswith("/big.mp4"))

    def test_parse_album_with_live(self):
        detail = _detail()
        detail.pop("video")
        detail["images"] = [
            {"url_list": ["https://p3.douyinpic.com/a.webp"],
             "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/live1.mp4"]}}},
            {"url_list": ["https://p3.douyinpic.com/b.jpeg"]},
        ]
        info = models.parse_aweme(detail)
        self.assertTrue(info.is_album())
        self.assertEqual(len(info.image_urls), 2)
        self.assertTrue(info.image_urls[0].endswith(".webp"))
        self.assertEqual(info.image_videos[0], "https://v3.douyinvod.com/live1.mp4")
        self.assertEqual(info.image_videos[1], "")

    def test_clean_url_removes_watermark(self):
        detail = _detail()
        detail["video"] = {
            "play_addr": {"url_list": ["http://v11.douyinvod.com/playwm/abc?watermark=1"]},
        }
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.startswith("https://"))
        self.assertIn("/play/abc", info.play_url)
        self.assertIn("watermark=0", info.play_url)

    def test_fallbacks(self):
        info = models.parse_aweme({"aweme_id": "1"})
        self.assertEqual(info.display_title(), "抖音作品 1")
        self.assertEqual(info.safe_author(), "抖音创作者")
        self.assertFalse(info.has_video())

    def test_find_detail(self):
        detail = {"aweme_id": "1"}
        self.assertIs(models.find_detail({"aweme_detail": detail}), detail)
        self.assertIs(models.find_detail({"data": detail}), None)
        self.assertIsNone(models.find_detail([1, 2]))


if __name__ == "__main__":
    unittest.main()
