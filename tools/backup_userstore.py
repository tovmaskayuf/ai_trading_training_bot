"""Dump the durable user store out of Render Postgres into a JSON file.

Free Render Postgres is deleted 30 days after creation (plus a 14-day grace
period), and it takes every account, portfolio and leaderboard standing with
it. This exists so that deadline stops being load-bearing.

Needs no pg_dump -- it goes through psycopg, which is already in the venv
because the app itself depends on it.

    DATABASE_URL='postgresql://...' .venv/bin/python tools/backup_userstore.py
    DATABASE_URL='postgresql://...' .venv/bin/python tools/backup_userstore.py --out ~/backups

`sessions` is skipped on purpose: the rows are live session tokens, they expire
on their own, and writing them to a file on disk is a liability with no upside.
A restore wipes them instead, which logs everybody out.

Restore with tools/restore_userstore.py. The app also serves this same dump at
GET /api/admin/export for the routine monthly case; this script is what you
want when the app is down, not deployed, or pointing at the wrong database.

Deliberately standalone -- it does not import `userstore`, because it has to
run against a database whose application is not running. That is why the table
list below is a second copy of `userstore.EXPORT_TABLES`; keep them in step.
"""

import argparse
import datetime
import decimal
import json
import os
import sys

import psycopg

TABLES = ["app_meta", "users", "portfolios", "holdings", "user_trades", "user_equity"]


def _plain(value):
    """Make psycopg's richer column types survive json.dumps."""
    if isinstance(value, decimal.Decimal):
        # str, not float -- money must not pick up binary-float error.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return value


def main():
    ap = argparse.ArgumentParser(description="Dump the durable user store to JSON.")
    ap.add_argument("--out", default=".", metavar="DIR",
                    help="directory to write into (default: current directory)")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Use the External Database URL from Render.")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(args.out, f"userstore-backup-{stamp}.json")
    dump = {"taken_at": datetime.datetime.now().isoformat(), "tables": {}}

    with psycopg.connect(url, connect_timeout=30) as conn:
        for table in TABLES:
            with conn.cursor() as cur:
                try:
                    cur.execute(f"SELECT * FROM {table}")
                except psycopg.errors.UndefinedTable:
                    # An older deployment may predate a table; that is not fatal.
                    print(f"  {table:<12} absent, skipped")
                    conn.rollback()
                    continue
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, (_plain(v) for v in r))) for r in cur.fetchall()]
            dump["tables"][table] = rows
            print(f"  {table:<12} {len(rows):>6} rows")

    # 0600, and O_EXCL so a same-second re-run cannot silently overwrite a
    # dump. This file is every account's PBKDF2 hash; the default umask would
    # leave it readable by anyone with a login on the machine.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dump, fh, indent=2)

    total = sum(len(r) for r in dump["tables"].values())
    print(f"\nWrote {out} -- {total} rows across {len(dump['tables'])} tables.")
    print("Contains password hashes. Keep it out of the repository and off "
          "shared drives; restore with tools/restore_userstore.py.")


if __name__ == "__main__":
    main()
