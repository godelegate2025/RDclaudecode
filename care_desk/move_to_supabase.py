"""Copy the desk's records from Firestore to Supabase. Safe to run more than once.

Run it from the repo folder, signed in to Google Cloud for Firestore (`gcloud
auth login` is enough; an application-default login is used when there is one)
and with the Supabase connection string in SUPABASE_DB_URL:

    python -m care_desk.move_to_supabase --check   # count both sides, copy nothing
    python -m care_desk.move_to_supabase           # copy

Each Firestore collection desk_<name> becomes the table care_desk.<name>.
Records keep their ids and their updated_at. When a record is in both places
the newer copy wins, so a second run only brings over what changed since the
first and never undoes work done on Supabase.

Supabase's id counters start ID_GAP past the highest id in Firestore. Until the
desk is redeployed on Supabase it keeps adding records to Firestore, and the
gap keeps their ids clear of the ones handed out on Supabase, so the catch-up
run copies them in instead of mistaking them for newer records. Ticket numbers
jump ahead once, by that much.

Only the desk moves. The app's own collections (team_members, profiles,
post_audits, sign-in codes) stay in Firestore and the desk keeps reading them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

from .store import PREFIX, SupabaseStore

ID_GAP = 100  # far more records than the old desk can add in the minutes the switch takes


def _plain(value):
    """Firestore hands back timestamps as datetimes; JSON wants text."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _collections(client) -> dict[str, object]:
    found = {}
    for coll in client.collections():
        if coll.id.startswith(PREFIX):
            name = coll.id[len(PREFIX):]
            if not SupabaseStore.NAME.fullmatch(name):
                sys.exit(f"Cannot move {coll.id}: table names may only use a-z and _.")
            found[name] = coll
    return dict(sorted(found.items()))


def check(client, store, out=print) -> None:
    out(f"{'table':<14}{'firestore':>10}{'supabase':>10}")
    for name, coll in _collections(client).items():
        theirs = sum(1 for _ in coll.stream())
        out(f"{name:<14}{theirs:>10}{len(store.all(name)):>10}")


def move(client, store, out=print) -> None:
    collections = _collections(client)
    if not collections:
        sys.exit(f"No {PREFIX}* collections found in Firestore project {client.project}. Wrong project?")
    out(f"{'table':<14}{'added':>8}{'updated':>8}{'kept':>8}")
    counters = collections.pop("counters", None)
    for name, coll in collections.items():
        tally = {"added": 0, "updated": 0, "kept": 0}
        top = 0
        for snap in coll.stream():
            tally[store.copy_in(name, snap.id, _plain(snap.to_dict() or {}))] += 1
            if snap.id.isdigit():
                top = max(top, int(snap.id))
        if top:
            store.raise_counter(name, top + ID_GAP)
        out(f"{name:<14}{tally['added']:>8}{tally['updated']:>8}{tally['kept']:>8}")
    if counters is not None:
        for snap in counters.stream():
            store.raise_counter(snap.id, int((snap.to_dict() or {}).get("next", 0)) + ID_GAP)
        out(f"counters      set {ID_GAP} past Firestore's")


def _gcloud(*args: str) -> str:
    # On Windows, plain "gcloud" can find the SDK's bash script, which can't be run directly.
    exe = (os.name == "nt" and shutil.which("gcloud.cmd")) or shutil.which("gcloud")
    if not exe:
        sys.exit("gcloud is not installed, and there is no application-default login either.")
    done = subprocess.run([exe, *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"gcloud {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _firestore(project: str | None):
    """Firestore through the application-default login, or else through gcloud's own login."""
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import firestore

    database = os.environ.get("FIRESTORE_DATABASE", "(default)")
    try:
        return firestore.Client(project=project, database=database)
    except DefaultCredentialsError:
        from google.oauth2.credentials import Credentials

        # An hour-long token from `gcloud auth login`: plenty for a copy this size.
        token = Credentials(_gcloud("auth", "print-access-token"))
        return firestore.Client(project=project or _gcloud("config", "get-value", "project"),
                                credentials=token, database=database)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="count records on both sides; copy nothing")
    parser.add_argument("--project", help="Google Cloud project, if gcloud's default is not the right one")
    args = parser.parse_args(argv)

    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not url:
        sys.exit("Set SUPABASE_DB_URL to the Supabase connection string first (CARE_DESK.md, Move to Supabase).")

    client = _firestore(args.project)
    store = SupabaseStore(url)
    try:
        print(f"Firestore project {client.project} -> Supabase schema {store.schema}\n")
        if args.check:
            check(client, store)
        else:
            move(client, store)
            print("\nDone. Re-run any time; it only copies what is newer in Firestore.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
