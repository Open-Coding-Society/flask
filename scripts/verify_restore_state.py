#!/usr/bin/env python3
"""Verify that a backup restore keeps every account's active/verified state.

Exports users from one throwaway SQLite DB through the real /api/export/users endpoint,
then imports them into fresh DBs both ways a restore can go: the /api/export/import/users
endpoint (db_restore-sqlite2prod.py) and load_users() (db_migrate-prod2sqlite.py).
Checksums the real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_restore_state.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, importlib.util, os, secrets, shutil, sys, tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.environ.setdefault("SECRET_KEY", "verify-restore-state")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run; the accounts live only in throwaway databases
ADMIN = "zz_admin"
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


class FakeGitHub:
    def get(self, uid):
        return {}, 200


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    tmp = tempfile.mkdtemp(prefix="restore-state-")

    import __init__ as pkg
    from sqlalchemy import create_engine
    app, db = pkg.app, pkg.db

    def use_db(name):
        with app.app_context():
            db.engines[None] = create_engine(f"sqlite:///{os.path.join(tmp, name)}")
            db.create_all()

    use_db("source.db")
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User
    app.config["TESTING"] = True

    def add_admin():
        with app.app_context():
            db.create_all()
            User(name=ADMIN, uid=ADMIN, password=PASSWORD, role="Admin").create()
            User.query.filter_by(_uid=ADMIN).first().activate()

    def states():
        with app.app_context():
            return {u.uid: (u.active, u.last_verified, u.verification_state)
                    for u in User.query.all() if u.uid != ADMIN}

    def login(c):
        r = c.post("/api/authenticate", json={"uid": ADMIN, "password": PASSWORD})
        assert r.status_code == 200, r.status_code

    # source: one account in each state
    add_admin()
    lapsed_at = datetime.utcnow() - timedelta(days=400)
    with app.app_context():
        for uid in ("zz_active", "zz_inactive", "zz_lapsed", "zz_never"):
            User(name=uid, uid=uid, password=PASSWORD).create()
        User.query.filter_by(_uid="zz_active").first().activate()
        u = User.query.filter_by(_uid="zz_inactive").first(); u.activate(); u.deactivate()
        u = User.query.filter_by(_uid="zz_lapsed").first(); u.active, u.last_verified = True, lapsed_at
        db.session.commit()
    before = states()
    check("source has an active, an inactive, a lapsed and a never-verified account",
          [before[k][2] for k in ("zz_active", "zz_inactive", "zz_lapsed", "zz_never")]
          == ["active_verified", "inactive", "active_verification_required", "inactive"], str(before))

    with patch("api.user.GitHubUser", FakeGitHub):
        with app.test_client() as c:
            login(c)
            r = c.get("/api/export/users?per_page=100")
            exported = [u for u in r.get_json()["users"] if u["uid"] != ADMIN]
        check("export carries active and last_verified", all("active" in u and "last_verified" in u for u in exported))

        # restore path 1: the import endpoint (db_restore-sqlite2prod.py)
        use_db("via_api.db")
        add_admin()
        with app.test_client() as c:
            login(c)
            r = c.post("/api/export/import/users", json={"users": exported})
            check("import endpoint accepts every user", r.status_code == 200 and r.get_json()["users"]["imported"] == 4,
                  str(r.get_json()))
        check("import endpoint keeps every account's state", states() == before, str(states()))

    # restore path 2: load_users() (db_migrate-prod2sqlite.py)
    use_db("via_loader.db")
    spec = importlib.util.spec_from_file_location("prod2sqlite", os.path.join(REPO, "scripts", "db_migrate-prod2sqlite.py"))
    prod2sqlite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prod2sqlite)
    with app.app_context():
        prod2sqlite.load_users(exported)
    check("load_users keeps every account's state", states() == before, str(states()))

    # a backup from before the lifecycle columns has no 'active' key: those accounts were all active
    use_db("old_backup.db")
    old = [{k: v for k, v in u.items() if k not in ("active", "last_verified")} for u in exported]
    with app.app_context():
        prod2sqlite.load_users(old)
    check("an old backup without the keys restores accounts as active",
          all(s[0] is True and s[1] is None for s in states().values()), str(states()))

    shutil.rmtree(tmp, ignore_errors=True)
    if real_before:
        check("real dev DB untouched", sha256(REAL_DB) == real_before)
    print("\n" + ("ALL PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
