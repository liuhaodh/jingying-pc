# -*- coding: utf-8 -*-
"""核心逻辑离线测试：parse_aweme + 结构化下载（真实直链）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import downloader
import models

config.DOWNLOAD_BASE = os.path.abspath("test_out/Videos")
config.MUSIC_BASE = os.path.abspath("test_out/Music")
os.makedirs(config.DOWNLOAD_BASE, exist_ok=True)
os.makedirs(config.MUSIC_BASE, exist_ok=True)

raw = json.load(open(
    r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\tools\dyprobe"
    r"\detail-7525082444551310602.json",
    encoding="utf-8"))
detail = models.find_detail(raw)
assert detail is not None, "detail not found"
info = models.parse_aweme(detail)
print("id     ", info.aweme_id)
print("title  ", info.title[:40])
print("author ", info.author, "| uniqueId:", info.unique_id)
print("play   ", info.play_url[:80])
print("music  ", info.music_url[:80])
print("create ", info.create_time, "| hasVideo:", info.has_video())
assert info.has_video(), "video url missing"
assert info.music_url.startswith("https://"), "music url missing: " + repr(info.music_url)

# 用已探测的有效直链做下载测试（视频 + 音乐）
play_url = open(
    r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\tools\dyprobe"
    r"\play-7525082444551310602.txt", encoding="utf-8").read().strip()
info.play_url = play_url
res = downloader.download_work(info, "程序员阿江_relakkes", "程序员阿江_TEST_20261029")
print("archive", res.archive_path, os.path.getsize(res.archive_path))
print("members", res.archive_members)
assert res.archive_path and res.archive_path.endswith(".tar")
assert os.path.getsize(res.archive_path) > 1000000
assert any(m.endswith("视频.mp4") for m in res.archive_members)
assert any(m.endswith("音乐.") for m in res.archive_members)
assert any(m.endswith("文案.txt") for m in res.archive_members)
assert res.music_saved
print("ALL PASS")