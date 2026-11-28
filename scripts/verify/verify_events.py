#!/usr/bin/env python3
"""Verify the Event Service (/api/event: api/event.py, model/event.py) end to end: events and their lifecycle,
who may do what, RSVPs with capacity and a waitlist, QR check-in, and event files.

Runs the real app against a throwaway SQLite database with the network blocked. Uploaded files go to a
temporary folder and the S3 checks use a fake S3 client, so nothing is stored anywhere real. Edited copies of
event_rules.json are written to a temporary folder too, never into the repo (verify_dataconfigs.py refuses a
stray JSON file). The real dev DB is checksummed before and after to prove it was never touched.

Every check has a number and prints one line for each thing it proves, so a failure is easy to find.

Usage:  python scripts/verify/verify_events.py        (from the flask repo root; about 5 seconds)
Exit:   0 all checks passed, 1 otherwise.
"""
import base64, copy, hashlib, io, json, logging, os, re, secrets, socket, sys, tempfile, time, warnings, zipfile
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.environ.setdefault("SECRET_KEY", "verify-events-throwaway-signing-key")  # 32+ bytes, so PyJWT does not warn

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
REAL_FILES = os.path.join(REPO, "instance", "data", "events")
RULES = os.path.join(REPO, "model", "dataconfigs", "event_rules.json")
PASSWORD = "Aa1!" + secrets.token_urlsafe(16)  # new every run; the accounts live only in the throwaway database
TOTAL = 56
PEOPLE = {"zz_teacher": "Teacher", "zz_admin": "Admin", "zz_alice": "User", "zz_bob": "User",
          "zz_carol": "User", "zz_dave": "User", "zz_guest": "Guest"}
FUTURE = (date.today() + timedelta(days=400)).isoformat()  # far enough ahead that RSVPs stay open whenever this runs
BAD_BODIES = {"no body": None, "a list": "[]", "null": "null", "a string": '"x"'}
# The event models and tables, as a SQLAlchemy warning would name them (not PresenceEvent or presence_events)
EVENT_NAMES = re.compile(r"(?<!\w)(Event|EventParticipant|EventFile)\.|(?<!\w)(events|event_participants|event_files)\b")
_results = {}  # check number -> [passed, ...]
ANSWERS = []   # (path, status, json) of every Event Service answer, for check 39


def check(number, label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + f"{number:>2}. {label}" + (f"   [{detail}]" if detail and not cond else ""))
    _results.setdefault(number, []).append(bool(cond))


@contextmanager
def checking(number):
    """One numbered check. An error inside it fails that check, and the script goes on to the next."""
    try:
        yield
    except Exception as error:  # noqa: BLE001 - reported as this check's failure
        check(number, "ran without an error", False, f"{type(error).__name__}: {error}"[:400])


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def files_under(folder):
    return sorted(os.path.join(d, f) for d, _, names in os.walk(folder) for f in names) if os.path.isdir(folder) else []


def b64(data):
    return base64.b64encode(data).decode()


def sets_another_rows_column(line):
    """Whether a line of Python assigns to another object's underscore column, like row._rsvp = ... (self._x is fine)."""
    left, sign, right = line.partition("=")
    if not sign or right.startswith("=") or left.rstrip()[-1:] in ("!", "<", ">"):
        return False  # no assignment: ==, !=, <=, >=
    return re.search(r"\b(?!self\b)\w+\._[a-z]", left) is not None


def zip_holding(*names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<xml/>")
    return buffer.getvalue()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%verify\n" + b"0" * 64
DOCX = zip_holding("[Content_Types].xml", "word/document.xml")
NOT_A_DOCX = zip_holding("notes.txt")


def mort(page_path, **changes):
    """Mort's example event from pages#2089, dated FUTURE. layout is his front matter key that the service drops."""
    body = {
        "layout": "event",
        "page_path": page_path,
        "title": "Community Technology Workshop",
        "description": "An introduction to practical AI.",
        "date": FUTURE, "start_time": "10:00", "end_time": "13:00", "timezone": "America/Los_Angeles",
        "categories": ["Community", "Technology", "Workshop"],
        "event_type": "workshop",
        "location": {"name": "Community Center", "address": "Poway, California"},
        "organizer": {"name": "Open Coding Society"},
        "registration": {"enabled": True, "capacity": 50},
        "attachments": [{"type": "gallery", "id": "workshop-gallery"}, {"type": "document", "id": "workshop-agenda"}],
        "features": {"microblog": True, "registration": True, "attendance": True, "gallery": True},
        "body": "## Agenda\n\nBring a laptop.",
    }
    body.update(changes)
    return body


class FakeS3:
    """Records what the S3 store asks for. fail=True makes the next call raise ClientError, as S3 does when down."""
    URL = "https://fake-s3.test/presigned-get"

    def __init__(self):
        self.puts, self.presigned, self.deletes, self.fail = [], [], [], False

    def _maybe_fail(self, operation):
        if self.fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "S3 is down"}}, operation)

    def put_object(self, **kwargs):
        self._maybe_fail("PutObject")
        self.puts.append(kwargs)

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self._maybe_fail("GetObject")
        self.presigned.append({"operation": operation, "params": Params, "expires": ExpiresIn})
        return self.URL

    def delete_object(self, **kwargs):
        self._maybe_fail("DeleteObject")
        self.deletes.append(kwargs)


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    real_files_before = files_under(REAL_FILES)

    def no_network(*_args, **_kwargs):
        raise OSError("network blocked by verify_events.py")
    socket.socket.connect = no_network

    import __init__ as pkg
    from flask.testing import FlaskClient
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SAWarning
    app, db = pkg.app, pkg.db
    work = tempfile.mkdtemp(prefix="events-")
    first_engine = create_engine(f"sqlite:///{os.path.join(work, 'events.db')}")
    with app.app_context():
        db.engines[None] = first_engine

    # 46 is decided here: importing every model and creating the tables, with every warning recorded
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        import main  # noqa: F401  registers every model and blueprint
        from sqlalchemy.orm import configure_mappers
        configure_mappers()
        with app.app_context():
            db.create_all()
    sa_warnings = [str(w.message) for w in seen if issubclass(w.category, SAWarning)]

    if os.path.exists(os.path.join(REPO, "model", "event_storage.py")):
        # Uploads go to a temporary folder. If this fails the script stops before any request is made.
        from model import event_storage
        event_storage.STORE = event_storage.EventFileStore(os.path.join(work, "files"))
    # (without the file the service is not built, and every check below fails)

    class RecordingClient(FlaskClient):
        """A test client that keeps every Event Service answer, so check 39 can read them all."""
        def open(self, *args, **kwargs):
            response = super().open(*args, **kwargs)
            path = args[0] if args and isinstance(args[0], str) else ""
            if path.startswith("/api/event") and response.is_json:
                ANSWERS.append((path, response.status_code, response.get_json(silent=True)))
            return response

    app.test_client_class = RecordingClient
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False  # an error in a route answers 500, as in production

    from model.user import User
    with app.app_context():
        for uid, role in PEOPLE.items():
            User(name=uid[3:].title() + " Verify", uid=uid, password=PASSWORD, role=role).create()
            User.query.filter_by(_uid=uid).first().activate()

        clients = {}
        for uid in PEOPLE:
            client = app.test_client()
            with redirect_stdout(io.StringIO()):  # the login route prints the token it sets
                r = client.post("/api/authenticate", json={"uid": uid, "password": PASSWORD})
            if r.status_code != 200:
                raise SystemExit(f"setup failed: {uid} could not log in ({r.status_code})")
            clients[uid] = client
        anon = app.test_client()
        teacher, admin, alice, bob, carol, dave, guest = (clients[u] for u in PEOPLE)
        person = lambda uid: User.query.filter_by(_uid=uid).first()
        ids, saved = {}, {}

        def body_of(r):
            return (r.get_json(silent=True) or {}) if r.is_json else {}

        def code_of(r):
            return body_of(r).get("code")

        def event_of(r):
            return body_of(r).get("event") or {}

        def model_error(call):
            """The EventError a model call raises, as its answer ({message, code, ...}, status), or None."""
            from model.event import EventError
            try:
                call()
            except EventError as error:
                return error.response()
            return None

        def upload(client, event_id, kind, name, data=None, alt=None, link=None, group="workshop-gallery"):
            body = {"event_id": event_id, "group": group, "kind": kind, "name": name}
            if data is not None:
                body["content"] = b64(data)
            if alt is not None:
                body["alt"] = alt
            if link is not None:
                body["link"] = link
            return client.post("/api/event/files", json=body)

        def roster_row(event_id, uid):
            rows = body_of(teacher.get("/api/event/participants", query_string={"event_id": event_id})).get("participants", [])
            return next((row for row in rows if row.get("uid") == uid), None)

        def status(client, event_id, to):
            return client.put("/api/event/status", json={"id": event_id, "status": to})

        E1 = "/events/events-verify/"

        # ---------------------------------------------------------------- the data file
        with checking(1):
            from model import event_rules
            with open(RULES, encoding="utf-8") as fh:
                rules = json.load(fh)
            try:
                event_rules._check(copy.deepcopy(rules))
                check(1, "event_rules.json as it is passes the loader's checks", True)
            except ValueError as error:
                check(1, "event_rules.json as it is passes the loader's checks", False, str(error))

            def refused(change, key):
                broken = copy.deepcopy(rules)
                change(broken)
                try:
                    event_rules._check(broken)
                except ValueError as error:
                    return str(error).startswith("event_rules.json:") and key in str(error)
                return False
            check(1, "a transition to an unknown status is refused, naming the key",
                  refused(lambda d: d["transitions"]["draft"].append("cancelled"), "transitions"))
            check(1, "an open_for action the service does not have is refused, naming the key",
                  refused(lambda d: d["open_for"].update(dance=["active"]), "open_for"))
            check(1, "a default_timezone that does not exist is refused, naming the key",
                  refused(lambda d: d.update(default_timezone="Mars/Base"), "default_timezone"))
            check(1, "max_bytes over 20,000,000 is refused, naming the key",
                  refused(lambda d: d["file_kinds"]["document"].update(max_bytes=20000001), "max_bytes"))
            check(1, "publish_roles naming a role account_types.json does not have is refused, naming the key",
                  refused(lambda d: d.update(publish_roles=["Principal"]), "publish_roles"))

        # ---------------------------------------------------------------- creating an event
        with checking(2):
            r = anon.post("/api/event", json=mort(E1))
            check(2, "POST signed out is 401", r.status_code == 401, r.status_code)
            r = alice.post("/api/event", json=mort(E1))
            check(2, "POST as a student is 403", r.status_code == 403, r.status_code)
            r = teacher.post("/api/event", json=mort(E1))
            event = event_of(r)
            ids["e1"] = event.get("id")
            check(2, "POST as a teacher is 201", r.status_code == 201, f"{r.status_code} {body_of(r)}")
            check(2, "the event starts as a draft and answers with Mort's keys",
                  event.get("status") == "draft" and event.get("page_path") == E1 and event.get("title") == "Community Technology Workshop"
                  and event.get("date") == FUTURE and event.get("start_time") == "10:00" and event.get("end_time") == "13:00"
                  and event.get("timezone") == "America/Los_Angeles" and event.get("registration") == {"enabled": True, "capacity": 50}
                  and event.get("features") == {"microblog": True, "registration": True, "attendance": True, "gallery": True}, event)
            check(2, "the rest of the definition is kept: description, location, attachments, body",
                  event.get("description") == "An introduction to practical AI." and event.get("event_type") == "workshop"
                  and event.get("location") == {"name": "Community Center", "address": "Poway, California"}
                  and event.get("attachments") == mort(E1)["attachments"] and event.get("body") == "## Agenda\n\nBring a laptop.", event)
            check(2, "a key the service does not keep (layout) is dropped", r.status_code == 201 and "layout" not in event)

        with checking(3):
            r = teacher.post("/api/event", json=mort("/events/verify-march/", date="2027-03-15", registration={"enabled": False}))
            event = event_of(r)
            ids["march"] = event.get("id")
            check(3, "2027-03-15 10:00-13:00 in America/Los_Angeles is 17:00Z to 20:00Z (daylight time)",
                  r.status_code == 201 and event.get("starts_at") == "2027-03-15T17:00:00.000Z" and event.get("ends_at") == "2027-03-15T20:00:00.000Z", event)
            r = teacher.post("/api/event", json={"page_path": "/events/verify-december/", "title": "December check",
                                                  "date": "2026-12-05", "start_time": "10:00", "timezone": "America/Los_Angeles"})
            event = event_of(r)
            ids["december"] = event.get("id")
            check(3, "2026-12-05 10:00 is 18:00Z (standard time)", r.status_code == 201 and event.get("starts_at") == "2026-12-05T18:00:00.000Z", event)
            check(3, "a start with no end lasts to the end of that day", event.get("ends_at") == "2026-12-06T08:00:00.000Z", event)

        with checking(4):
            r = teacher.post("/api/event", json=mort(E1))
            saved["page_taken"] = body_of(r)
            check(4, "the same page_path again is 409 with the existing event's id",
                  r.status_code == 409 and code_of(r) == "page_taken" and body_of(r).get("id") == ids["e1"], f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event", json={"id": ids["march"], "page_path": E1})
            check(4, "moving another draft onto that page_path is 409 with the existing event's id too",
                  r.status_code == 409 and code_of(r) == "page_taken" and body_of(r).get("id") == ids["e1"], f"{r.status_code} {body_of(r)}")

        with checking(5):
            cases = [
                ("a page_path outside /events/", {"page_path": "/blog/verify-bad/"}, "bad_page_path"),
                ("a page_path with capitals", {"page_path": "/events/Verify-Bad/"}, "bad_page_path"),
                ("a page_path with a space", {"page_path": "/events/verify bad/"}, "bad_page_path"),
                ("an empty title", {"title": ""}, "bad_title"),
                ("a 121-character title", {"title": "x" * 121}, "bad_title"),
                ("an end before the start", {"start_time": "13:00", "end_time": "10:00"}, "end_before_start"),
                ("an end with no start", {"start_time": None, "end_time": "13:00"}, "end_needs_start"),
                ("start_time 600 (YAML's unquoted 10:00)", {"start_time": 600}, "bad_time"),
                ('start_time "25:00"', {"start_time": "25:00"}, "bad_time"),
                ('date "03/15/2027"', {"date": "03/15/2027"}, "bad_date"),
                ('timezone "Mars/Base"', {"timezone": "Mars/Base"}, "bad_timezone"),
                ("capacity 0", {"registration": {"enabled": True, "capacity": 0}}, "bad_capacity"),
                ("capacity -1", {"registration": {"enabled": True, "capacity": -1}}, "bad_capacity"),
                ('capacity "x"', {"registration": {"enabled": True, "capacity": "x"}}, "bad_capacity"),
                ("capacity true", {"registration": {"enabled": True, "capacity": True}}, "bad_capacity"),
                ("an unknown feature key", {"features": {"microblog": True, "livestream": True}}, "bad_feature"),
                ("a notice over 280 characters", {"notice": "n" * 281}, "bad_notice"),
                ("status in the body", {"status": "published"}, "status_in_body"),
                ("a description over the limit", {"description": "d" * 501}, "bad_field"),
                ("categories that are not a list", {"categories": "Community"}, "bad_field"),
                ("categories that are not text", {"categories": [1, 2]}, "bad_field"),
                ("alert_days 400", {"alert_days": 400}, "bad_field"),
                ('presentation "javascript:alert(1)"', {"presentation": "javascript:alert(1)"}, "bad_field"),
                ('an image link to another site written //host', {"image": "//evil.test/x.png", "image_alt": "x"}, "bad_field"),
                ('an image with no image_alt', {"image": "/images/workshop.png"}, "bad_alt"),
            ]
            for number, (label, change, code) in enumerate(cases):
                body = mort(f"/events/verify-bad-{number}/")
                body.update(change)
                r = teacher.post("/api/event", json=body)
                check(5, f"{label} is a 400 ({code})", r.status_code == 400 and code_of(r) == code, f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- drafts, lists and the lifecycle
        with checking(6):
            r = anon.get("/api/event", query_string={"page_path": E1})
            check(6, "?page_path= of a draft is 200 with event null for an anonymous caller",
                  r.status_code == 200 and "event" in body_of(r) and body_of(r)["event"] is None and "viewer" in body_of(r), f"{r.status_code} {body_of(r)}")
            r = alice.get("/api/event", query_string={"page_path": E1})
            check(6, "and for alice, who does not organize it", r.status_code == 200 and body_of(r).get("event", "x") is None, body_of(r))
            r = teacher.get("/api/event", query_string={"page_path": E1})
            check(6, "the teacher sees the draft, and it can move only to published",
                  event_of(r).get("status") == "draft" and (body_of(r).get("viewer") or {}).get("next_statuses") == ["published"], body_of(r).get("viewer"))
            r = alice.get("/api/event", query_string={"id": ids["e1"]})
            check(6, "?id= of the draft is 404 for alice", r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "document", "agenda.pdf", PDF, group="workshop-agenda")
            ids["pdf"] = (body_of(r).get("file") or {}).get("id")
            check(6, "the teacher adds a PDF to the draft (201)", r.status_code == 201 and ids["pdf"], f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["pdf"]})
            check(6, "the draft's file is 404 for an anonymous caller", ids["pdf"] and r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")
            r = teacher.get("/api/event/files", buffered=True, query_string={"id": ids["pdf"]})
            check(6, "and the teacher can open it", r.status_code == 200 and r.data.startswith(b"%PDF-"), r.status_code)

        with checking(7):
            r = anon.get("/api/event")
            listed = [e.get("id") for e in body_of(r).get("events", [])]
            check(7, "the anonymous list leaves out drafts", r.status_code == 200 and ids["e1"] not in listed and body_of(r).get("count") == len(listed), body_of(r))
            r = teacher.get("/api/event", query_string={"mine": "true"})
            mine = body_of(r).get("events", [])
            check(7, "?mine=true includes the teacher's draft, without its body",
                  r.status_code == 200 and ids["e1"] in [e.get("id") for e in mine] and all("body" not in e for e in mine), body_of(r))
            r = anon.get("/api/event", query_string={"mine": "true"})
            check(7, "?mine=true signed out is 401", r.status_code == 401 and code_of(r) == "mine_needs_login", f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event", query_string={"status": "bogus"})
            check(7, "a status filter the service does not have is 400", r.status_code == 400 and code_of(r) == "bad_filter", f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event", query_string={"since": "03/15/2027"})
            check(7, "a since that is not YYYY-MM-DD is 400", r.status_code == 400 and code_of(r) == "bad_filter", f"{r.status_code} {body_of(r)}")

        with checking(8):
            r = status(teacher, ids["e1"], "registration")
            check(8, "draft to registration is 409 and says where it can go",
                  r.status_code == 409 and code_of(r) == "transition" and body_of(r).get("allowed") == ["published"], f"{r.status_code} {body_of(r)}")
            r = status(teacher, ids["e1"], "published")
            check(8, "draft to published is 200", r.status_code == 200 and event_of(r).get("status") == "published", f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event")
            listed = body_of(r).get("events", [])
            check(8, "the published event is in the anonymous list, without its body",
                  ids["e1"] in [e.get("id") for e in listed] and all("body" not in e for e in listed), body_of(r))
            r = status(alice, ids["e1"], "registration")
            check(8, "alice, who does not organize it, gets 403", r.status_code == 403 and code_of(r) == "not_manager", f"{r.status_code} {body_of(r)}")
            r = teacher.post("/api/event", json=mort("/events/verify-co-organizer/"))
            ids["co"] = event_of(r).get("id")
            r = teacher.put("/api/event/participants", json={"event_id": ids["co"], "uid": "zz_alice", "role": "organizer"})
            check(8, "the teacher makes alice an organizer of a new draft", r.status_code == 200 and (body_of(r).get("participant") or {}).get("role") == "organizer", body_of(r))
            r = status(alice, ids["co"], "published")
            check(8, "alice, an organizer who is not staff, cannot publish it (publish_role 403)",
                  r.status_code == 403 and code_of(r) == "publish_role", f"{r.status_code} {body_of(r)}")

        with checking(9):
            r = teacher.post("/api/event", json=mort("/events/verify-no-registration/", registration={"enabled": False, "capacity": None},
                                                    features={"microblog": True, "registration": True, "attendance": False, "gallery": False}))
            ids["noreg"] = event_of(r).get("id")
            r = status(teacher, ids["noreg"], "published")
            check(9, "an event with registration.enabled false can be published", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            r = status(teacher, ids["noreg"], "registration")
            check(9, "registration cannot open while registration.enabled is false (409)",
                  r.status_code == 409 and code_of(r) == "registration_off", f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- RSVPs
        with checking(10):
            r = teacher.put("/api/event", json={"id": ids["e1"], "registration": {"enabled": True, "capacity": 2}})
            check(10, "the teacher sets the capacity to 2", r.status_code == 200 and event_of(r).get("registration") == {"enabled": True, "capacity": 2}, body_of(r))
            r = status(teacher, ids["e1"], "registration")
            check(10, "registration opens (200)", r.status_code == 200 and event_of(r).get("status") == "registration", f"{r.status_code} {body_of(r)}")
            answers = {name: client.put("/api/event/rsvp", json={"event_id": ids["e1"]}) for name, client in (("alice", alice), ("bob", bob), ("carol", carol))}
            check(10, "alice and bob are going and carol is on the waitlist",
                  [body_of(answers[n]).get("rsvp") for n in ("alice", "bob", "carol")] == ["going", "going", "waitlist"],
                  {n: (a.status_code, body_of(a).get("rsvp")) for n, a in answers.items()})
            check(10, "counts: going 2, waitlist 1, spots_left 0",
                  event_of(answers["carol"]).get("counts") == {"going": 2, "waitlist": 1, "checked_in": 0, "spots_left": 0}, event_of(answers["carol"]).get("counts"))

        with checking(11):
            from model.event import EventParticipant
            r = alice.put("/api/event/rsvp", json={"event_id": ids["e1"]})
            rows = EventParticipant.query.filter_by(_event_id=ids["e1"], _user_id=person("zz_alice").id).count()
            check(11, "a repeated RSVP keeps alice's place, with one row and the same counts",
                  r.status_code == 200 and body_of(r).get("rsvp") == "going" and rows == 1
                  and event_of(r).get("counts") == {"going": 2, "waitlist": 1, "checked_in": 0, "spots_left": 0}, f"{rows} {body_of(r)}")

        with checking(12):
            r = alice.delete("/api/event/rsvp", json={"event_id": ids["e1"]})
            check(12, "alice cancels and is declined", r.status_code == 200 and body_of(r).get("rsvp") == "declined", f"{r.status_code} {body_of(r)}")
            check(12, "carol moves up from the waitlist", (body_of(carol.get("/api/event", query_string={"id": ids["e1"]})).get("viewer") or {}).get("rsvp") == "going")
            check(12, "counts: going 2, waitlist 0", event_of(r).get("counts") == {"going": 2, "waitlist": 0, "checked_in": 0, "spots_left": 0}, event_of(r).get("counts"))

        with checking(13):
            from model.event import Event
            r = alice.put("/api/event/rsvp", json={"event_id": ids["noreg"]})
            check(13, "an RSVP to a published event is 409", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")
            r = bob.put("/api/event/rsvp", json={"event_id": ids["co"]})
            check(13, "an RSVP to a draft is 404", r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")
            e1 = db.session.get(Event, ids["e1"])
            answer = model_error(lambda: e1.rsvp(person("zz_dave"), now=e1.starts_at + timedelta(minutes=1)))
            check(13, "an RSVP once the event has started is 409 (started)", answer is not None and answer[0].get("code") == "started" and answer[1] == 409, answer)

        with checking(14):
            r = anon.get("/api/event", query_string={"id": ids["e1"]})
            text = r.get_data(as_text=True)
            leaked = [word for uid in PEOPLE for word in (uid, uid[3:].title() + " Verify") if word in text]
            check(14, "the anonymous read has the counts", event_of(r).get("counts", {}).get("going") == 2, event_of(r).get("counts"))
            check(14, "and no participant's uid or name anywhere", r.status_code == 200 and not leaked, leaked)

        with checking(15):
            viewer = body_of(alice.get("/api/event", query_string={"id": ids["e1"]})).get("viewer") or {}
            check(15, "alice sees her own RSVP in viewer, and no next_statuses", viewer.get("rsvp") == "declined" and "next_statuses" not in viewer
                  and viewer.get("manages") is False and viewer.get("signed_in") is True, viewer)

        # ---------------------------------------------------------------- check-in
        with checking(16):
            r = teacher.get("/api/event/checkin", query_string={"event_id": ids["e1"]})
            check(16, "GET checkin before the event is active is 409", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")
            r = status(teacher, ids["e1"], "active")
            check(16, "the event moves to active", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            r = teacher.get("/api/event/checkin", query_string={"event_id": ids["e1"]})
            code = body_of(r).get("code")
            check(16, "the teacher gets an 8-character code with 0 < seconds_left <= 300",
                  r.status_code == 200 and isinstance(code, str) and len(code) == 8 and 0 < body_of(r).get("seconds_left", 0) <= 300
                  and body_of(r).get("code_seconds") == 300, body_of(r))
            r = alice.get("/api/event/checkin", query_string={"event_id": ids["e1"]})
            check(16, "alice gets 403", r.status_code == 403 and code_of(r) == "not_manager", f"{r.status_code} {body_of(r)}")

        def current_code():
            return body_of(teacher.get("/api/event/checkin", query_string={"event_id": ids["e1"]})).get("code")

        with checking(17):
            r = bob.post("/api/event/checkin", json={"event_id": ids["e1"], "code": current_code()})
            first = body_of(r).get("checked_in_at")
            check(17, "bob checks in with the code (200, method qr)",
                  r.status_code == 200 and first and (roster_row(ids["e1"], "zz_bob") or {}).get("check_in_method") == "qr", f"{r.status_code} {body_of(r)}")
            time.sleep(0.01)
            r = bob.post("/api/event/checkin", json={"event_id": ids["e1"], "code": current_code()})
            check(17, "checking in again keeps the first time", r.status_code == 200 and body_of(r).get("checked_in_at") == first, body_of(r))

        with checking(18):
            from model import event_rules
            from model.event import Event
            from model.presence import utc_now
            r = carol.post("/api/event/checkin", json={"event_id": ids["e1"], "code": "AAAAAAAA"})
            check(18, "a wrong code is 400", r.status_code == 400 and code_of(r) == "bad_code", f"{r.status_code} {body_of(r)}")
            e1, now, seconds = db.session.get(Event, ids["e1"]), utc_now(), event_rules.code_seconds()
            old, _ = e1.check_in_code(now=now - timedelta(seconds=2 * seconds))
            answer = model_error(lambda: e1.check_in(person("zz_carol"), old, now=now))
            check(18, "a code from two periods ago is 400", answer is not None and answer[0].get("code") == "bad_code" and answer[1] == 400, answer)
            previous, _ = e1.check_in_code(now=now - timedelta(seconds=seconds))
            participant = e1.check_in(person("zz_carol"), previous, now=now)
            check(18, "the previous period's code still works, for carol", participant.checked_in_at is not None and participant.check_in_method == "qr")

        with checking(19):
            r = dave.post("/api/event/checkin", json={"event_id": ids["e1"], "code": current_code()})
            row = roster_row(ids["e1"], "zz_dave") or {}
            check(19, "dave, who never RSVPed, checks in as a walk-in: rsvp null, with a check-in time",
                  r.status_code == 200 and "rsvp" in row and row["rsvp"] is None and row.get("checked_in_at"), f"{r.status_code} {row}")

        with checking(20):
            put = lambda **fields: teacher.put("/api/event/participants", json={"event_id": ids["e1"], "uid": "zz_carol", **fields})
            r = put(checked_in=False)
            check(20, "the teacher clears carol's QR check-in", r.status_code == 200 and (body_of(r).get("participant") or {}).get("checked_in_at") is None, body_of(r))
            r = put(checked_in=True)
            row = body_of(r).get("participant") or {}
            check(20, "the teacher checks carol in by name (method organizer)", r.status_code == 200 and row.get("checked_in_at") and row.get("check_in_method") == "organizer", row)
            r = put(checked_in=False)
            check(20, "and clears it", r.status_code == 200 and (body_of(r).get("participant") or {}).get("checked_in_at") is None, body_of(r))

        with checking(21):
            r = teacher.put("/api/event/participants", json={"event_id": ids["e1"], "uid": "zz_alice", "role": "organizer"})
            check(21, "the teacher makes alice an organizer", r.status_code == 200 and (body_of(r).get("participant") or {}).get("role") == "organizer", body_of(r))
            r = alice.put("/api/event", json={"id": ids["e1"], "notice": "Doors open at 9:45"})
            check(21, "alice can now set a notice", r.status_code == 200 and event_of(r).get("notice") == "Doors open at 9:45", f"{r.status_code} {body_of(r)}")
            r = alice.get("/api/event/participants", query_string={"event_id": ids["e1"]})
            check(21, "and read the roster", r.status_code == 200 and len(body_of(r).get("participants", [])) >= 4, f"{r.status_code}")
            r = alice.put("/api/event/participants", json={"event_id": ids["e1"], "uid": "zz_bob", "role": "organizer"})
            check(21, "alice making bob an organizer is 403: only the owner or staff change roles", r.status_code == 403 and code_of(r) == "not_owner", f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event/participants", json={"event_id": ids["e1"], "uid": "zz_bob", "role": "boss"})
            check(21, "a role that is not a participant role is 400, and the message lists the data's roles",
                  r.status_code == 400 and code_of(r) == "bad_role" and "attendee, organizer" in body_of(r).get("message", ""), body_of(r))
            r = teacher.put("/api/event/participants", json={"event_id": ids["e1"], "uid": "zz_alice", "role": "attendee"})
            check(21, "the teacher makes alice an attendee again", r.status_code == 200 and (body_of(r).get("participant") or {}).get("role") == "attendee", body_of(r))

        with checking(22):
            r = admin.get("/api/event/participants", query_string={"event_id": ids["e1"]})
            check(22, "the admin can read the roster of an event they do not own", r.status_code == 200, r.status_code)
            r = admin.put("/api/event", json={"id": ids["e1"], "notice": None})
            check(22, "and change it", r.status_code == 200 and event_of(r).get("notice") is None, f"{r.status_code} {body_of(r)}")

        with checking(23):
            from model.event import Event
            r = status(teacher, ids["e1"], "completed")
            check(23, "the event moves to completed", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            r = teacher.get("/api/event/checkin", query_string={"event_id": ids["e1"]})
            check(23, "GET checkin is 409 once completed", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")
            r = dave.post("/api/event/checkin", json={"event_id": ids["e1"], "code": "AAAAAAAA"})
            check(23, "POST checkin is 409 once completed", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")
            db.session.expire_all()
            check(23, "and the check-in secret is cleared", db.session.get(Event, ids["e1"])._checkin_secret is None)

        with checking(24):
            r = guest.put("/api/event/rsvp", json={"event_id": ids["e1"]})
            check(24, "a guest's RSVP is 403: guest allowed_requests do not list it", r.status_code == 403, f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- files
        with checking(25):
            r = upload(teacher, ids["e1"], "gallery", "students.png", PNG, alt="Students working at laptops")
            ids["png"] = (body_of(r).get("file") or {}).get("id")
            check(25, "a PNG for the gallery, with its alt text, is 201", r.status_code == 201 and ids["png"], f"{r.status_code} {body_of(r)}")
            files = event_of(anon.get("/api/event", query_string={"id": ids["e1"]})).get("files", [])
            check(25, "the public read shows the photo with its alt text",
                  any(f.get("id") == ids["png"] and f.get("alt") == "Students working at laptops" and f.get("url") == f"/api/event/files?id={ids['png']}" for f in files), files)
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["png"]})
            check(25, "an anonymous GET gives image/png, inline, with nosniff",
                  r.status_code == 200 and r.mimetype == "image/png" and r.headers.get("Content-Disposition", "").startswith("inline")
                  and r.headers.get("X-Content-Type-Options") == "nosniff" and r.data == PNG, f"{r.status_code} {dict(r.headers)}")

        with checking(26):
            for name in ("setup.exe", "logo.svg", "page.html"):
                r = upload(teacher, ids["e1"], "document", name, PDF, group="workshop-agenda")
                check(26, f"a file named {name} is 400", r.status_code == 400 and code_of(r) == "bad_file_type", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "document", "photo.png", PDF, group="workshop-agenda")
            check(26, "a .png name holding PDF bytes is 400", r.status_code == 400 and code_of(r) == "bad_content", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "document", "notes.docx", NOT_A_DOCX, group="workshop-agenda")
            check(26, "a .docx that is a zip without word/document.xml is 400", r.status_code == 400 and code_of(r) == "bad_content", f"{r.status_code} {body_of(r)}")
            from model import event_rules
            limit = event_rules.file_kind("document")["max_bytes"]
            r = upload(teacher, ids["e1"], "document", "big.pdf", b"%PDF-" + b"0" * (limit + 1 - 5), group="workshop-agenda")
            check(26, "a file of max_bytes + 1 is 413", r.status_code == 413 and code_of(r) == "too_big", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["noreg"], "gallery", "photo.png", PNG, alt="A photo")
            check(26, "a gallery upload while the gallery is off is 409", r.status_code == 409 and code_of(r) == "gallery_off", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "gallery", "photo.png", PNG)
            check(26, "a gallery photo with no alt text is 400", r.status_code == 400 and code_of(r) == "bad_alt", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "gallery", "photo.png", PNG, alt="a" * 201)
            check(26, "a gallery photo with 201 characters of alt text is 400", r.status_code == 400 and code_of(r) == "bad_alt", f"{r.status_code} {body_of(r)}")

        with checking(27):
            r = upload(teacher, ids["e1"], "document", "handout.docx", DOCX, group="workshop-agenda")
            ids["docx"] = (body_of(r).get("file") or {}).get("id")
            check(27, "a Word document is 201", r.status_code == 201 and ids["docx"], f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["docx"]})
            disposition = r.headers.get("Content-Disposition", "")
            check(27, "the docx downloads as an attachment with its name", r.status_code == 200 and disposition.startswith("attachment") and "handout.docx" in disposition, disposition)
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["pdf"]})
            check(27, "the pdf opens inline", r.status_code == 200 and r.mimetype == "application/pdf" and r.headers.get("Content-Disposition", "").startswith("inline"), dict(r.headers))
            r = upload(teacher, ids["e1"], "document", "Повестка дня.PDF", PDF, group="workshop-agenda")
            check(27, "a file name in another alphabet keeps its type: a safe name ending in .pdf",
                  r.status_code == 201 and (body_of(r).get("file") or {}).get("name", "").endswith(".pdf"), f"{r.status_code} {body_of(r)}")

        with checking(28):
            r = alice.delete("/api/event/files", json={"id": ids["docx"]})
            check(28, "alice deleting a file is 403", r.status_code == 403 and code_of(r) == "not_manager", f"{r.status_code} {body_of(r)}")
            r = teacher.delete("/api/event/files", json={"id": ids["docx"]})
            check(28, "the teacher deletes it (204)", r.status_code == 204, r.status_code)
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["docx"]})
            check(28, "and the file is then 404", r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")

        with checking(29):
            from model import event_storage as storage
            from model.event import EventFile
            disk, fake = storage.STORE, FakeS3()
            storage.STORE = storage.S3EventFileStore("verify-bucket", client=fake)
            try:
                r = upload(teacher, ids["e1"], "gallery", "stage.png", PNG, alt="The stage before the talk")
                ids["s3"] = (body_of(r).get("file") or {}).get("id")
                key = fake.puts[-1]["Key"] if fake.puts else ""
                check(29, "with S3 the upload is 201 and its key starts with flask/events/<id>/",
                      r.status_code == 201 and key.startswith(f"flask/events/{ids['e1']}/") and fake.puts[-1].get("Body") == PNG, f"{r.status_code} {key}")
                r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["s3"]})
                params = fake.presigned[-1]["params"] if fake.presigned else {}
                check(29, "a GET is a 302 to the presigned URL, for 5 minutes, inline as image/png",
                      r.status_code == 302 and r.headers.get("Location") == FakeS3.URL and fake.presigned[-1]["expires"] == 300
                      and params.get("ResponseContentType") == "image/png" and params.get("ResponseContentDisposition", "").startswith("inline"), f"{r.status_code} {params}")
                fake.fail = True
                before = EventFile.query.count()
                logging.disable(logging.CRITICAL)  # the service logs the store's failure; this one is on purpose
                try:
                    r = upload(teacher, ids["e1"], "gallery", "down.png", PNG, alt="Saved while S3 is down")
                finally:
                    logging.disable(logging.NOTSET)
                saved["storage_down"] = body_of(r)
                check(29, "a ClientError on save is 503 with Retry-After: 30", r.status_code == 503 and r.headers.get("Retry-After") == "30"
                      and code_of(r) == "storage_down", f"{r.status_code} {body_of(r)}")
                check(29, "and no row is left behind", EventFile.query.count() == before)
            finally:
                storage.STORE = disk

        # ---------------------------------------------------------------- the notice, the address, deleting and archiving
        with checking(30):
            r = teacher.put("/api/event", json={"id": ids["e1"], "notice": "Moved to Room 12"})
            check(30, "a notice is saved (200)", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            check(30, "the notice shows in the public read", event_of(anon.get("/api/event", query_string={"id": ids["e1"]})).get("notice") == "Moved to Room 12")
            teacher.put("/api/event", json={"id": ids["e1"], "notice": None})
            check(30, "null clears it", event_of(anon.get("/api/event", query_string={"id": ids["e1"]})).get("notice", "x") is None)
            r = teacher.put("/api/event", json={"id": ids["e1"], "page_path": "/events/events-verify-moved/"})
            check(30, "changing page_path after publishing is 409", r.status_code == 409 and code_of(r) == "page_frozen", f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event", json={"id": ids["e1"], "page_path": E1, "title": "Community Technology Workshop"})
            check(30, "sending the same page_path again is fine", r.status_code == 200, f"{r.status_code} {body_of(r)}")

        with checking(31):
            r = teacher.delete("/api/event", json={"id": ids["noreg"]})
            check(31, "deleting a published event is 409", r.status_code == 409 and code_of(r) == "delete_live", f"{r.status_code} {body_of(r)}")
            r = status(teacher, ids["e1"], "archived")
            check(31, "the event is archived (200)", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            check(31, "the public read says archived", event_of(anon.get("/api/event", query_string={"id": ids["e1"]})).get("status") == "archived")
            r = teacher.put("/api/event", json={"id": ids["e1"], "title": "Changed after archiving"})
            check(31, "PUT on an archived event is 409", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["e1"], "document", "late.pdf", PDF, group="workshop-agenda")
            check(31, "POST files on an archived event is 409", r.status_code == 409 and code_of(r) == "not_open", f"{r.status_code} {body_of(r)}")

        with checking(32):
            r = teacher.delete("/api/event", json={"id": ids["december"]})
            check(32, "deleting a fresh draft is 204", r.status_code == 204, f"{r.status_code} {body_of(r)}")
            check(32, "and it is gone", code_of(teacher.get("/api/event", query_string={"id": ids["december"]})) == "not_found")

        with checking(33):
            r = teacher.put("/api/event", json={"id": ids["noreg"], "calendar_item_id": 42})
            check(33, "a manager sets calendar_item_id (200)", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            check(33, "it is in a manager's read", event_of(teacher.get("/api/event", query_string={"id": ids["noreg"]})).get("calendar_item_id") == 42)
            reads = [event_of(c.get("/api/event", query_string={"id": ids["noreg"]})) for c in (anon, alice)]
            check(33, "and not in an anonymous read or alice's", ids["noreg"] and all(e.get("id") == ids["noreg"] and "calendar_item_id" not in e for e in reads), reads)
            r = teacher.put("/api/event", json={"id": ids["noreg"], "calendar_item_id": True})
            check(33, "a calendar_item_id that is not a whole number is 400", r.status_code == 400 and code_of(r) == "bad_field", f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- the backup bundle and removing a user
        with checking(34):
            from model.event_backup import event_counts, export_events, import_events
            bundle = json.loads(json.dumps(export_events()))  # through JSON, as the export route sends it
            before = event_counts()
            db.session.remove()
            restored_engine = create_engine(f"sqlite:///{os.path.join(work, 'restored.db')}")
            db.engines[None] = restored_engine
            try:
                db.create_all()
                for uid, role in PEOPLE.items():
                    User(name=uid[3:].title() + " Verify", uid=uid, password=PASSWORD, role=role).create()
                first = import_events(bundle)
                check(34, "importing the export into a second database gives the same counts", event_counts() == before and before["events"] > 0,
                      f"{before} -> {event_counts()} {first}")
                again = import_events(bundle)
                check(34, "importing it twice adds nothing", event_counts() == before and all(t["imported"] == 0 and t["failed"] == 0 for t in again.values()), again)
            finally:
                db.session.remove()
                db.engines[None] = first_engine
                restored_engine.dispose()

        with checking(35):
            from model.event import EventParticipant
            import remove_user
            carol_id = person("zz_carol").id
            had = EventParticipant.query.filter_by(_user_id=carol_id).count()
            person("zz_carol").delete()
            check(35, "deleting carol through the model removes her participant rows",
                  had > 0 and EventParticipant.query.filter_by(_user_id=carol_id).count() == 0 and person("zz_carol") is None, had)
            check(35, "remove_user.LINKS has event_participants", "event_participants" in remove_user.LINKS)

        # ---------------------------------------------------------------- the routes
        new_routes = [r for r in app.url_map.iter_rules()
                      if r.rule == "/api/event" or r.rule.startswith("/api/event/") or r.rule in ("/api/export/events", "/api/export/import/events")]
        requests = sorted((method, rule.rule) for rule in new_routes for method in rule.methods - {"HEAD", "OPTIONS"})

        with checking(36):
            from model import permissions
            missing = [f"{m} {r}" for m, r in requests if f"{m} {r}" not in permissions.every_request()]
            check(36, f"every one of the {len(requests)} new requests is in permissions.json", len(requests) == 19 and not missing, missing or len(requests))
            public = {("GET", "/api/event"), ("GET", "/api/event/options"), ("GET", "/api/event/files")}
            wrong = [f"{m} {r}: {permissions.who_may_call(m, r)}" for m, r in requests
                     if permissions.who_may_call(m, r) != ("anyone" if (m, r) in public else ["Admin", "Teacher"] if (m, r) == ("POST", "/api/event") else "signed_in")]
            check(36, "3 public reads, create for Admin and Teacher, and the other 15 for any signed-in account", len(requests) == 19 and not wrong, wrong)

        with checking(37):
            from api.request_body import NotJsonObjectError
            crashes = []
            for method, rule in requests:
                if method not in ("POST", "PUT", "DELETE"):
                    continue
                for label, raw in BAD_BODIES.items():
                    r = admin.open(rule, method=method, **({} if raw is None else {"data": raw, "content_type": "application/json"}))
                    if r.status_code != 400 or body_of(r).get("message") != NotJsonObjectError.description:
                        crashes.append(f"{method} {rule} with {label}: {r.status_code}")
            tried = sum(1 for method, _ in requests if method in ("POST", "PUT", "DELETE"))
            check(37, f"a body that is not a JSON object is a 400 on every one of the {tried} new POST, PUT and DELETE requests, never a 500",
                  tried == 13 and not crashes, "; ".join(crashes[:6]) or tried)

        # ---------------------------------------------------------------- one message table
        with checking(39):
            from api.request_body import NotJsonObjectError
            from model.event import Event, MESSAGES
            guard = lambda b: "error" in b and "data" in b  # the login guard's own answers, before the route
            from_werkzeug = lambda s, b: b == {"message": NotJsonObjectError.description} or (s == 413 and set(b) == {"message"})
            errors = [(p, s, b) for p, s, b in ANSWERS if s >= 400 and isinstance(b, dict)]
            uncoded = [(p, s, b) for p, s, b in errors if not guard(b) and not from_werkzeug(s, b) and b.get("code") not in MESSAGES]
            check(39, f"every Event Service error answer of this run ({len(errors)}) carries a code that is a key of MESSAGES", errors and not uncoded, uncoded[:3])
            check(39, "the storage answer has code storage_down", saved.get("storage_down", {}).get("code") == "storage_down", saved.get("storage_down"))
            original = Event.options
            Event.options = staticmethod(lambda: 1 / 0)
            logging.disable(logging.CRITICAL)
            try:
                r = anon.get("/api/event/options")
            finally:
                Event.options = original
                logging.disable(logging.NOTSET)
            check(39, "an unexpected error is a 500 with code server_error", r.status_code == 500 and code_of(r) == "server_error", f"{r.status_code} {body_of(r)}")
            check(39, "page_taken's id is still the event's id", saved.get("page_taken", {}).get("id") == ids["e1"], saved.get("page_taken"))
            r = teacher.put("/api/event", data="[]", content_type="application/json")
            check(39, "json_object's 400 has no code", r.status_code == 400 and "code" not in body_of(r), body_of(r))

        with checking(40):
            from model.event_backup import export_events
            path = "/events/verify-published-at/"
            r = teacher.post("/api/event", json=mort(path))
            ids["pa"] = event_of(r).get("id")
            check(40, "published_at is null on a draft", r.status_code == 201 and "published_at" in event_of(r) and event_of(r)["published_at"] is None, event_of(r))
            published = event_of(status(teacher, ids["pa"], "published")).get("published_at")
            check(40, "it is set when the draft is published", isinstance(published, str) and published.endswith("Z"), published)
            time.sleep(0.01)
            back = event_of(status(teacher, ids["pa"], "draft")).get("published_at")
            again = event_of(status(teacher, ids["pa"], "published")).get("published_at")
            check(40, "and unchanged after it goes back to draft and is published again", back == published and again == published, f"{published} {back} {again}")
            in_bundle = next((e.get("published_at") for e in export_events()["events"] if e.get("page_path") == path), None)
            check(40, "the backup bundle keeps it (to the microsecond; the read gives milliseconds)",
                  isinstance(in_bundle, str) and isinstance(published, str) and in_bundle[:23] == published[:23], f"{in_bundle} {published}")

        with checking(41):
            # a photo of the published event: e1's are read only now that it is archived (check 49)
            ids["pa_png"] = (body_of(upload(teacher, ids["pa"], "gallery", "students.png", PNG, alt="Students at laptops")).get("file") or {}).get("id")
            r = teacher.put("/api/event/files", json={"id": ids["pa_png"], "alt": "Students pair-programming at laptops"})
            check(41, "a manager sets a photo's alt text (200)", r.status_code == 200 and (body_of(r).get("file") or {}).get("alt") == "Students pair-programming at laptops", body_of(r))
            r = alice.put("/api/event/files", json={"id": ids["pa_png"], "alt": "Changed by alice"})
            check(41, "it is 403 for alice", r.status_code == 403 and code_of(r) == "not_manager", f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event/files", json={"id": ids["pa_png"], "alt": ""})
            check(41, "an empty alt is 400", r.status_code == 400 and code_of(r) == "bad_alt", f"{r.status_code} {body_of(r)}")

        with checking(42):
            youtube = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            r = upload(teacher, ids["co"], "gallery", "Workshop recording", link=youtube)
            video = body_of(r).get("file") or {}
            check(42, "a YouTube link in the gallery is 201, with link and no url",
                  r.status_code == 201 and video.get("link") == youtube and "url" not in video and video.get("name") == "Workshop recording", f"{r.status_code} {body_of(r)}")
            for label, kind, link in (("a link to another host", "gallery", "https://evil.test/v"), ("an http link", "gallery", "http://youtu.be/x"),
                                      ("a link in the document kind", "document", youtube)):
                r = upload(teacher, ids["co"], kind, "Recording", link=link)
                check(42, f"{label} is 400", r.status_code == 400 and code_of(r) == "bad_video", f"{r.status_code} {body_of(r)}")
            # urlsplit and a browser must read the same host: a browser reads \ as /, and user@ hides the real host
            for label, link in (("a backslash before @ (a browser goes to evil.test)", "https://evil.test\\@www.youtube.com/watch?v=x"),
                                ("a user name before the host", "https://someone@www.youtube.com/watch?v=x"),
                                ("a user name and password before the host", "https://a:b@www.youtube.com/watch?v=x"),
                                ("a double quote", 'https://www.youtube.com/watch?v="x"'),
                                ("a single quote", "https://www.youtube.com/watch?v=x'y"),
                                ("< and >", "https://www.youtube.com/watch?v=<x>"),
                                ("a control character", "https://www.youtube.com/watch?v=x\x01"),
                                ("a % that is not %XX", "https://www.youtube.com/watch?v=%zz")):
                r = upload(teacher, ids["co"], "gallery", "Recording", link=link)
                check(42, f"a video link with {label} is 400", r.status_code == 400 and code_of(r) == "bad_video", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["co"], "gallery", "Second recording", link="HTTPS://www.youtube.com/watch?v=abc123")
            check(42, "a link is saved as urlsplit reads it, not as sent (https in lower case)",
                  r.status_code == 201 and (body_of(r).get("file") or {}).get("link") == "https://www.youtube.com/watch?v=abc123", f"{r.status_code} {body_of(r)}")
            for label, change in (("a site path with a backslash", {"presentation": "/slides\\evil.test"}),
                                  ("a site path with < and >", {"presentation": "/talks/<script>"}),
                                  ("an https link with a double quote", {"image_credit": {"name": "A", "url": 'https://example.com/a"b'}}),
                                  ("an https link with a space", {"location": {"name": "Room", "url": "https://example.com/a b"}})):
                r = teacher.put("/api/event", json={"id": ids["co"], **change})
                check(42, f"a definition link, {label}, is 400", r.status_code == 400 and code_of(r) == "bad_field", f"{r.status_code} {code_of(r)}")
            r = teacher.put("/api/event", json={"id": ids["co"], "presentation": "/talks/intro%20slides/"})
            check(42, "a site path with %XX is fine", r.status_code == 200 and event_of(r).get("presentation") == "/talks/intro%20slides/", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["co"], "gallery", "both.png", PNG, alt="Both", link=youtube)
            check(42, "content and link together is 400", r.status_code == 400 and code_of(r) == "bad_item", f"{r.status_code} {body_of(r)}")
            r = teacher.get("/api/event/files", buffered=True, query_string={"id": video.get("id")})
            check(42, "GET files of the video is 404: nothing is stored", r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")

        with checking(43):
            from importlib.metadata import version
            flask_version = version("flask")
            major_minor = tuple(int(part) for part in flask_version.split(".")[:2])
            check(43, f"Flask is 3.1 or newer, for the upload route's own size limit (this is Flask {flask_version})", major_minor >= (3, 1), f"Flask {flask_version} is older than 3.1")
            r = upload(teacher, ids["co"], "gallery", "poster.png", PNG + b"\x00" * (9 * 1024 * 1024 - len(PNG)), alt="The event poster")
            check(43, "a 9 MB PNG uploads on POST /api/event/files (201)", r.status_code == 201, f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event", data=json.dumps({"id": ids["co"], "description": "x" * 6000000}), content_type="application/json")
            check(43, "a 6 MB body to PUT /api/event is still 413: the 5 MB limit holds everywhere else", r.status_code == 413, r.status_code)

        # ---------------------------------------------------------------- roles and moves are data
        rules_folder = tempfile.mkdtemp(prefix="event-rules-")

        def rules_copy(name, change):
            with open(RULES, encoding="utf-8") as fh:
                data = json.load(fh)
            change(data)
            path = os.path.join(rules_folder, f"{name}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return path

        with checking(44):
            from model import event_rules
            try:
                event_rules.load(rules_copy("helper", lambda d: (d["participant_roles"].append("helper"), d["manager_roles"].append("helper"))))
                r = teacher.put("/api/event/participants", json={"event_id": ids["co"], "uid": "zz_bob", "role": "helper"})
                check(44, "with helper added to participant_roles, the teacher makes bob a helper", r.status_code == 200, f"{r.status_code} {body_of(r)}")
                r = bob.get("/api/event/participants", query_string={"event_id": ids["co"]})
                check(44, "and with helper in manager_roles, bob can read the roster", r.status_code == 200, f"{r.status_code} {body_of(r)}")
                event_rules.load(rules_copy("no-managers", lambda d: d.update(manager_roles=[])))
                r = alice.get("/api/event/participants", query_string={"event_id": ids["co"]})
                check(44, "with manager_roles empty, an organizer cannot", r.status_code in (403, 404), f"{r.status_code} {body_of(r)}")
                check(44, "while the owner and staff still can",
                      teacher.get("/api/event/participants", query_string={"event_id": ids["co"]}).status_code == 200
                      and admin.get("/api/event/participants", query_string={"event_id": ids["co"]}).status_code == 200)
            finally:
                event_rules.load()

        with checking(45):
            from model import event_rules
            from model.event import Event
            try:
                event_rules.load(rules_copy("check-in-after", lambda d: d["open_for"].update(check_in=["active", "completed"])))
                secret = lambda: (db.session.expire_all(), db.session.get(Event, ids["noreg"])._checkin_secret)[1]
                status(teacher, ids["noreg"], "active")
                opened = secret()
                r = status(teacher, ids["noreg"], "completed")
                check(45, "with check_in open while completed, active to completed keeps the check-in secret", r.status_code == 200 and opened and secret() == opened, r.status_code)
                r = status(teacher, ids["noreg"], "archived")
                check(45, "and moving to archived clears it", r.status_code == 200 and secret() is None, r.status_code)
            finally:
                event_rules.load()

        with checking(46):
            # Upstream's own models already give two warnings (Section.users and User.sections), so an error filter
            # would fail on code this service does not touch: the check is that none of them is about the event tables.
            ours = [w for w in sa_warnings if EVENT_NAMES.search(w)]
            from model.event import Event, EventFile, EventParticipant  # noqa: F401  the models are there to be checked
            check(46, "importing the models and creating the tables gives no SQLAlchemy warning about the event tables", not ours, "; ".join(ours)[:300])

        # ---------------------------------------------------------------- the model's style
        with checking(47):
            from model.event import EventFile, EventParticipant
            for model in (EventParticipant, EventFile):
                missing = [c.name for c in model.__table__.columns
                           if c.name != "id" and not isinstance(getattr(model, c.name.lstrip("_"), None), property)]
                check(47, f"{model.__name__} has a property for every column", not missing, missing)
            with open(os.path.join(REPO, "model", "event.py"), encoding="utf-8") as fh:
                reaches = [line.strip() for line in fh if sets_another_rows_column(line)]
            check(47, "Event changes another class's row only through its property setters", not reaches, reaches[:4])

        # ---------------------------------------------------------------- archived is read only
        with checking(49):
            from model.event import Event
            # an event taking RSVPs, for the RSVP window (50 and 51 use it too)
            ids["window"] = event_of(teacher.post("/api/event", json=mort("/events/verify-window/"))).get("id")
            status(teacher, ids["window"], "published")
            status(teacher, ids["window"], "registration")
            r = alice.put("/api/event/rsvp", json={"event_id": ids["window"]})
            check(49, "alice RSVPs to an event taking RSVPs", r.status_code == 200 and body_of(r).get("rsvp") == "going", f"{r.status_code} {body_of(r)}")
            window = db.session.get(Event, ids["window"])
            answer = model_error(lambda: window.cancel_rsvp(person("zz_alice"), now=window.starts_at + timedelta(minutes=1)))
            db.session.rollback()
            check(49, "cancelling once the event has started is 409 (started), the same window as an RSVP",
                  answer is not None and answer[0].get("code") == "started" and answer[1] == 409, answer)

            participants = lambda send, **fields: send("/api/event/participants", json={"event_id": ids["e1"], **fields})
            refusals = [
                ("bob cancels his RSVP", "rsvp", lambda: bob.delete("/api/event/rsvp", json={"event_id": ids["e1"]})),
                ("the teacher makes bob an organizer", "edit", lambda: participants(teacher.put, uid="zz_bob", role="organizer")),
                ("the teacher removes bob", "edit", lambda: participants(teacher.delete, uid="zz_bob")),
                ("the teacher checks bob in by name", "check_in", lambda: participants(teacher.put, uid="zz_bob", checked_in=True)),
                ("the teacher clears dave's check-in", "check_in", lambda: participants(teacher.put, uid="zz_dave", checked_in=False)),
                ("the teacher changes a photo's alt text", "files", lambda: teacher.put("/api/event/files", json={"id": ids["png"], "alt": "Changed after archiving"})),
            ]
            for label, action, send in refusals:
                r = send()
                check(49, f"on the archived event, {label} is 409 (not_open, action {action})",
                      r.status_code == 409 and code_of(r) == "not_open" and body_of(r).get("action") == action, f"{r.status_code} {body_of(r)}")
            r = teacher.delete("/api/event/files", json={"id": ids["png"]})
            check(49, "while a manager can still take a photo down (204)", r.status_code == 204, f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event/files", buffered=True, query_string={"id": ids["png"]})
            check(49, "and it is gone", r.status_code == 404, r.status_code)

        # ---------------------------------------------------------------- two workers at once
        with checking(50):
            from sqlalchemy import update
            from model.event import Event, EventParticipant
            real_lock, real_promote = Event._lock, Event._promote

            def closed_by_another_worker(event):
                """Another worker moves the event on just before this one takes the lock."""
                db.session.execute(update(Event).where(Event.id == event.id).values({Event._status: "active"}))
                real_lock(event)
            Event._lock = closed_by_another_worker
            try:
                answer = model_error(lambda: db.session.get(Event, ids["window"]).rsvp(person("zz_bob")))
            finally:
                Event._lock = real_lock
                db.session.rollback()
                db.session.execute(update(Event).where(Event.id == ids["window"]).values({Event._status: "registration"}))
                db.session.commit()
            bob_rows = EventParticipant.query.filter_by(_event_id=ids["window"], _user_id=person("zz_bob").id).count()
            check(50, "an RSVP reads the status after it takes the lock: closed by another worker in between, it is 409 not_open",
                  answer is not None and answer[0].get("code") == "not_open" and bob_rows == 0, f"{answer} rows={bob_rows}")

            order = []

            def spy(name, real):
                def wrapped(event):
                    order.append(name)
                    return real(event)
                return wrapped
            Event._lock, Event._promote = spy("lock", real_lock), spy("promote", real_promote)
            try:
                teacher.put("/api/event", json={"id": ids["window"], "registration": {"enabled": True, "capacity": 60}})
                grew = list(order)
                teacher.put("/api/event/participants", json={"event_id": ids["window"], "uid": "zz_bob", "role": "attendee"})
                del order[:]
                teacher.delete("/api/event/participants", json={"event_id": ids["window"], "uid": "zz_bob"})
                removed = list(order)
            finally:
                Event._lock, Event._promote = real_lock, real_promote
            check(50, "a capacity change takes the lock before the waitlist moves up", grew == ["lock", "promote"], grew)
            check(50, "and so does removing someone", removed == ["lock", "promote"], removed)

        with checking(51):
            from model.event import Event, EventParticipant
            real_participant = Event._participant

            def misses_once():
                """_participant as another worker's request sees it: the person's row is not saved yet, the first time."""
                missed = []

                def find(event, user):
                    if not missed:
                        missed.append(True)
                        return None
                    return real_participant(event, user)
                return find

            def at_once(send):
                Event._participant = misses_once()
                try:
                    return send()
                finally:
                    Event._participant = real_participant
            rows = lambda: EventParticipant.query.filter_by(_event_id=ids["window"], _user_id=person("zz_dave").id).count()
            dave.put("/api/event/rsvp", json={"event_id": ids["window"]})
            r = at_once(lambda: dave.put("/api/event/rsvp", json={"event_id": ids["window"]}))
            check(51, "two RSVPs of one person at once: the second is 200 and there is still one row",
                  r.status_code == 200 and body_of(r).get("rsvp") == "going" and rows() == 1, f"{r.status_code} {body_of(r)} rows={rows()}")
            r = at_once(lambda: teacher.put("/api/event/participants", json={"event_id": ids["window"], "uid": "zz_dave", "role": "attendee"}))
            check(51, "an organizer's change to the same person at once is 200, on the one row", r.status_code == 200 and rows() == 1, f"{r.status_code} {body_of(r)} rows={rows()}")
            status(teacher, ids["window"], "active")
            code = body_of(teacher.get("/api/event/checkin", query_string={"event_id": ids["window"]})).get("code")
            r = at_once(lambda: dave.post("/api/event/checkin", json={"event_id": ids["window"], "code": code}))
            check(51, "two check-ins of one person at once: 200, checked in once, on the one row",
                  r.status_code == 200 and body_of(r).get("checked_in_at") and rows() == 1, f"{r.status_code} {body_of(r)} rows={rows()}")

        # ---------------------------------------------------------------- the day the clocks change
        with checking(52):
            # On 2027-03-14 Los Angeles moves its clocks from 02:00 straight to 03:00
            spring_forward = lambda name, **times: teacher.post("/api/event", json=mort(f"/events/verify-dst-{name}/", date="2027-03-14", **times))
            r = spring_forward("gap", start_time="02:30", end_time="03:00")
            check(52, "a start_time the clocks skip (02:30) is 400 no_such_time, naming the field, time, date and zone",
                  r.status_code == 400 and code_of(r) == "no_such_time" and body_of(r).get("field") == "start_time" and body_of(r).get("time") == "02:30"
                  and body_of(r).get("date") == "2027-03-14" and body_of(r).get("timezone") == "America/Los_Angeles"
                  and "because the clocks change" in body_of(r).get("message", ""), f"{r.status_code} {body_of(r)}")
            r = spring_forward("gap-end", start_time="01:00", end_time="02:15")
            check(52, "an end_time the clocks skip is no_such_time too", r.status_code == 400 and code_of(r) == "no_such_time"
                  and body_of(r).get("field") == "end_time", f"{r.status_code} {body_of(r)}")
            r = spring_forward("across", start_time="01:30", end_time="03:00")
            check(52, "01:30 to 03:00 that day is half an hour: 09:30Z to 10:00Z", r.status_code == 201
                  and event_of(r).get("starts_at") == "2027-03-14T09:30:00.000Z" and event_of(r).get("ends_at") == "2027-03-14T10:00:00.000Z", f"{r.status_code} {body_of(r)}")
            r = spring_forward("all-day", start_time=None, end_time=None)
            check(52, "an all-day event that day lasts 23 hours", r.status_code == 201
                  and event_of(r).get("starts_at") == "2027-03-14T08:00:00.000Z" and event_of(r).get("ends_at") == "2027-03-15T07:00:00.000Z", f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- numbers too big for the database or the calendar
        with checking(53):
            from model import event_rules
            from model.event import Event
            first, last = event_rules.limit("first_year"), event_rules.limit("last_year")
            check(53, "event_rules.json's limits have first_year 2000 and last_year 2100, and GET options sends them",
                  (first, last) == (2000, 2100) and {k: body_of(anon.get("/api/event/options")).get("limits", {}).get(k) for k in ("first_year", "last_year")}
                  == {"first_year": 2000, "last_year": 2100}, (first, last))
            for day in ("1999-12-31", "2101-01-01", "9999-12-31"):
                r = teacher.post("/api/event", json=mort(f"/events/verify-year-{day[:4]}/", date=day))
                check(53, f"date {day} is 400 bad_date, which names the years", r.status_code == 400 and code_of(r) == "bad_date"
                      and body_of(r).get("first_year") == 2000 and body_of(r).get("last_year") == 2100 and "between 2000 and 2100" in body_of(r).get("message", ""),
                      f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event", query_string={"since": "1999-12-31"})
            check(53, "since before first_year is 400 bad_filter", r.status_code == 400 and code_of(r) == "bad_filter", f"{r.status_code} {body_of(r)}")
            from model import event as event_model
            check(53, "the database's integer limit is a named constant, 2**31 - 1", getattr(event_model, "DB_INT_MAX", None) == 2 ** 31 - 1)
            for capacity in (2 ** 31, 2 ** 64):
                r = teacher.put("/api/event", json={"id": ids["co"], "registration": {"enabled": True, "capacity": capacity}})
                check(53, f"capacity {capacity} is 400 bad_capacity", r.status_code == 400 and code_of(r) == "bad_capacity", f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event", json={"id": ids["co"], "registration": {"enabled": True, "capacity": 2 ** 31 - 1}})
            check(53, "capacity 2**31 - 1 is fine", r.status_code == 200, f"{r.status_code} {body_of(r)}")
            for label, send in (("?id= of 20 digits", lambda: anon.get("/api/event", query_string={"id": "9" * 20})),
                                ("?id= of 2**31", lambda: anon.get("/api/event", query_string={"id": str(2 ** 31)})),
                                ("a body id of 2**64", lambda: teacher.put("/api/event", json={"id": 2 ** 64, "title": "x"})),
                                ("an event_id of 2**64", lambda: alice.put("/api/event/rsvp", json={"event_id": 2 ** 64})),
                                ("a file ?id= of 2**64", lambda: anon.get("/api/event/files", query_string={"id": str(2 ** 64)}))):
                r = send()
                check(53, f"{label} is 404 not_found", r.status_code == 404 and code_of(r) == "not_found", f"{r.status_code} {body_of(r)}")
            r = anon.get("/api/event", query_string={"id": "0000000000" + str(ids["e1"])})
            check(53, "an id with leading zeros is still that id", r.status_code == 200 and event_of(r).get("id") == ids["e1"], f"{r.status_code}")
            broken = copy.deepcopy(rules)
            broken["limits"].update(first_year=2101)
            try:
                event_rules._check(broken)
                refused = False
            except ValueError as error:
                refused = "first_year" in str(error)
            check(53, "event_rules.json with first_year after last_year is refused, naming the key", refused)

        # ---------------------------------------------------------------- page_taken names a draft only to its managers
        with checking(54):
            march_path = "/events/verify-march/"  # the teacher's draft from check 3, which alice does not manage
            r = alice.put("/api/event", json={"id": ids["co"], "page_path": march_path})
            check(54, "alice, an organizer of another draft, moving it onto that address is 409 page_taken without the draft's id",
                  r.status_code == 409 and code_of(r) == "page_taken" and "id" not in body_of(r), f"{r.status_code} {body_of(r)}")
            r = teacher.put("/api/event", json={"id": ids["co"], "page_path": march_path})
            check(54, "the teacher, who can manage it, gets its id", r.status_code == 409 and code_of(r) == "page_taken"
                  and body_of(r).get("id") == ids["march"], f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- data the rules file does not know
        with checking(55):
            from sqlalchemy import update
            from model import event_rules
            from model.event import Event, EventFile
            from model.event_backup import import_events
            check(55, "a status event_rules.json does not list counts as private", event_rules.is_private("cancelled") and not event_rules.is_private("published"))
            ids["odd"] = event_of(teacher.post("/api/event", json=mort("/events/verify-unknown-status/"))).get("id")
            status(teacher, ids["odd"], "published")
            db.session.execute(update(Event).where(Event.id == ids["odd"]).values({Event._status: "cancelled"}))  # as a row left by an older rules file
            db.session.commit()
            r = anon.get("/api/event", query_string={"id": ids["odd"]})
            listed = [e.get("id") for e in body_of(anon.get("/api/event")).get("events", [])]
            check(55, "so an event with one is hidden from an anonymous read and the public list",
                  r.status_code == 404 and ids["odd"] not in listed, f"{r.status_code} listed={ids['odd'] in listed}")
            check(55, "while its managers still see it", teacher.get("/api/event", query_string={"id": ids["odd"]}).status_code == 200)

            moment = "2027-06-01T17:00:00.000000Z"
            event_row = lambda path, state: {"page_path": path, "title": "Imported", "owner": "zz_teacher", "starts_at": moment, "ends_at": moment,
                                             "timezone": "America/Los_Angeles", "status": state, "data": {"date": "2027-06-01"}}
            person_row = lambda uid, role, rsvp: {"event": "/events/verify-import/", "uid": uid, "role": role, "rsvp": rsvp}
            file_row = lambda **item: {"event": "/events/verify-import/", "group": "workshop-gallery", "kind": "gallery", "name": "x", "uploader_uid": "zz_teacher", **item}
            result = import_events({
                "events": [event_row("/events/verify-import/", "published"), event_row("/events/verify-import-odd/", "cancelled")],
                "event_participants": [person_row("zz_alice", "attendee", "going"), person_row("zz_bob", "boss", "going"), person_row("zz_dave", "attendee", "maybe")],
                "event_files": [file_row(key="901/aa.png", content_type="text/html", size=10, alt="A photo"),
                                file_row(key="901/bb.html", content_type="text/html", size=10),
                                file_row(link="https://evil.test\\@www.youtube.com/watch?v=x")],
            })
            counts = {table: (r["imported"], r["failed"]) for table, r in result.items()}
            check(55, "import refuses an event whose status the rules file does not list", counts.get("events") == (1, 1)
                  and "status" in " ".join(result["events"]["errors"]), result.get("events"))
            check(55, "and a participant whose role or rsvp the service does not have", counts.get("event_participants") == (1, 2)
                  and "role" in " ".join(result["event_participants"]["errors"]) and "rsvp" in " ".join(result["event_participants"]["errors"]), result.get("event_participants"))
            saved_type = (EventFile.query.filter_by(_key="901/aa.png").first() or EventFile).content_type
            check(55, "a stored file's content_type comes from its extension (image/png), never from the bundle (text/html)",
                  saved_type == "image/png", saved_type)
            check(55, "a stored file with an extension the service does not send, and a video link a page could not trust, are refused",
                  counts.get("event_files") == (1, 2), result.get("event_files"))

            youtube = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # already in co's gallery (check 42)
            for label, link in (("the same video link again", youtube), ("the same link written another way", "HTTPS://www.youtube.com/watch?v=dQw4w9WgXcQ")):
                r = upload(teacher, ids["co"], "gallery", "Recording again", link=link)
                check(55, f"{label} in the same group is 409 link_taken", r.status_code == 409 and code_of(r) == "link_taken", f"{r.status_code} {body_of(r)}")
            r = upload(teacher, ids["co"], "gallery", "Recording", link=youtube, group="other-gallery")
            check(55, "while another group can have it (201)", r.status_code == 201, f"{r.status_code} {body_of(r)}")

        # ---------------------------------------------------------------- removing a file while the store is down
        with checking(56):
            from model import event_storage as storage
            from model.event import EventFile
            disk, fake = storage.STORE, FakeS3()
            storage.STORE = storage.S3EventFileStore("verify-bucket", client=fake)
            kept = []

            class Keep(logging.Handler):
                def emit(self, record):
                    kept.append(record.getMessage())
            handlers, app.logger.handlers = app.logger.handlers, [Keep()]  # the warning is on purpose: keep it off the screen
            try:
                r = upload(teacher, ids["pa"], "gallery", "flyer.png", PNG, alt="The event flyer")
                file_id, key = (body_of(r).get("file") or {}).get("id"), (fake.puts[-1]["Key"] if fake.puts else "")
                fake.fail = True
                r = teacher.delete("/api/event/files", json={"id": file_id})
            finally:
                app.logger.handlers = handlers
                storage.STORE = disk
            check(56, "deleting a file while S3 is down is still 204: the row is saved first", r.status_code == 204, f"{r.status_code} {body_of(r)}")
            check(56, "and the row is gone", file_id and db.session.get(EventFile, file_id) is None)
            check(56, "and the bytes left behind are logged, with their key", any(key.split("/", 2)[-1] in message for message in kept), kept)

        # ---------------------------------------------------------------- error extras: last, so it reads every answer
        with checking(48):
            from model import event_rules
            from model.event import MESSAGES
            r = alice.put("/api/event/rsvp", json={"event_id": ids["noreg"]})
            check(48, "not_open names the action by its open_for key (rsvp); the message keeps the words",
                  r.status_code == 409 and code_of(r) == "not_open" and body_of(r).get("action") == "rsvp" and "RSVPs" in body_of(r).get("message", ""), body_of(r))
            text, items = event_rules.limit("text"), event_rules.limit("list")
            cases = [({"categories": "Community"}, {"field": "categories", "kind": "text_list", "limit": text, "items": items}),
                     ({"description": "d" * 501}, {"field": "description", "kind": "text", "limit": event_rules.limit("description")}),
                     ({"alert_days": 400}, {"field": "alert_days", "kind": "days", "limit": event_rules.limit("alert_days")}),
                     ({"location": "Room 12"}, {"field": "location", "kind": "object", "limit": None, "keys": ["name", "address", "url"]})]
            for change, want in cases:
                body = mort("/events/verify-extras/")
                body.update(change)
                r = teacher.post("/api/event", json=body)
                extras = {key: value for key, value in body_of(r).items() if key not in ("message", "code")}
                check(48, f"bad_field for {want['field']} carries its kind id and its limit as a number",
                      r.status_code == 400 and code_of(r) == "bad_field" and extras == want, extras)
            too_big = next((b for _, _, b in ANSWERS if isinstance(b, dict) and b.get("code") == "too_big"), {})
            check(48, "too_big carries max_bytes, a number", too_big.get("max_bytes") == event_rules.file_kind("document")["max_bytes"]
                  and "size" not in too_big, too_big)

            def is_id(value):
                if isinstance(value, list):
                    return all(is_id(item) for item in value)
                return value is None or (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, str) and re.fullmatch(r"\S+", value) is not None)
            english = [(path, b.get("code"), key, value) for path, status_code, b in ANSWERS
                       if status_code >= 400 and isinstance(b, dict) and b.get("code") in MESSAGES
                       for key, value in b.items() if key not in ("message", "code") and not is_id(value)]
            check(48, f"every extra field of the {len(ANSWERS)} answers this run is an id or a number, never English", not english, english[:4])

    with app.app_context():
        db.session.remove()
    first_engine.dispose()  # close the throwaway database, so -W default shows no unclosed connection

    with checking(38):
        real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
        check(38, "the real dev database was not touched", real_before == real_after)
        check(38, "and no file was written to instance/data/events", files_under(REAL_FILES) == real_files_before)

    failed = [n for n in range(1, TOTAL + 1) if not _results.get(n) or not all(_results[n])]
    for number in failed:
        if not _results.get(number):
            print(f"FAIL  {number:>2}. did not run")
    print(f"\n{'ALL PASSED' if not failed else f'{len(failed)} FAILED: ' + ', '.join(map(str, failed))} ({TOTAL - len(failed)} of {TOTAL} checks pass)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
