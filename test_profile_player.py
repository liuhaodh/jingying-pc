# -*- coding: utf-8 -*-
"""主页未登录检测 + 播放器初始化测试。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parser
from player import PlayerWindow
from PySide6.QtWidgets import QApplication

SEC_UID = "MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE"

print("--- profile (expected NeedLogin) ---")
try:
    profile = parser.resolve_profile_sync(SEC_UID, stage=lambda t: None)
    print("unexpected success:", profile.nickname, len(profile.videos), len(profile.albums))
except parser.NeedLogin as e:
    print("NeedLogin OK:", e)
except Exception as e:  # noqa: BLE001
    print("other error:", type(e).__name__, e)

print("--- player init ---")
qa = QApplication(sys.argv)
video = r"C:\Users\liuhaokan\Documents\deepseek-harness\default-workspace\tools\dyprobe\video-7695044955277987091.mp4"
if os.path.exists(video):
    player = PlayerWindow([(video, "测试标题", "测试作者")])
    player.show()
    from PySide6.QtCore import QTimer
    QTimer.singleShot(2500, qa.quit)
    qa.exec()
    player.close()
    print("PLAYER OK")
else:
    print("video sample missing, skipped")