"""What the desk does: take a message in, sort it, route it, and follow up.

Emails are collected while a change is being made and sent after it is saved,
so a slow email service never holds anything up, and a change that fails part
way never emails anyone.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from . import auth, config, mailer, sorter
from .intake import Incoming
from .store import chunks

log = logging.getLogger("care-desk")

MIN = 60_000
PLACEHOLDER = re.compile(r"\[([A-Z][A-Z0-9 ,.'/&-]+)\]")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHANNEL_LABEL = {"facebook": "Facebook comment", "instagram": "Instagram comment", "messenger": "Messenger message",
                 "tiktok": "TikTok comment", "linkedin": "LinkedIn comment", "threads": "Threads comment",
                 "email": "email", "website": "website form", "other": "message"}
MAX_SORT_ATTEMPTS = 3
OPEN_LEAD = ("active", "queued", "manual")

# How much the page loads on first open; later refreshes only fetch changes.
LOAD = {"messages": 400, "tickets": 600, "leads": 2000, "emails": 150}

# Ticket notes. Files live in Firestore next to everything else, split into
# parts because one Firestore document holds at most 1 MiB.
NOTE_CHARS = 5000
NOTE_FILES = 5
NOTE_BYTES = 10 * 1024 * 1024  # all of one note's files together
FILE_PART = 700_000


class DeskError(ValueError):
    """A request the desk refuses; the text is shown to staff as is."""


class NotFound(LookupError):
    pass


@dataclass
class Outgoing:
    journey: str
    step: int
    to: str
    subject: str
    body: str
    marketing: bool
    ticket_id: int | None = None
    lead_id: int | None = None


def code(ticket_id: int) -> str:
    return f"{config.TICKET_PREFIX}-{1000 + ticket_id}"


def minutes(n: int) -> str:
    if n < 60:
        return f"{n}m"
    if n < 1440:
        return f"{n // 60}h" + (f" {n % 60}m" if n % 60 else "")
    return f"{n // 1440}d"


def fill(template: str, values: dict) -> str:
    def swap(match):
        return str(values.get(match.group(1), match.group(0)))
    return re.sub(r"\{(\w+)\}", swap, template)


def placeholders(steps: list[dict]) -> list[str]:
    found = []
    for step in steps:
        for text in (step["subject"], step["body"]):
            found += [m for m in PLACEHOLDER.findall(text) if m not in found]
    return found


def first_name(name: str) -> str:
    part = (name or "").strip().split(" ")[0]
    return part if part and part.lower() != "unknown" else "there"


class Desk:
    def __init__(self, store, *, now: Callable[[], int] | None = None,
                 sort: Callable[..., sorter.Sorting] | None = None,
                 send: Callable[..., str] | None = None,
                 mail_ready: Callable[[], bool] | None = None,
                 public_url: str | None = None):
        self.db = store
        self.now = now or (lambda: int(time.time() * 1000))
        self.sort = sort or sorter.sort_message
        self.send = send or mailer.send
        self.mail_ready = mail_ready or mailer.configured
        self.public_url = (public_url if public_url is not None else os.environ.get("DESK_PUBLIC_URL", "")).rstrip("/")

    # ------------------------------------------------------------ helpers

    def _one(self, coll: str, doc_id: int) -> dict:
        doc = self.db.get(coll, doc_id)
        if not doc:
            raise NotFound(f"{coll[:-1].capitalize()} {doc_id} not found")
        return doc

    def _event(self, label: str, detail: str = "", *, ticket_id=None, lead_id=None) -> None:
        self.db.insert("events", {"at": self.now(), "label": label, "detail": detail,
                                  "ticket_id": ticket_id, "lead_id": lead_id})

    def journey(self, key: str) -> dict:
        base = config.JOURNEYS[key]
        saved = self.db.get("journeys", key) or {}
        steps = saved.get("steps") or [dict(s) for s in base["steps"]]
        return {**{k: v for k, v in base.items() if k != "steps"}, "key": key,
                "status": saved.get("status") or base["default_status"],
                "steps": steps, "placeholders": placeholders(steps)}

    def _save_journey(self, key: str, **fields) -> None:
        current = self.db.get("journeys", key) or {}
        self.db.put("journeys", key, {"status": current.get("status") or config.JOURNEYS[key]["default_status"],
                                      "steps": current.get("steps"), **fields})

    def _ticket_email(self, key: str, ticket: dict, out: list[Outgoing]) -> None:
        if not ticket.get("email"):
            return
        j = self.journey(key)
        if j["status"] != "live":
            return
        values = {"first_name": first_name(ticket["customer"]), "ticket": code(ticket["id"]),
                  "team": ticket["team"].lower(), "sign_off": config.SIGN_OFF}
        step = j["steps"][0]
        out.append(Outgoing(key, 0, ticket["email"], fill(step["subject"], values), fill(step["body"], values),
                            marketing=False, ticket_id=ticket["id"]))

    def _lead_email(self, lead: dict, out: list[Outgoing]) -> bool:
        key = "oldlead" if lead["kind"] == "old" else "newlead"
        j = self.journey(key)
        if j["status"] != "live" or lead["step"] >= len(j["steps"]):
            return False
        step = j["steps"][lead["step"]]
        values = {"first_name": first_name(lead["name"]), "interest": lead["interest"].lower(),
                  "sign_off": config.SIGN_OFF}
        out.append(Outgoing(key, lead["step"], lead["email"], fill(step["subject"], values),
                            fill(step["body"], values), marketing=True, lead_id=lead["id"]))
        return True

    def unsubscribe_url(self, email: str) -> str | None:
        if not self.public_url:
            return None
        return f"{self.public_url}/unsubscribe?t={quote(auth.sign(email.lower(), 'unsubscribe'))}"

    def _blocked(self, email: str) -> bool:
        return self.db.get("suppressions", email.lower()) is not None

    def _flush(self, out: list[Outgoing]) -> None:
        """Send what a change queued, then record each result."""
        if not out:
            return
        ready = self.mail_ready()
        for o in out:
            status, error, provider_id = "sent", None, None
            if o.marketing and self._blocked(o.to):
                status, error = "not_sent", "Unsubscribed"
            elif not ready:
                status, error = "not_sent", "The email service is not set up"
            else:
                footer = (f"You are getting this because you contacted {config.BRAND}."
                          if o.marketing else f"This is about a message you sent {config.BRAND}.")
                try:
                    provider_id = self.send(o.to, o.subject, o.body, footer,
                                            self.unsubscribe_url(o.to) if o.marketing else None)
                except mailer.MailError as exc:
                    status, error = "failed", str(exc)
                except Exception as exc:  # noqa: BLE001 - one bad send must not lose the record
                    log.exception("email send failed")
                    status, error = "failed", f"Sending failed: {exc}"
            self.db.insert("emails", {
                "at": self.now(), "journey": o.journey, "step": o.step, "to_addr": o.to, "subject": o.subject,
                "body": o.body, "status": status, "error": error, "provider_id": provider_id,
                "ticket_id": o.ticket_id, "lead_id": o.lead_id})
            label = "Email sent" if status == "sent" else "Email not sent"
            detail = o.subject if status == "sent" else f"{o.subject} ({error})"
            self._event(label, detail, ticket_id=o.ticket_id, lead_id=o.lead_id)

    # ------------------------------------------------------------ intake

    def ingest(self, incoming: Incoming) -> dict:
        """Store one message, then sort and route it. Repeats of the same message are ignored."""
        now = self.now()
        if incoming.external_id:
            dup = self.db.where("messages", "external_id", "==", incoming.external_id, limit=1)
        elif incoming.text:
            # No id from GHL: the same person sending the same words within a day is a retry.
            same = self.db.where("messages", "text", "==", incoming.text)
            dup = [m for m in same if m["channel"] == incoming.channel and m["received_at"] > now - 24 * 60 * MIN
                   and ((m.get("contact_id") == incoming.contact_id) if incoming.contact_id
                        else m["author"] == incoming.author)]
        else:
            dup = []
        if dup:
            return {"id": dup[0]["id"], "duplicate": True}
        message_id = self.db.insert("messages", {
            "source": incoming.source, "channel": incoming.channel, "external_id": incoming.external_id,
            "author": incoming.author, "email": incoming.email, "contact_id": incoming.contact_id,
            "location_id": incoming.location_id, "text": incoming.text, "subject": incoming.subject,
            "post_url": incoming.post_url, "received_at": now,
            "sort_status": "pending" if incoming.text else "unparsed", "sort_attempts": 0, "sort_error": None,
            # When tick() should sort it again if the sort below never finishes.
            "retry_at": now + 2 * MIN if incoming.text else None,
            "sorted_by": None, "category": None, "intent": None, "priority": None, "confidence": None,
            "summary": None, "suggested_reply": None, "interest": None, "route": None, "ticket_id": None,
            "lead_id": None, "replied_at": None, "replied_by": None, "raw": incoming.raw})
        if incoming.text:
            self.process(message_id)
        return {"id": message_id, "duplicate": False}

    def process(self, message_id: int) -> None:
        """Sort a stored message and route it. A failed sort is kept and retried by tick()."""
        msg = self._one("messages", message_id)
        try:
            s = self.sort(msg["text"], msg["channel"], msg["author"], msg.get("subject"))
        except Exception as exc:  # noqa: BLE001 - the message must survive any sorting failure
            if not isinstance(exc, sorter.SortError):
                log.exception("sorting message %s failed", message_id)
            attempts = msg["sort_attempts"] + 1
            self.db.update("messages", message_id, {
                "sort_status": "failed", "sort_attempts": attempts,
                "retry_at": self.now() + MIN if attempts < MAX_SORT_ATTEMPTS else None,
                "sort_error": str(exc) if isinstance(exc, sorter.SortError) else "Sorting failed. It will be retried."})
            return

        out: list[Outgoing] = []
        self.db.update("messages", message_id, {
            "sort_status": "sorted", "sort_attempts": msg["sort_attempts"] + 1, "sort_error": None, "retry_at": None,
            "sorted_by": s.sorted_by, "category": s.category, "intent": s.intent, "priority": s.priority,
            "confidence": s.confidence, "summary": s.summary, "suggested_reply": s.suggested_reply,
            "interest": s.interest})
        msg = self._one("messages", message_id)

        # Anyone in a lead journey who writes in again has replied: stop emailing them.
        existing = None
        if msg.get("email"):
            for lead in self.db.where("leads", "email", "==", msg["email"]):
                if lead["status"] not in OPEN_LEAD:
                    continue
                self.db.update("leads", lead["id"], {"status": "replied", "next_at": None})
                self._event("Replied", f"Wrote in again by {CHANNEL_LABEL[msg['channel']]}, so the journey stopped",
                            lead_id=lead["id"])
                existing = lead["id"]

        if s.intent == "sales":
            if existing:
                self.db.update("messages", message_id, {"route": "lead", "lead_id": existing})
            else:
                self._create_lead_from(msg)
        elif s.intent == "service":
            self._create_ticket(msg, out)
        else:
            self.db.update("messages", message_id, {"route": "logged"})
        self._flush(out)

    # ------------------------------------------------------------ tickets

    def _create_ticket(self, msg: dict, out: list[Outgoing]) -> int:
        category = msg["category"] if msg["intent"] == "service" else "General question"
        priority = msg["priority"] if msg["intent"] == "service" and msg.get("priority") else "Normal"
        team = config.TEAMS.get(category, config.DEFAULT_TEAM)
        ticket_id = self.db.insert("tickets", {
            "message_id": msg["id"], "customer": msg["author"], "email": msg.get("email"), "channel": msg["channel"],
            "category": category, "priority": priority, "team": team, "owner": None, "status": "Open",
            "created_at": self.now(), "first_reply_at": None, "resolved_at": None, "escalated_at": None,
            # When tick() escalates it if nobody has replied; cleared by the first reply.
            "escalate_at": self.now() + config.SLA[priority]["first"] * MIN})
        self._event("Ticket created", f"From a {CHANNEL_LABEL[msg['channel']]}", ticket_id=ticket_id)
        self._event("Assigned", f"To {team}. First reply due within {minutes(config.SLA[priority]['first'])}",
                    ticket_id=ticket_id)
        self.db.update("messages", msg["id"], {"route": "ticket", "ticket_id": ticket_id})
        self._ticket_email("received", self._one("tickets", ticket_id), out)
        return ticket_id

    def ticket_action(self, ticket_id: int, action: str, user: str) -> None:
        out: list[Outgoing] = []
        now = self.now()
        t = self._one("tickets", ticket_id)
        if action == "assign":
            self.db.update("tickets", ticket_id, {"owner": user})
            self._event("Assigned", f"To {user}", ticket_id=ticket_id)
        elif action == "first_reply":
            if t["first_reply_at"]:
                raise DeskError("The first reply is already recorded.")
            self._mark_first_reply(t, user, now)
        elif action == "progress":
            if t["status"] != "Open":
                raise DeskError(f"The ticket is already {t['status'].lower()}.")
            self.db.update("tickets", ticket_id, {"status": "In progress", "owner": t["owner"] or user})
            self._event("Moved to In progress", f"By {user}", ticket_id=ticket_id)
            self._ticket_email("update", self._one("tickets", ticket_id), out)
        elif action == "resolve":
            if t["status"] == "Resolved":
                raise DeskError("The ticket is already resolved.")
            if not t["first_reply_at"]:
                self._mark_first_reply(t, user, now)
            self.db.update("tickets", ticket_id, {"status": "Resolved", "resolved_at": now, "owner": t["owner"] or user})
            self._event("Resolved", f"By {user} in {minutes((now - t['created_at']) // MIN)}", ticket_id=ticket_id)
            self._ticket_email("resolved", self._one("tickets", ticket_id), out)
        elif action == "reopen":
            if t["status"] != "Resolved":
                raise DeskError("Only a resolved ticket can be reopened.")
            self.db.update("tickets", ticket_id, {"status": "In progress", "resolved_at": None})
            self._event("Reopened", f"By {user}", ticket_id=ticket_id)
        else:
            raise DeskError(f"Unknown ticket action: {action}")
        self._flush(out)

    def _mark_first_reply(self, t: dict, user: str, now: int) -> None:
        self.db.update("tickets", t["id"], {"first_reply_at": now, "owner": t["owner"] or user, "escalate_at": None})
        self._event("First reply sent", f"By {user}, {minutes((now - t['created_at']) // MIN)} after it came in",
                    ticket_id=t["id"])

    def add_note(self, ticket_id: int, user: str, text: str, files: list[tuple[str, str, bytes]]) -> int:
        """A note for the team on an open ticket, with optional files as (name, type, data)."""
        t = self._one("tickets", ticket_id)
        if t["status"] == "Resolved":
            raise DeskError("Reopen the ticket to add notes or files.")
        text = text.strip()
        if not text and not files:
            raise DeskError("Write a note or attach a file first.")
        if len(text) > NOTE_CHARS:
            raise DeskError(f"Keep the note under {NOTE_CHARS:,} characters.")
        if len(files) > NOTE_FILES:
            raise DeskError(f"Attach up to {NOTE_FILES} files per note.")
        if sum(len(data) for _, _, data in files) > NOTE_BYTES:
            raise DeskError(f"Attach up to {NOTE_BYTES // 1024 // 1024} MB per note.")
        for name, _, data in files:
            if not data:
                raise DeskError(f"{name or 'A file'} is empty.")
        now = self.now()
        saved = []
        for name, ctype, data in files:
            name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip()[:200] or "file"
            parts = (len(data) + FILE_PART - 1) // FILE_PART
            file_id = self.db.insert("files", {"ticket_id": ticket_id, "name": name, "type": ctype[:100],
                                               "size": len(data), "parts": parts, "by": user, "at": now})
            for k in range(parts):
                self.db.put("file_parts", f"{file_id}-{k}", {"file_id": file_id,
                                                             "data": data[k * FILE_PART:(k + 1) * FILE_PART]})
            saved.append({"id": file_id, "name": name, "type": ctype[:100], "size": len(data)})
        note_id = self.db.insert("notes", {"ticket_id": ticket_id, "by": user, "at": now, "text": text,
                                           "files": saved})
        detail = f"By {user}" + (f", {len(saved)} file{'s' if len(saved) > 1 else ''} attached" if saved else "")
        self._event("Note added", detail, ticket_id=ticket_id)
        return note_id

    def file(self, file_id: int) -> tuple[dict, bytes]:
        meta = self._one("files", file_id)
        parts = [self.db.get("file_parts", f"{file_id}-{k}") for k in range(meta["parts"])]
        if not all(parts):
            raise NotFound(f"File {file_id} is incomplete")
        return meta, b"".join(bytes(p["data"]) for p in parts)

    # ------------------------------------------------------------ messages

    def message_action(self, message_id: int, action: str, user: str) -> None:
        msg = self._one("messages", message_id)
        if action == "resort":
            if not msg["text"]:
                raise DeskError("This message has no text to sort.")
            if msg["route"]:
                raise DeskError("This message is already routed.")
            self.db.update("messages", message_id, {"sort_status": "pending"})
            self.process(message_id)
            return
        out: list[Outgoing] = []
        if action == "replied":
            if msg["replied_at"]:
                raise DeskError("This reply is already marked as posted.")
            now = self.now()
            self.db.update("messages", message_id, {"replied_at": now, "replied_by": user})
            if msg["ticket_id"]:
                t = self._one("tickets", msg["ticket_id"])
                if not t["first_reply_at"]:
                    self._mark_first_reply(t, user, now)
        elif action == "ticket":
            if msg["ticket_id"]:
                raise DeskError("This message already has a ticket.")
            if msg["sort_status"] != "sorted":
                raise DeskError("Sort the message before making it a ticket.")
            self._create_ticket(msg, out)
        elif action == "lead":
            if msg["lead_id"]:
                raise DeskError("This message is already a lead.")
            if msg["sort_status"] != "sorted":
                raise DeskError("Sort the message before making it a lead.")
            self._create_lead_from(msg)
        else:
            raise DeskError(f"Unknown message action: {action}")
        self._flush(out)

    # ------------------------------------------------------------ leads

    def _create_lead_from(self, msg: dict) -> int:
        lead_id = self._create_lead(kind="new", name=msg["author"], email=msg.get("email"),
                                    source=CHANNEL_LABEL[msg["channel"]].capitalize(),
                                    interest=msg.get("interest") or "Other", message_id=msg["id"])
        self.db.update("messages", msg["id"], {"route": "lead", "lead_id": lead_id})
        return lead_id

    def _create_lead(self, *, kind: str, name: str, email: str | None, source: str, interest: str,
                     message_id: int | None = None, last_contact_days: int | None = None,
                     start_at: int | None = None) -> int:
        live = self.journey("oldlead" if kind == "old" else "newlead")["status"] == "live"
        if not email:
            status, next_at = "manual", None
        elif live:
            status, next_at = "active", start_at or self.now()
        else:
            status, next_at = "queued", None
        lead_id = self.db.insert("leads", {
            "message_id": message_id, "kind": kind, "name": name, "email": email.lower() if email else None,
            "source": source, "interest": interest, "status": status, "step": 0, "next_at": next_at,
            "created_at": self.now(), "last_contact_days": last_contact_days})
        detail = f"From {source}, interested in {interest.lower()}"
        if status == "manual":
            detail += ". No email on file, so follow up by direct message"
        elif status == "queued":
            detail += ". Queued until the journey is approved"
        self._event("Lead created" if kind == "new" else "Imported", detail, lead_id=lead_id)
        return lead_id

    def lead_action(self, lead_id: int, status: str, user: str) -> None:
        labels = {"replied": "Marked replied", "won": "Marked won", "closed": "Closed"}
        if status not in labels:
            raise DeskError(f"Unknown lead status: {status}")
        lead = self._one("leads", lead_id)
        if lead["status"] == status:
            raise DeskError(f"The lead is already {status}.")
        self.db.update("leads", lead_id, {"status": status, "next_at": None})
        self._event(labels[status], f"By {user}. No more journey emails", lead_id=lead_id)

    def check_import(self, rows: list[dict]) -> list[dict]:
        """One verdict per row: whether it can be enrolled, and why not."""
        emails = {str(r.get("email") or "").strip().lower() for r in rows[:5000]}
        emails.discard("")
        known: set[str] = set()
        blocked: set[str] = set()
        for group in chunks(emails):
            known |= {l["email"] for l in self.db.where("leads", "email", "in", group)}
            blocked |= {e for e in group if self._blocked(e)}
        seen: set[str] = set()
        verdicts = []
        for row in rows[:5000]:
            name = str(row.get("name") or "").strip()
            email = str(row.get("email") or "").strip().lower()
            consent = str(row.get("consent") or "").strip().lower() in ("yes", "y", "true", "1", "opted in")
            days = row.get("last_contact_days")
            try:
                days = int(float(days)) if days not in (None, "") else None
            except (TypeError, ValueError):
                days = None
            interest = str(row.get("interest") or "").strip()
            interest = next((i for i in config.INTERESTS if i.lower() == interest.lower()), interest or "Other")
            reason = None
            if not name:
                reason = "No name"
            elif not EMAIL_RE.match(email):
                reason = "No valid email"
            elif not consent:
                reason = "No email consent"
            elif email in blocked:
                reason = "Unsubscribed"
            elif email in known or email in seen:
                reason = "Already a lead"
            elif days is not None and days < 90:
                reason = "Contacted in the last 90 days"
            seen.add(email)
            verdicts.append({"name": name, "email": email, "interest": interest, "last_contact_days": days,
                             "ok": reason is None, "reason": reason})
        return verdicts

    def import_leads(self, rows: list[dict], user: str) -> dict:
        verdicts = self.check_import(rows)
        enrolled = 0
        for k, v in enumerate(x for x in verdicts if x["ok"]):
            # Spread the first emails out so a big list does not land in one burst.
            self._create_lead(kind="old", name=v["name"], email=v["email"], source="Imported list",
                              interest=v["interest"], last_contact_days=v["last_contact_days"],
                              start_at=self.now() + k * 2 * MIN)
            enrolled += 1
        log.info("%s imported %s old leads", user, enrolled)
        return {"enrolled": enrolled, "skipped": len(verdicts) - enrolled}

    # ------------------------------------------------------------ journeys

    def set_journey_status(self, key: str, status: str, user: str) -> int:
        if key not in config.JOURNEYS or status not in ("live", "paused"):
            raise DeskError("Unknown journey or status.")
        j = self.journey(key)
        if status == "live" and j["placeholders"]:
            raise DeskError("Fill in " + ", ".join(f"[{p}]" for p in j["placeholders"]) + " before this goes live.")
        self._save_journey(key, status=status)
        released = 0
        if status == "live" and j["group"] == "leads":
            kind = "old" if key == "oldlead" else "new"
            queued = sorted((l for l in self.db.where("leads", "status", "==", "queued") if l["kind"] == kind),
                            key=lambda l: l["id"])
            for k, lead in enumerate(queued):
                self.db.update("leads", lead["id"], {"status": "active", "next_at": self.now() + k * 2 * MIN})
                self._event("Released", f"{j['name']} approved by {user}", lead_id=lead["id"])
            released = len(queued)
        log.info("%s set %s to %s", user, key, status)
        return released

    def save_journey_step(self, key: str, index: int, subject: str, body: str) -> None:
        if key not in config.JOURNEYS:
            raise DeskError("Unknown journey.")
        subject, body = subject.strip(), body.strip()
        if not subject or not body:
            raise DeskError("An email needs a subject and a body.")
        j = self.journey(key)
        if not 0 <= index < len(j["steps"]):
            raise DeskError("Unknown step.")
        steps = [dict(s) for s in j["steps"]]
        steps[index].update(subject=subject[:300], body=body[:10_000])
        fields = {"steps": steps}
        if j["status"] == "live" and placeholders(steps):
            fields["status"] = "pending"  # never send a half-filled email
        self._save_journey(key, **fields)

    def unsubscribe(self, email: str) -> None:
        email = email.lower()
        if not self._blocked(email):
            self.db.put("suppressions", email, {"email": email, "at": self.now(), "reason": "unsubscribed"})
        for lead in self.db.where("leads", "email", "==", email):
            if lead["status"] in ("active", "queued"):
                self.db.update("leads", lead["id"], {"status": "unsubscribed", "next_at": None})
                self._event("Unsubscribed", "From the link in an email", lead_id=lead["id"])

    # ------------------------------------------------------------ the clock

    def tick(self) -> dict:
        """Escalate late tickets, retry failed sorts and send due journey emails."""
        now = self.now()
        escalated = 0
        # Each query reads only records that are due, so a quiet desk costs a few reads.
        for t in self.db.where("tickets", "escalate_at", "<=", now):
            if t["first_reply_at"] or t["escalated_at"] or t["status"] == "Resolved":
                self.db.update("tickets", t["id"], {"escalate_at": None})
                continue
            target = config.SLA[t["priority"]]["first"]
            self.db.update("tickets", t["id"], {"escalated_at": now, "escalate_at": None})
            self._event("Escalated", f"The first reply target of {minutes(target)} passed", ticket_id=t["id"])
            escalated += 1

        retry = [m["id"] for m in self.db.where("messages", "retry_at", "<=", now, limit=10)]
        for message_id in retry:
            self.process(message_id)

        sent = 0
        if self.mail_ready():  # without an email service, journeys wait rather than skip emails
            out: list[Outgoing] = []
            due = [l for l in self.db.where("leads", "next_at", "<=", now, order="next_at", limit=50)
                   if l["status"] == "active"]
            for lead in due:
                if not self._lead_email(lead, out):
                    continue
                key = "oldlead" if lead["kind"] == "old" else "newlead"
                steps = self.journey(key)["steps"]
                step = lead["step"] + 1
                if step < len(steps):
                    gap = steps[step]["delay"] - steps[lead["step"]]["delay"]
                    self.db.update("leads", lead["id"], {"step": step, "next_at": max(now, lead["next_at"] + gap)})
                else:
                    self.db.update("leads", lead["id"], {
                        "step": step, "next_at": None, "status": "closed" if lead["kind"] == "old" else "completed"})
                    self._event("Journey finished", "All emails sent with no reply", lead_id=lead["id"])
                sent += 1
            self._flush(out)
        return {"escalated": escalated, "resorted": len(retry), "journey_emails": sent}

    # ------------------------------------------------------------ the page's view

    def state(self, since: int = 0) -> dict:
        """Everything the page shows. With `since`, only records changed after it."""
        if since:
            changed = lambda coll: self.db.where(coll, "updated_at", ">", since)  # noqa: E731
            msgs, tix, lds = changed("messages"), changed("tickets"), changed("leads")
            evs, sent, notes = changed("events"), changed("emails"), changed("notes")
            # Templates rarely change: re-send them only when one did.
            journeys_changed = bool(changed("journeys"))
        else:
            msgs = self.db.recent("messages", "received_at", LOAD["messages"])
            tix = self.db.recent("tickets", "created_at", LOAD["tickets"])
            lds = self.db.recent("leads", "created_at", LOAD["leads"])
            sent = self.db.recent("emails", "at", LOAD["emails"])
            evs = []
            for coll, ids in (("ticket_id", [t["id"] for t in tix]), ("lead_id", [l["id"] for l in lds])):
                for group in chunks(ids):
                    evs += self.db.where("events", coll, "in", group)
            notes = []
            for group in chunks([t["id"] for t in tix]):
                notes += self.db.where("notes", "ticket_id", "in", group)
            journeys_changed = True
        for m in msgs:
            if m["sort_status"] != "unparsed":
                m["raw"] = None  # only needed to debug a webhook that could not be read
        for t in tix:
            t["code"] = code(t["id"])
        location = next((m["location_id"] for m in msgs if m.get("location_id")), None)
        return {
            "now": self.now(),
            "full": not since,
            "brand": config.BRAND,
            "messages": msgs, "tickets": tix, "leads": lds, "events": evs, "emails": sent, "notes": notes,
            "journeys": [self.journey(key) for key in config.JOURNEYS] if journeys_changed else None,
            "config": {
                "categories": {k: v["intent"] for k, v in config.CATEGORIES.items()},
                "channels": config.CHANNELS, "sla": config.SLA, "interests": config.INTERESTS,
                "ticket_prefix": config.TICKET_PREFIX, "sign_off": config.SIGN_OFF,
            },
            "status": {
                "sorting": "claude" if os.environ.get("ANTHROPIC_API_KEY") else "rules",
                "email": self.mail_ready(),
                "webhook": bool(os.environ.get("DESK_WEBHOOK_KEY")),
                "ghl_location": location,
            },
        }
