#!/usr/bin/env python3
"""rbac_reconcile.py
Put the evidence about each route side by side, and say where the guards written on it do not explain what it answers.

This is RBAC phase 0, the comparison. rbac_inventory.py says what guards each route (evidence A and B: the
decorators, and the role list the running app holds). rbac_prober.py says what each kind of caller really gets
(evidence C). Here the two are joined, for development and for production mode:

  guard     what the decorators add up to: open, login, session_login, roles or device_key
  expected  for each caller, whether that guard lets them through
  observed  what the app answered: refused (401, 403, or sent to the login page), answered, error, timeout
  findings  where the two differ, in the words listed at FINDINGS below

  python scripts/rbac/rbac_reconcile.py                       # runs both tools on this repo; counts only
  python scripts/rbac/rbac_reconcile.py --out report.json     # the full comparison, one record per method and route
  python scripts/rbac/rbac_reconcile.py --inventory a.json --probe c.json    # reports made earlier
  python scripts/rbac/rbac_reconcile.py --sample              # also check against scripts/rbac/rbac_sample_labels.py
  python scripts/rbac/rbac_reconcile.py --draft-policy permissions.json --owner NAME
                                                         # a first model/dataconfigs/permissions.json to finish by hand

The full record shows which routes are open, so write it outside the repo and do not commit or post it.
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))  # this folder, scripts/rbac: the tools import each other from here
REPO = os.path.dirname(os.path.dirname(SCRIPTS))
MODES = ("development", "production")

# Callers with no account, and callers whose account must not be let in whatever its role
NO_ACCOUNT = ("anonymous", "invalid_token")
INACTIVE = ("inactive_mentor",)
EXPIRED = ("expired_guest",)
SESSION_DECORATORS = {"login_required"}

FINDINGS = """
wider_than_guard      a caller the guard should refuse got an answer or an error from inside the route. The guard is
                      not doing what the source says: every one of these needs a person.
narrower_in_code      a caller the guard lets through was refused anyway: a check inside the function. Expected for
                      many routes; these are the checks a policy file will take over later.
account_type_limit    the same, for a caller whose account type is held to a short list of requests (a guest).
error_for_anonymous   an open route answers a signed-in caller and fails for an anonymous one: it reads the caller
                      without a guard, so it is closed by an error and not by a refusal.
error_for_everyone    nobody got an answer, only errors: the route needs data or a provider the probe does not have,
                      or is broken. Says nothing about who may call it.
no_answer_seen        nobody got a 2xx, so what an allowed caller is given was not seen. The refusals still count.
timeout               a call did not finish.
unknown_guard         a decorator the inventory does not know is on the route.
differs_by_mode       guarded differently in development and in production.
"""


def guard_in_mode(record, mode):
    """{kind, roles} for one inventory record in one mode. kind: open, login, session_login, roles, device_key."""
    kind = record["guard"]["kind"]
    in_mode = (record.get("modes") or {}).get(mode) or {}
    roles = in_mode.get("runtime_roles", record["guard"].get("runtime_roles"))
    if kind == "login_in_production_only":
        kind = "login" if mode == "production" else "none"
    elif kind == "admin_in_production_only":
        kind = "roles" if mode == "production" else "none"
    if kind in ("none", "unknown"):
        return {"kind": "open", "roles": None}
    if kind == "device_key":
        return {"kind": "device_key", "roles": None}
    if kind == "login" and any(d["name"] in SESSION_DECORATORS for d in record["decorators"]) \
            and not any(d["name"] in ("token_required", "auth_required") for d in record["decorators"]):
        return {"kind": "session_login", "roles": None}
    if roles:
        return {"kind": "roles", "roles": sorted(roles)}
    return {"kind": "login", "roles": None}


def expected(guard, caller, role):
    """True when this guard lets this caller through to the route's own code."""
    kind = guard["kind"]
    if kind == "open":
        return True
    if kind == "device_key":
        return False  # the probe sends no device key
    if caller in NO_ACCOUNT or caller in EXPIRED:
        return False
    if kind == "session_login":
        return True  # the session login does not ask whether the account is active
    if caller in INACTIVE:
        return False
    if kind == "roles":
        return role in guard["roles"]
    return True


def observed(probe):
    """refused, answered, error or timeout for one probe record."""
    status = probe["status"]
    if status == "timeout":
        return "timeout"
    if not isinstance(status, int):
        return "error"
    if status in (401, 403):
        return "refused"
    if 300 <= status < 400 and "/login" in (probe.get("redirect") or ""):
        return "refused"
    return "error" if status >= 500 else "answered"


def compare(guard, probes, limited_roles=()):
    """The findings for one route in one mode. probes: {caller: probe record}."""
    findings = set()
    seen = {caller: observed(p) for caller, p in probes.items()}
    for caller, probe in probes.items():
        through, got = expected(guard, caller, probe.get("role")), seen[caller]
        if got == "timeout":
            findings.add("timeout")
        elif not through and got != "refused":
            findings.add("wider_than_guard")
        elif through and got == "refused":
            findings.add("account_type_limit" if probe.get("role") in limited_roles else "narrower_in_code")
    answers = [c for c, got in seen.items() if got == "answered"]
    errors = [c for c, got in seen.items() if got == "error"]
    if errors and not answers:
        findings.add("error_for_everyone")
    if guard["kind"] == "open" and any(c in errors for c in NO_ACCOUNT) and any(c not in NO_ACCOUNT for c in answers):
        findings.add("error_for_anonymous")
    if not any(isinstance(p["status"], int) and 200 <= p["status"] < 300 for p in probes.values()):
        findings.add("no_answer_seen")
    return sorted(findings), seen


def reconcile(inventory, probes, limited_roles=()):
    """One record per method and route: the guard, what each caller got and the findings, in each mode."""
    by_route = collections.defaultdict(lambda: collections.defaultdict(dict))
    for probe in probes:
        by_route[(probe["method"], probe["rule"])][probe["mode"]][probe["caller"]] = probe
    records = []
    for record in sorted(inventory, key=lambda r: (r["rule"], r["method"])):
        key = (record["method"], record["rule"])
        out = {"method": record["method"], "rule": record["rule"], "file": record.get("file"), "function": record.get("function"),
               "modes": {}, "findings": []}
        findings = set()
        if record["guard"]["kind"] == "unknown":
            findings.add("unknown_guard")
        for mode in MODES:
            guard = guard_in_mode(record, mode)
            calls = by_route.get(key, {}).get(mode)
            if not calls:
                out["modes"][mode] = {"guard": guard, "probed": False}
                continue
            found, seen = compare(guard, calls, limited_roles)
            findings.update(found)
            out["modes"][mode] = {"guard": guard, "probed": True, "findings": found, "observed": seen,
                                  "status": {c: p["status"] for c, p in calls.items()},
                                  "leaked_to": sorted(c for c, p in calls.items() if p.get("canary")),
                                  "wrote_for": sorted(c for c, p in calls.items() if p.get("wrote"))}
        if out["modes"]["development"]["guard"] != out["modes"]["production"]["guard"]:
            findings.add("differs_by_mode")
        out["findings"] = sorted(findings)
        out["probed"] = any(m["probed"] for m in out["modes"].values())
        records.append(out)
    return records


def who_may_call(record, mode="production"):
    """The guard of one reconciled record in the words a person labels it with:
    anyone, signed_in, session, device_key, or a sorted list of roles."""
    guard = record["modes"][mode]["guard"]
    return {"open": "anyone", "login": "signed_in", "session_login": "session", "device_key": "device_key"}.get(guard["kind"], guard["roles"])


def check_sample(records, sample):
    """Mismatches between the reconciled guards and routes a person labelled by reading the source.

    sample: [{"request": "GET /api/id", "who": "signed_in", "development": "anyone"}]; development is given only
    where it differs from production.
    """
    by_request = {f'{r["method"]} {r["rule"]}': r for r in records}
    problems = []
    for entry in sample:
        record = by_request.get(entry["request"])
        if record is None:
            problems.append(f'{entry["request"]}: no such route')
            continue
        for mode in MODES:
            want = entry.get("development", entry["who"]) if mode == "development" else entry["who"]
            want = sorted(want) if isinstance(want, list) else want
            got = who_may_call(record, mode)
            if got != want:
                problems.append(f'{entry["request"]} in {mode}: labelled {want}, read as {got}')
    return problems


def draft_policy(records, owner):
    """A first permissions.json from the guards as they are today, for a person to finish.

    Every route says who its guard lets through in production. A route with no guard goes under legacy_open,
    because the tools cannot tell an open route that is meant to be open from one nobody guarded: a person moves
    the ones that are open on purpose to public.
    """
    policy = {"enforce": [], "public": [], "public_restricted": [], "legacy_open": [], "open_in_development": [], "device_key": [],
              "session": [], "signed_in": [], "roles": {}}
    unsorted = []
    for record in records:
        request = f'{record["method"]} {record["rule"]}'
        who = who_may_call(record, "production")
        if who == "anyone":
            unsorted.append(request)
        elif isinstance(who, list):
            for role in who:
                policy["roles"].setdefault(role, []).append(request)
        else:
            policy[who].append(request)
        if who != "anyone" and who_may_call(record, "development") == "anyone":
            policy["open_in_development"].append(request)
    if unsorted:
        policy["legacy_open"].append({"owner": owner, "requests": unsorted})
    policy["roles"] = dict(sorted(policy["roles"].items()))
    return policy


def summary(records):
    """Counts only: nothing here names a route."""
    out = {"pairs": len(records), "not_probed": sum(1 for r in records if not r["probed"]), "by_mode": {}}
    for mode in MODES:
        guards = collections.Counter(r["modes"][mode]["guard"]["kind"] for r in records)
        findings = collections.Counter(f for r in records for f in r["modes"][mode].get("findings", []))
        probed = [r["modes"][mode] for r in records if r["modes"][mode]["probed"]]
        out["by_mode"][mode] = {"guards": dict(sorted(guards.items())), "findings": dict(sorted(findings.items())),
                                "routes_that_leaked_a_canary_to_a_caller_with_no_account": sum(1 for m in probed if set(m["leaked_to"]) & set(NO_ACCOUNT)),
                                "routes_that_wrote_for_a_caller_with_no_account": sum(1 for m in probed if set(m["wrote_for"]) & set(NO_ACCOUNT))}
    out["differs_by_mode"] = sum(1 for r in records if "differs_by_mode" in r["findings"])
    return out


# ---------------------------------------------------------------- running it

def limited_roles_of(repo=REPO):
    """Roles whose account type is held to a list of requests, read from the account types data."""
    path = os.path.join(repo, "model", "dataconfigs", "account_types.json")
    with open(path, encoding="utf-8") as fh:
        types = json.load(fh).get("account_types", {})
    return sorted(t["role"] for t in types.values() if t.get("allowed_requests") is not None)


def _run_tool(script, options, repo):
    """Run one of the tools on the repo and return the records it writes with --out."""
    workdir = tempfile.mkdtemp(prefix="rbac-reconcile-")
    out = os.path.join(workdir, "records.json")
    try:
        env = dict(os.environ)
        env.setdefault("SECRET_KEY", "rbac-reconcile")
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + options + ["--out", out], cwd=repo, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            sys.exit(f"{script} failed:\n{done.stderr[-1500:]}")
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        if os.path.exists(out):
            os.remove(out)
        os.rmdir(workdir)


def project_inventory(repo=REPO):
    """The inventory of this repo, in both modes."""
    return _run_tool("rbac_inventory.py", ["--both-modes"], repo)


def gather(repo=REPO):
    """Run the inventory and the prober on this repo and return (inventory records, probe records)."""
    return project_inventory(repo), _run_tool("rbac_prober.py", ["--both-modes"], repo)


def main_cli():
    parser = argparse.ArgumentParser(description="Compare what guards each route with what it really answers.")
    parser.add_argument("--inventory", help="a report from rbac_inventory.py --both-modes --out (default: run it now)")
    parser.add_argument("--probe", help="a report from rbac_prober.py --both-modes --out (default: run it now)")
    parser.add_argument("--out", help="write the full comparison here (JSON), outside the repo")
    parser.add_argument("--draft-policy", metavar="FILE", help="write a first permissions.json from today's guards, for a person to finish")
    parser.add_argument("--owner", default="", help="with --draft-policy: who owns the review of the routes that are open today")
    parser.add_argument("--sample", action="store_true", help="check the reading against the routes labelled by hand in rbac_sample_labels.py")
    args = parser.parse_args()
    if bool(args.inventory) != bool(args.probe):
        parser.error("give both --inventory and --probe, or neither")
    if args.inventory:
        with open(args.inventory, encoding="utf-8") as fh:
            inventory = json.load(fh)
        with open(args.probe, encoding="utf-8") as fh:
            probes = json.load(fh)
    else:
        inventory, probes = gather()
    records = reconcile(inventory, probes, limited_roles_of())
    print(json.dumps(summary(records), indent=1))
    failed = False
    if args.sample:
        sys.path.insert(0, SCRIPTS)
        from rbac_sample_labels import ROUTES as sample
        problems = check_sample(records, sample)
        print(f"sample: {len(sample)} labelled routes, {len(problems)} read differently")
        for problem in problems:
            print("  " + problem)
        failed = bool(problems)
    if args.draft_policy:
        if not args.owner:
            parser.error("--draft-policy needs --owner")
        with open(args.draft_policy, "w", encoding="utf-8") as fh:
            json.dump(draft_policy(records, args.owner), fh, indent=2)
            fh.write("\n")
        print(f"wrote a draft policy to {args.draft_policy}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=1, sort_keys=True)
        print(f"wrote {len(records)} records to {args.out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main_cli()
