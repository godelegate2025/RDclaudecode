"""Email code sign-in: codes, limits, and the routes on both apps. No network."""

import os
import unittest
from unittest import mock

os.environ.setdefault("DESK_AUTOSTART", "0")

from fastapi.testclient import TestClient

from care_desk.app import create_app
from care_desk.desk import Desk
from care_desk.store import MemoryStore as DeskStore
from service import auth, email_login
from service.app import app
from service.team import MemoryStore, get_team, use_store

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "owner@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
    "RESEND_API_KEY": "re_test",
    "LOGIN_FROM_EMAIL": "Redefine <login@redefine.example>",
}
SECRET = SETTINGS["SESSION_SECRET"].encode()


class CodeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        use_store(MemoryStore())
        get_team().add("mia@studio.ph", "member", by="owner@gmail.com")
        self.store = email_login.MemoryStore()
        email_login.use_store(self.store)
        self.sent = []
        self.t = 1_800_000_000.0

    def ask(self, email="Mia@Studio.ph", after=0):
        self.t += after
        email_login.request_code(email, SECRET, send=lambda to, code: self.sent.append((to, code)), now=self.t)
        return self.sent[-1][1] if self.sent else None

    def check(self, code, email="mia@studio.ph", after=0):
        self.t += after
        return email_login.verify_code(email, code, SECRET, now=self.t)

    def test_a_code_signs_in_once(self):
        code = self.ask()
        self.assertEqual(self.sent, [("mia@studio.ph", code)])
        self.assertNotIn(code, str(self.store.docs))  # only a keyed hash is kept
        self.assertEqual(self.check(f" {code[:3]} {code[3:]} "), "mia@studio.ph")
        with self.assertRaisesRegex(email_login.LoginError, "expired"):
            self.check(code)

    def test_codes_expire_and_allow_five_tries(self):
        code = self.ask()
        with self.assertRaisesRegex(email_login.LoginError, "expired"):
            self.check(code, after=601)
        code = self.ask(after=30)
        wrong = "000000" if code != "000000" else "111111"
        for left in (4, 3, 2, 1):
            with self.assertRaisesRegex(email_login.LoginError, f"{left} tries left"):
                self.check(wrong)
        with self.assertRaisesRegex(email_login.LoginError, "Ask for a new one"):
            self.check(wrong)
        with self.assertRaisesRegex(email_login.LoginError, "Too many"):
            self.check(code)

    def test_never_says_who_is_on_the_team(self):
        self.ask("stranger@gmail.com")
        self.assertEqual((self.sent, self.store.docs), ([], {}))
        with self.assertRaisesRegex(email_login.LoginError, "valid email"):
            self.ask("not-an-email")

    def test_double_clicks_and_floods_send_nothing_new(self):
        first = self.ask()
        self.ask(after=5)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.check(first), "mia@studio.ph")
        for _ in range(4):
            self.ask(after=60)
        self.assertEqual(len(self.sent), 5)
        self.ask(after=60)  # sixth within the hour
        self.assertEqual(len(self.sent), 5)
        self.ask(after=3600)
        self.assertEqual(len(self.sent), 6)

    def test_a_failed_send_can_be_retried_at_once(self):
        def fail(to, code):
            raise email_login.LoginError("Could not send the code. Try again in a minute.")
        with self.assertRaises(email_login.LoginError):
            email_login.request_code("mia@studio.ph", SECRET, send=fail, now=self.t)
        self.assertIsNotNone(self.ask(after=1))

    def test_removed_from_the_team_means_no_session(self):
        code = self.ask()
        get_team().remove("mia@studio.ph", by="owner@gmail.com")
        with self.assertRaisesRegex(email_login.LoginError, "isn't on the Redefine team list"):
            self.check(code)


class RoutesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        use_store(MemoryStore())
        email_login.use_store(email_login.MemoryStore())
        self.codes = []
        sender = mock.patch.object(email_login, "_send", lambda to, code: self.codes.append(code))
        sender.start()
        self.addCleanup(sender.stop)

    def sign_in(self, client, next_path):
        self.assertIn("'on' === 'on'", client.get("/login").text)
        self.assertEqual(client.post("/auth/email/start", json={"email": "owner@gmail.com"}).json(), {"ok": True})
        bad = client.post("/auth/email/verify", json={"email": "owner@gmail.com", "code": "x"})
        self.assertEqual(bad.status_code, 403)
        good = client.post("/auth/email/verify", json={"email": "owner@gmail.com", "code": self.codes[-1], "next": next_path})
        self.assertEqual((good.status_code, good.json()["next"]), (200, next_path))
        self.assertEqual(auth.read_session(good.cookies[auth.COOKIE], auth.config()), "owner@gmail.com")

    def test_redefine_app(self):
        self.sign_in(TestClient(app, follow_redirects=False), "/post-audit")

    def test_care_desk(self):
        self.sign_in(TestClient(create_app(Desk(DeskStore()))), "/desk")

    def test_hidden_and_refused_without_an_email_service(self):
        os.environ.pop("RESEND_API_KEY")
        for client in (TestClient(app, follow_redirects=False), TestClient(create_app(Desk(DeskStore())))):
            self.assertIn("'off' === 'on'", client.get("/login").text)
            self.assertEqual(client.post("/auth/email/start", json={"email": "owner@gmail.com"}).status_code, 503)
        self.assertEqual(self.codes, [])


if __name__ == "__main__":
    unittest.main()
