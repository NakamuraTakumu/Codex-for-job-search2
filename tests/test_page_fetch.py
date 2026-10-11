"""No real network: DNS, curl and time are mocked in every acquisition test."""
import contextlib
import hashlib
import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jobs.common import page_fetch as pf

URL = "https://example.com/recruit"
STAMP = "2026-10-11T00:00:00+00:00"


def answers(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
             6, "", (ip, 443)) for ip in ips]


class PageFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fetch"
        self.clock = 100.0
        self.sleeps = []
        pf._HOST_LOCKS.clear()
        pf._ROBOTS_LOCKS.clear()
        pf._LAST_REQUEST.clear()
        pf._COOLDOWN.clear()
        pf._ROBOTS.clear()
        patches = [patch.object(pf, "FETCH_ROOT", self.root),
                   patch.object(pf.socket, "getaddrinfo", return_value=answers("8.8.8.8")),
                   patch.object(pf.subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
                   patch.object(pf.time, "monotonic", side_effect=lambda: self.clock),
                   patch.object(pf.time, "sleep", side_effect=self.sleep)]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.dns, self.run = self.mocks[1:3]

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def curl(self, status=200, body="<html><body><p>2028年1月31日 締切</p></body></html>".encode(), headers=None):
        headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}

        def result(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text(
                f"HTTP/1.1 {status} test\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n")
            Path(command[command.index("--output") + 1]).write_bytes(body)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return result

    def live(self, **kwargs):
        self.run.side_effect = self.curl(**kwargs)
        with patch.object(pf, "_robots_allowed", return_value=True):
            return json.loads(pf.fetch_page(URL))

    def test_reject_non_global_ipv4_ipv6_and_mixed_answers(self):
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.2.3", "192.168.1.1", "169.254.1.1",
                   "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1", "240.0.0.1",
                   "192.0.2.1", "::1", "::", "fc00::1", "fd00::1", "fe80::1", "ff02::1",
                   "::ffff:127.0.0.1", "::ffff:100.64.1.1"):
            for ips in ((ip,), ("8.8.8.8", ip)):
                with self.subTest(ips=ips):
                    self.dns.return_value = answers(*ips)
                    result = json.loads(pf.fetch_page(URL))
                    self.assertFalse(result["ok"])
                    self.assertIn("SSRF guard", result["error"])
        self.run.assert_not_called()
        self.assertTrue(all(row["category"] == "url_rejection" for row in pf.read_log(self.root / "failures.jsonl")))

    def test_reject_bad_scheme_userinfo_port_and_localhost_names(self):
        for url in ("ftp://example.com/a", "file:///etc/passwd", "gopher://example.com",
                    "https://user@example.com/", "https://user:pass@example.com/",
                    "https://example.com:22/", "https://example.com:0/", "https://example.com:8000/",
                    "https://example.com:bad/", "https://example.com:65536/", "http://localhost/",
                    "http://local.localhost/", "http://LOCALHOST./", "https://example.com/\nfoo",
                    "https://example.com\\@127.0.0.1/", "http://[fe80::1%eth0]/"):
            with self.subTest(url=url):
                result = json.loads(pf.fetch_page(url))
                self.assertFalse(result["ok"])
        self.run.assert_not_called()

    def test_redirect_to_private_is_rechecked_and_rejected(self):
        self.dns.side_effect = lambda host, *a, **k: answers("127.0.0.1" if host == "127.0.0.1" else "8.8.8.8")
        self.run.side_effect = self.curl(status=302, headers={"Location": "http://127.0.0.1/private"})
        with patch.object(pf, "_robots_allowed", return_value=None):
            result = json.loads(pf.fetch_page(URL))
        self.assertFalse(result["ok"])
        self.assertIn("SSRF guard", result["error"])
        self.assertEqual(result["final_url"], "http://127.0.0.1/private")
        self.assertEqual(self.run.call_count, 1)

    def test_redirect_limit_and_relative_url(self):
        self.run.side_effect = self.curl(status=302, headers={"Location": "/next"})
        with patch.object(pf, "_robots_allowed", return_value=None):
            result = json.loads(pf.fetch_page(URL))
        self.assertIn("5 redirects", result["error"])
        self.assertEqual(self.run.call_count, 6)
        self.assertEqual(self.run.call_args.args[0][-1], "https://example.com/next")

    def test_curl_pins_checked_ip_and_disables_external_configuration(self):
        self.run.side_effect = self.curl()
        pf._curl_get(URL)
        command = self.run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/curl", "--disable"])
        for flag, value in (("--resolve", "example.com:443:8.8.8.8"), ("--proto", "=http,https"),
                            ("--noproxy", "*"), ("--proxy", ""), ("--max-time", "20"),
                            ("--max-filesize", str(5 * 1024 * 1024)), ("--request", "GET"),
                            ("--user-agent", pf.USER_AGENT)):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("--location", command)
        self.assertNotIn("--cookie", command)
        self.assertNotIn("--user", command)
        self.dns.assert_called_once()

    def test_ipv6_global_pinning(self):
        self.dns.return_value = answers("2606:4700:4700::1111")
        self.run.side_effect = self.curl()
        pf._curl_get("https://[2606:4700:4700::1111]/")
        command = self.run.call_args.args[0]
        self.assertIn("*:443:[2606:4700:4700::1111]", command)

    def test_timeout_and_curl_size_errors_logged(self):
        for code, expected in ((28, "timeout"), (63, "5 MiB")):
            with self.subTest(code=code), patch.object(pf, "_robots_allowed", return_value=None):
                self.run.side_effect = None
                self.run.return_value = subprocess.CompletedProcess([], code)
                result = json.loads(pf.fetch_page(URL))
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])
        self.assertEqual(len(pf.read_log(self.root / "failures.jsonl")), 2)

    def test_subprocess_timeout_is_json_failure(self):
        self.run.side_effect = subprocess.TimeoutExpired("curl", 22)
        with patch.object(pf, "_robots_allowed", return_value=None):
            result = json.loads(pf.fetch_page(URL))
        self.assertFalse(result["ok"])
        self.assertIn("TimeoutExpired", result["error"])

    def test_body_size_checked_even_without_content_length(self):
        with patch.object(pf, "MAX_BYTES", 16):
            result = self.live(body=b"x" * 17)
        self.assertIn("5 MiB", result["error"])

    def test_challenge_status_and_body_are_not_successes(self):
        for status, body in ((403, b"blocked"), (429, b"rate limit"), (503, b"busy"),
                             (200, b"<p>captcha</p>"),
                             (200, b"<head><title>Just a moment...</title></head><body>Wait</body>")):
            with self.subTest(status=status, body=body):
                self.assertFalse(self.live(status=status, body=body)["ok"])
        self.assertEqual(len(pf.read_log(self.root / "failures.jsonl")), 5)
        self.assertFalse((self.root / "log.jsonl").exists())

    def test_retry_after_honored_once_capped_without_retry(self):
        result = self.live(status=429, headers={"Retry-After": "1000"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.sleeps, [30])
        self.assertEqual(pf._retry_seconds("invalid"), 0)
        with patch.object(pf.time, "time", return_value=0):
            self.assertEqual(pf._retry_seconds("Thu, 01 Jan 1970 00:00:10 GMT"), 10)

    def test_rate_limiter_is_process_shared_and_thread_safe(self):
        starts = []
        fake_curl = self.curl()
        def run(command, **kwargs):
            starts.append(self.clock)
            return fake_curl(command, **kwargs)
        self.run.side_effect = run
        errors = []
        def call():
            try:
                pf._curl_get(URL)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(starts, [100, 103, 106, 109])
        self.assertFalse(any(t.is_alive() for t in threads))

    def test_independent_job_fetchers_share_host_spacing(self):
        starts, results, errors = [], [], []
        fake_curl = self.curl()
        def run(command, **kwargs):
            starts.append(self.clock)
            return fake_curl(command, **kwargs)
        self.run.side_effect = run
        jobs = [pf.JobPageFetcher(cap=1, job_name=f"job-{i}") for i in range(4)]
        def call(job, url):
            try:
                results.append(json.loads(job.fetch_page(url)))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=call, args=(job, URL + str(i)))
                   for i, job in enumerate(jobs)]
        with patch.object(pf, "_robots_allowed", return_value=True):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(starts, [100, 103, 106, 109])
        for i, job in enumerate(jobs):
            self.assertEqual(job.fetched_urls(), {URL + str(i)})

    def test_different_hosts_do_not_share_spacing(self):
        self.run.side_effect = self.curl()
        pf._curl_get(URL)
        pf._curl_get("https://other.example/recruit")
        self.assertEqual(self.sleeps, [])

    def test_robots_once_per_host_reports_disallow_but_does_not_block(self):
        def run(command, **kwargs):
            url = command[-1]
            if url.endswith("/robots.txt"):
                return self.curl(body=b"User-agent: company-survey-fetch\nDisallow: /recruit\n",
                                 headers={"Content-Type": "text/plain"})(command, **kwargs)
            return self.curl()(command, **kwargs)
        self.run.side_effect = run
        first = json.loads(pf.fetch_page(URL))
        second = json.loads(pf.fetch_page(URL + "2"))
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["robots_allowed"])
        self.assertEqual(sum(c.args[0][-1].endswith("/robots.txt") for c in self.run.call_args_list), 1)
        self.assertEqual(self.sleeps, [3, 3])

    def test_robots_redirect_is_guarded_and_failure_is_unknown(self):
        self.dns.side_effect = lambda host, *a, **k: answers("127.0.0.1" if host == "127.0.0.1" else "8.8.8.8")
        def run(command, **kwargs):
            if command[-1].endswith("/robots.txt"):
                return self.curl(status=302, headers={"Location": "http://127.0.0.1/robots.txt"})(command, **kwargs)
            return self.curl()(command, **kwargs)
        self.run.side_effect = run
        result = json.loads(pf.fetch_page(URL))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["robots_allowed"])
        self.assertEqual(self.run.call_count, 2)

    def test_visible_html_raw_snapshot_hash_and_full_companion(self):
        body = ("<head><title>hidden title</title></head><script>evil</script><style>bad</style>"
                "<noscript>hidden</noscript><p>First<br>Second</p><div hidden>not visible</div>"
                "<div style='display:none'>invisible</div><p>" + "長" * 41000 + "</p>").encode()
        result = self.live(body=body)
        self.assertTrue(result["ok"])
        self.assertNotIn("text", result)
        self.assertNotIn("truncated", result)
        text = Path(result["text_path"]).read_text(encoding="utf-8")
        self.assertEqual(result["text_chars"], len(text))
        self.assertGreater(len(text), 40000)
        self.assertTrue(text.startswith("First\nSecond\n"))
        self.assertTrue(text.endswith("長" * 41000))
        self.assertNotIn("evil", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("invisible", text)
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(result["snapshot_id"], digest)
        self.assertEqual(Path(result["raw_path"]).read_bytes(), body)
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["raw_path"], str(self.root / "snapshots" / f"{digest}.html"))
        self.assertEqual(result["text_path"], str(self.root / "snapshots" / f"{digest}.extracted.txt"))
        self.assertTrue(Path(result["raw_path"]).is_absolute())
        self.assertTrue(Path(result["text_path"]).is_absolute())
        entry = pf.read_log(self.root / "log.jsonl")[0]
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual(entry["origin"], "host")
        self.assertEqual(entry["fetched_at"], result["fetched_at"])
        for key in ("raw_path", "text_path", "bytes", "text_chars", "robots_allowed"):
            self.assertEqual(entry[key], result[key])
        self.assertNotIn("text", entry)

    def test_table_html_preserved_byte_exact(self):
        body = ('<!DOCTYPE html>\r\n<table><tr><th>ES受付開始日</th>'
                '<td class="schedule_table_cool" data-v-51610a2b>3/1<br>00:00</td>'
                '<td>3/16<br>08:01</td><td rowspan="3">決まり次第<br>ご案内</td>'
                '</tr></table>\r\n').encode()
        result = self.live(body=body)
        self.assertTrue(result["ok"])
        self.assertNotIn("text", result)
        self.assertEqual(result["snapshot_id"], hashlib.sha256(body).hexdigest())
        self.assertEqual(Path(result["raw_path"]).read_bytes(), body)
        self.assertIn(b'rowspan="3"', Path(result["raw_path"]).read_bytes())

    def test_empty_visible_text_is_failure_without_snapshot(self):
        result = self.live(body=b"<script>dates</script><p hidden>deadline</p>")
        self.assertFalse(result["ok"])
        self.assertIn("empty visible text", result["error"])
        self.assertNotIn("text", result)
        self.assertIsNone(result["raw_path"])
        self.assertIsNone(result["text_path"])
        self.assertFalse((self.root / "snapshots").exists())

    def test_plain_text_raw_and_companion_do_not_collide(self):
        body = b"  Saved deadline\r\n"
        digest = hashlib.sha256(body).hexdigest()
        result = self.live(body=body, headers={"Content-Type": "text/plain; charset=utf-8"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["raw_path"], str(self.root / "snapshots" / f"{digest}.txt"))
        self.assertNotEqual(result["raw_path"], result["text_path"])
        self.assertEqual(Path(result["raw_path"]).read_bytes(), body)
        self.assertEqual(Path(result["text_path"]).read_text(), "Saved deadline")

    def test_xhtml_extension_and_declared_encoding(self):
        body = '<html><p>締切</p></html>'.encode("shift_jis")
        result = self.live(body=body, headers={"Content-Type": "application/xhtml+xml; charset=shift_jis"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["raw_path"].endswith(".html"))
        self.assertEqual(Path(result["raw_path"]).read_bytes(), body)
        self.assertEqual(Path(result["text_path"]).read_text(), "締切")

    def test_unsupported_type_is_logged(self):
        result = self.live(body=b"{}", headers={"Content-Type": "application/json"})
        self.assertFalse(result["ok"])
        self.assertIn("unsupported", pf.read_log(self.root / "failures.jsonl")[0]["reason"])

    def test_pdf_uses_pdftotext_or_fails_without_it(self):
        with patch.object(pf.shutil, "which", return_value=None):
            self.assertIn("pdftotext", self.live(body=b"%PDF", headers={"Content-Type": "application/pdf"})["error"])
        with patch.object(pf.shutil, "which", return_value="/usr/bin/pdftotext"):
            def run(command, **kwargs):
                if command[0].endswith("pdftotext"):
                    self.assertIn("-layout", command)
                    return subprocess.CompletedProcess(command, 0, b"2028-01-31 deadline")
                return self.curl(body=b"%PDF", headers={"Content-Type": "application/pdf"})(command, **kwargs)
            self.run.side_effect = run
            with patch.object(pf, "_robots_allowed", return_value=True):
                result = json.loads(pf.fetch_page(URL))
            self.assertTrue(result["ok"])
            self.assertNotIn("text", result)
            self.assertEqual(result["snapshot_id"], hashlib.sha256(b"%PDF").hexdigest())
            self.assertTrue(result["raw_path"].endswith(".pdf"))
            self.assertEqual(Path(result["raw_path"]).read_bytes(), b"%PDF")
            self.assertEqual(Path(result["text_path"]).read_text(), "2028-01-31 deadline")

    def test_per_job_cap_failure_context_and_independent_instances(self):
        fetcher = pf.JobPageFetcher(cap=2, company_id="1234567890123", recruitment_year=2028, job_name="fill")
        self.run.side_effect = self.curl(status=403)
        with patch.object(pf, "_robots_allowed", return_value=None):
            for _ in range(3):
                self.assertFalse(json.loads(fetcher.fetch_page(URL))["ok"])
            other = pf.JobPageFetcher(cap=1)
            self.assertFalse(json.loads(other.fetch_page(URL))["ok"])
        self.assertEqual(self.run.call_count, 3)
        failures = pf.read_log(self.root / "failures.jsonl")
        self.assertEqual(len(failures), 4)
        self.assertIn("cap", failures[2]["reason"])
        self.assertEqual(failures[2]["company_id"], "1234567890123")
        self.assertEqual(failures[2]["recruitment_year"], 2028)
        self.assertEqual(failures[2]["job_name"], "fill")

    def test_job_success_is_immutable_for_repeated_calls(self):
        self.run.side_effect = self.curl()
        with patch.object(pf, "_robots_allowed", return_value=None):
            job = pf.JobPageFetcher()
            first = json.loads(job.fetch_page(URL))
            self.run.side_effect = self.curl(status=403)
            second = json.loads(job.fetch_page(URL))
        self.assertEqual(first, second)
        self.assertNotIn("text", second)
        self.assertTrue(Path(second["raw_path"]).is_file())
        self.assertTrue(Path(second["text_path"]).is_file())
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(job.fetched_urls(), {URL})

    def test_job_normalized_cache_aliases_never_bypass_guards(self):
        self.run.side_effect = self.curl()
        with patch.object(pf, "_robots_allowed", return_value=None):
            job = pf.JobPageFetcher()
            first = json.loads(job.fetch_page(URL))
            alias = "https://EXAMPLE.COM.:443/recruit#deadline"
            self.assertEqual(json.loads(job.fetch_page(alias)), first)
            self.assertEqual(self.run.call_count, 1)
            rejected = json.loads(job.fetch_page("https://user@example.com/recruit"))
            self.assertFalse(rejected["ok"])
            self.assertIn("userinfo", rejected["error"])
            self.dns.return_value = answers("127.0.0.1")
            rejected = json.loads(job.fetch_page(URL))
            self.assertFalse(rejected["ok"])
            self.assertIn("SSRF guard", rejected["error"])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(len(pf.read_log(self.root / "failures.jsonl")), 2)

    def test_manual_html_and_text_import_without_network(self):
        for suffix, body, expected in (("html", "<script>hidden</script><p>Saved deadline</p>", "Saved deadline"),
                                       ("txt", "Saved plain text", "Saved plain text")):
            path = Path(self.temp.name) / f"saved.{suffix}"
            path.write_text(body)
            result = pf.import_snapshot(URL, path, STAMP)
            self.assertNotIn("text", result)
            self.assertEqual(Path(result["text_path"]).read_text(), expected)
            self.assertEqual(Path(result["raw_path"]).read_bytes(), body.encode())
            self.assertEqual(result["snapshot_id"], hashlib.sha256(body.encode()).hexdigest())
            self.assertEqual(result["bytes"], len(body.encode()))
            self.assertEqual(result["text_chars"], len(expected))
            self.assertNotEqual(result["raw_path"], result["text_path"])
            self.assertEqual(result["origin"], "manual")
            self.assertEqual(result["fetched_at"], STAMP)
        self.dns.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(len(pf.read_log(self.root / "log.jsonl")), 2)

    def test_manual_pdf_import(self):
        path = Path(self.temp.name) / "saved.pdf"
        path.write_bytes(b"%PDF")
        with patch.object(pf.shutil, "which", return_value="pdftotext"):
            self.run.side_effect = None
            self.run.return_value = subprocess.CompletedProcess([], 0, b"Saved PDF date")
            result = pf.import_snapshot(URL, path, STAMP)
        self.assertNotIn("text", result)
        self.assertEqual(Path(result["raw_path"]).read_bytes(), b"%PDF")
        self.assertEqual(result["snapshot_id"], hashlib.sha256(b"%PDF").hexdigest())
        self.assertEqual(Path(result["text_path"]).read_text(), "Saved PDF date")
        self.dns.assert_not_called()

    def test_manual_import_rejects_bad_timestamp_empty_and_challenge(self):
        path = Path(self.temp.name) / "saved.txt"
        path.write_text("Saved")
        for timestamp in ("2026-10-11T00:00:00", "2026-10-11T00:00:00+09:00"):
            with self.assertRaisesRegex(ValueError, "UTC"):
                pf.import_snapshot(URL, path, timestamp)
        for body in ("", "Just a moment captcha"):
            path.write_text(body)
            with self.assertRaises(ValueError):
                pf.import_snapshot(URL, path, STAMP)

    def test_manual_import_rejects_challenge_in_hidden_title(self):
        path = Path(self.temp.name) / "saved.html"
        path.write_text("<head><title>Just a moment...</title></head><body>Please wait</body>")
        with self.assertRaisesRegex(ValueError, "bot challenge"):
            pf.import_snapshot(URL, path, STAMP)
        self.assertFalse((self.root / "log.jsonl").exists())

    def test_cli_manual_import_resolves_failure_using_import_time(self):
        from scripts import import_page_snapshot as importer
        from scripts import page_fetch_failures as listing
        path = Path(self.temp.name) / "saved.txt"
        path.write_text("Saved date")
        self.live(status=403)
        before = io.StringIO()
        with patch("sys.argv", ["failures", "--root", str(self.root), "--json"]), contextlib.redirect_stdout(before):
            listing.main()
        self.assertEqual(len(json.loads(before.getvalue())), 1)
        imported = io.StringIO()
        with patch("sys.argv", ["import", URL, str(path), "--fetched-at", STAMP]), contextlib.redirect_stdout(imported):
            importer.main()
        metadata = json.loads(imported.getvalue())
        self.assertEqual(metadata["origin"], "manual")
        self.assertEqual(metadata["fetched_at"], STAMP)
        self.assertNotIn("text", metadata)
        self.assertEqual(Path(metadata["raw_path"]).read_bytes(), path.read_bytes())
        self.assertEqual(Path(metadata["text_path"]).read_text(), "Saved date")
        self.assertEqual(pf.unresolved_failures(self.root), [])
        self.assertGreater(pf.read_log(self.root / "log.jsonl")[-1]["logged_at"],
                           pf.read_log(self.root / "failures.jsonl")[-1]["attempted_at"])

    def test_cli_lists_context_and_filters_unsafe_urls(self):
        from scripts import page_fetch_failures as listing
        job = pf.JobPageFetcher(company_id="1234567890123")
        self.run.side_effect = self.curl(status=403)
        with patch.object(pf, "_robots_allowed", return_value=None):
            job.fetch_page(URL)
            job.fetch_page("http://localhost/")
        out = io.StringIO()
        with patch("sys.argv", ["failures", "--root", str(self.root)]), contextlib.redirect_stdout(out):
            listing.main()
        self.assertIn(URL, out.getvalue())
        self.assertIn("1234567890123", out.getvalue())
        self.assertIn("HTTP 403", out.getvalue())
        self.assertNotIn("localhost", out.getvalue())
        out = io.StringIO()
        with patch("sys.argv", ["failures", "--root", str(self.root), "--json", "--include-rejected"]), contextlib.redirect_stdout(out):
            listing.main()
        self.assertEqual(len(json.loads(out.getvalue())), 2)

    def test_cli_import_reports_invalid_input(self):
        from scripts import import_page_snapshot as importer
        with patch("sys.argv", ["import", URL, str(Path(self.temp.name) / "missing.txt")]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                importer.main()
        self.assertEqual(exc.exception.code, 2)
        self.run.assert_not_called()

    def test_live_failure_falls_back_to_manual_with_live_error_and_failure_log(self):
        path = Path(self.temp.name) / "saved.txt"
        path.write_text("2028-01-31 deadline")
        manual = pf.import_snapshot(URL, path, STAMP)
        result = self.live(status=403)
        self.assertTrue(result["ok"])
        self.assertEqual(result["origin"], "manual")
        self.assertEqual(result["snapshot_id"], manual["snapshot_id"])
        self.assertEqual(result["fetched_at"], STAMP)
        self.assertIn("HTTP 403", result["live_error"])
        self.assertNotIn("text", result)
        for key in ("raw_path", "text_path", "bytes", "text_chars", "robots_allowed"):
            self.assertEqual(result[key], manual[key])
        self.assertEqual(len(pf.read_log(self.root / "failures.jsonl")), 1)
        self.assertEqual(len(pf.read_log(self.root / "log.jsonl")), 1)

    def test_live_success_never_replaced_by_manual_and_unsafe_url_never_falls_back(self):
        path = Path(self.temp.name) / "saved.txt"
        path.write_text("Old manually saved date")
        pf.import_snapshot(URL, path, STAMP)
        result = self.live()
        self.assertTrue(result["ok"])
        self.assertEqual(result["origin"], "host")
        self.assertNotIn("Old", Path(result["text_path"]).read_text())
        pf.import_snapshot("http://127.0.0.1/", path, STAMP)
        self.dns.return_value = answers("127.0.0.1")
        self.assertFalse(json.loads(pf.fetch_page("http://127.0.0.1/"))["ok"])

    def test_corrupt_manual_snapshot_not_used(self):
        path = Path(self.temp.name) / "saved.txt"
        path.write_text("Saved date")
        manual = pf.import_snapshot(URL, path, STAMP)
        Path(manual["raw_path"]).write_text("tampered")
        self.assertFalse(self.live(status=403)["ok"])

    def test_old_manual_snapshot_fallback_returns_paths_without_rewriting(self):
        text = "Old saved deadline\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        snapshot = self.root / "snapshots" / f"{digest}.txt"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(text.encode())
        entry = dict(requested_url=URL, final_url=URL, fetched_at=STAMP,
                     status=200, content_type="text/html", snapshot_id=digest,
                     sha256=digest, origin="manual", truncated=True)
        pf._append_jsonl(self.root / "log.jsonl", entry)
        log_before = (self.root / "log.jsonl").read_bytes()
        result = self.live(status=403)
        self.assertTrue(result["ok"])
        self.assertEqual(result["origin"], "manual")
        self.assertEqual(result["snapshot_id"], digest)
        self.assertEqual(result["fetched_at"], STAMP)
        self.assertEqual(result["raw_path"], str(snapshot))
        self.assertEqual(result["text_path"], str(snapshot))
        self.assertEqual(result["bytes"], len(text.encode()))
        self.assertEqual(result["text_chars"], len(text))
        self.assertIsNone(result["robots_allowed"])
        self.assertNotIn("text", result)
        self.assertNotIn("truncated", result)
        self.assertIn("HTTP 403", result["live_error"])
        self.assertEqual(snapshot.read_bytes(), text.encode())
        self.assertEqual((self.root / "log.jsonl").read_bytes(), log_before)
        self.assertEqual(list(snapshot.parent.iterdir()), [snapshot])
        snapshot.write_text("corrupt")
        self.assertFalse(self.live(status=403)["ok"])

    def test_import_does_not_rewrite_existing_legacy_snapshot(self):
        body = b"Saved deadline"
        digest = hashlib.sha256(body).hexdigest()
        old = self.root / "snapshots" / f"{digest}.txt"
        old.parent.mkdir(parents=True)
        old.write_bytes(body)
        before = old.stat().st_mtime_ns
        saved = Path(self.temp.name) / "saved.txt"
        saved.write_bytes(body)
        result = pf.import_snapshot(URL, saved, STAMP)
        self.assertEqual(result["raw_path"], str(old))
        self.assertEqual(old.read_bytes(), body)
        self.assertEqual(old.stat().st_mtime_ns, before)
        self.assertEqual(Path(result["text_path"]).read_bytes(), body)

    def test_failures_deduplicated_and_resolved_by_later_success(self):
        fail = dict(requested_url=URL, final_url=URL, status=403,
                    robots_allowed=None, company_id="1234567890123", category="fetch_failure")
        pf._append_jsonl(self.root / "failures.jsonl", {**fail, "attempted_at": "2026-10-10T00:00:00+00:00", "reason": "old"})
        pf._append_jsonl(self.root / "failures.jsonl", {**fail, "attempted_at": STAMP, "reason": "latest"})
        pf._append_jsonl(self.root / "log.jsonl", dict(requested_url=URL, final_url=URL, fetched_at="2026-10-10T12:00:00+00:00"))
        listed = pf.unresolved_failures(self.root)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["reason"], "latest")
        self.assertEqual(listed[0]["company_id"], "1234567890123")
        pf._append_jsonl(self.root / "log.jsonl", dict(requested_url=URL + "?alias=1", final_url=URL, fetched_at="2026-10-11T00:01:00+00:00"))
        self.assertEqual(pf.unresolved_failures(self.root), [])

    def test_concurrent_persistent_appends_are_complete(self):
        def write(index):
            pf._append_jsonl(self.root / "failures.jsonl", {"index": index})
        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({e["index"] for e in pf.read_log(self.root / "failures.jsonl")}, set(range(20)))


if __name__ == "__main__":
    unittest.main()
