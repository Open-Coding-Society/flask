#!/usr/bin/env python3
"""Verify the account activation model end-to-end through the real request stack.

Runs against a throwaway SQLite DB created from the current models (and a second
throwaway DB with the OLD schema to rehearse the migration). The real dev DB is
checksummed before and after to prove it was never touched. Google and GitHub are
faked, so no network is needed.

Usage:  python scripts/verify_activation.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib
import os
import sys
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-activation")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Passw0rd!123"
CLIENT_ID = "test-client-id"
GOOD_TOKEN = "good-student-token"

_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def fake_tokeninfo(url, params=None, timeout=None):
    """Stand-in for Google's tokeninfo endpoint."""
    token = (params or {}).get("id_token")
    claims = {
        GOOD_TOKEN: dict(aud=CLIENT_ID, iss="https://accounts.google.com", email_verified="true",
                         email="kid@stu.powayusd.com"),
        "wrong-aud": dict(aud="someone-else", iss="https://accounts.google.com", email_verified="true",
                          email="kid@stu.powayusd.com"),
        "wrong-iss": dict(aud=CLIENT_ID, iss="https://evil.example", email_verified="true",
                          email="kid@stu.powayusd.com"),
        "unverified": dict(aud=CLIENT_ID, iss="accounts.google.com", email_verified="false",
                           email="kid@stu.powayusd.com"),
        "not-student": dict(aud=CLIENT_ID, iss="accounts.google.com", email_verified="true",
                            email="someone@gmail.com"),
    }.get(token)
    return FakeResponse(200, claims) if claims else FakeResponse(400, {"error": "invalid_token"})


class FakeGitHub:
    def get(self, uid):
        return {}, 200


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    tmpdir = tempfile.mkdtemp(prefix="activation-")
    new_db = os.path.join(tmpdir, "new.db")

    import __init__ as pkg
    from sqlalchemy import create_engine

    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{new_db}")

    import main  # noqa: F401  registers every model + api blueprint
    from model.user import User

    app.config["TESTING"] = True
    app.config["GOOGLE_CLIENT_ID"] = CLIENT_ID

    with app.app_context():
        assert str(db.engine.url).endswith("new.db"), db.engine.url
        db.create_all()
        now = datetime.utcnow()
        for uid, role in (("zz_admin", "Admin"), ("zz_teacher", "Teacher"), ("zz_student", "User")):
            db.session.add(User(name=uid, uid=uid, password=PASSWORD, role=role,
                                active=True, last_verified=now))
        db.session.commit()

    def get_user(uid):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            return None if u is None else dict(role=u.role, active=u.active, last_verified=u.last_verified,
                                               state=u.verification_state, email=u.email)

    def login(client, uid):
        return client.post("/api/authenticate", json={"uid": uid, "password": PASSWORD})

    def signup(client, uid, **extra):
        body = dict(name=f"Name {uid}", uid=uid, password=PASSWORD, email=f"{uid}@stu.powayusd.com")
        body.update(extra)
        return client.post("/api/user", json=body)

    with patch("api.user.GitHubUser", FakeGitHub), \
         patch("model.google_token.requests.get", fake_tokeninfo):

        # --- signup defaults ---------------------------------------------------------------
        with app.test_client() as c:
            r = signup(c, "zz_mentor", accountType="mentor")
            check("mentor signup -> 200", r.status_code == 200, f"got {r.status_code}")
            m = get_user("zz_mentor")
            check("mentor is role Mentor and inactive", m["role"] == "Mentor" and m["active"] is False, str(m))

            signup(c, "zz_mentor_tok", accountType="mentor", idToken=GOOD_TOKEN)
            m = get_user("zz_mentor_tok")
            check("mentor with a valid student token is still inactive", m["active"] is False, str(m))

            signup(c, "zz_good", accountType="student", idToken=GOOD_TOKEN)
            g = get_user("zz_good")
            check("student with valid token is active and verified now",
                  g["active"] and g["state"] == "active_verified" and g["last_verified"] is not None, str(g))
            check("verified email comes from Google", g["email"] == "kid@stu.powayusd.com", g["email"])

            signup(c, "zz_noacct", idToken=GOOD_TOKEN)
            check("no accountType but valid token is active", get_user("zz_noacct")["active"] is True)

            for label, tok in (("claimed email, no token", None), ("wrong audience", "wrong-aud"),
                               ("wrong issuer", "wrong-iss"), ("unverified email", "unverified"),
                               ("non-student email", "not-student"), ("garbage token", "garbage")):
                uid = "zz_" + label.replace(" ", "_").replace(",", "")
                extra = {"idToken": tok} if tok else {}
                signup(c, uid, accountType="student", **extra)
                u = get_user(uid)
                check(f"student signup with {label} stays inactive", u is not None and u["active"] is False, str(u))

            app.config["GOOGLE_CLIENT_ID"] = None
            signup(c, "zz_noclient", accountType="student", idToken=GOOD_TOKEN)
            check("no GOOGLE_CLIENT_ID configured -> nothing auto-activates",
                  get_user("zz_noclient")["active"] is False)
            app.config["GOOGLE_CLIENT_ID"] = CLIENT_ID

            r = c.post("/api/user/guest", json={"uid": "zz_guest", "password": PASSWORD})
            guest = get_user("zz_guest")
            check("guest creation is inactive", guest is not None and guest["active"] is False,
                  f"{r.status_code} {guest}")

            r = c.post("/api/users", json=[{"name": "Bulk One", "uid": "zz_bulk"}])
            bulk = get_user("zz_bulk")
            check("bulk creation is inactive", bulk is not None and bulk["active"] is False, f"{r.status_code} {bulk}")

        # --- enforcement ---------------------------------------------------------------------
        with app.test_client() as c:
            r = login(c, "zz_mentor")
            check("inactive account can log in", r.status_code == 200, f"got {r.status_code}")
            body = r.get_json() or {}
            check("login response reports state", body.get("user", {}).get("verification_state") == "inactive"
                  and body["user"]["active"] is False, str(body))
            check("inactive account blocked on protected endpoint", c.get("/api/id").status_code == 403)
            r = c.get("/api/id")
            check("block message says pending verification", "pending verification" in (r.get_data(as_text=True)))
            check("inactive account can still log out", c.delete("/api/authenticate").status_code == 200)

        with app.test_client() as c:
            check("active account passes protected endpoint", (login(c, "zz_student").status_code == 200)
                  and c.get("/api/id").status_code == 200)

        # --- verify / deactivate ---------------------------------------------------------
        with app.test_client() as c:
            login(c, "zz_teacher")
            r = c.post("/api/user/zz_mentor/verify")
            check("teacher verifies a mentor -> 200", r.status_code == 200, f"got {r.status_code}")
            m = get_user("zz_mentor")
            check("verify activates and stamps last_verified",
                  m["active"] and m["last_verified"] is not None and m["state"] == "active_verified", str(m))
            check("verified mentor now passes a protected endpoint", (login(app.test_client(), "zz_mentor").status_code == 200))
            check("unknown uid -> 404", c.post("/api/user/zz_nobody/verify").status_code == 404)

            r = c.post("/api/user/zz_mentor/deactivate")
            check("teacher deactivates -> 200", r.status_code == 200, f"got {r.status_code}")
            m = get_user("zz_mentor")
            check("deactivate keeps the account and its data", m is not None and m["active"] is False
                  and m["role"] == "Mentor" and m["last_verified"] is not None, str(m))
            with app.test_client() as c2:
                login(c2, "zz_mentor")
                check("deactivated account is blocked again", c2.get("/api/id").status_code == 403)

            check("teacher cannot deactivate an admin", c.post("/api/user/zz_admin/deactivate").status_code == 403)
            check("teacher cannot verify an admin", c.post("/api/user/zz_admin/verify").status_code == 403)
            check("admin still active", get_user("zz_admin")["active"] is True)

            r = c.post("/api/user/admin-create", json=dict(name="Staff Made", uid="zz_made", password=PASSWORD,
                                                           accountType="mentor"))
            made = get_user("zz_made")
            check("teacher creates a mentor, inactive", r.status_code == 200 and made["role"] == "Mentor"
                  and made["active"] is False, f"{r.status_code} {made}")
            r = c.post("/api/user/admin-create", json=dict(name="Staff Kid", uid="zz_madekid", password=PASSWORD,
                                                           accountType="student"))
            check("teacher creates a student, inactive", r.status_code == 200
                  and get_user("zz_madekid")["role"] == "User" and get_user("zz_madekid")["active"] is False)
            for bad in ("teacher", "admin", "observer", ""):
                r = c.post("/api/user/admin-create", json=dict(name="Bad Role", uid="zz_bad_" + (bad or "empty"),
                                                               password=PASSWORD, accountType=bad))
                check(f"admin-create refuses accountType '{bad}'", r.status_code == 400
                      and get_user("zz_bad_" + (bad or "empty")) is None, f"got {r.status_code}")
            check("admin-create needs a password",
                  c.post("/api/user/admin-create", json=dict(name="No Pw", uid="zz_nopw",
                                                             accountType="student")).status_code == 400)

        with app.test_client() as c:
            login(c, "zz_admin")
            check("admin can deactivate a teacher", c.post("/api/user/zz_teacher/deactivate").status_code == 200)
            check("admin can verify a teacher again", c.post("/api/user/zz_teacher/verify").status_code == 200)

        with app.test_client() as c:
            login(c, "zz_student")
            for path in ("/api/user/zz_mentor/verify", "/api/user/zz_mentor/deactivate", "/api/user/admin-create"):
                r = c.post(path, json=dict(name="X Y", uid="zz_sneaky", password=PASSWORD, accountType="student"))
                check(f"non-staff blocked on {path}", r.status_code == 403, f"got {r.status_code}")

        # --- self-service cannot activate -----------------------------------------------
        with app.test_client() as c:
            login(c, "zz_noclient")
            r = c.put("/api/user", json={"active": True, "last_verified": "2030-01-01T00:00:00"})
            check("an inactive user cannot self-update", r.status_code == 403, f"got {r.status_code}")
        with app.app_context():
            u = User.query.filter_by(_uid="zz_student").first()
            u.update({"active": False, "last_verified": "2000-01-01T00:00:00"})
            db.session.commit()
            check("update() ignores active and last_verified", u.active is True)

        # --- the 365 day boundary --------------------------------------------------------
        with app.app_context():
            probe = User(name="Probe", uid="zz_probe", password=PASSWORD)
            probe.active = True
            probe.last_verified = datetime.utcnow() - timedelta(days=364)
            check("364 days -> active_verified", probe.verification_state == "active_verified")
            probe.last_verified = datetime.utcnow() - timedelta(days=365, seconds=1)
            check("just over 365 days -> verification required", probe.verification_state == "active_verification_required")
            probe.last_verified = None
            check("active with no last_verified -> verification required",
                  probe.verification_state == "active_verification_required")
            probe.active = False
            check("inactive -> inactive", probe.verification_state == "inactive")

    # --- migration rehearsal on an OLD-schema database ----------------------------------
    from sqlalchemy import text, inspect
    from scripts.migrate_activation import migrate
    old_db = os.path.join(tmpdir, "old.db")
    old_engine = create_engine(f"sqlite:///{old_db}")
    with old_engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, _uid VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO users (_uid) VALUES ('legacy_a'), ('legacy_b')"))
    check("dry run reports three steps and changes nothing", len(migrate(old_engine, False)) == 3
          and "active" not in {c["name"] for c in inspect(old_engine).get_columns("users")})
    check("apply runs the migration", len(migrate(old_engine, True)) == 3)
    with old_engine.connect() as conn:
        rows = conn.execute(text("SELECT active, last_verified FROM users")).fetchall()
    check("every existing account is active and verified 2026-01-30",
          len(rows) == 2 and all(r[0] == 1 and str(r[1]).startswith("2026-01-30") for r in rows), str(rows))
    with old_engine.begin() as conn:
        conn.execute(text("UPDATE users SET active = 0 WHERE _uid = 'legacy_a'"))
    check("running it again does nothing", migrate(old_engine, True) == [])
    with old_engine.connect() as conn:
        left = conn.execute(text("SELECT active FROM users WHERE _uid = 'legacy_a'")).scalar()
    check("a deactivated account is not re-activated by a second run", left == 0)

    shutil.rmtree(tmpdir, ignore_errors=True)
    if real_before:
        check("real dev DB untouched", sha256(REAL_DB) == real_before)

    print("\n" + ("ALL PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
