#!/usr/bin/env python3
"""Verify the Account Administration screen: the user table and the deactivate endpoint.

Runs the real app against a throwaway SQLite database and checksums the real dev DB before and after
to prove it was never touched. Complements scripts/verify_activation.py, which covers activation itself.

Usage:  python scripts/verify_account_admin.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, re, secrets, sys, tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-account-admin")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = secrets.token_urlsafe(16)  # new every run; the accounts live only in a throwaway database
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
        check("staff_verifies: true for a Mentor, false for a student (Google verifies students)",
              u("zz_mentor").staff_verifies and not u("zz_student").staff_verifies)
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
    check("a student gets no Activate or Verify (Google does that), whatever their state",
          all(a != "verify" for uid in ("zz_student", "zz_student_lapsed", "zz_student_pending") for a, _ in actions(html, uid)))
    check("a pending student has no buttons; an active student can be deactivated",
          actions(html, "zz_student_pending") == [] and actions(html, "zz_student") == [("deactivate", "Deactivate")])
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
    import copy
    from unittest.mock import patch
    from model import account_types
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

    after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("real dev DB untouched", real_before == after)
    print("\nALL PASSED" if not _failures else f"\nFAILURES: {_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
