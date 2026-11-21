#!/usr/bin/env python3
"""Verify model/dataconfigs/permissions.json against the app: the check to run whenever a route is added or changed.

  1. The loader refuses unclear data (a request in two lists, an unknown role, a group with no owner).
  2. Every request the app answers is in the file, and the file lists no request the app does not answer.
  3. The file says the same as the guard written on each route, in development and in production. Once the file is
     enforced for every caller it may hold a route tighter than its guard does; it may never be looser.
  4. Every request the file does not list as open refuses a caller with no account, when really called.
  5. No legacy_open group is past its review date.

A new route with no entry fails 2. A route listed as guarded that answers an anonymous caller fails 4.

Usage:  python scripts/verify/verify_permissions.py        (from the flask repo root; about a minute)
Exit:   0 all checks passed, 1 otherwise.
"""
import copy, datetime, json, os, sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts", "rbac"))
os.environ.setdefault("SECRET_KEY", "verify-permissions")

import rbac_prober as prober
import rbac_reconcile as rec
from model import permissions

# Callers with no account they can use. An account switched off while it still holds a session is not here: the
# session login does not ask, which the comparison of the evidence reports.
NO_USABLE_ACCOUNT = ["anonymous", "invalid_token", "expired_guest"]
MODES = {"development": False, "production": True}
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


# ---------------------------------------------------------------- the rules, each usable on its own

def coverage_problems(app_requests, listed):
    """Requests the app answers that the file does not list, and listed requests the app does not answer."""
    return [f"{r} is not in permissions.json" for r in sorted(set(app_requests) - set(listed))] + \
           [f"{r} is in permissions.json and the app has no such route" for r in sorted(set(listed) - set(app_requests))]


def no_looser(data, code):
    """Whether everyone the file lets through, the guard in the code lets through too."""
    if data == code:
        return True
    if isinstance(data, list):  # some roles: tighter than any login, than no guard, and than a longer list of roles
        return code in ("anyone", "signed_in") or (isinstance(code, list) and set(data) <= set(code))
    return data == "signed_in" and code == "anyone"


def mirror_problems(reconciled, who_may_call, may_be_tighter=False):
    """Routes where the file and the guard in the code say different things, in either mode.

    While some callers are still in shadow mode the two must say the same. Once the file is enforced for everyone
    (may_be_tighter) it may hold a route tighter than its guard, because the check before the route refuses first.
    It may never be looser: the guard would still refuse, and the file would be wrong about the route.
    """
    problems = []
    for record in reconciled:
        for mode, production in MODES.items():
            code, data = rec.who_may_call(record, mode), who_may_call(record["method"], record["rule"], production)
            if code != data and not (may_be_tighter and no_looser(data, code)):
                problems.append(f'{record["method"]} {record["rule"]} in {mode}: the code lets {code} through, the file says {data}')
    return problems


def tighter_in_the_file(reconciled, who_may_call):
    """Requests the file holds tighter than the guard in the code does, in production."""
    held = []
    for record in reconciled:
        code, data = rec.who_may_call(record, "production"), who_may_call(record["method"], record["rule"], True)
        if code != data and no_looser(data, code):
            held.append(f'{record["method"]} {record["rule"]}')
    return held


def anonymous_problems(probes, who_may_call):
    """Calls by a caller with no account that got past a request the file does not list as open."""
    problems = []
    for probe in probes:
        who = who_may_call(probe["method"], probe["rule"], MODES[probe["mode"]])
        if who != "anyone" and rec.observed(probe) != "refused":
            problems.append(f'{probe["method"]} {probe["rule"]} in {probe["mode"]}: answered {probe["caller"]} with {probe["status"]}, and the file says {who}')
    return problems


def overdue_groups(groups, today):
    return [f'{g["owner"]}, due {g["review_by"]}, {len(g["requests"])} requests' for g in groups if datetime.date.fromisoformat(g["review_by"]) < today]


def main():
    path = os.path.join(REPO, "model", "dataconfigs", "permissions.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    roles = {"Admin", "Teacher", "User", "Mentor", "Guest"}

    # ---- 1. the loader
    def refused(change, words):
        broken = copy.deepcopy(data)
        change(broken)
        try:
            permissions._check(broken, roles)
        except ValueError as error:
            return words in str(error)
        return False

    try:
        permissions._check(copy.deepcopy(data), roles)
        check("the file as it is passes the loader's checks", True)
    except ValueError as error:
        check("the file as it is passes the loader's checks", False, str(error))
    guarded, public = data["signed_in"][0], data["public"][0]
    check("a missing list is refused", refused(lambda d: d.pop("session"), "session is missing"))
    check("a list the file does not have is refused, so a misspelt name is not ignored", refused(lambda d: d.update(pubilc=[]), "pubilc is not a list"))
    for bad in ("GET api/id", "FETCH /api/id", "/api/id", 5):
        check(f"an entry that is not 'METHOD /route' is refused: {bad!r}", refused(lambda d: d["signed_in"].append(bad), "must be a list of 'METHOD /route'"))
    check("the same request twice in one list is refused", refused(lambda d: d["signed_in"].append(guarded), "twice"))
    check("a request in two lists is refused: it would be both guarded and open", refused(lambda d: d["public"].append(guarded), "it can be in only one"))
    check("a request in a role's list and in signed_in is refused", refused(lambda d: d["roles"]["Admin"].append(guarded), "it can be in only one"))
    check("a request in legacy_open and in public is refused", refused(lambda d: d["legacy_open"][0]["requests"].append(public), "it can be in only one"))
    check("a request may be in several roles' lists", not refused(lambda d: d["roles"].setdefault("Mentor", []).append(d["roles"]["Admin"][0]), ""))
    check("a role that is not in account_types.json is refused", refused(lambda d: d["roles"].update(Principal=[]), "Principal, which is not a role"))
    check("a request open in development must say who may make it in production", refused(lambda d: d["open_in_development"].append(public), "open in development"))
    check("a legacy_open group needs an owner", refused(lambda d: d["legacy_open"][0].pop("owner"), "needs an owner")
          and refused(lambda d: d["legacy_open"][0].update(owner="  "), "needs an owner"))
    check("a legacy_open group needs a review date that is a date", refused(lambda d: d["legacy_open"][0].update(review_by="soon"), "review_by date")
          and refused(lambda d: d["legacy_open"][0].pop("review_by"), "review_by date"))
    check("a legacy_open group with no requests is refused", refused(lambda d: d["legacy_open"][0].update(requests=[]), "lists no requests"))
    check("roles must be lists by role, and legacy_open a list of groups", refused(lambda d: d.update(roles=[]), "roles must give")
          and refused(lambda d: d.update(legacy_open={}), "list of groups"))

    # ---- who may call, as the loader answers it
    method_route = lambda request: request.split(" ", 1)
    check("a public request is open to anyone", permissions.who_may_call(*method_route(public)) == "anyone")
    check("a legacy_open request is open to anyone too", permissions.who_may_call(*method_route(data["legacy_open"][0]["requests"][0])) == "anyone")
    check("a signed_in request is for any active account", permissions.who_may_call(*method_route(guarded)) == "signed_in")
    check("a session page and a device request are their own kinds", permissions.who_may_call(*method_route(data["session"][0])) == "session"
          and permissions.who_may_call(*method_route(data["device_key"][0])) == "device_key")
    staff_only = next(r for r in data["roles"]["Teacher"] if r in data["roles"]["Admin"])
    admin_only = next(r for r in data["roles"]["Admin"] if r not in data["roles"]["Teacher"])
    check("a request in two roles' lists is for both, one in a single list for that role alone",
          permissions.who_may_call(*method_route(staff_only)) == ["Admin", "Teacher"] and permissions.who_may_call(*method_route(admin_only)) == ["Admin"])
    development_open = data["open_in_development"][0]
    check("a request open in development is open there and guarded in production",
          permissions.who_may_call(*method_route(development_open), production=False) == "anyone"
          and permissions.who_may_call(*method_route(development_open), production=True) != "anyone")
    check("a request in no list is nobody's", permissions.who_may_call("GET", "/api/not-a-route") is None)
    check("nothing is public_restricted until it has been reviewed for it", data["public_restricted"] == [])

    # ---- the rules of this script, on small cases
    check("coverage: a route with no entry and an entry with no route are both reported",
          coverage_problems(["GET /a", "GET /new"], ["GET /a", "GET /gone"]) == ["GET /new is not in permissions.json", "GET /gone is in permissions.json and the app has no such route"]
          and coverage_problems(["GET /a"], ["GET /a"]) == [])
    one = rec.reconcile([{"method": "GET", "rule": "/a", "decorators": [{"name": "token_required", "args": ""}],
                          "guard": {"kind": "login", "roles": None, "unknown": [], "runtime_roles": None}}], [], [])
    check("mirror: a file that agrees with the guard gives no problem", mirror_problems(one, lambda m, r, production: "signed_in") == [])
    check("mirror: a file that calls a guarded route open is reported, in both modes", len(mirror_problems(one, lambda m, r, production: "anyone")) == 2)
    check("mirror: a file that differs in one mode only is reported for that mode",
          mirror_problems(one, lambda m, r, production: "signed_in" if production else "anyone") == ["GET /a in development: the code lets signed_in through, the file says anyone"])
    roles_route = rec.reconcile([{"method": "GET", "rule": "/a", "decorators": [{"name": "token_required", "args": ""}],
                                  "guard": {"kind": "roles", "roles": None, "unknown": [], "runtime_roles": ["Admin", "Teacher"]}}], [], [])
    open_route = rec.reconcile([{"method": "GET", "rule": "/a", "decorators": [], "guard": {"kind": "none", "roles": None, "unknown": [], "runtime_roles": None}}], [], [])
    says = lambda who: (lambda m, r, production: who)
    check("mirror: while anyone is in shadow mode, a file tighter than the guard is reported", len(mirror_problems(open_route, says("signed_in"))) == 2)
    check("mirror: enforced for everyone, a file tighter than the guard is accepted: an open route held to a login, a login held to a role, a role list made shorter",
          mirror_problems(open_route, says("signed_in"), True) == [] and mirror_problems(open_route, says(["Admin"]), True) == []
          and mirror_problems(one, says(["Admin"]), True) == [] and mirror_problems(roles_route, says(["Admin"]), True) == [])
    check("mirror: a file looser than the guard is reported even then: the guard would still refuse",
          len(mirror_problems(one, says("anyone"), True)) == 2 and len(mirror_problems(roles_route, says("signed_in"), True)) == 2
          and len(mirror_problems(roles_route, says(["Admin", "Mentor"]), True)) == 2 and len(mirror_problems(roles_route, says("anyone"), True)) == 2)
    session_route = rec.reconcile([{"method": "GET", "rule": "/a", "decorators": [{"name": "login_required", "args": ""}],
                                    "guard": {"kind": "login", "roles": None, "unknown": [], "runtime_roles": None}}], [], [])
    check("mirror: a session page and a device route are never said to be something else", len(mirror_problems(session_route, says("signed_in"), True)) == 2
          and len(mirror_problems(session_route, says(["Admin"]), True)) == 2
          and len(mirror_problems(open_route, says("device_key"), True)) == 2 and len(mirror_problems(open_route, says(None), True)) == 2)
    check("the requests the file holds tighter than the code are listed, and one it holds looser is not among them",
          tighter_in_the_file(open_route, says("signed_in")) == ["GET /a"] and tighter_in_the_file(one, says("signed_in")) == [] and tighter_in_the_file(one, says("anyone")) == [])
    probe = lambda status, caller="anonymous", redirect=None: {"method": "GET", "rule": "/a", "mode": "production", "caller": caller, "status": status, "redirect": redirect}
    guarded_in_file = lambda m, r, production: "signed_in"
    check("anonymous: a guarded request that refuses, or sends to the login page, is fine",
          anonymous_problems([probe(401), probe(403, "expired_guest"), probe(302, redirect="/login?next=/a")], guarded_in_file) == [])
    check("anonymous: a guarded request that answers a caller with no account is reported", len(anonymous_problems([probe(200)], guarded_in_file)) == 1
          and len(anonymous_problems([probe(404, "invalid_token")], guarded_in_file)) == 1)
    check("anonymous: an error is not a refusal", len(anonymous_problems([probe(500)], guarded_in_file)) == 1)
    check("anonymous: a request in no list must refuse too", len(anonymous_problems([probe(200)], lambda m, r, production: None)) == 1)
    check("anonymous: an open request may answer anyone", anonymous_problems([probe(200)], lambda m, r, production: "anyone") == [])
    groups = [{"owner": "a", "review_by": "2027-01-31", "requests": ["GET /x"]}]
    check("a group is overdue the day after its review date, not on it", overdue_groups(groups, datetime.date(2027, 2, 1)) != [] and overdue_groups(groups, datetime.date(2027, 1, 31)) == [])

    # ---- 2 and 3. this project: every route is listed, and the file says what the guards say
    inventory = rec.project_inventory()
    app_requests = [f'{r["method"]} {r["rule"]}' for r in inventory]
    problems = coverage_problems(app_requests, permissions.every_request())
    check(f"every one of the app's {len(app_requests)} requests is in the file, and the file lists nothing else", not problems, "; ".join(problems[:8]))
    from model import account_types
    callers = ["anonymous"] + sorted(set(account_types.assignable_roles()) | set(account_types.guest_roles()))
    for_everyone = all(permissions.enforced(caller) for caller in callers)
    reconciled = rec.reconcile(inventory, [], [])
    problems = mirror_problems(reconciled, permissions.who_may_call, may_be_tighter=for_everyone)
    check("the file says the same as the guard on every route, in development and in production"
          + (", or holds the route tighter" if for_everyone else " (it may not be tighter while some callers are in shadow mode)"), not problems, "; ".join(problems[:8]))
    if for_everyone:
        print(f"      {len(tighter_in_the_file(reconciled, permissions.who_may_call))} requests are held tighter by the file than by the guard in the code")

    # ---- 4. really call every route with no account
    probes = prober.probe_project(list(MODES), callers=NO_USABLE_ACCOUNT)
    problems = anonymous_problems(probes, permissions.who_may_call)
    called = {(p["method"], p["rule"]) for p in probes}
    check(f"every request not listed as open refuses a caller with no account: {len(probes)} calls, as {', '.join(NO_USABLE_ACCOUNT)}, in both modes",
          not problems and len(called) >= len(app_requests) - 1 and {p["mode"] for p in probes} == set(MODES), "; ".join(problems[:8]))

    # ---- 5. the review dates
    overdue = overdue_groups(permissions.legacy_open_groups(), datetime.date.today())
    check("no legacy_open group is past its review date", not overdue, "; ".join(overdue))

    readme = open(os.path.join(REPO, "model", "dataconfigs", "README.md"), encoding="utf-8").read()
    for word in ("permissions.json", "legacy_open", "public_restricted", "verify_permissions.py"):
        check(f"the README explains {word}", word in readme)

    print(f"\n{len(_failures)} failed" if _failures else "\nall checks passed")
    sys.exit(1 if _failures else 0)


main()
