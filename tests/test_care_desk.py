"""The Care Desk: intake, routing, timers, journeys and the HTTP surface. No network required."""

import base64
import os
import unittest

os.environ["DESK_AUTOSTART"] = "0"  # the module-level app would open Firestore

from fastapi.testclient import TestClient

from care_desk import auth, config
from care_desk.app import create_app
from care_desk.meta import Comment, MetaError
from care_desk.desk import MIN, Desk, DeskError
from care_desk.intake import manual, parse_ghl
from care_desk.sorter import SortError, Sorting, sort_with_rules
from care_desk.store import MemoryStore
from service import auth as team_auth
from service import team


class Clock:
    def __init__(self):
        self.t = 1_800_000_000_000

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * MIN


def sorting(category, priority="Normal", interest=None):
    return Sorting(category=category, priority=priority, interest=interest, confidence=0.9,
                   summary="s", suggested_reply="Thanks!", sorted_by="claude")


class DeskTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = MemoryStore(clock=self.clock)
        self.next_sort = sorting("General question")
        self.sent = []
        self.mail_on = False

        def sort(text, channel, author, subject=None):
            if isinstance(self.next_sort, Exception):
                raise self.next_sort
            return self.next_sort

        def send(to, subject, body, footer, unsubscribe_url=None):
            self.sent.append({"to": to, "subject": subject, "body": body, "unsubscribe": unsubscribe_url})
            return "msg_1"

        self.desk = Desk(self.store, now=self.clock, sort=sort, send=send,
                         mail_ready=lambda: self.mail_on, public_url="https://desk.example")

    def rows(self, coll):
        return sorted(self.store.all(coll), key=lambda d: d["id"])

    def add(self, text="hello", email=None, author="Ana Cruz", channel="facebook"):
        return self.desk.ingest(manual(text, channel, author, email))["id"]

    def make_newlead_live(self):
        for i, step in enumerate(config.JOURNEYS["newlead"]["steps"]):
            body = step["body"].replace("[PORTFOLIO LINK]", "https://redefine.example/work") \
                               .replace("[BOOKING LINK]", "https://redefine.example/call")
            self.desk.save_journey_step("newlead", i, step["subject"], body)
        return self.desk.set_journey_status("newlead", "live", "tester@redefine.ph")


class IntakeTest(unittest.TestCase):
    def test_reads_the_custom_data_the_setup_guide_asks_for(self):
        msg = parse_ghl({
            "contact_id": "c1", "full_name": "Kian Resma", "email": "kian@example.com",
            "location": {"id": "loc9"},
            "customData": {"comment": "wow", "channel": "Facebook", "comment_id": "fb_123", "post_url": "https://fb.com/p/1"},
        })
        self.assertEqual((msg.text, msg.channel, msg.author, msg.external_id), ("wow", "facebook", "Kian Resma", "fb_123"))
        self.assertEqual((msg.location_id, msg.post_url, msg.email), ("loc9", "https://fb.com/p/1", "kian@example.com"))

    def test_finds_text_in_nested_message_and_names_the_channel(self):
        msg = parse_ghl({"first_name": "Nicole", "last_name": "Abrenica",
                         "message": {"body": "#highlight", "type": "Instagram"}})
        self.assertEqual((msg.text, msg.author, msg.channel), ("#highlight", "Nicole Abrenica", "instagram"))

    def test_payload_without_text_is_kept_for_debugging(self):
        msg = parse_ghl({"contact_id": "c2", "customData": {"something": "else"}})
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.raw["customData"], {"something": "else"})


class RulesTest(unittest.TestCase):
    def test_short_reactions_are_engagement(self):
        self.assertEqual(sort_with_rules("wow").category, "Engagement")
        self.assertEqual(sort_with_rules("#highlight").category, "Engagement")

    def test_price_question_is_a_lead_with_an_interest(self):
        s = sort_with_rules("How much for a website for my cafe?")
        self.assertEqual((s.category, s.intent, s.interest), ("Pricing question", "sales", "Website"))

    def test_urgent_issue(self):
        s = sort_with_rules("Our website is down since this morning!")
        self.assertEqual((s.category, s.priority, s.intent), ("Urgent issue", "Urgent", "service"))


class RoutingTest(DeskTestCase):
    def test_concern_becomes_a_ticket_and_logs_the_unsent_email(self):
        self.next_sort = sorting("Client concern", "High")
        self.add("Any update on our project?", email="ana@example.com")
        ticket = self.rows("tickets")[0]
        self.assertEqual((ticket["team"], ticket["priority"], ticket["status"]), ("Client success", "High", "Open"))
        self.assertEqual(self.rows("messages")[0]["route"], "ticket")
        mail = self.rows("emails")[0]
        self.assertEqual((mail["journey"], mail["status"]), ("received", "not_sent"))
        self.assertEqual(self.sent, [])

    def test_concern_email_goes_out_when_the_service_is_on(self):
        self.mail_on = True
        self.next_sort = sorting("Billing question")
        self.add("Where is my receipt?", email="ana@example.com")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("RD-1001", self.sent[0]["subject"])
        self.assertIsNone(self.sent[0]["unsubscribe"])  # a service update, not marketing
        self.assertEqual(self.rows("tickets")[0]["team"], "Finance")

    def test_sales_question_becomes_a_queued_lead_until_the_journey_is_approved(self):
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much for a website?", email="ben@example.com")
        lead = self.rows("leads")[0]
        self.assertEqual((lead["status"], lead["interest"], lead["kind"]), ("queued", "Website", "new"))

    def test_lead_without_email_is_followed_up_by_hand(self):
        self.next_sort = sorting("Service inquiry", interest="Branding")
        self.add("Do you do logos?")
        self.assertEqual(self.rows("leads")[0]["status"], "manual")

    def test_engagement_is_logged_only(self):
        self.next_sort = sorting("Engagement")
        self.add("wow")
        self.assertEqual(self.rows("messages")[0]["route"], "logged")
        self.assertEqual(self.rows("tickets"), [])

    def test_ghl_retries_are_ignored(self):
        payload = {"contact_id": "c1", "customData": {"comment": "hi", "comment_id": "x1"}}
        first = self.desk.ingest(parse_ghl(payload))
        again = self.desk.ingest(parse_ghl(payload))
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["id"], again["id"])
        no_id = {"contact_id": "c2", "customData": {"comment": "same words"}}
        self.desk.ingest(parse_ghl(no_id))
        self.assertTrue(self.desk.ingest(parse_ghl(no_id))["duplicate"])
        self.clock.advance(25 * 60)
        self.assertFalse(self.desk.ingest(parse_ghl(no_id))["duplicate"])

    def test_unreadable_webhook_is_stored_not_sorted(self):
        self.desk.ingest(parse_ghl({"customData": {}}))
        self.assertEqual(self.rows("messages")[0]["sort_status"], "unparsed")

    def test_failed_sort_is_kept_and_retried_by_the_clock(self):
        self.next_sort = SortError("Could not reach the Claude API.")
        self.add("Our site is down")
        msg = self.rows("messages")[0]
        self.assertEqual((msg["sort_status"], msg["sort_attempts"]), ("failed", 1))
        self.next_sort = sorting("Urgent issue", "Urgent")
        self.desk.tick()  # too soon: a sort may still be in flight
        self.assertEqual(self.rows("messages")[0]["sort_status"], "failed")
        self.clock.advance(2)
        self.desk.tick()
        self.assertEqual(self.rows("messages")[0]["sort_status"], "sorted")
        self.assertEqual(self.rows("tickets")[0]["priority"], "Urgent")

    def test_writing_in_again_stops_the_lead_journey(self):
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="ben@example.com")
        self.next_sort = sorting("Service inquiry", interest="Website")
        self.add("Also, can you do the logo?", email="ben@example.com")
        all_leads = self.rows("leads")
        self.assertEqual(len(all_leads), 1)
        self.assertEqual(all_leads[0]["status"], "replied")
        self.assertEqual(self.rows("messages")[1]["lead_id"], all_leads[0]["id"])


class TicketTest(DeskTestCase):
    def setUp(self):
        super().setUp()
        self.mail_on = True
        self.next_sort = sorting("Complaint", "High")
        self.add("Nobody replied for 3 days", email="ana@example.com")
        self.ticket_id = self.rows("tickets")[0]["id"]

    def test_escalates_after_the_first_reply_target(self):
        self.clock.advance(59)
        self.assertEqual(self.desk.tick()["escalated"], 0)
        self.clock.advance(2)
        self.assertEqual(self.desk.tick()["escalated"], 1)
        self.assertEqual(self.desk.tick()["escalated"], 0)  # once only

    def test_reply_progress_resolve(self):
        self.clock.advance(10)
        self.desk.ticket_action(self.ticket_id, "first_reply", "lanz@redefine.ph")
        self.desk.ticket_action(self.ticket_id, "progress", "lanz@redefine.ph")
        self.clock.advance(30)
        self.desk.ticket_action(self.ticket_id, "resolve", "lanz@redefine.ph")
        t = self.rows("tickets")[0]
        self.assertEqual((t["status"], t["owner"]), ("Resolved", "lanz@redefine.ph"))
        self.assertEqual(t["first_reply_at"] - t["created_at"], 10 * MIN)
        self.assertEqual([e["journey"] for e in self.rows("emails")], ["received", "update", "resolved"])
        with self.assertRaises(DeskError):
            self.desk.ticket_action(self.ticket_id, "resolve", "lanz@redefine.ph")

    def test_marking_the_public_reply_counts_as_first_reply(self):
        message_id = self.rows("messages")[0]["id"]
        self.desk.message_action(message_id, "replied", "mia@redefine.ph")
        self.assertIsNotNone(self.rows("tickets")[0]["first_reply_at"])
        self.assertEqual(self.rows("tickets")[0]["owner"], "mia@redefine.ph")

    def test_notes_keep_their_files_whole_across_parts(self):
        big = bytes(range(256)) * 6000  # 1.5 MB: three Firestore-sized parts
        self.desk.add_note(self.ticket_id, "mia@redefine.ph", "  Called her back  ",
                           [("../screens/shot.png", "image/png", big), ("brief.txt", "text/plain", b"hi")])
        note = self.rows("notes")[0]
        self.assertEqual((note["text"], note["by"]), ("Called her back", "mia@redefine.ph"))
        self.assertEqual([f["name"] for f in note["files"]], ["..screensshot.png", "brief.txt"])
        meta, data = self.desk.file(note["files"][0]["id"])
        self.assertEqual((meta["parts"], data), (3, big))
        self.assertEqual(self.rows("events")[-1]["detail"], "By mia@redefine.ph, 2 files attached")
        self.assertEqual(len(self.desk.state()["notes"]), 1)

    def test_notes_only_on_open_tickets_and_within_limits(self):
        with self.assertRaisesRegex(DeskError, "Write a note"):
            self.desk.add_note(self.ticket_id, "mia@redefine.ph", "   ", [])
        with self.assertRaisesRegex(DeskError, "MB per note"):
            self.desk.add_note(self.ticket_id, "mia@redefine.ph", "", [("a.bin", "", b"x" * (10 * 1024 * 1024 + 1))])
        with self.assertRaisesRegex(DeskError, "5 files"):
            self.desk.add_note(self.ticket_id, "mia@redefine.ph", "", [(f"{k}.txt", "", b"x") for k in range(6)])
        self.desk.ticket_action(self.ticket_id, "resolve", "mia@redefine.ph")
        with self.assertRaisesRegex(DeskError, "Reopen"):
            self.desk.add_note(self.ticket_id, "mia@redefine.ph", "late", [])
        self.desk.ticket_action(self.ticket_id, "reopen", "mia@redefine.ph")
        self.desk.add_note(self.ticket_id, "mia@redefine.ph", "back on it", [])
        self.assertEqual(self.store.all("files"), [])


class JourneyTest(DeskTestCase):
    def test_placeholders_block_going_live(self):
        with self.assertRaisesRegex(DeskError, "PORTFOLIO LINK"):
            self.desk.set_journey_status("newlead", "live", "tester@redefine.ph")

    def test_approval_releases_queued_leads_and_the_clock_sends_on_schedule(self):
        self.mail_on = True
        self.next_sort = sorting("Pricing question", interest="Video production")
        self.add("Rates for a product video?", email="cara@example.com")
        self.assertEqual(self.make_newlead_live(), 1)

        self.desk.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("video production", self.sent[0]["subject"])
        self.assertTrue(self.sent[0]["unsubscribe"].startswith("https://desk.example/unsubscribe?t="))

        self.clock.advance(60 * 24)  # day 1: nothing due
        self.desk.tick()
        self.assertEqual(len(self.sent), 1)
        self.clock.advance(60 * 24 + 1)  # day 2
        self.desk.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("https://redefine.example/work", self.sent[1]["body"])
        for _ in range(3):
            self.clock.advance(60 * 24 * 5)
            self.desk.tick()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.rows("leads")[0]["status"], "completed")

    def test_journeys_wait_while_email_is_off(self):
        self.make_newlead_live()
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="dan@example.com")
        self.desk.tick()
        self.assertEqual(self.rows("leads")[0]["step"], 0)
        self.assertEqual(self.rows("emails"), [])

    def test_editing_a_live_journey_back_to_a_placeholder_pulls_it(self):
        self.make_newlead_live()
        self.desk.save_journey_step("newlead", 0, "Hi", "Body with [PRICE LIST]")
        self.assertEqual(self.desk.journey("newlead")["status"], "pending")

    def test_unsubscribe_stops_marketing_but_not_service_email(self):
        self.mail_on = True
        self.make_newlead_live()
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="eve@example.com")
        self.desk.unsubscribe("EVE@example.com")
        self.assertEqual(self.rows("leads")[0]["status"], "unsubscribed")
        self.desk.tick()
        self.assertEqual(self.sent, [])
        self.next_sort = sorting("Client concern")
        self.add("Any update on our project?", email="eve@example.com")
        self.assertEqual(len(self.sent), 1)


class ImportTest(DeskTestCase):
    def test_checks_every_row(self):
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="known@example.com")
        self.desk.unsubscribe("gone@example.com")
        rows = [
            {"name": "Ok Person", "email": "ok@example.com", "interest": "branding", "last_contact_days": "120", "consent": "yes"},
            {"name": "No Consent", "email": "nc@example.com", "consent": "no"},
            {"name": "Known", "email": "known@example.com", "consent": "yes"},
            {"name": "Gone", "email": "gone@example.com", "consent": "yes"},
            {"name": "Recent", "email": "recent@example.com", "last_contact_days": "30", "consent": "yes"},
            {"name": "Bad", "email": "not-an-email", "consent": "yes"},
            {"name": "Ok Twice", "email": "ok@example.com", "consent": "yes"},
        ]
        reasons = [v["reason"] for v in self.desk.check_import(rows)]
        self.assertEqual(reasons, [None, "No email consent", "Already a lead", "Unsubscribed",
                                   "Contacted in the last 90 days", "No valid email", "Already a lead"])
        result = self.desk.import_leads(rows, "tester@redefine.ph")
        self.assertEqual(result, {"enrolled": 1, "skipped": 6})
        old = [l for l in self.rows("leads") if l["kind"] == "old"]
        self.assertEqual((old[0]["interest"], old[0]["status"]), ("Branding", "queued"))


class StateTest(DeskTestCase):
    def test_refresh_brings_only_what_changed(self):
        self.next_sort = sorting("Client concern")
        self.add("Any update?", email="ana@example.com")
        full = self.desk.state()
        self.assertTrue(full["full"])
        self.assertEqual((len(full["messages"]), len(full["tickets"])), (1, 1))
        self.assertEqual(full["tickets"][0]["code"], "RD-1001")

        since = full["now"]
        self.clock.advance(1)
        self.assertEqual([len(self.desk.state(since)[k]) for k in ("messages", "tickets", "events")], [0, 0, 0])

        self.desk.ticket_action(self.rows("tickets")[0]["id"], "assign", "mia@redefine.ph")
        later = self.desk.state(since)
        self.assertFalse(later["full"])
        self.assertEqual((len(later["messages"]), len(later["tickets"]), len(later["events"])), (0, 1, 1))


class TokenTest(unittest.TestCase):
    def test_unsubscribe_tokens_cannot_be_reused_or_forged(self):
        token = auth.sign("a@example.com", "unsubscribe")
        self.assertEqual(auth.unsign(token, "unsubscribe"), "a@example.com")
        self.assertIsNone(auth.unsign(token, "other"))
        self.assertIsNone(auth.unsign(token[:-2] + "xx", "unsubscribe"))


SIGN_IN = {"GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
           "ALLOWED_EMAILS": "owner@redefine.ph", "SESSION_SECRET": "test-secret-" + "x" * 32}


class HTTPTest(DeskTestCase):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in (*SIGN_IN, "DESK_WEBHOOK_KEY", "DESK_DEV_EMAIL")}
        os.environ.update(SIGN_IN, DESK_WEBHOOK_KEY="hook-secret")
        os.environ.pop("DESK_DEV_EMAIL", None)
        team.use_store(team.MemoryStore())
        self.client = TestClient(create_app(self.desk))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        team.use_store(team.MemoryStore())

    def sign_in(self, email="owner@redefine.ph"):
        self.client.cookies.set(team_auth.COOKIE, team_auth.make_session(email, SIGN_IN["SESSION_SECRET"].encode()))

    def test_desk_needs_a_team_sign_in(self):
        self.assertEqual(self.client.get("/api/desk/state").status_code, 401)
        r = self.client.get("/desk", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        login = self.client.get("/login")
        self.assertIn(SIGN_IN["GOOGLE_CLIENT_ID"], login.text)

    def test_the_desk_keeps_its_own_team(self):
        self.sign_in("stranger@gmail.com")
        self.assertEqual(self.client.get("/api/desk/state").status_code, 401)
        # On the app's team only: no desk access, and the reverse.
        team.get_team().add("app-only@redefine.ph", "member", by="owner@redefine.ph")
        self.sign_in("app-only@redefine.ph")
        self.assertEqual(self.client.get("/api/desk/state").status_code, 401)
        self.client.app.state.team.add("agent@redefine.ph", "agent", by="owner@redefine.ph")
        self.sign_in("agent@redefine.ph")
        state = self.client.get("/api/desk/state").json()
        self.assertEqual(state["user"], {"email": "agent@redefine.ph", "role": "agent"})
        self.assertFalse(team.get_team().is_allowed("agent@redefine.ph"))
        self.client.app.state.team.remove("agent@redefine.ph", by="owner@redefine.ph")
        self.assertEqual(self.client.get("/api/desk/state").status_code, 401)

    def test_first_read_copies_the_apps_team_so_nobody_loses_access(self):
        team.get_team().add("lead@redefine.ph", "admin", by="owner@redefine.ph")
        team.get_team().add("helper@redefine.ph", "member", by="owner@redefine.ph")
        desk_team = self.client.app.state.team
        self.assertEqual([(p["email"], p["role"]) for p in desk_team.listing()],
                         [("owner@redefine.ph", "owner"), ("helper@redefine.ph", "agent"), ("lead@redefine.ph", "admin")])
        team.get_team().add("later@redefine.ph", "member", by="owner@redefine.ph")
        self.assertFalse(desk_team.is_allowed("later@redefine.ph"))  # copied once, then separate

    def test_adding_someone_emails_them_an_invite(self):
        from unittest import mock
        from service import email_login
        h = {"X-Requested-With": "desk"}
        self.sign_in("owner@redefine.ph")
        # Email off: they are added, and the admin is told to share the link.
        r = self.client.post("/api/desk/team", json={"email": "a@y.ph", "role": "agent"}, headers=h).json()
        self.assertEqual((r["invited"], "send them the desk link" in r["note"]), (False, True))

        sent = []
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "LOGIN_FROM_EMAIL": "Redefine <login@x.ph>"}), \
                mock.patch.object(email_login, "deliver", lambda *a: sent.append(a)):
            r = self.client.post("/api/desk/team", json={"email": "B@y.ph", "role": "admin"}, headers=h).json()
        self.assertTrue(r["invited"])
        to, subject, text, html = sent[0]
        self.assertEqual((to, subject), ("b@y.ph", "You're invited to the Redefine Care Desk"))
        self.assertIn("owner@redefine.ph added you to Redefine's Care Desk as an admin", text)
        self.assertIn('href="http://testserver/login"', html)
        self.assertIn('src="http://testserver/static/logo.png"', html)

        def fail(*a):
            raise email_login.LoginError("down")
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "LOGIN_FROM_EMAIL": "x@x.ph"}), \
                mock.patch.object(email_login, "deliver", fail):
            r = self.client.post("/api/desk/team", json={"email": "c@y.ph", "role": "agent"}, headers=h).json()
        self.assertEqual((r["invited"], "could not be sent" in r["note"]), (False, True))
        self.assertTrue(self.client.app.state.team.is_allowed("c@y.ph"))  # still added

    def test_invites_stay_pending_until_first_sign_in_and_can_be_resent(self):
        from unittest import mock
        from service import email_login
        h, sent, now = {"X-Requested-With": "desk"}, [], [1_800_000_000.0]
        desk_team = self.client.app.state.team
        desk_team.clock = lambda: now[0]
        person = lambda: next(p for p in desk_team.listing() if p["email"] == "new@y.ph")  # noqa: E731
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "LOGIN_FROM_EMAIL": "x@x.ph"}), \
                mock.patch.object(email_login, "deliver", lambda *a: sent.append(a)):
            self.sign_in("owner@redefine.ph")
            self.client.post("/api/desk/team", json={"email": "new@y.ph", "role": "agent"}, headers=h)
            self.assertEqual((person()["status"], person()["invites"]), ("pending", 1))
            again = self.client.post("/api/desk/team/new@y.ph/invite", headers=h)
            self.assertEqual((again.status_code, "Wait a minute" in again.text), (400, True))
            now[0] += 61
            self.assertEqual(self.client.post("/api/desk/team/new@y.ph/invite", headers=h).status_code, 200)
            self.assertEqual((len(sent), person()["invites"]), (2, 2))
            self.assertEqual(self.client.post("/api/desk/team/owner@redefine.ph/invite", headers=h).status_code, 400)

            self.sign_in("new@y.ph")  # first visit approves the invite
            self.assertEqual(self.client.post("/api/desk/team/new@y.ph/invite", headers=h).status_code, 403)
            self.client.get("/api/desk/state")
            self.assertEqual(person()["status"], "approved")
            self.assertIsNotNone(person()["joined_at"])
            self.sign_in("owner@redefine.ph")
            now[0] += 61
            done = self.client.post("/api/desk/team/new@y.ph/invite", headers=h)
            self.assertEqual((done.status_code, "already uses the desk" in done.text), (400, True))

    def test_only_desk_admins_manage_the_team(self):
        desk_team, h = self.client.app.state.team, {"X-Requested-With": "desk"}
        desk_team.add("boss@redefine.ph", "admin", by="owner@redefine.ph")
        desk_team.add("agent@redefine.ph", "agent", by="owner@redefine.ph")
        self.sign_in("agent@redefine.ph")
        self.assertEqual(self.client.get("/api/desk/team").status_code, 403)
        self.assertEqual(self.client.post("/api/desk/team", json={"email": "x@y.ph", "role": "agent"}, headers=h).status_code, 403)

        self.sign_in("boss@redefine.ph")
        self.assertEqual(self.client.post("/api/desk/team", json={"email": "new@y.ph", "role": "agent"}).status_code, 403)
        r = self.client.post("/api/desk/team", json={"email": " New@Y.ph ", "role": "agent"}, headers=h)
        self.assertIn(("new@y.ph", "agent"), [(p["email"], p["role"]) for p in r.json()["people"]])
        self.assertEqual(self.client.patch("/api/desk/team/new@y.ph", json={"role": "admin"}, headers=h).status_code, 200)
        self.assertTrue(desk_team.is_admin("new@y.ph"))
        for bad in (self.client.patch("/api/desk/team/boss@redefine.ph", json={"role": "agent"}, headers=h),
                    self.client.delete("/api/desk/team/boss@redefine.ph", headers=h),
                    self.client.delete("/api/desk/team/owner@redefine.ph", headers=h),
                    self.client.post("/api/desk/team", json={"email": "new@y.ph", "role": "agent"}, headers=h),
                    self.client.post("/api/desk/team", json={"email": "nope", "role": "agent"}, headers=h),
                    self.client.post("/api/desk/team", json={"email": "a@b.ph", "role": "owner"}, headers=h)):
            self.assertEqual(bad.status_code, 400, bad.text)
        self.assertEqual(self.client.delete("/api/desk/team/new@y.ph", headers=h).status_code, 200)
        self.assertFalse(desk_team.is_allowed("new@y.ph"))
        self.sign_in("owner@redefine.ph")  # owners are always desk admins
        self.assertEqual(self.client.get("/api/desk/state").json()["user"]["role"], "owner")
        self.assertEqual(self.client.get("/api/desk/team").status_code, 200)

    def test_never_runs_open_without_sign_in(self):
        for name in SIGN_IN:
            os.environ.pop(name)
        self.assertEqual(self.client.get("/api/desk/state").status_code, 503)
        self.assertEqual(self.client.get("/desk").status_code, 503)
        os.environ["DESK_DEV_EMAIL"] = "dev@redefine.ph"  # laptop runs only
        self.assertEqual(self.client.get("/api/desk/state").json()["user"]["email"], "dev@redefine.ph")

    def test_signed_in_staff_see_state_and_need_the_header_to_change_things(self):
        self.sign_in()
        state = self.client.get("/api/desk/state").json()
        self.assertEqual(state["user"]["email"], "owner@redefine.ph")
        self.assertEqual(self.client.post("/api/desk/messages", json={"text": "hi"}).status_code, 403)
        r = self.client.post("/api/desk/messages", json={"text": "hi", "channel": "email"},
                             headers={"X-Requested-With": "desk"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/desk/tickets/999/resolve", headers={"X-Requested-With": "desk"}).status_code, 404)

    def test_notes_and_files_over_http(self):
        self.next_sort = sorting("Complaint", "High")
        self.add("Still broken")
        ticket_id = self.rows("tickets")[0]["id"]
        desk = {"X-Requested-With": "desk"}
        page = {"text": "See the screenshot", "files": [
            {"name": "shot.png", "type": "image/png", "data": base64.b64encode(b"\x89PNG data").decode()},
            {"name": "page.html", "type": "text/html", "data": base64.b64encode(b"<script>x</script>").decode()}]}
        self.assertEqual(self.client.post(f"/api/desk/tickets/{ticket_id}/notes", json=page, headers=desk).status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.post(f"/api/desk/tickets/{ticket_id}/notes", json=page).status_code, 403)
        bad = {"files": [{"name": "x.png", "data": "not base64!"}]}
        self.assertEqual(self.client.post(f"/api/desk/tickets/{ticket_id}/notes", json=bad, headers=desk).status_code, 400)
        self.assertEqual(self.client.post(f"/api/desk/tickets/{ticket_id}/notes", json=page, headers=desk).status_code, 200)

        files = self.client.get("/api/desk/state").json()["notes"][0]["files"]
        image = self.client.get(f"/api/desk/files/{files[0]['id']}")
        self.assertEqual((image.content, image.headers["content-type"]), (b"\x89PNG data", "image/png"))
        self.assertTrue(image.headers["content-disposition"].startswith("inline"))
        html_file = self.client.get(f"/api/desk/files/{files[1]['id']}")
        self.assertEqual(html_file.headers["content-type"], "application/octet-stream")
        self.assertTrue(html_file.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(html_file.headers["content-security-policy"], "sandbox")
        self.assertEqual(self.client.get("/api/desk/files/999").status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/desk/files/{files[0]['id']}").status_code, 401)

    def test_webhook_needs_its_key(self):
        body = {"full_name": "Kian", "customData": {"comment": "wow", "channel": "facebook", "comment_id": "c9"}}
        self.assertEqual(self.client.post("/hooks/ghl", json=body).status_code, 401)
        self.assertEqual(self.client.post("/hooks/ghl?key=wrong", json=body).status_code, 401)
        r = self.client.post("/hooks/ghl?key=hook-secret", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["duplicate"])
        self.assertTrue(self.client.post("/hooks/ghl?key=hook-secret", json=body).json()["duplicate"])

    def test_serves_the_redefine_logo(self):
        r = self.client.get("/static/favicon.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("#FC4452", r.text)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 200)

    def test_unsubscribe_link(self):
        token = auth.sign("x@example.com", "unsubscribe")
        self.assertEqual(self.client.get(f"/unsubscribe?t={token}").status_code, 200)
        self.assertEqual(self.client.get("/unsubscribe?t=forged.token").status_code, 400)


class FakeMeta:
    """Stands in for Meta's Graph API: the Page's recent comments, and replies posted to them."""

    def __init__(self):
        self.on = True
        self.comments = []
        self.replies = []
        self.fail = None

    def configured(self):
        return self.on

    def recent_comments(self):
        if self.fail:
            raise self.fail
        return list(self.comments)

    def reply(self, comment_id, text):
        if self.fail:
            raise self.fail
        self.replies.append((comment_id, text))
        return f"reply_{len(self.replies)}"


class FacebookCase(DeskTestCase):
    def setUp(self):
        super().setUp()
        self.meta = FakeMeta()
        self.desk.meta = self.meta

    def comment(self, cid, text, minutes_ago=1, author=None):
        self.meta.comments.append(Comment(id=cid, message=text, created_at=self.clock() - minutes_ago * MIN,
                                          author=author, post_url="https://fb.example/post",
                                          comment_url=f"https://fb.example/c/{cid}"))

    def msg(self, message_id):
        return self.store.get("messages", message_id)


class FacebookTest(FacebookCase):
    def test_finds_the_comment_by_text_and_time(self):
        self.comment("c1", "Lezzzgo !")
        m = self.msg(self.add("lezzzgo   !", author="Nilo Urmeneta"))
        self.assertEqual((m["fb_match"], m["fb_comment_id"]), ("matched", "c1"))
        self.assertEqual(m["post_url"], "https://fb.example/c/c1")  # "Open on Facebook" goes to the comment

    def test_a_comment_from_someone_else_is_not_taken(self):
        self.comment("c1", "Nice one", author="Someone Else")
        self.comment("c2", "Nice one")  # name hidden by Meta: still a candidate
        self.assertEqual(self.msg(self.add("Nice one", author="Ana Cruz"))["fb_comment_id"], "c2")

    def test_the_clock_keeps_looking_then_gives_up(self):
        self.comment("old", "wow", minutes_ago=40)  # too old to be this message
        mid = self.add("wow")
        self.assertEqual(self.msg(mid)["fb_match"], "pending")
        for _ in range(6):
            self.clock.advance(1)
            self.desk.tick()
        self.assertEqual((self.msg(mid)["fb_match"], self.msg(mid)["match_at"]), ("none", None))

    def test_a_comment_that_shows_up_late_is_matched_by_the_clock(self):
        mid = self.add("Any update?")
        self.assertEqual(self.msg(mid)["fb_match"], "pending")
        self.comment("c9", "Any update?", minutes_ago=0)
        self.clock.advance(1)
        self.assertEqual(self.desk.tick()["matched"], 1)
        self.assertEqual(self.msg(mid)["fb_comment_id"], "c9")

    def test_identical_comments_are_picked_by_hand_and_never_reused(self):
        self.comment("h1", "❤️", minutes_ago=2)
        self.comment("h2", "❤️", minutes_ago=1)
        first = self.add("❤️", author="Ana Cruz")
        self.assertEqual(self.msg(first)["fb_match"], "ambiguous")
        self.assertEqual({c["id"] for c in self.msg(first)["fb_candidates"]}, {"h1", "h2"})
        with self.assertRaises(DeskError):
            self.desk.pick_comment(first, "not-a-candidate")
        self.desk.pick_comment(first, "h2")
        self.assertEqual(self.msg(first)["fb_comment_id"], "h2")
        second = self.add("❤️", author="Ben Lim")
        self.assertEqual(self.msg(second)["fb_comment_id"], "h1")  # h2 is taken

    def test_sending_a_reply_posts_once_and_counts_as_the_first_reply(self):
        self.next_sort = sorting("Complaint", "High")
        self.comment("c1", "Nobody replied for 3 days")
        mid = self.add("Nobody replied for 3 days")
        self.desk.send_reply(mid, "  Sorry about that, we just sent you a message.  ", "mia@redefine.ph")
        self.assertEqual(self.meta.replies, [("c1", "Sorry about that, we just sent you a message.")])
        m = self.msg(mid)
        self.assertEqual((m["replied_by"], m["fb_reply_id"]), ("mia@redefine.ph", "reply_1"))
        ticket = self.rows("tickets")[0]
        self.assertIsNotNone(ticket["first_reply_at"])
        self.assertIn("Replied on Facebook", [e["label"] for e in self.rows("events")])
        with self.assertRaisesRegex(DeskError, "already"):
            self.desk.send_reply(mid, "Again", "mia@redefine.ph")
        self.assertEqual(len(self.meta.replies), 1)

    def test_a_reply_needs_a_match_and_text_and_survives_facebook_errors(self):
        mid = self.add("Hello?")
        with self.assertRaisesRegex(DeskError, "isn't matched"):
            self.desk.send_reply(mid, "Hi!", "mia@redefine.ph")
        self.comment("c1", "Hello?", minutes_ago=0)
        self.desk.message_action(mid, "rematch", "mia@redefine.ph")
        with self.assertRaisesRegex(DeskError, "Write the reply"):
            self.desk.send_reply(mid, "   ", "mia@redefine.ph")
        self.meta.fail = MetaError("The Facebook token stopped working.")
        with self.assertRaisesRegex(DeskError, "token stopped working"):
            self.desk.send_reply(mid, "Hi!", "mia@redefine.ph")
        self.assertIsNone(self.msg(mid)["replied_at"])  # nothing recorded when Facebook refused
        self.meta.fail = RuntimeError("socket closed")
        with self.assertRaisesRegex(DeskError, "in case the reply went through"):
            self.desk.send_reply(mid, "Hi!", "mia@redefine.ph")

    def test_facebook_trouble_while_matching_is_retried(self):
        self.meta.fail = MetaError("Could not reach Facebook.")
        mid = self.add("Hi there")
        self.assertEqual((self.msg(mid)["fb_match"], self.msg(mid)["match_error"]), ("pending", "Could not reach Facebook."))
        self.meta.fail = None
        self.comment("c1", "Hi there", minutes_ago=0)
        self.clock.advance(1)
        self.desk.tick()
        self.assertEqual(self.msg(mid)["fb_match"], "matched")

    def test_only_facebook_comments_and_only_when_set_up(self):
        self.assertIsNone(self.msg(self.add("Hi", channel="instagram"))["fb_match"])
        self.meta.on = False
        self.comment("c1", "Hi")
        self.assertIsNone(self.msg(self.add("Hi"))["fb_match"])
        self.assertFalse(self.desk.state()["status"]["facebook_replies"])


class MetaClientTest(unittest.TestCase):
    """The Graph API client against canned responses shaped like Redefine Ph's real feed."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("META_PAGE_TOKEN", "META_PAGE_ID")}
        os.environ["META_PAGE_TOKEN"] = "test-token"
        os.environ.pop("META_PAGE_ID", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def respond(self, status, body):
        from unittest import mock
        response = mock.Mock(status_code=status)
        response.json.return_value = body
        return mock.patch("care_desk.meta.requests.request", return_value=response)

    def test_reads_comments_with_and_without_names(self):
        from care_desk import meta
        feed = {"data": [{"id": "1316682498188449_122119669659430233",
                          "permalink_url": "https://www.facebook.com/x/posts/122119669659430233",
                          "comments": {"data": [
                              {"id": "122119669659430233_1733430087734015", "message": "True!",
                               "from": {"name": "Nilo Amor Urmeneta Jr.", "id": "1"},
                               "created_time": "2026-10-16T19:28:24+0000"},
                              {"id": "122119669659430233_2", "message": "wow", "created_time": "2026-10-15T18:19:50+0000"},
                              {"id": "122119669659430233_3", "created_time": "2026-10-15T18:20:00+0000"}]}},
                         {"id": "post_without_comments", "permalink_url": "https://www.facebook.com/x/2"}]}
        with self.respond(200, feed) as call:
            found = meta.recent_comments()
        self.assertEqual(call.call_args.args[1], "https://graph.facebook.com/v26.0/1316682498188449/feed")
        self.assertEqual([(c.id, c.author) for c in found],
                         [("122119669659430233_1733430087734015", "Nilo Amor Urmeneta Jr."), ("122119669659430233_2", None)])
        self.assertEqual(found[0].created_at, 1792178904000)
        self.assertEqual(found[0].post_url, "https://www.facebook.com/x/posts/122119669659430233")

    def test_errors_become_plain_messages(self):
        from care_desk import meta
        for code, words in ((190, "token stopped working"), (200, "missing a permission"), (4, "rate limiting"),
                            (100, "could not find that comment")):
            with self.subTest(code=code), self.respond(400, {"error": {"code": code, "message": "x"}}):
                with self.assertRaisesRegex(MetaError, words):
                    meta.reply("c1", "Hi")
        with self.respond(200, {"id": "r1"}):
            self.assertEqual(meta.reply("c1", "Hi"), "r1")


class FacebookHTTPTest(FacebookCase):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in SIGN_IN}
        os.environ.update(SIGN_IN)
        team.use_store(team.MemoryStore())
        self.client = TestClient(create_app(self.desk))
        self.client.cookies.set(team_auth.COOKIE, team_auth.make_session("owner@redefine.ph", SIGN_IN["SESSION_SECRET"].encode()))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_send_and_pick_routes(self):
        self.comment("c1", "Price?")
        mid = self.add("Price?")
        self.assertEqual(self.client.post(f"/api/desk/messages/{mid}/send", json={"text": "DM us!"}).status_code, 403)
        r = self.client.post(f"/api/desk/messages/{mid}/send", json={"text": "DM us!"}, headers={"X-Requested-With": "desk"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.meta.replies, [("c1", "DM us!")])
        again = self.client.post(f"/api/desk/messages/{mid}/send", json={"text": "DM us!"}, headers={"X-Requested-With": "desk"})
        self.assertEqual(again.status_code, 400)
        bad_pick = self.client.post(f"/api/desk/messages/{mid}/pick", json={"comment_id": "zzz"}, headers={"X-Requested-With": "desk"})
        self.assertEqual(bad_pick.status_code, 400)


if __name__ == "__main__":
    unittest.main()
