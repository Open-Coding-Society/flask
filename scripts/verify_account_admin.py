#!/usr/bin/env python3
"""Verify the Account Administration screen: the user table and the deactivate endpoint.

Runs the real app against a throwaway SQLite database and checksums the real dev DB before and after
to prove it was never touched. Complements scripts/verify_activation.py, which covers activation itself.

Usage:  python scripts/verify_account_admin.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import copy, hashlib, os, re, secrets, sys, tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-account-admin")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run, with one of each character class; the accounts live only in a throwaway database
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
    from model import account_types
    from model.user import User
    app.config["TESTING"] = True

    # uid -> (role, state): the four states the table has to tell apart, for a Google-verified and a
    # staff-verified kind of account
    accounts = {
        "zz_admin": ("Admin", "verified"), "zz_admin2": ("Admin", "verified"),
        "zz_teacher": ("Teacher", "verified"), "zz_teacher2": ("Teacher", "verified"),
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

    # ---- John's rules: protect Admin accounts and the last Admin and Teacher ---------------
    def set_active(uid, value):
        with app.app_context():
            User.query.filter_by(_uid=uid).first().active = value
            db.session.commit()

    def exists(uid):
        return get(uid) is not None

    def table_delete(login_uid, target_id):
        with app.test_client() as c:
            c.post("/login", data={"username": login_uid, "password": PASSWORD})
            return c.delete(f"/users/delete/{target_id}")

    with app.test_client() as c:
        login_api(c, "zz_teacher")
        r = c.post("/api/user/zz_admin/deactivate")
        check("a Teacher cannot deactivate an Admin (403) and the Admin stays active",
              r.status_code == 403 and get("zz_admin")["active"] is True, r.status_code)
    with app.test_client() as c:
        login_api(c, "zz_admin")
        check("an Admin can deactivate another Admin while another active Admin remains",
              c.post("/api/user/zz_admin2/deactivate").status_code == 200 and get("zz_admin2")["active"] is False)
        r = c.post("/api/user/zz_admin/deactivate")
        check("the last active Admin cannot be deactivated (409), not even by themselves",
              r.status_code == 409 and get("zz_admin")["active"] is True, f"{r.status_code} {r.get_json()}")
        check("a Teacher can be deactivated while another active Teacher remains",
              c.post("/api/user/zz_teacher2/deactivate").status_code == 200)
        r = c.post("/api/user/zz_teacher/deactivate")
        check("the last active Teacher cannot be deactivated (409)", r.status_code == 409 and get("zz_teacher")["active"] is True,
              f"{r.status_code} {r.get_json()}")
    set_active("zz_admin2", True)
    set_active("zz_teacher2", True)

    with app.app_context():  # accounts of their own, so the table checks above are unaffected
        for uid, role in (("zz_gone_admin", "Admin"), ("zz_gone_mentor", "Mentor"), ("zz_gone_mentor2", "Mentor"), ("zz_gone_mentor3", "Mentor")):
            u = User(name=uid, uid=uid, password=PASSWORD, role=role)
            db.session.add(u)
            db.session.commit()
            u.activate()
        ids = {u.uid: u.id for u in User.query.all()}
    with app.test_client() as c:
        login_api(c, "zz_teacher")
        r = c.delete("/api/user", json={"uid": "zz_gone_mentor"})
        check("a Teacher cannot delete an account through the API (403)", r.status_code == 403 and exists("zz_gone_mentor"), r.status_code)
    r = table_delete("zz_teacher", ids["zz_gone_mentor"])
    check("a Teacher cannot delete an account through the table (403)", r.status_code == 403 and exists("zz_gone_mentor"), r.status_code)
    r = table_delete("zz_student", ids["zz_gone_mentor"])
    check("a student cannot delete an account through the table (403)", r.status_code == 403 and exists("zz_gone_mentor"), r.status_code)
    r = table_delete("zz_admin", ids["zz_admin"])
    check("an Admin cannot delete their own account through the table (403)", r.status_code == 403 and exists("zz_admin"), r.status_code)
    with app.test_client() as c:
        login_api(c, "zz_admin")
        r = c.delete("/api/user", json={"uid": "zz_admin"})
        check("an Admin cannot delete their own account through the API (403)", r.status_code == 403 and exists("zz_admin"), r.status_code)
        check("an Admin can delete another Admin through the API (204)",
              c.delete("/api/user", json={"uid": "zz_gone_admin"}).status_code == 204 and not exists("zz_gone_admin"))
    r = table_delete("zz_admin", ids["zz_gone_mentor"])
    check("an Admin can delete another account through the table (200)", r.status_code == 200 and not exists("zz_gone_mentor"), r.status_code)
    set_active("zz_teacher2", False)
    r = table_delete("zz_admin", ids["zz_teacher"])
    check("the last active Teacher cannot be deleted (409)", r.status_code == 409 and exists("zz_teacher"), r.status_code)
    set_active("zz_teacher2", True)
    # a deactivated Admin whose login session is still open has no rights
    admin_browser = app.test_client()
    admin_browser.post("/login", data={"username": "zz_admin2", "password": PASSWORD})
    set_active("zz_admin2", False)
    r = admin_browser.delete(f"/users/delete/{ids['zz_gone_mentor3']}")
    check("a deactivated Admin's still-open session cannot delete (403)", r.status_code == 403 and exists("zz_gone_mentor3"), r.status_code)
    set_active("zz_admin2", True)

    # the must-keep roles come from the data (model/account_types.json), not from code
    set_active("zz_admin2", False)
    changed = copy.deepcopy(account_types._data)
    changed["essential_roles"] = ["Mentor"]
    with app.app_context():
        sole_admin = User.query.filter_by(_uid="zz_admin").first()
        protected_by_default = sole_admin.is_last_active_essential()
        with patch.object(account_types, "_data", changed):
            protected_when_changed = sole_admin.is_last_active_essential()
    set_active("zz_admin2", True)
    check("the protected roles follow the data: the only Admin is protected by default, not once the data drops Admin",
          protected_by_default is True and protected_when_changed is False, f"{protected_by_default} {protected_when_changed}")

    # what the table offers
    html_t, _ = table("zz_teacher")
    html_a, _ = table("zz_admin")
    check("a Teacher is offered no Deactivate on an Admin's row", actions(html_t, "zz_admin") == [], str(actions(html_t, "zz_admin")))
    check("an Admin is offered Deactivate on another Admin's row, not on their own",
          ("deactivate", "Deactivate") in actions(html_a, "zz_admin2") and actions(html_a, "zz_admin") == [])
    check("an Admin is offered Delete on another row, but not on their own",
          "delete-btn" in row(html_a, "zz_gone_mentor2") and "delete-btn" not in row(html_a, "zz_admin"))
    set_active("zz_teacher2", False)
    html_a2, _ = table("zz_admin")
    check("the last active Teacher's row offers neither Deactivate nor Delete",
          actions(html_a2, "zz_teacher") == [] and "delete-btn" not in row(html_a2, "zz_teacher"))
    set_active("zz_teacher2", True)

    # ---- changing roles -------------------------------------------------------------------
    with app.app_context():  # accounts of their own
        for uid, role in (("zz_r_student", "User"), ("zz_r_mentor", "Mentor"), ("zz_r_admin", "Admin"), ("zz_r_teacher", "Teacher")):
            u = User(name=uid, uid=uid, password=PASSWORD, role=role)
            db.session.add(u)
            db.session.commit()
            u.activate()

    def role_of(uid):
        return get(uid)["role"]

    with app.test_client() as c:
        login_api(c, "zz_teacher")
        r = c.post("/api/user/zz_r_student/role", json={"role": "Mentor"})
        check("a Teacher can change a student to a mentor (200)", r.status_code == 200 and role_of("zz_r_student") == "Mentor", r.status_code)
        r = c.post("/api/user/zz_r_student/role", json={"role": "Guest"})
        check("a Teacher cannot turn an existing account into a Guest (400): that role is for sign-up only",
              r.status_code == 400 and role_of("zz_r_student") != "Guest", f"{r.status_code} {role_of('zz_r_student')}")
        check("... and to a teacher, and back to a student",
              c.post("/api/user/zz_r_student/role", json={"role": "Teacher"}).status_code == 200
              and c.post("/api/user/zz_r_student/role", json={"role": "User"}).status_code == 200 and role_of("zz_r_student") == "User")
        r = c.post("/api/user/zz_r_student/role", json={"role": "Admin"})
        check("a Teacher cannot make someone an Admin (403)", r.status_code == 403 and role_of("zz_r_student") == "User", r.status_code)
        r = c.post("/api/user/zz_r_admin/role", json={"role": "Mentor"})
        check("a Teacher cannot change an Admin's role (403)", r.status_code == 403 and role_of("zz_r_admin") == "Admin", r.status_code)
        r = c.post("/api/user/zz_teacher/role", json={"role": "Mentor"})
        check("nobody changes their own role: a Teacher is refused (403)", r.status_code == 403 and role_of("zz_teacher") == "Teacher", r.status_code)
        check("a role that does not exist is a 400", c.post("/api/user/zz_r_student/role", json={"role": "Wizard"}).status_code == 400)
        for odd in ({}, {"role": None}, [], "Mentor"):
            check(f"a missing or malformed role ({odd!r}) is a 400",
                  c.post("/api/user/zz_r_student/role", json=odd).status_code == 400 and role_of("zz_r_student") == "User")
        check("an unknown uid is 404", c.post("/api/user/zz_nobody/role", json={"role": "Mentor"}).status_code == 404)
    with app.test_client() as c:
        login_api(c, "zz_student")
        check("a non-staff account is refused (403)", c.post("/api/user/zz_r_mentor/role", json={"role": "User"}).status_code == 403
              and role_of("zz_r_mentor") == "Mentor")
    with app.test_client() as c:
        login_api(c, "zz_admin")
        check("an Admin can make a Teacher an Admin (200)",
              c.post("/api/user/zz_r_teacher/role", json={"role": "Admin"}).status_code == 200 and role_of("zz_r_teacher") == "Admin")
        check("an Admin can change another Admin to something else (200)",
              c.post("/api/user/zz_r_teacher/role", json={"role": "Teacher"}).status_code == 200 and role_of("zz_r_teacher") == "Teacher")
        r = c.post("/api/user/zz_admin/role", json={"role": "Teacher"})
        check("nobody changes their own role: an Admin is refused (403)", r.status_code == 403 and role_of("zz_admin") == "Admin", r.status_code)
        set_active("zz_teacher2", False)
        set_active("zz_r_teacher", False)
        r = c.post("/api/user/zz_teacher/role", json={"role": "Mentor"})
        check("the last active Teacher cannot be changed to another role (409)",
              r.status_code == 409 and role_of("zz_teacher") == "Teacher", f"{r.status_code} {r.get_json()}")
        set_active("zz_teacher2", True)
        set_active("zz_r_teacher", True)
        check("setting the role an account already has is accepted",
              c.post("/api/user/zz_r_mentor/role", json={"role": "Mentor"}).status_code == 200)
    changed = copy.deepcopy(account_types._data)
    changed["staff_roles"] = ["Observer", "Admin"]
    with patch.object(account_types, "_data", changed):
        with app.test_client() as c:
            login_api(c, "zz_admin")
            a = c.post("/api/user/zz_r_mentor/role", json={"role": "Observer"}).status_code
            b = c.post("/api/user/zz_r_mentor/role", json={"role": "Teacher"}).status_code
    check("the roles that can be given come from the data: Observer is accepted and Teacher is refused once the data says so",
          a == 200 and b == 400 and role_of("zz_r_mentor") == "Observer", f"{a} {b} {role_of('zz_r_mentor')}")
    with app.app_context():
        User.query.filter_by(_uid="zz_r_mentor").first().role = "Mentor"
        db.session.commit()

    with app.app_context():  # an account whose role is no longer in the list can still be fixed
        u = User(name="zz_r_legacy", uid="zz_r_legacy", password=PASSWORD, role="Pending")
        db.session.add(u)
        db.session.commit()
        u.activate()
    with app.test_client() as c:
        login_api(c, "zz_teacher")
        check("an account with a role that is no longer offered can still be moved to a valid one",
              c.post("/api/user/zz_r_legacy/role", json={"role": "User"}).status_code == 200 and role_of("zz_r_legacy") == "User")
    with app.app_context():
        User.query.filter_by(_uid="zz_r_legacy").first().role = "Pending"
        db.session.commit()

    # the Edit button and its dialog
    html_t, _ = table("zz_teacher")
    html_a, _ = table("zz_admin")
    edit = lambda h, uid: 'edit-btn" data-uid="%s"' % uid in row(h, uid)
    check("a Teacher gets Edit on a student and on another Teacher, not on an Admin or on their own row",
          edit(html_t, "zz_student") and edit(html_t, "zz_teacher2") and not edit(html_t, "zz_admin") and not edit(html_t, "zz_teacher"))
    html_t, _ = table("zz_teacher")
    check("Edit is still offered on an account whose current role is no longer in the list", edit(html_t, "zz_r_legacy"))
    check("an Admin gets Edit on another Admin, not on their own row", edit(html_a, "zz_admin2") and not edit(html_a, "zz_admin"))
    h, _ = table("zz_student")
    check("a student has no Edit button and no role dialog", "edit-btn" not in h.split("<script>")[0] and 'id="roleModal"' not in h)
    options = lambda h: re.findall(r'<option value="(\w+)">\1</option>', h.split('id="roleSelect"')[1].split("</select>")[0])
    check("the role dialog offers a Teacher User, Mentor and Teacher; an Admin also gets Admin (from the data)",
          options(html_t) == ["User", "Mentor", "Teacher"] and options(html_a) == ["User", "Mentor", "Teacher", "Admin"],
          f"{options(html_t)} | {options(html_a)}")

    # ---- how long an activation by a Teacher or Admin covers -----------------------------------
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

    # bulk import is for the admin role only, and the role is read from the data
    with app.test_client() as c:
        check("bulk import: nobody logged in is refused (401)", c.post("/api/users", json=[]).status_code == 401)
    with app.test_client() as c:
        login_api(c, "zz_teacher")
        check("bulk import: a Teacher is refused (403)", c.post("/api/users", json=[]).status_code == 403)
    with app.test_client() as c:
        login_api(c, "zz_admin")
        r = c.post("/api/users", json={})
        check("bulk import: an Admin gets as far as the request being checked (400, not a list)", r.status_code == 400, r.status_code)

    # the table asks the user who they are (is_admin, is_teacher); it does not name roles itself
    with open(os.path.join(REPO, "templates", "u2table.html"), encoding="utf-8") as fh:
        named = re.findall(r"""['"](?:Admin|Teacher|Mentor|User)['"]""", fh.read())
    check("the table template names no roles", not named, str(named))
    for name in ("api/user.py", "main.py"):
        with open(os.path.join(REPO, name), encoding="utf-8") as fh:
            named = re.findall(r"""['"](?:Admin|Teacher|Mentor|User)['"]""", fh.read())
        check(f"{name} names no roles", not named, str(named))

    # which role is the Admin role comes from the data (model/account_types.json), not from code
    changed = copy.deepcopy(account_types._data)
    changed["admin_role"] = "Principal"
    with app.app_context():
        principal = User(name="zz_principal", uid="zz_principal", password=PASSWORD, role="Principal")
        admin = User.query.filter_by(_uid="zz_admin").first()
        by_default = (admin.is_admin(), principal.is_admin())
        with patch.object(account_types, "_data", changed):
            when_changed = (admin.is_admin(), principal.is_admin())
    check("the Admin role follows the data: Admin is the admin by default, Principal once the data says so",
          by_default == (True, False) and when_changed == (False, True), f"{by_default} {when_changed}")
    typo = copy.deepcopy(account_types._data)
    typo["admin_role"] = "Admn"
    try:
        account_types._check(typo)
        refused = False
    except ValueError:
        refused = True
    check("an admin_role that is not one of the staff roles is refused at startup", refused)

    # who counts as staff comes from the data (model/account_types.json), not from code
    changed = copy.deepcopy(account_types._data)
    changed["staff_roles"] = ["Teacher"]
    with app.app_context():
        admin = User.query.filter_by(_uid="zz_admin").first()
        staff_by_default = admin.is_staff()
        with patch.object(account_types, "_data", changed):
            staff_when_changed = admin.is_staff()
    check("staff follow the data: an Admin is staff by default, not once the data drops Admin",
          staff_by_default is True and staff_when_changed is False, f"{staff_by_default} {staff_when_changed}")

    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
