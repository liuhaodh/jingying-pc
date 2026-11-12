# -*- coding: utf-8 -*-
"""通讯录 / 历史持久化与并发。"""
import json
import os
import threading
import unittest

from tests.support import isolated_config

import config
import store as store_mod


def _item(aweme_id, title="作品"):
    item = store_mod.HistoryItem()
    item.aweme_id = aweme_id
    item.title = title
    item.author = "作者"
    item.video_path = os.path.join(config.DOWNLOAD_BASE, "视频.mp4")
    item.saved_at = 1
    return item


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self.store = store_mod.Store()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_history_add_and_order(self):
        self.store.add_history(_item("1", "第一条"))
        self.store.add_history(_item("2", "第二条"))
        items = self.store.load_history()
        self.assertEqual([x.aweme_id for x in items], ["2", "1"])

    def test_history_dedupe(self):
        self.store.add_history(_item("1", "旧标题"))
        self.store.add_history(_item("1", "新标题"))
        items = self.store.load_history()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "新标题")

    def test_history_index_survives_reload_and_clear(self):
        """去重索引在 merge/save/clear 之后仍然正确，不会残留已删除的条目。"""
        self.store.add_history(_item("1", "旧"))
        self.store.merge_history([_item("2")])
        self.store.add_history(_item("2", "新"))
        ids = [x.aweme_id for x in self.store.load_history()]
        self.assertEqual(ids, ["2", "1"])
        self.store.save_history([_item("3")])
        self.store.add_history(_item("3", "改"))
        self.assertEqual([x.aweme_id for x in self.store.load_history()], ["3"])
        self.store.clear_history()
        self.store.add_history(_item("3", "再来"))
        self.assertEqual([x.title for x in self.store.load_history()], ["再来"])

    def test_add_history_persists_immediately(self):
        """历史走 SQLite 增量写：不需要 flush，新开 Store 也能读到。"""
        self.store.add_history(_item("1"), flush=False)
        self.assertTrue(os.path.exists(config.HISTORY_DB))
        fresh = store_mod.Store()
        self.assertEqual([x.aweme_id for x in fresh.load_history()], ["1"])

    def test_reload_from_disk(self):
        self.store.add_history(_item("1"))
        fresh = store_mod.Store()
        self.assertEqual([x.aweme_id for x in fresh.load_history()], ["1"])

    def test_save_history_replaces(self):
        self.store.add_history(_item("1"))
        self.store.save_history([_item("9", "保留")])
        self.assertEqual([x.aweme_id for x in self.store.load_history()], ["9"])

    def test_merge_history_dedupes(self):
        self.store.add_history(_item("1", "已有"))
        added = self.store.merge_history([_item("1", "重复"), _item("2", "新增")])
        self.assertEqual(added, 1)
        self.assertEqual([x.aweme_id for x in self.store.load_history()], ["2", "1"])

    def test_rewrite_history_keeps_concurrent_additions(self):
        self.store.add_history(_item("1", "旧"))
        items = self.store.load_history()
        items[0].title = "已修改"
        self.store.add_history(_item("2", "并发新增"), flush=False)
        self.store.rewrite_history()
        loaded = self.store.load_history()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(next(x for x in loaded if x.aweme_id == "1").title, "已修改")

    def test_authors_add_update_merge(self):
        first = store_mod.Author("sec1", "作者A", "uid1", "")
        first.saved_ids = ["a"]
        self.store.add_author(first)
        second = store_mod.Author("sec1", "作者A改名", "uid2", "")
        second.saved_ids = ["b"]
        self.store.add_author(second)
        authors = self.store.load_authors()
        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0].nickname, "作者A改名")
        self.assertEqual(sorted(authors[0].saved_ids), ["a", "b"])

    def test_mark_saved(self):
        author = store_mod.Author("sec1", "作者A", "uid1", "")
        self.store.add_author(author)
        self.store.mark_saved(author, "123")
        self.store.mark_saved(author, "123")
        authors = self.store.load_authors()
        self.assertEqual(authors[0].saved_ids, ["123"])
        self.assertGreater(authors[0].last_sync_at, 0)

    def test_remove_author(self):
        self.store.add_author(store_mod.Author("sec1", "A"))
        self.store.add_author(store_mod.Author("sec2", "B"))
        self.store.remove_author("sec1")
        self.assertEqual([a.sec_uid for a in self.store.load_authors()], ["sec2"])

    def test_authors_flush_is_deferred_and_durable(self):
        """同步期间作者资料交给后台写：flush() 不阻塞，退出时保证落盘。

        主线程同步写 700KB authors.json，磁盘被下载占满时要卡 3～4 秒，
        所以只有用户手动增删作者和程序退出才做同步写。
        """
        self.store.add_author(store_mod.Author("sec1", "A"), flush=False)
        self.assertFalse(os.path.exists(config.AUTHORS_FILE))
        self.assertFalse(self.store.flush())
        self.store.close()
        with open(config.AUTHORS_FILE, encoding="utf-8") as f:
            self.assertEqual([a["sec_uid"] for a in json.load(f)], ["sec1"])

    def test_authors_flush_block_writes_immediately(self):
        self.store.add_author(store_mod.Author("sec1", "A"), flush=False)
        self.assertTrue(self.store.flush(block=True))
        with open(config.AUTHORS_FILE, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 1)

    def test_clear_history(self):
        self.store.add_history(_item("1"))
        self.store.clear_history()
        self.assertEqual(self.store.load_history(), [])
        fresh = store_mod.Store()
        self.assertEqual(fresh.load_history(), [])

    def test_concurrent_add_history(self):
        errors = []

        def worker(start):
            try:
                for i in range(start, start + 20):
                    self.store.add_history(_item(str(i)), flush=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n * 20,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.store.flush()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.load_history()), 100)
        fresh = store_mod.Store()
        self.assertEqual(len(fresh.load_history()), 100)

    def test_no_tmp_left(self):
        self.store.add_history(_item("1"))
        leftovers = [n for n in os.listdir(config.DATA_DIR) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_blank_aweme_id_rows_keep_separate_seqs(self):
        """空 aweme_id 的历史允许多条，回填的 seq 不能挤到同一行。

        旧版文案.txt 缺作品链接时会写空 id。以前 `_backfill_seqs` 把所有空 id
        都映射到同一个 seq，之后 `update_history_items` 反复 UPDATE 同一行：
        最后一条覆盖前面全部，其余条目在库里永远停在旧值（作者改名后路径没落库，
        下次启动就提示「文件可能已被移动或删除」）。
        """
        self.store.add_history(_item("", "空一"))
        self.store.add_history(_item("", "空二"))
        # merge 会触发一次 _backfill_seqs
        self.store.merge_history([_item("keep", "有 id")])
        blanks = [x for x in self.store.load_history() if not x.aweme_id]
        self.assertEqual(len(blanks), 2)
        self.assertEqual(len({x._db_seq for x in blanks}), 2)
        for item in blanks:
            item.title = "改-" + item.title
        updated = self.store.update_history_items(blanks)
        self.assertEqual(updated, 2)
        fresh = store_mod.Store()
        titles = sorted(x.title for x in fresh.load_history() if not x.aweme_id)
        self.assertEqual(titles, ["改-空一", "改-空二"])

    def test_corrupt_file_is_ignored(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.HISTORY_DB, "wb") as f:
            f.write(b"this is not a sqlite database")
        fresh = store_mod.Store()
        self.assertEqual(fresh.load_history(), [])
        # 损坏的库挪到 .corrupt，程序从空历史继续
        self.assertTrue(os.path.exists(config.HISTORY_DB))


if __name__ == "__main__":
    unittest.main()
