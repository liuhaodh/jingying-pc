# -*- coding: utf-8 -*-
"""「导入 txt」按文件对齐通讯录：批量删除、确认保护与接线回归。"""
import inspect
import unittest
from unittest import mock

from tests.support import isolated_config

import store as store_mod

SID_A = "MS4wLjABAAAA" + "A" * 30
SID_B = "MS4wLjABAAAA" + "B" * 30
SID_C = "MS4wLjABAAAA" + "C" * 30
SID_D = "MS4wLjABAAAA" + "D" * 30


def text_of(*sids):
    return "\n".join(f"https://www.douyin.com/user/{sid}" for sid in sids)


class _StoreFixture:
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self.store = store_mod.Store()
        for sid, name in ((SID_A, "甲"), (SID_B, "乙"), (SID_C, "丙")):
            self.store.add_author(store_mod.Author(sid, name, "", ""),
                                  flush=False)
        self.store.flush(block=True)

    def tearDown(self):
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def ids(self):
        # add_author 是 insert(0, ...)，通讯录里 newest-first；断言只关心集合，
        # 排序后比较，免得被内部顺序绑死。
        return sorted(a.sec_uid for a in self.store.load_authors())


class StorePruneTests(_StoreFixture, unittest.TestCase):
    def test_authors_not_in_is_read_only(self):
        stale = self.store.authors_not_in([SID_A, SID_C])
        self.assertEqual([a.sec_uid for a in stale], [SID_B])
        self.assertEqual(len(self.ids()), 3)

    def test_authors_not_in_keep_nothing_returns_all(self):
        self.assertEqual(len(self.store.authors_not_in([])), 3)

    def test_authors_not_in_keep_all_returns_none(self):
        self.assertEqual(self.store.authors_not_in([SID_A, SID_B, SID_C]), [])

    def test_remove_authors_is_bulk_and_persisted(self):
        self.assertEqual(self.store.remove_authors([SID_A, SID_C]), 2)
        self.assertEqual(self.ids(), [SID_B])
        # 换一个 Store 重新读盘，确认真的落盘而不是只改了内存
        other = store_mod.Store()
        try:
            self.assertEqual([a.sec_uid for a in other.load_authors()], [SID_B])
        finally:
            other.close()

    def test_remove_authors_noop_and_idempotent(self):
        self.assertEqual(self.store.remove_authors([]), 0)
        self.assertEqual(self.store.remove_authors(["NOPE"]), 0)
        self.assertEqual(len(self.ids()), 3)
        self.assertEqual(self.store.remove_authors([SID_A]), 1)
        self.assertEqual(self.store.remove_authors([SID_A]), 0)
        self.assertEqual(self.ids(), [SID_B, SID_C])


class ImportBehaviorTests(_StoreFixture, unittest.TestCase):
    """用 MagicMock 当 self 调未绑定方法：只让 store 和 links 是真的。"""

    def _host(self, syncing=False):
        import app
        host = mock.MagicMock()
        host.store = self.store
        host.syncing = syncing
        # 模拟同步失败队列里正好有待删的作者
        host._sync_failed_objs = [store_mod.Author(SID_C, "丙", "", "")]
        return host, app

    def _run(self, host, app, text, prune=True, yes=True):
        with mock.patch.object(app, "QMessageBox") as box:
            box.question.return_value = (box.StandardButton.Yes if yes
                                         else box.StandardButton.No)
            app.MainWindow._import_authors_text(
                host, text, "txt 导入", prune=prune)
        return box

    def test_confirm_removes_absent_authors_and_adds_new(self):
        host, app = self._host()
        self._run(host, app, text_of(SID_A, SID_D))
        self.assertEqual(self.ids(), [SID_A, SID_D])
        # 删除者的失败重试记录也要清掉
        self.assertEqual(
            [a.sec_uid for a in host._sync_failed_objs], [])
        host._save_failed_authors.assert_called_once()
        host._update_retry_button.assert_called_once()
        host._render_contacts.assert_called_once()

    def test_cancel_leaves_contacts_untouched(self):
        host, app = self._host()
        self._run(host, app, text_of(SID_A, SID_D), yes=False)
        self.assertEqual(self.ids(), [SID_A, SID_B, SID_C])
        self.assertEqual([a.sec_uid for a in host._sync_failed_objs], [SID_C])
        host._render_contacts.assert_not_called()

    def test_syncing_blocks_the_whole_import(self):
        host, app = self._host(syncing=True)
        box = self._run(host, app, text_of(SID_A, SID_D))
        self.assertEqual(self.ids(), [SID_A, SID_B, SID_C])
        box.information.assert_called_once()
        box.question.assert_not_called()

    def test_batch_import_remains_additive(self):
        host, app = self._host()
        self._run(host, app, text_of(SID_A, SID_D), prune=False)
        self.assertEqual(self.ids(), [SID_A, SID_B, SID_C, SID_D])

    def test_unparseable_text_deletes_nothing(self):
        host, app = self._host()
        box = self._run(host, app, "这里没有任何主页链接")
        self.assertEqual(self.ids(), [SID_A, SID_B, SID_C])
        box.warning.assert_called_once()
        box.question.assert_not_called()

    def test_all_present_authors_need_no_confirmation(self):
        host, app = self._host()
        box = self._run(host, app, text_of(SID_A, SID_B, SID_C))
        self.assertEqual(self.ids(), [SID_A, SID_B, SID_C])
        box.question.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_only_txt_import_enables_prune(self):
        import app
        txt_src = inspect.getsource(app.MainWindow._import_authors_txt)
        batch_src = inspect.getsource(app.MainWindow._batch_import_authors)
        self.assertIn("prune=True", txt_src)
        self.assertNotIn("prune=True", batch_src)

    def test_confirmation_happens_before_deletion(self):
        import app
        src = inspect.getsource(app.MainWindow._import_authors_text)
        self.assertIn("QMessageBox.question", src)
        self.assertIn("remove_authors", src)
        self.assertIn("不会删除已下载作品", src)
        self.assertIn("self.syncing", src)
        self.assertLess(src.index("QMessageBox.question"),
                        src.index("remove_authors"))


if __name__ == "__main__":
    unittest.main()
