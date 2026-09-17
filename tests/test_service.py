"""The SSRF guard and the HTTP surface. No network required."""

import unittest

from fastapi.testclient import TestClient

from service.app import app
from service.security import UnsafeURL, guard_route, validate


class FakeRequest:
    def __init__(self, url, navigation=True):
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


class SSRFGuardTest(unittest.TestCase):
    def test_blocks_infrastructure_addresses(self):
        blocked = [
            "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata
            "http://metadata.google.internal/",           # GCP metadata by name
            "http://127.0.0.1:8080/admin",                # loopback
            "http://localhost/",                          # loopback by name
            "http://10.0.0.5/",                           # RFC1918
            "http://192.168.1.1/",                        # RFC1918
            "http://172.16.0.9/",                         # RFC1918
            "http://[::1]/",                              # IPv6 loopback
            "http://0.0.0.0/",                            # this network
            "http://consul.service.local/",               # internal TLD
        ]
        for url in blocked:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURL):
                    validate(url)

    def test_blocks_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURL):
                    validate(url)

    def test_rejects_junk(self):
        for url in ("", "https://", "not a url", "https://" + "a" * 3000):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURL):
                    validate(url)

    def test_allows_a_public_host(self):
        safe = validate("example.com")
        self.assertEqual(safe.hostname, "example.com")
        self.assertTrue(safe.url.startswith("https://"))
        self.assertTrue(safe.addresses)


class RouteGuardTest(unittest.TestCase):
    """The in-browser guard catches a redirect that lands somewhere private."""

    def test_blocks_a_navigation_to_a_private_address(self):
        route = FakeRoute()
        guard_route(route, FakeRequest("http://169.254.169.254/latest/meta-data/"))
        self.assertEqual(route.aborted_with, "blockedbyclient")
        self.assertFalse(route.continued)

    def test_allows_a_public_navigation(self):
        route = FakeRoute()
        guard_route(route, FakeRequest("https://example.com/pricing"))
        self.assertTrue(route.continued)
        self.assertIsNone(route.aborted_with)

    def test_subresources_are_not_re_resolved(self):
        """Only documents are checked; a DNS lookup per image would be crippling."""
        route = FakeRoute()
        guard_route(route, FakeRequest("http://10.0.0.1/logo.png", navigation=False))
        self.assertTrue(route.continued)


class HTTPSurfaceTest(unittest.TestCase):
    def setUp(self):
        import service.app as app_module

        app_module._hits.clear()
        self.client = TestClient(app)

    def test_index_serves_the_form(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Redefine Website Design Audit", response.text)
        self.assertIn('id="form"', response.text)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_service_worker_is_served_from_the_root(self):
        # Its scope must cover /reports/, which only a root-level script can do.
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("/reports/", response.text)
        self.assertIn("register('/sw.js')", self.client.get("/").text)

    def test_reports_are_not_kept_on_the_server(self):
        response = self.client.get("/reports/example.com-site-audit.pdf")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Run the audit again", response.text)

    def test_unsafe_url_is_refused_before_any_browser_starts(self):
        response = self.client.post("/api/audit", json={"url": "http://169.254.169.254/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("reserved", response.json()["detail"].lower())

    def test_missing_url_is_a_client_error(self):
        self.assertEqual(self.client.post("/api/audit", json={}).status_code, 400)

    def test_whole_site_is_the_default_mode(self):
        """The tool audits sites; auditing one page is the opt-out."""
        from service.app import AuditRequest

        self.assertEqual(AuditRequest(url="https://example.com").mode, "site")

    def test_draft_is_off_unless_asked_for(self):
        from service.app import AuditRequest

        self.assertFalse(AuditRequest(url="https://example.com").draft)

    def test_draft_request_returns_report_and_draft_together(self):
        """With draft=true one request carries the PDF (base64) and the HTML draft."""
        import base64
        import service.app as app_module
        from types import SimpleNamespace

        def fake_audit(target, work_dir):
            pdf = work_dir / "report.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            data = SimpleNamespace(final_url="https://example.com/", probe={
                "title": "Example Co", "headings": [{"level": 1, "text": "Hello there"}],
                "body_text": "Example Co Hello there We make examples.", "nav_links": [], "footer_links": [],
                "ctas": [], "links": [], "lang": "en", "meta_description": None,
            })
            results = {
                "scores": {"overall": 61, "grade": "C", "categories": {}, "counts": {}},
                "findings": [], "palette": [], "typography": {"families": []},
            }
            return pdf, data, results

        original = app_module.run_audit
        app_module.run_audit = fake_audit
        try:
            response = self.client.post("/api/audit", json={"url": "https://example.com", "mode": "page", "draft": True})
        finally:
            app_module.run_audit = original

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["score"], 61)
        self.assertEqual(body["report"]["filename"], "example.com-audit.pdf")
        self.assertEqual(base64.b64decode(body["report"]["pdf_base64"]), b"%PDF-1.4 fake")
        self.assertEqual(body["draft"]["filename"], "example.com-home-draft.html")
        self.assertIn("<h1 id=\"hero-title\">Hello there</h1>", body["draft"]["html"])
        self.assertIn("We make examples.", body["draft"]["html"])
        self.assertEqual(body["draft"]["fonts"], {"display": "Fraunces", "body": "DM Sans"})

    def test_the_page_limit_is_clamped(self):
        import service.app as app_module

        captured = {}

        def fake_site(target, work_dir, limit):
            captured["limit"] = limit
            raise app_module.BlockedError("stop here")

        original = app_module.run_site_audit
        app_module.run_site_audit = fake_site
        try:
            self.client.post("/api/audit", json={"url": "https://example.com", "limit": 9999})
            self.assertEqual(captured["limit"], app_module.SITE_PAGE_MAX)
            app_module._hits.clear()
            self.client.post("/api/audit", json={"url": "https://example.com", "limit": 0})
            self.assertEqual(captured["limit"], 1)
        finally:
            app_module.run_site_audit = original

    def test_a_site_audit_costs_more_rate_budget_than_a_page(self):
        import service.app as app_module

        app_module._hits.clear()
        self.assertFalse(app_module.rate_limited("1.2.3.4", app_module.SITE_RATE_COST))
        # The hourly allowance should now be mostly spent by that one site audit.
        remaining = app_module.RATE_LIMIT_PER_HOUR - app_module.SITE_RATE_COST
        for _ in range(remaining):
            self.assertFalse(app_module.rate_limited("1.2.3.4"))
        self.assertTrue(app_module.rate_limited("1.2.3.4"))

    def test_a_blocked_site_returns_422_not_a_report(self):
        """The wiring, not the detector: a BlockedError must not become a PDF."""
        import service.app as app_module

        original = app_module.run_audit
        app_module.run_audit = lambda *a, **k: (_ for _ in ()).throw(
            app_module.BlockedError("example.com blocked the audit (Cloudflare).")
        )
        try:
            response = self.client.post(
                "/api/audit", json={"url": "https://example.com", "mode": "page"}
            )
        finally:
            app_module.run_audit = original

        self.assertEqual(response.status_code, 422)
        self.assertIn("blocked", response.json()["detail"].lower())
        self.assertNotIn("application/pdf", response.headers.get("content-type", ""))

    def test_body_must_be_valid(self):
        self.assertEqual(self.client.post("/api/audit", json={"url": 42}).status_code, 422)


if __name__ == "__main__":
    unittest.main()


class BlockDetectionTest(unittest.TestCase):
    """A bot wall must be reported as a wall, never scored as a design."""

    def _page(self, status=200, title="", text="", runs=200):
        from website_audit.collector import PageData

        return PageData(
            url="https://example.com",
            final_url="https://example.com",
            status=status,
            load_ms=100,
            probe={"title": title, "body_text_sample": text, "text_runs": [{}] * runs},
            mobile={},
        )

    def test_detects_the_page_that_shipped_a_false_grade_a(self):
        from website_audit.blocking import detect

        verdict = detect(self._page(
            status=403,
            title="Access denied",
            text="Sorry, you have been blocked. You are unable to access "
                 "secureservercdn2.net. Cloudflare Ray ID: 8f2c",
            runs=12,
        ))
        self.assertTrue(verdict)
        self.assertEqual(verdict.vendor, "Cloudflare")
        self.assertIn("403", verdict.reason)

    def test_detects_a_200_challenge_page(self):
        from website_audit.blocking import detect

        verdict = detect(self._page(
            status=200,
            title="Just a moment...",
            text="Just a moment... Enable JavaScript and cookies to continue",
            runs=6,
        ))
        self.assertTrue(verdict)

    def test_a_real_page_is_not_flagged(self):
        from website_audit.blocking import detect

        verdict = detect(self._page(
            status=200,
            title="Acme — Insurance for families",
            text="Innovative health coverage tailored to your individual needs. "
                 "Apply anytime of the year. Get started now.",
            runs=180,
        ))
        self.assertFalse(verdict)

    def test_an_article_about_being_blocked_is_not_flagged(self):
        """Phrase matching alone must not condemn a long, real page."""
        from website_audit.blocking import detect

        verdict = detect(self._page(
            status=200,
            title="Why your users see 'access denied'",
            text="access denied is a common error. " + ("Body copy. " * 100),
            runs=300,
        ))
        self.assertFalse(verdict)

    def test_explanation_names_the_host_and_the_remedy(self):
        from website_audit.blocking import detect, explain

        verdict = detect(self._page(status=403, text="sorry, you have been blocked", runs=5))
        message = explain(verdict, "healthpro-consultants.com")
        self.assertIn("healthpro-consultants.com", message)
        self.assertIn("allowlist", message)
