"""Where the desk keeps its records: Firestore in production, memory in tests.

Every record is a document with a small integer id, so tickets can be numbered
RD-1001, RD-1002 and so on. Every write stamps `updated_at`, which lets the
page ask only for what changed since its last look: an idle desk costs a
handful of Firestore reads per refresh, not a re-read of every record.

Queries stay on one field each (an equality, an `in`, or a range on the field
being ordered by). Firestore serves those from its automatic single-field
indexes, so nothing needs a composite index created by hand; anything more
specific is filtered in Python on the few documents that come back.

Collections are all prefixed `desk_`, next to the app's `team_members` and
`post_audits` in the same database.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable

PREFIX = "desk_"
OPS = {
    "==": lambda a, b: a == b,
    ">": lambda a, b: a is not None and a > b,
    "<=": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
}


def _now() -> int:
    return int(time.time() * 1000)


class MemoryStore:
    """For tests and local runs without Firestore."""

    def __init__(self, clock=_now):
        self.clock = clock
        self.docs: dict[str, dict[str, dict]] = {}
        self.counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def _coll(self, name: str) -> dict[str, dict]:
        return self.docs.setdefault(name, {})

    def insert(self, coll: str, data: dict) -> int:
        with self._lock:
            new_id = self.counters.get(coll, 0) + 1
            self.counters[coll] = new_id
            self._coll(coll)[str(new_id)] = {**data, "id": new_id, "updated_at": self.clock()}
        return new_id

    def get(self, coll: str, doc_id) -> dict | None:
        doc = self._coll(coll).get(str(doc_id))
        return dict(doc) if doc else None

    def update(self, coll: str, doc_id, fields: dict) -> None:
        with self._lock:
            doc = self._coll(coll).get(str(doc_id))
            if doc is None:
                raise KeyError(doc_id)
            doc.update(fields, updated_at=self.clock())

    def put(self, coll: str, key: str, data: dict) -> None:
        """A document with a chosen key (journeys, unsubscribes)."""
        with self._lock:
            self._coll(coll)[key] = {**data, "updated_at": self.clock()}

    def delete(self, coll: str, key: str) -> None:
        with self._lock:
            self._coll(coll).pop(str(key), None)

    def where(self, coll: str, field: str, op: str, value, *, order: str | None = None,
              desc: bool = False, limit: int | None = None) -> list[dict]:
        rows = [dict(d) for d in self._coll(coll).values() if OPS[op](d.get(field), value)]
        if order:
            rows.sort(key=lambda d: (d.get(order) is None, d.get(order)), reverse=desc)
        return rows[:limit] if limit else rows

    def recent(self, coll: str, order: str, limit: int) -> list[dict]:
        rows = sorted((dict(d) for d in self._coll(coll).values()), key=lambda d: d.get(order) or 0, reverse=True)
        return rows[:limit]

    def all(self, coll: str) -> list[dict]:
        return [dict(d) for d in self._coll(coll).values()]


class FirestoreStore:
    """The same calls against Firestore, through the app's shared client."""

    def __init__(self, clock=_now):
        self.clock = clock

    def _c(self, coll: str):
        from service import firestore_db
        return firestore_db.collection(PREFIX + coll)

    def insert(self, coll: str, data: dict) -> int:
        from google.cloud import firestore
        from service import firestore_db

        counters = firestore_db.collection(PREFIX + "counters").document(coll)
        target = self._c(coll)
        client = firestore_db._client  # set by the collection() call above
        stamp = self.clock()

        @firestore.transactional
        def run(tx) -> int:
            snap = counters.get(transaction=tx)
            new_id = (snap.to_dict() or {}).get("next", 0) + 1 if snap.exists else 1
            tx.set(counters, {"next": new_id})
            tx.set(target.document(str(new_id)), {**data, "id": new_id, "updated_at": stamp})
            return new_id

        return run(client.transaction())

    def get(self, coll: str, doc_id) -> dict | None:
        snap = self._c(coll).document(str(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def update(self, coll: str, doc_id, fields: dict) -> None:
        self._c(coll).document(str(doc_id)).update({**fields, "updated_at": self.clock()})

    def put(self, coll: str, key: str, data: dict) -> None:
        self._c(coll).document(key).set({**data, "updated_at": self.clock()})

    def delete(self, coll: str, key: str) -> None:
        self._c(coll).document(str(key)).delete()

    def where(self, coll: str, field: str, op: str, value, *, order: str | None = None,
              desc: bool = False, limit: int | None = None) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._c(coll).where(filter=FieldFilter(field, op, list(value) if op == "in" else value))
        if order:
            # Only ever the filtered field itself, which needs no composite index.
            query = query.order_by(order, direction="DESCENDING" if desc else "ASCENDING")
        if limit:
            query = query.limit(limit)
        return [snap.to_dict() for snap in query.stream()]

    def recent(self, coll: str, order: str, limit: int) -> list[dict]:
        query = self._c(coll).order_by(order, direction="DESCENDING").limit(limit)
        return [snap.to_dict() for snap in query.stream()]

    def all(self, coll: str) -> list[dict]:
        return [snap.to_dict() for snap in self._c(coll).stream()]


def chunks(values: Iterable, size: int = 30) -> list[list]:
    """Firestore's `in` takes at most 30 values."""
    values = list(values)
    return [values[i:i + size] for i in range(0, len(values), size)]
