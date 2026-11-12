# -*- coding: utf-8 -*-
"""播放器切换测试：模拟滚轮/按键。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
exe_data = os.path.join(config.ROOT, "dist", "JingYingPC", "_internal", "data")
config.DATA_DIR = exe_data
config.HISTORY_FILE = os.path.join(exe_data, "history.json")
config.HISTORY_DB = os.path.join(exe_data, "history.db")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

import store as store_mod
from player import PlayerWindow

qa = QApplication(sys.argv)
items = [x for x in store_mod.Store().load_history()
         if not x.is_album and x.video_path and os.path.exists(x.video_path)]
print("videos:", len(items))
if len(items) < 2:
    print("not enough videos")
    sys.exit(0)
videos = [(x.video_path, x.title, x.author) for x in items[:3]]
w = PlayerWindow(videos)
w.show()
qa.processEvents()
print("start index:", w.index, "counter:", w.counter.text())

# 直接调用 _load
w._load(1)
qa.processEvents()
print("after _load(1):", w.index, w.counter.text(), "source:", w.player.source().toString()[-40:])

# 模拟滚轮
ev = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 0), QPoint(0, -120),
                 Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                 Qt.ScrollPhase.NoScrollPhase, False)
QApplication.sendEvent(w.video_widget, ev)
qa.processEvents()
print("after wheel down:", w.index, w.counter.text())

# 模拟按键
from PySide6.QtGui import QKeyEvent
ke = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(w, ke)
qa.processEvents()
print("after key down:", w.index, w.counter.text())
w.close()