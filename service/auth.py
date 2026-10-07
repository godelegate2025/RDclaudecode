"""Team sign-in: "Sign in with Google", limited to the team.

Google holds the accounts (ordinary Gmail works; no Workspace needed). Who is
on the team is decided in team.py: owners from the ALLOWED_EMAILS setting plus
the members managed on the admin page. A signed cookie keeps someone signed in.

Settings (all three switch sign-in on; none of them leaves the app public):
  GOOGLE_CLIENT_ID  OAuth client ID of type "Web application"
  ALLOWED_EMAILS    the owners, comma-separated, e.g. "you@gmail.com"; everyone
                    else is added on the admin page (/admin/team, see team.py)
  SESSION_SECRET    a long random string that signs the session cookie

With none set, sign-in is off and the app behaves as before. With only some
set, it fails closed: every protected page says sign-in is misconfigured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import quote

from .team import get_team

log = logging.getLogger("auth")

COOKIE = "rd_session"
SESSION_DAYS = 14
SETTINGS = ("GOOGLE_CLIENT_ID", "ALLOWED_EMAILS", "SESSION_SECRET")

# Reachable without signing in: the login flow itself, health checks, and the
# brand assets the login page shows.
PUBLIC_PREFIXES = ("/login", "/auth/", "/static/", "/healthz", "/favicon.ico", "/sw.js")


class NotAllowed(Exception):
    """The Google account is real but not on the list; the message is safe to show."""


@dataclass
class Config:
    client_id: str
    secret: bytes


def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def mode() -> str:
    """'off' (none set), 'on' (all set) or 'broken' (some set) — broken fails closed."""
    present = [bool(_setting(name)) for name in SETTINGS]
    if not any(present):
        return "off"
    return "on" if all(present) else "broken"


def config() -> Config:
    return Config(
        client_id=_setting("GOOGLE_CLIENT_ID"),
        secret=_setting("SESSION_SECRET").encode(),
    )


def missing_settings() -> list[str]:
    return [name for name in SETTINGS if not _setting(name)]


def is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PREFIXES)


# ------------------------------------------------------------------ cookie

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(email: str, secret: bytes, now: float | None = None) -> str:
    payload = _b64(json.dumps({"email": email, "exp": int((now or time.time()) + SESSION_DAYS * 86400)}).encode())
    signature = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_session(value: str | None, cfg: Config, now: float | None = None) -> str | None:
    """The signed-in email, or None for a missing, forged, expired or no-longer-allowed session."""
    if not value or "." not in value:
        return None
    payload, signature = value.rsplit(".", 1)
    expected = _b64(hmac.new(cfg.secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now or time.time()):
        return None
    email = str(data.get("email", "")).lower()
    # Re-checked on every request, so removing someone from the team signs
    # them out at once rather than when their cookie expires.
    return email if get_team().is_allowed(email) else None


# ------------------------------------------------------------------ Google

def verify_google_credential(credential: str, cfg: Config) -> str:
    """Check Google's signed ID token and the team list; return the email."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(credential, google_requests.Request(), cfg.client_id)
    except ValueError as exc:  # bad signature, wrong audience, expired
        raise NotAllowed("Google sign-in could not be verified. Try again.") from exc
    email = str(claims.get("email", "")).lower()
    if not email or not claims.get("email_verified"):
        raise NotAllowed("That Google account has no verified email address.")
    if not get_team().is_allowed(email):
        log.info("sign-in refused for %s", email)
        raise NotAllowed(f"{email} isn't on the Redefine team list. Ask the admin to add it.")
    return email


def safe_next(target: str | None) -> str:
    """Only same-site paths: never let ?next= send someone to another site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def login_url(path: str) -> str:
    return "/login?next=" + quote(safe_next(path), safe="/")
