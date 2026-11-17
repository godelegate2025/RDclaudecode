"""Auditing an uploaded HTML file: a design that is not on a website yet."""

import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from service.app import app
from service.security import upload_guard
from website_audit.analysis import analyse
from website_audit.collector import UPLOAD_ORIGIN, collect
from website_audit.report import render_html

FIXTURE = Path(__file__).parent / "fixture.html"


class FakeRequest:
    def __init__(self, url, navigation=False):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    def __init__(self):
        self.aborted_with = None
        self.continued = False

    def abort(self, reason=None):
        self.aborted_with = reason

    def continue_(self):
        self.continued = True


class UploadGuardTest(unittest.TestCase):
    """An uploaded file is written by the uploader, so everything it loads is checked."""

    def _run(self, url):
        route = FakeRoute()
        upload_guard()(route, FakeRequest(url))
        return route

    def test_blocks_an_image_on_a_private_address(self):
        for url in ("http://169.254.169.254/computeMetadata/v1/", "http://10.0.0.1/logo.png",
                    "http://localhost:8080/x.css"):
            with self.subTest(url=url):
                self.assertEqual(self._run(url).aborted_with, "blockedbyclient")

    def test_allows_inline_data(self):
        self.assertTrue(self._run("data:image/png;base64,iVBORw0KGgo=").continued)

    def test_allows_a_public_font_host(self):
        self.assertTrue(self._run("https://example.com/font.woff2").continued)


class Recorder(socketserver.BaseRequestHandler):
    """Counts connections. The file asks for https, which this server can't speak;
    a TLS handshake reaching it is already the leak. (Chrome refuses plain-http
    requests from the https upload page by itself, so those would prove nothing.)"""

    hits: list[int] = []

    def handle(self):
        Recorder.hits.append(1)


class UploadedFileAuditTest(unittest.TestCase):
    """A real browser render of an uploaded file, end to end."""

    @classmethod
    def setUpClass(cls):
        # A local server stands in for an internal address the file must not reach.
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_address[1]
        Recorder.hits = []

        # Without the guard Chromium opens three connections for these.
        sneaky = (
            f'<img src="https://127.0.0.1:{port}/pixel.png" alt="">'
            f"<script>fetch('https://127.0.0.1:{port}/fetched').catch(() => {{}})</script>"
        )
        html = FIXTURE.read_text(encoding="utf-8").replace("</body>", sneaky + "</body>")
        cls.work = Path(tempfile.mkdtemp(prefix="upload-test-"))
        cls.data = collect("", cls.work, html=html, label="Wind & Water.html", route_guard=upload_guard())
        cls.results = analyse(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def finding_ids(self):
        return {f.id for f in self.results["findings"]}

    def test_the_file_is_rendered_and_measured(self):
        self.assertTrue(self.data.is_upload)
        self.assertEqual(self.data.status, 200)
        self.assertTrue(self.data.probe["text_runs"])
        self.assertLessEqual({"a11y-contrast", "a11y-lang", "type-small-body", "resp-viewport"},
                             self.finding_ids())

    def test_checks_that_need_a_live_site_are_left_out(self):
        self.assertIsNone(self.data.site_files)
        hosted_only = {"find-no-robots", "find-no-sitemap", "find-no-llms-txt",
                       "find-no-canonical", "perf-load-time"}
        self.assertFalse(hosted_only & self.finding_ids())

    def test_the_file_cannot_reach_private_addresses(self):
        self.assertEqual(Recorder.hits, [])

    def test_report_names_the_file_not_the_internal_address(self):
        html = render_html(self.data, self.results)
        self.assertIn("Wind &amp; Water.html", html)
        self.assertNotIn(UPLOAD_ORIGIN, html)


class UploadEndpointTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        self.app_module = app_module
        app_module._hits.clear()
        self.client = TestClient(app)
        self.original = app_module.run_upload_audit

    def tearDown(self):
        self.app_module.run_upload_audit = self.original
        self.app_module._hits.clear()

    def post(self, filename="design.html", html="<html><body><h1>Hi</h1></body></html>"):
        return self.client.post("/api/audit-upload", json={"filename": filename, "html": html})

    def test_only_html_files_are_accepted(self):
        response = self.post(filename="design.pdf")
        self.assertEqual(response.status_code, 400)
        self.assertIn(".html", response.json()["detail"])

    def test_an_empty_file_is_refused(self):
        self.assertEqual(self.post(html="   ").status_code, 400)

    def test_a_file_over_the_limit_is_refused_before_any_browser_starts(self):
        self.app_module.run_upload_audit = lambda *a, **k: self.fail("browser should not start")
        limit = self.app_module.MAX_UPLOAD_BYTES
        self.app_module.MAX_UPLOAD_BYTES = 100
        try:
            response = self.post(html="<p>" + "x" * 200 + "</p>")
        finally:
            self.app_module.MAX_UPLOAD_BYTES = limit
        self.assertEqual(response.status_code, 413)

    def test_a_report_comes_back_named_after_the_file(self):
        work = Path(tempfile.mkdtemp(prefix="upload-endpoint-"))
        pdf = work / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        seen = {}

        def fake(html_text, filename, work_dir):
            seen["filename"] = filename
            scores = {"overall": 71, "grade": "C"}
            return pdf, None, {"scores": scores, "findings": [1, 2, 3]}

        self.app_module.run_upload_audit = fake
        response = self.post(filename=r"C:\Users\me\Desktop\Wind & Water.html")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["filename"], "Wind & Water.html")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["X-Audit-Host"], "Wind & Water.html")
        self.assertEqual(response.headers["X-Audit-Score"], "71")
        self.assertEqual(response.headers["X-Audit-Findings"], "3")
        self.assertIn('filename="Wind-&-Water-audit.pdf"', response.headers["content-disposition"])

    def test_a_failed_audit_refunds_its_unit(self):
        self.app_module.run_upload_audit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not audit that file", response.json()["detail"])
        self.assertEqual(self.app_module._hits.get("testclient", []), [])

    def test_the_form_offers_the_upload(self):
        page = self.client.get("/website-audit").text
        self.assertIn('id="file"', page)
        self.assertIn("/api/audit-upload", page)


class CommandLineTest(unittest.TestCase):
    def test_a_local_html_file_is_audited(self):
        from website_audit.cli import main

        out = Path(tempfile.mkdtemp(prefix="upload-cli-")) / "report.pdf"
        self.assertEqual(main([str(FIXTURE), "-o", str(out), "--quiet"]), 0)
        self.assertEqual(out.read_bytes()[:5], b"%PDF-")

    def test_a_file_cannot_be_crawled_as_a_site(self):
        from website_audit.cli import main

        self.assertEqual(main([str(FIXTURE), "--site", "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main()
