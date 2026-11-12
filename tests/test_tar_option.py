# -*- coding: utf-8 -*-
"""「下载后打包成 tar」开关：下载产物形态、上传队列、UI 默认值与提示。"""
import os
import unittest
from unittest import mock

from tests.support import isolated_config

import archive
import downloader
import models
import webdav_panel


class _Recorder:
    def __init__(self):
        self.batches = []

    def enqueue(self, paths):
        self.batches.append(list(paths))
        return len(paths)


class _UploadMixin(webdav_panel.WebdavMixin):
    """只测入队逻辑：不构造 QWidget，只提供 _auto_uploader。"""

    def __init__(self):
        self._auto_uploader = _Recorder()


class DownloadPackTarTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def _info(self):
        info = models.MediaInfo()
        info.aweme_id = "7525082444551310602"
        info.title = "测试作品"
        info.author = "作者甲"
        info.unique_id = "uid123"
        info.create_time = 1700000000
        info.play_url = "https://example.invalid/video.mp4"
        return info

    @staticmethod
    def _fake_download(url, target, *args, **kwargs):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(b"x" * 4096)
        return 4096

    def _download(self, pack_tar):
        info = self._info()
        with mock.patch.object(downloader, "_download_to_file",
                               side_effect=self._fake_download):
            return downloader.download_work(
                info, "作者甲_uid123", "7525082444551310602", pack_tar=pack_tar)

    def test_pack_tar_creates_archive_and_removes_folder(self):
        result = self._download(pack_tar=True)
        folder = os.path.join(self.tmp, "净影", "作者甲_uid123", "7525082444551310602")
        tar_path = os.path.join(self.tmp, "净影", "作者甲_uid123",
                                "7525082444551310602.tar")
        self.assertEqual(result.archive_path, tar_path)
        self.assertTrue(os.path.isfile(tar_path))
        self.assertFalse(os.path.isdir(folder), "打包成功后不应再留散文件目录")
        self.assertEqual(result.video_path, tar_path)
        self.assertEqual(result.music_path, "")
        self.assertEqual(result.caption_path, "")
        members = archive.member_names(tar_path)
        self.assertTrue(any(name.endswith("视频.mp4") for name in members), members)
        self.assertTrue(any(name.endswith("文案.txt") for name in members), members)

    def test_default_keeps_folder_without_archive(self):
        result = self._download(pack_tar=False)
        folder = os.path.join(self.tmp, "净影", "作者甲_uid123", "7525082444551310602")
        tar_path = folder + ".tar"
        self.assertEqual(result.archive_path, "")
        self.assertTrue(os.path.isdir(folder))
        self.assertFalse(os.path.exists(tar_path))
        self.assertTrue(os.path.isfile(result.video_path))

    def test_pack_failure_keeps_folder(self):
        """打包失败（磁盘只读/权限）不能丢作品：保留目录并正常返回。"""
        info = self._info()
        with mock.patch.object(downloader, "_download_to_file",
                               side_effect=self._fake_download), \
                mock.patch.object(archive, "pack", side_effect=OSError("disk full")):
            result = downloader.download_work(
                info, "作者甲_uid123", "7525082444551310602", pack_tar=True)
        folder = os.path.join(self.tmp, "净影", "作者甲_uid123", "7525082444551310602")
        self.assertEqual(result.archive_path, "")
        self.assertTrue(os.path.isdir(folder))
        self.assertTrue(os.path.isfile(result.video_path))

    def test_album_archive_paths_point_to_tar(self):
        info = models.MediaInfo()
        info.aweme_id = "7525082444551310603"
        info.title = "图集作品"
        info.author = "作者乙"
        info.unique_id = "uid456"
        info.create_time = 1700000001
        info.image_urls = ["https://example.invalid/1.jpg",
                           "https://example.invalid/2.jpg"]
        with mock.patch.object(downloader, "_download_to_file",
                               side_effect=self._fake_download):
            result = downloader.download_work(
                info, "作者乙_uid456", "7525082444551310603", pack_tar=True)
        self.assertTrue(archive.is_archive(result.archive_path))
        self.assertEqual(result.album_paths, [result.archive_path])
        self.assertEqual(result.live_paths, [])


class UploadQueueTests(unittest.TestCase):
    def test_archive_result_uploads_only_tar(self):
        mixin = _UploadMixin()
        result = downloader.DownloadResult()
        result.archive_path = r"C:\x\净影\作者\123.tar"
        result.video_path = result.archive_path
        result.album_paths = [result.archive_path]
        result.music_path = r"C:\x\净影\作者\123\音乐.mp3"
        result.caption_path = r"C:\x\净影\作者\123\文案.txt"
        mixin._queue_auto_upload(result)
        self.assertEqual(mixin._auto_uploader.batches, [[result.archive_path]])

    def test_folder_result_uploads_all_files(self):
        mixin = _UploadMixin()
        result = downloader.DownloadResult()
        result.video_path = r"C:\x\123\视频.mp4"
        result.music_path = r"C:\x\123\音乐.mp3"
        result.caption_path = r"C:\x\123\文案.txt"
        result.album_paths = [r"C:\x\123\图片_01.jpg"]
        mixin._queue_auto_upload(result)
        self.assertEqual(mixin._auto_uploader.batches, [[
            result.video_path, result.music_path, result.caption_path,
            result.album_paths[0]]])


class DownloadManagerOptionTests(unittest.TestCase):
    def test_pack_tar_is_passed_to_download_work(self):
        import parallel
        manager = parallel.DownloadManager(2, pack_tar=True)
        self.assertTrue(manager.pack_tar)
        info = models.MediaInfo()
        info.aweme_id = "1"
        info.play_url = "https://example.invalid/v.mp4"
        captured = {}

        def fake_download_work(*args, **kwargs):
            captured.update(kwargs)
            return downloader.DownloadResult()

        with mock.patch.object(downloader, "download_work", side_effect=fake_download_work):
            manager._run_one(0, (info, "作者", "1"))
        self.assertTrue(captured.get("pack_tar"))


class DownloadedCheckTests(unittest.TestCase):
    """通讯录同步的「已下载」判断要认新位置的 tar，否则每轮都重下一遍。"""

    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_tar_under_save_root_counts_as_downloaded(self):
        import config
        from contacts_sync import ContactsSyncMixin
        info = models.MediaInfo()
        info.aweme_id = "7525082444551310604"
        info.title = "作品"
        info.create_time = 1700000000
        author_folder = "作者甲_uid123"
        work_folder = downloader.build_work_folder(info, "作者甲")
        tar_dir = os.path.join(config.SAVE_ROOT, author_folder)
        os.makedirs(tar_dir, exist_ok=True)
        tar_path = os.path.join(tar_dir,
                                downloader.archive_filename(info, work_folder))
        with open(tar_path, "wb") as f:
            f.write(b"x" * 128)
        self.assertTrue(ContactsSyncMixin._work_files_exist(
            object(), info, author_folder, "作者甲", work_folder))

    def test_empty_tar_is_not_treated_as_downloaded(self):
        import config
        from contacts_sync import ContactsSyncMixin
        info = models.MediaInfo()
        info.aweme_id = "7525082444551310605"
        info.title = "作品"
        info.create_time = 1700000000
        author_folder = "作者甲_uid123"
        work_folder = downloader.build_work_folder(info, "作者甲")
        tar_dir = os.path.join(config.SAVE_ROOT, author_folder)
        os.makedirs(tar_dir, exist_ok=True)
        open(os.path.join(tar_dir,
                          downloader.archive_filename(info, work_folder)), "wb").close()
        self.assertFalse(ContactsSyncMixin._work_files_exist(
            object(), info, author_folder, "作者甲", work_folder))


if __name__ == "__main__":
    unittest.main()
