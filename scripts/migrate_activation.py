"""Add the account activation columns to an existing users table.

Dry run by default; pass --apply to change the database it points at (SQLite in dev,
MySQL when the DB_* env vars are set). db.create_all() cannot add columns to a table
that already exists, so this is how an existing database gets them.

Every account that exists when the columns are added is marked active and verified on
2026-01-30, so nobody is locked out and everyone falls due for re-verification in
January. The UPDATE only runs in the same run that adds the columns, so running this
again never re-activates an account that has since been deactivated.

Take a backup first (a copy of the SQLite file, or mysqldump for MySQL).
"""
import argparse
import os
import shutil
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import inspect, text
from main import app, db


def migrate(engine, apply):
    """Plan, and with apply=True execute, the migration. Returns the list of statements."""
    columns = {c['name'] for c in inspect(engine).get_columns('users')}
    if {'active', 'last_verified'} <= columns:
        return []

    steps = []
    if 'active' not in columns:
        steps.append('ALTER TABLE users ADD COLUMN active BOOLEAN NOT NULL DEFAULT 0')
    if 'last_verified' not in columns:
        steps.append('ALTER TABLE users ADD COLUMN last_verified DATETIME NULL')
    steps.append("UPDATE users SET active = 1, last_verified = '2026-01-30 00:00:00'")

    if apply:
        with engine.begin() as conn:
            for step in steps:
                conn.execute(text(step))
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='make the change (default is a dry run)')
    args = parser.parse_args()

    with app.app_context():
        engine = db.engine
        print(f"Database: {engine.url.render_as_string(hide_password=True)}")
        if args.apply and engine.dialect.name == 'sqlite' and engine.url.database:
            backup = engine.url.database + '.pre-activation.bak'
            shutil.copy2(engine.url.database, backup)
            print(f'Backed up SQLite file to {backup}')
        steps = migrate(engine, args.apply)
        if not steps:
            print('users already has active and last_verified; nothing to do.')
            return
        for step in steps:
            print('  ' + step)
        print('Done.' if args.apply else 'Dry run only. Re-run with --apply to execute.')


if __name__ == '__main__':
    main()
