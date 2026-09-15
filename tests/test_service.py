"""The SSRF guard and the HTTP surface. No network required."""

import unittest

from fastapi.testclient import TestClient

from service.app import app
from service.security import UnsafeURL, validate


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


class HTTPSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_index_serves_the_form(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Website design audit", response.text)
        self.assertIn('id="form"', response.text)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_unsafe_url_is_refused_before_any_browser_starts(self):
        response = self.client.post("/api/audit", json={"url": "http://169.254.169.254/"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("reserved", response.json()["detail"].lower())

    def test_missing_url_is_a_client_error(self):
        self.assertEqual(self.client.post("/api/audit", json={}).status_code, 400)

    def test_body_must_be_valid(self):
        self.assertEqual(self.client.post("/api/audit", json={"url": 42}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
