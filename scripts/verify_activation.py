#!/usr/bin/env python3
"""Verify account activation end-to-end through the real request stack.

Runs against throwaway SQLite DBs with Google and GitHub faked, and checksums the
real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_activation.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, shutil, sys, tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-activation")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD, CLIENT_ID, GOOD = "Passw0rd!123", "test-client-id", "good-token"
MIGRATION_SQL = [
    "ALTER TABLE users ADD COLUMN active BOOLEAN NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_verified DATETIME NULL",
    "UPDATE users SET active = 1, last_verified = '2026-01-30 00:00:00'",
]
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


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


def fake_tokeninfo(url, params=None, timeout=None):
    good = dict(aud=CLIENT_ID, iss="https://accounts.google.com", email_verified="true", email="kid@stu.powayusd.com")
    claims = {GOOD: good,
              "wrong-aud": {**good, "aud": "other"},
              "wrong-iss": {**good, "iss": "https://evil.example"},
              "unverified": {**good, "email_verified": "false"},
              "not-student": {**good, "email": "someone@gmail.com"}}.get((params or {}).get("id_token"))
    return Resp(200, claims) if claims else Resp(400, {})


class FakeGitHub:
    def get(self, uid):
        return {}, 200


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    tmp = tempfile.mkdtemp(prefix="activation-")

    import __init__ as pkg
    from sqlalchemy import create_engine, text, inspect
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tmp, 'new.db')}")
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User

    app.config["TESTING"] = True
    app.config["GOOGLE_CLIENT_ID"] = CLIENT_ID
    with app.app_context():
        db.create_all()
        for uid, role in (("zz_admin", "Admin"), ("zz_teacher", "Teacher"), ("zz_student", "User")):
            db.session.add(User(name=uid, uid=uid, password=PASSWORD, role=role))
        for u in User.query.all():
            u.activate()

    def get(uid):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            return None if u is None else dict(role=u.role, active=u.active, state=u.verification_state,
                                               last_verified=u.last_verified, email=u.email)

    def login(c, uid):
        r = c.post("/api/authenticate", json={"uid": uid, "password": PASSWORD})
        assert r.status_code == 200, (uid, r.status_code)

    def signup(c, uid, **extra):
        body = dict(name=f"Name {uid}", uid=uid, password=PASSWORD, email=f"{uid}@stu.powayusd.com")
        body.update(extra)
        return c.post("/api/user", json=body)

    with patch("api.user.GitHubUser", FakeGitHub), patch("model.google_token.requests.get", fake_tokeninfo):
        # signup
        with app.test_client() as c:
            signup(c, "zz_mentor", accountType="mentor")
            m = get("zz_mentor")
            check("mentor signup is Mentor and inactive", m["role"] == "Mentor" and m["active"] is False, str(m))
            signup(c, "zz_mentor_tok", accountType="mentor", idToken=GOOD)
            check("mentor with a valid student token is still inactive", get("zz_mentor_tok")["active"] is False)
            signup(c, "zz_good", accountType="student", idToken=GOOD)
            g = get("zz_good")
            check("student with a verified token is active, verified now",
                  g["active"] and g["state"] == "active_verified" and g["last_verified"], str(g))
            signup(c, "zz_personal", accountType="student", idToken=GOOD, email="personal@example.com")
            check("a verified student keeps the personal email they entered",
                  get("zz_personal")["email"] == "personal@example.com", get("zz_personal")["email"])
            for label, tok in (("no token", None), ("wrong audience", "wrong-aud"), ("wrong issuer", "wrong-iss"),
                               ("unverified email", "unverified"), ("non-student email", "not-student"),
                               ("garbage token", "garbage")):
                uid = "zz_" + label.replace(" ", "_").replace("-", "_")
                signup(c, uid, accountType="student", **({"idToken": tok} if tok else {}))
                check(f"student signup with {label} stays inactive", get(uid)["active"] is False)
            app.config["GOOGLE_CLIENT_ID"] = None
            signup(c, "zz_noclient", accountType="student", idToken=GOOD)
            check("no GOOGLE_CLIENT_ID -> nothing auto-activates", get("zz_noclient")["active"] is False)
            app.config["GOOGLE_CLIENT_ID"] = CLIENT_ID

        # inactive means inactive
        with app.test_client() as c:
            login(c, "zz_mentor")
            r = c.get("/api/user")
            check("inactive account is refused on a protected endpoint",
                  r.status_code == 403 and "pending verification" in r.get_data(as_text=True))
            r = c.get("/api/id")
            check("but can read its own state from /api/id, so the page can explain",
                  r.status_code == 200 and r.get_json().get("verification_state") == "inactive"
                  and r.get_json().get("active") is False, str(r.status_code))
        with app.test_client() as c:
            login(c, "zz_student")
            check("active account passes", c.get("/api/id").status_code == 200)
        with app.test_client() as c:
            r = c.post("/login", data={"username": "zz_mentor", "password": PASSWORD})
            check("form login tells an inactive account why", "Account pending verification." in r.get_data(as_text=True))
            check("form login grants an inactive account no session",
                  c.get("/users/table2", follow_redirects=False).status_code == 302)
            r = c.post("/login", data={"username": "zz_mentor", "password": "wrong-password"})
            check("form login keeps the generic message for a bad password",
                  "Invalid username or password." in r.get_data(as_text=True))
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_student", "password": PASSWORD})
            check("form login still works for an active account",
                  c.get("/users/table2", follow_redirects=False).status_code == 200)

        # verify (1a) and admin-create (1b)
        with app.test_client() as c:
            login(c, "zz_teacher")
            r = c.post("/api/user/zz_mentor/verify")
            m = get("zz_mentor")
            check("teacher verify activates and stamps last_verified",
                  r.status_code == 200 and m["active"] and m["state"] == "active_verified", str(m))
            with app.test_client() as c2:
                login(c2, "zz_mentor")
                check("a verified mentor now passes", c2.get("/api/id").status_code == 200)
            check("verifying an unknown uid is 404", c.post("/api/user/zz_nobody/verify").status_code == 404)
            for kind, role in (("mentor", "Mentor"), ("student", "User")):
                r = c.post("/api/user/admin-create", json=dict(name="Staff Made", uid=f"zz_made_{kind}",
                                                               password=PASSWORD, accountType=kind))
                u = get(f"zz_made_{kind}")
                check(f"staff-created {kind} starts inactive", r.status_code == 200 and u["role"] == role
                      and u["active"] is False, f"{r.status_code} {u}")
            for bad in ("teacher", "admin", ""):
                r = c.post("/api/user/admin-create", json=dict(name="Bad Role", uid="zz_bad_" + (bad or "x"),
                                                               password=PASSWORD, accountType=bad))
                check(f"admin-create refuses accountType '{bad}'", r.status_code == 400)
        with app.test_client() as c:
            login(c, "zz_admin")
            check("an admin can verify too", c.post("/api/user/zz_good/verify").status_code == 200)
        with app.test_client() as c:
            login(c, "zz_student")
            for path in ("/api/user/zz_mentor/verify", "/api/user/admin-create"):
                check(f"non-staff refused on {path}", c.post(path, json={}).status_code == 403)

    # update() cannot self-activate
    with app.app_context():
        u = User.query.filter_by(_uid="zz_noclient").first()
        u.update({"active": True, "last_verified": "2030-01-01T00:00:00"})
        check("update() ignores active and last_verified", u.active is False)

        # 365 day interval
        probe = User(name="Probe", uid="zz_probe", password=PASSWORD)
        probe.active = True
        probe.last_verified = datetime.utcnow() - timedelta(days=364)
        check("364 days -> active_verified", probe.verification_state == "active_verified")
        probe.last_verified = datetime.utcnow() - timedelta(days=365, seconds=1)
        check("over 365 days -> verification required", probe.verification_state == "active_verification_required")
        probe.active = False
        check("inactive -> inactive", probe.verification_state == "inactive")

    # migration SQL on an old-schema database
    old = create_engine(f"sqlite:///{os.path.join(tmp, 'old.db')}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, _uid VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO users (_uid) VALUES ('a'), ('b')"))
        for stmt in MIGRATION_SQL:
            conn.execute(text(stmt))
    with old.connect() as conn:
        rows = conn.execute(text("SELECT active, last_verified FROM users")).fetchall()
    check("migration leaves every existing account active, verified 2026-01-30",
          len(rows) == 2 and all(r[0] == 1 and str(r[1]).startswith("2026-01-30") for r in rows), str(rows))

    shutil.rmtree(tmp, ignore_errors=True)
    if real_before:
        check("real dev DB untouched", sha256(REAL_DB) == real_before)
    print("\n" + ("ALL PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
