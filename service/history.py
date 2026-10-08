"""Audit history: every finished post audit, saved to Firestore.

Each audit is one document in `post_audits`: a small summary used by the
history list (who, when, which post, headline numbers, a cover thumbnail),
plus everything needed to show the full report again — the analysis, the
numbers and the frames. Frames are re-encoded smaller so a document stays well
under Firestore's 1 MB limit; the PDF is not stored, it is rebuilt on demand.

Saving is best effort: an audit that cannot be saved is still returned to the
person who ran it.
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import firestore_db
from .firestore_db import StoreUnavailable

log = logging.getLogger("history")

COLLECTION = "post_audits"
LIST_LIMIT = 300
FRAME_WIDTH = 320          # enough for the report strips and the PDF
COVER_WIDTH = 160          # the history list thumbnail
MAX_FRAMES_BYTES = 600_000  # base64 budget for all frames in one document
SUMMARY_FIELDS = [
    "created_at", "by", "platform", "platform_label", "url", "author", "handle", "media_type",
    "posted_at", "views", "likes", "comments", "engagement", "hook_score", "hook_type",
    "cta_strength", "verdict", "cover",
]


class NotFound(Exception):
    pass


_ID = re.compile(r"[A-Za-z0-9]{1,64}")


def _doc(store, audit_id: str) -> dict:
    # Firestore ids are 20 letters and digits; anything else is not ours to look up.
    doc = store.get(audit_id) if isinstance(audit_id, str) and _ID.fullmatch(audit_id) else None
    if not doc:
        raise NotFound(audit_id)
    return doc


# ------------------------------------------------------------------ stores

class MemoryStore:
    """For tests and local runs without Firestore."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self._next = 0

    def add(self, data: dict) -> str:
        self._next += 1
        audit_id = f"a{self._next:06d}"
        self.docs[audit_id] = dict(data)
        return audit_id

    def get(self, audit_id: str) -> dict | None:
        return dict(self.docs[audit_id]) if audit_id in self.docs else None

    def recent(self, limit: int) -> list[tuple[str, dict]]:
        rows = sorted(self.docs.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)[:limit]
        return [(k, {f: v.get(f) for f in SUMMARY_FIELDS}) for k, v in rows]

    def delete(self, audit_id: str) -> None:
        self.docs.pop(audit_id, None)


class FirestoreStore:
    def add(self, data: dict) -> str:
        def write(collection):
            ref = collection.document()
            ref.set(data)
            return ref.id
        return firestore_db.call(COLLECTION, write)

    def get(self, audit_id: str) -> dict | None:
        def read(collection):
            doc = collection.document(audit_id).get()
            return doc.to_dict() if doc.exists else None
        return firestore_db.call(COLLECTION, read)

    def recent(self, limit: int) -> list[tuple[str, dict]]:
        from google.cloud import firestore

        # Only the summary fields: the list never downloads frames or analyses.
        return firestore_db.call(COLLECTION, lambda c: [
            (doc.id, doc.to_dict() or {})
            for doc in c.select(SUMMARY_FIELDS)
                        .order_by("created_at", direction=firestore.Query.DESCENDING)
                        .limit(limit).stream()
        ])

    def delete(self, audit_id: str) -> None:
        firestore_db.call(COLLECTION, lambda c: c.document(audit_id).delete())


# ------------------------------------------------------------------ images

def _shrink(src: str, width: int) -> str | None:
    """Re-encode a data: JPEG at `width` px; None if it isn't a usable image."""
    if not isinstance(src, str) or not src.startswith("data:image/"):
        return None
    try:
        from post_audit.media import ffmpeg_bin

        raw = base64.b64decode(src.split(",", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "in", Path(tmp) / "out.jpg"
            source.write_bytes(raw)
            subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(source),
                            "-vf", f"scale='min({width},iw)':-2", "-q:v", "5", "-y", str(out)],
                           capture_output=True, timeout=30, check=False)
            if out.exists() and out.stat().st_size:
                return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()
    except Exception:  # noqa: BLE001 - a thumbnail is never worth failing a save
        log.debug("could not shrink a frame", exc_info=True)
    return None


def _frames_for_storage(frames: list[dict]) -> list[dict]:
    kept, budget = [], MAX_FRAMES_BYTES
    # Hook frames first: if anything must be dropped, it is the body.
    for frame in sorted(frames or [], key=lambda f: f.get("label") != "hook"):
        small = _shrink(frame.get("src", ""), FRAME_WIDTH)
        if not small or len(small) > budget:
            continue
        budget -= len(small)
        kept.append({"seconds": frame.get("seconds"), "label": frame.get("label", ""), "src": small})
    return sorted(kept, key=lambda f: (f["label"] != "hook", f["seconds"] if f["seconds"] is not None else 0))


def _cover(frames: list[dict]) -> str | None:
    hook = [f for f in frames or [] if f.get("label") == "hook"]
    pick = (hook[-2] if len(hook) > 1 else hook[-1]) if hook else (frames[0] if frames else None)
    return _shrink(pick.get("src", ""), COVER_WIDTH) if pick else None


# ------------------------------------------------------------------ history

@dataclass
class History:
    store: object

    def save(self, report: dict, by: str) -> str:
        post = report.get("post") or {}
        metrics = report.get("metrics") or {}
        analysis = report.get("analysis") or {}
        hook = analysis.get("hook") or {}
        doc = {
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by": by or "",
            "platform": report.get("platform", ""),
            "platform_label": report.get("platform_label", ""),
            "url": post.get("url", ""),
            "author": post.get("author", ""),
            "handle": post.get("author_handle", ""),
            "media_type": post.get("media_type", ""),
            "posted_at": post.get("posted_at", ""),
            "views": post.get("views"),
            "likes": post.get("likes"),
            "comments": post.get("comments"),
            "engagement": metrics.get("engagement_rate_by_views"),
            "hook_score": hook.get("score"),
            "hook_type": hook.get("type", ""),
            "cta_strength": (analysis.get("caption") or {}).get("cta_strength", ""),
            "verdict": str(analysis.get("verdict") or "")[:400],
            "cover": _cover(report.get("frames") or []),
            # The full report, minus the one-off PDF, so it can be shown again.
            "report": {k: v for k, v in report.items() if k not in ("pdf", "frames", "history_id")},
            "frames": _frames_for_storage(report.get("frames") or []),
        }
        return self.store.add(doc)

    def recent(self, limit: int = LIST_LIMIT) -> list[dict]:
        return [{"id": audit_id, **data} for audit_id, data in self.store.recent(limit)]

    def get(self, audit_id: str) -> dict:
        doc = _doc(self.store, audit_id)
        report = dict(doc.get("report") or {})
        report["frames"] = doc.get("frames") or []
        report["history_id"] = audit_id
        report["saved"] = {"by": doc.get("by", ""), "created_at": doc.get("created_at", "")}
        return report

    def owner_of(self, audit_id: str) -> str:
        return _doc(self.store, audit_id).get("by", "")

    def delete(self, audit_id: str) -> None:
        self.store.delete(audit_id)


_history: History | None = None


def get_history() -> History:
    global _history
    if _history is None:
        _history = History(store=FirestoreStore())
    return _history


def use_store(store) -> History:
    global _history
    _history = History(store=store)
    return _history


__all__ = ["History", "MemoryStore", "FirestoreStore", "NotFound", "StoreUnavailable", "get_history", "use_store"]
