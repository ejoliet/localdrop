from __future__ import annotations

import base64
import importlib.util
import unittest
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = ROOT / "native" / "localdrop_host.py"
spec = importlib.util.spec_from_file_location("localdrop_host", HOST_PATH)
assert spec and spec.loader
host_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = host_module
spec.loader.exec_module(host_module)


class LocalDropHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_send = host_module.WRITER.send
        host_module.WRITER.send = lambda _message: None
        self.host = host_module.LocalDropHost()

    def tearDown(self) -> None:
        self.host.close()
        host_module.WRITER.send = self.original_send

    def upload(self, path: str, content: bytes, file_id: str = "file-1") -> dict:
        self.host.begin_file({"fileId": file_id, "path": path, "size": len(content)})
        midpoint = max(1, len(content) // 2)
        chunks = [content[:midpoint], content[midpoint:]] if content else []
        for sequence, chunk in enumerate(chunks):
            self.host.file_chunk(
                {
                    "fileId": file_id,
                    "sequence": sequence,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
            )
        return self.host.end_file({"fileId": file_id})

    def test_safe_relative_path_rejects_traversal(self) -> None:
        for value in ("../secret", "/absolute", "a/../../secret", "", "."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    host_module.safe_relative_path(value)

    def test_create_upload_finalize_and_serve(self) -> None:
        session = self.host.create_session({"spaFallback": False, "allowCors": False})
        root = self.host.session.root
        content = b"hello localdrop\n"
        result = self.upload("docs/hello.txt", content)
        self.assertEqual(result["size"], len(content))
        finalized = self.host.finalize({})
        self.assertTrue(finalized["finalized"])
        self.assertEqual(finalized["fileCount"], 1)

        with urllib.request.urlopen(session["localUrl"] + "docs/hello.txt", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), content)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")

        self.host.clear_session({})
        self.assertFalse(root.exists())

    def test_directory_listing_escapes_names(self) -> None:
        session = self.host.create_session({})
        self.upload("unsafe-<name>.txt", b"x")
        self.host.finalize({})
        with urllib.request.urlopen(session["localUrl"], timeout=3) as response:
            body = response.read().decode("utf-8")
        self.assertIn("unsafe-&lt;name&gt;.txt", body)
        self.assertNotIn("unsafe-<name>.txt", body)

    def test_index_and_spa_fallback(self) -> None:
        session = self.host.create_session({"spaFallback": True})
        self.upload("index.html", b"<h1>app</h1>")
        self.host.finalize({"spaFallback": True})
        with urllib.request.urlopen(session["localUrl"], timeout=3) as response:
            self.assertEqual(response.read(), b"<h1>app</h1>")
        with urllib.request.urlopen(session["localUrl"] + "route/deep", timeout=3) as response:
            self.assertEqual(response.read(), b"<h1>app</h1>")

    def test_range_request(self) -> None:
        session = self.host.create_session({})
        self.upload("video.bin", b"0123456789")
        self.host.finalize({})
        request = urllib.request.Request(session["localUrl"] + "video.bin", headers={"Range": "bytes=2-5"})
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")

    def test_wrong_token_and_traversal_are_not_found(self) -> None:
        session = self.host.create_session({})
        self.upload("safe.txt", b"safe")
        self.host.finalize({})
        wrong = session["localUrl"].replace(self.host.session.token, "wrong-token")
        for url in (wrong, session["localUrl"] + "%2e%2e/safe.txt"):
            with self.subTest(url=url):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url, timeout=3)
                self.assertEqual(caught.exception.code, 404)

    def test_cors_is_opt_in(self) -> None:
        session = self.host.create_session({"allowCors": True})
        self.upload("data.json", b"{}")
        self.host.finalize({"allowCors": True})
        with urllib.request.urlopen(session["localUrl"] + "data.json", timeout=3) as response:
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

    def test_cancel_removes_partial_file(self) -> None:
        self.host.create_session({})
        self.host.begin_file({"fileId": "cancel", "path": "large.bin", "size": 10})
        self.host.file_chunk(
            {"fileId": "cancel", "sequence": 0, "data": base64.b64encode(b"123").decode("ascii")}
        )
        result = self.host.cancel_file({"fileId": "cancel"})
        self.assertTrue(result["cancelled"])
        self.assertFalse((self.host.session.root / "large.bin").exists())
        self.assertFalse(any(self.host.session.root.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
