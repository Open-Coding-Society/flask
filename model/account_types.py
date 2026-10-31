"""Account types and their verification rules, read from dataconfigs/account_types.json so a fork of
OCS changes data, not code.

Each account type has:
  role                     the User role an account of this type is created with
  google_verified_domains  email domains Google can verify for this type. If set, the account
                           is activated at sign-up when Google verifies an email in one of them.
                           If empty, the account is created inactive until a Teacher/Admin
                           activates it. A Teacher/Admin can activate any account.
  staff_created            whether a Teacher/Admin may create this type directly
  verification_days        optional. How many days a Teacher/Admin's activation covers for this
                           type (a Mentor's 365). Without it the Teacher/Admin is asked, and
                           default_verification_days is the suggested answer.
  github_check             optional, true by default. Whether the uid must be a real GitHub account
                           at sign-up. A type that turns it off must have allowed_requests, and its
                           role cannot be an admin or staff role.
  active_on_signup         optional, false by default. The account is active as soon as it is created.
  lifetime_hours           optional. The account is refused this many hours after it was activated,
                           and made inactive. Its record is kept.
  requires_open_window     optional, false by default. The account can be created, or come back after
                           its lifetime, only while a Teacher or Admin has the Guest Window open
                           (see model/guest_window.py). A type that skips the GitHub check needs it.
  window_max_minutes       how long the Guest Window can be opened for. Needed with requires_open_window.
  requests_per_minute      optional. The most requests one account of this type can make in a minute.
  allowed_requests         optional. When present the account can only make these requests, written
                           "METHOD /path" (for example "GET /api/id"), and is refused everything else.
verification_interval_days is how long a verification stays current, and the most a Teacher/Admin
can grant. verification_exempt_roles are never re-verified. default_account_type is used when a
signup names no type, or one that is not listed. essential_roles are the roles of which at least
one active account must always remain. admin_role is the one role that can do everything, and the
only one that can make another account the same. staff_roles are the roles that manage accounts:
they see the staff controls in the Account Administration screen.
"""
import json
import os
import re


def _check(data):
    """Refuse data that would leave nobody able to administer, so a typo fails at startup."""
    if data["default_account_type"] not in data["account_types"]:
        raise ValueError("account_types.json: default_account_type is not a listed account type")
    for key in ("verification_interval_days", "default_verification_days", "verification_exempt_roles",
                "admin_role", "essential_roles", "staff_roles"):
        if key not in data:
            raise ValueError(f"account_types.json: {key} is missing")
    if data["admin_role"] not in data["staff_roles"]:
        raise ValueError("account_types.json: admin_role must be one of the staff_roles")
    roles = [t["role"] for t in data["account_types"].values()]
    if len(set(roles)) != len(roles):
        raise ValueError("account_types.json: every account type needs its own role")
    privileged = set(data["staff_roles"]) | {data["admin_role"]}
    for name, account_type in data["account_types"].items():
        allowed = account_type.get("allowed_requests")
        if allowed is not None and not (isinstance(allowed, list) and allowed and all(
                isinstance(item, str) and re.fullmatch(r"(GET|POST|PUT|PATCH|DELETE) /\S*", item) for item in allowed)):
            raise ValueError(f"account_types.json: {name}.allowed_requests must be a list of 'METHOD /path' entries")
        if account_type.get("github_check", True) is False:
            if allowed is None:
                raise ValueError(f"account_types.json: {name} skips the GitHub check, so it needs allowed_requests")
            if account_type["role"] in privileged:
                raise ValueError(f"account_types.json: {name} skips the GitHub check, so its role cannot be a staff role")
            if account_type.get("requires_open_window") is not True:
                raise ValueError(f"account_types.json: {name} skips the GitHub check, so it needs requires_open_window")
        for flag in ("github_check", "active_on_signup", "requires_open_window"):
            if flag in account_type and not isinstance(account_type[flag], bool):
                raise ValueError(f"account_types.json: {name}.{flag} must be true or false")
        for key in ("lifetime_hours", "window_max_minutes", "requests_per_minute"):
            number = account_type.get(key)
            if key in account_type and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
                raise ValueError(f"account_types.json: {name}.{key} must be a whole number, 1 or more")
        if account_type.get("requires_open_window") and "window_max_minutes" not in account_type:
            raise ValueError(f"account_types.json: {name} needs window_max_minutes")


def _read():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataconfigs", "account_types.json")
    with open(path) as fh:
        data = json.load(fh)
    _check(data)
    return data


_data = _read()


def get(name):
    """The account type called name, or the default type when name is empty or unknown."""
    types = _data["account_types"]
    return types.get((name or "").lower()) or types[_data["default_account_type"]]


def google_domains(role):
    """The email domains Google can verify for accounts of this role. Empty when staff verify
    them instead."""
    return tuple(d for t in _data["account_types"].values() if t["role"] == role for d in t.get("google_verified_domains", ()))


def verification_exempt_roles():
    """Roles that are never re-verified: they do not need annual verification."""
    return tuple(_data["verification_exempt_roles"])


def default_verification_days():
    """The suggested number of days when a Teacher/Admin is asked how long to verify for."""
    return _data["default_verification_days"]


def fixed_verification_days(role):
    """The days a Teacher/Admin's activation covers for this role when it is not their choice:
    the whole interval for a role that is never re-verified, or the account type's own number.
    None means the Teacher/Admin chooses."""
    if role in verification_exempt_roles():
        return verification_interval_days()
    for account_type in _data["account_types"].values():
        if account_type["role"] == role and account_type.get("verification_days"):
            return account_type["verification_days"]
    return None


def staff_created():
    """Account type name -> role, for the types a Teacher or Admin may create directly."""
    return {name: t["role"] for name, t in _data["account_types"].items() if t["staff_created"]}


def verification_interval_days():
    return _data["verification_interval_days"]


def essential_roles():
    """Roles of which at least one active account must always remain."""
    return tuple(_data["essential_roles"])


def staff_roles():
    """Roles that manage accounts."""
    return tuple(_data["staff_roles"])


def assignable_roles():
    """Every role an existing account can be given: the account types' roles, then the staff roles.
    A type limited to a list of requests is for sign-up only: giving it to an existing account
    would limit that account at once, and its lifetime could end it."""
    roles = [t["role"] for t in _data["account_types"].values() if "allowed_requests" not in t]
    return tuple(roles + [r for r in _data["staff_roles"] if r not in roles])


def admin_role():
    """The role that can do everything, and the only one that can make another account the same."""
    return _data["admin_role"]


def needs_github_check(account_type):
    """Whether a sign-up of this account type must use a uid that is a real GitHub account."""
    return account_type.get("github_check", True)


def active_on_signup(account_type):
    """Whether an account of this type is active as soon as it is created."""
    return bool(account_type.get("active_on_signup", False))


def _type_for_role(role):
    return next((t for t in _data["account_types"].values() if t["role"] == role), None)


def allowed_requests(role):
    """The "METHOD /path" requests an account of this role is limited to, or None when it is not
    limited."""
    account_type = _type_for_role(role)
    allowed = account_type.get("allowed_requests") if account_type else None
    return None if allowed is None else tuple(allowed)


def lifetime_hours(role):
    """How many hours an account of this role lasts after it was activated, or None for no limit."""
    account_type = _type_for_role(role)
    return account_type.get("lifetime_hours") if account_type else None


def request_allowed(role, method, path):
    """False when an account of this role is limited to a list of requests and this one is not on
    it. A trailing slash on the path does not matter."""
    allowed = allowed_requests(role)
    return allowed is None or f"{method} {path.rstrip('/') or '/'}" in allowed



def requires_open_window(account_type):
    """Whether accounts of this type can be created, or come back, only during the Guest Window."""
    return bool(account_type.get("requires_open_window", False))


def role_requires_open_window(role):
    account_type = _type_for_role(role)
    return account_type is not None and requires_open_window(account_type)


def guest_roles():
    """The roles of the account types that need the Guest Window."""
    return tuple(t["role"] for t in _data["account_types"].values() if requires_open_window(t))


def window_max_minutes():
    """The longest the Guest Window can be opened for."""
    return max((t["window_max_minutes"] for t in _data["account_types"].values() if requires_open_window(t)), default=0)


def requests_per_minute(role):
    """The most requests one account of this role can make in a minute, or None for no limit."""
    account_type = _type_for_role(role)
    return account_type.get("requests_per_minute") if account_type else None
