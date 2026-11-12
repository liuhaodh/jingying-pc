# -*- coding: utf-8 -*-
"""排查标题乱码：抓取真实作品，检查 raw detail 与解析结果的编码。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
os.environ.setdefault("JINGYING_CHROME", config.CHROME_PATH)

import parser
import models

info = parser.resolve_video_sync("https://www.douyin.com/video/7525082444551310602")
print("title ascii:", ascii(info.title))
print("author ascii:", ascii(info.author))
print("unique:", info.unique_id)

# 尝试从乱码恢复
def repair(s):
    try:
        return s.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return ""

print("title repaired:", repair(info.title))
print("author repaired:", repair(info.author))

# 看 detail 原文的一段
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        config.SESSION_DIR, headless=True, executable_path=config.CHROME_PATH,
        args=["--headless=new", "--no-sandbox", "--disable-gpu"],
        viewport={"width": 1280, "height": 900}, user_agent=config.DESKTOP_UA)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.add_init_script(open("resolver.js", encoding="utf-8").read())
    page.goto("https://www.douyin.com/video/7525082444551310602", wait_until="domcontentloaded")
    detail = ""
    for _ in range(40):
        detail = page.evaluate("() => (window.__JY_STATE__ || {}).detail || ''")
        if detail:
            break
        page.wait_for_timeout(500)
    ctx.close()

print("detail len:", len(detail))
if detail:
    idx = detail.find('"desc"')
    print("raw desc snippet:", detail[idx:idx + 120])
    root = json.loads(detail)
    d = models.find_detail(root)
    raw_title = d.get("desc")
    print("json title ascii:", ascii(raw_title[:60]))
    print("json title bytes utf8:", raw_title[:12].encode("utf-8"))
    try:
        print("repaired json title:", raw_title.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")[:60])
    except Exception as e:
        print("repair fail", e)