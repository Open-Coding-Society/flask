#!/usr/bin/env python3
"""Verify api/access.py, the check every request passes before it reaches a route, and that enforcing
model/dataconfigs/permissions.json changes no answer the app gives today.

  1. The check on a small app: what it decides, what it logs in shadow mode, when it refuses, and that an error
     in it never fails a request in shadow mode. Then who is calling, on this app with a throwaway database: a
     token, a session, a token from before a password change, and that it is worked out once for each request.
  2. This project in shadow mode, every route as every kind of caller in both modes: the decision made from the
     file is the one the route's own guard makes, for every call, except for the requests in HELD_TIGHTER.
  3. The same calls with the file enforced for everyone, and enforced as the file is committed when that is
     something in between: every answer is the same as in shadow mode, except that a caller the file does not let
     make a request in HELD_TIGHTER is now refused, 401 or 403. This is the gate for turning enforcement on.
  4. What only enforcement gives: a route that is in no list refuses everyone, and an open route is closed by
     moving its entry, with no change to the code.

Every call is made by scripts/rbac/rbac_prober.py on a throwaway copy of the app, with no network and no provider keys.

Usage:  python scripts/verify/verify_access.py        (from the flask repo root; about four minutes)
Exit:   0 all checks passed, 1 otherwise.
"""
import datetime, hashlib, json, logging, os, secrets, sys, tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts", "rbac"))
os.environ.setdefault("SECRET_KEY", "verify-access")

import rbac_prober as prober
import rbac_reconcile as rec

POLICY = "model/dataconfigs/permissions.json"
_failures = []

# Requests the file holds tighter than the route's own guard, on purpose: each was open to anyone, or to any
# account, until legacy_open was sorted, and the file now says who may make it. The check before the route refuses
# first, so for these and only these an answer changes when enforcement is on, from what the route gave to a refusal.
# The list is exact: a request that is tightened without being added here fails the gate, and so does an entry
# whose answers do not change.
HELD_TIGHTER = [
    "GET /api/analytics/github/org/<string:org_name>/repos",
    "GET /api/analytics/github/org/<string:org_name>/users",
    "GET /api/feedback/all",
    "GET /api/feedback/user/<string:uid>",
    "GET /api/groq/health",
    "GET /api/groq/models",
    "GET /api/persona",
    "GET /api/persona/<int:id>",
    "GET /api/post/<int:post_id>",
    "GET /api/post/all",
    "GET /api/post/page",
    "GET /api/post/user/<int:user_id>",
    "GET /api/study",
    "GET /api/study/stats",
    "GET /uploads/<path:filename>",
    "POST /api/groq",
    "POST /api/groq/analyze",
    "POST /api/groq/chat",
    "POST /api/persona/evaluate-group",
    "POST /api/persona/form-groups",
    "POST /api/study",
    "PUT /update_user/<string:uid>",
]


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def policy(change=None, enforce=None):
    """The text of permissions.json with a change made to it, for the prober to use in its copy of the app."""
    with open(os.path.join(REPO, POLICY), encoding="utf-8") as fh:
        data = json.load(fh)
    if enforce is not None:
        data["enforce"] = enforce
    if change:
        change(data)
    return json.dumps(data, indent=2)


def everyone():
    """Every caller the file can be enforced for: anonymous and every role."""
    from model import account_types
    return ["anonymous"] + sorted(set(account_types.assignable_roles()) | set(account_types.guest_roles()))


def answers(records):
    """What a caller can see of each call: the status, where it was sent, whether canary data came back, whether it wrote."""
    return {(r["mode"], r["method"], r["rule"], r["caller"]): (r["status"], r["redirect"], bool(r["canary"]), r["wrote"]) for r in records}


def decision_problems(inventory, probes):
    """Calls where the decision made from the file is not the one the route's own guard makes."""
    guards = {(r["method"], r["rule"]): r for r in inventory}
    problems = []
    for probe in probes:
        guard = rec.guard_in_mode(guards[(probe["method"], probe["rule"])], probe["mode"])
        # a device route is left to its own guard, which checks the key: the check before it lets every caller through
        through = True if guard["kind"] == "device_key" else rec.expected(guard, probe["caller"], probe["role"])
        decision = probe["decision"]
        held_tighter = f'{probe["method"]} {probe["rule"]}' in HELD_TIGHTER and through and decision is not None and not decision["allow"]
        if (decision is None or decision["allow"] != through) and not held_tighter:
            problems.append(f'{probe["method"]} {probe["rule"]} in {probe["mode"]} as {probe["caller"]}: the guard says {"pass" if through else "refuse"}, '
                            f'the file says {None if decision is None else ("pass" if decision["allow"] else "refuse")}')
    return problems


def unexpected_changes(before, after):
    """(changes nobody listed, changes that are not a refusal, requests in HELD_TIGHTER whose answers did not change).

    The answers may differ only for a request in HELD_TIGHTER, and only to a 401 or 403 from the check.
    """
    one, two = answers(before), answers(after)
    unlisted, not_refused, changed = [], [], set()
    for key in sorted(set(one) | set(two), key=str):
        if one.get(key) == two.get(key):
            continue
        request = f"{key[1]} {key[2]}"
        text = f"{' '.join(map(str, key))}: {one.get(key)} became {two.get(key)}"
        if request not in HELD_TIGHTER:
            unlisted.append(text)
        elif two.get(key) is None or two[key][0] not in (401, 403):
            not_refused.append(text)
        else:
            changed.add(request)
    return unlisted, not_refused, sorted(set(HELD_TIGHTER) - changed)


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def small_app_checks():
    """The check on an app of three unguarded routes, so that only the check stands between a caller and a route."""
    from flask import Flask, request
    from flask_login import LoginManager
    from api import access
    from model import permissions

    app = Flask("verify_access")
    app.config.update(SECRET_KEY="verify-access", JWT_TOKEN_NAME="jwt_python_flask", TESTING=True)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.user_loader(lambda user_id: None)  # nobody has a session here
    seen = {}

    @app.route("/")
    def home():
        seen["decision"] = getattr(request, "access_decision", None)
        return "home"

    @app.route("/api/id")  # signed_in in the file; here it has no guard of its own
    def unguarded():
        seen["decision"] = getattr(request, "access_decision", None)
        return "answered"

    @app.route("/api/microblog")  # signed_in in the file too; this one refuses by itself, as a guarded route does
    def refuses_by_itself():
        return {"message": "no"}, 401

    @app.route("/api/not-in-the-file", methods=["GET", "POST"])
    def not_listed():
        return "answered"

    access.init_app(app)
    log = Captured()
    app.logger.addHandler(log)
    app.logger.setLevel(logging.INFO)
    client = app.test_client()
    enforce_as_committed = permissions._enforce

    def call(path, enforce, method="GET"):
        permissions._enforce = frozenset(enforce)
        log.messages.clear()
        seen.clear()
        try:
            return client.open(path, method=method)
        finally:
            permissions._enforce = enforce_as_committed

    response = call("/", [])
    check("a public request is allowed and nothing is logged", response.status_code == 200 and seen["decision"]["allow"] is True and log.messages == [])

    response = call("/api/id", [])
    check("shadow mode: a request the file would refuse still reaches the route", response.status_code == 200 and seen["decision"]["allow"] is False
          and seen["decision"]["caller"] == "anonymous")
    check("shadow mode: and it is logged, with the request, the caller and no more",
          len(log.messages) == 1 and "would refuse GET /api/id for anonymous" in log.messages[0] and "answered 200" in log.messages[0], str(log.messages))

    response = call("/api/microblog", [])
    check("shadow mode: a request the route refuses by itself is not logged: the file and the route agree", response.status_code == 401 and log.messages == [], str(log.messages))

    response = call("/api/id", ["anonymous"])
    check("enforced: the same request is refused before the route, with the 401 the route's guard gives",
          response.status_code == 401 and response.get_json()["error"] == "Unauthorized" and seen == {} and log.messages == [])
    response = call("/api/id", ["Admin"])
    check("enforced for another caller only: an anonymous caller is still in shadow mode", response.status_code == 200 and len(log.messages) == 1)

    response = call("/api/not-in-the-file", [])
    check("a request in no list is logged in shadow mode", response.status_code == 200 and "not in permissions.json" in log.messages[0], str(log.messages))
    response = call("/api/not-in-the-file", ["anonymous"])
    check("enforced: a request in no list is refused, and the answer says why", response.status_code == 401 and "permissions.json" in response.get_json()["message"])
    check("every method of a route is its own request", call("/api/not-in-the-file", ["anonymous"], "POST").status_code == 401)

    response = call("/api/id", ["anonymous"], "OPTIONS")
    check("a CORS preflight is never refused: it carries no login", response.status_code == 200)
    response = call("/api/id", ["anonymous"], "HEAD")
    check("HEAD is held to what GET is: refused where GET is, answered where GET is", response.status_code == 401 and call("/", ["anonymous"], "HEAD").status_code == 200)
    response = call("/no/such/address", ["anonymous"])
    check("an address with no route is left to the 404", response.status_code == 404)

    def broken(*args):
        raise RuntimeError("a mistake in the check")
    real_decide, access.decide = access.decide, broken
    try:
        response = call("/api/id", [])
        check("shadow mode: an error in the check is logged and the request goes on", response.status_code == 200
              and any("the check failed" in m for m in log.messages), str(log.messages))
        app.config["PROPAGATE_EXCEPTIONS"] = False
        response = call("/api/id", ["anonymous"])
        check("enforced: an error in the check fails the request, it does not let it through", response.status_code == 500 and seen == {})
    finally:
        access.decide = real_decide

    def refused(change, words):
        with open(os.path.join(REPO, POLICY), encoding="utf-8") as fh:
            data = json.load(fh)
        change(data)
        try:
            permissions._check(data, {"Admin", "Teacher", "User", "Mentor", "Guest"})
        except ValueError as error:
            return words in str(error)
        return False
    check("the loader refuses an enforce list naming a caller that does not exist", refused(lambda d: d.update(enforce=["anonymous", "Principal"]), "enforce names Principal"))
    check("and one that is not a list, or names a caller twice", refused(lambda d: d.update(enforce="anonymous"), "enforce must be a list")
          and refused(lambda d: d.update(enforce=["Admin", "Admin"]), "enforce must be a list"))
    check("anonymous and the roles are accepted", not refused(lambda d: d.update(enforce=["anonymous", "Guest", "User", "Mentor", "Teacher", "Admin"]), ""))
    check("enforce is a key the file must have", refused(lambda d: d.pop("enforce"), "enforce is missing"))


def who_is_calling_checks():
    """identify(), which the check and every guard share, on this project's app with a throwaway database."""
    import jwt
    import __init__ as pkg
    from sqlalchemy import create_engine
    real_db = os.path.join(REPO, "instance", "volumes", "user_management.db")
    digest = lambda: hashlib.sha256(open(real_db, "rb").read()).hexdigest() if os.path.exists(real_db) else None
    real_before = digest()
    app, db = pkg.app, pkg.db
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='verify-access-'), 'new.db')}")
    import main  # noqa: F401  registers every model and blueprint
    from api import authorize
    from model.user import User

    app.config["TESTING"] = True
    password = "Aa1!" + secrets.token_urlsafe(16)
    with app.app_context():
        db.create_all()
        db.session.add(User(name="Verify Access", uid="zz_access", password=password, role="User"))
        db.session.commit()
        User.query.filter_by(_uid="zz_access").first().activate()

    def token(uid="zz_access", version=None, hours=1, key=None):
        with app.app_context():
            user = User.query.filter_by(_uid="zz_access").first()
            claims = {"_uid": uid, "token_version": user.token_version if version is None else version,
                      "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=hours)}
        return jwt.encode(claims, key or app.config["SECRET_KEY"], algorithm="HS256")

    def who_am_i(cookie=None, login=False):
        client = app.test_client()
        if login:
            client.post("/login", data={"username": "zz_access", "password": password})
        if cookie:
            client.set_cookie(app.config["JWT_TOKEN_NAME"], cookie)
        return client.get("/api/id").status_code

    good = token()
    check("a caller with a token and no session is who the token says", who_am_i(good) == 200)
    check("a caller with a session and no token is who the session says", who_am_i(login=True) == 200)
    check("no token and no session is nobody", who_am_i() == 401)
    check("a token that is not one, one signed with another key, one past its time and one for an account that does not exist are nobody",
          [who_am_i("not-a-token"), who_am_i(token(key="another-key")), who_am_i(token(hours=-1)), who_am_i(token(uid="zz_nobody"))] == [401, 401, 401, 401])

    calls = []
    real_identify = authorize._identify
    authorize._identify = lambda: calls.append(1) or real_identify()
    try:
        who_am_i(good)
    finally:
        authorize._identify = real_identify
    check("who is calling is worked out once for a request, though the check and the route's guard both ask", len(calls) == 1, f"{len(calls)} times")

    with app.app_context():
        user = User.query.filter_by(_uid="zz_access").first()
        user.token_version = (user.token_version or 0) + 1  # what a password change does
        db.session.commit()
    check("a token from before a password change is refused, and a new one is accepted", who_am_i(good) == 401 and who_am_i(token()) == 200)
    check("the project's own database was not touched", digest() == real_before)


def main():
    small_app_checks()
    who_is_calling_checks()
    modes = ["development", "production"]

    # ---- 2. this project in shadow mode
    inventory = rec.project_inventory()
    shadow = prober.probe_project(modes, overlay={POLICY: policy(enforce=[])})
    check(f"shadow mode: every one of {len(shadow)} calls left a decision, and none was enforced",
          len(shadow) > 4000 and all(r["decision"] is not None and not r["decision"].get("enforced") for r in shadow))
    problems = decision_problems(inventory, shadow)
    check("shadow mode: the decision made from the file is the one the route's guard makes, for every call", not problems, f"{len(problems)}: " + "; ".join(problems[:6]))
    would_refuse = [r for r in shadow if not r["decision"]["allow"] and rec.observed(r) != "refused" and f'{r["method"]} {r["rule"]}' not in HELD_TIGHTER]
    check("shadow mode: no call the file would refuse was answered by its route, so nothing is logged, except the requests in HELD_TIGHTER", not would_refuse,
          "; ".join(f'{r["method"]} {r["rule"]} {r["caller"]}' for r in would_refuse[:6]))

    # ---- 3. enforced for everyone: the gate
    enforced = prober.probe_project(modes, overlay={POLICY: policy(enforce=everyone())})
    unlisted, not_refused, unchanged = unexpected_changes(shadow, enforced)
    check(f"enforced for everyone: of {len(enforced)} answers, none is different from shadow mode except for the requests in HELD_TIGHTER",
          len(enforced) == len(shadow) and not unlisted, f"{len(unlisted)}: " + "; ".join(unlisted[:6]))
    check(f"enforced for everyone: and for those {len(HELD_TIGHTER)} requests the only change is a 401 or 403 from the check", not not_refused, "; ".join(not_refused[:6]))
    check("enforced for everyone: every request in HELD_TIGHTER is refused to someone it was not before, so the list is not out of date", not unchanged, "; ".join(unchanged))
    refused_here = [r for r in enforced if not r["decision"]["allow"]]
    check("enforced: every refusal is now made by the check, before the route", refused_here and all(r["decision"].get("enforced") for r in refused_here))

    committed = json.loads(policy())["enforce"]
    if committed and sorted(committed) != sorted(everyone()):
        as_committed = prober.probe_project(modes)
        unlisted, not_refused, _ = unexpected_changes(shadow, as_committed)
        check(f"enforced as the file is committed, for {', '.join(committed)}: of {len(as_committed)} answers, none is different from shadow mode except for the requests in HELD_TIGHTER, which are refused",
              len(as_committed) == len(shadow) and not unlisted and not not_refused, f"{len(unlisted) + len(not_refused)}: " + "; ".join((unlisted + not_refused)[:6]))
        refused_here = [r for r in as_committed if not r["decision"]["allow"]]
        kinds = {r["decision"]["caller"] for r in refused_here if r["decision"].get("enforced")}
        check("and the check refuses only the callers the file names", kinds == set(committed) & {r["decision"]["caller"] for r in refused_here}, str(sorted(kinds)))
    else:
        check("the file as committed is at one of the two ends already compared: " + ("shadow mode" if not committed else "enforced for everyone"), True)

    # ---- 4. what only enforcement gives
    marker = "/api/verify-access-unlisted"
    with open(os.path.join(REPO, "main.py"), encoding="utf-8") as fh:
        main_with_a_new_route = fh.read() + f"\n\n@app.route('{marker}')\ndef verify_access_unlisted():\n    return 'a new route with no guard and no entry'\n"

    def by_caller(overlay, only):
        return {r["caller"]: r for r in prober.probe_project(["production"], only=only, overlay=overlay)}

    new_route = by_caller({"main.py": main_with_a_new_route, POLICY: policy(enforce=everyone())}, marker)
    check("deny by default: a new route with no guard and no entry refuses every caller, 401 without an account and 403 with one",
          new_route["anonymous"]["status"] == 401 and new_route["invalid_token"]["status"] == 401
          and all(new_route[c]["status"] == 403 for c in ("guest", "user", "mentor", "teacher", "admin", "inactive_mentor")), str({c: r["status"] for c, r in new_route.items()}))
    new_route = by_caller({"main.py": main_with_a_new_route, POLICY: policy(enforce=[])}, marker)
    check("in shadow mode the same route answers, and the decision says it is in no list",
          new_route["anonymous"]["status"] == 200 and new_route["anonymous"]["decision"] == {"allow": False, "why": "not in permissions.json", "caller": "anonymous"})
    new_route = by_caller({"main.py": main_with_a_new_route, POLICY: policy(enforce=["anonymous"])}, marker)
    check("enforced for anonymous only: anonymous is refused, a signed-in caller is still in shadow mode",
          new_route["anonymous"]["status"] == 401 and new_route["user"]["status"] == 200 and new_route["admin"]["status"] == 200)

    def move(request, source, target):
        def change(data):
            (data["legacy_open"][0]["requests"] if source == "legacy_open" else data[source]).remove(request)
            (data["roles"][target[6:]] if target.startswith("roles.") else data[target]).append(request)
        return change

    # a request that is still in legacy_open: the NPC game's connection test, which answers whoever asks
    open_today, open_rule = "GET /api/ainpc/test", "/api/ainpc/test"
    def open_route_with(target):
        overlay = {POLICY: policy(move(open_today, "legacy_open", target), enforce=everyone())}
        return {r["caller"]: r["status"] for r in prober.probe_project(["production"], only=open_rule, overlay=overlay) if r["rule"] == open_rule and r["method"] == "GET"}

    in_shadow = [r for r in prober.probe_project(["production"], only=open_rule, overlay={POLICY: policy(move(open_today, "legacy_open", "signed_in"), enforce=[])})
                 if r["rule"] == open_rule and r["method"] == "GET"]
    # a guest is held to its own short list of requests whatever this file says, so it is left out
    check("in shadow mode a route the file would close still answers as before, and the check itself changes nothing: an expired account's call writes nothing",
          len(in_shadow) == len(prober.CALLERS) and all(r["status"] == 200 for r in in_shadow if r["caller"] != "guest") and not any(r["wrote"] for r in in_shadow)
          and not {r["caller"]: r for r in in_shadow}["expired_guest"]["decision"]["allow"], str({r["caller"]: (r["status"], r["wrote"]) for r in in_shadow}))
    open_answers = open_route_with("signed_in")
    check("an open route is closed by moving its entry to signed_in, with no change to the code: 401 without an account, 403 for an inactive one, answered for an active one",
          open_answers["anonymous"] == 401 and open_answers["invalid_token"] == 401 and open_answers["inactive_mentor"] == 403 and open_answers["expired_guest"] == 403
          and open_answers["user"] == 200 and open_answers["admin"] == 200, str(open_answers))
    staff = open_route_with("roles.Admin")
    check("and held to one role by moving it to that role's list", staff["user"] == 403 and staff["teacher"] == 403 and staff["admin"] == 200 and staff["anonymous"] == 401, str(staff))

    print(f"\n{len(_failures)} failed" if _failures else "\nall checks passed")
    sys.exit(1 if _failures else 0)


main()
