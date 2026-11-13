# -*- coding: utf-8 -*-
"""QListWidget 自定义行生命周期回归。

一轮全量同步会产生上万条任务记录。QListWidget.clear()/takeItem() 只删
QListWidgetItem，不删 setItemWidget 挂上去的 QWidget；不显式回收就会留下
成千上万个孤儿控件挂在 viewport 上，Qt 后续布局/重绘时可能直接
0xc0000005 崩在 QtWidgets.pyd（2026-11-13 实测）。
"""
import unittest

from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QListWidgetItem, QWidget

from tests.support import isolated_config

import transfer


class WidgetLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.panel = transfer.TransferPanel("下载")
        self.addCleanup(self._dispose_panel)

    def _dispose_panel(self):
        self.panel.clear_items()
        self.panel.deleteLater()
        self.qa.processEvents()
        self.panel = None

    @staticmethod
    def _widget_count(list_widget):
        return len(list_widget.viewport().findChildren(QWidget))

    def test_long_run_does_not_leak_row_widgets(self):
        for i in range(400):
            self.panel.add_item(f"job-{i}", f"作品 {i}")
        self.qa.processEvents()
        self.assertLessEqual(self.panel.list.count(), transfer.MAX_VISIBLE_ROWS)
        self.assertLessEqual(len(self.panel._rows), transfer.MAX_VISIBLE_ROWS)
        # 每行 = row widget + 3 个 QLabel + 1 个进度条，留一点余量。
        # 修复前这里会是 (400 - 60) * 5 量级。
        self.assertLessEqual(self._widget_count(self.panel.list),
                             (transfer.MAX_VISIBLE_ROWS + 2) * 6)

    def test_clear_items_disposes_all_row_widgets(self):
        for i in range(25):
            self.panel.add_item(f"k{i}", f"t{i}")
        self.assertGreater(self._widget_count(self.panel.list), 0)
        self.panel.clear_items()
        self.qa.processEvents()
        self.assertEqual(self.panel.list.count(), 0)
        self.assertEqual(self.panel._rows, {})
        self.assertLessEqual(self._widget_count(self.panel.list), 2)

    def test_late_callbacks_after_eviction_are_safe(self):
        for i in range(transfer.MAX_VISIBLE_ROWS + 5):
            self.panel.add_item(f"job-{i}", f"作品 {i}")
            self.panel.update_item(f"job-{i}", "完成")
            self.panel.set_item_progress(f"job-{i}", 100)
        # 最早几行已被回收，迟到的信号不能再去碰失效的 C++ 对象。
        for i in range(5):
            self.panel.update_item(f"job-{i}", "完成")
            self.panel.set_item_progress(f"job-{i}", 50)
        self.qa.processEvents()
        self.assertLessEqual(self.panel.list.count(), transfer.MAX_VISIBLE_ROWS)

    def test_snapshot_skips_invalid_rows(self):
        import shiboken6
        self.panel.add_item("k1", "作品 1")
        # 造一个 C++ 侧已经销毁、Python 侧还被引用的 wrapper：模拟迟到信号
        # 拿到已回收行的情形。不动列表里真正的行，避免污染 Qt 对象树。
        doomed = QWidget()
        shiboken6.delete(doomed)
        self.panel._rows["k1"]["widget"] = doomed
        self.assertEqual(self.panel.snapshot(), [])
        self.assertNotIn("k1", self.panel._rows)
        self.panel.add_item("k1", "作品 1")
        self.assertEqual(len(self.panel.texts()), 1)

    def test_sync_from_removed_rows_are_disposed(self):
        source = transfer.TransferPanel("源")
        mirror = transfer.TransferPanel("镜像")
        try:
            for i in range(10):
                source.add_item(f"k{i}", f"t{i}")
            mirror.sync_from(source, force=True)
            self.qa.processEvents()
            self.assertEqual(mirror.list.count(), 10)
            source.clear_items()
            mirror.sync_from(source, force=True)
            self.qa.processEvents()
            self.assertEqual(mirror.list.count(), 0)
            self.assertEqual(mirror._rows, {})
            self.assertLessEqual(self._widget_count(mirror.list), 2)
        finally:
            source.deleteLater()
            mirror.deleteLater()
            self.qa.processEvents()

    def test_dispose_list_widgets_handles_plain_list(self):
        lw = QListWidget()
        try:
            for i in range(8):
                item = QListWidgetItem()
                lw.addItem(item)
                lw.setItemWidget(item, QLabel(f"行{i}"))
            self.assertEqual(self._widget_count(lw), 8)
            transfer.dispose_list_widgets(lw)
            lw.clear()
            self.qa.processEvents()
            self.assertEqual(self._widget_count(lw), 0)
        finally:
            lw.deleteLater()
            self.qa.processEvents()


if __name__ == "__main__":
    unittest.main()
