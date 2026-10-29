"""Where the desk keeps its records: Supabase or Firestore in production, memory in tests.

SupabaseStore is used when SUPABASE_DB_URL is set, FirestoreStore otherwise.
All three answer the same few calls the same way, so the desk never knows
which one it has. What follows describes Firestore, which came first.

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

import base64
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterable

log = logging.getLogger("care-desk")

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


class SupabaseStore:
    """The same calls against Postgres on Supabase.

    Each collection is a table in the `care_desk` schema: `key` is the document
    id and `data` the document itself as jsonb, so records keep the shape they
    had in Firestore and SQL can still read any field (`data->>'category'`).
    Tables are made on first use. The schema is not exposed through Supabase's
    Data API, and row level security is on with no policies besides, so only
    the desk's own database login reads it.

    Ids come from the `counters` table, one row per collection, exactly as
    `desk_counters` did in Firestore. File parts are bytes, which JSON cannot
    hold, so they are stored as {"$bytes": base64} and turned back on read.
    """

    # Fields the desk filters or sorts on, besides updated_at, which every table indexes.
    INDEXED = {
        "messages": ("received_at", "external_id", "fb_comment_id", "match_at", "retry_at", "lead_id"),
        "tickets": ("created_at", "escalate_at"),
        "leads": ("created_at", "email", "next_at", "status", "contact_id", "name_key"),
        "events": ("ticket_id", "lead_id"),
        "emails": ("at", "lead_id", "ticket_id"),
        "notes": ("ticket_id",),
    }
    NAME = re.compile(r"[a-z_]{1,40}")
    SQL_OPS = {"==": "=", ">": ">", "<=": "<="}

    def __init__(self, url: str, clock=_now, schema: str = "care_desk"):
        from psycopg import sql
        from psycopg.types.json import Jsonb

        self.url = url
        self.clock = clock
        self.schema = schema
        self._sql, self._Jsonb = sql, Jsonb
        self._pool = None
        self._ready: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ plumbing

    def _connections(self):
        with self._lock:
            if self._pool is None:
                from psycopg_pool import ConnectionPool

                # prepare_threshold=None: Supabase's transaction pooler cannot keep prepared statements.
                self._pool = ConnectionPool(self.url, min_size=1, max_size=5, timeout=10, name="care-desk", open=True,
                                            kwargs={"prepare_threshold": None, "connect_timeout": 10},
                                            check=ConnectionPool.check_connection)
        return self._pool

    @contextmanager
    def _cursor(self, coll: str):
        """One transaction on `coll`'s table, committed when the block ends."""
        import psycopg
        from psycopg_pool import PoolTimeout
        from service.firestore_db import StoreUnavailable

        try:
            with self._connections().connection() as conn:
                self._ensure(conn, coll)
                with conn.cursor() as cur:
                    yield cur
        except (psycopg.OperationalError, PoolTimeout) as exc:
            log.warning("supabase call on %s failed: %s", coll, exc)
            raise StoreUnavailable("The desk's database (Supabase) is not reachable right now.") from exc

    def _table(self, coll: str):
        if not self.NAME.fullmatch(coll):
            raise ValueError(f"Not a collection name: {coll!r}")
        return self._sql.Identifier(self.schema, coll)

    def _field(self, name: str):
        if not self.NAME.fullmatch(name):
            raise ValueError(f"Not a field name: {name!r}")
        return self._sql.SQL("data->{}").format(self._sql.Literal(name))

    def _ensure(self, conn, coll: str) -> None:
        if coll in self._ready:
            return
        import psycopg

        sql, table = self._sql, self._table(coll)
        statements = [
            sql.SQL("create schema if not exists {}").format(sql.Identifier(self.schema)),
            sql.SQL("create table if not exists {} (key text primary key, data jsonb not null)").format(table),
            sql.SQL("alter table {} enable row level security").format(table),
        ]
        for name in ("updated_at", *self.INDEXED.get(coll, ())):
            statements.append(sql.SQL("create index if not exists {} on {} (({}))").format(
                sql.Identifier(f"{coll}_{name}"), table, self._field(name)))
        try:
            with conn.transaction():
                for statement in statements:
                    conn.execute(statement)
        except (psycopg.errors.UniqueViolation, psycopg.errors.DuplicateObject):
            pass  # another instance made it at the same moment
        self._ready.add(coll)

    def _out(self, value):
        if isinstance(value, dict):
            if value.keys() == {"$bytes"}:
                return base64.b64decode(value["$bytes"])
            return {k: self._out(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._out(v) for v in value]
        return value

    def _in(self, value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
        if isinstance(value, dict):
            return {k: self._in(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._in(v) for v in value]
        return value

    def _json(self, value):
        return self._Jsonb(self._in(value))

    # ------------------------------------------------------------ the store's calls

    def insert(self, coll: str, data: dict) -> int:
        sql = self._sql
        with self._cursor("counters") as cur:
            cur.execute(sql.SQL(
                "insert into {t} as c (key, data) values (%s, '{{\"next\": 1}}') on conflict (key) do update "
                "set data = jsonb_build_object('next', (c.data->>'next')::bigint + 1) "
                "returning (data->>'next')::bigint").format(t=self._table("counters")), (coll,))
            new_id = cur.fetchone()[0]
            self._ensure(cur.connection, coll)
            cur.execute(sql.SQL("insert into {} (key, data) values (%s, %s)").format(self._table(coll)),
                        (str(new_id), self._json({**data, "id": new_id, "updated_at": self.clock()})))
        return new_id

    def get(self, coll: str, doc_id) -> dict | None:
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL("select data from {} where key = %s").format(self._table(coll)), (str(doc_id),))
            row = cur.fetchone()
        return self._out(row[0]) if row else None

    def update(self, coll: str, doc_id, fields: dict) -> None:
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL("update {} set data = data || %s where key = %s").format(self._table(coll)),
                        (self._json({**fields, "updated_at": self.clock()}), str(doc_id)))
            if cur.rowcount == 0:
                raise KeyError(doc_id)

    def put(self, coll: str, key: str, data: dict) -> None:
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL("insert into {} (key, data) values (%s, %s) "
                                      "on conflict (key) do update set data = excluded.data").format(self._table(coll)),
                        (key, self._json({**data, "updated_at": self.clock()})))

    def delete(self, coll: str, key: str) -> None:
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL("delete from {} where key = %s").format(self._table(coll)), (str(key),))

    def where(self, coll: str, field: str, op: str, value, *, order: str | None = None,
              desc: bool = False, limit: int | None = None) -> list[dict]:
        sql, f = self._sql, self._field(field)
        if op == "in":
            cond, params = sql.SQL("{} = any(%s)").format(f), [[self._json(v) for v in value]]
        elif op == "==":
            cond, params = sql.SQL("{} = %s").format(f), [self._json(value)]
        elif op in self.SQL_OPS:
            # Like Firestore, a range only matches values of the same type: never null, never a string.
            cond = sql.SQL("{f} {op} %s and jsonb_typeof({f}) = jsonb_typeof(%s)").format(
                f=f, op=sql.SQL(self.SQL_OPS[op]))
            params = [self._json(value)] * 2
        else:
            raise ValueError(f"Unsupported operator: {op}")
        query = sql.SQL("select data from {} where {}").format(self._table(coll), cond)
        if order:
            query += sql.SQL(" order by {} {}").format(self._field(order), sql.SQL("desc" if desc else "asc"))
        if limit:
            query += sql.SQL(" limit {}").format(sql.Literal(int(limit)))
        with self._cursor(coll) as cur:
            cur.execute(query, params)
            return [self._out(row[0]) for row in cur.fetchall()]

    def recent(self, coll: str, order: str, limit: int) -> list[dict]:
        f = self._field(order)
        query = self._sql.SQL("select data from {} where {f} is not null order by {f} desc limit {n}").format(
            self._table(coll), f=f, n=self._sql.Literal(int(limit)))
        with self._cursor(coll) as cur:
            cur.execute(query)
            return [self._out(row[0]) for row in cur.fetchall()]

    def all(self, coll: str) -> list[dict]:
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL("select data from {}").format(self._table(coll)))
            return [self._out(row[0]) for row in cur.fetchall()]

    # ------------------------------------------------------------ the move from Firestore

    def copy_in(self, coll: str, key: str, data: dict) -> str:
        """Store a document as it was, keeping its updated_at. The newer copy wins, so a re-run is safe.

        Returns "added", "updated", or "kept" when the copy here is as new or newer.
        """
        with self._cursor(coll) as cur:
            cur.execute(self._sql.SQL(
                "insert into {} as t (key, data) values (%s, %s) on conflict (key) do update set data = excluded.data "
                "where coalesce(t.data->'updated_at', '0') < coalesce(excluded.data->'updated_at', '0') "
                "returning xmax = 0").format(self._table(coll)), (key, self._json(data)))
            row = cur.fetchone()
        return "kept" if row is None else "added" if row[0] else "updated"

    def raise_counter(self, coll: str, at_least: int) -> None:
        """Make sure the next id handed out for `coll` is above `at_least`."""
        with self._cursor("counters") as cur:
            cur.execute(self._sql.SQL(
                "insert into {t} as c (key, data) values (%s, jsonb_build_object('next', %s::bigint)) "
                "on conflict (key) do update set data = jsonb_build_object('next', "
                "greatest((c.data->>'next')::bigint, (excluded.data->>'next')::bigint))").format(t=self._table("counters")),
                (coll, at_least))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


def chunks(values: Iterable, size: int = 30) -> list[list]:
    """Firestore's `in` takes at most 30 values."""
    values = list(values)
    return [values[i:i + size] for i in range(0, len(values), size)]
