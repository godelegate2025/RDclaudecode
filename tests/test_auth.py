"""Team sign-in: the gate, the session cookie, and the Google hand-off. No network."""

import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from service import auth
from service.app import app
from service.team import MemoryStore, use_store

# Never reach for real Firestore from tests.
use_store(MemoryStore())

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "Owner@gmail.com, kian@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
}
OFF = {name: "" for name in SETTINGS}


def signed_in_client(email="owner@gmail.com"):
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.make_session(email, SETTINGS["SESSION_SECRET"].encode()))
    return client


class OffByDefaultTest(unittest.TestCase):
    def test_no_settings_means_no_sign_in(self):
        with mock.patch.dict(os.environ, OFF):
            client = TestClient(app, follow_redirects=False)
            self.assertEqual(client.get("/post-audit").status_code, 200)
            self.assertEqual(client.get("/login").headers["location"], "/")
            self.assertEqual(client.get("/auth/me").json()["sign_in"], "off")


class GateTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app, follow_redirects=False)

    def test_pages_redirect_to_login_and_keep_the_destination(self):
        for path in ("/", "/post-audit", "/website-audit"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 303, path)
            self.assertEqual(response.headers["location"], "/login?next=" + path)

    def test_api_answers_401_not_a_redirect(self):
        response = self.client.post("/api/post-audit", json={"url": "https://www.tiktok.com/@a/video/1"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("sign in", response.json()["detail"].lower())

    def test_login_flow_assets_stay_public(self):
        for path in ("/login", "/static/logo.svg", "/favicon.ico", "/healthz", "/auth/me"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertIn(SETTINGS["GOOGLE_CLIENT_ID"], self.client.get("/login").text)

    def test_signed_in_member_gets_through(self):
        client = signed_in_client()
        self.assertEqual(client.get("/post-audit").status_code, 200)
        me = client.get("/auth/me").json()
        self.assertEqual((me["signed_in"], me["email"]), (True, "owner@gmail.com"))
        # Already signed in: the login page sends you on.
        self.assertEqual(client.get("/login?next=/post-audit").headers["location"], "/post-audit")

    def test_forged_expired_and_removed_sessions_are_refused(self):
        cfg = auth.config()
        good = auth.make_session("owner@gmail.com", cfg.secret)
        payload, signature = good.rsplit(".", 1)
        forged = auth.make_session("owner@gmail.com", b"wrong-secret")
        expired = auth.make_session("owner@gmail.com", cfg.secret, now=time.time() - 15 * 86400)
        outsider = auth.make_session("someone@gmail.com", cfg.secret)
        for value in (forged, expired, outsider, payload + ".x", "garbage", ""):
            self.assertIsNone(auth.read_session(value, cfg), value)
        self.assertEqual(auth.read_session(good, cfg), "owner@gmail.com")

    def test_removing_someone_from_the_list_signs_them_out(self):
        client = signed_in_client("kian@gmail.com")
        self.assertEqual(client.get("/post-audit").status_code, 200)
        with mock.patch.dict(os.environ, {"ALLOWED_EMAILS": "owner@gmail.com"}):
            self.assertEqual(client.get("/post-audit").status_code, 303)

    def test_sign_out_clears_the_cookie(self):
        client = signed_in_client()
        response = client.get("/auth/logout")
        self.assertEqual(response.headers["location"], "/login")
        self.assertIn(f'{auth.COOKIE}=""', response.headers["set-cookie"])


class GoogleHandOffTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app, follow_redirects=False)

    def _claims(self, **overrides):
        claims = {"email": "Owner@Gmail.com", "email_verified": True}
        claims.update(overrides)
        return mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims)

    def test_allowed_account_gets_a_secure_session(self):
        with self._claims() as verify:
            response = self.client.post("/auth/google", json={"credential": "tok", "next": "/post-audit"},
                                        headers={"x-forwarded-proto": "https"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next"], "/post-audit")
        self.assertEqual(verify.call_args.args[2], SETTINGS["GOOGLE_CLIENT_ID"])  # audience is checked
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(flag, cookie)

    def test_account_not_on_the_list_is_refused_by_name(self):
        with self._claims(email="stranger@gmail.com"):
            response = self.client.post("/auth/google", json={"credential": "tok"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("stranger@gmail.com isn't on the Redefine team list", response.json()["detail"])
        self.assertNotIn("set-cookie", response.headers)

    def test_unverified_email_and_bad_tokens_are_refused(self):
        with self._claims(email_verified=False):
            self.assertEqual(self.client.post("/auth/google", json={"credential": "t"}).status_code, 403)
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("bad sig")):
            self.assertEqual(self.client.post("/auth/google", json={"credential": "t"}).status_code, 403)

    def test_next_never_leaves_the_site(self):
        for target in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
            self.assertEqual(auth.safe_next(target), "/", target)
        self.assertEqual(auth.safe_next("/post-audit?x=1"), "/post-audit?x=1")


class HalfConfiguredTest(unittest.TestCase):
    def test_missing_setting_fails_closed(self):
        with mock.patch.dict(os.environ, {**SETTINGS, "SESSION_SECRET": ""}):
            client = TestClient(app, follow_redirects=False)
            page = client.get("/post-audit")
            api = client.post("/api/post-audit", json={"url": "x"})
        self.assertEqual((page.status_code, api.status_code), (503, 503))
        self.assertIn("SESSION_SECRET", api.json()["detail"])


if __name__ == "__main__":
    unittest.main()
