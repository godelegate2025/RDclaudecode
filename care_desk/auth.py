"""Signed tokens for unsubscribe links.

Staff sign-in is the Redefine App's own (service/auth.py): Google accounts on
the team list, the same SESSION_SECRET and the same cookie format. This module
only signs values that leave the app, like the email in an unsubscribe link,
with that same secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger("care-desk")

_dev_secret: bytes | None = None


def secret() -> bytes:
    value = os.environ.get("SESSION_SECRET", "").strip()
    if value:
        return value.encode()
    global _dev_secret
    if _dev_secret is None:
        # Only reachable with sign-in off, which the desk allows on a laptop alone.
        log.warning("SESSION_SECRET is not set: unsubscribe links will stop working on restart")
        _dev_secret = secrets.token_bytes(32)
    return _dev_secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(value: str, purpose: str) -> str:
    """value + signature; `purpose` stops a token minted for one use working for another."""
    mac = hmac.new(secret(), f"desk:{purpose}:{value}".encode(), hashlib.sha256).digest()
    return f"{_b64(value.encode())}.{_b64(mac)}"


def unsign(token: str, purpose: str) -> str | None:
    try:
        body, mac = token.split(".")
        value = _unb64(body).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(secret(), f"desk:{purpose}:{value}".encode(), hashlib.sha256).digest()
    return value if hmac.compare_digest(_b64(expected), mac) else None
