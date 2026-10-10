"""Account types and their verification rules, read from account_types.json so a fork of
OCS changes data, not code.

Each account type has:
  role                     the User role an account of this type is created with
  google_verified_domains  email domains Google can verify for this type. If set, the account
                           is activated when Google verifies an email in one of them, and a
                           Teacher/Admin does not verify it. If empty, the account is created
                           inactive and a Teacher/Admin verifies it.
  staff_created            whether a Teacher/Admin may create this type directly
verification_interval_days is how long a verification stays current. default_account_type is
used when a signup names no type, or one that is not listed.
"""
import json
import os


def _read():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_types.json")
    with open(path) as fh:
        data = json.load(fh)
    if data["default_account_type"] not in data["account_types"]:
        raise ValueError("account_types.json: default_account_type is not a listed account type")
    return data


_data = _read()


def get(name):
    """The account type called name, or the default type when name is empty or unknown."""
    types = _data["account_types"]
    return types.get((name or "").lower()) or types[_data["default_account_type"]]


def oauth_roles():
    """Roles whose accounts are verified through Google, not by a Teacher or Admin."""
    return {t["role"] for t in _data["account_types"].values() if t["google_verified_domains"]}


def staff_created():
    """Account type name -> role, for the types a Teacher or Admin may create directly."""
    return {name: t["role"] for name, t in _data["account_types"].items() if t["staff_created"]}


def verification_interval_days():
    return _data["verification_interval_days"]
