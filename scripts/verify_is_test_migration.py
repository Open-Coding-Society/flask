#!/usr/bin/env python3
"""Verify the is_test column migration on an old-shaped database.

Builds a throwaway SQLite database, removes the is_test column to look like production before this
change, and checks that:
  - the new code needs the column (it fails without it),
  - the ALTER TABLE documented in model/user.py fixes that and every existing account gets false,
  - code that does not know about the column keeps working once it exists (so the ALTER can run
    BEFORE the deploy),
  - running the ALTER a second time fails cleanly and changes nothing.
The real dev DB is checksummed before and after. MySQL is not tested here.

Usage:  python scripts/verify_is_test_migration.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, re, secrets, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-is-test-migration")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run; the accounts live only in a throwaway database
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def documented_sql():
    """The ALTER TABLE statement written above the column in model/user.py."""
    with open(os.path.join(REPO, "model", "user.py"), encoding="utf-8") as fh:
        found = re.findall(r"^\s*#\s+(ALTER TABLE users ADD COLUMN is_test [^;]*);", fh.read(), re.M)
    return found


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    import __init__ as pkg
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    app, db = pkg.app, pkg.db
    engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='istest-'), 'old.db')}")
    with app.app_context():
        db.engines[None] = engine
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User

    statements = documented_sql()
    check("model/user.py documents exactly one ALTER TABLE for is_test", len(statements) == 1, str(statements))
    if len(statements) != 1:
        return finish(real_before)
    migration = statements[0]

    with app.app_context():
        db.create_all()
        for uid, role in (("zz_admin", "Admin"), ("zz_student", "User"), ("zz_mentor", "Mentor")):
            db.session.add(User(name=uid, uid=uid, password=PASSWORD, role=role))
        db.session.commit()
        db.session.execute(text("ALTER TABLE users DROP COLUMN is_test"))  # production before this change
        db.session.commit()
        columns = [row[1] for row in db.session.execute(text("PRAGMA table_info(users)")).fetchall()]
        check("the test database looks like production before the change: no is_test column", "is_test" not in columns, str(columns))

    with app.app_context():
        try:
            User.query.all()
            failed_without = False
        except OperationalError as e:
            failed_without = "is_test" in str(e)
            db.session.rollback()
        check("the new code needs the column: without it, reading users fails", failed_without)

    with app.app_context():
        db.session.execute(text(migration))
        db.session.commit()
        users = User.query.all()
        check("after the documented ALTER the new code reads every user", len(users) == 3, str(len(users)))
        check("every existing account is not a test account", all(u.is_test is False for u in users), str([(u.uid, u.is_test) for u in users]))

        db.session.execute(text("INSERT INTO users (_name, _uid, _email, _password, _role, token_version, active) "
                                "VALUES ('Old Code', 'zz_old_code', '?', 'x', 'User', 0, 0)"))  # an insert from code that does not know is_test
        db.session.commit()
        row = db.session.execute(text("SELECT is_test FROM users WHERE _uid = 'zz_old_code'")).fetchone()
        check("code that does not know the column can still add users, and they are not test accounts", row is not None and row[0] == 0, str(row))

        try:
            db.session.execute(text(migration))
            db.session.commit()
            twice_ok = False
        except OperationalError as e:
            db.session.rollback()
            twice_ok = "duplicate column" in str(e).lower()
        count = db.session.execute(text("SELECT COUNT(*) FROM users")).scalar()
        check("running the ALTER a second time fails with 'duplicate column' and changes nothing", twice_ok and count == 4, f"{twice_ok} {count}")

    app.config["TESTING"] = True
    with app.test_client() as c:
        from unittest.mock import patch

        class NoGitHub:
            def get(self, uid):
                return ({}, 404)
        with patch("api.user.GitHubUser", NoGitHub):
            with app.app_context():
                User.query.filter_by(_uid="zz_admin").first().activate()
            logged_in = c.post("/api/authenticate", json={"uid": "zz_admin", "password": PASSWORD}).status_code
            r = c.post("/api/user", json={"uid": "zz_fake_after", "name": "After", "password": PASSWORD,
                                          "accountType": "student", "isTest": True})
            check("on the migrated database an Admin can log in and create a test account",
                  logged_in == 200 and r.status_code == 200 and r.get_json().get("is_test") is True,
                  f"login {logged_in}, signup {r.status_code} {r.get_data(as_text=True)[:100]}")
    return finish(real_before)


def finish(real_before):
    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
