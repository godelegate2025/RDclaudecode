"""The team admin page: owners, members, admins, and the Firestore fallback. No network."""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from service import auth, team
from service.app import app
from service.team import MemoryStore, StoreUnavailable, TeamError

SETTINGS = {
    "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
    "ALLOWED_EMAILS": "owner@gmail.com",
    "SESSION_SECRET": "a-long-random-test-secret",
}


class BrokenStore:
    def all(self):
        raise StoreUnavailable("Firestore is not set up for this project yet.")

    put = delete = lambda self, *a: (_ for _ in ()).throw(StoreUnavailable("Firestore is not set up for this project yet."))


def client_for(email):
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.make_session(email, SETTINGS["SESSION_SECRET"].encode()))
    return client


class TeamRulesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.team = team.use_store(MemoryStore())

    def test_roles(self):
        self.team.add("Ana@Gmail.com", "admin", by="owner@gmail.com")
        self.team.add("ben@gmail.com", "member", by="owner@gmail.com")
        self.assertEqual(self.team.role_of("owner@gmail.com"), "owner")
        self.assertEqual(self.team.role_of("ana@gmail.com"), "admin")
        self.assertEqual(self.team.role_of("BEN@gmail.com"), "member")
        self.assertIsNone(self.team.role_of("stranger@gmail.com"))
        self.assertTrue(self.team.can_manage("ana@gmail.com"))
        self.assertFalse(self.team.can_manage("ben@gmail.com"))

    def test_guard_rails(self):
        for email in ("", "not-an-email", "a@b", "two@@gmail.com"):
            with self.assertRaises(TeamError, msg=email):
                self.team.add(email, "member", by="owner@gmail.com")
        with self.assertRaisesRegex(TeamError, "member or admin"):
            self.team.add("x@gmail.com", "owner", by="owner@gmail.com")
        with self.assertRaisesRegex(TeamError, "already an owner"):
            self.team.add("owner@gmail.com", "member", by="owner@gmail.com")
        with self.assertRaisesRegex(TeamError, "can't be removed"):
            self.team.remove("owner@gmail.com", by="ana@gmail.com")
        self.team.add("ana@gmail.com", "admin", by="owner@gmail.com")
        with self.assertRaisesRegex(TeamError, "remove yourself"):
            self.team.remove("ana@gmail.com", by="ana@gmail.com")

    def test_firestore_down_keeps_owners_working(self):
        broken = team.use_store(BrokenStore())
        self.assertEqual(broken.role_of("owner@gmail.com"), "owner")
        self.assertIsNone(broken.role_of("ben@gmail.com"))
        with self.assertRaises(StoreUnavailable):
            broken.add("ben@gmail.com", "member", by="owner@gmail.com")


class TeamPageTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.team = team.use_store(MemoryStore())

    def test_owner_adds_a_member_who_can_then_sign_in(self):
        owner = client_for("owner@gmail.com")
        self.assertEqual(owner.get("/admin/team").status_code, 200)
        response = owner.post("/api/team", json={"email": "Ben@Gmail.com", "role": "member"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "ben@gmail.com")
        listing = owner.get("/api/team").json()
        self.assertTrue(listing["store_ready"])
        self.assertEqual([(m["email"], m["role"]) for m in listing["members"]],
                         [("owner@gmail.com", "owner"), ("ben@gmail.com", "member")])
        self.assertEqual(listing["members"][1]["added_by"], "owner@gmail.com")
        # Ben's session is now valid, and Google sign-in accepts him.
        self.assertEqual(client_for("ben@gmail.com").get("/post-audit").status_code, 200)
        with mock.patch("google.oauth2.id_token.verify_oauth2_token",
                        return_value={"email": "ben@gmail.com", "email_verified": True}):
            self.assertEqual(TestClient(app).post("/auth/google", json={"credential": "t"}).status_code, 200)

    def test_removal_signs_them_out(self):
        owner = client_for("owner@gmail.com")
        owner.post("/api/team", json={"email": "ben@gmail.com", "role": "member"})
        ben = client_for("ben@gmail.com")
        self.assertEqual(ben.get("/post-audit").status_code, 200)
        self.assertEqual(owner.delete("/api/team/ben@gmail.com").status_code, 200)
        self.assertEqual(ben.get("/post-audit").status_code, 303)

    def test_members_cannot_manage_but_admins_can(self):
        owner = client_for("owner@gmail.com")
        owner.post("/api/team", json={"email": "ben@gmail.com", "role": "member"})
        owner.post("/api/team", json={"email": "ana@gmail.com", "role": "admin"})
        ben = client_for("ben@gmail.com")
        self.assertEqual(ben.get("/admin/team").status_code, 403)
        self.assertEqual(ben.get("/api/team").status_code, 403)
        self.assertEqual(ben.post("/api/team", json={"email": "x@gmail.com"}).status_code, 403)
        self.assertEqual(ben.delete("/api/team/ana@gmail.com").status_code, 403)
        self.assertFalse(ben.get("/auth/me").json()["can_manage_team"])
        ana = client_for("ana@gmail.com")
        self.assertTrue(ana.get("/auth/me").json()["can_manage_team"])
        self.assertEqual(ana.post("/api/team", json={"email": "cy@gmail.com", "role": "member"}).status_code, 200)
        # Admins add members only; making admins is for owners.
        refused = ana.post("/api/team", json={"email": "dee@gmail.com", "role": "admin"})
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Only owners can add admins", refused.json()["detail"])
        self.assertIsNone(self.team.role_of("dee@gmail.com"))
        # Admins remove members, but not other admins; owners can.
        owner.post("/api/team", json={"email": "eve@gmail.com", "role": "admin"})
        refused = ana.delete("/api/team/eve@gmail.com")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Only owners can remove admins", refused.json()["detail"])
        self.assertEqual(self.team.role_of("eve@gmail.com"), "admin")
        self.assertEqual(ana.delete("/api/team/cy@gmail.com").status_code, 200)
        self.assertEqual(owner.delete("/api/team/eve@gmail.com").status_code, 200)
        self.assertIsNone(self.team.role_of("eve@gmail.com"))
        self.assertEqual(ana.delete("/api/team/owner@gmail.com").status_code, 400)

    def test_owner_switches_roles_both_ways(self):
        owner = client_for("owner@gmail.com")
        owner.post("/api/team", json={"email": "ben@gmail.com", "role": "member"})
        self.assertEqual(owner.get("/api/team").json()["me_role"], "owner")

        promoted = owner.patch("/api/team/Ben@Gmail.com", json={"role": "admin"})
        self.assertEqual((promoted.status_code, promoted.json()["role"]), (200, "admin"))
        self.assertTrue(client_for("ben@gmail.com").get("/auth/me").json()["can_manage_team"])
        stored = self.team.store.all()["ben@gmail.com"]
        self.assertEqual((stored["added_by"], stored["role_changed_by"]), ("owner@gmail.com", "owner@gmail.com"))

        demoted = owner.patch("/api/team/ben@gmail.com", json={"role": "member"})
        self.assertEqual(demoted.json()["role"], "member")
        self.assertEqual(client_for("ben@gmail.com").get("/admin/team").status_code, 403)

    def test_only_owners_change_roles_and_only_real_members(self):
        owner = client_for("owner@gmail.com")
        owner.post("/api/team", json={"email": "ana@gmail.com", "role": "admin"})
        owner.post("/api/team", json={"email": "ben@gmail.com", "role": "member"})
        ana = client_for("ana@gmail.com")
        self.assertEqual(ana.get("/api/team").json()["me_role"], "admin")
        response = ana.patch("/api/team/ben@gmail.com", json={"role": "admin"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Only owners", response.json()["detail"])
        self.assertEqual(client_for("ben@gmail.com").patch("/api/team/ben@gmail.com", json={"role": "admin"}).status_code, 403)
        self.assertEqual(owner.patch("/api/team/owner@gmail.com", json={"role": "member"}).status_code, 400)
        self.assertEqual(owner.patch("/api/team/nobody@gmail.com", json={"role": "admin"}).status_code, 400)
        self.assertEqual(owner.patch("/api/team/ben@gmail.com", json={"role": "owner"}).status_code, 400)
        self.assertEqual(TestClient(app).patch("/api/team/ben@gmail.com", json={"role": "admin"}).status_code, 401)

    def test_signed_out_visitors_get_nothing(self):
        anon = TestClient(app, follow_redirects=False)
        self.assertEqual(anon.get("/admin/team").status_code, 303)
        self.assertEqual(anon.get("/api/team").status_code, 401)
        self.assertEqual(anon.post("/api/team", json={"email": "me@gmail.com"}).status_code, 401)

    def test_firestore_not_set_up_is_explained(self):
        team.use_store(BrokenStore())
        owner = client_for("owner@gmail.com")
        listing = owner.get("/api/team").json()
        self.assertFalse(listing["store_ready"])
        self.assertIn("Firestore", listing["problem"])
        self.assertEqual([m["email"] for m in listing["members"]], ["owner@gmail.com"])
        response = owner.post("/api/team", json={"email": "ben@gmail.com", "role": "member"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
