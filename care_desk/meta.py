"""Facebook, through Meta's Graph API: find a comment's id, and reply to it as the Page.

GHL tells the desk a comment's text and who wrote it, but not its Facebook id,
and GHL's API cannot post a reply. Meta can do both with a Page token. In the
app's development mode Meta hides who wrote a comment unless that person has a
role on the app, so the desk keeps GHL as the source of names and uses Meta
only for ids, post links and replies.

Settings:
  META_PAGE_TOKEN  a long-lived Page token for Redefine Ph (Secret Manager: meta-page-token)
  META_PAGE_ID     the Page's id; defaults to Redefine Ph's
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import requests

GRAPH = "https://graph.facebook.com/v26.0"
REDEFINE_PH = "1316682498188449"
# Enough to cover any comment a few minutes old: the newest posts and their newest comments.
POSTS = 15
COMMENTS = 50


class MetaError(RuntimeError):
    """Facebook refused or could not be reached; the message is safe to show staff."""


@dataclass
class Comment:
    id: str
    message: str
    created_at: int        # ms since the epoch
    author: str | None     # None when Meta hides it (development mode)
    post_url: str | None
    comment_url: str | None


def configured() -> bool:
    return bool(os.environ.get("META_PAGE_TOKEN", "").strip())


def _token() -> str:
    return os.environ["META_PAGE_TOKEN"].strip()


def _ms(stamp: str) -> int:
    return int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)


def _call(method: str, path: str, **params) -> dict:
    try:
        response = requests.request(method, f"{GRAPH}/{path}", params={**params, "access_token": _token()},
                                    timeout=15)
    except requests.RequestException as exc:
        raise MetaError("Could not reach Facebook. Try again in a moment.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or "error" in body:
        error = body.get("error") or {}
        code = error.get("code")
        if code == 190:
            raise MetaError("The Facebook token stopped working. Make a new Page token (CARE_DESK.md, Facebook replies).")
        if code in (10, 200) or (isinstance(code, int) and 200 <= code < 300):
            raise MetaError("Facebook refused: the token is missing a permission for this.")
        if code in (4, 17, 32, 613):
            raise MetaError("Facebook is rate limiting the Page. Try again in a few minutes.")
        if code == 100:
            raise MetaError("Facebook could not find that comment. It may have been deleted.")
        raise MetaError(f"Facebook refused it: {error.get('message') or f'HTTP {response.status_code}'}")
    return body


def recent_comments() -> list[Comment]:
    """The newest comments, replies included, on the Page's newest posts."""
    page = os.environ.get("META_PAGE_ID", REDEFINE_PH).strip() or REDEFINE_PH
    fields = (f"id,permalink_url,comments.filter(stream).order(reverse_chronological).limit({COMMENTS})"
              "{id,message,from,created_time,permalink_url}")
    body = _call("GET", f"{page}/feed", fields=fields, limit=POSTS)
    found = []
    for post in body.get("data", []):
        for c in (post.get("comments") or {}).get("data", []):
            if not c.get("message"):
                continue  # stickers and photos with no text cannot be matched by text
            found.append(Comment(
                id=c["id"], message=c["message"], created_at=_ms(c["created_time"]),
                author=(c.get("from") or {}).get("name"), post_url=post.get("permalink_url"),
                comment_url=c.get("permalink_url")))
    return found


def reply(comment_id: str, text: str) -> str:
    """Post `text` as the Page under the comment; returns the reply's id."""
    return str(_call("POST", f"{comment_id}/comments", message=text).get("id", ""))
