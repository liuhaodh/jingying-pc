# -*- coding: utf-8 -*-
"""检查 UI 控件状态：历史/通讯录条数与几何尺寸。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
# 指向 exe 当前的数据目录
exe_data = os.path.join(config.ROOT, "dist", "JingYingPC", "_internal", "data")
config.DATA_DIR = exe_data
config.AUTHORS_FILE = os.path.join(exe_data, "authors.json")
config.HISTORY_FILE = os.path.join(exe_data, "history.json")
config.HISTORY_DB = os.path.join(exe_data, "history.db")

import app
from PySide6.QtWidgets import QApplication

qa = QApplication(sys.argv)
w = app.MainWindow()
w.resize(560, 950)
w.show()
for _ in range(30):
    qa.processEvents()

print("history items:", w.history_list.count())
for i in range(min(3, w.history_list.count())):
    print("  item:", ascii(w.history_list.item(i).text()[:60]))
w._open_history_window()
for _ in range(10):
    qa.processEvents()
print("history_list size:", w.history_list.width(), "x", w.history_list.height())
print("history window size:", w.history_window.width(), "x", w.history_window.height())
print("contacts items:", w.contact_list.count())
w._open_contacts_window()
for _ in range(10):
    qa.processEvents()
print("contact_list size:", w.contact_list.width(), "x", w.contact_list.height())
print("contacts window size:", w.contacts_window.width(), "x", w.contacts_window.height())
for a in w.store.load_authors():
    print("  author ascii:", ascii(a.nickname), a.unique_id)
h = w.store.load_history()
print("videos in history:", sum(1 for x in h if not x.is_album and x.video_path))
print("albums in history:", sum(1 for x in h if x.is_album))
print("video paths exist:", sum(1 for x in h if x.video_path and os.path.exists(x.video_path)))