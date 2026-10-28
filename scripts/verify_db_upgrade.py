#!/usr/bin/env python3
"""Verify that db_upgrade.py adds missing columns in place, and refuses the unsafe ones.

Builds a throwaway SQLite database with real accounts, removes columns and a table to give
it an older shape, then runs db_upgrade.py against copies of it. Each run is its own
process, so a column added to the models for one case can't leak into the next.
Checksums the real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_db_upgrade.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, json, os, secrets, shutil, sqlite3, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run; the accounts live only in throwaway databases
UIDS = ["zz_one", "zz_two", "zz_three"]
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"\n{detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def child():
    """Runs in a subprocess: point the app at one database, then seed it or run db_upgrade."""
    spec = json.loads(os.environ["VERIFY_DB_UPGRADE"])
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    os.chdir(REPO)
    import sqlalchemy as sa
    import __init__ as pkg
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = sa.create_engine(f"sqlite:///{spec['db']}")
    import main  # noqa: F401  registers every model
    from model.user import User

    if spec.get("seed"):
        with app.app_context():
            db.create_all()
            for uid in UIDS:
                User(name=uid, uid=uid, password=PASSWORD).create()
                User.query.filter_by(_uid=uid).first().activate()
        return 0

    users = db.metadata.tables["users"]
    for name, kind in spec.get("extra", []):
        users.append_column({
            "required": sa.Column(name, sa.String(10), nullable=False),
            "unique": sa.Column(name, sa.String(10), nullable=True, unique=True),
            "foreign": sa.Column(name, sa.Integer, sa.ForeignKey("sections.id"), nullable=True),
            "timestamp": sa.Column(name, sa.DateTime, nullable=False, default=sa.func.now()),
        }[kind])
    import db_upgrade
    db_upgrade.BACKUP_DIR = spec["backups"]
    if spec.get("break"):
        real_plan = db_upgrade.plan_columns
        def broken_plan(missing, fills):
            plan = real_plan(missing, fills)
            plan["users"].append(("zz_broken", "ALTER TABLE no_such_table ADD COLUMN x INTEGER NULL", None))
            return plan
        db_upgrade.plan_columns = broken_plan
    sys.argv = ["db_upgrade.py"] + spec.get("args", [])
    return db_upgrade.main()


def run(db, **spec):
    env = dict(os.environ, VERIFY_DB_UPGRADE=json.dumps(dict(spec, db=db)),
               SECRET_KEY=os.environ.get("SECRET_KEY", "verify-db-upgrade"))
    r = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def query(db, sql):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def columns(db, table):
    return {row[1] for row in query(db, f"PRAGMA table_info({table})")}


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    tmp = tempfile.mkdtemp(prefix="db-upgrade-")
    backups = os.path.join(tmp, "backups")

    # An older production: no lifecycle columns, no token_version, no presence_events table.
    old = os.path.join(tmp, "old.db")
    code, out = run(old, seed=True, backups=backups)
    check("seed a throwaway database", code == 0, out[-300:])
    with sqlite3.connect(old) as conn:
        for col in ("active", "last_verified", "token_version"):
            conn.execute(f"ALTER TABLE users DROP COLUMN {col}")
        conn.execute("DROP TABLE presence_events")
    users_before = query(old, "SELECT COUNT(*) FROM users")[0][0]
    check("old shape has the seeded accounts", users_before >= len(UIDS), str(users_before))

    def fresh(name):
        path = os.path.join(tmp, name)
        shutil.copy(old, path)
        return path

    # --check shows the SQL and changes nothing
    db = fresh("check.db")
    before = sha256(db)
    code, out = run(db, backups=backups, args=["--check"])
    check("--check exits 2 with changes to make", code == 2, out[-300:])
    check("--check prints the ALTER for each column",
          all(f"ADD COLUMN {c}" in out for c in ("active", "last_verified", "token_version")))
    check("--check changes nothing", sha256(db) == before)

    # upgrade with --fill: existing accounts stay active
    db = fresh("upgrade.db")
    code, out = run(db, backups=backups, args=["--fill", "users.active=1"])
    check("upgrade exits 0", code == 0, out[-400:])
    check("columns added", {"active", "last_verified", "token_version"} <= columns(db, "users"))
    check("missing table created", query(db, "SELECT name FROM sqlite_master WHERE name='presence_events'") != [])
    check("account count unchanged", query(db, "SELECT COUNT(*) FROM users")[0][0] == users_before)
    check("--fill kept every existing account active", query(db, "SELECT MIN(active), MAX(active) FROM users")[0] == (1, 1))
    check("token_version got its default", query(db, "SELECT COUNT(*) FROM users WHERE token_version = 0")[0][0] == users_before)
    check("last_verified left NULL", query(db, "SELECT COUNT(*) FROM users WHERE last_verified IS NULL")[0][0] == users_before)
    check("a rollback point was taken", os.path.isdir(backups) and len(os.listdir(backups)) == 1)
    code, out = run(db, backups=backups)
    check("second run is up to date", code == 0 and "Up to date" in out, out[-200:])

    # without --fill the model default applies (active = 0); --check says so in the SQL
    db = fresh("nofill.db")
    code, out = run(db, backups=backups, args=["--check"])
    check("--check shows existing rows would get active = 0", "ADD COLUMN active BOOLEAN DEFAULT 0 NOT NULL" in out)

    # refused, changing nothing
    for kind in ("required", "unique", "foreign", "timestamp"):
        db = fresh(f"refuse_{kind}.db")
        before = sha256(db)
        code, out = run(db, backups=backups, extra=[["zz_new", kind]])
        check(f"refuses a {kind} column", code == 1 and "CANNOT ADD" in out and "users.zz_new" in out, out[-300:])
        check(f"refusing a {kind} column changes nothing", sha256(db) == before)

    # --fill makes a NOT NULL column with no fixed default addable
    for kind, value in (("required", "x"), ("timestamp", "2026-01-30 00:00:00")):
        db = fresh(f"fill_{kind}.db")
        code, out = run(db, backups=backups, extra=[["zz_new", kind]], args=["--fill", "users.active=1", "--fill", f"users.zz_new={value}"])
        check(f"--fill adds a NOT NULL {kind} column", code == 0, out[-300:])
        check(f"every existing row got the {kind} fill",
              query(db, "SELECT COUNT(*) FROM users WHERE zz_new IS NULL")[0][0] == 0
              and query(db, "SELECT COUNT(*) FROM users")[0][0] == users_before)

    # bad --fill values are refused before anything happens
    for bad in ("users.active=maybe", "users.uid=1", "users.active", "nope.active=1"):
        db = fresh("badfill.db")
        before = sha256(db)
        code, out = run(db, backups=backups, args=["--fill", bad])
        check(f"refuses --fill {bad}", code == 1 and "Refusing" in out and sha256(db) == before, out[-200:])

    # a step that fails part way reports what already ran
    db = fresh("broken.db")
    code, out = run(db, backups=backups, args=["--fill", "users.active=1"], **{"break": True})
    check("a failing step exits 1", code == 1, out[-300:])
    check("the failure names the step and what already ran",
          "FAILED on: ALTER TABLE no_such_table" in out and "Already applied:" in out and "ADD COLUMN active" in out, out[-500:])

    shutil.rmtree(tmp, ignore_errors=True)
    real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev database untouched", real_before == real_after)
    print(f"\n{'ALL CHECKS PASSED' if not _failures else f'{len(_failures)} CHECK(S) FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(child() if "VERIFY_DB_UPGRADE" in os.environ else main())
