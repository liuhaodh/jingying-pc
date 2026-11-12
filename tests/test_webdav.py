# -*- coding: utf-8 -*-
"""上传进度：文件分块读取与 upload_tree 的字节进度回调（本地 HTTP 服务，不联网）。"""
import http.server
import os
import threading
import unittest
import urllib.parse
from unittest import mock

from tests.support import isolated_config

import webdav


class _DummyClient(webdav.WebDAVClient):
    def __init__(self):
        super().__init__("http://example.invalid/dav")
        self.uploaded = []

    def upload_file(self, local_path, rel_path, on_bytes=None):
        for _chunk in webdav._iter_file(local_path, on_bytes):
            pass
        self.uploaded.append(rel_path)


class _PutHandler(http.server.BaseHTTPRequestHandler):
    payloads = {}

    def log_message(self, *_args):
        pass

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length)
        _PutHandler.payloads[self.path] = (data, length)
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _DirHandler(http.server.BaseHTTPRequestHandler):
    """模拟 123 云盘：MKCOL 对已存在/暂时不可建的目录返回 409。"""

    mkcol_status = 409
    exists = set()

    def log_message(self, *_args):
        pass

    def _reply(self, code, body=b"", content_type=None):
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_MKCOL(self):
        self._reply(self.mkcol_status)

    def do_PROPFIND(self):
        path = urllib.parse.unquote(self.path).rstrip("/")
        if path in self.exists:
            self._reply(207, b"<D:multistatus xmlns:D='DAV:'></D:multistatus>",
                        "text/xml; charset=utf-8")
        else:
            self._reply(404)


class UploadProgressTests(unittest.TestCase):
    def setUp(self):
        self._ctx = isolated_config()
        self.tmp = self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def test_iter_file_reports_bytes(self):
        path = os.path.join(self.tmp, "a.bin")
        data = b"x" * (600 * 1024)
        with open(path, "wb") as f:
            f.write(data)
        seen = []
        total = sum(len(chunk) for chunk in webdav._iter_file(path, seen.append))
        self.assertEqual(total, len(data))
        self.assertEqual(sum(seen), len(data))

    def test_progress_file_is_seekable_and_counts_once(self):
        path = os.path.join(self.tmp, "a.bin")
        data = b"x" * 500
        with open(path, "wb") as f:
            f.write(data)
        seen = []
        with open(path, "rb") as raw:
            body = webdav._ProgressFile(raw, seen.append)
            self.assertTrue(body.seekable())
            self.assertEqual(len(body), 500)
            body.read(200)
            body.seek(0)
            body.read(200)
            self.assertEqual(sum(seen), 200)

    def test_videos_are_uploaded_before_music(self):
        os.makedirs(os.path.join(self.tmp, "Videos", "净影"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "Music", "净影"), exist_ok=True)
        for rel in ("Music/净影/a.mp3", "Videos/净影/b.mp4"):
            path = os.path.join(self.tmp, rel.replace("/", os.sep))
            with open(path, "wb") as f:
                f.write(b"x" * 10)
        client = _DummyClient()
        client.upload_tree(self.tmp, max_workers=1)
        self.assertEqual(client.uploaded, ["Videos/净影/b.mp4", "Music/净影/a.mp3"])

    def test_upload_tree_progress_reaches_total(self):
        files = {"a.bin": b"a" * 1000, "b.bin": b"b" * 2000}
        for name, data in files.items():
            with open(os.path.join(self.tmp, name), "wb") as f:
                f.write(data)
        progress = []
        sent = []
        file_progress = []
        client = _DummyClient()
        ok, skipped, failed = client.upload_tree(
            self.tmp, max_workers=2,
            on_progress=lambda done, total: progress.append((done, total)),
            on_bytes=sent.append,
            on_file_progress=lambda rel, done, total: file_progress.append(
                (rel, done, total)))
        self.assertEqual((ok, skipped, failed), (2, 0, []))
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (3000, 3000))
        self.assertEqual(sum(sent), 3000)
        self.assertTrue(file_progress)
        self.assertIn(("a.bin", 1000, 1000), file_progress)
        self.assertIn(("b.bin", 2000, 2000), file_progress)

    def test_upload_tree_skip_still_counts_progress(self):
        path = os.path.join(self.tmp, "a.bin")
        with open(path, "wb") as f:
            f.write(b"a" * 1000)
        fingerprint = f"{os.path.getsize(path)}:{int(os.path.getmtime(path))}"
        progress = []
        client = _DummyClient()
        ok, skipped, failed = client.upload_tree(
            self.tmp, state={"a.bin": fingerprint},
            on_progress=lambda done, total: progress.append((done, total)))
        self.assertEqual((ok, skipped, failed), (0, 1, []))
        self.assertEqual(progress[-1], (1000, 1000))

    def test_state_save_error_does_not_mark_file_failed(self):
        path = os.path.join(self.tmp, "a.bin")
        with open(path, "wb") as f:
            f.write(b"a" * 1000)
        state = {}

        def broken_save():
            raise RuntimeError("disk full")

        client = _DummyClient()
        ok, skipped, failed = client.upload_tree(
            self.tmp, max_workers=2, state=state, save_state=broken_save)
        self.assertEqual((ok, skipped, failed), (1, 0, []))
        self.assertIn("a.bin", state)

    def _start_dir_server(self, mkcol_status=409, exists=()):
        _DirHandler.mkcol_status = mkcol_status
        _DirHandler.exists = set(exists)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DirHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return webdav.WebDAVClient(
            f"http://127.0.0.1:{server.server_address[1]}/dav")

    def test_mkcol_409_but_dir_exists_is_success(self):
        """服务端对已存在目录回 409 时，不能把它当成失败，也不能当成没建。"""
        client = self._start_dir_server(
            mkcol_status=409, exists={"/dav/净影", "/dav/净影/作者"})
        with mock.patch("webdav.time.sleep"):
            client.ensure_dir("净影/作者")
        self.assertIn("净影/作者", client._created_dirs)

    def test_mkcol_409_and_missing_dir_raises(self):
        """父目录确实建不出来时必须报错，而不是默默继续导致所有上传失败。"""
        client = self._start_dir_server(mkcol_status=409, exists=set())
        with mock.patch("webdav.time.sleep"):
            with self.assertRaises(webdav.WebDAVError) as ctx:
                client.ensure_dir("净影/作者")
        self.assertIn("创建远程目录失败", str(ctx.exception))
        self.assertNotIn("净影/作者", client._created_dirs)

    def test_failed_dir_is_not_retried_for_every_file(self):
        """目录失败后短时间内直接失败，避免几千个文件各等一轮重试。"""
        client = self._start_dir_server(mkcol_status=409, exists=set())
        with mock.patch("webdav.time.sleep"):
            with self.assertRaises(webdav.WebDAVError):
                client.ensure_dir("净影/作者")
        calls = []
        client._request = lambda *a, **k: calls.append(a) or self.fail("不应再发请求")
        with self.assertRaises(webdav.WebDAVError):
            client.ensure_dir("净影/作者")
        self.assertEqual(calls, [])

    def test_upload_tree_roots_limit_scan(self):
        """手动「上传全部」只扫描作品/音乐/作者资料，不碰主目录其他文件。"""
        home = self.tmp
        for rel in ("净影/a.mp4", "Desktop/私人.txt", "Videos/b.mp4"):
            path = os.path.join(home, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"x")
        client = _DummyClient()
        client.upload_tree(
            home, roots=[os.path.join(home, "净影"),
                         os.path.join(home, "Videos")],
            max_workers=1)
        self.assertEqual(sorted(client.uploaded), ["Videos/b.mp4", "净影/a.mp4"])

    def test_url_does_not_duplicate_remote_root(self):
        """远程根目录叫「净影」、本地相对路径也以「净影/」开头时不能拼成两层。"""
        client = webdav.WebDAVClient("http://example.invalid/dav", remote_root="净影")
        self.assertEqual(
            client._url("净影/作者/1.mp4"),
            "http://example.invalid/dav/%E5%87%80%E5%BD%B1/%E4%BD%9C%E8%80%85/1.mp4")
        self.assertEqual(
            client._url("Videos/a.mp4"),
            "http://example.invalid/dav/%E5%87%80%E5%BD%B1/Videos/a.mp4")

    def test_put_sends_body_and_reports_bytes(self):
        _PutHandler.payloads = {}
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PutHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        data = b"z" * (300 * 1024 + 17)
        path = os.path.join(self.tmp, "a.mp4")
        with open(path, "wb") as f:
            f.write(data)
        seen = []
        client = webdav.WebDAVClient(
            f"http://127.0.0.1:{server.server_address[1]}", remote_root="净影")
        client.upload_file(path, "a.mp4", on_bytes=seen.append)

        body, length = _PutHandler.payloads["/%E5%87%80%E5%BD%B1/a.mp4"]
        self.assertEqual(body, data)
        self.assertEqual(length, len(data))
        self.assertEqual(sum(seen), len(data))


if __name__ == "__main__":
    unittest.main()
