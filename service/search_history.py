"""Search history: every finished content search, saved to Firestore.

Each search is one document in `post_searches`: a small summary for the
history list (who, when, what was searched, per-platform counts, the best
post's engagement per day and the top three posts), plus every result row, so
the full table reopens without scraping again. Nine platforms at 50 posts each
stays well under Firestore's 1 MB document limit.

Saving is best effort, like audits: a search that cannot be saved is still
shown to the person who ran it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone

from post_audit.finder import NOTHING_FOUND

from .history import FirestoreStore, MemoryStore, NotFound, StoreUnavailable, _doc

COLLECTION = "post_searches"
LIST_LIMIT = 300
TOP_ROWS = 3
SUMMARY_FIELDS = ["created_at", "by", "mode", "term", "items", "platforms", "total", "best", "top"]

# The Viral Content Ideas sheet's columns, in its order.
CSV_COLUMNS = [
    ("Search Term", "search_term"), ("Platform", "platform_label"), ("Scraped At", "scraped_at"),
    ("Posted At", "posted_at"), ("Author", "author"), ("Content", "content"), ("URL", "url"),
    ("Views", "views"), ("Likes", "likes"), ("Comments", "comments"), ("Shares", "shares"),
    ("Days Since", "days_since"), ("Engagement", "engagement"),
]


def _summary_row(row: dict) -> dict:
    return {
        "platform_label": row.get("platform_label", ""), "author": row.get("author", ""),
        "content": (row.get("content") or "")[:160], "url": row.get("url", ""),
        "views": row.get("views"), "likes": row.get("likes"), "engagement": row.get("engagement"),
    }


def _platforms(result: dict) -> list[dict]:
    """Each platform searched, with how many posts it returned or why it failed (nothing found is not a failure)."""
    counts: dict[str, int] = {}
    for row in result.get("rows") or []:
        counts[row.get("platform", "")] = counts.get(row.get("platform", ""), 0) + 1
    failed = {e.get("platform"): e.get("message", "") for e in result.get("errors") or []
              if e.get("message") != NOTHING_FOUND}
    return [{"platform": p["platform"], "label": p["label"], "count": counts.get(p["platform"], 0),
             "error": failed.get(p["platform"], "") if not counts.get(p["platform"]) else ""}
            for p in result.get("platforms") or []]


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([label for label, _ in CSV_COLUMNS])
    for row in rows:
        cells = []
        for _, key in CSV_COLUMNS:
            value = row.get(key)
            text = "" if value is None else str(value)
            # Keep spreadsheets from running scraped text as a formula.
            if isinstance(value, str) and text[:1] in ("=", "+", "-", "@", "\t", "\r"):
                text = "'" + text
            cells.append(text)
        writer.writerow(cells)
    return "﻿" + out.getvalue()


@dataclass
class SearchHistory:
    store: object

    def save(self, result: dict, by: str, items: int) -> str:
        rows = result.get("rows") or []
        rated = [r["engagement"] for r in rows if r.get("engagement") is not None]
        ranked = sorted(rows, key=lambda r: (r.get("engagement") is None, -(r.get("engagement") or 0)))
        doc = {
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by": by or "",
            "mode": result.get("mode", ""),
            "term": result.get("term", ""),
            "items": items,
            "platforms": _platforms(result),
            "total": len(rows),
            "best": max(rated) if rated else None,
            "top": [_summary_row(r) for r in ranked[:TOP_ROWS]],
            # The whole result as the page got it, so the table can be shown again.
            "result": {k: v for k, v in result.items() if k != "history_id"},
        }
        return self.store.add(doc)

    def recent(self, limit: int = LIST_LIMIT) -> list[dict]:
        return [{"id": search_id, **data} for search_id, data in self.store.recent(limit)]

    def get(self, search_id: str) -> dict:
        doc = _doc(self.store, search_id)
        result = dict(doc.get("result") or {})
        result["history_id"] = search_id
        result["saved"] = {"by": doc.get("by", ""), "created_at": doc.get("created_at", "")}
        return result

    def owner_of(self, search_id: str) -> str:
        return _doc(self.store, search_id).get("by", "")

    def delete(self, search_id: str) -> None:
        self.store.delete(search_id)


_searches: SearchHistory | None = None


def get_searches() -> SearchHistory:
    global _searches
    if _searches is None:
        _searches = SearchHistory(store=FirestoreStore(COLLECTION, SUMMARY_FIELDS))
    return _searches


def use_store(store) -> SearchHistory:
    global _searches
    _searches = SearchHistory(store=store)
    return _searches


def memory_store() -> MemoryStore:
    return MemoryStore(SUMMARY_FIELDS)


__all__ = ["SearchHistory", "NotFound", "StoreUnavailable", "get_searches", "use_store", "memory_store", "to_csv"]
