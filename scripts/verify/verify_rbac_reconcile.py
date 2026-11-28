#!/usr/bin/env python3
"""Verify rbac_reconcile.py: that it reads the guard of a route in each mode, knows which callers that guard lets
through, and names every way the answers can differ from it.

The rules are checked on small records written here, one case each. Then the tools are run on this project and the
result is compared with scripts/rbac/rbac_sample_labels.py, routes a person labelled by reading the source.

Usage:  python scripts/verify/verify_rbac_reconcile.py        (from the flask repo root; about two minutes)
Exit:   0 all checks passed, 1 otherwise.
"""
import json, os, sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "scripts", "rbac"))
os.environ.setdefault("SECRET_KEY", "verify-rbac-reconcile")

import rbac_reconcile as rec

_failures = []
CALLERS = ["anonymous", "invalid_token", "guest", "user", "mentor", "teacher", "admin", "inactive_mentor", "expired_guest"]
ROLES = {"guest": "Guest", "user": "User", "mentor": "Mentor", "teacher": "Teacher", "admin": "Admin", "inactive_mentor": "Mentor", "expired_guest": "Guest"}


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        _failures.append(label)


def route(kind, decorators=(), roles=None, production_roles=None, rule="/r", method="GET"):
    """An inventory record. production_roles: the role list held only in production."""
    return {"method": method, "rule": rule, "file": "x.py", "function": "f", "decorators": [{"name": d, "args": ""} for d in decorators],
            "guard": {"kind": kind, "roles": None, "unknown": [], "runtime_roles": roles},
            "modes": {"development": {"wrapper_depth": 0, "runtime_roles": roles},
                      "production": {"wrapper_depth": 1, "runtime_roles": production_roles if production_roles is not None else roles}}}


def calls(statuses, rule="/r", method="GET", mode="production", redirect=None, canary=(), wrote=()):
    """Probe records for one route: statuses is {caller: status}, or one status for everybody."""
    if not isinstance(statuses, dict):
        statuses = {c: statuses for c in CALLERS}
    return [{"method": method, "rule": rule, "mode": mode, "caller": c, "role": ROLES.get(c), "status": s,
             "redirect": redirect if isinstance(s, int) and 300 <= s < 400 else None,
             "canary": ["x"] if c in canary else [], "wrote": c in wrote} for c, s in statuses.items()]


def by_caller(statuses):
    return {p["caller"]: p for p in calls(statuses)}


def login_answers(**changed):
    """What a route behind a login answers when all is as the guard says."""
    out = {"anonymous": 401, "invalid_token": 401, "guest": 200, "user": 200, "mentor": 200, "teacher": 200, "admin": 200,
           "inactive_mentor": 403, "expired_guest": 403}
    out.update(changed)
    return out


def main():
    # ---- the guard of a route in each mode
    g = rec.guard_in_mode
    check("no guard, and a decorator nobody knows, are read as open", g(route("none"), "production")["kind"] == "open" and g(route("unknown"), "production")["kind"] == "open")
    check("a token login is a login", g(route("login", ["token_required"]), "production") == {"kind": "login", "roles": None})
    check("the Flask session login is told apart from the token login", g(route("login", ["login_required"]), "production")["kind"] == "session_login")
    check("a role list is the one the running app holds, sorted", g(route("roles", ["token_required"], roles=["Teacher", "Admin"]), "production") == {"kind": "roles", "roles": ["Admin", "Teacher"]})
    check("a device key guard is its own kind", g(route("device_key", ["require_device_api_key"]), "development")["kind"] == "device_key")
    production_only = route("login_in_production_only", ["require_auth_if_production"])
    check("a login only in production is open in development", g(production_only, "development")["kind"] == "open" and g(production_only, "production")["kind"] == "login")
    admin_in_production = route("admin_in_production_only", ["require_admin_if_production"], production_roles=["Teacher", "Admin"])
    check("a role list only in production is open in development and that list in production",
          g(admin_in_production, "development")["kind"] == "open" and g(admin_in_production, "production") == {"kind": "roles", "roles": ["Admin", "Teacher"]})

    # ---- who each guard lets through
    def through(guard):
        return [c for c in CALLERS if rec.expected(guard, c, ROLES.get(c))]
    check("an open route lets every caller through", through({"kind": "open", "roles": None}) == CALLERS)
    check("a login lets every active account through and nobody else",
          through({"kind": "login", "roles": None}) == ["guest", "user", "mentor", "teacher", "admin"])
    check("a role list lets only those roles through", through({"kind": "roles", "roles": ["Admin", "Teacher"]}) == ["teacher", "admin"])
    check("a role list does not let an inactive account of that role through", through({"kind": "roles", "roles": ["Mentor"]}) == ["mentor"])
    check("the session login lets an inactive account through, because it does not ask, and not an expired one",
          through({"kind": "session_login", "roles": None}) == ["guest", "user", "mentor", "teacher", "admin", "inactive_mentor"])
    check("a device key guard lets no caller of the probe through", through({"kind": "device_key", "roles": None}) == [])

    # ---- what an answer counts as
    seen = lambda status, redirect=None: rec.observed({"status": status, "redirect": redirect})
    check("401 and 403 are refusals", seen(401) == seen(403) == "refused")
    check("being sent to the login page is a refusal, another redirect is an answer", seen(302, "/login?next=/x") == "refused" and seen(302, "/home") == "answered")
    check("404, 400 and 200 are answers: the caller got past the guard", seen(404) == seen(400) == seen(200) == "answered")
    check("a 500 is an error and a timeout is a timeout, neither is a refusal", seen(500) == "error" and seen("timeout") == "timeout" and seen("error") == "error")

    # ---- the findings
    login, open_guard, admin_only = {"kind": "login", "roles": None}, {"kind": "open", "roles": None}, {"kind": "roles", "roles": ["Admin"]}
    found = lambda guard, statuses: rec.compare(guard, by_caller(statuses), limited_roles=["Guest"])[0]
    check("answers that match the guard give no finding", found(login, login_answers()) == [] and found(open_guard, 200) == []
          and found(admin_only, login_answers(guest=403, user=403, mentor=403, teacher=403)) == [])
    check("an anonymous caller answered by a guarded route is wider than the guard", found(login, login_answers(anonymous=200)) == ["wider_than_guard"])
    check("so is an invalid token", found(login, login_answers(invalid_token=200)) == ["wider_than_guard"])
    check("so is an inactive account, and an expired one", found(login, login_answers(inactive_mentor=200)) == ["wider_than_guard"]
          and found(login, login_answers(expired_guest=200)) == ["wider_than_guard"])
    check("so is a role that is not on the list", found(admin_only, login_answers(guest=403, user=403, mentor=403, teacher=200)) == ["wider_than_guard"])
    check("an error from inside the route for a caller who should have been refused is wider too: they got in",
          found(login, login_answers(anonymous=500)) == ["wider_than_guard"])
    check("a caller the guard lets through and the function refuses is a check in the code", found(login, login_answers(user=403)) == ["narrower_in_code"])
    check("the same for an account type held to a list of requests is that limit, not a check in the code", found(login, login_answers(guest=403)) == ["account_type_limit"])
    check("an open route that fails for anonymous callers and answers signed-in ones is closed by an error",
          "error_for_anonymous" in found(open_guard, {**{c: 200 for c in CALLERS}, "anonymous": 500, "invalid_token": 500}))
    check("a route where nobody gets an answer says nothing about access", found(open_guard, 500) == ["error_for_everyone", "no_answer_seen"])
    check("a route that fails for everyone is not called closed by an error", "error_for_anonymous" not in found(open_guard, 500))
    check("no 2xx for anyone is noted, and the refusals still count", found(login, login_answers(guest=404, user=404, mentor=404, teacher=404, admin=404)) == ["no_answer_seen"]
          and found(login, login_answers(anonymous=404, guest=404, user=404, mentor=404, teacher=404, admin=404)) == ["no_answer_seen", "wider_than_guard"])
    check("a call that did not finish is a timeout", "timeout" in found(open_guard, {**{c: 200 for c in CALLERS}, "user": "timeout"}))
    check("a redirect to the login page counts as the guard refusing",
          rec.compare({"kind": "session_login", "roles": None}, {p["caller"]: p for p in calls(
              {"anonymous": 302, "invalid_token": 302, "guest": 200, "user": 200, "mentor": 200, "teacher": 200, "admin": 200, "inactive_mentor": 200, "expired_guest": 302},
              redirect="/login?next=/r")})[0] == [])

    # ---- joining the two reports
    inventory = [route("login", ["token_required"], rule="/a"), production_only | {"rule": "/b"}, route("none", rule="/static/<path:filename>"),
                 route("unknown", ["some_guard"], rule="/c")]
    probes = (calls(login_answers(), rule="/a") + calls(login_answers(), rule="/a", mode="development")
              + calls(login_answers(), rule="/b") + calls(200, rule="/b", mode="development", canary=("anonymous", "admin"), wrote=("anonymous",))
              + calls(200, rule="/c") + calls(200, rule="/c", mode="development"))
    records = {r["rule"]: r for r in rec.reconcile(inventory, probes, ["Guest"])}
    check("every route of the inventory has a record, in both modes", set(records) == {"/a", "/b", "/c", "/static/<path:filename>"}
          and all(set(r["modes"]) == {"development", "production"} for r in records.values()))
    check("a route with matching answers in both modes has no findings", records["/a"]["findings"] == [])
    check("a route guarded in production only is marked as differing by mode, each mode with its own guard",
          records["/b"]["findings"] == ["differs_by_mode"] and records["/b"]["modes"]["development"]["guard"]["kind"] == "open"
          and records["/b"]["modes"]["production"]["guard"]["kind"] == "login")
    check("who got canary data and whose call wrote are kept per mode",
          records["/b"]["modes"]["development"]["leaked_to"] == ["admin", "anonymous"] and records["/b"]["modes"]["development"]["wrote_for"] == ["anonymous"]
          and records["/b"]["modes"]["production"]["leaked_to"] == [])
    check("a route the prober did not call is kept and marked, not dropped", records["/static/<path:filename>"]["probed"] is False)
    check("a decorator nobody knows is a finding", "unknown_guard" in records["/c"]["findings"])
    check("who may call, in a person's words", [rec.who_may_call(records[r]) for r in ("/a", "/b", "/c")] == ["signed_in", "signed_in", "anyone"]
          and rec.who_may_call(records["/b"], "development") == "anyone")

    # ---- the hand-labelled sample
    listed = list(records.values())
    good = [{"request": "GET /a", "who": "signed_in"}, {"request": "GET /b", "who": "signed_in", "development": "anyone"}, {"request": "GET /c", "who": "anyone"}]
    check("labels that match give no problem", rec.check_sample(listed, good) == [])
    wrong = rec.check_sample(listed, [{"request": "GET /a", "who": "anyone"}])
    check("a label that differs is reported, with the mode", len(wrong) == 2 and "in production: labelled anyone, read as signed_in" in wrong[1])
    check("a label that forgets the development difference is reported", rec.check_sample(listed, [{"request": "GET /b", "who": "signed_in"}])
          == ["GET /b in development: labelled signed_in, read as anyone"])
    check("a labelled route that does not exist is reported", rec.check_sample(listed, [{"request": "GET /gone", "who": "anyone"}]) == ["GET /gone: no such route"])
    role_route = rec.reconcile([route("roles", ["token_required"], roles=["Teacher", "Admin"], rule="/d")], [], [])
    check("a role list label matches in any order", rec.check_sample(role_route, [{"request": "GET /d", "who": ["Teacher", "Admin"]}]) == [])

    # ---- a first policy file from the guards
    draft = rec.draft_policy(listed + role_route, "someone")
    check("a draft policy lists each route under who its guard lets through in production",
          draft["signed_in"] == ["GET /a", "GET /b"] and draft["roles"] == {"Admin": ["GET /d"], "Teacher": ["GET /d"]}, str(draft))
    check("a route with no guard goes to legacy_open with the owner, never to public: only a person can say it is open on purpose",
          draft["public"] == [] and draft["legacy_open"] == [{"owner": "someone", "requests": ["GET /c", "GET /static/<path:filename>"]}])
    check("a route open only in development is marked as that, and still listed for production", draft["open_in_development"] == ["GET /b"])
    check("a draft with no open route has no empty legacy_open group", rec.draft_policy(role_route, "someone")["legacy_open"] == [])

    text = json.dumps(rec.summary(listed))
    check("the summary counts and names no route", "/a" not in text and "/static" not in text and rec.summary(listed)["pairs"] == 4 and rec.summary(listed)["not_probed"] == 1)
    check("roles held to a list of requests come from the account types data", rec.limited_roles_of() == ["Guest"], str(rec.limited_roles_of()))

    # ---- this project
    inventory, probes = rec.gather()
    project = rec.reconcile(inventory, probes, rec.limited_roles_of())
    from rbac_sample_labels import ROUTES as sample
    problems = rec.check_sample(project, sample)
    check("the project is reconciled: every route has a record and all but Flask's static route were called",
          len(project) > 200 and [r["rule"] for r in project if not r["probed"]] == ["/static/<path:filename>"], f"{len(project)} records")
    check(f"the tools read the same guard as a person did on all {len(sample)} hand-labelled routes", not problems and len(sample) >= 40, "; ".join(problems))
    check("no route of the project has a decorator the tools do not know", not any("unknown_guard" in r["findings"] for r in project))

    print(f"\n{len(_failures)} failed" if _failures else "\nall checks passed")
    sys.exit(1 if _failures else 0)


main()
