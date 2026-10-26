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

    # ---- a logged-in Teacher or Admin can create test accounts that are not real GitHub accounts ----
    from model import account_types as _types

    class OnlyRealGitHub:
        def get(self, uid):
            return ({}, 404) if uid.startswith("zz_fake") else ({}, 200)

    with app.app_context():
        db.session.add(User(name="zz_teacher_off", uid="zz_teacher_off", password=PASSWORD, role="Teacher"))  # never activated
        db.session.commit()

    with patch("api.user.GitHubUser", OnlyRealGitHub):
        with app.test_client() as c:
            r = signup(c, "zz_fake_anon", accountType="mentor")
            check("not logged in: a uid that is not a GitHub account is refused (404) and nothing is created",
                  r.status_code == 404 and "GitHub" in r.get_json()["message"] and get("zz_fake_anon") is None, r.status_code)
        with app.test_client() as c:
            r = signup(c, "zz_real_anon", accountType="mentor")
            check("not logged in: a real GitHub uid still signs up (200)", r.status_code == 200 and get("zz_real_anon"), r.status_code)
        with app.test_client() as c:
            login(c, "zz_student")
            r = signup(c, "zz_fake_student", accountType="mentor")
            check("logged in as a student: no bypass (404)", r.status_code == 404 and get("zz_fake_student") is None, r.status_code)
        with app.test_client() as c:
            logged_in = c.post("/api/authenticate", json={"uid": "zz_teacher_off", "password": PASSWORD})
            r = signup(c, "zz_fake_off", accountType="mentor")
            check("an inactive Teacher, who can log in but is not active, cannot bypass (404)",
                  logged_in.status_code == 200 and c.get_cookie(app.config["JWT_TOKEN_NAME"]) is not None
                  and r.status_code == 404 and get("zz_fake_off") is None, f"login {logged_in.status_code}, signup {r.status_code}")
        with app.test_client() as c:
            c.set_cookie(app.config["JWT_TOKEN_NAME"], "not-a-real-token")
            r = signup(c, "zz_fake_badjwt", accountType="mentor")
            check("a garbage login cookie does not bypass (404)", r.status_code == 404 and get("zz_fake_badjwt") is None, r.status_code)

        for staff in ("zz_teacher", "zz_admin"):
            with app.test_client() as c:
                login(c, staff)
                r = signup(c, f"zz_fake_by_{staff[3:]}", accountType="mentor")
                u = get(f"zz_fake_by_{staff[3:]}")
                check(f"logged in as {staff[3:].title()}: a uid that is not a GitHub account can be created (200, an inactive Mentor)",
                      r.status_code == 200 and u and u["role"] == "Mentor" and u["active"] is False, f"{r.status_code} {u}")
        with app.test_client() as c:
            login(c, "zz_teacher")
            r = signup(c, "zz_fake_student2", accountType="student")
            u = get("zz_fake_student2")
            check("a staff-made student test account is created too, inactive until staff activate it (no Google token)",
                  r.status_code == 200 and u and u["role"] == "User" and u["active"] is False, f"{r.status_code} {u}")
            with app.app_context():
                before = User.query.filter_by(_uid="zz_fake_student2").count()
            other_password = "Aa1!" + secrets.token_urlsafe(8)
            signup(c, "zz_fake_student2", accountType="mentor", password=other_password, name="Someone Else")
            with app.app_context():
                after = User.query.filter_by(_uid="zz_fake_student2").count()
                kept = User.query.filter_by(_uid="zz_fake_student2").first()
                untouched = kept.is_password(PASSWORD) and not kept.is_password(other_password) and kept.name == "Name zz_fake_student2"
            check("a repeated uid creates no second account and changes nothing in the first (role, password, name)",
                  before == 1 and after == 1 and get("zz_fake_student2")["role"] == "User" and untouched, f"{before} {after} {untouched}")
            r = signup(c, "z", accountType="student")
            check("the bypass does not skip the uid length check (400)", r.status_code == 400, r.status_code)
            r = signup(c, "zz_fake_notstaff", accountType="teacher")
            u = get("zz_fake_notstaff")
            check("staff still cannot make a Teacher or Admin this way: an unknown type is an ordinary student",
                  r.status_code == 200 and u and u["role"] == "User", f"{r.status_code} {u}")

        # the audit log: one line per account actually created by staff, and no forged lines
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
                login(c, "zz_teacher")
                signup(c, "zz_fake_logged", accountType="mentor")
                one = list(collector.lines)
                signup(c, "zz_fake_logged", accountType="mentor")     # a repeat: nothing new is created
                signup(c, "zz_fake_x", accountType="mentor", password="short")  # refused: too short
                after_failures = list(collector.lines)
                signup(c, "zz_fake_nl\nAUDIT signup uid=admin created by staff uid=other", accountType="mentor")
                forged = list(collector.lines)
            check("a staff-created account is logged once, with who made it",
                  len(one) == 1 and "zz_fake_logged" in one[0] and "zz_teacher" in one[0], str(one))
            check("a repeated uid and a refused signup are not logged as created", after_failures == one, str(after_failures))
            check("a uid with a newline cannot forge a second log line", all("\n" not in l for l in forged) and len(forged) <= len(one) + 1, str(forged))
        finally:
            app.logger.removeHandler(collector)
            app.logger.setLevel(old_level)

        changed = copy.deepcopy(_types._data)
        changed["staff_roles"] = ["Admin"]
        with patch.object(_types, "_data", changed):
            with app.test_client() as c:
                login(c, "zz_teacher")
                r = signup(c, "zz_fake_data", accountType="mentor")
                check("who may bypass comes from the data: with Teacher dropped from staff_roles a Teacher no longer can (404)",
                      r.status_code == 404 and get("zz_fake_data") is None, r.status_code)

    # ---- a guest: anyone can sign up, active at once, and can only view and comment, for a few hours ----
    from model import account_types as _gt

    class NeverAGitHubAccount:
        def get(self, uid):
            return ({}, 404)

    def set_verified_ago(uid, **delta):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            u.last_verified = datetime.utcnow() - timedelta(**delta)
            db.session.commit()

    def count_users(uid):
        with app.app_context():
            return User.query.filter_by(_uid=uid).count()

    with patch("api.user.GitHubUser", NeverAGitHubAccount), patch("model.google_token.requests.get", fake_tokeninfo):
        with app.test_client() as c:
            r = signup(c, "zz_guest1", accountType="guest", name="Guest One", email="guest1@example.com")
            u = get("zz_guest1")
            check("anyone can sign up as a guest with a uid that is not a GitHub account: created (200), role Guest, active at once",
                  r.status_code == 200 and u and u["role"] == "Guest" and u["active"] is True and "password" not in r.get_json(), f"{r.status_code} {u}")
            r = signup(c, "zz_guest2", accountType="guest", idToken="not-a-token")
            check("a guest needs no Google sign-in: a bad token is ignored", r.status_code == 200 and get("zz_guest2")["role"] == "Guest", r.status_code)
            r = signup(c, "zz_guest3", accountType="guest", role="Admin")
            check("a guest cannot ask for another role in the request", r.status_code == 200 and get("zz_guest3")["role"] == "Guest", f"{r.status_code} {get('zz_guest3')}")
            r = signup(c, "z", accountType="guest")
            check("the uid length check applies to a guest (400)", r.status_code == 400, r.status_code)
            r = signup(c, "zz_guest_weak", accountType="guest", password="weak")
            check("the password rule applies to a guest (400) and nothing is created", r.status_code == 400 and count_users("zz_guest_weak") == 0, r.status_code)
            r = signup(c, "zz_guest1", accountType="guest", password=PASSWORD, name="Someone Else")
            check("a repeated uid says it already exists (409), with no other account's details, and creates nothing",
                  r.status_code == 409 and "email" not in r.get_json() and count_users("zz_guest1") == 1, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            r = signup(c, "zz_fake_student_check", accountType="student")
            check("an ordinary student with a uid that is not a GitHub account is still refused (404)", r.status_code == 404 and count_users("zz_fake_student_check") == 0, r.status_code)

        # what a guest can and cannot do
        with app.test_client() as c:
            login(c, "zz_student")
            post = c.post("/api/microblog", json={"content": "a post to comment on"})
            post_id = (post.get_json() or {}).get("id")
        with app.test_client() as c:
            r = c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD})
            who = (r.get_json() or {}).get("user", {})
            check("a guest can log in, and the login says the account is limited", r.status_code == 200 and who.get("role") == "Guest" and who.get("limited") is True, f"{r.status_code} {who}")
            check("a student's login says it is not limited", login_user_body("zz_student").get("limited") is False, str(login_user_body("zz_student")))
            check("a guest can ask who they are (200)", c.get("/api/id").status_code == 200)
            r = c.get("/api/microblog")
            check("a guest can view the microblog (200)", r.status_code == 200 and "microblogs" in r.get_json(), r.status_code)
            r = c.post("/api/microblog/reply", json={"postId": post_id, "content": "a guest comment"})
            check("a guest can comment on a microblog post (200)", post_id is not None and r.status_code == 200 and "reply" in r.get_json(), f"{post_id} {r.status_code}")
            denied = [("POST", "/api/microblog", {"content": "a new post"}), ("POST", "/api/microblog/reaction", {"postId": post_id, "type": "like"}),
                      ("PUT", "/api/user", {"name": "Hacked"}), ("GET", "/api/user", None), ("GET", "/api/user/section", None),
                      ("POST", "/api/user/section", {"sections": []}), ("DELETE", "/api/user", {"uid": "zz_student"}),
                      ("GET", "/api/user/class", None), ("POST", "/api/user/zz_student/verify", {})]
            for method, path, body in denied:
                r = c.open(path, method=method, json=body)
                check(f"a guest cannot {method} {path} (403)", r.status_code == 403, r.status_code)
            with app.app_context():
                kept_name = User.query.filter_by(_uid="zz_guest1").first().name
            check("the guest's name was not changed by the refused update", kept_name == "Guest One", kept_name)
            r = c.delete("/api/authenticate")
            check("a guest can log out (200)", r.status_code == 200, r.status_code)
        with app.test_client() as c:
            login(c, "zz_student")
            check("the same requests still work for a student: update own profile (200) and post to the microblog (200)",
                  c.put("/api/user", json={"name": "Student Renamed"}).status_code == 200 and c.post("/api/microblog", json={"content": "x"}).status_code == 200)
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            r = c.put("/api/user", json={"name": "Via the form login"})
            check("the limit holds for the Flask form login (session), not only the token (403)", r.status_code == 403, r.status_code)

        # a guest lasts a few hours
        with app.test_client() as c:
            login(c, "zz_guest2")
            set_verified_ago("zz_guest2", hours=2, minutes=59)
            check("a guest is still usable just inside the lifetime (200)", c.get("/api/id").status_code == 200)
            set_verified_ago("zz_guest2", hours=3, minutes=1)
            r = c.get("/api/id")
            check("a guest is refused once the lifetime has passed (403)", r.status_code == 403 and "expired" in r.get_json()["message"].lower(), f"{r.status_code} {r.get_data(as_text=True)[:90]}")
            r2 = c.post("/api/authenticate", json={"uid": "zz_guest2", "password": PASSWORD})
            check("and cannot log in again (403)", r2.status_code == 403 and "expired" in r2.get_json()["message"].lower(), f"{r2.status_code} {r2.get_data(as_text=True)[:90]}")
        set_verified_ago("zz_student", days=400)
        with app.test_client() as c:
            login(c, "zz_student")
            check("the lifetime applies only to guests: a student with an old verification is not refused", c.get("/api/id").status_code == 200)
        with app.app_context():
            User.query.filter_by(_uid="zz_student").first().activate()
        with app.test_client() as c:
            login(c, "zz_admin")
            c.post("/api/user/zz_guest3/deactivate")
        with app.test_client() as c:
            r = c.post("/api/authenticate", json={"uid": "zz_guest3", "password": PASSWORD})
            check("an Admin can deactivate a guest, and it can no longer use the API", r.status_code == 200 and c.get("/api/id").status_code == 403, f"{r.status_code}")

        # Flask's own login form makes a session: the limits must hold there too
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            for path in ("/users/table2", "/sections/", "/persona/", "/congrats"):
                r = c.get(path)
                check(f"a guest logged in through the form cannot open {path} (403)", r.status_code == 403, r.status_code)
            check("the form login's landing page works for a guest, not a JSON error (200)", c.get("/").status_code == 200, c.get("/").status_code)
            check("and can still ask who they are (200) and log out", c.get("/api/id").status_code == 200 and c.get("/logout").status_code == 302)
            check("after logging out the session is gone (401)", c.get("/api/id").status_code == 401)
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            r = c.post("/api/authenticate", json={"uid": "zz_student", "password": PASSWORD})
            check("a guest's session can still log in as somebody else (200), so it is never stuck", r.status_code == 200, r.status_code)
        set_verified_ago("zz_guest1", minutes=1)
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            set_verified_ago("zz_guest1", hours=3, minutes=1)
            r = c.get("/users/table2")
            check("a form-login session stops working when the lifetime passes (not 200)", r.status_code in (302, 401, 403), r.status_code)
            check("and so does the API through that session (not 200)", c.get("/api/id").status_code in (401, 403))
        set_verified_ago("zz_guest1", minutes=1)
        with app.app_context():
            guest_row = User.query.filter_by(_uid="zz_guest1").first()
            session_id = f"{guest_row.id}:{guest_row.token_version}"
        with app.app_context():
            fresh_session = main.load_user(session_id)
        set_verified_ago("zz_guest1", hours=3, minutes=1)
        with app.app_context():
            expired_session = main.load_user(session_id)
        set_verified_ago("zz_guest1", minutes=1)
        check("a session for an account whose lifetime has passed is not a login at all (load_user)",
              fresh_session is not None and expired_session is None, f"fresh found: {fresh_session is not None}, expired found: {expired_session is not None}")

        set_verified_ago("zz_guest1", hours=3, minutes=1)
        with app.test_client() as c:
            page = c.post("/login", data={"username": "zz_guest1", "password": PASSWORD}).get_data(as_text=True)
            check("the Flask login form tells an expired guest the account has expired", "expired" in page.lower(), page[:100])
        set_verified_ago("zz_guest1", minutes=1)

        # the allowed requests are matched in one place
        check("a trailing slash does not change what is allowed, a different method does, and an unlimited role is always allowed",
              _gt.request_allowed("Guest", "GET", "/api/microblog/") and not _gt.request_allowed("Guest", "POST", "/api/microblog")
              and not _gt.request_allowed("Guest", "GET", "/api/user") and _gt.request_allowed("User", "DELETE", "/api/user"))

        # staff cannot verify a guest: that would restart or end its few hours
        with app.test_client() as c:
            login(c, "zz_teacher")
            before_verified = get("zz_guest1")["last_verified"]
            r = c.post("/api/user/zz_guest1/verify", json={"days": 1})
            check("a Teacher cannot verify a guest (400), and the guest's clock is untouched",
                  r.status_code == 400 and get("zz_guest1")["last_verified"] == before_verified, f"{r.status_code}")

        # an open sign-up needs a password, and an expired guest's name is free again
        with app.test_client() as c:
            body = dict(name="No Password", uid="zz_guest_nopw", accountType="guest", email="x@example.com")
            r = c.post("/api/user", json=body)
            check("a guest cannot be created without a password (400), so none gets the default one",
                  r.status_code == 400 and count_users("zz_guest_nopw") == 0, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            signup(c, "zz_guest_old", accountType="guest")
            r = signup(c, "zz_guest_old", accountType="guest")
            check("a guest uid that is still in use stays taken (409)", r.status_code == 409, r.status_code)
            set_verified_ago("zz_guest_old", hours=3, minutes=1)
            r = signup(c, "zz_guest_old", accountType="guest", password="weak", name="Rejected")
            with app.app_context():
                still_old = User.query.filter_by(_uid="zz_guest_old").first()
            check("a sign-up that is refused (a weak password, 400) does not remove the expired account it would have replaced",
                  r.status_code == 400 and still_old is not None and still_old.name != "Rejected", f"{r.status_code}")
            new_password = "Aa1!" + secrets.token_urlsafe(8)
            r = signup(c, "zz_guest_old", accountType="guest", password=new_password, name="Fresh Guest")
            with app.app_context():
                fresh = User.query.filter_by(_uid="zz_guest_old").first()
                reused = fresh is not None and fresh.name == "Fresh Guest" and fresh.is_password(new_password) and not fresh.expired()
            check("once a guest has expired its uid is free again: a new guest can take it (200) and the old account is gone",
                  r.status_code == 200 and count_users("zz_guest_old") == 1 and reused, f"{r.status_code} {count_users('zz_guest_old')} {reused}")

        import logging

        class CollectAudit(logging.Handler):
            def __init__(self):
                super().__init__(logging.INFO)
                self.lines = []
            def emit(self, record):
                if "AUDIT" in record.getMessage():
                    self.lines.append(record.getMessage())
        audit = CollectAudit()
        app.logger.addHandler(audit)
        old_level = app.logger.level
        app.logger.setLevel(logging.INFO)
        try:
            with app.test_client() as c:
                login(c, "zz_teacher")
                signup(c, "zz_guest_by_staff", accountType="guest")
                no_line_for_guest = [l for l in audit.lines if "zz_guest_by_staff" in l]
                signup(c, "zz_mentor_by_staff", accountType="mentor")
                line_for_mentor = [l for l in audit.lines if "zz_mentor_by_staff" in l]
            check("a staff member creating a guest is not logged as skipping a GitHub check, since none was needed", no_line_for_guest == [], str(no_line_for_guest))
            check("but staff creating an ordinary account that skips it still is", len(line_for_mentor) == 1, str(line_for_mentor))
        finally:
            app.logger.removeHandler(audit)
            app.logger.setLevel(old_level)

        # the rules come from the data
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["lifetime_hours"] = 1
        set_verified_ago("zz_guest1", hours=2)
        with patch.object(_gt, "_data", changed):
            with app.test_client() as c:
                r = c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD})
                check("the lifetime comes from the data: with 1 hour a guest verified 2 hours ago is refused (403)", r.status_code == 403, r.status_code)
        set_verified_ago("zz_guest1", minutes=1)
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["allowed_requests"] = ["GET /api/id", "GET /api/user"]
        with patch.object(_gt, "_data", changed):
            with app.test_client() as c:
                login(c, "zz_guest1")
                check("what a guest may do comes from the data: with the list changed, GET /api/user (200) is allowed and commenting (403) is not",
                      c.get("/api/user").status_code == 200 and c.post("/api/microblog/reply", json={"postId": post_id, "content": "x"}).status_code == 403)

        # the startup check refuses data that would make a public type unsafe
        def refused(mutate):
            bad = copy.deepcopy(_gt._data)
            mutate(bad)
            try:
                _gt._check(bad)
                return False
            except ValueError:
                return True
        check("startup refuses a type that skips the GitHub check but has no list of allowed requests",
              refused(lambda d: d["account_types"]["guest"].pop("allowed_requests")))
        check("startup refuses an empty list of allowed requests", refused(lambda d: d["account_types"]["guest"].update(allowed_requests=[])))
        check("startup refuses a public type whose role is an Admin or Teacher role",
              refused(lambda d: d["account_types"]["guest"].update(role="Teacher")) and refused(lambda d: d["account_types"]["guest"].update(role=d["admin_role"])))
        check("startup refuses an allowed request that is not written as METHOD /path",
              refused(lambda d: d["account_types"]["guest"].update(allowed_requests=["/api/id"])))
        check("startup refuses flags that are not true or false",
              refused(lambda d: d["account_types"]["guest"].update(github_check="false")) and refused(lambda d: d["account_types"]["guest"].update(active_on_signup="yes")))
        check("startup refuses two account types that share a role", refused(lambda d: d["account_types"]["guest"].update(role="User")))
        check("startup refuses a lifetime that is not a positive number of hours",
              refused(lambda d: d["account_types"]["guest"].update(lifetime_hours=0)) and refused(lambda d: d["account_types"]["guest"].update(lifetime_hours="3")))

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
