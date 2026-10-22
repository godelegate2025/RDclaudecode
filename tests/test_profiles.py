"""Names: taken from Google, asked for once otherwise, and shown in both apps. No network."""

import os
import unittest
from unittest import mock

os.environ.setdefault("DESK_AUTOSTART", "0")

from fastapi.testclient import TestClient

from care_desk.app import create_app
from care_desk.desk import Desk
from care_desk.store import MemoryStore as DeskStore
from service import auth, profiles
from service.app import app
from service.firestore_db import StoreUnavailable
from service.team import MemoryStore, use_store

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "owner@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
}


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        use_store(MemoryStore())
        self.names = profiles.use_store(profiles.MemoryStore())
        self.addCleanup(profiles.use_store, profiles.MemoryStore())

    def session(self, client, email="owner@gmail.com"):
        client.cookies.set(auth.COOKIE, auth.make_session(email, SETTINGS["SESSION_SECRET"].encode()))
        return client


class NameRulesTest(Base):
    def test_google_fills_the_name_but_never_replaces_a_typed_one(self):
        self.names.from_google("owner@gmail.com", "Nilo Urmeneta")
        self.assertEqual(self.names.name_of("owner@gmail.com"), "Nilo Urmeneta")
        self.names.from_google("owner@gmail.com", "Nilo U.")  # renamed on Google: follow it
        self.assertEqual(self.names.name_of("owner@gmail.com"), "Nilo U.")
        self.names.set_name("owner@gmail.com", "  Nilo   ")
        self.names.from_google("owner@gmail.com", "Someone Else")
        self.assertEqual(self.names.name_of("owner@gmail.com"), "Nilo")

    def test_a_name_saved_through_another_server_is_seen_at_once(self):
        self.assertTrue(self.names.needs_name("owner@gmail.com"))  # caches "no names yet"
        self.names.store.put("owner@gmail.com", {"email": "owner@gmail.com", "name": "Nilo", "source": "typed"})
        self.assertFalse(self.names.needs_name("owner@gmail.com"))
        self.assertEqual(self.names.name_of("owner@gmail.com"), "Nilo")

    def test_bad_names_are_refused(self):
        for bad in ("", "   ", "me@gmail.com", "x" * 81):
            with self.assertRaises(profiles.NameError_):
                self.names.set_name("owner@gmail.com", bad)

    def test_nobody_is_asked_while_names_cannot_be_saved(self):
        class Down(profiles.MemoryStore):
            def all(self):
                raise StoreUnavailable("down")
        names = profiles.use_store(Down())
        self.assertFalse(names.needs_name("owner@gmail.com"))


class RedefineAppTest(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, follow_redirects=False)

    def test_google_sign_in_saves_the_google_name(self):
        claims = {"email": "owner@gmail.com", "email_verified": True, "name": "Nilo Urmeneta"}
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
            self.assertEqual(self.client.post("/auth/google", json={"credential": "t"}).status_code, 200)
        self.assertEqual(self.client.get("/post-audit").status_code, 200)  # no name step
        self.assertEqual(self.client.get("/auth/me").json()["name"], "Nilo Urmeneta")

    def test_without_a_name_the_first_page_asks_for_one(self):
        self.session(self.client)
        page = self.client.get("/post-audit?x=1")
        self.assertEqual((page.status_code, page.headers["location"]), (303, "/welcome?next=/post-audit%3Fx%3D1"))
        self.assertEqual(self.client.get("/api/history").status_code != 303, True)  # APIs are never redirected
        welcome = self.client.get("/welcome")
        self.assertIn("'off' === 'on'", welcome.text)  # first time, not editing
        self.assertIn("owner@gmail.com", welcome.text)

        self.assertEqual(self.client.post("/auth/name", json={"name": "Nilo"}).status_code, 403)  # header needed
        bad = self.client.post("/auth/name", json={"name": ""}, headers={"X-Requested-With": "redefine"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/auth/name", json={"name": "Nilo <b>"}, headers={"X-Requested-With": "redefine"})
        self.assertEqual(ok.json(), {"ok": True, "name": "Nilo <b>"})
        self.assertEqual(self.client.get("/post-audit").status_code, 200)
        edit = self.client.get("/welcome").text
        self.assertIn('value="Nilo &lt;b&gt;"', edit)  # escaped
        self.assertIn("'on' === 'on'", edit)

    def test_signed_out_people_cannot_set_a_name(self):
        r = self.client.post("/auth/name", json={"name": "X"}, headers={"X-Requested-With": "redefine"})
        self.assertEqual(r.status_code, 401)

    def test_team_page_lists_names(self):
        self.names.set_name("owner@gmail.com", "Nilo")
        self.session(self.client)
        member = self.client.get("/api/team").json()["members"][0]
        self.assertEqual((member["email"], member["name"]), ("owner@gmail.com", "Nilo"))


class CareDeskTest(Base):
    def setUp(self):
        super().setUp()
        self.desk_app = create_app(Desk(DeskStore()))
        self.client = TestClient(self.desk_app, follow_redirects=False)

    def test_first_visit_asks_then_the_desk_shows_names(self):
        h = {"X-Requested-With": "desk"}
        self.desk_app.state.team.add("mia@studio.ph", "agent", by="owner@gmail.com")
        self.names.set_name("mia@studio.ph", "Mia Cruz")
        self.names.set_name("outsider@gmail.com", "Not On The Desk")
        self.session(self.client)
        self.assertEqual(self.client.get("/desk").headers["location"], "/welcome")
        self.assertEqual(self.client.post("/auth/name", json={"name": "Nilo"}).status_code, 403)
        self.assertEqual(self.client.post("/auth/name", json={"name": "Nilo"}, headers=h).status_code, 200)
        self.assertEqual(self.client.get("/desk").status_code, 200)

        state = self.client.get("/api/desk/state").json()
        self.assertEqual(state["user"]["name"], "Nilo")
        self.assertEqual(state["people"], {"mia@studio.ph": "Mia Cruz", "owner@gmail.com": "Nilo"})
        people = {p["email"]: p["name"] for p in self.client.get("/api/desk/team").json()["people"]}
        self.assertEqual(people, {"owner@gmail.com": "Nilo", "mia@studio.ph": "Mia Cruz"})


if __name__ == "__main__":
    unittest.main()
