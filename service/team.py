"""Who may sign in: owners from the server setting, plus a team list kept in Firestore.

Owners are the emails in ALLOWED_EMAILS. They can always sign in and always
manage the team, and the admin page cannot remove them, so no one can lock
everyone out from inside the app. Everyone else is a document in the
Firestore collection `team_members`, added and removed on the admin page.

If Firestore is not set up (or briefly unreachable), sign-in keeps working
for owners and the admin page says what is missing.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import firestore_db
from .firestore_db import StoreUnavailable  # noqa: F401 - re-exported for callers

log = logging.getLogger("team")

COLLECTION = "team_members"
ROLES = ("member", "admin")
# Each instance re-reads the team list at most this often. A change made on
# the admin page applies at once on the instance that made it, and within
# this window on any other.
CACHE_SECONDS = 30
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TeamError(Exception):
    """A change the admin page cannot make; the message is safe to show."""


@dataclass
class Member:
    email: str
    role: str = "member"
    added_by: str = ""
    added_at: str = ""
    owner: bool = False

    def to_dict(self) -> dict:
        return {"email": self.email, "role": "owner" if self.owner else self.role,
                "added_by": self.added_by, "added_at": self.added_at, "owner": self.owner}


class MemoryStore:
    """For tests and local runs without Firestore."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def all(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self.docs.items()}

    def put(self, email: str, data: dict) -> None:
        self.docs[email] = dict(data)

    def delete(self, email: str) -> None:
        self.docs.pop(email, None)


class FirestoreStore:
    def all(self) -> dict[str, dict]:
        return firestore_db.call(COLLECTION, lambda c: {doc.id: doc.to_dict() or {} for doc in c.stream()})

    def put(self, email: str, data: dict) -> None:
        firestore_db.call(COLLECTION, lambda c: c.document(email).set(data))

    def delete(self, email: str) -> None:
        firestore_db.call(COLLECTION, lambda c: c.document(email).delete())


@dataclass
class Team:
    store: object
    _cache: dict[str, dict] | None = None
    _cached_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------ reading

    def owners(self) -> list[str]:
        raw = os.environ.get("ALLOWED_EMAILS", "")
        return sorted({e.strip().lower() for e in raw.split(",") if e.strip()})

    def _members(self, fresh: bool = False) -> dict[str, dict]:
        """Firestore members, cached; {} when Firestore is unavailable (owners still work)."""
        with self._lock:
            if not fresh and self._cache is not None and time.time() - self._cached_at < CACHE_SECONDS:
                return self._cache
        try:
            members = self.store.all()
        except StoreUnavailable:
            members = {} if self._cache is None else self._cache
        with self._lock:
            self._cache, self._cached_at = members, time.time()
        return members

    def role_of(self, email: str) -> str | None:
        """'owner', 'admin', 'member', or None if this email may not sign in."""
        email = (email or "").lower()
        if not email:
            return None
        if email in self.owners():
            return "owner"
        data = self._members().get(email)
        if not data:
            return None
        return data.get("role") if data.get("role") in ROLES else "member"

    def is_allowed(self, email: str) -> bool:
        return self.role_of(email) is not None

    def can_manage(self, email: str) -> bool:
        return self.role_of(email) in ("owner", "admin")

    def listing(self) -> list[Member]:
        """Owners first, then members by email. Raises StoreUnavailable if Firestore is down."""
        stored = self.store.all()
        with self._lock:
            self._cache, self._cached_at = stored, time.time()
        owners = self.owners()
        people = [Member(email=e, owner=True, role="owner", added_by="Server setting") for e in owners]
        for email in sorted(stored):
            if email in owners:
                continue
            data = stored[email]
            people.append(Member(email=email, role=data.get("role", "member"),
                                 added_by=data.get("added_by", ""), added_at=data.get("added_at", "")))
        return people

    # ------------------------------------------------------------ changing

    def add(self, email: str, role: str, by: str) -> Member:
        email = (email or "").strip().lower()
        if not EMAIL.match(email) or len(email) > 254:
            raise TeamError("Enter a valid email address.")
        if role not in ROLES:
            raise TeamError("Choose member or admin.")
        if email in self.owners():
            raise TeamError(f"{email} is already an owner (set on the server).")
        member = Member(email=email, role=role, added_by=by,
                        added_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.store.put(email, {"role": role, "added_by": by, "added_at": member.added_at})
        self._forget()
        return member

    def set_role(self, email: str, role: str, by: str) -> Member:
        """Switch a member between member and admin. Only owners may (enforced by the route)."""
        email = (email or "").strip().lower()
        if role not in ROLES:
            raise TeamError("Choose member or admin.")
        if email in self.owners():
            raise TeamError("Owners are set on the server; their role can't be changed here.")
        current = self.store.all().get(email)
        if current is None:
            raise TeamError(f"{email} isn't on the team.")
        updated = {**current, "role": role, "role_changed_by": by,
                   "role_changed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.store.put(email, updated)
        self._forget()
        return Member(email=email, role=role, added_by=current.get("added_by", ""),
                      added_at=current.get("added_at", ""))

    def remove(self, email: str, by: str) -> None:
        email = (email or "").strip().lower()
        if email in self.owners():
            raise TeamError("Owners are set on the server and can't be removed here.")
        if email == (by or "").lower():
            raise TeamError("You can't remove yourself. Ask another admin or an owner.")
        self.store.delete(email)
        self._forget()

    def _forget(self) -> None:
        with self._lock:
            self._cache, self._cached_at = None, 0.0


_team: Team | None = None


def get_team() -> Team:
    global _team
    if _team is None:
        _team = Team(store=FirestoreStore())
    return _team


def use_store(store) -> Team:
    """Swap the backing store (tests, local runs)."""
    global _team
    _team = Team(store=store)
    return _team
