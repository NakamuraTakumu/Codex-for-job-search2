"""Manual browser save imports; only the HTTP handler test uses loopback."""
import http.client
import json
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from jobs.common import page_fetch as pf
from scripts import import_page_server as server

URL = "https://example.com/recruit"
OTHER = "https://example.com/other"
CID = "1234567890123"
MODIFIED = "1791676800000"
MHTML = b"""MIME-Version: 1.0
Snapshot-Content-Location: https://example.com/recruit#saved
Date: Sun, 11 Oct 2026 09:00:00 +0900
Content-Type: multipart/related; boundary="saved"

--saved
Content-Type: image/png
Content-Location: https://example.com/logo.png

image
--saved
Content-Type: text/html; charset=utf-8
Content-Location: https://example.com/recruit
Content-Transfer-Encoding: quoted-printable

<html><body>Saved =64eadline</body></html>
--saved--
"""


class ImportServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fetch_root = self.root / "fetch"
        patcher = patch.object(pf, "FETCH_ROOT", self.fetch_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.company_dir = self.root / "companies"
        self.company_dir.mkdir()
        (self.company_dir / f"{CID}.json").write_text(json.dumps({"legal_name": "試験株式会社"}))
        self.failure(URL)
        self.session = server.ImportSession(self.company_dir)

    def failure(self, url, **kwargs):
        entry = dict(requested_url=url, final_url=url, status=403, reason="HTTP 403",
                     attempted_at="2026-01-01T00:00:00+00:00", category="fetch_failure",
                     company_id=CID, recruitment_year=2028, job_name="recruitment_case_fill_revise")
        entry.update(kwargs)
        pf._append_jsonl(self.fetch_root / "failures.jsonl", entry)
        return entry

    def test_chrome_url_and_fragment_exact_matching(self):
        body = b'<!-- saved from url=(0027)https://example.com/recruit#saved -->\n<html>Deadline</html>'
        original, payload, name, stamp = server.saved_document("file.html", body)
        self.assertEqual(original, URL + "#saved")
        self.assertEqual(payload, body)
        self.assertEqual(name, "saved.html")
        self.assertIsNone(stamp)
        self.assertEqual(server.match_url(original, None, self.session.pending()), URL)
        for different in (OTHER, URL + "/", "https://EXAMPLE.com/recruit", URL + "?x=1"):
            with self.assertRaisesRegex(ValueError, "行へ"):
                server.match_url(different, None, self.session.pending())

    def test_mhtml_main_html_date_and_import(self):
        original, body, name, stamp = server.saved_document("saved.mhtml", MHTML)
        self.assertEqual(original, URL + "#saved")
        self.assertEqual(body, b"<html><body>Saved deadline</body></html>")
        self.assertEqual(name, "saved.html")
        self.assertEqual(stamp, "2026-10-11T00:00:00+00:00")
        result = self.session.upload("saved.mhtml", MHTML, "invalid")
        self.assertEqual(result["fetched_at"], stamp)
        self.assertEqual(Path(result["raw_path"]).read_bytes(), body)
        self.assertEqual(pf.unresolved_failures(), [])

    def test_mhtml_content_location_without_snapshot_header(self):
        sample = MHTML.replace(b"Snapshot-Content-Location: https://example.com/recruit#saved\n", b"")
        self.assertEqual(server.saved_document("saved.mht", sample)[0], URL)
        sample = sample.replace(b'Date: Sun, 11 Oct 2026 09:00:00 +0900\n', b'')
        self.assertIsNone(server.saved_document("saved.mht", sample)[3])
        sample = sample.replace(b"Content-Type: text/html; charset=utf-8", b"Content-Type: text/plain")
        with self.assertRaisesRegex(ValueError, "text/html"):
            server.saved_document("saved.mht", sample)

    def test_row_fallback_and_embedded_mismatch_rejection(self):
        with self.assertRaisesRegex(ValueError, "行へ"):
            self.session.upload("firefox.html", b"<p>Saved</p>", MODIFIED)
        mismatch = b'<!-- saved from url=(0027)https://example.com/other --><p>Saved</p>'
        with self.assertRaisesRegex(ValueError, "異なります"):
            self.session.upload("saved.html", mismatch, MODIFIED, URL)
        result = self.session.upload("firefox.html", b"<p>Saved</p>", MODIFIED, URL + "#row")
        self.assertTrue(result["ok"])
        self.assertEqual(result["fetched_at"], "2026-10-11T00:00:00+00:00")
        self.assertIn("完了", self.session.page())
        self.assertIn("試験株式会社", self.session.page())
        command = self.session.revise_commands()
        self.assertIn(f"2028 --revise --phase cases --company-ids {CID}", command)
        self.assertEqual(self.session.pending(), [])

    def test_redirected_urls_match_and_import_under_requested_url(self):
        for requested, final in (("http://example.com/recruit", URL), (URL, URL + "/")):
            for target in (None, requested + "#row"):
                with self.subTest(requested=requested, final=final, target=target):
                    row = self.failure(requested, final_url=final)
                    self.assertEqual(server.match_url(final + "#saved", target, [row]), requested)
                    self.assertEqual(server.match_url(requested + "#saved", target, [row]), requested)
                    body = f"<!-- saved from url=(0027){final}#saved --><p>Deadline</p>".encode()
                    with patch.object(self.session, "pending", return_value=[row]):
                        result = self.session.upload("saved.html", body, MODIFIED, target)
                    self.assertEqual(result["requested_url"], requested)
                    self.assertIn(requested, self.session.imported)
                    with self.assertRaisesRegex(ValueError, "異なります"):
                        server.match_url(OTHER, requested, [row])

    def test_generated_companies_json_is_accepted_by_survey_loader(self):
        from scripts.survey_companies import load_targets
        self.session.upload("saved.html", b"<p>Deadline</p>", MODIFIED, URL)
        command = self.session.revise_commands()
        printf = shlex.split(command.splitlines()[1])
        path = self.root / "generated_companies.json"
        path.write_text(printf[2], encoding="utf-8")
        self.assertEqual(load_targets(path, None), [{"company_id": CID, "legal_name": "試験株式会社"}])

    def test_saved_document_size_limits_and_extracted_html_cap(self):
        large = MHTML.replace(b"\nimage\n", b"\n" + b"x" * pf.MAX_BYTES + b"\n")
        for name in ("saved.mhtml", "saved.MHT"):
            with self.subTest(name=name):
                self.assertGreater(len(large), pf.MAX_BYTES)
                self.assertEqual(server.saved_document(name, large)[1], b"<html><body>Saved deadline</body></html>")
        for name in ("saved.html", "saved.pdf", "saved.txt"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "5 MiB"):
                server.saved_document(name, b"x" * (pf.MAX_BYTES + 1))
        for name in ("saved.mhtml", "saved.mht"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "50 MiB"):
                server.saved_document(name, b"x" * (50 * 1024 * 1024 + 1))
        large_html = MHTML.replace(b"Saved =64eadline", b"x" * (pf.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "5 MiB"):
            self.session.upload("saved.mhtml", large_html, MODIFIED)

    def test_handler_filename_specific_content_length_caps(self):
        httpd = server.make_server("127.0.0.1", 0, "secret", self.session)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(worker.join, 5)
        self.addCleanup(httpd.shutdown)
        with patch.object(self.session, "upload", return_value={"ok": True}) as upload:
            for name, size, accepted in (("saved.html", pf.MAX_BYTES, True),
                                         ("saved.pdf", pf.MAX_BYTES + 1, False),
                                         ("saved.txt", pf.MAX_BYTES + 1, False),
                                         ("saved.html", pf.MAX_BYTES + 1, False),
                                         ("saved.mhtml", pf.MAX_BYTES + 1, True),
                                         ("保存.MHT", pf.MAX_BYTES + 1, True),
                                         ("saved.mhtml", 50 * 1024 * 1024 + 1, False),
                                         ("saved.mht", 50 * 1024 * 1024 + 1, False)):
                with self.subTest(name=name, size=size):
                    connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
                    try:
                        headers = {"X-File-Name": quote(name), "Content-Length": str(size)}
                        connection.request("POST", "/secret/upload", b"x" * size if accepted else b"", headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200 if accepted else 400)
                        result = json.loads(response.read())
                        if not accepted:
                            self.assertIn("50 MiB" if name.endswith((".mhtml", ".mht")) else "5 MiB", result["error"])
                    finally:
                        connection.close()
            self.assertEqual(upload.call_count, 3)

    def test_snapshot_rejections_propagate(self):
        for name, body, reason in (("login.html", b'<p>Login</p><input type="password">', "login page"),
                                   ("challenge.html", b"<p>captcha</p>", "bot challenge"),
                                   ("empty.html", b"<script>app</script>", "visible text"),
                                   ("image.png", b"PNG", "HTML, PDF"),
                                   ("huge.txt", b"x" * (pf.MAX_BYTES + 1), "5 MiB")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, reason):
                self.session.upload(name, body, MODIFIED, URL)
        self.assertFalse((self.fetch_root / "log.jsonl").exists())
        self.assertEqual(self.session.imported, {})

    def test_filter_manual_save_candidates(self):
        for status in (403, 429, 500, 503):
            self.assertTrue(server.needs_manual_save({"status": status}))
        self.assertTrue(server.needs_manual_save({"status": 200, "reason": "bot challenge detected"}))
        for entry in ({"status": 400}, {"status": 404}, {"status": 410}, {"status": None},
                      {"status": 200, "reason": "empty visible text"},
                      {"status": 403, "content_type": "image/png"},
                      {"status": 200, "reason": "unsupported content type: image/png"},
                      {"status": 403, "category": "url_rejection"}):
            self.assertFalse(server.needs_manual_save(entry))

    def test_handler_token_upload_and_rejection(self):
        httpd = server.make_server("127.0.0.1", 0, "secret", self.session)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(worker.join, 5)
        self.addCleanup(httpd.shutdown)
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/wrong/")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        connection.request("GET", "/secret/")
        response = connection.getresponse()
        page = response.read().decode()
        self.assertIn("試験株式会社", page)
        self.assertIn("未インポート", page)
        headers = {"X-File-Name": quote("保存.html"), "X-Last-Modified": MODIFIED}
        connection.request("POST", "/secret/upload", b"<p>Saved</p>", headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("行へ", json.loads(response.read())["error"])
        headers["X-Target-URL"] = quote(URL)
        connection.request("POST", "/wrong/upload", b"<p>Saved</p>", headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        headers["Origin"] = "http://evil.example"
        connection.request("POST", "/secret/upload", b"<p>Saved</p>", headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        del headers["Origin"]
        connection.request("POST", "/secret/upload", b"<p>Saved</p>", headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["requested_url"], URL)
        connection.request("GET", "/secret/")
        page = connection.getresponse().read().decode()
        self.assertIn("完了", page)
        self.assertIn(f"--company-ids {CID}", page)
        self.assertEqual(pf.unresolved_failures(), [])


if __name__ == "__main__":
    unittest.main()
