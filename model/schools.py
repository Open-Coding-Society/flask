"""The schools a student can choose when signing up, read from dataconfigs/schools.json so a fork of OCS
changes data, not code.

Each school has a name, which is what is saved on the account, and a label, which is what the
sign-up form shows. Nothing checks an account's school against this list: it is what the form
offers, not a rule about who can have an account.
"""
import json
import os


def _check(data):
    """Refuse data the sign-up form could not use, so a typo fails at startup."""
    schools = data.get("schools")
    if not isinstance(schools, list) or not schools:
        raise ValueError("schools.json: schools must be a list with at least one school")
    names = set()
    for school in schools:
        if not (isinstance(school, dict) and set(school) == {"name", "label"}):
            raise ValueError("schools.json: each school must be an object with a name and a label")
        for key in ("name", "label"):
            if not isinstance(school[key], str) or not school[key].strip():
                raise ValueError(f"schools.json: every school needs a {key} that is text")
        if school["name"] in names:
            raise ValueError(f"schools.json: two schools are named {school['name']!r}")
        names.add(school["name"])


def _read():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataconfigs", "schools.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    _check(data)
    return data


_data = _read()


def schools():
    """The schools to offer, in order: [{"name": ..., "label": ...}, ...]."""
    return [dict(school) for school in _data["schools"]]
