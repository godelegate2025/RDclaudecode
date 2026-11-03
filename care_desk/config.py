"""One desk's settings: who it is for, issue types, teams, response targets and email journeys.

Each desk serves one business, named by DESK_CLIENT (default "redefine"). What
differs between businesses is in care_desk/clients/<client>.json: the brand,
what the business does, its services, the hints the AI sorts by, and, under
"deploy", what deploy-desk.ps1 needs. Everything else here is the same for
every desk. Redefine's own desk is the one that shares sign-in, names and the
team list with the Redefine App ("with_redefine_app"); a client's desk keeps
all of that, and its data, to itself.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CLIENTS = Path(__file__).parent / "clients"


def load_client(name: str) -> dict:
    """The settings file for one client; refuses names that could reach outside clients/."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise RuntimeError(f"DESK_CLIENT must be a client file name like 'redefine', not {name!r}.")
    path = CLIENTS / f"{name}.json"
    if not path.is_file():
        raise RuntimeError(f"No settings for client {name!r}: add care_desk/clients/{name}.json "
                           "(copy _example.json).")
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("brand", "about", "services") if not data.get(k)]
    if missing:
        raise RuntimeError(f"care_desk/clients/{name}.json is missing {', '.join(missing)}.")
    return data


CLIENT = os.environ.get("DESK_CLIENT", "").strip().lower() or "redefine"
_CLIENT = load_client(CLIENT)

BRAND = _CLIENT["brand"]
COMPANY = _CLIENT.get("company") or BRAND           # the footer of every email
ABOUT = _CLIENT["about"]                            # "a dental clinic in Cebu that ...", for the AI
LANGUAGES = _CLIENT.get("languages") or "English"   # what customers write in
TICKET_PREFIX = _CLIENT.get("ticket_prefix") or BRAND[:2].upper()
SIGN_OFF = _CLIENT.get("sign_off") or f"The {BRAND} team"
FACEBOOK_PAGE = _CLIENT.get("facebook_page") or BRAND
# Only Redefine's own desk: sign-in, names and the starting team come from the Redefine App.
WITH_REDEFINE_APP = bool(_CLIENT.get("with_redefine_app"))

CHANNELS = ["facebook", "instagram", "messenger", "tiktok", "linkedin", "threads", "email", "website", "other"]

INTERESTS = [s for s in _CLIENT["services"] if s != "Other"] + ["Other"]

# intent decides the route: sales becomes a lead, service a ticket, other is logged.
# A client file can replace any hint ("category_hints"); the categories themselves are fixed.
_HINTS = {
    "Service inquiry": f"Wants {BRAND} to provide something it offers: {', '.join(INTERESTS[:-1])}.",
    "Pricing question": "Asks how much a service costs or for a quote.",
    "Urgent issue": "Something is broken or at risk now: a service failed, an account was hacked, "
                    "a legal or safety problem.",
    "Client concern": "An existing customer asks about something already booked or bought: a delay, a change, "
                      "or its status.",
    "Complaint": f"Unhappy with {BRAND}'s work, service or response time.",
    "Billing question": "Invoices, payments, refunds, receipts.",
    "General question": "Any other question that needs an answer from the team.",
    "Job inquiry": f"Applying for a job or internship, or asking if {BRAND} is hiring.",
    "Engagement": "Praise, reactions, emojis, tagging friends, short remarks like 'wow' or '#highlight'. Nothing to answer.",
    "Spam": "Bots, scams, unrelated promotions.",
}
_INTENTS = {
    "Service inquiry": "sales", "Pricing question": "sales",
    "Urgent issue": "service", "Client concern": "service", "Complaint": "service",
    "Billing question": "service", "General question": "service",
    "Job inquiry": "other", "Engagement": "other", "Spam": "other",
}
_unknown = set(_CLIENT.get("category_hints") or {}) - set(_INTENTS)
if _unknown:
    raise RuntimeError(f"care_desk/clients/{CLIENT}.json has hints for unknown categories: {', '.join(sorted(_unknown))}.")
CATEGORIES = {name: {"intent": intent, "hint": (_CLIENT.get("category_hints") or {}).get(name) or _HINTS[name]}
              for name, intent in _INTENTS.items()}

TEAMS = {
    "Urgent issue": "Client success",
    "Client concern": "Client success",
    "Complaint": "Client success",
    "Billing question": "Finance",
    "General question": "Client success",
    **(_CLIENT.get("teams") or {}),
}
DEFAULT_TEAM = _CLIENT.get("default_team") or "Client success"

PRIORITIES = ["Urgent", "High", "Normal"]

# Minutes. Time to first reply, and time to resolution.
SLA = {
    "Urgent": {"first": 15, "resolve": 240},
    "High": {"first": 60, "resolve": 1440},
    "Normal": {"first": 240, "resolve": 2880},
}

DAY = 24 * 60 * 60 * 1000

# Templates use {first_name}, {ticket}, {team}, {interest} and {sign_off}.
# Anything in [SQUARE BRACKETS] is for the business to fill in; a journey that still
# has one cannot go live, so a placeholder never reaches a real inbox.
JOURNEYS = {
    "received": {
        "group": "service", "name": "Concern received", "default_status": "live",
        "trigger": "A ticket is created and the customer has an email on file",
        "description": "Confirms the concern is logged, with the ticket number and the team handling it.",
        "steps": [{"delay": 0, "label": "Instant", "subject": "We got your message · {ticket}",
                   "body": "Hi {first_name},\n\nYour message is logged as {ticket} and our {team} team has it. We will update you as it moves.\n\nIf you want to add anything, just reply to this email.\n\n{sign_off}"}],
    },
    "update": {
        "group": "service", "name": "Concern update", "default_status": "live",
        "trigger": "A ticket moves to In progress",
        "description": "Tells the customer someone is working on it, so they do not have to follow up.",
        "steps": [{"delay": 0, "label": "Instant", "subject": "We're on it · {ticket}",
                   "body": "Hi {first_name},\n\nOur {team} team is working on {ticket} now. We will email you again as soon as it is sorted.\n\n{sign_off}"}],
    },
    "resolved": {
        "group": "service", "name": "Concern resolved", "default_status": "live",
        "trigger": "A ticket is marked Resolved",
        "description": "Closes the loop and asks one question about how it was handled.",
        "steps": [{"delay": 0, "label": "Instant", "subject": "Sorted · {ticket}",
                   "body": "Hi {first_name},\n\n{ticket} is resolved. If anything still looks off, reply to this email and we will pick it back up.\n\nHow did we do? Reply with a number from 1 to 5.\n\n{sign_off}"}],
    },
    "newlead": {
        "group": "leads", "name": "New contact journey", "default_status": "pending",
        "trigger": "Someone asks about a service or price, in a comment, a message or an email",
        "description": "Answers fast, shows the work, then invites them to a call.",
        "exit": "Stops when the contact replies, is marked won or closed, or unsubscribes.",
        "steps": [
            {"delay": 0, "label": "Instant", "subject": "Thanks for asking about {interest}",
             "body": "Hi {first_name},\n\nThanks for reaching out about {interest}. Someone from our team will get back to you within one business day.\n\nIf it helps, reply with your timeline and a rough budget and we will come back with options that fit.\n\n{sign_off}"},
            {"delay": 2 * DAY, "label": "Day 2", "subject": "Some of our recent work",
             "body": "Hi {first_name},\n\nWhile you think it over, here is some of our recent work: [PORTFOLIO LINK]\n\nReply with any questions and we will answer them directly.\n\n{sign_off}"},
            {"delay": 5 * DAY, "label": "Day 5", "subject": "Want to talk it through?",
             "body": "Hi {first_name},\n\nIf you would like to talk through your {interest} project, book a short call here: [BOOKING LINK]\n\n{sign_off}"},
            {"delay": 10 * DAY, "label": "Day 10", "subject": "Should we close this for now?",
             "body": "Hi {first_name},\n\nWe do not want to crowd your inbox. If now is not the right time, there is nothing to do and we will stop here.\n\nIf you are still interested, reply \"yes\" and we will reach out this week.\n\n{sign_off}"},
        ],
    },
    "oldlead": {
        "group": "leads", "name": "Old contact reactivation", "default_status": "pending",
        "trigger": "An imported contact with no contact for 90 days or more, who agreed to receive email",
        "description": "Re-opens old inquiries with three short emails, then closes them politely.",
        "exit": "Stops when the contact replies or unsubscribes. Contacts who finish all three emails without replying are closed.",
        "steps": [
            {"delay": 0, "label": "Day 0", "subject": "Still thinking about {interest}?",
             "body": "Hi {first_name},\n\nYou asked us about {interest} a while back. If it is still on your mind, we can pick up where we left off.\n\nReply to this email and we will set up a quick call.\n\n{sign_off}"},
            {"delay": 4 * DAY, "label": "Day 4", "subject": f"What's new at {BRAND}",
             "body": "Hi {first_name},\n\n[WHAT IS NEW OR ON OFFER THIS MONTH]\n\nReply if you want to hear more.\n\n{sign_off}"},
            {"delay": 10 * DAY, "label": "Day 10", "subject": "Should we close your inquiry?",
             "body": "Hi {first_name},\n\nWe do not want to fill your inbox. If you are no longer interested, there is nothing to do and we will stop here.\n\nIf you are, reply \"yes\" and we will reach out this week.\n\n{sign_off}"},
        ],
    },
}
