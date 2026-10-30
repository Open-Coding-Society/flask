#!/usr/bin/env python3
"""Verify that every way of setting a password follows the one rule in model/user.py.

Runs the real app against a throwaway SQLite database and checksums the real dev DB
before and after to prove it was never touched.

Usage:  python scripts/verify_password_rule.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, secrets, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-password-rule")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
GOOD = "Aa1!" + secrets.token_urlsafe(16)  # new every run, with one of each character class
WEAK = ["", "Aa1!", "password", "password123", "PASSWORD123!", "Password123", "Password!!!"]
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
    from sqlalchemy import create_engine
    from unittest.mock import patch
    app, db = pkg.app, pkg.db
    engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='pwrule-'), 'new.db')}")
    with app.app_context():
        db.engines[None] = engine
    import main  # noqa: F401  registers every model and blueprint
    from model.user import User, PASSWORD_RULE, WeakPasswordError, meets_password_rule
    app.config["TESTING"] = True

    # The rule itself, same cases as Spring's PasswordCheckTest
    check("a password with every character class passes", meets_password_rule(GOOD))
    for weak in WEAK:
        check(f"'{weak}' fails the rule", not meets_password_rule(weak))
    check("None fails the rule", not meets_password_rule(None))
    for name in ("ADMIN_PASSWORD", "TEACHER_PASSWORD", "USER_PASSWORD", "DEFAULT_PASSWORD", "MY_PASSWORD"):
        check(f"{name} in the app config follows the rule", meets_password_rule(app.config[name]))

    with app.app_context():
        db.create_all()

        # The model is where weak passwords get rejected
        try:
            User(name="Weak", uid="zz_weak", password="password123")
            check("the model rejects a weak password", False)
        except WeakPasswordError as e:
            check("the model rejects a weak password with the rule as its message", e.description == PASSWORD_RULE)
        hashed = User(name="Hash", uid="zz_hash", password=GOOD)._password
        check("an existing hash (backup restore) still passes through unchanged",
              User(name="Copy", uid="zz_copy", password=hashed)._password == hashed)

        db.session.add(User(name="Adminxx", uid="zz_admin", password=GOOD, role="Admin"))
        db.session.add(User(name="Plain", uid="zz_plain", password=GOOD))
        db.session.commit()
        User.query.filter_by(_uid="zz_admin").first().activate()
        User.query.filter_by(_uid="zz_plain").first().activate()

        plain = User.query.filter_by(_uid="zz_plain").first()
        try:
            plain.update({"name": "Changed", "password": "weak"})
        except WeakPasswordError:
            pass
        check("a rejected password in update() leaves the other fields alone", plain.name == "Plain")
        db.session.rollback()

    def rejected(r):
        return r.status_code == 400 and (r.get_json() or {}).get("message") == PASSWORD_RULE

    with patch("api.user.GitHubUser", FakeGitHub):
        with app.test_client() as c:
            r = c.post("/api/user", json={"uid": "zz_signup_weak", "name": "Sign Up", "password": "password123",
                                          "accountType": "mentor"})
            check("signup: a weak password gets a 400 with the rule", rejected(r), f"{r.status_code} {r.get_data(as_text=True)[:120]}")
            r = c.post("/api/user", json={"uid": "zz_signup", "name": "Sign Up", "password": GOOD,
                                          "accountType": "mentor"})
            check("signup: a strong password is accepted", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:120]}")

            r = c.post("/api/user/guest", json={"uid": "zz_guest_weak", "password": "ab"})
            check("guest signup: a 2-character password is now rejected with the rule", rejected(r), f"{r.status_code} {r.get_data(as_text=True)[:120]}")
            r = c.post("/api/user/guest", json={"uid": "zz_guest", "password": GOOD})
            check("guest signup: a strong password is accepted", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:120]}")

            r = c.post("/api/reset-password",
                       json={"uid": "zz_plain", "resetToken": "not-a-real-token", "newPassword": "password123"})
            check("reset: a weak password is rejected before the token is checked", rejected(r), f"{r.status_code} {r.get_data(as_text=True)[:120]}")

            assert c.post("/api/authenticate", json={"uid": "zz_plain", "password": GOOD}).status_code == 200
            r = c.put("/api/user", json={"password": "password123"})
            check("profile edit: a weak password gets a 400 with the rule", rejected(r), f"{r.status_code} {r.get_data(as_text=True)[:120]}")
            r = c.post("/api/authenticate", json={"uid": "zz_plain", "password": GOOD})
            check("profile edit: the old password still works after the rejected change", r.status_code == 200)

        with app.test_client() as c:
            assert c.post("/api/authenticate", json={"uid": "zz_admin", "password": GOOD}).status_code == 200
            r = c.post("/api/user/admin-create", json={"accountType": "mentor", "name": "Staff", "uid": "zz_staff_weak",
                                                       "password": "password123"})
            check("admin-create: a weak password gets a 400 with the rule", rejected(r), f"{r.status_code} {r.get_data(as_text=True)[:120]}")

    real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("the real dev database was not touched", real_before == real_after)

    print(f"\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILED'}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
