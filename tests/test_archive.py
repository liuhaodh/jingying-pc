# -*- coding: utf-8 -*-
"""作品归档包：打包 / 读成员 / 解包缓存 / 文件分类。"""
import os
import tarfile
import unittest

from tests.support import isolated_config

import archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()
        self.work_dir = os.path.join(self.tmp, "作者_文案_20261108_101010")
        os.makedirs(self.work_dir)
        for name, data in (("视频.mp4", b"v" * 100),
                           ("图片_01.jpg", b"i1"),
                           ("图片_02.jpg", b"i2"),
                           ("图片_10.jpg", b"i10"),
                           ("动图_01.mp4", b"l1"),
                           ("音乐.mp3", b"m" * 50),
                           ("文案.txt", "文案：测试".encode("utf-8"))):
            with open(os.path.join(self.work_dir, name), "wb") as f:
                f.write(data)
        self.tar_path = os.path.join(self.tmp, "Videos", "作者_uid", "7525082444551310602.tar")

    def test_is_archive(self):
        self.assertTrue(archive.is_archive("a.tar"))
        self.assertTrue(archive.is_archive("C:/x/123.TAR"))
        self.assertFalse(archive.is_archive("a.mp4"))
        self.assertFalse(archive.is_archive(""))

    def test_pack_keeps_folder_and_members(self):
        archive.pack(self.work_dir, self.tar_path)
        self.assertTrue(os.path.isfile(self.tar_path))
        members = archive.member_names(self.tar_path)
        # tar 里保留作品文件夹作为顶层目录，解压不会散落一地
        self.assertEqual({os.path.dirname(m) for m in members},
                         {"作者_文案_20261108_101010"})
        self.assertIn("作者_文案_20261108_101010/视频.mp4", members)
        # 打包完成后没有 .part 残留
        self.assertFalse(os.path.exists(self.tar_path + ".part"))

    def test_read_member(self):
        archive.pack(self.work_dir, self.tar_path)
        text = archive.read_member(self.tar_path, "文案.txt")
        self.assertIn("文案：测试", text)
        self.assertEqual(archive.read_member(self.tar_path, "不存在.txt"), "")

    def test_extract_sorts_images_and_links_lives(self):
        archive.pack(self.work_dir, self.tar_path)
        result = archive.extract(self.tar_path, os.path.join(self.tmp, "cache"))
        self.assertTrue(os.path.isfile(result["video"]))
        self.assertEqual([os.path.basename(p) for p in result["images"]],
                         ["图片_01.jpg", "图片_02.jpg", "图片_10.jpg"])
        self.assertEqual([os.path.basename(p) for p in result["lives"]], ["动图_01.mp4"])
        self.assertTrue(result["music"].endswith("音乐.mp3"))
        self.assertTrue(result["caption"].endswith("文案.txt"))

    def test_extract_is_cached(self):
        archive.pack(self.work_dir, self.tar_path)
        cache = os.path.join(self.tmp, "cache")
        first = archive.extract(self.tar_path, cache)
        stamp = os.path.getmtime(first["video"])
        second = archive.extract(self.tar_path, cache)
        # 同一个包（大小+修改时间一致）不重复解压
        self.assertEqual(first["dir"], second["dir"])
        self.assertEqual(os.path.getmtime(second["video"]), stamp)

    def test_extract_broken_tar_returns_empty(self):
        os.makedirs(os.path.dirname(self.tar_path), exist_ok=True)
        with open(self.tar_path, "wb") as f:
            f.write(b"not a tar at all")
        result = archive.extract(self.tar_path, os.path.join(self.tmp, "cache"))
        self.assertEqual(result["video"], "")
        self.assertEqual(result["images"], [])
        self.assertEqual(archive.member_names(self.tar_path), [])

    def test_cleanup_removes_cache(self):
        archive.pack(self.work_dir, self.tar_path)
        cache = os.path.join(self.tmp, "cache")
        archive.extract(self.tar_path, cache)
        self.assertTrue(os.path.isdir(cache))
        archive.cleanup(cache)
        self.assertFalse(os.path.isdir(cache))

    def test_pack_overwrites_previous_archive(self):
        archive.pack(self.work_dir, self.tar_path)
        with tarfile.open(self.tar_path) as tar:
            self.assertEqual(len(tar.getmembers()), 7)
        os.remove(os.path.join(self.work_dir, "音乐.mp3"))
        archive.pack(self.work_dir, self.tar_path)
        with tarfile.open(self.tar_path) as tar:
            self.assertEqual(len(tar.getmembers()), 6)


if __name__ == "__main__":
    unittest.main()
