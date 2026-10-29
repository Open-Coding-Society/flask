#!/usr/bin/env python3
"""db_upgrade.py
Bring an existing database up to the models WITHOUT dropping anything.

Compares the models against the live database, then:
- creates tables the models have and the database does not (db.create_all()),
- adds columns the models have and an existing table does not (ALTER TABLE ... ADD
  COLUMN), when existing rows can be given a value: the column allows NULL, or it has a
  fixed default. Existing rows get NULL or that default.
- --fill table.column=value gives existing rows a different value than new rows get,
  e.g. --fill users.active=1 keeps current accounts active while new ones start
  inactive. It also lets a NOT NULL column with no fixed default be added.
- says "Rebuild needed" and changes nothing when a missing column is a primary key,
  unique, a foreign key, or NOT NULL with no fixed default and no --fill, and prints
  the full db_init.py + restore workflow to run instead.
- leaves tables and columns the models no longer use in place, and lists them.

It is the one command to run after a deploy: it decides which path the change needs.
The comparison, backup and checks are shared with db_init.py in db_schema.py, so both
paths go through the same steps. No row is deleted or rewritten; every pre-existing
table is re-counted afterwards and the run fails if any lost rows.

A rollback point is required first: a mysqldump of the database (MySQL) or an
online-backup copy of the file (SQLite). The script takes it itself when it can;
the web container has no mysqldump, so on production take the dump on the host and
pass it in with --backup-file (see the migration runbook).

Usage (from the flask repo root):
  python scripts/db_upgrade.py --check                   # report only, change nothing
  python scripts/db_upgrade.py                           # backup, then upgrade
  python scripts/db_upgrade.py --backup-file instance/backups/<dump>.sql
  python scripts/db_upgrade.py --fill users.active=1     # existing rows get 1

Exit: 0 up to date or upgraded, 1 refused or failed, 2 (--check) in-place changes to make,
      3 rebuild needed.
"""
import argparse
import os
import sys

import sqlalchemy as sa

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import db_schema as schema
from db_schema import app, db
from db_utils import report_reconciliation


def report(to_create, plan, unused_tables, unused_cols):
    print(f"Database: {db.engine.dialect.name}")
    print(f"Tables to create:  {', '.join(to_create) or 'none'}")
    for t, cols in plan.items():
        for c, sql, reason in cols:
            print(f"Column to add:     {sql}" if sql else f"CANNOT ADD:        {t}.{c}: {reason}")
    if unused_tables:
        print(f"Unused tables (left in place): {', '.join(unused_tables)}")
    for t, cols in unused_cols.items():
        print(f"Unused columns (left in place): {t}: {', '.join(cols)}")


def main():
    parser = argparse.ArgumentParser(description="Bring the database up to the models; says when a rebuild is needed instead.")
    parser.add_argument('--check', action='store_true', help="report only, change nothing")
    parser.add_argument('--backup-file', help="a backup you already took (required where mysqldump is unavailable)")
    parser.add_argument('--fill', action='append', metavar='TABLE.COLUMN=VALUE',
                        help="value for existing rows in a column being added (repeatable)")
    args = parser.parse_args()

    with app.app_context():
        to_create, missing, unused_tables, unused_cols = schema.compare()
        try:
            fills = schema.read_fills(args.fill, missing)
        except ValueError as e:
            print(f"Refusing: {e}. Nothing was changed.")
            return 1
        plan = schema.plan_columns(missing, fills)
        report(to_create, plan, unused_tables, unused_cols)
        for (t, c), value in fills.items():
            print(f"Existing rows:     {t}.{c} = {value!r}")
        statements = [sql for cols in plan.values() for _, sql, _ in cols if sql]

        path = schema.decide(to_create, plan)
        if path == schema.CURRENT:
            print("\nUp to date. Nothing to do.")
            return 0
        if path == schema.REBUILD:
            refused = [f"{t}.{c}" for t, cols in plan.items() for c, sql, _ in cols if not sql]
            print(f"\nRebuild needed: {', '.join(refused)} can't be added in place (reasons above).")
            print("Nothing was changed. Run the full workflow (README, Database Management Workflow):")
            print("  1. local:       python scripts/db_migrate-prod2sqlite.py")
            print("  2. production:  python scripts/db_init.py")
            print("  3. local:       python scripts/db_restore-sqlite2prod.py")
            return 3
        if args.check:
            print(f"\nIn place: {len(to_create)} table(s) would be created and {len(statements)} column(s) added."
                  " Run without --check to apply.")
            return 2

        if not schema.take_backup("pre_upgrade", args.backup_file):
            print("\nRefusing to change the schema without a rollback point. Nothing was changed.")
            return 1

        existing = sorted(set(db.metadata.tables) - set(to_create))
        before = schema.row_counts(existing)
        steps = [(sql, sa.text(sql)) for sql in statements]
        steps += [(f"UPDATE {t} SET {c} = {value!r}", sa.update(db.metadata.tables[t]).values({c: value}))
                  for (t, c), value in fills.items()]
        done = []
        try:
            # One transaction per step: MySQL commits each ALTER on its own anyway, so this
            # keeps the list of what ran honest on both databases.
            for label, step in steps:
                with db.engine.begin() as conn:
                    conn.execute(step)
                done.append(label)
        except Exception as e:
            print(f"\nFAILED on: {steps[len(done)][0]}\n  {e}")
            print("Already applied:" + "".join(f"\n  {label}" for label in done) if done else "Nothing was applied.")
            print("Restore from the rollback point above, or fix the cause and run again"
                  " (columns already added are skipped).")
            return 1
        db.create_all()

        # The same checks db_init.py runs after a rebuild.
        kept = report_reconciliation(before, schema.row_counts(existing), "before", "after")
        problems = schema.verify_schema()
        if not kept or problems:
            print("\nFAILED." + "".join(f"\n  {p}" for p in problems) + ("\n  rows were lost (table above)" if not kept else ""))
            return 1
        print(f"\nCreated {len(to_create)} table(s), added {len(statements)} column(s);"
              f" all {len(existing)} existing tables kept their rows.")
        print("Upgrade complete.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
