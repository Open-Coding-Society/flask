#!/usr/bin/env python3
"""db_schema.py
The steps every schema change shares, however it is applied:

1. compare the models with the live database and decide how the change can be applied,
2. take a rollback point,
3. apply it -- the only step that differs:
   - in place (db_upgrade.py): ALTER TABLE ... ADD COLUMN and create_all(), data stays put,
   - rebuild (db_init.py, then db_restore-sqlite2prod.py): drop_all(), create_all(), reload,
4. check the result: the database matches the models, with no NULLs in NOT NULL columns.

decide() picks the path, so nobody has to know which script a change needs. Because the
rebuild uses the same compare, backup and checks as the in-place upgrade, the rarely run
path is exercised by the same code that runs every time.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

import sqlalchemy as sa

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from main import app, db  # registers every model

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'instance', 'backups'))

CURRENT, IN_PLACE, REBUILD = "up to date", "in place", "rebuild"


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


def take_backup(label, backup_file=None):
    """The rollback point every schema change needs first. Returns its path, or None.

    backup_file is a dump taken by hand (production's web container has no mysqldump);
    it is checked for completeness instead of taking a new one."""
    if backup_file:
        problem = check_backup_file(backup_file)
        if problem:
            print(f"Refusing: {problem}.")
            return None
        print(f"Rollback point: {backup_file}")
        return backup_file
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if db.engine.dialect.name == 'sqlite':
        src = db.engine.url.database
        dest = os.path.join(BACKUP_DIR, f"user_management_{datetime.now():%Y%m%d_%H%M%S}_{label}.db")
        with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
            s.backup(d)  # consistent even while the app is writing, unlike a file copy
        print(f"SQLite database backed up to {dest}")
        return dest
    if shutil.which('mysqldump') is None:
        print("mysqldump is not on PATH here (it is not in the web container).")
        print("Take the dump on the host and pass it with --backup-file; see the migration runbook.")
        return None
    return backup_mysql_database()


def backup_mysql_database():
    """mysqldump the production database before it is dropped.

    Returns the dump path, or None if the dump could not be taken. This step
    used to print "Backup not supported for production database" and continue
    straight into drop_all(), leaving no rollback point at all.
    """
    if shutil.which('mysqldump') is None:
        print("ERROR: mysqldump not found on PATH; cannot back up MySQL.")
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    db_name = app.config['SQLALCHEMY_DATABASE_NAME']
    dump_path = os.path.join(BACKUP_DIR, f"{db_name}_{timestamp}.sql")

    cmd = [
        'mysqldump',
        f"--host={app.config['DB_ENDPOINT']}",
        '--port=3306',
        f"--user={app.config['DB_USERNAME']}",
        '--single-transaction', '--routines', '--triggers',
        # RDS's admin user has no RELOAD privilege; without this, mysqldump 8 on a GTID
        # server runs FLUSH TABLES WITH READ LOCK, is refused, and no dump is written.
        '--set-gtid-purged=OFF',
        db_name,
    ]
    env = dict(os.environ, MYSQL_PWD=app.config['DB_PASSWORD'])

    print(f"Backing up MySQL database to {dump_path} ...")
    with open(dump_path, 'w') as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, env=env)

    if result.returncode != 0:
        print(f"ERROR: mysqldump failed: {result.stderr.decode(errors='replace').strip()[:400]}")
        if os.path.exists(dump_path):
            os.remove(dump_path)
        return None

    size = os.path.getsize(dump_path)
    print(f"MySQL database backed up to {dump_path} ({size} bytes)")
    print(f"Roll back with: mysql -h {app.config['DB_ENDPOINT']} "
          f"-u {app.config['DB_USERNAME']} -p {db_name} < {dump_path}")
    return dump_path


def decide(to_create, plan):
    """CURRENT, IN_PLACE or REBUILD for a compare() + plan_columns() result."""
    if any(sql is None for cols in plan.values() for _, sql, _ in cols):
        return REBUILD
    if to_create or plan:
        return IN_PLACE
    return CURRENT


def verify_schema():
    """What is still wrong after a change, as a list of messages; empty when the database matches the models."""
    to_create, missing = compare()[:2]
    problems = [f"table {t} is missing" for t in to_create]
    problems += [f"column {t}.{c} is missing" for t, cols in missing.items() for c in cols]
    with db.engine.connect() as conn:
        for table in db.metadata.tables.values():
            if table.name in to_create:
                continue
            for col in table.columns:
                if col.nullable or col.primary_key or col.name in missing.get(table.name, []):
                    continue
                n = conn.execute(sa.select(sa.func.count()).select_from(table).where(col.is_(None))).scalar()
                if n:
                    problems.append(f"{n} NULL(s) in NOT NULL column {table.name}.{col.name}")
    return problems
