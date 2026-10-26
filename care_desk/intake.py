"""Read what a GHL workflow webhook sends and pull out one message.

GHL's Webhook action posts the contact's standard fields plus a `customData`
object whose keys you choose in the workflow. The setup guide asks for
`comment`, `channel`, `post_url` and `comment_id`; anything else is a best
guess, and a payload with no readable text is still stored so staff can see
exactly what GHL sent and fix the mapping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape

from . import config

TEXT_KEYS = ("comment", "comment_text", "message", "message_body", "text", "body", "content")
ID_KEYS = ("comment_id", "message_id", "id")
MAX_RAW = 20_000


@dataclass
class Incoming:
    text: str
    channel: str
    author: str
    source: str = "ghl"
    email: str | None = None
    phone: str | None = None
    subject: str | None = None
    post_url: str | None = None
    contact_id: str | None = None
    location_id: str | None = None
    external_id: str | None = None
    raw: dict | None = field(default=None, repr=False)


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first(mapping: dict, keys) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):  # GHL nests some fields: {"message": {"body": "..."}}
            value = value.get("body") or value.get("text")
        if _str(value):
            return _str(value)
    return ""


def normalise_channel(value: str) -> str:
    v = value.lower()
    for name in ("instagram", "messenger", "tiktok", "linkedin", "threads", "email", "website"):
        if name in v:
            return name
    if "ig" == v or v.startswith("ig_"):
        return "instagram"
    if "facebook" in v or v in ("fb", "fb_comment"):
        return "facebook"
    if "form" in v or "web" in v:
        return "website"
    return "other"


def _small(payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return payload if len(text) <= MAX_RAW else {"truncated": text[:MAX_RAW]}


def parse_ghl(payload: dict) -> Incoming:
    custom = payload.get("customData") or payload.get("custom_data") or {}
    if not isinstance(custom, dict):
        custom = {}

    text = _first(custom, TEXT_KEYS) or _first(payload, TEXT_KEYS)
    channel_hint = _str(custom.get("channel")) or _str(payload.get("channel")) or _str(payload.get("source")) \
        or _str((payload.get("message") or {}).get("type") if isinstance(payload.get("message"), dict) else "")
    full_name = _str(payload.get("full_name")) or " ".join(
        p for p in (_str(payload.get("first_name")), _str(payload.get("last_name"))) if p)
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}

    return Incoming(
        text=text[:5000],
        channel=normalise_channel(channel_hint) if channel_hint else "other",
        author=(_str(custom.get("author")) or full_name or _str(contact.get("name")) or "Unknown")[:200],
        email=(_str(payload.get("email")) or _str(custom.get("email")) or _str(contact.get("email")) or None),
        phone=((_str(payload.get("phone")) or _str(custom.get("phone")) or _str(contact.get("phone")))[:40] or None),
        subject=(_str(custom.get("subject")) or None),
        post_url=(_str(custom.get("post_url")) or None),
        contact_id=(_str(payload.get("contact_id")) or _str(contact.get("id")) or None),
        location_id=(_str(location.get("id")) or _str(payload.get("locationId")) or None),
        external_id=(_first(custom, ID_KEYS) or None),
        raw=_small(payload),
    )


# ------------------------------------------------------------------ email (n8n watching info@)

# Senders that are never a person writing in: bounces, auto-replies, notifications.
AUTO_SENDER = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounces?|notifications?)[@+.]", re.I)
# Where a reply's quoted history starts, in the common mail apps.
QUOTE_START = [
    re.compile(r"^\s*On .{0,300}wrote:\s*$", re.I | re.M),                    # Gmail, Apple Mail
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I | re.M),        # Outlook
    re.compile(r"^\s*From:\s.+\n\s*(Sent|Date):\s", re.I | re.M),            # Outlook, forwarded headers
    re.compile(r"^\s*_{20,}\s*$", re.M),                                      # Outlook web divider
    re.compile(r"^\s*Sent from my (iPhone|iPad|Android|mobile)", re.I | re.M),
]


def new_text_of(body: str) -> str:
    """The part of an email the sender just wrote: quoted history and signatures cut off."""
    text = (body or "").replace("\r\n", "\n")
    cut = min((m.start() for p in QUOTE_START if (m := p.search(text))), default=len(text))
    fresh = "\n".join(line for line in text[:cut].split("\n") if not line.lstrip().startswith(">"))
    fresh = re.sub(r"\n{3,}", "\n\n", fresh).strip()
    return fresh or text.strip()  # a reply that is all quote: keep everything rather than nothing


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))


def parse_email(payload: dict, own_domains: tuple[str, ...] = ()) -> Incoming | None:
    """One email forwarded by n8n, or None for one that isn't from a customer:
    bounces and no-reply senders, and mail from our own domain."""
    sender = _str(payload.get("from_email")).lower()
    if not sender or AUTO_SENDER.match(sender) or sender.rsplit("@", 1)[-1] in own_domains:
        return None
    body = _str(payload.get("text")) or html_to_text(_str(payload.get("html")))
    name = _str(payload.get("from_name")).strip('"\' ') or sender.split("@")[0]
    message_id = _str(payload.get("message_id")).strip("<>")
    return Incoming(
        text=new_text_of(body)[:5000],
        channel="email",
        author=name[:200],
        source="email",
        email=sender,
        subject=(_str(payload.get("subject")) or None),
        external_id=f"email:{message_id}" if message_id else None,
        raw=_small({k: v for k, v in payload.items() if k not in ("html",)}),
    )


def manual(text: str, channel: str, author: str, email: str | None) -> Incoming:
    channel = channel if channel in config.CHANNELS else "other"
    return Incoming(text=text.strip()[:5000], channel=channel, author=(author.strip() or "Unknown")[:200],
                    source="manual", email=(email or "").strip() or None)
