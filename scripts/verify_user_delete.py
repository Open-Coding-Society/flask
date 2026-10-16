#!/usr/bin/env python3
"""Verify that deleting a user really deletes them, or fails loudly.

Runs the real app against a throwaway SQLite database with foreign keys ENFORCED, the way production
MySQL does (SQLite ignores them by default, which hides the bug). Kasm is faked. Checksums the real
dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_user_delete.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, sys, tempfile
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-user-delete")

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


class FakeKasm:
    deleted = []

    def delete(self, uid):
        FakeKasm.deleted.append(uid)


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    import __init__ as pkg
    from sqlalchemy import create_engine, event, text
    app, db = pkg.app, pkg.db
    engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='userdelete-'), 'new.db')}")

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    with app.app_context():
        db.engines[None] = engine
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User
    from model.post import Post
    from model.microblog import MicroBlog
    from model.persona import Persona, UserPersona
    app.config["TESTING"] = True

    with app.app_context():
        db.create_all()
        persona = Persona("alias", "student", {"x": 1})
        db.session.add(persona)
        for uid, role in (("zz_admin", "Admin"), ("zz_student", "User"), ("zz_plain", "User"),
                          ("zz_social", "User"), ("zz_social2", "User"), ("zz_kasm", "User"), ("zz_blocked", "User"), ("zz_bystander", "User")):
            db.session.add(User(name=uid, uid=uid, password=PASSWORD, role=role))
        db.session.commit()
        for u in User.query.all():
            u.activate()
        ids = {u.uid: u.id for u in User.query.all()}
        for uid in ("zz_social", "zz_social2", "zz_bystander"):
            db.session.add_all([MicroBlog(ids[uid], f"{uid} one"), MicroBlog(ids[uid], f"{uid} two")])
            db.session.add(UserPersona(User.query.get(ids[uid]), persona, 1))
        db.session.add(MicroBlog(ids["zz_blocked"], "blocked user's microblog"))
        db.session.add(Post(user_id=ids["zz_blocked"], content="a post that blocks the delete"))
        db.session.commit()

    def exists(uid):
        with app.app_context():
            return User.query.filter_by(_uid=uid).first() is not None

    def rows(table, column, uid):
        with app.app_context():
            return db.session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :i"), {"i": ids[uid]}).scalar()

    def login_api(c, uid):
        assert c.post("/api/authenticate", json={"uid": uid, "password": PASSWORD}).status_code == 200

    with patch("model.user.KasmUser", FakeKasm):
        # API: DELETE /api/user
        with app.test_client() as c:
            login_api(c, "zz_admin")
            r = c.delete("/api/user", json={"uid": "zz_plain"})
            check("API: a user with no related data is deleted (204)", r.status_code == 204 and not exists("zz_plain"), r.status_code)
            check("the Kasm account is cleaned up once the user is really gone", FakeKasm.deleted == ["zz_plain"], str(FakeKasm.deleted))

            r = c.delete("/api/user", json={"uid": "zz_social"})
            check("API: a user whose only related data is microblogs and a persona link IS deleted (204)",
                  r.status_code == 204 and not exists("zz_social"), r.status_code)
            check("their own microblogs and persona link went with them",
                  rows("microblogs", "_user_id", "zz_social") == 0 and rows("user_personas", "user_id", "zz_social") == 0)
            check("another user's microblogs and persona are untouched",
                  rows("microblogs", "_user_id", "zz_bystander") == 2 and rows("user_personas", "user_id", "zz_bystander") == 1)

            kasm_before = list(FakeKasm.deleted)
            r = c.delete("/api/user", json={"uid": "zz_blocked"})
            check("API: a user with a post is NOT deleted, and the answer says so (409, not 204)",
                  r.status_code == 409 and exists("zz_blocked") and "related records" in r.get_data(as_text=True), r.status_code)
            check("the refusal changes nothing: their microblog is still there (one transaction)",
                  rows("microblogs", "_user_id", "zz_blocked") == 1 and rows("posts", "_user_id", "zz_blocked") == 1)
            check("no Kasm cleanup happens for a delete that did not happen", FakeKasm.deleted == kasm_before)
            check("an unknown uid is 404", c.delete("/api/user", json={"uid": "zz_nobody"}).status_code == 404)

            class BrokenKasm:
                def delete(self, uid):
                    raise RuntimeError("kasm is down")
            with patch("model.user.KasmUser", BrokenKasm):
                r = c.delete("/api/user", json={"uid": "zz_kasm"})
            check("if Kasm fails after the user is deleted, the delete still succeeds (204, not a 500)",
                  r.status_code == 204 and not exists("zz_kasm"), r.status_code)

        with app.test_client() as c:
            login_api(c, "zz_student")
            check("a non-admin is still refused on the API (403)", c.delete("/api/user", json={"uid": "zz_bystander"}).status_code == 403
                  and exists("zz_bystander"))

        # The user table's Delete button: DELETE /users/delete/<id>
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_admin", "password": PASSWORD})
            r = c.delete(f"/users/delete/{ids['zz_social2']}")
            check("table: a deletable user is deleted (200)", r.status_code == 200 and not exists("zz_social2"), r.status_code)
            r = c.delete(f"/users/delete/{ids['zz_blocked']}")
            body = r.get_json() or {}
            check("table: a blocked user is NOT reported as deleted (409 with an error message)",
                  r.status_code == 409 and "related records" in body.get("error", "") and exists("zz_blocked"), f"{r.status_code} {body}")
            check("table: an unknown id is 404", c.delete("/users/delete/999999").status_code == 404)

    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
