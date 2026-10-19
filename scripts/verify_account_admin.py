#!/usr/bin/env python3
"""Verify the Account Administration screen: the user table and the deactivate endpoint.

Runs the real app against a throwaway SQLite database and checksums the real dev DB before and after
to prove it was never touched. Complements scripts/verify_activation.py, which covers activation itself.

Usage:  python scripts/verify_account_admin.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, re, sys, tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-account-admin")

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


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    import __init__ as pkg
    from sqlalchemy import create_engine
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='acctadmin-'), 'new.db')}")
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User
    app.config["TESTING"] = True

    # uid -> (role, state): the four states the table has to tell apart, for a Google-verified and a
    # staff-verified kind of account
    accounts = {
        "zz_admin": ("Admin", "verified"), "zz_teacher": ("Teacher", "verified"),
        "zz_student": ("User", "verified"), "zz_student_lapsed": ("User", "lapsed"), "zz_student_pending": ("User", "pending"),
        "zz_mentor": ("Mentor", "verified"), "zz_mentor_lapsed": ("Mentor", "lapsed"), "zz_mentor_pending": ("Mentor", "pending"),
    }
    with app.app_context():
        db.create_all()
        for uid, (role, state) in accounts.items():
            u = User(name=uid, uid=uid, password=PASSWORD, role=role)
            db.session.add(u)
            db.session.commit()
            if state != "pending":
                u.activate()
            if state == "lapsed":
                u.last_verified = datetime.utcnow() - timedelta(days=400)
                db.session.commit()

    def get(uid):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            return None if u is None else dict(role=u.role, active=u.active, state=u.verification_state,
                                               last_verified=u.last_verified)

    def login_api(c, uid):
        assert c.post("/api/authenticate", json={"uid": uid, "password": PASSWORD}).status_code == 200

    def table(uid):
        """Log in through the form; return the table page and the uids of the rows it shows."""
        with app.test_client() as c:
            c.post("/login", data={"username": uid, "password": PASSWORD})
            html = c.get("/users/table2").get_data(as_text=True)
        return html, re.findall(r'<tr id="user-\d+"[^>]*>\s*<td>\d+</td>\s*<td>(\w+)</td>', html)

    def row(html, uid):
        return re.search(r'<tr id="user-\d+"[^>]*>\s*<td>\d+</td>\s*<td>%s</td>.*?</tr>' % uid, html, re.S).group(0)

    def actions(html, uid):
        return re.findall(r'data-action="(\w+)"[^>]*>([^<]*)<', row(html, uid))

    # ---- the deactivate endpoint -------------------------------------------------------------
    with app.test_client() as c:
        login_api(c, "zz_student")  # active here, so the 403 comes from the role guard
        check("a non-staff account is refused on deactivate (403)",
              c.post("/api/user/zz_mentor_pending/deactivate").status_code == 403)
    with app.test_client() as c:
        login_api(c, "zz_teacher")
        before = get("zz_mentor")
        r = c.post("/api/user/zz_mentor/deactivate")
        after = get("zz_mentor")
        check("a Teacher can deactivate an account (200)", r.status_code == 200 and after["active"] is False
              and after["state"] == "inactive", f"{r.status_code} {after}")
        check("deactivating keeps the account, its role and last_verified",
              after["role"] == before["role"] and after["last_verified"] == before["last_verified"])
        with app.test_client() as m:
            login_api(m, "zz_mentor")
            check("a deactivated account is refused on protected endpoints (403)", m.get("/api/user").status_code == 403)
        check("a deactivated Mentor can be verified again (reactivation)",
              c.post("/api/user/zz_mentor/verify").status_code == 200 and get("zz_mentor")["active"] is True)
        check("deactivating an unknown uid is 404", c.post("/api/user/zz_nobody/deactivate").status_code == 404)
    with app.test_client() as c:
        login_api(c, "zz_admin")
        check("an Admin can deactivate too", c.post("/api/user/zz_mentor_lapsed/deactivate").status_code == 200)

    # the table's buttons call the API with only the page's own login session (no separate token)
    with app.test_client() as c:
        c.post("/login", data={"username": "zz_teacher", "password": PASSWORD})
        check("the buttons work with the table page's own login session: Deactivate",
              c.post("/api/user/zz_mentor/deactivate").status_code == 200 and get("zz_mentor")["active"] is False)
        check("the buttons work with the table page's own login session: Activate",
              c.post("/api/user/zz_mentor/verify").status_code == 200 and get("zz_mentor")["active"] is True)

    # put the accounts back to their starting states for the table checks
    with app.app_context():
        for uid, (role, state) in accounts.items():
            u = User.query.filter_by(_uid=uid).first()
            if state == "pending":
                u.active, u.last_verified = False, None
            else:
                u.active, u.last_verified = True, datetime.utcnow() - (timedelta(days=400) if state == "lapsed" else timedelta(0))
        db.session.commit()

    # ---- the two named properties -------------------------------------------------------------
    with app.app_context():
        u = lambda uid: User.query.filter_by(_uid=uid).first()
        check("verification_prompt_days: asked for a student (30), not for a Mentor (fixed) or an Admin (never re-verified)",
              u("zz_student").verification_prompt_days == 30 and u("zz_mentor").verification_prompt_days is None
              and u("zz_admin").verification_prompt_days is None)
        check("verification_due: false only when active and verified",
              not u("zz_mentor").verification_due and u("zz_mentor_lapsed").verification_due and u("zz_mentor_pending").verification_due)

    # ---- who sees what in the table ----------------------------------------------------------
    everyone = sorted(accounts)
    html, rows = table("zz_teacher")
    check("a Teacher sees every account", sorted(rows) == everyone, f"{len(rows)} of {len(everyone)}")
    html_admin, rows = table("zz_admin")
    check("an Admin sees every account", sorted(rows) == everyone)
    for uid in ("zz_student", "zz_mentor"):
        h, rows = table(uid)
        check(f"a {accounts[uid][0]} sees only their own row, with no status buttons or filters",
              rows == [uid] and 'data-action="' not in h and 'id="accountFilters"' not in h, str(rows))

    # ---- the status badge for each state ----------------------------------------------------
    badge = lambda uid: re.search(r'<span class="badge[^>]*>([^<]*)</span>', row(html, uid)).group(1)
    check("badges: Active, Active - verification required, Inactive",
          badge("zz_mentor") == "Active" and badge("zz_mentor_lapsed") == "Active - verification required"
          and badge("zz_mentor_pending") == "Inactive", f"{badge('zz_mentor')} | {badge('zz_mentor_lapsed')} | {badge('zz_mentor_pending')}")

    # ---- the buttons for each state, as a Teacher -------------------------------------------
    check("a pending Mentor gets Activate only", actions(html, "zz_mentor_pending") == [("verify", "Activate")], str(actions(html, "zz_mentor_pending")))
    check("a Mentor whose verification lapsed gets Verify and Deactivate",
          actions(html, "zz_mentor_lapsed") == [("verify", "Verify"), ("deactivate", "Deactivate")], str(actions(html, "zz_mentor_lapsed")))
    check("an up-to-date Mentor gets Deactivate only", actions(html, "zz_mentor") == [("deactivate", "Deactivate")])
    check("students get Activate or Verify like any account: pending -> Activate, lapsed -> Verify and Deactivate, up to date -> Deactivate",
          actions(html, "zz_student_pending") == [("verify", "Activate")]
          and actions(html, "zz_student_lapsed") == [("verify", "Verify"), ("deactivate", "Deactivate")]
          and actions(html, "zz_student") == [("deactivate", "Deactivate")])
    prompt = lambda uid: re.search(r'data-action="verify"( data-prompt-days="(\d+)")?', row(html, uid))
    check("the button asks for days (default 30) for a student, and not for a Mentor",
          prompt("zz_student_pending").group(2) == "30" and prompt("zz_mentor_pending").group(2) is None)
    check("your own row has no Deactivate button", actions(html, "zz_teacher") == [])
    check("a Teacher does not get Delete or Reset Password",
          'delete-btn" data-id' not in html and 'reset-password-btn" data-id' not in html)
    check("an Admin keeps Delete and Reset Password", 'delete-btn" data-id' in html_admin and 'reset-password-btn" data-id' in html_admin)

    # ---- the filters -----------------------------------------------------------------------
    check("staff get the All / Active / Inactive buttons",
          all(f'data-status="{v}"' in html for v in ("all", "true", "false")))
    options = re.findall(r'<option value="([^"]*)">', html.split('id="roleFilter"')[1].split("</select>")[0])
    with app.app_context():
        present = {u.role for u in User.query.all()}
    check("the Role filter lists All plus exactly the roles present (none hardcoded)", options == [""] + sorted(present), str(options))
    states = {uid: (re.search(r'data-role="(\w+)" data-active="(\w+)"', row(html, uid)).groups()) for uid in accounts}
    check("each row carries its role and active state for the filters",
          states["zz_mentor"] == ("Mentor", "true") and states["zz_mentor_pending"] == ("Mentor", "false")
          and states["zz_student_pending"] == ("User", "false"), str(states))

    # ---- how long an activation by a Teacher or Admin covers -----------------------------------
    import copy
    from unittest.mock import patch
    from model import account_types
    with app.app_context():  # accounts of their own, all inactive
        for uid, role in (("zz_v_student", "User"), ("zz_v_student2", "User"), ("zz_v_mentor", "Mentor"),
                          ("zz_v_teacher", "Teacher"), ("zz_v_bad", "User")):
            db.session.add(User(name=uid, uid=uid, password=PASSWORD, role=role))
        db.session.commit()
    interval = account_types.verification_interval_days()

    def days_left(uid):
        with app.app_context():
            u = User.query.filter_by(_uid=uid).first()
            return None if u.last_verified is None else interval - (datetime.utcnow() - u.last_verified).total_seconds() / 86400

    with app.test_client() as c:
        login_api(c, "zz_teacher")
        r = c.post("/api/user/zz_v_student/verify")
        check("activating a student with no days covers the default (30)",
              r.status_code == 200 and get("zz_v_student")["active"] and abs(days_left("zz_v_student") - 30) < 0.01, days_left("zz_v_student"))
        r = c.post("/api/user/zz_v_student2/verify", json={"days": 10})
        check("activating a student with days=10 covers 10 days and reads as verified",
              r.status_code == 200 and abs(days_left("zz_v_student2") - 10) < 0.01 and get("zz_v_student2")["state"] == "active_verified")
        check("a Teacher can grant up to the full interval",
              c.post("/api/user/zz_v_student2/verify", json={"days": interval}).status_code == 200
              and abs(days_left("zz_v_student2") - interval) < 0.01)
        for bad in (0, interval + 1, -5, "x", 1.5, True, None):
            r = c.post("/api/user/zz_v_bad/verify", json={"days": bad})
            check(f"days={bad!r} is refused (400) and the account stays inactive",
                  r.status_code == 400 and get("zz_v_bad")["active"] is False, r.status_code)
        for odd in ([], [30], "30", 5):
            r = c.post("/api/user/zz_v_bad/verify", json=odd)
            check(f"a body that is not a JSON object ({odd!r}) is a 400, not a crash",
                  r.status_code == 400 and get("zz_v_bad")["active"] is False, r.status_code)
        r = c.post("/api/user/zz_v_mentor/verify", json={"days": 5})
        check("a Mentor always gets the full 365 days; days in the request are ignored",
              r.status_code == 200 and abs(days_left("zz_v_mentor") - 365) < 0.01, days_left("zz_v_mentor"))
        r = c.post("/api/user/zz_v_teacher/verify")
        check("a Teacher account can be activated too", r.status_code == 200 and get("zz_v_teacher")["active"] is True)
    with app.app_context():
        t = User.query.filter_by(_uid="zz_v_teacher").first()
        t.last_verified = datetime.utcnow() - timedelta(days=900)
        db.session.commit()
    check("Teacher and Admin accounts never need re-verification, however old last_verified is",
          get("zz_v_teacher")["state"] == "active_verified" and get("zz_admin")["state"] == "active_verified")

    changed = copy.deepcopy(account_types._data)
    changed["default_verification_days"] = 7
    changed["verification_exempt_roles"] = ["Mentor"]
    for t in changed["account_types"].values():
        t.pop("verification_days", None)
    with app.app_context():
        db.session.add(User(name="zz_v_data", uid="zz_v_data", password=PASSWORD, role="User"))
        db.session.commit()
    with patch.object(account_types, "_data", changed):
        with app.test_client() as c:
            login_api(c, "zz_teacher")
            c.post("/api/user/zz_v_data/verify")
        with app.app_context():
            u = User.query.filter_by(_uid="zz_v_data").first()
            m = User.query.filter_by(_uid="zz_mentor").first()
            seven = abs(interval - (datetime.utcnow() - u.last_verified).total_seconds() / 86400 - 7) < 0.01
            m.last_verified = datetime.utcnow() - timedelta(days=900)
            mentor_exempt = m.verification_state == "active_verified"
            teacher_lapses = User.query.filter_by(_uid="zz_v_teacher").first().verification_state == "active_verification_required"
    check("the numbers and the exempt roles come from the data: default 7 days, Mentor exempt, Teacher no longer exempt",
          seven and mentor_exempt and teacher_lapses, f"{seven} {mentor_exempt} {teacher_lapses}")

    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
