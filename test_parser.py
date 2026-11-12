# -*- coding: utf-8 -*-
"""解析链路验证：playwright + 本地 Chromium 抓取真实作品详情。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import models

RESOLVER = open("resolver.js", encoding="utf-8").read()
CHROME = config.CHROME_PATH
print("chrome:", CHROME, "exists:", os.path.exists(CHROME))
assert CHROME and os.path.exists(CHROME), "chromium not found; set JINGYING_CHROME"

from playwright.sync_api import sync_playwright  # noqa: E402

VIDEO_ID = "7525082444551310602"
_session_dir = config.SESSION_DIR
os.makedirs(_session_dir, exist_ok=True)

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        _session_dir,
        headless=True,
        executable_path=CHROME,
        args=["--no-first-run", "--no-default-browser-check", "--no-sandbox",
              "--disable-gpu", "--disable-dev-shm-usage", "--headless=new"],
        viewport={"width": 1280, "height": 900},
        user_agent=config.DESKTOP_UA,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.add_init_script(RESOLVER)
    page.set_default_timeout(20000)
    page.goto(f"https://www.douyin.com/video/{VIDEO_ID}", wait_until="domcontentloaded")
    detail = ""
    for _ in range(60):
        state = page.evaluate("() => (window.__JY_STATE__ || {}).detail || ''")
        if state:
            detail = state
            break
        page.wait_for_timeout(700)
    print("detail len:", len(detail))
    if detail:
        root = json.loads(detail)
        d = models.find_detail(root)
        info = models.parse_aweme(d)
        print("title:", info.title[:40])
        print("author:", info.author, "| uniqueId:", info.unique_id)
        print("play:", info.play_url[:80])
        print("music:", info.music_url[:80])
        print("create:", info.create_time, "| hasVideo:", info.has_video())
    context.close()

print("PARSER OK" if detail else "PARSER NO DETAIL")