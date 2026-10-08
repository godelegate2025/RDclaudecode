"""One shared Firestore client for the team list and the audit history.

On Cloud Run the project and credentials come from the service's own
identity; no key file is involved. Every failure — no database yet, API
off, no credentials, network — surfaces as StoreUnavailable, which callers
treat as "work without Firestore" rather than as an error page.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("firestore")

_client = None
_lock = threading.Lock()


class StoreUnavailable(Exception):
    """Firestore is not set up or not reachable; the message is safe to show."""


def collection(name: str):
    global _client
    try:
        with _lock:
            if _client is None:
                from google.cloud import firestore

                _client = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
        return _client.collection(name)
    except Exception as exc:  # noqa: BLE001 - missing project, API off, no credentials
        raise StoreUnavailable("Firestore is not set up for this project yet.") from exc


def call(name: str, fn):
    """Run fn(collection) and turn any Firestore failure into StoreUnavailable."""
    try:
        return fn(collection(name))
    except StoreUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - database not created, permission denied, network
        log.warning("firestore call on %s failed: %s", name, exc)
        raise StoreUnavailable("Firestore is not set up for this project yet, or not reachable.") from exc
