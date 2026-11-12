# -*- coding: utf-8 -*-
"""云端画质核对：MP4/tar 分辨率解析、比较阈值、预检跳过。"""
import json
import os
import tarfile
import tempfile
import time
import types
import unittest
from unittest import mock

from tests.support import isolated_config  # noqa: F401  （确保 Qt/路径环境已初始化）

from PySide6.QtWidgets import QApplication

import contacts_sync
import media_probe
import parallel
import remote_quality
import webdav_panel


def _box(kind, payload):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _tkhd(width, height):
    # 真正的 tkhd 有 84 字节内容，这里只保证末尾 8 字节是 16.16 的宽高
    payload = b"\x00" * 76 + (width << 16).to_bytes(4, "big") + \
        (height << 16).to_bytes(4, "big")
    return _box(b"tkhd", payload)


def make_mp4(path, width, height, moov_at_end=True):
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    mdat = _box(b"mdat", b"\x00" * 8192)
    moov = _box(b"moov", _box(b"trak", _tkhd(width, height)))
    body = ftyp + (mdat + moov if moov_at_end else moov + mdat)
    with open(path, "wb") as handle:
        handle.write(body)
    return body


class MediaProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jy-probe-")

    def test_moov_at_head(self):
        path = os.path.join(self.tmp, "head.mp4")
        make_mp4(path, 1080, 1920, moov_at_end=False)
        self.assertEqual(media_probe.probe_path(path), (1080, 1920))

    def test_moov_at_tail(self):
        """抖音的原片常把 moov 放在文件末尾，必须回退扫描。"""
        path = os.path.join(self.tmp, "tail.mp4")
        make_mp4(path, 2160, 3840, moov_at_end=True)
        self.assertEqual(media_probe.probe_path(path), (2160, 3840))

    def test_range_reader_only_fetches_small_blocks(self):
        path = os.path.join(self.tmp, "range.mp4")
        body = make_mp4(path, 1440, 2560, moov_at_end=False)
        calls = []

        def fetch(start, end):
            calls.append((start, end))
            return body[start:end + 1]

        reader = media_probe.RangeReader(fetch, len(body), block=4096)
        self.assertEqual(media_probe.probe_mp4(reader, len(body)), (1440, 2560))
        self.assertLessEqual(sum(end - start + 1 for start, end in calls), 16384)

    def test_probe_tar_member(self):
        mp4 = os.path.join(self.tmp, "v.mp4")
        body = make_mp4(mp4, 720, 1280)
        tar_path = os.path.join(self.tmp, "work.tar")
        with tarfile.open(tar_path, "w") as bundle:
            import io
            info = tarfile.TarInfo("视频.mp4")
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))
            caption = tarfile.TarInfo("文案.txt")
            caption.size = 2
            bundle.addfile(caption, io.BytesIO(b"hi"))
        self.assertEqual(media_probe.probe_tar(tar_path), (720, 1280))

    def test_shortest_and_should_upgrade(self):
        self.assertEqual(media_probe.shortest(1920, 1080), 1080)
        self.assertEqual(media_probe.shortest(0, 1080), 0)
        # 4K 换了 1080p 的旧备份：要重下
        self.assertTrue(media_probe.should_upgrade(2160, 3840, 1080, 1920))
        # 同档抖动不重下
        self.assertFalse(media_probe.should_upgrade(1080, 1920, 1080, 1920))
        self.assertFalse(media_probe.should_upgrade(1080, 1920, 1078, 1918))
        # 任一侧未知一律不重下，避免解析失败把整个库重下一遍
        self.assertFalse(media_probe.should_upgrade(0, 0, 1080, 1920))
        self.assertFalse(media_probe.should_upgrade(2160, 3840, 0, 0))
        # 云端更高（历史遗留）也不重下
        self.assertFalse(media_probe.should_upgrade(1080, 1920, 2160, 3840))


class FakeClient:
    def __init__(self, body):
        self.body = body
        self.fetches = 0
        self.downloads = 0

    def stat(self, rel):
        return (True, len(self.body))

    def fetch_range(self, rel, start, end):
        self.fetches += 1
        return self.body[start:end + 1]

    def download_to(self, rel, path):
        self.downloads += 1
        with open(path, "wb") as handle:
            handle.write(self.body)


class RemoteQualityTests(unittest.TestCase):
    def test_probe_caches_by_size(self):
        tmp = tempfile.mkdtemp(prefix="jy-rq-")
        body = make_mp4(os.path.join(tmp, "a.mp4"), 1080, 1920)
        client = FakeClient(body)
        probe = remote_quality.RemoteQualityProbe(
            client, cache_path=os.path.join(tmp, "cache.json"), tmp_dir=tmp)
        first = probe.probe("净影/作者/1/视频.mp4")
        self.assertEqual(first[:2], (1080, 1920))
        calls = client.fetches
        second = probe.probe("净影/作者/1/视频.mp4")
        self.assertEqual(second[:2], (1080, 1920))
        self.assertEqual(client.fetches, calls, "同一文件不应重复读远端")
        probe.save(force=True)
        # 新实例从缓存读，连 stat 都省了 fetch
        again = remote_quality.RemoteQualityProbe(
            FakeClient(body), cache_path=probe.cache_path, tmp_dir=tmp)
        self.assertEqual(again.probe("净影/作者/1/视频.mp4")[:2], (1080, 1920))
        self.assertEqual(again.client.fetches, 0)

    def test_expired_ttl_reuses_resolution_and_refreshes_stamp(self):
        """TTL 过期只意味着「重新问一次云端」，同尺寸文件绝不能因此触发重下。

        顺带锁住：核对成功后要刷新时间戳，否则过期的记录会永远过期，
        以后每轮同步都要再发一次 stat。
        """
        tmp = tempfile.mkdtemp(prefix="jy-rq-ttl-")
        body = make_mp4(os.path.join(tmp, "a.mp4"), 1080, 1920)
        rel = "净影/作者/1/视频.mp4"
        cache_path = os.path.join(tmp, "cache.json")
        stale = {"files": {rel: {"size": len(body), "w": 1080, "h": 1920,
                                 "ts": int(time.time()) - 8 * 24 * 3600}}}
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(stale, handle, ensure_ascii=False)

        client = FakeClient(body)
        probe = remote_quality.RemoteQualityProbe(
            client, cache_path=cache_path, tmp_dir=tmp)
        result = probe.probe(rel)
        # 过期缓存仍给出同一分辨率：不会变成「读不出分辨率 → 照常下载」
        self.assertEqual(result[:2], (1080, 1920))
        self.assertEqual(result[2], len(body))
        self.assertEqual(client.fetches, 0, "尺寸一致时不该再读 Range")
        probe.save(force=True)
        # 时间戳已刷新：下一次连 stat 都不需要
        again = remote_quality.RemoteQualityProbe(
            FakeClient(body), cache_path=cache_path, tmp_dir=tmp)
        self.assertEqual(again._cached_fresh(rel)[:2], (1080, 1920))

    def test_tar_probe_cleans_temp(self):
        tmp = tempfile.mkdtemp(prefix="jy-rq-tar-")
        mp4 = os.path.join(tmp, "v.mp4")
        body = make_mp4(mp4, 540, 960)
        tar_path = os.path.join(tmp, "w.tar")
        with tarfile.open(tar_path, "w") as bundle:
            bundle.add(mp4, arcname="视频.mp4")
        with open(tar_path, "rb") as handle:
            tar_body = handle.read()
        probe = remote_quality.RemoteQualityProbe(
            FakeClient(tar_body), cache_path=os.path.join(tmp, "c.json"),
            tmp_dir=os.path.join(tmp, "work"))
        result = probe.probe("净影/作者/1/1.tar")
        self.assertEqual(result[:2], (540, 960))
        leftovers = os.listdir(os.path.join(tmp, "work"))
        self.assertEqual(leftovers, [], "探测完成后临时 tar 必须删掉")

    def test_namespace_isolates_different_servers(self):
        """换了 WebDAV 服务器，同名路径不能复用上一台服务器的分辨率。

        否则 A 服务器的 4K 记录会让 B 服务器的 720p 备份被误判为「够清楚」。
        """
        tmp = tempfile.mkdtemp(prefix="jy-rq-ns-")
        body = make_mp4(os.path.join(tmp, "a.mp4"), 1080, 1920)
        rel = "净影/作者/1/视频.mp4"
        cache = os.path.join(tmp, "cache.json")
        probe_a = remote_quality.RemoteQualityProbe(
            FakeClient(body), cache_path=cache, tmp_dir=tmp,
            namespace="https://a.example|净影")
        self.assertEqual(probe_a.probe(rel)[:2], (1080, 1920))
        probe_a.save(force=True)

        client_b = FakeClient(body)
        probe_b = remote_quality.RemoteQualityProbe(
            client_b, cache_path=cache, tmp_dir=tmp,
            namespace="https://b.example|净影")
        self.assertEqual(probe_b.probe(rel)[:2], (1080, 1920))
        self.assertGreater(client_b.fetches, 0, "换服务器必须重新读远端")
        probe_b.save(force=True)

        # 切回 A 仍命中自己的缓存，不必再问一次
        client_a2 = FakeClient(body)
        probe_a2 = remote_quality.RemoteQualityProbe(
            client_a2, cache_path=cache, tmp_dir=tmp,
            namespace="https://a.example|净影")
        self.assertEqual(probe_a2.probe(rel)[:2], (1080, 1920))
        self.assertEqual(client_a2.fetches, 0)


class FakeProbe:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def probe(self, rel):
        self.seen.append(rel)
        return self.result


class SequenceProbe:
    """同一作品云端有多份文件时，按探测顺序给出不同分辨率。"""

    def __init__(self, results):
        self.results = list(results)
        self.seen = []

    def probe(self, rel):
        self.seen.append(rel)
        return self.results.pop(0) if self.results else None


class QualityGateStub(webdav_panel.WebdavMixin):
    """只挂 WebdavMixin 的 precheck 相关方法，不构造整个 MainWindow。"""

    def __init__(self, files, probe_result, upgrade=True, auto=True):
        self._webdav = {"url": "https://dav.example.com", "remote_root": "净影"}
        self._webdav_state_cache = None
        self._uploaded_units_cache = None
        self._remote_unit_index_cache = None
        self._settings = {}
        self._cloud_upgrade_flag = upgrade
        self._auto_upload_flag = auto
        self._fake_files = files
        self.fake_probe = FakeProbe(probe_result)

    def _load_webdav_state(self):
        return {"files": dict(self._fake_files)}

    def _probe_client(self):
        return self.fake_probe


def _work(width, height, album=False, size=0):
    return types.SimpleNamespace(
        aweme_id="7704638824517644474", width=width, height=height,
        size=size, is_album=lambda: album)


class CloudPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.files = {
            "净影/KiKi_123/7704638824517644474/视频.mp4": "5943169:1700000000",
            "净影/KiKi_123/7704638824517644474/文案.txt": "10:1700000000",
        }

    def test_low_cloud_quality_triggers_download(self):
        stub = QualityGateStub(self.files, (1080, 1920, 2814060))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "KiKi_作品_20261105")
        self.assertEqual(reason, "")
        self.assertEqual(stub.fake_probe.seen,
                         ["净影/KiKi_123/7704638824517644474/视频.mp4"])

    def test_same_quality_skips(self):
        stub = QualityGateStub(self.files, (2160, 3840, 5943169))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "KiKi_作品_20261105")
        self.assertIn("云端已是", reason)

    def test_album_never_checked(self):
        stub = QualityGateStub(self.files, (1080, 1920, 0))
        self.assertEqual(
            stub._cloud_quality_precheck(
                _work(2160, 3840, album=True), "KiKi_123", "x"), "")
        self.assertEqual(stub.fake_probe.seen, [])

    def test_disabled_flags_skip_probe(self):
        stub = QualityGateStub(self.files, (1080, 1920, 0), upgrade=False)
        self.assertEqual(
            stub._cloud_quality_precheck(_work(2160, 3840), "KiKi_123", "x"), "")
        self.assertEqual(stub.fake_probe.seen, [])
        stub = QualityGateStub(self.files, (1080, 1920, 0), auto=False)
        self.assertIn("自动上传", stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "x"))

    def test_similar_size_skips_probe(self):
        """云端 5.94 MB / 本次解析 5.94 MB：同画质，连远端请求都不用发。"""
        stub = QualityGateStub(self.files, (1080, 1920, 0))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840, size=5943169), "KiKi_123", "KiKi_作品_20261105")
        self.assertIn("相近", reason)
        self.assertEqual(stub.fake_probe.seen, [])

    def test_much_smaller_cloud_file_is_probed(self):
        """云端 2.68 MB / 本次 5.94 MB：明显小一档，才去读真实分辨率。"""
        files = {"净影/KiKi_123/7704638824517644474/视频.mp4": "2814060:1700000000"}
        stub = QualityGateStub(files, (1080, 1920, 2814060))
        self.assertEqual(stub._cloud_quality_precheck(
            _work(2160, 3840, size=5943169), "KiKi_123", "KiKi_作品_20261105"), "")
        self.assertEqual(len(stub.fake_probe.seen), 1)

    def test_unknown_target_quality_keeps_downloading(self):
        stub = QualityGateStub(self.files, (1080, 1920, 0))
        self.assertEqual(
            stub._cloud_quality_precheck(_work(0, 0), "KiKi_123", "x"), "")
        self.assertEqual(stub.fake_probe.seen, [])

    def test_tar_backup_is_located(self):
        """开启过「打包成 tar」的老库：备份单元是 <作品id>.tar，也能核对到。"""
        files = {"净影/KiKi_123/7704638824517644474.tar": "2814060:1700000000"}
        stub = QualityGateStub(files, (1080, 1920, 2814060))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "KiKi_作品_20261105")
        self.assertEqual(reason, "")
        self.assertEqual(stub.fake_probe.seen,
                         ["净影/KiKi_123/7704638824517644474.tar"])

    def test_unreadable_remote_quality_keeps_existing_backup(self):
        """远端分辨率读不出来时保留旧备份，绝不能因此重下整个库。"""
        stub = QualityGateStub(self.files, None)
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "KiKi_作品_20261105")
        self.assertIn("画质无法确认", reason)
        self.assertEqual(len(stub.fake_probe.seen), 1)

    def test_missing_remote_path_keeps_existing_backup(self):
        stub = QualityGateStub({"净影/别的作者/1/视频.mp4": "1:1"},
                               (1080, 1920, 0))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "x")
        self.assertIn("路径无法定位", reason)
        self.assertEqual(stub.fake_probe.seen, [])

    def test_any_high_quality_copy_skips_redownload(self):
        """散文件是 720p、同作品还有 4K tar：只要有一份够清楚就别重下。"""
        files = {
            "净影/KiKi_123/7704638824517644474/视频.mp4": "2814060:1",
            "净影/KiKi_123/7704638824517644474.tar": "5943169:1",
        }
        stub = QualityGateStub(files, None)
        stub.fake_probe = SequenceProbe([(1080, 1920, 2814060),
                                         (2160, 3840, 5943169)])
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840), "KiKi_123", "KiKi_作品_20261105")
        self.assertIn("云端已是", reason)
        self.assertEqual(len(stub.fake_probe.seen), 2)

    def test_size_coarse_filter_checks_every_copy(self):
        """第一个文件明显小，但第二个同画质：粗筛必须看完所有候选再决定。"""
        files = {
            "净影/KiKi_123/7704638824517644474/视频.mp4": "1000:1",
            "净影/KiKi_123/7704638824517644474.tar": "5943169:1",
        }
        stub = QualityGateStub(files, (1080, 1920, 0))
        reason = stub._cloud_quality_precheck(
            _work(2160, 3840, size=5943169), "KiKi_123", "KiKi_作品_20261105")
        self.assertIn("相近", reason)
        self.assertEqual(stub.fake_probe.seen, [])


class SkipSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa = QApplication.instance() or QApplication([])

    def test_precheck_skip_emits_item_skipped(self):
        """预检判定云端够清楚时：不发 item_done、不算失败、不调下载。"""
        from unittest import mock

        skipped, done, errors, calls = [], [], [], []

        def fake_download(info, author_folder, work_folder, **_kwargs):
            calls.append(info)
            return types.SimpleNamespace(video_path="", music_path="",
                                         album_paths=[], live_paths=[])

        info = types.SimpleNamespace(aweme_id="42")
        dm = parallel.DownloadManager(1, precheck=lambda *_a: "云端已是 2160×3840")
        dm.item_skipped.connect(lambda index, reason: skipped.append((index, reason)))
        dm.item_done.connect(lambda index, _r: done.append(index))
        dm.item_error.connect(lambda index, _m: errors.append(index))
        with mock.patch.object(parallel.downloader, "download_work", fake_download):
            dm.begin(1)
            dm.upgrade_ids.add("42")   # 名单在 begin 之后登记（同步流程就是这样）
            dm.enqueue([(info, "author", "work")])
            deadline = time.time() + 5
            while not skipped and time.time() < deadline:
                self.qa.processEvents()
                time.sleep(0.01)
            dm.stop()
        self.assertEqual(skipped[0][0], 0)
        self.assertIn("2160", skipped[0][1])
        self.assertEqual(done, [])
        self.assertEqual(errors, [])
        self.assertEqual(calls, [])


class SyncSummaryTests(unittest.TestCase):
    def test_finish_sync_reports_quality_skips(self):
        """同步收尾要把「云端已是高画质」的跳过数写进汇总，不然用户看不出效果。"""
        stub = mock.MagicMock()
        stub.syncing = True
        stub._sync_total_new = 3
        stub._sync_total_fail = 0
        stub._dl_skip = 2
        stub._sync_failed_authors = []
        with mock.patch.object(contacts_sync.sync_log, "append_event") as append:
            contacts_sync.ContactsSyncMixin._finish_sync(stub)
        text = stub.contact_status.setText.call_args[0][0]
        self.assertIn("全部同步完成：成功 3 个作品", text)
        self.assertIn("云端已是高画质跳过 2 个", text)
        self.assertIn(text, [call[0][0] for call in append.call_args_list])

    def test_finish_sync_keeps_old_text_without_skips(self):
        stub = mock.MagicMock()
        stub.syncing = True
        stub._sync_total_new = 3
        stub._sync_total_fail = 0
        stub._dl_skip = 0
        stub._sync_failed_authors = []
        with mock.patch.object(contacts_sync.sync_log, "append_event"):
            contacts_sync.ContactsSyncMixin._finish_sync(stub)
        self.assertEqual(stub.contact_status.setText.call_args[0][0],
                         "全部同步完成：成功 3 个作品，失败 0 个")


if __name__ == "__main__":
    unittest.main()
