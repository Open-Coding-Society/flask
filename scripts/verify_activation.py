#!/usr/bin/env python3
"""Verify account activation end-to-end through the real request stack.

Runs against throwaway SQLite DBs with Google and GitHub faked, and checksums the
real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_activation.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import copy, hashlib, os, secrets, shutil, sys, tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-activation")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run, with one of each character class; the accounts live only in a throwaway database
CLIENT_ID, GOOD = "test-client-id", "good-token"
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
              "not-student": {**good, "email": "someone@gmail.com"},
              "other-domain": {**good, "email": "kid@school.example"}}.get((params or {}).get("id_token"))
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
                                               last_verified=u.last_verified, email=u.email,
                                               is_test=getattr(u, "is_test", None))

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
            r = c.get("/api/id")
            check("inactive account is refused on a protected endpoint",
                  r.status_code == 403 and "pending verification" in r.get_data(as_text=True))
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
            r = c.post("/api/user/admin-create", json=dict(name="Staff Made", uid="zz_made_mentor",
                                                           password=PASSWORD, accountType="mentor"))
            u = get("zz_made_mentor")
            check("staff-created mentor starts inactive", r.status_code == 200 and u["role"] == "Mentor"
                  and u["active"] is False, f"{r.status_code} {u}")
            r = c.post("/api/user/admin-create", json=dict(name="Staff Made", uid="zz_made_student",
                                                           password=PASSWORD, accountType="student"))
            check("admin-create does not create students", r.status_code == 400 and get("zz_made_student") is None)
            r = c.post("/api/user/zz_no_token/verify")
            check("a Teacher can verify and activate a student",
                  r.status_code == 200 and get("zz_no_token")["active"] is True, f"{r.status_code} {get('zz_no_token')}")
            for bad in ("teacher", "admin", ""):
                r = c.post("/api/user/admin-create", json=dict(name="Bad Role", uid="zz_bad_" + (bad or "x"),
                                                               password=PASSWORD, accountType=bad))
                check(f"admin-create refuses accountType '{bad}'", r.status_code == 400)
        with app.test_client() as c:
            login(c, "zz_admin")
            check("an admin can verify a mentor too", c.post("/api/user/zz_mentor_tok/verify").status_code == 200
                  and get("zz_mentor_tok")["active"])
        with app.test_client() as c:
            login(c, "zz_student")
            for path in ("/api/user/zz_mentor/verify", "/api/user/admin-create"):
                check(f"non-staff refused on {path}", c.post(path, json={}).status_code == 403)

        # the rules come from data (model/account_types.json), not code: change the data and
        # the behaviour changes with it
        from model import account_types
        data = copy.deepcopy(account_types._data)
        data["account_types"]["student"]["google_verified_domains"] = ["school.example"]
        data["account_types"]["observer"] = dict(role="Observer", google_verified_domains=[], staff_created=True)
        data["verification_interval_days"] = 30
        with patch("api.user.GitHubUser", FakeGitHub), patch("model.google_token.requests.get", fake_tokeninfo), \
                patch.object(account_types, "_data", data):
            with app.test_client() as c:
                signup(c, "zz_new_domain", accountType="student", idToken="other-domain")
                check("a student is activated by the domain listed in the data", get("zz_new_domain")["active"])
                signup(c, "zz_old_domain", accountType="student", idToken=GOOD)
                check("the old school domain no longer activates once the data changes",
                      get("zz_old_domain")["active"] is False)
            with app.test_client() as c:
                login(c, "zz_teacher")
                r = c.post("/api/user/admin-create", json=dict(name="Obs", uid="zz_observer", password=PASSWORD,
                                                               accountType="observer"))
                u = get("zz_observer")
                check("an account type added to the data can be created by staff",
                      r.status_code == 200 and u and u["role"] == "Observer" and u["active"] is False, f"{r.status_code} {u}")
                check("and is verified by staff, not by Google",
                      c.post("/api/user/zz_observer/verify").status_code == 200 and get("zz_observer")["active"])
            with app.app_context():
                probe = User(name="Probe", uid="zz_probe30", password=PASSWORD)
                probe.active = True
                probe.last_verified = datetime.utcnow() - timedelta(days=31)
                check("the re-verification interval comes from the data",
                      probe.verification_state == "active_verification_required")

    # ---- a student whose verification has lapsed verifies again with school Google sign-in -----
    with app.app_context():
        for uid, role, days in (("zz_due", "User", 400), ("zz_due2", "User", 400), ("zz_due3", "User", 400),
                                ("zz_due4", "User", 400), ("zz_fresh", "User", 10), ("zz_mentor_due", "Mentor", 400)):
            u = User(name=uid, uid=uid, password=PASSWORD, role=role)
            db.session.add(u)
            db.session.commit()
            u.activate()
            u.last_verified = datetime.utcnow() - timedelta(days=days)
            db.session.commit()
        db.session.add(User(name="zz_off", uid="zz_off", password=PASSWORD))  # a student never activated
        db.session.commit()

    def login_user_body(uid):
        with app.test_client() as c:
            return c.post("/api/authenticate", json={"uid": uid, "password": PASSWORD}).get_json()["user"]

    def reverify(uid, token="good-token", **extra):
        with app.test_client() as c:
            login(c, uid)
            return c.post("/api/user/reverify", json={"idToken": token, **extra})

    with patch("model.google_token.requests.get", fake_tokeninfo):
        due, fresh, mentor, off = (login_user_body(u) for u in ("zz_due", "zz_fresh", "zz_mentor_due", "zz_off"))
        check("login tells a lapsed student that verification is required, and that Google does it",
              due["active"] is True and due["verification_state"] == "active_verification_required"
              and due["verification_required"] is True and due["verified_by_google"] is True, str(due))
        check("login tells a verified student that nothing is required",
              fresh["verification_state"] == "active_verified" and fresh["verification_required"] is False, str(fresh))
        check("login tells a lapsed Mentor that verification is required but staff do it, not Google",
              mentor["verification_required"] is True and mentor["verified_by_google"] is False, str(mentor))
        check("login shows an inactive account as inactive, not as verification required",
              off["active"] is False and off["verification_state"] == "inactive" and off["verification_required"] is False, str(off))

        r = reverify("zz_due")
        u = get("zz_due")
        check("a lapsed student with a valid school token is verified again (200, active_verified, stamped now)",
              r.status_code == 200 and u["state"] == "active_verified"
              and (datetime.utcnow() - u["last_verified"]).total_seconds() < 60, f"{r.status_code} {u}")
        for bad in ("wrong-aud", "wrong-iss", "unverified", "not-student", "other-domain", "no-such-token", ""):
            r = reverify("zz_due2", bad)
            check(f"token {bad!r} does not verify (403) and the student stays lapsed",
                  r.status_code == 403 and get("zz_due2")["state"] == "active_verification_required", f"{r.status_code}")
        with app.test_client() as c:
            login(c, "zz_due2")
            r = c.post("/api/user/reverify", json={})
            check("no token at all is refused (403)", r.status_code == 403 and get("zz_due2")["state"] == "active_verification_required")
            for odd in ([], "x", 5):
                r = c.post("/api/user/reverify", json=odd)
                check(f"a body that is not a JSON object ({odd!r}) is a 400", r.status_code == 400, r.status_code)
        before = get("zz_fresh")["last_verified"]
        r = reverify("zz_fresh")
        check("a student whose verification is current is told none is due (409) and nothing changes",
              r.status_code == 409 and get("zz_fresh")["last_verified"] == before, r.status_code)
        r = reverify("zz_mentor_due")
        check("a lapsed Mentor cannot verify with Google (403): staff verify Mentors",
              r.status_code == 403 and get("zz_mentor_due")["state"] == "active_verification_required", r.status_code)
        r = reverify("zz_off")
        check("an inactive student cannot activate themselves with a valid token (403)",
              r.status_code == 403 and get("zz_off")["active"] is False, r.status_code)
        with app.test_client() as c:
            r = c.post("/api/user/reverify", json={"idToken": GOOD})
            check("without logging in it is refused (401)", r.status_code == 401, r.status_code)

        changed = copy.deepcopy(account_types._data)
        changed["account_types"]["student"]["google_verified_domains"] = ["school.example"]
        with patch.object(account_types, "_data", changed):
            r_old = reverify("zz_due3", GOOD)
            r_new = reverify("zz_due4", "other-domain")
        check("the school domain comes from the data: the old domain is refused and the new one accepted",
              r_old.status_code == 403 and r_new.status_code == 200
              and get("zz_due3")["state"] == "active_verification_required" and get("zz_due4")["state"] == "active_verified",
              f"{r_old.status_code} {r_new.status_code}")

    # ---- a test account: a student an Admin tags as a test, with no GitHub or Google check ------
    from model import account_types as _types

    class OnlyRealGitHub:
        def get(self, uid):
            return ({}, 404) if uid.startswith("zz_fake") else ({}, 200)

    with app.app_context():
        db.session.add(User(name="zz_admin_off", uid="zz_admin_off", password=PASSWORD, role="Admin"))  # never activated
        db.session.commit()

    def count(uid):
        with app.app_context():
            return User.query.filter_by(_uid=uid).count()

    with patch("api.user.GitHubUser", OnlyRealGitHub), patch("model.google_token.requests.get", fake_tokeninfo):
        with app.test_client() as c:
            r = signup(c, "zz_fake_anon", accountType="mentor")
            check("not logged in: a uid that is not a GitHub account is refused (404) and nothing is created",
                  r.status_code == 404 and "GitHub" in r.get_json()["message"] and count("zz_fake_anon") == 0, r.status_code)
            r = signup(c, "zz_real_anon", accountType="mentor")
            check("not logged in: a real GitHub uid still signs up (200), and is not a test account",
                  r.status_code == 200 and get("zz_real_anon") and get("zz_real_anon")["is_test"] is False, r.status_code)
            r = signup(c, "zz_real_anon_test", accountType="student", isTest=True)
            check("not logged in: asking for a test account is refused (403), even for a real GitHub uid, and nothing is created",
                  r.status_code == 403 and count("zz_real_anon_test") == 0, r.status_code)
            r = signup(c, "zz_fake_anon2", accountType="student", isTest="true")
            check("isTest must be the JSON value true: the string 'true' is an ordinary signup, so the GitHub check applies (404)",
                  r.status_code == 404 and count("zz_fake_anon2") == 0, r.status_code)
        with app.test_client() as c:
            login(c, "zz_student")
            r = signup(c, "zz_fake_student", accountType="student", isTest=True)
            check("logged in as a student: refused (403)", r.status_code == 403 and count("zz_fake_student") == 0, r.status_code)
        with app.test_client() as c:
            login(c, "zz_teacher")
            r = signup(c, "zz_fake_teacher", accountType="student", isTest=True)
            check("logged in as a Teacher: refused (403), a test account is Admin only",
                  r.status_code == 403 and count("zz_fake_teacher") == 0, r.status_code)
            r = signup(c, "zz_fake_student_by_teacher", accountType="student")
            check("a Teacher signing up an ordinary student with a uid that is not a GitHub account is refused (404)",
                  r.status_code == 404 and count("zz_fake_student_by_teacher") == 0, r.status_code)
        with app.test_client() as c:
            logged_in = c.post("/api/authenticate", json={"uid": "zz_admin_off", "password": PASSWORD})
            r = signup(c, "zz_fake_adminoff", accountType="student", isTest=True)
            check("an inactive Admin, who can log in but is not active, is refused (403)",
                  logged_in.status_code == 200 and c.get_cookie(app.config["JWT_TOKEN_NAME"]) is not None
                  and r.status_code == 403 and count("zz_fake_adminoff") == 0, f"login {logged_in.status_code}, signup {r.status_code}")
        with app.test_client() as c:
            c.set_cookie(app.config["JWT_TOKEN_NAME"], "not-a-real-token")
            r = signup(c, "zz_fake_badjwt", accountType="student", isTest=True)
            check("a garbage login cookie is refused (403)", r.status_code == 403 and count("zz_fake_badjwt") == 0, r.status_code)

        with app.test_client() as c:
            login(c, "zz_admin")
            r = signup(c, "zz_fake_test1", accountType="student", isTest=True)
            u = get("zz_fake_test1")
            check("logged in as an Admin: a test account with a uid that is not a GitHub account is created (200), a tagged, inactive student",
                  r.status_code == 200 and u and u["role"] == "User" and u["is_test"] is True and u["active"] is False, f"{r.status_code} {u}")
            r = signup(c, "zz_real_test2", accountType="student", isTest=True)
            check("an Admin can make a test account with a real GitHub uid too (200)",
                  r.status_code == 200 and get("zz_real_test2") and get("zz_real_test2")["is_test"] is True, r.status_code)
            r = signup(c, "zz_fake_test3", accountType="student", isTest=True, idToken=GOOD)
            u = get("zz_fake_test3")
            check("a test account is never activated by a Google token: it starts inactive even with a valid one",
                  r.status_code == 200 and u and u["active"] is False and u["is_test"] is True, f"{r.status_code} {u}")
            r = signup(c, "zz_fake_test4", accountType="mentor", isTest=True)
            u = get("zz_fake_test4")
            check("a test account is always a student, whatever type was asked for",
                  r.status_code == 200 and u and u["role"] == "User" and u["is_test"] is True, f"{r.status_code} {u}")
            for kind in ("student", "mentor"):
                r = signup(c, f"zz_fake_{kind}_by_admin", accountType=kind)
                check(f"an Admin signing up an ordinary {kind} with a uid that is not a GitHub account is refused (404): no hidden bypass",
                      r.status_code == 404 and count(f"zz_fake_{kind}_by_admin") == 0, r.status_code)
            other_password = "Aa1!" + secrets.token_urlsafe(8)
            r_dup = signup(c, "zz_fake_test1", accountType="student", isTest=True, password=other_password, name="Someone Else")
            check("a repeated uid for a test account says it already exists (409), with no other account's details",
                  r_dup.status_code == 409 and "already exists" in r_dup.get_json()["message"] and "email" not in r_dup.get_json(), f"{r_dup.status_code} {r_dup.get_data(as_text=True)[:80]}")
            with app.app_context():
                kept = User.query.filter_by(_uid="zz_fake_test1").first()
                untouched = kept.is_password(PASSWORD) and not kept.is_password(other_password) and kept.name == "Name zz_fake_test1"
            check("a repeated uid creates no second account and changes nothing in the first (password, name)",
                  count("zz_fake_test1") == 1 and untouched, f"{count('zz_fake_test1')} {untouched}")
            r_real = signup(c, "zz_real_anon", accountType="student", isTest=True)
            check("asking for a test account with the uid of an existing real account is refused (409) and does not tag it",
                  r_real.status_code == 409 and get("zz_real_anon")["is_test"] is False, f"{r_real.status_code} {get('zz_real_anon')}")
            r = signup(c, "z", accountType="student", isTest=True)
            check("the uid length check still applies to a test account (400)", r.status_code == 400, r.status_code)
            r = signup(c, "zz_fake_test_weak", accountType="student", isTest=True, password="weak")
            check("the password rule still applies to a test account (400)", r.status_code == 400 and count("zz_fake_test_weak") == 0, r.status_code)
            listed = {x["uid"]: x for x in c.get("/api/user").get_json()}
            check("the user list shows the tag", listed.get("zz_fake_test1", {}).get("is_test") is True
                  and listed.get("zz_student", {}).get("is_test") is False, str({k: v.get("is_test") for k, v in listed.items() if k in ("zz_fake_test1", "zz_student")}))
        with app.test_client() as c:
            login(c, "zz_student")
            c.put("/api/user", json={"is_test": True})
            check("a user cannot tag themselves by updating their profile", get("zz_student")["is_test"] is False)
        with app.test_client() as c:
            login(c, "zz_admin")
            c.put("/api/user", json={"uid": "zz_fake_test1", "is_test": False})
            check("and the tag cannot be removed through an update either", get("zz_fake_test1")["is_test"] is True)

        changed = copy.deepcopy(_types._data)
        changed["admin_role"] = "Teacher"
        with patch.object(_types, "_data", changed):
            with app.test_client() as c:
                login(c, "zz_teacher")
                r = signup(c, "zz_fake_data1", accountType="student", isTest=True)
                check("who may make a test account comes from the data: with the Admin role renamed to Teacher, a Teacher can (200) ",
                      r.status_code == 200 and get("zz_fake_data1") and get("zz_fake_data1")["is_test"] is True, r.status_code)

        import logging

        class Collect(logging.Handler):
            def __init__(self):
                super().__init__(logging.INFO)
                self.lines = []
            def emit(self, record):
                if "AUDIT" in record.getMessage():
                    self.lines.append(record.getMessage())
        collector = Collect()
        app.logger.addHandler(collector)
        old_level = app.logger.level
        app.logger.setLevel(logging.INFO)
        try:
            with app.test_client() as c:
                login(c, "zz_admin")
                signup(c, "zz_fake_logged", accountType="student", isTest=True)
                one = list(collector.lines)
                signup(c, "zz_fake_logged", accountType="student", isTest=True)
                signup(c, "zz_fake_x", accountType="student", isTest=True, password="short")
                after_failures = list(collector.lines)
                signup(c, "zz_fake_nl\nAUDIT signup uid=admin created by admin uid=other", accountType="student", isTest=True)
                forged = list(collector.lines)
            check("a test account is logged once, with who made it",
                  len(one) == 1 and "zz_fake_logged" in one[0] and "zz_admin" in one[0], str(one))
            check("a repeated uid and a refused signup are not logged as created", after_failures == one, str(after_failures))
            check("a uid with a newline cannot forge a second log line", all("\n" not in l for l in forged) and len(forged) <= len(one) + 1, str(forged))
        finally:
            app.logger.removeHandler(collector)
            app.logger.setLevel(old_level)

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
