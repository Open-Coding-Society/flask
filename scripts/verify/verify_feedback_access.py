#!/usr/bin/env python3
"""Verify who may read feedback: model/dataconfigs/permissions.json decides who may reach each route, and
GET /api/feedback/user/<uid> decides whose feedback an account may read.

  * GET /api/feedback/all is for an Admin only: a Teacher, a student and a caller with no account are refused.
  * GET /api/feedback/user/<uid> needs an account; an account reads its own feedback, an Admin reads anyone's,
    and everyone else gets 403 with none of the other account's feedback in the answer.
  * POST /api/feedback/ is still open to a caller with no account (the feedback form allows "Anonymous").

Runs against a throwaway SQLite DB, with the GitHub API replaced so nothing is sent, and checksums the real dev DB
before and after to prove it was never touched.

Usage:  python scripts/verify/verify_feedback_access.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, os, secrets, sys, tempfile
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-feedback-access")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run; the accounts live only in a throwaway database
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    tmp = tempfile.mkdtemp(prefix="feedback-access-")

    import __init__ as pkg
    from sqlalchemy import create_engine
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tmp, 'new.db')}")
    import main  # noqa: F401  registers every model and blueprint
    from model.feedback import Feedback
    from model.user import User

    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False  # an error in a route answers 500, as it does in production
    with app.app_context():
        db.create_all()
        for uid, role in (("zz_admin", "Admin"), ("zz_teacher", "Teacher"), ("zz_student", "User"), ("zz_other", "User")):
            db.session.add(User(name=f"Name {uid}", uid=uid, password=PASSWORD, role=role))
        for u in User.query.all():
            u.activate()
        db.session.commit()
        Feedback("Mine", "my own words", "Bug", "zz_student").create()
        Feedback("Theirs", "someone else's words", "Bug", "zz_other").create()

    def login(c, uid):
        r = c.post("/login", data={"username": uid, "password": PASSWORD})
        assert r.status_code in (200, 302), (uid, r.status_code)

    def titles(response):
        try:
            return sorted(item["title"] for item in response.get_json())
        except Exception:
            return None

    # ---- all feedback: an Admin only
    with app.test_client() as c:
        check("all feedback: a caller with no account is refused with 401", c.get("/api/feedback/all").status_code == 401)
    for uid in ("zz_student", "zz_teacher"):
        with app.test_client() as c:
            login(c, uid)
            r = c.get("/api/feedback/all")
            check(f"all feedback: {uid} is refused with 403, and none of it is in the answer", r.status_code == 403 and b"words" not in r.data, str(r.status_code))
    with app.test_client() as c:
        login(c, "zz_admin")
        r = c.get("/api/feedback/all")
        check("all feedback: an Admin gets all of it", r.status_code == 200 and len(r.get_json()) == 2, str(r.status_code))

    # ---- one account's feedback: its own, or any for an Admin
    with app.test_client() as c:
        check("one account's feedback: a caller with no account is refused with 401", c.get("/api/feedback/user/zz_student").status_code == 401)
    with app.test_client() as c:
        login(c, "zz_student")
        r = c.get("/api/feedback/user/zz_student")
        check("one account's feedback: a student reads their own", r.status_code == 200 and titles(r) == ["Mine"], f"{r.status_code} {titles(r)}")
        r = c.get("/api/feedback/user/zz_other")
        check("one account's feedback: a student asking for another account gets 403, with none of it in the answer",
              r.status_code == 403 and b"Theirs" not in r.data and b"someone else" not in r.data, str(r.status_code))
        r = c.get("/api/feedback/user/zz_nobody")
        check("one account's feedback: and asking for an account that does not exist is 403 as well, so it does not say who exists", r.status_code == 403, str(r.status_code))
    with app.test_client() as c:
        login(c, "zz_admin")
        r = c.get("/api/feedback/user/zz_other")
        check("one account's feedback: an Admin reads any account's", r.status_code == 200 and titles(r) == ["Theirs"], f"{r.status_code} {titles(r)}")

    # ---- sending feedback stays open to a caller with no account
    sent = mock.Mock(status_code=500)
    sent.json.return_value = {}
    with mock.patch("api.feedback_api.requests.post", return_value=sent) as github, app.test_client() as c:
        r = c.post("/api/feedback/", json={"title": "Anonymous note", "body": "from nobody", "type": "Other"})
        check("sending feedback: a caller with no account is still accepted", r.status_code == 200 and github.called, str(r.status_code))
    with app.app_context():
        saved = Feedback.query.filter_by(title="Anonymous note").first()
        check("sending feedback: and it is saved as Anonymous", saved is not None and saved.github_username == "Anonymous")

    real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("the real dev database was not touched", real_before == real_after)
    print(f"\n{len(_failures)} failed" if _failures else "\nall checks passed")
    sys.exit(1 if _failures else 0)


main()
