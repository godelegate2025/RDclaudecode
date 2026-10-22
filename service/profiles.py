"""People's names, shared by the Redefine App and the Care Desk.

One Firestore document per email in `user_profiles`. Google sign-in fills the
name from the Google account; someone who signs in with an emailed code types
it once. A name someone typed is theirs: Google never overwrites it, while a
name that came from Google follows later changes to the Google account.

Everything else keeps recording emails (ticket owners, activity, the team
lists); names are only for showing people, looked up when a page loads.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from . import firestore_db
from .firestore_db import StoreUnavailable

log = logging.getLogger("profiles")

COLLECTION = "user_profiles"
MAX_NAME = 80
CACHE_SECONDS = 60


class NameError_(ValueError):
    """A name the form refuses; the message is safe to show."""


class MemoryStore:
    """For tests and local runs without Firestore."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def get(self, email: str) -> dict | None:
        doc = self.docs.get(email)
        return dict(doc) if doc else None

    def put(self, email: str, data: dict) -> None:
        self.docs[email] = dict(data)

    def all(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self.docs.items()}


class FirestoreStore:
    def get(self, email: str) -> dict | None:
        def read(c):
            snap = c.document(email).get()
            return snap.to_dict() if snap.exists else None
        return firestore_db.call(COLLECTION, read)

    def put(self, email: str, data: dict) -> None:
        firestore_db.call(COLLECTION, lambda c: c.document(email).set(data))

    def all(self) -> dict[str, dict]:
        return firestore_db.call(COLLECTION, lambda c: {d.id: d.to_dict() or {} for d in c.stream()})


class Profiles:
    def __init__(self, store):
        self.store = store
        self._cache: dict[str, dict] | None = None
        self._cached_at = 0.0
        self._ok = True  # False while Firestore can't be read, so nobody is asked for a name we can't save
        self._lock = threading.Lock()

    def _all(self) -> dict[str, dict]:
        with self._lock:
            if self._cache is not None and time.time() - self._cached_at < CACHE_SECONDS:
                return self._cache
        try:
            docs, ok = self.store.all(), True
        except StoreUnavailable:
            docs, ok = self._cache or {}, False  # names are a nicety: carry on with emails
        with self._lock:
            self._cache, self._cached_at, self._ok = docs, time.time(), ok
        return docs

    def needs_name(self, email: str) -> bool:
        """True when this person has no name yet and one could be saved now.

        Checks the database itself before asking, because the name may have
        just been saved through another server whose copy is fresher.
        """
        email = (email or "").lower()
        if self.name_of(email) or not self._ok:
            return False
        try:
            doc = self.store.get(email) or {}
        except StoreUnavailable:
            return False
        if doc.get("name"):
            with self._lock:
                self._cache = {**(self._cache or {}), email: doc}
            return False
        return True

    def name_of(self, email: str) -> str | None:
        return (self._all().get((email or "").lower()) or {}).get("name") or None

    def names(self) -> dict[str, str]:
        """Every known name by email, for pages that show people."""
        return {e: d["name"] for e, d in self._all().items() if d.get("name")}

    def set_name(self, email: str, name: str, source: str = "typed") -> str:
        name = clean_name(name)
        email = (email or "").lower()
        self.store.put(email, {"email": email, "name": name, "source": source,
                               "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        with self._lock:
            if self._cache is not None:
                self._cache = {**self._cache, email: {"name": name, "source": source}}
        return name

    def from_google(self, email: str, google_name: str | None) -> None:
        """Take the Google account's name unless the person typed their own."""
        if not google_name or not google_name.strip():
            return
        try:
            current = self.store.get((email or "").lower()) or {}
            if current.get("source") == "typed" or current.get("name") == google_name.strip()[:MAX_NAME]:
                return
            self.set_name(email, google_name, source="google")
        except (StoreUnavailable, NameError_) as exc:
            log.warning("could not save the Google name for %s: %s", email, exc)


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        raise NameError_("Enter your name.")
    if len(name) > MAX_NAME:
        raise NameError_(f"Keep your name under {MAX_NAME} characters.")
    if "@" in name:
        raise NameError_("Enter your name, not your email.")
    return name


_profiles: Profiles | None = None


def get_profiles() -> Profiles:
    global _profiles
    if _profiles is None:
        _profiles = Profiles(FirestoreStore())
    return _profiles


def use_store(store) -> Profiles:
    """Swap the backing store (tests, local runs)."""
    global _profiles
    _profiles = Profiles(store)
    return _profiles
