"""Sign in with a one-time code sent by email, for team members without a Google account.

The team list is the same one Google sign-in checks (team.py), and a correct
code ends in the same session cookie (auth.py). The Redefine App and the Care
Desk both use this module.

Settings, on top of the three that switch sign-in on (auth.py):
  RESEND_API_KEY    the Resend key that sends the codes
  LOGIN_FROM_EMAIL  the sender, e.g. "Redefine <login@redefine.ph>", on a domain
                    verified in Resend (DESK_FROM_EMAIL is used if this is unset)
Without them, the login pages show Google only.

Codes are six digits, work once, for ten minutes, and allow five tries. Only a
keyed hash is stored, so reading Firestore does not reveal a live code. Asking
for a code never says whether an email is on the team, so the form cannot be
used to find out who is; members get at most five codes an hour.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time

import requests

from . import firestore_db
from .firestore_db import StoreUnavailable
from .team import get_team

log = logging.getLogger("email-login")

COLLECTION = "login_codes"
CODE_SECONDS = 10 * 60
MAX_TRIES = 5
RESEND_GAP = 30          # a second request within this many seconds sends nothing new
PER_HOUR = 5
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESEND_URL = "https://api.resend.com/emails"


class LoginError(Exception):
    """A request the code sign-in refuses; the message is safe to show."""


class MemoryStore:
    """For tests and local runs without Firestore."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def get(self, email: str) -> dict | None:
        doc = self.docs.get(email)
        return dict(doc) if doc else None

    def put(self, email: str, data: dict) -> None:
        self.docs[email] = dict(data)


class FirestoreStore:
    def get(self, email: str) -> dict | None:
        def read(c):
            snap = c.document(email).get()
            return snap.to_dict() if snap.exists else None
        return firestore_db.call(COLLECTION, read)

    def put(self, email: str, data: dict) -> None:
        firestore_db.call(COLLECTION, lambda c: c.document(email).set(data))


_store = None


def store():
    global _store
    if _store is None:
        _store = FirestoreStore()
    return _store


def use_store(new) -> None:
    """Swap the backing store (tests, local runs)."""
    global _store
    _store = new


def sender() -> str:
    return (os.environ.get("LOGIN_FROM_EMAIL") or os.environ.get("DESK_FROM_EMAIL") or "").strip()


def available() -> bool:
    return bool(os.environ.get("RESEND_API_KEY", "").strip() and sender())


def _digest(email: str, code: str, secret: bytes) -> str:
    return hmac.new(secret, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL.match(email) or len(email) > 254:
        raise LoginError("Enter a valid email address.")
    return email


def request_code(email: str, secret: bytes, *, send=None, now: float | None = None) -> None:
    """Email a code if this address is on the team. Says nothing either way."""
    email = clean_email(email)
    if not get_team().is_allowed(email):
        log.info("code not sent: %s is not on the team", email)
        return
    now = now or time.time()
    try:
        record = store().get(email) or {}
    except StoreUnavailable as exc:
        raise LoginError("Email sign-in is unavailable right now. Use Google, or try again later.") from exc
    sent = [t for t in record.get("sent", []) if t > now - 3600]
    if sent and now - sent[-1] < RESEND_GAP:
        return  # a double click, or a resend asked for too soon: the last code still works
    if len(sent) >= PER_HOUR:
        log.warning("code not sent: %s asked for %s codes in an hour", email, len(sent))
        return
    code = f"{secrets.randbelow(10 ** 6):06d}"
    store().put(email, {"digest": _digest(email, code, secret), "expires": now + CODE_SECONDS,
                        "tries": 0, "sent": sent + [now]})
    try:
        (send or _send)(email, code)
    except LoginError:
        store().put(email, record)  # nothing went out, so an immediate retry may send
        raise
    log.info("sign-in code sent to %s", email)


def verify_code(email: str, code: str, secret: bytes, *, now: float | None = None) -> str:
    """The email, once its code is right and it is still on the team."""
    email = clean_email(email)
    code = re.sub(r"\s", "", code or "")
    now = now or time.time()
    try:
        record = store().get(email)
    except StoreUnavailable as exc:
        raise LoginError("Email sign-in is unavailable right now. Use Google, or try again later.") from exc
    if not record or not record.get("digest") or record.get("expires", 0) < now:
        raise LoginError("That code has expired. Ask for a new one.")
    if record.get("tries", 0) >= MAX_TRIES:
        raise LoginError("Too many wrong codes. Ask for a new one.")
    if not hmac.compare_digest(record["digest"], _digest(email, code, secret)):
        store().put(email, {**record, "tries": record.get("tries", 0) + 1})
        left = MAX_TRIES - record.get("tries", 0) - 1
        raise LoginError("That code isn't right." + (f" {left} tries left." if left else " Ask for a new one."))
    store().put(email, {**record, "digest": None})  # once only; keep the send history for the hourly limit
    if not get_team().is_allowed(email):
        raise LoginError(f"{email} isn't on the Redefine team list. Ask the admin to add it.")
    return email


def _send(email: str, code: str) -> None:
    text = (f"Your Redefine sign-in code is {code}\n\n"
            "It works once, for the next 10 minutes.\n\n"
            "If you didn't try to sign in, ignore this email. Nobody can sign in without the code.")
    html = ('<div style="background:#F4F3F0;padding:24px 12px"><div style="max-width:480px;margin:0 auto;'
            'background:#fff;border-radius:12px;padding:28px 26px;font:15px/1.6 Arial,sans-serif;color:#2C2828">'
            '<p style="margin:0 0 10px">Your Redefine sign-in code is</p>'
            f'<p style="margin:0 0 16px;font:700 32px/1 Arial,sans-serif;letter-spacing:.18em;color:#191616">{code}</p>'
            '<p style="margin:0 0 10px">It works once, for the next 10 minutes.</p>'
            '<p style="margin:0;color:#8a8580;font-size:13px">If you didn\'t try to sign in, ignore this email. '
            'Nobody can sign in without the code.</p></div></div>')
    try:
        response = requests.post(RESEND_URL, timeout=15,
                                 headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}"},
                                 json={"from": sender(), "to": [email], "subject": f"{code} is your Redefine sign-in code",
                                       "text": text, "html": html})
    except requests.RequestException as exc:
        raise LoginError("Could not send the code. Try again in a minute.") from exc
    if response.status_code >= 400:
        log.error("Resend refused a sign-in code (HTTP %s): %s", response.status_code, response.text[:300])
        raise LoginError("Could not send the code. Try again in a minute.")
