#!/usr/bin/env python3
"""Verify account activation end-to-end through the real request stack.

Runs against throwaway SQLite DBs with Google and GitHub faked, and checksums the
real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_activation.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import copy, hashlib, json, os, re, secrets, shutil, sys, tempfile
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

    # ---- the Guest Window and the guest account --------------------------------------------------
    import logging
    from model import account_types as _gt
    from model import guest_window as _gw
    import api.authorize as _authz

    class NeverAGitHubAccount:
        def get(self, uid):
            return ({}, 404)

    app.config["GUEST_WINDOW_FILE"] = os.path.join(tmp, "guest_window.json")

    def set_verified_ago(uid, **delta):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            u.last_verified = datetime.utcnow() - timedelta(**delta)
            db.session.commit()

    def row(uid):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            return None if u is None else dict(id=u.id, active=u.active, role=u.role, name=u.name, last_verified=u.last_verified)

    def count_users(uid):
        with app.app_context():
            return User.query.filter_by(_uid=uid).count()

    def staff(uid, method, path, **kw):
        with app.test_client() as sc:
            login(sc, uid)
            return sc.open(path, method=method, **kw)

    def window():
        with app.test_client() as wc:
            return wc.get("/api/guest/window").get_json()

    class CollectAudit(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.lines = []
        def emit(self, record):
            if "AUDIT" in record.getMessage():
                self.lines.append(record.getMessage())
    audit = CollectAudit()
    app.logger.addHandler(audit)
    app.logger.setLevel(logging.INFO)

    with patch("api.user.GitHubUser", NeverAGitHubAccount), patch("model.google_token.requests.get", fake_tokeninfo):
        # the staff rule from before is retired: staff must have a real GitHub uid like everyone
        for who in ("zz_teacher", "zz_admin"):
            for kind in ("student", "mentor"):
                with app.test_client() as c:
                    login(c, who)
                    r = signup(c, f"zz_fake_{kind}_{who[3:]}", accountType=kind)
                    check(f"{who[3:].title()} signing up an ordinary {kind} with a uid that is not a GitHub account is refused (404): no staff bypass",
                          r.status_code == 404 and count_users(f"zz_fake_{kind}_{who[3:]}") == 0, r.status_code)

        # ---- the window is closed to start with
        check("the Guest Window starts closed, and anyone can ask", window() == {"open": False, "closes_at": None}, str(window()))
        with app.test_client() as c:
            r = signup(c, "zz_guest_early", accountType="guest")
            check("while the window is closed nobody can sign up as a guest (403), and nothing is created",
                  r.status_code == 403 and "closed" in r.get_json()["message"].lower() and count_users("zz_guest_early") == 0, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
        with app.test_client() as c:
            check("only staff can open it: not logged in (401)", c.post("/api/guest/window", json={"minutes": 60}).status_code == 401)
        with app.test_client() as c:
            login(c, "zz_student")
            check("a student cannot open it (403) or close it or disable guests",
                  c.post("/api/guest/window", json={"minutes": 60}).status_code == 403 and c.delete("/api/guest/window").status_code == 403
                  and c.post("/api/guest/disable").status_code == 403)
        check("and the window is still closed", window()["open"] is False)

        # ---- a Teacher opens it, for a limited time
        r = staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 60})
        body = r.get_json() or {}
        closes = datetime.fromisoformat(body["closes_at"]) if body.get("closes_at") else None
        minutes_left = (closes - datetime.utcnow()).total_seconds() / 60 if closes else None
        check("a Teacher can open the window for 60 minutes (200)", r.status_code == 200 and body.get("open") is True and minutes_left and 59 < minutes_left <= 60, f"{r.status_code} {body}")
        check("anyone can see that it is open and when it closes, and nothing else (not who opened it)",
              window()["open"] is True and set(window()) == {"open", "closes_at"}, str(window()))
        first_close = window()["closes_at"]
        for bad in (0, 181, -5, "60", 1.5, True, None):
            r = staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": bad})
            check(f"minutes={bad!r} is refused (400) and the window is unchanged", r.status_code == 400 and window()["closes_at"] == first_close, r.status_code)
        r = staff("zz_teacher", "POST", "/api/guest/window", json={})
        check("a request with no minutes is refused (400)", r.status_code == 400, r.status_code)
        check("it is kept in a file, so it survives a restart and is shared by every worker (no database table)",
              os.path.exists(app.config["GUEST_WINDOW_FILE"]) and "closes_at" in open(app.config["GUEST_WINDOW_FILE"]).read())
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["window_max_minutes"] = 30
        with patch.object(_gt, "_data", changed):
            over = staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 31})
            at_max = staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 30})
        check("the longest a window can be comes from the data: with a 30 minute maximum 31 is refused (400) and 30 is allowed",
              over.status_code == 400 and at_max.status_code == 200, f"{over.status_code} {at_max.status_code}")
        r = staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 180})
        check("and the default maximum is 3 hours (180 minutes)", r.status_code == 200, r.status_code)

        # ---- it closes by itself
        with open(app.config["GUEST_WINDOW_FILE"], "w") as fh:
            json.dump({"closes_at": (datetime.utcnow() - timedelta(minutes=1)).isoformat()}, fh)
        with app.test_client() as c:
            check("a window whose time has passed is closed again", window()["open"] is False and signup(c, "zz_guest_late", accountType="guest").status_code == 403)
        staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 120})
        r = staff("zz_admin", "DELETE", "/api/guest/window")
        check("staff can close the window at any time (200)", r.status_code == 200 and window()["open"] is False, r.status_code)
        staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 120})
        check("the window was opened, and closed, with a note of who did it in the log",
              any("guest window opened" in l and "zz_teacher" in l for l in audit.lines) and any("guest window closed" in l and "zz_admin" in l for l in audit.lines), str(audit.lines[-3:]))

        # ---- while it is open, anyone can sign up
        with app.test_client() as c:
            r = signup(c, "zz_guest1", accountType="guest", name="Guest One", email="guest1@example.com")
            u = row("zz_guest1")
            check("while the window is open anyone can sign up as a guest with a uid that is not a GitHub account: created (200), role Guest, active at once",
                  r.status_code == 200 and u and u["role"] == "Guest" and u["active"] is True and "password" not in r.get_json(), f"{r.status_code} {u}")
            r = signup(c, "zz_guest2", accountType="guest", idToken="not-a-token")
            check("a guest needs no Google sign-in: a bad token is ignored", r.status_code == 200 and row("zz_guest2")["role"] == "Guest", r.status_code)
            r = signup(c, "zz_guest3", accountType="guest", role="Admin")
            check("a guest cannot ask for another role in the request", r.status_code == 200 and row("zz_guest3")["role"] == "Guest", f"{r.status_code} {row('zz_guest3')}")
            check("the uid length check applies to a guest (400)", signup(c, "z", accountType="guest").status_code == 400)
            r = signup(c, "zz_guest_weak", accountType="guest", password="weak")
            check("the password rule applies to a guest (400) and nothing is created", r.status_code == 400 and count_users("zz_guest_weak") == 0, r.status_code)
            r = c.post("/api/user", json=dict(name="No Password", uid="zz_guest_nopw", accountType="guest", email="x@example.com"))
            check("a guest cannot be created without a password (400), so none gets the default one", r.status_code == 400 and count_users("zz_guest_nopw") == 0, r.status_code)
            r = signup(c, "zz_guest1", accountType="guest", password=PASSWORD, name="Someone Else")
            check("a repeated uid says it already exists (409), tells them to log in, shows nothing about the account, and creates nothing",
                  r.status_code == 409 and "email" not in r.get_json() and "log in" in r.get_json()["message"].lower() and count_users("zz_guest1") == 1,
                  f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            r = signup(c, "zz_fake_student_check", accountType="student")
            check("an ordinary student with a uid that is not a GitHub account is still refused (404)", r.status_code == 404 and count_users("zz_fake_student_check") == 0, r.status_code)

        # ---- what a guest can and cannot do
        with app.test_client() as c:
            login(c, "zz_student")
            post_id = (c.post("/api/microblog", json={"content": "a post to comment on"}).get_json() or {}).get("id")
        with app.test_client() as c:
            r = c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD})
            who = (r.get_json() or {}).get("user", {})
            check("a guest can log in, and the login says the account is limited", r.status_code == 200 and who.get("role") == "Guest" and who.get("limited") is True, f"{r.status_code} {who}")
            check("a student's login says it is not limited", login_user_body("zz_student").get("limited") is False)
            check("a guest can ask who they are (200)", c.get("/api/id").status_code == 200)
            r = c.get("/api/microblog")
            check("a guest can view the microblog (200)", r.status_code == 200 and "microblogs" in r.get_json(), r.status_code)
            r = c.post("/api/microblog", json={"content": "a guest post"})
            own_id = (r.get_json() or {}).get("id")
            check("a guest can write a post (200), and it is theirs", r.status_code == 200 and own_id is not None and (r.get_json() or {}).get("userId", (r.get_json() or {}).get("user_id", row("zz_guest1")["id"])) == row("zz_guest1")["id"], f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            check("and it shows in the microblog", own_id in [m.get("id") for m in c.get("/api/microblog").get_json().get("microblogs", [])])
            r = c.post("/api/microblog/reaction", json={"postId": own_id, "reactionType": "like"})
            check("a guest can react to their own post (200)", own_id is not None and r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            r = c.post("/api/microblog", json={"content": "x" * 281})
            check("the post rules still apply to a guest: more than 280 characters is refused (400)", r.status_code == 400, r.status_code)
            r = c.put("/api/microblog", json={"id": own_id, "content": "a guest post, edited"})
            check("a guest can edit their own post (200)", r.status_code == 200 and "edited" in r.get_data(as_text=True), f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            r = c.put("/api/microblog", json={"id": post_id, "content": "a guest editing a student's post"})
            check("but not somebody else's: the post's own owner rule answers, not the guest limit (403, Permission denied)", r.status_code == 403 and "Permission denied" in r.get_data(as_text=True), f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            r = c.delete("/api/microblog", json={"id": post_id})
            check("a guest cannot delete somebody else's post either (403, Permission denied)", r.status_code == 403 and "Permission denied" in r.get_data(as_text=True), f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            check("and that post is still there", post_id in [m.get("id") for m in c.get("/api/microblog").get_json().get("microblogs", [])])
            r = c.delete("/api/microblog", json={"id": own_id})
            check("a guest can delete their own post (200), and it is gone", r.status_code == 200 and own_id not in [m.get("id") for m in c.get("/api/microblog").get_json().get("microblogs", [])], f"{r.status_code} {r.get_data(as_text=True)[:100]}")
            r = c.post("/api/microblog/reply", json={"postId": post_id, "content": "a guest comment"})
            check("a guest can comment on a post (200)", post_id is not None and r.status_code == 200 and "reply" in r.get_json(), f"{post_id} {r.status_code}")
            r = c.post("/api/microblog/reaction", json={"postId": post_id, "reactionType": "like"})
            check("a guest can react to a post (200)", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            r = c.delete("/api/microblog/reaction", json={"postId": post_id, "reactionType": "like"})
            check("and take the reaction back (200)", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
            denied = [("PUT", "/api/user", {"name": "Hacked"}), ("GET", "/api/user", None),
                      ("GET", "/api/user/section", None), ("POST", "/api/user/section", {"sections": []}), ("DELETE", "/api/user", {"uid": "zz_student"}),
                      ("GET", "/api/user/class", None), ("POST", "/api/user/zz_student/verify", {}), ("POST", "/api/guest/window", {"minutes": 60}),
                      ("DELETE", "/api/guest/window", None), ("POST", "/api/guest/disable", None)]
            for method, path, body in denied:
                r = c.open(path, method=method, json=body)
                check(f"a guest cannot {method} {path} (403)", r.status_code == 403, r.status_code)
            check("the guest's name was not changed by the refused update", row("zz_guest1")["name"] == "Guest One", row("zz_guest1")["name"])
            check("a guest can log out (200)", c.delete("/api/authenticate").status_code == 200)
        with app.test_client() as c:
            login(c, "zz_student")
            check("the same requests still work for a student: update own profile (200) and post to the microblog (200)",
                  c.put("/api/user", json={"name": "Student Renamed"}).status_code == 200 and c.post("/api/microblog", json={"content": "x"}).status_code == 200)

        # Flask's own login form makes a session: the limits must hold there too
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            for path in ("/users/table2", "/sections/", "/persona/", "/congrats"):
                check(f"a guest logged in through the form cannot open {path} (403)", c.get(path).status_code == 403, c.get(path).status_code)
            check("the form login's landing page works for a guest (200), and so does asking who they are", c.get("/").status_code == 200 and c.get("/api/id").status_code == 200)
            check("it can log out, and then the session is gone (401)", c.get("/logout").status_code == 302 and c.get("/api/id").status_code == 401)
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            r = c.post("/api/authenticate", json={"uid": "zz_student", "password": PASSWORD})
            check("a guest's session can still log in as somebody else (200), so it is never stuck", r.status_code == 200, r.status_code)
        with app.test_client() as c:
            c.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
            r = c.put("/api/user", json={"name": "Via the form login"})
            check("the limit holds for the form login session on the API too (403)", r.status_code == 403, r.status_code)
        check("a trailing slash does not change what is allowed, a different method does, and an unlimited role is always allowed",
              _gt.request_allowed("Guest", "GET", "/api/microblog/") and _gt.request_allowed("Guest", "POST", "/api/microblog") and not _gt.request_allowed("Guest", "DELETE", "/api/microblog/reply")
              and not _gt.request_allowed("Guest", "GET", "/api/user") and _gt.request_allowed("User", "DELETE", "/api/user"))

        # ---- three hours, then inactive, and the record stays
        with app.test_client() as c:
            login(c, "zz_guest2")
            set_verified_ago("zz_guest2", hours=2, minutes=59)
            check("a guest is still usable just inside the three hours (200)", c.get("/api/id").status_code == 200)
            set_verified_ago("zz_guest2", hours=3, minutes=1)
            r = c.get("/api/id")
            check("a guest is refused once the three hours have passed (403)", r.status_code == 403 and "expired" in r.get_json()["message"].lower(), f"{r.status_code} {r.get_data(as_text=True)[:90]}")
        check("and the account is now inactive, but the record is kept", row("zz_guest2")["active"] is False and count_users("zz_guest2") == 1, str(row("zz_guest2")))
        set_verified_ago("zz_student", days=400)
        with app.test_client() as c:
            login(c, "zz_student")
            check("the three hours apply only to guests: a student with an old verification is not refused", c.get("/api/id").status_code == 200)
        with app.app_context():
            User.query.filter_by(_uid="zz_student").first().activate()

        # ---- coming back needs an open window
        staff("zz_teacher", "DELETE", "/api/guest/window")
        with app.test_client() as c:
            r = c.post("/api/authenticate", json={"uid": "zz_guest2", "password": PASSWORD})
            check("with the window closed a returning guest cannot log in (403) and stays inactive",
                  r.status_code == 403 and "closed" in r.get_json()["message"].lower() and row("zz_guest2")["active"] is False, f"{r.status_code} {r.get_data(as_text=True)[:90]}")
            r = signup(c, "zz_guest2", accountType="guest")
            check("and cannot sign up again either (403)", r.status_code == 403, r.status_code)
        staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 90})
        guest_id = row("zz_guest2")["id"]
        with app.test_client() as c:
            r = c.post("/api/authenticate", json={"uid": "zz_guest2", "password": PASSWORD})
            after = row("zz_guest2")
            check("with the window open the same guest logs in (200) and is active again, for a new three hours, as the same account",
                  r.status_code == 200 and after["active"] is True and after["id"] == guest_id
                  and (datetime.utcnow() - after["last_verified"]).total_seconds() < 120 and c.get("/api/id").status_code == 200, f"{r.status_code} {after}")
            r = c.post("/api/authenticate", json={"uid": "zz_guest2", "password": "wrong-password"})
            check("a wrong password does not reactivate anything (401)", r.status_code == 401)
        with app.test_client() as c:
            page = None
            set_verified_ago("zz_guest3", hours=3, minutes=1)
            page = c.post("/login", data={"username": "zz_guest3", "password": PASSWORD}).get_data(as_text=True)
            check("the Flask login form tells an expired guest the account has expired", "expired" in page.lower(), page[:100])
        with app.test_client() as c:
            with app.app_context():
                gr = User.query.filter_by(_uid="zz_guest1").first()
                sid = f"{gr.id}:{gr.token_version}"
            with app.app_context():
                fresh_session = main.load_user(sid)
            set_verified_ago("zz_guest1", hours=3, minutes=1)
            with app.app_context():
                expired_session = main.load_user(sid)
            check("a session for an account whose three hours have passed is not a login at all (load_user)",
                  fresh_session is not None and expired_session is None, f"fresh found: {fresh_session is not None}, expired found: {expired_session is not None}")
            set_verified_ago("zz_guest1", minutes=1)
        with app.test_client() as c:
            login(c, "zz_teacher")
            before_verified = row("zz_guest1")["last_verified"]
            r = c.post("/api/user/zz_guest1/verify", json={"days": 1})
            check("a Teacher cannot verify a guest (400), and the guest's clock is untouched", r.status_code == 400 and row("zz_guest1")["last_verified"] == before_verified, r.status_code)

        # ---- the switch that disables guests at once
        with app.test_client() as c:
            login(c, "zz_guest1")
            check("(a guest is logged in and working)", c.get("/api/id").status_code == 200)
            r = staff("zz_admin", "POST", "/api/guest/disable")
            body = r.get_json() or {}
            check("an Admin can disable guest access at once (200): the window closes and every active guest is made inactive",
                  r.status_code == 200 and body.get("disabled", 0) >= 2 and window()["open"] is False and row("zz_guest1")["active"] is False and row("zz_guest2")["active"] is False, f"{r.status_code} {body}")
            check("a guest that was logged in is refused straight away (403)", c.get("/api/id").status_code == 403)
        check("it touches nothing but guests: the student, mentor and staff accounts are still active",
              all(row(u)["active"] for u in ("zz_student", "zz_teacher", "zz_admin")), str({u: row(u)["active"] for u in ("zz_student", "zz_teacher", "zz_admin")}))
        check("the switch is logged with who used it and how many guests it affected", any("guest access disabled" in l and "zz_admin" in l for l in audit.lines), str(audit.lines[-2:]))
        with app.test_client() as c:
            check("while closed, nobody can come back (403)", c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD}).status_code == 403)
        staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 60})
        with app.test_client() as c:
            check("in a later window a disabled guest can come back (200), the record having been kept",
                  c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD}).status_code == 200 and row("zz_guest1")["active"] is True)

        # ---- one account cannot flood the server
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["requests_per_minute"] = 3
        clock = [1000.0]
        _authz._recent.clear()
        with patch.object(_gt, "_data", changed), patch.object(_authz.time, "monotonic", lambda: clock[0]):
            c1, c2, cs = app.test_client(), app.test_client(), app.test_client()
            if True:
                login(c1, "zz_guest1")
                login(c2, "zz_guest2")  # a guest disabled earlier comes back, the window being open
                login(cs, "zz_student")
                codes = [c1.get("/api/id").status_code for _ in range(5)]
                check("one guest is limited to the number of requests a minute set in the data: 3 allowed, then 429", codes == [200, 200, 200, 429, 429], str(codes))
                check("another guest is not affected (200)", c2.get("/api/id").status_code == 200)
                check("a student is never throttled", all(cs.get("/api/id").status_code == 200 for _ in range(10)))
                clock[0] += 61
                check("and after a minute the guest can go on (200)", c1.get("/api/id").status_code == 200)
                clock[0] += 61
                form = app.test_client()
                form.post("/login", data={"username": "zz_guest1", "password": PASSWORD})
                codes = [form.get("/").status_code for _ in range(5)]
                check("a guest logged in through the form is held to the same limit on the pages too: 3 allowed, then 429", codes == [200, 200, 200, 429, 429], str(codes))
        _authz._recent.clear()

        # ---- the Account Administration table (a page, so it needs the form login)
        def table(uid):
            with app.test_client() as c:
                c.post("/login", data={"username": uid, "password": PASSWORD})
                return c.get("/users/table2")

        r = table("zz_teacher")
        html = r.get_data(as_text=True)
        check("the table gives staff a Guest Window panel that says whether it is open",
              r.status_code == 200 and 'id="guestWindow"' in html and "open until" in html, f"{r.status_code}")
        staff("zz_teacher", "DELETE", "/api/guest/window")
        html = table("zz_teacher").get_data(as_text=True)
        check("and says when it is closed, with the Close button disabled",
              "closed</span>" in html and "open until" not in html and re.search(r'id="guestWindowClose"\s+disabled', html), "")
        r = table("zz_student")
        check("a student can see the table (200) but does not get the panel", r.status_code == 200 and 'id="guestWindowOpen"' not in r.get_data(as_text=True), f"{r.status_code}")
        with app.app_context():
            User.query.filter_by(_uid="zz_guest3").first().activate()
        set_verified_ago("zz_guest3", hours=4)
        check("(an expired guest that nothing has touched is still marked active)", row("zz_guest3")["active"] is True)
        table("zz_admin")
        check("opening the table marks expired guests inactive, and keeps them", row("zz_guest3")["active"] is False and count_users("zz_guest3") == 1)

        # ---- the rules come from the data
        staff("zz_teacher", "POST", "/api/guest/window", json={"minutes": 60})
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["lifetime_hours"] = 1
        set_verified_ago("zz_guest1", hours=2)
        with patch.object(_gt, "_data", changed):
            with app.test_client() as c:
                login_ok = c.post("/api/authenticate", json={"uid": "zz_guest1", "password": PASSWORD})
                check("the lifetime comes from the data: with 1 hour a guest verified 2 hours ago is made active again by logging in, for a new period",
                      login_ok.status_code == 200 and row("zz_guest1")["active"] is True, login_ok.status_code)
        set_verified_ago("zz_guest1", minutes=1)
        changed = copy.deepcopy(_gt._data)
        changed["account_types"]["guest"]["allowed_requests"] = ["GET /api/id", "GET /api/user"]
        with patch.object(_gt, "_data", changed):
            with app.test_client() as c:
                login(c, "zz_guest1")
                check("what a guest may do comes from the data: with the list changed, GET /api/user (200) is allowed and commenting (403) is not",
                      c.get("/api/user").status_code == 200 and c.post("/api/microblog/reply", json={"postId": post_id, "content": "x"}).status_code == 403)

        def refused(mutate):
            bad = copy.deepcopy(_gt._data)
            mutate(bad)
            try:
                _gt._check(bad)
                return False
            except ValueError:
                return True
        guest = lambda d: d["account_types"]["guest"]
        check("startup refuses a type that skips the GitHub check but has no list of allowed requests", refused(lambda d: guest(d).pop("allowed_requests")))
        check("startup refuses a type that skips the GitHub check without a Guest Window", refused(lambda d: guest(d).update(requires_open_window=False)) and refused(lambda d: guest(d).pop("requires_open_window")))
        check("startup refuses an empty list of allowed requests", refused(lambda d: guest(d).update(allowed_requests=[])))
        check("startup refuses a public type whose role is an Admin or Teacher role",
              refused(lambda d: guest(d).update(role="Teacher")) and refused(lambda d: guest(d).update(role=d["admin_role"])))
        check("startup refuses an allowed request that is not written as METHOD /path", refused(lambda d: guest(d).update(allowed_requests=["/api/id"])))
        check("startup refuses a lifetime, a window maximum or a request limit that is not a positive whole number",
              all(refused(lambda d, k=k, v=v: guest(d).update({k: v})) for k in ("lifetime_hours", "window_max_minutes", "requests_per_minute") for v in (0, "3", True)))
        check("startup refuses flags that are not true or false",
              refused(lambda d: guest(d).update(github_check="false")) and refused(lambda d: guest(d).update(active_on_signup="yes")) and refused(lambda d: guest(d).update(requires_open_window="yes")))
        check("startup refuses two account types that share a role", refused(lambda d: guest(d).update(role="User")))
    app.logger.removeHandler(audit)

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
