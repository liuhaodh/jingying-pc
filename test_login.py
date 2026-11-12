# -*- coding: utf-8 -*-
"""登录流程冒烟：确认有头浏览器能打开，且取消后能正常退出。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
os.environ.setdefault("JINGYING_CHROME", config.CHROME_PATH)

import parser

stop = threading.Event()
result = {}


def worker():
    try:
        parser.login_sync(stop, 120)
        result["ok"] = True
        result["error"] = ""
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(e)


thread = threading.Thread(target=worker, daemon=True)
thread.start()
time.sleep(10)

# 检查有头 chromium 进程是否起来
import subprocess
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                     capture_output=True, text=True, errors="replace").stdout
print("chrome processes:", out.count("chrome.exe"))

stop.set()
thread.join(30)
print("thread alive:", thread.is_alive())
print("result:", result)