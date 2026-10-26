"""Who may use the Care Desk, and who runs it.

The desk keeps its own list, separate from the Redefine App's team: being on
one does not put you on the other. Each person is an Admin, who can manage
this list on the desk's Team page, or an Agent, who works tickets and leads.
The app's owners (ALLOWED_EMAILS) are always desk Admins and cannot be
removed here, so nobody can lock everyone out.

The list lives in Firestore as `desk_team`, one document per email. The first
time it is read it is filled from the app's team, so the people who used the
desk before it had its own list keep their access: app admins become desk
Admins, everyone else Agents. After that the two lists are independent.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from html import escape

from service.email_login import DIVIDER, button, heading, layout, para
from service.firestore_db import StoreUnavailable
from service.profiles import get_profiles
from service.team import EMAIL, get_team

log = logging.getLogger("care-desk")

ROLES = ("admin", "agent")
CACHE_SECONDS = 30
RESEND_GAP = 60  # seconds between invites to the same person


class TeamError(ValueError):
    """A change the Team page cannot make; the text is shown as is."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def invite_message(email: str, role: str, by: str, desk_url: str, logo_url: str | None) -> tuple[str, str, str]:
    """Subject, plain text and HTML of the email someone gets when an admin adds them."""
    as_role = "an admin" if role == "admin" else "an agent"
    can = ("As an admin you can also manage who has access to the desk."
           if role == "admin" else "You'll work support tickets, contacts and follow-up emails.")
    subject = "You're invited to the Redefine Care Desk"
    text = (f"You're invited to the Redefine Care Desk\n\n"
            f"{by} added you to Redefine's Care Desk as {as_role}. The Care Desk is where the team answers "
            f"comments, works support tickets and follows up contacts. {can}\n\n"
            f"Open the Care Desk: {desk_url}\n\n"
            f"Sign in with Google, or choose \"Continue with email\" and we'll send a code to {email}. "
            "Use this address: the desk only lets in the people on its team.\n\n"
            "Not expecting this? You can ignore this email.\n\n"
            "Redefine Agency\nYou're getting this because a Care Desk admin added this address to the team.")
    body = (heading("You're invited to the Care Desk")
            + para(f'<strong style="color:#191616">{escape(by)}</strong> added you to Redefine\'s Care Desk as '
                   f"{as_role}. The Care Desk is where the team answers comments, works support tickets and "
                   f"follows up contacts. {escape(can)}", margin="0 0 22px")
            + button("Open the Care Desk", desk_url)
            + para('Sign in with Google, or choose <strong style="color:#4A4545">Continue with email</strong> and '
                   f'we\'ll send a code to <strong style="color:#4A4545">{escape(email)}</strong>. Use this address: '
                   "the desk only lets in the people on its team.", small=True, margin="0")
            + DIVIDER
            + para('<strong style="color:#4A4545">Not expecting this?</strong> You can ignore this email.',
                   small=True, margin="20px 0 0"))
    footer = "Redefine Agency<br>You're getting this because a Care Desk admin added this address to the team."
    return subject, text, layout(subject, f"{by} added you as {as_role}. Open the desk to sign in.", body, footer, logo_url)


class DeskTeam:
    def __init__(self, store, clock=time.time):
        self.db = store
        self.clock = clock
        self._cache: dict[str, dict] | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ reading

    def owners(self) -> list[str]:
        return get_team().owners()

    def _seed(self) -> None:
        """Copy the app's team in, once, so turning on the desk's own list locks nobody out."""
        if self.db.get("settings", "team"):
            return
        try:
            members = get_team().store.all()
        except StoreUnavailable:
            return  # try again on the next read rather than seed an empty list
        owners = set(self.owners())
        for email, data in members.items():
            if email in owners or self.db.get("team", email):
                continue
            role = "admin" if data.get("role") == "admin" else "agent"
            self.db.put("team", email, {"email": email, "role": role, "added_by": "Copied from the Redefine App",
                                        "added_at": _today()})
        self.db.put("settings", "team", {"seeded": True, "at": _today()})
        log.info("desk team filled from the app's team: %s people", len(members))

    def _members(self, fresh: bool = False) -> dict[str, dict]:
        with self._lock:
            if not fresh and self._cache is not None and time.time() - self._cached_at < CACHE_SECONDS:
                return self._cache
        try:
            self._seed()
            members = {d["email"]: d for d in self.db.all("team")}
        except StoreUnavailable:
            members = {} if self._cache is None else self._cache  # owners still get in
        with self._lock:
            self._cache, self._cached_at = members, time.time()
        return members

    def role_of(self, email: str) -> str | None:
        """'owner', 'admin', 'agent', or None if this email may not use the desk."""
        email = (email or "").strip().lower()
        if not email:
            return None
        if email in self.owners():
            return "owner"
        data = self._members().get(email)
        if not data:
            return None
        return data.get("role") if data.get("role") in ROLES else "agent"

    def emails(self) -> list[str]:
        """Everyone who may use the desk, from the cache (cheap enough for every page refresh)."""
        return sorted(set(self.owners()) | set(self._members()))

    def is_allowed(self, email: str) -> bool:
        return self.role_of(email) is not None

    def is_admin(self, email: str) -> bool:
        return self.role_of(email) in ("owner", "admin")

    def listing(self) -> list[dict]:
        """Owners first, then everyone else by email.

        `status` is "approved" once someone has used the desk, and "pending"
        until then; owners are always approved.
        """
        members = self._members(fresh=True)
        owners = self.owners()
        people = [{"email": e, "role": "owner", "status": "approved", "added_by": "Server setting", "added_at": None,
                   "joined_at": None, "invited_at": None, "invites": 0, "invite_error": None} for e in owners]
        people += [{"email": e, "role": d.get("role", "agent"), "status": "approved" if d.get("joined_at") else "pending",
                    "added_by": d.get("added_by", ""), "added_at": d.get("added_at"), "joined_at": d.get("joined_at"),
                    "invited_at": d.get("invited_at"), "invites": d.get("invites", 0),
                    "invite_error": d.get("invite_error")}
                   for e, d in sorted(members.items()) if e not in owners]
        names = get_profiles().names()
        for person in people:
            person["name"] = names.get(person["email"])
        return people

    # ------------------------------------------------------------ changing

    def add(self, email: str, role: str, by: str) -> None:
        email = (email or "").strip().lower()
        if not EMAIL.match(email) or len(email) > 254:
            raise TeamError("Enter a valid email address.")
        if role not in ROLES:
            raise TeamError("Choose Admin or Agent.")
        if email in self.owners():
            raise TeamError(f"{email} is an owner of the Redefine App, so they already run the desk.")
        if email in self._members(fresh=True):
            raise TeamError(f"{email} is already on the desk team.")
        self.db.put("team", email, {"email": email, "role": role, "added_by": by, "added_at": _today()})
        self._forget()

    def set_role(self, email: str, role: str, by: str) -> None:
        email = (email or "").strip().lower()
        if role not in ROLES:
            raise TeamError("Choose Admin or Agent.")
        if email in self.owners():
            raise TeamError("Owners are set on the server; their role can't be changed here.")
        if email == (by or "").lower():
            raise TeamError("You can't change your own role. Ask another admin.")
        current = self._members(fresh=True).get(email)
        if not current:
            raise TeamError(f"{email} isn't on the desk team.")
        self.db.put("team", email, {**current, "role": role, "role_changed_by": by, "role_changed_at": _today()})
        self._forget()

    def remove(self, email: str, by: str) -> None:
        email = (email or "").strip().lower()
        if email in self.owners():
            raise TeamError("Owners are set on the server and can't be removed here.")
        if email == (by or "").lower():
            raise TeamError("You can't remove yourself. Ask another admin.")
        if email not in self._members(fresh=True):
            raise TeamError(f"{email} isn't on the desk team.")
        self.db.delete("team", email)
        self._forget()

    def mark_joined(self, email: str) -> None:
        """Called on every signed-in request; writes only the first time someone uses the desk."""
        data = self._members().get((email or "").lower())
        if not data or data.get("joined_at"):
            return
        current = self.db.get("team", data["email"])
        if current and not current.get("joined_at"):
            self.db.put("team", data["email"], {**current, "joined_at": _today()})
            log.info("%s used the desk for the first time", data["email"])
        self._forget()

    def check_invite(self, email: str) -> dict:
        """The person to (re)invite; refuses owners, people who already use the desk, and rapid resends."""
        email = (email or "").strip().lower()
        if email in self.owners():
            raise TeamError("Owners already run the desk; they don't need an invite.")
        current = self.db.get("team", email)
        if not current:
            raise TeamError(f"{email} isn't on the desk team.")
        if current.get("joined_at"):
            raise TeamError(f"{email} already uses the desk, so there's nothing to resend.")
        if self.clock() - current.get("invited_ts", 0) < RESEND_GAP:
            raise TeamError("An invite was just sent. Wait a minute before sending another.")
        return current

    def record_invite(self, email: str, error: str | None) -> None:
        current = self.db.get("team", email)
        if not current:
            return
        fields = {"invite_error": error}
        if not error:
            fields.update(invited_at=_today(), invited_ts=self.clock(), invites=current.get("invites", 0) + 1)
        self.db.put("team", email, {**current, **fields})
        self._forget()

    def _forget(self) -> None:
        with self._lock:
            self._cache, self._cached_at = None, 0.0
