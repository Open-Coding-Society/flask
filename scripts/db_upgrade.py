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
- REFUSES, changing nothing, when a missing column is a primary key, unique, a foreign
  key, or NOT NULL with no fixed default and no --fill. Those need the full
  db_init.py + restore workflow.
- leaves tables and columns the models no longer use in place, and lists them.

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
  python scripts/db_upgrade.py --fill users.active=1     # existing rows get 1

Exit: 0 up to date or upgraded, 1 refused or failed, 2 (--check) changes to make.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

import sqlalchemy as sa

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


def fixed_default(col, dialect):
    """SQL for the value existing rows get, None for NULL, or raise ValueError."""
    if col.server_default is not None:
        arg = getattr(col.server_default, 'arg', None)
        if isinstance(arg, str):
            return sa.literal(arg).compile(dialect=dialect, compile_kwargs={'literal_binds': True}).string
        if arg is not None:
            return str(arg.compile(dialect=dialect))
    if col.default is not None:
        if not col.default.is_scalar:
            return None  # a Python function: new rows only, existing rows get NULL
        return sql_literal(col.default.arg, col, dialect)
    return None


def parse_fill(col, text):
    """The Python value for --fill, read as the column's type."""
    kind = col.type.python_type
    if kind is bool:
        if text.lower() not in ('1', '0', 'true', 'false'):
            raise ValueError("must be 1, 0, true or false")
        return text.lower() in ('1', 'true')
    if kind is datetime:
        return datetime.fromisoformat(text)
    return kind(text)


def sql_literal(value, col, dialect):
    try:
        return sa.literal(value, col.type).compile(dialect=dialect, compile_kwargs={'literal_binds': True}).string
    except Exception as e:
        raise ValueError(f"{value!r} can't be written as SQL ({e})")


def add_column_sql(table, col, fill=None):
    """(ALTER TABLE statement, None) when the column can be added in place, else (None, reason).

    fill is the --fill value, or None. Without one, existing rows get the column's fixed
    default or NULL. With one, a NOT NULL column with no fixed default takes it as its
    default; otherwise it is set by an UPDATE after the ALTER."""
    dialect = db.engine.dialect
    q = dialect.identifier_preparer.quote
    if col.primary_key:
        return None, "it is a primary key"
    if col.unique or any(idx.unique and col.name in idx.columns for idx in table.indexes):
        return None, "it is unique, which SQLite cannot add to an existing table"
    if col.foreign_keys:
        return None, "it is a foreign key"
    try:
        col_type = col.type.compile(dialect=dialect)
        default = fixed_default(col, dialect)
    except Exception as e:
        return None, str(e)
    if default is None and not col.nullable:
        if fill is None:
            return None, ("it is NOT NULL with no fixed default, so existing rows would have no value"
                          f" (give them one with --fill {table.name}.{col.name}=VALUE)")
        try:
            default = sql_literal(fill, col, dialect)
        except ValueError as e:
            return None, str(e)
    if default is not None and dialect.name == 'mysql' and col_type.upper().split('(')[0] in ('TEXT', 'MEDIUMTEXT', 'LONGTEXT', 'BLOB', 'JSON'):
        default = f"({default})"  # MySQL takes a default on these types only as an expression
    sql = f"ALTER TABLE {q(table.name)} ADD COLUMN {q(col.name)} {col_type}"
    if default is not None:
        sql += f" DEFAULT {default}"
    sql += " NULL" if col.nullable else " NOT NULL"
    return sql, None


def plan_columns(missing, fills):
    """{table: [(column, sql, reason)]} for every missing column."""
    tables = db.metadata.tables
    return {t: [(c, *add_column_sql(tables[t], tables[t].columns[c], fills.get((t, c)))) for c in cols]
            for t, cols in missing.items()}


def read_fills(args_fill, missing):
    """{(table, column): value} from --fill, or raise ValueError. Only columns being added."""
    fills = {}
    for item in args_fill or []:
        target, sep, text = item.partition('=')
        table, dot, column = target.partition('.')
        if not sep or not dot:
            raise ValueError(f"--fill {item}: use table.column=value")
        if column not in missing.get(table, []):
            raise ValueError(f"--fill {item}: {table}.{column} is not a column being added")
        try:
            fills[(table, column)] = parse_fill(db.metadata.tables[table].columns[column], text)
        except Exception as e:
            raise ValueError(f"--fill {item}: {e}")
    return fills


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
    parser = argparse.ArgumentParser(description="Create missing tables and columns without dropping anything.")
    parser.add_argument('--check', action='store_true', help="report only, change nothing")
    parser.add_argument('--backup-file', help="a backup you already took (required where mysqldump is unavailable)")
    parser.add_argument('--fill', action='append', metavar='TABLE.COLUMN=VALUE',
                        help="value for existing rows in a column being added (repeatable)")
    args = parser.parse_args()

    with app.app_context():
        to_create, missing, unused_tables, unused_cols = compare()
        try:
            fills = read_fills(args.fill, missing)
        except ValueError as e:
            print(f"Refusing: {e}. Nothing was changed.")
            return 1
        plan = plan_columns(missing, fills)
        report(to_create, plan, unused_tables, unused_cols)
        for (t, c), value in fills.items():
            print(f"Existing rows:     {t}.{c} = {value!r}")
        statements = [sql for cols in plan.values() for _, sql, _ in cols if sql]
        refused = [f"{t}.{c}" for t, cols in plan.items() for c, sql, _ in cols if not sql]

        if refused:
            print(f"\nRefusing: {', '.join(refused)} can't be added in place (reasons above).")
            print("Make the column nullable or give it a fixed default, or use the full")
            print("db_init.py + restore workflow. Nothing was changed.")
            return 1
        if not to_create and not statements:
            print("\nUp to date. Nothing to do.")
            return 0
        if args.check:
            print(f"\n{len(to_create)} table(s) would be created and {len(statements)} column(s) added."
                  " Run without --check to apply.")
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
        after = row_counts(existing)

        still_to_create, still_missing = compare()[:2]
        changed = {t: (before[t], after[t]) for t in existing if before[t] != after[t]}
        nulls = {}
        with db.engine.connect() as conn:
            for t, cols in plan.items():
                for c, _, _ in cols:
                    column = db.metadata.tables[t].columns[c]
                    if not column.nullable and c not in still_missing.get(t, []):
                        n = conn.execute(sa.select(sa.func.count()).where(column.is_(None))).scalar()
                        if n:
                            nulls[f"{t}.{c}"] = n
        if to_create:
            print(f"\nCreated: {', '.join(t for t in to_create if t not in still_to_create)}")
        if statements:
            print(f"Added {len(statements)} column(s).")
        if still_to_create or still_missing or changed or nulls:
            print(f"FAILED. Not created: {still_to_create or 'none'}. Columns still missing: "
                  f"{still_missing or 'none'}. Row counts changed: {changed or 'none'}. "
                  f"NULLs in NOT NULL columns: {nulls or 'none'}")
            return 1
        print(f"All {len(existing)} existing tables kept their row counts ({sum(after.values())} rows).")
        print("Upgrade complete.")
        return 0

if __name__ == "__main__":
    sys.exit(main())
