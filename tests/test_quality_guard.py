# -*- coding: utf-8 -*-
"""原画质保障：阶梯择优、去水印、降级标注、下载日志画质。

对应 HANDOFF 文档里的三条：
- 去水印 = `/playwm/` → `/play/`（地址变体替换，不重编码）
- 原画 = `bit_rate[]` 阶梯按 (面积, 完整 mp4, 码率, 体积, H.265) 择优
- 只拿到兜底地址时必须显式标注，不能假装是原画
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import download_log
import models
import parser


def _detail(video):
    return {
        "aweme_id": "7525082444551310602",
        "desc": "测试作品",
        "author": {"nickname": "作者", "sec_uid": "sec", "unique_id": "uid"},
        "video": video,
    }


class StreamPickTests(unittest.TestCase):
    """清晰度阶梯的择优规则（排序键顺序不能改）。"""

    def test_picks_2160p_from_bit_rate_ladder(self):
        detail = _detail({
            "play_addr": {"url_list": ["https://v.example.com/720.mp4"],
                          "width": 780, "height": 720, "data_size": 40_000_000},
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 8_331_951, "is_h265": 1,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/2160.mp4"],
                               "width": 2340, "height": 2160, "data_size": 59_895_193}},
                {"gear_name": "normal_1080_0", "bit_rate": 778_933, "is_h265": 0,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/1080.mp4"],
                               "width": 1170, "height": 1080, "data_size": 55_994_498}},
            ],
        })
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/2160.mp4"))
        self.assertEqual((info.width, info.height), (2340, 2160))
        self.assertIn("2160p", info.quality)

    def test_bitrate_beats_missing_data_size(self):
        """部分 bit_rate 条目不带 data_size：按体积比会把它当 0，反而选中低码率档。"""
        detail = _detail({
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 8_331_951, "is_h265": 1,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/hi.mp4"],
                               "width": 2160, "height": 2160}},
                {"gear_name": "normal_2160_0", "bit_rate": 1_220_000, "is_h265": 0,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/lo.mp4"],
                               "width": 2160, "height": 2160, "data_size": 99_000_000}},
            ],
        })
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/hi.mp4"))

    def test_strips_watermark_variant(self):
        """去水印就是把 playwm 换成 play，不重新编码、不掉画质。"""
        detail = _detail({
            "play_addr": {"url_list": [
                "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200&watermark=1"],
                "width": 1920, "height": 1080},
        })
        info = models.parse_aweme(detail)
        self.assertIn("/aweme/v1/play/", info.play_url)
        self.assertNotIn("playwm", info.play_url)
        self.assertIn("watermark=0", info.play_url)

    def test_same_resolution_prefers_complete_mp4_over_dash(self):
        detail = _detail({
            "bit_rate": [
                {"gear_name": "adapt_lowest_4_1", "bit_rate": 8_000_000, "is_h265": 1,
                 "format": "dash",
                 "play_addr": {"url_list": ["https://v.example.com/dash.mp4"],
                               "width": 2160, "height": 2160, "data_size": 60_000_000}},
                {"gear_name": "normal_2160_0", "bit_rate": 7_000_000, "is_h265": 0,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/mp4.mp4"],
                               "width": 2160, "height": 2160, "data_size": 55_000_000}},
            ],
        })
        info = models.parse_aweme(detail)
        self.assertTrue(info.play_url.endswith("/mp4.mp4"))

    def test_h265_detected_from_gear_name(self):
        """有的响应不填 is_h265，只在 gear_name 里写 265/HEVC。"""
        detail = _detail({
            "bit_rate": [
                {"gear_name": "normal_2160_265", "bit_rate": 8_000_000,
                 "format": "mp4",
                 "play_addr": {"url_list": ["https://v.example.com/x.mp4"],
                               "width": 2160, "height": 2160, "data_size": 60_000_000}},
            ],
        })
        info = models.parse_aweme(detail)
        self.assertIn("H.265", info.quality)


class FallbackQualityTests(unittest.TestCase):
    """抓不到详情接口时只能拿播放地址，必须标注降级。"""

    def test_fallback_marks_low_quality(self):
        info = parser._fallback_info("7525082444551310602",
                                     "https://v.example.com/play.mp4")
        self.assertEqual(info.aweme_id, "7525082444551310602")
        self.assertTrue(info.play_url.endswith("/play.mp4"))
        self.assertEqual(info.quality, parser.FALLBACK_QUALITY)
        self.assertIn("兜底", parser.FALLBACK_QUALITY)

    def test_fallback_keeps_aweme_id_and_tolerates_empty_url(self):
        info = parser._fallback_info("7525082444551310699", "")
        self.assertEqual(info.aweme_id, "7525082444551310699")
        self.assertEqual(info.quality, parser.FALLBACK_QUALITY)


class DownloadLogQualityTests(unittest.TestCase):
    """下载日志要写出画质，用户能核对下到的是不是原画；不传时保持原格式。"""

    def test_line_contains_quality(self):
        line = download_log.format_line(
            "作者甲", "作品", 3, when="2026-11-12 10:00:00",
            quality="原画 2160p · H.265")
        self.assertIn("画质 原画 2160p · H.265", line)
        self.assertIn("还剩 3 个没下完", line)

    def test_line_without_quality_keeps_old_format(self):
        line = download_log.format_line(
            "作者甲", "作品", 0, when="2026-11-12 10:00:00")
        self.assertEqual(
            line,
            "[2026-11-12 10:00:00] 下载完成 作者甲 的《作品》，该作者已全部下完")


if __name__ == "__main__":
    unittest.main()
