"""The Care Desk: intake, routing, timers, journeys and the HTTP surface. No network required."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["DESK_AUTOSTART"] = "0"  # the module-level app would open Firestore

from fastapi.testclient import TestClient

from care_desk import auth, config
from care_desk.app import create_app
from care_desk.meta import Comment, MetaError
from care_desk.desk import MIN, Desk, DeskError, NotFound
from care_desk.intake import manual, new_text_of, parse_email, parse_ghl
from care_desk.sorter import SortError, Sorting, sort_with_rules
from care_desk.store import MemoryStore, SupabaseStore
from service import auth as team_auth
from service import team


# With a Postgres URL (any empty database will do), every desk test also runs on
# the Supabase store, in a throwaway schema that is dropped before each test.
PG_URL = os.environ.get("DESK_TEST_PG_URL", "").strip()
PG_SCHEMA = "care_desk_test"


def make_store(clock):
    if not PG_URL:
        return MemoryStore(clock=clock)
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"drop schema if exists {PG_SCHEMA} cascade")
    return SupabaseStore(PG_URL, clock=clock, schema=PG_SCHEMA)


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
        self.store = make_store(self.clock)
        if PG_URL:
            self.addCleanup(self.store.close)
        self.next_sort = sorting("General question")
        self.sent = []
        self.mail_on = False

        def sort(text, channel, author, subject=None):
            if isinstance(self.next_sort, Exception):
                raise self.next_sort
            return self.next_sort

        def send(to, subject, body, footer, unsubscribe_url=None, headers=None):
            self.sent.append({"to": to, "subject": subject, "body": body, "unsubscribe": unsubscribe_url,
                              "headers": headers})
            return "msg_1"

        self.desk = Desk(self.store, now=self.clock, sort=sort, send=send,
                         mail_ready=lambda: self.mail_on, public_url="https://desk.example",
                         pause=lambda seconds: None)

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


class EmailIntakeTest(unittest.TestCase):
    def test_keeps_only_what_the_sender_just_wrote(self):
        gmail = ("How much is a website?\n\nThanks,\nAna\n\n"
                 "On Mon, Oct 26, 2026 at 9:00 AM Redefine <info@redefineph.com> wrote:\n> Hi Ana,\n> old text")
        self.assertEqual(new_text_of(gmail), "How much is a website?\n\nThanks,\nAna")
        outlook = "Yes please send rates\r\n\r\n-----Original Message-----\r\nFrom: Redefine\r\nSent: Monday"
        self.assertEqual(new_text_of(outlook), "Yes please send rates")
        self.assertEqual(new_text_of("Call me tomorrow\n\nSent from my iPhone"), "Call me tomorrow")
        self.assertEqual(new_text_of("> only a quote"), "> only a quote")  # never empty

    def test_reads_the_fields_n8n_sends(self):
        msg = parse_email({"from_email": "Ana@Example.com", "from_name": '"Ana Cruz"', "subject": "Website",
                           "html": "<p>Hello&nbsp;there</p><style>x{}</style><p>Rates?</p>", "message_id": "<abc@mail>"})
        self.assertEqual((msg.channel, msg.source, msg.email, msg.author, msg.subject, msg.external_id),
                         ("email", "email", "ana@example.com", "Ana Cruz", "Website", "email:abc@mail"))
        self.assertIn("Rates?", msg.text)
        self.assertNotIn("<p>", msg.text)

    def test_ignores_bounces_no_reply_and_our_own_domain(self):
        for sender in ("MAILER-DAEMON@mx.example.com", "no-reply@shop.com", "noreply@x.com", "postmaster@x.com"):
            self.assertIsNone(parse_email({"from_email": sender, "text": "hi"}), sender)
        self.assertIsNone(parse_email({"from_email": "info@redefineph.com", "text": "hi"}, ("redefineph.com",)))
        self.assertIsNone(parse_email({"text": "no sender"}))


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

    def test_keeps_the_contacts_phone_for_texting_later(self):
        self.assertEqual(parse_ghl({"phone": " +1 678 541 1468 ", "customData": {"comment": "hi"}}).phone, "+1 678 541 1468")
        self.assertIsNone(parse_ghl({"customData": {"comment": "hi"}}).phone)

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


class EveryoneIsAContactTest(DeskTestCase):
    """Everyone who writes in is saved as a contact, spam aside, once: duplicates are filed under one."""

    def ghl(self, text, contact_id=None, name="", email=""):
        payload = {"customData": {"comment": text, "author": name}, "email": email}
        if contact_id:
            payload["contact_id"] = contact_id
        return self.desk.ingest(parse_ghl(payload))["id"]

    def test_a_complaint_saves_a_contact_with_no_sales_journey(self):
        self.make_newlead_live()
        self.mail_on = True
        self.next_sort = sorting("Complaint", "High")
        self.add("Nobody answered my emails!", email="Ana@Example.com")
        lead, msg = self.rows("leads")[0], self.rows("messages")[0]
        self.assertEqual((lead["kind"], lead["status"], lead["interest"], lead["email"], lead["next_at"]),
                         ("general", "saved", "Complaint", "ana@example.com", None))
        self.assertEqual((msg["route"], msg["lead_id"]), ("ticket", lead["id"]))  # still a ticket too
        self.clock.advance(60 * 24 * 30)
        self.desk.tick()
        self.assertEqual([m["subject"] for m in self.sent], ["We got your message · RD-1001"])  # no sales emails

    def test_engagement_and_job_inquiries_are_contacts_but_spam_is_not(self):
        for category, name in (("Engagement", "Joy Uy"), ("Job inquiry", "Rico Go"), ("Spam", "Win A Prize")):
            self.next_sort = sorting(category)
            self.add("hi", author=name)
        self.assertEqual([(l["name"], l["interest"]) for l in self.rows("leads")],
                         [("Joy Uy", "Engagement"), ("Rico Go", "Job inquiry")])
        self.assertIsNone(self.rows("messages")[2]["lead_id"])

    def test_the_same_email_is_the_same_person(self):
        self.next_sort = sorting("Engagement")
        self.add("love it", email="ana@example.com", author="Ana")
        self.next_sort = sorting("Billing question")
        self.add("where is my receipt?", email="ANA@example.com", author="Ana Cruz")
        leads, msgs = self.rows("leads"), self.rows("messages")
        self.assertEqual(len(leads), 1)
        self.assertEqual([m["lead_id"] for m in msgs], [leads[0]["id"]] * 2)
        events = [e["label"] for e in self.rows("events") if e["lead_id"] == leads[0]["id"]]
        self.assertEqual(events, ["Contact saved", "Wrote in again"])

    def test_the_same_ghl_contact_is_the_same_person_and_fills_in_what_was_missing(self):
        self.next_sort = sorting("Engagement")
        self.ghl("nice", contact_id="g1", name="Mia")
        self.next_sort = sorting("Client concern")
        self.ghl("any update?", contact_id="g1", name="Mia Cruz", email="mia@example.com")
        leads = self.rows("leads")
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads[0]["name"], leads[0]["email"], leads[0]["contact_id"]), ("Mia", "mia@example.com", "g1"))

    def test_with_no_email_or_ghl_contact_the_same_name_is_the_same_person(self):
        self.next_sort = sorting("Engagement")
        self.add("wow", author="José  Cruz")
        self.add("great work", author="jose cruz")
        self.assertEqual(len(self.rows("leads")), 1)
        # Different emails are different people, whatever the name.
        self.add("hi", author="Ana Reyes", email="ana1@example.com")
        self.add("hi again", author="Ana Reyes", email="ana2@example.com")
        self.assertEqual([l["name"] for l in self.rows("leads")], ["José  Cruz", "Ana Reyes", "Ana Reyes"])

    def test_no_name_email_or_ghl_contact_saves_no_contact(self):
        self.next_sort = sorting("Engagement")
        self.add("wow", author="")
        self.add("nice", author="Facebook User")
        self.assertEqual(self.rows("leads"), [])

    def test_a_saved_contact_who_asks_about_a_service_joins_the_sales_journey(self):
        self.make_newlead_live()
        self.next_sort = sorting("Complaint")
        self.add("You were late", email="ben@example.com", author="Ben")
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much is a website?", email="ben@example.com", author="Ben")
        leads = self.rows("leads")
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads[0]["kind"], leads[0]["status"], leads[0]["interest"]), ("new", "active", "Website"))
        self.assertEqual(self.rows("messages")[1]["route"], "lead")

    def test_a_sales_contact_who_complains_stays_one_contact_and_their_journey_stops(self):
        self.make_newlead_live()
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="ben@example.com")
        self.next_sort = sorting("Complaint")
        self.add("Still no answer", email="ben@example.com")
        leads = self.rows("leads")
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads[0]["kind"], leads[0]["status"]), ("new", "replied"))
        self.assertEqual(self.rows("messages")[1]["lead_id"], leads[0]["id"])

    def test_the_clock_adds_past_senders_once_oldest_first(self):
        # Messages and a contact from before everyone became a contact.
        def old_message(author, email=None, category="Complaint", lead_id=None, at=0):
            return self.store.insert("messages", {
                "author": author, "email": email, "phone": None, "contact_id": None, "channel": "facebook",
                "text": "x", "sort_status": "sorted", "category": category, "lead_id": lead_id,
                "received_at": self.clock() - at * MIN})
        first = old_message("Ana Cruz", "ana@example.com", at=300)
        old_message("Spam Bot", category="Spam", at=200)
        sales = old_message("Ben Lim", "ben@example.com", category="Pricing question", at=150)
        ben = self.store.insert("leads", {"kind": "new", "name": "Ben Lim", "email": "ben@example.com",
                                          "status": "completed", "created_at": self.clock(), "message_id": sales})
        self.store.update("messages", sales, {"lead_id": ben})
        later = old_message("ana cruz", "ana@example.com", category="Engagement", at=100)
        old_message("Ben Lim", category="Billing question", at=50)  # no email: matched by name

        self.assertEqual(self.desk.add_past_contacts(batch=2), 1)  # a batch at a time: Ana
        self.desk.add_past_contacts(batch=2)
        self.desk.add_past_contacts(batch=2)
        self.assertEqual(self.desk.add_past_contacts(batch=2), 0)  # done: from now on it does nothing
        self.assertTrue(self.store.get("jobs", "past_contacts")["done"])

        leads = self.rows("leads")
        self.assertEqual([(l["name"], l["kind"]) for l in leads], [("Ben Lim", "new"), ("Ana Cruz", "general")])
        ana = leads[1]
        self.assertEqual(ana["created_at"], self.clock() - 300 * MIN)  # when they first wrote, not today
        msgs = {m["id"]: m for m in self.rows("messages")}
        self.assertEqual((msgs[first]["lead_id"], msgs[later]["lead_id"]), (ana["id"], ana["id"]))
        self.assertEqual(msgs[max(msgs)]["lead_id"], ben)
        self.assertEqual(leads[0]["name_key"], "ben lim")  # older contacts get the key duplicates are checked by
        self.assertEqual(len([m for m in msgs.values() if m["lead_id"] is None]), 1)  # only the spam

    def test_a_facebook_comment_then_an_email_are_two_contacts_until_merged(self):
        # GHL gives the comment a contact but no email; the email has no GHL contact. Nothing
        # proves they are one person, so the desk keeps two and leaves it to someone to merge.
        self.make_newlead_live()
        self.next_sort = sorting("Pricing question", interest="Website")
        self.ghl("How much is a website?", contact_id="g-juan", name="Juan Dela Cruz")
        self.next_sort = sorting("Complaint")
        self.add("Nobody replied to my comment", email="juan@example.com", author="Juan Dela Cruz", channel="email")
        fb, mail = self.rows("leads")
        self.assertEqual((fb["kind"], mail["kind"]), ("new", "general"))
        self.store.insert("emails", {"lead_id": mail["id"], "subject": "s", "at": self.clock()})
        self.desk.tag_leads([mail["id"]], ["VIP"], "mia@redefine.ph")

        self.desk.merge_leads(mail["id"], fb["id"], "mia@redefine.ph")  # keep the email one
        kept = self.desk._one("leads", mail["id"])
        self.assertEqual((kept["email"], kept["contact_id"], kept["tags"]), ("juan@example.com", "g-juan", ["VIP"]))
        # The sales journey wins over none, and with an email now it is settled as one contact's
        # would be: Juan wrote in again after asking, which stops a journey.
        self.assertEqual((kept["kind"], kept["status"], kept["interest"]), ("new", "replied", "Website"))
        self.assertEqual(kept["created_at"], fb["created_at"])
        self.assertTrue(self.store.get("leads", fb["id"])["deleted"])
        self.assertEqual({m["lead_id"] for m in self.rows("messages")}, {mail["id"]})
        self.assertEqual({e["lead_id"] for e in self.rows("events") if e["lead_id"]}, {mail["id"]})
        self.assertEqual(self.rows("emails")[-1]["lead_id"], mail["id"])
        self.assertEqual(self.rows("events")[-1]["label"], "Merged")
        # From now on both the email and the GHL contact find the one contact.
        self.next_sort = sorting("Engagement")
        self.ghl("thanks!", contact_id="g-juan", name="Juan")
        self.assertEqual(len([l for l in self.rows("leads") if not l.get("deleted")]), 1)

    def test_merging_gives_a_dm_only_sales_contact_its_journey_when_they_had_not_written_again(self):
        self.make_newlead_live()
        self.next_sort = sorting("Engagement")
        mail = self.desk._one("messages", self.add("love your work", email="lea@example.com", author="Lea Tan"))["lead_id"]
        self.clock.advance(5)
        self.next_sort = sorting("Service inquiry", interest="Branding")
        fb = self.desk._one("messages", self.ghl("Do you do logos?", contact_id="g-lea", name="Lea Tan"))["lead_id"]
        self.assertEqual(self.desk._one("leads", fb)["status"], "manual")
        self.desk.merge_leads(mail, fb, "mia@redefine.ph")
        kept = self.desk._one("leads", mail)
        self.assertEqual((kept["kind"], kept["status"], kept["email"]), ("new", "active", "lea@example.com"))
        self.assertIsNotNone(kept["next_at"])

    def test_merging_keeps_a_second_email_in_the_activity(self):
        self.next_sort = sorting("Engagement")
        a = self.desk._one("messages", self.add("hi", email="ana1@example.com", author="Ana Reyes"))["lead_id"]
        b = self.desk._one("messages", self.add("hey", email="ana2@example.com", author="Ana Reyes"))["lead_id"]
        self.desk.merge_leads(a, b, "mia@redefine.ph")
        self.assertEqual(self.desk._one("leads", a)["email"], "ana1@example.com")
        self.assertIn("Their other email ana2@example.com", self.rows("events")[-1]["detail"])
        with self.assertRaises(DeskError):
            self.desk.merge_leads(a, a, "mia@redefine.ph")
        with self.assertRaises(NotFound):
            self.desk.merge_leads(a, b, "mia@redefine.ph")  # already merged in

    def test_two_people_with_one_name_can_be_kept_apart(self):
        self.next_sort = sorting("Engagement")
        a = self.desk._one("messages", self.add("nice post", email="juan1@example.com", author="Juan Dela Cruz"))["lead_id"]
        b = self.desk._one("messages", self.add("great reel", email="juan2@example.com", author="Juan Dela Cruz"))["lead_id"]
        self.desk.keep_leads_separate(a, b, "mia@redefine.ph")
        self.assertEqual((self.desk._one("leads", a)["distinct"], self.desk._one("leads", b)["distinct"]), ([b], [a]))

    def test_a_contacts_page_has_their_whole_conversation(self):
        self.mail_on = True
        self.next_sort = sorting("Complaint")
        first = self.add("You were late", email="ana@example.com", author="Ana", channel="email")
        self.clock.advance(5)
        self.next_sort = sorting("Engagement")
        self.add("ok thanks", email="ana@example.com", author="Ana")
        self.next_sort = sorting("Engagement")
        self.add("someone else", email="ben@example.com", author="Ben")
        self.desk.email_reply(first, "Sorry, we're on it.", "mia@redefine.ph")
        lead_id = self.desk._one("messages", first)["lead_id"]
        thread = self.desk.contact_thread(lead_id)
        self.assertEqual(thread["lead"]["id"], lead_id)
        self.assertEqual([m["text"] for m in thread["messages"]], ["You were late", "ok thanks"])  # oldest first, only hers
        self.assertNotIn("raw", thread["messages"][0])
        self.assertEqual([t["code"] for t in thread["tickets"]], ["RD-1001"])  # the ticket from her complaint
        self.assertEqual([e["journey"] for e in thread["emails"]], ["received", "reply"])  # the ticket's email and our reply
        self.assertIn("Contact saved", [e["label"] for e in thread["events"]])
        self.assertIn("Ticket created", [e["label"] for e in thread["events"]])  # the ticket's activity too
        with self.assertRaises(NotFound):
            self.desk.contact_thread(999)

    def test_unsubscribing_marks_every_contact_with_that_email(self):
        self.make_newlead_live()
        self.next_sort = sorting("Complaint")
        self.add("Late again", email="ana@example.com", author="Ana")
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="ben@example.com", author="Ben")
        self.desk.unsubscribe("ANA@example.com")
        self.desk.unsubscribe("ben@example.com")
        ana, ben = self.rows("leads")
        self.assertEqual((ana["status"], ana["unsubscribed_at"]), ("saved", self.clock()))  # no journey to stop
        self.assertEqual((ben["status"], ben["unsubscribed_at"]), ("unsubscribed", self.clock()))
        # Someone who unsubscribed earlier is marked the moment they become a contact.
        self.desk.unsubscribe("cy@example.com")
        self.clock.advance(5)
        self.next_sort = sorting("Engagement")
        self.add("nice", email="cy@example.com", author="Cy")
        self.assertEqual(self.rows("leads")[-1]["unsubscribed_at"], self.clock() - 5 * MIN)

    def test_the_clock_marks_contacts_who_unsubscribed_before_once(self):
        self.store.put("suppressions", "old@example.com", {"email": "old@example.com", "at": 123, "reason": "unsubscribed"})
        lead = self.store.insert("leads", {"name": "Old", "email": "old@example.com", "kind": "general",
                                           "status": "saved", "created_at": self.clock()})
        self.assertEqual(self.desk.tick()["unsubscribed_contacts"], 1)
        self.assertEqual(self.store.get("leads", lead)["unsubscribed_at"], 123)
        self.assertEqual(self.desk.tick()["unsubscribed_contacts"], 0)

    def test_the_clock_runs_the_catch_up(self):
        self.store.insert("messages", {"author": "Ana", "email": None, "contact_id": None, "channel": "facebook",
                                       "text": "x", "sort_status": "sorted", "category": "Engagement",
                                       "lead_id": None, "received_at": self.clock()})
        self.assertEqual(self.desk.tick()["past_contacts"], 1)
        self.assertEqual(self.desk.tick()["past_contacts"], 0)


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


class EmailFromDeskTest(DeskTestCase):
    def email_in(self, text="My site is down since yesterday", category="Client concern"):
        self.next_sort = sorting(category)
        return self.desk.ingest(parse_email({"from_email": "ana@example.com", "from_name": "Ana Cruz",
                                             "subject": "Site down", "text": text, "message_id": "<abc@mail>"}))["id"]

    def test_replies_land_in_the_customers_thread_and_count_as_first_reply(self):
        self.mail_on = True
        message_id = self.email_in()
        self.sent.clear()  # the "we received your concern" email
        self.desk.email_reply(message_id, "Sorry Ana, we're on it.", "mia@redefine.ph")
        reply = self.sent[-1]
        self.assertEqual((reply["to"], reply["subject"], reply["unsubscribe"]), ("ana@example.com", "Re: Site down", None))
        self.assertEqual(reply["headers"], {"In-Reply-To": "<abc@mail>", "References": "<abc@mail>"})
        self.assertTrue(reply["body"].startswith("Sorry Ana, we're on it.\n\nOn "))
        self.assertIn("Ana Cruz wrote:\n> My site is down since yesterday", reply["body"])
        msg, ticket = self.rows("messages")[0], self.rows("tickets")[0]
        self.assertEqual((msg["reply_text"], msg["replied_by"]), ("Sorry Ana, we're on it.", "mia@redefine.ph"))
        self.assertIsNotNone(ticket["first_reply_at"])
        self.desk.email_reply(message_id, "Fixed now.", "mia@redefine.ph")  # a second reply is fine
        self.assertEqual(self.sent[-1]["subject"], "Re: Site down")

    def test_reply_refusals(self):
        message_id = self.email_in()
        with self.assertRaisesRegex(DeskError, "isn't set up"):
            self.desk.email_reply(message_id, "hi", "mia@redefine.ph")
        self.mail_on = True
        with self.assertRaisesRegex(DeskError, "Write the reply"):
            self.desk.email_reply(message_id, "  ", "mia@redefine.ph")
        facebook = self.add("Nice work!")
        with self.assertRaisesRegex(DeskError, "Only emails"):
            self.desk.email_reply(facebook, "thanks", "mia@redefine.ph")

    def test_contacts_get_their_own_email_with_their_first_name(self):
        self.mail_on = True
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="ana@example.com", author="Ana Cruz")
        self.add("Rates?", email="ben@example.com", author="Ben Reyes")
        self.add("Price?", author="No Email")
        ids = [l["id"] for l in self.rows("leads")]
        self.desk.unsubscribe("ben@example.com")
        self.sent.clear()
        result = self.desk.email_contacts(ids, "Hi {first_name}", "Hello {first_name}, here are our rates.", "mia@redefine.ph")
        self.assertEqual(result, {"sent": 1, "unsubscribed": 1, "failed": 0, "no_email": 1})
        self.assertEqual([(m["to"], m["subject"]) for m in self.sent], [("ana@example.com", "Hi Ana")])
        self.assertTrue(self.sent[0]["unsubscribe"].startswith("https://desk.example/unsubscribe"))  # a group send

        self.sent.clear()
        one = self.desk.email_contacts([ids[0]], "Quick one", "Hi {first_name}", "mia@redefine.ph")
        self.assertEqual((one["sent"], self.sent[0]["unsubscribe"], self.sent[0]["body"]), (1, None, "Hi Ana"))

        with self.assertRaisesRegex(DeskError, "subject and a message"):
            self.desk.email_contacts(ids, "", "body", "mia@redefine.ph")
        with self.assertRaisesRegex(DeskError, "None of the selected"):
            self.desk.email_contacts([ids[2]], "s", "b", "mia@redefine.ph")
        self.mail_on = False
        with self.assertRaisesRegex(DeskError, "isn't set up"):
            self.desk.email_contacts(ids, "s", "b", "mia@redefine.ph")


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
        self.assertEqual(reasons, [None, "No email consent", "Already a contact", "Unsubscribed",
                                   "Contacted in the last 90 days", "No valid email", "Already a contact"])
        result = self.desk.import_leads(rows, "tester@redefine.ph")
        self.assertEqual(result, {"enrolled": 1, "skipped": 6})
        old = [l for l in self.rows("leads") if l["kind"] == "old"]
        self.assertEqual((old[0]["interest"], old[0]["status"]), ("Branding", "queued"))


class ContactsTest(DeskTestCase):
    def setUp(self):
        super().setUp()
        self.next_sort = sorting("Pricing question", interest="Website")
        for name in ("Ben Cruz", "Carla Santos", "Dan Reyes"):
            self.add(f"How much for a website? - {name}", email=f"{name.split()[0].lower()}@example.com", author=name)
        self.ids = [l["id"] for l in self.rows("leads")]

    def tags(self):
        return [l["tags"] for l in self.rows("leads")]

    def test_new_contacts_start_with_no_tags_and_keep_a_phone_from_ghl(self):
        self.desk.ingest(parse_ghl({"full_name": "Eve Lim", "phone": "+639171234567",
                                    "customData": {"comment": "Rates for ads?", "channel": "Facebook"}}))
        lead = self.rows("leads")[-1]
        self.assertEqual((lead["name"], lead["phone"], lead["tags"]), ("Eve Lim", "+639171234567", []))

    def test_tags_are_added_once_and_removed_ignoring_case(self):
        self.assertEqual(self.desk.tag_leads(self.ids[:2], [" VIP ", "Cebu", "vip", ""], "mia@redefine.ph"), 2)
        self.assertEqual(self.tags(), [["VIP", "Cebu"], ["VIP", "Cebu"], []])
        self.assertEqual(self.desk.tag_leads(self.ids, ["vip", "Hot  lead"], "mia@redefine.ph"), 3)
        self.assertEqual(self.tags(), [["VIP", "Cebu", "Hot lead"], ["VIP", "Cebu", "Hot lead"], ["vip", "Hot lead"]])
        self.assertEqual(self.desk.tag_leads(self.ids, ["VIP"], "mia@redefine.ph", True), 3)
        self.assertEqual(self.tags(), [["Cebu", "Hot lead"], ["Cebu", "Hot lead"], ["Hot lead"]])
        self.assertEqual(self.desk.tag_leads(self.ids, ["Hot lead"], "mia@redefine.ph"), 0)  # nothing new
        labels = [e["label"] for e in self.rows("events") if e["lead_id"] == self.ids[0]]
        self.assertEqual(labels[-3:], ["Tagged", "Tagged", "Tags removed"])

    def test_tags_must_be_given_and_fit(self):
        with self.assertRaises(DeskError):
            self.desk.tag_leads(self.ids, [" ", ""], "mia@redefine.ph")
        self.desk.tag_leads(self.ids[:1], [f"t{i}" for i in range(20)], "mia@redefine.ph")
        with self.assertRaises(DeskError):  # one contact would go over: nobody is changed
            self.desk.tag_leads(self.ids, ["one more"], "mia@redefine.ph")
        self.assertEqual(self.tags()[1:], [[], []])

    def test_delete_keeps_the_message_but_not_the_contact(self):
        since = self.desk.state()["now"]
        self.clock.advance(1)
        self.assertEqual(self.desk.delete_leads([self.ids[0], self.ids[0], 999], "boss@redefine.ph"), 1)

        gone = self.store.get("leads", self.ids[0])
        self.assertEqual(gone, {"id": self.ids[0], "deleted": True, "updated_at": self.clock()})  # no name, email or phone
        self.assertFalse([e for e in self.rows("events") if e["lead_id"] == self.ids[0]])
        msg = self.rows("messages")[0]
        self.assertEqual((msg["text"], msg["lead_id"], msg["route"]), ("How much for a website? - Ben Cruz", None, None))

        self.assertEqual(sorted(l["id"] for l in self.desk.state()["leads"]), self.ids[1:])  # full load leaves it out
        self.assertIn({"id": self.ids[0], "deleted": True, "updated_at": self.clock()},
                      self.desk.state(since)["leads"])  # open pages are told it went
        with self.assertRaises(Exception):
            self.desk.lead_action(self.ids[0], "won", "boss@redefine.ph")
        self.assertEqual(self.desk.tag_leads([self.ids[0]], ["VIP"], "boss@redefine.ph"), 0)

        self.desk.message_action(msg["id"], "lead", "boss@redefine.ph")  # the message can become a contact again
        self.assertEqual(len([l for l in self.rows("leads") if not l.get("deleted")]), 3)


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
        # Signing in brings them back to the view they opened.
        for path in ("/", "/tickets", "/email/oldlead"):
            r = self.client.get(path, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/login?next=" + path))
        login = self.client.get("/login")
        self.assertIn(SIGN_IN["GOOGLE_CLIENT_ID"], login.text)

    def test_the_desk_build_is_served_behind_the_sign_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp)
            (web / "assets").mkdir()
            (web / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
            (web / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
            with mock.patch("care_desk.app.WEB", web):
                client = TestClient(create_app(self.desk))
            client.cookies.set(team_auth.COOKIE, team_auth.make_session("owner@redefine.ph", SIGN_IN["SESSION_SECRET"].encode()))
            client.post("/auth/name", json={"name": "Owner"}, headers={"X-Requested-With": "desk"})
            # Every view's address gets the same page; the page picks the view.
            for path in ("/", "/inbox", "/tickets", "/contacts", "/contacts/12", "/email", "/email/oldlead", "/team"):
                r = client.get(path)
                self.assertEqual(r.status_code, 200, path)
                self.assertIn('id="root"', r.text)
                self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/settings").status_code, 404)
            self.assertEqual(client.get("/assets/index-abc.js").text, "console.log(1)")
            self.assertEqual(client.get("/assets/missing.js").status_code, 404)
            # The desk's old address still opens it.
            r = client.get("/desk", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (301, "/"))
            self.assertEqual(client.get("/desk-next").status_code, 404)
            # Already signed in, the sign-in page sends them on to where they were going.
            r = client.get("/login?next=/tickets", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/tickets"))
            r = client.get("/login?next=//elsewhere.example", follow_redirects=False)
            self.assertEqual(r.headers["location"], "/")

    def test_the_desk_says_so_when_it_was_not_built(self):
        with mock.patch("care_desk.app.WEB", Path(tempfile.gettempdir()) / "no-desk-build-here"):
            client = TestClient(create_app(self.desk))
        client.cookies.set(team_auth.COOKIE, team_auth.make_session("owner@redefine.ph", SIGN_IN["SESSION_SECRET"].encode()))
        client.post("/auth/name", json={"name": "Owner"}, headers={"X-Requested-With": "desk"})
        r = client.get("/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("npm run build", r.text)

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
        self.assertEqual(state["user"], {"email": "agent@redefine.ph", "role": "agent", "name": None})
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

    def test_contacts_in_bulk_over_http(self):
        desk_team, h = self.client.app.state.team, {"X-Requested-With": "desk"}
        desk_team.add("agent@redefine.ph", "agent", by="owner@redefine.ph")
        self.next_sort = sorting("Pricing question", interest="Website")
        self.add("How much?", email="ben@example.com")
        lead_id = self.rows("leads")[0]["id"]

        self.sign_in("agent@redefine.ph")
        bulk = lambda body, headers=h: self.client.post("/api/desk/leads/bulk", json=body, headers=headers)  # noqa: E731
        self.assertEqual(bulk({"action": "tag", "ids": [lead_id], "tags": ["VIP"]}, {}).status_code, 403)
        r = bulk({"action": "tag", "ids": [lead_id], "tags": ["VIP"]})
        self.assertEqual((r.status_code, r.json()["changed"]), (200, 1))
        self.assertEqual(self.rows("leads")[0]["tags"], ["VIP"])
        self.assertEqual(bulk({"action": "untag", "ids": [lead_id], "tags": ["vip"]}).json()["changed"], 1)
        self.assertEqual(bulk({"action": "tag", "ids": [], "tags": ["VIP"]}).status_code, 400)
        self.assertEqual(bulk({"action": "tag", "ids": [lead_id], "tags": []}).status_code, 400)
        self.assertEqual(bulk({"action": "explode", "ids": [lead_id]}).status_code, 400)
        r = bulk({"action": "delete", "ids": [lead_id]})
        self.assertEqual(r.status_code, 403)
        self.assertIn("admins", r.json()["detail"])

        self.sign_in("owner@redefine.ph")
        self.assertEqual(bulk({"action": "delete", "ids": [lead_id]}).json()["changed"], 1)
        self.assertEqual(self.client.get("/api/desk/state").json()["leads"], [])

    def test_merge_and_keep_apart_over_http(self):
        self.client.app.state.team.add("agent@redefine.ph", "agent", by="owner@redefine.ph")
        h = {"X-Requested-With": "desk"}
        self.next_sort = sorting("Engagement")
        self.add("nice post", email="juan1@example.com", author="Juan Dela Cruz")
        self.add("great reel", email="juan2@example.com", author="Juan Dela Cruz")
        self.add("love it", email="juan3@example.com", author="Juan Dela Cruz")
        a, b, c = (l["id"] for l in self.rows("leads"))
        self.sign_in("agent@redefine.ph")  # merging moves everything and loses nothing, so agents may
        post = lambda path, other, headers=h: self.client.post(f"/api/desk/leads/{path}", json={"other": other}, headers=headers)  # noqa: E731
        self.assertEqual(post(f"{a}/merge", b, {}).status_code, 403)
        self.assertEqual(post(f"{a}/merge", b).status_code, 200)
        self.assertEqual(post(f"{a}/merge", b).status_code, 404)  # already merged in
        self.assertEqual(post(f"{a}/merge", a).status_code, 400)
        self.assertEqual(post(f"{a}/separate", c).status_code, 200)
        leads = {l["id"]: l for l in self.client.get("/api/desk/state").json()["leads"]}
        self.assertEqual(sorted(leads), [a, c])
        page = self.client.get(f"/api/desk/leads/{a}").json()  # a contact's page
        self.assertEqual((page["lead"]["id"], len(page["messages"])), (a, 2))
        self.assertEqual(self.client.get(f"/api/desk/leads/{b}").status_code, 404)
        self.assertEqual((leads[a]["distinct"], leads[c]["distinct"]), ([c], [a]))

    def test_never_runs_open_without_sign_in(self):
        for name in SIGN_IN:
            os.environ.pop(name)
        self.assertEqual(self.client.get("/api/desk/state").status_code, 503)
        self.assertEqual(self.client.get("/").status_code, 503)
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

    def test_email_webhook_files_emails_like_any_message(self):
        os.environ["DESK_FROM_EMAIL"] = "Redefine <info@redefineph.com>"
        self.addCleanup(os.environ.pop, "DESK_FROM_EMAIL", None)
        body = {"from_email": "cara@example.com", "from_name": "Cara", "subject": "Rates",
                "text": "How much for a product video?", "message_id": "<m1@mail>"}
        self.assertEqual(self.client.post("/hooks/email", json=body).status_code, 401)
        self.assertEqual(self.client.post("/hooks/email?key=wrong", json=body).status_code, 401)
        self.next_sort = sorting("Pricing question", interest="Video production")
        r = self.client.post("/hooks/email", json=body, headers={"X-Desk-Key": "hook-secret"}).json()
        self.assertEqual((r["ignored"], r["duplicate"]), (False, False))
        msg, lead = self.rows("messages")[0], self.rows("leads")[0]
        self.assertEqual((msg["channel"], msg["source"], msg["route"]), ("email", "email", "lead"))
        self.assertEqual((lead["email"], lead["name"]), ("cara@example.com", "Cara"))
        # n8n retries and re-reads are ignored by Message-ID.
        again = self.client.post("/hooks/email?key=hook-secret", json=body).json()
        self.assertTrue(again["duplicate"])
        # Our own mail and bounces never land in the desk.
        own = {**body, "from_email": "info@redefineph.com", "message_id": "<m2@mail>"}
        self.assertTrue(self.client.post("/hooks/email?key=hook-secret", json=own).json()["ignored"])
        self.assertEqual(len(self.rows("messages")), 1)
        empty = {**body, "text": "", "message_id": "<m3@mail>"}
        self.assertEqual(self.client.post("/hooks/email?key=hook-secret", json=empty).status_code, 400)

    def test_email_routes_need_staff_and_the_header(self):
        self.mail_on = True
        self.next_sort = sorting("Pricing question", interest="Website")
        message_id = self.desk.ingest(parse_email({"from_email": "cara@example.com", "text": "Rates?", "message_id": "<x@m>"}))["id"]
        lead_id = self.rows("leads")[0]["id"]
        h = {"X-Requested-With": "desk"}
        self.assertEqual(self.client.post(f"/api/desk/messages/{message_id}/email", json={"text": "hi"}, headers=h).status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.post(f"/api/desk/messages/{message_id}/email", json={"text": "hi"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/desk/messages/{message_id}/email", json={"text": "Here you go"}, headers=h).status_code, 200)
        r = self.client.post("/api/desk/leads/email", json={"ids": [lead_id], "subject": "Rates", "body": "Hi {first_name}"}, headers=h)
        self.assertEqual((r.status_code, r.json()["sent"]), (200, 1))
        self.assertEqual(self.client.post("/api/desk/leads/email", json={"ids": [lead_id], "subject": "", "body": "x"}, headers=h).status_code, 400)

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



@unittest.skipUnless(PG_URL, "set DESK_TEST_PG_URL to test the Supabase store")
class SupabaseStoreTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make_store(self.clock)
        self.addCleanup(self.store.close)

    def test_ids_count_up_per_collection(self):
        self.assertEqual([self.store.insert("tickets", {}) for _ in range(3)], [1, 2, 3])
        self.assertEqual(self.store.insert("leads", {}), 1)
        self.assertEqual(self.store.get("tickets", 2)["id"], 2)

    def test_bytes_come_back_as_bytes(self):
        data = bytes(range(256)) * 10
        self.store.put("file_parts", "1-0", {"file_id": 1, "data": data})
        self.assertEqual(self.store.get("file_parts", "1-0")["data"], data)

    def test_update_merges_and_stamps(self):
        i = self.store.insert("messages", {"text": "hi", "route": None})
        self.clock.advance(1)
        self.store.update("messages", i, {"route": "ticket"})
        doc = self.store.get("messages", i)
        self.assertEqual((doc["text"], doc["route"], doc["updated_at"]), ("hi", "ticket", self.clock()))
        with self.assertRaises(KeyError):
            self.store.update("messages", 99, {"route": "x"})

    def test_ranges_skip_nulls_and_other_types(self):
        for at in (5, None, "7", 9):
            self.store.insert("leads", {"next_at": at})
        self.assertEqual([d["next_at"] for d in self.store.where("leads", "next_at", "<=", 10, order="next_at")], [5, 9])
        self.assertEqual([d["next_at"] for d in self.store.where("leads", "next_at", ">", 5)], [9])

    def test_equality_and_in_respect_types(self):
        self.store.insert("events", {"ticket_id": 1})
        self.store.insert("events", {"ticket_id": "1"})
        self.store.insert("events", {"ticket_id": 2})
        self.assertEqual(len(self.store.where("events", "ticket_id", "==", 1)), 1)
        self.assertEqual(len(self.store.where("events", "ticket_id", "in", [1, 2])), 2)
        self.assertEqual(self.store.where("events", "ticket_id", "in", []), [])

    def test_recent_is_newest_first_and_skips_records_without_the_field(self):
        for at in (3, 1, 2):
            self.store.insert("emails", {"at": at})
        self.store.insert("emails", {})
        self.assertEqual([d["at"] for d in self.store.recent("emails", "at", 2)], [3, 2])

    def test_copy_in_keeps_the_newer_copy_and_counters_only_go_up(self):
        self.assertEqual(self.store.copy_in("tickets", "4", {"id": 4, "status": "Open", "updated_at": 10}), "added")
        self.assertEqual(self.store.copy_in("tickets", "4", {"id": 4, "status": "Old", "updated_at": 5}), "kept")
        self.assertEqual(self.store.copy_in("tickets", "4", {"id": 4, "status": "Resolved", "updated_at": 20}), "updated")
        self.assertEqual(self.store.get("tickets", 4)["status"], "Resolved")
        self.store.raise_counter("tickets", 4)
        self.store.raise_counter("tickets", 2)
        self.assertEqual(self.store.insert("tickets", {}), 5)

    def test_rejects_names_that_are_not_plain_words(self):
        with self.assertRaises(ValueError):
            self.store.get("tickets; drop table x", 1)
        with self.assertRaises(ValueError):
            self.store.where("tickets", "status' or '1", "==", 1)



class FakeSnap:
    def __init__(self, key, data):
        self.id, self._data = key, data

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, name, docs):
        self.id, self.docs = name, docs

    def stream(self):
        return [FakeSnap(k, v) for k, v in self.docs.items()]


class FakeFirestore:
    project = "redefine-test"

    def __init__(self, collections):
        self.data = collections

    def collections(self):
        return [FakeCollection(name, docs) for name, docs in self.data.items()]


@unittest.skipUnless(PG_URL, "set DESK_TEST_PG_URL to test the move to Supabase")
class MoveToSupabaseTest(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone

        self.clock = Clock()
        self.store = make_store(self.clock)
        self.addCleanup(self.store.close)
        self.firestore = FakeFirestore({
            "desk_tickets": {"7": {"id": 7, "status": "Open", "updated_at": 10},
                             "8": {"id": 8, "status": "Open", "updated_at": 10}},
            "desk_file_parts": {"1-0": {"file_id": 1, "data": b"\x00\xffpdf", "updated_at": 10}},
            "desk_team": {"ana@redefine.ph": {"email": "ana@redefine.ph", "role": "agent",
                                              "seen": datetime(2026, 10, 1, tzinfo=timezone.utc)}},
            "desk_counters": {"tickets": {"next": 9}},
            "team_members": {"x@redefine.ph": {"role": "admin"}},  # the app's, not the desk's
        })

    def move(self):
        from care_desk import move_to_supabase

        move_to_supabase.move(self.firestore, self.store, out=lambda _line: None)

    def test_copies_the_desk_and_leaves_the_app_alone(self):
        self.move()
        self.assertEqual(self.store.get("tickets", 7)["status"], "Open")
        self.assertEqual(self.store.get("file_parts", "1-0")["data"], b"\x00\xffpdf")
        self.assertEqual(self.store.get("team", "ana@redefine.ph")["seen"], "2026-10-01T00:00:00+00:00")
        from care_desk.move_to_supabase import _collections

        self.assertEqual(list(_collections(self.firestore)), ["counters", "file_parts", "team", "tickets"])
        self.assertEqual(self.store.insert("tickets", {}), 110)  # ID_GAP past Firestore's counter

    def test_records_the_old_desk_adds_meanwhile_get_ids_the_new_desk_never_uses(self):
        self.move()
        new = self.store.insert("tickets", {"status": "Open"})  # the new desk, after the switch
        self.firestore.data["desk_tickets"]["9"] = {"id": 9, "status": "Late arrival", "updated_at": 30}
        self.move()
        self.assertEqual(self.store.get("tickets", 9)["status"], "Late arrival")
        self.assertEqual(self.store.get("tickets", new)["status"], "Open")

    def test_a_second_run_brings_only_newer_changes(self):
        self.move()
        self.store.update("tickets", 7, {"status": "Resolved"})  # worked on Supabase after the first run
        self.firestore.data["desk_tickets"]["8"] = {"id": 8, "status": "In progress", "updated_at": 20}
        self.move()
        self.assertEqual(self.store.get("tickets", 7)["status"], "Resolved")
        self.assertEqual(self.store.get("tickets", 8)["status"], "In progress")


if __name__ == "__main__":
    unittest.main()
