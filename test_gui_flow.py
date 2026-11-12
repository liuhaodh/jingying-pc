# -*- coding: utf-8 -*-
"""GUI 端到端测试（offscreen）：输入视频链接 → 解析 → 结果卡片。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

qa = QApplication(sys.argv)
w = app.MainWindow()
w.show()
w.input.setPlainText("https://www.douyin.com/video/7525082444551310602")
w._parse()


def check():
    print("worker running:", w.worker.isRunning())
    print("status:", w.status.text())
    print("result visible:", w.result_card.isVisible())
    if w.current:
        print("aweme:", w.current.aweme_id)
        print("author:", w.current.safe_author())
        print("uniqueId:", w.current.unique_id)
        print("play:", bool(w.current.play_url))
        print("music:", bool(w.current.music_url))
    qa.quit()


QTimer.singleShot(60000, check)
qa.exec()
print("FLOW DONE")