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
from html import escape

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


def site_for(request) -> str:
    """The address of the site this request came to (Cloud Run only routes its own hosts here)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def logo_for(request) -> str:
    return site_for(request) + "/static/logo.png"


def _digest(email: str, code: str, secret: bytes) -> str:
    return hmac.new(secret, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL.match(email) or len(email) > 254:
        raise LoginError("Enter a valid email address.")
    return email


def request_code(email: str, secret: bytes, *, app: str = "Redefine App", logo_url: str | None = None,
                 allowed=None, send=None, now: float | None = None) -> None:
    """Email a code if this address is on the team (or passes `allowed`). Says nothing either way."""
    email = clean_email(email)
    if not (allowed or get_team().is_allowed)(email):
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
        (send or _send)(email, code, app=app, logo_url=logo_url)
    except LoginError:
        store().put(email, record)  # nothing went out, so an immediate retry may send
        raise
    log.info("sign-in code sent to %s", email)


def verify_code(email: str, code: str, secret: bytes, *, allowed=None,
                refused: str = "{email} isn't on the Redefine team list. Ask the admin to add it.",
                now: float | None = None) -> str:
    """The email, once its code is right and it is still on the team (or passes `allowed`)."""
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
    if not (allowed or get_team().is_allowed)(email):
        raise LoginError(refused.format(email=email))
    return email


FONT = "font-family:Arial,Helvetica,sans-serif"
DIVIDER = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
           '<td style="padding-top:24px;border-bottom:1px solid #E6E3DF;font-size:0;line-height:0">&nbsp;</td></tr></table>')


def heading(text: str) -> str:
    return f'<p style="margin:0 0 8px;{FONT};font-size:22px;line-height:28px;font-weight:700;color:#191616">{text}</p>'


def para(html: str, *, small: bool = False, margin: str = "0 0 16px") -> str:
    size, color = ("13px;line-height:20px", "#7C7773") if small else ("15px;line-height:23px", "#4A4545")
    return f'<p style="margin:{margin};{FONT};font-size:{size};color:{color}">{html}</p>'


def button(label: str, url: str) -> str:
    """A link styled as a button, built so Outlook keeps the shape."""
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 22px"><tr>'
            '<td style="background:#FC4452;border-radius:10px">'
            f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:14px 26px;{FONT};font-size:15px;'
            f'line-height:18px;font-weight:700;color:#191616;text-decoration:none">{escape(label)}</a></td></tr></table>')


def layout(title: str, preheader: str, body: str, footer: str, logo_url: str | None) -> str:
    """Redefine's email frame: logo and wordmark, a white card with the coral rule, a footer.

    Tables and inline styles only, because that is all email programs agree
    on. The logo is a PNG on the app's public /static/ path: email programs
    block SVG and most strip embedded images. `body` and `footer` are HTML.
    """
    logo = (f'<td style="padding-right:10px;vertical-align:middle"><img src="{escape(logo_url, quote=True)}" '
            'width="36" height="36" alt="" style="display:block;border:0;width:36px;height:36px"></td>' if logo_url else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#F4F3F0">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#F4F3F0">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F4F3F0">
<tr><td align="center" style="padding:36px 16px 40px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:480px">
  <tr><td style="padding:0 4px 18px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      {logo}<td style="vertical-align:middle;{FONT};font-size:20px;font-weight:900;letter-spacing:1px;color:#191616">REDEFINE</td>
    </tr></table>
  </td></tr>
  <tr><td style="background:#FFFFFF;border:1px solid #E6E3DF;border-top:4px solid #FC4452;border-radius:12px;padding:32px 32px 28px">
    {body}
  </td></tr>
  <tr><td align="center" style="padding:22px 16px 0;{FONT};font-size:12px;line-height:18px;color:#8A8580">{footer}</td></tr>
  </table>
</td></tr></table>
</body></html>"""


def message(email: str, code: str, app: str, logo_url: str | None) -> tuple[str, str, str]:
    """Subject, plain text and HTML of the code email."""
    minutes = CODE_SECONDS // 60
    subject = f"{code} is your Redefine sign-in code"
    text = (f"Your Redefine sign-in code\n\n{code}\n\n"
            f"Enter this code on the {app} sign-in page to sign in as {email}. "
            f"It expires in {minutes} minutes and can be used once.\n\n"
            "Didn't try to sign in? You can ignore this email. Nobody can sign in without this code.\n\n"
            f"Redefine Agency\nYou're getting this because someone asked to sign in to the {app} with this address.")
    body = (heading("Your sign-in code")
            + para(f'Enter this code on the {escape(app)} sign-in page to sign in as '
                   f'<strong style="color:#191616">{escape(email)}</strong>.', margin="0 0 24px")
            + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
              f'<td align="center" style="background:#F4F3F0;border-radius:10px;padding:20px 12px 20px 22px;{FONT};'
              f'font-size:34px;line-height:38px;font-weight:700;letter-spacing:10px;color:#191616">{code}</td></tr></table>'
            + para(f"It expires in {minutes} minutes and can be used once.", small=True, margin="16px 0 0")
            + DIVIDER
            + para('<strong style="color:#4A4545">Didn\'t try to sign in?</strong> You can ignore this email. '
                   "Nobody can sign in without this code.", small=True, margin="20px 0 0"))
    footer = f"Redefine Agency<br>You're getting this because someone asked to sign in to the {escape(app)} with this address."
    return subject, text, layout("Your Redefine sign-in code", f"Your code is {code}. It expires in {minutes} minutes.",
                                 body, footer, logo_url)


def deliver(to: str, subject: str, text: str, html: str) -> None:
    """Send one email through Resend from the sign-in sender."""
    try:
        response = requests.post(RESEND_URL, timeout=15,
                                 headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}"},
                                 json={"from": sender(), "to": [to], "subject": subject, "text": text, "html": html})
    except requests.RequestException as exc:
        raise LoginError("Could not send the email. Try again in a minute.") from exc
    if response.status_code >= 400:
        log.error("Resend refused an email to %s (HTTP %s): %s", to, response.status_code, response.text[:300])
        raise LoginError("Could not send the email. Try again in a minute.")


def _send(email: str, code: str, *, app: str = "Redefine App", logo_url: str | None = None) -> None:
    deliver(email, *message(email, code, app, logo_url))
