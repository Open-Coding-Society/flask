#!/usr/bin/env python3
"""Verify rbac_prober.py: that it records what each kind of caller really gets, and that running it is safe.

Probes scripts/rbac_probe_fixture.py, a small app with one route for each case that matters, and checks every
record against what that route does. Also checks the safety rules (clean environment, network off, fresh data for
every call, inert code payload, nothing written to the repo) and that the project's own app can be probed.

Usage:  python scripts/verify_rbac_prober.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import os, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.environ.setdefault("SECRET_KEY", "verify-rbac-prober")

import rbac_prober as prober
from rbac_probe_fixture import (CANARY_EMAIL, CANARY_NAME, NETWORK_REACHED, VICTIM_UID, FixtureState, make_probe_app)

_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        _failures.append(label)


def table(records):
    return {(r["method"], r["rule"], r["caller"]): r for r in records}


def statuses(got, method, rule):
    return {c: got[(method, rule, c)]["status"] for c in prober.CALLERS}


def main():
    prober.block_network()  # as the child does, before any route runs
    workdir = tempfile.mkdtemp(prefix="rbac-prober-fixture-")
    state = FixtureState(workdir)
    app = make_probe_app(state)
    callers = state.callers()

    records = prober.run_probe(app, callers, state, timeout=1)
    got = table(records)
    check("every route is probed as every kind of caller", len(records) == len({(r["method"], r["rule"]) for r in records}) * len(prober.CALLERS)
          and set(prober.CALLERS) == {r["caller"] for r in records})

    # ---- what each caller gets
    check("an open route answers everyone", set(statuses(got, "GET", "/open").values()) == {200})
    needs = statuses(got, "GET", "/needs-login")
    check("a route that needs a login: 401 without one or with a bad one, 200 with one, 403 for an inactive or expired account",
          needs["anonymous"] == needs["invalid_token"] == 401 and all(needs[c] == 200 for c in ("guest", "user", "mentor", "teacher", "admin"))
          and needs["inactive_mentor"] == needs["expired_guest"] == 403, str(needs))
    admin = statuses(got, "POST", "/admin-only")
    check("an admin-only route: 401 for anonymous, 403 for every other role, 200 for the admin",
          admin["anonymous"] == 401 and admin["invalid_token"] == 401 and all(admin[c] == 403 for c in ("guest", "user", "mentor", "teacher", "inactive_mentor", "expired_guest"))
          and admin["admin"] == 200, str(admin))
    check("only the one call that wrote is marked as writing", [c for c in prober.CALLERS if got[("POST", "/admin-only", c)]["wrote"]] == ["admin"])
    check("a route that crashes is a 500 for everyone, not a refusal", set(statuses(got, "GET", "/crash").values()) == {500})
    check("a route that hangs is a timeout, and the run goes on", set(statuses(got, "GET", "/hang").values()) == {"timeout"})
    check("a route nobody guards but that needs no login is told apart from one that crashes", statuses(got, "GET", "/open")["anonymous"] == 200)

    # ---- leaks
    leak_any = got[("GET", "/leaks-to-anyone", "anonymous")]
    check("canary data in a body is noticed, even for an anonymous caller", leak_any["canary"] == [CANARY_NAME], str(leak_any["canary"]))
    staff = {c: got[("GET", "/leaks-to-staff", c)]["canary"] for c in prober.CALLERS}
    check("a leak shows only for the callers who got it", staff["teacher"] == staff["admin"] == [CANARY_EMAIL]
          and all(staff[c] == [] for c in ("anonymous", "invalid_token", "guest", "user", "mentor", "inactive_mentor", "expired_guest")), str(staff))
    check("canary data in a response header is noticed too", got[("GET", "/leaks-in-a-header", "user")]["canary"] == [CANARY_EMAIL])

    # ---- writes
    check("a GET that writes is marked as writing", all(got[("GET", "/writes-on-get", c)]["wrote"] for c in prober.CALLERS))
    check("a write to a file, not the database, is noticed", all(got[("POST", "/writes-a-file", c)]["wrote"] for c in prober.CALLERS))
    check("a route that writes nothing is not marked", not any(got[("GET", "/open", c)]["wrote"] for c in prober.CALLERS))

    # ---- fresh data for every call
    prober.run_probe(app, callers, state, only="/counter")
    import sqlite3
    check("every call gets fresh data: nine callers each incremented a counter and the last copy shows one, not nine",
          sqlite3.connect(state.work).execute("SELECT n FROM counter").fetchone()[0] == 1)
    check("so each mutating call sees the same answer", all(got[("POST", "/counter", c)]["status"] == 200 for c in prober.CALLERS))

    # ---- files a route writes, on the real app's side
    folder = tempfile.mkdtemp(prefix="rbac-prober-files-")
    os.makedirs(os.path.join(folder, "data"))
    votes = os.path.join(folder, "data", "votes.json")
    open(votes, "w").write('{"haha": 8}')
    open(os.path.join(folder, "app.db"), "w").write("database")
    files = prober.FileSnapshot(folder)
    clean = files.digest()
    open(votes, "w").write('{"haha": 9}')  # the same size as before
    check("a write that leaves a file the same size still shows", files.digest() != clean)
    open(os.path.join(folder, "data", "upload.bin"), "w").write("new")
    files.restore()
    check("the next call starts from the files as they were: the change is undone and the new file is gone",
          open(votes).read() == '{"haha": 8}' and not os.path.exists(os.path.join(folder, "data", "upload.bin")) and files.digest() == files.clean)
    before_database_write = files.digest()
    open(os.path.join(folder, "app.db"), "w").write("database changed")
    check("the database file is left to the database copy, not counted here", files.digest() == before_database_write)

    # ---- what is sent
    prober.run_probe(app, {"anonymous": callers["anonymous"]}, state, only="/run")
    check("a route that runs code is sent print(1), never anything else", state.received("received_code.txt") == "print(1)", str(state.received("received_code.txt")))
    echo = got[("POST", "/echo", "user")]
    check("the fields a route reads are the ones sent: json keys and query keys, not headers", echo["payload_keys"] == ["email", "name", "password", "q"], str(echo["payload_keys"]))
    prober.run_probe(app, {"anonymous": callers["anonymous"]}, state, only="/echo")
    received = state.received("received_echo.json")
    check("each field gets a value that fits its name", "'name': 'probe'" in received and "'email': 'probe@example.invalid'" in received
          and "'password': 'Aa1!probe-password'" in received and "'q': 'probe'" in received, received)
    prober.run_probe(app, {"anonymous": callers["anonymous"]}, state, only="/uid-field")
    check("a body field called uid is the victim's uid", state.received("received_uidfield.txt") == VICTIM_UID, str(state.received("received_uidfield.txt")))
    prober.run_probe(app, {"anonymous": callers["anonymous"]}, state, only="/item/")
    check("an int route parameter is 1", state.received("received_item.txt") == "1")
    prober.run_probe(app, {"anonymous": callers["anonymous"]}, state, only="/user/")
    check("a uid route parameter is the victim's uid", state.received("received_uid.txt") == VICTIM_UID)
    check("url building: ints, uids, paths and anything else",
          prober.build_url("/api/x/<int:id>/y/<string:uid>/z/<path:p>/<other>", "victim") == "/api/x/1/y/victim/z/probe/path/probe"
          and prober.build_url("/api/classroom/<id>", "victim") == "/api/classroom/1" and prober.build_url("/u/<username>", "victim") == "/u/victim")

    # ---- safety
    check("the network is off: nothing is reached, so no route can spend a provider's credit",
          all(got[("GET", "/net", c)]["canary"] == [] for c in prober.CALLERS) and NETWORK_REACHED not in str(records))
    for attempt in (lambda: __import__("socket").create_connection(("127.0.0.1", 9)), lambda: __import__("socket").getaddrinfo("example.com", 80),
                    lambda: __import__("socket").socket().connect(("127.0.0.1", 9))):
        try:
            attempt()
            check("a connection attempt is refused", False)
        except prober.NetworkOff:
            pass
    check("a connection attempt is refused", True)

    secrets_in_parent = dict(GROQ_API_KEY="k1", GEMINI_API_KEY="k2", GITHUB_TOKEN="k3", DB_PASSWORD="k4", DB_ENDPOINT="db.invalid", AUTOMATOR_API_KEY="k5",
                             AWS_SECRET_ACCESS_KEY="k6", GOOGLE_CLIENT_ID="k7", RFID_API_KEY="k8", SECRET_KEY="the-real-key", DEFAULT_PASSWORD="the-real-password")
    saved = dict(os.environ)
    os.environ.update(secrets_in_parent)
    try:
        env = prober.child_env("production")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    leaked = [k for k in secrets_in_parent if k in env and k not in ("SECRET_KEY", "DEFAULT_PASSWORD", "RFID_API_KEY")]
    check("the app process gets no provider key, database login or other secret from the parent", not leaked, str(leaked))
    check("it gets its own throwaway secret key and password, not the parent's", env["SECRET_KEY"] != "the-real-key" and env["SECRET_KEY"].startswith("rbac-prober-")
          and env["DEFAULT_PASSWORD"] != "the-real-password")
    check("a device-key guard has a throwaway key to compare with, not the parent's", env["RFID_API_KEY"].startswith("rbac-prober-") and env["RFID_API_KEY"] != "k8")
    check("it does get what it needs to run, and the mode", "PATH" in env and env["IS_PRODUCTION"] == "true" and prober.child_env("development")["IS_PRODUCTION"] == "false")
    check("nothing else is in it", set(env) <= set(prober.ENV_KEEP) | {"SECRET_KEY", "IS_PRODUCTION", "DEFAULT_PASSWORD", "RESET_TOKEN_SECRET", "RFID_API_KEY"}, str(sorted(env)))

    # ---- the project's own app, and that the repo is left alone
    def tree_state():
        done = subprocess.run(["git", "status", "--porcelain", "--ignored"], cwd=REPO, capture_output=True, text=True)
        return done.stdout if done.returncode == 0 else None
    before = tree_state()
    project = prober.probe_project(["development"], only="/api/id")
    after = tree_state()
    check("the project's own app can be probed, as every kind of caller", {r["caller"] for r in project} == set(prober.CALLERS) and len(project) % len(prober.CALLERS) == 0
          and all(r["mode"] == "development" for r in project), f"{len(project)} records")
    anonymous = [r for r in project if r["caller"] == "anonymous" and r["rule"] == "/api/id"]
    check("on the project, an anonymous caller is refused by /api/id", anonymous and all(r["status"] == 401 for r in anonymous))
    check("probing the project writes nothing into the repo", before is None or before == after, "git status changed" if before != after else "")
    check("a summary names no route", "/api" not in str(prober.summary(project)))

    print(f"\n{len(_failures)} failed" if _failures else "\nall checks passed")
    sys.exit(1 if _failures else 0)


main()
