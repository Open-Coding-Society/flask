#!/usr/bin/env python3
"""db_upgrade.py
Bring an existing database up to the models WITHOUT dropping anything.

Compares the models against the live database, then:
- creates tables the models have and the database does not (db.create_all()),
- leaves tables and columns the models no longer use in place, and lists them,
- REFUSES when an existing table is missing a column. create_all() cannot add one,
  so that change needs an ALTER TABLE (or the full db_init.py + restore workflow).

No row is deleted or rewritten. Every pre-existing table is re-counted afterwards
and the run fails if any count changed.

A rollback point is required first: a mysqldump of the database (MySQL) or an
online-backup copy of the file (SQLite). The script takes it itself when it can;
the web container has no mysqldump, so on production take the dump on the host and
pass it in with --backup-file (see the migration runbook).

Usage (from the flask repo root):
  python scripts/db_upgrade.py --check                   # report only, change nothing
  python scripts/db_upgrade.py                           # backup, then upgrade
  python scripts/db_upgrade.py --backup-file instance/backups/<dump>.sql

Exit: 0 up to date or upgraded, 1 refused or failed, 2 (--check) tables to create.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from main import app, db  # registers every model

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'instance', 'backups'))


def compare():
    """(tables to create, missing columns as {table: [col]}, unused tables, unused columns)."""
    inspector = db.inspect(db.engine)
    live = set(inspector.get_table_names())
    models = db.metadata.tables
    to_create = sorted(t for t in models if t not in live)
    missing, unused_cols = {}, {}
    for name in sorted(t for t in models if t in live):
        live_cols = {c['name'] for c in inspector.get_columns(name)}
        model_cols = set(models[name].columns.keys())
        if model_cols - live_cols:
            missing[name] = sorted(model_cols - live_cols)
        if live_cols - model_cols:
            unused_cols[name] = sorted(live_cols - model_cols)
    unused_tables = sorted(live - set(models))
    return to_create, missing, unused_tables, unused_cols


def row_counts(tables):
    with db.engine.connect() as conn:
        return {t: conn.execute(db.text(f'SELECT COUNT(*) FROM {db.engine.dialect.identifier_preparer.quote(t)}')).scalar()
                for t in tables}


def check_backup_file(path):
    """A dump passed in by hand must exist and be complete; a partial dump is not a rollback point."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return f"{path} does not exist or is empty"
    if path.endswith('.sql'):
        with open(path, 'rb') as fh:
            fh.seek(max(0, os.path.getsize(path) - 200))
            if b'Dump completed' not in fh.read():
                return f"{path} does not end with 'Dump completed' -- the dump was cut short"
    return None


def take_backup():
    """Back up the live database. Returns the backup path, or None."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if db.engine.dialect.name == 'sqlite':
        src = db.engine.url.database
        dest = os.path.join(BACKUP_DIR, f"user_management_{datetime.now():%Y%m%d_%H%M%S}_pre_upgrade.db")
        with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
            s.backup(d)  # consistent even while the app is writing, unlike a file copy
        print(f"SQLite database backed up to {dest}")
        return dest
    if shutil.which('mysqldump') is None:
        print("mysqldump is not on PATH here (it is not in the web container).")
        print("Take the dump on the host and pass it with --backup-file; see the migration runbook.")
        return None
    from db_init import backup_mysql_database
    return backup_mysql_database()


def report(to_create, missing, unused_tables, unused_cols):
    print(f"Database: {db.engine.dialect.name}")
    print(f"Tables to create:  {', '.join(to_create) or 'none'}")
    for t, cols in missing.items():
        print(f"MISSING COLUMNS:   {t}: {', '.join(cols)}")
    if unused_tables:
        print(f"Unused tables (left in place): {', '.join(unused_tables)}")
    for t, cols in unused_cols.items():
        print(f"Unused columns (left in place): {t}: {', '.join(cols)}")


def main():
    parser = argparse.ArgumentParser(description="Create missing tables without dropping anything.")
    parser.add_argument('--check', action='store_true', help="report only, change nothing")
    parser.add_argument('--backup-file', help="a backup you already took (required where mysqldump is unavailable)")
    args = parser.parse_args()

    with app.app_context():
        to_create, missing, unused_tables, unused_cols = compare()
        report(to_create, missing, unused_tables, unused_cols)

        if missing:
            print("\nRefusing: create_all() cannot add a column to an existing table.")
            print("Add it with ALTER TABLE (and a default for existing rows), or use the full")
            print("db_init.py + restore workflow. Nothing was changed.")
            return 1
        if not to_create:
            print("\nUp to date. Nothing to do.")
            return 0
        if args.check:
            print(f"\n{len(to_create)} table(s) would be created. Run without --check to apply.")
            return 2

        if args.backup_file:
            problem = check_backup_file(args.backup_file)
            if problem:
                print(f"\nRefusing: {problem}.")
                return 1
            print(f"Rollback point: {args.backup_file}")
        elif take_backup() is None:
            print("\nRefusing to change the schema without a rollback point. Nothing was changed.")
            return 1

        existing = sorted(set(db.metadata.tables) - set(to_create))
        before = row_counts(existing)
        db.create_all()
        after = row_counts(existing)

        still_missing = compare()[0]
        changed = {t: (before[t], after[t]) for t in existing if before[t] != after[t]}
        print(f"\nCreated: {', '.join(t for t in to_create if t not in still_missing)}")
        if still_missing or changed:
            print(f"FAILED. Not created: {still_missing or 'none'}. Row counts changed: {changed or 'none'}")
            return 1
        print(f"All {len(existing)} existing tables kept their row counts ({sum(after.values())} rows).")
        print("Upgrade complete.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
