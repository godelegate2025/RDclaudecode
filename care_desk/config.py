"""Redefine's desk settings: issue types, teams, response targets and email journeys.

Everything a second client would change lives here, so a later multi-client
version only has to move this into the database.
"""

from __future__ import annotations

import os

BRAND = os.environ.get("DESK_BRAND", "Redefine")
TICKET_PREFIX = os.environ.get("DESK_TICKET_PREFIX", "RD")
SIGN_OFF = os.environ.get("DESK_SIGN_OFF", "The Redefine team")

CHANNELS = ["facebook", "instagram", "messenger", "tiktok", "linkedin", "threads", "email", "website", "other"]

# intent decides the route: sales becomes a lead, service a ticket, other is logged.
CATEGORIES = {
    "Service inquiry": {"intent": "sales", "hint": "Wants Redefine to make something: a website, videos, branding, social media management, ads."},
    "Pricing question": {"intent": "sales", "hint": "Asks how much a service costs or for a quote."},
    "Urgent issue": {"intent": "service", "hint": "Something is broken or at risk now: a client's site is down, an account was hacked, an ad is running wrong, a legal or safety problem."},
    "Client concern": {"intent": "service", "hint": "An existing client asks about a deliverable, a delay, revisions, or the status of their project."},
    "Complaint": {"intent": "service", "hint": "Unhappy with Redefine's work, service or response time."},
    "Billing question": {"intent": "service", "hint": "Invoices, payments, refunds, receipts."},
    "General question": {"intent": "service", "hint": "Any other question that needs an answer from the team."},
    "Job inquiry": {"intent": "other", "hint": "Applying for a job or internship, or asking if Redefine is hiring."},
    "Engagement": {"intent": "other", "hint": "Praise, reactions, emojis, tagging friends, short remarks like 'wow' or '#highlight'. Nothing to answer."},
    "Spam": {"intent": "other", "hint": "Bots, scams, unrelated promotions."},
}

TEAMS = {
    "Urgent issue": "Client success",
    "Client concern": "Client success",
    "Complaint": "Client success",
    "Billing question": "Finance",
    "General question": "Client success",
}
DEFAULT_TEAM = "Client success"

INTERESTS = ["Website", "Social media management", "Video production", "Branding", "Paid ads",
             "Content creation", "Photography", "Other"]

PRIORITIES = ["Urgent", "High", "Normal"]

# Minutes. Time to first reply, and time to resolution.
SLA = {
    "Urgent": {"first": 15, "resolve": 240},
    "High": {"first": 60, "resolve": 1440},
    "Normal": {"first": 240, "resolve": 2880},
}

DAY = 24 * 60 * 60 * 1000

# Templates use {first_name}, {ticket}, {team}, {interest} and {sign_off}.
# Anything in [SQUARE BRACKETS] is for Redefine to fill in; a journey that still
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
        "group": "leads", "name": "New lead journey", "default_status": "pending",
        "trigger": "Someone asks about a service or price, in a comment, a message or an email",
        "description": "Answers fast, shows the work, then invites them to a call.",
        "exit": "Stops when the lead replies, is marked won or closed, or unsubscribes.",
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
        "group": "leads", "name": "Old lead reactivation", "default_status": "pending",
        "trigger": "An imported lead with no contact for 90 days or more, who agreed to receive email",
        "description": "Re-opens old inquiries with three short emails, then closes them politely.",
        "exit": "Stops when the lead replies or unsubscribes. Leads who finish all three emails without replying are closed.",
        "steps": [
            {"delay": 0, "label": "Day 0", "subject": "Still thinking about {interest}?",
             "body": "Hi {first_name},\n\nYou asked us about {interest} a while back. If it is still on your mind, we can pick up where we left off.\n\nReply to this email and we will set up a quick call.\n\n{sign_off}"},
            {"delay": 4 * DAY, "label": "Day 4", "subject": "What's new at Redefine",
             "body": "Hi {first_name},\n\n[WHAT IS NEW OR ON OFFER THIS MONTH]\n\nReply if you want to hear more.\n\n{sign_off}"},
            {"delay": 10 * DAY, "label": "Day 10", "subject": "Should we close your inquiry?",
             "body": "Hi {first_name},\n\nWe do not want to fill your inbox. If you are no longer interested, there is nothing to do and we will stop here.\n\nIf you are, reply \"yes\" and we will reach out this week.\n\n{sign_off}"},
        ],
    },
}
