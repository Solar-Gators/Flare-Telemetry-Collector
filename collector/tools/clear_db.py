#!/usr/bin/env python3
"""Clear the local telemetry store.

Deletes the SQLite database (and its WAL/SHM sidecars) so the next app run
starts fresh. Honours FLARE_DB_PATH, same as the app.

Usage:
    .venv/bin/python tools/clear_db.py            # asks for confirmation
    .venv/bin/python tools/clear_db.py --yes      # no prompt
    FLARE_DB_PATH=/some/other.db tools/clear_db.py --yes

Stop the app before running this — deleting the DB while the writer thread has
it open can error or leave a partial file behind.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import DEFAULT_DB_PATH


def main():
    ap = argparse.ArgumentParser(description="Delete the local telemetry database.")
    ap.add_argument("--path", default=os.environ.get("FLARE_DB_PATH") or DEFAULT_DB_PATH,
                    help="database path (default: FLARE_DB_PATH or data/telemetry.db)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    # WAL mode keeps the DB across three files; remove all of them.
    targets = [args.path, args.path + "-wal", args.path + "-shm"]
    existing = [p for p in targets if os.path.exists(p)]

    if not existing:
        print(f"nothing to clear: {args.path} does not exist")
        return

    print("about to delete:")
    for p in existing:
        print(f"  {p}  ({os.path.getsize(p)} bytes)")

    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return

    for p in existing:
        try:
            os.remove(p)
            print(f"removed {p}")
        except OSError as err:
            print(f"could not remove {p}: {err}", file=sys.stderr)
            sys.exit(1)

    print("done — a fresh database is created on the next run")


if __name__ == "__main__":
    main()
