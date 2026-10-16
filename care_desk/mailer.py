"""Send email through Resend. Without a key, nothing is sent and the desk says so."""

from __future__ import annotations

import html
import os

import requests

RESEND_URL = "https://api.resend.com/emails"


class MailError(RuntimeError):
    """The email service refused or could not be reached; safe to show staff."""


def configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("DESK_FROM_EMAIL"))


def render_html(body: str, footer: str, unsubscribe_url: str | None) -> str:
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font:15px/1.6 Arial,sans-serif;color:#2C2828">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in body.split("\n\n") if p.strip()
    )
    link = (f' <a href="{html.escape(unsubscribe_url)}" style="color:#6b6b6b">Unsubscribe</a>.'
            if unsubscribe_url else "")
    return (
        '<div style="background:#F4F3F0;padding:24px 12px"><div style="max-width:560px;margin:0 auto;'
        'background:#fff;border-radius:12px;padding:28px 26px">'
        f"{paragraphs}"
        f'<p style="margin:22px 0 0;border-top:1px solid #e6e3df;padding-top:12px;font:12px/1.5 Arial,sans-serif;'
        f'color:#8a8580">{html.escape(footer)}{link}</p></div></div>'
    )


def send(to: str, subject: str, body: str, footer: str, unsubscribe_url: str | None = None) -> str:
    """Send one email and return the provider's id."""
    payload = {
        "from": os.environ["DESK_FROM_EMAIL"],
        "to": [to],
        "subject": subject,
        "text": body + "\n\n--\n" + footer + (f"\nUnsubscribe: {unsubscribe_url}" if unsubscribe_url else ""),
        "html": render_html(body, footer, unsubscribe_url),
    }
    if os.environ.get("DESK_REPLY_TO"):
        payload["reply_to"] = os.environ["DESK_REPLY_TO"]
    if unsubscribe_url:
        payload["headers"] = {"List-Unsubscribe": f"<{unsubscribe_url}>"}
    try:
        response = requests.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise MailError("Could not reach the email service.") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = ""
        raise MailError(f"The email service refused it (HTTP {response.status_code}){': ' + detail if detail else ''}.")
    return str(response.json().get("id", ""))
