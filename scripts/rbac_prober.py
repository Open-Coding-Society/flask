#!/usr/bin/env python3
"""rbac_prober.py
Call every route of the Flask app as each kind of caller and record what it really answers.

This is RBAC phase 0, evidence C: what the app does, which the source (rbac_inventory.py, evidence A and B) can only
suggest. It is built so that running it can do no harm:

  * It runs on a copy of the tracked files in a temporary folder, so nothing a route writes reaches your repo.
  * The app runs in a child process with a clean environment: no provider key, no database login, no .env.
  * Outbound network access is off in that process.
  * Every call gets its own fresh copy of a throwaway SQLite database seeded with canary records.
  * A route that runs code is sent print(1), never anything generated.
  * Response bodies are never kept, only whether a canary was in them.

Callers: anonymous, invalid_token, guest, user, mentor, teacher, admin, inactive_mentor, expired_guest.

  python scripts/rbac_prober.py                     # development mode; counts only
  python scripts/rbac_prober.py --both-modes --out report.json    # also IS_PRODUCTION=true, full record

The full record shows which routes are open, so write it outside the repo and do not commit or post it.
"""
import argparse
import ast
import collections
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SCRIPTS)

CALLERS = ["anonymous", "invalid_token", "guest", "user", "mentor", "teacher", "admin", "inactive_mentor", "expired_guest"]
# The only variables the app process is given from the one this runs in. Everything else, above all every provider
# key, database login and .env value, is left out.
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHASHSEED")
SAFE_CODE = "print(1)"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
TIMEOUT_SECONDS = 8
NO_LOOKUP_RECEIVERS = {"headers", "cookies", "environ", "config", "environment"}


# ---------------------------------------------------------------- safety

def clean_env(parent_env, extra):
    """The environment the app process gets: a short list from the parent, plus what the prober sets itself."""
    env = {k: v for k, v in parent_env.items() if k in ENV_KEEP}
    env.update(extra)
    return env


class NetworkOff(OSError):
    pass


def block_network():
    """Make every attempt to open a connection or look up a name fail, in this process."""
    def refuse(*args, **kwargs):
        raise NetworkOff("rbac_prober: network is off")
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    socket.gethostbyname = refuse


class ProbeTimeout(BaseException):
    """Not an Exception, so a route's own error handling, and Flask's, cannot catch it and call a hang a crash."""


def _alarm(signum, frame):
    raise ProbeTimeout()


# ---------------------------------------------------------------- what to send

def _receiver_name(node):
    """The last name in a receiver such as request.args or request.headers, or None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


class _Fields(ast.NodeVisitor):
    def __init__(self):
        self.json = []
        self.query = []

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            receiver = _receiver_name(func.value)
            key = node.args[0].value
            if receiver in ("args", "form"):
                self.query.append(key)
            elif receiver not in NO_LOOKUP_RECEIVERS:
                self.json.append(key)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            receiver = _receiver_name(node.value)
            if receiver in ("args", "form"):
                self.query.append(key.value)
            elif receiver not in NO_LOOKUP_RECEIVERS:
                self.json.append(key.value)
        self.generic_visit(node)


def fields_read(handler):
    """(json field names, query field names) the handler, and the helpers it calls, read. Noise is harmless."""
    import rbac_inventory as inv
    json_keys, query_keys, seen = [], [], set()

    def walk(func, depth):
        path, node = inv._find_def(func)
        if node is None:
            return
        visitor = _Fields()
        for statement in node.body:
            visitor.visit(statement)
        json_keys.extend(visitor.json)
        query_keys.extend(visitor.query)
        if depth:
            calls = inv._read_body(node).calls
            for name in calls:
                helper = inspect.unwrap(func).__globals__.get(name)
                if name not in seen and inspect.isfunction(helper) and helper is not inspect.unwrap(func):
                    seen.add(name)
                    walk(helper, depth - 1)

    walk(handler, 1)
    return list(dict.fromkeys(json_keys)), list(dict.fromkeys(query_keys))


def sample_value(key, victim_uid):
    lowered = key.lower()
    if lowered in ("code", "source", "script", "program"):
        return SAFE_CODE
    if "password" in lowered:
        return "Aa1!probe-password"
    if "email" in lowered:
        return "probe@example.invalid"
    if lowered in ("uid", "username", "user", "login"):
        return victim_uid
    if lowered.endswith("id") or lowered in ("page", "limit", "offset", "count", "year", "n"):
        return 1
    if lowered in ("role",):
        return "User"
    return "probe"


_CONVERTER = re.compile(r"<(?:(?P<conv>\w+)(?:\([^)]*\))?:)?(?P<name>\w+)>")


def build_url(rule, victim_uid):
    """A request path for a Flask route template: ids are 1, uids are the victim's, anything else is 'probe'."""
    def fill(match):
        conv, name = match.group("conv"), match.group("name")
        if conv in ("int", "float"):
            return "1"
        if conv == "path":
            return "probe/path"
        lowered = name.lower()
        if "uid" in lowered or lowered in ("username", "user"):
            return victim_uid
        return "1" if lowered.endswith("id") else "probe"
    return _CONVERTER.sub(fill, rule)


# ---------------------------------------------------------------- the probe

def _location_path(location):
    return None if not location else re.sub(r"^https?://[^/]+", "", location)


def run_probe(app, callers, state, only=None, timeout=TIMEOUT_SECONDS, progress=None):
    """One record per route, method and caller.

    callers  {name: setup(client)}, each giving a fresh test client that caller's credentials
    state    .victim_uid, .canaries (strings that must never come back to a caller), .fresh() makes a new copy of the
             data before every call, .fingerprint() says what the data and files look like now
    only     a substring of the route to limit the run to
    """
    import rbac_inventory as inv
    records = []
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False  # an error in a route answers 500, as it does in production
    previous = signal.signal(signal.SIGALRM, _alarm)
    try:
        rules = sorted(app.url_map.iter_rules(), key=lambda r: r.rule)
        for rule in rules:
            if rule.endpoint == "static" or (only and only not in rule.rule):
                continue
            url = build_url(rule.rule, state.victim_uid)
            for method, handler, view_class in inv._handlers(app, rule):
                json_keys, query_keys = fields_read(handler)
                body = {k: sample_value(k, state.victim_uid) for k in json_keys}
                query = {k: sample_value(k, state.victim_uid) for k in query_keys}
                for caller, setup in callers.items():
                    state.fresh()
                    client = app.test_client()
                    setup(client)
                    before = state.fingerprint()
                    record = {"method": method, "rule": rule.rule, "caller": caller, "payload_keys": sorted(body) + sorted(query)}
                    signal.setitimer(signal.ITIMER_REAL, timeout)
                    try:
                        kwargs = {"method": method, "query_string": query}
                        if method in MUTATING:
                            kwargs["json"] = body
                        response = client.open(url, **kwargs)
                        text = response.get_data(as_text=True)
                        headers = "\n".join(f"{k}: {v}" for k, v in response.headers)
                        record["status"] = response.status_code
                        record["redirect"] = _location_path(response.headers.get("Location"))
                        record["canary"] = sorted(c for c in state.canaries if c in text or c in headers)
                    except ProbeTimeout:
                        record.update(status="timeout", redirect=None, canary=[])
                    except Exception as error:  # the test client itself failing, not the route
                        record.update(status="error", redirect=None, canary=[], error=type(error).__name__)
                    finally:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    record["wrote"] = state.fingerprint() != before
                    records.append(record)
                if progress:
                    progress(len(records))
    finally:
        signal.signal(signal.SIGALRM, previous)
    return records


def summary(records):
    out = {"calls": len(records), "by_caller": {}}
    for caller in CALLERS:
        mine = [r for r in records if r["caller"] == caller]
        if not mine:
            continue
        kinds = collections.Counter()
        for r in mine:
            s = r["status"]
            kinds["2xx" if isinstance(s, int) and 200 <= s < 300 else "3xx" if isinstance(s, int) and 300 <= s < 400
                  else "401" if s == 401 else "403" if s == 403 else "other 4xx" if isinstance(s, int) and 400 <= s < 500
                  else "5xx" if isinstance(s, int) and s >= 500 else str(s)] += 1
        out["by_caller"][caller] = {**dict(sorted(kinds.items())), "leaked_canary": sum(1 for r in mine if r["canary"]),
                                    "wrote": sum(1 for r in mine if r["wrote"])}
    return out


# ---------------------------------------------------------------- the real app

class FileSnapshot:
    """The files under a folder, databases apart, remembered so that a write to one shows and can be undone.

    A route can keep its data in a file (a vote count, an upload). A change that leaves the size the same, 9 becoming
    8, still has to show, so small files are compared by content and every file by size and time of writing.
    """
    SMALL = 64 * 1024

    def __init__(self, folder):
        self.folder = folder
        self.kept = {}
        for path in self._paths():
            with open(path, "rb") as fh:
                self.kept[path] = fh.read()
        self.clean = self.digest()

    def _paths(self):
        found = []
        for folder, _, files in os.walk(self.folder):
            found += [os.path.join(folder, name) for name in files if not name.endswith((".db", ".db-journal", ".db-wal", ".db-shm"))]
        return sorted(found)

    def digest(self):
        digest = hashlib.sha256()
        for path in self._paths():
            try:
                info = os.stat(path)
                digest.update(f"{path}:{info.st_size}:{info.st_mtime_ns}".encode())
                if info.st_size <= self.SMALL:
                    with open(path, "rb") as fh:
                        digest.update(fh.read())
            except OSError:
                digest.update(f"{path}:gone".encode())
        return digest.hexdigest()

    def restore(self):
        """Put every file back as it was when this was made, and remove the ones that were not there."""
        if self.digest() == self.clean:
            return
        for path in self._paths():
            if path not in self.kept:
                os.remove(path)
        for path, content in self.kept.items():
            current = None
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    current = fh.read()
            if current != content:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(content)
        self.clean = self.digest()


class RealState:
    """The project's own database, as a seeded template that every call gets a fresh copy of."""

    def __init__(self, app, db, workdir):
        self.app, self.db, self.workdir = app, db, workdir
        self.template = os.path.join(workdir, "template.db")
        self.work = os.path.join(workdir, "work.db")
        self.instance = app.instance_path
        self.victim_uid = None
        self.canaries = []

    def _point_at(self, path):
        from sqlalchemy import create_engine
        with self.app.app_context():
            self.db.session.remove()
            old = self.db.engines.get(None) if hasattr(self.db.engines, "get") else None
            self.db.engines[None] = create_engine(f"sqlite:///{path}")
            if old is not None:
                old.dispose()

    def seed(self):
        """Build the template: every table, one account per kind of caller, and a victim whose data is canary."""
        import jwt
        from datetime import datetime, timedelta
        from model.user import User
        from model import account_types
        marker = secrets.token_hex(4)
        self.victim_uid = f"canary_uid_{marker}"
        self.canaries = [f"CANARY_NAME_{marker}", f"canary_{marker}@victim.example", f"CANARY_SID_{marker}"]
        self._point_at(self.template)
        types = {t: account_types.get(t) for t in ("student", "mentor", "guest")}
        guest_role = (types["guest"] or {}).get("role", "Guest")
        who = {"guest": guest_role, "user": (types["student"] or {}).get("role", "User"), "mentor": (types["mentor"] or {}).get("role", "Mentor"),
               "teacher": "Teacher", "admin": account_types.admin_role(), "inactive_mentor": (types["mentor"] or {}).get("role", "Mentor"),
               "expired_guest": guest_role}
        password = "Aa1!" + secrets.token_urlsafe(12)
        tokens, ids = {}, {}
        with self.app.app_context():
            self.db.create_all()
            victim = User(name=self.canaries[0], uid=self.victim_uid, password=password, role="User", sid=self.canaries[2])
            victim._email = self.canaries[1]
            self.db.session.add(victim)
            for caller, role in who.items():
                user = User(name=f"prober {caller}", uid=f"prober_{caller}", password=password, role=role)
                self.db.session.add(user)
            self.db.session.commit()
            for caller in who:
                user = User.query.filter_by(_uid=f"prober_{caller}").first()
                if caller != "inactive_mentor":
                    user.activate()
                if caller == "expired_guest":
                    user.last_verified = datetime.utcnow() - timedelta(hours=(account_types.lifetime_hours(user.role) or 1) + 5)
                ids[caller] = user.id
                tokens[caller] = jwt.encode({"_uid": user._uid, "token_version": user.token_version,
                                             "exp": datetime.utcnow() + timedelta(hours=2)},
                                            self.app.config["SECRET_KEY"], algorithm="HS256")
            User.query.filter_by(_uid=self.victim_uid).first().activate()
            self.db.session.commit()
            self.db.session.remove()
        shutil.copy(self.template, self.work)
        self._point_at(self.work)
        self.tokens, self.user_ids = tokens, ids
        self.files = FileSnapshot(self.instance)

    def fresh(self):
        shutil.copy(self.template, self.work)
        self._point_at(self.work)
        self.files.restore()

    def fingerprint(self):
        digest = hashlib.sha256()
        with open(self.work, "rb") as fh:
            digest.update(fh.read())
        digest.update(self.files.digest().encode())
        return digest.hexdigest()

    def callers(self):
        app = self.app
        jwt_name = app.config.get("JWT_TOKEN_NAME", "jwt_python_flask")

        def authenticated(caller):
            def setup(client):
                client.set_cookie(jwt_name, self.tokens[caller])
                with client.session_transaction() as session:
                    session["_user_id"] = str(self.user_ids[caller])
                    session["_fresh"] = True
            return setup

        def anonymous(client):
            return None

        def invalid_token(client):
            client.set_cookie(jwt_name, "not-a-real-token")
            client.environ_base["HTTP_AUTHORIZATION"] = "Bearer not-a-real-token"

        out = {"anonymous": anonymous, "invalid_token": invalid_token}
        for caller in CALLERS[2:]:
            out[caller] = authenticated(caller)
        return out


# ---------------------------------------------------------------- running it

def _child(mode, out_path, only):
    sys.path.insert(0, SCRIPTS)
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    block_network()
    import __init__ as package
    import main  # noqa: F401  registers every model and blueprint
    app, db = package.app, package.db
    workdir = tempfile.mkdtemp(prefix="rbac-prober-db-")
    state = RealState(app, db, workdir)
    state.seed()
    records = run_probe(app, state.callers(), state, only=only)
    for r in records:
        r["mode"] = mode
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh)


def _copy_tracked_files(destination):
    """The tracked files of this repo, in a folder of their own. Falls back to a plain copy without git."""
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout.decode().split("\0")
        names = [n for n in listing if n and os.path.isfile(os.path.join(REPO, n))]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for folder, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "venv", ".venv", "__pycache__", "node_modules", "volumes")]
            names += [os.path.relpath(os.path.join(folder, f), REPO) for f in files]
    for name in names:
        target = os.path.join(destination, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(REPO, name), target)


def child_env(mode):
    """What the app process is started with: a short list from this process, plus values of the prober's own."""
    return clean_env(os.environ, {"SECRET_KEY": "rbac-prober-" + secrets.token_hex(8), "IS_PRODUCTION": "true" if mode == "production" else "false",
                                  "DEFAULT_PASSWORD": "Aa1!" + secrets.token_urlsafe(8), "RESET_TOKEN_SECRET": secrets.token_hex(8),
                                  # so a guard that needs the device key answers 401 to a caller without it, as it does with a key set
                                  "RFID_API_KEY": "rbac-prober-" + secrets.token_hex(8)})


def probe_project(modes, only=None):
    """Probe a copy of this repo in each mode, each in a child process with a clean environment."""
    records = []
    sandbox = tempfile.mkdtemp(prefix="rbac-prober-repo-")
    try:
        _copy_tracked_files(sandbox)
        for mode in modes:
            out = os.path.join(sandbox, f"probe-{mode}.json")
            env = child_env(mode)
            command = [sys.executable, os.path.join(sandbox, "scripts", "rbac_prober.py"), "--child", mode, "--child-out", out]
            if only:
                command += ["--only", only]
            done = subprocess.run(command, cwd=sandbox, env=env, capture_output=True, text=True)
            if done.returncode != 0 or not os.path.exists(out):
                sys.exit(f"{mode} run failed:\n{done.stderr[-1500:]}")
            with open(out, encoding="utf-8") as fh:
                records += json.load(fh)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return records


def main_cli():
    parser = argparse.ArgumentParser(description="Call every route as each kind of caller and record what it answers.")
    parser.add_argument("--out", help="write the full records here (JSON), outside the repo")
    parser.add_argument("--both-modes", action="store_true", help="also run with IS_PRODUCTION=true")
    parser.add_argument("--only", help="only routes containing this text")
    parser.add_argument("--child", choices=["development", "production"], help=argparse.SUPPRESS)
    parser.add_argument("--child-out", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        _child(args.child, args.child_out, args.only)
        return
    records = probe_project(["development", "production"] if args.both_modes else ["development"], args.only)
    for mode in sorted({r["mode"] for r in records}):
        print(f"== {mode}")
        print(json.dumps(summary([r for r in records if r["mode"] == mode]), indent=1))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(sorted(records, key=lambda r: (r["mode"], r["rule"], r["method"], CALLERS.index(r["caller"]))), fh, indent=1)
        print(f"wrote {len(records)} records to {args.out}")


if __name__ == "__main__":
    main_cli()
