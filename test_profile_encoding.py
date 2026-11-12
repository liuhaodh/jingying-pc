# -*- coding: utf-8 -*-
"""用已登录会话测试主页抓取，检查标题编码。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
os.environ.setdefault("JINGYING_CHROME", config.CHROME_PATH)

import parser

SEC = "MS4wLjABAAAAiJTvEnMUiK_yeQjHheh-k1sc1ybVJ6z5NAu-Cg2H2bmmge3UVjZH1tj-dXQ7play"

profile = parser.resolve_profile_sync(SEC)
print("nickname ascii:", ascii(profile.nickname))
print("unique:", profile.unique_id)
print("videos:", len(profile.videos), "albums:", len(profile.albums))
sample = (profile.videos + profile.albums)[:3]
for item in sample:
    print("----")
    print("title ascii:", ascii(item.title[:80]))
    print("author ascii:", ascii(item.author))
    print("unique:", item.unique_id, "album:", item.is_album())