#!/usr/bin/env python3
"""Verify that the Kasm attribute and code are gone and nothing depends on them.

Runs the real app against a throwaway SQLite database, including one that still has the old
kasm_server_needed column (as production does), and checksums the real dev DB before and after to
prove it was never touched.

Usage:  python scripts/verify_kasm_removed.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, importlib.util, os, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-kasm-removed")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Passw0rd!123"
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class FakeGitHub:
    def get(self, uid):
        return {}, 200


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    import __init__ as pkg
    from sqlalchemy import create_engine, text
    from unittest.mock import patch
    app, db = pkg.app, pkg.db
    engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='nokasm-'), 'new.db')}")
    with app.app_context():
        db.engines[None] = engine
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User, initUsers
    app.config["TESTING"] = True

    check("the Kasm module is gone", importlib.util.find_spec("model.kasm") is None)
    check("no Kasm settings are left in the app config", not any(k.startswith("KASM") for k in app.config))

    with app.app_context():
        db.create_all()
        # production still has the old column; it is nullable, so leave it in place and unused
        db.session.execute(text("ALTER TABLE users ADD COLUMN kasm_server_needed BOOLEAN"))
        db.session.add(User(name="Adminxx", uid="zz_admin", password=PASSWORD, role="Admin"))
        db.session.commit()
        User.query.filter_by(_uid="zz_admin").first().activate()
        user = User(name="Plain", uid="zz_plain", password=PASSWORD)
        db.session.add(user)
        db.session.commit()
        check("the attribute is gone from the model and from read()",
              not hasattr(user, "kasm_server_needed") and "kasm_server_needed" not in user.read())
        left = db.session.execute(text("SELECT kasm_server_needed FROM users WHERE _uid = 'zz_plain'")).scalar()
        check("a database that still has the old column works: new users get NULL in it", left is None, str(left))

    def login(c, uid):
        assert c.post("/api/authenticate", json={"uid": uid, "password": PASSWORD}).status_code == 200

    with patch("api.user.GitHubUser", FakeGitHub):
        with app.test_client() as c:
            r = c.post("/api/user", json={"uid": "zz_signup", "name": "Sign Up", "password": PASSWORD,
                                          "accountType": "mentor", "kasm_server_needed": True})
            check("a request that still sends kasm_server_needed is accepted and the field ignored",
                  r.status_code == 200 and "kasm_server_needed" not in r.get_json(), f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            r = c.post("/api/user/guest", json={"uid": "zz_guest", "password": PASSWORD})
            check("guest signup works", r.status_code == 200, r.status_code)
        with app.test_client() as c:
            login(c, "zz_admin")
            r = c.put("/api/user", json={"uid": "zz_plain", "name": "Plain Renamed", "kasm_server_needed": True})
            check("updating a user, even with the old field in the request, works",
                  r.status_code == 200 and r.get_json()["name"] == "Plain Renamed", r.status_code)
            backup = [dict(name="Old Backup", uid="zz_old", password=PASSWORD, role="User", kasm_server_needed=True)]
            r = c.post("/api/export/import/users", json={"users": backup})
            result = (r.get_json() or {}).get("users", {})
            check("restoring a backup whose users carry the old field works (0 failed)",
                  r.status_code == 200 and result.get("failed") == 0 and result.get("imported") == 1, str(result))
            r = c.get("/api/user")
            check("the user list has no Kasm field", r.status_code == 200 and "kasm_server_needed" not in r.get_data(as_text=True))
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_admin", "password": PASSWORD})
            check("the Kasm report page is gone (404)", c.get("/kasm_users").status_code == 404)
            check("the Kasm delete route is gone (404)", c.delete("/delete_user/1").status_code == 404)
            page = c.get("/users/table2")
            html = page.get_data(as_text=True)
            check("the user table renders with no Kasm column, and the menu has no Kasm report",
                  page.status_code == 200 and 'id="userTable"' in html and "kasm" not in html.lower(),
                  f"{page.status_code} table={'id=\"userTable\"' in html} kasm={'kasm' in html.lower()}")

    with app.app_context():
        initUsers()
        check("seeding the database still works", User.query.count() >= 4, str(User.query.count()))

    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
