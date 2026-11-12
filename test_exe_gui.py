# -*- coding: utf-8 -*-
"""启动打包后的 exe（detached），检查存活与日志。"""
import os
import subprocess
import sys
import time

exe = r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\jingying-pc\dist\JingYingPC\JingYingPC.exe"
log = r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\jingying-pc\dist\JingYingPC\_internal\data\app.log"
env = dict(os.environ)
env["QT_QPA_PLATFORM"] = "offscreen"
env["JINGYING_CHROME"] = r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\tools\chromium\chrome-win\chrome.exe"
proc = subprocess.Popen(
    [exe],
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
)
time.sleep(10)
alive = proc.poll() is None
print("alive after 10s:", alive, "pid:", proc.pid)
if alive:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("--- log tail ---")
if os.path.exists(log):
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in lines[-8:]:
        print(line.rstrip())