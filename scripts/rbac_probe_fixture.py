"""A small Flask app, and a place to keep its data, for scripts/verify_rbac_prober.py to probe.

Who is calling comes from a cookie called fixture_role, so the prober's callers need no login. The routes are the
cases the prober has to get right: open, refused to anonymous callers, refused by role, leaking, writing, crashing,
trying the network, hanging, and reading fields from the request.
"""
import os
import shutil
import socket
import sqlite3
import time

from flask import Flask, jsonify, request

# What stands in for the real accounts: the cookie value each of the prober's callers sends
ROLE_COOKIE = {"guest": "Guest", "user": "User", "mentor": "Mentor", "teacher": "Teacher", "admin": "Admin",
               "inactive_mentor": "InactiveMentor", "expired_guest": "ExpiredGuest"}
CANARY_NAME = "CANARY_NAME_fixture"
CANARY_EMAIL = "canary_fixture@victim.example"
NETWORK_REACHED = "NETWORK_REACHED_MARKER"
VICTIM_UID = "victim_uid"


class FixtureState:
    """What the prober needs of the data: a fresh copy before every call, and a fingerprint of it."""
    victim_uid = VICTIM_UID
    canaries = [CANARY_NAME, CANARY_EMAIL, NETWORK_REACHED]

    def __init__(self, workdir):
        self.workdir = workdir
        self.template = os.path.join(workdir, "template.db")
        self.work = os.path.join(workdir, "work.db")
        self.files = os.path.join(workdir, "files")
        os.makedirs(self.files, exist_ok=True)
        connection = sqlite3.connect(self.template)
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
        connection.execute("CREATE TABLE counter (n INTEGER)")
        connection.execute("INSERT INTO counter VALUES (0)")
        connection.execute("INSERT INTO notes (text) VALUES (?)", (CANARY_NAME,))
        connection.commit()
        connection.close()
        self.fresh()

    def fresh(self):
        shutil.copy(self.template, self.work)
        for name in os.listdir(self.files):
            os.remove(os.path.join(self.files, name))

    def fingerprint(self):
        import hashlib
        digest = hashlib.sha256(open(self.work, "rb").read())
        for name in sorted(os.listdir(self.files)):
            digest.update(name.encode() + open(os.path.join(self.files, name), "rb").read())
        return digest.hexdigest()

    def received(self, name):
        path = os.path.join(self.files, name)
        return open(path).read() if os.path.exists(path) else None

    def callers(self):
        def with_role(value):
            return lambda client: client.set_cookie("fixture_role", value) if value else None
        out = {"anonymous": with_role(None), "invalid_token": with_role("invalid")}
        out.update({caller: with_role(role) for caller, role in ROLE_COOKIE.items()})
        return out


def make_probe_app(state):
    app = Flask(__name__)

    def role():
        return request.cookies.get("fixture_role")

    def refuse(allowed=None):
        """None when the caller may go on, else the response: 401 without a valid login, 403 when the role is not allowed."""
        who = role()
        if who not in ROLE_COOKIE.values():
            return jsonify({"message": "login required"}), 401
        if who in ("InactiveMentor", "ExpiredGuest") or (allowed and who not in allowed):
            return jsonify({"message": "forbidden"}), 403
        return None

    def db():
        return sqlite3.connect(state.work)

    @app.route("/open")
    def open_route():
        return jsonify({"hello": "anyone"})

    @app.route("/needs-login")
    def needs_login():
        return refuse() or jsonify({"ok": True})

    @app.route("/admin-only", methods=["POST"])
    def admin_only():
        denied = refuse(["Admin"])
        if denied:
            return denied
        connection = db()
        connection.execute("INSERT INTO notes (text) VALUES ('written')")
        connection.commit()
        return jsonify({"ok": True})

    @app.route("/leaks-to-anyone")
    def leaks_to_anyone():
        return jsonify({"name": db().execute("SELECT text FROM notes WHERE id = 1").fetchone()[0]})

    @app.route("/leaks-to-staff")
    def leaks_to_staff():
        return refuse(["Admin", "Teacher"]) or jsonify({"email": CANARY_EMAIL})

    @app.route("/leaks-in-a-header")
    def leaks_in_a_header():
        response = jsonify({"ok": True})
        response.headers["X-Owner"] = CANARY_EMAIL
        return response

    @app.route("/counter", methods=["POST"])
    def counter():
        connection = db()
        connection.execute("UPDATE counter SET n = n + 1")
        connection.commit()
        return jsonify({"n": connection.execute("SELECT n FROM counter").fetchone()[0]})

    @app.route("/writes-on-get")
    def writes_on_get():
        connection = db()
        connection.execute("INSERT INTO notes (text) VALUES ('a GET wrote this')")
        connection.commit()
        return jsonify({"ok": True})

    @app.route("/writes-a-file", methods=["POST"])
    def writes_a_file():
        open(os.path.join(state.files, "upload.bin"), "w").write("x")
        return jsonify({"ok": True})

    @app.route("/crash")
    def crash():
        raise RuntimeError("boom")

    @app.route("/net")
    def net():
        try:
            socket.create_connection(("example.com", 80), timeout=1)
            return jsonify({"reached": NETWORK_REACHED})
        except OSError as error:
            # a network that is only down, not off, would still be a way out
            return jsonify({"reached": NETWORK_REACHED if "network is off" not in str(error) else None})

    @app.route("/hang")
    def hang():
        time.sleep(30)
        return jsonify({"ok": True})

    @app.route("/run", methods=["POST"])
    def run():
        data = request.get_json(silent=True) or {}
        open(os.path.join(state.files, "received_code.txt"), "w").write(str(data.get("code")))
        return jsonify({"ran": False})

    @app.route("/echo", methods=["POST"])
    def echo():
        data = request.get_json(silent=True) or {}
        received = {"name": data.get("name"), "email": data["email"] if "email" in data else None,
                    "password": data.get("password"), "q": request.args.get("q"), "ua": request.headers.get("User-Agent")}
        open(os.path.join(state.files, "received_echo.json"), "w").write(str(received))
        return jsonify({"ok": True})

    @app.route("/uid-field", methods=["POST"])
    def uid_field():
        data = request.get_json(silent=True) or {}
        open(os.path.join(state.files, "received_uidfield.txt"), "w").write(str(data.get("uid")))
        return jsonify({"ok": True})

    @app.route("/item/<int:item_id>")
    def item(item_id):
        open(os.path.join(state.files, "received_item.txt"), "w").write(str(item_id))
        return jsonify({"id": item_id})

    @app.route("/user/<string:uid>")
    def user(uid):
        open(os.path.join(state.files, "received_uid.txt"), "w").write(uid)
        return jsonify({"uid": uid})

    @app.route("/page/<path:page_key>")
    def page(page_key):
        return jsonify({"page": page_key})

    return app
