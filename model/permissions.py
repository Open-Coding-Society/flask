"""Who may make each request, read from dataconfigs/permissions.json so a fork of OCS changes data, not code.

A request is written "METHOD route", with the route as Flask knows it: "GET /api/id",
"POST /api/user/<string:uid>/verify". Every request the app answers is in exactly one of these lists, and a request
in none of them is one nobody may make:

  public               anyone may make it, with or without an account, and that is on purpose (the home page, login,
                       sign-up)
  public_restricted    open on purpose too, but it runs code or spends a provider's credit, so it has been reviewed
                       to run in a sandbox and under a rate limit. Nothing goes here without that review.
  legacy_open          anyone can make it today because it always was so, and nobody has yet decided that it should
                       be. Kept in groups, each with an owner, so the list gets shorter.
  device_key           made by a device that sends the device key; the route checks the key itself
  session              a page behind the Flask session login
  signed_in            any active account
  roles                for each role, the requests only some roles may make. A request can be in several roles' lists.

  open_in_development  requests that are open while IS_PRODUCTION is not true, and in production are held to the
                       signed_in or roles list they are also in

  enforce              the callers this file is enforced for: "anonymous" (no usable login) and role names. A caller
                       in it who is not allowed is refused before the route (api/access.py). For every other
                       caller the file is only compared with what the route does, and a difference is logged.

This file says who gets as far as the route. A check written inside a route (that an account may change only its own
record, for one) is still made there.
"""
import json
import os
import re

from model import account_types

OPEN_TO_ANYONE = ("public", "public_restricted", "legacy_open")
LISTS = ("public", "public_restricted", "open_in_development", "device_key", "session", "signed_in")
KEYS = LISTS + ("legacy_open", "roles", "enforce")
ANONYMOUS = "anonymous"
_REQUEST = re.compile(r"(GET|POST|PUT|PATCH|DELETE) /\S*")


def _requests(value, where):
    if not isinstance(value, list) or not all(isinstance(item, str) and _REQUEST.fullmatch(item) for item in value):
        raise ValueError(f"permissions.json: {where} must be a list of 'METHOD /route' entries")
    if len(set(value)) != len(value):
        repeated = sorted({item for item in value if value.count(item) > 1})[0]
        raise ValueError(f"permissions.json: {where} lists {repeated} twice")
    return value


def _check(data, known_roles):
    """Refuse data that is unclear about who may make a request, so a typo fails at startup."""
    for key in KEYS:
        if key not in data:
            raise ValueError(f"permissions.json: {key} is missing")
    for key in data:
        if key not in KEYS:
            raise ValueError(f"permissions.json: {key} is not a list this file has")
    where = {}  # request -> the one list that says who may make it

    def place(request, name):
        if request in where:
            raise ValueError(f"permissions.json: {request} is in {where[request]} and in {name}; it can be in only one")
        where[request] = name

    for name in LISTS:
        for request in _requests(data[name], name):
            if name != "open_in_development":
                place(request, name)
    if not isinstance(data["legacy_open"], list):
        raise ValueError("permissions.json: legacy_open must be a list of groups")
    for number, group in enumerate(data["legacy_open"], 1):
        name = f"legacy_open group {number}"
        if not isinstance(group, dict) or not isinstance(group.get("owner"), str) or not group["owner"].strip():
            raise ValueError(f"permissions.json: {name} needs an owner")
        if not group.get("requests"):
            raise ValueError(f"permissions.json: {name} lists no requests")
        for request in _requests(group["requests"], name):
            place(request, "legacy_open")
    if not isinstance(data["roles"], dict):
        raise ValueError("permissions.json: roles must give a list of requests for each role")
    in_a_role_list = set()
    for role, requests in data["roles"].items():
        if role not in known_roles:
            raise ValueError(f"permissions.json: roles names {role}, which is not a role in account_types.json")
        for request in _requests(requests, f"roles.{role}"):
            if request in where:
                raise ValueError(f"permissions.json: {request} is in {where[request]} and in roles.{role}; it can be in only one")
            in_a_role_list.add(request)
    for request in data["open_in_development"]:
        if where.get(request) != "signed_in" and request not in in_a_role_list:
            raise ValueError(f"permissions.json: {request} is open in development, so signed_in or roles must say who may make it in production")
    enforce = data["enforce"]
    if not isinstance(enforce, list) or len(set(map(str, enforce))) != len(enforce):
        raise ValueError("permissions.json: enforce must be a list of callers, each once")
    for caller in enforce:
        if caller != ANONYMOUS and caller not in known_roles:
            raise ValueError(f"permissions.json: enforce names {caller}, which is not \"anonymous\" or a role in account_types.json")


def _read():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataconfigs", "permissions.json")
    with open(path) as fh:
        data = json.load(fh)
    _check(data, set(account_types.assignable_roles()) | set(account_types.guest_roles()))
    return data


_data = _read()
_legacy_open = frozenset(request for group in _data["legacy_open"] for request in group["requests"])
_in = {name: frozenset(_data[name]) for name in LISTS}
_in["legacy_open"] = _legacy_open
_enforce = frozenset(_data["enforce"])


def who_may_call(method, route, production=True):
    """Who gets as far as the route for this request: "anyone", "device_key", "session", "signed_in", a sorted list
    of roles, or None when the request is in no list."""
    request = f"{method} {route}"
    if any(request in _in[name] for name in OPEN_TO_ANYONE) or (not production and request in _in["open_in_development"]):
        return "anyone"
    for name in ("device_key", "session", "signed_in"):
        if request in _in[name]:
            return name
    roles = sorted(role for role, requests in _data["roles"].items() if request in requests)
    return roles or None


def listed_as(method, route):
    """The names of the lists this request is in, for a message or a report."""
    request = f"{method} {route}"
    return [name for name in LISTS + ("legacy_open",) if request in _in[name]] + \
           [f"roles.{role}" for role, requests in _data["roles"].items() if request in requests]


def every_request():
    """Every request the file lists."""
    listed = set().union(*_in.values())
    for requests in _data["roles"].values():
        listed.update(requests)
    return listed


def enforced(caller):
    """Whether the file is enforced for this caller: "anonymous" or a role."""
    return caller in _enforce


def enforcing():
    """Whether the file is enforced for anyone at all."""
    return bool(_enforce)


def legacy_open_groups():
    """The legacy_open groups: [{owner, requests}]."""
    return [dict(group) for group in _data["legacy_open"]]
