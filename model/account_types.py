"""Account types and their verification rules, read from account_types.json so a fork of
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
verification_interval_days is how long a verification stays current, and the most a Teacher/Admin
can grant. verification_exempt_roles are never re-verified. default_account_type is used when a
signup names no type, or one that is not listed.
"""
import json
import os


def _read():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_types.json")
    with open(path) as fh:
        data = json.load(fh)
    if data["default_account_type"] not in data["account_types"]:
        raise ValueError("account_types.json: default_account_type is not a listed account type")
    for key in ("verification_interval_days", "default_verification_days", "verification_exempt_roles"):
        if key not in data:
            raise ValueError(f"account_types.json: {key} is missing")
    return data


_data = _read()


def get(name):
    """The account type called name, or the default type when name is empty or unknown."""
    types = _data["account_types"]
    return types.get((name or "").lower()) or types[_data["default_account_type"]]


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
