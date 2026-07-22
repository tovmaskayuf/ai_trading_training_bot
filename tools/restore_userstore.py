"""Load a tools/backup_userstore.py dump back into a Postgres user store.

The counterpart to the backup. Free Render Postgres is deleted 30 days after
creation, so this runs roughly monthly: create a new database, let the app boot
once against it so the schema exists, then point this at the dump.

    DATABASE_URL='postgresql://...' .venv/bin/python tools/restore_userstore.py \
        ~/backups/userstore-backup-20260722-140300.json --wipe --dry-run

**This does not create schema.** Boot the app against the new database first --
that is what runs userstore._init_schema() -- or every table here is undefined.

Deliberately standalone, like the backup: it does not import `userstore`,
because it has to run against a database whose application may not be deployed
yet. The table lists below are therefore a second copy of
`userstore.EXPORT_TABLES`; keep them in step.

`sessions` is wiped but never inserted. The backup skips it on purpose, and any
session rows already on the target point at user ids this restore is about to
reassign -- leaving them would hand somebody else's account to an old cookie.
**A restore logs everyone out.**
"""

import argparse
import datetime
import decimal
import json
import os
import sys

import psycopg

# Dependency order for insertion. No backend declares foreign keys, so nothing
# enforces this -- it is for the human reading the output.
INSERT_ORDER = ["app_meta", "users", "portfolios", "holdings",
                "user_trades", "user_equity"]

# Reverse, plus `sessions`: children before parents, so no row outlives its user.
WIPE_ORDER = ["user_equity", "user_trades", "holdings", "portfolios",
              "sessions", "app_meta", "users"]

# The only BIGSERIAL columns in the schema. Inserting explicit ids does not
# advance a sequence, so without a setval the first signup after a restore
# collides on the primary key.
SEQUENCES = [("users", "id"), ("user_trades", "id")]


def _restore(value, pg_type):
    """The inverse of backup's _plain().

    Today's schema has no column any of this fires on -- it is only TEXT,
    BIGINT, INTEGER, DOUBLE PRECISION and BIGSERIAL. But the dump is built from
    SELECT *, so it carries whatever the live schema had, and a dump taken from
    a future schema should still load. Driven by the target's own catalogue
    rather than a hardcoded column map for the same reason.
    """
    if value is None or not isinstance(value, str):
        return value
    if pg_type == "bytea":
        return bytes.fromhex(value)
    if pg_type in ("numeric", "money"):
        return decimal.Decimal(value)
    if pg_type.startswith("timestamp"):
        return datetime.datetime.fromisoformat(value)
    if pg_type == "date":
        return datetime.date.fromisoformat(value)
    return value


def _column_types(cur, table):
    # Scoped to current_schema(): information_schema spans every schema on the
    # database, and an unrelated `users` table elsewhere would report the wrong
    # types. userstore._existing_columns does the same for the same reason.
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = %s AND table_schema = current_schema()",
                (table,))
    return dict(cur.fetchall())


def _describe_target(url):
    """Host and database name, never the password."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(url)
    except Exception:
        return "(unparseable connection string)"
    return (f"{info.get('host', '?')}:{info.get('port', 5432)}"
            f"/{info.get('dbname', '?')}")


def _counts(cur, tables):
    out = {}
    for table in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = cur.fetchone()[0]
        except psycopg.errors.UndefinedTable:
            cur.connection.rollback()
            out[table] = None
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Restore a userstore backup into Postgres.")
    ap.add_argument("backup", help="path to a userstore-backup-*.json file")
    ap.add_argument("--wipe", action="store_true",
                    help="delete existing rows first (required if the target "
                         "is not empty)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything, then roll back and report")
    ap.add_argument("--yes", action="store_true",
                    help="skip the typed confirmation (non-interactive use)")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Use the External Database URL from Render.")

    with open(args.backup) as fh:
        dump = json.load(fh)
    if "tables" not in dump:
        sys.exit(f"{args.backup} does not look like a userstore backup "
                 "(no 'tables' key).")

    file_counts = {t: len(rows) for t, rows in dump["tables"].items()}
    target = _describe_target(url)

    with psycopg.connect(url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            live = _counts(cur, INSERT_ORDER)

        missing = [t for t, n in live.items() if n is None]
        if missing:
            sys.exit(f"These tables do not exist on the target: {missing}.\n"
                     "Boot the app against this database first -- that is what "
                     "creates the schema -- then re-run.")

        # Both sides, side by side, immediately above the prompt. A target with
        # more users than the backup is the signature of a stale DATABASE_URL
        # still exported from the backup step, i.e. pointing at production.
        print(f"\n  target : {target}")
        print(f"  backup : {os.path.basename(args.backup)}"
              f"  (taken {dump.get('taken_at', '?')})\n")
        print(f"  {'table':<14}{'target':>10}{'backup':>10}")
        for table in INSERT_ORDER:
            got = file_counts.get(table)
            print(f"  {table:<14}{live[table]:>10}"
                  f"{'-' if got is None else got:>10}")

        occupied = sum(live.values())
        if occupied and not args.wipe:
            sys.exit("\nThe target already holds rows. Pass --wipe to replace "
                     "them, or point at an empty database.\n"
                     "Restoring into a populated store would merge two "
                     "unrelated histories under colliding ids.")

        if args.wipe and not args.yes and not args.dry_run:
            dbname = _describe_target(url).rsplit("/", 1)[-1]
            print(f"\nThis DELETES every row in {target} and replaces it.")
            try:
                typed = input(f"Type the database name ({dbname}) to continue: ")
            except EOFError:
                sys.exit("\nNot a terminal. Pass --yes if you mean it.")
            if typed.strip() != dbname:
                sys.exit("Names did not match. Nothing was changed.")

        print()
        with conn.cursor() as cur:
            if args.wipe:
                for table in WIPE_ORDER:
                    cur.execute(f"DELETE FROM {table}")
                    print(f"  wiped   {table:<14}{cur.rowcount:>8} rows")
                print()

            for table in INSERT_ORDER:
                rows = dump["tables"].get(table)
                # None and [] mean different things: a table absent from the
                # dump entirely (backup_userstore skips one it cannot read, and
                # every dump predating app_meta lacks it) versus one that was
                # genuinely empty.
                if rows is None:
                    print(f"  {table:<14} not in backup, skipped")
                    continue
                if not rows:
                    print(f"  {table:<14}{0:>8} rows")
                    continue

                cols = list(rows[0])
                odd = [i for i, r in enumerate(rows) if list(r) != cols]
                if odd:
                    sys.exit(f"{table}: row {odd[0]} has different columns from "
                             "row 0. The dump is inconsistent; aborting.")

                types = _column_types(cur, table)
                unknown = [c for c in cols if c not in types]
                if unknown:
                    sys.exit(
                        f"{table}: the backup has columns the target does not: "
                        f"{unknown}.\nDeploy the current code against this "
                        "database so the schema catches up, then re-run.")
                extra = [c for c in types if c not in cols]
                if extra:
                    print(f"  ! {table}: target has {extra} not in the backup; "
                          "they take their defaults")

                marks = ",".join(["%s"] * len(cols))
                collist = ",".join(f'"{c}"' for c in cols)
                cur.executemany(
                    f"INSERT INTO {table} ({collist}) VALUES ({marks})",
                    [tuple(_restore(r[c], types[c]) for c in cols) for r in rows])
                print(f"  {table:<14}{len(rows):>8} rows")

            print()
            for table, column in SEQUENCES:
                cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, column))
                seq = cur.fetchone()[0]
                if seq is None:
                    print(f"  ! {table}.{column} is not a serial column, "
                          "sequence not reset")
                    continue
                cur.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}")
                nxt = cur.fetchone()[0]
                if args.dry_run:
                    # setval is NOT transactional -- Postgres does not undo it
                    # on rollback. Running it here would leave the sequence
                    # pinned to the *backup's* max after the rows themselves
                    # were restored, so a target that held higher ids would
                    # collide on its very next insert. Report, do not touch.
                    print(f"  {table}.{column} sequence would go to {nxt}")
                    continue
                # is_called=false rather than setval(MAX(id), true): on an
                # empty table COALESCE gives 0, and setval(seq, 0, true) is
                # invalid for a sequence whose minvalue is 1. This form is
                # correct for both the empty and the populated case.
                cur.execute("SELECT setval(%s, %s, false)", (seq, nxt))
                print(f"  {table}.{column} sequence -> {cur.fetchone()[0]}")

        if args.dry_run:
            conn.rollback()
            print("\nDRY RUN -- rolled back, nothing was changed.")
            return

    # psycopg commits on clean exit from the context manager and rolls back on
    # any exception, so a failure anywhere above leaves the target untouched.
    print("\nRestored. Sessions were not restored, so everyone is logged out; "
          "that is expected.")
    print("Check /api/health: store_backend should read postgres and "
          "accounts_durable true.")


if __name__ == "__main__":
    main()
