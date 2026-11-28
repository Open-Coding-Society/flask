"""The Event Service's rules, read from dataconfigs/event_rules.json so a fork of OCS changes data, not code.

  page_prefix            where every event page lives (/events/). An event's page_path is this plus a slug.
  default_timezone       the zone of an event that names none. Pages always sends the zone it resolved from
                         events.timezone in its _config.yml, so this meets only someone calling the API directly.
                         Keep the two the same.
  statuses               the lifecycle, in order. The first is where every event starts.
  private_statuses       statuses only the event's managers can see (a draft). Leaving one is publishing.
  transitions            for each status, the statuses it can move to.
  open_for               for each action (edit, rsvp, check_in, files, delete), the statuses that allow it.
  publish_roles          the account roles that may publish an event. null means the staff_roles in
                         account_types.json.
  participant_roles      the roles a person can have in one event. The first is the one a new participant gets.
  manager_roles          the participant roles that may manage the event, besides its owner and staff.
  features               the parts an event can turn on (the features block of the event's front matter).
  file_kinds             for each kind of file, the extensions it takes and its largest size in bytes.
  inline_extensions      the files a browser may show in place. Every other file downloads.
  video_hosts            the hosts a hosted video link in the gallery may point at.
  check_in_code_seconds  how long one check-in code lasts. The code before it is still accepted.
  s3_prefix              where event files go in the S3 bucket, when S3 is set up.
  limits                 the most characters (text, description, body), items (list) and days (alert_days) a
                         field of an event's definition may have, and the first and last year an event's date
                         (and a list's since) may be in (first_year, last_year).

Code never names a status or a participant role: it asks this module, so a kit changes the lifecycle or adds a
role (say helper) by editing the data file alone. The file is read once, at startup, and bad data stops the app
there with a message naming the key. load(path) reads another copy instead, which verify_events.py uses.
"""
import json
import os
import re

from model import account_types
from model.presence_schedule import ScheduleError, check_timezone

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataconfigs", "event_rules.json")
KEYS = ("page_prefix", "default_timezone", "statuses", "private_statuses", "transitions", "open_for",
        "publish_roles", "participant_roles", "manager_roles", "features", "file_kinds", "inline_extensions",
        "video_hosts", "check_in_code_seconds", "s3_prefix", "limits")
ACTIONS = ("edit", "rsvp", "check_in", "files", "delete")
LIMITS = ("text", "description", "body", "list", "alert_days", "first_year", "last_year")
YEARS = (1, 9998)  # the years datetime has, less the last: an all-day event on its last day would end in year 10000
MAX_BYTES_CAP = 20000000  # so a typo in max_bytes cannot open the upload route to huge bodies
WORD = re.compile(r"[a-z]+(_[a-z]+)*")  # statuses and roles are stored in 16-character columns and sent in queries
EXTENSION = re.compile(r"[a-z0-9]+")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def _fail(key, problem):
    raise ValueError(f"event_rules.json: {key} {problem}")


def _words(value, key, what="lowercase words of up to 16 letters"):
    """A list that is not empty, with no value twice, each one a lowercase word."""
    if not isinstance(value, list) or not value:
        _fail(key, f"must be a list of {what}")
    if len(set(value)) != len(value):
        _fail(key, "repeats a value")
    if not all(isinstance(item, str) and WORD.fullmatch(item) and len(item) <= 16 for item in value):
        _fail(key, f"must be a list of {what}")
    return value


def _whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _check(data):
    """Refuse data that would leave an event in a state the service cannot reason about, so a typo fails at startup."""
    if not isinstance(data, dict):
        raise ValueError("event_rules.json: must be a JSON object")
    for key in KEYS:
        if key not in data:
            _fail(key, "is missing")
    for key in data:
        if key not in KEYS:
            _fail(key, "is not a key this file has")

    statuses = _words(data["statuses"], "statuses")
    if not isinstance(data["private_statuses"], list) or not set(data["private_statuses"]) <= set(statuses):
        _fail("private_statuses", "must be a list of statuses")
    transitions = data["transitions"]
    if not isinstance(transitions, dict) or set(transitions) != set(statuses):
        _fail("transitions", "must give the moves for exactly the statuses")
    for status, targets in transitions.items():
        if not isinstance(targets, list) or not set(targets) <= set(statuses):
            _fail("transitions", f"of {status} names a status that is not in statuses")
    open_for = data["open_for"]
    if not isinstance(open_for, dict) or set(open_for) != set(ACTIONS):
        _fail("open_for", f"must give the statuses for exactly {', '.join(ACTIONS)}")
    for action, allowed in open_for.items():
        if not isinstance(allowed, list) or not set(allowed) <= set(statuses):
            _fail("open_for", f"of {action} names a status that is not in statuses")

    roles = data["publish_roles"]
    known = set(account_types.assignable_roles())
    if roles is not None and not (isinstance(roles, list) and roles and all(role in known for role in roles)):
        _fail("publish_roles", f"must be null (the staff roles) or a list of roles from account_types.json: {', '.join(sorted(known))}")
    participant_roles = _words(data["participant_roles"], "participant_roles")
    if not isinstance(data["manager_roles"], list) or not set(data["manager_roles"]) <= set(participant_roles):
        _fail("manager_roles", "must be a list of participant_roles")
    _words(data["features"], "features")

    kinds = data["file_kinds"]
    if not isinstance(kinds, dict) or not kinds:
        _fail("file_kinds", "must give at least one kind of file")
    every_extension = set()
    for kind, rule in kinds.items():
        if not (isinstance(kind, str) and WORD.fullmatch(kind) and len(kind) <= 16):
            _fail("file_kinds", f"{kind!r} must be a lowercase word of up to 16 letters")
        if not isinstance(rule, dict) or set(rule) != {"extensions", "max_bytes"}:
            _fail(f"file_kinds.{kind}", "must have extensions and max_bytes")
        extensions = rule["extensions"]
        if not isinstance(extensions, list) or not extensions or not all(isinstance(e, str) and EXTENSION.fullmatch(e) for e in extensions):
            _fail(f"file_kinds.{kind}.extensions", "must be a list of lowercase letters and digits, like pdf")
        every_extension.update(extensions)
        if not _whole(rule["max_bytes"], 1, MAX_BYTES_CAP):
            _fail(f"file_kinds.{kind}.max_bytes", f"must be a whole number from 1 to {MAX_BYTES_CAP:,}")
    inline = data["inline_extensions"]
    if not isinstance(inline, list) or not set(inline) <= every_extension:
        _fail("inline_extensions", "must be a list of extensions the file kinds take")
    hosts = data["video_hosts"]
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and HOST.fullmatch(h) for h in hosts):
        _fail("video_hosts", "must be a list of lowercase host names, like www.youtube.com")
    if not _whole(data["check_in_code_seconds"], 30, 3600):
        _fail("check_in_code_seconds", "must be a whole number from 30 to 3600")
    for key in ("page_prefix", "s3_prefix"):
        if not isinstance(data[key], str) or not data[key].endswith("/"):
            _fail(key, "must end in /")
    if not data["page_prefix"].startswith("/"):
        _fail("page_prefix", "must be a site path that starts with /")
    try:
        if not isinstance(data["default_timezone"], str) or check_timezone(data["default_timezone"]) is None:
            raise ScheduleError("missing")
    except ScheduleError:
        _fail("default_timezone", f"{data['default_timezone']!r} is not a timezone, like America/Los_Angeles")
    limits = data["limits"]
    if not isinstance(limits, dict) or set(limits) != set(LIMITS):
        _fail("limits", f"must give exactly {', '.join(LIMITS)}")
    for name, value in limits.items():
        if not _whole(value, 1, 10 ** 9):
            _fail(f"limits.{name}", "must be a positive whole number")
    first, last = limits["first_year"], limits["last_year"]
    if not (_whole(first, *YEARS) and _whole(last, first, YEARS[1])):
        _fail("limits.first_year", f"and last_year must be years from {YEARS[0]} to {YEARS[1]}, first_year no later than last_year")


_data = {}


def load(path=PATH):
    """Read and check the rules from path (the dataconfigs file unless a test gives an edited copy)."""
    global _data
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    _check(data)
    _data = data


load()


def page_prefix():
    return _data["page_prefix"]


def default_timezone():
    return _data["default_timezone"]


def statuses():
    return tuple(_data["statuses"])


def initial_status():
    """Where every event starts: the first status."""
    return _data["statuses"][0]


def is_private(status):
    """Whether only the event's managers can see an event in this status. A status the file does not list (a row
    left by an older file, say) is private too, so an event never shows to everyone by mistake."""
    return status in _data["private_statuses"] or status not in _data["statuses"]


def next_statuses(status):
    """The statuses an event in this status can move to, in the file's order."""
    return list(_data["transitions"].get(status, []))


def open_for(action, status):
    """Whether an event in this status allows the action: edit, rsvp, check_in, files or delete."""
    return status in _data["open_for"][action]


def publish_roles():
    """The account roles that may publish an event."""
    roles = _data["publish_roles"]
    return account_types.staff_roles() if roles is None else tuple(roles)


def participant_roles():
    return tuple(_data["participant_roles"])


def initial_role():
    """The role a new participant gets: the first participant role."""
    return _data["participant_roles"][0]


def is_manager_role(role):
    """Whether a participant with this role may manage the event."""
    return role in _data["manager_roles"]


def features():
    return tuple(_data["features"])


def kinds():
    return tuple(_data["file_kinds"])


def file_kind(kind):
    """{extensions, max_bytes} for a kind of file, or None when there is no such kind."""
    rule = _data["file_kinds"].get(kind)
    return None if rule is None else {"extensions": list(rule["extensions"]), "max_bytes": rule["max_bytes"]}


def upload_cap():
    """The body the upload route may read: the largest file as base64 (4/3 of its size) plus 64 KB for the rest."""
    largest = max(rule["max_bytes"] for rule in _data["file_kinds"].values())
    return largest * 4 // 3 + 64 * 1024


def is_inline(extension):
    return extension in _data["inline_extensions"]


def video_hosts():
    return tuple(_data["video_hosts"])


def is_video_host(host):
    return (host or "").lower() in _data["video_hosts"]


def code_seconds():
    return _data["check_in_code_seconds"]


def s3_prefix():
    return _data["s3_prefix"]


def limit(name):
    return _data["limits"][name]


def public_settings():
    """Everything a page needs to check an event before sending it: the file without s3_prefix, with publish_roles
    resolved to role names."""
    settings = {key: json.loads(json.dumps(_data[key])) for key in KEYS if key != "s3_prefix"}
    settings["publish_roles"] = list(publish_roles())
    return settings
